"""Dialysis Adequacy Tracker — ติดตามความเพียงพอของการฟอกเลือด

ตรวจสอบ Kt/V และ URR ของผู้ป่วยฟอกเลือดทุกราย ตามเกณฑ์ KDOQI:
  - Kt/V เป้าหมาย: ≥1.2 (ขั้นต่ำ), ≥1.4 (เหมาะสม)
  - URR เป้าหมาย: ≥65%
  - แจ้งเตือนเมื่อ Kt/V ลดลง >0.1 ใน 3 เดือน

ใช้ข้อมูลจาก quarterly_compare.get_patient_dialysis_history()
ไม่ log ข้อมูลผู้ป่วย (PDPA)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ─── KDOQI Clinical Thresholds ───────────────────────────────

KT_V_MINIMUM = 1.2       # ขั้นต่ำ (KDOQI target)
KT_V_OPTIMAL = 1.4       # เหมาะสม
URR_MINIMUM = 65.0        # ขั้นต่ำ (%)
KT_V_DECLINE_THRESHOLD = 0.1  # ลดลง > 0.1 ใน 3 เดือน ถือว่า declining


# ─── Core Calculations ───────────────────────────────────────

def calculate_urr(pre_bun: float, post_bun: float) -> float | None:
    """คำนวณ Urea Reduction Ratio (URR).

    URR = (pre_BUN - post_BUN) / pre_BUN × 100

    Returns:
        URR เป็น %, None ถ้าค่า input ไม่ถูกต้อง
    """
    if pre_bun is None or post_bun is None:
        return None
    try:
        pre = float(pre_bun)
        post = float(post_bun)
    except (TypeError, ValueError):
        return None
    if pre <= 0:
        return None
    urr = ((pre - post) / pre) * 100.0
    return round(urr, 1)


def assess_adequacy(
    kt_v: float | None = None,
    urr: float | None = None,
) -> dict[str, Any]:
    """ประเมินความเพียงพอของการฟอกเลือดตามเกณฑ์ KDOQI.

    Returns:
        dict with: adequate, kt_v_status, urr_status, recommendation (Thai)
    """
    result: dict[str, Any] = {
        "adequate": False,
        "kt_v_status": "missing",
        "kt_v_value": kt_v,
        "urr_status": "missing",
        "urr_value": urr,
        "recommendation": "",
    }

    kt_v_ok = None
    urr_ok = None

    # ── Kt/V assessment ──
    if kt_v is not None:
        try:
            kv = float(kt_v)
            if kv >= KT_V_OPTIMAL:
                result["kt_v_status"] = "optimal"
                kt_v_ok = True
            elif kv >= KT_V_MINIMUM:
                result["kt_v_status"] = "adequate"
                kt_v_ok = True
            else:
                result["kt_v_status"] = "inadequate"
                kt_v_ok = False
        except (TypeError, ValueError):
            result["kt_v_status"] = "invalid"

    # ── URR assessment ──
    if urr is not None:
        try:
            u = float(urr)
            if u >= URR_MINIMUM:
                result["urr_status"] = "adequate"
                urr_ok = True
            else:
                result["urr_status"] = "inadequate"
                urr_ok = False
        except (TypeError, ValueError):
            result["urr_status"] = "invalid"

    # ── Overall adequacy (need at least one metric) ──
    if kt_v_ok is not None and urr_ok is not None:
        result["adequate"] = kt_v_ok and urr_ok
    elif kt_v_ok is not None:
        result["adequate"] = kt_v_ok
    elif urr_ok is not None:
        result["adequate"] = urr_ok
    # else: both missing → stays False

    # ── Thai recommendation ──
    recommendations = []
    if kt_v_ok is False:
        recommendations.append(
            f"Kt/V ({kt_v:.2f}) ต่ำกว่าเกณฑ์ (≥{KT_V_MINIMUM}) "
            "— พิจารณาเพิ่มเวลาฟอก/อัตราเลือด/ขนาด dialyzer"
        )
    if urr_ok is False:
        recommendations.append(
            f"URR ({urr:.1f}%) ต่ำกว่าเกณฑ์ (≥{URR_MINIMUM}%) "
            "— พิจารณาปรับ dialysis prescription"
        )
    if kt_v_ok is True and result["kt_v_status"] == "adequate":
        recommendations.append(
            f"Kt/V ({kt_v:.2f}) ผ่านเกณฑ์ขั้นต่ำ "
            f"— เป้าหมายเหมาะสม ≥{KT_V_OPTIMAL}"
        )
    if not recommendations:
        if result["adequate"]:
            recommendations.append("ผลการฟอกเลือดเพียงพอตามเกณฑ์ KDOQI")
        elif kt_v is None and urr is None:
            recommendations.append(
                "ไม่มีข้อมูล Kt/V หรือ URR — "
                "ต้องตรวจเพิ่มเพื่อประเมินความเพียงพอ"
            )

    result["recommendation"] = "; ".join(recommendations)
    return result


# ─── Patient Trend Analysis ──────────────────────────────────

def get_patient_adequacy_trend(
    storage,
    doc_index,
    patient_folder: str,
    months: int = 6,
) -> list[dict]:
    """ดึงเทรนด์ Kt/V + URR ของผู้ป่วยรายนี้ย้อนหลัง N เดือน.

    Returns:
        list of {"date", "kt_v", "urr", "adequate", "assessment"}
        เรียงจากเก่า → ใหม่
    """
    try:
        from quarterly_compare import get_patient_dialysis_history
    except ImportError:
        logger.warning("quarterly_compare not available")
        return []

    history = get_patient_dialysis_history(
        storage, doc_index, owner_hash="", patient_folder=patient_folder,
    )
    if not history:
        return []

    # Filter by date range
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    trend = []
    for entry in history:
        upload_date = entry.get("upload_date", "")
        if upload_date and upload_date < cutoff_str:
            continue

        data = entry.get("data", {})
        kt_v = _safe_float(data.get("kt_v"))
        # URR might not be directly in data — calculate if BUN values present
        urr = _safe_float(data.get("urr"))
        if urr is None:
            pre_bun = _safe_float(data.get("pre_bun"))
            post_bun = _safe_float(data.get("post_bun"))
            if pre_bun is not None and post_bun is not None:
                urr = calculate_urr(pre_bun, post_bun)

        assessment = assess_adequacy(kt_v=kt_v, urr=urr)

        trend.append({
            "date": upload_date,
            "kt_v": kt_v,
            "urr": urr,
            "adequate": assessment["adequate"],
            "kt_v_status": assessment["kt_v_status"],
            "urr_status": assessment["urr_status"],
        })

    # Sort old → new
    trend.sort(key=lambda x: x.get("date", ""))
    return trend


def detect_declining_kt_v(trend: list[dict]) -> dict | None:
    """ตรวจว่า Kt/V ลดลง >0.1 ใน 3 เดือนล่าสุดหรือไม่.

    Returns:
        dict {"declining": True, "drop": float, "from": float, "to": float,
              "from_date": str, "to_date": str}
        หรือ None ถ้าไม่ declining / ข้อมูลไม่พอ
    """
    # Filter entries that have Kt/V, last 3 months
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    recent = [
        t for t in trend
        if t.get("kt_v") is not None and t.get("date", "") >= cutoff_str
    ]
    if len(recent) < 2:
        return None

    # Compare earliest vs latest in the 3-month window
    first = recent[0]
    last = recent[-1]
    drop = first["kt_v"] - last["kt_v"]

    if drop > KT_V_DECLINE_THRESHOLD:
        return {
            "declining": True,
            "drop": round(drop, 2),
            "from_value": first["kt_v"],
            "to_value": last["kt_v"],
            "from_date": first["date"],
            "to_date": last["date"],
        }
    return None


# ─── Clinic-Wide Summary ─────────────────────────────────────

def get_clinic_adequacy_summary(
    storage,
    doc_index,
    master_registry,
) -> dict[str, Any]:
    """สรุปความเพียงพอการฟอกเลือดทั้งคลินิก.

    Returns:
        {total_patients, adequate_count, inadequate_count,
         missing_data_count, declining_count,
         patients_below_target: [{clinic_hn, name, last_kt_v, last_date, status}]}
    """
    summary: dict[str, Any] = {
        "total_patients": 0,
        "adequate_count": 0,
        "inadequate_count": 0,
        "missing_data_count": 0,
        "declining_count": 0,
        "patients_below_target": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        patients = master_registry.list_all()
    except Exception:
        logger.warning("master_registry.list_all() failed")
        return summary

    summary["total_patients"] = len(patients)

    for patient in patients:
        folder = patient.folder_name
        if not folder:
            summary["missing_data_count"] += 1
            continue

        trend = get_patient_adequacy_trend(
            storage, doc_index, patient_folder=folder, months=3,
        )

        if not trend:
            summary["missing_data_count"] += 1
            continue

        # Use latest entry
        latest = trend[-1]
        if latest["adequate"]:
            summary["adequate_count"] += 1
        else:
            summary["inadequate_count"] += 1
            # ไม่ log ชื่อจริง — ใช้ HN prefix เท่านั้น
            summary["patients_below_target"].append({
                "clinic_hn": patient.clinic_hn,
                "name": patient.patient_name,
                "last_kt_v": latest.get("kt_v"),
                "last_urr": latest.get("urr"),
                "last_date": latest.get("date", ""),
                "status": latest.get("kt_v_status", "unknown"),
            })

        # Check declining trend
        decline = detect_declining_kt_v(trend)
        if decline:
            summary["declining_count"] += 1

    return summary


def get_inadequate_patients_alert(
    storage,
    doc_index,
    master_registry,
) -> list[dict]:
    """รายชื่อผู้ป่วยที่ Kt/V < 1.2 หรือ URR < 65% — สำหรับแจ้งเตือนแพทย์.

    Returns:
        list of {clinic_hn, name, kt_v, urr, date, recommendation, declining}
    """
    alerts = []

    try:
        patients = master_registry.list_all()
    except Exception:
        return alerts

    for patient in patients:
        folder = patient.folder_name
        if not folder:
            continue

        trend = get_patient_adequacy_trend(
            storage, doc_index, patient_folder=folder, months=3,
        )
        if not trend:
            continue

        latest = trend[-1]
        if latest["adequate"]:
            continue

        assessment = assess_adequacy(
            kt_v=latest.get("kt_v"), urr=latest.get("urr"),
        )
        decline = detect_declining_kt_v(trend)

        alerts.append({
            "clinic_hn": patient.clinic_hn,
            "name": patient.patient_name,
            "kt_v": latest.get("kt_v"),
            "urr": latest.get("urr"),
            "date": latest.get("date", ""),
            "recommendation": assessment["recommendation"],
            "declining": decline is not None,
            "decline_info": decline,
        })

    # Sort: declining first, then by Kt/V ascending
    alerts.sort(key=lambda a: (
        not a.get("declining", False),
        a.get("kt_v") if a.get("kt_v") is not None else 999,
    ))

    return alerts


# ─── Status ──────────────────────────────────────────────────

def get_status() -> dict:
    """สถานะโมดูล Dialysis Adequacy Tracker."""
    available = False
    try:
        from quarterly_compare import get_patient_dialysis_history  # noqa: F401
        available = True
    except ImportError:
        pass

    return {
        "available": available,
        "thresholds": {
            "kt_v_minimum": KT_V_MINIMUM,
            "kt_v_optimal": KT_V_OPTIMAL,
            "urr_minimum": URR_MINIMUM,
            "decline_threshold": KT_V_DECLINE_THRESHOLD,
        },
    }


# ─── Helpers ─────────────────────────────────────────────────

def _safe_float(val) -> float | None:
    """แปลงค่าเป็น float อย่างปลอดภัย."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
