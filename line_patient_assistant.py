#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Feature 6: LINE OA ผู้ช่วยผู้ป่วยเพิ่มเติม

ฟีเจอร์:
  1. นัดหมาย — บันทึก/ดู/ยกเลิกนัดหมาย + เตือนล่วงหน้า
  2. คำแนะนำสุขภาพ — ความรู้ไตเบื้องต้น (ไม่ใช้ External AI)
  3. ติดต่อเจ้าหน้าที่ — ส่งข้อความถึงทีม admin ผ่าน LINE

ข้อจำกัดด้านความปลอดภัย:
  - ไม่ส่งข้อมูลผู้ป่วยไป External AI
  - ไม่บันทึกข้อความหรือข้อมูลระบุตัวตนใน logs
  - Owner-isolated: ผู้ใช้เห็นเฉพาะนัดหมายของตัวเอง
  - ข้อมูลนัดหมายจัดเก็บใน GCS แยกตาม owner_hash
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 1. Constants & Data Models
# ═══════════════════════════════════════════════════════════

APPOINTMENTS_PREFIX = "_appointments/"
ESCALATION_PREFIX = "_escalations/"

# Appointment status
class AppointmentStatus(str, Enum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class Appointment:
    """ข้อมูลนัดหมาย."""
    appointment_id: str
    owner_hash: str
    date: str           # YYYY-MM-DD
    time: str           # HH:MM
    purpose: str        # เช่น "ฟอกไต", "ตรวจเลือด", "พบแพทย์"
    status: str = AppointmentStatus.SCHEDULED
    created_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "appointment_id": self.appointment_id,
            "owner_hash": self.owner_hash,
            "date": self.date,
            "time": self.time,
            "purpose": self.purpose,
            "status": self.status,
            "created_at": self.created_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Appointment":
        return cls(
            appointment_id=d.get("appointment_id", ""),
            owner_hash=d.get("owner_hash", ""),
            date=d.get("date", ""),
            time=d.get("time", ""),
            purpose=d.get("purpose", ""),
            status=d.get("status", AppointmentStatus.SCHEDULED),
            created_at=d.get("created_at", ""),
            notes=d.get("notes", ""),
        )


# ═══════════════════════════════════════════════════════════
# 2. Appointment Management — นัดหมาย
# ═══════════════════════════════════════════════════════════

# Thai date parsing patterns
THAI_DATE_PATTERNS = [
    # "25/12/2569" or "25/12/69"
    r"(\d{1,2})/(\d{1,2})/(\d{2,4})",
    # "25-12-2569"
    r"(\d{1,2})-(\d{1,2})-(\d{2,4})",
    # "25 ธ.ค. 2569" or "25 ธันวาคม 2569"
    r"(\d{1,2})\s+(ม\.?ค\.?|ก\.?พ\.?|มี\.?ค\.?|เม\.?ย\.?|พ\.?ค\.?|มิ\.?ย\.?|ก\.?ค\.?|ส\.?ค\.?|ก\.?ย\.?|ต\.?ค\.?|พ\.?ย\.?|ธ\.?ค\.?|มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)\s*(\d{2,4})?",
]

THAI_MONTH_MAP: dict[str, int] = {
    "ม.ค.": 1, "มค": 1, "มกราคม": 1,
    "ก.พ.": 2, "กพ": 2, "กุมภาพันธ์": 2,
    "มี.ค.": 3, "มีค": 3, "มีนาคม": 3,
    "เม.ย.": 4, "เมย": 4, "เมษายน": 4,
    "พ.ค.": 5, "พค": 5, "พฤษภาคม": 5,
    "มิ.ย.": 6, "มิย": 6, "มิถุนายน": 6,
    "ก.ค.": 7, "กค": 7, "กรกฎาคม": 7,
    "ส.ค.": 8, "สค": 8, "สิงหาคม": 8,
    "ก.ย.": 9, "กย": 9, "กันยายน": 9,
    "ต.ค.": 10, "ตค": 10, "ตุลาคม": 10,
    "พ.ย.": 11, "พย": 11, "พฤศจิกายน": 11,
    "ธ.ค.": 12, "ธค": 12, "ธันวาคม": 12,
}

# Time parsing
TIME_PATTERN = re.compile(r"(\d{1,2})[:.：](\d{2})\s*(?:น\.?)?")
TIME_WORD_PATTERN = re.compile(r"(เช้า|บ่าย|เย็น)")

# Purpose keywords
PURPOSE_KEYWORDS: dict[str, str] = {
    "ฟอกไต": "ฟอกไต",
    "ฟอกเลือด": "ฟอกไต",
    "HD": "ฟอกไต",
    "hd": "ฟอกไต",
    "ไตเทียม": "ฟอกไต",
    "ตรวจเลือด": "ตรวจเลือด",
    "เจาะเลือด": "ตรวจเลือด",
    "แล็บ": "ตรวจเลือด",
    "lab": "ตรวจเลือด",
    "พบแพทย์": "พบแพทย์",
    "หาหมอ": "พบแพทย์",
    "ตรวจร่างกาย": "ตรวจร่างกาย",
    "ตรวจสุขภาพ": "ตรวจร่างกาย",
    "ติดตามผล": "ติดตามผล",
    "followup": "ติดตามผล",
    "follow up": "ติดตามผล",
    "นัดตรวจ": "ตรวจติดตาม",
    "ตรวจไต": "ตรวจไต",
}


def _parse_thai_year(year_str: str) -> int:
    """แปลงปี พ.ศ./ค.ศ. → ค.ศ."""
    if not year_str:
        return datetime.now().year
    y = int(year_str)
    if y > 2500:
        return y - 543  # พ.ศ. → ค.ศ.
    if y < 100:
        # 2-digit year: ถ้า >50 ถือเป็น 19xx, ถ้า <=50 ถือเป็น 20xx
        # แต่สำหรับ พ.ศ. 2-digit: 69 = 2569 = 2026
        if y + 2500 > datetime.now().year + 543:
            return y + 1957  # e.g. 69 → 2026 (2569-543)
        return y + 2000
    return y


def _parse_thai_month(month_str: str) -> int | None:
    """แปลงชื่อเดือนไทย → เลขเดือน."""
    # Clean up dots and spaces
    cleaned = month_str.strip().replace(" ", "")
    for key, val in THAI_MONTH_MAP.items():
        if cleaned.startswith(key.replace(".", "").replace(" ", "")) or key.replace(".", "").startswith(cleaned.replace(".", "")):
            return val
    # Direct lookup
    return THAI_MONTH_MAP.get(month_str.strip())


def parse_appointment_text(text: str) -> dict[str, str]:
    """แยกข้อมูลนัดหมายจากข้อความ.

    รองรับ:
      - "นัดหมาย ฟอกไต 25/12/2569 09:00"
      - "นัด ตรวจเลือด พรุ่งนี้ เช้า"
      - "ตั้งนัด พบแพทย์ 3 ธ.ค. 10:30"

    Returns:
        dict with keys: date, time, purpose, notes
    """
    result: dict[str, str] = {
        "date": "",
        "time": "",
        "purpose": "",
        "notes": "",
    }

    now = datetime.now()

    # ── Extract purpose ──
    text_lower = text.lower()
    for keyword, purpose in PURPOSE_KEYWORDS.items():
        if keyword.lower() in text_lower:
            result["purpose"] = purpose
            break

    if not result["purpose"]:
        result["purpose"] = "นัดหมายทั่วไป"

    # ── Extract date ──
    # Check relative dates first
    if "วันนี้" in text:
        result["date"] = now.strftime("%Y-%m-%d")
    elif "พรุ่งนี้" in text:
        result["date"] = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    elif "มะรืน" in text or "มะรืนนี้" in text:
        result["date"] = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    else:
        # Try numeric patterns: DD/MM/YYYY or DD-MM-YYYY
        m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", text)
        if m:
            day = int(m.group(1))
            month = int(m.group(2))
            year = _parse_thai_year(m.group(3))
            try:
                dt = datetime(year, month, day)
                result["date"] = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

        # Try Thai month pattern
        if not result["date"]:
            m = re.search(
                r"(\d{1,2})\s+(ม\.?ค\.?|ก\.?พ\.?|มี\.?ค\.?|เม\.?ย\.?|พ\.?ค\.?|มิ\.?ย\.?|ก\.?ค\.?|ส\.?ค\.?|ก\.?ย\.?|ต\.?ค\.?|พ\.?ย\.?|ธ\.?ค\.?|มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)\s*(\d{2,4})?",
                text,
            )
            if m:
                day = int(m.group(1))
                month = _parse_thai_month(m.group(2))
                year_str = m.group(3) if m.group(3) else ""
                year = _parse_thai_year(year_str)
                if month:
                    try:
                        dt = datetime(year, month, day)
                        result["date"] = dt.strftime("%Y-%m-%d")
                    except ValueError:
                        pass

    # ── Extract time ──
    m = TIME_PATTERN.search(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            result["time"] = f"{hour:02d}:{minute:02d}"

    if not result["time"]:
        m = TIME_WORD_PATTERN.search(text)
        if m:
            word = m.group(1)
            if word == "เช้า":
                result["time"] = "08:00"
            elif word == "บ่าย":
                result["time"] = "13:00"
            elif word == "เย็น":
                result["time"] = "16:00"

    if not result["time"]:
        result["time"] = "09:00"  # Default

    return result


def save_appointment(
    storage: Any,
    owner_hash: str,
    parsed: dict[str, str],
    appointment_id: str | None = None,
) -> Appointment:
    """บันทึกนัดหมายลง GCS.

    Args:
        storage: GCS storage reference
        owner_hash: hash ของ LINE user
        parsed: ข้อมูลที่ parse แล้ว
        appointment_id: ระบุ ID เอง (สำหรับ test)

    Returns:
        Appointment object
    """
    now = datetime.now()
    if not appointment_id:
        appointment_id = f"appt-{now.strftime('%Y%m%d%H%M%S')}-{owner_hash[:6]}"

    appt = Appointment(
        appointment_id=appointment_id,
        owner_hash=owner_hash,
        date=parsed["date"],
        time=parsed["time"],
        purpose=parsed["purpose"],
        status=AppointmentStatus.SCHEDULED,
        created_at=now.isoformat(timespec="seconds"),
        notes=parsed.get("notes", ""),
    )

    path = f"{APPOINTMENTS_PREFIX}{appointment_id}.json"
    data = json.dumps(appt.to_dict(), ensure_ascii=False).encode("utf-8")
    storage.put(path, data, content_type="application/json")

    logger.info(f"Appointment saved: id={appointment_id[:8]}…")
    return appt


def list_appointments(
    storage: Any,
    owner_hash: str,
    status_filter: str | None = None,
    upcoming_only: bool = True,
) -> list[Appointment]:
    """ดึงรายการนัดหมายของผู้ใช้.

    Args:
        storage: GCS storage reference
        owner_hash: hash ของ LINE user
        status_filter: กรองตามสถานะ (scheduled/completed/cancelled)
        upcoming_only: แสดงเฉพาะนัดในอนาคต

    Returns:
        List of Appointment objects, sorted by date
    """
    appointments: list[Appointment] = []
    today = datetime.now().strftime("%Y-%m-%d")

    files = storage.list_files(prefix=APPOINTMENTS_PREFIX)
    for f in files:
        try:
            raw = storage.get(f)
            if not raw:
                continue
            data = json.loads(raw)
            # Owner isolation — fail-closed
            if data.get("owner_hash") != owner_hash:
                continue

            appt = Appointment.from_dict(data)

            # Status filter
            if status_filter and appt.status != status_filter:
                continue

            # Upcoming filter
            if upcoming_only and appt.date < today:
                continue

            appointments.append(appt)
        except (json.JSONDecodeError, KeyError):
            continue

    # Sort by date + time
    appointments.sort(key=lambda a: f"{a.date} {a.time}")
    return appointments


def cancel_appointment(
    storage: Any,
    owner_hash: str,
    appointment_id_prefix: str,
) -> str:
    """ยกเลิกนัดหมาย.

    Returns:
        ข้อความตอบกลับ
    """
    files = storage.list_files(prefix=APPOINTMENTS_PREFIX)
    for f in files:
        try:
            raw = storage.get(f)
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("owner_hash") != owner_hash:
                continue
            if data.get("appointment_id", "").startswith(appointment_id_prefix):
                data["status"] = AppointmentStatus.CANCELLED
                storage.put(f, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                           content_type="application/json")
                logger.info(f"Appointment cancelled: id={appointment_id_prefix[:8]}…")
                return (
                    f"✅ ยกเลิกนัดหมายแล้ว\n"
                    f"📅 {data['date']} เวลา {data['time']}\n"
                    f"📋 {data['purpose']}"
                )
        except (json.JSONDecodeError, KeyError):
            continue

    return "❌ ไม่พบนัดหมายที่ระบุ\nกรุณาตรวจสอบรหัสนัดหมาย"


def format_appointments_list(appointments: list[Appointment]) -> str:
    """จัดรูปแบบรายการนัดหมายเป็นข้อความ."""
    if not appointments:
        return (
            "📅 นัดหมายของคุณ\n\n"
            "ยังไม่มีนัดหมายที่กำลังจะมาถึง\n\n"
            "💡 พิมพ์ 'นัดหมาย [วัตถุประสงค์] [วันที่] [เวลา]'\n"
            "เช่น: นัดหมาย ฟอกไต 25/12/69 09:00"
        )

    lines = ["📅 นัดหมายที่กำลังจะมาถึง\n"]
    for i, appt in enumerate(appointments[:10], 1):
        status_icon = "🟢" if appt.status == AppointmentStatus.SCHEDULED else "🔴"
        lines.append(
            f"{i}. {status_icon} {appt.date} เวลา {appt.time}\n"
            f"   📋 {appt.purpose}"
        )
        if appt.notes:
            lines.append(f"   📝 {appt.notes}")
        # Show truncated ID for cancellation
        lines.append(f"   🔖 รหัส: {appt.appointment_id[:12]}")

    lines.append(
        "\n💡 ยกเลิกนัด: พิมพ์ 'ยกเลิกนัด [รหัส]'"
    )
    return "\n".join(lines)


def format_appointment_confirmation(appt: Appointment) -> str:
    """แสดงข้อความยืนยันนัดหมาย."""
    return (
        f"✅ บันทึกนัดหมายเรียบร้อย\n\n"
        f"📅 วันที่: {appt.date}\n"
        f"⏰ เวลา: {appt.time}\n"
        f"📋 วัตถุประสงค์: {appt.purpose}\n"
        f"🔖 รหัส: {appt.appointment_id[:12]}\n\n"
        f"💡 ดูนัดหมาย: พิมพ์ 'ดูนัด'\n"
        f"💡 ยกเลิก: พิมพ์ 'ยกเลิกนัด {appt.appointment_id[:12]}'"
    )


def get_upcoming_reminders(
    storage: Any,
    days_ahead: int = 1,
) -> list[tuple[str, Appointment]]:
    """ดึงนัดหมายที่จะถึงในอีก N วัน สำหรับส่ง reminder.

    Returns:
        List of (line_user_id_hash, Appointment) — ไม่มี line_user_id จริง
        เพราะระบบ owner-isolated ใช้ hash เท่านั้น
        การส่ง push_message ต้องมี mapping hash→userId ซึ่งจัดการใน server.py
    """
    target_date = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    reminders: list[tuple[str, Appointment]] = []

    files = storage.list_files(prefix=APPOINTMENTS_PREFIX)
    for f in files:
        try:
            raw = storage.get(f)
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("status") != AppointmentStatus.SCHEDULED:
                continue
            if data.get("date") == target_date:
                appt = Appointment.from_dict(data)
                reminders.append((appt.owner_hash, appt))
        except (json.JSONDecodeError, KeyError):
            continue

    return reminders


def format_reminder_message(appt: Appointment) -> str:
    """สร้างข้อความเตือนนัดหมาย."""
    return (
        f"🔔 เตือนนัดหมาย\n\n"
        f"📅 พรุ่งนี้ ({appt.date})\n"
        f"⏰ เวลา: {appt.time}\n"
        f"📋 {appt.purpose}\n\n"
        f"กรุณาเตรียมตัวและมาตามนัด\n"
        f"หากต้องการเลื่อนนัด กรุณาติดต่อเจ้าหน้าที่"
    )


# ═══════════════════════════════════════════════════════════
# 3. Health Advice — คำแนะนำสุขภาพไต (Local Content, No External AI)
# ═══════════════════════════════════════════════════════════

# ความรู้เรื่องไตและการดูแลสุขภาพ — hardcoded, ไม่ใช้ AI
HEALTH_TIPS: list[dict[str, str]] = [
    {
        "topic": "อาหาร",
        "title": "🍽️ อาหารสำหรับผู้ป่วยโรคไต",
        "content": (
            "• จำกัดโซเดียม (เกลือ) ไม่เกิน 2,000 มก./วัน\n"
            "• ลดอาหารที่มีโพแทสเซียมสูง เช่น กล้วย ส้ม มะละกอ\n"
            "• จำกัดฟอสฟอรัส — ลดนม ชีส ถั่ว ช็อกโกแลต\n"
            "• ควบคุมโปรตีนตามคำแนะนำแพทย์\n"
            "• ดื่มน้ำตามปริมาณที่แพทย์กำหนด\n"
            "• หลีกเลี่ยงอาหารแปรรูป อาหารกระป๋อง บะหมี่กึ่งสำเร็จรูป"
        ),
    },
    {
        "topic": "น้ำ",
        "title": "💧 การดื่มน้ำสำหรับผู้ป่วยไต",
        "content": (
            "• ปริมาณน้ำที่ดื่มขึ้นกับระยะของโรคไต\n"
            "• ผู้ป่วยฟอกไต: จำกัดน้ำตามที่แพทย์แนะนำ\n"
            "• น้ำหนักที่เพิ่มระหว่างฟอก ไม่ควรเกิน 2-3 กก.\n"
            "• วิธีลดกระหายน้ำ: อมน้ำแข็ง บ้วนปาก เคี้ยวหมากฝรั่ง\n"
            "• หลีกเลี่ยงอาหารเค็มซึ่งทำให้กระหายน้ำ"
        ),
    },
    {
        "topic": "ยา",
        "title": "💊 การใช้ยาสำหรับผู้ป่วยไต",
        "content": (
            "• ทานยาตามแพทย์สั่งอย่างเคร่งครัด\n"
            "• ห้ามหยุดยาเองโดยไม่ปรึกษาแพทย์\n"
            "• หลีกเลี่ยงยาแก้ปวด NSAIDs (เช่น Ibuprofen, Diclofenac)\n"
            "• ระวังยาสมุนไพรและอาหารเสริมที่อาจเป็นพิษต่อไต\n"
            "• แจ้งแพทย์ทุกครั้งที่ได้ยาใหม่จากที่อื่น\n"
            "• พกรายชื่อยาติดตัวเสมอ"
        ),
    },
    {
        "topic": "ออกกำลังกาย",
        "title": "🏃 การออกกำลังกายสำหรับผู้ป่วยไต",
        "content": (
            "• ออกกำลังกายเบาๆ สม่ำเสมอ เช่น เดิน ปั่นจักรยาน\n"
            "• เป้าหมาย 30 นาที/วัน อย่างน้อย 3-5 วัน/สัปดาห์\n"
            "• ไม่ออกกำลังกายหนักในวันฟอกไต\n"
            "• หยุดทันทีหากเหนื่อยผิดปกติ เจ็บหน้าอก\n"
            "• ปรึกษาแพทย์ก่อนเริ่มโปรแกรมออกกำลังกาย"
        ),
    },
    {
        "topic": "ฟอกไต",
        "title": "🏥 การเตรียมตัวก่อนฟอกไต",
        "content": (
            "• ล้างแขนบริเวณ Fistula/Graft ให้สะอาด\n"
            "• ชั่งน้ำหนักก่อนมาทุกครั้ง\n"
            "• แจ้งเจ้าหน้าที่หากมีไข้ บวม หรืออาการผิดปกติ\n"
            "• นำยาที่ทานประจำมาด้วย\n"
            "• ห้ามวัดความดัน เจาะเลือด หรือรัดแขนข้างที่มี Fistula\n"
            "• มาตามนัดสม่ำเสมอ ห้ามขาดนัดฟอกไต"
        ),
    },
    {
        "topic": "สัญญาณเตือน",
        "title": "⚠️ สัญญาณเตือนที่ต้องพบแพทย์ทันที",
        "content": (
            "• หอบเหนื่อย นอนราบไม่ได้\n"
            "• บวมมาก โดยเฉพาะหน้า มือ เท้า\n"
            "• ปัสสาวะน้อยลงมาก หรือไม่ปัสสาวะ\n"
            "• เจ็บหน้าอก ใจสั่น\n"
            "• ไข้สูง หนาวสั่น\n"
            "• คลื่นไส้ อาเจียนรุนแรง\n"
            "• เลือดออกจาก Fistula ไม่หยุด\n"
            "• ชัก หมดสติ\n\n"
            "📞 หากมีอาการข้างต้น โทรหาคลินิกทันที\n"
            "หรือพิมพ์ 'ติดต่อเจ้าหน้าที่' ในแชท"
        ),
    },
    {
        "topic": "ดูแลfistula",
        "title": "🩺 การดูแล Fistula (เส้นฟอกไต)",
        "content": (
            "• ตรวจสอบ thrill (สั่น) ที่ Fistula ทุกวัน\n"
            "• ล้างมือก่อนสัมผัส Fistula\n"
            "• ห้ามวัดความดันแขนข้างที่มี Fistula\n"
            "• ห้ามนอนทับหรือสวมนาฬิกา/สร้อยข้อมือข้างนั้น\n"
            "• ห้ามยกของหนักด้วยแขนข้าง Fistula\n"
            "• หากไม่รู้สึกสั่น หรือบวมแดง ให้มาพบแพทย์ทันที"
        ),
    },
    {
        "topic": "โพแทสเซียม",
        "title": "🍌 การควบคุมโพแทสเซียม",
        "content": (
            "โพแทสเซียมสูง อันตรายต่อหัวใจ ❗\n\n"
            "อาหารโพแทสเซียมสูง (ควรหลีกเลี่ยง):\n"
            "• กล้วย ส้ม มะละกอ ทุเรียน ขนุน\n"
            "• มันฝรั่ง มะเขือเทศ ผักโขม\n"
            "• ช็อกโกแลต กาแฟ น้ำมะพร้าว\n\n"
            "อาหารโพแทสเซียมต่ำ (ทานได้):\n"
            "• แอปเปิ้ล องุ่น สับปะรด แตงโม\n"
            "• แครอท ถั่วงอก กะหล่ำปลี\n\n"
            "💡 เคล็ดลับ: หั่นผักแช่น้ำ 2 ชม. ก่อนปรุง\n"
            "ช่วยลดโพแทสเซียมได้"
        ),
    },
]

# Keyword → topic mapping for quick lookup
HEALTH_TOPIC_KEYWORDS: dict[str, str] = {
    "อาหาร": "อาหาร", "กิน": "อาหาร", "ทาน": "อาหาร", "ห้ามกิน": "อาหาร",
    "น้ำ": "น้ำ", "ดื่ม": "น้ำ", "กระหาย": "น้ำ",
    "ยา": "ยา", "ยาแก้ปวด": "ยา", "สมุนไพร": "ยา",
    "ออกกำลังกาย": "ออกกำลังกาย", "วิ่ง": "ออกกำลังกาย", "เดิน": "ออกกำลังกาย",
    "ฟอกไต": "ฟอกไต", "ฟอกเลือด": "ฟอกไต", "เตรียมตัว": "ฟอกไต", "HD": "ฟอกไต",
    "อันตราย": "สัญญาณเตือน", "ฉุกเฉิน": "สัญญาณเตือน", "เตือน": "สัญญาณเตือน",
    "อาการ": "สัญญาณเตือน", "หมดสติ": "สัญญาณเตือน",
    "fistula": "ดูแลfistula", "เส้นฟอก": "ดูแลfistula", "thrill": "ดูแลfistula",
    "โพแทสเซียม": "โพแทสเซียม", "กล้วย": "โพแทสเซียม", "potassium": "โพแทสเซียม",
}


def get_health_advice(query: str = "") -> str:
    """คืนคำแนะนำสุขภาพ — local content เท่านั้น, ไม่ใช้ External AI.

    Args:
        query: คำค้น (optional) — ถ้าว่าง แสดง menu หัวข้อทั้งหมด

    Returns:
        ข้อความคำแนะนำสุขภาพ
    """
    if not query:
        # แสดง menu หัวข้อทั้งหมด
        lines = [
            "🏥 คำแนะนำสุขภาพสำหรับผู้ป่วยโรคไต\n",
            "เลือกหัวข้อที่สนใจ:\n",
        ]
        for i, tip in enumerate(HEALTH_TIPS, 1):
            lines.append(f"  {i}. {tip['title']}")
        lines.append(
            "\n💡 พิมพ์ 'สุขภาพ [หัวข้อ]' เช่น:\n"
            "  สุขภาพ อาหาร\n"
            "  สุขภาพ ฟอกไต\n"
            "  สุขภาพ โพแทสเซียม"
        )
        return "\n".join(lines)

    # ค้นหาตาม keyword
    query_lower = query.lower().strip()

    # Try by number
    try:
        idx = int(query_lower) - 1
        if 0 <= idx < len(HEALTH_TIPS):
            tip = HEALTH_TIPS[idx]
            return f"{tip['title']}\n\n{tip['content']}\n\n⚠️ ข้อมูลนี้เป็นความรู้ทั่วไป ไม่ใช่คำแนะนำทางการแพทย์\nกรุณาปรึกษาแพทย์สำหรับกรณีเฉพาะของคุณ"
    except ValueError:
        pass

    # Try by keyword
    matched_topic = HEALTH_TOPIC_KEYWORDS.get(query_lower)
    if not matched_topic:
        # Fuzzy search — check if any keyword is in the query
        for keyword, topic in HEALTH_TOPIC_KEYWORDS.items():
            if keyword in query_lower or query_lower in keyword:
                matched_topic = topic
                break

    if matched_topic:
        for tip in HEALTH_TIPS:
            if tip["topic"] == matched_topic:
                return f"{tip['title']}\n\n{tip['content']}\n\n⚠️ ข้อมูลนี้เป็นความรู้ทั่วไป ไม่ใช่คำแนะนำทางการแพทย์\nกรุณาปรึกษาแพทย์สำหรับกรณีเฉพาะของคุณ"

    # ไม่พบ — แสดง menu
    return (
        f"❓ ไม่พบหัวข้อ '{query}'\n\n"
        + get_health_advice("")
    )


# ═══════════════════════════════════════════════════════════
# 4. Staff Escalation — ติดต่อเจ้าหน้าที่
# ═══════════════════════════════════════════════════════════

def create_escalation(
    storage: Any,
    owner_hash: str,
    message: str,
    escalation_id: str | None = None,
) -> dict:
    """สร้างคำร้องติดต่อเจ้าหน้าที่.

    บันทึกข้อมูลการร้องขอ (ไม่เก็บข้อความจริง เก็บแค่ประเภท)
    เพื่อให้ admin เห็นว่ามีผู้ป่วยต้องการความช่วยเหลือ

    Args:
        storage: GCS storage reference
        owner_hash: hash ของ LINE user
        message: ข้อความ (จะ classify ประเภท แต่ไม่เก็บข้อความจริง)
        escalation_id: ระบุ ID เอง (สำหรับ test)

    Returns:
        dict with escalation info
    """
    now = datetime.now()
    if not escalation_id:
        escalation_id = f"esc-{now.strftime('%Y%m%d%H%M%S')}-{owner_hash[:6]}"

    # Classify urgency จาก keywords (ไม่เก็บข้อความจริง)
    urgency = "normal"
    urgent_keywords = ["ฉุกเฉิน", "ด่วน", "เลือด", "หมดสติ", "ชัก", "หายใจ",
                       "เจ็บหน้าอก", "ไม่หยุด", "urgent", "emergency"]
    message_lower = message.lower()
    for kw in urgent_keywords:
        if kw in message_lower:
            urgency = "urgent"
            break

    # Classify category (ไม่เก็บข้อความดิบ)
    category = "general"
    if any(w in message_lower for w in ["นัด", "เลื่อน", "ยกเลิก"]):
        category = "appointment"
    elif any(w in message_lower for w in ["ยา", "ใบสั่ง", "prescription"]):
        category = "medication"
    elif any(w in message_lower for w in ["ผล", "แล็บ", "ตรวจ"]):
        category = "lab_result"
    elif any(w in message_lower for w in ["เจ็บ", "ปวด", "ไข้", "อาการ", "ไม่สบาย"]):
        category = "symptom"

    escalation = {
        "escalation_id": escalation_id,
        "owner_hash": owner_hash,
        "category": category,
        "urgency": urgency,
        "status": "pending",
        "created_at": now.isoformat(timespec="seconds"),
        # ไม่เก็บข้อความจริงของผู้ป่วย — เก็บแค่ category/urgency
    }

    path = f"{ESCALATION_PREFIX}{escalation_id}.json"
    data = json.dumps(escalation, ensure_ascii=False).encode("utf-8")
    storage.put(path, data, content_type="application/json")

    logger.info(f"Escalation created: id={escalation_id[:8]}…, urgency={urgency}")
    return escalation


def format_escalation_confirmation(escalation: dict) -> str:
    """สร้างข้อความยืนยันการติดต่อเจ้าหน้าที่."""
    urgency_text = "🔴 ด่วน" if escalation["urgency"] == "urgent" else "🟡 ปกติ"
    category_map = {
        "appointment": "📅 เรื่องนัดหมาย",
        "medication": "💊 เรื่องยา",
        "lab_result": "🔬 เรื่องผลตรวจ",
        "symptom": "🩺 เรื่องอาการ",
        "general": "📋 เรื่องทั่วไป",
    }
    category_text = category_map.get(escalation["category"], "📋 เรื่องทั่วไป")

    return (
        f"📨 ส่งข้อความถึงเจ้าหน้าที่แล้ว\n\n"
        f"ระดับ: {urgency_text}\n"
        f"ประเภท: {category_text}\n"
        f"🔖 รหัส: {escalation['escalation_id'][:12]}\n\n"
        f"เจ้าหน้าที่จะติดต่อกลับโดยเร็ว\n"
        f"{'⚠️ กรณีฉุกเฉิน กรุณาโทรหาคลินิกโดยตรง' if escalation['urgency'] == 'urgent' else ''}"
    )


def list_pending_escalations(
    storage: Any,
    limit: int = 20,
) -> list[dict]:
    """ดึงรายการคำร้องที่รอดำเนินการ (สำหรับ admin).

    Returns:
        List of escalation dicts (ไม่มีข้อมูลผู้ป่วย)
    """
    escalations: list[dict] = []
    files = storage.list_files(prefix=ESCALATION_PREFIX)

    for f in files:
        try:
            raw = storage.get(f)
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("status") == "pending":
                # Truncate owner_hash for admin view
                data["owner_hash"] = data.get("owner_hash", "")[:8]
                escalations.append(data)
        except (json.JSONDecodeError, KeyError):
            continue

    # Sort by urgency (urgent first) then by date (newest first)
    escalations.sort(
        key=lambda e: (0 if e.get("urgency") == "urgent" else 1, e.get("created_at", "")),
    )
    return escalations[:limit]


def resolve_escalation(
    storage: Any,
    escalation_id_prefix: str,
) -> str:
    """ปิดคำร้อง (admin only).

    Returns:
        ข้อความตอบกลับ
    """
    files = storage.list_files(prefix=ESCALATION_PREFIX)
    for f in files:
        try:
            raw = storage.get(f)
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("escalation_id", "").startswith(escalation_id_prefix):
                data["status"] = "resolved"
                data["resolved_at"] = datetime.now().isoformat(timespec="seconds")
                storage.put(f, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                           content_type="application/json")
                return f"✅ ปิดคำร้อง {escalation_id_prefix[:12]} แล้ว"
        except (json.JSONDecodeError, KeyError):
            continue

    return "❌ ไม่พบคำร้องที่ระบุ"


# ═══════════════════════════════════════════════════════════
# 5. Admin API Blueprint — สำหรับ Web Dashboard
# ═══════════════════════════════════════════════════════════

def register_patient_assistant(app, storage_ref, doc_index_ref, audit_ref):
    """Register patient assistant admin API routes.

    Args:
        app: Flask app
        storage_ref: GCS storage
        doc_index_ref: Document index
        audit_ref: Audit logger (optional)
    """
    import os
    from flask import Blueprint, jsonify, request as flask_request

    pa_bp = Blueprint("patient_assistant", __name__)

    def _check_admin():
        secret = flask_request.args.get("secret", "")
        admin_secret = os.environ.get("ADMIN_SECRET", "")
        if not admin_secret or secret != admin_secret:
            return False
        return True

    @pa_bp.route("/admin/api/v2/patient/appointments", methods=["GET"])
    def api_appointments():
        if not _check_admin():
            return jsonify({"error": "unauthorized"}), 401
        status = flask_request.args.get("status", "")
        upcoming = flask_request.args.get("upcoming", "true").lower() == "true"
        owner = flask_request.args.get("owner", "")

        if owner:
            appointments = list_appointments(
                storage_ref, owner,
                status_filter=status or None,
                upcoming_only=upcoming,
            )
        else:
            # Admin view — all appointments
            appointments = []
            files = storage_ref.list_files(prefix=APPOINTMENTS_PREFIX)
            for f in files:
                try:
                    raw = storage_ref.get(f)
                    if not raw:
                        continue
                    data = json.loads(raw)
                    appt = Appointment.from_dict(data)
                    if status and appt.status != status:
                        continue
                    if upcoming and appt.date < datetime.now().strftime("%Y-%m-%d"):
                        continue
                    # Truncate owner_hash
                    appt.owner_hash = appt.owner_hash[:8]
                    appointments.append(appt)
                except (json.JSONDecodeError, KeyError):
                    continue
            appointments.sort(key=lambda a: f"{a.date} {a.time}")

        return jsonify({
            "total": len(appointments),
            "items": [a.to_dict() for a in appointments],
        })

    @pa_bp.route("/admin/api/v2/patient/escalations", methods=["GET"])
    def api_escalations():
        if not _check_admin():
            return jsonify({"error": "unauthorized"}), 401
        escalations = list_pending_escalations(storage_ref)
        return jsonify({
            "total": len(escalations),
            "items": escalations,
        })

    @pa_bp.route("/admin/api/v2/patient/escalation/<esc_id>/resolve", methods=["POST"])
    def api_resolve_escalation(esc_id):
        if not _check_admin():
            return jsonify({"error": "unauthorized"}), 401
        result = resolve_escalation(storage_ref, esc_id)
        return jsonify({"message": result})

    @pa_bp.route("/admin/api/v2/patient/reminders", methods=["GET"])
    def api_reminders():
        """ดูนัดหมายที่ต้องเตือนพรุ่งนี้."""
        if not _check_admin():
            return jsonify({"error": "unauthorized"}), 401
        days = int(flask_request.args.get("days", "1"))
        reminders = get_upcoming_reminders(storage_ref, days_ahead=days)
        return jsonify({
            "total": len(reminders),
            "reminders": [
                {
                    "owner_hash": oh[:8],
                    "date": appt.date,
                    "time": appt.time,
                    "purpose": appt.purpose,
                }
                for oh, appt in reminders
            ],
        })

    app.register_blueprint(pa_bp)
    logger.info("Patient Assistant: admin API registered")
