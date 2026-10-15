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
#   "testing"    = ค่าเริ่มต้น — ปฏิเสธ webhook, ไม่ดาวน์โหลด, ไม่เรียก API
#   "production" = ใช้งานจริง — ต้องตั้งค่าด้วยตนเองใน Render Dashboard
APP_ENV = os.getenv("APP_ENV", "testing").lower().strip()
IS_TESTING = APP_ENV != "production"

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
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")
STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "")

# โฟลเดอร์ชั่วคราวบน Render (ephemeral)
BASE_DIR = os.getenv("BASE_DIR", "/tmp/ranong_raktai")

# ─── ข้อจำกัดไฟล์ ────────────────────────────────────────
MAX_FILE_SIZE = 20 * 1024 * 1024      # 20 MB
MAX_PDF_PAGES = 50
MAX_PDF_PAGES_VISION = 5
ALLOWED_EXTENSIONS = {".txt", ".pdf", ".jpg", ".jpeg", ".png"}

FOLDERS = {
    "inbox":    "01_LINE_INBOX",
    "bills":    "02_BILLS_RECEIPTS",
    "rx":       "03_PRESCRIPTIONS",
    "lab":      "04_LAB_RESULTS",
    "dialysis": "05_DIALYSIS_RECORDS",
    "review":   "06_AI_REVIEW",
}

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

    def save(self, data: bytes, category: str, filename: str) -> str:
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

    def save(self, data: bytes, category: str, filename: str) -> str:
        # TODO: เชื่อมต่อ boto3 เมื่อพร้อม
        # import boto3
        # s3 = boto3.client('s3')
        # key = f"{FOLDERS.get(category, FOLDERS['review'])}/{filename}"
        # s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        # return f"s3://{self.bucket}/{key}"
        logger.warning("S3 Storage ยังไม่เชื่อมต่อ — fallback ไป local")
        return StorageLocal().save(data, category, filename)

    def list_files(self, category: str) -> list[str]:
        logger.warning("S3 Storage ยังไม่เชื่อมต่อ")
        return []


class StorageGCS:
    """เก็บไฟล์บน Google Cloud Storage — ยังไม่เชื่อมต่อจริง (placeholder)."""

    def __init__(self):
        self.bucket = STORAGE_BUCKET
        logger.info(f"GCS Storage: bucket={self.bucket} (ยังไม่เชื่อมต่อ)")

    def save(self, data: bytes, category: str, filename: str) -> str:
        # TODO: เชื่อมต่อ google-cloud-storage เมื่อพร้อม
        # from google.cloud import storage
        # client = storage.Client()
        # bucket = client.bucket(self.bucket)
        # blob = bucket.blob(f"{FOLDERS.get(category, FOLDERS['review'])}/{filename}")
        # blob.upload_from_string(data)
        # return f"gs://{self.bucket}/{blob.name}"
        logger.warning("GCS Storage ยังไม่เชื่อมต่อ — fallback ไป local")
        return StorageLocal().save(data, category, filename)

    def list_files(self, category: str) -> list[str]:
        logger.warning("GCS Storage ยังไม่เชื่อมต่อ")
        return []


def get_storage():
    """สร้าง storage instance ตาม STORAGE_BACKEND."""
    if STORAGE_BACKEND == "s3":
        return StorageS3()
    elif STORAGE_BACKEND == "gcs":
        return StorageGCS()
    return StorageLocal()


storage = get_storage()


# ═══════════════════════════════════════════════════════════
# File Validation
# ═══════════════════════════════════════════════════════════

MAGIC_BYTES = {
    b"\xff\xd8\xff":          ".jpg",   # JPEG
    b"\x89PNG\r\n\x1a\n":    ".png",   # PNG
    b"%PDF":                  ".pdf",   # PDF
}


def detect_file_type(filepath: str) -> str | None:
    """ตรวจสอบชนิดไฟล์จริงจาก magic bytes."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(8)
    except Exception:
        return None

    for magic, ext in MAGIC_BYTES.items():
        if header[:len(magic)] == magic:
            return ext

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            f.read(1024)
        return ".txt"
    except (UnicodeDecodeError, Exception):
        pass

    return None


def validate_file(filepath: str) -> tuple[bool, str]:
    """ตรวจสอบไฟล์ก่อนประมวลผล."""
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

    if ext != ".txt":
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
        logger.error(f"อ่าน PDF ล้มเหลว: {e}")
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

MAX_OCR_PAGES = 5


def extract_text_with_ocr(filepath: str) -> str:
    """ดึงข้อความด้วย Tesseract OCR (tha+eng)."""
    if not HAS_TESSERACT or not HAS_PIL:
        logger.warning("OCR ไม่พร้อม — ขาด pytesseract หรือ Pillow")
        return ""

    ext = os.path.splitext(filepath)[1].lower()
    ocr_lang = "tha+eng"

    try:
        if ext in (".jpg", ".jpeg", ".png"):
            img = Image.open(filepath)
            text = pytesseract.image_to_string(img, lang=ocr_lang)
            logger.info(f"OCR ภาพ: ได้ {len(text.strip())} ตัวอักษร")
            return text.strip()

        if ext == ".pdf":
            if not HAS_PYMUPDF:
                logger.warning("PyMuPDF ไม่ได้ติดตั้ง — ไม่สามารถ OCR PDF สแกนได้")
                return ""

            doc = pymupdf.open(filepath)
            num_pages = min(len(doc), MAX_OCR_PAGES)
            texts = []

            for i in range(num_pages):
                page = doc[i]
                mat = pymupdf.Matrix(300 / 72, 300 / 72)
                pix = page.get_pixmap(matrix=mat)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                page_text = pytesseract.image_to_string(img, lang=ocr_lang)
                if page_text.strip():
                    texts.append(page_text.strip())

            doc.close()
            full_text = "\n\n".join(texts)
            logger.info(f"OCR PDF สแกน ({num_pages} หน้า): ได้ {len(full_text)} ตัวอักษร")
            return full_text

    except Exception as e:
        logger.error(f"OCR ล้มเหลว ({filepath}): {e}")

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
    if IS_TESTING:
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
    except Exception as e:
        logger.warning(f"ตอบกลับ LINE ไม่สำเร็จ: {e}")


def download_line_content(message_id: str) -> bytes | None:
    """ดาวน์โหลดไฟล์จาก LINE Content API."""
    if IS_TESTING:
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
        logger.error(f"ดาวน์โหลดไฟล์จาก LINE ล้มเหลว (msg_id={message_id}): {e}")
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


def save_to_inbox(data: bytes, filename: str) -> str:
    """บันทึกไฟล์ลง inbox (ephemeral disk)."""
    # ป้องกัน path traversal — ใช้เฉพาะชื่อไฟล์ ไม่รวม path
    filename = os.path.basename(filename)
    if not filename:
        filename = f"unnamed_{datetime.now():%Y%m%d_%H%M%S}.dat"
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
    logger.info(f"บันทึกไฟล์: {filepath} ({len(data):,} bytes)")
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
            try:
                err_body = resp.text[:500]
            except Exception:
                err_body = "(ไม่สามารถอ่าน response ได้)"
            logger.error(f"Claude API HTTP {resp.status_code}: {err_body}")
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
        logger.error(f"Claude API error: {e}")
        return None


def classify_with_claude(filepath: str) -> dict:
    """ส่งเอกสารไปให้ Claude API จำแนกประเภท."""
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
        logger.error(f"classify_with_claude error: {e}")
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

    for cat, matched in all_matches.items():
        if matched:
            logger.info(f"  keyword [{cat}]: {len(matched)} hits = {matched[:5]}")

    return {
        "category": best,
        "reason": f"keyword match ({best_score} hits)",
        "confidence": min(best_score / 5.0, 1.0) if best_score > 0 else 0.0,
    }


def copy_to_category(src_path: str, category: str) -> str:
    """คัดลอกไฟล์ไปยังโฟลเดอร์ตามประเภท + cloud storage."""
    # Local copy (ephemeral)
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
    logger.info(f"คัดลอก → {dest_path}")

    # Cloud storage (ถ้าตั้งค่าไว้)
    if STORAGE_BACKEND != "local":
        try:
            with open(src_path, "rb") as f:
                data = f.read()
            cloud_path = storage.save(data, category, filename)
            logger.info(f"Cloud storage → {cloud_path}")
        except Exception as e:
            logger.error(f"Cloud storage error: {e}")

    return dest_path


def process_file_async(filepath: str, reply_token: str | None = None):
    """ประมวลผลไฟล์ในเธรดแยก."""
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
            dest = copy_to_category(filepath, category)

            # บันทึก log
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "source": filepath,
                "destination": dest,
                "file_size": os.path.getsize(filepath),
                "file_type": os.path.splitext(filepath)[1].lower(),
                "classification": result,
            }
            log_file = os.path.join(BASE_DIR, "logs", "classification_log.jsonl")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

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
        except Exception as e:
            logger.error(f"ประมวลผลไฟล์ล้มเหลว: {e}")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


# ═══════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    """รับ LINE Webhook events."""
    logger.info(f"POST /webhook received (content_length={request.content_length}, mode={'testing' if IS_TESTING else 'production'})")

    # Testing Mode Guard
    if IS_TESTING:
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

        if event_type == "message":
            msg = event.get("message", {})
            msg_type = msg.get("type")
            msg_id = msg.get("id")

            if msg_type == "image":
                logger.info(f"รับภาพ (id={msg_id})")
                ext = get_content_type_ext(msg_id)
                if ext not in ALLOWED_EXTENSIONS:
                    ext = ".jpg"
                data = download_line_content(msg_id)
                if data:
                    filename = f"LINE_{datetime.now():%Y%m%d_%H%M%S}_{msg_id}{ext}"
                    filepath = save_to_inbox(data, filename)
                    process_file_async(filepath, reply_token)
                else:
                    reply_message(reply_token, "ไม่สามารถดาวน์โหลดภาพได้")

            elif msg_type == "file":
                file_name = msg.get("fileName", f"file_{msg_id}")
                logger.info(f"รับไฟล์: {file_name} (id={msg_id})")

                file_ext = os.path.splitext(file_name)[1].lower()
                if file_ext not in ALLOWED_EXTENSIONS:
                    reply_message(
                        reply_token,
                        f"ไม่รองรับไฟล์ประเภท {file_ext}\n"
                        f"รองรับ: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
                    )
                    continue

                data = download_line_content(msg_id)
                if data:
                    safe_name = "".join(
                        c for c in file_name
                        if c.isalnum() or c in ".-_ "
                    ).strip() or f"file_{msg_id}"
                    filename = f"LINE_{datetime.now():%Y%m%d_%H%M%S}_{safe_name}"
                    filepath = save_to_inbox(data, filename)
                    process_file_async(filepath, reply_token)
                else:
                    reply_message(reply_token, "ไม่สามารถดาวน์โหลดไฟล์ได้")

            elif msg_type == "text":
                text = msg.get("text", "")
                logger.info(f"รับข้อความ: {text[:50]}...")
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
    """Health check endpoint — ทำงานได้โดยไม่ต้องใช้ API Key."""
    return jsonify({
        "status": "running",
        "version": "cloud-1.0",
        "app_env": APP_ENV,
        "testing_mode": IS_TESTING,
        "timestamp": datetime.now().isoformat(),
        "ai_classify": ENABLE_AI_CLASSIFY and not IS_TESTING,
        "storage_backend": STORAGE_BACKEND,
        "ocr_ready": HAS_TESSERACT and HAS_PIL,
        "pymupdf_ready": HAS_PYMUPDF,
        "supported_types": list(ALLOWED_EXTENSIONS),
    }), 200


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
logger.info(f"TESTING MODE: {'เปิด (ปลอดภัย — ไม่รับเอกสาร/เรียก API)' if IS_TESTING else 'ปิด (Production)'}")
logger.info(f"PORT: {PORT}")
logger.info(f"STORAGE_BACKEND: {STORAGE_BACKEND}")
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
logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════
# Main (สำหรับทดสอบ local เท่านั้น — Render ใช้ Gunicorn)
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info("Running in development mode (Flask dev server)")
    app.run(host="0.0.0.0", port=PORT, debug=False)
