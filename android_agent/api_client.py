
import requests
from requests.adapters import HTTPAdapter
import time
from typing import List, Dict
from .adb_service import list_adb_devices
from .config import REPORT_INTERVAL_SEC
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
API_BASE_URL = os.getenv("API_BASE_URL", "http://160.25.81.154:9000")

# Global session with connection pooling (thread-safe module-level initialization)
session = requests.Session()

# Configure connection pooling (disable urllib3 retry to avoid conflict with manual retry logic)
adapter = HTTPAdapter(
    pool_connections=10,    # Keep 10 connections alive
    pool_maxsize=20,        # Max 20 total connections
    max_retries=False       # Disable urllib3 retry, use manual retry logic
)

# Mount adapter for both HTTP and HTTPS
session.mount('http://', adapter)
session.mount('https://', adapter)

def report_devices(room_hash_value: str):
    """Report devices with retry logic (fire-and-forget)"""
    url = f"{API_BASE_URL}/api/v1/report-devices"
    devices = list_adb_devices()
    payload = {
        "room_hash": room_hash_value,
        "devices": devices,
    }

    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            resp = session.post(url, json=payload, timeout=5)

            if resp.status_code in (200, 201):
                return  # Success
            elif resp.status_code >= 500:  # Server errors - retry
                if attempt < max_retries:
                    delay = 1 * (2 ** attempt)  # Exponential: 1s, 2s, 4s
                    print(f"[report] Server error {resp.status_code}, retrying in {delay}s (attempt {attempt + 1}/{max_retries + 1})")
                    time.sleep(delay)
                    continue
                else:
                    print(f"[report] Server error {resp.status_code} after {max_retries + 1} attempts")
                    return
            else:  # Client errors (4xx) - don't retry
                print(f"[report] Client error {resp.status_code}, not retrying")
                return

        except (requests.RequestException, requests.Timeout) as e:
            if attempt < max_retries:
                delay = 1 * (2 ** attempt)
                print(f"[report] Network error, retrying in {delay}s (attempt {attempt + 1}/{max_retries + 1}): {e}")
                time.sleep(delay)
            else:
                print(f"[report] Network error after {max_retries + 1} attempts: {e}")
                return

def fetch_commands(room_hash_value: str) -> List[Dict[str, object]]:
    """Fetch commands with retry logic"""
    url = f"{API_BASE_URL}/api/v1/subscribe/{room_hash_value}"
    max_retries = 3

    for attempt in range(max_retries + 1):
        try:
            resp = session.get(url, timeout=5)

            if resp.status_code == 200:
                data = resp.json()
                return data.get("commands") or []
            elif resp.status_code >= 500:  # Server errors - retry
                if attempt < max_retries:
                    delay = 1 * (2 ** attempt)  # Exponential: 1s, 2s, 4s
                    print(f"[fetch] Server error {resp.status_code}, retrying in {delay}s (attempt {attempt + 1}/{max_retries + 1})")
                    time.sleep(delay)
                    continue
                else:
                    print(f"[fetch] Server error {resp.status_code} after {max_retries + 1} attempts")
                    return []
            else:  # Client errors (4xx) - don't retry
                print(f"[fetch] Client error {resp.status_code}, not retrying")
                return []

        except (requests.RequestException, requests.Timeout) as e:
            if attempt < max_retries:
                delay = 1 * (2 ** attempt)
                print(f"[fetch] Network error, retrying in {delay}s (attempt {attempt + 1}/{max_retries + 1}): {e}")
                time.sleep(delay)
            else:
                print(f"[fetch] Network error after {max_retries + 1} attempts: {e}")
                return []

    return []  # Fallback (should not reach here)

def report_command_result(payload: dict):
    """Report command result with retry logic"""
    url = f"{API_BASE_URL}/api/v1/report-result"
    serial = payload.get('serial', 'unknown')
    max_retries = 3

    for attempt in range(max_retries + 1):
        try:
            resp = session.post(url, json=payload, timeout=5)

            if resp.status_code in (200, 201):
                return  # Success
            elif resp.status_code >= 500:  # Server errors - retry
                if attempt < max_retries:
                    delay = 1 * (2 ** attempt)  # Exponential: 1s, 2s, 4s
                    print(f"[report-result] Server error {resp.status_code} for {serial}, retrying in {delay}s (attempt {attempt + 1}/{max_retries + 1})")
                    time.sleep(delay)
                    continue
                else:
                    print(f"[report-result] Server error {resp.status_code} for {serial} after {max_retries + 1} attempts")
                    return
            else:  # Client errors (4xx) - don't retry
                print(f"[report-result] Client error {resp.status_code} for {serial}, not retrying")
                return

        except (requests.RequestException, requests.Timeout) as e:
            if attempt < max_retries:
                delay = 1 * (2 ** attempt)
                print(f"[report-result] Network error for {serial}, retrying in {delay}s (attempt {attempt + 1}/{max_retries + 1}): {e}")
                time.sleep(delay)
            else:
                print(f"[report-result] Network error for {serial} after {max_retries + 1} attempts: {e}")
                return
