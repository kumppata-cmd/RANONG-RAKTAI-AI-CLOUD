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

import threading

# ── Thread-safety guard ──────────────────────────────────────
# googleapiclient/httplib2 ไม่ thread-safe: เมื่อเอกสารหลายฉบับถูก
# ประมวลผลพร้อมกัน (multi-image batch) แล้วเรียก API ผ่าน service
# object เดียวกัน จะเกิด SSLError และ worker ถูก abort (exit 134)
# → ทุกการเรียก API ของโมดูลนี้ต้องผ่าน lock เดียวกัน (serialize)
_api_lock = threading.RLock()


def _serialized(fn):
    """เรียก fn ทีละ thread เท่านั้น — กัน SSL abort ของ httplib2."""
    import functools

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        with _api_lock:
            return fn(*args, **kwargs)

    return _wrapper

DRIVE_ENABLED = os.getenv("GOOGLE_DRIVE_ENABLED", "").strip().lower() == "true"
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()

_drive_service = None
_drive_mode = ""            # "oauth" (user-owned files) | "service_account"
_refresh_token_getter = None   # callable → refresh token stored by the app (hd_settings)
OAUTH_CLIENT_ID = os.getenv("GOOGLE_DRIVE_OAUTH_CLIENT_ID", "").strip()
OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_DRIVE_OAUTH_CLIENT_SECRET", "").strip()
OAUTH_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def set_refresh_token_getter(fn):
    """App supplies how to read the OAuth refresh token (e.g. from hd_settings)."""
    global _refresh_token_getter, _drive_service
    _refresh_token_getter = fn
    _drive_service = None


def _oauth_refresh_token() -> str:
    tok = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN", "").strip()
    if not tok and _refresh_token_getter:
        try:
            tok = (_refresh_token_getter() or "").strip()
        except Exception:
            tok = ""
    return tok


def oauth_configured() -> bool:
    return bool(OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET)


def reset_service():
    global _drive_service, _oauth_root_cache
    _drive_service = None
    _oauth_root_cache = ""


def _get_drive_service():
    """สร้าง Drive API service (lazy init).

    ลำดับ: 1) OAuth ของผู้ใช้ (ไฟล์เป็นของบัญชี Google ของหมอ — service account
    ไม่มีโควตา Drive จึงอัปโหลดลง My Drive ไม่ได้: 403 storageQuotaExceeded)
    2) service account จาก GCS_CREDENTIALS_BASE64 / ADC (ใช้ได้กับ Shared Drive เท่านั้น)
    """
    global _drive_service, _drive_mode
    if _drive_service is not None:
        return _drive_service
    try:
        import google.auth
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/drive"]
        rt = _oauth_refresh_token()
        if oauth_configured() and rt:
            from google.oauth2.credentials import Credentials as _UserCreds
            credentials = _UserCreds(None, refresh_token=rt, token_uri="https://oauth2.googleapis.com/token",
                                     client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET,
                                     scopes=OAUTH_SCOPES)
            _drive_mode = "oauth"
            _drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)
            logger.info("Google Drive service initialized (user OAuth)")
            return _drive_service
        creds_b64 = os.getenv("GCS_CREDENTIALS_BASE64", "").strip()
        _drive_mode = "service_account"
        if creds_b64 and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
            # Render has only the base64 key (same SA as GCS) — ADC would fail
            import base64 as _b64
            import json as _json
            from google.oauth2 import service_account
            info = _json.loads(_b64.b64decode(creds_b64))
            credentials = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        else:
            credentials, _project = google.auth.default(scopes=scopes)
        _drive_service = build(
            "drive", "v3", credentials=credentials, cache_discovery=False
        )
        logger.info("Google Drive service initialized")
        return _drive_service
    except Exception as e:
        logger.warning("Google Drive init failed: %s", type(e).__name__)
        return None


OAUTH_ROOT_FOLDER_NAME = "RANONG-RAKTAI-HD-Records"
_oauth_root_cache = ""


def _oauth_root_id(service) -> str:
    """With scope drive.file the app can only see files it created, so the
    doctor's own folder (DRIVE_FOLDER_ID) is invisible. Use/create an
    app-owned folder at the top of My Drive instead."""
    global _oauth_root_cache
    if _oauth_root_cache:
        return _oauth_root_cache
    q = (f"name='{OAUTH_ROOT_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' "
         f"and trashed=false and 'root' in parents")
    res = service.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
    if res:
        _oauth_root_cache = res[0]["id"]
    else:
        f = service.files().create(body={"name": OAUTH_ROOT_FOLDER_NAME,
                                         "mimeType": "application/vnd.google-apps.folder"},
                                   fields="id").execute()
        _oauth_root_cache = f["id"]
        logger.info("Drive: created app root folder")
    return _oauth_root_cache


def _root_folder_id(service) -> str:
    return _oauth_root_id(service) if drive_mode() == "oauth" else DRIVE_FOLDER_ID


def is_available() -> bool:
    """ตรวจว่า Drive พร้อมใช้งาน."""
    if not DRIVE_ENABLED:
        return False
    return drive_mode() == "oauth" or bool(DRIVE_FOLDER_ID)


def drive_mode() -> str:
    """'oauth' when the doctor's account is connected, else 'service_account'."""
    return "oauth" if (oauth_configured() and _oauth_refresh_token()) else "service_account"


@_serialized
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
        root = _root_folder_id(service)
        info = service.files().get(fileId=root, fields="id,name").execute()
        status["connected"] = True
        status["folder_name"] = info.get("name", "")
        status["mode"] = drive_mode()
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


def _ensure_path(service, rel_path: str, root_id: str) -> str:
    """Walk/create nested folders 'a/b/c' under root; returns the leaf folder id."""
    parent = root_id
    for part in [x for x in rel_path.split("/") if x]:
        parent = _find_or_create_folder(service, part, parent)
    return parent


@_serialized
def upload_path(file_content: bytes, rel_path: str, mime_type: str = "application/pdf",
                replace: bool = True) -> dict:
    """Upload to a nested path under the root folder, e.g.
    'HD-Records/2026/2026-11/2026-11-12/HD-2026-11-12.csv'. Creates folders
    as needed; with replace=True an existing file of the same name in that
    folder is updated in place (so a re-run does not create duplicates)."""
    if not is_available():
        return {"error": "Google Drive ไม่ได้เปิดใช้งาน"}
    service = _get_drive_service()
    if not service:
        return {"error": "ไม่สามารถเชื่อมต่อ Google Drive"}
    try:
        from googleapiclient.http import MediaInMemoryUpload
        folder, _, name = rel_path.rpartition("/")
        root = _root_folder_id(service)
        parent_id = _ensure_path(service, folder, root) if folder else root
        # Simple (multipart) upload is capped at 5 MB by Google — the nightly DB backup
        # (~15 MB) failed there. Use resumable upload for anything over 4 MB.
        media = MediaInMemoryUpload(file_content, mimetype=mime_type,
                                    resumable=len(file_content or b"") > 4 * 1024 * 1024)
        existing = []
        if replace:
            q = (f"name='{name.replace(chr(39), chr(92) + chr(39))}' and '{parent_id}' in parents and trashed=false")
            existing = service.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
        if existing:
            result = service.files().update(fileId=existing[0]["id"], media_body=media,
                                            fields="id, name, webViewLink").execute()
        else:
            result = service.files().create(body={"name": name, "parents": [parent_id]},
                                            media_body=media, fields="id, name, webViewLink").execute()
        logger.info("Drive upload_path OK: id=%s", result.get("id", "")[:8])
        return result
    except Exception as e:
        reason = ""
        try:
            import json as _j
            reason = _j.loads(getattr(e, "content", b"{}") or b"{}").get("error", {}).get("errors", [{}])[0].get("reason", "")
        except Exception:
            pass
        logger.warning("Drive upload_path failed: %s %s", type(e).__name__, reason)
        if reason == "storageQuotaExceeded":
            return {"error": "storageQuotaExceeded — service account ไม่มีโควตา Drive; ต้องเชื่อมบัญชี Google ของหมอ (OAuth) ใน ⚙️ ตั้งค่า"}
        return {"error": f"{type(e).__name__} {reason}".strip()}


# ─── Upload ──────────────────────────────────────────────────


@_serialized
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


@_serialized
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


@_serialized
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


@_serialized
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
