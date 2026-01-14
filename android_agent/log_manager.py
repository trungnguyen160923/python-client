import subprocess
import time
import os
import signal
import sys
from typing import Dict, List, Optional
from pathlib import Path

MAX_LOG_COLLECTORS = 80
SPAWN_DELAY = 0.1  # 100ms delay giữa các spawn để tránh spike ADB


def start_collectors(serials: List[str], room_hash: str, game_package: str, start_run: int = None, max_limit: int = MAX_LOG_COLLECTORS) -> Dict[str, Optional[subprocess.Popen]]:
    """
    Khởi chạy log collectors cho danh sách serials.
    
    Args:
        serials: Danh sách serial device
        room_hash: Room hash hiện tại
        game_package: Package name của game đang chạy
        start_run: Timestamp bắt đầu session (optional)
        max_limit: Giới hạn tối đa số collectors (mặc định 20)
    
    Returns:
        Dict[serial] -> Popen object hoặc None nếu spawn thất bại
    """
    log_procs = {}
    log_data_script = Path(__file__).parent / "log_data.py"
    
    # Trên Windows, cần tạo process group mới để có thể gửi tín hiệu CTRL_BREAK
    popen_kwargs = {}
    if os.name == 'nt':
        popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

    if start_run is None:
        start_run = int(time.time())

    for i, serial in enumerate(serials):
        if i >= max_limit:
            print(f"[log_manager warn] Vượt quá MAX_LOG_COLLECTORS ({max_limit}), dừng spawn")
            break
        
        try:
            # Kiểm tra đang chạy source hay chạy exe
            if getattr(sys, 'frozen', False):
                # Đang chạy file EXE - gọi chính mình kèm cờ --worker
                executable = sys.executable
                cmd = [executable, "--worker", "log_data", serial, room_hash, game_package, str(start_run)]
            else:
                # Đang chạy code Python thường (Dev)
                executable = sys.executable  # python.exe
                script_path = Path(__file__).parent / "log_data.py"
                cmd = [executable, "-u", str(script_path), serial, room_hash, game_package, str(start_run)]

            print(f"[log_manager] Spawning collector for {serial} with start_run={start_run}", flush=True)
            proc = subprocess.Popen(
                cmd,
                text=True,
                bufsize=1,
                **popen_kwargs
            )
            log_procs[serial] = proc
            print(f"[log_manager] Started collector for {serial} (PID: {proc.pid})")

            # Delay để tránh spike ADB
            if i < len(serials) - 1:
                time.sleep(SPAWN_DELAY)
        except Exception as e:
            print(f"[log_manager err] Failed to start collector for {serial}: {e}")
            log_procs[serial] = None
    
    return log_procs


def stop_collectors(log_procs: Dict[str, Optional[subprocess.Popen]]) -> None:
    """
    Dừng và cleanup tất cả log collectors.
    
    Args:
        log_procs: Dict serial -> Popen object
    """
    for serial, proc in log_procs.items():
        if proc is None:
            continue
        
        try:
            print(f"[log_manager] Requesting stop for {serial}...", flush=True)
            # Graceful shutdown: Gửi tín hiệu dừng nhẹ nhàng thay vì kill ngay
            if os.name == 'nt':
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()

            try:
                # Tăng timeout lên 5s để log_data.py kịp gửi API (request timeout là 3s)
                proc.wait(timeout=5)
                print(f"[log_manager] Terminated collector for {serial}")
            except subprocess.TimeoutExpired:
                # Kill nếu terminate không hoạt động
                print(f"[log_manager] Timeout waiting for {serial}, killing...", flush=True)
                proc.kill()
                proc.wait(timeout=1)
                print(f"[log_manager] Killed collector for {serial}")
        except Exception as e:
            print(f"[log_manager err] Failed to stop collector for {serial}: {e}")


def is_collector_alive(proc: Optional[subprocess.Popen]) -> bool:
    """Kiểm tra xem collector process còn chạy không"""
    if proc is None:
        return False
    return proc.poll() is None
