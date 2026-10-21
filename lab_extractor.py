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
    # BUN
    "bun": "BUN",
    "blood urea nitrogen": "BUN",
    "urea nitrogen": "BUN",
    # eGFR
    "egfr": "eGFR",
    "gfr": "eGFR",
    "estimated gfr": "eGFR",
    # Sodium
    "sodium": "Sodium",
    "sodium (na)": "Sodium",
    "na": "Sodium",
    "na+": "Sodium",
    # Potassium
    "potassium": "Potassium",
    "potassium (k)": "Potassium",
    "k": "Potassium",
    "k+": "Potassium",
    # Chloride
    "chloride": "Chloride",
    "chloride (cl)": "Chloride",
    "cl": "Chloride",
    "cl-": "Chloride",
    # Bicarbonate
    "bicarbonate": "Bicarbonate",
    "bicarbonate (hco3)": "Bicarbonate",
    "hco3": "Bicarbonate",
    "hco3-": "Bicarbonate",
    "co2": "Bicarbonate",
    "total co2": "Bicarbonate",
    "tco2": "Bicarbonate",
    # Calcium
    "calcium": "Calcium",
    "calcium (ca)": "Calcium",
    "ca": "Calcium",
    "total calcium": "Calcium",
    # Phosphate
    "phosphate": "Phosphate",
    "phosphate (po4)": "Phosphate",
    "phosphorus": "Phosphate",
    "po4": "Phosphate",
    "phos": "Phosphate",
    "inorganic phosphorus": "Phosphate",
    # Albumin
    "albumin": "Albumin",
    "alb": "Albumin",
    "serum albumin": "Albumin",
    # Glucose / FBS
    "fbs": "FBS",
    "fbs (glucose)": "FBS",
    "fasting blood sugar": "FBS",
    "fasting glucose": "FBS",
    "glucose": "Glucose",
    "blood glucose": "Glucose",
    # Hemoglobin
    "hemoglobin": "Hemoglobin",
    "hemoglobin (hb)": "Hemoglobin",
    "hb": "Hemoglobin",
    "hgb": "Hemoglobin",
    # Hematocrit
    "hematocrit": "Hematocrit",
    "hematocrit (hct)": "Hematocrit",
    "hct": "Hematocrit",
    # WBC
    "wbc": "WBC",
    "white blood cell": "WBC",
    "white blood cells": "WBC",
    "white cell count": "WBC",
    # Platelet
    "platelet": "Platelet",
    "platelets": "Platelet",
    "plt": "Platelet",
    "platelet count": "Platelet",
    # PTH
    "pth": "PTH",
    "parathyroid hormone": "PTH",
    "intact pth": "PTH",
    "ipth": "PTH",
    # Ferritin
    "ferritin": "Ferritin",
    "serum ferritin": "Ferritin",
    # TSAT
    "tsat": "TSAT",
    "transferrin saturation": "TSAT",
    "iron saturation": "TSAT",
    # Kt/V
    "kt/v": "Kt/V",
    "ktv": "Kt/V",
    "kt / v": "Kt/V",
}


# ═══════════════════════════════════════════════════════════
# Unit patterns
# ═══════════════════════════════════════════════════════════

UNIT_PATTERNS = [
    r"mg/[dD][lL]",
    r"g/[dD][lL]",
    r"mmol/[lL]",
    r"mEq/[lL]",
    r"mL/min(?:/1\.73m2)?",
    r"ml/min(?:/1\.73m2)?",
    r"x?10\^[39]/[uµ][lL]",
    r"cells/[uµ][lL]",
    r"pg/[mM][lL]",
    r"ng/[mM][lL]",
    r"[pnuµ]mol/[lL]",
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
TABULAR_RE = re.compile(
    r"(?P<name>[A-Za-z/\s()]+?)"       # analyte name
    r"\s+"
    r"(?P<value>\d+\.?\d*)"            # numeric value
    r"\s+"
    r"(?P<unit>[a-zA-Z%/^µu0-9]+(?:/[a-zA-Z0-9.µu^]+)*)"  # unit
    r"(?:\s+"
    r"(?P<ref>[<>]?\d+\.?\d*(?:\s*-\s*\d+\.?\d*)?))?",  # optional reference
    re.IGNORECASE,
)


def _normalize_analyte(name: str) -> Optional[str]:
    """แปลงชื่อ analyte เป็นชื่อมาตรฐาน."""
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

    return None


def _parse_value(text: str) -> Optional[float]:
    """แปลง string เป็น float อย่างปลอดภัย."""
    text = text.strip()
    # Handle common OCR artifacts
    text = text.replace(",", ".")  # European decimal
    text = text.replace("O", "0").replace("o", "0")  # OCR O→0
    text = text.replace("l", "1").replace("I", "1")  # OCR l→1 only for standalone

    try:
        return float(text)
    except ValueError:
        return None


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

    # Strategy 2: If per-line found few results, try tabular
    if len(results) < 3:
        for match in TABULAR_RE.finditer(text):
            name = match.group("name")
            canonical = _normalize_analyte(name)
            if canonical and canonical not in seen_analytes:
                value_f = _parse_value(match.group("value"))
                if value_f is None:
                    continue

                unit = _normalize_unit(match.group("unit") or "")
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
