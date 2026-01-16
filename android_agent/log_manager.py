import subprocess
import time
import os
import signal
import sys
from typing import Dict, List, Optional
from pathlib import Path

MAX_LOG_COLLECTORS = 80
SPAWN_DELAY = 0.1  # 100ms delay giữa các spawn để tránh spike ADB

def get_process_group_info_safe(proc: subprocess.Popen) -> Dict[str, Optional[int]]:
    """Get process group info một cách an toàn"""
    try:
        pid = proc.pid
        if not pid:
            return {}

        info = {'pid': pid}

        if os.name == 'nt':
            # Windows: Process created with CREATE_NEW_PROCESS_GROUP
            # Group ID = Process ID for group leaders
            info['pgid'] = pid
            info['is_group_leader'] = True
        else:
            # Unix/Linux/macOS: Safe PGID retrieval
            try:
                pgid = os.getpgid(pid)
                info['pgid'] = pgid
                info['is_group_leader'] = (pgid == pid)
            except (OSError, ProcessLookupError):
                info['pgid'] = None
                info['is_group_leader'] = False

        return info
    except Exception:
        return {'pid': proc.pid if proc.pid else None}

def kill_process_group_safe(pgid: Optional[int], pid: int, serial: str) -> bool:
    """
    Kill process group một cách AN TOÀN - tránh tự sát agent

    Args:
        pgid: Process Group ID (None nếu không lấy được)
        pid: Process ID
        serial: Device serial for logging

    Returns:
        bool: True nếu kill thành công
    """
    if os.name == 'nt':
        # --- WINDOWS: Safe tree killing ---
        # Windows handles process trees very well via taskkill /T
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if result.returncode == 0:
                print(f"[log_manager] ✓ Killed process tree for {serial} (PID: {pid}) via taskkill")
                return True
        except subprocess.TimeoutExpired:
            print(f"[log_manager] Taskkill timeout for {serial}")
        except Exception as e:
            print(f"[log_manager] Taskkill failed for {serial}: {e}")

        # Fallback: Single process kill
        try:
            os.kill(pid, signal.SIGTERM)  # Graceful first
            time.sleep(0.5)
            # Check if still alive
            try:
                os.waitpid(pid, os.WNOHANG)
                # If we get here, process was waited on (dead)
                return True
            except OSError:
                # Process still alive, force kill
                os.kill(pid, signal.SIGKILL)
                return True
        except OSError:
            return False

    else:
        # --- UNIX/LINUX/MACOS: CRITICAL SAFETY CHECK ---
        if pgid is None:
            # Không lấy được PGID, fallback to single process
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"[log_manager] ✓ Killed single process {pid} for {serial} (no PGID)")
                return True
            except OSError:
                return False

        try:
            # 🔴 CRITICAL SAFETY CHECK: Prevent suicide
            parent_pgid = os.getpgrp()  # Get parent's PGID

            if pgid == parent_pgid:
                # ⚠️ DANGER: Child shares PGID with parent agent
                print(f"[log_manager] ⚠️ Child {pid} shares PGID {pgid} with Parent. SKIPPING Group Kill to avoid suicide.")

                # Safe: Kill only the child process
                os.kill(pid, signal.SIGKILL)
                print(f"[log_manager] ✓ Safely killed single process {pid} for {serial}")
                return True

            # ✅ SAFE: Child has different PGID, kill entire group
            print(f"[log_manager] Killing process group {pgid} for {serial}...")
            os.killpg(pgid, signal.SIGKILL)
            print(f"[log_manager] ✓ Killed process group {pgid} for {serial}")
            return True

        except ProcessLookupError:
            # Process already dead
            return True
        except OSError as e:
            print(f"[log_manager] Group kill failed for {serial}: {e}")

            # Fallback: Try single process kill
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"[log_manager] ✓ Fallback: Killed single process {pid} for {serial}")
                return True
            except OSError:
                return False

def _force_kill_windows(proc: subprocess.Popen, serial: str) -> bool:
    """Windows-specific force kill với process group awareness"""
    # [FIX] Thử dừng nhẹ nhàng bằng CTRL_BREAK trước để log_data kịp gửi API
    try:
        print(f"[log_manager] Sending CTRL_BREAK to {serial} (PID: {proc.pid})...")
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            # Chờ tối đa 5s để process kịp gửi API (timeout của request là 3s)
            proc.wait(timeout=5.0)
            print(f"[log_manager] ✓ Gracefully stopped {serial}")
            return True
        except subprocess.TimeoutExpired:
            print(f"[log_manager] Graceful stop timed out for {serial}, escalating...")
    except Exception as e:
        print(f"[log_manager] Failed to send CTRL_BREAK to {serial}: {e}")

    # Get process group info
    pg_info = get_process_group_info_safe(proc)
    pgid = pg_info.get('pgid')

    # Use safe process group kill
    return kill_process_group_safe(pgid, proc.pid, serial)

def _force_kill_unix(proc: subprocess.Popen, serial: str) -> bool:
    """Unix/Linux/macOS specific force kill với process group safety"""
    # Get process group info
    pg_info = get_process_group_info_safe(proc)
    pgid = pg_info.get('pgid')

    # Use safe process group kill
    return kill_process_group_safe(pgid, proc.pid, serial)

def _force_kill_unix(proc: subprocess.Popen, serial: str) -> bool:
    """Unix/Linux/macOS specific force kill với process group support"""
    try:
        # Strategy 1: Graceful terminate
        print(f"[log_manager] Terminating {serial}...")
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
            print(f"[log_manager] ✓ Gracefully terminated {serial}")
            return True
        except subprocess.TimeoutExpired:
            print(f"[log_manager] Timeout, escalating kill for {serial}...")

        # Strategy 2: Process group kill (if process is session leader)
        can_use_group_kill = False
        try:
            pgid = os.getpgid(proc.pid)
            can_use_group_kill = (pgid == proc.pid)  # Is session leader?
        except (OSError, ProcessLookupError):
            can_use_group_kill = False

        if can_use_group_kill:
            try:
                print(f"[log_manager] Killing process group for {serial}...")
                os.killpg(pgid, signal.SIGKILL)
                time.sleep(0.5)  # Brief wait for group kill
                if proc.poll() is None:  # Check if still running
                    print(f"[log_manager] ✓ Process group kill successful for {serial}")
                    return True
                else:
                    print(f"[log_manager] Process group kill may have worked for {serial}")
            except (OSError, ProcessLookupError) as e:
                print(f"[log_manager] Process group kill failed for {serial}: {e}")

        # Strategy 3: Single process kill
        print(f"[log_manager] Force killing process {proc.pid} for {serial}...")
        proc.kill()
        try:
            proc.wait(timeout=1.0)
            print(f"[log_manager] ✓ Force killed {serial}")
            return True
        except subprocess.TimeoutExpired:
            print(f"[log_manager] Kill timeout, using kill -9 for {serial}...")

        # Strategy 4: OS-level kill -9 (SIGKILL)
        print(f"[log_manager] Using kill -9 for {serial}...")
        result = subprocess.run(
            ["kill", "-9", str(proc.pid)],
            capture_output=True,
            timeout=3
        )
        success = result.returncode == 0
        if success:
            print(f"[log_manager] ✓ kill -9 successful for {serial}")
        else:
            print(f"[log_manager] ❌ kill -9 failed for {serial}")
        return success

    except Exception as e:
        print(f"[log_manager] Unix kill failed for {serial}: {e}")
        return False

def force_kill_log_collector(proc: subprocess.Popen, serial: str) -> bool:
    """Unified cross-platform force kill cho log collectors"""
    if not proc or not proc.pid:
        return True  # Already dead

    if os.name == 'nt':
        return _force_kill_windows(proc, serial)
    else:
        return _force_kill_unix(proc, serial)

def check_collector_zombies(log_procs: Dict[str, Optional[subprocess.Popen]]) -> List[str]:
    """Detect potential zombie collectors bằng cách check responsiveness"""
    zombies = []

    for serial, proc in log_procs.items():
        if not proc:
            continue

        try:
            # Check if process still exists and is running
            if proc.poll() is None:  # Still running
                # Send signal 0 (non-killing) to check if process is responsive
                if os.name != 'nt':
                    try:
                        os.kill(proc.pid, 0)  # Signal 0 just checks existence
                    except OSError:
                        # Process exists but not responding to signals = ZOMBIE
                        zombies.append(serial)
                # On Windows, harder to detect zombies without trying operations
                # Could add timeout-based detection here if needed
        except Exception as e:
            print(f"[log_manager] Error checking zombie status for {serial}: {e}")
            zombies.append(serial)

    return zombies

def start_collectors(serials: List[str], room_hash: str, game_package: str, start_run: int = None, max_limit: int = MAX_LOG_COLLECTORS) -> Dict[str, Optional[subprocess.Popen]]:
    """
    Khởi chạy log collectors cho danh sách serials.
    
    Args:
        serials: Danh sách serial device
        room_hash: Room hash hiện tại
        game_package: Package name của game đang chạy
        start_run: Timestamp bắt đầu session (optional)
        max_limit: Giới hạn tối đa số collectors (mặc định 20)
    
    Returns:
        Dict[serial] -> Popen object hoặc None nếu spawn thất bại
    """
    log_procs = {}
    log_data_script = Path(__file__).parent / "log_data.py"
    
    # Trên Windows, cần tạo process group mới để có thể gửi tín hiệu CTRL_BREAK
    popen_kwargs = {}
    if os.name == 'nt':
        popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

    if start_run is None:
        start_run = int(time.time())

    for i, serial in enumerate(serials):
        if i >= max_limit:
            print(f"[log_manager warn] Vượt quá MAX_LOG_COLLECTORS ({max_limit}), dừng spawn")
            break
        
        try:
            # Kiểm tra đang chạy source hay chạy exe
            if getattr(sys, 'frozen', False):
                # Đang chạy file EXE - gọi chính mình kèm cờ --worker
                executable = sys.executable
                cmd = [executable, "--worker", "log_data", serial, room_hash, game_package, str(start_run)]
            else:
                # Đang chạy code Python thường (Dev)
                executable = sys.executable  # python.exe
                script_path = Path(__file__).parent / "log_data.py"
                cmd = [executable, "-u", str(script_path), serial, room_hash, game_package, str(start_run)]

            print(f"[log_manager] Spawning collector for {serial} with start_run={start_run}", flush=True)
            proc = subprocess.Popen(
                cmd,
                text=True,
                bufsize=1,
                **popen_kwargs
            )
            log_procs[serial] = proc
            print(f"[log_manager] Started collector for {serial} (PID: {proc.pid})")

            # Delay để tránh spike ADB
            if i < len(serials) - 1:
                time.sleep(SPAWN_DELAY)
        except Exception as e:
            print(f"[log_manager err] Failed to start collector for {serial}: {e}")
            log_procs[serial] = None
    
    return log_procs


def stop_collectors(log_procs: Dict[str, Optional[subprocess.Popen]]) -> Dict[str, bool]:
    """
    Enhanced stop_collectors với SAFE process group management và zombie detection

    Uses kill_process_group_safe() to prevent agent suicide on Linux while
    ensuring complete cleanup of process trees (including ADB child processes).

    Args:
        log_procs: Dict serial -> Popen object

    Returns:
        Dict[serial, success]: Cleanup result cho mỗi collector
    """
    results = {}
    zombie_warnings = []

    for serial, proc in log_procs.items():
        if proc is None:
            results[serial] = True  # Already cleaned
            continue

        try:
            print(f"[log_manager] Stopping collector for {serial}...", flush=True)

            # Check if already dead
            if proc.poll() is not None:
                print(f"[log_manager] Collector {serial} already dead")
                results[serial] = True
                continue

            # Use enhanced force kill với all strategies
            success = force_kill_log_collector(proc, serial)
            results[serial] = success

            # Final verification và zombie detection
            if not success:
                if proc.poll() is None:
                    zombie_warnings.append(serial)
                    print(f"[log_manager] ⚠️  Potential zombie process for {serial} (PID: {proc.pid})")
                else:
                    print(f"[log_manager] ✓ Eventually stopped {serial}")
            elif success:
                print(f"[log_manager] ✓ Successfully stopped {serial}")

        except Exception as e:
            print(f"[log_manager] Failed to stop {serial}: {e}")
            results[serial] = False
            zombie_warnings.append(serial)

    # Summary reporting
    successful_stops = sum(1 for success in results.values() if success)
    total_collectors = len(results)

    if zombie_warnings:
        print(f"[log_manager] ⚠️  {len(zombie_warnings)} collectors may be zombies: {zombie_warnings}")
        print("   These may require manual cleanup or system restart")

    print(f"[log_manager] Stopped {successful_stops}/{total_collectors} collectors")

    return results


def is_collector_alive(proc: Optional[subprocess.Popen]) -> bool:
    """Kiểm tra xem collector process còn chạy không"""
    if proc is None:
        return False
    return proc.poll() is None
