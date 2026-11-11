"""Clinical alert evaluation for vital signs recorded during HD.

Single source of truth used by both the quick "วัด BP" button on the
dashboard and the full vital-signs form on the session page. It only
*detects and notifies* — it never changes UF, drugs or the machine.

Rules (thresholds come from hd_settings where configurable):
    SBP  < severe_sbp (default 90)          → critical  ความดันต่ำวิกฤต
    SBP  drop ≥ sbp_drop (default 20) vs pre-HD → warning  ความดันตก (IDH)
    SBP  drop ≥ 30 vs previous reading       → warning  ความดันตกเร็ว
    SBP  ≥ 200 / ≥ 180                       → critical / warning ความดันสูง
    HR   < 40 or > 140 / < 50 or > 120       → critical / warning
    SpO2 < 88 / < 92                         → critical / warning
    Temp ≥ 39.0 / ≥ 38.0                     → critical / warning ไข้
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SEVERITY_ICON = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
DEDUPE_MINUTES = 30          # same alert_type on same session → don't re-insert
_LAST_PUSH = {}              # (session_id, alert_type, severity) → datetime (per process)
PUSH_COOLDOWN_MINUTES = 15


def _i(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def evaluate(sbp=None, dbp=None, hr=None, spo2=None, temp=None,
             pre_sbp=None, prev_sbp=None, severe_sbp=90, sbp_drop=20):
    """Return a list of alert dicts for one set of readings (no DB access)."""
    sbp, dbp, hr, spo2 = _i(sbp), _i(dbp), _i(hr), _i(spo2)
    temp = _f(temp)
    severe_sbp = _i(severe_sbp) or 90
    sbp_drop = _i(sbp_drop) or 20
    alerts = []
    bp_txt = f"BP {sbp}/{dbp if dbp is not None else '-'} mmHg" if sbp is not None else ""

    if sbp is not None:
        if sbp < severe_sbp:
            alerts.append({"alert_type": "hypotension", "severity": "critical",
                           "title": f"ความดันต่ำวิกฤต — {bp_txt}",
                           "message": f"SBP {sbp} < {severe_sbp} mmHg (เกณฑ์วิกฤต)"})
        elif pre_sbp and (pre_sbp - sbp) >= sbp_drop:
            alerts.append({"alert_type": "idh", "severity": "warning",
                           "title": f"ความดันตก (IDH) — {bp_txt}",
                           "message": f"SBP ลดลง {pre_sbp - sbp} mmHg จากก่อนฟอก ({pre_sbp} → {sbp})"})
        elif prev_sbp and (prev_sbp - sbp) >= 30:
            alerts.append({"alert_type": "idh", "severity": "warning",
                           "title": f"ความดันตกเร็ว — {bp_txt}",
                           "message": f"SBP ลดลง {prev_sbp - sbp} mmHg จากครั้งก่อน ({prev_sbp} → {sbp})"})
        if sbp >= 200:
            alerts.append({"alert_type": "hypertension", "severity": "critical",
                           "title": f"ความดันสูงวิกฤต — {bp_txt}", "message": f"SBP {sbp} ≥ 200 mmHg"})
        elif sbp >= 180:
            alerts.append({"alert_type": "hypertension", "severity": "warning",
                           "title": f"ความดันสูง — {bp_txt}", "message": f"SBP {sbp} ≥ 180 mmHg"})

    if hr is not None:
        if hr < 40 or hr > 140:
            alerts.append({"alert_type": "heart_rate", "severity": "critical",
                           "title": f"ชีพจรผิดปกติวิกฤต — HR {hr} bpm", "message": f"HR {hr} (เกณฑ์ <40 หรือ >140)"})
        elif hr < 50 or hr > 120:
            alerts.append({"alert_type": "heart_rate", "severity": "warning",
                           "title": f"ชีพจรผิดปกติ — HR {hr} bpm", "message": f"HR {hr} (เกณฑ์ <50 หรือ >120)"})

    if spo2 is not None:
        if spo2 < 88:
            alerts.append({"alert_type": "spo2", "severity": "critical",
                           "title": f"ออกซิเจนต่ำวิกฤต — SpO2 {spo2}%", "message": f"SpO2 {spo2}% < 88%"})
        elif spo2 < 92:
            alerts.append({"alert_type": "spo2", "severity": "warning",
                           "title": f"ออกซิเจนต่ำ — SpO2 {spo2}%", "message": f"SpO2 {spo2}% < 92%"})

    if temp is not None:
        if temp >= 39.0:
            alerts.append({"alert_type": "fever", "severity": "critical",
                           "title": f"ไข้สูง — {temp:.1f} °C", "message": f"อุณหภูมิ {temp:.1f} °C ≥ 39.0"})
        elif temp >= 38.0:
            alerts.append({"alert_type": "fever", "severity": "warning",
                           "title": f"มีไข้ — {temp:.1f} °C", "message": f"อุณหภูมิ {temp:.1f} °C ≥ 38.0"})
    return alerts


def session_context(cur, session_id):
    """Fetch what the evaluator and the LINE message need. Returns dict or None."""
    cur.execute("""
        SELECT s.id, s.bed_number, s.patient_id, p.hn, p.patient_name, s.status
        FROM hd_sessions s LEFT JOIN hd_patient_mapping p ON p.id = s.patient_id
        WHERE s.id = %s
    """, (session_id,))
    r = cur.fetchone()
    if not r:
        return None
    ctx = {"session_id": r[0], "bed": r[1], "patient_id": r[2], "hn": r[3],
           "name": r[4] or "", "status": r[5], "pre_sbp": None, "prev_sbp": None}
    cur.execute("""SELECT sbp FROM hd_vital_signs WHERE session_id = %s AND sbp IS NOT NULL
                   ORDER BY recorded_at ASC LIMIT 1""", (session_id,))
    row = cur.fetchone()
    ctx["pre_sbp"] = row[0] if row else None
    cur.execute("""SELECT sbp FROM hd_vital_signs WHERE session_id = %s AND sbp IS NOT NULL
                   ORDER BY recorded_at DESC LIMIT 1 OFFSET 1""", (session_id,))
    row = cur.fetchone()
    ctx["prev_sbp"] = row[0] if row else None
    return ctx


def store_alerts(cur, session_id, patient_id, alerts):
    """Insert alerts, skipping duplicates of the same type still open (<30 min).

    Returns the alerts that were actually inserted (with their ids).
    """
    inserted = []
    for a in alerts:
        cur.execute("""
            SELECT id, severity FROM hd_clinical_alerts
            WHERE session_id = %s AND alert_type = %s AND acknowledged = FALSE
              AND created_at > NOW() - INTERVAL '%s minutes'
            ORDER BY created_at DESC LIMIT 1
        """, (session_id, a["alert_type"], DEDUPE_MINUTES))
        dup = cur.fetchone()
        if dup and not (dup[1] == "warning" and a["severity"] == "critical"):
            continue
        cur.execute("""
            INSERT INTO hd_clinical_alerts
                (session_id, patient_id, alert_type, severity, title, message, acknowledged)
            VALUES (%s, %s, %s, %s::hd_alert_severity, %s, %s, FALSE)
            RETURNING id
        """, (session_id, patient_id, a["alert_type"], a["severity"], a["title"], a["message"]))
        a = dict(a, id=cur.fetchone()[0])
        inserted.append(a)
    return inserted


def _thai_now_str():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%d/%m %H:%M")


def build_line_message(ctx, alert, base_url):
    icon = SEVERITY_ICON.get(alert["severity"], "⚠️")
    level = "วิกฤต" if alert["severity"] == "critical" else "เฝ้าระวัง"
    return (f"{icon} HD ALERT — {level}\n"
            f"━━━━━━━━━━━━\n"
            f"👤 {ctx['name']}  (HN {ctx['hn'] or '-'})\n"
            f"🛏 เตียง {ctx['bed'] or '-'}\n"
            f"⚠️ สาเหตุ: {alert['title']}\n"
            f"📊 {alert['message']}\n"
            f"🕒 {_thai_now_str()} น.\n"
            f"━━━━━━━━━━━━\n"
            f"✍️ สั่งการรักษา / ดูรายละเอียด:\n{base_url}/hd/session/{ctx['session_id']}#alerts")


def notify_physician(push_fn, recipients, ctx, alerts, base_url):
    """Push each new warning/critical alert to the physician(s). Returns count sent."""
    if not push_fn or not recipients:
        return 0
    sent = 0
    now = datetime.now(timezone.utc)
    for a in alerts:
        if a["severity"] not in ("critical", "warning"):
            continue
        key = (ctx["session_id"], a["alert_type"], a["severity"])
        last = _LAST_PUSH.get(key)
        if last and (now - last) < timedelta(minutes=PUSH_COOLDOWN_MINUTES):
            continue
        msg = build_line_message(ctx, a, base_url)
        ok_any = False
        for uid in recipients:
            try:
                # push_message() returns False (and logs) on LINE API failure
                # instead of raising — treat that as not sent so cooldown is
                # not armed and the next reading retries.
                if push_fn(uid, msg) is not False:
                    ok_any = True
                else:
                    logger.warning("alert push to %s returned False (LINE API error / token missing)", str(uid)[:6])
            except Exception as e:   # never let LINE failure break the vital save
                logger.warning("alert push to %s failed: %s", str(uid)[:6], e)
        if ok_any:
            _LAST_PUSH[key] = now
            sent += 1
    return sent
