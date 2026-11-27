"""🪪 Dialysis Passport — บัตรฟอกไตฉุกเฉินสำหรับ ER / รพ.อื่น (ว้าว C, 27 พ.ย. 2569)

ผู้ป่วย (จาก Patient Portal) หรือเจ้าหน้าที่ สร้างลิงก์อ่านอย่างเดียวอายุสั้น (ค่าเริ่มต้น 72 ชม.)
+ QR → ER ต่างพื้นที่เห็นใน 10 วินาที: DW, สูตรฟอกล่าสุด, เส้นเลือด (ห้ามวัด BP/เจาะแขนไหน),
K/Hb/HBsAg/anti-HBs/HCV ล่าสุด, ยาปัจจุบัน, แพ้ยา, เบอร์คลินิก · ไทย/อังกฤษ

หลักความปลอดภัย (PDPA):
- สวิตช์ ⚙️ `passport_enabled` default off → ปิด = สร้างไม่ได้ และลิงก์เดิมเปิดไม่ได้
- token สุ่ม 32 ตัว, หมดอายุ, เพิกถอนได้, นับการเปิดทุกครั้ง + audit
- **ไม่มี Anti-HIV เด็ดขาด** (NEVER_SHOW) · ไม่แสดงเลขบัตรประชาชน
- หน้าใส่ X-Robots-Tag: noindex · ไม่มี JS ภายนอก
- ผู้ป่วยกดสร้างเองจาก Portal = ความยินยอม (บันทึก created_via='portal')
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SETTING_KEY = "passport_enabled"
TTL_SETTING = "passport_ttl_hours"
DEFAULT_TTL_HOURS = 72
MAX_ACTIVE_PER_HN = 3
NEVER_SHOW = ("antihiv", "hiv", "anti_hiv")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hd_passport_tokens (
    id SERIAL PRIMARY KEY,
    hn TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    created_by TEXT,
    created_via TEXT DEFAULT 'staff',
    revoked_at TIMESTAMPTZ,
    views INTEGER DEFAULT 0,
    last_viewed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_passport_hn ON hd_passport_tokens (hn);
"""


# ───────────────────────── switch / schema ─────────────────────────

def is_enabled(get_setting=None) -> bool:
    try:
        if get_setting is None:
            from .blueprint import get_setting_value as get_setting
        return str(get_setting(SETTING_KEY, "off") or "off").strip().lower() == "on"
    except Exception:
        return False


def ttl_hours(get_setting=None) -> int:
    try:
        if get_setting is None:
            from .blueprint import get_setting_value as get_setting
        v = int(float(get_setting(TTL_SETTING, DEFAULT_TTL_HOURS) or DEFAULT_TTL_HOURS))
        return max(1, min(v, 24 * 30))
    except Exception:
        return DEFAULT_TTL_HOURS


def ensure_schema(cur):
    cur.execute(CREATE_SQL)


def new_token() -> str:
    return secrets.token_urlsafe(24)  # 32 ตัว URL-safe


# ───────────────────────── tokens ─────────────────────────

def _row(r):
    if not r:
        return None
    return {"id": r[0], "hn": r[1], "token": r[2], "created_at": r[3], "expires_at": r[4],
            "created_by": r[5], "created_via": r[6], "revoked_at": r[7], "views": r[8] or 0, "last_viewed_at": r[9]}


_COLS = "id, hn, token, created_at, expires_at, created_by, created_via, revoked_at, views, last_viewed_at"


def active_for(cur, hn) -> list:
    cur.execute(f"""SELECT {_COLS} FROM hd_passport_tokens
                    WHERE hn = %s AND revoked_at IS NULL AND expires_at > NOW()
                    ORDER BY created_at DESC""", (str(hn),))
    return [_row(r) for r in cur.fetchall()]


def issue(cur, hn, created_by: str, via: str = "staff", hours: int = DEFAULT_TTL_HOURS, reuse: bool = True) -> dict:
    """ออก token ใหม่ (หรือใช้ตัวที่ยัง active อยู่ถ้า reuse และสร้างจากช่องทางเดียวกัน) · จำกัด MAX_ACTIVE_PER_HN"""
    hn = str(hn).strip()
    act = active_for(cur, hn)
    if reuse:
        for a in act:
            if a["created_via"] == via and (a["expires_at"] - datetime.now(timezone.utc)) > timedelta(hours=1):
                return a
    if len(act) >= MAX_ACTIVE_PER_HN:
        oldest = act[-1]
        cur.execute("UPDATE hd_passport_tokens SET revoked_at = NOW() WHERE id = %s", (oldest["id"],))
    tok = new_token()
    cur.execute(f"""INSERT INTO hd_passport_tokens (hn, token, expires_at, created_by, created_via)
                    VALUES (%s, %s, NOW() + (%s || ' hours')::interval, %s, %s)
                    RETURNING {_COLS}""", (hn, tok, str(int(hours)), (created_by or "")[:80], via[:20]))
    return _row(cur.fetchone())


def revoke(cur, token_id: int, hn: str | None = None) -> bool:
    if hn:
        cur.execute("UPDATE hd_passport_tokens SET revoked_at = NOW() WHERE id = %s AND hn = %s AND revoked_at IS NULL",
                    (int(token_id), str(hn)))
    else:
        cur.execute("UPDATE hd_passport_tokens SET revoked_at = NOW() WHERE id = %s AND revoked_at IS NULL", (int(token_id),))
    return cur.rowcount > 0


def lookup(cur, token: str) -> tuple[str, dict | None]:
    """→ (status, row) · status ∈ active | expired | revoked | missing"""
    if not token or len(token) < 16 or len(token) > 64:
        return "missing", None
    cur.execute(f"SELECT {_COLS} FROM hd_passport_tokens WHERE token = %s", (token,))
    r = _row(cur.fetchone())
    if not r:
        return "missing", None
    if r["revoked_at"]:
        return "revoked", r
    if r["expires_at"] <= datetime.now(timezone.utc):
        return "expired", r
    return "active", r


def record_view(cur, token_id: int):
    cur.execute("UPDATE hd_passport_tokens SET views = COALESCE(views,0) + 1, last_viewed_at = NOW() WHERE id = %s",
                (int(token_id),))


# ───────────────────────── data ─────────────────────────

def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _age(dob):
    if not dob:
        return None
    try:
        today = datetime.now(timezone.utc).date()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except Exception:
        return None


def _hours(started, ended):
    try:
        if started and ended:
            h = (ended - started).total_seconds() / 3600.0
            if 0.5 <= h <= 8:
                return round(h, 1)
    except Exception:
        pass
    return None


def assemble(cur, hn, get_setting=None) -> dict | None:
    """ข้อมูลที่ ER ต้องรู้ — เฉพาะชุดที่แพทย์อนุมัติ (ไม่มี Anti-HIV, ไม่มีเลขบัตร)"""
    hn = str(hn).strip()
    cur.execute("""SELECT id, patient_name, date_of_birth, gender, blood_type, allergies, primary_diagnosis,
                          vascular_access, dry_weight, schedule_days, esa_drug, esa_dose
                   FROM hd_patient_mapping WHERE hn = %s AND is_active = TRUE""", (hn,))
    p = cur.fetchone()
    if not p:
        return None
    pid = p[0]
    profile = {"hn": hn, "name": p[1], "age": _age(p[2]), "gender": p[3], "blood_type": p[4],
               "allergies": (p[5] or "").strip() or None, "diagnosis": (p[6] or "").strip() or None,
               "vascular_access": (p[7] or "").strip() or None, "dry_weight": _f(p[8]),
               "schedule_days": p[9], "esa": f"{p[10]} {p[11]}".strip() if (p[10] or p[11]) else None}

    rx = None
    try:
        cur.execute("""SELECT session_date, dialyzer_type, blood_flow_rate, dialysate_flow_rate, heparin_dose,
                              access_type, started_at, ended_at, uf_goal, post_weight
                       FROM hd_sessions WHERE patient_id = %s AND status IN ('completed','active','in_progress')
                       ORDER BY session_date DESC, id DESC LIMIT 1""", (pid,))
        s = cur.fetchone()
        if s:
            rx = {"date": s[0].isoformat() if s[0] else None, "dialyzer": s[1], "bfr": _f(s[2]), "dfr": _f(s[3]),
                  "heparin": s[4], "access": s[5], "hours": _hours(s[6], s[7]), "uf_goal": _f(s[8]),
                  "post_weight": _f(s[9])}
    except Exception as e:  # noqa: BLE001
        logger.debug("passport sessions: %s", e)

    labs = {}
    try:
        cur.execute("""SELECT lab_date, k, hb, hbsag, antihbs, antihcv, ca, phos, creatinine
                       FROM hd_labs WHERE patient_id = %s AND verified_status <> 'rejected'
                       ORDER BY lab_date DESC LIMIT 12""", (pid,))
        rows = cur.fetchall()
        keys = ("k", "hb", "hbsag", "antihbs", "antihcv", "ca", "phos", "creatinine")
        for r in rows:
            for i, k in enumerate(keys, start=1):
                if k not in labs and r[i] not in (None, ""):
                    v = r[i]
                    labs[k] = {"value": (float(v) if isinstance(v, (int, float)) or str(v).replace('.', '', 1).isdigit() else str(v)),
                               "date": r[0].isoformat() if r[0] else None}
    except Exception as e:  # noqa: BLE001
        logger.debug("passport labs: %s", e)
    for bad in NEVER_SHOW:
        labs.pop(bad, None)

    meds = []
    try:
        cur.execute("""SELECT drug_name, dose, route, frequency FROM hd_medication_orders
                       WHERE patient_id = %s AND is_active = TRUE ORDER BY drug_category NULLS LAST, drug_name LIMIT 25""", (pid,))
        meds = [{"name": r[0], "dose": r[1], "route": r[2], "freq": r[3]} for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        logger.debug("passport meds: %s", e)

    clinic = {"name": "คลินิกระนอง รักษ์ไต", "phone": ""}
    try:
        if get_setting is None:
            from .blueprint import get_setting_value as get_setting
        clinic = {"name": get_setting("clinic_name", "") or clinic["name"], "phone": get_setting("clinic_phone", "") or ""}
    except Exception:
        pass

    return {"profile": profile, "prescription": rx, "labs": labs, "medications": meds, "clinic": clinic,
            "generated_at": (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M")}


def access_warning(access_text: str | None) -> dict | None:
    """'AVF ซ้าย' → เตือนห้ามวัด BP/เจาะ/ให้น้ำเกลือแขนซ้าย (ไทย/อังกฤษ)"""
    if not access_text:
        return None
    t = access_text.lower()
    side_th = side_en = None
    if any(w in t for w in ("ซ้าย", "left", " lt", "(l)", "l ")):
        side_th, side_en = "ซ้าย", "LEFT"
    elif any(w in t for w in ("ขวา", "right", " rt", "(r)", "r ")):
        side_th, side_en = "ขวา", "RIGHT"
    is_fistula = any(w in t for w in ("avf", "avg", "fistula", "graft", "ฟิสตูล่า", "เส้นฟอก"))
    if not (side_th and is_fistula):
        return None
    return {"th": f"ห้ามวัดความดัน เจาะเลือด หรือให้สารน้ำที่แขน{side_th} (เส้นฟอกเลือด)",
            "en": f"NO BP cuff, venipuncture or IV on the {side_en} arm (dialysis access)"}
