#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR Clinical Safety — RANONG-RAKTAI-AI 2.0

การตรวจสอบคุณภาพ OCR extraction ของผลตรวจทางห้องปฏิบัติการ

กฎความปลอดภัย:
  - Low-confidence results ห้าม auto-approve
  - ต้องมี unit validation
  - ต้องตรวจสอบ specimen date
  - ตรวจจับ duplicate analyte
  - จัดการค่าที่กำกวม
  - อ้างอิง source page
  - Clinical plausibility checks
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional, Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# Confidence & Safety Thresholds
# ═══════════════════════════════════════════════════════════

# Configurable thresholds — ต้อง reviewed by qualified clinician
DEFAULT_THRESHOLDS = {
    "min_auto_confidence": 0.85,     # ต่ำกว่านี้ต้อง manual review
    "low_confidence": 0.70,          # ต่ำกว่านี้ต้อง source verification
    "critical_confidence": 0.50,     # ต่ำกว่านี้ ไม่ใช้ค่านี้เลย
    "max_specimen_age_days": 365,    # เก่าเกิน 1 ปี — ต้องตรวจสอบ
    "future_date_tolerance_hours": 24,  # อนุญาตวันที่อนาคต max 24 ชม.
}


class OcrFlag(str):
    """OCR quality flags."""
    LOW_CONFIDENCE = "low_confidence"
    VERY_LOW_CONFIDENCE = "very_low_confidence"
    UNIT_MISMATCH = "unit_mismatch"
    UNIT_MISSING = "unit_missing"
    IMPLAUSIBLE_VALUE = "implausible_value"
    DUPLICATE_ANALYTE = "duplicate_analyte"
    AMBIGUOUS_VALUE = "ambiguous_value"
    SPECIMEN_DATE_MISSING = "specimen_date_missing"
    SPECIMEN_DATE_FUTURE = "specimen_date_future"
    SPECIMEN_DATE_OLD = "specimen_date_old"
    MULTI_PAGE_MISMATCH = "multi_page_mismatch"
    SOURCE_VERIFICATION_NEEDED = "source_verification_needed"


@dataclass
class OcrValidationResult:
    """ผลการตรวจสอบ OCR quality."""
    analyte: str
    is_valid: bool = True
    confidence: float = 1.0
    flags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_page: int = 0            # หน้าที่พบ
    requires_review: bool = False    # ต้อง physician review
    requires_source_verification: bool = False  # ต้องดูต้นฉบับ
    suggested_value: float | None = None  # ค่าที่แนะนำ (ถ้ากำกวม)

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════
# Clinical Plausibility Ranges
# ═══════════════════════════════════════════════════════════

# ช่วงค่าที่ "เป็นไปได้" ทางคลินิก (กว้างกว่า reference range)
# ค่านอกช่วงนี้ = น่าจะ OCR error
PLAUSIBILITY_RANGES: dict[str, dict] = {
    "hemoglobin": {"min": 1.0, "max": 30.0, "units": ["g/dL", "g/dl"]},
    "hematocrit": {"min": 5.0, "max": 80.0, "units": ["%"]},
    "wbc": {"min": 0.1, "max": 500.0, "units": ["x10^3/uL", "x10³/µL", "cells/uL", "10^3/uL"]},
    "platelet": {"min": 1.0, "max": 2000.0, "units": ["x10^3/uL", "x10³/µL", "10^3/uL"]},
    "bun": {"min": 1.0, "max": 300.0, "units": ["mg/dL", "mg/dl"]},
    "creatinine": {"min": 0.1, "max": 50.0, "units": ["mg/dL", "mg/dl"]},
    "egfr": {"min": 1.0, "max": 200.0, "units": ["mL/min/1.73m²", "ml/min/1.73m2", "mL/min"]},
    "sodium": {"min": 100, "max": 200, "units": ["mEq/L", "meq/l", "mmol/L", "mmol/l"]},
    "potassium": {"min": 1.0, "max": 12.0, "units": ["mEq/L", "meq/l", "mmol/L", "mmol/l"]},
    "chloride": {"min": 60, "max": 150, "units": ["mEq/L", "meq/l", "mmol/L", "mmol/l"]},
    "bicarbonate": {"min": 1.0, "max": 60.0, "units": ["mEq/L", "meq/l", "mmol/L", "mmol/l"]},
    "calcium": {"min": 2.0, "max": 20.0, "units": ["mg/dL", "mg/dl"]},
    "phosphate": {"min": 0.5, "max": 20.0, "units": ["mg/dL", "mg/dl"]},
    "albumin": {"min": 0.5, "max": 8.0, "units": ["g/dL", "g/dl"]},
    "fbs": {"min": 10, "max": 1000, "units": ["mg/dL", "mg/dl"]},
    "glucose": {"min": 10, "max": 1000, "units": ["mg/dL", "mg/dl"]},
    "pth": {"min": 1.0, "max": 5000.0, "units": ["pg/mL", "pg/ml"]},
    "ferritin": {"min": 1.0, "max": 100000.0, "units": ["ng/mL", "ng/ml"]},
    "iron": {"min": 1.0, "max": 1000.0, "units": ["µg/dL", "ug/dL", "mcg/dL"]},
    "tibc": {"min": 50, "max": 1000, "units": ["µg/dL", "ug/dL", "mcg/dL"]},
    "tsat": {"min": 1.0, "max": 100.0, "units": ["%"]},
    "kt_v": {"min": 0.1, "max": 5.0, "units": [""]},
}

# Known unit aliases (case-insensitive normalization)
UNIT_ALIASES: dict[str, str] = {
    "g/dl": "g/dL",
    "mg/dl": "mg/dL",
    "meq/l": "mEq/L",
    "mmol/l": "mmol/L",
    "pg/ml": "pg/mL",
    "ng/ml": "ng/mL",
    "ml/min/1.73m2": "mL/min/1.73m²",
    "ml/min": "mL/min",
    "ug/dl": "µg/dL",
    "mcg/dl": "µg/dL",
    "cells/ul": "cells/uL",
    "x10^3/ul": "x10^3/uL",
    "10^3/ul": "x10^3/uL",
}


# ═══════════════════════════════════════════════════════════
# OCR Safety Validator
# ═══════════════════════════════════════════════════════════

class OcrSafetyValidator:
    """ตรวจสอบคุณภาพ OCR extraction.

    ใช้ก่อน physician review — flag ปัญหาให้แพทย์ทราบ
    """

    def __init__(self, thresholds: dict | None = None):
        self._thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    def normalize_unit(self, unit: str) -> str:
        """Normalize unit string."""
        if not unit:
            return ""
        return UNIT_ALIASES.get(unit.lower().strip(), unit.strip())

    def validate_result(self, analyte: str, value: float,
                         unit: str, confidence: float = 1.0,
                         source_page: int = 0) -> OcrValidationResult:
        """ตรวจสอบผลตรวจเดี่ยว."""
        result = OcrValidationResult(
            analyte=analyte,
            confidence=confidence,
            source_page=source_page,
        )

        # 1. Confidence check
        self._check_confidence(result, confidence)

        # 2. Unit validation
        self._check_unit(result, analyte, unit)

        # 3. Clinical plausibility
        self._check_plausibility(result, analyte, value, unit)

        # 4. Determine overall validity
        if result.requires_source_verification:
            result.is_valid = False
        if OcrFlag.IMPLAUSIBLE_VALUE in result.flags:
            result.is_valid = False
            result.requires_review = True
        if confidence < self._thresholds["critical_confidence"]:
            result.is_valid = False

        return result

    def validate_report(self, results: list[dict],
                         specimen_date: str | None = None,
                         total_pages: int = 1) -> dict:
        """ตรวจสอบ lab report ทั้งหมด.

        Returns dict with:
          - validations: list of OcrValidationResult
          - report_flags: list of report-level flags
          - requires_review: bool
          - summary: str
        """
        validations = []
        report_flags = []
        seen_analytes: dict[str, list[int]] = {}

        for r in results:
            analyte = r.get("analyte", "")
            value = r.get("value")
            unit = r.get("unit", "")
            confidence = r.get("confidence", 1.0)
            source_page = r.get("source_page", 0)

            if value is None:
                continue

            try:
                value = float(value)
            except (ValueError, TypeError):
                validations.append(OcrValidationResult(
                    analyte=analyte,
                    is_valid=False,
                    confidence=confidence,
                    flags=[OcrFlag.AMBIGUOUS_VALUE],
                    warnings=[f"ค่า '{r.get('value')}' ไม่สามารถแปลงเป็นตัวเลขได้"],
                    requires_review=True,
                ))
                continue

            validation = self.validate_result(
                analyte, value, unit, confidence, source_page
            )
            validations.append(validation)

            # Track for duplicate detection
            analyte_key = analyte.lower().strip()
            if analyte_key not in seen_analytes:
                seen_analytes[analyte_key] = []
            seen_analytes[analyte_key].append(len(validations) - 1)

        # Check duplicates
        for analyte_key, indices in seen_analytes.items():
            if len(indices) > 1:
                report_flags.append(OcrFlag.DUPLICATE_ANALYTE)
                for idx in indices:
                    validations[idx].flags.append(OcrFlag.DUPLICATE_ANALYTE)
                    validations[idx].warnings.append(
                        f"พบ {analyte_key} ซ้ำ {len(indices)} ครั้ง"
                    )
                    validations[idx].requires_review = True

        # Specimen date validation
        date_flags = self._check_specimen_date(specimen_date)
        report_flags.extend(date_flags)

        # Multi-page check
        if total_pages > 1:
            pages_with_results = set(
                v.source_page for v in validations if v.source_page > 0
            )
            if len(pages_with_results) > 1:
                report_flags.append(OcrFlag.MULTI_PAGE_MISMATCH)

        # Overall assessment
        any_review = any(v.requires_review for v in validations)
        any_source_verify = any(
            v.requires_source_verification for v in validations
        )
        any_invalid = any(not v.is_valid for v in validations)

        requires_review = any_review or len(report_flags) > 0

        # Summary
        total = len(validations)
        valid_count = sum(1 for v in validations if v.is_valid)
        review_count = sum(1 for v in validations if v.requires_review)

        summary_parts = [f"ตรวจสอบ {total} รายการ"]
        if valid_count == total:
            summary_parts.append("✅ ผ่านทุกรายการ")
        else:
            summary_parts.append(f"✅ ผ่าน {valid_count}")
            if review_count > 0:
                summary_parts.append(f"⚠️ ต้องตรวจสอบ {review_count}")
            invalid_count = total - valid_count
            if invalid_count > 0:
                summary_parts.append(f"❌ ไม่ผ่าน {invalid_count}")

        if any_source_verify:
            summary_parts.append("📋 ต้องตรวจสอบเอกสารต้นฉบับ")

        return {
            "validations": [v.to_dict() for v in validations],
            "report_flags": report_flags,
            "requires_review": requires_review,
            "requires_source_verification": any_source_verify,
            "summary": " | ".join(summary_parts),
            "stats": {
                "total": total,
                "valid": valid_count,
                "review_needed": review_count,
                "invalid": total - valid_count,
            },
        }

    def _check_confidence(self, result: OcrValidationResult,
                           confidence: float):
        """ตรวจสอบ OCR confidence level."""
        if confidence < self._thresholds["critical_confidence"]:
            result.flags.append(OcrFlag.VERY_LOW_CONFIDENCE)
            result.warnings.append(
                f"OCR confidence ต่ำมาก ({confidence:.0%}) — "
                f"ต้องตรวจสอบเอกสารต้นฉบับ"
            )
            result.requires_review = True
            result.requires_source_verification = True
        elif confidence < self._thresholds["low_confidence"]:
            result.flags.append(OcrFlag.LOW_CONFIDENCE)
            result.flags.append(OcrFlag.SOURCE_VERIFICATION_NEEDED)
            result.warnings.append(
                f"OCR confidence ต่ำ ({confidence:.0%}) — "
                f"กรุณาตรวจสอบกับเอกสารต้นฉบับ"
            )
            result.requires_review = True
            result.requires_source_verification = True
        elif confidence < self._thresholds["min_auto_confidence"]:
            result.flags.append(OcrFlag.LOW_CONFIDENCE)
            result.warnings.append(
                f"OCR confidence ({confidence:.0%}) ต่ำกว่าเกณฑ์ auto-approve"
            )
            result.requires_review = True

    def _check_unit(self, result: OcrValidationResult,
                     analyte: str, unit: str):
        """ตรวจสอบ unit."""
        analyte_lower = analyte.lower().strip()
        spec = PLAUSIBILITY_RANGES.get(analyte_lower)

        if not unit:
            result.flags.append(OcrFlag.UNIT_MISSING)
            result.warnings.append("ไม่พบหน่วย")
            result.requires_review = True
            return

        if spec and spec.get("units"):
            normalized = self.normalize_unit(unit)
            valid_units = [self.normalize_unit(u) for u in spec["units"]]
            if valid_units and normalized not in valid_units:
                result.flags.append(OcrFlag.UNIT_MISMATCH)
                result.warnings.append(
                    f"หน่วย '{unit}' ไม่ตรงกับที่คาดหวัง "
                    f"({', '.join(spec['units'][:3])})"
                )
                result.requires_review = True

    def _check_plausibility(self, result: OcrValidationResult,
                              analyte: str, value: float, unit: str):
        """ตรวจสอบ clinical plausibility."""
        analyte_lower = analyte.lower().strip()
        spec = PLAUSIBILITY_RANGES.get(analyte_lower)

        if spec is None:
            return  # ไม่มี spec สำหรับ analyte นี้

        if value < spec["min"] or value > spec["max"]:
            result.flags.append(OcrFlag.IMPLAUSIBLE_VALUE)
            result.warnings.append(
                f"ค่า {value} {unit} อยู่นอกช่วงที่เป็นไปได้ "
                f"({spec['min']}-{spec['max']}) — น่าจะเป็น OCR error"
            )
            result.requires_review = True
            result.requires_source_verification = True

    def _check_specimen_date(self, specimen_date: str | None) -> list[str]:
        """ตรวจสอบ specimen date."""
        flags = []

        if not specimen_date:
            flags.append(OcrFlag.SPECIMEN_DATE_MISSING)
            return flags

        try:
            date = datetime.fromisoformat(specimen_date.replace("Z", "+00:00"))
            now = datetime.now(date.tzinfo) if date.tzinfo else datetime.now()

            # Future date
            tolerance = timedelta(
                hours=self._thresholds["future_date_tolerance_hours"]
            )
            if date > now + tolerance:
                flags.append(OcrFlag.SPECIMEN_DATE_FUTURE)

            # Very old date
            max_age = timedelta(
                days=self._thresholds["max_specimen_age_days"]
            )
            if date < now - max_age:
                flags.append(OcrFlag.SPECIMEN_DATE_OLD)

        except (ValueError, TypeError):
            flags.append(OcrFlag.SPECIMEN_DATE_MISSING)

        return flags


def format_validation_summary(report: dict) -> str:
    """สร้างข้อความสรุป validation สำหรับ LINE/physician review."""
    lines = [report["summary"], ""]

    if report.get("requires_source_verification"):
        lines.append("📋 กรุณาตรวจสอบเอกสารต้นฉบับประกอบ")
        lines.append("")

    for v in report.get("validations", []):
        if v.get("warnings"):
            icon = "❌" if not v["is_valid"] else "⚠️"
            lines.append(f"{icon} {v['analyte']}:")
            for w in v["warnings"]:
                lines.append(f"  • {w}")

    if report.get("report_flags"):
        lines.append("")
        for f in report["report_flags"]:
            if f == OcrFlag.DUPLICATE_ANALYTE:
                lines.append("⚠️ พบรายการซ้ำ")
            elif f == OcrFlag.SPECIMEN_DATE_MISSING:
                lines.append("⚠️ ไม่พบวันที่เก็บตัวอย่าง")
            elif f == OcrFlag.SPECIMEN_DATE_FUTURE:
                lines.append("⚠️ วันที่เก็บตัวอย่างเป็นอนาคต")
            elif f == OcrFlag.SPECIMEN_DATE_OLD:
                lines.append("⚠️ วันที่เก็บตัวอย่างเก่ามาก")
            elif f == OcrFlag.MULTI_PAGE_MISMATCH:
                lines.append("ℹ️ ผลตรวจจากหลายหน้า")

    return "\n".join(lines)
