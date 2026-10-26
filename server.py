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
import re
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, request, abort, jsonify, redirect
import requests as http_requests

# ─── Quarterly/Monthly Organization & HN Registry ──────────
from patient_hn_registry import (
    HNRegistry,
    get_quarter,
    resolve_patient_folder,
)
from patient_master_registry import (
    MasterPatientRegistry,
    parse_patient_list_ocr,
    parse_patient_text_input,
    extract_hn_from_document,
)
from vendor_registry import (
    VendorRegistry,
    VendorMatch,
    REVIEW_VENDOR_FOLDER,
    UNKNOWN_VENDOR_FOLDER,
    extract_candidate_names,
    is_valid_canonical_name,
    to_canonical_name,
)
from quarterly_compare import (
    build_quarterly_path,
    build_monthly_patient_path,
    build_legacy_path,
    build_document_path,
    _safe_folder_component,
    bangkok_month,
    to_bangkok,
    BANGKOK_TZ,
    CLINICAL_CATEGORIES,
    compare_lab_reports,
    compare_latest_labs,
    compare_latest_rx,
    get_patient_lab_history,
)

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
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ─── รูปแบบภาพที่อนุญาตให้ Pillow ถอดรหัส ───────────────────
#
# ทำไมต้องจำกัด: Pillow เลือกตัวถอดรหัสจาก "เนื้อหาไฟล์" ไม่ใช่นามสกุล
# ไฟล์ที่ชื่อ .png แต่ข้างในเป็น PSD, FITS หรือ PDF จะถูกส่งเข้า
# ตัวถอดรหัสของรูปแบบนั้นแทน — ด่านกรองนามสกุลจึงกันไม่ได้
#
# ช่องโหว่ที่ปิดด้วยการจำกัดนี้ (พบจาก pip-audit 2026-10-26):
#   PSD   CVE-2026-42311, CVE-2026-25990   memory corruption / OOB write
#   FITS  CVE-2026-40192                   decompression bomb (OOM)
#   PDF   CVE-2026-42310, CVE-2026-112895  ค้างกิน CPU 100% / OOB
#   EPS   CVE-2026-112900, 112899, 112402  Ghostscript cmdline injection
#   TIFF  CVE-2026-112395, 112896, 112897  _decodeTile OOB
#   GIF   CVE-2026-112400                  seek
#   PPM   CVE-2026-112397                  width 0
#
# เหลือเฉพาะ JPEG และ PNG ตามที่เส้นทางเข้าจริงจำกัดไว้แล้ว:
# ทุกจุดที่เรียก Image.open ผ่านด่าน ext in (.jpg/.jpeg/.png) มาก่อน
# และ LINE ส่งภาพมาเป็น JPEG หรือ PNG เท่านั้น การอนุญาตรูปแบบอื่น
# จึงไม่ได้ประโยชน์ แต่เปิดตัวถอดรหัสที่มีช่องโหว่อยู่จริงให้เข้าถึงได้
# (เคยใส่ GIF WEBP BMP TIFF ไว้ด้วย — TIFF กับ GIF มี CVE ค้างอยู่)
#
# กรณีที่ยังต้องรองรับ: นามสกุลไม่ตรงเนื้อหา เช่น JPEG ในไฟล์ .png
# ซึ่งเกิดขึ้นจริงกับภาพที่ส่งทาง LINE — ครอบคลุมด้วยสองรูปแบบนี้แล้ว
#
# เอกสาร PDF ไม่ได้ใช้เส้นทางนี้ — ใช้ PyMuPDF แปลงเป็น pixmap ก่อน
SAFE_IMAGE_FORMATS = ["JPEG", "PNG"]

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

# ── OCR Debug Storage (ชั่วคราว เก็บใน memory เท่านั้น ไม่ลง log) ──
_ocr_debug_store: dict = {}

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
_early_logger = logging.getLogger(__name__)  # Early logger ก่อน basicConfig
# โหมดใดก็ตามที่ "จัดเก็บเอกสารจริง" ต้องมี salt คงที่
#
# [SECURITY_GAP_ANALYSIS §3.2] เดิมเงื่อนไขนี้เช็คเฉพาะ production แต่
# โหมด testing-docs ก็ดาวน์โหลดและจัดเก็บเอกสารจริงลง GCS เหมือนกัน
# เมื่อใช้ salt สุ่มที่เปลี่ยนทุก restart owner_hash ของเอกสารที่เก็บไว้
# ก่อนหน้าจะไม่มีทางตรงกับของผู้ใช้คนเดิมอีกเลย
# → เอกสารกลายเป็นเข้าถึงไม่ได้ถาวร แบบเงียบ ๆ ไม่มีใครรู้
# (fail-closed จึงไม่รั่ว แต่เป็นการสูญเสียการเข้าถึงข้อมูลผู้ป่วย)
_SALT_REQUIRED = IS_PRODUCTION or IS_TESTING_DOCS

if _SALT_REQUIRED and not _user_id_salt_env:
    # ต้องมี salt คงที่ — ห้ามใช้ค่า default
    # ไม่ crash แต่ fail-closed: ปฏิเสธทุก request ที่ต้องใช้ hash
    _USER_ID_SALT_MISSING = True
    USER_ID_SALT = "MISSING-SALT-DO-NOT-USE"
    _mode_note = (
        "Production" if IS_PRODUCTION
        else "โหมด testing-docs (จัดเก็บเอกสารจริง)"
    )
    _early_logger.critical(
        f"CRITICAL: USER_ID_SALT ต้องตั้งค่าใน {_mode_note} ENV — "
        "ระบบจะปฏิเสธทุก request จนกว่าจะตั้งค่า — "
        "ถ้าใช้ salt สุ่ม เอกสารที่เก็บไว้จะเข้าถึงไม่ได้หลัง restart — "
        "ใช้ค่าสุ่มที่ไม่ซ้ำ เช่น: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    )
elif not _user_id_salt_env:
    # โหมดทดสอบที่ไม่จัดเก็บเอกสาร: ใช้ random salt ได้
    # (เปลี่ยนทุก restart — ไม่มีเอกสารให้สูญหาย)
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


# ─── CORS for Admin API (เฉพาะ /admin/api/ endpoints) ──────────
# อนุญาตเฉพาะ claude.ai artifact เรียกใช้งาน admin API ได้
_CORS_ALLOWED_ORIGINS = {
    "https://claude.ai",
    "https://preview.claude.ai",
    "https://artifact.claude.ai",
}

@app.after_request
def _add_cors_headers(response):
    """เพิ่ม CORS headers สำหรับ admin API endpoints เท่านั้น."""
    if request.path.startswith("/admin/api/"):
        origin = request.headers.get("Origin", "")
        # อนุญาตทุก origin เพราะ admin API ป้องกันด้วย Admin Secret อยู่แล้ว
        # จำเป็นสำหรับ file:// (Origin: null) และ localhost
        if True:  # Admin API ป้องกันด้วย Secret — อนุญาตทุก origin
            response.headers["Access-Control-Allow-Origin"] = origin or "*"
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, X-Admin-Secret"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Max-Age"] = "3600"
    return response

@app.route("/admin/api/<path:_path>", methods=["OPTIONS"])
def _cors_preflight(_path):
    """Handle CORS preflight (OPTIONS) for admin API."""
    from flask import Response as _Resp
    origin = request.headers.get("Origin", "")
    resp = _Resp("", status=204)
    if True:  # Admin API ป้องกันด้วย Secret
        resp.headers["Access-Control-Allow-Origin"] = origin or "*"
        resp.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, X-Admin-Secret"
        )
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "3600"
    return resp


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

    def put(
        self,
        path: str,
        data: bytes,
        content_type: str = "application/json",
        create_only: bool = False,
    ) -> str:
        """เขียนไฟล์ไปที่ path ที่ระบุบน local disk.

        Args:
            path: path ปลายทาง เช่น _lab_results/doc123.json
            data: ข้อมูลที่ต้องการเขียน
            content_type: ไม่ใช้บน local — รับไว้ให้ interface ตรงกัน
            create_only: ถ้า True จะไม่เขียนทับไฟล์ที่มีอยู่แล้ว
        """
        dest = os.path.join(BASE_DIR, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if create_only and os.path.exists(dest):
            logger.info(f"Local put: ไฟล์มีอยู่แล้ว ไม่เขียนทับ ({os.path.basename(path)})")
            return dest
        with open(dest, "wb") as f:
            f.write(data)
        return dest

    def get(self, path: str) -> bytes | None:
        """อ่านไฟล์จาก path ที่ระบุบน local disk.

        Returns:
            bytes ถ้าไฟล์มี, None ถ้าไม่มี
        """
        full = os.path.join(BASE_DIR, path)
        if not os.path.exists(full):
            return None
        with open(full, "rb") as f:
            return f.read()

    def get_with_generation(self, path: str) -> tuple:
        """อ่านไฟล์พร้อม generation number สำหรับ CAS.

        Returns:
            (bytes, generation: int) ถ้าไฟล์มี
            (None, None) ถ้าไฟล์ไม่มี

        Local storage ใช้ file mtime (nanoseconds) เป็น generation แทน.
        """
        full = os.path.join(BASE_DIR, path)
        if not os.path.exists(full):
            return None, None
        with open(full, "rb") as f:
            data = f.read()
        # ใช้ mtime * 1e9 เป็น pseudo-generation เพื่อตรวจจับการเปลี่ยนแปลง
        generation = int(os.path.getmtime(full) * 1_000_000_000)
        return data, generation

    def put_conditional(
        self,
        path: str,
        data: bytes,
        if_generation_match: int,
        content_type: str = "application/json",
    ) -> tuple:
        """เขียนไฟล์แบบ conditional (CAS) บน local disk.

        Args:
            path: path ปลายทาง
            data: ข้อมูลที่ต้องการเขียน
            if_generation_match: generation ที่คาดหวัง (จาก get_with_generation)
            content_type: ไม่ใช้บน local

        Returns:
            (True, new_generation) ถ้าเขียนสำเร็จ
            (False, current_generation) ถ้า generation ไม่ตรง (conflict)

        Raises:
            FileNotFoundError: ถ้าไฟล์ไม่มีอยู่ (ไม่ควรเกิดในการ approve)
        """
        full = os.path.join(BASE_DIR, path)
        if not os.path.exists(full):
            raise FileNotFoundError(f"ไม่พบไฟล์สำหรับ conditional write: {os.path.basename(path)}")
        current_gen = int(os.path.getmtime(full) * 1_000_000_000)
        if current_gen != if_generation_match:
            return False, current_gen
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        new_gen = int(os.path.getmtime(full) * 1_000_000_000)
        return True, new_gen

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

    def put(
        self,
        path: str,
        data: bytes,
        content_type: str = "application/json",
        create_only: bool = False,
    ) -> str:
        """เขียนไฟล์ — S3 ยังไม่เชื่อมต่อ, fallback ไป local."""
        logger.warning("S3 Storage ยังไม่เชื่อมต่อ — put() fallback ไป local")
        return StorageLocal().put(path, data, content_type, create_only)

    def get(self, path: str) -> bytes | None:
        """อ่านไฟล์ — S3 ยังไม่เชื่อมต่อ."""
        logger.warning("S3 Storage ยังไม่เชื่อมต่อ — get() return None")
        return None

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
        subfolder: str | None = None,
        owner_hash_full: str = "",
    ) -> str:
        """อัปโหลดไฟล์ไป GCS.

        Production mode: ถ้า GCS ไม่พร้อมหรือ upload ล้มเหลว → raise RuntimeError
        Testing mode: fallback ไป local storage พร้อม ERROR log

        filename ที่ส่งเข้ามาควรเป็นชื่อแบบ UUID ที่สร้างตอนรับไฟล์
        (ดู build_storage_filename) — ไม่ใช่ชื่อไฟล์ต้นฉบับจากผู้ใช้

        metadata ต้องไม่มีข้อมูลส่วนบุคคล — ดู build_object_metadata

        subfolder: subfolder เพิ่มเติม เช่น ชื่อบริษัทสำหรับ bills
                   → path จะเป็น folder/subfolder/YYYY-MM/filename

        owner_hash_full: owner_hash แบบเต็มสำหรับค้นทะเบียน LINE→HN
                         (ไม่เขียนลง object metadata — ใช้หา path เท่านั้น)
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
        # ──────────────────────────────────────────────────────
        # Sub-folder organization (ใช้ canonical builder ที่เดียว):
        #   lab / rx / dialysis → folder/2026-Q4/2026-10/[HN_ชื่อ/]uuid.pdf
        #   อื่นๆ (bills ฯลฯ)   → folder/[subfolder/]2026-10/uuid.pdf
        #
        # ใบยาและใบฟอกเลือดใช้โครงสร้างเดียวกับผลแล็บ — แยกไตรมาส
        # แยกเดือน แล้วแยกรายผู้ป่วย
        # ──────────────────────────────────────────────────────
        patient_hn_folder = (metadata or {}).get("patient_hn_folder", "")

        # ถ้า OCR หา HN ไม่ได้ → ใช้ทะเบียน LINE→HN ของผู้ส่งแทน
        # (ผู้ป่วยพิมพ์ "ลงทะเบียนHN <HN>" ใน LINE ไว้ล่วงหน้า)
        #
        # สำคัญ: ต้องใช้ owner_hash แบบเต็ม เพราะทะเบียน index ด้วยค่าเต็ม
        # และต้องแปลง HN → ชื่อแฟ้มของ master registry เพื่อให้เอกสาร
        # ของผู้ป่วยคนเดียวกันอยู่แฟ้มเดียว ไม่แตกเป็นสองแฟ้ม
        if (
            not patient_hn_folder
            and category in CLINICAL_CATEGORIES
            and owner_hash_full
        ):
            try:
                # บัญชีเจ้าหน้าที่ส่งเอกสารแทนผู้ป่วยหลายคน — ห้ามใช้การผูก HN
                # ของบัญชีนั้นจัดแฟ้ม ไม่งั้นเอกสารทุกคนจะไปรวมแฟ้มเดียว
                _is_staff = False
                try:
                    _is_staff = is_authorized_user(owner_hash_full)
                except Exception:
                    _is_staff = False
                patient_hn_folder = resolve_patient_folder(
                    owner_hash_full, hn_registry, master_registry,
                    is_staff_account=_is_staff,
                )
            except Exception as _link_err:
                logger.warning(
                    "LINE→HN lookup failed: %s", type(_link_err).__name__
                )
                patient_hn_folder = ""

        # เดือน/ไตรมาสต้องมาจากเวลาที่ "ผู้ส่งส่งเข้ามา" (timestamp ของ LINE)
        # ไม่ใช่เวลาที่ประมวลผล ไม่งั้นเอกสารที่ retry หรือรวมหลายหน้าจะ
        # ไปอยู่เดือนที่ประมวลผลสำเร็จ ไม่ใช่เดือนที่ส่ง
        submitted_at = None
        _submitted_raw = (metadata or {}).get("submitted_at", "")
        if _submitted_raw:
            try:
                submitted_at = datetime.fromisoformat(_submitted_raw)
            except (ValueError, TypeError):
                logger.warning("submitted_at อ่านไม่ได้ — ใช้เวลาปัจจุบันแทน")
                submitted_at = None

        blob_path = build_document_path(
            folder,
            filename,
            category,
            patient_folder=patient_hn_folder,
            subfolder=subfolder or "",
            date=submitted_at,
        )

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

    def put(
        self,
        path: str,
        data: bytes,
        content_type: str = "application/json",
        create_only: bool = False,
    ) -> str:
        """เขียนไฟล์ไปที่ path ที่ระบุบน GCS.

        ใช้สำหรับ JSON metadata (lab results, clinical analysis, alerts)
        ที่ต้องการเขียนไปยัง path เฉพาะ เช่น _lab_results/{doc_id}.json

        Args:
            path: blob path ใน bucket เช่น _lab_results/abc123.json
            data: ข้อมูลที่ต้องการเขียน (bytes)
            content_type: MIME type (default: application/json)
            create_only: ถ้า True ใช้ if_generation_match=0 ป้องกันเขียนทับ

        Returns:
            gs:// URI ของ object ที่เขียน

        Raises:
            RuntimeError: ใน production mode เมื่อเขียนล้มเหลว

        หมายเหตุ:
            - path ต้องเป็น internal path (ไม่ใช่ข้อมูลจากผู้ใช้)
            - ไม่ log path เต็ม — ใช้ prefix + basename เพื่อป้องกัน info leak
        """
        if not self.is_ready:
            err_msg = (
                f"GCS ไม่พร้อม ({self._init_error}) — "
                f"ไม่สามารถเขียน metadata ได้"
            )
            logger.error(err_msg)
            if IS_PRODUCTION:
                raise RuntimeError(err_msg)
            return self._fallback.put(path, data, content_type, create_only)

        try:
            blob = self._bucket.blob(path)
            upload_kwargs = {
                "content_type": content_type,
            }
            if create_only:
                upload_kwargs["if_generation_match"] = 0

            blob.upload_from_string(data, **upload_kwargs)
            gcs_uri = f"gs://{self.bucket_name}/{path}"
            # log เฉพาะ prefix ของ path — ไม่ log doc_id เต็ม
            path_prefix = path.split("/")[0] if "/" in path else path
            logger.info(
                f"GCS put สำเร็จ: {path_prefix}/ "
                f"({len(data):,} bytes, {content_type})"
            )
            return gcs_uri
        except Exception as e:
            err_code = getattr(e, "code", None)
            # 412 = object มีอยู่แล้ว (create_only) — ถือว่าสำเร็จ
            if err_code == 412:
                gcs_uri = f"gs://{self.bucket_name}/{path}"
                path_prefix = path.split("/")[0] if "/" in path else path
                logger.info(
                    f"GCS put: object มีอยู่แล้ว ไม่เขียนทับ ({path_prefix}/)"
                )
                return gcs_uri
            # ไม่ log str(e) — อาจมี blob path หรือ project ID
            path_prefix = path.split("/")[0] if "/" in path else path
            logger.error(
                f"GCS put ล้มเหลว: {type(e).__name__} ({path_prefix}/)"
            )
            if IS_PRODUCTION:
                raise RuntimeError(
                    f"GCS put ล้มเหลว — ไม่สามารถบันทึก metadata ได้"
                )
            return self._fallback.put(path, data, content_type, create_only)

    def get(self, path: str) -> bytes | None:
        """อ่านไฟล์จาก path ที่ระบุบน GCS.

        Args:
            path: blob path ใน bucket เช่น _lab_results/abc123.json

        Returns:
            bytes ถ้า object มี, None ถ้าไม่มีหรืออ่านไม่ได้

        หมายเหตุ:
            - ไม่ raise exception — คืน None เมื่อไม่พบหรือ error
            - ไม่ log path เต็ม — ใช้ prefix เพื่อป้องกัน info leak
        """
        if not self.is_ready:
            logger.warning(
                f"GCS ไม่พร้อม — get() return None"
            )
            return None

        try:
            blob = self._bucket.blob(path)
            return blob.download_as_bytes()
        except Exception as e:
            err_code = getattr(e, "code", None)
            if err_code == 404:
                # Object ไม่มี — ไม่ใช่ error ปกติ
                return None
            # Error อื่น — log แต่ไม่ raise
            path_prefix = path.split("/")[0] if "/" in path else path
            logger.warning(
                f"GCS get ล้มเหลว: {type(e).__name__} ({path_prefix}/)"
            )
            return None

    def get_with_generation(self, path: str) -> tuple:
        """อ่านไฟล์จาก GCS พร้อม generation number สำหรับ CAS.

        Returns:
            (bytes, generation: int) ถ้า object มี
            (None, None) ถ้า object ไม่มีหรือ GCS ไม่พร้อม

        generation คือ GCS object generation ที่เพิ่มทุกครั้งที่ object ถูกเขียนใหม่.
        ใช้คู่กับ put_conditional() เพื่อป้องกัน lost-update race.
        """
        if not self.is_ready:
            logger.warning("GCS ไม่พร้อม — get_with_generation() return (None, None)")
            return None, None

        try:
            blob = self._bucket.blob(path)
            # reload() ดึง metadata รวมถึง generation โดยไม่ต้องดาวน์โหลดข้อมูล
            blob.reload()
            data = blob.download_as_bytes()
            return data, blob.generation
        except Exception as e:
            err_code = getattr(e, "code", None)
            if err_code == 404:
                return None, None
            path_prefix = path.split("/")[0] if "/" in path else path
            logger.warning(
                f"GCS get_with_generation ล้มเหลว: {type(e).__name__} ({path_prefix}/)"
            )
            return None, None

    def put_conditional(
        self,
        path: str,
        data: bytes,
        if_generation_match: int,
        content_type: str = "application/json",
    ) -> tuple:
        """เขียนไฟล์แบบ conditional (CAS) บน GCS.

        ใช้ if_generation_match ของ GCS เพื่อป้องกัน lost-update race:
        เขียนสำเร็จก็ต่อเมื่อ object generation ตรงกับที่คาดหวัง
        ถ้า worker อื่นเขียนไปก่อน → generation เปลี่ยน → 412 Precondition Failed

        Args:
            path: blob path ใน bucket
            data: ข้อมูลที่ต้องการเขียน (bytes)
            if_generation_match: generation ที่คาดหวัง (จาก get_with_generation)
            content_type: MIME type

        Returns:
            (True, new_generation) ถ้าเขียนสำเร็จ
            (False, None) ถ้า generation ไม่ตรง (conflict — worker อื่นเขียนไปก่อน)

        Raises:
            RuntimeError: ใน production mode เมื่อเขียนล้มเหลว (ไม่ใช่ conflict)
        """
        if not self.is_ready:
            err_msg = f"GCS ไม่พร้อม ({self._init_error}) — ไม่สามารถเขียน conditional ได้"
            logger.error(err_msg)
            if IS_PRODUCTION:
                raise RuntimeError(err_msg)
            # Local fallback ไม่รองรับ CAS จริง — return failure เพื่อความปลอดภัย
            return False, None

        try:
            blob = self._bucket.blob(path)
            blob.upload_from_string(
                data,
                content_type=content_type,
                if_generation_match=if_generation_match,
            )
            # reload เพื่อดึง new generation
            blob.reload()
            path_prefix = path.split("/")[0] if "/" in path else path
            logger.info(
                f"GCS put_conditional สำเร็จ: {path_prefix}/ "
                f"({len(data):,} bytes, gen={blob.generation})"
            )
            return True, blob.generation
        except Exception as e:
            err_code = getattr(e, "code", None)
            if err_code == 412:
                # 412 Precondition Failed → worker อื่นเขียนไปก่อนแล้ว
                path_prefix = path.split("/")[0] if "/" in path else path
                logger.warning(
                    f"GCS put_conditional: conflict (412) — "
                    f"worker อื่นเขียนไปก่อน ({path_prefix}/)"
                )
                return False, None
            # Error อื่น — ไม่ใช่ conflict
            path_prefix = path.split("/")[0] if "/" in path else path
            logger.error(
                f"GCS put_conditional ล้มเหลว: {type(e).__name__} ({path_prefix}/)"
            )
            if IS_PRODUCTION:
                raise RuntimeError(
                    f"GCS put_conditional ล้มเหลว — ไม่สามารถบันทึกได้"
                )
            return False, None

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

# ─── HN Registry: จัดการรหัส HN คลินิก + HN โรงพยาบาล ────
hn_registry = HNRegistry()
if STORAGE_BACKEND == "gcs" and hasattr(storage, '_bucket'):
    hn_registry.set_storage(storage._bucket)
    logger.info("HNRegistry initialized with GCS bucket")

# ─── Master Patient Registry: ทะเบียนผู้ป่วยกลาง ─────────
master_registry = MasterPatientRegistry()
if STORAGE_BACKEND == "gcs" and hasattr(storage, '_bucket'):
    master_registry.set_storage(storage._bucket)
    logger.info("MasterPatientRegistry initialized with GCS bucket")

# ─── Vendor Registry: ทะเบียนบริษัทคู่ค้า (แฟ้มบิล) ───────
# ย้ายรายชื่อบริษัทออกจาก source code มาอยู่บน GCS เพื่อให้ผู้ดูแล
# เพิ่มบริษัทใหม่ได้เองโดยไม่ต้องแก้โค้ดและ deploy ใหม่
vendor_registry = VendorRegistry()
if STORAGE_BACKEND == "gcs" and hasattr(storage, '_bucket'):
    vendor_registry.set_storage(storage._bucket)
    logger.info("VendorRegistry initialized with GCS bucket")


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

    ออกแบบสำหรับ Gunicorn multi-worker (prefork model):

    ⚠️ Gunicorn ใช้ prefork — แต่ละ worker เป็น process แยก
    threading.Lock ป้องกันเฉพาะ threads ภายใน process เดียวกัน
    ไม่สามารถ lock ข้าม workers ได้

    การป้องกัน cross-worker consistency:
    1. GCS เป็น authoritative store (source of truth)
    2. ทุก read operation โหลดจาก GCS ใหม่ผ่าน short-lived cache (5 วินาที)
    3. add() เขียน GCS ก่อน → ถ้าสำเร็จจึงอัพเดท memory
    4. if_generation_match=0 ป้องกัน duplicate ข้าม workers (create-only)
    5. 412 Precondition Failed = worker อื่นสร้างไปก่อน → reload จาก GCS
    6. Owner isolation ใช้ SHA-256 hash filter — ข้อมูลจาก GCS เสมอ

    threading.Lock ใช้สำหรับ:
    - ป้องกัน concurrent access ภายใน thread pool ของ worker เดียว
    - ไม่ใช้เพื่อ lock ข้าม Gunicorn workers (ไม่สามารถทำได้)
    """

    def __init__(self, storage_instance):
        self._storage = storage_instance
        self._docs: list[dict] = []
        self._doc_map: dict[str, dict] = {}  # doc_id -> doc
        # threading.Lock สำหรับ intra-worker thread safety เท่านั้น
        # cross-worker consistency ป้องกันด้วย GCS (see docstring above)
        self._lock = threading.Lock()
        self._cache_ts: float = 0  # timestamp ที่ load cache ล่าสุด
        self._cache_ttl: float = INDEX_CACHE_TTL

    def _reload_from_gcs(self) -> bool:
        """โหลด index จาก GCS เข้า memory ใหม่ทั้งหมด.

        อ่านจาก 2 แหล่ง:
        1. Legacy _index/documents.jsonl (backward compatibility)
        2. Per-document files _index/docs/{doc_id}.json

        Deduplicate ด้วย doc_id — per-doc files มีความสำคัญกว่า legacy

        Return True ถ้าโหลดสำเร็จ (อย่างน้อย 1 แหล่งอ่านได้)
        Return False ถ้าทุกแหล่งล้มเหลว — เก็บ stale data ไว้ดีกว่าลบทิ้ง

        Phase 14.8 Fix: ป้องกัน silent index wipe เมื่อ GCS reload ล้มเหลว
        Root cause: เมื่อทั้ง legacy JSONL และ list_blobs ล้มเหลว
        new_docs = [] ถูก set เป็น self._docs → index ว่างเปล่า →
        ผู้ใช้เห็น "ไม่พบผลแล็บ" ทั้งที่เอกสารถูกจัดเก็บแล้ว
        """
        if not hasattr(self._storage, '_client') or not self._storage.is_ready:
            return False
        try:
            new_docs = []
            new_map = {}

            # ── Phase 14.8: track ว่าแต่ละแหล่งอ่านสำเร็จหรือไม่ ──
            _legacy_ok = False
            _perdoc_ok = False

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
                    _legacy_ok = True
                else:
                    # JSONL ไม่มี — ถือว่าอ่านสำเร็จ (empty is valid)
                    _legacy_ok = True
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
                else:
                    # 404 = ไม่มีไฟล์ — ถือว่าอ่านสำเร็จ (empty is valid)
                    _legacy_ok = True

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
                _perdoc_ok = True
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

            # ── Phase 14.8: ป้องกัน silent index wipe ──
            # Per-doc files เป็นแหล่งข้อมูลหลัก (legacy เป็น backup)
            # ถ้า per-doc list ล้มเหลว → ไม่ไว้ใจ new_docs
            # เพราะ legacy อาจเป็น empty/404 ตามปกติ (ไม่ได้ write ใน add)
            # → เก็บ stale cache ไว้ดีกว่า replace ด้วย empty
            if not _perdoc_ok:
                if new_docs:
                    # Legacy มีข้อมูล → ใช้ legacy data ได้
                    logger.warning(
                        "Per-doc index list ล้มเหลว แต่มี legacy data "
                        f"({len(new_docs)} docs) — ใช้ legacy data"
                    )
                else:
                    # ไม่มีข้อมูลจากแหล่งใดเลย → เก็บ stale cache
                    logger.error(
                        "Document index reload: per-doc list ล้มเหลว "
                        "และไม่มี legacy data — "
                        "เก็บ stale cache ไว้ "
                        f"(มี {len(self._docs)} docs ใน memory)"
                    )
                    # ไม่อัพเดท cache_ts → จะลอง reload อีกครั้ง
                    return False

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

    def update_category(
        self, doc_id: str, new_category: str, new_gcs_path: str,
    ) -> bool:
        """อัปเดต category และ gcs_object_path ของเอกสาร.

        ใช้เมื่อ reclassify จาก interactive classification
        เขียน per-doc JSON file ใน GCS ทับ doc เดิม
        คืน True ถ้าพบและอัปเดตสำเร็จ
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
        doc["category"] = new_category
        if new_gcs_path:
            if new_gcs_path.startswith("gs://"):
                parts = new_gcs_path.split("/", 3)
                doc["gcs_object_path"] = parts[3] if len(parts) > 3 else ""
            else:
                doc["gcs_object_path"] = new_gcs_path
        # เขียน GCS — overwrite per-doc JSON
        #
        # ต้องใช้ document_id จริงของเอกสารที่หาเจอ ไม่ใช่ค่าที่ส่งเข้ามา
        # เพราะเมธอดนี้ยอมให้ค้นด้วยคำนำหน้า (startswith) ถ้าใช้ค่าที่
        # ส่งเข้ามาตรง ๆ แล้วมีคนเรียกด้วยคำนำหน้า จะเขียน index ไปที่
        # path ผิด กลายเป็นไฟล์ index ปลอมและของจริงไม่ถูกอัปเดต
        # (ปัจจุบันผู้เรียกส่ง id เต็มเสมอ จึงยังไม่เกิดจริง — แก้กันไว้)
        _real_doc_id = doc.get("document_id", "") or doc_id
        if self._storage.is_ready:
            try:
                blob_path = f"{INDEX_DOC_PREFIX}{_real_doc_id}.json"
                doc_json = json.dumps(doc, ensure_ascii=False)
                blob = self._storage._bucket.blob(blob_path)
                blob.upload_from_string(
                    doc_json, content_type="application/json",
                )
                logger.info(
                    f"Document index: updated category "
                    f"doc={_real_doc_id[:8]}… → {new_category}"
                )
            except Exception as e:
                logger.warning(
                    f"Document index update GCS write failed: "
                    f"{type(e).__name__}"
                )
        return True

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

        Phase 14.9: ใช้ append-or-create pattern ที่ต้องการแค่
        storage.objects.create + storage.objects.get permissions.
        ถ้า objects.get ไม่มี (403) → fallback เป็น per-event files
        เพื่อไม่ให้ audit logging ล้มเหลวเงียบ

        Phase 14.9 Safety Check:
        - ใช้ UUID ใน filename ป้องกัน collision เมื่อ 2 workers
          เขียนในเวลาเดียวกัน
        - ถ้า GCS ทั้ง append และ per-event ล้มเหลว → log WARNING
          เพื่อให้ monitoring ตรวจจับได้
        - Local logger.info เป็นแค่ secondary signal (Render log
          ไม่ถาวร) — ไม่ถือเป็น durable audit trail
        """
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "user_hash": user_hash[:8] + "…",
            "details": details or {},
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"

        # ── Track ว่า GCS audit เขียนสำเร็จหรือไม่ ──
        _gcs_audit_ok = False

        # เขียนลง GCS
        if self._storage.is_ready:
            try:
                month_key = datetime.now().strftime("%Y-%m")
                audit_path = f"{AUDIT_PREFIX}{month_key}.jsonl"
                blob = self._storage._bucket.blob(audit_path)
                try:
                    # ลอง append (ต้องมี objects.get สำหรับ download)
                    existing = blob.download_as_text(encoding="utf-8")
                    blob.upload_from_string(
                        existing + line, content_type="application/jsonl"
                    )
                    _gcs_audit_ok = True
                except Exception as get_err:
                    err_msg = str(get_err)
                    if "Not Found" in err_msg or "404" in err_msg:
                        # ไฟล์ยังไม่มี — สร้างใหม่
                        blob.upload_from_string(
                            line, content_type="application/jsonl"
                        )
                        _gcs_audit_ok = True
                    elif "Forbidden" in err_msg or "403" in err_msg:
                        # ── Phase 14.9: Fallback เป็น per-event file ──
                        # ถ้า objects.get ไม่มี permission
                        # ใช้ per-event file แทน append
                        # (ต้องการแค่ objects.create)
                        # ใช้ UUID ป้องกัน filename collision
                        # ระหว่าง Gunicorn workers
                        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                        event_id = uuid.uuid4().hex[:8]
                        event_path = (
                            f"{AUDIT_PREFIX}{month_key}/"
                            f"{ts}-{event_id}-{action}.json"
                        )
                        event_blob = self._storage._bucket.blob(event_path)
                        try:
                            event_blob.upload_from_string(
                                line,
                                content_type="application/json",
                                if_generation_match=0,
                            )
                            _gcs_audit_ok = True
                        except Exception as create_err:
                            logger.error(
                                f"Audit log per-event write ล้มเหลว: "
                                f"{type(create_err).__name__} — "
                                f"ตรวจสอบ IAM: Service Account ต้องมี "
                                f"storage.objects.create "
                                f"สำหรับ path '{AUDIT_PREFIX}*' "
                                f"ใน bucket "
                                f"'{self._storage.bucket_name}'"
                            )
                    else:
                        # Error อื่น — ลองสร้างใหม่
                        blob.upload_from_string(
                            line, content_type="application/jsonl"
                        )
                        _gcs_audit_ok = True
            except Exception as e:
                # แยกประเภท error เพื่อช่วย debug IAM permissions
                err_type = type(e).__name__
                err_msg = str(e)
                if "Forbidden" in err_msg or "403" in err_msg:
                    logger.error(
                        f"Audit log write Forbidden — "
                        f"ตรวจสอบ IAM: Service Account ต้องมี "
                        f"storage.objects.create "
                        f"สำหรับ path '{AUDIT_PREFIX}*' "
                        f"ใน bucket '{self._storage.bucket_name}'"
                    )
                else:
                    logger.error(f"Audit log write ล้มเหลว: {err_type}")
        else:
            logger.warning(
                "Audit log: GCS ไม่พร้อม — audit event ไม่ได้เขียนลง "
                "durable storage (เฉพาะ local log เท่านั้น)"
            )

        # ── ตรวจจับ audit event ที่ไม่ได้เขียนลง durable storage ──
        if not _gcs_audit_ok:
            logger.warning(
                f"AUDIT_PERSISTENCE_FAILURE: action={action} "
                f"user={user_hash[:8]}… — audit event ไม่ได้เขียน "
                f"ลง GCS (durable storage) — ต้องตรวจสอบ"
            )

        # Secondary signal: เขียนลง local log ด้วย
        # หมายเหตุ: Render log ไม่ถาวร — ไม่ถือเป็น durable audit trail
        logger.info(f"AUDIT: {action} user={user_hash[:8]}…")


audit_logger = AuditLogger(storage)


# ─── Phase 2A: Enhanced Audit Trail ─────────────────────
try:
    from audit_trail import (
        PersistentAuditTrail, EnhancedAuditLogger,
        AuditPersistenceError, SECURITY_CRITICAL_ACTIONS,
    )
    enhanced_audit = EnhancedAuditLogger(storage, fail_closed=True)
    logger.info("Phase 2A: Enhanced audit trail initialized")
except ImportError:
    enhanced_audit = None
    logger.warning("Phase 2A: audit_trail module not available")

# ─── Phase 2A: OCR Clinical Safety ──────────────────────
try:
    from ocr_safety import OcrSafetyValidator
    ocr_validator = OcrSafetyValidator()
    logger.info("Phase 2A: OCR safety validator initialized")
except ImportError:
    ocr_validator = None
    logger.warning("Phase 2A: ocr_safety module not available")

# PHYSICIAN_USERS: SHA-256 hash ของ LINE userId ของแพทย์ที่ได้รับอนุมัติ
# ตั้งค่าผ่าน Render environment variable (comma-separated hashes)
PHYSICIAN_USERS = set(
    h.strip() for h in
    os.getenv("PHYSICIAN_USERS", "").split(",")
    if h.strip()
)

# ─── Phase 2A: Physician Approval ───────────────────────
try:
    from physician_approval import (
        PhysicianApprovalService,
        InvalidStateTransitionError,
        StaleVersionError,
        DuplicateApprovalError,
    )
    physician_service = PhysicianApprovalService(
        storage, audit_logger, PHYSICIAN_USERS, ADMIN_USERS,
    )
    logger.info("Phase 2A: Physician approval service initialized")
except ImportError:
    physician_service = None
    InvalidStateTransitionError = Exception
    StaleVersionError = Exception
    DuplicateApprovalError = Exception
    logger.warning("Phase 2A: physician_approval module not available")

# ─── Phase 2A: Critical Alert Delivery ──────────────────
line_push_fn = None
alert_retry_manager = None
try:
    from alert_delivery import (
        AlertDeliveryService, AlertSeverity,
        create_line_push_fn, build_alert_recipients_from_env,
        describe_alert_recipients, resolve_alert_recipients,
        AlertRetryManager, rebuild_dedup_cache,
        format_active_alerts,
    )
    # ผู้รับมาจากสองที่: ENV และทะเบียนบน GCS (เพิ่มด้วย hash)
    # ถ้าอ่านทะเบียนไม่ได้ ยังต้องได้ผู้รับจาก ENV ไม่ปล่อยให้ว่าง
    try:
        _alert_recipients = resolve_alert_recipients(storage)
    except Exception as _ar_err:
        logger.error(
            "อ่านทะเบียนผู้รับไม่สำเร็จ ใช้เฉพาะ ENV: %s",
            type(_ar_err).__name__,
        )
        _alert_recipients = build_alert_recipients_from_env()
    alert_service = AlertDeliveryService(
        storage, audit_logger,
        clinician_recipients=_alert_recipients,
        # [CS-4] production ต้อง fail-closed: ถ้าไม่มีช่องทางส่งจริง
        # alert ต้องเป็น delivery_failed เพื่อเข้า retry/escalation
        # ห้ามบันทึกว่า SENT ทั้งที่ไม่มีใครได้รับ
        allow_simulated_delivery=not IS_PRODUCTION,
    )
    # Create real LINE push function if token available
    _line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if _line_token:
        line_push_fn = create_line_push_fn(_line_token)
        logger.info("Phase 2A: LINE push function configured")
    else:
        logger.warning("Phase 2A: LINE_CHANNEL_ACCESS_TOKEN not set — alert push disabled")
    # Rebuild dedup cache from GCS on startup
    rebuild_dedup_cache(alert_service)
    # Initialize retry manager
    alert_retry_manager = AlertRetryManager(alert_service)
    logger.info("Phase 2A: Alert delivery service initialized with LINE push")
except ImportError:
    alert_service = None
    describe_alert_recipients = None
    logger.warning("Phase 2A: alert_delivery module not available")

# ─── Phase 2A: Patient Identity ─────────────────────────
try:
    from patient_identity import PatientIdentityService
    patient_identity_service = PatientIdentityService(storage, audit_logger)
    logger.info("Phase 2A: Patient identity service initialized")
except ImportError:
    patient_identity_service = None
    logger.warning("Phase 2A: patient_identity module not available")

# ─── Phase 2B: Monitoring Schedule ──────────────────────
try:
    from monitoring_schedule import (
        check_monitoring_status,
        monitoring_report_text_th,
    )
    logger.info("Phase 2B: Monitoring schedule module loaded")
except ImportError:
    check_monitoring_status = None
    monitoring_report_text_th = None
    logger.warning("Phase 2B: monitoring_schedule module not available")

# ─── Phase 2B: Nephrology KPI Dashboard ─────────────────
try:
    from nephrology_kpi import (
        generate_nephrology_dashboard,
        dashboard_text_th,
    )
    logger.info("Phase 2B: Nephrology KPI dashboard module loaded")
except ImportError:
    generate_nephrology_dashboard = None
    dashboard_text_th = None
    logger.warning("Phase 2B: nephrology_kpi module not available")

# ─── Dashboard V2 ─────────────────────────────────────────
try:
    from dashboard import register_dashboard
    register_dashboard(app, storage, doc_index, enhanced_audit)
    logger.info("Dashboard V2: registered successfully")
except ImportError:
    logger.warning("Dashboard V2: dashboard module not available")
except Exception as e:
    logger.warning(f"Dashboard V2: registration failed: {e}")

# ─── OCR Review (Feature 4) ──────────────────────────────
try:
    from ocr_review import register_ocr_review
    register_ocr_review(app, storage, doc_index, enhanced_audit)
    logger.info("OCR Review: registered successfully")
except ImportError:
    logger.warning("OCR Review: ocr_review module not available")
except Exception as e:
    logger.warning(f"OCR Review: registration failed: {e}")

# ─── Lab Dashboard (Feature 5) ───────────────────────────
try:
    from lab_dashboard import register_lab_dashboard
    register_lab_dashboard(app, storage, doc_index, enhanced_audit)
    logger.info("Lab Dashboard: registered successfully")
except ImportError:
    logger.warning("Lab Dashboard: lab_dashboard module not available")
except Exception as e:
    logger.warning(f"Lab Dashboard: registration failed: {e}")

# ─── Patient Assistant (Feature 6) ────────────────────────
try:
    from line_patient_assistant import register_patient_assistant
    register_patient_assistant(app, storage, doc_index, enhanced_audit)
    logger.info("Patient Assistant: registered successfully")
except ImportError:
    logger.warning("Patient Assistant: module not available")
except Exception as e:
    logger.warning(f"Patient Assistant: registration failed: {e}")

# ─── Document Workflow (Feature 7) ───────────────────────
try:
    from document_workflow import register_workflow
    register_workflow(app, storage, doc_index, enhanced_audit)
    logger.info("Document Workflow: registered successfully")
except ImportError:
    logger.warning("Document Workflow: module not available")
except Exception as e:
    logger.warning(f"Document Workflow: registration failed: {e}")


# ═══════════════════════════════════════════════════════════
# Comparison API & HN Registry — Feature: Quarterly Compare
# ═══════════════════════════════════════════════════════════

@app.route("/admin/api/v2/compare/lab", methods=["GET"])
def api_compare_lab():
    """เปรียบเทียบผลแล็บ 2 เอกสาร — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    old_id = request.args.get("old", "").strip()
    new_id = request.args.get("new", "").strip()
    if not old_id or not new_id:
        return jsonify({"error": "ต้องระบุ ?old=DOC_ID&new=DOC_ID"}), 400

    result = compare_lab_reports(storage, old_id, new_id)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


@app.route("/admin/api/v2/compare/patient-labs", methods=["GET"])
def api_compare_patient_labs():
    """เปรียบเทียบผลแล็บล่าสุด vs ก่อนหน้าของผู้ป่วย — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    owner = request.args.get("owner", "").strip()
    if not owner:
        return jsonify({"error": "ต้องระบุ ?owner=OWNER_HASH"}), 400

    result = compare_latest_labs(storage, doc_index, owner)
    if "error" in result:
        return jsonify(result), 404 if "ไม่พบ" in result.get("error", "") else 200
    return jsonify(result)


@app.route("/admin/api/v2/patient-lab-history", methods=["GET"])
def api_patient_lab_history():
    """ประวัติผลแล็บของผู้ป่วย — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    owner = request.args.get("owner", "").strip()
    if not owner:
        return jsonify({"error": "ต้องระบุ ?owner=OWNER_HASH"}), 400

    history = get_patient_lab_history(storage, doc_index, owner)
    # ซ่อน raw results จาก response — แสดงเฉพาะ summary
    for h in history:
        h["results"] = [
            {"analyte_name": r.get("analyte_name", ""), "value": r.get("value")}
            for r in h.get("results", [])
        ]
    return jsonify({"owner": owner[:6], "history": history, "count": len(history)})


@app.route("/admin/api/v2/hn-registry/register", methods=["POST"])
def api_hn_register():
    """ลงทะเบียน HN ผู้ป่วย — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    owner_hash = data.get("owner_hash", "").strip()
    clinic_hn = data.get("clinic_hn", "").strip()
    hospital_hn = data.get("hospital_hn", "").strip()
    display_name = data.get("display_name", "").strip()

    if not owner_hash:
        return jsonify({"error": "ต้องระบุ owner_hash"}), 400
    # [§3.1] ผูกบัญชีผิดคน = เอกสารของผู้ป่วยคนหนึ่งไปอยู่แฟ้มอีกคน
    # จึงต้องตรวจรูปแบบก่อน ไม่รับ LINE userId ดิบ
    if not is_valid_owner_hash(owner_hash):
        return jsonify({
            "error": "owner_hash ผิดรูปแบบ",
            "detail": "ต้องเป็น hex ตัวพิมพ์เล็ก 32 ตัว "
                      "(ผลลัพธ์ของ hash_user_id) — ไม่ใช่ LINE userId ดิบ",
        }), 400
    if not clinic_hn and not hospital_hn:
        return jsonify({"error": "ต้องระบุ clinic_hn หรือ hospital_hn อย่างน้อย 1 รายการ"}), 400

    success, msg = hn_registry.register(
        owner_hash=owner_hash,
        clinic_hn=clinic_hn,
        hospital_hn=hospital_hn,
        display_name=display_name,
        registered_by="admin",
    )
    return jsonify({"success": success, "message": msg}), 200 if success else 400


@app.route("/admin/api/v2/hn-registry/list", methods=["GET"])
def api_hn_list():
    """แสดงรายชื่อ HN ทั้งหมด — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    patients = hn_registry.list_all()
    return jsonify({
        "count": len(patients),
        "patients": [
            {
                "hn_pair": p.hn_pair,
                "clinic_hn": p.clinic_hn,
                "hospital_hn": p.hospital_hn,
                "display_name": p.display_name,
                "owner_hash": p.owner_hash[:6] + "...",
                "registered_at": p.registered_at,
            }
            for p in patients
        ],
    })


@app.route("/admin/api/v2/hn-registry/search", methods=["GET"])
def api_hn_search():
    """ค้นหา HN — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "ต้องระบุ ?q=KEYWORD"}), 400

    results = hn_registry.search_hn(q)
    return jsonify({
        "query": q,
        "count": len(results),
        "results": [
            {
                "hn_pair": p.hn_pair,
                "clinic_hn": p.clinic_hn,
                "hospital_hn": p.hospital_hn,
                "display_name": p.display_name,
            }
            for p in results
        ],
    })


logger.info("Comparison API & HN Registry: endpoints registered")


# ─── Master Patient Registry API ──────────────────────────

@app.route("/admin/api/v2/master-registry/list", methods=["GET"])
def api_master_registry_list():
    """แสดงรายชื่อทะเบียนผู้ป่วย — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    patients = master_registry.list_all()
    return jsonify({
        "count": len(patients),
        "patients": [
            {
                "clinic_hn": p.clinic_hn,
                "hospital_hn": p.hospital_hn,
                "patient_name": p.patient_name,
                "folder_name": p.folder_name,
                "registered_at": p.registered_at,
                "registered_by": p.registered_by,
            }
            for p in patients
        ],
    })


@app.route("/admin/api/v2/master-registry/register", methods=["POST"])
def api_master_registry_register():
    """ลงทะเบียนผู้ป่วย — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    clinic_hn = data.get("clinic_hn", "").strip()
    hospital_hn = data.get("hospital_hn", "").strip()
    patient_name = data.get("patient_name", "").strip()

    if not clinic_hn and not hospital_hn:
        return jsonify({"error": "ต้องระบุ clinic_hn หรือ hospital_hn"}), 400

    success, msg = master_registry.register(
        clinic_hn=clinic_hn,
        hospital_hn=hospital_hn,
        patient_name=patient_name,
        registered_by="admin_api",
    )
    return jsonify({"success": success, "message": msg}), 200 if success else 400


@app.route("/admin/api/v2/master-registry/search", methods=["GET"])
def api_master_registry_search():
    """ค้นหาทะเบียนผู้ป่วย — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "ต้องระบุ ?q=KEYWORD"}), 400

    results = master_registry.search(q)
    return jsonify({
        "query": q,
        "count": len(results),
        "results": [
            {
                "clinic_hn": p.clinic_hn,
                "hospital_hn": p.hospital_hn,
                "patient_name": p.patient_name,
                "folder_name": p.folder_name,
            }
            for p in results
        ],
    })


@app.route("/admin/api/v2/master-registry/delete", methods=["POST"])
def api_master_registry_delete():
    """ลบผู้ป่วยจากทะเบียน — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    clinic_hn = data.get("clinic_hn", "").strip()
    if not clinic_hn:
        return jsonify({"error": "ต้องระบุ clinic_hn"}), 400

    success, msg = master_registry.delete(clinic_hn)
    return jsonify({"success": success, "message": msg}), 200 if success else 400


@app.route("/admin/api/v2/master-registry/update", methods=["POST"])
def api_master_registry_update():
    """แก้ไขข้อมูลผู้ป่วย — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    hn = data.get("hn", "").strip()
    if not hn:
        return jsonify({"error": "ต้องระบุ hn (HN ที่ต้องการแก้ไข)"}), 400

    new_name = data.get("new_name", "").strip()
    new_clinic_hn = data.get("new_clinic_hn", "").strip()
    new_hospital_hn = data.get("new_hospital_hn", "").strip()

    if not new_name and not new_clinic_hn and not new_hospital_hn:
        return jsonify({"error": "ต้องระบุข้อมูลที่ต้องการแก้ไข (new_name, new_clinic_hn, หรือ new_hospital_hn)"}), 400

    success, msg = master_registry.update_patient(
        hn=hn,
        new_name=new_name,
        new_clinic_hn=new_clinic_hn,
        new_hospital_hn=new_hospital_hn,
    )
    return jsonify({"success": success, "message": msg}), 200 if success else 400


# ═══════════════════════════════════════════════════════════
# Admin API — ทะเบียนบริษัทคู่ค้า และการอ่านผลแล็บซ้ำ
#
# Vendor Registry คือเป้าหมายหลักของฟีเจอร์: บริษัทใหม่ลงทะเบียนผ่าน API นี้
# แล้วเอกสารของบริษัทนั้นเข้าแฟ้มเองทันที ไม่ต้อง deploy ใหม่
#
# การยืนยันตัวตน: รับ secret ทาง header เท่านั้น ไม่รับทาง query string
# เพราะ query string ถูกบันทึกใน access log ของ proxy และแพลตฟอร์ม
# ═══════════════════════════════════════════════════════════

def _admin_api_auth() -> "tuple | None":
    """ตรวจสิทธิ์ผู้ดูแล — คืน error response ถ้าไม่ผ่าน, None ถ้าผ่าน

    รับ secret ทาง header เท่านั้น ไม่รับทาง query string เพราะ
    query string ถูกบันทึกใน access log ของ proxy และแพลตฟอร์ม"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "")
    # เทียบเป็น bytes — compare_digest โยน TypeError ถ้า str มีอักขระนอก
    # ASCII (header latin-1 ส่งค่านั้นมาได้) ซึ่งจะกลายเป็น 500 แทน 401
    if not auth or not hmac.compare_digest(
        auth.encode("utf-8", "surrogateescape"),
        admin_secret.encode("utf-8"),
    ):
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/admin/api/v2/vendors/list", methods=["GET"])
def api_vendors_list():
    """รายชื่อบริษัทคู่ค้าทั้งหมดในทะเบียน"""
    err = _admin_api_auth()
    if err:
        return err
    include_inactive = request.args.get("include_inactive") == "1"
    try:
        vendors = vendor_registry.list_all(include_inactive=include_inactive)
    except Exception as e:
        logger.error("vendors/list ล้มเหลว: %s", type(e).__name__)
        return jsonify({"error": "อ่านทะเบียนไม่สำเร็จ"}), 503
    return jsonify({
        "count": len(vendors),
        "vendors": [
            {
                **v.to_dict(),
                "keywords": vendor_registry.keywords_for(v.canonical_name),
            }
            for v in vendors
        ],
    })


@app.route("/admin/api/v2/vendors/suggest", methods=["POST"])
def api_vendors_suggest():
    """วิเคราะห์ข้อความเอกสาร แล้วเสนอว่าควรลงทะเบียนเป็นบริษัทใด

    ใช้ก่อนลงทะเบียน เพื่อให้ผู้ดูแลเห็นว่าระบบอ่านชื่อบริษัทได้ว่าอย่างไร
    และมีบริษัทในทะเบียนที่อาจเป็นรายเดียวกันอยู่แล้วหรือไม่
    """
    err = _admin_api_auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "ต้องระบุ text"}), 400

    match = resolve_bill_vendor(text)
    names = extract_candidate_names(text)
    proposed = to_canonical_name(names[0]) if names else ""
    conflicts = []
    if proposed:
        try:
            conflicts = vendor_registry.find_conflicts(proposed, names)
        except Exception as e:
            logger.error("find_conflicts ล้มเหลว: %s", type(e).__name__)
    return jsonify({
        "match": match.to_dict(),
        "candidate_names": names,
        "proposed_canonical_name": proposed,
        "proposed_is_valid": bool(
            proposed and is_valid_canonical_name(proposed)
        ),
        "possible_duplicates": conflicts,
    })


@app.route("/admin/api/v2/vendors/register", methods=["POST"])
def api_vendors_register():
    """ลงทะเบียนบริษัทคู่ค้าใหม่

    body: canonical_name (ชื่อแฟ้ม A-Z0-9_), display_name, aliases[],
          tax_id (13 หลัก), keywords[], verified, notes, allow_similar
    """
    err = _admin_api_auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    canonical = (data.get("canonical_name") or "").strip()
    if not canonical:
        return jsonify({"error": "ต้องระบุ canonical_name"}), 400
    aliases = data.get("aliases") or []
    if not isinstance(aliases, list):
        return jsonify({"error": "aliases ต้องเป็น list"}), 400
    keywords = data.get("keywords") or []
    if not isinstance(keywords, list):
        return jsonify({"error": "keywords ต้องเป็น list"}), 400

    try:
        ok, msg, record = vendor_registry.register(
            canonical_name=canonical,
            display_name=(data.get("display_name") or "").strip(),
            aliases=aliases,
            tax_id=(data.get("tax_id") or "").strip(),
            keywords=keywords,
            # ยืนยันทันทีได้ เพราะผู้เรียกคือผู้ดูแลที่ถือ secret อยู่แล้ว
            verified=bool(data.get("verified", True)),
            created_by="admin_api",
            notes=(data.get("notes") or "").strip(),
            allow_similar=bool(data.get("allow_similar", False)),
        )
    except Exception as e:
        logger.error("vendors/register ล้มเหลว: %s", type(e).__name__)
        return jsonify({"error": "บันทึกทะเบียนไม่สำเร็จ"}), 503
    return jsonify({
        "success": ok,
        "message": msg,
        "vendor": record.to_dict() if record else None,
    }), 200 if ok else 400


@app.route("/admin/api/v2/vendors/update", methods=["POST"])
def api_vendors_update():
    """แก้ไขบริษัท — เพิ่ม/ลบ alias, แก้เลขผู้เสียภาษี, ยืนยัน, ปิดใช้งาน"""
    err = _admin_api_auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    canonical = (data.get("canonical_name") or "").strip()
    if not canonical:
        return jsonify({"error": "ต้องระบุ canonical_name"}), 400

    kwargs: dict = {"updated_by": "admin_api"}
    for key in ("display_name", "notes"):
        if key in data:
            kwargs[key] = (data.get(key) or "").strip()
    for key in ("add_aliases", "remove_aliases", "keywords"):
        if key in data:
            if not isinstance(data[key], list):
                return jsonify({"error": f"{key} ต้องเป็น list"}), 400
            kwargs[key] = data[key]
    if "tax_id" in data:
        kwargs["tax_id"] = (data.get("tax_id") or "").strip()
    for key in ("verified", "active"):
        if key in data:
            kwargs[key] = bool(data[key])

    try:
        ok, msg, record = vendor_registry.update(canonical, **kwargs)
    except Exception as e:
        logger.error("vendors/update ล้มเหลว: %s", type(e).__name__)
        return jsonify({"error": "บันทึกทะเบียนไม่สำเร็จ"}), 503
    return jsonify({
        "success": ok,
        "message": msg,
        "vendor": record.to_dict() if record else None,
    }), 200 if ok else 400


@app.route("/admin/api/v2/vendors/review-queue", methods=["GET"])
def api_vendors_review_queue():
    """บิลที่ระบุบริษัทไม่ได้ — รอผู้ดูแลตรวจ

    แสดงชื่อบริษัทที่ระบบอ่านได้จากเอกสาร เพื่อให้ผู้ดูแลลงทะเบียน
    บริษัทใหม่แล้วย้ายเอกสารเข้าแฟ้มด้วยคำสั่ง "ย้ายเอกสาร"
    """
    err = _admin_api_auth()
    if err:
        return err
    limit = min(max(int(request.args.get("limit", "100") or 100), 1), 500)
    pending: list[dict] = []
    try:
        doc_index._ensure_fresh()
        for d in reversed(doc_index._docs):
            if d.get("category") != "bills":
                continue
            folder = d.get("bill_company", "")
            if folder not in (REVIEW_VENDOR_FOLDER, UNKNOWN_VENDOR_FOLDER):
                continue
            pending.append({
                "document_id": d.get("document_id", "")[:8],
                "upload_date": d.get("upload_date", ""),
                "folder": folder,
                "extracted_name": d.get("vendor_extracted_name", ""),
                "method": d.get("vendor_method", ""),
            })
            if len(pending) >= limit:
                break
    except Exception as e:
        logger.error("vendors/review-queue ล้มเหลว: %s", type(e).__name__)
        return jsonify({"error": "อ่าน index ไม่สำเร็จ"}), 503
    return jsonify({"count": len(pending), "documents": pending})


@app.route("/admin/api/v2/vendors/relocation-plan", methods=["GET"])
def api_vendors_relocation_plan():
    """แผนย้ายบิลเดิมที่อยู่ผิดแฟ้ม — อ่านอย่างเดียว ไม่ย้ายอะไร

    เอกสารที่จัดเก็บก่อนมีทะเบียนบริษัทจะอยู่ที่
    02_BILLS_RECEIPTS/YYYY-MM/ หรือแฟ้มที่คำนวณจากเดือน UTC
    endpoint นี้คำนวณว่าแต่ละใบ "ควร" อยู่ที่ใด แล้วคืนเป็นแผน

    ไม่คัดลอก ไม่ย้าย ไม่ลบ และไม่แก้ index — การย้ายจริงต้องได้รับ
    อนุมัติจากผู้ใช้ก่อน ตามข้อกำหนด
    """
    err = _admin_api_auth()
    if err:
        return err
    limit = min(max(int(request.args.get("limit", "200") or 200), 1), 1000)

    plan: list[dict] = []
    unchanged = 0
    needs_review = 0
    try:
        doc_index._ensure_fresh()
        existing_paths = {
            d.get("gcs_object_path", "").replace(
                f"gs://{STORAGE_BUCKET}/", ""
            )
            for d in doc_index._docs
        }
        for d in doc_index._docs:
            if d.get("category") != "bills":
                continue
            raw = d.get("gcs_object_path", "")
            current = raw.replace(f"gs://{STORAGE_BUCKET}/", "")
            if not current:
                continue

            match = resolve_bill_vendor(d.get("extracted_text", ""))
            if match.needs_review:
                needs_review += 1
                continue

            filename = current.rsplit("/", 1)[-1]
            proposed = build_document_path(
                FOLDERS["bills"], filename, "bills",
                subfolder=match.folder,
                date=_doc_submitted_date(d),
            )
            if proposed == current:
                unchanged += 1
                continue
            plan.append({
                "document_id": d.get("document_id", "")[:8],
                "upload_date": d.get("upload_date", ""),
                "current_gcs_path": current,
                "proposed_gcs_path": proposed,
                # Windows sync ดึงจาก GCS จึงใช้โครงสร้างเดียวกัน
                # ไม่ระบุ drive letter เพราะ sync root ตั้งที่เครื่องคลินิก
                "proposed_relative_windows_path": proposed.replace("/", "\\"),
                "content_sha256": d.get("content_sha256", "") or "ไม่มีในindex",
                "vendor": match.folder,
                "vendor_method": match.method,
                "vendor_confidence": match.confidence,
                "collision_risk": proposed in existing_paths,
            })
            if len(plan) >= limit:
                break
    except Exception as e:
        logger.error("relocation-plan ล้มเหลว: %s", type(e).__name__)
        return jsonify({"error": "อ่าน index ไม่สำเร็จ"}), 503

    return jsonify({
        "dry_run": True,
        "applied": False,
        "note": (
            "แผนนี้ยังไม่ถูกดำเนินการ การย้ายไฟล์จริงต้องได้รับอนุมัติ "
            "และจะใช้วิธีคัดลอกแล้วอัปเดต index โดยไม่ลบไฟล์ต้นฉบับ"
        ),
        "to_move": len(plan),
        "already_correct": unchanged,
        "needs_vendor_review": needs_review,
        "rollback": (
            "ไฟล์ต้นฉบับไม่ถูกลบ การย้อนกลับทำได้โดยชี้ "
            "gcs_object_path ใน index กลับไปที่ current_gcs_path"
        ),
        "plan": plan,
    })


# ═══════════════════════════════════════════════════════════
# Lab Re-extraction API
# ═══════════════════════════════════════════════════════════

# เครื่องหมายที่คงไว้ในรูปแบบบรรทัด — เป็นตัวบอกการแบ่งคอลัมน์
_PATTERN_KEEP = frozenset(".,:;|=()[]{}/<>+-*%#")


def _masked_line_patterns(text: str, limit: int = 12) -> list:
    """คืน "รูปแบบ" ของบรรทัด โดยปิดบังเนื้อหาทั้งหมด

    ตัวเลขทุกตัว -> 9   ตัวอักษรทุกตัว (ไทย/ละติน) -> x
    เครื่องหมายวรรคตอนและช่องว่างคงไว้ เพราะเป็นตัวบอกโครงสร้างคอลัมน์

    ใช้ระบุว่าเอกสารจัดวางแบบไหน เมื่อตัวนับรูปร่างบรรทัดยังไม่พอชี้ชัด
    ผลลัพธ์อ่านไม่ได้ว่าเขียนอะไรไว้ จึงไม่มีชื่อผู้ป่วย ไม่มีชื่อรายการ
    ตรวจ และไม่มีค่าผลตรวจหลุดออกมา

    ตัวอย่าง: "Creatinine  5.2  mg/dL  (0.6-1.2)"
         ->   "xxxxxxxxxx  9.9  xx/xx  (9.9-9.9)"
    """
    out = []
    for raw in (text or "").split("\n"):
        ln = raw.rstrip()
        if not ln.strip():
            continue
        # whitelist: เก็บเฉพาะช่องว่างและเครื่องหมายที่บอกโครงสร้างคอลัมน์
        # ที่เหลือแปลงหมด — ตัวเลขเป็น 9 อย่างอื่นเป็น x
        # ใช้ whitelist เพราะ blocklist ทิ้งสระและวรรณยุกต์ไทยไว้
        masked = "".join(
            " " if ch in " \t"
            else ("9" if ch.isdigit() else (ch if ch in _PATTERN_KEEP else "x"))
            for ch in ln
        )
        out.append(masked[:110])
        if len(out) >= limit:
            break
    return out


def _line_shape(text: str) -> dict:
    """นับ "รูปร่าง" ของบรรทัดในเอกสาร โดยไม่เปิดเผยเนื้อหา

    ใช้ระบุว่าใบแล็บจัดวางแบบไหน เมื่ออ่านค่าไม่ออกและต้องรู้ว่า
    ควรแก้ตัวแยกค่าอย่างไร โดยไม่ต้องเปิดอ่านเอกสารผู้ป่วย

    แปลผล:
      name_with_number สูง  → ชื่อกับค่าอยู่บรรทัดเดียวกัน (ตาราง)
      name_only สูง         → ชื่อแยกบรรทัดจากค่า (คอลัมน์/แนวตั้ง)
      number_only สูง       → มีค่าลอยอยู่โดยไม่มีชื่อกำกับ
      max_name_run          → ชื่อเรียงติดกันยาวสุดกี่บรรทัด
    """
    try:
        from lab_extractor import _normalize_analyte
    except ImportError:
        return {}
    name_only = name_and_number = number_only = other = 0
    run = max_run = 0
    for raw in (text or "").split("\n"):
        ln = raw.strip()
        if not ln:
            continue
        has_num = bool(re.search(r"\d", ln))
        # ชื่อรายการตรวจคือข้อความ "ก่อนตัวเลขตัวแรก"
        # ตัดทั้งบรรทัดที่ตัวเลขไม่ได้ เพราะหน่วยที่อยู่หลังค่า
        # (mg/dL) จะติดมากับชื่อและทำให้เทียบไม่ติด
        name_part = re.split(r"\d", ln, maxsplit=1)[0].strip(" :=|\t-")
        is_name = bool(_normalize_analyte(name_part or ln))
        if is_name and has_num:
            name_and_number += 1
            run = 0
        elif is_name:
            name_only += 1
            run += 1
            max_run = max(max_run, run)
        elif has_num and not name_part:
            number_only += 1
            run = 0
        else:
            other += 1
            run = 0
    return {
        "lines_name_with_number": name_and_number,
        "lines_name_only": name_only,
        "lines_number_only": number_only,
        "lines_other": other,
        "max_consecutive_name_lines": max_run,
    }


def _count_analyte_mentions(text: str) -> int:
    """นับว่าพบชื่อรายการตรวจที่ระบบรู้จักกี่ชนิดในข้อความ

    ใช้วินิจฉัยว่าเอกสารที่อ่านค่าไม่ออก "เป็นใบแล็บแต่รูปแบบแปลก"
    หรือ "ไม่ใช่ใบแล็บตั้งแต่แรก" — คืนเฉพาะจำนวน ไม่บอกว่าชนิดใด
    """
    try:
        from lab_extractor import ANALYTE_ALIASES
    except ImportError:
        return -1
    low = (text or "").lower()
    seen = set()
    for alias, canonical in ANALYTE_ALIASES.items():
        # alias สั้นมาก (เช่น "k", "na") จับพลาดง่าย จึงนับเฉพาะที่ยาวพอ
        if len(alias) >= 3 and alias.lower() in low:
            seen.add(canonical)
    return len(seen)


@app.route("/admin/api/v2/alerts/recipients", methods=["GET", "POST"])
def api_alert_recipients():
    """จัดการผู้รับการแจ้งเตือนค่าวิกฤต — อ้างอิงด้วย hash เท่านั้น

    ทำไมต้องมี: ALERT_RECIPIENTS ใน Render ต้องใช้ LINE user ID ดิบ
    แต่คำสั่งลงทะเบียนของระบบตั้งใจไว้ว่า "ไม่แสดง LINE userId จริง"
    (มาตรการลดการเปิดเผยตัวระบุตัวตนตาม PDPA) สองอย่างนี้ขัดกันตรง ๆ
    และเป็นเหตุที่การแจ้งเตือนค่าวิกฤตไปถึงแพทย์คนเดียว
    เพิ่มพยาบาลไม่ได้เลย

    วิธีทำงาน: เจ้าตัวพิมพ์ "ยืนยัน <รหัส>" ใน LINE → ระบบเก็บคู่
    hash -> LINE ID ไว้เอง → ผู้ดูแลเพิ่มผู้รับด้วย hash ที่เจ้าตัว
    ได้รับ → ระบบไปหา ID ดิบเองตอนส่ง

    ไม่มี endpoint ใดคืน LINE user ID และการเพิ่ม/ลบมีผลทันที
    ไม่ต้อง redeploy

    GET                      รายการผู้รับ (hash 12 ตัวแรก + บทบาท)
    POST {hash, role}        เพิ่ม/แก้บทบาท  role: physician|nurse|admin
    POST {hash, remove:true} ปิดการใช้งาน (ไม่ลบประวัติ)
    """
    err = _admin_api_auth()
    if err:
        return err

    if alert_service is None:
        return jsonify({"error": "ระบบแจ้งเตือนไม่พร้อมใช้งาน"}), 503

    try:
        from alert_delivery import (
            add_recipient, remove_recipient, list_recipients,
            resolve_alert_recipients, describe_alert_recipients,
        )
    except ImportError:
        return jsonify({"error": "alert_delivery ไม่พร้อมใช้งาน"}), 503

    def _state():
        return {
            "recipients": list_recipients(storage),
            "delivery": describe_alert_recipients(storage),
        }

    if request.method == "GET":
        return jsonify(_state())

    body = request.get_json(silent=True) or {}
    user_hash = str(body.get("hash", "")).strip()
    actor = (request.headers.get("X-Admin-Actor", "") or "admin")[:16]

    if body.get("remove") is True:
        res = remove_recipient(storage, user_hash, actor)
    else:
        res = add_recipient(
            storage, user_hash,
            str(body.get("role", "nurse")).strip().lower(),
            actor, str(body.get("note", "")).strip(),
        )

    if res.get("error"):
        return jsonify(res), 400

    # มีผลทันที — ไม่ต้องรอ redeploy
    try:
        alert_service.set_recipients(resolve_alert_recipients(storage))
    except Exception as e:
        logger.error(
            "โหลดผู้รับใหม่หลังแก้ทะเบียนไม่สำเร็จ: %s", type(e).__name__
        )
        return jsonify({
            **res,
            "warning": (
                "บันทึกทะเบียนแล้ว แต่โหลดเข้าระบบส่งไม่สำเร็จ — "
                "การเปลี่ยนแปลงจะมีผลหลัง redeploy"
            ),
        }), 200

    audit_logger.log("alert_recipient_changed", actor, {
        "action": res.get("status", ""),
        "hash_prefix": user_hash[:12],
        "role": res.get("role", ""),
    })
    return jsonify({**res, **_state()})


@app.route("/admin/api/v2/labs/reextract", methods=["GET", "POST"])
def api_labs_reextract():
    """อ่านค่าผลแล็บจากเอกสารที่เก็บไว้แล้วซ้ำอีกครั้ง

    ทำไมต้องมี: ตัวแยกค่าเดิมบังคับว่าต้องมี "หน่วย" ต่อท้ายค่าเสมอ
    ใบแล็บที่ไม่เป็นรูปแบบนั้นจึงถูกเก็บไว้โดยไม่เคยถูกแปลงเป็นตัวเลข
    เครื่องตรวจจับค่าวิกฤตจึงไม่มีทางเห็นเอกสารเหล่านั้นเลย
    เมื่อแก้ตัวแยกค่าแล้ว ต้องย้อนกลับไปอ่านเอกสารเดิมใหม่

    ค่าเริ่มต้นเป็น dry-run — ดูว่าจะได้อะไรบ้างโดยไม่เขียนอะไรทั้งสิ้น
    ต้องส่ง ?apply=1 จึงจะเขียนจริง

    ผลลัพธ์ไม่มีข้อมูลผู้ป่วย: คืนเฉพาะรหัสเอกสาร 8 ตัว วันที่ และ
    จำนวนค่าที่อ่านได้/จำนวนค่าวิกฤต ไม่มีชื่อผู้ป่วยและไม่มีค่าผลตรวจ

    ไม่ส่งการแจ้งเตือนย้อนหลังโดยเจตนา — เอกสารเก่าอาจมีหลายใบ
    การ push ทีเดียวจำนวนมากทำให้แพทย์แยกไม่ออกว่าอันไหนเร่งด่วนจริง
    ผลลัพธ์บอกว่าเอกสารใบไหนมีค่าวิกฤต ให้เปิดดูได้ตรงจุด
    """
    err = _admin_api_auth()
    if err:
        return err

    apply_changes = (
        request.args.get("apply") == "1"
        or (request.get_json(silent=True) or {}).get("apply") is True
    )
    include_review = request.args.get("include_review") == "1"
    categories = {"lab", "review"} if include_review else {"lab"}

    try:
        from lab_extractor import extract_lab_results_from_text
        from lab_models import ExtractionMethod
    except ImportError:
        return jsonify({"error": "lab_extractor ไม่พร้อมใช้งาน"}), 503

    scanned = 0
    already_had = 0
    no_text = 0
    still_empty = 0
    extracted: list[dict] = []
    unreadable: list[dict] = []
    write_failed = 0

    try:
        doc_index._ensure_fresh()
        for d in list(doc_index._docs):
            if d.get("category") not in categories:
                continue
            scanned += 1
            doc_id = d.get("document_id", "")
            if not doc_id:
                continue

            lab_path = f"{LAB_RESULTS_PREFIX}{doc_id}.json"
            try:
                if storage.get(lab_path) is not None:
                    already_had += 1
                    continue
            except Exception:
                pass  # อ่านไม่ได้ → ถือว่ายังไม่มี แล้วใช้ create_only กันเขียนทับ

            text = d.get("extracted_text", "")
            if not text or not text.strip():
                no_text += 1
                continue

            method = (
                ExtractionMethod.REGEX_OCR.value
                if str(d.get("ocr_status", "")).startswith("ocr")
                else ExtractionMethod.REGEX_PDF_TEXT.value
            )
            try:
                report = extract_lab_results_from_text(text, doc_id, method)
            except Exception as ex_err:
                logger.warning(
                    "reextract: แยกค่าล้มเหลว doc=%s: %s",
                    doc_id[:8], type(ex_err).__name__,
                )
                continue

            if not report.results:
                still_empty += 1
                # เก็บสัญญาณที่ "ไม่ใช่ข้อมูลผู้ป่วย" ไว้วินิจฉัยว่าทำไมอ่านไม่ออก
                #
                # แยกสองกรณีที่ต้องจัดการคนละแบบ:
                #   ไม่พบชื่อรายการตรวจเลย → น่าจะไม่ใช่ใบแล็บตั้งแต่แรก
                #                             (จัดหมวดผิด เช่นใบตรวจ OPD)
                #   พบชื่อรายการตรวจแต่อ่านค่าไม่ได้ → เป็นใบแล็บจริง
                #                             แต่รูปแบบยังไม่รองรับ
                #
                # รายงานเฉพาะ "จำนวน" ไม่บอกว่าเป็นรายการตรวจอะไร
                # และไม่บอกค่า — ชื่อรายการตรวจรวมกับวันที่พอจะชี้ตัวผู้ป่วยได้
                unreadable.append({
                    "document_id": doc_id[:8],
                    "upload_date": d.get("upload_date", ""),
                    "ocr_status": str(d.get("ocr_status", "")),
                    "text_length": len(text),
                    "analyte_names_found": _count_analyte_mentions(text),
                    "numbers_found": len(re.findall(r"\d+(?:\.\d+)?", text)),
                    # รูปร่างของบรรทัด — บอกว่าเอกสารจัดวางแบบไหน
                    # โดยไม่เปิดเผยว่าเขียนอะไรไว้
                    **_line_shape(text),
                    "line_patterns": _masked_line_patterns(text),
                })
                continue

            n_critical = sum(
                1 for r in report.results
                if any("critical" in f for f in r.flags)
            )
            # ไม่ใส่ชื่อ analyte หรือค่าผลตรวจ — เป็นข้อมูลผู้ป่วย
            entry = {
                "document_id": doc_id[:8],
                "upload_date": d.get("upload_date", ""),
                "category": d.get("category", ""),
                "results": len(report.results),
                "critical": n_critical,
                "written": False,
            }

            if apply_changes:
                try:
                    storage.put(
                        lab_path,
                        report.to_json().encode("utf-8"),
                        content_type="application/json",
                        create_only=True,
                    )
                    entry["written"] = True
                except Exception as put_err:
                    write_failed += 1
                    logger.error(
                        "reextract: เขียนผลแล็บไม่สำเร็จ doc=%s: %s",
                        doc_id[:8], type(put_err).__name__,
                    )
            extracted.append(entry)
    except Exception as e:
        logger.error("reextract ล้มเหลว: %s", type(e).__name__)
        return jsonify({"error": "ประมวลผลไม่สำเร็จ"}), 503

    total_critical = sum(e["critical"] for e in extracted)
    return jsonify({
        "dry_run": not apply_changes,
        "applied": bool(apply_changes),
        "categories": sorted(categories),
        "scanned": scanned,
        "already_extracted": already_had,
        "no_text_available": no_text,
        "newly_readable": len(extracted),
        "still_unreadable": still_empty,
        "write_failed": write_failed,
        "documents_with_critical_values": total_critical,
        "note": (
            "dry-run: ยังไม่เขียนอะไร ส่ง ?apply=1 เพื่อบันทึกจริง"
            if not apply_changes else
            "บันทึกผลแล็บแล้ว ไม่ส่งการแจ้งเตือนย้อนหลัง"
        ),
        "documents": extracted,
        "unreadable_documents": unreadable,
    })




@app.route("/admin/registry")
def admin_registry_page():
    """หน้าเว็บทะเบียนผู้ป่วย — เปิดจาก browser ได้เลย (same-origin)"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    auth = request.args.get("secret", "")
    # compare_digest — เทียบเวลาคงที่ เหมือน _admin_api_auth
    # เทียบเป็น bytes เพราะ compare_digest โยน TypeError ถ้า str มีอักขระ
    # นอก ASCII (เช่น ?secret=ไทย) ซึ่งจะกลายเป็น 500 แทนที่จะเป็น 401
    if not admin_secret or not hmac.compare_digest(
        auth.encode("utf-8"), admin_secret.encode("utf-8")
    ):
        return "<h2>Unauthorized</h2><p>กรุณาเพิ่ม ?secret=... ใน URL</p>", 401
    return REGISTRY_HTML.replace("__ADMIN_SECRET__", admin_secret)


REGISTRY_HTML = r'''<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ทะเบียนผู้ป่วย — คลินิกระนอง รักษ์ไต</title>
<style>
  :root {
    --bg: #f8f9fb; --surface: #fff; --fg: #1a1d23; --fg2: #5a6070;
    --accent: #2563eb; --accent-light: #dbeafe;
    --success: #059669; --success-light: #d1fae5;
    --warning: #d97706; --danger: #dc2626; --danger-light: #fee2e2;
    --border: #e2e5ea; --input-bg: #f1f3f6;
    --shadow: 0 1px 3px rgba(0,0,0,.06);
    --font: system-ui, -apple-system, sans-serif;
    --mono: 'Courier New', monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0f1117; --surface: #1a1d26; --fg: #e8eaef; --fg2: #8b90a0;
      --accent: #60a5fa; --accent-light: #1e3a5f;
      --success: #34d399; --success-light: #064e3b;
      --danger: #f87171; --danger-light: #7f1d1d;
      --border: #2a2d38; --input-bg: #252830;
      --shadow: 0 1px 3px rgba(0,0,0,.3);
    }
  }
  :root[data-theme="dark"] {
    --bg: #0f1117; --surface: #1a1d26; --fg: #e8eaef; --fg2: #8b90a0;
    --accent: #60a5fa; --accent-light: #1e3a5f;
    --success: #34d399; --success-light: #064e3b;
    --danger: #f87171; --danger-light: #7f1d1d;
    --border: #2a2d38; --input-bg: #252830;
    --shadow: 0 1px 3px rgba(0,0,0,.3);
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--fg); font-family: var(--font); padding: 0 16px; }
  .app { max-width: 960px; margin: 0 auto; padding: 24px 0 60px; }
  .header { display: flex; align-items: center; gap: 14px; margin-bottom: 20px; flex-wrap: wrap; }
  .header h1 { font-size: 1.4rem; font-weight: 700; }
  .badge { background: var(--accent-light); color: var(--accent); font-size: .75rem; font-weight: 600; padding: 3px 10px; border-radius: 20px; }
  .conn-ok { color: var(--success); font-size: .85rem; font-weight: 500; }
  .stats { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
  .stat { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; flex: 1; min-width: 130px; box-shadow: var(--shadow); }
  .stat .val { font-size: 1.5rem; font-weight: 700; }
  .stat .lbl { font-size: .75rem; color: var(--fg2); margin-top: 2px; }
  .toolbar { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; align-items: center; }
  .search-box { flex: 1; min-width: 180px; position: relative; }
  .search-box input { width: 100%; padding: 9px 12px 9px 36px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--fg); font-size: .88rem; }
  .search-box input:focus { outline: 2px solid var(--accent); border-color: transparent; }
  .search-box svg { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); width: 18px; height: 18px; color: var(--fg2); }
  .btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border: none; border-radius: 8px; font-size: .85rem; font-weight: 500; cursor: pointer; transition: opacity .15s; }
  .btn:hover { opacity: .85; } .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-success { background: var(--success); color: #fff; }
  .btn-danger { background: var(--danger); color: #fff; }
  .btn-outline { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
  .btn-sm { padding: 5px 10px; font-size: .78rem; }
  .table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow-x: auto; box-shadow: var(--shadow); }
  table { width: 100%; border-collapse: collapse; font-size: .88rem; }
  th { text-align: left; padding: 12px 16px; font-weight: 600; font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; color: var(--fg2); border-bottom: 2px solid var(--border); white-space: nowrap; }
  td { padding: 10px 16px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--accent-light); }
  .hn { font-family: var(--mono); font-weight: 500; }
  .name-cell { font-weight: 500; }
  .actions { display: flex; gap: 6px; }
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: none; align-items: center; justify-content: center; z-index: 100; padding: 20px; }
  .modal-overlay.show { display: flex; }
  .modal { background: var(--surface); border-radius: 14px; width: 100%; max-width: 460px; box-shadow: 0 20px 60px rgba(0,0,0,.3); }
  .modal-head { padding: 16px 20px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  .modal-head h2 { font-size: 1rem; font-weight: 600; }
  .modal-body { padding: 20px; display: flex; flex-direction: column; gap: 14px; }
  .modal-foot { padding: 14px 20px; border-top: 1px solid var(--border); display: flex; gap: 10px; justify-content: flex-end; }
  .field label { display: block; font-size: .78rem; font-weight: 500; color: var(--fg2); margin-bottom: 4px; }
  .field input { width: 100%; padding: 9px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--input-bg); color: var(--fg); font-size: .9rem; }
  .field input:focus { outline: 2px solid var(--accent); border-color: transparent; }
  .toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); padding: 12px 22px; border-radius: 10px; font-size: .88rem; font-weight: 500; z-index: 200; opacity: 0; transition: opacity .3s; max-width: calc(100% - 40px); }
  .toast.show { opacity: 1; } .toast.ok { background: var(--success); color: #fff; } .toast.err { background: var(--danger); color: #fff; }
  .empty { text-align: center; padding: 50px 20px; color: var(--fg2); }
  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .6s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <h1>🏥 ทะเบียนผู้ป่วย — คลินิกระนอง รักษ์ไต</h1>
    <div class="badge" id="countBadge">0 คน</div>
    <div class="conn-ok" id="connStatus"></div>
  </div>
  <div class="stats" id="statsBar">
    <div class="stat"><div class="val" id="totalCount">-</div><div class="lbl">ผู้ป่วยทั้งหมด</div></div>
    <div class="stat"><div class="val" id="withHospHN">-</div><div class="lbl">มี HN โรงพยาบาล</div></div>
    <div class="stat"><div class="val" id="withoutName">-</div><div class="lbl">ยังไม่มีชื่อ</div></div>
  </div>
  <div class="toolbar">
    <div class="search-box">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
      <input type="text" id="searchInput" placeholder="ค้นหาชื่อ หรือ HN..." oninput="filterTable()">
    </div>
    <button class="btn btn-success btn-sm" onclick="showAddModal()">+ เพิ่มผู้ป่วย</button>
    <button class="btn btn-outline btn-sm" onclick="loadPatients()" id="refreshBtn">รีเฟรช</button>
  </div>
  <div class="table-wrap">
    <table><thead><tr><th>#</th><th>HN คลินิก</th><th>HN รพ.</th><th>ชื่อ-สกุล</th><th>วันลงทะเบียน</th><th>จัดการ</th></tr></thead>
    <tbody id="tbody"><tr><td colspan="6"><div class="empty"><span class="spinner"></span> กำลังโหลด...</div></td></tr></tbody></table>
  </div>
</div>

<div class="modal-overlay" id="editModal">
  <div class="modal">
    <div class="modal-head"><h2 id="modalTitle">แก้ไขข้อมูลผู้ป่วย</h2><button class="btn" onclick="closeModal('editModal')" style="background:none;font-size:1.2rem">✕</button></div>
    <div class="modal-body">
      <input type="hidden" id="editHN">
      <div class="field"><label>HN คลินิก</label><input type="text" id="editClinicHN"></div>
      <div class="field"><label>HN โรงพยาบาล</label><input type="text" id="editHospHN"></div>
      <div class="field"><label>ชื่อ-สกุล</label><input type="text" id="editName"></div>
    </div>
    <div class="modal-foot"><button class="btn btn-outline btn-sm" onclick="closeModal('editModal')">ยกเลิก</button><button class="btn btn-primary btn-sm" id="saveEditBtn" onclick="saveEdit()">บันทึก</button></div>
  </div>
</div>

<div class="modal-overlay" id="addModal">
  <div class="modal">
    <div class="modal-head"><h2>เพิ่มผู้ป่วยใหม่</h2><button class="btn" onclick="closeModal('addModal')" style="background:none;font-size:1.2rem">✕</button></div>
    <div class="modal-body">
      <div class="field"><label>HN คลินิก *</label><input type="text" id="addClinicHN" placeholder="เช่น 56001"></div>
      <div class="field"><label>HN โรงพยาบาล</label><input type="text" id="addHospHN" placeholder="เช่น 0002567"></div>
      <div class="field"><label>ชื่อ-สกุล *</label><input type="text" id="addName" placeholder="ชื่อ นามสกุล"></div>
    </div>
    <div class="modal-foot"><button class="btn btn-outline btn-sm" onclick="closeModal('addModal')">ยกเลิก</button><button class="btn btn-success btn-sm" onclick="saveAdd()">เพิ่ม</button></div>
  </div>
</div>

<div class="modal-overlay" id="delModal">
  <div class="modal">
    <div class="modal-head"><h2>ยืนยันลบผู้ป่วย</h2></div>
    <div class="modal-body"><p id="delMsg"></p><input type="hidden" id="delHN"></div>
    <div class="modal-foot"><button class="btn btn-outline btn-sm" onclick="closeModal('delModal')">ยกเลิก</button><button class="btn btn-danger btn-sm" onclick="confirmDelete()">ลบ</button></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const SECRET = "__ADMIN_SECRET__";
let patients = [];

function api(path, opts={}) {
  // ส่ง secret ทาง header เท่านั้น — ไม่ใส่ใน query string
  // เพราะ query string ถูกบันทึกลง access log ของ proxy และแพลตฟอร์ม
  // (ฝั่ง server ก็ไม่อ่านจาก query string อยู่แล้ว — ดู _admin_api_auth)
  const url = new URL(path, location.origin);
  const h = {"X-Admin-Secret": SECRET};
  if (opts.body) h["Content-Type"] = "application/json";
  return fetch(url, {method: opts.method||"GET", headers: h, body: opts.body ? JSON.stringify(opts.body) : undefined}).then(r => r.json());
}

function toast(msg, ok=true) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.className = "toast show " + (ok?"ok":"err");
  setTimeout(() => t.className = "toast", 3500);
}
function closeModal(id) { document.getElementById(id).classList.remove("show"); }
function esc(s) { return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;"); }

async function loadPatients() {
  try {
    const d = await api("/admin/api/v2/master-registry/list");
    if (d.error) throw new Error(d.error);
    patients = d.patients || [];
    renderTable(); updateStats();
    document.getElementById("connStatus").textContent = "✅ เชื่อมต่อแล้ว";
  } catch(e) { toast("โหลดไม่ได้: "+e.message, false); }
}

function updateStats() {
  document.getElementById("totalCount").textContent = patients.length;
  document.getElementById("countBadge").textContent = patients.length + " คน";
  document.getElementById("withHospHN").textContent = patients.filter(p=>p.hospital_hn).length;
  document.getElementById("withoutName").textContent = patients.filter(p=>!p.patient_name).length;
}

function filterTable() {
  const q = document.getElementById("searchInput").value.toLowerCase().trim();
  renderTable(q);
}

function renderTable(q="") {
  const tbody = document.getElementById("tbody");
  let list = patients;
  if (q) list = patients.filter(p=>(p.clinic_hn||"").toLowerCase().includes(q)||(p.hospital_hn||"").toLowerCase().includes(q)||(p.patient_name||"").toLowerCase().includes(q));
  if (!list.length) { tbody.innerHTML = '<tr><td colspan="6"><div class="empty">'+(q?"ไม่พบผู้ป่วยที่ค้นหา":"ยังไม่มีรายชื่อ")+"</div></td></tr>"; return; }
  tbody.innerHTML = list.map((p,i) => {
    const dt = p.registered_at ? new Date(p.registered_at).toLocaleDateString("th-TH",{day:"numeric",month:"short",year:"numeric"}) : "-";
    return `<tr><td>${i+1}</td><td class="hn">${esc(p.clinic_hn||"-")}</td><td class="hn">${esc(p.hospital_hn||"-")}</td><td class="name-cell">${esc(p.patient_name||"ไม่มีชื่อ")}</td><td style="color:var(--fg2);font-size:.82rem">${dt}</td><td><div class="actions"><button class="btn btn-outline btn-sm" onclick="showEditModal('${esc(p.clinic_hn)}','${esc(p.hospital_hn)}','${esc(p.patient_name)}')" title="แก้ไข">✏️</button><button class="btn btn-sm" style="background:var(--danger-light);color:var(--danger)" onclick="showDelModal('${esc(p.clinic_hn)}','${esc(p.patient_name)}')" title="ลบ">🗑️</button></div></td></tr>`;
  }).join("");
}

function showEditModal(chn,hhn,name) {
  document.getElementById("editHN").value = chn||hhn;
  document.getElementById("editClinicHN").value = chn;
  document.getElementById("editHospHN").value = hhn;
  document.getElementById("editName").value = name;
  document.getElementById("modalTitle").textContent = "แก้ไข: "+(name||chn);
  document.getElementById("editModal").classList.add("show");
}

async function saveEdit() {
  const hn = document.getElementById("editHN").value;
  const newName = document.getElementById("editName").value.trim();
  const newChn = document.getElementById("editClinicHN").value.trim();
  const newHhn = document.getElementById("editHospHN").value.trim();
  if (!newName) { toast("กรุณากรอกชื่อ",false); return; }
  const btn = document.getElementById("saveEditBtn"); btn.disabled = true;
  try {
    const d = await api("/admin/api/v2/master-registry/update",{method:"POST",body:{hn,new_name:newName,new_clinic_hn:newChn,new_hospital_hn:newHhn}});
    if (!d.success) throw new Error(d.message||d.error);
    toast(d.message||"แก้ไขสำเร็จ"); closeModal("editModal"); await loadPatients();
  } catch(e) { toast(e.message,false); }
  btn.disabled = false;
}

function showAddModal() {
  document.getElementById("addClinicHN").value = "";
  document.getElementById("addHospHN").value = "";
  document.getElementById("addName").value = "";
  document.getElementById("addModal").classList.add("show");
}

async function saveAdd() {
  const chn = document.getElementById("addClinicHN").value.trim();
  const name = document.getElementById("addName").value.trim();
  const hhn = document.getElementById("addHospHN").value.trim();
  if (!chn) { toast("กรุณากรอก HN คลินิก",false); return; }
  if (!name) { toast("กรุณากรอกชื่อ",false); return; }
  try {
    const d = await api("/admin/api/v2/master-registry/register",{method:"POST",body:{clinic_hn:chn,hospital_hn:hhn,patient_name:name}});
    if (!d.success) throw new Error(d.message||d.error);
    toast(d.message||"เพิ่มสำเร็จ"); closeModal("addModal"); await loadPatients();
  } catch(e) { toast(e.message,false); }
}

function showDelModal(chn,name) {
  document.getElementById("delHN").value = chn;
  document.getElementById("delMsg").textContent = "ต้องการลบ "+(name||chn)+" (HN: "+chn+") จริงหรือไม่?";
  document.getElementById("delModal").classList.add("show");
}

async function confirmDelete() {
  const chn = document.getElementById("delHN").value;
  try {
    const d = await api("/admin/api/v2/master-registry/delete",{method:"POST",body:{clinic_hn:chn}});
    if (!d.success) throw new Error(d.message||d.error);
    toast("ลบสำเร็จ"); closeModal("delModal"); await loadPatients();
  } catch(e) { toast(e.message,false); }
}

loadPatients();
</script>
</body>
</html>'''


@app.route("/admin/api/v2/master-registry/debug-gcs", methods=["GET"])
def api_master_registry_debug_gcs():
    """ทดสอบ GCS write โดยตรง — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    auth = request.headers.get("X-Admin-Secret", "") or request.args.get("secret", "")
    if not admin_secret or auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    results = {}
    # 1) ตรวจ storage backend
    results["storage_backend"] = STORAGE_BACKEND
    results["has_storage_bucket"] = hasattr(storage, '_bucket') if storage else False

    # 2) ตรวจ master_registry._storage
    results["registry_has_storage"] = master_registry._storage is not None
    results["registry_storage_type"] = type(master_registry._storage).__name__ if master_registry._storage else None
    # ใช้ count() ซึ่ง force-reload จาก GCS (ไม่ใช่แค่อ่าน raw state)
    results["registry_patient_count"] = master_registry.count()
    results["registry_loaded"] = master_registry._loaded

    # 3) ทดสอบ write test blob (ใช้ unique name + if_generation_match=0
    #    เพื่อไม่ต้องมี delete permission)
    if master_registry._storage:
        try:
            _ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            test_blob = master_registry._storage.blob(
                f"_patient_master/_write_test_{_ts}.txt"
            )
            test_blob.upload_from_string(
                "write_test_ok", content_type="text/plain",
                if_generation_match=0,
            )
            results["write_test"] = "OK"
        except Exception as e:
            err_name = type(e).__name__
            # 412 = object exists (ไม่น่าเกิดเพราะใช้ unique name)
            if getattr(e, "code", None) == 412:
                results["write_test"] = "OK (object existed)"
            else:
                results["write_test"] = f"FAILED: {err_name}"

    # 4) ทดสอบ write registry path
    if master_registry._storage:
        try:
            reg_blob = master_registry._storage.blob(master_registry.GCS_FILE)
            results["registry_blob_exists"] = reg_blob.exists()
            # ลอง download เพื่อดูว่า read ได้
            try:
                content = reg_blob.download_as_text()
                results["registry_read"] = f"OK ({len(content)} bytes)"
            except Exception as e:
                results["registry_read"] = f"FAILED: {type(e).__name__}: {e}"
        except Exception as e:
            results["registry_blob_check"] = f"FAILED: {type(e).__name__}: {e}"

    return jsonify(results), 200


logger.info("Master Patient Registry API: endpoints registered")


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
            # formats= จำกัดตัวถอดรหัสที่ Pillow จะเรียก — ดู SAFE_IMAGE_FORMATS
            img = Image.open(tmp.name, formats=SAFE_IMAGE_FORMATS)
            # Apply same preprocessing as extract_text_with_ocr
            gray = img.convert("L")
            img.close()
            w, h = gray.size
            if w < 2000:
                sf = 2000 / w
                gray = gray.resize((int(w * sf), int(h * sf)), Image.Resampling.LANCZOS)
            sharpened = gray.filter(ImageFilter.SHARPEN)
            processed = ImageOps.autocontrast(sharpened, cutoff=2)
            sharpened.close()
            gray.close()
            text = pytesseract.image_to_string(
                processed, lang="tha+eng", config="--psm 3",
            ).strip()
            processed.close()
            return text


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
    """ตรวจสอบว่า user hash อยู่ใน Allowlist.

    ⚠️ นี่คือสิทธิ์ "เข้าถึงระบบ" เท่านั้น ไม่ใช่สิทธิ์ทางคลินิก
    ใน allowlist นี้มีได้ทั้งเจ้าหน้าที่และผู้ป่วย — ห้ามใช้ตัดสินว่า
    ผู้ใช้เห็นผลวิเคราะห์ที่ยังไม่ผ่านแพทย์ได้หรือไม่ (ดู CS-5)
    """
    if not AUTHORIZED_USERS:
        return False  # ถ้าไม่ตั้งค่า = ปิดระบบค้นหา
    return user_hash in AUTHORIZED_USERS


def is_physician_user(user_hash: str) -> bool:
    """แพทย์ที่ได้รับอนุมัติให้ตรวจสอบผลวิเคราะห์ทางคลินิก"""
    return bool(user_hash) and user_hash in PHYSICIAN_USERS


def is_admin_user(user_hash: str) -> bool:
    """ผู้ดูแลระบบ"""
    return bool(user_hash) and user_hash in ADMIN_USERS


def can_view_unapproved_clinical(user_hash: str) -> bool:
    """[CS-5] ใครเห็นผลวิเคราะห์ที่ยังไม่ผ่านการตรวจสอบของแพทย์ได้

    เฉพาะแพทย์และผู้ดูแลระบบ เพราะ "ต้องเห็นเพื่อตรวจสอบ" ถ้าซ่อนจาก
    แพทย์ด้วย กระบวนการอนุมัติก็ทำงานไม่ได้

    ผู้ใช้อื่นใน AUTHORIZED_USERS (เจ้าหน้าที่/ผู้ป่วย) ต้องไม่เห็น
    เนื้อหาข้อสรุปทางคลินิกจนกว่าแพทย์จะอนุมัติ

    เดิมระบบส่งเนื้อหาเต็มให้ทุกคนใน AUTHORIZED_USERS โดยติดป้ายกำกับ
    ไว้เฉย ๆ ซึ่งแปลว่าการกันไม่ให้ผู้ป่วยเห็นขึ้นกับการตั้งค่า env
    ไม่ใช่การบังคับใช้ในโค้ด — การเพิ่มผู้ป่วยเข้า allowlist เพียงคนเดียว
    ก็ทำให้เห็นผลวิเคราะห์ที่ยังไม่ผ่านแพทย์ทันที
    """
    return is_physician_user(user_hash) or is_admin_user(user_hash)


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


def _update_clinical_on_approve(
    storage_inst, doc_id: str, user_hash: str,
    report_version: int | None,
):
    """อัพเดท clinical analysis หลัง physician approve.

    ตั้ง PHYSICIAN_VERIFIED เฉพาะเมื่อ source_report_version ตรงกับ
    report version ปัจจุบัน — ถ้า report ถูกแก้ไขหลังวิเคราะห์ ให้
    ตั้ง STALE_AFTER_CORRECTION แทน.
    """
    try:
        ca_path = f"{CLINICAL_ANALYSIS_PREFIX}{doc_id}.json"
        ca_data = storage_inst.get(ca_path)
        if not ca_data:
            return
        ca_dict = json.loads(ca_data)
        source_ver = ca_dict.get("source_report_version")
        # ถ้า report version เปลี่ยนไปจากตอนวิเคราะห์ → stale
        if (source_ver is not None and report_version is not None
                and source_ver < report_version):
            ca_dict["verification_status"] = "STALE_AFTER_CORRECTION"
            ca_dict["stale_reason"] = (
                f"report version changed: {source_ver} → {report_version}"
            )
        else:
            ca_dict["verification_status"] = "PHYSICIAN_VERIFIED"
        ca_dict["verified_by"] = user_hash[:8]
        ca_dict["verified_at"] = datetime.now().isoformat(
            timespec="seconds"
        )
        storage_inst.put(
            ca_path,
            json.dumps(ca_dict, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
    except Exception as e:
        logger.warning(
            f"Clinical analysis update failed: "
            f"doc={doc_id[:8]}, error={type(e).__name__}"
        )


def _invalidate_clinical_on_correction(
    storage_inst, doc_id: str, report_version: int,
):
    """Invalidate clinical analysis หลังแก้ไขค่า report.

    ตั้ง STALE_AFTER_CORRECTION เพื่อบังคับให้สร้างผลวิเคราะห์ใหม่
    หลังจากแพทย์แก้ไขค่า OCR
    """
    try:
        ca_path = f"{CLINICAL_ANALYSIS_PREFIX}{doc_id}.json"
        ca_data = storage_inst.get(ca_path)
        if not ca_data:
            return
        ca_dict = json.loads(ca_data)
        ca_dict["verification_status"] = "STALE_AFTER_CORRECTION"
        ca_dict["stale_reason"] = (
            f"report corrected to version {report_version}"
        )
        ca_dict["invalidated_at"] = datetime.now().isoformat(
            timespec="seconds"
        )
        storage_inst.put(
            ca_path,
            json.dumps(ca_dict, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
    except Exception as e:
        logger.warning(
            f"Clinical analysis invalidation failed: "
            f"doc={doc_id[:8]}, error={type(e).__name__}"
        )


def handle_search_command(
    text: str, user_hash: str, line_user_id: str = "",
) -> str | None:
    """ประมวลผลคำสั่งค้นหาเอกสาร — คืนข้อความตอบกลับ หรือ None ถ้าไม่ใช่คำสั่ง.

    line_user_id: LINE user ID ดิบ ใช้เฉพาะตอนลงทะเบียนสำเร็จ เพื่อเก็บ
        คู่ hash -> ID ไว้ส่งการแจ้งเตือน ไม่ถูกแสดง ไม่ใส่ log
    """
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

        # เก็บคู่ hash -> LINE user ID ไว้ใช้ส่งการแจ้งเตือนค่าวิกฤต
        #
        # ทำไมต้องเก็บ: ALERT_RECIPIENTS ต้องใช้ LINE user ID ดิบ แต่
        # คำสั่งนี้ตั้งใจไว้ว่า "ไม่แสดง LINE userId จริง" สองอย่างนี้
        # ขัดกัน และเป็นเหตุที่เพิ่มพยาบาลเป็นผู้รับไม่ได้เลย
        # การเก็บไว้ในระบบทำให้ผู้ดูแลเพิ่มผู้รับด้วย hash ได้ โดยที่
        # ไม่มีใครเห็น ID ดิบ และ ID ไม่ต้องเดินทางผ่านแชท
        #
        # ค่านี้ไม่ถูกแสดงที่ใด ไม่ใส่ log และไม่มี API ใดคืนออกไป
        _can_be_recipient = False
        if line_user_id and alert_service is not None:
            try:
                from alert_delivery import store_enrolled_line_id
                _can_be_recipient = store_enrolled_line_id(
                    storage, user_hash, line_user_id,
                )
            except Exception as _e:
                logger.warning(
                    "เก็บ LINE ID ของผู้ลงทะเบียนไม่สำเร็จ: %s",
                    type(_e).__name__,
                )

        salt_warning = ""
        if IS_PRODUCTION and not os.getenv("USER_ID_SALT", "").strip():
            salt_warning = (
                "\n\n⚠️ ยังไม่ได้ตั้ง USER_ID_SALT ใน Render\n"
                "ค่า hash นี้ใช้ default salt\n"
                "แนะนำ: ตั้ง USER_ID_SALT แล้วลงทะเบียนใหม่"
            )
        alert_note = ""
        if _can_be_recipient:
            alert_note = (
                "\n\n📣 ถ้าต้องการให้ได้รับแจ้งเตือนผลวิกฤต\n"
                "ส่งค่า Hash ข้างบนให้ผู้ดูแลระบบ\n"
                "ระบบเก็บ LINE ID ของคุณไว้เองแล้ว "
                "ไม่ต้องส่ง LINE ID ให้ใคร"
            )
        return (
            f"✅ ลงทะเบียนสำเร็จ\n\n"
            f"ค่า Hash สำหรับ AUTHORIZED_USERS:\n"
            f"{user_hash}\n\n"
            f"วิธีใช้: เพิ่มค่านี้ใน Render Dashboard\n"
            f"AUTHORIZED_USERS={user_hash}\n"
            f"(หลายคนคั่นด้วยเครื่องหมายจุลภาค)"
            f"{alert_note}{salt_warning}"
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
        CommandType.MONITORING_STATUS, CommandType.NEPHROLOGY_KPI,
        CommandType.MY_STATS,
        # Feature 6: Patient Assistant
        CommandType.APPOINTMENT, CommandType.VIEW_APPOINTMENTS,
        CommandType.CANCEL_APPOINTMENT, CommandType.VIEW_ESCALATIONS,
        # Feature: Quarterly Compare & HN
        CommandType.COMPARE_LAB, CommandType.COMPARE_RX,
        CommandType.REGISTER_HN, CommandType.MY_HN,
    }
    # Feature 6 commands that don't require authorization
    # (health advice and contact staff are open to all users)
    OPEN_COMMANDS = {
        CommandType.HEALTH_ADVICE, CommandType.CONTACT_STAFF,
    }
    # ── Feature 6: Open commands (ไม่ต้อง authorize) ──
    if parsed.command_type in OPEN_COMMANDS:
        try:
            from line_patient_assistant import (
                get_health_advice,
                create_escalation, format_escalation_confirmation,
            )
            if parsed.command_type == CommandType.HEALTH_ADVICE:
                return get_health_advice(parsed.args)
            if parsed.command_type == CommandType.CONTACT_STAFF:
                msg = parsed.args or "ต้องการติดต่อเจ้าหน้าที่"
                esc = create_escalation(storage, user_hash, msg)
                audit_logger.log("escalation_created", user_hash, {
                    "urgency": esc["urgency"],
                    "category": esc["category"],
                })
                # Notify admin users via push_message
                for admin_hash in ADMIN_USERS:
                    # Cannot push to hash — admin notification handled via dashboard
                    pass
                return format_escalation_confirmation(esc)
        except ImportError:
            logger.warning("Feature 6: line_patient_assistant module not available")
            if parsed.command_type == CommandType.HEALTH_ADVICE:
                return "ฟีเจอร์คำแนะนำสุขภาพยังไม่พร้อมใช้งาน"
            return "ฟีเจอร์ติดต่อเจ้าหน้าที่ยังไม่พร้อมใช้งาน"
        except Exception as e:
            logger.warning(f"Feature 6 error: {type(e).__name__}")
            return "ขออภัย ระบบขัดข้องชั่วคราว กรุณาลองใหม่"

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
                or text_stripped.startswith("ตรวจยา")
                or text_stripped in ("ตารางตรวจ", "กำหนดตรวจ", "แล็บถัดไป")
                or text_stripped in ("KPIไต", "kpiไต", "เป้าหมายไต", "dashboard")):
            audit_logger.log("unauthorized_access", user_hash, {
                "command": text_stripped[:20]
            })
            return "ขออภัย คุณไม่มีสิทธิ์ใช้งานระบบค้นหาเอกสาร\nกรุณาติดต่อผู้ดูแลระบบ"
        return None  # ไม่ใช่คำสั่งค้นหา

    # ── Feature 6: Protected commands (ต้อง authorize) ──
    if parsed.command_type in (
        CommandType.APPOINTMENT,
        CommandType.VIEW_APPOINTMENTS,
        CommandType.CANCEL_APPOINTMENT,
        CommandType.VIEW_ESCALATIONS,
    ):
        try:
            from line_patient_assistant import (
                parse_appointment_text, save_appointment,
                format_appointment_confirmation,
                list_appointments, format_appointments_list,
                cancel_appointment,
                list_pending_escalations,
            )
            if parsed.command_type == CommandType.APPOINTMENT:
                appt_data = parse_appointment_text(parsed.args)
                if not appt_data["date"]:
                    return (
                        "กรุณาระบุวันที่นัดหมาย เช่น:\n"
                        "  นัดหมาย ฟอกไต 25/12/69 09:00\n"
                        "  นัดหมาย ตรวจเลือด พรุ่งนี้ เช้า"
                    )
                appt = save_appointment(storage, user_hash, appt_data)
                audit_logger.log("appointment_created", user_hash, {
                    "date": appt.date,
                    "purpose": appt.purpose,
                })
                return format_appointment_confirmation(appt)

            if parsed.command_type == CommandType.VIEW_APPOINTMENTS:
                appointments = list_appointments(storage, user_hash)
                return format_appointments_list(appointments)

            if parsed.command_type == CommandType.CANCEL_APPOINTMENT:
                result = cancel_appointment(storage, user_hash, parsed.args)
                audit_logger.log("appointment_cancelled", user_hash, {
                    "id_prefix": parsed.args[:8],
                })
                return result

            if parsed.command_type == CommandType.VIEW_ESCALATIONS:
                # Admin only
                if user_hash not in ADMIN_USERS:
                    return "ขออภัย เฉพาะผู้ดูแลเท่านั้นที่ดูคำร้องได้"
                escalations = list_pending_escalations(storage)
                if not escalations:
                    return "📨 ไม่มีคำร้องที่รอดำเนินการ"
                lines = ["📨 คำร้องที่รอดำเนินการ\n"]
                for i, esc in enumerate(escalations, 1):
                    urgency_icon = "🔴" if esc["urgency"] == "urgent" else "🟡"
                    lines.append(
                        f"{i}. {urgency_icon} [{esc['category']}] "
                        f"ผู้ป่วย: {esc['owner_hash'][:8]}…\n"
                        f"   🕐 {esc['created_at'][:16]}"
                    )
                return "\n".join(lines)

        except ImportError:
            logger.warning("Feature 6: line_patient_assistant module not available")
            return "ฟีเจอร์นัดหมายยังไม่พร้อมใช้งาน"
        except Exception as e:
            logger.warning(f"Feature 6 error: {type(e).__name__}")
            return "ขออภัย ระบบขัดข้องชั่วคราว กรุณาลองใหม่"

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
                # ── On-demand re-extraction ──
                # ถ้า lab JSON ไม่มี แต่เอกสารเป็น lab และมี extracted_text
                # ให้ลอง extract ใหม่อัตโนมัติ (แก้ปัญหา silent failure)
                _reextracted = False
                _doc_cat = doc.get("category", "")
                _doc_text = doc.get("extracted_text", "")
                _doc_text_len = len(_doc_text.strip()) if _doc_text else 0
                logger.info(
                    f"On-demand re-extraction check: "
                    f"doc={full_doc_id[:8]}, "
                    f"cat={_doc_cat}, "
                    f"text_len={_doc_text_len}"
                )
                if _doc_cat == "lab" and _doc_text and _doc_text_len >= 50:
                    try:
                        from lab_extractor import extract_lab_results_from_text
                        from lab_models import ExtractionMethod
                        _re_method = ExtractionMethod.REGEX_PDF_TEXT.value
                        _re_report = extract_lab_results_from_text(
                            _doc_text, full_doc_id, _re_method
                        )
                        if _re_report.results:
                            _re_json = _re_report.to_json()
                            # ── FIX Phase 14.7: ตั้ง lab_data ก่อน GCS write ──
                            # เพื่อให้ผู้ใช้เห็นผลแล็บแม้ GCS write ล้มเหลว
                            lab_data = _re_json.encode("utf-8")
                            _reextracted = True
                            _persistence_ok = False
                            # GCS write เป็น best-effort cache
                            try:
                                storage.put(
                                    lab_path,
                                    lab_data,
                                    content_type="application/json",
                                    create_only=True,
                                )
                                _persistence_ok = True
                            except Exception as gcs_err:
                                # GCS write ล้มเหลว — ผู้ใช้ยังเห็นผล
                                # แต่ไม่รายงานว่าจัดเก็บถาวรสำเร็จ
                                logger.warning(
                                    f"On-demand lab GCS cache write "
                                    f"ล้มเหลว: "
                                    f"{type(gcs_err).__name__} "
                                    f"(doc_id={full_doc_id[:8]})"
                                )
                                audit_logger.log(
                                    "lab_persistence_failed",
                                    user_hash,
                                    {
                                        "doc_id": full_doc_id[:8],
                                        "error_type": type(
                                            gcs_err
                                        ).__name__,
                                        "result_count": len(
                                            _re_report.results
                                        ),
                                    },
                                )
                            # ── แยกสถานะ extraction vs persistence ──
                            logger.info(
                                f"On-demand lab re-extraction สำเร็จ: "
                                f"doc={full_doc_id[:8]}, "
                                f"results={len(_re_report.results)}, "
                                f"persisted={_persistence_ok}"
                            )
                            audit_logger.log(
                                "lab_reextraction_success", user_hash,
                                {
                                    "doc_id": full_doc_id[:8],
                                    "result_count": len(
                                        _re_report.results
                                    ),
                                    "persisted": _persistence_ok,
                                },
                            )
                    except Exception as reext_err:
                        import traceback as _reext_tb_mod
                        _reext_tb = _reext_tb_mod.format_exc()
                        _reext_msg = str(reext_err)
                        # Sanitize: ตัด full doc_id, GCS path,
                        # signed URL ออกจาก error message + traceback
                        if full_doc_id and len(full_doc_id) > 8:
                            _reext_msg = _reext_msg.replace(
                                full_doc_id, full_doc_id[:8] + "…"
                            )
                            _reext_tb = _reext_tb.replace(
                                full_doc_id, full_doc_id[:8] + "…"
                            )
                        # ตัด signed URL ที่อาจมี token
                        import re as _re_mod
                        _reext_tb = _re_mod.sub(
                            r'https://storage\.googleapis\.com/[^\s"\']+',
                            "[GCS_URL_REDACTED]",
                            _reext_tb,
                        )
                        _reext_msg = _re_mod.sub(
                            r'https://storage\.googleapis\.com/[^\s"\']+',
                            "[GCS_URL_REDACTED]",
                            _reext_msg,
                        )
                        logger.warning(
                            f"On-demand lab re-extraction ล้มเหลว: "
                            f"{type(reext_err).__name__}: {_reext_msg} "
                            f"(doc_id={full_doc_id[:8]})"
                        )
                        logger.warning(
                            f"On-demand re-extraction traceback "
                            f"(doc_id={full_doc_id[:8]}):\n{_reext_tb}"
                        )
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

        # ── Phase 2A: ต้องเป็น Admin หรือ Physician ──
        is_physician = user_hash in PHYSICIAN_USERS
        is_admin = user_hash in ADMIN_USERS
        if not is_admin and not is_physician:
            audit_logger.log("lab_approve_unauthorized", user_hash, {
                "doc_id": doc_id_query[:8],
            })
            if enhanced_audit:
                try:
                    enhanced_audit.log(
                        "physician_approval_denied",
                        user_hash,
                        {"doc_id": doc_id_query[:8]},
                    )
                except Exception:
                    pass
            return (
                "ขออภัย เฉพาะแพทย์หรือผู้ดูแลระบบ"
                "เท่านั้นที่สามารถยืนยันผลแล็บได้"
            )

        # ตรวจสอบเอกสาร (admin/physician เห็นทุกเอกสาร)
        doc = doc_index.get(doc_id_query)
        if doc is None:
            return "ไม่พบเอกสารที่ตรงกับรหัสนี้"

        full_doc_id = doc.get("document_id", doc_id_query)
        lab_path = f"{LAB_RESULTS_PREFIX}{full_doc_id}.json"
        try:
            # ── Phase 2A: ใช้ PhysicianApprovalService ถ้ามี ──
            if physician_service is not None and is_physician:
                try:
                    # โหลด report เพื่อหา current version
                    _pre_approve_enhanced = (
                        physician_service
                        ._load_enhanced_report(lab_path)
                    )
                    _expected_ver = (
                        _pre_approve_enhanced.version
                        if _pre_approve_enhanced else 1
                    )
                    result = physician_service.approve_report(
                        lab_path=lab_path,
                        user_hash=user_hash,
                        expected_version=_expected_ver,
                        notes="Approved via LINE command",
                    )
                    if result.get("status") == "success":
                        audit_logger.log("lab_approve", user_hash, {
                            "doc_id": full_doc_id[:8],
                            "via": "physician_service",
                        })
                        if enhanced_audit:
                            try:
                                enhanced_audit.log(
                                    "physician_approval",
                                    user_hash,
                                    {
                                        "doc_id": full_doc_id[:8],
                                        "version": result.get("version"),
                                    },
                                )
                            except Exception:
                                pass
                        # อัพเดท clinical analysis → PHYSICIAN_VERIFIED
                        _update_clinical_on_approve(
                            storage, full_doc_id,
                            user_hash, result.get("version"),
                        )
                        return (
                            f"✅ แพทย์ยืนยันผลแล็บ "
                            f"{full_doc_id[:8]} สำเร็จ\n"
                            f"สถานะ: {result.get('new_status')}\n"
                            f"version: {result.get('version')}"
                        )
                    else:
                        return (
                            f"ไม่สามารถยืนยันได้: "
                            f"{result.get('error', 'unknown')}"
                        )
                except DuplicateApprovalError:
                    return (
                        f"ผลแล็บ {full_doc_id[:8]} "
                        f"ได้รับการยืนยันแล้ว"
                    )
                except StaleVersionError:
                    return (
                        "มีการแก้ไขผลแล็บพร้อมกัน "
                        "กรุณาลองใหม่อีกครั้ง"
                    )
                except InvalidStateTransitionError:
                    return (
                        f"ผลแล็บ {full_doc_id[:8]} "
                        f"อยู่ในสถานะที่ไม่สามารถยืนยันได้"
                    )

            # ── Fallback: Admin approval (legacy) ──
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

            # อัพเดท clinical analysis verification_status
            # legacy admin path: version = 1 (ไม่มี EnhancedLabReport)
            _update_clinical_on_approve(
                storage, full_doc_id, user_hash,
                report_version=1,
            )

            return (
                f"✅ ยืนยันผลแล็บ {full_doc_id[:8]} สำเร็จ\n"
                f"จำนวนผลตรวจ: {len(lab_report.results)} รายการ\n"
                f"สถานะ: approved"
            )
        except Exception as e:
            logger.warning(
                f"Lab approve error: {type(e).__name__} "
                f"(doc_id={doc_id_query[:8]})"
            )
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

            # ── Stale-check: ถ้า report ถูกแก้ไขหลังวิเคราะห์ ──
            verification_status = clinical_dict.get(
                "verification_status", ""
            )
            source_ver = clinical_dict.get("source_report_version")
            stale_warning = ""
            if source_ver is not None:
                # โหลด report ปัจจุบันเพื่อเทียบ version
                _check_lab_path = (
                    f"{LAB_RESULTS_PREFIX}{full_doc_id}.json"
                )
                try:
                    _check_lab_data = storage.get(_check_lab_path)
                    if _check_lab_data:
                        _check_parsed = json.loads(_check_lab_data)
                        current_ver = _check_parsed.get("version", 1)
                        if current_ver > source_ver:
                            stale_warning = (
                                "⚠️ ผลวิเคราะห์นี้อ้างอิงผลแล็บ "
                                f"version {source_ver} "
                                f"แต่ผลแล็บปัจจุบันเป็น "
                                f"version {current_ver}\n"
                                "ผลวิเคราะห์อาจไม่ตรงกับค่าล่าสุด\n\n"
                            )
                            # ป้องกัน: ถ้ายัง PHYSICIAN_VERIFIED
                            # แต่ report เปลี่ยนแล้ว → แสดงเป็น STALE
                            if verification_status == "PHYSICIAN_VERIFIED":
                                verification_status = "STALE_AFTER_CORRECTION"
                except Exception:
                    pass  # ถ้าอ่าน report ไม่ได้ ไม่ block การแสดงผล

            audit_logger.log("clinical_analysis_view", user_hash, {
                "doc_id": full_doc_id[:8],
                "finding_count": clinical_dict.get("finding_count", 0),
                "ckd_stage": clinical_dict.get("ckd_stage", "UNKNOWN"),
                "stale": bool(stale_warning),
            })

            # ── [CS-5] บังคับ approval gate ก่อนเปิดเผยข้อสรุปทางคลินิก ──
            # ผลวิเคราะห์ที่ยังไม่ผ่านแพทย์ หรือที่ล้าสมัยเพราะผลแล็บถูก
            # แก้ไขภายหลัง ต้อง "ไม่ถูกส่งเนื้อหา" ให้ผู้ที่ไม่ใช่แพทย์
            # ไม่ใช่แค่ติดป้ายกำกับแล้วส่งเนื้อหาเต็มเหมือนเดิม
            _needs_review = verification_status in (
                "PENDING_VERIFICATION", "STALE_AFTER_CORRECTION",
            )
            if _needs_review and not can_view_unapproved_clinical(user_hash):
                audit_logger.log(
                    "clinical_analysis_withheld", user_hash, {
                        "doc_id": full_doc_id[:8],
                        "status": verification_status,
                    },
                )
                if verification_status == "STALE_AFTER_CORRECTION":
                    return (
                        "⚠️ ผลวิเคราะห์ของเอกสารนี้ล้าสมัย\n"
                        "ผลแล็บถูกแก้ไขหลังจากวิเคราะห์ จึงยังไม่แสดง\n\n"
                        "ระบบจะแสดงให้เมื่อแพทย์ตรวจสอบผลที่แก้ไขแล้ว"
                    )
                return (
                    "⏳ ผลวิเคราะห์ของเอกสารนี้ยังไม่ผ่านการตรวจสอบ"
                    "โดยแพทย์\n\n"
                    "เพื่อความปลอดภัย ระบบจะยังไม่แสดงข้อสรุป "
                    "จนกว่าแพทย์จะตรวจสอบและยืนยัน\n\n"
                    f"รหัสเอกสาร: {full_doc_id[:8]}"
                )

            # ── Verification status banner (สำหรับผู้มีสิทธิ์ตรวจสอบ) ──
            status_banner = ""
            if verification_status == "PENDING_VERIFICATION":
                status_banner = (
                    "⏳ ผลวิเคราะห์ยังไม่ผ่านการตรวจสอบโดยแพทย์\n"
                    "(แสดงเพื่อการตรวจสอบ — ห้ามส่งต่อให้ผู้ป่วย"
                    "ก่อนยืนยัน)\n\n"
                )
            elif verification_status == "STALE_AFTER_CORRECTION":
                status_banner = (
                    "⚠️ ผลวิเคราะห์นี้ล้าสมัย — "
                    "ผลแล็บถูกแก้ไขหลังจากวิเคราะห์\n"
                    "กรุณารอผลวิเคราะห์ใหม่หลัง"
                    "แพทย์ยืนยันผลแล็บที่แก้ไข\n\n"
                )

            # แสดงข้อความสรุปที่สร้างไว้
            summary = clinical_dict.get("summary_text_th", "")
            if summary:
                return f"{stale_warning}{status_banner}{summary}"
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

            summary_text = analysis.summary_text_th()

            # สร้างกราฟ PNG (ถ้ามี matplotlib)
            try:
                chart_png = generate_trend_chart_png(analysis)
                if chart_png and user_hash:
                    import uuid as _uuid_mod
                    chart_id = _uuid_mod.uuid4().hex[:12]
                    chart_path = os.path.join("/tmp/ranong_raktai", f"chart_{chart_id}.png")
                    os.makedirs(os.path.dirname(chart_path), exist_ok=True)
                    with open(chart_path, "wb") as cf:
                        cf.write(chart_png)
                    token = create_chart_token(chart_id)
                    base_url = request.host_url.rstrip("/")
                    chart_url = f"{base_url}/admin/chart/{token}"

                    # ส่ง Flex Message พร้อมกราฟ
                    try:
                        push_url = "https://api.line.me/v2/bot/message/push"
                        push_headers = {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                        }
                        flex_msg = {
                            "to": "",  # NOTE: chart push ต้องใช้ LINE user ID — ยังไม่มีใน search command
                            "messages": [{
                                "type": "flex",
                                "altText": "📊 กราฟแนวโน้มผลตรวจ Lab",
                                "contents": {
                                    "type": "bubble",
                                    "size": "giga",
                                    "header": {
                                        "type": "box",
                                        "layout": "vertical",
                                        "contents": [{
                                            "type": "text",
                                            "text": "📊 กราฟแนวโน้มผลตรวจ Lab",
                                            "weight": "bold",
                                            "size": "lg",
                                            "color": "#38bdf8",
                                        }],
                                        "backgroundColor": "#0f172a",
                                        "paddingAll": "15px",
                                    },
                                    "hero": {
                                        "type": "image",
                                        "url": chart_url,
                                        "size": "full",
                                        "aspectRatio": "20:13",
                                        "aspectMode": "fit",
                                        "backgroundColor": "#0f172a",
                                    },
                                    "footer": {
                                        "type": "box",
                                        "layout": "vertical",
                                        "contents": [{
                                            "type": "text",
                                            "text": "กราฟหมดอายุใน 5 นาที",
                                            "size": "xs",
                                            "color": "#94a3b8",
                                            "align": "center",
                                        }],
                                        "backgroundColor": "#0f172a",
                                        "paddingAll": "10px",
                                    },
                                },
                            }],
                        }
                        if LINE_CHANNEL_ACCESS_TOKEN:
                            http_requests.post(push_url, headers=push_headers,
                                               json=flex_msg, timeout=10)
                            logger.info(f"Chart sent via LINE Flex for trend analysis")
                    except Exception as push_err:
                        logger.warning(f"Chart push failed: {type(push_err).__name__}")
            except Exception as chart_err:
                logger.warning(f"Chart generation skipped: {type(chart_err).__name__}")

            return summary_text

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

    # ── เปรียบเทียบผลแล็บ (ล่าสุด vs ก่อนหน้า) ──
    if parsed.command_type == CommandType.COMPARE_LAB:
        try:
            result = compare_latest_labs(storage, doc_index, user_hash)
            if "error" in result:
                return result["error"]
            audit_logger.log("compare_lab", user_hash, {
                "old_doc": result.get("old_doc_id", "")[:8],
                "new_doc": result.get("new_doc_id", "")[:8],
            })
            return result.get("text_th", "ไม่สามารถเปรียบเทียบได้")
        except Exception as e:
            logger.warning(f"Compare lab error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถเปรียบเทียบผลแล็บได้ในขณะนี้"

    # ── เปรียบเทียบใบยา ──
    if parsed.command_type == CommandType.COMPARE_RX:
        try:
            result = compare_latest_rx(storage, doc_index, user_hash)
            if "error" in result:
                return result["error"]
            audit_logger.log("compare_rx", user_hash, {
                "old_doc": result.get("old_doc_id", "")[:8],
                "new_doc": result.get("new_doc_id", "")[:8],
            })
            return result.get("text_th", "ไม่สามารถเปรียบเทียบได้")
        except Exception as e:
            logger.warning(f"Compare rx error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถเปรียบเทียบใบยาได้ในขณะนี้"

    # ── ลงทะเบียน HN ──
    if parsed.command_type == CommandType.REGISTER_HN:
        hn_input = parsed.args.strip()
        if not hn_input:
            return (
                "📋 ลงทะเบียน HN\n\n"
                "รูปแบบ: ลงทะเบียนHN [HNคลินิก] [HNรพ.]\n\n"
                "ตัวอย่าง:\n"
                "• ลงทะเบียนHN 1234 56789\n"
                "• ลงทะเบียนHN 1234\n"
                "• HN 1234 56789"
            )
        try:
            # parse HN input: อาจเป็น "1234 56789" หรือ "1234"
            parts = hn_input.split()
            clinic_hn = parts[0] if len(parts) >= 1 else ""
            hospital_hn = parts[1] if len(parts) >= 2 else ""

            success, msg = hn_registry.register(
                owner_hash=user_hash,
                clinic_hn=clinic_hn,
                hospital_hn=hospital_hn,
                registered_by="patient",
            )
            audit_logger.log("hn_register", user_hash, {
                "success": success,
                "has_clinic_hn": bool(clinic_hn),
                "has_hospital_hn": bool(hospital_hn),
            })
            # บัญชีเจ้าหน้าที่ส่งเอกสารแทนผู้ป่วยหลายคน — แจ้งให้ชัดว่า
            # ระบบจะไม่จัดเอกสารที่ส่งจากบัญชีนี้เข้าแฟ้ม HN นี้
            if success and is_authorized_user(user_hash):
                msg = (
                    f"{msg}\n\n"
                    "⚠️ บัญชีนี้เป็นบัญชีผู้ดูแล\n"
                    "เอกสารที่ส่งจากบัญชีนี้จะไม่ถูกจัดเข้าแฟ้ม HN นี้ "
                    "โดยอัตโนมัติ (ป้องกันเอกสารของผู้ป่วยหลายคน"
                    "ไปรวมแฟ้มเดียว) ระบบจะใช้ HN ที่อ่านได้จากเอกสารแทน"
                )
            return msg
        except Exception as e:
            logger.warning(f"HN register error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถลงทะเบียน HN ได้ในขณะนี้"

    # ── ดูข้อมูล HN ของฉัน ──
    if parsed.command_type == CommandType.MY_HN:
        try:
            patient = hn_registry.get_by_owner(user_hash)
            if patient:
                lines = [
                    "🏥 ข้อมูล HN ของคุณ",
                    f"📋 รหัส: {patient.hn_pair}",
                ]
                if patient.clinic_hn:
                    lines.append(f"  • HN คลินิก: {patient.clinic_hn}")
                if patient.hospital_hn:
                    lines.append(f"  • HN โรงพยาบาล: {patient.hospital_hn}")
                lines.append(f"📅 ลงทะเบียน: {patient.registered_at[:10]}")
                return "\n".join(lines)
            else:
                return (
                    "ยังไม่มี HN ในระบบ\n\n"
                    "พิมพ์ 'ลงทะเบียนHN [HNคลินิก] [HNรพ.]'\n"
                    "ตัวอย่าง: ลงทะเบียนHN 1234 56789"
                )
        except Exception as e:
            logger.warning(f"MY_HN error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถดึงข้อมูล HN ได้ในขณะนี้"

    # ── ตารางตรวจ — Monitoring Schedule ──
    if parsed.command_type == CommandType.MONITORING_STATUS or text_stripped in (
        "ตารางตรวจ", "กำหนดตรวจ", "แล็บถัดไป"
    ):
        if check_monitoring_status is None:
            return "ขออภัย ฟีเจอร์ตารางตรวจยังไม่พร้อมใช้งาน"

        try:
            # ดึงผลแล็บของผู้ใช้
            user_docs = doc_index.list_by_owner(user_hash, limit=200)
            lab_reports = []
            for doc_meta in (user_docs or []):
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

            report = check_monitoring_status(
                lab_reports=lab_reports,
                owner_hash=user_hash,
            )

            audit_logger.log("monitoring_status_view", user_hash, {
                "total_overdue": report.total_overdue,
                "total_missing": report.total_missing,
                "lab_reports_checked": len(lab_reports),
            })

            return monitoring_report_text_th(report)

        except Exception as e:
            logger.warning(f"Monitoring status error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถตรวจสอบตารางตรวจได้ในขณะนี้"

    # ── KPI ไต — Nephrology KPI Dashboard ──
    if parsed.command_type == CommandType.NEPHROLOGY_KPI or text_stripped in (
        "KPIไต", "kpiไต", "เป้าหมายไต", "dashboard"
    ):
        if generate_nephrology_dashboard is None:
            return "ขออภัย ฟีเจอร์ KPI ไตยังไม่พร้อมใช้งาน"

        try:
            # ดึงผลแล็บของผู้ใช้
            user_docs = doc_index.list_by_owner(user_hash, limit=200)
            lab_reports = []
            for doc_meta in (user_docs or []):
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

            # ดึง monitoring report ถ้ามี
            monitoring_report = None
            if check_monitoring_status is not None:
                try:
                    monitoring_report = check_monitoring_status(
                        lab_reports=lab_reports,
                        owner_hash=user_hash,
                    )
                except Exception:
                    pass

            # ดึงข้อมูล approval backlog (ถ้ามี physician service)
            approval_data = None
            if physician_service is not None:
                try:
                    pending = physician_service.get_pending_reports()
                    approval_data = {
                        "total_pending": len(pending) if pending else 0,
                    }
                except Exception:
                    pass

            # ดึงข้อมูล alert performance (ถ้ามี alert service)
            alert_data = None
            if alert_service is not None:
                try:
                    active = alert_service.get_active_alerts()
                    alert_data = {
                        "total_active": len(active) if active else 0,
                    }
                except Exception:
                    pass

            dashboard = generate_nephrology_dashboard(
                lab_reports=lab_reports,
                owner_hash=user_hash,
                medication_review_data=None,
                monitoring_report=monitoring_report,
                approval_data=approval_data,
                alert_data=alert_data,
            )

            audit_logger.log("nephrology_kpi_view", user_hash, {
                "domain_count": len(dashboard.domain_kpis),
                "missing_investigations": dashboard.missing_investigation_count,
                "lab_reports_checked": len(lab_reports),
            })

            return dashboard_text_th(dashboard)

        except Exception as e:
            logger.warning(f"Nephrology KPI error: {type(e).__name__}")
            return "ขออภัย ไม่สามารถสร้างรายงาน KPI ไตได้ในขณะนี้"

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


# owner_hash ที่ถูกต้องคือผลลัพธ์ของ hash_user_id() เสมอ
# = SHA-256 ตัด 32 ตัวแรก → hex ตัวพิมพ์เล็ก 32 ตัว
_OWNER_HASH_RE = re.compile(r"^[0-9a-f]{32}$")


def is_valid_owner_hash(value: str) -> bool:
    """ตรวจรูปแบบ owner_hash ก่อนนำไปใช้ที่ขอบระบบ

    [SECURITY_GAP_ANALYSIS §3.1] ไม่มีจุดใดในระบบยืนยันว่า owner_hash
    ที่รับเข้ามามีรูปแบบถูกต้อง ทำให้ค่าที่ผิดรูป (เช่น LINE userId ดิบ
    หรือพิมพ์ตกหล่น) ถูกนำไปใช้เงียบ ๆ

    ความเสียหายที่เกิดได้จริง:
      - /admin/assign-owner ผูก owner ให้เอกสารแบบถาวร และ "ไม่ยอม
        ผูกซ้ำ" ถ้าใส่ค่าผิดรูปเพียงครั้งเดียว เอกสารนั้นจะเข้าถึงไม่ได้
        ตลอดไปโดยแก้ไม่ได้ผ่าน endpoint เดิม
      - ข้อบกพร่องของ LIFF (SEC-1) ที่ client ส่ง userId ดิบ จะถูกจับได้
        ทันทีตั้งแต่วันแรกถ้ามีตัวตรวจนี้ แทนที่จะเงียบจนมา audit เจอ
    """
    return bool(value) and bool(_OWNER_HASH_RE.match(value))


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


def extract_text_with_ocr(filepath: str, timeout_sec: int = 60) -> str:
    """ดึงข้อความด้วย Tesseract OCR (tha+eng).

    การจัดการหน่วยความจำ:
      - render ที่ OCR_DPI (ค่าเริ่มต้น 200) แทน 300 — ลดหน่วยความจำราว 56%
        (A4 ที่ 300 DPI ≈ 26 MB ต่อ buffer, ที่ 200 DPI ≈ 11.6 MB)
      - คืนหน่วยความจำของ pixmap และ image ทันทีหลังจบแต่ละหน้า
        แทนที่จะสะสมไว้จนจบลูป → พีคเท่ากับหนึ่งหน้า ไม่ใช่ทุกหน้ารวมกัน
      - จำกัดงาน OCR พร้อมกันต่อ worker ด้วย semaphore — ป้องกัน OOM
        บน Render instance 512 MB ที่รัน 2 workers
      - timeout_sec: จำกัดเวลา Tesseract ต่อหน้า — ป้องกัน hang
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
                processed = None
                try:
                    # formats= จำกัดตัวถอดรหัส — ดู SAFE_IMAGE_FORMATS
                    img = Image.open(filepath, formats=SAFE_IMAGE_FORMATS)
                    orig_w, orig_h = img.size
                    logger.info(f"[OCR-PRE] Original: {orig_w}x{orig_h} mode={img.mode}")
                    _ocr_debug_store["image_original_size"] = f"{orig_w}x{orig_h}"
                    _ocr_debug_store["image_mode"] = img.mode

                    # 1. Convert to grayscale
                    gray = img.convert("L")
                    img.close()
                    img = None

                    # 2. Upscale — LINE photos are ~1024px, OCR needs ≥2000px
                    w, h = gray.size
                    if w < 2000:
                        sf = 2000 / w
                        gray = gray.resize(
                            (int(w * sf), int(h * sf)), Image.Resampling.LANCZOS
                        )
                        logger.info(f"[OCR-PRE] Upscaled → {gray.size[0]}x{gray.size[1]}")

                    _ocr_debug_store["processed_size"] = f"{gray.size[0]}x{gray.size[1]}"

                    # ── Multi-pass OCR: try different preprocessing + PSM ──
                    # แต่ละ pass ใช้ preprocessing ต่างกันเพื่อรองรับ
                    # ภาพถ่ายจาก LINE OA ที่มีคุณภาพหลากหลาย
                    ocr_timeout = min(timeout_sec, 20)
                    text = ""

                    # Pass 1: Sharpen + autocontrast + PSM 3 (auto segmentation)
                    # PSM 3 handles mixed layouts (headers + tables + text)
                    # better than PSM 6 for prescription/medical documents
                    try:
                        sharpened = gray.filter(ImageFilter.SHARPEN)
                        p1 = ImageOps.autocontrast(sharpened, cutoff=2)
                        sharpened.close()
                        text = pytesseract.image_to_string(
                            p1, lang="tha+eng",
                            timeout=ocr_timeout, config="--psm 3",
                        ).strip()
                        p1.close()
                        _ocr_debug_store["pass1"] = f"psm3-sharp-ac2: {len(text)} chars"
                        logger.info(f"[OCR] Pass1 PSM3+sharp: {len(text)} chars")
                    except Exception as e:
                        logger.warning(f"[OCR] Pass1 error: {e}")
                        _ocr_debug_store["pass1"] = f"ERROR: {e}"

                    # Pass 2: If pass 1 got < 15 chars, try binarization + PSM 6
                    # Otsu-like: compute mean, use as threshold
                    if len(text) < 15:
                        try:
                            # Enhance contrast first
                            enhanced = ImageEnhance.Contrast(gray).enhance(2.0)
                            sharpened2 = enhanced.filter(
                                ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)
                            )
                            enhanced.close()
                            # Compute adaptive threshold using PIL histogram
                            hist = sharpened2.histogram()
                            total_px = sum(hist)
                            cumsum = 0
                            threshold = 128
                            for i, count in enumerate(hist):
                                cumsum += i * count
                            mean_val = cumsum / max(total_px, 1)
                            threshold = int(mean_val * 0.85)
                            p2 = sharpened2.point(lambda x: 255 if x > threshold else 0)
                            sharpened2.close()
                            text2 = pytesseract.image_to_string(
                                p2, lang="tha+eng",
                                timeout=ocr_timeout, config="--psm 6",
                            ).strip()
                            p2.close()
                            _ocr_debug_store["pass2"] = f"psm6-binary-t{threshold}: {len(text2)} chars"
                            logger.info(f"[OCR] Pass2 PSM6+binary(t={threshold}): {len(text2)} chars")
                            if len(text2) > len(text):
                                text = text2
                        except Exception as e:
                            logger.warning(f"[OCR] Pass2 error: {e}")
                            _ocr_debug_store["pass2"] = f"ERROR: {e}"

                    # Pass 3: If still < 15 chars, try UnsharpMask + autocontrast + PSM 4
                    # PSM 4 = single column of text (many prescriptions are columnar)
                    if len(text) < 15:
                        try:
                            p3_sharp = gray.filter(
                                ImageFilter.UnsharpMask(radius=3, percent=200, threshold=2)
                            )
                            p3 = ImageOps.autocontrast(p3_sharp, cutoff=5)
                            p3_sharp.close()
                            text3 = pytesseract.image_to_string(
                                p3, lang="tha+eng",
                                timeout=ocr_timeout, config="--psm 4",
                            ).strip()
                            p3.close()
                            _ocr_debug_store["pass3"] = f"psm4-unsharp-ac5: {len(text3)} chars"
                            logger.info(f"[OCR] Pass3 PSM4+unsharp: {len(text3)} chars")
                            if len(text3) > len(text):
                                text = text3
                        except Exception as e:
                            logger.warning(f"[OCR] Pass3 error: {e}")
                            _ocr_debug_store["pass3"] = f"ERROR: {e}"

                    # Pass 4: English-only OCR — ใบยามีชื่อยาภาษาอังกฤษ
                    # เช่น losartan, amlodipine, mg, tab
                    # Tesseract eng-only มักอ่านได้แม้ tha+eng จะล้มเหลว
                    if len(text) < 15:
                        try:
                            p4_sharp = gray.filter(ImageFilter.SHARPEN)
                            p4 = ImageOps.autocontrast(p4_sharp, cutoff=2)
                            p4_sharp.close()
                            text4 = pytesseract.image_to_string(
                                p4, lang="eng",
                                timeout=ocr_timeout, config="--psm 3",
                            ).strip()
                            p4.close()
                            _ocr_debug_store["pass4"] = f"psm3-eng-only: {len(text4)} chars"
                            logger.info(f"[OCR] Pass4 ENG-only: {len(text4)} chars")
                            if len(text4) > len(text):
                                text = text4
                        except Exception as e:
                            logger.warning(f"[OCR] Pass4 error: {e}")
                            _ocr_debug_store["pass4"] = f"ERROR: {e}"

                    # Pass 5: English-only with binarization + PSM 6
                    if len(text) < 15:
                        try:
                            enhanced5 = ImageEnhance.Contrast(gray).enhance(2.5)
                            p5 = enhanced5.point(lambda x: 255 if x > 140 else 0)
                            enhanced5.close()
                            text5 = pytesseract.image_to_string(
                                p5, lang="eng",
                                timeout=ocr_timeout, config="--psm 6",
                            ).strip()
                            p5.close()
                            _ocr_debug_store["pass5"] = f"psm6-eng-binary: {len(text5)} chars"
                            logger.info(f"[OCR] Pass5 ENG+binary: {len(text5)} chars")
                            if len(text5) > len(text):
                                text = text5
                        except Exception as e:
                            logger.warning(f"[OCR] Pass5 error: {e}")
                            _ocr_debug_store["pass5"] = f"ERROR: {e}"

                    # Pass 6: ALWAYS run English-only OCR and COMBINE with existing text
                    # ใบฟอกเลือด/ใบยามีคำภาษาอังกฤษสำคัญ (Hemodialysis, Heparin, Dialyzer)
                    # ที่ tha+eng OCR อาจอ่านผิด แต่ eng-only อ่านได้ชัด
                    # รวมข้อความ eng เข้ากับ text ที่มีอยู่เพื่อเพิ่มโอกาส keyword match
                    try:
                        p6_sharp = gray.filter(ImageFilter.SHARPEN)
                        p6 = ImageOps.autocontrast(p6_sharp, cutoff=2)
                        p6_sharp.close()
                        text6 = pytesseract.image_to_string(
                            p6, lang="eng",
                            timeout=ocr_timeout, config="--psm 3",
                        ).strip()
                        p6.close()
                        _ocr_debug_store["pass6"] = f"psm3-eng-always: {len(text6)} chars"
                        logger.info(f"[OCR] Pass6 ENG-always: {len(text6)} chars")
                        if text6 and len(text6) >= 10:
                            # รวม eng text เข้ากับ text ที่มีอยู่ (ไม่ใช่แทนที่)
                            # เพื่อให้ keyword matcher เห็นทั้งคำไทยและอังกฤษ
                            if len(text) > 0:
                                text = text + "\n" + text6
                            else:
                                text = text6
                            logger.info(f"[OCR] Combined thai+eng: {len(text)} chars")
                    except Exception as e:
                        logger.warning(f"[OCR] Pass6 error: {e}")
                        _ocr_debug_store["pass6"] = f"ERROR: {e}"

                    _ocr_debug_store["ocr_char_count"] = len(text)
                    _ocr_debug_store["preprocess"] = "multi-pass-6"
                    logger.info(f"[OCR] Final: {len(text)} chars")
                finally:
                    if processed is not None:
                        try:
                            processed.close()
                        except Exception:
                            pass
                    if img is not None:
                        try:
                            img.close()
                        except Exception:
                            pass
                    try:
                        gray.close()
                    except Exception:
                        pass
                logger.info(f"OCR ภาพ: ได้ {len(text)} ตัวอักษร")
                if text:
                    logger.info(f"[OCR-PRE] preview: {text[:200]}")
                return text

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
                            page_text = pytesseract.image_to_string(
                                img, lang=ocr_lang, timeout=timeout_sec
                            )
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

        except RuntimeError as e:
            # pytesseract timeout raises RuntimeError
            logger.error(f"OCR timeout หรือ Tesseract error: {type(e).__name__}")
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


def push_message(user_id: str, text: str) -> bool:
    """ส่งข้อความ Push ไปยัง LINE user.

    ใช้สำหรับแจ้งเตือนผลตรวจผิดปกติ
    ไม่ log user_id หรือเนื้อหาข้อความ
    Returns True if sent successfully, False otherwise.
    """
    if IS_TESTING and not IS_TESTING_DOCS:
        logger.info("TESTING MODE: ไม่ส่ง push message")
        return False
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.error("[PUSH] ไม่มี LINE_CHANNEL_ACCESS_TOKEN — ส่ง push message ไม่ได้")
        return False
    if not user_id:
        logger.error("[PUSH] user_id ว่าง — ส่ง push message ไม่ได้")
        return False
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
        return True
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "-")
        resp_body = ""
        try:
            resp_body = getattr(e, "response", None)
            resp_body = resp_body.text[:200] if resp_body is not None else ""
        except Exception:
            pass
        logger.error(
            f"[PUSH] Push message ไม่สำเร็จ: {type(e).__name__} "
            f"(status={status}) body={resp_body}"
        )
        return False


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
    submitted_at_ms: int = 0,
) -> dict:
    """สร้าง metadata สำหรับ GCS object.

    ต้องไม่มีข้อมูลส่วนบุคคล — ไม่เก็บชื่อไฟล์ต้นฉบับ, LINE User ID,
    ชื่อผู้ป่วย หรือเนื้อหาเอกสาร

    submitted_at_ms: timestamp จาก LINE event (มิลลิวินาที) — เวลาที่
        "ผู้ส่งส่งเอกสารเข้ามา" ใช้ตัดสินว่าเอกสารอยู่เดือนใด
        ต่างจาก received_at ซึ่งเป็นเวลาที่เซิร์ฟเวอร์ประมวลผล และจะ
        เคลื่อนไปถ้ามีการ retry หรือรวมเอกสารหลายหน้า
    """
    meta = {
        "doc_id": doc_id,
        "category": category,
        "ext": ext,
        "size_bytes": str(size_bytes),
        "content_sha256": sha256_hex,
        "received_at": datetime.now().isoformat(timespec="seconds"),
        "source": "line_oa",
        "classifier": "internal",
    }
    submitted = parse_line_timestamp(submitted_at_ms)
    if submitted:
        meta["submitted_at"] = submitted.isoformat(timespec="seconds")
    return meta


def _doc_submitted_date(doc: dict) -> "datetime | None":
    """วันที่ที่ใช้ตัดสินเดือน/ไตรมาสของเอกสารหนึ่งใบใน index

    ลำดับ: submitted_at (เวลาเต็มจาก LINE) → upload_date (วันที่ล้วน)

    upload_date เก็บเป็น "YYYY-MM-DD" ไม่มีเวลา จึงตรึงไว้ที่ "เที่ยงวัน
    ตามเวลาไทย" ไม่ใช่เที่ยงคืน UTC

    เหตุผล: ถ้าตรึงที่เที่ยงคืน UTC แล้วแปลงเป็นเวลาไทย จะได้ 07:00 ของ
    วันเดียวกัน ซึ่งยังเดือนเดิม แต่ถ้าวันนั้นเป็นวันแรกของเดือนและเคย
    ถูกคำนวณด้วยตรรกะ UTC เดิม การคำนวณใหม่อาจได้คนละเดือนและทำให้
    ระบบคิดว่าต้องย้ายไฟล์เก่า เที่ยงวันตามเวลาไทยอยู่ห่างจากขอบเขต
    วันทั้งสองฝั่ง 12 ชั่วโมง จึงให้เดือนเดิมเสมอ
    """
    if not isinstance(doc, dict):
        return None
    raw = doc.get("submitted_at", "")
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            pass
    raw_date = doc.get("upload_date", "")
    if raw_date:
        try:
            naive = datetime.strptime(raw_date, "%Y-%m-%d")
            return naive.replace(hour=12, tzinfo=BANGKOK_TZ)
        except (ValueError, TypeError):
            return None
    return None


def parse_line_timestamp(timestamp_ms) -> "datetime | None":
    """แปลง timestamp จาก LINE event (มิลลิวินาที) เป็น datetime แบบ UTC

    คืน None ถ้าค่าใช้ไม่ได้ — ให้ผู้เรียก fallback ไปเวลาปัจจุบัน
    ไม่ยอมรับเวลาที่เพี้ยนไปไกล (ก่อนปี 2020 หรืออนาคตเกิน 1 วัน)
    เพราะ timestamp ที่ผิดจะทำให้เอกสารไปอยู่เดือนที่ไม่มีอยู่จริง
    """
    try:
        ms = int(timestamp_ms)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    now = datetime.now(timezone.utc)
    if dt.year < 2020 or dt > now + timedelta(days=1):
        logger.warning(
            "LINE timestamp อยู่นอกช่วงที่รับได้ — ใช้เวลาปัจจุบันแทน"
        )
        return None
    return dt


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


# ── Bill Company Registry (มุมมองเพื่อความเข้ากันได้) ──
# รายชื่อบริษัท "ไม่ได้" อยู่ในไฟล์นี้แล้ว — ย้ายไปอยู่บน GCS ผ่าน
# VendorRegistry เพื่อให้ผู้ดูแลเพิ่มบริษัทใหม่ได้เองโดยไม่ต้องแก้โค้ด
# (ดู vendor_registry.py และ endpoint /admin/vendors)
#
# ฟังก์ชันนี้สร้าง dict รูปแบบเดิม {ชื่อแฟ้ม: [keywords]} จากทะเบียนจริง
# ไว้ให้โค้ดเดิมที่ยังอ้างถึงใช้งานต่อได้ ไม่ใช่แหล่งข้อมูลหลักอีกต่อไป


def get_bill_company_registry() -> dict:
    """คืนรายชื่อบริษัทในรูปแบบเดิม — อ่านจากทะเบียนบน GCS"""
    out: dict[str, list[str]] = {}
    try:
        for v in vendor_registry.list_all():
            keys = list(v.aliases)
            if v.tax_id:
                keys.append(v.tax_id)
            keys.extend(vendor_registry.keywords_for(v.canonical_name))
            out[v.canonical_name] = keys
    except Exception as e:
        logger.error(
            "get_bill_company_registry ล้มเหลว: %s", type(e).__name__
        )
    return out


def get_vendor_name_keywords() -> list[str]:
    """ชื่อบริษัททุกรายในทะเบียน — ใช้เป็น keyword ช่วยจัดประเภทเป็น bills

    ทำให้บริษัทที่ผู้ดูแลเพิ่งลงทะเบียนมีผลทันทีกับการจัดประเภท
    ไม่ต้องรอแก้ keyword ในโค้ด
    """
    out: list[str] = []
    try:
        for v in vendor_registry.list_all():
            out.extend(a for a in v.aliases if len(a) >= 3)
    except Exception as e:
        logger.error(
            "get_vendor_name_keywords ล้มเหลว: %s", type(e).__name__
        )
    return out


def resolve_bill_vendor(text: str) -> VendorMatch:
    """ระบุบริษัทผู้ขายจากข้อความ OCR — เส้นทางเดียวที่ใช้ทุกที่

    ทุกจุดที่ต้องรู้ว่าบิลใบนี้เป็นของบริษัทใด (จัดประเภทอัตโนมัติ,
    จัดประเภทด้วยมือ, จัดประเภทใหม่) ต้องเรียกฟังก์ชันนี้ เพื่อให้
    ผลลัพธ์ตรงกันทุกเส้นทาง

    คืน VendorMatch ซึ่งบอกทั้งแฟ้มปลายทางและว่าต้องให้คนตรวจหรือไม่
    """
    try:
        match = vendor_registry.match_document(text or "")
    except Exception as e:
        # ทะเบียนล่มต้องไม่ทำให้เอกสารหาย — ส่งเข้าแฟ้มตรวจสอบแทน
        logger.error("VendorRegistry: match ล้มเหลว: %s", type(e).__name__)
        return VendorMatch(
            method="none", reason=f"ทะเบียนบริษัทใช้งานไม่ได้ ({type(e).__name__})"
        )
    # log เฉพาะผลสรุป — ห้ามบันทึกเนื้อหาเอกสาร
    logger.info(
        "  vendor: %s (method=%s conf=%.2f auto=%s)",
        match.folder, match.method, match.confidence, match.auto_route,
    )
    return match


def detect_bill_company(text: str) -> str:
    """ตรวจจับชื่อบริษัทจากข้อความ OCR ของบิล.

    คงชื่อเดิมไว้เพื่อความเข้ากันได้ของผู้เรียกที่ต้องการแค่ชื่อแฟ้ม
    ผู้เรียกที่ต้องรู้ว่า "ต้องให้คนตรวจหรือไม่" ให้ใช้ resolve_bill_vendor

    Returns:
        ชื่อแฟ้มบริษัท หรือแฟ้มตรวจสอบ (_PENDING_REVIEW / _UNIDENTIFIED)
        ถ้าระบุบริษัทไม่ได้อย่างมั่นใจ

    หมายเหตุพฤติกรรมที่เปลี่ยน: เดิมคืน "OTHER" เมื่อไม่พบบริษัท และ
    คืนชื่อบริษัททันทีเมื่อเจอ keyword เพียงคำเดียว (ซึ่งอาจเป็นชื่อ
    สินค้าหรือชื่อผู้ซื้อ) ตอนนี้เอกสารที่ยังมั่นใจไม่พอจะเข้าแฟ้ม
    ตรวจสอบ เพื่อไม่ให้ถูกจัดเข้าแฟ้มบริษัทที่ผิด
    """
    return resolve_bill_vendor(text).folder


def classify_with_keywords(filepath: str, override_text: str | None = None) -> dict:
    """Fallback: จำแนกด้วย keyword matching + OCR.

    override_text: ถ้ามี ใช้ข้อความนี้แทน OCR จากไฟล์
    (ใช้สำหรับ multi-page merge ที่ OCR รวมไว้แล้ว)
    """
    ext = os.path.splitext(filepath)[1].lower()

    if override_text:
        # ใช้ข้อความที่ส่งมาแทน OCR — จาก multi-page merge
        content = override_text
    elif ext in (".jpg", ".jpeg", ".png"):
        content = extract_text_with_ocr(filepath)
        if not content or len(content.strip()) < 5:
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
             "ค่าเบิก", "สิทธิ์", "เบิกได้", "ส่วนเกิน",
             # สลิปโอนเงิน / ธนาคาร
             "โอนเงินสำเร็จ", "ค่าธรรมเนียม", "พร้อมเพย์", "promptpay",
             "รหัสอ้างอิง", "สลิป", "บัญชี", "เลขที่บัญชี",
             "ธนาคาร", "กรุงไทย", "กสิกร", "ไทยพาณิชย์",
             "กรุงเทพ", "ออมสิน", "ทหารไทย", "กรุงศรี",
             "ไปยัง", "ผู้รับเงิน", "ผู้โอน", "โอนสำเร็จ",
             "qr code", "วันที่ทำรายการ", "mobile banking",
             "krungthai", "kasikorn", "scb", "bbl", "ktb",
             "ยอดโอน", "transfer",
             # ใบกำกับภาษี / ใบวางบิล (medical supply)
             "ใบกำกับภาษี", "ใบวางบิล", "ใบส่งของ", "ใบส่งสินค้า",
             "เลขประจำตัวผู้เสียภาษี", "tax id", "สาขา",
             "purchase order", "po no", "p.o.", "delivery order",
             "ใบเสนอราคา", "quotation", "credit note",
             # ชื่อบริษัทคู่ค้า — ดึงจากทะเบียนบน GCS ไม่ได้ hardcode
             # บริษัทที่ผู้ดูแลเพิ่งลงทะเบียนจึงมีผลทันที
             *get_vendor_name_keywords(),
             ],
            2,
        ),
        "rx": (
            ["ใบสั่งยา", "prescription", "รายการยา", "mg", "mcg",
             "1x1", "1x2", "หลังอาหาร", "ก่อนอาหาร", "เม็ด",
             "tablet", "capsule", "แคปซูล", "ขนาดยา",
             "วันละ", "ครั้งละ", "เช้า", "กลางวัน", "เย็น",
             "ก่อนนอน", "tab", "cap", "sig", "prn",
             "ยา", "สั่งยา", "จ่ายยา", "เภสัชกร", "pharmacist",
             # ── ใบตรวจรักษาผู้ป่วย / OPD card ──
             "ใบตรวจรักษา", "visit note", "diag text",
             "diagnosis", "cc:", "hpi:", "pe text",
             "mh/pe", "อาการสำคัญ", "ประวัติปัจจุบัน",
             "ประวัติโรคประจำตัว", "ประวัติการแพ้ยา",
             "โรงพยาบาลระนอง",
             # ชื่อยาสามัญที่พบบ่อยในคลินิกไต
             "losartan", "enalapril", "amlodipine",
             "atorvastatin", "omeprazole", "aspirin",
             "carvedilol", "calcium carbonate", "folic acid",
             "ferrous", "fumarate", "calcitriol",
             "sodium bicarbonate", "venofer", "epo",
             "erythropoietin", "darbepoetin",
             "hold", "ครั้ง/สัปดาห์",
             ],
            2,
        ),
        "lab": (
            ["ผลตรวจ", "laboratory", "lab report", "ผลเลือด",
             "BUN", "creatinine", "eGFR", "hemoglobin",
             "ค่าปกติ", "mg/dL", "mg/dl", "CBC", "potassium",
             "result", "reference range", "normal range", "abnormal",
             "hematocrit", "WBC", "RBC", "platelet", "sodium",
             "albumin", "glucose", "cholesterol", "HbA1c",
             "uric acid", "calcium", "phosphorus",
             "ห้องปฏิบัติการ", "ผลแล็บ", "ผลlab", "ผล lab",
             "specimen", "serum", "blood", "urine",
             "mmol", "umol", "g/dl", "g/dL",
             # Microbiology / Culture & Sensitivity
             "microbiology", "culture", "sensitivity",
             "susceptible", "resistant", "intermediate",
             "MIC", "hemoculture", "เพาะเชื้อ", "ดื้อยา",
             "จุลชีววิทยา", "ความไวต่อยา", "antibiotic",
             "staphylococcus", "streptococcus", "pseudomonas",
             "e.coli", "klebsiella", "enterococcus", "acinetobacter",
             "coagulase", "MRSA", "ESBL", "VRE",
             "ciprofloxacin", "vancomycin", "gentamycin",
             "meropenem", "imipenem", "amikacin", "clindamycin",
             "oxacillin", "ceftriaxone", "piperacillin",
             "levofloxacin", "linezolid", "colistin",
             "tetracycline", "erythromycin", "rifampicin",
             "fosfomycin", "tigecycline", "moxifloxacin",
             "co-trimoxazole", "teicoplanin", "fusidic",
             "benzylpenicillin",
             # Pathology / Special tests
             "pathology", "cytology", "histology", "biopsy",
             "smear", "stain", "gram stain", "AFB",
             "request no", "collection date"],
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
             "ไตเทียม", "ล้างไต", "บันทึกฟอก",
             # ── เพิ่มจาก Hemodialysis work sheet จริง ──
             "hemodialysis work sheet", "HD No", "HD sheet",
             "pre-dialysis", "nursing diagnosis", "nursing care plan",
             "dialysate temp", "dialysate flow", "Total UF",
             "BFR", "UFR", "vascular access", "nephrologist",
             "pre HD", "post HD", "HD note",
             "anticoagulant", "surface area",
             "conduct", "repletement", "complication",
             "hypotension", "muscle cramp", "dialyzer clot",
             "clotted bloodline", "discharge by",
             "last TCV", "use No", "loading", "maintenance",
             "flush Nss", "machine", "SADL",
             ],
            2,
        ),
    }

    # ── Pre-check: สลิปโอนเงิน / ธนาคาร ──
    # ถ้าพบ keyword ที่บ่งชี้ว่าเป็นสลิปโอนเงินชัดเจน → บังคับ bills
    # เพื่อป้องกันการจัดประเภทผิดเป็นผลแล็บ
    _bank_strong = [
        "โอนเงินสำเร็จ", "โอนสำเร็จ", "พร้อมเพย์", "promptpay",
        "mobile banking", "สลิปโอน", "ค่าธรรมเนียม",
        "krungthai", "kasikorn", "กรุงไทย", "กสิกร",
    ]
    bank_hits = [kw for kw in _bank_strong if kw.lower() in content_lower]
    if len(bank_hits) >= 2:
        _slip_match = resolve_bill_vendor(content)
        logger.info(
            "  pre-check: bank slip detected (%d strong hits) → bills/%s",
            len(bank_hits), _slip_match.folder,
        )
        return {
            "category": "bills",
            "bill_company": _slip_match.folder,
            "vendor_match": _slip_match.to_dict(),
            "reason": f"bank slip pre-check ({len(bank_hits)} strong hits)",
            "confidence": min(len(bank_hits) / 3.0, 1.0),
        }

    # ── Pre-check: บริษัท supplier ที่รู้จัก ──
    # ถ้าระบุบริษัทในทะเบียนได้อย่างมั่นใจ (auto_route) และมี keyword
    # เกี่ยวกับเงิน/ราคาอย่างน้อย 1 → บังคับ bills
    #
    # เดิมใช้การนับ keyword จาก BILL_COMPANY_REGISTRY ≥2 ซึ่งนับชื่อสินค้า
    # รวมกับชื่อบริษัท ทำให้บิลที่เอ่ยถึงสินค้าของบริษัทอื่นถูกจัดผิดแฟ้มได้
    # ตอนนี้ใช้ผลจากทะเบียนบริษัทซึ่งต้องตรงด้วยเลขผู้เสียภาษีหรือชื่อบริษัท
    _vendor_match = resolve_bill_vendor(content)
    _money_kws = [
        "บาท", "ราคา", "จำนวนเงิน", "ยอดรวม", "รวมทั้งสิ้น",
        "ภาษี", "vat", "tax", "ใบกำกับ", "invoice", "ใบเสร็จ",
        "ใบแจ้งหนี้", "ชำระเงิน", "ส่วนลด", "receipt",
        "ใบกำกับภาษี", "ใบวางบิล", "ใบส่งของ",
    ]
    _money_hits = sum(1 for kw in _money_kws if kw.lower() in content_lower)
    if _vendor_match.auto_route and _money_hits >= 1:
        logger.info(
            "  pre-check: known supplier %s (%s, %d money hits) → bills",
            _vendor_match.folder, _vendor_match.method, _money_hits,
        )
        return {
            "category": "bills",
            "bill_company": _vendor_match.folder,
            "vendor_match": _vendor_match.to_dict(),
            "reason": (
                f"known supplier pre-check: {_vendor_match.folder} "
                f"({_vendor_match.method}, {_money_hits} money hits)"
            ),
            "confidence": min(
                _vendor_match.confidence + _money_hits / 20.0, 1.0
            ),
        }

    # ── Pre-check: ใบฟอกเลือด (Hemodialysis) ──
    # ถ้าพบ strong HD keywords ≥2 → บังคับ dialysis
    # ป้องกันใบ HD ที่มี Hb/Hct/EPO จากการถูกจำแนกเป็น lab/rx
    _hd_strong_kws = [
        "hemodialysis", "dialysis", "dialyzer", "dialysate",
        "hemodialysis work sheet", "HD No", "HD sheet",
        "ฟอกเลือด", "ฟอกไต", "ไตเทียม", "ล้างไต",
        "pre-HD", "post-HD", "pre HD", "post HD",
        "vascular access", "nephrologist",
        "ultrafiltration", "Total UF", "UFR", "BFR",
        "dry weight", "น้ำหนักแห้ง", "บันทึกการฟอก",
        "nursing diagnosis", "nursing care plan",
        "anticoagulant", "dialysate temp", "dialysate flow",
        "flush Nss", "repletement", "clotted bloodline",
    ]
    _hd_hits = [kw for kw in _hd_strong_kws if kw.lower() in content_lower]
    if len(_hd_hits) >= 2:
        logger.info(
            f"  pre-check: hemodialysis detected "
            f"({len(_hd_hits)} strong HD kws) → dialysis"
        )
        return {
            "category": "dialysis",
            "reason": f"hemodialysis pre-check ({len(_hd_hits)} strong HD hits)",
            "confidence": min(len(_hd_hits) / 4.0, 1.0),
        }

    # ── Pre-check: ใบสั่งยา / ใบตรวจรักษาผู้ป่วย ──
    # ถ้าพบชื่อยา ≥3 ตัว + keyword ทางการแพทย์ → บังคับ rx
    # ป้องกัน "บาท" ท้ายใบยาจากการดึงเอกสารเข้า bills
    _rx_drug_names = [
        "losartan", "enalapril", "amlodipine", "atorvastatin",
        "omeprazole", "aspirin", "carvedilol", "metformin",
        "calcium carbonate", "folic acid", "ferrous", "calcitriol",
        "sodium bicarbonate", "venofer", "furosemide", "simvastatin",
        "clopidogrel", "warfarin", "insulin", "metoprolol",
        "nifedipine", "diltiazem", "verapamil", "hydralazine",
        "spironolactone", "hydrochlorothiazide", "doxazosin",
        "allopurinol", "colchicine", "prednisolone", "dexamethasone",
        "pantoprazole", "ranitidine", "domperidone", "ondansetron",
        "paracetamol", "tramadol", "gabapentin", "pregabalin",
        "amitriptyline", "fluoxetine", "diazepam", "alprazolam",
        "phenytoin", "levetiracetam", "ciprofloxacin", "amoxicillin",
        "cephalexin", "clindamycin", "metronidazole", "doxycycline",
        "azithromycin", "levofloxacin", "co-trimoxazole",
        "sevelamer", "lanthanum", "cinacalcet", "epo",
        "erythropoietin", "darbepoetin", "epoetin",
    ]
    _rx_medical_kws = [
        "mg", "mcg", "tab", "cap", "เม็ด", "หลังอาหาร",
        "ก่อนอาหาร", "ก่อนนอน", "วันละ", "ครั้งละ",
        "hold", "diagnosis", "diag text", "visit note",
        "ใบตรวจรักษา", "mh/pe", "cc:",
    ]
    _drug_hits = [d for d in _rx_drug_names if d.lower() in content_lower]
    _rx_med_hits = [k for k in _rx_medical_kws if k.lower() in content_lower]
    if len(_drug_hits) >= 3 and len(_rx_med_hits) >= 2:
        logger.info(
            f"  pre-check: prescription detected "
            f"({len(_drug_hits)} drugs, {len(_rx_med_hits)} medical kws) → rx"
        )
        return {
            "category": "rx",
            "reason": (
                f"prescription pre-check "
                f"({len(_drug_hits)} drugs, {len(_rx_med_hits)} medical kws)"
            ),
            "confidence": min((len(_drug_hits) + len(_rx_med_hits)) / 8.0, 1.0),
        }

    best, best_score = "review", 0
    all_matches = {}
    for cat, (kws, min_match) in rules.items():
        matched = [kw for kw in kws if kw.lower() in content_lower]
        score = len(matched)
        all_matches[cat] = matched
        if score >= min_match and score > best_score:
            best, best_score = cat, score

    # ── Tie-breaker: rx vs bills ──
    # ถ้า bills ชนะแต่ rx มี hits สูงเกือบเท่า → ตรวจสอบว่ามีชื่อยาจริง
    # ใบยาที่มีราคาท้ายใบมักถูกจัดเป็น bills เพราะ "บาท"
    if best == "bills" and "rx" in all_matches:
        rx_hits = len(all_matches["rx"])
        bills_hits = len(all_matches["bills"])
        drug_in_text = len(_drug_hits)
        if rx_hits >= 2 and drug_in_text >= 2:
            logger.info(
                f"  tie-breaker: bills({bills_hits}) vs rx({rx_hits}) "
                f"+ {drug_in_text} drug names → rx wins"
            )
            best = "rx"
            best_score = rx_hits

    # log เฉพาะจำนวน hits — ไม่ log รายการคำที่ match
    # เพราะคำที่พบเปิดเผยเนื้อหาเชิงคลินิกของเอกสาร
    for cat, matched in all_matches.items():
        if matched:
            logger.info(f"  keyword [{cat}]: {len(matched)} hits")

    result = {
        "category": best,
        "reason": f"keyword match ({best_score} hits)",
        "confidence": min(best_score / 5.0, 1.0) if best_score > 0 else 0.0,
    }

    # ถ้าจัดเป็น bills → ตรวจจับบริษัทจาก OCR text
    if best == "bills":
        result["bill_company"] = detect_bill_company(content)

    return result


def copy_to_category(
    src_path: str, category: str, metadata: dict | None = None,
    subfolder: str | None = None, owner_hash_full: str = "",
) -> str:
    """คัดลอกไฟล์ไปยังโฟลเดอร์ตามประเภท + cloud storage.

    Production mode: ถ้า cloud upload ล้มเหลว จะ raise RuntimeError
    เพื่อให้ process_file_async() จับแล้วแจ้งข้อผิดพลาดกลับไป

    subfolder: subfolder เพิ่มเติม เช่น ชื่อบริษัทสำหรับ bills
               → GCS path จะเป็น 02_BILLS_RECEIPTS/NIKKISO/2026-10/file.jpg
    """
    # Local copy (ephemeral — เป็น temp ระหว่างรอ upload)
    base_folder = FOLDERS.get(category, FOLDERS["review"])
    # ชื่อ subfolder ต้องผ่านการตรวจก่อนต่อเป็น path บนดิสก์
    # ปกติมาจากทะเบียนบริษัทซึ่งตรวจแล้ว แต่กันไว้อีกชั้นเพราะจุดนี้
    # เป็นการเขียนไฟล์จริง ถ้ามีผู้เรียกใหม่ส่งค่าที่ไม่ได้ตรวจเข้ามา
    # path จะหลุดออกนอก BASE_DIR ได้
    safe_subfolder = _safe_folder_component(subfolder or "")
    if safe_subfolder:
        dest_folder = os.path.join(BASE_DIR, base_folder, safe_subfolder)
    else:
        dest_folder = os.path.join(BASE_DIR, base_folder)
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
    log_dest = (
        f"{base_folder}/{safe_subfolder}" if safe_subfolder else base_folder
    )
    logger.info(f"คัดลอก → {log_dest}")

    # Cloud storage (ถ้าตั้งค่าไว้ และ backend ไม่ใช่ local)
    cloud_path = None
    if STORAGE_BACKEND != "local":
        try:
            with open(src_path, "rb") as f:
                data = f.read()
            cloud_path = storage.save(
                data, category, filename, metadata, subfolder=subfolder,
                owner_hash_full=owner_hash_full,
            )
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
    merged_ocr_text: str | None = None,
    merged_page_count: int = 0,
    submitted_at_ms: int = 0,
):
    """ประมวลผลไฟล์ในเธรดแยก.

    doc_id และ sha256_hex มาจากตอนรับไฟล์ ถ้าไม่ส่งมาจะอนุมานจากชื่อไฟล์
    (ชื่อไฟล์เป็น UUID อยู่แล้ว) เพื่อให้เรียกใช้แบบเดิมได้

    owner_hash: SHA-256 hash ของ LINE user ID ของผู้ส่ง — ใช้สำหรับ
    cross-patient access prevention (ผู้ใช้เห็นเฉพาะเอกสารที่ตัวเองส่ง)

    line_user_id: LINE user ID ดิบ — ใช้สำหรับ push_message กลับไปหาผู้ใช้
    เมื่อพบผลตรวจผิดปกติ (Lab Alert) ไม่ถูกบันทึกลง log หรือ metadata

    merged_ocr_text: ข้อความ OCR ที่รวมจากหลายหน้าแล้ว (จาก multi-page buffer)
    merged_page_count: จำนวนหน้าที่รวม (0 = ไม่ได้รวม)
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
                # ถ้ามี merged_ocr_text จาก multi-page → ใช้ข้อความรวมสำหรับ classify
                if merged_ocr_text:
                    result = classify_with_keywords(filepath, override_text=merged_ocr_text)
                else:
                    result = classify_with_keywords(filepath)

            category = result.get("category", "review")
            # ชื่อแฟ้มบริษัทต้องมาจากทะเบียนเท่านั้น
            #
            # ห้ามนำค่าที่ผู้จำแนกส่งกลับมาใช้เป็นชื่อแฟ้มโดยตรง เพราะถ้า
            # เปิดการจำแนกด้วย AI ค่านั้นคือข้อความอิสระจากโมเดล การนำไป
            # ต่อเป็น path จึงเป็นการเชื่อข้อความที่ไม่ได้ตรวจ ที่นี่จึง
            # รับเฉพาะชื่อที่ผ่านการตรวจรูปแบบและมีอยู่ในทะเบียนจริง
            bill_company = ""
            _vendor_info: dict = {}
            if category == "bills":
                _claimed = (result.get("bill_company") or "").strip().upper()
                _vm = result.get("vendor_match") or {}
                if _vm.get("folder") and _vm.get("auto_route") is not None:
                    # มาจาก resolve_bill_vendor ใน pre-check แล้ว — ตรวจแล้ว
                    bill_company = _vm["folder"]
                    _vendor_info = _vm
                elif (
                    _claimed
                    and is_valid_canonical_name(_claimed)
                    and vendor_registry.get(_claimed) is not None
                ):
                    bill_company = _claimed
                    _vendor_info = {
                        "folder": _claimed, "method": "classifier_validated",
                        "needs_review": False, "extracted_name": "",
                    }
                else:
                    if _claimed:
                        logger.warning(
                            "ชื่อบริษัทจากผู้จำแนกไม่อยู่ในทะเบียน — "
                            "ไม่ใช้เป็นชื่อแฟ้ม"
                        )
                    bill_company = ""  # เติมภายหลังเมื่อได้ข้อความ OCR
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
                submitted_at_ms=submitted_at_ms,
            )
            # เพิ่ม owner_hash สำหรับ quarterly/monthly path construction
            if owner_hash:
                metadata["owner_hash_short"] = owner_hash[:8]

            # ── Document Search V1: extract text for indexing ──
            extracted_text = ""
            ocr_status = "none"

            # ถ้ามี merged_ocr_text จาก multi-page buffer → ใช้ข้อความรวมเลย
            if merged_ocr_text:
                extracted_text = merged_ocr_text
                ocr_status = f"ocr_merged_{merged_page_count}pages"
                logger.info(
                    f"ใช้ merged OCR text: {merged_page_count} หน้า, "
                    f"{len(extracted_text)} chars"
                )
            else:
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

            # ── Auto HN Routing: ดึง HN จาก OCR → จับคู่กับทะเบียน ──
            if extracted_text and category in ("lab", "rx", "dialysis"):
                try:
                    found_hns = extract_hn_from_document(extracted_text)
                    logger.info(
                        "Auto HN route: extracted %d HN(s) from OCR "
                        "(category=%s, text_len=%d)",
                        len(found_hns), category, len(extracted_text),
                    )
                    if found_hns and master_registry:
                        _reg_count = master_registry.count()
                        logger.info(
                            "Auto HN route: registry has %d patients, "
                            "checking %d HN candidates",
                            _reg_count, len(found_hns),
                        )
                        for hn_candidate in found_hns:
                            patient_folder = master_registry.get_folder_for_hn(
                                hn_candidate
                            )
                            if patient_folder:
                                metadata["patient_hn_folder"] = patient_folder
                                logger.info(
                                    "Auto HN route: matched HN → patient folder "
                                    "(category=%s)", category
                                )
                                break
                            else:
                                logger.info(
                                    "Auto HN route: no match for HN candidate "
                                    "(category=%s)", category
                                )
                    elif not found_hns:
                        logger.info(
                            "Auto HN route: no HN found in OCR text "
                            "(category=%s)", category
                        )
                except Exception as hn_err:
                    logger.warning(
                        "HN extraction failed: %s", type(hn_err).__name__
                    )

            # ── โหมดเลือกผู้ป่วย: เจ้าหน้าที่ระบุผู้ป่วยไว้ก่อนส่งเอกสาร ──
            # เอกสารคลินิกส่งจากบัญชีหมอ/เจ้าหน้าที่ จึงใช้ HN จากเอกสาร
            # (OCR) ไม่ได้เสมอ — ถ้าเจ้าหน้าที่เลือกผู้ป่วยไว้ ให้ใช้ค่านั้น
            _ctx_conflict = ""
            if category in CLINICAL_CATEGORIES and owner_hash:
                try:
                    _ctx = _get_patient_context(owner_hash)
                    if _ctx:
                        _ctx_folder = _ctx.get("patient_folder", "")
                        _ocr_folder = metadata.get("patient_hn_folder", "")
                        if _ctx_folder and _ocr_folder and _ctx_folder != _ocr_folder:
                            # OCR กับที่เจ้าหน้าที่เลือกไม่ตรงกัน — ใช้ค่าที่
                            # เจ้าหน้าที่เลือก (เป็นการระบุโดยเจตนา) แต่ต้อง
                            # เตือนให้เห็นชัด เพื่อให้แก้ได้ทันทีถ้าผิด
                            _ctx_conflict = _ocr_folder
                            metadata["patient_hn_folder"] = _ctx_folder
                            logger.warning(
                                "Patient context conflicts with OCR HN "
                                "(category=%s) — using staff selection",
                                category,
                            )
                        elif _ctx_folder and not _ocr_folder:
                            metadata["patient_hn_folder"] = _ctx_folder
                            logger.info(
                                "Patient context applied (category=%s)",
                                category,
                            )
                except Exception as ctx_err:
                    logger.warning(
                        "Patient context lookup failed: %s",
                        type(ctx_err).__name__,
                    )

            # บิลที่ยังไม่รู้บริษัท — ระบุจากข้อความ OCR ที่ได้มาแล้ว
            # เป็นจุดเดียวที่ตัดสินแฟ้มบริษัทของเส้นทางอัตโนมัติ
            if category == "bills" and not bill_company:
                _vm_obj = resolve_bill_vendor(extracted_text)
                bill_company = _vm_obj.folder
                _vendor_info = _vm_obj.to_dict()

            dest = copy_to_category(
                filepath, category, metadata=metadata,
                subfolder=bill_company,
                owner_hash_full=owner_hash or "",
            )

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
                # วันที่แสดงผล — ใช้ปฏิทินไทย เพื่อให้ตรงกับแฟ้มที่เอกสาร
                # ไปอยู่ ไม่ให้วันที่ใน index ต่างจากชื่อแฟ้มบน storage
                _submitted_dt = metadata.get("submitted_at", "")
                try:
                    _display_dt = (
                        datetime.fromisoformat(_submitted_dt)
                        if _submitted_dt else datetime.now(timezone.utc)
                    )
                except (ValueError, TypeError):
                    _display_dt = datetime.now(timezone.utc)

                doc_meta = {
                    "document_id": _doc_id,
                    "original_filename": f"document{ext_now}",
                    "gcs_object_path": gcs_object_path,
                    "category": category,
                    "upload_date": to_bangkok(_display_dt).strftime("%Y-%m-%d"),
                    "mime_type": mime_map.get(ext_now, "application/octet-stream"),
                    "file_size": size_now,
                    "ocr_status": ocr_status,
                    "extracted_text": extracted_text,
                    # Owner tracking — ป้องกัน cross-patient access
                    # ผู้ใช้เห็นเฉพาะเอกสารที่ตัวเองส่ง
                    "owner_hash": owner_hash or "",
                }
                # เวลาส่งแบบเต็ม — ให้การจัดประเภทใหม่ภายหลังคำนวณเดือน
                # ได้ตรงกับตอนส่ง ไม่ใช่เดือนที่จัดประเภทใหม่
                if _submitted_dt:
                    doc_meta["submitted_at"] = _submitted_dt
                # บิล: เก็บผลการระบุบริษัทไว้ให้คิวตรวจสอบและหน้า dashboard
                if category == "bills" and bill_company:
                    doc_meta["bill_company"] = bill_company
                    if _vendor_info:
                        doc_meta["vendor_method"] = _vendor_info.get("method", "")
                        doc_meta["vendor_needs_review"] = bool(
                            _vendor_info.get("needs_review")
                        )
                        doc_meta["vendor_extracted_name"] = _vendor_info.get(
                            "extracted_name", ""
                        )
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
                        # ── Phase 2A: OCR Safety Validation ──
                        if ocr_validator is not None:
                            try:
                                _validation = ocr_validator.validate_report(
                                    lab_report
                                )
                                if _validation.get("flags"):
                                    lab_report.ocr_flags = _validation[
                                        "flags"
                                    ]
                                    logger.info(
                                        f"OCR validation flags: "
                                        f"doc={_doc_id[:8]}, "
                                        f"flags={len(_validation['flags'])}"
                                    )
                            except Exception as _ocr_err:
                                logger.warning(
                                    f"OCR validation error: "
                                    f"{type(_ocr_err).__name__}"
                                )

                        # บันทึกผลแล็บลง GCS — create-only
                        _lab_json = lab_report.to_json()
                        _lab_path = f"{LAB_RESULTS_PREFIX}{_doc_id}.json"
                        storage.put(
                            _lab_path,
                            _lab_json.encode("utf-8"),
                            content_type="application/json",
                            create_only=True,
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
                                        create_only=True,
                                    )
                                    logger.info(
                                        f"Critical alert stored: "
                                        f"doc={_doc_id[:8]}"
                                    )

                            # ── Phase 2A: Enhanced Alert Delivery ──
                            if (alert_service is not None
                                    and _alert_summary.critical_count > 0):
                                try:
                                    # สร้าง alert สำหรับแต่ละ critical
                                    for _ca in getattr(
                                        _alert_summary, 'alerts', []
                                    ):
                                        if getattr(
                                            _ca, 'level', ''
                                        ) == 'critical':
                                            _new_alert = (
                                                alert_service.create_alert(
                                                    doc_id=_doc_id[:8],
                                                    owner_hash=(
                                                        owner_hash or ""
                                                    )[:8],
                                                    analyte=getattr(
                                                        _ca, 'analyte', ''
                                                    ),
                                                    value=getattr(
                                                        _ca, 'value', 0
                                                    ),
                                                    unit=getattr(
                                                        _ca, 'unit', ''
                                                    ),
                                                    alert_level='critical',
                                                )
                                            )
                                            # ส่ง LINE push ทันที
                                            if (_new_alert is not None
                                                    and line_push_fn
                                                    is not None):
                                                alert_service \
                                                    .attempt_delivery(
                                                        _new_alert
                                                        .alert_id,
                                                        send_push_fn=(
                                                            line_push_fn
                                                        ),
                                                    )
                                    logger.info(
                                        f"Phase 2A alerts created: "
                                        f"doc={_doc_id[:8]}"
                                    )
                                except Exception as _adel_err:
                                    logger.warning(
                                        f"Phase 2A alert delivery "
                                        f"error: "
                                        f"{type(_adel_err).__name__}"
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
                    # แต่ต้อง log รายละเอียดให้เพียงพอสำหรับ debug
                    import traceback as _tb_mod
                    _lab_tb = _tb_mod.format_exc()
                    # sanitize: ไม่ log doc_id เต็ม, ไม่ log ข้อมูลผู้ป่วย
                    _lab_err_msg = str(lab_err)
                    # ตัด path ที่อาจมี doc_id เต็ม
                    if _doc_id and len(_doc_id) > 8:
                        if _doc_id in _lab_err_msg:
                            _lab_err_msg = _lab_err_msg.replace(
                                _doc_id, _doc_id[:8] + "…"
                            )
                        _lab_tb = _lab_tb.replace(
                            _doc_id, _doc_id[:8] + "…"
                        )
                    # ตัด signed URL ที่อาจมี token
                    import re as _re_mod_lab
                    _lab_tb = _re_mod_lab.sub(
                        r'https://storage\.googleapis\.com/[^\s"\']+',
                        "[GCS_URL_REDACTED]",
                        _lab_tb,
                    )
                    _lab_err_msg = _re_mod_lab.sub(
                        r'https://storage\.googleapis\.com/[^\s"\']+',
                        "[GCS_URL_REDACTED]",
                        _lab_err_msg,
                    )
                    logger.error(
                        f"Lab extraction ล้มเหลว: {type(lab_err).__name__}: "
                        f"{_lab_err_msg} (doc_id={_doc_id[:8]})"
                    )
                    logger.error(
                        f"Lab extraction traceback (doc_id={_doc_id[:8]}):\n"
                        f"{_lab_tb}"
                    )

            # ── Clinical Analysis V1: วิเคราะห์ผลแล็บด้วย Rule-based Engine ──
            # SAFETY NOTE: ผลวิเคราะห์ทำทันทีหลัง OCR เพื่อแจ้งเตือน
            # แต่จะถูก tag ว่า PENDING_VERIFICATION จนกว่าแพทย์จะ approve
            clinical_analyzed = False
            if lab_extracted and lab_report and lab_report.results:
                try:
                    from clinical_analyzer import analyze_lab_report as run_clinical
                    clinical_result = run_clinical(lab_report)
                    if clinical_result.findings:
                        _clinical_dict = clinical_result.to_dict()
                        # Tag ว่าผล OCR ยังไม่ผ่าน physician verification
                        _clinical_dict["verification_status"] = "PENDING_VERIFICATION"
                        # ผูก version ของ report ที่ใช้วิเคราะห์
                        # ณ จุดนี้ report เพิ่งสร้าง → version = 1
                        _clinical_dict["source_report_version"] = 1
                        _clinical_dict["disclaimer"] = (
                            "ผลวิเคราะห์นี้สร้างจากผล OCR อัตโนมัติ "
                            "ยังไม่ผ่านการตรวจสอบโดยแพทย์ "
                            "ห้ามใช้ประกอบการตัดสินใจทางคลินิก"
                            "จนกว่าจะได้รับการยืนยัน"
                        )
                        _clinical_json = json.dumps(
                            _clinical_dict, ensure_ascii=False
                        )
                        _clinical_path = (
                            f"{CLINICAL_ANALYSIS_PREFIX}{_doc_id}.json"
                        )
                        storage.put(
                            _clinical_path,
                            _clinical_json.encode("utf-8"),
                            content_type="application/json",
                            create_only=True,
                        )
                        clinical_analyzed = True
                        logger.info(
                            f"Clinical analysis สำเร็จ (PENDING_VERIFICATION): "
                            f"doc={_doc_id[:8]}, "
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
                    _cat_folder = FOLDERS.get(category, FOLDERS["review"])
                    if bill_company:
                        _local_cat_dest = os.path.join(
                            BASE_DIR, _cat_folder, bill_company,
                            os.path.basename(filepath),
                        )
                    else:
                        _local_cat_dest = os.path.join(
                            BASE_DIR, _cat_folder,
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

            # ── แสดง sub-folder path ที่จัดเก็บจริง ──
            # ดึง relative path จาก dest (gs://bucket/path)
            _storage_display = FOLDERS.get(category, FOLDERS['review'])
            if dest and dest.startswith("gs://"):
                # dest = gs://bucket/04_LAB_RESULTS/2026-Q4/2026-10/uuid.pdf
                # ตัดเอาเฉพาะ folder path (ไม่รวม filename)
                _gcs_rel = dest.split("/", 3)[-1] if "/" in dest else dest
                # ตัด filename ออก เหลือแค่ folder structure
                _folder_path = _gcs_rel.rsplit("/", 1)[0] if "/" in _gcs_rel else _gcs_rel
                _storage_display = _folder_path
            elif bill_company and category == "bills":
                _storage_display = f"{_storage_display}/{bill_company}"
            _reply_lines = [
                f"รับเอกสารแล้ว ({file_type_name})",
                f"📁 ประเภท: {cat_name}",
                f"📂 จัดเก็บใน: {_storage_display}",
            ]
            if bill_company and category == "bills":
                _reply_lines.append(f"🏢 บริษัท: {bill_company}")
            if merged_page_count > 1:
                _reply_lines.insert(0, f"📄 รวมผลแล็บ {merged_page_count} หน้าสำเร็จ")
            if lab_extracted:
                _reply_lines.append(
                    f"📋 พบผลตรวจ {len(lab_report.results)} รายการ"
                )
                _reply_lines.append(
                    f"พิมพ์ 'ผลแล็บ {_doc_id[:8]}' เพื่อดูผลตรวจ"
                )
                # แนะนำให้เปรียบเทียบกับผลเก่า
                _reply_lines.append(
                    "📊 พิมพ์ 'เปรียบเทียบแล็บ' เพื่อเทียบกับผลครั้งก่อน"
                )
            if clinical_analyzed:
                _reply_lines.append(
                    f"🔬 วิเคราะห์ทางคลินิกเสร็จสิ้น"
                )
                _reply_lines.append(
                    f"พิมพ์ 'วิเคราะห์แล็บ {_doc_id[:8]}' เพื่อดูผลวิเคราะห์"
                )
            # แสดงแฟ้มผู้ป่วยทุกหมวดคลินิก — เจ้าหน้าที่ต้องเห็นว่าเอกสาร
            # เข้าแฟ้มใคร เพื่อจับความผิดพลาดได้ทันทีที่ส่ง
            if category in CLINICAL_CATEGORIES:
                if metadata.get("patient_hn_folder"):
                    _reply_lines.append(
                        f"👤 แฟ้มผู้ป่วย: {metadata['patient_hn_folder']}"
                    )
                    if _ctx_conflict:
                        _reply_lines.append(
                            f"⚠️ HN ในเอกสารอ่านได้เป็น: {_ctx_conflict}\n"
                            f"ระบบใช้ผู้ป่วยที่เลือกไว้ หากผิดให้พิมพ์ "
                            f"'ผู้ป่วย <HN>' ให้ถูกแล้วส่งเอกสารใหม่"
                        )
                else:
                    _reply_lines.append(
                        "👤 ยังไม่ระบุผู้ป่วย — เก็บไว้ที่ระดับไตรมาส/เดือน\n"
                        "พิมพ์ 'ผู้ป่วย <HN>' ก่อนส่ง เพื่อเข้าแฟ้มรายคน"
                    )
            _result_text = "\n".join(_reply_lines)

            # ── Interactive Classification: ถามผู้ใช้เมื่อ OCR ล้มเหลว ──
            if category == "review" and line_user_id:
                # OCR ไม่สามารถจำแนกได้ → ถามผู้ใช้ผ่าน LINE
                _pending_user_hash = owner_hash or ""
                if _pending_user_hash and dest:
                    _set_pending_classification(
                        _pending_user_hash,
                        _doc_id,
                        dest,
                        _pending_user_hash,
                        line_user_id,
                        sha256_hex or "",
                    )
                    _ask_text = (
                        "📋 ระบบไม่สามารถจำแนกเอกสารอัตโนมัติได้\n"
                        "กรุณาเลือกประเภทเอกสาร:\n\n"
                        "ตอบ 1 = ใบสั่งยา 💊\n"
                        "ตอบ 2 = ผลตรวจแล็บ 🔬\n"
                        "ตอบ 3 = เอกสารฟอกเลือด 🩸\n"
                        "ตอบ 4 = ใบเสร็จ/บิล 🧾\n\n"
                        "⏱️ กรุณาตอบภายใน 5 นาที"
                    )
                    if reply_token:
                        reply_message(reply_token, _ask_text)
                    else:
                        push_message(line_user_id, _ask_text)
                else:
                    # ไม่มี owner_hash หรือ dest → ส่ง reply ปกติ
                    if reply_token:
                        reply_message(reply_token, _result_text)
                    elif line_user_id:
                        push_message(line_user_id, _result_text)
            elif reply_token:
                reply_message(reply_token, _result_text)
            elif line_user_id:
                # Multi-page merge: reply_token หมดอายุ → ใช้ push_message
                push_message(line_user_id, _result_text)
        except RuntimeError as e:
            # Production mode — GCS ล้มเหลว ต้องแจ้งผู้ใช้
            # บันทึกเฉพาะชนิด exception — ห้าม log message
            # เพราะอาจมี path หรือข้อมูลผู้ป่วยรั่ว
            logger.error(
                f"จัดเก็บเอกสารล้มเหลว (Production): "
                f"{type(e).__name__} (doc_id={_doc_id})"
            )
            _store_err_msg = (
                "ขออภัย ระบบไม่สามารถจัดเก็บเอกสารได้ในขณะนี้\n"
                "กรุณาลองส่งใหม่ในอีกสักครู่\n"
                "หากยังไม่ได้ กรุณาแจ้งเจ้าหน้าที่"
            )
            if reply_token:
                reply_message(reply_token, _store_err_msg)
            elif line_user_id:
                # Multi-page flush: reply_token หมดอายุ → ใช้ push_message
                push_message(line_user_id, _store_err_msg)
        except Exception as e:
            # ไม่ log str(e) — ข้อความ exception ของ I/O มักมี path พร้อมชื่อไฟล์
            logger.error(
                f"ประมวลผลไฟล์ล้มเหลว: {type(e).__name__} (doc_id={_doc_id})"
            )
            _err_msg = (
                "ขออภัย เกิดข้อผิดพลาดในการประมวลผลเอกสาร\n"
                "กรุณาลองส่งใหม่อีกครั้ง"
            )
            if reply_token:
                reply_message(reply_token, _err_msg)
            elif line_user_id:
                # Multi-page flush: reply_token หมดอายุ → ใช้ push_message
                push_message(line_user_id, _err_msg)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


# ─── Multi-Page Lab Report Buffer ─────────────────────────
# เมื่อผู้ใช้ส่งภาพหลายรูปติดกัน (เช่น ผลแล็บหลายหน้า)
# ระบบจะรอรวมรูปทั้งหมดเป็นเอกสารเดียว แทนที่จะแยกประมวลผลทีละรูป
# Window: 30 วินาที — หลังรูปสุดท้ายจะเริ่มประมวลผลรวม
# ใช้ได้เฉพาะภาพ (image) — ไฟล์ PDF/TXT ประมวลผลทันทีเหมือนเดิม
#
# [FIX] ใช้ /tmp filesystem แทน in-memory dict เพื่อให้ทุก Gunicorn
# worker เข้าถึงข้อมูลเดียวกัน — แก้ปัญหาภาพจาก user เดียวกัน
# hit worker คนละตัว แล้วบัฟเฟอร์แยกกัน
# ─────────────────────────────────────────────────────────────

_MULTIPAGE_WINDOW_SECS = int(os.environ.get("MULTIPAGE_WINDOW_SECS", "30"))
_MULTIPAGE_MAX_PAGES = int(os.environ.get("MULTIPAGE_MAX_PAGES", "10"))

# /tmp-based multipage buffer (shared across Gunicorn workers)
_MULTIPAGE_DIR = os.path.join(tempfile.gettempdir(), "ranong_raktai", "multipage")
os.makedirs(_MULTIPAGE_DIR, exist_ok=True)

# Per-worker timer tracking (for cancellation only — data is in /tmp)
_multipage_lock = threading.Lock()
_multipage_timers: dict[str, threading.Timer] = {}


def _mp_meta_path(user_hash: str) -> str:
    return os.path.join(_MULTIPAGE_DIR, f"{user_hash}.json")


def _mp_page_path(user_hash: str, page_num: int) -> str:
    return os.path.join(_MULTIPAGE_DIR, f"{user_hash}_p{page_num}.dat")


def _mp_lock_path(user_hash: str) -> str:
    return os.path.join(_MULTIPAGE_DIR, f"{user_hash}.lock")


def _mp_read_meta(user_hash: str) -> dict | None:
    """Read multipage buffer metadata from /tmp."""
    path = _mp_meta_path(user_hash)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None


def _mp_write_meta(user_hash: str, meta: dict):
    """Write multipage buffer metadata to /tmp atomically."""
    path = _mp_meta_path(user_hash)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.replace(tmp, path)


def _mp_clear(user_hash: str, page_count: int):
    """Clear multipage buffer files from /tmp.

    NOTE: ไม่ลบ lock file (.lock) — เพราะ worker อื่นอาจยังถือ fd อยู่
    lock file ขนาดเล็กมาก (0 bytes) ไม่กระทบ disk
    """
    try:
        os.remove(_mp_meta_path(user_hash))
    except FileNotFoundError:
        pass
    for i in range(1, page_count + 1):
        try:
            os.remove(_mp_page_path(user_hash, i))
        except FileNotFoundError:
            pass
    # ไม่ลบ .lock file — ป้องกัน race condition กับ worker อื่นที่อาจถือ lock อยู่


def _mp_acquire_lock(user_hash: str):
    """Acquire file-based lock for cross-worker synchronization."""
    import fcntl
    lock_path = _mp_lock_path(user_hash)
    fd = open(lock_path, "w")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _mp_release_lock(fd):
    """Release file-based lock."""
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    except Exception:
        pass

# ── Registration Mode: admin ถ่ายรูปรายชื่อผู้ป่วย ──────────
# เมื่อ admin พิมพ์ "ลงทะเบียนรายชื่อ" → เข้าโหมดรอรับรูป
# รูปถัดไปที่ส่งจะถูก OCR แล้วแปลงเป็นทะเบียนผู้ป่วย
_REGISTRATION_TIMEOUT_SECS = 120  # หมดเวลาหลัง 2 นาที

# ── File-based registration mode (shared across Gunicorn workers) ──
_REG_MODE_DIR = os.path.join(tempfile.gettempdir(), "ranong_raktai", "reg_mode")
os.makedirs(_REG_MODE_DIR, exist_ok=True)


def _set_registration_mode(user_hash: str):
    """ตั้ง registration mode — เขียนไฟล์ใน /tmp เพื่อ share ข้าม workers"""
    filepath = os.path.join(_REG_MODE_DIR, f"{user_hash}.flag")
    with open(filepath, "w") as f:
        f.write(str(time.time()))


def _check_registration_mode(user_hash: str) -> bool:
    """ตรวจ registration mode — อ่านไฟล์จาก /tmp (shared ข้าม workers)"""
    filepath = os.path.join(_REG_MODE_DIR, f"{user_hash}.flag")
    try:
        with open(filepath, "r") as f:
            ts = float(f.read().strip())
        if (time.time() - ts) < _REGISTRATION_TIMEOUT_SECS:
            return True
        else:
            # หมดเวลา → ลบ
            _clear_registration_mode(user_hash)
            return False
    except (FileNotFoundError, ValueError):
        return False


def _clear_registration_mode(user_hash: str):
    """ลบ registration mode flag"""
    filepath = os.path.join(_REG_MODE_DIR, f"{user_hash}.flag")
    try:
        os.remove(filepath)
    except FileNotFoundError:
        pass


# ── Pending Classification: ถามผู้ใช้ผ่าน LINE เมื่อ OCR ล้มเหลว ──────
# เมื่อ classify_with_keywords returns "review" เพราะ OCR อ่านไม่ได้
# ระบบจะเก็บข้อมูลเอกสาร + ส่งข้อความถามผู้ใช้ให้เลือกประเภท
# ผู้ใช้ตอบเลข 1-4 → ระบบย้ายเอกสารไปยัง folder ที่ถูกต้อง
# ใช้ GCS-based state แทน /tmp เพื่อให้ทุก Gunicorn worker เข้าถึงได้
_PENDING_CLASS_GCS_PREFIX = "_state/pending/"
_PENDING_CLASS_TIMEOUT = 300  # 5 นาที


def _set_pending_classification(
    user_hash: str, doc_id: str, gcs_path: str,
    owner_hash: str, line_user_id: str, sha256_hex: str,
):
    """บันทึก pending classification ไปยัง GCS (shared across workers)"""
    data = {
        "timestamp": time.time(),
        "doc_id": doc_id,
        "gcs_path": gcs_path,
        "owner_hash": owner_hash,
        "line_user_id": line_user_id,
        "sha256_hex": sha256_hex,
    }
    try:
        if not storage.is_ready or not hasattr(storage, '_bucket'):
            logger.warning("Storage not ready for pending classification")
            return
        blob_path = f"{_PENDING_CLASS_GCS_PREFIX}{user_hash}.json"
        blob = storage._bucket.blob(blob_path)
        blob.upload_from_string(
            json.dumps(data), content_type="application/json"
        )
    except Exception as e:
        logger.warning(f"Failed to set pending classification: {type(e).__name__}")


def _get_pending_classification(user_hash: str) -> dict | None:
    """ดึง pending classification จาก GCS — คืน None ถ้าไม่มีหรือหมดเวลา"""
    try:
        if not storage.is_ready or not hasattr(storage, '_bucket'):
            return None
        blob_path = f"{_PENDING_CLASS_GCS_PREFIX}{user_hash}.json"
        blob = storage._bucket.blob(blob_path)
        if not blob.exists():
            return None
        content = blob.download_as_text()
        data = json.loads(content)
        # ตรวจว่า expired หรือหมดเวลา
        if data.get("expired"):
            return None
        if (time.time() - data.get("timestamp", 0)) < _PENDING_CLASS_TIMEOUT:
            return data
        else:
            _clear_pending_classification(user_hash)
            return None
    except Exception:
        return None


def _clear_pending_classification(user_hash: str):
    """เคลียร์ pending classification — เขียนทับด้วย expired marker
    (ไม่สามารถลบ blob ได้เนื่องจากไม่มี delete permission)"""
    try:
        if not storage.is_ready or not hasattr(storage, '_bucket'):
            return
        blob_path = f"{_PENDING_CLASS_GCS_PREFIX}{user_hash}.json"
        blob = storage._bucket.blob(blob_path)
        blob.upload_from_string(
            json.dumps({"expired": True, "timestamp": 0}),
            content_type="application/json",
        )
    except Exception:
        pass


# ── โหมดเลือกผู้ป่วย: หมอ/เจ้าหน้าที่ส่งเอกสารแทนผู้ป่วยหลายคน ──────
# workflow จริงของคลินิก: เอกสารคลินิก (ใบแล็บ ใบยา ใบฟอกเลือด)
# ส่งจากบัญชีหมอ/เจ้าหน้าที่ ไม่ใช่บัญชีผู้ป่วย จึงผูก 1 บัญชี = 1 คน
# ไม่ได้ ต้องให้เจ้าหน้าที่ "เลือกผู้ป่วย" ก่อนส่งเอกสารแทน
#
# เจ้าหน้าที่พิมพ์ "ผู้ป่วย 57042" → เอกสารที่ส่งต่อจากนั้นเข้าแฟ้มคนนั้น
# ใช้ GCS-based state เพื่อให้ทุก Gunicorn worker เห็นค่าเดียวกัน
_PATIENT_CTX_GCS_PREFIX = "_state/patient_ctx/"
# 30 นาที — พอสำหรับส่งเอกสารของผู้ป่วยหนึ่งคนเป็นชุด
# แต่สั้นพอที่จะไม่ค้างข้ามคนไข้รายถัดไป
_PATIENT_CTX_TIMEOUT = 1800


def _set_patient_context(
    user_hash: str, patient_folder: str, hn_display: str, patient_name: str,
) -> bool:
    """ตั้งผู้ป่วยปัจจุบันของเจ้าหน้าที่คนนี้"""
    try:
        if not storage.is_ready or not hasattr(storage, "_bucket"):
            logger.warning("Storage not ready for patient context")
            return False
        blob = storage._bucket.blob(
            f"{_PATIENT_CTX_GCS_PREFIX}{user_hash}.json"
        )
        blob.upload_from_string(
            json.dumps({
                "timestamp": time.time(),
                "patient_folder": patient_folder,
                "hn_display": hn_display,
                "patient_name": patient_name,
            }, ensure_ascii=False),
            content_type="application/json",
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to set patient context: {type(e).__name__}")
        return False


def _get_patient_context(user_hash: str) -> dict | None:
    """ดึงผู้ป่วยปัจจุบัน — คืน None ถ้าไม่มีหรือหมดเวลา"""
    try:
        if not storage.is_ready or not hasattr(storage, "_bucket"):
            return None
        blob = storage._bucket.blob(
            f"{_PATIENT_CTX_GCS_PREFIX}{user_hash}.json"
        )
        if not blob.exists():
            return None
        data = json.loads(blob.download_as_text())
        if data.get("cleared") or not data.get("patient_folder"):
            return None
        if (time.time() - data.get("timestamp", 0)) >= _PATIENT_CTX_TIMEOUT:
            return None
        return data
    except Exception:
        return None


def _clear_patient_context(user_hash: str):
    """ล้างผู้ป่วยปัจจุบัน — เขียนทับด้วย marker (ไม่มีสิทธิ์ลบ blob)"""
    try:
        if not storage.is_ready or not hasattr(storage, "_bucket"):
            return
        blob = storage._bucket.blob(
            f"{_PATIENT_CTX_GCS_PREFIX}{user_hash}.json"
        )
        blob.upload_from_string(
            json.dumps({"cleared": True, "timestamp": 0}),
            content_type="application/json",
        )
    except Exception:
        pass


def _parse_move_document(text: str):
    """แยกคำสั่งย้ายเอกสารไปแฟ้มผู้ป่วยที่ถูกต้อง

    รองรับ:
      ย้ายเอกสาร a1b2c3d4 57042
      ย้าย a1b2c3d4 57042
      move a1b2c3d4 57042

    คืน (doc_id_prefix, hn) หรือ None ถ้าไม่ใช่คำสั่งนี้
    """
    import re as _re_mv

    m = _re_mv.match(
        r"^\s*(?:ย้ายเอกสาร|ย้าย|move)\s+"
        r"([0-9a-fA-F]{6,32})\s+"
        r"([A-Za-z0-9\-/]{2,20})\s*$",
        (text or "").strip(),
        _re_mv.IGNORECASE,
    )
    if not m:
        return None
    return (m.group(1), m.group(2))


_CATEGORY_WORDS = {
    "lab": "lab", "แล็บ": "lab", "ผลแล็บ": "lab", "ผลตรวจ": "lab",
    "rx": "rx", "ยา": "rx", "ใบยา": "rx", "ใบสั่งยา": "rx",
    "dialysis": "dialysis", "ฟอกเลือด": "dialysis", "ใบฟอกเลือด": "dialysis",
    "bills": "bills", "bill": "bills", "บิล": "bills", "ใบเสร็จ": "bills",
    "review": "review", "ตรวจสอบ": "review", "รอตรวจ": "review",
}


def _parse_change_category(text: str):
    """แยกคำสั่งเปลี่ยนประเภทเอกสารที่จัดหมวดไว้ผิด

    รองรับ:
      เปลี่ยนประเภท a1b2c3d4 ใบยา
      เปลี่ยนประเภท a1b2c3d4 rx 57042
      recategorize a1b2c3d4 dialysis

    คืน (doc_id_prefix, category, hn_or_empty) หรือ None ถ้าไม่ใช่คำสั่งนี้
    """
    import re as _re_cc

    m = _re_cc.match(
        r"^\s*(?:เปลี่ยนประเภท|เปลี่ยนหมวด|recategorize|recat)\s+"
        r"([0-9a-fA-F]{6,32})\s+"
        r"([^\s]{2,20})"
        r"(?:\s+([A-Za-z0-9\-/]{2,20}))?\s*$",
        (text or "").strip(),
        _re_cc.IGNORECASE,
    )
    if not m:
        return None
    cat = _CATEGORY_WORDS.get(m.group(2).strip().lower())
    if not cat:
        return None
    return (m.group(1), cat, (m.group(3) or "").strip())


def _change_document_category(
    doc_id_prefix: str, new_category: str, hn: str, actor_hash: str,
) -> dict:
    """เปลี่ยนประเภทเอกสารที่ถูกจัดหมวดไว้ผิด

    เหตุผลที่ต้องมี:
      ตัวจำแนกอัตโนมัติจัดหมวดผิดได้ และของเดิมแก้ได้เฉพาะ "ตอนเพิ่ง
      ส่งเข้ามา" ผ่านการตอบเลข 1-4 ตามที่ระบบถาม (pending
      classification) พอเลยช่วงนั้นไปแล้วก็ไม่มีวิธีแก้อีกเลย
      ตรวจ production 26 ต.ค. 2026 พบเอกสาร 2 ใบถูกจัดเป็น lab
      ทั้งที่ไม่มีชื่อรายการตรวจแม้แต่ชนิดเดียว — แก้ไม่ได้ด้วยคำสั่งใด

    หลักที่ยึด:
      ไม่ลบไฟล์ต้นฉบับ — ใช้ GCS server-side copy แล้วชี้ index ไป path
        ใหม่ (ระบบไม่มีสิทธิ์ลบอยู่แล้ว และห้ามลบตามข้อกำหนด)
      ไม่เดาประเภทจากชื่อไฟล์หรือรูปร่างเอกสาร — คนระบุประเภทเท่านั้น
      บันทึก audit ทุกครั้ง พร้อมประเภทเดิมและประเภทใหม่
    """
    if not storage.is_ready:
        return {"error": "ระบบจัดเก็บยังไม่พร้อม"}
    if new_category not in FOLDERS:
        return {"error": f"ไม่รู้จักประเภท '{new_category}'"}

    doc, err = _find_doc_by_prefix(doc_id_prefix)
    if doc is None:
        return {"error": err}

    doc_id = doc.get("document_id", "")
    old_category = doc.get("category", "")
    if old_category == new_category:
        return {
            "status": "unchanged",
            "message": f"เอกสารนี้เป็นประเภท '{new_category}' อยู่แล้ว",
        }

    gcs_path = doc.get("gcs_object_path", "")
    blob_path = (
        gcs_path.split("/", 3)[3]
        if gcs_path.startswith("gs://") and gcs_path.count("/") >= 3
        else gcs_path
    )
    if not blob_path:
        return {"error": "เอกสารนี้ไม่มีไฟล์ใน cloud storage"}

    doc_text = doc.get("extracted_text", "")
    doc_dt = _doc_submitted_date(doc)

    # ── แฟ้มรายผู้ป่วย (เฉพาะเอกสารทางคลินิก) ──
    patient_folder = ""
    patient_label = ""
    if new_category in CLINICAL_CATEGORIES and master_registry is not None:
        if hn:
            patient = master_registry.match_hn(hn)
            if patient is None:
                return {"error": f"ไม่พบ HN {hn} ในทะเบียนผู้ป่วย"}
            patient_folder = patient.folder_name
            patient_label = (
                f"{patient.hn_display} — "
                f"{patient.patient_name or 'ไม่มีชื่อ'}"
            )
        elif doc_text:
            # ไม่ระบุ HN มา — ลองหาจาก HN ในเอกสาร เหมือนเส้นทางอัตโนมัติ
            try:
                for hn_candidate in extract_hn_from_document(doc_text):
                    _p = master_registry.match_hn(hn_candidate)
                    if _p is not None:
                        patient_folder = _p.folder_name
                        patient_label = (
                            f"{_p.hn_display} — "
                            f"{_p.patient_name or 'ไม่มีชื่อ'}"
                        )
                        break
            except Exception as _hn_err:
                logger.warning(
                    "change-category: HN lookup ล้มเหลว: %s",
                    type(_hn_err).__name__,
                )
        if not patient_folder:
            return {"error": (
                f"ประเภท '{new_category}' ต้องเข้าแฟ้มรายผู้ป่วย "
                "แต่หา HN ในเอกสารไม่ได้\n"
                f"กรุณาระบุ HN ต่อท้าย เช่น\n"
                f"  เปลี่ยนประเภท {doc_id[:8]} {new_category} 57042"
            )}

    # ── แฟ้มบริษัท (เฉพาะบิล) ──
    vendor_subfolder = ""
    if new_category == "bills":
        vendor_subfolder = resolve_bill_vendor(doc_text).folder

    filename = blob_path.rsplit("/", 1)[-1]
    new_blob_path = build_document_path(
        FOLDERS.get(new_category, FOLDERS["review"]),
        filename, new_category,
        patient_folder=patient_folder,
        subfolder=vendor_subfolder,
        date=doc_dt,
    )

    # ผลแล็บที่อ่านไว้แล้วจะค้างอยู่ถ้าย้ายออกจากหมวด lab — เตือนให้รู้
    # ไม่ลบให้เอง เพราะห้ามลบข้อมูล และการลบต้องเป็นการตัดสินใจของคน
    stale_lab = False
    if old_category == "lab" and new_category != "lab":
        try:
            stale_lab = storage.get(
                f"{LAB_RESULTS_PREFIX}{doc_id}.json"
            ) is not None
        except Exception:
            stale_lab = False

    try:
        src = storage._bucket.blob(blob_path)
        if not src.exists():
            return {"error": "ไม่พบไฟล์ต้นฉบับใน cloud storage"}
        dest = storage._bucket.blob(new_blob_path)
        if not dest.exists():
            storage._bucket.copy_blob(src, storage._bucket, new_blob_path)

        doc_index.update_category(doc_id, new_category, new_blob_path)

        audit_logger.log("document_category_changed", actor_hash, {
            "doc_id": doc_id[:8],
            "from_category": old_category,
            "to_category": new_category,
            "to_patient_folder": patient_folder,
            "stale_lab_results": stale_lab,
        })
        logger.info(
            "เปลี่ยนประเภทเอกสาร %s: %s → %s",
            doc_id[:8], old_category, new_category,
        )

        cat_names = {
            "bills": "ใบเสร็จ/บิล", "rx": "ใบสั่งยา",
            "lab": "ผลตรวจแล็บ", "dialysis": "เอกสารฟอกเลือด",
            "review": "รอตรวจสอบ",
        }
        lines = [
            "✅ เปลี่ยนประเภทเอกสารเรียบร้อย",
            f"📄 รหัส: {doc_id[:8]}",
            f"📁 {cat_names.get(old_category, old_category)}"
            f" → {cat_names.get(new_category, new_category)}",
        ]
        if patient_label:
            lines.append(f"👤 แฟ้ม: {patient_label}")
        lines.append(f"📂 ที่เก็บ: {new_blob_path.rsplit('/', 1)[0]}")
        lines.append("")
        lines.append("หมายเหตุ: ไฟล์เดิมยังอยู่ ระบบชี้ไปที่แฟ้มใหม่แล้ว")
        if stale_lab:
            lines.append(
                "⚠️ เอกสารนี้มีผลแล็บที่ระบบอ่านไว้แล้ว "
                "ค่าเหล่านั้นยังอยู่ในระบบ กรุณาตรวจทานอีกครั้ง"
            )
        return {"status": "changed", "message": "\n".join(lines)}
    except Exception as e:
        logger.error("เปลี่ยนประเภทเอกสารไม่สำเร็จ: %s", type(e).__name__)
        return {"error": f"เปลี่ยนประเภทไม่สำเร็จ ({type(e).__name__})"}


def _parse_alert_ack(text: str):
    """แยกคำสั่งรับทราบ/ปิด alert วิกฤต

    [CS-2] ข้อความแจ้งเตือนที่ส่งให้แพทย์มีบรรทัด
        ตอบ 'รับทราบ <8 ตัวแรกของ alert_id>' เพื่อยืนยัน
    จึงต้องรองรับรูปแบบนั้นเป็นหลัก

    รองรับ:
      รับทราบ a1b2c3d4   / ack a1b2c3d4        → ("a1b2c3d4", "ack")
      แก้ไขแล้ว a1b2c3d4 / resolve a1b2c3d4    → ("a1b2c3d4", "resolve")

    คืน None ถ้าไม่ใช่คำสั่งนี้ — (tuple, str) ถ้าใช่
    """
    import re as _re_ack

    m = _re_ack.match(
        r"^\s*(รับทราบ|ack|acknowledge|แก้ไขแล้ว|resolve)\s+"
        r"([0-9a-fA-F]{6,32})\s*$",
        (text or "").strip(),
        _re_ack.IGNORECASE,
    )
    if not m:
        return None
    verb = m.group(1).lower()
    action = "resolve" if verb in ("แก้ไขแล้ว", "resolve") else "ack"
    return (m.group(2), action)


def _parse_patient_select(text: str) -> str | None:
    """แยก HN จากคำสั่งเลือกผู้ป่วย

    รองรับ: "ผู้ป่วย 57042", "คนไข้ 57042", "เลือกผู้ป่วย 57042",
            "patient 57042", "ผู้ป่วย57042"
    คืน None ถ้าไม่ใช่คำสั่งนี้
    """
    import re as _re_sel

    m = _re_sel.match(
        r"^\s*(?:เลือก)?(?:ผู้ป่วย|คนไข้|patient)\s*[:=]?\s*"
        r"([A-Za-z0-9\-/]{2,20})\s*$",
        text.strip(),
        _re_sel.IGNORECASE,
    )
    return m.group(1) if m else None


def _reclassify_document(
    pending: dict, new_category: str, line_user_id: str,
):
    """ย้ายเอกสารจาก 06_AI_REVIEW ไป folder ที่ถูกต้องใน GCS.

    ใช้ GCS server-side copy (bucket.copy_blob) — ไม่ต้อง download/upload
    หมายเหตุ: ไม่สามารถลบ source ได้ (ไม่มี delete permission)
    """
    gcs_path = pending.get("gcs_path", "")
    doc_id = pending.get("doc_id", "")

    if not gcs_path or not gcs_path.startswith("gs://"):
        push_message(
            line_user_id,
            "❌ ไม่สามารถย้ายเอกสารได้ — ไม่พบไฟล์ใน cloud storage",
        )
        return

    # Parse GCS path: gs://bucket/06_AI_REVIEW/2026-10/uuid.jpg
    try:
        bucket_name = gcs_path.split("/")[2]
        object_path = "/".join(gcs_path.split("/")[3:])
    except (IndexError, ValueError):
        push_message(line_user_id, "❌ path ไม่ถูกต้อง — ไม่สามารถย้ายได้")
        return

    # Build new path — ใช้ canonical builder เดียวกับ upload ปกติ
    dest_folder = FOLDERS.get(new_category, FOLDERS["review"])
    filename = object_path.rsplit("/", 1)[-1] if "/" in object_path else object_path

    # ดึงข้อความและวันที่ของเอกสารจาก index — ใช้ทั้งหา HN และหาบริษัท
    doc_text = ""
    doc_submitted = None
    try:
        for _d in doc_index._docs:
            if _d.get("document_id") == doc_id:
                doc_text = _d.get("extracted_text", "")
                doc_submitted = _doc_submitted_date(_d)
                break
    except Exception as _idx_err:
        logger.warning(
            "Reclassify: อ่าน index ไม่ได้: %s", type(_idx_err).__name__
        )

    # หา patient folder จาก HN ใน OCR text ของเอกสาร — ใบยา/ใบฟอกเลือด
    # ต้องเข้าแฟ้มรายผู้ป่วยเหมือนผลแล็บ
    patient_folder = ""
    if new_category in CLINICAL_CATEGORIES and master_registry and doc_text:
        try:
            for hn_candidate in extract_hn_from_document(doc_text):
                _folder = master_registry.get_folder_for_hn(hn_candidate)
                if _folder:
                    patient_folder = _folder
                    break
        except Exception as _hn_err:
            logger.warning(
                "Reclassify: HN lookup failed: %s", type(_hn_err).__name__
            )

    # บิลต้องเข้าแฟ้มบริษัทเหมือนเส้นทางอัตโนมัติ
    #
    # เดิมเส้นทางนี้ไม่ส่ง subfolder เลย บิลที่ผู้ดูแลจัดประเภทด้วยมือจึง
    # ลงที่ 02_BILLS_RECEIPTS/YYYY-MM/ ตรงๆ ไม่มีแฟ้มบริษัท ต่างจาก
    # เส้นทางอัตโนมัติ — เป็นเหตุให้โครงสร้างแฟ้มไม่ตรงกันสองแบบ
    vendor_subfolder = ""
    if new_category == "bills":
        vendor_subfolder = resolve_bill_vendor(doc_text).folder

    new_path = build_document_path(
        dest_folder, filename, new_category,
        patient_folder=patient_folder,
        subfolder=vendor_subfolder,
        date=doc_submitted,
    )

    try:
        from google.cloud import storage as gcs_storage
        client = gcs_storage.Client()
        bucket = client.bucket(bucket_name)
        src_blob = bucket.blob(object_path)
        if not src_blob.exists():
            push_message(
                line_user_id,
                "❌ ไม่พบไฟล์ต้นฉบับใน cloud storage\n"
                "เอกสารอาจถูกย้ายแล้ว กรุณาส่งใหม่",
            )
            return
        # Server-side copy
        bucket.copy_blob(src_blob, bucket, new_path)
        logger.info(
            f"Reclassify: copied doc {doc_id[:8]} → {new_category}"
        )

        # ── อัปเดต document index ──
        try:
            doc_index.update_category(doc_id, new_category, new_path)
        except Exception:
            pass  # Index update ล้มเหลว — ไม่ร้ายแรง

        cat_names = {
            "bills": "ใบเสร็จ/บิล",
            "rx": "ใบสั่งยา",
            "lab": "ผลตรวจแล็บ",
            "dialysis": "เอกสารฟอกเลือด",
        }
        cat_name = cat_names.get(new_category, new_category)
        push_message(
            line_user_id,
            f"✅ จำแนกเอกสารสำเร็จ\n"
            f"📁 ประเภท: {cat_name}\n"
            f"📂 จัดเก็บใน: {new_path.rsplit('/', 1)[0]}",
        )
    except Exception as e:
        logger.error(
            f"Reclassify failed: {type(e).__name__} (doc={doc_id[:8]})"
        )
        push_message(
            line_user_id,
            "❌ ย้ายเอกสารไม่สำเร็จ — กรุณาส่งเอกสารใหม่อีกครั้ง",
        )


def _find_doc_by_prefix(doc_id_prefix: str):
    """หาเอกสารจากคำนำหน้า document_id

    คืน (doc, error_message) — doc เป็น None เมื่อหาไม่ได้
    ปฏิเสธเมื่อกำกวม ไม่เดา เพราะปลายทางคือการย้ายเวชระเบียน
    """
    prefix = (doc_id_prefix or "").strip().lower()
    if len(prefix) < 6:
        return None, "รหัสเอกสารสั้นเกินไป (ต้องอย่างน้อย 6 ตัว)"
    try:
        doc_index._ensure_fresh()
    except Exception:
        pass
    matches = [
        d for d in doc_index._docs
        if d.get("document_id", "").lower().startswith(prefix)
    ]
    if not matches:
        return None, f"ไม่พบเอกสารรหัส {prefix}"
    if len(matches) > 1:
        return None, (
            f"รหัส {prefix} ตรงกับ {len(matches)} เอกสาร — "
            "กรุณาระบุให้ยาวขึ้น"
        )
    return matches[0], ""


def _move_document_to_patient(
    doc_id_prefix: str, hn: str, actor_hash: str,
) -> dict:
    """ย้ายเอกสารไปแฟ้มของผู้ป่วยที่ถูกต้อง

    เหตุผลที่ต้องมี:
      workflow จริงของคลินิกคือเจ้าหน้าที่เลือกผู้ป่วยก่อนส่งเอกสาร
      ถ้าลืมเปลี่ยนผู้ป่วย เอกสารชุดถัดไปจะเข้าแฟ้มคนก่อนทั้งชุด
      ก่อนหน้านี้ "ไม่มีวิธีแก้เลย" ต้องส่งเอกสารใหม่อย่างเดียว

    วิธีทำงาน: ใช้ GCS server-side copy เหมือน retroactive routing
      - ไม่ลบไฟล์ต้นฉบับ (ระบบไม่มีสิทธิ์ลบอยู่แล้ว) ต้นฉบับยังอยู่ครบ
      - อัปเดต index ให้ชี้ไปที่ path ใหม่
      - บันทึก audit ทุกครั้ง
    """
    if master_registry is None or not storage.is_ready:
        return {"error": "ระบบจัดเก็บยังไม่พร้อม"}

    doc, err = _find_doc_by_prefix(doc_id_prefix)
    if doc is None:
        return {"error": err}

    patient = master_registry.match_hn(hn)
    if patient is None:
        return {"error": f"ไม่พบ HN {hn} ในทะเบียนผู้ป่วย"}

    cat = doc.get("category", "")
    if cat not in CLINICAL_CATEGORIES:
        return {"error": (
            f"เอกสารนี้เป็นประเภท '{cat}' ซึ่งไม่ได้จัดเก็บตามรายผู้ป่วย"
        )}

    gcs_path = doc.get("gcs_object_path", "")
    blob_path = (
        gcs_path.split("/", 3)[3]
        if gcs_path.startswith("gs://") and gcs_path.count("/") >= 3
        else gcs_path
    )
    if not blob_path:
        return {"error": "เอกสารนี้ไม่มีไฟล์ใน cloud storage"}

    # ใช้วันที่ของเอกสาร เพื่อให้ยังอยู่ไตรมาส/เดือนเดิม ไม่ย้ายข้ามช่วงเวลา
    doc_dt = _doc_submitted_date(doc)

    filename = blob_path.rsplit("/", 1)[-1]
    new_blob_path = build_document_path(
        FOLDERS.get(cat, FOLDERS["review"]), filename, cat,
        patient_folder=patient.folder_name, date=doc_dt,
    )

    if new_blob_path == blob_path:
        return {
            "status": "unchanged",
            "message": (
                f"เอกสารนี้อยู่ในแฟ้มของ {patient.hn_display} อยู่แล้ว"
            ),
        }

    try:
        src = storage._bucket.blob(blob_path)
        if not src.exists():
            return {"error": "ไม่พบไฟล์ต้นฉบับใน cloud storage"}
        dest = storage._bucket.blob(new_blob_path)
        if not dest.exists():
            storage._bucket.copy_blob(src, storage._bucket, new_blob_path)

        doc_index.update_category(
            doc.get("document_id", ""), cat, new_blob_path,
        )

        audit_logger.log("document_refiled", actor_hash, {
            "doc_id": doc.get("document_id", "")[:8],
            "category": cat,
            "to_patient_folder": patient.folder_name,
        })
        logger.info(
            "Re-filed document %s → patient folder (cat=%s)",
            doc.get("document_id", "")[:8], cat,
        )
        return {
            "status": "moved",
            "message": (
                f"✅ ย้ายเอกสารเรียบร้อย\n"
                f"📄 รหัส: {doc.get('document_id', '')[:8]}\n"
                f"👤 แฟ้มใหม่: {patient.hn_display} — "
                f"{patient.patient_name or 'ไม่มีชื่อ'}\n\n"
                f"หมายเหตุ: ไฟล์เดิมยังอยู่ ระบบชี้ไปที่แฟ้มใหม่แล้ว"
            ),
        }
    except Exception as e:
        logger.error("Re-file failed: %s", type(e).__name__)
        return {"error": f"ย้ายเอกสารไม่สำเร็จ ({type(e).__name__})"}


# /tmp metadata structure (per user_hash):
# {
#   "pages": [{"ext": ".jpg", "sha256": "...", "doc_id": "...", "filename": "..."}, ...],
#   "line_user_id": str,
#   "last_image_ts": float (time.time())
# }
# Page image data: /tmp/ranong_raktai/multipage/{user_hash}_p{N}.dat


def _multipage_flush(user_hash: str):
    """ประมวลผลรูปภาพทั้งหมดในบัฟเฟอร์ของผู้ใช้เมื่อหมดเวลารอ.

    [FIX] อ่านข้อมูลจาก /tmp filesystem แทน in-memory dict
    เพื่อให้ทุก Gunicorn worker เข้าถึงได้

    ถ้ามีรูปเดียว → ประมวลผลปกติ (เหมือนไม่มีบัฟเฟอร์)
    ถ้ามีหลายรูป → รวมเป็นเอกสารเดียว, OCR ทุกหน้า, ต่อข้อความ
    """
    # Acquire cross-worker lock
    lock_fd = None
    try:
        lock_fd = _mp_acquire_lock(user_hash)
    except Exception as lock_err:
        logger.warning(
            f"Multi-page flush: lock acquisition failed: "
            f"{type(lock_err).__name__} (user={user_hash[:8]}…) — "
            f"proceeding without cross-worker lock"
        )

    try:
        meta = _mp_read_meta(user_hash)
        if not meta or not meta.get("pages"):
            return

        # ตรวจว่ามีรูปใหม่เข้ามาหลัง timer เริ่ม — ถ้ามี, เริ่ม timer ใหม่
        last_ts = meta.get("last_image_ts", 0)
        elapsed = time.time() - last_ts
        if elapsed < _MULTIPAGE_WINDOW_SECS - 1:
            # ยังไม่หมดเวลา — มีรูปใหม่เข้ามาจาก worker อื่น
            remaining = _MULTIPAGE_WINDOW_SECS - elapsed + 0.5
            logger.info(
                f"Multi-page flush deferred: {remaining:.0f}s remaining "
                f"(user={user_hash[:8]}…)"
            )
            new_timer = threading.Timer(remaining, _multipage_flush, args=[user_hash])
            new_timer.daemon = True
            with _multipage_lock:
                _multipage_timers[user_hash] = new_timer
            new_timer.start()
            return

        page_entries = meta["pages"]
        line_user_id = meta.get("line_user_id", "")
        page_count = len(page_entries)
        # เวลาส่งหน้าแรก — เดือนของเอกสารที่รวมเสร็จต้องอ้างเวลานี้
        submitted_at_ms = meta.get("submitted_at_ms", 0)

        # อ่านข้อมูลภาพจาก /tmp files
        pages_data = []
        for i, entry in enumerate(page_entries, 1):
            page_file = _mp_page_path(user_hash, i)
            try:
                with open(page_file, "rb") as f:
                    page_data = f.read()
                pages_data.append((
                    page_data,
                    entry["ext"],
                    entry["sha256"],
                    entry["doc_id"],
                    entry["filename"],
                ))
            except FileNotFoundError:
                logger.warning(
                    f"Multi-page buffer: page {i} file missing "
                    f"(user={user_hash[:8]}…)"
                )

        if not pages_data:
            logger.warning(
                f"Multi-page buffer: no page files found "
                f"(user={user_hash[:8]}…)"
            )
            _mp_clear(user_hash, page_count)
            return

        # ลบไฟล์ buffer ก่อนประมวลผล
        _mp_clear(user_hash, page_count)
        with _multipage_lock:
            _multipage_timers.pop(user_hash, None)

    finally:
        if lock_fd:
            _mp_release_lock(lock_fd)

    # จากนี้ทำงานนอก lock — ประมวลผลข้อมูลที่อ่านมาแล้ว
    actual_count = len(pages_data)

    if actual_count == 1:
        # รูปเดียว — ประมวลผลปกติ
        data, ext, sha256_hex, doc_id, filename = pages_data[0]
        filepath = save_to_inbox(data, filename)
        process_file_async(
            filepath, None, doc_id=doc_id, sha256_hex=sha256_hex,
            owner_hash=user_hash, line_user_id=line_user_id,
            submitted_at_ms=submitted_at_ms,
        )
        return

    # หลายรูป — รวมเป็นเอกสารเดียว
    logger.info(
        f"Multi-page merge: user={user_hash[:8]}…, pages={actual_count}"
    )

    # แจ้งผู้ใช้ว่าเริ่มประมวลผลรวม
    if line_user_id:
        push_message(
            line_user_id,
            f"📄 กำลังรวมผลแล็บ {actual_count} หน้าเป็นเอกสารเดียว…",
        )

    # ใช้ doc_id ของหน้าแรกเป็นตัวแทนเอกสารรวม
    primary_doc_id = pages_data[0][3]

    # รวม SHA256 ของทุกหน้าเป็น combined hash
    combined_sha_input = "|".join(p[2] for p in pages_data)
    combined_sha256 = hashlib.sha256(
        combined_sha_input.encode("utf-8")
    ).hexdigest()

    # บันทึกหน้าแรกลง inbox เป็นไฟล์หลัก
    primary_data = pages_data[0][0]
    primary_filename = pages_data[0][4]
    primary_filepath = save_to_inbox(primary_data, primary_filename)

    # OCR ทุกหน้าและรวมข้อความ
    combined_texts = []
    for i, (page_data, page_ext, _, _, _) in enumerate(pages_data, 1):
        page_text = ""
        if HAS_TESSERACT and HAS_PIL:
            try:
                page_text = _ocr_from_bytes(page_data, "image")
            except Exception as ocr_err:
                logger.warning(
                    f"Multi-page OCR ล้มเหลว หน้า {i}: "
                    f"{type(ocr_err).__name__}"
                )
        if page_text.strip():
            combined_texts.append(f"[หน้า {i}/{actual_count}]\n{page_text.strip()}")
        else:
            combined_texts.append(f"[หน้า {i}/{actual_count}]\n(ไม่สามารถอ่านข้อความได้)")

    merged_text = "\n\n".join(combined_texts)
    logger.info(
        f"Multi-page OCR สำเร็จ: pages={actual_count}, "
        f"total_chars={len(merged_text)}"
    )

    # ประมวลผลด้วยข้อความรวม — เรียก process_file_async
    process_file_async(
        primary_filepath,
        None,  # reply_token หมดอายุแล้ว — ใช้ push_message แทน
        doc_id=primary_doc_id,
        sha256_hex=combined_sha256,
        owner_hash=user_hash,
        line_user_id=line_user_id,
        merged_ocr_text=merged_text,
        merged_page_count=actual_count,
        submitted_at_ms=submitted_at_ms,
    )


def _process_registration_image(
    data: bytes,
    ext: str,
    user_hash: str,
    line_user_id: str,
):
    """ประมวลผลรูปรายชื่อผู้ป่วย → OCR → ลงทะเบียน

    ทำงานในเธรดแยก — ใช้ push_message แทน reply_token
    """
    # ─── Safety: ตรวจ line_user_id ก่อนเริ่ม ──────────────
    if not line_user_id:
        logger.error("[REG-IMG] line_user_id ว่าง — ไม่สามารถแจ้งผลกลับได้")
        return

    tmp_path: str | None = None
    try:
        logger.info(
            f"[REG-IMG] เริ่มประมวลผลรูปลงทะเบียน "
            f"({len(data)} bytes, ext={ext}, uid_ok={bool(line_user_id)})"
        )
        # บันทึกรูปชั่วคราว
        tmp_dir = os.path.join(tempfile.gettempdir(), "ranong_raktai", "reg_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, f"reg_{uuid.uuid4().hex[:8]}{ext}")
        with open(tmp_path, "wb") as f:
            f.write(data)
        logger.info(f"[REG-IMG] บันทึกรูปชั่วคราว: {tmp_path}")

        # ── OCR (with pytesseract timeout) ──────────────
        logger.info("[REG-IMG] เริ่ม OCR...")
        ocr_text = extract_text_with_ocr(tmp_path, timeout_sec=60)
        logger.info(f"[REG-IMG] OCR เสร็จ: {len(ocr_text or '')} chars")

        # ลบไฟล์ชั่วคราว
        try:
            if tmp_path:
                os.remove(tmp_path)
                tmp_path = None
        except OSError:
            pass

        # ── Garbage detection: ตรวจว่า OCR ได้ข้อความจริงหรือไม่ ──
        import re as _re
        _ocr_clean = (ocr_text or "").strip()
        _thai_count = len(_re.findall(r'[฀-๿]', _ocr_clean))
        _digit_groups = _re.findall(r'\d{5,}', _ocr_clean)
        _is_garbage = (
            len(_ocr_clean) < 10
            or (_thai_count < 3 and len(_digit_groups) < 1)
        )

        # ── Debug: เก็บ OCR text ไว้ใน memory (ไม่ลง log) ──
        from patient_master_registry import _parse_patient_line
        _debug_lines = []
        for _dl in _ocr_clean.split("\n"):
            _dl_stripped = _dl.strip()
            if _dl_stripped:
                _result = _parse_patient_line(_dl_stripped)
                _debug_lines.append({
                    "line": _dl_stripped[:120],
                    "parsed": _result,
                })
        _ocr_debug_store.clear()
        _ocr_debug_store["timestamp"] = datetime.now().isoformat()
        _ocr_debug_store["ocr_char_count"] = len(_ocr_clean)
        _ocr_debug_store["ocr_line_count"] = len(_ocr_clean.split("\n"))
        _ocr_debug_store["ocr_text"] = _ocr_clean[:3000]
        _ocr_debug_store["thai_chars"] = _thai_count
        _ocr_debug_store["digit_groups_5plus"] = _digit_groups[:10]
        _ocr_debug_store["is_garbage"] = _is_garbage
        _ocr_debug_store["line_parse_detail"] = _debug_lines[:30]

        if _is_garbage:
            logger.warning(
                f"[REG-IMG] OCR garbage detected: "
                f"{len(_ocr_clean)}c, thai={_thai_count}, nums={len(_digit_groups)}"
            )
            _ocr_debug_store["result"] = "garbage_detected"
            push_message(
                line_user_id,
                "📸 ระบบอ่านรูปไม่ออก\n"
                "(รูปถ่ายหน้าจอมีลายมัวเร)\n\n"
                "✅ วิธีที่ดีที่สุด: พิมพ์รายชื่อ\n"
                "ส่งข้อความรูปแบบนี้:\n\n"
                "สมชาย ใจดี | 00012345 | 63001\n"
                "สมหญิง รักดี | 00067890 | 63002\n\n"
                "หรือ:\n"
                "63001 สมชาย ใจดี\n"
                "63002/00067890 สมหญิง รักดี\n\n"
                "📋 วิธีก็อปจากคอม:\n"
                "เปิดตาราง → เลือกข้อมูล → Copy\n"
                "→ วางใน LINE แล้วส่ง",
            )
            return

        logger.info(f"[REG-IMG] OCR {len(_ocr_clean)} chars, thai={_thai_count}, "
                     f"nums={len(_digit_groups)}, lines={len(_debug_lines)}")

        # แยก HN + ชื่อจาก OCR text
        patients_data = parse_patient_list_ocr(ocr_text)
        _ocr_debug_store["parsed_patient_count"] = len(patients_data)
        logger.info(f"[REG-IMG] parsed {len(patients_data)} patients")

        if not patients_data:
            _ocr_debug_store["result"] = "no_patients_parsed"
            push_message(
                line_user_id,
                "❌ ไม่พบรายชื่อผู้ป่วยในรูป\n\n"
                "✅ แนะนำ: พิมพ์รายชื่อแทน\n"
                "รูปแบบ: ชื่อ | HN รพ | HN คลินิก\n\n"
                "ตัวอย่าง:\n"
                "สมชาย ใจดี | 00012345 | 63001\n"
                "สมหญิง รักดี | 00067890 | 63002",
            )
            return

        # ลงทะเบียน batch
        logger.info("[REG-IMG] เริ่ม register_batch...")
        new_count, updated_count, messages = master_registry.register_batch(
            patients_data, registered_by="admin_ocr"
        )
        logger.info(f"[REG-IMG] register_batch เสร็จ: new={new_count}, updated={updated_count}")

        # แจ้งผล
        report_lines = [
            f"📋 ลงทะเบียนรายชื่อเสร็จสิ้น",
            f"",
            f"👤 พบ {len(patients_data)} รายการจากรูป",
            f"✅ ใหม่: {new_count} คน",
            f"🔄 อัปเดต: {updated_count} คน",
        ]
        # ถ้าพบน้อยกว่า 5 คน — แนะนำพิมพ์ข้อความแทน
        if len(patients_data) < 5:
            report_lines.append("")
            report_lines.append("⚠️ พบน้อย? ลองพิมพ์รายชื่อแทนรูป:")
            report_lines.append("ชื่อ | HN รพ | HN คลินิก")
            report_lines.append("เช่น: สมชาย ใจดี | 00012345 | 63001")
        if messages:
            report_lines.append("")
            report_lines.extend(messages)

        # แสดงรายชื่อที่ลงทะเบียน (ไม่เกิน 15 รายการ)
        report_lines.append("")
        report_lines.append("รายชื่อ:")
        for i, pd in enumerate(patients_data[:15], 1):
            hn = pd.get("clinic_hn", "?")
            hosp = pd.get("hospital_hn", "")
            name = pd.get("patient_name", "-")
            hn_disp = f"{hn}/{hosp}" if hosp else hn
            report_lines.append(f"  {i}. {hn_disp} — {name}")
        if len(patients_data) > 15:
            report_lines.append(f"  ...และอีก {len(patients_data)-15} คน")

        logger.info("[REG-IMG] กำลังส่ง push_message ผลลงทะเบียน...")
        sent = push_message(line_user_id, "\n".join(report_lines))
        logger.info(
            f"[REG-IMG] Registration done: {new_count} new, {updated_count} updated, "
            f"push_sent={sent}"
        )
    except Exception as e:
        logger.error(f"[REG-IMG] Processing failed: {type(e).__name__}: {e}")
        try:
            push_message(
                line_user_id,
                "❌ เกิดข้อผิดพลาดในการลงทะเบียน\n"
                "กรุณาลองใหม่ หรือแจ้งผู้ดูแลระบบ",
            )
        except Exception as e2:
            logger.error(f"[REG-IMG] push_message ส่งแจ้ง error ไม่ได้: {e2}")
    finally:
        # cleanup temp file ถ้ายังอยู่
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        # คืนหน่วยความจำ — สำคัญมากบน Render 512MB
        import gc
        gc.collect()


def _handle_incoming_document(
    data: bytes,
    ext: str,
    user_hash: str,
    reply_token: str | None,
    line_user_id: str = "",
    submitted_at_ms: int = 0,
):
    """ขั้นตอนร่วมของการรับเอกสารจาก LINE (ทั้งภาพและไฟล์).

    ชั้นที่ 3 ของการกันไฟล์ซ้ำ — ตรวจ sha256 ของเนื้อหา
    ต้องทำหลังดาวน์โหลด (ต้องมีข้อมูลถึงจะ hash ได้) แต่ก่อนบันทึกลง inbox
    จึงไม่มี temp file ค้างเมื่อพบว่าซ้ำ

    สำหรับภาพ (jpg/jpeg/png): เข้า multi-page buffer เพื่อรอรวมหลายหน้า
    สำหรับไฟล์อื่น (pdf/txt): ประมวลผลทันที
    """
    sha256_hex = content_sha256(data)

    if not dedup_guard.is_new_content(user_hash, sha256_hex):
        logger.info(f"ข้ามเอกสารเนื้อหาซ้ำ (sha={sha256_hex[:8]}…)")
        _safe_reply(
            reply_token, line_user_id,
            "ได้รับเอกสารนี้แล้วเมื่อสักครู่\nไม่ต้องส่งซ้ำครับ",
        )
        return

    doc_id, filename = build_storage_filename(ext)

    # ── Multi-page buffer: เฉพาะภาพเท่านั้น ──
    # [FIX] ใช้ /tmp filesystem + file lock แทน in-memory dict
    # เพื่อให้ทุก Gunicorn worker เข้าถึงบัฟเฟอร์เดียวกัน
    if ext in (".jpg", ".jpeg", ".png"):
        lock_fd = None
        try:
            lock_fd = _mp_acquire_lock(user_hash)
        except Exception as lock_err:
            logger.warning(
                f"Multi-page buffer: lock acquisition failed: "
                f"{type(lock_err).__name__} (user={user_hash[:8]}…) — "
                f"proceeding without cross-worker lock"
            )

        try:
            meta = _mp_read_meta(user_hash)
            if meta is not None and meta.get("pages"):
                # มีบัฟเฟอร์อยู่แล้ว — เพิ่มหน้าใหม่
                page_num = len(meta["pages"]) + 1

                if page_num > _MULTIPAGE_MAX_PAGES:
                    # เกินจำนวนสูงสุด — ปฏิเสธ
                    _safe_reply(
                        reply_token, line_user_id,
                        f"⚠️ รองรับสูงสุด {_MULTIPAGE_MAX_PAGES} หน้าต่อครั้ง\n"
                        "กรุณาส่งผลแล็บชุดใหม่หลังจากชุดนี้ประมวลผลเสร็จ",
                    )
                    # อัปเดต timestamp เพื่อให้ timer defer ได้
                    meta["last_image_ts"] = time.time()
                    _mp_write_meta(user_hash, meta)
                    # เริ่ม timer ใหม่ (ใน worker นี้)
                    with _multipage_lock:
                        old_timer = _multipage_timers.pop(user_hash, None)
                        if old_timer:
                            old_timer.cancel()
                    new_timer = threading.Timer(
                        _MULTIPAGE_WINDOW_SECS,
                        _multipage_flush,
                        args=[user_hash],
                    )
                    new_timer.daemon = True
                    with _multipage_lock:
                        _multipage_timers[user_hash] = new_timer
                    new_timer.start()
                    return

                # บันทึกข้อมูลภาพลง /tmp
                page_file = _mp_page_path(user_hash, page_num)
                with open(page_file, "wb") as f:
                    f.write(data)

                meta["pages"].append({
                    "ext": ext,
                    "sha256": sha256_hex,
                    "doc_id": doc_id,
                    "filename": filename,
                })
                if line_user_id:
                    meta["line_user_id"] = line_user_id
                meta["last_image_ts"] = time.time()
                _mp_write_meta(user_hash, meta)

                # ยกเลิก timer เดิม แล้วเริ่มใหม่
                with _multipage_lock:
                    old_timer = _multipage_timers.pop(user_hash, None)
                    if old_timer:
                        old_timer.cancel()
                new_timer = threading.Timer(
                    _MULTIPAGE_WINDOW_SECS,
                    _multipage_flush,
                    args=[user_hash],
                )
                new_timer.daemon = True
                with _multipage_lock:
                    _multipage_timers[user_hash] = new_timer
                new_timer.start()

                _safe_reply(
                    reply_token, line_user_id,
                    f"📄 ได้รับหน้าที่ {page_num} แล้ว\n"
                    f"ส่งหน้าถัดไปได้เลย (รอ {_MULTIPAGE_WINDOW_SECS} วินาทีแล้วจะรวมอัตโนมัติ)",
                )
                return
            else:
                # สร้างบัฟเฟอร์ใหม่ — หน้าแรก
                page_file = _mp_page_path(user_hash, 1)
                with open(page_file, "wb") as f:
                    f.write(data)

                new_meta = {
                    "pages": [{
                        "ext": ext,
                        "sha256": sha256_hex,
                        "doc_id": doc_id,
                        "filename": filename,
                    }],
                    "line_user_id": line_user_id,
                    "last_image_ts": time.time(),
                    # เวลาที่ผู้ส่งส่ง "หน้าแรก" เข้ามา — ใช้ตัดสินเดือน
                    # ของเอกสารที่รวมเสร็จ ไม่ใช่เวลาที่รวมสำเร็จ
                    "submitted_at_ms": submitted_at_ms,
                }
                _mp_write_meta(user_hash, new_meta)

                new_timer = threading.Timer(
                    _MULTIPAGE_WINDOW_SECS,
                    _multipage_flush,
                    args=[user_hash],
                )
                new_timer.daemon = True
                with _multipage_lock:
                    _multipage_timers[user_hash] = new_timer
                new_timer.start()

                _safe_reply(
                    reply_token, line_user_id,
                    "📄 ได้รับหน้าที่ 1 แล้ว\n"
                    f"ถ้ามีหลายหน้า ส่งต่อได้เลย (รอ {_MULTIPAGE_WINDOW_SECS} วินาทีแล้วจะรวมอัตโนมัติ)\n"
                    "ถ้ามีหน้าเดียว ไม่ต้องทำอะไร — จะประมวลผลอัตโนมัติ",
                )
                return
        finally:
            if lock_fd:
                _mp_release_lock(lock_fd)

    # ── ไฟล์อื่น (PDF, TXT) — ประมวลผลทันทีเหมือนเดิม ──
    filepath = save_to_inbox(data, filename)
    process_file_async(
        filepath, reply_token, doc_id=doc_id, sha256_hex=sha256_hex,
        owner_hash=user_hash, line_user_id=line_user_id,
        submitted_at_ms=submitted_at_ms,
    )


# ─── Async-safe message helper ────────────────────────────
# reply_token หมดอายุเร็ว (~30 วินาที) — ถ้าอยู่ใน background thread
# ให้ fallback ไป push_message แทน
# ─────────────────────────────────────────────────────────────

def _safe_reply(reply_token: str | None, line_user_id: str, text: str):
    """ตอบกลับผู้ใช้ — ลอง reply ก่อน ถ้าไม่ได้ให้ใช้ push."""
    if reply_token:
        reply_message(reply_token, text)
    elif line_user_id:
        push_message(line_user_id, text)
    else:
        logger.warning("_safe_reply: ไม่มีทั้ง reply_token และ line_user_id — ส่งข้อความไม่ได้")


def _safe_reply_flex(reply_token: str | None, line_user_id: str, message: dict):
    """ตอบกลับผู้ใช้ด้วย Flex Message — ลอง reply ก่อน ถ้าไม่ได้ให้ใช้ push text."""
    if reply_token:
        reply_line_message(reply_token, message)
    elif line_user_id:
        # Push API ส่ง Flex ได้เช่นกัน
        _push_flex_message(line_user_id, message)
    else:
        logger.warning("_safe_reply_flex: ไม่มีทั้ง reply_token และ line_user_id")


def _push_flex_message(user_id: str, message: dict) -> bool:
    """Push a Flex/non-text message to a LINE user."""
    if not LINE_CHANNEL_ACCESS_TOKEN or not user_id:
        return False
    url = "https://api.line.me/v2/bot/message/push"
    request_body = json.dumps(
        {"to": user_id, "messages": [message]},
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
        return True
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "-")
        logger.error(f"[PUSH-FLEX] ไม่สำเร็จ: {type(e).__name__} (status={status})")
        return False


# ─── Async webhook event processors ──────────────────────
# เหล่านี้ทำงานใน background thread หลังจาก webhook return 200 แล้ว
# ใช้ push_message แทน reply_message เพราะ reply_token หมดอายุ
# ─────────────────────────────────────────────────────────────

def _async_process_image(
    msg_id: str, user_hash: str, line_user_id: str,
    submitted_at_ms: int = 0,
):
    """Background thread: ดาวน์โหลดภาพจาก LINE แล้วส่งเข้า multi-page buffer."""
    try:
        ext = get_content_type_ext(msg_id)
        if ext not in ALLOWED_EXTENSIONS:
            ext = ".jpg"
        logger.info(f"[ASYNC-IMG] ext={ext}, downloading content...")

        data = download_line_content(msg_id)
        if not data:
            logger.error(f"[ASYNC-IMG] download FAILED for msg_id={msg_id}")
            push_message(line_user_id, "ไม่สามารถดาวน์โหลดภาพได้ กรุณาลองส่งใหม่")
            return
        logger.info(f"[ASYNC-IMG] downloaded {len(data)} bytes")

        # ตรวจ Registration Mode
        reg_flag = _check_registration_mode(user_hash)
        if reg_flag:
            _clear_registration_mode(user_hash)
            _process_registration_image(data, ext, user_hash, line_user_id)
            return

        # reply_token=None เพราะหมดอายุแล้ว — _handle_incoming_document จะใช้ push_message
        _handle_incoming_document(
            data, ext, user_hash, None, line_user_id,
            submitted_at_ms=submitted_at_ms,
        )
    except Exception as e:
        logger.error(f"[ASYNC-IMG] unexpected error: {type(e).__name__}: {e}")
        push_message(line_user_id, "เกิดข้อผิดพลาดในการประมวลผลภาพ กรุณาลองส่งใหม่")


def _async_process_file(
    msg_id: str, file_ext: str, user_hash: str, line_user_id: str,
    submitted_at_ms: int = 0,
):
    """Background thread: ดาวน์โหลดไฟล์จาก LINE แล้วประมวลผล."""
    try:
        data = download_line_content(msg_id)
        if not data:
            push_message(line_user_id, "ไม่สามารถดาวน์โหลดไฟล์ได้ กรุณาลองส่งใหม่")
            return

        # reply_token=None — _handle_incoming_document จะใช้ push_message
        _handle_incoming_document(
            data, file_ext, user_hash, None, line_user_id,
            submitted_at_ms=submitted_at_ms,
        )
    except Exception as e:
        logger.error(f"[ASYNC-FILE] unexpected error: {type(e).__name__}: {e}")
        push_message(line_user_id, "เกิดข้อผิดพลาดในการประมวลผลไฟล์ กรุณาลองส่งใหม่")


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

    # ─── DEBUG: log สรุป event types ที่เข้ามาทั้งหมด ──
    for _i, _ev in enumerate(events):
        _et = _ev.get("type", "?")
        _mt = _ev.get("message", {}).get("type", "-") if _et == "message" else "-"
        _mid = _ev.get("message", {}).get("id", "-") if _et == "message" else "-"
        logger.info(f"[DEBUG] event[{_i}] type={_et} msg_type={_mt} msg_id={_mid}")

    for event in events:
        event_type = event.get("type")
        reply_token = event.get("replyToken")

        # ─── ชั้นที่ 1: dedup ด้วย webhookEventId ────────────────
        # LINE อาจส่ง event เดียวกันซ้ำจาก retry หรือปัญหาเครือข่าย
        # event ที่ส่งซ้ำมี webhookEventId เดิม ต่างแค่ isRedelivery
        # ต้องตรวจก่อน rate limit เพื่อไม่ให้ retry กิน quota ของผู้ป่วย
        # เวลาที่ผู้ส่งส่งข้อความเข้ามา (มิลลิวินาที จาก LINE)
        # ใช้ตัดสินว่าเอกสารอยู่เดือนใด — ต่างจากเวลาที่เซิร์ฟเวอร์
        # ประมวลผล ซึ่งเคลื่อนได้จาก retry และการรวมเอกสารหลายหน้า
        event_ts_ms = event.get("timestamp", 0)

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

                # [FIX] Async processing — ดาวน์โหลดและประมวลผลใน background thread
                # เพื่อให้ webhook return 200 ให้ LINE ทันก่อน timeout
                reply_message(reply_token, "📷 ได้รับภาพแล้ว กำลังประมวลผล...")
                threading.Thread(
                    target=_async_process_image,
                    args=(msg_id, user_hash, _line_user_id, event_ts_ms),
                    daemon=True,
                ).start()
                continue

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

                # [FIX] Async processing — ดาวน์โหลดและประมวลผลใน background thread
                reply_message(reply_token, "📄 ได้รับไฟล์แล้ว กำลังประมวลผล...")
                threading.Thread(
                    target=_async_process_file,
                    args=(msg_id, file_ext, user_hash, _line_user_id,
                          event_ts_ms),
                    daemon=True,
                ).start()
                continue

            elif msg_type == "text":
                text = msg.get("text", "")
                logger.info(f"รับข้อความ: {len(text)} chars")

                # ── ตรวจ Registration Mode สำหรับข้อความพิมพ์ (file-based) ──
                _in_registration = _check_registration_mode(user_hash)

                if _in_registration and text.strip() not in (
                    "ยกเลิก", "cancel", "ลงทะเบียนรายชื่อ", "ลงทะเบียน",
                    "register", "register patients",
                    "เพิ่มผู้ป่วย", "เพิ่มชื่อ",
                ):
                    # ── ข้อความพิมพ์ในโหมดลงทะเบียน → parse เป็นข้อมูลผู้ป่วย ──
                    _clear_registration_mode(user_hash)
                    patients_data = parse_patient_text_input(text)
                    if patients_data:
                        new_count, updated_count, messages = master_registry.register_batch(
                            patients_data, registered_by="admin_text"
                        )
                        report_lines = [
                            f"📋 ลงทะเบียนจากข้อความเสร็จสิ้น",
                            f"",
                            f"👤 พบ {len(patients_data)} รายการ",
                            f"✅ ใหม่: {new_count} คน",
                            f"🔄 อัปเดต: {updated_count} คน",
                        ]
                        if messages:
                            report_lines.append("")
                            report_lines.extend(messages)
                        report_lines.append("")
                        report_lines.append("รายชื่อ:")
                        for i, pd in enumerate(patients_data[:15], 1):
                            hn = pd.get("clinic_hn", "?")
                            hosp = pd.get("hospital_hn", "")
                            name = pd.get("patient_name", "-")
                            hn_disp = f"{hn}/{hosp}" if hosp else hn
                            report_lines.append(f"  {i}. {hn_disp} — {name}")
                        if len(patients_data) > 15:
                            report_lines.append(f"  ...และอีก {len(patients_data)-15} คน")
                        reply_message(reply_token, "\n".join(report_lines))
                    else:
                        reply_message(
                            reply_token,
                            "❌ ไม่พบ HN + ชื่อในข้อความ\n\n"
                            "รูปแบบที่รองรับ:\n"
                            "  • 56001 สมชาย ใจดี\n"
                            "  • HN:56001 สมชาย\n"
                            "  • 56001/00002567 สมชาย\n"
                            "  • สมชาย ใจดี 00002567 56001\n\n"
                            "พิมพ์ 'ลงทะเบียนรายชื่อ' เพื่อลองใหม่",
                        )
                    continue

                # ── Pending Classification: ตรวจว่าผู้ใช้ตอบเลข 1-4 ──
                _pending = _get_pending_classification(user_hash)
                if _pending and text.strip() in ("1", "2", "3", "4"):
                    _class_map = {
                        "1": "rx", "2": "lab",
                        "3": "dialysis", "4": "bills",
                    }
                    _new_cat = _class_map[text.strip()]
                    _clear_pending_classification(user_hash)
                    reply_message(
                        reply_token,
                        f"⏳ กำลังย้ายเอกสาร...",
                    )
                    # รัน reclassify ใน thread แยก
                    _reclass_uid = _pending.get("line_user_id", "")
                    threading.Thread(
                        target=_reclassify_document,
                        args=(_pending, _new_cat, _reclass_uid),
                        daemon=True,
                    ).start()
                    continue

                # ── Document Search V1: ตรวจคำสั่งค้นหาก่อน ──
                search_response = handle_search_command(
                    text, user_hash, _line_user_id,
                )
                if search_response:
                    if isinstance(search_response, dict) and search_response.get("type") == "flex":
                        # Flex Message (เช่น ขอไฟล์ → ปุ่มเปิด PDF)
                        reply_line_message(reply_token, search_response)
                    else:
                        reply_message(reply_token, search_response)
                # ── โหมดเลือกผู้ป่วย: ระบุผู้ป่วยก่อนส่งเอกสาร ──
                elif _parse_patient_select(text) is not None:
                    _sel_hn = _parse_patient_select(text)
                    _sel_patient = (
                        master_registry.match_hn(_sel_hn)
                        if master_registry else None
                    )
                    if not _sel_patient:
                        reply_message(
                            reply_token,
                            f"❌ ไม่พบ HN {_sel_hn} ในทะเบียนผู้ป่วย\n\n"
                            "ตรวจ HN อีกครั้ง หรือพิมพ์ 'ทะเบียนผู้ป่วย' "
                            "เพื่อดูรายชื่อ",
                        )
                    else:
                        _ok = _set_patient_context(
                            user_hash,
                            _sel_patient.folder_name,
                            _sel_patient.hn_display,
                            _sel_patient.patient_name,
                        )
                        if _ok:
                            reply_message(
                                reply_token,
                                f"👤 ผู้ป่วยปัจจุบัน\n"
                                f"{_sel_patient.hn_display} — "
                                f"{_sel_patient.patient_name or 'ไม่มีชื่อ'}\n\n"
                                "📂 เอกสารที่ส่งต่อจากนี้ (ใบแล็บ ใบยา "
                                "ใบฟอกเลือด) จะเข้าแฟ้มของผู้ป่วยคนนี้\n\n"
                                "⏱️ ค้างไว้ 30 นาที\n"
                                "• เปลี่ยนคน: พิมพ์ 'ผู้ป่วย <HN>'\n"
                                "• เลิกใช้: พิมพ์ 'ล้างผู้ป่วย'",
                            )
                        else:
                            reply_message(
                                reply_token,
                                "❌ ไม่สามารถตั้งผู้ป่วยได้ในขณะนี้ "
                                "กรุณาลองใหม่",
                            )
                elif text.strip() in (
                    "ล้างผู้ป่วย", "ยกเลิกผู้ป่วย", "ปิดผู้ป่วย",
                    "clear patient", "เสร็จแล้ว",
                ):
                    _clear_patient_context(user_hash)
                    reply_message(
                        reply_token,
                        "✅ ล้างผู้ป่วยปัจจุบันแล้ว\n\n"
                        "เอกสารที่ส่งต่อจากนี้จะเก็บที่ระดับไตรมาส/เดือน "
                        "จนกว่าจะเลือกผู้ป่วยใหม่",
                    )
                elif text.strip() in (
                    "ผู้ป่วยปัจจุบัน", "ดูผู้ป่วย", "current patient",
                ):
                    _cur = _get_patient_context(user_hash)
                    if _cur:
                        _mins = int(
                            (_PATIENT_CTX_TIMEOUT
                             - (time.time() - _cur.get("timestamp", 0))) / 60
                        )
                        reply_message(
                            reply_token,
                            f"👤 ผู้ป่วยปัจจุบัน\n"
                            f"{_cur.get('hn_display', '')} — "
                            f"{_cur.get('patient_name') or 'ไม่มีชื่อ'}\n\n"
                            f"⏱️ เหลืออีกประมาณ {max(_mins, 0)} นาที",
                        )
                    else:
                        reply_message(
                            reply_token,
                            "ยังไม่ได้เลือกผู้ป่วย\n\n"
                            "พิมพ์ 'ผู้ป่วย <HN>' เพื่อเลือก\n"
                            "ตัวอย่าง: ผู้ป่วย 57042",
                        )
                # ── [CS-2] รับทราบ alert วิกฤต ──────────────────────
                # ข้อความแจ้งเตือนบอกให้แพทย์ตอบ 'รับทราบ <id>' มาตลอด
                # แต่ไม่เคยมี handler รองรับ → คำตอบของแพทย์ถูกทิ้งเงียบ
                # และระบบไม่มีทางรู้เลยว่ามีคนเห็นผลวิกฤตแล้วหรือยัง
                elif _parse_alert_ack(text) is not None:
                    _ack_ref, _ack_action = _parse_alert_ack(text)
                    if alert_service is None:
                        reply_message(
                            reply_token,
                            "ขออภัย ระบบแจ้งเตือนไม่พร้อมใช้งานในขณะนี้",
                        )
                    elif (user_hash not in PHYSICIAN_USERS
                          and user_hash not in ADMIN_USERS):
                        # การรับทราบผลวิกฤตเป็นการกระทำทางคลินิก
                        logger.warning(
                            "Alert ack denied — ไม่ใช่แพทย์/ผู้ดูแล"
                        )
                        audit_logger.log("alert_ack_denied", user_hash, {
                            "action": _ack_action,
                        })
                        reply_message(
                            reply_token,
                            "⚠️ เฉพาะแพทย์หรือผู้ดูแลระบบเท่านั้น"
                            "ที่รับทราบการแจ้งเตือนได้",
                        )
                    else:
                        _full_id = alert_service.find_alert_id_by_prefix(
                            _ack_ref
                        )
                        if not _full_id:
                            reply_message(
                                reply_token,
                                f"❌ ไม่พบการแจ้งเตือน {_ack_ref}\n\n"
                                "ตรวจรหัสอีกครั้ง หรือพิมพ์ "
                                "'แจ้งเตือนค้าง' เพื่อดูรายการ",
                            )
                        else:
                            if _ack_action == "resolve":
                                _res = alert_service.resolve_alert(
                                    _full_id, user_hash
                                )
                            else:
                                _res = alert_service.acknowledge_alert(
                                    _full_id, user_hash
                                )
                            if _res.get("error"):
                                reply_message(
                                    reply_token, f"❌ {_res['error']}"
                                )
                            else:
                                reply_message(
                                    reply_token,
                                    _res.get("message", "✅ บันทึกแล้ว"),
                                )
                # ── ย้ายเอกสารที่เข้าแฟ้มผิดคน ──────────────────────
                # workflow จริงใช้เจ้าหน้าที่เลือกผู้ป่วยก่อนส่ง ถ้าลืม
                # เปลี่ยนผู้ป่วย เอกสารชุดถัดไปเข้าแฟ้มคนก่อนทั้งชุด
                # ก่อนหน้านี้ไม่มีวิธีแก้เลย ต้องส่งใหม่อย่างเดียว
                elif _parse_move_document(text) is not None:
                    _mv_doc, _mv_hn = _parse_move_document(text)
                    if (user_hash not in PHYSICIAN_USERS
                            and user_hash not in ADMIN_USERS):
                        audit_logger.log(
                            "document_refile_denied", user_hash, {},
                        )
                        reply_message(
                            reply_token,
                            "⚠️ เฉพาะแพทย์หรือผู้ดูแลระบบเท่านั้น"
                            "ที่ย้ายเอกสารได้",
                        )
                    else:
                        _mv_res = _move_document_to_patient(
                            _mv_doc, _mv_hn, user_hash,
                        )
                        if _mv_res.get("error"):
                            reply_message(
                                reply_token, f"❌ {_mv_res['error']}"
                            )
                        else:
                            reply_message(
                                reply_token,
                                _mv_res.get("message", "✅ ดำเนินการแล้ว"),
                            )
                # ── เปลี่ยนประเภทเอกสารที่จัดหมวดไว้ผิด ─────────────
                # ของเดิมแก้ได้เฉพาะตอนเพิ่งส่งเข้ามา (ตอบเลข 1-4)
                # พอเลยช่วงนั้นไปแล้วไม่มีวิธีแก้เลย — ตรวจ production
                # 26 ต.ค. 2026 พบเอกสาร 2 ใบถูกจัดเป็น lab ทั้งที่ไม่มี
                # ชื่อรายการตรวจแม้แต่ชนิดเดียว และแก้ไม่ได้ด้วยคำสั่งใด
                elif _parse_change_category(text) is not None:
                    _cc_doc, _cc_cat, _cc_hn = _parse_change_category(text)
                    if (user_hash not in PHYSICIAN_USERS
                            and user_hash not in ADMIN_USERS):
                        audit_logger.log(
                            "document_category_change_denied", user_hash, {},
                        )
                        reply_message(
                            reply_token,
                            "⚠️ เฉพาะแพทย์หรือผู้ดูแลระบบเท่านั้น"
                            "ที่เปลี่ยนประเภทเอกสารได้",
                        )
                    else:
                        _cc_res = _change_document_category(
                            _cc_doc, _cc_cat, _cc_hn, user_hash,
                        )
                        if _cc_res.get("error"):
                            reply_message(
                                reply_token, f"❌ {_cc_res['error']}"
                            )
                        else:
                            reply_message(
                                reply_token,
                                _cc_res.get("message", "✅ ดำเนินการแล้ว"),
                            )
                # ── [CS-2] ดูรายการแจ้งเตือนที่ยังค้าง ──────────────
                # แพทย์ต้องเห็นรายการก่อนจึงจะรับทราบได้ ลำพังคำสั่ง
                # 'รับทราบ <id>' ใช้ไม่ได้ถ้าหา id ไม่ได้
                elif text.strip() in (
                    "แจ้งเตือนค้าง", "แจ้งเตือน", "active alerts", "alerts",
                ):
                    if alert_service is None:
                        reply_message(
                            reply_token,
                            "ขออภัย ระบบแจ้งเตือนไม่พร้อมใช้งานในขณะนี้",
                        )
                    elif (user_hash not in PHYSICIAN_USERS
                          and user_hash not in ADMIN_USERS):
                        reply_message(
                            reply_token,
                            "⚠️ เฉพาะแพทย์หรือผู้ดูแลระบบเท่านั้น",
                        )
                    else:
                        try:
                            _active = alert_service.get_active_alerts()
                            _txt = format_active_alerts(_active)
                            if _active:
                                _txt += (
                                    "\n\nรับทราบ: พิมพ์ 'รับทราบ <รหัส>'"
                                )
                            reply_message(reply_token, _txt)
                        except Exception as _al_err:
                            logger.warning(
                                "active alerts failed: %s",
                                type(_al_err).__name__,
                            )
                            reply_message(
                                reply_token,
                                "ขออภัย ไม่สามารถดึงรายการแจ้งเตือนได้",
                            )
                # ── Master Patient Registry: ลงทะเบียนรายชื่อ ──
                elif text.strip() in (
                    "ลงทะเบียนรายชื่อ", "ลงทะเบียน",
                    "register", "register patients",
                    "เพิ่มผู้ป่วย", "เพิ่มชื่อ",
                ):
                    # ตรวจสิทธิ์ admin (เฉพาะ admin เท่านั้น)
                    _admin_hash = os.getenv("ADMIN_LINE_HASH", "").strip()
                    if _admin_hash and user_hash != _admin_hash:
                        reply_message(
                            reply_token,
                            "⚠️ เฉพาะผู้ดูแลระบบเท่านั้นที่ลงทะเบียนรายชื่อได้",
                        )
                    else:
                        _set_registration_mode(user_hash)
                        reply_message(
                            reply_token,
                            "📋 โหมดลงทะเบียนรายชื่อผู้ป่วย\n\n"
                            "⌨️ พิมพ์หรือวางรายชื่อ:\n"
                            "1 บรรทัดต่อ 1 คน\n\n"
                            "รูปแบบ:\n"
                            "สมชาย ใจดี | 00012345 | 63001\n"
                            "สมหญิง รักดี | 00067890 | 63002\n\n"
                            "หรือ:\n"
                            "63001 สมชาย ใจดี\n"
                            "63002/00067890 สมหญิง รักดี\n\n"
                            "📋 วิธีง่าย: Copy จาก Excel/ตาราง\n"
                            "→ วางใน LINE แล้วกดส่ง\n\n"
                            "⏱️ มีเวลา 2 นาที\n"
                            "พิมพ์ 'ยกเลิก' เพื่อออก",
                        )
                elif text.strip() in ("ยกเลิก", "cancel"):
                    if _check_registration_mode(user_hash):
                        _clear_registration_mode(user_hash)
                        reply_message(
                            reply_token,
                            "❌ ยกเลิกโหมดลงทะเบียนรายชื่อแล้ว",
                        )
                    else:
                        reply_message(reply_token, "ไม่มีโหมดที่ต้องยกเลิก")
                elif text.strip() in ("ทะเบียนผู้ป่วย", "ดูรายชื่อ", "patient list"):
                    # แสดงรายชื่อผู้ป่วย (admin only)
                    _admin_hash = os.getenv("ADMIN_LINE_HASH", "").strip()
                    if _admin_hash and user_hash != _admin_hash:
                        reply_message(
                            reply_token,
                            "⚠️ เฉพาะผู้ดูแลระบบเท่านั้น",
                        )
                    else:
                        patients = master_registry.list_all()
                        if not patients:
                            reply_message(
                                reply_token,
                                "📋 ยังไม่มีรายชื่อในทะเบียน\n"
                                "พิมพ์ 'ลงทะเบียนรายชื่อ' แล้วส่งรูปรายชื่อ",
                            )
                        else:
                            lines = [f"📋 ทะเบียนผู้ป่วย ({len(patients)} คน)\n"]
                            for i, p in enumerate(patients[:30], 1):
                                lines.append(
                                    f"{i}. {p.hn_display} — {p.patient_name or 'ไม่มีชื่อ'}"
                                )
                            if len(patients) > 30:
                                lines.append(f"\n...และอีก {len(patients)-30} คน")
                            reply_message(reply_token, "\n".join(lines))

                # ── แก้ไขชื่อผู้ป่วย ──
                elif text.strip().startswith(("แก้ไข ", "แก้ชื่อ ", "edit ")):
                    _admin_hash = os.getenv("ADMIN_LINE_HASH", "").strip()
                    if _admin_hash and user_hash != _admin_hash:
                        reply_message(
                            reply_token,
                            "⚠️ เฉพาะผู้ดูแลระบบเท่านั้นที่แก้ไขข้อมูลได้",
                        )
                    else:
                        # รูปแบบ: แก้ไข HN ชื่อใหม่
                        # เช่น: แก้ไข 56001 สมชาย ใจดี
                        #       แก้ชื่อ HN:56001 สมหญิง ดีใจ
                        import re as _re
                        _edit_text = text.strip()
                        # ตัด prefix command ออก
                        for _prefix in ("แก้ไข ", "แก้ชื่อ ", "edit "):
                            if _edit_text.startswith(_prefix):
                                _edit_text = _edit_text[len(_prefix):].strip()
                                break
                        # แยก HN กับชื่อใหม่
                        _hn_match = _re.match(
                            r"(?:HN[:\s]*)?(\d{4,8})\s+(.+)",
                            _edit_text, _re.IGNORECASE,
                        )
                        if not _hn_match:
                            reply_message(
                                reply_token,
                                "❌ รูปแบบไม่ถูกต้อง\n\n"
                                "วิธีใช้:\n"
                                "  แก้ไข [HN] [ชื่อใหม่]\n\n"
                                "ตัวอย่าง:\n"
                                "  แก้ไข 56001 สมชาย ใจดี\n"
                                "  แก้ชื่อ HN:56001 สมหญิง ดีใจ",
                            )
                        else:
                            _hn = _hn_match.group(1)
                            _new_name = _hn_match.group(2).strip()
                            _ok, _msg = master_registry.update_patient(
                                hn=_hn, new_name=_new_name,
                            )
                            reply_message(reply_token, _msg)

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
                        "  ลงทะเบียนรายชื่อ — ลงทะเบียนผู้ป่วย (admin)\n"
                        "  ทะเบียนผู้ป่วย — ดูรายชื่อ (admin)\n"
                        "  แก้ไข [HN] [ชื่อใหม่] — แก้ชื่อผู้ป่วย (admin)\n"
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


# ─── Phase 2A Health Monitoring: /health/live, /health/ready ──
try:
    from health_check import init_health, _check_storage, _health_state
    init_health()
except ImportError:
    pass


@app.route("/health/parser-test", methods=["GET"])
def health_parser_test():
    """ทดสอบ OCR parser — ไม่มีข้อมูลผู้ป่วยจริง"""
    from patient_master_registry import _parse_patient_line, parse_patient_list_ocr
    test_line = "P35 | Sarr situ annues | 00063695 | 63203"
    result = _parse_patient_line(test_line)
    # ทดสอบ full pipeline
    test_block = "36 | cure laut | 00032210 | 63208\n37 | cans guts | 00007094 | 63215"
    batch = parse_patient_list_ocr(test_block)
    return jsonify({
        "parser_version": "pattern7-ocr-fallback",
        "single_line_test": {"input": test_line, "result": result},
        "batch_test": {"input_lines": 2, "parsed_count": len(batch), "results": batch},
    }), 200


@app.route("/health/live", methods=["GET"])
def health_live():
    """Liveness probe — process สามารถรับ request ได้หรือไม่."""
    try:
        return jsonify({
            "status": "alive",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "uptime_since": _health_state.get("started_at", "unknown"),
        }), 200
    except Exception:
        return jsonify({"status": "unhealthy"}), 503


@app.route("/health/ready", methods=["GET"])
def health_ready():
    """Readiness probe — dependencies พร้อมใช้งานหรือไม่.

    ไม่เปิดเผย credentials, bucket name, service account
    """
    checks = {}
    all_ready = True

    if storage is not None and hasattr(storage, 'list_files'):
        try:
            gcs_ok = _check_storage(storage)
        except Exception:
            gcs_ok = False
        checks["storage"] = "ok" if gcs_ok else "unavailable"
        if not gcs_ok:
            all_ready = False
    else:
        checks["storage"] = "not_configured"

    # Piggyback: check alert retries on readiness probe
    if alert_retry_manager is not None:
        try:
            alert_retry_manager.check_and_retry(
                send_push_fn=line_push_fn
            )
        except Exception:
            pass  # retry failure ไม่กระทบ readiness

    status_code = 200 if all_ready else 503
    status_text = "ready" if all_ready else "not_ready"

    return jsonify({
        "status": status_text,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
    }), status_code


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
        "version": "cloud-2.0-production-docroute",
        "app_env": APP_ENV,
        "testing_mode": IS_TESTING,
        "production_mode": IS_PRODUCTION,
        "timestamp": datetime.now().isoformat(),
        # ── สถานะการจำแนกเอกสาร ──────────────────────────────
        # ต้องรายงาน "ผลจริง" ไม่ใช่ค่า ENV ที่ตั้งไว้
        #
        # ของเดิมรายงาน ai_classify จาก ENABLE_AI_CLASSIFY เพียงตัวเดียว
        # บน production ที่ไม่ได้ตั้ง ENV ค่านั้นเป็น true (ค่าเริ่มต้นใน
        # โค้ด) /health จึงขึ้นว่า ai_classify: true ทั้งที่
        # EXTERNAL_AI_CLASSIFICATION_ALLOWED = False บังคับให้ตกไปใช้การ
        # จำแนกด้วยคำสำคัญภายในเสมอ ไม่มีข้อมูลผู้ป่วยออกนอกระบบเลย
        #
        # ตัวเลขที่อ่านผิดได้แบบนี้เป็นอันตรายกับการตรวจสอบ PDPA —
        # ผู้ตรวจที่อ่าน /health จะสรุปว่าระบบส่งข้อมูลไป AI ภายนอก
        "ai_classify": (
            EXTERNAL_AI_CLASSIFICATION_ALLOWED
            and ENABLE_AI_CLASSIFY
            and not IS_TESTING
        ),
        "document_classifier": (
            "external_ai"
            if (
                EXTERNAL_AI_CLASSIFICATION_ALLOWED
                and ENABLE_AI_CLASSIFY
                and not IS_TESTING
            )
            else "internal_keywords"
        ),
        "external_ai_allowed": EXTERNAL_AI_CLASSIFICATION_ALLOWED,
        "ai_classify_env_setting": ENABLE_AI_CLASSIFY,
        "storage_backend": STORAGE_BACKEND,
        # ── ความพร้อมของเส้นทางแจ้งเตือนค่าวิกฤต ──────────────
        # เดิมไม่มีทางรู้เลยว่าการแจ้งเตือนส่งถึงใครได้หรือไม่ นอกจาก
        # ไปอ่าน log ซึ่งไม่มีใครอ่าน จนตรวจพบเมื่อ 26 ต.ค. 2026 ว่า
        # ALERT_RECIPIENTS ไม่ได้ตั้งค่าเลย และ alert ค่าวิกฤตทุกใบ
        # ถูกตีตรา delivery_failed แบบเงียบ ๆ
        #
        # คืนเฉพาะจำนวนและสาเหตุ ไม่มี LINE user ID — /health
        # เปิดอ่านได้โดยไม่ต้องยืนยันตัวตน
        "critical_alert_delivery": (
            describe_alert_recipients(storage)
            if describe_alert_recipients is not None
            else {"can_deliver": False, "problem": "โมดูลแจ้งเตือนไม่พร้อม"}
        ),
        "alert_push_configured": bool(line_push_fn),
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
            # [SEC-1] LIFF ยืนยันตัวตนด้วย ID token ซึ่งต้องมี channel id
            # ถ้าไม่ได้ตั้ง endpoint จะปฏิเสธทุกคำขอ (fail-closed)
            "liff_channel_id_set": bool(
                os.getenv("LIFF_CHANNEL_ID", "").strip()
            ),
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


# ─── OCR Debug Endpoint (ชั่วคราว — ไม่เก็บ PHI ลง disk/log) ──────────
@app.route("/admin/ocr-debug", methods=["GET"])
def admin_ocr_debug():
    """คืนค่า OCR debug จาก memory — ต้องมี secret parameter"""
    secret = request.args.get("secret", "")
    if secret != ADMIN_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    if not _ocr_debug_store:
        return jsonify({"status": "no_data", "message": "ยังไม่มีข้อมูล OCR — ส่งรูปผ่าน LINE OA ก่อน"}), 200
    return jsonify(_ocr_debug_store), 200


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

    # [§3.1] ตรวจรูปแบบ owner_hash ทั้งชุดก่อนเริ่มผูกแม้แต่รายการเดียว
    # การผูกเป็นแบบถาวรและไม่ยอมผูกซ้ำ ถ้าปล่อยค่าผิดรูปผ่านไป เอกสาร
    # นั้นจะเข้าถึงไม่ได้ตลอดไปและแก้ผ่าน endpoint นี้ไม่ได้อีก
    _invalid = [
        item.get("doc_id", "")[:8]
        for item in assignments
        if item.get("owner_hash")
        and not is_valid_owner_hash(item.get("owner_hash", ""))
    ]
    if _invalid:
        logger.warning(
            "assign-owner: ปฏิเสธ %d รายการที่ owner_hash ผิดรูปแบบ",
            len(_invalid),
        )
        return jsonify({
            "error": "invalid owner_hash format",
            "detail": "owner_hash ต้องเป็น hex ตัวพิมพ์เล็ก 32 ตัว "
                      "(ผลลัพธ์ของ hash_user_id) — ไม่ใช่ LINE userId ดิบ",
            "invalid_doc_ids": _invalid,
        }), 400

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


# ─── Admin Debug: GCS Paths ──────────────────────────────

@app.route("/admin/api/v2/debug-paths", methods=["GET"])
def admin_debug_paths():
    """แสดง GCS path ของเอกสาร — ไม่แสดงข้อมูลผู้ป่วย."""
    auth_param = request.args.get("secret", "")
    if not ADMIN_SECRET or auth_param != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    cat_filter = request.args.get("category", "")
    paths = []
    for doc in doc_index._docs:
        if cat_filter and doc.get("category") != cat_filter:
            continue
        paths.append({
            "doc_id": doc.get("document_id", "")[:8],
            "category": doc.get("category", ""),
            "gcs_path": doc.get("gcs_object_path", ""),
            "upload_date": doc.get("upload_date", ""),
        })
    return jsonify({"count": len(paths), "paths": paths}), 200


# ─── Admin Debug: HN Extraction Test ─────────────────────

@app.route("/admin/api/v2/debug-hn-extract", methods=["GET"])
def admin_debug_hn_extract():
    """ทดสอบ HN extraction จากเอกสารที่มีอยู่ — ไม่แสดง PHI.

    ตรวจว่า extract_hn_from_document พบ HN ในข้อความ OCR หรือไม่
    และ master_registry จับคู่ได้หรือไม่
    """
    auth_param = request.args.get("secret", "")
    if not ADMIN_SECRET or auth_param != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    cat_filter = request.args.get("category", "lab")
    limit = min(int(request.args.get("limit", "5")), 20)

    results = []
    checked = 0
    for doc in doc_index._docs:
        if doc.get("category") != cat_filter:
            continue
        if checked >= limit:
            break
        checked += 1

        text = doc.get("extracted_text", "")
        doc_id_short = doc.get("document_id", "")[:8]

        entry = {
            "doc_id": doc_id_short,
            "category": cat_filter,
            "has_text": bool(text),
            "text_length": len(text),
        }

        if text:
            found_hns = extract_hn_from_document(text)
            entry["extracted_hns"] = found_hns
            entry["hn_count"] = len(found_hns)

            # ทดสอบ match กับ registry
            matched = []
            for hn in found_hns:
                folder = master_registry.get_folder_for_hn(hn)
                matched.append({
                    "hn": hn,
                    "matched_folder": folder or "(no match)",
                })
            entry["registry_matches"] = matched

            # ── วินิจฉัยว่าทำไมหา HN ไม่พบ (ไม่เปิดเผย PHI) ──
            # รายงานเฉพาะว่า "มี label นี้ในเอกสารหรือไม่" (boolean)
            # และรูปแบบความยาวของกลุ่มตัวเลข — ไม่คืนตัวเลขหรือชื่อจริง
            import re as _re_dbg

            labels = {
                "HN": r"H\.?N\.?",
                "เลขที่": r"เลข(?:ที่)?",
                "รหัส": r"รหัส",
                "ผู้ป่วย": r"ผู้ป่วย",
                "Patient": r"patient",
                "AN": r"\bA\.?N\.?\b",
                "VN": r"\bV\.?N\.?\b",
                "ชื่อ": r"ชื่อ",
            }
            entry["labels_present"] = {
                name: bool(_re_dbg.search(pat, text, _re_dbg.IGNORECASE))
                for name, pat in labels.items()
            }
            # ความยาวของกลุ่มตัวเลขที่พบ (histogram) — ไม่ใช่ค่าตัวเลข
            digit_runs = _re_dbg.findall(r"\d+", text)
            hist: dict[str, int] = {}
            for run in digit_runs:
                key = str(len(run))
                hist[key] = hist.get(key, 0) + 1
            entry["digit_run_length_histogram"] = hist
            entry["digit_run_total"] = len(digit_runs)
        else:
            entry["extracted_hns"] = []
            entry["hn_count"] = 0
            entry["note"] = "no OCR text in index"

        results.append(entry)

    return jsonify({
        "category": cat_filter,
        "checked": checked,
        "registry_count": master_registry.count(),
        "results": results,
    }), 200


# ─── Admin Dashboard: Stats API ──────────────────────────

@app.route("/admin/api/v2/retroactive-route", methods=["POST"])
def admin_retroactive_route():
    """จัดเอกสารเก่าเข้า folder ผู้ป่วยย้อนหลัง (retroactive routing).

    Scan เอกสาร lab/rx/dialysis ที่ยังไม่ได้จัดเข้า patient folder
    → ดึง HN จาก OCR text → จับคู่กับ master registry
    → copy blob ไป path ใหม่ใน patient folder
    → อัปเดต doc index ให้ชี้ไปที่ path ใหม่

    ไม่ลบไฟล์เดิม (ไม่มี delete permission)

    Query params:
      secret: ADMIN_SECRET
      dry_run: "true" เพื่อดู preview ก่อน (ไม่ copy จริง)
      category: "all" (default) หรือ "lab", "rx", "dialysis"
    """
    auth_param = request.args.get("secret", "")
    if not ADMIN_SECRET or auth_param != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    dry_run = request.args.get("dry_run", "true").lower() == "true"
    cat_filter = request.args.get("category", "all")

    if not storage.is_ready:
        return jsonify({"error": "GCS not ready"}), 500

    # Force reload doc index + registry
    doc_index._reload_from_gcs()
    reg_count = master_registry.count()

    if reg_count == 0:
        return jsonify({"error": "Registry empty — ไม่มีผู้ป่วยลงทะเบียน"}), 400

    categories = ["lab", "rx", "dialysis"] if cat_filter == "all" else [cat_filter]
    routed = []
    skipped = []
    errors = []
    already_in_folder = 0

    for doc in doc_index._docs:
        cat = doc.get("category", "")
        if cat not in categories:
            continue

        gcs_path = doc.get("gcs_object_path", "")
        if not gcs_path:
            continue

        doc_id = doc.get("document_id", "")
        doc_id_short = doc_id[:8] if doc_id else "unknown"

        # ไม่ตัดเอกสารที่อยู่ใน patient folder แล้วออกตรงนี้ — เอกสารบางชุด
        # ถูกย้ายด้วยตรรกะเดิมไปอยู่ใต้ path แบบเก่า (folder/YYYY-MM/HN_ชื่อ/)
        # จึงต้องคำนวณ path มาตรฐานก่อน แล้วค่อยเทียบว่าตรงอยู่แล้วหรือไม่
        # (เทียบที่ `new_blob_path == blob_path` ด้านล่าง)

        # ดึง HN จาก OCR text แล้วจับคู่กับทะเบียน
        # หมายเหตุ: ถ้าจับคู่ไม่ได้ ก็ยังย้ายเข้าโครงสร้างไตรมาส/เดือน
        # (ไม่มีแฟ้มรายผู้ป่วย) เพื่อให้ทุก folder หลักมี sub-folder
        # สม่ำเสมอกัน — ไม่ทิ้งเอกสารไว้แบบ folder/YYYY-MM/ อย่างเดียว
        text = doc.get("extracted_text", "")
        matched_folder = ""
        matched_hn = ""
        hn_note = ""

        if not text:
            hn_note = "no OCR text"
        else:
            found_hns = extract_hn_from_document(text)
            if not found_hns:
                hn_note = "no HN found in OCR"
            else:
                for hn in found_hns:
                    folder = master_registry.get_folder_for_hn(hn)
                    if folder:
                        matched_folder = folder
                        matched_hn = hn
                        break
                if not matched_folder:
                    hn_note = "HN found but no registry match"

        # สร้าง path ใหม่ (มี patient folder ถ้าจับคู่ได้)
        # gcs_path เป็น gs://bucket/... ต้องตัดเอาเฉพาะ blob path
        if gcs_path.startswith("gs://"):
            blob_path = gcs_path.split("/", 3)[3] if gcs_path.count("/") >= 3 else ""
        else:
            blob_path = gcs_path

        if not blob_path:
            continue

        # สร้าง path มาตรฐานใหม่ทั้งเส้น (ไม่ใช่แค่แทรก patient folder)
        # เอกสารเก่าอยู่แบบ folder/YYYY-MM/filename ต้องย้ายเข้าโครงสร้าง
        # folder/YYYY-QN/YYYY-MM/HN_ชื่อ/filename ให้ครบทั้งไตรมาส/เดือน/ผู้ป่วย
        filename = blob_path.rsplit("/", 1)[-1]
        dest_folder = FOLDERS.get(cat, FOLDERS["review"])

        # ใช้วันที่ของเอกสารเอง เพื่อให้เข้าไตรมาส/เดือนที่ถูกต้อง
        # และไม่ให้เอกสารเก่าถูกคิดว่าต้องย้ายเพียงเพราะเปลี่ยนเขตเวลา
        doc_dt = _doc_submitted_date(doc)

        new_blob_path = build_document_path(
            dest_folder, filename, cat,
            patient_folder=matched_folder,
            date=doc_dt,
        )

        # ถ้า path เดิมตรงกับ path ใหม่อยู่แล้ว ไม่ต้องทำอะไร
        if new_blob_path == blob_path:
            already_in_folder += 1
            continue

        entry = {
            "doc_id": doc_id_short,
            "category": cat,
            "matched_hn": matched_hn,
            "patient_folder": matched_folder,
            # ระบุว่าได้แฟ้มรายผู้ป่วยหรือย้ายเข้าแค่ไตรมาส/เดือน
            "routed_to": "patient_folder" if matched_folder else "quarter_month_only",
            "old_path_suffix": blob_path.split("/", 1)[-1][:40] + "...",
            "new_path_suffix": new_blob_path.split("/", 1)[-1][:50] + "...",
        }
        if hn_note:
            entry["hn_note"] = hn_note

        if dry_run:
            entry["action"] = "would_copy"
            routed.append(entry)
            continue

        # Copy blob ไป path ใหม่
        try:
            # เช็ค destination ก่อน — ถ้ามีอยู่แล้วไม่ต้อง copy ซ้ำ
            # (copy_blob ไป existing blob จะ Forbidden เพราะไม่มี delete perm)
            dest_blob = storage._bucket.blob(new_blob_path)
            if dest_blob.exists():
                # อัปเดต doc index ให้ชี้ path ใหม่ (ถ้ายังไม่ได้อัปเดต)
                new_gcs_uri = f"gs://{storage.bucket_name}/{new_blob_path}"
                if doc.get("gcs_object_path") != new_gcs_uri:
                    doc["gcs_object_path"] = new_gcs_uri
                    idx_path = f"_index/docs/{doc_id}.json"
                    try:
                        idx_blob = storage._bucket.blob(idx_path)
                        idx_blob.upload_from_string(
                            json.dumps(doc, ensure_ascii=False),
                            content_type="application/json",
                        )
                    except Exception:
                        pass
                entry["action"] = "already_copied"
                already_in_folder += 1
                continue

            src_blob = storage._bucket.blob(blob_path)
            if not src_blob.exists():
                entry["action"] = "error"
                entry["error"] = "source blob not found"
                errors.append(entry)
                continue

            # Server-side copy (ไม่ต้อง download)
            new_blob = storage._bucket.copy_blob(
                src_blob, storage._bucket, new_blob_path,
            )

            # อัปเดต doc index — เขียน per-doc JSON ใหม่ ด้วย gcs_object_path ใหม่
            new_gcs_uri = f"gs://{storage.bucket_name}/{new_blob_path}"
            doc["gcs_object_path"] = new_gcs_uri

            # เขียน per-doc index file ใหม่
            idx_path = f"_index/docs/{doc_id}.json"
            try:
                idx_blob = storage._bucket.blob(idx_path)
                idx_blob.upload_from_string(
                    json.dumps(doc, ensure_ascii=False),
                    content_type="application/json",
                )
            except Exception as idx_err:
                logger.warning(
                    "Retroactive route: index update failed for %s: %s",
                    doc_id_short, type(idx_err).__name__,
                )

            entry["action"] = "copied"
            routed.append(entry)
            logger.info(
                "Retroactive route: copied doc %s → patient folder (cat=%s)",
                doc_id_short, cat,
            )

        except Exception as copy_err:
            entry["action"] = "error"
            entry["error"] = type(copy_err).__name__
            errors.append(entry)
            logger.warning(
                "Retroactive route: copy failed for %s: %s",
                doc_id_short, type(copy_err).__name__,
            )

    return jsonify({
        "dry_run": dry_run,
        "registry_count": reg_count,
        "categories": categories,
        "already_in_folder": already_in_folder,
        "routed_count": len(routed),
        "routed_to_patient_folder": sum(
            1 for e in routed if e.get("routed_to") == "patient_folder"
        ),
        "routed_quarter_month_only": sum(
            1 for e in routed if e.get("routed_to") == "quarter_month_only"
        ),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "routed": routed,
        "skipped": skipped,
        "errors": errors,
    }), 200


@app.route("/admin/api/stats", methods=["GET"])
def admin_api_stats():
    """API สำหรับดึงข้อมูลสถิติระบบ — ใช้โดย Dashboard."""
    auth_header = request.headers.get("X-Admin-Secret", "")
    auth_param = request.args.get("secret", "")
    if not ADMIN_SECRET or (auth_header != ADMIN_SECRET and auth_param != ADMIN_SECRET):
        return jsonify({"error": "Unauthorized"}), 401

    # Document counts by category
    cat_counts: dict[str, int] = {}
    for doc in doc_index._docs:
        cat = doc.get("category", "other")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    # Recent documents (last 20)
    sorted_docs = sorted(
        doc_index._docs,
        key=lambda d: d.get("upload_date", ""),
        reverse=True,
    )[:20]
    recent_docs = []
    for d in sorted_docs:
        recent_docs.append({
            "doc_id": d.get("document_id", "")[:8],
            "category": d.get("category", ""),
            "upload_date": d.get("upload_date", ""),
            "owner": d.get("owner_hash", "")[:8] or "—",
            "size_kb": round(d.get("file_size", 0) / 1024, 1),
            "ocr": d.get("ocr_status", ""),
        })

    # Count alerts from GCS
    alert_count = 0
    recent_alerts = []
    try:
        if hasattr(storage, '_bucket') and storage._bucket:
            alert_blobs = list(storage._bucket.list_blobs(
                prefix="_alerts/", max_results=50
            ))
            alert_count = len(alert_blobs)
            for ab in alert_blobs[:10]:
                try:
                    adata = json.loads(ab.download_as_text())
                    recent_alerts.append({
                        "doc_id": adata.get("document_id", "")[:8],
                        "critical": adata.get("critical_count", 0),
                        "warning": adata.get("warning_count", 0),
                        "date": adata.get("created_at", "")[:16],
                    })
                except Exception:
                    pass
    except Exception:
        pass

    # Count approved labs
    approved_count = 0
    try:
        if hasattr(storage, '_bucket') and storage._bucket:
            lab_blobs = list(storage._bucket.list_blobs(
                prefix="_lab_results/", max_results=200
            ))
            for lb in lab_blobs:
                try:
                    ldata = json.loads(lb.download_as_text())
                    if ldata.get("status") == "approved":
                        approved_count += 1
                except Exception:
                    pass
    except Exception:
        pass

    return jsonify({
        "gcs_ready": getattr(storage, "is_ready", False),
        "total_docs": doc_index.count(),
        "legacy_docs": doc_index.count_legacy(),
        "categories": cat_counts,
        "recent_docs": recent_docs,
        "alert_count": alert_count,
        "recent_alerts": recent_alerts,
        "approved_count": approved_count,
        "app_env": APP_ENV,
        "physician_users": len(PHYSICIAN_USERS),
        "admin_users": len(ADMIN_USERS),
        "authorized_users": len(AUTHORIZED_USERS),
    }), 200


# ─── Admin Dashboard: Web UI ────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RANONG RAKTAI AI — Admin Dashboard</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;
--muted:#94a3b8;--accent:#38bdf8;--green:#22c55e;--red:#ef4444;
--yellow:#eab308;--purple:#a78bfa}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,sans-serif;
padding:16px;max-width:1200px;margin:0 auto}
h1{font-size:1.5rem;color:var(--accent);margin-bottom:4px}
.subtitle{color:var(--muted);font-size:0.85rem;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
.card h3{font-size:0.8rem;color:var(--muted);text-transform:uppercase;margin-bottom:8px}
.card .val{font-size:2rem;font-weight:700}
.card .val.ok{color:var(--green)}.card .val.warn{color:var(--yellow)}.card .val.err{color:var(--red)}
.section{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:16px}
.section h2{font-size:1rem;color:var(--accent);margin-bottom:12px;border-bottom:1px solid var(--border);padding-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:0.85rem}
th{text-align:left;color:var(--muted);padding:6px 8px;border-bottom:1px solid var(--border);font-weight:500}
td{padding:6px 8px;border-bottom:1px solid var(--border)}
.badge{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:0.7rem;font-weight:600}
.badge-lab{background:#164e63;color:#22d3ee}
.badge-rx{background:#4a1d96;color:#c4b5fd}
.badge-bill{background:#713f12;color:#fde047}
.badge-inbox{background:#1e3a5f;color:#93c5fd}
.badge-dialysis{background:#14532d;color:#86efac}
.badge-review{background:#3f3f46;color:#d4d4d8}
.cats{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.cat-chip{display:flex;align-items:center;gap:6px;padding:8px 14px;border-radius:10px;
background:var(--bg);border:1px solid var(--border);font-size:0.85rem}
.cat-chip .cnt{font-weight:700;color:var(--accent)}
.refresh{color:var(--muted);font-size:0.75rem;text-align:right}
#error{display:none;background:#450a0a;border:1px solid var(--red);border-radius:8px;
padding:12px;margin-bottom:16px;color:#fca5a5}
</style>
</head>
<body>
<h1>🏥 RANONG RAKTAI AI</h1>
<p class="subtitle">Admin Dashboard — ระบบจัดการเอกสารทางการแพทย์</p>
<div id="error"></div>

<div class="grid">
  <div class="card"><h3>สถานะ GCS</h3><div class="val" id="gcs">—</div></div>
  <div class="card"><h3>เอกสารทั้งหมด</h3><div class="val" id="total">—</div></div>
  <div class="card"><h3>แจ้งเตือนวิกฤต</h3><div class="val" id="alerts">—</div></div>
  <div class="card"><h3>ยืนยันแล้ว</h3><div class="val" id="approved">—</div></div>
  <div class="card"><h3>เอกสาร Legacy</h3><div class="val" id="legacy">—</div></div>
  <div class="card"><h3>โหมด</h3><div class="val" id="env" style="font-size:1.2rem">—</div></div>
</div>

<div class="section">
<h2>📁 จำนวนเอกสารตามหมวดหมู่</h2>
<div class="cats" id="categories"></div>
</div>

<div class="section">
<h2>🔔 การแจ้งเตือนล่าสุด</h2>
<table><thead><tr><th>รหัส</th><th>วิกฤต</th><th>เตือน</th><th>วันที่</th></tr></thead>
<tbody id="alert-rows"></tbody></table>
</div>

<div class="section">
<h2>📋 เอกสารล่าสุด (20 รายการ)</h2>
<table><thead><tr><th>รหัส</th><th>หมวด</th><th>วันที่</th><th>เจ้าของ</th><th>ขนาด</th><th>OCR</th></tr></thead>
<tbody id="doc-rows"></tbody></table>
</div>

<div class="section">
<h2>👥 ผู้ใช้ระบบ</h2>
<div class="grid" style="margin-bottom:0">
  <div class="card" style="border:0;padding:8px"><h3>แพทย์</h3><div class="val" id="physicians" style="font-size:1.5rem">—</div></div>
  <div class="card" style="border:0;padding:8px"><h3>ผู้ดูแล</h3><div class="val" id="admins" style="font-size:1.5rem">—</div></div>
  <div class="card" style="border:0;padding:8px"><h3>ผู้ใช้ที่อนุมัติ</h3><div class="val" id="authed" style="font-size:1.5rem">—</div></div>
</div>
</div>

<p class="refresh">อัปเดตอัตโนมัติทุก 30 วินาที — <span id="last-update">—</span></p>

<script>
const SECRET=new URLSearchParams(location.search).get('secret')||'';
const CAT_NAMES={lab:'ผลแล็บ',rx:'ใบสั่งยา',bills:'ใบเสร็จ',inbox:'กล่องข้อความ',
dialysis:'ฟอกไต',review:'รอตรวจสอบ',other:'อื่นๆ'};
const CAT_BADGE={lab:'badge-lab',rx:'badge-rx',bills:'badge-bill',
inbox:'badge-inbox',dialysis:'badge-dialysis',review:'badge-review'};

async function load(){
  try{
    const r=await fetch('/admin/api/stats?secret='+encodeURIComponent(SECRET));
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    document.getElementById('error').style.display='none';

    // Cards
    const gcsEl=document.getElementById('gcs');
    gcsEl.textContent=d.gcs_ready?'พร้อม':'ไม่พร้อม';
    gcsEl.className='val '+(d.gcs_ready?'ok':'err');
    document.getElementById('total').textContent=d.total_docs;
    const alertEl=document.getElementById('alerts');
    alertEl.textContent=d.alert_count;
    alertEl.className='val '+(d.alert_count>0?'warn':'ok');
    document.getElementById('approved').textContent=d.approved_count;
    document.getElementById('approved').className='val ok';
    const legEl=document.getElementById('legacy');
    legEl.textContent=d.legacy_docs;
    legEl.className='val '+(d.legacy_docs>0?'yellow':'ok');
    document.getElementById('env').textContent=d.app_env;

    // Categories
    const cc=document.getElementById('categories');
    cc.innerHTML='';
    for(const[k,v]of Object.entries(d.categories||{})){
      const chip=document.createElement('div');chip.className='cat-chip';
      chip.innerHTML='<span class="badge '+(CAT_BADGE[k]||'badge-review')+'">'
        +(CAT_NAMES[k]||k)+'</span><span class="cnt">'+v+'</span>';
      cc.appendChild(chip);
    }

    // Alerts table
    const ar=document.getElementById('alert-rows');
    ar.innerHTML='';
    (d.recent_alerts||[]).forEach(a=>{
      ar.innerHTML+='<tr><td>'+a.doc_id+'</td><td style="color:var(--red)">'+a.critical
        +'</td><td style="color:var(--yellow)">'+a.warning+'</td><td>'+a.date+'</td></tr>';
    });
    if(!d.recent_alerts||!d.recent_alerts.length)
      ar.innerHTML='<tr><td colspan="4" style="color:var(--muted)">ไม่มีการแจ้งเตือน</td></tr>';

    // Docs table
    const dr=document.getElementById('doc-rows');
    dr.innerHTML='';
    (d.recent_docs||[]).forEach(doc=>{
      dr.innerHTML+='<tr><td>'+doc.doc_id+'</td><td><span class="badge '
        +(CAT_BADGE[doc.category]||'badge-review')+'">'+(CAT_NAMES[doc.category]||doc.category)
        +'</span></td><td>'+doc.upload_date+'</td><td>'+doc.owner
        +'</td><td>'+doc.size_kb+' KB</td><td>'+doc.ocr+'</td></tr>';
    });

    // Users
    document.getElementById('physicians').textContent=d.physician_users;
    document.getElementById('admins').textContent=d.admin_users;
    document.getElementById('authed').textContent=d.authorized_users;

    document.getElementById('last-update').textContent=new Date().toLocaleTimeString('th-TH');
  }catch(e){
    const err=document.getElementById('error');
    err.style.display='block';
    err.textContent='โหลดข้อมูลไม่สำเร็จ: '+e.message;
  }
}
load();
setInterval(load,30000);
</script>
</body>
</html>"""


# ─── Clinical KPI Dashboard (Phase A) ─────────────────────

try:
    from clinical_dashboard import compute_clinical_kpi, CLINICAL_DASHBOARD_HTML
except ImportError:
    compute_clinical_kpi = None
    CLINICAL_DASHBOARD_HTML = "<html><body><h1>Clinical Dashboard module not found</h1></body></html>"

try:
    from nephrology_drug_db import review_ckd_medications, lookup_drug
except ImportError:
    review_ckd_medications = None
    lookup_drug = None

try:
    from ckd_progression import analyze_ckd_progression
except ImportError:
    analyze_ckd_progression = None

try:
    from auto_report import generate_monthly_report, report_to_line_text, report_to_flex_message
except ImportError:
    generate_monthly_report = None
    report_to_line_text = None
    report_to_flex_message = None

try:
    from liff_app import (
        get_patient_labs_for_liff, get_liff_html,
        verify_liff_id_token, LiffAuthError,
    )
except ImportError:
    get_patient_labs_for_liff = None
    get_liff_html = None
    verify_liff_id_token = None

    class LiffAuthError(Exception):
        """Fallback เมื่อโหลด liff_app ไม่ได้"""


@app.route("/admin/clinical-dashboard", methods=["GET"])
def admin_clinical_dashboard():
    """Kidney Clinical Intelligence Dashboard — Phase A."""
    auth_param = request.args.get("secret", "")
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or (auth_header != ADMIN_SECRET and auth_param != ADMIN_SECRET):
        return (
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h2>🔒 ต้องการรหัสผู้ดูแล</h2>"
            "<p>เข้าถึง: /admin/clinical-dashboard?secret=YOUR_ADMIN_SECRET</p>"
            "</body></html>"
        ), 401
    return CLINICAL_DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/admin/api/clinical-kpi", methods=["GET"])
def admin_api_clinical_kpi():
    """API สำหรับดึงข้อมูล Clinical KPI — Phase A."""
    auth_param = request.args.get("secret", "")
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or (auth_header != ADMIN_SECRET and auth_param != ADMIN_SECRET):
        return jsonify({"error": "Unauthorized"}), 401

    if compute_clinical_kpi is None:
        return jsonify({"error": "Clinical dashboard module not available"}), 500

    try:
        kpi = compute_clinical_kpi(
            storage=storage,
            patient_registry=master_registry if 'master_registry' in dir() else None,
        )
        return jsonify(kpi), 200
    except Exception as e:
        logger.error(f"Clinical KPI compute error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/drug-review", methods=["POST"])
def admin_api_drug_review():
    """API ตรวจสอบยาผู้ป่วย CKD — Phase E.

    Body: {"medications": ["drug1","drug2",...], "egfr": 25}

    Safety: ผลเป็นข้อมูลประกอบ — ห้ามใช้สั่งยา
    """
    auth_param = request.args.get("secret", "")
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or (auth_header != ADMIN_SECRET and auth_param != ADMIN_SECRET):
        return jsonify({"error": "Unauthorized"}), 401

    if review_ckd_medications is None:
        return jsonify({"error": "Drug review module not available"}), 500

    data = request.get_json(silent=True) or {}
    meds = data.get("medications", [])
    egfr = data.get("egfr")
    owner_hash = data.get("owner_hash", "")

    if not meds or egfr is None:
        return jsonify({"error": "medications (list) and egfr (number) required"}), 400

    try:
        summary = review_ckd_medications(meds, float(egfr), owner_hash)
        return jsonify(summary.to_dict()), 200
    except Exception as e:
        logger.error(f"Drug review error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/monthly-report", methods=["GET"])
def admin_api_monthly_report():
    """API สร้างรายงาน KPI รายเดือน — Phase C.

    Query params: ?secret=ADMIN_SECRET&month=2026-09 (optional)
    Returns: JSON monthly report

    Safety: aggregate only — ไม่มีข้อมูลระบุตัวผู้ป่วย
    """
    auth_param = request.args.get("secret", "")
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or (auth_header != ADMIN_SECRET and auth_param != ADMIN_SECRET):
        return jsonify({"error": "Unauthorized"}), 401

    if generate_monthly_report is None:
        return jsonify({"error": "Auto report module not available"}), 500

    try:
        ref_date = None
        month_param = request.args.get("month")
        if month_param:
            ref_date = datetime.strptime(month_param, "%Y-%m")

        report = generate_monthly_report(
            storage=storage,
            patient_registry=master_registry if 'master_registry' in dir() else None,
            reference_date=ref_date,
        )
        result = report.to_dict()
        result["line_text"] = report_to_line_text(report)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Monthly report error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/send-report", methods=["POST"])
def admin_api_send_report():
    """API ส่งรายงาน KPI ผ่าน LINE — Phase C.

    Body: {"month": "2026-09"} (optional, default: last month)

    Safety:
    - ส่งเฉพาะ aggregate data
    - ส่งเฉพาะ PHYSICIAN_USERS
    - ห้ามส่งข้อมูลผู้ป่วยออกภายนอก
    """
    auth_param = request.args.get("secret", "")
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or (auth_header != ADMIN_SECRET and auth_param != ADMIN_SECRET):
        return jsonify({"error": "Unauthorized"}), 401

    if generate_monthly_report is None:
        return jsonify({"error": "Auto report module not available"}), 500

    try:
        data = request.get_json(silent=True) or {}
        ref_date = None
        month_param = data.get("month")
        if month_param:
            ref_date = datetime.strptime(month_param, "%Y-%m")

        report = generate_monthly_report(
            storage=storage,
            patient_registry=master_registry if 'master_registry' in dir() else None,
            reference_date=ref_date,
        )

        # Send via LINE — only to registered physician users
        physician_ids = json.loads(os.environ.get("PHYSICIAN_LINE_IDS", "[]"))
        if not physician_ids:
            return jsonify({
                "status": "report_generated",
                "warning": "No PHYSICIAN_LINE_IDS configured — report not sent",
                "report": report.to_dict(),
                "line_text": report_to_line_text(report),
            }), 200

        # Use LINE Bot API to push
        channel_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
        sent = 0
        errors = []
        for uid in physician_ids:
            try:
                text = report_to_line_text(report)
                resp = http_requests.post(
                    "https://api.line.me/v2/bot/message/push",
                    headers={
                        "Authorization": f"Bearer {channel_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "to": uid,
                        "messages": [{"type": "text", "text": text}],
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    sent += 1
                else:
                    errors.append(f"{uid[:8]}: HTTP {resp.status_code}")
            except Exception as e:
                errors.append(f"{uid[:8]}: {str(e)}")

        return jsonify({
            "status": "sent",
            "sent_count": sent,
            "failed_count": len(errors),
            "errors": errors,
            "period": report.period_th,
        }), 200
    except Exception as e:
        logger.error(f"Send report error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/liff", methods=["GET"])
def liff_page():
    """LINE LIFF Mini-App — ผู้ป่วยดูผลแล็บ (Read-Only) — Phase F.

    Safety:
    - PDPA consent required
    - Read-Only — ไม่มีปุ่มแก้ไข
    - ข้อมูลกรองด้วย LINE userId (owner_hash)
    """
    if get_liff_html is None:
        return "<html><body><h1>LIFF module not available</h1></body></html>", 500
    api_base = request.host_url.rstrip("/")
    return get_liff_html(api_base), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/liff/api/my-labs", methods=["POST"])
def liff_api_my_labs():
    """API ดึงผลแล็บสำหรับ LIFF — Phase F.

    Body (JSON): {"id_token": "<LIFF ID token>"}

    [SEC-1] เดิม endpoint นี้รับ owner_hash จาก query string ตรง ๆ แล้ว
    ใช้ค่านั้นกรองข้อมูลผู้ป่วย = ให้ผู้เรียกประกาศเองว่าตัวเองเป็นใคร
    (broken authorization) และข้อมูลผู้ป่วยยังไปอยู่ใน URL ซึ่งถูกบันทึก
    ลง access log ด้วย

    ตอนนี้:
    - ยืนยัน ID token กับ LINE ที่ฝั่ง server (ตรวจ aud ซ้ำด้วย)
    - คำนวณ owner_hash จาก sub ที่ยืนยันแล้วเท่านั้น ไม่รับจากผู้เรียก
    - ใช้ POST เพื่อให้ตัวระบุตัวตนไม่อยู่ใน query string
    - ไม่คืนรายละเอียด exception กลับไปให้ client
    """
    if get_patient_labs_for_liff is None or verify_liff_id_token is None:
        return jsonify({"error": "LIFF module not available"}), 500

    body = request.get_json(silent=True) or {}
    id_token = body.get("id_token", "")
    if not id_token:
        return jsonify({"error": "id_token required"}), 400

    # ── ยืนยันตัวตนกับ LINE ──
    try:
        line_user_id = verify_liff_id_token(id_token)
    except LiffAuthError as auth_err:
        # เหตุผลละเอียดลง log ฝั่ง server เท่านั้น
        logger.warning("LIFF auth failed: %s", auth_err)
        audit_logger.log("liff_auth_failed", "", {})
        return jsonify({"error": "unauthorized"}), 401
    except Exception as e:
        logger.error("LIFF auth error: %s", type(e).__name__)
        return jsonify({"error": "unauthorized"}), 401

    # owner_hash มาจาก sub ที่ยืนยันแล้ว ใช้ salt เดียวกับทั้งระบบ
    owner_hash = hash_user_id(line_user_id)

    try:
        data = get_patient_labs_for_liff(
            storage=storage,
            owner_hash=owner_hash,
        )
        return jsonify(data), 200
    except Exception as e:
        logger.error("LIFF API error: %s", type(e).__name__)
        return jsonify({"error": "internal error"}), 500


@app.route("/admin/dashboard", methods=["GET"])
def admin_dashboard():
    """Admin Dashboard — Web UI สำหรับดูสถิติระบบ."""
    # Auth: ตรวจสอบ secret จาก query parameter (สำหรับเปิดในเบราว์เซอร์)
    auth_param = request.args.get("secret", "")
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or (auth_header != ADMIN_SECRET and auth_param != ADMIN_SECRET):
        return (
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h2>🔒 ต้องการรหัสผู้ดูแล</h2>"
            "<p>เข้าถึง: /admin/dashboard?secret=YOUR_ADMIN_SECRET</p>"
            "</body></html>"
        ), 401
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


# ─── Lab Trend Chart Generation ──────────────────────────

def generate_trend_chart_png(analysis, owner_prefix: str = "") -> bytes | None:
    """สร้างกราฟแนวโน้มผลแล็บเป็น PNG bytes.

    ไม่ใส่ชื่อผู้ป่วย — ใช้เฉพาะข้อมูล analyte/value.
    Returns None if matplotlib unavailable or no data.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # Non-interactive backend
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        logger.warning("matplotlib ไม่ได้ติดตั้ง — ข้ามสร้างกราฟ")
        return None

    trends = getattr(analysis, "trends", [])
    if not trends:
        return None

    # Filter to trends with at least 2 data points
    plottable = [t for t in trends if len(getattr(t, "data_points", [])) >= 2]
    if not plottable:
        return None

    fig, axes = plt.subplots(
        len(plottable), 1,
        figsize=(8, max(3, 2.5 * len(plottable))),
        squeeze=False,
    )
    fig.suptitle("แนวโน้มผลตรวจ Lab", fontsize=14, fontweight="bold", y=0.98)

    COLORS = ["#38bdf8", "#f472b6", "#a78bfa", "#22c55e", "#fb923c", "#e879f9"]

    for idx, trend in enumerate(plottable):
        ax = axes[idx][0]
        points = trend.data_points
        dates = []
        values = []
        for p in points:
            try:
                d = p.date if hasattr(p, "date") else p.get("date", "")
                v = p.value if hasattr(p, "value") else p.get("value", 0)
                if isinstance(d, str):
                    from datetime import datetime as _dtc
                    d = _dtc.fromisoformat(d.replace("Z", "+00:00"))
                dates.append(d)
                values.append(float(v))
            except (ValueError, TypeError, AttributeError):
                continue

        if len(dates) < 2:
            ax.text(0.5, 0.5, "ข้อมูลไม่เพียงพอ", transform=ax.transAxes,
                    ha="center", va="center", color="#94a3b8")
            continue

        color = COLORS[idx % len(COLORS)]
        ax.plot(dates, values, marker="o", color=color, linewidth=2, markersize=5)

        # Reference range shading
        ref = getattr(trend, "reference_range", None)
        if ref and len(ref) == 2:
            try:
                lo, hi = float(ref[0]), float(ref[1])
                ax.axhspan(lo, hi, alpha=0.15, color="#22c55e", label="ค่าปกติ")
            except (ValueError, TypeError):
                pass

        analyte = getattr(trend, "analyte", f"Lab {idx+1}")
        unit = getattr(trend, "unit", "")
        direction = getattr(trend, "direction", None)
        dir_sym = ""
        if direction:
            d_name = direction.name if hasattr(direction, "name") else str(direction)
            dir_sym = {"INCREASING": " ↑", "DECREASING": " ↓", "STABLE": " →"}.get(
                d_name, ""
            )

        ax.set_ylabel(f"{analyte} ({unit})" if unit else analyte, fontsize=10)
        ax.set_title(f"{analyte}{dir_sym}", fontsize=11, loc="left", color=color)
        ax.grid(True, alpha=0.2)
        ax.tick_params(labelsize=8)

        # Format dates
        if len(dates) > 5:
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))

    fig.tight_layout(rect=[0, 0, 1, 0.96])

    # Save to bytes
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor="#0f172a", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


@app.route("/admin/chart/<token>", methods=["GET"])
def admin_chart_download(token):
    """ดาวน์โหลดกราฟแนวโน้ม — token-based access (5 นาที)."""
    import hmac as _hmac_mod
    import hashlib as _hash_mod

    # Validate token: HMAC(secret, "chart:{timestamp}:{chart_id}")
    # Token format: {timestamp}:{chart_id}:{signature}
    parts = token.split(":", 2)
    if len(parts) != 3:
        return "Invalid token", 403

    ts_str, chart_id, sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return "Invalid token", 403

    # Check expiry (5 minutes)
    import time
    if abs(time.time() - ts) > 300:
        return "Token expired", 403

    # Verify HMAC
    key = (ADMIN_SECRET or "fallback").encode("utf-8")
    msg = f"chart:{ts_str}:{chart_id}".encode("utf-8")
    expected = _hmac_mod.new(key, msg, _hash_mod.sha256).hexdigest()[:16]
    if not _hmac_mod.compare_digest(sig, expected):
        return "Invalid token", 403

    # Load chart from temp storage
    chart_path = os.path.join("/tmp/ranong_raktai", f"chart_{chart_id}.png")
    if not os.path.isfile(chart_path):
        return "Chart not found", 404

    with open(chart_path, "rb") as f:
        data = f.read()

    # Clean up
    try:
        os.remove(chart_path)
    except OSError:
        pass

    from flask import Response
    return Response(data, mimetype="image/png")


def create_chart_token(chart_id: str) -> str:
    """สร้าง HMAC token สำหรับดาวน์โหลดกราฟ (อายุ 5 นาที)."""
    import hmac as _hmac_mod
    import hashlib as _hash_mod
    import time

    ts = str(int(time.time()))
    key = (ADMIN_SECRET or "fallback").encode("utf-8")
    msg = f"chart:{ts}:{chart_id}".encode("utf-8")
    sig = _hmac_mod.new(key, msg, _hash_mod.sha256).hexdigest()[:16]
    return f"{ts}:{chart_id}:{sig}"


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


# ─── Cold Start Prevention: Self-Ping Keep-Alive ─────────
# Render Free Tier จะปิด service หลังไม่มี traffic 15 นาที
# ระบบนี้จะ ping ตัวเองทุก 10 นาที เพื่อป้องกัน cold start
# หมายเหตุ: ไม่ใช้ external cron — ใช้ in-process thread ที่เบา

import threading

_KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "600"))  # 10 นาที
_KEEPALIVE_ENABLED = os.environ.get("KEEPALIVE_ENABLED", "true").lower() == "true"
_RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")


def _keepalive_worker():
    """Background thread: ping /health/live ทุก 10 นาที."""
    import time as _time
    while True:
        _time.sleep(_KEEPALIVE_INTERVAL)
        try:
            target_url = _RENDER_EXTERNAL_URL or f"http://localhost:{PORT}"
            resp = http_requests.get(
                f"{target_url}/health/live",
                timeout=15,
                headers={"User-Agent": "RanongRaktaiAI-KeepAlive/1.0"},
            )
            logger.debug(f"Keep-alive ping: {resp.status_code}")
        except Exception as e:
            logger.debug(f"Keep-alive ping failed: {type(e).__name__}")


if _KEEPALIVE_ENABLED and not IS_TESTING:
    _keepalive_thread = threading.Thread(
        target=_keepalive_worker,
        daemon=True,
        name="keepalive-ping",
    )
    _keepalive_thread.start()
    logger.info(
        f"Keep-alive: เปิดใช้งาน (ทุก {_KEEPALIVE_INTERVAL} วินาที)"
    )
else:
    logger.info("Keep-alive: ปิด (KEEPALIVE_ENABLED=false หรือ testing mode)")


# ═══════════════════════════════════════════════════════════
# [CS-1] Critical Alert Delivery Driver
#
# ปัญหาเดิม: AlertRetryManager.check_and_retry() มี call site เดียว
# อยู่ใน /health/ready แต่ไม่มีอะไรในระบบที่ deploy อยู่เรียก path นั้น
#   - Render health check เรียก /health      (render.yaml)
#   - keep-alive thread เรียก /health/live
#   - ไม่มี external cron (ตามคอมเมนต์เดิมในไฟล์นี้)
# ผลคือ retry และ escalation ของ alert วิกฤต "ไม่เคยทำงานเลย"
# alert ที่ส่งไม่สำเร็จค้างอยู่ใน delivery_failed ตลอดไป
#
# แก้: thread เฉพาะของตัวเอง รอบถี่กว่า keep-alive เพราะผลวิกฤต
# รอ 10 นาทีต่อรอบนานเกินไป
#
# ความปลอดภัยข้าม worker: Gunicorn รัน 2 workers ในคอนเทนเนอร์เดียว
# จึงแชร์ /tmp กัน ใช้ fcntl.flock แบบ non-blocking — worker ที่ไม่ได้
# lock ข้ามรอบนั้นไป กันการส่งซ้ำซึ่งทางคลินิกจะทำให้เข้าใจผิดว่าเป็น
# เหตุการณ์ใหม่ (directive §6 P0-A)
# ═══════════════════════════════════════════════════════════

_ALERT_DRIVER_INTERVAL = int(
    os.environ.get("ALERT_DRIVER_INTERVAL", "120")
)
_ALERT_DRIVER_ENABLED = os.environ.get(
    "ALERT_DRIVER_ENABLED", "true"
).lower() == "true"
_ALERT_DRIVER_LOCK = os.path.join(
    tempfile.gettempdir(), "ranong_raktai", "alert_driver.lock"
)


def _run_alert_delivery_tick() -> list[dict]:
    """รัน retry/escalation หนึ่งรอบ โดยถือ lock ข้าม worker

    คืน list ผลลัพธ์ (ว่างได้) — ไม่ raise ออกไปหา caller
    """
    if alert_retry_manager is None:
        return []
    os.makedirs(os.path.dirname(_ALERT_DRIVER_LOCK), exist_ok=True)
    lock_fd = None
    try:
        lock_fd = open(_ALERT_DRIVER_LOCK, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            # worker อื่นกำลังทำรอบนี้อยู่ — ข้ามไป ไม่ใช่ข้อผิดพลาด
            return []
        try:
            results = alert_retry_manager.check_and_retry(line_push_fn)
            if results:
                logger.info(
                    "Alert driver: ดำเนินการ %d รายการ", len(results)
                )
            return results or []
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
    except Exception as e:
        logger.warning(
            "Alert driver tick failed: %s", type(e).__name__
        )
        return []
    finally:
        if lock_fd is not None:
            try:
                lock_fd.close()
            except Exception:
                pass


def _alert_driver_worker():
    """Background thread: ขับ retry/escalation ของ alert วิกฤต."""
    import time as _time
    while True:
        _time.sleep(_ALERT_DRIVER_INTERVAL)
        _run_alert_delivery_tick()


if _ALERT_DRIVER_ENABLED and not IS_TESTING and alert_retry_manager is not None:
    _alert_driver_thread = threading.Thread(
        target=_alert_driver_worker,
        daemon=True,
        name="alert-delivery-driver",
    )
    _alert_driver_thread.start()
    logger.info(
        "Alert delivery driver: เปิดใช้งาน (ทุก %d วินาที)",
        _ALERT_DRIVER_INTERVAL,
    )
else:
    logger.warning(
        "Alert delivery driver: ปิด — retry/escalation ของ alert วิกฤต "
        "จะไม่ทำงานอัตโนมัติ"
    )


# ═══════════════════════════════════════════════════════════
# Main (สำหรับทดสอบ local เท่านั้น — Render ใช้ Gunicorn)
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("Running in development mode (Flask dev server)")
    app.run(host="0.0.0.0", port=PORT, debug=False)
