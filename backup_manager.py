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
