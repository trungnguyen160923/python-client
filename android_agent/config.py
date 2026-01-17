from pathlib import Path

CONFIG_FILE = Path(__file__).parent.parent / "config.txt"
LOG_FILE = Path(__file__).parent.parent / "log_error.txt"
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
