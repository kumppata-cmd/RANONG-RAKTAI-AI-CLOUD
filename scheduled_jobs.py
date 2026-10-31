"""Scheduled Jobs Manager — V4.0 Phase 5.1

จัดการ scheduled jobs สำหรับ Render Cron Jobs
- HD reminders (แจ้งเตือนนัดฟอกเลือด)
- Weekly report (สรุปรายสัปดาห์)
- Daily backup check
- CVE audit (ตรวจ dependencies)

ออกแบบให้เรียกจาก Render Cron Jobs endpoint
ไม่เก็บข้อมูลผู้ป่วยใน job logs (PDPA)

Usage:
    # ใน server.py
    @app.route("/cron/<job_name>", methods=["POST"])
    def run_cron_job(job_name):
        return job_manager.run(job_name)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class JobResult:
    job_name: str
    success: bool
    message: str = ""
    duration_ms: int = 0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "job": self.job_name,
            "success": self.success,
            "message": self.message,
            "duration_ms": self.duration_ms,
            "details": self.details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class ScheduledJobManager:
    """จัดการ scheduled jobs ทั้งหมด."""

    def __init__(self):
        self._handlers: dict[str, callable] = {}
        self._history: list[dict] = []
        self._max_history = 100

    def register(self, job_name: str, handler: callable) -> None:
        self._handlers[job_name] = handler
        logger.info("Job registered: %s", job_name)

    def run(self, job_name: str, **kwargs) -> JobResult:
        """รัน job ตามชื่อ."""
        handler = self._handlers.get(job_name)
        if not handler:
            result = JobResult(
                job_name=job_name,
                success=False,
                message=f"ไม่พบ job: {job_name}",
            )
            self._history.append(result.to_dict())
            return result

        start = time.monotonic()
        try:
            result = handler(**kwargs)
            duration = int((time.monotonic() - start) * 1000)

            job_result = JobResult(
                job_name=job_name,
                success=True,
                message=result.get("message", "OK") if isinstance(result, dict) else str(result),
                duration_ms=duration,
                details=result if isinstance(result, dict) else {},
            )
        except Exception as e:
            duration = int((time.monotonic() - start) * 1000)
            job_result = JobResult(
                job_name=job_name,
                success=False,
                message=f"{type(e).__name__}: {str(e)[:100]}",
                duration_ms=duration,
            )

        self._history.append(job_result.to_dict())
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        logger.info(
            "Job %s: %s (%dms)",
            job_name,
            "OK" if job_result.success else "FAILED",
            job_result.duration_ms,
        )
        return job_result

    def get_jobs(self) -> list[dict]:
        """ดู jobs ที่ลงทะเบียนไว้."""
        return [
            {"name": name, "registered": True}
            for name in self._handlers.keys()
        ]

    def get_history(self, limit: int = 20) -> list[dict]:
        """ดูประวัติ job runs."""
        return self._history[-limit:]

    def get_stats(self) -> dict:
        """สถิติ jobs."""
        success = sum(1 for h in self._history if h.get("success"))
        failed = sum(1 for h in self._history if not h.get("success"))
        return {
            "registered_jobs": list(self._handlers.keys()),
            "total_runs": len(self._history),
            "success": success,
            "failed": failed,
        }


# ─── Built-in Job Functions ─────────────────────────────


def job_daily_backup_check(storage=None, backup_manager=None) -> dict:
    """ตรวจสอบ backup ประจำวัน."""
    if backup_manager:
        status = backup_manager.get_backup_status()
        channels = status.get("channels", {})
        ok = all(
            ch.get("status") in ("connected", "configured")
            for ch in channels.values()
        )
        return {
            "message": "Backup check OK" if ok else "Backup check WARNING",
            "channels": {
                name: ch.get("status")
                for name, ch in channels.items()
            },
        }
    return {"message": "No backup manager configured"}


def job_weekly_summary(
    doc_index=None, storage=None, gmail_report=None,
) -> dict:
    """สร้างและส่งสรุปรายสัปดาห์."""
    if not doc_index or not hasattr(doc_index, "_docs"):
        return {"message": "No doc_index available"}

    total = len(doc_index._docs)
    cats: dict[str, int] = {}
    for d in doc_index._docs:
        c = d.get("category", "other")
        cats[c] = cats.get(c, 0) + 1

    stats = {
        "total_docs": total,
        "by_category": cats,
        "week": datetime.now(timezone.utc).strftime("%Y-W%W"),
    }

    # ส่ง email ถ้าเปิดใช้
    email_sent = False
    if gmail_report and hasattr(gmail_report, "is_available"):
        try:
            if gmail_report.is_available():
                result = gmail_report.send_weekly_report(stats)
                email_sent = not result.get("error")
        except Exception:
            pass

    return {
        "message": f"Weekly summary: {total} documents",
        "stats": stats,
        "email_sent": email_sent,
    }


def job_sheets_sync(
    doc_index=None, storage=None, master_registry=None,
) -> dict:
    """Sync ข้อมูลไป Google Sheets."""
    results = {}
    try:
        import google_sheets_sync
        if google_sheets_sync.is_available():
            if master_registry:
                r1 = google_sheets_sync.sync_patient_registry(master_registry)
                results["registry"] = r1
            if doc_index and storage:
                r2 = google_sheets_sync.sync_lab_summary(doc_index, storage)
                results["labs"] = r2
            return {"message": "Sheets sync done", **results}
        return {"message": "Google Sheets not available"}
    except Exception as e:
        return {"message": f"Sheets sync failed: {type(e).__name__}"}
