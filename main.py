import os
import sys
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests


CONFIG_FILE = Path(__file__).with_name("config.txt")
LOG_FILE = Path(__file__).with_name("log_error.txt")
REPORT_INTERVAL_SEC = 3.0
FETCH_INTERVAL_SEC = 1.0
PRINT_INTERVAL_SEC = 1.0
STATUS_INTERVAL_SEC = 3.0
CLEAR_INTERVAL_SEC = 120.0


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


def run_adb_sequence(serial: str, command_text: str) -> Dict[str, object]:
    """
    Execute semicolon-separated commands sequentially for the given serial.
    Stops on first failure and returns aggregated output.
    """
    # --- XỬ LÝ LỆNH ĐẶC BIỆT: net-push ---
    # Cú pháp: net-push <URL> <DESTINATION_PATH>
    if command_text.strip().startswith("net-push"):
        parts = shlex.split(command_text)
        if len(parts) >= 3:
            url = parts[1]
            dest = parts[2]
            local_file = download_temp_file(url)
            
            if local_file:
                # Chuyển đổi thành lệnh adb push thông thường
                # Thêm dấu nháy đơn để shlex xử lý đúng đường dẫn Windows (tránh lỗi mất dấu \)
                push_cmd = f"push '{local_file}' '{dest}'"
                result = run_adb_once(serial, push_cmd)
                
                # (Tùy chọn) Xóa file sau khi push xong để tiết kiệm ổ cứng
                # try:
                #     os.remove(local_file)
                # except: pass
                
                return result
            else:
                return {"serial": serial, "code": 1, "stdout": "", "stderr": "Failed to download file from URL"}

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
    # url = f"http://localhost:8000/api/v1/subscribe/{room_hash_value}"

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

                        simplified.append(
                            {
                                "command_text": command_text,
                                "serial": serial,
                                "room_hash": room_hash,
                                "command_id": command_id,
                            }
                        )
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
    ) -> None:
        with game_sessions_lock:
            session = game_sessions.get(serial)
            if session and session.get("thread") and session["thread"].is_alive():
                return
            stop_evt = threading.Event()
            stop_flag = threading.Event()  # flag to request stop from stop handler
            session = {"stop": stop_evt, "stop_flag": stop_flag, "thread": None, "process": None}
            game_sessions[serial] = session

        cmd = ["adb", "-s", serial] + shlex.split(command_text)

        def loop() -> None:
            while not stop_evt.is_set() and not session["stop_flag"].is_set():
                proc = None
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    with game_sessions_lock:
                        session["process"] = proc
                    out, err = proc.communicate()
                    code = proc.returncode
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
            # Mặc định dùng package nat.myc.test giống pattern phân loại ở dưới
            package_name = "nat.myc.test"
            time.sleep(5)
            check_cmd = f"shell pidof {package_name}"
            res = run_adb_once(serial, check_cmd)
            # Giữ nguyên exit code thực từ adb; chỉ fallback -1 nếu không có
            code = res.get("code", -1)
            stdout = str(res.get("stdout", ""))
            stderr = str(res.get("stderr", ""))
            # Thành công thực sự: có pid (stdout không rỗng) và exit code = 0
            if code == 0 and stdout.strip():
                report_command_result(
                    room_hash=room_hash,
                    serial=serial,
                    command_id=command_id,
                    code=0,
                    stdout=stdout,
                    stderr=stderr,
                )
            else:
                # Nếu không tìm thấy process thì coi là fail nghiệp vụ
                report_command_result(
                    room_hash=room_hash,
                    serial=serial,
                    command_id=command_id,
                    code=1,
                    stdout=stdout,
                    stderr=stderr or "Game process not found after start command",
                )

        threading.Thread(target=verify_start, daemon=True).start()

    def handle_stop_game(
        serial: str,
        command_text: str,
        room_hash: str,
        command_id: Optional[int],
    ) -> None:
        with game_sessions_lock:
            session = game_sessions.get(serial)
        if session:
            stop_evt = session.get("stop")
            if stop_evt:
                stop_evt.set()
            stop_flag = session.get("stop_flag")
            if stop_flag:
                stop_flag.set()

            # First attempt: stop thread cleanly
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
                

            # Final attempt: join thread again after process kill
            if thread:
                thread.join(timeout=2)

            thread = session.get("thread")
            if thread:
                thread.join(timeout=1)
            with game_sessions_lock:
                    game_sessions.pop(serial, None)

            # Thực thi lệnh stop chính
            _ = run_adb_once(serial, command_text)

            # Verify: game đã thật sự dừng chưa (không còn process)
            package_name = "nat.myc.test"
            check_cmd = f"shell pidof {package_name}"
            res = run_adb_once(serial, check_cmd)
            # Giữ nguyên exit code thực từ adb; chỉ fallback -1 nếu không có
            code = res.get("code", -1)
            stdout = str(res.get("stdout", ""))
            stderr = str(res.get("stderr", ""))
            # Thành công nghiệp vụ: không còn pid => stdout rỗng hoặc exit code != 0
            if (code != 0) or (not stdout.strip()):
                report_command_result(
                    room_hash=room_hash,
                    serial=serial,
                    command_id=command_id,
                    code=0,
                    stdout=stdout,
                    stderr=stderr,
                )
            else:
                report_command_result(
                    room_hash=room_hash,
                    serial=serial,
                    command_id=command_id,
                    code=1,
                    stdout=stdout,
                    stderr=stderr or "Game process still running after stop command",
                )

    def report_command_result(
        room_hash: str,
        serial: str,
        command_id: Optional[int],
        code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        """Gửi kết quả thực thi về server để BE/FE biết thiết bị đã chạy xong hay chưa."""
        try:
            url = "http://160.25.81.154:9000/api/v1/report-result"
            # url = "http://localhost:8000/api/v1/report-result"
            success = code == 0
            output = stderr or stdout or f"exit_code={code}"
            payload = {
                "room_hash": room_hash,
                "serial": serial,
                "command_id": int(command_id) if command_id is not None else 0,
                "success": success,
                "output": output[:4000],
            }
            # print(
            #     "[report-result] room=",
            #     room_hash,
            #     " serial=",
            #     serial,
            #     " command_id=",
            #     payload["command_id"],
            #     " success=",
            #     payload["success"],
            # )
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
    ) -> None:
        result = run_adb_sequence(serial, command_text)
        # Kiểm tra lỗi instrument đặc biệt
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
                        }
                    )

            for item in start_batch:
                handle_start_game(
                    serial=item["serial"],
                    command_text=item["command_text"],
                    room_hash=str(item.get("room_hash", "")),
                    command_id=item.get("command_id"),
                )

            for item in stop_batch:
                handle_stop_game(
                    serial=item["serial"],
                    command_text=item["command_text"],
                    room_hash=str(item.get("room_hash", "")),
                    command_id=item.get("command_id"),
                )

            if regular_batch:
                workers: List[threading.Thread] = []
                results: List[Dict[str, object]] = []
                results_lock = threading.Lock()
                for item in regular_batch:
                    room_hash = str(item.get("room_hash", ""))
                    command_id = item.get("command_id")
                    worker = threading.Thread(
                        target=run_regular_command,
                        args=(
                            str(item["serial"]),
                            str(item["command_text"]),
                            room_hash,
                            command_id,
                            results,
                            results_lock,
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
                    # Không được dùng "or -1" vì 0 là success nhưng là giá trị falsy trong Python
                    try:
                        code = int(r.get("code", -1))
                    except (TypeError, ValueError):
                        code = -1
                    stdout = str(r.get("stdout", ""))
                    stderr = str(r.get("stderr", ""))
                    room_hash = str(r.get("room_hash", ""))
                    command_id = r.get("command_id")

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

    start_reporter(room_hash, stop_event)
    start_command_fetcher(room_hash, commands, commands_lock, stop_event)
    start_command_printer(commands, commands_lock, stop_event, game_sessions, game_sessions_lock)
    start_status_monitor(stop_event, game_sessions, game_sessions_lock)
    start_console_clearer(stop_event)
    print("Background threads running. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        stop_event.set()


if __name__ == "__main__":
    main()
