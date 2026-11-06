"""นัดหมายและการแจ้งเตือนผู้ป่วย — เจาะเลือด / รับยา / พบแพทย์ (+ ฟอกเลือดประจำ)

หลักการ:
  - เจ้าหน้าที่/แพทย์เป็นผู้ตั้งนัดใน LINE (ผู้ป่วยตั้งเองไม่ได้)
  - ส่งเตือนถึง "ผู้ป่วยที่ผูกบัญชี LINE แล้วเท่านั้น" (ผ่านรหัสผูกบัญชี)
    จึงไม่มีทางส่งผิดคน  ผู้ป่วยที่ยังไม่ผูก → ข้ามและนับเป็น skipped
  - ข้อความเตือนไม่มีผลแล็บ/ข้อมูลทางการแพทย์ — มีแค่ ประเภทนัด วันที่ เวลา
    และสิ่งที่ต้องเตรียม
  - เตือน 2 ครั้ง: ล่วงหน้า 1 วัน และเช้าวันนัด  ส่งซ้ำไม่ได้ (marker create-only)
  - นัดประจำ: รับยาทุก N วัน / เจาะเลือดทุก N เดือน → เมื่อถึงวันนัดแล้ว
    ระบบเลื่อนวันถัดไปให้เองอัตโนมัติ  ฟอกเลือดรายสัปดาห์ใช้
    hd_schedule_reminder ที่มีอยู่แล้ว (daily job เรียกให้)

ที่เก็บ (GCS, Service Account create-only → ทุกการแก้ไข = ไฟล์เวอร์ชันใหม่):
  _state/appointments/<APT-id>.json            นัดครั้งแรก
  _state/appointments/<APT-id>_v<ts>.json      สถานะใหม่ (ยืนยัน/เลื่อน/ยกเลิก)
  _state/appointments/_sent/<APT-id>_<kind>_<YYYY-MM-DD>.json  กันส่งซ้ำ
  _state/appointment_rules/<HN>_v<ts>.json     นัดประจำของผู้ป่วย (ล่าสุดชนะ)

ไม่ log ชื่อผู้ป่วย/HN เต็มในระบบ log (PDPA)
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_BKK = timezone(timedelta(hours=7))

APPT_PREFIX = "_state/appointments/"
SENT_PREFIX = "_state/appointments/_sent/"
RULES_PREFIX = "_state/appointment_rules/"

APPT_TYPES = {
    "lab": "เจาะเลือด",
    "rx": "รับยา",
    "doctor": "พบแพทย์",
    "hd": "ฟอกเลือด",
}
_TYPE_ALIASES = {
    "เจาะเลือด": "lab", "เจาะแล็บ": "lab", "แล็บ": "lab", "lab": "lab",
    "ตรวจเลือด": "lab",
    "รับยา": "rx", "ยา": "rx", "rx": "rx",
    "พบแพทย์": "doctor", "พบหมอ": "doctor", "หมอ": "doctor",
    "แพทย์": "doctor", "doctor": "doctor", "นัดพิเศษ": "doctor",
    "ฟอกเลือด": "hd", "ฟอก": "hd", "hd": "hd",
}
_PREP_TEXT = {
    "lab": "⚠️ เตรียมตัว: งดอาหาร/น้ำตามที่แพทย์หรือเจ้าหน้าที่แจ้ง และนำบัตรประชาชนมาด้วย",
    "rx": "📌 นำถุงยาเดิม/ยาที่เหลือมาด้วย เพื่อตรวจสอบรายการยา",
    "doctor": "📌 นำบัตรประชาชน สมุดประจำตัว และรายการยาที่ใช้อยู่มาด้วย",
    "hd": "📌 มาถึงก่อนเวลานัด 15 นาที",
}
STATUS_TH = {
    "scheduled": "รอยืนยัน",
    "confirmed": "ยืนยันแล้ว",
    "reschedule_requested": "ขอเลื่อน",
    "cancelled": "ยกเลิก",
    "done": "เสร็จสิ้น",
}

_ID_RE = re.compile(r"^APT-[0-9A-Z]{6}$")


def now_bkk() -> datetime:
    return datetime.now(_BKK)


def today_bkk() -> date:
    return now_bkk().date()


# ─────────────────────────────────────────────────────────────
# วันที่: รับ 15/11/2569, 15/11/2026, 2026-11-15, 15/11 (ปีปัจจุบัน)
# ─────────────────────────────────────────────────────────────
def parse_thai_date(text: str) -> Optional[date]:
    text = (text or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?$", text)
        if not m:
            return None
        d, mo = int(m.group(1)), int(m.group(2))
        y_raw = m.group(3)
        if y_raw is None:
            y = today_bkk().year
        else:
            y = int(y_raw)
            if y < 100:
                y += 2500 if y >= 60 else 2000
    if y > 2400:  # พ.ศ. → ค.ศ.
        y -= 543
    try:
        dt = date(y, mo, d)
    except ValueError:
        return None
    if m.lastindex is not None and m.group(3) is None and dt < today_bkk():
        # ไม่ระบุปีและวันผ่านไปแล้ว → ปีหน้า
        try:
            dt = date(y + 1, mo, d)
        except ValueError:
            return None
    return dt


def fmt_thai_date(d: date) -> str:
    months = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
              "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    days = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
    return f"วัน{days[d.weekday()]}ที่ {d.day} {months[d.month - 1]} {d.year + 543}"


def normalize_type(word: str) -> Optional[str]:
    return _TYPE_ALIASES.get((word or "").strip().lower())


# ─────────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────────
class AppointmentStore:
    def __init__(self, storage):
        self._storage = storage

    def _bucket(self):
        if self._storage is None or not getattr(self._storage, "is_ready", False):
            return None
        return getattr(self._storage, "_bucket", None)

    def _write_new(self, path: str, data: dict, quiet: bool = False) -> bool:
        bucket = self._bucket()
        if bucket is None:
            return False
        try:
            bucket.blob(path).upload_from_string(
                json.dumps(data, ensure_ascii=False),
                content_type="application/json",
                if_generation_match=0,
            )
            return True
        except Exception as e:
            if not quiet:
                logger.warning("appointment write failed: %s", type(e).__name__)
            return False

    # ── นัดครั้งเดียว ───────────────────────────────────────
    def create(
        self, clinic_hn: str, appt_type: str, on: date, at: str = "",
        note: str = "", created_by: str = "",
    ) -> Optional[dict]:
        if appt_type not in APPT_TYPES:
            return None
        appt_id = "APT-" + "".join(
            secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)
        )
        rec = {
            "id": appt_id,
            "clinic_hn": (clinic_hn or "").strip().upper(),
            "type": appt_type,
            "date": on.isoformat(),
            "time": (at or "").strip()[:5],
            "note": (note or "").strip()[:120],
            "status": "scheduled",
            "created_by": (created_by or "")[:16],
            "created_at": now_bkk().isoformat(),
            "updated_at": now_bkk().isoformat(),
        }
        return rec if self._write_new(f"{APPT_PREFIX}{appt_id}.json", rec) else None

    def _load_all(self) -> dict[str, dict]:
        """อ่านทุกนัด — ไฟล์เวอร์ชันล่าสุด (ชื่อเรียงท้ายสุด) ชนะ"""
        bucket = self._bucket()
        if bucket is None:
            return {}
        out: dict[str, dict] = {}
        try:
            blobs = sorted(
                (b for b in bucket.list_blobs(prefix=APPT_PREFIX)
                 if not b.name.startswith(SENT_PREFIX)),
                key=lambda b: b.name,
            )
            for b in blobs:
                try:
                    rec = json.loads(b.download_as_text())
                except Exception:
                    continue
                if rec.get("id"):
                    out[rec["id"]] = rec
        except Exception as e:
            logger.warning("appointment load failed: %s", type(e).__name__)
        return out

    def get(self, appt_id: str) -> Optional[dict]:
        appt_id = (appt_id or "").strip().upper()
        if not _ID_RE.match(appt_id):
            return None
        return self._load_all().get(appt_id)

    def list_for_hn(self, clinic_hn: str, include_past: bool = False) -> list[dict]:
        hn = (clinic_hn or "").strip().upper()
        today = today_bkk().isoformat()
        rows = [
            r for r in self._load_all().values()
            if r.get("clinic_hn") == hn
            and r.get("status") not in ("cancelled", "done")
            and (include_past or r.get("date", "") >= today)
        ]
        return sorted(rows, key=lambda r: (r.get("date", ""), r.get("time", "")))

    def list_on(self, on: date) -> list[dict]:
        key = on.isoformat()
        rows = [
            r for r in self._load_all().values()
            if r.get("date") == key and r.get("status") != "cancelled"
        ]
        return sorted(rows, key=lambda r: (r.get("time", ""), r.get("clinic_hn", "")))

    def set_status(self, appt_id: str, status: str, by: str = "", note: str = "") -> Optional[dict]:
        rec = self.get(appt_id)
        if rec is None or status not in STATUS_TH:
            return None
        rec = dict(rec)
        rec["status"] = status
        rec["updated_at"] = now_bkk().isoformat()
        rec["updated_by"] = (by or "")[:16]
        if note:
            rec["status_note"] = note[:120]
        ts = f"{time.time_ns()}{secrets.token_hex(2)}"
        ok = self._write_new(f"{APPT_PREFIX}{rec['id']}_v{ts}.json", rec)
        return rec if ok else None

    # ── กันส่งซ้ำ ──────────────────────────────────────────
    def mark_sent(self, appt_id: str, kind: str, on: date) -> bool:
        """True = ยังไม่เคยส่ง และจองสิทธิ์ส่งได้ (create-only = atomic)"""
        return self._write_new(
            f"{SENT_PREFIX}{appt_id}_{kind}_{on.isoformat()}.json",
            {"at": now_bkk().isoformat()},
            quiet=True,  # ส่งซ้ำ = ไฟล์มีอยู่แล้ว เป็นเรื่องปกติ ไม่ใช่ข้อผิดพลาด
        )

    # ── นัดประจำ ────────────────────────────────────────────
    def get_rule(self, clinic_hn: str) -> Optional[dict]:
        bucket = self._bucket()
        hn = (clinic_hn or "").strip().upper()
        if bucket is None or not hn:
            return None
        try:
            blobs = sorted(bucket.list_blobs(prefix=f"{RULES_PREFIX}{hn}_v"), key=lambda b: b.name)
            if not blobs:
                return None
            return json.loads(blobs[-1].download_as_text())
        except Exception:
            return None

    def set_rule(self, clinic_hn: str, updates: dict, by: str = "") -> Optional[dict]:
        hn = (clinic_hn or "").strip().upper()
        rule = self.get_rule(hn) or {"clinic_hn": hn}
        rule.update(updates)
        rule["updated_at"] = now_bkk().isoformat()
        rule["updated_by"] = (by or "")[:16]
        ts = f"{time.time_ns()}{secrets.token_hex(2)}"
        return rule if self._write_new(f"{RULES_PREFIX}{hn}_v{ts}.json", rule) else None

    def all_rules(self) -> list[dict]:
        bucket = self._bucket()
        if bucket is None:
            return []
        latest: dict[str, dict] = {}
        try:
            for b in sorted(bucket.list_blobs(prefix=RULES_PREFIX), key=lambda b: b.name):
                try:
                    r = json.loads(b.download_as_text())
                except Exception:
                    continue
                if r.get("clinic_hn"):
                    latest[r["clinic_hn"]] = r
        except Exception as e:
            logger.warning("rules load failed: %s", type(e).__name__)
        return list(latest.values())


# ─────────────────────────────────────────────────────────────
# ข้อความ
# ─────────────────────────────────────────────────────────────
def reminder_text(rec: dict, patient_name: str, kind: str) -> str:
    t = APPT_TYPES.get(rec.get("type", ""), rec.get("type", ""))
    d = date.fromisoformat(rec["date"])
    when = "พรุ่งนี้" if kind == "tomorrow" else "วันนี้"
    at = f" เวลา {rec['time']} น." if rec.get("time") else ""
    lines = [
        f"🔔 แจ้งเตือนนัด{t} {when}",
        f"👤 คุณ{patient_name}" if patient_name else "",
        f"📅 {fmt_thai_date(d)}{at}",
        "🏥 คลินิกระนอง รักษ์ไต",
    ]
    if rec.get("note"):
        lines.append(f"📝 {rec['note']}")
    lines.append(_PREP_TEXT.get(rec.get("type", ""), ""))
    lines.append("")
    lines.append(f"ตอบ \"ยืนยัน {rec['id']}\" เพื่อยืนยัน หรือ \"เลื่อนนัด {rec['id']}\" ถ้ามาไม่ได้")
    return "\n".join(x for x in lines if x)


def appointment_line(rec: dict) -> str:
    d = date.fromisoformat(rec["date"])
    at = f" {rec['time']} น." if rec.get("time") else ""
    return (
        f"• {rec['id']} {APPT_TYPES.get(rec['type'], rec['type'])} "
        f"{d.day}/{d.month}/{d.year + 543}{at} [{STATUS_TH.get(rec.get('status', ''), '')}]"
        + (f" — {rec['note']}" if rec.get("note") else "")
    )


# ─────────────────────────────────────────────────────────────
# งานประจำวัน
# ─────────────────────────────────────────────────────────────
def run_daily(
    store: AppointmentStore,
    resolve_line_id: Callable[[str], str],
    resolve_name: Callable[[str], str],
    push_text: Callable[[str, str], bool],
    staff_line_ids: list[str],
    today: Optional[date] = None,
) -> dict:
    """ส่งเตือนล่วงหน้า 1 วัน + เช้าวันนัด, เลื่อนนัดประจำ, สรุปให้เจ้าหน้าที่

    resolve_line_id(clinic_hn) → LINE userId ของผู้ป่วยที่ผูกบัญชีแล้ว ("" ถ้าไม่มี)
    """
    today = today or today_bkk()
    tomorrow = today + timedelta(days=1)
    result = {"sent": 0, "skipped_unlinked": 0, "already": 0, "errors": 0,
              "rolled": 0, "today_count": 0, "staff_notified": 0}

    # 1) นัดประจำ: สร้างนัดจริงล่วงหน้าเมื่อใกล้ถึง และเลื่อนรอบถัดไป
    for rule in store.all_rules():
        hn = rule.get("clinic_hn", "")
        for key, typ in (("rx", "rx"), ("lab", "lab")):
            nxt = rule.get(f"{key}_next")
            if not nxt:
                continue
            try:
                nd = date.fromisoformat(nxt)
            except ValueError:
                continue
            if nd <= tomorrow:
                existing = [
                    r for r in store.list_for_hn(hn, include_past=True)
                    if r.get("type") == typ and r.get("date") == nd.isoformat()
                ]
                if not existing:
                    store.create(hn, typ, nd, rule.get(f"{key}_time", ""),
                                 note="นัดประจำ", created_by="rule")
                # เลื่อนรอบถัดไป
                if key == "rx":
                    nd2 = nd + timedelta(days=int(rule.get("rx_every_days") or 30))
                else:
                    months = int(rule.get("lab_every_months") or 3)
                    mo = nd.month - 1 + months
                    nd2 = date(nd.year + mo // 12, mo % 12 + 1, min(nd.day, 28))
                store.set_rule(hn, {f"{key}_next": nd2.isoformat()}, by="rule")
                result["rolled"] += 1

    # 2) ส่งเตือน
    for kind, on in (("tomorrow", tomorrow), ("today", today)):
        for rec in store.list_on(on):
            if rec.get("status") in ("cancelled", "done"):
                continue
            line_id = resolve_line_id(rec.get("clinic_hn", ""))
            if not line_id:
                result["skipped_unlinked"] += 1
                continue
            if not store.mark_sent(rec["id"], kind, on):
                result["already"] += 1
                continue
            try:
                if push_text(line_id, reminder_text(rec, resolve_name(rec["clinic_hn"]), kind)):
                    result["sent"] += 1
                else:
                    result["errors"] += 1
            except Exception:
                result["errors"] += 1

    # 3) สรุปให้เจ้าหน้าที่ (รายชื่อนัดวันนี้ — ส่งครั้งเดียวต่อวัน)
    todays = store.list_on(today)
    result["today_count"] = len(todays)
    if staff_line_ids and store.mark_sent("STAFF", "summary", today):
        if todays:
            lines = [f"📋 นัดวันนี้ {fmt_thai_date(today)} ({len(todays)} ราย)"]
            for r in todays:
                nm = resolve_name(r["clinic_hn"]) or ""
                lines.append(
                    f"• {r.get('time') or '--:--'} {APPT_TYPES.get(r['type'], r['type'])} "
                    f"HN {r['clinic_hn']} {nm} [{STATUS_TH.get(r.get('status', ''), '')}]"
                )
            text = "\n".join(lines)
        else:
            text = f"📋 วันนี้ {fmt_thai_date(today)} ไม่มีนัดในระบบ"
        for sid in staff_line_ids:
            try:
                if push_text(sid, text):
                    result["staff_notified"] += 1
            except Exception:
                pass
    logger.info("daily reminders: %s", {k: v for k, v in result.items()})
    return result


# ─────────────────────────────────────────────────────────────
# Parser คำสั่ง LINE
# ─────────────────────────────────────────────────────────────
def parse_staff_appointment(text: str):
    """"นัด <HN> <ประเภท> <วันที่> [เวลา] [หมายเหตุ]" → dict หรือ None"""
    m = re.match(
        r"^\s*(?:นัด|ตั้งนัด)\s+(?:HN\s*)?([A-Za-z0-9-]{3,20})\s+(\S+)\s+(\S+)(?:\s+(\d{1,2}[:.]\d{2}))?(?:\s+(.+))?\s*$",
        text or "",
    )
    if not m:
        return None
    typ = normalize_type(m.group(2))
    on = parse_thai_date(m.group(3))
    if typ is None or on is None:
        return None
    at = (m.group(4) or "").replace(".", ":")
    if at and len(at.split(":")[0]) == 1:
        at = "0" + at
    return {"hn": m.group(1).upper(), "type": typ, "date": on, "time": at,
            "note": (m.group(5) or "").strip()}


def parse_staff_rule(text: str):
    """"นัดประจำ <HN> รับยา ทุก 30 วัน เริ่ม 20/11/2569 [เวลา]"
       "นัดประจำ <HN> เจาะเลือด ทุก 3 เดือน เริ่ม 15/11/2569"
    """
    m = re.match(
        r"^\s*นัดประจำ\s+(?:HN\s*)?([A-Za-z0-9-]{3,20})\s+(\S+)\s+ทุก\s*(\d{1,3})\s*(วัน|เดือน)\s+เริ่ม\s+(\S+)(?:\s+(\d{1,2}[:.]\d{2}))?\s*$",
        text or "",
    )
    if not m:
        return None
    typ = normalize_type(m.group(2))
    start = parse_thai_date(m.group(5))
    if typ not in ("rx", "lab") or start is None:
        return None
    n, unit = int(m.group(3)), m.group(4)
    return {"hn": m.group(1).upper(), "type": typ, "every": n, "unit": unit,
            "start": start, "time": (m.group(6) or "").replace(".", ":")}


def parse_view(text: str):
    """"ดูนัด <HN>" (เจ้าหน้าที่) / "ดูนัด" (ผู้ป่วย) → HN หรือ "" ; None ถ้าไม่ใช่"""
    m = re.match(r"^\s*(?:ดูนัด|นัดของฉัน|ดูนัดหมาย|นัดหมาย|นัดหมายของฉัน)\s*(?:HN\s*)?([A-Za-z0-9-]{3,20})?\s*$", text or "")
    return (m.group(1) or "").upper() if m else None


def parse_patient_action(text: str):
    """"ยืนยัน APT-xxxxxx" / "ยืนยันนัด APT-…" / "เลื่อนนัด APT-… [เหตุผล]" / "ยกเลิกนัด APT-…"
    → (action, id, note) หรือ None"""
    m = re.match(
        r"^\s*(ยืนยันนัด|ยืนยัน|เลื่อนนัด|ขอเลื่อน|ยกเลิกนัด)\s+(APT-[0-9A-Za-z]{6})(?:\s+(.+))?\s*$",
        text or "",
    )
    if not m:
        return None
    act = {"ยืนยันนัด": "confirm", "ยืนยัน": "confirm", "เลื่อนนัด": "reschedule",
           "ขอเลื่อน": "reschedule", "ยกเลิกนัด": "cancel"}[m.group(1)]
    return act, m.group(2).upper(), (m.group(3) or "").strip()
