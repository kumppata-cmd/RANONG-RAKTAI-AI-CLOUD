#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clinical Analyzer — Kidney Clinical Intelligence Platform 2.0

Rule-based clinical analysis engine สำหรับผู้ป่วยโรคไตเรื้อรังและผู้ป่วยฟอกเลือด

ความสามารถ:
  1. CKD Staging — จัดระดับ CKD จาก eGFR (KDIGO 2012)
  2. Anemia Assessment — ประเมินภาวะโลหิตจางในผู้ป่วย CKD
  3. Mineral-Bone Assessment — ประเมิน CKD-MBD (Ca, PO4, PTH)
  4. Electrolyte Assessment — ประเมินอิเล็กโทรไลต์
  5. Dialysis Adequacy — ประเมิน Kt/V, URR
  6. Nutrition Assessment — ประเมินภาวะโภชนาการ (Albumin)
  7. Clinical Summary — สรุปผลวิเคราะห์ทั้งหมด

กฎความปลอดภัย:
  - Rule Engine เท่านั้น — ไม่ใช้ AI ภายนอก
  - ผลวิเคราะห์เป็น "ข้อมูลประกอบ" ไม่ใช่การวินิจฉัย
  - ต้องผ่าน Human Review ก่อนนำไปใช้ประกอบการรักษา
  - ห้ามสั่งยา เปลี่ยนยา หรือสั่งการรักษาโดยอัตโนมัติ
  - ไม่เก็บข้อมูลระบุตัวผู้ป่วย
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional

from lab_models import LabReport, LabResult, ReviewStatus


# ═══════════════════════════════════════════════════════════
# Enums & Constants
# ═══════════════════════════════════════════════════════════

class CKDStage(str, Enum):
    """CKD staging ตาม KDIGO 2012."""
    G1 = "G1"    # ≥90  — ไตทำงานปกติหรือเกือบปกติ
    G2 = "G2"    # 60-89 — ลดลงเล็กน้อย
    G3a = "G3a"  # 45-59 — ลดลงเล็กน้อยถึงปานกลาง
    G3b = "G3b"  # 30-44 — ลดลงปานกลางถึงรุนแรง
    G4 = "G4"    # 15-29 — ลดลงรุนแรง
    G5 = "G5"    # <15   — ไตวายระยะสุดท้าย
    UNKNOWN = "unknown"


class Severity(str, Enum):
    """ระดับความรุนแรงของ finding."""
    CRITICAL = "critical"      # ต้องดำเนินการทันที
    WARNING = "warning"        # ต้องติดตามใกล้ชิด
    MILD = "mild"              # ผิดปกติเล็กน้อย
    NORMAL = "normal"          # ปกติ
    INFO = "info"              # ข้อมูลเพิ่มเติม


class AssessmentArea(str, Enum):
    """หมวดการประเมิน."""
    CKD_STAGING = "ckd_staging"
    ANEMIA = "anemia"
    MINERAL_BONE = "mineral_bone"
    ELECTROLYTE = "electrolyte"
    DIALYSIS_ADEQUACY = "dialysis_adequacy"
    NUTRITION = "nutrition"
    GLYCEMIC = "glycemic"
    PROTEINURIA = "proteinuria"
    LIPID = "lipid"


# ═══════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════

@dataclass
class ClinicalFinding:
    """ผลการวิเคราะห์ทางคลินิกแต่ละรายการ.

    เป็นข้อมูลประกอบการตัดสินใจเท่านั้น
    ไม่ใช่การวินิจฉัยหรือสั่งการรักษา
    """
    area: str                  # AssessmentArea value
    finding: str               # คำอธิบายสิ่งที่พบ
    severity: str              # Severity value
    recommendation: str        # คำแนะนำ (ข้อมูลประกอบ ไม่ใช่คำสั่งการรักษา)
    based_on: list[str] = field(default_factory=list)  # analyte ที่ใช้ในการวิเคราะห์
    values: dict = field(default_factory=dict)  # ค่าที่ใช้ เช่น {"eGFR": 25.0}

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClinicalAnalysis:
    """ผลวิเคราะห์ทางคลินิกทั้งฉบับ.

    เชื่อมกับ LabReport ผ่าน source_document_id
    ไม่เก็บข้อมูลระบุตัวผู้ป่วย

    ผลวิเคราะห์ทั้งหมดเป็น "ข้อมูลประกอบ" เท่านั้น
    ต้องผ่าน Human Review ก่อนนำไปใช้
    """
    source_document_id: str
    specimen_date: str
    # ⚠️ ckd_stage เก็บ "หมวดค่า eGFR" (G1–G5) ไม่ใช่การวินิจฉัย CKD
    # ชื่อฟิลด์คงไว้เพื่อ backward compatibility — ข้อสรุปว่าเป็น CKD
    # หรือไม่ (พร้อมเหตุผลและสิ่งที่ยังขาด) อยู่ใน ckd_assessment
    ckd_stage: str = CKDStage.UNKNOWN.value
    ckd_assessment: dict = field(default_factory=dict)
    findings: list[ClinicalFinding] = field(default_factory=list)
    summary_text_th: str = ""   # สรุปภาษาไทย
    analyzed_at: str = ""
    analyzer_version: str = "2.0.0"
    disclaimer: str = (
        "ผลวิเคราะห์นี้เป็นข้อมูลประกอบการตัดสินใจเท่านั้น "
        "ไม่ใช่การวินิจฉัยโรค — ต้องผ่านการตรวจสอบโดยแพทย์ก่อนนำไปใช้"
    )

    def __post_init__(self):
        if not self.analyzed_at:
            self.analyzed_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "source_document_id": self.source_document_id,
            "specimen_date": self.specimen_date,
            "ckd_stage": self.ckd_stage,
            "findings": [f.to_dict() for f in self.findings],
            "summary_text_th": self.summary_text_th,
            "analyzed_at": self.analyzed_at,
            "analyzer_version": self.analyzer_version,
            "disclaimer": self.disclaimer,
            "finding_count": len(self.findings),
            "critical_count": sum(
                1 for f in self.findings
                if f.severity == Severity.CRITICAL.value
            ),
            "warning_count": sum(
                1 for f in self.findings
                if f.severity == Severity.WARNING.value
            ),
        }

    def has_critical(self) -> bool:
        return any(
            f.severity == Severity.CRITICAL.value for f in self.findings
        )


# ═══════════════════════════════════════════════════════════
# CKD Staging — KDIGO 2012
# ═══════════════════════════════════════════════════════════

def classify_egfr_category(egfr: float) -> CKDStage:
    """จัดหมวด eGFR (G1–G5) ตาม KDIGO

    ⚠️ สำคัญ: นี่คือ "หมวดของค่า eGFR" ไม่ใช่การวินิจฉัย CKD

    ค่า eGFR ตั้งแต่ 60 ขึ้นไป (G1/G2) ไม่ได้แปลว่าเป็นโรคไตเรื้อรัง
    ต้องมีหลักฐานความผิดปกติของไตร่วมด้วย เช่น albuminuria หรือ
    ความผิดปกติของโครงสร้างไต นานอย่างน้อย 3 เดือน
    ส่วน eGFR ต่ำกว่า 60 ก็ต้องประเมินความเรื้อรังเช่นกัน
    ไม่ควรวินิจฉัย CKD จากผลตรวจครั้งเดียว

    ใช้ assess_ckd() เมื่อต้องการข้อสรุปว่าเป็น CKD หรือไม่

    Args:
        egfr: eGFR ในหน่วย mL/min/1.73m²

    Returns:
        CKDStage enum value (หมวด eGFR)
    """
    if egfr >= 90:
        return CKDStage.G1
    elif egfr >= 60:
        return CKDStage.G2
    elif egfr >= 45:
        return CKDStage.G3a
    elif egfr >= 30:
        return CKDStage.G3b
    elif egfr >= 15:
        return CKDStage.G4
    else:
        return CKDStage.G5


def classify_ckd_stage(egfr: float) -> CKDStage:
    """DEPRECATED — ใช้ classify_egfr_category() หรือ assess_ckd() แทน

    ชื่อเดิมสื่อว่าเป็นการ "จัดระยะ CKD" จาก eGFR ค่าเดียว ซึ่งไม่ถูกต้อง
    ตาม KDIGO (ดู CLINICAL_SAFETY_GAP_ANALYSIS CS-6)
    คงไว้เพื่อ backward compatibility — พฤติกรรมเหมือน
    classify_egfr_category() ทุกประการ
    """
    return classify_egfr_category(egfr)


# ─────────────────────────────────────────────────────────────
# Albuminuria (UACR) — KDIGO A1/A2/A3
# ─────────────────────────────────────────────────────────────

class AlbuminuriaCategory(str, Enum):
    """หมวด albuminuria ตาม UACR (mg/g)."""
    A1 = "A1"   # <30    — ปกติถึงเพิ่มเล็กน้อย
    A2 = "A2"   # 30-299 — เพิ่มปานกลาง (microalbuminuria)
    A3 = "A3"   # ≥300   — เพิ่มรุนแรง (macroalbuminuria)
    UNKNOWN = "unknown"


ALBUMINURIA_DESCRIPTIONS = {
    AlbuminuriaCategory.A1: "ปกติถึงเพิ่มเล็กน้อย (UACR <30 mg/g)",
    AlbuminuriaCategory.A2: "เพิ่มปานกลาง (UACR 30–299 mg/g)",
    AlbuminuriaCategory.A3: "เพิ่มรุนแรง (UACR ≥300 mg/g)",
    AlbuminuriaCategory.UNKNOWN: "ไม่มีผล UACR",
}


def classify_albuminuria(uacr_mg_g: float | None) -> AlbuminuriaCategory:
    """จัดหมวด albuminuria จาก UACR (mg/g)."""
    if uacr_mg_g is None:
        return AlbuminuriaCategory.UNKNOWN
    try:
        v = float(uacr_mg_g)
    except (TypeError, ValueError):
        return AlbuminuriaCategory.UNKNOWN
    if v < 0:
        return AlbuminuriaCategory.UNKNOWN
    if v < 30:
        return AlbuminuriaCategory.A1
    if v < 300:
        return AlbuminuriaCategory.A2
    return AlbuminuriaCategory.A3


# ─────────────────────────────────────────────────────────────
# KDIGO risk heat map — eGFR category × albuminuria
# ─────────────────────────────────────────────────────────────

class KDIGORisk(str, Enum):
    """ระดับความเสี่ยงตามตาราง KDIGO."""
    LOW = "low"              # ต่ำ
    MODERATE = "moderate"    # ปานกลาง
    HIGH = "high"            # สูง
    VERY_HIGH = "very_high"  # สูงมาก
    UNKNOWN = "unknown"


KDIGO_RISK_LABELS_TH = {
    KDIGORisk.LOW: "ต่ำ",
    KDIGORisk.MODERATE: "ปานกลาง",
    KDIGORisk.HIGH: "สูง",
    KDIGORisk.VERY_HIGH: "สูงมาก",
    KDIGORisk.UNKNOWN: "ประเมินไม่ได้",
}

# ตาราง KDIGO: (eGFR category, albuminuria category) → ระดับความเสี่ยง
# หมายเหตุ G1/G2 + A1: ถ้าไม่มีหลักฐานความผิดปกติของไตอื่น ไม่จัดเป็น CKD
_KDIGO_RISK_MATRIX = {
    CKDStage.G1: {
        AlbuminuriaCategory.A1: KDIGORisk.LOW,
        AlbuminuriaCategory.A2: KDIGORisk.MODERATE,
        AlbuminuriaCategory.A3: KDIGORisk.HIGH,
    },
    CKDStage.G2: {
        AlbuminuriaCategory.A1: KDIGORisk.LOW,
        AlbuminuriaCategory.A2: KDIGORisk.MODERATE,
        AlbuminuriaCategory.A3: KDIGORisk.HIGH,
    },
    CKDStage.G3a: {
        AlbuminuriaCategory.A1: KDIGORisk.MODERATE,
        AlbuminuriaCategory.A2: KDIGORisk.HIGH,
        AlbuminuriaCategory.A3: KDIGORisk.VERY_HIGH,
    },
    CKDStage.G3b: {
        AlbuminuriaCategory.A1: KDIGORisk.HIGH,
        AlbuminuriaCategory.A2: KDIGORisk.VERY_HIGH,
        AlbuminuriaCategory.A3: KDIGORisk.VERY_HIGH,
    },
    CKDStage.G4: {
        AlbuminuriaCategory.A1: KDIGORisk.VERY_HIGH,
        AlbuminuriaCategory.A2: KDIGORisk.VERY_HIGH,
        AlbuminuriaCategory.A3: KDIGORisk.VERY_HIGH,
    },
    CKDStage.G5: {
        AlbuminuriaCategory.A1: KDIGORisk.VERY_HIGH,
        AlbuminuriaCategory.A2: KDIGORisk.VERY_HIGH,
        AlbuminuriaCategory.A3: KDIGORisk.VERY_HIGH,
    },
}


def kdigo_risk(egfr_category: CKDStage,
               albuminuria: AlbuminuriaCategory) -> KDIGORisk:
    """ระดับความเสี่ยงตามตาราง KDIGO (eGFR × albuminuria)

    คืน UNKNOWN เมื่อขาดข้อมูลด้านใดด้านหนึ่ง — ไม่เดา
    """
    row = _KDIGO_RISK_MATRIX.get(egfr_category)
    if row is None:
        return KDIGORisk.UNKNOWN
    return row.get(albuminuria, KDIGORisk.UNKNOWN)


# ─────────────────────────────────────────────────────────────
# การประเมิน CKD แบบครบเกณฑ์ KDIGO
# ─────────────────────────────────────────────────────────────

# ต้องมีความผิดปกติต่อเนื่องอย่างน้อย 3 เดือนจึงเข้าเกณฑ์ "เรื้อรัง"
CKD_CHRONICITY_DAYS = 90


@dataclass
class CKDAssessment:
    """ผลประเมิน CKD ตามเกณฑ์ KDIGO

    แยก "หมวดค่า eGFR" ออกจาก "การวินิจฉัย CKD" อย่างชัดเจน
    เพราะสองอย่างนี้ไม่เหมือนกัน และการสับสนทำให้ผู้ที่ไตปกติ
    ถูกติดป้ายว่าเป็น CKD (ดู CS-6)

    is_ckd:
        True  — เข้าเกณฑ์ CKD
        False — ไม่เข้าเกณฑ์ (เช่น G1/G2 + A1 ไม่มีหลักฐานความผิดปกติ)
        None  — ข้อมูลไม่พอสรุป (เช่น มีผลตรวจครั้งเดียว)
    """
    egfr: float | None = None
    egfr_category: CKDStage = CKDStage.UNKNOWN
    albuminuria_category: AlbuminuriaCategory = AlbuminuriaCategory.UNKNOWN
    uacr: float | None = None
    chronicity_met: bool = False
    chronicity_days: int = 0
    has_damage_marker: bool = False
    is_ckd: bool | None = None
    risk: KDIGORisk = KDIGORisk.UNKNOWN
    missing: list[str] = field(default_factory=list)
    reason_th: str = ""

    @property
    def stage_label_th(self) -> str:
        """ป้ายที่ปลอดภัยสำหรับแสดงผล

        ถ้ายังสรุปไม่ได้ว่าเป็น CKD ต้องไม่เรียกว่า "CKD Stage"
        """
        cat = self.egfr_category.value
        if self.is_ckd is True:
            if self.albuminuria_category != AlbuminuriaCategory.UNKNOWN:
                return f"CKD {cat}{self.albuminuria_category.value}"
            return f"CKD {cat}"
        if self.is_ckd is False:
            return f"eGFR category {cat} (ไม่เข้าเกณฑ์ CKD)"
        return f"eGFR category {cat} (ยังสรุปไม่ได้)"

    def to_dict(self) -> dict:
        return {
            "egfr": self.egfr,
            "egfr_category": self.egfr_category.value,
            "albuminuria_category": self.albuminuria_category.value,
            "uacr": self.uacr,
            "chronicity_met": self.chronicity_met,
            "chronicity_days": self.chronicity_days,
            "has_damage_marker": self.has_damage_marker,
            "is_ckd": self.is_ckd,
            "risk": self.risk.value,
            "missing": list(self.missing),
            "reason_th": self.reason_th,
            "stage_label_th": self.stage_label_th,
        }


def assess_ckd(
    egfr: float | None,
    uacr: float | None = None,
    prior_egfr_dates: list[tuple[float, datetime]] | None = None,
    has_structural_abnormality: bool = False,
    damage_marker_since: datetime | None = None,
    now: datetime | None = None,
) -> CKDAssessment:
    """ประเมิน CKD ตามเกณฑ์ KDIGO — ไม่วินิจฉัยจากผลตรวจครั้งเดียว

    Args:
        egfr: eGFR ปัจจุบัน (mL/min/1.73m²)
        uacr: UACR ปัจจุบัน (mg/g) — ใช้จัดหมวด albuminuria
        prior_egfr_dates: ผล eGFR ก่อนหน้า [(ค่า, วันที่), ...]
            ใช้พิสูจน์ความเรื้อรัง ≥3 เดือน
        has_structural_abnormality: มีความผิดปกติของโครงสร้างไต
        damage_marker_since: วันที่เริ่มพบหลักฐานความผิดปกติของไต
        now: เวลาอ้างอิง (สำหรับทดสอบ)

    Returns:
        CKDAssessment — ระบุชัดว่าสรุปได้หรือไม่ และขาดอะไร
    """
    now = now or datetime.now()
    a = CKDAssessment(egfr=egfr, uacr=uacr)

    if egfr is None:
        a.missing.append("eGFR")
        a.reason_th = "ไม่มีค่า eGFR — ประเมินไม่ได้"
        return a

    a.egfr_category = classify_egfr_category(egfr)
    a.albuminuria_category = classify_albuminuria(uacr)
    a.risk = kdigo_risk(a.egfr_category, a.albuminuria_category)

    # ── หลักฐานความผิดปกติของไต (damage marker) ──
    a.has_damage_marker = bool(
        has_structural_abnormality
        or a.albuminuria_category in (
            AlbuminuriaCategory.A2, AlbuminuriaCategory.A3
        )
    )

    # ── ความเรื้อรัง: ต้องมีหลักฐานต่อเนื่อง ≥ 3 เดือน ──
    span_days = 0
    low_egfr_now = egfr < 60
    for prior_value, prior_date in (prior_egfr_dates or []):
        try:
            days = (now - prior_date).days
        except TypeError:
            continue
        if days < CKD_CHRONICITY_DAYS:
            continue
        # ความผิดปกติต้องคงอยู่ในอดีตด้วย ไม่ใช่แค่มีผลเก่า
        if low_egfr_now and prior_value < 60:
            span_days = max(span_days, days)
        elif not low_egfr_now and a.has_damage_marker:
            span_days = max(span_days, days)

    if damage_marker_since is not None:
        try:
            marker_days = (now - damage_marker_since).days
            if marker_days >= CKD_CHRONICITY_DAYS:
                span_days = max(span_days, marker_days)
        except TypeError:
            pass

    a.chronicity_days = span_days
    a.chronicity_met = span_days >= CKD_CHRONICITY_DAYS

    # ── สรุป ──
    if a.egfr_category in (CKDStage.G1, CKDStage.G2):
        # eGFR ≥60: ต้องมีหลักฐานความผิดปกติของไตจึงจะเป็น CKD
        if not a.has_damage_marker:
            if a.albuminuria_category == AlbuminuriaCategory.UNKNOWN:
                a.is_ckd = None
                a.missing.append("UACR")
                a.reason_th = (
                    f"eGFR {egfr:.0f} อยู่ในหมวด {a.egfr_category.value} "
                    "ซึ่งยังไม่บอกว่าเป็นโรคไตเรื้อรัง "
                    "ต้องมีผล UACR หรือหลักฐานความผิดปกติของไตประกอบ"
                )
            else:
                a.is_ckd = False
                a.reason_th = (
                    f"eGFR {egfr:.0f} ({a.egfr_category.value}) ร่วมกับ "
                    f"{a.albuminuria_category.value} และไม่พบหลักฐาน"
                    "ความผิดปกติของไต — ไม่เข้าเกณฑ์ CKD"
                )
            return a

        # มี damage marker → ต้องพิสูจน์ความเรื้อรังด้วย
        if a.chronicity_met:
            a.is_ckd = True
            a.reason_th = (
                f"{a.egfr_category.value}{a.albuminuria_category.value}: "
                f"มีหลักฐานความผิดปกติของไตต่อเนื่อง "
                f"{a.chronicity_days} วัน (≥90 วัน)"
            )
        else:
            a.is_ckd = None
            a.missing.append("ผลติดตาม ≥90 วัน")
            a.reason_th = (
                f"พบความผิดปกติ ({a.albuminuria_category.value}) "
                f"แต่ยังไม่มีหลักฐานว่าเป็นต่อเนื่อง ≥90 วัน "
                "ไม่ควรวินิจฉัย CKD จากผลตรวจครั้งเดียว"
            )
        return a

    # eGFR <60 (G3a ขึ้นไป) — ยังต้องประเมินความเรื้อรังเช่นกัน
    if a.chronicity_met:
        a.is_ckd = True
        a.reason_th = (
            f"eGFR {egfr:.0f} ({a.egfr_category.value}) ต่ำกว่า 60 "
            f"ต่อเนื่อง {a.chronicity_days} วัน (≥90 วัน)"
        )
    else:
        a.is_ckd = None
        a.missing.append("ผลติดตาม ≥90 วัน")
        a.reason_th = (
            f"eGFR {egfr:.0f} ต่ำกว่า 60 แต่ยังไม่มีผลยืนยันว่าต่ำ"
            "ต่อเนื่อง ≥90 วัน — ต้องแยกภาวะไตบาดเจ็บเฉียบพลัน (AKI) "
            "ก่อน ไม่ควรวินิจฉัย CKD จากผลตรวจครั้งเดียว"
        )
    return a


CKD_STAGE_DESCRIPTIONS = {
    CKDStage.G1: "ไตทำงานปกติหรือเกือบปกติ (eGFR ≥90)",
    CKDStage.G2: "การทำงานของไตลดลงเล็กน้อย (eGFR 60-89)",
    CKDStage.G3a: "การทำงานของไตลดลงเล็กน้อยถึงปานกลาง (eGFR 45-59)",
    CKDStage.G3b: "การทำงานของไตลดลงปานกลางถึงรุนแรง (eGFR 30-44)",
    CKDStage.G4: "การทำงานของไตลดลงรุนแรง (eGFR 15-29)",
    CKDStage.G5: "ไตวายระยะสุดท้าย (eGFR <15)",
    CKDStage.UNKNOWN: "ไม่สามารถประเมินได้",
}

CKD_STAGE_RECOMMENDATIONS = {
    CKDStage.G1: "ติดตามผลเป็นระยะ ควบคุมปัจจัยเสี่ยง",
    CKDStage.G2: "ติดตาม eGFR ทุก 6-12 เดือน ควบคุมความดันโลหิตและน้ำตาล",
    CKDStage.G3a: "ติดตาม eGFR ทุก 3-6 เดือน ปรึกษาอายุรแพทย์โรคไต",
    CKDStage.G3b: "ติดตาม eGFR ทุก 3 เดือน เตรียมพร้อมสำหรับการรักษาทดแทนไต",
    CKDStage.G4: "ติดตามอย่างใกล้ชิด เตรียมทางเลือกบำบัดทดแทนไต (ฟอกเลือด/ย้ายไต)",
    CKDStage.G5: "พิจารณาเริ่มบำบัดทดแทนไต ติดตามอาการยูรีเมีย",
}


def _assess_ckd(results: dict[str, LabResult]) -> list[ClinicalFinding]:
    """ประเมินหมวด eGFR และ CKD จากผล Lab ชุดเดียว

    [CS-6] ผล Lab ชุดเดียวพิสูจน์ความเรื้อรังไม่ได้ จึงรายงานเป็น
    "หมวด eGFR" พร้อมระบุสิ่งที่ยังขาด ไม่ประกาศว่าเป็น CKD Stage
    """
    findings = []

    egfr_result = results.get("eGFR")
    cr_result = results.get("Creatinine")
    bun_result = results.get("BUN")
    uacr_result = results.get("UACR")

    if egfr_result:
        stage = classify_egfr_category(egfr_result.value)
        desc = CKD_STAGE_DESCRIPTIONS.get(stage, "")
        rec = CKD_STAGE_RECOMMENDATIONS.get(stage, "")

        assessment = assess_ckd(
            egfr=egfr_result.value,
            uacr=uacr_result.value if uacr_result else None,
        )

        severity = Severity.NORMAL
        if stage in (CKDStage.G4, CKDStage.G5):
            severity = Severity.CRITICAL
        elif stage in (CKDStage.G3a, CKDStage.G3b):
            severity = Severity.WARNING
        elif stage == CKDStage.G2:
            severity = Severity.MILD

        values = {"eGFR": egfr_result.value}
        based_on = ["eGFR"]

        if cr_result:
            values["Creatinine"] = cr_result.value
            based_on.append("Creatinine")
        if bun_result:
            values["BUN"] = bun_result.value
            based_on.append("BUN")
        if uacr_result:
            values["UACR"] = uacr_result.value
            based_on.append("UACR")

        # ข้อความต้องไม่ประกาศการวินิจฉัยจากผลครั้งเดียว
        finding_text = f"{assessment.stage_label_th}: {desc}"
        if assessment.reason_th:
            finding_text += f"\n{assessment.reason_th}"
        if assessment.risk != KDIGORisk.UNKNOWN:
            finding_text += (
                f"\nความเสี่ยงตาม KDIGO: "
                f"{KDIGO_RISK_LABELS_TH[assessment.risk]}"
            )
        if assessment.missing:
            finding_text += f"\nยังขาด: {', '.join(assessment.missing)}"

        findings.append(ClinicalFinding(
            area=AssessmentArea.CKD_STAGING.value,
            finding=finding_text,
            severity=severity.value,
            recommendation=rec,
            based_on=based_on,
            values=values,
        ))

    elif cr_result:
        # ไม่มี eGFR แต่มี Creatinine — ประเมินจาก Cr
        cr = cr_result.value
        severity = Severity.NORMAL
        finding = f"Creatinine {cr} mg/dL"
        rec = ""

        if cr > 10.0:
            severity = Severity.CRITICAL
            finding += " — สูงมาก (critical)"
            rec = "ปรึกษาแพทย์ทันที อาจต้องพิจารณาบำบัดทดแทนไต"
        elif cr > 5.0:
            severity = Severity.WARNING
            finding += " — สูง ไตทำงานลดลงมาก"
            rec = "ติดตาม eGFR ประเมิน CKD stage อย่างละเอียด"
        elif cr > 1.2:
            severity = Severity.MILD
            finding += " — สูงกว่าปกติ"
            rec = "ตรวจ eGFR เพื่อประเมิน CKD stage"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.CKD_STAGING.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["Creatinine"],
                values={"Creatinine": cr},
            ))

    return findings


# ═══════════════════════════════════════════════════════════
# Anemia Assessment — KDIGO Anemia Guidelines
# ═══════════════════════════════════════════════════════════

def _assess_anemia(results: dict[str, LabResult]) -> list[ClinicalFinding]:
    """ประเมินภาวะโลหิตจางในผู้ป่วย CKD."""
    findings = []
    hb_result = results.get("Hemoglobin")
    hct_result = results.get("Hematocrit")
    ferritin_result = results.get("Ferritin")
    tsat_result = results.get("TSAT")

    if hb_result:
        hb = hb_result.value
        severity = Severity.NORMAL
        finding = f"Hemoglobin {hb} g/dL"
        rec = ""
        based_on = ["Hemoglobin"]
        values = {"Hemoglobin": hb}

        if hct_result:
            values["Hematocrit"] = hct_result.value
            based_on.append("Hematocrit")

        if hb < 7.0:
            severity = Severity.CRITICAL
            finding += " — ภาวะโลหิตจางรุนแรง (critical)"
            rec = "พิจารณาให้เลือด ตรวจหาสาเหตุเลือดออก"
        elif hb < 10.0:
            severity = Severity.WARNING
            finding += " — ภาวะโลหิตจางปานกลาง"
            rec = "ประเมินสาเหตุ ตรวจ Iron studies (Ferritin, TSAT)"
        elif hb < 12.0:
            severity = Severity.MILD
            finding += " — ภาวะโลหิตจางเล็กน้อย"
            rec = "ติดตาม Hemoglobin และ Iron studies"
        elif hb > 13.0:
            # สำหรับผู้ป่วย CKD ที่ได้รับ ESA, Hb > 13 อาจสูงเกินไป
            severity = Severity.INFO
            finding += " — อยู่ในเกณฑ์ปกติ"
            rec = "หากได้รับ ESA พิจารณาปรับลดขนาดยาเมื่อ Hb >13"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.ANEMIA.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=based_on,
                values=values,
            ))

    # Iron assessment (Ferritin, TSAT, Iron, TIBC)
    iron_result = results.get("Iron")
    tibc_result = results.get("TIBC")

    if ferritin_result or tsat_result or iron_result or tibc_result:
        iron_finding_parts = []
        iron_values = {}
        iron_based_on = []
        severity = Severity.NORMAL
        rec = ""

        if ferritin_result:
            ferritin = ferritin_result.value
            iron_values["Ferritin"] = ferritin
            iron_based_on.append("Ferritin")
            iron_finding_parts.append(f"Ferritin {ferritin} ng/mL")

            if ferritin < 100:
                severity = Severity.WARNING
                rec = "ภาวะเหล็กสะสมต่ำ พิจารณาให้ IV Iron"
            elif ferritin > 800:
                severity = Severity.WARNING
                rec = "Ferritin สูง พิจารณาลดหรือหยุด IV Iron ชั่วคราว"

        if tsat_result:
            tsat = tsat_result.value
            iron_values["TSAT"] = tsat
            iron_based_on.append("TSAT")
            iron_finding_parts.append(f"TSAT {tsat}%")

            if tsat < 20:
                if severity.value != Severity.WARNING.value:
                    severity = Severity.WARNING
                rec = "TSAT ต่ำ มีภาวะ functional iron deficiency พิจารณาให้ IV Iron"
            elif tsat > 50:
                severity = Severity.MILD
                rec = "TSAT สูง ระวัง iron overload"

        # Phase B: Iron (Serum Iron)
        if iron_result:
            iron_val = iron_result.value
            iron_values["Iron"] = iron_val
            iron_based_on.append("Iron")
            iron_finding_parts.append(f"Iron {iron_val} µg/dL")

            if iron_val < 60:
                if severity.value != Severity.WARNING.value:
                    severity = Severity.WARNING
                rec = "เหล็กในเลือดต่ำ ประเมินร่วมกับ Ferritin/TSAT"

        # Phase B: TIBC
        if tibc_result:
            tibc = tibc_result.value
            iron_values["TIBC"] = tibc
            iron_based_on.append("TIBC")
            iron_finding_parts.append(f"TIBC {tibc} µg/dL")

            if tibc > 400:
                if severity.value != Severity.WARNING.value:
                    severity = Severity.MILD
                rec = "TIBC สูง บ่งชี้ iron deficiency"

        # Calculate TSAT from Iron/TIBC if not directly available
        if iron_result and tibc_result and not tsat_result:
            if tibc_result.value > 0:
                calc_tsat = (iron_result.value / tibc_result.value) * 100
                iron_values["TSAT_calculated"] = round(calc_tsat, 1)
                iron_finding_parts.append(f"TSAT (calculated) {calc_tsat:.1f}%")
                if calc_tsat < 20:
                    severity = Severity.WARNING
                    rec = "TSAT (คำนวณ) ต่ำ functional iron deficiency พิจารณา IV Iron"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.ANEMIA.value,
                finding=" / ".join(iron_finding_parts),
                severity=severity.value,
                recommendation=rec,
                based_on=iron_based_on,
                values=iron_values,
            ))

    return findings


# ═══════════════════════════════════════════════════════════
# Mineral-Bone Disease Assessment — KDIGO CKD-MBD
# ═══════════════════════════════════════════════════════════

def _assess_mineral_bone(results: dict[str, LabResult]) -> list[ClinicalFinding]:
    """ประเมิน CKD-MBD (Mineral and Bone Disorder)."""
    findings = []

    ca_result = results.get("Calcium")
    po4_result = results.get("Phosphate")
    pth_result = results.get("PTH")

    # Calcium
    if ca_result:
        ca = ca_result.value
        severity = Severity.NORMAL
        finding = f"Calcium {ca} mg/dL"
        rec = ""

        if ca < 7.0:
            severity = Severity.CRITICAL
            finding += " — ภาวะแคลเซียมต่ำรุนแรง"
            rec = "เสี่ยงต่อหัวใจเต้นผิดจังหวะ ต้องแก้ไขเร่งด่วน"
        elif ca < 8.5:
            severity = Severity.WARNING
            finding += " — แคลเซียมต่ำ"
            rec = "พิจารณาเสริม Calcium และ Active Vitamin D"
        elif ca > 10.5:
            severity = Severity.WARNING
            finding += " — แคลเซียมสูง"
            rec = "พิจารณาลด Calcium-based binder และ Active Vitamin D"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.MINERAL_BONE.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["Calcium"],
                values={"Calcium": ca},
            ))

    # Phosphate
    if po4_result:
        po4 = po4_result.value
        severity = Severity.NORMAL
        finding = f"Phosphate {po4} mg/dL"
        rec = ""

        if po4 > 6.5:
            severity = Severity.CRITICAL
            finding += " — ฟอสเฟตสูงมาก"
            rec = "เสี่ยงต่อ vascular calcification ปรับ phosphate binder และจำกัดอาหาร"
        elif po4 > 4.5:
            severity = Severity.WARNING
            finding += " — ฟอสเฟตสูง"
            rec = "ปรับ phosphate binder จำกัดอาหารที่มีฟอสเฟตสูง"
        elif po4 < 2.5:
            severity = Severity.MILD
            finding += " — ฟอสเฟตต่ำ"
            rec = "ตรวจสอบการใช้ phosphate binder"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.MINERAL_BONE.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["Phosphate"],
                values={"Phosphate": po4},
            ))

    # PTH
    if pth_result:
        pth = pth_result.value
        severity = Severity.NORMAL
        finding = f"PTH {pth} pg/mL"
        rec = ""

        # KDIGO target สำหรับ CKD G5D: 2-9 เท่าของค่าปกติบน (65)
        # = 130-585 pg/mL
        if pth > 600:
            severity = Severity.WARNING
            finding += " — PTH สูงมาก (secondary hyperparathyroidism)"
            rec = "พิจารณา Active Vitamin D หรือ Calcimimetics"
        elif pth > 300:
            severity = Severity.MILD
            finding += " — PTH สูง"
            rec = "ติดตาม PTH ทุก 3 เดือน พิจารณาปรับ Vitamin D"
        elif pth < 100:
            # สำหรับผู้ป่วยฟอกเลือด PTH ที่ต่ำเกินไปอาจบ่งชี้ adynamic bone
            severity = Severity.INFO
            finding += " — PTH ค่อนข้างต่ำ"
            rec = "ระวัง adynamic bone disease ในผู้ป่วยฟอกเลือด"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.MINERAL_BONE.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["PTH"],
                values={"PTH": pth},
            ))

    # Ca x PO4 product
    if ca_result and po4_result:
        ca_po4 = ca_result.value * po4_result.value
        if ca_po4 > 55:
            findings.append(ClinicalFinding(
                area=AssessmentArea.MINERAL_BONE.value,
                finding=f"Ca×PO₄ product = {ca_po4:.1f} (>55)",
                severity=Severity.CRITICAL.value,
                recommendation="เสี่ยง metastatic calcification สูง ต้องลด Ca หรือ PO₄ ลง",
                based_on=["Calcium", "Phosphate"],
                values={
                    "Calcium": ca_result.value,
                    "Phosphate": po4_result.value,
                    "CaxPO4": round(ca_po4, 1),
                },
            ))

    return findings


# ═══════════════════════════════════════════════════════════
# Electrolyte Assessment
# ═══════════════════════════════════════════════════════════

def _assess_electrolytes(results: dict[str, LabResult]) -> list[ClinicalFinding]:
    """ประเมินอิเล็กโทรไลต์."""
    findings = []

    # Potassium — สำคัญมากสำหรับผู้ป่วย CKD
    k_result = results.get("Potassium")
    if k_result:
        k = k_result.value
        severity = Severity.NORMAL
        finding = f"Potassium {k} mmol/L"
        rec = ""

        if k > 6.5:
            severity = Severity.CRITICAL
            finding += " — โพแทสเซียมสูงมาก"
            rec = "เสี่ยงหัวใจเต้นผิดจังหวะ ต้องรักษาเร่งด่วน ตรวจ EKG"
        elif k > 5.5:
            severity = Severity.WARNING
            finding += " — โพแทสเซียมสูง"
            rec = "จำกัดอาหาร K สูง ตรวจสอบยาที่เพิ่ม K (ACEI/ARB/Spironolactone)"
        elif k > 5.0:
            severity = Severity.MILD
            finding += " — โพแทสเซียมค่อนข้างสูง"
            rec = "ติดตาม จำกัดอาหาร K สูง"
        elif k < 3.0:
            severity = Severity.CRITICAL
            finding += " — โพแทสเซียมต่ำมาก"
            rec = "เสี่ยงหัวใจเต้นผิดจังหวะ ต้องเสริมโพแทสเซียม"
        elif k < 3.5:
            severity = Severity.WARNING
            finding += " — โพแทสเซียมต่ำ"
            rec = "ประเมินสาเหตุ พิจารณาเสริมโพแทสเซียม"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.ELECTROLYTE.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["Potassium"],
                values={"Potassium": k},
            ))

    # Sodium
    na_result = results.get("Sodium")
    if na_result:
        na = na_result.value
        severity = Severity.NORMAL
        finding = f"Sodium {na} mmol/L"
        rec = ""

        if na < 125:
            severity = Severity.CRITICAL
            finding += " — ภาวะโซเดียมต่ำรุนแรง"
            rec = "ต้องแก้ไขอย่างระมัดระวัง เสี่ยง osmotic demyelination"
        elif na < 136:
            severity = Severity.MILD
            finding += " — โซเดียมต่ำ"
            rec = "ประเมินสาเหตุ ตรวจสอบน้ำส่วนเกิน"
        elif na > 150:
            severity = Severity.CRITICAL
            finding += " — ภาวะโซเดียมสูงรุนแรง"
            rec = "ประเมินภาวะขาดน้ำ แก้ไขอย่างค่อยเป็นค่อยไป"
        elif na > 145:
            severity = Severity.MILD
            finding += " — โซเดียมสูง"
            rec = "ตรวจสอบปริมาณน้ำที่ดื่ม"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.ELECTROLYTE.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["Sodium"],
                values={"Sodium": na},
            ))

    # Bicarbonate — metabolic acidosis ใน CKD
    hco3_result = results.get("Bicarbonate")
    if hco3_result:
        hco3 = hco3_result.value
        severity = Severity.NORMAL
        finding = f"Bicarbonate {hco3} mmol/L"
        rec = ""

        if hco3 < 15:
            severity = Severity.CRITICAL
            finding += " — metabolic acidosis รุนแรง"
            rec = "ต้องแก้ไขเร่งด่วน พิจารณา sodium bicarbonate"
        elif hco3 < 22:
            severity = Severity.WARNING
            finding += " — metabolic acidosis"
            rec = "พิจารณาให้ sodium bicarbonate เสริม"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.ELECTROLYTE.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["Bicarbonate"],
                values={"Bicarbonate": hco3},
            ))

    return findings


# ═══════════════════════════════════════════════════════════
# Dialysis Adequacy Assessment
# ═══════════════════════════════════════════════════════════

def _assess_dialysis(results: dict[str, LabResult]) -> list[ClinicalFinding]:
    """ประเมินความเพียงพอของการฟอกเลือด."""
    findings = []

    ktv_result = results.get("Kt/V")
    if ktv_result:
        ktv = ktv_result.value
        severity = Severity.NORMAL
        finding = f"Kt/V = {ktv}"
        rec = ""

        if ktv < 1.0:
            severity = Severity.CRITICAL
            finding += " — ฟอกเลือดไม่เพียงพอ"
            rec = "ต้องเพิ่มเวลาฟอก/ขนาด dialyzer/blood flow rate (เป้าหมาย Kt/V ≥1.2)"
        elif ktv < 1.2:
            severity = Severity.WARNING
            finding += " — ต่ำกว่าเป้าหมาย"
            rec = "พิจารณาปรับ dialysis prescription (เป้าหมาย Kt/V ≥1.2)"
        elif ktv >= 1.2:
            severity = Severity.NORMAL
            finding += " — เพียงพอ (เป้าหมาย ≥1.2)"
            rec = "Dialysis adequacy อยู่ในเกณฑ์"

        findings.append(ClinicalFinding(
            area=AssessmentArea.DIALYSIS_ADEQUACY.value,
            finding=finding,
            severity=severity.value,
            recommendation=rec,
            based_on=["Kt/V"],
            values={"Kt/V": ktv},
        ))

    return findings


# ═══════════════════════════════════════════════════════════
# Nutrition Assessment
# ═══════════════════════════════════════════════════════════

def _assess_nutrition(results: dict[str, LabResult]) -> list[ClinicalFinding]:
    """ประเมินภาวะโภชนาการ."""
    findings = []

    alb_result = results.get("Albumin")
    if alb_result:
        alb = alb_result.value
        severity = Severity.NORMAL
        finding = f"Albumin {alb} g/dL"
        rec = ""

        if alb < 2.5:
            severity = Severity.CRITICAL
            finding += " — ภาวะทุพโภชนาการรุนแรง"
            rec = "ประเมินโภชนาการอย่างละเอียด พิจารณา nutritional supplement"
        elif alb < 3.5:
            severity = Severity.WARNING
            finding += " — Albumin ต่ำ (malnutrition/inflammation)"
            rec = "ประเมินภาวะโภชนาการ ตรวจ CRP เพื่อแยก inflammation"
        elif alb < 3.8:
            severity = Severity.MILD
            finding += " — Albumin ค่อนข้างต่ำ"
            rec = "ติดตามภาวะโภชนาการ"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.NUTRITION.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["Albumin"],
                values={"Albumin": alb},
            ))

    return findings


# ═══════════════════════════════════════════════════════════
# Glycemic Assessment
# ═══════════════════════════════════════════════════════════

def _assess_glycemic(results: dict[str, LabResult]) -> list[ClinicalFinding]:
    """ประเมินการควบคุมน้ำตาล."""
    findings = []

    fbs_result = results.get("FBS") or results.get("Glucose")
    if fbs_result:
        fbs = fbs_result.value
        severity = Severity.NORMAL
        analyte_name = "FBS" if results.get("FBS") else "Glucose"
        finding = f"{analyte_name} {fbs} mg/dL"
        rec = ""

        if fbs < 50:
            severity = Severity.CRITICAL
            finding += " — ภาวะน้ำตาลต่ำรุนแรง"
            rec = "ต้องแก้ไขทันที ให้น้ำตาลเร่งด่วน"
        elif fbs < 70:
            severity = Severity.WARNING
            finding += " — น้ำตาลต่ำ"
            rec = "ตรวจสอบยาเบาหวาน อาจต้องปรับลดขนาด"
        elif fbs > 300:
            severity = Severity.CRITICAL
            finding += " — น้ำตาลสูงมาก"
            rec = "ปรับยาเบาหวาน ตรวจหาภาวะ DKA/HHS"
        elif fbs > 180:
            severity = Severity.WARNING
            finding += " — น้ำตาลสูง"
            rec = "ปรับยาเบาหวาน ตรวจ HbA1c"
        elif fbs > 100:
            severity = Severity.MILD
            finding += " — น้ำตาลสูงกว่าปกติเล็กน้อย"
            rec = "ติดตามน้ำตาล ปรับอาหาร"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.GLYCEMIC.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=[analyte_name],
                values={analyte_name: fbs},
            ))

    # ── Phase B: HbA1c assessment ──
    hba1c_result = results.get("HbA1c")
    if hba1c_result:
        hba1c = hba1c_result.value
        severity = Severity.NORMAL
        finding = f"HbA1c {hba1c}%"
        rec = ""

        # ⚠️ HbA1c อาจไม่แม่นยำในผู้ป่วย CKD เนื่องจาก anemia, ESA, uremia
        if hba1c > 10.0:
            severity = Severity.CRITICAL
            finding += " — ควบคุมน้ำตาลไม่ดี (poorly controlled)"
            rec = "ปรับยาเบาหวานเร่งด่วน ส่งต่อแพทย์เบาหวาน"
        elif hba1c > 8.0:
            severity = Severity.WARNING
            finding += " — น้ำตาลสูง ควรปรับยา"
            rec = "ปรับยาเบาหวาน เป้าหมาย HbA1c <7% (อาจผ่อนปรนในผู้ป่วย CKD)"
        elif hba1c > 7.0:
            severity = Severity.MILD
            finding += " — สูงกว่าเป้าหมายเล็กน้อย"
            rec = "ติดตาม ปรับอาหาร/ยา เป้าหมาย <7%"
        elif hba1c < 5.0:
            severity = Severity.MILD
            finding += " — ต่ำ อาจบ่งบอก anemia หรือ hemolysis"
            rec = "ตรวจ Hemoglobin ร่วม อาจไม่แม่นยำในผู้ป่วย CKD ที่มีภาวะซีด"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.GLYCEMIC.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["HbA1c"],
                values={"HbA1c": hba1c},
            ))

    return findings


# ═══════════════════════════════════════════════════════════
# Proteinuria Assessment — KDIGO Albuminuria Staging
# ═══════════════════════════════════════════════════════════

def _assess_proteinuria(results: dict[str, LabResult]) -> list[ClinicalFinding]:
    """ประเมินภาวะโปรตีนรั่วในปัสสาวะ (UPCR/UACR).

    KDIGO Albuminuria Staging:
      A1: <30 mg/g — ปกติหรือเพิ่มเล็กน้อย
      A2: 30-300 mg/g — เพิ่มปานกลาง (microalbuminuria)
      A3: >300 mg/g — เพิ่มรุนแรง (macroalbuminuria)
    """
    findings = []

    # UACR — ตัวชี้วัดหลักสำหรับ CKD progression
    uacr_result = results.get("UACR")
    if uacr_result:
        uacr = uacr_result.value
        severity = Severity.NORMAL
        finding = f"UACR {uacr} mg/g"
        rec = ""

        if uacr > 2000:
            severity = Severity.CRITICAL
            finding += " — โปรตีนรั่วรุนแรงมาก (nephrotic range)"
            rec = "ส่งต่อแพทย์โรคไต ประเมิน nephrotic syndrome ปรับ ACEI/ARB"
        elif uacr > 300:
            severity = Severity.WARNING
            finding += " — A3 macroalbuminuria"
            rec = "ปรับ ACEI/ARB ให้ได้ขนาดสูงสุด ควบคุม BP <130/80 ติดตามทุก 3 เดือน"
        elif uacr > 30:
            severity = Severity.MILD
            finding += " — A2 microalbuminuria"
            rec = "เริ่มหรือปรับ ACEI/ARB ควบคุม BP และน้ำตาล ติดตามทุก 6 เดือน"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.PROTEINURIA.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["UACR"],
                values={"UACR": uacr},
            ))

    # UPCR — ตัวชี้วัดรอง
    upcr_result = results.get("UPCR")
    if upcr_result:
        upcr = upcr_result.value
        severity = Severity.NORMAL
        finding = f"UPCR {upcr} g/g"
        rec = ""

        if upcr > 3.5:
            severity = Severity.CRITICAL
            finding += " — nephrotic range proteinuria"
            rec = "ส่งต่อแพทย์โรคไต ประเมิน nephrotic syndrome"
        elif upcr > 1.0:
            severity = Severity.WARNING
            finding += " — โปรตีนรั่วปานกลาง-มาก"
            rec = "ปรับ ACEI/ARB ติดตามอย่างใกล้ชิด ตรวจ UACR ร่วม"
        elif upcr > 0.2:
            severity = Severity.MILD
            finding += " — โปรตีนรั่วเล็กน้อย"
            rec = "ติดตาม พิจารณา ACEI/ARB ถ้ายังไม่ได้ใช้"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.PROTEINURIA.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["UPCR"],
                values={"UPCR": upcr},
            ))

    return findings


# ═══════════════════════════════════════════════════════════
# Lipid Assessment — CKD Cardiovascular Risk
# ═══════════════════════════════════════════════════════════

def _assess_lipid(results: dict[str, LabResult]) -> list[ClinicalFinding]:
    """ประเมินไขมันในเลือด สำหรับผู้ป่วย CKD.

    ⚠️ ผู้ป่วย CKD มีความเสี่ยง cardiovascular สูง
    KDIGO แนะนำ statin ในผู้ป่วย CKD อายุ ≥50 ปี
    """
    findings = []

    tc_result = results.get("Total Cholesterol")
    ldl_result = results.get("LDL")
    hdl_result = results.get("HDL")
    tg_result = results.get("Triglycerides")

    # LDL — ตัวชี้วัดหลัก
    if ldl_result:
        ldl = ldl_result.value
        severity = Severity.NORMAL
        finding = f"LDL {ldl} mg/dL"
        rec = ""
        values = {"LDL": ldl}
        based_on = ["LDL"]

        if ldl > 190:
            severity = Severity.WARNING
            finding += " — สูงมาก"
            rec = "เริ่มหรือปรับ statin ผู้ป่วย CKD มีความเสี่ยง CV สูง"
        elif ldl > 130:
            severity = Severity.MILD
            finding += " — สูง"
            rec = "พิจารณา statin โดยเฉพาะผู้ป่วย CKD อายุ ≥50 ปี (KDIGO)"
        elif ldl > 100:
            severity = Severity.INFO
            finding += " — ค่อนข้างสูง"
            rec = "เป้าหมาย LDL <100 ในผู้ป่วย CKD (high CV risk)"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.LIPID.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=based_on,
                values=values,
            ))

    # Triglycerides
    if tg_result:
        tg = tg_result.value
        severity = Severity.NORMAL
        finding = f"Triglycerides {tg} mg/dL"
        rec = ""

        if tg > 500:
            severity = Severity.CRITICAL
            finding += " — สูงมาก เสี่ยงตับอ่อนอักเสบ"
            rec = "เสี่ยง acute pancreatitis พิจารณา fibrate/omega-3"
        elif tg > 200:
            severity = Severity.MILD
            finding += " — สูง"
            rec = "ปรับอาหาร ออกกำลังกาย ตรวจสอบสาเหตุ (DM, nephrotic)"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.LIPID.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["Triglycerides"],
                values={"Triglycerides": tg},
            ))

    # HDL — ต่ำ = เสี่ยง CV
    if hdl_result:
        hdl = hdl_result.value
        severity = Severity.NORMAL
        finding = f"HDL {hdl} mg/dL"
        rec = ""

        if hdl < 35:
            severity = Severity.WARNING
            finding += " — ต่ำมาก เสี่ยง CV สูง"
            rec = "ออกกำลังกาย ปรับอาหาร HDL ต่ำเพิ่มความเสี่ยงหลอดเลือดหัวใจ"
        elif hdl < 40:
            severity = Severity.MILD
            finding += " — ต่ำ"
            rec = "ปรับพฤติกรรม ออกกำลังกาย"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.LIPID.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["HDL"],
                values={"HDL": hdl},
            ))

    # Total Cholesterol summary (if no LDL available)
    if tc_result and not ldl_result:
        tc = tc_result.value
        severity = Severity.NORMAL
        finding = f"Total Cholesterol {tc} mg/dL"
        rec = ""

        if tc > 300:
            severity = Severity.WARNING
            finding += " — สูงมาก"
            rec = "ตรวจ LDL/HDL แยก พิจารณา statin"
        elif tc > 200:
            severity = Severity.MILD
            finding += " — สูง"
            rec = "ตรวจ LDL/HDL แยก ปรับอาหาร"

        if severity != Severity.NORMAL:
            findings.append(ClinicalFinding(
                area=AssessmentArea.LIPID.value,
                finding=finding,
                severity=severity.value,
                recommendation=rec,
                based_on=["Total Cholesterol"],
                values={"Total Cholesterol": tc},
            ))

    return findings


# ═══════════════════════════════════════════════════════════
# Summary Text Generator
# ═══════════════════════════════════════════════════════════

def _generate_summary(analysis: ClinicalAnalysis) -> str:
    """สร้างข้อความสรุปผลวิเคราะห์ภาษาไทย."""
    lines = []
    lines.append("🔬 ผลวิเคราะห์ทางคลินิก")
    lines.append(f"📅 วันที่ตัวอย่าง: {analysis.specimen_date}")

    if analysis.ckd_stage != CKDStage.UNKNOWN.value:
        stage_desc = CKD_STAGE_DESCRIPTIONS.get(
            CKDStage(analysis.ckd_stage), ""
        )
        lines.append(f"🏥 CKD Stage: {analysis.ckd_stage} — {stage_desc}")

    lines.append("")

    # Group findings by severity
    criticals = [f for f in analysis.findings
                 if f.severity == Severity.CRITICAL.value]
    warnings = [f for f in analysis.findings
                if f.severity == Severity.WARNING.value]
    milds = [f for f in analysis.findings
             if f.severity == Severity.MILD.value]
    infos = [f for f in analysis.findings
             if f.severity == Severity.INFO.value]

    if criticals:
        lines.append("🔴 ค่าวิกฤต:")
        for f in criticals:
            lines.append(f"  • {f.finding}")
            lines.append(f"    → {f.recommendation}")
        lines.append("")

    if warnings:
        lines.append("🟡 ต้องติดตาม:")
        for f in warnings:
            lines.append(f"  • {f.finding}")
            lines.append(f"    → {f.recommendation}")
        lines.append("")

    if milds:
        lines.append("🟢 ผิดปกติเล็กน้อย:")
        for f in milds:
            lines.append(f"  • {f.finding}")
        lines.append("")

    if infos:
        lines.append("ℹ️ ข้อมูลเพิ่มเติม:")
        for f in infos:
            lines.append(f"  • {f.finding}")
        lines.append("")

    if not analysis.findings:
        lines.append("✅ ผลตรวจอยู่ในเกณฑ์ปกติ")
        lines.append("")

    lines.append("⚠️ " + analysis.disclaimer)

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# Main Analysis Entry Point
# ═══════════════════════════════════════════════════════════

def analyze_lab_report(lab_report: LabReport) -> ClinicalAnalysis:
    """วิเคราะห์ผล Lab ด้วย rule-based clinical engine.

    Args:
        lab_report: LabReport จาก lab_extractor

    Returns:
        ClinicalAnalysis พร้อมผลวิเคราะห์ทั้งหมด

    กฎความปลอดภัย:
        - ใช้ deterministic rules เท่านั้น ไม่ใช้ AI ภายนอก
        - ผลวิเคราะห์เป็นข้อมูลประกอบ ไม่ใช่การวินิจฉัย
        - ต้องผ่าน Human Review ก่อนนำไปใช้
    """
    # สร้าง lookup dict: analyte name → LabResult
    results: dict[str, LabResult] = {}
    for r in lab_report.results:
        # เก็บผลแรกที่พบ (ป้องกัน duplicate)
        if r.analyte not in results:
            results[r.analyte] = r

    # สร้าง analysis
    analysis = ClinicalAnalysis(
        source_document_id=lab_report.source_document_id,
        specimen_date=lab_report.specimen_date,
    )

    # หมวด eGFR (ไม่ใช่การวินิจฉัย CKD — ดู CS-6)
    # ฟิลด์ ckd_stage เก็บ "หมวด eGFR" เพื่อคง backward compatibility
    # กับผู้ใช้เดิม ส่วนข้อสรุปว่าเป็น CKD หรือไม่อยู่ใน ckd_assessment
    egfr = results.get("eGFR")
    if egfr:
        analysis.ckd_stage = classify_egfr_category(egfr.value).value
        _uacr = results.get("UACR")
        analysis.ckd_assessment = assess_ckd(
            egfr=egfr.value,
            uacr=_uacr.value if _uacr else None,
        ).to_dict()

    # รัน assessments ทั้งหมด
    all_findings = []
    all_findings.extend(_assess_ckd(results))
    all_findings.extend(_assess_anemia(results))
    all_findings.extend(_assess_mineral_bone(results))
    all_findings.extend(_assess_electrolytes(results))
    all_findings.extend(_assess_dialysis(results))
    all_findings.extend(_assess_nutrition(results))
    all_findings.extend(_assess_glycemic(results))
    all_findings.extend(_assess_proteinuria(results))
    all_findings.extend(_assess_lipid(results))

    analysis.findings = all_findings

    # สร้างข้อความสรุป
    analysis.summary_text_th = _generate_summary(analysis)

    return analysis
