#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nephrology Drug Database & Interaction Checker — Phase E
Kidney Clinical Intelligence Platform 2.0

ฐานข้อมูลยาโรคไตและระบบตรวจสอบอันตรกิริยาระหว่างยา:
  1. Nephrology Drug Database — ยาที่ใช้บ่อยในคลินิกโรคไต 40+ รายการ
  2. eGFR-Based Dose Adjustment — ปรับขนาดยาตาม eGFR แบบ tiered
  3. Drug-Drug Interactions — ตรวจสอบอันตรกิริยาระหว่างยาคู่
  4. CKD Medication Summary — สรุปยาที่ใช้ในผู้ป่วย CKD แบบครบวงจร

กฎความปลอดภัย (MUST preserve):
  - ห้ามสั่งยา เปลี่ยนยา หรือสั่งการรักษาโดยอัตโนมัติ
  - ผลเป็นข้อมูลประกอบการพิจารณาเท่านั้น
  - Rule Engine เท่านั้น — ไม่ใช้ AI ภายนอก
  - ต้องผ่าน Human Review ก่อนนำไปใช้ประกอบการรักษา
  - ไม่เก็บข้อมูลระบุตัวผู้ป่วย — ใช้ owner_hash เท่านั้น
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ═══════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════

class DrugClass(str, Enum):
    """หมวดยาทางโรคไต."""
    ANTIHYPERTENSIVE = "antihypertensive"
    DIURETIC = "diuretic"
    PHOSPHATE_BINDER = "phosphate_binder"
    ESA = "esa"
    IRON_SUPPLEMENT = "iron_supplement"
    VITAMIN_D = "vitamin_d"
    CALCIMIMETIC = "calcimimetic"
    POTASSIUM_BINDER = "potassium_binder"
    ANTICOAGULANT = "anticoagulant"
    ANTIDIABETIC = "antidiabetic"
    URIC_ACID = "uric_acid"
    ANTIBIOTIC = "antibiotic"
    ANALGESIC = "analgesic"
    LIPID_LOWERING = "lipid_lowering"
    IMMUNOSUPPRESSANT = "immunosuppressant"
    OTHER = "other"


class InteractionSeverity(str, Enum):
    """ระดับความรุนแรงของอันตรกิริยา."""
    CONTRAINDICATED = "contraindicated"  # ห้ามใช้ร่วม
    MAJOR = "major"                       # อันตราย — ต้องหลีกเลี่ยง
    MODERATE = "moderate"                 # ต้องระวัง — ติดตาม
    MINOR = "minor"                       # ระวังเล็กน้อย


class DoseAdjustAction(str, Enum):
    """ชนิดการปรับขนาดยา."""
    NO_CHANGE = "no_change"
    REDUCE_25 = "reduce_25"
    REDUCE_50 = "reduce_50"
    REDUCE_75 = "reduce_75"
    MAX_DOSE = "max_dose"
    EXTEND_INTERVAL = "extend_interval"
    AVOID = "avoid"
    CONTRAINDICATED = "contraindicated"
    MONITOR = "monitor"


# ═══════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════

@dataclass
class DrugInfo:
    """ข้อมูลยาในฐานข้อมูล."""
    generic_name: str
    drug_class: str               # DrugClass value
    thai_name: str = ""
    brand_names: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    usual_dose: str = ""           # ขนาดยาปกติ
    max_dose: str = ""
    routes: list[str] = field(default_factory=list)
    ckd_notes_th: str = ""         # หมายเหตุเฉพาะ CKD
    dialysis_notes_th: str = ""    # หมายเหตุสำหรับผู้ป่วยฟอกเลือด
    is_nephrotoxic: bool = False
    egfr_adjustments: list[dict] = field(default_factory=list)
    # egfr_adjustments: [{"egfr_min": 0, "egfr_max": 30, "action": "avoid", "dose_th": "ห้ามใช้"}]

    def to_dict(self) -> dict:
        return {
            "generic_name": self.generic_name,
            "drug_class": self.drug_class,
            "thai_name": self.thai_name,
            "brand_names": self.brand_names,
            "usual_dose": self.usual_dose,
            "is_nephrotoxic": self.is_nephrotoxic,
            "ckd_notes_th": self.ckd_notes_th,
            "egfr_adjustments": self.egfr_adjustments,
        }


@dataclass
class DrugInteraction:
    """อันตรกิริยาระหว่างยาคู่."""
    drug_a: str
    drug_b: str
    severity: str                 # InteractionSeverity value
    mechanism_th: str             # กลไกภาษาไทย
    effect_th: str                # ผลกระทบ
    management_th: str            # การจัดการ
    monitoring: str = ""          # สิ่งที่ต้องติดตาม

    def to_dict(self) -> dict:
        return {
            "drug_a": self.drug_a,
            "drug_b": self.drug_b,
            "severity": self.severity,
            "mechanism_th": self.mechanism_th,
            "effect_th": self.effect_th,
            "management_th": self.management_th,
            "monitoring": self.monitoring,
        }


@dataclass
class DoseRecommendation:
    """คำแนะนำการปรับขนาดยาตาม eGFR."""
    drug_name: str
    egfr: float
    action: str                    # DoseAdjustAction value
    current_dose: str = ""
    recommended_dose_th: str = ""  # ขนาดยาที่แนะนำ
    reason_th: str = ""            # เหตุผล
    monitoring_th: str = ""        # สิ่งที่ต้องติดตาม

    def to_dict(self) -> dict:
        return {
            "drug_name": self.drug_name,
            "egfr": round(self.egfr, 1),
            "action": self.action,
            "recommended_dose_th": self.recommended_dose_th,
            "reason_th": self.reason_th,
            "monitoring_th": self.monitoring_th,
        }


@dataclass
class CKDMedicationSummary:
    """สรุปการใช้ยาสำหรับผู้ป่วย CKD."""
    owner_hash: str
    review_date: str
    egfr: float
    ckd_stage: str
    medications: list[str]
    dose_recommendations: list[DoseRecommendation] = field(default_factory=list)
    interactions: list[DrugInteraction] = field(default_factory=list)
    contraindicated: list[str] = field(default_factory=list)
    needs_monitoring: list[str] = field(default_factory=list)
    disclaimer: str = (
        "⚠️ ข้อมูลยานี้เป็นข้อมูลประกอบเท่านั้น — "
        "ห้ามใช้แทนการตัดสินใจทางคลินิก — "
        "กรุณาปรึกษาแพทย์หรือเภสัชกร"
    )

    def to_dict(self) -> dict:
        return {
            "owner_hash_prefix": self.owner_hash[:8] if self.owner_hash else "",
            "review_date": self.review_date,
            "egfr": round(self.egfr, 1),
            "ckd_stage": self.ckd_stage,
            "medication_count": len(self.medications),
            "dose_recommendations": [r.to_dict() for r in self.dose_recommendations],
            "interactions": [i.to_dict() for i in self.interactions],
            "contraindicated": self.contraindicated,
            "needs_monitoring": self.needs_monitoring,
            "disclaimer": self.disclaimer,
        }

    def summary_text_th(self) -> str:
        """สร้างข้อความสรุปภาษาไทยสำหรับ LINE."""
        return _generate_medication_summary(self)


# ═══════════════════════════════════════════════════════════
# Nephrology Drug Database — 40+ ยาที่ใช้บ่อย
# ═══════════════════════════════════════════════════════════

NEPHROLOGY_DRUG_DB: dict[str, DrugInfo] = {}


def _build_drug_db() -> dict[str, DrugInfo]:
    """สร้างฐานข้อมูลยาโรคไต."""
    drugs: list[DrugInfo] = [
        # ── Antihypertensives: ACEI ──
        DrugInfo("enalapril", DrugClass.ANTIHYPERTENSIVE.value,
                 thai_name="อีนาลาพริล", brand_names=["Renitec"],
                 aliases=["enalapril maleate"],
                 usual_dose="5-20 mg BID", max_dose="40 mg/d",
                 ckd_notes_th="ลดโปรตีนในปัสสาวะ — ดีต่อไต แต่ต้องติดตาม Cr/K+",
                 dialysis_notes_th="ถูกดึงออกบางส่วนโดย dialysis — ให้หลังฟอก",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 30, "action": "reduce_50",
                      "dose_th": "เริ่ม 2.5 mg OD — ปรับช้าๆ ติดตาม Cr/K+"},
                     {"egfr_min": 30, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ขนาดปกติ — ติดตาม Cr/K+ หลังเริ่มยา"},
                 ]),
        DrugInfo("ramipril", DrugClass.ANTIHYPERTENSIVE.value,
                 thai_name="รามิพริล", brand_names=["Tritace"],
                 usual_dose="2.5-10 mg OD", max_dose="10 mg/d",
                 ckd_notes_th="ACEI — ชะลอการเสื่อมของไต ติดตาม Cr/K+",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 30, "action": "reduce_50",
                      "dose_th": "เริ่ม 1.25 mg OD — สูงสุด 5 mg OD"},
                     {"egfr_min": 30, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ขนาดปกติ"},
                 ]),

        # ── Antihypertensives: ARB ──
        DrugInfo("losartan", DrugClass.ANTIHYPERTENSIVE.value,
                 thai_name="ลอซาร์แทน", brand_names=["Cozaar"],
                 usual_dose="50-100 mg OD", max_dose="100 mg/d",
                 ckd_notes_th="ARB — ลด proteinuria, ชะลอ CKD progression",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ไม่ต้องปรับขนาด — ติดตาม Cr/K+"},
                 ]),
        DrugInfo("valsartan", DrugClass.ANTIHYPERTENSIVE.value,
                 thai_name="วาลซาร์แทน", brand_names=["Diovan"],
                 usual_dose="80-160 mg OD", max_dose="320 mg/d",
                 ckd_notes_th="ARB — ลด proteinuria",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ไม่ต้องปรับขนาด — ติดตาม Cr/K+"},
                 ]),
        DrugInfo("irbesartan", DrugClass.ANTIHYPERTENSIVE.value,
                 thai_name="เออร์เบซาร์แทน", brand_names=["Aprovel"],
                 usual_dose="150-300 mg OD", max_dose="300 mg/d",
                 ckd_notes_th="ARB — proven renoprotective in type 2 DM nephropathy"),

        # ── Antihypertensives: CCB ──
        DrugInfo("amlodipine", DrugClass.ANTIHYPERTENSIVE.value,
                 thai_name="แอมโลดิปีน", brand_names=["Norvasc"],
                 usual_dose="5-10 mg OD", max_dose="10 mg/d",
                 ckd_notes_th="CCB — ไม่ต้องปรับขนาดตาม eGFR"),
        DrugInfo("nifedipine", DrugClass.ANTIHYPERTENSIVE.value,
                 thai_name="นิฟิดิปีน", brand_names=["Adalat"],
                 usual_dose="30-60 mg OD (SR)", max_dose="90 mg/d",
                 ckd_notes_th="CCB — ใช้ได้โดยไม่ปรับขนาด"),

        # ── Antihypertensives: Beta-blocker ──
        DrugInfo("atenolol", DrugClass.ANTIHYPERTENSIVE.value,
                 thai_name="อะทีโนลอล", brand_names=["Tenormin"],
                 usual_dose="50-100 mg OD", max_dose="100 mg/d",
                 ckd_notes_th="ขับทางไต — ต้องปรับขนาด",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 15, "action": "reduce_50",
                      "dose_th": "สูงสุด 25 mg OD"},
                     {"egfr_min": 15, "egfr_max": 35, "action": "reduce_25",
                      "dose_th": "สูงสุด 50 mg OD"},
                     {"egfr_min": 35, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ขนาดปกติ"},
                 ]),
        DrugInfo("carvedilol", DrugClass.ANTIHYPERTENSIVE.value,
                 thai_name="คาร์วีดิลอล", brand_names=["Dilatrend"],
                 usual_dose="6.25-25 mg BID", max_dose="50 mg/d",
                 ckd_notes_th="ขับทางตับ — ไม่ต้องปรับขนาดตาม eGFR"),

        # ── Diuretics ──
        DrugInfo("furosemide", DrugClass.DIURETIC.value,
                 thai_name="ฟูโรซีไมด์", brand_names=["Lasix"],
                 usual_dose="20-80 mg OD-BID", max_dose="600 mg/d (CKD)",
                 ckd_notes_th="Loop diuretic — อาจต้องเพิ่มขนาดใน CKD",
                 dialysis_notes_th="ไม่มีประโยชน์ในผู้ป่วย anuria on dialysis"),
        DrugInfo("hydrochlorothiazide", DrugClass.DIURETIC.value,
                 thai_name="ไฮโดรคลอโรไทอะไซด์", brand_names=["HCTZ"],
                 aliases=["hctz"],
                 usual_dose="12.5-25 mg OD", max_dose="50 mg/d",
                 ckd_notes_th="ประสิทธิภาพลดลงเมื่อ eGFR < 30 — พิจารณาเปลี่ยนเป็น loop diuretic",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 30, "action": "avoid",
                      "dose_th": "ไม่แนะนำ — ประสิทธิภาพต่ำมาก พิจารณา furosemide แทน"},
                     {"egfr_min": 30, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ได้ตามปกติ"},
                 ]),
        DrugInfo("spironolactone", DrugClass.DIURETIC.value,
                 thai_name="สไปโรโนแลคโตน", brand_names=["Aldactone"],
                 usual_dose="25-50 mg OD", max_dose="100 mg/d",
                 ckd_notes_th="K+-sparing — เสี่ยง hyperkalemia สูงใน CKD",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 30, "action": "avoid",
                      "dose_th": "หลีกเลี่ยง — เสี่ยง hyperkalemia สูง"},
                     {"egfr_min": 30, "egfr_max": 999, "action": "monitor",
                      "dose_th": "ใช้ได้ — ติดตาม K+ ทุก 1-2 สัปดาห์"},
                 ]),

        # ── Phosphate Binders ──
        DrugInfo("calcium carbonate", DrugClass.PHOSPHATE_BINDER.value,
                 thai_name="แคลเซียมคาร์บอเนต", brand_names=["CaCO3"],
                 aliases=["caco3"],
                 usual_dose="500-1500 mg กับอาหาร TID",
                 ckd_notes_th="Phosphate binder — ระวังภาวะ hypercalcemia"),
        DrugInfo("sevelamer", DrugClass.PHOSPHATE_BINDER.value,
                 thai_name="เซเวลาเมอร์", brand_names=["Renagel", "Renvela"],
                 usual_dose="800-1600 mg กับอาหาร TID",
                 ckd_notes_th="Non-calcium phosphate binder — ดีกว่า CaCO3 ในแง่ vascular calcification"),
        DrugInfo("lanthanum carbonate", DrugClass.PHOSPHATE_BINDER.value,
                 thai_name="แลนทานัมคาร์บอเนต", brand_names=["Fosrenol"],
                 usual_dose="500-1000 mg กับอาหาร TID",
                 ckd_notes_th="Non-calcium phosphate binder — เคี้ยวก่อนกลืน"),

        # ── ESA (Erythropoiesis-Stimulating Agents) ──
        DrugInfo("epoetin alfa", DrugClass.ESA.value,
                 thai_name="อีโพอีติน อัลฟา", brand_names=["Eprex", "Epogen"],
                 aliases=["epo", "erythropoietin"],
                 usual_dose="50-100 U/kg SC 3x/wk หรือ IV",
                 ckd_notes_th="Hb target 10-11.5 g/dL — ห้าม >13 g/dL",
                 dialysis_notes_th="ให้ IV ขณะฟอกเลือด"),
        DrugInfo("darbepoetin alfa", DrugClass.ESA.value,
                 thai_name="ดาร์บีโพอีติน อัลฟา", brand_names=["Aranesp"],
                 usual_dose="0.45 mcg/kg SC q2wk",
                 ckd_notes_th="ESA ออกฤทธิ์ยาว — ให้ทุก 2-4 สัปดาห์"),

        # ── Iron Supplements ──
        DrugInfo("iron sucrose", DrugClass.IRON_SUPPLEMENT.value,
                 thai_name="ไอรอน ซูโครส", brand_names=["Venofer"],
                 usual_dose="100-200 mg IV ต่อครั้ง",
                 ckd_notes_th="IV iron — TSAT target 20-50%, Ferritin 200-500 ng/mL",
                 dialysis_notes_th="ให้ IV ขณะฟอกเลือด"),
        DrugInfo("ferrous sulfate", DrugClass.IRON_SUPPLEMENT.value,
                 thai_name="เฟอรัส ซัลเฟต", brand_names=["FeSO4"],
                 aliases=["feso4", "ferrous fumarate"],
                 usual_dose="200-325 mg OD-TID",
                 ckd_notes_th="Oral iron — ดูดซึมไม่ดีใน CKD, พิจารณา IV iron ถ้าไม่ตอบสนอง"),

        # ── Vitamin D ──
        DrugInfo("calcitriol", DrugClass.VITAMIN_D.value,
                 thai_name="แคลซิไตรออล", brand_names=["Rocaltrol"],
                 usual_dose="0.25-0.5 mcg OD",
                 ckd_notes_th="Active vitamin D — ติดตาม Ca/P",
                 dialysis_notes_th="ให้ IV 1-2 mcg หลังฟอก ถ้า iPTH สูงมาก"),
        DrugInfo("alfacalcidol", DrugClass.VITAMIN_D.value,
                 thai_name="อัลฟาแคลซิดอล", brand_names=["One-Alpha"],
                 usual_dose="0.25-1 mcg OD",
                 ckd_notes_th="Vitamin D analog — ติดตาม Ca/P"),
        DrugInfo("cholecalciferol", DrugClass.VITAMIN_D.value,
                 thai_name="โคเลแคลซิเฟอรอล", brand_names=["Vitamin D3"],
                 aliases=["vitamin d3", "vit d3"],
                 usual_dose="1000-4000 IU OD",
                 ckd_notes_th="Nutritional vitamin D — supplement ถ้า 25(OH)D < 30"),

        # ── Calcimimetic ──
        DrugInfo("cinacalcet", DrugClass.CALCIMIMETIC.value,
                 thai_name="ซินาแคลเซท", brand_names=["Sensipar", "Mimpara"],
                 usual_dose="30 mg OD (เริ่มต้น)", max_dose="180 mg/d",
                 ckd_notes_th="ลด iPTH — ติดตาม Ca (ระวัง hypocalcemia)",
                 dialysis_notes_th="ใช้เมื่อ iPTH สูงไม่ตอบสนองต่อ vitamin D"),

        # ── Potassium Binders ──
        DrugInfo("sodium polystyrene sulfonate", DrugClass.POTASSIUM_BINDER.value,
                 thai_name="โซเดียม โพลีสไตรีน ซัลโฟเนต",
                 brand_names=["Kayexalate", "Kalimate"],
                 aliases=["sps", "kayexalate", "kalimate"],
                 usual_dose="15-30 g OD-QID",
                 ckd_notes_th="จับ K+ ในลำไส้ — ใช้ treat hyperkalemia"),
        DrugInfo("patiromer", DrugClass.POTASSIUM_BINDER.value,
                 thai_name="พาทิโรเมอร์", brand_names=["Veltassa"],
                 usual_dose="8.4 g OD", max_dose="25.2 g/d",
                 ckd_notes_th="K+ binder ใหม่ — ทนยาได้ดีกว่า SPS"),

        # ── Antidiabetics ──
        DrugInfo("metformin", DrugClass.ANTIDIABETIC.value,
                 thai_name="เมทฟอร์มิน", brand_names=["Glucophage"],
                 usual_dose="500-1000 mg BID", max_dose="2000 mg/d",
                 ckd_notes_th="ลดขนาดเมื่อ eGFR 30-45, ห้ามใช้เมื่อ eGFR < 30",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 30, "action": "contraindicated",
                      "dose_th": "ห้ามใช้ — เสี่ยง lactic acidosis"},
                     {"egfr_min": 30, "egfr_max": 45, "action": "max_dose",
                      "dose_th": "สูงสุด 1000 mg/d — ห้ามเริ่มยาใหม่ใน range นี้"},
                     {"egfr_min": 45, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ขนาดปกติ"},
                 ]),
        DrugInfo("glipizide", DrugClass.ANTIDIABETIC.value,
                 thai_name="กลิพิไซด์", brand_names=["Glucotrol"],
                 usual_dose="5-10 mg OD-BID", max_dose="20 mg/d",
                 ckd_notes_th="SU ที่ปลอดภัยกว่าใน CKD — ขับทางตับ",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 999, "action": "monitor",
                      "dose_th": "เริ่มขนาดต่ำ — เสี่ยง hypoglycemia ใน CKD"},
                 ]),
        DrugInfo("gliclazide", DrugClass.ANTIDIABETIC.value,
                 thai_name="กลิคลาไซด์", brand_names=["Diamicron"],
                 usual_dose="30-120 mg OD (MR)", max_dose="120 mg/d",
                 ckd_notes_th="SU ที่ปลอดภัยกว่าใน CKD — ขับทางตับ"),
        DrugInfo("empagliflozin", DrugClass.ANTIDIABETIC.value,
                 thai_name="เอ็มพากลิโฟลซิน", brand_names=["Jardiance"],
                 usual_dose="10-25 mg OD",
                 ckd_notes_th="SGLT2i — ชะลอ CKD progression (EMPA-KIDNEY trial)",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 20, "action": "avoid",
                      "dose_th": "ไม่แนะนำเริ่มยาใหม่ ถ้ากำลังใช้อยู่อาจใช้ต่อได้"},
                     {"egfr_min": 20, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ได้ — 10 mg OD สำหรับ CKD"},
                 ]),
        DrugInfo("dapagliflozin", DrugClass.ANTIDIABETIC.value,
                 thai_name="ดาพากลิโฟลซิน", brand_names=["Forxiga"],
                 usual_dose="10 mg OD",
                 ckd_notes_th="SGLT2i — ชะลอ CKD progression (DAPA-CKD trial)",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 25, "action": "avoid",
                      "dose_th": "ไม่แนะนำเริ่มยาใหม่"},
                     {"egfr_min": 25, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ได้ 10 mg OD — สำหรับ CKD + DM หรือ CKD อย่างเดียว"},
                 ]),

        # ── Uric Acid ──
        DrugInfo("allopurinol", DrugClass.URIC_ACID.value,
                 thai_name="อัลโลพูรินอล", brand_names=["Zyloric"],
                 usual_dose="100-300 mg OD", max_dose="800 mg/d",
                 ckd_notes_th="ต้องปรับขนาดตาม eGFR",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 15, "action": "max_dose",
                      "dose_th": "สูงสุด 50 mg OD"},
                     {"egfr_min": 15, "egfr_max": 30, "action": "max_dose",
                      "dose_th": "สูงสุด 100 mg OD"},
                     {"egfr_min": 30, "egfr_max": 60, "action": "max_dose",
                      "dose_th": "สูงสุด 200 mg OD"},
                     {"egfr_min": 60, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ขนาดปกติ"},
                 ]),
        DrugInfo("febuxostat", DrugClass.URIC_ACID.value,
                 thai_name="ฟีบูโซสแตท", brand_names=["Uloric", "Feburic"],
                 usual_dose="40-80 mg OD",
                 ckd_notes_th="ขับทางตับ — ไม่ต้องปรับขนาดมากใน CKD",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 15, "action": "avoid",
                      "dose_th": "หลีกเลี่ยง — ข้อมูลจำกัด"},
                     {"egfr_min": 15, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ได้ 40-80 mg OD"},
                 ]),

        # ── Lipid Lowering ──
        DrugInfo("atorvastatin", DrugClass.LIPID_LOWERING.value,
                 thai_name="อะทอร์วาสแตติน", brand_names=["Lipitor"],
                 usual_dose="10-40 mg OD", max_dose="80 mg/d",
                 ckd_notes_th="Statin — ไม่ต้องปรับขนาดตาม eGFR"),
        DrugInfo("rosuvastatin", DrugClass.LIPID_LOWERING.value,
                 thai_name="โรซูวาสแตติน", brand_names=["Crestor"],
                 usual_dose="5-20 mg OD", max_dose="40 mg/d",
                 ckd_notes_th="Statin — eGFR < 30 เริ่ม 5 mg สูงสุด 10 mg",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 30, "action": "max_dose",
                      "dose_th": "เริ่ม 5 mg OD — สูงสุด 10 mg OD"},
                     {"egfr_min": 30, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ขนาดปกติ"},
                 ]),

        # ── Analgesics ──
        DrugInfo("paracetamol", DrugClass.ANALGESIC.value,
                 thai_name="พาราเซตามอล", brand_names=["Tylenol", "Panadol"],
                 aliases=["acetaminophen"],
                 usual_dose="500-1000 mg q4-6h prn", max_dose="3000 mg/d (CKD)",
                 ckd_notes_th="ยาแก้ปวดที่ปลอดภัยที่สุดใน CKD — ลดขนาดสูงสุด"),

        # ── Antibiotics (common in CKD) ──
        DrugInfo("amoxicillin", DrugClass.ANTIBIOTIC.value,
                 thai_name="อะม็อกซีซิลลิน", brand_names=["Amoxil"],
                 usual_dose="500 mg TID", max_dose="3000 mg/d",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 10, "action": "extend_interval",
                      "dose_th": "250-500 mg q12h"},
                     {"egfr_min": 10, "egfr_max": 30, "action": "extend_interval",
                      "dose_th": "250-500 mg q8-12h"},
                     {"egfr_min": 30, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ขนาดปกติ"},
                 ]),
        DrugInfo("ciprofloxacin", DrugClass.ANTIBIOTIC.value,
                 thai_name="ซิโปรฟลอกซาซิน", brand_names=["Cipro"],
                 usual_dose="250-500 mg BID",
                 egfr_adjustments=[
                     {"egfr_min": 0, "egfr_max": 30, "action": "reduce_50",
                      "dose_th": "ลดขนาด 50% หรือยืดช่วงเป็น q24h"},
                     {"egfr_min": 30, "egfr_max": 999, "action": "no_change",
                      "dose_th": "ใช้ขนาดปกติ"},
                 ]),

        # ── Immunosuppressants (for glomerulonephritis/transplant) ──
        DrugInfo("prednisolone", DrugClass.IMMUNOSUPPRESSANT.value,
                 thai_name="เพร็ดนิโซโลน", brand_names=["Prednisolone"],
                 usual_dose="ขึ้นกับข้อบ่งชี้",
                 ckd_notes_th="ไม่ต้องปรับขนาดตาม eGFR — ระวัง infection/glucose/bone"),
        DrugInfo("mycophenolate", DrugClass.IMMUNOSUPPRESSANT.value,
                 thai_name="ไมโคฟีโนเลท", brand_names=["CellCept", "Myfortic"],
                 aliases=["mmf", "mycophenolate mofetil", "mycophenolic acid"],
                 usual_dose="500-1500 mg BID",
                 ckd_notes_th="ใช้ใน glomerulonephritis/transplant — ติดตาม CBC"),
        DrugInfo("cyclosporine", DrugClass.IMMUNOSUPPRESSANT.value,
                 thai_name="ไซโคลสปอริน", brand_names=["Neoral", "Sandimmun"],
                 usual_dose="ขึ้นกับระดับยาในเลือด",
                 is_nephrotoxic=True,
                 ckd_notes_th="Nephrotoxic! — ต้องติดตามระดับยาและ Cr อย่างใกล้ชิด"),
        DrugInfo("tacrolimus", DrugClass.IMMUNOSUPPRESSANT.value,
                 thai_name="แทคโครลิมัส", brand_names=["Prograf"],
                 usual_dose="ขึ้นกับระดับยาในเลือด",
                 is_nephrotoxic=True,
                 ckd_notes_th="Nephrotoxic! — ต้องติดตามระดับยาและ Cr อย่างใกล้ชิด"),
    ]

    db: dict[str, DrugInfo] = {}
    for drug in drugs:
        key = drug.generic_name.lower()
        db[key] = drug
        for alias in drug.aliases:
            db[alias.lower()] = drug
    return db


# Build on import
NEPHROLOGY_DRUG_DB = _build_drug_db()


# ═══════════════════════════════════════════════════════════
# Drug-Drug Interaction Database
# ═══════════════════════════════════════════════════════════

# Key = frozenset({drug_a, drug_b}) → DrugInteraction
_INTERACTION_RULES: list[dict] = [
    # ── ACEI + ARB (Dual RAAS blockade) ──
    {
        "drugs": (
            ["enalapril", "ramipril", "lisinopril", "captopril", "perindopril"],
            ["losartan", "valsartan", "irbesartan", "candesartan", "telmisartan", "olmesartan"],
        ),
        "severity": InteractionSeverity.MAJOR.value,
        "mechanism_th": "Dual RAAS blockade — บล็อค RAAS 2 ทาง",
        "effect_th": "เสี่ยง hyperkalemia, AKI, hypotension สูง",
        "management_th": "หลีกเลี่ยงการใช้ ACEI + ARB ร่วมกัน",
        "monitoring": "K+, Cr, BP",
    },
    # ── ACEI/ARB + K+-sparing diuretic ──
    {
        "drugs": (
            ["enalapril", "ramipril", "lisinopril", "losartan", "valsartan",
             "irbesartan", "candesartan", "captopril", "perindopril",
             "telmisartan", "olmesartan"],
            ["spironolactone", "amiloride", "triamterene", "eplerenone"],
        ),
        "severity": InteractionSeverity.MODERATE.value,
        "mechanism_th": "ทั้งสองกลุ่มเพิ่ม K+ — เสี่ยง hyperkalemia",
        "effect_th": "Hyperkalemia โดยเฉพาะใน CKD",
        "management_th": "ติดตาม K+ ทุก 1-2 สัปดาห์แรก — ลดขนาดถ้า K+ > 5.5",
        "monitoring": "K+, Cr",
    },
    # ── ACEI/ARB + NSAIDs ──
    {
        "drugs": (
            ["enalapril", "ramipril", "lisinopril", "losartan", "valsartan",
             "irbesartan", "candesartan", "captopril"],
            ["ibuprofen", "naproxen", "diclofenac", "piroxicam",
             "indomethacin", "mefenamic acid", "celecoxib", "meloxicam", "ketorolac"],
        ),
        "severity": InteractionSeverity.MAJOR.value,
        "mechanism_th": "NSAIDs ลดฤทธิ์ ACEI/ARB + เพิ่ม nephrotoxicity",
        "effect_th": "ลดการลดความดัน, เพิ่มเสี่ยง AKI, hyperkalemia (Triple Whammy)",
        "management_th": "หลีกเลี่ยง NSAIDs ในผู้ป่วย CKD ที่ใช้ ACEI/ARB",
        "monitoring": "Cr, K+, BP",
    },
    # ── Digoxin + Amiodarone ──
    {
        "drugs": (["digoxin"], ["amiodarone"]),
        "severity": InteractionSeverity.MAJOR.value,
        "mechanism_th": "Amiodarone เพิ่มระดับ Digoxin ในเลือด",
        "effect_th": "Digoxin toxicity — คลื่นไส้ อาเจียน หัวใจเต้นผิดจังหวะ",
        "management_th": "ลดขนาด Digoxin 50% เมื่อเริ่ม Amiodarone — ตรวจระดับยา",
        "monitoring": "Digoxin level, ECG, K+",
    },
    # ── Metformin + Contrast ──
    {
        "drugs": (["metformin"], ["iodinated contrast", "contrast media"]),
        "severity": InteractionSeverity.MAJOR.value,
        "mechanism_th": "Contrast + Metformin เพิ่มเสี่ยง lactic acidosis",
        "effect_th": "Lactic acidosis — โดยเฉพาะถ้า AKI หลัง contrast",
        "management_th": "หยุด Metformin 48 ชม. ก่อน/หลังได้ contrast — ตรวจ Cr ก่อนเริ่มยาใหม่",
        "monitoring": "Cr, lactate",
    },
    # ── Cyclosporine + Tacrolimus ──
    {
        "drugs": (["cyclosporine"], ["tacrolimus"]),
        "severity": InteractionSeverity.CONTRAINDICATED.value,
        "mechanism_th": "ทั้งสองเป็น calcineurin inhibitor — nephrotoxicity เพิ่ม",
        "effect_th": "Nephrotoxicity รุนแรง — ห้ามใช้ร่วมกัน",
        "management_th": "ใช้อย่างใดอย่างหนึ่ง — ห้ามให้ร่วมกัน",
        "monitoring": "Cr, drug levels",
    },
    # ── Statin + Cyclosporine ──
    {
        "drugs": (
            ["atorvastatin", "rosuvastatin", "simvastatin"],
            ["cyclosporine"],
        ),
        "severity": InteractionSeverity.MAJOR.value,
        "mechanism_th": "Cyclosporine เพิ่มระดับ statin ผ่าน CYP3A4/OATP",
        "effect_th": "เสี่ยง rhabdomyolysis สูง",
        "management_th": "ใช้ statin ขนาดต่ำ — fluvastatin หรือ pravastatin ปลอดภัยกว่า",
        "monitoring": "CK, LFT",
    },
    # ── ESA + Iron ──
    {
        "drugs": (
            ["epoetin alfa", "darbepoetin alfa"],
            ["iron sucrose", "ferrous sulfate", "ferric carboxymaltose"],
        ),
        "severity": InteractionSeverity.MINOR.value,
        "mechanism_th": "ESA ต้องการ iron เพียงพอจึงจะออกฤทธิ์ได้ดี",
        "effect_th": "ถ้า iron ไม่พอ ESA จะไม่ได้ผล — functional iron deficiency",
        "management_th": "ให้ iron เสริมเมื่อ TSAT < 20% หรือ Ferritin < 200",
        "monitoring": "TSAT, Ferritin, Hb",
    },
    # ── Calcium-based phosphate binder + Calcitriol ──
    {
        "drugs": (
            ["calcium carbonate", "calcium acetate"],
            ["calcitriol", "alfacalcidol"],
        ),
        "severity": InteractionSeverity.MODERATE.value,
        "mechanism_th": "ทั้งสองเพิ่ม calcium — เสี่ยง hypercalcemia",
        "effect_th": "Hypercalcemia, metastatic calcification",
        "management_th": "ติดตาม Ca ทุก 2-4 สัปดาห์ — พิจารณา non-calcium binder",
        "monitoring": "Ca, P, iPTH",
    },
    # ── SGLT2i + Diuretics ──
    {
        "drugs": (
            ["empagliflozin", "dapagliflozin", "canagliflozin"],
            ["furosemide", "hydrochlorothiazide", "bumetanide"],
        ),
        "severity": InteractionSeverity.MODERATE.value,
        "mechanism_th": "ทั้งสองเพิ่มการขับน้ำ — เสี่ยง dehydration",
        "effect_th": "Hypotension, dehydration, AKI",
        "management_th": "ลดขนาด diuretic เมื่อเริ่ม SGLT2i — ติดตามอาการ",
        "monitoring": "BP, Cr, volume status",
    },
]


def _normalize_for_lookup(name: str) -> str:
    """Normalize ชื่อยาสำหรับค้นหา."""
    return name.strip().lower()


def lookup_drug(name: str) -> Optional[DrugInfo]:
    """ค้นหายาในฐานข้อมูล.

    Args:
        name: ชื่อยา (generic, brand, alias)

    Returns:
        DrugInfo or None
    """
    key = _normalize_for_lookup(name)
    # Direct match
    if key in NEPHROLOGY_DRUG_DB:
        return NEPHROLOGY_DRUG_DB[key]

    # Search by brand name — dedup by id (DrugInfo has mutable fields)
    seen: set[int] = set()
    for drug in NEPHROLOGY_DRUG_DB.values():
        if id(drug) in seen:
            continue
        seen.add(id(drug))
        for brand in drug.brand_names:
            if brand.lower() == key:
                return drug
        # Partial match on generic name
        if key in drug.generic_name.lower() or drug.generic_name.lower() in key:
            return drug

    return None


def get_dose_recommendation(
    drug_name: str,
    egfr: float,
) -> Optional[DoseRecommendation]:
    """คำแนะนำการปรับขนาดยาตาม eGFR.

    Args:
        drug_name: ชื่อยา
        egfr: ค่า eGFR

    Returns:
        DoseRecommendation or None

    Safety: ผลเป็นข้อมูลประกอบ — ห้ามใช้สั่งยาอัตโนมัติ
    """
    drug = lookup_drug(drug_name)
    if not drug or not drug.egfr_adjustments:
        return None

    for adj in drug.egfr_adjustments:
        if adj["egfr_min"] <= egfr < adj["egfr_max"]:
            return DoseRecommendation(
                drug_name=drug.generic_name,
                egfr=egfr,
                action=adj["action"],
                recommended_dose_th=adj["dose_th"],
                reason_th=drug.ckd_notes_th,
                monitoring_th="ติดตาม Cr, K+ หลังเริ่มหรือปรับยา"
                if drug.drug_class == DrugClass.ANTIHYPERTENSIVE.value
                else "",
            )

    return None


def check_drug_interactions(
    medications: list[str],
) -> list[DrugInteraction]:
    """ตรวจสอบอันตรกิริยาระหว่างยาทุกคู่.

    Args:
        medications: รายการชื่อยา

    Returns:
        list ของ DrugInteraction

    Safety: ผลเป็นข้อมูลประกอบ — ไม่ใช่การสั่งยา
    """
    if len(medications) < 2:
        return []

    # Normalize
    meds_lower = [_normalize_for_lookup(m) for m in medications]
    interactions: list[DrugInteraction] = []
    seen: set[tuple[str, str]] = set()

    for rule in _INTERACTION_RULES:
        group_a, group_b = rule["drugs"]

        # Find matches in patient's medication list
        matched_a: list[str] = []
        matched_b: list[str] = []

        for i, med in enumerate(meds_lower):
            for drug_a in group_a:
                if drug_a in med or med in drug_a:
                    matched_a.append(medications[i])
                    break
            for drug_b in group_b:
                if drug_b in med or med in drug_b:
                    matched_b.append(medications[i])
                    break

        # Generate interactions for all matching pairs
        for a in matched_a:
            for b in matched_b:
                pair_key = tuple(sorted([a.lower(), b.lower()]))
                if pair_key not in seen:
                    seen.add(pair_key)
                    interactions.append(DrugInteraction(
                        drug_a=a,
                        drug_b=b,
                        severity=rule["severity"],
                        mechanism_th=rule["mechanism_th"],
                        effect_th=rule["effect_th"],
                        management_th=rule["management_th"],
                        monitoring=rule.get("monitoring", ""),
                    ))

    # Sort by severity
    severity_order = {
        InteractionSeverity.CONTRAINDICATED.value: 0,
        InteractionSeverity.MAJOR.value: 1,
        InteractionSeverity.MODERATE.value: 2,
        InteractionSeverity.MINOR.value: 3,
    }
    interactions.sort(key=lambda x: severity_order.get(x.severity, 99))
    return interactions


# ═══════════════════════════════════════════════════════════
# Main Review Function
# ═══════════════════════════════════════════════════════════

def review_ckd_medications(
    medications: list[str],
    egfr: float,
    owner_hash: str = "",
    ckd_stage: str = "",
) -> CKDMedicationSummary:
    """ตรวจสอบยาทั้งหมดของผู้ป่วย CKD — entry point หลัก.

    Args:
        medications: รายการชื่อยา
        egfr: ค่า eGFR ล่าสุด
        owner_hash: hash ของเจ้าของ
        ckd_stage: CKD Stage (ถ้ามี)

    Returns:
        CKDMedicationSummary

    Safety:
        - ผลเป็นข้อมูลประกอบ — ห้ามใช้สั่งยา
        - Rule engine only — ไม่ใช้ AI ภายนอก
    """
    from ckd_progression import get_ckd_stage  # avoid circular
    if not ckd_stage:
        ckd_stage = get_ckd_stage(egfr)

    review_date = datetime.now().strftime("%Y-%m-%d")

    # 1. Dose recommendations for each drug
    dose_recs: list[DoseRecommendation] = []
    contraindicated: list[str] = []
    needs_monitoring: list[str] = []

    for med in medications:
        rec = get_dose_recommendation(med, egfr)
        if rec:
            dose_recs.append(rec)
            if rec.action in (DoseAdjustAction.CONTRAINDICATED.value,
                              DoseAdjustAction.AVOID.value):
                contraindicated.append(f"{rec.drug_name}: {rec.recommended_dose_th}")
            elif rec.action == DoseAdjustAction.MONITOR.value:
                needs_monitoring.append(rec.drug_name)

        # Also check nephrotoxicity from DB
        drug = lookup_drug(med)
        if drug and drug.is_nephrotoxic:
            needs_monitoring.append(f"{drug.generic_name} (nephrotoxic)")

    # 2. Drug-drug interactions
    interactions = check_drug_interactions(medications)

    return CKDMedicationSummary(
        owner_hash=owner_hash,
        review_date=review_date,
        egfr=egfr,
        ckd_stage=ckd_stage,
        medications=medications,
        dose_recommendations=dose_recs,
        interactions=interactions,
        contraindicated=contraindicated,
        needs_monitoring=list(set(needs_monitoring)),
    )


# ═══════════════════════════════════════════════════════════
# LINE Summary Generation
# ═══════════════════════════════════════════════════════════

def _generate_medication_summary(summary: CKDMedicationSummary) -> str:
    """สร้างข้อความสรุปยาภาษาไทยสำหรับ LINE."""
    lines = [
        "💊 สรุปการตรวจสอบยา — CKD Medication Review",
        f"วันที่: {summary.review_date}",
        f"eGFR: {summary.egfr:.0f} | Stage: {summary.ckd_stage}",
        f"จำนวนยา: {len(summary.medications)} รายการ",
        "",
    ]

    # Contraindicated drugs
    if summary.contraindicated:
        lines.append("🔴 ยาที่ห้ามใช้/ควรหลีกเลี่ยง:")
        for c in summary.contraindicated:
            lines.append(f"   • {c}")
        lines.append("")

    # Drug interactions
    critical_interactions = [
        i for i in summary.interactions
        if i.severity in (InteractionSeverity.CONTRAINDICATED.value,
                          InteractionSeverity.MAJOR.value)
    ]
    moderate_interactions = [
        i for i in summary.interactions
        if i.severity == InteractionSeverity.MODERATE.value
    ]

    if critical_interactions:
        lines.append("⚠️ อันตรกิริยายาที่สำคัญ:")
        for ix in critical_interactions:
            sev_text = "ห้ามใช้ร่วม" if ix.severity == InteractionSeverity.CONTRAINDICATED.value else "อันตราย"
            lines.append(f"   🔴 {ix.drug_a} + {ix.drug_b} [{sev_text}]")
            lines.append(f"      ผล: {ix.effect_th}")
            lines.append(f"      → {ix.management_th}")
        lines.append("")

    if moderate_interactions:
        lines.append("🟡 อันตรกิริยายาที่ต้องระวัง:")
        for ix in moderate_interactions:
            lines.append(f"   • {ix.drug_a} + {ix.drug_b}")
            lines.append(f"     → {ix.management_th}")
        lines.append("")

    # Dose adjustments needed
    adjustments = [
        r for r in summary.dose_recommendations
        if r.action not in (DoseAdjustAction.NO_CHANGE.value,
                            DoseAdjustAction.CONTRAINDICATED.value,
                            DoseAdjustAction.AVOID.value)
    ]
    if adjustments:
        lines.append("📋 การปรับขนาดยาตาม eGFR:")
        for r in adjustments:
            lines.append(f"   • {r.drug_name}: {r.recommended_dose_th}")
        lines.append("")

    # Monitoring needed
    if summary.needs_monitoring:
        lines.append("🔍 ยาที่ต้องติดตาม:")
        for m in summary.needs_monitoring:
            lines.append(f"   • {m}")
        lines.append("")

    lines.append(summary.disclaimer)
    return "\n".join(lines)
