"""🩸 Kidney Twin — เตือนความดันตกระหว่างฟอก (IDH) ล่วงหน้า แบบ event-driven (ว้าว A, 27 พ.ย. 2569)

ต่อยอด idh_predictor (คะแนน 0–100 โปร่งใส) ให้ "มีชีวิต" ระหว่างรอบ:
- คำนวณใหม่ทุกครั้งที่พยาบาลบันทึก vitals / flowsheet / UF / ถ่ายจอเครื่อง (hook หลัง request สำเร็จ)
- รวมข้อมูลสดที่ idh_predictor เดิมไม่ได้ใช้: SBP จาก flowsheet, UF ที่ดึงแล้วจริง, อัตรา UF จริงของเครื่อง,
  เวลาที่ผ่านไปในรอบ, HR ขึ้น → ปัจจัยเพิ่ม (อธิบายได้ทุกคะแนน)
- ข้ามเป็นระดับ "สูง" → บันทึก clinical alert + push กลุ่มพยาบาล **1 ครั้งต่อรอบ** (ซ้ำได้เมื่อคะแนนพุ่ง ≥15 หลัง 45 นาที)
- จบรอบ → บันทึกผลจริง (เกิด IDH หรือไม่) เพื่อวัด sensitivity / false alarm ของตัวเอง

คำเตือนเป็น "ข้อเสนอ" ให้วัด BP ถี่ขึ้น/พิจารณาลด UF — ไม่สั่งการ ไม่แก้ค่าใด ๆ
สวิตช์ ⚙️ `idh_twin` default off · ช่องทาง `idh_twin_channel` group|card · เกณฑ์ `idh_twin_high` (default 60)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SETTING_KEY = "idh_twin"
DEFAULTS = {"idh_twin_high": 60.0, "idh_twin_realert_min": 45.0, "idh_twin_realert_rise": 15.0}
IDH_SBP = 90          # นิยามเหตุการณ์ IDH ที่ใช้ทั่วระบบ (uf-coach / worksheet)
IDH_DROP = 30         # หรือ SBP ตก ≥30 จากค่าแรกของรอบ

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hd_idh_twin_state (
    session_id INTEGER PRIMARY KEY,
    patient_id INTEGER,
    last_score INTEGER,
    last_level VARCHAR(10),
    last_factors TEXT,
    last_trigger VARCHAR(30),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    alerted_at TIMESTAMPTZ,
    alert_score INTEGER,
    alert_count INTEGER DEFAULT 0,
    peak_score INTEGER DEFAULT 0,
    outcome VARCHAR(10),
    outcome_detail TEXT,
    outcome_at TIMESTAMPTZ
);
"""


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def is_enabled(get_setting=None) -> bool:
    try:
        if get_setting is None:
            from .blueprint import get_setting_value as get_setting
        return str(get_setting(SETTING_KEY, "off") or "off").strip().lower() == "on"
    except Exception:
        return False


def config(get_setting=None) -> dict:
    out = dict(DEFAULTS)
    out["channel"] = "group"
    if get_setting is None:
        try:
            from .blueprint import get_setting_value as get_setting
        except Exception:
            return out
    for k, dv in DEFAULTS.items():
        try:
            v = get_setting(k, "")
            out[k] = float(v) if v not in ("", None) else dv
        except Exception:
            out[k] = dv
    try:
        ch = str(get_setting("idh_twin_channel", "group") or "group").strip().lower()
        out["channel"] = ch if ch in ("group", "card") else "group"
    except Exception:
        pass
    return out


# ───────────────────────── live features ─────────────────────────

def live_features(cur, session_id) -> dict | None:
    """idh_predictor.features_for + ข้อมูลสดจาก flowsheet/UF/เวลาในรอบ"""
    from .idh_predictor import features_for
    f = features_for(cur, session_id)
    if not f:
        return None
    # SBP/HR ทุกแหล่ง (vital_signs + flowsheet) เรียงเวลา
    series = []
    try:
        cur.execute("""SELECT recorded_at, sbp, heart_rate FROM hd_vital_signs WHERE session_id = %s AND sbp IS NOT NULL
                       UNION ALL
                       SELECT COALESCE(recorded_at, NOW()), sbp, pr FROM hd_flowsheet WHERE session_id = %s AND sbp IS NOT NULL
                       ORDER BY 1 ASC""", (session_id, session_id))
        series = [(r[0], int(r[1]), (int(r[2]) if r[2] is not None else None)) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        logger.debug("idh twin series: %s", e)
    if series:
        sb = [s for _, s, _ in series]
        f["sbp_series"] = sb[-4:]
        f["pre_sbp"] = sb[0]
        f["min_sbp"] = min(sb)
        hrs = [h for _, _, h in series if h]
        if len(hrs) >= 2:
            f["hr_first"], f["hr_last"] = hrs[0], hrs[-1]
    # UF สด: ล่าสุดจาก flowsheet (ufr/total_uf) หรือ hd_uf_records
    try:
        cur.execute("""SELECT uf_removed, uf_rate, recorded_at FROM (
                         SELECT total_uf AS uf_removed, ufr AS uf_rate, COALESCE(recorded_at, NOW()) AS recorded_at
                         FROM hd_flowsheet WHERE session_id = %s AND (total_uf IS NOT NULL OR ufr IS NOT NULL)
                         UNION ALL
                         SELECT uf_removed, uf_rate, recorded_at FROM hd_uf_records WHERE session_id = %s) t
                       ORDER BY recorded_at DESC LIMIT 1""", (session_id, session_id))
        r = cur.fetchone()
        if r:
            f["uf_removed_now"], f["uf_rate_now"] = _f(r[0]), _f(r[1])
    except Exception as e:  # noqa: BLE001
        logger.debug("idh twin uf: %s", e)
    try:
        cur.execute("SELECT started_at FROM hd_sessions WHERE id = %s", (session_id,))
        r = cur.fetchone()
        if r and r[0]:
            started = r[0] if r[0].tzinfo else r[0].replace(tzinfo=timezone.utc)
            f["elapsed_min"] = max(0.0, (datetime.now(timezone.utc) - started).total_seconds() / 60.0)
    except Exception:
        pass
    return f


def score_live(f: dict) -> dict:
    """คะแนนพื้นฐานจาก idh_predictor + ปัจจัยสดเพิ่ม (อธิบายได้ทุกข้อ) · เพดาน 100"""
    from .idh_predictor import score as base_score
    res = base_score(f)
    pts, factors = res["score"], list(res["factors"])
    dw = _f(f.get("dry_weight"))
    rate_now = _f(f.get("uf_rate_now"))
    if rate_now and dw and not any("mL/kg/h" in x["text"] and "ตอนนี้" in x["text"] for x in factors):
        r_kg = rate_now / dw
        if r_kg >= 13 and not any("UF" in x["text"] and "(≥13)" in x["text"] for x in factors):
            pts += 15; factors.append({"text": f"UF rate ตอนนี้ {rate_now:.0f} mL/h = {r_kg:.1f} mL/kg/h (≥13)", "points": 15})
        elif r_kg >= 13:
            pts += 5; factors.append({"text": f"UF rate ตอนนี้ {rate_now:.0f} mL/h = {r_kg:.1f} mL/kg/h", "points": 5})
    goal, removed, hours, elapsed = _f(f.get("uf_goal")), _f(f.get("uf_removed_now")), _f(f.get("duration_hours")) or 4.0, _f(f.get("elapsed_min"))
    if goal and removed and elapsed and hours:
        frac_uf, frac_t = removed / goal, elapsed / (hours * 60.0)
        if 0.2 < frac_t < 0.9 and frac_uf >= frac_t + 0.25:
            pts += 10; factors.append({"text": f"ดึงน้ำเร็วกว่าเวลา: UF {frac_uf*100:.0f}% ที่เวลา {frac_t*100:.0f}% ของรอบ", "points": 10})
    if elapsed and hours and elapsed >= hours * 60.0 * 0.6:
        pts += 5; factors.append({"text": f"ช่วงท้ายรอบ ({elapsed/60:.1f} ชม.) — IDH พบบ่อยช่วงนี้", "points": 5})
    hr0, hr1 = _f(f.get("hr_first")), _f(f.get("hr_last"))
    if hr0 and hr1 and hr1 - hr0 >= 20:
        pts += 8; factors.append({"text": f"HR ขึ้น {int(hr0)}→{int(hr1)} (≥20) — สัญญาณชดเชย", "points": 8})
    pts = min(100, int(pts))
    level = "high" if pts >= 60 else ("moderate" if pts >= 35 else "low")
    res.update({"score": pts, "level": level, "factors": factors})
    if level == "high" and not res.get("advice"):
        res["advice"] = "⚠️ วัด BP ทุก 15 นาที · เตรียมแจ้งแพทย์หาก SBP ลดอีก"
    return res


# ───────────────────────── state / alert decision ─────────────────────────

def should_alert(state: dict | None, score: int, cfg: dict, now: datetime) -> tuple[bool, str]:
    """→ (alert?, reason) · ครั้งแรกเมื่อ ≥ idh_twin_high · ซ้ำเมื่อผ่าน realert_min และคะแนนขึ้น ≥ realert_rise"""
    if score < cfg["idh_twin_high"]:
        return False, "below"
    if not state or not state.get("alerted_at"):
        return True, "first"
    mins = (now - state["alerted_at"]).total_seconds() / 60.0
    if mins >= cfg["idh_twin_realert_min"] and score - int(state.get("alert_score") or 0) >= cfg["idh_twin_realert_rise"]:
        return True, "re-alert"
    return False, "already"


def _load_state(cur, session_id):
    cur.execute("""SELECT session_id, patient_id, last_score, last_level, alerted_at, alert_score, alert_count, peak_score, outcome
                   FROM hd_idh_twin_state WHERE session_id = %s""", (session_id,))
    r = cur.fetchone()
    if not r:
        return None
    return {"session_id": r[0], "patient_id": r[1], "last_score": r[2], "last_level": r[3], "alerted_at": r[4],
            "alert_score": r[5], "alert_count": r[6] or 0, "peak_score": r[7] or 0, "outcome": r[8]}


def _save_state(cur, session_id, patient_id, res, trigger, alerted: bool):
    factors = "; ".join(x["text"] for x in res["factors"])[:1500]
    cur.execute("""INSERT INTO hd_idh_twin_state (session_id, patient_id, last_score, last_level, last_factors, last_trigger,
                       updated_at, alerted_at, alert_score, alert_count, peak_score)
                   VALUES (%s,%s,%s,%s,%s,%s,NOW(), CASE WHEN %s THEN NOW() END, CASE WHEN %s THEN %s END, CASE WHEN %s THEN 1 ELSE 0 END, %s)
                   ON CONFLICT (session_id) DO UPDATE SET
                       last_score = EXCLUDED.last_score, last_level = EXCLUDED.last_level, last_factors = EXCLUDED.last_factors,
                       last_trigger = EXCLUDED.last_trigger, updated_at = NOW(),
                       alerted_at = CASE WHEN %s THEN NOW() ELSE hd_idh_twin_state.alerted_at END,
                       alert_score = CASE WHEN %s THEN %s ELSE hd_idh_twin_state.alert_score END,
                       alert_count = hd_idh_twin_state.alert_count + CASE WHEN %s THEN 1 ELSE 0 END,
                       peak_score = GREATEST(COALESCE(hd_idh_twin_state.peak_score, 0), EXCLUDED.peak_score)""",
                (session_id, patient_id, res["score"], res["level"], factors, trigger[:30],
                 alerted, alerted, res["score"], alerted, res["score"],
                 alerted, alerted, res["score"], alerted))


def alert_text(bed, name, hn, res, reason) -> str:
    L = [f"🩸 Kidney Twin — เสี่ยงความดันตก (IDH) {'ซ้ำ ' if reason == 're-alert' else ''}เตียง {bed} {name} (HN {hn})",
         f"คะแนน {res['score']}/100 (สูง)"]
    for x in res["factors"][:5]:
        L.append(f"• {x['text']} (+{x['points']})")
    recs = [r for r in res.get("recommendations", []) if r.get("priority") == "high"][:2] or res.get("recommendations", [])[:2]
    for r in recs:
        L.append(f"{r.get('icon', '👉')} {r.get('text')}")
    if res.get("advice"):
        L.append(res["advice"])
    L.append("(ข้อเสนอจากระบบ — พยาบาล/แพทย์ตัดสินใจ)")
    return "\n".join(L)


def evaluate(conn, session_id: int, trigger: str, cfg: dict, push_fn=None, log_fn=None) -> dict:
    """คำนวณ + ตัดสินเตือน + บันทึก state · คืน {"score","level","alerted","reason"} · ไม่ raise"""
    now = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            cur.execute("""SELECT s.patient_id, s.bed_number, p.patient_name, p.hn, s.status FROM hd_sessions s
                           JOIN hd_patient_mapping p ON p.id = s.patient_id WHERE s.id = %s""", (session_id,))
            r = cur.fetchone()
            if not r or r[4] not in ("active", "in_progress", "scheduled"):
                conn.commit()
                return {"skipped": "not_active"}
            pid, bed, name, hn = r[0], r[1], r[2], r[3]
            f = live_features(cur, session_id)
            if not f:
                conn.commit()
                return {"skipped": "no_features"}
            res = score_live(f)
            state = _load_state(cur, session_id)
            do_alert, reason = should_alert(state, res["score"], cfg, now)
            _save_state(cur, session_id, pid, res, trigger, do_alert)
            if do_alert:
                title = f"Kidney Twin เสี่ยง IDH สูง — {name} (HN {hn}) เตียง {bed}"
                msg = f"คะแนน {res['score']} · " + " · ".join(x["text"] for x in res["factors"][:4])
                try:
                    cur.execute("""INSERT INTO hd_clinical_alerts (session_id, patient_id, alert_type, severity, title, message, acknowledged, auto_generated)
                                   VALUES (%s, %s, 'idh_risk', 'warning', %s, %s, FALSE, TRUE)""", (session_id, pid, title[:200], msg[:1000]))
                except Exception as e:  # noqa: BLE001
                    logger.info("idh twin clinical alert insert: %s", e)
                    conn.rollback()
                    cur.execute(CREATE_SQL)
                    _save_state(cur, session_id, pid, res, trigger, do_alert)
            conn.commit()
        if do_alert and cfg.get("channel", "group") == "group" and push_fn:
            try:
                push_fn(alert_text(bed, name, hn, res, reason))
            except Exception as e:  # noqa: BLE001
                logger.warning("idh twin push: %s", e)
        if do_alert and log_fn:
            try:
                log_fn(session_id, hn, res, reason, trigger)
            except Exception:
                pass
        return {"score": res["score"], "level": res["level"], "alerted": do_alert, "reason": reason, "factors": res["factors"]}
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("idh twin evaluate failed (session %s): %s", session_id, e)
        return {"error": str(e)[:120]}


# ───────────────────────── outcome ─────────────────────────

def record_outcomes(conn, max_rows: int = 200) -> int:
    """รอบที่จบแล้วและยังไม่มี outcome → ตัดสินจาก SBP ต่ำสุด (<90) หรือตก ≥30 จากค่าแรก หรือ worksheet comp_hypotension"""
    n = 0
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        cur.execute("""SELECT t.session_id FROM hd_idh_twin_state t JOIN hd_sessions s ON s.id = t.session_id
                       WHERE t.outcome IS NULL AND s.status IN ('completed', 'cancelled') LIMIT %s""", (max_rows,))
        ids = [r[0] for r in cur.fetchall()]
        for sid in ids:
            cur.execute("""SELECT sbp FROM (
                             SELECT recorded_at, sbp FROM hd_vital_signs WHERE session_id = %s AND sbp IS NOT NULL
                             UNION ALL SELECT COALESCE(recorded_at, NOW()), sbp FROM hd_flowsheet WHERE session_id = %s AND sbp IS NOT NULL) t
                           ORDER BY recorded_at ASC""", (sid, sid))
            sb = [int(r[0]) for r in cur.fetchall()]
            comp = False
            try:
                cur.execute("SAVEPOINT ot")
                cur.execute("SELECT COALESCE(comp_hypotension, FALSE) FROM hd_worksheet_data WHERE session_id = %s LIMIT 1", (sid,))
                r = cur.fetchone()
                comp = bool(r and r[0])
                cur.execute("RELEASE SAVEPOINT ot")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT ot")
            idh = comp or (bool(sb) and (min(sb) < IDH_SBP or (sb[0] - min(sb)) >= IDH_DROP))
            detail = ("worksheet: hypotension" if comp else (f"min SBP {min(sb)} (first {sb[0]})" if sb else "no BP data"))
            outcome = "idh" if idh else ("no_idh" if sb else "unknown")
            cur.execute("UPDATE hd_idh_twin_state SET outcome = %s, outcome_detail = %s, outcome_at = NOW() WHERE session_id = %s",
                        (outcome, detail, sid))
            n += 1
    conn.commit()
    return n


def stats(conn, days: int = 30) -> dict:
    """ความแม่นของตัวเอง: sensitivity (เตือนก่อนเกิด IDH) / false alarm (เตือนแต่ไม่เกิด)"""
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        cur.execute("""SELECT outcome, (alert_count > 0) AS alerted, COUNT(*) FROM hd_idh_twin_state
                       WHERE updated_at >= NOW() - (%s || ' days')::interval AND outcome IS NOT NULL
                       GROUP BY 1, 2""", (str(int(days)),))
        c = {}
        for outcome, alerted, cnt in cur.fetchall():
            c[(outcome, bool(alerted))] = int(cnt)
    tp, fn_ = c.get(("idh", True), 0), c.get(("idh", False), 0)
    fp, tn = c.get(("no_idh", True), 0), c.get(("no_idh", False), 0)
    sens = round(tp / (tp + fn_) * 100, 1) if (tp + fn_) else None
    ppv = round(tp / (tp + fp) * 100, 1) if (tp + fp) else None
    return {"days": days, "sessions": tp + fn_ + fp + tn, "idh_events": tp + fn_, "alerted": tp + fp,
            "true_positive": tp, "missed": fn_, "false_alarm": fp, "quiet_ok": tn,
            "sensitivity_pct": sens, "precision_pct": ppv, "unknown": c.get(("unknown", True), 0) + c.get(("unknown", False), 0)}
