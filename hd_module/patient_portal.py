"""Patient Portal — patient-facing self-view with friendly Thai interpretations (ว้าว #3).

Accessed via /hd/api/v1/my-results/<token> (JSON) or /hd/my/<token> (web page).
Token = HN for now (no auth except obscurity — will upgrade to HMAC-signed link per patient).

Returns data the patient can understand:
- Profile & schedule with next-dialysis countdown
- Lab trends (Hb, Hct, Kt/V, Ferritin, iPTH) with plain-Thai interpretation
- BCM / Fluid status summary
- Last 10 sessions
- Friendly health tips

**Never shows**: raw alert severity, physician orders, nurse notes, IDH risk scores,
or any clinical decision-support detail. This is the patient's own health diary.
"""
import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

WEEK_DAYS = {"จ": 0, "อ": 1, "พ": 2, "พฤ": 3, "ศ": 4, "ส": 5, "อา": 6}
DAY_TH = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
SHIFT_TH = {"morning": "รอบเช้า", "afternoon": "รอบบ่าย", "evening": "รอบค่ำ"}


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def _next_dialysis(schedule_days):
    """From schedule_days 'จ,พ,ศ' → next dialysis date and Thai day name."""
    if not schedule_days:
        return None
    parts = [d.strip() for d in schedule_days.replace("/", ",").split(",") if d.strip()]
    wkdays = [WEEK_DAYS[p] for p in parts if p in WEEK_DAYS]
    if not wkdays:
        return None
    today = _thai_today()
    tw = today.weekday()
    for i in range(7):
        d = (tw + i) % 7
        if d in wkdays:
            dt = today + timedelta(days=i)
            return {"date": dt.isoformat(), "day": DAY_TH[d], "in_days": i,
                    "text": "วันนี้" if i == 0 else ("พรุ่งนี้" if i == 1 else f"อีก {i} วัน ({DAY_TH[d]})")}
    return None


def _interpret_hb(v):
    if v is None:
        return None
    v = float(v)
    if v < 8:
        return {"emoji": "🔴", "text": f"Hb {v:.1f} — ต่ำมาก ควรปรึกษาแพทย์เรื่องยากระตุ้นเม็ดเลือดแดง", "status": "critical"}
    if v < 10:
        return {"emoji": "🟠", "text": f"Hb {v:.1f} — ยังต่ำกว่าเป้า (10–12) แพทย์อาจปรับยาให้", "status": "low"}
    if v <= 12:
        return {"emoji": "🟢", "text": f"Hb {v:.1f} — อยู่ในเป้าหมาย ✓ ดีมาก!", "status": "ok"}
    return {"emoji": "🟡", "text": f"Hb {v:.1f} — สูงกว่าเป้า แพทย์อาจลดยากระตุ้นเม็ดเลือดแดง", "status": "high"}


def _interpret_ktv(v):
    if v is None:
        return None
    v = float(v)
    if v >= 1.4:
        return {"emoji": "🟢", "text": f"Kt/V {v:.2f} — ฟอกได้ดีมาก ✓", "status": "ok"}
    if v >= 1.2:
        return {"emoji": "🟢", "text": f"Kt/V {v:.2f} — ถึงเป้าหมาย ✓", "status": "ok"}
    if v >= 1.0:
        return {"emoji": "🟡", "text": f"Kt/V {v:.2f} — ใกล้เป้าแต่ยังไม่ถึง แพทย์อาจปรับเวลาฟอก", "status": "low"}
    return {"emoji": "🔴", "text": f"Kt/V {v:.2f} — ต่ำกว่าเป้า ควรเพิ่มเวลาฟอกหรืออัตราการไหลเลือด", "status": "critical"}


def _interpret_ferritin(v):
    if v is None:
        return None
    v = float(v)
    if v < 200:
        return {"emoji": "🟡", "text": f"Ferritin {v:.0f} — ธาตุเหล็กสะสมต่ำ อาจต้องเสริมเหล็กทางเส้นเลือด", "status": "low"}
    if v <= 500:
        return {"emoji": "🟢", "text": f"Ferritin {v:.0f} — ธาตุเหล็กปกติ ✓", "status": "ok"}
    return {"emoji": "🟠", "text": f"Ferritin {v:.0f} — สูง แพทย์จะพิจารณาหยุดเสริมเหล็กชั่วคราว", "status": "high"}


def portal_data(conn, hn):
    """Full patient portal data for one HN → dict or None."""
    with conn.cursor() as cur:
        cur.execute("""SELECT id, patient_name, vascular_access, schedule_days, dry_weight,
                              esa_drug, esa_dose, default_shift, date_of_birth
                       FROM hd_patient_mapping WHERE hn = %s AND is_active = TRUE""", (hn,))
        p = cur.fetchone()
        if not p:
            return None
        pid = p[0]
        profile = {"hn": hn, "name": p[1], "va": p[2], "schedule": p[3],
                   "dry_weight": _f(p[4]), "esa_drug": p[5], "esa_dose": p[6],
                   "shift": SHIFT_TH.get(p[7], ""), "dob": p[8].isoformat() if p[8] else None}
        profile["next_dialysis"] = _next_dialysis(p[3])

        # Lab trends (last 12 months, newest first)
        labs = []
        cur.execute("""SELECT lab_date, hemoglobin, hematocrit, ferritin, ipth, kt_v
                       FROM hd_lab_results WHERE patient_id = %s AND lab_date >= CURRENT_DATE - 365
                       ORDER BY lab_date DESC, id DESC LIMIT 20""", (pid,))
        for r in cur.fetchall():
            labs.append({"date": r[0].isoformat(), "hb": _f(r[1]), "hct": _f(r[2]),
                         "ferritin": _f(r[3]), "ipth": _f(r[4]), "ktv": _f(r[5])})
        # Latest values with interpretation
        latest = {}
        for lab in labs:
            for k in ("hb", "hct", "ferritin", "ipth", "ktv"):
                if lab[k] is not None and k not in latest:
                    latest[k] = lab[k]
        interpretations = {}
        if "hb" in latest:
            interpretations["hb"] = _interpret_hb(latest["hb"])
        if "ktv" in latest:
            interpretations["ktv"] = _interpret_ktv(latest["ktv"])
        if "ferritin" in latest:
            interpretations["ferritin"] = _interpret_ferritin(latest["ferritin"])

        # BCM / Fluid
        fluid = None
        try:
            cur.execute("SAVEPOINT pp_fluid")
            cur.execute("""SELECT assessed_at, weight_kg, oh_pre_l, rel_oh_pct, lti
                           FROM hd_fluid_assessments WHERE patient_id = %s
                           ORDER BY assessed_at DESC LIMIT 1""", (pid,))
            fr = cur.fetchone()
            cur.execute("RELEASE SAVEPOINT pp_fluid")
            if fr:
                oh = float(fr[2])
                fluid = {"date": fr[0].isoformat(), "weight": _f(fr[1]), "oh": oh,
                         "rel_oh": _f(fr[3]),
                         "text": ("น้ำในร่างกายปกติ ✓" if -1 <= oh <= 1.5
                                  else ("มีน้ำเกินเล็กน้อย — รักษา DW ตามแพทย์สั่ง" if oh <= 2.5
                                        else "มีน้ำเกินมาก — ควรจำกัดน้ำและเกลือ") if oh > 0
                                  else "น้ำในร่างกายน้อย — ดื่มน้ำเพิ่มได้")}
        except Exception:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT pp_fluid")
            except Exception:
                pass

        # Last 10 sessions
        sessions = []
        cur.execute("""SELECT session_date, pre_weight, post_weight, actual_uf, hb_value,
                              started_at, ended_at, esa_given, shift, bed_number
                       FROM hd_sessions WHERE patient_id = %s AND status = 'completed'
                       ORDER BY session_date DESC, started_at DESC LIMIT 10""", (pid,))
        for r in cur.fetchall():
            duration_h = None
            if r[5] and r[6]:
                duration_h = round((r[6] - r[5]).total_seconds() / 3600, 1)
            sessions.append({"date": r[0].isoformat(), "pre_weight": _f(r[1]), "post_weight": _f(r[2]),
                             "uf": _f(r[3]), "hb": _f(r[4]), "duration": duration_h,
                             "esa": r[7], "shift": SHIFT_TH.get(r[8], ""), "bed": r[9]})

        # Health tips based on current status
        tips = []
        if profile["next_dialysis"] and profile["next_dialysis"]["in_days"] >= 2:
            tips.append({"icon": "💧", "text": "วันหยุดฟอก: จำกัดน้ำ < 1 ลิตร/วัน, ลดเกลือ, หลีกเลี่ยงผลไม้โพแทสเซียมสูง (กล้วย ส้ม มะละกอ)"})
        if latest.get("hb") and latest["hb"] < 10:
            tips.append({"icon": "🥩", "text": "Hb ต่ำ: ทานโปรตีนสูง (ไข่ เนื้อแดง ปลา) ทานยากระตุ้นเม็ดเลือดแดงตามแพทย์สั่ง"})
        if latest.get("ferritin") and latest["ferritin"] < 200:
            tips.append({"icon": "💊", "text": "เหล็กต่ำ: แพทย์อาจให้เหล็กทางเส้นเลือดในรอบฟอกถัดไป"})
        if fluid and fluid["oh"] > 2:
            tips.append({"icon": "🧂", "text": "น้ำเกิน: ลดเกลือ, จำกัดน้ำ, ชั่งน้ำหนักทุกวันเช้าหลังตื่นนอน"})
        if not tips:
            tips.append({"icon": "💪", "text": "ออกกำลังกายเบา ๆ เช่น เดิน 30 นาทีในวันที่ไม่ฟอก ช่วยให้ร่างกายแข็งแรงขึ้น"})
            tips.append({"icon": "📝", "text": "จดน้ำหนักก่อนฟอกทุกครั้ง ถ้าเพิ่มเกิน 3% ของ DW ให้ลดน้ำก่อนรอบถัดไป"})

    return {"profile": profile, "labs": labs, "latest": latest, "interpretations": interpretations,
            "fluid": fluid, "sessions": sessions, "tips": tips,
            "generated_at": (datetime.now(timezone.utc) + timedelta(hours=7)).isoformat(timespec="minutes")}
