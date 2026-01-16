# android_agent/health.py
# Comprehensive system health monitoring for the Android Agent

import threading
import time
import psutil
from typing import Dict, Deque
from .config import MAX_MEMORY_MB, MAX_THREADS, MEMORY_THRESHOLD_PCT, CPU_THRESHOLD_PCT, HEALTH_CHECK_INTERVAL

class SystemHealthState:
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"

class SystemHealthMonitor:
    def __init__(self):
        self.state = SystemHealthState.HEALTHY
        self.metrics_history = []
        self.alert_count = 0
        self.last_alert_time = 0

        self.thresholds = {
            'max_threads': MAX_THREADS,
            'max_memory_mb': MAX_MEMORY_MB,
            'memory_threshold_pct': MEMORY_THRESHOLD_PCT,
            'cpu_threshold_pct': CPU_THRESHOLD_PCT,
            'queue_critical_pct': 95.0,
            'max_zombie_procs': 5,
        }

    def collect_metrics(self) -> Dict[str, float]:
        """Collect comprehensive system metrics using psutil"""
        process = psutil.Process()
        system = psutil.virtual_memory()

        metrics = {
            'timestamp': time.time(),
            'thread_count': len(threading.enumerate()),
            'memory_rss_mb': process.memory_info().rss / 1024 / 1024,
            'memory_vms_mb': process.memory_info().vms / 1024 / 1024,
            'cpu_percent': process.cpu_percent(interval=0.1),
            'open_files': len(process.open_files()),
            'connections': len(process.connections()),
            'system_memory_total_mb': system.total / 1024 / 1024,
            'system_memory_available_mb': system.available / 1024 / 1024,
            'system_memory_percent': system.percent,
        }

        # Keep last 10 measurements for trend analysis
        self.metrics_history.append(metrics)
        if len(self.metrics_history) > 10:
            self.metrics_history.pop(0)

        return metrics

    def evaluate_health(self, metrics: Dict, queue_utilization: float, active_processes: int) -> str:
        """Evaluate overall system health based on metrics and thresholds"""

        # EMERGENCY: Immediate action required
        if (metrics['thread_count'] > self.thresholds['max_threads'] or
            metrics['memory_rss_mb'] > self.thresholds['max_memory_mb'] or
            metrics['system_memory_percent'] > self.thresholds['memory_threshold_pct'] or
            queue_utilization > self.thresholds['queue_critical_pct']):
            return SystemHealthState.EMERGENCY

        # CRITICAL: Performance degraded significantly
        if (metrics['cpu_percent'] > self.thresholds['cpu_threshold_pct'] or
            active_processes > self.thresholds['max_zombie_procs']):
            return SystemHealthState.CRITICAL

        # WARNING: Monitor closely
        if (metrics['thread_count'] > self.thresholds['max_threads'] * 0.7 or
            metrics['memory_rss_mb'] > self.thresholds['max_memory_mb'] * 0.7 or
            queue_utilization > 50.0):
            return SystemHealthState.WARNING

        return SystemHealthState.HEALTHY

    def generate_alert(self, new_state: str, metrics: Dict, queue_info: Dict, process_info: Dict) -> None:
        """Generate contextual alerts with rate limiting"""
        current_time = time.time()

        # Rate limiting: Max 1 alert per 30 seconds to prevent spam
        if current_time - self.last_alert_time < 30:
            return

        self.last_alert_time = current_time
        self.alert_count += 1

        # Build contextual alert message
        alert_msg = f"[HEALTH] State: {new_state.upper()} | "
        alert_msg += f"Threads: {metrics['thread_count']} | "
        alert_msg += f"Memory: {metrics['memory_rss_mb']:.1f}MB | "
        alert_msg += f"CPU: {metrics['cpu_percent']:.1f}% | "
        alert_msg += f"System RAM: {metrics['system_memory_percent']:.1f}% | "
        alert_msg += f"Queue: {queue_info['utilization']:.1f}% | "
        alert_msg += f"Processes: {process_info['active']}"

        # State-specific messaging and actions
        if new_state == SystemHealthState.EMERGENCY:
            print(f"🚨 EMERGENCY: {alert_msg}")
            print("   ⚠️  Immediate action required - system at risk of crash!")
        elif new_state == SystemHealthState.CRITICAL:
            print(f"🔴 CRITICAL: {alert_msg}")
            print("   ⚠️  System performance degraded significantly")
        elif new_state == SystemHealthState.WARNING:
            print(f"🟡 WARNING: {alert_msg}")
            print("   ℹ️  Monitor closely - potential issues ahead")
        else:
            # Only log healthy state occasionally to reduce noise
            if self.alert_count % 10 == 0:
                print(f"🟢 HEALTHY: {alert_msg}")

def perform_deep_health_check() -> Dict[str, any]:
    """Perform deep health check for diagnostics"""
    import sys

    result = {
        'agent_info': {
            'active_threads': len(threading.enumerate()),
            'python_version': f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            'platform': sys.platform,
        }
    }

    try:
        process = psutil.Process()
        system = psutil.virtual_memory()
        cpu_freq = psutil.cpu_freq()

        result.update({
            'process_info': {
                'pid': process.pid,
                'threads': process.num_threads(),
                'open_files': len(process.open_files()),
                'connections': len(process.connections()),
                'cpu_times': process.cpu_times(),
            },
            'system_info': {
                'memory_total': system.total / 1024 / 1024,
                'memory_available': system.available / 1024 / 1024,
                'memory_percent': system.percent,
                'cpu_count': psutil.cpu_count(),
                'cpu_freq_current': cpu_freq.current if cpu_freq else 0,
            }
        })
    except Exception as e:
        result['error'] = str(e)

    return result

def export_health_report(health_monitor: SystemHealthMonitor, filename: str = None) -> str:
    """Export comprehensive health report for post-mortem analysis"""
    if not filename:
        timestamp = int(time.time())
        filename = f"health_report_{timestamp}.json"

    report = {
        'timestamp': time.time(),
        'current_state': health_monitor.state,
        'alert_count': health_monitor.alert_count,
        'metrics_history': health_monitor.metrics_history[-10:],  # Last 10 measurements
        'thresholds': health_monitor.thresholds,
        'deep_check': perform_deep_health_check(),
    }

    import json
    try:
        with open(filename, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        print(f"[HEALTH] Report exported to: {filename}")
        return filename
    except Exception as e:
        print(f"[HEALTH] Failed to export report: {e}")
        return None

def start_comprehensive_health_monitor(
    stop_signal: threading.Event,
    game_sessions: Dict[str, Dict[str, object]],
    game_sessions_lock: threading.Lock,
    commands: Deque[Dict[str, object]],
    commands_lock: threading.Lock,
    interval: float = HEALTH_CHECK_INTERVAL
):
    """Enhanced health monitor with comprehensive system monitoring"""
    health_monitor = SystemHealthMonitor()

    def monitor_loop():
        report_counter = 0

        while not stop_signal.is_set():
            try:
                # Collect comprehensive metrics
                metrics = health_monitor.collect_metrics()

                # Get queue information
                with commands_lock:
                    q_len = len(commands)
                    q_max = commands.maxlen or MAX_COMMANDS_QUEUE_SIZE
                    queue_utilization = (q_len / q_max * 100) if q_max > 0 else 0

                # Get process information
                with game_sessions_lock:
                    active_processes = sum(
                        1 for sess in game_sessions.values()
                        for proc in [sess.get("process")]
                        if proc and proc.poll() is None
                    )

                # Evaluate overall health
                new_state = health_monitor.evaluate_health(
                    metrics, queue_utilization, active_processes
                )

                # State transition detection and alerting
                if new_state != health_monitor.state:
                    queue_info = {
                        'current': q_len,
                        'max': q_max,
                        'utilization': queue_utilization
                    }
                    process_info = {'active': active_processes}

                    health_monitor.generate_alert(
                        new_state, metrics, queue_info, process_info
                    )

                    health_monitor.state = new_state

                # Periodic detailed status reporting (every 5 intervals)
                report_counter += 1
                if report_counter % 5 == 0:
                    status_msg = f"[STATUS] Threads: {metrics['thread_count']} | "
                    status_msg += f"Memory: {metrics['memory_rss_mb']:.1f}MB | "
                    status_msg += f"CPU: {metrics['cpu_percent']:.1f}% | "
                    status_msg += f"System RAM: {metrics['system_memory_percent']:.1f}% | "
                    status_msg += f"Queue: {q_len}/{q_max} ({queue_utilization:.1f}%) | "
                    status_msg += f"Processes: {active_processes}"

                    print(status_msg)

                stop_signal.wait(interval)

            except Exception as e:
                print(f"[HEALTH] Monitor error: {e}")
                stop_signal.wait(interval)

    threading.Thread(target=monitor_loop, daemon=True).start()
    return health_monitor
