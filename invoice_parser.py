"""Invoice/Bill Parser — ดึงข้อมูลจากบิล/ใบเสร็จของคลินิกไต

ดึงข้อมูล:
- invoice_number: เลขที่ใบเสร็จ/ใบแจ้งหนี้
- invoice_date: วันที่ในเอกสาร
- vendor_name: ชื่อบริษัท
- tax_id: เลขประจำตัวผู้เสียภาษี (13 หลัก)
- subtotal: ยอดก่อน VAT
- vat_amount: VAT 7%
- total_amount: ยอดรวมทั้งสิ้น
- items: รายการสินค้า [{name, qty, unit_price, amount}]
- payment_status: ชำระแล้ว/ยังไม่ชำระ

รองรับ:
- บิลภาษาไทยและอังกฤษ
- วันที่ พ.ศ. (2569) → ค.ศ. (2026)
- ตัวเลขแบบ comma-separated (1,234,567.89)
- Tax ID 13 หลัก

ไม่ log ยอดเงินหรือรายละเอียดบิล (ป้องกันข้อมูลการเงินรั่ว)
"""

import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ─── Regex Patterns ──────────────────────────────────────────

# Tax ID: 13 digits (may have dashes/spaces)
_TAX_ID_RE = re.compile(
    r"(?:เลขประจำตัวผู้เสียภาษี|เลขที่ผู้เสียภาษี|Tax\s*(?:ID|Id)"
    r"|Taxpayer\s*ID|TIN|เลขผู้เสียภาษี)"
    r"[.:\s]*"
    r"(\d[\d\-\s]{10,15}\d)",
    re.IGNORECASE,
)

# Standalone 13-digit tax ID
_TAX_ID_STANDALONE_RE = re.compile(r"\b(\d{1}\s*\d{4}\s*\d{5}\s*\d{2}\s*\d{1})\b")

# Invoice number
_INVOICE_NO_RE = re.compile(
    r"(?:เลขที่(?:ใบ(?:เสร็จ|แจ้งหนี้|กำกับ(?:ภาษี)?))?|Invoice\s*(?:No|Number|#)"
    r"|Bill\s*No|Receipt\s*No|เอกสารเลขที่|Doc\s*No)"
    r"[.:\s]*"
    r"([A-Za-z0-9\-/().]+(?:\s*[A-Za-z0-9\-/().]+)?)",
    re.IGNORECASE,
)

# Total amount (Thai + English patterns)
_TOTAL_PATTERNS = [
    re.compile(
        r"(?:รวม(?:ทั้งสิ้น|เงินทั้งสิ้น|สุทธิ)|ยอดรวม(?:ทั้งสิ้น|สุทธิ)?"
        r"|Grand\s*Total|Net\s*(?:Total|Amount)|จำนวนเงินรวม)"
        r"[:\s]*"
        r"([0-9,]+\.?\d*)",
        re.IGNORECASE,
    ),
    # Fallback: "รวมเงิน" or "Total"
    re.compile(
        r"(?:รวมเงิน|Total\s*(?:Amount)?|ยอดชำระ|จำนวนเงิน)"
        r"[:\s]*"
        r"([0-9,]+\.?\d*)",
        re.IGNORECASE,
    ),
]

# VAT
_VAT_RE = re.compile(
    r"(?:ภาษีมูลค่าเพิ่ม|VAT|Vat|ภาษี\s*7\s*%|Tax\s*7\s*%)"
    r"[:\s]*"
    r"([0-9,]+\.?\d*)",
    re.IGNORECASE,
)

# Subtotal (before VAT)
_SUBTOTAL_RE = re.compile(
    r"(?:ก่อนภาษี|ยอดก่อน(?:ภาษี)?|Subtotal|ราคาสินค้า(?:รวม)?"
    r"|มูลค่า(?:สินค้า)?(?:รวม)?|Amount\s*before)"
    r"[:\s]*"
    r"([0-9,]+\.?\d*)",
    re.IGNORECASE,
)

# Date patterns (Thai and English)
_DATE_PATTERNS = [
    # DD/MM/YYYY or DD-MM-YYYY
    re.compile(
        r"(?:วันที่|Date|ลงวันที่)[:\s]*"
        r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})",
        re.IGNORECASE,
    ),
    # Thai written date: 30 ตุลาคม 2569
    re.compile(
        r"(\d{1,2})\s*"
        r"(มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน"
        r"|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม"
        r"|ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\."
        r"|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)"
        r"\s*(\d{2,4})"
    ),
    # Standalone DD/MM/YYYY in text
    re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})"),
]

# Thai month name → number
_THAI_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4,
    "พฤษภาคม": 5, "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8,
    "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
    "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4,
    "พ.ค.": 5, "มิ.ย.": 6, "ก.ค.": 7, "ส.ค.": 8,
    "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12,
}

# Payment status
_PAID_PATTERNS = re.compile(
    r"ชำระ(?:เงิน)?แล้ว|PAID|จ่ายแล้ว|ได้รับเงินแล้ว|ชำระครบ",
    re.IGNORECASE,
)
_UNPAID_PATTERNS = re.compile(
    r"ยังไม่(?:ได้)?ชำระ|UNPAID|ค้างชำระ|รอชำระ|ครบกำหนด",
    re.IGNORECASE,
)

# Line item pattern (qty x unit_price = amount)
_LINE_ITEM_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"          # qty
    r"(?:x|×|X|ชิ้น|กล่อง|ขวด|หลอด|ม้วน|ชุด|set|pcs?)?\s*"
    r"(?:@|x|×)?\s*"
    r"([0-9,]+\.?\d*)\s*"          # unit_price
    r"(?:=|รวม)?\s*"
    r"([0-9,]+\.?\d*)",            # amount
)


# ─── Helper Functions ────────────────────────────────────────

def _parse_amount(text: str) -> float:
    """Parse comma-separated number to float: '1,234,567.89' → 1234567.89"""
    if not text:
        return 0.0
    try:
        return float(text.replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _parse_thai_date(day: str, month, year: str) -> str:
    """Parse Thai date to ISO format YYYY-MM-DD

    Handles:
    - พ.ศ. (2569) → ค.ศ. (2026): subtract 543
    - Thai month names → month numbers
    """
    try:
        d = int(day)
        # Month: could be number or Thai name
        if isinstance(month, str) and not month.isdigit():
            m = _THAI_MONTHS.get(month.strip(), 0)
        else:
            m = int(month)
        y = int(year)

        # Handle 2-digit year
        if y < 100:
            y += 2500 if y > 40 else 2000  # 69 → 2569, 26 → 2026

        # พ.ศ. → ค.ศ.
        if y > 2400:
            y -= 543

        if 1 <= m <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100:
            return f"{y:04d}-{m:02d}-{d:02d}"
    except (ValueError, TypeError):
        pass
    return ""


def _clean_tax_id(raw: str) -> str:
    """Clean tax ID: remove dashes and spaces, keep digits only"""
    digits = re.sub(r"[^0-9]", "", raw or "")
    return digits if len(digits) == 13 else ""


# ─── Clinic Vendor Tax IDs ───────────────────────────────────
# Known tax IDs for the 9 clinic vendors (for matching)
_CLINIC_VENDOR_TAX_IDS: dict[str, str] = {
    # Populated from vendor_registry at runtime via match_vendor_tax_id()
    # These are placeholders — actual IDs come from the registry
}


# ─── Main Parse Function ─────────────────────────────────────

def parse_invoice(ocr_text: str, vendor_hint: str = "") -> dict:
    """Parse invoice/bill from OCR text

    Args:
        ocr_text: OCR-extracted text from the bill image/PDF
        vendor_hint: Optional vendor name hint from classification

    Returns:
        dict with: invoice_number, invoice_date, vendor_name, tax_id,
                   subtotal, vat_amount, total_amount, items,
                   payment_status, confidence, parse_method
    """
    if not ocr_text or len(ocr_text.strip()) < 20:
        return {}

    result: dict = {
        "invoice_number": "",
        "invoice_date": "",
        "vendor_name": vendor_hint or "",
        "tax_id": "",
        "subtotal": 0.0,
        "vat_amount": 0.0,
        "total_amount": 0.0,
        "items": [],
        "payment_status": "unknown",
        "confidence": 0.0,
        "parse_method": "ocr_regex",
    }

    text = ocr_text.strip()

    # ── Tax ID ──
    m = _TAX_ID_RE.search(text)
    if m:
        result["tax_id"] = _clean_tax_id(m.group(1))
    if not result["tax_id"]:
        m = _TAX_ID_STANDALONE_RE.search(text)
        if m:
            result["tax_id"] = _clean_tax_id(m.group(1))

    # ── Invoice Number ──
    m = _INVOICE_NO_RE.search(text)
    if m:
        result["invoice_number"] = m.group(1).strip()[:30]

    # ── Date ──
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            groups = m.groups()
            if len(groups) == 3:
                result["invoice_date"] = _parse_thai_date(
                    groups[0], groups[1], groups[2]
                )
                if result["invoice_date"]:
                    break

    # ── Total Amount ──
    for pat in _TOTAL_PATTERNS:
        m = pat.search(text)
        if m:
            result["total_amount"] = _parse_amount(m.group(1))
            if result["total_amount"] > 0:
                break

    # ── VAT ──
    m = _VAT_RE.search(text)
    if m:
        result["vat_amount"] = _parse_amount(m.group(1))

    # ── Subtotal ──
    m = _SUBTOTAL_RE.search(text)
    if m:
        result["subtotal"] = _parse_amount(m.group(1))

    # Compute subtotal from total - VAT if missing
    if (
        not result["subtotal"]
        and result["total_amount"] > 0
        and result["vat_amount"] > 0
    ):
        result["subtotal"] = round(
            result["total_amount"] - result["vat_amount"], 2
        )

    # Compute VAT from total * 7/107 if missing
    if (
        not result["vat_amount"]
        and result["total_amount"] > 0
        and not result["subtotal"]
    ):
        result["vat_amount"] = round(
            result["total_amount"] * 7 / 107, 2
        )
        result["subtotal"] = round(
            result["total_amount"] - result["vat_amount"], 2
        )

    # ── Payment Status ──
    if _PAID_PATTERNS.search(text):
        result["payment_status"] = "paid"
    elif _UNPAID_PATTERNS.search(text):
        result["payment_status"] = "unpaid"

    # ── Line Items ──
    result["items"] = extract_line_items(text)

    # ── Confidence ──
    score = 0.0
    if result["total_amount"] > 0:
        score += 0.3
    if result["invoice_number"]:
        score += 0.2
    if result["tax_id"]:
        score += 0.2
    if result["invoice_date"]:
        score += 0.15
    if result["vendor_name"]:
        score += 0.1
    if result["items"]:
        score += 0.05
    result["confidence"] = round(min(score, 1.0), 2)

    return result


def extract_line_items(ocr_text: str) -> list[dict]:
    """Extract individual line items from invoice text

    Looks for patterns like:
    - qty x unit_price = amount
    - item name followed by numbers
    """
    items: list[dict] = []
    if not ocr_text:
        return items

    lines = ocr_text.split("\n")
    for line in lines:
        line = line.strip()
        if not line or len(line) < 5:
            continue

        m = _LINE_ITEM_RE.search(line)
        if m:
            qty = _parse_amount(m.group(1))
            unit_price = _parse_amount(m.group(2))
            amount = _parse_amount(m.group(3))

            if amount > 0 and qty > 0:
                # Try to extract item name (text before the numbers)
                name_part = line[: m.start()].strip()
                # Clean up: remove leading numbers, bullets, dashes
                name_part = re.sub(r"^[\d.\-)\]]+\s*", "", name_part)
                name_part = name_part.strip("- •·")

                items.append({
                    "name": name_part[:80] if name_part else "รายการ",
                    "qty": qty,
                    "unit_price": unit_price,
                    "amount": amount,
                })

    return items[:50]  # Limit to 50 items


def match_vendor_tax_id(
    tax_id: str, vendor_registry=None
) -> str:
    """Match tax ID to vendor name using vendor registry

    Args:
        tax_id: 13-digit Thai tax ID
        vendor_registry: VendorRegistry instance

    Returns:
        Vendor name if matched, empty string otherwise
    """
    if not tax_id or len(tax_id) != 13 or not vendor_registry:
        return ""

    try:
        # Use vendor_registry's tax ID matching
        if hasattr(vendor_registry, "match_by_tax_id"):
            vendor, _matched_id = vendor_registry.match_by_tax_id(tax_id)
            if vendor:
                return getattr(vendor, "folder_name", "") or ""
        # Fallback: search all vendors
        if hasattr(vendor_registry, "list_all"):
            for v in vendor_registry.list_all():
                if getattr(v, "tax_id", "") == tax_id:
                    return getattr(v, "folder_name", "") or ""
    except Exception:
        pass
    return ""
