#!/usr/bin/env python3
"""
RANONG-RAKTAI-AI 2.0 — GCS-to-Windows Cloud Sync (Production Hardened)
=======================================================================
One-way download from Google Cloud Storage to local Windows folders.

Safety Rules (ENFORCED):
- Download ONLY — never delete or modify source files in GCS
- Never overwrite existing local files
- Never log PHI, tokens, credentials, or raw object names
- Track sync state atomically to prevent duplicate downloads
- Verify file integrity (size + checksum) before marking complete
- Retry on network failures with exponential backoff
- Prevent path traversal attacks from malicious object names
- Lock file with PID-alive check prevents concurrent runs

Bucket: ranong-raktai-ai-prod-2026
Auth: ADC (preferred) or Service Account Key (fallback)
"""

VERSION = "2.2-hardened"

import json
import os
import sys
import time
import hashlib
import logging
import re
import shutil
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


# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────

BUCKET_NAME = "ranong-raktai-ai-prod-2026"
GCP_PROJECT = os.environ.get("GCP_PROJECT", "ranong-raktai-ai")
BASE_DIR = Path(os.environ.get("SYNC_BASE_DIR", r"C:\RANONG_RAKTAI_AI"))
KEY_FILE = Path(os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(BASE_DIR / "service-account-key.json")
))

# Expected GCS prefixes — validated against actual bucket contents
EXPECTED_PREFIXES = [
    "01_LINE_INBOX",
    "02_BILLS_RECEIPTS",
    "03_PRESCRIPTIONS",
    "04_LAB_RESULTS",
    "05_DIALYSIS_RECORDS",
    "06_AI_REVIEW",
    "07_APPROVED_DOCUMENTS",
]

# Files to skip (internal metadata, not patient documents)
SKIP_PATTERNS = re.compile(
    r"(^_index|^\.|\._|\.tmp$|\.partial$|sync_state|sync\.lock)",
    re.IGNORECASE
)

# Allowed file extensions (whitelist for safety)
ALLOWED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif",
    ".txt", ".json", ".csv",
}

# Sync state and lock
STATE_FILE = BASE_DIR / "sync_state.json"
STATE_BACKUP = BASE_DIR / "sync_state.json.bak"
LOCK_FILE = BASE_DIR / "sync.lock"

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY_BASE = 5  # seconds
LOCK_STALE_SECONDS = 1800  # 30 minutes

# Maximum file size to download (500 MB safety limit)
MAX_FILE_SIZE = 500 * 1024 * 1024

# Logging — NO PHI, NO tokens, NO credentials
LOG_DIR = BASE_DIR / "logs"


# ──────────────────────────────────────────────────────────────
# 5. Safe Logging — no PHI, credentials, object names, or
#    sensitive exception messages
# ──────────────────────────────────────────────────────────────

def setup_logging():
    """Configure logging with safe format — no PHI in logs."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"sync_{datetime.now().strftime('%Y%m')}.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    logger = logging.getLogger("gcs_sync")
    # Prevent duplicate handlers on re-entry
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    return logger


def safe_log_path(blob_name: str) -> str:
    """
    Sanitize blob/object name for logging.
    Keep folder prefix + date prefix visible but hash the filename,
    which may contain patient identifiers, owner hashes, or UUIDs
    that could be correlated to patients.
    """
    if not blob_name:
        return "[empty]"
    parts = blob_name.split("/")
    if len(parts) <= 1:
        # Just a prefix name — safe
        return parts[0]
    # First part = folder (safe), middle parts = date prefix (safe)
    # Last part = filename (potentially PHI)
    safe_parts = parts[:-1]
    filename_hash = hashlib.sha256(parts[-1].encode("utf-8")).hexdigest()[:12]
    ext = PurePosixPath(parts[-1]).suffix.lower()
    safe_parts.append(f"[h:{filename_hash}]{ext}")
    return "/".join(safe_parts)


def safe_error_msg(exc: Exception) -> str:
    """
    Sanitize exception message — strip paths, URLs, tokens that
    might leak PHI or credentials in log files.
    """
    msg = str(exc)
    # Remove anything that looks like a file path with patient info
    msg = re.sub(r"[A-Za-z]:\\[^\s]+", "[path-redacted]", msg)
    msg = re.sub(r"/[^\s]+/[^\s]+", "[path-redacted]", msg)
    # Remove bearer tokens, API keys
    msg = re.sub(r"(Bearer\s+|token[=:]\s*)[^\s]+", r"\1[redacted]", msg, flags=re.I)
    msg = re.sub(r"(key[=:]\s*)[^\s]+", r"\1[redacted]", msg, flags=re.I)
    # Truncate to prevent log overflow
    if len(msg) > 200:
        msg = msg[:200] + "...[truncated]"
    return msg


# ──────────────────────────────────────────────────────────────
# 1. Atomic Lock with PID-alive checking
# ──────────────────────────────────────────────────────────────

def _get_process_create_time(pid: int) -> float | None:
    """Get process creation time as a timestamp (Windows + Unix)."""
    try:
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return None
            try:
                creation = ctypes.wintypes.FILETIME()
                exit_t = ctypes.wintypes.FILETIME()
                kernel_t = ctypes.wintypes.FILETIME()
                user_t = ctypes.wintypes.FILETIME()
                ok = kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_t),
                    ctypes.byref(kernel_t),
                    ctypes.byref(user_t),
                )
                if ok:
                    # Convert FILETIME to Unix timestamp
                    ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                    return (ft - 116444736000000000) / 10000000.0
                return None
            finally:
                kernel32.CloseHandle(handle)
        else:
            # Unix: use /proc or psutil
            stat_path = f"/proc/{pid}/stat"
            if os.path.exists(stat_path):
                with open(stat_path, "r") as f:
                    parts = f.read().split(")")[-1].split()
                    starttime = int(parts[19])
                    return starttime / os.sysconf("SC_CLK_TCK")
    except Exception:
        pass
    return None


def _is_process_alive(pid: int, lock_create_time: float | None = None) -> bool:
    """
    Check if a process with given PID is still running (Windows + Unix).
    FIX v2.2: Also verifies process creation time to guard against PID reuse.
    """
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                # PID exists — verify it's the same process via creation time
                if lock_create_time is not None:
                    actual_ct = _get_process_create_time(pid)
                    if actual_ct is not None:
                        # Allow 2-second tolerance for timestamp precision
                        if abs(actual_ct - lock_create_time) > 2.0:
                            return False  # PID reused by different process
                return True
            return False
        else:
            os.kill(pid, 0)
            if lock_create_time is not None:
                actual_ct = _get_process_create_time(pid)
                if actual_ct is not None and abs(actual_ct - lock_create_time) > 2.0:
                    return False
            return True
    except (OSError, PermissionError):
        return False
    except Exception:
        return False


def acquire_lock(logger) -> bool:
    """
    Acquire lock file atomically.
    - Uses os.open with O_CREAT|O_EXCL for atomic creation
    - Stores PID and timestamp
    - Checks if holding process is alive before declaring stale
    """
    my_create_time = _get_process_create_time(os.getpid())
    lock_data = {
        "pid": os.getpid(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "pct": my_create_time,  # FIX v2.2: process creation time for PID reuse guard
    }

    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r") as f:
                existing = json.load(f)
            existing_pid = existing.get("pid", 0)
            existing_ts = existing.get("ts", "")
            existing_pct = existing.get("pct")  # process creation time

            # Check if holding process is still alive (with PID reuse guard)
            if _is_process_alive(existing_pid, existing_pct):
                logger.info(
                    f"Lock held by active process (pid={existing_pid}) — skipping"
                )
                return False

            # Process is dead — check staleness as double-check
            lock_age = time.time() - LOCK_FILE.stat().st_mtime
            if lock_age < 10:
                # Very fresh lock, process might be starting — be cautious
                logger.info("Lock is fresh but holder not found — waiting")
                return False

            # Stale lock from dead process — safe to remove
            logger.info(
                f"Removing stale lock (pid={existing_pid} not alive, "
                f"age={int(lock_age)}s)"
            )
            LOCK_FILE.unlink()

        except (json.JSONDecodeError, KeyError, IOError):
            # Corrupt lock file — check age
            try:
                lock_age = time.time() - LOCK_FILE.stat().st_mtime
                if lock_age > LOCK_STALE_SECONDS:
                    logger.info("Removing corrupt stale lock")
                    LOCK_FILE.unlink()
                else:
                    logger.warning("Corrupt lock file, not stale — skipping")
                    return False
            except IOError:
                return False

    # Atomic create using O_CREAT | O_EXCL (fails if file exists)
    try:
        fd = os.open(
            str(LOCK_FILE),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644
        )
        try:
            os.write(fd, json.dumps(lock_data).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        # Another process grabbed the lock between our check and create
        logger.info("Lock race lost — skipping")
        return False
    except IOError as e:
        logger.error(f"Lock acquire failed: {type(e).__name__}")
        return False


def release_lock(logger):
    """Release lock file, but only if we own it."""
    try:
        if LOCK_FILE.exists():
            with open(LOCK_FILE, "r") as f:
                data = json.load(f)
            if data.get("pid") == os.getpid():
                LOCK_FILE.unlink()
            else:
                logger.warning("Lock not owned by this process — not releasing")
    except (json.JSONDecodeError, IOError):
        # If we can't read it, try to remove if it has our PID
        try:
            LOCK_FILE.unlink()
        except IOError:
            pass


# ──────────────────────────────────────────────────────────────
# 7. Atomic Sync State with Recovery
# ──────────────────────────────────────────────────────────────

def load_state(logger) -> dict:
    """
    Load sync state with recovery from backup.
    If primary is corrupt, tries backup. If both fail, starts fresh.
    """
    default_state = {
        "downloaded": {},
        "last_sync": None,
        "total_downloaded": 0,
        "total_errors": 0,
        "version": 2,
    }

    for path in [STATE_FILE, STATE_BACKUP]:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "downloaded" in data:
                    # Migrate v1 state if needed
                    if data.get("version", 1) < 2:
                        data["version"] = 2
                        data.setdefault("total_downloaded", 0)
                        data.setdefault("total_errors", 0)
                    if path == STATE_BACKUP:
                        logger.warning("Recovered state from backup")
                    return data
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(
                    f"State file corrupt ({path.name}): {type(e).__name__}"
                )
                continue

    logger.info("Starting with fresh sync state")
    return default_state


def save_state(state: dict, logger):
    """
    Save sync state atomically:
    1. Write to .tmp with fsync
    2. Backup current state to .bak
    3. os.replace .tmp → .json (atomic on same filesystem)
    """
    tmp_file = STATE_FILE.with_suffix(".json.tmp")

    try:
        # Step 1: Write to temp with fsync
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        # Step 2: Backup current state
        if STATE_FILE.exists():
            try:
                shutil.copy2(str(STATE_FILE), str(STATE_BACKUP))
            except IOError:
                pass  # Backup failure is non-fatal

        # Step 3: Atomic replace
        os.replace(str(tmp_file), str(STATE_FILE))

    except IOError as e:
        logger.error(f"Failed to save state: {type(e).__name__}")
        # Cleanup temp
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except IOError:
            pass


# ──────────────────────────────────────────────────────────────
# 9. Path Traversal Prevention
# ──────────────────────────────────────────────────────────────

def validate_blob_path(blob_name: str, base_dir: Path) -> Path | None:
    """
    Validate blob name is safe — prevents path traversal attacks.
    Returns resolved local path or None if unsafe.
    """
    if not blob_name:
        return None

    # Reject path traversal patterns
    if ".." in blob_name or blob_name.startswith("/") or blob_name.startswith("\\"):
        return None

    # Reject null bytes
    if "\x00" in blob_name:
        return None

    # Reject absolute Windows paths
    if len(blob_name) >= 2 and blob_name[1] == ":":
        return None

    # FIX v2.2: Reject Windows reserved device names in any path component.
    # Files named CON, PRN, NUL, AUX, COM1-9, LPT1-9 (with or without
    # extensions) are special on Windows and can cause unexpected behavior.
    _WIN_RESERVED = frozenset({
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    })
    for component in PurePosixPath(blob_name).parts:
        # Strip extension: "CON.txt" is still reserved on Windows
        stem = component.split(".")[0].upper()
        if stem in _WIN_RESERVED:
            return None

    # Reject symlinks and Windows junctions in path components
    # (prevents following symlinks planted in the download directory)
    parts = PurePosixPath(blob_name).parts
    check_path = base_dir
    for part in parts[:-1]:  # Check directory components only
        if part in (".", ""):
            continue
        check_path = check_path / part
        if check_path.exists():
            if check_path.is_symlink():
                return None
            # Windows junction detection
            if sys.platform == "win32":
                try:
                    import ctypes
                    FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
                    attrs = ctypes.windll.kernel32.GetFileAttributesW(
                        str(check_path)
                    )
                    if attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
                        return None
                except Exception:
                    pass

    # Build local path and verify it's under base_dir
    try:
        local_path = (base_dir / blob_name).resolve()
        base_resolved = base_dir.resolve()

        # Must be under base_dir — append os.sep to prevent
        # /foo matching /foobar (only /foo/ prefix is valid)
        base_prefix = str(base_resolved) + os.sep
        if not (str(local_path) + os.sep).startswith(base_prefix):
            if local_path != base_resolved:
                return None

        return local_path
    except (ValueError, OSError):
        return None


# ──────────────────────────────────────────────────────────────
# 6. Authentication — ADC preferred, SA key fallback,
#    read-only scope
# ──────────────────────────────────────────────────────────────

GCS_READ_ONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"


def create_client(logger):
    """
    Create GCS client with minimal privileges (read-only).
    Auth priority:
    1. Application Default Credentials (ADC) — if gcloud is configured
    2. Service Account Key JSON file — fallback

    FIX 2026-10-24: Enforce read-only scope for SA key auth.
    FIX 2026-10-23: Explicitly pass project=GCP_PROJECT to avoid 404
    when using user-level ADC (authorized_user credentials don't carry
    a project_id field, so storage.Client() gets project=None and GCS
    cannot resolve the bucket).
    """
    # Try ADC first (gcloud auth application-default login)
    try:
        client = storage.Client(project=GCP_PROJECT)
        # Test connectivity
        bucket = client.bucket(BUCKET_NAME)
        bucket.reload()
        logger.info(
            f"Auth: using ADC with project={GCP_PROJECT}"
        )
        return client
    except Exception:
        pass

    # Fallback: Service Account Key — enforce read-only scope
    if KEY_FILE.exists():
        try:
            from google.oauth2 import service_account as _sa
            credentials = _sa.Credentials.from_service_account_file(
                str(KEY_FILE),
                scopes=[GCS_READ_ONLY_SCOPE],
            )
            client = storage.Client(
                project=GCP_PROJECT,
                credentials=credentials,
            )
            logger.info("Auth: using Service Account Key (read-only scope)")
            return client
        except ImportError:
            # FIX v2.2: FAIL-CLOSED — refuse unscoped SA key auth.
            # Without google-auth, we cannot enforce read-only scope,
            # which would grant full GCS access including writes/deletes.
            logger.error(
                "Auth: google-auth package required for SA key auth "
                "(read-only scope enforcement). "
                "Run: pip install google-auth"
            )
            raise ImportError(
                "google-auth required for SA key auth (fail-closed: "
                "refusing unscoped fallback to prevent write access)"
            )
        except Exception as e:
            logger.error(f"SA key auth failed: {type(e).__name__}")
            raise

    raise FileNotFoundError(
        "No authentication available.\n"
        "Option 1: Run 'gcloud auth application-default login'\n"
        f"Option 2: Place SA key at {KEY_FILE}"
    )


# ──────────────────────────────────────────────────────────────
# 4. Verify actual GCS Object Prefixes before mapping
# ──────────────────────────────────────────────────────────────

def discover_prefixes(bucket, logger) -> list[str]:
    """
    List actual top-level prefixes in the GCS bucket and
    validate against expected prefixes.
    Returns list of valid prefixes to sync.
    """
    valid_prefixes = []

    try:
        # List with delimiter to get top-level "directories"
        iterator = bucket.list_blobs(delimiter="/")
        # Consume the iterator to populate prefixes
        _ = list(iterator)
        actual_prefixes = set()
        for prefix in iterator.prefixes:
            # prefix comes as "01_LINE_INBOX/" — strip trailing /
            clean = prefix.rstrip("/")
            actual_prefixes.add(clean)

        for expected in EXPECTED_PREFIXES:
            if expected in actual_prefixes:
                valid_prefixes.append(expected)
            else:
                logger.warning(f"Expected prefix [{expected}] not found in bucket")

        # Log any unexpected prefixes (but don't sync them)
        unexpected = actual_prefixes - set(EXPECTED_PREFIXES)
        if unexpected:
            count = len(unexpected)
            logger.info(f"Found {count} non-mapped prefix(es) in bucket — skipping")

    except Exception as e:
        logger.error(f"Prefix discovery failed: {type(e).__name__}")
        # Fallback to expected prefixes (best effort)
        logger.info("Falling back to expected prefix list")
        valid_prefixes = list(EXPECTED_PREFIXES)

    return valid_prefixes


# ──────────────────────────────────────────────────────────────
# 2 + 3. Download with checksum verification and atomic writes
# ──────────────────────────────────────────────────────────────

def verify_checksum(local_path: Path, blob) -> bool:
    """
    Verify downloaded file integrity against GCS blob metadata.
    Checks: size match + CRC32C checksum (GCS native).
    """
    try:
        local_size = local_path.stat().st_size

        # Size check
        if blob.size is not None and local_size != blob.size:
            return False

        # CRC32C check (GCS always provides this)
        if blob.crc32c:
            crc = 0
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    # CRC32C using standard algorithm
                    crc = _crc32c_update(crc, chunk)

            # GCS returns CRC32C as base64-encoded 4-byte big-endian
            local_crc_bytes = struct.pack(">I", crc)
            local_crc_b64 = base64.b64encode(local_crc_bytes).decode("ascii")

            if local_crc_b64 != blob.crc32c:
                return False

        return True
    except (IOError, OSError):
        return False


def _crc32c_update(crc: int, data: bytes) -> int:
    """
    CRC32C (Castagnoli) with incremental computation support.
    Tries hardware-accelerated implementations first, falls back to software.
    """
    try:
        # Try crc32c package (supports 2-arg: data, initial_crc)
        import crc32c as _crc32c
        return _crc32c.crc32c(data, crc)
    except (ImportError, TypeError):
        pass

    try:
        # google-crc32c: use extend(crc, data) for incremental CRC
        # Note: google_crc32c.value(data) takes only 1 arg (no initial CRC)
        # but extend(crc, data) supports incremental computation
        import google_crc32c
        return google_crc32c.extend(crc, data)
    except (ImportError, AttributeError, TypeError):
        pass

    # Software fallback — CRC32C lookup table
    if not hasattr(_crc32c_update, "_table"):
        table = []
        for i in range(256):
            val = i
            for _ in range(8):
                if val & 1:
                    val = (val >> 1) ^ 0x82F63B78
                else:
                    val >>= 1
            table.append(val)
        _crc32c_update._table = table

    crc ^= 0xFFFFFFFF
    for byte in data:
        crc = (crc >> 8) ^ _crc32c_update._table[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFFFFFF


def download_blob_atomic(blob, local_path: Path, logger) -> bool:
    """
    Download a GCS blob to local path with:
    - Unique temp file name (PID + timestamp) to prevent collisions
    - Atomic rename to final location
    - Size + CRC32C checksum verification
    - Generation-aware (tracks object version)
    - Never overwrites existing files
    """
    if local_path.exists():
        return True  # Already exists — success (no overwrite)

    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Unique temp file to prevent collision between concurrent processes
    tmp_name = f".dl_{os.getpid()}_{int(time.time() * 1000)}{local_path.suffix}.tmp"
    tmp_path = local_path.parent / tmp_name

    for attempt in range(MAX_RETRIES):
        try:
            # Download to temp file
            # FIX v2.2: if_generation_match ensures we download the exact
            # version we listed, not a newer one that appeared in between.
            dl_kwargs = {}
            if blob.generation:
                dl_kwargs["if_generation_match"] = blob.generation
            blob.download_to_filename(str(tmp_path), **dl_kwargs)

            # Verify file is not empty
            if not tmp_path.exists() or tmp_path.stat().st_size == 0:
                logger.warning(
                    f"Empty download: {safe_log_path(blob.name)} "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})"
                )
                _cleanup_tmp(tmp_path)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY_BASE * (2 ** attempt))
                continue

            # Check size limit
            if tmp_path.stat().st_size > MAX_FILE_SIZE:
                logger.warning(
                    f"File exceeds size limit: {safe_log_path(blob.name)} "
                    f"({tmp_path.stat().st_size} bytes)"
                )
                _cleanup_tmp(tmp_path)
                return False  # Don't retry — file is too large

            # 2. Verify checksum
            if not verify_checksum(tmp_path, blob):
                logger.warning(
                    f"Checksum mismatch: {safe_log_path(blob.name)} "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})"
                )
                _cleanup_tmp(tmp_path)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY_BASE * (2 ** attempt))
                continue

            # 3. Atomic no-overwrite move
            # FIX v2.2: os.replace() OVERWRITES existing files — race condition!
            # On Windows, os.rename() raises FileExistsError if target exists,
            # making it a true atomic no-overwrite primitive.
            # On Unix, use os.link() + os.unlink() for the same guarantee.
            try:
                if sys.platform == "win32":
                    os.rename(str(tmp_path), str(local_path))
                else:
                    # os.link() fails atomically if target exists
                    os.link(str(tmp_path), str(local_path))
                    os.unlink(str(tmp_path))
            except FileExistsError:
                # File appeared during download — no-overwrite is correct
                _cleanup_tmp(tmp_path)
                return True
            except OSError as move_err:
                # Cross-volume: check first, then copy
                if local_path.exists():
                    _cleanup_tmp(tmp_path)
                    return True
                try:
                    shutil.copy2(str(tmp_path), str(local_path))
                    _cleanup_tmp(tmp_path)
                except OSError:
                    logger.error(
                        f"Move failed: {safe_log_path(blob.name)} "
                        f"({type(move_err).__name__})"
                    )
                    _cleanup_tmp(tmp_path)
                    return False

            return True

        except gcs_exceptions.NotFound:
            logger.info(f"Object removed during sync: {safe_log_path(blob.name)}")
            _cleanup_tmp(tmp_path)
            return False  # Don't retry

        except gcs_exceptions.PreconditionFailed:
            logger.info(
                f"Object changed during download: {safe_log_path(blob.name)}"
            )
            _cleanup_tmp(tmp_path)
            return False  # Don't retry — generation changed

        except Exception as e:
            delay = RETRY_DELAY_BASE * (2 ** attempt)
            logger.warning(
                f"Download error: {safe_log_path(blob.name)} "
                f"({type(e).__name__}, attempt {attempt + 1}/{MAX_RETRIES}, "
                f"retry in {delay}s)"
            )
            _cleanup_tmp(tmp_path)
            if attempt < MAX_RETRIES - 1:
                time.sleep(delay)

    logger.error(
        f"Failed after {MAX_RETRIES} attempts: {safe_log_path(blob.name)}"
    )
    _cleanup_tmp(tmp_path)
    return False


def _cleanup_tmp(tmp_path: Path):
    """Safely remove temp file."""
    try:
        if tmp_path.exists():
            tmp_path.unlink()
    except IOError:
        pass


def cleanup_stale_temps(base_dir: Path, logger, max_age_hours: int = 2):
    """
    FIX v2.2: Clean up stale .tmp files from interrupted downloads.
    Only removes files matching our temp naming pattern that are older
    than max_age_hours, to avoid deleting active downloads.
    """
    import glob as _glob
    pattern = str(base_dir / "**" / ".dl_*.tmp")
    cutoff = time.time() - (max_age_hours * 3600)
    cleaned = 0
    try:
        for tmp_file in _glob.glob(pattern, recursive=True):
            tmp_path = Path(tmp_file)
            try:
                if tmp_path.stat().st_mtime < cutoff:
                    tmp_path.unlink()
                    cleaned += 1
            except IOError:
                pass
        if cleaned > 0:
            logger.info(f"Cleaned {cleaned} stale temp file(s)")
    except Exception:
        pass  # Non-fatal


# ──────────────────────────────────────────────────────────────
# Core Sync Logic
# ──────────────────────────────────────────────────────────────

def sync_folder(client, bucket, prefix: str, state: dict, logger) -> dict:
    """
    Sync one GCS prefix to local folder.
    Returns stats: {downloaded: int, skipped: int, errors: int}
    """
    stats = {"downloaded": 0, "skipped": 0, "errors": 0}
    local_folder = BASE_DIR / prefix
    local_folder.mkdir(parents=True, exist_ok=True)

    try:
        blobs = list(bucket.list_blobs(prefix=prefix + "/"))
    except Exception as e:
        logger.error(f"Failed to list [{prefix}]: {type(e).__name__}")
        stats["errors"] += 1
        return stats

    for blob in blobs:
        # Skip "directory" markers
        if blob.name.endswith("/"):
            continue

        # Skip internal/metadata files
        basename = PurePosixPath(blob.name).name
        if SKIP_PATTERNS.search(basename):
            continue

        # Check file extension whitelist
        ext = PurePosixPath(blob.name).suffix.lower()
        if ext and ext not in ALLOWED_EXTENSIONS:
            continue

        # 3. Check if already downloaded (by blob name + generation)
        blob_key = f"{blob.name}::g{blob.generation}"
        if blob_key in state.get("downloaded", {}):
            stats["skipped"] += 1
            continue

        # 9. Path traversal prevention
        local_path = validate_blob_path(blob.name, BASE_DIR)
        if local_path is None:
            logger.warning(
                f"Rejected unsafe path: {safe_log_path(blob.name)}"
            )
            stats["errors"] += 1
            continue

        # Skip if local file already exists (never overwrite)
        if local_path.exists():
            state.setdefault("downloaded", {})[blob_key] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": "exists_locally",
            }
            stats["skipped"] += 1
            continue

        # 2 + 3. Download with checksum verification + atomic write
        success = download_blob_atomic(blob, local_path, logger)
        if success and local_path.exists():
            file_size = local_path.stat().st_size
            state.setdefault("downloaded", {})[blob_key] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "size": file_size,
                "gen": blob.generation,
                "status": "verified",
            }
            stats["downloaded"] += 1
            logger.info(
                f"OK: {safe_log_path(blob.name)} ({file_size} bytes, verified)"
            )
        elif not success:
            stats["errors"] += 1

    return stats


# ──────────────────────────────────────────────────────────────
# Main Sync Function
# ──────────────────────────────────────────────────────────────

def run_sync(dry_run: bool = False) -> int:
    """
    Main sync entry point.
    Args:
        dry_run: If True, list files without downloading.
    Returns:
        Exit code: 0=success, 1=fatal error, 2=partial failure, 3=lock held
    """
    logger = setup_logging()

    # 1. Acquire atomic lock with PID checking
    if not acquire_lock(logger):
        return 3

    exit_code = 0
    try:
        logger.info("=" * 50)
        mode_str = " [DRY-RUN]" if dry_run else ""
        logger.info(f"GCS Sync started (v2.2-hardened){mode_str}")

        # FIX v2.2: Clean up stale temp files from interrupted downloads
        cleanup_stale_temps(BASE_DIR, logger)

        # 7. Load state with recovery
        state = load_state(logger)

        # 6. Create client with minimal-privilege auth
        try:
            client = create_client(logger)
            bucket = client.bucket(BUCKET_NAME)
        except FileNotFoundError as e:
            logger.error(safe_error_msg(e))
            exit_code = 1
            return exit_code
        except Exception as e:
            logger.error(f"GCS connection failed: {type(e).__name__}")
            exit_code = 1
            return exit_code

        # 4. Discover and validate actual prefixes
        valid_prefixes = discover_prefixes(bucket, logger)
        if not valid_prefixes:
            logger.error("No valid prefixes found — aborting")
            exit_code = 1
            return exit_code

        logger.info(f"Syncing {len(valid_prefixes)} folder(s)")

        if dry_run:
            # Dry-run: list files only, no downloads
            total_count = 0
            for prefix in valid_prefixes:
                try:
                    blobs = list(bucket.list_blobs(prefix=prefix + "/"))
                    file_blobs = [b for b in blobs if not b.name.endswith("/")]
                    total_count += len(file_blobs)
                    logger.info(
                        f"[DRY-RUN] {prefix}: {len(file_blobs)} object(s)"
                    )
                except Exception as e:
                    logger.error(
                        f"[DRY-RUN] {prefix}: list failed — {type(e).__name__}"
                    )
            logger.info(f"[DRY-RUN] Total: {total_count} object(s) in bucket")
            logger.info("=" * 50)
            return 0

        # Sync each folder
        total_stats = {"downloaded": 0, "skipped": 0, "errors": 0}

        for prefix in valid_prefixes:
            folder_stats = sync_folder(client, bucket, prefix, state, logger)
            for k in total_stats:
                total_stats[k] += folder_stats[k]

            # Save state after each folder (crash recovery)
            state["last_sync"] = datetime.now(timezone.utc).isoformat()
            save_state(state, logger)

        # Update totals
        state["total_downloaded"] = state.get("total_downloaded", 0) + total_stats["downloaded"]
        state["total_errors"] = total_stats["errors"]
        state["last_sync"] = datetime.now(timezone.utc).isoformat()
        save_state(state, logger)

        # Summary
        logger.info(
            f"Sync complete: "
            f"{total_stats['downloaded']} downloaded, "
            f"{total_stats['skipped']} skipped, "
            f"{total_stats['errors']} errors"
        )
        logger.info("=" * 50)

        if total_stats["errors"] > 0:
            exit_code = 2  # Partial failure

    except Exception as e:
        logger.error(f"Sync failed: {type(e).__name__}: {safe_error_msg(e)}")
        exit_code = 1
    finally:
        release_lock(logger)

    return exit_code


def main():
    """CLI entry point — parses --dry-run flag and exits with code."""
    dry_run = "--dry-run" in sys.argv
    exit_code = run_sync(dry_run=dry_run)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
