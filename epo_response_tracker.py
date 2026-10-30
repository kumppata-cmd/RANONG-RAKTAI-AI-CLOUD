"""EPO Response Tracker — ติดตามการตอบสนองต่อ EPO (Erythropoietin)

วิเคราะห์ความสัมพันธ์ระหว่างระดับ Hb กับขนาด EPO ที่ใช้
เพื่อช่วยแพทย์ปรับขนาดยาและตรวจจับ ESA hyporesponsiveness

ใช้ข้อมูลจาก quarterly_compare.py:
- get_patient_lab_history → ค่า Hb/Hgb
- get_patient_rx_history → ชื่อยา EPO ที่ใช้

เกณฑ์ทางคลินิก (KDOQI / KDIGO):
- เป้าหมาย Hb: 10-12 g/dL
- ESA hyporesponsiveness: Hb <10 ทั้งที่ได้ EPO ≥3 เดือน
- Over-response: Hb >13 → ลดขนาด EPO

ไม่ log ข้อมูลผู้ป่วย (PDPA)
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# ─── EPO drug name patterns (case-insensitive) ─────────────

_EPO_PATTERNS: list[str] = [
    "epo",
    "erythropoietin",
    "epoetin",
    "epoetin alfa",
    "epoetin beta",
    "darbepoetin",
    "hemax",
    "hypercrit",
    "eposis",
    "epiao",
    "hema-plus",
    "hema plus",
    "hemaplus",
    "recormon",
    "eprex",
    "aranesp",
    "mircera",
    "cera",
    "อีพีโอ",
    "อิริโทรพอยอิติน",
]

# Compiled regex for EPO detection in drug text
_EPO_RE = re.compile(
    "|".join(re.escape(p) for p in _EPO_PATTERNS),
    re.IGNORECASE,
)

# Regex to extract dose from drug text: e.g. "EPO 4000 IU", "Hemax 4000"
_DOSE_RE = re.compile(
    r"(\d[\d,\.]*)\s*(?:iu|unit|ยูนิต|หน่วย|u\.?)?",
    re.IGNORECASE,
)

# ─── Hb analyte name patterns ──────────────────────────────

_HB_PATTERNS: list[str] = [
    "hb", "hgb", "hemoglobin", "haemoglobin",
    "ฮีโมโกลบิน", "hb.",
]

# Clinical thresholds
HB_TARGET_LOW = 10.0   # g/dL — KDIGO minimum target
HB_TARGET_HIGH = 12.0  # g/dL — KDIGO upper target
HB_DANGER_HIGH = 13.0  # g/dL — reduce EPO


# ─── Helper functions ──────────────────────────────────────

def _parse_date_month(date_str: str) -> str:
    """แปลง upload_date → YYYY-MM format."""
    if not date_str:
        return ""
    try:
        # handle both "2026-10-30" and "2026-10-30T12:00:00"
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m")
    except (ValueError, TypeError):
        # fallback: extract first 7 chars
        return date_str[:7] if len(date_str) >= 7 else ""


def _is_epo_drug(drug_name: str) -> bool:
    """ตรวจว่าชื่อยาเป็น EPO หรือไม่."""
    if not drug_name:
        return False
    return bool(_EPO_RE.search(drug_name))


def _extract_dose(text: str) -> str:
    """ดึงขนาดยาจากข้อความ เช่น '4000 IU' → '4000'."""
    if not text:
        return ""
    m = _DOSE_RE.search(text)
    if m:
        return m.group(1).replace(",", "")
    return ""


def _is_hb_analyte(analyte_name: str) -> bool:
    """ตรวจว่า analyte เป็น Hb/Hgb (hemoglobin concentration) หรือไม่.

    ต้องไม่จับ HbA1c, HbE, HbF ซึ่งเป็นคนละค่า
    """
    if not analyte_name:
        return False
    name_lower = analyte_name.strip().lower()
    # Exclude HbA1c, HbE, HbF, Hb typing etc.
    if re.search(r"hba1c|hb\s*a1c|hb\s*e\b|hb\s*f\b|hb\s*typ", name_lower):
        return False
    for pattern in _HB_PATTERNS:
        if pattern in name_lower:
            return True
    return False


def _parse_float(val: Any) -> float | None:
    """แปลง value เป็น float อย่างปลอดภัย."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        # handle strings like "12.5", "10.0 g/dL", ">15"
        cleaned = re.sub(r"[<>≤≥]", "", str(val).strip())
        cleaned = cleaned.split()[0] if cleaned else ""
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError, IndexError):
        return None


# ─── Core functions ────────────────────────────────────────

def extract_hb_trend(lab_history: list[dict]) -> list[dict]:
    """ดึง Hb/Hgb values จากประวัติผลแล็บ.

    Args:
        lab_history: จาก get_patient_lab_history()
            [{"doc_id", "upload_date", "results": [{"analyte", "value", "unit"}]}]

    Returns:
        [{"date": "2026-10-30", "month": "2026-10", "hb_value": 10.5}]
        เรียงจากเก่า → ใหม่
    """
    trend: list[dict] = []

    for entry in lab_history:
        upload_date = entry.get("upload_date", "")
        results = entry.get("results", [])

        for result in results:
            analyte = result.get("analyte", "")
            if not _is_hb_analyte(analyte):
                continue

            hb_val = _parse_float(result.get("value"))
            if hb_val is None or hb_val <= 0 or hb_val > 25:
                # ค่าผิดปกติ (Hb ปกติ 3-20 g/dL)
                continue

            trend.append({
                "date": upload_date,
                "month": _parse_date_month(upload_date),
                "hb_value": round(hb_val, 1),
            })
            break  # หนึ่ง entry มี Hb ค่าเดียว

    # เรียงจากเก่า → ใหม่
    trend.sort(key=lambda x: x.get("date", ""))
    return trend


def extract_epo_usage(rx_history: list[dict]) -> list[dict]:
    """ดึงข้อมูลการใช้ EPO จากประวัติใบยา.

    Args:
        rx_history: จาก get_patient_rx_history()
            [{"doc_id", "upload_date", "drugs": ["drug_name_str"]}]

    Returns:
        [{"date": "2026-10-30", "month": "2026-10",
          "epo_drug": "Hemax", "dose_text": "4000"}]
        เรียงจากเก่า → ใหม่
    """
    usage: list[dict] = []

    for entry in rx_history:
        upload_date = entry.get("upload_date", "")
        drugs = entry.get("drugs", [])

        for drug in drugs:
            drug_str = str(drug).strip()
            if not _is_epo_drug(drug_str):
                continue

            # หาชื่อยา EPO ที่ match
            match = _EPO_RE.search(drug_str)
            epo_name = match.group(0) if match else drug_str

            usage.append({
                "date": upload_date,
                "month": _parse_date_month(upload_date),
                "epo_drug": epo_name.strip().title(),
                "dose_text": _extract_dose(drug_str),
            })
            break  # หนึ่ง entry นับ EPO ครั้งเดียว

    usage.sort(key=lambda x: x.get("date", ""))
    return usage


def correlate_hb_epo(
    lab_history: list[dict],
    rx_history: list[dict],
) -> list[dict]:
    """รวม Hb trend กับ EPO usage ตามเดือน.

    Returns:
        [{"month": "2026-10", "hb": 10.5, "epo_drug": "Hemax",
          "epo_dose": "4000", "has_hb": True, "has_epo": True}]
        เรียงจากเก่า → ใหม่
    """
    hb_data = extract_hb_trend(lab_history)
    epo_data = extract_epo_usage(rx_history)

    # Group by month
    monthly: dict[str, dict] = defaultdict(lambda: {
        "month": "",
        "hb": None,
        "hb_values": [],
        "epo_drug": "",
        "epo_dose": "",
        "has_hb": False,
        "has_epo": False,
    })

    for hb in hb_data:
        month = hb["month"]
        if not month:
            continue
        monthly[month]["month"] = month
        monthly[month]["hb_values"].append(hb["hb_value"])
        monthly[month]["has_hb"] = True

    for epo in epo_data:
        month = epo["month"]
        if not month:
            continue
        monthly[month]["month"] = month
        monthly[month]["epo_drug"] = epo["epo_drug"]
        monthly[month]["epo_dose"] = epo["dose_text"]
        monthly[month]["has_epo"] = True

    # Average Hb per month
    result: list[dict] = []
    for month in sorted(monthly.keys()):
        entry = monthly[month]
        hb_vals = entry.pop("hb_values", [])
        if hb_vals:
            entry["hb"] = round(sum(hb_vals) / len(hb_vals), 1)
        result.append(entry)

    return result


def assess_epo_response(correlation_data: list[dict]) -> dict:
    """ประเมินการตอบสนองต่อ EPO.

    Args:
        correlation_data: จาก correlate_hb_epo()

    Returns:
        {
            "status": "responding" | "hyporesponsive" | "over_response"
                      | "no_epo" | "insufficient_data",
            "latest_hb": float | None,
            "hb_in_target": bool,
            "epo_months": int,
            "recommendation": str (ภาษาไทย),
            "trend": "stable" | "improving" | "declining" | "unknown",
        }
    """
    if not correlation_data:
        return {
            "status": "insufficient_data",
            "latest_hb": None,
            "hb_in_target": False,
            "epo_months": 0,
            "recommendation": "ข้อมูลไม่เพียงพอสำหรับประเมิน",
            "trend": "unknown",
        }

    # นับเดือนที่มี EPO
    epo_months = sum(1 for d in correlation_data if d.get("has_epo"))

    # หา Hb ล่าสุด
    hb_entries = [d for d in correlation_data if d.get("hb") is not None]
    latest_hb = hb_entries[-1]["hb"] if hb_entries else None

    # ไม่มีข้อมูล EPO เลย
    if epo_months == 0:
        return {
            "status": "no_epo",
            "latest_hb": latest_hb,
            "hb_in_target": (
                HB_TARGET_LOW <= latest_hb <= HB_TARGET_HIGH
                if latest_hb else False
            ),
            "epo_months": 0,
            "recommendation": "ไม่พบข้อมูลการใช้ EPO ในช่วงที่วิเคราะห์",
            "trend": "unknown",
        }

    # คำนวณ trend (3 ค่าล่าสุด)
    trend = "unknown"
    if len(hb_entries) >= 3:
        recent = [d["hb"] for d in hb_entries[-3:]]
        if recent[-1] > recent[0] + 0.5:
            trend = "improving"
        elif recent[-1] < recent[0] - 0.5:
            trend = "declining"
        else:
            trend = "stable"
    elif len(hb_entries) >= 2:
        if hb_entries[-1]["hb"] > hb_entries[-2]["hb"] + 0.5:
            trend = "improving"
        elif hb_entries[-1]["hb"] < hb_entries[-2]["hb"] - 0.5:
            trend = "declining"
        else:
            trend = "stable"

    if latest_hb is None:
        return {
            "status": "insufficient_data",
            "latest_hb": None,
            "hb_in_target": False,
            "epo_months": epo_months,
            "recommendation": "มีข้อมูล EPO แต่ไม่มีค่า Hb สำหรับประเมิน",
            "trend": trend,
        }

    hb_in_target = HB_TARGET_LOW <= latest_hb <= HB_TARGET_HIGH

    # ── Assess status ──

    # Over-response: Hb > 13
    if latest_hb > HB_DANGER_HIGH:
        return {
            "status": "over_response",
            "latest_hb": latest_hb,
            "hb_in_target": False,
            "epo_months": epo_months,
            "recommendation": (
                f"⚠️ Hb สูงเกินเป้า ({latest_hb:.1f} g/dL > {HB_DANGER_HIGH}) "
                f"— พิจารณาลดขนาด EPO หรือหยุดชั่วคราว"
            ),
            "trend": trend,
        }

    # Hyporesponsive: Hb < 10 despite EPO ≥3 months
    if latest_hb < HB_TARGET_LOW and epo_months >= 3:
        return {
            "status": "hyporesponsive",
            "latest_hb": latest_hb,
            "hb_in_target": False,
            "epo_months": epo_months,
            "recommendation": (
                f"⚠️ ESA hyporesponsiveness — Hb {latest_hb:.1f} g/dL "
                f"ต่ำกว่าเป้าแม้ได้ EPO {epo_months} เดือน "
                f"— ตรวจหาสาเหตุ: iron deficiency, infection, "
                f"inadequate dialysis, malnutrition"
            ),
            "trend": trend,
        }

    # Hb < target but EPO < 3 months — still early
    if latest_hb < HB_TARGET_LOW and epo_months < 3:
        return {
            "status": "responding",
            "latest_hb": latest_hb,
            "hb_in_target": False,
            "epo_months": epo_months,
            "recommendation": (
                f"Hb {latest_hb:.1f} g/dL — ยังต่ำกว่าเป้า "
                f"แต่ได้ EPO เพียง {epo_months} เดือน "
                f"— ติดตามต่อ"
            ),
            "trend": trend,
        }

    # In target — responding well
    return {
        "status": "responding",
        "latest_hb": latest_hb,
        "hb_in_target": hb_in_target,
        "epo_months": epo_months,
        "recommendation": (
            f"✅ Hb {latest_hb:.1f} g/dL อยู่ในเป้าหมาย "
            f"({HB_TARGET_LOW}-{HB_TARGET_HIGH} g/dL) "
            f"— คงขนาด EPO เดิม"
            if hb_in_target
            else f"Hb {latest_hb:.1f} g/dL — สูงกว่าเป้าเล็กน้อย ติดตามต่อ"
        ),
        "trend": trend,
    }


def get_patient_epo_summary(
    storage,
    doc_index,
    patient_folder: str,
    owner_hash: str = "",
) -> dict:
    """สรุป EPO response สำหรับผู้ป่วย 1 ราย.

    Args:
        storage: GCS storage instance
        doc_index: DocumentIndex instance
        patient_folder: patient_hn_folder เช่น "56001_นายปิยะ_อะนะบุหรง"
        owner_hash: fallback owner hash

    Returns:
        {
            "hb_trend": [...],
            "epo_usage": [...],
            "correlation": [...],
            "assessment": {...},
            "data_months": int,
        }
    """
    try:
        from quarterly_compare import (
            get_patient_lab_history,
            get_patient_rx_history,
        )

        lab_h = get_patient_lab_history(
            storage, doc_index, owner_hash,
            patient_folder=patient_folder,
        )
        rx_h = get_patient_rx_history(
            storage, doc_index, owner_hash,
            patient_folder=patient_folder,
        )

        hb_trend = extract_hb_trend(lab_h)
        epo_usage = extract_epo_usage(rx_h)
        correlation = correlate_hb_epo(lab_h, rx_h)
        assessment = assess_epo_response(correlation)

        return {
            "hb_trend": hb_trend,
            "epo_usage": epo_usage,
            "correlation": correlation,
            "assessment": assessment,
            "data_months": len(correlation),
        }

    except Exception as e:
        logger.error(
            "get_patient_epo_summary failed: %s", type(e).__name__
        )
        return {
            "hb_trend": [],
            "epo_usage": [],
            "correlation": [],
            "assessment": {
                "status": "insufficient_data",
                "recommendation": "ไม่สามารถดึงข้อมูลได้",
            },
            "data_months": 0,
        }


def get_clinic_epo_overview(
    storage,
    doc_index,
    master_registry,
) -> dict:
    """สรุป EPO response ทั้งคลินิก.

    Returns:
        {
            "total_assessed": int,
            "responding": int,
            "hyporesponsive": int,
            "over_response": int,
            "no_epo": int,
            "insufficient_data": int,
            "hyporesponsive_patients": [
                {"clinic_hn": str, "name": str, "hb": float, "epo_months": int}
            ],
            "over_response_patients": [...],
        }
    """
    overview = {
        "total_assessed": 0,
        "responding": 0,
        "hyporesponsive": 0,
        "over_response": 0,
        "no_epo": 0,
        "insufficient_data": 0,
        "hyporesponsive_patients": [],
        "over_response_patients": [],
    }

    try:
        patients = master_registry.list_all()
    except Exception as e:
        logger.error("get_clinic_epo_overview: registry error: %s", e)
        return overview

    for patient in patients:
        try:
            clinic_hn = getattr(patient, "clinic_hn", "") or ""
            patient_name = getattr(patient, "patient_name", "") or ""
            folder = getattr(patient, "folder_name", "") or ""

            if not folder:
                continue

            summary = get_patient_epo_summary(
                storage, doc_index, patient_folder=folder,
            )
            assessment = summary.get("assessment", {})
            status = assessment.get("status", "insufficient_data")

            overview["total_assessed"] += 1
            overview[status] = overview.get(status, 0) + 1

            # ไม่ log ชื่อ/HN ของผู้ป่วย (PDPA)
            patient_info = {
                "clinic_hn": clinic_hn,
                "name": patient_name[:10] + "…" if len(patient_name) > 10 else patient_name,
                "hb": assessment.get("latest_hb"),
                "epo_months": assessment.get("epo_months", 0),
                "trend": assessment.get("trend", "unknown"),
            }

            if status == "hyporesponsive":
                overview["hyporesponsive_patients"].append(patient_info)
            elif status == "over_response":
                overview["over_response_patients"].append(patient_info)

        except Exception:
            # ข้ามผู้ป่วยที่มีปัญหา — ไม่ log ข้อมูลผู้ป่วย
            overview["insufficient_data"] += 1
            continue

    return overview


def get_status() -> dict:
    """สถานะ EPO Response Tracker module."""
    return {
        "module": "epo_response_tracker",
        "available": True,
        "hb_target": f"{HB_TARGET_LOW}-{HB_TARGET_HIGH} g/dL",
        "epo_patterns": len(_EPO_PATTERNS),
        "thresholds": {
            "hyporesponsive": f"Hb <{HB_TARGET_LOW} with EPO ≥3 months",
            "over_response": f"Hb >{HB_DANGER_HIGH}",
        },
    }
