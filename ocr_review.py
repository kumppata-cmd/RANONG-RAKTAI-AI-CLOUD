#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR Review & Structured Data Extraction — RANONG RAKTAI AI 2.0

Feature 4: AI OCR อ่านและจัดหมวดหมู่เอกสารขั้นสูง

ฟีเจอร์:
  - OCR Review UI: ตรวจสอบ/แก้ไขผล OCR
  - Structured data extraction: ดึงข้อมูลจากบิล (ยอดเงิน, วันที่, รายการ)
  - ไม่ส่งข้อมูลไป External AI (EXTERNAL_AI_CLASSIFICATION_ALLOWED = False)

กฎความปลอดภัย:
  - ใช้ hashed IDs — ไม่แสดง PHI
  - ADMIN_SECRET auth ทุก endpoint
  - Read-only ยกเว้น approve/reject OCR
  - ไม่ log เนื้อหาเอกสาร
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

ocr_review_bp = Blueprint("ocr_review", __name__)

# Module-level refs
_storage = None
_doc_index = None
_audit_logger = None


def _check_admin_secret() -> bool:
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    if not admin_secret:
        return False
    auth_header = request.headers.get("X-Admin-Secret", "")
    auth_param = request.args.get("secret", "")
    return auth_header == admin_secret or auth_param == admin_secret


def _unauthorized():
    return jsonify({"error": "Unauthorized"}), 401


# ═══════════════════════════════════════════════════════════
# Structured Data Extraction (regex-based, no External AI)
# ═══════════════════════════════════════════════════════════

# Thai number words → digits
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

def _normalize_thai_digits(text: str) -> str:
    """แปลงตัวเลขไทยเป็นอารบิก."""
    return text.translate(THAI_DIGITS)


def extract_bill_structured_data(text: str) -> dict:
    """ดึงข้อมูลจากบิล/ใบเสร็จ — regex-based, ไม่ใช้ External AI.

    Returns:
        dict with keys: amounts, dates, tax_id, items, total, vat, company_ref
    """
    if not text or not text.strip():
        return {"amounts": [], "dates": [], "items": [], "total": None, "vat": None}

    normalized = _normalize_thai_digits(text)

    # ── ยอดเงิน ──
    amount_patterns = [
        # Thai: จำนวนเงิน 1,234.56 บาท
        r'(?:จำนวนเงิน|ยอดรวม|รวมทั้งสิ้น|รวมเงิน|ยอดชำระ|ยอดเงิน|ยอดโอน|total|grand\s*total|net\s*amount|amount)\s*[:：]?\s*([\d,]+\.?\d*)',
        # Amount with unit: 1,234.56 บาท
        r'([\d,]+\.?\d*)\s*(?:บาท|baht|THB)',
        # Generic large numbers (likely amounts)
        r'(?:฿|THB)\s*([\d,]+\.?\d*)',
    ]

    amounts = []
    for pat in amount_patterns:
        for m in re.finditer(pat, normalized, re.IGNORECASE):
            try:
                val = float(m.group(1).replace(",", ""))
                if val > 0 and val not in amounts:
                    amounts.append(val)
            except (ValueError, IndexError):
                pass

    # Sort by magnitude — largest likely the total
    amounts.sort(reverse=True)

    # ── วันที่ ──
    date_patterns = [
        # DD/MM/YYYY or DD-MM-YYYY
        r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})',
        # Thai: วันที่ DD เดือน YYYY
        r'วันที่\s*(\d{1,2})\s*(?:มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)\s*(\d{4})',
    ]

    dates = []
    for pat in date_patterns:
        for m in re.finditer(pat, normalized):
            dates.append(m.group(0))
    # Deduplicate
    dates = list(dict.fromkeys(dates))[:5]

    # ── เลขประจำตัวผู้เสียภาษี ──
    tax_id = None
    tax_match = re.search(
        r'(?:เลขประจำตัวผู้เสียภาษี|tax\s*id|เลขที่ผู้เสียภาษี|tin)\s*[:：]?\s*(\d[\d\s\-]{8,})',
        normalized, re.IGNORECASE,
    )
    if tax_match:
        tax_id = re.sub(r'[\s\-]', '', tax_match.group(1))[:13]

    # ── VAT ──
    vat_val = None
    vat_match = re.search(
        r'(?:ภาษีมูลค่าเพิ่ม|VAT|vat|ภาษี\s*7%)\s*[:：]?\s*([\d,]+\.?\d*)',
        normalized, re.IGNORECASE,
    )
    if vat_match:
        try:
            vat_val = float(vat_match.group(1).replace(",", ""))
        except ValueError:
            pass

    # ── Total (largest amount as heuristic) ──
    total = amounts[0] if amounts else None

    # ── Line items (simple heuristic: numbered lines with amounts) ──
    items = []
    item_patterns = [
        # ลำดับ. ชื่อสินค้า จำนวน ราคา
        r'(\d+)\.\s*(.{5,50}?)\s+([\d,]+\.?\d*)\s*(?:บาท|baht)?',
        # Tabular: item description followed by amount
        r'[-•]\s*(.{5,60}?)\s+([\d,]+\.?\d*)\s*(?:บาท|baht)?',
    ]
    for pat in item_patterns:
        for m in re.finditer(pat, normalized, re.IGNORECASE):
            groups = m.groups()
            if len(groups) == 3:
                items.append({
                    "line": groups[0],
                    "description": groups[1].strip(),
                    "amount": groups[2],
                })
            elif len(groups) == 2:
                items.append({
                    "description": groups[0].strip(),
                    "amount": groups[1],
                })
    items = items[:20]  # Limit

    # ── Invoice/Receipt number ──
    invoice_no = None
    inv_match = re.search(
        r'(?:เลขที่ใบเสร็จ|เลขที่|invoice\s*(?:no|number)|receipt\s*(?:no|number)|no\.)\s*[:：]?\s*([A-Za-z0-9\-/]+)',
        normalized, re.IGNORECASE,
    )
    if inv_match:
        invoice_no = inv_match.group(1).strip()

    return {
        "amounts": amounts[:10],
        "total": total,
        "vat": vat_val,
        "dates": dates,
        "tax_id": tax_id,
        "invoice_no": invoice_no,
        "items": items,
        "extraction_method": "regex_local",
    }


def extract_lab_structured_data(text: str) -> dict:
    """ดึงข้อมูลจากผลแล็บ — regex-based.

    Returns: dict with analytes list
    """
    if not text or not text.strip():
        return {"analytes": []}

    normalized = _normalize_thai_digits(text)

    # Common kidney-related lab analytes
    analyte_patterns = {
        "Creatinine": r'(?:creatinine|ครีอะตินิน)\s*[:=]?\s*([\d.]+)\s*(mg/[dD][lL])?',
        "BUN": r'(?:BUN|blood\s*urea\s*nitrogen)\s*[:=]?\s*([\d.]+)\s*(mg/[dD][lL])?',
        "eGFR": r'(?:eGFR|GFR)\s*[:=]?\s*([\d.]+)\s*(mL/min)?',
        "Potassium": r'(?:potassium|K\+?|โพแทสเซียม)\s*[:=]?\s*([\d.]+)\s*(m[mM]ol/[lL])?',
        "Sodium": r'(?:sodium|Na\+?|โซเดียม)\s*[:=]?\s*([\d.]+)\s*(m[mM]ol/[lL])?',
        "Hemoglobin": r'(?:hemoglobin|Hb|Hgb|ฮีโมโกลบิน)\s*[:=]?\s*([\d.]+)\s*(g/[dD][lL])?',
        "Hematocrit": r'(?:hematocrit|Hct|ฮีมาโตคริต)\s*[:=]?\s*([\d.]+)\s*(%)?',
        "Albumin": r'(?:albumin|อัลบูมิน)\s*[:=]?\s*([\d.]+)\s*(g/[dD][lL])?',
        "Phosphorus": r'(?:phosphorus|phosphate|ฟอสฟอรัส)\s*[:=]?\s*([\d.]+)\s*(mg/[dD][lL])?',
        "Calcium": r'(?:calcium|Ca|แคลเซียม)\s*[:=]?\s*([\d.]+)\s*(mg/[dD][lL])?',
        "Uric Acid": r'(?:uric\s*acid|กรดยูริก)\s*[:=]?\s*([\d.]+)\s*(mg/[dD][lL])?',
        "HbA1c": r'(?:HbA1c|A1C|glycated)\s*[:=]?\s*([\d.]+)\s*(%)?',
        "Glucose": r'(?:glucose|FBS|FPG|น้ำตาล)\s*[:=]?\s*([\d.]+)\s*(mg/[dD][lL])?',
        "WBC": r'(?:WBC|white\s*blood\s*cell|เม็ดเลือดขาว)\s*[:=]?\s*([\d.]+)',
        "Platelet": r'(?:platelet|PLT|เกล็ดเลือด)\s*[:=]?\s*([\d.,]+)',
    }

    analytes = []
    for name, pat in analyte_patterns.items():
        m = re.search(pat, normalized, re.IGNORECASE)
        if m:
            try:
                value = float(m.group(1))
                unit = m.group(2) if m.lastindex >= 2 and m.group(2) else ""
                analytes.append({
                    "analyte": name,
                    "value": value,
                    "unit": unit or "",
                    "confidence": 0.7,
                    "extraction_method": "regex_local",
                })
            except (ValueError, IndexError):
                pass

    return {
        "analytes": analytes,
        "extraction_method": "regex_local",
    }


# ═══════════════════════════════════════════════════════════
# API Endpoints
# ═══════════════════════════════════════════════════════════

@ocr_review_bp.route("/admin/api/v2/ocr/queue", methods=["GET"])
def ocr_review_queue():
    """รายการเอกสารที่ต้องตรวจสอบ OCR."""
    if not _check_admin_secret():
        return _unauthorized()

    docs = _doc_index._docs if _doc_index else []
    status_filter = request.args.get("status", "").strip()
    category_filter = request.args.get("category", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(50, max(5, int(request.args.get("per_page", 20))))

    # Filter to docs with OCR text
    queue = []
    for d in reversed(docs):
        ocr_st = d.get("ocr_status", "none")
        has_text = bool(d.get("extracted_text", "").strip())
        cat = d.get("category", "")

        if status_filter:
            if status_filter == "has_text" and not has_text:
                continue
            if status_filter == "no_text" and has_text:
                continue
            if status_filter == "failed" and ocr_st != "failed":
                continue

        if category_filter and cat != category_filter:
            continue

        queue.append({
            "doc_id": d.get("document_id", "")[:8],
            "category": cat,
            "upload_date": d.get("upload_date", ""),
            "ocr_status": ocr_st,
            "has_text": has_text,
            "text_length": len(d.get("extracted_text", "")),
            "bill_company": d.get("bill_company", ""),
            "mime_type": d.get("mime_type", ""),
            "review_status": d.get("review_status", ""),
        })

    total = len(queue)
    start = (page - 1) * per_page
    page_items = queue[start:start + per_page]

    return jsonify({
        "items": page_items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, -(-total // per_page)),
        "summary": {
            "total_docs": len(docs),
            "with_text": sum(1 for d in docs if d.get("extracted_text", "").strip()),
            "ocr_done": sum(1 for d in docs if d.get("ocr_status") in ("ocr", "native", "ocr_merged")),
            "ocr_failed": sum(1 for d in docs if d.get("ocr_status") == "failed"),
        },
    }), 200


@ocr_review_bp.route("/admin/api/v2/ocr/document/<doc_id_prefix>", methods=["GET"])
def ocr_document_detail(doc_id_prefix: str):
    """รายละเอียดเอกสาร + ข้อความ OCR + structured data."""
    if not _check_admin_secret():
        return _unauthorized()

    docs = _doc_index._docs if _doc_index else []

    # Find by prefix (8 chars)
    doc = None
    for d in docs:
        if d.get("document_id", "").startswith(doc_id_prefix):
            doc = d
            break

    if not doc:
        return jsonify({"error": "ไม่พบเอกสาร"}), 404

    extracted_text = doc.get("extracted_text", "")
    category = doc.get("category", "")

    # Auto-extract structured data
    structured = {}
    if extracted_text.strip():
        if category == "bills":
            structured = extract_bill_structured_data(extracted_text)
        elif category == "lab":
            structured = extract_lab_structured_data(extracted_text)

    # Truncate text for response (avoid huge payloads)
    display_text = extracted_text[:5000]
    if len(extracted_text) > 5000:
        display_text += f"\n... (ตัดทอน — ทั้งหมด {len(extracted_text)} ตัวอักษร)"

    return jsonify({
        "doc_id": doc.get("document_id", "")[:8],
        "category": category,
        "upload_date": doc.get("upload_date", ""),
        "ocr_status": doc.get("ocr_status", ""),
        "text_length": len(extracted_text),
        "extracted_text": display_text,
        "bill_company": doc.get("bill_company", ""),
        "mime_type": doc.get("mime_type", ""),
        "file_size": doc.get("file_size", 0),
        "structured_data": structured,
        "review_status": doc.get("review_status", ""),
    }), 200


@ocr_review_bp.route("/admin/api/v2/ocr/extract-bills", methods=["GET"])
def ocr_extract_bills():
    """ดึง structured data จากบิลทั้งหมดที่มี OCR text."""
    if not _check_admin_secret():
        return _unauthorized()

    docs = _doc_index._docs if _doc_index else []
    results = []

    for d in docs:
        if d.get("category") != "bills":
            continue
        text = d.get("extracted_text", "").strip()
        if not text:
            continue

        structured = extract_bill_structured_data(text)
        results.append({
            "doc_id": d.get("document_id", "")[:8],
            "upload_date": d.get("upload_date", ""),
            "bill_company": d.get("bill_company", "OTHER"),
            "total": structured.get("total"),
            "vat": structured.get("vat"),
            "dates": structured.get("dates", []),
            "invoice_no": structured.get("invoice_no"),
            "item_count": len(structured.get("items", [])),
            "amounts": structured.get("amounts", [])[:3],
        })

    # Sort by date desc
    results.sort(key=lambda x: x.get("upload_date", ""), reverse=True)

    return jsonify({
        "bills": results[:100],
        "total": len(results),
        "summary": {
            "total_amount": sum(r["total"] for r in results if r["total"]),
            "with_amount": sum(1 for r in results if r["total"]),
            "without_amount": sum(1 for r in results if not r["total"]),
        },
    }), 200


# ═══════════════════════════════════════════════════════════
# OCR Review UI HTML
# ═══════════════════════════════════════════════════════════

OCR_REVIEW_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OCR Review — RANONG RAKTAI AI</title>
<style>
:root {
  --bg:#0f172a; --bg2:#1e293b; --bg3:#334155;
  --border:#475569; --text:#e2e8f0; --muted:#94a3b8;
  --accent:#38bdf8; --green:#22c55e; --red:#ef4444;
  --yellow:#eab308; --purple:#a78bfa; --orange:#fb923c;
  --radius:12px;
}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;}

.header{background:var(--bg2);border-bottom:1px solid var(--bg3);padding:16px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;}
.header h1{font-size:1.2rem;color:var(--accent);}
.header a{color:var(--muted);font-size:.85rem;text-decoration:none;}
.header a:hover{color:var(--accent);}

.layout{display:grid;grid-template-columns:380px 1fr;gap:0;height:calc(100vh - 60px);overflow:hidden;}

/* Left panel: queue */
.queue-panel{background:var(--bg2);border-right:1px solid var(--bg3);display:flex;flex-direction:column;overflow:hidden;}
.queue-header{padding:14px 16px;border-bottom:1px solid var(--bg3);}
.queue-header h2{font-size:.9rem;color:var(--accent);margin-bottom:10px;}
.queue-filters{display:flex;gap:6px;flex-wrap:wrap;}
.queue-filters select,.queue-filters input{padding:6px 10px;border-radius:8px;border:1px solid var(--bg3);background:var(--bg);color:var(--text);font-size:.8rem;}
.queue-filters select:focus,.queue-filters input:focus{outline:none;border-color:var(--accent);}
.queue-list{flex:1;overflow-y:auto;padding:8px;}
.queue-item{padding:10px 12px;border-radius:8px;border:1px solid transparent;cursor:pointer;margin-bottom:4px;transition:background .15s;}
.queue-item:hover{background:rgba(56,189,248,.05);border-color:var(--bg3);}
.queue-item.active{background:rgba(56,189,248,.1);border-color:var(--accent);}
.queue-item .qi-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;}
.queue-item .qi-id{font-family:monospace;color:var(--accent);font-size:.85rem;}
.queue-item .qi-date{font-size:.7rem;color:var(--muted);}
.queue-item .qi-meta{display:flex;gap:6px;flex-wrap:wrap;}
.badge{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:.65rem;font-weight:600;}
.badge-lab{background:#164e63;color:#22d3ee;}
.badge-rx{background:#4a1d96;color:#c4b5fd;}
.badge-bills{background:#713f12;color:#fde047;}
.badge-inbox{background:#1e3a5f;color:#93c5fd;}
.badge-dialysis{background:#14532d;color:#86efac;}
.badge-review{background:#3f3f46;color:#d4d4d8;}
.badge-ok{background:#14532d;color:#86efac;}
.badge-fail{background:#450a0a;color:#fca5a5;}
.badge-warn{background:#713f12;color:#fde047;}

.queue-pagination{display:flex;align-items:center;justify-content:center;gap:8px;padding:10px;border-top:1px solid var(--bg3);}
.queue-pagination button{padding:5px 12px;border-radius:6px;border:1px solid var(--bg3);background:var(--bg);color:var(--text);cursor:pointer;font-size:.8rem;}
.queue-pagination button:disabled{opacity:.3;cursor:default;}

/* Right panel: detail */
.detail-panel{overflow-y:auto;padding:24px;}
.detail-empty{text-align:center;padding:80px 20px;color:var(--muted);}
.detail-empty .icon{font-size:3rem;margin-bottom:16px;}

.detail-header{margin-bottom:20px;}
.detail-header h2{font-size:1.1rem;color:var(--accent);margin-bottom:8px;}
.detail-meta{display:flex;gap:12px;flex-wrap:wrap;font-size:.8rem;color:var(--muted);}

.section{background:var(--bg2);border:1px solid var(--bg3);border-radius:var(--radius);padding:16px;margin-bottom:16px;}
.section h3{font-size:.9rem;color:var(--accent);margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--bg3);}

.ocr-text{white-space:pre-wrap;font-family:'Courier New',monospace;font-size:.8rem;color:var(--text);background:var(--bg);border:1px solid var(--bg3);border-radius:8px;padding:12px;max-height:400px;overflow-y:auto;line-height:1.6;}

/* Structured data */
.data-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;}
.data-card{background:var(--bg);border:1px solid var(--bg3);border-radius:10px;padding:12px;text-align:center;}
.data-card .label{font-size:.7rem;color:var(--muted);text-transform:uppercase;margin-bottom:4px;}
.data-card .value{font-size:1.2rem;font-weight:700;color:var(--accent);}
.data-card .value.money{color:var(--green);}

.items-table{width:100%;border-collapse:collapse;font-size:.8rem;}
.items-table th{text-align:left;color:var(--muted);padding:6px 8px;border-bottom:1px solid var(--bg3);font-size:.7rem;text-transform:uppercase;}
.items-table td{padding:6px 8px;border-bottom:1px solid rgba(71,85,105,.3);}

.analyte-list{display:flex;flex-direction:column;gap:6px;}
.analyte-row{display:flex;align-items:center;gap:12px;padding:8px 12px;background:var(--bg);border-radius:8px;}
.analyte-name{width:120px;font-weight:600;font-size:.85rem;}
.analyte-value{font-family:monospace;font-size:.9rem;color:var(--accent);font-weight:700;}
.analyte-unit{font-size:.8rem;color:var(--muted);}

.summary-bar{display:flex;gap:16px;margin-bottom:16px;flex-wrap:wrap;}
.summary-stat{display:flex;flex-direction:column;align-items:center;padding:12px 20px;background:var(--bg2);border:1px solid var(--bg3);border-radius:10px;}
.summary-stat .s-value{font-size:1.5rem;font-weight:700;}
.summary-stat .s-label{font-size:.7rem;color:var(--muted);margin-top:2px;}

/* Loading */
.loading{text-align:center;padding:30px;color:var(--muted);}
.spinner{display:inline-block;width:20px;height:20px;border:3px solid var(--bg3);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}

@media(max-width:768px){
  .layout{grid-template-columns:1fr;height:auto;}
  .queue-panel{max-height:40vh;}
  .detail-panel{padding:16px;}
}
</style>
</head>
<body>

<div class="header">
  <h1>🔍 OCR Review — RANONG RAKTAI AI</h1>
  <a href="/admin/v2/dashboard?secret=" id="back-link">← กลับ Dashboard</a>
</div>

<div class="layout">
  <!-- Queue panel -->
  <div class="queue-panel">
    <div class="queue-header">
      <h2>📋 คิวเอกสาร</h2>
      <div class="queue-filters">
        <select id="q-category">
          <option value="">ทุกหมวด</option>
          <option value="bills">ใบเสร็จ</option>
          <option value="lab">ผลแล็บ</option>
          <option value="rx">ใบสั่งยา</option>
          <option value="dialysis">ฟอกไต</option>
          <option value="inbox">กล่องข้อความ</option>
        </select>
        <select id="q-status">
          <option value="">ทุกสถานะ</option>
          <option value="has_text">มีข้อความ</option>
          <option value="no_text">ไม่มีข้อความ</option>
          <option value="failed">OCR ล้มเหลว</option>
        </select>
      </div>
      <div class="summary-bar" id="queue-summary" style="margin-top:10px"></div>
    </div>
    <div class="queue-list" id="queue-list">
      <div class="loading"><div class="spinner"></div></div>
    </div>
    <div class="queue-pagination" id="queue-pagination"></div>
  </div>

  <!-- Detail panel -->
  <div class="detail-panel" id="detail-panel">
    <div class="detail-empty">
      <div class="icon">📄</div>
      <p>เลือกเอกสารจากคิวด้านซ้าย<br>เพื่อดูข้อความ OCR และข้อมูลที่สกัดได้</p>
    </div>
  </div>
</div>

<script>
const SECRET = new URLSearchParams(location.search).get('secret') || '';
const API = '/admin/api/v2/ocr';
document.getElementById('back-link').href = '/admin/v2/dashboard?secret=' + encodeURIComponent(SECRET);

const CAT_NAMES = {lab:'ผลแล็บ',rx:'ใบสั่งยา',bills:'ใบเสร็จ',inbox:'กล่องข้อความ',dialysis:'ฟอกไต',review:'รอตรวจสอบ'};
const CAT_BADGE = {lab:'badge-lab',rx:'badge-rx',bills:'badge-bills',inbox:'badge-inbox',dialysis:'badge-dialysis',review:'badge-review'};

let queuePage = 1;
let activeDocId = null;

async function apiFetch(path) {
  const sep = path.includes('?') ? '&' : '?';
  const r = await fetch(API + path + sep + 'secret=' + encodeURIComponent(SECRET));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

function badge(cat) {
  return '<span class="badge ' + (CAT_BADGE[cat] || 'badge-review') + '">' + (CAT_NAMES[cat] || cat) + '</span>';
}

function ocrBadge(status) {
  if (status === 'failed') return '<span class="badge badge-fail">OCR ล้มเหลว</span>';
  if (status === 'none') return '<span class="badge badge-warn">ไม่มี OCR</span>';
  if (status && status !== 'none') return '<span class="badge badge-ok">' + status + '</span>';
  return '';
}

function formatMoney(val) {
  if (val == null) return '—';
  return val.toLocaleString('th-TH', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

// ── Load queue ──
async function loadQueue() {
  try {
    const cat = document.getElementById('q-category').value;
    const status = document.getElementById('q-status').value;
    let url = '/queue?page=' + queuePage + '&per_page=30';
    if (cat) url += '&category=' + cat;
    if (status) url += '&status=' + status;

    const data = await apiFetch(url);
    const items = data.items || [];
    const summary = data.summary || {};

    // Summary bar
    document.getElementById('queue-summary').innerHTML =
      '<div class="summary-stat"><div class="s-value">' + summary.total_docs + '</div><div class="s-label">ทั้งหมด</div></div>'
      + '<div class="summary-stat"><div class="s-value" style="color:var(--green)">' + summary.with_text + '</div><div class="s-label">มีข้อความ</div></div>'
      + '<div class="summary-stat"><div class="s-value" style="color:var(--accent)">' + summary.ocr_done + '</div><div class="s-label">OCR สำเร็จ</div></div>'
      + '<div class="summary-stat"><div class="s-value" style="color:var(--red)">' + summary.ocr_failed + '</div><div class="s-label">ล้มเหลว</div></div>';

    // Queue list
    if (!items.length) {
      document.getElementById('queue-list').innerHTML = '<div style="text-align:center;padding:30px;color:var(--muted)">ไม่พบเอกสาร</div>';
      document.getElementById('queue-pagination').innerHTML = '';
      return;
    }

    document.getElementById('queue-list').innerHTML = items.map(item => {
      const isActive = activeDocId === item.doc_id;
      return '<div class="queue-item' + (isActive ? ' active' : '') + '" onclick="loadDetail(\'' + item.doc_id + '\')">'
        + '<div class="qi-header"><span class="qi-id">' + item.doc_id + '</span><span class="qi-date">' + (item.upload_date || '') + '</span></div>'
        + '<div class="qi-meta">'
        + badge(item.category)
        + ocrBadge(item.ocr_status)
        + (item.text_length > 0 ? '<span class="badge badge-ok">' + item.text_length + ' ตัวอักษร</span>' : '')
        + (item.bill_company && item.bill_company !== 'OTHER' ? '<span style="color:var(--orange);font-size:.7rem">🏢 ' + item.bill_company + '</span>' : '')
        + '</div></div>';
    }).join('');

    // Pagination
    document.getElementById('queue-pagination').innerHTML =
      '<button ' + (data.page <= 1 ? 'disabled' : '') + ' onclick="queuePage--;loadQueue()">← ก่อนหน้า</button>'
      + '<span style="font-size:.8rem;color:var(--muted)">' + data.page + '/' + data.total_pages + '</span>'
      + '<button ' + (data.page >= data.total_pages ? 'disabled' : '') + ' onclick="queuePage++;loadQueue()">ถัดไป →</button>';
  } catch (e) {
    document.getElementById('queue-list').innerHTML = '<div style="color:var(--red);padding:20px">โหลดไม่สำเร็จ: ' + e.message + '</div>';
  }
}

// ── Load detail ──
async function loadDetail(docId) {
  activeDocId = docId;
  loadQueue(); // Refresh highlight

  const panel = document.getElementById('detail-panel');
  panel.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

  try {
    const data = await apiFetch('/document/' + docId);

    let html = '<div class="detail-header">';
    html += '<h2>📄 เอกสาร ' + data.doc_id + '</h2>';
    html += '<div class="detail-meta">'
      + badge(data.category)
      + '<span>📅 ' + (data.upload_date || '—') + '</span>'
      + '<span>📊 ' + data.ocr_status + '</span>'
      + '<span>📏 ' + (data.file_size / 1024).toFixed(1) + ' KB</span>'
      + (data.bill_company ? '<span style="color:var(--orange)">🏢 ' + data.bill_company + '</span>' : '')
      + '</div></div>';

    // OCR Text
    html += '<div class="section"><h3>📝 ข้อความ OCR (' + data.text_length + ' ตัวอักษร)</h3>';
    if (data.extracted_text && data.extracted_text.trim()) {
      html += '<div class="ocr-text">' + escapeHtml(data.extracted_text) + '</div>';
    } else {
      html += '<div style="text-align:center;padding:20px;color:var(--muted)">ไม่มีข้อความ OCR</div>';
    }
    html += '</div>';

    // Structured data
    const sd = data.structured_data || {};
    if (data.category === 'bills' && Object.keys(sd).length > 0) {
      html += renderBillStructured(sd);
    } else if (data.category === 'lab' && sd.analytes && sd.analytes.length > 0) {
      html += renderLabStructured(sd);
    }

    panel.innerHTML = html;
  } catch (e) {
    panel.innerHTML = '<div style="color:var(--red);padding:40px;text-align:center">โหลดไม่สำเร็จ: ' + e.message + '</div>';
  }
}

function renderBillStructured(sd) {
  let html = '<div class="section"><h3>💰 ข้อมูลบิลที่สกัดได้</h3>';

  // Data cards
  html += '<div class="data-grid">';
  html += '<div class="data-card"><div class="label">ยอดรวม</div><div class="value money">' + formatMoney(sd.total) + '</div></div>';
  html += '<div class="data-card"><div class="label">VAT</div><div class="value">' + formatMoney(sd.vat) + '</div></div>';
  html += '<div class="data-card"><div class="label">เลขใบเสร็จ</div><div class="value" style="font-size:.9rem">' + (sd.invoice_no || '—') + '</div></div>';
  html += '<div class="data-card"><div class="label">เลขผู้เสียภาษี</div><div class="value" style="font-size:.8rem">' + (sd.tax_id || '—') + '</div></div>';
  html += '</div>';

  // Dates
  if (sd.dates && sd.dates.length) {
    html += '<div style="margin-top:12px;font-size:.85rem;color:var(--muted)">📅 วันที่พบ: ' + sd.dates.join(', ') + '</div>';
  }

  // Amounts
  if (sd.amounts && sd.amounts.length > 1) {
    html += '<div style="margin-top:8px;font-size:.85rem;color:var(--muted)">💵 ยอดเงินทั้งหมด: '
      + sd.amounts.map(a => formatMoney(a) + ' ฿').join(', ') + '</div>';
  }

  // Line items
  if (sd.items && sd.items.length) {
    html += '<div style="margin-top:12px"><table class="items-table"><thead><tr><th>ลำดับ</th><th>รายการ</th><th>ยอด</th></tr></thead><tbody>';
    sd.items.forEach((item, i) => {
      html += '<tr><td>' + (item.line || (i + 1)) + '</td><td>' + escapeHtml(item.description) + '</td><td style="text-align:right">' + item.amount + '</td></tr>';
    });
    html += '</tbody></table></div>';
  }

  html += '<div style="margin-top:10px;font-size:.7rem;color:var(--muted)">⚙ วิธีการ: ' + (sd.extraction_method || 'regex_local') + ' — ไม่ใช้ External AI</div>';
  html += '</div>';
  return html;
}

function renderLabStructured(sd) {
  let html = '<div class="section"><h3>🔬 ค่าแล็บที่สกัดได้</h3>';
  html += '<div class="analyte-list">';
  (sd.analytes || []).forEach(a => {
    html += '<div class="analyte-row">'
      + '<span class="analyte-name">' + a.analyte + '</span>'
      + '<span class="analyte-value">' + a.value + '</span>'
      + '<span class="analyte-unit">' + (a.unit || '') + '</span>'
      + '</div>';
  });
  html += '</div>';
  html += '<div style="margin-top:10px;font-size:.7rem;color:var(--muted)">⚙ วิธีการ: ' + (sd.extraction_method || 'regex_local') + ' — ไม่ใช้ External AI</div>';
  html += '</div>';
  return html;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// Filters
document.getElementById('q-category').addEventListener('change', () => { queuePage = 1; loadQueue(); });
document.getElementById('q-status').addEventListener('change', () => { queuePage = 1; loadQueue(); });

// Initial load
loadQueue();
</script>
</body>
</html>"""


@ocr_review_bp.route("/admin/v2/ocr-review", methods=["GET"])
def ocr_review_page():
    """OCR Review UI page."""
    admin_secret = os.getenv("ADMIN_SECRET", "").strip()
    auth_param = request.args.get("secret", "")
    auth_header = request.headers.get("X-Admin-Secret", "")
    if not admin_secret or (auth_header != admin_secret and auth_param != admin_secret):
        return (
            "<html><body style='font-family:sans-serif;text-align:center;"
            "padding:60px;background:#0f172a;color:#e2e8f0'>"
            "<h2>🔒 ต้องการรหัสผู้ดูแล</h2>"
            "<p style='color:#94a3b8'>เข้าถึง: /admin/v2/ocr-review?secret=YOUR_ADMIN_SECRET</p>"
            "</body></html>"
        ), 401
    return OCR_REVIEW_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


def register_ocr_review(app, storage_ref, doc_index_ref, audit_ref):
    """Register OCR review blueprint."""
    global _storage, _doc_index, _audit_logger
    _storage = storage_ref
    _doc_index = doc_index_ref
    _audit_logger = audit_ref
    app.register_blueprint(ocr_review_bp)
    logger.info("OCR Review: registered on /admin/v2/ocr-review")
