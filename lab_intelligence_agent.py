#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lab Intelligence Agent — AI Interagent Platform

Agent เฉพาะทางสำหรับวิเคราะห์ผลแล็บเชิงลึก:
1. Trend Analysis — ดูแนวโน้มค่าแล็บย้อนหลัง
2. KDIGO Clinical Assessment — ประเมินตาม guideline
3. ESA-Hb Optimization — แนะนำปรับขนาดยา ESA
4. Cross-lab Correlation — ค่าแล็บหลายตัวสัมพันธ์กัน
5. Clinical Recommendations — คำแนะนำตาม guideline (ต้องแพทย์ approve)

Safety:
  - ผลวิเคราะห์ทั้งหมดเป็น "ข้อมูลประกอบ" ต้องแพทย์ approve
  - ห้ามสั่งยา/เปลี่ยนยา/สั่งการรักษาอัตโนมัติ
  - ไม่เก็บข้อมูลระบุตัวผู้ป่วย (ใช้ HN เท่านั้น)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("lab_intelligence")


# ─────────────────────────────────────────────────────
# Constants — KDIGO Guidelines
# ─────────────────────────────────────────────────────

# Hb targets (KDIGO 2012 Anemia in CKD)
HB_TARGET_LOW = 10.0   # g/dL — ไม่ควรเริ่ม ESA ถ้า Hb > 10
HB_TARGET_HIGH = 12.0  # g/dL — ไม่ควรให้ Hb สูงเกิน 13 (ลด ESA)
HB_CRITICAL_LOW = 7.0  # g/dL — พิจารณาให้เลือด
HB_AVOID_ABOVE = 13.0  # g/dL — ลด/หยุด ESA

# Iron targets (KDIGO)
FERRITIN_LOW_HD = 200    # ng/mL — HD patients
FERRITIN_HIGH = 500      # ng/mL — ไม่ควรให้ IV iron ถ้า > 500
TSAT_LOW = 20            # % — ต่ำกว่านี้ = iron deficiency
TSAT_HIGH = 50           # %

# Electrolytes
K_CRITICAL_HIGH = 6.5    # mEq/L
K_HIGH = 5.5
K_LOW = 3.5
K_CRITICAL_LOW = 3.0

PO4_HIGH = 5.5           # mg/dL (HD patients)
PO4_LOW = 2.5
CA_HIGH = 10.5           # mg/dL
CA_LOW = 8.4

# CKD progression
EGFR_RAPID_DECLINE = 5.0  # mL/min/year — rapid decline threshold

# ESA dose reference (units/week)
ESA_DOSE_MAP = {
    "E4": {"name": "Epiao", "dose_per_injection": 4000, "unit": "unit"},
    "H4": {"name": "Hemax", "dose_per_injection": 4000, "unit": "unit"},
    "HM": {"name": "Hema-plus", "dose_per_injection": 4000, "unit": "unit"},
    "HM4": {"name": "Hema-plus", "dose_per_injection": 4000, "unit": "unit"},
    "H4+H10": {"name": "Hemax", "dose_per_injection": 14000, "unit": "unit"},
    "hypercrit": {"name": "Hypercrit", "dose_per_injection": 4000, "unit": "unit"},
    "eposis": {"name": "Eposis", "dose_per_injection": 4000, "unit": "unit"},
    "Eposis": {"name": "Eposis", "dose_per_injection": 4000, "unit": "unit"},
}


# ─────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────

@dataclass
class TrendPoint:
    """จุดข้อมูลใน trend"""
    date: str
    value: float
    doc_id: str = ""


@dataclass
class TrendAnalysis:
    """ผลวิเคราะห์แนวโน้ม"""
    analyte: str
    current_value: float
    previous_value: float | None
    change: float | None           # delta
    change_percent: float | None   # % change
    direction: str                 # "rising", "falling", "stable", "insufficient"
    points: list[TrendPoint] = field(default_factory=list)
    assessment: str = ""           # Thai description


@dataclass
class ESARecommendation:
    """คำแนะนำปรับ ESA (ต้องแพทย์ approve)"""
    current_esa: str
    current_dose: str
    current_hb: float
    hb_trend: str
    recommendation: str       # Thai recommendation
    action: str               # "increase", "decrease", "maintain", "start", "stop", "transfuse"
    reason: str               # Thai reason
    target_hb: str = "10-12 g/dL"
    guideline: str = "KDIGO 2012 Anemia"
    iron_status: str = ""     # Iron assessment if available
    disclaimer: str = "คำแนะนำนี้เป็นข้อมูลประกอบ ต้องแพทย์พิจารณาและอนุมัติ"


@dataclass
class LabIntelligenceReport:
    """รายงานวิเคราะห์แล็บเชิงลึก"""
    patient_hn: str
    generated_at: str = ""
    trends: list[TrendAnalysis] = field(default_factory=list)
    esa_recommendation: ESARecommendation | None = None
    clinical_flags: list[str] = field(default_factory=list)
    next_lab_suggestions: list[str] = field(default_factory=list)
    summary_th: str = ""

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────
# Trend Analysis Engine
# ─────────────────────────────────────────────────────

def analyze_trend(values: list[dict], analyte: str = "") -> TrendAnalysis:
    """วิเคราะห์แนวโน้มจาก list of {date, value, doc_id}

    values ควรเรียงจากเก่า → ใหม่
    """
    if not values or len(values) < 1:
        return TrendAnalysis(
            analyte=analyte, current_value=0, previous_value=None,
            change=None, change_percent=None, direction="insufficient",
        )

    current = values[-1]
    current_val = float(current.get("value", 0))

    points = [
        TrendPoint(
            date=v.get("date", v.get("specimen_date", "")),
            value=float(v.get("value", 0)),
            doc_id=v.get("doc_id", ""),
        )
        for v in values
    ]

    if len(values) < 2:
        return TrendAnalysis(
            analyte=analyte, current_value=current_val,
            previous_value=None, change=None, change_percent=None,
            direction="insufficient", points=points,
            assessment="มีข้อมูลเพียงครั้งเดียว ยังเปรียบเทียบไม่ได้",
        )

    prev_val = float(values[-2].get("value", 0))
    delta = current_val - prev_val
    pct = (delta / prev_val * 100) if prev_val != 0 else 0

    # Direction
    if abs(pct) < 5:
        direction = "stable"
    elif delta > 0:
        direction = "rising"
    else:
        direction = "falling"

    # Slope analysis (if >= 3 points)
    slope_desc = ""
    if len(values) >= 3:
        recent_3 = [float(v.get("value", 0)) for v in values[-3:]]
        if all(recent_3[i] < recent_3[i+1] for i in range(len(recent_3)-1)):
            slope_desc = "เพิ่มขึ้นต่อเนื่อง 3 ครั้ง"
        elif all(recent_3[i] > recent_3[i+1] for i in range(len(recent_3)-1)):
            slope_desc = "ลดลงต่อเนื่อง 3 ครั้ง"

    # Assessment text
    arrow = {"rising": "↑", "falling": "↓", "stable": "→"}.get(direction, "")
    assessment = f"{current_val:.1f} {arrow} (เปลี่ยน {delta:+.1f}, {pct:+.1f}%)"
    if slope_desc:
        assessment += f" — {slope_desc}"

    return TrendAnalysis(
        analyte=analyte, current_value=current_val,
        previous_value=prev_val, change=delta, change_percent=pct,
        direction=direction, points=points, assessment=assessment,
    )


# ─────────────────────────────────────────────────────
# ESA-Hb Optimization Engine (KDIGO 2012)
# ─────────────────────────────────────────────────────

def analyze_esa_response(
    hb_history: list[dict],
    esa_code: str = "",
    esa_medication: str = "",
    esa_dose: int = 0,
    ferritin: float | None = None,
    tsat: float | None = None,
) -> ESARecommendation:
    """วิเคราะห์ Hb response ต่อ ESA → แนะนำปรับขนาด

    ตาม KDIGO 2012 Clinical Practice Guideline for Anemia in CKD:
    - Target Hb: 10-12 g/dL (ไม่เกิน 13)
    - เริ่ม ESA เมื่อ Hb < 10 g/dL
    - ลด ESA เมื่อ Hb > 13 g/dL
    - ปรับ dose ≤25% ต่อครั้ง
    - ตรวจ iron status ก่อนเริ่ม/เพิ่ม ESA

    Returns: ESARecommendation
    """
    esa_name = esa_medication or ESA_DOSE_MAP.get(esa_code, {}).get("name", esa_code) or "ไม่ทราบ"
    dose_text = f"{esa_dose} unit" if esa_dose else esa_code or "ไม่ทราบ"

    # Get current Hb
    if not hb_history:
        return ESARecommendation(
            current_esa=esa_name, current_dose=dose_text,
            current_hb=0, hb_trend="insufficient",
            recommendation="ไม่มีข้อมูล Hb — ควรเจาะ CBC",
            action="check", reason="ไม่มีข้อมูล Hb ในระบบ",
        )

    hb_trend = analyze_trend(hb_history, analyte="Hb")
    current_hb = hb_trend.current_value

    # Iron status assessment
    iron_status = _assess_iron(ferritin, tsat)

    # ── Decision Logic (KDIGO 2012) ──

    # Critical low — consider transfusion
    if current_hb < HB_CRITICAL_LOW:
        return ESARecommendation(
            current_esa=esa_name, current_dose=dose_text,
            current_hb=current_hb, hb_trend=hb_trend.direction,
            recommendation=(
                f"🔴 Hb {current_hb:.1f} ต่ำวิกฤต!\n"
                f"พิจารณาให้เลือด (PRC transfusion)\n"
                f"ถ้ามี iron deficiency → ให้ IV iron ร่วมด้วย"
            ),
            action="transfuse",
            reason=f"Hb {current_hb:.1f} < {HB_CRITICAL_LOW} g/dL (วิกฤต)",
            iron_status=iron_status,
        )

    # Too high — reduce/stop ESA
    if current_hb > HB_AVOID_ABOVE:
        return ESARecommendation(
            current_esa=esa_name, current_dose=dose_text,
            current_hb=current_hb, hb_trend=hb_trend.direction,
            recommendation=(
                f"⚠️ Hb {current_hb:.1f} สูงเกินเป้า\n"
                f"พิจารณาลด ESA 25% หรือหยุดชั่วคราว\n"
                f"Hb > 13 เพิ่มความเสี่ยง thromboembolic events"
            ),
            action="decrease",
            reason=f"Hb {current_hb:.1f} > {HB_AVOID_ABOVE} g/dL (KDIGO: avoid > 13)",
            iron_status=iron_status,
        )

    # Below target — consider increasing
    if current_hb < HB_TARGET_LOW:
        # Check iron first
        if ferritin is not None and ferritin < FERRITIN_LOW_HD:
            return ESARecommendation(
                current_esa=esa_name, current_dose=dose_text,
                current_hb=current_hb, hb_trend=hb_trend.direction,
                recommendation=(
                    f"Hb {current_hb:.1f} ต่ำกว่าเป้า + Iron deficiency\n"
                    f"Ferritin {ferritin:.0f} < {FERRITIN_LOW_HD}\n"
                    f"→ ให้ IV iron ก่อนเพิ่ม ESA\n"
                    f"เป้าหมาย: Ferritin 200-500, TSAT 20-50%"
                ),
                action="iron_first",
                reason="Iron deficiency — ต้องแก้ก่อนเพิ่ม ESA",
                iron_status=iron_status,
            )

        # ESA not started
        if not esa_code and not esa_medication:
            return ESARecommendation(
                current_esa="ยังไม่ได้เริ่ม", current_dose="-",
                current_hb=current_hb, hb_trend=hb_trend.direction,
                recommendation=(
                    f"Hb {current_hb:.1f} ต่ำกว่าเป้า\n"
                    f"พิจารณาเริ่ม ESA (target Hb 10-12)\n"
                    f"ตรวจ iron status ก่อนเริ่ม"
                ),
                action="start",
                reason=f"Hb {current_hb:.1f} < {HB_TARGET_LOW} + ยังไม่ได้รับ ESA",
                iron_status=iron_status,
            )

        # ESA started but Hb still low
        if hb_trend.direction == "falling":
            return ESARecommendation(
                current_esa=esa_name, current_dose=dose_text,
                current_hb=current_hb, hb_trend=hb_trend.direction,
                recommendation=(
                    f"Hb {current_hb:.1f} ↓ ต่ำลงแม้ได้ {esa_name}\n"
                    f"พิจารณาเพิ่ม ESA 25% (ไม่เกิน 50%)\n"
                    f"หรือตรวจหาสาเหตุ: inflammation, blood loss, iron deficiency"
                ),
                action="increase",
                reason=f"Hb ↓ falling + ยังต่ำกว่า {HB_TARGET_LOW}",
                iron_status=iron_status,
            )

        return ESARecommendation(
            current_esa=esa_name, current_dose=dose_text,
            current_hb=current_hb, hb_trend=hb_trend.direction,
            recommendation=(
                f"Hb {current_hb:.1f} ต่ำกว่าเป้า (10-12)\n"
                f"พิจารณาเพิ่ม ESA 25%\n"
                f"ตรวจ Hb ซ้ำใน 2-4 สัปดาห์"
            ),
            action="increase",
            reason=f"Hb {current_hb:.1f} < {HB_TARGET_LOW}",
            iron_status=iron_status,
        )

    # In target range
    if HB_TARGET_LOW <= current_hb <= HB_TARGET_HIGH:
        return ESARecommendation(
            current_esa=esa_name, current_dose=dose_text,
            current_hb=current_hb, hb_trend=hb_trend.direction,
            recommendation=(
                f"✅ Hb {current_hb:.1f} อยู่ในเป้าหมาย (10-12)\n"
                f"คง {esa_name} {dose_text} เดิม\n"
                f"ตรวจ Hb ทุก 1-3 เดือน"
            ),
            action="maintain",
            reason="Hb อยู่ใน target range",
            iron_status=iron_status,
        )

    # 12 < Hb <= 13 — approaching ceiling
    return ESARecommendation(
        current_esa=esa_name, current_dose=dose_text,
        current_hb=current_hb, hb_trend=hb_trend.direction,
        recommendation=(
            f"Hb {current_hb:.1f} ใกล้เพดาน\n"
            f"{'ถ้า Hb ยังเพิ่มขึ้น → ลด ESA 25%' if hb_trend.direction == 'rising' else 'ติดตามใกล้ชิด'}\n"
            f"เป้าหมาย: ไม่เกิน 13 g/dL"
        ),
        action="decrease" if hb_trend.direction == "rising" else "maintain",
        reason=f"Hb {current_hb:.1f} ใกล้เพดาน 13",
        iron_status=iron_status,
    )


def _assess_iron(ferritin: float | None, tsat: float | None) -> str:
    """ประเมิน iron status สำหรับผู้ป่วย HD"""
    parts = []
    if ferritin is not None:
        if ferritin < FERRITIN_LOW_HD:
            parts.append(f"Ferritin {ferritin:.0f} ↓ (ต่ำ, เป้า ≥{FERRITIN_LOW_HD})")
        elif ferritin > FERRITIN_HIGH:
            parts.append(f"Ferritin {ferritin:.0f} ↑ (สูง, ไม่ควรให้ IV iron)")
        else:
            parts.append(f"Ferritin {ferritin:.0f} ✓")

    if tsat is not None:
        if tsat < TSAT_LOW:
            parts.append(f"TSAT {tsat:.0f}% ↓ (ต่ำ, เป้า ≥{TSAT_LOW}%)")
        elif tsat > TSAT_HIGH:
            parts.append(f"TSAT {tsat:.0f}% ↑ (สูง)")
        else:
            parts.append(f"TSAT {tsat:.0f}% ✓")

    if not parts:
        return "ไม่มีข้อมูล iron status"
    return " | ".join(parts)


# ─────────────────────────────────────────────────────
# Cross-Lab Correlation
# ─────────────────────────────────────────────────────

def check_cross_correlations(labs: dict) -> list[str]:
    """ตรวจความสัมพันธ์ระหว่างค่าแล็บหลายตัว

    Args:
        labs: dict of {analyte: value} e.g. {"hb": 9.5, "k": 5.8, ...}

    Returns: list of clinical flags (Thai)
    """
    flags = []

    hb = labs.get("hb") or labs.get("hemoglobin")
    k = labs.get("k") or labs.get("potassium")
    ca = labs.get("ca") or labs.get("calcium")
    po4 = labs.get("po4") or labs.get("phosphorus") or labs.get("phosphate")
    pth = labs.get("pth")
    albumin = labs.get("albumin") or labs.get("alb")
    creatinine = labs.get("cr") or labs.get("creatinine")
    egfr = labs.get("egfr")
    ferritin = labs.get("ferritin")
    tsat = labs.get("tsat")
    bun = labs.get("bun")

    # 1. Anemia + Iron deficiency
    if hb and hb < HB_TARGET_LOW:
        if ferritin and ferritin < FERRITIN_LOW_HD:
            flags.append("⚠️ โลหิตจาง + Iron deficiency — ควรให้ IV iron ก่อนเพิ่ม ESA")
        elif tsat and tsat < TSAT_LOW:
            flags.append("⚠️ โลหิตจาง + TSAT ต่ำ — functional iron deficiency")

    # 2. Hyperkalemia
    if k and k >= K_CRITICAL_HIGH:
        flags.append(f"🔴 K+ {k:.1f} วิกฤต! — ECG + calcium gluconate + kayexalate")
    elif k and k >= K_HIGH:
        flags.append(f"⚠️ K+ {k:.1f} สูง — ตรวจอาหาร, ตรวจ ACEi/ARB dose")

    # 3. Ca × PO4 product
    if ca and po4:
        ca_po4 = ca * po4
        if ca_po4 > 55:
            flags.append(
                f"🔴 Ca×PO4 = {ca_po4:.1f} (>{55}) — ความเสี่ยง calcification สูง"
            )

    # 4. Secondary hyperparathyroidism
    if po4 and po4 > PO4_HIGH:
        flags.append(f"⚠️ PO4 {po4:.1f} สูง — พิจารณา phosphate binder")
    if pth and pth > 600:
        flags.append(f"⚠️ PTH {pth:.0f} สูงมาก — พิจารณา calcimimetics/parathyroidectomy")
    elif pth and pth > 300:
        flags.append(f"PTH {pth:.0f} สูง — ติดตามใกล้ชิด")

    # 5. Malnutrition
    if albumin and albumin < 3.5:
        flags.append(f"⚠️ Albumin {albumin:.1f} ต่ำ — ภาวะโภชนาการไม่ดี")
        if albumin < 3.0:
            flags.append(f"🔴 Albumin {albumin:.1f} ต่ำมาก — protein-energy wasting")

    # 6. BUN/Cr ratio
    if bun and creatinine and creatinine > 0:
        ratio = bun / creatinine
        if ratio > 20:
            flags.append(f"BUN/Cr ratio {ratio:.0f} สูง — อาจมี GI bleeding/dehydration")

    # 7. eGFR decline concern
    if egfr and egfr < 15 and not (hb and hb < HB_TARGET_LOW):
        flags.append(f"eGFR {egfr:.0f} — CKD G5 ควรเตรียม vascular access ถ้ายังไม่มี")

    return flags


# ─────────────────────────────────────────────────────
# CKD Progression Tracker
# ─────────────────────────────────────────────────────

@dataclass
class CKDProgressionReport:
    """รายงานการดำเนินโรค CKD จาก eGFR slope"""
    current_egfr: float
    ckd_stage: str
    egfr_slope: float | None         # mL/min/year (negative = declining)
    decline_rate: str                 # "rapid", "moderate", "stable", "improving"
    months_to_dialysis: float | None  # ถ้า slope negative + eGFR > 15
    risk_assessment: str              # Thai description
    egfr_history: list[TrendPoint] = field(default_factory=list)


def _classify_ckd(egfr: float) -> str:
    if egfr >= 90: return "G1"
    if egfr >= 60: return "G2"
    if egfr >= 45: return "G3a"
    if egfr >= 30: return "G3b"
    if egfr >= 15: return "G4"
    return "G5"


# ── Canonical CKD stage classification (Phase 2 Consolidation) ──
# ใช้ร่วมกันทุก module: ckd_staging, ckd_progression, clinical_analyzer
_CKD_STAGE_INFO = {
    "G1": {"description": "ปกติหรือสูง", "color": "green", "egfr_range": "≥90"},
    "G2": {"description": "ลดลงเล็กน้อย", "color": "yellow", "egfr_range": "60-89"},
    "G3a": {"description": "ลดลงเล็กน้อยถึงปานกลาง", "color": "orange", "egfr_range": "45-59"},
    "G3b": {"description": "ลดลงปานกลางถึงรุนแรง", "color": "orange", "egfr_range": "30-44"},
    "G4": {"description": "ลดลงรุนแรง", "color": "red", "egfr_range": "15-29"},
    "G5": {"description": "ไตวายระยะสุดท้าย", "color": "darkred", "egfr_range": "<15"},
}


def classify_ckd_full(egfr: float) -> dict:
    """จำแนก CKD stage พร้อมรายละเอียด (KDIGO 2012)

    Returns: {"stage": "G4", "description": "ลดลงรุนแรง", "color": "red", "egfr_range": "15-29"}
    เป็น canonical function — ckd_staging.get_ckd_stage() delegate มาที่นี่
    """
    stage = _classify_ckd(egfr)
    info = _CKD_STAGE_INFO.get(stage, _CKD_STAGE_INFO["G5"])
    return {"stage": stage, **info}


def analyze_ckd_progression(
    egfr_history: list[dict],
) -> CKDProgressionReport:
    """วิเคราะห์การดำเนินโรค CKD จาก eGFR ย้อนหลัง

    Args:
        egfr_history: [{date: "YYYY-MM-DD", value: float}] เรียงเก่า→ใหม่

    KDIGO: eGFR decline > 5 mL/min/1.73m²/year = rapid progression
    """
    if not egfr_history:
        return CKDProgressionReport(
            current_egfr=0, ckd_stage="unknown",
            egfr_slope=None, decline_rate="insufficient",
            months_to_dialysis=None,
            risk_assessment="ไม่มีข้อมูล eGFR",
        )

    current = float(egfr_history[-1].get("value", 0))
    stage = _classify_ckd(current)

    points = [
        TrendPoint(
            date=v.get("date", ""),
            value=float(v.get("value", 0)),
        )
        for v in egfr_history
    ]

    if len(egfr_history) < 2:
        return CKDProgressionReport(
            current_egfr=current, ckd_stage=stage,
            egfr_slope=None, decline_rate="insufficient",
            months_to_dialysis=None,
            risk_assessment=f"eGFR {current:.0f} (CKD {stage}) — ต้องมีค่าอย่างน้อย 2 ครั้งเพื่อดูแนวโน้ม",
            egfr_history=points,
        )

    # Calculate slope (mL/min/year) using first and last values
    first = egfr_history[0]
    last = egfr_history[-1]
    first_val = float(first.get("value", 0))
    last_val = float(last.get("value", 0))

    # Calculate time difference in years
    try:
        from datetime import datetime as _dt
        d1 = _dt.strptime(first.get("date", "")[:10], "%Y-%m-%d")
        d2 = _dt.strptime(last.get("date", "")[:10], "%Y-%m-%d")
        years = (d2 - d1).days / 365.25
    except (ValueError, TypeError):
        years = 0

    if years < 0.08:  # less than 1 month
        return CKDProgressionReport(
            current_egfr=current, ckd_stage=stage,
            egfr_slope=None, decline_rate="insufficient",
            months_to_dialysis=None,
            risk_assessment=f"eGFR {current:.0f} (CKD {stage}) — ช่วงเวลาสั้นเกินไป ต้อง ≥1 เดือน",
            egfr_history=points,
        )

    slope = (last_val - first_val) / years  # positive = improving

    # Classify decline rate
    if slope > 1:
        decline_rate = "improving"
    elif slope > -3:
        decline_rate = "stable"
    elif slope > -EGFR_RAPID_DECLINE:
        decline_rate = "moderate"
    else:
        decline_rate = "rapid"

    # Estimate months to dialysis (eGFR = 10)
    months_to_dialysis = None
    if slope < -0.5 and current > 10:
        years_to_10 = (current - 10) / abs(slope)
        months_to_dialysis = round(years_to_10 * 12, 0)

    # Risk assessment text
    arrows = {"rapid": "↓↓↓", "moderate": "↓↓", "stable": "→", "improving": "↑"}
    assessment_parts = [
        f"eGFR {current:.0f} (CKD {stage}) {arrows.get(decline_rate, '')}",
        f"อัตราเปลี่ยนแปลง: {slope:+.1f} mL/min/year",
    ]

    if decline_rate == "rapid":
        assessment_parts.append(
            f"🔴 eGFR ลดเร็ว! (>{EGFR_RAPID_DECLINE} mL/min/year = rapid progression)"
        )
        assessment_parts.append("ควรหาสาเหตุ: AKI, obstruction, drug toxicity, uncontrolled HTN/DM")
    elif decline_rate == "moderate":
        assessment_parts.append("⚠️ eGFR ลดปานกลาง — ติดตามใกล้ชิดทุก 1-3 เดือน")
    elif decline_rate == "stable":
        assessment_parts.append("✅ eGFR คงที่")
    else:
        assessment_parts.append("✅ eGFR ดีขึ้น")

    if months_to_dialysis is not None:
        if months_to_dialysis < 12:
            assessment_parts.append(
                f"🔴 คาดว่าถึง eGFR 10 ใน ~{months_to_dialysis:.0f} เดือน — เตรียม vascular access!"
            )
        elif months_to_dialysis < 24:
            assessment_parts.append(
                f"⚠️ คาดว่าถึง eGFR 10 ใน ~{months_to_dialysis:.0f} เดือน — วางแผน dialysis"
            )

    if stage in ("G4", "G5") and decline_rate in ("rapid", "moderate"):
        assessment_parts.append("📋 ส่งต่อ nephrologist ถ้ายังไม่ได้ดูแล")

    return CKDProgressionReport(
        current_egfr=current, ckd_stage=stage,
        egfr_slope=slope, decline_rate=decline_rate,
        months_to_dialysis=months_to_dialysis,
        risk_assessment="\n".join(assessment_parts),
        egfr_history=points,
    )


# ─────────────────────────────────────────────────────
# Next Lab Suggestions
# ─────────────────────────────────────────────────────

def suggest_next_labs(
    patient_data: dict,
    last_lab_date: str = "",
    available_labs: list[str] | None = None,
) -> list[str]:
    """แนะนำ lab ที่ควรตรวจครั้งถัดไป

    Based on KDIGO monitoring frequencies:
    - CKD G3: BMP, CBC, iron q3-6 months
    - CKD G4-5/HD: BMP, CBC, iron q1-3 months; Ca/PO4/PTH q1-3 months
    """
    suggestions = []
    esa_code = patient_data.get("esa_code", "")
    hd_per_week = patient_data.get("hd_per_week", 0)

    if hd_per_week > 0:
        # HD patient — more frequent monitoring
        suggestions.append("🔬 CBC (Hb/Hct) — ทุก 2-4 สัปดาห์ เมื่อได้ ESA")
        suggestions.append("🔬 BUN, Cr, Electrolytes — ทุกเดือน")
        suggestions.append("🔬 Ca, PO4, iPTH — ทุก 1-3 เดือน")
        suggestions.append("🔬 Ferritin, TSAT — ทุก 3 เดือน")
        suggestions.append("🔬 Kt/V — ทุกเดือน")
        if esa_code:
            suggestions.append(f"💊 ติดตาม Hb response ต่อ {esa_code} — ทุก 2 สัปดาห์ช่วงปรับ dose")
        suggestions.append("🔬 Albumin — ทุก 3 เดือน (ภาวะโภชนาการ)")
        suggestions.append("🔬 HBsAg, Anti-HCV — ทุก 6-12 เดือน")
    else:
        # CKD non-HD
        suggestions.append("🔬 CBC, BUN, Cr — ทุก 3-6 เดือน")
        suggestions.append("🔬 UACR (urine albumin) — ทุกปี")

    return suggestions


# ─────────────────────────────────────────────────────
# Main Agent: Generate Full Intelligence Report
# ─────────────────────────────────────────────────────

def generate_intelligence_report(
    patient_hn: str,
    patient_data: dict,
    lab_history: list[dict],
    latest_labs: dict | None = None,
) -> LabIntelligenceReport:
    """สร้างรายงานวิเคราะห์แล็บเชิงลึก

    Args:
        patient_hn: HN ผู้ป่วย
        patient_data: ข้อมูลผู้ป่วย (from MasterPatient.to_dict())
        lab_history: ประวัติ Hb [{date, value, doc_id}] เรียงเก่า→ใหม่
        latest_labs: ค่าแล็บล่าสุดทั้งหมด {analyte: value}
    """
    report = LabIntelligenceReport(patient_hn=patient_hn)
    latest_labs = latest_labs or {}

    # 1. Hb Trend
    hb_values = [v for v in lab_history if v.get("value")]
    if hb_values:
        hb_trend = analyze_trend(hb_values, analyte="Hemoglobin")
        report.trends.append(hb_trend)

    # 2. ESA-Hb Optimization
    report.esa_recommendation = analyze_esa_response(
        hb_history=hb_values,
        esa_code=patient_data.get("esa_code", ""),
        esa_medication=patient_data.get("esa_medication", ""),
        esa_dose=patient_data.get("esa_dose", 0),
        ferritin=latest_labs.get("ferritin"),
        tsat=latest_labs.get("tsat"),
    )

    # 3. Cross-lab Correlations
    if latest_labs:
        report.clinical_flags = check_cross_correlations(latest_labs)

    # 4. Next Lab Suggestions
    report.next_lab_suggestions = suggest_next_labs(patient_data)

    # 5. Summary
    report.summary_th = _build_summary(report, patient_data)

    return report


def _build_summary(report: LabIntelligenceReport, patient_data: dict) -> str:
    """สร้างข้อความสรุปภาษาไทย"""
    lines = [
        f"🧠 Lab Intelligence Report",
        f"━━━━━━━━━━━━━━━━",
        f"🔢 HN: {report.patient_hn}",
    ]

    name = patient_data.get("patient_name", "")
    if name:
        lines.append(f"👤 {name}")

    # Trends
    if report.trends:
        lines.append("")
        for t in report.trends:
            arrow = {"rising": "↑", "falling": "↓", "stable": "→"}.get(t.direction, "?")
            lines.append(f"📈 {t.analyte}: {t.current_value:.1f} {arrow}")
            if t.assessment:
                lines.append(f"   {t.assessment}")

    # ESA recommendation
    if report.esa_recommendation:
        r = report.esa_recommendation
        # แสดง ESA เฉพาะเมื่อมีข้อมูลจริง (ไม่ใช่ "ไม่ทราบ ไม่ทราบ")
        if r.current_esa and r.current_esa != "ไม่ทราบ":
            lines.append(f"\n💊 ESA: {r.current_esa} {r.current_dose}")
        else:
            lines.append(f"\n💊 ESA: ยังไม่ได้บันทึกข้อมูล ESA")
        # แสดง Hb เฉพาะเมื่อมีค่าจริง (> 0)
        if r.current_hb > 0:
            lines.append(f"🩸 Hb: {r.current_hb:.1f} g/dL")
        else:
            lines.append(f"🩸 Hb: ไม่มีข้อมูล")
        lines.append(f"\n📋 คำแนะนำ:")
        lines.append(r.recommendation)
        if r.iron_status and r.iron_status != "ไม่มีข้อมูล iron status":
            lines.append(f"\n🧲 Iron: {r.iron_status}")

    # Clinical flags
    if report.clinical_flags:
        lines.append(f"\n⚠️ ข้อสังเกตทางคลินิก:")
        for f in report.clinical_flags:
            lines.append(f"  {f}")

    lines.append(f"\n{'─' * 20}")
    lines.append("⚕️ ข้อมูลประกอบการตัดสินใจ ต้องแพทย์พิจารณา")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────
# LINE Command Interface
# ─────────────────────────────────────────────────────

def format_for_line(report: LabIntelligenceReport, max_length: int = 2000) -> str:
    """Format report สำหรับ LINE message (จำกัดความยาว)"""
    text = report.summary_th
    if len(text) > max_length:
        text = text[:max_length - 20] + "\n\n... (ตัดทอน)"
    return text
