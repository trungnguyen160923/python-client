import subprocess
import time
from typing import Dict, List, Optional
from pathlib import Path

MAX_LOG_COLLECTORS = 80
SPAWN_DELAY = 0.1  # 100ms delay giữa các spawn để tránh spike ADB


def start_collectors(serials: List[str], max_limit: int = MAX_LOG_COLLECTORS) -> Dict[str, Optional[subprocess.Popen]]:
    """
    Khởi chạy log collectors cho danh sách serials.
    
    Args:
        serials: Danh sách serial device
        max_limit: Giới hạn tối đa số collectors (mặc định 20)
    
    Returns:
        Dict[serial] -> Popen object hoặc None nếu spawn thất bại
    """
    log_procs = {}
    log_data_script = Path(__file__).parent / "log_data.py"
    
    for i, serial in enumerate(serials):
        if i >= max_limit:
            print(f"[log_manager warn] Vượt quá MAX_LOG_COLLECTORS ({max_limit}), dừng spawn")
            break
        
        try:
            # Spawn: python -u android_agent/log_data.py <serial>
            proc = subprocess.Popen(
                ["python", "-u", str(log_data_script), serial],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
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
            # Terminate
            proc.terminate()
            try:
                proc.wait(timeout=2)
                print(f"[log_manager] Terminated collector for {serial}")
            except subprocess.TimeoutExpired:
                # Kill nếu terminate không hoạt động
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
