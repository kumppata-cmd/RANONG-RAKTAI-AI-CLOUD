#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE OA Webhook Server — คลินิกระนอง รักษ์ไต (Cloud Version)
Deploy บน Render Managed Cloud ด้วย Gunicorn

รองรับ: TXT, PDF (ข้อความ + สแกน), JPG, JPEG, PNG

กฎความปลอดภัย:
  - ตรวจสอบ LINE Webhook Signature ทุกครั้ง
  - ตรวจสอบชนิดไฟล์จริง (magic bytes) ก่อนประมวลผล
  - ไม่ลบไฟล์ต้นฉบับ
  - ไม่แก้ไขข้อมูลทางการแพทย์
  - ไม่ส่งข้อมูลผู้ป่วยจริงไปยัง Claude API (เฉพาะเอกสารทดสอบ)

ความแตกต่างจาก Windows version:
  - ใช้ Gunicorn แทน Flask dev server
  - PORT จาก environment variable (Render กำหนดให้)
  - Tesseract path สำหรับ Linux (apt install)
  - Storage abstraction สำหรับ cloud storage (S3/GCS) — ยังไม่เชื่อมต่อจริง
  - ไม่มี ngrok, batch files, Windows-specific code
"""

import fcntl
import os
import secrets
import sys
import hmac
import hashlib
import base64
import json
import logging
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, request, abort, jsonify, redirect
import requests as http_requests

# ─── OCR (Tesseract) ────────────────────────────────────────
try:
    import pytesseract
    # Linux: Tesseract ติดตั้งผ่าน apt อยู่ที่ /usr/bin/tesseract
    _tess_cmd = "/usr/bin/tesseract"
    if os.path.isfile(_tess_cmd):
        pytesseract.pytesseract.tesseract_cmd = _tess_cmd
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import fitz as pymupdf  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

# ─── Config ──────────────────────────────────────────────
# Cloud: ไม่ใช้ dotenv — ตั้งค่า ENV ผ่าน Render Dashboard

# APP_ENV: โหมดการทำงาน
#   "testing"      = ค่าเริ่มต้น — ไม่เรียก Claude API, ไม่ตอบกลับ LINE
#   "testing-docs" = ทดสอบรับเอกสาร — ดาวน์โหลด+จัดเก็บ GCS ได้ แต่ไม่เรียก Claude
#   "production"   = ใช้งานจริง — ต้องตั้งค่าด้วยตนเองใน Render Dashboard
APP_ENV = os.getenv("APP_ENV", "testing").lower().strip()
IS_TESTING = APP_ENV != "production"
IS_PRODUCTION = APP_ENV == "production"
# testing-docs: อนุญาตดาวน์โหลดไฟล์และจัดเก็บ GCS แต่ไม่เรียก Claude API
IS_TESTING_DOCS = APP_ENV == "testing-docs"

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Render กำหนด PORT ให้ — ต้องใช้ค่านี้
PORT = int(os.getenv("PORT", "10000"))

ENABLE_AI_CLASSIFY = os.getenv("ENABLE_AI_CLASSIFY", "true").lower() == "true"

# ─── Storage Abstraction ────────────────────────────────
# Render disk เป็น ephemeral — ไฟล์จะหายเมื่อ redeploy
# ใช้ STORAGE_BACKEND เพื่อเลือก backend:
#   "local"  = เก็บบน disk ชั่วคราว (ค่าเริ่มต้น, สำหรับทดสอบ)
#   "s3"     = Amazon S3 (ต้องตั้ง AWS_* env vars)
#   "gcs"    = Google Cloud Storage (ต้องตั้ง GCS_* env vars)
_storage_backend_env = os.getenv("STORAGE_BACKEND", "local")
# Production: บังคับใช้ GCS เท่านั้น — ถ้าไม่ได้ตั้ง STORAGE_BACKEND จะใช้ gcs อัตโนมัติ
if IS_PRODUCTION and _storage_backend_env == "local":
    STORAGE_BACKEND = "gcs"
else:
    STORAGE_BACKEND = _storage_backend_env
# รองรับทั้ง GCS_BUCKET_NAME (แนะนำ) และ STORAGE_BUCKET (backward-compatible)
STORAGE_BUCKET = os.getenv("GCS_BUCKET_NAME") or os.getenv("STORAGE_BUCKET", "")

# โฟลเดอร์ชั่วคราวบน Render (ephemeral)
BASE_DIR = os.getenv("BASE_DIR", "/tmp/ranong_raktai")

# ─── ข้อจำกัดไฟล์ ────────────────────────────────────────
MAX_FILE_SIZE = 20 * 1024 * 1024      # 20 MB
# ขนาดขั้นต่ำเฉพาะไฟล์ binary — ไฟล์จริงของรูปแบบเหล่านี้ไม่เล็กกว่านี้
# ไม่ใช้กับ .txt เพราะบันทึกข้อความสั้น ๆ เป็นเอกสารที่ถูกต้องได้
MIN_BINARY_FILE_SIZE = 100
MAX_PDF_PAGES = 50
MAX_PDF_PAGES_VISION = 5
ALLOWED_EXTENSIONS = {".txt", ".pdf", ".jpg", ".jpeg", ".png"}

# ความยาวสูงสุดของชื่อไฟล์ขาเข้าที่ยอมรับก่อนตัดทิ้ง
# (ชื่อไฟล์จากผู้ใช้ไม่ถูกนำไปใช้เป็นชื่อจัดเก็บแล้ว — ดู Phase 1 §2
#  แต่ยังจำกัดไว้เพื่อกัน input ที่ยาวผิดปกติ)
MAX_FILENAME_LENGTH = 200

FOLDERS = {
    "inbox":    "01_LINE_INBOX",
    "bills":    "02_BILLS_RECEIPTS",
    "rx":       "03_PRESCRIPTIONS",
    "lab":      "04_LAB_RESULTS",
    "dialysis": "05_DIALYSIS_RECORDS",
    "review":   "06_AI_REVIEW",
}

# ─── AI ภายนอก — ล็อกที่ระดับโค้ด ────────────────────────
# ข้อมูลผู้ป่วยต้องไม่ถูกส่งออกนอกระบบ จึงไม่อ่านค่านี้จาก ENV โดยเจตนา
# การเปลี่ยนค่าต้องแก้โค้ด + review + commit เท่านั้น
# ENABLE_AI_CLASSIFY ถูกเพิกเฉยสำหรับเส้นทาง AI ภายนอก
EXTERNAL_AI_CLASSIFICATION_ALLOWED = False

# ─── Document Search V1 ────────────────────────────────────
# Allowlist: LINE user IDs (hashed) ที่อนุญาตให้ค้นหา/เรียกดูเอกสาร
# ตั้งค่าใน Render Dashboard: AUTHORIZED_USERS=hash1,hash2,...
# ถ้าไม่ตั้ง = ปิดระบบค้นหาสำหรับทุกคน (ปลอดภัย)
_authorized_env = os.getenv("AUTHORIZED_USERS", "").strip()
AUTHORIZED_USERS: set[str] = {
    u.strip() for u in _authorized_env.split(",") if u.strip()
}

# Admin secret สำหรับ backfill endpoint
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

# จำนวนผลค้นหาสูงสุดต่อครั้ง
SEARCH_MAX_RESULTS = 5

# Signed URL expiry (วินาที) — สั้นเพื่อความปลอดภัย
SIGNED_URL_EXPIRY = 900  # 15 นาที

# OCR text extraction ขีดจำกัด (ตัวอักษร) สำหรับเก็บใน index
MAX_INDEX_TEXT_LENGTH = 5000

# GCS paths for index/audit data
INDEX_OBJECT_PATH = "_index/documents.jsonl"  # legacy — อ่านอย่างเดียว (backward compat)
INDEX_DOC_PREFIX = "_index/docs/"  # per-document JSON files (ใช้แทน JSONL)
AUDIT_PREFIX = "_audit/"
INDEX_CACHE_TTL = 5  # วินาที — reload index จาก GCS ทุก 5 วินาที (multi-worker safe)

# ─── Lab Extraction V1 ────────────────────────────────────
# GCS path สำหรับเก็บผล Lab ที่ดึงจากเอกสาร
LAB_RESULTS_PREFIX = "_lab_results/"  # _lab_results/{doc_id}.json
CLINICAL_ANALYSIS_PREFIX = "_clinical_analysis/"  # _clinical_analysis/{doc_id}.json

# Admin users — ใช้สำหรับยืนยันผลแล็บ
# ตั้งค่าใน Render Dashboard: ADMIN_USERS=hash1,hash2,...
_admin_env = os.getenv("ADMIN_USERS", "").strip()
ADMIN_USERS: set[str] = {
    u.strip() for u in _admin_env.split(",") if u.strip()
}

# ─── OCR ─────────────────────────────────────────────────
# 200 DPI เพียงพอสำหรับ Tesseract กับข้อความขนาดปกติ และใช้หน่วยความจำ
# น้อยกว่า 300 DPI ราว 56% (A4: ~26 MB → ~11.6 MB ต่อหน้า)
OCR_DPI = int(os.getenv("OCR_DPI", "200"))
# จำกัดงาน OCR ที่ทำพร้อมกันต่อ worker — ป้องกัน OOM บน instance เล็ก
OCR_MAX_CONCURRENT = int(os.getenv("OCR_MAX_CONCURRENT", "1"))

# ─── Rate Limiting / Deduplication ───────────────────────
# Phase 1: เตรียมโค้ดไว้ ยังไม่เชื่อมบริการ Redis จริง
# ถ้า REDIS_URL ว่าง (ค่าเริ่มต้น) ระบบใช้ in-memory backend ต่อ worker
REDIS_URL = os.getenv("REDIS_URL", "").strip()

RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "5"))
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "30"))
RATE_LIMIT_PER_DAY = int(os.getenv("RATE_LIMIT_PER_DAY", "100"))

DEDUP_TTL_SECONDS = int(os.getenv("DEDUP_TTL_SECONDS", "86400"))      # 24 ชม.
DEDUP_CONTENT_TTL_SECONDS = int(os.getenv("DEDUP_CONTENT_TTL", "600"))  # 10 นาที

# Salt สำหรับ hash LINE User ID ก่อนใช้เป็น key
# *** ต้องตั้ง USER_ID_SALT ใน Render Dashboard ***
# ถ้าไม่ตั้ง → hash เปลี่ยนทุก restart → AUTHORIZED_USERS ใช้งานไม่ได้
# สร้างค่าด้วย: python3 -c "import uuid; print(uuid.uuid4().hex)"
_user_id_salt_env = os.getenv("USER_ID_SALT", "").strip()
_USER_ID_SALT_MISSING = False  # Flag สำหรับ fail-closed ถ้า salt ไม่ได้ตั้ง
if IS_PRODUCTION and not _user_id_salt_env:
    # Production ต้องมี salt คงที่ — ห้ามใช้ค่า default
    # ไม่ crash แต่ fail-closed: ปฏิเสธทุก request ที่ต้องใช้ hash
    _USER_ID_SALT_MISSING = True
    USER_ID_SALT = "MISSING-SALT-DO-NOT-USE"
    logger.critical(
        "CRITICAL: USER_ID_SALT ต้องตั้งค่าใน Production ENV — "
        "ระบบจะปฏิเสธทุก request จนกว่าจะตั้งค่า — "
        "ใช้ค่าสุ่มที่ไม่ซ้ำ เช่น: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    )
elif not _user_id_salt_env:
    # Non-production: ใช้ random salt (เปลี่ยนทุก restart — ปลอดภัยสำหรับ testing)
    USER_ID_SALT = uuid.uuid4().hex
else:
    USER_ID_SALT = _user_id_salt_env

# ─── One-Time Enrollment Token ─────────────────────────────
ENROLLMENT_TOKEN_TTL = 300       # 5 นาที
ENROLLMENT_TOKEN_LENGTH = 32     # 32 bytes → 64 hex chars
ENROLLMENT_MAX_FAILED = 5        # จำกัดครั้งที่พิมพ์ token ผิดต่อ user ต่อชั่วโมง

# ─── Logging ─────────────────────────────────────────────
log_dir = os.path.join(BASE_DIR, "logs")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(stream=sys.stderr),
        logging.FileHandler(
            os.path.join(log_dir, f"server_{datetime.now():%Y%m%d}.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)

# ─── Flask App ───────────────────────────────────────────
app = Flask(__name__)


# ═══════════════════════════════════════════════════════════
# One-Time Enrollment Token Store
# ═══════════════════════════════════════════════════════════

class EnrollmentTokenStore:
    """จัดการ One-Time Enrollment Token สำหรับลงทะเบียนผู้ดูแล.

    ใช้ file-based storage + fcntl.flock เพื่อแชร์ token ข้าม Gunicorn workers
    บน Render instance เดียวกัน — ป้องกันปัญหา token สร้างใน worker A
    แต่ validate ใน worker B

    - Token สุ่มด้วย secrets.token_hex (cryptographically secure)
    - อายุ 5 นาที, ใช้ได้ครั้งเดียว (atomic single-use consumption)
    - ป้องกัน replay attack: ใช้แล้วลบทันที
    - Rate limiting: จำกัดจำนวนครั้งที่พิมพ์ token ผิดต่อ user ต่อชั่วโมง
    - Token ไม่ถูกบันทึกลง log
    - Restart-safe: token หมดอายุตามธรรมชาติ (5 นาที TTL)
    """

    def __init__(self, store_path: str | None = None) -> None:
        self._store_path = store_path or os.path.join(
            tempfile.gettempdir(), "ranong_enrollment_tokens.json"
        )
        # Fallback in-process lock สำหรับ thread safety ภายใน worker
        self._thread_lock = threading.Lock()

    def _read_store(self, fd: int) -> dict:
        """อ่าน store จากไฟล์ — เรียกภายใต้ file lock เท่านั้น."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            data = os.read(fd, 1024 * 1024)  # max 1 MB
            if not data:
                return {"tokens": {}, "failed_attempts": {}}
            return json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"tokens": {}, "failed_attempts": {}}

    def _write_store(self, fd: int, store: dict) -> None:
        """เขียน store ลงไฟล์ — เรียกภายใต้ file lock เท่านั้น."""
        raw = json.dumps(store, separators=(",", ":")).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, raw)

    def _cleanup_expired(self, store: dict, now: float) -> None:
        """ลบ token หมดอายุ และ failed attempts เก่ากว่า 1 ชั่วโมง."""
        # ลบ token หมดอายุ
        expired = [
            t for t, info in store["tokens"].items()
            if now - info["created_at"] > ENROLLMENT_TOKEN_TTL
        ]
        for t in expired:
            del store["tokens"][t]
        # ลบ failed attempts เก่ากว่า 1 ชั่วโมง
        for user_hash in list(store["failed_attempts"].keys()):
            attempts = store["failed_attempts"][user_hash]
            fresh = [ts for ts in attempts if now - ts < 3600]
            if fresh:
                store["failed_attempts"][user_hash] = fresh
            else:
                del store["failed_attempts"][user_hash]

    def _with_lock(self, operation):
        """เปิดไฟล์ + exclusive lock → ทำ operation → ปิด.

        ใช้ fcntl.flock (LOCK_EX) เพื่อ atomic access ข้าม worker.
        """
        with self._thread_lock:
            fd = os.open(
                self._store_path,
                os.O_RDWR | os.O_CREAT,
                0o600,  # owner-only permissions
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    store = self._read_store(fd)
                    result = operation(store)
                    self._write_store(fd, store)
                    return result
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def generate_token(self) -> str:
        """สร้าง token ใหม่ — เรียกจาก admin endpoint เท่านั้น."""
        token = secrets.token_hex(ENROLLMENT_TOKEN_LENGTH)

        def _op(store: dict) -> str:
            now = time.time()
            self._cleanup_expired(store, now)
            store["tokens"][token] = {"created_at": now}
            return token

        self._with_lock(_op)
        return token

    def validate_and_consume(self, token: str, user_hash: str) -> str | None:
        """ตรวจสอบ token — atomic single-use consumption ข้าม workers.

        คืน "VALID" หาก token ถูกต้อง
        คืน "RATE_LIMITED" หาก rate-limited
        คืน None หาก token ไม่ถูกต้อง/หมดอายุ/ใช้แล้ว
        """
        def _op(store: dict) -> str | None:
            now = time.time()
            self._cleanup_expired(store, now)

            # ── Rate limit check ──
            attempts = store["failed_attempts"].get(user_hash, [])
            if len(attempts) >= ENROLLMENT_MAX_FAILED:
                return "RATE_LIMITED"

            # ── ตรวจ token ──
            entry = store["tokens"].get(token)
            if entry is None:
                # token ไม่มี/หมดอายุ → นับ failed attempt
                attempts.append(now)
                store["failed_attempts"][user_hash] = attempts
                return None

            # ── Token ถูกต้อง → ลบทันที (atomic single-use) ──
            del store["tokens"][token]
            return "VALID"

        return self._with_lock(_op)

    def get_active_count(self) -> int:
        """จำนวน token ที่ยังใช้ได้ (สำหรับ testing)."""
        def _op(store: dict) -> int:
            now = time.time()
            self._cleanup_expired(store, now)
            return len(store["tokens"])

        return self._with_lock(_op)


# Singleton instance
enrollment_store = EnrollmentTokenStore()


# ═══════════════════════════════════════════════════════════
# Storage Abstraction Layer
# ═══════════════════════════════════════════════════════════

class StorageLocal:
    """เก็บไฟล์บน local disk (ephemeral บน Render)."""

    is_ready = False  # Local storage ไม่ถือว่า "พร้อม" สำหรับ Document Search

    def save(
        self,
        data: bytes,
        category: str,
        filename: str,
        metadata: dict | None = None,
    ) -> str:
        # metadata ไม่ถูกใช้บน local disk — รับไว้ให้ interface ตรงกับ GCS
        folder = os.path.join(BASE_DIR, FOLDERS.get(category, FOLDERS["review"]))
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, filename)
        if os.path.exists(dest):
            name, ext = os.path.splitext(filename)
            dest = os.path.join(folder, f"{name}_{datetime.now():%Y%m%d_%H%M%S}{ext}")
        with open(dest, "wb") as f:
            f.write(data)
        return dest

    def list_files(self, category: str) -> list[str]:
        folder = os.path.join(BASE_DIR, FOLDERS.get(category, FOLDERS["review"]))
        if not os.path.exists(folder):
            return []
        return os.listdir(folder)


class StorageS3:
    """เก็บไฟล์บน Amazon S3 — ยังไม่เชื่อมต่อจริง (placeholder)."""

    def __init__(self):
        self.bucket = STORAGE_BUCKET
        logger.info(f"S3 Storage: bucket={self.bucket} (ยังไม่เชื่อมต่อ)")

    def save(
        self,
        data: bytes,
        category: str,
        filename: str,
        metadata: dict | None = None,
    ) -> str:
        # TODO: เชื่อมต่อ boto3 เมื่อพร้อม
        # import boto3
        # s3 = boto3.client('s3')
        # key = f"{FOLDERS.get(category, FOLDERS['review'])}/{filename}"
        # s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        # return f"s3://{self.bucket}/{key}"
        logger.warning("S3 Storage ยังไม่เชื่อมต่อ — fallback ไป local")
        return StorageLocal().save(data, category, filename, metadata)

    def list_files(self, category: str) -> list[str]:
        logger.warning("S3 Storage ยังไม่เชื่อมต่อ")
        return []


class StorageGCS:
    """เก็บไฟล์บน Google Cloud Storage.

    Authentication (ลำดับความสำคัญ):
      1. GOOGLE_APPLICATION_CREDENTIALS — path ไปที่ Service Account JSON Key
         (Application Default Credentials — ADC, แนะนำสำหรับ Render)
      2. GCS_CREDENTIALS_BASE64 — Base64 ของ Service Account JSON Key
         (backward-compatible)
      3. ถ้าไม่มีทั้งสอง → google-cloud-storage จะลอง ADC อื่น ๆ
         (เช่น Workload Identity, metadata server)

    Bucket name: อ่านจาก GCS_BUCKET_NAME หรือ STORAGE_BUCKET

    ถ้า STORAGE_BACKEND=gcs แต่เชื่อมต่อ GCS ไม่ได้ → ERROR ชัดเจน
    ห้าม fallback ไป local แบบเงียบ ๆ เพราะจะทำให้เอกสารหายตอน redeploy
    """

    # Content-Type mapping สำหรับไฟล์ที่ระบบรองรับ
    CONTENT_TYPES = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".txt": "text/plain; charset=utf-8",
    }

    def __init__(self):
        self.bucket_name = STORAGE_BUCKET
        self._client = None
        self._bucket = None
        self._fallback = StorageLocal()
        self._init_error = None
        self._auth_method = None  # "adc", "base64", "default"

        if not self.bucket_name:
            self._init_error = (
                "GCS_BUCKET_NAME หรือ STORAGE_BUCKET ไม่ได้ตั้งค่า"
            )
            logger.error(f"GCS Storage: {self._init_error}")
            return

        try:
            from google.cloud import storage as gcs_storage
        except ImportError:
            self._init_error = "google-cloud-storage ไม่ได้ติดตั้ง"
            logger.error(f"GCS Storage: {self._init_error}")
            return

        try:
            # ลำดับ 1: GOOGLE_APPLICATION_CREDENTIALS (ADC — path ไปที่ JSON Key)
            adc_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
            creds_b64 = os.getenv("GCS_CREDENTIALS_BASE64", "").strip()

            if adc_path:
                # ADC: google-cloud-storage ใช้ GOOGLE_APPLICATION_CREDENTIALS อัตโนมัติ
                self._client = gcs_storage.Client()
                self._auth_method = "adc"
                logger.info("GCS Storage: ใช้ Application Default Credentials (ADC)")
            elif creds_b64:
                # ลำดับ 2: Base64-encoded Service Account JSON Key
                from google.oauth2 import service_account
                creds_json = json.loads(base64.b64decode(creds_b64))
                credentials = service_account.Credentials.from_service_account_info(
                    creds_json
                )
                self._client = gcs_storage.Client(
                    credentials=credentials,
                    project=creds_json.get("project_id"),
                )
                self._auth_method = "base64"
                logger.info("GCS Storage: ใช้ GCS_CREDENTIALS_BASE64")
            else:
                # ลำดับ 3: ลอง ADC อื่น ๆ (Workload Identity, metadata server)
                self._client = gcs_storage.Client()
                self._auth_method = "default"
                logger.info("GCS Storage: ลอง default credentials")

            self._bucket = self._client.bucket(self.bucket_name)

            # ตรวจสอบการเชื่อมต่อจริงตอน init — ไม่รอ health check
            # ใช้ list_blobs(max_results=1) เพราะต้องการแค่ storage.objects.list
            try:
                _ = list(self._client.list_blobs(self.bucket_name, max_results=1))
                logger.info(
                    f"GCS Storage: bucket={self.bucket_name}, "
                    f"auth={self._auth_method} — เชื่อมต่อสำเร็จ (verified)"
                )
            except Exception as verify_err:
                # Credential หรือ permission มีปัญหา — เก็บ error แต่ยังไม่ล้ม
                # เพราะอาจเป็น transient error ตอน startup
                self._init_error = f"GCS verify failed: {type(verify_err).__name__}"
                self._client = None
                self._bucket = None
                logger.error(
                    f"GCS Storage: bucket={self.bucket_name}, "
                    f"auth={self._auth_method} — {self._init_error}"
                )
        except Exception as e:
            self._init_error = f"GCS init error: {type(e).__name__}"
            logger.error(f"GCS Storage: {self._init_error}")

    @property
    def is_ready(self) -> bool:
        return self._client is not None and self._bucket is not None

    def _get_content_type(self, filename: str) -> str:
        ext = os.path.splitext(filename)[1].lower()
        return self.CONTENT_TYPES.get(ext, "application/octet-stream")

    def save(
        self,
        data: bytes,
        category: str,
        filename: str,
        metadata: dict | None = None,
    ) -> str:
        """อัปโหลดไฟล์ไป GCS.

        Production mode: ถ้า GCS ไม่พร้อมหรือ upload ล้มเหลว → raise RuntimeError
        Testing mode: fallback ไป local storage พร้อม ERROR log

        filename ที่ส่งเข้ามาควรเป็นชื่อแบบ UUID ที่สร้างตอนรับไฟล์
        (ดู build_storage_filename) — ไม่ใช่ชื่อไฟล์ต้นฉบับจากผู้ใช้

        metadata ต้องไม่มีข้อมูลส่วนบุคคล — ดู build_object_metadata
        """
        # ป้องกัน path traversal (ต้องทำก่อน is_ready check เพราะ fallback ก็ต้องใช้ชื่อที่ปลอดภัย)
        filename = os.path.basename(filename)
        if not filename:
            filename = f"unnamed_{datetime.now():%Y%m%d_%H%M%S}.dat"

        if not self.is_ready:
            err_msg = (
                f"GCS ไม่พร้อม ({self._init_error}) — "
                f"ไม่สามารถจัดเก็บเอกสารได้"
            )
            logger.error(err_msg)
            if IS_PRODUCTION:
                raise RuntimeError(err_msg)
            # Testing mode: fallback ไป local พร้อมแจ้งเตือน
            logger.error(
                "เอกสารจะเก็บบน local disk ชั่วคราว (หายเมื่อ redeploy)"
            )
            return self._fallback.save(data, category, filename)

        folder = FOLDERS.get(category, FOLDERS["review"])
        # แบ่งตามเดือน เพื่อไม่ให้โฟลเดอร์เดียวสะสมไฟล์จนเปิดช้า
        blob_path = f"{folder}/{datetime.now():%Y-%m}/{filename}"

        content_type = self._get_content_type(filename)

        try:
            blob = self._bucket.blob(blob_path)
            if metadata:
                blob.metadata = metadata
            # if_generation_match=0 → เขียนได้เฉพาะเมื่อ object ยังไม่มี
            # เป็นการกันเขียนทับแบบ atomic ฝั่ง GCS ซึ่งทำงานแม้ dedup layer ล่ม
            blob.upload_from_string(
                data, content_type=content_type, if_generation_match=0
            )
            gcs_uri = f"gs://{self.bucket_name}/{blob_path}"
            # ไม่ log ชื่อไฟล์ต้นฉบับ — ชื่อ object เป็น UUID จึงไม่ระบุตัวบุคคล
            logger.info(
                f"GCS upload สำเร็จ: {folder} ({len(data):,} bytes, {content_type})"
            )
            return gcs_uri
        except Exception as e:
            # 412 Precondition Failed = object นี้มีอยู่แล้ว → ถือว่าจัดเก็บสำเร็จ
            # (เป็นผลจากการส่งซ้ำ ไม่ใช่ความผิดพลาด) จึงไม่ fallback ไป local
            if getattr(e, "code", None) == 412:
                gcs_uri = f"gs://{self.bucket_name}/{blob_path}"
                logger.info(f"GCS: object มีอยู่แล้ว ไม่เขียนทับ ({folder})")
                return gcs_uri
            # ไม่ log str(e) — ข้อความ error ของ GCS อาจมี blob path
            logger.error(
                f"GCS upload ล้มเหลว: {type(e).__name__}"
            )
            if IS_PRODUCTION:
                raise RuntimeError(
                    f"GCS upload ล้มเหลว — ไม่สามารถจัดเก็บเอกสารได้"
                )
            # Testing mode: fallback ไป local
            logger.error("fallback ไป local storage")
            return self._fallback.save(data, category, filename)

    def list_files(self, category: str) -> list[str]:
        """แสดงรายการไฟล์ใน GCS bucket ตามประเภท."""
        if not self.is_ready:
            return []

        folder = FOLDERS.get(category, FOLDERS["review"])
        prefix = f"{folder}/"

        try:
            blobs = self._client.list_blobs(self.bucket_name, prefix=prefix)
            return [blob.name.split("/")[-1] for blob in blobs if blob.name != prefix]
        except Exception as e:
            logger.error(f"GCS list_files ล้มเหลว: {type(e).__name__}")
            return []

    def check_connection(self) -> dict:
        """ตรวจสอบการเชื่อมต่อ GCS — ใช้ใน health check.

        ใช้ list_blobs(max_results=1) แทน bucket.reload() เพราะ:
        - bucket.reload() ต้องการ storage.buckets.get (ซึ่ง objectAdmin ไม่มี)
        - list_blobs ใช้แค่ storage.objects.list ซึ่ง objectAdmin มีอยู่แล้ว
        - ตรวจสอบ credential + bucket + network ได้ครบในคำสั่งเดียว
        """
        if not self.is_ready:
            return {"status": "not_ready", "error": self._init_error}
        try:
            # list 1 object — ใช้ permission น้อยที่สุด (storage.objects.list)
            # iterator ยังไม่ fetch จนกว่าจะ iterate
            blobs = self._client.list_blobs(self.bucket_name, max_results=1)
            # Force API call by consuming iterator
            _ = list(blobs)
            result = {"status": "connected", "bucket": self.bucket_name}
            # ตรวจ per-doc index architecture:
            # - storage.objects.create (Object Creator) → เขียน per-doc files
            # - storage.objects.list (Object Viewer) → list blobs (ตรวจด้านบนแล้ว)
            # - storage.objects.get (Object Viewer) → อ่าน per-doc files
            # ทดสอบ objects.get ด้วย blob.reload() บน test blob
            try:
                # ทดสอบ objects.get — ลองอ่าน legacy index หรือ docs prefix
                test_blob = self._bucket.blob(INDEX_OBJECT_PATH)
                test_blob.reload()  # ต้องใช้ storage.objects.get
                result["index_writable"] = True
                result["index_architecture"] = "per-document"
            except Exception as perm_err:
                perm_code = getattr(perm_err, "code", None)
                if perm_code == 403:
                    result["index_writable"] = False
                    result["permission_warning"] = (
                        "storage.objects.get missing — "
                        "document index อ่านไม่ได้"
                    )
                    logger.warning(
                        "GCS permission gap: storage.objects.get "
                        "ไม่มี — document index read จะล้มเหลว"
                    )
                elif perm_code == 404:
                    # legacy index ไม่มี — ลอง list per-doc files แทน
                    result["index_writable"] = True
                    result["index_architecture"] = "per-document"
                # อื่นๆ ไม่บล็อก health check
            return result
        except Exception as e:
            # ไม่เปิดเผย exception detail — อาจมี internal GCS URL หรือ project ID
            logger.error(f"GCS check_connection ล้มเหลว: {type(e).__name__}")
            return {"status": "error", "error": type(e).__name__}


def get_storage():
    """สร้าง storage instance ตาม STORAGE_BACKEND."""
    if STORAGE_BACKEND == "s3":
        return StorageS3()
    elif STORAGE_BACKEND == "gcs":
        return StorageGCS()
    return StorageLocal()


storage = get_storage()


# ═══════════════════════════════════════════════════════════
# Document Index — GCS-based persistent index
#
# เก็บ metadata ของเอกสารทั้งหมดเป็น JSONL ใน GCS bucket
# โหลดเข้า memory ตอน startup, append เมื่อรับเอกสารใหม่
# ค้นหาแบบ substring match ใน memory — เพียงพอสำหรับคลินิก
#
# ไม่ใช้ local SQLite เพราะ Render filesystem เป็น ephemeral
# ไม่เพิ่ม database service ภายนอก — ใช้ GCS ที่มีอยู่แล้ว
# ค่าใช้จ่ายเพิ่ม: $0 (ใช้ GCS bucket เดิม)
#
# สำหรับคลินิกที่มีเอกสาร < 50,000 ชิ้น index ≈ 50 MB ใน memory
# ═══════════════════════════════════════════════════════════

class DocumentIndex:
    """Persistent document index stored in GCS as per-document JSON files.

    สถาปัตยกรรม: ใช้ per-document JSON files แทน single JSONL เพื่อ:
    - ไม่ต้องใช้ storage.objects.delete (ซึ่ง Object Creator + Viewer ไม่มี)
    - ไม่มี concurrent append conflict (แต่ละ doc เป็นไฟล์แยก)
    - ใช้แค่ storage.objects.create + list + get (least privilege)

    แต่ละเอกสารเก็บเป็น _index/docs/{doc_id}.json
    อ่าน legacy _index/documents.jsonl ด้วย (backward compatibility)

    ออกแบบสำหรับ Gunicorn multi-worker:
    - ทุก read operation โหลดจาก GCS ใหม่ผ่าน short-lived cache
    - add() เขียน GCS ก่อน แล้วจึงอัพเดท memory
    - ป้องกัน duplicate ด้วย if_generation_match=0 (create-only)
    """

    def __init__(self, storage_instance):
        self._storage = storage_instance
        self._docs: list[dict] = []
        self._doc_map: dict[str, dict] = {}  # doc_id -> doc
        self._lock = threading.Lock()
        self._cache_ts: float = 0  # timestamp ที่ load cache ล่าสุด
        self._cache_ttl: float = INDEX_CACHE_TTL

    def _reload_from_gcs(self) -> bool:
        """โหลด index จาก GCS เข้า memory ใหม่ทั้งหมด.

        อ่านจาก 2 แหล่ง:
        1. Legacy _index/documents.jsonl (backward compatibility)
        2. Per-document files _index/docs/{doc_id}.json

        Deduplicate ด้วย doc_id — per-doc files มีความสำคัญกว่า legacy

        Return True ถ้าโหลดสำเร็จ
        """
        if not hasattr(self._storage, '_client') or not self._storage.is_ready:
            return False
        try:
            new_docs = []
            new_map = {}

            # 1. อ่าน legacy JSONL (backward compatibility)
            try:
                legacy_blob = self._storage._bucket.blob(INDEX_OBJECT_PATH)
                if legacy_blob.exists():
                    content = legacy_blob.download_as_text(encoding="utf-8")
                    for line in content.strip().split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            doc = json.loads(line)
                            doc_id = doc.get("document_id", "")
                            if doc_id and doc_id not in new_map:
                                new_docs.append(doc)
                                new_map[doc_id] = doc
                        except json.JSONDecodeError:
                            continue
            except Exception as legacy_err:
                err_code = getattr(legacy_err, "code", None)
                if err_code == 403:
                    logger.warning(
                        "Legacy index JSONL อ่านไม่ได้ (403) — "
                        "ข้ามไป ใช้ per-doc files แทน"
                    )
                elif err_code != 404:
                    logger.warning(
                        f"Legacy index JSONL อ่านไม่ได้: "
                        f"{type(legacy_err).__name__} (code={err_code})"
                    )

            # 2. อ่าน per-document JSON files
            try:
                blobs = self._storage._client.list_blobs(
                    self._storage.bucket_name,
                    prefix=INDEX_DOC_PREFIX,
                )
                for blob in blobs:
                    try:
                        content = blob.download_as_text(encoding="utf-8")
                        doc = json.loads(content)
                        doc_id = doc.get("document_id", "")
                        if doc_id:
                            if doc_id in new_map:
                                # per-doc file overrides legacy entry
                                old_doc = new_map[doc_id]
                                idx = new_docs.index(old_doc)
                                new_docs[idx] = doc
                            else:
                                new_docs.append(doc)
                            new_map[doc_id] = doc
                    except (json.JSONDecodeError, Exception) as blob_err:
                        logger.warning(
                            f"Per-doc index file อ่านไม่ได้: "
                            f"{blob.name} — {type(blob_err).__name__}"
                        )
                        continue
            except Exception as list_err:
                err_code = getattr(list_err, "code", None)
                if err_code == 403:
                    logger.error(
                        "Per-doc index list ล้มเหลว: Forbidden (403) — "
                        "service account ขาด storage.objects.list permission"
                    )
                else:
                    logger.error(
                        f"Per-doc index list ล้มเหลว: "
                        f"{type(list_err).__name__} (code={err_code})"
                    )
                # ถ้า list ล้มเหลว แต่ legacy สำเร็จ ก็ยังใช้ legacy data ได้

            with self._lock:
                self._docs = new_docs
                self._doc_map = new_map
                self._cache_ts = time.time()
            return True
        except Exception as e:
            err_code = getattr(e, "code", None)
            logger.error(
                f"Document index reload ล้มเหลว: "
                f"{type(e).__name__} (code={err_code})"
            )
            return False

    def _ensure_fresh(self):
        """ตรวจสอบว่า cache ยังใหม่อยู่ ถ้าหมดอายุให้โหลดใหม่จาก GCS."""
        now = time.time()
        if now - self._cache_ts > self._cache_ttl:
            self._reload_from_gcs()

    def add(self, doc_meta: dict) -> bool:
        """เพิ่มเอกสารใหม่ลง index — เขียน per-doc JSON file ใน GCS.

        ใช้ if_generation_match=0 → สร้างไฟล์ใหม่เท่านั้น (ไม่ overwrite)
        ต้องการแค่ storage.objects.create — ไม่ต้องใช้ objects.delete

        ถ้า GCS เขียนไม่ได้ → raise RuntimeError
        Return True ถ้าเพิ่มสำเร็จ, False ถ้า idempotent (มีอยู่แล้ว)
        """
        doc_id = doc_meta.get("document_id", "")

        # Idempotent check — โหลด fresh ก่อนเช็ค
        self._ensure_fresh()
        with self._lock:
            if doc_id in self._doc_map:
                return False  # มีอยู่แล้ว

        # เขียน GCS ก่อน — ถ้าล้มเหลวไม่เพิ่มเข้า memory
        if not self._storage.is_ready:
            raise RuntimeError(
                "Document index: GCS ไม่พร้อม — ไม่สามารถบันทึก metadata"
            )

        # สร้าง per-document JSON file: _index/docs/{doc_id}.json
        doc_json = json.dumps(doc_meta, ensure_ascii=False)
        blob_path = f"{INDEX_DOC_PREFIX}{doc_id}.json"
        try:
            blob = self._storage._bucket.blob(blob_path)
            blob.upload_from_string(
                doc_json,
                content_type="application/json",
                if_generation_match=0,  # create-only — ไม่ overwrite
            )
        except Exception as e:
            err_code = getattr(e, "code", None)
            if err_code == 412:
                # ไฟล์มีอยู่แล้ว — idempotent (worker อื่นสร้างไปก่อน)
                logger.info(
                    f"Document index: doc_id={doc_id[:8]}… "
                    f"มีใน GCS แล้ว (idempotent, 412)"
                )
                self._reload_from_gcs()
                return False
            elif err_code == 403:
                logger.error(
                    "Document index GCS write ล้มเหลว: Forbidden (403) — "
                    "service account ขาด storage.objects.create permission"
                )
                raise RuntimeError(
                    "Document index: GCS permission denied (403) — "
                    "ตรวจสอบว่า service account มี "
                    "Storage Object Creator role"
                )
            else:
                logger.error(
                    f"Document index GCS write ล้มเหลว: "
                    f"{type(e).__name__} (code={err_code})"
                )
                raise RuntimeError(
                    f"Document index: บันทึก metadata ลง GCS ล้มเหลว: "
                    f"{type(e).__name__}"
                )

        logger.info(
            f"Document index: เพิ่ม doc_id={doc_id[:8]}… "
            f"ลง GCS สำเร็จ ({blob_path})"
        )

        # GCS เขียนสำเร็จ → อัพเดท memory cache
        with self._lock:
            if doc_id not in self._doc_map:
                self._docs.append(doc_meta)
                self._doc_map[doc_id] = doc_meta
            self._cache_ts = time.time()
        return True

    def get(self, doc_id: str) -> dict | None:
        """ดึงเอกสารตาม document_id."""
        self._ensure_fresh()
        return self._doc_map.get(doc_id)

    def recent(self, n: int = 5) -> list[dict]:
        """เอกสารล่าสุด n ชิ้น."""
        self._ensure_fresh()
        return list(reversed(self._docs[-n:]))

    def search(self, query: str, max_results: int = SEARCH_MAX_RESULTS) -> list[dict]:
        """ค้นหาเอกสาร — substring match ใน category, extracted_text, upload_date.

        รองรับภาษาไทย — ใช้ substring match ซึ่งทำงานได้ดีกับภาษาไทย
        เพราะไม่ต้องแบ่งคำ

        โหลดใหม่จาก GCS ทุกครั้งที่ cache หมดอายุ — รองรับ multi-worker
        """
        self._ensure_fresh()
        query_lower = query.lower().strip()
        if not query_lower:
            return []

        results = []
        for doc in reversed(self._docs):  # ใหม่ก่อน
            searchable = " ".join([
                doc.get("category", ""),
                doc.get("original_filename", ""),
                doc.get("extracted_text", ""),
                doc.get("upload_date", ""),
                doc.get("mime_type", ""),
            ]).lower()
            if query_lower in searchable:
                results.append(doc)
                if len(results) >= max_results:
                    break
        return results

    def search_by_owner(
        self, query: str, owner_hash: str, max_results: int = SEARCH_MAX_RESULTS
    ) -> list[dict]:
        """ค้นหาเอกสารเฉพาะของ owner — ป้องกัน cross-patient access.

        เอกสารที่ไม่มี owner_hash (เก่า) จะไม่ปรากฏในผลค้นหาแบบ owner-aware
        ยกเว้นผู้ดูแลระบบเรียกดูผ่าน admin endpoint
        """
        self._ensure_fresh()
        query_lower = query.lower().strip()
        if not query_lower:
            return []

        results = []
        for doc in reversed(self._docs):  # ใหม่ก่อน
            # ตรวจ owner — Fail Closed: เอกสารที่ไม่มี owner_hash ถูกซ่อน
            doc_owner = doc.get("owner_hash", "")
            if not doc_owner or doc_owner != owner_hash:
                continue  # ข้ามเอกสารที่ไม่มีเจ้าของยืนยัน หรือเป็นของผู้อื่น

            searchable = " ".join([
                doc.get("category", ""),
                doc.get("original_filename", ""),
                doc.get("extracted_text", ""),
                doc.get("upload_date", ""),
                doc.get("mime_type", ""),
            ]).lower()
            if query_lower in searchable:
                results.append(doc)
                if len(results) >= max_results:
                    break
        return results

    def list_by_owner(self, owner_hash: str, limit: int = 100) -> list[dict]:
        """รายการเอกสารทั้งหมดของ owner — Fail Closed."""
        self._ensure_fresh()
        results = []
        for doc in self._docs:
            doc_owner = doc.get("owner_hash", "")
            if not doc_owner or doc_owner != owner_hash:
                continue
            results.append(doc)
            if len(results) >= limit:
                break
        return results

    def list_all(self, limit: int = 1000) -> list[dict]:
        """รายการเอกสารทั้งหมด — สำหรับ admin KPI เท่านั้น."""
        self._ensure_fresh()
        return self._docs[:limit]

    def recent_by_owner(self, owner_hash: str, n: int = 5) -> list[dict]:
        """เอกสารล่าสุด n ชิ้นของ owner — Fail Closed."""
        self._ensure_fresh()
        results = []
        for doc in reversed(self._docs):
            doc_owner = doc.get("owner_hash", "")
            if not doc_owner or doc_owner != owner_hash:
                continue  # ข้ามเอกสารไม่มีเจ้าของ หรือเป็นของผู้อื่น
            results.append(doc)
            if len(results) >= n:
                break
        return results

    def get_with_owner_check(
        self, doc_id: str, owner_hash: str
    ) -> tuple[dict | None, bool]:
        """ดึงเอกสารตาม document_id พร้อมตรวจ owner — Fail Closed.

        คืน (doc, authorized):
        - (doc, True) ถ้าเป็นเจ้าของที่ยืนยันแล้ว
        - (doc, False) ถ้าเอกสารไม่มี owner หรือเป็นของผู้อื่น
        - (None, False) ถ้าไม่พบเอกสาร
        """
        self._ensure_fresh()
        doc = self._doc_map.get(doc_id)
        if doc is None:
            # ลอง partial match
            for d in self._docs:
                if d.get("document_id", "").startswith(doc_id):
                    doc = d
                    break
        if doc is None:
            return None, False

        doc_owner = doc.get("owner_hash", "")
        if not doc_owner:
            # Fail Closed — เอกสาร legacy ไม่มี owner → ปฏิเสธ
            return doc, False
        if doc_owner == owner_hash:
            return doc, True
        return doc, False

    def count(self) -> int:
        """จำนวนเอกสารทั้งหมดใน index."""
        self._ensure_fresh()
        return len(self._docs)

    def count_legacy(self) -> int:
        """จำนวนเอกสารที่ไม่มี owner_hash (legacy, fail-closed)."""
        self._ensure_fresh()
        return sum(1 for d in self._docs if not d.get("owner_hash", ""))

    def assign_owner(self, doc_id: str, owner_hash: str) -> bool:
        """กำหนด owner_hash ให้เอกสาร — สำหรับ admin เท่านั้น.

        คืน True ถ้าพบและกำหนดสำเร็จ, False ถ้าไม่พบ
        ไม่เปลี่ยน owner_hash ที่มีอยู่แล้ว (ป้องกันการ reassign)
        """
        self._ensure_fresh()
        doc = self._doc_map.get(doc_id)
        if doc is None:
            for d in self._docs:
                if d.get("document_id", "").startswith(doc_id):
                    doc = d
                    break
        if doc is None:
            return False
        if doc.get("owner_hash", ""):
            return False  # ห้าม reassign — ต้องลบ owner เดิมก่อน
        doc["owner_hash"] = owner_hash
        return True

    def _save_full(self):
        """เขียน index ทั้งหมดลง GCS — ใช้ตอน backfill.

        Raise RuntimeError ถ้าเขียนไม่ได้
        """
        if not self._storage.is_ready:
            raise RuntimeError("Document index: GCS ไม่พร้อม — ไม่สามารถบันทึก index")
        try:
            lines = []
            for doc in self._docs:
                lines.append(json.dumps(doc, ensure_ascii=False))
            content = "\n".join(lines) + "\n" if lines else ""
            blob = self._storage._bucket.blob(INDEX_OBJECT_PATH)
            blob.upload_from_string(content, content_type="application/jsonl")
            logger.info(f"Document index: บันทึก {len(self._docs)} เอกสารลง GCS")
            self._cache_ts = time.time()
            return True
        except Exception as e:
            logger.error(f"Document index save ล้มเหลว: {type(e).__name__}")
            raise RuntimeError(
                f"Document index: บันทึก index ลง GCS ล้มเหลว: {type(e).__name__}"
            )


# สร้าง document index instance
doc_index = DocumentIndex(storage)
# Lazy load — จะโหลดตอนเรียกใช้ครั้งแรก


# ═══════════════════════════════════════════════════════════
# Audit Logger — บันทึกการค้นหา/เรียกดูเอกสาร
# ═══════════════════════════════════════════════════════════

class AuditLogger:
    """บันทึก audit log ลง GCS แบบ append-per-month."""

    def __init__(self, storage_instance):
        self._storage = storage_instance

    def log(self, action: str, user_hash: str, details: dict | None = None):
        """บันทึก audit event.

        action: search, view, download, backfill
        user_hash: SHA-256 hash ของ LINE user ID
        details: ข้อมูลเพิ่มเติม (ห้ามมีข้อมูลผู้ป่วย)
        """
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "user_hash": user_hash[:8] + "…",
            "details": details or {},
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"

        # เขียนลง GCS
        if self._storage.is_ready:
            try:
                month_key = datetime.now().strftime("%Y-%m")
                audit_path = f"{AUDIT_PREFIX}{month_key}.jsonl"
                blob = self._storage._bucket.blob(audit_path)
                if blob.exists():
                    existing = blob.download_as_text(encoding="utf-8")
                    blob.upload_from_string(
                        existing + line, content_type="application/jsonl"
                    )
                else:
                    blob.upload_from_string(
                        line, content_type="application/jsonl"
                    )
            except Exception as e:
                # แยกประเภท error เพื่อช่วย debug IAM permissions
                err_type = type(e).__name__
                err_msg = str(e)
                if "Forbidden" in err_msg or "403" in err_msg:
                    logger.error(
                        f"Audit log write Forbidden — "
                        f"ตรวจสอบ IAM: Service Account ต้องมี "
                        f"storage.objects.create + storage.objects.get "
                        f"สำหรับ path '{AUDIT_PREFIX}*' "
                        f"ใน bucket '{self._storage.bucket_name}'"
                    )
                else:
                    logger.error(f"Audit log write ล้มเหลว: {err_type}")

        # Fallback: เขียนลง local log ด้วย
        logger.info(f"AUDIT: {action} user={user_hash[:8]}…")


audit_logger = AuditLogger(storage)


# ─── Document Content Reading from GCS ────────────────────
# อ่านเนื้อหาเอกสารโดยตรงจาก GCS — ไม่ต้องส่ง signed URL ให้ผู้ใช้

# ขีดจำกัดขนาดสำหรับอ่านเนื้อหา (ป้องกัน memory overflow)
MAX_CONTENT_READ_SIZE = 10 * 1024 * 1024  # 10 MB
# ขีดจำกัดข้อความสรุปที่ส่งกลับทาง LINE
MAX_LINE_RESPONSE_TEXT = 4000  # LINE message limit ~5000 chars


def read_document_content(gcs_object_path: str, user_hash: str) -> dict:
    """อ่านเนื้อหาเอกสารจาก GCS โดยตรง.

    คืน dict:
        success: bool
        content_type: str (mime type)
        text: str (extracted text สำหรับ text-based docs)
        binary: bytes | None (raw bytes สำหรับ image/PDF)
        error: str | None (ถ้าล้มเหลว)
        size: int (ขนาดไฟล์)

    Security:
    - ต้องตรวจ is_authorized_user() ก่อนเรียก
    - ต้องตรวจ document owner ก่อนเรียก (ป้องกันข้ามผู้ป่วย)
    - ไม่ log เนื้อหาเอกสาร
    - ไม่ log gcs_object_path เต็ม
    """
    if not storage.is_ready or not hasattr(storage, '_bucket'):
        return {"success": False, "error": "GCS ไม่พร้อมใช้งาน"}

    try:
        blob = storage._bucket.blob(gcs_object_path)
        if not blob.exists():
            return {"success": False, "error": "ไม่พบไฟล์ใน GCS"}

        # ตรวจขนาดไฟล์ก่อนดาวน์โหลด
        blob.reload()
        file_size = blob.size or 0
        if file_size > MAX_CONTENT_READ_SIZE:
            return {
                "success": False,
                "error": f"ไฟล์ใหญ่เกินไป ({file_size:,} bytes, จำกัด {MAX_CONTENT_READ_SIZE:,})",
            }

        # ดาวน์โหลดเนื้อหา
        content_bytes = blob.download_as_bytes()
        content_type = blob.content_type or "application/octet-stream"

        result = {
            "success": True,
            "content_type": content_type,
            "size": len(content_bytes),
            "text": "",
            "binary": None,
        }

        # แยกข้อความจาก text-based content
        if content_type.startswith("text/"):
            try:
                result["text"] = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                result["text"] = content_bytes.decode("utf-8", errors="replace")

        elif content_type == "application/pdf":
            # ดึงข้อความจาก PDF ด้วย PyMuPDF
            result["binary"] = content_bytes
            if HAS_PYMUPDF:
                try:
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
                        tmp.write(content_bytes)
                        tmp.flush()
                        pdf_doc = pymupdf.open(tmp.name)
                        pages_text = []
                        for page_num in range(min(len(pdf_doc), MAX_PDF_PAGES)):
                            page_text = pdf_doc[page_num].get_text()
                            if page_text.strip():
                                pages_text.append(
                                    f"[หน้า {page_num + 1}]\n{page_text.strip()}"
                                )
                        pdf_doc.close()
                        result["text"] = "\n\n".join(pages_text)
                except Exception as pdf_err:
                    logger.warning(f"PDF text extraction ล้มเหลว: {type(pdf_err).__name__}")

            # ถ้าไม่มีข้อความ → ลอง OCR
            if not result["text"].strip() and HAS_TESSERACT and HAS_PIL:
                try:
                    result["text"] = _ocr_from_bytes(content_bytes, "pdf")
                except Exception:
                    pass

        elif content_type.startswith("image/"):
            result["binary"] = content_bytes
            # OCR สำหรับรูปภาพ
            if HAS_TESSERACT and HAS_PIL:
                try:
                    result["text"] = _ocr_from_bytes(content_bytes, "image")
                except Exception:
                    pass

        # ไม่ log เนื้อหาเอกสาร — เฉพาะ metadata เท่านั้น
        logger.info(f"Document read: size={len(content_bytes)}, type={content_type}")
        return result

    except Exception as e:
        logger.error(f"Document read ล้มเหลว: {type(e).__name__}")
        return {"success": False, "error": f"อ่านเอกสารล้มเหลว: {type(e).__name__}"}


def _ocr_from_bytes(data: bytes, file_type: str) -> str:
    """OCR จาก raw bytes — ใช้ภายในเท่านั้น."""
    with tempfile.NamedTemporaryFile(
        suffix=".pdf" if file_type == "pdf" else ".png", delete=True
    ) as tmp:
        tmp.write(data)
        tmp.flush()
        if file_type == "pdf" and HAS_PYMUPDF:
            pdf_doc = pymupdf.open(tmp.name)
            texts = []
            for page_num in range(min(len(pdf_doc), MAX_PDF_PAGES_VISION)):
                pix = pdf_doc[page_num].get_pixmap(dpi=200)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                page_text = pytesseract.image_to_string(img, lang="tha+eng")
                if page_text.strip():
                    texts.append(page_text.strip())
            pdf_doc.close()
            return "\n\n".join(texts)
        else:
            img = Image.open(tmp.name)
            return pytesseract.image_to_string(img, lang="tha+eng")


def format_document_text_for_line(text: str, doc_meta: dict) -> str:
    """จัดรูปแบบเนื้อหาเอกสารสำหรับส่งทาง LINE.

    ตัดข้อความถ้ายาวเกิน LINE limit
    """
    cat_names = {
        "bills": "ใบเสร็จ/บิล", "rx": "ใบสั่งยา",
        "lab": "ผลตรวจแล็บ", "dialysis": "เอกสารฟอกเลือด",
        "review": "รอตรวจสอบ",
    }
    cat = cat_names.get(doc_meta.get("category", ""), doc_meta.get("category", ""))
    header = (
        f"📄 เอกสาร: [{cat}]\n"
        f"วันที่: {doc_meta.get('upload_date', 'ไม่ทราบ')}\n"
        f"รหัส: {doc_meta.get('document_id', '')[:8]}…\n"
        f"{'─' * 20}\n"
    )

    if not text or not text.strip():
        return header + "⚠️ ไม่สามารถดึงข้อความจากเอกสารนี้ได้\nลองใช้ 'ขอไฟล์ [รหัส]' เพื่อดาวน์โหลดแทน"

    # ตัดข้อความไม่ให้เกิน LINE limit
    max_text = MAX_LINE_RESPONSE_TEXT - len(header) - 50
    if len(text) > max_text:
        text = text[:max_text] + "\n\n…(ข้อความถูกตัดเนื่องจากยาวเกินไป)\nใช้ 'ขอไฟล์ [รหัส]' เพื่อดาวน์โหลดฉบับเต็ม"

    return header + text


# ─── Document Search V1: Signed URL + Search Helpers ─────
def generate_signed_url(gcs_object_path: str, expiry_seconds: int = SIGNED_URL_EXPIRY) -> str | None:
    """สร้าง signed URL สำหรับเข้าถึงไฟล์ใน GCS อย่างปลอดภัย.

    ต้องใช้ Service Account JSON Key (ไม่รองรับ Workload Identity)
    signed URL หมดอายุตาม SIGNED_URL_EXPIRY (default 15 นาที)
    """
    if not storage.is_ready or not hasattr(storage, '_bucket'):
        return None
    try:
        blob = storage._bucket.blob(gcs_object_path)
        if not blob.exists():
            return None
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=expiry_seconds),
            method="GET",
        )
        return url
    except Exception as e:
        logger.error(f"Signed URL generation ล้มเหลว: {type(e).__name__}")
        return None


# ─── Download Proxy: Stateless HMAC Token ─────────────────
# ใช้เมื่อ Signed URL ยาวเกิน LINE URI action limit (1000 chars)
# Token = base64url(gcs_path|expiry_ts).HMAC_signature
# ทำงานข้าม workers (stateless), หมดอายุตาม SIGNED_URL_EXPIRY
# ─────────────────────────────────────────────────────────────

_DOWNLOAD_TOKEN_KEY = (USER_ID_SALT + "::dl").encode("utf-8")

LINE_URI_ACTION_MAX_LENGTH = 1000  # LINE Messaging API limit


def generate_download_token(gcs_path: str, expiry_seconds: int = SIGNED_URL_EXPIRY) -> str:
    """สร้าง stateless HMAC download token (ไม่ต้องเก็บ state ข้าม workers)."""
    expiry = int(time.time()) + expiry_seconds
    payload = f"{gcs_path}|{expiry}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(
        _DOWNLOAD_TOKEN_KEY, payload.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{payload_b64}.{sig}"


def validate_download_token(token: str) -> str | None:
    """ตรวจสอบ token — คืน gcs_path ถ้าถูกต้องและยังไม่หมดอายุ, None ถ้าไม่ผ่าน."""
    try:
        parts = token.split(".", 1)
        if len(parts) != 2:
            return None
        payload = base64.urlsafe_b64decode(parts[0]).decode()
        expected_sig = hmac.new(
            _DOWNLOAD_TOKEN_KEY, payload.encode(), hashlib.sha256
        ).hexdigest()[:32]
        if not hmac.compare_digest(expected_sig, parts[1]):
            return None
        gcs_path, expiry_str = payload.rsplit("|", 1)
        if int(expiry_str) < time.time():
            return None
        return gcs_path
    except Exception:
        return None


def get_download_url(gcs_path: str) -> str | None:
    """สร้าง URL สำหรับดาวน์โหลด — ใช้ Signed URL ตรงถ้าสั้นพอ, ไม่งั้นใช้ proxy."""
    signed_url = generate_signed_url(gcs_path)
    if not signed_url:
        return None

    if len(signed_url) <= LINE_URI_ACTION_MAX_LENGTH:
        return signed_url

    # Signed URL ยาวเกิน → ใช้ proxy endpoint
    token = generate_download_token(gcs_path)
    # RENDER_EXTERNAL_URL อาจตั้งไว้ หรือใช้ request context
    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url:
        # Fallback: ใช้ Signed URL ตรงแม้จะยาว (LINE อาจ truncate)
        logger.warning("RENDER_EXTERNAL_URL ไม่ได้ตั้ง — ใช้ Signed URL ตรง")
        return signed_url
    return f"{base_url}/dl/{token}"


# ─── LINE Flex Message: ปุ่มเปิดเอกสาร ──────────────────────
# แทนที่การส่ง URL เป็นข้อความยาว → ส่ง Flex Message พร้อมปุ่ม
# ─────────────────────────────────────────────────────────────

_CAT_NAMES = {
    "bills": "ใบเสร็จ/บิล",
    "rx": "ใบสั่งยา",
    "lab": "ผลตรวจแล็บ",
    "dialysis": "เอกสารฟอกเลือด",
    "review": "รอตรวจสอบ",
}


def build_file_flex_message(doc: dict, download_url: str) -> dict:
    """สร้าง LINE Flex Message สำหรับดาวน์โหลดเอกสาร."""
    cat = _CAT_NAMES.get(doc.get("category", ""), doc.get("category", ""))
    date = doc.get("upload_date", "ไม่ทราบ")
    doc_id = doc.get("document_id", "")[:8]
    expiry_min = SIGNED_URL_EXPIRY // 60

    flex_contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "📎 เอกสาร",
                    "weight": "bold",
                    "size": "lg",
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "md",
                    "contents": [
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": "ประเภท:",
                                    "size": "sm",
                                    "color": "#555555",
                                    "flex": 0,
                                },
                                {
                                    "type": "text",
                                    "text": cat,
                                    "size": "sm",
                                    "color": "#111111",
                                    "align": "end",
                                },
                            ],
                        },
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "margin": "sm",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": "วันที่:",
                                    "size": "sm",
                                    "color": "#555555",
                                    "flex": 0,
                                },
                                {
                                    "type": "text",
                                    "text": date,
                                    "size": "sm",
                                    "color": "#111111",
                                    "align": "end",
                                },
                            ],
                        },
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "margin": "sm",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": "รหัส:",
                                    "size": "sm",
                                    "color": "#555555",
                                    "flex": 0,
                                },
                                {
                                    "type": "text",
                                    "text": f"{doc_id}…",
                                    "size": "sm",
                                    "color": "#111111",
                                    "align": "end",
                                },
                            ],
                        },
                    ],
                },
                {
                    "type": "text",
                    "text": f"⏱️ ลิงก์หมดอายุใน {expiry_min} นาที",
                    "size": "xxs",
                    "color": "#999999",
                    "margin": "md",
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#1DB446",
                    "action": {
                        "type": "uri",
                        "label": "เปิด PDF",
                        "uri": download_url,
                    },
                },
            ],
        },
    }

    return {
        "type": "flex",
        "altText": f"📎 เอกสาร [{cat}] — กดเพื่อเปิด",
        "contents": flex_contents,
    }


def is_authorized_user(user_hash: str) -> bool:
    """ตรวจสอบว่า user hash อยู่ใน Allowlist."""
    if not AUTHORIZED_USERS:
        return False  # ถ้าไม่ตั้งค่า = ปิดระบบค้นหา
    return user_hash in AUTHORIZED_USERS


def format_search_results(results: list[dict]) -> str:
    """จัดรูปแบบผลค้นหาเป็นข้อความ LINE."""
    if not results:
        return "ไม่พบเอกสารที่ตรงกับคำค้น"

    cat_names = {
        "bills": "ใบเสร็จ/บิล",
        "rx": "ใบสั่งยา",
        "lab": "ผลตรวจแล็บ",
        "dialysis": "เอกสารฟอกเลือด",
        "review": "รอตรวจสอบ",
    }
    lines = [f"พบ {len(results)} เอกสาร:\n"]
    for i, doc in enumerate(results, 1):
        cat = cat_names.get(doc.get("category", ""), doc.get("category", ""))
        date = doc.get("upload_date", "ไม่ทราบ")
        doc_id = doc.get("document_id", "")[:8]
        lines.append(
            f"{i}. [{cat}] {date}\n"
            f"   รหัส: {doc_id}…"
        )
    lines.append("\nพิมพ์ 'ขอไฟล์ [รหัส]' เพื่อเรียกดู")
    return "\n".join(lines)


def handle_search_command(text: str, user_hash: str) -> str | None:
    """ประมวลผลคำสั่งค้นหาเอกสาร — คืนข้อความตอบกลับ หรือ None ถ้าไม่ใช่คำสั่ง."""
    text_stripped = text.strip()

    # ── ยืนยันตัวตน (One-Time Enrollment Token) ──────────────
    # คำสั่ง: "ยืนยัน [TOKEN]"
    # ผู้ดูแลได้รับ Token จาก POST /admin/enroll (ต้องยืนยัน ADMIN_SECRET)
    # แล้วพิมพ์ Token ผ่าน LINE — ระบบตรวจสอบและคืน hash ของ LINE userId
    # ─ Token ใช้ได้ครั้งเดียว อายุ 5 นาที
    # ─ ไม่แสดง LINE userId จริง
    # ─ ไม่บันทึก Token หรือ userId ลง log
    # ─ ป้องกัน replay attack และ rate limiting
    if text_stripped.startswith("ยืนยัน "):
        provided_token = text_stripped[len("ยืนยัน "):].strip()
        if not provided_token:
            return None  # ไม่ตอบกลับ — ไม่เปิดเผยว่ามีคำสั่งนี้

        result = enrollment_store.validate_and_consume(provided_token, user_hash)

        if result == "RATE_LIMITED":
            audit_logger.log("enroll_rate_limited", user_hash, {})
            return "⚠️ คุณพิมพ์รหัสผิดหลายครั้ง\nกรุณารอ 1 ชั่วโมงแล้วลองใหม่"

        if result != "VALID":
            # Token ไม่ถูก/หมดอายุ/ใช้แล้ว → บันทึก audit แต่ไม่บอกรายละเอียด
            audit_logger.log("enroll_failed", user_hash, {})
            return None  # ไม่ตอบกลับ — ป้องกัน brute force

        # ─ Token ถูกต้อง → ลงทะเบียนสำเร็จ ─
        audit_logger.log("enroll_success", user_hash, {})
        salt_warning = ""
        if IS_PRODUCTION and not os.getenv("USER_ID_SALT", "").strip():
            salt_warning = (
                "\n\n⚠️ ยังไม่ได้ตั้ง USER_ID_SALT ใน Render\n"
                "ค่า hash นี้ใช้ default salt\n"
                "แนะนำ: ตั้ง USER_ID_SALT แล้วลงทะเบียนใหม่"
            )
        return (
            f"✅ ลงทะเบียนสำเร็จ\n\n"
            f"ค่า Hash สำหรับ AUTHORIZED_USERS:\n"
            f"{user_hash}\n\n"
            f"วิธีใช้: เพิ่มค่านี้ใน Render Dashboard\n"
            f"AUTHORIZED_USERS={user_hash}\n"
            f"(หลายคนคั่นด้วยเครื่องหมายจุลภาค){salt_warning}"
        )

    # ── Parse command ด้วย line_bot_enhancer ──
    from line_bot_enhancer import (
        parse_command as _parse_cmd, CommandType,
        parse_search_query, smart_search,
        format_search_results_enhanced,
        generate_help_text,
        compute_user_stats, user_stats_text_th,
        find_latest_lab_doc,
    )
    parsed = _parse_cmd(text_stripped)

    # ช่วยเหลือ / help / เมนู
    if parsed.command_type == CommandType.HELP:
        is_admin = user_hash in ADMIN_USERS
        has_docs = doc_index.count_by_owner(user_hash) > 0 if hasattr(doc_index, 'count_by_owner') else True
        return generate_help_text(is_admin=is_admin, has_docs=has_docs)

    # Authorization check — ต้องผ่านก่อนใช้คำสั่งค้นหา/อ่าน
    # ใช้ parsed command แทนการเช็คทีละคำสั่ง
    PROTECTED_COMMANDS = {
        CommandType.SEARCH, CommandType.RECENT, CommandType.READ_DOC,
        CommandType.REQUEST_FILE, CommandType.LAB_RESULT,
        CommandType.APPROVE_LAB, CommandType.CLINICAL_ANALYSIS,
        CommandType.TREND, CommandType.PATIENT_REPORT,
        CommandType.CLINIC_KPI, CommandType.MEDICATION_REVIEW,
        CommandType.MY_STATS,
    }
    if not is_authorized_user(user_hash):
        if parsed.command_type in PROTECTED_COMMANDS:
            audit_logger.log("unauthorized_access", user_hash, {
                "command": text_stripped[:20]
            })
            return "ขออภัย คุณไม่มีสิทธิ์ใช้งานระบบค้นหาเอกสาร\nกรุณาติดต่อผู้ดูแลระบบ"
        # ตรวจสอบคำสั่งเดิมที่อาจยังไม่ถูก parse
        if (text_stripped.startswith("ค้นหา ")
                or text_stripped == "รายการล่าสุด"
                or text_stripped.startswith("อ่านเอกสาร ")
                or text_stripped.startswith("ขอไฟล์ ")
                or text_stripped.startswith("ผลแล็บ ")
                or text_stripped.startswith("ยืนยันผลแล็บ ")
                or text_stripped.startswith("แนวโน้มแล็บ")
                or text_stripped == "แนวโน้มแล็บ"
                or text_stripped == "รายงานแล็บ"
                or text_stripped == "KPI"
                or text_stripped.startswith("ตรวจยา")):
            audit_logger.log("unauthorized_access", user_hash, {
                "command": text_stripped[:20]
            })
            return "ขออภัย คุณไม่มีสิทธิ์ใช้งานระบบค้นหาเอกสาร\nกรุณาติดต่อผู้ดูแลระบบ"
        return None  # ไม่ใช่คำสั่งค้นหา

    # ── สถิติ — แสดงสรุปเอกสารของผู้ใช้ ──
    if parsed.command_type == CommandType.MY_STATS:
        try:
            user_docs = doc_index.list_by_owner(user_hash, limit=200)
            # นับผลแล็บและผลวิเคราะห์จาก GCS
            lab_count = 0
            approved_count = 0
            pending_count = 0
            clinical_count = 0
            for d in user_docs:
                did = d.get("document_id", "")
                lab_path = f"{LAB_RESULTS_PREFIX}{did}.json"
                lab_data = storage.get(lab_path)
                if lab_data:
                    lab_count += 1
                    ld = json.loads(lab_data)
                    if ld.get("approved"):
                        approved_count += 1
                    else:
                        pending_count += 1
                clinical_path = f"{CLINICAL_ANALYSIS_PREFIX}{did}.json"
                if storage.get(clinical_path):
                    clinical_count += 1
            stats = compute_user_stats(
                user_docs,
                lab_report_count=lab_count,
                clinical_analysis_count=clinical_count,
                approved_count=approved_count,
                pending_count=pending_count,
            )
            audit_logger.log("user_stats", user_hash, {
                "total_docs": stats.total_documents,
            })
            return user_stats_text_th(stats)
        except Exception as e:
            logger.warning(f"Stats error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถแสดงสถิติได้ในขณะนี้"

    # ค้นหา [คำค้น] — owner-aware + smart search
    if parsed.command_type == CommandType.SEARCH:
        query = parsed.args
        if not query:
            return "กรุณาระบุคำค้น เช่น: ค้นหา ผลแล็บ"
        # ใช้ smart search ที่รองรับ multi-keyword + category + date filter
        search_filter = parse_search_query(query)
        all_docs = doc_index.list_by_owner(user_hash, limit=200)
        results = smart_search(
            all_docs, search_filter, user_hash,
            max_results=SEARCH_MAX_RESULTS,
        )
        audit_logger.log("search", user_hash, {
            "query": query[:50],
            "results_count": len(results),
            "category_filter": search_filter.category or "",
            "date_filter": search_filter.date_range_days or 0,
        })
        return format_search_results_enhanced(results, query=query)
    # รองรับคำสั่งเดิม "ค้นหา ..." ที่ parser อาจไม่จับ
    if text_stripped.startswith("ค้นหา "):
        query = text_stripped[len("ค้นหา "):].strip()
        if not query:
            return "กรุณาระบุคำค้น เช่น: ค้นหา ผลแล็บ"
        search_filter = parse_search_query(query)
        all_docs = doc_index.list_by_owner(user_hash, limit=200)
        results = smart_search(
            all_docs, search_filter, user_hash,
            max_results=SEARCH_MAX_RESULTS,
        )
        audit_logger.log("search", user_hash, {
            "query": query[:50],
            "results_count": len(results),
        })
        return format_search_results_enhanced(results, query=query)

    # รายการล่าสุด — owner-aware
    if parsed.command_type == CommandType.RECENT or text_stripped == "รายการล่าสุด":
        results = doc_index.recent_by_owner(user_hash, 5)
        audit_logger.log("list_recent", user_hash, {
            "results_count": len(results),
        })
        if not results:
            return "ยังไม่มีเอกสารในระบบ"
        return format_search_results_enhanced(results)

    # อ่านเอกสาร [document_id] — อ่านเนื้อหาจาก GCS โดยตรง
    # รองรับ alias: อ่าน [รหัส]
    _read_doc_id = None
    if parsed.command_type == CommandType.READ_DOC:
        _read_doc_id = parsed.args
    elif text_stripped.startswith("อ่านเอกสาร "):
        _read_doc_id = text_stripped[len("อ่านเอกสาร "):].strip()
    if _read_doc_id is not None:
        doc_id_query = _read_doc_id
        if not doc_id_query:
            return "กรุณาระบุรหัสเอกสาร เช่น: อ่านเอกสาร a1b2c3d4"

        doc, authorized = doc_index.get_with_owner_check(doc_id_query, user_hash)

        if doc is None:
            audit_logger.log("read_not_found", user_hash, {"query": doc_id_query[:8]})
            return "ไม่พบเอกสารที่ตรงกับรหัสนี้\nลองใช้คำสั่ง 'ค้นหา' หรือ 'รายการล่าสุด' ก่อน"

        if not authorized:
            audit_logger.log("read_unauthorized", user_hash, {
                "doc_id": doc.get("document_id", "")[:8],
            })
            return "ขออภัย คุณไม่มีสิทธิ์เข้าถึงเอกสารนี้"

        gcs_path = doc.get("gcs_object_path", "")
        if not gcs_path:
            return "ไม่สามารถเข้าถึงไฟล์นี้ได้ — ไม่มี path ใน GCS"

        # ลองใช้ extracted_text จาก index ก่อน (เร็วกว่าดาวน์โหลด)
        idx_text = doc.get("extracted_text", "").strip()
        if idx_text and len(idx_text) > 50:
            audit_logger.log("read_from_index", user_hash, {
                "doc_id": doc.get("document_id", "")[:8],
            })
            return format_document_text_for_line(idx_text, doc)

        # ถ้าไม่มี extracted_text → อ่านจาก GCS โดยตรง
        content = read_document_content(gcs_path, user_hash)
        if not content["success"]:
            audit_logger.log("read_failed", user_hash, {
                "doc_id": doc.get("document_id", "")[:8],
                "error": content.get("error", "")[:50],
            })
            return f"ขออภัย ไม่สามารถอ่านเอกสารได้: {content.get('error', 'ไม่ทราบสาเหตุ')}"

        audit_logger.log("read_from_gcs", user_hash, {
            "doc_id": doc.get("document_id", "")[:8],
            "size": content.get("size", 0),
        })
        return format_document_text_for_line(content.get("text", ""), doc)

    # ขอไฟล์ [document_id] — owner-aware → ตอบด้วย Flex Message
    # รองรับ alias: ดาวน์โหลด [รหัส]
    _file_doc_id = None
    if parsed.command_type == CommandType.REQUEST_FILE:
        _file_doc_id = parsed.args
    elif text_stripped == "ขอไฟล์":
        _file_doc_id = ""
    elif text_stripped.startswith("ขอไฟล์ "):
        _file_doc_id = text_stripped[len("ขอไฟล์ "):].strip()
    if _file_doc_id is not None:
        doc_id_query = _file_doc_id
        if not doc_id_query:
            return "กรุณาระบุรหัสเอกสาร เช่น: ขอไฟล์ a1b2c3d4"

        doc, authorized = doc_index.get_with_owner_check(doc_id_query, user_hash)

        if doc is None:
            audit_logger.log("file_request_not_found", user_hash, {
                "query": doc_id_query[:8],
            })
            return "ไม่พบเอกสารที่ตรงกับรหัสนี้\nลองใช้คำสั่ง 'ค้นหา' หรือ 'รายการล่าสุด' ก่อน"

        if not authorized:
            audit_logger.log("file_request_unauthorized", user_hash, {
                "doc_id": doc.get("document_id", "")[:8],
            })
            return "ขออภัย คุณไม่มีสิทธิ์เข้าถึงเอกสารนี้"

        gcs_path = doc.get("gcs_object_path", "")
        if not gcs_path:
            return "ไม่สามารถเข้าถึงไฟล์นี้ได้ — ไม่มี path ใน GCS"

        download_url = get_download_url(gcs_path)
        if not download_url:
            audit_logger.log("file_request_failed", user_hash, {
                "doc_id": doc.get("document_id", "")[:8],
            })
            return "ขออภัย ไม่สามารถสร้างลิงก์ดาวน์โหลดได้ในขณะนี้\nกรุณาลองใหม่ในอีกสักครู่"

        audit_logger.log("file_request", user_hash, {
            "doc_id": doc.get("document_id", "")[:8],
            "category": doc.get("category", ""),
        })

        # คืน Flex Message dict → caller จะส่งเป็น Flex แทน text
        return build_file_flex_message(doc, download_url)

    # ── ผลแล็บ [document_id] — ดูผล Lab ที่ดึงจากเอกสาร ──
    # รองรับ alias: ผลตรวจ, ผลเลือด, ผลแล็บล่าสุด
    _lab_doc_id = None
    if parsed.command_type == CommandType.LAB_RESULT:
        if parsed.args:
            _lab_doc_id = parsed.args
        else:
            # "ผลแล็บล่าสุด" — ค้นหา lab doc ล่าสุดของผู้ใช้
            all_docs = doc_index.list_by_owner(user_hash, limit=100)
            latest = find_latest_lab_doc(all_docs, user_hash)
            if latest:
                _lab_doc_id = latest.get("document_id", "")
            else:
                return "ไม่พบผลตรวจ Lab ในระบบ\nกรุณาอัปโหลดเอกสารผลตรวจก่อน"
    elif text_stripped == "ผลแล็บ":
        return "กรุณาระบุรหัสเอกสาร เช่น: ผลแล็บ a1b2c3d4\nหรือพิมพ์ 'ผลแล็บล่าสุด'"
    elif text_stripped.startswith("ผลแล็บ "):
        _lab_doc_id = text_stripped[len("ผลแล็บ "):].strip()
    if _lab_doc_id is not None:
        doc_id_query = _lab_doc_id
        if not doc_id_query:
            return "กรุณาระบุรหัสเอกสาร เช่น: ผลแล็บ a1b2c3d4"

        # ตรวจสอบ ownership ผ่าน document index
        doc, authorized = doc_index.get_with_owner_check(doc_id_query, user_hash)
        if doc is None:
            audit_logger.log("lab_result_not_found", user_hash, {
                "query": doc_id_query[:8],
            })
            return "ไม่พบเอกสารที่ตรงกับรหัสนี้"
        if not authorized:
            audit_logger.log("lab_result_unauthorized", user_hash, {
                "doc_id": doc.get("document_id", "")[:8],
            })
            return "ขออภัย คุณไม่มีสิทธิ์เข้าถึงเอกสารนี้"

        # อ่านผลแล็บจาก GCS
        full_doc_id = doc.get("document_id", doc_id_query)
        lab_path = f"{LAB_RESULTS_PREFIX}{full_doc_id}.json"
        try:
            lab_data = storage.get(lab_path)
            if lab_data is None:
                audit_logger.log("lab_result_none", user_hash, {
                    "doc_id": full_doc_id[:8],
                })
                return (
                    "ยังไม่มีผลแล็บสำหรับเอกสารนี้\n"
                    "เอกสารอาจไม่ใช่ผลตรวจ Lab หรือยังไม่ได้ประมวลผล"
                )
            from lab_models import LabReport
            lab_report = LabReport.from_dict(json.loads(lab_data))
            audit_logger.log("lab_result_view", user_hash, {
                "doc_id": full_doc_id[:8],
                "result_count": len(lab_report.results),
                "status": lab_report.status,
            })
            return lab_report.summary_text()
        except Exception as e:
            logger.warning(f"Lab result read error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถอ่านผลแล็บได้ในขณะนี้"

    # ── ยืนยันผลแล็บ [document_id] — Admin only ──
    # รองรับ alias: approve [id]
    _approve_doc_id = None
    if parsed.command_type == CommandType.APPROVE_LAB:
        _approve_doc_id = parsed.args if parsed.args else None
    elif text_stripped == "ยืนยันผลแล็บ":
        _approve_doc_id = None  # will prompt for ID
    elif text_stripped.startswith("ยืนยันผลแล็บ "):
        _approve_doc_id = text_stripped[len("ยืนยันผลแล็บ "):].strip()

    if parsed.command_type == CommandType.APPROVE_LAB or text_stripped == "ยืนยันผลแล็บ" or text_stripped.startswith("ยืนยันผลแล็บ "):
        if not _approve_doc_id:
            return "กรุณาระบุรหัสเอกสาร เช่น: ยืนยันผลแล็บ a1b2c3d4"
        doc_id_query = _approve_doc_id

        # ต้องเป็น Admin เท่านั้น
        if user_hash not in ADMIN_USERS:
            audit_logger.log("lab_approve_not_admin", user_hash, {
                "doc_id": doc_id_query[:8],
            })
            return "ขออภัย เฉพาะผู้ดูแลระบบเท่านั้นที่สามารถยืนยันผลแล็บได้"

        # ตรวจสอบเอกสาร (admin เห็นทุกเอกสาร)
        doc = doc_index.get(doc_id_query)
        if doc is None:
            return "ไม่พบเอกสารที่ตรงกับรหัสนี้"

        full_doc_id = doc.get("document_id", doc_id_query)
        lab_path = f"{LAB_RESULTS_PREFIX}{full_doc_id}.json"
        try:
            lab_data = storage.get(lab_path)
            if lab_data is None:
                return "ยังไม่มีผลแล็บสำหรับเอกสารนี้"

            from lab_models import LabReport, ReviewStatus
            lab_report = LabReport.from_dict(json.loads(lab_data))

            if lab_report.status == ReviewStatus.APPROVED.value:
                return f"ผลแล็บ {full_doc_id[:8]} ได้รับการยืนยันแล้ว"

            # อัพเดทสถานะเป็น approved
            lab_report.status = ReviewStatus.APPROVED.value
            lab_report.reviewed_by = user_hash[:8]  # เก็บเฉพาะ prefix
            lab_report.reviewed_at = datetime.now().isoformat()

            # บันทึกกลับ GCS
            storage.put(
                lab_path,
                lab_report.to_json().encode("utf-8"),
                content_type="application/json",
            )

            audit_logger.log("lab_approve", user_hash, {
                "doc_id": full_doc_id[:8],
                "result_count": len(lab_report.results),
            })

            return (
                f"✅ ยืนยันผลแล็บ {full_doc_id[:8]} สำเร็จ\n"
                f"จำนวนผลตรวจ: {len(lab_report.results)} รายการ\n"
                f"สถานะ: approved"
            )
        except Exception as e:
            logger.warning(f"Lab approve error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถยืนยันผลแล็บได้ในขณะนี้"

    # ── วิเคราะห์แล็บ [document_id] — ดูผลวิเคราะห์ทางคลินิก ──
    # รองรับ alias: วิเคราะห์, วิเคราะห์แล็บล่าสุด
    _analyze_doc_id = None
    if parsed.command_type == CommandType.CLINICAL_ANALYSIS:
        if parsed.args:
            _analyze_doc_id = parsed.args
        else:
            # "วิเคราะห์แล็บล่าสุด" — ค้นหา lab doc ล่าสุด
            all_docs = doc_index.list_by_owner(user_hash, limit=100)
            latest = find_latest_lab_doc(all_docs, user_hash)
            if latest:
                _analyze_doc_id = latest.get("document_id", "")
            else:
                return "ไม่พบผลตรวจ Lab ในระบบ\nกรุณาอัปโหลดเอกสารผลตรวจก่อน"
    elif text_stripped == "วิเคราะห์แล็บ":
        return "กรุณาระบุรหัสเอกสาร เช่น: วิเคราะห์แล็บ a1b2c3d4\nหรือพิมพ์ 'วิเคราะห์แล็บล่าสุด'"
    elif text_stripped.startswith("วิเคราะห์แล็บ "):
        _analyze_doc_id = text_stripped[len("วิเคราะห์แล็บ "):].strip()
    if _analyze_doc_id is not None:
        doc_id_query = _analyze_doc_id
        if not doc_id_query:
            return "กรุณาระบุรหัสเอกสาร เช่น: วิเคราะห์แล็บ a1b2c3d4"

        # ตรวจสอบ ownership ผ่าน document index
        doc, authorized = doc_index.get_with_owner_check(
            doc_id_query, user_hash
        )
        if doc is None:
            return "ไม่พบเอกสารที่ตรงกับรหัสนี้"
        if not authorized:
            audit_logger.log("clinical_access_denied", user_hash, {
                "doc_id": doc.get("document_id", "")[:8],
            })
            return "ขออภัย คุณไม่มีสิทธิ์เข้าถึงเอกสารนี้"

        full_doc_id = doc.get("document_id", doc_id_query)
        clinical_path = f"{CLINICAL_ANALYSIS_PREFIX}{full_doc_id}.json"
        try:
            clinical_data = storage.get(clinical_path)
            if clinical_data is None:
                audit_logger.log("clinical_analysis_none", user_hash, {
                    "doc_id": full_doc_id[:8],
                })
                return (
                    "ยังไม่มีผลวิเคราะห์ทางคลินิกสำหรับเอกสารนี้\n"
                    "กรุณาตรวจสอบว่า:\n"
                    "1. เอกสารเป็นผลตรวจ Lab\n"
                    "2. ระบบได้ประมวลผลเรียบร้อยแล้ว"
                )
            clinical_dict = json.loads(clinical_data)
            audit_logger.log("clinical_analysis_view", user_hash, {
                "doc_id": full_doc_id[:8],
                "finding_count": clinical_dict.get("finding_count", 0),
                "ckd_stage": clinical_dict.get("ckd_stage", "UNKNOWN"),
            })
            # แสดงข้อความสรุปที่สร้างไว้
            summary = clinical_dict.get("summary_text_th", "")
            if summary:
                return summary
            return "มีผลวิเคราะห์แต่ไม่สามารถแสดงสรุปได้"
        except Exception as e:
            logger.warning(
                f"Clinical analysis read error: {type(e).__name__}"
            )
            return "ขออภัย ไม่สามารถอ่านผลวิเคราะห์ได้ในขณะนี้"

    # ── แนวโน้มแล็บ [period] — ดูแนวโน้ม Lab ตามช่วงเวลา ──
    # รองรับ alias: แนวโน้ม, trend
    PERIOD_MAP = {
        "เดือน": "monthly",
        "รายเดือน": "monthly",
        "3เดือน": "quarterly",
        "ราย3เดือน": "quarterly",
        "6เดือน": "semi_annual",
        "ราย6เดือน": "semi_annual",
        "ปี": "annual",
        "รายปี": "annual",
    }
    _trend_help = (
        "📊 ดูแนวโน้มผลตรวจ Lab\n"
        "กรุณาระบุช่วงเวลา:\n"
        "  แนวโน้มแล็บ เดือน\n"
        "  แนวโน้มแล็บ 3เดือน\n"
        "  แนวโน้มแล็บ 6เดือน\n"
        "  แนวโน้มแล็บ ปี"
    )
    _trend_input = None
    if parsed.command_type == CommandType.TREND:
        _trend_input = parsed.args if parsed.args else None
        if _trend_input is None:
            return _trend_help
    elif text_stripped == "แนวโน้มแล็บ":
        return _trend_help
    elif text_stripped.startswith("แนวโน้มแล็บ "):
        _trend_input = text_stripped[len("แนวโน้มแล็บ "):].strip()
    if _trend_input is not None:
        period_input = _trend_input
        period = PERIOD_MAP.get(period_input)
        if period is None:
            return (
                "ไม่รู้จักช่วงเวลานี้ — กรุณาใช้:\n"
                "  แนวโน้มแล็บ เดือน\n"
                "  แนวโน้มแล็บ 3เดือน\n"
                "  แนวโน้มแล็บ 6เดือน\n"
                "  แนวโน้มแล็บ ปี"
            )

        # รวบรวมผลแล็บทั้งหมดของผู้ใช้จาก GCS
        try:
            from lab_tracker import analyze_trends, PERIOD_DAYS
            from datetime import datetime as _dt, timedelta as _td

            # ดึงรายการเอกสารของผู้ใช้
            user_docs = doc_index.list_by_owner(user_hash, limit=100)
            if not user_docs:
                return "ไม่พบเอกสารของคุณ — กรุณาอัปโหลดผลตรวจ Lab ก่อน"

            # ดึงผลแล็บจาก GCS สำหรับแต่ละเอกสาร
            lab_reports = []
            for doc_meta in user_docs:
                did = doc_meta.get("document_id", "")
                if not did:
                    continue
                lab_path = f"{LAB_RESULTS_PREFIX}{did}.json"
                try:
                    lab_data = storage.get(lab_path)
                    if lab_data:
                        report = json.loads(lab_data)
                        lab_reports.append(report)
                except Exception:
                    continue

            if not lab_reports:
                return (
                    "ไม่พบผลตรวจ Lab ในระบบ\n"
                    "กรุณาอัปโหลดเอกสารผลตรวจ Lab ก่อน"
                )

            # วิเคราะห์แนวโน้ม
            analysis = analyze_trends(
                lab_reports=lab_reports,
                period=period,
                owner_hash=user_hash,
            )

            audit_logger.log("trend_analysis_view", user_hash, {
                "period": period,
                "trend_count": len(analysis.trends),
                "alert_count": len(analysis.alerts),
                "critical_count": len(analysis.critical_alerts()),
            })

            return analysis.summary_text_th()

        except Exception as e:
            logger.warning(f"Trend analysis error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถวิเคราะห์แนวโน้มได้ในขณะนี้"

    # ── รายงานแล็บ — สรุป KPI ผลตรวจรายบุคคล ──
    # รองรับ alias: รายงาน, สรุปผลตรวจ, report
    if text_stripped == "รายงานแล็บ" or parsed.command_type == CommandType.PATIENT_REPORT:
        try:
            from clinical_reports import (
                generate_patient_report,
                patient_report_text_th,
            )

            # ดึงรายการเอกสารของผู้ใช้
            user_docs = doc_index.list_by_owner(user_hash, limit=200)
            doc_count = len(user_docs) if user_docs else 0

            # ดึงผลแล็บและผลวิเคราะห์จาก GCS
            lab_reports = []
            clinical_analyses = []
            for doc_meta in (user_docs or []):
                did = doc_meta.get("document_id", "")
                if not did:
                    continue
                # Lab results
                lab_path = f"{LAB_RESULTS_PREFIX}{did}.json"
                try:
                    lab_data = storage.get(lab_path)
                    if lab_data:
                        lab_reports.append(json.loads(lab_data))
                except Exception:
                    pass
                # Clinical analyses
                ca_path = f"{CLINICAL_ANALYSIS_PREFIX}{did}.json"
                try:
                    ca_data = storage.get(ca_path)
                    if ca_data:
                        clinical_analyses.append(json.loads(ca_data))
                except Exception:
                    pass

            if not lab_reports and not clinical_analyses:
                return (
                    "ไม่พบข้อมูลผลตรวจ Lab หรือผลวิเคราะห์ของคุณ\n"
                    "กรุณาอัปโหลดเอกสารผลตรวจ Lab ก่อน"
                )

            kpi = generate_patient_report(
                lab_reports=lab_reports,
                clinical_analyses=clinical_analyses,
                doc_count=doc_count,
                owner_hash=user_hash,
            )

            audit_logger.log("patient_report_view", user_hash, {
                "total_documents": doc_count,
                "total_lab_reports": len(lab_reports),
                "total_clinical_analyses": len(clinical_analyses),
                "ckd_stage": kpi.latest_ckd_stage,
            })

            return patient_report_text_th(kpi)

        except Exception as e:
            logger.warning(f"Patient report error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถสร้างรายงานได้ในขณะนี้"

    # ── KPI — รายงาน KPI รวมคลินิก (admin only) ──
    if text_stripped == "KPI":
        if user_hash not in ADMIN_USERS:
            return "คำสั่งนี้สำหรับผู้ดูแลระบบเท่านั้น"

        try:
            from clinical_reports import (
                generate_patient_report,
                generate_clinic_summary,
                clinic_summary_text_th,
            )

            # ดึงรายชื่อ owner ทั้งหมดจาก index
            all_docs = doc_index.list_all(limit=1000)
            total_docs = len(all_docs) if all_docs else 0

            # จัดกลุ่มเอกสารตาม owner
            owner_docs: dict[str, list] = {}
            for doc_meta in (all_docs or []):
                oh = doc_meta.get("owner_hash", "")
                if oh:
                    owner_docs.setdefault(oh, []).append(doc_meta)

            # สร้าง PatientKPI ต่อ owner (ไม่เก็บ identity)
            all_kpis = []
            for oh, docs in owner_docs.items():
                lab_reports = []
                clinical_analyses = []
                for doc_meta in docs:
                    did = doc_meta.get("document_id", "")
                    if not did:
                        continue
                    lab_path = f"{LAB_RESULTS_PREFIX}{did}.json"
                    try:
                        lab_data = storage.get(lab_path)
                        if lab_data:
                            lab_reports.append(json.loads(lab_data))
                    except Exception:
                        pass
                    ca_path = f"{CLINICAL_ANALYSIS_PREFIX}{did}.json"
                    try:
                        ca_data = storage.get(ca_path)
                        if ca_data:
                            clinical_analyses.append(json.loads(ca_data))
                    except Exception:
                        pass

                pkpi = generate_patient_report(
                    lab_reports=lab_reports,
                    clinical_analyses=clinical_analyses,
                    doc_count=len(docs),
                    owner_hash=oh,
                )
                all_kpis.append(pkpi)

            clinic = generate_clinic_summary(all_kpis, total_docs)

            audit_logger.log("clinic_kpi_view", user_hash, {
                "total_patients": clinic.total_patients,
                "total_documents": clinic.total_documents,
            })

            return clinic_summary_text_th(clinic)

        except Exception as e:
            logger.warning(f"Clinic KPI error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถสร้างรายงาน KPI ได้ในขณะนี้"

    # ── ตรวจยา [medication_list] — ตรวจสอบยาเพื่อไต ──
    # รองรับ alias: เช็คยา, ตรวจสอบยา, med
    _med_input = None
    if parsed.command_type == CommandType.MEDICATION_REVIEW:
        _med_input = parsed.args
    elif text_stripped == "ตรวจยา":
        _med_input = ""
    elif text_stripped.startswith("ตรวจยา "):
        _med_input = text_stripped[len("ตรวจยา "):].strip()
    if _med_input is not None:
        med_input = _med_input
        if not med_input:
            return (
                "กรุณาระบุรายการยา เช่น:\n"
                "ตรวจยา Metformin, Ibuprofen, Losartan\n\n"
                "ระบบจะตรวจสอบ:\n"
                "• ยาที่เป็นพิษต่อไต\n"
                "• การปรับขนาดยาตาม eGFR\n"
                "• ปฏิกิริยายากับผลแล็บ\n"
                "• ภาวะน้ำในร่างกาย"
            )

        try:
            from medication_review import (
                review_medications,
                review_text_th,
            )

            # แยกรายการยาจาก comma / space
            medications = [
                m.strip() for m in med_input.replace(",", " ").replace("、", " ").replace("，", " ").split()
                if m.strip()
            ]
            if not medications:
                return "กรุณาระบุชื่อยาอย่างน้อย 1 รายการ"

            # ดึงผลแล็บล่าสุดของผู้ใช้เพื่อประกอบการตรวจ
            lab_values: dict[str, float] = {}
            ckd_stage = ""
            egfr_val = None

            user_docs = doc_index.list_by_owner(user_hash, limit=50)
            if user_docs:
                # หาผลแล็บล่าสุด
                for doc_meta in user_docs:
                    did = doc_meta.get("document_id", "")
                    if not did:
                        continue
                    lab_path = f"{LAB_RESULTS_PREFIX}{did}.json"
                    try:
                        lab_data = storage.get(lab_path)
                        if lab_data:
                            lr = json.loads(lab_data)
                            # ดึง analyte values ล่าสุด
                            results = lr.get("results", [])
                            for r in results:
                                name = r.get("analyte", "")
                                val = r.get("value")
                                if name and val is not None:
                                    try:
                                        lab_values[name] = float(val)
                                    except (ValueError, TypeError):
                                        pass
                    except Exception:
                        pass

                    # ดึง CKD stage และ eGFR จากผลวิเคราะห์
                    ca_path = f"{CLINICAL_ANALYSIS_PREFIX}{did}.json"
                    try:
                        ca_data = storage.get(ca_path)
                        if ca_data:
                            ca = json.loads(ca_data)
                            if not ckd_stage:
                                ckd_stage = ca.get("ckd_stage", "")
                            if egfr_val is None:
                                egfr_val = ca.get("egfr")
                                if egfr_val is not None:
                                    try:
                                        egfr_val = float(egfr_val)
                                    except (ValueError, TypeError):
                                        egfr_val = None
                    except Exception:
                        pass

                    # หยุดเมื่อได้ข้อมูลพอแล้ว
                    if lab_values and ckd_stage and egfr_val is not None:
                        break

            result = review_medications(
                medications=medications,
                lab_values=lab_values,
                ckd_stage=ckd_stage,
                egfr=egfr_val,
            )

            audit_logger.log("medication_review", user_hash, {
                "medication_count": len(medications),
                "alert_count": len(result.alerts),
                "critical_count": len(result.critical_alerts()),
                "has_lab_data": bool(lab_values),
                "ckd_stage": ckd_stage or "UNKNOWN",
            })

            return review_text_th(result)

        except Exception as e:
            logger.warning(f"Medication review error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถตรวจสอบยาได้ในขณะนี้"

    return None  # ไม่ใช่คำสั่งค้นหา


# ═══════════════════════════════════════════════════════════
# Rate Limiting & Deduplication
#
# ออกแบบให้ปลอดภัยเมื่อมีหลาย Gunicorn worker:
#   - backend ที่แชร์ได้ (Redis) ทำให้ dedup เป็น atomic ข้าม worker
#   - ถ้ายังไม่เชื่อม Redis หรือ Redis ล่ม → fallback เป็น in-memory ต่อ worker
#     พร้อมลดเพดาน rate limit ลงครึ่งหนึ่ง เพื่อให้เพดานรวมใกล้เคียงค่าที่ตั้งไว้
#
# Phase 1: ยังไม่เชื่อมบริการ Redis จริง (REDIS_URL ว่าง) และไม่เพิ่ม
#          dependency — import redis แบบ optional เท่านั้น
# ═══════════════════════════════════════════════════════════

def hash_user_id(user_id: str) -> str:
    """แปลง LINE User ID เป็น hash — ไม่เก็บ ID ดิบใน key, log หรือ metadata."""
    return hashlib.sha256(
        ((user_id or "unknown") + USER_ID_SALT).encode("utf-8")
    ).hexdigest()[:32]


class InMemoryBackend:
    """Backend เริ่มต้น — อยู่ใน process เดียว ไม่แชร์ข้าม worker."""

    name = "memory"
    is_shared = False

    def __init__(self):
        self._data: dict[str, list] = {}   # key -> [value, expire_at]
        self._lock = threading.Lock()
        self._last_cleanup = time.time()

    def _purge(self, now: float):
        """ลบ key ที่หมดอายุ — ทำทุก 60 วินาที ป้องกัน memory leak."""
        if now - self._last_cleanup < 60:
            return
        expired = [k for k, v in self._data.items() if v[1] <= now]
        for k in expired:
            self._data.pop(k, None)
        self._last_cleanup = now

    def set_if_absent(self, key: str, ttl: int) -> bool:
        """ตั้งค่าเฉพาะเมื่อยังไม่มี key — atomic ภายใน process."""
        now = time.time()
        with self._lock:
            self._purge(now)
            entry = self._data.get(key)
            if entry and entry[1] > now:
                return False
            self._data[key] = [1, now + ttl]
            return True

    def incr(self, key: str, ttl: int) -> int:
        now = time.time()
        with self._lock:
            self._purge(now)
            entry = self._data.get(key)
            if entry and entry[1] > now:
                entry[0] += 1
                return entry[0]
            self._data[key] = [1, now + ttl]
            return 1

    def decr(self, key: str) -> None:
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry and entry[1] > now and entry[0] > 0:
                entry[0] -= 1


class RedisBackend:
    """Backend ที่แชร์ข้าม worker — ใช้คำสั่ง atomic ของ Redis."""

    name = "redis"
    is_shared = True

    def __init__(self, client):
        self._c = client

    def set_if_absent(self, key: str, ttl: int) -> bool:
        # SET key 1 NX EX ttl — atomic ในคำสั่งเดียว
        return bool(self._c.set(key, 1, nx=True, ex=ttl))

    def incr(self, key: str, ttl: int) -> int:
        value = int(self._c.incr(key))
        if value == 1:
            self._c.expire(key, ttl)
        return value

    def decr(self, key: str) -> None:
        self._c.decr(key)


class StateStore:
    """เลือก backend และจัดการเมื่อ Redis ใช้ไม่ได้ (fail-open + fallback).

    สถานะที่เป็นไปได้:
      not_configured = ยังไม่ตั้ง REDIS_URL (ค่าเริ่มต้นใน Phase 1)
      unavailable    = ตั้ง REDIS_URL แล้วแต่ไม่มี redis package
      connected      = เชื่อมต่อ Redis สำเร็จ
      degraded       = เคยเชื่อมได้/ตั้งค่าไว้ แต่เรียกใช้ไม่สำเร็จ → ใช้ fallback
    """

    def __init__(self, redis_url: str = ""):
        self._fallback = InMemoryBackend()
        self._redis = None
        self._status = "not_configured"
        self._error = None
        if redis_url:
            self._connect(redis_url)

    def _connect(self, url: str):
        try:
            import redis as redis_lib   # optional — ไม่อยู่ใน requirements.txt
        except ImportError:
            self._status = "unavailable"
            self._error = "redis package ไม่ได้ติดตั้ง"
            logger.warning(f"Redis: {self._error} — ใช้ in-memory fallback")
            return
        try:
            client = redis_lib.Redis.from_url(
                url,
                socket_connect_timeout=2,   # timeout สั้น — ไม่ให้ Redis ที่ค้าง
                socket_timeout=2,           # ทำให้ LINE webhook timeout ตาม
                retry_on_timeout=False,
                health_check_interval=30,
                decode_responses=True,
            )
            client.ping()
            self._redis = RedisBackend(client)
            self._status = "connected"
            logger.info("Redis: เชื่อมต่อสำเร็จ — dedup/rate limit แชร์ข้าม worker")
        except Exception as e:
            self._status = "degraded"
            self._error = type(e).__name__
            logger.error(f"Redis: เชื่อมต่อไม่สำเร็จ ({self._error}) — ใช้ in-memory fallback")

    @property
    def status(self) -> str:
        return self._status

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def is_shared(self) -> bool:
        """True เฉพาะเมื่อ backend แชร์ข้าม worker ได้จริง."""
        return self._redis is not None

    def _call(self, method: str, *args):
        if self._redis is not None:
            try:
                return getattr(self._redis, method)(*args)
            except Exception as e:
                self._status = "degraded"
                self._error = type(e).__name__
                self._redis = None
                logger.error(
                    f"Redis ใช้ไม่ได้ ({self._error}) — สลับไป in-memory fallback"
                )
        return getattr(self._fallback, method)(*args)

    def set_if_absent(self, key: str, ttl: int) -> bool:
        return self._call("set_if_absent", key, ttl)

    def incr(self, key: str, ttl: int) -> int:
        return self._call("incr", key, ttl)

    def decr(self, key: str) -> None:
        return self._call("decr", key)


class DedupGuard:
    """ป้องกันการประมวลผลซ้ำจาก LINE retry และ event ที่ส่งซ้ำ.

    LINE ระบุว่า event เดียวกันอาจถูกส่งมามากกว่าหนึ่งครั้ง และ event ที่
    ส่งซ้ำจะมี webhookEventId เดิม ต่างกันเฉพาะ deliveryContext.isRedelivery
    """

    def __init__(self, store: StateStore):
        self._store = store

    def is_new_event(self, event_id: str) -> bool:
        if not event_id:
            return True     # ไม่มี id → ให้ชั้นอื่น (message id / sha256) กันแทน
        return self._store.set_if_absent(f"dedup:evt:{event_id}", DEDUP_TTL_SECONDS)

    def is_new_message(self, message_id: str) -> bool:
        if not message_id:
            return True
        return self._store.set_if_absent(f"dedup:msg:{message_id}", DEDUP_TTL_SECONDS)

    def is_new_content(self, user_hash: str, content_sha256: str) -> bool:
        """กันผู้ใช้ส่งไฟล์เนื้อหาเดิมซ้ำด้วยมือ — หน้าต่างสั้นเพียง 10 นาที
        เพราะผู้ป่วยอาจตั้งใจส่งเอกสารหน้าตาเหมือนกันคนละรอบจริง ๆ
        """
        key = f"dedup:sha:{user_hash}:{content_sha256}"
        return self._store.set_if_absent(key, DEDUP_CONTENT_TTL_SECONDS)


class RateLimiter:
    """จำกัดจำนวนไฟล์ต่อผู้ส่ง — นับ 3 หน้าต่างเวลาพร้อมกัน.

    เพิ่มตัวนับก่อนแล้วตรวจ ถ้าเกินจะคืนค่า (decr) ทุก key ที่เพิ่งเพิ่มไป
    เพื่อไม่ให้คำขอที่ถูกปฏิเสธกิน quota ของหน้าต่างอื่น
    """

    def __init__(self, store: StateStore):
        self._store = store

    def _limit(self, configured: int) -> int:
        """ลดเพดานครึ่งหนึ่งเมื่อ backend ไม่ได้แชร์ข้าม worker.

        Gunicorn 2 workers ที่นับแยกกันจะทำให้เพดานรวมเป็น 2 เท่า
        การหารสองทำให้เพดานรวมใกล้เคียงค่าที่ตั้งไว้
        """
        if self._store.is_shared:
            return configured
        return max(1, configured // 2)

    def check(self, user_hash: str) -> tuple[bool, str]:
        now = int(time.time())
        windows = [
            (f"rl:{user_hash}:m:{now // 60}", 60,
             self._limit(RATE_LIMIT_PER_MINUTE), "นาที"),
            (f"rl:{user_hash}:h:{now // 3600}", 3600,
             self._limit(RATE_LIMIT_PER_HOUR), "ชั่วโมง"),
            (f"rl:{user_hash}:d:{now // 86400}", 86400,
             self._limit(RATE_LIMIT_PER_DAY), "วัน"),
        ]
        incremented: list[str] = []
        for key, ttl, limit, unit in windows:
            value = self._store.incr(key, ttl)
            incremented.append(key)
            if value > limit:
                for k in incremented:
                    self._store.decr(k)
                return False, (
                    f"ส่งเอกสารได้ไม่เกิน {limit} ไฟล์ต่อ{unit}\n"
                    f"กรุณารอสักครู่แล้วส่งใหม่อีกครั้ง"
                )
        return True, "OK"


state_store = StateStore(REDIS_URL)
dedup_guard = DedupGuard(state_store)
rate_limiter = RateLimiter(state_store)

# จำกัดงาน OCR ที่ทำพร้อมกันต่อ worker — ป้องกัน OOM บน instance เล็ก
_ocr_semaphore = threading.Semaphore(max(1, OCR_MAX_CONCURRENT))


# ═══════════════════════════════════════════════════════════
# File Validation
# ═══════════════════════════════════════════════════════════

MAGIC_BYTES = {
    b"\xff\xd8\xff":          ".jpg",   # JPEG
    b"\x89PNG\r\n\x1a\n":    ".png",   # PNG
    b"%PDF":                  ".pdf",   # PDF
}


def looks_like_utf8_text(filepath: str) -> bool:
    """ตรวจว่าเป็นไฟล์ข้อความ UTF-8 ที่ถูกต้องและไม่มี null byte.

    ตรวจทั้งไฟล์ ไม่ใช่แค่ 1024 ไบต์แรก — ไฟล์ binary ที่ขึ้นต้นด้วยข้อความ
    จะถูกจับได้ที่ไบต์ถัดไป และ null byte เป็นสัญญาณชัดว่าไม่ใช่ไฟล์ข้อความ
    """
    try:
        with open(filepath, "rb") as f:
            data = f.read(MAX_FILE_SIZE + 1)
    except OSError:
        return False

    if not data:
        return False
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def detect_file_type(filepath: str) -> str | None:
    """ตรวจสอบชนิดไฟล์จริงจาก magic bytes."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(8)
    except OSError:
        return None

    for magic, ext in MAGIC_BYTES.items():
        if header[:len(magic)] == magic:
            return ext

    if looks_like_utf8_text(filepath):
        return ".txt"

    return None


def validate_file(filepath: str) -> tuple[bool, str]:
    """ตรวจสอบไฟล์ก่อนประมวลผล.

    ข้อความที่คืนกลับต้องไม่มีชื่อไฟล์หรือข้อมูลส่วนบุคคล
    เพราะถูกนำไปบันทึกลง log และ classification record
    """
    try:
        size = os.path.getsize(filepath)
    except OSError:
        return False, "ไม่สามารถอ่านขนาดไฟล์ได้"

    if size == 0:
        return False, "ไฟล์ว่างเปล่า"

    if size > MAX_FILE_SIZE:
        return False, f"ไฟล์ใหญ่เกิน {MAX_FILE_SIZE // (1024*1024)} MB ({size:,} bytes)"

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, f"ไม่รองรับนามสกุล {ext}"

    if ext != ".txt" and size < MIN_BINARY_FILE_SIZE:
        return False, f"ไฟล์ {ext} เล็กผิดปกติ ({size} bytes) — อาจเสียหาย"

    if ext == ".txt":
        # เดิมข้าม .txt ไปเลย ทำให้ไฟล์ binary ที่เปลี่ยนนามสกุลเป็น .txt ผ่านได้
        if not looks_like_utf8_text(filepath):
            return False, "นามสกุล .txt แต่เนื้อหาไม่ใช่ข้อความ UTF-8"
    else:
        real_type = detect_file_type(filepath)
        if real_type is None:
            return False, "ไม่สามารถระบุชนิดไฟล์จริงได้"
        expected = ext if ext != ".jpeg" else ".jpg"
        if real_type != expected:
            return False, f"นามสกุล {ext} ไม่ตรงกับชนิดไฟล์จริง ({real_type})"

    return True, "OK"


# ═══════════════════════════════════════════════════════════
# PDF Helpers
# ═══════════════════════════════════════════════════════════

def extract_pdf_text(filepath: str) -> str:
    """ดึงข้อความจาก PDF ด้วย pypdf."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(filepath)
        num_pages = len(reader.pages)
        logger.info(f"PDF มี {num_pages} หน้า")

        texts = []
        for page in reader.pages[:MAX_PDF_PAGES]:
            text = page.extract_text() or ""
            texts.append(text.strip())

        return "\n\n".join(t for t in texts if t)
    except ImportError:
        logger.warning("pypdf ไม่ได้ติดตั้ง")
        return ""
    except Exception as e:
        # ไม่ log str(e) — OSError จาก pypdf มี path พร้อมชื่อไฟล์
        logger.error(f"อ่าน PDF ล้มเหลว: {type(e).__name__}")
        return ""


def get_pdf_page_count(filepath: str) -> int:
    """นับจำนวนหน้า PDF."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(filepath).pages)
    except Exception:
        return 0


def is_scanned_pdf(filepath: str) -> bool:
    """ตรวจว่า PDF เป็นแบบสแกน (ไม่มีข้อความ)."""
    text = extract_pdf_text(filepath)
    return len(text.strip()) < 50


# ═══════════════════════════════════════════════════════════
# OCR — Tesseract (tha+eng)
# ═══════════════════════════════════════════════════════════

MAX_OCR_PAGES = int(os.getenv("MAX_OCR_PAGES", "5"))


def extract_text_with_ocr(filepath: str) -> str:
    """ดึงข้อความด้วย Tesseract OCR (tha+eng).

    การจัดการหน่วยความจำ:
      - render ที่ OCR_DPI (ค่าเริ่มต้น 200) แทน 300 — ลดหน่วยความจำราว 56%
        (A4 ที่ 300 DPI ≈ 26 MB ต่อ buffer, ที่ 200 DPI ≈ 11.6 MB)
      - คืนหน่วยความจำของ pixmap และ image ทันทีหลังจบแต่ละหน้า
        แทนที่จะสะสมไว้จนจบลูป → พีคเท่ากับหนึ่งหน้า ไม่ใช่ทุกหน้ารวมกัน
      - จำกัดงาน OCR พร้อมกันต่อ worker ด้วย semaphore — ป้องกัน OOM
        บน Render instance 512 MB ที่รัน 2 workers
    """
    if not HAS_TESSERACT or not HAS_PIL:
        logger.warning("OCR ไม่พร้อม — ขาด pytesseract หรือ Pillow")
        return ""

    ext = os.path.splitext(filepath)[1].lower()
    ocr_lang = "tha+eng"
    scale = OCR_DPI / 72

    with _ocr_semaphore:
        try:
            if ext in (".jpg", ".jpeg", ".png"):
                img = None
                try:
                    img = Image.open(filepath)
                    text = pytesseract.image_to_string(img, lang=ocr_lang)
                finally:
                    if img is not None:
                        img.close()
                logger.info(f"OCR ภาพ: ได้ {len(text.strip())} ตัวอักษร")
                return text.strip()

            if ext == ".pdf":
                if not HAS_PYMUPDF:
                    logger.warning("PyMuPDF ไม่ได้ติดตั้ง — ไม่สามารถ OCR PDF สแกนได้")
                    return ""

                doc = pymupdf.open(filepath)
                try:
                    num_pages = min(len(doc), MAX_OCR_PAGES)
                    texts = []

                    for i in range(num_pages):
                        pix = None
                        img = None
                        try:
                            page = doc[i]
                            mat = pymupdf.Matrix(scale, scale)
                            pix = page.get_pixmap(matrix=mat)
                            img = Image.frombytes(
                                "RGB", [pix.width, pix.height], pix.samples
                            )
                            page_text = pytesseract.image_to_string(img, lang=ocr_lang)
                            if page_text.strip():
                                texts.append(page_text.strip())
                        finally:
                            # คืนหน่วยความจำทันที ไม่รอ garbage collector
                            if img is not None:
                                img.close()
                            if pix is not None:
                                pix = None
                finally:
                    doc.close()

                full_text = "\n\n".join(texts)
                logger.info(
                    f"OCR PDF สแกน ({num_pages} หน้า @ {OCR_DPI} DPI): "
                    f"ได้ {len(full_text)} ตัวอักษร"
                )
                return full_text

        except Exception as e:
            # ไม่ log filepath หรือ str(e) — ทั้งสองอย่างอาจมีชื่อไฟล์ของผู้ป่วย
            logger.error(f"OCR ล้มเหลว: {type(e).__name__}")

    return ""


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

def verify_signature(body: bytes, signature: str) -> bool:
    """ตรวจสอบ LINE Webhook Signature (HMAC-SHA256)."""
    if not LINE_CHANNEL_SECRET:
        logger.error("LINE_CHANNEL_SECRET ไม่ได้ตั้งค่า")
        return False
    mac = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    )
    expected = base64.b64encode(mac.digest()).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def push_message(user_id: str, text: str):
    """ส่งข้อความ Push ไปยัง LINE user.

    ใช้สำหรับแจ้งเตือนผลตรวจผิดปกติ
    ไม่ log user_id หรือเนื้อหาข้อความ
    """
    if IS_TESTING and not IS_TESTING_DOCS:
        logger.info("TESTING MODE: ไม่ส่ง push message")
        return
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return
    if not user_id:
        return
    url = "https://api.line.me/v2/bot/message/push"
    request_body = json.dumps(
        {
            "to": user_id,
            "messages": [{"type": "text", "text": text[:5000]}],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        resp = http_requests.post(
            url,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=request_body,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"Push message สำเร็จ (status={resp.status_code})")
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "-")
        logger.error(
            f"Push message ไม่สำเร็จ: {type(e).__name__} (status={status})"
        )


def reply_message(reply_token: str, text: str):
    """ส่งข้อความตอบกลับไปยัง LINE."""
    if IS_TESTING and not IS_TESTING_DOCS:
        logger.info("TESTING MODE: ไม่ส่งข้อความตอบกลับ LINE")
        return
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return
    url = "https://api.line.me/v2/bot/message/reply"
    request_body = json.dumps(
        {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text[:5000]}],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        resp = http_requests.post(
            url,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=request_body,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"ตอบกลับ LINE สำเร็จ (status={resp.status_code})")
    except Exception as e:
        # ไม่ log str(e) — exception ของ requests อาจมี URL และบริบทของ request
        status = getattr(getattr(e, "response", None), "status_code", "-")
        logger.error(
            f"ตอบกลับ LINE ไม่สำเร็จ: {type(e).__name__} (status={status})"
        )


def reply_line_message(reply_token: str, message: dict):
    """ส่ง message object (Flex, Image ฯลฯ) ตอบกลับไปยัง LINE.

    ใช้สำหรับ non-text messages เช่น Flex Message
    ไม่ log เนื้อหา message เพื่อไม่เปิดเผย URL หรือข้อมูลผู้ป่วย
    """
    if IS_TESTING and not IS_TESTING_DOCS:
        logger.info("TESTING MODE: ไม่ส่งข้อความตอบกลับ LINE")
        return
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return
    url = "https://api.line.me/v2/bot/message/reply"
    request_body = json.dumps(
        {
            "replyToken": reply_token,
            "messages": [message],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        resp = http_requests.post(
            url,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=request_body,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"ตอบกลับ LINE (flex) สำเร็จ (status={resp.status_code})")
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "-")
        logger.error(
            f"ตอบกลับ LINE (flex) ไม่สำเร็จ: {type(e).__name__} (status={status})"
        )


def download_line_content(message_id: str) -> bytes | None:
    """ดาวน์โหลดไฟล์จาก LINE Content API."""
    if IS_TESTING and not IS_TESTING_DOCS:
        logger.info("TESTING MODE: ไม่ดาวน์โหลดไฟล์จาก LINE")
        return None
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    try:
        resp = http_requests.get(url, headers=headers, timeout=30, stream=True)
        resp.raise_for_status()
        content = resp.content
        if len(content) > MAX_FILE_SIZE:
            logger.warning(f"ไฟล์ใหญ่เกิน {MAX_FILE_SIZE // (1024*1024)} MB — ปฏิเสธ")
            return None
        return content
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "-")
        logger.error(
            f"ดาวน์โหลดไฟล์จาก LINE ล้มเหลว: {type(e).__name__} "
            f"(status={status}, msg_id={message_id})"
        )
        return None


def get_content_type_ext(message_id: str) -> str:
    """ตรวจสอบ Content-Type เพื่อกำหนดนามสกุลไฟล์."""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    try:
        resp = http_requests.head(url, headers=headers, timeout=10)
        ct = resp.headers.get("Content-Type", "")
        if "pdf" in ct:
            return ".pdf"
        elif "png" in ct:
            return ".png"
        elif "jpeg" in ct or "jpg" in ct:
            return ".jpg"
    except Exception:
        pass
    return ".dat"


def normalize_incoming_extension(raw_name_or_ext: str) -> str:
    """ดึงนามสกุลที่ปลอดภัยจากชื่อไฟล์หรือนามสกุลที่ผู้ใช้ส่งมา.

    ชื่อไฟล์ต้นฉบับถูกใช้เพื่อดูนามสกุลเท่านั้น ไม่ถูกนำไปตั้งชื่อไฟล์จัดเก็บ
    """
    raw = (raw_name_or_ext or "")[:MAX_FILENAME_LENGTH]
    ext = raw if raw.startswith(".") else os.path.splitext(raw)[1]
    ext = ext.lower().strip()
    return ext if ext in ALLOWED_EXTENSIONS else ""


def build_storage_filename(ext: str) -> tuple[str, str]:
    """สร้างชื่อไฟล์สุ่มที่ไม่ระบุตัวบุคคล.

    ชื่อไฟล์ต้นฉบับจากผู้ใช้มักมีชื่อผู้ป่วย (เช่น "สมชาย_ผลเลือด.pdf")
    จึงไม่นำมาใช้เป็นชื่อจัดเก็บและไม่บันทึกลง log

    UUID v4 มีความสุ่ม 122 บิต — ชื่อไฟล์ซ้ำกันแทบเป็นไปไม่ได้
    จึงไม่ต้องพึ่ง timestamp ต่อท้ายอีกต่อไป

    คืนค่า (doc_id, filename)
    """
    safe_ext = ext if ext in ALLOWED_EXTENSIONS else ".dat"
    doc_id = str(uuid.uuid4())
    return doc_id, f"{doc_id}{safe_ext}"


def content_sha256(data: bytes) -> str:
    """hash เนื้อหาไฟล์ — ใช้ตรวจไฟล์ซ้ำและยืนยันความถูกต้อง."""
    return hashlib.sha256(data).hexdigest()


def build_object_metadata(
    doc_id: str,
    category: str,
    ext: str,
    size_bytes: int,
    sha256_hex: str,
) -> dict:
    """สร้าง metadata สำหรับ GCS object.

    ต้องไม่มีข้อมูลส่วนบุคคล — ไม่เก็บชื่อไฟล์ต้นฉบับ, LINE User ID,
    ชื่อผู้ป่วย หรือเนื้อหาเอกสาร
    """
    return {
        "doc_id": doc_id,
        "category": category,
        "ext": ext,
        "size_bytes": str(size_bytes),
        "content_sha256": sha256_hex,
        "received_at": datetime.now().isoformat(timespec="seconds"),
        "source": "line_oa",
        "classifier": "internal",
    }


def save_to_inbox(data: bytes, filename: str) -> str:
    """บันทึกไฟล์ลง inbox (ephemeral disk).

    filename ควรเป็นชื่อแบบ UUID จาก build_storage_filename
    """
    # ป้องกัน path traversal — ใช้เฉพาะชื่อไฟล์ ไม่รวม path
    filename = os.path.basename(filename)
    if not filename:
        filename = f"unnamed_{datetime.now():%Y%m%d_%H%M%S}.dat"
    if len(filename) > MAX_FILENAME_LENGTH:
        name, ext = os.path.splitext(filename)
        filename = name[: max(1, MAX_FILENAME_LENGTH - len(ext))] + ext
    inbox = os.path.join(BASE_DIR, FOLDERS["inbox"])
    os.makedirs(inbox, exist_ok=True)
    filepath = os.path.join(inbox, filename)
    if os.path.exists(filepath):
        name, ext = os.path.splitext(filename)
        filepath = os.path.join(
            inbox, f"{name}_{datetime.now():%Y%m%d_%H%M%S}{ext}"
        )
    with open(filepath, "wb") as f:
        f.write(data)
    # ไม่ log path เต็ม — log เฉพาะ basename ซึ่งเป็น UUID และขนาดไฟล์
    logger.info(f"บันทึกไฟล์ inbox: {os.path.basename(filepath)} ({len(data):,} bytes)")
    return filepath


# ═══════════════════════════════════════════════════════════
# Claude API Classification
# ═══════════════════════════════════════════════════════════

CLASSIFY_PROMPT = (
    "จำแนกประเภทเอกสารทางการแพทย์นี้เป็นหนึ่งในหมวดต่อไปนี้:\n"
    '- "bills" = ใบเสร็จรับเงิน ใบแจ้งหนี้ ใบกำกับภาษี\n'
    '- "rx" = ใบสั่งยา รายการยา\n'
    '- "lab" = ผลตรวจทางห้องปฏิบัติการ ผลเลือด\n'
    '- "dialysis" = บันทึกการฟอกเลือด/ฟอกไต\n'
    '- "review" = ไม่สามารถจำแนกได้ / ไม่ใช่เอกสารทางการแพทย์\n\n'
    "ตอบเป็น JSON เท่านั้น ในรูปแบบ:\n"
    '{"category": "...", "confidence": 0.0-1.0, "reason": "..."}\n'
    "ห้ามตอบข้อความอื่นนอกจาก JSON"
)


def call_claude_api(messages_content: list) -> dict | None:
    """เรียก Claude API และ parse JSON response."""
    try:
        request_body = json.dumps(
            {
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 256,
                "messages": [
                    {"role": "user", "content": messages_content}
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")

        resp = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json; charset=utf-8",
            },
            data=request_body,
            timeout=60,
        )
        if resp.status_code != 200:
            # ไม่ log response body — error ของ API อาจสะท้อนเนื้อหา request
            # ซึ่งเป็น base64 ของเอกสารผู้ป่วยกลับมา
            logger.error(f"Claude API HTTP {resp.status_code}")
            resp.raise_for_status()

        result = resp.json()
        answer_text = result["content"][0]["text"].strip()
        if answer_text.startswith("```"):
            answer_text = answer_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        classification = json.loads(answer_text)
        valid_categories = {"bills", "rx", "lab", "dialysis", "review"}
        if classification.get("category") not in valid_categories:
            classification["category"] = "review"

        if classification.get("confidence", 1.0) < 0.5:
            classification["category"] = "review"
            classification["reason"] = (
                classification.get("reason", "") + " (confidence ต่ำ)"
            )

        return classification

    except json.JSONDecodeError:
        logger.warning("Claude ตอบไม่เป็น JSON")
        return None
    except Exception as e:
        # เส้นทางนี้ถูกล็อกไว้แล้ว แต่ยังต้องปลอดภัยหากมีการเปิดใช้ในอนาคต
        # — str(e) อาจมีเนื้อหา request ซึ่งเป็น base64 ของเอกสารผู้ป่วย
        logger.error(f"Claude API error: {type(e).__name__}")
        return None


def classify_with_claude(filepath: str) -> dict:
    """ส่งเอกสารไปให้ Claude API จำแนกประเภท.

    ถูกล็อกไว้ที่ระดับโค้ดด้วย EXTERNAL_AI_CLASSIFICATION_ALLOWED
    เพื่อไม่ให้การตั้งค่า ENV เปิดการส่งข้อมูลผู้ป่วยออกนอกระบบได้
    """
    if not EXTERNAL_AI_CLASSIFICATION_ALLOWED:
        logger.info("AI ภายนอกปิดอยู่ที่ระดับโค้ด — ใช้การจำแนกภายใน")
        return classify_with_keywords(filepath)

    if IS_TESTING:
        logger.info("TESTING MODE: ไม่เรียก Claude API — ใช้ keyword classification แทน")
        return classify_with_keywords(filepath)

    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY ไม่ได้ตั้งค่า — ใช้ keyword classification แทน")
        return classify_with_keywords(filepath)

    ext = os.path.splitext(filepath)[1].lower()
    messages_content = []

    try:
        if ext in (".jpg", ".jpeg", ".png"):
            with open(filepath, "rb") as f:
                img_data = base64.b64encode(f.read()).decode("utf-8")
            media_type = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png",
            }.get(ext, "image/jpeg")
            messages_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": img_data,
                },
            })

        elif ext == ".pdf":
            page_count = get_pdf_page_count(filepath)
            if page_count > MAX_PDF_PAGES:
                return {
                    "category": "review",
                    "reason": f"PDF มี {page_count} หน้า เกินขีดจำกัด",
                }

            with open(filepath, "rb") as f:
                pdf_data = base64.b64encode(f.read()).decode("utf-8")
            messages_content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_data,
                },
            })

        elif ext in (".txt", ".csv"):
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(10000)
            if not text.strip():
                return {"category": "review", "reason": "ไฟล์ข้อความว่างเปล่า"}
            messages_content.append({"type": "text", "text": text})

        else:
            return {"category": "review", "reason": f"ไม่รองรับนามสกุล {ext}"}

        messages_content.append({"type": "text", "text": CLASSIFY_PROMPT})
        result = call_claude_api(messages_content)
        if result:
            logger.info(
                f"Claude จำแนก: {os.path.basename(filepath)} → "
                f"{result['category']} (confidence: {result.get('confidence', '?')})"
            )
            return result
        else:
            logger.warning("Claude ตอบไม่ได้ — fallback ไป keyword")
            return classify_with_keywords(filepath)

    except Exception as e:
        logger.error(f"classify_with_claude error: {type(e).__name__}")
        return classify_with_keywords(filepath)


def classify_with_keywords(filepath: str) -> dict:
    """Fallback: จำแนกด้วย keyword matching + OCR."""
    ext = os.path.splitext(filepath)[1].lower()

    if ext in (".jpg", ".jpeg", ".png"):
        content = extract_text_with_ocr(filepath)
        if not content or len(content.strip()) < 20:
            return {
                "category": "review",
                "reason": "OCR อ่านข้อความจากภาพไม่ได้ — ต้องใช้ AI ตรวจสอบ",
            }
        logger.info(f"OCR ภาพสำเร็จ — ได้ข้อความ {len(content.strip())} ตัวอักษร")

    elif ext == ".pdf":
        content = extract_pdf_text(filepath)
        if not content or len(content.strip()) < 50:
            logger.info("PDF ไม่มีข้อความเพียงพอ — ลอง OCR...")
            content = extract_text_with_ocr(filepath)
            if not content or len(content.strip()) < 20:
                return {
                    "category": "review",
                    "reason": "PDF สแกน — OCR อ่านไม่ได้ ต้องใช้ AI ตรวจสอบ",
                }

    elif ext in (".txt", ".csv"):
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            return {"category": "review", "reason": "อ่านไฟล์ไม่ได้"}
    else:
        return {"category": "review", "reason": f"ไม่รองรับนามสกุล {ext}"}

    content_lower = content.lower()

    # Log เฉพาะจำนวนตัวอักษร — ห้ามบันทึกเนื้อหาเอกสารลง log
    logger.info(f"OCR extracted: {len(content.strip())} chars")

    rules = {
        "bills": (
            ["ใบเสร็จ", "ใบเสร็จรับเงิน", "ชำระเงิน", "จำนวนเงิน",
             "ค่าตรวจ", "ค่ายา", "ค่ารักษา", "invoice", "receipt",
             "ยอดรวม", "ราคา", "ส่วนลด", "ค่าบริการ", "ค่าเวชภัณฑ์",
             "ค่าห้อง", "ค่าแพทย์", "เงินสด", "โอนเงิน", "บาท",
             "ภาษี", "vat", "tax", "ใบกำกับ", "ใบแจ้งหนี้",
             "ค่าใช้จ่าย", "รวมทั้งสิ้น", "ชื่อผู้ป่วย", "เลขที่ใบเสร็จ",
             "ค่าเบิก", "สิทธิ์", "เบิกได้", "ส่วนเกิน"],
            2,
        ),
        "rx": (
            ["ใบสั่งยา", "prescription", "รายการยา", "mg", "mcg",
             "1x1", "1x2", "หลังอาหาร", "ก่อนอาหาร", "เม็ด",
             "tablet", "capsule", "แคปซูล", "ขนาดยา",
             "วันละ", "ครั้งละ", "เช้า", "กลางวัน", "เย็น",
             "ก่อนนอน", "tab", "cap", "sig", "prn",
             "ยา", "สั่งยา", "จ่ายยา", "เภสัชกร", "pharmacist"],
            2,
        ),
        "lab": (
            ["ผลตรวจ", "laboratory", "lab report", "ผลเลือด",
             "BUN", "creatinine", "eGFR", "hemoglobin",
             "ค่าปกติ", "mg/dL", "mg/dl", "CBC", "potassium",
             "result", "reference", "normal", "abnormal",
             "hematocrit", "WBC", "RBC", "platelet", "sodium",
             "albumin", "glucose", "cholesterol", "HbA1c",
             "uric acid", "calcium", "phosphorus", "ค่า",
             "ห้องปฏิบัติการ", "ผลแล็บ", "ผลlab", "ผล lab",
             "specimen", "serum", "blood", "urine",
             "mmol", "umol", "g/dl", "g/dL"],
            2,
        ),
        "dialysis": (
            ["ฟอกเลือด", "ฟอกไต", "hemodialysis", "dialysis",
             "blood flow", "UF goal", "บันทึกการฟอก",
             "น้ำหนักแห้ง", "dry weight", "dialysate",
             "HD", "heparin", "dialyzer", "QB", "QD",
             "pre-HD", "post-HD", "KT/V", "Kt/V",
             "URR", "access", "fistula", "catheter",
             "ultrafiltration", "UF", "transmembrane",
             "ไตเทียม", "ล้างไต", "บันทึกฟอก"],
            2,
        ),
    }

    best, best_score = "review", 0
    all_matches = {}
    for cat, (kws, min_match) in rules.items():
        matched = [kw for kw in kws if kw.lower() in content_lower]
        score = len(matched)
        all_matches[cat] = matched
        if score >= min_match and score > best_score:
            best, best_score = cat, score

    # log เฉพาะจำนวน hits — ไม่ log รายการคำที่ match
    # เพราะคำที่พบเปิดเผยเนื้อหาเชิงคลินิกของเอกสาร
    for cat, matched in all_matches.items():
        if matched:
            logger.info(f"  keyword [{cat}]: {len(matched)} hits")

    return {
        "category": best,
        "reason": f"keyword match ({best_score} hits)",
        "confidence": min(best_score / 5.0, 1.0) if best_score > 0 else 0.0,
    }


def copy_to_category(
    src_path: str, category: str, metadata: dict | None = None
) -> str:
    """คัดลอกไฟล์ไปยังโฟลเดอร์ตามประเภท + cloud storage.

    Production mode: ถ้า cloud upload ล้มเหลว จะ raise RuntimeError
    เพื่อให้ process_file_async() จับแล้วแจ้งข้อผิดพลาดกลับไป
    """
    # Local copy (ephemeral — เป็น temp ระหว่างรอ upload)
    dest_folder = os.path.join(BASE_DIR, FOLDERS.get(category, FOLDERS["review"]))
    os.makedirs(dest_folder, exist_ok=True)
    filename = os.path.basename(src_path)
    dest_path = os.path.join(dest_folder, filename)
    if os.path.exists(dest_path):
        name, ext = os.path.splitext(filename)
        dest_path = os.path.join(
            dest_folder, f"{name}_{datetime.now():%Y%m%d_%H%M%S}{ext}"
        )
    shutil.copy2(src_path, dest_path)
    # ไม่ log path เต็ม
    logger.info(f"คัดลอก → {FOLDERS.get(category, FOLDERS['review'])}")

    # Cloud storage (ถ้าตั้งค่าไว้ และ backend ไม่ใช่ local)
    cloud_path = None
    if STORAGE_BACKEND != "local":
        try:
            with open(src_path, "rb") as f:
                data = f.read()
            cloud_path = storage.save(data, category, filename, metadata)
        except RuntimeError:
            # Production mode — GCS ล้มเหลว ห้าม fallback
            raise
        except Exception as e:
            logger.error(f"Cloud storage error: {type(e).__name__}")
            if IS_PRODUCTION:
                raise RuntimeError(
                    f"จัดเก็บเอกสารใน GCS ล้มเหลว: {type(e).__name__}"
                )

    return cloud_path if cloud_path and cloud_path.startswith("gs://") else dest_path


def process_file_async(
    filepath: str,
    reply_token: str | None = None,
    doc_id: str | None = None,
    sha256_hex: str | None = None,
    owner_hash: str | None = None,
    line_user_id: str = "",
):
    """ประมวลผลไฟล์ในเธรดแยก.

    doc_id และ sha256_hex มาจากตอนรับไฟล์ ถ้าไม่ส่งมาจะอนุมานจากชื่อไฟล์
    (ชื่อไฟล์เป็น UUID อยู่แล้ว) เพื่อให้เรียกใช้แบบเดิมได้

    owner_hash: SHA-256 hash ของ LINE user ID ของผู้ส่ง — ใช้สำหรับ
    cross-patient access prevention (ผู้ใช้เห็นเฉพาะเอกสารที่ตัวเองส่ง)

    line_user_id: LINE user ID ดิบ — ใช้สำหรับ push_message กลับไปหาผู้ใช้
    เมื่อพบผลตรวจผิดปกติ (Lab Alert) ไม่ถูกบันทึกลง log หรือ metadata
    """
    _doc_id = doc_id or os.path.splitext(os.path.basename(filepath))[0]

    def _run():
        try:
            is_valid, reason = validate_file(filepath)
            if not is_valid:
                logger.warning(f"ไฟล์ไม่ผ่านการตรวจสอบ: {reason}")
                result = {"category": "review", "reason": reason}
            elif ENABLE_AI_CLASSIFY:
                result = classify_with_claude(filepath)
            else:
                result = classify_with_keywords(filepath)

            category = result.get("category", "review")
            ext_now = os.path.splitext(filepath)[1].lower()
            try:
                size_now = os.path.getsize(filepath)
            except OSError:
                size_now = 0

            metadata = build_object_metadata(
                doc_id=_doc_id,
                category=category,
                ext=ext_now,
                size_bytes=size_now,
                sha256_hex=sha256_hex or "",
            )

            # ── Document Search V1: extract text for indexing ──
            extracted_text = ""
            ocr_status = "none"
            try:
                if ext_now == ".txt":
                    try:
                        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                            extracted_text = f.read()
                        ocr_status = "native"
                    except OSError:
                        ocr_status = "failed"
                elif ext_now == ".pdf":
                    extracted_text = extract_pdf_text(filepath)
                    if len(extracted_text.strip()) < 50:
                        # scanned PDF → OCR
                        extracted_text = extract_text_with_ocr(filepath)
                        ocr_status = "ocr"
                    else:
                        ocr_status = "native"
                elif ext_now in (".jpg", ".jpeg", ".png"):
                    extracted_text = extract_text_with_ocr(filepath)
                    ocr_status = "ocr"
            except Exception as ocr_err:
                logger.warning(
                    f"Text extraction ล้มเหลว: {type(ocr_err).__name__} "
                    f"(doc_id={_doc_id})"
                )
                ocr_status = "failed"
            extracted_text = extracted_text[:MAX_INDEX_TEXT_LENGTH]

            dest = copy_to_category(filepath, category, metadata=metadata)

            # ── Document Search V1: add to index after GCS upload ──
            stored_in_gcs = bool(dest and dest.startswith("gs://"))
            index_saved = False
            if stored_in_gcs:
                # Extract GCS object path from gs:// URI
                gcs_object_path = dest.replace(
                    f"gs://{STORAGE_BUCKET}/", ""
                ) if STORAGE_BUCKET else ""
                mime_map = {
                    ".pdf": "application/pdf",
                    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png",
                    ".txt": "text/plain",
                }
                doc_meta = {
                    "document_id": _doc_id,
                    "original_filename": f"document{ext_now}",
                    "gcs_object_path": gcs_object_path,
                    "category": category,
                    "upload_date": datetime.now().strftime("%Y-%m-%d"),
                    "mime_type": mime_map.get(ext_now, "application/octet-stream"),
                    "file_size": size_now,
                    "ocr_status": ocr_status,
                    "extracted_text": extracted_text,
                    # Owner tracking — ป้องกัน cross-patient access
                    # ผู้ใช้เห็นเฉพาะเอกสารที่ตัวเองส่ง
                    "owner_hash": owner_hash or "",
                }
                try:
                    doc_index.add(doc_meta)
                    index_saved = True
                except RuntimeError as idx_err:
                    # Index เขียน GCS ไม่ได้ — ไฟล์ upload แล้ว แต่ metadata หาย
                    # ไม่ตอบว่าสำเร็จ — แจ้ง error กลับไป
                    logger.error(
                        f"Document index write ล้มเหลว: {idx_err} "
                        f"(doc_id={_doc_id})"
                    )
                    raise RuntimeError(
                        f"ไฟล์ถูกอัปโหลดแล้ว แต่บันทึก metadata ล้มเหลว — "
                        f"กรุณาแจ้งผู้ดูแลระบบ"
                    )

            # ── Lab Extraction V1: ดึงผลแล็บอัตโนมัติ ──
            lab_extracted = False
            if category == "lab" and stored_in_gcs and extracted_text:
                try:
                    from lab_extractor import extract_lab_results_from_text
                    from lab_models import ExtractionMethod

                    _lab_method = (
                        ExtractionMethod.REGEX_OCR.value
                        if ocr_status == "ocr"
                        else ExtractionMethod.REGEX_PDF_TEXT.value
                    )
                    lab_report = extract_lab_results_from_text(
                        extracted_text, _doc_id, _lab_method
                    )
                    if lab_report.results:
                        # บันทึกผลแล็บลง GCS — create-only
                        _lab_json = lab_report.to_json()
                        _lab_path = f"{LAB_RESULTS_PREFIX}{_doc_id}.json"
                        storage.put(
                            _lab_path,
                            _lab_json.encode("utf-8"),
                            content_type="application/json",
                        )
                        lab_extracted = True
                        logger.info(
                            f"Lab extraction สำเร็จ: doc={_doc_id[:8]}, "
                            f"results={len(lab_report.results)}"
                        )

                        # ── Lab Alert V1: แจ้งเตือนผลตรวจผิดปกติ ──
                        try:
                            from lab_alert import (
                                check_lab_alerts,
                                format_user_alert_th,
                                format_admin_alert_th,
                            )
                            _alert_summary = check_lab_alerts(lab_report)

                            # แจ้งเตือนผู้ใช้ (critical + warning)
                            if _alert_summary.should_notify_user and line_user_id:
                                _user_alert_text = format_user_alert_th(
                                    _alert_summary
                                )
                                if _user_alert_text:
                                    push_message(line_user_id, _user_alert_text)
                                    logger.info(
                                        f"Lab alert sent to user: "
                                        f"doc={_doc_id[:8]}, "
                                        f"critical={_alert_summary.critical_count}, "
                                        f"warning={_alert_summary.warning_count}"
                                    )

                            # แจ้งเตือน admin (เฉพาะ critical)
                            if _alert_summary.should_notify_admin:
                                _admin_alert_text = format_admin_alert_th(
                                    _alert_summary,
                                    owner_hash_prefix=(owner_hash or "")[:8],
                                )
                                if _admin_alert_text:
                                    # ส่งให้ admin ทุกคนที่ลงทะเบียน
                                    # ใช้ push_message กับ admin user_id
                                    # หมายเหตุ: ADMIN_USERS เก็บเป็น hash
                                    # ไม่สามารถ push กลับไปยัง admin ได้
                                    # เก็บ alert ไว้ใน GCS แทน
                                    _alert_path = (
                                        f"_alerts/{_doc_id}.json"
                                    )
                                    _alert_data = {
                                        "document_id": _doc_id[:8],
                                        "specimen_date": lab_report.specimen_date,
                                        "critical_count": _alert_summary.critical_count,
                                        "warning_count": _alert_summary.warning_count,
                                        "created_at": datetime.now().isoformat(),
                                        "notified_user": bool(line_user_id),
                                    }
                                    storage.put(
                                        _alert_path,
                                        json.dumps(
                                            _alert_data, ensure_ascii=False
                                        ).encode("utf-8"),
                                        content_type="application/json",
                                    )
                                    logger.info(
                                        f"Critical alert stored: "
                                        f"doc={_doc_id[:8]}"
                                    )
                        except Exception as alert_err:
                            # Alert ล้มเหลวไม่ทำให้ upload ล้มเหลว
                            logger.warning(
                                f"Lab alert ล้มเหลว: "
                                f"{type(alert_err).__name__} "
                                f"(doc_id={_doc_id[:8]})"
                            )

                except Exception as lab_err:
                    # Lab extraction ล้มเหลวไม่ทำให้ upload ล้มเหลว
                    logger.warning(
                        f"Lab extraction ล้มเหลว: {type(lab_err).__name__} "
                        f"(doc_id={_doc_id[:8]})"
                    )

            # ── Clinical Analysis V1: วิเคราะห์ผลแล็บด้วย Rule-based Engine ──
            clinical_analyzed = False
            if lab_extracted and lab_report and lab_report.results:
                try:
                    from clinical_analyzer import analyze_lab_report as run_clinical
                    clinical_result = run_clinical(lab_report)
                    if clinical_result.findings:
                        _clinical_json = json.dumps(
                            clinical_result.to_dict(), ensure_ascii=False
                        )
                        _clinical_path = (
                            f"{CLINICAL_ANALYSIS_PREFIX}{_doc_id}.json"
                        )
                        storage.put(
                            _clinical_path,
                            _clinical_json.encode("utf-8"),
                            content_type="application/json",
                        )
                        clinical_analyzed = True
                        logger.info(
                            f"Clinical analysis สำเร็จ: doc={_doc_id[:8]}, "
                            f"findings={len(clinical_result.findings)}, "
                            f"ckd_stage={clinical_result.ckd_stage}"
                        )
                except Exception as ca_err:
                    # Clinical analysis ล้มเหลวไม่ทำให้ upload ล้มเหลว
                    logger.warning(
                        f"Clinical analysis ล้มเหลว: "
                        f"{type(ca_err).__name__} "
                        f"(doc_id={_doc_id[:8]})"
                    )

            # บันทึก log — ไม่เก็บ path หรือชื่อไฟล์ต้นฉบับ
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "doc_id": _doc_id,
                "stored": stored_in_gcs,
                "category": category,
                "file_size": size_now,
                "file_type": ext_now,
                "classification": result,
                "lab_extracted": lab_extracted,
                "clinical_analyzed": clinical_analyzed,
            }
            log_file = os.path.join(BASE_DIR, "logs", "classification_log.jsonl")
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

            # ลบไฟล์ชั่วคราวหลัง cloud upload สำเร็จ
            # ลบเฉพาะเมื่อ (1) dest เป็น gs:// (upload สำเร็จ)
            #              (2) filepath อยู่ใน BASE_DIR (ป้องกันลบไฟล์นอก temp)
            if dest and dest.startswith("gs://"):
                _safe_base = os.path.realpath(BASE_DIR)
                _real_fp = os.path.realpath(filepath)
                if _real_fp.startswith(_safe_base + os.sep):
                    try:
                        os.remove(filepath)
                        logger.info(f"ลบไฟล์ชั่วคราว inbox: {_doc_id}")
                    except OSError as e:
                        logger.warning(
                            f"ลบไฟล์ชั่วคราวไม่สำเร็จ: {type(e).__name__}"
                        )
                    # ลบ local copy ใน category folder ด้วย (ephemeral)
                    _local_cat_dest = os.path.join(
                        BASE_DIR,
                        FOLDERS.get(category, FOLDERS["review"]),
                        os.path.basename(filepath),
                    )
                    if os.path.exists(_local_cat_dest):
                        try:
                            os.remove(_local_cat_dest)
                            logger.info(f"ลบ local copy: {_doc_id}")
                        except OSError:
                            pass  # ไม่ร้ายแรง — จะหายเมื่อ Render restart
                else:
                    logger.warning("ข้ามการลบ — ไฟล์อยู่นอก BASE_DIR")

            # ตอบกลับ LINE
            cat_names = {
                "bills": "ใบเสร็จ/บิล",
                "rx": "ใบสั่งยา",
                "lab": "ผลตรวจแล็บ",
                "dialysis": "เอกสารฟอกเลือด",
                "review": "รอตรวจสอบ",
            }
            cat_name = cat_names.get(category, "ไม่ทราบ")
            ext = os.path.splitext(filepath)[1].lower()
            file_type_name = {
                ".txt": "TXT", ".pdf": "PDF",
                ".jpg": "JPG", ".jpeg": "JPEG", ".png": "PNG",
            }.get(ext, ext)

            if reply_token:
                _reply_lines = [
                    f"รับเอกสารแล้ว ({file_type_name})",
                    f"ประเภท: {cat_name}",
                    f"จัดเก็บใน: {FOLDERS.get(category, FOLDERS['review'])}",
                ]
                if lab_extracted:
                    _reply_lines.append(
                        f"📋 พบผลตรวจ {len(lab_report.results)} รายการ"
                    )
                    _reply_lines.append(
                        f"พิมพ์ 'ผลแล็บ {_doc_id[:8]}' เพื่อดูผลตรวจ"
                    )
                if clinical_analyzed:
                    _reply_lines.append(
                        f"🔬 วิเคราะห์ทางคลินิกเสร็จสิ้น"
                    )
                    _reply_lines.append(
                        f"พิมพ์ 'วิเคราะห์แล็บ {_doc_id[:8]}' เพื่อดูผลวิเคราะห์"
                    )
                reply_message(reply_token, "\n".join(_reply_lines))
        except RuntimeError as e:
            # Production mode — GCS ล้มเหลว ต้องแจ้งผู้ใช้
            # บันทึก error message เต็ม (ไม่มี path/ข้อมูลผู้ป่วย
            # เพราะ RuntimeError message มาจากโค้ดเราเอง)
            logger.error(
                f"จัดเก็บเอกสารล้มเหลว (Production): {type(e).__name__}: "
                f"{e} (doc_id={_doc_id})"
            )
            if reply_token:
                reply_message(
                    reply_token,
                    "ขออภัย ระบบไม่สามารถจัดเก็บเอกสารได้ในขณะนี้\n"
                    "กรุณาลองส่งใหม่ในอีกสักครู่\n"
                    "หากยังไม่ได้ กรุณาแจ้งเจ้าหน้าที่",
                )
        except Exception as e:
            # ไม่ log str(e) — ข้อความ exception ของ I/O มักมี path พร้อมชื่อไฟล์
            logger.error(
                f"ประมวลผลไฟล์ล้มเหลว: {type(e).__name__} (doc_id={_doc_id})"
            )
            if IS_PRODUCTION and reply_token:
                reply_message(
                    reply_token,
                    "ขออภัย เกิดข้อผิดพลาดในการประมวลผลเอกสาร\n"
                    "กรุณาลองส่งใหม่อีกครั้ง",
                )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


def _handle_incoming_document(
    data: bytes,
    ext: str,
    user_hash: str,
    reply_token: str | None,
    line_user_id: str = "",
):
    """ขั้นตอนร่วมของการรับเอกสารจาก LINE (ทั้งภาพและไฟล์).

    ชั้นที่ 3 ของการกันไฟล์ซ้ำ — ตรวจ sha256 ของเนื้อหา
    ต้องทำหลังดาวน์โหลด (ต้องมีข้อมูลถึงจะ hash ได้) แต่ก่อนบันทึกลง inbox
    จึงไม่มี temp file ค้างเมื่อพบว่าซ้ำ
    """
    sha256_hex = content_sha256(data)

    if not dedup_guard.is_new_content(user_hash, sha256_hex):
        logger.info(f"ข้ามเอกสารเนื้อหาซ้ำ (sha={sha256_hex[:8]}…)")
        reply_message(
            reply_token,
            "ได้รับเอกสารนี้แล้วเมื่อสักครู่\nไม่ต้องส่งซ้ำครับ",
        )
        return

    doc_id, filename = build_storage_filename(ext)
    filepath = save_to_inbox(data, filename)
    process_file_async(
        filepath, reply_token, doc_id=doc_id, sha256_hex=sha256_hex,
        owner_hash=user_hash, line_user_id=line_user_id,
    )


# ═══════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    """รับ LINE Webhook events."""
    logger.info(f"POST /webhook received (content_length={request.content_length}, mode={'testing' if IS_TESTING else 'production'})")

    # Testing Mode Guard
    if IS_TESTING and not IS_TESTING_DOCS:
        signature = request.headers.get("X-Line-Signature", "")
        body = request.get_data()

        sig_valid = verify_signature(body, signature)
        logger.info(f"TESTING MODE: signature_valid={sig_valid}")
        if not sig_valid:
            logger.warning("TESTING MODE: Webhook signature ไม่ถูกต้อง — ปฏิเสธ")
            abort(403)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("TESTING MODE: JSON decode ล้มเหลว")
            abort(400)

        event_count = len(payload.get("events", []))
        logger.info(f"TESTING MODE: รับ {event_count} event(s)")

        for event in payload.get("events", []):
            if event.get("type") == "message":
                msg = event.get("message", {})
                msg_type = msg.get("type", "unknown")
                if (
                    msg_type == "text"
                    and msg.get("text", "").strip() == "TEST-03"
                ):
                    logger.info("TESTING MODE: TEST-03 received safely")
                else:
                    logger.info(f"TESTING MODE: รับข้อความ type={msg_type} (ไม่ประมวลผล)")

        logger.info("TESTING MODE: ส่ง response status=testing — ไม่ดาวน์โหลด/จัดเก็บเอกสาร")
        return jsonify({"status": "testing", "accepted": True}), 200
    # ── End Testing Guard ───────────────────────────────

    # testing-docs: อนุญาตรับเอกสารและจัดเก็บ แต่ไม่เรียก Claude API
    if IS_TESTING_DOCS:
        logger.info("TESTING-DOCS MODE: อนุญาตรับเอกสาร — ไม่เรียก Claude API")

    # Production guard: ตรวจว่า USER_ID_SALT ตั้งค่าแล้ว
    if _USER_ID_SALT_MISSING:
        logger.error("PRODUCTION: USER_ID_SALT ไม่ได้ตั้งค่า — ปฏิเสธ webhook (fail-closed)")
        return jsonify({
            "status": "error",
            "reason": "configuration_incomplete",
        }), 503

    # Production guard: ตรวจว่า GCS พร้อมก่อนรับเอกสาร
    # ถ้า GCS ไม่พร้อม ปฏิเสธ webhook ทั้งหมดเพื่อไม่ให้เอกสารหายตอน redeploy
    if IS_PRODUCTION and STORAGE_BACKEND == "gcs":
        if not hasattr(storage, "is_ready") or not storage.is_ready:
            gcs_err = getattr(storage, "_init_error", "unknown")
            logger.error(
                f"PRODUCTION: GCS ไม่พร้อม ({gcs_err}) — ปฏิเสธ webhook"
            )
            return jsonify({
                "status": "error",
                "reason": "storage_not_ready",
            }), 503

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        logger.warning("Webhook signature ไม่ถูกต้อง — ปฏิเสธ")
        abort(403)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        abort(400)

    events = payload.get("events", [])
    logger.info(f"รับ {len(events)} event(s) จาก LINE")

    for event in events:
        event_type = event.get("type")
        reply_token = event.get("replyToken")

        # ─── ชั้นที่ 1: dedup ด้วย webhookEventId ────────────────
        # LINE อาจส่ง event เดียวกันซ้ำจาก retry หรือปัญหาเครือข่าย
        # event ที่ส่งซ้ำมี webhookEventId เดิม ต่างแค่ isRedelivery
        # ต้องตรวจก่อน rate limit เพื่อไม่ให้ retry กิน quota ของผู้ป่วย
        event_id = event.get("webhookEventId", "")
        is_redelivery = bool(
            event.get("deliveryContext", {}).get("isRedelivery", False)
        )
        if not dedup_guard.is_new_event(event_id):
            logger.info(
                f"ข้าม event ซ้ำ (id={event_id[:8]}…, redelivery={is_redelivery})"
            )
            continue
        if is_redelivery:
            logger.info(f"event นี้เป็น redelivery (id={event_id[:8]}…)")

        _line_user_id = event.get("source", {}).get("userId", "")
        user_hash = hash_user_id(_line_user_id)

        if event_type == "message":
            msg = event.get("message", {})
            msg_type = msg.get("type")
            msg_id = msg.get("id")

            if msg_type in ("image", "file") and not msg_id:
                logger.warning(f"ข้อความ type={msg_type} ไม่มี message id — ข้าม")
                continue

            # ─── ชั้นที่ 2: dedup ด้วย message id ────────────────
            if msg_type in ("image", "file") and not dedup_guard.is_new_message(msg_id):
                logger.info(f"ข้ามข้อความซ้ำ (msg_id={msg_id})")
                continue

            if msg_type == "image":
                logger.info(f"รับภาพ (id={msg_id})")

                allowed, limit_msg = rate_limiter.check(user_hash)
                if not allowed:
                    logger.warning(f"เกิน rate limit (user={user_hash[:8]}…)")
                    reply_message(reply_token, limit_msg)
                    continue

                ext = get_content_type_ext(msg_id)
                if ext not in ALLOWED_EXTENSIONS:
                    ext = ".jpg"
                data = download_line_content(msg_id)
                if not data:
                    reply_message(reply_token, "ไม่สามารถดาวน์โหลดภาพได้")
                    continue
                _handle_incoming_document(data, ext, user_hash, reply_token, _line_user_id)

            elif msg_type == "file":
                # ชื่อไฟล์จากผู้ใช้ใช้เพื่อดูนามสกุลเท่านั้น
                # ไม่นำไปตั้งชื่อจัดเก็บและไม่บันทึกลง log เพราะมักมีชื่อผู้ป่วย
                file_ext = normalize_incoming_extension(msg.get("fileName", ""))
                logger.info(f"รับไฟล์: ext={file_ext or 'unknown'} (id={msg_id})")

                if not file_ext:
                    reply_message(
                        reply_token,
                        "ไม่รองรับไฟล์ประเภทนี้\n"
                        f"รองรับ: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
                    )
                    continue

                allowed, limit_msg = rate_limiter.check(user_hash)
                if not allowed:
                    logger.warning(f"เกิน rate limit (user={user_hash[:8]}…)")
                    reply_message(reply_token, limit_msg)
                    continue

                data = download_line_content(msg_id)
                if not data:
                    reply_message(reply_token, "ไม่สามารถดาวน์โหลดไฟล์ได้")
                    continue
                _handle_incoming_document(data, file_ext, user_hash, reply_token, _line_user_id)

            elif msg_type == "text":
                text = msg.get("text", "")
                logger.info(f"รับข้อความ: {len(text)} chars")

                # ── Document Search V1: ตรวจคำสั่งค้นหาก่อน ──
                search_response = handle_search_command(text, user_hash)
                if search_response:
                    if isinstance(search_response, dict) and search_response.get("type") == "flex":
                        # Flex Message (เช่น ขอไฟล์ → ปุ่มเปิด PDF)
                        reply_line_message(reply_token, search_response)
                    else:
                        reply_message(reply_token, search_response)
                elif text.strip().lower() in ("สถานะ", "status"):
                    inbox_path = os.path.join(BASE_DIR, FOLDERS["inbox"])
                    inbox_count = len(os.listdir(inbox_path)) if os.path.exists(inbox_path) else 0
                    doc_count = doc_index.count()
                    reply_message(
                        reply_token,
                        f"ระบบระนอง รักษ์ไต (Cloud) ทำงานปกติ\n"
                        f"เอกสารใน inbox: {inbox_count} ไฟล์\n"
                        f"เอกสารในดัชนี: {doc_count} รายการ\n"
                        f"AI classify: {'เปิด' if ENABLE_AI_CLASSIFY else 'ปิด'}\n"
                        f"Storage: {STORAGE_BACKEND}\n"
                        f"รองรับ: TXT, PDF, JPG, PNG",
                    )
                else:
                    reply_message(
                        reply_token,
                        "📄 ส่งภาพหรือไฟล์เพื่อจำแนกประเภทอัตโนมัติ\n\n"
                        "คำสั่งที่ใช้บ่อย:\n"
                        "  ค้นหา [คำค้น] — ค้นหาเอกสาร\n"
                        "  ผลแล็บล่าสุด — ดูผลตรวจล่าสุด\n"
                        "  สถิติ — ดูสถิติเอกสาร\n"
                        "  ช่วยเหลือ — ดูคำสั่งทั้งหมด",
                    )
            else:
                logger.info(f"ข้าม message type: {msg_type}")

        elif event_type == "follow":
            reply_message(
                reply_token,
                "🏥 ยินดีต้อนรับสู่ระบบจัดการเอกสาร คลินิกระนอง รักษ์ไต\n\n"
                "📄 ส่งภาพหรือไฟล์เพื่อจำแนกประเภทอัตโนมัติ\n"
                "🔍 พิมพ์ 'ค้นหา' ตามด้วยคำค้น เพื่อค้นหาเอกสาร\n"
                "❓ พิมพ์ 'ช่วยเหลือ' เพื่อดูคำสั่งทั้งหมด",
            )

    return jsonify({"status": "ok"}), 200


# ─── Download Proxy Endpoint ─────────────────────────────────
# ใช้เมื่อ Signed URL ยาวเกิน LINE URI action limit
# Stateless — ตรวจ HMAC token แล้ว redirect ไปยัง Signed URL
# ไม่ log URL, ไม่ log gcs_path, ไม่ cache
# ─────────────────────────────────────────────────────────────

@app.route("/dl/<token>", methods=["GET"])
def download_proxy(token):
    """Proxy download — validate HMAC token แล้ว redirect ไป Signed URL."""
    gcs_path = validate_download_token(token)
    if gcs_path is None:
        logger.info("Download proxy: token ไม่ถูกต้องหรือหมดอายุ")
        return (
            "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
            "<h2>ลิงก์หมดอายุ</h2>"
            "<p>กรุณาขอลิงก์ใหม่ผ่าน LINE<br>"
            "พิมพ์: <b>ขอไฟล์ [รหัส]</b></p>"
            "</body></html>"
        ), 403

    signed_url = generate_signed_url(gcs_path, expiry_seconds=300)
    if not signed_url:
        logger.error("Download proxy: สร้าง Signed URL ไม่ได้")
        return (
            "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
            "<h2>ไม่สามารถดาวน์โหลดได้</h2>"
            "<p>กรุณาลองใหม่ในอีกสักครู่</p>"
            "</body></html>"
        ), 500

    # 302 redirect → ไม่ cache, Signed URL มีอายุ 5 นาที
    return redirect(signed_url)


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint — ทำงานได้โดยไม่ต้องใช้ API Key.

    คืน HTTP 200 เสมอเมื่อ app process ทำงาน (ให้ Render ยอมรับ deploy)
    แต่รายงานสถานะจริงใน body: status = running / degraded / misconfigured
    accepting_documents = False ถ้ายังไม่พร้อมรับข้อมูล
    Webhook ยังคง fail-closed (503) จนกว่า config จะครบ
    """
    health_info = {
        "status": "running",
        "version": "cloud-2.0-production",
        "app_env": APP_ENV,
        "testing_mode": IS_TESTING,
        "production_mode": IS_PRODUCTION,
        "timestamp": datetime.now().isoformat(),
        "ai_classify": ENABLE_AI_CLASSIFY and not IS_TESTING,
        "storage_backend": STORAGE_BACKEND,
        "ocr_ready": HAS_TESSERACT and HAS_PIL,
        "ocr_dpi": OCR_DPI,
        "pymupdf_ready": HAS_PYMUPDF,
        "supported_types": list(ALLOWED_EXTENSIONS),
        # ล็อกที่ระดับโค้ด — ตรวจสอบจากภายนอกได้ว่ายังปิดอยู่จริง
        "external_ai": (
            "enabled" if EXTERNAL_AI_CLASSIFICATION_ALLOWED else "disabled_by_code"
        ),
        "state_store": {
            "status": state_store.status,
            "shared_across_workers": state_store.is_shared,
            "error": state_store.error,
        },
        "rate_limit": {
            "per_minute": RATE_LIMIT_PER_MINUTE,
            "per_hour": RATE_LIMIT_PER_HOUR,
            "per_day": RATE_LIMIT_PER_DAY,
            "effective_halved": not state_store.is_shared,
        },
    }

    http_status = 200

    # เพิ่มสถานะ GCS ถ้าใช้ gcs backend
    if STORAGE_BACKEND == "gcs" and hasattr(storage, "check_connection"):
        gcs_status = storage.check_connection()
        if hasattr(storage, "_auth_method"):
            gcs_status["auth_method"] = storage._auth_method
        health_info["gcs"] = gcs_status

        # Production: ถ้า GCS ไม่พร้อม → รายงานสถานะ แต่ไม่ block deploy
        # Webhook ยังคง fail-closed (503) — ไม่รับข้อมูลจนกว่า GCS จะพร้อม
        if IS_PRODUCTION and gcs_status.get("status") != "connected":
            health_info["status"] = "degraded"
            health_info["accepting_documents"] = False
        else:
            health_info["accepting_documents"] = True
    elif IS_PRODUCTION and STORAGE_BACKEND != "gcs":
        # Production ต้องใช้ GCS เท่านั้น — รายงานแต่ไม่ block deploy
        health_info["status"] = "misconfigured"
        health_info["accepting_documents"] = False
        health_info["error"] = "Production ต้องตั้ง STORAGE_BACKEND=gcs"
    else:
        health_info["accepting_documents"] = not IS_TESTING or IS_TESTING_DOCS

    # Production checklist
    if IS_PRODUCTION:
        health_info["production_checklist"] = {
            "line_channel_secret": bool(LINE_CHANNEL_SECRET),
            "line_access_token": bool(LINE_CHANNEL_ACCESS_TOKEN),
            "storage_gcs": STORAGE_BACKEND == "gcs",
            "gcs_ready": (
                hasattr(storage, "is_ready") and storage.is_ready
            ),
            "external_ai_locked": not EXTERNAL_AI_CLASSIFICATION_ALLOWED,
            "user_id_salt_set": not _USER_ID_SALT_MISSING,
        }
        if _USER_ID_SALT_MISSING:
            health_info["status"] = "misconfigured"
            health_info["accepting_documents"] = False
            health_info["critical_error"] = "USER_ID_SALT ไม่ได้ตั้งค่า"
        health_info["document_search"] = {
            "indexed_documents": doc_index.count(),
            "authorized_users": len(AUTHORIZED_USERS),
            "admin_secret_set": bool(ADMIN_SECRET),
        }

    return jsonify(health_info), http_status


@app.route("/", methods=["GET"])
def index():
    """Root endpoint — แสดงสถานะระบบ."""
    return jsonify({
        "service": "RANONG RAKTAI AI - Document Classification",
        "status": "running",
        "health_check": "/health",
        "webhook": "/webhook",
    }), 200


# ─── Document Search V1: Admin Enrollment Endpoint ───────

@app.route("/admin/enroll", methods=["POST"])
def admin_enroll():
    """สร้าง One-Time Enrollment Token สำหรับลงทะเบียนผู้ดูแลผ่าน LINE.

    ต้องมี X-Admin-Secret header เพื่อยืนยันตัวตน
    Token ใช้ได้ครั้งเดียว อายุ 5 นาที
    ไม่บันทึก token ลง log

    ตัวอย่างการใช้:
      curl -X POST https://your-app.onrender.com/admin/enroll \\
           -H "X-Admin-Secret: YOUR_ADMIN_SECRET"

    แล้วส่ง token ที่ได้ผ่าน LINE: "ยืนยัน [TOKEN]"
    """
    # Auth check
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or auth_header != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    token = enrollment_store.generate_token()
    # ไม่บันทึก token ลง log — ส่งกลับทาง HTTPS เท่านั้น
    return jsonify({
        "token": token,
        "expires_in_seconds": ENROLLMENT_TOKEN_TTL,
        "instruction": "ส่ง token ผ่าน LINE: พิมพ์ 'ยืนยัน [TOKEN]' ภายใน 5 นาที",
    }), 200


# ─── Document Search V1: Backfill Endpoint ───────────────

@app.route("/admin/backfill", methods=["POST"])
def admin_backfill():
    """Backfill job: สร้าง index จากไฟล์ที่มีอยู่แล้วใน GCS.

    ต้องมี ADMIN_SECRET header เพื่อป้องกันการเข้าถึง
    Idempotent — ไฟล์ที่มี doc_id ใน index แล้วจะข้าม
    ไม่แก้ไขไฟล์ต้นฉบับใน GCS
    """
    # Auth check
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or auth_header != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    if not storage.is_ready or not hasattr(storage, '_bucket'):
        return jsonify({"error": "GCS not ready"}), 503

    try:
        # List all blobs in bucket (skip _index/ and _audit/)
        blobs = storage._bucket.list_blobs()
        processed = 0
        skipped = 0
        errors = 0

        for blob in blobs:
            # Skip index and audit files
            if blob.name.startswith("_index/") or blob.name.startswith("_audit/"):
                continue

            # Extract doc_id from filename (UUID before extension)
            filename = os.path.basename(blob.name)
            doc_id = os.path.splitext(filename)[0]

            # Idempotent: skip if already indexed
            if doc_index.get(doc_id):
                skipped += 1
                continue

            # Extract metadata from blob path: folder/YYYY-MM/uuid.ext
            parts = blob.name.split("/")
            category = parts[0] if parts else "review"
            ext = os.path.splitext(blob.name)[1].lower()

            # Map folder names back to categories
            folder_to_cat = {v: k for k, v in FOLDERS.items()}
            category = folder_to_cat.get(category, "review")

            mime_map = {
                ".pdf": "application/pdf",
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".txt": "text/plain",
            }

            # Note: OCR during backfill would be too slow for inline HTTP
            # This creates the index entry with ocr_status="pending"
            # A separate OCR pass can be done later

            # owner_hash จาก request body — ถ้าไม่ระบุจะเป็น "" (legacy, fail-closed)
            backfill_owner = request.json.get("owner_hash", "") if request.is_json else ""

            doc_meta = {
                "document_id": doc_id,
                "original_filename": f"document{ext}",
                "gcs_object_path": blob.name,
                "category": category,
                "upload_date": (
                    blob.time_created.strftime("%Y-%m-%d")
                    if blob.time_created else "unknown"
                ),
                "mime_type": mime_map.get(ext, "application/octet-stream"),
                "file_size": blob.size or 0,
                "ocr_status": "pending",
                "extracted_text": "",
                "owner_hash": backfill_owner,
            }

            try:
                doc_index.add(doc_meta)
                processed += 1
            except Exception:
                errors += 1

        # Save full index to GCS
        try:
            doc_index._save_full()
        except RuntimeError as save_err:
            return jsonify({
                "error": "Index save to GCS failed",
                "detail": str(save_err),
                "processed_before_failure": processed,
            }), 500

        audit_logger.log("backfill", "admin", {
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "total_indexed": doc_index.count(),
        })

        return jsonify({
            "status": "ok",
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "total_indexed": doc_index.count(),
        }), 200

    except Exception as e:
        logger.error(f"Backfill ล้มเหลว: {type(e).__name__}")
        return jsonify({"error": "Backfill failed"}), 500


@app.route("/admin/assign-owner", methods=["POST"])
def admin_assign_owner():
    """กำหนด owner_hash ให้เอกสาร legacy — Fail Closed recovery.

    ต้องมี ADMIN_SECRET header
    Body JSON: {"doc_id": "...", "owner_hash": "..."}
    หรือ batch: {"assignments": [{"doc_id": "...", "owner_hash": "..."}, ...]}
    ไม่เปลี่ยน owner_hash ที่มีอยู่แล้ว (ป้องกัน reassign)
    """
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or auth_header != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    if not request.is_json:
        return jsonify({"error": "JSON body required"}), 400

    data = request.json

    # Single assignment
    if "doc_id" in data and "owner_hash" in data:
        assignments = [{"doc_id": data["doc_id"], "owner_hash": data["owner_hash"]}]
    elif "assignments" in data:
        assignments = data["assignments"]
    else:
        return jsonify({"error": "doc_id+owner_hash or assignments required"}), 400

    assigned = 0
    not_found = 0
    already_owned = 0

    for item in assignments:
        doc_id = item.get("doc_id", "")
        owner = item.get("owner_hash", "")
        if not doc_id or not owner:
            continue

        doc = doc_index._doc_map.get(doc_id)
        if doc is None:
            for d in doc_index._docs:
                if d.get("document_id", "").startswith(doc_id):
                    doc = d
                    break

        if doc is None:
            not_found += 1
        elif doc.get("owner_hash", ""):
            already_owned += 1
        else:
            result = doc_index.assign_owner(doc_id, owner)
            if result:
                assigned += 1
            else:
                not_found += 1

    # Save to GCS if any changes
    if assigned > 0:
        try:
            doc_index._save_full()
        except RuntimeError as e:
            return jsonify({
                "error": "Index save failed after assignment",
                "assigned_in_memory": assigned,
            }), 500

    audit_logger.log("assign_owner", "admin", {
        "assigned": assigned,
        "not_found": not_found,
        "already_owned": already_owned,
        "legacy_remaining": doc_index.count_legacy(),
    })

    return jsonify({
        "status": "ok",
        "assigned": assigned,
        "not_found": not_found,
        "already_owned": already_owned,
        "legacy_remaining": doc_index.count_legacy(),
    }), 200


@app.route("/admin/legacy-docs", methods=["GET"])
def admin_legacy_docs():
    """แสดงจำนวนเอกสาร legacy ที่ยังไม่มี owner — สำหรับ admin."""
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or auth_header != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    legacy_count = doc_index.count_legacy()
    total_count = doc_index.count()

    # แสดง doc_id ของ legacy docs (ไม่แสดงเนื้อหา)
    legacy_ids = []
    for doc in doc_index._docs:
        if not doc.get("owner_hash", ""):
            legacy_ids.append({
                "doc_id": doc.get("document_id", "")[:12],
                "category": doc.get("category", ""),
                "upload_date": doc.get("upload_date", ""),
            })

    return jsonify({
        "legacy_count": legacy_count,
        "total_count": total_count,
        "legacy_docs": legacy_ids,
    }), 200


# ═══════════════════════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════════════════════

# สร้างโฟลเดอร์ทั้งหมด
for folder in FOLDERS.values():
    os.makedirs(os.path.join(BASE_DIR, folder), exist_ok=True)

# Startup log
logger.info("=" * 60)
logger.info("RANONG RAKTAI AI — Cloud Version")
logger.info("=" * 60)
logger.info(f"APP_ENV: {APP_ENV}")
if IS_TESTING_DOCS:
    _mode_desc = "TESTING-DOCS (รับเอกสาร+จัดเก็บ GCS ได้ — ไม่เรียก Claude API)"
elif IS_TESTING:
    _mode_desc = "เปิด (ปลอดภัย — ไม่รับเอกสาร/เรียก API)"
else:
    _mode_desc = "ปิด (Production)"
logger.info(f"TESTING MODE: {_mode_desc}")
logger.info(f"PORT: {PORT}")
logger.info(f"STORAGE_BACKEND: {STORAGE_BACKEND}")
if STORAGE_BACKEND == "gcs":
    logger.info(f"GCS_BUCKET: {STORAGE_BUCKET or '(ไม่ได้ตั้งค่า)'}")
    if hasattr(storage, '_auth_method') and storage._auth_method:
        logger.info(f"GCS_AUTH: {storage._auth_method}")
    if hasattr(storage, '_init_error') and storage._init_error:
        logger.error(f"GCS_ERROR: {storage._init_error}")
logger.info(f"BASE_DIR: {BASE_DIR}")
logger.info(f"AI Classification: {'เปิด' if ENABLE_AI_CLASSIFY else 'ปิด'}")
logger.info(f"OCR (Tesseract): {'พร้อม' if HAS_TESSERACT and HAS_PIL else 'ไม่พร้อม'}")
logger.info(f"PyMuPDF: {'พร้อม' if HAS_PYMUPDF else 'ไม่พร้อม'}")

# ── Document Search V1: โหลด index ตอน startup ──
doc_index._reload_from_gcs()
logger.info(f"Document Index: {doc_index.count()} เอกสาร")
logger.info(f"Authorized Users: {len(AUTHORIZED_USERS)} users")
logger.info(f"Admin Secret: {'ตั้งค่าแล้ว' if ADMIN_SECRET else 'ไม่ได้ตั้งค่า'}")

if ANTHROPIC_API_KEY:
    logger.info(f"ANTHROPIC_API_KEY: ตั้งค่าแล้ว (len={len(ANTHROPIC_API_KEY)})")
else:
    logger.warning("ANTHROPIC_API_KEY: ไม่ได้ตั้งค่า — ใช้ keyword classification")

if LINE_CHANNEL_ACCESS_TOKEN:
    logger.info(f"LINE_CHANNEL_ACCESS_TOKEN: ตั้งค่าแล้ว (len={len(LINE_CHANNEL_ACCESS_TOKEN)})")
else:
    logger.warning("LINE_CHANNEL_ACCESS_TOKEN: ไม่ได้ตั้งค่า")

logger.info(f"รองรับไฟล์: {', '.join(sorted(ALLOWED_EXTENSIONS))}")

# ─── Production Startup Validation ────────────────────────
if IS_PRODUCTION:
    logger.info("=" * 40)
    logger.info("PRODUCTION MODE — ตรวจสอบความพร้อม")
    _prod_ok = True

    if _USER_ID_SALT_MISSING:
        logger.critical(
            "PRODUCTION CRITICAL: USER_ID_SALT ไม่ได้ตั้งค่า — "
            "ระบบจะปฏิเสธทุก webhook (fail-closed)"
        )
        _prod_ok = False

    if STORAGE_BACKEND != "gcs":
        logger.error(
            "PRODUCTION ERROR: STORAGE_BACKEND ต้องเป็น 'gcs' — "
            f"ปัจจุบัน: '{STORAGE_BACKEND}'"
        )
        _prod_ok = False

    if not (hasattr(storage, "is_ready") and storage.is_ready):
        logger.error(
            "PRODUCTION ERROR: GCS ไม่พร้อม — "
            "ระบบจะปฏิเสธ webhook จนกว่า GCS จะเชื่อมต่อได้"
        )
        _prod_ok = False

    if not LINE_CHANNEL_SECRET:
        logger.error("PRODUCTION ERROR: LINE_CHANNEL_SECRET ไม่ได้ตั้งค่า")
        _prod_ok = False

    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.error("PRODUCTION ERROR: LINE_CHANNEL_ACCESS_TOKEN ไม่ได้ตั้งค่า")
        _prod_ok = False

    if not ADMIN_SECRET:
        logger.warning(
            "PRODUCTION WARNING: ADMIN_SECRET ไม่ได้ตั้งค่า — "
            "admin endpoints จะปฏิเสธทุก request"
        )

    if not AUTHORIZED_USERS:
        logger.warning(
            "PRODUCTION WARNING: AUTHORIZED_USERS ว่าง — "
            "ระบบจะปฏิเสธผู้ใช้ทุกคน (fail-closed)"
        )

    if EXTERNAL_AI_CLASSIFICATION_ALLOWED:
        logger.warning(
            "PRODUCTION WARNING: EXTERNAL_AI_CLASSIFICATION_ALLOWED=True — "
            "ข้อมูลผู้ป่วยจะถูกส่งไป Claude API"
        )

    if _prod_ok:
        logger.info("PRODUCTION: ✓ ระบบพร้อมรับเอกสาร")
    else:
        logger.error(
            "PRODUCTION: ✗ ระบบยังไม่พร้อม — "
            "จะปฏิเสธเอกสารจนกว่าจะแก้ไข"
        )
    logger.info("=" * 40)

logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════
# Main (สำหรับทดสอบ local เท่านั้น — Render ใช้ Gunicorn)
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("Running in development mode (Flask dev server)")
    app.run(host="0.0.0.0", port=PORT, debug=False)
