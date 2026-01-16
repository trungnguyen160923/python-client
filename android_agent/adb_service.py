import shlex
import subprocess
import os
import signal
import time
import threading
from typing import Dict, List

# Global locks và state cho ADB health monitoring
_ADB_SERVER_LOCK = threading.Lock()
_HEALTH_STATE_LOCK = threading.Lock()

# Health tracking variables
_adb_timeout_count = 0
_last_server_restart = 0
_adb_restart_attempts = 0
_adb_health_state = "healthy"  # healthy, degrading, unhealthy, recovering

class ADBHealthState:
    HEALTHY = "healthy"
    DEGRADING = "degrading"
    UNHEALTHY = "unhealthy"
    RECOVERING = "recovering"

def force_kill_process(proc: subprocess.Popen, pid: int = None) -> bool:
    """
    Cross-platform force kill cho ADB processes với safety checks

    Args:
        proc: subprocess.Popen object
        pid: Process ID (optional)

    Returns:
        bool: True nếu kill thành công
    """
    if not proc and not pid:
        return False

    target_pid = pid or proc.pid
    if not target_pid:
        return False

    try:
        # 1. Thử terminate graceful trước
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
                return True
            except subprocess.TimeoutExpired:
                pass

        # 2. Detect if process can use process group kill (chỉ cho processes được tạo với new session)
        can_use_killpg = False
        if proc and hasattr(proc, '_popen') and os.name != 'nt':
            try:
                # Check if process is session leader (PGID == PID)
                pgid = os.getpgid(target_pid)
                can_use_killpg = (pgid == target_pid)
            except (OSError, AttributeError):
                can_use_killpg = False

        # 3. Force kill cross-platform
        if os.name == 'nt':  # Windows
            # /F = force, /T = tree (kill child processes)
            kill_cmd = ["taskkill", "/F", "/T", "/PID", str(target_pid)]
            # CREATE_NO_WINDOW = 0x08000000 để tránh nháy cmd window
            result = subprocess.run(kill_cmd, capture_output=True, timeout=5,
                                    creationflags=0x08000000)  # CREATE_NO_WINDOW
            return result.returncode == 0
        else:  # Unix/Linux/macOS
            # SAFE: Chỉ dùng killpg nếu chắc chắn process có process group riêng
            if can_use_killpg:
                try:
                    os.killpg(os.getpgid(target_pid), signal.SIGKILL)
                    return True
                except (OSError, ProcessLookupError):
                    pass

            # Fallback: Single process kill (an toàn cho ADB)
            try:
                os.kill(target_pid, signal.SIGKILL)
                return True
            except (OSError, ProcessLookupError):
                pass

        return False
    except Exception as e:
        print(f"[ADB] Force kill failed for PID {target_pid}: {e}")
        return False

def check_and_restart_adb_server() -> bool:
    """
    Restart ADB server với thread synchronization và rate limiting

    Returns:
        bool: True nếu restart thành công
    """
    global _adb_restart_attempts, _last_server_restart

    current_time = time.time()

    # Rate limiting: Max 3 attempts per minute
    if _adb_restart_attempts >= 3 and (current_time - _last_server_restart) < 60:
        print(f"[ADB] Too many restart attempts ({_adb_restart_attempts}), waiting...")
        return False

    # Non-blocking check: Nếu đang có thread khác restart, bỏ qua
    if _ADB_SERVER_LOCK.locked():
        print("[ADB] Server restart already in progress, skipping...")
        return False

    with _ADB_SERVER_LOCK:  # Acquire lock
        try:
            print("[ADB] Checking server health...")
            # Double-check: Verify server health trong lock
            check = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=5)

            # Nếu server vẫn ổn (exit 0 và không quá nhiều offline), không cần restart
            offline_count = check.stdout.count("offline") if check.stdout else 0
            if check.returncode == 0 and offline_count < 3:
                print("[ADB] Server seems healthy now, cancelling restart.")
                return True

            print(f"[ADB] Server unhealthy detected ({offline_count} offline devices). Restarting...")

            # Kill server
            kill_result = subprocess.run(["adb", "kill-server"], timeout=5, capture_output=True)
            if kill_result.returncode != 0:
                print(f"[ADB] Warning: kill-server returned {kill_result.returncode}")

            time.sleep(1)  # Brief pause

            # Start server
            start_result = subprocess.run(["adb", "start-server"], timeout=15, capture_output=True)
            if start_result.returncode != 0:
                print(f"[ADB] Failed to start server: {start_result.stderr}")
                _adb_restart_attempts += 1
                _last_server_restart = current_time
                return False

            # Verify restart success
            verify = subprocess.run(["adb", "devices"], capture_output=True, timeout=5)
            if verify.returncode == 0:
                print("[ADB] Server restarted SUCCESSFULLY.")
                _adb_restart_attempts = 0  # Reset counter on success
                _last_server_restart = current_time
                return True
            else:
                print("[ADB] Server restart FAILED - verification failed.")
                _adb_restart_attempts += 1
                _last_server_restart = current_time
                return False

        except subprocess.TimeoutExpired:
            print("[ADB] Server restart timed out")
            _adb_restart_attempts += 1
            _last_server_restart = current_time
            return False
        except Exception as e:
            print(f"[ADB] Server restart exception: {e}")
            _adb_restart_attempts += 1
            _last_server_restart = current_time
            return False

def update_adb_health(is_timeout: bool = False, is_success: bool = False):
    """
    Update ADB health state machine

    Args:
        is_timeout: True if command timed out
        is_success: True if command succeeded
    """
    global _adb_health_state, _adb_timeout_count

    with _HEALTH_STATE_LOCK:
        if is_success and _adb_timeout_count > 0:
            _adb_timeout_count = max(0, _adb_timeout_count - 1)  # Decay counter
            if _adb_timeout_count == 0:
                _adb_health_state = ADBHealthState.HEALTHY
        elif is_timeout:
            _adb_timeout_count += 1
            if _adb_health_state == ADBHealthState.HEALTHY and _adb_timeout_count >= 2:
                _adb_health_state = ADBHealthState.DEGRADING
            elif _adb_health_state == ADBHealthState.DEGRADING and _adb_timeout_count >= 5:
                _adb_health_state = ADBHealthState.UNHEALTHY

        return _adb_health_state

def should_attempt_restart() -> bool:
    """Check if ADB server restart should be attempted"""
    with _HEALTH_STATE_LOCK:
        return _adb_health_state in [ADBHealthState.UNHEALTHY, ADBHealthState.RECOVERING]

def run_adb_once(serial: str, command_text: str, timeout: int = None) -> Dict[str, object]:
    # Dynamic timeout based on command type
    if timeout is None:
        cmd_lower = command_text.strip().lower()
        if cmd_lower.startswith("install"):
            timeout = 300  # 5 minutes for APK installation (large files, slow devices)
        elif cmd_lower.startswith("push"):
            timeout = 120  # 2 minutes for file transfer
        elif cmd_lower.startswith("pull"):
            timeout = 120  # 2 minutes for file transfer
        elif "net-install" in cmd_lower or "download" in cmd_lower:
            timeout = 180  # 3 minutes for network operations
        else:
            timeout = 60   # 1 minute for regular commands

    cmd = ["adb", "-s", serial] + shlex.split(command_text)
    code = -1
    out = ""
    err = ""
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate(timeout=timeout)
        code = proc.returncode

        # Track successful commands để improve health state
        if code == 0:
            update_adb_health(is_success=True)
    except subprocess.TimeoutExpired:
        # ENHANCED: Force kill với multiple fallback strategies
        print(f"[ADB] Command timeout after {timeout}s, force killing: {command_text[:50]}...")

        if proc:
            # Thử force kill process
            if not force_kill_process(proc):
                print(f"[ADB] Primary kill failed for PID {proc.pid}, attempting OS-level kill...")
                # Fallback: OS-level kill
                try:
                    if os.name == 'nt':
                        subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                                     capture_output=True, timeout=5)
                    else:
                        os.kill(proc.pid, signal.SIGKILL)
                except Exception as e:
                    print(f"[ADB] OS kill also failed: {e}")

            # Cố gắng lấy output còn sót lại
            try:
                out, err = proc.communicate(timeout=1)
            except Exception:
                pass

        err = f"Command timed out after {timeout} seconds (command: {command_text[:50]}...)"
        code = 124

        # Track health và trigger restart nếu cần
        current_health = update_adb_health(is_timeout=True)
        print(f"[ADB] Health state: {current_health}, timeout count: {_adb_timeout_count}")

        # Attempt server restart nếu unhealthy
        if should_attempt_restart():
            print("[ADB] Attempting server restart due to degraded health...")
            if check_and_restart_adb_server():
                print("[ADB] Server restart successful, resetting health state")
                update_adb_health(is_success=True)  # Reset health
    except Exception as exc:
        err = str(exc)
    return {
        "serial": serial,
        "code": code,
        "stdout": (out or "").strip(),
        "stderr": (err or "").strip(),
    }

def list_adb_devices() -> List[Dict[str, object]]:
    try:
        proc = subprocess.Popen(
            ["adb", "devices"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate(timeout=5)
        if proc.returncode != 0:
            return []
    except Exception:
        return []
    lines = (out or "").splitlines()
    devices: List[Dict[str, object]] = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]

        # Get ADB status
        adb_status = "active" if state == "device" else state

        # Check if we have session status for this device
        session_status = None
        try:
            from android_agent.session_manager import get_session_status
            session_status = get_session_status(serial)
        except Exception as e:
            session_status = None

        # Priority: Session status > ADB status
        # If device has running session, show session status instead of ADB status
        final_status = session_status if session_status else adb_status

        devices.append({
            "serial": serial,
            "data": {},
            "status": final_status,
            "device_type": "android",
        })
    return devices
