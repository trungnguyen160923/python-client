import time
import os
import requests
from .config import LOG_FILE
from typing import Optional

def append_error_log(serial: str, message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{timestamp}   {serial}   :   {message}\n")
    except Exception:
        pass

def download_temp_file(url: str) -> Optional[str]:
    from pathlib import Path
    import sys
    import hashlib
    try:
        filename = url.split("/")[-1] or "temp_file"
        # Nếu không có đuôi .apk hoặc .apex thì thêm .apk (ưu tiên .apk)
        if not (filename.endswith('.apk') or filename.endswith('.apex')):
            filename += '.apk'
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        unique_filename = f"{url_hash}_{filename}"
        if getattr(sys, 'frozen', False):
            local_path = Path(sys.executable).with_name(unique_filename)
        else:
            local_path = Path(__file__).parent.parent / unique_filename
        if local_path.exists():
            print(f"[download] File {local_path} đã tồn tại, dùng lại.")
            return str(local_path)
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

def clear_console():
    try:
        os.system("cls")
    except Exception:
        pass
