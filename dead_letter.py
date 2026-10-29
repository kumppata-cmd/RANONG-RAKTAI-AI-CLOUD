"""Dead Letter Queue — เก็บเอกสารที่ classify/process ไม่สำเร็จ

เอกสารที่ตก dead-letter:
1. OCR ล้มเหลว (ภาพเสีย, PDF เข้ารหัส)
2. Classification ไม่ชัดเจน (ไปอยู่ "review")
3. HN extraction ล้มเหลว (ไม่พบ HN ในเอกสาร)
4. GCS upload ล้มเหลว

แต่ละรายการเก็บ:
- doc_id, timestamp, error_type, error_message
- retry_count (สูงสุด 3 ครั้ง)
- status: pending / retrying / resolved / abandoned
- ไม่เก็บเนื้อหาเอกสาร (PDPA)

Storage: GCS path _dead_letter/{doc_id}.json
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DLQ_PREFIX = "_dead_letter/"
MAX_RETRIES = 3

# Error types
ERROR_OCR_FAILED = "ocr_failed"
ERROR_CLASSIFY_REVIEW = "classify_review"
ERROR_HN_NOT_FOUND = "hn_not_found"
ERROR_UPLOAD_FAILED = "upload_failed"
ERROR_VALIDATION_FAILED = "validation_failed"

# Statuses
STATUS_PENDING = "pending"
STATUS_RETRYING = "retrying"
STATUS_RESOLVED = "resolved"
STATUS_ABANDONED = "abandoned"


class DeadLetterQueue:
    """Dead-letter queue backed by GCS JSON files.

    แต่ละ entry เก็บเป็น JSON ที่ _dead_letter/{doc_id}.json
    ไม่เก็บเนื้อหาเอกสาร หรือข้อมูลผู้ป่วย (PDPA compliant)
    """

    def __init__(self, storage):
        self._storage = storage
        self._cache: list[dict] = []
        self._cache_ts: float = 0
        self._cache_ttl: float = 30  # seconds

    # ── Write ────────────────────────────────────────────────

    def add(
        self,
        doc_id: str,
        error_type: str,
        error_message: str,
        category: str = "",
    ) -> bool:
        """เพิ่มเอกสารเข้า dead-letter queue.

        Args:
            doc_id: รหัสเอกสาร (ไม่ใช่ข้อมูลผู้ป่วย)
            error_type: ประเภทข้อผิดพลาด (ERROR_* constants)
            error_message: ข้อความ error (ห้ามมี PHI)
            category: ประเภทเอกสาร (ถ้าทราบ)

        Returns: True ถ้าเพิ่มสำเร็จ
        """
        if not doc_id or not self._storage:
            return False

        entry = {
            "doc_id": doc_id[:64],
            "error_type": error_type[:32],
            "error_message": _sanitize_message(error_message),
            "category": category[:16],
            "status": STATUS_PENDING,
            "retry_count": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "resolved_at": "",
            "resolution": "",
        }

        try:
            path = f"{DLQ_PREFIX}{doc_id[:64]}.json"
            data = json.dumps(entry, ensure_ascii=False).encode("utf-8")
            self._storage.put(path, data)
            self._cache_ts = 0  # invalidate cache
            logger.info(
                "DLQ add: type=%s, status=pending",
                error_type,
            )
            return True
        except Exception as e:
            logger.warning("DLQ add failed: %s", type(e).__name__)
            return False

    def mark_resolved(
        self, doc_id: str, resolution: str = "manual"
    ) -> bool:
        """ทำเครื่องหมายว่าแก้ไขแล้ว."""
        entry = self.get(doc_id)
        if not entry:
            return False

        entry["status"] = STATUS_RESOLVED
        entry["resolution"] = resolution[:64]
        entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        return self._save(entry)

    def mark_retrying(self, doc_id: str) -> bool:
        """ทำเครื่องหมายว่ากำลัง retry."""
        entry = self.get(doc_id)
        if not entry:
            return False

        entry["status"] = STATUS_RETRYING
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        return self._save(entry)

    def increment_retry(self, doc_id: str) -> int:
        """เพิ่ม retry count — ถ้าเกิน MAX_RETRIES จะ abandon.

        Returns: retry count ปัจจุบัน (-1 ถ้าล้มเหลว)
        """
        entry = self.get(doc_id)
        if not entry:
            return -1

        entry["retry_count"] = entry.get("retry_count", 0) + 1
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()

        if entry["retry_count"] >= MAX_RETRIES:
            entry["status"] = STATUS_ABANDONED
            entry["resolution"] = f"max_retries_{MAX_RETRIES}"
        else:
            entry["status"] = STATUS_PENDING

        self._save(entry)
        return entry["retry_count"]

    # ── Read ─────────────────────────────────────────────────

    def get(self, doc_id: str) -> dict | None:
        """ดึงข้อมูล dead-letter entry."""
        if not doc_id or not self._storage:
            return None

        try:
            path = f"{DLQ_PREFIX}{doc_id[:64]}.json"
            data = self._storage.get(path)
            if data:
                return json.loads(data)
        except Exception as e:
            logger.warning("DLQ get failed: %s", type(e).__name__)
        return None

    def list_pending(self, limit: int = 50) -> list[dict]:
        """รายการที่รอ retry/review."""
        all_entries = self._list_all()
        pending = [
            e for e in all_entries
            if e.get("status") in (STATUS_PENDING, STATUS_RETRYING)
        ]
        # เรียงตามวันที่ (ใหม่ก่อน)
        pending.sort(
            key=lambda x: x.get("created_at", ""), reverse=True
        )
        return pending[:limit]

    def list_all(self, limit: int = 100) -> list[dict]:
        """รายการทั้งหมด."""
        entries = self._list_all()
        entries.sort(
            key=lambda x: x.get("created_at", ""), reverse=True
        )
        return entries[:limit]

    def get_stats(self) -> dict:
        """สถิติ: pending, retrying, resolved, abandoned."""
        entries = self._list_all()
        stats = {
            "total": len(entries),
            "pending": 0,
            "retrying": 0,
            "resolved": 0,
            "abandoned": 0,
        }
        for e in entries:
            s = e.get("status", "")
            if s in stats:
                stats[s] += 1
        return stats

    # ── Internal ─────────────────────────────────────────────

    def _save(self, entry: dict) -> bool:
        """บันทึก entry ลง GCS."""
        try:
            doc_id = entry.get("doc_id", "")
            if not doc_id:
                return False
            path = f"{DLQ_PREFIX}{doc_id[:64]}.json"
            data = json.dumps(entry, ensure_ascii=False).encode("utf-8")
            self._storage.put(path, data)
            self._cache_ts = 0  # invalidate
            return True
        except Exception as e:
            logger.warning("DLQ save failed: %s", type(e).__name__)
            return False

    def _list_all(self) -> list[dict]:
        """ดึงรายการทั้งหมดจาก GCS (cached)."""
        now = time.monotonic()
        if now - self._cache_ts < self._cache_ttl and self._cache:
            return self._cache

        entries = []
        if not self._storage:
            return entries

        try:
            # List blobs under _dead_letter/
            bucket = getattr(self._storage, "_bucket", None)
            if not bucket:
                return entries

            blobs = bucket.list_blobs(
                prefix=DLQ_PREFIX, max_results=200
            )
            for blob in blobs:
                if not blob.name.endswith(".json"):
                    continue
                try:
                    data = blob.download_as_bytes()
                    entry = json.loads(data)
                    entries.append(entry)
                except Exception:
                    pass

            self._cache = entries
            self._cache_ts = now
        except Exception as e:
            logger.warning(
                "DLQ list failed: %s", type(e).__name__
            )

        return entries


def _sanitize_message(msg: str) -> str:
    """ลบข้อมูลที่อาจเป็น PHI ออกจาก error message."""
    if not msg:
        return ""
    # ตัดให้ไม่เกิน 200 ตัวอักษร
    s = msg[:200]
    # ลบ path ที่อาจมีชื่อคนไข้
    if "/" in s:
        parts = s.split("/")
        # เก็บเฉพาะส่วนแรกและส่วนสุดท้าย
        if len(parts) > 2:
            s = f"{parts[0]}/.../{parts[-1]}"
    return s
