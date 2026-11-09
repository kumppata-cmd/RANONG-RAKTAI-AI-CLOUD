"""HD Smart Monitoring — Self-Monitoring & Error Tracking

ระบบ monitoring ภายใน:
1. Health self-ping ทุก 5 นาที (ไม่ให้ Render free tier sleep)
2. Error counter — นับ error ต่อ endpoint
3. Response time tracking
4. LINE push alert เมื่อ error rate สูง
"""

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ErrorTracker:
    """Track errors per endpoint — in-memory, resets on restart."""

    def __init__(self):
        self._errors = defaultdict(list)   # endpoint → [timestamp, ...]
        self._requests = defaultdict(int)  # endpoint → count
        self._lock = threading.Lock()

    def record_request(self, endpoint: str):
        with self._lock:
            self._requests[endpoint] += 1

    def record_error(self, endpoint: str, error_msg: str):
        with self._lock:
            self._errors[endpoint].append({
                "time": datetime.now(timezone.utc).isoformat(),
                "error": error_msg[:200],
            })
            # Keep only last 100 errors per endpoint
            if len(self._errors[endpoint]) > 100:
                self._errors[endpoint] = self._errors[endpoint][-100:]

    def get_error_rate(self, endpoint: str, window_minutes: int = 15) -> float:
        """Error rate ใน window ที่กำหนด"""
        with self._lock:
            total = self._requests.get(endpoint, 0)
            if total == 0:
                return 0.0
            cutoff = time.time() - (window_minutes * 60)
            recent_errors = sum(
                1 for e in self._errors.get(endpoint, [])
                if e["time"] > datetime.fromtimestamp(
                    cutoff, tz=timezone.utc
                ).isoformat()
            )
            return recent_errors / total

    def get_summary(self) -> dict:
        """สรุป error tracker"""
        with self._lock:
            summary = {}
            for ep in set(list(self._requests.keys()) + list(self._errors.keys())):
                err_count = len(self._errors.get(ep, []))
                req_count = self._requests.get(ep, 0)
                summary[ep] = {
                    "requests": req_count,
                    "errors": err_count,
                    "error_rate": f"{(err_count/req_count*100):.1f}%" if req_count else "0%",
                    "last_error": self._errors[ep][-1] if self._errors[ep] else None,
                }
            return summary

    def get_recent_errors(self, limit: int = 20) -> list:
        """ดึง error ล่าสุดทุก endpoint"""
        with self._lock:
            all_errors = []
            for ep, errors in self._errors.items():
                for e in errors[-limit:]:
                    all_errors.append({**e, "endpoint": ep})
            all_errors.sort(key=lambda x: x["time"], reverse=True)
            return all_errors[:limit]


# Singleton
error_tracker = ErrorTracker()


# ── Self-Ping (prevent Render free tier sleep) ─────────────

_ping_thread = None


def start_self_ping(app_url: str, interval: int = 300):
    """Start background thread that pings health endpoint every N seconds.

    Prevents Render free tier from sleeping after 15 min inactivity.
    Uses /health endpoint which is lightweight.
    """
    global _ping_thread
    if _ping_thread and _ping_thread.is_alive():
        return  # Already running

    def _ping_loop():
        import urllib.request
        health_url = f"{app_url.rstrip('/')}/health"
        logger.info("Self-ping started: %s every %ds", health_url, interval)
        while True:
            try:
                time.sleep(interval)
                req = urllib.request.Request(health_url, method="GET")
                req.add_header("User-Agent", "HD-SelfPing/1.0")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status = resp.status
                    if status != 200:
                        logger.warning("Self-ping got status %d", status)
            except Exception as e:
                logger.warning("Self-ping failed: %s", type(e).__name__)

    _ping_thread = threading.Thread(target=_ping_loop, daemon=True)
    _ping_thread.start()


# ── Uptime Tracking ────────────────────────────────────────

_boot_time = datetime.now(timezone.utc)


def get_uptime() -> dict:
    """ระยะเวลาที่ server ทำงาน"""
    now = datetime.now(timezone.utc)
    delta = now - _boot_time
    hours = delta.total_seconds() / 3600
    return {
        "boot_time": _boot_time.isoformat(),
        "uptime_hours": round(hours, 1),
        "uptime_human": f"{int(hours)}h {int(delta.total_seconds() % 3600 / 60)}m",
    }
