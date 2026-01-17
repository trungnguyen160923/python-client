
import requests
import time
import random  # For jitter in backoff
import sys
import os
from typing import Dict, List, Optional
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv
from .adb_service import list_adb_devices
from .utils import safe_log_exception, exception_storage

# Load environment configuration
if getattr(sys, 'frozen', False):
    # [FIX] Ưu tiên load .env từ bên trong file EXE (sys._MEIPASS)
    if hasattr(sys, '_MEIPASS'):
        env_path = os.path.join(sys._MEIPASS, ".env")
    else:
        env_path = os.path.join(os.path.dirname(sys.executable), ".env")
    
    if not os.path.exists(env_path):
        env_path = os.path.join(os.path.dirname(sys.executable), ".env")
else:
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

load_dotenv(env_path)
API_BASE_URL = os.getenv("API_BASE_URL")

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

class CircuitBreaker:
    """Circuit breaker for API resilience - prevents cascade failures"""

    def __init__(self, failure_threshold=5, recovery_timeout=60):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.last_failure_time = 0
        self.state = 'CLOSED'  # CLOSED, OPEN, HALF_OPEN

    def should_attempt(self) -> bool:
        """Check if request should be attempted based on circuit state"""
        current_time = time.time()

        if self.state == 'CLOSED':
            return True
        elif self.state == 'OPEN':
            # Check if recovery timeout has passed
            if current_time - self.last_failure_time > self.recovery_timeout:
                self.state = 'HALF_OPEN'
                print("[API] Circuit breaker transitioning to HALF_OPEN")
                return True
            return False
        else:  # HALF_OPEN
            return True

    def record_success(self):
        """Record successful request - reset circuit"""
        self.failure_count = 0
        if self.state != 'CLOSED':
            print("[API] Circuit breaker resetting to CLOSED")
        self.state = 'CLOSED'

    def record_failure(self):
        """Record failed request - potentially open circuit"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            if self.state != 'OPEN':
                print(f"[API] Circuit breaker OPENING after {self.failure_count} failures")
            self.state = 'OPEN'

# Global circuit breaker instance
api_circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)

def get_dynamic_timeout(operation: str) -> float:
    """Dynamic timeout based on operation type and expected duration"""
    timeouts = {
        'report_devices': 3.0,      # Quick status report
        'fetch_commands': 30.0,     # Long polling - need longer timeout!
        'report_result': 10.0,      # Critical data - longer timeout
        'start_session': 8.0,       # Session initialization
        'default': 5.0
    }
    return timeouts.get(operation, timeouts['default'])

def calculate_backoff_delay(attempt: int, base_delay: float = 1.0, max_delay: float = 30.0) -> float:
    """Calculate exponential backoff delay with jitter to prevent thundering herd"""
    # Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s...
    delay = min(base_delay * (2 ** attempt), max_delay)

    # Add ±25% jitter to distribute load when multiple clients recover simultaneously
    jitter_range = delay * 0.25
    jitter = random.uniform(-jitter_range, jitter_range)

    return max(0.1, delay + jitter)  # Minimum 100ms delay

def classify_error_for_retry(response: requests.Response = None, exception: Exception = None) -> tuple[bool, str]:
    """
    Classify error for smart retry decisions following HTTP semantics

    Returns:
        (should_retry: bool, reason: str)
    """
    if response:
        status_code = response.status_code

        # Success codes
        if status_code in (200, 201, 202):
            return False, "success"

        # Client errors (4xx) - don't retry (except rate limiting)
        elif 400 <= status_code < 500:
            if status_code == 429:  # Rate limited - special case, can retry
                return True, "rate_limited"
            reasons = {
                401: "authentication_error",
                403: "authorization_error",
                404: "not_found",
                422: "validation_error"
            }
            return False, reasons.get(status_code, "client_error")

        # Server errors (5xx) - retry
        elif status_code >= 500:
            reasons = {
                500: "internal_server_error",
                502: "bad_gateway",
                503: "service_unavailable",
                504: "gateway_timeout"
            }
            return True, reasons.get(status_code, "server_error")

    elif exception:
        # Network/timeout errors - retry
        if isinstance(exception, requests.Timeout):
            return True, "timeout"
        elif isinstance(exception, requests.ConnectionError):
            return True, "connection_error"
        elif isinstance(exception, requests.RequestException):
            return True, "request_error"

    return False, "unknown"

def api_request_with_resilience(method: str, url: str, operation: str,
                               json_data=None, max_retries=3, serial_context="") -> Optional[requests.Response]:
    """
    Unified API client with production-grade resilience patterns

    Args:
        method: HTTP method ('GET', 'POST')
        url: API endpoint URL
        operation: Operation name for logging/metrics ('fetch_commands', 'report_result', etc.)
        json_data: JSON payload for POST requests
        max_retries: Maximum retry attempts
        serial_context: Device serial for contextual logging

    Returns:
        requests.Response on success, None on permanent failure
    """
    # Check circuit breaker first
    if not api_circuit_breaker.should_attempt():
        print(f"[API] Circuit breaker OPEN, skipping {operation}")
        return None

    timeout = get_dynamic_timeout(operation)
    context = f" for {serial_context}" if serial_context else ""

    for attempt in range(max_retries + 1):
        try:
            start_time = time.time()

            # Make request with appropriate method
            if method.upper() == 'GET':
                resp = session.get(url, timeout=timeout)
            elif method.upper() == 'POST':
                resp = session.post(url, json=json_data, timeout=timeout)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            duration = time.time() - start_time

            # Classify response for retry decision
            should_retry, reason = classify_error_for_retry(response=resp)

            if not should_retry:
                # Final outcome - success or permanent failure
                if resp.status_code in (200, 201, 202):
                    api_circuit_breaker.record_success()
                    # Success logging removed to reduce console noise
                    return resp
                else:
                    api_circuit_breaker.record_failure()
                    print(f"[API] ✗ {operation}{context} failed ({reason}) in {duration:.2f}s")
                    return resp

            # Should retry - calculate backoff and wait
            if attempt < max_retries:
                delay = calculate_backoff_delay(attempt)
                print(f"[API] {operation}{context} {reason}, retrying in {delay:.2f}s (attempt {attempt + 1}/{max_retries + 1})")
                time.sleep(delay)
            else:
                # All retries exhausted
                api_circuit_breaker.record_failure()
                print(f"[API] {operation}{context} failed permanently after {max_retries + 1} attempts")
                return resp

        except Exception:
            # Network or other exceptions - safe handling without keeping references
            should_retry, reason = classify_error_for_retry(exception=sys.exc_info()[1])

            if should_retry and attempt < max_retries:
                delay = calculate_backoff_delay(attempt)
                print(f"[API] {operation}{context} {reason}, retrying in {delay:.2f}s (attempt {attempt + 1}/{max_retries + 1})")
                time.sleep(delay)
            else:
                api_circuit_breaker.record_failure()
                safe_log_exception("api_client", operation)
                exception_storage.add_exception("api_client", operation)
                return None

    return None

# Public API functions using unified resilient client

def report_devices(room_hash_value: str):
    """Report devices with full network resilience"""
    url = f"{API_BASE_URL}/api/v1/report-devices"
    devices = list_adb_devices()
    payload = {
        "room_hash": room_hash_value,
        "devices": devices,
    }

    resp = api_request_with_resilience('POST', url, 'report_devices', json_data=payload)
    return resp is not None and resp.status_code in (200, 201)

def fetch_commands(room_hash_value: str) -> List[Dict[str, object]]:
    """Fetch commands with full network resilience (supports long polling)"""
    url = f"{API_BASE_URL}/api/v1/subscribe/{room_hash_value}"

    resp = api_request_with_resilience('GET', url, 'fetch_commands')
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            return data.get("commands") or []
        except ValueError as e:
            print(f"[API] Failed to parse fetch_commands response as JSON: {e}")
            return []
    return []

def report_command_result(payload: dict):
    """Report command result with full network resilience"""
    url = f"{API_BASE_URL}/api/v1/report-result"
    serial = payload.get('serial', 'unknown')

    resp = api_request_with_resilience('POST', url, 'report_result',
                                     json_data=payload, serial_context=serial)
    return resp is not None and resp.status_code in (200, 201)
