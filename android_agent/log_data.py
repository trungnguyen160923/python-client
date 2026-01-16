import subprocess
import sys
import json
import re
import os
from datetime import datetime
import requests
import threading
import time
import copy
import signal
import tempfile
from queue import Queue, Empty
from collections import deque
from dotenv import load_dotenv

class RateLimiter:
    """Simple rate limiter per log_data process"""

    def __init__(self, max_per_minute: int = 30):
        self.max_per_minute = max_per_minute
        self.requests = deque()

    def allow(self) -> bool:
        """Check if request is allowed within rate limit"""
        current_time = time.time()

        # Remove requests outside 1-minute window
        while self.requests and self.requests[0] < current_time - 60:
            self.requests.popleft()

        # Check if under limit
        if len(self.requests) >= self.max_per_minute:
            return False  # Rate limited

        # Add current request
        self.requests.append(current_time)
        return True

class LogBatcher:
    """Local log batcher cho mỗi log_data.py process"""

    def __init__(self, serial: str, api_url: str):
        self.serial = serial
        self.api_url = api_url
        self.queue = Queue(maxsize=1000)  # Local queue per process
        self.batch_size = 10
        self.flush_interval = 5.0  # seconds
        self.stop_event = threading.Event()

        # Start background sender thread
        self.sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
        self.sender_thread.start()

    def _sender_loop(self):
        """Background thread để batch send logs"""
        batch = []
        last_flush = time.time()

        while not self.stop_event.is_set():
            try:
                current_time = time.time()

                # Collect items for batch
                while len(batch) < self.batch_size:
                    try:
                        # Non-blocking get with short timeout
                        item = self.queue.get_nowait()
                        batch.append(item)
                    except:
                        break  # No more items available

                # Check flush conditions
                is_full = len(batch) >= self.batch_size
                is_timeout = (current_time - last_flush) >= self.flush_interval

                if batch and (is_full or is_timeout):
                    self._send_batch(batch)
                    batch = []  # Reset batch
                    last_flush = current_time

                # Short sleep để không busy loop
                time.sleep(0.1)

            except Exception as e:
                print(f"[LogBatcher] Sender error for {self.serial}: {e}")
                time.sleep(5)  # Backoff on error

    def _send_batch(self, batch: list):
        """Send batch của logs đến API"""
        if not batch:
            return

        payload = {
            'serial': self.serial,
            'logs': batch,
            'batch_size': len(batch),
            'timestamp': time.time()
        }

        try:
            # Send với timeout
            resp = requests.post(self.api_url, json=payload, timeout=10)

            if resp.status_code in (200, 201):
                print(f"[LogBatcher] ✓ Sent {len(batch)} logs for {self.serial}")
            else:
                print(f"[LogBatcher] ✗ API error {resp.status_code} for {self.serial}")

        except requests.Timeout:
            print(f"[LogBatcher] Timeout sending batch for {self.serial}")
        except Exception as e:
            print(f"[LogBatcher] Failed to send batch for {self.serial}: {e}")

    def flush_remaining(self):
        """Flush remaining logs khi shutdown"""
        print(f"[LogBatcher] Flushing remaining logs for {self.serial}")
        self.stop_event.set()

        # Collect remaining items
        remaining = []
        while True:
            try:
                item = self.queue.get_nowait()
                remaining.append(item)
            except:
                break

        if remaining:
            print(f"[LogBatcher] Sending final {len(remaining)} logs for {self.serial}")
            self._send_batch(remaining)

def run_collector():
    """Main function to run log collector"""
    # Parse arguments
    SERIAL = sys.argv[1] if len(sys.argv) > 1 else None
    ROOM_HASH = sys.argv[2] if len(sys.argv) > 2 else "unknown"
    GAME_PACKAGE = sys.argv[3] if len(sys.argv) > 3 else "unknown"
    START_RUN_ARG = sys.argv[4] if len(sys.argv) > 4 else None

    if START_RUN_ARG:
        START_RUN = int(START_RUN_ARG)
    else:
        START_RUN = int(datetime.now().timestamp())

    if not SERIAL:
        print("Usage: python log_data.py <serial>", file=sys.stderr)
        sys.exit(1)

    print(f"[log_data] INIT: Serial={SERIAL} Room={ROOM_HASH} Package={GAME_PACKAGE} StartRun={START_RUN}", flush=True)

    # ================== SINGLE INSTANCE CHECK ==================
    # Tạo file lock để đảm bảo chỉ có 1 process chạy cho mỗi Serial
    lock_file_path = os.path.join(tempfile.gettempdir(), f"log_data_{SERIAL}.lock")
    lock_handle = None
    try:
        if os.path.exists(lock_file_path):
            # Thử xóa file lock cũ. Nếu file đang được process khác mở, lệnh này sẽ lỗi PermissionError
            os.remove(lock_file_path)
        # Tạo và giữ file lock
        lock_handle = open(lock_file_path, 'w')
        lock_handle.write(str(os.getpid()))
        lock_handle.flush()
    except OSError:
        print(f"[log_data] Another instance is running for {SERIAL}. Exiting to prevent duplicate logs.", flush=True)
        sys.exit(0)
    # ===========================================================

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    API_BASE_URL = os.getenv("API_BASE_URL")
    API_URL = API_BASE_URL  + "/api/v1/report"

    # Initialize local batcher và rate limiter cho process này
    batcher = LogBatcher(SERIAL, API_URL)
    rate_limiter = RateLimiter(max_per_minute=30)

    # ================== STATS ==================

    TOTAL_BANNER_REVENUE = 0.0
    last_event_signature = None
    last_event_time = 0

    # ==========================================
    # Lấy thời gian hiện tại để logcat bắt từ đó
    def get_logcat_time():
        now = datetime.now()
        MM = str(now.month).zfill(2)
        DD = str(now.day).zfill(2)
        HH = str(now.hour).zfill(2)
        mm = str(now.minute).zfill(2)
        ss = str(now.second).zfill(2)
        return f"{MM}-{DD} {HH}:{mm}:{ss}.000"


    start_time = get_logcat_time()

    # Spawn adb logcat từ thời điểm hiện tại
    adb = subprocess.Popen(
        ["adb", "-s", SERIAL, "logcat", "-v", "time", "-T", start_time],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )

    print(f"[log_data] ADB Logcat process started with PID {adb.pid}", flush=True)

    buffer = ""

    is_ended = False

    def create_log_entry(event_type: str, ad_format: str = None, value: float = 0.0, ad_unit_name: str = ""):
        """Create standardized log entry for batching"""
        return {
            "timestamp": time.time(),
            "event_type": event_type,
            "ad_format": ad_format,
            "value": value,
            "ad_unit_name": ad_unit_name,
            "start_run": START_RUN,
            "room_hash": ROOM_HASH,
            "game_package": GAME_PACKAGE,
            "raw_line": ""  # Can be populated if needed
        }

    def send_event_to_api(ad_format, value):
        """Gửi event lên API ngay lập tức (for critical events)"""
        try:
            extra_data = {
                "start_run": START_RUN,
                # "end_run": int(datetime.now().timestamp()), # Bỏ end_run ở request thường
                "inter": value if ad_format == "INTER" else 0.0,
                "rewarded": value if ad_format == "REWARDED" else 0.0,
                "banner": value if ad_format == "BANNER" else 0.0,
            }

            final_payload = {
                "room_hash": ROOM_HASH,
                "serial": SERIAL,
                "status": "pass",
                "game_package": GAME_PACKAGE,
                "extra_data": extra_data,
            }

            def _send():
                resp = requests.post(API_URL, json=final_payload, timeout=5)
                if resp.ok:
                    print(
                        f"[log_data] {SERIAL} sent to API (status {resp.status_code}): {json.dumps(final_payload)}"
                    )
                else:
                    print(f"[log_data err] {SERIAL} HTTP {resp.status_code}: {resp.text}")

            threading.Thread(target=_send, daemon=True).start()
        except Exception as e:
            print(f"[log_data err] {SERIAL}: {e}")

    def send_end_session():
        """Gửi request kết thúc phiên với end_run khi process dừng"""
        nonlocal is_ended
        if is_ended:
            return
        is_ended = True

        try:
            extra_data = {
                "start_run": START_RUN,
                "end_run": int(time.time()),  # Sử dụng thời gian thực để khớp với start_run
                "inter": 0.0,
                "rewarded": 0.0,
                "banner": TOTAL_BANNER_REVENUE,
            }
            final_payload = {
                "room_hash": ROOM_HASH,
                "serial": SERIAL,
                "status": "pass",
                "game_package": GAME_PACKAGE, 
                "extra_data": extra_data,
            }
            # Gửi đồng bộ (không dùng thread) vì process sắp tắt
            print(f"[log_data] Sending END_RUN for {SERIAL}...", flush=True)
            print(f"[log_data DEBUG] Final Payload: {json.dumps(final_payload)}", flush=True)
            requests.post(API_URL, json=final_payload, timeout=3)
            print(f"[log_data] Sent END_RUN for {SERIAL}")
        except Exception as e:
            print(f"[log_data] Failed to send END_RUN: {e}", flush=True)

    def signal_handler(signum, frame):
        print(f"[log_data] Received signal {signum}, stopping...", flush=True)
        send_end_session()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    # Đăng ký thêm SIGBREAK cho Windows (tương ứng với CTRL_BREAK_EVENT)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, signal_handler)

    def process_line(line):
        # Debug: In ra nếu dòng log có chứa từ khóa quan trọng để kiểm tra xem có bị lọc sai không
        if "ad_impression" in line or "Start sending" in line:
            print(f"[log_data DEBUG] {SERIAL} Found keyword: {line}", flush=True)

        # Chỉ xử lý dòng log chứa event gửi đi từ Unity/Game
        if "Start sending event to main app:" not in line:
            return

        print(f"[log_data] {SERIAL} RAW EVENT: {line}")

        if "ad_impression" not in line:
            return

        match = re.search(r"(\{.*\})", line)
        if not match:
            return

        try:
            obj = json.loads(match.group(1))
            events = obj.get("events") or []
            if not events:
                return

            event = events[0]
            if not event or event.get("name") != "ad_impression":
                return

            p = event.get("params", {})
            value = float(p.get("value") or 0)
            ad_format = p.get("ad_format")
            ad_unit_name = p.get("ad_unit_name", "")

            # --- Deduplication Logic (Chống trùng lặp) ---
            nonlocal last_event_signature, last_event_time
            current_time = time.time()
            event_signature = (ad_format, value, ad_unit_name)

            # Nếu sự kiện giống hệt sự kiện trước đó trong vòng 5 giây -> Bỏ qua
            if event_signature == last_event_signature and (current_time - last_event_time) < 5.0:
                print(f"[log_data] {SERIAL} Duplicate event detected, skipping: {event_signature}", flush=True)
                return

            last_event_signature = event_signature
            last_event_time = current_time
            # ---------------------------------------------

            # Create log entry for batching
            log_entry = create_log_entry("ad_impression", ad_format, value, ad_unit_name)

            # Rate limiting check
            if not rate_limiter.allow():
                print(f"[log_data] {SERIAL} Rate limited, skipping log entry")
                return

            if ad_format == "BANNER":
                nonlocal TOTAL_BANNER_REVENUE
                TOTAL_BANNER_REVENUE += value
                print(f"[log_data] {SERIAL} ACCUMULATED BANNER: +{value} | Total: {TOTAL_BANNER_REVENUE}", flush=True)
                # Banner events are batched, not sent immediately
            else:
                # Add to batcher (non-blocking) only for non-banner logs if needed, or remove completely if handled by send_event_to_api
                print(f"[log_data] {SERIAL} DETECTED AD: {ad_format} | Value: {value}")
                # Critical events (Inter/Rewarded) still sent immediately for real-time processing
                send_event_to_api(ad_format, value)

        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    try:
        print(f"[log_data] Entering main loop for {SERIAL}...", flush=True)
        for line in adb.stdout:
            buffer += line
            lines = buffer.split("\n")
            buffer = lines[-1]  # giữ dòng cuối chưa hoàn chỉnh

            for l in lines[:-1]:
                process_line(l.strip())
    finally:
        # Flush remaining batched logs before shutdown
        batcher.flush_remaining()

        send_end_session()
        adb.terminate()
        print("ADB process closed")

        # Cleanup lock file to prevent accumulation
        try:
            if 'lock_handle' in locals() and lock_handle:
                lock_handle.close()
            if os.path.exists(lock_file_path):
                os.remove(lock_file_path)
                print(f"[Cleanup] Removed lock file for {SERIAL}")
        except Exception as e:
            print(f"[Cleanup] Failed to cleanup lock file for {SERIAL}: {e}")


if __name__ == "__main__":
    run_collector()