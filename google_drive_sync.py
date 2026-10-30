"""Google Drive sync — สำเนาเอกสารจาก GCS ไป Drive อัตโนมัติ

ใช้ Service Account ตัวเดียวกับ GCS (ADC)
ต้องเปิด Drive API ใน Google Cloud Console
ต้อง share Drive folder กับ Service Account email

ENV VARS:
- GOOGLE_DRIVE_FOLDER_ID: ID ของ folder ใน Drive ที่จะเก็บ backup
- GOOGLE_DRIVE_ENABLED: "true" เพื่อเปิดใช้งาน (default: false)

ไม่ log ข้อมูลผู้ป่วย (PDPA)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DRIVE_ENABLED = os.getenv("GOOGLE_DRIVE_ENABLED", "").strip().lower() == "true"
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()

_drive_service = None


def _get_drive_service():
    """สร้าง Drive API service (lazy init, ใช้ ADC)."""
    global _drive_service
    if _drive_service is not None:
        return _drive_service
    try:
        import google.auth
        from googleapiclient.discovery import build

        credentials, _project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        _drive_service = build(
            "drive", "v3", credentials=credentials, cache_discovery=False
        )
        logger.info("Google Drive service initialized")
        return _drive_service
    except Exception as e:
        logger.warning("Google Drive init failed: %s", type(e).__name__)
        return None


def is_available() -> bool:
    """ตรวจว่า Drive พร้อมใช้งาน."""
    return DRIVE_ENABLED and bool(DRIVE_FOLDER_ID)


def get_status() -> dict:
    """สถานะการเชื่อมต่อ Drive."""
    status: dict = {
        "enabled": DRIVE_ENABLED,
        "folder_id": (DRIVE_FOLDER_ID[:8] + "...") if DRIVE_FOLDER_ID else "",
        "connected": False,
    }
    if not is_available():
        return status
    service = _get_drive_service()
    if not service:
        return status
    try:
        info = service.files().get(
            fileId=DRIVE_FOLDER_ID, fields="id,name"
        ).execute()
        status["connected"] = True
        status["folder_name"] = info.get("name", "")
    except Exception as e:
        status["error"] = type(e).__name__
    return status


# ─── Folder helpers ──────────────────────────────────────────


def _sanitize_folder_name(name: str) -> str:
    """ทำชื่อ folder ให้ปลอดภัยสำหรับ Drive."""
    if not name:
        return "unknown"
    import re
    safe = re.sub(r'[<>:"/\\|?*]', "_", name)
    return safe[:100].strip(". ")


def _find_or_create_folder(
    service, name: str, parent_id: str
) -> str:
    """ค้นหา folder ใน Drive หรือสร้างใหม่."""
    safe_name = _sanitize_folder_name(name)
    try:
        query = (
            f"name='{safe_name}' and '{parent_id}' in parents "
            f"and mimeType='application/vnd.google-apps.folder' "
            f"and trashed=false"
        )
        results = service.files().list(
            q=query, fields="files(id)", pageSize=1
        ).execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"]

        folder_meta = {
            "name": safe_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = service.files().create(
            body=folder_meta, fields="id"
        ).execute()
        logger.info("Drive: created folder %s", safe_name)
        return folder["id"]
    except Exception as e:
        logger.warning("Drive folder create failed: %s", type(e).__name__)
        return parent_id


# ─── Upload ──────────────────────────────────────────────────


def upload_document(
    file_content: bytes,
    filename: str,
    mime_type: str = "application/pdf",
    subfolder: str = "",
) -> dict:
    """อัปโหลดเอกสารไป Drive.

    Args:
        file_content: เนื้อหาไฟล์
        filename: ชื่อไฟล์
        mime_type: MIME type
        subfolder: subfolder ภายใน Drive folder (สร้างอัตโนมัติ)

    Returns:
        {"id": file_id, "name": filename, "webViewLink": url}
        หรือ {"error": msg}
    """
    if not is_available():
        return {"error": "Google Drive ไม่ได้เปิดใช้งาน"}

    service = _get_drive_service()
    if not service:
        return {"error": "ไม่สามารถเชื่อมต่อ Google Drive"}

    try:
        from googleapiclient.http import MediaInMemoryUpload

        parent_id = DRIVE_FOLDER_ID
        if subfolder:
            parent_id = _find_or_create_folder(
                service, subfolder, DRIVE_FOLDER_ID
            )

        media = MediaInMemoryUpload(file_content, mimetype=mime_type)
        file_metadata = {
            "name": filename,
            "parents": [parent_id],
        }
        result = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webViewLink",
        ).execute()

        # ไม่ log ชื่อไฟล์ (อาจมีข้อมูลผู้ป่วย)
        logger.info(
            "Drive upload OK: id=%s", result.get("id", "")[:8]
        )
        return result
    except Exception as e:
        logger.warning("Drive upload failed: %s", type(e).__name__)
        return {"error": f"อัปโหลดไม่สำเร็จ: {type(e).__name__}"}


# ─── List / Query ────────────────────────────────────────────


def list_backups(subfolder: str = "", limit: int = 50) -> list[dict]:
    """แสดงรายการ backup ใน Drive."""
    if not is_available():
        return []
    service = _get_drive_service()
    if not service:
        return []
    try:
        parent_id = DRIVE_FOLDER_ID
        if subfolder:
            parent_id = _find_or_create_folder(
                service, subfolder, DRIVE_FOLDER_ID
            )

        query = f"'{parent_id}' in parents and trashed=false"
        results = service.files().list(
            q=query,
            pageSize=limit,
            fields="files(id, name, createdTime, size, webViewLink)",
            orderBy="createdTime desc",
        ).execute()
        return results.get("files", [])
    except Exception as e:
        logger.warning("Drive list failed: %s", type(e).__name__)
        return []


def count_backups() -> int:
    """นับจำนวนไฟล์ทั้งหมดใน backup folder."""
    if not is_available():
        return 0
    service = _get_drive_service()
    if not service:
        return 0
    try:
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        results = service.files().list(
            q=query, pageSize=1, fields="files(id)"
        ).execute()
        # Drive API ไม่มี count — ใช้ list ทั้งหมด
        return len(results.get("files", []))
    except Exception:
        return 0


# ─── Backup from GCS ─────────────────────────────────────────


def backup_document_from_gcs(
    gcs_storage, gcs_path: str, subfolder: str = ""
) -> dict:
    """ดาวน์โหลดจาก GCS แล้วอัปโหลดไป Drive.

    Args:
        gcs_storage: GCS storage object (StorageGCS)
        gcs_path: GCS object path
        subfolder: subfolder ใน Drive

    Returns: upload result dict
    """
    if not is_available():
        return {"error": "Google Drive ไม่ได้เปิดใช้งาน"}

    try:
        content = gcs_storage.get(gcs_path)
        if not content:
            return {"error": "ไม่พบไฟล์บน GCS"}

        if isinstance(content, str):
            content = content.encode("utf-8")

        filename = gcs_path.rsplit("/", 1)[-1] if "/" in gcs_path else gcs_path
        mime = "application/pdf" if filename.endswith(".pdf") else "image/jpeg"

        return upload_document(content, filename, mime, subfolder)
    except Exception as e:
        logger.warning("GCS→Drive backup failed: %s", type(e).__name__)
        return {"error": f"backup ไม่สำเร็จ: {type(e).__name__}"}
