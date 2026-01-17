import threading
import time
import collections
import multiprocessing
import subprocess
import sys
from typing import Dict, List, Deque, Optional
from android_agent.config import load_room_hash, REPORT_INTERVAL_SEC, FETCH_INTERVAL_SEC, PRINT_INTERVAL_SEC, STATUS_INTERVAL_SEC, CLEAR_INTERVAL_SEC, MAX_COMMANDS_QUEUE_SIZE, QUEUE_WARNING_THRESHOLD
from android_agent.utils import (
    append_error_log, clear_console, cleanup_old_logs, cleanup_temp_files, cleanup_lock_files,
    safe_log_exception, format_exception_safe, exception_storage
)
from android_agent.api_client import report_devices, fetch_commands, report_command_result
from android_agent.adb_service import list_adb_devices, run_adb_once
from android_agent.command_processor import run_adb_sequence
from android_agent.session_manager import handle_start_game, handle_stop_game, unregister_session
from android_agent.log_manager import stop_collectors
from android_agent import log_data

def start_reporter(room_hash_value: str, stop_signal: threading.Event, interval: float = REPORT_INTERVAL_SEC):
    def report_loop():
        while not stop_signal.is_set():
            try:
                report_devices(room_hash_value)
            except Exception as exc:
                print(f"[report err] {exc}")
            stop_signal.wait(interval)
    threading.Thread(target=report_loop, daemon=True).start()

def start_command_fetcher(room_hash_value: str, commands: Deque[Dict[str, object]], commands_lock: threading.Lock, stop_signal: threading.Event, interval: float = FETCH_INTERVAL_SEC):
    def fetch_loop():
        while not stop_signal.is_set():
            try:
                cmd_items = fetch_commands(room_hash_value)
                simplified: List[Dict[str, object]] = []
                for item in cmd_items:
                    command_text = item.get("command_text", "")
                    serial = item.get("serial", "")
                    if not command_text or not serial:
                        continue
                    room_hash = item.get("room_hash", room_hash_value)
                    command_id = item.get("command_id")
                    meta = item.get("meta") or {}
                    if not command_id:
                        command_id = meta.get("command_id")
                    simplified.append({
                        "command_text": command_text,
                        "serial": serial,
                        "room_hash": room_hash,
                        "command_id": command_id,
                        "meta": meta,
                    })
                if simplified:
                    print(f"[fetch] room={room_hash_value} commands={len(simplified)} serials={[d.get('serial') for d in simplified]}")

                    # [OPTIMIZATION] Batch locking với overflow protection
                    with commands_lock:
                        current_size = len(commands)
                        max_size = commands.maxlen or MAX_COMMANDS_QUEUE_SIZE

                        # Warning Threshold Check (Check 1 lần trước khi add batch)
                        if current_size >= max_size * QUEUE_WARNING_THRESHOLD:
                            utilization = current_size / max_size * 100
                            print(f"⚠️  Commands queue high usage: {current_size}/{max_size} ({utilization:.1f}%)")

                        # Process batch with overflow protection
                        dropped_count = 0
                        for cmd in simplified:
                            # Overflow Protection Logic
                            if len(commands) >= max_size:
                                # Phải pop tay để lấy thông tin log
                                dropped = commands.popleft()
                                dropped_serial = dropped.get('serial', 'unknown')
                                dropped_count += 1
                                # Chỉ print warning, hạn chế ghi file log quá nhiều nếu spam
                                print(f"🚨 Queue FULL! Dropped cmd for {dropped_serial}")

                            commands.append(cmd)

                        if dropped_count > 0:
                            print(f"🚨 Dropped {dropped_count} commands due to queue overflow")
            except Exception as exc:
                print(f"[fetch err] {exc}")
            stop_signal.wait(interval)
    threading.Thread(target=fetch_loop, daemon=True).start()

def safe_join_threads(threads: List[threading.Thread], batch_timeout: float = 60.0) -> tuple[bool, int]:
    """
    Join threads with deadline-based timeout to prevent infinite hangs.

    Args:
        threads: List of threads to join
        batch_timeout: Maximum time to wait for entire batch

    Returns:
        (all_completed: bool, hanging_count: int)
    """
    if not threads:
        return True, 0

    start_time = time.time()
    deadline = start_time + batch_timeout
    hanging_threads = []

    for i, thread in enumerate(threads):
        # Calculate remaining time until deadline
        time_left = deadline - time.time()

        # If already past deadline, don't wait
        wait_time = max(0.0, time_left)

        if wait_time > 0:
            thread.join(timeout=wait_time)

        if thread.is_alive():
            hanging_threads.append(thread)
            print(f"[WARN] Thread {i} (ID: {thread.ident}) hung after {batch_timeout:.1f}s - continuing")
        else:
            print(f"[DEBUG] Thread {i} completed normally")

    hanging_count = len(hanging_threads)
    all_completed = hanging_count == 0

    if hanging_threads:
        print(f"[CRITICAL] {hanging_count}/{len(threads)} threads hung - system operating in degraded mode")
        # Note: Threads are still alive as zombies, but system continues

    return all_completed, hanging_count

def start_command_printer(commands: Deque[Dict[str, object]], commands_lock: threading.Lock, stop_signal: threading.Event, game_sessions: Dict[str, Dict[str, object]], game_sessions_lock: threading.Lock, interval: float = PRINT_INTERVAL_SEC):
    def print_loop():
        from android_agent.command_processor import cleanup_apk_files
        while not stop_signal.is_set():
            batch: List[Dict[str, object]] = []

            # [CRITICAL SECTION] - Keep lock as short as possible
            with commands_lock:
                if commands:
                    # Copy entire queue to list for processing
                    batch = list(commands)
                    # Clear queue immediately to allow fetcher to queue more commands
                    commands.clear()

            # [NON-CRITICAL SECTION] - Heavy processing outside lock
            if batch:
                start_batch: List[Dict[str, object]] = []
                stop_batch: List[Dict[str, object]] = []
                regular_batch: List[Dict[str, object]] = []
                for cmd in batch:
                    serial = str(cmd.get("serial", ""))
                    text = str(cmd.get("command_text", ""))
                    room_hash = str(cmd.get("room_hash", ""))
                    command_id = cmd.get("command_id")
                    meta = cmd.get("meta") if "meta" in cmd else None
                    if not serial or not text:
                        continue
                    if ("nat.myc.test/androidx.test.runner.AndroidJUnitRunner" in text and "runPlayGame" in text):
                        print(f"[CLASSIFY] Start Game: serial={serial} cmd={text}", flush=True)
                        start_batch.append({"serial": serial, "command_text": text, "room_hash": room_hash, "command_id": command_id, "meta": meta})
                    elif "force-stop nat.myc.test" in text:
                        print(f"[CLASSIFY] Stop Game: serial={serial} cmd={text}")
                        stop_batch.append({"serial": serial, "command_text": text, "room_hash": room_hash, "command_id": command_id, "meta": meta})
                    else:
                        print(f"[CLASSIFY] Regular Command: serial={serial} cmd={text}")
                        regular_batch.append({"serial": serial, "command_text": text, "room_hash": room_hash, "command_id": command_id, "meta": meta})
                for item in start_batch:
                    handle_start_game(item["serial"], item["command_text"], str(item.get("room_hash", "")), item.get("command_id"), item.get("meta"), game_sessions, game_sessions_lock)
                for item in stop_batch:
                    handle_stop_game(item["serial"], item["command_text"], str(item.get("room_hash", "")), item.get("command_id"), item.get("meta"), game_sessions, game_sessions_lock)
                if regular_batch:
                    workers: List[threading.Thread] = []
                    results: List[Dict[str, object]] = []
                    results_lock = threading.Lock()
                    # Remove global all_apk_files - will collect from results later
                    from android_agent.command_processor import run_adb_sequence
                    def worker_func(item):
                        # Per-thread APK file collection (thread-safe)
                        local_apk_files = set()

                        room_hash = str(item.get("room_hash", ""))
                        command_id = item.get("command_id")
                        meta = item.get("meta") if "meta" in item else None
                        result = run_adb_sequence(str(item["serial"]), str(item["command_text"]))

                        # Collect APK files locally (no race condition)
                        if str(item["command_text"]).strip().startswith("net-install"):
                            for f in result.get("downloaded_files", []):
                                local_apk_files.add(f)

                        stdout = str(result.get("stdout", ""))
                        stderr = str(result.get("stderr", ""))
                        instrument_fail_patterns = ["ClassNotFoundException", "initializationError", "FAILURES!!!", "Tests run:", "Failed loading specified test class"]
                        is_instrument_fail = any(pat in stdout or pat in stderr for pat in instrument_fail_patterns)
                        if is_instrument_fail:
                            result["code"] = 1

                        # Embed APK files in result (data embedding approach)
                        result_copy: Dict[str, object] = dict(result)
                        result_copy["__cleanup_files"] = list(local_apk_files)  # Embed APK data
                        result_copy["room_hash"] = room_hash
                        result_copy["command_id"] = command_id
                        if meta:
                            result_copy["meta"] = meta

                        with results_lock:
                            results.append(result_copy)
                    threads = []
                    for item in regular_batch:
                        t = threading.Thread(target=worker_func, args=(item,))
                        threads.append(t)
                        t.start()
                    # Safe join with timeout to prevent infinite hangs
                    all_completed, hanging_count = safe_join_threads(threads, batch_timeout=60.0)

                    if not all_completed:
                        print(f"[WARN] {hanging_count} worker threads hung - processing available results")

                    # Safe merge APK files from all results (after threads complete)
                    all_apk_files = set()
                    for res in results:
                        if "__cleanup_files" in res:
                            all_apk_files.update(res["__cleanup_files"])

                    # Cleanup APK files
                    if all_apk_files:
                        cleanup_apk_files(list(all_apk_files))
                    success_count = sum(1 for r in results if r.get("code") == 0)
                    fail_results = [r for r in results if r.get("code") != 0]
                    fail_count = len(fail_results)
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[SUMARY] {timestamp} : success={success_count} fail={fail_count}")
                    for r in results:
                        serial = str(r.get("serial", ""))
                        try:
                            code = int(r.get("code", -1))
                        except (TypeError, ValueError):
                            code = -1
                        stdout = str(r.get("stdout", ""))
                        stderr = str(r.get("stderr", ""))
                        room_hash = str(r.get("room_hash", ""))
                        command_id = r.get("command_id")
                        meta = r.get("meta") if "meta" in r else None
                        if code != 0:
                            error_text = stderr or stdout or f"exit_code={code}"
                            append_error_log(serial, error_text)
                        if room_hash:
                            report_command_result({
                                "room_hash": room_hash,
                                "serial": serial,
                                "command_id": command_id if isinstance(command_id, int) else None,
                                "success": code == 0,
                                "output": stderr or stdout or f"exit_code={code}",
                                "meta": meta,
                            })
                # Commands already cleared above in critical section
                stop_signal.wait(interval)
    threading.Thread(target=print_loop, daemon=True).start()

def force_stop_game_session(serial: str, session: Dict[str, object],
                          room_hash: str, timeout: float) -> bool:
    """
    Force stop một game session với comprehensive cleanup

    Returns:
        bool: True nếu cleanup thành công
    """
    try:
        # 1. Stop log collectors
        log_procs = session.get("log_procs") or {}
        if log_procs:
            print(f"[Cleanup] Stopping log collectors for {serial}...")
            stop_collectors(log_procs)

        # 2. Signal game thread to stop
        stop_evt = session.get("stop")
        stop_flag = session.get("stop_flag")

        if stop_evt:
            stop_evt.set()
        if stop_flag:
            stop_flag.set()

        # 3. Wait for thread với timeout
        thread = session.get("thread")
        if thread and thread.is_alive():
            print(f"[Cleanup] Waiting for game thread {serial}...")
            thread.join(timeout=timeout)

            if thread.is_alive():
                print(f"[Cleanup] Thread {serial} still alive after timeout, may become zombie")
                return False

        # 4. Force kill game process
        proc = session.get("process")
        if proc and proc.poll() is None:
            print(f"[Cleanup] Force killing game process {serial} (PID: {proc.pid})...")

            # Try graceful terminate first
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                # Force kill nếu terminate không work
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    safe_log_exception("session_cleanup", "process_kill")
                    exception_storage.add_exception("session_cleanup", "process_kill")
                    return False

        # 5. Run ADB force-stop command
        try:
            force_stop_cmd = "shell am force-stop nat.myc.test"
            result = run_adb_once(serial, force_stop_cmd)
            if result.get("code") != 0:
                print(f"[Cleanup] Warning: ADB force-stop failed for {serial}")
        except Exception:
            safe_log_exception("session_cleanup", f"adb_force_stop_{serial}")
            exception_storage.add_exception("session_cleanup", f"adb_force_stop_{serial}")

        return True

    except Exception:
        safe_log_exception("session_cleanup", f"cleanup_{serial}")
        exception_storage.add_exception("session_cleanup", f"cleanup_{serial}")
        return False

def cleanup_all_sessions(game_sessions: Dict[str, Dict[str, object]],
                        game_sessions_lock: threading.Lock,
                        room_hash: str,
                        timeout_per_session: float = 5.0) -> Dict[str, bool]:
    """
    Synchronous cleanup tất cả game sessions với timeout

    Args:
        game_sessions: Dict of active sessions
        game_sessions_lock: Threading lock for sessions
        room_hash: Current room hash
        timeout_per_session: Timeout cho mỗi session cleanup

    Returns:
        Dict[serial, success]: Cleanup result cho mỗi device
    """
    results = {}

    # Get snapshot của tất cả sessions để tránh modify dict while iterating
    with game_sessions_lock:
        sessions_to_cleanup = dict(game_sessions)  # Copy

    if not sessions_to_cleanup:
        print("[Cleanup] No active sessions to cleanup")
        return results

    print(f"[Cleanup] Starting cleanup for {len(sessions_to_cleanup)} sessions...")

    # Cleanup từng session synchronous
    for serial, session in sessions_to_cleanup.items():
        try:
            print(f"[Cleanup] Cleaning up session for {serial}...")

            # Force stop game với timeout
            success = force_stop_game_session(serial, session, room_hash, timeout_per_session)
            results[serial] = success

            if success:
                print(f"[Cleanup] ✓ Successfully cleaned up {serial}")
            else:
                print(f"[Cleanup] ✗ Failed to cleanup {serial} (timeout or error)")

        except Exception:
            safe_log_exception("cleanup_all_sessions", f"cleanup_{serial}")
            exception_storage.add_exception("cleanup_all_sessions", f"cleanup_{serial}")
            results[serial] = False

        # Remove from session registry after cleanup attempt
        with game_sessions_lock:
            game_sessions.pop(serial, None)

        # Unregister from global registry
        unregister_session(serial)

    successful_count = sum(1 for success in results.values() if success)
    print(f"[Cleanup] Completed: {successful_count}/{len(results)} sessions cleaned up")

    return results

def start_status_monitor(stop_signal: threading.Event, game_sessions: Dict[str, Dict[str, object]], game_sessions_lock: threading.Lock, commands: Deque[Dict[str, object]], commands_lock: threading.Lock, interval: float = STATUS_INTERVAL_SEC):
    def monitor_loop():
        zombie_warning_threshold = 50  # Alert if >50 threads (potential zombies)
        while not stop_signal.is_set():
            thread_count = len(threading.enumerate())
            with game_sessions_lock:
                proc_count = sum(1 for sess in game_sessions.values() for proc in [sess.get("process")] if proc and proc.poll() is None)

            if thread_count > zombie_warning_threshold:
                print(f"[CRITICAL] High thread count: {thread_count} - possible zombie threads!")

            # Queue Monitoring
            # Không cần lock ở đây nếu chỉ đọc len() (deque.len() là thread-safe trong CPython),
            # nhưng lock cũng không sao vì tần suất thấp.
            with commands_lock:  # Safe to avoid race conditions
                q_len = len(commands)
                q_max = commands.maxlen or MAX_COMMANDS_QUEUE_SIZE

                if q_len > 0:
                    util_pct = (q_len / q_max) * 100
                    if util_pct >= (QUEUE_WARNING_THRESHOLD * 100):
                        print(f"[WARN] Queue Utilization: {util_pct:.1f}% ({q_len}/{q_max})")

            print(f"[STATUS] Threads: {thread_count} | Processes: {proc_count} | Queue: {q_len}")
            stop_signal.wait(interval)
    threading.Thread(target=monitor_loop, daemon=True).start()

def start_console_clearer(stop_signal: threading.Event, interval: float = CLEAR_INTERVAL_SEC):
    def clear_loop():
        while not stop_signal.is_set():
            stop_signal.wait(interval)
            if stop_signal.is_set():
                break
            clear_console()
    threading.Thread(target=clear_loop, daemon=True).start()

def main():
    room_hash = load_room_hash()
    print(f"Room hash: {room_hash}")

    # Comprehensive startup cleanup (prevent resource accumulation)
    print("[Init] Cleaning up stale resources...")
    cleanup_old_logs(days=3)
    cleanup_temp_files(older_than_hours=24)
    cleanup_lock_files()
    commands: Deque[Dict[str, object]] = collections.deque(maxlen=MAX_COMMANDS_QUEUE_SIZE)
    commands_lock = threading.Lock()
    stop_event = threading.Event()
    game_sessions: Dict[str, Dict[str, object]] = {}
    game_sessions_lock = threading.Lock()
    start_reporter(room_hash, stop_event)
    start_command_fetcher(room_hash, commands, commands_lock, stop_event)
    start_command_printer(commands, commands_lock, stop_event, game_sessions, game_sessions_lock)
    start_status_monitor(stop_event, game_sessions, game_sessions_lock, commands, commands_lock)
    start_console_clearer(stop_event)
    print("Background threads running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 KeyboardInterrupt received - Starting graceful shutdown...")

        # Signal tất cả background threads to stop
        stop_event.set()

        # CRITICAL: Cleanup tất cả game sessions trước khi exit
        print("🧹 Cleaning up game sessions...")
        cleanup_results = cleanup_all_sessions(
            game_sessions,
            game_sessions_lock,
            room_hash,
            timeout_per_session=5.0  # 5 seconds per device
        )

        # Report cleanup results
        if cleanup_results:
            successful = sum(1 for success in cleanup_results.values() if success)
            total = len(cleanup_results)
            print(f"🧹 Session cleanup: {successful}/{total} successful")

            if successful < total:
                failed_devices = [serial for serial, success in cleanup_results.items() if not success]
                print(f"⚠️  Warning: Failed to cleanup devices: {failed_devices}")
                print("   These devices may have zombie processes - manual cleanup may be needed")

        print("✅ Shutdown complete - Exiting...")

if __name__ == "__main__":
    multiprocessing.freeze_support()  # Bắt buộc cho PyInstaller trên Windows

    # --- LOGIC ĐIỀU PHỐI (DISPATCHER) ---
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        # Đây là Process con (Worker)
        if sys.argv[2] == "log_data":
            # Chạy logic của Log Collector
            # Cắt bỏ các tham số --worker log_data để log_data.py đọc đúng index
            sys.argv = [sys.argv[0]] + sys.argv[3:]
            log_data.run_collector()
            sys.exit(0)

    # --- LOGIC CHÍNH (MAIN AGENT) ---
    main()
