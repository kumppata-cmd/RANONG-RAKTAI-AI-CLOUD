"""Dry Weight Autopilot + UF Planner (ว้าว รอบ 2 — #1).

Two pure, explainable pieces — nothing is applied automatically:

UF Planner (before a session starts)
    Given weight on arrival, dry weight, planned hours → UF needed, UF rate
    (mL/kg/h).  > 13 is the widely used ceiling (KDIGO/EuroBCM); 10–13 is
    "caution".  The planner returns the options a physician usually weighs:
    stretch the time, or limit UF and leave a residual gain.

Dry Weight review (every N completed sessions)
    Pattern over the last 10 sessions:
        post-weight consistently BELOW dry weight + IDH/cramps  → DW too low  → propose +0.5
        post-weight consistently ABOVE dry weight (UF not met) + high post-HD SBP,
        edema notes, large IDWG                                → DW too high → propose −0.5
    A proposal goes to hd_dw_proposals; the physician approves/rejects on
    /hd/esa (Autopilot page). Only approval changes hd_patient_mapping.dry_weight.
"""
import logging
from datetime import datetime, timedelta, timezone
from statistics import median

logger = logging.getLogger(__name__)

DEFAULTS = {"uf_rate_max": 13.0, "uf_rate_caution": 10.0, "dw_review_every": 10,
            "dw_step_kg": 0.5, "dw_min_interval_days": 21}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hd_dw_proposals (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES hd_patient_mapping(id),
    hn VARCHAR(20),
    current_dw NUMERIC(5,1),
    proposed_dw NUMERIC(5,1),
    direction VARCHAR(10) NOT NULL,
    rationale TEXT,
    evidence JSONB DEFAULT '{}'::jsonb,
    sessions_used INTEGER,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW(),
    decided_by VARCHAR(100),
    decided_at TIMESTAMP,
    note TEXT
)
"""


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ───────────────────────── UF Planner ─────────────────────────

def plan_uf(pre_weight, dry_weight, hours=4.0, limits=None, extra_intake_ml=0):
    """→ {"uf_ml", "rate", "level": ok|caution|high, "advice": [...], "options": [...]}"""
    lim = dict(DEFAULTS, **(limits or {}))
    pw, dw, h = _f(pre_weight), _f(dry_weight), _f(hours) or 4.0
    if not pw or not dw:
        return {"uf_ml": None, "rate": None, "level": "unknown", "advice": ["ยังไม่มีน้ำหนักมาฟอก/น้ำหนักแห้ง"], "options": []}
    gain = pw - dw
    uf_ml = max(0.0, gain * 1000 + (extra_intake_ml or 0))
    rate = uf_ml / dw / h
    mx, ca = float(lim["uf_rate_max"]), float(lim["uf_rate_caution"])
    level = "high" if rate > mx else ("caution" if rate > ca else "ok")
    advice, options = [], []
    idwg_pct = gain / dw * 100
    if level == "ok":
        advice.append(f"UF {uf_ml/1000:.1f} L ใน {h:g} ชม. = {rate:.1f} mL/kg/h — อยู่ในเกณฑ์ (≤{ca:g})")
    else:
        advice.append(f"UF {uf_ml/1000:.1f} L ใน {h:g} ชม. = {rate:.1f} mL/kg/h — {'เกินเพดาน' if level == 'high' else 'ระวัง'} (เพดาน {mx:g}, เสี่ยง IDH/ตะคริว)")
        # option A: stretch time to reach the ceiling
        need_h = uf_ml / dw / mx
        need_h = (int(need_h * 4) + (1 if need_h * 4 % 1 else 0)) / 4  # round up to 15 min
        if need_h > h:
            options.append({"type": "time", "hours": need_h,
                            "text": f"ยืดเวลาเป็น {need_h:g} ชม. → {uf_ml/dw/need_h:.1f} mL/kg/h"})
        # option B: cap UF at ceiling in the planned time
        cap_ml = mx * dw * h
        rest = (uf_ml - cap_ml) / 1000
        if rest > 0.05:
            options.append({"type": "cap", "uf_ml": round(cap_ml / 100) * 100,
                            "text": f"ดึงไม่เกิน {cap_ml/1000:.1f} L ในรอบนี้ (เหลือค้าง {rest:.1f} kg เก็บรอบหน้า)"})
        if idwg_pct > 4:
            advice.append(f"น้ำหนักขึ้นระหว่างรอบ {gain:.1f} kg ({idwg_pct:.1f}% ของ DW) — ทบทวนการจำกัดน้ำ/เกลือ")
    return {"uf_ml": round(uf_ml), "rate": round(rate, 1), "level": level, "gain_kg": round(gain, 2),
            "idwg_pct": round(idwg_pct, 1), "hours": h, "advice": advice, "options": options}


# ───────────────────────── Dry-weight review ─────────────────────────

def review_dry_weight(sessions, dry_weight, limits=None):
    """sessions: oldest→newest list of dicts
        {post_weight, pre_weight, actual_uf, uf_goal, idh (bool), cramp (bool), post_sbp, min_sbp, edema (bool)}
    → None (keep) or {"direction": up|down, "proposed_dw", "rationale", "evidence"}"""
    lim = dict(DEFAULTS, **(limits or {}))
    dw = _f(dry_weight)
    ss = [s for s in sessions if _f(s.get("post_weight"))][-10:]
    if not dw or len(ss) < 5:
        return None
    step = float(lim["dw_step_kg"])
    posts = [_f(s["post_weight"]) for s in ss]
    below = sum(1 for p in posts if p <= dw - 0.3)
    above = sum(1 for p in posts if p >= dw + 0.3)
    idh = sum(1 for s in ss if s.get("idh"))
    cramp = sum(1 for s in ss if s.get("cramp"))
    low_post = sum(1 for s in ss if (_f(s.get("post_sbp")) or 999) < 100)
    high_post = sum(1 for s in ss if (_f(s.get("post_sbp")) or 0) >= 160)
    edema = sum(1 for s in ss if s.get("edema"))
    uf_short = sum(1 for s in ss if _f(s.get("uf_goal")) and _f(s.get("actual_uf")) is not None
                   and _f(s["actual_uf"]) < 0.85 * _f(s["uf_goal"]))
    n = len(ss)
    ev = {"n": n, "median_post": round(median(posts), 1), "below_dw": below, "above_dw": above, "idh": idh,
          "cramp": cramp, "post_sbp_low": low_post, "post_sbp_high": high_post, "edema": edema, "uf_short": uf_short}
    # DW too LOW: reaching/below DW but paying for it with IDH/cramps/low post-BP
    if (idh + cramp) >= max(3, n // 3) and (below + sum(1 for p in posts if abs(p - dw) < 0.3)) >= n * 0.6:
        return {"direction": "up", "proposed_dw": round(dw + step, 1), "evidence": ev,
                "rationale": f"ถึง/ต่ำกว่า DW ใน {below + sum(1 for p in posts if abs(p - dw) < 0.3)}/{n} รอบ แต่มี IDH {idh} ครั้ง ตะคริว {cramp} ครั้ง"
                             f"{', SBP หลังฟอก <100 ' + str(low_post) + ' ครั้ง' if low_post else ''} → DW อาจต่ำเกินไป"}
    # DW too HIGH: never getting down to DW, fluid signs, high post-BP, no IDH
    if above >= n * 0.6 and idh == 0 and (high_post >= 3 or edema >= 2 or uf_short >= 3):
        return {"direction": "down", "proposed_dw": round(dw - step, 1), "evidence": ev,
                "rationale": f"หลังฟอกสูงกว่า DW ใน {above}/{n} รอบ โดยไม่มี IDH"
                             f"{', SBP หลังฟอก ≥160 ' + str(high_post) + ' ครั้ง' if high_post else ''}"
                             f"{', บวม ' + str(edema) + ' ครั้ง' if edema else ''}"
                             f"{', UF ไม่ถึงเป้า ' + str(uf_short) + ' ครั้ง' if uf_short else ''} → DW อาจสูงเกินไป"}
    # stable below DW without symptoms → the real DW may be lower (gentle)
    if below >= n * 0.8 and idh == 0 and cramp == 0 and low_post == 0 and median(posts) <= dw - 0.5:
        return {"direction": "down", "proposed_dw": round(median(posts), 1), "evidence": ev,
                "rationale": f"หลังฟอกต่ำกว่า DW ใน {below}/{n} รอบ (มัธยฐาน {median(posts):.1f}) โดยไม่มีอาการ → ปรับ DW ตามน้ำหนักจริง"}
    return None


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def collect_sessions(cur, patient_id, n=10):
    """Last n completed sessions with the signals review_dry_weight needs (read-only)."""
    cur.execute("""
        SELECT s.id, s.session_date, s.pre_weight, s.post_weight, s.uf_goal, s.actual_uf, s.notes, s.pre_hd_note,
               (SELECT COUNT(*) FROM hd_clinical_alerts a WHERE a.session_id = s.id AND a.alert_type IN ('idh','hypotension')),
               (SELECT sbp FROM hd_vital_signs v WHERE v.session_id = s.id AND v.sbp IS NOT NULL ORDER BY v.recorded_at DESC LIMIT 1),
               (SELECT MIN(sbp) FROM hd_vital_signs v WHERE v.session_id = s.id),
               (SELECT string_agg(COALESCE(f.remark,''), ' ') FROM hd_flowsheet f WHERE f.session_id = s.id),
               (SELECT string_agg(COALESCE(v.symptoms,''), ' ') FROM hd_vital_signs v WHERE v.session_id = s.id)
        FROM hd_sessions s WHERE s.patient_id = %s AND s.status = 'completed'
        ORDER BY s.session_date DESC, s.id DESC LIMIT %s""", (patient_id, n))
    out = []
    for r in cur.fetchall():
        txt = " ".join(str(x or "") for x in (r[6], r[7], r[11], r[12])).lower()
        out.append({"session_id": r[0], "date": r[1].isoformat(), "pre_weight": _f(r[2]), "post_weight": _f(r[3]),
                    "uf_goal": _f(r[4]), "actual_uf": _f(r[5]), "idh": (r[8] or 0) > 0 or (r[10] or 999) < 90,
                    "cramp": any(w in txt for w in ("ตะคริว", "cramp")),
                    "edema": any(w in txt for w in ("บวม", "edema", "เหนื่อย", "dyspnea", "orthopnea")),
                    "post_sbp": r[9], "min_sbp": r[10]})
    out.reverse()
    return out


def generate(conn, limits=None):
    """Scan active patients; create DW proposals where the pattern is clear. Returns created list."""
    lim = dict(DEFAULTS, **(limits or {}))
    created = []
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        cur.execute("""SELECT p.id, p.hn, p.patient_name, p.dry_weight,
                              (SELECT COUNT(*) FROM hd_sessions s WHERE s.patient_id = p.id AND s.status = 'completed'
                                 AND s.session_date >= CURRENT_DATE - 60)
                       FROM hd_patient_mapping p WHERE p.is_active = TRUE AND p.dry_weight IS NOT NULL""")
        pats = cur.fetchall()
        for pid, hn, name, dw, recent in pats:
            if recent < 5:
                continue
            cur.execute("""SELECT status, decided_at, created_at FROM hd_dw_proposals WHERE patient_id = %s
                           ORDER BY id DESC LIMIT 1""", (pid,))
            last = cur.fetchone()
            if last and last[0] == "pending":
                continue
            if last and (last[1] or last[2]) and (_thai_today() - (last[1] or last[2]).date()).days < int(lim["dw_min_interval_days"]):
                continue
            sess = collect_sessions(cur, pid, 10)
            prop = review_dry_weight(sess, dw, lim)
            if not prop:
                continue
            import json
            cur.execute("""INSERT INTO hd_dw_proposals (patient_id, hn, current_dw, proposed_dw, direction, rationale, evidence, sessions_used)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (pid, hn, dw, prop["proposed_dw"], prop["direction"], prop["rationale"],
                         json.dumps(prop["evidence"]), len(sess)))
            created.append({"id": cur.fetchone()[0], "hn": hn, "name": name, "current_dw": float(dw),
                            "proposed_dw": prop["proposed_dw"], "direction": prop["direction"], "rationale": prop["rationale"]})
    conn.commit()
    return created


def line_summary(created, base_url):
    if not created:
        return ""
    L = [f"🎯 Dry Weight Autopilot — {len(created)} คน เสนอปรับ DW (รออนุมัติ)", "━━━━━━━━━━━━"]
    for c in created[:8]:
        arrow = "⬆️" if c["direction"] == "up" else "⬇️"
        L.append(f"• {c['name']} (HN {c['hn']}) DW {c['current_dw']:.1f} → {arrow} {c['proposed_dw']:.1f} kg")
    if len(created) > 8:
        L.append(f"… อีก {len(created) - 8} คน")
    L.append("━━━━━━━━━━━━")
    L.append(f"✍️ อนุมัติ/ไม่ปรับ: {base_url}/hd/esa")
    return "\n".join(L)
