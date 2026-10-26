#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Laboratory Data Extraction Engine — Kidney Clinical Intelligence Platform 2.0

Regex-based extraction ไม่ใช้ AI ภายนอก — ดึงข้อมูลจากข้อความที่
extract จาก PDF หรือ OCR แล้ว

Analytes ที่รองรับ (15+):
  CBC: Hemoglobin, Hematocrit, WBC, Platelet
  Renal: BUN, Creatinine, eGFR
  Electrolytes: Sodium, Potassium, Chloride, Bicarbonate
  Minerals: Calcium, Phosphate
  Others: Albumin, FBS/Glucose, PTH, Ferritin, TSAT, Kt/V
  Proteinuria: UPCR, UACR
  Glycemic: HbA1c
  Lipids: Total Cholesterol, LDL, HDL, Triglycerides
  Iron Studies: Iron, TIBC

กฎความปลอดภัย:
  - ห้ามสร้างค่าที่ไม่มีอยู่ในเอกสาร
  - ผลทั้งหมดเริ่มเป็น pending_review
  - ไม่เก็บข้อมูลระบุตัวผู้ป่วย
  - ไม่ส่งข้อมูลไปยังบริการ AI ภายนอก
"""

from __future__ import annotations

import re
import logging
from datetime import datetime
from typing import Optional

from lab_models import (
    LabResult, LabReport, ExtractionMethod, ReviewStatus,
)
from lab_rules import validate_result, get_reference_range, ANALYTE_SPECS

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# Analyte name mappings — maps various text forms to canonical names
# ═══════════════════════════════════════════════════════════

ANALYTE_ALIASES: dict[str, str] = {
    # Creatinine
    "creatinine": "Creatinine",
    "cr": "Creatinine",
    "creat": "Creatinine",
    "serum creatinine": "Creatinine",
    "ครีอะตินีน": "Creatinine",
    "ครีเอตินิน": "Creatinine",
    "ครีเอตินีน": "Creatinine",
    # BUN
    "bun": "BUN",
    "blood urea nitrogen": "BUN",
    "urea nitrogen": "BUN",
    "บียูเอ็น": "BUN",
    "ยูเรียไนโตรเจน": "BUN",
    # eGFR
    "egfr": "eGFR",
    "gfr": "eGFR",
    "estimated gfr": "eGFR",
    "อีจีเอฟอาร์": "eGFR",
    # Sodium
    "sodium": "Sodium",
    "sodium (na)": "Sodium",
    "na": "Sodium",
    "na+": "Sodium",
    "โซเดียม": "Sodium",
    # Potassium
    "potassium": "Potassium",
    "potassium (k)": "Potassium",
    "k": "Potassium",
    "k+": "Potassium",
    "โพแทสเซียม": "Potassium",
    # Chloride
    "chloride": "Chloride",
    "chloride (cl)": "Chloride",
    "cl": "Chloride",
    "cl-": "Chloride",
    "คลอไรด์": "Chloride",
    # Bicarbonate
    "bicarbonate": "Bicarbonate",
    "bicarbonate (hco3)": "Bicarbonate",
    "hco3": "Bicarbonate",
    "hco3-": "Bicarbonate",
    "co2": "Bicarbonate",
    "total co2": "Bicarbonate",
    "tco2": "Bicarbonate",
    "ไบคาร์บอเนต": "Bicarbonate",
    # Calcium
    "calcium": "Calcium",
    "calcium (ca)": "Calcium",
    "ca": "Calcium",
    "total calcium": "Calcium",
    "แคลเซียม": "Calcium",
    # Phosphate
    "phosphate": "Phosphate",
    "phosphate (po4)": "Phosphate",
    "phosphorus": "Phosphate",
    "po4": "Phosphate",
    "phos": "Phosphate",
    "inorganic phosphorus": "Phosphate",
    "ฟอสเฟต": "Phosphate",
    "ฟอสฟอรัส": "Phosphate",
    # Albumin
    "albumin": "Albumin",
    "alb": "Albumin",
    "serum albumin": "Albumin",
    "อัลบูมิน": "Albumin",
    # Glucose / FBS
    "fbs": "FBS",
    "fbs (glucose)": "FBS",
    "fasting blood sugar": "FBS",
    "fasting glucose": "FBS",
    "น้ำตาลอดอาหาร": "FBS",
    "glucose": "Glucose",
    "blood glucose": "Glucose",
    "กลูโคส": "Glucose",
    "น้ำตาลในเลือด": "Glucose",
    # Hemoglobin
    "hemoglobin": "Hemoglobin",
    "hemoglobin (hb)": "Hemoglobin",
    "hb": "Hemoglobin",
    "hgb": "Hemoglobin",
    "ฮีโมโกลบิน": "Hemoglobin",
    # Hematocrit
    "hematocrit": "Hematocrit",
    "hematocrit (hct)": "Hematocrit",
    "hct": "Hematocrit",
    "ฮีมาโตคริต": "Hematocrit",
    # WBC
    "wbc": "WBC",
    "white blood cell": "WBC",
    "white blood cells": "WBC",
    "white cell count": "WBC",
    "เม็ดเลือดขาว": "WBC",
    # Platelet
    "platelet": "Platelet",
    "platelets": "Platelet",
    "plt": "Platelet",
    "platelet count": "Platelet",
    "เกล็ดเลือด": "Platelet",
    # PTH
    "pth": "PTH",
    "parathyroid hormone": "PTH",
    "intact pth": "PTH",
    "ipth": "PTH",
    "พาราไทรอยด์ฮอร์โมน": "PTH",
    # Ferritin
    "ferritin": "Ferritin",
    "serum ferritin": "Ferritin",
    "เฟอร์ริติน": "Ferritin",
    # TSAT
    "tsat": "TSAT",
    "transferrin saturation": "TSAT",
    "iron saturation": "TSAT",
    # Uric Acid
    "uric acid": "Uric Acid",
    "กรดยูริก": "Uric Acid",
    "ยูริก": "Uric Acid",
    # Kt/V
    "kt/v": "Kt/V",
    "ktv": "Kt/V",
    "kt / v": "Kt/V",
    # ── Phase B: New analytes ──
    # UPCR (Urine Protein-Creatinine Ratio)
    "upcr": "UPCR",
    "urine protein creatinine ratio": "UPCR",
    "urine protein/creatinine ratio": "UPCR",
    "protein creatinine ratio": "UPCR",
    "up/cr": "UPCR",
    "up/ucr": "UPCR",
    "อัตราส่วนโปรตีนต่อครีอะตินีนในปัสสาวะ": "UPCR",
    # UACR (Urine Albumin-Creatinine Ratio)
    "uacr": "UACR",
    "urine albumin creatinine ratio": "UACR",
    "urine albumin/creatinine ratio": "UACR",
    "albumin creatinine ratio": "UACR",
    "acr": "UACR",
    "ua/cr": "UACR",
    "microalbumin/creatinine": "UACR",
    "microalbumin": "UACR",
    "อัตราส่วนอัลบูมินต่อครีอะตินีนในปัสสาวะ": "UACR",
    "ไมโครอัลบูมิน": "UACR",
    # HbA1c
    "hba1c": "HbA1c",
    "hb a1c": "HbA1c",
    "hemoglobin a1c": "HbA1c",
    "glycated hemoglobin": "HbA1c",
    "glycosylated hemoglobin": "HbA1c",
    "a1c": "HbA1c",
    "เอชบีเอวันซี": "HbA1c",
    "ฮีโมโกลบินเอวันซี": "HbA1c",
    # Total Cholesterol
    "total cholesterol": "Total Cholesterol",
    "cholesterol": "Total Cholesterol",
    "cholesterol total": "Total Cholesterol",
    "t.chol": "Total Cholesterol",
    "t-chol": "Total Cholesterol",
    "tc": "Total Cholesterol",
    "คอเลสเตอรอล": "Total Cholesterol",
    "โคเลสเตอรอลรวม": "Total Cholesterol",
    # LDL
    "ldl": "LDL",
    "ldl cholesterol": "LDL",
    "ldl-c": "LDL",
    "ldl-chol": "LDL",
    "low density lipoprotein": "LDL",
    "แอลดีแอล": "LDL",
    # HDL
    "hdl": "HDL",
    "hdl cholesterol": "HDL",
    "hdl-c": "HDL",
    "hdl-chol": "HDL",
    "high density lipoprotein": "HDL",
    "เอชดีแอล": "HDL",
    # Triglycerides
    "triglyceride": "Triglycerides",
    "triglycerides": "Triglycerides",
    "tg": "Triglycerides",
    "trig": "Triglycerides",
    "ไตรกลีเซอไรด์": "Triglycerides",
    "ไตรกลีเซอร์ไรด์": "Triglycerides",
    # Iron (Serum Iron)
    "iron": "Iron",
    "serum iron": "Iron",
    "fe": "Iron",
    "เหล็ก": "Iron",
    "ธาตุเหล็ก": "Iron",
    "เหล็กในเลือด": "Iron",
    # TIBC (Total Iron Binding Capacity)
    "tibc": "TIBC",
    "total iron binding capacity": "TIBC",
    "iron binding capacity": "TIBC",
    "ความสามารถในการจับธาตุเหล็ก": "TIBC",
}


# ═══════════════════════════════════════════════════════════
# Unit patterns
# ═══════════════════════════════════════════════════════════

UNIT_PATTERNS = [
    r"mg/[dD][lL]",
    r"g/[dD][lL]",
    r"g/g(?:Cr)?",              # Phase B: UPCR (g/g, g/gCr)
    r"mg/g(?:Cr)?",             # Phase B: UACR (mg/g, mg/gCr)
    r"mg/mg",                   # Phase B: UPCR alt
    r"mmol/[lL]",
    r"mmol/mol",                # Phase B: HbA1c (mmol/mol)
    r"mEq/[lL]",
    r"mL/min(?:/1\.73m2)?",
    r"ml/min(?:/1\.73m2)?",
    r"x?10\^[39]/[uµ][lL]",
    r"cells/[uµ][lL]",
    # หน่วยนับเม็ดเลือดที่เขียนขึ้นต้นด้วยขีดทับ พบในใบ CBC ทั่วไป
    # เช่น "WBC  5,600 /uL" และ "Platelet  250,000 /uL"
    r"/[uµ][lL]",
    r"/mm3",
    r"/cu\.?mm",
    r"pg/[mM][lL]",
    r"ng/[mM][lL]",
    r"[pnuµ]mol/[lL]",
    r"[µu]g/[dD][lL]",         # Phase B: Iron/TIBC (µg/dL)
    r"[µu]g/[lL]",
    r"%",
]

UNIT_RE = re.compile(r"(" + "|".join(UNIT_PATTERNS) + r")")


# ═══════════════════════════════════════════════════════════
# Date extraction
# ═══════════════════════════════════════════════════════════

DATE_PATTERNS = [
    # 2026-01-15
    re.compile(r"(?:specimen|collection|date)[:\s]*(\d{4}-\d{2}-\d{2})", re.IGNORECASE),
    # 15/01/2026
    re.compile(r"(?:specimen|collection|date)[:\s]*(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE),
    # วันที่ 15 ม.ค. 2569
    re.compile(r"วันที่[:\s]*(\d{1,2}\s*[ก-๙.]+\s*\d{4})", re.IGNORECASE),
    # Standalone date patterns
    re.compile(r"Specimen Date[:\s]*(\d{4}-\d{2}-\d{2})", re.IGNORECASE),
]


def extract_specimen_date(text: str) -> str:
    """ดึงวันที่เก็บตัวอย่างจากข้อความ."""
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return "unknown"


# ═══════════════════════════════════════════════════════════
# Main extraction — supports two text layouts:
#   1. Tabular: "Creatinine    5.2    mg/dL    0.6-1.2"
#   2. Per-line (pypdf column extraction):
#      "Creatinine\n5.2\nmg/dL\n0.6-1.2"
# ═══════════════════════════════════════════════════════════

# Pattern for tabular single-line extraction
# e.g., "Creatinine    5.2    mg/dL    0.6-1.2"
# e.g., "Creatinine: 5.2 mg/dL 0.6-1.2"  (colon — Phase 14.11 fix)
# Phase 14.9: รองรับ Thai+English mixed names:
# e.g., "ครีอะตินีน (Cr)     5.2     mg/dL     0.6-1.2"
# NOTE: Uses [^\S\n]+ to prevent cross-line matching (Phase 14.11).
TABULAR_RE = re.compile(
    r"(?P<name>[A-Za-z/ \t()0-9]+?)"    # analyte name (English, space/tab only, digits for HbA1c)
    r":?[^\S\n]+"                        # optional colon + horizontal whitespace (no newline)
    r"(?P<value>\d+\.?\d*)"            # numeric value
    r"[^\S\n]+"
    # flag ของห้องแล็บ (H/L/HH/LL/High/Low/*) อาจคั่นระหว่างค่ากับหน่วย
    # ถ้าไม่ข้าม flag จะถูกจับเป็น "หน่วย" แล้วหน่วยจริงตกหล่น
    r"(?:(?:HH|LL|High|Low|H|L|\*)[^\S\n]+)?"
    r"(?P<unit>[a-zA-Z%/^µu0-9]+(?:/[a-zA-Z0-9.µu^]+)*)"  # unit
    r"(?:[^\S\n]+"
    r"(?P<ref>[<>]?\d+\.?\d*(?:\s*-\s*\d+\.?\d*)?))?",  # optional reference
    re.IGNORECASE,
)

# Phase 14.9: Tabular pattern for Thai analyte names
# e.g., "ครีอะตินีน     5.2     mg/dL     0.6-1.2"
# e.g., "ครีอะตินีน (Cr)     5.2     mg/dL     0.6-1.2"
# e.g., "ครีเอตินิน (Creatinine) 5.2 mg/dL 0.6-1.2"  (single-space — Phase 14.10 fix)
# NOTE: Uses [^\S\n]+ (horizontal whitespace) to prevent cross-line matching.
#       The name group requires ≥2 chars to reduce false positives.
TABULAR_THAI_RE = re.compile(
    r"(?P<name>[ก-๙A-Za-z/ ()0-9]{2,}?)"  # analyte name (Thai+English+digits, ≥2 chars)
    r":?[^\S\n]+"                           # optional colon + 1+ horizontal spaces (no newline)
    r"(?P<value>\d+\.?\d*)"                # numeric value
    r"[^\S\n]+"
    # flag ของห้องแล็บ (H/L/HH/LL/High/Low/*) อาจคั่นระหว่างค่ากับหน่วย
    # ถ้าไม่ข้าม flag จะถูกจับเป็น "หน่วย" แล้วหน่วยจริงตกหล่น
    r"(?:(?:HH|LL|High|Low|H|L|\*)[^\S\n]+)?"
    r"(?P<unit>[a-zA-Z%/^µu0-9]+(?:/[a-zA-Z0-9.µu^]+)*)"  # unit
    r"(?:[^\S\n]+"
    r"(?P<ref>[<>]?\d+\.?\d*(?:\s*-\s*\d+\.?\d*)?))?",  # optional reference
    re.IGNORECASE,
)


def _normalize_analyte(name: str) -> Optional[str]:
    """แปลงชื่อ analyte เป็นชื่อมาตรฐาน.

    รองรับ:
    - English: "Creatinine", "Cr", "serum creatinine"
    - Thai: "ครีอะตินีน", "บียูเอ็น"
    - Mixed: "ครีอะตินีน (Cr)", "บียูเอ็น (BUN)"
    """
    name_clean = name.strip().lower()
    # Remove trailing whitespace and common suffixes
    name_clean = re.sub(r"\s+$", "", name_clean)

    # Direct match
    if name_clean in ANALYTE_ALIASES:
        return ANALYTE_ALIASES[name_clean]

    # Try removing common prefixes
    for prefix in ("serum ", "plasma ", "blood ", "total "):
        stripped = name_clean.removeprefix(prefix)
        if stripped in ANALYTE_ALIASES:
            return ANALYTE_ALIASES[stripped]

    # ── Phase 14.9: Thai+English mixed format ──
    # Pattern: "ครีอะตินีน (Cr)" or "บียูเอ็น(BUN)"
    paren_match = re.match(r"(.+?)\s*\(([^)]+)\)", name_clean)
    if paren_match:
        thai_part = paren_match.group(1).strip()
        eng_part = paren_match.group(2).strip().lower()
        # Try English part first (more specific)
        if eng_part in ANALYTE_ALIASES:
            return ANALYTE_ALIASES[eng_part]
        # Then try Thai part
        if thai_part in ANALYTE_ALIASES:
            return ANALYTE_ALIASES[thai_part]

    # ── Phase 14.9: Thai text with colon separator ──
    # Pattern: "ครีอะตินีน:" or "ครีอะตินีน :"
    colon_stripped = re.sub(r"\s*:\s*$", "", name_clean)
    if colon_stripped != name_clean and colon_stripped in ANALYTE_ALIASES:
        return ANALYTE_ALIASES[colon_stripped]

    return None


def _parse_value(text: str) -> Optional[float]:
    """แปลง string เป็น float อย่างปลอดภัย

    จุลภาคมีสองความหมาย ต้องแยกให้ถูก มิฉะนั้นค่าจะผิดเป็นพันเท่า:

      คั่นหลักพัน   "250,000"  -> 250000.0   (ตามด้วยเลข 3 หลักพอดี)
      ทศนิยมยุโรป   "5,2"      -> 5.2        (ตามด้วยเลข 1-2 หลัก)

    เดิมแปลงจุลภาคเป็นจุดเสมอ ทำให้ "250,000" กลายเป็น 250.0
    เกล็ดเลือด 250,000 (ค่าปกติ) จึงถูกอ่านเป็น 250 ซึ่งต่ำวิกฤต
    และ "5,600" (เม็ดเลือดขาวปกติ) กลายเป็น 5.6 ซึ่งต่ำวิกฤตเช่นกัน
    ผลคือแจ้งเตือนเท็จกับผู้ป่วยทุกคนที่ตรวจ CBC
    """
    text = text.strip()

    # จุลภาคคั่นหลักพัน: มีเลข 1-3 หลักนำ แล้วตามด้วย ",nnn" อย่างน้อยหนึ่งชุด
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        text = text.replace(",", "")
    else:
        # ที่เหลือถือเป็นทศนิยมแบบยุโรป
        text = text.replace(",", ".")

    # Handle common OCR artifacts
    text = text.replace("O", "0").replace("o", "0")  # OCR O→0
    text = text.replace("l", "1").replace("I", "1")  # OCR l→1 only for standalone

    try:
        return float(text)
    except ValueError:
        return None


# flag ที่ห้องแล็บใส่ไว้ข้างค่า — ไม่ใช่หน่วย
_LAB_FLAG_TOKENS = frozenset({
    "h", "l", "hh", "ll", "high", "low", "n", "normal", "abn", "a",
})


def _clean_unit_token(unit: str) -> str:
    """กรองสิ่งที่ "ไม่ใช่หน่วย" ออก คืนค่าว่างถ้าไม่ใช่หน่วยจริง

    ทำไมต้องมี: กลุ่ม unit ของ TABULAR_RE ยอมรับทั้งตัวอักษรและตัวเลข
    จึงไปคว้าสิ่งที่อยู่ถัดจากค่ามาเป็นหน่วยได้ สองกรณีที่พบจริง:

      "Creatinine  5.2      0.6-1.2"   → หน่วยกลายเป็น "0"  (เลขจากช่วงอ้างอิง)
      "Creatinine  5.2  H   mg/dL"     → หน่วยกลายเป็น "H"  (flag ของห้องแล็บ)

    หน่วยผิดมีผลจริง เพราะ validate_result() ใช้หน่วยตรวจความสมเหตุสมผล
    และหน่วยถูกแสดงในข้อความแจ้งเตือนที่ส่งถึงแพทย์
    """
    u = (unit or "").strip()
    if not u:
        return ""
    # ตัวเลขล้วน (รวมทศนิยม/ช่วง) ไม่ใช่หน่วย
    if re.fullmatch(r"[<>]?\d+(?:[.,]\d+)?(?:\s*-\s*\d+(?:[.,]\d+)?)?", u):
        return ""
    if u.lower() in _LAB_FLAG_TOKENS:
        return ""
    return u


def _normalize_unit(unit: str) -> str:
    """ปรับรูปแบบหน่วยให้เป็นมาตรฐาน."""
    unit = unit.strip()
    # Normalize case for common units
    replacements = {
        "mg/dl": "mg/dL",
        "g/dl": "g/dL",
        "mmol/l": "mmol/L",
        "meq/l": "mEq/L",
        "ml/min/1.73m2": "mL/min/1.73m2",
        "ml/min": "mL/min",
        "pg/ml": "pg/mL",
        "ng/ml": "ng/mL",
    }
    return replacements.get(unit.lower(), unit)


# ชื่อ-ค่า-หน่วย-ช่วงอ้างอิง บนบรรทัดเดียว โดย "ไม่บังคับ" ว่าต้องมีหน่วย
#
# ต่างจาก TABULAR_RE ตรงที่หน่วยเป็นตัวเลือก และยอมรับตัวคั่นหลายแบบ
# ชื่อห้ามมีตัวเลข เพื่อไม่ให้ไปคาบเกี่ยวกับค่าหรือช่วงอ้างอิง
_GENERIC_LINE_RE = re.compile(
    r"^[\s|]*"                                  # ขอบซ้าย อาจมี | ของตาราง
    r"(?P<name>[ก-๙A-Za-z][ก-๙A-Za-z\s()/-]{1,39}?)"   # ชื่อ ไม่มีตัวเลข/จุด
    # ตัวคั่น: ช่องว่าง, : = |, หรือ "จุดไข่ปลา" ที่ใบแล็บใช้ลากจากชื่อไปค่า
    # เช่น "Hemoglobin .............. 10.5 g/dL"
    r"\s*(?:\.{2,}|[:=|])?\s*"
    r"(?P<value>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)"  # ค่า (มีจุลภาคได้)
    r"(?P<rest>.*)$"                            # ที่เหลือ: หน่วย/ช่วงอ้างอิง/flag
)

# ช่วงอ้างอิงที่พบท้ายบรรทัด เช่น (0.6-1.2) หรือ 0.6 - 1.2 หรือ <200
_REF_RE = re.compile(
    r"\(?\s*(?P<ref>[<>]?\s*\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?)?)\s*\)?\s*$"
)


def _parse_generic_line(line: str):
    """อ่าน 'ชื่อ ค่า [หน่วย] [ช่วงอ้างอิง]' จากหนึ่งบรรทัด

    คืน (ชื่อมาตรฐาน, ค่า, หน่วย, ช่วงอ้างอิง) หรือ None ถ้าบรรทัดนี้ไม่ใช่ผลตรวจ

    หน่วยเป็นตัวเลือก — ใบแล็บจำนวนมากวางหน่วยไว้ที่หัวคอลัมน์
    หรือไม่ระบุเลย ซึ่งเดิมทำให้ทั้งบรรทัดถูกข้าม

    ความปลอดภัย: คืนค่าเฉพาะเมื่อชื่ออยู่ในรายการ analyte ที่รู้จัก
    """
    if not line or not line.strip():
        return None
    # ตัวคั่นตาราง | และ tab ให้เป็นช่องว่าง เพื่อให้รูปแบบเดียวกันหมด
    cleaned = line.replace("\t", " ").replace("|", " ")
    m = _GENERIC_LINE_RE.match(cleaned)
    if not m:
        return None

    canonical = _normalize_analyte(m.group("name"))
    if not canonical:
        return None

    value_f = _parse_value(m.group("value"))
    if value_f is None:
        return None

    rest = (m.group("rest") or "").strip()
    # ตัด flag H/L/High/Low ที่ห้องแล็บใส่ไว้ท้ายค่า
    rest = re.sub(r"^\s*(?:H|L|HH|LL|High|Low|\*)\b\s*", "", rest, flags=re.I)

    unit = ""
    unit_match = UNIT_RE.match(rest.strip())
    if unit_match:
        unit = _normalize_unit(_clean_unit_token(unit_match.group(0)))
        rest = rest.strip()[unit_match.end():]

    ref_range = ""
    ref_match = _REF_RE.search(rest.strip()) if rest.strip() else None
    if ref_match:
        ref_range = re.sub(r"\s+", "", ref_match.group("ref"))

    return canonical, value_f, unit, ref_range


# หน่วยที่นับ "ต่อไมโครลิตร" ตรงๆ — ต้องหารพันเพื่อให้ตรงกับเกณฑ์
_PER_UL_UNITS = frozenset({
    "/ul", "/µl", "cells/ul", "cells/µl", "/mm3", "/cumm", "/cu.mm",
})


def _to_count_primary_unit(analyte: str, value: float, unit: str):
    """แปลงค่านับเม็ดเลือดจาก /uL เป็นหน่วยพัน (x10^3/uL)

    คืน (ค่าใหม่, หน่วยใหม่) หรือ None ถ้าไม่ต้องแปลง

    แปลงเฉพาะรายการที่เกณฑ์ระบุ primary_unit เป็น x10^3/uL เท่านั้น
    และเฉพาะเมื่อหน่วยที่อ่านได้เป็นแบบต่อไมโครลิตรตรงๆ — ไม่เดาจาก
    ขนาดของตัวเลข เพราะการเดาจากขนาดจะพังเมื่อค่าผิดปกติมากจริงๆ
    """
    if not unit:
        return None
    try:
        from lab_rules import ANALYTE_SPECS
    except ImportError:
        return None
    spec = ANALYTE_SPECS.get(analyte)
    if spec is None or getattr(spec, "primary_unit", "") != "x10^3/uL":
        return None
    if unit.strip().lower().replace(" ", "") not in _PER_UL_UNITS:
        return None
    return value / 1000.0, spec.primary_unit


def _find_column_major_runs(text: str):
    """หาช่วงที่เป็น 'ชื่อรายการตรวจเรียงกัน แล้วค่าเรียงกัน'

    คืน [(รายชื่อมาตรฐาน, [(ค่า, หน่วย), ...]), ...]
    ผู้เรียกเป็นผู้ตัดสินว่าจะจับคู่หรือไม่เมื่อจำนวนไม่เท่ากัน

    ต้องมีอย่างน้อย 3 ชื่อติดกันจึงนับว่าเป็นรูปแบบคอลัมน์ —
    น้อยกว่านั้นมักเป็นเอกสารทั่วไปที่บังเอิญมีชื่อรายการตรวจติดกัน
    """
    MIN_RUN = 3
    lines = [ln.strip() for ln in text.split("\n")]
    runs = []
    i = 0
    while i < len(lines):
        if not lines[i]:
            i += 1
            continue
        # เก็บชื่อที่เรียงติดกัน (ข้ามบรรทัดว่างได้)
        names, j = [], i
        while j < len(lines):
            if not lines[j]:
                j += 1
                continue
            canonical = _normalize_analyte(lines[j])
            if not canonical:
                break
            names.append(canonical)
            j += 1
        if len(names) < MIN_RUN:
            i = j + 1 if j == i else j
            continue

        # เก็บค่าที่เรียงติดกันถัดจากชุดชื่อ
        values = []
        while j < len(lines):
            if not lines[j]:
                j += 1
                continue
            m = re.match(
                r"^(?P<v>\d+(?:[.,]\d+)?)\s*(?P<u>[A-Za-z%/^µ]\S*)?$",
                lines[j],
            )
            if not m:
                break
            v = _parse_value(m.group("v"))
            if v is None:
                break
            values.append(
                (v, _normalize_unit(_clean_unit_token(m.group("u") or "")))
            )
            j += 1

        if values:
            runs.append((names, values))
        i = j if j > i else i + 1
    return runs


def extract_lab_results_from_text(
    text: str,
    source_document_id: str,
    extraction_method: str = ExtractionMethod.REGEX_PDF_TEXT.value,
) -> LabReport:
    """ดึงผลแล็บจากข้อความ.

    รองรับ 2 รูปแบบ:
    1. Tabular — แต่ละแถวมี analyte, value, unit, reference บนบรรทัดเดียว
    2. Per-line — pypdf อ่าน PDF column แยกบรรทัด:
       "Creatinine\\n5.2\\nmg/dL\\n0.6-1.2\\n"

    Args:
        text: ข้อความจาก PDF หรือ OCR
        source_document_id: doc_id ของเอกสารต้นทาง
        extraction_method: วิธีที่ใช้ดึงข้อมูล

    Returns:
        LabReport (status = pending_review เสมอ)
    """
    results: list[LabResult] = []
    warnings: list[str] = []
    seen_analytes: set[str] = set()  # ป้องกัน duplicate

    specimen_date = extract_specimen_date(text)

    # Strategy 1: Try per-line extraction (common with pypdf)
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        canonical = _normalize_analyte(line)

        # ─ กันการจับคู่ผิดเมื่อชื่อรายการตรวจเรียงติดกันเป็นชุด ─
        #
        # pypdf อ่าน PDF แบบคอลัมน์มักพ่น "ชื่อทั้งหมด" ก่อน แล้วค่อยพ่น
        # "ค่าทั้งหมด" การมองไปข้างหน้า 4 บรรทัดจึงจับชื่อตัวสุดท้ายของชุด
        # คู่กับค่าตัวแรกของอีกชุด ซึ่งเป็นคนละรายการกัน
        #
        # ตัวอย่างจริงที่วัดได้: ชื่อ 11 รายการเรียงกัน ตามด้วยค่า 11 ตัว
        # ผลคือได้ "eGFR = 5.2" ทั้งที่ 5.2 เป็นค่าของ Creatinine
        # eGFR 5.2 อ่านว่าไตวายระยะสุดท้าย — แจ้งเตือนเท็จที่อันตราย
        # และยังบังชื่ออื่นไม่ให้ถูกจับคู่ถูกต้องด้วย
        #
        # ถ้าบรรทัดก่อนหน้า "หรือ" บรรทัดถัดไปเป็นชื่อรายการตรวจอีก
        # แปลว่าอยู่ในชุดชื่อที่เรียงติดกัน ให้ strategy คอลัมน์จัดการแทน
        #
        # ต้องดูทั้งสองทาง: ชื่อ "ตัวสุดท้าย" ของชุดมีค่าตามหลังพอดี
        # (เพราะชุดค่าเริ่มตรงนั้น) การดูแต่บรรทัดถัดไปจึงไม่พอ
        # นี่คือกรณี eGFR ที่ถูกจับคู่กับค่าของ Creatinine
        def _is_name_at(rng):
            for _k in rng:
                _nxt = lines[_k].strip()
                if not _nxt:
                    continue
                return bool(_normalize_analyte(_nxt))
            return False

        if canonical and (
            _is_name_at(range(i + 1, min(i + 3, len(lines))))
            or _is_name_at(range(i - 1, max(i - 3, -1), -1))
        ):
            i += 1
            continue

        if canonical and canonical not in seen_analytes:
            # Found an analyte name — look ahead for value, unit, reference
            value_f = None
            unit = ""
            ref_range = ""

            # Search next lines for value, unit, reference
            lookahead = min(i + 4, len(lines))
            for j in range(i + 1, lookahead):
                next_line = lines[j].strip()
                if not next_line:
                    continue

                # Try to parse as value
                if value_f is None:
                    parsed = _parse_value(next_line)
                    if parsed is not None:
                        value_f = parsed
                        continue
                    # PDF/OCR บางฉบับวาง "ค่า" กับ "หน่วย" ไว้บรรทัดเดียวกัน
                    # ใต้ชื่อรายการ เช่น  "Creatinine\n5.2 mg/dL"
                    # เดิมบรรทัดนี้ parse เป็นตัวเลขไม่ได้จึงถูกข้ามทั้งคู่
                    combo = re.match(
                        r"^(?P<v>\d+(?:[.,]\d+)?)\s+(?P<u>\S+)", next_line
                    )
                    if combo:
                        parsed = _parse_value(combo.group("v"))
                        if parsed is not None:
                            value_f = parsed
                            if not unit:
                                unit = _normalize_unit(
                                    _clean_unit_token(combo.group("u"))
                                )
                            continue

                # Try to parse as unit
                if value_f is not None and not unit:
                    unit_match = UNIT_RE.match(next_line)
                    if unit_match:
                        unit = _normalize_unit(next_line)
                        continue

                # Try to parse as reference range
                if value_f is not None and not ref_range:
                    if re.match(r"[<>]?\d+\.?\d*(?:\s*-\s*\d+\.?\d*)?$", next_line):
                        ref_range = next_line
                        continue

                # If it's another analyte name, stop
                if _normalize_analyte(next_line):
                    break

            if value_f is not None:
                # Use rule engine reference if not found in text
                if not ref_range:
                    ref_range = get_reference_range(canonical)

                # Validate with rule engine
                flags = validate_result(canonical, value_f, unit)

                # Determine confidence
                confidence = 0.9  # base confidence for regex
                if "unit_mismatch" in flags:
                    confidence -= 0.2
                if "physiologic_impossible" in flags:
                    confidence = 0.3  # likely extraction error
                if not unit:
                    confidence -= 0.1

                result = LabResult(
                    analyte=canonical,
                    value=value_f,
                    unit=unit,
                    reference_range=ref_range,
                    confidence=round(confidence, 2),
                    extraction_method=extraction_method,
                    flags=flags,
                    raw_text=line,
                )
                results.append(result)
                seen_analytes.add(canonical)

        i += 1

    # Strategy 2: If per-line found few results, try tabular (English)
    if len(results) < 3:
        for match in TABULAR_RE.finditer(text):
            name = match.group("name")
            canonical = _normalize_analyte(name)
            if canonical and canonical not in seen_analytes:
                value_f = _parse_value(match.group("value"))
                if value_f is None:
                    continue

                unit = _normalize_unit(_clean_unit_token(match.group("unit") or ""))
                ref_range = match.group("ref") or get_reference_range(canonical)

                flags = validate_result(canonical, value_f, unit)
                confidence = 0.85
                if "unit_mismatch" in flags:
                    confidence -= 0.2
                if "physiologic_impossible" in flags:
                    confidence = 0.3

                result = LabResult(
                    analyte=canonical,
                    value=value_f,
                    unit=unit,
                    reference_range=ref_range,
                    confidence=round(confidence, 2),
                    extraction_method=extraction_method,
                    flags=flags,
                    raw_text=match.group(0).strip(),
                )
                results.append(result)
                seen_analytes.add(canonical)

    # Strategy 3 (Phase 14.9/14.11): Try Thai+colon tabular pattern
    # Phase 14.11: Removed _has_thai guard — TABULAR_THAI_RE also handles
    # English colon format (e.g., "Creatinine: 5.2 mg/dL") since its name
    # group accepts [ก-๙A-Za-z/ ()]. Allowlist check via _normalize_analyte()
    # prevents false positives from non-analyte text.
    if len(results) < 3:
        for match in TABULAR_THAI_RE.finditer(text):
            name = match.group("name")
            canonical = _normalize_analyte(name)
            if canonical and canonical not in seen_analytes:
                value_f = _parse_value(match.group("value"))
                if value_f is None:
                    continue

                unit = _normalize_unit(_clean_unit_token(match.group("unit") or ""))
                ref_range = match.group("ref") or get_reference_range(canonical)

                flags = validate_result(canonical, value_f, unit)
                confidence = 0.80  # slightly lower for Thai OCR
                if "unit_mismatch" in flags:
                    confidence -= 0.2
                if "physiologic_impossible" in flags:
                    confidence = 0.3

                result = LabResult(
                    analyte=canonical,
                    value=value_f,
                    unit=unit,
                    reference_range=ref_range,
                    confidence=round(confidence, 2),
                    extraction_method=extraction_method,
                    flags=flags,
                    raw_text=match.group(0).strip(),
                )
                results.append(result)
                seen_analytes.add(canonical)

    # ── Strategy 3.5: รูปแบบคอลัมน์ (ชื่อทั้งชุด แล้วค่าทั้งชุด) ──
    #
    # pypdf อ่าน PDF ที่จัดเป็นคอลัมน์มักพ่นชื่อรายการตรวจทั้งหมดก่อน
    # แล้วค่อยพ่นค่าทั้งหมดตามลำดับเดียวกัน:
    #
    #     Creatinine        5.2
    #     BUN          →    45
    #     Potassium         6.1
    #
    # กลายเป็น "Creatinine\nBUN\nPotassium\n5.2\n45\n6.1"
    #
    # จับคู่ตามลำดับได้ "ก็ต่อเมื่อจำนวนชื่อเท่ากับจำนวนค่าพอดี"
    # ถ้าไม่เท่ากันแปลว่าอ่านมาไม่ครบหรือมีอย่างอื่นปน การจับคู่จะเลื่อน
    # ทั้งแถวและได้ค่าผิดทุกตัว — ในงานนี้ "ไม่ได้ค่า" ปลอดภัยกว่า
    # "ได้ค่าผิด" เพราะค่าผิดนำไปสู่การแจ้งเตือนเท็จหรือบังค่าวิกฤตจริง
    for name_run, value_run in _find_column_major_runs(text):
        if len(name_run) != len(value_run):
            warnings.append(
                f"พบรูปแบบคอลัมน์ที่จำนวนชื่อ ({len(name_run)}) "
                f"ไม่เท่าจำนวนค่า ({len(value_run)}) — ไม่จับคู่ให้"
            )
            continue
        for canonical, (value_f, unit) in zip(name_run, value_run):
            if canonical in seen_analytes:
                continue
            flags = validate_result(canonical, value_f, unit)
            confidence = 0.70  # ต่ำกว่าแบบบรรทัดเดียว เพราะอาศัยลำดับ
            if not unit:
                confidence -= 0.05
            if "unit_mismatch" in flags:
                confidence -= 0.20
            if "physiologic_impossible" in flags:
                confidence = 0.3
            results.append(LabResult(
                analyte=canonical,
                value=value_f,
                unit=unit,
                reference_range=get_reference_range(canonical),
                confidence=round(confidence, 2),
                extraction_method=extraction_method,
                flags=flags,
                raw_text="(column-major)",
            ))
            seen_analytes.add(canonical)

    # ── Strategy 4: ตัวอ่านแบบทั่วไป (ทำงานเสมอ) ──
    #
    # ทำไมต้องมี: สาม strategy ข้างบน "บังคับว่าต้องมีหน่วย" ต่อท้ายค่า
    # แต่ใบแล็บจริงจำนวนมากไม่เป็นแบบนั้น — วัดจากรูปแบบที่พบจริง 10 แบบ
    # มี 5 แบบที่อ่านไม่ออกเลย:
    #   - ไม่มีหน่วยเลย                      "Creatinine   5.2"
    #   - ช่วงอ้างอิงในวงเล็บ                "Creatinine  5.2  (0.6-1.2)"
    #   - ชื่อกับค่าอยู่คนละบรรทัด            "Creatinine\n5.2 mg/dL"
    #   - ใช้ = คั่น                         "Creatinine = 5.2 mg/dL"
    #   - ตารางคั่นด้วย | (พบบ่อยมากใน OCR)  "Creatinine | 5.2 | mg/dL"
    #
    # ทำไมทำงานเสมอ ไม่ใช่เฉพาะตอนที่ strategy อื่นอ่านไม่ได้:
    #   การกั้นด้วย len(results) < 3 ทำให้พออ่านได้ 3 ค่าก็หยุดมองหาต่อ
    #   แต่ใบแล็บจริงมี 10-20 ค่า เอกสารที่ "อ่านสำเร็จ" จึงอาจได้ไม่ครบ
    #   seen_analytes กันค่าซ้ำอยู่แล้ว การให้ทำงานเสมอจึงเพิ่มได้อย่างเดียว
    #
    # ความปลอดภัย: ชื่อที่อ่านได้ต้องอยู่ใน ANALYTE_ALIASES เท่านั้น
    # (ผ่าน _normalize_analyte) ข้อความอื่นในเอกสารจึงไม่กลายเป็นผลตรวจ
    for line in text.split("\n"):
        parsed = _parse_generic_line(line)
        if parsed is None:
            continue
        canonical, value_f, unit, ref_range = parsed
        if canonical in seen_analytes:
            continue

        if not ref_range:
            ref_range = get_reference_range(canonical)
        flags = validate_result(canonical, value_f, unit)
        # ความเชื่อมั่นต่ำกว่า strategy อื่น เพราะรูปแบบกำกวมกว่า
        # ถ้าไม่มีหน่วยให้ตรวจ ยิ่งต้องให้คนดูซ้ำ
        confidence = 0.75
        if not unit:
            confidence -= 0.10
        if "unit_mismatch" in flags:
            confidence -= 0.20
        if "physiologic_impossible" in flags:
            confidence = 0.3

        results.append(LabResult(
            analyte=canonical,
            value=value_f,
            unit=unit,
            reference_range=ref_range,
            confidence=round(confidence, 2),
            extraction_method=extraction_method,
            flags=flags,
            raw_text=line.strip()[:120],
        ))
        seen_analytes.add(canonical)

    # ── ปรับหน่วยนับเม็ดเลือดให้ตรงกับที่เกณฑ์ใช้ ──
    #
    # ใบ CBC รายงานเป็นจำนวนเต็มต่อไมโครลิตร ("250,000 /uL") แต่เกณฑ์
    # ในระบบใช้หน่วยพัน (x10^3/uL) ค่าดิบจึงเกิน physiologic_max เสมอ
    # แล้วถูกตั้งธงว่า "เป็นไปไม่ได้" และความเชื่อมั่นตกเหลือ 0.3
    #
    # ผลที่อันตราย: เกล็ดเลือด 30,000 ซึ่งต่ำวิกฤตจริง ถูกอ่านเป็น 30000
    # แล้วถูกตีว่าเป็นค่าอ่านผิด จึงไม่ถูกแจ้งเตือน — ค่าวิกฤตจริงหายไป
    #
    # แปลงที่นี่จุดเดียวหลังทุก strategy แล้วคำนวณธงใหม่ เพื่อไม่ต้อง
    # แก้ซ้ำทุกจุดที่สร้างผลลัพธ์
    for r in results:
        converted = _to_count_primary_unit(r.analyte, r.value, r.unit)
        if converted is None:
            continue
        r.value, r.unit = converted
        r.flags = validate_result(r.analyte, r.value, r.unit)
        r.confidence = 0.80
        if "unit_mismatch" in r.flags:
            r.confidence -= 0.20
        if "physiologic_impossible" in r.flags:
            r.confidence = 0.3
        r.confidence = round(r.confidence, 2)

    # Generate warnings
    if not results:
        warnings.append("ไม่พบผลตรวจ Lab ในเอกสาร")
    else:
        # Check for critical values
        for r in results:
            if any("critical" in f for f in r.flags):
                warnings.append(
                    f"⚠️ {r.analyte} = {r.value} {r.unit} — ค่าวิกฤต!"
                )
            if "physiologic_impossible" in r.flags:
                warnings.append(
                    f"⚠️ {r.analyte} = {r.value} {r.unit} — "
                    f"ค่าเป็นไปไม่ได้ ตรวจสอบการอ่านซ้ำ"
                )
            if "unit_mismatch" in r.flags:
                warnings.append(
                    f"⚠️ {r.analyte} หน่วย '{r.unit}' ไม่ตรงกับที่คาดหวัง"
                )

    if specimen_date == "unknown":
        warnings.append("ไม่พบวันที่เก็บตัวอย่างในเอกสาร")

    report = LabReport(
        source_document_id=source_document_id,
        specimen_date=specimen_date,
        extraction_method=extraction_method,
        results=results,
        status=ReviewStatus.PENDING_REVIEW.value,
        warnings=warnings,
    )

    logger.info(
        f"Lab extraction: doc={source_document_id[:8]}, "
        f"results={len(results)}, warnings={len(warnings)}"
    )

    return report


def extract_lab_from_file(
    filepath: str,
    source_document_id: str,
    extracted_text: str = "",
) -> LabReport:
    """ดึงผลแล็บจากไฟล์ (PDF หรือรูปภาพ).

    ใช้ extracted_text ถ้ามี (จาก process_file_async ที่ extract ไว้แล้ว)
    ไม่เรียก extract ซ้ำเพื่อประหยัด memory

    Args:
        filepath: path ของไฟล์ (ใช้ดูนามสกุลเท่านั้น)
        source_document_id: doc_id
        extracted_text: ข้อความที่ extract ไว้แล้ว (ถ้ามี)

    Returns:
        LabReport
    """
    import os
    ext = os.path.splitext(filepath)[1].lower()

    if extracted_text:
        method = ExtractionMethod.REGEX_PDF_TEXT.value
        if ext in (".jpg", ".jpeg", ".png"):
            method = ExtractionMethod.REGEX_OCR.value
        return extract_lab_results_from_text(
            extracted_text, source_document_id, method
        )

    # Fallback: extract text here (should rarely happen)
    # Import from server.py's functions
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(filepath)
            texts = []
            for page in reader.pages[:10]:
                t = page.extract_text() or ""
                texts.append(t.strip())
            text = "\n\n".join(t for t in texts if t)
            return extract_lab_results_from_text(
                text, source_document_id,
                ExtractionMethod.REGEX_PDF_TEXT.value,
            )
        elif ext in (".jpg", ".jpeg", ".png"):
            # Cannot OCR here without importing server.py
            return LabReport(
                source_document_id=source_document_id,
                specimen_date="unknown",
                extraction_method=ExtractionMethod.REGEX_OCR.value,
                warnings=["ไม่สามารถอ่านรูปภาพได้ — ต้องใช้ OCR จาก server"],
            )
    except Exception as e:
        logger.error(f"Lab extraction error: {type(e).__name__}")
        return LabReport(
            source_document_id=source_document_id,
            specimen_date="unknown",
            extraction_method="error",
            warnings=[f"เกิดข้อผิดพลาดในการอ่านเอกสาร: {type(e).__name__}"],
        )

    return LabReport(
        source_document_id=source_document_id,
        specimen_date="unknown",
        extraction_method="unsupported",
        warnings=[f"ไม่รองรับไฟล์นามสกุล {ext}"],
    )
