import threading
import re
import time
import requests
from typing import Dict, Optional
from .adb_service import run_adb_once
from .api_client import report_command_result, API_BASE_URL
from .log_manager import start_collectors, stop_collectors
import os
import shlex
import subprocess

# Global registry for session status - shared across modules
_session_registry: Dict[str, Dict[str, object]] = {}
_session_registry_lock = threading.Lock()

def register_session(serial: str, session_data: Dict[str, object]) -> None:
    """Register a session in global registry"""
    with _session_registry_lock:
        _session_registry[serial] = session_data

def unregister_session(serial: str) -> None:
    """Unregister a session from global registry"""
    with _session_registry_lock:
        _session_registry.pop(serial, None)

def get_session_status(serial: str) -> Optional[str]:
    """Get session status from global registry"""
    with _session_registry_lock:
        session = _session_registry.get(serial)
        return session.get("status") if session else None

def handle_start_game(serial: str, command_text: str, room_hash: str, command_id: Optional[int], meta: Optional[dict], game_sessions: Dict[str, Dict[str, object]], game_sessions_lock: threading.Lock):
    with game_sessions_lock:
        session = game_sessions.get(serial)
        if session and session.get("thread") and session["thread"].is_alive():
            return
        stop_evt = threading.Event()
        stop_flag = threading.Event()
        session = {"stop": stop_evt, "stop_flag": stop_flag, "thread": None, "process": None, "log_procs": {}, "status": "INITIALIZING"}
        game_sessions[serial] = session

        # Register in global registry
        register_session(serial, session)
    
    # Trích xuất game_package
    game_package = "unknown"
    
    # Debug: In ra command_text để kiểm tra xem server gửi xuống cái gì
    print(f"[session_manager DEBUG] Processing start_game cmd: {command_text} | Meta: {meta}", flush=True)

    # 1. Ưu tiên tìm trong command_text vì đây là lệnh thực tế chạy (chứa giá trị thật)
    match = re.search(r"-e game_package\s+([^\s]+)", command_text)
    if match:
        game_package = match.group(1).strip("'\"")

    # 2. Nếu không tìm thấy hoặc giá trị là placeholder, mới thử lấy từ meta
    if (game_package == "unknown" or "{" in game_package):
        print(f"[session_manager DEBUG] game_package extracted as '{game_package}' from cmd. Checking meta...", flush=True)
        if meta and "game_package" in meta:
            game_package = meta["game_package"]
            print(f"[session_manager DEBUG] Used game_package from meta: {game_package}", flush=True)
        else:
            print(f"[session_manager DEBUG] Meta not available or missing game_package. Meta: {meta}", flush=True)

    # Gọi API start_session ngay lập tức tại đây
    start_run = int(time.time())  # Sử dụng thời gian thực, không cộng 7h để tránh lỗi logic server
    try:
        url = f"{API_BASE_URL}/api/v1/ads_statistics/start_session"
        payload = {
            "room_hash": room_hash,
            "serial": serial,
            "game_package": game_package,
            "start_run": str(start_run)  # ms -> string
        }
        print(f"[session_manager DEBUG] Calling start_session: {url} | Payload: {payload}", flush=True)
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code in (200, 201):
            print(f"[session_manager] Started session SUCCESS for {serial}. Resp: {resp.text}", flush=True)
        else:
            print(f"[session_manager ERROR] Failed start_session. Code: {resp.status_code} | Body: {resp.text}", flush=True)
    except Exception as e:
        print(f"[session_manager EXCEPTION] Failed to start session API: {e}", flush=True)

    # Khởi chạy log collector cho serial này
    log_procs = start_collectors([serial], room_hash, game_package, start_run=start_run)
    with game_sessions_lock:
        session["log_procs"] = log_procs

    # Ensure logs directory exists
    os.makedirs("logs", exist_ok=True)

    cmd = ["adb", "-s", serial] + shlex.split(command_text)

    def terminate_process_safely(proc: subprocess.Popen) -> None:
        """Graceful process termination with fallback to force kill"""
        if not proc or proc.poll() is not None:
            return

        try:
            proc.terminate()
            proc.wait(timeout=3.0)  # Give 3s for graceful shutdown
        except subprocess.TimeoutExpired:
            print(f"[Cleanup] Force killing process {proc.pid} for {serial}")
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception as e:
                print(f"[Cleanup] Error force killing {serial}: {e}")

    def loop():
        # Local restart counter - thread-safe per device
        restart_count = 0
        max_restarts = 2

        # File-based logging to prevent pipe buffer deadlock
        timestamp = int(time.time())
        log_file_path = f"logs/session_{serial}_{timestamp}.log"

        while not stop_evt.is_set() and not session["stop_flag"].is_set():
            proc = None
            log_file = None
            is_stable_run = False  # Flag to track if this run was stable

            try:
                # Open log file for stdout redirection (prevents pipe deadlock)
                log_file = open(log_file_path, "w", encoding="utf-8")

                # Start process with file redirection
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_file,           # Direct to file (no PIPE deadlock)
                    stderr=subprocess.STDOUT,  # Merge stderr to stdout
                    text=True
                )

                with game_sessions_lock:
                    session["process"] = proc
                    session["status"] = "RUNNING_GAME"
                    # Update global registry
                    register_session(serial, session)

                print(f"[Game] Started session for {serial} (PID: {proc.pid}) - Status: RUNNING_GAME")
                start_time = time.time()

                # POLLING LOOP - Safe monitoring for long-running processes
                session_duration = 0

                while True:
                    current_time = time.time()
                    session_duration = current_time - start_time

                    # CHECK 1: User stop signal (highest priority - immediate response)
                    if stop_evt.is_set() or session["stop_flag"].is_set():
                        print(f"[Game] Stop signal received for {serial}")
                        break

                    # CHECK 2: Process completion (normal finish or crash)
                    ret_code = proc.poll()
                    if ret_code is not None:
                        print(f"[Game] Session {serial} finished (code {ret_code}) after {session_duration:.1f}s")

                        # EVALUATE STABILITY: If ran > 60s, consider stable
                        if session_duration > 60:
                            is_stable_run = True

                        break

                    # CHECK 3: Safety timeout (24h absolute limit)
                    if session_duration > 86400:  # 24 hours
                        print(f"[Game] Session {serial} reached 24h safety timeout")
                        with game_sessions_lock:
                            session["status"] = "ACTIVE"
                            # Update global registry
                            register_session(serial, session)
                        break

                    # CHECK 4: Health monitoring (every 5min after 1h)
                    if session_duration > 3600 and int(session_duration) % 300 == 0:
                        print(f"[Game] Session {serial} running healthy ({session_duration/3600:.1f}h)")

                    # CHECK 5: Log collector health monitoring
                    # Check if log collectors are still running, restart if dead
                    with game_sessions_lock:
                        log_procs = session.get("log_procs") or {}

                    for log_serial, log_proc in log_procs.items():
                        if log_proc and log_proc.poll() is not None:  # Log collector died
                            print(f"[LogMonitor] Log collector for {log_serial} died, restarting...")
                            try:
                                # Restart log collector
                                new_log_procs = start_collectors([log_serial], room_hash, game_package, start_run=start_run)
                                with game_sessions_lock:
                                    session["log_procs"].update(new_log_procs)
                                print(f"[LogMonitor] Successfully restarted log collector for {log_serial}")
                            except Exception as e:
                                print(f"[LogMonitor] Failed to restart log collector for {log_serial}: {e}")

                    # Non-blocking sleep - allows immediate signal response
                    time.sleep(1)

                # Post-loop cleanup: terminate if still running
                if proc.poll() is None:
                    print(f"[Game] Terminating leftover process for {serial}")
                    terminate_process_safely(proc)

            except subprocess.SubprocessError as e:
                # Handle subprocess-specific errors
                print(f"[Game] Subprocess error for {serial}: {e}")
                is_stable_run = False  # Definitely unstable

            except Exception as e:
                # General errors
                print(f"[Game] Exception for {serial}: {e}")
                is_stable_run = False  # Definitely unstable

            finally:
                # CRITICAL: Always cleanup resources to prevent leaks
                if log_file:
                    try:
                        log_file.close()
                    except Exception as e:
                        print(f"[Cleanup] Error closing log file for {serial}: {e}")

                terminate_process_safely(proc)

                with game_sessions_lock:
                    session["process"] = None

            # SMART CIRCUIT BREAKER: Evaluate run stability and decide next action
            if is_stable_run:
                restart_count = 0  # Reset on successful stable run
            else:
                restart_count += 1  # Count unstable runs (crashes, fast failures)
                print(f"[Game] Unstable run detected ({restart_count}/{max_restarts}) for {serial}")

            # CIRCUIT BREAKER: Stop after too many consecutive failures
            if restart_count >= max_restarts:
                print(f"[Game] CRITICAL: {serial} failed {max_restarts} times consecutively. STOPPING RESTART LOOP.")

                # REPORT FAILURE TO SERVER IMMEDIATELY
                report_command_result({
                    "room_hash": room_hash,
                    "serial": serial,
                    "command_id": int(command_id) if command_id is not None else 0,
                    "success": False,
                    "output": f"CRITICAL: Game crashed {restart_count} times consecutively. Circuit breaker tripped.",
                    "meta": meta,
                })

                # STATE SYNCHRONIZATION: Update session status for main.py visibility
                with game_sessions_lock:
                    session["status"] = "ERROR_CRASH"
                    session["error_info"] = {
                        "reason": "circuit_breaker_tripped",
                        "restart_attempts": restart_count,
                        "last_error_time": time.time(),
                        "total_uptime": time.time() - start_time if 'start_time' in locals() else 0
                    }
                    # Update global registry
                    register_session(serial, session)

                break

            # Final stop check before auto-restart
            if stop_evt.is_set() or session["stop_flag"].is_set():
                print(f"[Game] Stop confirmed for {serial}")
                with game_sessions_lock:
                    session["status"] = "ACTIVE"
                    # Update global registry
                    register_session(serial, session)
                break

            # PROGRESSIVE BACKOFF: Wait longer after each failure
            if not is_stable_run:
                backoff_time = min(30, 5 * restart_count)  # 5s, 10s, 15s... max 30s
                print(f"[Game] Backing off {backoff_time}s before retry...")
                time.sleep(backoff_time)
            else:
                # Normal restart delay for stable runs
                print(f"[Game] Auto-restarting session for {serial} in 2s...")
                time.sleep(2)
    thread = threading.Thread(target=loop, daemon=True)
    session["thread"] = thread
    thread.start()
    def verify_start():
        max_retries = 30  # Allow up to 30 seconds to wait
        target_package = game_package  # Sử dụng game_package thực tế thay vì "nat.myc.test"

        print(f"[Verify] Checking start status for {serial} (Max 30s)... Target package: {target_package}")

        # Check if circuit breaker already reported failure
        with game_sessions_lock:
            if session.get("status") == "ERROR_CRASH":
                print(f"[Verify] Circuit breaker already reported failure for {serial}, skipping verification")
                return

        for i in range(max_retries):
            # Check if PID exists
            check_cmd = f"shell pidof {target_package}"
            res = run_adb_once(serial, check_cmd)

            code = res.get("code", -1)
            pid = str(res.get("stdout", "")).strip()

            # If PID found -> Game is running -> Report SUCCESS immediately
            if code == 0 and pid:
                print(f"[Verify] SUCCESS: {target_package} is running (PID: {pid}) after {i}s")
                report_command_result({
                    "room_hash": room_hash,
                    "serial": serial,
                    "command_id": int(command_id) if command_id is not None else 0,
                    "success": True,
                    "output": f"Game started successfully. PID: {pid}",
                    "meta": meta,
                })
                return  # Exit function immediately, no need to wait further

            # Not found yet -> Wait 1 second then retry
            time.sleep(1)

        # If we exhaust all 30 attempts (30s) and still no PID -> Report FAILED
        print(f"[Verify] FAILED: Timed out waiting for {target_package}")

        # Try to get final error log, but don't hang if ADB is overloaded
        try:
            res = run_adb_once(serial, check_cmd)  # Get final error log
            stderr = str(res.get("stderr", ""))
            output_msg = stderr or "Timeout: Game process not found after 30s"
        except Exception as e:
            print(f"[Verify] Warning: Could not get final error log: {e}")
            output_msg = "Timeout: Game process not found after 30s"

        report_command_result({
            "room_hash": room_hash,
            "serial": serial,
            "command_id": int(command_id) if command_id is not None else 0,
            "success": False,
            "output": output_msg,
            "meta": meta,
        })
    threading.Thread(target=verify_start, daemon=True).start()

def handle_stop_game(serial: str, command_text: str, room_hash: str, command_id: Optional[int], meta: Optional[dict], game_sessions: Dict[str, Dict[str, object]], game_sessions_lock: threading.Lock):
    with game_sessions_lock:
        session = game_sessions.get(serial)
        if session:
            session["status"] = "ACTIVE"  # Set status when stopping
            # Update global registry
            register_session(serial, session)
    if session:
        # Dừng log collectors
        log_procs = session.get("log_procs") or {}
        if log_procs:
            stop_collectors(log_procs)
        
        stop_evt = session.get("stop")
        if stop_evt:
            stop_evt.set()
        stop_flag = session.get("stop_flag")
        if stop_flag:
            stop_flag.set()
        thread = session.get("thread")
        if thread:
            thread.join(timeout=2)
        proc = session.get("process")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                pass
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
        if thread:
            thread.join(timeout=2)
        thread = session.get("thread")
        if thread:
            thread.join(timeout=1)
        with game_sessions_lock:
            game_sessions.pop(serial, None)

        # Unregister from global registry
        unregister_session(serial)
        _ = run_adb_once(serial, command_text)
        check_cmd = f"shell pidof nat.myc.test"
        res = run_adb_once(serial, check_cmd)
        code = res.get("code", -1)
        stdout = str(res.get("stdout", ""))
        stderr = str(res.get("stderr", ""))
        if (code != 0) or (not stdout.strip()):
            report_command_result({
                "room_hash": room_hash,
                "serial": serial,
                "command_id": int(command_id) if command_id is not None else 0,
                "success": True,
                "output": stdout,
                "meta": meta,
            })
        else:
            report_command_result({
                "room_hash": room_hash,
                "serial": serial,
                "command_id": int(command_id) if command_id is not None else 0,
                "success": False,
                "output": stderr or "Game process still running after stop command",
                "meta": meta,
            })
