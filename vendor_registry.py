# =============================================================================
# VENDOR REGISTRY — ทะเบียนบริษัทคู่ค้า (สำหรับจัดแฟ้มบิล/ใบกำกับภาษี)
# คลินิกระนอง รักษ์ไต × RANONG RAKTAI AI
#
# ทำไมต้องมีโมดูลนี้:
#   เดิมรายชื่อบริษัทอยู่ใน BILL_COMPANY_REGISTRY ซึ่ง hardcode ไว้ใน
#   server.py การเพิ่มบริษัทใหม่จึงต้องแก้ source code แล้ว deploy ใหม่
#   ทุกครั้ง โมดูลนี้ย้ายทะเบียนไปอยู่บน GCS เพื่อให้ผู้ดูแลลงทะเบียน
#   บริษัทใหม่ได้เองโดยไม่ต้องแก้โค้ด
#
# GCS path: _vendors/registry.json (+ versioned snapshot)
#
# หลักความปลอดภัยที่โมดูลนี้ยึด:
#   1. ชื่อแฟ้มบนดิสก์ต้องผ่าน allowlist [A-Z0-9_] เท่านั้น
#      ไม่เคยนำข้อความจาก OCR หรือ AI ไปเป็น path โดยตรง
#   2. การจับคู่แบบ fuzzy "ไม่เคย" จัดเก็บอัตโนมัติ — เสนอให้คนยืนยันเท่านั้น
#      เพราะชื่อบริษัทที่คล้ายกันอาจเป็นคนละบริษัท
#   3. ชื่อคลินิกเอง (ผู้ซื้อ) อยู่ในรายการกันไว้ ไม่ถูกจับเป็นผู้ขาย
#   4. ไม่บันทึกเนื้อหาเอกสารลง log — log เฉพาะชื่อบริษัทที่จับได้
# =============================================================================

import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# ชื่อแฟ้มปลอดภัย — ใช้ allowlist ไม่ใช่ blocklist
# ─────────────────────────────────────────────────────────────

# ชื่อแฟ้มอนุญาต: A-Z, 0-9, ขีดล่าง และอักษรไทย
#
# การใช้ allowlist ทำให้ path traversal ("..", "/", "\\") และอักขระที่
# Windows ห้าม (< > : " | ? *) เป็นไปไม่ได้โดยโครงสร้าง ไม่ต้องไล่ลบทีละตัว
#
# เหตุที่อนุญาตอักษรไทย: บริษัทคู่ค้าหลายรายมีชื่อภาษาไทยล้วน ถ้าบังคับ
# ASCII ผู้ดูแลจะต้องตั้งชื่อแฟ้มเองทุกครั้งหรือได้แฟ้มเป็นรหัสที่อ่าน
# ไม่รู้ความ ระบบนี้สร้างแฟ้มผู้ป่วยเป็นภาษาไทยอยู่แล้ว (เช่น
# '57042_สมชาย') และ Windows รองรับ Unicode ในชื่อโฟลเดอร์เต็มรูปแบบ
_CANONICAL_RE = re.compile(r"^[A-Z0-9_฀-๿]{2,40}$")

# ชื่อที่ Windows สงวนไว้ — สร้างเป็นชื่อแฟ้มไม่ได้
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

# แฟ้มพักเอกสารที่ยังระบุบริษัทไม่ได้ — ไม่ใช่ชื่อบริษัท จึงกันไว้ไม่ให้จดทะเบียน
UNKNOWN_VENDOR_FOLDER = "_UNIDENTIFIED"
REVIEW_VENDOR_FOLDER = "_PENDING_REVIEW"
_RESERVED_FOLDERS = frozenset({
    UNKNOWN_VENDOR_FOLDER, REVIEW_VENDOR_FOLDER, "OTHER",
})


class VendorRegistryError(ValueError):
    """ข้อมูลที่ส่งมาลงทะเบียนไม่ผ่านการตรวจสอบ"""


def _is_thai(ch: str) -> bool:
    return "฀" <= ch <= "๿"


def _fold_latin_accents(text: str) -> str:
    """พับเครื่องหมายของอักษรละติน (é → e) โดยไม่แตะอักษรไทย

    ต้องทำทีละอักขระ ไม่ใช่ normalize ทั้งสตริง เพราะการ decompose
    ทั้งก้อนจะแยก "ำ" ออกเป็น U+0E4D + U+0E32 และประกอบกลับไม่ได้
    (เป็น compatibility decomposition ไม่ใช่ canonical) ชื่อแฟ้มภาษาไทย
    จะเพี้ยนอย่างถาวร
    """
    out: list[str] = []
    for ch in text:
        if _is_thai(ch):
            out.append(ch)
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        out.append("".join(
            c for c in decomposed if not unicodedata.combining(c)
        ))
    return "".join(out)


def is_valid_canonical_name(name: str) -> bool:
    """ตรวจว่าชื่อนี้ใช้เป็นชื่อแฟ้มได้จริงหรือไม่"""
    if not name or not _CANONICAL_RE.match(name):
        return False
    if name in _WINDOWS_RESERVED:
        return False
    # ขีดล่างนำหน้าเป็นของแฟ้มระบบ (_UNIDENTIFIED ฯลฯ)
    if name.startswith("_"):
        return False
    return True


def to_canonical_name(raw: str) -> str:
    """เสนอชื่อแฟ้มจากชื่อบริษัท — ผลลัพธ์ต้องผ่าน is_valid_canonical_name

    ใช้เป็น "ข้อเสนอ" ให้ผู้ดูแลตรวจก่อนบันทึกเท่านั้น
    ไม่ใช้ผลของฟังก์ชันนี้ไปเขียนไฟล์โดยตรงหากยังไม่มีคนยืนยัน

    รองรับชื่อภาษาไทย — จะตัดคำบอกรูปแบบนิติบุคคล (บริษัท/จำกัด) ออก
    เพื่อให้ได้ชื่อแฟ้มที่สั้นและอ่านรู้ความ

    คืนค่าว่างถ้าแปลงแล้วไม่เหลืออักขระที่ใช้เป็นชื่อแฟ้มได้
    """
    if not raw:
        return ""
    text = _nfkc(raw)
    # ตัดคำบอกรูปแบบนิติบุคคลออกจากชื่อแฟ้ม (ยังเก็บชื่อเต็มใน display_name)
    # คำที่จะตัดต้องผ่าน _nfkc เหมือนกับข้อความ ไม่งั้น "จำกัด" ไม่ match
    for token in _THAI_LEGAL_STRIP:
        text = text.replace(token, " ")
    text = re.sub(
        r"\b(?:CO\.?,?\s*LTD\.?|COMPANY|LIMITED|PUBLIC|LTD\.?|INC\.?|CORP\.?)\b",
        " ", text, flags=re.IGNORECASE,
    )
    folded = _fold_latin_accents(text)
    upper = folded.upper()
    # อักขระที่ไม่อยู่ใน allowlist → ขีดล่าง แล้วยุบขีดล่างซ้ำ
    candidate = re.sub(r"[^A-Z0-9฀-๿]+", "_", upper).strip("_")
    candidate = re.sub(r"_{2,}", "_", candidate)[:40].strip("_")
    if len(candidate) < 2:
        return ""
    if candidate in _WINDOWS_RESERVED or candidate in _RESERVED_FOLDERS:
        return ""
    return candidate


# ─────────────────────────────────────────────────────────────
# การทำให้ชื่อบริษัทเป็นรูปแบบเทียบกันได้ (ไทย/อังกฤษ)
# ─────────────────────────────────────────────────────────────

def _nfkc(text: str) -> str:
    """NFKC normalize — ใช้ตัวเดียวกันทั้งกับข้อความและกับคำที่จะตัดออก

    สำคัญ: NFKC แยก "ำ" (U+0E33 SARA AM) ออกเป็น U+0E4D + U+0E32
    ถ้า normalize แต่ข้อความแล้วเทียบกับคำที่เขียนด้วย U+0E33 ตรงๆ
    คำนั้นจะไม่เคย match เลย — "จำกัด" เป็นกรณีที่พบจริง ทำให้คำบอก
    รูปแบบนิติบุคคลภาษาไทยไม่ถูกตัดออก และชื่อบริษัทไทยเทียบกันไม่ตรง
    """
    return unicodedata.normalize("NFKC", text or "")


# คำบอกรูปแบบนิติบุคคล — ไม่ช่วยแยกบริษัท จึงตัดออกก่อนเทียบ
# เรียงจากยาวไปสั้น เพื่อให้ตัดวลียาวก่อน
_LEGAL_TOKENS_RAW = [
    "บริษัทจำกัดมหาชน", "ห้างหุ้นส่วนจำกัด", "ห้างหุ้นส่วนสามัญ",
    "บริษัทจำกัด", "บริษัท", "บจก", "บมจ", "หจก", "จำกัดมหาชน", "จำกัด",
    "publiccompanylimited", "companylimited", "corporation", "incorporated",
    "limited", "company", "public", "intertrade", "trading",
    "coltd", "ltd", "inc", "corp", "plc", "llc", "co",
]

# ประเทศ/สาขาในชื่อบริษัท — "NIKKISO MEDICAL (THAILAND)" ต้องเทียบเท่า "NIKKISO"
_REGION_TOKENS_RAW = ["thailand", "ไทยแลนด์", "ประเทศไทย", "asia", "seasia"]

# คำที่จะตัดต้องผ่าน normalizer เดียวกับข้อความ ไม่งั้นไม่ match (ดู _nfkc)
# ตัดอักขระที่ไม่ใช่ตัวอักษร/เลขออกให้ตรงกับที่ทำกับข้อความด้วย
_LEGAL_TOKENS = sorted(
    {
        re.sub(r"[^0-9a-z฀-๿]+", "", _nfkc(t).lower())
        for t in _LEGAL_TOKENS_RAW
    } - {""},
    key=len, reverse=True,
)
_REGION_TOKENS = [
    re.sub(r"[^0-9a-z฀-๿]+", "", _nfkc(t).lower())
    for t in _REGION_TOKENS_RAW
]

# คำบอกรูปแบบนิติบุคคลภาษาไทย สำหรับตัดออกจาก "ชื่อแฟ้ม" (คงช่องว่างไว้)
# ผ่าน _nfkc เหมือนข้อความ เรียงยาวไปสั้นเพื่อให้ตัดวลียาวก่อน
_THAI_LEGAL_STRIP = sorted(
    {
        _nfkc(t) for t in (
            "ห้างหุ้นส่วนจำกัด", "ห้างหุ้นส่วนสามัญ", "บริษัท",
            "จำกัด", "(มหาชน)", "มหาชน",
            "บจก.", "บจก", "บมจ.", "บมจ", "หจก.", "หจก",
        )
    },
    key=len, reverse=True,
)


def normalize_company_name(raw: str) -> str:
    """ทำชื่อบริษัทให้เป็นรูปแบบเทียบกันได้

    ขั้นตอน: NFKC → lowercase → ตัดวรรณยุกต์ซ้ำ/ช่องว่าง/เครื่องหมาย
             → ตัดคำบอกรูปแบบนิติบุคคล → ตัดชื่อประเทศ

    ตัวอย่าง:
      "NIKKISO MEDICAL (THAILAND) CO., LTD." → "nikkisomedical"
      "บริษัท นิคคิโซ เมดิคัล (ประเทศไทย) จำกัด" → "นิคคิโซเมดิคัล"
    """
    if not raw:
        return ""
    text = _nfkc(raw).lower()
    # ตัดทุกอย่างที่ไม่ใช่ตัวอักษร/ตัวเลข (ช่องว่าง จุด จุลภาค วงเล็บ ขีด)
    text = re.sub(r"[^0-9a-z฀-๿]+", "", text)
    for token in _LEGAL_TOKENS:
        text = text.replace(token, "")
    for token in _REGION_TOKENS:
        text = text.replace(token, "")
    return text


_TAX_ID_RE = re.compile(r"^[0-9]{13}$")


def is_valid_tax_id(tax_id: str) -> bool:
    """เลขประจำตัวผู้เสียภาษีไทยมี 13 หลักพอดี

    ยอมรับเฉพาะ 13 หลักเต็ม — ไม่ยอมรับเลขบางส่วน เพราะการเทียบ
    เลขที่ไม่ครบอาจชนกับบริษัทอื่นที่มีเลขขึ้นต้นเหมือนกัน
    """
    return bool(tax_id and _TAX_ID_RE.match(tax_id.strip()))


def extract_tax_ids(text: str) -> list[str]:
    """ดึงเลขผู้เสียภาษี 13 หลักจากข้อความ OCR

    OCR มักแทรกช่องว่างหรือขีดระหว่างกลุ่มเลข จึงรวมเลขที่ติดกัน
    แล้วค่อยมองหาช่วง 13 หลัก
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    # จับกลุ่มเลขที่อาจคั่นด้วยช่องว่าง/ขีด แล้วถอดตัวคั่นออก
    for chunk in re.findall(r"[0-9][0-9\s\-]{11,25}[0-9]", text):
        digits = re.sub(r"[^0-9]", "", chunk)
        # เลื่อนหน้าต่าง 13 หลัก เพราะกลุ่มที่จับได้อาจยาวกว่า
        for i in range(0, len(digits) - 12):
            candidate = digits[i:i + 13]
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
    return found


# ─────────────────────────────────────────────────────────────
# ผู้ซื้อ vs ผู้ขาย
# ─────────────────────────────────────────────────────────────

# ชื่อของคลินิกเองและโรงพยาบาลที่เกี่ยวข้อง — ปรากฏในบิลในฐานะ "ผู้ซื้อ"
# จึงต้องไม่ถูกจับเป็นบริษัทคู่ค้า
BUYER_SELF_NAMES: tuple[str, ...] = (
    "คลินิกระนองรักษ์ไต", "ระนองรักษ์ไต", "รักษ์ไต",
    "ranongraktai", "raktai",
    "โรงพยาบาลระนอง", "ranonghospital",
)

# คำที่บอกว่าข้อความถัดจากนี้คือฝั่งผู้ซื้อ ไม่ใช่ผู้ขาย
BUYER_SECTION_MARKERS: tuple[str, ...] = (
    "ลูกค้า", "ผู้ซื้อ", "ชื่อผู้ซื้อ", "นามผู้ซื้อ", "ผู้รับสินค้า",
    "สถานที่ส่งของ", "ส่งของที่", "ที่อยู่จัดส่ง",
    "customer", "bill to", "billto", "ship to", "shipto",
    "sold to", "soldto", "deliver to", "buyer",
)


def _is_buyer_self(normalized: str) -> bool:
    """ชื่อนี้คือคลินิกเอง (ผู้ซื้อ) หรือไม่"""
    if not normalized:
        return False
    return any(
        normalize_company_name(n) and normalize_company_name(n) in normalized
        for n in BUYER_SELF_NAMES
    )


def split_supplier_section(text: str) -> tuple[str, str]:
    """แบ่งข้อความเป็น (ส่วนผู้ขาย, ส่วนผู้ซื้อ)

    ในใบกำกับภาษีไทย ชื่อผู้ขายอยู่หัวเอกสาร ส่วนผู้ซื้ออยู่ใต้คำว่า
    "ลูกค้า"/"ผู้ซื้อ"/"Bill To" การแยกช่วยให้ไม่จับชื่อผู้ซื้อ
    มาเป็นบริษัทคู่ค้า

    ถ้าไม่พบคำบอกฝั่งผู้ซื้อ จะคืนข้อความทั้งก้อนเป็นส่วนผู้ขาย
    """
    if not text:
        return "", ""
    lowered = text.lower()
    cut = -1
    for marker in BUYER_SECTION_MARKERS:
        idx = lowered.find(marker)
        if idx >= 0 and (cut < 0 or idx < cut):
            cut = idx
    if cut < 0:
        return text, ""
    return text[:cut], text[cut:]


# ─────────────────────────────────────────────────────────────
# Data Model
# ─────────────────────────────────────────────────────────────

class VendorRecord:
    """บริษัทคู่ค้าหนึ่งราย

    canonical_name คือชื่อแฟ้มบนดิสก์ (ASCII, [A-Z0-9_])
    display_name เก็บชื่อเต็มตามหนังสือรับรอง (มีภาษาไทยได้) — ไม่ใช้เป็น path
    """

    def __init__(
        self,
        vendor_id: str = "",
        canonical_name: str = "",
        display_name: str = "",
        aliases: Optional[list] = None,
        tax_id: str = "",
        verified: bool = False,
        created_at: str = "",
        updated_at: str = "",
        created_by: str = "",
        updated_by: str = "",
        notes: str = "",
        active: bool = True,
    ):
        self.canonical_name = (canonical_name or "").strip().upper()
        self.vendor_id = (vendor_id or "").strip() or (
            f"v_{self.canonical_name.lower()}" if self.canonical_name else ""
        )
        self.display_name = (display_name or "").strip()
        self.tax_id = re.sub(r"[^0-9]", "", tax_id or "")
        self.verified = bool(verified)
        self.active = bool(active)
        now = datetime.now(timezone.utc).isoformat()
        self.created_at = created_at or now
        self.updated_at = updated_at or self.created_at
        self.created_by = created_by or "seed"
        self.updated_by = updated_by or self.created_by
        self.notes = notes or ""
        self.aliases = self._clean_aliases(aliases or [])

    @staticmethod
    def _clean_aliases(raw: list) -> list[str]:
        """ลบช่องว่างเกิน ตัดซ้ำ คงลำดับเดิม"""
        out: list[str] = []
        seen: set[str] = set()
        for a in raw:
            if not isinstance(a, str):
                continue
            cleaned = unicodedata.normalize("NFKC", a).strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(cleaned)
        return out

    @property
    def normalized_aliases(self) -> list[str]:
        """alias ทุกตัวในรูปแบบเทียบกันได้ (รวมชื่อเต็มและชื่อแฟ้ม)"""
        names = list(self.aliases)
        if self.display_name:
            names.append(self.display_name)
        if self.canonical_name:
            names.append(self.canonical_name.replace("_", " "))
        out: list[str] = []
        seen: set[str] = set()
        for n in names:
            norm = normalize_company_name(n)
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out

    def to_dict(self) -> dict:
        return {
            "vendor_id": self.vendor_id,
            "canonical_name": self.canonical_name,
            "display_name": self.display_name,
            "aliases": self.aliases,
            "tax_id": self.tax_id,
            "verified": self.verified,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VendorRecord":
        allowed = {
            "vendor_id", "canonical_name", "display_name", "aliases",
            "tax_id", "verified", "active", "created_at", "updated_at",
            "created_by", "updated_by", "notes",
        }
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def __repr__(self) -> str:
        return f"<VendorRecord {self.canonical_name} verified={self.verified}>"


# ─────────────────────────────────────────────────────────────
# ผลการจับคู่
# ─────────────────────────────────────────────────────────────

# วิธีจับคู่ เรียงตามลำดับความน่าเชื่อถือในโจทย์
MATCH_TAX_ID = "tax_id"            # 1. เลขผู้เสียภาษีที่ยืนยันแล้ว
MATCH_ALIAS_EXACT = "alias_exact"  # 2. ตรง alias ที่อนุมัติไว้
MATCH_NAME_NORM = "name_normalized"  # 3. ชื่อไทย/อังกฤษหลัง normalize
MATCH_FUZZY = "fuzzy"              # 4. คล้าย — เสนอให้คนยืนยัน ไม่จัดเก็บเอง
MATCH_NONE = "none"                # 5/6. ไม่พบ → เข้าคิวตรวจสอบ

# วิธีที่เชื่อถือได้พอจะจัดเก็บอัตโนมัติ
_AUTO_ROUTE_METHODS = frozenset({
    MATCH_TAX_ID, MATCH_ALIAS_EXACT, MATCH_NAME_NORM,
})

# เกณฑ์ความคล้ายขั้นต่ำที่จะ "เสนอ" เป็นตัวเลือกให้คนดู
FUZZY_SUGGEST_THRESHOLD = float(
    os.getenv("VENDOR_FUZZY_THRESHOLD", "0.82").strip() or "0.82"
)


class VendorMatch:
    """ผลการระบุบริษัทจากข้อความเอกสาร

    auto_route บอกว่าเชื่อถือได้พอจะจัดแฟ้มเองหรือไม่
    candidates คือบริษัทที่ "อาจจะ" ตรง — ให้ผู้ดูแลยืนยัน
    """

    def __init__(
        self,
        vendor: Optional[VendorRecord] = None,
        method: str = MATCH_NONE,
        confidence: float = 0.0,
        candidates: Optional[list] = None,
        extracted_name: str = "",
        reason: str = "",
    ):
        self.vendor = vendor
        self.method = method
        self.confidence = round(float(confidence), 3)
        self.candidates = candidates or []
        self.extracted_name = extracted_name
        self.reason = reason

    @property
    def auto_route(self) -> bool:
        """จัดเก็บอัตโนมัติได้หรือไม่

        ต้องได้บริษัทที่ยืนยันแล้ว และจับคู่ด้วยวิธีที่เชื่อถือได้
        fuzzy ไม่เข้าเงื่อนไขนี้โดยเจตนา
        """
        return bool(
            self.vendor
            and self.vendor.verified
            and self.vendor.active
            and self.method in _AUTO_ROUTE_METHODS
        )

    @property
    def folder(self) -> str:
        """ชื่อแฟ้มปลายทาง — ถ้าเชื่อถือไม่พอจะได้แฟ้มพักสำหรับตรวจสอบ"""
        if self.auto_route and self.vendor:
            return self.vendor.canonical_name
        if self.candidates or self.extracted_name:
            return REVIEW_VENDOR_FOLDER
        return UNKNOWN_VENDOR_FOLDER

    @property
    def needs_review(self) -> bool:
        return not self.auto_route

    def to_dict(self) -> dict:
        return {
            "vendor_id": self.vendor.vendor_id if self.vendor else "",
            "canonical_name": self.vendor.canonical_name if self.vendor else "",
            "folder": self.folder,
            "method": self.method,
            "confidence": self.confidence,
            "auto_route": self.auto_route,
            "needs_review": self.needs_review,
            "extracted_name": self.extracted_name,
            "candidates": [
                {"canonical_name": c[0], "similarity": round(c[1], 3)}
                for c in self.candidates
            ],
            "reason": self.reason,
        }

    def __repr__(self) -> str:
        return (
            f"<VendorMatch {self.folder} method={self.method} "
            f"auto={self.auto_route}>"
        )


# ─────────────────────────────────────────────────────────────
# ข้อมูลตั้งต้น — ย้ายมาจาก BILL_COMPANY_REGISTRY ใน server.py
#
# แยกเป็นสองช่อง:
#   aliases  = ชื่อบริษัท ใช้จับคู่แล้วจัดเก็บอัตโนมัติได้
#   keywords = ชื่อสินค้า/โดเมน/อีเมล เป็นสัญญาณประกอบเท่านั้น
#              ชื่อสินค้าเพียงอย่างเดียว "ไม่" พอให้ตัดสินว่าเป็นบริษัทใด
#              เพราะสินค้ารุ่นเดียวกันอาจถูกอ้างในเอกสารของบริษัทอื่น
#
# ทุกรายการตั้ง verified=True เพราะใช้งานบน production มาแล้ว
# การเพิ่มบริษัทใหม่ "ไม่ต้อง" แก้ไฟล์นี้ — ลงทะเบียนผ่าน endpoint ผู้ดูแล
# ─────────────────────────────────────────────────────────────

SEED_VENDORS: tuple[dict, ...] = (
    {
        "canonical_name": "NIKKISO",
        "display_name": "NIKKISO MEDICAL (THAILAND) CO., LTD.",
        "tax_id": "0105537100222",
        "aliases": [
            "nikkiso", "nikkiso medical", "nikkiso thailand",
            "nikkiso medical (thailand)", "nikkiso co",
            "นิคคิโซ", "นิคคิโซ เมดิคัล",
        ],
        "keywords": [
            "hemodialysis package", "วัสดุสิ้นเปลืองสำหรับฟอกเลือด",
        ],
    },
    {
        "canonical_name": "MEDITOP",
        "display_name": "บริษัท เมดิท็อป จำกัด",
        "tax_id": "0105528039974",
        "aliases": [
            "meditop", "meditop co", "meditop thailand",
            "เมดิท็อป", "บจก.เมดิท็อป", "บริษัท เมดิท็อป",
        ],
        "keywords": [
            "blood line tubing", "mdt-bl-vt", "two filter vt", "fm-act",
        ],
    },
    {
        "canonical_name": "FRESENIUS",
        "display_name": "Fresenius Medical Care",
        "tax_id": "",
        "aliases": [
            "fresenius", "fresenius kabi", "fresenius medical care",
            "เฟรเซนีอุส",
        ],
        "keywords": ["fmc"],
    },
    {
        "canonical_name": "SP_LOGISTIC",
        "display_name": "S.P. Logistic",
        "tax_id": "",
        "aliases": [
            "s.p.logistic", "sp logistic", "s.p. logistic", "sp logistics",
            "เอส.พี.โลจิสติก",
        ],
        "keywords": [],
    },
    {
        "canonical_name": "THAI_WATER_GENERATION",
        "display_name": "บริษัท ไทย วอเตอร์ เจเนอเรชั่น จำกัด",
        "tax_id": "0315557000647",
        "aliases": [
            "thai water generation", "thai water",
            "ไทย วอเตอร์ เจเนอเรชั่น", "ไทย วอเตอร์ เจนเนอเรชั่น",
        ],
        "keywords": [
            "twg", "thaiwatergen.co.th", "thaiwatergen",
            "supernova", "ผลิตน้ำบริสุทธิ์", "ระบบ ro", "flo filter",
        ],
    },
    {
        "canonical_name": "BJC",
        "display_name": "Berli Jucker Public Company Limited",
        "tax_id": "0107536009226",
        "aliases": [
            "berli jucker", "berli jucker public", "bjc",
            "เบอร์ลี่ ยุคเกอร์", "บจก.เบอร์ลี่", "บริษัท เบอร์ลี่",
        ],
        "keywords": ["agani needle", "kdl 3ml syringe"],
    },
    {
        "canonical_name": "BLH",
        "display_name": "B.L.H. Trading Co., Ltd.",
        "tax_id": "0105496000431",
        "aliases": [
            "b.l.h. trading", "blh trading", "b.l.h.", "blh",
            "บี.เอล.เอช",
        ],
        "keywords": ["service@blh.co.th"],
    },
    {
        "canonical_name": "CENTRAL_POLY",
        "display_name": "Central Poly Trading Co., Ltd.",
        "tax_id": "0835560000178",
        "aliases": [
            "central poly", "central polytrading", "central poly trading",
            "centralpolytrading", "เซ็นทรัลโพลีเทรดดิ้ง", "เซ็นทรัลโพลี",
        ],
        "keywords": ["pharmasinlaboratories"],
    },
    {
        "canonical_name": "PINYO_PHARMACY",
        "display_name": "ห้างหุ้นส่วนจำกัด ภิญโญฟาร์มาซี",
        "tax_id": "",
        "aliases": [
            "pinyo pharmacy", "pinyo pharmacy ltd", "pinyo",
            "ภิญโญฟาร์มาซี", "ภิญโญ ฟาร์มาซี",
            "ห้างหุ้นส่วนจำกัด ภิญโญ",
        ],
        # "0103510000" เป็นเลขผู้เสียภาษีที่อ่านได้ไม่ครบ (10 จาก 13 หลัก)
        # จากตารางเดิม ใส่ไว้เป็นสัญญาณประกอบ ไม่ใส่ในช่อง tax_id เพราะ
        # เลขไม่ครบใช้จับคู่เองไม่ได้ — บริษัทอื่นอาจมีเลขขึ้นต้นเหมือนกัน
        "keywords": ["0103510000"],
    },
)


# ─────────────────────────────────────────────────────────────
# ดึงชื่อบริษัทที่ "น่าจะเป็น" ออกจากเอกสาร (สำหรับบริษัทที่ยังไม่รู้จัก)
# ─────────────────────────────────────────────────────────────

# รูปแบบชื่อนิติบุคคลที่พบในใบกำกับภาษีไทย/อังกฤษ
_NAME_PATTERNS = (
    # บริษัท ... จำกัด (มหาชน)
    re.compile(r"(บริษัท\s*[^\n]{2,60}?\s*จำกัด(?:\s*\(มหาชน\))?)"),
    # ห้างหุ้นส่วนจำกัด ...
    re.compile(r"(ห้างหุ้นส่วนจำกัด\s*[^\n]{2,60})"),
    # รูปย่อ: บจก. / บมจ. / หจก. ตามด้วยชื่อ
    # จำเป็นเพราะเอกสารจำนวนมากเขียนชื่อย่อ ไม่เขียน "บริษัท ... จำกัด"
    # เต็มรูป ถ้าไม่จับรูปนี้ ชื่อบริษัทจะดึงไม่ออกและเอกสารเข้าแฟ้ม
    # ตรวจสอบทั้งที่บริษัทอยู่ในทะเบียนแล้ว
    re.compile(r"((?:บจก|บมจ|หจก)\.?\s*[^\n]{2,60})"),
    # XXX CO., LTD. / XXX COMPANY LIMITED / XXX PUBLIC COMPANY LIMITED
    re.compile(
        r"([A-Za-z][A-Za-z0-9&.,()\-\s]{2,60}?"
        r"(?:CO\.?,?\s*LTD\.?|COMPANY\s+LIMITED|PUBLIC\s+COMPANY\s+LIMITED))",
        re.IGNORECASE,
    ),
)


def extract_candidate_names(text: str, limit: int = 5) -> list[str]:
    """ดึงชื่อบริษัทที่น่าจะเป็นผู้ขายออกจากข้อความ

    มองเฉพาะส่วนหัวเอกสาร (ก่อนคำว่า ลูกค้า/ผู้ซื้อ/Bill To)
    และกันชื่อคลินิกเองออก เพื่อไม่ให้ชื่อผู้ซื้อหลุดมาเป็นผู้ขาย

    ใช้เพื่อ "เสนอ" ให้ผู้ดูแลตรวจเท่านั้น ไม่นำไปเป็นชื่อแฟ้มโดยตรง
    """
    if not text:
        return []
    supplier_text, _buyer = split_supplier_section(text)
    out: list[str] = []
    seen: set[str] = set()
    for pattern in _NAME_PATTERNS:
        for raw in pattern.findall(supplier_text):
            name = re.sub(r"\s+", " ", raw).strip(" .,-")
            if len(name) < 4:
                continue
            norm = normalize_company_name(name)
            if not norm or norm in seen:
                continue
            if _is_buyer_self(norm):
                continue
            seen.add(norm)
            out.append(name)
            if len(out) >= limit:
                return out
    return out


# ─────────────────────────────────────────────────────────────
# ทะเบียนบริษัท — เก็บบน GCS
# ─────────────────────────────────────────────────────────────

class VendorRegistry:
    """ทะเบียนบริษัทคู่ค้า อ่าน/เขียนบน GCS

    โครงสร้างตามแบบเดียวกับ MasterPatientRegistry:
      - เขียนเป็นไฟล์ versioned แล้วชี้ด้วยการเรียงชื่อ (ไม่ลบไฟล์เก่า)
      - เส้นทางอ่านใช้แคชอายุสั้น เพื่อไม่ให้ทุกเอกสารยิง GCS ซ้ำ
      - เส้นทางเขียนบังคับโหลดใหม่ก่อน เพื่อความถูกต้องของ read-modify-write
    """

    GCS_PREFIX = "_vendors"
    GCS_FILE = "_vendors/registry.json"
    GCS_VERSIONED_PREFIX = "_vendors/registry_v"
    PENDING_PREFIX = "_vendors/pending"

    CACHE_TTL_SECONDS = float(
        os.getenv("VENDOR_CACHE_TTL", "30").strip() or "30"
    )

    def __init__(self, storage=None, seed: bool = True):
        self._storage = storage
        self._vendors: list[VendorRecord] = []
        # ตารางค้นหา
        self._by_canonical: dict[str, int] = {}
        self._by_tax_id: dict[str, int] = {}
        self._keywords: dict[str, list[str]] = {}  # canonical -> keywords
        self._loaded = False
        self._loaded_at = 0.0
        self._cache_ttl = self.CACHE_TTL_SECONDS
        self._use_seed = seed
        if seed:
            self._apply_seed()

    def set_storage(self, storage) -> None:
        self._storage = storage
        # บังคับโหลดใหม่ เพื่อให้ข้อมูลบน GCS ทับค่า seed
        self._loaded = False
        self._loaded_at = 0.0

    # ── Seed ─────────────────────────────────────────────────

    def _apply_seed(self) -> None:
        """ใส่ข้อมูลตั้งต้นเฉพาะบริษัทที่ยังไม่มีในทะเบียน

        ข้อมูลบน GCS สำคัญกว่า seed เสมอ — ถ้าผู้ดูแลแก้ alias หรือปิด
        บริษัทไว้ seed จะไม่เขียนทับ
        """
        for entry in SEED_VENDORS:
            canonical = entry["canonical_name"]
            if canonical in self._by_canonical:
                continue
            record = VendorRecord(
                canonical_name=canonical,
                display_name=entry.get("display_name", ""),
                aliases=list(entry.get("aliases", [])),
                tax_id=entry.get("tax_id", ""),
                verified=True,
                created_by="seed",
                notes="ข้อมูลตั้งต้นจาก BILL_COMPANY_REGISTRY",
            )
            self._vendors.append(record)
            self._keywords[canonical] = list(entry.get("keywords", []))
        self._rebuild_index()

    # ── Load / Save ──────────────────────────────────────────

    def _find_latest_versioned(self):
        try:
            blobs = list(self._storage.list_blobs(
                prefix=self.GCS_VERSIONED_PREFIX
            ))
            if not blobs:
                return None
            blobs.sort(key=lambda b: b.name, reverse=True)
            return blobs[0]
        except Exception as e:
            logger.warning("VendorRegistry: list versioned failed: %s", e)
            return None

    def _ingest(self, data: dict) -> None:
        """รับข้อมูลจากไฟล์ทะเบียน แล้วเติม seed สำหรับบริษัทที่ยังไม่มี"""
        records = []
        for item in data.get("vendors", []):
            try:
                records.append(VendorRecord.from_dict(item))
            except Exception:
                logger.warning("VendorRegistry: ข้ามรายการที่อ่านไม่ได้ 1 รายการ")
        self._vendors = records
        self._keywords = {
            k: list(v) for k, v in (data.get("keywords") or {}).items()
            if isinstance(v, list)
        }
        self._rebuild_index()
        if self._use_seed:
            self._apply_seed()

    def _load(self) -> None:
        if self._loaded or not self._storage:
            return
        try:
            latest = self._find_latest_versioned()
            if latest:
                self._ingest(json.loads(latest.download_as_text()))
                logger.info(
                    "VendorRegistry: โหลด %d บริษัทจาก %s",
                    len(self._vendors), latest.name,
                )
                self._loaded = True
                self._loaded_at = time.monotonic()
                return

            blob = self._storage.blob(self.GCS_FILE)
            if blob.exists():
                self._ingest(json.loads(blob.download_as_text()))
                logger.info(
                    "VendorRegistry: โหลด %d บริษัท (legacy path)",
                    len(self._vendors),
                )
            else:
                logger.info(
                    "VendorRegistry: ยังไม่มีไฟล์ทะเบียน — ใช้ข้อมูลตั้งต้น %d บริษัท",
                    len(self._vendors),
                )
            self._loaded = True
            self._loaded_at = time.monotonic()
        except Exception as e:
            # โหลดไม่ได้ → ยังมี seed ใช้งานต่อได้ ไม่ทำให้เอกสารตกหล่น
            logger.error("VendorRegistry: load ล้มเหลว: %s", type(e).__name__)

    def _refresh_if_stale(self) -> None:
        """เส้นทางอ่าน — โหลดใหม่เฉพาะเมื่อแคชเกิน TTL

        TTL ยาวกว่าทะเบียนผู้ป่วย (30 วิ) ได้ เพราะรายชื่อบริษัท
        เปลี่ยนน้อยมาก และผลของความไม่สดคือเอกสารเข้าแฟ้มตรวจสอบ
        ชั่วคราว ซึ่งย้ายย้อนหลังได้ ไม่ใช่การเข้าแฟ้มผิดบริษัท
        """
        if not self._loaded:
            self._load()
            return
        if (time.monotonic() - self._loaded_at) >= self._cache_ttl:
            self._loaded = False
            self._load()

    def invalidate_cache(self) -> None:
        self._loaded = False
        self._loaded_at = 0.0

    def _save(self) -> tuple:
        """เขียนทะเบียนลง GCS เป็นไฟล์ versioned ใหม่ (ไม่ลบไฟล์เก่า)"""
        if not self._storage:
            return False, "ไม่ได้ตั้งค่าที่เก็บข้อมูล"
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "vendors": [v.to_dict() for v in self._vendors],
            "keywords": self._keywords,
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        versioned = f"{self.GCS_VERSIONED_PREFIX}{stamp}.json"
        try:
            blob = self._storage.blob(versioned)
            blob.upload_from_string(body, content_type="application/json")
            # ไฟล์ legacy ใช้เป็น fallback สำหรับผู้อ่านที่ยังไม่รู้ versioned
            try:
                self._storage.blob(self.GCS_FILE).upload_from_string(
                    body, content_type="application/json"
                )
            except Exception as legacy_err:
                logger.warning(
                    "VendorRegistry: เขียน legacy path ไม่ได้: %s",
                    type(legacy_err).__name__,
                )
            self._loaded = True
            self._loaded_at = time.monotonic()
            return True, versioned
        except Exception as e:
            logger.error("VendorRegistry: save ล้มเหลว: %s", type(e).__name__)
            return False, type(e).__name__

    def _rebuild_index(self) -> None:
        self._by_canonical = {}
        self._by_tax_id = {}
        for i, v in enumerate(self._vendors):
            if v.canonical_name:
                self._by_canonical[v.canonical_name] = i
            # เลขผู้เสียภาษีต้องครบ 13 หลักจึงใช้เป็นกุญแจได้
            if is_valid_tax_id(v.tax_id):
                self._by_tax_id[v.tax_id] = i

    # ── อ่าน ─────────────────────────────────────────────────

    def get(self, canonical_name: str) -> Optional[VendorRecord]:
        self._refresh_if_stale()
        idx = self._by_canonical.get((canonical_name or "").strip().upper())
        return self._vendors[idx] if idx is not None else None

    def list_all(self, include_inactive: bool = False) -> list[VendorRecord]:
        self._refresh_if_stale()
        if include_inactive:
            return list(self._vendors)
        return [v for v in self._vendors if v.active]

    def count(self) -> int:
        self._refresh_if_stale()
        return len([v for v in self._vendors if v.active])

    def keywords_for(self, canonical_name: str) -> list[str]:
        self._refresh_if_stale()
        return list(self._keywords.get(canonical_name, []))

    # ── จับคู่บริษัทจากเอกสาร ────────────────────────────────

    # alias ที่สั้นกว่านี้ไม่ใช้ค้นในข้อความทั้งก้อน
    #
    # ตั้งไว้สูง (12) โดยเจตนา เพราะการค้นในข้อความที่ตัดช่องว่างออกแล้ว
    # มองขอบเขตคำไม่เห็น alias สั้นจึงอาจไปตรงกับ "ต้นชื่อ" ของบริษัทอื่น
    # เช่น "thaiwater" (9) เป็นส่วนต้นของ "thaiwatersupply" ซึ่งเป็นคนละ
    # บริษัท ความยาว 12 ตัวอักษรขึ้นไปทำให้โอกาสชนกันโดยบังเอิญต่ำมาก
    # alias ที่สั้นกว่านี้ยังใช้ได้ในชั้นแรก (เทียบกับชื่อที่ดึงมาได้)
    # ซึ่งมีขอบเขตคำชัดเจน เพียงแต่เอกสารนั้นต้องเข้าแฟ้มตรวจสอบก่อน
    MIN_BLOB_ALIAS_LEN = 12
    # สัดส่วนความยาวขั้นต่ำ เมื่อ alias เป็นส่วนหนึ่งของชื่อที่ดึงมาได้
    # กัน "THAI WATER" ไปตรงกับ "THAI WATER SUPPLY" ซึ่งเป็นคนละบริษัท
    MIN_CONTAINMENT_RATIO = 0.75

    def _match_by_tax_id(self, text: str) -> Optional[tuple]:
        """หาบริษัทจากเลขผู้เสียภาษี 13 หลัก

        ต้องตรงทั้ง 13 หลัก — ไม่รับเลขบางส่วน เพราะเลขขึ้นต้นเหมือนกัน
        เป็นเรื่องปกติและจะทำให้จับผิดบริษัท
        """
        for tax_id in extract_tax_ids(text):
            idx = self._by_tax_id.get(tax_id)
            if idx is not None:
                return self._vendors[idx], tax_id
        return None

    def _alias_entries(self) -> list[tuple]:
        """(vendor, normalized_alias) ทุกคู่ เรียงจาก alias ยาวไปสั้น"""
        pairs: list[tuple] = []
        for v in self._vendors:
            if not v.active:
                continue
            for norm in v.normalized_aliases:
                pairs.append((v, norm))
        pairs.sort(key=lambda p: len(p[1]), reverse=True)
        return pairs

    @staticmethod
    def _blob_contains_alias(norm_blob: str, alias: str) -> bool:
        """alias ปรากฏในข้อความที่ normalize แล้วหรือไม่

        เป็นการค้นแบบ substring ตรงๆ — ความปลอดภัยมาจากเกณฑ์ความยาว
        MIN_BLOB_ALIAS_LEN ไม่ใช่จากขอบเขตคำ เพราะข้อความที่ normalize
        แล้วไม่มีช่องว่างเหลือให้ดูขอบเขต
        """
        return bool(alias) and alias in norm_blob

    def _match_by_name(self, supplier_text: str) -> tuple:
        """จับคู่ด้วยชื่อบริษัท

        ทำสองชั้น:
          ชั้นแรก  เทียบกับ "ชื่อที่ดึงออกมาได้" (บริษัท...จำกัด / ...CO.,LTD)
                   ต้องตรงกันทั้งชื่อ หรือ alias กินพื้นที่ชื่อนั้น ≥75%
          ชั้นสอง  ถ้าดึงชื่อไม่ได้เลย ค่อยค้นในข้อความทั้งก้อน
                   และรับเฉพาะ alias ที่ยาวพอ (≥8 ตัวอักษรหลัง normalize)

        คืน (รายการบริษัทที่ตรง, วิธีที่ใช้, ชื่อที่ดึงได้)
        """
        candidate_names = extract_candidate_names(supplier_text)
        norm_candidates = [
            (raw, normalize_company_name(raw)) for raw in candidate_names
        ]
        norm_candidates = [(r, n) for r, n in norm_candidates if n]

        # ── ชั้นแรก: เทียบกับชื่อที่ดึงมาได้ ──
        hits: dict[str, tuple] = {}  # canonical -> (vendor, score, method)
        for raw_name, norm_name in norm_candidates:
            if _is_buyer_self(norm_name):
                continue
            for vendor, alias in self._alias_entries():
                if alias == norm_name:
                    score = 1.0
                elif alias in norm_name and (
                    len(alias) / len(norm_name) >= self.MIN_CONTAINMENT_RATIO
                ):
                    score = len(alias) / len(norm_name)
                elif norm_name in alias and (
                    len(norm_name) / len(alias) >= self.MIN_CONTAINMENT_RATIO
                ):
                    score = len(norm_name) / len(alias)
                else:
                    continue
                method = (
                    MATCH_ALIAS_EXACT if score >= 1.0 else MATCH_NAME_NORM
                )
                prev = hits.get(vendor.canonical_name)
                if prev is None or score > prev[1]:
                    hits[vendor.canonical_name] = (vendor, score, method)
        if hits:
            ordered = sorted(hits.values(), key=lambda h: h[1], reverse=True)
            return ordered, ordered[0][2], (
                candidate_names[0] if candidate_names else ""
            )

        # ── ชั้นสอง: ค้นในข้อความทั้งก้อน ──
        # ใช้เฉพาะเมื่อ "ดึงชื่อนิติบุคคลไม่ได้เลย" เช่นเอกสารสแกนที่ OCR
        # อ่านรูปแบบ "บริษัท…จำกัด" ไม่ออก
        #
        # สำคัญ: ถ้าดึงชื่อได้แล้วแต่ไม่ตรงทะเบียน ต้องไม่ตกมาชั้นนี้
        # ไม่งั้น "THAI WATER SUPPLY" (คนละบริษัท) จะไปตรงกับ alias
        # "thai water" ของ THAI_WATER_GENERATION ซึ่งเป็นการรวมบริษัทผิด
        if norm_candidates:
            return [], MATCH_NONE, candidate_names[0]

        norm_blob = normalize_company_name(supplier_text)
        if not norm_blob:
            return [], MATCH_NONE, ""
        blob_hits: dict[str, tuple] = {}
        for vendor, alias in self._alias_entries():
            if len(alias) < self.MIN_BLOB_ALIAS_LEN:
                continue
            if not self._blob_contains_alias(norm_blob, alias):
                continue
            prev = blob_hits.get(vendor.canonical_name)
            score = min(len(alias) / 20.0, 0.95)
            if prev is None or score > prev[1]:
                blob_hits[vendor.canonical_name] = (
                    vendor, score, MATCH_NAME_NORM
                )
        if blob_hits:
            ordered = sorted(
                blob_hits.values(), key=lambda h: h[1], reverse=True
            )
            return ordered, MATCH_NAME_NORM, ""
        return [], MATCH_NONE, (candidate_names[0] if candidate_names else "")

    def _fuzzy_candidates(self, supplier_text: str) -> list[tuple]:
        """เสนอบริษัทที่ชื่อ "คล้าย" — ไม่ใช้จัดเก็บอัตโนมัติ

        คืน [(canonical_name, similarity)] เรียงจากคล้ายมากไปน้อย
        """
        out: list[tuple] = []
        names = extract_candidate_names(supplier_text)
        if not names:
            return out
        best: dict[str, float] = {}
        for raw in names:
            norm = normalize_company_name(raw)
            if not norm or _is_buyer_self(norm):
                continue
            for vendor, alias in self._alias_entries():
                ratio = SequenceMatcher(None, norm, alias).ratio()
                if ratio >= FUZZY_SUGGEST_THRESHOLD:
                    key = vendor.canonical_name
                    if ratio > best.get(key, 0.0):
                        best[key] = ratio
        out = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        return out[:5]

    def _keyword_hits(self, text: str) -> dict:
        """นับ keyword สินค้า/โดเมนที่พบ — เป็นสัญญาณประกอบเท่านั้น"""
        lowered = text.lower()
        out: dict[str, int] = {}
        for canonical, keywords in self._keywords.items():
            hits = sum(1 for kw in keywords if kw and kw.lower() in lowered)
            if hits:
                out[canonical] = hits
        return out

    def match_document(self, text: str) -> VendorMatch:
        """ระบุบริษัทผู้ขายจากข้อความเอกสาร

        ลำดับตามข้อกำหนด:
          1. เลขผู้เสียภาษีที่ยืนยันแล้ว
          2. ชื่อตรงกับ alias ที่อนุมัติไว้
          3. ชื่อไทย/อังกฤษหลัง normalize
          4. ความคล้าย → เสนอให้คนยืนยัน (ไม่จัดเก็บเอง)
          5. ไม่พบ → เข้าคิวตรวจสอบ

        ข้อที่ไม่ทำโดยเจตนา: ไม่ส่งเนื้อหาเอกสารไป External AI
        (EXTERNAL_AI_CLASSIFICATION_ALLOWED = False) ข้อ 5 ในข้อกำหนด
        จึงเป็นการตรวจโดยคนแทน ซึ่งปลอดภัยกว่าและตรงกับนโยบาย PDPA
        """
        self._refresh_if_stale()
        if not text or not text.strip():
            return VendorMatch(
                method=MATCH_NONE, reason="ไม่มีข้อความให้ตรวจ"
            )

        supplier_text, _buyer_text = split_supplier_section(text)
        keyword_hits = self._keyword_hits(supplier_text or text)

        # ── 1. เลขผู้เสียภาษี ──
        # ดูเฉพาะส่วนผู้ขาย เพื่อไม่ให้เลขผู้เสียภาษีของคลินิก (ผู้ซื้อ)
        # ไปจับเป็นบริษัทคู่ค้า
        tax_hit = self._match_by_tax_id(supplier_text or text)
        if tax_hit:
            vendor, tax_id = tax_hit
            if vendor.active:
                return VendorMatch(
                    vendor=vendor,
                    method=MATCH_TAX_ID,
                    confidence=1.0,
                    extracted_name=vendor.display_name,
                    reason=f"เลขผู้เสียภาษีตรงทะเบียน ({tax_id[:4]}…)",
                )

        # ── 2–3. ชื่อบริษัท ──
        hits, method, extracted = self._match_by_name(supplier_text or text)

        if len(hits) == 1:
            vendor, score, used = hits[0]
            confidence = min(0.6 + score * 0.35, 0.98)
            if keyword_hits.get(vendor.canonical_name):
                confidence = min(confidence + 0.02, 0.99)
            return VendorMatch(
                vendor=vendor, method=used, confidence=confidence,
                extracted_name=extracted or vendor.display_name,
                reason=f"ชื่อบริษัทตรงทะเบียน ({used})",
            )

        if len(hits) > 1:
            # ตรงหลายบริษัท — ห้ามเดา ให้คนตัดสิน
            top, top_score, used = hits[0]
            second_score = hits[1][1]
            # ยอมรับเฉพาะเมื่อตัวแรกชัดกว่าตัวที่สองอย่างมีนัย
            # และมี keyword ของบริษัทนั้นช่วยยืนยัน
            if (
                top_score >= 0.99
                and second_score < 0.9
                and keyword_hits.get(top.canonical_name)
            ):
                return VendorMatch(
                    vendor=top, method=used,
                    confidence=0.9,
                    extracted_name=extracted or top.display_name,
                    reason="ชื่อตรงพอดีและมีสินค้าของบริษัทนี้ยืนยัน",
                )
            return VendorMatch(
                vendor=None, method=MATCH_FUZZY,
                confidence=round(top_score, 3),
                candidates=[(h[0].canonical_name, h[1]) for h in hits[:5]],
                extracted_name=extracted,
                reason=(
                    f"ชื่อตรงได้หลายบริษัท ({len(hits)} ราย) — "
                    "ต้องให้ผู้ดูแลยืนยัน"
                ),
            )

        # ── 4. ความคล้าย → เสนอให้คนยืนยัน ──
        fuzzy = self._fuzzy_candidates(supplier_text or text)
        if fuzzy:
            return VendorMatch(
                vendor=None, method=MATCH_FUZZY,
                confidence=fuzzy[0][1],
                candidates=fuzzy,
                extracted_name=extracted,
                reason="ชื่อคล้ายบริษัทในทะเบียน — ต้องให้ผู้ดูแลยืนยัน",
            )

        # keyword สินค้าอย่างเดียวไม่พอตัดสิน แต่ใช้เป็นข้อเสนอได้
        if keyword_hits:
            ordered = sorted(
                keyword_hits.items(), key=lambda kv: kv[1], reverse=True
            )
            return VendorMatch(
                vendor=None, method=MATCH_FUZZY,
                confidence=0.4,
                candidates=[(c, 0.4) for c, _ in ordered[:5]],
                extracted_name=extracted,
                reason=(
                    "พบเฉพาะชื่อสินค้าที่เคยซื้อจากบริษัทนี้ "
                    "ไม่พบชื่อบริษัท — ต้องให้ผู้ดูแลยืนยัน"
                ),
            )

        # ── 5. ไม่พบ ──
        return VendorMatch(
            vendor=None, method=MATCH_NONE, confidence=0.0,
            extracted_name=extracted,
            reason=(
                "ไม่พบบริษัทในทะเบียน — เข้าแฟ้มตรวจสอบ"
                if extracted else "ระบุชื่อบริษัทจากเอกสารไม่ได้"
            ),
        )

    # ── เขียน: ลงทะเบียน / แก้ไข / ยืนยัน ────────────────────

    def _force_reload(self) -> None:
        """บังคับโหลดใหม่ก่อนเขียน — ความถูกต้องของ read-modify-write

        จำเป็นเพราะมีหลาย worker เขียนทะเบียนเดียวกันได้
        """
        self._loaded = False
        self._load()

    def find_conflicts(
        self, canonical_name: str, aliases: list, tax_id: str = "",
        exclude: str = "",
    ) -> list[dict]:
        """หาบริษัทในทะเบียนที่อาจเป็นรายเดียวกัน — กันแฟ้มซ้ำ

        ตรวจสามแบบ:
          1. เลขผู้เสียภาษีซ้ำ → เป็นรายเดียวกันแน่นอน
          2. alias หลัง normalize ซ้ำกันพอดี
          3. ชื่อคล้ายเกินเกณฑ์ → เตือนให้คนดู ไม่รวมให้เอง

        ใช้ก่อน register เพื่อไม่ให้ "บริษัท เอ จำกัด" กับ "บจก. เอ"
        กลายเป็นสองแฟ้ม
        """
        self._refresh_if_stale()
        exclude_key = (exclude or "").strip().upper()
        new_norms = {
            normalize_company_name(a) for a in (aliases or [])
        }
        new_norms.add(normalize_company_name(canonical_name.replace("_", " ")))
        new_norms.discard("")
        clean_tax = re.sub(r"[^0-9]", "", tax_id or "")

        out: list[dict] = []
        for v in self._vendors:
            if v.canonical_name == exclude_key:
                continue
            if clean_tax and is_valid_tax_id(clean_tax) and v.tax_id == clean_tax:
                out.append({
                    "canonical_name": v.canonical_name,
                    "kind": "tax_id",
                    "detail": "เลขผู้เสียภาษีเดียวกัน — น่าจะเป็นบริษัทเดียวกัน",
                    "similarity": 1.0,
                })
                continue
            existing = set(v.normalized_aliases)
            overlap = existing & new_norms
            if overlap:
                out.append({
                    "canonical_name": v.canonical_name,
                    "kind": "alias",
                    "detail": f"ชื่อซ้ำกับที่มีอยู่: {sorted(overlap)[0]}",
                    "similarity": 1.0,
                })
                continue
            best = 0.0
            for a in existing:
                for b in new_norms:
                    best = max(best, SequenceMatcher(None, a, b).ratio())
            if best >= FUZZY_SUGGEST_THRESHOLD:
                out.append({
                    "canonical_name": v.canonical_name,
                    "kind": "similar",
                    "detail": "ชื่อคล้ายกันมาก — ตรวจว่าเป็นบริษัทเดียวกันหรือไม่",
                    "similarity": round(best, 3),
                })
        return sorted(out, key=lambda c: c["similarity"], reverse=True)

    def register(
        self,
        canonical_name: str,
        display_name: str = "",
        aliases: Optional[list] = None,
        tax_id: str = "",
        keywords: Optional[list] = None,
        verified: bool = False,
        created_by: str = "admin",
        notes: str = "",
        allow_similar: bool = False,
    ) -> tuple:
        """ลงทะเบียนบริษัทใหม่

        ไม่ต้องแก้ source code — นี่คือทางเพิ่มบริษัทใหม่

        allow_similar=False จะปฏิเสธถ้าพบบริษัทที่อาจเป็นรายเดียวกัน
        เพื่อกันแฟ้มซ้ำจากการสะกดต่างกัน ผู้ดูแลยืนยันแล้วส่ง True มาได้

        Returns: (สำเร็จ, ข้อความ, VendorRecord | None)
        """
        name = (canonical_name or "").strip().upper().replace(" ", "_")
        if not is_valid_canonical_name(name):
            return False, (
                "ชื่อแฟ้มต้องเป็น A-Z, 0-9, _ หรืออักษรไทย ยาว 2–40 ตัว "
                "ไม่ขึ้นต้นด้วย _ และไม่ใช่ชื่อที่ Windows สงวนไว้"
            ), None
        if name in _RESERVED_FOLDERS:
            return False, f"'{name}' เป็นชื่อแฟ้มระบบ ใช้เป็นชื่อบริษัทไม่ได้", None

        clean_tax = re.sub(r"[^0-9]", "", tax_id or "")
        if clean_tax and not is_valid_tax_id(clean_tax):
            return False, (
                "เลขประจำตัวผู้เสียภาษีต้องมี 13 หลักพอดี "
                "(เลขไม่ครบใช้จับคู่ไม่ได้ เพราะอาจชนกับบริษัทอื่น)"
            ), None

        self._force_reload()
        if name in self._by_canonical:
            return False, f"มีบริษัท '{name}' ในทะเบียนอยู่แล้ว", None

        conflicts = self.find_conflicts(name, aliases or [], clean_tax)
        blocking = [c for c in conflicts if c["kind"] in ("tax_id", "alias")]
        if blocking:
            return False, (
                f"ซ้ำกับ '{blocking[0]['canonical_name']}': "
                f"{blocking[0]['detail']}"
            ), None
        if conflicts and not allow_similar:
            names = ", ".join(c["canonical_name"] for c in conflicts[:3])
            return False, (
                f"ชื่อคล้ายกับบริษัทที่มีอยู่ ({names}) — "
                "ตรวจว่าเป็นรายเดียวกันหรือไม่ "
                "ถ้าเป็นบริษัทใหม่จริงให้ยืนยันด้วย allow_similar"
            ), None

        record = VendorRecord(
            canonical_name=name,
            display_name=display_name or name.replace("_", " "),
            aliases=list(aliases or []),
            tax_id=clean_tax,
            verified=bool(verified),
            created_by=created_by,
            updated_by=created_by,
            notes=notes,
        )
        self._vendors.append(record)
        if keywords:
            self._keywords[name] = list(keywords)
        self._rebuild_index()
        ok, detail = self._save()
        if not ok:
            # ย้อนรายการออกจากหน่วยความจำ เพื่อไม่ให้สถานะต่างจากที่เก็บจริง
            self._vendors = [
                v for v in self._vendors if v.canonical_name != name
            ]
            self._keywords.pop(name, None)
            self._rebuild_index()
            return False, f"บันทึกทะเบียนไม่สำเร็จ ({detail})", None
        logger.info("VendorRegistry: ลงทะเบียน %s (verified=%s)", name, verified)
        return True, f"ลงทะเบียน '{name}' สำเร็จ", record

    def update(
        self,
        canonical_name: str,
        display_name: Optional[str] = None,
        add_aliases: Optional[list] = None,
        remove_aliases: Optional[list] = None,
        tax_id: Optional[str] = None,
        keywords: Optional[list] = None,
        verified: Optional[bool] = None,
        active: Optional[bool] = None,
        updated_by: str = "admin",
        notes: Optional[str] = None,
    ) -> tuple:
        """แก้ไขบริษัทที่มีอยู่ — ชื่อแฟ้ม (canonical_name) เปลี่ยนไม่ได้

        ชื่อแฟ้มเปลี่ยนไม่ได้โดยเจตนา เพราะเอกสารเก่าอ้างอิง path นั้นไว้แล้ว
        ถ้าต้องเปลี่ยนจริง ให้ลงทะเบียนใหม่แล้วปิดรายการเดิม (active=False)
        """
        name = (canonical_name or "").strip().upper()
        self._force_reload()
        idx = self._by_canonical.get(name)
        if idx is None:
            return False, f"ไม่พบบริษัท '{name}' ในทะเบียน", None

        v = self._vendors[idx]
        before = v.to_dict()

        if tax_id is not None:
            clean_tax = re.sub(r"[^0-9]", "", tax_id)
            if clean_tax and not is_valid_tax_id(clean_tax):
                return False, "เลขประจำตัวผู้เสียภาษีต้องมี 13 หลักพอดี", None
            dup = self._by_tax_id.get(clean_tax)
            if clean_tax and dup is not None and dup != idx:
                return False, (
                    f"เลขผู้เสียภาษีนี้เป็นของ "
                    f"'{self._vendors[dup].canonical_name}' อยู่แล้ว"
                ), None
            v.tax_id = clean_tax
        if display_name is not None:
            v.display_name = display_name.strip()
        if add_aliases:
            v.aliases = VendorRecord._clean_aliases(
                list(v.aliases) + list(add_aliases)
            )
        if remove_aliases:
            drop = {a.strip().lower() for a in remove_aliases}
            v.aliases = [a for a in v.aliases if a.lower() not in drop]
        if keywords is not None:
            self._keywords[name] = list(keywords)
        if verified is not None:
            v.verified = bool(verified)
        if active is not None:
            v.active = bool(active)
        if notes is not None:
            v.notes = notes
        v.updated_at = datetime.now(timezone.utc).isoformat()
        v.updated_by = updated_by

        self._rebuild_index()
        ok, detail = self._save()
        if not ok:
            self._vendors[idx] = VendorRecord.from_dict(before)
            self._rebuild_index()
            return False, f"บันทึกทะเบียนไม่สำเร็จ ({detail})", None
        logger.info("VendorRegistry: แก้ไข %s โดย %s", name, updated_by)
        return True, f"แก้ไข '{name}' สำเร็จ", v

    def verify(self, canonical_name: str, updated_by: str = "admin") -> tuple:
        """ยืนยันบริษัท — เฉพาะบริษัทที่ยืนยันแล้วจึงจัดเก็บอัตโนมัติได้"""
        return self.update(
            canonical_name, verified=True, updated_by=updated_by,
        )

    def deactivate(self, canonical_name: str, updated_by: str = "admin") -> tuple:
        """ปิดการใช้งานบริษัท — ไม่ลบข้อมูลและไม่แตะเอกสารเดิม

        ไม่มีเมธอดลบ เพราะชื่อแฟ้มถูกอ้างอยู่ใน path ของเอกสารที่เก็บไปแล้ว
        """
        return self.update(
            canonical_name, active=False, updated_by=updated_by,
        )
