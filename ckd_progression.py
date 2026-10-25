#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CKD Progression Tracking — Kidney Clinical Intelligence Platform 2.0
Phase D: eGFR Slope, Time-to-Dialysis, Rapid Decline Alerts

ระบบติดตามการดำเนินโรคไตเรื้อรัง:
  1. eGFR Slope — คำนวณอัตราการลดลงของ eGFR (mL/min/year) ด้วย linear regression
  2. Time-to-Dialysis Prediction — คาดการณ์เวลาที่ eGFR จะถึง 10 mL/min
  3. Rapid Decline Alerts — แจ้งเตือนเมื่อ eGFR ลดลง >5 mL/min/year (KDIGO)
  4. CKD Stage Transition — ตรวจจับการเปลี่ยน Stage

กฎความปลอดภัย (MUST preserve):
  - ห้ามลบต้นฉบับ (never delete originals)
  - ห้ามแก้ข้อมูลการแพทย์ (never modify medical data)
  - ห้ามอนุมัติเอกสารเอง (never auto-approve)
  - ห้ามส่งข้อมูลผู้ป่วยออกภายนอก (never send patient data externally)
  - Rule engine only — ไม่ใช้ AI ภายนอก
  - ผลวิเคราะห์ต้องผ่าน Human Review
  - ใช้ owner_hash — ไม่เก็บ patient identity
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from lab_tracker import (
    TrendPoint,
    TrendAlert,
    AlertSeverity,
    collect_trend_points,
)


# ═══════════════════════════════════════════════════════════
# Constants — KDIGO CKD Staging
# ═══════════════════════════════════════════════════════════

class CKDStage(str, Enum):
    """KDIGO CKD Stage by eGFR."""
    G1 = "G1"       # ≥90
    G2 = "G2"       # 60-89
    G3a = "G3a"     # 45-59
    G3b = "G3b"     # 30-44
    G4 = "G4"       # 15-29
    G5 = "G5"       # <15
    G5D = "G5D"     # Dialysis


class DeclineCategory(str, Enum):
    """eGFR decline rate categories."""
    STABLE = "stable"                    # <1 mL/min/year
    NORMAL_DECLINE = "normal_decline"    # 1-3 mL/min/year (age-related)
    MODERATE_DECLINE = "moderate_decline"  # 3-5 mL/min/year
    RAPID_DECLINE = "rapid_decline"      # >5 mL/min/year (KDIGO threshold)
    VERY_RAPID_DECLINE = "very_rapid_decline"  # >10 mL/min/year
    INSUFFICIENT_DATA = "insufficient_data"


# KDIGO stage boundaries (lower bound of eGFR)
STAGE_BOUNDARIES: dict[str, tuple[float, float]] = {
    CKDStage.G1.value: (90.0, 999.0),
    CKDStage.G2.value: (60.0, 89.99),
    CKDStage.G3a.value: (45.0, 59.99),
    CKDStage.G3b.value: (30.0, 44.99),
    CKDStage.G4.value: (15.0, 29.99),
    CKDStage.G5.value: (0.0, 14.99),
}

STAGE_NAMES_TH: dict[str, str] = {
    CKDStage.G1.value: "ไตปกติหรือสูง",
    CKDStage.G2.value: "ไตทำงานลดลงเล็กน้อย",
    CKDStage.G3a.value: "ไตทำงานลดลงเล็กน้อยถึงปานกลาง",
    CKDStage.G3b.value: "ไตทำงานลดลงปานกลางถึงรุนแรง",
    CKDStage.G4.value: "ไตทำงานลดลงรุนแรง",
    CKDStage.G5.value: "ไตวาย",
    CKDStage.G5D.value: "ไตวาย — ฟอกเลือด",
}

# Dialysis threshold: eGFR at which dialysis is typically initiated
DIALYSIS_EGFR_THRESHOLD = 10.0  # mL/min/1.73m²

# Minimum data requirements
MIN_POINTS_FOR_SLOPE = 3        # ≥3 eGFR values for reliable slope
MIN_SPAN_DAYS = 90              # ≥90 days between first and last measurement
MIN_POINTS_FOR_PREDICTION = 4   # ≥4 points for time-to-dialysis


# ═══════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════

@dataclass
class EGFRSlope:
    """ผลการคำนวณ eGFR slope."""
    slope: float                # mL/min/1.73m²/year (negative = declining)
    intercept: float            # y-intercept ของ regression line
    r_squared: float            # R² — goodness of fit
    n_points: int               # จำนวนจุดข้อมูล
    span_days: int              # จำนวนวันระหว่างจุดแรก-สุดท้าย
    first_date: str             # วันที่ eGFR ครั้งแรก
    last_date: str              # วันที่ eGFR ล่าสุด
    first_egfr: float           # eGFR ครั้งแรก
    last_egfr: float            # eGFR ล่าสุด
    decline_category: str       # DeclineCategory value
    is_reliable: bool = True    # slope เชื่อถือได้หรือไม่

    def to_dict(self) -> dict:
        return {
            "slope_per_year": round(self.slope, 2),
            "intercept": round(self.intercept, 2),
            "r_squared": round(self.r_squared, 3),
            "n_points": self.n_points,
            "span_days": self.span_days,
            "first_date": self.first_date,
            "last_date": self.last_date,
            "first_egfr": round(self.first_egfr, 1),
            "last_egfr": round(self.last_egfr, 1),
            "decline_category": self.decline_category,
            "is_reliable": self.is_reliable,
        }


@dataclass
class DialysisPrediction:
    """ผลการคาดการณ์เวลาถึง dialysis."""
    predicted_date: str         # วันที่คาดว่า eGFR จะถึง threshold
    years_remaining: float      # จำนวนปีที่เหลือ
    months_remaining: float     # จำนวนเดือนที่เหลือ
    current_egfr: float         # eGFR ปัจจุบัน
    current_stage: str          # CKD Stage ปัจจุบัน
    slope_used: float           # slope ที่ใช้คำนวณ
    threshold_egfr: float       # eGFR threshold (default 10)
    confidence: str             # low / moderate / high
    is_applicable: bool = True  # applicable (eGFR declining and >threshold)

    def to_dict(self) -> dict:
        d = {
            "current_egfr": round(self.current_egfr, 1),
            "current_stage": self.current_stage,
            "threshold_egfr": self.threshold_egfr,
            "slope_per_year": round(self.slope_used, 2),
            "is_applicable": self.is_applicable,
            "confidence": self.confidence,
        }
        if self.is_applicable:
            d.update({
                "predicted_date": self.predicted_date,
                "years_remaining": round(self.years_remaining, 1),
                "months_remaining": round(self.months_remaining, 0),
            })
        return d


@dataclass
class StageTransition:
    """การเปลี่ยน CKD Stage."""
    from_stage: str
    to_stage: str
    from_egfr: float
    to_egfr: float
    from_date: str
    to_date: str
    is_progression: bool        # True = worse, False = improved

    def to_dict(self) -> dict:
        return {
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "from_egfr": round(self.from_egfr, 1),
            "to_egfr": round(self.to_egfr, 1),
            "from_date": self.from_date,
            "to_date": self.to_date,
            "is_progression": self.is_progression,
        }


@dataclass
class CKDProgressionReport:
    """รายงานการดำเนินโรค CKD — ผลวิเคราะห์ทั้งหมด."""
    owner_hash: str
    analysis_date: str
    current_stage: str
    current_egfr: float
    egfr_slope: Optional[EGFRSlope] = None
    dialysis_prediction: Optional[DialysisPrediction] = None
    stage_transitions: list[StageTransition] = field(default_factory=list)
    alerts: list[TrendAlert] = field(default_factory=list)
    disclaimer: str = (
        "⚠️ ข้อมูลนี้เป็นการคาดการณ์จาก deterministic model เท่านั้น "
        "— ไม่ใช่การวินิจฉัยหรือพยากรณ์โรค — "
        "การดำเนินโรคอาจเปลี่ยนแปลงได้ตามการรักษาและปัจจัยอื่น "
        "— กรุณาปรึกษาแพทย์"
    )

    def __post_init__(self):
        if not self.analysis_date:
            self.analysis_date = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "owner_hash_prefix": self.owner_hash[:8] if self.owner_hash else "",
            "analysis_date": self.analysis_date,
            "current_stage": self.current_stage,
            "current_stage_th": STAGE_NAMES_TH.get(self.current_stage, ""),
            "current_egfr": round(self.current_egfr, 1),
            "egfr_slope": self.egfr_slope.to_dict() if self.egfr_slope else None,
            "dialysis_prediction": (
                self.dialysis_prediction.to_dict()
                if self.dialysis_prediction else None
            ),
            "stage_transitions": [t.to_dict() for t in self.stage_transitions],
            "alerts": [a.to_dict() for a in self.alerts],
            "alert_count": len(self.alerts),
            "disclaimer": self.disclaimer,
        }

    def summary_text_th(self) -> str:
        """สร้างข้อความสรุปภาษาไทยสำหรับ LINE."""
        return _generate_progression_summary(self)


# ═══════════════════════════════════════════════════════════
# Core Functions
# ═══════════════════════════════════════════════════════════

def get_ckd_stage(egfr: float) -> str:
    """กำหนด CKD Stage จากค่า eGFR.

    Args:
        egfr: ค่า eGFR (mL/min/1.73m²)

    Returns:
        CKDStage value string
    """
    if egfr >= 90:
        return CKDStage.G1.value
    elif egfr >= 60:
        return CKDStage.G2.value
    elif egfr >= 45:
        return CKDStage.G3a.value
    elif egfr >= 30:
        return CKDStage.G3b.value
    elif egfr >= 15:
        return CKDStage.G4.value
    else:
        return CKDStage.G5.value


def _date_to_days(date_str: str, reference_date: str) -> float:
    """แปลงวันที่เป็นจำนวนวันจาก reference_date.

    Args:
        date_str: ISO date string
        reference_date: ISO date string (day 0)

    Returns:
        จำนวนวัน (float)
    """
    try:
        dt = datetime.fromisoformat(date_str)
        ref = datetime.fromisoformat(reference_date)
        return (dt - ref).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 0.0


def calculate_egfr_slope(
    egfr_points: list[TrendPoint],
) -> Optional[EGFRSlope]:
    """คำนวณ eGFR slope ด้วย Ordinary Least Squares (OLS) linear regression.

    Slope = Σ((x-x̄)(y-ȳ)) / Σ((x-x̄)²)
    x = เวลา (ปี), y = eGFR

    Args:
        egfr_points: list ของ TrendPoint สำหรับ eGFR เรียงตามวันที่

    Returns:
        EGFRSlope or None (ถ้าข้อมูลไม่เพียงพอ)
    """
    if len(egfr_points) < MIN_POINTS_FOR_SLOPE:
        return None

    # ใช้จุดแรกเป็น reference
    ref_date = egfr_points[0].date

    # Convert to (x=years, y=egfr)
    xs: list[float] = []
    ys: list[float] = []
    for p in egfr_points:
        days = _date_to_days(p.date, ref_date)
        years = days / 365.25
        xs.append(years)
        ys.append(p.value)

    n = len(xs)

    # Check span
    span_days = int(xs[-1] * 365.25)
    if span_days < MIN_SPAN_DAYS:
        return None

    # OLS linear regression
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n

    ss_xy = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
    ss_xx = sum((xs[i] - x_mean) ** 2 for i in range(n))

    if ss_xx == 0:
        return None

    slope = ss_xy / ss_xx       # mL/min/year
    intercept = y_mean - slope * x_mean

    # R² calculation
    ss_yy = sum((ys[i] - y_mean) ** 2 for i in range(n))
    if ss_yy == 0:
        r_squared = 1.0  # all values identical
    else:
        ss_res = sum((ys[i] - (slope * xs[i] + intercept)) ** 2 for i in range(n))
        r_squared = 1.0 - (ss_res / ss_yy)

    # Determine decline category (slope is negative for decline)
    abs_slope = abs(slope)
    if slope > -1.0:  # gaining or losing <1
        category = DeclineCategory.STABLE.value
    elif abs_slope <= 3.0:
        category = DeclineCategory.NORMAL_DECLINE.value
    elif abs_slope <= 5.0:
        category = DeclineCategory.MODERATE_DECLINE.value
    elif abs_slope <= 10.0:
        category = DeclineCategory.RAPID_DECLINE.value
    else:
        category = DeclineCategory.VERY_RAPID_DECLINE.value

    # Reliability: R² > 0.3 and span > 90 days and n >= 3
    is_reliable = (r_squared > 0.3 and span_days >= MIN_SPAN_DAYS
                   and n >= MIN_POINTS_FOR_SLOPE)

    return EGFRSlope(
        slope=slope,
        intercept=intercept,
        r_squared=r_squared,
        n_points=n,
        span_days=span_days,
        first_date=egfr_points[0].date,
        last_date=egfr_points[-1].date,
        first_egfr=egfr_points[0].value,
        last_egfr=egfr_points[-1].value,
        decline_category=category,
        is_reliable=is_reliable,
    )


def predict_time_to_dialysis(
    egfr_slope: EGFRSlope,
    current_egfr: float,
    threshold: float = DIALYSIS_EGFR_THRESHOLD,
    reference_date: Optional[str] = None,
) -> DialysisPrediction:
    """คาดการณ์เวลาที่ eGFR จะถึง dialysis threshold.

    ใช้ linear extrapolation: time = (threshold - current_egfr) / slope

    Args:
        egfr_slope: ผล slope calculation
        current_egfr: eGFR ปัจจุบัน
        threshold: eGFR threshold สำหรับ dialysis (default 10)
        reference_date: วันที่อ้างอิง (default=today)

    Returns:
        DialysisPrediction
    """
    if reference_date:
        ref_dt = datetime.fromisoformat(reference_date)
    else:
        ref_dt = datetime.now()

    current_stage = get_ckd_stage(current_egfr)

    # Not applicable if eGFR not declining or already below threshold
    if egfr_slope.slope >= 0 or current_egfr <= threshold:
        return DialysisPrediction(
            predicted_date="",
            years_remaining=0,
            months_remaining=0,
            current_egfr=current_egfr,
            current_stage=current_stage,
            slope_used=egfr_slope.slope,
            threshold_egfr=threshold,
            confidence="n/a",
            is_applicable=False,
        )

    # Time = (current - threshold) / |slope|
    years_to_threshold = (current_egfr - threshold) / abs(egfr_slope.slope)
    months_to_threshold = years_to_threshold * 12

    # Predicted date
    predicted_dt = ref_dt + timedelta(days=years_to_threshold * 365.25)
    predicted_date = predicted_dt.strftime("%Y-%m-%d")

    # Confidence based on R², n_points, span
    if (egfr_slope.r_squared >= 0.7
            and egfr_slope.n_points >= 6
            and egfr_slope.span_days >= 365):
        confidence = "high"
    elif (egfr_slope.r_squared >= 0.4
            and egfr_slope.n_points >= MIN_POINTS_FOR_PREDICTION
            and egfr_slope.span_days >= 180):
        confidence = "moderate"
    else:
        confidence = "low"

    return DialysisPrediction(
        predicted_date=predicted_date,
        years_remaining=years_to_threshold,
        months_remaining=months_to_threshold,
        current_egfr=current_egfr,
        current_stage=current_stage,
        slope_used=egfr_slope.slope,
        threshold_egfr=threshold,
        confidence=confidence,
        is_applicable=True,
    )


def detect_stage_transitions(
    egfr_points: list[TrendPoint],
) -> list[StageTransition]:
    """ตรวจจับการเปลี่ยน CKD Stage จากข้อมูล eGFR.

    Args:
        egfr_points: list ของ TrendPoint สำหรับ eGFR เรียงตามวันที่

    Returns:
        list ของ StageTransition
    """
    if len(egfr_points) < 2:
        return []

    transitions: list[StageTransition] = []
    prev_point = egfr_points[0]
    prev_stage = get_ckd_stage(prev_point.value)

    for point in egfr_points[1:]:
        current_stage = get_ckd_stage(point.value)
        if current_stage != prev_stage:
            # Determine if progression (worse) or improvement
            stage_order = [
                CKDStage.G1.value, CKDStage.G2.value,
                CKDStage.G3a.value, CKDStage.G3b.value,
                CKDStage.G4.value, CKDStage.G5.value,
            ]
            prev_idx = (stage_order.index(prev_stage)
                        if prev_stage in stage_order else -1)
            curr_idx = (stage_order.index(current_stage)
                        if current_stage in stage_order else -1)

            transitions.append(StageTransition(
                from_stage=prev_stage,
                to_stage=current_stage,
                from_egfr=prev_point.value,
                to_egfr=point.value,
                from_date=prev_point.date,
                to_date=point.date,
                is_progression=(curr_idx > prev_idx),
            ))
            prev_stage = current_stage
        prev_point = point

    return transitions


def generate_progression_alerts(
    egfr_slope: Optional[EGFRSlope],
    dialysis_pred: Optional[DialysisPrediction],
    transitions: list[StageTransition],
) -> list[TrendAlert]:
    """สร้าง alerts จากผลวิเคราะห์ CKD progression.

    Args:
        egfr_slope: ผล slope calculation
        dialysis_pred: ผล dialysis prediction
        transitions: list ของ stage transitions

    Returns:
        list ของ TrendAlert
    """
    alerts: list[TrendAlert] = []

    # ── Alert 1: Rapid eGFR decline (KDIGO: >5 mL/min/year) ──
    if egfr_slope and egfr_slope.is_reliable:
        abs_slope = abs(egfr_slope.slope)
        if egfr_slope.slope < 0:  # declining
            if abs_slope > 10:
                alerts.append(TrendAlert(
                    analyte="eGFR",
                    severity=AlertSeverity.CRITICAL.value,
                    message=(
                        f"🔴 eGFR ลดลงเร็วมาก ({egfr_slope.slope:+.1f} mL/min/ปี) "
                        f"— เร็วกว่าเกณฑ์ KDIGO (>5) มาก"
                    ),
                    direction="decreasing",
                    change_percent=0,
                    period=f"{egfr_slope.span_days} days",
                    recommendation=(
                        "พิจารณาหาสาเหตุ reversible causes ของ AKI-on-CKD "
                        "— ตรวจ obstruction, nephrotoxins, volume status"
                    ),
                ))
            elif abs_slope > 5:
                alerts.append(TrendAlert(
                    analyte="eGFR",
                    severity=AlertSeverity.WARNING.value,
                    message=(
                        f"🟡 eGFR ลดลงเร็ว ({egfr_slope.slope:+.1f} mL/min/ปี) "
                        f"— เกินเกณฑ์ KDIGO rapid decline (>5 mL/min/ปี)"
                    ),
                    direction="decreasing",
                    change_percent=0,
                    period=f"{egfr_slope.span_days} days",
                    recommendation=(
                        "พิจารณาประเมินสาเหตุ — ปรับยา, ควบคุมความดัน/น้ำตาล, "
                        "ส่งต่อ nephrologist"
                    ),
                ))
            elif abs_slope > 3:
                alerts.append(TrendAlert(
                    analyte="eGFR",
                    severity=AlertSeverity.INFO.value,
                    message=(
                        f"eGFR ลดลงปานกลาง ({egfr_slope.slope:+.1f} mL/min/ปี) "
                        f"— ติดตามใกล้ชิด"
                    ),
                    direction="decreasing",
                    change_percent=0,
                    period=f"{egfr_slope.span_days} days",
                    recommendation="ติดตาม eGFR ทุก 3 เดือน",
                ))

    # ── Alert 2: Approaching dialysis ──
    if (dialysis_pred and dialysis_pred.is_applicable):
        if dialysis_pred.months_remaining <= 12:
            alerts.append(TrendAlert(
                analyte="eGFR",
                severity=AlertSeverity.CRITICAL.value,
                message=(
                    f"🔴 คาดการณ์ถึง dialysis ภายใน "
                    f"{dialysis_pred.months_remaining:.0f} เดือน "
                    f"(eGFR {dialysis_pred.current_egfr:.0f} → "
                    f"{dialysis_pred.threshold_egfr:.0f})"
                ),
                direction="decreasing",
                change_percent=0,
                period="projection",
                recommendation=(
                    "เตรียม vascular access, ให้ความรู้ผู้ป่วย, "
                    "พิจารณา predialysis education"
                ),
            ))
        elif dialysis_pred.months_remaining <= 24:
            alerts.append(TrendAlert(
                analyte="eGFR",
                severity=AlertSeverity.WARNING.value,
                message=(
                    f"🟡 คาดการณ์ถึง dialysis ภายใน "
                    f"{dialysis_pred.years_remaining:.1f} ปี "
                    f"(~{dialysis_pred.months_remaining:.0f} เดือน)"
                ),
                direction="decreasing",
                change_percent=0,
                period="projection",
                recommendation=(
                    "วางแผน renal replacement therapy, "
                    "เริ่ม predialysis education"
                ),
            ))

    # ── Alert 3: Stage transitions ──
    for t in transitions:
        if t.is_progression:
            # G4→G5 or G3→G4 are more critical
            if t.to_stage in (CKDStage.G5.value, CKDStage.G4.value):
                alerts.append(TrendAlert(
                    analyte="eGFR",
                    severity=AlertSeverity.CRITICAL.value,
                    message=(
                        f"🔴 CKD เปลี่ยน Stage: {t.from_stage} → {t.to_stage} "
                        f"(eGFR {t.from_egfr:.0f} → {t.to_egfr:.0f})"
                    ),
                    direction="decreasing",
                    change_percent=0,
                    period=f"{t.from_date} to {t.to_date}",
                    recommendation=(
                        f"Stage {t.to_stage} — "
                        f"{STAGE_NAMES_TH.get(t.to_stage, '')}"
                    ),
                ))
            else:
                alerts.append(TrendAlert(
                    analyte="eGFR",
                    severity=AlertSeverity.WARNING.value,
                    message=(
                        f"CKD เปลี่ยน Stage: {t.from_stage} → {t.to_stage} "
                        f"(eGFR {t.from_egfr:.0f} → {t.to_egfr:.0f})"
                    ),
                    direction="decreasing",
                    change_percent=0,
                    period=f"{t.from_date} to {t.to_date}",
                    recommendation=(
                        f"Stage {t.to_stage} — "
                        f"{STAGE_NAMES_TH.get(t.to_stage, '')}"
                    ),
                ))

    return alerts


# ═══════════════════════════════════════════════════════════
# Main Analysis Function
# ═══════════════════════════════════════════════════════════

def analyze_ckd_progression(
    lab_reports: list[dict],
    owner_hash: str = "",
    reference_date: Optional[str] = None,
) -> CKDProgressionReport:
    """วิเคราะห์การดำเนินโรค CKD จาก lab reports.

    นี่คือ entry point หลัก — รวม slope, prediction, transitions, alerts

    Args:
        lab_reports: list ของ lab report dicts (from LabReport.to_dict())
        owner_hash: hash ของเจ้าของ (ไม่ใช่ patient identity)
        reference_date: วันที่อ้างอิง (default=today)

    Returns:
        CKDProgressionReport

    Safety:
        - Deterministic rule engine only
        - ผลต้องผ่าน Human Review
        - ไม่ใช่การวินิจฉัยหรือพยากรณ์โรค
    """
    if reference_date:
        ref_date_str = reference_date
    else:
        ref_date_str = datetime.now().strftime("%Y-%m-%d")

    # Collect eGFR points from all reports
    egfr_points = collect_trend_points(lab_reports, "eGFR")

    # Current eGFR (most recent)
    current_egfr = egfr_points[-1].value if egfr_points else 0.0
    current_stage = get_ckd_stage(current_egfr) if current_egfr > 0 else ""

    # 1. Calculate eGFR slope
    egfr_slope = calculate_egfr_slope(egfr_points)

    # 2. Predict time-to-dialysis
    dialysis_pred = None
    if egfr_slope and egfr_slope.is_reliable:
        dialysis_pred = predict_time_to_dialysis(
            egfr_slope, current_egfr,
            reference_date=ref_date_str,
        )

    # 3. Detect stage transitions
    transitions = detect_stage_transitions(egfr_points)

    # 4. Generate alerts
    alerts = generate_progression_alerts(
        egfr_slope, dialysis_pred, transitions,
    )

    return CKDProgressionReport(
        owner_hash=owner_hash,
        analysis_date=ref_date_str,
        current_stage=current_stage,
        current_egfr=current_egfr,
        egfr_slope=egfr_slope,
        dialysis_prediction=dialysis_pred,
        stage_transitions=transitions,
        alerts=alerts,
    )


# ═══════════════════════════════════════════════════════════
# LINE Summary Generation
# ═══════════════════════════════════════════════════════════

def _generate_progression_summary(report: CKDProgressionReport) -> str:
    """สร้างข้อความสรุป CKD progression สำหรับ LINE.

    Safety: ข้อความต้องมี disclaimer เสมอ
    """
    lines = [
        "🏥 รายงานการดำเนินโรค CKD",
        f"วันที่วิเคราะห์: {report.analysis_date}",
        "",
    ]

    if not report.current_egfr:
        lines.append("❌ ไม่พบข้อมูล eGFR — ไม่สามารถวิเคราะห์ได้")
        lines.append("")
        lines.append(report.disclaimer)
        return "\n".join(lines)

    # Current status
    stage_th = STAGE_NAMES_TH.get(report.current_stage, "")
    lines.append(
        f"📊 สถานะปัจจุบัน: Stage {report.current_stage}"
    )
    lines.append(f"   {stage_th}")
    lines.append(f"   eGFR ล่าสุด: {report.current_egfr:.1f} mL/min/1.73m²")
    lines.append("")

    # eGFR Slope
    if report.egfr_slope:
        s = report.egfr_slope
        cat_names = {
            DeclineCategory.STABLE.value: "คงที่",
            DeclineCategory.NORMAL_DECLINE.value: "ลดลงตามอายุ (ปกติ)",
            DeclineCategory.MODERATE_DECLINE.value: "ลดลงปานกลาง",
            DeclineCategory.RAPID_DECLINE.value: "⚠️ ลดลงเร็ว (KDIGO rapid decline)",
            DeclineCategory.VERY_RAPID_DECLINE.value: "🔴 ลดลงเร็วมาก",
        }
        cat_text = cat_names.get(s.decline_category, s.decline_category)
        lines.append(f"📉 อัตราการเปลี่ยนแปลง eGFR:")
        lines.append(f"   Slope: {s.slope:+.1f} mL/min/ปี")
        lines.append(f"   หมวด: {cat_text}")
        lines.append(
            f"   ข้อมูล {s.n_points} ครั้ง "
            f"({s.first_date} ถึง {s.last_date})"
        )
        lines.append(
            f"   eGFR: {s.first_egfr:.0f} → {s.last_egfr:.0f} "
            f"(R²={s.r_squared:.2f})"
        )
        if not s.is_reliable:
            lines.append("   ⚠️ ข้อมูลยังไม่เพียงพอ — slope อาจไม่แม่นยำ")
        lines.append("")

    # Dialysis prediction
    if report.dialysis_prediction:
        dp = report.dialysis_prediction
        if dp.is_applicable:
            conf_text = {
                "high": "สูง", "moderate": "ปานกลาง", "low": "ต่ำ"
            }.get(dp.confidence, dp.confidence)
            lines.append("⏱️ คาดการณ์ถึง Dialysis:")
            lines.append(
                f"   ประมาณ {dp.years_remaining:.1f} ปี "
                f"(~{dp.months_remaining:.0f} เดือน)"
            )
            lines.append(f"   วันที่คาด: {dp.predicted_date}")
            lines.append(f"   ความเชื่อมั่น: {conf_text}")
            lines.append("")
        else:
            if dp.current_egfr <= dp.threshold_egfr:
                lines.append("⏱️ eGFR ต่ำกว่า dialysis threshold แล้ว")
            else:
                lines.append("⏱️ eGFR ไม่ได้ลดลง — ไม่จำเป็นต้องคาดการณ์")
            lines.append("")

    # Stage transitions
    if report.stage_transitions:
        lines.append("🔄 การเปลี่ยน Stage:")
        for t in report.stage_transitions:
            arrow = "⬇️" if t.is_progression else "⬆️"
            lines.append(
                f"   {arrow} {t.from_stage} → {t.to_stage} "
                f"(eGFR {t.from_egfr:.0f}→{t.to_egfr:.0f}) "
                f"[{t.to_date}]"
            )
        lines.append("")

    # Alerts
    critical = [a for a in report.alerts
                if a.severity == AlertSeverity.CRITICAL.value]
    warnings = [a for a in report.alerts
                if a.severity == AlertSeverity.WARNING.value]

    if critical:
        lines.append("🔴 แจ้งเตือนสำคัญ:")
        for a in critical:
            lines.append(f"   {a.message}")
            if a.recommendation:
                lines.append(f"   → {a.recommendation}")
        lines.append("")

    if warnings:
        lines.append("🟡 ข้อสังเกต:")
        for a in warnings:
            lines.append(f"   {a.message}")
            if a.recommendation:
                lines.append(f"   → {a.recommendation}")
        lines.append("")

    lines.append(report.disclaimer)
    return "\n".join(lines)
