#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Medication Review & Volume Assessment — Kidney Clinical Intelligence Platform 2.0

Rule-based medication screening and volume status assessment:
  1. Nephrotoxic Drug Screening — ตรวจสอบยาที่เป็นพิษต่อไต
  2. CKD Dose Adjustment Alerts — เตือนยาที่ต้องปรับขนาดตาม eGFR
  3. Drug-Lab Interaction Checks — ตรวจสอบปฏิกิริยายากับผลแล็บ
  4. Volume Status Assessment — ประเมินภาวะน้ำในร่างกาย
  5. Dry Weight Assessment — ประเมิน Dry Weight สำหรับผู้ป่วยฟอกเลือด

กฎความปลอดภัย:
  - Rule Engine เท่านั้น — ไม่ใช้ AI ภายนอก
  - ผลเป็น "ข้อมูลประกอบ" ไม่ใช่การสั่งยาหรือเปลี่ยนยา
  - ห้ามสั่งยา เปลี่ยนยา หรือสั่งการรักษาโดยอัตโนมัติ
  - ต้องผ่าน Human Review ก่อนนำไปใช้ประกอบการรักษา
  - ไม่เก็บข้อมูลระบุตัวผู้ป่วย
  - ใช้ข้อมูล DEMO/สังเคราะห์ในการทดสอบ
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ═══════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════

class AlertLevel(str, Enum):
    """ระดับความสำคัญของการแจ้งเตือนยา."""
    CRITICAL = "critical"    # ห้ามใช้ / อันตราย
    WARNING = "warning"      # ต้องระวัง / ปรับขนาด
    INFO = "info"            # ข้อมูลเพิ่มเติม


class VolumeStatus(str, Enum):
    """สถานะปริมาณน้ำในร่างกาย."""
    OVERLOADED = "overloaded"    # น้ำเกิน (fluid overload)
    EUVOLEMIC = "euvolemic"      # น้ำปกติ
    DEHYDRATED = "dehydrated"    # ขาดน้ำ
    UNKNOWN = "unknown"


class DrugCategory(str, Enum):
    """หมวดหมู่ยา."""
    NEPHROTOXIC = "nephrotoxic"
    DOSE_ADJUST = "dose_adjust"
    POTASSIUM_RAISING = "potassium_raising"
    POTASSIUM_LOWERING = "potassium_lowering"
    PHOSPHATE_BINDER = "phosphate_binder"
    ESA = "esa"  # Erythropoiesis-Stimulating Agent
    ANTICOAGULANT = "anticoagulant"
    ANTIHYPERTENSIVE = "antihypertensive"


# ═══════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════

@dataclass
class MedicationAlert:
    """การแจ้งเตือนเกี่ยวกับยา."""
    drug_name: str
    alert_level: str  # AlertLevel value
    category: str     # DrugCategory value
    message_th: str   # คำอธิบายภาษาไทย
    recommendation_th: str  # คำแนะนำภาษาไทย (ห้ามสั่งยา)

    def to_dict(self) -> dict:
        return {
            "drug_name": self.drug_name,
            "alert_level": self.alert_level,
            "category": self.category,
            "message_th": self.message_th,
            "recommendation_th": self.recommendation_th,
        }


@dataclass
class VolumeAssessment:
    """ผลประเมินภาวะน้ำในร่างกาย."""
    status: str  # VolumeStatus value
    indicators: list[str] = field(default_factory=list)
    pre_weight_kg: Optional[float] = None
    post_weight_kg: Optional[float] = None
    dry_weight_kg: Optional[float] = None
    interdialytic_weight_gain_kg: Optional[float] = None
    ultrafiltration_rate: Optional[float] = None  # mL/kg/hr

    def to_dict(self) -> dict:
        d: dict = {
            "status": self.status,
            "indicators": self.indicators,
        }
        if self.pre_weight_kg is not None:
            d["pre_weight_kg"] = self.pre_weight_kg
        if self.post_weight_kg is not None:
            d["post_weight_kg"] = self.post_weight_kg
        if self.dry_weight_kg is not None:
            d["dry_weight_kg"] = self.dry_weight_kg
        if self.interdialytic_weight_gain_kg is not None:
            d["interdialytic_weight_gain_kg"] = (
                self.interdialytic_weight_gain_kg
            )
        if self.ultrafiltration_rate is not None:
            d["ultrafiltration_rate"] = self.ultrafiltration_rate
        return d


@dataclass
class MedicationReviewResult:
    """ผลการตรวจสอบยาทั้งหมด."""
    alerts: list[MedicationAlert] = field(default_factory=list)
    volume_assessment: Optional[VolumeAssessment] = None
    review_date: str = ""
    disclaimer: str = (
        "⚠️ ข้อมูลนี้เป็นการคัดกรองเบื้องต้นเท่านั้น — "
        "ห้ามใช้แทนการตัดสินใจทางคลินิก — "
        "กรุณาปรึกษาแพทย์หรือเภสัชกร"
    )

    def __post_init__(self):
        if not self.review_date:
            self.review_date = datetime.now().strftime("%Y-%m-%d")

    def critical_alerts(self) -> list[MedicationAlert]:
        return [a for a in self.alerts
                if a.alert_level == AlertLevel.CRITICAL.value]

    def warning_alerts(self) -> list[MedicationAlert]:
        return [a for a in self.alerts
                if a.alert_level == AlertLevel.WARNING.value]

    def to_dict(self) -> dict:
        d: dict = {
            "review_date": self.review_date,
            "alerts": [a.to_dict() for a in self.alerts],
            "critical_count": len(self.critical_alerts()),
            "warning_count": len(self.warning_alerts()),
            "disclaimer": self.disclaimer,
        }
        if self.volume_assessment:
            d["volume_assessment"] = self.volume_assessment.to_dict()
        return d


# ═══════════════════════════════════════════════════════════
# Nephrotoxic Drug Database
# ═══════════════════════════════════════════════════════════

# ยาที่เป็นพิษต่อไต — ชื่อ generic (lowercase)
# กฎ: แจ้งเตือนเมื่อพบในรายการยาของผู้ป่วย CKD
NEPHROTOXIC_DRUGS: dict[str, dict] = {
    "nsaid": {
        "names": [
            "ibuprofen", "naproxen", "diclofenac", "piroxicam",
            "indomethacin", "mefenamic acid", "celecoxib",
            "meloxicam", "ketorolac", "aspirin",
        ],
        "thai_names": [
            "ไอบูโพรเฟน", "นาพรอกเซน", "ไดโคลฟีแนค", "พิร็อกซิแคม",
            "อินโดเมทาซิน", "มีฟีนามิก", "เซเลค็อกซิบ",
            "เมล็อกซิแคม", "คีโตโรแลค",
        ],
        "level": AlertLevel.WARNING.value,
        "message_th": "ยาต้านอักเสบ (NSAIDs) อาจทำให้ไตเสื่อมเร็วขึ้น",
        "recommendation_th": (
            "พิจารณาทบทวนความจำเป็น — "
            "ปรึกษาแพทย์เกี่ยวกับทางเลือกอื่น"
        ),
    },
    "aminoglycoside": {
        "names": [
            "gentamicin", "amikacin", "tobramycin",
            "streptomycin", "neomycin",
        ],
        "thai_names": [
            "เจนตาไมซิน", "อะมิคาซิน", "โทบราไมซิน",
        ],
        "level": AlertLevel.CRITICAL.value,
        "message_th": (
            "ยากลุ่ม Aminoglycoside มีพิษต่อไตสูง — "
            "ต้องติดตามระดับยาในเลือดและ Cr อย่างใกล้ชิด"
        ),
        "recommendation_th": (
            "ปรึกษาแพทย์เพื่อพิจารณาปรับขนาดยาหรือเปลี่ยนยา"
        ),
    },
    "contrast": {
        "names": [
            "iodinated contrast", "contrast media",
            "gadolinium",
        ],
        "thai_names": ["สารทึบรังสี"],
        "level": AlertLevel.CRITICAL.value,
        "message_th": (
            "สารทึบรังสีอาจทำให้ไตวายเฉียบพลัน "
            "(Contrast-Induced Nephropathy)"
        ),
        "recommendation_th": (
            "ปรึกษาแพทย์เรื่องการให้สารน้ำป้องกัน "
            "และทางเลือกการตรวจที่ไม่ใช้สารทึบรังสี"
        ),
    },
    "acei_arb": {
        "names": [
            "enalapril", "lisinopril", "ramipril", "captopril",
            "perindopril", "losartan", "valsartan", "irbesartan",
            "candesartan", "telmisartan", "olmesartan",
        ],
        "thai_names": [
            "อีนาลาพริล", "ไลซิโนพริล", "ราปริล", "แคปโตพริล",
            "ลอซาร์แทน", "วาลซาร์แทน",
        ],
        "level": AlertLevel.INFO.value,
        "message_th": (
            "ACEI/ARB อาจทำให้ Cr สูงขึ้นชั่วคราว — "
            "ต้องติดตาม Cr และ K+"
        ),
        "recommendation_th": (
            "ปรึกษาแพทย์ — ติดตาม Creatinine และ Potassium "
            "หลังเริ่มยาหรือปรับขนาด"
        ),
    },
    "metformin": {
        "names": ["metformin"],
        "thai_names": ["เมทฟอร์มิน"],
        "level": AlertLevel.WARNING.value,
        "message_th": "Metformin ต้องปรับขนาดหรือหยุดเมื่อ eGFR ต่ำ",
        "recommendation_th": (
            "ปรึกษาแพทย์ — eGFR <30: ห้ามใช้, "
            "eGFR 30-45: ลดขนาด, eGFR >45: ใช้ได้ตามปกติ"
        ),
    },
    "lithium": {
        "names": ["lithium"],
        "thai_names": ["ลิเทียม"],
        "level": AlertLevel.WARNING.value,
        "message_th": "Lithium เป็นพิษต่อไต — ต้องติดตามระดับยาและ Cr",
        "recommendation_th": (
            "ปรึกษาแพทย์เพื่อติดตามระดับยาในเลือดอย่างใกล้ชิด"
        ),
    },
}

# ยาที่เพิ่ม Potassium — เสี่ยง hyperkalemia ในผู้ป่วย CKD
POTASSIUM_RAISING_DRUGS: dict[str, str] = {
    "spironolactone": "Spironolactone (ยาขับปัสสาวะเก็บ K+)",
    "amiloride": "Amiloride (ยาขับปัสสาวะเก็บ K+)",
    "triamterene": "Triamterene (ยาขับปัสสาวะเก็บ K+)",
    "potassium chloride": "KCl supplement",
    "kcl": "KCl supplement",
    "trimethoprim": "Trimethoprim (เพิ่ม K+ ในเลือด)",
}


# ═══════════════════════════════════════════════════════════
# CKD Dose Adjustment Rules
# ═══════════════════════════════════════════════════════════

# eGFR threshold → drug → dose adjustment message
CKD_DOSE_RULES: list[dict] = [
    {
        "drug_pattern": ["metformin"],
        "egfr_threshold": 30,
        "level": AlertLevel.CRITICAL.value,
        "message_th": "Metformin ห้ามใช้เมื่อ eGFR < 30",
        "recommendation_th": "ปรึกษาแพทย์เพื่อหยุดยาหรือเปลี่ยนยา",
    },
    {
        "drug_pattern": ["metformin"],
        "egfr_threshold": 45,
        "level": AlertLevel.WARNING.value,
        "message_th": "Metformin ต้องลดขนาดเมื่อ eGFR 30-45",
        "recommendation_th": "ปรึกษาแพทย์เพื่อปรับขนาดยา",
    },
    {
        "drug_pattern": ["allopurinol"],
        "egfr_threshold": 30,
        "level": AlertLevel.WARNING.value,
        "message_th": "Allopurinol ต้องลดขนาดเมื่อ eGFR < 30",
        "recommendation_th": "ปรึกษาแพทย์เพื่อปรับขนาดยา (สูงสุด 100 mg/d)",
    },
    {
        "drug_pattern": ["gabapentin", "pregabalin"],
        "egfr_threshold": 30,
        "level": AlertLevel.WARNING.value,
        "message_th": "Gabapentin/Pregabalin ต้องลดขนาดเมื่อ eGFR < 30",
        "recommendation_th": "ปรึกษาแพทย์เพื่อปรับขนาดยาตาม eGFR",
    },
    {
        "drug_pattern": [
            "enoxaparin", "fondaparinux", "dabigatran", "rivaroxaban",
        ],
        "egfr_threshold": 30,
        "level": AlertLevel.CRITICAL.value,
        "message_th": (
            "ยาต้านการแข็งตัวของเลือดต้องปรับขนาดหรือหยุดเมื่อ eGFR < 30"
        ),
        "recommendation_th": (
            "ปรึกษาแพทย์เพื่อปรับขนาดหรือเปลี่ยนยา — "
            "เสี่ยงเลือดออกง่าย"
        ),
    },
    {
        "drug_pattern": ["digoxin"],
        "egfr_threshold": 30,
        "level": AlertLevel.WARNING.value,
        "message_th": "Digoxin ต้องลดขนาดและติดตามระดับยาเมื่อ eGFR < 30",
        "recommendation_th": "ปรึกษาแพทย์เพื่อตรวจระดับ Digoxin ในเลือด",
    },
]


# ═══════════════════════════════════════════════════════════
# Core Functions
# ═══════════════════════════════════════════════════════════

def normalize_drug_name(name: str) -> str:
    """ทำให้ชื่อยาเป็น lowercase, ตัดช่องว่าง."""
    return name.strip().lower()


def screen_nephrotoxic_drugs(
    medications: list[str],
    ckd_stage: str = "",
) -> list[MedicationAlert]:
    """คัดกรองยาที่เป็นพิษต่อไตจากรายการยา.

    Args:
        medications: รายการชื่อยา (generic หรือ Thai)
        ckd_stage: ระดับ CKD (G1-G5) — ถ้ามี จะเพิ่มบริบทการแจ้งเตือน

    Returns:
        list of MedicationAlert
    """
    alerts: list[MedicationAlert] = []
    seen_categories: set[str] = set()

    for med in medications:
        med_lower = normalize_drug_name(med)
        if not med_lower:
            continue

        for cat_key, cat_data in NEPHROTOXIC_DRUGS.items():
            if cat_key in seen_categories:
                continue

            matched = False
            for drug_name in cat_data["names"]:
                if drug_name in med_lower or med_lower in drug_name:
                    matched = True
                    break

            if not matched:
                for thai_name in cat_data.get("thai_names", []):
                    if thai_name in med or med in thai_name:
                        matched = True
                        break

            if matched:
                seen_categories.add(cat_key)

                # Upgrade ACEI/ARB to WARNING if advanced CKD
                level = cat_data["level"]
                msg = cat_data["message_th"]
                if cat_key == "acei_arb" and ckd_stage in ("G4", "G5"):
                    level = AlertLevel.WARNING.value
                    msg += " (CKD ระยะรุนแรง — ต้องติดตามใกล้ชิด)"

                # Upgrade metformin to CRITICAL if G5
                if cat_key == "metformin" and ckd_stage == "G5":
                    level = AlertLevel.CRITICAL.value
                    msg = "Metformin ห้ามใช้ในผู้ป่วย CKD G5"

                alerts.append(MedicationAlert(
                    drug_name=med,
                    alert_level=level,
                    category=DrugCategory.NEPHROTOXIC.value,
                    message_th=msg,
                    recommendation_th=cat_data["recommendation_th"],
                ))

    return alerts


def check_dose_adjustments(
    medications: list[str],
    egfr: Optional[float],
) -> list[MedicationAlert]:
    """ตรวจสอบว่ายาใดต้องปรับขนาดตาม eGFR.

    Args:
        medications: รายการชื่อยา
        egfr: ค่า eGFR ล่าสุด (None = ข้ามการตรวจสอบ)

    Returns:
        list of MedicationAlert
    """
    if egfr is None:
        return []

    alerts: list[MedicationAlert] = []
    seen_drugs: set[str] = set()

    for med in medications:
        med_lower = normalize_drug_name(med)
        if not med_lower:
            continue

        for rule in CKD_DOSE_RULES:
            if egfr >= rule["egfr_threshold"]:
                continue

            for pattern in rule["drug_pattern"]:
                if pattern in med_lower and pattern not in seen_drugs:
                    seen_drugs.add(pattern)
                    alerts.append(MedicationAlert(
                        drug_name=med,
                        alert_level=rule["level"],
                        category=DrugCategory.DOSE_ADJUST.value,
                        message_th=rule["message_th"],
                        recommendation_th=rule["recommendation_th"],
                    ))

    return alerts


def check_drug_lab_interactions(
    medications: list[str],
    lab_values: dict[str, float],
) -> list[MedicationAlert]:
    """ตรวจสอบปฏิกิริยายากับผลแล็บ.

    Args:
        medications: รายการชื่อยา
        lab_values: dict ชื่อ analyte → ค่า (เช่น {"Potassium": 5.8})

    Returns:
        list of MedicationAlert
    """
    alerts: list[MedicationAlert] = []
    potassium = lab_values.get("Potassium")

    if potassium is not None and potassium > 5.0:
        # ตรวจยาที่เพิ่ม K+
        for med in medications:
            med_lower = normalize_drug_name(med)
            for drug, desc in POTASSIUM_RAISING_DRUGS.items():
                if drug in med_lower:
                    level = AlertLevel.CRITICAL.value if potassium > 5.5 \
                        else AlertLevel.WARNING.value
                    alerts.append(MedicationAlert(
                        drug_name=med,
                        alert_level=level,
                        category=DrugCategory.POTASSIUM_RAISING.value,
                        message_th=(
                            f"{desc} อาจเพิ่ม K+ ในเลือด — "
                            f"K+ ปัจจุบัน = {potassium:.1f} mEq/L"
                        ),
                        recommendation_th=(
                            "ปรึกษาแพทย์เรื่องความเสี่ยง Hyperkalemia"
                        ),
                    ))
                    break

    # ตรวจ ACEI/ARB + Hyperkalemia
    if potassium is not None and potassium > 5.5:
        for med in medications:
            med_lower = normalize_drug_name(med)
            acei_arb_data = NEPHROTOXIC_DRUGS.get("acei_arb", {})
            for drug_name in acei_arb_data.get("names", []):
                if drug_name in med_lower:
                    alerts.append(MedicationAlert(
                        drug_name=med,
                        alert_level=AlertLevel.CRITICAL.value,
                        category=DrugCategory.POTASSIUM_RAISING.value,
                        message_th=(
                            f"ACEI/ARB + K+ สูง ({potassium:.1f}) — "
                            f"เสี่ยง Hyperkalemia รุนแรง"
                        ),
                        recommendation_th=(
                            "ปรึกษาแพทย์ทันที — "
                            "พิจารณาลดขนาดหรือหยุด ACEI/ARB"
                        ),
                    ))
                    break

    return alerts


# ═══════════════════════════════════════════════════════════
# Volume Assessment
# ═══════════════════════════════════════════════════════════

def assess_volume_status(
    lab_values: dict[str, float],
    pre_weight_kg: Optional[float] = None,
    post_weight_kg: Optional[float] = None,
    dry_weight_kg: Optional[float] = None,
    dialysis_hours: float = 4.0,
) -> VolumeAssessment:
    """ประเมินภาวะน้ำในร่างกายจากผลแล็บและน้ำหนัก.

    Args:
        lab_values: dict ชื่อ analyte → ค่า
        pre_weight_kg: น้ำหนักก่อนฟอก (kg)
        post_weight_kg: น้ำหนักหลังฟอก (kg)
        dry_weight_kg: Dry weight (kg)
        dialysis_hours: ชั่วโมงฟอกเลือด (default 4)

    Returns:
        VolumeAssessment
    """
    va = VolumeAssessment(status=VolumeStatus.UNKNOWN.value)
    indicators: list[str] = []

    # ── Weight-based assessment (dialysis patients) ──
    if pre_weight_kg is not None and dry_weight_kg is not None:
        va.pre_weight_kg = pre_weight_kg
        va.dry_weight_kg = dry_weight_kg

        idwg = pre_weight_kg - dry_weight_kg
        va.interdialytic_weight_gain_kg = round(idwg, 2)

        idwg_pct = (idwg / dry_weight_kg) * 100 if dry_weight_kg > 0 else 0

        if idwg_pct > 5:
            indicators.append(
                f"น้ำหนักเพิ่มระหว่างฟอก > 5% ({idwg_pct:.1f}%) — "
                f"น้ำเกินมาก"
            )
        elif idwg_pct > 3:
            indicators.append(
                f"น้ำหนักเพิ่มระหว่างฟอก > 3% ({idwg_pct:.1f}%) — "
                f"น้ำเกินปานกลาง"
            )
        elif idwg_pct < 0:
            indicators.append("น้ำหนักต่ำกว่า Dry Weight — อาจขาดน้ำ")

    if post_weight_kg is not None:
        va.post_weight_kg = post_weight_kg

    # UF rate calculation
    if (pre_weight_kg is not None and post_weight_kg is not None
            and dry_weight_kg is not None and dry_weight_kg > 0
            and dialysis_hours > 0):
        uf_volume_ml = (pre_weight_kg - post_weight_kg) * 1000
        uf_rate = uf_volume_ml / dry_weight_kg / dialysis_hours
        va.ultrafiltration_rate = round(uf_rate, 1)

        if uf_rate > 13:
            indicators.append(
                f"UF rate สูง ({uf_rate:.1f} mL/kg/hr > 13) — "
                f"เสี่ยง hemodynamic instability"
            )

    # ── Lab-based indicators ──
    sodium = lab_values.get("Sodium")
    albumin = lab_values.get("Albumin")
    hematocrit = lab_values.get("Hematocrit")
    bun = lab_values.get("BUN")
    creatinine = lab_values.get("Creatinine")

    if sodium is not None:
        if sodium < 130:
            indicators.append(
                f"Na+ ต่ำ ({sodium:.0f}) — อาจเกิดจากน้ำเกิน (dilutional)"
            )
        elif sodium > 148:
            indicators.append(
                f"Na+ สูง ({sodium:.0f}) — อาจขาดน้ำ (dehydration)"
            )

    if albumin is not None and albumin < 3.0:
        indicators.append(
            f"Albumin ต่ำ ({albumin:.1f}) — "
            f"oncotic pressure ลด, เสี่ยง fluid shift"
        )

    if bun is not None and creatinine is not None and creatinine > 0:
        bun_cr_ratio = bun / creatinine
        if bun_cr_ratio > 20:
            indicators.append(
                f"BUN/Cr ratio สูง ({bun_cr_ratio:.0f}) — "
                f"อาจขาดน้ำ (prerenal)"
            )

    # ── Determine overall status ──
    overload_signals = sum(1 for i in indicators if "น้ำเกิน" in i
                          or "dilutional" in i)
    dehydration_signals = sum(1 for i in indicators if "ขาดน้ำ" in i
                              or "dehydration" in i or "prerenal" in i)

    if overload_signals > 0 and overload_signals >= dehydration_signals:
        va.status = VolumeStatus.OVERLOADED.value
    elif dehydration_signals > 0:
        va.status = VolumeStatus.DEHYDRATED.value
    elif indicators:
        va.status = VolumeStatus.UNKNOWN.value
    elif pre_weight_kg is not None or any(
        v is not None for v in [sodium, albumin, bun]
    ):
        va.status = VolumeStatus.EUVOLEMIC.value

    va.indicators = indicators
    return va


# ═══════════════════════════════════════════════════════════
# Comprehensive Review
# ═══════════════════════════════════════════════════════════

def review_medications(
    medications: list[str],
    lab_values: dict[str, float],
    ckd_stage: str = "",
    egfr: Optional[float] = None,
    pre_weight_kg: Optional[float] = None,
    post_weight_kg: Optional[float] = None,
    dry_weight_kg: Optional[float] = None,
) -> MedicationReviewResult:
    """ตรวจสอบยาทั้งหมดแบบครบวงจร.

    Args:
        medications: รายการชื่อยา
        lab_values: dict ชื่อ analyte → ค่า
        ckd_stage: ระดับ CKD (G1-G5)
        egfr: ค่า eGFR ล่าสุด
        pre_weight_kg: น้ำหนักก่อนฟอก (kg)
        post_weight_kg: น้ำหนักหลังฟอก (kg)
        dry_weight_kg: Dry weight (kg)

    Returns:
        MedicationReviewResult
    """
    result = MedicationReviewResult()

    # 1. Nephrotoxic drug screening
    result.alerts.extend(
        screen_nephrotoxic_drugs(medications, ckd_stage)
    )

    # 2. Dose adjustment checks
    result.alerts.extend(
        check_dose_adjustments(medications, egfr)
    )

    # 3. Drug-lab interactions
    result.alerts.extend(
        check_drug_lab_interactions(medications, lab_values)
    )

    # 4. Volume assessment
    result.volume_assessment = assess_volume_status(
        lab_values=lab_values,
        pre_weight_kg=pre_weight_kg,
        post_weight_kg=post_weight_kg,
        dry_weight_kg=dry_weight_kg,
    )

    # Deduplicate alerts by drug_name + category
    seen: set[tuple[str, str]] = set()
    unique_alerts: list[MedicationAlert] = []
    for alert in result.alerts:
        key = (normalize_drug_name(alert.drug_name), alert.category)
        if key not in seen:
            seen.add(key)
            unique_alerts.append(alert)
    result.alerts = unique_alerts

    # Sort: critical first, then warning, then info
    priority = {
        AlertLevel.CRITICAL.value: 0,
        AlertLevel.WARNING.value: 1,
        AlertLevel.INFO.value: 2,
    }
    result.alerts.sort(key=lambda a: priority.get(a.alert_level, 9))

    return result


# ═══════════════════════════════════════════════════════════
# Thai Text Output
# ═══════════════════════════════════════════════════════════

def review_text_th(result: MedicationReviewResult) -> str:
    """สร้างข้อความผลการตรวจสอบยาภาษาไทย."""
    lines = [
        "💊 ผลตรวจสอบยาและภาวะน้ำ",
        f"วันที่: {result.review_date}",
        "",
    ]

    has_volume_info = (
        result.volume_assessment is not None
        and (result.volume_assessment.status != VolumeStatus.UNKNOWN.value
             or result.volume_assessment.indicators)
    )
    if not result.alerts and not has_volume_info:
        lines.append("✅ ไม่พบข้อควรระวังจากข้อมูลที่มี")
        lines.append("")
        lines.append(f"⚠️ {result.disclaimer}")
        return "\n".join(lines)

    # ── Medication Alerts ──
    if result.alerts:
        critical = result.critical_alerts()
        warnings = result.warning_alerts()

        if critical:
            lines.append(f"🔴 แจ้งเตือนสำคัญ ({len(critical)} รายการ):")
            for a in critical:
                lines.append(f"  ❌ {a.drug_name}")
                lines.append(f"     {a.message_th}")
                lines.append(f"     → {a.recommendation_th}")
            lines.append("")

        if warnings:
            lines.append(f"🟡 ข้อควรระวัง ({len(warnings)} รายการ):")
            for a in warnings:
                lines.append(f"  ⚠️ {a.drug_name}")
                lines.append(f"     {a.message_th}")
                lines.append(f"     → {a.recommendation_th}")
            lines.append("")

        info_alerts = [a for a in result.alerts
                       if a.alert_level == AlertLevel.INFO.value]
        if info_alerts:
            lines.append("ℹ️ ข้อมูลเพิ่มเติม:")
            for a in info_alerts:
                lines.append(f"  • {a.drug_name}: {a.message_th}")
            lines.append("")

    # ── Volume Assessment ──
    va = result.volume_assessment
    if va and va.status != VolumeStatus.UNKNOWN.value:
        status_emoji = {
            VolumeStatus.OVERLOADED.value: "💧",
            VolumeStatus.EUVOLEMIC.value: "✅",
            VolumeStatus.DEHYDRATED.value: "🏜️",
        }
        status_text = {
            VolumeStatus.OVERLOADED.value: "น้ำเกิน (Fluid Overload)",
            VolumeStatus.EUVOLEMIC.value: "น้ำปกติ (Euvolemic)",
            VolumeStatus.DEHYDRATED.value: "ขาดน้ำ (Dehydrated)",
        }
        emoji = status_emoji.get(va.status, "❓")
        text = status_text.get(va.status, va.status)
        lines.append(f"💧 ภาวะน้ำ: {emoji} {text}")

        if va.interdialytic_weight_gain_kg is not None:
            lines.append(
                f"  น้ำหนักเพิ่มระหว่างฟอก: "
                f"{va.interdialytic_weight_gain_kg:.1f} kg"
            )
        if va.ultrafiltration_rate is not None:
            lines.append(
                f"  UF rate: {va.ultrafiltration_rate:.1f} mL/kg/hr"
            )
        for ind in va.indicators:
            lines.append(f"  • {ind}")
        lines.append("")

    elif va and va.indicators:
        lines.append("💧 ตัวชี้วัดภาวะน้ำ:")
        for ind in va.indicators:
            lines.append(f"  • {ind}")
        lines.append("")

    lines.append(f"⚠️ {result.disclaimer}")
    return "\n".join(lines)
