"""Backup Manager — V4.0 Phase 2.4

ระบบ backup อัตโนมัติ พร้อม DR plan
- GCS Object Versioning สำหรับ point-in-time recovery
- Google Drive auto-backup ทุกเอกสารใหม่
- Windows GCS Sync (gcs_sync.py v2.2)
- Backup status tracking

RPO/RTO Targets:
- RPO: 5 นาที (GCS Sync interval)
- RTO: 10 นาที (Render restart + rollback)

Usage:
    from backup_manager import BackupManager
    bm = BackupManager(storage=storage)
    bm.run_backup_check()
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class BackupManager:
    """จัดการ backup ทุกช่องทาง."""

    RPO_MINUTES = 5    # Recovery Point Objective
    RTO_MINUTES = 10   # Recovery Time Objective

    def __init__(self, storage=None):
        self._storage = storage
        self._last_check: str | None = None

    def set_storage(self, storage) -> None:
        self._storage = storage

    def get_backup_status(self) -> dict:
        """สถานะ backup ทุกช่องทาง."""
        status: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "rpo_minutes": self.RPO_MINUTES,
            "rto_minutes": self.RTO_MINUTES,
            "channels": {},
        }

        # 1. GCS (primary storage)
        gcs_ok = False
        if self._storage:
            try:
                # ทดสอบว่าเขียน/อ่านได้
                test_path = "_backup_check/heartbeat.json"
                heartbeat = json.dumps({
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "source": "backup_manager",
                }).encode("utf-8")
                self._storage.put(
                    test_path, heartbeat,
                    content_type="application/json",
                )
                gcs_ok = True
            except Exception as e:
                logger.warning("Backup check GCS failed: %s", type(e).__name__)

        status["channels"]["gcs"] = {
            "status": "connected" if gcs_ok else "error",
            "description": "Primary storage — เอกสาร, metadata, events",
            "backup_method": "GCS Object Versioning (server-side)",
        }

        # 2. Google Drive
        try:
            import google_drive_sync
            ds = google_drive_sync.get_status()
            status["channels"]["google_drive"] = {
                "status": "connected" if ds.get("connected") else "not_connected",
                "description": "Secondary backup — สำเนาเอกสารไป Drive อัตโนมัติ",
                "enabled": ds.get("enabled", False),
                "folder": ds.get("folder_name", ""),
            }
        except Exception:
            status["channels"]["google_drive"] = {
                "status": "not_available",
                "description": "Google Drive module ไม่พร้อม",
            }

        # 3. Windows Sync
        status["channels"]["windows_sync"] = {
            "status": "configured",
            "description": "GCS → Windows PC ทุก 5 นาที (gcs_sync.py v2.2)",
            "interval": "5 minutes",
            "direction": "GCS → Windows (one-way download)",
        }

        # 4. Google Sheets
        try:
            import google_sheets_sync
            ss = google_sheets_sync.get_status()
            status["channels"]["google_sheets"] = {
                "status": "connected" if ss.get("connected") else "not_connected",
                "description": "ข้อมูลผู้ป่วย/แล็บ sync ไป Sheets",
                "enabled": ss.get("enabled", False),
            }
        except Exception:
            status["channels"]["google_sheets"] = {
                "status": "not_available",
            }

        self._last_check = status["timestamp"]
        return status

    def get_dr_plan(self) -> dict:
        """Disaster Recovery Plan."""
        return {
            "scenarios": [
                {
                    "scenario": "Render service down",
                    "rto": "~2 นาที",
                    "action": "Render auto-restart, ถ้าไม่ได้ → Manual rollback จาก dashboard",
                    "data_loss": "ไม่มี (GCS ไม่กระทบ)",
                },
                {
                    "scenario": "Deploy ทำระบบพัง",
                    "rto": "~5 นาที",
                    "action": "Render Rollback ไป commit ก่อนหน้า หรือ git revert + push",
                    "data_loss": "ไม่มี (เอกสารอยู่ใน GCS)",
                },
                {
                    "scenario": "GCS bucket ถูกลบ/เสียหาย",
                    "rto": "~30 นาที",
                    "action": "กู้จาก GCS Object Versioning + Google Drive backup + Windows local copy",
                    "data_loss": "ขึ้นกับ versioning retention period",
                },
                {
                    "scenario": "LINE Webhook ไม่ทำงาน",
                    "rto": "~5 นาที",
                    "action": "ตรวจ Render logs, LINE Developer Console, /health endpoint",
                    "data_loss": "LINE retry เอกสารที่ส่งไม่สำเร็จ (retry 3 ครั้ง)",
                },
                {
                    "scenario": "Windows PC เสีย",
                    "rto": "ไม่กระทบ Cloud",
                    "action": "ติดตั้ง gcs_sync.py ใหม่บนเครื่องอื่น, ตั้ง Task Scheduler",
                    "data_loss": "ไม่มี (ข้อมูลอยู่ใน GCS)",
                },
                {
                    "scenario": "Google Drive/Sheets ไม่ทำงาน",
                    "rto": "ไม่กระทบระบบหลัก",
                    "action": "ระบบ main ทำงานต่อได้ (Drive/Sheets เป็น secondary backup)",
                    "data_loss": "ไม่มี (GCS เป็น primary)",
                },
            ],
            "contacts": {
                "system_admin": "หมอกัมพล (Dr. Kamphon)",
                "render_dashboard": "https://dashboard.render.com",
                "line_developer": "https://developers.line.biz",
                "gcs_console": "https://console.cloud.google.com",
            },
            "last_drill": None,  # ยังไม่เคยทำ restoration drill
            "next_drill_recommended": "ภายใน 30 วัน",
        }

    def run_daily_backup(self, doc_index=None, master_registry=None) -> dict:
        """สร้าง daily snapshot ของข้อมูลสำคัญไว้บน GCS

        เรียกจาก scheduled job ทุกวัน เก็บไว้ใน _backups/daily/ บน GCS
        เก็บ snapshot ย้อนหลัง 30 วัน (ลบอัตโนมัติ)

        Safety:
          - อ่านอย่างเดียว ไม่แก้ไขข้อมูลต้นฉบับ
          - เขียน backup ไปที่ _backups/ prefix เท่านั้น
          - ไม่ส่งข้อมูลออกนอกระบบ
        """
        if not self._storage:
            return {"status": "skipped", "reason": "no storage"}

        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        backup_prefix = f"_backups/daily/{date_str}"
        result = {
            "date": date_str,
            "timestamp": now.isoformat(),
            "files": [],
            "errors": [],
        }

        # 1. Backup document index
        if doc_index:
            try:
                docs = doc_index.list_all() if hasattr(doc_index, 'list_all') else []
                if not docs and hasattr(doc_index, '_docs'):
                    docs = list(doc_index._docs.values())
                if docs:
                    # ลบ extracted_text เพื่อลดขนาด (เก็บแค่ metadata)
                    backup_docs = []
                    for d in docs:
                        doc_copy = {k: v for k, v in d.items()
                                    if k != "extracted_text"}
                        backup_docs.append(doc_copy)
                    payload = json.dumps({
                        "type": "document_index_backup",
                        "date": date_str,
                        "count": len(backup_docs),
                        "documents": backup_docs,
                    }, ensure_ascii=False, default=str).encode("utf-8")
                    path = f"{backup_prefix}/document_index.json"
                    self._storage.put(path, payload,
                                      content_type="application/json")
                    result["files"].append({
                        "path": path,
                        "records": len(backup_docs),
                    })
                    logger.info("Backup: document index %d records → %s",
                                len(backup_docs), path)
            except Exception as e:
                result["errors"].append(f"document_index: {e}")
                logger.warning("Backup document index failed: %s", e)

        # 2. Backup patient registry
        if master_registry:
            try:
                patients = []
                if hasattr(master_registry, '_patients'):
                    patients = [p.to_dict() if hasattr(p, 'to_dict') else p
                                for p in master_registry._patients.values()]
                elif hasattr(master_registry, 'list_all'):
                    patients = master_registry.list_all()
                if patients:
                    payload = json.dumps({
                        "type": "patient_registry_backup",
                        "date": date_str,
                        "count": len(patients),
                        "patients": patients,
                    }, ensure_ascii=False, default=str).encode("utf-8")
                    path = f"{backup_prefix}/patient_registry.json"
                    self._storage.put(path, payload,
                                      content_type="application/json")
                    result["files"].append({
                        "path": path,
                        "records": len(patients),
                    })
                    logger.info("Backup: patient registry %d records → %s",
                                len(patients), path)
            except Exception as e:
                result["errors"].append(f"patient_registry: {e}")
                logger.warning("Backup patient registry failed: %s", e)

        # 3. Backup summary (heartbeat)
        try:
            summary = json.dumps({
                "type": "daily_backup_summary",
                "date": date_str,
                "timestamp": now.isoformat(),
                "files_created": len(result["files"]),
                "errors": len(result["errors"]),
                "total_records": sum(f.get("records", 0) for f in result["files"]),
            }, ensure_ascii=False).encode("utf-8")
            summary_path = f"{backup_prefix}/backup_summary.json"
            self._storage.put(summary_path, summary,
                              content_type="application/json")
        except Exception:
            pass

        # 4. Cleanup old backups (> 30 days)
        try:
            self._cleanup_old_backups(30)
        except Exception as e:
            result["errors"].append(f"cleanup: {e}")

        result["status"] = "completed" if not result["errors"] else "partial"
        return result

    def _cleanup_old_backups(self, keep_days: int = 30) -> int:
        """ลบ backup เก่ากว่า keep_days วัน

        Returns: จำนวน folders ที่ลบ
        """
        if not self._storage or not hasattr(self._storage, 'list_files'):
            return 0

        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        deleted = 0

        try:
            # List backup directories
            files = self._storage.list_files(prefix="_backups/daily/")
            if not files:
                return 0

            # Extract unique dates
            dates = set()
            for f in files:
                name = f if isinstance(f, str) else f.get("name", "")
                parts = name.split("/")
                if len(parts) >= 3:
                    dates.add(parts[2])  # _backups/daily/YYYY-MM-DD

            # Delete old ones
            for date_dir in sorted(dates):
                if date_dir < cutoff_str:
                    try:
                        old_files = self._storage.list_files(
                            prefix=f"_backups/daily/{date_dir}/"
                        )
                        for f in (old_files or []):
                            fname = f if isinstance(f, str) else f.get("name", "")
                            if fname:
                                self._storage.delete(fname)
                        deleted += 1
                        logger.info("Backup cleanup: deleted %s", date_dir)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("Backup cleanup error: %s", e)

        return deleted
