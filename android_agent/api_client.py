
import requests
from typing import List, Dict
from .adb_service import list_adb_devices
from .config import REPORT_INTERVAL_SEC
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
API_BASE_URL = os.getenv("API_BASE_URL", "http://160.25.81.154:9000")

def report_devices(room_hash_value: str):
    url = f"{API_BASE_URL}/api/v1/report-devices"
    devices = list_adb_devices()
    payload = {
        "room_hash": room_hash_value,
        "devices": devices,
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as exc:
        print(f"[report err] {exc}")

def fetch_commands(room_hash_value: str) -> List[Dict[str, object]]:
    url = f"{API_BASE_URL}/api/v1/subscribe/{room_hash_value}"
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("commands") or []
        else:
            print(f"[fetch warn] HTTP {resp.status_code}")
    except Exception as exc:
        print(f"[fetch err] {exc}")
    return []

def report_command_result(payload: dict):
    url = f"{API_BASE_URL}/api/v1/report-result"
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as exc:
        print(f"[report-result err] {payload.get('serial', '')}: {exc}")
