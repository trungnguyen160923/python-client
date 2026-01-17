from pathlib import Path
import sys
import os
from dotenv import load_dotenv

if getattr(sys, 'frozen', False):
    # Nếu đang chạy file EXE, lấy đường dẫn của file EXE
    BASE_DIR = Path(sys.executable).parent
    
    # [FIX] Ưu tiên load .env từ bên trong file EXE (sys._MEIPASS)
    if hasattr(sys, '_MEIPASS'):
        env_path = Path(sys._MEIPASS) / ".env"
    else:
        env_path = BASE_DIR / ".env"
        
    # Fallback: Nếu không có trong bundle, tìm bên cạnh EXE
    if not env_path.exists():
        env_path = BASE_DIR / ".env"
else:
    # Nếu đang chạy code Python, lấy đường dẫn thư mục gốc project
    BASE_DIR = Path(__file__).parent.parent
    env_path = BASE_DIR / ".env"

print(f"[CONFIG] Checking .env at: {env_path}", flush=True)

if env_path.exists():
    load_dotenv(env_path)
    api_url = os.getenv("API_BASE_URL")
    if api_url:
        print(f"[CONFIG] Loaded API_BASE_URL: {api_url}", flush=True)
    else:
        print(f"[CONFIG] ⚠️ Found .env but API_BASE_URL is missing or empty", flush=True)
else:
    print(f"[CONFIG] ❌ .env file NOT FOUND at {env_path}", flush=True)

CONFIG_FILE = BASE_DIR / "config.txt"
LOG_FILE = BASE_DIR / "log_error.txt"
REPORT_INTERVAL_SEC = 3.0
FETCH_INTERVAL_SEC = 1.0
PRINT_INTERVAL_SEC = 1.0
STATUS_INTERVAL_SEC = 3.0
CLEAR_INTERVAL_SEC = 120.0

# Queue configuration to prevent memory accumulation
MAX_COMMANDS_QUEUE_SIZE = 1000
QUEUE_WARNING_THRESHOLD = 0.8  # 80% capacity warning

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
