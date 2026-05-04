import os
import sys
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional
import json

import requests


CONFIG_FILE = Path(__file__).with_name("config.txt")
LOG_FILE = Path(__file__).with_name("log_error.txt")
REPORT_INTERVAL_SEC = 3.0
FETCH_INTERVAL_SEC = 1.0
PRINT_INTERVAL_SEC = 1.0
STATUS_INTERVAL_SEC = 3.0
CLEAR_INTERVAL_SEC = 120.0

# UI + device status
UI_REFRESH_SEC = 5.0
TEST_LOG_DEVICE_PATH = "/sdcard/test_log.txt"
GAME_PROCESS_MATCH = "nat.myc"  # check `adb shell ps` output contains this
GAME_PACKAGE_NAME = "nat.myc.test"  # fast pidof check
FORCE_STOP_PACKAGE = "nat.myc.test"  # package to stop/kill for GUI action


def load_room_hash() -> str:
    if CONFIG_FILE.exists():
        saved = CONFIG_FILE.read_text(encoding="utf-8").strip()
        if saved:
            return saved

    room_hash = input("Enter room hash: ").strip()
    while not room_hash:
        room_hash = input("Room hash cannot be empty. Enter room hash: ").strip()

    CONFIG_FILE.write_text(room_hash, encoding="utf-8")
    return room_hash


def append_error_log(serial: str, message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{timestamp}   {serial}   :   {message}\n")
    except Exception:
        # keep silent on logging failures
        pass


def download_temp_file(url: str) -> Optional[str]:
    """Tải file từ URL về thư mục temp và trả về đường dẫn file."""
    try:
        filename = url.split("/")[-1] or "temp_file"
        
        # Nếu đang chạy file .exe (frozen) thì lưu cạnh file .exe
        if getattr(sys, 'frozen', False):
            local_path = Path(sys.executable).with_name(filename)
        else:
            local_path = Path(__file__).with_name(filename)
        
        print(f"[download] Downloading {url} -> {local_path}")
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(local_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return str(local_path)
    except Exception as e:
        print(f"[download err] {e}")
        return None

def run_adb_once(serial: str, command_text: str) -> Dict[str, object]:
    cmd = ["adb", "-s", serial] + shlex.split(command_text)
    code = -1
    out = ""
    err = ""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate()
        code = proc.returncode
    except Exception as exc:
        err = str(exc)
    return {
        "serial": serial,
        "code": code,
        "stdout": (out or "").strip(),
        "stderr": (err or "").strip(),
    }


def cleanup_apk_files(apk_files: List[str]) -> None:
    for file_path in apk_files:
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass


def run_adb_with_timeout(serial: str, adb_args: List[str], timeout_sec: float) -> Dict[str, object]:
    cmd = ["adb", "-s", serial] + adb_args
    try:
        # print(f"[adb cmd] {' '.join(cmd)} (timeout={timeout_sec}s)")
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return {
            "serial": serial,
            "code": int(cp.returncode),
            "stdout": (cp.stdout or "").strip(),
            "stderr": (cp.stderr or "").strip(),
        }
    except subprocess.TimeoutExpired:
        return {
            "serial": serial,
            "code": 124,
            "stdout": "",
            "stderr": f"timeout after {timeout_sec}s",
        }
    except Exception as exc:
        return {
            "serial": serial,
            "code": -1,
            "stdout": "",
            "stderr": str(exc),
        }


def run_adb_sequence(serial: str, command_text: str) -> Dict[str, object]:
    """Execute semicolon-separated commands sequentially for the given serial."""

    # --- HÀM PHỤ TRỢ: Lấy danh sách package name (Tận dụng run_adb_once) ---
    def get_installed_packages(target_serial: str) -> set:
        res = run_adb_once(target_serial, "shell pm list packages")
        if res.get("code") != 0:
            return set()

        out = str(res.get("stdout", ""))
        packages = set()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                packages.add(line.replace("package:", ""))
        return packages

    # =========================================================================
    # 1. XỬ LÝ LỆNH: net-install (Hỗ trợ nhiều URL + Rollback)
    # Cú pháp: net-install <URL_1> <URL_2> ...
    # =========================================================================
    if command_text.strip().startswith("net-install"):
        parts = shlex.split(command_text)
        urls = parts[1:]

        if not urls:
            return {"serial": serial, "code": 1, "stdout": "", "stderr": "No URLs provided"}

        downloaded_files: List[str] = []
        installed_packages_list: List[str] = []
        install_logs: List[str] = []
        final_code = 0

        try:
            for i, url in enumerate(urls):
                step_num = i + 1

                local_file = download_temp_file(url)
                if not local_file:
                    install_logs.append(f"File {step_num}: Download failed ({url})")
                    final_code = 1
                    break

                if not local_file.lower().endswith(".apk"):
                    new_path = local_file + f"_{i}.apk"
                    try:
                        os.rename(local_file, new_path)
                        local_file = new_path
                    except OSError:
                        pass

                downloaded_files.append(local_file)

                packages_before = get_installed_packages(serial)

                print(f"[install] Installing {step_num}/{len(urls)}: {local_file}")
                install_cmd = f"install -r -t \"{local_file}\""
                result = run_adb_once(serial, install_cmd)

                stdout = str(result.get("stdout", "")).strip()
                stderr = str(result.get("stderr", "")).strip()
                combined_output = f"{stdout} {stderr}".strip()

                if "Success" in combined_output:
                    install_logs.append(f"File {step_num}: Success ({os.path.basename(url)})")

                    packages_after = get_installed_packages(serial)
                    new_packages = packages_after - packages_before

                    if new_packages:
                        pkg_name = list(new_packages)[0]
                        installed_packages_list.append(pkg_name)
                else:
                    install_logs.append(f"File {step_num}: FAILED - {combined_output}")
                    install_logs.append("!!! TRIGGERING ROLLBACK (Uninstalling previous apps) !!!")
                    final_code = 1

                    for pkg in reversed(installed_packages_list):
                        uninstall_res = run_adb_once(serial, f"uninstall {pkg}")
                        if str(uninstall_res.get("code")) == "0":
                            install_logs.append(f"Rollback: Uninstalled {pkg} (Success)")
                        else:
                            install_logs.append(f"Rollback: Uninstalled {pkg} (Failed)")
                    break

            return {
                "serial": serial,
                "code": final_code,
                "stdout": "\n".join(install_logs),
                "stderr": "" if final_code == 0 else "Installation sequence failed with rollback.",
            }

        finally:
            cleanup_apk_files(downloaded_files)

    # =========================================================================
    # 2. XỬ LÝ LỆNH: net-push
    # Cú pháp: net-push <URL> <DESTINATION_PATH>
    # =========================================================================
    if command_text.strip().startswith("net-push"):
        parts = shlex.split(command_text)
        if len(parts) < 3:
            return {"serial": serial, "code": 1, "stdout": "", "stderr": "Usage: net-push <URL> <DESTINATION_PATH>"}

        url = parts[1]
        dest = parts[2]
        local_file = download_temp_file(url)
        if not local_file:
            return {"serial": serial, "code": 1, "stdout": "", "stderr": "Failed to download file from URL"}

        try:
            push_cmd = f"push \"{local_file}\" \"{dest}\""
            return run_adb_once(serial, push_cmd)
        finally:
            cleanup_apk_files([local_file])

    # =========================================================================
    # 3. Lệnh thường: hỗ trợ chuỗi "cmd1; cmd2; cmd3"
    # =========================================================================
    steps = [step.strip() for step in command_text.split(";") if step.strip()]

    if not steps:
        return run_adb_once(serial, command_text)

    combined_stdout: List[str] = []
    combined_stderr: List[str] = []
    last_code = 0

    for step in steps:
        res = run_adb_once(serial, step)
        last_code = res.get("code", -1) or 0
        if res.get("stdout"):
            combined_stdout.append(str(res["stdout"]))
        if res.get("stderr"):
            combined_stderr.append(str(res["stderr"]))
        if last_code != 0:
            break

    return {
        "serial": serial,
        "code": last_code,
        "stdout": "\n".join(combined_stdout).strip(),
        "stderr": "\n".join(combined_stderr).strip(),
    }


def list_adb_devices() -> List[Dict[str, object]]:
    """Return list of connected adb devices as payload items for report-devices."""
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
    # First line is usually "List of devices attached"
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        # Format: <serial>\t<state>
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        status = "active" if state == "device" else state
        devices.append(
            {
                "serial": serial,
                "data": {},
                "status": status,
                "device_type": "android",
            }
        )
    return devices


def _summarize_log_text(text: str, max_chars: int = 220) -> str:
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    last_lines = lines[-3:]
    summary = " | ".join(last_lines)
    if len(summary) > max_chars:
        summary = summary[-max_chars:]
    return summary


def _device_is_running_game(serial: str) -> bool:
    # Fast path: pidof for known package
    res = run_adb_with_timeout(serial, ["shell", "pidof", GAME_PACKAGE_NAME], timeout_sec=2.0)
    if res.get("code") == 0 and str(res.get("stdout", "")).strip():
        return True

    # Spec: check `adb shell ps` output contains nat.myc
    res = run_adb_with_timeout(serial, ["shell", "ps"], timeout_sec=3.0)
    if res.get("code") == 0:
        out = str(res.get("stdout", ""))
        return GAME_PROCESS_MATCH in out
    return False


def _device_has_root(serial: str) -> bool:
    """Best-effort root check.

    Returns True when `su -c id` succeeds and indicates uid=0.
    Uses a short timeout to avoid hanging on su prompts.
    """
    # Some devices will block on the first `su` call (waiting for user approval).
    # In that case `adb shell su -c ...` can hang and hit our timeout, even though
    # the device *is* rooted. We treat this as a "root-capable" device so we can
    # attempt root-mode start (which is also guarded by timeouts + throttling).
    res = run_adb_with_timeout(serial, ["shell", "su", "-c", "id"], timeout_sec=3.0)
    print(f"[root check] serial={serial} code={res.get('code')} stdout={res.get('stdout')} stderr={res.get('stderr')}")
    try:
        code = int(res.get("code", -1))
    except (TypeError, ValueError):
        code = -1
    out = f"{res.get('stdout', '')}\n{res.get('stderr', '')}".lower()
    if code == 0 and "uid=0" in out:
        return True

    if code == 124:
        # Timeout: try to detect if `su` binary exists. If it exists, assume root-capable.
        which = run_adb_with_timeout(
            serial,
            ["shell", "sh", "-c", "command -v su || which su"],
            timeout_sec=2.0,
        )
        try:
            which_code = int(which.get("code", -1))
        except (TypeError, ValueError):
            which_code = -1
        if which_code == 0 and str(which.get("stdout", "")).strip():
            return True
    return False


def _build_root_nohup_instrument_su_cmd(command_text: str) -> str:
    """Convert `shell am instrument ...` to a `su -c` nohup command.

    Keeps all original tokens/params; only wraps with nohup + redirect to TEST_LOG_DEVICE_PATH.
    """
    tokens = shlex.split(command_text)
    if tokens and tokens[0] == "shell":
        tokens = tokens[1:]

    # Expect tokens to start with: am instrument ...
    base_cmd = " ".join(shlex.quote(t) for t in tokens)
    return f"nohup {base_cmd} > {TEST_LOG_DEVICE_PATH} 2>&1 &"


def _device_read_test_log(serial: str) -> str:
    # Prefer tail: avoids lag when file is large.
    res = run_adb_with_timeout(
        serial,
        ["shell", "tail", "-n", "50", TEST_LOG_DEVICE_PATH],
        timeout_sec=3.0,
    )
    if res.get("code") == 0 and str(res.get("stdout", "")).strip():
        return _summarize_log_text(str(res.get("stdout", "")))

    # Fallback: cat + truncate
    res = run_adb_with_timeout(serial, ["shell", "cat", TEST_LOG_DEVICE_PATH], timeout_sec=3.0)
    if res.get("code") == 0 and str(res.get("stdout", "")).strip():
        text = str(res.get("stdout", ""))
        if len(text) > 4000:
            text = text[-4000:]
        return _summarize_log_text(text)

    err = str(res.get("stderr", "")).strip()
    return _summarize_log_text(err) or "(no log)"


def _force_stop_game_for_serial(serial: str, game_sessions: Dict[str, Dict[str, object]], game_sessions_lock: threading.Lock, command_text: Optional[str] = None) -> Dict[str, object]:
    """Force stop game on a single device.

    Works even if the client was restarted (no in-memory session).
    Best-effort: non-root friendly; ignores failures.
    """
    # Stop any in-memory watchdog/session first (if present)
    with game_sessions_lock:
        session = game_sessions.get(serial)

    if session:
        try:
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
                thread.join(timeout=1)
        finally:
            with game_sessions_lock:
                game_sessions.pop(serial, None)

    # Always run stop command(s) on device.
    stop_cmd = command_text or f"shell am force-stop {FORCE_STOP_PACKAGE}"
    res_stop = run_adb_once(serial, stop_cmd)

    # Best-effort hard kill (non-root + root)
    try:
        _ = run_adb_with_timeout(serial, ["shell", "pkill", "-f", FORCE_STOP_PACKAGE], timeout_sec=3.0)
    except Exception:
        pass
    try:
        _ = run_adb_with_timeout(serial, ["shell", "su", "-c", f"pkill -f {FORCE_STOP_PACKAGE}"], timeout_sec=3.0)
    except Exception:
        pass

    # Verify
    res_pid = run_adb_once(serial, f"shell pidof {FORCE_STOP_PACKAGE}")
    pid_stdout = str(res_pid.get("stdout", "")).strip()
    ok = (int(res_pid.get("code", -1) or -1) != 0) or (not pid_stdout)
    return {
        "serial": serial,
        "ok": ok,
        "stop": res_stop,
        "pidof": res_pid,
    }


def start_device_gui(stop_signal: threading.Event, game_sessions: Dict[str, Dict[str, object]], game_sessions_lock: threading.Lock, interval: float = UI_REFRESH_SEC) -> bool:
    """Show device status UI using Dear PyGui.

    Runs a GUI loop on the main thread; returns False if Dear PyGui is unavailable.
    """
    try:
        import dearpygui.dearpygui as dpg
    except Exception:
        print("[ui] Dear PyGui not installed. Run: pip install dearpygui")
        return False

    snapshot_lock = threading.Lock()
    snapshot: Dict[str, Dict[str, object]] = {}

    status_lock = threading.Lock()
    status_message = {"text": "Ready"}

    def set_status(text: str) -> None:
        with status_lock:
            status_message["text"] = text

    def query_one(serial: str) -> Dict[str, object]:
        is_running = _device_is_running_game(serial)
        log_text = _device_read_test_log(serial)
        return {"serial": serial, "is_running": is_running, "log": log_text}

    def snapshot_loop() -> None:
        while not stop_signal.is_set():
            devices = list_adb_devices()
            serials = [str(d.get("serial")) for d in devices if d.get("serial")]
            new_snapshot: Dict[str, Dict[str, object]] = {}
            if serials:
                max_workers = min(6, max(1, len(serials)))
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futures = {ex.submit(query_one, s): s for s in serials}
                    for fut in as_completed(futures):
                        serial = futures[fut]
                        try:
                            new_snapshot[serial] = fut.result(timeout=0)
                        except Exception as exc:
                            new_snapshot[serial] = {"serial": serial, "is_running": False, "log": f"error: {exc}"}
            with snapshot_lock:
                snapshot.clear()
                snapshot.update(new_snapshot)
            stop_signal.wait(interval)

    def on_force_stop(sender, app_data, user_data):
        serial = str(user_data)
        set_status(f"Force stopping {serial}...")

        def worker():
            result = _force_stop_game_for_serial(serial, game_sessions, game_sessions_lock)
            ok = bool(result.get("ok"))
            set_status(f"Force stop {serial}: {'OK' if ok else 'FAILED'}")

        threading.Thread(target=worker, daemon=True).start()

    dpg.create_context()
    dpg.create_viewport(title="Android Devices", width=980, height=520)
    dpg.setup_dearpygui()

    with dpg.window(tag="__main_window", label="Devices", width=960, height=480):
        dpg.add_text("Device Monitor", tag="__title")
        dpg.add_text("Ready", tag="__status")
        dpg.add_separator()
        with dpg.table(tag="__devices_table", header_row=True, resizable=True, borders_innerH=True, borders_innerV=True, borders_outerH=True, borders_outerV=True):
            dpg.add_table_column(label="STT", width_fixed=True, init_width_or_weight=50)
            dpg.add_table_column(label="serial", width_fixed=True, init_width_or_weight=180)
            dpg.add_table_column(label="isRunning", width_fixed=True, init_width_or_weight=90)
            dpg.add_table_column(label="log")
            dpg.add_table_column(label="action", width_fixed=True, init_width_or_weight=140)

    dpg.set_primary_window("__main_window", True)
    dpg.show_viewport()

    threading.Thread(target=snapshot_loop, daemon=True).start()

    last_refresh = 0.0
    while dpg.is_dearpygui_running() and not stop_signal.is_set():
        now = time.time()
        if now - last_refresh >= 0.5:
            # Update status line
            with status_lock:
                dpg.set_value("__status", status_message["text"])

            # Rebuild table rows
            table_tag = "__devices_table"
            children = dpg.get_item_children(table_tag, 1) or []
            for child in list(children):
                try:
                    dpg.delete_item(child)
                except Exception:
                    pass

            with snapshot_lock:
                items = list(snapshot.items())
            serials = [k for k, _ in items]
            serials.sort()
            for idx, serial in enumerate(serials, start=1):
                info = snapshot.get(serial, {})
                is_running = bool(info.get("is_running", False))
                run_text = "RUN" if is_running else "STOP"
                run_color = (0, 180, 0, 255) if is_running else (220, 40, 40, 255)
                log_text = str(info.get("log", ""))
                with dpg.table_row(parent=table_tag):
                    dpg.add_text(str(idx))
                    dpg.add_text(serial)
                    dpg.add_text(run_text, color=run_color)
                    dpg.add_text(log_text)
                    dpg.add_button(label="Force stop", callback=on_force_stop, user_data=serial)

            last_refresh = now

        dpg.render_dearpygui_frame()

    dpg.destroy_context()
    return True


def start_reporter(room_hash_value: str, stop_signal: threading.Event, interval: float = REPORT_INTERVAL_SEC) -> None:
    """
    Background thread that reports devices every `interval` seconds.
    """
    url = "http://160.25.81.154:9000/api/v1/report-devices"
    # url = "http://localhost:9000/api/v1/report-devices"

    def report_loop() -> None:
        while not stop_signal.is_set():
            try:
                devices = list_adb_devices()
                # Đã xoá log danh sách thiết bị kết nối

                payload = {
                    "room_hash": room_hash_value,
                    "devices": devices,
                }
                requests.post(url, json=payload, timeout=5)
            except Exception as exc:
                print(f"[report err] {exc}")
            stop_signal.wait(interval)

    threading.Thread(target=report_loop, daemon=True).start()


def start_command_fetcher(
    room_hash_value: str,
    commands: List[Dict[str, object]],
    commands_lock: threading.Lock,
    stop_signal: threading.Event,
    interval: float = FETCH_INTERVAL_SEC,
) -> None:
    """
    Background thread to poll subscribe API and store commands (command_text, serial) in a shared list.
    """
    url = f"http://160.25.81.154:9000/api/v1/subscribe/{room_hash_value}"
    # url = f"http://localhost:9000/api/v1/subscribe/{room_hash_value}"

    def fetch_loop() -> None:
        while not stop_signal.is_set():
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    cmd_items = data.get("commands") or []
                    simplified: List[Dict[str, object]] = []
                    for item in cmd_items:
                        command_text = item.get("command_text", "")
                        serial = item.get("serial", "")
                        if not command_text or not serial:
                            continue

                        # Lấy room_hash và command_id từ response (hoặc meta.command_id nếu cần)
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
                        print(
                            "[fetch] room=",
                            room_hash_value,
                            " commands=",
                            len(simplified),
                            " serials=",
                            [d.get("serial") for d in simplified],
                        )
                        with commands_lock:
                            if commands:
                                # still pending; skip adding new commands until queue is empty
                                pass
                            else:
                                commands.extend(simplified)
                else:
                    print(f"[fetch warn] HTTP {resp.status_code}")
            except Exception as exc:
                print(f"[fetch err] {exc}")
            stop_signal.wait(interval)

    threading.Thread(target=fetch_loop, daemon=True).start()


def start_command_printer(
    commands: List[Dict[str, object]],
    commands_lock: threading.Lock,
    stop_signal: threading.Event,
    game_sessions: Dict[str, Dict[str, object]],
    game_sessions_lock: threading.Lock,
    interval: float = PRINT_INTERVAL_SEC,
) -> None:
    """
    Background thread to consume queued commands.
    - Start game commands run persistently per-serial (auto-restart on crash).
    - Stop game commands stop any running game process and execute the stop command once.
    - Other commands run once with summary + error logging.
    """

    def handle_start_game(
        serial: str,
        command_text: str,
        room_hash: str,
        command_id: Optional[int],
        meta: Optional[dict] = None,
    ) -> None:
        with game_sessions_lock:
            session = game_sessions.get(serial)
            if session and session.get("thread") and session["thread"].is_alive():
                return
            stop_evt = threading.Event()
            stop_flag = threading.Event()  # flag to request stop from stop handler
            session = {"stop": stop_evt, "stop_flag": stop_flag, "thread": None, "process": None}
            game_sessions[serial] = session

        use_root = _device_has_root(serial)
        session["use_root"] = use_root

        # Helpful to understand which mode is used when debugging.
        print(f"[START] serial={serial} use_root={use_root}")

        # Non-root: execute original command normally.
        cmd_normal = ["adb", "-s", serial] + shlex.split(command_text)

        # Root: run instrumentation in background via su/nohup, writing logs to /sdcard/test_log.txt.
        su_nohup_cmd = _build_root_nohup_instrument_su_cmd(command_text)
        # Note: we execute root commands via run_adb_with_timeout to avoid keeping a long-lived adb process.

        def loop() -> None:
            if use_root:
                # Root mode: start instrumentation in background (nohup + &),
                # then periodically ensure the game is running.
                next_attempt_at = 0.0
                first_attempt = True
                while not stop_evt.is_set() and not session["stop_flag"].is_set():
                    if not _device_is_running_game(serial):
                        now = time.time()
                        if now >= next_attempt_at:
                            # First attempt can take longer if device asks for root permission.
                            timeout_sec = 15.0 if first_attempt else 5.0
                            first_attempt = False
                            res = run_adb_with_timeout(
                                serial,
                                ["shell", "su", "-c", su_nohup_cmd],
                                timeout_sec=timeout_sec,
                            )
                            # If it fails/timeouts, back off to avoid spawning adb repeatedly.
                            try:
                                code = int(res.get("code", -1))
                            except (TypeError, ValueError):
                                code = -1
                            next_attempt_at = now + (10.0 if code != 0 else 2.0)
                    stop_evt.wait(2)
                return

            # Non-root mode: keep a persistent adb process (blocks) and auto-restart when it exits.
            while not stop_evt.is_set() and not session["stop_flag"].is_set():
                proc = None
                try:
                    proc = subprocess.Popen(
                        cmd_normal,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    with game_sessions_lock:
                        session["process"] = proc
                    _out, _err = proc.communicate()
                    _code = proc.returncode
                except Exception as exc:
                    _ = exc  # ignore logging for start commands
                finally:
                    with game_sessions_lock:
                        session["process"] = None
                if stop_evt.is_set() or session["stop_flag"].is_set():
                    break
                stop_evt.wait(1)

        thread = threading.Thread(target=loop, daemon=True)
        session["thread"] = thread
        thread.start()

        # Sau khi start, chạy thêm bước verify xem game đã thực sự chạy chưa
        def verify_start() -> None:
            time.sleep(5)
            # Verify using the same heuristic as the UI monitor to avoid false negatives.
            is_running = _device_is_running_game(serial)
            log_tail = _device_read_test_log(serial)
            if is_running:
                report_command_result(
                    room_hash=room_hash,
                    serial=serial,
                    command_id=command_id,
                    code=0,
                    stdout=log_tail,
                    stderr="",
                    meta=meta,
                )
            else:
                # Nếu không tìm thấy process thì coi là fail nghiệp vụ
                report_command_result(
                    room_hash=room_hash,
                    serial=serial,
                    command_id=command_id,
                    code=1,
                    stdout=log_tail,
                    stderr="Game process not found after start command",
                    meta=meta,
                )

        threading.Thread(target=verify_start, daemon=True).start()

    def handle_stop_game(
        serial: str,
        command_text: str,
        room_hash: str,
        command_id: Optional[int],
        meta: Optional[dict] = None,
    ) -> None:
        with game_sessions_lock:
            session = game_sessions.get(serial)
        # If this process has an in-memory session (watchdog thread), stop it first.
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
            if thread:
                thread.join(timeout=1)

            with game_sessions_lock:
                game_sessions.pop(serial, None)

        # Always execute stop commands even if there is no session (e.g. client restarted).
        _ = run_adb_once(serial, command_text)

        # Best-effort hard-kill to avoid the game/instrumentation staying alive.
        # Must not require root.
        try:
            _ = run_adb_with_timeout(serial, ["shell", "pkill", "-f", "nat.myc.test"], timeout_sec=3.0)
        except Exception:
            pass
        try:
            _ = run_adb_with_timeout(serial, ["shell", "su", "-c", "pkill -f nat.myc.test"], timeout_sec=3.0)
        except Exception:
            pass

        # Verify: game đã thật sự dừng chưa (không còn process)
        package_name = "nat.myc.test"
        check_cmd = f"shell pidof {package_name}"
        res = run_adb_once(serial, check_cmd)
        code = res.get("code", -1)
        stdout = str(res.get("stdout", ""))
        stderr = str(res.get("stderr", ""))
        if (code != 0) or (not stdout.strip()):
            report_command_result(
                room_hash=room_hash,
                serial=serial,
                command_id=command_id,
                code=0,
                stdout=stdout,
                stderr=stderr,
                meta=meta,
            )
        else:
            report_command_result(
                room_hash=room_hash,
                serial=serial,
                command_id=command_id,
                code=1,
                stdout=stdout,
                stderr=stderr or "Game process still running after stop command",
                meta=meta,
            )

    def report_command_result(
        room_hash: str,
        serial: str,
        command_id: Optional[int],
        code: int,
        stdout: str,
        stderr: str,
        meta: Optional[dict] = None,
    ) -> None:
        """Gửi kết quả thực thi về server để BE/FE biết thiết bị đã chạy xong hay chưa."""
        try:
            print(f"[AGENT] Báo kết quả về BE: serial={serial} command_id={command_id} batch_id={meta.get('batch_id') if meta else None} success={code==0}")
            # url = "http://localhost:9000/api/v1/report-result"
            url = "http://160.25.81.154:9000/api/v1/report-result"
            success = code == 0
            output = stderr or stdout or f"exit_code={code}"
            payload = {
                "room_hash": room_hash,
                "serial": serial,
                "command_id": int(command_id) if command_id is not None else 0,
                "success": success,
                "output": output[:4000],
            }
            if meta:
                payload["meta"] = meta
            import json
            print(f"[AGENT] Chuẩn bị gửi report: serial={serial} command_id={command_id} batch_id={meta.get('batch_id') if meta else None} payload={json.dumps(payload, ensure_ascii=False)}")
            requests.post(url, json=payload, timeout=5)
        except Exception as exc:
            print(f"[report-result err] {serial}: {exc}")

    def run_regular_command(
        serial: str,
        command_text: str,
        room_hash: str,
        command_id: Optional[int],
        results: List[Dict[str, object]],
        results_lock: threading.Lock,
        meta: Optional[dict] = None,
    ) -> None:
        result = run_adb_sequence(serial, command_text)
        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        instrument_fail_patterns = [
            "ClassNotFoundException",
            "initializationError",
            "FAILURES!!!",
            "Tests run:",
            "Failed loading specified test class",
        ]
        is_instrument_fail = any(pat in stdout or pat in stderr for pat in instrument_fail_patterns)
        if is_instrument_fail:
            result["code"] = 1
        with results_lock:
            result_copy: Dict[str, object] = dict(result)
            result_copy["room_hash"] = room_hash
            result_copy["command_id"] = command_id
            if meta:
                result_copy["meta"] = meta
            results.append(result_copy)

    def print_loop() -> None:
        while not stop_signal.is_set():
            batch: List[Dict[str, object]] = []
            with commands_lock:
                if commands:
                    batch = commands[:]

            if not batch:
                stop_signal.wait(interval)
                continue

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
                if (
                    "nat.myc.test/androidx.test.runner.AndroidJUnitRunner" in text
                    and "runPlayGame" in text
                ):
                    print(f"[CLASSIFY] Start Game: serial={serial} cmd={text}")
                    start_batch.append(
                        {
                            "serial": serial,
                            "command_text": text,
                            "room_hash": room_hash,
                            "command_id": command_id,
                            "meta": meta,
                        }
                    )
                elif "force-stop nat.myc.test" in text:
                    print(f"[CLASSIFY] Stop Game: serial={serial} cmd={text}")
                    stop_batch.append(
                        {
                            "serial": serial,
                            "command_text": text,
                            "room_hash": room_hash,
                            "command_id": command_id,
                            "meta": meta,
                        }
                    )
                else:
                    print(f"[CLASSIFY] Regular Command: serial={serial} cmd={text}")
                    regular_batch.append(
                        {
                            "serial": serial,
                            "command_text": text,
                            "room_hash": room_hash,
                            "command_id": command_id,
                            "meta": meta,
                        }
                    )

            for item in start_batch:
                handle_start_game(
                    serial=item["serial"],
                    command_text=item["command_text"],
                    room_hash=str(item.get("room_hash", "")),
                    command_id=item.get("command_id"),
                    meta=item.get("meta"),
                )

            for item in stop_batch:
                handle_stop_game(
                    serial=item["serial"],
                    command_text=item["command_text"],
                    room_hash=str(item.get("room_hash", "")),
                    command_id=item.get("command_id"),
                    meta=item.get("meta"),
                )

            if regular_batch:
                workers: List[threading.Thread] = []
                results: List[Dict[str, object]] = []
                results_lock = threading.Lock()
                for item in regular_batch:
                    room_hash = str(item.get("room_hash", ""))
                    command_id = item.get("command_id")
                    meta = item.get("meta") if "meta" in item else None
                    worker = threading.Thread(
                        target=run_regular_command,
                        args=(
                            str(item["serial"]),
                            str(item["command_text"]),
                            room_hash,
                            command_id,
                            results,
                            results_lock,
                            meta,
                        ),
                    )
                    workers.append(worker)
                    worker.start()

                for worker in workers:
                    worker.join()

                success_count = sum(1 for r in results if r.get("code") == 0)
                fail_results = [r for r in results if r.get("code") != 0]
                fail_count = len(fail_results)
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"[SUMARY] {timestamp} : success={success_count} fail={fail_count}")
                # Ghi log lỗi cục bộ và report kết quả lên server cho tất cả results
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
                        report_command_result(
                            room_hash=room_hash,
                            serial=serial,
                            command_id=command_id if isinstance(command_id, int) else None,
                            code=code,
                            stdout=stdout,
                            stderr=stderr,
                            meta=meta,
                        )

            with commands_lock:
                commands.clear()

            stop_signal.wait(interval)

    threading.Thread(target=print_loop, daemon=True).start()


def start_status_monitor(
    stop_signal: threading.Event,
    game_sessions: Dict[str, Dict[str, object]],
    game_sessions_lock: threading.Lock,
    interval: float = STATUS_INTERVAL_SEC,
) -> None:
    """
    Background thread to print counts of alive threads and game processes.
    """

    def monitor_loop() -> None:
        while not stop_signal.is_set():
            thread_count = len(threading.enumerate())
            with game_sessions_lock:
                proc_count = sum(
                    1
                    for sess in game_sessions.values()
                    for proc in [sess.get("process")]
                    if proc and proc.poll() is None
                )
            print(f"[STATUS] threads={thread_count} processes={proc_count}")
            stop_signal.wait(interval)

    threading.Thread(target=monitor_loop, daemon=True).start()


def start_console_clearer(stop_signal: threading.Event, interval: float = CLEAR_INTERVAL_SEC) -> None:
    """
    Background thread to clear console periodically.
    """

    def clear_loop() -> None:
        while not stop_signal.is_set():
            stop_signal.wait(interval)
            if stop_signal.is_set():
                break
            try:
                os.system("cls")
            except Exception:
                pass

    threading.Thread(target=clear_loop, daemon=True).start()


def main() -> None:
    room_hash = load_room_hash()
    print(f"Room hash: {room_hash}")

    commands: List[Dict[str, object]] = []
    commands_lock = threading.Lock()
    stop_event = threading.Event()
    game_sessions: Dict[str, Dict[str, object]] = {}
    game_sessions_lock = threading.Lock()

    # Prefer GUI. It runs on the main thread.
    ui_started = False

    start_reporter(room_hash, stop_event)
    start_command_fetcher(room_hash, commands, commands_lock, stop_event)
    start_command_printer(commands, commands_lock, stop_event, game_sessions, game_sessions_lock)
    start_status_monitor(stop_event, game_sessions, game_sessions_lock)

    ui_started = start_device_gui(stop_event, game_sessions, game_sessions_lock, interval=UI_REFRESH_SEC)
    if not ui_started:
        # Fallback: no GUI => keep old behavior (no always-on table), just keep process alive.
        print("Background threads running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping...")
            stop_event.set()
    else:
        # GUI loop exited
        stop_event.set()


if __name__ == "__main__":
    main()
