import shlex
import subprocess
from typing import Dict, List

def run_adb_once(serial: str, command_text: str) -> Dict[str, object]:
    cmd = ["adb", "-s", serial] + shlex.split(command_text)
    code = -1
    out = ""
    err = ""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate()
        code = proc.returncode
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
