"""HD Schedule Reminder — ระบบเตือนนัดฟอกเลือดผ่าน LINE

จัดการตารางฟอกเลือดของผู้ป่วยทุกราย และส่งข้อความเตือนผ่าน LINE OA
ก่อนวันนัดฟอก 1 วัน โดยอัตโนมัติ

ข้อมูลตาราง HD เก็บบน GCS ที่ _state/hd_schedules.json
ใช้ร่วมกับ patient_hn_registry (หา owner_hash จาก clinic_hn)
และ alert_delivery (_lookup_enrolled_line_id) เพื่อหา LINE userId

ไม่ log ข้อมูลผู้ป่วย (PDPA)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Bangkok timezone (UTC+7)
_BKK_TZ = timezone(timedelta(hours=7))

# GCS paths
_SCHEDULES_BLOB = "_state/hd_schedules.json"
_SCHEDULES_PREFIX = "_state/hd_schedules_v"

# Default day mapping by HD frequency
_DEFAULT_DAYS: dict[int, list[str]] = {
    3: ["mon", "wed", "fri"],
    2: ["tue", "thu"],
    1: ["wed"],
}

# Day name mapping (English short → Thai)
_DAY_TH: dict[str, str] = {
    "mon": "จันทร์",
    "tue": "อังคาร",
    "wed": "พุธ",
    "thu": "พฤหัสบดี",
    "fri": "ศุกร์",
    "sat": "เสาร์",
    "sun": "อาทิตย์",
}

# Day name mapping to Python weekday (0=Mon, 6=Sun)
_DAY_WEEKDAY: dict[str, int] = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}


# ─── Load / Save ─────────────────────────────────────────────


def load_schedules(storage) -> list[dict]:
    """โหลดตาราง HD จาก GCS — คืน list ของ schedule dicts."""
    if not storage or not storage.is_ready:
        return []
    try:
        # ลอง versioned files ก่อน (ใหม่กว่า)
        latest = _load_latest_versioned(storage)
        if latest is not None:
            return latest

        # Fallback: ไฟล์เดิม
        raw = storage.get(_SCHEDULES_BLOB)
        if raw:
            data = json.loads(raw)
            return data.get("schedules", [])
    except Exception as e:
        logger.warning("HD schedule load failed: %s", type(e).__name__)
    return []


def _load_latest_versioned(storage) -> Optional[list[dict]]:
    """โหลดไฟล์ versioned ล่าสุด (ถ้ามี)."""
    try:
        blobs = list(storage._client.list_blobs(
            storage.bucket_name,
            prefix=_SCHEDULES_PREFIX,
        ))
        if not blobs:
            return None
        # เรียงตามชื่อ — ชื่อมี timestamp จึงเรียงตามเวลาได้
        blobs.sort(key=lambda b: b.name, reverse=True)
        content = blobs[0].download_as_text(encoding="utf-8")
        data = json.loads(content)
        return data.get("schedules", [])
    except Exception:
        return None


def save_schedules(storage, schedules: list[dict]) -> bool:
    """บันทึกตาราง HD ไป GCS — ใช้ versioned write (ไม่ต้อง delete)."""
    if not storage or not storage.is_ready:
        return False
    try:
        data = {"schedules": schedules, "updated_at": _now_bkk_str()}
        content = json.dumps(data, ensure_ascii=False, indent=2)
        ts = int(datetime.now(_BKK_TZ).timestamp())
        blob_path = f"{_SCHEDULES_PREFIX}{ts}.json"
        blob = storage._bucket.blob(blob_path)
        blob.upload_from_string(
            content,
            content_type="application/json",
            if_generation_match=0,  # create-only
        )
        logger.info("HD schedules saved: %d entries", len(schedules))
        return True
    except Exception as e:
        logger.warning("HD schedule save failed: %s", type(e).__name__)
        return False


# ─── Schedule CRUD ───────────────────────────────────────────


def set_schedule(
    storage,
    clinic_hn: str,
    hd_per_week: int,
    days: Optional[list[str]] = None,
    time: str = "08:00",
) -> dict:
    """ตั้ง/อัปเดตตารางฟอกเลือดของผู้ป่วย.

    Args:
        clinic_hn: HN คลินิก
        hd_per_week: จำนวนครั้ง/สัปดาห์ (1, 2, 3)
        days: วันฟอก เช่น ["mon","wed","fri"] — ถ้าไม่ระบุใช้ default
        time: เวลานัด เช่น "08:00"

    Returns:
        {"success": True/False, "message": str}
    """
    clinic_hn = str(clinic_hn).strip()
    if not clinic_hn:
        return {"success": False, "message": "ต้องระบุ clinic_hn"}

    hd_per_week = int(hd_per_week) if hd_per_week else 0
    if hd_per_week not in (1, 2, 3):
        return {"success": False, "message": "hd_per_week ต้องเป็น 1, 2 หรือ 3"}

    if not days:
        days = _DEFAULT_DAYS.get(hd_per_week, ["mon", "wed", "fri"])

    # Validate days
    valid_days = set(_DAY_WEEKDAY.keys())
    days = [d.lower().strip() for d in days if d.lower().strip() in valid_days]
    if not days:
        return {"success": False, "message": "วัน (days) ไม่ถูกต้อง"}

    schedules = load_schedules(storage)

    # Update existing or add new
    found = False
    for s in schedules:
        if s.get("clinic_hn") == clinic_hn:
            s["hd_per_week"] = hd_per_week
            s["days"] = days
            s["time"] = time
            s["active"] = True
            s["updated_at"] = _now_bkk_str()
            found = True
            break

    if not found:
        schedules.append({
            "clinic_hn": clinic_hn,
            "hd_per_week": hd_per_week,
            "days": days,
            "time": time,
            "active": True,
            "updated_at": _now_bkk_str(),
        })

    ok = save_schedules(storage, schedules)
    action = "อัปเดต" if found else "เพิ่ม"
    return {
        "success": ok,
        "message": f"{action}ตาราง HD สำเร็จ" if ok else "บันทึกล้มเหลว",
    }


def get_schedule(storage, clinic_hn: str) -> Optional[dict]:
    """ดึงตาราง HD ของผู้ป่วยรายหนึ่ง."""
    clinic_hn = str(clinic_hn).strip()
    for s in load_schedules(storage):
        if s.get("clinic_hn") == clinic_hn and s.get("active", True):
            return s
    return None


# ─── Daily Patient Lists ────────────────────────────────────


def _get_patients_by_day(storage, day_name: str) -> list[dict]:
    """ดึงรายชื่อผู้ป่วยที่มีนัดในวันที่ระบุ."""
    day_name = day_name.lower().strip()
    results = []
    for s in load_schedules(storage):
        if not s.get("active", True):
            continue
        if day_name in s.get("days", []):
            results.append(s)
    return results


def get_todays_patients(storage) -> list[dict]:
    """ผู้ป่วยที่มีนัดฟอกวันนี้."""
    now = datetime.now(_BKK_TZ)
    day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today_name = day_names[now.weekday()]
    return _get_patients_by_day(storage, today_name)


def get_tomorrows_patients(storage) -> list[dict]:
    """ผู้ป่วยที่มีนัดฟอกพรุ่งนี้ (สำหรับส่ง reminder)."""
    tomorrow = datetime.now(_BKK_TZ) + timedelta(days=1)
    day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    tomorrow_name = day_names[tomorrow.weekday()]
    return _get_patients_by_day(storage, tomorrow_name)


# ─── LINE Reminders ──────────────────────────────────────────


def send_reminders(
    storage,
    master_registry,
    hn_registry,
    line_push_fn: Callable,
) -> dict:
    """ส่งข้อความเตือนนัดฟอกเลือดพรุ่งนี้ผ่าน LINE.

    Args:
        storage: GCS storage instance
        master_registry: MasterPatientRegistry instance
        hn_registry: PatientHNRegistry instance
        line_push_fn: function(user_id, messages) สำหรับส่ง LINE push

    Returns:
        {"sent": int, "skipped": int, "errors": int, "details": list}
    """
    tomorrow_patients = get_tomorrows_patients(storage)
    if not tomorrow_patients:
        return {"sent": 0, "skipped": 0, "errors": 0, "details": []}

    # หาชื่อวันพรุ่งนี้ (ภาษาไทย)
    tomorrow = datetime.now(_BKK_TZ) + timedelta(days=1)
    day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    tomorrow_day = day_names[tomorrow.weekday()]
    tomorrow_day_th = _DAY_TH.get(tomorrow_day, tomorrow_day)

    sent = 0
    skipped = 0
    errors = 0

    for sched in tomorrow_patients:
        clinic_hn = sched.get("clinic_hn", "")
        hd_time = sched.get("time", "08:00")

        # หาชื่อผู้ป่วยจาก master_registry
        patient_name = ""
        try:
            patient = master_registry.search(clinic_hn)
            if patient:
                p = patient[0]
                patient_name = getattr(p, "patient_name", "") or ""
        except Exception:
            pass

        if not patient_name:
            patient_name = f"HN {clinic_hn}"

        # หา LINE userId จาก hn_registry → owner_hash → line_id
        line_user_id = _resolve_patient_line_id(
            storage, clinic_hn, hn_registry
        )
        if not line_user_id:
            skipped += 1
            continue

        # สร้างข้อความเตือน
        msg_text = (
            f"🏥 แจ้งเตือน: คุณ{patient_name} "
            f"มีนัดฟอกเลือดพรุ่งนี้ (วัน{tomorrow_day_th}) "
            f"เวลา {hd_time} น. "
            f"ที่คลินิกระนอง รักษ์ไต"
        )

        try:
            line_push_fn(line_user_id, [{"type": "text", "text": msg_text}])
            sent += 1
        except Exception as e:
            logger.warning("HD reminder push failed: %s", type(e).__name__)
            errors += 1

    # ไม่ log ข้อมูลผู้ป่วย — log แค่จำนวน
    logger.info(
        "HD reminders: sent=%d skipped=%d errors=%d (total=%d)",
        sent, skipped, errors, len(tomorrow_patients),
    )

    return {"sent": sent, "skipped": skipped, "errors": errors}


def _resolve_patient_line_id(
    storage, clinic_hn: str, hn_registry
) -> str:
    """หา LINE userId ของผู้ป่วยจาก clinic_hn.

    Flow: clinic_hn → hn_registry.get_by_clinic_hn → owner_hash
          → alert_delivery._lookup_enrolled_line_id → LINE userId
    """
    try:
        patient_hn = hn_registry.get_by_clinic_hn(clinic_hn)
        if not patient_hn:
            return ""
        owner_hash = patient_hn.owner_hash
        if not owner_hash:
            return ""

        from alert_delivery import _lookup_enrolled_line_id
        return _lookup_enrolled_line_id(storage, owner_hash)
    except Exception:
        return ""


# ─── Admin API Helpers ───────────────────────────────────────


def api_list_schedules(storage) -> dict:
    """รายการตาราง HD ทั้งหมด (สำหรับ admin API)."""
    schedules = load_schedules(storage)
    active = [s for s in schedules if s.get("active", True)]
    return {
        "total": len(active),
        "schedules": active,
    }


def api_bulk_import_schedules(
    storage, schedule_list: list[dict]
) -> dict:
    """นำเข้าตาราง HD จำนวนมาก.

    Args:
        schedule_list: list of {"clinic_hn", "hd_per_week", "days"(optional)}

    Returns:
        {"imported": int, "skipped": int, "errors": int}
    """
    if not schedule_list:
        return {"imported": 0, "skipped": 0, "errors": 0}

    schedules = load_schedules(storage)
    existing_hns = {s["clinic_hn"] for s in schedules}

    imported = 0
    skipped = 0
    errs = 0

    for item in schedule_list:
        clinic_hn = str(item.get("clinic_hn", "")).strip()
        if not clinic_hn:
            errs += 1
            continue

        hd_per_week = item.get("hd_per_week", 0)
        try:
            hd_per_week = int(hd_per_week)
        except (ValueError, TypeError):
            errs += 1
            continue

        if hd_per_week not in (1, 2, 3):
            errs += 1
            continue

        days = item.get("days") or _DEFAULT_DAYS.get(hd_per_week, [])
        time_str = item.get("time", "08:00")

        if clinic_hn in existing_hns:
            # อัปเดต
            for s in schedules:
                if s.get("clinic_hn") == clinic_hn:
                    s["hd_per_week"] = hd_per_week
                    s["days"] = days
                    s["time"] = time_str
                    s["active"] = True
                    s["updated_at"] = _now_bkk_str()
                    break
            skipped += 1
        else:
            schedules.append({
                "clinic_hn": clinic_hn,
                "hd_per_week": hd_per_week,
                "days": days,
                "time": time_str,
                "active": True,
                "updated_at": _now_bkk_str(),
            })
            imported += 1
            existing_hns.add(clinic_hn)

    ok = save_schedules(storage, schedules)
    if not ok:
        return {"imported": 0, "skipped": 0, "errors": len(schedule_list),
                "message": "GCS save failed"}

    logger.info(
        "HD bulk import: imported=%d skipped=%d errors=%d",
        imported, skipped, errs,
    )
    return {"imported": imported, "skipped": skipped, "errors": errs}


# ─── Status ──────────────────────────────────────────────────


def get_status() -> dict:
    """สถานะ module."""
    return {
        "module": "hd_schedule_reminder",
        "available": True,
        "description": "ระบบเตือนนัดฟอกเลือดผ่าน LINE",
    }


# ─── Helpers ─────────────────────────────────────────────────


def _now_bkk_str() -> str:
    """ISO timestamp ใน timezone Bangkok."""
    return datetime.now(_BKK_TZ).isoformat(timespec="seconds")
