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

import os
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
from datetime import datetime
from pathlib import Path

from flask import Flask, request, abort, jsonify
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
# ยังไม่ตั้งใน ENV (Phase 1 ห้ามแก้ ENV) — ใช้ค่าสุ่มต่อ process ไปก่อน
# ผลคือ rate limit counter จะรีเซ็ตเมื่อ restart ซึ่งยอมรับได้ในเฟสนี้
USER_ID_SALT = os.getenv("USER_ID_SALT", "").strip() or uuid.uuid4().hex

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
# Storage Abstraction Layer
# ═══════════════════════════════════════════════════════════

class StorageLocal:
    """เก็บไฟล์บน local disk (ephemeral บน Render)."""

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
            logger.info(
                f"GCS Storage: bucket={self.bucket_name}, "
                f"auth={self._auth_method} — เชื่อมต่อสำเร็จ"
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
        """ตรวจสอบการเชื่อมต่อ GCS — ใช้ใน health check."""
        if not self.is_ready:
            return {"status": "not_ready", "error": self._init_error}
        try:
            self._bucket.reload()
            return {"status": "connected", "bucket": self.bucket_name}
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
):
    """ประมวลผลไฟล์ในเธรดแยก.

    doc_id และ sha256_hex มาจากตอนรับไฟล์ ถ้าไม่ส่งมาจะอนุมานจากชื่อไฟล์
    (ชื่อไฟล์เป็น UUID อยู่แล้ว) เพื่อให้เรียกใช้แบบเดิมได้
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
            dest = copy_to_category(filepath, category, metadata=metadata)

            # บันทึก log — ไม่เก็บ path หรือชื่อไฟล์ต้นฉบับ
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "doc_id": _doc_id,
                "stored": bool(dest and dest.startswith("gs://")),
                "category": category,
                "file_size": size_now,
                "file_type": ext_now,
                "classification": result,
            }
            log_file = os.path.join(BASE_DIR, "logs", "classification_log.jsonl")
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
                reply_message(
                    reply_token,
                    f"รับเอกสารแล้ว ({file_type_name})\n"
                    f"ประเภท: {cat_name}\n"
                    f"จัดเก็บใน: {FOLDERS.get(category, FOLDERS['review'])}",
                )
        except RuntimeError as e:
            # Production mode — GCS ล้มเหลว ต้องแจ้งผู้ใช้
            logger.error(
                f"จัดเก็บเอกสารล้มเหลว (Production): {type(e).__name__} "
                f"(doc_id={_doc_id})"
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
        filepath, reply_token, doc_id=doc_id, sha256_hex=sha256_hex
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

        user_hash = hash_user_id(event.get("source", {}).get("userId", ""))

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
                _handle_incoming_document(data, ext, user_hash, reply_token)

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
                _handle_incoming_document(data, file_ext, user_hash, reply_token)

            elif msg_type == "text":
                text = msg.get("text", "")
                logger.info(f"รับข้อความ: {len(text)} chars")
                if text.strip().lower() in ("สถานะ", "status"):
                    inbox_path = os.path.join(BASE_DIR, FOLDERS["inbox"])
                    inbox_count = len(os.listdir(inbox_path)) if os.path.exists(inbox_path) else 0
                    reply_message(
                        reply_token,
                        f"ระบบระนอง รักษ์ไต (Cloud) ทำงานปกติ\n"
                        f"เอกสารใน inbox: {inbox_count} ไฟล์\n"
                        f"AI classify: {'เปิด' if ENABLE_AI_CLASSIFY else 'ปิด'}\n"
                        f"Storage: {STORAGE_BACKEND}\n"
                        f"รองรับ: TXT, PDF, JPG, PNG",
                    )
                else:
                    reply_message(
                        reply_token,
                        "ส่งภาพหรือไฟล์เอกสาร (PDF, TXT, JPG, PNG)\n"
                        "เพื่อจำแนกประเภทอัตโนมัติ\n"
                        "พิมพ์ 'สถานะ' เพื่อตรวจสอบระบบ",
                    )
            else:
                logger.info(f"ข้าม message type: {msg_type}")

        elif event_type == "follow":
            reply_message(
                reply_token,
                "ยินดีต้อนรับสู่ระบบจัดการเอกสาร คลินิกระนอง รักษ์ไต\n"
                "ส่งภาพหรือไฟล์ (PDF, TXT, JPG, PNG)\n"
                "เพื่อจำแนกประเภทอัตโนมัติ",
            )

    return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint — ทำงานได้โดยไม่ต้องใช้ API Key.

    Production mode: คืน HTTP 503 ถ้า GCS ไม่พร้อม
    เพื่อให้ Render ตรวจจับปัญหาและรายงาน
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

        # Production: ถ้า GCS ไม่พร้อม → health check fail
        if IS_PRODUCTION and gcs_status.get("status") != "connected":
            health_info["status"] = "degraded"
            health_info["accepting_documents"] = False
            http_status = 503
        else:
            health_info["accepting_documents"] = True
    elif IS_PRODUCTION and STORAGE_BACKEND != "gcs":
        # Production ต้องใช้ GCS เท่านั้น
        health_info["status"] = "misconfigured"
        health_info["accepting_documents"] = False
        health_info["error"] = "Production ต้องตั้ง STORAGE_BACKEND=gcs"
        http_status = 503
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
