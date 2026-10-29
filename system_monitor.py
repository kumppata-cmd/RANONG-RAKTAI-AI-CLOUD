"""System Monitor — ตรวจสอบสุขภาพระบบและแจ้งเตือน

ตรวจ:
1. GCS connectivity
2. Document processing rate
3. Dead-letter queue depth
4. Memory usage

แจ้งเตือนทาง LINE push ไปยัง physician recipients
ไม่เก็บหรือแสดงข้อมูลผู้ป่วย (PDPA compliant)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Thresholds
DLQ_WARN_THRESHOLD = 5
DLQ_CRITICAL_THRESHOLD = 10
MEMORY_WARN_MB = 400
MEMORY_CRITICAL_MB = 600
STALE_HOURS = 48  # ไม่มีเอกสารใหม่ > 48 ชม. = warning

STATUS_HEALTHY = "healthy"
STATUS_WARNING = "warning"
STATUS_CRITICAL = "critical"


class SystemMonitor:
    """ตรวจสอบสุขภาพระบบ RANONG-RAKTAI-AI."""

    def __init__(self, storage, doc_index, dlq, alert_recipients):
        """
        Args:
            storage: GCS storage object
            doc_index: DocumentIndex instance
            dlq: DeadLetterQueue instance (or None)
            alert_recipients: dict of role → set of LINE user IDs
        """
        self._storage = storage
        self._doc_index = doc_index
        self._dlq = dlq
        self._alert_recipients = alert_recipients or {}
        self._last_check_time: float = 0
        self._last_alert_time: float = 0
        self._alert_cooldown: float = 3600  # 1 hour between alerts

    def check_health(self) -> dict:
        """ตรวจสุขภาพระบบทั้งหมด."""
        gcs = self._check_gcs()
        processing = self._check_processing()
        dlq = self._check_dlq()
        memory = self._check_memory()

        # Determine overall status
        statuses = [
            gcs.get("status", STATUS_HEALTHY),
            processing.get("status", STATUS_HEALTHY),
            dlq.get("status", STATUS_HEALTHY),
            memory.get("status", STATUS_HEALTHY),
        ]

        if STATUS_CRITICAL in statuses:
            overall = STATUS_CRITICAL
        elif STATUS_WARNING in statuses:
            overall = STATUS_WARNING
        else:
            overall = STATUS_HEALTHY

        self._last_check_time = time.monotonic()

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "overall": overall,
            "gcs": gcs,
            "processing": processing,
            "dead_letter": dlq,
            "memory": memory,
        }

    def _check_gcs(self) -> dict:
        """ตรวจ GCS connectivity."""
        result = {"status": STATUS_HEALTHY, "detail": ""}

        if not self._storage:
            result["status"] = STATUS_CRITICAL
            result["detail"] = "storage ไม่ได้ตั้งค่า"
            return result

        is_ready = getattr(self._storage, "is_ready", False)
        if not is_ready:
            result["status"] = STATUS_CRITICAL
            result["detail"] = "GCS ไม่พร้อม"
            return result

        # Try a quick read
        try:
            bucket = getattr(self._storage, "_bucket", None)
            if bucket:
                # List 1 blob to check connectivity
                blobs = list(bucket.list_blobs(max_results=1))
                result["detail"] = "เชื่อมต่อปกติ"
            else:
                result["status"] = STATUS_WARNING
                result["detail"] = "ไม่มี bucket object"
        except Exception as e:
            result["status"] = STATUS_CRITICAL
            result["detail"] = f"เชื่อมต่อไม่ได้: {type(e).__name__}"

        return result

    def _check_processing(self) -> dict:
        """ตรวจ document processing rate."""
        result = {
            "status": STATUS_HEALTHY,
            "detail": "",
            "doc_count": 0,
        }

        if not self._doc_index:
            result["detail"] = "doc_index ไม่พร้อม"
            return result

        try:
            docs = getattr(self._doc_index, "_docs", [])
            result["doc_count"] = len(docs)

            if not docs:
                result["detail"] = "ยังไม่มีเอกสาร"
                return result

            # Find latest upload date
            latest_date = ""
            for d in docs:
                ud = d.get("upload_date", "")
                if ud > latest_date:
                    latest_date = ud

            result["latest_upload"] = latest_date

            if latest_date:
                try:
                    from datetime import datetime as dt

                    last_dt = dt.fromisoformat(latest_date)
                    now = dt.now(timezone.utc)
                    if last_dt.tzinfo is None:
                        from zoneinfo import ZoneInfo

                        last_dt = last_dt.replace(
                            tzinfo=ZoneInfo("Asia/Bangkok")
                        )
                    hours_ago = (
                        now - last_dt
                    ).total_seconds() / 3600
                    result["hours_since_last"] = round(hours_ago, 1)

                    if hours_ago > STALE_HOURS:
                        result["status"] = STATUS_WARNING
                        result["detail"] = (
                            f"ไม่มีเอกสารใหม่ {int(hours_ago)} ชม."
                        )
                    else:
                        result["detail"] = "ปกติ"
                except Exception:
                    result["detail"] = f"ล่าสุด: {latest_date}"
            else:
                result["detail"] = "ไม่มีวันที่"

        except Exception as e:
            result["status"] = STATUS_WARNING
            result["detail"] = f"ตรวจไม่ได้: {type(e).__name__}"

        return result

    def _check_dlq(self) -> dict:
        """ตรวจ dead-letter queue depth."""
        result = {
            "status": STATUS_HEALTHY,
            "detail": "",
            "pending": 0,
            "total": 0,
        }

        if not self._dlq:
            result["detail"] = "DLQ ไม่ได้เปิดใช้"
            return result

        try:
            stats = self._dlq.get_stats()
            pending = stats.get("pending", 0) + stats.get(
                "retrying", 0
            )
            result["pending"] = pending
            result["total"] = stats.get("total", 0)

            if pending >= DLQ_CRITICAL_THRESHOLD:
                result["status"] = STATUS_CRITICAL
                result["detail"] = (
                    f"{pending} เอกสารรอตรวจสอบ (วิกฤต)"
                )
            elif pending >= DLQ_WARN_THRESHOLD:
                result["status"] = STATUS_WARNING
                result["detail"] = (
                    f"{pending} เอกสารรอตรวจสอบ"
                )
            elif pending > 0:
                result["detail"] = f"{pending} เอกสารรอตรวจสอบ"
            else:
                result["detail"] = "ไม่มีเอกสารค้าง"
        except Exception as e:
            result["status"] = STATUS_WARNING
            result["detail"] = f"ตรวจไม่ได้: {type(e).__name__}"

        return result

    def _check_memory(self) -> dict:
        """ตรวจ memory usage."""
        result = {
            "status": STATUS_HEALTHY,
            "detail": "",
            "memory_mb": 0,
        }

        try:
            import psutil

            proc = psutil.Process()
            mem_mb = proc.memory_info().rss / 1024 / 1024
            result["memory_mb"] = round(mem_mb, 1)

            if mem_mb >= MEMORY_CRITICAL_MB:
                result["status"] = STATUS_CRITICAL
                result["detail"] = (
                    f"ใช้ memory {mem_mb:.0f} MB (วิกฤต)"
                )
            elif mem_mb >= MEMORY_WARN_MB:
                result["status"] = STATUS_WARNING
                result["detail"] = (
                    f"ใช้ memory {mem_mb:.0f} MB (สูง)"
                )
            else:
                result["detail"] = f"{mem_mb:.0f} MB"
        except ImportError:
            result["detail"] = "psutil ไม่พร้อม"
        except Exception as e:
            result["detail"] = f"ตรวจไม่ได้: {type(e).__name__}"

        return result

    def should_alert(self, health: dict) -> bool:
        """ตรวจว่าต้องแจ้งเตือนไหม."""
        if health.get("overall") not in (
            STATUS_WARNING,
            STATUS_CRITICAL,
        ):
            return False

        # Cooldown — ไม่แจ้งถี่กว่า 1 ชม.
        now = time.monotonic()
        if now - self._last_alert_time < self._alert_cooldown:
            return False

        return True

    def format_alert(self, health: dict) -> str:
        """สร้างข้อความแจ้งเตือนภาษาไทย."""
        overall = health.get("overall", STATUS_HEALTHY)

        emoji = {"warning": "⚠️", "critical": "🔴"}.get(
            overall, "ℹ️"
        )

        lines = [
            f"{emoji} RANONG RAKTAI AI — แจ้งเตือนระบบ",
            f"สถานะ: {overall.upper()}",
            "",
        ]

        for section in ("gcs", "processing", "dead_letter", "memory"):
            data = health.get(section, {})
            status = data.get("status", STATUS_HEALTHY)
            detail = data.get("detail", "")
            if status != STATUS_HEALTHY:
                icon = "🔴" if status == STATUS_CRITICAL else "⚠️"
                name = {
                    "gcs": "GCS Storage",
                    "processing": "เอกสาร",
                    "dead_letter": "Dead-Letter",
                    "memory": "Memory",
                }.get(section, section)
                lines.append(f"{icon} {name}: {detail}")

        lines.append("")
        lines.append(
            f"เวลา: "
            f"{health.get('timestamp', '')[:19].replace('T', ' ')}"
        )

        return "\n".join(lines)

    def send_alert_if_needed(
        self, health: dict, push_fn=None
    ) -> bool:
        """แจ้งเตือนทาง LINE ถ้าจำเป็น.

        Args:
            health: ผลจาก check_health()
            push_fn: function(user_id, message) สำหรับส่ง LINE push

        Returns: True ถ้าส่งแจ้งเตือนแล้ว
        """
        if not self.should_alert(health):
            return False
        if not push_fn:
            return False

        msg = self.format_alert(health)
        physician_ids = list(
            self._alert_recipients.get("physician", [])
        )

        sent = 0
        for uid in physician_ids:
            try:
                push_fn(uid, msg)
                sent += 1
            except Exception:
                pass

        if sent:
            self._last_alert_time = time.monotonic()
            logger.info(
                "System alert sent to %d recipients", sent
            )
            return True

        return False
