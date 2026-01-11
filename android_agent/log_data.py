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

SERIAL = sys.argv[1] if len(sys.argv) > 1 else None
if not SERIAL:
    print("Usage: python log_data.py <serial>", file=sys.stderr)
    sys.exit(1)

API_URL = "http://160.25.81.154:9000/api/v1/report"
SEND_INTERVAL = 2.0  # Gửi API mỗi 2 giây

# ================== STATS ==================
stats = {
    "serial": SERIAL,
    "banner_usd": 0,
    "rewarded": {"pass": 0, "usd": 0},
    "inter": {"pass": 0, "usd": 0},
    "fail": 0,
    "parent_app_count": 0,
    "start_sending_count": 0,
}
stats_lock = threading.Lock()
last_sent: dict = None

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
    bufsize=1
)

buffer = ""

def send_stats_to_api():
    """Gửi stats lên API theo định kỳ"""
    global last_sent
    while adb.poll() is None:
        try:
            with stats_lock:
                payload = dict(stats)
                # Ẩn parent_app_count và start_sending_count khi gửi
                extra_data = {k: v for k, v in payload.items() if k not in ["parent_app_count", "start_sending_count"]}
            # Nếu dữ liệu không thay đổi so với lần gửi trước, bỏ qua
            if last_sent is not None and extra_data == last_sent:
                # không gửi, chờ lần tiếp theo
                time.sleep(SEND_INTERVAL)
                continue
            # Gói payload theo yêu cầu: { "extra_data": { ... } }
            payload = {"extra_data": extra_data}
            resp = requests.post(API_URL, json=payload, timeout=5)
            # Nếu server trả về mã 2xx thì coi là gửi thành công
            if resp.ok:
                print(f"[log_data] {SERIAL} sent to API (status {resp.status_code}): {json.dumps(payload)}")
                # Lưu bản sao payload đã gửi
                last_sent = copy.deepcopy(extra_data)
            else:
                print(f"[log_data err] {SERIAL} HTTP {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"[log_data err] {SERIAL}: {e}")
        time.sleep(SEND_INTERVAL)

def process_line(line):
    global stats
    
    if "ad_impression" not in line:
        return
    
    with stats_lock:
        if "LogEventParentApp:" in line:
            stats["parent_app_count"] += 1
        if "Start sending event to main app:" in line:
            stats["start_sending_count"] += 1
        
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
            stats["fail"] = stats["parent_app_count"] - stats["start_sending_count"]
            
            ad_format = p.get("ad_format")
            if ad_format == "BANNER":
                stats["banner_usd"] += value
            elif ad_format == "REWARDED":
                stats["rewarded"]["pass"] += 1
                stats["rewarded"]["usd"] += value
            elif ad_format == "INTER":
                stats["inter"]["pass"] += 1
                stats["inter"]["usd"] += value
            
            # In stats cộng dồn, ẩn parent_app_count và start_sending_count
            os.system("cls" if os.name == "nt" else "clear")
            print("================ STATS ================")
            stats_to_show = {k: v for k, v in stats.items() if k not in ["parent_app_count", "start_sending_count"]}
            print(json.dumps(stats_to_show, indent=2))
        except (json.JSONDecodeError, KeyError, TypeError):
            # Ignore parse error
            pass

# Khởi chạy thread gửi API
api_sender_thread = threading.Thread(target=send_stats_to_api, daemon=True)
api_sender_thread.start()

try:
    for line in adb.stdout:
        buffer += line
        lines = buffer.split("\n")
        buffer = lines[-1]  # giữ dòng cuối chưa hoàn chỉnh
        
        for l in lines[:-1]:
            process_line(l.strip())
finally:
    adb.terminate()
    api_sender_thread.join(timeout=2)
    print("ADB process closed")
