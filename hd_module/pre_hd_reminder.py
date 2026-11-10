"""F9: Pre-HD LINE Reminder — ส่งข้อความแจ้งเตือนคนไข้ล่วงหน้า 1 วัน.

Separate from locked hd_schedule_reminder.
Called by background scheduler daily at 18:00.
"""
import logging
from datetime import date, timedelta, timezone, datetime as _dt

logger = logging.getLogger(__name__)

# Thai day abbreviations → weekday index (Mon=0)
_THAI_DAY_MAP = {
    "จ": 0, "อ": 1, "พ": 2, "พฤ": 3, "ศ": 4, "ส": 5, "อา": 6,
}


def _parse_schedule_days(schedule_text):
    """Parse 'จ/พ/ศ' → set of weekday indices."""
    if not schedule_text:
        return set()
    result = set()
    # Split by common separators
    import re
    parts = re.split(r"[/,\-\s.]+", schedule_text.strip())
    for p in parts:
        p = p.strip()
        if p in _THAI_DAY_MAP:
            result.add(_THAI_DAY_MAP[p])
    return result


def get_tomorrow_patients(db_url):
    """Get patients scheduled for tomorrow."""
    import psycopg2
    _thai_tz = timezone(timedelta(hours=7))
    thai_today = _dt.now(_thai_tz).date()
    tomorrow = thai_today + timedelta(days=1)
    tomorrow_weekday = tomorrow.weekday()  # Mon=0

    conn = psycopg2.connect(db_url, connect_timeout=5)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT hn, patient_name, schedule_days, vascular_access
            FROM hd_patient_mapping
            WHERE is_active = TRUE AND schedule_days IS NOT NULL
              AND schedule_days != ''
        """)
        rows = cur.fetchall()
    conn.close()

    patients = []
    for hn, name, sched, va in rows:
        days = _parse_schedule_days(sched)
        if tomorrow_weekday in days:
            patients.append({
                "hn": hn, "name": name,
                "schedule": sched, "va": va,
            })
    return patients, tomorrow


def format_reminder_message(patient, tomorrow_date):
    """Format LINE reminder message."""
    thai_days = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
    day_name = thai_days[tomorrow_date.weekday()]
    dd = tomorrow_date.strftime("%d/%m")

    return (
        f"🔔 แจ้งเตือนนัดฟอกเลือด\n"
        f"━━━━━━━━━━━━\n"
        f"สวัสดีครับ/ค่ะ คุณ{patient['name']}\n"
        f"📅 พรุ่งนี้ วัน{day_name} ({dd})\n"
        f"🏥 คลินิกระนอง รักษ์ไต\n"
        f"━━━━━━━━━━━━\n"
        f"📋 เตรียมตัวก่อนฟอก:\n"
        f"  ✅ ทานยาตามปกติ\n"
        f"  ✅ อาบน้ำให้สะอาด\n"
        f"  ✅ นำบัตร รพ. มาด้วย\n"
        f"  ✅ จำกัดน้ำและอาหารเค็ม\n"
        f"━━━━━━━━━━━━\n"
        f"หากมีอาการผิดปกติหรือต้องเลื่อนนัด\n"
        f"กรุณาแจ้งล่วงหน้า ผ่าน LINE หรือติดต่อ รพ.ระนอง"
    )


def send_pre_hd_reminders(push_fn, db_url, master_registry=None):
    """Send LINE reminders to all patients scheduled tomorrow.

    Args:
        push_fn: LINE push function (user_id, message)
        db_url: Database URL
        master_registry: Master patient registry for LINE ID lookup
    Returns:
        dict with sent count and details
    """
    try:
        patients, tomorrow = get_tomorrow_patients(db_url)
    except Exception as e:
        logger.error("pre_hd_reminder DB error: %s", e)
        return {"sent": 0, "error": str(e)}

    if not patients:
        return {"sent": 0, "total_scheduled": 0, "message": "ไม่มีคนไข้นัดพรุ่งนี้"}

    sent = 0
    skipped = 0
    details = []

    for p in patients:
        # Find patient LINE ID from master registry
        line_id = None
        if master_registry:
            for reg_p in master_registry.list_all():
                if getattr(reg_p, "hn", "") == p["hn"]:
                    line_id = getattr(reg_p, "line_user_id", None)
                    break

        if not line_id:
            skipped += 1
            details.append({"hn": p["hn"], "name": p["name"], "status": "no_line_id"})
            continue

        msg = format_reminder_message(p, tomorrow)
        try:
            push_fn(line_id, msg)
            sent += 1
            details.append({"hn": p["hn"], "name": p["name"], "status": "sent"})
        except Exception as e:
            logger.warning("Reminder send failed for %s: %s", p["hn"], e)
            details.append({"hn": p["hn"], "name": p["name"], "status": f"error: {e}"})

    return {
        "sent": sent,
        "skipped": skipped,
        "total_scheduled": len(patients),
        "tomorrow": str(tomorrow),
        "details": details,
    }


def format_nurse_summary(result):
    """Format summary for nurse LINE group notification."""
    return (
        f"📋 สรุปนัดฟอกวันพรุ่งนี้ ({result.get('tomorrow','-')})\n"
        f"━━━━━━━━━━━━\n"
        f"📊 ทั้งหมด: {result.get('total_scheduled',0)} คน\n"
        f"✅ ส่ง LINE แจ้งเตือน: {result.get('sent',0)} คน\n"
        f"⚠️ ยังไม่ผูก LINE: {result.get('skipped',0)} คน\n"
        f"━━━━━━━━━━━━\n"
        f"กรุณาเตรียม Dialyzer และ VA supplies"
    )
