import time
import os
import sys
import traceback
import threading
import requests
import psutil
import tempfile
from .config import LOG_FILE
from typing import Optional, Dict, List

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

    lock_dir = tempfile.gettempdir()
    print(f"[Init] Scanning lock files in {lock_dir}...") # Debug log

    try:
        for filename in os.listdir(lock_dir):
            if not filename.startswith("log_data_") or not filename.endswith(".lock"):
                continue

            filepath = os.path.join(lock_dir, filename)

            try:
                with open(filepath, 'r') as f:
                    content = f.read().strip()
                    if not content:
                        os.remove(filepath)
                        continue
                    pid = int(content)

                # --- ĐOẠN FIX QUAN TRỌNG ---
                is_running = False
                try:
                    if pid <= 0:
                        is_running = False # PID 0 hoặc âm là không hợp lệ với user process
                    else:
                        is_running = psutil.pid_exists(pid)
                except OSError:
                    # Catch WinError 87 hoặc Access Denied -> Coi như process không tồn tại
                    is_running = False
                except Exception:
                    is_running = False
                # ---------------------------

                # Check if process still exists
                if not is_running:
                    try:
                        os.remove(filepath)
                        print(f"[cleanup] Removed stale lock file: {filename} (PID {pid} dead)")
                    except OSError:
                        pass # File có thể đã bị xóa bởi thread khác

            except (ValueError, FileNotFoundError, PermissionError):
                # Lock file corrupted or process gone
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                        print(f"[cleanup] Removed invalid lock file: {filename}")
                except:
                    pass
    except Exception as e:
        print(f"[Init] Warning: Failed to cleanup lock files: {e}")

def clear_console():
    try:
        os.system("cls")
    except Exception:
        pass

# Exception Memory Leak Prevention Utilities

def format_exception_safe(e: Exception = None) -> Dict[str, str]:
    """
    Safely format exception details without keeping object references

    Args:
        e: Exception object (if None, uses sys.exc_info())

    Returns:
        Dict with safe string representations
    """
    if e is None:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        if exc_value is None:
            return {"type": "Unknown", "message": "No exception", "traceback": "", "timestamp": str(time.time())}
        e = exc_value
        # Use the traceback from sys.exc_info()
        tb_str = ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback)) if exc_traceback else ""
    else:
        tb_str = traceback.format_exc()

    return {
        "type": type(e).__name__,
        "message": str(e),
        "traceback": tb_str,
        "timestamp": str(time.time())
    }

def safe_log_exception(context: str, operation: str = None, include_traceback: bool = False):
    """
    Safely log current exception without memory leaks

    Args:
        context: Where the error occurred (e.g., "api_client")
        operation: Specific operation (e.g., "fetch_commands")
        include_traceback: Whether to include full traceback
    """
    exc_type, exc_value, exc_traceback = sys.exc_info()

    if exc_value:
        error_info = {
            'context': context,
            'operation': operation or 'unknown',
            'type': exc_type.__name__ if exc_type else 'Unknown',
            'message': str(exc_value),
            'timestamp': time.time()
        }

        if include_traceback and exc_traceback:
            error_info['traceback'] = ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))

        # Log safely without keeping object references
        log_msg = f"[{error_info['context']}] {error_info['type']}: {error_info['message']}"
        if operation:
            log_msg += f" in {operation}"

        print(log_msg)

        # For critical errors, write to file safely
        if error_info['type'] in ['MemoryError', 'SystemExit', 'KeyboardInterrupt']:
            try:
                with open('critical_errors.log', 'a') as f:
                    f.write(f"{error_info['timestamp']}: {log_msg}\n")
            except:
                pass  # Don't fail if logging fails

    # Clear exception info to prevent any potential leaks
    sys.exc_info()  # This clears the exception info

class ExceptionSafeStorage:
    """Thread-safe storage for exception info without memory leaks"""

    def __init__(self, max_entries: int = 500):
        self._entries: List[Dict[str, str]] = []
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def add_exception(self, context: str, operation: str = None):
        """Add exception info safely without keeping object references"""
        with self._lock:
            entry = format_exception_safe()
            entry.update({
                'context': context,
                'operation': operation or 'unknown'
            })

            self._entries.append(entry)

            # Maintain size limit to prevent unbounded growth
            if len(self._entries) > self._max_entries:
                self._entries.pop(0)  # Remove oldest

    def get_recent(self, limit: int = 10) -> List[Dict[str, str]]:
        """Get recent exceptions safely"""
        with self._lock:
            return self._entries[-limit:] if self._entries else []

    def clear_old(self, older_than_seconds: int = 3600):
        """Clear exceptions older than specified time"""
        cutoff_time = time.time() - older_than_seconds
        with self._lock:
            self._entries = [
                entry for entry in self._entries
                if float(entry.get('timestamp', 0)) > cutoff_time
            ]

    def get_stats(self) -> Dict[str, int]:
        """Get storage statistics"""
        with self._lock:
            return {
                'total_entries': len(self._entries),
                'max_entries': self._max_entries,
                'utilization_percent': (len(self._entries) / self._max_entries) * 100
            }

# Global exception storage for monitoring
exception_storage = ExceptionSafeStorage(max_entries=500)
