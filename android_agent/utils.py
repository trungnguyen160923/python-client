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
    import uuid
    try:
        # 1. Extract filename from URL (handle query parameters)
        filename = url.split("/")[-1].split("?")[0] or "temp_file"

        # 2. PREVENTION: Ensure .apk extension at creation time (Best Practice)
        # This eliminates the need for os.rename() later in command_processor.py
        if not filename.lower().endswith('.apk'):
            filename += '.apk'

        # 3. Create unique filename with UUID (from previous fix)
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        unique_id = str(uuid.uuid4())[:8]  # Short UUID (8 chars) for filename
        unique_filename = f"{url_hash}_{unique_id}_{filename}"
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

def cleanup_old_logs(log_dir="logs", days=3):
    """Clean up old log files to prevent disk space issues"""
    import time

    if not os.path.exists(log_dir):
        return

    now = time.time()
    cutoff = now - (days * 86400)  # Convert days to seconds

    cleaned_count = 0
    total_size_cleaned = 0

    for filename in os.listdir(log_dir):
        filepath = os.path.join(log_dir, filename)

        # Only process files (not directories)
        if not os.path.isfile(filepath):
            continue

        # Check file modification time
        try:
            file_mtime = os.path.getmtime(filepath)
            file_size = os.path.getsize(filepath)

            if file_mtime < cutoff:
                os.remove(filepath)
                cleaned_count += 1
                total_size_cleaned += file_size
                print(f"[Cleanup] Removed old log: {filename}")
        except Exception as e:
            print(f"[Cleanup] Error processing {filename}: {e}")

    if cleaned_count > 0:
        size_mb = total_size_cleaned / (1024 * 1024)
        print(f"[Cleanup] Removed {cleaned_count} old log files ({size_mb:.1f} MB)")

def cleanup_temp_files(directory=".", older_than_hours=24):
    """Clean up temporary files older than specified hours to prevent disk space issues"""
    import time

    if not os.path.exists(directory):
        return

    now = time.time()
    cutoff = now - (older_than_hours * 3600)  # Convert hours to seconds

    cleaned_count = 0
    cleaned_size = 0

    for filename in os.listdir(directory):
        filepath = os.path.join(directory, filename)

        # Only process files (not directories)
        if not os.path.isfile(filepath):
            continue

        # Skip certain file types we want to keep
        if filename.endswith('.log') or filename.startswith('session_'):
            continue

        # Check file age
        try:
            file_mtime = os.path.getmtime(filepath)
            file_size = os.path.getsize(filepath)

            if file_mtime < cutoff:
                os.remove(filepath)
                cleaned_count += 1
                cleaned_size += file_size
                print(f"[Cleanup] Removed old temp file: {filename}")
        except Exception as e:
            print(f"[Cleanup] Error processing temp file {filename}: {e}")

    if cleaned_count > 0:
        size_mb = cleaned_size / (1024 * 1024)
        print(f"[Cleanup] Removed {cleaned_count} temp files ({size_mb:.1f} MB)")

def cleanup_lock_files():
    """Clean up stale lock files from crashed processes"""
    import tempfile

    lock_dir = tempfile.gettempdir()
    cleaned_count = 0

    if not os.path.exists(lock_dir):
        return

    for filename in os.listdir(lock_dir):
        if not filename.startswith("log_data_") or not filename.endswith(".lock"):
            continue

        filepath = os.path.join(lock_dir, filename)

        try:
            # Read PID from lock file
            with open(filepath, 'r') as f:
                pid_str = f.read().strip()

            try:
                pid = int(pid_str)

                # Check if process still exists (Unix/Linux/Mac)
                try:
                    os.kill(pid, 0)  # Signal 0 doesn't kill, just checks existence
                    # Process exists, keep lock file
                    continue
                except OSError:
                    # Process doesn't exist, safe to remove lock
                    pass

            except ValueError:
                # Invalid PID format, remove corrupted lock
                pass

            # Remove stale/invalid lock file
            os.remove(filepath)
            cleaned_count += 1
            print(f"[Cleanup] Removed stale lock file: {filename}")

        except (FileNotFoundError, OSError) as e:
            # Lock file already gone or permission issue
            print(f"[Cleanup] Could not process lock file {filename}: {e}")

    if cleaned_count > 0:
        print(f"[Cleanup] Removed {cleaned_count} stale lock files")

def clear_console():
    try:
        os.system("cls")
    except Exception:
        pass
