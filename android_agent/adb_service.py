import shlex
import subprocess
from typing import Dict, List

def run_adb_once(serial: str, command_text: str, timeout: int = None) -> Dict[str, object]:
    # Dynamic timeout based on command type
    if timeout is None:
        cmd_lower = command_text.strip().lower()
        if cmd_lower.startswith("install"):
            timeout = 300  # 5 minutes for APK installation (large files, slow devices)
        elif cmd_lower.startswith("push"):
            timeout = 120  # 2 minutes for file transfer
        elif cmd_lower.startswith("pull"):
            timeout = 120  # 2 minutes for file transfer
        elif "net-install" in cmd_lower or "download" in cmd_lower:
            timeout = 180  # 3 minutes for network operations
        else:
            timeout = 60   # 1 minute for regular commands

    cmd = ["adb", "-s", serial] + shlex.split(command_text)
    code = -1
    out = ""
    err = ""
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        # Nếu quá giờ, kill process để tránh treo luồng
        if proc:
            proc.kill()
            # Cố gắng lấy output còn sót lại (nếu có)
            try:
                out, err = proc.communicate(timeout=1)
            except Exception:
                pass
        err = f"Command timed out after {timeout} seconds (command: {command_text[:50]}...)"
        code = 124  # Mã lỗi thường dùng cho timeout
    except Exception as exc:
        err = str(exc)
    return {
        "serial": serial,
        "code": code,
        "stdout": (out or "").strip(),
        "stderr": (err or "").strip(),
    }

def list_adb_devices() -> List[Dict[str, object]]:
    try:
        proc = subprocess.Popen(
            ["adb", "devices"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate(timeout=5)
        if proc.returncode != 0:
            return []
    except Exception:
        return []
    lines = (out or "").splitlines()
    devices: List[Dict[str, object]] = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        status = "active" if state == "device" else state
        devices.append({
            "serial": serial,
            "data": {},
            "status": status,
            "device_type": "android",
        })
    return devices
