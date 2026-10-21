#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab Data Models — Kidney Clinical Intelligence Platform 2.0

Dataclasses สำหรับเก็บผลตรวจ Lab ที่ดึงจากเอกสาร
แยก patient identity ออกจากข้อมูลผลตรวจ

กฎความปลอดภัย:
  - LabResult เก็บเฉพาะค่าตรวจ ไม่เก็บข้อมูลระบุตัวบุคคล
  - LabReport เก็บ source_document_id แทน patient_id
  - status เริ่มต้นเป็น pending_review เสมอ
  - ห้ามสร้างค่าที่ไม่มีอยู่ในเอกสาร
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional


class ReviewStatus(str, Enum):
    """สถานะการตรวจสอบผลแล็บ."""
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class ExtractionMethod(str, Enum):
    """วิธีที่ใช้ในการดึงข้อมูล."""
    REGEX_PDF_TEXT = "regex_pdf_text"
    REGEX_OCR = "regex_ocr"
    MANUAL = "manual"


class RuleFlag(str, Enum):
    """ประเภท flag จาก Rule Engine."""
    CRITICAL_HIGH = "critical_high"
    CRITICAL_LOW = "critical_low"
    ABNORMAL_HIGH = "abnormal_high"
    ABNORMAL_LOW = "abnormal_low"
    UNIT_MISMATCH = "unit_mismatch"
    PHYSIOLOGIC_IMPOSSIBLE = "physiologic_impossible"
    LOW_CONFIDENCE = "low_confidence"


@dataclass
class LabResult:
    """ผลตรวจ Lab แต่ละรายการ.

    เก็บเฉพาะค่าตรวจ — ไม่เก็บข้อมูลที่ระบุตัวผู้ป่วย
    ค่าทุกตัวต้องมาจากเอกสารจริง ห้ามสร้างขึ้น
    """
    analyte: str              # ชื่อ analyte เช่น "Creatinine", "Potassium"
    value: float              # ค่าที่วัดได้
    unit: str                 # หน่วย เช่น "mg/dL", "mmol/L"
    reference_range: str      # ค่าปกติ เช่น "0.6-1.2"
    confidence: float         # ความมั่นใจ 0.0-1.0
    extraction_method: str    # วิธีดึงข้อมูล
    flags: list[str] = field(default_factory=list)  # RuleFlag values
    raw_text: str = ""        # ข้อความต้นฉบับที่ใช้ดึงค่า

    def to_dict(self) -> dict:
        """แปลงเป็น dict สำหรับ JSON serialization."""
        return asdict(self)


@dataclass
class LabReport:
    """รายงานผลแล็บทั้งฉบับ.

    เชื่อมกับเอกสารด้วย source_document_id เท่านั้น
    ไม่เก็บ patient_id หรือข้อมูลระบุตัวบุคคลใดๆ

    status เริ่มต้นเป็น pending_review เสมอ —
    ต้องผ่าน Human Review ก่อนนำไปใช้
    """
    source_document_id: str   # doc_id ของเอกสารต้นทาง
    specimen_date: str        # วันที่เก็บตัวอย่าง (จากเอกสาร หรือ "unknown")
    extraction_method: str    # วิธีดึงข้อมูลหลัก
    results: list[LabResult] = field(default_factory=list)
    status: str = ReviewStatus.PENDING_REVIEW.value
    extracted_at: str = ""    # ISO timestamp ที่ดึงข้อมูล
    reviewed_by: str = ""     # ผู้ตรวจสอบ (admin)
    reviewed_at: str = ""     # ISO timestamp ที่ตรวจสอบ
    review_notes: str = ""    # หมายเหตุจากผู้ตรวจสอบ
    warnings: list[str] = field(default_factory=list)  # คำเตือนจาก extraction

    def __post_init__(self):
        if not self.extracted_at:
            self.extracted_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        """แปลงเป็น dict สำหรับ JSON — พร้อมบันทึกลง GCS."""
        d = {
            "source_document_id": self.source_document_id,
            "specimen_date": self.specimen_date,
            "extraction_method": self.extraction_method,
            "status": self.status,
            "extracted_at": self.extracted_at,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "review_notes": self.review_notes,
            "warnings": self.warnings,
            "results": [r.to_dict() for r in self.results],
            "result_count": len(self.results),
        }
        return d

    def to_json(self) -> str:
        """แปลงเป็น JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> LabReport:
        """สร้าง LabReport จาก dict (เช่น จาก JSON ที่เก็บใน GCS)."""
        results = [
            LabResult(**r) for r in data.get("results", [])
        ]
        return cls(
            source_document_id=data["source_document_id"],
            specimen_date=data.get("specimen_date", "unknown"),
            extraction_method=data.get("extraction_method", "unknown"),
            results=results,
            status=data.get("status", ReviewStatus.PENDING_REVIEW.value),
            extracted_at=data.get("extracted_at", ""),
            reviewed_by=data.get("reviewed_by", ""),
            reviewed_at=data.get("reviewed_at", ""),
            review_notes=data.get("review_notes", ""),
            warnings=data.get("warnings", []),
        )

    def summary_text(self) -> str:
        """สร้างข้อความสรุปผลแล็บสำหรับแสดงผล."""
        lines = []
        lines.append(f"📋 ผลแล็บ — เอกสาร {self.source_document_id[:8]}")
        lines.append(f"📅 วันที่เก็บตัวอย่าง: {self.specimen_date}")
        lines.append(f"🔍 สถานะ: {self.status}")
        lines.append("")

        if not self.results:
            lines.append("❌ ไม่พบผลตรวจในเอกสาร")
            return "\n".join(lines)

        for r in self.results:
            flag_str = ""
            if r.flags:
                flag_icons = []
                for f in r.flags:
                    if "critical" in f:
                        flag_icons.append("🔴")
                    elif "abnormal" in f:
                        flag_icons.append("🟡")
                    elif "impossible" in f or "mismatch" in f:
                        flag_icons.append("⚠️")
                    else:
                        flag_icons.append("ℹ️")
                flag_str = " " + "".join(flag_icons)

            lines.append(
                f"  {r.analyte}: {r.value} {r.unit} "
                f"(ref: {r.reference_range}){flag_str}"
            )

        if self.warnings:
            lines.append("")
            lines.append("⚠️ คำเตือน:")
            for w in self.warnings:
                lines.append(f"  - {w}")

        if self.status == ReviewStatus.PENDING_REVIEW.value:
            lines.append("")
            lines.append("⏳ รอการตรวจสอบโดยแพทย์ก่อนนำไปใช้")

        return "\n".join(lines)
