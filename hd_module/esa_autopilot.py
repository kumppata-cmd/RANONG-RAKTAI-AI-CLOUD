"""ESA Autopilot — proposes, never prescribes (ว้าว #3).

When a new Hb arrives (lab document or session entry) the protocol the
physician configured in ⚙️ ตั้งค่า is applied to the patient's current ESA
dose and a *proposal* is written to hd_esa_proposals. The physician gets one
LINE per batch and approves/rejects each proposal from /hd/esa. Only an
approved proposal changes hd_patient_mapping.esa_dose (what the nurse sees
on the bed card). Nothing is auto-applied.

Default protocol (KDIGO-style, all overridable in settings):
    target 10–12 g/dL · hold when > 13 · step 25 % · no new proposal within
    28 days of the last decision · resume at 75 % of the held dose when Hb
    drops back under 11.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEFAULTS = {"esa_target_low": 10.0, "esa_target_high": 12.0, "esa_hold_above": 13.0,
            "esa_step_pct": 25, "esa_min_interval_days": 28, "esa_resume_below": 11.0}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hd_esa_proposals (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES hd_patient_mapping(id),
    hn VARCHAR(20),
    hb NUMERIC(4,1) NOT NULL,
    hb_date DATE,
    hb_source VARCHAR(80),
    current_drug VARCHAR(50),
    current_dose VARCHAR(50),
    action VARCHAR(20) NOT NULL,
    proposed_dose VARCHAR(50),
    rationale TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW(),
    decided_by VARCHAR(100),
    decided_at TIMESTAMP,
    note TEXT,
    UNIQUE (patient_id, hb_source)
)
"""

_DOSE_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*(iu|unit|u|mcg|µg|ug)?", re.I)


def parse_dose(text):
    """'4000 IU' → (4000.0, 'IU'); '40 mcg' → (40.0, 'mcg'); None if no number."""
    if not text:
        return None, ""
    m = _DOSE_RE.search(str(text))
    if not m:
        return None, ""
    val = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    unit = "mcg" if unit in ("mcg", "µg", "ug") else "IU"
    return val, unit


def _round_dose(val, unit):
    if unit == "mcg":
        return max(10, round(val / 10) * 10)
    return max(1000, round(val / 1000) * 1000)


def propose(hb, current_dose_text, protocol=None, held=False):
    """Pure decision. Returns None (no change) or
    {"action": increase|decrease|hold|resume|review, "proposed_dose": str|None, "rationale": str}"""
    p = dict(DEFAULTS, **(protocol or {}))
    try:
        hb = float(hb)
    except (TypeError, ValueError):
        return None
    dose, unit = parse_dose(current_dose_text)
    step = float(p["esa_step_pct"]) / 100.0
    if hb > float(p["esa_hold_above"]):
        return {"action": "hold", "proposed_dose": "งดชั่วคราว",
                "rationale": f"Hb {hb:.1f} > {p['esa_hold_above']} — งด ESA จนกว่า Hb < {p['esa_resume_below']}"}
    if held:
        if hb < float(p["esa_resume_below"]) and dose:
            new = _round_dose(dose * 0.75, unit)
            return {"action": "resume", "proposed_dose": f"{int(new)} {unit}",
                    "rationale": f"Hb {hb:.1f} < {p['esa_resume_below']} หลังงด — กลับมาให้ที่ 75% ของขนาดเดิม"}
        return None
    if hb > float(p["esa_target_high"]):
        if not dose:
            return {"action": "review", "proposed_dose": None, "rationale": f"Hb {hb:.1f} เกินเป้า แต่ไม่ทราบขนาด ESA ปัจจุบัน"}
        new = _round_dose(dose * (1 - step), unit)
        return {"action": "decrease", "proposed_dose": f"{int(new)} {unit}",
                "rationale": f"Hb {hb:.1f} > {p['esa_target_high']} — ลด {int(step * 100)}%"}
    if hb < float(p["esa_target_low"]):
        if not dose:
            return {"action": "review", "proposed_dose": None, "rationale": f"Hb {hb:.1f} ต่ำกว่าเป้า — ยังไม่มีขนาด ESA ในระบบ พิจารณาเริ่ม/ระบุขนาด"}
        new = _round_dose(dose * (1 + step), unit)
        return {"action": "increase", "proposed_dose": f"{int(new)} {unit}",
                "rationale": f"Hb {hb:.1f} < {p['esa_target_low']} — เพิ่ม {int(step * 100)}%"}
    return None


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def generate(conn, protocol=None, lookback_days=60):
    """Scan patients with a recent Hb and create pending proposals.
    Returns list of created proposals (dicts)."""
    p = dict(DEFAULTS, **(protocol or {}))
    created = []
    since = (_thai_today() - timedelta(days=lookback_days)).isoformat()
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        # newest Hb per patient: lab documents and session entries
        cur.execute("""
            WITH src AS (
                SELECT patient_id, hb, lab_date AS d, 'lab' AS src
                FROM hd_labs WHERE hb IS NOT NULL AND lab_date >= %s
                UNION ALL
                SELECT patient_id, hb_value, session_date, 'session:' || id FROM hd_sessions
                WHERE hb_value IS NOT NULL AND session_date >= %s
            )
            SELECT DISTINCT ON (s.patient_id) s.patient_id, p.hn, p.patient_name, s.hb, s.d, s.src,
                   p.esa_drug, p.esa_dose
            FROM src s JOIN hd_patient_mapping p ON p.id = s.patient_id
            WHERE p.is_active = TRUE
            ORDER BY s.patient_id, s.d DESC, s.src DESC
        """, (since, since))
        rows = cur.fetchall()
        for pid, hn, name, hb, d, src, drug, dose in rows:
            cur.execute("""SELECT status, action, decided_at, created_at FROM hd_esa_proposals
                           WHERE patient_id = %s ORDER BY id DESC LIMIT 1""", (pid,))
            last = cur.fetchone()
            if last and last[0] == "pending":
                continue
            held = bool(last and last[0] == "approved" and last[1] == "hold")
            if last and last[2] and (_thai_today() - last[2].date()).days < int(p["esa_min_interval_days"]) and not held:
                continue
            cur.execute("SELECT 1 FROM hd_esa_proposals WHERE patient_id = %s AND hb_source = %s", (pid, src))
            if cur.fetchone():
                continue
            prop = propose(hb, dose, p, held=held)
            if not prop:
                continue
            # ว้าว รอบ 2 #4: Hb ต่ำ + ขาดเหล็ก → เสนอเหล็ก IV ก่อนเพิ่ม ESA (KDOQI: ferritin <200 / TSAT <20%)
            if prop["action"] in ("increase", "review") and hb < float(p["esa_target_low"]):
                try:
                    from .anemia import iron_status_for
                    st, fer, tsat = iron_status_for(cur, pid)
                    if st == "deficient":
                        fer_s = f"{fer:g}" if fer is not None else "?"
                        tsat_s = f"{tsat:g}%" if tsat is not None else "?"
                        prop = {"action": "iron", "proposed_dose": "เหล็ก IV ตาม protocol",
                                "rationale": f"Hb {float(hb):.1f} < {p['esa_target_low']} แต่ขาดเหล็ก (ferritin {fer_s} / TSAT {tsat_s}) — เติมเหล็กก่อน ยังไม่เพิ่ม ESA"}
                except Exception as e:
                    logger.info("iron check skipped: %s", e)
            cur.execute("""INSERT INTO hd_esa_proposals (patient_id, hn, hb, hb_date, hb_source, current_drug,
                           current_dose, action, proposed_dose, rationale)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (pid, hn, hb, d, src, drug, dose, prop["action"], prop["proposed_dose"], prop["rationale"]))
            created.append({"id": cur.fetchone()[0], "hn": hn, "name": name, "hb": float(hb), "action": prop["action"],
                            "current_dose": dose, "proposed_dose": prop["proposed_dose"], "drug": drug})
    conn.commit()
    return created


ACTION_TH = {"increase": "เพิ่ม", "decrease": "ลด", "hold": "งด", "resume": "กลับมาให้", "review": "ทบทวน", "iron": "ให้เหล็กก่อน"}


def line_summary(created, base_url):
    if not created:
        return ""
    L = [f"💊 ESA Autopilot — {len(created)} คน ถึงเวลาปรับ (รออนุมัติ)", "━━━━━━━━━━━━"]
    for c in created[:8]:
        L.append(f"• {c['name']} (HN {c['hn']}) Hb {c['hb']:.1f} → {ACTION_TH.get(c['action'], c['action'])}"
                 f"{(' ' + c['proposed_dose']) if c['proposed_dose'] else ''}"
                 f"{(' (เดิม ' + c['current_dose'] + ')') if c['current_dose'] else ''}")
    if len(created) > 8:
        L.append(f"… อีก {len(created) - 8} คน")
    L.append("━━━━━━━━━━━━")
    L.append(f"✍️ อนุมัติ/ไม่ปรับ ทีละคน: {base_url}/hd/esa")
    return "\n".join(L)
