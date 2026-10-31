"""Structured Event Logger — V4.0 Phase 1.2

ทุก request ได้ job_id + idempotency_key
ทุก event ถูกบันทึกเป็น structured JSON

ไม่ log ข้อมูลผู้ป่วย (PDPA compliant)

Usage:
    from event_logger import EventLogger, generate_job_id

    logger = EventLogger()
    job_id = generate_job_id()
    logger.log("document_received", job_id=job_id, data={...})
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ─── Job ID / Idempotency Key ─────────────────────────────


def generate_job_id() -> str:
    """สร้าง unique job ID สำหรับทุก request.

    Format: JOB-{timestamp_hex}-{random_4chars}
    ตัวอย่าง: JOB-1935ab7c-k2m9
    """
    ts_hex = hex(int(time.time()))[2:]
    rand = uuid.uuid4().hex[:4]
    return f"JOB-{ts_hex}-{rand}"


def generate_idempotency_key(
    user_hash: str, content_hash: str
) -> str:
    """สร้าง idempotency key จาก user + content.

    ใช้ตรวจว่าเคยประมวลผล request นี้แล้วหรือยัง
    """
    return f"idem:{user_hash[:8]}:{content_hash[:16]}"


# ─── Event Types ──────────────────────────────────────────

EVENT_TYPES = {
    # Document lifecycle
    "document_received",
    "document_classified",
    "document_stored",
    "document_indexed",
    "document_duplicate",
    "document_rejected",

    # Lab processing
    "lab_extracted",
    "lab_alert_triggered",
    "lab_approved",

    # Google sync
    "drive_backup_success",
    "drive_backup_failed",
    "sheets_sync_success",
    "sheets_sync_failed",
    "email_sent",
    "email_failed",

    # User actions
    "user_command",
    "user_registration",
    "admin_action",

    # System
    "system_startup",
    "system_error",
    "rate_limited",
    "dedup_blocked",
}


# ─── Structured Event ────────────────────────────────────


class StructuredEvent:
    """เหตุการณ์หนึ่ง event ที่มีข้อมูลครบถ้วน."""

    __slots__ = (
        "event_id", "event_type", "job_id", "timestamp",
        "data", "duration_ms", "success",
    )

    def __init__(
        self,
        event_type: str,
        job_id: str = "",
        data: dict | None = None,
        duration_ms: int | None = None,
        success: bool = True,
    ):
        self.event_id = uuid.uuid4().hex[:12]
        self.event_type = event_type
        self.job_id = job_id
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.data = data or {}
        self.duration_ms = duration_ms
        self.success = success

    def to_dict(self) -> dict:
        """แปลงเป็น dict สำหรับ JSON serialization."""
        d: dict = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "success": self.success,
        }
        if self.job_id:
            d["job_id"] = self.job_id
        if self.data:
            d["data"] = self.data
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        return d

    def to_json(self) -> str:
        """แปลงเป็น JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ─── Event Logger ────────────────────────────────────────


class EventLogger:
    """Structured Event Logger — บันทึก events แบบ structured JSON.

    รองรับ:
    - In-memory buffer (ดูย้อนหลัง)
    - GCS persistence (ถ้าเปิดใช้)
    - Structured JSON format ทุก event
    """

    def __init__(
        self,
        max_buffer: int = 1000,
        gcs_storage=None,
        gcs_prefix: str = "_events/",
    ):
        self._buffer: list[StructuredEvent] = []
        self._max_buffer = max_buffer
        self._storage = gcs_storage
        self._gcs_prefix = gcs_prefix
        self._event_count = 0

    def set_storage(self, storage) -> None:
        """ตั้ง GCS storage สำหรับ persistence."""
        self._storage = storage

    def log(
        self,
        event_type: str,
        job_id: str = "",
        data: dict | None = None,
        duration_ms: int | None = None,
        success: bool = True,
    ) -> StructuredEvent:
        """บันทึก event ใหม่.

        Args:
            event_type: ประเภท event (ดู EVENT_TYPES)
            job_id: Job ID ที่เกี่ยวข้อง
            data: ข้อมูลเพิ่มเติม (ห้ามมี PHI)
            duration_ms: เวลาที่ใช้ (ms)
            success: สำเร็จหรือไม่

        Returns:
            StructuredEvent ที่สร้าง
        """
        event = StructuredEvent(
            event_type=event_type,
            job_id=job_id,
            data=data,
            duration_ms=duration_ms,
            success=success,
        )

        # เก็บใน buffer
        self._buffer.append(event)
        if len(self._buffer) > self._max_buffer:
            self._buffer = self._buffer[-self._max_buffer:]

        self._event_count += 1

        # Log to Python logger (structured)
        logger.info(
            "EVENT: %s | job=%s | success=%s | %s",
            event_type,
            job_id[:16] if job_id else "-",
            success,
            json.dumps(
                {k: v for k, v in (data or {}).items()
                 if k not in ("content", "text", "body")},
                ensure_ascii=False,
            )[:200],
        )

        # Persist to GCS (async-safe, fire-and-forget)
        self._persist_event(event)

        return event

    def _persist_event(self, event: StructuredEvent) -> None:
        """บันทึก event ลง GCS (ไม่ block main thread)."""
        if not self._storage:
            return
        try:
            # ใช้ event_id เป็นชื่อไฟล์ — ไม่ซ้ำกัน
            path = (
                f"{self._gcs_prefix}"
                f"{event.timestamp[:10]}/"
                f"{event.event_id}.json"
            )
            self._storage.put(
                path,
                event.to_json().encode("utf-8"),
                content_type="application/json",
            )
        except Exception as e:
            # Persistence ล้มเหลวไม่กระทบระบบหลัก
            logger.warning(
                "Event persist failed: %s (event=%s)",
                type(e).__name__, event.event_id,
            )

    def get_recent(
        self, limit: int = 50, event_type: str = ""
    ) -> list[dict]:
        """ดู events ล่าสุดจาก buffer.

        Args:
            limit: จำนวนสูงสุด
            event_type: กรองตามประเภท (ว่าง = ทั้งหมด)
        """
        events = self._buffer
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return [e.to_dict() for e in events[-limit:]]

    def get_stats(self) -> dict:
        """สถิติ events."""
        by_type: dict[str, int] = {}
        by_success: dict[str, int] = {"true": 0, "false": 0}
        for e in self._buffer:
            by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
            by_success["true" if e.success else "false"] += 1

        return {
            "total_events": self._event_count,
            "buffer_size": len(self._buffer),
            "max_buffer": self._max_buffer,
            "by_type": by_type,
            "success_rate": (
                by_success["true"] / max(1, len(self._buffer))
            ),
            "has_persistence": self._storage is not None,
        }


# ─── Timing Context Manager ─────────────────────────────


class EventTimer:
    """วัดเวลาแล้ว log event อัตโนมัติ.

    Usage:
        with EventTimer(event_logger, "lab_extracted", job_id=jid) as t:
            t.data["analyte_count"] = len(results)
            # ... do work ...
        # event ถูก log อัตโนมัติพร้อม duration_ms
    """

    def __init__(
        self,
        event_logger: EventLogger,
        event_type: str,
        job_id: str = "",
        data: dict | None = None,
    ):
        self._logger = event_logger
        self._event_type = event_type
        self._job_id = job_id
        self.data = data or {}
        self._start = 0.0
        self._success = True

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = int((time.monotonic() - self._start) * 1000)
        if exc_type is not None:
            self._success = False
            self.data["error"] = type(exc_val).__name__ if exc_val else ""

        self._logger.log(
            event_type=self._event_type,
            job_id=self._job_id,
            data=self.data,
            duration_ms=duration_ms,
            success=self._success,
        )
        return False  # ไม่ suppress exception
