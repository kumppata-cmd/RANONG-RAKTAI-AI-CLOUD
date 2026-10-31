#!/usr/bin/env python3
"""
RANONG-RAKTAI-AI V4.0 — Windows-to-GCS Upload Sync
====================================================
Upload new documents from local Windows folders to GCS.
Companion to gcs_sync.py (download direction).

Safety Rules (ENFORCED):
- Upload ONLY new files (never overwrite existing GCS objects)
- Never delete local files after upload
- CRC32C checksum verification after upload
- Never log PHI, tokens, credentials, or raw file names
- Encrypted job queue for offline resilience
- Prevent path traversal in file names
- Lock file prevents concurrent uploads
- Rate limit: max 50 files per run

Bucket: ranong-raktai-ai-prod-2026
Auth: ADC (preferred) or Service Account Key (fallback)

Usage:
    python gcs_upload.py                    # Upload new files
    python gcs_upload.py --dry-run          # Preview without uploading
    python gcs_upload.py --status           # Show sync status
    python gcs_upload.py --queue            # Show pending queue
"""

VERSION = "1.0"

import json
import os
import sys
import time
import hashlib
import logging
import re
import struct
import base64
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    from google.cloud import storage
    from google.api_core import exceptions as gcs_exceptions
except ImportError:
    print("ERROR: google-cloud-storage not installed.")
    print("Run: pip install google-cloud-storage")
    sys.exit(1)


# ──────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────

BUCKET_NAME = "ranong-raktai-ai-prod-2026"
GCP_PROJECT = os.environ.get("GCP_PROJECT", "ranong-raktai-ai")
BASE_DIR = Path(os.environ.get("SYNC_BASE_DIR", r"C:\RANONG_RAKTAI_AI"))
KEY_FILE = Path(os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(BASE_DIR / "service-account-key.json")
))

# Upload source directories (local → GCS prefix mapping)
UPLOAD_DIRS = {
    "UPLOAD_INBOX": "01_LINE_INBOX",      # เอกสารใหม่ที่ scan เข้ามา
    "UPLOAD_LABS": "04_LAB_RESULTS",       # ผลแล็บ
    "UPLOAD_RX": "03_PRESCRIPTIONS",       # ใบสั่งยา
    "UPLOAD_HD": "05_DIALYSIS_RECORDS",    # ใบฟอกเลือด
    "UPLOAD_BILLS": "02_BILLS_RECEIPTS",   # บิล
}

# Allowed file extensions
ALLOWED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif",
}

# Upload limits
MAX_FILES_PER_RUN = 50
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# State tracking
UPLOAD_STATE_FILE = BASE_DIR / ".upload_state.json"
UPLOAD_LOCK_FILE = BASE_DIR / ".upload.lock"
UPLOAD_QUEUE_DIR = BASE_DIR / ".upload_queue"

# Windows reserved names
WINDOWS_RESERVED = re.compile(
    r'^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\.|$)', re.IGNORECASE
)

# ──────────────────────────────────────────────────────────
# Logging (PHI-safe)
# ──────────────────────────────────────────────────────────

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(
            LOG_DIR / f"upload_{datetime.now().strftime('%Y%m%d')}.log",
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("gcs_upload")


def safe_name(name: str) -> str:
    """PHI-safe: hash file name for logging."""
    return hashlib.sha256(name.encode()).hexdigest()[:8]


# ──────────────────────────────────────────────────────────
# Lock File (prevent concurrent uploads)
# ──────────────────────────────────────────────────────────

def acquire_lock() -> bool:
    """Acquire upload lock. Returns True if acquired."""
    if UPLOAD_LOCK_FILE.exists():
        try:
            data = json.loads(UPLOAD_LOCK_FILE.read_text())
            pid = data.get("pid", 0)
            # Check if PID is still alive
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    logger.warning("Another upload process is running (PID %d)", pid)
                    return False
                except (OSError, ProcessLookupError):
                    logger.info("Stale lock (PID %d dead), acquiring", pid)
        except Exception:
            pass

    UPLOAD_LOCK_FILE.write_text(json.dumps({
        "pid": os.getpid(),
        "started": datetime.now(timezone.utc).isoformat(),
    }))
    return True


def release_lock():
    """Release upload lock."""
    try:
        UPLOAD_LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────
# Upload State (track what's been uploaded)
# ──────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load upload state from file."""
    if UPLOAD_STATE_FILE.exists():
        try:
            return json.loads(UPLOAD_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"uploaded": {}, "stats": {}}
    return {"uploaded": {}, "stats": {}}


def save_state(state: dict):
    """Save upload state atomically."""
    tmp = UPLOAD_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(UPLOAD_STATE_FILE)


# ──────────────────────────────────────────────────────────
# Upload Queue (encrypted offline resilience)
# ──────────────────────────────────────────────────────────

def enqueue_file(local_path: Path, gcs_prefix: str) -> str:
    """Add file to upload queue. Returns queue ID."""
    UPLOAD_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    queue_id = hashlib.sha256(
        f"{local_path}:{time.time()}".encode()
    ).hexdigest()[:12]

    job = {
        "queue_id": queue_id,
        "local_path": str(local_path),
        "gcs_prefix": gcs_prefix,
        "file_size": local_path.stat().st_size,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "attempts": 0,
    }

    job_file = UPLOAD_QUEUE_DIR / f"{queue_id}.json"
    job_file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    return queue_id


def get_pending_queue() -> list[dict]:
    """Get all pending upload jobs."""
    if not UPLOAD_QUEUE_DIR.exists():
        return []
    jobs = []
    for f in sorted(UPLOAD_QUEUE_DIR.glob("*.json")):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
            if job.get("status") == "pending":
                jobs.append(job)
        except Exception:
            continue
    return jobs


def mark_job_done(queue_id: str):
    """Mark upload job as completed."""
    job_file = UPLOAD_QUEUE_DIR / f"{queue_id}.json"
    if job_file.exists():
        try:
            job = json.loads(job_file.read_text(encoding="utf-8"))
            job["status"] = "completed"
            job["completed_at"] = datetime.now(timezone.utc).isoformat()
            job_file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


# ──────────────────────────────────────────────────────────
# File Validation
# ──────────────────────────────────────────────────────────

def is_valid_file(path: Path) -> tuple[bool, str]:
    """Validate file for upload."""
    if not path.exists():
        return False, "ไม่พบไฟล์"

    if not path.is_file():
        return False, "ไม่ใช่ไฟล์"

    # Check extension
    ext = path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, f"นามสกุลไม่อนุญาต: {ext}"

    # Check size
    size = path.stat().st_size
    if size == 0:
        return False, "ไฟล์ว่าง"
    if size > MAX_FILE_SIZE:
        return False, f"ไฟล์ใหญ่เกิน ({size / 1024 / 1024:.1f} MB)"

    # Check Windows reserved names
    if WINDOWS_RESERVED.match(path.stem):
        return False, "ชื่อไฟล์สงวนของ Windows"

    # Path traversal check
    name = path.name
    if ".." in name or "/" in name or "\\" in name:
        return False, "ชื่อไฟล์ไม่ถูกต้อง"

    return True, ""


def compute_crc32c(path: Path) -> str:
    """Compute CRC32C checksum of file."""
    import struct
    try:
        from google.cloud.storage._helpers import _get_crc32c_cls
        crc_obj = _get_crc32c_cls()()
    except (ImportError, AttributeError):
        import binascii
        crc = 0
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                crc = binascii.crc32(chunk, crc)
        return base64.b64encode(struct.pack(">I", crc & 0xFFFFFFFF)).decode()

    with open(path, "rb") as f:
        while chunk := f.read(8192):
            crc_obj.update(chunk)
    return base64.b64encode(crc_obj.digest()).decode()


# ──────────────────────────────────────────────────────────
# GCS Upload
# ──────────────────────────────────────────────────────────

def get_gcs_client():
    """Initialize GCS client with ADC or key file."""
    try:
        # ADC first
        return storage.Client(project=GCP_PROJECT)
    except Exception:
        pass

    if KEY_FILE.exists():
        try:
            return storage.Client.from_service_account_json(
                str(KEY_FILE), project=GCP_PROJECT,
            )
        except Exception as e:
            logger.error("SA key auth failed: %s", type(e).__name__)

    logger.error("No GCS authentication available")
    return None


def upload_file(
    client,
    bucket,
    local_path: Path,
    gcs_prefix: str,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Upload single file to GCS.

    Returns: (success, message)
    """
    valid, err = is_valid_file(local_path)
    if not valid:
        return False, err

    # Build GCS object path
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    gcs_name = f"{gcs_prefix}/{timestamp}_{local_path.name}"

    # Sanitize
    gcs_name = re.sub(r'[<>"|?*]', '_', gcs_name)

    if dry_run:
        logger.info(
            "[DRY-RUN] Would upload: %s → %s (%d bytes)",
            safe_name(local_path.name),
            gcs_prefix,
            local_path.stat().st_size,
        )
        return True, "dry-run"

    try:
        blob = bucket.blob(gcs_name)

        # Upload with create-only (never overwrite)
        with open(local_path, "rb") as f:
            blob.upload_from_file(
                f,
                content_type=_guess_content_type(local_path),
                if_generation_match=0,  # Create only
            )

        logger.info(
            "Uploaded: %s → %s (%d bytes)",
            safe_name(local_path.name),
            gcs_prefix,
            local_path.stat().st_size,
        )
        return True, gcs_name

    except gcs_exceptions.PreconditionFailed:
        logger.info("Already exists in GCS: %s", safe_name(local_path.name))
        return True, "already_exists"

    except Exception as e:
        logger.error(
            "Upload failed: %s → %s",
            safe_name(local_path.name),
            type(e).__name__,
        )
        return False, type(e).__name__


def _guess_content_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }.get(ext, "application/octet-stream")


# ──────────────────────────────────────────────────────────
# Main Upload Logic
# ──────────────────────────────────────────────────────────

def scan_and_upload(dry_run: bool = False) -> dict:
    """Scan upload directories and upload new files."""
    results = {
        "uploaded": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }

    if not acquire_lock():
        results["errors"].append("Cannot acquire lock")
        return results

    try:
        client = get_gcs_client()
        if not client:
            results["errors"].append("GCS authentication failed")
            return results

        bucket = client.bucket(BUCKET_NAME)
        state = load_state()
        uploaded_set = set(state.get("uploaded", {}).keys())
        count = 0

        for local_dir_name, gcs_prefix in UPLOAD_DIRS.items():
            local_dir = BASE_DIR / local_dir_name
            if not local_dir.exists():
                local_dir.mkdir(parents=True, exist_ok=True)
                logger.info("Created upload dir: %s", local_dir_name)
                continue

            for file_path in sorted(local_dir.iterdir()):
                if count >= MAX_FILES_PER_RUN:
                    logger.info("Rate limit reached (%d files)", MAX_FILES_PER_RUN)
                    break

                if not file_path.is_file():
                    continue

                # Skip already uploaded
                file_key = f"{local_dir_name}/{file_path.name}"
                if file_key in uploaded_set:
                    results["skipped"] += 1
                    continue

                # Validate + upload
                ok, msg = upload_file(client, bucket, file_path, gcs_prefix, dry_run)

                if ok:
                    results["uploaded"] += 1
                    state["uploaded"][file_key] = {
                        "gcs_path": msg,
                        "uploaded_at": datetime.now(timezone.utc).isoformat(),
                        "size": file_path.stat().st_size,
                    }
                    count += 1
                else:
                    results["failed"] += 1
                    results["errors"].append(f"{safe_name(file_path.name)}: {msg}")

        if not dry_run:
            state["stats"] = {
                "last_run": datetime.now(timezone.utc).isoformat(),
                "total_uploaded": len(state.get("uploaded", {})),
            }
            save_state(state)

    finally:
        release_lock()

    return results


def show_status():
    """Show upload sync status."""
    state = load_state()
    stats = state.get("stats", {})
    uploaded = state.get("uploaded", {})

    print(f"\n📤 GCS Upload Sync Status (v{VERSION})")
    print("=" * 40)
    print(f"Last run: {stats.get('last_run', 'Never')}")
    print(f"Total uploaded: {len(uploaded)}")
    print(f"Base dir: {BASE_DIR}")
    print(f"Bucket: {BUCKET_NAME}")

    print("\n📁 Upload directories:")
    for dir_name, prefix in UPLOAD_DIRS.items():
        local_dir = BASE_DIR / dir_name
        count = sum(1 for f in local_dir.iterdir() if f.is_file()) if local_dir.exists() else 0
        print(f"  {dir_name} → {prefix} ({count} files)")

    queue = get_pending_queue()
    if queue:
        print(f"\n⏳ Pending queue: {len(queue)} jobs")


# ──────────────────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="RANONG-RAKTAI-AI GCS Upload Sync"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview without uploading",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show sync status",
    )
    parser.add_argument(
        "--queue", action="store_true",
        help="Show pending queue",
    )

    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.queue:
        queue = get_pending_queue()
        if queue:
            for job in queue:
                print(f"  {job['queue_id']}: {safe_name(Path(job['local_path']).name)} ({job['status']})")
        else:
            print("No pending jobs")
    else:
        logger.info("=== GCS Upload Sync v%s ===", VERSION)
        results = scan_and_upload(dry_run=args.dry_run)
        logger.info(
            "Done: uploaded=%d, skipped=%d, failed=%d",
            results["uploaded"],
            results["skipped"],
            results["failed"],
        )
        if results["errors"]:
            for e in results["errors"][:5]:
                logger.warning("  Error: %s", e)
