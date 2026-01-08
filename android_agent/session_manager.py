import threading
from typing import Dict, Optional
from .adb_service import run_adb_once
from .api_client import report_command_result

def handle_start_game(serial: str, command_text: str, room_hash: str, command_id: Optional[int], meta: Optional[dict], game_sessions: Dict[str, Dict[str, object]], game_sessions_lock: threading.Lock):
    with game_sessions_lock:
        session = game_sessions.get(serial)
        if session and session.get("thread") and session["thread"].is_alive():
            return
        stop_evt = threading.Event()
        stop_flag = threading.Event()
        session = {"stop": stop_evt, "stop_flag": stop_flag, "thread": None, "process": None}
        game_sessions[serial] = session
    import shlex
    cmd = ["adb", "-s", serial] + shlex.split(command_text)
    def loop():
        while not stop_evt.is_set() and not session["stop_flag"].is_set():
            proc = None
            try:
                import subprocess
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                with game_sessions_lock:
                    session["process"] = proc
                out, err = proc.communicate()
                code = proc.returncode
            except Exception:
                pass
            finally:
                with game_sessions_lock:
                    session["process"] = None
            if stop_evt.is_set() or session["stop_flag"].is_set():
                break
            stop_evt.wait(1)
    thread = threading.Thread(target=loop, daemon=True)
    session["thread"] = thread
    thread.start()
    def verify_start():
        import time
        time.sleep(5)
        check_cmd = f"shell pidof nat.myc.test"
        res = run_adb_once(serial, check_cmd)
        code = res.get("code", -1)
        stdout = str(res.get("stdout", ""))
        stderr = str(res.get("stderr", ""))
        if code == 0 and stdout.strip():
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
                "output": stderr or "Game process not found after start command",
                "meta": meta,
            })
    threading.Thread(target=verify_start, daemon=True).start()

def handle_stop_game(serial: str, command_text: str, room_hash: str, command_id: Optional[int], meta: Optional[dict], game_sessions: Dict[str, Dict[str, object]], game_sessions_lock: threading.Lock):
    with game_sessions_lock:
        session = game_sessions.get(serial)
    if session:
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
