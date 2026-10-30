"""CKD Staging + eGFR Tracking — ติดตามระยะโรคไตเรื้อรังอัตโนมัติ

คำนวณ eGFR จากค่า Creatinine ด้วยสูตร CKD-EPI 2021 (race-free)
จำแนก CKD stage (G1-G5) ติดตามแนวโน้ม eGFR และแจ้งเตือนเมื่อ
eGFR ลดลงเร็ว (rapid progressor)

ใช้กับคลินิกระนอง รักษ์ไต — ผู้ป่วยฟอกเลือดและ CKD ทุกระยะ

ENV VARS:
- DEFAULT_PATIENT_AGE: อายุ default (default: 55)
- DEFAULT_PATIENT_GENDER: เพศ default "male"/"female" (default: "male")

ไม่ log ข้อมูลผู้ป่วย (PDPA)
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_AGE = int(os.getenv("DEFAULT_PATIENT_AGE", "55").strip() or "55")
DEFAULT_IS_FEMALE = (
    os.getenv("DEFAULT_PATIENT_GENDER", "male").strip().lower() == "female"
)


# ═══════════════════════════════════════════════════════════
# eGFR Calculation — CKD-EPI 2021 (race-free)
# ═══════════════════════════════════════════════════════════


def calculate_egfr_ckd_epi(
    creatinine: float,
    age: int,
    is_female: bool = False,
) -> float:
    """คำนวณ eGFR ด้วยสูตร CKD-EPI 2021 (race-free).

    Parameters:
        creatinine: ค่า Serum Creatinine (mg/dL)
        age: อายุ (ปี)
        is_female: True = หญิง, False = ชาย

    Returns:
        eGFR (ml/min/1.73m²) ปัดเป็น 1 ตำแหน่ง

    Reference: Inker LA, et al. NEJM 2021;385:1737-1749
    """
    if creatinine <= 0 or age <= 0:
        return 0.0

    if is_female:
        kappa = 0.7
        alpha = -0.241
        sex_factor = 1.012
    else:
        kappa = 0.9
        alpha = -0.302
        sex_factor = 1.0

    cr_ratio = creatinine / kappa
    term1 = min(cr_ratio, 1.0) ** alpha
    term2 = max(cr_ratio, 1.0) ** (-1.200)
    age_factor = 0.9938 ** age

    egfr = 142 * term1 * term2 * age_factor * sex_factor
    return round(egfr, 1)


# ═══════════════════════════════════════════════════════════
# CKD Stage Classification — KDIGO 2012
# ═══════════════════════════════════════════════════════════


def get_ckd_stage(egfr: float) -> dict:
    """จำแนกระยะ CKD จาก eGFR (KDIGO 2012).

    Returns:
        {"stage", "description" (Thai), "color", "egfr_range"}
    """
    if egfr >= 90:
        return {
            "stage": "G1",
            "description": "ปกติหรือสูง",
            "color": "green",
            "egfr_range": "≥90",
        }
    elif egfr >= 60:
        return {
            "stage": "G2",
            "description": "ลดลงเล็กน้อย",
            "color": "yellow",
            "egfr_range": "60-89",
        }
    elif egfr >= 45:
        return {
            "stage": "G3a",
            "description": "ลดลงเล็กน้อยถึงปานกลาง",
            "color": "orange",
            "egfr_range": "45-59",
        }
    elif egfr >= 30:
        return {
            "stage": "G3b",
            "description": "ลดลงปานกลางถึงรุนแรง",
            "color": "orange",
            "egfr_range": "30-44",
        }
    elif egfr >= 15:
        return {
            "stage": "G4",
            "description": "ลดลงรุนแรง",
            "color": "red",
            "egfr_range": "15-29",
        }
    else:
        return {
            "stage": "G5",
            "description": "ไตวายระยะสุดท้าย",
            "color": "darkred",
            "egfr_range": "<15",
        }


# ═══════════════════════════════════════════════════════════
# Lab Data Extraction
# ═══════════════════════════════════════════════════════════

_CR_NAMES = {
    "cr", "creatinine", "serum creatinine", "s.creatinine",
    "s.cr", "scr", "crea", "creat",
}


def _is_creatinine(analyte_name: str) -> bool:
    """ตรวจว่าชื่อ analyte เป็น Creatinine หรือไม่."""
    name = analyte_name.strip().lower()
    return name in _CR_NAMES or "creatinine" in name


def _parse_float(val: Any) -> Optional[float]:
    """แปลงค่าแล็บเป็น float อย่างปลอดภัย."""
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    # ตัด prefix/suffix เช่น ">10.0", "<0.5", "H 5.2"
    for prefix in (">", "<", "H ", "L ", "* "):
        s = s.removeprefix(prefix)
    try:
        v = float(s)
        # Creatinine ปกติ 0.5 - 30 mg/dL — ค่าผิดปกติ filter ออก
        if 0.1 <= v <= 50.0:
            return v
    except (ValueError, TypeError):
        pass
    return None


def extract_creatinine_trend(
    lab_history: list[dict],
    age: Optional[int] = None,
    is_female: Optional[bool] = None,
) -> list[dict]:
    """ดึง Creatinine trend จากประวัติ lab.

    Parameters:
        lab_history: จาก get_patient_lab_history()
        age: อายุผู้ป่วย (ถ้าไม่ระบุใช้ default)
        is_female: เพศผู้ป่วย (ถ้าไม่ระบุใช้ default)

    Returns:
        [{date, cr_value, egfr, egfr_estimated}] เรียงจากเก่าไปใหม่
    """
    _age = age if age is not None else DEFAULT_AGE
    _female = is_female if is_female is not None else DEFAULT_IS_FEMALE
    demographics_known = age is not None

    trend = []
    for entry in lab_history:
        date_str = entry.get("upload_date", "")
        for result in entry.get("results", []):
            analyte = result.get("analyte", "")
            if not _is_creatinine(analyte):
                continue
            cr_val = _parse_float(result.get("value"))
            if cr_val is None:
                continue
            egfr = calculate_egfr_ckd_epi(cr_val, _age, _female)
            trend.append({
                "date": date_str,
                "cr_value": cr_val,
                "egfr": egfr,
                "egfr_estimated": not demographics_known,
            })

    # เรียงจากเก่าไปใหม่ (สำหรับ trend/slope calculation)
    trend.sort(key=lambda x: x.get("date", ""))

    # ลบ duplicate dates (เก็บค่าล่าสุด)
    seen_dates: dict[str, int] = {}
    for i, item in enumerate(trend):
        seen_dates[item["date"]] = i
    trend = [trend[i] for i in sorted(seen_dates.values())]

    return trend


# ═══════════════════════════════════════════════════════════
# eGFR Slope / Decline Detection
# ═══════════════════════════════════════════════════════════


def _calculate_egfr_slope(egfr_trend: list[dict]) -> Optional[float]:
    """คำนวณ eGFR slope (ml/min/1.73m²/year) ด้วย linear regression.

    ต้องมีอย่างน้อย 4 data points คร่อม ≥6 เดือน
    Returns: slope (ลบ = ลดลง) หรือ None ถ้าข้อมูลไม่พอ
    """
    if len(egfr_trend) < 4:
        return None

    # แปลง date เป็น years จาก first date
    points: list[tuple[float, float]] = []
    first_date = None
    for item in egfr_trend:
        try:
            dt = datetime.fromisoformat(
                item["date"].replace("Z", "+00:00")
            ) if "T" in item["date"] else datetime.strptime(
                item["date"][:10], "%Y-%m-%d"
            )
            if first_date is None:
                first_date = dt
            years = (dt - first_date).days / 365.25
            points.append((years, item["egfr"]))
        except (ValueError, TypeError):
            continue

    if len(points) < 4:
        return None

    # ต้องคร่อมอย่างน้อย 6 เดือน (0.5 ปี)
    time_span = points[-1][0] - points[0][0]
    if time_span < 0.5:
        return None

    # Simple linear regression: slope = Σ(xi-x̄)(yi-ȳ) / Σ(xi-x̄)²
    n = len(points)
    x_mean = sum(p[0] for p in points) / n
    y_mean = sum(p[1] for p in points) / n

    numerator = sum((p[0] - x_mean) * (p[1] - y_mean) for p in points)
    denominator = sum((p[0] - x_mean) ** 2 for p in points)

    if denominator == 0:
        return None

    slope = numerator / denominator
    return round(slope, 2)


def detect_rapid_decline(egfr_trend: list[dict]) -> dict:
    """ตรวจหา eGFR decline ที่เร็วผิดปกติ.

    KDIGO criteria:
    - Rapid: >5 ml/min/1.73m²/year
    - Moderate: >3 ml/min/1.73m²/year
    - Stable: ≤3

    Returns:
        {declining, rate, category, recommendation (Thai), data_points, time_span_months}
    """
    slope = _calculate_egfr_slope(egfr_trend)

    if slope is None:
        return {
            "declining": False,
            "rate": None,
            "category": "insufficient_data",
            "recommendation": "ข้อมูลไม่เพียงพอ — ต้องมี ≥4 ค่า คร่อม ≥6 เดือน",
            "data_points": len(egfr_trend),
            "time_span_months": 0,
        }

    # คำนวณ time span
    dates = [item["date"] for item in egfr_trend if item.get("date")]
    time_span_months = 0
    if len(dates) >= 2:
        try:
            d1 = datetime.strptime(dates[0][:10], "%Y-%m-%d")
            d2 = datetime.strptime(dates[-1][:10], "%Y-%m-%d")
            time_span_months = round((d2 - d1).days / 30.44)
        except (ValueError, TypeError):
            pass

    # Decline rate = absolute value of negative slope
    decline_rate = abs(slope) if slope < 0 else 0

    if slope < -5:
        return {
            "declining": True,
            "rate": slope,
            "category": "rapid",
            "recommendation": (
                "⚠️ eGFR ลดลงเร็วมาก (>5 ml/min/ปี) — "
                "ควรตรวจหาสาเหตุที่แก้ไขได้: obstruction, "
                "nephrotoxic drugs, volume depletion, "
                "uncontrolled DM/HT"
            ),
            "data_points": len(egfr_trend),
            "time_span_months": time_span_months,
        }
    elif slope < -3:
        return {
            "declining": True,
            "rate": slope,
            "category": "moderate",
            "recommendation": (
                "⚡ eGFR ลดลงปานกลาง (3-5 ml/min/ปี) — "
                "ควรปรับยาและควบคุมปัจจัยเสี่ยง (BP, proteinuria, "
                "DM control) อย่างเข้มงวด"
            ),
            "data_points": len(egfr_trend),
            "time_span_months": time_span_months,
        }
    else:
        return {
            "declining": False,
            "rate": slope,
            "category": "stable",
            "recommendation": "✅ eGFR คงที่หรือลดลงช้า (≤3 ml/min/ปี)",
            "data_points": len(egfr_trend),
            "time_span_months": time_span_months,
        }


# ═══════════════════════════════════════════════════════════
# Patient-level CKD Profile
# ═══════════════════════════════════════════════════════════


def get_patient_ckd_profile(
    storage,
    doc_index,
    patient_folder: str,
    age: Optional[int] = None,
    is_female: Optional[bool] = None,
    owner_hash: str = "",
) -> dict:
    """สรุป CKD profile ของผู้ป่วยรายเดียว.

    Returns:
        {
            current_cr, current_egfr, ckd_stage,
            demographics_known, cr_trend, egfr_trend,
            decline_assessment, data_count
        }
    """
    try:
        from quarterly_compare import get_patient_lab_history
    except ImportError:
        return {"error": "quarterly_compare not available"}

    lab_history = get_patient_lab_history(
        storage, doc_index, owner_hash,
        patient_folder=patient_folder,
    )

    cr_trend = extract_creatinine_trend(lab_history, age, is_female)

    if not cr_trend:
        return {
            "current_cr": None,
            "current_egfr": None,
            "ckd_stage": None,
            "demographics_known": age is not None,
            "cr_trend": [],
            "decline_assessment": detect_rapid_decline([]),
            "data_count": 0,
        }

    latest = cr_trend[-1]
    stage = get_ckd_stage(latest["egfr"])

    return {
        "current_cr": latest["cr_value"],
        "current_egfr": latest["egfr"],
        "current_date": latest["date"],
        "ckd_stage": stage,
        "demographics_known": age is not None,
        "egfr_estimated": latest.get("egfr_estimated", True),
        "cr_trend": cr_trend,
        "decline_assessment": detect_rapid_decline(cr_trend),
        "data_count": len(cr_trend),
    }


# ═══════════════════════════════════════════════════════════
# Clinic-level Summaries
# ═══════════════════════════════════════════════════════════


def get_clinic_ckd_distribution(
    storage,
    doc_index,
    master_registry,
) -> dict:
    """สรุป CKD stage distribution ทั้งคลินิก.

    Returns:
        {
            total_patients, with_data, no_data,
            distribution: {G1: n, G2: n, ...},
            stage_details: [{stage, count, percentage, color}],
            rapid_decliners: int,
        }
    """
    try:
        from quarterly_compare import get_patient_lab_history
    except ImportError:
        return {"error": "quarterly_compare not available"}

    patients = master_registry.list_all()
    total = len(patients)
    distribution: dict[str, int] = {
        "G1": 0, "G2": 0, "G3a": 0, "G3b": 0, "G4": 0, "G5": 0,
    }
    with_data = 0
    rapid_count = 0

    for patient in patients:
        folder = patient.folder_name
        if not folder:
            continue

        lab_history = get_patient_lab_history(
            storage, doc_index, "",
            patient_folder=folder,
        )
        cr_trend = extract_creatinine_trend(lab_history)

        if not cr_trend:
            continue

        with_data += 1
        latest_egfr = cr_trend[-1]["egfr"]
        stage = get_ckd_stage(latest_egfr)
        stage_key = stage["stage"]
        if stage_key in distribution:
            distribution[stage_key] += 1

        # Check rapid decline
        decline = detect_rapid_decline(cr_trend)
        if decline.get("category") == "rapid":
            rapid_count += 1

    stage_details = []
    for stage_key in ["G1", "G2", "G3a", "G3b", "G4", "G5"]:
        count = distribution[stage_key]
        pct = round(count / with_data * 100, 1) if with_data > 0 else 0
        colors = {
            "G1": "green", "G2": "yellow", "G3a": "orange",
            "G3b": "orange", "G4": "red", "G5": "darkred",
        }
        stage_details.append({
            "stage": stage_key,
            "count": count,
            "percentage": pct,
            "color": colors[stage_key],
        })

    return {
        "total_patients": total,
        "with_data": with_data,
        "no_data": total - with_data,
        "distribution": distribution,
        "stage_details": stage_details,
        "rapid_decliners": rapid_count,
        "note": "eGFR คำนวณด้วย CKD-EPI 2021 (race-free), "
                f"default age={DEFAULT_AGE}, "
                f"default gender={'female' if DEFAULT_IS_FEMALE else 'male'}",
    }


def get_rapid_decliners_alert(
    storage,
    doc_index,
    master_registry,
) -> list[dict]:
    """รายชื่อผู้ป่วยที่ eGFR ลดเร็ว — สำหรับแจ้งเตือนแพทย์.

    Returns:
        [{clinic_hn, current_egfr, ckd_stage, decline_rate, category,
          recommendation, data_points}]
    """
    try:
        from quarterly_compare import get_patient_lab_history
    except ImportError:
        return []

    patients = master_registry.list_all()
    alerts = []

    for patient in patients:
        folder = patient.folder_name
        if not folder:
            continue

        lab_history = get_patient_lab_history(
            storage, doc_index, "",
            patient_folder=folder,
        )
        cr_trend = extract_creatinine_trend(lab_history)

        if len(cr_trend) < 4:
            continue

        decline = detect_rapid_decline(cr_trend)
        if decline.get("category") in ("rapid", "moderate"):
            latest = cr_trend[-1]
            stage = get_ckd_stage(latest["egfr"])
            alerts.append({
                "clinic_hn": patient.clinic_hn,
                "current_egfr": latest["egfr"],
                "current_cr": latest["cr_value"],
                "ckd_stage": stage["stage"],
                "decline_rate": decline["rate"],
                "category": decline["category"],
                "recommendation": decline["recommendation"],
                "data_points": decline["data_points"],
                "time_span_months": decline["time_span_months"],
            })

    # เรียงตาม decline rate (ลดเร็วที่สุดก่อน)
    alerts.sort(key=lambda x: x.get("decline_rate", 0))
    return alerts


# ═══════════════════════════════════════════════════════════
# Module Status
# ═══════════════════════════════════════════════════════════


def get_status() -> dict:
    """สถานะ module."""
    return {
        "module": "ckd_staging",
        "version": "1.0",
        "formula": "CKD-EPI 2021 (race-free)",
        "default_age": DEFAULT_AGE,
        "default_gender": "female" if DEFAULT_IS_FEMALE else "male",
        "available": True,
    }
