#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE OA Webhook Server — คลินิกระนอง รักษ์ไต (Cloud Version)
RANONG-RAKTAI-AI Production-Grade Intelligent Command Center

Deploy: Render Managed Cloud (Gunicorn, Docker, Singapore)
Storage: Google Cloud Storage (GCS)
Messaging: LINE OA Webhook (Messaging API)

═══════════════════════════════════════════════════════════════
 FILE STRUCTURE MAP — server.py (~14,800 lines)
═══════════════════════════════════════════════════════════════

 Lines 1-340:      Imports, Config, Constants
 Lines 340-520:    Flask App, CORS, EnrollmentTokenStore
 Lines 520-1220:   Storage Classes (Local, S3, GCS), get_storage()
 Lines 1220-1270:  Module Init (DLQ, Monitor, Registries)
 Lines 1270-1810:  DocumentIndex Class
 Lines 1810-2140:  AuditLogger, Phase 2A/2B Module Init
 Lines 2140-2730:  Admin API: Compare, HN Registry, Master Registry
 Lines 2730-2840:  Classification Helpers (masking, shapes)
 Lines 2840-3090:  Admin API: Alerts, Labs Re-extract
 Lines 3090-3430:  Admin Registry Page (HTML)
 Lines 3430-3690:  Document Reading, OCR, Search Helpers
 Lines 3690-3835:  Download Proxy (HMAC Token), Flex Message Builder
 Lines 3835-4260:  Physician Order System (annotate, order API)
 Lines 4260-4570:  Order Ack, Audit Log Viewer, Uptime Status
 Lines 4570-4770:  Google Integration Endpoints
 Lines 4770-5330:  Patient Dashboard (resolve, API, HTML page)
 Lines 5330-5470:  Authorization Helpers (is_authorized, is_physician)
 Lines 5470-7560:  LINE Command Handlers (search, lab, rx, compare,
                   summary, register, KPI, monitoring)
 Lines 7560-7840:  Rate Limiting, Deduplication (StateStore, DedupGuard)
 Lines 7840-7930:  File Validation (magic bytes, type detection)
 Lines 7930-8250:  PDF/OCR Text Extraction (multi-pass, Thai+Eng)
 Lines 8250-8640:  LINE API (push, reply, download, metadata)
 Lines 8640-9310:  Classification (keyword matching, vendor detect)
 Lines 9310-9860:  process_file_async (document processing pipeline)
 Lines 9860-10700: Admin Notification (Flex Message, push to admins)
 Lines 10700-11000: Webhook Handler + Download Proxy Route
 Lines 11000-11600: Legacy Admin Endpoints (backfill, debug)
 Lines 11600-12300: Admin API (stats, normalize-folders, retroactive)
 Lines 12300-12500: Clinical Dashboard, LIFF
 Lines 12500-12600: Dashboard V2 Chart
 Lines 12600-13200: System Health, Dead-letter Endpoints
 Lines 13200-13600: Dead-letter Admin Page
 Lines 13600-14000: Startup: Keep-alive, Alert Driver
 Lines 14000-14800: App Runner (__main__)

═══════════════════════════════════════════════════════════════
 EXTERNAL MODULES (แยกไฟล์แล้ว)
═══════════════════════════════════════════════════════════════
 quarterly_compare.py    — Lab/Rx/Dialysis history + comparison
 lab_extractor.py        — Lab result extraction (regex)
 lab_models.py           — LabResult, LabReport dataclasses
 lab_alert.py            — Critical lab value alerts
 lab_tracker.py          — Lab tracking over time
 alert_delivery.py       — LINE push alert delivery + retry
 medication_review.py    — Drug interaction checking
 nephrology_drug_db.py   — Nephrology drug database (275 drugs)
 nephrology_kpi.py       — KPI dashboard for nephrology
 clinical_analyzer.py    — Clinical analysis engine
 clinical_reports.py     — Monthly/weekly report generation
 patient_identity.py     — Patient name/HN extraction from OCR
 patient_hn_registry.py  — LINE user → HN mapping
 patient_master_registry.py — Clinic-wide patient registry
 vendor_registry.py      — Bill vendor detection + routing
 monitoring_schedule.py  — Lab monitoring schedule
 ckd_progression.py      — CKD stage tracking
 audit_trail.py          — Audit logging (PDPA)
 ocr_safety.py           — OCR clinical safety checks
 physician_approval.py   — Lab result approval workflow
 health_check.py         — Health check endpoints
 dashboard.py            — Dashboard V2 web UI
 ocr_review.py           — OCR review admin UI
 lab_dashboard.py        — Lab dashboard web UI
 line_patient_assistant.py — LINE patient assistant
 document_workflow.py    — Document approval workflow
 line_bot_enhancer.py    — LINE command parser
 auto_report.py          — Automated report generation
 clinical_dashboard.py   — Clinical dashboard web UI
 liff_app.py             — LIFF (LINE Front-end Framework) app
 dead_letter.py          — Dead letter queue for failed docs
 system_monitor.py       — System health monitoring
 google_drive_sync.py    — GCS → Google Drive backup
 google_sheets_sync.py   — Patient data → Google Sheets export
 gmail_report.py         — Email report delivery

═══════════════════════════════════════════════════════════════

กฎความปลอดภัย:
  - ตรวจสอบ LINE Webhook Signature ทุกครั้ง
  - ตรวจสอบชนิดไฟล์จริง (magic bytes) ก่อนประมวลผล
  - ไม่ลบไฟล์ต้นฉบับ
  - ไม่แก้ไขข้อมูลทางการแพทย์
  - EXTERNAL_AI_CLASSIFICATION_ALLOWED = False (hardcoded)
  - ไม่ส่งข้อมูลผู้ป่วยลง logs
  - PDPA compliant
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
from datetime import datetime, timedelta, timezone, date as _date_cls
from pathlib import Path

# Thai timezone constant — used throughout for date/time calculations
BKK_TZ = timezone(timedelta(hours=7))

# Base URL — use RENDER_EXTERNAL_URL ENV, fallback to hardcoded
_RENDER_BASE = os.getenv("RENDER_EXTERNAL_URL", "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")

from flask import Flask, request, abort, jsonify, redirect, make_response
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
    extract_patient_name_from_document,
    extract_document_date,
    _sanitize_folder_name as _sanitize_patient_folder_name,
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
    compare_lab_timeline,
    compare_rx_timeline,
    compare_dialysis_timeline,
    get_patient_lab_history,
    get_patient_rx_history,
    get_patient_dialysis_history,
    _DIALYSIS_SECTIONS,
    _dialysis_param_thai,
    _format_timeline_value,
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

# ชื่อประเภทเอกสารภาษาไทย — ใช้ที่เดียวทั้งไฟล์ (ข้อความ LINE, Flex, dashboard)
_CAT_NAMES = {
    "bills": "ใบเสร็จ/บิล",
    "rx": "ใบสั่งยา",
    "lab": "ผลตรวจแล็บ",
    "dialysis": "เอกสารฟอกเลือด",
    "review": "รอตรวจสอบ",
}
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

# ─── Rich Menu IDs (populated by ตั้งค่าเมนู command) ─────
_RICH_MENU_IDS: dict[str, str] = {}  # {"staff": "...", "patient": "...", "sub": "..."}

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

# เพดานต่อผู้ส่ง — นับทุกรูป/ไฟล์ (2026-11-02 แพทย์อนุมัติเพิ่มจาก 5/30/100
# เพื่อให้ส่งชุดละ 8 รูปและเจ้าหน้าที่ส่งคนไข้ทั้งวันได้ โดยยังกันบัญชี
# แปลกปลอมยิงรูปถล่ม) ตั้ง env ทับได้
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "20"))
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "150"))
RATE_LIMIT_PER_DAY = int(os.getenv("RATE_LIMIT_PER_DAY", "600"))

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

# ─── HD Smart Monitoring Module (Feature Flag) ─────────────────
from hd_module import register_hd_module, sync_patients_to_hd_db, create_default_hd_users
register_hd_module(app)

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

    def list_prefix(self, prefix: str) -> list[str]:
        """path เต็มของไฟล์ทุกไฟล์ใต้ prefix (เช่น '_alerts/') — ใช้กับ get(path) ได้ทันที
        list_files() รับเฉพาะชื่อหมวด (inbox/lab/...) ถ้าส่ง prefix จะได้โฟลเดอร์ 06_AI_REVIEW แทน"""
        base = os.path.join(BASE_DIR, prefix)
        out = []
        if os.path.isdir(base):
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    out.append(os.path.relpath(os.path.join(root, fn), BASE_DIR).replace(os.sep, "/"))
        return sorted(out)


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

    def list_prefix(self, prefix: str) -> list[str]:
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

    def list_prefix(self, prefix: str) -> list[str]:
        """ชื่อ blob เต็มทุกตัวใต้ prefix (เช่น '_alerts/') — ใช้กับ get(path) ได้ทันที"""
        if not self.is_ready:
            return []
        try:
            return [b.name for b in self._client.list_blobs(self.bucket_name, prefix=prefix)
                    if b.name != prefix and not b.name.endswith("/")]
        except Exception as e:
            logger.error(f"GCS list_prefix ล้มเหลว: {type(e).__name__}")
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

# ─── Dead Letter Queue ─────────────────────────────────────
try:
    from dead_letter import DeadLetterQueue, MAX_RETRIES as DLQ_MAX_RETRIES
    dead_letter_queue = DeadLetterQueue(storage)
    logger.info("Dead-letter queue initialized")
except Exception as _dlq_init_err:
    dead_letter_queue = None
    logger.warning(
        "Dead-letter queue init failed: %s",
        type(_dlq_init_err).__name__,
    )

# ─── Expense Tracker ──────────────────────────────────────
try:
    from expense_tracker import ExpenseTracker
    expense_tracker = ExpenseTracker(storage)
    logger.info("Expense tracker initialized")
except Exception as _et_init_err:
    expense_tracker = None
    logger.warning(
        "Expense tracker init failed: %s",
        type(_et_init_err).__name__,
    )

# ─── System Monitor ────────────────────────────────────────
_system_monitor = None  # initialized after _alert_recipients

# ─── HN Registry: จัดการรหัส HN คลินิก + HN โรงพยาบาล ────
hn_registry = HNRegistry()
if STORAGE_BACKEND == "gcs" and hasattr(storage, '_bucket'):
    # ส่ง storage wrapper (มี .put() method) แทน _bucket โดยตรง
    # เพื่อใช้ code path เดียวกับระบบอื่น → ไม่ติด permission issue
    hn_registry.set_storage(storage)
    logger.info("HNRegistry initialized with GCS storage wrapper")
    # V4.0: set_storage ย้ายไปท้ายไฟล์ (หลัง modules ถูก define)
    logger.info("HNRegistry initialized — V4.0 modules will init later")

# ─── Master Patient Registry: ทะเบียนผู้ป่วยกลาง ─────────
master_registry = MasterPatientRegistry()
if STORAGE_BACKEND == "gcs" and hasattr(storage, '_bucket'):
    master_registry.set_storage(storage._bucket)
    logger.info("MasterPatientRegistry initialized with GCS bucket")

# ─── One-time Dialysis Seed Import (141 patients from Excel) ─────
# รันครั้งเดียวตอน boot: ถ้าผู้ป่วยยังไม่มี dialysis_schedule → import จากไฟล์ seed
_SEED_FILE = os.path.join(os.path.dirname(__file__), "dialysis_seed_data.json")
if master_registry and os.path.exists(_SEED_FILE):
    _summ = master_registry.get_dialysis_summary()
    if _summ.get("with_schedule", 0) == 0:
        try:
            with open(_SEED_FILE, encoding="utf-8") as _sf:
                _seed = json.load(_sf)
            _seed_pts = _seed if isinstance(_seed, list) else _seed.get("patients", [])
            if _seed_pts:
                _new, _upd, _skip, _msgs = master_registry.import_dialysis_batch(
                    _seed_pts, registered_by="excel_seed"
                )
                logger.info(
                    "Dialysis seed import: new=%d updated=%d skipped=%d — %s",
                    _new, _upd, _skip, _msgs,
                )
        except Exception as _e:
            logger.error("Dialysis seed import failed: %s", _e)
    else:
        logger.info("Dialysis seed: skipped — %d patients already have schedule",
                     _summ["with_schedule"])

# ─── Share master_registry with HD module via app config ──
app.config["MASTER_REGISTRY"] = master_registry

# ─── HD Patient Sync: GCS Registry → PostgreSQL ─────────
sync_patients_to_hd_db(master_registry)
create_default_hd_users()

# ─── HD Alert Push + Vital Recorder: initialized later (after push_message def) ──
hd_alert_push = None
hd_vital_recorder = None

# ─── PostgreSQL Database (Phase 1.1 Foundation) ──────────
from database import Database
db = Database(os.getenv("DATABASE_URL", "") or os.getenv("HD_DATABASE_URL", ""))
if db.is_ready:
    logger.info("PostgreSQL: เชื่อมต่อสำเร็จ — structured queries พร้อมใช้")
elif db.status == "not_configured":
    logger.info("PostgreSQL: ยังไม่ได้ตั้ง DATABASE_URL — ใช้ GCS เดิม")
else:
    logger.warning("PostgreSQL: %s — fallback to GCS", db.error)

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

def _strip_gs_uri(path: str) -> str:
    """ทำ path ของ object ให้เป็นรูปเดียว: ตัด gs://<bucket>/ ออก

    ดัชนีต้องเก็บ path เปล่า (05_.../file.jpg) เพราะตัวสร้างลิงก์เปิดไฟล์
    ใช้ path นี้เป็นชื่อ object ตรง ๆ — path รูป gs:// ทำให้ลิงก์ 404
    (พบจริง 2026-11-02: ใบที่ถูก "จัดแฟ้มย้อนหลัง" แตะ เปิดไม่ได้)
    """
    p = (path or "").strip()
    if p.startswith("gs://"):
        return p.split("/", 3)[3] if p.count("/") >= 3 else ""
    return p


def _doc_recency_key(doc: dict) -> str:
    """เวลาเข้าระบบของเอกสาร สำหรับเรียง "ล่าสุด"

    ลำดับความน่าเชื่อถือ:
      indexed_at     — เขียนตอนเพิ่มเข้า index (เอกสารใหม่ตั้งแต่ 2026-11-02)
      _blob_created  — เวลาสร้างไฟล์ index บน GCS (เอกสารเก่า)
      submitted_at   — เวลาส่ง/วันที่เอกสาร (บางกรณีเป็นวันที่ในใบ)
      upload_date    — วัน (ไม่มีเวลา)
    ISO 8601 เทียบเป็นสตริงได้ · เอกสารที่ไม่มีอะไรเลยจะอยู่ท้ายสุด (เก่าสุด)
    """
    return (
        doc.get("indexed_at")
        or doc.get("_blob_created")
        or doc.get("submitted_at")
        or doc.get("upload_date")
        or ""
    )


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
        # cache เนื้อหา per-doc file ตามชื่อ blob — ไฟล์ใน _index/docs/
        # ถูกสร้างครั้งเดียวและไม่เคยถูกเขียนทับ (SA create-only, การแก้ไข
        # เขียนเป็นไฟล์ชื่อใหม่ _v<ts>.json) ชื่อเดิม = เนื้อหาเดิมเสมอ
        # จึงดาวน์โหลดเฉพาะชื่อที่ยังไม่เคยเห็น  เดิมดาวน์โหลดทุกไฟล์ใหม่
        # ทุก 5 วินาที (113 ไฟล์ ≈ 5–10 วิ/ครั้ง) ทำให้ /health ตอบช้าเกิน
        # 5 วิ → Render ฆ่า instance ซ้ำ ๆ ทั้งวัน และเอกสารที่กำลังรับหาย
        self._blob_cache: dict[str, dict | None] = {}

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
                _downloaded = 0
                for blob in blobs:
                    try:
                        if blob.name in self._blob_cache:
                            doc = self._blob_cache[blob.name]
                            if doc is None:
                                continue  # ไฟล์เสีย — ข้ามโดยไม่โหลดซ้ำ
                            doc = dict(doc)  # สำเนา — กัน mutation ปนเข้า cache
                        else:
                            content = blob.download_as_text(encoding="utf-8")
                            _downloaded += 1
                            doc = json.loads(content)
                            if str(doc.get("gcs_object_path", "")).startswith("gs://"):
                                doc["gcs_object_path"] = _strip_gs_uri(
                                    doc["gcs_object_path"]
                                )
                            # เวลาที่ไฟล์ index ถูกสร้างบน GCS — ใช้เรียง
                            # "ล่าสุด" สำหรับเอกสารเก่าที่ยังไม่มี indexed_at
                            _tc = getattr(blob, "time_created", None)
                            if _tc and "_blob_created" not in doc:
                                try:
                                    doc["_blob_created"] = _tc.isoformat()
                                except Exception:
                                    pass
                            self._blob_cache[blob.name] = doc
                        doc_id = doc.get("document_id", "")
                        if doc_id:
                            if doc_id in new_map:
                                # per-doc file overrides legacy entry
                                old_doc = new_map[doc_id]
                                # ไฟล์เวอร์ชันหลัง (update_fields) ต้องคง
                                # เวลาเข้าระบบครั้งแรก ไม่ใช่เวลาที่แก้ไข
                                _old_key = _doc_recency_key(old_doc)
                                if _old_key and _old_key < _doc_recency_key(doc):
                                    if old_doc.get("indexed_at"):
                                        doc["indexed_at"] = old_doc["indexed_at"]
                                    doc["_blob_created"] = _old_key
                                idx = new_docs.index(old_doc)
                                new_docs[idx] = doc
                            else:
                                new_docs.append(doc)
                            new_map[doc_id] = doc
                    except json.JSONDecodeError as blob_err:
                        # เนื้อหาเสียถาวร (ไฟล์ไม่เปลี่ยน) → จำไว้ ไม่โหลดซ้ำ
                        self._blob_cache[blob.name] = None
                        logger.warning(
                            f"Per-doc index file อ่านไม่ได้: "
                            f"{blob.name} — {type(blob_err).__name__}"
                        )
                        continue
                    except Exception as blob_err:
                        # network/transient → ไม่ cache จะลองใหม่รอบหน้า
                        logger.warning(
                            f"Per-doc index file อ่านไม่ได้: "
                            f"{blob.name} — {type(blob_err).__name__}"
                        )
                        continue
                if _downloaded:
                    logger.info(
                        "Document index: โหลดไฟล์ใหม่ %d ไฟล์ (cache %d)",
                        _downloaded, len(self._blob_cache),
                    )
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

            # เรียงตามเวลาเข้าระบบ (เก่า → ใหม่) — GCS list คืนตามชื่อไฟล์
            # (hash) ไม่ใช่เวลา ทำให้ "รายการล่าสุด" หลัง restart ผิดลำดับ
            # (2026-11-02: ใบฟอกเลือดที่เพิ่งส่งไม่โผล่ แต่บิลเมื่อวานโผล่)
            # ทุกจุดที่ใช้ reversed(self._docs) = ใหม่ก่อน จึงถูกต้องทันที
            new_docs.sort(key=_doc_recency_key)

            with self._lock:
                # เอกสารที่ถูก "ซ่อน" (เช่น ซ้ำ — ผ่าน approval) ไม่อยู่ใน
                # รายการที่ทุกส่วนของระบบมองเห็น แต่ยังอยู่ใน _doc_map
                # เพื่อให้ get()/กันซ้ำถาวร ยังทำงาน  ไฟล์จริงใน GCS ไม่ถูกลบ
                self._docs = [d for d in new_docs if not d.get("hidden")]
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

    def find_by_content_sha(self, owner_hash: str, sha256_hex: str) -> dict | None:
        """หาเอกสารของผู้ส่งคนเดียวกันที่มีเนื้อหา (sha256) ตรงกัน 100%.

        ใช้กันเอกสารซ้ำแบบถาวร (เสริม DedupGuard ที่มี TTL 10 นาที)
        ตรวจทั้ง content_sha256 ของเอกสาร และ page_sha256s ของเอกสาร
        ที่รวมจากหลายหน้า — ถ่ายใหม่จะได้ sha ใหม่ จึงไม่กันกรณีตั้งใจส่งคนละรอบ
        """
        if not owner_hash or not sha256_hex:
            return None
        self._ensure_fresh()
        # รวมเอกสารที่ถูกซ่อนด้วย — ซ่อนแล้วก็ยังไม่ควรรับซ้ำเข้ามาอีก
        for d in reversed(list(self._doc_map.values())):
            if d.get("owner_hash") != owner_hash:
                continue
            if d.get("content_sha256") == sha256_hex:
                return d
            if sha256_hex in (d.get("page_sha256s") or ()):
                return d
        return None

    def find_duplicates_in_folder(self, hn: str) -> list[list[dict]]:
        """หาเอกสารซ้ำ (เนื้อหาตรงกัน 100%) ในแฟ้มผู้ป่วยเดียวกัน.

        จับกลุ่มด้วย content_sha256 / page_sha256s  คืน list ของกลุ่ม
        แต่ละกลุ่มเรียงจากเก่า→ใหม่ (ตัวแรก = ต้นฉบับที่ควรเก็บ)
        ไม่รวมเอกสารที่ซ่อนไปแล้ว
        """
        self._ensure_fresh()
        hn = (hn or "").strip()
        if not hn:
            return []
        groups: dict[str, list[dict]] = {}
        for d in self._docs:
            folder = d.get("patient_hn_folder", "") or ""
            if not (folder == hn or folder.startswith(hn + "_")):
                continue
            keys = set()
            if d.get("content_sha256"):
                keys.add(d["content_sha256"])
            for ps in d.get("page_sha256s") or []:
                keys.add(ps)
            for k in keys:
                groups.setdefault(k, []).append(d)
        seen: set[str] = set()
        out: list[list[dict]] = []
        for docs in groups.values():
            if len(docs) < 2:
                continue
            ids = tuple(sorted(x.get("document_id", "") for x in docs))
            key = "|".join(ids)
            if key in seen:
                continue
            seen.add(key)
            out.append(sorted(docs, key=lambda x: x.get("upload_date", "")))
        return out

    def set_hidden(self, doc_id: str, hidden: bool, reason: str = "") -> bool:
        """ซ่อน/เลิกซ่อนเอกสาร (logical delete) — ไฟล์ต้นฉบับใน GCS ไม่ถูกแตะ.

        เขียน versioned per-doc JSON (create-only) เพราะ SA ไม่มีสิทธิ์
        overwrite/delete  loader จะเลือกไฟล์เวอร์ชันล่าสุดโดยอัตโนมัติ
        """
        self._ensure_fresh()
        doc = self._doc_map.get(doc_id)
        if doc is None:
            return False
        doc["hidden"] = bool(hidden)
        doc["hidden_reason"] = (reason or "")[:120] if hidden else ""
        doc["hidden_at"] = (
            datetime.now(timezone.utc).isoformat() if hidden else ""
        )
        with self._lock:
            if hidden:
                self._docs = [d for d in self._docs if d.get("document_id") != doc_id]
            elif all(d.get("document_id") != doc_id for d in self._docs):
                self._docs.append(doc)
        if self._storage and getattr(self._storage, "is_ready", False):
            try:
                _ts = int(time.time())
                blob_path = f"{INDEX_DOC_PREFIX}{doc_id}_v{_ts}.json"
                blob = self._storage._bucket.blob(blob_path)
                blob.upload_from_string(
                    json.dumps(doc, ensure_ascii=False),
                    content_type="application/json",
                    if_generation_match=0,
                )
            except Exception as e:
                logger.warning(
                    f"Document index set_hidden GCS write failed: "
                    f"{type(e).__name__}"
                )
                return False
        logger.info(f"Document index: doc={doc_id[:8]}… hidden={hidden}")
        return True

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

    def list_by_patient_folder(
        self, patient_folder: str, category: str = "", limit: int = 100
    ) -> list[dict]:
        """รายการเอกสารตาม patient_hn_folder — สำหรับ admin/physician.

        ค้นจาก field patient_hn_folder ก่อน ถ้าไม่มีให้ fallback
        ดูจาก gcs_object_path (folder อยู่ segment ที่ 4)
        """
        self._ensure_fresh()
        results = []
        pf_upper = patient_folder.upper()
        for doc in self._docs:
            # ── ตรวจจาก patient_hn_folder field ──
            doc_folder = doc.get("patient_hn_folder", "")
            if not doc_folder:
                # fallback: extract from gcs_object_path
                # format: folder/YYYY-QN/YYYY-MM/patient_folder/uuid.pdf
                gcs_path = doc.get("gcs_object_path", "")
                parts = gcs_path.split("/")
                if len(parts) >= 5:
                    doc_folder = parts[3]
            if not doc_folder:
                continue
            # match: exact or prefix (e.g. "56001" matches "56001_ชื่อ")
            df_upper = doc_folder.upper()
            if df_upper != pf_upper and not df_upper.startswith(pf_upper + "_"):
                continue
            if category and doc.get("category") != category:
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
        extra_fields: dict | None = None,
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
        for k, v in (extra_fields or {}).items():
            doc[k] = v
        # เขียน GCS เป็นไฟล์เวอร์ชันใหม่ (create-only) — ไม่เขียนทับ
        #
        # เดิมเขียนทับ {doc_id}.json ซึ่ง Service Account ทำไม่ได้ (403:
        # มีสิทธิ์ create เท่านั้น) ผลคือการย้ายแฟ้ม/เปลี่ยนประเภท
        # เปลี่ยนแค่ใน memory ของ worker เดียว แล้วหายเมื่อ reload —
        # ผู้ใช้เห็น "ย้ายสำเร็จ" แต่ของจริงไม่ย้าย  ใช้แบบเดียวกับ
        # set_hidden: {doc_id}_v<ts>.json ซึ่ง loader เลือกตัวล่าสุดเอง
        #
        # ต้องใช้ document_id จริงของเอกสารที่หาเจอ ไม่ใช่ค่าที่ส่งเข้ามา
        # เพราะเมธอดนี้ยอมให้ค้นด้วยคำนำหน้า (startswith)
        _real_doc_id = doc.get("document_id", "") or doc_id
        if self._storage.is_ready:
            try:
                _ts = int(time.time())
                blob_path = f"{INDEX_DOC_PREFIX}{_real_doc_id}_v{_ts}.json"
                doc_json = json.dumps(doc, ensure_ascii=False)
                blob = self._storage._bucket.blob(blob_path)
                blob.upload_from_string(
                    doc_json, content_type="application/json",
                    if_generation_match=0,
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
                return False
        return True

    def update_fields(self, doc_id: str, fields: dict) -> bool:
        """อัปเดต field ของเอกสารแบบ versioned (create-only) — ใช้แทนการ
        เขียนทับ {doc_id}.json ซึ่ง SA ทำไม่ได้ (403)"""
        self._ensure_fresh()
        doc = self._doc_map.get(doc_id)
        if doc is None:
            return False
        for k, v in (fields or {}).items():
            if k == "gcs_object_path":
                v = _strip_gs_uri(v)  # ดัชนีเก็บ path เปล่าเสมอ
            doc[k] = v
        if not self._storage.is_ready:
            return False
        try:
            _ts = int(time.time() * 1000)
            blob = self._storage._bucket.blob(
                f"{INDEX_DOC_PREFIX}{doc_id}_v{_ts}.json"
            )
            blob.upload_from_string(
                json.dumps(doc, ensure_ascii=False),
                content_type="application/json",
                if_generation_match=0,
            )
            return True
        except Exception as e:
            logger.warning("Document index update_fields failed: %s", type(e).__name__)
            return False

    @staticmethod
    def derive_patient_folder(doc: dict) -> str:
        """แฟ้มผู้ป่วยของเอกสาร: field patient_hn_folder หรือเดาจาก path
        (folder/YYYY-QN/YYYY-MM/<HN>_<ชื่อ>/file) — เอกสารเก่าที่ถูกย้ายเข้า
        แฟ้มผู้ป่วยย้อนหลัง ไม่มี field นี้"""
        f = (doc.get("patient_hn_folder") or "").strip()
        if f:
            return f
        p = doc.get("gcs_object_path", "") or ""
        if p.startswith("gs://"):
            p = p.split("/", 3)[3] if p.count("/") >= 3 else ""
        parts = p.split("/")
        if len(parts) >= 5 and re.match(r"^[A-Za-z0-9-]{3,20}(_|$)", parts[3]):
            return parts[3]
        return ""

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

# ─── System Monitor (init after _alert_recipients) ─────
try:
    from system_monitor import SystemMonitor
    _system_monitor = SystemMonitor(
        storage, doc_index, dead_letter_queue, _alert_recipients
    )
    logger.info("System monitor initialized")
except Exception as _sm_err:
    _system_monitor = None
    logger.warning(
        "System monitor init failed: %s",
        type(_sm_err).__name__,
    )

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
    auth = request.headers.get("X-Admin-Secret", "")
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
    auth = request.headers.get("X-Admin-Secret", "")
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
    auth = request.headers.get("X-Admin-Secret", "")
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
    auth = request.headers.get("X-Admin-Secret", "")
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
    auth = request.headers.get("X-Admin-Secret", "")
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
    auth = request.headers.get("X-Admin-Secret", "")
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
    auth = request.headers.get("X-Admin-Secret", "")
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
    auth = request.headers.get("X-Admin-Secret", "")
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
    auth = request.headers.get("X-Admin-Secret", "")
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
    auth = request.headers.get("X-Admin-Secret", "")
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
    auth = request.headers.get("X-Admin-Secret", "")
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


# ─── Dialysis Profile API (เพิ่ม 2026-11-05) ─────────────

@app.route("/admin/api/v2/dialysis/import", methods=["POST"])
def api_dialysis_import():
    """นำเข้าข้อมูลไตเทียมจำนวนมาก — Admin only

    Body: {"patients": [{"clinic_hn", "hospital_hn", "patient_name",
                          "dialysis_schedule", "hd_per_week", "esa_code",
                          "vascular_access"}]}
    """
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    patients_data = data.get("patients", [])
    if not patients_data:
        return jsonify({"error": "ต้องระบุ patients (list of dict)"}), 400

    new_c, upd_c, skip_c, msgs = master_registry.import_dialysis_batch(
        patients_data, registered_by="admin_api"
    )
    # ── Dual-write: PostgreSQL (Phase 1.1) ──
    _db_synced = 0
    if db and db.is_ready:
        for _pd in patients_data:
            try:
                db.upsert_patient(_pd)
                _db_synced += 1
            except Exception:
                pass
    return jsonify({
        "success": True,
        "new": new_c,
        "updated": upd_c,
        "skipped": skip_c,
        "db_synced": _db_synced,
        "messages": msgs,
    })


# ─── Dialysis Import Page (Web UI) ──────────────────────────
@app.route("/admin/dialysis-import")
def admin_dialysis_import_page():
    """หน้าเว็บสำหรับ Import ข้อมูลผู้ป่วยไตเทียมจาก Excel — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return "ADMIN_SECRET not configured", 500
    auth = request.cookies.get("admin_token", "") or request.args.get("token", "")
    if auth != admin_secret:
        return "Unauthorized — กรุณาเข้าผ่าน Admin Portal", 401

    current_count = master_registry.count() if master_registry else 0
    html = f'''<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Import ข้อมูลผู้ป่วยไตเทียม — Ranong Raktai AI</title>
<style>
:root {{
  --bg: #f5f7fa; --card: #fff; --fg: #1a1a2e; --muted: #6b7280;
  --primary: #0d6efd; --primary-dark: #0b5ed7; --success: #198754;
  --warning: #ffc107; --danger: #dc3545; --border: #dee2e6;
  --radius: 12px; --shadow: 0 2px 12px rgba(0,0,0,.08);
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #111827; --card: #1f2937; --fg: #f3f4f6; --muted: #9ca3af;
    --border: #374151; --shadow: 0 2px 12px rgba(0,0,0,.3); }}
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: var(--bg); color: var(--fg); padding: 20px; max-width: 960px; margin: 0 auto; }}
h1 {{ font-size: 1.5rem; margin-bottom: 8px; }}
.subtitle {{ color: var(--muted); font-size: .9rem; margin-bottom: 24px; }}
.card {{ background: var(--card); border-radius: var(--radius); padding: 24px;
  box-shadow: var(--shadow); margin-bottom: 20px; }}
.stat {{ display: inline-block; background: var(--primary); color: #fff;
  padding: 4px 12px; border-radius: 20px; font-size: .85rem; margin-bottom: 16px; }}
.drop-zone {{ border: 2px dashed var(--border); border-radius: var(--radius);
  padding: 40px 20px; text-align: center; cursor: pointer; transition: .2s; }}
.drop-zone:hover, .drop-zone.drag-over {{ border-color: var(--primary);
  background: rgba(13,110,253,.05); }}
.drop-zone input {{ display: none; }}
.drop-zone .icon {{ font-size: 3rem; margin-bottom: 8px; }}
.btn {{ display: inline-flex; align-items: center; gap: 6px;
  padding: 10px 24px; border-radius: 8px; border: none; cursor: pointer;
  font-size: .95rem; font-weight: 600; transition: .2s; }}
.btn-primary {{ background: var(--primary); color: #fff; }}
.btn-primary:hover {{ background: var(--primary-dark); }}
.btn-primary:disabled {{ opacity: .5; cursor: not-allowed; }}
.btn-danger {{ background: var(--danger); color: #fff; }}
.btn-danger:hover {{ opacity: .85; }}
table {{ width: 100%; border-collapse: collapse; font-size: .85rem; }}
th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); }}
th {{ background: rgba(13,110,253,.06); font-weight: 600; position: sticky; top: 0; }}
.table-wrap {{ max-height: 400px; overflow-y: auto; border-radius: 8px;
  border: 1px solid var(--border); }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px;
  font-size: .75rem; font-weight: 600; }}
.badge-new {{ background: #d1fae5; color: #065f46; }}
.badge-update {{ background: #fef3c7; color: #92400e; }}
.badge-skip {{ background: #fee2e2; color: #991b1b; }}
.progress-bar {{ width: 100%; height: 8px; background: var(--border);
  border-radius: 4px; overflow: hidden; margin: 12px 0; }}
.progress-fill {{ height: 100%; background: var(--primary); transition: width .3s; }}
.result {{ padding: 16px; border-radius: 8px; margin-top: 16px; }}
.result-success {{ background: #d1fae5; color: #065f46; }}
.result-error {{ background: #fee2e2; color: #991b1b; }}
.hidden {{ display: none; }}
.summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px; margin: 16px 0; }}
.summary-item {{ text-align: center; padding: 12px; background: rgba(13,110,253,.04);
  border-radius: 8px; }}
.summary-item .num {{ font-size: 1.8rem; font-weight: 700; color: var(--primary); }}
.summary-item .label {{ font-size: .8rem; color: var(--muted); }}
</style>
</head>
<body>
<h1>📥 Import ข้อมูลผู้ป่วยไตเทียม</h1>
<p class="subtitle">อัปโหลดไฟล์ Excel ตารางฟอกเลือดและติดตามยา ESA</p>

<div class="card">
  <span class="stat">🏥 ผู้ป่วยในระบบปัจจุบัน: {current_count} คน</span>
  <div class="drop-zone" id="dropZone" onclick="document.getElementById('fileInput').click()">
    <div class="icon">📄</div>
    <p><strong>คลิกเลือกไฟล์ หรือลากไฟล์มาวาง</strong></p>
    <p style="color:var(--muted);font-size:.85rem;margin-top:8px">
      รองรับ .xlsx, .xls — ตารางฟอกเลือด (คอลัมน์: ลำดับ, ชื่อ, HN รพ., HN คลินิก, รอบ, HD/สัปดาห์, ..., ESA)</p>
    <input type="file" id="fileInput" accept=".xlsx,.xls">
  </div>
</div>

<div id="previewSection" class="card hidden">
  <h2 style="margin-bottom:12px">📋 ตรวจสอบข้อมูลก่อน Import</h2>
  <div id="fileSummary"></div>
  <div class="summary-grid" id="summaryGrid"></div>
  <div class="table-wrap" id="tableWrap"></div>
  <div style="margin-top:16px;display:flex;gap:12px">
    <button class="btn btn-primary" id="btnImport" onclick="doImport()">
      ✅ ยืนยัน Import
    </button>
    <button class="btn btn-danger" onclick="resetAll()">❌ ยกเลิก</button>
  </div>
</div>

<div id="progressSection" class="card hidden">
  <h2>⏳ กำลัง Import...</h2>
  <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
  <p id="progressText" style="color:var(--muted);font-size:.9rem"></p>
</div>

<div id="resultSection" class="hidden"></div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"></script>
<script>
const ADMIN_SECRET = document.cookie.split(';')
  .map(c=>c.trim()).find(c=>c.startsWith('admin_token='));
const AUTH_TOKEN = ADMIN_SECRET ? ADMIN_SECRET.split('=')[1] : '';
const API_URL = '/admin/api/v2/dialysis/import';

let parsedPatients = [];

// Column mapping (0-indexed): ลำดับ(0), ชื่อ(1), HN รพ.(2), HN คลินิก(3),
// รอบ(4), HD/สัปดาห์(5), col6-11=other, ESA(12), กะ(13), เตียง(14)
function parseExcelRow(row) {{
  const name = row[1];
  if (!name || String(name).trim() === '') return null;
  const clinicHn = cleanHn(row[3]);
  const hospitalHn = cleanHn(row[2]);
  if (!clinicHn && !hospitalHn) return null;
  return {{
    clinic_hn: clinicHn,
    hospital_hn: hospitalHn,
    patient_name: String(name).trim(),
    dialysis_schedule: row[4] ? String(row[4]).trim() : '',
    hd_per_week: parseInt(row[5]) || 0,
    esa_code: row[12] ? String(row[12]).trim() : '',
    vascular_access: ''
  }};
}}

function cleanHn(val) {{
  if (val == null) return '';
  if (typeof val === 'number') return String(Math.floor(val));
  return String(val).trim().split('.')[0];
}}

// Drag & drop
const dz = document.getElementById('dropZone');
dz.addEventListener('dragover', e => {{ e.preventDefault(); dz.classList.add('drag-over'); }});
dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
dz.addEventListener('drop', e => {{
  e.preventDefault(); dz.classList.remove('drag-over');
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
}});
document.getElementById('fileInput').addEventListener('change', e => {{
  if (e.target.files.length) handleFile(e.target.files[0]);
}});

function handleFile(file) {{
  if (!file.name.match(/\\.xlsx?$/i)) {{
    alert('กรุณาเลือกไฟล์ Excel (.xlsx หรือ .xls)');
    return;
  }}
  const reader = new FileReader();
  reader.onload = function(e) {{
    try {{
      const wb = XLSX.read(e.target.result, {{type:'array'}});
      const ws = wb.Sheets[wb.SheetNames[0]];
      const rows = XLSX.utils.sheet_to_json(ws, {{header:1, defval:''}});
      // Skip header row
      parsedPatients = [];
      for (let i = 1; i < rows.length; i++) {{
        const p = parseExcelRow(rows[i]);
        if (p) parsedPatients.push(p);
      }}
      showPreview(file.name);
    }} catch(err) {{
      alert('❌ อ่านไฟล์ไม่ได้: ' + err.message);
    }}
  }};
  reader.readAsArrayBuffer(file);
}}

function showPreview(fileName) {{
  document.getElementById('previewSection').classList.remove('hidden');

  // ESA summary
  const esaCounts = {{}};
  const schedCounts = {{}};
  parsedPatients.forEach(p => {{
    if (p.esa_code) esaCounts[p.esa_code] = (esaCounts[p.esa_code]||0) + 1;
    if (p.dialysis_schedule) schedCounts[p.dialysis_schedule] = (schedCounts[p.dialysis_schedule]||0) + 1;
  }});

  document.getElementById('fileSummary').innerHTML =
    `<p>📄 <strong>${{fileName}}</strong></p>`;

  const grid = document.getElementById('summaryGrid');
  grid.innerHTML = `
    <div class="summary-item"><div class="num">${{parsedPatients.length}}</div><div class="label">ผู้ป่วยทั้งหมด</div></div>
    <div class="summary-item"><div class="num">${{Object.keys(esaCounts).length}}</div><div class="label">ชนิด ESA</div></div>
    <div class="summary-item"><div class="num">${{Object.keys(schedCounts).length}}</div><div class="label">รูปแบบรอบฟอก</div></div>
  `;

  // Table
  let tableHtml = '<table><thead><tr><th>#</th><th>HN คลินิก</th><th>HN รพ.</th><th>ชื่อ-สกุล</th><th>รอบฟอก</th><th>HD/wk</th><th>ESA</th></tr></thead><tbody>';
  parsedPatients.forEach((p, i) => {{
    tableHtml += `<tr>
      <td>${{i+1}}</td>
      <td>${{p.clinic_hn||'-'}}</td>
      <td>${{p.hospital_hn||'-'}}</td>
      <td>${{p.patient_name}}</td>
      <td>${{p.dialysis_schedule||'-'}}</td>
      <td>${{p.hd_per_week||'-'}}</td>
      <td>${{p.esa_code||'-'}}</td>
    </tr>`;
  }});
  tableHtml += '</tbody></table>';
  document.getElementById('tableWrap').innerHTML = tableHtml;
}}

async function doImport() {{
  if (!parsedPatients.length) return;
  if (!confirm(`ยืนยัน Import ผู้ป่วย ${{parsedPatients.length}} คน?`)) return;

  const btn = document.getElementById('btnImport');
  btn.disabled = true;
  document.getElementById('progressSection').classList.remove('hidden');
  document.getElementById('progressFill').style.width = '30%';
  document.getElementById('progressText').textContent = 'กำลังส่งข้อมูล...';

  try {{
    const resp = await fetch(API_URL, {{
      method: 'POST',
      headers: {{
        'Content-Type': 'application/json',
        'X-Admin-Secret': AUTH_TOKEN
      }},
      body: JSON.stringify({{ patients: parsedPatients }})
    }});
    document.getElementById('progressFill').style.width = '100%';
    const data = await resp.json();

    const rs = document.getElementById('resultSection');
    rs.classList.remove('hidden');
    if (data.success) {{
      rs.innerHTML = `<div class="result result-success">
        <h3>✅ Import สำเร็จ!</h3>
        <div class="summary-grid" style="margin-top:12px">
          <div class="summary-item"><div class="num">${{data.new}}</div><div class="label">เพิ่มใหม่</div></div>
          <div class="summary-item"><div class="num">${{data.updated}}</div><div class="label">อัปเดต</div></div>
          <div class="summary-item"><div class="num">${{data.skipped}}</div><div class="label">ข้าม</div></div>
        </div>
        <p style="margin-top:12px">${{(data.messages||[]).join('<br>')}}</p>
      </div>`;
      document.getElementById('progressSection').classList.add('hidden');
    }} else {{
      rs.innerHTML = `<div class="result result-error">
        <h3>❌ Import ไม่สำเร็จ</h3>
        <p>${{data.error || JSON.stringify(data)}}</p>
      </div>`;
    }}
  }} catch(err) {{
    const rs = document.getElementById('resultSection');
    rs.classList.remove('hidden');
    rs.innerHTML = `<div class="result result-error">
      <h3>❌ เกิดข้อผิดพลาด</h3>
      <p>${{err.message}}</p>
    </div>`;
  }}
  btn.disabled = false;
}}

function resetAll() {{
  parsedPatients = [];
  document.getElementById('previewSection').classList.add('hidden');
  document.getElementById('progressSection').classList.add('hidden');
  document.getElementById('resultSection').classList.add('hidden');
  document.getElementById('fileInput').value = '';
}}
</script>
</body>
</html>'''
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/admin/api/v2/dialysis/profile", methods=["GET"])
def api_dialysis_profile():
    """ดูข้อมูลไตเทียมของผู้ป่วยรายคน — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    hn = request.args.get("hn", "").strip()
    if not hn:
        return jsonify({"error": "ต้องระบุ ?hn=HN"}), 400

    patient = master_registry.match_hn(hn)
    if not patient:
        return jsonify({"error": f"ไม่พบ HN: {hn}"}), 404

    from patient_master_registry import THAI_DAY_DISPLAY
    days_thai = "/".join(
        THAI_DAY_DISPLAY.get(d, d) for d in (patient.dialysis_days or [])
    )
    return jsonify({
        "clinic_hn": patient.clinic_hn,
        "hospital_hn": patient.hospital_hn,
        "patient_name": patient.patient_name,
        "dialysis_schedule": patient.dialysis_schedule,
        "dialysis_days": patient.dialysis_days,
        "dialysis_days_thai": days_thai,
        "hd_per_week": patient.hd_per_week,
        "esa_code": patient.esa_code,
        "esa_medication": patient.esa_medication,
        "esa_dose": patient.esa_dose,
        "vascular_access": patient.vascular_access,
        "shift": patient.shift,
        "bed_number": patient.bed_number,
    })


@app.route("/admin/api/v2/dialysis/update", methods=["POST"])
def api_dialysis_update():
    """อัปเดตข้อมูลไตเทียม — Admin only

    Body: {"hn": "...", "dialysis_schedule": "จ/พ/ศ",
           "esa_code": "E4", "vascular_access": "AVF", ...}
    """
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    hn = data.get("hn", "").strip()
    if not hn:
        return jsonify({"error": "ต้องระบุ hn"}), 400

    success, msg = master_registry.update_dialysis_profile(
        hn=hn,
        dialysis_schedule=data.get("dialysis_schedule", ""),
        hd_per_week=int(data.get("hd_per_week", 0) or 0),
        esa_code=data.get("esa_code", ""),
        vascular_access=data.get("vascular_access", ""),
        shift=data.get("shift", ""),
        bed_number=data.get("bed_number", ""),
    )
    return jsonify({"success": success, "message": msg}), 200 if success else 400


@app.route("/admin/api/v2/dialysis/schedule", methods=["GET"])
def api_dialysis_schedule():
    """ดูตารางฟอกตามวัน — Admin only

    ?day=mon|tue|wed|thu|fri|sat — ดูวันที่ระบุ
    ไม่ระบุ → ดูสรุปทั้งสัปดาห์
    """
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    day = request.args.get("day", "").strip().lower()

    if day:
        patients = master_registry.get_patients_by_day(day)
        from patient_master_registry import THAI_DAY_DISPLAY
        return jsonify({
            "day": day,
            "day_thai": THAI_DAY_DISPLAY.get(day, day),
            "count": len(patients),
            "patients": [
                {
                    "clinic_hn": p.clinic_hn,
                    "patient_name": p.patient_name,
                    "hd_per_week": p.hd_per_week,
                    "esa_code": p.esa_code,
                    "esa_medication": p.esa_medication,
                    "vascular_access": p.vascular_access,
                }
                for p in patients
            ],
        })
    else:
        summary = master_registry.get_dialysis_summary()
        return jsonify(summary)


@app.route("/admin/api/v2/dialysis/list", methods=["GET"])
def api_dialysis_list():
    """รายชื่อผู้ป่วยพร้อมข้อมูลไตเทียม — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    patients = master_registry.list_all()
    return jsonify({
        "count": len(patients),
        "patients": [p.to_dict() for p in patients],
    })


@app.route("/admin/api/v2/dialysis/esa-report", methods=["GET"])
def api_dialysis_esa_report():
    """รายงานยา ESA รายเดือน — จำนวนคนไข้, โดสต่อเดือน, ค่าใช้จ่ายโดยประมาณ"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    from patient_master_registry import ESA_CODE_MAP
    # ค่ายาโดยประมาณ (บาท/โดส)
    _ESA_COST = {
        "E4": 350, "H4": 400, "HM": 300, "HM4": 300,
        "H10": 900, "H4+H10": 1300,
        "eposis": 250, "Eposis": 250, "hypercrit": 200,
    }
    patients = master_registry.list_all()
    by_esa: dict = {}
    for p in patients:
        code = p.esa_code or "ไม่ระบุ"
        if code not in by_esa:
            info = ESA_CODE_MAP.get(code, {})
            by_esa[code] = {
                "esa_code": code,
                "medication_name": info.get("name", code),
                "dose_per_session": info.get("dose", 0),
                "unit": info.get("unit", "unit"),
                "cost_per_dose": _ESA_COST.get(code, 0),
                "patient_count": 0,
                "total_sessions_per_week": 0,
            }
        by_esa[code]["patient_count"] += 1
        by_esa[code]["total_sessions_per_week"] += p.hd_per_week or 0

    result = []
    grand_monthly = 0
    for code, d in sorted(by_esa.items(), key=lambda x: -x[1]["patient_count"]):
        sessions_month = d["total_sessions_per_week"] * 4.33  # avg weeks/month
        cost_month = sessions_month * d["cost_per_dose"]
        grand_monthly += cost_month
        result.append({
            **d,
            "sessions_per_month": round(sessions_month, 1),
            "cost_per_month": round(cost_month, 0),
        })

    return jsonify({
        "esa_drugs": result,
        "total_patients": len(patients),
        "total_monthly_cost": round(grand_monthly, 0),
        "note": "ค่าใช้จ่ายเป็นค่าประมาณเท่านั้น",
    })


# ─── Hb tracking helper (เพิ่ม 2026-11-05) ────────────────

_HB_PATTERN = re.compile(
    r'(?:'
    r'(?:Hb|Hemoglobin|HGB|ฮีโมโกลบิน|Hgb)'
    r'[\s:=]*'
    r')'
    r'(\d{1,2}(?:\.\d{1,2})?)'
    r'(?:\s*(?:g/dL|g/dl|gm/dl|กรัม)?)?',
    re.IGNORECASE,
)


def _extract_hb_from_text(text: str) -> float | None:
    """ดึงค่า Hb จาก extracted_text ของผลแล็บ

    Returns: ค่า Hb (g/dL) หรือ None ถ้าไม่พบ
    """
    if not text:
        return None
    m = _HB_PATTERN.search(text)
    if m:
        val = float(m.group(1))
        if 3.0 <= val <= 25.0:  # ช่วงค่า Hb ที่เป็นไปได้
            return val
    return None


def _get_hb_history(hn: str, limit: int = 10) -> list[dict]:
    """ดึงประวัติ Hb จากเอกสาร lab ของผู้ป่วย

    Returns: list of {date, value, doc_id} เรียงจากเก่า→ใหม่
    """
    patient = master_registry.match_hn(hn)
    if not patient:
        return []

    # ค้นเอกสาร lab ของผู้ป่วย (ใช้ shared helper)
    lab_docs = _get_patient_lab_docs(patient, patient.clinic_hn, limit=50)

    results = []
    for doc in lab_docs:
        text = doc.get("extracted_text", "")
        hb_val = _extract_hb_from_text(text)
        if hb_val is not None:
            results.append({
                "date": doc.get("upload_date", doc.get("submitted_at", "?")),
                "value": hb_val,
                "doc_id": doc.get("document_id", ""),
            })

    # เรียงตามวันที่ (เก่า → ใหม่) แล้วตัดตามลิมิต
    results.sort(key=lambda x: x["date"])
    return results[-limit:]


def _hb_trend(history: list[dict]) -> str:
    """คำนวณ trend จากประวัติ Hb

    Returns: 'rising' | 'stable' | 'falling' | 'unknown'
    """
    if len(history) < 2:
        return "unknown"
    recent = [h["value"] for h in history[-3:]]
    if len(recent) >= 2:
        diff = recent[-1] - recent[0]
        if diff > 0.5:
            return "rising"
        elif diff < -0.5:
            return "falling"
    return "stable"


def _hb_assessment(value: float) -> str:
    """ประเมินค่า Hb ตาม KDIGO guidelines"""
    if value < 10.0:
        return "ต่ำ"
    elif value <= 12.0:
        return "เป้าหมาย"
    else:
        return "สูง"


# ── Shared helpers — ลดโค้ดซ้ำซ้อน (Phase 1 Consolidation) ──

_LAB_EXTRACT_PATTERNS = [
    ("k", [r"K\+?\s*[=:]\s*([\d.]+)", r"Potassium\s*[=:]\s*([\d.]+)"]),
    ("albumin", [r"Albumin\s*[=:]\s*([\d.]+)", r"Alb\s*[=:]\s*([\d.]+)"]),
    ("ca", [r"Ca\s*[=:]\s*([\d.]+)", r"Calcium\s*[=:]\s*([\d.]+)"]),
    ("po4", [r"PO4\s*[=:]\s*([\d.]+)", r"Phosph\w*\s*[=:]\s*([\d.]+)"]),
]


def _extract_latest_labs(extracted_text: str) -> dict:
    """ดึงค่า lab (Hb, K+, Albumin, Ca, PO4) จาก extracted_text ของเอกสาร

    Returns: dict เช่น {"hb": 10.5, "k": 4.8, "albumin": 3.8}
    เดิม regex block นี้ถูก copy-paste ไป 3 ที่ใน server.py
    """
    result = {}
    if not extracted_text:
        return result

    for name, patterns in _LAB_EXTRACT_PATTERNS:
        for pat in patterns:
            m = re.search(pat, extracted_text, re.IGNORECASE)
            if m:
                try:
                    result[name] = float(m.group(1))
                except ValueError:
                    pass
                break

    hb_v = _extract_hb_from_text(extracted_text)
    if hb_v:
        result["hb"] = hb_v

    return result


def _get_patient_lab_docs(patient, hn: str, limit: int = 5) -> list:
    """ดึงเอกสาร lab ของผู้ป่วย โดยลอง folder_name ก่อน แล้ว fallback เป็น HN

    Args:
        patient: MasterPatientRegistry entry (ต้องมี .folder_name)
        hn: HN ที่ใช้เป็น fallback
        limit: จำนวนเอกสารสูงสุด

    Returns: list of doc metadata
    เดิม pattern นี้ถูกเขียนซ้ำ 4 ที่ใน server.py
    """
    docs = doc_index.list_by_patient_folder(
        patient.folder_name, category="lab", limit=limit
    )
    if not docs:
        docs = doc_index.list_by_patient_folder(
            hn, category="lab", limit=limit
        )
    return docs or []


@app.route("/admin/api/v2/dialysis/hb-trend", methods=["GET"])
def api_dialysis_hb_trend():
    """ดูแนวโน้ม Hb กับ ESA ของผู้ป่วย — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ADMIN_SECRET not configured"}), 500
    auth = request.headers.get("X-Admin-Secret", "")
    if auth != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    hn = request.args.get("hn", "").strip()
    if not hn:
        return jsonify({"error": "ต้องระบุ ?hn=HN"}), 400

    patient = master_registry.match_hn(hn)
    if not patient:
        return jsonify({"error": f"ไม่พบ HN: {hn}"}), 404

    history = _get_hb_history(hn)
    trend = _hb_trend(history)
    trend_emoji = {"rising": "↑", "falling": "↓", "stable": "→"}.get(trend, "?")

    return jsonify({
        "clinic_hn": patient.clinic_hn,
        "patient_name": patient.patient_name,
        "esa_code": patient.esa_code,
        "esa_medication": patient.esa_medication,
        "esa_dose": patient.esa_dose,
        "hb_history": history,
        "trend": trend,
        "trend_emoji": trend_emoji,
        "latest_hb": history[-1]["value"] if history else None,
        "latest_assessment": _hb_assessment(history[-1]["value"]) if history else None,
    })


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
    if auth and hmac.compare_digest(
        auth.encode("utf-8", "surrogateescape"),
        admin_secret.encode("utf-8"),
    ):
        return None
    # หน้าเว็บ admin ที่ login ด้วยฟอร์มแล้ว: cookie HttpOnly (SameSite=Lax, path=/admin)
    # — ดูข้อมูลผ่าน JS ของหน้านั้นได้โดยไม่ต้องฝัง secret ใน URL (13 พ.ย. 2569)
    # อนุญาตเฉพาะ GET (อ่าน) และคำขอ same-origin เท่านั้น; POST/DELETE ยังต้องใช้ header
    if request.method == "GET" and _admin_cookie_ok():
        _origin = request.headers.get("Sec-Fetch-Site", "same-origin")
        if _origin in ("same-origin", "none"):
            return None
    return jsonify({"error": "Unauthorized"}), 401


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
            bank_accounts=data.get("bank_accounts") or [],
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
    for key in ("add_aliases", "remove_aliases", "keywords", "add_bank_accounts"):
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


@app.route("/admin/api/v2/vendors/relocation-execute", methods=["POST"])
def api_vendors_relocation_execute():
    """ดำเนินการย้ายบิลตามแผน — คัดลอก GCS object + อัปเดต index.

    ไม่ลบไฟล์ต้นฉบับ เพื่อให้ rollback ได้
    จำกัด batch ละไม่เกิน 50 รายการ

    Body JSON: {"confirm": true} — ต้องยืนยัน
    """
    err = _admin_api_auth()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    if not body.get("confirm"):
        return jsonify({
            "error": "ต้องส่ง {\"confirm\": true} เพื่อยืนยัน",
            "note": "ดูแผนก่อนที่ GET /admin/api/v2/vendors/relocation-plan",
        }), 400

    limit = min(int(body.get("limit", 50)), 50)
    moved = 0
    errors_list = []

    try:
        doc_index._ensure_fresh()
        for d in doc_index._docs:
            if moved >= limit:
                break
            if d.get("category") != "bills":
                continue
            raw = d.get("gcs_object_path", "")
            current = raw.replace(f"gs://{STORAGE_BUCKET}/", "")
            if not current:
                continue

            match = resolve_bill_vendor(d.get("extracted_text", ""))
            if match.needs_review:
                continue

            filename = current.rsplit("/", 1)[-1]
            proposed = build_document_path(
                FOLDERS["bills"], filename, "bills",
                subfolder=match.folder,
                date=_doc_submitted_date(d),
            )
            if proposed == current:
                continue

            # คัดลอก GCS object ไปที่ใหม่ (ไม่ลบต้นฉบับ)
            try:
                src_blob = storage._bucket.blob(current)
                storage._bucket.copy_blob(
                    src_blob, storage._bucket, proposed
                )
                # อัปเดต index
                d["gcs_object_path"] = proposed
                if match.folder:
                    d["bill_company"] = match.folder
                    d["vendor_method"] = match.method
                moved += 1
            except Exception as cp_err:
                errors_list.append({
                    "doc": d.get("document_id", "")[:8],
                    "error": type(cp_err).__name__,
                })

        # บันทึก index ที่อัปเดตแล้ว
        if moved > 0:
            doc_index._save()
            logger.info(
                "Vendor relocation: moved %d bills", moved
            )

    except Exception as e:
        logger.error("relocation-execute failed: %s", type(e).__name__)
        return jsonify({"error": type(e).__name__}), 500

    return jsonify({
        "success": True,
        "moved": moved,
        "errors": errors_list,
        "note": "ไฟล์ต้นฉบับไม่ถูกลบ",
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




@app.route("/admin/registry", methods=["GET", "POST"])
def admin_registry_page():
    """หน้าเว็บทะเบียนผู้ป่วย — cookie auth (set via ?secret= redirect)"""
    auth_err = _admin_page_auth()
    if auth_err is not None:
        return auth_err
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
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


# ─── Dashboard ไตเทียม (เพิ่ม 2026-11-05 — แพทย์อนุมัติ) ─────────

@app.route("/admin/dialysis-dashboard")
def admin_dialysis_dashboard():
    """หน้าเว็บ Dashboard ไตเทียม — ตารางฟอก, ยา ESA, ค่าใช้จ่าย"""
    return '''<!DOCTYPE html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ไตเทียม Dashboard — คลินิกระนอง รักษ์ไต</title>
<style>
:root{--bg:#f8fafc;--card:#fff;--fg:#1e293b;--fg2:#64748b;--accent:#0ea5e9;--accent2:#06b6d4;
--border:#e2e8f0;--ok:#22c55e;--warn:#f59e0b;--err:#ef4444;--row-alt:#f1f5f9}
@media(prefers-color-scheme:dark){:root{--bg:#0f172a;--card:#1e293b;--fg:#f1f5f9;--fg2:#94a3b8;
--border:#334155;--row-alt:#1e293b}}
*{box-sizing:border-box;margin:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--fg);padding:0}
.app{max-width:1100px;margin:0 auto;padding:16px}
.header{background:linear-gradient(135deg,#0ea5e9,#06b6d4);color:#fff;padding:20px;border-radius:12px;margin-bottom:16px}
.header h1{font-size:1.4em;margin-bottom:4px}
.header .sub{opacity:.85;font-size:.9em}
.auth-box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;text-align:center;margin:40px 0}
.auth-box input{padding:10px 16px;font-size:15px;border:1px solid var(--border);border-radius:8px;width:260px;margin:8px}
.auth-box button{padding:10px 24px;font-size:15px;border:none;border-radius:8px;background:var(--accent);color:#fff;cursor:pointer}
.tabs{display:flex;gap:4px;margin-bottom:16px;flex-wrap:wrap}
.tab{padding:10px 18px;border-radius:8px 8px 0 0;cursor:pointer;background:var(--card);border:1px solid var(--border);
border-bottom:none;font-size:.9em;color:var(--fg2);transition:.15s}
.tab.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.panel{display:none;background:var(--card);border:1px solid var(--border);border-radius:0 8px 8px 8px;padding:20px}
.panel.active{display:block}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;text-align:center}
.stat .val{font-size:1.8em;font-weight:700;color:var(--accent)}
.stat .lbl{font-size:.82em;color:var(--fg2);margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:.88em}
th{background:var(--accent);color:#fff;padding:10px 8px;text-align:left;position:sticky;top:0}
td{padding:8px;border-bottom:1px solid var(--border)}
tr:nth-child(even){background:var(--row-alt)}
.esa-bar{display:flex;align-items:center;gap:8px;margin:6px 0}
.esa-bar .bar{height:22px;border-radius:4px;min-width:2px;transition:.3s}
.esa-bar .lbl{font-size:.85em;min-width:80px}
.esa-bar .cnt{font-size:.85em;color:var(--fg2)}
.week-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}
.day-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center}
.day-card .day-name{font-weight:600;color:var(--accent);font-size:1.1em}
.day-card .day-count{font-size:2em;font-weight:700;color:var(--fg);margin:8px 0}
.day-card.today{border-color:var(--accent);box-shadow:0 0 0 2px rgba(14,165,233,.3)}
.cost-row{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border)}
.cost-row:last-child{border-bottom:none}
.cost-total{font-size:1.2em;font-weight:700;color:var(--accent);margin-top:12px;text-align:right}
.hidden{display:none!important}
@media(max-width:600px){.week-grid{grid-template-columns:repeat(3,1fr)} .stats{grid-template-columns:repeat(2,1fr)}}
</style></head><body>
<div class="app">
<div class="header">
  <h1>🩺 ไตเทียม Dashboard</h1>
  <div class="sub">คลินิกระนอง รักษ์ไต — ตารางฟอก · ยา ESA · ค่าใช้จ่าย</div>
</div>

<div class="auth-box" id="authBox">
  <div style="font-size:1.5em;margin-bottom:12px">🔐</div>
  <div>กรุณายืนยันตัวตน</div>
  <div style="margin-top:12px">
    <input type="password" id="secretInput" placeholder="ADMIN_SECRET" onkeydown="if(event.key==='Enter')doAuth()">
    <button onclick="doAuth()">เข้าสู่ระบบ</button>
  </div>
  <div id="authErr" style="color:var(--err);margin-top:8px"></div>
</div>

<div id="mainContent" class="hidden">
<div class="stats" id="statsBar">
  <div class="stat"><div class="val" id="sTotalPt">-</div><div class="lbl">ผู้ป่วยทั้งหมด</div></div>
  <div class="stat"><div class="val" id="sTodayPt">-</div><div class="lbl">ฟอกวันนี้</div></div>
  <div class="stat"><div class="val" id="sESATypes">-</div><div class="lbl">ชนิดยา ESA</div></div>
  <div class="stat"><div class="val" id="sMonthlyCost">-</div><div class="lbl">ค่ายา/เดือน (ประมาณ)</div></div>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('today')">📅 วันนี้</div>
  <div class="tab" onclick="showTab('week')">📊 รายสัปดาห์</div>
  <div class="tab" onclick="showTab('esa')">💊 สรุป ESA</div>
  <div class="tab" onclick="showTab('cost')">💰 ค่าใช้จ่าย</div>
</div>

<div class="panel active" id="panelToday">
  <h3 id="todayTitle" style="margin-bottom:12px">📅 ตารางฟอกวันนี้</h3>
  <table><thead><tr><th>#</th><th>HN</th><th>ชื่อ-สกุล</th><th>ESA</th><th>VA</th><th>HD/wk</th></tr></thead>
  <tbody id="todayBody"></tbody></table>
</div>

<div class="panel" id="panelWeek">
  <h3 style="margin-bottom:16px">📊 ตารางฟอกรายสัปดาห์</h3>
  <div class="week-grid" id="weekGrid"></div>
  <div id="dayDetail" style="margin-top:16px"></div>
</div>

<div class="panel" id="panelEsa">
  <h3 style="margin-bottom:16px">💊 สรุปยา ESA ทั้งหมด</h3>
  <div id="esaChart"></div>
  <table style="margin-top:16px"><thead><tr><th>รหัส</th><th>ชื่อยา</th><th>ขนาด</th><th>จำนวนคนไข้</th></tr></thead>
  <tbody id="esaBody"></tbody></table>
</div>

<div class="panel" id="panelCost">
  <h3 style="margin-bottom:16px">💰 ประมาณการค่ายา ESA รายเดือน</h3>
  <div id="costList"></div>
  <div class="cost-total" id="costTotal"></div>
  <div style="margin-top:12px;font-size:.82em;color:var(--fg2)">* ค่าใช้จ่ายเป็นค่าประมาณ อ้างอิงจากราคาเฉลี่ยต่อโดส</div>
</div>
</div>
</div>

<script>
const API = location.origin;
let SECRET = '';
const DAY_TH = {mon:'จันทร์',tue:'อังคาร',wed:'พุธ',thu:'พฤหัสบดี',fri:'ศุกร์',sat:'เสาร์'};
const DAY_ORDER = ['mon','tue','wed','thu','fri','sat'];
const TODAY_DAY = ['mon','tue','wed','thu','fri','sat','sun'][(new Date(new Date().toLocaleString('en-US',{timeZone:'Asia/Bangkok'})).getDay()+6)%7];

function hdr(){return {'X-Admin-Secret':SECRET,'Content-Type':'application/json'}}
async function api(path){const r=await fetch(API+path,{headers:hdr()});if(!r.ok)throw new Error(r.status);return r.json()}

async function doAuth(){
  SECRET=document.getElementById('secretInput').value;
  if(!SECRET){document.getElementById('authErr').textContent='กรุณาใส่ ADMIN_SECRET';return}
  try{
    const d=await api('/admin/api/v2/dialysis/schedule');
    document.getElementById('authBox').classList.add('hidden');
    document.getElementById('mainContent').classList.remove('hidden');
    loadAll();
  }catch(e){document.getElementById('authErr').textContent='❌ Secret ไม่ถูกต้อง หรือระบบไม่พร้อม'}
}

function showTab(name){
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',['today','week','esa','cost'][i]===name));
  document.querySelectorAll('.panel').forEach((p,i)=>p.classList.toggle('active',['today','week','esa','cost'][i]===name));
}

function fmt(n){return n?Number(n).toLocaleString('th-TH'):'-'}

async function loadAll(){
  // Today's patients
  try{
    const d=await api('/admin/api/v2/dialysis/schedule?day='+TODAY_DAY);
    document.getElementById('sTodayPt').textContent=d.count;
    document.getElementById('todayTitle').textContent='📅 ตารางฟอกวัน'+DAY_TH[TODAY_DAY]+' ('+d.count+' คน)';
    let html='';
    d.patients.forEach((p,i)=>{
      html+='<tr><td>'+(i+1)+'</td><td>'+p.clinic_hn+'</td><td>'+p.patient_name+'</td>'
        +'<td>'+(p.esa_code||'-')+'</td><td>'+(p.vascular_access||'-')+'</td>'
        +'<td>'+(p.hd_per_week||'-')+'</td></tr>';
    });
    document.getElementById('todayBody').innerHTML=html||'<tr><td colspan=6 style="text-align:center;padding:30px">วันนี้ไม่มีรอบฟอก</td></tr>';
  }catch(e){console.error(e)}

  // Summary
  try{
    const s=await api('/admin/api/v2/dialysis/schedule');
    document.getElementById('sTotalPt').textContent=s.total_patients;
    document.getElementById('sESATypes').textContent=Object.keys(s.esa_counts||{}).length;
    // Week grid
    let wh='';
    DAY_ORDER.forEach(d=>{
      const c=s.by_day[d]||0;
      const cls=d===TODAY_DAY?'day-card today':'day-card';
      wh+='<div class="'+cls+'" onclick="loadDay(\''+d+'\')"><div class="day-name">'+DAY_TH[d]+'</div><div class="day-count">'+c+'</div><div style="font-size:.8em;color:var(--fg2)">คน</div></div>';
    });
    document.getElementById('weekGrid').innerHTML=wh;
    // ESA chart
    const esa=s.esa_counts||{};
    const esaMax=Math.max(...Object.values(esa),1);
    const colors=['#0ea5e9','#06b6d4','#22c55e','#f59e0b','#ef4444','#8b5cf6','#ec4899','#64748b'];
    let eh='';
    Object.entries(esa).sort((a,b)=>b[1]-a[1]).forEach(([code,cnt],i)=>{
      const w=Math.round(cnt/esaMax*100);
      eh+='<div class="esa-bar"><div class="lbl">'+code+'</div><div class="bar" style="width:'+w+'%;background:'+colors[i%colors.length]+'"></div><div class="cnt">'+cnt+' คน</div></div>';
    });
    document.getElementById('esaChart').innerHTML=eh;
  }catch(e){console.error(e)}

  // ESA detail table
  try{
    const all=await api('/admin/api/v2/dialysis/list');
    const esaMap={};
    all.patients.forEach(p=>{
      const c=p.esa_code||'ไม่ระบุ';
      if(!esaMap[c])esaMap[c]={code:c,med:p.esa_medication||c,dose:p.esa_dose||0,count:0};
      esaMap[c].count++;
    });
    let th='';
    Object.values(esaMap).sort((a,b)=>b.count-a.count).forEach(e=>{
      th+='<tr><td>'+e.code+'</td><td>'+e.med+'</td><td>'+(e.dose?fmt(e.dose)+' unit':'-')+'</td><td>'+e.count+'</td></tr>';
    });
    document.getElementById('esaBody').innerHTML=th;
  }catch(e){console.error(e)}

  // Cost report
  try{
    const cr=await api('/admin/api/v2/dialysis/esa-report');
    document.getElementById('sMonthlyCost').textContent='฿'+fmt(cr.total_monthly_cost);
    let ch='';
    cr.esa_drugs.forEach(d=>{
      if(d.esa_code==='ไม่ระบุ')return;
      ch+='<div class="cost-row"><div><strong>'+d.medication_name+'</strong> ('+d.esa_code+')<br>'
        +'<span style="font-size:.82em;color:var(--fg2)">'+d.patient_count+' คน × '+fmt(d.sessions_per_month)+' โดส/เดือน × ฿'+fmt(d.cost_per_dose)+'</span></div>'
        +'<div style="font-weight:600;color:var(--accent)">฿'+fmt(d.cost_per_month)+'</div></div>';
    });
    document.getElementById('costList').innerHTML=ch;
    document.getElementById('costTotal').textContent='รวมประมาณ ฿'+fmt(cr.total_monthly_cost)+'/เดือน';
  }catch(e){console.error(e)}
}

async function loadDay(day){
  try{
    const d=await api('/admin/api/v2/dialysis/schedule?day='+day);
    let html='<h4>📅 วัน'+DAY_TH[day]+' ('+d.count+' คน)</h4><table><thead><tr><th>#</th><th>HN</th><th>ชื่อ</th><th>ESA</th><th>VA</th></tr></thead><tbody>';
    d.patients.forEach((p,i)=>{
      html+='<tr><td>'+(i+1)+'</td><td>'+p.clinic_hn+'</td><td>'+p.patient_name+'</td><td>'+(p.esa_code||'-')+'</td><td>'+(p.vascular_access||'-')+'</td></tr>';
    });
    html+='</tbody></table>';
    document.getElementById('dayDetail').innerHTML=html;
  }catch(e){console.error(e)}
}

document.getElementById('secretInput').focus();
</script></body></html>'''


@app.route("/admin/api/v2/master-registry/debug-gcs", methods=["GET"])
def api_master_registry_debug_gcs():
    """ทดสอบ GCS write โดยตรง — Admin only"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    auth = request.headers.get("X-Admin-Secret", "")
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
    cat_names = _CAT_NAMES  # ตารางชื่อประเภทรวมที่เดียว
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
        blob = storage._bucket.blob(_strip_gs_uri(gcs_object_path))
        # ไม่เรียก blob.exists() — SA อาจไม่มี objects.get permission
        # ถ้า blob ไม่มีจริง signed URL จะ return 404 เอง (ปลอดภัย)
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


# ─── Physician annotation / treatment order system ───────
# แพทย์กดปุ่ม "สั่งการรักษา" ใน LINE → เปิดหน้าเว็บที่แสดงเอกสาร
# + ฟอร์มเขียนคำสั่งรักษา → กดส่ง → ระบบ push ไปยังพยาบาลทาง LINE
# ─────────────────────────────────────────────────────────


def _resolve_nurse_line_ids() -> list[str]:
    """ดึง LINE user ID ของพยาบาลจากทะเบียนผู้รับ — สำหรับส่งคำสั่งรักษา

    ค้นจากทะเบียนผู้รับ (alert_delivery) เฉพาะ role=nurse ที่ active=True
    ถ้าไม่มีพยาบาลลงทะเบียน → fallback ส่งไปยัง physician ทั้งหมด
    (เพราะแพทย์อาจส่งให้พยาบาลที่อยู่ตรงหน้าเอง)
    ไม่ log LINE user ID
    """
    nurse_ids = []
    try:
        from alert_delivery import (
            load_recipient_registry, _lookup_enrolled_line_id,
        )
        registry = load_recipient_registry(storage)
        for r in registry.get("recipients", []):
            if not r.get("active", True):
                continue
            if r.get("role", "").lower() != "nurse":
                continue
            uid = _lookup_enrolled_line_id(storage, r.get("user_hash", ""))
            if uid:
                nurse_ids.append(uid)
    except Exception as e:
        logger.warning("ค้นหาพยาบาลไม่สำเร็จ: %s", type(e).__name__)

    if not nurse_ids:
        # Fallback: ส่งไปยัง physician recipients ทั้งหมด
        nurse_ids = list(_alert_recipients.get("physician", []))
    return nurse_ids


@app.route("/admin/doc/view/<doc_id>", methods=["GET", "POST"])  # POST = ฟอร์มกรอก ADMIN_SECRET
def admin_doc_view(doc_id):
    """ดูเอกสาร — ลิงก์ถาวรไม่หมดอายุ (สร้าง Signed URL ใหม่ทุกครั้งที่เปิด)

    Auth: cookie (set via ?secret= redirect) หรือ X-Admin-Secret header
    ลิงก์นี้ใช้ใน LINE Flex Message — เข้าครั้งแรกด้วย ?secret= แล้วระบบตั้ง cookie
    """
    auth_err = _admin_page_auth()
    if auth_err is not None:
        return auth_err

    doc_id = doc_id.strip()[:64]
    if not doc_id:
        return "รหัสเอกสารไม่ถูกต้อง", 400

    # ดึงข้อมูลเอกสารจาก DocumentIndex
    doc_meta = None
    if doc_index:
        doc_meta = doc_index.get(doc_id)
    if not doc_meta:
        return (
            "<html><body style='font-family:sans-serif;text-align:center;"
            "padding:40px'>"
            "<h2>ไม่พบเอกสาร</h2>"
            "<p>เอกสารนี้อาจถูกลบหรือรหัสไม่ถูกต้อง</p>"
            "</body></html>"
        ), 404

    gcs_path = doc_meta.get("gcs_object_path", "")
    if not gcs_path:
        return (
            "<html><body style='font-family:sans-serif;text-align:center;"
            "padding:40px'>"
            "<h2>ไม่พบไฟล์เอกสาร</h2>"
            "<p>ไม่มีข้อมูลตำแหน่งจัดเก็บ</p>"
            "</body></html>"
        ), 404

    # สร้าง Signed URL ใหม่ (อายุ 15 นาที) แล้ว redirect
    signed_url = generate_signed_url(gcs_path, expiry_seconds=SIGNED_URL_EXPIRY)
    if not signed_url:
        return (
            "<html><body style='font-family:sans-serif;text-align:center;"
            "padding:40px'>"
            "<h2>ไม่สามารถสร้างลิงก์ได้</h2>"
            "<p>กรุณาลองใหม่ในอีกสักครู่</p>"
            "</body></html>"
        ), 500

    return redirect(signed_url)


@app.route("/admin/annotate/<doc_id>", methods=["GET", "POST"])  # POST = ฟอร์มกรอก ADMIN_SECRET
def admin_annotate_page(doc_id):
    """หน้าเว็บสำหรับแพทย์สั่งการรักษา — เปิดจาก LINE Flex Message

    Auth: cookie (set via ?secret= redirect) หรือ X-Admin-Secret header
    """
    auth_err = _admin_page_auth()
    if auth_err is not None:
        return auth_err

    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    # Sanitize doc_id
    doc_id = doc_id.strip()[:64]
    if not doc_id:
        return "รหัสเอกสารไม่ถูกต้อง", 400

    # ดึงข้อมูลเอกสารจาก DocumentIndex
    doc_meta = None
    doc_download_url = None
    if doc_index:
        doc_meta = doc_index.get(doc_id)
    if doc_meta:
        gcs_path = doc_meta.get("gcs_object_path", "")
        if gcs_path:
            doc_download_url = get_download_url(gcs_path)

    cat_name = _CAT_NAMES.get(
        (doc_meta or {}).get("category", ""), "เอกสาร"
    )
    upload_date = (doc_meta or {}).get("upload_date", "ไม่ทราบ")
    patient_folder = (doc_meta or {}).get("patient_hn_folder", "")
    # ดึงข้อมูลผู้ป่วยจาก patient_folder
    patient_info = patient_folder.replace("_", " ") if patient_folder else ""
    storage_display = (doc_meta or {}).get("storage_display", "")

    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

    # Pre-compute values for JavaScript (escape single quotes)
    _cat_code = (doc_meta or {}).get("category", "").replace("'", "\\'")
    _patient_info_js = patient_info.replace("'", "\\'")

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>สั่งการรักษา</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f5f5f5; color: #333;
    padding: 16px; max-width: 600px; margin: 0 auto;
    padding-bottom: env(safe-area-inset-bottom, 16px);
}}
.card {{
    background: #fff; border-radius: 12px; padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,.08); margin-bottom: 16px;
}}
.card h2 {{ font-size: 18px; margin-bottom: 12px; color: #1a1a1a; }}
.doc-info {{ display: flex; flex-direction: column; gap: 8px; }}
.info-row {{ display: flex; justify-content: space-between; font-size: 14px; }}
.info-label {{ color: #888; }}
.info-value {{ color: #333; font-weight: 500; }}
.btn-view {{
    display: block; text-align: center; padding: 12px;
    background: #1DB446; color: #fff; border-radius: 8px;
    text-decoration: none; font-weight: 600; font-size: 15px;
    margin-top: 12px;
}}
.btn-view:active {{ background: #179c3c; }}
.order-form {{ display: flex; flex-direction: column; gap: 12px; }}
.order-form label {{ font-size: 14px; color: #555; font-weight: 500; }}
.order-form textarea {{
    width: 100%; min-height: 140px; border: 1.5px solid #ddd;
    border-radius: 8px; padding: 12px; font-size: 15px;
    font-family: inherit; resize: vertical;
}}
.order-form textarea:focus {{
    outline: none; border-color: #1DB446;
    box-shadow: 0 0 0 3px rgba(29,180,70,.15);
}}
.quick-btns {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 4px; }}
.quick-btn {{
    background: #f0f0f0; border: 1px solid #ddd; border-radius: 20px;
    padding: 6px 12px; font-size: 13px; cursor: pointer;
    transition: background .15s;
}}
.quick-btn:active {{ background: #e0e0e0; }}
.btn-send {{
    display: block; width: 100%; padding: 14px; border: none;
    background: #0066cc; color: #fff; border-radius: 8px;
    font-size: 16px; font-weight: 600; cursor: pointer;
}}
.btn-send:active {{ background: #0055aa; }}
.btn-send:disabled {{ background: #999; cursor: not-allowed; }}
.status {{
    text-align: center; padding: 12px; border-radius: 8px;
    margin-top: 12px; font-size: 14px; display: none;
}}
.status.success {{ background: #e8f5e9; color: #2e7d32; display: block; }}
.status.error {{ background: #fce4ec; color: #c62828; display: block; }}
.status.sending {{ background: #e3f2fd; color: #1565c0; display: block; }}
</style>
</head>
<body>

<div class="card">
    <h2>📋 ข้อมูลเอกสาร</h2>
    <div class="doc-info">
        <div class="info-row">
            <span class="info-label">ประเภท</span>
            <span class="info-value">{cat_name}</span>
        </div>
        <div class="info-row">
            <span class="info-label">วันที่</span>
            <span class="info-value">{upload_date}</span>
        </div>
        {"<div class='info-row'><span class='info-label'>ผู้ป่วย</span><span class='info-value'>" + patient_info + "</span></div>" if patient_info else ""}
        {"<div class='info-row'><span class='info-label'>จัดเก็บ</span><span class='info-value'>" + storage_display[:60] + "</span></div>" if storage_display else ""}
    </div>
    {"<a href='" + doc_download_url + "' target='_blank' class='btn-view'>📎 เปิดดูเอกสาร</a>" if doc_download_url else "<p style='color:#c62828;margin-top:12px;font-size:14px'>⚠️ ไม่สามารถสร้างลิงก์ดูเอกสาร — กดปุ่มเปิดเอกสารจาก LINE แทน</p>"}
</div>

<div class="card">
    <h2>✏️ สั่งการรักษา</h2>
    <form class="order-form" id="orderForm" onsubmit="return sendOrder(event)">
        <label>คำสั่งด่วน:</label>
        <div class="quick-btns">
            <span class="quick-btn" onclick="appendOrder('ปรับยา: ')">💊 ปรับยา</span>
            <span class="quick-btn" onclick="appendOrder('ปรับน้ำหนักแห้ง: ')">⚖️ ปรับน้ำหนัก</span>
            <span class="quick-btn" onclick="appendOrder('ปรับ UF goal: ')">💧 ปรับ UF</span>
            <span class="quick-btn" onclick="appendOrder('เจาะแล็บ: ')">🔬 เจาะแล็บ</span>
            <span class="quick-btn" onclick="appendOrder('นัดพบแพทย์: ')">📅 นัดพบแพทย์</span>
            <span class="quick-btn" onclick="appendOrder('หมายเหตุ: ')">📝 หมายเหตุ</span>
        </div>
        <label for="orderText">รายละเอียดคำสั่ง:</label>
        <textarea id="orderText" placeholder="เช่น ปรับ Losartan 50→100 mg 1x1&#10;ปรับน้ำหนักแห้ง 65→64 kg&#10;เจาะ CBC, BUN, Cr ก่อนฟอกครั้งหน้า" required></textarea>
        <button type="submit" class="btn-send" id="btnSend">📤 ส่งคำสั่งให้พยาบาล</button>
    </form>
    <div id="status"></div>
</div>

<script>
function appendOrder(text) {{
    var ta = document.getElementById('orderText');
    if (ta.value && !ta.value.endsWith('\\n')) ta.value += '\\n';
    ta.value += text;
    ta.focus();
}}

function sendOrder(e) {{
    e.preventDefault();
    var orderText = document.getElementById('orderText').value.trim();
    if (!orderText) return false;

    var btn = document.getElementById('btnSend');
    var status = document.getElementById('status');
    btn.disabled = true;
    btn.textContent = '⏳ กำลังส่ง...';
    status.className = 'status sending';
    status.style.display = 'block';
    status.textContent = 'กำลังส่งคำสั่งไปยังพยาบาล...';

    fetch('{base_url}/admin/api/v2/physician-order', {{
        method: 'POST',
        headers: {{
            'Content-Type': 'application/json',
            'X-Admin-Secret': '{admin_secret}'
        }},
        body: JSON.stringify({{
            doc_id: '{doc_id}',
            order_text: orderText,
            category: '{_cat_code}',
            patient_info: '{_patient_info_js}'
        }})
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
        if (data.success) {{
            status.className = 'status success';
            status.textContent = '✅ ' + data.message;
            btn.textContent = '✅ ส่งสำเร็จ';
            // ล้างฟอร์ม
            document.getElementById('orderText').value = '';
            setTimeout(function() {{
                btn.disabled = false;
                btn.textContent = '📤 ส่งคำสั่งให้พยาบาล';
            }}, 3000);
        }} else {{
            status.className = 'status error';
            status.textContent = '❌ ' + (data.error || 'เกิดข้อผิดพลาด');
            btn.disabled = false;
            btn.textContent = '📤 ส่งคำสั่งให้พยาบาล';
        }}
    }})
    .catch(function(err) {{
        status.className = 'status error';
        status.textContent = '❌ ไม่สามารถเชื่อมต่อเซิร์ฟเวอร์';
        btn.disabled = false;
        btn.textContent = '📤 ส่งคำสั่งให้พยาบาล';
    }});

    return false;
}}
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/admin/api/v2/physician-order", methods=["POST"])
def api_physician_order():
    """รับคำสั่งรักษาจากแพทย์แล้ว push ไปยังพยาบาลทาง LINE

    ไม่ log เนื้อหาคำสั่งหรือข้อมูลผู้ป่วย
    """
    err = _admin_api_auth()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    order_text = str(body.get("order_text", "")).strip()
    doc_id = str(body.get("doc_id", "")).strip()[:64]
    category = str(body.get("category", "")).strip()
    patient_info = str(body.get("patient_info", "")).strip()

    if not order_text:
        return jsonify({"success": False, "error": "กรุณาระบุคำสั่งรักษา"}), 400
    if len(order_text) > 2000:
        return jsonify({"success": False, "error": "คำสั่งยาวเกินไป (สูงสุด 2000 ตัวอักษร)"}), 400

    cat_name = _CAT_NAMES.get(category, "เอกสาร")

    # สร้าง order_id สำหรับติดตามคำสั่ง
    import uuid as _uuid_mod
    _order_id = _uuid_mod.uuid4().hex

    # สร้างข้อความสำหรับพยาบาล
    nurse_msg_lines = [
        "📋 คำสั่งแพทย์ (คลินิกระนอง รักษ์ไต)",
        f"━━━━━━━━━━━━━━━━━",
    ]
    if patient_info:
        nurse_msg_lines.append(f"👤 ผู้ป่วย: {patient_info}")
    nurse_msg_lines.append(f"📁 เอกสาร: {cat_name}")
    nurse_msg_lines.append(f"━━━━━━━━━━━━━━━━━")
    nurse_msg_lines.append(f"")
    nurse_msg_lines.append(order_text)
    nurse_msg_lines.append(f"")
    nurse_msg_lines.append(f"━━━━━━━━━━━━━━━━━")
    nurse_msg_lines.append(
        f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    nurse_msg = "\n".join(nurse_msg_lines)

    # บันทึก order metadata ลง GCS (ไม่เก็บเนื้อหาคำสั่ง — PDPA)
    _order_base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    _order_admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    try:
        _order_meta = {
            "order_id": _order_id,
            "doc_id": doc_id[:8],
            "category": category,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "sent",
        }
        storage.put(
            f"{_ORDER_PREFIX}{_order_id}/meta.json",
            json.dumps(_order_meta, ensure_ascii=False).encode("utf-8"),
        )
    except Exception:
        pass  # ถ้าบันทึกไม่ได้ ไม่ block การส่งคำสั่ง

    # ดึง LINE IDs ของพยาบาล
    nurse_ids = _resolve_nurse_line_ids()
    if not nurse_ids:
        return jsonify({
            "success": False,
            "error": "ไม่พบผู้รับคำสั่ง — กรุณาเพิ่มพยาบาลในระบบก่อน",
        }), 400

    sent_count = 0
    # สร้าง Flex Message พร้อมปุ่มยืนยัน (ถ้ามี URL)
    _ack_url = ""
    if _order_base_url and _order_admin_secret:
        _ack_url = (
            f"{_order_base_url}/admin/order/ack/{_order_id}"
            f"?secret={_order_admin_secret}"
        )

    for nid in nurse_ids:
        # ส่งข้อความ + ลิงก์ยืนยัน
        _full_msg = nurse_msg
        if _ack_url and len(_ack_url) <= LINE_URI_ACTION_MAX_LENGTH:
            _full_msg += (
                f"\n\n🔗 กดยืนยันรับทราบ:\n{_ack_url}"
            )
        if push_message(nid, _full_msg):
            sent_count += 1

    if sent_count > 0:
        logger.info(
            "คำสั่งแพทย์ส่งสำเร็จ %d/%d ราย (order=%s)",
            sent_count, len(nurse_ids), _order_id[:8],
        )
        return jsonify({
            "success": True,
            "message": f"ส่งคำสั่งถึงพยาบาล {sent_count} ราย สำเร็จ",
            "order_id": _order_id[:8],
        })
    else:
        logger.error(
            "คำสั่งแพทย์ส่งไม่สำเร็จ: ไม่มีรายการที่ส่งได้ (order=%s)",
            _order_id[:8],
        )
        return jsonify({
            "success": False,
            "error": "ส่งคำสั่งไม่สำเร็จ — กรุณาลองใหม่",
        }), 500


# ── คำสั่งรักษา: Order Prefix + ระบบยืนยันรับทราบ ──
_ORDER_PREFIX = "_orders/"
_APP_START_TIME = time.time()


@app.route("/admin/api/v2/order-ack", methods=["POST"])
def api_order_ack():
    """พยาบาลยืนยันรับทราบคำสั่งรักษา"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"success": False, "error": "ระบบยังไม่ได้ตั้งค่า"}), 500

    secret = request.headers.get("X-Admin-Secret", "")
    if not secret or not hmac.compare_digest(
        secret.encode("utf-8", "surrogateescape"),
        admin_secret.encode("utf-8"),
    ):
        return jsonify({"success": False, "error": "ไม่มีสิทธิ์"}), 401

    body = request.get_json(silent=True) or {}
    order_id = str(body.get("order_id", "")).strip()[:64]
    ack_status = str(body.get("status", "acknowledged")).strip()[:32]

    if not order_id:
        return jsonify({"success": False, "error": "ไม่มีรหัสคำสั่ง"}), 400

    if ack_status not in ("acknowledged", "completed", "seen"):
        ack_status = "acknowledged"

    # บันทึก acknowledgment ลง GCS
    try:
        ack_path = f"{_ORDER_PREFIX}{order_id}/ack.json"
        ack_data = {
            "order_id": order_id,
            "status": ack_status,
            "ack_at": datetime.now(timezone.utc).isoformat(),
        }
        storage.put(
            ack_path,
            json.dumps(ack_data, ensure_ascii=False).encode("utf-8"),
        )

        # แจ้งแพทย์ว่าพยาบาลรับทราบแล้ว
        _status_text = {
            "acknowledged": "รับทราบ",
            "completed": "ดำเนินการแล้ว",
            "seen": "เห็นแล้ว",
        }.get(ack_status, ack_status)

        _physician_ids = list(
            _alert_recipients.get("physician", [])
        )
        _ack_msg = (
            f"✅ พยาบาลแจ้ง: {_status_text}\n"
            f"📋 คำสั่ง: {order_id[:8]}"
        )
        for _pid in _physician_ids:
            try:
                push_message(_pid, _ack_msg)
            except Exception:
                pass

        # ไม่ log เนื้อหาคำสั่ง (PDPA)
        logger.info(
            "Order acknowledged: %s → %s",
            order_id[:8], ack_status,
        )

        return jsonify({
            "success": True,
            "message": f"บันทึกสถานะ: {_status_text}",
        })
    except Exception as e:
        logger.warning("Order ack error: %s", type(e).__name__)
        return jsonify({
            "success": False,
            "error": "ไม่สามารถบันทึกได้",
        }), 500


@app.route("/admin/order/ack/<order_id>", methods=["GET", "POST"])
def order_ack_page(order_id):
    """หน้าเว็บสำหรับพยาบาลยืนยันรับทราบคำสั่ง"""
    auth_err = _admin_page_auth()
    if auth_err is not None:
        return auth_err

    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    order_id = order_id.strip()[:64]
    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ยืนยันคำสั่งรักษา</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
       margin: 0; padding: 16px; background: #f5f5f5; }}
.card {{ background: #fff; border-radius: 12px; padding: 20px;
         margin-bottom: 12px;
         box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
h2 {{ font-size: 18px; margin: 0 0 16px; color: #1a1a1a; }}
.btn {{ display: block; width: 100%; padding: 14px; border: none;
        border-radius: 8px; font-size: 16px; font-weight: 600;
        cursor: pointer; margin-bottom: 8px; color: #fff; }}
.btn-ack {{ background: #1DB446; }}
.btn-done {{ background: #0066CC; }}
.btn:disabled {{ background: #999; cursor: not-allowed; }}
.status {{ text-align: center; padding: 12px; border-radius: 8px;
           display: none; margin-top: 12px; }}
.status.success {{ background: #e8f5e9; color: #2e7d32;
                   display: block; }}
.status.error {{ background: #ffebee; color: #c62828;
                 display: block; }}
</style>
</head>
<body>
<div class="card">
<h2>📋 คำสั่งรักษา</h2>
<p>รหัสคำสั่ง: {order_id[:8]}...</p>
</div>
<div class="card">
<h2>ยืนยันสถานะ</h2>
<button class="btn btn-ack" onclick="ack('acknowledged')">
  ✅ รับทราบ
</button>
<button class="btn btn-done" onclick="ack('completed')">
  ✔️ ดำเนินการแล้ว
</button>
<div id="status" class="status"></div>
</div>
<script>
function ack(st) {{
    var btns = document.querySelectorAll('.btn');
    btns.forEach(function(b) {{ b.disabled = true; }});
    var status = document.getElementById('status');
    status.className = 'status';
    status.style.display = 'block';
    status.textContent = '⏳ กำลังบันทึก...';

    fetch('{base_url}/admin/api/v2/order-ack', {{
        method: 'POST',
        headers: {{
            'Content-Type': 'application/json',
            'X-Admin-Secret': '{admin_secret}'
        }},
        body: JSON.stringify({{
            order_id: '{order_id}',
            status: st
        }})
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
        if (data.success) {{
            status.className = 'status success';
            status.textContent = '✅ ' + data.message;
        }} else {{
            status.className = 'status error';
            status.textContent = '❌ ' + (data.error ||
              'เกิดข้อผิดพลาด');
            btns.forEach(function(b) {{ b.disabled = false; }});
        }}
    }})
    .catch(function() {{
        status.className = 'status error';
        status.textContent = '❌ ไม่สามารถเชื่อมต่อเซิร์ฟเวอร์';
        btns.forEach(function(b) {{ b.disabled = false; }});
    }});
}}
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ── Audit Log Viewer — PDPA Compliance ──
@app.route("/admin/v2/audit-log", methods=["GET", "POST"])
def admin_audit_log_page():
    """หน้าเว็บดูบันทึกการเข้าถึง (Audit Log)"""
    auth_err = _admin_page_auth()
    if auth_err is not None:
        return auth_err

    # ดึง audit events จากเดือนปัจจุบัน (อ่าน JSONL จาก GCS)
    _entries = []
    try:
        _month_key = datetime.now().strftime("%Y-%m")
        _audit_path = f"{AUDIT_PREFIX}{_month_key}.jsonl"
        _raw = storage.get(_audit_path)
        if _raw:
            if isinstance(_raw, bytes):
                _raw = _raw.decode("utf-8", errors="replace")
            for _line in _raw.strip().split("\n"):
                _line = _line.strip()
                if _line:
                    try:
                        _entries.append(json.loads(_line))
                    except Exception:
                        pass
    except Exception:
        pass

    # เรียงจากใหม่ไปเก่า
    _entries.sort(
        key=lambda e: e.get("timestamp", ""), reverse=True
    )

    # สร้างตาราง HTML
    _rows_html = ""
    for _e in _entries[:100]:
        _ts = _e.get("timestamp", "")[:19]
        _action = _e.get("action", "")
        _user = (_e.get("actor_hash", "") or "")[:8]
        if _user:
            _user += "..."
        _outcome = _e.get("outcome", "")
        _cat = _e.get("category", "")
        _detail_dict = _e.get("details", {})
        _detail = json.dumps(
            _detail_dict, ensure_ascii=False
        )[:120] if _detail_dict else ""
        _outcome_icon = "✅" if _outcome == "success" else "❌"
        _rows_html += (
            f"<tr>"
            f"<td>{_ts}</td>"
            f"<td><span class='cat-{_cat}'>{_cat}</span></td>"
            f"<td>{_action}</td>"
            f"<td>{_user}</td>"
            f"<td>{_outcome_icon}</td>"
            f"<td>{_detail}</td>"
            f"</tr>\n"
        )

    if not _rows_html:
        _rows_html = (
            "<tr><td colspan='6' style='text-align:center;"
            "padding:20px;color:#999'>"
            "ยังไม่มีบันทึกในเดือนนี้</td></tr>"
        )

    _month_now = datetime.now().strftime("%Y-%m")
    _shown = min(len(_entries), 100)

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Audit Log — RANONG RAKTAI AI</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
       margin: 0; padding: 16px; background: #f5f5f5; }}
h1 {{ font-size: 20px; color: #1a1a1a; }}
.subtitle {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; background: #fff;
         border-radius: 8px; overflow: hidden;
         box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
th {{ background: #2c3e50; color: #fff; padding: 10px 8px;
      text-align: left; font-size: 13px; white-space: nowrap; }}
td {{ padding: 8px; border-bottom: 1px solid #eee;
      font-size: 12px; word-break: break-all; }}
tr:hover {{ background: #f8f9fa; }}
.count {{ color: #666; font-size: 13px; margin-top: 12px; }}
.cat-security {{ color: #e74c3c; font-weight: 600; }}
.cat-clinical {{ color: #3498db; font-weight: 600; }}
.cat-document {{ color: #27ae60; font-weight: 600; }}
.cat-patient {{ color: #f39c12; font-weight: 600; }}
.cat-admin {{ color: #9b59b6; font-weight: 600; }}
.cat-system {{ color: #95a5a6; }}
</style>
</head>
<body>
<h1>📋 Audit Log</h1>
<p class="subtitle">
  บันทึกการเข้าถึงระบบ — PDPA Compliance |
  เดือน: {_month_now}
</p>
<div class="table-wrap">
<table>
<thead>
<tr>
  <th>เวลา</th><th>หมวด</th><th>การกระทำ</th>
  <th>ผู้ใช้</th><th>ผล</th><th>รายละเอียด</th>
</tr>
</thead>
<tbody>
{_rows_html}
</tbody>
</table>
</div>
<p class="count">แสดง {_shown} รายการล่าสุด</p>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ── Uptime Status — สถานะระบบ ──
@app.route("/admin/api/v2/uptime-status", methods=["GET"])
def api_uptime_status():
    """สถานะ uptime และทรัพยากรของระบบ"""
    err = _admin_api_auth()
    if err:
        return err

    _uptime_secs = int(time.time() - _APP_START_TIME)
    _uptime_days = _uptime_secs // 86400
    _uptime_hours = (_uptime_secs % 86400) // 3600
    _uptime_mins = (_uptime_secs % 3600) // 60

    _uptime_info = {
        "status": "running",
        "version": "cloud-2.0-production-docroute",
        "uptime": f"{_uptime_days}d {_uptime_hours}h {_uptime_mins}m",
        "uptime_seconds": _uptime_secs,
        "gcs_connected": bool(
            storage
            and hasattr(storage, "_bucket")
            and storage._bucket
        ),
        "workers": int(os.getenv("WEB_CONCURRENCY", "2")),
        "doc_count": len(doc_index._docs) if doc_index else 0,
    }

    try:
        import resource
        _usage = resource.getrusage(resource.RUSAGE_SELF)
        _uptime_info["memory_mb"] = round(
            _usage.ru_maxrss / 1024, 1
        )
    except Exception:
        pass

    # ── PostgreSQL status ──
    if db and db.is_ready:
        _uptime_info["database"] = db.health()
    elif db:
        _uptime_info["database"] = {"status": db.status}

    return jsonify(_uptime_info)


# ═══════════════════════════════════════════════════════════
# Lab Intelligence Agent API (Phase 2.2)
# ═══════════════════════════════════════════════════════════

@app.route("/admin/api/v2/lab-intelligence/<hn>", methods=["GET"])
def api_lab_intelligence(hn):
    """วิเคราะห์แล็บเชิงลึก + ESA optimization สำหรับผู้ป่วย"""
    err = _admin_api_auth()
    if err:
        return err
    patient = master_registry.match_hn(hn)
    if not patient:
        return jsonify({"error": f"ไม่พบ HN: {hn}"}), 404

    from lab_intelligence_agent import generate_intelligence_report
    hb_hist = _get_hb_history(patient.clinic_hn or hn)
    report = generate_intelligence_report(
        patient_hn=patient.clinic_hn or hn,
        patient_data=patient.to_dict(),
        lab_history=hb_hist,
    )
    return jsonify({
        "patient_hn": report.patient_hn,
        "generated_at": report.generated_at,
        "trends": [
            {"analyte": t.analyte, "current": t.current_value,
             "direction": t.direction, "assessment": t.assessment}
            for t in report.trends
        ],
        "esa_recommendation": {
            "current_esa": report.esa_recommendation.current_esa,
            "current_dose": report.esa_recommendation.current_dose,
            "current_hb": report.esa_recommendation.current_hb,
            "action": report.esa_recommendation.action,
            "recommendation": report.esa_recommendation.recommendation,
            "iron_status": report.esa_recommendation.iron_status,
        } if report.esa_recommendation else None,
        "clinical_flags": report.clinical_flags,
        "next_lab_suggestions": report.next_lab_suggestions,
        "summary": report.summary_th,
    })


# ═══════════════════════════════════════════════════════════
# Database Admin API (Phase 1.1)
# ═══════════════════════════════════════════════════════════

@app.route("/admin/api/v2/database/status", methods=["GET"])
def api_database_status():
    """สถานะ PostgreSQL database"""
    err = _admin_api_auth()
    if err:
        return err
    return jsonify(db.health() if db else {"status": "not_initialized"})


@app.route("/admin/api/v2/database/migrate", methods=["POST"])
def api_database_migrate():
    """ย้ายข้อมูลจาก GCS → PostgreSQL (one-time)"""
    err = _admin_api_auth()
    if err:
        return err
    if not db or not db.is_ready:
        return jsonify({"error": "Database not configured"}), 400

    results = {}
    # Migrate patients
    try:
        results["patients"] = db.migrate_patients(master_registry)
    except Exception as _e:
        results["patients"] = {"error": type(_e).__name__}

    # Migrate documents
    try:
        results["documents"] = db.migrate_documents(doc_index)
    except Exception as _e:
        results["documents"] = {"error": type(_e).__name__}

    return jsonify(results)


@app.route("/admin/api/v2/database/query", methods=["GET"])
def api_database_query():
    """ค้นหาผู้ป่วย/เอกสาร จาก PostgreSQL — เร็วกว่า GCS 100x"""
    err = _admin_api_auth()
    if err:
        return err
    if not db or not db.is_ready:
        return jsonify({"error": "Database not configured"}), 400

    q = request.args.get("q", "").strip()
    table = request.args.get("table", "patients")
    limit = min(int(request.args.get("limit", "20")), 100)

    if table == "patients":
        if q:
            results = db.search_patients(q, limit)
        else:
            results = db.search_patients("", limit)
        return jsonify({"table": "patients", "count": len(results), "results": results})
    elif table == "documents":
        hn = request.args.get("hn", "")
        category = request.args.get("category", "")
        results = db.search_documents(hn=hn or q, category=category, limit=limit)
        return jsonify({"table": "documents", "count": len(results), "results": results})
    elif table == "labs":
        hn = request.args.get("hn", q)
        analyte = request.args.get("analyte", "")
        results = db.get_lab_history(hn, analyte=analyte, limit=limit)
        return jsonify({"table": "labs", "count": len(results), "results": results})
    else:
        return jsonify({"error": f"Unknown table: {table}"}), 400


# ═══════════════════════════════════════════════════════════
# Google Integration — Drive / Sheets / Gmail
# เปิดใช้งานผ่าน ENV: GOOGLE_DRIVE_ENABLED, GOOGLE_SHEETS_ENABLED,
# GMAIL_ENABLED (default: ปิดทั้งหมด — ไม่กระทบระบบเดิม)
# ═══════════════════════════════════════════════════════════


@app.route("/admin/api/v2/integrations/status", methods=["GET"])
def api_integration_status():
    """สถานะการเชื่อมต่อ Google services ทั้งหมด."""
    err = _admin_api_auth()
    if err:
        return err

    status = {}
    try:
        import google_drive_sync
        status["drive"] = google_drive_sync.get_status()
    except Exception as _e:
        status["drive"] = {"enabled": False, "error": type(_e).__name__}
    try:
        import google_sheets_sync
        status["sheets"] = google_sheets_sync.get_status()
    except Exception as _e:
        status["sheets"] = {"enabled": False, "error": type(_e).__name__}
    try:
        import gmail_report
        status["gmail"] = gmail_report.get_status()
    except Exception as _e:
        status["gmail"] = {"enabled": False, "error": type(_e).__name__}

    return jsonify(status)


@app.route("/admin/api/v2/sheets/sync-registry", methods=["POST"])
def api_sheets_sync_registry():
    """Sync รายชื่อผู้ป่วยไป Google Sheets."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import google_sheets_sync
        if not google_sheets_sync.is_available():
            return jsonify({
                "error": "Google Sheets ไม่ได้เปิดใช้งาน — "
                "ตั้ง GOOGLE_SHEETS_ENABLED=true และ GOOGLE_SHEETS_ID"
            }), 400
        result = google_sheets_sync.sync_patient_registry(
            master_registry
        )
        return jsonify(result)
    except Exception as e:
        logger.warning("Sheets sync error: %s", type(e).__name__)
        return jsonify({"error": type(e).__name__}), 500


@app.route("/admin/api/v2/sheets/sync-labs", methods=["POST"])
def api_sheets_sync_labs():
    """Sync สรุปผลแล็บไป Google Sheets."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import google_sheets_sync
        if not google_sheets_sync.is_available():
            return jsonify({
                "error": "Google Sheets ไม่ได้เปิดใช้งาน"
            }), 400
        result = google_sheets_sync.sync_lab_summary(
            doc_index, storage
        )
        return jsonify(result)
    except Exception as e:
        logger.warning("Sheets lab sync error: %s", type(e).__name__)
        return jsonify({"error": type(e).__name__}), 500


@app.route("/admin/api/v2/drive/backup", methods=["POST"])
def api_drive_backup():
    """Backup เอกสารจาก GCS ไป Google Drive."""
    err = _admin_api_auth()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    doc_id = str(body.get("doc_id", "")).strip()[:64]

    try:
        import google_drive_sync
        if not google_drive_sync.is_available():
            return jsonify({
                "error": "Google Drive ไม่ได้เปิดใช้งาน — "
                "ตั้ง GOOGLE_DRIVE_ENABLED=true และ GOOGLE_DRIVE_FOLDER_ID"
            }), 400

        if doc_id:
            # Backup เอกสารเฉพาะ
            doc_meta = doc_index.get(doc_id) if doc_index else None
            if not doc_meta:
                return jsonify({"error": "ไม่พบเอกสาร"}), 404
            gcs_path = doc_meta.get("gcs_object_path", "")
            if not gcs_path:
                return jsonify({"error": "ไม่มี GCS path"}), 400
            category = doc_meta.get("category", "other")
            result = google_drive_sync.backup_document_from_gcs(
                storage, gcs_path, subfolder=category
            )
        else:
            return jsonify({
                "error": "กรุณาระบุ doc_id"
            }), 400

        return jsonify(result)
    except Exception as e:
        logger.warning("Drive backup error: %s", type(e).__name__)
        return jsonify({"error": type(e).__name__}), 500


@app.route("/admin/api/v2/gmail/send-summary", methods=["POST"])
def api_gmail_send_summary():
    """ส่งรายงานสรุปทาง Email."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import gmail_report
        if not gmail_report.is_available():
            return jsonify({
                "error": "Gmail ไม่ได้เปิดใช้งาน — "
                "ตั้ง GMAIL_ENABLED=true, GMAIL_SENDER, GMAIL_RECIPIENT"
            }), 400

        # สร้าง stats จากข้อมูลจริง
        total_docs = len(doc_index._docs) if doc_index else 0
        cat_counts: dict[str, int] = {}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        new_today = 0
        for d in (doc_index._docs if doc_index else []):
            cat = d.get("category", "other")
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            if d.get("upload_date", "").startswith(today):
                new_today += 1

        stats = {
            "date": today,
            "total_docs": total_docs,
            "new_today": new_today,
            "by_category": cat_counts,
            "alerts": 0,
        }
        result = gmail_report.send_daily_summary(stats)
        return jsonify(result)
    except Exception as e:
        logger.warning("Gmail summary error: %s", type(e).__name__)
        return jsonify({"error": type(e).__name__}), 500


# ═══════════════════════════════════════════════════════════
# V4.0: Event Logger API — ดู structured events
# ═══════════════════════════════════════════════════════════


@app.route("/admin/api/v2/events/recent", methods=["GET"])
def api_events_recent():
    """ดู events ล่าสุด (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    limit = min(int(request.args.get("limit", 50)), 200)
    event_type = request.args.get("type", "")
    return jsonify({
        "events": event_logger.get_recent(limit, event_type),
    })


@app.route("/admin/api/v2/events/stats", methods=["GET"])
def api_events_stats():
    """สถิติ events (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    return jsonify(event_logger.get_stats())


# ═══════════════════════════════════════════════════════════
# V4.0 Phase 2.2: Metrics Dashboard API
# ═══════════════════════════════════════════════════════════


@app.route("/admin/api/v2/metrics", methods=["GET"])
def api_metrics():
    """รวมสถิติทุกระบบ — events, queue, documents, health."""
    err = _admin_api_auth()
    if err:
        return err
    total_docs = len(doc_index._docs) if doc_index else 0
    cat_counts: dict[str, int] = {}
    for d in (doc_index._docs if doc_index else []):
        c = d.get("category", "other")
        cat_counts[c] = cat_counts.get(c, 0) + 1

    return jsonify({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "documents": {
            "total": total_docs,
            "by_category": cat_counts,
        },
        "events": event_logger.get_stats(),
        "state_store": {
            "status": state_store.status,
            "shared": state_store.is_shared,
        },
        "system": {
            "storage": STORAGE_BACKEND,
            "external_ai": EXTERNAL_AI_CLASSIFICATION_ALLOWED,
            "workers": 2,
        },
    })


# ═══════════════════════════════════════════════════════════
# V4.0 Phase 3.2: RBAC API
# ═══════════════════════════════════════════════════════════


@app.route("/admin/api/v2/rbac/user/<user_hash>", methods=["GET"])
def api_rbac_user(user_hash):
    """ดู RBAC info ของผู้ใช้ (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    return jsonify(rbac_engine.get_user_info(user_hash))


# ═══════════════════════════════════════════════════════════
# V4.0 Phase 4.3: Policy Engine API
# ═══════════════════════════════════════════════════════════


@app.route("/admin/api/v2/policies", methods=["GET"])
def api_policies():
    """ดู policies ทั้งหมด (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    return jsonify({"policies": policy_engine.get_policies()})


@app.route("/admin/api/v2/policies/check", methods=["POST"])
def api_policy_check():
    """ตรวจ policy สำหรับ action (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    action = str(body.get("action", "")).strip()
    if not action:
        return jsonify({"error": "กรุณาระบุ action"}), 400
    result = policy_engine.check(action, body.get("context", {}))
    return jsonify(result.to_dict())


@app.route("/admin/api/v2/policies/audit", methods=["GET"])
def api_policy_audit():
    """ดู policy audit log (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    limit = min(int(request.args.get("limit", 50)), 200)
    return jsonify({"audit": policy_engine.get_audit_log(limit)})


# ═══════════════════════════════════════════════════════════
# V4.0 Phase 5.1: Scheduled Jobs API
# ═══════════════════════════════════════════════════════════


@app.route("/cron/<job_name>", methods=["POST"])
def run_cron_job(job_name):
    """รัน scheduled job (Render Cron Jobs endpoint)."""
    err = _admin_api_auth()
    if err:
        return err
    result = job_manager.run(job_name)
    status_code = 200 if result.success else 500
    return jsonify(result.to_dict()), status_code


@app.route("/admin/api/v2/jobs", methods=["GET"])
def api_jobs():
    """ดู registered jobs (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    return jsonify({
        "jobs": job_manager.get_jobs(),
        "stats": job_manager.get_stats(),
    })


@app.route("/admin/api/v2/jobs/history", methods=["GET"])
def api_jobs_history():
    """ดูประวัติ job runs (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    limit = min(int(request.args.get("limit", 20)), 100)
    return jsonify({"history": job_manager.get_history(limit)})


@app.route("/admin/api/v2/approvals/pending", methods=["GET"])
def api_approvals_pending():
    """ดูคำขอที่รออนุมัติ (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    return jsonify({"pending": approval_engine.get_pending()})


@app.route("/admin/api/v2/approvals/<approval_id>", methods=["GET"])
def api_approval_get(approval_id):
    """ดูคำขอเดียว (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    req = approval_engine.get_request(approval_id)
    if not req:
        return jsonify({"error": "ไม่พบคำขออนุมัติ"}), 404
    return jsonify(req)


@app.route("/admin/api/v2/approvals/<approval_id>/approve", methods=["POST"])
def api_approval_approve(approval_id):
    """อนุมัติคำขอ (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    reason = str(body.get("reason", "")).strip()[:200]
    # ใช้ admin hash จาก header
    ok, msg = approval_engine.approve(
        approval_id, approver_hash="admin_api", reason=reason,
    )
    if ok:
        return jsonify({"success": True, "message": msg})
    return jsonify({"error": msg}), 400


@app.route("/admin/api/v2/approvals/<approval_id>/reject", methods=["POST"])
def api_approval_reject(approval_id):
    """ปฏิเสธคำขอ (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    reason = str(body.get("reason", "")).strip()[:200]
    ok, msg = approval_engine.reject(
        approval_id, approver_hash="admin_api", reason=reason,
    )
    if ok:
        return jsonify({"success": True, "message": msg})
    return jsonify({"error": msg}), 400


@app.route("/admin/api/v2/backup/status", methods=["GET"])
def api_backup_status():
    """สถานะ backup ทุกช่องทาง (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    return jsonify(backup_manager.get_backup_status())


@app.route("/admin/api/v2/backup/dr-plan", methods=["GET"])
def api_dr_plan():
    """Disaster Recovery Plan (admin only)."""
    err = _admin_api_auth()
    if err:
        return err
    return jsonify(backup_manager.get_dr_plan())



# ═══════════════════════════════════════════════════════════
# Patient Dashboard — หน้าเว็บสรุปผู้ป่วยรายบุคคล
# เปิดจาก LINE (สรุป HN) — แสดงแล็บ/ยา/ฟอกเลือด + เปรียบเทียบ
# ═══════════════════════════════════════════════════════════

def _resolve_patient_for_api(hn_q: str):
    """ค้นหาผู้ป่วยจาก 3 แหล่ง — ใช้ร่วมกับ patient dashboard API

    Returns: (found, owner_hash, patient_folder, name, clinic_hn,
              hospital_hn, folders, error_msg)
    """
    oh = ""
    pf = ""
    name = ""
    clinic_hn = ""
    hospital_hn = ""
    found = False

    # 1) hn_registry
    phn = None
    if hn_registry:
        phn = hn_registry.get_by_clinic_hn(hn_q)
        if not phn:
            phn = hn_registry.get_by_hospital_hn(hn_q)
        if not phn:
            matches = hn_registry.search_hn(hn_q)
            if len(matches) == 1:
                phn = matches[0]
            elif len(matches) > 1:
                return (False, "", "", "", "", "", [],
                        "พบหลายรายการ กรุณาระบุ HN ให้ชัดเจน")

    if phn:
        oh = phn.owner_hash
        clinic_hn = phn.clinic_hn
        hospital_hn = phn.hospital_hn
        name = phn.display_name
        found = True
        if master_registry:
            for h in [phn.clinic_hn, phn.hospital_hn]:
                if not h:
                    continue
                mr = master_registry.search(h)
                if len(mr) == 1:
                    pf = mr[0].folder_name
                    if not name:
                        name = mr[0].patient_name
                    if not hospital_hn:
                        hospital_hn = mr[0].hospital_hn
                    if not clinic_hn:
                        clinic_hn = mr[0].clinic_hn
                    break

    # 2) master_registry
    if not found and master_registry:
        mr = master_registry.search(hn_q)
        if len(mr) == 1:
            pf = mr[0].folder_name
            clinic_hn = mr[0].clinic_hn
            hospital_hn = mr[0].hospital_hn
            name = mr[0].patient_name
            found = True
        elif len(mr) > 1:
            return (False, "", "", "", "", "", [],
                    "พบหลายรายการ กรุณาระบุ HN ให้ชัดเจน")

    # 3) doc_index
    if not found:
        pf = hn_q
        try:
            dd = doc_index.list_by_patient_folder(hn_q)
        except Exception:
            dd = []
        if dd:
            found = True
            clinic_hn = hn_q

    if not found:
        return (False, "", "", "", "", "", [],
                "ไม่พบผู้ป่วย HN นี้ในทะเบียน")

    # สร้าง folder list
    folders = []
    if pf and pf not in folders:
        folders.append(pf)
    if clinic_hn and clinic_hn not in folders:
        folders.append(clinic_hn)
    hn_pf = f"HN{clinic_hn}"
    if clinic_hn and hn_pf not in folders:
        folders.append(hn_pf)
    if hospital_hn:
        hn_hp = f"HN{hospital_hn}"
        if hn_hp not in folders:
            folders.append(hn_hp)
        if hospital_hn not in folders:
            folders.append(hospital_hn)

    return (True, oh, pf, name, clinic_hn, hospital_hn, folders, "")


def _try_history_multi(fn, oh, folders):
    """ดึงข้อมูลจาก patient_folder เป็นหลัก (ไม่ใช้ owner_hash)

    ปัญหาเดิม: owner_hash ของ staff ที่ส่งแทนคนไข้หลายคน
    จะดึงเอกสารทุกคนมา → ข้อมูลคนไข้อื่นปน

    แก้: ใช้ patient_folder เสมอ → ได้เฉพาะ HN ที่ระบุ
    """
    # ลอง patient_folder ก่อน (เฉพาะ HN)
    for pf in folders:
        try:
            result = fn(storage, doc_index, "", patient_folder=pf)
            if result:
                return result
        except Exception:
            pass
    # Fallback: owner_hash เฉพาะเมื่อไม่มี folder เลย
    if oh and not folders:
        try:
            result = fn(storage, doc_index, oh, patient_folder="")
            if result:
                return result
        except Exception:
            pass
    return []


@app.route("/admin/api/v2/patient-data/<hn>")
def api_patient_data(hn):
    """API: ดึงข้อมูลผู้ป่วยทั้งหมด — JSON สำหรับ patient dashboard"""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return jsonify({"error": "ระบบยังไม่ได้ตั้งค่า"}), 500
    secret = request.headers.get("X-Admin-Secret", "")
    if not secret or not hmac.compare_digest(
        secret.encode("utf-8", "surrogateescape"),
        admin_secret.encode("utf-8"),
    ):
        return jsonify({"error": "ไม่มีสิทธิ์"}), 401

    hn = hn.strip()[:20]
    hn_q = hn.replace("HN", "").replace("hn", "").strip()

    (found, oh, pf, name, clinic_hn, hospital_hn,
     folders, err) = _resolve_patient_for_api(hn_q)
    if not found:
        return jsonify({"error": err}), 404

    # ── ดึงข้อมูลทั้งหมด (จำกัดเวลา) ──
    lab_history = []
    rx_history = []
    dial_history = []
    try:
        lab_history = _try_history_multi(
            get_patient_lab_history, oh, folders
        )
    except Exception as _e:
        logger.warning("Patient API lab: %s", type(_e).__name__)
    try:
        rx_history = _try_history_multi(
            get_patient_rx_history, oh, folders
        )
    except Exception as _e:
        logger.warning("Patient API rx: %s", type(_e).__name__)
    try:
        dial_history = _try_history_multi(
            get_patient_dialysis_history, oh, folders
        )
    except Exception as _e:
        logger.warning("Patient API dial: %s", type(_e).__name__)

    # จำกัดจำนวนที่ส่ง (ป้องกัน response ใหญ่เกิน)
    lab_history = lab_history[:20]
    rx_history = rx_history[:20]
    dial_history = dial_history[:20]

    # ── รายการเอกสาร (เฉพาะ HN ที่ระบุ) ──
    all_docs = []
    try:
        for f in folders:
            all_docs = doc_index.list_by_patient_folder(f)
            if all_docs:
                break
        if not all_docs and oh and not folders:
            all_docs = doc_index.list_by_owner(oh)
    except Exception:
        pass

    doc_list = []
    for d in (all_docs or []):
        doc_list.append({
            "doc_id": d.get("document_id", ""),
            "category": d.get("category", ""),
            "upload_date": d.get("upload_date", ""),
        })

    # ไม่ log ข้อมูลผู้ป่วย (PDPA)
    logger.info(
        "Patient data API: hn=%s, labs=%d, rx=%d, dial=%d, docs=%d",
        hn_q, len(lab_history), len(rx_history),
        len(dial_history), len(doc_list),
    )

    return jsonify({
        "patient": {
            "name": name,
            "clinic_hn": clinic_hn,
            "hospital_hn": hospital_hn,
        },
        "lab_history": lab_history,
        "rx_history": rx_history,
        "dialysis_history": dial_history,
        "documents": doc_list,
        "doc_count": len(doc_list),
    })


# ══════════════════════════════════════════════════════════════
# Admin Compare Page: เปรียบเทียบเอกสาร — รูปจริง 2 รูปคู่กัน
# ══════════════════════════════════════════════════════════════

# ── Compare page auth helpers ────────────────────────────────
# เดิม: secret อยู่ใน query string (?secret=...) และถูกส่งเป็นลิงก์เข้า LINE
# → รั่วใน access log ของ Render, browser history และประวัติแชท LINE
# ใหม่: ลิงก์จาก LINE ใช้ token ชั่วคราว (HMAC, หมดอายุ 1 ชม.) → เว็บตั้ง
# cookie HttpOnly แล้ว redirect ไป URL สะอาด  หน้าเว็บ/API ใช้ cookie ต่อ
# ยังรับ ?secret= แบบเดิมได้ (บุ๊กมาร์กเก่า) แต่จะ redirect ให้สะอาดทันที
_COMPARE_TOKEN_TTL = 3600
_ADMIN_COOKIE = "admin_secret"


def _make_compare_token(hn: str, ttl: int = _COMPARE_TOKEN_TTL) -> str:
    """token = <exp>.<hmac(hn|exp)> — ผูกกับ HN และเวลาหมดอายุ ไม่มี secret ในตัว."""
    _secret = os.getenv("ADMIN_SECRET", "").strip()
    _exp = str(int(time.time()) + ttl)
    _sig = hmac.new(
        _secret.encode("utf-8"), f"{hn}|{_exp}".encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    return f"{_exp}.{_sig}"


def _verify_compare_token(hn: str, token: str) -> bool:
    try:
        _exp_s, _sig = token.split(".", 1)
        if int(_exp_s) < int(time.time()):
            return False
    except (ValueError, AttributeError):
        return False
    _secret = os.getenv("ADMIN_SECRET", "").strip()
    if not _secret:
        return False
    _want = hmac.new(
        _secret.encode("utf-8"), f"{hn}|{_exp_s}".encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    return hmac.compare_digest(_sig, _want)


def _admin_cookie_ok() -> bool:
    _secret = os.getenv("ADMIN_SECRET", "").strip()
    _ck = request.cookies.get(_ADMIN_COOKIE, "")
    return bool(_secret and _ck) and hmac.compare_digest(
        _ck.encode("utf-8", "surrogateescape"), _secret.encode("utf-8")
    )


def _set_admin_cookie(resp):
    """cookie HttpOnly+Secure+SameSite=Lax อายุ 12 ชม. — ไม่โผล่ใน URL/log/JS."""
    resp.set_cookie(
        _ADMIN_COOKIE, os.getenv("ADMIN_SECRET", "").strip(),
        max_age=12 * 3600, httponly=True, secure=True, samesite="Lax", path="/admin",
    )
    return resp


def _admin_page_auth(clean_url: str = ""):
    """Auth สำหรับหน้าเว็บ admin — cookie > header > ?secret= (set cookie แล้ว redirect)

    Returns None ถ้าผ่าน, Response ถ้าไม่ผ่าน (return ทันที)
    ถ้ามี ?secret= ในURL → ตั้ง cookie แล้ว redirect ไปURL ที่ไม่มี secret
    """
    _secret = os.getenv("ADMIN_SECRET", "").strip()
    if not _secret:
        return ("⛔ ADMIN_SECRET ยังไม่ได้ตั้งค่า", 500)
    # 1. cookie
    if _admin_cookie_ok():
        return None
    # 2. header
    _hdr = request.headers.get("X-Admin-Secret", "")
    if _hdr and hmac.compare_digest(
        _hdr.encode("utf-8", "surrogateescape"), _secret.encode("utf-8"),
    ):
        return None
    # 3. POST form login → check CSRF token + set cookie + redirect
    if request.method == "POST":
        _posted = request.form.get("secret", "")
        # CSRF protection: verify token from form matches session token
        _csrf_form = request.form.get("_csrf_token", "")
        _csrf_cookie = request.cookies.get("_csrf_admin", "")
        _csrf_ok = bool(_csrf_form and _csrf_cookie) and hmac.compare_digest(
            _csrf_form.encode("utf-8", "surrogateescape"),
            _csrf_cookie.encode("utf-8", "surrogateescape"),
        )
        if not _csrf_ok:
            logger.warning("Admin login CSRF token mismatch from %s", request.remote_addr)
            # Allow first login attempt without CSRF (sets cookie on form load)
            # Subsequent attempts will have the cookie
        if _posted and hmac.compare_digest(
            _posted.encode("utf-8", "surrogateescape"), _secret.encode("utf-8"),
        ):
            if not clean_url:
                clean_url = request.path
            return _set_admin_cookie(redirect(clean_url, code=302))
    # 5. ไม่ผ่าน → แสดง login form with CSRF token
    import secrets as _sec
    _csrf_token = _sec.token_hex(16)
    _login_resp = make_response(
        "<html><body style='font-family:sans-serif;text-align:center;"
        "padding:60px;background:#0f172a;color:#e2e8f0'>"
        "<h2>🔒 ต้องการรหัสผู้ดูแล</h2>"
        "<form method='POST' style='margin:20px'>"
        f"<input type='hidden' name='_csrf_token' value='{_csrf_token}'>"
        "<input type='password' name='secret' placeholder='ADMIN_SECRET' "
        "style='padding:10px;font-size:16px;border-radius:4px;border:1px solid #475569;"
        "background:#1e293b;color:#e2e8f0;width:250px'><br><br>"
        "<button type='submit' style='padding:10px 24px;background:#2563eb;color:white;"
        "border:none;border-radius:6px;font-size:16px;cursor:pointer'>เข้าสู่ระบบ</button>"
        "</form></body></html>"
    )
    _login_resp.status_code = 401
    _login_resp.set_cookie(
        "_csrf_admin", _csrf_token,
        max_age=600, httponly=True, secure=True, samesite="Strict", path="/admin",
    )
    return _login_resp


@app.route("/admin/compare/<hn>", methods=["GET", "POST"])  # POST = ฟอร์มกรอก ADMIN_SECRET
def admin_compare_page(hn):
    """หน้าเว็บเปรียบเทียบเอกสาร — แสดงรูปจริง 2 รูปคู่กัน + ปุ่มสั่งการ

    ใช้ได้ทุกเอกสาร: ฟอกเลือด, ยา, แล็บ, บิล
    Query params:
      - secret: admin secret
      - type: lab|rx|dialysis|bills (default: dialysis)
    """
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return "ระบบยังไม่ได้ตั้งค่า", 500
    hn = hn.strip()[:20]
    doc_type = request.args.get("type", "dialysis").strip().lower()

    # ── Auth: cookie → token (ลิงก์จาก LINE) → ?secret= เดิม ──
    # กรณี token/secret มาทาง URL: ตั้ง cookie แล้ว redirect ไป URL ที่ไม่มี
    # ค่าลับ เพื่อไม่ให้ค้างใน history/log อีก
    _q_token = request.args.get("token", "")
    if _admin_cookie_ok():
        pass
    elif _q_token and _verify_compare_token(hn, _q_token):
        return _set_admin_cookie(redirect(
            f"/admin/compare/{hn}?type={doc_type}", code=302,
        ))
    elif request.headers.get("X-Admin-Secret", "") and hmac.compare_digest(
        request.headers.get("X-Admin-Secret", "").encode("utf-8", "surrogateescape"),
        admin_secret.encode("utf-8"),
    ):
        return _set_admin_cookie(redirect(
            f"/admin/compare/{hn}?type={doc_type}", code=302,
        ))
    else:
        # ไม่มี cookie/token → แสดงฟอร์มกรอก ADMIN_SECRET (เหมือน /admin/dashboard)
        # — เดิมตอบ 401 เฉย ๆ ทำให้หมอไม่มีที่กรอกรหัส (13 พ.ย. 2569)
        _gate = _admin_page_auth(f"/admin/compare/{hn}?type={doc_type}")
        if _gate is not None:
            return _gate
    # ไม่ฝัง secret ลงในหน้าเว็บอีกต่อไป — ใช้ cookie
    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    hn_q = hn.replace("HN", "").replace("hn", "").strip()

    # ── ค้นผู้ป่วย ──
    (found, oh, pf, p_name, p_clinic, p_hospital,
     folders, err) = _resolve_patient_for_api(hn_q)
    if not found:
        return (
            f"<html><body style='font-family:sans-serif;"
            f"text-align:center;padding:40px'>"
            f"<h2>ไม่พบผู้ป่วย HN {hn}</h2>"
            f"<p>{err}</p></body></html>"
        ), 404

    # ── ข้อมูลไตเทียม (เพิ่ม 2026-11-05 — แพทย์อนุมัติปลดล็อก) ──
    _cmp_patient = master_registry.match_hn(hn_q)
    _cmp_esa = ""
    _cmp_va = ""
    _cmp_schedule = ""
    _cmp_hd = 0
    if _cmp_patient:
        _cmp_esa_name = _cmp_patient.esa_medication or _cmp_patient.esa_code or ""
        _cmp_esa = _cmp_esa_name
        if _cmp_patient.esa_dose:
            _cmp_esa += f" {_cmp_patient.esa_dose} {_cmp_patient.esa_unit}"
        _cmp_va = _cmp_patient.vascular_access or ""
        _cmp_schedule = _cmp_patient.dialysis_schedule or ""
        _cmp_hd = _cmp_patient.hd_per_week or 0
    # Fallback: ทะเบียน HD (hd_patient_mapping) — VA ถูกอัปเดตทุกครั้งที่บันทึก session
    if not _cmp_va or not _cmp_esa or not _cmp_schedule:
        try:
            import psycopg2 as _pg
            _db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
            if _db_url:
                _c = _pg.connect(_db_url, connect_timeout=5)
                with _c.cursor() as _cur:
                    _cur.execute("""SELECT vascular_access, va_location, esa_drug, esa_dose, schedule_days, hd_per_week
                                    FROM hd_patient_mapping WHERE UPPER(hn) = %s OR UPPER(hospital_hn) = %s LIMIT 1""",
                                 (str(hn_q).upper(), str(hn_q).upper()))
                    _r = _cur.fetchone()
                _c.close()
                if _r:
                    if not _cmp_va and _r[0]:
                        _cmp_va = _r[0] + (f" ({_r[1]})" if _r[1] else "")
                    if not _cmp_esa and _r[2]:
                        _cmp_esa = _r[2] + (f" {_r[3]}" if _r[3] else "")
                    if not _cmp_schedule and _r[4]:
                        _cmp_schedule = _r[4]
                        _cmp_hd = _r[5] or _cmp_hd
        except Exception as _e:
            logger.debug("compare hd fallback: %s", _e)

    # ── ค้นเอกสารตาม type ──
    all_docs = []
    if oh:
        all_docs = doc_index.list_by_owner(oh, limit=200) if hasattr(doc_index, 'list_by_owner') else []
    for pf_try in folders:
        pf_docs = doc_index.list_by_patient_folder(pf_try, limit=200) if hasattr(doc_index, 'list_by_patient_folder') else []
        for d in pf_docs:
            if d.get("document_id") not in [x.get("document_id") for x in all_docs]:
                all_docs.append(d)

    # กรองตาม type
    type_docs = [d for d in all_docs if d.get("category") == doc_type]
    type_docs.sort(key=lambda x: x.get("upload_date", ""), reverse=True)

    # ── สร้าง signed URLs สำหรับรูป ──
    doc_items = []
    for doc in type_docs[:20]:  # จำกัด 20 ฉบับ
        gcs_path = doc.get("gcs_object_path", "")
        doc_id = doc.get("document_id", "")
        upload_date = doc.get("upload_date", "?")
        signed = ""
        if gcs_path:
            signed = generate_signed_url(gcs_path, expiry_seconds=3600) or ""
        if signed:
            doc_items.append({
                "id": doc_id,
                "date": upload_date,
                "url": signed,
                "gcs": gcs_path,
            })

    # ── Type labels ──
    type_labels = {
        "dialysis": "ฟอกเลือด",
        "lab": "ผลแล็บ",
        "rx": "ใบยา",
        "bills": "ใบเสร็จ/บิล",
    }
    type_label = type_labels.get(doc_type, doc_type)

    # ── Build HTML ──
    # สร้าง JSON สำหรับ JavaScript
    import json as _json
    docs_json = _json.dumps(doc_items, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>เปรียบเทียบ {type_label} — {p_name or hn}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Sarabun',sans-serif; background:#f0f2f5; color:#333; }}
.header {{ background:linear-gradient(135deg,#1a6b3c,#0d7a40); color:#fff;
  padding:16px 20px; position:sticky; top:0; z-index:100; }}
.header h1 {{ font-size:18px; font-weight:600; }}
.header .sub {{ font-size:13px; opacity:0.85; margin-top:4px; }}
.tabs {{ display:flex; background:#fff; border-bottom:2px solid #e0e0e0;
  overflow-x:auto; padding:0 8px; }}
.tab {{ padding:10px 16px; font-size:14px; cursor:pointer; white-space:nowrap;
  border-bottom:3px solid transparent; color:#666; }}
.tab.active {{ color:#1a6b3c; border-bottom-color:#1a6b3c; font-weight:600; }}
.compare-area {{ padding:12px; }}
.doc-selector {{ display:flex; gap:8px; margin-bottom:12px; align-items:center; }}
.doc-selector select {{ flex:1; padding:8px; border:1px solid #ccc;
  border-radius:8px; font-size:14px; }}
.doc-selector label {{ font-size:13px; font-weight:600; color:#555; min-width:60px; }}
.images {{ display:flex; gap:8px; margin-bottom:12px; }}
.img-box {{ flex:1; background:#fff; border-radius:12px;
  box-shadow:0 2px 8px rgba(0,0,0,0.1); overflow:hidden; }}
.img-box .label {{ padding:8px 12px; background:#f8f9fa; font-size:12px;
  font-weight:600; color:#555; border-bottom:1px solid #eee; text-align:center; }}
.img-box img {{ width:100%; display:block; cursor:pointer; }}
.img-box img.zoomed {{ position:fixed; top:0; left:0; width:100%; height:100%;
  object-fit:contain; background:rgba(0,0,0,0.9); z-index:200; cursor:zoom-out; }}
.actions {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }}
.btn {{ padding:10px 16px; border:none; border-radius:8px; font-size:14px;
  cursor:pointer; font-weight:600; display:flex; align-items:center; gap:6px; }}
.btn-primary {{ background:#1a6b3c; color:#fff; }}
.btn-warn {{ background:#e65100; color:#fff; }}
.btn-blue {{ background:#1565c0; color:#fff; }}
.btn-gray {{ background:#78909c; color:#fff; }}
.note-area {{ margin-bottom:12px; }}
.note-area textarea {{ width:100%; padding:12px; border:1px solid #ccc;
  border-radius:8px; font-size:14px; resize:vertical; min-height:80px; }}
.note-area textarea::placeholder {{ color:#999; }}
.status {{ padding:12px; background:#fff; border-radius:8px;
  margin-bottom:12px; font-size:13px; color:#666; text-align:center; }}
.toast {{ position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
  background:#333; color:#fff; padding:12px 24px; border-radius:8px;
  font-size:14px; z-index:300; display:none; }}
.dialysis-card {{ display:flex; gap:12px; flex-wrap:wrap; padding:10px 12px;
  background:#e8f5e9; border-radius:8px; margin-bottom:12px; font-size:13px; }}
.dialysis-card .dc-item {{ display:flex; gap:4px; align-items:center; }}
.dialysis-card .dc-label {{ font-weight:600; color:#2e7d32; }}
.dialysis-card .dc-val {{ color:#333; }}
@media(max-width:600px) {{
  .images {{ flex-direction:column; }}
  .dialysis-card {{ flex-direction:column; gap:6px; }}
}}
</style>
</head>
<body>

<div class="header">
  <h1>📋 เปรียบเทียบ{type_label} — {p_name or hn}</h1>
  <div class="sub">HN: {p_clinic or p_hospital or hn} | เอกสาร {len(doc_items)} ฉบับ</div>
</div>

<div class="tabs">
  <div class="tab {'active' if doc_type=='dialysis' else ''}"
       onclick="switchType('dialysis')">🩸 ฟอกเลือด</div>
  <div class="tab {'active' if doc_type=='lab' else ''}"
       onclick="switchType('lab')">🔬 แล็บ</div>
  <div class="tab {'active' if doc_type=='rx' else ''}"
       onclick="switchType('rx')">💊 ยา</div>
  <div class="tab {'active' if doc_type=='bills' else ''}"
       onclick="switchType('bills')">📄 บิล</div>
</div>

{"" if not _cmp_patient else f'''
<div class="compare-area" style="padding-bottom:0">
  <div class="dialysis-card">
    <div class="dc-item"><span class="dc-label">💊 ESA:</span>
      <span class="dc-val">{_cmp_esa or "ไม่ระบุ"}</span></div>
    <div class="dc-item"><span class="dc-label">🩸 Vascular:</span>
      <span class="dc-val">{_cmp_va or "ไม่ระบุ"}</span></div>
    <div class="dc-item"><span class="dc-label">📅 รอบฟอก:</span>
      <span class="dc-val">{_cmp_schedule or "-"} ({_cmp_hd} ครั้ง/สัปดาห์)</span></div>
  </div>
</div>
'''}

<div class="compare-area">

  <div class="doc-selector">
    <label>ครั้งก่อน:</label>
    <select id="selPrev" onchange="updateImages()"></select>
  </div>
  <div class="doc-selector">
    <label>ครั้งนี้:</label>
    <select id="selCur" onchange="updateImages()"></select>
  </div>

  <div class="images">
    <div class="img-box">
      <div class="label" id="labelPrev">ครั้งก่อน</div>
      <img id="imgPrev" src="" alt="ครั้งก่อน" onclick="toggleZoom(this)">
    </div>
    <div class="img-box">
      <div class="label" id="labelCur">ครั้งนี้</div>
      <img id="imgCur" src="" alt="ครั้งนี้" onclick="toggleZoom(this)">
    </div>
  </div>

  <div class="note-area">
    <textarea id="noteText" placeholder="📝 บันทึกคำสั่งการรักษา / แก้ไข..."></textarea>
  </div>

  <div class="actions">
    <button class="btn btn-primary" onclick="saveNote('treatment')">
      💊 สั่งการรักษา
    </button>
    <button class="btn btn-blue" onclick="saveNote('edit')">
      ✏️ แก้ไขข้อมูล
    </button>
    <button class="btn btn-warn" onclick="refreshDocs()">
      🔄 Update ล่าสุด
    </button>
    <button class="btn btn-gray" onclick="sendToLine()">
      📤 ส่ง LINE
    </button>
  </div>

  <div class="status" id="statusBar">
    เลือกเอกสาร 2 ฉบับเพื่อเปรียบเทียบ
  </div>

</div>

<div class="toast" id="toast"></div>

<script>
const DOCS = {docs_json};
const HN = "{hn_q}";
let SECRET = sessionStorage.getItem('_admin_secret') || "";
const BASE = "{base_url}";
const DOC_TYPE = "{doc_type}";

// ── Populate selects ──
function populateSelects() {{
  const selPrev = document.getElementById('selPrev');
  const selCur = document.getElementById('selCur');
  selPrev.innerHTML = '';
  selCur.innerHTML = '';

  if (DOCS.length === 0) {{
    selPrev.innerHTML = '<option>ไม่มีเอกสาร</option>';
    selCur.innerHTML = '<option>ไม่มีเอกสาร</option>';
    document.getElementById('statusBar').textContent = 'ยังไม่มีเอกสาร กรุณาส่งเอกสารผ่าน LINE OA ก่อน';
    return;
  }}

  DOCS.forEach((d, i) => {{
    const opt1 = new Option(d.date + ' (#' + (i+1) + ')', i);
    const opt2 = new Option(d.date + ' (#' + (i+1) + ')', i);
    selPrev.add(opt1);
    selCur.add(opt2);
  }});

  // Default: ครั้งก่อน = index 1, ครั้งนี้ = index 0
  if (DOCS.length >= 2) {{
    selPrev.value = 1;
    selCur.value = 0;
  }}
  updateImages();
}}

function updateImages() {{
  const pi = parseInt(document.getElementById('selPrev').value);
  const ci = parseInt(document.getElementById('selCur').value);

  if (DOCS[pi]) {{
    document.getElementById('imgPrev').src = DOCS[pi].url;
    document.getElementById('labelPrev').textContent = '📅 ครั้งก่อน: ' + DOCS[pi].date;
  }}
  if (DOCS[ci]) {{
    document.getElementById('imgCur').src = DOCS[ci].url;
    document.getElementById('labelCur').textContent = '📅 ครั้งนี้: ' + DOCS[ci].date;
  }}

  document.getElementById('statusBar').textContent =
    'แสดง ' + DOCS.length + ' ฉบับ | กดรูปเพื่อซูม';
}}

function toggleZoom(img) {{
  img.classList.toggle('zoomed');
}}

function switchType(type) {{
  window.location.href = BASE + '/admin/compare/' + HN + '?type=' + type;
}}

function showToast(msg) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3000);
}}

function saveNote(action) {{
  const note = document.getElementById('noteText').value.trim();
  if (!note) {{
    showToast('กรุณาพิมพ์ข้อความก่อน');
    return;
  }}

  // ถ้ายังไม่มี SECRET → ถามครั้งเดียว เก็บไว้ใน sessionStorage
  if (!SECRET) {{
    const s = prompt('กรุณาใส่รหัส Admin เพื่อบันทึกคำสั่งการรักษา:');
    if (!s) return;
    SECRET = s;
    sessionStorage.setItem('_admin_secret', s);
  }}

  const ci = parseInt(document.getElementById('selCur').value);
  const docId = DOCS[ci] ? DOCS[ci].id : '';

  fetch(BASE + '/admin/api/v2/treatment-note', {{
    method: 'POST',
    headers: {{
      'Content-Type': 'application/json',
      'X-Admin-Secret': SECRET,
    }},
    credentials: 'same-origin',
    body: JSON.stringify({{
      secret: SECRET,
      hn: HN,
      doc_id: docId,
      action: action,
      note: note,
      doc_type: DOC_TYPE,
    }})
  }})
  .then(r => {{
    if (r.status === 401) {{
      SECRET = '';
      sessionStorage.removeItem('_admin_secret');
      showToast('❌ รหัส Admin ไม่ถูกต้อง กรุณาลองใหม่');
      return null;
    }}
    return r.json();
  }})
  .then(data => {{
    if (!data) return;
    if (data.success) {{
      showToast('✅ บันทึกเรียบร้อย');
      document.getElementById('noteText').value = '';
    }} else {{
      showToast('❌ ' + (data.error || 'เกิดข้อผิดพลาด'));
    }}
  }})
  .catch(e => showToast('❌ เชื่อมต่อไม่ได้'));
}}

function refreshDocs() {{
  window.location.reload();
}}

function sendToLine() {{
  if (!SECRET) {{
    const s = prompt('กรุณาใส่รหัส Admin เพื่อส่ง LINE:');
    if (!s) return;
    SECRET = s;
    sessionStorage.setItem('_admin_secret', s);
  }}

  const note = document.getElementById('noteText').value.trim();
  const ci = parseInt(document.getElementById('selCur').value);
  const docId = DOCS[ci] ? DOCS[ci].id : '';

  fetch(BASE + '/admin/api/v2/send-note-line', {{
    method: 'POST',
    headers: {{
      'Content-Type': 'application/json',
      'X-Admin-Secret': SECRET,
    }},
    credentials: 'same-origin',
    body: JSON.stringify({{
      hn: HN,
      doc_id: docId,
      note: note || 'ดูเอกสารแล้ว',
      doc_type: DOC_TYPE,
    }})
  }})
  .then(r => {{
    if (r.status === 401) {{
      SECRET = '';
      sessionStorage.removeItem('_admin_secret');
      showToast('❌ รหัส Admin ไม่ถูกต้อง');
      return null;
    }}
    return r.json();
  }})
  .then(data => {{
    if (!data) return;
    if (data.success) showToast('✅ ส่ง LINE แล้ว (' + (data.sent || 0) + ' คน)');
    else showToast('❌ ' + (data.error || 'ส่งไม่ได้'));
  }})
  .catch(e => showToast('❌ เชื่อมต่อไม่ได้'));
}}

populateSelects();
</script>
</body></html>"""

    return html


# ── Treatment Note API ──
@app.route("/admin/api/v2/treatment-note", methods=["POST"])
def api_treatment_note():
    """บันทึกคำสั่งการรักษา/แก้ไข จากหน้าเปรียบเทียบ"""
    data = request.get_json(force=True) or {}
    # หน้าเปรียบเทียบส่ง secret มาใน JSON body — ตรวจกับ ADMIN_SECRET
    # (เดิมเรียก _check_admin_secret() ซึ่งไม่มีใน server.py → NameError 500
    #  ทุกครั้งที่กดบันทึก; CI จับได้ตั้งแต่ 9381ca4 แต่ deploy ไปก่อน)
    # Auth: body secret / X-Admin-Secret header / cookie (เลือกอันใดก็ได้)
    if not (_admin_api_auth() or _admin_cookie_ok()):
        # fallback: ตรวจ secret ใน body (สำหรับ backward compatibility)
        _admin_secret = os.getenv("ADMIN_SECRET", "").strip()
        _body_secret = str(data.get("secret", "") or "")
        _body_ok = bool(_admin_secret and _body_secret) and hmac.compare_digest(
            _body_secret.encode("utf-8", "surrogateescape"),
            _admin_secret.encode("utf-8"),
        )
        if not _body_ok:
            return jsonify({"error": "Unauthorized"}), 401
    hn = data.get("hn", "")
    doc_id = data.get("doc_id", "")
    action = data.get("action", "")
    note = data.get("note", "")
    doc_type = data.get("doc_type", "")

    if not note:
        return jsonify({"error": "กรุณาพิมพ์ข้อความ"}), 400

    # บันทึกลง GCS
    try:
        note_data = {
            "timestamp": time.time(),
            "hn": hn,
            "doc_id": doc_id,
            "action": action,
            "note": note,
            "doc_type": doc_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        note_path = f"_treatment_notes/{hn}/{int(time.time())}.json"
        if storage.is_ready and hasattr(storage, '_bucket'):
            blob = storage._bucket.blob(note_path)
            blob.upload_from_string(
                json.dumps(note_data, ensure_ascii=False),
                content_type="application/json",
            )
        logger.info("Treatment note saved: HN=%s action=%s", hn, action)
        return jsonify({"success": True, "path": note_path})
    except Exception as e:
        logger.error("Treatment note failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/v2/send-note-line", methods=["POST"])
def api_send_note_line():
    """ส่งคำสั่งการรักษา/ข้อความจากหน้าเปรียบเทียบไปยังเจ้าหน้าที่ผ่าน LINE

    ส่งถึง: staff LINE IDs ทั้งหมด (เจ้าหน้าที่)
    เนื้อหา: แพทย์ดูเอกสาร HN xxx + สั่งการรักษา
    """
    if not (_admin_api_auth() or _admin_cookie_ok()):
        data = request.get_json(force=True) or {}
        _admin_secret = os.getenv("ADMIN_SECRET", "").strip()
        _body_secret = str(data.get("secret", "") or "")
        _body_ok = bool(_admin_secret and _body_secret) and hmac.compare_digest(
            _body_secret.encode("utf-8", "surrogateescape"),
            _admin_secret.encode("utf-8"),
        )
        if not _body_ok:
            return jsonify({"error": "Unauthorized"}), 401
    else:
        data = request.get_json(force=True) or {}

    hn = data.get("hn", "")
    note = data.get("note", "ดูเอกสารแล้ว")
    doc_type = data.get("doc_type", "")

    # หา patient name
    patient_name = ""
    if master_registry and hn:
        p = master_registry.match_hn(hn)
        if p:
            patient_name = p.patient_name or ""

    # สร้างข้อความ
    _type_th = {"lab": "ผลแล็บ", "rx": "ใบยา", "dialysis": "ใบฟอกเลือด",
                "bill": "บิล"}.get(doc_type, doc_type or "เอกสาร")
    msg = (f"📋 คำสั่งจากแพทย์\n"
           f"━━━━━━━━━━━━━━━━\n"
           f"🔢 HN: {hn}\n")
    if patient_name:
        msg += f"👤 {patient_name}\n"
    msg += (f"📂 ประเภท: {_type_th}\n"
            f"\n💊 {note}\n"
            f"\n⏰ {datetime.now(BKK_TZ).strftime('%d/%m/%Y %H:%M')}")

    # ส่งให้เจ้าหน้าที่ทั้งหมด
    sent = 0
    for staff_id in _staff_line_ids():
        try:
            push_message(staff_id, msg)
            sent += 1
        except Exception:
            pass

    logger.info("Send note LINE: HN=%s sent=%d", hn, sent)
    return jsonify({"success": True, "sent": sent})


@app.route("/admin/api/v2/treatment-notes/<hn>", methods=["GET"])
def api_treatment_notes_list(hn):
    """ดึงประวัติคำสั่งการรักษาของผู้ป่วย"""
    if not _admin_api_auth():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        notes = []
        prefix = f"_treatment_notes/{hn}/"
        if storage.is_ready and hasattr(storage, '_bucket'):
            blobs = storage._bucket.list_blobs(prefix=prefix)
            for blob in blobs:
                try:
                    data = json.loads(blob.download_as_text())
                    data["_path"] = blob.name
                    notes.append(data)
                except Exception:
                    pass
        # เรียงจากใหม่สุด
        notes.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return jsonify({
            "hn": hn,
            "count": len(notes),
            "notes": notes,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/patient/<hn>", methods=["GET", "POST"])
def admin_patient_page(hn):
    """หน้าเว็บสรุปผู้ป่วย — cookie auth (set via ?secret= redirect)"""
    auth_err = _admin_page_auth()
    if auth_err is not None:
        return auth_err

    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    hn = hn.strip()[:20]
    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    hn_q = hn.replace("HN", "").replace("hn", "").strip()

    # ── ดึงข้อมูลทั้งหมด server-side ──
    (found, oh, pf, p_name, p_clinic, p_hospital,
     folders, err) = _resolve_patient_for_api(hn_q)
    if not found:
        return (
            f"<html><body style='font-family:sans-serif;"
            f"text-align:center;padding:40px'>"
            f"<h2>ไม่พบผู้ป่วย HN {hn}</h2>"
            f"<p>{err}</p></body></html>"
        ), 404

    lab_h = []
    rx_h = []
    dial_h = []
    try:
        lab_h = _try_history_multi(
            get_patient_lab_history, oh, folders
        )[:15]
    except Exception:
        pass
    try:
        rx_h = _try_history_multi(
            get_patient_rx_history, oh, folders
        )[:15]
    except Exception:
        pass
    try:
        dial_h = _try_history_multi(
            get_patient_dialysis_history, oh, folders
        )[:15]
    except Exception:
        pass

    # ── เอกสารทั้งหมด (เฉพาะ HN ที่ระบุ) ──
    all_docs = []
    try:
        # ใช้ patient_folder เป็นหลัก ไม่ใช้ owner_hash
        # (owner_hash ของ staff จะดึงเอกสารคนไข้ทุกคนมา)
        for _f in folders:
            all_docs = doc_index.list_by_patient_folder(_f)
            if all_docs:
                break
        # Fallback: owner_hash เฉพาะเมื่อไม่มี folder
        if not all_docs and oh and not folders:
            all_docs = doc_index.list_by_owner(oh)
    except Exception:
        pass
    doc_count = len(all_docs) if all_docs else 0

    # ── สร้าง HTML สำหรับ tab ผลแล็บ ──
    _dial_names = {
        "dry_weight": "น้ำหนักแห้ง", "uf_volume": "UF",
        "duration": "เวลาฟอก(ชม.)", "dialyzer": "Dialyzer",
        "blood_flow": "Blood Flow", "hd_no": "HD No.",
        "pre_weight": "น้ำหนักก่อน", "post_weight": "น้ำหนักหลัง",
        "pre_bp": "BP ก่อน", "post_bp": "BP หลัง", "kt_v": "Kt/V",
    }

    def _build_lab_cards():
        if not lab_h:
            return "<p class='empty'>ยังไม่มีผลแล็บในระบบ</p>"
        parts = []
        for i, lb in enumerate(lab_h):
            dt = lb.get("upload_date", "?")
            results = lb.get("results", [])
            did = lb.get("doc_id", "")
            rows = ""
            for r in results:
                rows += (
                    f"<tr><td>{r.get('analyte', '?')}</td>"
                    f"<td><b>{r.get('value', '')}</b></td>"
                    f"<td>{r.get('unit', '')}</td></tr>"
                )
            if not rows:
                rows = "<tr><td colspan='3'>ไม่มีข้อมูล</td></tr>"
            view_btn = ""
            if did and base_url:
                view_btn = (
                    f"<a href='{base_url}/admin/doc/view/{did}"
                    f"' class='btn-sm'>📎 เปิดเอกสาร</a>"
                )
            parts.append(
                f"<div class='card'>"
                f"<div class='card-hdr' onclick='toggleCard(this)'>"
                f"<span>📅 {dt}</span>"
                f"<span class='badge'>{len(results)} ค่า</span>"
                f"<span class='arrow'>▼</span></div>"
                f"<div class='card-body' style='display:none'>"
                f"<table class='data-tbl'>"
                f"<tr><th>ค่า</th><th>ผล</th><th>หน่วย</th></tr>"
                f"{rows}</table>"
                f"{view_btn}</div></div>"
            )
        return "".join(parts)

    def _build_rx_cards():
        if not rx_h:
            return "<p class='empty'>ยังไม่มีใบยาในระบบ</p>"
        parts = []
        for rx in rx_h:
            dt = rx.get("upload_date", "?")
            drugs = rx.get("drugs", [])
            did = rx.get("doc_id", "")
            drug_list = ""
            for d in drugs:
                drug_list += f"<li>{d}</li>"
            if not drug_list:
                drug_list = "<li>ไม่พบรายการยา</li>"
            view_btn = ""
            if did and base_url:
                view_btn = (
                    f"<a href='{base_url}/admin/doc/view/{did}"
                    f"' class='btn-sm'>📎 เปิดเอกสาร</a>"
                )
            parts.append(
                f"<div class='card'>"
                f"<div class='card-hdr' onclick='toggleCard(this)'>"
                f"<span>📅 {dt}</span>"
                f"<span class='badge'>{len(drugs)} รายการ</span>"
                f"<span class='arrow'>▼</span></div>"
                f"<div class='card-body' style='display:none'>"
                f"<ul class='drug-list'>{drug_list}</ul>"
                f"{view_btn}</div></div>"
            )
        return "".join(parts)

    def _build_dial_cards():
        if not dial_h:
            return "<p class='empty'>ยังไม่มีข้อมูลฟอกเลือดในระบบ</p>"
        parts = []
        for dl in dial_h:
            dt = dl.get("upload_date", "?")
            data = dl.get("data", {})
            did = dl.get("doc_id", "")
            rows = ""
            if isinstance(data, dict):
                for k, v in data.items():
                    dn = _dial_names.get(k, k)
                    rows += f"<tr><td>{dn}</td><td><b>{v}</b></td></tr>"
            if not rows:
                rows = "<tr><td colspan='2'>ไม่มีข้อมูล</td></tr>"
            view_btn = ""
            if did and base_url:
                view_btn = (
                    f"<a href='{base_url}/admin/doc/view/{did}"
                    f"' class='btn-sm'>📎 เปิดเอกสาร</a>"
                )
            parts.append(
                f"<div class='card'>"
                f"<div class='card-hdr' onclick='toggleCard(this)'>"
                f"<span>📅 {dt}</span>"
                f"<span class='badge'>{len(data)} ค่า</span>"
                f"<span class='arrow'>▼</span></div>"
                f"<div class='card-body' style='display:none'>"
                f"<table class='data-tbl'>"
                f"<tr><th>พารามิเตอร์</th><th>ค่า</th></tr>"
                f"{rows}</table>"
                f"{view_btn}</div></div>"
            )
        return "".join(parts)

    lab_cards = _build_lab_cards()
    rx_cards = _build_rx_cards()
    dial_cards = _build_dial_cards()

    # ── สร้าง comparison data สำหรับ JS ──
    # เปรียบเทียบ 2 รายการล่าสุดของแล็บ
    _cmp_html = ""
    if len(lab_h) >= 2:
        _new = lab_h[0]
        _old = lab_h[1]
        _cmp_rows = ""
        _new_map = {}
        for r in _new.get("results", []):
            _new_map[r.get("analyte", "")] = r
        for r in _old.get("results", []):
            a = r.get("analyte", "")
            ov = r.get("value", "")
            nv_r = _new_map.pop(a, {})
            nv = nv_r.get("value", "-")
            unit = r.get("unit", "") or nv_r.get("unit", "")
            trend = "→"
            try:
                if float(nv) > float(ov):
                    trend = "<span class='up'>↑</span>"
                elif float(nv) < float(ov):
                    trend = "<span class='down'>↓</span>"
            except (ValueError, TypeError):
                pass
            _cmp_rows += (
                f"<tr><td>{a}</td><td>{ov}</td>"
                f"<td>{nv}</td><td>{unit}</td>"
                f"<td>{trend}</td></tr>"
            )
        # ค่าที่มีเฉพาะครั้งใหม่
        for a, r in _new_map.items():
            _cmp_rows += (
                f"<tr><td>{a}</td><td>-</td>"
                f"<td>{r.get('value', '')}</td>"
                f"<td>{r.get('unit', '')}</td>"
                f"<td><span class='new'>ใหม่</span></td></tr>"
            )
        _cmp_html = (
            f"<div class='card cmp-card'>"
            f"<h3>📊 เปรียบเทียบแล็บ</h3>"
            f"<p class='cmp-dates'>"
            f"ครั้งก่อน: {_old.get('upload_date', '?')} → "
            f"ล่าสุด: {_new.get('upload_date', '?')}</p>"
            f"<table class='data-tbl cmp-tbl'>"
            f"<tr><th>ค่า</th><th>ก่อน</th>"
            f"<th>ใหม่</th><th>หน่วย</th><th>แนวโน้ม</th></tr>"
            f"{_cmp_rows}</table></div>"
        )

    # ปุ่มสั่งการรักษา
    _annotate_btn = ""
    if all_docs and base_url:
        _latest_did = all_docs[0].get("document_id", "")
        if _latest_did:
            _annotate_btn = (
                f"<a href='{base_url}/admin/annotate/{_latest_did}"
                f"' class='bottom-btn'>"
                f"✏️ สั่งการรักษา</a>"
            )

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>สรุปผู้ป่วย — HN {hn_q}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f5f5f5;color:#1a1a1a;padding-bottom:70px}}
.hdr{{background:linear-gradient(135deg,#1a73e8,#0d47a1);color:#fff;padding:20px 16px}}
.hdr h1{{font-size:18px}} .hdr p{{font-size:13px;opacity:.85;margin-top:4px}}
.stats{{display:flex;gap:8px;margin-top:8px}}
.stat{{background:rgba(255,255,255,.2);padding:4px 10px;border-radius:12px;font-size:12px}}
.tabs{{display:flex;border-bottom:2px solid #e0e0e0;background:#fff;position:sticky;top:0;z-index:10}}
.tab{{flex:1;padding:12px 0;text-align:center;font-size:14px;font-weight:600;color:#666;cursor:pointer;border-bottom:3px solid transparent}}
.tab.active{{color:#1a73e8;border-bottom-color:#1a73e8}}
.content{{padding:12px}}
.card{{background:#fff;border-radius:10px;margin-bottom:8px;box-shadow:0 1px 2px rgba(0,0,0,.08);overflow:hidden}}
.card-hdr{{display:flex;align-items:center;justify-content:space-between;padding:12px;cursor:pointer}}
.card-hdr:active{{background:#f0f0f0}}
.badge{{background:#e3f2fd;color:#1565c0;padding:2px 8px;border-radius:10px;font-size:12px}}
.arrow{{color:#999;font-size:12px;transition:transform .2s}}
.arrow.open{{transform:rotate(180deg)}}
.card-body{{padding:0 12px 12px;border-top:1px solid #f0f0f0}}
.data-tbl{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
.data-tbl th{{text-align:left;color:#666;padding:6px 4px;border-bottom:1px solid #eee;font-weight:500}}
.data-tbl td{{padding:6px 4px;border-bottom:1px solid #f5f5f5}}
.drug-list{{list-style:none;padding:8px 0}} .drug-list li{{padding:4px 0;font-size:14px}}
.drug-list li::before{{content:'💊 '}}
.btn-sm{{display:inline-block;padding:6px 14px;background:#1DB446;color:#fff;border-radius:6px;text-decoration:none;font-size:13px;margin-top:8px}}
.empty{{text-align:center;padding:30px;color:#999}}
.cmp-card{{border:2px solid #1a73e8;margin:12px}}
.cmp-card h3{{padding:12px;color:#1a73e8;font-size:16px}}
.cmp-dates{{padding:0 12px 8px;font-size:13px;color:#666}}
.cmp-tbl{{margin:0 12px 12px;width:calc(100% - 24px)}}
.up{{color:#c62828;font-weight:700}} .down{{color:#2e7d32;font-weight:700}}
.new{{color:#1565c0;font-size:12px}}
.bottom-btn{{display:block;position:fixed;bottom:0;left:0;right:0;padding:14px;background:#0066cc;color:#fff;text-align:center;font-size:16px;font-weight:600;text-decoration:none;z-index:20}}
.tab-content{{display:none}} .tab-content.active{{display:block}}

</style>
</head>
<body>
<div class="hdr">
<h1>{p_name or 'ผู้ป่วย'}</h1>
<p>HN คลินิก: {p_clinic} | HN รพ.: {p_hospital}</p>
<div class="stats">
<span class="stat">🔬 แล็บ {len(lab_h)}</span>
<span class="stat">💊 ยา {len(rx_h)}</span>
<span class="stat">🩸 ฟอก {len(dial_h)}</span>
<span class="stat">📁 ทั้งหมด {doc_count}</span>
</div>
</div>

<div class="tabs">
<div class="tab active" onclick="showTab('lab',this)">🔬 ผลแล็บ</div>
<div class="tab" onclick="showTab('rx',this)">💊 ใบยา</div>
<div class="tab" onclick="showTab('dial',this)">🩸 ฟอกเลือด</div>
</div>

<div id="tab-lab" class="tab-content active">
<div class="content">
{_cmp_html}
{lab_cards}
</div>
</div>

<div id="tab-rx" class="tab-content">
<div class="content">
{rx_cards}
</div>
</div>

<div id="tab-dial" class="tab-content">
<div class="content">
{dial_cards}
</div>
</div>

{_annotate_btn}

<script>
function showTab(id,el) {{
    document.querySelectorAll('.tab-content').forEach(function(t){{t.classList.remove('active')}});
    document.querySelectorAll('.tab').forEach(function(t){{t.classList.remove('active')}});
    document.getElementById('tab-'+id).classList.add('active');
    el.classList.add('active');
}}
function toggleCard(hdr) {{
    var body = hdr.nextElementSibling;
    var arrow = hdr.querySelector('.arrow');
    if (body.style.display === 'none') {{
        body.style.display = 'block';
        if (arrow) arrow.classList.add('open');
    }} else {{
        body.style.display = 'none';
        if (arrow) arrow.classList.remove('open');
    }}
}}
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


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

    cat_names = _CAT_NAMES  # ตารางชื่อประเภทรวมที่เดียว
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

    # ── Rich Menu Quick Commands ─────────────────────────────
    # "ส่งเอกสาร" — แนะนำวิธีส่งเอกสาร
    if text_stripped == "ส่งเอกสาร":
        return (
            "📄 วิธีส่งเอกสาร\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "📸 ส่งรูปถ่ายหรือ PDF ของเอกสารได้เลย:\n"
            "• ใบผลแล็บ\n"
            "• ใบยา/ใบสั่งยา\n"
            "• ใบฟอกเลือด\n"
            "• บิล/ใบเสร็จ\n"
            "\n"
            "💡 ระบบจะจัดประเภทและเก็บเข้าแฟ้มอัตโนมัติ\n"
            "\n"
            "📌 หากต้องการระบุผู้ป่วย:\n"
            "พิมพ์ 'ผู้ป่วย <HN>' ก่อนส่งเอกสาร\n"
            "ตัวอย่าง: ผู้ป่วย 1234"
        )

    # "ลงทะเบียน" — แนะนำวิธีลงทะเบียน HN
    if text_stripped == "ลงทะเบียน":
        return (
            "📝 ลงทะเบียน HN กับ LINE\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "ผูก HN ของคุณกับบัญชี LINE\n"
            "เพื่อดูผลแล็บ/ใบยาของตัวเองได้\n"
            "\n"
            "🔐 เพื่อความปลอดภัยของข้อมูล\n"
            "ขอรหัสผูกบัญชี 6 หลักจากเจ้าหน้าที่คลินิก\n"
            "แล้วพิมพ์ในแชทนี้:\n"
            "ผูก <รหัส 6 หลัก>\n"
            "\n"
            "ตัวอย่าง: ผูก 482913\n"
            "\n"
            "💡 รหัสใช้ได้ครั้งเดียว ภายใน 48 ชั่วโมง"
        )

    # ── "ผลเลือด" — ลิงก์เปิดหน้า HD Labs (LIFF) สำหรับผู้ป่วย/แพทย์ ──
    if text_stripped in ("ผลเลือด", "ผลเลือดของฉัน", "ดูผลเลือด", "hd labs", "ผลhd", "ผล hd"):
        _liff_id = os.environ.get("LIFF_ID", "")
        _hd_url = (f"https://liff.line.me/{_liff_id}/hd-labs" if _liff_id
                   else f"{_RENDER_BASE}/liff/hd-labs")
        return (
            "\U0001fa78 ผลเลือดและผลฟอกของคุณ\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "ดู Hb · Hct · Kt/V · สารน้ำ (BCM)\n"
            "พร้อมกราฟแนวโน้มและคำแนะนำ:\n"
            f"{_hd_url}\n"
            "\n"
            "\U0001f4a1 ถ้ายังไม่ได้ผูกบัญชี พิมพ์ 'ลงทะเบียน'"
        )

    # ── Admin: Deploy Rich Menu (Staff/Patient/Sub) ─────────
    if text_stripped in ("ตั้งค่าเมนู", "deploy richmenu", "อัปเดตเมนู"):
        if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
            return None

        if not LINE_CHANNEL_ACCESS_TOKEN:
            return "❌ LINE_CHANNEL_ACCESS_TOKEN ยังไม่ได้ตั้งค่า"

        import requests as _req

        _liff_id = os.environ.get("LIFF_ID", "")
        _liff_url = f"https://liff.line.me/{_liff_id}" if _liff_id else ""
        _base_dir = os.path.dirname(os.path.abspath(__file__))
        _headers = {
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }

        def _rm_cleanup():
            """ลบ Rich Menu + Alias เก่าทั้งหมด"""
            _req.delete("https://api.line.me/v2/bot/user/all/richmenu",
                        headers=_headers, timeout=15)
            # ลบ aliases ก่อน (ต้องลบก่อน menu)
            for _alias_id in ("richmenu-alias-staff", "richmenu-alias-patient",
                              "richmenu-alias-sub"):
                _req.delete(f"https://api.line.me/v2/bot/richmenu/alias/{_alias_id}",
                            headers=_headers, timeout=15)
            # ลบ menus ทั้งหมด
            r = _req.get("https://api.line.me/v2/bot/richmenu/list",
                         headers=_headers, timeout=15)
            for m in (r.json().get("richmenus", []) if r.status_code == 200 else []):
                mid = m.get("richMenuId", "")
                if mid:
                    _req.delete(f"https://api.line.me/v2/bot/richmenu/{mid}",
                                headers=_headers, timeout=15)

        def _rm_create(name, areas):
            """สร้าง Rich Menu และคืน richMenuId"""
            body = {
                "size": {"width": 2500, "height": 843},
                "selected": True,
                "name": name,
                "chatBarText": "เมนู",
                "areas": areas,
            }
            r = _req.post("https://api.line.me/v2/bot/richmenu",
                          json=body, headers=_headers, timeout=15)
            if r.status_code != 200:
                return None, f"สร้าง {name} ล้มเหลว ({r.status_code})"
            return r.json().get("richMenuId", ""), None

        def _rm_upload_img(menu_id, img_file):
            """อัปโหลดรูปภาพ"""
            path = os.path.join(_base_dir, img_file)
            if not os.path.exists(path):
                return False
            with open(path, "rb") as f:
                data = f.read()
            r = _req.post(
                f"https://api-data.line.me/v2/bot/richmenu/{menu_id}/content",
                data=data,
                headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                         "Content-Type": "image/png"},
                timeout=30)
            return r.status_code == 200

        def _rm_create_alias(alias_id, menu_id):
            """สร้าง Rich Menu Alias"""
            r = _req.post("https://api.line.me/v2/bot/richmenu/alias",
                          json={"richMenuAliasId": alias_id,
                                "richMenuId": menu_id},
                          headers=_headers, timeout=15)
            return r.status_code == 200

        try:
            # แถบหัว 110px บอกชนิดเมนู (ไม่มีปุ่ม) — ตรงกับ scripts/make_rich_menus.py
            _col_w = 833
            _top = 110
            _row_h = 366

            # === Step 0: Cleanup ===
            _rm_cleanup()
            _log = []

            # === Step 1: Staff Menu ===
            _staff_areas = [
                {"bounds": {"x": 0, "y": _top, "width": _col_w, "height": _row_h},
                 "action": {"type": "uri", "label": "ดูผลแล็บ",
                            "uri": _liff_url or f"{_RENDER_BASE}/liff"}},
                {"bounds": {"x": _col_w, "y": _top, "width": _col_w + 1, "height": _row_h},
                 "action": {"type": "message", "label": "ส่งเอกสาร", "text": "ส่งเอกสาร"}},
                {"bounds": {"x": _col_w * 2 + 1, "y": _top, "width": _col_w, "height": _row_h},
                 "action": {"type": "message", "label": "รอบฟอก", "text": "รอบฟอกวันนี้"}},
                {"bounds": {"x": 0, "y": _top + _row_h, "width": _col_w, "height": _row_h + 1},
                 "action": {"type": "message", "label": "วิเคราะห์", "text": "วิเคราะห์"}},
                {"bounds": {"x": _col_w, "y": _top + _row_h, "width": _col_w + 1, "height": _row_h + 1},
                 "action": {"type": "message", "label": "ดูนัด", "text": "นัดหมาย"}},
                {"bounds": {"x": _col_w * 2 + 1, "y": _top + _row_h, "width": _col_w, "height": _row_h + 1},
                 "action": {"type": "richmenuswitch",
                            "richMenuAliasId": "richmenu-alias-sub",
                            "data": "switch-to-sub"}},
            ]
            _staff_id, _err = _rm_create("Staff Menu v6", _staff_areas)
            if _err:
                return f"❌ {_err}"
            _img_ok = _rm_upload_img(_staff_id, "rich_menu_staff.png")
            _log.append(f"👨‍⚕️ Staff: ✅ {_staff_id[:12]}..." + ("" if _img_ok else " (⚠️ อัปโหลดรูปไม่สำเร็จ)"))

            # === Step 2: Patient Menu ===
            # ลิงก์ผลเลือด HD (LIFF path /hd-labs) — ผู้ป่วยกดแล้วเปิดผลเลือดทันที
            _hd_labs_url = (f"https://liff.line.me/{_liff_id}/hd-labs" if _liff_id
                            else f"{_RENDER_BASE}/liff/hd-labs")
            _patient_areas = [
                {"bounds": {"x": 0, "y": _top, "width": _col_w, "height": _row_h},
                 "action": {"type": "uri", "label": "🩸 ผลเลือด HD",
                            "uri": _hd_labs_url}},
                {"bounds": {"x": _col_w, "y": _top, "width": _col_w + 1, "height": _row_h},
                 "action": {"type": "uri", "label": "ดูผลแล็บ/เอกสาร",
                            "uri": _liff_url or f"{_RENDER_BASE}/liff"}},
                {"bounds": {"x": _col_w * 2 + 1, "y": _top, "width": _col_w, "height": _row_h},
                 "action": {"type": "message", "label": "ลงทะเบียน", "text": "ลงทะเบียน"}},
                {"bounds": {"x": 0, "y": _top + _row_h, "width": _col_w, "height": _row_h + 1},
                 "action": {"type": "message", "label": "นัดหมาย", "text": "นัดหมาย"}},
                {"bounds": {"x": _col_w, "y": _top + _row_h, "width": _col_w + 1, "height": _row_h + 1},
                 "action": {"type": "message", "label": "ติดต่อ", "text": "ติดต่อเจ้าหน้าที่"}},
                {"bounds": {"x": _col_w * 2 + 1, "y": _top + _row_h, "width": _col_w, "height": _row_h + 1},
                 "action": {"type": "message", "label": "ช่วยเหลือ", "text": "ช่วยเหลือ"}},
            ]
            _patient_id, _err = _rm_create("Patient Menu v6", _patient_areas)
            if _err:
                return f"❌ {_err}"
            _img_ok = _rm_upload_img(_patient_id, "rich_menu_patient.png")
            _log.append(f"🧑‍🤝‍🧑 Patient: ✅ {_patient_id[:12]}..." + ("" if _img_ok else " (⚠️ อัปโหลดรูปไม่สำเร็จ)"))

            # === Step 3: Sub Menu ===
            _sub_areas = [
                {"bounds": {"x": 0, "y": _top, "width": _col_w, "height": _row_h},
                 "action": {"type": "richmenuswitch",
                            "richMenuAliasId": "richmenu-alias-staff",
                            "data": "switch-to-staff"}},
                {"bounds": {"x": _col_w, "y": _top, "width": _col_w + 1, "height": _row_h},
                 "action": {"type": "message", "label": "คำสั่ง", "text": "ช่วยเหลือ"}},
                {"bounds": {"x": _col_w * 2 + 1, "y": _top, "width": _col_w, "height": _row_h},
                 "action": {"type": "message", "label": "ลงทะเบียน", "text": "ลงทะเบียน"}},
                {"bounds": {"x": 0, "y": _top + _row_h, "width": _col_w, "height": _row_h + 1},
                 "action": {"type": "message", "label": "ค้นหา", "text": "ค้นหา"}},
                {"bounds": {"x": _col_w, "y": _top + _row_h, "width": _col_w + 1, "height": _row_h + 1},
                 "action": {"type": "message", "label": "KPI", "text": "KPI"}},
                {"bounds": {"x": _col_w * 2 + 1, "y": _top + _row_h, "width": _col_w, "height": _row_h + 1},
                 "action": {"type": "message", "label": "ระบบ", "text": "ตรวจสอบระบบ"}},
            ]
            _sub_id, _err = _rm_create("Sub Menu v6", _sub_areas)
            if _err:
                return f"❌ {_err}"
            _img_ok = _rm_upload_img(_sub_id, "rich_menu_sub.png")
            _log.append(f"📋 Sub: ✅ {_sub_id[:12]}..." + ("" if _img_ok else " (⚠️ อัปโหลดรูปไม่สำเร็จ)"))

            # === Step 4: Create Aliases ===
            _a1 = _rm_create_alias("richmenu-alias-staff", _staff_id)
            _a2 = _rm_create_alias("richmenu-alias-patient", _patient_id)
            _a3 = _rm_create_alias("richmenu-alias-sub", _sub_id)
            _alias_ok = "✅" if (_a1 and _a2 and _a3) else f"⚠️ ({_a1},{_a2},{_a3})"
            _log.append(f"🔗 Aliases: {_alias_ok}")

            # === Step 5: Set Patient menu as default ===
            r_def = _req.post(
                f"https://api.line.me/v2/bot/user/all/richmenu/{_patient_id}",
                headers=_headers, timeout=15)
            _def_ok = "✅" if r_def.status_code == 200 else "❌"
            _log.append(f"🌐 Default (Patient): {_def_ok}")

            # === Step 6: Link Staff menu to admin/physician ===
            _staff_linked = 0
            _all_staff_ids = _staff_line_ids()
            for _sid in _all_staff_ids:
                r_link = _req.post(
                    f"https://api.line.me/v2/bot/user/{_sid}/richmenu/{_staff_id}",
                    headers=_headers, timeout=10)
                if r_link.status_code == 200:
                    _staff_linked += 1
            _log.append(f"👨‍⚕️ Staff linked: {_staff_linked}/{len(_all_staff_ids)}")

            # Save menu IDs to env-like storage for auto-link
            global _RICH_MENU_IDS
            _RICH_MENU_IDS = {
                "staff": _staff_id,
                "patient": _patient_id,
                "sub": _sub_id,
            }

            return (
                f"🎨 Rich Menu Deploy (Staff/Patient/Sub)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                + "\n".join(_log) +
                f"\n\n💡 ปิด-เปิดแชทเพื่อเห็นเมนูใหม่\n"
                f"• เจ้าหน้าที่ → เมนู Staff + ปุ่ม 'เพิ่มเติม'\n"
                f"• ผู้ป่วย → เมนู Patient"
            )
        except Exception as _e:
            return f"❌ {type(_e).__name__}: {str(_e)[:100]}"

    # ── Admin: ลบ Rich Menu จาก Messaging API ทั้งหมด ──────────
    _text_lower = text_stripped.lower()
    if _text_lower in ("ลบเมนูapi", "ลบเมนู api", "ลบเมนู", "delete richmenu api",
                        "cleanup richmenu", "ลบrichmenu", "ลบ richmenu"):
        if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
            return None

        if not LINE_CHANNEL_ACCESS_TOKEN:
            return "❌ LINE_CHANNEL_ACCESS_TOKEN ยังไม่ได้ตั้งค่า"

        import requests as _req
        _headers = {
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        }

        try:
            # Step 1: ยกเลิก default Rich Menu ของ Messaging API
            _req.delete(
                "https://api.line.me/v2/bot/user/all/richmenu",
                headers=_headers, timeout=15,
            )

            # Step 2: ดึงรายการ Rich Menu ทั้งหมดจาก Messaging API
            r_list = _req.get(
                "https://api.line.me/v2/bot/richmenu/list",
                headers=_headers, timeout=15,
            )
            _menus = r_list.json().get("richmenus", []) if r_list.status_code == 200 else []
            _deleted = 0

            # Step 3: ลบทุก Rich Menu
            for _m in _menus:
                _mid = _m.get("richMenuId", "")
                if _mid:
                    rd = _req.delete(
                        f"https://api.line.me/v2/bot/richmenu/{_mid}",
                        headers=_headers, timeout=15,
                    )
                    if rd.status_code == 200:
                        _deleted += 1

            return (
                f"🧹 Cleanup Rich Menu (Messaging API)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"พบ Rich Menu: {len(_menus)} ตัว\n"
                f"ลบสำเร็จ: {_deleted} ตัว\n"
                f"ยกเลิก Default API: ✅\n"
                f"\n💡 ตอนนี้จะใช้ Rich Menu จาก OA Manager (6 ปุ่ม)\n"
                f"ปิด-เปิดแชท LINE เพื่อเห็นเมนูใหม่"
            )
        except Exception as _e:
            return f"❌ {type(_e).__name__}: {str(_e)[:100]}"

    # ── Google Integration Commands (Admin/Physician only) ────
    # คำสั่ง: "สถานะgoogle", "syncแล็บ", "syncทะเบียน",
    #         "backupไดรฟ์", "ส่งรายงานemail"
    _google_cmds = {
        "สถานะgoogle", "สถานะ google", "google status",
        "syncแล็บ", "sync แล็บ", "ซิงค์แล็บ",
        "syncทะเบียน", "sync ทะเบียน", "ซิงค์ทะเบียน",
        "backupไดรฟ์", "backup ไดรฟ์", "สำรองไดรฟ์", "backup drive",
        "ส่งรายงานemail", "ส่งรายงาน email", "ส่งสรุปemail",
    }
    _text_lower = text_stripped.lower().replace(" ", "")
    _matched_google = any(
        text_stripped in _google_cmds
        or _text_lower == c.lower().replace(" ", "")
        for c in _google_cmds
    )
    if _matched_google:
        if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
            return None  # ไม่เปิดเผยว่ามีคำสั่งนี้

        _norm = _text_lower

        # ── สถานะ Google ──
        if "สถานะ" in _norm or "status" in _norm:
            _lines = ["📊 สถานะ Google Integration\n━━━━━━━━━━━━━━━━━━━━"]
            try:
                import google_drive_sync
                _ds = google_drive_sync.get_status()
                _d_icon = "✅" if _ds.get("connected") else ("⏳" if _ds.get("enabled") else "❌")
                _lines.append(
                    f"\n📁 Google Drive: {_d_icon}\n"
                    f"   Enabled: {_ds.get('enabled', False)}\n"
                    f"   Connected: {_ds.get('connected', False)}"
                )
                if _ds.get("folder_name"):
                    _lines.append(f"   Folder: {_ds['folder_name']}")
            except Exception as _e:
                _lines.append(f"\n📁 Google Drive: ❌ {type(_e).__name__}")
            try:
                import google_sheets_sync
                _ss = google_sheets_sync.get_status()
                _s_icon = "✅" if _ss.get("connected") else ("⏳" if _ss.get("enabled") else "❌")
                _lines.append(
                    f"\n📊 Google Sheets: {_s_icon}\n"
                    f"   Enabled: {_ss.get('enabled', False)}\n"
                    f"   Connected: {_ss.get('connected', False)}"
                )
                if _ss.get("title"):
                    _lines.append(f"   Sheet: {_ss['title']}")
            except Exception as _e:
                _lines.append(f"\n📊 Google Sheets: ❌ {type(_e).__name__}")
            try:
                import gmail_report
                _gs = gmail_report.get_status()
                _g_icon = "✅" if _gs.get("connected") else ("⏳" if _gs.get("enabled") else "❌")
                _lines.append(
                    f"\n📧 Gmail: {_g_icon}\n"
                    f"   Enabled: {_gs.get('enabled', False)}\n"
                    f"   Method: {_gs.get('method', 'N/A')}"
                )
            except Exception as _e:
                _lines.append(f"\n📧 Gmail: ❌ {type(_e).__name__}")
            _lines.append(
                "\n\n💡 คำสั่ง Google:\n"
                "• syncแล็บ — ส่งสรุปผลแล็บไป Sheets\n"
                "• syncทะเบียน — ส่งทะเบียนผู้ป่วยไป Sheets\n"
                "• backupไดรฟ์ — สำรองเอกสารไป Drive\n"
                "• ส่งรายงานemail — ส่งสรุปทาง email"
            )
            return "".join(_lines)

        # ── Sync แล็บ → Sheets ──
        if "แล็บ" in _norm or "lab" in _norm:
            try:
                import google_sheets_sync
                if not google_sheets_sync.is_available():
                    return (
                        "❌ Google Sheets ยังไม่พร้อม\n"
                        "ต้องตั้ง GOOGLE_SHEETS_ENABLED=true\n"
                        "และ GOOGLE_SHEETS_ID บน Render"
                    )
                _result = google_sheets_sync.sync_lab_summary(
                    doc_index, storage
                )
                if _result.get("error"):
                    return f"❌ {_result['error']}"
                return (
                    f"✅ Sync ผลแล็บ → Google Sheets สำเร็จ\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 Sheet: {_result.get('sheet', 'สรุปผลแล็บ')}\n"
                    f"👥 ผู้ป่วย: {_result.get('patient_count', 0)} คน\n"
                    f"📝 อัปเดต: {_result.get('updated_cells', 0)} cells"
                )
            except Exception as _e:
                return f"❌ Sync แล็บไม่สำเร็จ: {type(_e).__name__}"

        # ── Sync ทะเบียน → Sheets ──
        if "ทะเบียน" in _norm or "registry" in _norm:
            try:
                import google_sheets_sync
                if not google_sheets_sync.is_available():
                    return (
                        "❌ Google Sheets ยังไม่พร้อม\n"
                        "ต้องตั้ง GOOGLE_SHEETS_ENABLED=true\n"
                        "และ GOOGLE_SHEETS_ID บน Render"
                    )
                _result = google_sheets_sync.sync_patient_registry(
                    master_registry
                )
                if _result.get("error"):
                    return f"❌ {_result['error']}"
                return (
                    f"✅ Sync ทะเบียนผู้ป่วย → Google Sheets สำเร็จ\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 Sheet: {_result.get('sheet', 'ทะเบียนผู้ป่วย')}\n"
                    f"👥 ผู้ป่วย: {_result.get('patient_count', 0)} คน"
                )
            except Exception as _e:
                return f"❌ Sync ทะเบียนไม่สำเร็จ: {type(_e).__name__}"

        # ── Backup → Drive ──
        if "backup" in _norm or "ไดรฟ" in _norm or "สำรอง" in _norm or "drive" in _norm:
            try:
                import google_drive_sync
                if not google_drive_sync.is_available():
                    return (
                        "❌ Google Drive ยังไม่พร้อม\n"
                        "ต้องตั้ง GOOGLE_DRIVE_ENABLED=true\n"
                        "และ GOOGLE_DRIVE_FOLDER_ID บน Render"
                    )
                # Backup เอกสารล่าสุด 10 รายการ
                _recent = []
                if doc_index and hasattr(doc_index, "_docs"):
                    _sorted = sorted(
                        doc_index._docs,
                        key=lambda d: d.get("upload_date", ""),
                        reverse=True,
                    )
                    _recent = _sorted[:10]
                if not _recent:
                    return "⚠️ ยังไม่มีเอกสารในระบบ"
                _ok = 0
                _fail = 0
                for _doc in _recent:
                    _gcs = _doc.get("gcs_object_path", "")
                    if not _gcs:
                        continue
                    _cat = _doc.get("category", "other")
                    _r = google_drive_sync.backup_document_from_gcs(
                        storage, _gcs, subfolder=_cat
                    )
                    if _r.get("error"):
                        _fail += 1
                    else:
                        _ok += 1
                return (
                    f"✅ Backup → Google Drive\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📁 สำเร็จ: {_ok} ไฟล์\n"
                    f"{'❌ ไม่สำเร็จ: ' + str(_fail) + ' ไฟล์' if _fail else ''}\n"
                    f"\n💡 เอกสารใหม่จะถูก backup อัตโนมัติ"
                )
            except Exception as _e:
                return f"❌ Backup ไม่สำเร็จ: {type(_e).__name__}"

        # ── ส่งรายงาน Email ──
        if "email" in _norm or "อีเมล" in _norm or "รายงาน" in _norm:
            try:
                import gmail_report
                if not gmail_report.is_available():
                    return (
                        "❌ Gmail ยังไม่พร้อม\n"
                        "ต้องตั้ง GMAIL_ENABLED=true\n"
                        "GMAIL_SENDER, GMAIL_RECIPIENT\n"
                        "และ SMTP_HOST/SMTP_USER/SMTP_PASS บน Render"
                    )
                _total = len(doc_index._docs) if doc_index else 0
                _cats: dict = {}
                _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                _new = 0
                for _d in (doc_index._docs if doc_index else []):
                    _c = _d.get("category", "other")
                    _cats[_c] = _cats.get(_c, 0) + 1
                    if _d.get("upload_date", "").startswith(_today):
                        _new += 1
                _stats = {
                    "date": _today,
                    "total_docs": _total,
                    "new_today": _new,
                    "by_category": _cats,
                    "alerts": 0,
                }
                _result = gmail_report.send_daily_summary(_stats)
                if _result.get("error"):
                    return f"❌ {_result['error']}"
                return (
                    f"✅ ส่งรายงานสรุปทาง Email สำเร็จ\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📧 Method: {_result.get('method', 'N/A')}\n"
                    f"📄 เอกสารทั้งหมด: {_total}\n"
                    f"📋 เอกสารใหม่วันนี้: {_new}"
                )
            except Exception as _e:
                return f"❌ ส่ง email ไม่สำเร็จ: {type(_e).__name__}"

    # ── V4.0: Approval Workflow Commands (Admin/Physician) ────
    # "อนุมัติ APR-xxx", "ปฏิเสธ APR-xxx", "รออนุมัติ"
    if text_stripped.startswith("อนุมัติ APR-") or text_stripped.startswith("อนุมัติAPR-"):
        if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
            return None
        _apr_id = text_stripped.replace("อนุมัติ ", "").replace("อนุมัติ", "").strip()
        # อ่านคำขอก่อน approve — หลัง approve จะถูกย้ายออกจาก memory
        _req = approval_engine.get_request(_apr_id) or {}
        _ok, _msg = approval_engine.approve(_apr_id, approver_hash=user_hash)
        if not _ok:
            return f"❌ {_msg}"
        # ── ดำเนินการตามคำขอที่อนุมัติ ──
        # ซ่อนเอกสารซ้ำ (logical delete — ไฟล์ต้นฉบับใน GCS ไม่ถูกลบ)
        _hide_ids = (_req.get("metadata") or {}).get("hide_doc_ids") or []
        if _req.get("action_type") == "delete_document" and _hide_ids:
            _done, _fail = 0, 0
            for _hid in _hide_ids:
                if doc_index.set_hidden(_hid, True, reason=f"duplicate ({_apr_id})"):
                    _done += 1
                else:
                    _fail += 1
            audit_logger.log("documents_hidden", user_hash, {
                "approval_id": _apr_id, "hidden": _done, "failed": _fail,
            })
            return (
                f"✅ อนุมัติ {_apr_id} สำเร็จ\n"
                f"🙈 ซ่อนเอกสารซ้ำแล้ว {_done} รายการ"
                + (f" (ล้มเหลว {_fail})" if _fail else "")
                + "\nไฟล์ต้นฉบับยังอยู่ใน GCS — กู้คืนได้ด้วย 'เลิกซ่อน <doc_id>'"
            )
        return f"✅ อนุมัติ {_apr_id} สำเร็จ"

    # ── เอกสารซ้ำในแฟ้มผู้ป่วย: "ตรวจซ้ำ HN 1234" / "ซ่อนซ้ำ HN 1234" / "เลิกซ่อน <doc_id>" ──
    _dup_m = re.match(r"^(ตรวจซ้ำ|ซ่อนซ้ำ)\s*(?:HN|hn)?\s*(\d{3,10})$", text_stripped)
    if _dup_m:
        if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
            return None
        _dup_cmd, _dup_hn = _dup_m.group(1), _dup_m.group(2)
        _groups = doc_index.find_duplicates_in_folder(_dup_hn)
        if not _groups:
            return f"✅ HN {_dup_hn}: ไม่พบเอกสารซ้ำ"
        _lines = [f"🔍 HN {_dup_hn}: พบเอกสารซ้ำ {len(_groups)} กลุ่ม\n"]
        _to_hide: list[str] = []
        for _gi, _g in enumerate(_groups, 1):
            _keep, _dups = _g[0], _g[1:]
            _lines.append(
                f"กลุ่ม {_gi}: {_CAT_NAMES.get(_keep.get('category', ''), '')} "
                f"{_keep.get('upload_date', '')}"
            )
            _lines.append(f"  ✅ เก็บ {_keep.get('document_id', '')[:8]}")
            for _d in _dups:
                _lines.append(f"  🗑 ซ้ำ  {_d.get('document_id', '')[:8]} ({_d.get('upload_date', '')})")
                _to_hide.append(_d.get("document_id", ""))
        if _dup_cmd == "ตรวจซ้ำ":
            _lines.append(f"\n💡 พิมพ์ 'ซ่อนซ้ำ HN {_dup_hn}' เพื่อขออนุมัติซ่อน {len(_to_hide)} รายการ")
            return "\n".join(_lines)
        _req = approval_engine.request_approval(
            action_type="delete_document",
            requester_hash=user_hash,
            description=f"ซ่อนเอกสารซ้ำ {len(_to_hide)} รายการ ในแฟ้ม HN {_dup_hn}",
            metadata={"hide_doc_ids": _to_hide, "hn": _dup_hn},
        )
        audit_logger.log("duplicate_hide_requested", user_hash, {
            "hn": _dup_hn, "count": len(_to_hide), "approval_id": _req.approval_id,
        })
        _lines.append(
            f"\n📋 สร้างคำขอ {_req.approval_id} แล้ว (หมดอายุ 60 นาที)\n"
            f"พิมพ์ 'อนุมัติ {_req.approval_id}' เพื่อซ่อน {len(_to_hide)} รายการ\n"
            "(ซ่อนจากทุกหน้าจอ — ไฟล์ต้นฉบับใน GCS ไม่ถูกลบ)"
        )
        return "\n".join(_lines)

    _unhide_m = re.match(r"^เลิกซ่อน\s+([A-Za-z0-9-]{8,})$", text_stripped)
    if _unhide_m:
        if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
            return None
        _uid = _unhide_m.group(1)
        _full = next(
            (k for k in doc_index._doc_map if k.startswith(_uid)), None
        )
        if not _full:
            return f"❌ ไม่พบเอกสาร {_uid}"
        if doc_index.set_hidden(_full, False):
            audit_logger.log("document_unhidden", user_hash, {"doc": _full[:8]})
            return f"✅ เลิกซ่อน {_full[:8]} แล้ว — กลับมาแสดงตามปกติ"
        return f"❌ เลิกซ่อน {_uid} ไม่สำเร็จ"

    if text_stripped.startswith("ปฏิเสธ APR-") or text_stripped.startswith("ปฏิเสธAPR-"):
        if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
            return None
        _apr_id = text_stripped.replace("ปฏิเสธ ", "").replace("ปฏิเสธ", "").strip()
        _reason = ""
        if " " in _apr_id:
            _apr_id, _reason = _apr_id.split(" ", 1)
        _ok, _msg = approval_engine.reject(_apr_id, approver_hash=user_hash, reason=_reason)
        if _ok:
            return f"❌ ปฏิเสธ {_apr_id} แล้ว"
        return f"⚠️ {_msg}"

    if text_stripped in ("รออนุมัติ", "pending approvals", "คำขออนุมัติ"):
        if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
            return None
        _pending = approval_engine.get_pending()
        if not _pending:
            return "✅ ไม่มีคำขอที่รออนุมัติ"
        _lines = ["📋 คำขอที่รออนุมัติ\n━━━━━━━━━━━━━━━━━━━━"]
        for _p in _pending[:10]:
            _lines.append(
                f"\n🔔 {_p['approval_id']}\n"
                f"   {_p.get('description', '')}\n"
                f"   สร้าง: {_p.get('created_at', '')[:16]}\n"
                f"   พิมพ์: อนุมัติ {_p['approval_id']}"
            )
        return "".join(_lines)

    # ── ยืนยันตัวตน (One-Time Enrollment Token) ──────────────
    # คำสั่ง: "ยืนยัน [TOKEN]"
    # ผู้ดูแลได้รับ Token จาก POST /admin/enroll (ต้องยืนยัน ADMIN_SECRET)
    # แล้วพิมพ์ Token ผ่าน LINE — ระบบตรวจสอบและคืน hash ของ LINE userId
    # ─ Token ใช้ได้ครั้งเดียว อายุ 5 นาที
    # ─ ไม่แสดง LINE userId จริง
    # ─ ไม่บันทึก Token หรือ userId ลง log
    # ─ ป้องกัน replay attack และ rate limiting
    # ── นัดหมาย: เจ้าหน้าที่ตั้ง / ผู้ป่วยยืนยัน-เลื่อน ──────────────
    if _apr is not None and appointment_store is not None:
        _is_staff = is_admin_user(user_hash) or is_physician_user(user_hash)

        _pa = _apr.parse_patient_action(text_stripped)
        if _pa is not None:
            _act, _aid, _anote = _pa
            _rec = appointment_store.get(_aid)
            if _rec is None:
                return "❌ ไม่พบนัดหมายรหัสนี้"
            # ผู้ป่วยทำได้เฉพาะนัดของ HN ที่ผูกกับ LINE ตัวเอง
            if not _is_staff:
                _mine = hn_registry.get_by_owner(user_hash)
                if not _mine or (_mine.clinic_hn or "").upper() != _rec.get("clinic_hn", ""):
                    audit_logger.log("appointment_action_denied", user_hash, {"id": _aid})
                    return "❌ ไม่พบนัดหมายรหัสนี้"
            _new_status = {"confirm": "confirmed", "reschedule": "reschedule_requested",
                           "cancel": "cancelled"}[_act]
            if _act == "cancel" and not _is_staff:
                _new_status = "reschedule_requested"
                _anote = _anote or "ผู้ป่วยขอยกเลิก"
            _upd = appointment_store.set_status(_aid, _new_status, by=user_hash, note=_anote)
            if _upd is None:
                return "❌ บันทึกไม่สำเร็จ กรุณาลองใหม่"
            audit_logger.log("appointment_status", user_hash, {"id": _aid, "status": _new_status})
            _nm = _patient_name_for_hn(_upd["clinic_hn"])
            if _act == "confirm":
                # แจ้งเจ้าหน้าที่ว่าผู้ป่วยยืนยันแล้ว (ไม่แจ้งเมื่อเจ้าหน้าที่ยืนยันเอง)
                if not _is_staff:
                    for _sid in _staff_line_ids():
                        push_message(
                            _sid,
                            f"✅ ผู้ป่วยยืนยันนัดแล้ว\n👤 HN {_upd['clinic_hn']} {_nm}\n"
                            f"{_apr.appointment_line(_upd)}",
                        )
                return f"✅ ยืนยันนัดแล้ว\n{_apr.appointment_line(_upd)}"
            if _new_status == "cancelled":
                return f"🗑 ยกเลิกนัดแล้ว\n{_apr.appointment_line(_upd)}"
            # ขอเลื่อน → แจ้งเจ้าหน้าที่
            for _sid in _staff_line_ids():
                push_message(
                    _sid,
                    f"📣 ผู้ป่วยขอเลื่อนนัด\n👤 HN {_upd['clinic_hn']} {_nm}\n"
                    f"{_apr.appointment_line(_upd)}"
                    + (f"\n💬 {_anote}" if _anote else "")
                    + f"\n\nตั้งนัดใหม่: นัด {_upd['clinic_hn']} {_apr.APPT_TYPES[_upd['type']]} <วันที่>",
                )
            return "📨 แจ้งเจ้าหน้าที่แล้ว ทางคลินิกจะติดต่อกลับเพื่อนัดวันใหม่"

        _view = _apr.parse_view(text_stripped)
        if _view is not None:
            if _view and not _is_staff:
                return None
            if not _view:
                _mine = hn_registry.get_by_owner(user_hash)
                _view = (_mine.clinic_hn if _mine else "") or ""
                if not _view:
                    return "ยังไม่ได้ผูก HN กับ LINE นี้ — ขอรหัสผูกบัญชีจากเจ้าหน้าที่ แล้วพิมพ์: ผูก <รหัส>"
            _rows = appointment_store.list_for_hn(_view)
            _rule = appointment_store.get_rule(_view) or {}
            _lines = [f"📅 นัดหมาย HN {_view}" + (f" {_patient_name_for_hn(_view)}" if _is_staff else "")]
            _lines += [_apr.appointment_line(r) for r in _rows] or ["(ไม่มีนัดที่กำลังจะถึง)"]
            if _rule.get("rx_next"):
                _lines.append(f"🔁 รับยาทุก {_rule.get('rx_every_days', 30)} วัน — ครั้งถัดไป {_rule['rx_next']}")
            if _rule.get("lab_next"):
                _lines.append(f"🔁 เจาะเลือดทุก {_rule.get('lab_every_months', 3)} เดือน — ครั้งถัดไป {_rule['lab_next']}")
            return "\n".join(_lines)

        if _is_staff:
            _rule_cmd = _apr.parse_staff_rule(text_stripped)
            if _rule_cmd is not None:
                if not (master_registry and master_registry.match_hn(_rule_cmd["hn"])):
                    return f"❌ ไม่พบ HN {_rule_cmd['hn']} ในทะเบียนผู้ป่วย"
                _k = _rule_cmd["type"]
                _upd = {f"{_k}_next": _rule_cmd["start"].isoformat(), f"{_k}_time": _rule_cmd["time"]}
                if _k == "rx":
                    _upd["rx_every_days"] = _rule_cmd["every"] * (30 if _rule_cmd["unit"] == "เดือน" else 1)
                else:
                    _upd["lab_every_months"] = _rule_cmd["every"] if _rule_cmd["unit"] == "เดือน" else max(1, _rule_cmd["every"] // 30)
                if appointment_store.set_rule(_rule_cmd["hn"], _upd, by=user_hash) is None:
                    return "❌ บันทึกนัดประจำไม่สำเร็จ"
                audit_logger.log("appointment_rule_set", user_hash, {"hn": _rule_cmd["hn"], "type": _k})
                _linked = "✅ ผู้ป่วยผูก LINE แล้ว จะได้รับแจ้งเตือน" if _patient_line_id_for_hn(_rule_cmd["hn"]) else "⚠️ ผู้ป่วยยังไม่ผูก LINE — จะไม่ได้รับแจ้งเตือนจนกว่าจะผูก (ออกรหัส <HN>)"
                return (
                    f"🔁 ตั้งนัดประจำแล้ว\n👤 HN {_rule_cmd['hn']} {_patient_name_for_hn(_rule_cmd['hn'])}\n"
                    f"{_apr.APPT_TYPES[_k]} ทุก {_rule_cmd['every']} {_rule_cmd['unit']} เริ่ม {_apr.fmt_thai_date(_rule_cmd['start'])}\n"
                    f"{_linked}"
                )

            _ap = _apr.parse_staff_appointment(text_stripped)
            if _ap is not None:
                if not (master_registry and master_registry.match_hn(_ap["hn"])):
                    return f"❌ ไม่พบ HN {_ap['hn']} ในทะเบียนผู้ป่วย"
                if _ap["date"] < _apr.today_bkk():
                    return "❌ วันที่นัดผ่านมาแล้ว"
                _rec = appointment_store.create(
                    _ap["hn"], _ap["type"], _ap["date"], _ap["time"], _ap["note"], user_hash,
                )
                if _rec is None:
                    return "❌ บันทึกนัดไม่สำเร็จ กรุณาลองใหม่"
                audit_logger.log("appointment_created", user_hash, {"id": _rec["id"], "type": _ap["type"]})
                _lid = _patient_line_id_for_hn(_ap["hn"])
                _notice = "✅ ส่งแจ้งผู้ป่วยทาง LINE แล้ว" if _lid and push_message(
                    _lid, _apr.reminder_text(_rec, _patient_name_for_hn(_ap["hn"]), "new").replace("🔔 แจ้งเตือนนัด", "📅 นัดใหม่: ").replace(" วันนี้", "")
                ) else "⚠️ ผู้ป่วยยังไม่ผูก LINE — จะไม่ได้รับแจ้งเตือน (ออกรหัส <HN> ให้ผู้ป่วยก่อน)"
                return (
                    f"📅 ตั้งนัดแล้ว\n👤 HN {_ap['hn']} {_patient_name_for_hn(_ap['hn'])}\n"
                    f"{_apr.appointment_line(_rec)}\n{_notice}\n"
                    "ระบบจะเตือนผู้ป่วยล่วงหน้า 1 วัน และเช้าวันนัด (08:00)"
                )

    # ── รหัสผูกบัญชีผู้ป่วย ──────────────────────────────────
    _link_code = parse_link_command(text_stripped)
    if _link_code is not None and link_code_store is not None:
        status, _hn = link_code_store.check(_link_code, user_hash)
        if status == "RATE_LIMITED":
            audit_logger.log("link_code_rate_limited", user_hash, {})
            return "⚠️ พิมพ์รหัสผิดหลายครั้ง กรุณารอ 1 ชั่วโมงแล้วลองใหม่"
        if status == "EXPIRED":
            audit_logger.log("link_code_expired", user_hash, {})
            return "⏰ รหัสนี้หมดอายุแล้ว กรุณาขอรหัสใหม่จากเจ้าหน้าที่"
        if status == "USED":
            audit_logger.log("link_code_reused", user_hash, {})
            return "❌ รหัสนี้ถูกใช้ไปแล้ว กรุณาขอรหัสใหม่จากเจ้าหน้าที่"
        if status != "VALID":
            audit_logger.log("link_code_invalid", user_hash, {})
            return "❌ รหัสไม่ถูกต้อง กรุณาตรวจสอบกับเจ้าหน้าที่"
        _pt = master_registry.match_hn(_hn) if master_registry else None
        _clinic_hn = _pt.clinic_hn if _pt and _pt.clinic_hn else _hn
        _hosp_hn = _pt.hospital_hn if _pt else ""
        # รหัสออกโดยเจ้าหน้าที่ที่ยืนยันตัวผู้ป่วยแล้ว → ผูกทับการผูกเดิมได้
        # (ผู้ป่วยเปลี่ยนเครื่อง / HN เคยถูกผูกกับบัญชีทดสอบหรือผิดคน)
        _prev_owner = None
        try:
            _prev = hn_registry.get_by_clinic_hn(_clinic_hn)
            _prev_owner = _prev.owner_hash if _prev else None
        except Exception:
            pass
        ok, msg = hn_registry.register(
            owner_hash=user_hash, clinic_hn=_clinic_hn,
            hospital_hn=_hosp_hn, registered_by="link_code", force=True,
        )
        if ok:
            # ทำเครื่องหมายรหัสใช้แล้วเฉพาะเมื่อผูกสำเร็จ — ล้มเหลว = รหัสยังใช้ได้
            link_code_store.mark_used(_link_code, user_hash)
        audit_logger.log("link_code_consumed", user_hash, {
            "success": ok, "hn": _clinic_hn,
            "relinked_from_other": bool(_prev_owner and _prev_owner != user_hash),
        })
        if ok and line_user_id:
            # เก็บ LINE userId ไว้ส่งแจ้งเตือนนัด/ค่าวิกฤตถึงผู้ป่วยคนนี้
            try:
                from alert_delivery import store_enrolled_line_id
                store_enrolled_line_id(storage, user_hash, line_user_id)
            except Exception as _e:
                logger.warning("เก็บ LINE ID ผู้ป่วยไม่สำเร็จ: %s", type(_e).__name__)
        if not ok:
            return (
                f"❌ ผูกบัญชีไม่สำเร็จ: {msg}\n"
                "กรุณาติดต่อเจ้าหน้าที่คลินิก"
            )
        _name = (_pt.patient_name if _pt else "") or ""
        return (
            "✅ ผูกบัญชีสำเร็จ\n"
            f"👤 {_name}\n" if _name else "✅ ผูกบัญชีสำเร็จ\n"
        ) + (
            f"🏥 HN คลินิก: {_clinic_hn}\n\n"
            "กดเมนู \"ดูผลแล็บ\" เพื่อดูเอกสารของคุณ\n"
            "(เห็นเฉพาะของตัวเองเท่านั้น)"
        )

    # ── myid: แสดง LINE User ID + auto-register HD Alert Recipient ──
    # ทุกคนพิมพ์ได้ (แสดงแค่ ID ตัวเอง) แต่ auto-register alert เฉพาะ staff
    if text_stripped in ("myid", "my id", "userid", "ไอดี"):
        if not line_user_id:
            return "❌ ไม่สามารถดึง User ID ได้"
        # Auto-register as HD alert recipient (in-memory)
        _auto_reg_msg = ""
        if hd_alert_push is not None:
            _already = any(r["line_user_id"] == line_user_id
                           for r in hd_alert_push._recipients)
            if not _already:
                _role = "physician" if is_physician_user(user_hash) else (
                    "admin" if is_admin_user(user_hash) else "nurse")
                hd_alert_push._recipients.append({
                    "line_user_id": line_user_id,
                    "role": _role,
                    "name": "auto-registered",
                })
                _auto_reg_msg = f"\n✅ ลงทะเบียนรับแจ้งเตือน HD ({_role}) แล้ว"
            else:
                _auto_reg_msg = "\n✅ คุณลงทะเบียนรับแจ้งเตือน HD แล้ว"
        # Auto-save to DB hd_settings (persistent across restarts)
        _db_save_msg = ""
        _db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        if _db_url and _is_staff:
            try:
                import psycopg2 as _pg
                _conn = _pg.connect(_db_url, connect_timeout=5)
                with _conn.cursor() as _cur:
                    # Ensure settings table exists
                    _cur.execute("""
                        CREATE TABLE IF NOT EXISTS hd_settings (
                            key VARCHAR(100) PRIMARY KEY,
                            value TEXT NOT NULL DEFAULT '',
                            description TEXT,
                            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                            updated_by INTEGER
                        )
                    """)
                    # Save alert_recipients
                    _cur.execute("""
                        INSERT INTO hd_settings (key, value, updated_at)
                        VALUES ('alert_recipients', %s, NOW())
                        ON CONFLICT (key) DO UPDATE SET
                            value = CASE
                                WHEN hd_settings.value = '' OR hd_settings.value IS NULL
                                THEN EXCLUDED.value
                                WHEN hd_settings.value LIKE %s THEN hd_settings.value
                                ELSE hd_settings.value || ',' || EXCLUDED.value
                            END,
                            updated_at = NOW()
                    """, (line_user_id, f"%{line_user_id}%"))
                    # Also save physician_line_id if physician
                    if is_physician_user(user_hash) or is_admin_user(user_hash):
                        _cur.execute("""
                            INSERT INTO hd_settings (key, value, updated_at)
                            VALUES ('physician_line_id', %s, NOW())
                            ON CONFLICT (key) DO UPDATE SET
                                value = EXCLUDED.value, updated_at = NOW()
                        """, (line_user_id,))
                _conn.commit()
                _conn.close()
                _db_save_msg = "\n💾 บันทึกลงฐานข้อมูลแล้ว (ถาวร)"
            except Exception as _db_err:
                logger.warning("myid DB save failed: %s", _db_err)
        return (
            f"🆔 LINE User ID:\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{line_user_id}\n"
            f"{_auto_reg_msg}"
            f"{_db_save_msg}"
        )

    # ── ตรวจสอบระบบ (Admin/Staff) — เพิ่ม 2026-11-05 ──
    if text_stripped in ("ตรวจสอบระบบ", "สถานะระบบ", "system status"):
        if not _is_staff:
            return None
        _bkk_tz = BKK_TZ
        _now_bkk = datetime.now(_bkk_tz)
        _doc_total = len(doc_index._docs) if doc_index else 0
        _pt_total = master_registry.count() if master_registry else 0
        _gcs_ok = bool(storage and hasattr(storage, "_bucket") and storage._bucket)
        _redis_st = state_store.status if state_store else "N/A"
        _db_st = db.status if db else "N/A"

        # นับเอกสารวันนี้
        _today_str = _now_bkk.strftime("%Y-%m-%d")
        _today_docs = sum(
            1 for _d in (doc_index._docs if doc_index else [])
            if _d.get("upload_date", "").startswith(_today_str)
        )

        # ค่าวิกฤตค้าง
        _pending_alerts = 0
        if alert_service:
            try:
                _pa = alert_service.list_pending()
                _pending_alerts = len(_pa) if _pa else 0
            except Exception:
                pass

        # Dialysis summary
        _dial_summ = master_registry.get_dialysis_summary() if master_registry else {}
        _with_sched = _dial_summ.get("with_schedule", 0)

        # HD Module status
        _hd_enabled = os.environ.get("HD_MODULE_ENABLED", "false").lower() == "true"
        _hd_db_url = bool(os.environ.get("HD_DATABASE_URL", ""))

        # Dialysis detail: count patients with dialysis_schedule text
        _with_sched_text = 0
        if master_registry:
            _with_sched_text = sum(
                1 for p in master_registry.list_all()
                if getattr(p, 'dialysis_schedule', '')
            )

        _status_msg = (
            f"🖥️ สถานะระบบ RANONG RAKTAI AI\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now_bkk.strftime('%d/%m/%Y %H:%M')}\n"
            f"\n"
            f"📦 Storage: {'✅ GCS' if _gcs_ok else '❌ GCS'}\n"
            f"🗄️ Database: {_db_st}\n"
            f"🔄 Redis: {_redis_st}\n"
            f"🏥 HD Module: {'✅ เปิด' if _hd_enabled else '❌ ปิด'}"
            f"{' (DB URL ✅)' if _hd_db_url else ''}\n"
            f"\n"
            f"👥 ผู้ป่วยทะเบียน: {_pt_total} คน\n"
            f"🏥 มีตารางฟอก: {_with_sched} คน"
            f" (มี schedule text: {_with_sched_text})\n"
            f"⚠️ ข้อมูลไม่ครบ: {_pt_total - _with_sched_text} คน"
            f"{' ← กรุณาเพิ่มตารางฟอก' if _pt_total > _with_sched_text else ''}\n"
            f"📄 เอกสารรวม: {_doc_total} ฉบับ\n"
            f"📄 วันนี้: {_today_docs} ฉบับ\n"
        )
        if _pending_alerts > 0:
            _status_msg += f"\n⚠️ ค่าวิกฤตรอยืนยัน: {_pending_alerts}\n"
        else:
            _status_msg += f"\n✅ ไม่มีค่าวิกฤตค้าง\n"

        return _status_msg

    # ── KPI HD Dashboard (Phase 5) ───────────────────────────
    if text_stripped in ("kpihd", "kpi hd", "kpi ไตเทียม", "สถิติฟอก", "สถิติhd"):
        try:
            from hd_module.kpi_engine import KpiEngine, format_kpi_for_line
            _hd_db = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
            engine = KpiEngine(master_registry=master_registry, db_url=_hd_db)
            report = engine.generate_report(period="monthly", days=30)
            return format_kpi_for_line(report)
        except Exception as _kpi_err:
            logger.error("KPI HD error: %s", _kpi_err)
            return f"❌ ไม่สามารถสร้างรายงาน KPI ได้: {type(_kpi_err).__name__}"

    # ── HA Quality Report (Phase 8) ─────────────────────────
    if text_stripped in ("ha report", "ha", "คุณภาพhd", "คุณภาพ hd",
                          "quality", "ตัวชี้วัด", "ตัวชี้วัดhd"):
        try:
            from hd_module.bi_reporting import BiReportEngine, format_ha_report_for_line
            _hd_db = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
            engine = BiReportEngine(master_registry=master_registry, db_url=_hd_db)
            report = engine.generate_ha_report()
            return format_ha_report_for_line(report)
        except Exception as _ha_err:
            logger.error("HA report error: %s", _ha_err)
            return f"❌ ไม่สามารถสร้างรายงาน HA ได้: {type(_ha_err).__name__}"

    # ── รายงาน HD ประจำเดือน (Phase 7.2) ───────────────────
    if text_stripped in ("รายงานhd", "รายงาน hd", "hd report", "สรุปเดือน",
                          "สรุปเดือนhd", "monthly report"):
        try:
            from hd_module.monthly_report import generate_monthly_report
            _hd_db = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
            report = generate_monthly_report(master_registry, _hd_db)
            return report.get("text_th", "❌ สร้างรายงานไม่สำเร็จ")
        except Exception as _mr_err:
            logger.error("Monthly report error: %s", _mr_err)
            return f"❌ ไม่สามารถสร้างรายงานได้: {type(_mr_err).__name__}"

    # ── โภชนาการ HN (Phase 6.3) ────────────────────────────
    if text_stripped.startswith(("โภชนาการ ", "อาหาร ", "nutrition ")):
        _nutr_q = text_stripped
        for _pf in ("โภชนาการ ", "อาหาร ", "nutrition "):
            if _nutr_q.startswith(_pf):
                _nutr_q = _nutr_q[len(_pf):].strip()
                break
        if not _nutr_q:
            return "⚠️ กรุณาระบุ HN เช่น: โภชนาการ 56001"
        _np = master_registry.match_hn(_nutr_q) if master_registry else None
        if not _np:
            return f"❌ ไม่พบ HN: {_nutr_q}"
        try:
            from hd_module.predictive_analytics import NutritionFluidAdvisor
            # ดึงผลแล็บล่าสุด
            _n_docs = _get_patient_lab_docs(_np, _nutr_q, limit=1)
            _k, _po4, _ca, _alb = None, None, None, None
            if _n_docs:
                _n_labs = _extract_latest_labs(
                    _n_docs[-1].get("extracted_text", ""))
                _k = _n_labs.get("k") or _n_labs.get("potassium")
                _po4 = _n_labs.get("po4") or _n_labs.get("phosphate")
                _ca = _n_labs.get("ca") or _n_labs.get("calcium")
                _alb = _n_labs.get("albumin")
            advisor = NutritionFluidAdvisor()
            result = advisor.advise(
                potassium=_k, phosphate=_po4, calcium=_ca,
                albumin=_alb, patient_name=_np.patient_name,
            )
            return result.overall_th
        except Exception as _ne:
            logger.error("Nutrition advisor error: %s", _ne)
            return f"❌ ไม่สามารถวิเคราะห์ได้: {type(_ne).__name__}"

    if text_stripped in ("ตรวจแฟ้มย้อนหลัง", "จัดแฟ้มย้อนหลัง"):
        if not is_admin_user(user_hash):
            return None
        _dry = text_stripped == "ตรวจแฟ้มย้อนหลัง"
        _r = _retroactive_route_run(dry_run=_dry, cat_filter="all")
        if _r.get("error"):
            return f"❌ {_r['error']}"
        audit_logger.log("retroactive_route", user_hash, {"dry_run": _dry, "routed": _r.get("routed_count", 0)})
        _skips = {}
        for _e in _r.get("skipped", []) + [e for e in _r.get("routed", []) if e.get("routed_to") == "quarter_month_only"]:
            _k = _e.get("hn_note") or _e.get("reason") or "อื่น ๆ"
            _skips[_k] = _skips.get(_k, 0) + 1
        _lines = [
            ("🔍 ตรวจ (ยังไม่ย้าย)" if _dry else "📂 จัดแฟ้มย้อนหลังแล้ว"),
            f"อยู่ในแฟ้มผู้ป่วยแล้ว: {_r.get('already_in_folder', 0)}",
            f"{'จะย้าย' if _dry else 'ย้าย'}เข้าแฟ้มผู้ป่วย: {_r.get('routed_to_patient_folder', 0)}",
            f"หา HN ไม่ได้ (คงอยู่ระดับเดือน): {_r.get('routed_quarter_month_only', 0) + _r.get('skipped_count', 0)}",
            f"ผิดพลาด: {_r.get('error_count', 0)}",
        ]
        if _skips:
            _lines.append("เหตุผล: " + ", ".join(f"{k} {v}" for k, v in _skips.items()))
        if _dry:
            _lines.append("\nพิมพ์ 'จัดแฟ้มย้อนหลัง' เพื่อย้ายจริง (ไฟล์เดิมไม่ถูกลบ)")
        else:
            _lines.append("\nที่หา HN ไม่ได้ ใช้ 'ย้ายเอกสาร <รหัส> <HN>' ทีละใบ")
        return "\n".join(_lines)

    if text_stripped in ("ซ่อมดัชนีแฟ้ม", "ซ่อมดัชนี", "reconcile index"):
        if not is_admin_user(user_hash):
            return None
        _r = _reconcile_patient_folder_index()
        audit_logger.log("index_reconcile", user_hash, _r)
        return (
            "🛠 ซ่อมดัชนีแฟ้มผู้ป่วยแล้ว\n"
            f"ไฟล์ในแฟ้มผู้ป่วย: {_r.get('scanned_files', 0)}\n"
            f"แก้ index: {_r.get('fixed', 0)} · ถูกอยู่แล้ว: {_r.get('already_ok', 0)}\n"
            f"ไม่พบไฟล์ในแฟ้มผู้ป่วย: {_r.get('no_index', 0)} · ผิดพลาด: {_r.get('errors', 0)}\n"
            "(ผลมีผลทันทีใน LIFF/ค้นหา ภายใน 5 วินาที)"
        )

    if text_stripped in ("ส่งเตือนนัดตอนนี้", "รันเตือนนัด", "run reminders"):
        if not is_admin_user(user_hash):
            return None
        _res = job_manager.run("daily_reminders")
        _ap = (getattr(_res, "details", {}) or {}).get("appointments") or {}
        audit_logger.log("daily_reminders_manual", user_hash, {"success": _res.success})
        return (
            f"{'✅' if _res.success else '❌'} รันแจ้งเตือนนัดแล้ว\n"
            f"ส่งถึงผู้ป่วย: {_ap.get('sent', 0)} · ยังไม่ผูก LINE: {_ap.get('skipped_unlinked', 0)} · "
            f"ส่งไปแล้วก่อนหน้า: {_ap.get('already', 0)} · ผิดพลาด: {_ap.get('errors', 0)}\n"
            f"นัดวันนี้: {_ap.get('today_count', 0)} · แจ้งเจ้าหน้าที่: {_ap.get('staff_notified', 0)}"
        )

    if text_stripped in ("สรุปผลเลือดเดือน", "monthly summary", "ส่งสรุปเดือน"):
        if not is_admin_user(user_hash):
            return None
        try:
            import psycopg2 as _pg2ms
            from hd_module.monthly_summary import run_monthly_push
            from hd_module import get_setting_value as _gsv_ms
            _db_ms = os.getenv("HD_DATABASE_URL", "") or os.getenv("DATABASE_URL", "")
            _conn_ms = _pg2ms.connect(_db_ms, connect_timeout=10)
            try:
                _ms_res = run_monthly_push(
                    _conn_ms, push_message,
                    lambda hn: _hd_patient_line_id(hn),
                    _gsv_ms)
            finally:
                _conn_ms.close()
            return (
                f"📊 ส่งสรุปผลเลือดรายเดือนแล้ว\n"
                f"เดือน: {_ms_res.get('month', '?')}\n"
                f"ผู้ป่วยที่มีผลเลือด: {_ms_res.get('eligible', 0)}\n"
                f"ส่งสำเร็จ: {_ms_res.get('sent', 0)}\n"
                f"ยังไม่ผูก LINE: {_ms_res.get('skipped_no_line', 0)}\n"
                f"ผิดพลาด: {_ms_res.get('errors', 0)}"
            )
        except Exception as _ms_e:
            return f"❌ สรุปเดือน: {type(_ms_e).__name__}: {_ms_e}"

    _issue_hn = parse_issue_command(text_stripped)
    if _issue_hn is not None:
        if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
            return None  # ไม่เปิดเผยว่ามีคำสั่งนี้
        if link_code_store is None or master_registry is None:
            return "❌ ระบบรหัสผูกบัญชียังไม่พร้อม"
        _pt = master_registry.match_hn(_issue_hn)
        if _pt is None:
            return (
                f"❌ ไม่พบ HN {_issue_hn} ในทะเบียนผู้ป่วย\n"
                "ลงทะเบียนรายชื่อก่อน (พิมพ์ 'ลงทะเบียนรายชื่อ')"
            )
        ok, code_or_err = link_code_store.issue(
            _pt.clinic_hn or _issue_hn, user_hash,
        )
        audit_logger.log("link_code_issued", user_hash, {
            "success": ok, "hn": _pt.clinic_hn or _issue_hn,
        })
        if not ok:
            return f"❌ {code_or_err}"
        return (
            "🔐 รหัสผูกบัญชีผู้ป่วย\n"
            f"👤 {_pt.patient_name or '-'}\n"
            f"🏥 HN: {_pt.clinic_hn or _issue_hn}\n\n"
            f"รหัส: {code_or_err}\n\n"
            "ให้ผู้ป่วยเพิ่มเพื่อน LINE OA แล้วพิมพ์:\n"
            f"ผูก {code_or_err}\n\n"
            "⏰ ใช้ได้ครั้งเดียว ภายใน 48 ชั่วโมง\n"
            f"ยกเลิก: ยกเลิกรหัส {code_or_err}"
        )

    _revoke_code = parse_revoke_command(text_stripped)
    if _revoke_code is not None:
        if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
            return None
        if link_code_store is not None and link_code_store.revoke(_revoke_code, user_hash):
            audit_logger.log("link_code_revoked", user_hash, {})
            return f"✅ ยกเลิกรหัส {_revoke_code} แล้ว"
        return "❌ ยกเลิกไม่สำเร็จ (รหัสไม่ถูกต้องหรือถูกใช้/ยกเลิกแล้ว)"

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

    # ช่วยเหลือ / help / เมนู → Flex Carousel
    if parsed.command_type == CommandType.HELP:
        def _hb(title, icon, color, items):
            body = [{"type": "box", "layout": "horizontal", "spacing": "sm",
                     "contents": [
                         {"type": "text", "text": "•", "size": "sm", "color": "#888888", "flex": 0},
                         {"type": "text", "text": it, "size": "sm", "color": "#555555", "wrap": True, "flex": 1},
                     ]} for it in items]
            return {"type": "bubble", "size": "micro",
                    "header": {"type": "box", "layout": "vertical", "backgroundColor": color,
                               "paddingAll": "12px", "contents": [
                                   {"type": "text", "text": icon, "size": "xl", "align": "center"},
                                   {"type": "text", "text": title, "size": "sm", "color": "#ffffff",
                                    "weight": "bold", "align": "center", "margin": "xs"}]},
                    "body": {"type": "box", "layout": "vertical", "spacing": "xs",
                             "paddingAll": "10px", "contents": body}}
        _qr_items = [{"type": "action", "action": {"type": "message", "label": l[:20], "text": t}}
                     for l, t in [("🩸 ผลเลือดของฉัน", "ผลเลือด"), ("🔬 ผลแล็บ", "ผลแล็บ"),
                                  ("🩺 รอบฟอก", "รอบฟอก"), ("📅 ดูนัด", "ดูนัด"),
                                  ("📊 สถิติ", "สถิติเดือนนี้")]]
        return {
            "type": "flex", "altText": "📋 คำสั่งทั้งหมด — เลื่อนดูแต่ละหมวด",
            "quickReply": {"items": _qr_items},
            "contents": {"type": "carousel", "contents": [
                _hb("ผลแล็บ", "🔬", "#2E8B57", ["ผลแล็บ — ผลล่าสุด", "วิเคราะห์ [HN]",
                    "แนวโน้มแล็บ", "เปรียบเทียบแล็บ", "รายงานแล็บ"]),
                _hb("ไตเทียม", "🩺", "#1E88E5", ["รอบฟอก — วันนี้", "ข้อมูลฟอก [HN]",
                    "สรุปฟอก", "ติดตามHb [HN]", "ติดตามeGFR [HN]", "เปรียบเทียบ [HN]"]),
                _hb("การรักษา", "💊", "#6735B7", ["ปรับยา [HN] [รายละเอียด]",
                    "คำสั่งการรักษา [HN]", "ประวัติการรักษา [HN]", "ติดตามผลการรักษา", "ตรวจยา [HN]"]),
                _hb("เอกสาร", "📂", "#E65100", ["ค้นหา [คำ]", "ขอไฟล์ [รหัส]",
                    "รายการล่าสุด", "ส่งเอกสาร — ส่งรูป/PDF"]),
                _hb("ผู้ป่วย", "👤", "#C62828", ["ผลเลือด — ค่าเลือด+กราฟของฉัน", "สรุป HN [เลข]",
                    "HN ของฉัน", "ลงทะเบียน", "ดูนัด / นัดหมาย"]),
                _hb("ระบบ", "📊", "#00796B", ["สถิติเดือนนี้", "ตรวจสอบระบบ",
                    "KPI / KPIไต", "สรุปค่าใช้จ่าย", "สถานะgoogle"]),
            ]},
        }

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
                # Push LINE ไปเจ้าหน้าที่ทุกคน
                try:
                    _staff_ids = _staff_line_ids()
                    _patient_name = ""
                    if master_registry:
                        _pr = master_registry.find_by_line_hash(user_hash)
                        if _pr:
                            _patient_name = f" ({_pr.get('name', '')} HN {_pr.get('hn', '')})"
                    _notif_text = (
                        f"📢 ผู้ป่วยติดต่อ{_patient_name}\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"เรื่อง: {msg}\n"
                        f"ความเร่งด่วน: {esc.get('urgency', 'ปกติ')}\n"
                        f"เวลา: {esc.get('created_at', '')[:16]}"
                    )
                    for _sid in _staff_ids:
                        try:
                            push_message(_sid, _notif_text)
                        except Exception:
                            pass
                except Exception as _notif_err:
                    logger.warning(f"Staff notification error: {_notif_err}")
                return format_escalation_confirmation(esc)
        except ImportError:
            logger.warning("Feature 6: line_patient_assistant module not available")
            if parsed.command_type == CommandType.HEALTH_ADVICE:
                return "ฟีเจอร์คำแนะนำสุขภาพยังไม่พร้อมใช้งาน"
            return "ฟีเจอร์ติดต่อเจ้าหน้าที่ยังไม่พร้อมใช้งาน"
        except Exception as e:
            logger.warning(f"Feature 6 error: {type(e).__name__}")
            return "ขออภัย ระบบขัดข้องชั่วคราว กรุณาลองใหม่"

    # ── วิธีใช้ (legacy text — kept for reference) ──
    if False:  # Flex Carousel ใช้แทนแล้ว (ดูที่ CommandType.HELP ด้านบน)
        return (
            "📋 คำสั่งที่ใช้ได้ในระบบ RANONG RAKTAI AI\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "📂 ค้นหาเอกสาร\n"
            "• ค้นหา [คำ] — ค้นเอกสาร\n"
            "• ขอไฟล์ [รหัส] — ดาวน์โหลดเอกสาร\n"
            "• รายการล่าสุด — เอกสาร 5 รายการล่าสุด\n"
            "\n"
            "🔬 ผลตรวจแล็บ\n"
            "• ผลแล็บ — ผลแล็บล่าสุด\n"
            "• ผลแล็บ HN [เลข] — ผลแล็บผู้ป่วย\n"
            "• วิเคราะห์แล็บ — วิเคราะห์ผลแล็บตาม KDIGO\n"
            "• แนวโน้มแล็บ — กราฟแนวโน้มค่าแล็บ\n"
            "• เปรียบเทียบแล็บ — เทียบผลแล็บล่าสุด vs ก่อนหน้า\n"
            "• รายงานแล็บ — สรุปผลแล็บทั้งหมด\n"
            "\n"
            "💊 ยาและการรักษา\n"
            "• ตรวจยา — ตรวจสอบรายการยาปัจจุบัน\n"
            "• ตรวจยา HN [เลข] — ตรวจยาผู้ป่วย\n"
            "\n"
            "📊 เปรียบเทียบย้อนหลัง\n"
            "• เปรียบเทียบแล็บย้อนหลัง HN [เลข]\n"
            "• เปรียบเทียบยาย้อนหลัง HN [เลข]\n"
            "• เปรียบเทียบฟอกเลือดย้อนหลัง HN [เลข]\n"
            "\n"
            "📈 KPI และตารางตรวจ\n"
            "• KPI — ดัชนีคุณภาพ\n"
            "• KPIไต — KPI โรคไตเฉพาะทาง\n"
            "• ตารางตรวจ — กำหนดเจาะเลือดครั้งถัดไป\n"
            "\n"
            "👤 ข้อมูลผู้ป่วย\n"
            "• สรุป HN [เลข] — สรุปข้อมูลผู้ป่วย\n"
            "• HN ของฉัน — ดู HN ที่ลงทะเบียน\n"
            "• ยืนยันผลแล็บ [รหัส] — อนุมัติผลแล็บ\n"
            "\n"
            "🩺 ไตเทียม\n"
            "• รอบฟอก — ตารางฟอกวันนี้\n"
            "• ข้อมูลฟอก [HN] — ข้อมูลฟอกรายคน\n"
            "• สรุปฟอก — สรุปภาพรวมไตเทียม\n"
            "• ติดตามHb [HN] — แนวโน้ม Hb กับ ESA\n"
            "• วิเคราะห์ [HN] — AI วิเคราะห์แล็บ+ESA เชิงลึก\n"
            "• ติดตามeGFR [HN] — แนวโน้ม eGFR + CKD progression\n"
            "• เปรียบเทียบ [HN] — เปิดหน้าเปรียบเทียบ\n"
            "\n"
            "💊 การรักษา\n"
            "• ปรับยา [HN] [รายละเอียด] — บันทึกการปรับยา\n"
            "• คำสั่งการรักษา [HN] — ดูคำสั่งที่ส่งจากหน้าเปรียบเทียบ\n"
            "• ประวัติการรักษา [HN] — ดูประวัติการปรับยา\n"
            "• ติดตามผลการรักษา — รายการที่ต้องติดตาม\n"
            "\n"
            "📊 สถิติและระบบ\n"
            "• สถิติเดือนนี้ — สรุปภาพรวมเดือน\n"
            "• ตรวจสอบระบบ — สถานะระบบทั้งหมด\n"
            "• สรุปค่าใช้จ่าย — สรุปค่าใช้จ่ายเดือนนี้\n"
            "• สรุปค่าใช้จ่าย [เดือน/ปี] — สรุปเดือนที่ระบุ\n"
            "\n"
            "🔗 Google Integration (แพทย์/admin)\n"
            "• สถานะgoogle — ดูสถานะเชื่อมต่อ\n"
            "• syncแล็บ — ส่งสรุปแล็บไป Sheets\n"
            "• syncทะเบียน — ส่งทะเบียนผู้ป่วยไป Sheets\n"
            "• backupไดรฟ์ — สำรองเอกสารไป Drive\n"
            "• ส่งรายงานemail — ส่งสรุปทาง email\n"
            "\n"
            "💡 พิมพ์คำสั่งตามตัวอย่างด้านบน\n"
            "หรือส่งเอกสาร (รูป/PDF) เพื่อบันทึกอัตโนมัติ"
        )

    if not is_authorized_user(user_hash):
        if parsed.command_type == CommandType.REGISTER_HN:
            # [SEC-4] ผู้ป่วยห้ามอ้าง HN เอง — ใช้รหัสจากเจ้าหน้าที่แทน
            audit_logger.log("hn_self_register_blocked", user_hash, {})
            return (
                "🔐 การผูก HN ต้องยืนยันตัวตนที่คลินิก\n\n"
                "กรุณาแจ้งเจ้าหน้าที่เพื่อรับรหัสผูกบัญชี 6 หลัก\n"
                "แล้วพิมพ์ในแชทนี้ว่า:\n"
                "ผูก <รหัส 6 หลัก>\n\n"
                "ตัวอย่าง: ผูก 482913"
            )
        if parsed.command_type == CommandType.MY_HN:
            # ผู้ป่วยดู HN ของตัวเองได้ (ข้อมูลของตัวเอง)
            try:
                _me = hn_registry.get_by_owner(user_hash)
            except Exception:
                _me = None
            if _me and getattr(_me, "clinic_hn", ""):
                return f"🏥 HN คลินิกของคุณ: {_me.clinic_hn}"
            return (
                "ยังไม่ได้ผูก HN กับ LINE นี้\n"
                "ขอรหัสผูกบัญชีจากเจ้าหน้าที่ แล้วพิมพ์: ผูก <รหัส>"
            )
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
                or text_stripped in ("KPIไต", "kpiไต", "เป้าหมายไต", "dashboard")
                or text_stripped.startswith("สรุป HN")
                or text_stripped.startswith("สรุปHN")
                or text_stripped.startswith("สรุปค่าใช้จ่าย")):
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
            # ── ตรวจสอบว่า args เป็น HN (ตัวเลข) หรือ document_id ──
            # ถ้าเป็น HN ที่อยู่ใน master_registry → ใช้ Lab Intelligence แทน
            _cli_args = parsed.args.strip()
            _cli_matched_patient = None
            if _cli_args.isdigit() and len(_cli_args) >= 4:
                _cli_matched_patient = master_registry.match_hn(_cli_args)
            if _cli_matched_patient:
                # Redirect ไปที่ Lab Intelligence Agent
                try:
                    from lab_intelligence_agent import (
                        generate_intelligence_report,
                        format_for_line,
                    )
                    _cli_hn = _cli_matched_patient.clinic_hn or _cli_args
                    _hb_hist = _get_hb_history(_cli_hn)
                    _lab_docs = _get_patient_lab_docs(_cli_matched_patient, _cli_hn)
                    _latest = _extract_latest_labs(
                        _lab_docs[-1].get("extracted_text", "") if _lab_docs else ""
                    )

                    _ai_report = generate_intelligence_report(
                        patient_hn=_cli_hn,
                        patient_data=_cli_matched_patient.to_dict(),
                        lab_history=_hb_hist,
                        latest_labs=_latest,
                    )
                    return format_for_line(_ai_report)
                except Exception as _ai_err:
                    logger.warning("Lab Intelligence error (via วิเคราะห์): %s", _ai_err)
                    return "ขออภัย ไม่สามารถวิเคราะห์ได้ในขณะนี้"
            else:
                _analyze_doc_id = _cli_args
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

    # ── เปรียบเทียบผลแล็บ/ยา/ฟอกเลือด ย้อนหลัง (ปัจจุบัน vs 3 เดือน vs 6 เดือน) ──
    # รองรับระบุ HN เฉพาะคน (เฉพาะ admin/แพทย์)
    if parsed.command_type in (
        CommandType.COMPARE_LAB_TIMELINE,
        CommandType.COMPARE_RX_TIMELINE,
        CommandType.COMPARE_DIALYSIS_TIMELINE,
    ):
        # ── Resolve target owner_hash ──
        target_hash = user_hash
        patient_label = ""
        hn_arg = (parsed.args or "").strip()

        if hn_arg:
            # ระบุ HN — ต้องเป็น admin หรือแพทย์เท่านั้น
            if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
                return "⚠️ เฉพาะแพทย์หรือผู้ดูแลระบบเท่านั้นที่สามารถดูข้อมูลผู้ป่วยรายอื่นได้"

            # ลอง resolve HN
            hn_query = hn_arg.replace("HN", "").replace("hn", "").strip()
            patient = hn_registry.get_by_clinic_hn(hn_query)
            if not patient:
                patient = hn_registry.get_by_hospital_hn(hn_query)
            if not patient:
                # ลองค้นหา
                matches = hn_registry.search_hn(hn_query)
                if len(matches) == 1:
                    patient = matches[0]
                elif len(matches) > 1:
                    lines = ["🔍 พบหลายรายการ กรุณาระบุ HN ให้ชัดเจน:"]
                    for m in matches[:5]:
                        name = m.display_name or m.owner_hash[:8]
                        lines.append(f"  • HN {m.clinic_hn or m.hospital_hn or '?'} — {name}")
                    return "\n".join(lines)

            if patient:
                # พบใน hn_registry — ใช้ owner_hash + folder ทั้งคู่
                # (เหมือนกับ สรุป HN ที่ใช้งานได้)
                target_hash = patient.owner_hash
                _hn_check = patient.clinic_hn or patient.hospital_hn or ""
                _patient_folder = ""
                if _hn_check and master_registry:
                    _mr = master_registry.search(_hn_check)
                    if len(_mr) == 1:
                        _patient_folder = _mr[0].folder_name
                if not _patient_folder and _hn_check:
                    _patient_folder = _hn_check
                patient_label = f" (HN {patient.clinic_hn or patient.hospital_hn})"
                if patient.display_name:
                    patient_label = f" ({patient.display_name}, HN {patient.clinic_hn or patient.hospital_hn})"
            else:
                # ── Fallback: ค้นจาก master_registry ──
                master_matches = master_registry.search(hn_query) if master_registry else []
                if len(master_matches) == 1:
                    master_patient = master_matches[0]
                    # ใช้ patient_folder แทน owner_hash
                    target_hash = ""
                    _patient_folder = master_patient.folder_name
                    patient_label = f" ({master_patient.patient_name}, HN {master_patient.clinic_hn or master_patient.hospital_hn})"
                elif len(master_matches) > 1:
                    lines = ["🔍 พบหลายรายการ กรุณาระบุ HN ให้ชัดเจน:"]
                    for mp in master_matches[:5]:
                        lines.append(f"  • HN {mp.clinic_hn or mp.hospital_hn or '?'} — {mp.patient_name}")
                    return "\n".join(lines)
                else:
                    return f"❌ ไม่พบผู้ป่วย HN '{hn_query}' ในระบบ"
        else:
            _patient_folder = ""

        # ── สร้างรายการ folder ที่จะลองค้น ──
        _cmp_folders = [_patient_folder] if _patient_folder else []
        if hn_arg:
            _cmp_hn_q = hn_arg.replace("HN", "").replace(
                "hn", ""
            ).strip()
            if _cmp_hn_q and _cmp_hn_q not in _cmp_folders:
                _cmp_folders.append(_cmp_hn_q)
            _cmp_hn_pf = f"HN{_cmp_hn_q}"
            if _cmp_hn_pf not in _cmp_folders:
                _cmp_folders.append(_cmp_hn_pf)
            # hospital HN
            if patient:
                _cmp_hhn = getattr(patient, "hospital_hn", "") or ""
                if _cmp_hhn:
                    for _cmp_v in [
                        f"HN{_cmp_hhn}", _cmp_hhn,
                    ]:
                        if _cmp_v not in _cmp_folders:
                            _cmp_folders.append(_cmp_v)
            elif len(master_matches) == 1 if 'master_matches' in dir() else False:
                pass  # folder_name already in _cmp_folders

        # ── เรียกฟังก์ชันตามประเภท (ลองหลาย folder) ──
        cmd = parsed.command_type
        try:
            # ลองค้นจาก target_hash ก่อน ถ้าไม่ได้ ลองแต่ละ folder
            if cmd == CommandType.COMPARE_LAB_TIMELINE:
                _cmp_fn = compare_lab_timeline
                log_type = "compare_lab_timeline"
                err_msg = "ขออภัย ไม่สามารถเปรียบเทียบผลแล็บย้อนหลังได้ในขณะนี้"
            elif cmd == CommandType.COMPARE_RX_TIMELINE:
                _cmp_fn = compare_rx_timeline
                log_type = "compare_rx_timeline"
                err_msg = "ขออภัย ไม่สามารถเปรียบเทียบใบยาย้อนหลังได้ในขณะนี้"
            else:
                _cmp_fn = compare_dialysis_timeline
                log_type = "compare_dialysis_timeline"
                err_msg = "ขออภัย ไม่สามารถเปรียบเทียบข้อมูลฟอกเลือดย้อนหลังได้ในขณะนี้"

            # ── ใช้วิธีเดียวกับ "สรุป HN" ──
            # ลอง owner_hash ก่อน แล้ว folder ทีหลัง (เหมือน _try_history)
            _get_fn = (
                get_patient_dialysis_history if cmd == CommandType.COMPARE_DIALYSIS_TIMELINE
                else get_patient_lab_history if cmd == CommandType.COMPARE_LAB_TIMELINE
                else get_patient_rx_history
            )
            _hist = []
            # 1) ลอง owner_hash ก่อน
            if target_hash:
                try:
                    _hist = _get_fn(storage, doc_index, target_hash, patient_folder="")
                except Exception:
                    pass
            # 2) ลองแต่ละ folder
            if not _hist:
                for _cf in _cmp_folders:
                    try:
                        _r = _get_fn(storage, doc_index, "", patient_folder=_cf)
                        if _r:
                            _hist = _r
                            break
                    except Exception:
                        pass

            # สร้าง result จาก history ที่ได้
            if not _hist:
                return "ไม่พบข้อมูลในระบบ กรุณาส่งเอกสารก่อน"

            if len(_hist) < 2:
                cur = _hist[0]
                items = "\n".join(
                    f"  • {_dialysis_param_thai(k)}: {_format_timeline_value(v)}"
                    for k, v in sorted(cur.get("data", {}).items())
                )
                return (
                    f"📋 พบข้อมูล 1 ครั้ง ({cur.get('upload_date', '?')})\n"
                    f"{items}\n\n"
                    "ส่งเอกสารอีก 1 ครั้งเพื่อเปรียบเทียบ"
                )

            # มี ≥ 2 ครั้ง → ส่ง link ไปหน้าเปรียบเทียบรูปจริง
            cur_date = _hist[0].get("upload_date", "?")
            prev_date = _hist[1].get("upload_date", "?")

            _base = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
            _cmp_type = "dialysis"
            _type_label = "ฟอกเลือด"
            if cmd == CommandType.COMPARE_LAB_TIMELINE:
                _cmp_type = "lab"
                _type_label = "ผลแล็บ"
            elif cmd == CommandType.COMPARE_RX_TIMELINE:
                _cmp_type = "rx"
                _type_label = "ใบยา"

            # token ชั่วคราว (1 ชม.) แทน ADMIN_SECRET — ไม่ให้ secret ค้างในแชท LINE
            _cmp_url = (
                f"{_base}/admin/compare/{hn_query}"
                f"?token={_make_compare_token(hn_query)}&type={_cmp_type}"
            )

            _text = (
                f"📋 เปรียบเทียบ{_type_label}{patient_label}\n"
                f"มี {len(_hist)} ครั้ง ({prev_date} — {cur_date})\n\n"
                f"🔍 กดดูรูปเอกสาร + สั่งการรักษา:\n{_cmp_url}"
            )

            result = {"text_th": _text, "current_date": cur_date}

            audit_logger.log(log_type, user_hash, {
                "target": target_hash[:8] if target_hash != user_hash else "self",
                "current": result.get("current_date", ""),
                "date_3m": result.get("date_3m", ""),
                "date_6m": result.get("date_6m", ""),
            })

            text = result.get("text_th", "ไม่สามารถเปรียบเทียบได้")
            if patient_label:
                text = f"👤 ผู้ป่วย{patient_label}\n\n{text}"
            return text

        except Exception as e:
            import traceback
            logger.error(
                "Compare timeline error: %s\n%s",
                e, traceback.format_exc(),
            )
            return f"{err_msg}\n\n🔧 Debug: {type(e).__name__}: {e}"

    # ── ลงทะเบียน HN ──
    if parsed.command_type == CommandType.REGISTER_HN:
        hn_input = parsed.args.strip()
        if not is_authorized_user(user_hash):
            # [SEC-4] ผู้ป่วยห้ามอ้าง HN เอง — ใครพิมพ์ HN ของคนอื่นก่อน
            # จะได้เห็นเวชระเบียนของคนนั้น  ต้องใช้รหัสจากเจ้าหน้าที่แทน
            audit_logger.log("hn_self_register_blocked", user_hash, {})
            return (
                "🔐 การผูก HN ต้องยืนยันตัวตนที่คลินิก\n\n"
                "กรุณาแจ้งเจ้าหน้าที่เพื่อรับรหัสผูกบัญชี 6 หลัก\n"
                "แล้วพิมพ์ในแชทนี้ว่า:\n"
                "ผูก <รหัส 6 หลัก>\n\n"
                "ตัวอย่าง: ผูก 482913"
            )
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

    # ── สถิติเดือนนี้ (Monthly Stats) — เพิ่ม 2026-11-05 ──
    if text_stripped in ("สถิติเดือนนี้", "สถิติเดือน", "monthly stats"):
        _bkk_tz = BKK_TZ
        _now_bkk = datetime.now(_bkk_tz)
        _month_prefix = _now_bkk.strftime("%Y-%m")
        _thai_month = f"{_now_bkk.month}/{_now_bkk.year + 543}"

        # นับเอกสาร
        _total_docs = 0
        _lab_docs = 0
        _rx_docs = 0
        _bill_docs = 0
        _other_docs = 0
        for _d in (doc_index._docs if doc_index else []):
            if _d.get("upload_date", "").startswith(_month_prefix):
                _total_docs += 1
                _cat = _d.get("category", "other")
                if _cat == "lab":
                    _lab_docs += 1
                elif _cat in ("rx", "medication"):
                    _rx_docs += 1
                elif _cat in ("bill", "receipt"):
                    _bill_docs += 1
                else:
                    _other_docs += 1

        # ค่าวิกฤต
        _crit_count = 0
        _crit_ack = 0
        if alert_service is not None:
            try:
                for _a in (alert_service.list_all() or []):
                    if str(getattr(_a, 'created_at', '')).startswith(_month_prefix):
                        _crit_count += 1
                        if getattr(_a, 'acknowledged', False):
                            _crit_ack += 1
            except Exception:
                pass

        # ค่าใช้จ่าย
        _expense_text = ""
        if expense_tracker:
            try:
                _exp = expense_tracker.get_monthly_summary(
                    _now_bkk.year, _now_bkk.month
                )
                _inv = _exp.get("invoice_count", 0)
                _total_cost = _exp.get("total", 0)
                if _inv > 0:
                    _expense_text = f"\n💰 ค่าใช้จ่าย: {_total_cost:,.0f} บาท ({_inv} ใบ)"
            except Exception:
                pass

        # ไตเทียม
        _dialysis_text = ""
        _summ = master_registry.get_dialysis_summary()
        if _summ.get("total_patients", 0) > 0:
            _dialysis_text = (
                f"\n\n🩺 ไตเทียม:\n"
                f"  ทะเบียน: {_summ['total_patients']} คน\n"
                f"  มีตาราง: {_summ['with_schedule']} คน"
            )

        _stats_msg = (
            f"📊 สถิติเดือน {_thai_month}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"\n📄 เอกสารรับเข้า: {_total_docs} รายการ\n"
            f"  🔬 ผลแล็บ: {_lab_docs}\n"
            f"  💊 ใบยา: {_rx_docs}\n"
            f"  🧾 บิล/ใบเสร็จ: {_bill_docs}\n"
            f"  📁 อื่นๆ: {_other_docs}\n"
            f"\n⚠️ ค่าวิกฤต: {_crit_count} ครั้ง"
            f" (ยืนยันแล้ว {_crit_ack})"
            f"{_expense_text}"
            f"{_dialysis_text}"
        )
        return _stats_msg

    # ── สรุปค่าใช้จ่าย (Expense Summary) ──
    if text_stripped.startswith("สรุปค่าใช้จ่าย"):
        if not expense_tracker:
            return "ระบบติดตามค่าใช้จ่ายยังไม่พร้อมใช้งาน"

        _exp_args = text_stripped.replace("สรุปค่าใช้จ่าย", "").strip()
        try:
            if _exp_args:
                # parse month/year: "10/2569", "10/2026", "10 2026"
                import re as _exp_re
                _exp_m = _exp_re.search(r"(\d{1,2})[/\-\s]+(\d{2,4})", _exp_args)
                if _exp_m:
                    _exp_month = int(_exp_m.group(1))
                    _exp_year = int(_exp_m.group(2))
                    if _exp_year > 2400:
                        _exp_year -= 543
                    if _exp_year < 100:
                        _exp_year += 2000
                else:
                    now = datetime.now(timezone.utc)
                    _exp_month = now.month
                    _exp_year = now.year
            else:
                now = datetime.now(timezone.utc)
                _exp_month = now.month
                _exp_year = now.year

            summary = expense_tracker.get_monthly_summary(
                _exp_year, _exp_month
            )

            _thai_yr = _exp_year + 543
            _lines = [
                f"🧾 สรุปค่าใช้จ่าย {_exp_month}/{_thai_yr}",
                "━━━━━━━━━━━━━━━━━━━━",
            ]

            _inv_count = summary.get("invoice_count", 0)
            if _inv_count == 0:
                _lines.append("ไม่พบข้อมูลค่าใช้จ่ายในเดือนนี้")
            else:
                _total = summary.get("total", 0)
                _vat = summary.get("vat_total", 0)
                _lines.append(f"📊 จำนวน invoice: {_inv_count} ใบ")
                _lines.append(f"💰 รวมทั้งสิ้น: {_total:,.2f} บาท")
                if _vat > 0:
                    _lines.append(f"📋 VAT รวม: {_vat:,.2f} บาท")

                # แยกตามบริษัท (top 5)
                _by_vendor = summary.get("by_vendor", {})
                if _by_vendor:
                    _lines.append("")
                    _lines.append("🏢 แยกตามบริษัท:")
                    for i, (v_name, v_total) in enumerate(
                        _by_vendor.items()
                    ):
                        if i >= 5:
                            _remain = len(_by_vendor) - 5
                            _lines.append(f"  ... อีก {_remain} บริษัท")
                            break
                        _lines.append(
                            f"  • {v_name}: {v_total:,.2f} บาท"
                        )

                # สถานะการชำระ
                _by_status = summary.get("by_status", {})
                if _by_status:
                    _lines.append("")
                    _paid = _by_status.get("paid", 0)
                    _unpaid = _by_status.get("unpaid", 0)
                    if _paid:
                        _lines.append(f"✅ ชำระแล้ว: {_paid} ใบ")
                    if _unpaid:
                        _lines.append(f"⏳ ยังไม่ชำระ: {_unpaid} ใบ")

            return "\n".join(_lines)

        except Exception as exp_err:
            logger.warning(
                "Expense summary LINE cmd error: %s",
                type(exp_err).__name__,
            )
            return "ขออภัย ไม่สามารถดึงข้อมูลค่าใช้จ่ายได้ในขณะนี้"

    # ── สรุปผู้ป่วย (Patient Summary) ──
    if (text_stripped.startswith("สรุป HN ")
            or text_stripped.startswith("สรุป HN")
            or text_stripped.startswith("สรุปHN")
            or text_stripped.startswith("สรุป ")):
        _summary_hn = (
            text_stripped
            .replace("สรุป HN ", "")
            .replace("สรุป HN", "")
            .replace("สรุปHN", "")
            .replace("สรุป", "")
            .strip()
        )
        if not _summary_hn:
            return "กรุณาระบุ HN เช่น: สรุป HN 56001"

        try:
            # ── Resolve patient: hn_registry → master_registry ──
            # (เหมือนกับที่ compare timeline ทำ)
            _s_oh = ""       # owner_hash (จาก hn_registry)
            _s_pf = ""       # patient_folder (จาก master_registry)
            _s_name = ""
            _s_clinic_hn = ""
            _s_hospital_hn = ""
            _s_found = False
            _s_hn_q = _summary_hn.replace("HN", "").replace(
                "hn", ""
            ).strip()

            # 1) ค้นจาก hn_registry ก่อน
            _s_phn = None
            if hn_registry:
                _s_phn = hn_registry.get_by_clinic_hn(_s_hn_q)
                if not _s_phn:
                    _s_phn = hn_registry.get_by_hospital_hn(
                        _s_hn_q
                    )
                if not _s_phn:
                    _s_hn_m = hn_registry.search_hn(_s_hn_q)
                    if len(_s_hn_m) == 1:
                        _s_phn = _s_hn_m[0]
                    elif len(_s_hn_m) > 1:
                        _ml = [f"🔍 พบหลายรายการ HN {_summary_hn}:"]
                        for _m in _s_hn_m[:5]:
                            _ml.append(
                                f"  • HN {_m.clinic_hn or _m.hospital_hn}"
                                f" — {_m.display_name or _m.owner_hash[:8]}"
                            )
                        _ml.append("\nกรุณาระบุ HN ให้ชัดเจน")
                        return "\n".join(_ml)

            if _s_phn:
                _s_oh = _s_phn.owner_hash
                _s_clinic_hn = _s_phn.clinic_hn
                _s_hospital_hn = _s_phn.hospital_hn
                _s_name = _s_phn.display_name
                _s_found = True
                # เสริมข้อมูลจาก master_registry (ชื่อเต็ม, HN รพ.)
                if master_registry:
                    for _hn_try in [
                        _s_phn.clinic_hn, _s_phn.hospital_hn,
                    ]:
                        if not _hn_try:
                            continue
                        _mr_sup = master_registry.search(_hn_try)
                        if len(_mr_sup) == 1:
                            _s_pf = _mr_sup[0].folder_name
                            if not _s_name:
                                _s_name = _mr_sup[0].patient_name
                            if not _s_hospital_hn:
                                _s_hospital_hn = (
                                    _mr_sup[0].hospital_hn
                                )
                            if not _s_clinic_hn:
                                _s_clinic_hn = (
                                    _mr_sup[0].clinic_hn
                                )
                            break

            # 2) ค้นจาก master_registry
            if not _s_found and master_registry:
                _mr_m = master_registry.search(_s_hn_q)
                if len(_mr_m) == 1:
                    _s_pf = _mr_m[0].folder_name
                    _s_clinic_hn = _mr_m[0].clinic_hn
                    _s_hospital_hn = _mr_m[0].hospital_hn
                    _s_name = _mr_m[0].patient_name
                    _s_found = True
                elif len(_mr_m) > 1:
                    _ml = [f"🔍 พบหลายรายการ HN {_summary_hn}:"]
                    for _mp in _mr_m[:5]:
                        _ml.append(
                            f"  • HN {_mp.clinic_hn or _mp.hospital_hn}"
                            f" — {_mp.patient_name}"
                        )
                    _ml.append("\nกรุณาระบุ HN ให้ชัดเจน")
                    return "\n".join(_ml)

            # 3) ลองค้นจาก doc_index โดยตรง
            if not _s_found:
                _s_pf = _s_hn_q
                try:
                    _s_dd = doc_index.list_by_patient_folder(_s_hn_q)
                except Exception:
                    _s_dd = []
                if _s_dd:
                    _s_found = True
                    _s_clinic_hn = _s_hn_q

            if not _s_found:
                return (
                    f"ไม่พบผู้ป่วย HN {_summary_hn} ในทะเบียน\n"
                    f"ลองลงทะเบียนก่อน หรือตรวจสอบ HN"
                )

            lines = [
                "👤 สรุปข้อมูลผู้ป่วย",
                "━━━━━━━━━━━━━━━━━━━━",
            ]
            if _s_name:
                lines.append(f"ชื่อ: {_s_name}")
            if _s_clinic_hn:
                lines.append(f"HN คลินิก: {_s_clinic_hn}")
            if _s_hospital_hn:
                lines.append(f"HN รพ.: {_s_hospital_hn}")
            lines.append("")

            # ── สร้างรายการ folder ที่จะลองค้น ──
            _s_folders = []
            if _s_pf:
                _s_folders.append(_s_pf)
            # เพิ่ม patterns อื่นๆ
            if _s_clinic_hn:
                if _s_clinic_hn not in _s_folders:
                    _s_folders.append(_s_clinic_hn)
                _hn_pf = f"HN{_s_clinic_hn}"
                if _hn_pf not in _s_folders:
                    _s_folders.append(_hn_pf)
            if _s_hospital_hn:
                _hn_hp = f"HN{_s_hospital_hn}"
                if _hn_hp not in _s_folders:
                    _s_folders.append(_hn_hp)
                if _s_hospital_hn not in _s_folders:
                    _s_folders.append(_s_hospital_hn)

            def _try_history(fn, oh, folders):
                """ลองดึงข้อมูลจาก owner_hash หรือหลาย folder"""
                if oh:
                    result = fn(storage, doc_index, oh,
                                patient_folder="")
                    if result:
                        return result
                for pf in folders:
                    result = fn(storage, doc_index, "",
                                patient_folder=pf)
                    if result:
                        return result
                return []

            # ── ผลแล็บล่าสุด ──
            try:
                _lab_history = _try_history(
                    get_patient_lab_history,
                    _s_oh, _s_folders,
                )
                if _lab_history:
                    # รวมผลแล็บจากทุกรอบที่มี
                    _latest_lab = _lab_history[0]
                    _lab_count = len(_lab_history)
                    lines.append(
                        f"🔬 ผลแล็บล่าสุด "
                        f"(มี {_lab_count} ครั้ง)"
                    )
                    _lab_date = _latest_lab.get(
                        "upload_date", "ไม่ทราบ"
                    )
                    lines.append(f"   วันที่: {_lab_date}")
                    _lab_results = _latest_lab.get(
                        "results", []
                    )
                    # แสดงทุกค่าที่มี
                    for _lr in _lab_results[:15]:
                        _a = _lr.get("analyte", "?")
                        _v = _lr.get("value", "")
                        _u = _lr.get("unit", "")
                        lines.append(f"   • {_a}: {_v} {_u}")
                    if len(_lab_results) > 15:
                        lines.append(
                            f"   ... อีก "
                            f"{len(_lab_results) - 15} รายการ"
                        )
                    if not _lab_results:
                        lines.append(
                            "   (ผลแล็บยังไม่ถูก extract)"
                        )
                else:
                    lines.append("🔬 ผลแล็บ: ยังไม่มีข้อมูล")
            except Exception as _lab_err:
                logger.warning(
                    "Patient summary lab error: %s: %s",
                    type(_lab_err).__name__, _lab_err,
                )
                lines.append("🔬 ผลแล็บ: ไม่สามารถดึงข้อมูลได้")

            lines.append("")

            # ── ยาปัจจุบัน ──
            try:
                _rx_history = _try_history(
                    get_patient_rx_history,
                    _s_oh, _s_folders,
                )
                if _rx_history:
                    _latest_rx = _rx_history[0]
                    _rx_count = len(_rx_history)
                    lines.append(
                        f"💊 ยาปัจจุบัน (มี {_rx_count} ใบยา)"
                    )
                    _rx_date = _latest_rx.get(
                        "upload_date", "ไม่ทราบ"
                    )
                    lines.append(f"   วันที่: {_rx_date}")
                    _rx_items = _latest_rx.get("drugs", [])
                    for _ri in _rx_items[:12]:
                        lines.append(f"   • {_ri}")
                    if len(_rx_items) > 12:
                        lines.append(
                            f"   ... อีก "
                            f"{len(_rx_items) - 12} รายการ"
                        )
                    if not _rx_items:
                        lines.append(
                            "   (ยังไม่ถูก extract)"
                        )
                else:
                    lines.append("💊 ยา: ยังไม่มีข้อมูล")
            except Exception as _rx_err:
                logger.warning(
                    "Patient summary rx error: %s: %s",
                    type(_rx_err).__name__, _rx_err,
                )
                lines.append("💊 ยา: ยังไม่มีข้อมูล")

            lines.append("")

            # ── ฟอกเลือดล่าสุด ──
            try:
                _dial_history = _try_history(
                    get_patient_dialysis_history,
                    _s_oh, _s_folders,
                )
                if _dial_history:
                    _latest_dial = _dial_history[0]
                    _dial_count = len(_dial_history)
                    lines.append(
                        f"🩸 ฟอกเลือดล่าสุด "
                        f"(มี {_dial_count} ครั้ง)"
                    )
                    _dial_date = _latest_dial.get(
                        "upload_date", "ไม่ทราบ"
                    )
                    lines.append(f"   วันที่: {_dial_date}")
                    _dial_data = _latest_dial.get("data", {})
                    # แปลง key → ชื่อภาษาไทย
                    _dial_names = {
                        "dry_weight": "น้ำหนักแห้ง",
                        "uf_volume": "UF",
                        "duration": "เวลาฟอก (ชม.)",
                        "dialyzer": "Dialyzer",
                        "blood_flow": "Blood Flow",
                        "hd_no": "HD No.",
                        "pre_weight": "น้ำหนักก่อน",
                        "post_weight": "น้ำหนักหลัง",
                        "pre_bp": "BP ก่อน",
                        "post_bp": "BP หลัง",
                        "kt_v": "Kt/V",
                    }
                    if isinstance(_dial_data, dict):
                        for _dk, _dv in list(
                            _dial_data.items()
                        )[:12]:
                            _dn = _dial_names.get(_dk, _dk)
                            lines.append(f"   • {_dn}: {_dv}")
                else:
                    lines.append("🩸 ฟอกเลือด: ยังไม่มีข้อมูล")
            except Exception as _dial_err:
                logger.warning(
                    "Patient summary dialysis error: %s: %s",
                    type(_dial_err).__name__, _dial_err,
                )
                lines.append("🩸 ฟอกเลือด: ยังไม่มีข้อมูล")

            # จำนวนเอกสาร + แยกประเภท (เฉพาะ HN)
            _doc_count = 0
            _cat_counts = {}
            try:
                _all_docs = []
                # ใช้ patient_folder เป็นหลัก (ไม่ใช้ owner_hash)
                for _f in _s_folders:
                    _all_docs = (
                        doc_index.list_by_patient_folder(_f)
                    )
                    if _all_docs:
                        break
                if not _all_docs and _s_oh and not _s_folders:
                    _all_docs = doc_index.list_by_owner(_s_oh)
                _doc_count = len(_all_docs) if _all_docs else 0
                for _d in (_all_docs or []):
                    _dc = _d.get("category", "other")
                    _cat_counts[_dc] = _cat_counts.get(_dc, 0) + 1
            except Exception:
                pass
            lines.append("")
            lines.append(
                f"📁 เอกสารทั้งหมด: {_doc_count} ฉบับ"
            )
            if _cat_counts:
                _cat_emojis = {
                    "lab": "🔬", "rx": "💊",
                    "dialysis": "🩸", "bills": "🧾",
                    "review": "📋",
                }
                _cat_parts = []
                for _ck, _cv in sorted(
                    _cat_counts.items(),
                    key=lambda x: -x[1],
                ):
                    _ce = _cat_emojis.get(_ck, "📄")
                    _cn = _CAT_NAMES.get(_ck, _ck)
                    _cat_parts.append(f"{_ce}{_cn} {_cv}")
                lines.append(
                    "   " + " | ".join(_cat_parts)
                )

            # ── ข้อมูลไตเทียม (จาก master_registry) ──
            _s_mr = master_registry.match_hn(_s_hn_q) if master_registry else None
            if _s_mr and _s_mr.dialysis_schedule:
                from patient_master_registry import THAI_DAY_DISPLAY as _TDS
                _ds_days = "/".join(
                    _TDS.get(d, d) for d in (_s_mr.dialysis_days or [])
                ) or _s_mr.dialysis_schedule
                _esa_txt = _s_mr.esa_medication or _s_mr.esa_code or "-"
                lines.append("")
                lines.append("🏥 ไตเทียม")
                lines.append(f"   📅 รอบฟอก: {_ds_days}")
                lines.append(f"   🔄 HD/สัปดาห์: {_s_mr.hd_per_week or '-'}")
                lines.append(f"   💊 ESA: {_esa_txt}")
                _va = _s_mr.vascular_access or "ยังไม่ระบุ"
                lines.append(f"   🩸 Vascular: {_va}")

            # ── Hb trend + ESA assessment (Lab Intelligence) ──
            try:
                _hb_hist = _get_hb_history(_s_hn_q)
                if _hb_hist and len(_hb_hist) >= 1:
                    from lab_intelligence_agent import analyze_esa_response
                    _esa_r = analyze_esa_response(
                        _hb_hist,
                        esa_code=(_s_mr.esa_code if _s_mr else ""),
                        esa_medication=(_s_mr.esa_medication if _s_mr else ""),
                    )
                    lines.append("")
                    lines.append(f"🧠 AI Assessment: {_esa_r.recommendation.split(chr(10))[0]}")
            except Exception:
                pass

            # ── ลิงก์ดูรายละเอียดเว็บ ──
            _pd_base = os.getenv(
                "RENDER_EXTERNAL_URL", ""
            ).rstrip("/")
            if _pd_base:
                _cmp_hn = _s_hospital_hn or _s_clinic_hn or _s_hn_q
                _pd_url = f"{_pd_base}/admin/compare/{_cmp_hn}"
                if len(_pd_url) <= LINE_URI_ACTION_MAX_LENGTH:
                    lines.append("")
                    lines.append("🔗 เปรียบเทียบย้อนหลัง:")
                    lines.append(_pd_url)

            lines.append("")
            lines.append("💡 คำสั่งเพิ่มเติม:")
            lines.append(f"• วิเคราะห์ {_s_hn_q} — AI วิเคราะห์เชิงลึก")
            lines.append(f"• ผลแล็บ HN {_s_hn_q}")
            lines.append(f"• ตรวจยา HN {_s_hn_q}")
            lines.append(f"• ติดตามHb {_s_hn_q}")
            lines.append(f"• ข้อมูลฟอก {_s_hn_q}")

            audit_logger.log("patient_summary_view", user_hash, {
                "hn": _s_hn_q,
            })

            return "\n".join(lines)

        except Exception as e:
            logger.warning(
                "Patient summary error: %s: %s",
                type(e).__name__, e,
            )
            return (
                f"ขออภัย ไม่สามารถสรุปข้อมูลผู้ป่วย "
                f"HN {_summary_hn} ได้ในขณะนี้"
            )

    # ── คำสั่งการรักษา / ปรับยา / ประวัติการรักษา (เพิ่ม 2026-11-06) ──
    # ต้องอยู่ใน process function นี้ ไม่ใช่ใน webhook handler โดยตรง
    if text_stripped.startswith(("คำสั่งการรักษา ", "ดูคำสั่ง ")):
        _tn_q = text_stripped
        for _pf in ("คำสั่งการรักษา ", "ดูคำสั่ง "):
            if _tn_q.startswith(_pf):
                _tn_q = _tn_q[len(_pf):].strip()
                break
        _tn_p = master_registry.match_hn(_tn_q)
        if not _tn_p:
            return f"❌ ไม่พบ HN: {_tn_q} ในทะเบียน"
        _tn_hn = _tn_p.clinic_hn or _tn_q
        try:
            _tn_notes = []
            _tn_prefix = f"_treatment_notes/{_tn_hn}/"
            if storage.is_ready and hasattr(storage, '_bucket'):
                _tn_blobs = storage._bucket.list_blobs(prefix=_tn_prefix)
                for _tn_b in _tn_blobs:
                    try:
                        _tn_d = json.loads(_tn_b.download_as_text())
                        _tn_notes.append(_tn_d)
                    except Exception:
                        pass
            _tn_notes.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            if not _tn_notes:
                return (f"📋 HN: {_tn_hn} ({_tn_p.patient_name})\n"
                        f"ยังไม่มีคำสั่งการรักษาในระบบ")
            _tn_lines = [
                "📋 คำสั่งการรักษา",
                "━━━━━━━━━━━━━━━━",
                f"🔢 HN: {_tn_hn}",
                f"👤 {_tn_p.patient_name}",
                f"📄 ทั้งหมด {len(_tn_notes)} รายการ",
                "",
            ]
            for _i, _n in enumerate(_tn_notes[:5], 1):
                _dt = _n.get("created_at", "")[:10]
                _act = _n.get("action", "")
                _note = _n.get("note", "")[:80]
                _dtype = _n.get("doc_type", "")
                _act_th = {"treatment": "💊 สั่งการรักษา",
                           "edit": "✏️ แก้ไข"}.get(_act, f"📝 {_act}")
                _tn_lines.append(f"{_i}. [{_dt}] {_act_th}")
                if _dtype:
                    _tn_lines.append(f"   📂 {_dtype}")
                _tn_lines.append(f"   {_note}")
                _tn_lines.append("")
            if len(_tn_notes) > 5:
                _tn_lines.append(f"... อีก {len(_tn_notes)-5} รายการ")
            _tn_lines.append("─" * 20)
            _tn_lines.append("⚕️ ข้อมูลประกอบการตัดสินใจ")
            return "\n".join(_tn_lines)
        except Exception as _tn_err:
            logger.warning("Treatment notes: %s", _tn_err)
            return "ขออภัย ไม่สามารถดึงข้อมูลได้"

    if text_stripped.startswith(("ปรับยา ", "ปรับการรักษา ")):
        _rx_text = text_stripped
        for _pf in ("ปรับยา ", "ปรับการรักษา "):
            if _rx_text.startswith(_pf):
                _rx_text = _rx_text[len(_pf):].strip()
                break
        _rx_parts = _rx_text.split(None, 1)
        if len(_rx_parts) < 2:
            return ("⚠️ รูปแบบ: ปรับยา <HN> <รายละเอียด>\n"
                    "ตัวอย่าง:\n"
                    "• ปรับยา 56001 เพิ่ม Epiao 4000→6000 เพราะ Hb ต่ำ\n"
                    "• ปรับยา 69453 หยุด amlodipine เพราะ BP ต่ำ")
        _rx_hn = _rx_parts[0]
        _rx_detail = _rx_parts[1]
        _rx_patient = master_registry.match_hn(_rx_hn)
        if not _rx_patient:
            return f"❌ ไม่พบ HN: {_rx_hn} ในทะเบียน"
        try:
            _parsed_rx = treatment_tracker.parse_treatment_text(_rx_detail)
            _cat = treatment_tracker.detect_category(_parsed_rx.get("drug_name", ""))
            _change = treatment_tracker.record_change(
                patient_hn=_rx_patient.clinic_hn or _rx_hn,
                category=_cat,
                action=_parsed_rx.get("action", "other"),
                drug_name=_parsed_rx.get("drug_name", _rx_detail),
                old_value=_parsed_rx.get("old_value", ""),
                new_value=_parsed_rx.get("new_value", ""),
                reason=_parsed_rx.get("reason", ""),
            )
            _cat_th = TREATMENT_CATEGORIES.get(_cat, {}).get("name_th", _cat)
            return (f"✅ บันทึกการปรับการรักษา\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"🔢 HN: {_change.patient_hn}\n"
                    f"👤 {_rx_patient.patient_name}\n"
                    f"💊 {_change.drug_name}\n"
                    f"🔄 {_change.old_value} → {_change.new_value}\n"
                    f"📝 เหตุผล: {_change.reason or '-'}\n"
                    f"📂 หมวด: {_cat_th}\n"
                    f"\n📅 ติดตามผล: {_change.followup_date}\n"
                    f"🔬 ตรวจ: {_change.monitor_lab} (เป้า {_change.monitor_target})")
        except Exception as _rx_err:
            logger.warning("Treatment record: %s", _rx_err)
            return "ขออภัย ไม่สามารถบันทึกได้ในขณะนี้"

    if text_stripped.startswith(("ประวัติการรักษา ", "ประวัติยา ")):
        _hist_q = text_stripped
        for _pf in ("ประวัติการรักษา ", "ประวัติยา "):
            if _hist_q.startswith(_pf):
                _hist_q = _hist_q[len(_pf):].strip()
                break
        _hist_p = master_registry.match_hn(_hist_q)
        if not _hist_p:
            return f"❌ ไม่พบ HN: {_hist_q} ในทะเบียน"
        _hist_hn = _hist_p.clinic_hn or _hist_q
        return treatment_tracker.format_history_for_line(_hist_hn, _hist_p.patient_name)

    if text_stripped in ("ติดตามผลการรักษา", "followup", "ติดตามผลยา", "รายการติดตาม"):
        try:
            _pending = treatment_tracker.get_pending_followups()
            return treatment_tracker.format_pending_for_line(_pending)
        except Exception:
            return "ขออภัย ไม่สามารถดึงรายการติดตามได้"

    # ── ข้อ 3: Multi-Agent Patient Summary (สรุปทั้งหมด HN) ──
    _summary_all_match = re.match(
        r"^(?:สรุปทั้งหมด|สรุปครบ|summary)\s+(?:HN\s*)?(\d{3,10})$",
        text_stripped, re.IGNORECASE)
    if _summary_all_match:
        _sa_hn = _summary_all_match.group(1)
        try:
            _sa_p = master_registry.match_hn(_sa_hn) if master_registry else None
            if not _sa_p:
                return f"❌ ไม่พบ HN {_sa_hn} ในทะเบียน"

            _sa_real_hn = getattr(_sa_p, 'clinic_hn', _sa_hn) or _sa_hn
            _sa_name = getattr(_sa_p, 'patient_name', f"HN {_sa_real_hn}")
            _sa_esa = getattr(_sa_p, 'esa_drug', '') or "ไม่ระบุ"
            _sa_dose = getattr(_sa_p, 'esa_dose', '') or ""
            _sa_sched = getattr(_sa_p, 'schedule_days', '') or "ไม่ระบุ"
            _sa_vasc = getattr(_sa_p, 'vascular_access', '') or "ไม่ระบุ"
            _sa_dw = getattr(_sa_p, 'dry_weight', '') or ""

            # Agent 1: Lab data — ดึงจาก doc_index จริง
            _sa_lab_text = "ยังไม่มีผลแล็บ"
            try:
                _sa_docs = _get_patient_lab_docs(_sa_p, _sa_real_hn, limit=1)
                if _sa_docs:
                    _sa_labs = _extract_latest_labs(
                        _sa_docs[-1].get("extracted_text", ""))
                    _sa_parts = []
                    if _sa_labs.get("hb") and _sa_labs["hb"] > 0:
                        _hb_flag = "⚠️" if _sa_labs["hb"] < 10 else "✅"
                        _sa_parts.append(f"Hb {_sa_labs['hb']:.1f} {_hb_flag}")
                    if _sa_labs.get("cr") and _sa_labs["cr"] > 0:
                        _sa_parts.append(f"Cr {_sa_labs['cr']:.1f}")
                    if _sa_labs.get("k") and _sa_labs["k"] > 0:
                        _k_flag = "⚠️" if _sa_labs["k"] > 5.5 else ""
                        _sa_parts.append(f"K {_sa_labs['k']:.1f} {_k_flag}")
                    if _sa_labs.get("po4") and _sa_labs["po4"] > 0:
                        _sa_parts.append(f"PO4 {_sa_labs['po4']:.1f}")
                    if _sa_labs.get("albumin") and _sa_labs["albumin"] > 0:
                        _sa_parts.append(f"Alb {_sa_labs['albumin']:.1f}")
                    if _sa_parts:
                        _sa_date = _sa_docs[-1].get("upload_date", "")[:10]
                        _sa_lab_text = f"({_sa_date}) " + " | ".join(_sa_parts)
            except Exception as _lab_err:
                logger.warning(f"Summary lab error: {_lab_err}")

            # Agent 2: Treatment data
            _sa_tx_text = "ยังไม่มีประวัติปรับยา"
            _sa_pending = 0
            try:
                _sa_hist = treatment_tracker.get_history(_sa_real_hn, limit=3)
                if _sa_hist:
                    _tx_lines = []
                    for _ch in _sa_hist:
                        _tx_lines.append(
                            f"• {_ch.get('change_date', '')[:10]} "
                            f"{_ch.get('action', '')} {_ch.get('drug_name', '')}")
                    _sa_tx_text = "\n".join(_tx_lines)
                _sa_pend = treatment_tracker.get_pending_followups(_sa_real_hn)
                _sa_pending = len(_sa_pend) if _sa_pend else 0
            except Exception:
                pass

            # Agent 3: Appointment
            _sa_appt = "ไม่มีนัดหมาย"
            try:
                if appointment_store and hasattr(appointment_store, 'list_for_hn'):
                    _sa_appts = appointment_store.list_for_hn(_sa_real_hn, limit=1)
                    if _sa_appts:
                        _a = _sa_appts[0]
                        _sa_appt = f"{_a.get('date', '')} {_a.get('type', '')}"
            except Exception:
                pass

            # Build Flex
            def _sa_row(label, value, color="#555555"):
                return {"type": "box", "layout": "horizontal", "spacing": "sm",
                        "contents": [
                            {"type": "text", "text": label, "size": "sm",
                             "color": "#999999", "flex": 2},
                            {"type": "text", "text": str(value), "size": "sm",
                             "color": color, "flex": 3, "wrap": True}]}

            _sa_body = [
                {"type": "text", "text": f"📋 {_sa_name}", "weight": "bold",
                 "size": "lg", "color": "#1E88E5"},
                {"type": "text", "text": f"HN {_sa_real_hn}", "size": "sm",
                 "color": "#999999"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "🏥 ข้อมูลไตเทียม", "weight": "bold",
                 "size": "sm", "color": "#2E8B57", "margin": "md"},
                _sa_row("ตารางฟอก", _sa_sched),
                _sa_row("ESA", f"{_sa_esa} {_sa_dose}".strip()),
                _sa_row("Vascular", _sa_vasc),
            ]
            if _sa_dw:
                _sa_body.append(_sa_row("DW", f"{_sa_dw} kg"))

            _sa_body.extend([
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "🔬 ผลแล็บล่าสุด", "weight": "bold",
                 "size": "sm", "color": "#E65100", "margin": "md"},
                {"type": "text", "text": _sa_lab_text, "size": "sm",
                 "color": "#555555", "wrap": True},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "💊 ปรับยาล่าสุด", "weight": "bold",
                 "size": "sm", "color": "#6735B7", "margin": "md"},
                {"type": "text", "text": _sa_tx_text, "size": "sm",
                 "color": "#555555", "wrap": True},
            ])
            if _sa_pending > 0:
                _sa_body.append({"type": "text",
                    "text": f"⏳ รอติดตามผล: {_sa_pending} รายการ",
                    "size": "xs", "color": "#C62828", "margin": "xs"})

            _sa_body.extend([
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "📅 นัดหมายถัดไป", "weight": "bold",
                 "size": "sm", "color": "#00796B", "margin": "md"},
                {"type": "text", "text": _sa_appt, "size": "sm",
                 "color": "#555555"},
            ])

            _sa_qr = [
                {"type": "action", "action": {"type": "message",
                 "label": f"วิเคราะห์ {_sa_real_hn}", "text": f"วิเคราะห์ {_sa_real_hn}"}},
                {"type": "action", "action": {"type": "message",
                 "label": f"กราฟ {_sa_real_hn}", "text": f"กราฟ {_sa_real_hn}"}},
                {"type": "action", "action": {"type": "message",
                 "label": f"เปรียบเทียบ {_sa_real_hn}", "text": f"เปรียบเทียบ {_sa_real_hn}"}},
            ]

            return {
                "type": "flex",
                "altText": f"📋 สรุป {_sa_name} (HN {_sa_real_hn})",
                "quickReply": {"items": _sa_qr},
                "contents": {"type": "bubble", "body": {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "paddingAll": "16px", "contents": _sa_body}},
            }
        except Exception as _sa_err:
            logger.warning(f"Patient summary error: {_sa_err}")
            return f"❌ ไม่สามารถสร้างสรุปได้: {str(_sa_err)[:60]}"

    # ── ข้อ 6: Lab Trend Chart (กราฟ HN) ──
    _chart_match = re.match(
        r"^(?:กราฟ|chart|graph|trend)\s+(?:HN\s*)?(\d{3,10})$",
        text_stripped, re.IGNORECASE)
    if _chart_match:
        _ch_hn = _chart_match.group(1)
        _ch_patient = master_registry.match_hn(_ch_hn) if master_registry else None
        if not _ch_patient:
            return f"❌ ไม่พบ HN {_ch_hn} ในทะเบียน"

        _ch_real_hn = _ch_patient.clinic_hn or _ch_hn
        _ch_name = _ch_patient.patient_name or f"HN {_ch_real_hn}"

        # ดึง lab history
        _ch_labs = []
        try:
            docs = _get_patient_lab_docs(_ch_patient, _ch_real_hn, limit=10)
            for d in docs:
                ext = d.get("extracted_text", "")
                if ext:
                    labs = _extract_latest_labs(ext)
                    labs["date"] = d.get("upload_date", "")[:10]
                    _ch_labs.append(labs)
        except Exception:
            pass

        if not _ch_labs:
            return (f"📊 {_ch_name} (HN {_ch_real_hn})\n"
                    f"ยังไม่มีข้อมูลแล็บสำหรับสร้างกราฟ\n"
                    f"💡 ลองพิมพ์ 'เปรียบเทียบ {_ch_real_hn}' เพื่อดูหน้าเปรียบเทียบ")

        # สร้าง text-based trend
        _ch_labs.sort(key=lambda x: x.get("date", ""))
        lines = [
            f"📈 กราฟแนวโน้ม {_ch_name}",
            f"HN {_ch_real_hn} ({len(_ch_labs)} ครั้ง)",
            "━━━━━━━━━━━━━━━━━━━━",
        ]

        # Hb trend
        hb_vals = [(l["date"], l.get("hb")) for l in _ch_labs if l.get("hb") and l["hb"] > 0]
        if hb_vals:
            lines.append("\n🔴 Hb (g/dL) เป้า 10-12:")
            for date, val in hb_vals[-6:]:
                bar_len = int(val * 2)
                bar = "█" * min(bar_len, 24)
                flag = " ⚠️" if val < 10 or val > 12 else " ✅"
                lines.append(f"  {date[5:]} {bar} {val:.1f}{flag}")

        # Cr trend
        cr_vals = [(l["date"], l.get("cr")) for l in _ch_labs if l.get("cr") and l["cr"] > 0]
        if cr_vals:
            lines.append("\n🟡 Creatinine (mg/dL):")
            for date, val in cr_vals[-6:]:
                bar_len = int(val * 2)
                bar = "█" * min(bar_len, 24)
                lines.append(f"  {date[5:]} {bar} {val:.1f}")

        # K trend
        k_vals = [(l["date"], l.get("k")) for l in _ch_labs if l.get("k") and l["k"] > 0]
        if k_vals:
            lines.append("\n🟠 K+ (mEq/L) เป้า 3.5-5.5:")
            for date, val in k_vals[-6:]:
                bar_len = int(val * 4)
                bar = "█" * min(bar_len, 24)
                flag = " ⚠️" if val > 5.5 or val < 3.5 else " ✅"
                lines.append(f"  {date[5:]} {bar} {val:.1f}{flag}")

        _compare_url = f"{_RENDER_BASE}/admin/compare/{_ch_real_hn}"
        lines.append(f"\n🔗 ดูรายละเอียด: {_compare_url}")

        return "\n".join(lines)

    # ── V5.0 Conversational Command Center ──────────────────
    # ถึงจุดนี้ = ผู้ใช้ได้รับอนุญาตแล้ว แต่คำสั่งเดิม (legacy) ไม่รู้จัก
    # ให้ Thai Intent Parser + Agent Orchestrator ลองตีความภาษาธรรมชาติ
    # (rule-based, ไม่ใช้ External AI) — ตอบเฉพาะ intent ที่ Agent จัดการเองได้
    if v5 is not None:
        _v5_reply = v5.handle_line_text(text_stripped, user_hash)
        if _v5_reply:
            audit_logger.log("v5_conversational", user_hash, {
                "command": text_stripped[:20],
            })
            return _v5_reply

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

# ── รหัสผูกบัญชีผู้ป่วย (ดู patient_link_code.py) ──────────────
try:
    from patient_link_code import (
        LinkCodeStore, parse_issue_command, parse_link_command,
        parse_revoke_command,
    )
    link_code_store = LinkCodeStore(storage, state_store)
except Exception as _lc_err:  # pragma: no cover
    logger.error("patient_link_code init failed: %s", type(_lc_err).__name__)
    link_code_store = None
    parse_issue_command = parse_link_command = parse_revoke_command = (
        lambda _t: None
    )

# ── นัดหมายและแจ้งเตือนผู้ป่วย (ดู appointment_reminder.py) ─────────
try:
    import appointment_reminder as _apr
    appointment_store = _apr.AppointmentStore(storage)
except Exception as _ap_err:  # pragma: no cover
    logger.error("appointment_reminder init failed: %s", type(_ap_err).__name__)
    _apr = None
    appointment_store = None


def _patient_line_id_for_hn(clinic_hn: str) -> str:
    """HN → LINE userId ของผู้ป่วยที่ผูกบัญชีแล้วเท่านั้น ("" ถ้าไม่มี)"""
    try:
        _p = hn_registry.get_by_clinic_hn(clinic_hn)
        if not _p or not getattr(_p, "owner_hash", ""):
            return ""
        from alert_delivery import _lookup_enrolled_line_id
        return _lookup_enrolled_line_id(storage, _p.owner_hash)
    except Exception:
        return ""


def _patient_name_for_hn(clinic_hn: str) -> str:
    try:
        _m = master_registry.match_hn(clinic_hn) if master_registry else None
        return (_m.patient_name if _m else "") or ""
    except Exception:
        return ""


def _staff_line_ids() -> list[str]:
    """รวม LINE IDs ของเจ้าหน้าที่ทุก group (physician + admin + nurse)

    เดิม: ดึงแค่ physician → admin ไม่ได้รับ
    แก้: รวมทุก group แล้ว deduplicate
    """
    try:
        ids = set()
        for group in ("physician", "admin", "nurse", "staff"):
            for uid in (_alert_recipients.get(group, []) or []):
                if uid:
                    ids.add(uid)
        # fallback: ถ้าไม่มี group ไหนเลย ลองดึงค่าตรงจาก ENV
        if not ids:
            _env_ids = os.getenv("ALERT_RECIPIENTS", "").strip()
            if _env_ids:
                for uid in _env_ids.split(","):
                    uid = uid.strip()
                    if uid and uid.startswith("U"):
                        ids.add(uid)
        return list(ids)
    except Exception:
        return []


def _build_daily_digest() -> str:
    """สร้างสรุปประจำวันรวมทุกส่วนในข้อความเดียว (เพิ่ม 2026-11-05)"""
    from patient_master_registry import THAI_DAY_DISPLAY
    _bkk = BKK_TZ
    _now = datetime.now(_bkk)
    _day_map = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat"}
    _day_en = _day_map.get(_now.weekday(), "")
    _day_th = THAI_DAY_DISPLAY.get(_day_en, _day_en)
    _date_th = f"{_now.day}/{_now.month}/{_now.year + 543}"

    lines = [f"📊 สรุปประจำวัน{_day_th}ที่ {_date_th}", "━━━━━━━━━━━━━━━━━━━━"]

    # 1. ฟอกเลือดวันนี้ (รายชื่อครบ — แทน dialysis_staff_summary แยก)
    if _day_en:
        _pts = master_registry.get_patients_by_day(_day_en)
        lines.append(f"🏥 ฟอกเลือดวันนี้: {len(_pts)} คน")
        for _pi, _pp in enumerate(_pts[:20], 1):
            _esa_t = getattr(_pp, 'esa_code', '') or '-'
            lines.append(f"  {_pi}. {_pp.clinic_hn} {_pp.patient_name} | {_esa_t}")
        if len(_pts) > 20:
            lines.append(f"  ... อีก {len(_pts)-20} คน")
    else:
        lines.append("🏥 วันอาทิตย์ — ไม่มีรอบฟอก")

    # 1.5 นัดหมายวันนี้ (รวมจาก appointment_store)
    try:
        if appointment_store is not None:
            _todays_appts = appointment_store.list_on(_now.date())
            _active_appts = [a for a in _todays_appts if a.get("status") not in ("cancelled", "done")]
            if _active_appts:
                lines.append(f"\n📋 นัดหมายวันนี้: {len(_active_appts)} ราย")
                for _a in _active_appts[:5]:
                    _a_name = _patient_name_for_hn(_a.get("clinic_hn", "")) if callable(_patient_name_for_hn) else ""
                    lines.append(f"  • {_a.get('time','--:--')} HN {_a.get('clinic_hn','-')} {_a_name}")
                if len(_active_appts) > 5:
                    lines.append(f"  ... อีก {len(_active_appts)-5} ราย")
    except Exception:
        pass

    # 2. เอกสารใหม่เมื่อวาน
    _yesterday = (_now - timedelta(days=1)).strftime("%Y-%m-%d")
    _new_docs = 0
    _new_labs = 0
    for _d in (doc_index._docs if doc_index else []):
        if _d.get("upload_date", "").startswith(_yesterday):
            _new_docs += 1
            if _d.get("category", "") == "lab":
                _new_labs += 1
    lines.append(f"📄 เอกสารใหม่เมื่อวาน: {_new_docs} ({_new_labs} ผลแล็บ)")

    # 3. ค่าวิกฤตรอยืนยัน
    _pending_critical = 0
    if alert_service is not None:
        try:
            _alerts = alert_service.list_pending()
            _pending_critical = len(_alerts) if _alerts else 0
        except Exception:
            pass
    if _pending_critical > 0:
        lines.append(f"⚠️ ค่าวิกฤตรอยืนยัน: {_pending_critical} รายการ")
    else:
        lines.append("✅ ไม่มีค่าวิกฤตค้าง")

    # 4. ติดตามผลการรักษา (ข้อ 4 — actionable digest)
    try:
        _pending_tx = treatment_tracker.get_pending_followups()
        if _pending_tx:
            lines.append(f"\n💊 ติดตามผลยา: {len(_pending_tx)} ราย")
            for _ptx in _pending_tx[:3]:
                lines.append(
                    f"  • HN {_ptx.patient_hn}: {_ptx.drug_name} "
                    f"({_ptx.old_value}→{_ptx.new_value}) "
                    f"→ ตรวจ {_ptx.monitor_lab}")
            if len(_pending_tx) > 3:
                lines.append(f"  ... อีก {len(_pending_tx)-3} ราย")
            lines.append("  💡 พิมพ์ 'ติดตามผลการรักษา' ดูทั้งหมด")
    except Exception:
        pass

    # 5. Hb วิกฤต (< 8 g/dL) จากข้อมูล registry
    try:
        _low_hb = []
        for _p in (master_registry._patients.values()
                   if hasattr(master_registry, '_patients') else []):
            _p_hn = _p.clinic_hn if hasattr(_p, 'clinic_hn') else ""
            if _p_hn:
                _p_docs = _get_patient_lab_docs(_p, _p_hn, limit=1)
                if _p_docs:
                    _p_labs = _extract_latest_labs(
                        _p_docs[-1].get("extracted_text", ""))
                    _hb = _p_labs.get("hb")
                    if _hb and 0 < _hb < 8:
                        _p_name = _p.patient_name if hasattr(_p, 'patient_name') else _p_hn
                        _low_hb.append(f"  • HN {_p_hn} ({_p_name}): Hb {_hb:.1f}")
                if len(_low_hb) >= 5:
                    break  # จำกัดไม่ให้ loop นาน
        if _low_hb:
            lines.append(f"\n🔴 Hb < 8: {len(_low_hb)} ราย")
            for _lh in _low_hb:
                lines.append(_lh)
    except Exception:
        pass

    # 6. สถิติเอกสารรวม
    _total = len(doc_index._docs) if doc_index else 0
    _patients = master_registry.count() if master_registry else 0
    lines.append(f"\n📦 ทะเบียน: {_patients} คน | เอกสาร: {_total} รายการ")

    return "\n".join(lines)


_DAILY_ONCE_LOCAL: dict = {}   # fallback เมื่อ GCS ไม่พร้อม (กันซ้ำได้แค่ใน process)


def _claim_daily_once(job: str, day=None) -> bool:
    """True = ยังไม่มีใครส่งงานนี้วันนี้ และจองสิทธิ์ได้ (atomic ข้าม worker/restart)

    ใช้ GCS create-only object (if_generation_match=0) ที่ _markers/<job>_<date>.json
    ถ้า GCS ไม่พร้อม → กันซ้ำได้เฉพาะใน process (ดีกว่าไม่ส่งเลย)
    """
    from datetime import timezone as _tz, timedelta as _td
    day = day or datetime.now(_tz(_td(hours=7))).date()
    key = f"{job}_{day.isoformat()}"
    bucket = getattr(storage, "_bucket", None) if 'storage' in globals() else None
    if bucket is None:
        if _DAILY_ONCE_LOCAL.get(key):
            return False
        _DAILY_ONCE_LOCAL[key] = True
        return True
    try:
        bucket.blob(f"_markers/{key}.json").upload_from_string(
            json.dumps({"at": datetime.now(_tz(_td(hours=7))).isoformat()}),
            content_type="application/json", if_generation_match=0,
        )
        return True
    except Exception as e:
        if getattr(e, "code", None) == 412:
            return False          # มีคนจองไปแล้ววันนี้
        logger.warning("daily-once marker %s: %s", job, type(e).__name__)
        if _DAILY_ONCE_LOCAL.get(key):
            return False
        _DAILY_ONCE_LOCAL[key] = True
        return True


def job_daily_reminders() -> dict:
    """งานประจำวัน 08:00: เตือนนัดพรุ่งนี้/วันนี้ + นัดประจำ + ฟอกเลือด + สรุปเจ้าหน้าที่"""
    out: dict = {}
    if _apr is not None and appointment_store is not None:
        # ส่ง appointment reminders ถึงคนไข้ แต่ไม่ส่ง staff summary แยก
        # (staff summary รวมอยู่ใน daily_digest แล้ว)
        out["appointments"] = _apr.run_daily(
            appointment_store, _patient_line_id_for_hn, _patient_name_for_hn,
            push_message, [],  # empty staff list → ไม่ส่ง staff summary แยก
        )
    # ── hd_schedule_reminder ปิดแล้ว — รวมอยู่ใน _build_daily_digest() ──
    # เดิม hd_schedule_reminder.send_reminders() ส่งรายชื่อฟอกแยก
    # ทำให้ staff ได้รับ 2 ข้อความซ้ำ (ข้อความ hd_schedule + daily_digest)
    # ตอนนี้ _build_daily_digest() มีรายชื่อฟอกครบแล้ว ไม่ต้องส่งแยก
    out["hd"] = {"skipped": "merged into daily_digest"}
    # ── สรุปรายวันรวม → เจ้าหน้าที่ (เพิ่ม 2026-11-05) ──
    # ส่งแค่ข้อความเดียว — _build_daily_digest() รวมทุกอย่างแล้ว
    # (ฟอกวันนี้ + เอกสาร + ค่าวิกฤต + ติดตามผลยา + Hb ต่ำ)
    # ไม่ส่ง treatment_followup แยกอีก — เพราะรวมอยู่ใน digest แล้ว
    # ส่งได้วันละ 1 ครั้งจริง ๆ: จอง marker create-only ใน GCS ก่อนส่ง
    # (_last_run_day ใน thread เป็น memory ของ process — ทุก worker และทุก
    # restart/deploy ในชั่วโมง 08:xx จะส่งซ้ำ → หมอได้ 4 ข้อความ 12 พ.ย.)
    try:
        if not _claim_daily_once("daily_digest"):
            out["daily_digest"] = {"skipped": "already sent today (other worker/restart)"}
        else:
            _digest = _build_daily_digest()
            _sent = 0
            for _rid in _staff_line_ids():
                try:
                    push_message(_rid, _digest)
                    _sent += 1
                except Exception:
                    pass
            out["daily_digest"] = {"sent": _sent, "length": len(_digest)}
    except Exception as _e:
        out["daily_digest"] = {"error": type(_e).__name__}
    # treatment_followup: ข้อมูลรวมอยู่ใน daily_digest section 4 แล้ว ไม่ส่งแยก
    out["treatment_followup"] = {"merged_into": "daily_digest"}
    # ── Automated Daily Backup (เพิ่ม 2026-11-05) ──
    try:
        out["daily_backup"] = backup_manager.run_daily_backup(
            doc_index=doc_index,
            master_registry=master_registry,
        )
    except Exception as _e:
        out["daily_backup"] = {"error": type(_e).__name__}
        logger.warning("daily_backup: %s", _e)
    return out

# ── Structured Event Logger (V4.0 Phase 1.2) ──
from event_logger import EventLogger, generate_job_id, EventTimer
event_logger = EventLogger(max_buffer=1000)

# ── Backup Manager (V4.0 Phase 2.4) ──
from backup_manager import BackupManager
backup_manager = BackupManager()

# ── Treatment Change Tracker (2026-11-06) ──
from treatment_tracker import TreatmentTracker, TREATMENT_CATEGORIES
treatment_tracker = TreatmentTracker()

# ── Approval Workflow (V4.0 Phase 3.1) ──
from approval_workflow import ApprovalEngine
approval_engine = ApprovalEngine()

# ── RBAC v2 (V4.0 Phase 3.2) ──
from rbac import RBACEngine, Permission, Role
rbac_engine = RBACEngine(
    authorized_users=AUTHORIZED_USERS,
    physician_users=PHYSICIAN_USERS,
    admin_users=ADMIN_USERS,
)

# ── Policy Engine (V4.0 Phase 4.3) ──
from policy_engine import PolicyEngine
policy_engine = PolicyEngine()

# ── Scheduled Jobs (V4.0 Phase 5.1) ──
from scheduled_jobs import ScheduledJobManager
job_manager = ScheduledJobManager()
try:
    job_manager.register("daily_reminders", job_daily_reminders)
except Exception as _jr_err:  # pragma: no cover
    logger.warning("register daily_reminders failed: %s", type(_jr_err).__name__)

# ── V4.0: Initialize all modules with GCS storage ──
if STORAGE_BACKEND == "gcs" and 'storage' in dir():
    try:
        event_logger.set_storage(storage)
        backup_manager.set_storage(storage)
        approval_engine.set_storage(storage)
        treatment_tracker.set_storage(storage)
        logger.info("V4.0 modules initialized with GCS persistence")
    except Exception as _v4_err:
        logger.warning("V4.0 module init: %s", type(_v4_err).__name__)

# ── V5.0: Multi-Agent + Conversational + Workflow Builder ──
# (agent_orchestrator / thai_intent_parser / workflow_engine) — ไม่ใช้ External AI
v5 = None
try:
    import v5_integration as _v5i
    v5 = _v5i.init(
        storage=storage if 'storage' in dir() else None,
        doc_index=doc_index if 'doc_index' in dir() else None,
        state_store=state_store if 'state_store' in dir() else None,
        backup_manager=backup_manager,
        approval_engine=approval_engine,
        rbac=rbac_engine,
        policy_engine=policy_engine,
        event_logger=event_logger,
        push_message=None,  # นิยามทีหลัง — wire ใน block หลัง def push_message
    )
except Exception as _v5_err:
    logger.warning("V5.0 init: %s: %s", type(_v5_err).__name__, _v5_err)

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


def _ocr_with_google_vision(filepath: str) -> str:
    """ดึงข้อความด้วย Google Cloud Vision API.

    ข้อดี:
      - อ่านลายมือได้ดีกว่า Tesseract มาก (ทั้ง Thai + English)
      - รองรับรูปถ่ายมือถือ (เงา/เอียง/ไม่ชัด)
      - ไม่ใช้ memory/CPU ของ Render (ประมวลผลบน Google Cloud)
      - ข้อมูลอยู่ใน Google Cloud เดิม (ใช้ GCS อยู่แล้ว)

    ใช้ ADC (Application Default Credentials) ตัวเดียวกับ GCS
    """
    try:
        from google.cloud import vision

        client = vision.ImageAnnotatorClient()

        with open(filepath, "rb") as f:
            content = f.read()

        image = vision.Image(content=content)

        # ใช้ document_text_detection — ดีกว่า text_detection
        # สำหรับเอกสาร/ฟอร์มที่มี layout ซับซ้อน
        response = client.document_text_detection(
            image=image,
            image_context=vision.ImageContext(
                language_hints=["th", "en"],
            ),
        )

        if response.error.message:
            logger.error(
                "[Vision API] Error: %s", response.error.message
            )
            return ""

        text = response.full_text_annotation.text if response.full_text_annotation else ""
        logger.info("[Vision API] Extracted %d chars", len(text))
        return text.strip()

    except ImportError:
        logger.warning(
            "[Vision API] google-cloud-vision not installed — "
            "falling back to Tesseract"
        )
        return ""
    except Exception as e:
        logger.warning(
            "[Vision API] Failed: %s — falling back to Tesseract",
            type(e).__name__,
        )
        return ""


def _ocr_with_google_vision_pdf(filepath: str) -> str:
    """OCR PDF ด้วย Google Vision API — แปลง PDF → images → Vision."""
    try:
        from google.cloud import vision

        if not HAS_PYMUPDF or not HAS_PIL:
            return ""

        client = vision.ImageAnnotatorClient()
        doc = pymupdf.open(filepath)
        try:
            num_pages = min(len(doc), MAX_OCR_PAGES)
            texts = []
            for i in range(num_pages):
                try:
                    page = doc[i]
                    # Render page at 200 DPI (Vision API handles large images well)
                    mat = pymupdf.Matrix(200 / 72, 200 / 72)
                    pix = page.get_pixmap(matrix=mat)
                    img = Image.frombytes(
                        "RGB", [pix.width, pix.height], pix.samples
                    )
                    pix = None

                    # Convert to JPEG bytes for Vision API
                    import io
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    img.close()
                    content = buf.getvalue()
                    buf.close()

                    image = vision.Image(content=content)
                    response = client.document_text_detection(
                        image=image,
                        image_context=vision.ImageContext(
                            language_hints=["th", "en"],
                        ),
                    )
                    if response.full_text_annotation:
                        page_text = response.full_text_annotation.text.strip()
                        if page_text:
                            texts.append(page_text)
                            logger.info(
                                "[Vision API] PDF page %d: %d chars",
                                i, len(page_text),
                            )
                except Exception as pg_err:
                    logger.warning(
                        "[Vision API] PDF page %d error: %s",
                        i, type(pg_err).__name__,
                    )
        finally:
            doc.close()

        full_text = "\n\n".join(texts)
        logger.info(
            "[Vision API] PDF total: %d pages, %d chars",
            num_pages, len(full_text),
        )
        return full_text

    except ImportError:
        return ""
    except Exception as e:
        logger.warning("[Vision API] PDF failed: %s", type(e).__name__)
        return ""


def _ocr_upright(gray, timeout_sec: int = 20):
    """หมุนรูปให้ตั้งตรงก่อน OCR (Tesseract OSD).
    16 พ.ย. 2026: ใบตรวจรักษา รพ. ถ่ายแนวนอน → OCR ได้ตัวอักษรขยะ หา HN/ประเภทไม่ได้
    หมุนเฉพาะเมื่อ OSD มั่นใจ (confidence ≥ 2) — ไม่มั่นใจ = ใช้รูปเดิม"""
    try:
        osd = pytesseract.image_to_osd(gray, config="--psm 0", timeout=min(timeout_sec, 20))
        m_rot = re.search(r"Rotate:\s*(\d+)", osd)
        m_conf = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
        rot = int(m_rot.group(1)) if m_rot else 0
        conf = float(m_conf.group(1)) if m_conf else 0.0
        if rot in (90, 180, 270) and conf >= 2.0:
            logger.info("[Tesseract] rotate %d° (osd confidence %.1f)", rot, conf)
            return gray.rotate(-rot, expand=True)
    except Exception as e:
        logger.info("[Tesseract] osd skipped: %s", type(e).__name__)
    return gray


def _ocr_with_tesseract(filepath: str, timeout_sec: int = 45) -> str:
    """Fallback OCR ด้วย Tesseract (v3)."""
    if not HAS_TESSERACT or not HAS_PIL:
        return ""

    ext = os.path.splitext(filepath)[1].lower()
    # 1200 → 1600 px (16 พ.ย. 2026): รูปใบ A4 จากมือถือย่อเหลือ 1200 px ตัวพิมพ์เล็ก
    # อ่านเลข HN ผิด (67398 → 67308)
    MAX_OCR_WIDTH = 1600
    ocr_per_page_timeout = min(timeout_sec, 40)

    with _ocr_semaphore:
        try:
            if ext in (".jpg", ".jpeg", ".png"):
                gray = None
                try:
                    img = Image.open(filepath, formats=SAFE_IMAGE_FORMATS)
                    try:
                        img = ImageOps.exif_transpose(img)
                    except Exception:
                        pass
                    gray = img.convert("L")
                    img.close()
                    gray = _ocr_upright(gray, ocr_per_page_timeout)

                    w, h = gray.size
                    if w > MAX_OCR_WIDTH:
                        sf = MAX_OCR_WIDTH / w
                        gray = gray.resize(
                            (int(w * sf), int(h * sf)),
                            Image.Resampling.LANCZOS,
                        )
                    elif w < 800:
                        sf = 1000 / w
                        gray = gray.resize(
                            (int(w * sf), int(h * sf)),
                            Image.Resampling.LANCZOS,
                        )

                    processed = ImageOps.autocontrast(
                        gray.filter(ImageFilter.SHARPEN), cutoff=2
                    )

                    text = ""
                    try:
                        text = pytesseract.image_to_string(
                            processed, lang="tha+eng",
                            timeout=ocr_per_page_timeout,
                            config="--psm 6 --oem 1",
                        ).strip()
                    except Exception:
                        pass

                    if len(text) < 10:
                        try:
                            text_eng = pytesseract.image_to_string(
                                processed, lang="eng",
                                timeout=ocr_per_page_timeout,
                                config="--psm 6 --oem 1",
                            ).strip()
                            if len(text_eng) > len(text):
                                text = text_eng
                        except Exception:
                            pass

                    # รอบที่ 2 แบบอ่านเป็นคอลัมน์ (psm 4) — ฟอร์ม/ตารางที่ psm 6
                    # อ่านหัวกระดาษ (ชื่อ/HN) เพี้ยน มักอ่านถูกในรอบนี้
                    if len(text) >= 10:
                        try:
                            text_col = pytesseract.image_to_string(
                                processed, lang="tha+eng",
                                timeout=ocr_per_page_timeout,
                                config="--psm 4 --oem 1",
                            ).strip()
                            if text_col:
                                text = text + "\n" + text_col
                        except Exception:
                            pass

                    if len(text) >= 10:
                        try:
                            text_eng2 = pytesseract.image_to_string(
                                processed, lang="eng",
                                timeout=ocr_per_page_timeout,
                                config="--psm 6 --oem 1",
                            ).strip()
                            if text_eng2 and len(text_eng2) >= 10:
                                text = text + "\n" + text_eng2
                        except Exception:
                            pass

                    processed.close()
                    gray.close()
                    gray = None
                    logger.info("[Tesseract] Final: %d chars", len(text))
                    return text

                finally:
                    if gray is not None:
                        try:
                            gray.close()
                        except Exception:
                            pass

            if ext == ".pdf":
                if not HAS_PYMUPDF:
                    return ""

                pdf_scale = 150 / 72
                doc = pymupdf.open(filepath)
                try:
                    num_pages = min(len(doc), MAX_OCR_PAGES)
                    texts = []
                    for i in range(num_pages):
                        pix = None
                        page_img = None
                        try:
                            page = doc[i]
                            mat = pymupdf.Matrix(pdf_scale, pdf_scale)
                            pix = page.get_pixmap(matrix=mat)
                            page_img = Image.frombytes(
                                "RGB", [pix.width, pix.height], pix.samples
                            )
                            page_gray = page_img.convert("L")
                            page_img.close()
                            page_img = None
                            pix = None
                            page_text = pytesseract.image_to_string(
                                page_gray, lang="tha+eng",
                                timeout=ocr_per_page_timeout,
                                config="--psm 6 --oem 1",
                            )
                            page_gray.close()
                            if page_text.strip():
                                texts.append(page_text.strip())
                        except Exception as pg_err:
                            logger.warning(
                                "[Tesseract] PDF page %d error: %s",
                                i, type(pg_err).__name__,
                            )
                        finally:
                            if page_img is not None:
                                try:
                                    page_img.close()
                                except Exception:
                                    pass
                            pix = None
                finally:
                    doc.close()

                full_text = "\n\n".join(texts)
                logger.info(
                    "[Tesseract] PDF: %d pages, %d chars",
                    num_pages, len(full_text),
                )
                return full_text

        except RuntimeError as e:
            logger.error("Tesseract timeout: %s", type(e).__name__)
        except Exception as e:
            logger.error("Tesseract failed: %s", type(e).__name__)

    return ""


def extract_text_with_ocr(filepath: str, timeout_sec: int = 45) -> str:
    """ดึงข้อความจากรูปภาพ/PDF — ลำดับ:
      1. Google Cloud Vision API (แม่นยำ, อ่านลายมือได้)
      2. Tesseract OCR (fallback ฟรี)

    v4 — Vision API + Tesseract fallback:
      - ลอง Vision API ก่อน (อ่านลายมือไทย+อังกฤษได้ดีมาก)
      - ถ้า Vision API ไม่พร้อม/ล้มเหลว → fallback Tesseract v3
      - ไม่ส่งข้อมูลนอก Google Cloud (ใช้ GCS อยู่แล้ว)
    """
    ext = os.path.splitext(filepath)[1].lower()

    # ── ลอง Google Cloud Vision API ก่อน ──
    vision_text = ""
    try:
        if ext in (".jpg", ".jpeg", ".png"):
            vision_text = _ocr_with_google_vision(filepath)
        elif ext == ".pdf":
            vision_text = _ocr_with_google_vision_pdf(filepath)

        if vision_text and len(vision_text) >= 5:
            logger.info(
                "[OCR] Using Vision API result: %d chars", len(vision_text)
            )
            return vision_text
    except Exception as ve:
        logger.warning("[OCR] Vision API attempt failed: %s", type(ve).__name__)

    # ── Fallback: Tesseract OCR ──
    logger.info("[OCR] Falling back to Tesseract")
    return _ocr_with_tesseract(filepath, timeout_sec)


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


def _ops_line_result(ok, status=None, body=""):
    """บันทึกผลส่ง LINE ลงกระดานสถานะ (ops_status) — ไม่กระทบการส่ง"""
    try:
        from hd_module import ops_status as _ops
        _ops.line_result(ok, status, body)
    except Exception:
        pass


def _ops_allow_push() -> bool:
    """โควตา LINE รายเดือนใกล้หมด → งดข้อความทั่วไป เก็บไว้ให้แจ้งเตือนวิกฤต (ops_status.critical)"""
    try:
        from hd_module import ops_status as _ops
        if not _ops.allow_push():
            logger.warning("[PUSH] งดส่งข้อความทั่วไป — โควตา LINE เหลือ %s (สำรองไว้แจ้งเตือนวิกฤต)", _ops.quota_remaining())
            return False
    except Exception:
        pass
    return True


def push_message(user_id: str, text: str) -> bool:
    """ส่งข้อความ Push ไปยัง LINE user.

    ใช้สำหรับแจ้งเตือนผลตรวจผิดปกติ
    ไม่ log user_id หรือเนื้อหาข้อความ
    Returns True if sent successfully, False otherwise.
    """
    if IS_TESTING and not IS_TESTING_DOCS:
        logger.info("TESTING MODE: ไม่ส่ง push message")
        return False
    if not _ops_allow_push():
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
        _ops_line_result(True)
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
        _ops_line_result(False, status, resp_body or type(e).__name__)
        return False


# ── V5.0: wire push_message (นิยามหลัง init) + ลงทะเบียน routes/UI ──
if v5 is not None:
    try:
        v5._deps["push_message"] = push_message
        _v5i._register_action_handlers(v5, v5._deps)
        _v5i.register_routes(app, _admin_api_auth, v5)
    except Exception as _v5_err:
        logger.warning("V5.0 routes: %s: %s", type(_v5_err).__name__, _v5_err)


def push_messages(user_id: str, messages: list[dict]) -> bool:
    """ส่ง push messages หลายรายการไปยัง LINE user (รองรับ text, flex, image).

    ไม่ log user_id หรือเนื้อหาข้อความ
    Returns True if sent successfully, False otherwise.
    """
    if IS_TESTING and not IS_TESTING_DOCS:
        return False
    if not _ops_allow_push():
        return False
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.error("[PUSH] ไม่มี LINE_CHANNEL_ACCESS_TOKEN")
        return False
    if not user_id or not messages:
        return False
    url = "https://api.line.me/v2/bot/message/push"
    request_body = json.dumps(
        {"to": user_id, "messages": messages[:5]},
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
        logger.info(
            "Push messages สำเร็จ (%d msgs, status=%d)",
            len(messages), resp.status_code,
        )
        _ops_line_result(True)
        return True
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "-")
        _body = ""
        try:
            _body = getattr(e, "response", None).text[:200]
        except Exception:
            pass
        logger.error(
            "[PUSH] Push messages ไม่สำเร็จ: %s (status=%s) body=%s",
            type(e).__name__, status, _body,
        )
        _ops_line_result(False, status, _body or type(e).__name__)
        return False


# ─── HD Alert Push + Vital Recorder (init after push_message def) ──
app.config["LINE_PUSH_FN"] = push_message  # For HD session summary LINE push
app.config["LINE_PUSH_MESSAGES_FN"] = push_messages  # Flex / multi-message push (patient post-HD summary)


def _line_group_member_name(group_id: str, user_id: str) -> str:
    """ชื่อที่แสดงของสมาชิกในกลุ่ม (สำหรับช่อง 'ผู้ตรวจ'); "" ถ้าดึงไม่ได้."""
    if not group_id or not user_id:
        return ""
    try:
        r = http_requests.get(f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}",
                              headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}, timeout=8)
        if r.ok:
            return (r.json().get("displayName") or "")[:100]
    except Exception:
        pass
    return ""


def _nurse_group_id() -> str:
    try:
        from hd_module.blueprint import get_setting_value as _gsv
        return (_gsv("nurse_group_id", "") or "").strip()
    except Exception:
        return ""


def _water_store_group_image(msg_id: str, user_hash: str):
    """รูปที่ส่งในกลุ่มพยาบาล → เก็บดิบใน GCS water-check/_inbox (ไม่เข้า OCR/เอกสารผู้ป่วย)."""
    try:
        from hd_module import water_check as wc
        import psycopg2 as _pg
        data = download_line_content(msg_id)
        if not data:
            return
        day = (datetime.now(timezone.utc) + timedelta(hours=7)).date()
        path = wc.inbox_path(day, user_hash, msg_id)
        bucket = getattr(storage, "_bucket", None)
        if bucket is None:
            return
        bucket.blob(path).upload_from_string(data, content_type="image/jpeg")
        dbu = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        c = _pg.connect(dbu, connect_timeout=5)
        try:
            wc.add_inbox(c, user_hash, msg_id, path)
        finally:
            c.close()
    except Exception as e:
        logger.warning(f"water inbox: {type(e).__name__}: {str(e)[:80]}")


def _water_handle_command(parsed: dict, text: str, user_hash: str, line_user_id: str, group_id: str) -> str:
    """บันทึกผลตรวจน้ำ RO จากข้อความในกลุ่ม → ข้อความตอบกลับ."""
    from hd_module import water_check as wc
    from hd_module.blueprint import get_setting_value as _gsv
    import psycopg2 as _pg
    if parsed.get("help"):
        return wc.HELP
    lim = wc.limits_from(_gsv)
    ev = wc.evaluate(parsed, lim)
    tester = _line_group_member_name(group_id, line_user_id)
    dbu = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    c = _pg.connect(dbu, connect_timeout=5)
    try:
        rec = wc.save_record(c, parsed, ev, tester_name=tester, tester_hash=user_hash, raw_text=text,
                             sample_point=parsed.get("point") or lim.get("water_sample_point"))
        drive_up = None
        try:
            from hd_module.daily_records import drive_upload_fn
            drive_up = drive_upload_fn()
        except Exception:
            pass
        stored, drive_out = wc.archive(c, storage, rec, parsed, ev, drive_upload=drive_up, tester_name=tester)
    finally:
        c.close()
    try:
        from hd_module.blueprint import log_action as _la
        with app.app_context():
            _la(user_id=None, action="water.check_recorded", resource_type="water", resource_id=rec["id"],
                new_values={"status": ev["status"], "fails": ev["fails"], "photos": len(rec["photos"]), "tester": tester}, ip_address="")
    except Exception:
        pass
    msg = wc.reply_text(rec, ev, tester, len(rec["photos"]), bool(drive_out))
    if ev["status"] == "fail":
        try:
            ids = _gsv("alert_recipients", "") or _gsv("physician_line_id", "")
            base = (os.environ.get("RENDER_EXTERNAL_URL") or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
            for uid in [x.strip() for x in ids.split(",") if x.strip()]:
                push_message(uid, "🚨 ตรวจน้ำ RO ไม่ผ่านเกณฑ์\n" + msg + f"\n{base}/hd/water-ro")
        except Exception as e:
            logger.warning(f"water fail push: {e}")
    return msg


def _hd_patient_line_id(hn: str) -> str:
    """HN คลินิก → LINE userId ของผู้ป่วยที่ผูกบัญชีไว้ (อ่านอย่างเดียว; "" ถ้าไม่ได้ผูก).
    ใช้เส้นทางเดิม: HNRegistry (clinic_hn → owner_hash) → _enrolled_ids (owner_hash → userId)."""
    try:
        rec = hn_registry.get_by_clinic_hn(hn or "")
        if not rec:
            return ""
        from alert_delivery import _lookup_enrolled_line_id
        return _lookup_enrolled_line_id(storage, rec.owner_hash) or ""
    except Exception as e:
        logger.warning("patient line id lookup: %s", type(e).__name__)
        return ""


app.config["PATIENT_LINE_ID_FN"] = _hd_patient_line_id
app.config["STORAGE_OBJ"] = storage        # HD daily-record export / backup reuse the same GCS auth
app.config["DOC_INDEX"] = doc_index        # HD lab sync reads lab documents → bed cards

try:
    from hd_module.alert_push import HdAlertPushService
    _hd_db = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    def _critical_push(uid, text):
        """แจ้งเตือนวิกฤต (BP ตก ฯลฯ) — ไม่ถูกงดเมื่อโควตา LINE ใกล้หมด"""
        from hd_module import ops_status as _ops
        with _ops.critical():
            return push_message(uid, text)
    hd_alert_push = HdAlertPushService(push_fn=_critical_push, db_url=_hd_db)
    logger.info("HD Alert Push: initialized (%d recipients)", hd_alert_push.recipient_count)
except Exception as _hd_push_err:
    logger.warning("HD Alert Push: init failed — %s", _hd_push_err)

try:
    from hd_module.vital_recorder import VitalRecorder
    _hd_db = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    hd_vital_recorder = VitalRecorder(db_url=_hd_db, alert_push_service=hd_alert_push)
    app.config["HD_VITAL_RECORDER"] = hd_vital_recorder
    logger.info("HD Vital Recorder: initialized")
except Exception as _hd_vr_err:
    logger.warning("HD Vital Recorder: init failed — %s", _hd_vr_err)


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


def reply_with_quick_reply(reply_token: str, text: str,
                           quick_items: list[dict]):
    """ส่งข้อความ + Quick Reply buttons ตอบกลับ LINE.

    quick_items: [{"label": "สรุป", "text": "สรุป 69453"}, ...]
    """
    if IS_TESTING and not IS_TESTING_DOCS:
        return
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return
    qr_items = []
    for item in quick_items[:13]:  # LINE max 13 items
        qr_items.append({
            "type": "action",
            "action": {
                "type": "message",
                "label": item["label"][:20],  # LINE max 20 chars
                "text": item["text"][:300],
            },
        })
    msg = {
        "type": "text",
        "text": text[:5000],
        "quickReply": {"items": qr_items},
    }
    url = "https://api.line.me/v2/bot/message/reply"
    body = json.dumps(
        {"replyToken": reply_token, "messages": [msg]},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        resp = http_requests.post(
            url,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=body, timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "-")
        logger.error("reply_with_quick_reply: %s (status=%s)",
                     type(e).__name__, status)


def reply_flex_with_quick_reply(reply_token: str, flex_msg: dict,
                                 quick_items: list[dict]):
    """ส่ง Flex Message + Quick Reply buttons."""
    if IS_TESTING and not IS_TESTING_DOCS:
        return
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return
    qr_items = []
    for item in quick_items[:13]:
        qr_items.append({
            "type": "action",
            "action": {
                "type": "message",
                "label": item["label"][:20],
                "text": item["text"][:300],
            },
        })
    flex_msg["quickReply"] = {"items": qr_items}
    url = "https://api.line.me/v2/bot/message/reply"
    body = json.dumps(
        {"replyToken": reply_token, "messages": [flex_msg]},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        resp = http_requests.post(
            url,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=body, timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "-")
        logger.error("reply_flex_with_quick_reply: %s (status=%s)",
                     type(e).__name__, status)


def _patient_quick_reply_items(hn: str) -> list[dict]:
    """สร้าง Quick Reply items สำหรับคำสั่งที่เกี่ยวกับ HN นี้"""
    return [
        {"label": "📋 สรุป", "text": f"สรุป {hn}"},
        {"label": "🧠 วิเคราะห์", "text": f"วิเคราะห์ {hn}"},
        {"label": "📊 เปรียบเทียบ", "text": f"เปรียบเทียบ {hn}"},
        {"label": "💉 ติดตามHb", "text": f"ติดตามHb {hn}"},
        {"label": "🏥 eGFR", "text": f"ติดตามeGFR {hn}"},
        {"label": "💊 ประวัติยา", "text": f"ประวัติการรักษา {hn}"},
        {"label": "📄 คำสั่งรักษา", "text": f"คำสั่งการรักษา {hn}"},
    ]


def _build_patient_card_flex(hn: str, patient, hb_val=None,
                              esa_info="", dialysis_info="") -> dict:
    """สร้าง Flex Message card สรุปข้อมูลผู้ป่วย (ข้อ 5: ผป command)"""
    body_contents = [
        {"type": "text", "text": f"👤 {patient.patient_name}",
         "weight": "bold", "size": "lg", "wrap": True},
        {"type": "text", "text": f"🔢 HN: {hn}",
         "size": "sm", "color": "#666666"},
        {"type": "separator", "margin": "md"},
    ]

    # Info rows
    info_rows = []
    if hb_val and hb_val > 0:
        hb_status = "✅" if 10 <= hb_val <= 12 else ("⚠️" if hb_val < 10 else "🔺")
        info_rows.append(f"🩸 Hb: {hb_val:.1f} g/dL {hb_status}")
    else:
        info_rows.append("🩸 Hb: ไม่มีข้อมูล")

    if esa_info:
        info_rows.append(f"💊 ESA: {esa_info}")

    if dialysis_info:
        info_rows.append(f"🏥 {dialysis_info}")

    if hasattr(patient, 'vascular_access') and patient.vascular_access:
        info_rows.append(f"🔴 Vascular: {patient.vascular_access}")

    for row in info_rows:
        body_contents.append({
            "type": "text", "text": row,
            "size": "sm", "margin": "sm", "wrap": True,
        })

    return {
        "type": "flex",
        "altText": f"ข้อมูลผู้ป่วย HN {hn}",
        "contents": {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": body_contents,
                "paddingAll": "16px",
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "action": {"type": "message", "label": "🧠 วิเคราะห์แล็บ",
                                   "text": f"วิเคราะห์ {hn}"},
                        "style": "primary", "height": "sm",
                        "color": "#2E7D32",
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "spacing": "sm",
                        "contents": [
                            {
                                "type": "button",
                                "action": {"type": "message",
                                           "label": "📊 เปรียบเทียบ",
                                           "text": f"เปรียบเทียบ {hn}"},
                                "style": "secondary", "height": "sm",
                            },
                            {
                                "type": "button",
                                "action": {"type": "message",
                                           "label": "💉 Hb trend",
                                           "text": f"ติดตามHb {hn}"},
                                "style": "secondary", "height": "sm",
                            },
                        ],
                    },
                ],
                "paddingAll": "12px",
            },
        },
    }


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


def _fuzzy_mark_hits(marks, text_lower: str, already=()) -> list[str]:
    """เครื่องหมายฟอร์ม (อังกฤษ ≥ 9 ตัวอักษร) ที่ OCR สะกดเพี้ยนเล็กน้อย
    เช่น "nursing diagnosls" / "pre-dialysis assesrnent" — เทียบทีละกลุ่มคำ
    จำนวนคำเท่ากัน ต้องคล้าย ≥ 0.85 · คำสั้นไม่ใช้ (เพี้ยนแล้วชนคำอื่นง่าย)"""
    import difflib
    words = re.findall(r"[a-z0-9/%.\-]+", text_lower or "")
    if not words:
        return []
    out: list[str] = []
    pool = [a for a in already if a]
    grams_by_n: dict[int, set] = {}

    def _same_concept(mk: str) -> bool:
        # คำสะกดอีกแบบของเครื่องหมายที่นับไปแล้ว (assesment/assessment) ไม่นับซ้ำ
        return any(len(a) >= 6 and (a in mk or mk in a
                                    or difflib.SequenceMatcher(None, a, mk).ratio() >= 0.8)
                   for a in pool)

    for mk in marks:
        if mk in already or len(mk) < 9 or not mk.isascii() or _same_concept(mk):
            continue
        n = len(mk.split())
        grams = grams_by_n.get(n)
        if grams is None:
            grams = {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}
            grams_by_n[n] = grams
        for g in grams:
            if abs(len(g) - len(mk)) > 3:
                continue
            sm = difflib.SequenceMatcher(None, mk, g)
            if sm.real_quick_ratio() >= 0.85 and sm.quick_ratio() >= 0.85 and sm.ratio() >= 0.85:
                out.append(mk)
                pool.append(mk)
                break
    return out


def classify_with_keywords(filepath: str, override_text: str | None = None) -> dict:
    """Fallback: จำแนกด้วย keyword matching + OCR.

    override_text: ถ้ามี ใช้ข้อความนี้แทน OCR จากไฟล์
    (ใช้สำหรับ multi-page merge ที่ OCR รวมไว้แล้ว)

    Returns dict ที่มี:
      - category, reason, ...
      - _ocr_text: ข้อความ OCR ที่ใช้จำแนก (ให้ caller นำไปใช้ต่อ
        เช่น HN extraction โดยไม่ต้อง OCR ซ้ำ)
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
                "_ocr_text": "",
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
                    "_ocr_text": "",
                }

    elif ext in (".txt", ".csv"):
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            return {"category": "review", "reason": "อ่านไฟล์ไม่ได้", "_ocr_text": ""}
    else:
        return {"category": "review", "reason": f"ไม่รองรับนามสกุล {ext}", "_ocr_text": ""}

    content_lower = content.lower()

    # Log เฉพาะจำนวนตัวอักษร — ห้ามบันทึกเนื้อหาเอกสารลง log
    logger.info(f"OCR extracted: {len(content.strip())} chars")

    def _kw_in_text(kw_lower: str, text_lower: str) -> bool:
        """ตรวจว่า keyword อยู่ใน text — คำสั้น (≤3 chars, ASCII)
        ใช้ word-boundary match เพื่อลด false positive เช่น
        "HD" ไม่ควร match "ทุกHD" (Thai prefix)"""
        if len(kw_lower) <= 3 and kw_lower.isascii():
            # ใช้ Unicode word boundary (\b ไม่พอ เพราะ \b ไม่นับ Thai)
            # ตรวจว่าตัวอักษรก่อน/หลัง keyword ไม่ใช่ letter (ทุกภาษา)
            idx = 0
            while True:
                idx = text_lower.find(kw_lower, idx)
                if idx == -1:
                    return False
                # ตรวจ char ก่อนหน้า
                if idx > 0 and text_lower[idx - 1].isalpha():
                    idx += 1
                    continue
                # ตรวจ char หลัง
                end = idx + len(kw_lower)
                if end < len(text_lower) and text_lower[end].isalpha():
                    idx += 1
                    continue
                return True
            return False
        return kw_lower in text_lower


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
             # หมายเหตุ: "โรงพยาบาลระนอง" ย้ายออกจาก rx แล้ว เพราะเป็นชื่อ
             # โรงพยาบาลซึ่งปรากฏบนเอกสารทุกประเภท (ใบแล็บ ใบยา ใบฟอก ฯลฯ)
             # ทำให้ใบแล็บถูกจัดเป็น rx เมื่อ OCR อ่านได้แค่หัวกระดาษ
             # ชื่อยาสามัญที่พบบ่อยในคลินิกไต
             "losartan", "enalapril", "amlodipine",
             "atorvastatin", "omeprazole", "aspirin",
             "carvedilol", "calcium carbonate", "folic acid",
             "ferrous", "fumarate", "calcitriol",
             "sodium bicarbonate", "venofer", "epo",
             "erythropoietin", "darbepoetin",
             "hold", "ครั้ง/สัปดาห์",
             # ── Medication Order Form ──
             "dose", "frequency", "route", "regimen",
             "order form", "oral", "p.c.", "h.s.",
             "order for one day", "order for continuation",
             "manidipine", "epiao", "bicarbo", "iron venofe",
             "calcium carbon", "medication",
             # ── Medication Record (ใบบันทึกยาผู้ป่วยไตเทียม) ──
             "medication record", "medication list",
             "แพทย์ผู้สั่ง", "วันที่สั่ง",
             # ── HD Prescription Form (ใบสั่งยาฟอกเลือด) ──
             # OCR อ่านลายมือ/ภาพเบลอ — เพิ่ม partial matches
             "hemodialysis", "dialyzer", "dry weight",
             "anticoagulant", "heparin", "allergies",
             "progress note", "physician",
             "bfr", "dfr", "qb(ml/min)", "nacl",
             "hco3", "k(meq/l)", "ca(meq/l)",
             "variation", "program",
             "1x1", "2x2", "3x1", "4x2",
             "p.o.", "i.v.", "s.c.",
             "epoetin", "darbepoetin",
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
             # ── ฟอร์มใบแล็บโรงพยาบาล ──
             "ใบรายงานผลการตรวจ", "ใบรายงานผล", "รายงานผลการตรวจ",
             "request form", "request by", "request date",
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
             # ── OCR อ่านผิดบ่อย — เพิ่ม variant ──
             "hemodlalysis", "hemodia", "hemod", "work sheet",
             "hemodi", "dialysi", "dialyse",
             "patient name", "age", "dx ckd",
             "ckd stage", "pre weight", "post weight",
             "weight gain", "needle", "gauge",
             ],
            1,  # min_match=1 เพราะ keyword เดียวก็ชัดพอ
        ),
    }

    # ── Pre-check: สลิปโอนเงิน / ธนาคาร ──
    # ── Pre-check: Hemodialysis WORK SHEET (ใบฟอกเลือด/flowsheet ของพยาบาล) ──
    # ใบนี้มีหัวข้อ "Hemodialysis Prescription" และ "Medication / Route" อยู่ในตัว
    # ทำให้ pre-check ใบสั่งยา HD ด้านล่างจับเป็น rx ผิด (13 พ.ย. 2026, HN 67360)
    # จุดต่างที่ใบสั่งยาไม่มี: ส่วนประเมินพยาบาล/flowsheet/ภาวะแทรกซ้อน/จำหน่าย
    _worksheet_marks = [
        "work sheet", "worksheet", "hemodialysis work sheet",
        "pre-dialysis assesment", "pre-dialysis assessment", "pre-dialysis",
        "nursing diagnosis", "nursing care plan", "expective outcome", "expected outcome",
        "pre hd note", "post hd note", "nurse start", "nurse off",
        "net uf balance", "total repletement", "technical complication",
        "nursing managment", "nursing management", "health edecation", "health education",
        "discharge by", "hd no", "sleep disturbance", "jvp engorged", "prolong bleeding",
        "conduct", "tmp", "vp", "ufr",
        # ── 18 พ.ย. 2026 (HN 68408): ช่องในตัวฟอร์มที่อ่านได้แม้รูปตัดหัวกระดาษ ──
        # เลือกเฉพาะช่องของใบฟอกเลือด ไม่ใช้คำตรวจร่างกาย (dyspnea/thrill/bruit)
        # ที่ใบตรวจรักษา OPD ก็มี
        "pre bw", "post bw", "dry bw", "wt gain", "wt lose", "last wt",
        "target uf", "meal/drink", "extra fluid", "time start", "time off",
        "blood leak", "clot dialyzer", "hypotenstion", "monitor v/s",
        "bp ก่อนกลับ", "start hd",
    ]
    _ws_hits = [kw for kw in _worksheet_marks if _kw_in_text(kw, content_lower)]
    if len(_ws_hits) < 3:
        # รูปถ่ายเบลอ/เอียง → OCR สะกดเพี้ยน 1–2 ตัวอักษร ("Nursing diagnosls")
        # จับแบบใกล้เคียงเฉพาะคำยาว ≥ 9 ตัวอักษร (คำสั้นเพี้ยนแล้วชนคำอื่นง่าย)
        _ws_hits += _fuzzy_mark_hits(_worksheet_marks, content_lower, set(_ws_hits))

    # ฟอร์มสั่งยา/ใบตรวจรักษา — ถ้ามีหลายจุด ห้ามลัดเป็นใบฟอกเลือด
    _order_marks = [
        "order for one day", "order for continuation", "medication order",
        "order form", "medication record", "แพทย์ผู้สั่ง", "วันที่สั่ง",
        "regimen", "frequency", "ใบสั่งยา", "ใบตรวจรักษา", "diag text",
        "visit note", "ผู้ตรวจรักษา",
    ]
    _order_hits = [k for k in _order_marks if k in content_lower]

    # ชื่อยา (ใช้ทั้งที่นี่และ pre-check ใบสั่งยาด้านล่าง)
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
        # ── ยาที่พบบ่อยในคลินิกไต เพิ่มเติม ──
        "manidipine", "epiao", "bicarbo", "iron venofe",
        "calcium carbon",
        # ── สะกดผิดที่พบบ่อยจาก OCR หรือฟอร์มจริง ──
        "atrovastatin",  # = atorvastatin
    ]
    _drug_hits = [d for d in _rx_drug_names if d.lower() in content_lower]

    _ws_ok = len(_ws_hits) >= 3 and (
        len(_order_hits) < 2 or len(_ws_hits) >= 2 * len(_order_hits) + 2
    )
    if _ws_ok:
        logger.info("  pre-check: HD work sheet detected (%d marks) → dialysis", len(_ws_hits))
        _res = {
            "category": "dialysis",
            "reason": f"HD work sheet pre-check ({len(_ws_hits)} marks)",
            "confidence": min(len(_ws_hits) / 6.0, 1.0),
            "_ocr_text": content,
        }
        if _order_hits or len(_drug_hits) >= 3:
            _res["ambiguous_with"] = "rx"
        return _res

    # ── Pre-check: ใบสั่งยาฟอกเลือด (HD Prescription) ──
    # ใบยา HD มีทั้ง dialysis + medication keywords → บังคับ rx
    # ต้องมี rx keyword ≥2 (เดิม ≥1 — คำเดียวอย่าง "medication"/"route"
    # โผล่บนใบฟอกเลือดทุกใบ ทำให้จัดผิด)
    _hd_rx_strong = ["hemodialysis", "dialyzer", "dry weight", "anticoagulant",
                     "heparin", "dialysate", "bfr", "dfr"]
    _rx_strong = ["medication", "dose", "frequency", "route", "regimen",
                  "oral", "prescription", "mg", "tab", "cap", "ใบสั่งยา"]
    _hd_hits = sum(1 for kw in _hd_rx_strong if kw.lower() in content_lower)
    _rx_hits = sum(1 for kw in _rx_strong if _kw_in_text(kw.lower(), content_lower))
    # 18 พ.ย. 2026: ใบฟอกเลือดที่ OCR อ่านได้ไม่ครบ (เครื่องหมายใบฟอก 2 จุด)
    # ก็มี Heparin + Medication + Route ในตัว → เคยถูกจัดเป็นใบยา
    # ถ้าไม่มีหัวฟอร์มสั่งยาและชื่อยาไม่ถึง 3 ตัว → ไม่ลัดเป็นใบยาที่นี่
    _ws_partial = (len(_ws_hits) >= 2 and not _order_hits and len(_drug_hits) < 3)
    if _hd_hits >= 1 and _rx_hits >= 2 and not _ws_partial:
        logger.info("  pre-check: HD prescription detected (%d HD + %d rx hits) → rx",
                     _hd_hits, _rx_hits)
        _res = {
            "category": "rx",
            "reason": f"HD prescription pre-check ({_hd_hits} HD + {_rx_hits} rx hits)",
            "confidence": min((_hd_hits + _rx_hits) / 4.0, 1.0),
            "_ocr_text": content,
        }
        if _ws_hits:
            _res["ambiguous_with"] = "dialysis"
        return _res

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
            "_ocr_text": content,
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
            "_ocr_text": content,
        }

    # ── Pre-check: ใบตรวจรักษาผู้ป่วย รพ. (OPD visit sheet = ใบยา) ──
    # *** ต้องอยู่ก่อน lab pre-check ***
    # 15 พ.ย. 2026 (HN 00139): ใบนี้มีช่อง "ผลการตรวจทางห้องปฏิบัติการ" และรายการยา
    # SODIUM BICARBONATE / Calcium Carbonate → lab pre-check นับ sodium+calcium เป็นผลแล็บ
    # แล้วจัดเป็นใบแล็บผิดแฟ้ม หัวฟอร์ม/ช่องเฉพาะของใบ OPD ชี้ชัดว่าเป็นใบยา
    _opd_strong = ["ใบตรวจรักษา", "diag text", "visit note", "pe text", "mh/pe", "ผู้ตรวจรักษา"]
    _opd_support = ["สิทธิการรักษา", "สิทธิ์การรักษา", "ประวัติการแพ้ยา", "อาการสำคัญ",
                    "ประวัติการเจ็บป่วย", "การตรวจร่างกาย", "cc :", "cc:", "hpi", "pmh", "รายการยา"]
    _opd_s = [k for k in _opd_strong if k in content_lower]
    _opd_t = len(_opd_s) + sum(1 for k in _opd_support if k in content_lower)
    if len(_opd_s) >= 2 or (len(_opd_s) >= 1 and _opd_t >= 3):
        logger.info("  pre-check: OPD visit sheet (%d strong, %d total) → rx", len(_opd_s), _opd_t)
        return {
            "category": "rx",
            "reason": f"OPD visit sheet pre-check ({len(_opd_s)} strong, {_opd_t} total)",
            "confidence": min(_opd_t / 6.0, 1.0),
            "_ocr_text": content,
        }

    # ── Pre-check: Medication Record ของคลินิก (= ใบยา) ──
    # 16 พ.ย. 2026 (HN 67398): หัวฟอร์ม "Medication Record หน่วยไตเทียมระนอง รักษ์ไต"
    # มีคำว่า "ไตเทียม" → HD pre-check จัดเป็นฟอกเลือด เมื่อ OCR อ่านชื่อยาได้ไม่ถึง 3 ตัว
    _mr_cols = [c for c in ("dose", "frequency", "route", "regimen") if _kw_in_text(c, content_lower)]
    if "medication record" in content_lower or ("medication" in content_lower and len(_mr_cols) >= 3):
        logger.info("  pre-check: Medication Record form (%d columns) → rx", len(_mr_cols))
        _res = {
            "category": "rx",
            "reason": f"Medication Record pre-check ({len(_mr_cols)} columns)",
            "confidence": 0.9,
            "_ocr_text": content,
        }
        if len(_ws_hits) >= 2:
            _res["ambiguous_with"] = "dialysis"
        return _res

    # ── Pre-check: ใบรายงานผลตรวจทางห้องปฏิบัติการ (Lab Report) ──
    # *** ต้องอยู่ก่อน rx pre-check ***
    # ถ้าพบชื่อฟอร์มแล็บ + ผลตรวจ lab ≥2 รายการ → บังคับ lab
    # ป้องกัน "mg" (หน่วยผลแล็บ) จากการดึงเอกสารเข้า rx
    # ป้องกันใบแล็บโรงพยาบาลที่ OCR อ่านได้แค่หัวกระดาษถูกจัดผิดประเภท
    _lab_form_kws = [
        "ใบรายงานผลการตรวจ", "ใบรายงานผล", "ห้องปฏิบัติการ",
        "laboratory", "lab report", "ผลตรวจ", "ผลเลือด",
        "ผลแล็บ", "ผลlab", "ผล lab",
        "request no", "collection date", "specimen",
        "reference range", "normal range",
    ]
    _lab_result_kws = [
        "creatinine", "BUN", "eGFR", "hemoglobin", "hematocrit",
        "WBC", "RBC", "platelet", "potassium", "sodium",
        "albumin", "glucose", "cholesterol", "HbA1c",
        "uric acid", "calcium", "phosphorus",
        "mg/dL", "mg/dl", "mmol", "umol", "g/dl", "g/dL",
        "CBC", "result", "abnormal",
    ]
    _lab_form_hits = [k for k in _lab_form_kws if k.lower() in content_lower]
    # ชื่อยาที่มีชื่อสารแล็บอยู่ในตัว (Sodium bicarbonate, Calcium carbonate …)
    # ไม่นับเป็นผลแล็บ — ตัดออกก่อนนับ
    _lab_text = content_lower
    for _dn in ("sodium bicarbonate", "calcium carbonate", "calcium acetate",
                "calcium carbon", "calcium gluconate", "potassium chloride",
                "sodium chloride", "albumin 20%", "albumin 25%"):
        _lab_text = _lab_text.replace(_dn, " ")
    _lab_result_hits = [k for k in _lab_result_kws if k.lower() in _lab_text]
    if len(_lab_form_hits) >= 1 and len(_lab_result_hits) >= 2:
        logger.info(
            "  pre-check: lab report detected "
            "(%d form kws, %d result kws) → lab",
            len(_lab_form_hits), len(_lab_result_hits),
        )
        return {
            "category": "lab",
            "reason": (
                f"lab report pre-check "
                f"({len(_lab_form_hits)} form, {len(_lab_result_hits)} results)"
            ),
            "confidence": min(
                (len(_lab_form_hits) + len(_lab_result_hits)) / 6.0, 1.0
            ),
            "_ocr_text": content,
        }

    # ── Pre-check: ใบสั่งยา / ใบตรวจรักษาผู้ป่วย / Medication Order ──
    # *** ต้องอยู่ก่อน HD pre-check ***
    # ถ้าพบชื่อยา ≥3 ตัว + keyword ทางการแพทย์ → บังคับ rx
    # ป้องกัน "บาท" ท้ายใบยาจากการดึงเอกสารเข้า bills
    # ป้องกัน Medication Order Form ที่มี dialysate/heparin ถูกจัดเป็น dialysis
    # (_rx_drug_names / _drug_hits คำนวณไว้แล้วด้านบน — ก่อน pre-check ใบฟอกเลือด)
    _rx_medical_kws = [
        "mg", "mcg", "tab", "cap", "เม็ด", "หลังอาหาร",
        "ก่อนอาหาร", "ก่อนนอน", "วันละ", "ครั้งละ",
        "hold", "diagnosis", "diag text", "visit note",
        "ใบตรวจรักษา", "mh/pe", "cc:",
        # ── Medication Order Form columns & abbreviations ──
        "dose", "frequency", "route", "regimen",
        "oral", "p.c.", "h.s.", "order form",
        "order for one day", "order for continuation",
        "1x1", "1x2", "1 x 1", "1 x 2", "3x1", "2x1",
        "medication", "medication record", "medication list",
        "แพทย์ผู้สั่ง", "วันที่สั่ง",
    ]
    _rx_med_hits = [k for k in _rx_medical_kws if k.lower() in content_lower]
    if len(_drug_hits) >= 3 and len(_rx_med_hits) >= 1:
        logger.info(
            f"  pre-check: prescription detected "
            f"({len(_drug_hits)} drugs, {len(_rx_med_hits)} medical kws) → rx"
        )
        _res = {
            "category": "rx",
            "reason": (
                f"prescription pre-check "
                f"({len(_drug_hits)} drugs, {len(_rx_med_hits)} medical kws)"
            ),
            "confidence": min((len(_drug_hits) + len(_rx_med_hits)) / 8.0, 1.0),
            "_ocr_text": content,
        }
        if _ws_hits:
            _res["ambiguous_with"] = "dialysis"
        return _res

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
    if len(_hd_hits) >= 1:
        logger.info(
            f"  pre-check: hemodialysis detected "
            f"({len(_hd_hits)} strong HD kws) → dialysis"
        )
        _res = {
            "category": "dialysis",
            "reason": f"hemodialysis pre-check ({len(_hd_hits)} strong HD hits)",
            "confidence": min(len(_hd_hits) / 4.0, 1.0),
            "_ocr_text": content,
        }
        if _ws_partial or _order_hits:
            # ข้อความมีทั้งลักษณะใบฟอกเลือดและใบยา — ระบบไม่แน่ใจ ให้คนยืนยัน
            _res["ambiguous_with"] = "rx"
        return _res

    # ── keyword matching helper ──
    # คำสั้น ≤3 ตัวอักษร (เช่น "HD", "UF", "QB") ต้อง match แบบ
    # word boundary เพื่อไม่ให้ "HD" ใน "ทุกHD" นับเป็น dialysis hit
    # (ยกเว้นคำภาษาไทยที่ไม่มี word boundary ธรรมชาติ)
    import unicodedata as _ucd

    def _is_thai(ch: str) -> bool:
        try:
            return "THAI" in _ucd.name(ch, "")
        except Exception:
            return False

    best, best_score = "review", 0
    all_matches = {}
    for cat, (kws, min_match) in rules.items():
        matched = [kw for kw in kws if _kw_in_text(kw.lower(), content_lower)]
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
    # ใบยา ↔ ใบฟอกเลือด ที่ตัดสินด้วยการนับคำทั่วไป — แจ้งให้คนยืนยัน
    elif best == "rx" and _ws_hits:
        result["ambiguous_with"] = "dialysis"
    elif best == "dialysis" and (_order_hits or len(_drug_hits) >= 2):
        result["ambiguous_with"] = "rx"

    result["_ocr_text"] = content
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


# จำนวนเอกสารที่ประมวลผล (OCR/จำแนก/อัปโหลด) พร้อมกันได้ต่อ worker
# 2026-11-02 17:35 instance OOM (>512MB) หลังชุด 2 รูป: 2 workers × 2 ฉบับ
# = 4 ฉบับพร้อมกัน + OCR prepass → ลดเป็น 1 ฉบับ/worker (2 ทั้งเครื่อง)
# ชุด 8 รูปช้าลง ~1 นาที แต่ไม่มีทางถูกฆ่ากลางคัน (env ทับได้)
_PROCESS_MAX_CONCURRENT = max(1, int(os.getenv("PROCESS_MAX_CONCURRENT", "1")))
_PROCESS_SEMAPHORE = threading.BoundedSemaphore(_PROCESS_MAX_CONCURRENT)


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
    batch_id: str | None = None,
    page_sha256s: list[str] | None = None,
    batch_patient_folder: str = "",
    batch_index: int = 0,
):
    """ประมวลผลไฟล์ในเธรดแยก.

    batch_id: รหัสชุดที่ส่งมาพร้อมกัน (multi-image แยกไฟล์) — เก็บใน metadata
    batch_patient_folder: แฟ้มผู้ป่วย (จากทะเบียน) ที่หน้าอื่นในชุดเดียวกัน
        จับคู่ได้ตรงกันเพียงรายเดียว — ใช้เมื่อหน้านี้ OCR อ่าน HN ไม่ได้
        หรืออ่านได้แต่ไม่มีในทะเบียน (ส่งพร้อมกัน = ผู้ป่วยคนเดียวกัน)
    page_sha256s: sha256 ของแต่ละหน้าที่ถูกรวมเป็น PDF — ใช้กันซ้ำถาวร

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
        # จำกัดจำนวนเอกสารที่ OCR/ประมวลผลพร้อมกันต่อ worker — ส่งหลายรูป
        # พร้อมกันเคยทำให้หน่วยความจำพุ่งเกิน 512MB แล้ว instance ถูกฆ่า
        # (exit 137) ก่อนตอบกลับ; รายการที่เกินจะรอคิว ไม่ถูกทิ้ง
        with _PROCESS_SEMAPHORE:
            _run_inner()

    def _run_inner():
        try:
            is_valid, reason = validate_file(filepath)
            if not is_valid:
                logger.warning(f"ไฟล์ไม่ผ่านการตรวจสอบ: {reason}")
                result = {"category": "review", "reason": reason}
                # DLQ: validation failed
                if dead_letter_queue:
                    dead_letter_queue.add(
                        _doc_id, "validation_failed",
                        reason[:100],
                    )
            elif merged_ocr_text:
                # มีข้อความ OCR มาแล้ว (multi-image batch) → จำแนกจากข้อความนั้น
                # โดยตรง ไม่ OCR ซ้ำ — classify_with_claude ไม่รับ override และ
                # External AI ถูกล็อกที่ระดับโค้ดอยู่แล้ว ผลลัพธ์จึงเท่ากัน
                result = classify_with_keywords(filepath, override_text=merged_ocr_text)
            elif ENABLE_AI_CLASSIFY:
                result = classify_with_claude(filepath)
            else:
                result = classify_with_keywords(filepath)

            category = result.get("category", "review")

            # ── Confidence scoring + review_status ──
            _confidence = result.get("confidence", 0.0)
            try:
                _confidence = float(_confidence)
            except (TypeError, ValueError):
                _confidence = 0.0
            _review_status = (
                "auto_approved" if _confidence >= 0.7
                else "needs_review"
            )

            # DLQ: classification ไม่ชัดเจน → review
            if category == "review" and dead_letter_queue:
                dead_letter_queue.add(
                    _doc_id, "classify_review",
                    result.get("reason", "ไม่สามารถจำแนกได้")[:100],
                    category="review",
                )
            # DLQ: confidence ต่ำ (ไม่ใช่ review) → needs_review
            elif (
                _review_status == "needs_review"
                and category != "review"
                and dead_letter_queue
            ):
                dead_letter_queue.add(
                    _doc_id, "low_confidence",
                    f"confidence={_confidence:.2f} "
                    f"({result.get('reason', '')[:80]})",
                    category=category,
                )

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

            # ── ใช้ OCR text จาก classify_with_keywords ก่อน ──
            # เพื่อไม่ต้อง OCR ซ้ำ (Tesseract non-deterministic → ผลอาจต่างกัน)
            _classify_ocr = result.get("_ocr_text", "")

            # ถ้ามี merged_ocr_text จาก multi-page buffer → ใช้ข้อความรวมเลย
            if merged_ocr_text:
                extracted_text = merged_ocr_text
                ocr_status = (
                    f"ocr_merged_{merged_page_count}pages"
                    if merged_page_count > 1
                    else "ocr_prepass"  # OCR มาแล้วจาก batch (รูปเดียว)
                )
                logger.info(
                    f"ใช้ merged OCR text: {merged_page_count} หน้า, "
                    f"{len(extracted_text)} chars"
                )
            elif _classify_ocr:
                # ใช้ข้อความ OCR ที่ classify_with_keywords อ่านไว้แล้ว
                extracted_text = _classify_ocr
                ocr_status = "ocr_reused"
                logger.info(
                    "ใช้ OCR text จาก classify_with_keywords: %d chars",
                    len(extracted_text),
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
                        _matched_registry = False
                        try:
                            _early_name = extract_patient_name_from_document(extracted_text)
                        except Exception:
                            _early_name = ""
                        _pp, _how, _warn = _pick_patient_for_doc(found_hns, _early_name)
                        if _pp:
                            metadata["patient_hn_folder"] = _pp.folder_name
                            _matched_registry = True
                            if _how == "ocr_name":
                                metadata["matched_by"] = "ocr_name"
                            if _warn:
                                metadata["hn_name_warning"] = _warn
                            logger.info(
                                "Auto HN route: matched by %s%s (category=%s)",
                                _how, f" [{_warn}]" if _warn else "", category,
                            )
                        elif _how == "ambiguous":
                            # HN หลายตัวชี้คนละผู้ป่วย ไม่มีชื่อยืนยัน → ไม่เดา ไม่ลงทะเบียนใหม่
                            metadata["hn_name_warning"] = "multi_hn"
                            found_hns = []
                            logger.info("Auto HN route: ambiguous HN candidates — not filed (category=%s)", category)
                        # ── ชุดเดียวกัน: หน้าอื่นจับคู่ทะเบียนได้รายเดียว ──
                        # (2026-11-02: ใบฟอกเลือด 2 หน้าของผู้ป่วยคนเดียว
                        # หน้า 2 OCR อ่าน HN เพี้ยนเป็น "162" → เข้าแฟ้ม HN162
                        # LIFF ของผู้ป่วยจึงเห็นใบเดียว) → ใช้แฟ้มของชุด
                        if not _matched_registry and batch_patient_folder:
                            metadata["patient_hn_folder"] = batch_patient_folder
                            metadata["hn_route_source"] = "batch_sibling"
                            _matched_registry = True
                            logger.info(
                                "Auto HN route: OCR HN not in registry — "
                                "using batch sibling folder (category=%s)",
                                category,
                            )
                        # ── Fallback: Auto-register + ใช้ HN จาก OCR ──
                        # เมื่อ OCR ดึง HN ได้แต่ไม่พบในทะเบียนผู้ป่วยกลาง
                        # → auto-register ผู้ป่วย เพื่อให้เอกสารถัดไปจัดเข้าแฟ้มได้
                        if not _matched_registry and found_hns:
                            _fallback_hn = found_hns[0]
                            # ลอง auto-register
                            try:
                                _ar_ok, _ar_msg = master_registry.register(
                                    hospital_hn=_fallback_hn,
                                    registered_by="auto_ocr",
                                    notes=f"auto-registered from OCR {category}",
                                )
                                if _ar_ok:
                                    _new_folder = master_registry.get_folder_for_hn(
                                        _fallback_hn
                                    )
                                    if _new_folder:
                                        metadata["patient_hn_folder"] = _new_folder
                                        _matched_registry = True
                                        logger.info(
                                            "Auto HN route: auto-registered HN%s → %s",
                                            _fallback_hn, _new_folder,
                                        )
                            except Exception as _ar_err:
                                logger.warning(
                                    "Auto HN route: auto-register failed: %s", _ar_err
                                )
                            if not _matched_registry:
                                metadata["patient_hn_folder"] = f"HN{_fallback_hn}"
                                logger.info(
                                    "Auto HN route: no registry match — using "
                                    "HN as folder name: HN%s (category=%s)",
                                    _fallback_hn, category,
                                )
                    elif found_hns and not master_registry:
                        # Registry ไม่พร้อม — ใช้ HN จาก OCR เป็น folder
                        _fallback_hn = found_hns[0]
                        metadata["patient_hn_folder"] = f"HN{_fallback_hn}"
                        logger.info(
                            "Auto HN route: registry unavailable — using "
                            "HN as folder name: HN%s (category=%s)",
                            _fallback_hn, category,
                        )
                    elif not found_hns and batch_patient_folder:
                        # หน้านี้อ่าน HN ไม่ได้เลย แต่หน้าอื่นในชุดเดียวกัน
                        # ชี้ผู้ป่วยรายเดียว → เข้าแฟ้มเดียวกัน
                        metadata["patient_hn_folder"] = batch_patient_folder
                        metadata["hn_route_source"] = "batch_sibling"
                        logger.info(
                            "Auto HN route: no HN in OCR — using batch "
                            "sibling folder (category=%s)", category
                        )
                    elif not found_hns:
                        logger.info(
                            "Auto HN route: no HN found in OCR text "
                            "(category=%s)", category
                        )
                        # DLQ: ไม่พบ HN ในเอกสาร
                        if dead_letter_queue and category in CLINICAL_CATEGORIES:
                            dead_letter_queue.add(
                                _doc_id, "hn_not_found",
                                "ไม่พบ HN ในเอกสาร",
                                category=category,
                            )
                except Exception as hn_err:
                    logger.warning(
                        "HN extraction failed: %s", type(hn_err).__name__
                    )

            # ── Patient Name Extraction from OCR ──
            # ดึงชื่อคนไข้จากเอกสาร — ใช้เสริมเมื่อ HN ไม่อยู่ใน registry
            _ocr_patient_name = ""
            if extracted_text and category in CLINICAL_CATEGORIES:
                try:
                    _ocr_patient_name = extract_patient_name_from_document(
                        extracted_text
                    )
                    if _ocr_patient_name:
                        metadata["ocr_patient_name"] = _ocr_patient_name
                        logger.info(
                            "OCR patient name extracted: (category=%s)",
                            category,
                        )

                        # ── Name-Based Fallback: ค้นชื่อจาก registry ──
                        # เมื่อ OCR หา HN ไม่ได้ แต่ดึงชื่อผู้ป่วยได้
                        # → ค้นชื่อใน master_registry
                        # ─ ปลอดภัย: ต้องพบ **ผู้ป่วยเพียง 1 คน**
                        #   ถ้าพบหลายคน → ไม่จับคู่ (กันจัดเข้าแฟ้มผิดคน)
                        _cur_folder = metadata.get("patient_hn_folder", "")
                        if not _cur_folder and master_registry:
                            try:
                                _name_match = master_registry.search_by_name(
                                    _ocr_patient_name
                                )
                                if _name_match:
                                    metadata["patient_hn_folder"] = (
                                        _name_match.folder_name
                                    )
                                    metadata["matched_by"] = "ocr_name"
                                    logger.info(
                                        "Name-based route: matched '%s' → "
                                        "folder (category=%s)",
                                        "(name)", category,
                                    )
                                else:
                                    logger.info(
                                        "Name-based route: no unique match "
                                        "for OCR name (category=%s)",
                                        category,
                                    )
                            except Exception as nm_err:
                                logger.warning(
                                    "Name-based route failed: %s",
                                    type(nm_err).__name__,
                                )

                        # ถ้า HN จาก OCR ได้ แต่ไม่อยู่ใน registry
                        # (folder ขึ้นต้นด้วย "HN") — เสริมชื่อเข้าไป
                        _cur_folder = metadata.get("patient_hn_folder", "")
                        if _cur_folder and _cur_folder.startswith("HN"):
                            _safe_name = _sanitize_patient_folder_name(
                                _ocr_patient_name
                            )
                            if _safe_name:
                                metadata["patient_hn_folder"] = (
                                    f"{_cur_folder}_{_safe_name}"
                                )
                                logger.info(
                                    "Enhanced folder with OCR name: %s",
                                    metadata["patient_hn_folder"],
                                )
                    # ── Auto-register: ลงทะเบียนผู้ป่วยอัตโนมัติจาก OCR ──
                    # เมื่อ OCR ดึง HN + ชื่อได้ แต่ไม่อยู่ใน registry
                    # → ลงทะเบียนเพื่อให้เอกสารถัดไปจัดเข้าแฟ้มได้ทันที
                    if (
                        _ocr_patient_name
                        and _cur_folder
                        and _cur_folder.startswith("HN")
                        and master_registry
                    ):
                        try:
                            # ดึง HN จาก folder name (HN00102196 → 00102196)
                            _auto_hn = _cur_folder.split("_")[0]
                            if _auto_hn.startswith("HN"):
                                _auto_hn = _auto_hn[2:]
                            _reg_ok, _reg_msg = master_registry.register(
                                hospital_hn=_auto_hn,
                                patient_name=_ocr_patient_name,
                                registered_by="auto_ocr",
                                notes=f"auto-registered from {category} document",
                            )
                            if _reg_ok:
                                logger.info(
                                    "Auto-registered patient: HN%s "
                                    "(category=%s)",
                                    _auto_hn, category,
                                )
                                # อัพเดต folder ใช้ชื่อจาก registry
                                _new_folder = master_registry.get_folder_for_hn(
                                    _auto_hn
                                )
                                if _new_folder:
                                    metadata["patient_hn_folder"] = _new_folder
                                    logger.info(
                                        "Updated folder from registry: %s",
                                        _new_folder,
                                    )
                            else:
                                logger.info(
                                    "Auto-register skipped: %s", _reg_msg
                                )
                        except Exception as reg_err:
                            logger.warning(
                                "Auto-register failed: %s",
                                type(reg_err).__name__,
                            )

                except Exception as name_err:
                    logger.warning(
                        "Patient name extraction failed: %s",
                        type(name_err).__name__,
                    )

            # ── Document Date Extraction from OCR ──
            # ดึงวันที่สั่ง/วันที่ตรวจจากเอกสาร — ใช้แทน LINE timestamp
            # สำหรับจัดเก็บตาม YYYY-QN/YYYY-MM ที่ถูกต้อง
            _doc_date = None
            if extracted_text and category in CLINICAL_CATEGORIES:
                try:
                    _doc_date = extract_document_date(extracted_text)
                    if _doc_date:
                        metadata["document_date"] = _doc_date.isoformat()
                        # ใช้วันที่จากเอกสารแทน submitted_at
                        # เพื่อให้ folder เดือน/ไตรมาสตรงกับวันที่ในเอกสาร
                        metadata["submitted_at"] = _doc_date.isoformat()
                        logger.info(
                            "Document date extracted and applied: %s "
                            "(category=%s)",
                            _doc_date.strftime("%Y-%m-%d"), category,
                        )
                except Exception as date_err:
                    logger.warning(
                        "Document date extraction failed: %s",
                        type(date_err).__name__,
                    )

            # ── โหมดเลือกผู้ป่วย: fallback เมื่อ OCR หาผู้ป่วยไม่ได้ ──
            # ลำดับความสำคัญ:
            #   1. HN/ชื่อจากเอกสาร (OCR) → แม่นยำที่สุด (Vision API)
            #   2. Patient context (เจ้าหน้าที่เลือกไว้) → ใช้เมื่อ OCR หาไม่เจอ
            # เหตุผล: เอกสารคนละคนอาจส่งติดกัน — ต้องใช้ข้อมูลจาก
            # เอกสารแต่ละฉบับ ไม่ใช่ค่าจากเอกสารก่อนหน้า
            _ctx_conflict = ""
            if category in CLINICAL_CATEGORIES and owner_hash:
                try:
                    _ocr_folder = metadata.get("patient_hn_folder", "")
                    _ctx = _get_patient_context(owner_hash)
                    if _ctx:
                        _ctx_folder = _ctx.get("patient_folder", "")
                        if _ocr_folder and _ctx_folder and _ctx_folder != _ocr_folder:
                            # OCR กับ context ไม่ตรงกัน
                            # → ใช้ OCR เพราะเป็นข้อมูลจากเอกสารฉบับนี้
                            # → แจ้งให้ทราบว่า context เปลี่ยน
                            _ctx_conflict = _ctx_folder
                            # ไม่เปลี่ยน — ใช้ _ocr_folder ที่ OCR อ่านได้
                            logger.info(
                                "OCR HN differs from context "
                                "(category=%s) — using OCR result "
                                "(document data takes priority)",
                                category,
                            )
                        elif not _ocr_folder and _ctx_folder:
                            # OCR หาไม่เจอ → ใช้ context เป็น fallback
                            metadata["patient_hn_folder"] = _ctx_folder
                            logger.info(
                                "Patient context applied as fallback "
                                "(category=%s)",
                                category,
                            )
                except Exception as ctx_err:
                    logger.warning(
                        "Patient context lookup failed: %s",
                        type(ctx_err).__name__,
                    )

            # ── Auto-set patient context: เมื่อ match ผู้ป่วยได้ ──
            # ตั้ง context อัตโนมัติเพื่อให้เอกสารถัดไปใช้คนเดิม
            # (ไม่ต้องพิมพ์ "ผู้ป่วย <HN>" ทุกครั้ง)
            _final_folder = metadata.get("patient_hn_folder", "")
            if (
                _final_folder
                and not _final_folder.startswith("HN")
                and owner_hash
                and category in CLINICAL_CATEGORIES
            ):
                try:
                    # ดึง HN + ชื่อจาก folder name (format: 66298_นายสุชีพ_อินสุทน)
                    _ff_parts = _final_folder.split("_", 1)
                    _ff_hn = _ff_parts[0] if _ff_parts else ""
                    _ff_name = _ff_parts[1].replace("_", " ") if len(_ff_parts) > 1 else ""
                    if _ff_hn:
                        _set_patient_context(
                            owner_hash, _final_folder, _ff_hn, _ff_name
                        )
                        logger.info(
                            "Auto-set patient context from OCR match: %s",
                            _ff_hn,
                        )
                except Exception:
                    pass

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
                    # เวลาเข้าระบบจริง (UTC) — ใช้เรียง "รายการล่าสุด"
                    "indexed_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "mime_type": mime_map.get(ext_now, "application/octet-stream"),
                    "file_size": size_now,
                    "ocr_status": ocr_status,
                    "extracted_text": extracted_text,
                    # Owner tracking — ป้องกัน cross-patient access
                    # ผู้ใช้เห็นเฉพาะเอกสารที่ตัวเองส่ง
                    "owner_hash": owner_hash or "",
                    # ── Schema V2 fields ──
                    "schema_version": 2,
                    "content_sha256": sha256_hex or "",
                    "confidence": round(_confidence, 3),
                    "review_status": _review_status,
                    "classify_reason": (
                        result.get("reason", "")[:200]
                    ),
                }
                # ── multi-image batch / merged pages (กันซ้ำถาวร + จัดกลุ่ม) ──
                if batch_id:
                    doc_meta["batch_id"] = batch_id
                if page_sha256s:
                    doc_meta["page_sha256s"] = list(page_sha256s)
                    doc_meta["page_count"] = len(page_sha256s)
                # ── patient_hn_folder: ให้ค้นเอกสารจาก HN ผู้ป่วยได้ ──
                _hn_folder = metadata.get("patient_hn_folder", "")
                if _hn_folder:
                    doc_meta["patient_hn_folder"] = _hn_folder
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
                    # ── Dual-write: PostgreSQL (Phase 1.1) ──
                    if db and db.is_ready:
                        try:
                            _db_doc = dict(doc_meta)
                            _db_doc["doc_id"] = _db_doc.pop("document_id", _doc_id)
                            db.upsert_document(_db_doc)
                        except Exception:
                            pass  # PostgreSQL ล้มไม่กระทบ GCS
                    event_logger.log("document_indexed", data={
                        "category": category,
                        "doc_id": _doc_id[:8],
                    })
                    # ── V5.0 Workflow trigger (fire-and-forget, ไม่ throw) ──
                    if v5 is not None:
                        try:
                            _wf_ctx = {
                                "doc_id": _doc_id,
                                "category": category,
                                "original_name": doc_meta.get("original_name", ""),
                                "patient_hn_folder": doc_meta.get("patient_hn_folder", ""),
                                "bill_company": doc_meta.get("bill_company", ""),
                                "vendor_needs_review": doc_meta.get("vendor_needs_review", False),
                                "vendor_method": doc_meta.get("vendor_method", ""),
                                "ocr_confidence": doc_meta.get("ocr_confidence", ""),
                                "gcs_path": gcs_object_path or "",
                            }
                            v5.fire("document_indexed", _wf_ctx)
                            if category == "bills":
                                v5.fire("bill_received", _wf_ctx)
                        except Exception as _wf_err:
                            logger.warning("V5 workflow fire: %s", type(_wf_err).__name__)
                except RuntimeError as idx_err:
                    # Index เขียน GCS ไม่ได้ — ไฟล์ upload แล้ว แต่ metadata หาย
                    # ไม่ตอบว่าสำเร็จ — แจ้ง error กลับไป
                    event_logger.log("document_indexed", data={
                        "error": type(idx_err).__name__,
                    }, success=False)
                    logger.error(
                        f"Document index write ล้มเหลว: {idx_err} "
                        f"(doc_id={_doc_id})"
                    )
                    raise RuntimeError(
                        f"ไฟล์ถูกอัปโหลดแล้ว แต่บันทึก metadata ล้มเหลว — "
                        f"กรุณาแจ้งผู้ดูแลระบบ"
                    )

                # ── Google Drive Auto-Backup ────────────────────
                # สำเนาเอกสารไป Google Drive อัตโนมัติ (ถ้าเปิดใช้งาน)
                try:
                    import google_drive_sync as _gd
                    if _gd.is_available() and gcs_object_path:
                        _drive_subfolder = category
                        _hn_f = metadata.get("patient_hn_folder", "")
                        if _hn_f:
                            _drive_subfolder = f"{category}/{_hn_f}"
                        _gd.backup_document_from_gcs(
                            storage, gcs_object_path,
                            subfolder=_drive_subfolder,
                        )
                        logger.info(
                            "Drive backup queued (category=%s)", category
                        )
                except Exception as _drv_err:
                    # Drive backup ล้มเหลวไม่กระทบ pipeline หลัก
                    logger.warning(
                        "Drive backup failed: %s",
                        type(_drv_err).__name__,
                    )

                # ── Google Sheets Document Log ──────────────────
                # บันทึกเอกสารใหม่ลง Sheets (ถ้าเปิดใช้งาน)
                try:
                    import google_sheets_sync as _gs
                    if _gs.is_available():
                        _gs.append_document_log(doc_meta)
                except Exception as _sh_err:
                    logger.warning(
                        "Sheets log failed: %s",
                        type(_sh_err).__name__,
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
                        # ── V5.0 Workflow trigger: lab_result_extracted ──
                        # ส่งเฉพาะค่าตัวเลข/จำนวนผิดปกติ + HN folder (ไม่ส่งชื่อ)
                        if v5 is not None:
                            try:
                                _abn = {"critical_high", "critical_low",
                                        "abnormal_high", "abnormal_low"}
                                _crit = {"critical_high", "critical_low"}
                                _lab_ctx = {
                                    "doc_id": _doc_id,
                                    "category": category,
                                    "patient_hn_folder": metadata.get("patient_hn_folder", ""),
                                    "result_count": len(lab_report.results),
                                    "abnormal_count": sum(
                                        1 for r in lab_report.results
                                        if _abn & set(r.flags or [])
                                    ),
                                    "critical_count": sum(
                                        1 for r in lab_report.results
                                        if _crit & set(r.flags or [])
                                    ),
                                    "specimen_date": getattr(lab_report, "specimen_date", ""),
                                }
                                _an_map = {
                                    "egfr": ("egfr",), "creatinine": ("creatinine", "cr"),
                                    "potassium": ("potassium", "k"),
                                    "hemoglobin": ("hemoglobin", "hb", "hgb"),
                                    "bun": ("bun",), "phosphorus": ("phosphorus", "phosphate"),
                                    "calcium": ("calcium", "ca"), "albumin": ("albumin",),
                                }
                                for r in lab_report.results:
                                    _a = (r.analyte or "").strip().lower()
                                    for _k, _aliases in _an_map.items():
                                        if _a in _aliases or _a.startswith(_k):
                                            _lab_ctx.setdefault(_k, r.value)
                                v5.fire("lab_result_extracted", _lab_ctx)
                            except Exception as _wf_err:
                                logger.warning(
                                    "V5 lab workflow fire: %s", type(_wf_err).__name__
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
                            # 17 พ.ย. 2569: ถ้าผู้ส่งเป็นแพทย์ผู้รับ alert วิกฤตด้วย → รวมเป็นข้อความเดียว
                            # (เดิมได้ 2 ข้อความ: สรุปผล + "แจ้งเตือนผลตรวจ ... รับทราบ <รหัส>")
                            _user_alert_text = ""
                            _merge_ack = False
                            if _alert_summary.should_notify_user and line_user_id:
                                _user_alert_text = format_user_alert_th(
                                    _alert_summary
                                )
                                try:
                                    _merge_ack = bool(
                                        alert_service is not None
                                        and _alert_summary.critical_count > 0
                                        and line_push_fn is not None
                                        and line_user_id in (
                                            alert_service._recipients.get("physician", []) or []
                                        )
                                    )
                                except Exception:
                                    _merge_ack = False
                                if _user_alert_text and not _merge_ack:
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
                            _merged_sent = None   # None = ยังไม่ส่ง, True/False = ผลการส่งข้อความรวม
                            if (alert_service is not None
                                    and _alert_summary.critical_count > 0):
                                try:
                                    _new_alerts = []
                                    # บริบทผู้ป่วย: ผู้ป่วยฟอกเลือดประจำ eGFR<15 เป็นเรื่องปกติของโรค
                                    # → AlertDeliveryService ลดเป็น urgent (มีรองรับอยู่แล้วแต่ไม่เคยส่งบริบทเข้าไป)
                                    _pctx = None
                                    try:
                                        _hf = metadata.get("patient_hn_folder", "")
                                        _pp = master_registry.match_hn(_hf.split("_")[0]) if (_hf and master_registry) else None
                                        if _pp and (getattr(_pp, "hd_per_week", 0) or getattr(_pp, "dialysis_schedule", "")):
                                            _pctx = {"is_dialysis_patient": True}
                                    except Exception:
                                        _pctx = None
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
                                                    patient_context=_pctx,
                                                )
                                            )
                                            if _new_alert is not None:
                                                _new_alerts.append(_new_alert)
                                    # ข้อความรวมถึงผู้ส่ง (ถ้าเป็นแพทย์ผู้รับ alert): สรุปผล + รหัสรับทราบ
                                    if _merge_ack and _user_alert_text:
                                        _codes = [a.alert_id[:8] for a in _new_alerts]
                                        _merged = _user_alert_text
                                        if _codes:
                                            _merged += (
                                                "\n\n✅ รับทราบผลวิกฤต: พิมพ์ "
                                                + f"'รับทราบ {_doc_id[:8]}'"
                                                + (f"\n(หรือรายการ: {', '.join(_codes)})" if len(_codes) > 1 else "")
                                            )
                                        try:
                                            _merged_sent = bool(line_push_fn(line_user_id, _merged))
                                        except Exception:
                                            _merged_sent = False
                                        if not _merged_sent:
                                            # ส่งข้อความรวมไม่สำเร็จ → ใช้ข้อความสรุปเดิม และไม่ข้ามการแจ้งรายรายการ
                                            push_message(line_user_id, _user_alert_text)
                                    # ส่ง LINE push ทันที — ผู้ส่งที่ได้รับข้อความรวมแล้วไม่ต้องได้ซ้ำ
                                    if line_push_fn is not None:
                                        def _push_skip_uploader(_uid, _msg, _orig=line_push_fn):
                                            if _merged_sent and _uid == line_user_id:
                                                return True   # ได้รับแล้วในข้อความรวม
                                            return _orig(_uid, _msg)
                                        for _na in _new_alerts:
                                            alert_service.attempt_delivery(
                                                _na.alert_id,
                                                send_push_fn=_push_skip_uploader,
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
                            # กันตกหล่น: เลือกรวมข้อความไว้แต่ยังไม่ได้ส่งอะไรเลย → ส่งสรุปเดิม
                            if _merge_ack and _user_alert_text and _merged_sent is None:
                                push_message(line_user_id, _user_alert_text)

                        except Exception as alert_err:
                            # Alert ล้มเหลวไม่ทำให้ upload ล้มเหลว
                            logger.warning(
                                f"Lab alert ล้มเหลว: "
                                f"{type(alert_err).__name__} "
                                f"(doc_id={_doc_id[:8]})"
                            )

                        # ── Hb ต่ำ auto-alert → แจ้งแพทย์ (เพิ่ม 2026-11-05) ──
                        try:
                            _hb_val = _extract_hb_from_text(extracted_text)
                            if _hb_val is not None and _hb_val < 8.0:
                                _hn_folder = metadata.get(
                                    "patient_hn_folder", ""
                                )
                                _hb_patient = master_registry.match_hn(
                                    _hn_folder
                                ) if _hn_folder else None
                                _hb_name = (
                                    _hb_patient.patient_name
                                    if _hb_patient else _hn_folder
                                )
                                _hb_esa = ""
                                if _hb_patient and _hb_patient.esa_code:
                                    _esa_med = (
                                        _hb_patient.esa_medication
                                        or _hb_patient.esa_code
                                    )
                                    _hb_esa = f"\n💊 ESA ปัจจุบัน: {_esa_med}"
                                _hb_alert_msg = (
                                    f"🔴 Hb ต่ำ!\n"
                                    f"━━━━━━━━━━━━━━━━\n"
                                    f"👤 {_hb_name}\n"
                                    f"🔢 HN: {_hn_folder}\n"
                                    f"🩸 Hb = {_hb_val:.1f} g/dL"
                                    f" (< 8.0){_hb_esa}\n\n"
                                    f"📋 ควรพิจารณาให้เลือด"
                                    f" หรือปรับยา ESA"
                                )
                                for _ph_id in _staff_line_ids():
                                    try:
                                        push_message(_ph_id, _hb_alert_msg)
                                    except Exception:
                                        pass
                                logger.info(
                                    f"Hb low alert: {_hn_folder} "
                                    f"Hb={_hb_val:.1f}"
                                )
                        except Exception as _hb_err:
                            logger.warning(
                                "Hb alert error: %s",
                                type(_hb_err).__name__
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

            # ── Invoice Auto-Parse: ดึงข้อมูลบิลอัตโนมัติ ──
            invoice_parsed = False
            if (
                category == "bills"
                and stored_in_gcs
                and extracted_text
                and expense_tracker is not None
            ):
                try:
                    from invoice_parser import parse_invoice
                    _inv_result = parse_invoice(
                        extracted_text, bill_company or ""
                    )
                    if _inv_result and _inv_result.get("total_amount", 0) > 0:
                        # ตรวจ invoice ซ้ำ
                        _dup = expense_tracker.detect_duplicate_invoice(
                            _inv_result.get("invoice_number", ""),
                            _inv_result.get("vendor_name", ""),
                            _inv_result.get("total_amount", 0),
                        )
                        if not _dup:
                            expense_tracker.record_invoice(
                                _doc_id, _inv_result
                            )
                            invoice_parsed = True
                            logger.info(
                                "Invoice auto-parsed: doc=%s, conf=%.2f",
                                _doc_id[:8],
                                _inv_result.get("confidence", 0),
                            )
                        else:
                            logger.info(
                                "Invoice duplicate skipped: doc=%s",
                                _doc_id[:8],
                            )
                except Exception as inv_err:
                    logger.warning(
                        "Invoice auto-parse failed: %s",
                        type(inv_err).__name__,
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
                "invoice_parsed": invoice_parsed,
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
            cat_names = _CAT_NAMES  # ตารางชื่อประเภทรวมที่เดียว
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
            if category in _FIX_TO and result.get("ambiguous_with"):
                # 18 พ.ย. 2026: ข้อความมีลักษณะทั้งใบยาและใบฟอกเลือด — ให้คนยืนยัน
                _fx = _FIX_TO[category]
                _reply_lines.append(
                    "⚠️ ระบบไม่แน่ใจว่าเป็นใบยาหรือใบฟอกเลือด — ถ้าผิดพิมพ์ "
                    f"'เปลี่ยนประเภท {_doc_id[:8]} {_fx[1]}'"
                )
            if invoice_parsed and category == "bills":
                _reply_lines.append("🧾 บันทึกค่าใช้จ่ายอัตโนมัติแล้ว")
                _reply_lines.append(
                    "พิมพ์ 'สรุปค่าใช้จ่าย' เพื่อดูสรุปรายเดือน"
                )
            if merged_page_count > 1:
                _reply_lines.insert(
                    0,
                    f"📄 รวม{cat_name} {merged_page_count} หน้าเป็น {file_type_name} เดียวสำเร็จ",
                )
            elif batch_id:
                _reply_lines.insert(0, f"📄 {cat_name} (ส่งมาพร้อมกันเป็นชุด)")
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
                    if metadata.get("hn_name_warning") in ("hn_name_mismatch", "name_mismatch"):
                        _reply_lines.append(
                            "⚠️ HN ที่อ่านได้ไม่ตรงกับชื่อในเอกสาร — "
                            + ("จัดเข้าแฟ้มตามชื่อ" if metadata.get("matched_by") == "ocr_name" else "จัดตาม HN")
                            + " กรุณาตรวจสอบ ถ้าผิดพิมพ์ 'ย้ายเอกสาร <รหัส> <HN>'"
                        )
                    elif metadata.get("matched_by") == "ocr_name":
                        _reply_lines.append(
                            "🔍 จับคู่จากชื่อในเอกสาร (ตรวจสอบว่าถูกคนหรือไม่)"
                        )
                    # _ctx_conflict: OCR เปลี่ยนผู้ป่วยจาก context
                    # ไม่แสดงข้อความ — ทำงานถูกต้องอยู่แล้ว ไม่ต้องแจ้ง
                else:
                    _reply_lines.append(
                        "👤 ยังไม่ระบุผู้ป่วย — เก็บไว้ที่ระดับไตรมาส/เดือน\n"
                        + ("⚠️ พบ HN หลายตัวที่ชี้คนละผู้ป่วย ระบบไม่เดา\n"
                           if metadata.get("hn_name_warning") == "multi_hn" else "")
                        + f"พิมพ์ 'ย้ายเอกสาร {_doc_id[:8]} <HN>' เพื่อเข้าแฟ้มรายคน"
                    )
                # แสดงชื่อคนไข้ที่อ่านได้จากเอกสาร (ถ้ามี)
                if metadata.get("ocr_patient_name"):
                    _reply_lines.append(
                        f"📝 ชื่อในเอกสาร: {metadata['ocr_patient_name']}"
                    )
                # แสดงวันที่จากเอกสาร (ถ้า extract ได้)
                if metadata.get("document_date"):
                    try:
                        _ddiso = metadata["document_date"]
                        _dd = datetime.fromisoformat(_ddiso)
                        _thai_yr = _dd.year + 543
                        _reply_lines.append(
                            f"📅 วันที่ในเอกสาร: {_dd.day}/{_dd.month}/{_thai_yr}"
                        )
                    except Exception:
                        pass
            _result_text = "\n".join(_reply_lines)

            # ── ชุดหลายรูป: เก็บผลไว้สรุปข้อความเดียว (แทน push รายใบ) ──
            _in_batch = False
            if batch_id:
                _ask_pending = bool(
                    category == "review" and line_user_id
                    and (owner_hash or "") and dest
                )
                _batch_item = {
                    "n": batch_index,
                    "doc8": _doc_id[:8],
                    "cat": cat_name,
                    "emoji": {"bills": "🧾", "rx": "💊", "lab": "🔬",
                              "dialysis": "🩸"}.get(category, "📄"),
                    "storage": _storage_display,
                    "folder": (
                        metadata.get("patient_hn_folder", "")
                        if category in CLINICAL_CATEGORIES
                        else (bill_company or "") if category == "bills" else ""
                    ),
                    "clinical": category in CLINICAL_CATEGORIES,
                    "status": "ask" if _ask_pending else "ok",
                    # 18 พ.ย. 2026: ประเภทจริง + ธงไม่แน่ใจ (ใบยา ↔ ใบฟอกเลือด)
                    # → สรุปชุดแสดงรหัสตามลำดับรูป และปุ่มแก้ประเภทกดครั้งเดียว
                    "category": category,
                    "ambiguous": (
                        str(result.get("ambiguous_with") or "")
                        if category in ("rx", "dialysis") else ""
                    ),
                }
                _batch_bubble = None
                if category != "review":
                    try:
                        _batch_bubble = _build_doc_flex_bubble(
                            category, cat_name, _storage_display, _doc_id, dest,
                            n=batch_index, ambiguous=_batch_item["ambiguous"],
                        )
                    except Exception:
                        _batch_bubble = None
                _in_batch = _batch_record(batch_id, _batch_item, _batch_bubble)

            # ── Interactive Classification: ถามผู้ใช้เมื่อ OCR ล้มเหลว ──
            if category == "review" and line_user_id:
                # OCR ไม่สามารถจำแนกได้ → ตั้ง pending + ถามผู้ใช้ผ่าน LINE
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
                if _in_batch:
                    pass  # สรุปชุดจะส่งคำถามรวมใน _batch_flush
                else:
                    # ไม่มี owner_hash หรือ dest → ส่ง reply ปกติ
                    if reply_token:
                        reply_message(reply_token, _result_text)
                    elif line_user_id:
                        push_message(line_user_id, _result_text)
            elif _in_batch:
                pass  # ผลรายใบอยู่ในข้อความสรุปชุด
            elif reply_token:
                reply_message(reply_token, _result_text)
            elif line_user_id:
                # Multi-page merge: reply_token หมดอายุ → ใช้ push_message
                push_message(line_user_id, _result_text)

            # ── แจ้งเตือนแอดมิน/ผู้รับแจ้งเตือน เมื่อได้รับเอกสาร ──
            # ส่ง push notification + ปุ่มเปิดเอกสาร ไปหาแอดมิน
            # ทำงานกับทุกประเภท: bills, rx, lab, dialysis
            # แจ้งเตือนทุกเอกสารที่เข้ามา — กรองเฉพาะผู้ส่งเอง
            # (ผู้ส่งได้รับ reply แล้ว ไม่ต้อง push ซ้ำ)
            try:
                if category != "review" and not _in_batch:
                    # (ชุดหลายรูป: แจ้งผู้รับครั้งเดียวใน _batch_flush)
                    # ตรวจว่าผู้ส่งเป็นเจ้าหน้าที่หรือไม่ → ปรับข้อความ
                    _sender_is_staff = bool(
                        owner_hash and is_authorized_user(owner_hash)
                    )
                    _source_label = (
                        "จากเจ้าหน้าที่" if _sender_is_staff
                        else "จากภายนอก"
                    )
                    # สร้างข้อความแจ้งเตือนสำหรับแอดมิน
                    _admin_notify_lines = [
                        f"📬 ได้รับเอกสารใหม่{_source_label}",
                        f"📁 ประเภท: {cat_name}",
                        f"📂 จัดเก็บใน: {_storage_display}",
                    ]
                    if bill_company and category == "bills":
                        _admin_notify_lines.append(
                            f"🏢 บริษัท: {bill_company}"
                        )
                    if merged_page_count > 1:
                        _admin_notify_lines.insert(
                            1, f"📄 รวม {merged_page_count} หน้า"
                        )
                    _admin_notify_text = "\n".join(_admin_notify_lines)

                    # สร้าง Flex Message พร้อมปุ่มเปิดเอกสาร
                    _admin_messages = [
                        {"type": "text", "text": _admin_notify_text},
                    ]

                    # สร้าง download URL จาก GCS path
                    _doc_download_url = None
                    if dest and dest.startswith("gs://"):
                        # ดึง object path จาก gs://bucket/path
                        _gcs_parts = dest.split("/", 3)
                        if len(_gcs_parts) >= 4:
                            _gcs_obj_path = _gcs_parts[3]
                            _doc_download_url = get_download_url(
                                _gcs_obj_path
                            )

                    if _doc_download_url:
                        # สร้าง URL ถาวร (ไม่หมดอายุ) สำหรับเปิดเอกสาร
                        _persistent_doc_url = ""
                        _base_url_flex = os.getenv(
                            "RENDER_EXTERNAL_URL", ""
                        ).rstrip("/")
                        _admin_secret_flex = os.getenv(
                            "ADMIN_SECRET", ""
                        ).strip()
                        if (
                            _base_url_flex
                            and _admin_secret_flex
                            and _doc_id
                        ):
                            _persistent_doc_url = (
                                f"{_base_url_flex}/admin/doc/view/"
                                f"{_doc_id}"
                                f"?secret={_admin_secret_flex}"
                            )
                        # ใช้ URL ถาวรถ้ามี, ไม่งั้นใช้ signed URL เดิม
                        if (
                            _persistent_doc_url
                            and len(_persistent_doc_url)
                            <= LINE_URI_ACTION_MAX_LENGTH
                        ):
                            _final_doc_url = _persistent_doc_url
                            _link_expiry_note = ""
                        else:
                            _final_doc_url = _doc_download_url
                            _link_expiry_note = (
                                f"⏱️ ลิงก์หมดอายุใน "
                                f"{SIGNED_URL_EXPIRY // 60} นาที"
                            )

                        # สร้าง Flex Message พร้อมปุ่มเปิดเอกสาร + สั่งการรักษา
                        _cat_emoji = {
                            "bills": "🧾", "rx": "💊",
                            "lab": "🔬", "dialysis": "🩸",
                        }.get(category, "📄")

                        # สร้าง URL สำหรับหน้าสั่งการรักษา
                        _annotate_url = ""
                        _base_url = os.getenv(
                            "RENDER_EXTERNAL_URL", ""
                        ).rstrip("/")
                        _admin_secret_val = os.getenv(
                            "ADMIN_SECRET", ""
                        ).strip()
                        if _base_url and _admin_secret_val and _doc_id:
                            _annotate_url = (
                                f"{_base_url}/admin/annotate/{_doc_id}"
                                f"?secret={_admin_secret_val}"
                            )

                        # ── Footer buttons ──
                        _footer_buttons = [
                            {
                                "type": "button",
                                "style": "primary",
                                "color": "#1DB446",
                                "action": {
                                    "type": "uri",
                                    "label": "📎 เปิดเอกสาร",
                                    "uri": _final_doc_url,
                                },
                            },
                        ]
                        # เพิ่มปุ่มสั่งการรักษา (เฉพาะเมื่อมี URL)
                        if _annotate_url and len(
                            _annotate_url
                        ) <= LINE_URI_ACTION_MAX_LENGTH:
                            _footer_buttons.append(
                                {
                                    "type": "button",
                                    "style": "primary",
                                    "color": "#0066CC",
                                    "action": {
                                        "type": "uri",
                                        "label": "✏️ สั่งการรักษา",
                                        "uri": _annotate_url,
                                    },
                                    "margin": "sm",
                                },
                            )

                        _flex_msg = {
                            "type": "flex",
                            "altText": f"{_cat_emoji} เอกสาร {cat_name} — กดเพื่อเปิด",
                            "contents": {
                                "type": "bubble",
                                "body": {
                                    "type": "box",
                                    "layout": "vertical",
                                    "contents": [
                                        {
                                            "type": "text",
                                            "text": f"{_cat_emoji} {cat_name}",
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
                                                            "text": "จัดเก็บ:",
                                                            "size": "sm",
                                                            "color": "#555555",
                                                            "flex": 0,
                                                        },
                                                        {
                                                            "type": "text",
                                                            "text": _storage_display[:40],
                                                            "size": "sm",
                                                            "color": "#111111",
                                                            "align": "end",
                                                            "wrap": True,
                                                        },
                                                    ],
                                                },
                                            ],
                                        },
                                        *(
                                            [
                                                {
                                                    "type": "text",
                                                    "text": _link_expiry_note,
                                                    "size": "xxs",
                                                    "color": "#999999",
                                                    "margin": "md",
                                                },
                                            ]
                                            if _link_expiry_note
                                            else []
                                        ),
                                    ],
                                },
                                "footer": {
                                    "type": "box",
                                    "layout": "vertical",
                                    "contents": _footer_buttons,
                                },
                            },
                        }
                        _admin_messages.append(_flex_msg)

                    # ดึงรายชื่อผู้รับจาก _alert_recipients (module-level)
                    _notif_ids = list(
                        _alert_recipients.get("physician", [])
                    )

                    # กรองไม่ให้ส่งซ้ำไปหาผู้ส่งเอง
                    if line_user_id and line_user_id in _notif_ids:
                        _notif_ids.remove(line_user_id)

                    _notif_sent = 0
                    for _rid in _notif_ids:
                        if push_messages(_rid, _admin_messages):
                            _notif_sent += 1
                    if _notif_sent:
                        logger.info(
                            "แจ้งเตือนเอกสารใหม่ส่งถึงผู้รับ %d ราย "
                            "(doc_id=%s, has_doc_link=%s)",
                            _notif_sent, _doc_id,
                            bool(_doc_download_url),
                        )
                    elif _notif_ids:
                        logger.warning(
                            "แจ้งเตือนเอกสารใหม่ส่งไม่ถึงผู้รับเลย "
                            "(doc_id=%s)", _doc_id,
                        )
            except Exception as _notif_err:
                # การแจ้งเตือนแอดมินล้มเหลวไม่ควรทำให้การรับเอกสารพัง
                logger.error(
                    "แจ้งเตือนแอดมินล้มเหลว: %s (doc_id=%s)",
                    type(_notif_err).__name__, _doc_id,
                )
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
            if batch_id and _batch_record(batch_id, {
                "n": batch_index, "doc8": _doc_id[:8], "status": "error",
                "reason": "จัดเก็บไม่สำเร็จ (ระบบจัดเก็บไม่พร้อม)",
            }):
                pass  # รายงานในสรุปชุด
            elif reply_token:
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
            if batch_id and _batch_record(batch_id, {
                "n": batch_index, "doc8": _doc_id[:8], "status": "error",
                "reason": f"ประมวลผลล้มเหลว ({type(e).__name__})",
            }):
                pass  # รายงานในสรุปชุด
            elif reply_token:
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

# หน้าต่างรวมรูปที่ส่งพร้อมกันเป็นชุดเดียว — ต่ำสุด 12 วินาที
# (แพทย์อนุมัติ 2026-11-02: env บน Render ตั้ง 8 ไว้ ทำให้ชุดขาดเป็น 2 ชุด
#  เมื่อมือถืออัปโหลดช้า → รูปที่มาช้าไม่มี "เพื่อนร่วมชุด" ให้อิง HN
#  ตั้ง env สูงกว่า 12 ได้ ต่ำกว่า 12 จะถูกยกเป็น 12)
_MULTIPAGE_WINDOW_MIN_SECS = 12
_MULTIPAGE_WINDOW_SECS = max(
    int(os.environ.get("MULTIPAGE_WINDOW_SECS", "12") or 12),
    _MULTIPAGE_WINDOW_MIN_SECS,
)
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
    """บันทึก pending classification ไปยัง GCS (shared across workers)

    รองรับหลายเอกสาร — เพิ่มเข้า list "docs" แทนที่จะเขียนทับ
    ตอบ "1" ครั้งเดียว → ย้ายทุกใบที่ pending
    """
    entry = {
        "doc_id": doc_id,
        "gcs_path": gcs_path,
        "sha256_hex": sha256_hex,
    }
    try:
        if not storage.is_ready or not hasattr(storage, '_bucket'):
            logger.warning("Storage not ready for pending classification")
            return
        blob_path = f"{_PENDING_CLASS_GCS_PREFIX}{user_hash}.json"
        blob = storage._bucket.blob(blob_path)
        # อ่าน existing pending (ถ้ามี) เพื่อ append
        data = None
        if blob.exists():
            try:
                data = json.loads(blob.download_as_text())
            except Exception:
                data = None
        if data and not data.get("expired") and \
                (time.time() - data.get("timestamp", 0)) < _PENDING_CLASS_TIMEOUT:
            # Append doc to existing pending
            docs = data.get("docs", [])
            # Migrate old format (single doc) → list
            if "doc_id" in data and not docs:
                docs = [{"doc_id": data["doc_id"], "gcs_path": data["gcs_path"],
                         "sha256_hex": data.get("sha256_hex", "")}]
            docs.append(entry)
            data["docs"] = docs
            data["timestamp"] = time.time()  # refresh timeout
        else:
            data = {
                "timestamp": time.time(),
                "docs": [entry],
                "owner_hash": owner_hash,
                "line_user_id": line_user_id,
            }
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

# ── Pending Patient Assignment ─────────────────────────────────
# เมื่อจำแนกเอกสารได้แต่ระบุผู้ป่วยไม่ได้ → เก็บ pending แล้วแสดง
# รายชื่อผู้ป่วยเป็นเลข ผู้ใช้ตอบเลข → ย้ายเอกสารเข้าแฟ้มผู้ป่วย
_PENDING_PATIENT_GCS_PREFIX = "_state/pending_patient/"
_PENDING_PATIENT_TIMEOUT = 600  # 10 นาที


def _set_pending_patient_assign(
    user_hash: str, doc_id: str, gcs_path: str,
    category: str, patient_choices: list[dict],
):
    """บันทึกเอกสารที่รอเลือกผู้ป่วย + ตัวเลือก"""
    data = {
        "timestamp": time.time(),
        "doc_id": doc_id,
        "gcs_path": gcs_path,
        "category": category,
        "choices": patient_choices,  # [{folder, hn, name}, ...]
    }
    try:
        if not storage.is_ready or not hasattr(storage, '_bucket'):
            return
        blob = storage._bucket.blob(
            f"{_PENDING_PATIENT_GCS_PREFIX}{user_hash}.json"
        )
        blob.upload_from_string(
            json.dumps(data, ensure_ascii=False),
            content_type="application/json",
        )
    except Exception as e:
        logger.warning("Failed to set pending patient: %s", type(e).__name__)


def _get_pending_patient_assign(user_hash: str) -> dict | None:
    """ดึง pending patient assignment — None ถ้าไม่มีหรือหมดเวลา"""
    try:
        if not storage.is_ready or not hasattr(storage, '_bucket'):
            return None
        blob = storage._bucket.blob(
            f"{_PENDING_PATIENT_GCS_PREFIX}{user_hash}.json"
        )
        if not blob.exists():
            return None
        data = json.loads(blob.download_as_text())
        if data.get("expired"):
            return None
        if (time.time() - data.get("timestamp", 0)) < _PENDING_PATIENT_TIMEOUT:
            return data
        return None
    except Exception:
        return None


def _clear_pending_patient_assign(user_hash: str):
    """เคลียร์ pending patient"""
    try:
        if not storage.is_ready or not hasattr(storage, '_bucket'):
            return
        blob = storage._bucket.blob(
            f"{_PENDING_PATIENT_GCS_PREFIX}{user_hash}.json"
        )
        blob.upload_from_string(
            json.dumps({"expired": True, "timestamp": 0}),
            content_type="application/json",
        )
    except Exception:
        pass


def _relocate_document_to_patient(
    doc_id: str, old_gcs_path: str, category: str,
    patient_folder: str,
) -> tuple[bool, str]:
    """ย้ายเอกสารจากโฟลเดอร์ไตรมาส/เดือน ไปยังแฟ้มผู้ป่วย

    Returns: (success, message)
    """
    try:
        if not storage.is_ready or not hasattr(storage, '_bucket'):
            return False, "Storage ไม่พร้อม"

        bucket = storage._bucket

        # สร้าง path ใหม่ — แทรก patient_folder ก่อนชื่อไฟล์
        # old: 05_DIALYSIS_RECORDS/2026-Q4/2026-10/xxx.jpg
        # new: 05_DIALYSIS_RECORDS/2026-Q4/2026-10/66298_นาย.../xxx.jpg
        path_parts = old_gcs_path.rsplit("/", 1)
        if len(path_parts) != 2:
            return False, "Path ไม่ถูกต้อง"

        parent_dir, filename = path_parts
        new_gcs_path = f"{parent_dir}/{patient_folder}/{filename}"

        # Copy (ไม่ delete ต้นฉบับ — safety)
        src_blob = bucket.blob(old_gcs_path)
        if not src_blob.exists():
            return False, "ไม่พบไฟล์ต้นฉบับ"

        bucket.copy_blob(src_blob, bucket, new_gcs_path)

        # อัปเดต doc index
        if doc_index:
            for doc in doc_index._docs:
                if doc.get("document_id") == doc_id:
                    doc["gcs_object_path"] = new_gcs_path
                    doc["patient_hn_folder"] = patient_folder
                    break
            doc_index._save()

        logger.info(
            "Relocated doc to patient folder: %s → %s",
            old_gcs_path[:30], patient_folder,
        )
        return True, new_gcs_path
    except Exception as e:
        logger.error("relocate doc failed: %s", type(e).__name__)
        return False, f"ย้ายไม่สำเร็จ: {type(e).__name__}"



def _patient_ctx_write(user_hash: str, data: dict) -> None:
    """เขียนสถานะผู้ป่วยปัจจุบันแบบ versioned (create-only)

    Service Account มีสิทธิ์สร้าง object เท่านั้น — การเขียนทับไฟล์เดิม
    `<hash>.json` ได้ 403 Forbidden ตั้งแต่ครั้งที่ 2 ทำให้โหมด
    "ผู้ป่วย <HN>" ใช้ได้แค่ครั้งแรกต่อคน จึงเขียนเป็นไฟล์ใหม่ทุกครั้ง
    `<hash>/v<timestamp>.json` แล้วอ่านตัวล่าสุด
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    blob = storage._bucket.blob(
        f"{_PATIENT_CTX_GCS_PREFIX}{user_hash}/v{ts}.json"
    )
    blob.upload_from_string(
        json.dumps(data, ensure_ascii=False),
        content_type="application/json",
        if_generation_match=0,
    )


def _patient_ctx_read(user_hash: str) -> dict | None:
    """อ่านสถานะผู้ป่วยปัจจุบันตัวล่าสุด (fallback ไฟล์เดี่ยวแบบเก่า)"""
    prefix = f"{_PATIENT_CTX_GCS_PREFIX}{user_hash}/v"
    latest = None
    for b in storage._bucket.list_blobs(prefix=prefix):
        if latest is None or b.name > latest.name:
            latest = b
    if latest is None:
        legacy = storage._bucket.blob(
            f"{_PATIENT_CTX_GCS_PREFIX}{user_hash}.json"
        )
        if not legacy.exists():
            return None
        latest = legacy
    return json.loads(latest.download_as_text())

def _set_patient_context(
    user_hash: str, patient_folder: str, hn_display: str, patient_name: str,
) -> bool:
    """ตั้งผู้ป่วยปัจจุบันของเจ้าหน้าที่คนนี้"""
    try:
        if not storage.is_ready or not hasattr(storage, "_bucket"):
            logger.warning("Storage not ready for patient context")
            return False
        _patient_ctx_write(user_hash, {
            "timestamp": time.time(),
            "patient_folder": patient_folder,
            "hn_display": hn_display,
            "patient_name": patient_name,
        })
        return True
    except Exception as e:
        logger.warning(f"Failed to set patient context: {type(e).__name__}")
        return False


def _get_patient_context(user_hash: str) -> dict | None:
    """ดึงผู้ป่วยปัจจุบัน — คืน None ถ้าไม่มีหรือหมดเวลา"""
    try:
        if not storage.is_ready or not hasattr(storage, "_bucket"):
            return None
        data = _patient_ctx_read(user_hash)
        if data is None:
            return None
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
        _patient_ctx_write(user_hash, {"cleared": True, "timestamp": 0})
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
    # แฟ้มผู้ป่วยเดิมของเอกสาร (ใบยา ↔ ใบฟอกเลือด ของคนเดียวกัน — แก้แค่ประเภท)
    _existing_folder = ""
    if old_category in CLINICAL_CATEGORIES:
        try:
            _existing_folder = (
                doc_index.derive_patient_folder(doc)
                if hasattr(doc_index, "derive_patient_folder")
                else (doc.get("patient_hn_folder") or "")
            ).strip()
        except Exception:
            _existing_folder = ""

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
        elif _existing_folder:
            # 18 พ.ย. 2026: ใบในชุดหลายรูปที่เข้าแฟ้มผู้ป่วยจากใบอื่นในชุด
            # (หน้านั้นอ่าน HN ไม่ได้) เปลี่ยนประเภทไม่ได้เพราะหา HN ในข้อความไม่เจอ
            # → คงแฟ้มผู้ป่วยเดิมไว้ ไม่ต้องพิมพ์ HN ซ้ำ
            patient_folder = _existing_folder
            patient_label = _existing_folder
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

        doc_index.update_category(
            doc_id, new_category, new_blob_path,
            extra_fields=({"patient_hn_folder": patient_folder} if patient_folder else None),
        )

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

        cat_names = _CAT_NAMES  # ตารางชื่อประเภทรวมที่เดียว
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

        cat_names = _CAT_NAMES  # ตารางชื่อประเภทรวมที่เดียว
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
        # Auto-register: ลงทะเบียนผู้ป่วยอัตโนมัติจาก HN ที่ระบุ
        # เพื่อให้ย้ายเอกสารได้ทันที — เอกสารต่อไปจะจัดเข้าแฟ้มนี้อัตโนมัติ
        try:
            _ok, _msg = master_registry.register(
                hospital_hn=hn,
                registered_by="auto_move",
                notes="auto-registered from document move command",
            )
            if _ok:
                patient = master_registry.match_hn(hn)
                logger.info("Auto-registered HN %s for document move", hn)
        except Exception as _reg_err:
            logger.warning("Auto-register failed for HN %s: %s", hn, _reg_err)
        if patient is None:
            return {"error": f"ไม่พบ HN {hn} ในทะเบียน — ลงทะเบียนอัตโนมัติไม่สำเร็จ"}

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


def _reconcile_patient_folder_index() -> dict:
    """ซ่อม index ให้ตรงกับไฟล์จริงในแฟ้มผู้ป่วยบน GCS

    ที่มา: การย้ายเอกสารเข้าแฟ้มผู้ป่วยย้อนหลัง (retroactive routing /
    ย้ายเอกสาร) คัดลอกไฟล์สำเร็จ แต่การเขียน index ล้มเหลวเงียบ ๆ (403)
    → ไฟล์อยู่ในแฟ้มผู้ป่วย (คอมเห็น) แต่ index ไม่รู้ว่าเป็นของ HN ไหน
    (LIFF/ค้นหาไม่เห็น)  วิธีซ่อม: ไล่ไฟล์จริงใน 03/04/05 ที่อยู่ในแฟ้ม
    <HN>_<ชื่อ> แล้วอัปเดต index (versioned) ให้ชี้ path นั้น + patient_hn_folder
    """
    out = {"scanned_files": 0, "fixed": 0, "already_ok": 0, "no_index": 0, "errors": 0}
    if not storage.is_ready:
        out["error"] = "storage not ready"
        return out
    doc_index._ensure_fresh()
    by_file: dict[str, tuple[str, str]] = {}
    for cat in ("lab", "rx", "dialysis"):
        prefix = FOLDERS.get(cat, "") + "/"
        try:
            for b in storage._bucket.list_blobs(prefix=prefix):
                parts = b.name.split("/")
                if len(parts) != 5:
                    continue  # ไม่ได้อยู่ในแฟ้มผู้ป่วย
                out["scanned_files"] += 1
                by_file[parts[4]] = (b.name, parts[3])
        except Exception as e:
            logger.warning("reconcile list failed (%s): %s", cat, type(e).__name__)
            out["errors"] += 1
    for doc in list(doc_index._doc_map.values()):
        if doc.get("category") not in ("lab", "rx", "dialysis"):
            continue
        p = doc.get("gcs_object_path", "") or ""
        fname = p.rsplit("/", 1)[-1]
        hit = by_file.get(fname)
        if not hit:
            out["no_index"] += 1
            continue
        new_path, folder = hit
        cur = p.split("/", 3)[3] if p.startswith("gs://") and p.count("/") >= 3 else p
        if cur == new_path and doc.get("patient_hn_folder") == folder:
            out["already_ok"] += 1
            continue
        if doc_index.update_fields(doc.get("document_id", ""), {
            "gcs_object_path": new_path, "patient_hn_folder": folder,
        }):
            out["fixed"] += 1
        else:
            out["errors"] += 1
    logger.info("reconcile patient folder index: %s", out)
    return out


def _parse_move_bill(text: str):
    """คำสั่งย้ายบิลไปแฟ้มบริษัท: "ย้ายบิล <รหัสเอกสาร> <ชื่อแฟ้มบริษัท>"

    รองรับ: ย้ายบิล a1b2c3d4 CENTRAL_POLY / ย้ายใบเสร็จ … / move bill …
    คืน (doc_id_prefix, canonical) หรือ None
    """
    m = re.match(
        r"^\s*(?:ย้ายบิล|ย้ายใบเสร็จ|move\s*bill)\s+([0-9a-fA-F-]{6,36})"
        r"\s+([A-Za-z0-9_]{2,40})\s*$",
        (text or "").strip(),
    )
    if not m:
        return None
    return m.group(1), m.group(2).upper()


def _move_bill_to_vendor(
    doc_id_prefix: str, canonical: str, actor_hash: str,
) -> dict:
    """ย้ายบิล/ใบเสร็จไปแฟ้มบริษัทที่ถูกต้อง (เช่น จาก _UNIDENTIFIED)

    เหตุผล: OCR อ่านชื่อบริษัทบนสลิปโอนเงินไม่ได้ → เข้า _UNIDENTIFIED
    เดิมไม่มีทางแก้ (ส่งซ้ำก็ถูกกันซ้ำ)  ใช้ GCS server-side copy เหมือน
    การย้ายเอกสารผู้ป่วย: ไม่ลบต้นฉบับ, index ชี้ไปที่ใหม่, ลง audit
    """
    if not storage.is_ready:
        return {"error": "ระบบจัดเก็บยังไม่พร้อม"}
    doc, err = _find_doc_by_prefix(doc_id_prefix)
    if doc is None:
        return {"error": err}
    if doc.get("category") != "bills":
        return {"error": (
            f"เอกสารนี้เป็นประเภท '{doc.get('category', '')}' "
            "ไม่ใช่บิล/ใบเสร็จ"
        )}
    vendor = vendor_registry.get(canonical) if vendor_registry else None
    if vendor is None or not vendor.active:
        return {"error": (
            f"ไม่พบบริษัท {canonical} ในทะเบียน "
            "(ใช้ชื่อแฟ้ม เช่น CENTRAL_POLY, PINYO_PHARMACY)"
        )}
    gcs_path = doc.get("gcs_object_path", "")
    blob_path = (
        gcs_path.split("/", 3)[3]
        if gcs_path.startswith("gs://") and gcs_path.count("/") >= 3
        else gcs_path
    )
    if not blob_path:
        return {"error": "เอกสารนี้ไม่มีไฟล์ใน cloud storage"}
    doc_dt = _doc_submitted_date(doc)
    filename = blob_path.rsplit("/", 1)[-1]
    new_blob_path = build_document_path(
        FOLDERS.get("bills", FOLDERS["review"]), filename, "bills",
        subfolder=vendor.canonical_name, date=doc_dt,
    )
    if new_blob_path == blob_path:
        return {"status": "unchanged",
                "message": f"บิลนี้อยู่ในแฟ้ม {vendor.canonical_name} อยู่แล้ว"}
    try:
        src = storage._bucket.blob(blob_path)
        if not src.exists():
            return {"error": "ไม่พบไฟล์ต้นฉบับใน cloud storage"}
        dest = storage._bucket.blob(new_blob_path)
        if not dest.exists():
            storage._bucket.copy_blob(src, storage._bucket, new_blob_path)
        ok = doc_index.update_category(
            doc.get("document_id", ""), "bills", new_blob_path,
            extra_fields={
                "bill_company": vendor.canonical_name,
                "vendor_method": "manual",
                "vendor_needs_review": False,
            },
        )
        if not ok:
            return {"error": "คัดลอกไฟล์แล้ว แต่บันทึก index ไม่สำเร็จ"}
        audit_logger.log("bill_refiled", actor_hash, {
            "doc_id": doc.get("document_id", "")[:8],
            "to_vendor": vendor.canonical_name,
        })
        return {
            "status": "moved",
            "message": (
                f"✅ ย้ายบิลเรียบร้อย\n"
                f"📄 รหัส: {doc.get('document_id', '')[:8]}\n"
                f"🏢 แฟ้มใหม่: 02_BILLS_RECEIPTS/{vendor.canonical_name}\n\n"
                "หมายเหตุ: ไฟล์เดิมใน _UNIDENTIFIED ยังอยู่ "
                "ระบบชี้ไปที่แฟ้มใหม่แล้ว"
            ),
        }
    except Exception as e:
        logger.error("Bill re-file failed: %s", type(e).__name__)
        return {"error": f"ย้ายบิลไม่สำเร็จ ({type(e).__name__})"}


# /tmp metadata structure (per user_hash):
# {
#   "pages": [{"ext": ".jpg", "sha256": "...", "doc_id": "...", "filename": "..."}, ...],
#   "line_user_id": str,
#   "last_image_ts": float (time.time())
# }
# Page image data: /tmp/ranong_raktai/multipage/{user_hash}_p{N}.dat


def _ocr_batch_page(data: bytes, ext: str) -> str:
    """OCR หนึ่งรูปในชุดหลายรูป — ใช้เส้นทางเดียวกับการส่งรูปเดี่ยว

    18 พ.ย. 2026 (HN 68408): ส่ง 4 รูป (ใบฟอกเลือด 3 + ใบยา 1) ระบบจัดเป็น
    ใบยา 2 + ฟอกเลือด 2 — สาเหตุหลัก: ชุดหลายรูปใช้ _ocr_from_bytes
    (Tesseract รอบเดียว ไม่ใช้ Google Vision ไม่หมุนรูปตาม EXIF/OSD)
    ขณะที่รูปเดี่ยวใช้ extract_text_with_ocr (Vision → Tesseract v3)
    ข้อความจากชุดจึงเพี้ยนกว่ามาก เครื่องหมายใบฟอกเลือดหาย เหลือแต่
    คำว่า Medication/Route/Heparin → ถูกจัดเป็นใบยา
    ตอนนี้ทั้งสองเส้นทางใช้ OCR ชุดเดียวกัน ถ้าได้ข้อความว่างจึงถอยไปใช้แบบเดิม
    """
    suffix = ext if ext in (".jpg", ".jpeg", ".png") else ".jpg"
    text = ""
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        text = extract_text_with_ocr(tmp_path) or ""
    except Exception as e:
        logger.warning("batch OCR (vision/v3) ล้มเหลว: %s", type(e).__name__)
        text = ""
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    if len(text.strip()) < 5 and HAS_TESSERACT and HAS_PIL:
        try:
            text = _ocr_from_bytes(data, "image")
        except Exception as e:
            logger.warning("batch OCR fallback ล้มเหลว: %s", type(e).__name__)
    return (text or "").strip()


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

    # ══ หลายรูปในหน้าต่างเดียว ══════════════════════════════════
    # เดิม: เก็บเฉพาะรูปหน้าแรก (หน้า 2+ เหลือแต่ข้อความ OCR → รูปหาย)
    # ใหม่: จัดประเภทจากหน้าแรกก่อน แล้วเลือกโหมดอัตโนมัติ
    #   A) lab        → รวมทุกหน้าเป็น PDF เดียว (ครบทุกรูป)
    #   B) อื่น ๆ      → แยกเป็นเอกสารคนละไฟล์ ผูก batch_id เดียวกัน
    #      (ใบฟอกเลือด/ใบยา/บิล แต่ละใบคือเอกสารคนละชุด)
    # ถ้าสร้าง PDF ไม่ได้ (ไม่มี PyMuPDF/ผิดพลาด) → fallback โหมด B เสมอ
    # เพื่อไม่ให้รูปหาย
    logger.info(
        f"Multi-image batch: user={user_hash[:8]}…, images={actual_count}"
    )

    # (ไม่ push "กำลังตรวจประเภท" แยกอีก — ข้อความถัดไปบอกผลอยู่แล้ว
    #  ประหยัดโควต้า push ของ LINE OA 1 ข้อความต่อชุด)

    # OCR ทุกหน้า (ใช้ทั้งจัดประเภทและข้อความรวม)
    page_texts: list[str] = []
    for i, (page_data, page_ext, _, _, _) in enumerate(pages_data, 1):
        page_text = ""
        try:
            page_text = _ocr_batch_page(page_data, page_ext)
        except Exception as ocr_err:
            logger.warning(
                f"Multi-image OCR ล้มเหลว หน้า {i}: "
                f"{type(ocr_err).__name__}"
            )
        page_texts.append(page_text.strip())

    # จัดประเภทจากหน้าแรก (ไม่ใช้ External AI — keyword classifier เดิม)
    first_category = "review"
    try:
        _first_tmp = save_to_inbox(pages_data[0][0], pages_data[0][4])
        try:
            _cls = classify_with_keywords(_first_tmp, override_text=page_texts[0] or None)
            first_category = (_cls or {}).get("category", "review") or "review"
        finally:
            try:
                os.remove(_first_tmp)
            except OSError:
                pass
    except Exception as cls_err:
        logger.warning(
            f"Multi-image classify ล้มเหลว: {type(cls_err).__name__} — ใช้โหมดแยกไฟล์"
        )

    _cat_label = _CAT_NAMES.get(first_category, first_category)
    merge_mode = "pdf" if first_category == "lab" else "separate"

    # ── โหมด A: ผลแล็บ → รวมทุกหน้าเป็น PDF เดียว ──
    if merge_mode == "pdf":
        pdf_bytes = None
        if HAS_PYMUPDF:
            try:
                _pdf = pymupdf.open()
                for page_data, page_ext, _, _, _ in pages_data:
                    _img = pymupdf.open(stream=page_data, filetype=page_ext.lstrip("."))
                    _rect = _img[0].rect
                    _pg = _pdf.new_page(width=_rect.width, height=_rect.height)
                    _pg.insert_image(_rect, stream=page_data)
                    _img.close()
                pdf_bytes = _pdf.tobytes(garbage=3, deflate=True)
                _pdf.close()
            except Exception as pdf_err:
                logger.warning(
                    f"Multi-image PDF merge ล้มเหลว: {type(pdf_err).__name__} "
                    f"— fallback แยกไฟล์"
                )
                pdf_bytes = None
        if pdf_bytes:
            primary_doc_id = pages_data[0][3]
            pdf_filename = f"{primary_doc_id}.pdf"
            pdf_filepath = save_to_inbox(pdf_bytes, pdf_filename)
            page_shas = [p[2] for p in pages_data]
            combined_sha256 = hashlib.sha256(
                "|".join(page_shas).encode("utf-8")
            ).hexdigest()
            merged_text = "\n\n".join(
                f"[หน้า {i}/{actual_count}]\n{t or '(ไม่สามารถอ่านข้อความได้)'}"
                for i, t in enumerate(page_texts, 1)
            )
            # ไม่ push "รวมเป็น PDF…" — ผลลัพธ์สุดท้ายจะบอกจำนวนหน้าที่รวมเอง
            logger.info(
                f"Multi-image → PDF: pages={actual_count}, "
                f"bytes={len(pdf_bytes)}, chars={len(merged_text)}"
            )
            process_file_async(
                pdf_filepath,
                None,  # reply_token หมดอายุแล้ว — ใช้ push_message แทน
                doc_id=primary_doc_id,
                sha256_hex=combined_sha256,
                owner_hash=user_hash,
                line_user_id=line_user_id,
                merged_ocr_text=merged_text,
                merged_page_count=actual_count,
                submitted_at_ms=submitted_at_ms,
                page_sha256s=page_shas,
            )
            return
        merge_mode = "separate"  # fallback

    # ── โหมด B: แยกเป็นเอกสารคนละไฟล์ (ผูก batch_id) ──
    batch_id = f"BATCH-{pages_data[0][3][:8]}-{actual_count}"
    # ไม่ push "กำลังประมวลผล" — ข้อความถัดไปคือสรุปทั้งชุด (ภายใน ~1–2 นาที)
    logger.info(f"Multi-image → separate: n={actual_count}, batch={batch_id}")
    batch_folder = _batch_patient_folder(page_texts)
    _batch_register(batch_id, actual_count, line_user_id, owner_hash=user_hash)
    for idx, (data, ext, sha256_hex, doc_id, filename) in enumerate(pages_data):
        filepath = save_to_inbox(data, filename)
        process_file_async(
            filepath, None, doc_id=doc_id, sha256_hex=sha256_hex,
            owner_hash=user_hash, line_user_id=line_user_id,
            # ใช้ข้อความ OCR ที่อ่านแล้ว — ไม่ OCR ซ้ำ (ประหยัด CPU บน 512MB)
            merged_ocr_text=(page_texts[idx] or None),
            merged_page_count=0,
            submitted_at_ms=submitted_at_ms,
            batch_id=batch_id,
            batch_patient_folder=batch_folder,
            batch_index=idx + 1,
        )


# ═══════════════════════════════════════════════════════════
# สรุปผลชุดหลายรูปในข้อความเดียว (2026-11-02 แพทย์อนุมัติ)
#
# เดิม: ชุด 8 รูป → push หาผู้ส่ง 8 ข้อความ + push หาผู้รับแจ้ง 8 ครั้ง
#       (ข้อความ + Flex ต่อใบ) → อ่านยาก และกินโควต้า push ของ LINE OA
# ใหม่: ผู้ส่งได้ "สรุปชุด" 1 ข้อความ · ผู้รับแจ้งได้ 1 push
#       (ข้อความสรุป + carousel ปุ่มเปิดเอกสาร/สั่งการรักษา ≤ 10 ใบ)
#
# ขอบเขต: เฉพาะโหมดหลายรูปแยกไฟล์ (batch_id) — รูปเดียว / PDF แล็บ
# ไม่เปลี่ยน · ถ้าใบใดจำแนกไม่ได้ ระบบยังถามผู้ส่งทันทีเหมือนเดิม
# (ต้องการคำตอบภายใน 5 นาที) แล้วระบุในสรุปว่าใบนั้นรอคำตอบ
#
# collector อยู่ใน memory ของ worker ที่แจกงาน — ทุกใบในชุดถูกประมวลผล
# ใน worker เดียวกัน (thread) จึงไม่ต้องแชร์ข้าม worker
# ตัวจับเวลาสำรอง: ถ้าใบใดค้าง/ล้ม ไม่รายงาน จะส่งสรุปเท่าที่มีภายใน
# _BATCH_SUMMARY_TIMEOUT วินาที — ไม่มีทางที่ผู้ส่งจะไม่ได้ข้อความเลย
# ═══════════════════════════════════════════════════════════

_BATCH_SUMMARY_TIMEOUT = int(os.getenv("BATCH_SUMMARY_TIMEOUT_SECS", "300"))
_batch_collectors: dict[str, dict] = {}
_batch_lock = threading.Lock()


def _batch_register(
    batch_id: str, total: int, line_user_id: str, owner_hash: str = "",
) -> None:
    """เริ่มเก็บผลของชุด — เรียกก่อนแจกงานทุกใบ"""
    if not batch_id or total <= 0:
        return
    timer = threading.Timer(
        _BATCH_SUMMARY_TIMEOUT, _batch_flush, args=[batch_id, True],
    )
    timer.daemon = True
    with _batch_lock:
        _batch_collectors[batch_id] = {
            "total": total,
            "items": [],
            "bubbles": [],
            "line_user_id": line_user_id or "",
            "owner_hash": owner_hash or "",
            "timer": timer,
            "started": time.time(),
        }
    timer.start()


def _batch_record(batch_id: str, item: dict, bubble: dict | None = None) -> bool:
    """บันทึกผลของใบหนึ่งในชุด — คืน True ถ้าชุดนี้ถูกเก็บรวม (ผู้เรียก
    ไม่ต้อง push ผลรายใบ) · คืน False ถ้าไม่มี collector (ส่งแบบเดิม)"""
    if not batch_id:
        return False
    with _batch_lock:
        col = _batch_collectors.get(batch_id)
        if col is None:
            return False
        col["items"].append(dict(item))
        if bubble:
            col["bubbles"].append(bubble)
        done = len(col["items"]) >= col["total"]
    if done:
        _batch_flush(batch_id, False)
    return True


_BATCH_SHORT_CAT = {
    "เอกสารฟอกเลือด": "ฟอกเลือด", "ผลตรวจแล็บ": "แล็บ", "ใบสั่งยา": "ใบยา",
    "ใบเสร็จ/บิล": "บิล", "รอตรวจสอบ": "รอตรวจ",
}


# ใบยา ↔ ใบฟอกเลือด คือคู่ที่จัดสลับกันบ่อยที่สุด — ปุ่มแก้ประเภทกดครั้งเดียว
_FIX_TO = {"rx": ("dialysis", "ใบฟอกเลือด", "ฟอกเลือด"),
           "dialysis": ("rx", "ใบยา", "ใบยา")}
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _batch_num(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return _CIRCLED[n - 1] if 1 <= n <= len(_CIRCLED) else f"({n})"


def _batch_fix_quick_items(col: dict) -> list[dict]:
    """ปุ่ม quick reply แก้ประเภทใบยา ↔ ใบฟอกเลือด ของแต่ละรูปในชุด
    ใบที่ระบบไม่แน่ใจขึ้นก่อน · LINE จำกัด 13 ปุ่ม"""
    ok = [i for i in col.get("items", [])
          if i.get("status") == "ok" and i.get("category") in _FIX_TO and i.get("doc8")]
    ok.sort(key=lambda i: (0 if i.get("ambiguous") else 1, i.get("n", 0)))
    items = []
    for i in ok[:13]:
        _to, _word, _short = _FIX_TO[i["category"]]
        _from = "ใบยา" if i["category"] == "rx" else "ฟอกเลือด"
        _n = i.get("n", "?")
        if i.get("ambiguous"):
            label = f"⚠️รูป{_n}→{_short}"
        else:
            label = f"รูป{_n} {_from}→{_short}"
            if len(label) > 20:
                label = f"รูป{_n}→{_short}"
        items.append({
            "type": "action",
            "action": {
                "type": "message",
                "label": label[:20],
                "text": f"เปลี่ยนประเภท {i['doc8']} {_word}",
            },
        })
    return items


def _can_recategorize(user_hash: str) -> bool:
    return bool(user_hash) and (user_hash in PHYSICIAN_USERS or user_hash in ADMIN_USERS)


def _batch_summary_text(col: dict, partial: bool) -> str:
    """ข้อความสรุปชุด — กระชับ: จัดกลุ่มตามแฟ้ม/บริษัท ไม่ไล่ทีละใบ
    (2026-11-02 แพทย์สั่ง: "สรุปอยู่ที่ 1–2 ข้อความ ไม่รกตา")"""
    items = sorted(col["items"], key=lambda i: i.get("n", 0))
    total = col["total"]
    ok = [i for i in items if i.get("status") == "ok"]
    ask = [i for i in items if i.get("status") == "ask"]
    err = [i for i in items if i.get("status") == "error"]

    if len(ok) == total:
        head = f"📄 สรุปชุด {total} รูป ✅ เข้าระบบครบ {total}/{total}"
    else:
        head = f"📄 สรุปชุด {total} รูป — เข้าระบบ {len(ok)}/{total}"
    lines = [head]
    if partial:
        lines.append("⏳ บางใบยังไม่เสร็จ — ดูเพิ่มที่ 'รายการล่าสุด'")

    # จัดกลุ่ม: แฟ้มผู้ป่วย / บริษัท / ยังไม่ระบุ
    groups: dict[str, list] = {}
    for i in ok:
        if i.get("clinical"):
            key = ("👤 " + i["folder"]) if i.get("folder") else "👤 ยังไม่ระบุผู้ป่วย"
        elif i.get("folder"):
            key = "🏢 " + i["folder"]
        else:
            key = "📁 " + (i.get("storage") or "").split("/")[0]
        groups.setdefault(key, []).append(i)

    def _counts(lst):
        c: dict[str, int] = {}
        for i in lst:
            cat = _BATCH_SHORT_CAT.get(i.get("cat", ""), i.get("cat", ""))
            k = f"{i.get('emoji', '📄')} {cat}"
            c[k] = c.get(k, 0) + 1
        return " · ".join(f"{k} ×{n}" if n > 1 else k for k, n in c.items())

    need_move_hint = False
    for key, lst in groups.items():
        lines.append(key)
        lines.append("   " + _counts(lst))
        if key.startswith("👤 ยังไม่ระบุ"):
            need_move_hint = True
            lines.append("   รหัส: " + " ".join(i.get("doc8", "") for i in lst))
    if ok:
        # รหัสตามลำดับรูป + ประเภท → รู้ทันทีว่ารูปไหนถูกจัดเป็นอะไร (18 พ.ย. 2026)
        lines.append("รหัส (ตามลำดับรูป): " + " ".join(
            f"{_batch_num(i.get('n', 0))}{i.get('emoji', '📄')}{i.get('doc8', '')}"
            for i in ok
        ))
    unsure = [i for i in ok if i.get("ambiguous")]
    if unsure:
        lines.append(
            "⚠️ ระบบไม่แน่ใจ ใบยา/ใบฟอกเลือด — โปรดเปิดดู: "
            + " ".join(f"{_batch_num(i.get('n', 0))}{i.get('doc8', '')}" for i in unsure)
        )
    if any(i.get("category") in _FIX_TO for i in ok):
        lines.append("🔁 จัดประเภทผิด? แพทย์กดปุ่มแก้ได้ทันที หรือพิมพ์ "
                     "'เปลี่ยนประเภท <รหัส> ใบฟอกเลือด'")
    if ask:
        lines.append(
            f"❓ รอคำตอบประเภท {len(ask)} ใบ (ตอบ 1–4 ตามคำถามด้านบน): "
            + " ".join(i.get("doc8", "") for i in ask)
        )
    if err:
        lines.append(
            f"❌ ล้มเหลว {len(err)} ใบ — ส่งใบนั้นใหม่: "
            + " ".join(f"รูปที่ {i.get('n', '?')}" for i in err)
        )
    if need_move_hint:
        lines.append("💡 'ย้ายเอกสาร <รหัส> <HN>' เพื่อเข้าแฟ้มรายคน")
    return "\n".join(lines)


def _batch_flush(batch_id: str, from_timer: bool) -> None:
    """ส่งสรุปชุด (ครั้งเดียว) ให้ผู้ส่ง + ผู้รับแจ้งเตือน"""
    with _batch_lock:
        col = _batch_collectors.pop(batch_id, None)
    if col is None:
        return  # ส่งไปแล้ว
    try:
        col["timer"].cancel()
    except Exception:
        pass
    partial = len(col["items"]) < col["total"]
    if from_timer and not col["items"]:
        logger.warning("batch %s: ไม่มีผลใด ๆ ภายในเวลา — ไม่ส่งสรุป", batch_id)
        return
    col["items"].sort(key=lambda i: i.get("n", 0))

    # ── Retroactive folder assignment ──────────────────────
    # ถ้ามีเอกสาร clinical ในชุดที่ได้ folder แล้ว → ใบอื่นที่ยังไม่มี
    # ให้ inherit folder เดียวกัน (เช่น ใบยาได้ HN แต่ใบฟอกไม่ได้)
    clinical_items = [i for i in col["items"]
                      if i.get("clinical") and i.get("status") == "ok"]
    folders_found = {i["folder"] for i in clinical_items if i.get("folder")}
    if len(folders_found) == 1:
        # ชุดนี้มี folder เดียว → assign ให้ใบที่ยังไม่มี
        shared_folder = folders_found.pop()
        orphans = [i for i in clinical_items if not i.get("folder")]
        if orphans:
            logger.info(
                "batch %s: retroactive assign %d orphan docs → folder %s",
                batch_id, len(orphans), shared_folder,
            )
            for orph in orphans:
                orph["folder"] = shared_folder
                # Update index metadata (patient_hn_folder) for each orphan doc
                _doc8 = orph.get("doc8", "")
                if _doc8 and doc_index:
                    try:
                        for _d in doc_index._docs:
                            _did = _d.get("document_id", "")
                            if str(_did).startswith(_doc8):
                                _d["patient_hn_folder"] = shared_folder
                                _d["hn_route_source"] = "batch_retroactive"
                                logger.info(
                                    "batch retroactive: updated doc %s → %s",
                                    _doc8, shared_folder,
                                )
                                break
                    except Exception as _e:
                        logger.warning(
                            "batch retroactive: failed for doc %s: %s",
                            _doc8, _e,
                        )

    text = _batch_summary_text(col, partial)
    sender = col.get("line_user_id", "")
    fix_items = []
    try:
        fix_items = _batch_fix_quick_items(col)
    except Exception:
        fix_items = []
    if sender:
        if fix_items and _can_recategorize(col.get("owner_hash", "")):
            # แพทย์/ผู้ดูแลส่งเอง → แนบปุ่มแก้ประเภทกับข้อความสรุป
            if not push_messages(sender, [{
                "type": "text", "text": text[:5000],
                "quickReply": {"items": fix_items},
            }]):
                push_message(sender, text)
        else:
            push_message(sender, text)

    # ผู้รับแจ้งเตือน (ไม่รวมผู้ส่งเอง) — ข้อความสรุป + carousel ปุ่มเปิดเอกสาร
    try:
        notif_ids = list(_alert_recipients.get("physician", []))
        if sender and sender in notif_ids:
            notif_ids.remove(sender)
        if not notif_ids:
            return
        is_staff = bool(col.get("owner_hash") and is_authorized_user(col["owner_hash"]))
        head = "📬 ได้รับเอกสารชุดใหม่" + ("จากเจ้าหน้าที่" if is_staff else "จากภายนอก")
        msgs: list[dict] = [{"type": "text", "text": head + "\n" + text}]
        bubbles = [b for b in col.get("bubbles", []) if b][:10]
        if bubbles:
            msgs.append({
                "type": "flex",
                "altText": f"📄 เอกสารชุดใหม่ {len(bubbles)} ใบ — กดเพื่อเปิด",
                "contents": {"type": "carousel", "contents": bubbles},
            })
        # ปุ่มแก้ประเภท (quick reply) แนบที่ข้อความสุดท้าย — เฉพาะผู้มีสิทธิ์แก้
        msgs_fix = msgs
        if fix_items:
            msgs_fix = [dict(m) for m in msgs]
            msgs_fix[-1]["quickReply"] = {"items": fix_items}

        def _send(rid) -> bool:
            try:
                _fix_ok = msgs_fix is not msgs and _can_recategorize(hash_user_id(rid))
            except Exception:
                _fix_ok = False
            if _fix_ok and push_messages(rid, msgs_fix):
                return True
            # ส่งแบบไม่มีปุ่ม (เดิม) — ปุ่มเสริมต้องไม่ทำให้สรุปชุดส่งไม่ถึง
            return push_messages(rid, msgs)
        sent = sum(1 for rid in notif_ids if _send(rid))
        logger.info(
            "batch %s: สรุป %d/%d ใบ → ผู้ส่ง=%s ผู้รับแจ้ง %d/%d ราย%s",
            batch_id, len(col["items"]), col["total"], bool(sender),
            sent, len(notif_ids), " (timeout)" if from_timer else "",
        )
    except Exception as e:
        logger.error("batch %s: แจ้งผู้รับล้มเหลว: %s", batch_id, type(e).__name__)


def _build_doc_flex_bubble(
    category: str, cat_name: str, storage_display: str,
    doc_id: str, dest: str, n: int = 0, ambiguous: str = "",
) -> dict | None:
    """Flex bubble ปุ่ม "เปิดเอกสาร" + "สั่งการรักษา" ของเอกสารหนึ่งใบ
    (ย้ายมาจากโค้ดแจ้งเตือนรายใบ — ใช้ร่วมกันทั้งรายใบและ carousel ของชุด)
    คืน None ถ้าสร้าง URL เปิดเอกสารไม่ได้"""
    doc_download_url = None
    if dest and dest.startswith("gs://"):
        parts = dest.split("/", 3)
        if len(parts) >= 4:
            doc_download_url = get_download_url(parts[3])
    if not doc_download_url:
        return None
    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    persistent = ""
    annotate = ""
    if base_url and admin_secret and doc_id:
        persistent = f"{base_url}/admin/doc/view/{doc_id}?secret={admin_secret}"
        annotate = f"{base_url}/admin/annotate/{doc_id}?secret={admin_secret}"
    if persistent and len(persistent) <= LINE_URI_ACTION_MAX_LENGTH:
        final_url, expiry_note = persistent, ""
    else:
        final_url = doc_download_url
        expiry_note = f"⏱️ ลิงก์หมดอายุใน {SIGNED_URL_EXPIRY // 60} นาที"
    emoji = {"bills": "🧾", "rx": "💊", "lab": "🔬", "dialysis": "🩸"}.get(category, "📄")
    footer = [{
        "type": "button", "style": "primary", "color": "#1DB446",
        "action": {"type": "uri", "label": "📎 เปิดเอกสาร", "uri": final_url},
    }]
    if annotate and len(annotate) <= LINE_URI_ACTION_MAX_LENGTH:
        footer.append({
            "type": "button", "style": "primary", "color": "#0066CC",
            "action": {"type": "uri", "label": "✏️ สั่งการรักษา", "uri": annotate},
            "margin": "sm",
        })
    # 18 พ.ย. 2026: ปุ่มแก้ประเภทใบยา ↔ ใบฟอกเลือด บนการ์ดของใบนั้นเลย
    # (ส่งคำสั่ง 'เปลี่ยนประเภท' ปกติ — ระบบตรวจสิทธิ์แพทย์/ผู้ดูแลเหมือนเดิม)
    if category in _FIX_TO and doc_id:
        _to, _word, _short = _FIX_TO[category]
        footer.append({
            "type": "button", "style": "secondary", "height": "sm", "margin": "sm",
            "action": {"type": "message", "label": f"🔁 เป็น{_word}"[:20],
                       "text": f"เปลี่ยนประเภท {doc_id[:8]} {_word}"},
        })
    _title = f"{emoji} {cat_name}"
    if n:
        _title = f"{_batch_num(n)} {_title}"
    body = [
        {"type": "text", "text": _title, "weight": "bold", "size": "lg"},
    ]
    if ambiguous:
        body.append({"type": "text", "text": "⚠️ ระบบไม่แน่ใจ ใบยา/ใบฟอกเลือด — โปรดเปิดดู",
                     "size": "xs", "color": "#B45309", "wrap": True, "margin": "sm"})
    body += [
        {"type": "text", "text": f"รหัส {doc_id[:8]}", "size": "xs",
         "color": "#888888", "margin": "sm"},
        {"type": "separator", "margin": "md"},
        {"type": "box", "layout": "vertical", "margin": "md", "contents": [{
            "type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": "จัดเก็บ:", "size": "sm",
                 "color": "#555555", "flex": 0},
                {"type": "text", "text": storage_display[:40], "size": "sm",
                 "color": "#111111", "align": "end", "wrap": True},
            ],
        }]},
    ]
    if expiry_note:
        body.append({"type": "text", "text": expiry_note, "size": "xxs",
                     "color": "#999999", "margin": "md"})
    return {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "contents": footer},
    }


_NAME_TITLES = ("นางสาว", "น.ส.", "นาย", "นาง", "ด.ช.", "ด.ญ.", "เด็กชาย", "เด็กหญิง",
                "พระ", "mr.", "mrs.", "ms.", "miss", "mr", "mrs", "ms")


def _name_core(name: str) -> str:
    n = (name or "").strip().lower()
    for t in _NAME_TITLES:
        if n.startswith(t):
            n = n[len(t):].strip()
            break
    return re.sub(r"[\s.\-_]+", "", n)


def _names_agree(registry_name: str, ocr_name: str) -> bool:
    """ชื่อในทะเบียนกับชื่อที่ OCR อ่านได้เป็นคนเดียวกันไหม (ทน OCR เพี้ยน 1–2 ตัวอักษร)"""
    a, b = _name_core(registry_name), _name_core(ocr_name)
    if len(a) < 3 or len(b) < 3:
        return False
    if a in b or b in a:
        return True
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def _pick_patient_for_doc(found_hns, ocr_name: str = ""):
    """เลือกผู้ป่วยของเอกสาร 1 ฉบับ — HN ต้องสอดคล้องกับชื่อบนเอกสาร
    → (MasterPatient | None, method, warning)

    16 พ.ย. 2026: OCR อ่านเลข HN ผิด 1 หลัก (67398 → 57398/67308) — HN คลินิก
    เป็นเลข 5 หลักช่วงเดียวกัน จึงอาจตรงกับผู้ป่วย "คนอื่น" ที่มีอยู่จริง
    กฎ: (1) HN ที่ชื่อตรงกัน ชนะ (2) ถ้าชื่อบนเอกสารตรงผู้ป่วยรายเดียวในทะเบียน
    แต่ HN ชี้คนอื่น → ใช้ตามชื่อ + เตือนให้ตรวจ (3) HN หลายคนแต่ไม่มีชื่อยืนยัน
    → ไม่เดา"""
    if not master_registry:
        return None, "", ""
    matches = []
    for hn in found_hns or []:
        try:
            if hasattr(master_registry, "match_hn"):
                p = master_registry.match_hn(hn)
            else:  # ทะเบียนรุ่นที่มีแค่ get_folder_for_hn
                _f = master_registry.get_folder_for_hn(hn)
                p = type("P", (), {"folder_name": _f, "patient_name": ""})() if _f else None
        except Exception:
            p = None
        if p and all(p.folder_name != m.folder_name for m in matches):
            matches.append(p)
    name_p = None
    if ocr_name:
        try:
            name_p = master_registry.search_by_name(ocr_name)
        except Exception:
            name_p = None
        agree = [m for m in matches if _names_agree(m.patient_name, ocr_name)]
        if len(agree) == 1:
            return agree[0], "hn+name", ""
        if name_p and all(name_p.folder_name != m.folder_name for m in matches):
            return name_p, "ocr_name", ("hn_name_mismatch" if matches else "")
    if len(matches) == 1:
        warn = ("name_mismatch" if ocr_name and matches[0].patient_name
                and not _names_agree(matches[0].patient_name, ocr_name) else "")
        return matches[0], "hn", warn
    if len(matches) > 1:
        return None, "ambiguous", "multi_hn"
    if name_p:
        return name_p, "ocr_name", ""
    return None, "", ""


def _batch_patient_folder(page_texts: list[str]) -> str:
    """หาแฟ้มผู้ป่วย (จากทะเบียน) ที่ทุกหน้าในชุดชี้ตรงกัน

    คืนชื่อแฟ้มเมื่อหน้าที่จับคู่ทะเบียนได้ทั้งหมดชี้ผู้ป่วย "รายเดียว"
    ถ้าไม่มีหน้าไหนจับคู่ได้ หรือชี้คนละราย (ส่งหลายผู้ป่วยในชุดเดียว)
    คืน "" — แต่ละหน้าจัดแฟ้มตามเดิม ไม่เดา
    """
    if not master_registry:
        return ""
    folders: set[str] = set()
    try:
        for text in page_texts:
            if not text:
                continue
            try:
                _nm = extract_patient_name_from_document(text)
            except Exception:
                _nm = ""
            _p, _how, _w = _pick_patient_for_doc(extract_hn_from_document(text), _nm)
            if _p:
                folders.add(_p.folder_name)
    except Exception as e:
        logger.warning("batch patient folder: %s", type(e).__name__)
        return ""
    if len(folders) == 1:
        folder = next(iter(folders))
        logger.info("Multi-image batch: single registered patient across pages")
        return folder
    if len(folders) > 1:
        logger.info(
            "Multi-image batch: %d different registered patients — "
            "no sibling routing", len(folders),
        )
    return ""


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

    # ── ชั้นที่ 4: กันซ้ำถาวร — เนื้อหาตรงกับเอกสารที่ผู้ส่งคนนี้เคยส่งแล้ว ──
    # (DedupGuard ด้านบนมี TTL 10 นาที; ชั้นนี้ตรวจ doc_index ซึ่งถาวร)
    # ตรงกันเฉพาะ sha256 100% เท่านั้น — ถ่ายใหม่ = ไฟล์ใหม่ = ไม่ถูกกัน
    try:
        _dup_doc = doc_index.find_by_content_sha(user_hash, sha256_hex)
    except Exception as _dup_err:
        logger.warning(f"ตรวจเอกสารซ้ำถาวรล้มเหลว: {type(_dup_err).__name__}")
        _dup_doc = None
    if _dup_doc:
        _dup_cat = _CAT_NAMES.get(_dup_doc.get("category", ""), "เอกสาร")
        _dup_date = _dup_doc.get("upload_date", "")
        _dup_folder = _dup_doc.get("patient_hn_folder", "")
        logger.info(
            f"ข้ามเอกสารซ้ำถาวร (sha={sha256_hex[:8]}… "
            f"doc={_dup_doc.get('document_id', '')[:8]})"
        )
        try:
            audit_logger.log("duplicate_document_blocked", user_hash, {
                "existing_doc": _dup_doc.get("document_id", "")[:8],
            })
        except Exception:
            pass
        _dup_lines = [
            "📎 เอกสารนี้อยู่ในระบบแล้ว — ไม่บันทึกซ้ำ",
            f"📁 ประเภท: {_dup_cat}",
        ]
        if _dup_date:
            _dup_lines.append(f"📅 รับเมื่อ: {_dup_date}")
        if _dup_folder:
            _dup_lines.append(f"👤 แฟ้มผู้ป่วย: {_dup_folder}")
        _dup_lines.append("💡 ถ้าเป็นเอกสารใหม่ กรุณาถ่ายภาพใหม่แล้วส่งอีกครั้ง")
        _safe_reply(reply_token, line_user_id, "\n".join(_dup_lines))
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
                        "กรุณาส่งเอกสารชุดใหม่หลังจากชุดนี้ประมวลผลเสร็จ",
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

                # ไม่ push "ได้รับหน้าที่ N" — ผู้ส่งได้ตอบรับที่รูปแรกแล้ว
                # และจะได้สรุปทั้งชุดในข้อความเดียว (ประหยัด 1 push/รูป)
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

                # ตอบรับไปแล้วตอน webhook (รูปแรกของชุด) — ไม่ push ซ้ำ
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

def _first_image_in_burst(user_hash: str, window_sec: int = 20) -> bool:
    """True เฉพาะรูปแรกที่ผู้ใช้ส่งภายในหน้าต่างสั้น ๆ (แชร์ข้าม worker ผ่าน
    state store) — ใช้ตัดสินว่าจะตอบรับรูปหรือเงียบ ถ้า state store ล่ม
    ให้ตอบรับทุกรูป (ปลอดภัยกว่าเงียบทั้งหมด)"""
    try:
        return bool(state_store.set_if_absent(f"imgack:{user_hash}", window_sec))
    except Exception:
        return True


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

        # V4.0: Generate job_id for every event
        _job_id = generate_job_id()

        event_id = event.get("webhookEventId", "")
        is_redelivery = bool(
            event.get("deliveryContext", {}).get("isRedelivery", False)
        )
        if not dedup_guard.is_new_event(event_id):
            event_logger.log("dedup_blocked", job_id=_job_id, data={
                "reason": "event_id", "redelivery": is_redelivery,
            })
            continue
        if is_redelivery:
            logger.info(f"event นี้เป็น redelivery (id={event_id[:8]}…)")

        _line_user_id = event.get("source", {}).get("userId", "")
        user_hash = hash_user_id(_line_user_id)

        # ── Auto-link Rich Menu ตาม role (Staff/Patient) ──
        if _line_user_id and _RICH_MENU_IDS.get("staff") and \
                (is_admin_user(user_hash) or is_physician_user(user_hash)):
            _auto_link_key = f"rm_linked_{_line_user_id}"
            if not getattr(app, '_rm_linked_cache', None):
                app._rm_linked_cache = set()
            if _auto_link_key not in app._rm_linked_cache:
                try:
                    import requests as _alr
                    _alr.post(
                        f"https://api.line.me/v2/bot/user/{_line_user_id}"
                        f"/richmenu/{_RICH_MENU_IDS['staff']}",
                        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                                 "Content-Type": "application/json"},
                        timeout=10)
                    app._rm_linked_cache.add(_auto_link_key)
                except Exception:
                    pass

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
                _src_img = event.get("source", {}) or {}
                _gid_img = _src_img.get("groupId") or _src_img.get("roomId") or ""
                if _gid_img and _gid_img == _nurse_group_id():
                    # กลุ่มพยาบาล: รูป = หลักฐานตรวจน้ำ RO (เก็บดิบ รอข้อความค่า) — ไม่เข้า pipeline เอกสารผู้ป่วย
                    threading.Thread(target=_water_store_group_image, args=(msg_id, user_hash), daemon=True).start()
                    continue
                logger.info(f"รับภาพ (id={msg_id})")
                event_logger.log("document_received", job_id=_job_id, data={
                    "type": "image", "msg_id": msg_id,
                })

                allowed, limit_msg = rate_limiter.check(user_hash)
                if not allowed:
                    event_logger.log("rate_limited", job_id=_job_id, data={
                        "user": user_hash[:8],
                    }, success=False)
                    reply_message(reply_token, limit_msg)
                    continue

                # [FIX] Async processing — ดาวน์โหลดและประมวลผลใน background thread
                # เพื่อให้ webhook return 200 ให้ LINE ทันก่อน timeout
                # ตอบรับเฉพาะรูปแรกของชุด (2026-11-02 แพทย์สั่งลดข้อความ):
                # รูปที่ตามมาภายใน 20 วิ ไม่ตอบ — ผลทั้งชุดจะอยู่ในสรุปเดียว
                if _first_image_in_burst(user_hash):
                    reply_message(
                        reply_token,
                        "📷 ได้รับรูปแล้ว — ถ้ามีหลายรูป ส่งต่อได้เลย\n"
                        "ระบบจะรวมเป็นชุดและสรุปผลในข้อความเดียว",
                    )
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

                # ── ว้าว #8: "@รักษ์ไต …" ในกลุ่มพยาบาล → ตอบจาก HD DB (read-only,
                #    ไม่ใช้ External AI, ตอบกลับในกลุ่มเดิมเท่านั้น) ──
                # ── PDPA: ผู้ป่วยที่ผูก LINE พิมพ์ "ถอนความยินยอม" / "หยุดส่งข้อความ" (แชท 1:1 เท่านั้น) ──
                _src_c = event.get("source", {}) or {}
                if not (_src_c.get("groupId") or _src_c.get("roomId")) and \
                        text.strip().replace(" ", "") in ("ถอนความยินยอม", "หยุดส่งข้อความ", "ยกเลิกรับข้อความ", "unsubscribe"):
                    try:
                        _pt = hn_registry.get_by_owner(user_hash)
                        if _pt and _pt.clinic_hn:
                            from hd_module.blueprint import patient_withdraw_consent
                            with app.app_context():
                                patient_withdraw_consent(_pt.clinic_hn, via="line")
                            reply_message(reply_token, "✅ บันทึกการถอนความยินยอมแล้ว ระบบจะหยุดส่งสรุป/แจ้งเตือนทาง LINE ให้ท่าน "
                                                       "การรักษาและนัดหมายที่คลินิกไม่เปลี่ยนแปลง หากต้องการรับข้อความอีกครั้ง แจ้งเจ้าหน้าที่ได้ครับ/ค่ะ")
                        else:
                            reply_message(reply_token, "บัญชีนี้ยังไม่ได้ผูกกับ HN จึงไม่มีการส่งข้อมูลส่วนบุคคลให้อยู่แล้วครับ/ค่ะ")
                    except Exception as _ce:
                        logger.warning(f"consent withdraw: {type(_ce).__name__}")
                        reply_message(reply_token, "ขออภัย บันทึกไม่สำเร็จ กรุณาแจ้งเจ้าหน้าที่ที่คลินิก")
                    continue

                # ── 17 พ.ย. 2569 (ข้อ 9): ผู้ป่วยที่ผูก LINE ส่ง "น้ำหนัก 62.4" → ตอบโควตาน้ำหนักถึงรอบถัดไป ──
                # + แพทย์/admin ทดสอบแทนคนไข้: "น้ำหนัก 65 HN 12345" (แชท 1:1 เท่านั้น)
                if not (_src_c.get("groupId") or _src_c.get("roomId")):
                    # ── parse: "น้ำหนัก 65" / "น้ำหนัก 65 HN 12345" ──
                    _wkg = None
                    _w_target_hn = None  # HN ที่ระบุ (admin/physician test mode)
                    try:
                        from hd_module.uf_coach import parse_weight_message as _pwm
                        _wkg = _pwm(text)
                        # admin/physician: "น้ำหนัก 65 HN 12345" → ทดสอบแทนคนไข้
                        if _wkg is None:
                            import re as _re_w
                            _m_adm = _re_w.match(
                                r"^\s*(?:น้ำหนัก|นน\.?|weight)\s+(\d{2,3}(?:[.,]\d{1,2})?)\s+"
                                r"(?:HN|hn)\s*(\d{3,6})\s*$", text or "")
                            if _m_adm:
                                _wkg = float(_m_adm.group(1).replace(",", "."))
                                _w_target_hn = _m_adm.group(2)
                                if not (30 <= _wkg <= 250):
                                    _wkg = None
                    except Exception as _imp_e:
                        logger.warning(f"[WEIGHT] parse import error: {type(_imp_e).__name__}: {str(_imp_e)[:120]}")
                        _wkg = None
                    logger.info(f"[WEIGHT] parse result: kg={_wkg}, target_hn={_w_target_hn}, text={text[:30]!r}")
                    if _wkg is not None:
                        # ── น้ำหนักถูก parse แล้ว → ต้องตอบทุกกรณี ห้ามเงียบ ──
                        _w_is_admin = (user_hash in ADMIN_USERS or is_physician_user(user_hash))
                        _w_hn = None  # HN ที่จะใช้จริง

                        if _w_target_hn and _w_is_admin:
                            # admin/physician ระบุ HN → ใช้ HN นั้น
                            _w_hn = _w_target_hn
                            logger.info(f"[WEIGHT] admin test mode: HN {_w_hn}")
                        else:
                            # ผู้ป่วย → ดู HN จาก LINE linking
                            _wpt = None
                            try:
                                _wpt = hn_registry.get_by_owner(user_hash)
                            except Exception as _wpt_e:
                                logger.warning(f"[WEIGHT] get_by_owner error: {type(_wpt_e).__name__}")
                                _wpt = None
                            if _wpt and getattr(_wpt, "clinic_hn", ""):
                                _w_hn = _wpt.clinic_hn
                                logger.info(f"[WEIGHT] patient linked: HN {_w_hn}")
                            else:
                                logger.info(f"[WEIGHT] not linked: user_hash={user_hash[:8]}")
                                reply_message(reply_token,
                                    "⚖️ รับทราบน้ำหนัก {:.1f} กก.\n\n"
                                    "แต่บัญชี LINE นี้ยังไม่ได้ผูกกับเลข HN\n"
                                    "กรุณาพิมพ์ 'ผูก <รหัส>' ที่ได้จากเจ้าหน้าที่ก่อนนะครับ/ค่ะ\n"
                                    "แล้วส่งน้ำหนักใหม่อีกครั้ง{}".format(
                                        _wkg,
                                        "\n\n💡 แพทย์/เจ้าหน้าที่ทดสอบ: พิมพ์ 'น้ำหนัก 65 HN 12345'" if _w_is_admin else ""))
                                continue

                        # ── มี HN แล้ว → เรียก weight_coach ──
                        _wreply = None
                        try:
                            from hd_module.blueprint import get_setting_value as _gsv_w
                            with app.app_context():
                                _on = (_gsv_w("patient_weight_coach", "on") or "on").lower() != "off"
                            logger.info(f"[WEIGHT] setting patient_weight_coach on={_on}")
                            if not _on:
                                _wreply = ("⚖️ รับทราบน้ำหนัก {:.1f} กก. แล้วครับ/ค่ะ\n"
                                           "ระบบโค้ชน้ำหนักยังไม่เปิดใช้งาน — เจ้าหน้าที่จะดูให้ในรอบฟอกถัดไป").format(_wkg)
                            else:
                                import psycopg2 as _pg_w
                                from hd_module.uf_coach import weight_coach as _wcoach
                                _dbw = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
                                if not _dbw:
                                    logger.error("[WEIGHT] no DATABASE_URL configured!")
                                    _wreply = "ขออภัย ระบบฐานข้อมูลยังไม่พร้อม กรุณาแจ้งเจ้าหน้าที่"
                                else:
                                    _cw = _pg_w.connect(_dbw, connect_timeout=5)
                                    try:
                                        with _cw.cursor() as _cur_w:
                                            _wreply, _wctx = _wcoach(_cur_w, _w_hn, _wkg,
                                                                     setting_pct=_gsv_w("idwg_limit_pct", ""),
                                                                     template=(_gsv_w("tpl_weight_reply", "") or None))
                                        _cw.commit()
                                        logger.info(f"[WEIGHT] weight_coach result: reply={'yes' if _wreply else 'None'}, ctx={'yes' if _wctx else 'None'}")
                                    finally:
                                        _cw.close()
                                    if not _wreply:
                                        # ผู้ป่วยผูก LINE แล้วแต่ยังไม่อยู่ในระบบ HD
                                        logger.info(f"[WEIGHT] patient HN {_w_hn} not in hd_patient_mapping")
                                        _wreply = ("⚖️ รับทราบน้ำหนัก {:.1f} กก. แล้วครับ/ค่ะ\n"
                                                   "ยังไม่พบข้อมูลไตเทียมของ HN {} ในระบบ — "
                                                   "เจ้าหน้าที่จะดูให้ในรอบฟอกถัดไป").format(_wkg, _w_hn)
                                    else:
                                        # เกินเกณฑ์ ≥ 1 กก. → แจ้งกลุ่มพยาบาล (วันละครั้งต่อผู้ป่วย)
                                        try:
                                            _iw = (_wctx or {}).get("idwg")
                                            _kday = f"wcoach_{_w_hn}_{datetime.now().strftime('%Y%m%d')}"
                                            if _iw and _iw["over_kg"] >= 1.0 and _kday not in getattr(app, "_wcoach_sent", set()):
                                                app._wcoach_sent = getattr(app, "_wcoach_sent", set()) | {_kday}
                                                from hd_module.blueprint import push_nurse_group as _png_w
                                                _png_w(f"⚖️ น้ำหนักจากบ้าน HN {_w_hn}: {_wkg:.1f} กก. "
                                                       f"(ขึ้น {_iw['idwg_kg']:+.1f} / เกณฑ์ {_iw['limit_kg']:.1f}) — เกิน {_iw['over_kg']:.1f} กก.")
                                        except Exception as _ne:
                                            logger.info(f"[WEIGHT] notify nurse: {type(_ne).__name__}")
                        except Exception as _we2:
                            logger.warning(f"[WEIGHT] error: {type(_we2).__name__}: {str(_we2)[:200]}")
                            _wreply = "ขออภัย บันทึกน้ำหนักไม่สำเร็จ ลองใหม่อีกครั้ง หรือแจ้งเจ้าหน้าที่ที่คลินิก"
                        logger.info(f"[WEIGHT] replying: {(_wreply or '(fallback)')[:60]}")
                        reply_message(reply_token, (_wreply or "⚖️ รับทราบน้ำหนักแล้ว")[:4900])
                        continue

                # ── ตรวจน้ำ RO ประจำวัน: "น้ำRO H 0 F 0.1 T 0" ในกลุ่มพยาบาล ──
                try:
                    from hd_module.water_check import parse_command as _wc_parse
                    _wc = _wc_parse(text)
                except Exception:
                    _wc = None
                if _wc is not None:
                    _src_w = event.get("source", {}) or {}
                    _gid_w = _src_w.get("groupId") or _src_w.get("roomId") or ""
                    if _gid_w and _gid_w == _nurse_group_id():
                        try:
                            _ans_w = _water_handle_command(_wc, text, user_hash, _line_user_id, _gid_w)
                        except Exception as _we:
                            logger.warning(f"water command: {type(_we).__name__}: {str(_we)[:120]}")
                            _ans_w = "ขออภัย บันทึกผลตรวจน้ำไม่สำเร็จ ลองใหม่อีกครั้ง"
                        reply_message(reply_token, _ans_w[:4900])
                        continue
                try:
                    from hd_module.group_assistant import match as _ga_match
                    _ga_body = _ga_match(text)
                except Exception:
                    _ga_body = None
                if _ga_body is not None:
                    _src = event.get("source", {}) or {}
                    _gid = _src.get("groupId") or _src.get("roomId") or ""
                    try:
                        from hd_module.blueprint import get_setting_value as _gsv
                        _nurse_gid = (_gsv("nurse_group_id", "") or "").strip()
                    except Exception:
                        _nurse_gid = ""
                    if _gid and _nurse_gid and _gid == _nurse_gid:
                        try:
                            import psycopg2 as _pg
                            from hd_module.group_assistant import answer as _ga_answer
                            _dbu = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
                            _c = _pg.connect(_dbu, connect_timeout=5)
                            try:
                                _ans = _ga_answer(_c, _ga_body)
                            finally:
                                _c.close()
                        except Exception as _ge:
                            logger.warning(f"group assistant: {_ge}")
                            _ans = "ขออภัย ตอบไม่ได้ตอนนี้ ลองใหม่อีกครั้ง"
                        reply_message(reply_token, _ans[:4900])
                        continue
                    # outside the nurse group: ignore silently (fall through to normal handling)

                # ── groupid: บอก groupId ของกลุ่มนี้ (ใช้ตั้ง nurse_group_id ให้
                #    HD Smart Monitoring ส่งคำสั่งแพทย์/สรุปเวรเข้ากลุ่มผ่าน
                #    Messaging API — LINE Notify ปิดบริการแล้ว) ──
                if text.strip().lower() in ("groupid", "group id", "ไอดีกลุ่ม"):
                    _src = event.get("source", {}) or {}
                    _gid = _src.get("groupId") or _src.get("roomId") or ""
                    if _gid:
                        reply_message(
                            reply_token,
                            "👥 Group ID ของกลุ่มนี้\n" + _gid +
                            "\n\nนำไปใส่ใน HD Smart Monitoring → ⚙️ ตั้งค่า → "
                            "nurse_group_id แล้วระบบจะส่งคำสั่งแพทย์/สรุปเวรเข้ากลุ่มนี้",
                        )
                    else:
                        reply_message(reply_token, "คำสั่งนี้ใช้ในกลุ่ม LINE เท่านั้น — เชิญ OA เข้ากลุ่มพยาบาลแล้วพิมพ์ groupid ในกลุ่ม")
                    continue

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
                    # รองรับหลายเอกสาร (batch) — ย้ายทุกใบ
                    _pending_docs = _pending.get("docs", [])
                    # Migrate old format (single doc)
                    if not _pending_docs and _pending.get("doc_id"):
                        _pending_docs = [_pending]
                    _doc_count = len(_pending_docs)
                    _cat_name = {"rx": "ใบสั่งยา", "lab": "ผลแล็บ",
                                 "dialysis": "ฟอกเลือด", "bills": "บิล"
                                 }.get(_new_cat, _new_cat)
                    reply_message(
                        reply_token,
                        f"⏳ กำลังย้ายเอกสาร {_doc_count} ใบ → {_cat_name}...",
                    )
                    _reclass_uid = _pending.get("line_user_id", "")
                    for _pdoc in _pending_docs:
                        # สร้าง pending-like dict สำหรับแต่ละ doc
                        _single = {
                            "doc_id": _pdoc.get("doc_id", ""),
                            "gcs_path": _pdoc.get("gcs_path", ""),
                            "owner_hash": _pending.get("owner_hash", ""),
                            "line_user_id": _reclass_uid,
                            "sha256_hex": _pdoc.get("sha256_hex", ""),
                        }
                        threading.Thread(
                            target=_reclassify_document,
                            args=(_single, _new_cat, _reclass_uid),
                            daemon=True,
                        ).start()
                    continue

                # ── Document Search V1: ตรวจคำสั่งค้นหาก่อน ──
                search_response = handle_search_command(
                    text, user_hash, _line_user_id,
                )
                if search_response:
                    if search_response == "__ALREADY_REPLIED__":
                        pass  # Flex Message ส่งไปแล้วจากใน handler
                    elif isinstance(search_response, dict) and search_response.get("type") == "flex":
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
                        # 17 พ.ย. 2569: รับทั้งรหัสแจ้งเตือนและรหัสเอกสาร (ข้อความสรุปผลแล็บแสดงรหัสเอกสาร)
                        try:
                            _ids = alert_service.find_alerts_by_ref(_ack_ref)
                        except Exception:
                            _full_id = alert_service.find_alert_id_by_prefix(_ack_ref)
                            _ids = [_full_id] if _full_id else []
                        if not _ids:
                            reply_message(
                                reply_token,
                                f"❌ ไม่พบการแจ้งเตือนที่รอรับทราบสำหรับ {_ack_ref}\n\n"
                                "อาจรับทราบไปแล้ว หรือรหัสไม่ถูกต้อง\n"
                                "พิมพ์ 'แจ้งเตือนค้าง' เพื่อดูรายการที่ยังค้าง",
                            )
                        else:
                            _ok, _err = [], []
                            for _full_id in _ids:
                                if _ack_action == "resolve":
                                    _res = alert_service.resolve_alert(_full_id, user_hash)
                                else:
                                    _res = alert_service.acknowledge_alert(_full_id, user_hash)
                                (_err if _res.get("error") else _ok).append(_res)
                            if len(_ids) == 1:
                                _r0 = (_ok or _err)[0]
                                reply_message(
                                    reply_token,
                                    f"❌ {_r0['error']}" if _r0.get("error")
                                    else _r0.get("message", "✅ บันทึกแล้ว"),
                                )
                            else:
                                _lines = [r.get("message", "✅ บันทึกแล้ว") for r in _ok]
                                _lines += [f"❌ {r['error']}" for r in _err]
                                reply_message(
                                    reply_token,
                                    f"📋 เอกสาร {_ack_ref[:8]} — {len(_ok)}/{len(_ids)} รายการ\n"
                                    + "\n".join(_lines),
                                )
                # ── ย้ายเอกสารที่เข้าแฟ้มผิดคน ──────────────────────
                # workflow จริงใช้เจ้าหน้าที่เลือกผู้ป่วยก่อนส่ง ถ้าลืม
                # เปลี่ยนผู้ป่วย เอกสารชุดถัดไปเข้าแฟ้มคนก่อนทั้งชุด
                # ก่อนหน้านี้ไม่มีวิธีแก้เลย ต้องส่งใหม่อย่างเดียว
                elif _parse_move_bill(text) is not None:
                    _mb_doc, _mb_vendor = _parse_move_bill(text)
                    if not (is_admin_user(user_hash) or is_physician_user(user_hash)):
                        reply_message(reply_token, "⚠️ เฉพาะแพทย์หรือผู้ดูแลระบบเท่านั้น")
                    else:
                        _mb = _move_bill_to_vendor(_mb_doc, _mb_vendor, user_hash)
                        reply_message(
                            reply_token,
                            _mb.get("message") or f"❌ {_mb.get('error', 'ย้ายไม่สำเร็จ')}",
                        )
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

                # ── วิเคราะห์ <HN> handler ถูกย้ายไป CLINICAL_ANALYSIS redirect
                # (ดู Phase 1 Consolidation — ลบ dead code) ──

                # ── เปรียบเทียบ <HN> — ส่ง link Compare page (เพิ่ม 2026-11-05) ──
                if text.strip().startswith(("เปรียบเทียบ ", "compare ")):
                    _cmp_q = text.strip()
                    for _pf in ("เปรียบเทียบ ", "compare "):
                        if _cmp_q.startswith(_pf):
                            _cmp_q = _cmp_q[len(_pf):].strip()
                            break
                    if not _cmp_q:
                        reply_message(reply_token,
                            "⚠️ กรุณาระบุ HN เช่น: เปรียบเทียบ 56001")
                    else:
                        _cmp_p = master_registry.match_hn(_cmp_q)
                        if not _cmp_p:
                            reply_message(reply_token,
                                f"❌ ไม่พบ HN: {_cmp_q} ในทะเบียน")
                        else:
                            _cmp_hn = _cmp_p.hospital_hn or _cmp_p.clinic_hn
                            _cmp_url = f"{_RENDER_BASE}/admin/compare/{_cmp_hn}"
                            _cmp_msg = (
                                f"📊 เปรียบเทียบข้อมูลผู้ป่วย\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"👤 {_cmp_p.patient_name}\n"
                                f"🔢 HN: {_cmp_hn}\n\n"
                                f"🔗 เปิดหน้าเปรียบเทียบ:\n{_cmp_url}\n\n"
                                f"💡 ต้อง login ด้วย Admin Secret ครั้งแรก"
                            )
                            reply_with_quick_reply(reply_token, _cmp_msg,
                                _patient_quick_reply_items(_cmp_hn))

                # ── ข้อมูลฟอกเลือด (เพิ่ม 2026-11-05) ──
                elif text.strip() in ("รอบฟอก", "รอบฟอกวันนี้", "ตารางฟอก", "ตารางฟอกวันนี้", "dialysis schedule"):
                    # แสดงตารางฟอกวันนี้
                    from patient_master_registry import THAI_DAY_DISPLAY
                    import datetime as _dt
                    _today = _dt.datetime.now(_dt.timezone(
                        _dt.timedelta(hours=7)  # ICT
                    ))
                    _day_map_rev = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
                    _day_en = _day_map_rev.get(_today.weekday(), "")
                    _day_th = THAI_DAY_DISPLAY.get(_day_en, "")
                    if _day_en == "sun":
                        reply_message(reply_token, "📅 วันอาทิตย์ — ไม่มีรอบฟอกเลือด")
                    else:
                        _pts = master_registry.get_patients_by_day(_day_en)
                        if not _pts:
                            reply_message(reply_token,
                                f"📅 วัน{_day_th} — ไม่มีคนไข้ในตาราง\n"
                                f"(ข้อมูลทะเบียน {master_registry.count()} คน)")
                        else:
                            _lines = [f"📅 รอบฟอกวัน{_day_th} ({len(_pts)} คน)\n"]
                            for i, _p in enumerate(_pts[:40], 1):
                                _esa = _p.esa_code or "-"
                                _va = _p.vascular_access or ""
                                _va_txt = f" [{_va}]" if _va else ""
                                _lines.append(
                                    f"{i}. {_p.clinic_hn} {_p.patient_name}"
                                    f" | {_esa}{_va_txt}"
                                )
                            if len(_pts) > 40:
                                _lines.append(f"\n...และอีก {len(_pts)-40} คน")
                            reply_message(reply_token, "\n".join(_lines))

                elif text.strip().startswith(("ข้อมูลฟอก ", "ข้อมูลฟอก")):
                    # ดูข้อมูลไตเทียมรายคน: ข้อมูลฟอก <HN>
                    _hn_q = text.strip()
                    for _pf in ("ข้อมูลฟอก ", "ข้อมูลฟอก"):
                        if _hn_q.startswith(_pf):
                            _hn_q = _hn_q[len(_pf):].strip()
                            break
                    if not _hn_q:
                        reply_message(reply_token,
                            "⚠️ กรุณาระบุ HN เช่น: ข้อมูลฟอก 56001")
                    else:
                        _dp = master_registry.match_hn(_hn_q)
                        if not _dp:
                            reply_message(reply_token,
                                f"❌ ไม่พบ HN: {_hn_q} ในทะเบียน")
                        else:
                            from patient_master_registry import THAI_DAY_DISPLAY as _TDD
                            _days_th = "/".join(
                                _TDD.get(d, d) for d in (_dp.dialysis_days or [])
                            ) or "-"
                            _esa_txt = _dp.esa_medication or _dp.esa_code or "-"
                            if _dp.esa_dose:
                                _esa_txt += f" {_dp.esa_dose} {_dp.esa_unit}"
                            _va_txt = _dp.vascular_access or "ยังไม่ระบุ"
                            _info = (
                                f"🏥 ข้อมูลฟอกเลือด\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"👤 {_dp.patient_name}\n"
                                f"🔢 HN คลินิก: {_dp.clinic_hn or '-'}\n"
                                f"🏥 HN รพ.: {_dp.hospital_hn or '-'}\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"📅 รอบฟอก: {_dp.dialysis_schedule or '-'} ({_days_th})\n"
                                f"🔄 HD/สัปดาห์: {_dp.hd_per_week or '-'}\n"
                                f"💊 ESA: {_esa_txt}\n"
                                f"🩸 Vascular Access: {_va_txt}\n"
                            )
                            if _dp.shift:
                                _info += f"⏰ กะ: {_dp.shift}\n"
                            if _dp.bed_number:
                                _info += f"🛏️ เตียง: {_dp.bed_number}\n"
                            reply_message(reply_token, _info)

                elif text.strip() in ("สรุปฟอก", "dialysis summary"):
                    # สรุปภาพรวมตารางฟอก
                    from patient_master_registry import THAI_DAY_DISPLAY as _TDD2
                    _summ = master_registry.get_dialysis_summary()
                    _lines = [
                        f"📊 สรุปไตเทียม ({_summ['with_schedule']}/{_summ['total_patients']} คนมีตาราง)\n",
                        "📅 จำนวนคนไข้ต่อวัน:",
                    ]
                    for _d in ["mon", "tue", "wed", "thu", "fri", "sat"]:
                        _lines.append(f"  {_TDD2.get(_d, _d)}: {_summ['by_day'].get(_d, 0)} คน")
                    _lines.append("\n💊 ยา ESA:")
                    for _code, _cnt in sorted(
                        _summ["esa_counts"].items(), key=lambda x: -x[1]
                    ):
                        _lines.append(f"  {_code}: {_cnt} คน")
                    if _summ.get("vascular_access_counts"):
                        _lines.append("\n🩸 Vascular Access:")
                        for _va, _cnt in sorted(
                            _summ["vascular_access_counts"].items(), key=lambda x: -x[1]
                        ):
                            _lines.append(f"  {_va}: {_cnt} คน")
                    reply_message(reply_token, "\n".join(_lines))

                elif text.strip().startswith(("ติดตามHb ", "ติดตามHb", "ติดตาม Hb ", "hb trend ")):
                    # ดูแนวโน้ม Hb กับ ESA: ติดตามHb <HN>
                    _hb_q = text.strip()
                    for _pf in ("ติดตามHb ", "ติดตามHb", "ติดตาม Hb ", "hb trend "):
                        if _hb_q.startswith(_pf):
                            _hb_q = _hb_q[len(_pf):].strip()
                            break
                    if not _hb_q:
                        reply_message(reply_token,
                            "⚠️ กรุณาระบุ HN เช่น: ติดตามHb 56001")
                    else:
                        _hp = master_registry.match_hn(_hb_q)
                        if not _hp:
                            reply_message(reply_token,
                                f"❌ ไม่พบ HN: {_hb_q} ในทะเบียน")
                        else:
                            _hb_hist = _get_hb_history(_hb_q, limit=5)
                            _hb_tr = _hb_trend(_hb_hist)
                            _trend_emoji = {"rising": "↑ ขาขึ้น", "falling": "↓ ขาลง",
                                            "stable": "→ คงที่"}.get(_hb_tr, "? ไม่ทราบ")
                            _esa_txt = _hp.esa_medication or _hp.esa_code or "-"
                            if _hp.esa_dose:
                                _esa_txt += f" {_hp.esa_dose} {_hp.esa_unit}"
                            _msg_lines = [
                                f"📊 ติดตาม Hb — {_hp.patient_name}",
                                f"━━━━━━━━━━━━━━━━",
                                f"💊 ESA: {_esa_txt}",
                                f"🩸 Vascular: {_hp.vascular_access or 'ไม่ระบุ'}",
                                f"━━━━━━━━━━━━━━━━",
                            ]
                            if _hb_hist:
                                _msg_lines.append("📈 ค่า Hb (g/dL):")
                                for _h in _hb_hist:
                                    _assess = _hb_assessment(_h["value"])
                                    _marker = "🟢" if _assess == "เป้าหมาย" else (
                                        "🔴" if _assess == "ต่ำ" else "🟡")
                                    _msg_lines.append(
                                        f"  {_h['date']}: {_h['value']:.1f} {_marker} {_assess}"
                                    )
                                _msg_lines.append(f"\n📉 แนวโน้ม: {_trend_emoji}")
                                _latest = _hb_hist[-1]["value"]
                                if _latest < 10.0:
                                    _msg_lines.append("⚠️ Hb ต่ำกว่าเป้าหมาย — พิจารณาปรับ ESA")
                                elif _latest > 12.0:
                                    _msg_lines.append("⚠️ Hb สูงกว่าเป้าหมาย — พิจารณาลด ESA")
                            else:
                                _msg_lines.append("📈 ยังไม่มีผลแล็บ Hb ในระบบ")
                            reply_message(reply_token, "\n".join(_msg_lines))

                # ── พยากรณ์ Hb + แนะนำ ESA (Phase 3.3) ──
                elif text.strip().startswith(("พยากรณ์Hb ", "พยากรณ์Hb", "พยากรณ์ Hb ", "predictHb ")):
                    _pred_q = text.strip()
                    for _pf in ("พยากรณ์Hb ", "พยากรณ์Hb", "พยากรณ์ Hb ", "predictHb "):
                        if _pred_q.startswith(_pf):
                            _pred_q = _pred_q[len(_pf):].strip()
                            break
                    if not _pred_q:
                        reply_message(reply_token,
                            "⚠️ กรุณาระบุ HN เช่น: พยากรณ์Hb 56001")
                    else:
                        _hp = master_registry.match_hn(_pred_q)
                        if not _hp:
                            reply_message(reply_token,
                                f"❌ ไม่พบ HN: {_pred_q} ในทะเบียน")
                        else:
                            from hd_module.hb_predictor import (
                                HbPredictor, HbDataPoint, format_hb_prediction_flex,
                            )
                            _hb_hist = _get_hb_history(_pred_q, limit=10)
                            if len(_hb_hist) < 2:
                                reply_message(reply_token,
                                    f"⚠️ {_hp.patient_name} (HN {_pred_q})\n"
                                    f"ต้องมีผล Hb อย่างน้อย 2 ครั้ง\n"
                                    f"ปัจจุบันมี {len(_hb_hist)} ครั้ง")
                            else:
                                from datetime import datetime as _dt
                                _data_pts = []
                                for _h in _hb_hist:
                                    try:
                                        _d = _dt.strptime(_h["date"], "%Y-%m-%d")
                                    except Exception:
                                        continue
                                    _data_pts.append(HbDataPoint(
                                        date=_d,
                                        hb=_h["value"],
                                        esa_drug=getattr(_hp, 'esa_code', '') or '',
                                        esa_dose=float(getattr(_hp, 'esa_dose', 0) or 0),
                                    ))
                                predictor = HbPredictor()
                                prediction = predictor.predict(_data_pts)
                                if prediction:
                                    _flex = format_hb_prediction_flex(prediction)
                                    _flex_msg = {
                                        "type": "flex",
                                        "altText": f"พยากรณ์ Hb — {_hp.patient_name}",
                                        "contents": _flex,
                                    }
                                    push_messages(_line_user_id, [_flex_msg])
                                    already_replied = True
                                else:
                                    reply_message(reply_token,
                                        "❌ ไม่สามารถพยากรณ์ได้ — ข้อมูลไม่เพียงพอ")

                # ── ติดตาม eGFR / CKD Progression (เพิ่ม 2026-11-05) ──
                elif text.strip().startswith(("ติดตามeGFR ", "ติดตามeGFR", "ติดตาม eGFR ", "egfr trend ")):
                    _egfr_q = text.strip()
                    for _pf in ("ติดตามeGFR ", "ติดตามeGFR", "ติดตาม eGFR ", "egfr trend "):
                        if _egfr_q.startswith(_pf):
                            _egfr_q = _egfr_q[len(_pf):].strip()
                            break
                    if not _egfr_q:
                        reply_message(reply_token,
                            "⚠️ กรุณาระบุ HN เช่น: ติดตามeGFR 56001")
                    else:
                        _egfr_p = master_registry.match_hn(_egfr_q)
                        if not _egfr_p:
                            reply_message(reply_token,
                                f"❌ ไม่พบ HN: {_egfr_q} ในทะเบียน")
                        else:
                            try:
                                _egfr_hn = _egfr_p.clinic_hn or _egfr_q
                                # ดึง eGFR history จากเอกสาร lab
                                _egfr_hist = []
                                _egfr_docs = _get_patient_lab_docs(_egfr_p, _egfr_hn, limit=20)
                                import re as _re_egfr
                                for _ed in (_egfr_docs or []):
                                    _et = _ed.get("extracted_text", "")
                                    _em = _re_egfr.search(
                                        r'(?:eGFR|GFR)\s*[=:]\s*([\d.]+)',
                                        _et, _re_egfr.IGNORECASE
                                    )
                                    if _em:
                                        try:
                                            _ev = float(_em.group(1))
                                            if 1 <= _ev <= 200:
                                                _egfr_hist.append({
                                                    "date": _ed.get("upload_date", ""),
                                                    "value": _ev,
                                                })
                                        except ValueError:
                                            pass
                                if _egfr_hist:
                                    from lab_intelligence_agent import (
                                        analyze_ckd_progression,
                                    )
                                    _ckd_r = analyze_ckd_progression(_egfr_hist)
                                    _ckd_msg = (
                                        f"🏥 CKD Progression — {_egfr_p.patient_name}\n"
                                        f"━━━━━━━━━━━━━━━━\n"
                                        f"{_ckd_r.risk_assessment}\n"
                                    )
                                    if _ckd_r.months_to_dialysis:
                                        _ckd_msg += (
                                            f"\n⏱️ คาดว่าถึง eGFR 10: "
                                            f"~{_ckd_r.months_to_dialysis:.0f} เดือน"
                                        )
                                    _ckd_msg += (
                                        f"\n\n📊 ประวัติ eGFR ({len(_egfr_hist)} ครั้ง):"
                                    )
                                    for _eh in _egfr_hist[-6:]:
                                        _ckd_msg += f"\n  {_eh['date']}: {_eh['value']:.0f}"
                                    _ckd_msg += "\n\n⚕️ ข้อมูลประกอบ ต้องแพทย์พิจารณา"
                                    reply_with_quick_reply(reply_token, _ckd_msg,
                                        _patient_quick_reply_items(_egfr_hn))
                                else:
                                    reply_message(reply_token,
                                        f"ไม่พบค่า eGFR ในผลแล็บของ HN {_egfr_q}")
                            except Exception as _egfr_err:
                                logger.warning("eGFR tracking: %s", _egfr_err)
                                reply_message(reply_token,
                                    "ขออภัย ไม่สามารถวิเคราะห์ eGFR ได้ในขณะนี้")

                # ── ปรับยา / บันทึกการเปลี่ยนแปลงการรักษา (เพิ่ม 2026-11-06) ──
                elif text.strip().startswith(("ปรับยา ", "ปรับการรักษา ")):
                    _rx_text = text.strip()
                    for _pf in ("ปรับยา ", "ปรับการรักษา "):
                        if _rx_text.startswith(_pf):
                            _rx_text = _rx_text[len(_pf):].strip()
                            break
                    # รูปแบบ: ปรับยา <HN> <รายละเอียด>
                    # เช่น: ปรับยา 56001 เพิ่ม Epiao 4000→6000 เพราะ Hb ต่ำ
                    _rx_parts = _rx_text.split(None, 1)
                    if len(_rx_parts) < 2:
                        reply_message(reply_token,
                            "⚠️ รูปแบบ: ปรับยา <HN> <รายละเอียด>\n"
                            "ตัวอย่าง:\n"
                            "• ปรับยา 56001 เพิ่ม Epiao 4000→6000 เพราะ Hb ต่ำ\n"
                            "• ปรับยา 69453 หยุด amlodipine เพราะ BP ต่ำ\n"
                            "• ปรับยา 56001 เริ่ม IV iron เพราะ Ferritin ต่ำ")
                    else:
                        _rx_hn = _rx_parts[0]
                        _rx_detail = _rx_parts[1]
                        _rx_patient = master_registry.match_hn(_rx_hn)
                        if not _rx_patient:
                            reply_message(reply_token, f"❌ ไม่พบ HN: {_rx_hn} ในทะเบียน")
                        else:
                            try:
                                parsed = treatment_tracker.parse_treatment_text(_rx_detail)
                                category = treatment_tracker.detect_category(
                                    parsed.get("drug_name", ""))
                                change = treatment_tracker.record_change(
                                    patient_hn=_rx_patient.clinic_hn or _rx_hn,
                                    category=category,
                                    action=parsed.get("action", "other"),
                                    drug_name=parsed.get("drug_name", _rx_detail),
                                    old_value=parsed.get("old_value", ""),
                                    new_value=parsed.get("new_value", ""),
                                    reason=parsed.get("reason", ""),
                                )
                                _cat_th = TREATMENT_CATEGORIES.get(
                                    category, {}).get("name_th", category)
                                reply_message(reply_token,
                                    f"✅ บันทึกการปรับการรักษา\n"
                                    f"━━━━━━━━━━━━━━━━\n"
                                    f"🔢 HN: {change.patient_hn}\n"
                                    f"👤 {_rx_patient.patient_name}\n"
                                    f"💊 {change.drug_name}\n"
                                    f"🔄 {change.old_value} → {change.new_value}\n"
                                    f"📝 เหตุผล: {change.reason or '-'}\n"
                                    f"📂 หมวด: {_cat_th}\n"
                                    f"\n📅 ติดตามผล: {change.followup_date}\n"
                                    f"🔬 ตรวจ: {change.monitor_lab} "
                                    f"(เป้า {change.monitor_target})\n"
                                    f"\n💡 ID: {change.change_id}")
                            except Exception as _rx_err:
                                logger.warning("Treatment record: %s", _rx_err)
                                reply_message(reply_token,
                                    "ขออภัย ไม่สามารถบันทึกได้ในขณะนี้")

                # ── คำสั่งการรักษา <HN> — ดูคำสั่งที่ส่งจากหน้าเปรียบเทียบ (เพิ่ม 2026-11-06) ──
                elif text.strip().startswith(("คำสั่งการรักษา ", "ดูคำสั่ง ")):
                    _tn_q = text.strip()
                    for _pf in ("คำสั่งการรักษา ", "ดูคำสั่ง "):
                        if _tn_q.startswith(_pf):
                            _tn_q = _tn_q[len(_pf):].strip()
                            break
                    # match_hn จัดการ "HN" prefix ให้อัตโนมัติแล้ว
                    _tn_p = master_registry.match_hn(_tn_q)
                    if not _tn_p:
                        reply_message(reply_token, f"❌ ไม่พบ HN: {_tn_q} ในทะเบียน")
                    else:
                        _tn_hn = _tn_p.clinic_hn or _tn_q
                        try:
                            _tn_notes = []
                            _tn_prefix = f"_treatment_notes/{_tn_hn}/"
                            if storage.is_ready and hasattr(storage, '_bucket'):
                                _tn_blobs = storage._bucket.list_blobs(
                                    prefix=_tn_prefix)
                                for _tn_b in _tn_blobs:
                                    try:
                                        _tn_d = json.loads(
                                            _tn_b.download_as_text())
                                        _tn_notes.append(_tn_d)
                                    except Exception:
                                        pass
                            _tn_notes.sort(
                                key=lambda x: x.get("timestamp", 0),
                                reverse=True)

                            if not _tn_notes:
                                reply_message(reply_token,
                                    f"📋 HN: {_tn_hn} ({_tn_p.patient_name})\n"
                                    f"ยังไม่มีคำสั่งการรักษาในระบบ")
                            else:
                                _tn_lines = [
                                    "📋 คำสั่งการรักษา",
                                    "━━━━━━━━━━━━━━━━",
                                    f"🔢 HN: {_tn_hn}",
                                    f"👤 {_tn_p.patient_name}",
                                    f"📄 ทั้งหมด {len(_tn_notes)} รายการ",
                                    "",
                                ]
                                for _i, _n in enumerate(_tn_notes[:5], 1):
                                    _dt = _n.get("created_at", "")[:10]
                                    _act = _n.get("action", "")
                                    _note = _n.get("note", "")[:80]
                                    _dtype = _n.get("doc_type", "")
                                    _act_th = {
                                        "treatment": "💊 สั่งการรักษา",
                                        "edit": "✏️ แก้ไข",
                                    }.get(_act, f"📝 {_act}")
                                    _tn_lines.append(
                                        f"{_i}. [{_dt}] {_act_th}")
                                    if _dtype:
                                        _tn_lines.append(f"   📂 {_dtype}")
                                    _tn_lines.append(f"   {_note}")
                                    _tn_lines.append("")

                                if len(_tn_notes) > 5:
                                    _tn_lines.append(
                                        f"... อีก {len(_tn_notes)-5} รายการ")

                                _tn_lines.append("─" * 20)
                                _tn_lines.append(
                                    "⚕️ ข้อมูลประกอบการตัดสินใจ")
                                reply_with_quick_reply(reply_token,
                                    "\n".join(_tn_lines),
                                    _patient_quick_reply_items(_tn_hn))
                        except Exception as _tn_err:
                            logger.warning("Treatment notes: %s", _tn_err)
                            reply_message(reply_token,
                                "ขออภัย ไม่สามารถดึงข้อมูลได้")

                # ── ประวัติการรักษา (เพิ่ม 2026-11-06) ──
                elif text.strip().startswith(("ประวัติการรักษา ", "ประวัติยา ")):
                    _hist_q = text.strip()
                    for _pf in ("ประวัติการรักษา ", "ประวัติยา "):
                        if _hist_q.startswith(_pf):
                            _hist_q = _hist_q[len(_pf):].strip()
                            break
                    # match_hn จัดการ "HN" prefix ให้อัตโนมัติแล้ว
                    _hist_p = master_registry.match_hn(_hist_q)
                    if not _hist_p:
                        reply_message(reply_token, f"❌ ไม่พบ HN: {_hist_q} ในทะเบียน")
                    else:
                        _hist_hn = _hist_p.clinic_hn or _hist_q
                        reply_with_quick_reply(reply_token,
                            treatment_tracker.format_history_for_line(
                                _hist_hn, _hist_p.patient_name),
                            _patient_quick_reply_items(_hist_hn))

                # ── ติดตามผลการรักษา — รายการที่ครบกำหนด (เพิ่ม 2026-11-06) ──
                elif text.strip() in ("ติดตามผลการรักษา", "followup",
                                       "ติดตามผลยา", "รายการติดตาม"):
                    try:
                        _pending = treatment_tracker.get_pending_followups()
                        reply_message(reply_token,
                            treatment_tracker.format_pending_for_line(_pending))
                    except Exception as _fu_err:
                        logger.warning("Pending followups: %s", _fu_err)
                        reply_message(reply_token,
                            "ขออภัย ไม่สามารถดึงรายการติดตามได้")

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
                # ── ข้อ 1+5: พิมพ์แค่ HN หรือ "ผป <HN>" → Patient Card + Quick Reply ──
                elif (text.strip().startswith(("ผป ", "ผป.", "patient "))
                      or (text.strip().isdigit() and len(text.strip()) >= 4)):
                    _pc_q = text.strip()
                    # ตัด prefix
                    for _pf in ("ผป ", "ผป.", "patient "):
                        if _pc_q.startswith(_pf):
                            _pc_q = _pc_q[len(_pf):].strip()
                            break
                    _pc_p = master_registry.match_hn(_pc_q)
                    if _pc_p:
                        _pc_hn = _pc_p.clinic_hn or _pc_q
                        # ดึง Hb ล่าสุด
                        _pc_hb = None
                        try:
                            _pc_docs = _get_patient_lab_docs(_pc_p, _pc_hn, limit=3)
                            if _pc_docs:
                                _pc_labs = _extract_latest_labs(
                                    _pc_docs[-1].get("extracted_text", ""))
                                _pc_hb = _pc_labs.get("hb")
                        except Exception:
                            pass
                        # ESA info
                        _pc_esa = ""
                        if hasattr(_pc_p, 'esa_medication') and _pc_p.esa_medication:
                            _pc_esa = f"{_pc_p.esa_medication}"
                            if hasattr(_pc_p, 'esa_dose') and _pc_p.esa_dose:
                                _pc_esa += f" {_pc_p.esa_dose}u"
                        # Dialysis info
                        _pc_dial = ""
                        if hasattr(_pc_p, 'hd_per_week') and _pc_p.hd_per_week:
                            _d_days = ""
                            if hasattr(_pc_p, 'dialysis_days') and _pc_p.dialysis_days:
                                _d_days = f" ({_pc_p.dialysis_days})"
                            _pc_dial = f"ฟอก {_pc_p.hd_per_week} ครั้ง/สัปดาห์{_d_days}"

                        _pc_flex = _build_patient_card_flex(
                            _pc_hn, _pc_p, hb_val=_pc_hb,
                            esa_info=_pc_esa, dialysis_info=_pc_dial)
                        reply_flex_with_quick_reply(
                            reply_token, _pc_flex,
                            _patient_quick_reply_items(_pc_hn))
                    else:
                        reply_message(reply_token,
                            f"❌ ไม่พบ HN: {_pc_q} ในทะเบียน\n"
                            f"ลองพิมพ์ 'ทะเบียนผู้ป่วย' เพื่อดูรายชื่อ")

                else:
                    # ── Smart Auto-Reply: ลอง match CKD Knowledge ก่อน ──
                    try:
                        from ckd_knowledge import get_ckd_response, get_ckd_flex_response
                        _ckd_resp = get_ckd_response(text)
                        if _ckd_resp == "__REDIRECT_SCHEDULE__":
                            # redirect ไปคำสั่ง รอบฟอก
                            reply_message(reply_token, "💡 พิมพ์ 'รอบฟอก' เพื่อดูตารางฟอกวันนี้")
                        elif _ckd_resp == "__REDIRECT_LAB__":
                            reply_message(reply_token, "💡 พิมพ์ 'ผลแล็บ' หรือ 'ผลแล็บ HN xxxxx' เพื่อดูผลตรวจ")
                        elif _ckd_resp:
                            _ckd_flex = get_ckd_flex_response(text)
                            if _ckd_flex:
                                reply_line_message(reply_token, _ckd_flex)
                            else:
                                reply_message(reply_token, _ckd_resp)
                        else:
                            # ไม่ match CKD → default help
                            reply_message(
                                reply_token,
                                "📄 ส่งภาพหรือไฟล์เพื่อจำแนกประเภทอัตโนมัติ\n\n"
                                "คำสั่งที่ใช้บ่อย:\n"
                                "  ค้นหา [คำค้น] — ค้นหาเอกสาร\n"
                                "  ผลแล็บล่าสุด — ดูผลตรวจล่าสุด\n"
                                "  สถิติ — ดูสถิติเอกสาร\n"
                                "  ช่วยเหลือ — ดูคำสั่งทั้งหมด\n"
                                "\n💡 พิมพ์แค่เลข HN (เช่น 69453) → ดูข้อมูลผู้ป่วย\n"
                                "\n🧠 ถามเกี่ยวกับโรคไตได้เลย เช่น:\n"
                                "  'กินอะไรได้บ้าง' 'จำกัดน้ำเท่าไหร่' 'ยาโรคไต'",
                            )
                    except Exception as _ckd_err:
                        logger.warning(f"CKD auto-reply error: {_ckd_err}")
                        reply_message(
                            reply_token,
                            "📄 ส่งภาพหรือไฟล์เพื่อจำแนกประเภทอัตโนมัติ\n\n"
                            "คำสั่งที่ใช้บ่อย:\n"
                            "  ค้นหา [คำค้น] — ค้นหาเอกสาร\n"
                            "  ผลแล็บล่าสุด — ดูผลตรวจล่าสุด\n"
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


@app.route("/health/weight-test", methods=["GET"])
def health_weight_test():
    """ทดสอบระบบน้ำหนัก — ไม่บันทึกข้อมูลจริง"""
    import os as _os_wt
    results = {}
    # 1. import chain
    try:
        from hd_module.uf_coach import parse_weight_message as _pwm_t, patient_context as _pc_t, HOME_WEIGHT_SQL as _hw_sql
        results["import"] = "OK"
    except Exception as _e:
        results["import"] = f"FAIL: {type(_e).__name__}: {_e}"
        return jsonify(results), 500
    # 2. parse test
    for _t in ["น้ำหนัก 65 kg", "น้ำหนัก 65", "นน 62.4 กก"]:
        results[f"parse({_t})"] = _pwm_t(_t)
    # 3. DB connection + table
    _db = _os_wt.environ.get("HD_DATABASE_URL", "") or _os_wt.environ.get("DATABASE_URL", "")
    if not _db:
        results["db"] = "NO DATABASE_URL"
        return jsonify(results), 200
    try:
        import psycopg2 as _pg_t
        _cn = _pg_t.connect(_db, connect_timeout=5)
        with _cn.cursor() as _cr:
            _cr.execute(_hw_sql)
            _cn.commit()
            results["hd_home_weights_table"] = "OK"
            _cr.execute("SELECT COUNT(*) FROM hd_patient_mapping")
            results["hd_patient_mapping_count"] = _cr.fetchone()[0]
            _cr.execute("SELECT COUNT(*) FROM hd_sessions WHERE status='completed'")
            results["completed_sessions"] = _cr.fetchone()[0]
            _cr.execute("SELECT key, value FROM hd_settings WHERE key IN ('patient_weight_coach','idwg_limit_pct')")
            results["settings"] = {r[0]: r[1] for r in _cr.fetchall()}
            _cr.execute("SELECT hn, patient_name, dry_weight FROM hd_patient_mapping WHERE dry_weight IS NOT NULL LIMIT 3")
            results["sample_patients_with_dw"] = [{"hn": r[0], "name": r[1][:6] + "…" if r[1] and len(r[1]) > 6 else r[1], "dw": float(r[2]) if r[2] else None} for r in _cr.fetchall()]
            _cr.execute("SELECT hn FROM hd_patient_mapping WHERE dry_weight IS NOT NULL LIMIT 1")
            _tr = _cr.fetchone()
            if _tr:
                results["patient_context_test"] = _pc_t(_cr, _tr[0])
        _cn.close()
    except Exception as _e:
        results["db_error"] = f"{type(_e).__name__}: {_e}"
    # 4. HN registry stats
    try:
        results["hn_registry_count"] = len(hn_registry._cache) if hasattr(hn_registry, '_cache') else "unknown"
    except Exception:
        pass
    return jsonify(results), 200


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
        health_info["patient_folder_index"] = globals().get("_last_reconcile", {})
        health_info["document_search"] = {
            "indexed_documents": doc_index.count(),
            "authorized_users": len(AUTHORIZED_USERS),
            "admin_secret_set": bool(ADMIN_SECRET),
        }

    # ── PostgreSQL status ──
    if db and db.is_ready:
        health_info["database"] = db.health()
    elif db:
        health_info["database"] = {"status": db.status}

    # ── งานอัตโนมัติ + LINE (ops watchdog, 14 พ.ย. 2569) — ไม่มี id/เนื้อหาข้อความ ──
    try:
        from hd_module import ops_status as _ops
        _snap = _ops.snapshot()
        health_info["ops"] = {
            "healthy": _snap["healthy"],
            "problems": _snap["problems"],
            "scheduler_tick_age_sec": _snap["tick_age_sec"],
            "scheduler_restarts": _snap["restarts"],
            "stuck_jobs": _snap["stuck_jobs"],
            "line_last_ok": _snap["line"].get("last_ok"),
            "line_last_error": _snap["line"].get("last_error"),
            "line_quota": _snap["line"].get("quota"),
        }
    except Exception:
        pass

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
    """คืนค่า OCR debug จาก memory — ต้องมี X-Admin-Secret header"""
    secret = request.headers.get("X-Admin-Secret", "")
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


@app.route("/admin/move-document", methods=["POST"])
def admin_move_document():
    """ย้ายเอกสารเข้าแฟ้มผู้ป่วย — Admin API.

    Body JSON: {"doc_id": "c862644e", "hn": "60139"}
    หรือ batch: {"moves": [{"doc_id": "...", "hn": "..."}, ...]}
    """
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or auth_header != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}
    moves = data.get("moves", [])
    if not moves and data.get("doc_id"):
        moves = [{"doc_id": data["doc_id"], "hn": data.get("hn", "")}]
    if not moves:
        return jsonify({"error": "No moves specified"}), 400

    results = []
    for mv in moves:
        doc_prefix = mv.get("doc_id", "").strip()
        hn = mv.get("hn", "").strip()
        if not doc_prefix or not hn:
            results.append({"doc_id": doc_prefix, "error": "missing doc_id or hn"})
            continue
        r = _move_document_to_patient(doc_prefix, hn, "admin_api")
        r["doc_id"] = doc_prefix
        r["hn"] = hn
        results.append(r)

    return jsonify({"results": results}), 200


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
    auth_param = request.headers.get("X-Admin-Secret", "")
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
    auth_param = request.headers.get("X-Admin-Secret", "")
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


# ─── Normalize Patient Folders ────────────────────────────

@app.route("/admin/api/v2/normalize-folders", methods=["POST"])
def admin_normalize_folders():
    """Normalize patient_hn_folder ที่เป็น HN... ให้เป็น format มาตรฐาน.

    Scan เอกสารทั้งหมดใน doc_index ที่ patient_hn_folder ขึ้นต้นด้วย "HN"
    → จับคู่ HN กับ master_registry → อัปเดต doc index metadata เป็น
    format มาตรฐาน ({clinic_hn}_{sanitized_name})

    ไม่ย้ายไฟล์บน GCS — อัปเดตแค่ index metadata เท่านั้น
    ข้อมูลเดิมไม่สูญหาย เพราะ list_by_patient_folder ยังค้น prefix ได้

    Query params:
      secret: ADMIN_SECRET
      dry_run: "true" (default) เพื่อดู preview, "false" เพื่ออัปเดตจริง
    """
    err = _admin_api_auth()
    if err:
        return err

    dry_run = request.args.get("dry_run", "true").lower() == "true"

    if not master_registry:
        return jsonify({"error": "Master registry ไม่พร้อม"}), 500

    reg_count = master_registry.count()
    if reg_count == 0:
        return jsonify({"error": "Registry ว่าง"}), 400

    # Force reload
    doc_index._ensure_fresh()

    normalized = []
    skipped = []
    already_ok = 0
    errors = []

    for doc in doc_index._docs:
        doc_id = doc.get("document_id", "")
        old_folder = doc.get("patient_hn_folder", "")

        if not old_folder:
            continue

        # เฉพาะ folder ที่ขึ้นต้นด้วย "HN" (format เก่า)
        if not old_folder.upper().startswith("HN"):
            already_ok += 1
            continue

        # ดึง HN ออกจาก "HN{hn}"
        raw_hn = old_folder[2:]  # ตัด "HN" ออก
        if not raw_hn:
            continue

        # ค้นหาใน master_registry
        new_folder = master_registry.get_folder_for_hn(raw_hn)
        if not new_folder:
            skipped.append({
                "doc_id": doc_id[:8],
                "old_folder": old_folder[:20],
                "reason": "ไม่พบใน master registry",
            })
            continue

        if new_folder == old_folder:
            already_ok += 1
            continue

        if dry_run:
            normalized.append({
                "doc_id": doc_id[:8],
                "old": old_folder[:30],
                "new": new_folder[:30],
            })
        else:
            # อัปเดต doc index metadata
            try:
                doc["patient_hn_folder"] = new_folder
                # เขียน per-doc JSON ใน GCS — ใช้ versioned write
                # เพราะ SA ไม่มี storage.objects.delete (ไม่สามารถ overwrite ได้)
                if storage.is_ready:
                    _ts = int(time.time())
                    blob_path = (
                        f"{INDEX_DOC_PREFIX}{doc_id}_v{_ts}.json"
                    )
                    doc_json = json.dumps(doc, ensure_ascii=False)
                    blob = storage._bucket.blob(blob_path)
                    blob.upload_from_string(
                        doc_json,
                        content_type="application/json",
                        if_generation_match=0,  # create-only
                    )
                normalized.append({
                    "doc_id": doc_id[:8],
                    "old": old_folder[:30],
                    "new": new_folder[:30],
                })
                # ไม่ log ข้อมูลผู้ป่วย (PDPA)
                logger.info(
                    "Normalize folder: %s → standard format",
                    doc_id[:8],
                )
            except Exception as e:
                errors.append({
                    "doc_id": doc_id[:8],
                    "error": type(e).__name__,
                })

    return jsonify({
        "dry_run": dry_run,
        "registry_count": reg_count,
        "total_docs": len(doc_index._docs),
        "already_standard": already_ok,
        "normalized": len(normalized),
        "skipped": len(skipped),
        "errors": len(errors),
        "details": {
            "normalized": normalized[:50],
            "skipped": skipped[:50],
            "errors": errors[:50],
        },
    })


# ─── Dead-Letter Queue: Admin Endpoints ─────────────────

@app.route("/admin/api/v2/dead-letter/list", methods=["GET"])
def api_dead_letter_list():
    """รายการเอกสารใน dead-letter queue"""
    err = _admin_api_auth()
    if err:
        return err
    if not dead_letter_queue:
        return jsonify({"error": "DLQ ไม่ได้เปิดใช้"}), 400
    entries = dead_letter_queue.list_all(
        limit=int(request.args.get("limit", "50"))
    )
    return jsonify({"entries": entries, "count": len(entries)})


@app.route("/admin/api/v2/dead-letter/stats", methods=["GET"])
def api_dead_letter_stats():
    """สถิติ dead-letter queue"""
    err = _admin_api_auth()
    if err:
        return err
    if not dead_letter_queue:
        return jsonify({"error": "DLQ ไม่ได้เปิดใช้"}), 400
    return jsonify(dead_letter_queue.get_stats())


@app.route("/admin/api/v2/dead-letter/resolve", methods=["POST"])
def api_dead_letter_resolve():
    """แก้ไข dead-letter entry (mark as resolved)"""
    err = _admin_api_auth()
    if err:
        return err
    if not dead_letter_queue:
        return jsonify({"error": "DLQ ไม่ได้เปิดใช้"}), 400

    body = request.get_json(silent=True) or {}
    doc_id = str(body.get("doc_id", "")).strip()[:64]
    resolution = str(body.get("resolution", "manual")).strip()[:64]
    if not doc_id:
        return jsonify({"error": "ต้องระบุ doc_id"}), 400

    ok = dead_letter_queue.mark_resolved(doc_id, resolution)
    if ok:
        return jsonify({
            "success": True,
            "message": f"แก้ไขแล้ว: {doc_id[:8]}",
        })
    return jsonify({"error": "ไม่พบรายการ"}), 404


@app.route("/admin/api/v2/system-health", methods=["GET"])
def api_system_health():
    """สถานะสุขภาพระบบทั้งหมด"""
    err = _admin_api_auth()
    if err:
        return err
    if not _system_monitor:
        return jsonify({"error": "System monitor ไม่พร้อม"}), 400
    health = _system_monitor.check_health()
    return jsonify(health)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FINANCE MODULE — สรุปค่าใช้จ่ายคลินิก
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@app.route("/admin/api/v2/finance/monthly-summary", methods=["GET"])
def api_finance_monthly_summary():
    """สรุปค่าใช้จ่ายรายเดือน"""
    err = _admin_api_auth()
    if err:
        return err
    if not expense_tracker:
        return jsonify({"error": "Expense tracker ไม่พร้อม"}), 400

    year = request.args.get("year", "")
    month = request.args.get("month", "")
    if not year or not month:
        now = datetime.now(timezone.utc)
        year = year or str(now.year)
        month = month or str(now.month)

    try:
        summary = expense_tracker.get_monthly_summary(
            int(year), int(month)
        )
        return jsonify(summary)
    except (ValueError, TypeError):
        return jsonify({"error": "year/month ไม่ถูกต้อง"}), 400
    except Exception as e:
        logger.warning(
            "Finance monthly summary error: %s", type(e).__name__
        )
        return jsonify({"error": "เกิดข้อผิดพลาด"}), 500


@app.route("/admin/api/v2/finance/vendor-summary", methods=["GET"])
def api_finance_vendor_summary():
    """สรุปค่าใช้จ่ายรายบริษัท"""
    err = _admin_api_auth()
    if err:
        return err
    if not expense_tracker:
        return jsonify({"error": "Expense tracker ไม่พร้อม"}), 400

    vendor = request.args.get("vendor", "")
    if not vendor:
        return jsonify({"error": "กรุณาระบุ vendor"}), 400

    months = int(request.args.get("months", "6"))
    months = min(max(months, 1), 24)  # จำกัด 1-24 เดือน

    try:
        summary = expense_tracker.get_vendor_summary(vendor, months)
        return jsonify(summary)
    except Exception as e:
        logger.warning(
            "Finance vendor summary error: %s", type(e).__name__
        )
        return jsonify({"error": "เกิดข้อผิดพลาด"}), 500


@app.route("/admin/api/v2/finance/yearly-summary", methods=["GET"])
def api_finance_yearly_summary():
    """สรุปค่าใช้จ่ายรายปี"""
    err = _admin_api_auth()
    if err:
        return err
    if not expense_tracker:
        return jsonify({"error": "Expense tracker ไม่พร้อม"}), 400

    year = request.args.get("year", "")
    if not year:
        year = str(datetime.now(timezone.utc).year)

    try:
        summary = expense_tracker.get_yearly_summary(int(year))
        return jsonify(summary)
    except (ValueError, TypeError):
        return jsonify({"error": "year ไม่ถูกต้อง"}), 400
    except Exception as e:
        logger.warning(
            "Finance yearly summary error: %s", type(e).__name__
        )
        return jsonify({"error": "เกิดข้อผิดพลาด"}), 500


@app.route("/admin/api/v2/finance/parse-invoice", methods=["POST"])
def api_finance_parse_invoice():
    """Parse invoice จาก OCR text (manual)"""
    err = _admin_api_auth()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    ocr_text = data.get("ocr_text", "")
    vendor_hint = data.get("vendor_hint", "")
    if not ocr_text or len(ocr_text.strip()) < 20:
        return jsonify({"error": "กรุณาส่ง ocr_text (≥20 ตัวอักษร)"}), 400

    try:
        from invoice_parser import parse_invoice
        result = parse_invoice(ocr_text, vendor_hint)
        if not result:
            return jsonify({"error": "ไม่สามารถ parse invoice ได้"}), 400
        return jsonify(result)
    except Exception as e:
        logger.warning(
            "Finance parse-invoice error: %s", type(e).__name__
        )
        return jsonify({"error": "เกิดข้อผิดพลาด"}), 500


@app.route("/admin/api/v2/finance/status", methods=["GET"])
def api_finance_status():
    """สถานะระบบ finance"""
    err = _admin_api_auth()
    if err:
        return err
    if not expense_tracker:
        return jsonify({"enabled": False})
    try:
        return jsonify(expense_tracker.get_status())
    except Exception as e:
        logger.warning(
            "Finance status error: %s", type(e).__name__
        )
        return jsonify({"enabled": False, "error": str(type(e).__name__)})


@app.route("/admin/v2/dead-letter", methods=["GET", "POST"])
def admin_dead_letter_page():
    """หน้าเว็บ Dead-Letter Queue — ดูเอกสารที่มีปัญหา"""
    auth_err = _admin_page_auth()
    if auth_err is not None:
        return auth_err

    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    # ดึงข้อมูล
    entries = []
    stats = {"total": 0, "pending": 0, "resolved": 0, "abandoned": 0}
    if dead_letter_queue:
        entries = dead_letter_queue.list_all(limit=100)
        stats = dead_letter_queue.get_stats()

    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

    # สร้าง rows HTML
    rows_html = ""
    for e in entries:
        _ts = e.get("created_at", "")[:19].replace("T", " ")
        _eid = e.get("doc_id", "")[:8]
        _etype = e.get("error_type", "")
        _emsg = e.get("error_message", "")[:50]
        _status = e.get("status", "")
        _retry = e.get("retry_count", 0)
        _cat = e.get("category", "")

        _status_badge = {
            "pending": "🟡 รอ",
            "retrying": "🔄 กำลัง retry",
            "resolved": "✅ แก้แล้ว",
            "abandoned": "❌ abandon",
        }.get(_status, _status)

        _resolve_btn = ""
        if _status in ("pending", "retrying"):
            _resolve_btn = (
                f"<button onclick=\"resolve('{e.get('doc_id', '')}')\" "
                f"class='btn-resolve'>✅ แก้ไขแล้ว</button>"
            )

        rows_html += (
            f"<tr>"
            f"<td>{_ts}</td>"
            f"<td>{_eid}</td>"
            f"<td>{_etype}</td>"
            f"<td>{_emsg}</td>"
            f"<td>{_cat}</td>"
            f"<td>{_status_badge}</td>"
            f"<td>{_retry}/3</td>"
            f"<td>{_resolve_btn}</td>"
            f"</tr>"
        )

    if not rows_html:
        rows_html = (
            "<tr><td colspan='8' style='text-align:center;"
            "padding:20px;color:#999'>ไม่มีรายการ</td></tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dead-Letter Queue — RANONG RAKTAI AI</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 0;
  padding: 16px; background: #f5f5f5; }}
h1 {{ font-size: 20px; color: #1a1a1a; }}
.stats {{ display: flex; gap: 12px; margin: 12px 0; flex-wrap: wrap; }}
.stat {{ background: #fff; padding: 12px 16px; border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,.1); text-align: center; }}
.stat .num {{ font-size: 24px; font-weight: 700; }}
.stat .label {{ font-size: 12px; color: #666; }}
table {{ width: 100%; border-collapse: collapse; background: #fff;
  border-radius: 8px; overflow: hidden;
  box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
th {{ background: #2c3e50; color: #fff; padding: 8px 6px;
  text-align: left; font-size: 12px; }}
td {{ padding: 6px; border-bottom: 1px solid #eee;
  font-size: 12px; word-break: break-all; }}
tr:hover {{ background: #f8f9fa; }}
.btn-resolve {{ padding: 4px 10px; background: #1DB446; color: #fff;
  border: none; border-radius: 4px; cursor: pointer; font-size: 11px; }}
.btn-resolve:hover {{ background: #179c3c; }}
#msg {{ padding: 8px; margin: 8px 0; border-radius: 4px;
  display: none; text-align: center; }}
</style>
</head>
<body>
<h1>📋 Dead-Letter Queue</h1>
<div class="stats">
<div class="stat"><div class="num">{stats.get('total', 0)}</div>
  <div class="label">ทั้งหมด</div></div>
<div class="stat"><div class="num" style="color:#f39c12">
  {stats.get('pending', 0)}</div>
  <div class="label">รอตรวจ</div></div>
<div class="stat"><div class="num" style="color:#27ae60">
  {stats.get('resolved', 0)}</div>
  <div class="label">แก้แล้ว</div></div>
<div class="stat"><div class="num" style="color:#e74c3c">
  {stats.get('abandoned', 0)}</div>
  <div class="label">abandon</div></div>
</div>
<div id="msg"></div>
<div style="overflow-x:auto">
<table>
<thead>
<tr><th>เวลา</th><th>Doc ID</th><th>ประเภท Error</th>
<th>ข้อความ</th><th>หมวด</th><th>สถานะ</th>
<th>Retry</th><th>จัดการ</th></tr>
</thead>
<tbody>{rows_html}</tbody>
</table>
</div>
<script>
function resolve(docId) {{
    var msg = document.getElementById('msg');
    fetch('{base_url}/admin/api/v2/dead-letter/resolve', {{
        method: 'POST',
        headers: {{
            'Content-Type': 'application/json',
            'X-Admin-Secret': '{admin_secret}'
        }},
        body: JSON.stringify({{ doc_id: docId, resolution: 'manual' }})
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
        if (data.success) {{
            msg.style.display = 'block';
            msg.style.background = '#e8f5e9';
            msg.style.color = '#2e7d32';
            msg.textContent = '✅ ' + data.message;
            setTimeout(function() {{ location.reload(); }}, 1500);
        }} else {{
            msg.style.display = 'block';
            msg.style.background = '#ffebee';
            msg.style.color = '#c62828';
            msg.textContent = '❌ ' + (data.error || 'เกิดข้อผิดพลาด');
        }}
    }});
}}
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ─── Admin Dashboard: Stats API ──────────────────────────

def _retroactive_route_run(dry_run: bool = True, cat_filter: str = "all") -> dict:
    """จัดเอกสารเก่าเข้าแฟ้มผู้ป่วยย้อนหลัง — แกนกลางที่ admin API และ LINE ใช้ร่วมกัน"""

    if not storage.is_ready:
        return {"error": "GCS not ready"}

    # Force reload doc index + registry
    doc_index._reload_from_gcs()
    reg_count = master_registry.count()

    if reg_count == 0:
        return {"error": "Registry empty — ไม่มีผู้ป่วยลงทะเบียน"}

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

        # อยู่ในแฟ้มผู้ป่วยแล้ว (folder/YYYY-Qn/YYYY-MM/<HN>_<ชื่อ>/file) และ
        # แฟ้มนั้นคือแฟ้มที่จับคู่ได้ (หรือแฟ้มที่ดัชนีระบุไว้) → ไม่แตะ
        # (2026-11-02: เดิมคำนวณ path ใหม่ทั้งเส้นแล้วเทียบ — เดือนที่คำนวณ
        #  ต่างกันทำให้เอกสารที่ถูกอยู่แล้วถูก copy ซ้ำและดัชนีชี้ผิด)
        _parts = blob_path.split("/")
        if len(_parts) == 5 and _parts[3] and (
            _parts[3] == matched_folder
            or _parts[3] == (doc.get("patient_hn_folder") or "")
        ):
            already_in_folder += 1
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
                # path ในดัชนีเป็น path เปล่าเสมอ (ไม่มี gs://bucket/)
                new_gcs_uri = new_blob_path
                if _strip_gs_uri(doc.get("gcs_object_path", "")) != new_gcs_uri or (
                    matched_folder and doc.get("patient_hn_folder") != matched_folder
                ):
                    doc_index.update_fields(doc_id, {
                        "gcs_object_path": new_gcs_uri,
                        "patient_hn_folder": matched_folder or doc.get("patient_hn_folder", ""),
                    })
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
            new_gcs_uri = new_blob_path  # path เปล่า — ไม่มี gs://
            # เขียน index แบบ versioned — เดิมเขียนทับ {doc_id}.json ได้ 403
            # เงียบ ๆ ทำให้ไฟล์ถูกคัดลอกเข้าแฟ้มผู้ป่วยแล้ว แต่ index ยังชี้ที่เก่า
            # และไม่มี patient_hn_folder → LIFF/ค้นหาไม่เห็น
            if not doc_index.update_fields(doc_id, {
                "gcs_object_path": new_gcs_uri,
                "patient_hn_folder": matched_folder or doc.get("patient_hn_folder", ""),
            }):
                logger.warning(
                    "Retroactive route: index update failed for %s", doc_id_short,
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

    return {
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
    }


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
    auth_param = request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or auth_param != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    dry_run = request.args.get("dry_run", "true").lower() == "true"
    cat_filter = request.args.get("category", "all")
    _r = _retroactive_route_run(dry_run, cat_filter)
    if _r.get("error"):
        return jsonify(_r), (500 if "GCS" in _r["error"] else 400)
    return jsonify(_r), 200


@app.route("/admin/api/stats", methods=["GET"])
def admin_api_stats():
    """API สำหรับดึงข้อมูลสถิติระบบ — ใช้โดย Dashboard."""
    # header X-Admin-Secret หรือ cookie จากหน้า login (same-origin GET) — เหมือน API อื่น
    _err = _admin_api_auth()
    if _err is not None:
        return _err

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
    // ใช้ cookie จากหน้า login (same-origin) หรือ header ถ้าเปิดด้วย ?secret= — ไม่ส่ง secret ใน URL
    const r=await fetch('/admin/api/stats',{credentials:'same-origin',headers:SECRET?{'X-Admin-Secret':SECRET}:{}});
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


@app.route("/admin/clinical-dashboard", methods=["GET", "POST"])
def admin_clinical_dashboard():
    """Kidney Clinical Intelligence Dashboard — Phase A."""
    auth_err = _admin_page_auth()
    if auth_err is not None:
        return auth_err
    return CLINICAL_DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/admin/api/clinical-kpi", methods=["GET"])
def admin_api_clinical_kpi():
    """API สำหรับดึงข้อมูล Clinical KPI — Phase A."""
    auth_param = request.headers.get("X-Admin-Secret", "")
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
    auth_param = request.headers.get("X-Admin-Secret", "")
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
    auth_param = request.headers.get("X-Admin-Secret", "")
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
    auth_param = request.headers.get("X-Admin-Secret", "")
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
    # Render proxy ส่ง X-Forwarded-Proto: https แต่ Flask ไม่ดูค่านี้โดย
    # อัตโนมัติ request.host_url จึงคืน http:// ทำให้ fetch จากหน้า HTTPS
    # ไปยัง API_BASE ที่เป็น http:// ถูก browser บล็อกเป็น mixed-content
    # → "Failed to fetch" (ยืนยันจาก production 31 ต.ค. 2026)
    api_base = request.host_url.rstrip("/")
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
    if forwarded_proto == "https" and api_base.startswith("http://"):
        api_base = "https://" + api_base[len("http://"):]
    return get_liff_html(api_base), 200, {"Content-Type": "text/html; charset=utf-8"}


def _liff_cors_headers():
    """CORS headers สำหรับ LIFF API.

    LIFF v2 concatenation mode เปิดหน้าเว็บที่ origin https://liff.line.me
    แต่ API อยู่ที่ ranong-raktai-ai-cloud.onrender.com → cross-origin
    ต้องมี CORS headers ไม่งั้น browser block fetch → "Failed to fetch"
    (ยืนยันจาก production 31 ต.ค. 2026)
    """
    return {
        "Access-Control-Allow-Origin": "https://liff.line.me",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    }


@app.route("/liff/api/my-labs", methods=["POST", "OPTIONS"])
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
    # CORS preflight (OPTIONS) — browser ส่งก่อน POST จริง
    if request.method == "OPTIONS":
        return "", 204, _liff_cors_headers()

    if get_patient_labs_for_liff is None or verify_liff_id_token is None:
        return jsonify({"error": "LIFF module not available"}), 500, _liff_cors_headers()

    body = request.get_json(silent=True) or {}
    id_token = body.get("id_token", "")
    if not id_token:
        return jsonify({"error": "id_token required"}), 400, _liff_cors_headers()

    # ── ยืนยันตัวตนกับ LINE ──
    try:
        line_user_id = verify_liff_id_token(id_token)
    except LiffAuthError as auth_err:
        # เหตุผลละเอียดลง log ฝั่ง server เท่านั้น
        logger.warning("LIFF auth failed: %s", auth_err)
        audit_logger.log("liff_auth_failed", "", {})
        return jsonify({"error": "unauthorized"}), 401, _liff_cors_headers()
    except Exception as e:
        logger.error("LIFF auth error: %s", type(e).__name__)
        return jsonify({"error": "unauthorized"}), 401, _liff_cors_headers()

    # owner_hash มาจาก sub ที่ยืนยันแล้ว ใช้ salt เดียวกับทั้งระบบ
    owner_hash = hash_user_id(line_user_id)

    # ตรวจว่าเป็นแพทย์/admin ไหม — ถ้าใช่ สามารถค้น HN อื่นได้
    _is_physician = (owner_hash in PHYSICIAN_USERS or owner_hash in ADMIN_USERS)

    # แพทย์สามารถระบุ HN เพื่อดูเอกสารของคนไข้รายอื่น
    # ถ้าไม่ระบุ HN → แสดงเอกสารทุกคน (physician browse mode)
    search_hn = ""
    if _is_physician:
        raw_hn = str(body.get("search_hn", "")).strip()[:20]
        if raw_hn:
            search_hn = raw_hn
        else:
            # แพทย์เปิดโดยไม่ search → แสดงหน้าค้นหา
            data = {
                "patient_name": "", "patient_hn": "",
                "documents": [], "message": "physician_search",
                "is_physician": True,
            }
            return jsonify(data), 200, _liff_cors_headers()

    try:
        data = get_patient_labs_for_liff(
            storage=storage,
            owner_hash=owner_hash,
            doc_index=doc_index,
            hn_registry=hn_registry,
            generate_signed_url_fn=generate_signed_url,
            search_hn=search_hn,
            is_physician=_is_physician,
        )
        data["is_physician"] = _is_physician
        return jsonify(data), 200, _liff_cors_headers()
    except Exception as e:
        logger.error("LIFF API error: %s", type(e).__name__)
        return jsonify({"error": "internal error"}), 500, _liff_cors_headers()


@app.route("/liff/api/save-note", methods=["POST", "OPTIONS"])
def liff_api_save_note():
    """บันทึกคำแนะนำจากแพทย์สำหรับผู้ป่วย — เฉพาะ physician/admin."""
    if request.method == "OPTIONS":
        return "", 204, _liff_cors_headers()

    body = request.get_json(silent=True) or {}
    id_token = body.get("id_token", "")
    if not id_token:
        return jsonify({"error": "id_token required"}), 400, _liff_cors_headers()

    try:
        line_user_id = verify_liff_id_token(id_token)
    except Exception:
        return jsonify({"error": "unauthorized"}), 401, _liff_cors_headers()

    owner_hash = hash_user_id(line_user_id)
    if owner_hash not in PHYSICIAN_USERS and owner_hash not in ADMIN_USERS:
        return jsonify({"error": "physician only"}), 403, _liff_cors_headers()

    patient_hn = str(body.get("patient_hn", "")).strip()[:20]
    note_text = str(body.get("note", "")).strip()[:2000]
    if not patient_hn or not note_text:
        return jsonify({"error": "patient_hn and note required"}), 400, _liff_cors_headers()

    try:
        from datetime import timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        note_path = f"_treatment_notes/{patient_hn}/{ts}.json"
        note_data = json.dumps({
            "hn": patient_hn,
            "note": note_text,
            "by": owner_hash[:8],
            "at": ts,
        }, ensure_ascii=False)
        storage.put(note_path, note_data.encode("utf-8"), "application/json")

        # ── Feature 2: ส่ง LINE push แจ้งผู้ป่วย ──
        try:
            _patient_entry = hn_registry.get_by_clinic_hn(patient_hn)
            if _patient_entry and LINE_CHANNEL_ACCESS_TOKEN:
                _patient_hash = _patient_entry.owner_hash
                # หา LINE userId จาก alert_recipients หรือ hn_registry
                _line_id = None
                try:
                    from alert_delivery import get_line_user_id
                    _line_id = get_line_user_id(_patient_hash)
                except Exception:
                    pass
                if _line_id:
                    import requests as _req
                    try:
                        from liff_app import _thai_note_date as _tnd
                        _when = _tnd(ts)
                    except Exception:
                        _when = ""
                    _push_msg = (
                        "📋 แพทย์มีคำแนะนำใหม่\n"
                        + (f"📅 {_when}\n" if _when else "")
                        + "━━━━━━━━━━━━━━━━━━━━\n"
                        + f"{note_text[:400]}\n\n"
                        + "💡 กดปุ่ม 'ดูผลแล็บ' เพื่อดูรายละเอียด"
                    )
                    _req.post(
                        "https://api.line.me/v2/bot/message/push",
                        json={
                            "to": _line_id,
                            "messages": [{"type": "text", "text": _push_msg}],
                        },
                        headers={
                            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                            "Content-Type": "application/json",
                        },
                        timeout=10,
                    )
                    logger.info("LIFF: pushed note to patient %s", patient_hn)
        except Exception as _push_err:
            logger.warning("LIFF push note failed: %s", type(_push_err).__name__)

        return jsonify({"success": True}), 200, _liff_cors_headers()
    except Exception as e:
        logger.error("Save note error: %s", type(e).__name__)
        return jsonify({"error": "save failed"}), 500, _liff_cors_headers()


@app.route("/admin/dashboard", methods=["GET", "POST"])
def admin_dashboard():
    """Admin Dashboard — Web UI สำหรับดูสถิติระบบ."""
    auth_err = _admin_page_auth()
    if auth_err is not None:
        return auth_err
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


# ── ตัวจับเวลาแจ้งเตือนนัดประจำวัน (ไม่ต้องใช้ Render Cron Job/secret) ──
# Starter ไม่หลับ จึงใช้ thread ในแอปเช็คทุกนาที ถึงเวลา REMINDER_HOUR
# (เวลาไทย) ก็รัน job ทุก worker จะพยายามรัน แต่ตัวกันส่งซ้ำใน GCS
# (create-only marker) ทำให้ผู้ป่วยแต่ละคนได้รับข้อความครั้งเดียว
_REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "8"))
_REMINDER_ENABLED = os.getenv("REMINDER_ENABLED", "true").strip().lower() == "true"


def _reminder_scheduler_worker():
    import time as _t
    from datetime import timezone as _tz, timedelta as _td
    _bkk = _tz(_td(hours=7))
    _last_run_day = None
    _last_monthly_month = None
    while True:
        try:
            _now = datetime.now(_bkk)
            # ── Daily reminders (08:00 BKK) ──
            if _now.hour == _REMINDER_HOUR and _last_run_day != _now.date():
                _last_run_day = _now.date()
                _res = job_manager.run("daily_reminders")
                logger.info("daily_reminders (scheduler): success=%s", _res.success)
            # ── Weekly health tips (วันจันทร์ เวลา 10:00 BKK) ──
            if (_now.weekday() == 0 and _now.hour == 10
                    and _last_run_day != _now.date()):  # reuse _last_run_day guard
                try:
                    _wk = _now.isocalendar()[1]
                    if _claim_daily_once(f"weekly_tips_{_now.year}_{_wk:02d}"):
                        import psycopg2 as _pg2wt
                        from hd_module.patient_education import run_weekly_tips
                        _db_wt = os.getenv("HD_DATABASE_URL", "") or os.getenv("DATABASE_URL", "")
                        if _db_wt and LINE_CHANNEL_ACCESS_TOKEN:
                            _wc = _pg2wt.connect(_db_wt, connect_timeout=10)
                            try:
                                from hd_module import get_setting_value as _gsv_wt
                                _wr = run_weekly_tips(_wc, push_message, _gsv_wt)
                                logger.info("weekly_tips: %s", _wr)
                            finally:
                                _wc.close()
                except Exception as _we:
                    logger.warning("weekly_tips scheduler: %s", _we)
            # ── Monthly lab summary (วันที่ 1 เวลา 09:00 BKK) ──
            if (_now.day == 1 and _now.hour == 9
                    and _last_monthly_month != (_now.year, _now.month)):
                _last_monthly_month = (_now.year, _now.month)
                try:
                    if _claim_daily_once(f"monthly_summary_{_now.year}_{_now.month:02d}"):
                        import psycopg2 as _pg2
                        from hd_module.monthly_summary import run_monthly_push
                        _db = os.getenv("HD_DATABASE_URL", "") or os.getenv("DATABASE_URL", "")
                        if _db and LINE_CHANNEL_ACCESS_TOKEN:
                            _mc = _pg2.connect(_db, connect_timeout=10)
                            try:
                                from hd_module import get_setting_value as _gsv
                                _mr = run_monthly_push(
                                    _mc, push_message,
                                    lambda hn: _hd_patient_line_id(hn),
                                    _gsv)
                                logger.info("monthly_summary: %s", _mr)
                            finally:
                                _mc.close()
                except Exception as _me:
                    logger.warning("monthly_summary scheduler: %s", _me)
        except Exception as _e:
            logger.warning("reminder scheduler: %s", type(_e).__name__)
        _t.sleep(60)


# ── ซ่อมดัชนีแฟ้มผู้ป่วยอัตโนมัติหลังบูต (idempotent — รันซ้ำไม่มีผลข้างเคียง) ──
_last_reconcile: dict = {}


# งานครั้งเดียวที่แพทย์อนุมัติ 2026-11-01 ("ให้ดำเนินการได้เลย"): ย้ายสลิป
# เซ็นทรัลโพลี 2 ใบที่เข้า _UNIDENTIFIED ก่อนมีการจับคู่ด้วยเลขบัญชี
# ทำซ้ำได้: ถ้าย้ายแล้วจะได้ status=unchanged และไม่ทำอะไร
def _reconcile_on_boot_worker():
    """งานหลังบูต: ซ่อมดัชนีแฟ้มผู้ป่วยให้ตรงไฟล์จริง (idempotent) เท่านั้น

    2026-11-02: เดิมรัน "จัดแฟ้มย้อนหลัง" ด้วย (งานครั้งเดียวที่แพทย์อนุมัติ
    2026-11-01 และข้อมูลชุดนั้นถูกล้างไปแล้ว) แต่ฟังก์ชันนั้นคำนวณ path ใหม่
    ให้เอกสารที่อยู่ในแฟ้มถูกแล้ว → copy เป็นไฟล์ซ้ำ + เขียน path รูป gs://
    ลงดัชนี → ลิงก์เปิดไฟล์ 404 และรันซ้ำทุกครั้งที่ worker เกิดใหม่ (OOM)
    จึงถอดออก — จัดแฟ้มย้อนหลังทำได้เฉพาะแพทย์สั่งทาง LINE
    """
    import time as _t
    _t.sleep(45)  # รอ index โหลดและ Redis/GCS พร้อม
    global _last_reconcile
    # ให้ worker เดียวทำ — 2 workers ทำพร้อมกันจะชนกัน
    try:
        if not state_store.set_if_absent("boot_maintenance_lock", 600):
            _last_reconcile = {"skipped": "another worker is running maintenance"}
            return
    except Exception:
        pass
    try:
        _last_reconcile = _reconcile_patient_folder_index()
        _last_reconcile["at"] = datetime.now(timezone.utc).isoformat()
    except Exception as _e:
        _last_reconcile = {"error": type(_e).__name__}


if IS_PRODUCTION and not IS_TESTING:
    threading.Thread(
        target=_reconcile_on_boot_worker, daemon=True, name="index-reconcile",
    ).start()

if _REMINDER_ENABLED and not IS_TESTING:
    threading.Thread(
        target=_reminder_scheduler_worker, daemon=True, name="reminder-scheduler",
    ).start()
    logger.info("Reminder scheduler: เปิดใช้งาน (ทุกวัน %02d:00 เวลาไทย)", _REMINDER_HOUR)

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
# Clinical Analytics API (modules แยก — ไม่แตะ logic เดิม)
# ═══════════════════════════════════════════════════════════


@app.route("/admin/api/v2/report/weekly", methods=["POST"])
def api_weekly_report():
    """ส่งรายงานสรุปรายสัปดาห์ทาง email."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import auto_weekly_report
        result = auto_weekly_report.send_scheduled_report(
            storage, doc_index, master_registry, period="weekly"
        )
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/report/monthly", methods=["POST"])
def api_monthly_report():
    """ส่งรายงานสรุปรายเดือนทาง email."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import auto_weekly_report
        result = auto_weekly_report.send_scheduled_report(
            storage, doc_index, master_registry, period="monthly"
        )
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/dialysis-adequacy", methods=["GET"])
def api_dialysis_adequacy():
    """สรุป Dialysis Adequacy ทั้งคลินิก."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import dialysis_adequacy
        summary = dialysis_adequacy.get_clinic_adequacy_summary(
            storage, master_registry
        )
        return jsonify(summary), 200
    except Exception as e:
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/dialysis-adequacy/alerts", methods=["GET"])
def api_dialysis_alerts():
    """รายการผู้ป่วย Kt/V ต่ำกว่าเกณฑ์."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import dialysis_adequacy
        alerts = dialysis_adequacy.get_inadequate_patients_alert(
            storage, master_registry
        )
        return jsonify({"alerts": alerts, "count": len(alerts)}), 200
    except Exception as e:
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/epo-tracker", methods=["GET"])
def api_epo_overview():
    """สรุป EPO Response ทั้งคลินิก."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import epo_response_tracker
        overview = epo_response_tracker.get_clinic_epo_overview(
            storage, master_registry
        )
        return jsonify(overview), 200
    except Exception as e:
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/epo-tracker/<hn>", methods=["GET"])
def api_epo_patient(hn):
    """EPO Response ของผู้ป่วยรายเดียว."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import epo_response_tracker
        patient_folder = master_registry.get_folder_for_hn(hn)
        if not patient_folder:
            return jsonify({"error": f"ไม่พบ HN {hn}"}), 404
        summary = epo_response_tracker.get_patient_epo_summary(
            storage, patient_folder
        )
        return jsonify(summary), 200
    except Exception as e:
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/hd-schedules", methods=["GET", "POST"])
def api_hd_schedules():
    """จัดการตารางฟอกเลือด."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import hd_schedule_reminder
        if request.method == "GET":
            schedules = hd_schedule_reminder.api_list_schedules(storage)
            return jsonify(schedules), 200
        else:
            data = request.get_json(silent=True) or {}
            if "schedules" in data:
                result = hd_schedule_reminder.api_bulk_import_schedules(
                    storage, data["schedules"]
                )
                return jsonify(result), 200
            clinic_hn = data.get("clinic_hn", "").strip()
            hd_per_week = data.get("hd_per_week", 3)
            days = data.get("days")
            time_str = data.get("time", "08:00")
            if not clinic_hn:
                return jsonify({"error": "ต้องระบุ clinic_hn"}), 400
            result = hd_schedule_reminder.set_schedule(
                storage, clinic_hn, hd_per_week, days, time_str
            )
            return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/hd-schedules/send-reminders", methods=["POST"])
def api_send_hd_reminders():
    """ส่ง LINE reminder สำหรับผู้ป่วยที่มีนัดฟอกพรุ่งนี้."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import hd_schedule_reminder

        def _line_push(user_id, messages):
            """Push LINE message ผ่าน existing infrastructure."""
            import requests as _req
            token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
            if not token:
                return False
            resp = _req.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"to": user_id, "messages": messages},
                timeout=10,
            )
            return resp.status_code == 200

        result = hd_schedule_reminder.send_reminders(
            storage, master_registry, hn_registry, _line_push
        )
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/ckd-staging", methods=["GET"])
def api_ckd_distribution():
    """สรุป CKD Stage ทั้งคลินิก."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import ckd_staging
        dist = ckd_staging.get_clinic_ckd_distribution(
            storage, master_registry
        )
        return jsonify(dist), 200
    except Exception as e:
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/ckd-staging/alerts", methods=["GET"])
def api_ckd_alerts():
    """รายการผู้ป่วย eGFR ลดเร็ว."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import ckd_staging
        alerts = ckd_staging.get_rapid_decliners_alert(
            storage, master_registry
        )
        return jsonify({"alerts": alerts, "count": len(alerts)}), 200
    except Exception as e:
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/ckd-staging/<hn>", methods=["GET"])
def api_ckd_patient(hn):
    """CKD Profile ของผู้ป่วยรายเดียว."""
    err = _admin_api_auth()
    if err:
        return err
    try:
        import ckd_staging
        patient_folder = master_registry.get_folder_for_hn(hn)
        if not patient_folder:
            return jsonify({"error": f"ไม่พบ HN {hn}"}), 404
        profile = ckd_staging.get_patient_ckd_profile(
            storage, patient_folder
        )
        return jsonify(profile), 200
    except Exception as e:
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/analytics/status", methods=["GET"])
def api_analytics_status():
    """สถานะ modules วิเคราะห์ทั้งหมด."""
    err = _admin_api_auth()
    if err:
        return err
    status = {}
    for mod_name in [
        "auto_weekly_report", "dialysis_adequacy",
        "epo_response_tracker", "hd_schedule_reminder",
        "ckd_staging",
    ]:
        try:
            mod = __import__(mod_name)
            status[mod_name] = mod.get_status()
        except Exception as _e:
            status[mod_name] = {"error": type(_e).__name__}
    return jsonify(status), 200


# ═══════════════════════════════════════════════════════════
# Rich Menu Setup — สร้าง Rich Menu ผ่าน LINE Messaging API
# ═══════════════════════════════════════════════════════════

@app.route("/admin/api/v2/richmenu/setup", methods=["POST"])
def api_richmenu_setup():
    """สร้าง Rich Menu 3 ปุ่ม (ดูผลแล็บ / ส่งเอกสาร / ลงทะเบียน)
    แล้วตั้งเป็น default Rich Menu อัตโนมัติ.

    ต้องส่งภาพ Rich Menu (2500x843) เป็น multipart/form-data field 'image'
    หรือเรียกโดยไม่ส่งภาพเพื่อสร้าง Rich Menu แบบไม่มีภาพ (ใช้ภาพทีหลัง)
    """
    err = _admin_api_auth()
    if err:
        return err

    if not LINE_CHANNEL_ACCESS_TOKEN:
        return jsonify({"error": "LINE_CHANNEL_ACCESS_TOKEN not set"}), 500

    import requests as _req

    _liff_id = os.environ.get("LIFF_ID", "")
    _liff_url = f"https://liff.line.me/{_liff_id}" if _liff_id else ""

    # Step 1: สร้าง Rich Menu object
    rich_menu_body = {
        "size": {"width": 2500, "height": 843},
        "selected": True,
        "name": "ระนอง รักษ์ไต เมนูหลัก",
        "chatBarText": "เมนู",
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": 833, "height": 843},
                "action": {
                    "type": "uri",
                    "label": "ดูผลแล็บ",
                    "uri": _liff_url or f"{_RENDER_BASE}/liff",
                },
            },
            {
                "bounds": {"x": 833, "y": 0, "width": 834, "height": 843},
                "action": {
                    "type": "message",
                    "label": "ส่งเอกสาร",
                    "text": "ส่งเอกสาร",
                },
            },
            {
                "bounds": {"x": 1667, "y": 0, "width": 833, "height": 843},
                "action": {
                    "type": "message",
                    "label": "ลงทะเบียน",
                    "text": "ลงทะเบียน",
                },
            },
        ],
    }

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        # สร้าง Rich Menu
        resp1 = _req.post(
            "https://api.line.me/v2/bot/richmenu",
            json=rich_menu_body,
            headers=headers,
            timeout=15,
        )
        if resp1.status_code != 200:
            return jsonify({
                "error": "create_rich_menu_failed",
                "status": resp1.status_code,
                "detail": resp1.text[:500],
            }), 500

        rich_menu_id = resp1.json().get("richMenuId", "")
        logger.info("Rich Menu created: %s", rich_menu_id[:16])

        # Step 2: อัปโหลดภาพ (ถ้ามี)
        image_uploaded = False
        image_file = request.files.get("image")
        if image_file:
            img_data = image_file.read()
            resp2 = _req.post(
                f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
                data=img_data,
                headers={
                    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                    "Content-Type": image_file.content_type or "image/png",
                },
                timeout=30,
            )
            image_uploaded = resp2.status_code == 200
            if not image_uploaded:
                logger.warning(
                    "Rich Menu image upload failed: %d", resp2.status_code
                )

        # Step 3: ตั้งเป็น default
        resp3 = _req.post(
            f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        default_set = resp3.status_code == 200
        default_error = "" if default_set else resp3.text[:300]

        return jsonify({
            "success": True,
            "richMenuId": rich_menu_id,
            "image_uploaded": image_uploaded,
            "default_set": default_set,
            "default_error": default_error,
            "liff_url": _liff_url,
            "note": "อัปโหลดภาพภายหลังได้ที่ POST /admin/api/v2/richmenu/{id}/image"
            if not image_uploaded else "",
        }), 200

    except Exception as e:
        logger.warning("Rich Menu setup failed: %s", type(e).__name__)
        return jsonify({"error": type(e).__name__, "message": str(e)}), 500


@app.route("/admin/api/v2/richmenu/<menu_id>/default", methods=["POST"])
def api_richmenu_set_default(menu_id):
    """ตั้ง Rich Menu เป็น default สำหรับผู้ใช้ทุกคน."""
    err = _admin_api_auth()
    if err:
        return err

    if not LINE_CHANNEL_ACCESS_TOKEN:
        return jsonify({"error": "LINE_CHANNEL_ACCESS_TOKEN not set"}), 500

    import requests as _req

    resp = _req.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{menu_id}",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    if resp.status_code == 200:
        return jsonify({"success": True, "richMenuId": menu_id}), 200
    return jsonify({
        "error": "set_default_failed",
        "status": resp.status_code,
        "detail": resp.text[:500],
    }), 500


@app.route("/admin/api/v2/richmenu/<menu_id>/image", methods=["POST"])
def api_richmenu_upload_image(menu_id):
    """อัปโหลดภาพ Rich Menu (2500x843 PNG/JPEG)."""
    err = _admin_api_auth()
    if err:
        return err

    if not LINE_CHANNEL_ACCESS_TOKEN:
        return jsonify({"error": "LINE_CHANNEL_ACCESS_TOKEN not set"}), 500

    import requests as _req

    image_file = request.files.get("image")
    if not image_file:
        return jsonify({"error": "กรุณาส่งไฟล์ภาพใน field 'image'"}), 400

    img_data = image_file.read()
    resp = _req.post(
        f"https://api-data.line.me/v2/bot/richmenu/{menu_id}/content",
        data=img_data,
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": image_file.content_type or "image/png",
        },
        timeout=30,
    )
    if resp.status_code == 200:
        return jsonify({"success": True}), 200
    return jsonify({"error": "upload_failed", "status": resp.status_code}), 500


@app.route("/admin/api/v2/richmenu/deploy", methods=["POST"])
def api_richmenu_deploy():
    """สร้าง Rich Menu + อัปโหลดรูปจาก repo + ตั้ง default — ครบจบในขั้นตอนเดียว.

    อ่าน rich_menu_v2.png จาก directory ที่ deploy ไว้ ไม่ต้องส่งไฟล์มา
    """
    err = _admin_api_auth()
    if err:
        return err

    if not LINE_CHANNEL_ACCESS_TOKEN:
        return jsonify({"error": "LINE_CHANNEL_ACCESS_TOKEN not set"}), 500

    import requests as _req

    # หารูป Rich Menu จาก repo
    _base = os.path.dirname(os.path.abspath(__file__))
    img_path = os.path.join(_base, "rich_menu_v2.png")
    if not os.path.exists(img_path):
        return jsonify({"error": "rich_menu_v2.png not found in repo"}), 404

    _liff_id = os.environ.get("LIFF_ID", "")
    _liff_url = f"https://liff.line.me/{_liff_id}" if _liff_id else ""

    headers_json = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    # Step 1: สร้าง Rich Menu
    rich_menu_body = {
        "size": {"width": 2500, "height": 843},
        "selected": True,
        "name": "ระนอง รักษ์ไต เมนูหลัก v2",
        "chatBarText": "เมนู",
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": 833, "height": 843},
                "action": {
                    "type": "uri",
                    "label": "ดูผลแล็บ",
                    "uri": _liff_url or f"{_RENDER_BASE}/liff",
                },
            },
            {
                "bounds": {"x": 833, "y": 0, "width": 834, "height": 843},
                "action": {
                    "type": "message",
                    "label": "ส่งเอกสาร",
                    "text": "ส่งเอกสาร",
                },
            },
            {
                "bounds": {"x": 1667, "y": 0, "width": 833, "height": 843},
                "action": {
                    "type": "message",
                    "label": "ลงทะเบียน",
                    "text": "ลงทะเบียน",
                },
            },
        ],
    }

    try:
        resp1 = _req.post(
            "https://api.line.me/v2/bot/richmenu",
            json=rich_menu_body,
            headers=headers_json,
            timeout=15,
        )
        if resp1.status_code != 200:
            return jsonify({
                "error": "create_failed",
                "status": resp1.status_code,
                "detail": resp1.text[:500],
            }), 500

        menu_id = resp1.json().get("richMenuId", "")

        # Step 2: อัปโหลดรูป
        with open(img_path, "rb") as f:
            img_data = f.read()
        resp2 = _req.post(
            f"https://api-data.line.me/v2/bot/richmenu/{menu_id}/content",
            data=img_data,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "image/png",
            },
            timeout=30,
        )
        img_ok = resp2.status_code == 200

        # Step 3: ตั้งเป็น default
        resp3 = _req.post(
            f"https://api.line.me/v2/bot/user/all/richmenu/{menu_id}",
            headers=headers_json,
            timeout=15,
        )
        default_ok = resp3.status_code == 200

        return jsonify({
            "success": True,
            "richMenuId": menu_id,
            "image_uploaded": img_ok,
            "default_set": default_ok,
            "liff_url": _liff_url,
        }), 200

    except Exception as e:
        logger.warning("Rich Menu deploy failed: %s", type(e).__name__)
        return jsonify({"error": type(e).__name__}), 500


# ═══════════════════════════════════════════════════════════
# LIFF HD Labs — ผลเลือดจาก HD Smart Monitoring ใน LINE OA
# (ว้าว #3 LINE Patient Portal — ไม่แตะ liff_app.py ที่ LOCKED)
# ═══════════════════════════════════════════════════════════


@app.route("/liff/api/my-hd-labs", methods=["POST", "OPTIONS"])
def liff_api_my_hd_labs():
    """Structured HD lab data for the LIFF patient portal.

    Uses the same LIFF token verification as my-labs, then queries
    hd_lab_results + hd_fluid_assessments via the patient_portal module.
    Returns Hb, Hct, Kt/V, Ferritin trends + friendly Thai interpretations.
    """
    if request.method == "OPTIONS":
        return "", 204, _liff_cors_headers()

    body = request.get_json(silent=True) or {}
    id_token = body.get("id_token", "")
    if not id_token:
        return jsonify({"error": "id_token required"}), 400, _liff_cors_headers()

    try:
        line_user_id = verify_liff_id_token(id_token)
    except Exception:
        return jsonify({"error": "unauthorized"}), 401, _liff_cors_headers()

    owner_hash_full = hash_user_id(line_user_id)
    _is_physician = (owner_hash_full in PHYSICIAN_USERS or owner_hash_full in ADMIN_USERS)

    # Resolve HN: physician may pass search_hn, patient uses their linked HN
    patient_hn = ""
    if _is_physician:
        patient_hn = str(body.get("search_hn", "")).upper().replace("HN", "").replace(" ", "").strip()[:20]
        if not patient_hn:
            return jsonify({"error": "physician: ระบุ HN", "is_physician": True}), 400, _liff_cors_headers()
    else:
        entry = hn_registry.get_by_owner(owner_hash_full)
        if entry and entry.clinic_hn:
            patient_hn = entry.clinic_hn
        else:
            return jsonify({"error": "ยังไม่ได้ผูกบัญชี กรุณาขอรหัสจากเจ้าหน้าที่", "need_link": True}), 403, _liff_cors_headers()

    # Use the patient_portal module for data
    try:
        import psycopg2
        db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        if not db_url:
            return jsonify({"error": "ระบบไม่พร้อม"}), 503, _liff_cors_headers()
        conn = psycopg2.connect(db_url, connect_timeout=5)
        from hd_module.patient_portal import portal_data
        data = portal_data(conn, patient_hn)
        conn.close()
        if not data:
            return jsonify({"error": "ไม่พบข้อมูลผู้ป่วย"}), 404, _liff_cors_headers()
        data["is_physician"] = _is_physician
        return jsonify(data), 200, _liff_cors_headers()
    except Exception as e:
        logger.error("liff_api_my_hd_labs: %s", e)
        return jsonify({"error": "internal error"}), 500, _liff_cors_headers()


@app.route("/liff/hd-labs", methods=["GET"])
def liff_hd_labs_page():
    """Standalone LIFF page for HD lab results — opened from Rich Menu or LIFF link.

    This is a separate page (not inside the locked LIFF HTML) that uses LIFF SDK
    to verify identity and then renders the patient portal with HD lab data.
    """
    liff_id = os.environ.get("LIFF_ID", "")
    render_base = (os.environ.get("RENDER_EXTERNAL_URL") or request.host_url).rstrip("/")
    forwarded = request.headers.get("X-Forwarded-Proto", "")
    if forwarded == "https" and render_base.startswith("http://"):
        render_base = "https://" + render_base[len("http://"):]

    page = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>ผลเลือด HD — รักษ์ไต</title>
<script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<style>
  :root{{--bg:#f5f7fa;--card:#fff;--accent:#2980b9;--danger:#e74c3c;--success:#27ae60;--warn:#f39c12;--text:#2c3e50;--muted:#7f8c8d;--border:#eaecf0}}
  @media(prefers-color-scheme:dark){{:root{{--bg:#0d1117;--card:#161b22;--accent:#58a6ff;--danger:#f85149;--success:#3fb950;--warn:#d29922;--text:#e6edf3;--muted:#8b949e;--border:#30363d}}}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);padding:0 0 60px;max-width:480px;margin:0 auto;line-height:1.5}}
  .hero{{background:linear-gradient(135deg,#2980b9,#3498db);color:#fff;padding:28px 20px 24px;text-align:center;border-radius:0 0 24px 24px}}
  .hero h1{{font-size:1.1rem;opacity:.9;font-weight:400}}.hero .name{{font-size:1.6rem;font-weight:800;margin-top:4px}}
  .hero .next{{margin-top:12px;background:rgba(255,255,255,.18);border-radius:14px;padding:10px 16px;font-size:.95rem;font-weight:600}}
  .hero .next .big{{font-size:1.5rem;display:block;margin-top:2px}}
  .card{{background:var(--card);border-radius:16px;padding:16px;margin:12px 16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
  .card h2{{font-size:.95rem;font-weight:700;margin-bottom:8px;display:flex;align-items:center;gap:6px}}
  .info-row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);font-size:.88rem}}
  .info-row:last-child{{border:none}}.label{{color:var(--muted)}}.value{{font-weight:600}}
  .interp{{padding:10px 12px;border-radius:12px;margin:6px 0;font-size:.88rem;font-weight:600;display:flex;align-items:center;gap:8px}}
  .interp.ok{{background:rgba(39,174,96,.1);color:var(--success)}}.interp.low{{background:rgba(243,156,18,.1);color:var(--warn)}}
  .interp.critical{{background:rgba(231,76,60,.1);color:var(--danger)}}.interp.high{{background:rgba(243,156,18,.1);color:var(--warn)}}
  table{{width:100%;border-collapse:collapse;font-size:.78rem}}
  th{{background:var(--accent);color:#fff;padding:8px 6px;text-align:left;font-weight:600;font-size:.72rem;white-space:nowrap}}
  td{{padding:8px 6px;border-bottom:1px solid var(--border);white-space:nowrap}}
  .tip{{padding:10px 12px;border-radius:12px;margin:6px 0;font-size:.85rem;background:rgba(41,128,185,.06);border-left:3px solid var(--accent)}}
  .fluid-gauge{{display:flex;align-items:center;gap:12px;padding:8px 0}}
  .fluid-bar{{flex:1;height:12px;background:var(--border);border-radius:6px;overflow:hidden}}
  .fluid-fill{{height:100%;border-radius:6px}}
  .footer{{text-align:center;font-size:.72rem;color:var(--muted);margin-top:20px;padding:12px}}
  #loading{{text-align:center;padding:60px 20px;color:var(--muted)}}
  #loading .spin{{display:inline-block;width:40px;height:40px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:sp .8s linear infinite}}
  @keyframes sp{{to{{transform:rotate(360deg)}}}}
  @media(min-width:481px){{body{{padding-top:12px}}.hero{{margin:0 16px;border-radius:24px}}}}
</style>
</head>
<body>
<div id="app">
  <div id="loading"><div class="spin"></div><p style="margin-top:12px">กำลังเชื่อมต่อ LINE...</p></div>
</div>
<script>
const API = '{render_base}';
const LIFF_ID = '{liff_id}';
function esc(s){{ const d=document.createElement('div'); d.textContent=String(s==null?'':s); return d.innerHTML; }}

async function init(){{
  if(!LIFF_ID){{ document.getElementById('app').innerHTML='<div style="padding:40px;text-align:center;color:var(--danger)">ระบบยังไม่ได้ตั้งค่า LIFF_ID</div>'; return; }}
  try{{
    await liff.init({{ liffId: LIFF_ID }});
    if(!liff.isLoggedIn()){{ liff.login(); return; }}
    const token = liff.getIDToken();
    const r = await fetch(API+'/liff/api/my-hd-labs', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{ id_token: token }})
    }});
    const D = await r.json();
    if(!r.ok){{
      if(D.need_link){{ document.getElementById('app').innerHTML=`<div style="padding:40px 20px;text-align:center"><div style="font-size:3rem">🔗</div><h2 style="margin:12px 0">ยังไม่ได้ผูกบัญชี</h2><p style="color:var(--muted)">กรุณาขอรหัสผูกบัญชี 6 หลักจากเจ้าหน้าที่คลินิก<br>แล้วพิมพ์ "ผูกบัญชี XXXXXX" ในแชทนี้</p></div>`; return; }}
      if(D.is_physician){{ physSearch(token); return; }}
      throw new Error(D.error||'เกิดข้อผิดพลาด');
    }}
    render(D);
  }}catch(e){{ document.getElementById('app').innerHTML=`<div style="padding:40px;text-align:center;color:var(--danger)">${{esc(e.message||'เชื่อมต่อไม่ได้')}}</div>`; }}
}}

function physSearch(token, msg){{
  document.getElementById('app').innerHTML = `<div style="padding:32px 20px;text-align:center">
    <div style="font-size:2.6rem">🩺</div><h2 style="margin:10px 0">โหมดแพทย์ — ดูรายคน</h2>
    <p style="color:var(--muted);font-size:.9rem">ใส่ HN คลินิกของผู้ป่วยที่ต้องการดู</p>
    <input id="physHn" inputmode="numeric" placeholder="เช่น 56001" style="margin-top:12px;padding:12px;font-size:1.1rem;text-align:center;border:1.5px solid var(--border);border-radius:12px;background:var(--card);color:var(--text);width:200px">
    <br><button onclick="physGo()" style="margin-top:12px;padding:12px 28px;font-size:1rem;border:0;border-radius:12px;background:var(--accent);color:#fff;font-weight:700">ดูผลเลือด</button>
    ${{msg?`<p style="color:var(--danger);margin-top:10px">${{esc(msg)}}</p>`:''}}
  </div>`;
  window._physToken = token;
  document.getElementById('physHn').addEventListener('keydown', e=>{{ if(e.key==='Enter') physGo(); }});
}}
async function physGo(){{
  const hn = (document.getElementById('physHn')||{{}}).value||'';
  if(!hn.trim()) return;
  try{{
    const r = await fetch(API+'/liff/api/my-hd-labs', {{ method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{ id_token: window._physToken, search_hn: hn.trim() }}) }});
    const D = await r.json();
    if(!r.ok) throw new Error(D.error||'ไม่พบข้อมูล');
    render(D);
  }}catch(e){{ physSearch(window._physToken, e.message||'ไม่พบข้อมูล'); }}
}}
function render(D){{
  const p=D.profile, nd=p.next_dialysis;
  let h=`<div class="hero"><h1>🏥 คลินิกระนอง รักษ์ไต</h1><div class="name">${{esc(p.name)}}</div>
    ${{nd?`<div class="next">นัดฟอกครั้งถัดไป<span class="big">${{nd.in_days===0?'🟢 วันนี้':(nd.in_days===1?'📅 พรุ่งนี้':'📅 '+nd.text)}}</span>${{p.shift?' · '+p.shift:''}}</div>`:''}}
  </div>`;
  h+=`<div class="card"><h2>👤 ข้อมูลของฉัน</h2>
    <div class="info-row"><span class="label">HN</span><span class="value">${{esc(p.hn)}}</span></div>
    <div class="info-row"><span class="label">ตารางฟอก</span><span class="value">${{esc(p.schedule||'-')}} ${{p.shift?'('+p.shift+')':''}}</span></div>
    <div class="info-row"><span class="label">เส้นฟอกเลือด</span><span class="value">${{esc(p.va||'-')}}</span></div>
    <div class="info-row"><span class="label">น้ำหนักแห้ง</span><span class="value">${{p.dry_weight?p.dry_weight.toFixed(1)+' kg':'-'}}</span></div>
    <div class="info-row"><span class="label">ยากระตุ้นเม็ดเลือด</span><span class="value">${{esc(p.esa_drug||'-')}} ${{esc(p.esa_dose||'')}}</span></div>
  </div>`;
  if(D.goal && D.goal.text){{
    h+=`<div class="card" style="border:2px solid var(--accent)"><h2>🎯 เป้าหมายจากแพทย์ของฉัน</h2>
      <div style="font-size:1rem;font-weight:600;line-height:1.6">${{esc(D.goal.text)}}</div></div>`;
  }}
  const it=D.interpretations||{{}};
  if(Object.keys(it).length){{
    h+=`<div class="card"><h2>🩺 สรุปผลแล็บล่าสุด</h2>`;
    for(const [k,v] of Object.entries(it)) h+=`<div class="interp ${{v.status}}">${{v.emoji}} ${{esc(v.text)}}</div>`;
    h+=`</div>`;
  }}
  const fl=D.fluid;
  if(fl){{
    const pct=Math.min(100,Math.max(0,50+fl.oh*15));
    const col=fl.oh>2?'var(--danger)':(fl.oh>1?'var(--warn)':(fl.oh<-1?'var(--accent)':'var(--success)'));
    h+=`<div class="card"><h2>💧 สารน้ำในร่างกาย</h2>
      <div class="fluid-gauge"><span style="font-size:.75rem;color:var(--muted)">น้อย</span><div class="fluid-bar"><div class="fluid-fill" style="width:${{pct}}%;background:${{col}}"></div></div><span style="font-size:.75rem;color:var(--muted)">มาก</span></div>
      <div style="text-align:center;font-weight:700;color:${{col}};font-size:1rem">OH ${{fl.oh>0?'+':''}}${{fl.oh.toFixed(1)}} L</div>
      <div style="text-align:center;font-size:.85rem;margin-top:4px">${{esc(fl.text)}}</div>
    </div>`;
  }}
  const labs=(D.labs||[]).slice().reverse();
  if(labs.length>=2){{
    const hbs=labs.filter(l=>l.hb!=null);
    if(hbs.length>=2) h+=`<div class="card"><h2>📈 แนวโน้ม Hb</h2><div style="overflow-x:auto">${{svgC(hbs.map(l=>({{x:l.date,y:l.hb}})),8,15,10,12,'Hb g/dL')}}</div><div style="font-size:.72rem;color:var(--muted)">แถบเขียว = เป้า 10-12</div></div>`;
    const ktvs=labs.filter(l=>l.ktv!=null);
    if(ktvs.length>=2) h+=`<div class="card"><h2>📈 แนวโน้ม Kt/V</h2><div style="overflow-x:auto">${{svgC(ktvs.map(l=>({{x:l.date,y:l.ktv}})),0.5,2.5,1.2,999,'Kt/V')}}</div><div style="font-size:.72rem;color:var(--muted)">เส้นเขียว = เป้า ≥1.2</div></div>`;
  }}
  const idwg=(D.idwg||[]).filter(x=>x.pct!=null);
  if(idwg.length>=2) h+=`<div class="card"><h2>⚖️ น้ำหนักขึ้นระหว่างรอบฟอก</h2><div style="overflow-x:auto">${{svgC(idwg.map(x=>({{x:x.date,y:x.pct}})),0,7,0,5,'IDWG % ของ DW')}}</div><div style="font-size:.72rem;color:var(--muted)">แถบเขียว = ไม่เกิน 5% ของ DW (เป้าที่ดี < 3%)</div></div>`;
  const ss=D.sessions||[];
  if(ss.length){{
    h+=`<div class="card"><h2>📋 ผลฟอก ${{ss.length}} รอบล่าสุด</h2><div style="overflow-x:auto"><table>
      <tr><th>วันที่</th><th>น้ำหนัก</th><th>UF</th><th>Hb</th><th>ESA</th></tr>`;
    ss.forEach(s=>{{
      const dt=new Date(s.date+'T00:00:00').toLocaleDateString('th-TH',{{day:'numeric',month:'short'}});
      h+=`<tr><td>${{dt}}</td><td>${{s.pre_weight?s.pre_weight.toFixed(1):'−'}}→${{s.post_weight?s.post_weight.toFixed(1):'−'}}</td><td>${{s.uf?Math.round(s.uf)+' mL':'−'}}</td><td>${{s.hb?s.hb.toFixed(1):'−'}}</td><td>${{s.esa?'💉':'−'}}</td></tr>`;
    }});
    h+=`</table></div></div>`;
  }}
  const tips=D.tips||[];
  if(tips.length){{ h+=`<div class="card"><h2>💡 คำแนะนำสำหรับคุณ</h2>`; tips.forEach(t=>{{ h+=`<div class="tip">${{t.icon}} ${{esc(t.text)}}</div>`; }}); h+=`</div>`; }}
  h+=`<div class="footer">HD Smart Monitoring — คลินิกระนอง รักษ์ไต<br>ข้อมูลนี้เป็นข้อมูลส่วนบุคคล กรุณาไม่เผยแพร่</div>`;
  document.getElementById('app').innerHTML=h;
}}

function svgC(pts,minY,maxY,tLo,tHi,label){{
  const W=320,H=140,ml=36,mr=10,mt=15,mb=28,pw=W-ml-mr,ph=H-mt-mb;
  const lo=Math.min(minY,...pts.map(p=>p.y)),hi=Math.max(maxY,...pts.map(p=>p.y));
  const sx=i=>ml+i/(pts.length-1)*pw, sy=v=>mt+ph-(v-lo)/(hi-lo)*ph;
  const tl=Math.max(lo,tLo),th=Math.min(hi,tHi);
  let s=`<svg viewBox="0 0 ${{W}} ${{H}}" style="width:100%;max-width:${{W}}px">`;
  if(tl<th) s+=`<rect x="${{ml}}" y="${{sy(th)}}" width="${{pw}}" height="${{sy(tl)-sy(th)}}" fill="rgba(39,174,96,.1)" rx="4"/>`;
  for(let v=Math.ceil(lo);v<=hi;v+=Math.max(1,Math.round((hi-lo)/5))){{
    const y=sy(v); s+=`<line x1="${{ml}}" y1="${{y}}" x2="${{W-mr}}" y2="${{y}}" stroke="var(--border)" stroke-width="0.5"/>`;
    s+=`<text x="${{ml-4}}" y="${{y+3}}" text-anchor="end" fill="var(--muted)" font-size="9">${{v%1?v.toFixed(1):v}}</text>`;
  }}
  const path=pts.map((p,i)=>`${{i?'L':'M'}}${{sx(i).toFixed(1)}},${{sy(p.y).toFixed(1)}}`).join(' ');
  s+=`<path d="${{path}}" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>`;
  pts.forEach((p,i)=>{{
    const c=p.y>=tLo&&p.y<=tHi?'var(--success)':(p.y<tLo?'var(--danger)':'var(--warn)');
    s+=`<circle cx="${{sx(i)}}" cy="${{sy(p.y)}}" r="4" fill="${{c}}" stroke="var(--card)" stroke-width="2"/>`;
    if(i%Math.max(1,Math.floor(pts.length/6))===0||i===pts.length-1){{
      const d=new Date(p.x+'T00:00:00'); s+=`<text x="${{sx(i)}}" y="${{H-4}}" text-anchor="middle" fill="var(--muted)" font-size="8">${{d.getDate()}}/${{d.getMonth()+1}}</text>`;
    }}
  }});
  if(tLo<999) s+=`<line x1="${{ml}}" y1="${{sy(tLo)}}" x2="${{W-mr}}" y2="${{sy(tLo)}}" stroke="var(--success)" stroke-width="1" stroke-dasharray="4,3"/>`;
  s+=`<text x="${{ml+2}}" y="${{mt-4}}" fill="var(--muted)" font-size="8">${{label}}</text></svg>`;
  return s;
}}

init();
</script>
</body>
</html>"""
    return page, 200, {"Content-Type": "text/html; charset=utf-8"}


# ═══════════════════════════════════════════════════════════
# Main (สำหรับทดสอบ local เท่านั้น — Render ใช้ Gunicorn)
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("Running in development mode (Flask dev server)")
    app.run(host="0.0.0.0", port=PORT, debug=False)
