"""IDH Predictor — intradialytic hypotension risk *before* it happens (ว้าว #1).

Transparent, rule-based score (0–100) from data the system already records.
Every point is explained ("UF 14.2 mL/kg/h", "SBP ตกต่อเนื่อง") so the nurse
and physician can see *why*. Output is advisory: suggests closer BP checks,
never changes UF or treatment.

Weights (sum capped at 100):
    UF rate   ≥ 13 mL/kg/h       +25    10–13 +12
    IDWG      > 4 % DW           +15    > 3 % +8
    SBP trend fall ≥ 10/reading  +20    ≥ 5 +10   (over the last 2 intervals)
    current SBP < 100            +15    < 110 +10
    IDH history ≥ 3 of last 10   +15    ≥ 1 +8
    Hb < 10 g/dL                 +5
    Albumin < 3.5 g/dL           +5
    Age ≥ 70                     +5
    Pre-HD SBP ≥ 160 (large drop potential) +4
Levels: ≥ 60 high · 35–59 moderate · < 35 low
"""
from datetime import date, datetime, timedelta, timezone


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def score(features):
    """features: dict with any of
        uf_goal (mL), dry_weight (kg), duration_hours, pre_weight (kg),
        sbp_series (oldest→newest ints), idh_history (count in last 10),
        hb, albumin, age, pre_sbp
    Returns {"score", "level", "factors": [{"text", "points"}], "advice"}"""
    pts = 0
    factors = []
    uf = _f(features.get("uf_goal"))
    dw = _f(features.get("dry_weight"))
    hours = _f(features.get("duration_hours")) or 4.0
    pre_w = _f(features.get("pre_weight"))

    if uf and dw and hours:
        rate = uf / dw / hours
        if rate >= 13:
            pts += 25; factors.append({"text": f"UF {rate:.1f} mL/kg/h (≥13)", "points": 25})
        elif rate >= 10:
            pts += 12; factors.append({"text": f"UF {rate:.1f} mL/kg/h (≥10)", "points": 12})
    if pre_w and dw and pre_w > dw:
        idwg_pct = (pre_w - dw) / dw * 100
        if idwg_pct > 4:
            pts += 15; factors.append({"text": f"IDWG {pre_w - dw:.1f} kg ({idwg_pct:.1f}% ของ DW)", "points": 15})
        elif idwg_pct > 3:
            pts += 8; factors.append({"text": f"IDWG {pre_w - dw:.1f} kg ({idwg_pct:.1f}% ของ DW)", "points": 8})

    s = [int(x) for x in (features.get("sbp_series") or []) if x is not None]
    if len(s) >= 3:
        d1, d2 = s[-2] - s[-1], s[-3] - s[-2]
        if d1 >= 10 and d2 >= 10:
            pts += 20; factors.append({"text": f"SBP ตกต่อเนื่อง {s[-3]}→{s[-2]}→{s[-1]}", "points": 20})
        elif d1 >= 5 and d2 >= 5:
            pts += 10; factors.append({"text": f"SBP ลดลงเรื่อย ๆ {s[-3]}→{s[-2]}→{s[-1]}", "points": 10})
    elif len(s) == 2 and s[0] - s[1] >= 15:
        pts += 10; factors.append({"text": f"SBP ตก {s[0]}→{s[1]}", "points": 10})
    if s:
        cur = s[-1]
        if cur < 100:
            pts += 15; factors.append({"text": f"SBP ปัจจุบัน {cur} (<100)", "points": 15})
        elif cur < 110:
            pts += 10; factors.append({"text": f"SBP ปัจจุบัน {cur} (<110)", "points": 10})

    hist = int(features.get("idh_history") or 0)
    if hist >= 3:
        pts += 15; factors.append({"text": f"มี IDH {hist} ครั้งใน 10 รอบล่าสุด", "points": 15})
    elif hist >= 1:
        pts += 8; factors.append({"text": f"มี IDH {hist} ครั้งใน 10 รอบล่าสุด", "points": 8})

    hb = _f(features.get("hb"))
    if hb is not None and hb < 10:
        pts += 5; factors.append({"text": f"Hb {hb:.1f}", "points": 5})
    alb = _f(features.get("albumin"))
    if alb is not None and alb < 3.5:
        pts += 5; factors.append({"text": f"Albumin {alb:.1f}", "points": 5})
    age = _f(features.get("age"))
    if age is not None and age >= 70:
        pts += 5; factors.append({"text": f"อายุ {int(age)} ปี", "points": 5})
    pre_sbp = _f(features.get("pre_sbp"))
    if pre_sbp is not None and pre_sbp >= 160:
        pts += 4; factors.append({"text": f"SBP ก่อนฟอก {int(pre_sbp)} (≥160)", "points": 4})

    pts = min(100, pts)
    level = "high" if pts >= 60 else ("moderate" if pts >= 35 else "low")
    # Personalized recommendations based on active risk factors
    recs = []
    factor_codes = {f["text"].split()[0] for f in factors}
    if any("UF" in f["text"] and "mL/kg/h" in f["text"] for f in factors):
        recs.append({"icon": "⏱", "text": "พิจารณายืดเวลาฟอกเพิ่ม 30 นาที เพื่อลด UF rate", "priority": "high"})
        recs.append({"icon": "🌡️", "text": "ใช้น้ำยาอุณหภูมิ 35.5°C (cool dialysate) ช่วยคงความดัน", "priority": "medium"})
    if any("IDWG" in f["text"] for f in factors):
        recs.append({"icon": "💧", "text": "แนะนำผู้ป่วยจำกัดน้ำ < 1 ลิตร/วัน ระหว่างวันหยุดฟอก", "priority": "medium"})
    if any("ตกต่อเนื่อง" in f["text"] or "ลดลง" in f["text"] for f in factors):
        recs.append({"icon": "📊", "text": "พิจารณา sodium profiling หรือลด UF rate ใน 2 ชม.สุดท้าย", "priority": "high"})
    if any("SBP ปัจจุบัน" in f["text"] for f in factors):
        recs.append({"icon": "🔔", "text": "เตรียม NSS 100–200 mL · จัดท่า Trendelenburg ถ้า SBP < 90", "priority": "high"})
    if any("IDH" in f["text"] and "ครั้ง" in f["text"] for f in factors):
        recs.append({"icon": "📋", "text": "ผู้ป่วยมีประวัติ IDH ซ้ำ — ทบทวน DW กับผล BCM อีกครั้ง", "priority": "medium"})
    if any("SBP ก่อนฟอก" in f["text"] for f in factors):
        recs.append({"icon": "💊", "text": "พิจารณางดยาลดความดันก่อนฟอก (ถ้ายังไม่ได้งด)", "priority": "medium"})
    if not recs and level != "low":
        recs.append({"icon": "👁️", "text": "เฝ้าระวังทั่วไป — วัด BP ตามกำหนด", "priority": "low"})
    advice = {"high": "⚠️ วัด BP ทุก 15 นาที · เตรียมแจ้งแพทย์หาก SBP ลดอีก",
              "moderate": "วัด BP ทุก 30 นาที · สังเกตอาการหน้ามืด/คลื่นไส้",
              "low": ""}[level]
    return {"score": pts, "level": level, "factors": factors, "advice": advice, "recommendations": recs}


def features_for(cur, session_id):
    """Collect predictor inputs for one session from the DB (read-only)."""
    cur.execute("""
        SELECT s.uf_goal, s.pre_weight,
               COALESCE((SELECT r.duration_hours FROM hd_prescriptions r
                         WHERE r.patient_id = s.patient_id AND r.is_active = TRUE ORDER BY r.id DESC LIMIT 1), 4.0),
               p.dry_weight, p.date_of_birth, s.patient_id
        FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id WHERE s.id = %s""", (session_id,))
    r = cur.fetchone()
    if not r:
        return None
    f = {"uf_goal": r[0], "pre_weight": r[1], "duration_hours": r[2], "dry_weight": r[3]}
    if r[4]:
        today = (datetime.now(timezone.utc) + timedelta(hours=7)).date()
        f["age"] = today.year - r[4].year - ((today.month, today.day) < (r[4].month, r[4].day))
    pid = r[5]
    cur.execute("""SELECT sbp FROM (SELECT sbp, recorded_at FROM hd_vital_signs
                   WHERE session_id = %s AND sbp IS NOT NULL ORDER BY recorded_at DESC LIMIT 4) t
                   ORDER BY recorded_at ASC""", (session_id,))
    f["sbp_series"] = [x[0] for x in cur.fetchall()]
    cur.execute("""SELECT sbp FROM hd_vital_signs WHERE session_id = %s AND sbp IS NOT NULL
                   ORDER BY recorded_at ASC LIMIT 1""", (session_id,))
    x = cur.fetchone()
    f["pre_sbp"] = x[0] if x else None
    cur.execute("""
        SELECT COUNT(DISTINCT a.session_id) FROM hd_clinical_alerts a
        WHERE a.alert_type IN ('idh', 'hypotension') AND a.session_id IN (
            SELECT id FROM hd_sessions WHERE patient_id = %s AND id <> %s AND status = 'completed'
            ORDER BY session_date DESC, id DESC LIMIT 10)""", (pid, session_id))
    f["idh_history"] = cur.fetchone()[0]
    try:
        cur.execute("""SELECT hb, albumin FROM hd_labs WHERE patient_id = %s
                       ORDER BY lab_date DESC NULLS LAST LIMIT 1""", (pid,))
        lab = cur.fetchone()
        if lab:
            f["hb"], f["albumin"] = lab[0], lab[1]
    except Exception:
        pass
    return f
