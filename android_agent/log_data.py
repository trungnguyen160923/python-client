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
from dotenv import load_dotenv

SERIAL = sys.argv[1] if len(sys.argv) > 1 else None
ROOM_HASH = sys.argv[2] if len(sys.argv) > 2 else "unknown"
GAME_PACKAGE = sys.argv[3] if len(sys.argv) > 3 else "unknown"
if not SERIAL:
    print("Usage: python log_data.py <serial>", file=sys.stderr)
    sys.exit(1)

print(f"[log_data] INIT: Serial={SERIAL} Room={ROOM_HASH} Package={GAME_PACKAGE}", flush=True)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
API_URL = os.getenv("API_URL", "http://160.25.81.154:9000") + "/api/v1/report"
SEND_INTERVAL = 2.0  # Gửi API mỗi 2 giây

# ================== STATS ==================
START_RUN = int(datetime.now().timestamp())  # Cộng 7 giờ (7*3600)
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


def send_event_to_api(ad_format, value):
    """Gửi event lên API ngay lập tức"""
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
    global is_ended
    if is_ended:
        return
    is_ended = True

    try:
        extra_data = {
            "start_run": START_RUN,
            "end_run": int(datetime.now().timestamp()),  # Chỉ gửi khi stop, cộng 7 giờ
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
        global last_event_signature, last_event_time
        current_time = time.time()
        event_signature = (ad_format, value, ad_unit_name)

        # Nếu sự kiện giống hệt sự kiện trước đó trong vòng 5 giây -> Bỏ qua
        if event_signature == last_event_signature and (current_time - last_event_time) < 5.0:
            print(f"[log_data] {SERIAL} Duplicate event detected, skipping: {event_signature}", flush=True)
            return

        last_event_signature = event_signature
        last_event_time = current_time
        # ---------------------------------------------

        if ad_format == "BANNER":
            global TOTAL_BANNER_REVENUE
            TOTAL_BANNER_REVENUE += value
            print(f"[log_data] {SERIAL} ACCUMULATED BANNER: +{value} | Total: {TOTAL_BANNER_REVENUE}", flush=True)
            # Không gửi API ngay với Banner
        else:
            print(f"[log_data] {SERIAL} DETECTED AD: {ad_format} | Value: {value}")
            # Gửi API ngay lập tức với Inter/Rewarded
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
    send_end_session()
    adb.terminate()
    print("ADB process closed")
