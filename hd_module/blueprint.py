"""HD Smart Monitoring — Flask Blueprint.

All routes under /hd/api/v1/.  Completely isolated from server.py.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone  # noqa: F401
from flask import (  # noqa: F401
    Blueprint, request, jsonify, g, render_template, redirect, url_for,
    current_app, make_response, Response,
)

from .auth import require_role  # noqa: F401
from .audit import log_action
from .config import HdConfig

logger = logging.getLogger(__name__)

# ── Thai Timezone Helper ─────────────────────────────────
# Render server runs in UTC. All "today" calculations must use Thai time.
_THAI_TZ = timezone(offset=__import__('datetime').timedelta(hours=7))


def push_nurse_group(message, fallback_recipients=True):
    """Send `message` to the nurse LINE group via Messaging API push
    (hd_settings.nurse_group_id = groupId from the 'groupid' command).

    LINE Notify was discontinued on 31 Mar 2025, so this is the only group
    channel. If no group id is set and fallback_recipients is True, the
    message goes to alert_recipients individually instead.
    Returns {"sent": bool, "method": "group"|"push"|None, "count": n, "error": str|None}.
    """
    from flask import current_app
    push_fn = current_app.config.get("LINE_PUSH_FN")
    if not push_fn:
        return {"sent": False, "method": None, "count": 0, "error": "LINE_PUSH_FN not configured"}
    gid = (get_setting_value("nurse_group_id", "") or "").strip()
    if gid:
        try:
            ok = push_fn(gid, message)
            if ok is not False:
                return {"sent": True, "method": "group", "count": 1, "error": None}
            err = "LINE API ปฏิเสธ (OA ยังไม่อยู่ในกลุ่ม หรือ groupId ผิด)"
        except Exception as e:
            err = str(e)
        logger.warning("nurse group push failed: %s", err)
        if not fallback_recipients:
            return {"sent": False, "method": "group", "count": 0, "error": err}
    else:
        err = "ยังไม่ตั้ง nurse_group_id"
    if not fallback_recipients:
        return {"sent": False, "method": None, "count": 0, "error": err}
    ids = get_setting_value("alert_recipients", "") or ""
    n = 0
    for uid in [r.strip() for r in ids.split(",") if r.strip()]:
        try:
            if push_fn(uid, message) is not False:
                n += 1
        except Exception as e:
            logger.warning("nurse fallback push %s: %s", uid[:6], e)
    return {"sent": n > 0, "method": "push", "count": n, "error": None if n else err}


def _safe_int(val):
    """Return int or None — prevents 'invalid input syntax for type integer'."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _content_disposition(filename, fallback="file"):
    """Build a Content-Disposition header safe for non-ASCII (Thai) filenames.

    HTTP headers must be Latin-1; a Thai filename in the plain `filename=`
    parameter makes the server reject the response (400/502). RFC 5987
    `filename*=UTF-8''...` carries the real name; an ASCII fallback keeps
    old clients working.
    """
    import re as _re
    from urllib.parse import quote
    name = (filename or fallback).replace('"', "")
    ext = ("." + name.rsplit(".", 1)[1]) if "." in name else ""
    ascii_name = _re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or (fallback + ext)
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def _thai_now():
    """Current datetime in Thai timezone (UTC+7)."""
    return datetime.now(_THAI_TZ)


def _thai_today():
    """Today's date in Thai timezone (UTC+7)."""
    return _thai_now().date()

hd_bp = Blueprint(
    "hd", __name__,
    url_prefix="/hd/api/v1",
    template_folder="templates",
)

# ── Web UI (template) routes ─────────────────────────────
# Mounted at /hd/* via a second Blueprint so the API prefix
# doesn't collide with page URLs.

hd_web = Blueprint(
    "hd_web", __name__,
    url_prefix="/hd",
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)


@hd_web.context_processor
def inject_total_beds():
    """ส่ง total_beds ให้ทุก template อัตโนมัติ — ไม่ต้องใส่รายหน้า."""
    return {"total_beds": HdConfig.TOTAL_BEDS}


@hd_web.route("/")
def hd_index():
    """Redirect root to login."""
    return redirect(url_for("hd_web.hd_login_page"))


@hd_web.route("/login")
def hd_login_page():
    """Render login page."""
    return render_template("hd_login.html")


@hd_web.route("/dashboard")
def hd_dashboard_page():
    """Render 30-bed dashboard."""
    resp = make_response(render_template("hd_dashboard.html", config=HdConfig, total_beds=HdConfig.TOTAL_BEDS))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@hd_web.route("/bedmap")
def hd_bedmap_page():
    """Smart Bed Map — แผนผังห้องฟอกจริง."""
    return render_template("hd_bedmap.html", total_beds=HdConfig.TOTAL_BEDS)


@hd_web.route("/session/new")
def hd_session_new():
    """Render new HD session form (Pre/Intra/Post tabs)."""
    # Pre-load patients for dropdown (server-side)
    patients = []
    db_err = ""
    try:
        import os, psycopg2
        db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        if db_url:
            conn = psycopg2.connect(db_url, connect_timeout=5)
            with conn.cursor() as cur:
                cur.execute("SELECT hn, patient_name, vascular_access, schedule_days FROM hd_patient_mapping WHERE is_active=TRUE ORDER BY patient_name LIMIT 200")
                patients = [{"hn": r[0], "name": r[1] or "", "va": r[2] or "", "sched": r[3] or ""} for r in cur.fetchall()]
            conn.close()
        else:
            db_err = "no_db_url"
    except Exception as e:
        db_err = str(e)
        logger.warning("session_new patient preload error: %s", e)
    resp = make_response(render_template("hd_session.html", server_patients=patients, db_err=db_err))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@hd_web.route("/session/<int:session_id>")
def hd_session_detail(session_id):
    """Render existing HD session — loads data via JS."""
    # Pre-load patients for dropdown
    patients = []
    try:
        import os, psycopg2
        db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        if db_url:
            conn = psycopg2.connect(db_url, connect_timeout=5)
            with conn.cursor() as cur:
                cur.execute("SELECT hn, patient_name, vascular_access, schedule_days FROM hd_patient_mapping WHERE is_active=TRUE ORDER BY patient_name LIMIT 200")
                patients = [{"hn": r[0], "name": r[1] or "", "va": r[2] or "", "sched": r[3] or ""} for r in cur.fetchall()]
            conn.close()
    except Exception:
        pass
    resp = make_response(render_template("hd_session.html", session_id=session_id, server_patients=patients, db_err=""))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@hd_web.route("/patients")
def hd_patient_list_page():
    """Render patient list."""
    return render_template("hd_patient_list.html")


@hd_web.route("/patient/<hn>")
def hd_patient_detail(hn):
    """Patient detail page with history."""
    return render_template("hd_patient_detail.html", patient_hn=hn)


@hd_web.route("/alerts")
def hd_alerts_page():
    """Render clinical alerts page."""
    return render_template("hd_alerts.html")


@hd_web.route("/kpi")
def hd_kpi_page():
    """Render KPI dashboard page."""
    return render_template("hd_kpi.html")


@hd_web.route("/checkin")
def hd_checkin_page():
    """QR Check-in V2 — auto-identify patient from schedule, show history, approve."""
    from .qr_checkin import verify_qr_token
    bed = request.args.get("bed", type=int)
    token = request.args.get("token", "")
    if not bed or not token or not verify_qr_token(bed, token):
        return "<h2>❌ QR Code ไม่ถูกต้องหรือหมดอายุ</h2><a href='/hd/dashboard'>กลับ Dashboard</a>", 400
    return render_template("hd_checkin.html", bed_number=bed, qr_token=token)


@hd_web.route("/calculator")
def hd_calculator_page():
    """Kt/V Calculator page."""
    return render_template("hd_calculator.html")


@hd_web.route("/analytics")
def hd_analytics_page():
    """Render analytics dashboard."""
    return render_template("hd_analytics.html")


@hd_web.route("/settings")
def hd_settings_page():
    """Render admin settings page."""
    return render_template("hd_settings.html")


@hd_web.route("/labs")
def hd_labs_index():
    """Lab Results — หน้ารวมเลือกคนไข้."""
    # Pre-load patients + latest lab server-side — ไม่ต้องพึ่ง API + token
    patients = []
    try:
        import os, psycopg2
        from datetime import date as _date
        db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        if db_url:
            conn = psycopg2.connect(db_url, connect_timeout=5)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT id, hn, patient_name, vascular_access, schedule_days FROM hd_patient_mapping WHERE is_active = TRUE ORDER BY patient_name LIMIT 300")
                prows = cur.fetchall()
                # ดึง Lab ล่าสุดแต่ละคน — ลอง hd_labs VIEW ก่อน, fallback ไป hd_lab_results
                lab_map = {}  # patient_id → {hb, k, ktv, lab_date}
                pids = [r[0] for r in prows]
                if pids:
                    for _lab_sql in [
                        """SELECT DISTINCT ON (patient_id) patient_id, hb, k, ktv, lab_date
                           FROM hd_labs WHERE patient_id = ANY(%s)
                           ORDER BY patient_id, lab_date DESC NULLS LAST""",
                        """SELECT DISTINCT ON (patient_id) patient_id,
                                  hemoglobin, potassium, kt_v, lab_date
                           FROM hd_lab_results WHERE patient_id = ANY(%s)
                           ORDER BY patient_id, lab_date DESC NULLS LAST""",
                    ]:
                        try:
                            cur.execute(_lab_sql, (pids,))
                            for lr in cur.fetchall():
                                _today = _thai_today()
                                _ld = lr[4]
                                lab_map[lr[0]] = {
                                    "hb": float(lr[1]) if lr[1] else None,
                                    "k": float(lr[2]) if lr[2] else None,
                                    "ktv": float(lr[3]) if lr[3] else None,
                                    "lab_date": _ld.isoformat() if _ld else None,
                                    "lab_age": (_today - _ld).days if _ld else None,
                                }
                            break  # สำเร็จ → ไม่ต้อง fallback
                        except Exception:
                            continue  # ลอง query ถัดไป
                for r in prows:
                    lb = lab_map.get(r[0], {})
                    patients.append({
                        "hn": r[1], "name": r[2] or "", "va": r[3] or "", "sched": r[4] or "",
                        "hb": lb.get("hb"), "k": lb.get("k"), "ktv": lb.get("ktv"),
                        "lab_date": lb.get("lab_date"), "lab_age": lb.get("lab_age"),
                    })
            conn.close()
    except Exception as e:
        logger.warning("labs_index preload: %s", e)
    import json
    resp = make_response(render_template("hd_labs_index.html", server_patients=json.dumps(patients, ensure_ascii=False)))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@hd_web.route("/labs/<hn>")
def hd_labs_page(hn):
    """Lab Results page for a patient."""
    return render_template("hd_labs.html", patient_hn=hn)


@hd_web.route("/sheet/<int:session_id>")
def hd_sheet_page(session_id):
    """HD Monitoring Sheet page."""
    return render_template("hd_sheet.html", session_id=session_id)


@hd_web.route("/worksheet/<int:session_id>")
def hd_worksheet_page(session_id):
    """HD Work Sheet Interactive — พยาบาลกรอก checklist."""
    return render_template("hd_worksheet.html", session_id=session_id)


@hd_web.route("/epo-dashboard/<hn>")
def hd_epo_dashboard_page(hn):
    """EPO Response Dashboard page."""
    return render_template("hd_epo_dashboard.html", patient_hn=hn)


@hd_web.route("/quality")
def hd_quality_page():
    """Quality indicators page."""
    return render_template("hd_quality.html")


@hd_web.route("/checklist/page")
def checklist_page():
    """Render the daily checklist page — auth handled by JS/API."""
    return render_template("hd_checklist.html")


@hd_web.route("/morning-brief")
def morning_brief_page():
    """Render the Morning Brief web page — auth handled by JS/API."""
    return render_template("hd_morning_brief.html")


@hd_web.route("/clinic-profile")
def clinic_profile_page():
    """Clinic Profile & Accreditation — 10 tabs."""
    return render_template("hd_clinic_profile.html")


@hd_web.route("/water-ro")
def water_ro_page():
    """ตรวจระบบน้ำ RO ประจำวัน — ปฏิทิน/รายการ/กรอกเอง."""
    return render_template("hd_water_ro.html")


@hd_web.route("/handover")
def hd_handover_page():
    """Shift Handover — สรุปส่งต่อเวรดิจิทัล."""
    return render_template("hd_handover.html")


@hd_web.route("/radar")
def hd_radar_page():
    """ข้อ 5 (17 พ.ย. 2569): เรดาร์หัวหน้าเวร — ทุกเตียงเรียงตามความเร่งด่วน"""
    return render_template("hd_radar.html")


@hd_web.route("/tv")
def hd_tv_page():
    """Nurse Station TV mode — standalone, dark, auto-refresh, no nav (ว้าว #6)."""
    resp = make_response(render_template("hd_tv.html"))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@hd_web.after_request
def _no_cache_sw(resp):
    """Service worker + manifest must never be cached by the browser/CDN, or a broken SW lingers for days."""
    try:
        if request.path.endswith(("/static/sw.js", "/static/manifest.json")):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Service-Worker-Allowed"] = "/hd/"
    except Exception:
        pass
    return resp


@hd_web.route("/patient-card/<hn>")
def patient_card_page(hn):
    """Printable patient card (name + schedule + portal QR) — staff page."""
    return render_template("hd_patient_card.html", patient_hn=hn)


@hd_web.route("/my/<token>")
def patient_portal_page(token):
    """Patient Portal — mobile-friendly page for the patient to view their own HD results."""
    return render_template("hd_patient_portal.html", token=token)


@hd_web.route("/weekly")
def weekly_page():
    """ตารางฟอกรายสัปดาห์ 7 วัน × 3 รอบ — for the unit wall / TV."""
    return render_template("hd_weekly.html")


@hd_web.route("/fluid")
def fluid_page():
    """Fluid Status Center — BCM quarterly assessments vs dry weight."""
    return render_template("hd_fluid.html")


@hd_web.route("/patient/<hn>/trends")
def patient_trends_page(hn):
    """Patient Trend Dashboard — กราฟ Hb, Kt/V, น้ำหนัก, BP 6 เดือน."""
    return render_template("hd_trends.html", patient_hn=hn)


@hd_web.route("/patient/<hn>/360")
def patient_360_page(hn):
    """Patient 360 — one timeline of sessions, alerts, labs, ESA, drugs, incidents."""
    return render_template("hd_patient_360.html", patient_hn=hn)


@hd_web.route("/privacy")
def privacy_page():
    """Public privacy notice (no patient data). Linked from patient-facing messages."""
    from .pdpa import NOTICE_VERSION, notice_text
    txt = notice_text(get_setting_value("clinic_name", "") or "คลินิกระนอง รักษ์ไต", get_setting_value("clinic_phone", "") or "")
    return render_template("hd_privacy.html", notice=txt, version=NOTICE_VERSION)


@hd_web.route("/consents")
def consents_page():
    return render_template("hd_consents.html")


@hd_web.route("/scheduler")
def scheduler_page():
    """Smart Bed Scheduler — week plan with HBV/HCV zones and machine status."""
    return render_template("hd_scheduler.html")


@hd_web.route("/clinic-pulse")
def clinic_pulse_page():
    """🫀 Live Clinic Pulse — ชีพจรคลินิก real-time."""
    return render_template("hd_clinic_pulse.html")


@hd_web.route("/ai")
def ai_chat_page():
    """🤖 AI Assistant — ถามอะไรก็ตอบจากข้อมูลจริง."""
    return render_template("hd_ai_chat.html")


@hd_web.route("/srt-report")
def srt_report_page():
    """ศ.ร.ต. Auto Report — รายงานคุณภาพรายเดือน."""
    return render_template("hd_srt_report.html")


@hd_web.route("/adequacy")
def adequacy_page():
    """Adequacy Guardian — Kt/V per patient with levers to reach target."""
    return render_template("hd_adequacy.html")


@hd_web.route("/anemia")
def anemia_page():
    """Anemia Command Center — iron + ESA + Hb trajectory for every patient."""
    return render_template("hd_anemia.html")


@hd_web.route("/va-watch")
def va_watch_page():
    """VA Watch — vascular access trend per patient."""
    return render_template("hd_va_watch.html")


@hd_web.route("/esa")
def esa_autopilot_page():
    """ESA Autopilot — proposals waiting for the physician."""
    return render_template("hd_esa.html")


@hd_web.route("/daily")
def daily_log_page():
    """ทะเบียนประจำวัน — every session of a date, grouped by shift."""
    return render_template("hd_daily_log.html")


# ── Daily log (ทะเบียนประจำวัน) ───────────────────────────

_DAILY_LOG_SQL = """
    SELECT s.id, s.shift, s.bed_number, s.status,
           p.hn, p.patient_name, p.vascular_access,
           s.started_at, s.ended_at,
           s.pre_weight, s.post_weight, p.dry_weight, s.uf_goal, s.actual_uf,
           s.esa_given, s.esa_drug_given, s.esa_dose_given, s.esa_route, s.esa_skip_reason,
           s.hb_value, s.hct_value, s.notes,
           (SELECT COUNT(*) FROM hd_vital_signs v WHERE v.session_id = s.id AND v.sbp IS NOT NULL),
           (SELECT COUNT(*) FROM hd_clinical_alerts a WHERE a.session_id = s.id),
           (SELECT COUNT(*) FROM hd_clinical_alerts a WHERE a.session_id = s.id AND a.severity::text = 'critical'),
           (SELECT sbp || '/' || dbp FROM hd_vital_signs v WHERE v.session_id = s.id AND v.sbp IS NOT NULL
              ORDER BY v.recorded_at ASC LIMIT 1),
           (SELECT sbp || '/' || dbp FROM hd_vital_signs v WHERE v.session_id = s.id AND v.sbp IS NOT NULL
              ORDER BY v.recorded_at DESC LIMIT 1),
           (SELECT MIN(sbp) FROM hd_vital_signs v WHERE v.session_id = s.id)
    FROM hd_sessions s
    JOIN hd_patient_mapping p ON p.id = s.patient_id
    WHERE s.session_date = %s
    ORDER BY CASE COALESCE(s.shift,'morning') WHEN 'morning' THEN 0 WHEN 'afternoon' THEN 1 ELSE 2 END,
             s.bed_number, s.id
"""

_DAILY_LOG_COLS = ["session_id", "shift", "bed", "status", "hn", "patient_name", "vascular_access",
                   "started_at", "ended_at", "pre_weight", "post_weight", "dry_weight", "uf_goal",
                   "actual_uf", "esa_given", "esa_drug", "esa_dose", "esa_route", "esa_skip_reason",
                   "hb", "hct", "notes", "vital_count", "alert_count", "critical_count",
                   "pre_bp", "post_bp", "min_sbp"]


def _daily_log_rows(date_str):
    import os
    import psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return []
    conn = psycopg2.connect(db_url, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(_DAILY_LOG_SQL, (date_str,))
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(zip(_DAILY_LOG_COLS, r))
        for k in ("started_at", "ended_at"):
            d[k] = (d[k].isoformat() + "Z") if d[k] else None
        for k in ("pre_weight", "post_weight", "dry_weight", "uf_goal", "actual_uf", "hb", "hct"):
            d[k] = float(d[k]) if d[k] is not None else None
        d["shift"] = d["shift"] or "morning"
        if d["actual_uf"] is None and d["pre_weight"] and d["post_weight"]:
            d["actual_uf"] = round((d["pre_weight"] - d["post_weight"]) * 1000)
        out.append(d)
    return out


def _parse_date_arg(default_today=True):
    from datetime import date as _d
    s = (request.args.get("date") or "").strip()
    if s:
        try:
            return _d.fromisoformat(s)
        except ValueError:
            pass
    return _thai_today() if default_today else None


@hd_bp.route("/daily-log", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def daily_log():
    """All sessions of one date (default: today, Thai time), grouped by shift."""
    day = _parse_date_arg()
    try:
        rows = _daily_log_rows(day.isoformat())
    except Exception as e:
        logger.error("daily_log error: %s", e)
        return jsonify({"date": day.isoformat(), "error": str(e), "shifts": {}, "total": 0}), 500
    shifts = {"morning": [], "afternoon": [], "evening": []}
    for r in rows:
        shifts.setdefault(r["shift"], []).append(r)
    completed = [r for r in rows if r["status"] == "completed"]
    summary = {
        "total": len(rows),
        "completed": len(completed),
        "in_progress": sum(1 for r in rows if r["status"] == "in_progress"),
        "cancelled": sum(1 for r in rows if r["status"] == "cancelled"),
        "esa_given": sum(1 for r in completed if r["esa_given"] is True),
        "with_alerts": sum(1 for r in rows if r["alert_count"]),
        "total_uf_ml": round(sum(r["actual_uf"] or 0 for r in completed)),
    }
    return jsonify({"date": day.isoformat(), "shifts": shifts, "summary": summary, "total": len(rows)})


def _daily_log_csv_bytes(day, rows):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["วันที่", "รอบ", "เตียง", "สถานะ", "HN", "ชื่อ-สกุล", "VA", "เริ่ม", "สิ้นสุด",
                "นน.ก่อน", "นน.หลัง", "DW", "UF เป้า", "UF จริง", "BP ก่อน", "BP หลัง", "SBP ต่ำสุด",
                "ESA ฉีด", "ชนิด ESA", "ขนาด", "ทาง", "เหตุผลงด", "Hb", "Hct",
                "Vital ครั้ง", "Alert", "Critical", "หมายเหตุ", "session_id"])
    th = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}
    for r in rows:
        def t(ts):
            if not ts:
                return ""
            from datetime import datetime, timedelta
            return (datetime.fromisoformat(ts.replace("Z", "")) + timedelta(hours=7)).strftime("%H:%M")
        w.writerow([day.isoformat(), th.get(r["shift"], r["shift"]), r["bed"], r["status"], r["hn"],
                    r["patient_name"], r["vascular_access"] or "", t(r["started_at"]), t(r["ended_at"]),
                    r["pre_weight"] or "", r["post_weight"] or "", r["dry_weight"] or "",
                    r["uf_goal"] or "", r["actual_uf"] or "", r["pre_bp"] or "", r["post_bp"] or "",
                    r["min_sbp"] or "",
                    {True: "ฉีด", False: "งด"}.get(r["esa_given"], ""), r["esa_drug"] or "",
                    r["esa_dose"] or "", r["esa_route"] or "", r["esa_skip_reason"] or "",
                    r["hb"] or "", r["hct"] or "", r["vital_count"], r["alert_count"],
                    r["critical_count"], (r["notes"] or "").replace("\n", " "), r["session_id"]])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _pdf_response(data, fname):
    resp = Response(data, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = _content_disposition(fname, "record.pdf")
    return resp


@hd_bp.route("/daily-log.csv", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def daily_log_csv():
    """CSV export of one day (UTF-8 BOM so Excel shows Thai correctly)."""
    day = _parse_date_arg()
    rows = _daily_log_rows(day.isoformat())
    data = _daily_log_csv_bytes(day, rows)
    fname = f"HD-{day.isoformat()}.csv"
    resp = Response(data, mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = _content_disposition(fname, fname)
    log_action(user_id=g.get("hd_user_id"), action="daily_log.export", resource_type="daily_log",
               new_values={"date": day.isoformat(), "rows": len(rows)}, ip_address=request.remote_addr or "")
    return resp


@hd_bp.route("/daily-log/<shift>.pdf", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def daily_log_shift_pdf(shift):
    """PDF table of one shift (เช้า/บ่าย/ค่ำ) of one day."""
    from .daily_records import shift_pdf, SHIFT_TH
    if shift not in SHIFT_TH:
        return jsonify({"error": "shift must be morning/afternoon/evening"}), 400
    day = _parse_date_arg()
    rows = [r for r in _daily_log_rows(day.isoformat()) if r["shift"] == shift]
    data = shift_pdf(day, shift, rows, get_setting_value("clinic_name", "") or "คลินิกระนอง รักษ์ไต")
    return _pdf_response(data, f"HD-{day.isoformat()}-{SHIFT_TH[shift]}.pdf")


@hd_bp.route("/sessions/<int:session_id>/record.pdf", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def session_record_pdf(session_id):
    """Per-session PDF: summary page + Monitoring Sheet page."""
    import os
    import psycopg2
    from .daily_records import load_session_record, session_pdf, session_filename
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "Database not configured"}), 500
    conn = psycopg2.connect(db_url, connect_timeout=5)
    conn.autocommit = True
    try:
        rec = load_session_record(conn, session_id)
    finally:
        conn.close()
    if not rec:
        return jsonify({"error": "session not found"}), 404
    data = session_pdf(rec, get_setting_value("clinic_name", "") or "คลินิกระนอง รักษ์ไต")
    log_action(user_id=g.get("hd_user_id"), action="session.pdf", resource_type="session",
               resource_id=session_id, ip_address=request.remote_addr or "")
    return _pdf_response(data, session_filename(rec["hn"], rec["name"], rec["bed"], rec["shift"]))


# ── Work Sheet Interactive API ─────────────────────────────

_WS_BOOL_COLS = [
    "assess_anxiety", "assess_sleep_disturbance", "assess_dyspnea", "assess_orthopnea",
    "assess_crepitation", "assess_jvp_engorged", "assess_arrhythmia", "assess_fever",
    "assess_headache", "assess_nausea_vomiting", "assess_anorexia", "assess_weakness",
    "assess_prolong_bleeding", "assess_pale", "assess_edema", "assess_itching",
    "assess_constipation", "assess_urine",
    "test_leak", "va_thrill", "va_bruit",
    "comp_hypotension", "comp_hypertension", "comp_muscle_cramp", "comp_chest_pain",
    "comp_headache", "comp_fever_chill", "comp_arrhythmia", "comp_va_problem",
    "comp_dialyzer", "comp_cardiac_arrest", "comp_dds", "comp_air_embolism", "comp_no_complication",
    "tech_blood_leak", "tech_clot_dialyzer", "tech_clotted_bloodline",
    "tech_machine_problem", "tech_needle_gt2", "tech_no_complication",
    # Nursing Management
    "nm_monitor_vs", "nm_psycho_support", "nm_oxygen", "nm_trendelenburg",
    "nm_decrease_ufr", "nm_decrease_bfr", "nm_hypertonic", "nm_access_flow",
    "nm_hot_compress", "nm_cold_compress", "nm_aspirate",
    "nm_chang_dialyzer", "nm_chang_bloodline", "nm_post_hd_care",
    # Health Education
    "he_nutrition", "he_vascular_access", "he_exercise", "he_personal_hygiene",
    "he_medication", "he_fluid_control", "he_kt",
]
_WS_TEXT_COLS = ["disinfectant", "va_type", "va_site", "nursing_dx",
                 "nursing_note", "doctor_order", "health_ed",
                 # Doctor Note interactive
                 "dx_fever", "dx_dyspnea", "dx_chest_pain", "dx_headache", "dx_cramp",
                 "post_dyspnea", "post_bleeding",
                 "discharge_type", "discharge_mobility",
                 "nurse_start_name", "nurse_end_name"]
_WS_INT_COLS = ["nurse_start_id", "nurse_end_id", "bp_discharge_sbp", "bp_discharge_dbp"]
_WS_ALL_COLS = _WS_BOOL_COLS + _WS_TEXT_COLS + _WS_INT_COLS


@hd_bp.route("/sessions/<int:session_id>/worksheet-data", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def get_worksheet_data(session_id):
    """Load Work Sheet checklist data for a session."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "Database not configured"}), 500
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        _ensure_ws_table(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_WS_ALL_COLS)}, updated_at FROM hd_worksheet_data WHERE session_id = %s", (session_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"data": None, "saved": False})
            d = {}
            for i, col in enumerate(_WS_ALL_COLS):
                d[col] = row[i]
            d["updated_at"] = row[-1].isoformat() if row[-1] else None
            return jsonify({"data": d, "saved": True})
    except Exception as e:
        logger.error("get_worksheet_data: %s", e)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


_WS_CREATE_SQL = """CREATE TABLE IF NOT EXISTS hd_worksheet_data (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL UNIQUE REFERENCES hd_sessions(id),
    assess_anxiety BOOLEAN, assess_sleep_disturbance BOOLEAN,
    assess_dyspnea BOOLEAN, assess_orthopnea BOOLEAN,
    assess_crepitation BOOLEAN, assess_jvp_engorged BOOLEAN,
    assess_arrhythmia BOOLEAN, assess_fever BOOLEAN,
    assess_headache BOOLEAN, assess_nausea_vomiting BOOLEAN,
    assess_anorexia BOOLEAN, assess_weakness BOOLEAN,
    assess_prolong_bleeding BOOLEAN, assess_pale BOOLEAN,
    assess_edema BOOLEAN, assess_itching BOOLEAN,
    assess_constipation BOOLEAN, assess_urine BOOLEAN,
    test_leak BOOLEAN, disinfectant VARCHAR(30),
    va_type VARCHAR(20), va_thrill BOOLEAN, va_bruit BOOLEAN, va_site VARCHAR(50),
    nursing_dx TEXT,
    comp_hypotension BOOLEAN, comp_hypertension BOOLEAN, comp_muscle_cramp BOOLEAN,
    comp_chest_pain BOOLEAN, comp_headache BOOLEAN, comp_fever_chill BOOLEAN,
    comp_arrhythmia BOOLEAN, comp_va_problem BOOLEAN, comp_dialyzer BOOLEAN,
    comp_cardiac_arrest BOOLEAN, comp_dds BOOLEAN, comp_air_embolism BOOLEAN,
    comp_no_complication BOOLEAN,
    tech_blood_leak BOOLEAN, tech_clot_dialyzer BOOLEAN, tech_clotted_bloodline BOOLEAN,
    tech_machine_problem BOOLEAN, tech_needle_gt2 BOOLEAN, tech_no_complication BOOLEAN,
    nursing_note TEXT, doctor_order TEXT, health_ed TEXT,
    nurse_start_id INTEGER, nurse_end_id INTEGER,
    created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(),
    updated_by INTEGER
)"""


_WS_ALTER_SQLS = [
    # Doctor Note
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS dx_fever VARCHAR(10)",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS dx_dyspnea VARCHAR(10)",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS dx_chest_pain VARCHAR(10)",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS dx_headache VARCHAR(10)",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS dx_cramp VARCHAR(10)",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS post_dyspnea VARCHAR(10)",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS post_bleeding VARCHAR(10)",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS discharge_type VARCHAR(20)",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS discharge_mobility VARCHAR(20)",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS bp_discharge_sbp INTEGER",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS bp_discharge_dbp INTEGER",
    # Nursing Management
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_monitor_vs BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_psycho_support BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_oxygen BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_trendelenburg BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_decrease_ufr BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_decrease_bfr BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_hypertonic BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_access_flow BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_hot_compress BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_cold_compress BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_aspirate BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_chang_dialyzer BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_chang_bloodline BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_post_hd_care BOOLEAN",
    # Health Education
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_nutrition BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_vascular_access BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_exercise BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_personal_hygiene BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_medication BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_fluid_control BOOLEAN",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_kt BOOLEAN",
    # Nurse signature names
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nurse_start_name VARCHAR(100)",
    "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nurse_end_name VARCHAR(100)",
]


def _ensure_ws_table(conn):
    """Ensure hd_worksheet_data table + all columns exist."""
    try:
        old_ac = conn.autocommit
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_WS_CREATE_SQL)
            for sql in _WS_ALTER_SQLS:
                try:
                    cur.execute(sql)
                except Exception:
                    pass
        conn.autocommit = old_ac
    except Exception as e:
        logger.debug("_ensure_ws_table: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass


@hd_bp.route("/sessions/<int:session_id>/worksheet-data", methods=["POST"])
@require_role("physician", "nurse", "admin")
def save_worksheet_data(session_id):
    """Save or update Work Sheet checklist data."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "Database not configured"}), 500
    body = request.get_json(silent=True) or {}
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        # Ensure table exists (autocommit mode — won't break main transaction)
        _ensure_ws_table(conn)

        with conn.cursor() as cur:
            # Auto-fill nurse name from logged-in user
            user_id = g.get("hd_user_id")
            user_role = g.get("hd_role", "")
            nurse_name_auto = None
            if user_id and user_role in ("nurse", "admin"):
                try:
                    cur.execute("SELECT full_name FROM hd_users WHERE id = %s", (user_id,))
                    nr = cur.fetchone()
                    if nr:
                        nurse_name_auto = nr[0]
                except Exception:
                    pass

            # Build values
            vals = {}
            for col in _WS_BOOL_COLS:
                v = body.get(col)
                vals[col] = bool(v) if v is not None else None
            for col in _WS_TEXT_COLS:
                vals[col] = body.get(col) or None
            for col in _WS_INT_COLS:
                v = body.get(col)
                vals[col] = int(v) if v else None

            # Auto-fill nurse names if not explicitly set
            if nurse_name_auto:
                if not vals.get("nurse_start_name"):
                    vals["nurse_start_name"] = nurse_name_auto
                vals["nurse_end_name"] = nurse_name_auto  # always update end nurse
                vals["nurse_end_id"] = user_id

            cols = list(vals.keys())
            placeholders = ", ".join(["%s"] * len(cols))
            update_set = ", ".join([f"{c} = EXCLUDED.{c}" for c in cols])
            sql = f"""INSERT INTO hd_worksheet_data (session_id, {', '.join(cols)}, updated_at, updated_by)
                      VALUES (%s, {placeholders}, NOW(), %s)
                      ON CONFLICT (session_id) DO UPDATE SET {update_set},
                          updated_at = NOW(), updated_by = EXCLUDED.updated_by
                      RETURNING id"""
            params = [session_id] + [vals[c] for c in cols] + [g.get("hd_user_id")]
            cur.execute(sql, params)
            rid = cur.fetchone()[0]
        conn.commit()
        log_action(user_id=g.get("hd_user_id"), action="worksheet.save", resource_type="hd_worksheet_data",
                   resource_id=session_id, ip_address=request.remote_addr or "")
        return jsonify({"id": rid, "saved": True}), 200
    except Exception as e:
        logger.error("save_worksheet_data: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@hd_bp.route("/sessions/<int:session_id>/worksheet.pdf", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def session_worksheet_pdf(session_id):
    """PDF ใบบันทึกรอบฟอก (Hemodialysis Work Sheet) ตามแบบฟอร์มคลินิก."""
    import os
    import psycopg2
    from .hd_worksheet import load_and_build
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "Database not configured"}), 500
    conn = psycopg2.connect(db_url, connect_timeout=5)
    conn.autocommit = True
    try:
        mode = request.args.get("mode", "draw")  # draw=แบบB (default), overlay=แบบA

        # Debug: if ?debug_ws=1, return ws_data as JSON instead of PDF
        if request.args.get("debug_ws"):
            from .hd_worksheet import load_and_build as _lab
            # Load ws_data the same way as load_and_build
            try:
                conn_dbg = psycopg2.connect(db_url, connect_timeout=5)
                conn_dbg.autocommit = True
                with conn_dbg.cursor() as cur_dbg:
                    cur_dbg.execute("SELECT * FROM hd_worksheet_data WHERE session_id = %s", (session_id,))
                    ws_row_dbg = cur_dbg.fetchone()
                    if ws_row_dbg:
                        col_names_dbg = [desc[0] for desc in cur_dbg.description]
                        ws_data_dbg = dict(zip(col_names_dbg, ws_row_dbg))
                        non_null = {k: v for k, v in ws_data_dbg.items() if v is not None}
                        return jsonify({"ws_data_found": True, "non_null_count": len(non_null),
                                        "sample": {k: v for k, v in list(non_null.items())[:15]}})
                    else:
                        return jsonify({"ws_data_found": False, "session_id": session_id})
                conn_dbg.close()
            except Exception as dbg_err:
                return jsonify({"debug_error": str(dbg_err)})

        data, fname = load_and_build(conn, session_id,
                                     get_setting_value("clinic_name", "") or "ระนอง รักษ์ไต",
                                     mode=mode)
    finally:
        conn.close()
    if not data:
        return jsonify({"error": "session not found"}), 404
    log_action(user_id=g.get("hd_user_id"), action="session.worksheet_pdf", resource_type="session",
               resource_id=session_id, ip_address=request.remote_addr or "")
    return _pdf_response(data, fname)


@hd_bp.route("/worksheets/batch.pdf", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def batch_worksheets_pdf():
    """ข้อ 5: Batch print — รวม Work Sheet ทั้งเวร/วัน เป็น PDF เดียว.
    ?date=YYYY-MM-DD&shift=morning|afternoon|evening (optional)"""
    import os
    import psycopg2
    from .hd_worksheet import load_and_build

    day = request.args.get("date", _thai_today().isoformat())
    shift = request.args.get("shift")

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "Database not configured"}), 500

    conn = psycopg2.connect(db_url, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            sql = "SELECT id FROM hd_sessions WHERE session_date = %s AND status = 'completed'"
            params = [day]
            if shift:
                sql += " AND shift = %s"
                params.append(shift)
            sql += " ORDER BY bed_number, started_at"
            cur.execute(sql, params)
            ids = [r[0] for r in cur.fetchall()]

        if not ids:
            return jsonify({"error": f"No completed sessions on {day}"}), 404

        # Merge all PDFs
        from pypdf import PdfReader, PdfWriter
        import io as _io
        writer = PdfWriter()
        clinic = get_setting_value("clinic_name", "") or "ระนอง รักษ์ไต"

        for sid in ids:
            pdf_bytes, _ = load_and_build(conn, sid, clinic, mode="draw")
            if pdf_bytes:
                reader = PdfReader(_io.BytesIO(pdf_bytes))
                for page in reader.pages:
                    writer.add_page(page)
    finally:
        conn.close()

    if len(writer.pages) == 0:
        return jsonify({"error": "No worksheets generated"}), 404

    out = _io.BytesIO()
    writer.write(out)
    shift_th = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}.get(shift or "", "ทั้งวัน")
    fname = f"HD-WorkSheets-{day}-{shift_th}.pdf"
    log_action(user_id=g.get("hd_user_id"), action="session.batch_worksheets", resource_type="batch",
               resource_id=day, ip_address=request.remote_addr or "")
    return _pdf_response(out.getvalue(), fname)



def run_daily_export(day, trigger="manual", user=None):
    """Render CSV + shift PDFs + session PDFs for `day` and upload to GCS
    (HD-Records/YYYY/YYYY-MM/YYYY-MM-DD/). Pushes a LINE summary to the
    physician. Returns the manifest. Used by the 22:30 scheduler and the
    '📦 ส่งสำเนา' button."""
    import os
    import psycopg2
    from flask import current_app
    from .daily_records import export_day, gcs_upload_fn, drive_upload_fn, combined_upload_fn, record_folder
    storage_obj = current_app.config.get("STORAGE_OBJ")
    gcs_up = gcs_upload_fn(storage_obj)
    _wire_drive_token()
    drive_up = drive_upload_fn()
    upload = combined_upload_fn(gcs_up, drive_up) if (gcs_up or drive_up) else None
    bucket_name = getattr(storage_obj, "bucket_name", None) or os.environ.get("GCS_BUCKET_NAME", "")
    if upload is None:
        res = {"ok": 0, "failed": 0, "files": [], "folder": record_folder(day), "date": day.isoformat(),
               "error": "GCS ไม่พร้อม (storage ไม่ได้เชื่อมต่อ) — ไฟล์ยังดาวน์โหลดจากหน้าทะเบียนได้"}
    else:
        db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        conn = psycopg2.connect(db_url, connect_timeout=5)
        conn.autocommit = True
        try:
            rows = _daily_log_rows(day.isoformat())
            csv_bytes = _daily_log_csv_bytes(day, rows)
            res = export_day(conn, day, rows, csv_bytes, upload,
                             get_setting_value("clinic_name", "") or "คลินิกระนอง รักษ์ไต")
        finally:
            conn.close()
        res["bucket"] = bucket_name
        res["drive_enabled"] = drive_up is not None
        res["drive_ok"] = sum(1 for f in res["files"] if f.get("drive") is True)
        res["drive_failed"] = sum(1 for f in res["files"] if "drive" in f and f.get("drive") is not True)
    res["trigger"] = trigger
    log_action(user_id=user, action="daily_log.gcs_export", resource_type="daily_log",
               new_values={"date": day.isoformat(), "ok": res["ok"], "failed": res["failed"],
                           "trigger": trigger, "error": res.get("error")}, ip_address="")
    # LINE the physician (success or failure) so a silent failure never goes unnoticed
    try:
        push_fn = current_app.config.get("LINE_PUSH_FN")
        ids = get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")
        if push_fn and ids:
            if res.get("error"):
                msg = f"⚠️ สำเนาบันทึก HD {day.isoformat()} ส่งไม่ได้\n{res['error']}"
            elif res["failed"]:
                msg = (f"⚠️ สำเนาบันทึก HD {day.isoformat()}: สำเร็จ {res['ok']} ไฟล์ ล้มเหลว {res['failed']} ไฟล์\n"
                       f"gs://{bucket_name}/{res['folder']}")
            else:
                drive_line = ""
                if res.get("drive_enabled"):
                    drive_line = (f"\n📁 Google Drive: {res.get('drive_ok', 0)} ไฟล์"
                                  + (f" (ล้มเหลว {res['drive_failed']})" if res.get("drive_failed") else " ✓"))
                msg = (f"📦 สำเนาบันทึก HD {day.isoformat()} เรียบร้อย {res['ok']} ไฟล์\n"
                       f"gs://{bucket_name}/{res['folder']}{drive_line}\n"
                       f"({'อัตโนมัติ 22:30' if trigger == 'scheduled' else 'กดส่งเอง'})")
            for uid in [r.strip() for r in ids.split(",") if r.strip()]:
                push_fn(uid, msg)
    except Exception as e:
        logger.warning("daily export LINE notify failed: %s", e)
    return res


def run_morning_brief(trigger="manual", user=None):
    """Build the physician Morning Brief and push it to alert_recipients."""
    import os
    import psycopg2
    from flask import current_app
    from .morning_brief import send_morning_brief
    push_fn = current_app.config.get("LINE_PUSH_FN")
    ids = get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")
    recipients = [r.strip() for r in ids.split(",") if r.strip()]
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return {"sent": 0, "error": "no database"}
    base_url = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("BASE_URL")
                or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        sent, msg = send_morning_brief(conn, push_fn, recipients if push_fn else [], base_url)
    finally:
        conn.close()
    log_action(user_id=user, action="morning_brief.push", resource_type="morning_brief",
               new_values={"sent": sent, "recipients": len(recipients), "trigger": trigger}, ip_address="")
    return {"sent": sent, "recipients": len(recipients), "trigger": trigger, "message": msg,
            "error": None if (push_fn and recipients) else "LINE ไม่พร้อม หรือยังไม่ตั้ง alert_recipients"}


@hd_bp.route("/morning-brief/push", methods=["POST"])
@require_role("physician", "admin")
def morning_brief_push_now():
    """Send the Morning Brief to the physician now (same text as the 07:30 job)."""
    try:
        res = run_morning_brief(trigger="manual", user=g.get("hd_user_id"))
    except Exception as e:
        logger.error("morning brief push error: %s", e)
        return jsonify({"sent": 0, "error": str(e)}), 500
    return jsonify(res), (200 if not res.get("error") else 503)


def _set_setting(key, value, user_id=None, description=""):
    import os
    import psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return False
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO hd_settings (key, value, description, updated_at, updated_by)
                VALUES (%s, %s, %s, NOW(), %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                    updated_at = NOW(), updated_by = EXCLUDED.updated_by
            """, (key, value, description, user_id))
        conn.commit()
        return True
    finally:
        conn.close()


def _wire_drive_token():
    """Let google_drive_sync read the physician's OAuth refresh token from hd_settings."""
    try:
        import google_drive_sync as gds
        if gds._refresh_token_getter is None:
            gds.set_refresh_token_getter(lambda: get_setting_value("drive_refresh_token", ""))
    except Exception as e:
        logger.warning("wire drive token: %s", e)


def _drive_oauth_redirect_uri():
    import os
    base = (os.environ.get("RENDER_EXTERNAL_URL") or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
    return f"{base}/hd/api/v1/drive/oauth/callback"


@hd_bp.route("/drive/status", methods=["GET"])
@require_role("physician", "admin")
def drive_status():
    """Is Google Drive export going to work? (mode, folder, OAuth connected?)"""
    try:
        import google_drive_sync as gds
        _wire_drive_token()
        info = {"enabled": gds.DRIVE_ENABLED, "folder_id_set": bool(gds.DRIVE_FOLDER_ID),
                "oauth_client_configured": gds.oauth_configured(),
                "connected_as_user": bool(gds.oauth_configured() and gds._oauth_refresh_token()),
                "mode": gds.drive_mode(), "redirect_uri": _drive_oauth_redirect_uri()}
        if info["connected_as_user"]:
            st = gds.get_status()
            info["folder_name"] = st.get("folder_name")
            info["ok"] = bool(st.get("connected"))
            info["error"] = st.get("error")
        else:
            info["ok"] = False
            info["error"] = ("ยังไม่ได้ตั้ง GOOGLE_DRIVE_OAUTH_CLIENT_ID/SECRET บน Render" if not info["oauth_client_configured"]
                             else "ยังไม่ได้เชื่อมบัญชี Google ของหมอ — กดปุ่มเชื่อม")
        return jsonify(info)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@hd_bp.route("/drive/oauth/start", methods=["GET"])
@require_role("physician", "admin")
def drive_oauth_start():
    """Redirect the physician to Google consent (scope drive.file only)."""
    import urllib.parse
    import google_drive_sync as gds
    if not gds.oauth_configured():
        return jsonify({"error": "GOOGLE_DRIVE_OAUTH_CLIENT_ID / SECRET ยังไม่ได้ตั้งบน Render"}), 400
    import jwt as pyjwt
    from .config import HdConfig
    from datetime import datetime, timedelta, timezone
    state = pyjwt.encode({"uid": g.get("hd_user_id"), "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
                          "purpose": "drive_oauth"}, HdConfig.JWT_SECRET, algorithm=HdConfig.JWT_ALGORITHM)
    params = {
        "client_id": gds.OAUTH_CLIENT_ID, "redirect_uri": _drive_oauth_redirect_uri(),
        "response_type": "code", "scope": " ".join(gds.OAUTH_SCOPES),
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state,
    }
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params))


@hd_bp.route("/drive/oauth/callback", methods=["GET"])
def drive_oauth_callback():
    """Google sends the code here; exchange for a refresh token and keep it in hd_settings."""
    import requests as req
    import jwt as pyjwt
    import google_drive_sync as gds
    from .config import HdConfig
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    err = request.args.get("error", "")
    if err:
        return redirect(f"/hd/settings?drive=error&msg={err}")
    try:
        claims = pyjwt.decode(state, HdConfig.JWT_SECRET, algorithms=[HdConfig.JWT_ALGORITHM])
        if claims.get("purpose") != "drive_oauth":
            raise ValueError("bad state")
    except Exception:
        return redirect("/hd/settings?drive=error&msg=state")
    try:
        r = req.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": gds.OAUTH_CLIENT_ID, "client_secret": gds.OAUTH_CLIENT_SECRET,
            "redirect_uri": _drive_oauth_redirect_uri(), "grant_type": "authorization_code",
        }, timeout=15)
        tok = r.json()
        rt = tok.get("refresh_token")
        if not rt:
            logger.warning("drive oauth: no refresh_token in response (%s)", list(tok.keys()))
            return redirect("/hd/settings?drive=error&msg=no_refresh_token")
        _set_setting("drive_refresh_token", rt, claims.get("uid"), "Google Drive OAuth refresh token (physician account)")
        gds.set_refresh_token_getter(lambda: get_setting_value("drive_refresh_token", ""))
        log_action(user_id=claims.get("uid"), action="drive.oauth_connected", resource_type="settings", ip_address=request.remote_addr or "")
        return redirect("/hd/settings?drive=connected")
    except Exception as e:
        logger.error("drive oauth callback: %s", e)
        return redirect("/hd/settings?drive=error&msg=exchange")


@hd_bp.route("/drive/disconnect", methods=["POST"])
@require_role("physician", "admin")
def drive_disconnect():
    import google_drive_sync as gds
    _set_setting("drive_refresh_token", "", g.get("hd_user_id"))
    gds.reset_service()
    return jsonify({"ok": True})


# ── B12: orphan files in hd_sop_files ─────────────────────

def cleanup_orphan_files(conn):
    """Delete hd_sop_files rows whose owner record no longer exists:
    water_quality_<id> with no hd_water_quality row. Returns count."""
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM hd_sop_files f
            WHERE f.sop_key LIKE 'water_quality_%%'
              AND NOT EXISTS (
                  SELECT 1 FROM hd_water_quality w
                  WHERE w.id::text = SUBSTRING(f.sop_key FROM 15))
        """)
        n = cur.rowcount
    conn.commit()
    return n


@hd_bp.route("/clinic-profile/orphan-files", methods=["GET", "POST"])
@require_role("physician", "admin")
def orphan_files():
    """GET = count + list; POST = delete them."""
    import os
    import psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        if request.method == "POST":
            n = cleanup_orphan_files(conn)
            log_action(user_id=g.get("hd_user_id"), action="sop_files.cleanup", resource_type="sop_files",
                       new_values={"removed": n}, ip_address=request.remote_addr or "")
            return jsonify({"ok": True, "removed": n})
        with conn.cursor() as cur:
            cur.execute("""
                SELECT f.id, f.sop_key, f.filename, f.file_size, f.uploaded_at FROM hd_sop_files f
                WHERE f.sop_key LIKE 'water_quality_%%'
                  AND NOT EXISTS (SELECT 1 FROM hd_water_quality w WHERE w.id::text = SUBSTRING(f.sop_key FROM 15))
                ORDER BY f.id DESC LIMIT 200""")
            rows = [{"id": r[0], "sop_key": r[1], "filename": r[2], "size": r[3],
                     "uploaded_at": (r[4].isoformat() + "Z") if r[4] else None} for r in cur.fetchall()]
        return jsonify({"count": len(rows), "files": rows})
    finally:
        conn.close()


@hd_bp.route("/sessions/<int:session_id>/idh-risk", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def session_idh_risk(session_id):
    import os
    import psycopg2
    from .idh_predictor import features_for, score as idh_score
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    conn = psycopg2.connect(db_url, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            f = features_for(cur, session_id)
    finally:
        conn.close()
    if not f:
        return jsonify({"error": "not found"}), 404
    # ว้าว รอบ 2 #5: "ถ้า…จะ…" simulator — override planned inputs (nothing is saved)
    simulated = False
    for k in ("uf_goal", "duration_hours", "pre_weight"):
        v = request.args.get(k)
        if v not in (None, ""):
            try:
                f[k] = float(v)
                simulated = True
            except ValueError:
                pass
    out = idh_score(f)
    out["simulated"] = simulated
    out["features"] = {k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                       for k, v in f.items() if k != "sbp_series"}
    out["features"]["sbp_series"] = f.get("sbp_series")
    return jsonify(out)


# ── สรุปผลเลือดรายเดือน (Monthly Lab Summary Push) ──────────────

@hd_bp.route("/monthly-summary/run", methods=["POST"])
@require_role("physician", "admin")
def run_monthly_summary():
    """Manual trigger for monthly lab summary push."""
    import os, psycopg2
    from .monthly_summary import run_monthly_push
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no database"}), 500
    push_fn = current_app.config.get("LINE_PUSH_FN")
    if not push_fn:
        return jsonify({"error": "LINE_PUSH_FN not configured"}), 500

    def _resolve(hn):
        return _resolve_patient_line_id(hn)

    try:
        conn = psycopg2.connect(db_url, connect_timeout=10)
        try:
            result = run_monthly_push(conn, push_fn, _resolve, get_setting_value, log_action)
        finally:
            conn.close()
        return jsonify(result)
    except Exception as e:
        logger.warning("monthly_summary run: %s", e)
        return jsonify({"error": str(e)}), 500


# ── ตรวจระบบน้ำ RO ประจำวัน (Hardness / Free Cl / Total Cl) ──

@hd_bp.route("/water-ro", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def water_ro_list():
    import os
    import psycopg2
    from .water_check import CREATE_SQL, limits_from
    days = min(int(request.args.get("days", 120) or 120), 730)
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            conn.commit()
            cur.execute("""SELECT id, check_date, shift, sample_point, hardness_ppm, hardness_text, free_cl, total_cl, status,
                                  fails, tester_name, source, note, photos, drive, created_at, raw_text
                           FROM hd_water_daily WHERE check_date >= CURRENT_DATE - %s AND deleted_at IS NULL
                           ORDER BY check_date DESC, id DESC""", (days,))
            items = [{"id": r[0], "date": r[1].isoformat(), "shift": r[2], "sample_point": r[3],
                      "hardness": float(r[4]) if r[4] is not None else None, "hardness_text": r[5],
                      "free_cl": float(r[6]) if r[6] is not None else None, "total_cl": float(r[7]) if r[7] is not None else None,
                      "status": r[8], "fails": r[9], "tester": r[10], "source": r[11], "note": r[12],
                      "photos": r[13] or [], "drive": r[14] or [], "created_at": (r[15].isoformat() + "Z") if r[15] else None,
                      "raw_text": r[16]} for r in cur.fetchall()]
    finally:
        conn.close()
    return jsonify({"items": items, "limits": limits_from(get_setting_value), "days": days})


@hd_bp.route("/water-ro", methods=["POST"])
@require_role("physician", "nurse")
def water_ro_add():
    """Entry from the web page: JSON, or multipart with up to 8 photos (phone camera/gallery).
    Photos go to GCS + Drive like the LINE flow; the result is pushed to the nurse group."""
    import os
    import psycopg2
    from datetime import date as _d
    from flask import current_app
    from .water_check import evaluate, limits_from, save_record, archive, reply_text
    photos = []
    if request.content_type and request.content_type.startswith("multipart/"):
        data = {k: request.form.get(k) for k in request.form}
        data["hardness_gt"] = (request.form.get("hardness_gt") or "").lower() in ("1", "true", "on")
        for f in request.files.getlist("photos")[:8]:
            blob = f.read()
            if blob and len(blob) <= 8 * 1024 * 1024 and (f.mimetype or "").startswith("image/"):
                photos.append(blob)
    else:
        data = request.get_json(silent=True) or {}
    vals = {"hardness": data.get("hardness"), "hardness_gt": bool(data.get("hardness_gt")), "free": data.get("free_cl"),
            "total": data.get("total_cl"), "note": (data.get("note") or "")[:200], "point": data.get("sample_point")}
    for k in ("hardness", "free", "total"):
        if vals[k] in ("", None):
            vals[k] = None
        else:
            try:
                vals[k] = float(vals[k])
            except (TypeError, ValueError):
                return jsonify({"error": f"{k} ต้องเป็นตัวเลข"}), 400
    if vals["hardness"] is None and vals["free"] is None and vals["total"] is None:
        return jsonify({"error": "ต้องระบุอย่างน้อย 1 ค่า"}), 400
    try:
        cd = _d.fromisoformat(data["date"]) if data.get("date") else None
    except ValueError:
        return jsonify({"error": "วันที่ไม่ถูกต้อง"}), 400
    lim = limits_from(get_setting_value)
    ev = evaluate(vals, lim)
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        rec = save_record(conn, vals, ev, tester_name=g.get("hd_username") or "", tester_hash=None, raw_text="web",
                          source="web", sample_point=vals["point"] or lim["water_sample_point"], check_date=cd,
                          shift=data.get("shift"))
        drive_up = None
        try:
            from .daily_records import drive_upload_fn
            drive_up = drive_upload_fn()
        except Exception:
            pass
        stored, drive_out = archive(conn, current_app.config.get("STORAGE_OBJ"), rec, vals, ev, drive_upload=drive_up,
                                    tester_name=g.get("hd_username") or "", extra_photos=photos)
    finally:
        conn.close()
    log_action(user_id=g.get("hd_user_id"), action="water.check_recorded", resource_type="water", resource_id=rec["id"],
               new_values={"status": ev["status"], "fails": ev["fails"], "source": "web", "photos": len(photos),
                           "drive": len(drive_out)}, ip_address=request.remote_addr or "")
    # LINE: nurse group always (so the record is visible in the OA/group); physician too when failing
    msg = reply_text(rec, ev, g.get("hd_username") or "", len(photos), bool(drive_out)) + "\n(บันทึกจากหน้าเว็บ — ดูรูปที่ /hd/water-ro)"
    try:
        if ev["status"] == "fail":
            push_nurse_group("🚨 ตรวจน้ำ RO ไม่ผ่านเกณฑ์\n" + msg, fallback_recipients=True)
            ids = get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")
            push_fn = current_app.config.get("LINE_PUSH_FN")
            for uid in [x.strip() for x in ids.split(",") if x.strip()]:
                if push_fn:
                    push_fn(uid, "🚨 ตรวจน้ำ RO ไม่ผ่านเกณฑ์\n" + msg)
        else:
            push_nurse_group(msg, fallback_recipients=False)
    except Exception as e:
        logger.warning("water web push: %s", e)
    return jsonify({"ok": True, "id": rec["id"], "status": ev["status"], "fails": ev["fails"], "lines": ev["lines"],
                    "photos": len(photos), "drive": len(drive_out)})


@hd_bp.route("/water-ro/<int:rid>/photos", methods=["POST"])
@require_role("physician", "nurse")
def water_ro_add_photos(rid):
    """Add up to 8 more photos to an existing record (multipart 'photos')."""
    from flask import current_app
    from .water_check import add_photos
    import os
    import psycopg2
    blobs = []
    for f in request.files.getlist("photos")[:8]:
        b = f.read()
        if b and len(b) <= 8 * 1024 * 1024 and (f.mimetype or "").startswith("image/"):
            blobs.append(b)
    if not blobs:
        return jsonify({"error": "ไม่มีรูป"}), 400
    drive_up = None
    try:
        from .daily_records import drive_upload_fn
        drive_up = drive_upload_fn()
    except Exception:
        pass
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        photos = add_photos(conn, current_app.config.get("STORAGE_OBJ"), rid, blobs, drive_upload=drive_up)
    finally:
        conn.close()
    if photos is None:
        return jsonify({"error": "not found"}), 404
    log_action(user_id=g.get("hd_user_id"), action="water.photos_added", resource_type="water", resource_id=rid,
               new_values={"added": len(blobs)}, ip_address=request.remote_addr or "")
    return jsonify({"ok": True, "photos": sum(1 for p in photos if p.endswith(".jpg"))})


@hd_bp.route("/water-ro/<int:rid>/photo/<int:idx>", methods=["DELETE"])
@require_role("physician", "nurse")
def water_ro_remove_photo(rid, idx):
    """Unlink one photo from the record. The file is kept (ห้ามลบไฟล์ต้นฉบับ) — only hidden from the record."""
    from .water_check import remove_photo
    import os
    import psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        r = remove_photo(conn, rid, idx)
    finally:
        conn.close()
    if r is None:
        return jsonify({"error": "not found"}), 404
    if r is False:
        return jsonify({"error": "ไม่มีรูปนี้"}), 404
    log_action(user_id=g.get("hd_user_id"), action="water.photo_removed", resource_type="water", resource_id=rid,
               new_values={"idx": idx}, ip_address=request.remote_addr or "")
    return jsonify({"ok": True})


@hd_bp.route("/water-ro/<int:rid>", methods=["DELETE"])
@require_role("physician", "nurse")
def water_ro_delete(rid):
    """Soft-delete a record (hidden from lists/brief; row and files are kept for audit)."""
    import os
    import psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE hd_water_daily SET deleted_at = NOW(), deleted_by = %s WHERE id = %s AND deleted_at IS NULL RETURNING id",
                        (g.get("hd_username") or str(g.get("hd_user_id") or ""), rid))
            ok = cur.fetchone() is not None
        conn.commit()
    finally:
        conn.close()
    if not ok:
        return jsonify({"error": "not found"}), 404
    log_action(user_id=g.get("hd_user_id"), action="water.record_deleted", resource_type="water", resource_id=rid,
               ip_address=request.remote_addr or "")
    return jsonify({"ok": True})


@hd_bp.route("/water-ro/<int:rid>/photo/<int:idx>", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def water_ro_photo(rid, idx):
    """Serve one archived photo through the app (signed-in staff only; no public URL)."""
    import os
    import psycopg2
    from flask import Response, current_app
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT photos FROM hd_water_daily WHERE id = %s", (rid,))
            r = cur.fetchone()
    finally:
        conn.close()
    photos = [p for p in (r[0] if r else []) if p.endswith(".jpg")]
    if idx < 0 or idx >= len(photos):
        return jsonify({"error": "not found"}), 404
    bucket = getattr(current_app.config.get("STORAGE_OBJ"), "_bucket", None)
    if bucket is None:
        return jsonify({"error": "no storage"}), 503
    data = bucket.blob(photos[idx]).download_as_bytes()
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=600"})


# ── ว้าว #5: Patient 360 timeline ──

@hd_bp.route("/patients/<int:patient_id>/timeline", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def patient_timeline(patient_id):
    import os
    import psycopg2
    from .patient360 import build
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    days = min(int(request.args.get("days", 365) or 365), 1095)
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        data = build(conn, patient_id, days=days)
    finally:
        conn.close()
    if data is None:
        return jsonify({"error": "not found"}), 404
    log_action(user_id=g.get("hd_user_id"), action="patient.timeline_view", resource_type="patient",
               resource_id=patient_id, ip_address=request.remote_addr or "")
    return jsonify(data)


@hd_bp.route("/patients/by-hn/<hn>/id", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def patient_id_by_hn(hn):
    import os
    import psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM hd_patient_mapping WHERE UPPER(hn) = %s", (hn.strip().upper(),))
            r = cur.fetchone()
    finally:
        conn.close()
    if not r:
        return jsonify({"error": "not found"}), 404
    return jsonify({"id": r[0]})


# ── ว้าว #2: VA Watch (vascular access surveillance from flowsheet) ──

@hd_bp.route("/va-watch", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def va_watch_list():
    import os
    import psycopg2
    from .va_watch import collect
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"items": []})
    days = min(int(request.args.get("days", 120) or 120), 365)
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        items = collect(conn, days=days)
    finally:
        conn.close()
    return jsonify({"items": items, "days": days,
                    "counts": {lv: sum(1 for i in items if i["level"] == lv) for lv in ("alert", "watch", "ok")}})


# ── ว้าว #3: ESA Autopilot (propose → physician approves) ──

def _esa_protocol():
    from .esa_autopilot import DEFAULTS
    out = {}
    for k, dv in DEFAULTS.items():
        v = get_setting_value(k, "")
        try:
            out[k] = float(v) if v not in ("", None) else dv
        except ValueError:
            out[k] = dv
    return out


def run_esa_autopilot(trigger="scheduled"):
    """Generate proposals from new Hb values and LINE the physician once per batch."""
    import os
    import psycopg2
    from flask import current_app
    from .esa_autopilot import generate, line_summary
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return {"created": 0}
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        created = generate(conn, _esa_protocol())
    finally:
        conn.close()
    sent = 0
    if created:
        push_fn = current_app.config.get("LINE_PUSH_FN")
        ids = get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")
        base = (os.environ.get("RENDER_EXTERNAL_URL") or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
        msg = line_summary(created, base)
        for uid in [r.strip() for r in ids.split(",") if r.strip()]:
            try:
                if push_fn and push_fn(uid, msg) is not False:
                    sent += 1
            except Exception as e:
                logger.warning("esa autopilot push: %s", e)
        log_action(user_id=None, action="esa.proposals_created", resource_type="esa",
                   new_values={"count": len(created), "trigger": trigger, "line_sent": sent}, ip_address="")
    return {"created": len(created), "line_sent": sent, "items": created}


@hd_bp.route("/esa/proposals", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def esa_proposals():
    import os
    import psycopg2
    from .esa_autopilot import CREATE_SQL
    status = request.args.get("status", "pending")
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"items": [], "protocol": _esa_protocol()})
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            conn.commit()
            q = """SELECT e.id, e.patient_id, e.hn, p.patient_name, e.hb, e.hb_date, e.hb_source, e.current_drug,
                          e.current_dose, e.action, e.proposed_dose, e.rationale, e.status, e.created_at,
                          e.decided_by, e.decided_at, e.note, p.esa_drug, p.esa_dose
                   FROM hd_esa_proposals e JOIN hd_patient_mapping p ON p.id = e.patient_id"""
            if status == "all":
                cur.execute(q + " ORDER BY e.id DESC LIMIT 200")
            else:
                cur.execute(q + " WHERE e.status = %s ORDER BY e.hb ASC, e.id DESC LIMIT 200", (status,))
            items = []
            for r in cur.fetchall():
                items.append({"id": r[0], "patient_id": r[1], "hn": r[2], "name": r[3], "hb": float(r[4]),
                              "hb_date": r[5].isoformat() if r[5] else None, "hb_source": r[6],
                              "current_drug": r[7], "current_dose": r[8], "action": r[9], "proposed_dose": r[10],
                              "rationale": r[11], "status": r[12],
                              "created_at": (r[13].isoformat() + "Z") if r[13] else None,
                              "decided_by": r[14], "decided_at": (r[15].isoformat() + "Z") if r[15] else None,
                              "note": r[16], "now_drug": r[17], "now_dose": r[18]})
    finally:
        conn.close()
    return jsonify({"items": items, "protocol": _esa_protocol()})


@hd_bp.route("/esa/proposals/<int:pid>/decide", methods=["POST"])
@require_role("physician")
def esa_decide(pid):
    """Physician approves (applies the dose to the patient record) or rejects."""
    import os
    import psycopg2
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")
    note = (data.get("note") or "").strip()[:300]
    override_dose = (data.get("dose") or "").strip()[:50]
    if decision not in ("approve", "reject"):
        return jsonify({"error": "decision must be approve|reject"}), 400
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    who = g.get("hd_username") or str(g.get("hd_user_id") or "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT patient_id, action, proposed_dose, current_dose, status, hn FROM hd_esa_proposals WHERE id = %s", (pid,))
            r = cur.fetchone()
            if not r:
                return jsonify({"error": "not found"}), 404
            if r[4] != "pending":
                return jsonify({"error": f"already {r[4]}"}), 409
            patient_id, action, proposed, current, _, hn = r
            applied = None
            if decision == "approve":
                new_dose = override_dose or proposed
                if action == "hold":
                    cur.execute("UPDATE hd_patient_mapping SET esa_dose = %s, updated_at = NOW() WHERE id = %s",
                                (f"งดชั่วคราว (เดิม {current or '-'})", patient_id))
                    applied = "hold"
                elif action in ("increase", "decrease", "resume") and new_dose:
                    cur.execute("UPDATE hd_patient_mapping SET esa_dose = %s, updated_at = NOW() WHERE id = %s",
                                (new_dose, patient_id))
                    applied = new_dose
                elif action == "review" and new_dose:
                    cur.execute("UPDATE hd_patient_mapping SET esa_dose = %s, updated_at = NOW() WHERE id = %s",
                                (new_dose, patient_id))
                    applied = new_dose
                elif action == "iron":
                    applied = "iron"          # ESA dose unchanged; physician orders iron
            cur.execute("""UPDATE hd_esa_proposals SET status = %s, decided_by = %s, decided_at = NOW(), note = %s,
                           proposed_dose = COALESCE(NULLIF(%s, ''), proposed_dose) WHERE id = %s""",
                        ("approved" if decision == "approve" else "rejected", who, note, override_dose, pid))
        conn.commit()
    finally:
        conn.close()
    log_action(user_id=g.get("hd_user_id"), action=f"esa.{decision}", resource_type="esa", resource_id=pid,
               new_values={"hn": hn, "action": action, "applied": applied, "note": note}, ip_address=request.remote_addr or "")
    # tell the nurse group so the next session uses the new dose
    if decision == "approve" and applied:
        try:
            cur_txt = (f"ให้เหล็ก IV ก่อน (ESA คงเดิม): HN {hn}" if applied == "iron" else
                       f"ESA ปรับแล้ว: HN {hn} → {'งด ESA ชั่วคราว' if applied == 'hold' else applied}") + (f" ({note})" if note else "")
            push_nurse_group(f"💊 {cur_txt}\nโดย {who} — ใช้ขนาดใหม่ตั้งแต่รอบถัดไป", fallback_recipients=False)
        except Exception as e:
            logger.warning("esa nurse push: %s", e)
    return jsonify({"ok": True, "decision": decision, "applied": applied})


@hd_bp.route("/esa/run", methods=["POST"])
@require_role("physician", "admin")
def esa_run_now():
    try:
        return jsonify(run_esa_autopilot(trigger="manual"))
    except Exception as e:
        logger.error("esa run: %s", e)
        return jsonify({"error": str(e)}), 500


# ── ว้าว รอบ 2 #3: Smart Bed Scheduler ──

def _sched_ctx(conn):
    from .bed_scheduler import load_context
    # คลินิก: เตียงโซน HBV = เตียง 28 เตียงเดียว (เปลี่ยน 18 พ.ย. 2569) — เปลี่ยนได้ใน ⚙️ hbv_beds
    return load_context(conn, HdConfig.TOTAL_BEDS, get_setting_value("hbv_beds", "") or "28", get_setting_value("hcv_beds", ""))


@hd_bp.route("/scheduler/week", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def scheduler_week():
    import os
    import psycopg2
    from datetime import date as _d
    from .bed_scheduler import plan_week
    start = request.args.get("start")
    try:
        start = _d.fromisoformat(start) if start else _thai_today()
    except ValueError:
        return jsonify({"error": "start ไม่ถูกต้อง"}), 400
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        ctx = _sched_ctx(conn)
        plan = plan_week(ctx, start)
        # mark beds that already have a scheduled/in-progress session (so the UI shows "already booked")
        with conn.cursor() as cur:
            cur.execute("""SELECT session_date, COALESCE(shift,'morning'), bed_number, p.patient_name, s.status FROM hd_sessions s
                           JOIN hd_patient_mapping p ON p.id = s.patient_id
                           WHERE session_date BETWEEN %s AND %s AND status IN ('scheduled','in_progress','completed')""",
                        (start.isoformat(), (start + timedelta(days=6)).isoformat()))
            booked = {}
            for d, sh, bed, name, st in cur.fetchall():
                booked.setdefault(d.isoformat(), {}).setdefault(sh, {})[bed] = {"name": name, "status": st}
        plan["booked"] = booked
        plan["patients_detail"] = [{"id": p["id"], "hn": p["hn"], "name": p["name"], "hbv": p["hbv"], "hcv": p["hcv"],
                                    "usual_shift": p["usual_shift"], "usual_bed": p["usual_bed"], "days": p["days"]} for p in ctx["patients"]]
        plan["bad_beds"] = ctx["bad_beds"]
    finally:
        conn.close()
    return jsonify(plan)


@hd_bp.route("/scheduler/check", methods=["POST"])
@require_role("physician", "nurse", "admin")
def scheduler_check():
    """{patient_id, bed} → warnings (drag/drop validation)."""
    import os
    import psycopg2
    from .bed_scheduler import check_bed
    data = request.get_json(silent=True) or {}
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        ctx = _sched_ctx(conn)
    finally:
        conn.close()
    p = next((x for x in ctx["patients"] if x["id"] == data.get("patient_id")), None)
    if not p:
        return jsonify({"error": "patient not found"}), 404
    try:
        bed = int(data.get("bed"))
    except (TypeError, ValueError):
        return jsonify({"error": "bed"}), 400
    # does an HBsAg+ patient dialyse in that date+shift? (planned ones + the one being moved)
    hbv_in_shift = True
    try:
        from datetime import date as _d
        from .bed_scheduler import plan_day
        if data.get("date") and data.get("shift"):
            dp = plan_day(ctx, _d.fromisoformat(data["date"]))
            hbv_in_shift = any(a["hbv"] for a in dp["shifts"].get(data["shift"], [])) or p["hbv"]
    except Exception:
        pass
    return jsonify({"warnings": check_bed(ctx, p, bed, hbv_in_shift=hbv_in_shift),
                    "patient": {"name": p["name"], "hbv": p["hbv"], "hcv": p["hcv"]}})


@hd_bp.route("/scheduler/apply", methods=["POST"])
@require_role("physician", "nurse", "admin")
def scheduler_apply():
    """Create scheduled sessions for one date + shift from the plan (with optional manual overrides {bed: patient_id})."""
    import os
    import psycopg2
    from datetime import date as _d
    from .bed_scheduler import apply_day, check_bed, plan_day
    data = request.get_json(silent=True) or {}
    try:
        day = _d.fromisoformat(data.get("date"))
    except (TypeError, ValueError):
        return jsonify({"error": "date"}), 400
    shift = data.get("shift", "morning")
    if shift not in ("morning", "afternoon", "evening"):
        return jsonify({"error": "shift"}), 400
    overrides = {int(k): int(v) for k, v in (data.get("overrides") or {}).items()}
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        ctx = _sched_ctx(conn)
        plan = plan_day(ctx, day)
        if overrides:
            by_id = {p["id"]: p for p in ctx["patients"]}
            rows = [a for a in plan["shifts"][shift] if a["patient_id"] not in overrides.values() and a["bed"] not in overrides]
            hbv_in_shift = any(a["hbv"] for a in rows) or any(by_id[pid]["hbv"] for pid in overrides.values() if pid in by_id)
            for bed, pid in overrides.items():
                p = by_id.get(pid)
                if p:
                    rows.append({"bed": bed, "patient_id": pid, "hn": p["hn"], "name": p["name"], "hbv": p["hbv"], "hcv": p["hcv"],
                                 "warnings": check_bed(ctx, p, bed, hbv_in_shift=hbv_in_shift)})
            plan["shifts"][shift] = sorted(rows, key=lambda a: a["bed"])
        res = apply_day(conn, plan, shift, nurse_id=g.get("hd_user_id"))
    finally:
        conn.close()
    log_action(user_id=g.get("hd_user_id"), action="scheduler.apply", resource_type="schedule", resource_id=None,
               new_values={"date": day.isoformat(), "shift": shift, **res, "overrides": len(overrides)}, ip_address=request.remote_addr or "")
    return jsonify({"ok": True, **res})


@hd_bp.route("/scheduler/push", methods=["POST"])
@require_role("physician", "nurse", "admin")
def scheduler_push():
    """Send one day's plan to the nurse group."""
    import os
    import psycopg2
    from datetime import date as _d
    from .bed_scheduler import line_text, plan_day
    data = request.get_json(silent=True) or {}
    try:
        day = _d.fromisoformat(data.get("date")) if data.get("date") else _thai_today() + timedelta(days=1)
    except ValueError:
        return jsonify({"error": "date"}), 400
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        plan = plan_day(_sched_ctx(conn), day)
    finally:
        conn.close()
    base = (os.environ.get("RENDER_EXTERNAL_URL") or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
    ok = push_nurse_group(line_text(plan, base), fallback_recipients=True)
    return jsonify({"ok": bool(ok), "date": day.isoformat()})


def push_tomorrow_schedule():
    """18:00 tick: tomorrow's bed plan → nurse group (skipped when nobody is scheduled)."""
    import os
    import psycopg2
    from .bed_scheduler import line_text, plan_day
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url or (get_setting_value("scheduler_push_1800", "on") or "on").lower() == "off":
        return False
    day = _thai_today() + timedelta(days=1)
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        plan = plan_day(_sched_ctx(conn), day)
    finally:
        conn.close()
    if not any(plan["shifts"].values()):
        return False
    base = (os.environ.get("RENDER_EXTERNAL_URL") or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
    return push_nurse_group(line_text(plan, base), fallback_recipients=False)


# ── ว้าว รอบ 2 #2: Adequacy Guardian + monthly quality report ──

def _ym(arg):
    from datetime import date as _d
    today = (datetime.now(timezone.utc) + timedelta(hours=7)).date()
    if arg:
        y, m = arg.split("-")[:2]
        return int(y), int(m)
    prev = (today.replace(day=1) - timedelta(days=1))
    return prev.year, prev.month


def run_quality_report(year=None, month=None, trigger="scheduled", send_line=True):
    """Build the month's quality report → GCS reports/quality + Drive 'รายงานคุณภาพ' + LINE summary."""
    import os
    import psycopg2
    from flask import current_app
    from .quality_report import collect_month, line_text, pdf_bytes
    if year is None or month is None:
        year, month = _ym(None)
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        D = collect_month(conn, year, month)
    finally:
        conn.close()
    pdf = pdf_bytes(D)
    name = f"HD-Quality-{year}-{month:02d}.pdf"
    stored, drive_ok = [], False
    try:
        bucket = getattr(current_app.config.get("STORAGE_OBJ"), "_bucket", None)
        if bucket is not None:
            gp = f"reports/quality/{year}/{name}"
            try:
                bucket.blob(gp).upload_from_string(pdf, content_type="application/pdf")
            except Exception:
                gp = f"reports/quality/{year}/{name[:-4]}.v{datetime.now(timezone.utc).strftime('%H%M')}.pdf"
                bucket.blob(gp).upload_from_string(pdf, content_type="application/pdf")
            stored.append(gp)
    except Exception as e:
        logger.warning("quality report gcs: %s", e)
    try:
        from .daily_records import drive_upload_fn
        up = drive_upload_fn()
        if up:
            up(f"รายงานคุณภาพ/{year}/{name}", pdf, "application/pdf")
            drive_ok = True
    except Exception as e:
        logger.warning("quality report drive: %s", str(e)[:80])
    sent = 0
    if send_line:
        base = (os.environ.get("RENDER_EXTERNAL_URL") or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
        msg = line_text(D, base)
        push_fn = current_app.config.get("LINE_PUSH_FN")
        ids = get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")
        for uid in [r.strip() for r in ids.split(",") if r.strip()]:
            try:
                if push_fn and push_fn(uid, msg) is not False:
                    sent += 1
            except Exception as e:
                logger.warning("quality report push: %s", e)
    log_action(user_id=None, action="quality.report_generated", resource_type="report", resource_id=None,
               new_values={"month": f"{year}-{month:02d}", "trigger": trigger, "gcs": stored, "drive": drive_ok, "line_sent": sent}, ip_address="")
    return {"month": f"{year}-{month:02d}", "gcs": stored, "drive": drive_ok, "line_sent": sent, "data": D}


@hd_bp.route("/adequacy", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def adequacy_list():
    import os
    import psycopg2
    from .quality_report import adequacy
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        items = adequacy(conn, min(int(request.args.get("days", 90) or 90), 365))
    finally:
        conn.close()
    return jsonify({"items": items, "counts": {"low": sum(1 for i in items if i["adequate"] is False),
                                              "ok": sum(1 for i in items if i["adequate"] is True),
                                              "unknown": sum(1 for i in items if i["adequate"] is None),
                                              "measured": sum(1 for i in items if i["source"] == "measured")}})


@hd_bp.route("/quality-report", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def quality_report_json():
    import os
    import psycopg2
    from .quality_report import collect_month, line_text
    y, m = _ym(request.args.get("month"))
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        D = collect_month(conn, y, m)
    finally:
        conn.close()
    D["text"] = line_text(D, "")
    return jsonify(D)


@hd_bp.route("/quality-report.pdf", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def quality_report_pdf():
    import os
    import psycopg2
    from flask import Response
    from .quality_report import collect_month, pdf_bytes
    y, m = _ym(request.args.get("month"))
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        D = collect_month(conn, y, m)
    finally:
        conn.close()
    log_action(user_id=g.get("hd_user_id"), action="quality.report_pdf", resource_type="report", resource_id=None,
               new_values={"month": f"{y}-{m:02d}"}, ip_address=request.remote_addr or "")
    return Response(pdf_bytes(D), mimetype="application/pdf",
                    headers={"Content-Disposition": _content_disposition(f"HD-Quality-{y}-{m:02d}.pdf")})


@hd_bp.route("/quality-report/send", methods=["POST"])
@require_role("physician", "admin")
def quality_report_send():
    y, m = _ym((request.get_json(silent=True) or {}).get("month"))
    try:
        r = run_quality_report(y, m, trigger="manual")
        r.pop("data", None)
        return jsonify(r)
    except Exception as e:
        logger.error("quality report: %s", e)
        return jsonify({"error": str(e)}), 500


# ── ว้าว รอบ 2 #4: Anemia Command Center ──

def _anemia_limits():
    from .anemia import DEFAULTS
    out = {}
    for k, dv in DEFAULTS.items():
        v = get_setting_value(k, "")
        try:
            out[k] = float(v) if v not in ("", None) else dv
        except ValueError:
            out[k] = dv
    return out


@hd_bp.route("/anemia", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def anemia_center():
    import os
    import psycopg2
    from .anemia import collect
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        items = collect(conn, _anemia_limits())
    finally:
        conn.close()
    counts = {}
    for i in items:
        counts[i["tag"]] = counts.get(i["tag"], 0) + 1
    return jsonify({"items": items, "counts": counts, "limits": _anemia_limits()})


# ── ว้าว รอบ 2 #1: Dry Weight Autopilot + UF Planner ──

def _dw_limits():
    from .dw_autopilot import DEFAULTS
    out = {}
    for k, dv in DEFAULTS.items():
        v = get_setting_value(k, "")
        try:
            out[k] = float(v) if v not in ("", None) else dv
        except ValueError:
            out[k] = dv
    return out


def run_dw_autopilot(trigger="scheduled"):
    import os
    import psycopg2
    from flask import current_app
    from .dw_autopilot import generate, line_summary
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return {"created": 0}
    conn = psycopg2.connect(db_url, connect_timeout=5)
    stats = {}
    try:
        created = generate(conn, _dw_limits(), stats=stats)
    finally:
        conn.close()
    sent = 0
    if created:
        push_fn = current_app.config.get("LINE_PUSH_FN")
        ids = get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")
        base = (os.environ.get("RENDER_EXTERNAL_URL") or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
        msg = line_summary(created, base)
        for uid in [r.strip() for r in ids.split(",") if r.strip()]:
            try:
                if push_fn and push_fn(uid, msg) is not False:
                    sent += 1
            except Exception as e:
                logger.warning("dw autopilot push: %s", e)
        log_action(user_id=None, action="dw.proposals_created", resource_type="dw",
                   new_values={"count": len(created), "trigger": trigger, "line_sent": sent}, ip_address="")
    return {"created": len(created), "line_sent": sent, "items": created, "stats": stats}


def _fluid_for_session(session_id):
    """Latest BCM for the session's patient + a plain-Thai hint (None when unavailable)."""
    try:
        from . import fluid_status as fs
        conn = _fluid_conn()
        if not conn:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT patient_id FROM hd_sessions WHERE id = %s", (session_id,))
                r = cur.fetchone()
                if not r:
                    return None
                lim = _fluid_limits()
                latest = fs.latest_for(cur, r[0], lim)
        finally:
            conn.close()
        if not latest:
            return None
        latest["hint"] = fs.uf_hint(latest, lim)
        return latest
    except Exception as e:
        logger.debug("fluid for session: %s", e)
        return None


@hd_bp.route("/uf-plan", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def uf_plan():
    """UF Planner: ?pre_weight=&dry_weight=&hours=  or ?session_id= (reads the session + prescription)."""
    import os
    import psycopg2
    from .dw_autopilot import plan_uf
    lim = _dw_limits()
    pre, dw, hours = request.args.get("pre_weight"), request.args.get("dry_weight"), request.args.get("hours")
    sid = request.args.get("session_id")
    if sid:
        db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        conn = psycopg2.connect(db_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT s.pre_weight, p.dry_weight,
                                      COALESCE((SELECT r.duration_hours FROM hd_prescriptions r WHERE r.patient_id = s.patient_id
                                                AND r.is_active = TRUE ORDER BY r.id DESC LIMIT 1), 4.0), s.uf_goal
                               FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id WHERE s.id = %s""", (sid,))
                r = cur.fetchone()
        finally:
            conn.close()
        if not r:
            return jsonify({"error": "not found"}), 404
        pre = pre or r[0]; dw = dw or r[1]; hours = hours or r[2]
        plan = plan_uf(pre, dw, hours, lim)
        plan["uf_goal_set"] = float(r[3]) if r[3] is not None else None
        plan["fluid"] = _fluid_for_session(sid)
        return jsonify(plan)
    return jsonify(plan_uf(pre, dw, hours or 4, lim))


@hd_bp.route("/dw/proposals", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def dw_proposals():
    import os
    import psycopg2
    from .dw_autopilot import CREATE_SQL
    status = request.args.get("status", "pending")
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            conn.commit()
            q = """SELECT d.id, d.patient_id, d.hn, p.patient_name, d.current_dw, d.proposed_dw, d.direction, d.rationale,
                          d.evidence, d.sessions_used, d.status, d.created_at, d.decided_by, d.decided_at, d.note, p.dry_weight
                   FROM hd_dw_proposals d JOIN hd_patient_mapping p ON p.id = d.patient_id"""
            if status == "all":
                cur.execute(q + " ORDER BY d.id DESC LIMIT 200")
            else:
                cur.execute(q + " WHERE d.status = %s ORDER BY d.id DESC LIMIT 200", (status,))
            items = [{"id": r[0], "patient_id": r[1], "hn": r[2], "name": r[3], "current_dw": float(r[4]) if r[4] is not None else None,
                      "proposed_dw": float(r[5]) if r[5] is not None else None, "direction": r[6], "rationale": r[7],
                      "evidence": r[8] or {}, "sessions_used": r[9], "status": r[10],
                      "created_at": (r[11].isoformat() + "Z") if r[11] else None, "decided_by": r[12],
                      "decided_at": (r[13].isoformat() + "Z") if r[13] else None, "note": r[14],
                      "now_dw": float(r[15]) if r[15] is not None else None,
                      "stale": bool(r[10] == "pending" and abs((float(r[4]) if r[4] is not None else -1) - (float(r[15]) if r[15] is not None else -1)) > 0.05)}
                     for r in cur.fetchall()]
    finally:
        conn.close()
    return jsonify({"items": items, "limits": _dw_limits()})


@hd_bp.route("/dw/proposals/<int:pid>/decide", methods=["POST"])
@require_role("physician")
def dw_decide(pid):
    """Approve (optionally with a different value) → updates hd_patient_mapping.dry_weight; or reject."""
    import os
    import psycopg2
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")
    note = (data.get("note") or "").strip()[:300]
    override = data.get("dry_weight")
    if decision not in ("approve", "reject"):
        return jsonify({"error": "decision must be approve|reject"}), 400
    who = g.get("hd_username") or str(g.get("hd_user_id") or "")
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    applied = None
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT d.patient_id, d.proposed_dw, d.current_dw, d.status, d.hn, p.dry_weight
                           FROM hd_dw_proposals d JOIN hd_patient_mapping p ON p.id = d.patient_id WHERE d.id = %s""", (pid,))
            r = cur.fetchone()
            if not r:
                return jsonify({"error": "not found"}), 404
            if r[3] != "pending":
                return jsonify({"error": f"already {r[3]}"}), 409
            patient_id, proposed, current, _, hn, live_dw = r
            cur_f = float(current) if current is not None else -1
            live_f = float(live_dw) if live_dw is not None else -1
            if decision == "approve" and abs(cur_f - live_f) > 0.05:
                from .dw_autopilot import supersede_stale
                supersede_stale(cur)
                conn.commit()
                return jsonify({"error": "ข้อเสนอนี้ทำไว้ตอน DW " + (f"{cur_f:.1f}" if cur_f >= 0 else "ยังไม่มี") +
                                f" kg แต่ตอนนี้ DW ในระบบเป็น {live_f:.1f} kg แล้ว — ระบบยกเลิกข้อเสนอเก่าให้ กด 'ทบทวน Dry Weight ตอนนี้' เพื่อประเมินใหม่"}), 409
            current = live_dw if live_dw is not None else current
            if decision == "approve":
                try:
                    new_dw = float(override) if override not in (None, "") else float(proposed)
                except (TypeError, ValueError):
                    return jsonify({"error": "dry_weight ต้องเป็นตัวเลข"}), 400
                if not (20 <= new_dw <= 200):
                    return jsonify({"error": "dry_weight อยู่นอกช่วงที่สมเหตุสมผล"}), 400
                cur.execute("UPDATE hd_patient_mapping SET dry_weight = %s, updated_at = NOW() WHERE id = %s", (new_dw, patient_id))
                cur.execute("UPDATE hd_prescriptions SET dry_weight_target = %s, updated_at = NOW() WHERE patient_id = %s AND is_active = TRUE",
                            (new_dw, patient_id))
                applied = new_dw
            cur.execute("""UPDATE hd_dw_proposals SET status = %s, decided_by = %s, decided_at = NOW(), note = %s,
                           proposed_dw = COALESCE(%s, proposed_dw) WHERE id = %s""",
                        ("approved" if decision == "approve" else "rejected", who, note, applied, pid))
        conn.commit()
    finally:
        conn.close()
    log_action(user_id=g.get("hd_user_id"), action=f"dw.{decision}", resource_type="dw", resource_id=pid,
               new_values={"hn": hn, "from": float(current) if current is not None else None, "to": applied, "note": note},
               ip_address=request.remote_addr or "")
    if applied is not None:
        try:
            push_nurse_group(f"🎯 ปรับน้ำหนักแห้ง: HN {hn} {(f'{float(current):.1f}' if current is not None else 'ยังไม่มี')} → {applied:.1f} kg" + (f" ({note})" if note else "") +
                             f"\nโดย {who} — ใช้ตั้งแต่รอบถัดไป", fallback_recipients=False)
        except Exception as e:
            logger.warning("dw nurse push: %s", e)
    return jsonify({"ok": True, "decision": decision, "applied": applied})


@hd_bp.route("/dw/run", methods=["POST"])
@require_role("physician", "admin")
def dw_run_now():
    try:
        return jsonify(run_dw_autopilot(trigger="manual"))
    except Exception as e:
        logger.error("dw run: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Go-Live R4: PDPA — consent registry, privacy notice, consent form ──

@hd_bp.route("/consents", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def consents_list():
    import os
    import psycopg2
    from .pdpa import CONSENT_TYPES, CREATE_INDEX_SQL, CREATE_SQL, NOTICE_VERSION, overview, status_for
    hn = request.args.get("hn")
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            cur.execute(CREATE_INDEX_SQL)
            conn.commit()
            if hn:
                return jsonify({"hn": hn, "consents": status_for(cur, hn), "types": CONSENT_TYPES, "version": NOTICE_VERSION})
            items = overview(cur)
    finally:
        conn.close()
    n = len(items)
    given = sum(1 for i in items if i["consents"].get("data_processing", {}).get("status") == "given")
    line_ok = sum(1 for i in items if i["consents"].get("line_messaging", {}).get("status") == "given")
    return jsonify({"items": items, "types": CONSENT_TYPES, "version": NOTICE_VERSION,
                    "counts": {"patients": n, "data_processing": given, "line_messaging": line_ok},
                    "require_consent_for_push": (get_setting_value("patient_push_requires_consent", "off") or "off").lower() == "on"})


@hd_bp.route("/consents", methods=["POST"])
@require_role("physician", "nurse", "admin")
def consents_record():
    """{hn, types: [..] or type, status: given|withdrawn, via: paper|web|line, note}"""
    import os
    import psycopg2
    from .pdpa import CONSENT_TYPES, CREATE_SQL, record
    data = request.get_json(silent=True) or {}
    hn = (data.get("hn") or "").strip()
    types = data.get("types") or ([data.get("type")] if data.get("type") else [])
    status = data.get("status", "given")
    via = data.get("via", "paper")
    if not hn or not types or any(t not in CONSENT_TYPES for t in types) or status not in ("given", "withdrawn"):
        return jsonify({"error": "hn/types/status ไม่ถูกต้อง"}), 400
    who = g.get("hd_username") or str(g.get("hd_user_id") or "")
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            ids = [record(cur, hn, t, status, via, who, data.get("note") or "") for t in types]
        conn.commit()
    finally:
        conn.close()
    log_action(user_id=g.get("hd_user_id"), action=f"consent.{status}", resource_type="consent", resource_id=ids[0],
               new_values={"hn": hn, "types": types, "via": via}, ip_address=request.remote_addr or "")
    return jsonify({"ok": True, "ids": ids})


@hd_bp.route("/consents/form.pdf", methods=["GET"])
@require_role("physician", "nurse", "admin")
def consents_form_pdf():
    import os
    import psycopg2
    from flask import Response
    from .pdpa import consent_form_pdf
    hn = request.args.get("hn", "")
    name = ""
    if hn:
        db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        conn = psycopg2.connect(db_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT patient_name FROM hd_patient_mapping WHERE UPPER(hn) = %s", (hn.upper(),))
                r = cur.fetchone()
                name = r[0] if r else ""
        finally:
            conn.close()
    pdf = consent_form_pdf(get_setting_value("clinic_name", "") or "คลินิกระนอง รักษ์ไต", name, hn)
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": _content_disposition(f"consent_{hn or 'blank'}.pdf")})


def patient_withdraw_consent(hn, via="line"):
    """Called from the LINE webhook when a linked patient types ถอนความยินยอม."""
    import os
    import psycopg2
    from .pdpa import CREATE_SQL, withdraw_all
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            withdraw_all(cur, hn, via)
        conn.commit()
    finally:
        conn.close()
    log_action(user_id=None, action="consent.withdrawn", resource_type="consent", resource_id=None,
               new_values={"hn": hn, "types": ["line_messaging"], "via": via}, ip_address="")
    try:
        push_nurse_group(f"ℹ️ ผู้ป่วย HN {hn} ถอนความยินยอมรับข้อความ LINE (PDPA) — ระบบหยุดส่งสรุป/แจ้งเตือนให้แล้ว", fallback_recipients=True)
    except Exception:
        pass


# ── Go-Live R1: nightly full DB backup (GCS + Drive) + verify + weekly restore test ──

def run_db_backup(trigger="scheduled", restore_test=False, send_line=True):
    import os
    import psycopg2
    from flask import current_app
    from . import db_backup as dbb
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return {"error": "no database"}
    conn = psycopg2.connect(db_url, connect_timeout=10)
    try:
        blob, man = dbb.dump(conn, app_version=(os.environ.get("RENDER_GIT_COMMIT") or "")[:7])
        restore = None
        if restore_test:
            try:
                restore = dbb.restore_to_schema(conn, blob)
                dbb.drop_restore_schema(conn, restore["schema"])
            except Exception as e:
                conn.rollback()
                restore = {"ok": False, "error": str(e)[:120], "restored": {}}
    finally:
        conn.close()
    verify_ok, problems = dbb.verify(blob)
    name = dbb.archive_name()
    now = datetime.now(timezone.utc) + timedelta(hours=7)
    gcs_path = ""
    try:
        bucket = getattr(current_app.config.get("STORAGE_OBJ"), "_bucket", None)
        if bucket is not None:
            gcs_path = f"{dbb.GCS_PREFIX}/{now.year}/{now.month:02d}/{name}"
            bucket.blob(gcs_path).upload_from_string(blob, content_type="application/gzip")
    except Exception as e:
        logger.error("db backup gcs: %s", e)
        gcs_path = ""
    drive_ok = False
    drive_err = ""
    try:
        from .daily_records import drive_upload_fn
        up = drive_upload_fn()
        if up:
            up(f"{dbb.DRIVE_DIR}/{now.year}/{now.month:02d}/{name}", blob, "application/gzip")
            drive_ok = True
        else:
            drive_err = "ไม่ได้เปิดใช้ Google Drive"
    except Exception as e:
        drive_err = str(e)[:120]
        logger.warning("db backup drive: %s", drive_err)
    status = {"name": name, "gcs": gcs_path, "drive": drive_ok, "drive_error": drive_err, "verify": verify_ok, "problems": problems,
              "tables": man.get("table_count"), "rows": man.get("row_total"), "bytes": man.get("archive_bytes"),
              "restore": {k: v for k, v in (restore or {}).items() if k != "restored"} if restore else None,
              "at": man["created_at"], "trigger": trigger}
    try:
        _set_setting("last_db_backup", json.dumps(status, ensure_ascii=False, default=str))
    except Exception:
        pass
    log_action(user_id=None, action="db.backup", resource_type="backup", resource_id=None,
               new_values={k: v for k, v in status.items() if k != "problems"}, ip_address="")
    if send_line:
        ok_all = bool(gcs_path) and verify_ok and (restore is None or restore.get("ok"))
        msg = dbb.line_text(man, gcs_path, drive_ok, verify_ok, problems, restore)
        if drive_err:
            msg += f"\nℹ️ Drive: {drive_err}"
        if not ok_all:
            msg = "🚨 สำรองฐานข้อมูลมีปัญหา\n" + msg
        push_fn = current_app.config.get("LINE_PUSH_FN")
        ids = get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")
        # quiet success: only on Monday (weekly restore test) or on any problem
        if not ok_all or restore_test or trigger == "manual":
            from . import ops_status as ops
            import contextlib
            with (ops.critical() if not ok_all else contextlib.nullcontext()):
                for uid in [r.strip() for r in ids.split(",") if r.strip()]:
                    try:
                        push_fn and push_fn(uid, msg)
                    except Exception as e:
                        logger.warning("db backup push: %s", e)
    return status


@hd_bp.route("/ops/backup/run", methods=["POST"])
@require_role("physician", "admin")
def ops_backup_run():
    data = request.get_json(silent=True) or {}
    try:
        st = run_db_backup(trigger="manual", restore_test=bool(data.get("restore_test")), send_line=True)
        return jsonify(st), (200 if st.get("gcs") else 500)
    except Exception as e:
        logger.error("backup run: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/ops/tv-link", methods=["GET", "POST"])
@require_role("physician", "admin")
def ops_tv_link():
    """📺 ลิงก์สำหรับจอ TV: token อ่านอย่างเดียว (role=display) อายุ 90 วัน ใช้ได้เฉพาะ TV Mode
    POST สร้างลิงก์ใหม่ (?revoke=1 ยกเลิกลิงก์เก่าทั้งหมดก่อน) · GET ดูสถานะ"""
    import os
    from .auth import create_token
    gen = int(get_setting_value("tv_link_generation", "1") or 1)
    base = (os.environ.get("RENDER_EXTERNAL_URL") or request.url_root.rstrip("/")).rstrip("/")
    if request.method == "GET":
        return jsonify({"generation": gen, "issued_at": get_setting_value("tv_link_issued_at", ""), "days": 90})
    if request.args.get("revoke") == "1":
        gen += 1
        _set_setting("tv_link_generation", str(gen))
    name = (request.get_json(silent=True) or {}).get("name") or "TV"
    import jwt as pyjwt
    from .config import HdConfig
    payload = {"user_id": g.get("hd_user_id"), "username": f"display:{name}"[:50], "role": "display", "gen": gen,
               "exp": datetime.now(timezone.utc) + timedelta(days=90), "iat": datetime.now(timezone.utc)}
    token = pyjwt.encode(payload, HdConfig.JWT_SECRET, algorithm=HdConfig.JWT_ALGORITHM) if pyjwt else create_token(g.get("hd_user_id"), f"display:{name}", "display", 90 * 24 * 60)
    url = f"{base}/hd/tv?token={token}"
    now = (datetime.now(timezone.utc) + timedelta(hours=7)).isoformat(timespec="minutes")
    _set_setting("tv_link_issued_at", now)
    qr = None
    try:
        import io
        import segno
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="png", scale=6, border=2)
        import base64
        qr = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    log_action(user_id=g.get("hd_user_id"), action="ops.tv_link", resource_type="ops", resource_id=None,
               new_values={"generation": gen, "name": name, "revoked_previous": request.args.get("revoke") == "1"},
               ip_address=request.remote_addr or "")
    return jsonify({"url": url, "qr": qr, "generation": gen, "expires_days": 90, "issued_at": now})


@hd_bp.route("/ops/status", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def ops_status_api():
    """กระดานสถานะงานอัตโนมัติ + LINE (ops watchdog)."""
    import os
    from . import ops_status as ops
    try:
        ops.set_reserve(int(get_setting_value("line_quota_reserve", "") or ops.QUOTA_RESERVE_DEFAULT))
    except Exception:
        pass
    if request.args.get("refresh_quota"):
        ops.check_line_quota(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip())
    s = ops.snapshot()
    s["text"] = ops.line_text(s)
    return jsonify(s)


@hd_bp.route("/ops/status/test-line", methods=["POST"])
@require_role("physician", "admin")
def ops_status_test_line():
    """ส่งข้อความทดสอบถึง alert_recipients แล้วรายงานผลจริงจาก LINE API."""
    from flask import current_app
    push_fn = current_app.config.get("LINE_PUSH_FN")
    ids = [r.strip() for r in (get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")).split(",") if r.strip()]
    if not push_fn or not ids:
        return jsonify({"ok": False, "error": "LINE ไม่พร้อม หรือยังไม่ตั้ง alert_recipients"}), 400
    from . import ops_status as ops
    now = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%d/%m %H:%M")
    with ops.critical():
        sent = sum(1 for u in ids if push_fn(u, f"📨 ทดสอบระบบแจ้งเตือน {now} — ถ้าได้รับข้อความนี้ แสดงว่า LINE ส่งได้ปกติ"))
    snap = ops.snapshot()
    log_action(user_id=g.get("hd_user_id"), action="ops.test_line", resource_type="ops", resource_id=None,
               new_values={"sent": sent, "recipients": len(ids)}, ip_address=request.remote_addr or "")
    return jsonify({"ok": sent > 0, "sent": sent, "recipients": len(ids), "line_last_error": snap["line"].get("last_error")})


@hd_bp.route("/ops/backup/status", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def ops_backup_status():
    import os
    from flask import current_app
    raw = get_setting_value("last_db_backup", "")
    try:
        last = json.loads(raw) if raw else None
    except Exception:
        last = None
    recent = []
    try:
        bucket = getattr(current_app.config.get("STORAGE_OBJ"), "_bucket", None)
        if bucket is not None:
            from . import db_backup as dbb
            blobs = sorted(bucket.list_blobs(prefix=dbb.GCS_PREFIX + "/"), key=lambda b: b.name)[-14:]
            recent = [{"name": b.name.split("/")[-1], "bytes": b.size, "updated": b.updated.isoformat() if b.updated else None} for b in blobs]
    except Exception as e:
        logger.info("backup list: %s", e)
    return jsonify({"last": last, "recent": recent[::-1]})


# ── B11: CI / deploy status → physician LINE ──────────────

@hd_bp.route("/ops/ci-status", methods=["POST"])
def ops_ci_status():
    """GitHub Actions posts here when CI fails (shared secret CI_NOTIFY_TOKEN).
    Without it Render silently skips the deploy and nobody notices."""
    import hmac
    import os
    from flask import current_app
    expected = os.environ.get("CI_NOTIFY_TOKEN", "").strip()
    got = (request.headers.get("X-CI-Token") or "").strip()
    if not expected or not got or not hmac.compare_digest(expected, got):
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    sha = str(data.get("sha", ""))[:7]
    msg = (f"❌ CI ล้ม — Render จะไม่ deploy commit นี้\n"
           f"commit {sha} ({data.get('branch', '')}): {str(data.get('message', ''))[:120]}\n"
           f"job ที่ล้ม: {data.get('failed_jobs') or '-'}\n"
           f"{data.get('run_url', '')}\n"
           f"production ยังเป็นเวอร์ชันก่อนหน้า — ต้องแก้แล้ว push ใหม่")
    push_fn = current_app.config.get("LINE_PUSH_FN")
    ids = get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")
    sent = 0
    for uid in [r.strip() for r in ids.split(",") if r.strip()]:
        try:
            if push_fn and push_fn(uid, msg) is not False:
                sent += 1
        except Exception as e:
            logger.warning("ci-status push: %s", e)
    log_action(user_id=None, action="ops.ci_failed", resource_type="ops",
               new_values={"sha": sha, "failed_jobs": data.get("failed_jobs")}, ip_address=request.remote_addr or "")
    return jsonify({"ok": True, "sent": sent})


def announce_deploy_once():
    """After boot: tell the physician which commit is live (once per commit,
    GCS marker so several workers / restarts do not repeat it)."""
    import os
    from flask import current_app
    sha = (os.environ.get("RENDER_GIT_COMMIT") or "").strip()
    if not sha:
        return False
    from . import _claim_once
    from datetime import date as _d
    if not _claim_once(current_app, f"deploy_{sha[:7]}", _d.today()):
        return False
    push_fn = current_app.config.get("LINE_PUSH_FN")
    ids = get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")
    msg = (f"🚀 Deploy สำเร็จ — production เป็นเวอร์ชัน {sha[:7]} แล้ว\n"
           f"{(os.environ.get('RENDER_EXTERNAL_URL') or 'https://ranong-raktai-ai-cloud.onrender.com')}/hd/dashboard")
    n = 0
    for uid in [r.strip() for r in ids.split(",") if r.strip()]:
        try:
            if push_fn and push_fn(uid, msg) is not False:
                n += 1
        except Exception as e:
            logger.warning("deploy announce: %s", e)
    return n > 0


# ── B6: Lab results → bed card ─────────────────────────────

def run_lab_sync():
    """Copy recent lab documents' extracted results into hd_lab_docs."""
    import os
    import psycopg2
    from flask import current_app
    from .lab_sync import sync_labs
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return {"error": "no database"}
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        return sync_labs(conn, current_app.config.get("STORAGE_OBJ"), current_app.config.get("DOC_INDEX"))
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Fluid Status Center — BCM quarterly assessments (14 Nov 2026)
# ═══════════════════════════════════════════════════════════════════════════
def _fluid_limits():
    from .fluid_status import limits_from
    return limits_from(get_setting_value)


def _fluid_conn():
    import os
    import psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return None
    return psycopg2.connect(db_url, connect_timeout=5)


@hd_bp.route("/fluid/import/preview", methods=["POST"])
@require_role("physician", "nurse", "admin")
def fluid_import_preview():
    """อัปโหลด .zip ของ PDF BCM / .pdf / .xlsx → อ่านค่า + จับคู่ HN → แสดงก่อนบันทึก (ไม่เขียนอะไร)."""
    from . import fluid_status as fs
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "ไม่มีไฟล์"}), 400
    data = f.read()
    if len(data) > 60 * 1024 * 1024:
        return jsonify({"error": "ไฟล์ใหญ่เกิน 60 MB"}), 400
    try:
        recs = fs.read_upload(f.filename, data)
    except Exception as e:
        return jsonify({"error": f"อ่านไฟล์ไม่ได้: {e}"}), 400
    conn = _fluid_conn()
    if not conn:
        return jsonify({"error": "No database"}), 503
    try:
        with conn.cursor() as cur:
            rows = fs.match(cur, recs)
    finally:
        conn.close()
    lim = _fluid_limits()
    for r in rows:
        r["flags"] = fs.flags(r, lim)
        r["flags_th"] = [fs.FLAG_TH[x] for x in r["flags"]]
    return jsonify({"filename": f.filename, "rows": rows, "limits": lim,
                    "matched": sum(1 for r in rows if r["patient_id"]), "unmatched": sum(1 for r in rows if not r["patient_id"]),
                    "duplicates": sum(1 for r in rows if r["duplicate"]),
                    "review": sum(1 for r in rows if r["how"] in ("name_fuzzy", "eng_name"))})


@hd_bp.route("/fluid/import/commit", methods=["POST"])
@require_role("physician", "nurse", "admin")
def fluid_import_commit():
    """บันทึกแถวที่ยืนยัน (แก้ HN ในหน้าจอได้ → ส่ง hn ใหม่มา) + จำ BCM ID ↔ ผู้ป่วย."""
    from . import fluid_status as fs
    body = request.get_json(silent=True) or {}
    rows = body.get("rows") or []
    if not rows:
        return jsonify({"error": "ไม่มีแถว"}), 400
    conn = _fluid_conn()
    if not conn:
        return jsonify({"error": "No database"}), 503
    try:
        with conn.cursor() as cur:
            # rows whose HN was edited by the nurse: resolve patient_id again
            fix = [r for r in rows if r.get("hn") and (not r.get("patient_id") or r.get("hn_edited"))]
            if fix:
                cur.execute("SELECT id, UPPER(hn) FROM hd_patient_mapping WHERE UPPER(hn) = ANY(%s)",
                            ([str(r["hn"]).upper() for r in fix],))
                m = {hn: pid for pid, hn in cur.fetchall()}
                for r in fix:
                    pid = m.get(str(r["hn"]).upper())
                    if pid:
                        r["patient_id"], r["how"], r["duplicate"] = pid, "manual", False
                    else:
                        r["patient_id"] = None
            res = fs.insert(cur, rows, confirmed_by=g.get("hd_username") or "", source_file=body.get("filename") or "")
        conn.commit()
    finally:
        conn.close()
    log_action(user_id=g.get("hd_user_id"), action="fluid.import", resource_type="hd_fluid_assessments", resource_id=None,
               new_values={"file": (body.get("filename") or "")[:80], **res}, ip_address=request.remote_addr or "")
    return jsonify({"ok": True, **res})


@hd_bp.route("/fluid/overview", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def fluid_overview():
    from . import fluid_status as fs
    conn = _fluid_conn()
    if not conn:
        return jsonify({"error": "No database"}), 503
    try:
        with conn.cursor() as cur:
            ov = fs.overview(cur, _fluid_limits())
    finally:
        conn.close()
    for p in ov["patients"]:
        p["flags_th"] = [fs.FLAG_TH[x] for x in p["flags"]]
    ov["text"] = fs.line_text(ov)
    return jsonify(ov)


@hd_bp.route("/fluid/push", methods=["POST"])
@require_role("physician", "admin")
def fluid_push():
    """ส่งสรุปสารน้ำเข้ากลุ่มพยาบาล (ข้อความเดียว)."""
    from . import fluid_status as fs
    conn = _fluid_conn()
    try:
        with conn.cursor() as cur:
            ov = fs.overview(cur, _fluid_limits())
    finally:
        conn.close()
    r = push_nurse_group(fs.line_text(ov))
    return jsonify(r)


@hd_bp.route("/patients/<hn>/fluid", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def fluid_for_patient(hn):
    from . import fluid_status as fs
    conn = _fluid_conn()
    if not conn:
        return jsonify({"error": "No database"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, dry_weight FROM hd_patient_mapping WHERE UPPER(hn) = %s", (hn.upper(),))
            r = cur.fetchone()
            if not r:
                return jsonify({"error": "ไม่พบ HN"}), 404
            lim = _fluid_limits()
            latest = fs.latest_for(cur, r[0], lim)
            series = fs.series_for(cur, r[0])
    finally:
        conn.close()
    return jsonify({"hn": hn, "dry_weight": float(r[1]) if r[1] is not None else None, "latest": latest, "series": series,
                    "hint": fs.uf_hint(latest, lim), "limits": lim})


@hd_bp.route("/labs/import/preview", methods=["POST"])
@require_role("physician", "nurse", "admin")
def labs_import_preview():
    """อัปโหลดไฟล์ Excel/CSV ผลแล็บทั้งหน่วย → จับคู่ HN + ตรวจซ้ำ → แสดงก่อนบันทึก (ยังไม่เขียนอะไร)."""
    import os
    from . import lab_import as li
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "ไม่มีไฟล์"}), 400
    data = f.read()
    if len(data) > 10 * 1024 * 1024:
        return jsonify({"error": "ไฟล์ใหญ่เกิน 10 MB"}), 400
    try:
        parsed = li.parse_rows(li.read_file(f.filename, data))
    except Exception as e:
        return jsonify({"error": f"อ่านไฟล์ไม่ได้: {e}"}), 400
    if not parsed["rows"]:
        return jsonify({"error": "; ".join(parsed["errors"]) or "ไม่พบข้อมูลผู้ป่วยในไฟล์"}), 400
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    import psycopg2
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            matched, unmatched = li.match_patients(cur, parsed["rows"])
    finally:
        conn.close()
    return jsonify({"filename": f.filename, "fields": parsed["fields"], "default_date": parsed["default_date"],
                    "errors": parsed["errors"], "matched": matched, "unmatched": unmatched,
                    "duplicates": sum(1 for r in matched if r["duplicate"]), "summary": li.summary(matched)})


@hd_bp.route("/labs/import/commit", methods=["POST"])
@require_role("physician", "nurse", "admin")
def labs_import_commit():
    """บันทึกแถวที่ยืนยันแล้วลง hd_lab_results (source=import:<file>) — ไม่แก้/ลบของเดิม."""
    import os
    from . import lab_import as li
    body = request.get_json(silent=True) or {}
    rows = [r for r in (body.get("rows") or []) if r.get("patient_id") and r.get("lab_date")]
    if not rows:
        return jsonify({"error": "ไม่มีแถวที่จะบันทึก"}), 400
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    import psycopg2
    source = "import:" + (body.get("filename") or "file")[:40]
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            res = li.insert_rows(cur, rows, source)
        conn.commit()
    finally:
        conn.close()
    log_action(user_id=g.get("hd_user_id"), action="labs.import", resource_type="hd_lab_results", resource_id=None,
               new_values={"source": source, **res, "fields": body.get("fields")}, ip_address=request.remote_addr or "")
    # ผล Hb ใหม่ → ESA Autopilot จะเสนอในรอบถัดไปเอง (ทุกชั่วโมง 06–20 น.)
    # แจ้งผู้ป่วยที่ผูก LINE ว่ามีผลใหม่ (เฉพาะแถวที่บันทึกจริง ไม่ใช่แถวซ้ำ)
    notified = notify_patients_new_results([r.get("hn") for r in rows if r.get("hn") and not r.get("duplicate")])
    return jsonify({"ok": True, **res, "source": source, "patient_notify": notified})


@hd_bp.route("/labs/import/template", methods=["GET"])
@require_role("physician", "nurse", "admin")
def labs_import_template():
    """แม่แบบ Excel สำหรับกรอกผลแล็บ — เติม HN+ชื่อคนไข้ทุกคนให้แล้ว เจ้าหน้าที่กรอกแค่ตัวเลข.
    หัวคอลัมน์ตรงกับ parser ของระบบนำเข้า → กรอกเสร็จอัปโหลดกลับได้ทันที. NEW — ไม่แตะของเดิม."""
    import os, io, psycopg2
    from flask import send_file
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    patients = []
    if db_url:
        try:
            conn = psycopg2.connect(db_url, connect_timeout=5)
            with conn.cursor() as cur:
                cur.execute("""SELECT hn, patient_name FROM hd_patient_mapping
                               WHERE is_active = TRUE ORDER BY patient_name""")
                patients = cur.fetchall()
            conn.close()
        except Exception as e:
            logger.warning("labs_import_template patients: %s", e)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ผลแล็บ"
    headers = ["HN", "ชื่อ-สกุล", "วันที่ตรวจ", "Hb(g/dL)", "Hct(%)", "K", "Na", "Ca", "P",
               "Alb", "iPTH", "Ferritin", "TSAT", "BUN", "Cr", "Kt/V", "URR", "HBsAg", "Anti-HCV"]
    ws.append(headers)
    hdr_fill = PatternFill("solid", fgColor="1F6FB2")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = hdr_fill
        c.alignment = Alignment(horizontal="center")
    today_str = _thai_today().strftime("%d/%m/%Y")
    for hn, name in patients:
        ws.append([hn, name, today_str] + [None] * (len(headers) - 3))
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["C"].width = 12
    for col in "DEFGHIJKLMNOPQRS":
        ws.column_dimensions[col].width = 9
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"lab_template_{_thai_today().isoformat()}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@hd_bp.route("/labs/import/history", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def labs_import_history():
    """ประวัติการนำเข้าผลแล็บ (group ตาม source) — ไฟล์ไหน เมื่อไหร่ กี่รายการ. NEW — ไม่แตะของเดิม."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"imports": []})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""SELECT source, COUNT(*), MIN(lab_date), MAX(lab_date), MAX(created_at),
                                  COUNT(DISTINCT patient_id)
                           FROM hd_lab_results
                           WHERE source LIKE 'import:%%' OR source = 'excel_import'
                           GROUP BY source ORDER BY MAX(created_at) DESC NULLS LAST LIMIT 20""")
            rows = cur.fetchall()
        conn.close()
        return jsonify({"imports": [{
            "source": r[0], "rows": r[1],
            "lab_date_from": r[2].isoformat() if r[2] else None,
            "lab_date_to": r[3].isoformat() if r[3] else None,
            "imported_at": r[4].isoformat() if r[4] else None,
            "patients": r[5],
        } for r in rows]})
    except Exception as e:
        logger.error("labs_import_history: %s", e)
        return jsonify({"imports": [], "error": str(e)})


# ── Lab Unified (1+2+3): ลงครั้งเดียวเห็นทุกหน้า · อ่านจากรูปถ่าย · กันซ้ำข้ามแหล่ง ──
_UNIFIED_COLS = ["hb", "hct", "k", "na", "ca", "phos", "albumin", "pth", "ferritin", "tsat",
                 "ktv", "urr", "bun", "creatinine", "hbsag", "antihcv"]
_UNIFIED_TO_TEMPLATE = {"hb": "hemoglobin", "hct": "hematocrit", "k": "potassium", "na": "sodium",
                        "ca": "calcium", "phos": "phosphate", "albumin": "albumin", "pth": "ipth",
                        "ferritin": "ferritin", "tsat": "tsat", "ktv": "kt_v", "urr": "urr",
                        "bun": "bun", "creatinine": "creatinine", "hbsag": "hep_b", "antihcv": "hep_c"}


@hd_bp.route("/patients/hn/<hn>/labs-unified", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def get_patient_labs_unified(hn):
    """(1) ประวัติแล็บรวมทุกแหล่ง — กรอกมือ/Excel + รูปถ่าย LINE — จาก hd_labs VIEW.
    NEW endpoint: หน้าเดิมที่ใช้ /labs ยังอยู่ครบ."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"labs": []})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            # _table DESC → lab_results (กรอกมือ) มาก่อน lab_docs (รูปถ่าย) ในวันเดียวกัน
            cur.execute(f"""SELECT l.lab_date, l._table, l.source, {', '.join('l.' + c for c in _UNIFIED_COLS)}
                            FROM hd_labs l JOIN hd_patient_mapping p ON p.id = l.patient_id
                            WHERE p.hn = %s AND l.lab_date IS NOT NULL
                            ORDER BY l.lab_date DESC, l._table DESC LIMIT 80""", (hn,))
            rows = cur.fetchall()
        conn.close()
        labs, seen = [], set()
        for r in rows:
            # ยุบแถวซ้ำ: วันที่ + ค่าทุกตัวเหมือนกัน → แสดงครั้งเดียว (เช่น กดบันทึกซ้ำ / รูปถ่ายของชุดเดียวกัน)
            key = (r[0],) + tuple(str(v) for v in r[3:])
            if key in seen:
                continue
            seen.add(key)
            entry = {"date": r[0].isoformat() if r[0] else None,
                     "source_type": "photo" if r[1] == "lab_docs" else "manual",
                     "source": r[2] or ""}
            for i, c in enumerate(_UNIFIED_COLS):
                v = r[3 + i]
                entry[_UNIFIED_TO_TEMPLATE[c]] = (float(v) if v is not None and c not in ("hbsag", "antihcv") else v)
            labs.append(entry)
            if len(labs) >= 24:
                break
        return jsonify({"labs": labs, "total": len(labs)})
    except Exception as e:
        logger.error("labs-unified: %s", e)
        return jsonify({"labs": [], "error": str(e)})


@hd_bp.route("/labs/read-photo", methods=["POST"])
@require_role("physician", "nurse", "admin")
def labs_read_photo():
    """(2) รูปถ่ายใบแล็บ → OCR → ดึงค่า → ส่งกลับให้เติมฟอร์ม (คนตรวจก่อนบันทึกเสมอ — ไม่บันทึกเอง)."""
    from .lab_extractor import extract_lab_values
    f = request.files.get("image")
    if not f:
        return jsonify({"error": "ไม่พบรูป"}), 400
    data = f.read()
    if not data or len(data) > 12 * 1024 * 1024:
        return jsonify({"error": "รูปว่างหรือใหญ่เกิน 12 MB"}), 400
    words, engine = _ocr_words(data)
    text = " ".join(w["t"] for w in words)
    res = extract_lab_values(text)
    name_to_field = {"Hb": "hemoglobin", "Hct": "hematocrit", "K": "potassium", "Na": "sodium",
                     "Ca": "calcium", "PO4": "phosphate", "Albumin": "albumin", "iPTH": "ipth",
                     "BUN": "bun", "Cr": "creatinine", "Ferritin": "ferritin", "TSAT": "tsat",
                     "Kt/V": "kt_v", "URR": "urr"}
    # ขอบเขตค่าที่เป็นไปได้ทางคลินิก — เกินนี้คือ OCR อ่านเพี้ยน ไม่ส่งไปเติมฟอร์ม
    sane = {"hemoglobin": (3, 20), "hematocrit": (10, 60), "potassium": (1.5, 9.9), "sodium": (110, 165),
            "calcium": (4, 15), "phosphate": (0.5, 12), "albumin": (1, 6), "ipth": (5, 3000),
            "bun": (2, 200), "creatinine": (0.3, 30), "ferritin": (5, 5000), "tsat": (2, 100),
            "kt_v": (0.3, 3.5), "urr": (20, 95)}
    values, rejected = {}, []
    for item in res.get("found", []):
        field = name_to_field.get(item["name"])
        if not field:
            continue
        lo, hi = sane[field]
        if lo <= item["value"] <= hi:
            values[field] = item["value"]
        else:
            rejected.append(f"{item['name']}={item['value']}")
    log_action(user_id=g.get("hd_user_id"), action="labs.read_photo", resource_type="lab",
               new_values={"engine": engine, "found": sorted(values), "rejected": rejected},
               ip_address=request.remote_addr or "")
    return jsonify({"values": values, "found": sorted(values), "rejected": rejected,
                    "engine": engine, "words": len(words),
                    "note": "ค่าแนะนำจาก OCR — ตรวจทุกตัวเลขก่อนบันทึก"})


@hd_bp.route("/patients/hn/<hn>/labs/check-date", methods=["GET"])
@require_role("physician", "nurse", "admin")
def labs_check_date(hn):
    """(3) กันซ้ำข้ามแหล่ง: วันนี้/วันที่เลือก คนนี้มีผลแล็บอยู่แล้วหรือยัง (ทุกแหล่ง)."""
    import os, psycopg2
    from datetime import date as _date
    q = (request.args.get("date") or "").strip()
    try:
        d = _date.fromisoformat(q) if q else _thai_today()
    except ValueError:
        return jsonify({"error": "รูปแบบวันที่ไม่ถูกต้อง"}), 400
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"exists": False, "rows": []})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""SELECT l._table, l.source, l.hb, l.k, l.ktv
                           FROM hd_labs l JOIN hd_patient_mapping p ON p.id = l.patient_id
                           WHERE p.hn = %s AND l.lab_date = %s LIMIT 5""", (hn, d))
            rows = [{"source_type": "photo" if r[0] == "lab_docs" else "manual", "source": r[1] or "",
                     "hb": float(r[2]) if r[2] is not None else None,
                     "k": float(r[3]) if r[3] is not None else None,
                     "ktv": float(r[4]) if r[4] is not None else None} for r in cur.fetchall()]
        conn.close()
        return jsonify({"exists": bool(rows), "date": d.isoformat(), "rows": rows})
    except Exception as e:
        logger.warning("labs_check_date: %s", e)
        return jsonify({"exists": False, "rows": [], "error": str(e)})


@hd_bp.route("/labs/sync", methods=["POST"])
@require_role("physician", "admin")
def labs_sync_now():
    try:
        st = run_lab_sync()
        log_action(user_id=g.get("hd_user_id"), action="labs.sync", resource_type="labs",
                   new_values=st, ip_address=request.remote_addr or "")
        return jsonify(st)
    except Exception as e:
        logger.error("labs sync: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/patients/<int:patient_id>/labs", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def patient_labs(patient_id):
    """Lab history (newest first, up to 12 rows) + flags on the latest."""
    import os
    import psycopg2
    from .lab_sync import NUMERIC_COLS, TEXT_COLS, flags_for, latest_for_patients
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"rows": [], "latest": None})
    conn = psycopg2.connect(db_url, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT COALESCE(specimen_date, doc_date), status, doc_id, {', '.join(NUMERIC_COLS + TEXT_COLS)}
                            FROM hd_lab_docs WHERE patient_id = %s
                            ORDER BY COALESCE(specimen_date, doc_date) DESC NULLS LAST, id DESC LIMIT 12""", (patient_id,))
            rows = []
            for r in cur.fetchall():
                d = dict(zip(NUMERIC_COLS + TEXT_COLS, r[3:]))
                for c in NUMERIC_COLS:
                    if d[c] is not None:
                        d[c] = float(d[c])
                d.update({"date": r[0].isoformat() if r[0] else None, "status": r[1], "doc_id": r[2]})
                d["flags"] = flags_for(d)
                rows.append(d)
            latest = latest_for_patients(cur, [patient_id]).get(patient_id)
    except Exception as e:
        logger.warning("patient_labs: %s", e)
        rows, latest = [], None
    finally:
        conn.close()
    return jsonify({"rows": rows, "latest": latest})


@hd_bp.route("/daily-log/export", methods=["POST"])
@require_role("physician", "admin")
def daily_log_export_now():
    """Manual trigger of the day export (same as the 22:30 job)."""
    day = _parse_date_arg()
    try:
        res = run_daily_export(day, trigger="manual", user=g.get("hd_user_id"))
    except Exception as e:
        logger.error("daily export error: %s", e)
        return jsonify({"ok": 0, "failed": 0, "error": str(e)}), 500
    status = 200 if not res.get("error") else 503
    return jsonify(res), status


# ── Health / Status ───────────────────────────────────────

@hd_bp.route("/health", methods=["GET"])
def hd_health():
    """HD module health check — checks DB connectivity."""
    import os
    from .config import HdConfig
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    db_ok = False
    db_patients = 0
    db_active_sessions = 0
    if db_url:
        try:
            import psycopg2
            conn = psycopg2.connect(db_url, connect_timeout=3)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM hd_patient_mapping WHERE is_active=TRUE")
                db_patients = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM hd_sessions WHERE status='in_progress'")
                db_active_sessions = cur.fetchone()[0]
            conn.close()
            db_ok = True
        except Exception as e:
            logger.warning("health check DB error: %s", e)

    alert_configured = bool(
        get_setting_value("alert_recipients", "")
        or get_setting_value("physician_line_id", "")
    )

    # Check Anthropic API Key presence
    api_key_warning = ""
    _api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not _api_key:
        api_key_warning = "⚠️ Anthropic API Key ไม่ได้ตั้งค่า"

    # ok: DB connected or not configured (standalone); degraded: configured but unreachable
    status = "ok" if (db_ok or not db_url) else "degraded"
    return jsonify({
        "module": "hd_smart_monitoring",
        "status": status,
        "version": "2.2.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": db_ok,
        "patients": db_patients,
        "active_sessions": db_active_sessions,
        "total_beds": HdConfig.TOTAL_BEDS,
        "alert_configured": alert_configured,
        "checklist": {
            "database": "✅" if db_ok else "❌ ไม่สามารถเชื่อมต่อ DB",
            "patients_imported": "✅" if db_patients > 0 else "⚠️ ยังไม่มีผู้ป่วย",
            "alert_recipients": "✅" if alert_configured else "⚠️ ยังไม่ตั้ง LINE alert → พิมพ์ myid ใน LINE",
            "api_key": api_key_warning or "✅",
        },
    })


# ── Auth ──────────────────────────────────────────────────

@hd_bp.route("/auth/login", methods=["POST"])
def hd_login():
    """Login with username+password OR PIN."""
    import os
    from .auth import verify_password, create_token
    from .config import HdConfig

    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    password = data.get("password", "")
    pin = data.get("pin", "")

    if not pin and (not username or not password):
        return jsonify({"error": "Username and password required"}), 400

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "Database not configured"}), 503

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)

        # ── PIN login ──
        if pin:
            with conn.cursor() as cur:
                # Check if pin_hash column exists
                cur.execute("""SELECT 1 FROM information_schema.columns
                    WHERE table_name='hd_users' AND column_name='pin_hash'""")
                if not cur.fetchone():
                    conn.close()
                    return jsonify({"error": "ระบบ PIN ยังไม่พร้อม — กรุณาใช้ username/password"}), 400

                # Find user by PIN
                cur.execute("""SELECT id, username, pin_hash, full_name, role,
                    is_active, failed_attempts, locked_until
                    FROM hd_users WHERE pin_hash IS NOT NULL AND is_active = TRUE""")
                pin_matched = False
                for prow in cur.fetchall():
                    if verify_password(pin, prow[2]):
                        user_id, uname, _, full_name, role = prow[0], prow[1], prow[2], prow[3], prow[4]
                        is_active, failed_attempts, locked_until = prow[5], prow[6], prow[7]
                        pin_matched = True
                        break
                if not pin_matched:
                    conn.close()
                    return jsonify({"error": "PIN ไม่ถูกต้อง"}), 401

                from datetime import datetime, timezone
                if locked_until and locked_until > datetime.now(timezone.utc):
                    conn.close()
                    return jsonify({"error": "บัญชีถูกล็อกชั่วคราว"}), 403

                # PIN success
                cur.execute("UPDATE hd_users SET failed_attempts = 0, locked_until = NULL, last_login = NOW() WHERE id = %s", (user_id,))
                conn.commit()
            conn.close()
            token = create_token(user_id, uname, role)
            return jsonify({
                "token": token,
                "expires_in": HdConfig.JWT_EXPIRY_MINUTES * 60,
                "user": {"id": user_id, "username": uname, "full_name": full_name, "role": role},
            })

        # ── Username/password login ──
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, password_hash, full_name, role,
                       is_active, failed_attempts, locked_until
                FROM hd_users WHERE username = %s
            """, (username,))
            row = cur.fetchone()

        if not row:
            conn.close()
            log_action(action="login.failed", resource_type="auth",
                       new_values={"username": username, "reason": "not_found"},
                       ip_address=request.remote_addr or "")
            return jsonify({"error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"}), 401

        user_id, uname, pw_hash, full_name, role, is_active, failed_attempts, locked_until = row

        if not is_active:
            conn.close()
            return jsonify({"error": "บัญชีถูกระงับ"}), 403

        # Check lockout
        from datetime import datetime, timezone
        if locked_until and locked_until > datetime.now(timezone.utc):
            conn.close()
            return jsonify({"error": "บัญชีถูกล็อกชั่วคราว กรุณาลองใหม่ภายหลัง"}), 403

        # Master password fallback: if password matches ADMIN_SECRET, re-hash and allow
        import os as _os
        _master = _os.environ.get("ADMIN_SECRET", "")
        _pw_ok = verify_password(password, pw_hash)
        if not _pw_ok and _master and password == _master:
            # ADMIN_SECRET used — update hash so normal login works next time
            from .auth import hash_password as _hp
            with conn.cursor() as cur:
                cur.execute("UPDATE hd_users SET password_hash = %s WHERE id = %s", (_hp(password), user_id))
                conn.commit()
            _pw_ok = True
            logger.info("Login: master password used for %s, hash updated", username)

        if not _pw_ok:
            # Increment failed attempts
            with conn.cursor() as cur:
                new_fails = (failed_attempts or 0) + 1
                lock_until = None
                if new_fails >= 5:
                    lock_until = datetime.now(timezone.utc) + __import__('datetime').timedelta(minutes=15)
                cur.execute("""
                    UPDATE hd_users SET failed_attempts = %s, locked_until = %s
                    WHERE id = %s
                """, (new_fails, lock_until, user_id))
                conn.commit()
            conn.close()
            log_action(action="login.failed", resource_type="auth",
                       new_values={"username": username, "reason": "bad_password"},
                       ip_address=request.remote_addr or "")
            return jsonify({"error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"}), 401

        # Success — reset failed attempts, update last_login
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE hd_users SET failed_attempts = 0, locked_until = NULL,
                       last_login = NOW() WHERE id = %s
            """, (user_id,))
            conn.commit()
        conn.close()

        token = create_token(user_id, uname, role)
        log_action(user_id=user_id, action="login.success", resource_type="auth",
                   ip_address=request.remote_addr or "")

        return jsonify({
            "token": token,
            "expires_in": HdConfig.JWT_EXPIRY_MINUTES * 60,
            "user": {
                "id": user_id,
                "username": uname,
                "full_name": full_name,
                "role": role,
            },
        })

    except Exception as e:
        import traceback
        logger.error("HD login error: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": "ระบบขัดข้อง กรุณาลองใหม่", "detail": str(e)}), 500


@hd_bp.route("/auth/force-reset", methods=["GET", "POST"])
def hd_force_reset():
    """Force reset password — requires ADMIN_SECRET, no login needed.
    GET: /auth/force-reset?secret=XXX&user=hd_admin&pw=newpass
    POST: JSON {admin_secret, username, new_password}
    """
    import os, psycopg2
    from .auth import hash_password
    if request.method == "GET":
        secret = request.args.get("secret", "")
        username = request.args.get("user", "hd_admin")
        new_pw = request.args.get("pw", "")
    else:
        data = request.get_json(silent=True) or {}
        secret = data.get("admin_secret", "")
        username = data.get("username", "hd_admin")
        new_pw = data.get("new_password", "")
    real_secret = os.environ.get("ADMIN_SECRET", "changeme")
    if not secret or secret != real_secret:
        return jsonify({"error": "Invalid admin_secret"}), 403
    if not new_pw:
        new_pw = real_secret
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("UPDATE hd_users SET password_hash = %s, failed_attempts = 0, locked_until = NULL WHERE username = %s",
                        (hash_password(new_pw), username))
            updated = cur.rowcount
            conn.commit()
        conn.close()
        if updated:
            return jsonify({"ok": True, "message": f"Reset '{username}' สำเร็จ"})
        return jsonify({"error": f"ไม่พบ username '{username}'"}), 404
    except Exception as e:
        logger.error("force reset error: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/auth/debug-login", methods=["GET"])
def hd_debug_login():
    """Diagnostic: hd_admin account state. Requires ?secret=ADMIN_SECRET (same guard as force-reset)."""
    import os, psycopg2
    real_secret = os.environ.get("ADMIN_SECRET", "")
    if not real_secret or request.args.get("secret", "") != real_secret:
        return jsonify({"error": "Invalid admin_secret"}), 403
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No DB configured", "HD_DATABASE_URL": bool(os.environ.get("HD_DATABASE_URL")),
                        "DATABASE_URL": bool(os.environ.get("DATABASE_URL"))}), 503
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""SELECT id, username, role, is_active, failed_attempts, locked_until,
                           last_login, password_hash IS NOT NULL as has_pw,
                           LENGTH(password_hash) as pw_len
                           FROM hd_users WHERE username = 'hd_admin'""")
            row = cur.fetchone()
            if not row:
                # Check if any users exist
                cur.execute("SELECT username, role, is_active FROM hd_users LIMIT 10")
                users = [{"u": r[0], "role": r[1], "active": r[2]} for r in cur.fetchall()]
                conn.close()
                return jsonify({"error": "hd_admin NOT FOUND", "existing_users": users,
                                "admin_secret_set": bool(os.environ.get("ADMIN_SECRET"))})
            conn.close()
            from datetime import datetime, timezone
            locked = row[5] and row[5] > datetime.now(timezone.utc)
            return jsonify({
                "user_id": row[0], "username": row[1], "role": row[2],
                "is_active": row[3], "failed_attempts": row[4],
                "locked_until": str(row[5]) if row[5] else None,
                "is_locked_now": locked,
                "last_login": str(row[6]) if row[6] else None,
                "has_password": row[7], "pw_hash_len": row[8],
                "admin_secret_set": bool(os.environ.get("ADMIN_SECRET")),
                "jwt_secret_set": bool(os.environ.get("HD_JWT_SECRET")),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/auth/set-pin", methods=["POST"])
@require_role("physician", "nurse", "admin")
def hd_set_pin():
    """Set or change PIN for quick login."""
    import os, psycopg2
    from .auth import hash_password
    data = request.get_json(silent=True) or {}
    new_pin = data.get("pin", "")
    if not new_pin or len(new_pin) < 4 or len(new_pin) > 8:
        return jsonify({"error": "PIN ต้องมี 4-8 หลัก"}), 400
    if not new_pin.isdigit():
        return jsonify({"error": "PIN ต้องเป็นตัวเลขเท่านั้น"}), 400

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("UPDATE hd_users SET pin_hash = %s WHERE id = %s",
                        (hash_password(new_pin), g.get("hd_user_id")))
            updated = cur.rowcount
            conn.commit()
        conn.close()
        if not updated:
            # 17 พ.ย. 2569: เดิมตอบ "สำเร็จ" แม้ไม่มีแถวไหนถูกแก้ (เช่นเข้าระบบด้วยบัญชีที่ไม่มีในตาราง)
            return jsonify({"error": "ไม่พบบัญชีผู้ใช้ของคุณ — ออกจากระบบแล้วเข้าใหม่ด้วย username/password ก่อนตั้ง PIN"}), 400
        log_action(user_id=g.get("hd_user_id"), action="auth.set_pin", resource_type="user",
                   resource_id=g.get("hd_user_id"), ip_address=request.remote_addr or "")
        return jsonify({"ok": True, "message": "ตั้ง PIN สำเร็จ"})
    except Exception as e:
        logger.error("set pin error: %s", e)
        return jsonify({"error": "ตั้ง PIN ไม่สำเร็จ กรุณาลองใหม่"}), 500


@hd_bp.route("/auth/pin-status", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def hd_pin_status():
    """ผู้ใช้คนนี้ตั้ง PIN ไว้หรือยัง (ไม่ส่งค่า PIN/hash กลับ)"""
    import os
    import psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT username, pin_hash IS NOT NULL FROM hd_users WHERE id = %s", (g.get("hd_user_id"),))
            r = cur.fetchone()
        conn.close()
        if not r:
            return jsonify({"has_pin": False, "found": False})
        return jsonify({"has_pin": bool(r[1]), "found": True, "username": r[0]})
    except Exception as e:
        logger.warning("pin status: %s", e)
        return jsonify({"error": "ตรวจสถานะไม่ได้"}), 500


@hd_bp.route("/auth/refresh", methods=["POST"])
@require_role("physician", "nurse", "admin", "auditor")
def hd_refresh_token():
    """Refresh JWT token — extends session for long nurse shifts."""
    from .auth import create_token, decode_token
    from .config import HdConfig
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    payload = decode_token(token)
    if not payload:
        return jsonify({"error": "Invalid token"}), 401
    new_token = create_token(
        payload["user_id"], payload["username"], payload["role"]
    )
    return jsonify({
        "token": new_token,
        "expires_in": HdConfig.JWT_EXPIRY_MINUTES * 60,
    })


# ── Change Password / User Management ─────────────────────

@hd_bp.route("/auth/change-password", methods=["POST"])
@require_role("physician", "nurse", "admin", "auditor")
def hd_change_password():
    """ผู้ใช้เปลี่ยนรหัสผ่านของตัวเอง — ต้องใส่รหัสเก่าก่อน."""
    import os, psycopg2
    from .auth import verify_password, hash_password

    data = request.get_json(silent=True) or {}
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")

    if not old_pw or not new_pw:
        return jsonify({"error": "กรุณาระบุรหัสผ่านเก่าและรหัสผ่านใหม่"}), 400
    if len(new_pw) < 4:
        return jsonify({"error": "รหัสผ่านใหม่ต้องมีอย่างน้อย 4 ตัวอักษร"}), 400

    user_id = g.get("hd_user_id")
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM hd_users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "ไม่พบบัญชีผู้ใช้"}), 404
            if not verify_password(old_pw, row[0]):
                conn.close()
                return jsonify({"error": "รหัสผ่านเก่าไม่ถูกต้อง"}), 401
            cur.execute("UPDATE hd_users SET password_hash = %s WHERE id = %s",
                        (hash_password(new_pw), user_id))
            conn.commit()
        conn.close()
        log_action(user_id=user_id, action="auth.change_password", resource_type="user",
                   resource_id=user_id, ip_address=request.remote_addr or "")
        return jsonify({"ok": True, "message": "เปลี่ยนรหัสผ่านสำเร็จ ✅"})
    except Exception as e:
        logger.error("change password error: %s", e)
        return jsonify({"error": "เปลี่ยนรหัสผ่านไม่สำเร็จ กรุณาลองใหม่"}), 500


@hd_bp.route("/auth/reset-password", methods=["POST"])
@require_role("admin")
def hd_reset_password():
    """Admin reset รหัสผ่านให้ผู้ใช้คนอื่น."""
    import os, psycopg2
    from .auth import hash_password

    data = request.get_json(silent=True) or {}
    target_user_id = data.get("user_id")
    new_pw = data.get("new_password", "")

    if not target_user_id or not new_pw:
        return jsonify({"error": "ต้องระบุ user_id และ new_password"}), 400
    if len(new_pw) < 4:
        return jsonify({"error": "รหัสผ่านต้องมีอย่างน้อย 4 ตัวอักษร"}), 400

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("UPDATE hd_users SET password_hash = %s, failed_attempts = 0, locked_until = NULL WHERE id = %s",
                        (hash_password(new_pw), target_user_id))
            if cur.rowcount == 0:
                conn.close()
                return jsonify({"error": "ไม่พบผู้ใช้ ID นี้"}), 404
            conn.commit()
        conn.close()
        log_action(user_id=g.get("hd_user_id"), action="admin.reset_password", resource_type="user",
                   resource_id=target_user_id, ip_address=request.remote_addr or "")
        return jsonify({"ok": True, "message": "Reset รหัสผ่านสำเร็จ ✅"})
    except Exception as e:
        logger.error("reset password error: %s", e)
        return jsonify({"error": "Reset ไม่สำเร็จ"}), 500


@hd_bp.route("/auth/users", methods=["GET"])
@require_role("admin")
def hd_list_users():
    """Admin ดูรายชื่อผู้ใช้ทั้งหมด."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""SELECT id, username, full_name, role, is_active,
                           last_login, license_number
                           FROM hd_users ORDER BY role, username""")
            rows = cur.fetchall()
        conn.close()
        users = [{"id": r[0], "username": r[1], "full_name": r[2], "role": r[3],
                  "is_active": r[4], "last_login": r[5].isoformat() if r[5] else None,
                  "license_number": r[6] or ""} for r in rows]
        return jsonify({"users": users})
    except Exception as e:
        logger.error("list users error: %s", e)
        return jsonify({"error": "โหลดรายชื่อไม่ได้"}), 500


@hd_bp.route("/auth/users", methods=["POST"])
@require_role("admin")
def hd_create_user():
    """Admin สร้างบัญชีผู้ใช้ใหม่."""
    import os, psycopg2
    from .auth import hash_password

    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    full_name = data.get("full_name", "").strip()
    role = data.get("role", "nurse")
    license_number = data.get("license_number", "").strip()

    if not username or not password:
        return jsonify({"error": "ต้องระบุ username และ password"}), 400
    if len(password) < 4:
        return jsonify({"error": "รหัสผ่านต้องมีอย่างน้อย 4 ตัวอักษร"}), 400
    if role not in ("physician", "nurse", "admin", "auditor"):
        return jsonify({"error": "role ต้องเป็น physician/nurse/admin/auditor"}), 400

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM hd_users WHERE username = %s", (username,))
            if cur.fetchone():
                conn.close()
                return jsonify({"error": f"ชื่อผู้ใช้ '{username}' มีอยู่แล้ว"}), 409
            cur.execute("""INSERT INTO hd_users (username, password_hash, full_name, role, license_number, is_active)
                           VALUES (%s, %s, %s, %s::hd_user_role, %s, TRUE) RETURNING id""",
                        (username, hash_password(password), full_name, role, license_number))
            new_id = cur.fetchone()[0]
            conn.commit()
        conn.close()
        log_action(user_id=g.get("hd_user_id"), action="admin.create_user", resource_type="user",
                   resource_id=new_id, new_values={"username": username, "role": role},
                   ip_address=request.remote_addr or "")
        return jsonify({"ok": True, "user_id": new_id, "message": f"สร้างบัญชี '{username}' สำเร็จ ✅"})
    except Exception as e:
        logger.error("create user error: %s", e)
        return jsonify({"error": "สร้างบัญชีไม่สำเร็จ"}), 500


@hd_bp.route("/auth/users/<int:user_id>/toggle", methods=["POST"])
@require_role("admin")
def hd_toggle_user(user_id):
    """Admin ระงับ/เปิดบัญชีผู้ใช้."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT username, is_active FROM hd_users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "ไม่พบผู้ใช้"}), 404
            new_active = not row[1]
            cur.execute("UPDATE hd_users SET is_active = %s WHERE id = %s", (new_active, user_id))
            conn.commit()
        conn.close()
        status = "เปิดใช้งาน" if new_active else "ระงับ"
        log_action(user_id=g.get("hd_user_id"), action="admin.toggle_user", resource_type="user",
                   resource_id=user_id, new_values={"is_active": new_active},
                   ip_address=request.remote_addr or "")
        return jsonify({"ok": True, "is_active": new_active, "message": f"{status}บัญชี '{row[0]}' แล้ว ✅"})
    except Exception as e:
        logger.error("toggle user error: %s", e)
        return jsonify({"error": "ดำเนินการไม่สำเร็จ"}), 500


@hd_bp.route("/auth/users/<int:user_id>", methods=["DELETE"])
@require_role("admin")
def hd_delete_user(user_id):
    """Admin ลบบัญชีผู้ใช้ (ถาวร)."""
    import os, psycopg2
    admin_id = g.get("hd_user_id")
    if user_id == admin_id:
        return jsonify({"error": "ไม่สามารถลบบัญชีตัวเอง"}), 400
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM hd_users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "ไม่พบผู้ใช้"}), 404
            cur.execute("DELETE FROM hd_users WHERE id = %s", (user_id,))
            conn.commit()
        conn.close()
        log_action(user_id=admin_id, action="admin.delete_user", resource_type="user",
                   resource_id=user_id, new_values={"username": row[0]},
                   ip_address=request.remote_addr or "")
        return jsonify({"ok": True, "message": f"ลบบัญชี '{row[0]}' แล้ว ✅"})
    except Exception as e:
        logger.error("delete user error: %s", e)
        return jsonify({"error": "ลบไม่สำเร็จ"}), 500


# ── Sessions ──────────────────────────────────────────────

@hd_bp.route("/sessions", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def list_sessions():
    """List HD sessions (filterable by date, bed, patient)."""
    session_date = request.args.get("date", _thai_today().isoformat())
    bed = request.args.get("bed", type=int)
    patient_hn = request.args.get("hn")

    log_action(
        user_id=g.get("hd_user_id"),
        action="session.list",
        resource_type="session",
        ip_address=request.remote_addr or "",
    )

    # Placeholder: database query goes here
    return jsonify({
        "sessions": [],
        "filters": {"date": session_date, "bed": bed, "hn": patient_hn},
        "total": 0,
    })



def _next_dialyzer_reuse(entered, d_type, existing):
    """reuse ครั้งที่ของรอบนี้: ใช้ค่าที่พยาบาลกรอก ถ้าไม่กรอก → นับต่อจากรอบก่อนเมื่อเป็นตัวกรองชนิดเดิม
    (ครบ max แล้ว หรือเปลี่ยนชนิด → เริ่ม 1). existing = (id, type, reuse_count, max_reuse) | None"""
    if entered and entered > 0:
        return entered
    if existing and existing[1] == d_type:
        last, mx = (existing[2] or 0), (existing[3] or 20)
        return 1 if last >= mx else last + 1
    return 1


def _vital_int(v, lo, hi):
    try:
        x = int(float(v))
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def _vital_float(v, lo, hi):
    try:
        x = round(float(v), 1)
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def _save_pre_hd(cur, session_id, data):
    """Pre-HD vitals → hd_vital_signs(phase='pre'); symptoms/dialyzer/heparin/access → hd_sessions."""
    sbp, dbp = _vital_int(data.get("pre_sbp"), 30, 300), _vital_int(data.get("pre_dbp"), 20, 200)
    hr = _vital_int(data.get("pre_hr"), 20, 250)
    temp, spo2 = _vital_float(data.get("pre_temp"), 30, 45), _vital_int(data.get("pre_spo2"), 50, 100)
    sym = data.get("pre_symptoms")
    sym_txt = ", ".join(str(x)[:40] for x in sym)[:500] if isinstance(sym, list) else (str(sym)[:500] if sym else None)
    if any(v is not None for v in (sbp, dbp, hr, temp, spo2)):
        cur.execute("SAVEPOINT sp_pre_vital")
        try:
            cur.execute("""
                INSERT INTO hd_vital_signs (session_id, phase, sbp, dbp, heart_rate, temperature, spo2,
                                            symptoms, nurse_id, recorded_at)
                VALUES (%s, 'pre', %s, %s, %s, %s, %s, %s, %s, NOW())
            """, (session_id, sbp, dbp, hr, temp, spo2, sym_txt, _safe_int(g.get("hd_user_id"))))
            cur.execute("RELEASE SAVEPOINT sp_pre_vital")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT sp_pre_vital")
            logger.warning("pre vital save error: %s", e)
    hep = None
    if data.get("hep_drug"):
        hep = str(data.get("hep_drug"))[:20]
        if data.get("hep_bolus"):
            hep += f" {_safe_int(data.get('hep_bolus'))}"
        if data.get("hep_maintenance"):
            hep += f"+{_safe_int(data.get('hep_maintenance'))}/h"
    cur.execute("SAVEPOINT sp_pre_sess")
    try:
        cur.execute("""
            UPDATE hd_sessions SET pre_symptoms = COALESCE(%s, pre_symptoms),
                   dialyzer_type = COALESCE(%s, dialyzer_type),
                   dialyzer_reuse = COALESCE(%s, dialyzer_reuse),
                   access_type = COALESCE(%s, access_type),
                   heparin_dose = COALESCE(%s, heparin_dose)
            WHERE id = %s
        """, (sym_txt, (str(data["dialyzer_type"])[:50] if data.get("dialyzer_type") else None),
              _safe_int(data.get("dialyzer_reuse")) or None,
              (str(data["vascular_access"])[:30] if data.get("vascular_access") else None),
              (hep[:50] if hep else None), session_id))
        cur.execute("RELEASE SAVEPOINT sp_pre_sess")
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT sp_pre_sess")
        logger.warning("pre session fields save error: %s", e)


@hd_bp.route("/sessions", methods=["POST"])
@require_role("physician", "nurse", "admin")
def create_session():
    """Create a new HD session (start dialysis)."""
    data = request.get_json(silent=True) or {}

    required = ["patient_hn", "bed_number"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    bed = data.get("bed_number")
    if not isinstance(bed, int) or bed < 1 or bed > HdConfig.TOTAL_BEDS:
        return jsonify({"error": "bed_number must be 1-50"}), 400

    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "Database not configured"}), 500

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Find patient_id from HN
            cur.execute("SELECT id FROM hd_patient_mapping WHERE hn = %s", (data["patient_hn"],))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": f"Patient HN {data['patient_hn']} not found"}), 404
            patient_id = row[0]

            # Check bed availability:
            # - Can't start 2 in_progress on same bed
            # - Can schedule (รอบถัดไป) even if bed has in_progress
            # - Can't double-schedule same bed same status
            is_schedule = data.get("schedule_only", False)
            init_status = "scheduled" if is_schedule else "in_progress"

            shift = data.get("shift", "morning")
            if shift not in ("morning", "afternoon", "evening"):
                shift = "morning"
            # จองล่วงหน้า: ระบุวันที่ได้ (เฉพาะการจอง ไม่ใช่การเริ่มฟอก) — วันนี้ถึง 60 วันข้างหน้า
            sess_day = _thai_today()
            if is_schedule and data.get("session_date"):
                try:
                    from datetime import date as _date
                    sess_day = _date.fromisoformat(str(data["session_date"])[:10])
                except ValueError:
                    conn.close()
                    return jsonify({"error": "รูปแบบวันที่ไม่ถูกต้อง"}), 400
                if sess_day < _thai_today() or sess_day > _thai_today() + timedelta(days=60):
                    conn.close()
                    return jsonify({"error": "จองได้ตั้งแต่วันนี้ถึง 60 วันข้างหน้า"}), 400
            if is_schedule:
                cur.execute("""SELECT bed_number, COALESCE(shift,'morning') FROM hd_sessions
                               WHERE patient_id = %s AND session_date = %s
                                 AND status IN ('scheduled', 'in_progress') LIMIT 1""",
                            (patient_id, sess_day.isoformat()))
                dup = cur.fetchone()
                if dup:
                    conn.close()
                    sn = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}.get(dup[1], dup[1])
                    return jsonify({"error": f"ผู้ป่วยมีรอบวันที่ {sess_day.strftime('%d/%m')} แล้ว (เตียง {dup[0]} รอบ{sn})"}), 409
            # ⚠️ ป้องกันเตียงซ้อน:
            # 1. in_progress: เช็คทุกรอบ — เตียงยังไม่ว่างจนกว่าจะฟอกเสร็จ
            # 2. scheduled: เช็คเฉพาะรอบเดียวกัน
            cur.execute("""
                SELECT id, status, shift FROM hd_sessions
                WHERE bed_number = %s
                  AND session_date = %s
                  AND status IN ('scheduled', 'in_progress')
                ORDER BY status
            """, (bed, sess_day.isoformat()))
            existing = cur.fetchall()
            for ex_id, ex_status, ex_shift in existing:
                if ex_status == "in_progress":
                    # เตียงมีคนฟอกอยู่ — ห้ามเริ่มรอบใหม่ ไม่ว่าจะรอบไหน
                    sn = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}.get(ex_shift, ex_shift or "")
                    conn.close()
                    return jsonify({"error": f"เตียง {bed} มีคนไข้กำลังฟอกอยู่ (รอบ{sn})"}), 409
                if init_status == "scheduled" and ex_status == "scheduled" and (ex_shift == shift or (ex_shift is None and shift == 'morning')):
                    conn.close()
                    return jsonify({"error": f"เตียง {bed} วันที่ {sess_day.strftime('%d/%m')} รอบนี้มีคนจองแล้ว"}), 409

            # Insert session with shift
            started = "NULL" if is_schedule else "NOW()"
            shift = data.get("shift", "morning")
            if shift not in ("morning", "afternoon", "evening"):
                shift = "morning"
            # Auto-calculate UF Goal from pre_weight - dry_weight
            pre_w = data.get("pre_weight")
            dry_w = data.get("dry_weight")
            uf_goal = data.get("uf_goal")
            if not uf_goal and pre_w and dry_w:
                try:
                    calc_uf = round((float(pre_w) - float(dry_w)) * 1000)
                    if calc_uf > 0:
                        uf_goal = calc_uf
                except (ValueError, TypeError):
                    pass
            if not uf_goal or uf_goal <= 0:
                uf_goal = 2000  # fallback only when no weights given

            # Auto-link equipment: ดึงเครื่องฟอก active ที่ผูกเตียงนี้
            _eq_id = data.get("equipment_id")  # manual override
            if not _eq_id:
                cur.execute("SAVEPOINT sp_eq_lookup")
                try:
                    cur.execute("""SELECT id FROM hd_equipment
                                   WHERE bed_number = %s AND equipment_type = 'dialysis_machine'
                                     AND status = 'active' LIMIT 1""", (bed,))
                    _eq_row = cur.fetchone()
                    if _eq_row:
                        _eq_id = _eq_row[0]
                    cur.execute("RELEASE SAVEPOINT sp_eq_lookup")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_eq_lookup")

            try:
                cur.execute(f"""
                    INSERT INTO hd_sessions
                        (patient_id, bed_number, session_date, status,
                         pre_weight, post_weight, uf_goal,
                         started_at, nurse_id, shift, equipment_id)
                    VALUES (%s, %s, %s, '{init_status}',
                            %s, NULL, %s,
                            {started}, %s, %s, %s)
                    RETURNING id
                """, (
                    patient_id, bed,
                    sess_day.isoformat(),
                    pre_w,
                    uf_goal,
                    g.get("hd_user_id"),
                    shift,
                    _eq_id,
                ))
            except Exception as ins_err:
                # unique index ux_hd_sessions_bed_active: 2 คนกดลงเตียงเดียวกันพร้อมกันเป๊ะ
                if "ux_hd_sessions_bed_active" in str(ins_err) or "duplicate key" in str(ins_err).lower():
                    conn.close()
                    return jsonify({"error": f"เตียง {bed} ถูกจองพร้อมกันโดยผู้ใช้อีกคน — รีเฟรช dashboard แล้วเลือกเตียงใหม่"}), 409
                raise
            session_id = cur.fetchone()[0]

            # Save Dialyzer tracking (upsert active dialyzer).
            # Each optional save runs inside a SAVEPOINT: in PostgreSQL a failed
            # statement aborts the whole transaction, which previously made the
            # session INSERT itself roll back whenever these failed.
            d_type = data.get("dialyzer_type")
            if d_type:
                d_max = _safe_int(data.get("dialyzer_max")) or 20
                cur.execute("SAVEPOINT sp_dialyzer")
                try:
                    cur.execute("""
                        SELECT id, dialyzer_type, reuse_count, max_reuse FROM hd_dialyzer_tracking
                        WHERE patient_id = %s AND status = 'active'
                        ORDER BY id DESC LIMIT 1
                    """, (patient_id,))
                    existing = cur.fetchone()
                    d_reuse = _next_dialyzer_reuse(_safe_int(data.get("dialyzer_reuse")), d_type, existing)
                    data["dialyzer_reuse"] = d_reuse
                    if existing:
                        cur.execute("""
                            UPDATE hd_dialyzer_tracking
                            SET dialyzer_type = %s, reuse_count = %s, max_reuse = %s,
                                last_used_session = %s
                            WHERE id = %s
                        """, (d_type, d_reuse, d_max, session_id, existing[0]))
                    else:
                        cur.execute("""
                            INSERT INTO hd_dialyzer_tracking
                                (patient_id, dialyzer_type, reuse_count, max_reuse,
                                 status, last_used_session, first_use_date)
                            VALUES (%s, %s, %s, %s, 'active', %s, CURRENT_DATE)
                        """, (patient_id, d_type, d_reuse, d_max, session_id))
                    cur.execute("RELEASE SAVEPOINT sp_dialyzer")
                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_dialyzer")
                    logger.warning("dialyzer save error: %s", e)

            # Save Heparin protocol
            hep_drug = data.get("hep_drug")
            if hep_drug:
                cur.execute("SAVEPOINT sp_heparin")
                try:
                    cur.execute("""
                        INSERT INTO hd_anticoagulation
                            (session_id, protocol, loading_dose, maintenance_dose,
                             recorded_by, recorded_at)
                        VALUES (%s, %s, %s, %s, %s, NOW())
                    """, (session_id, str(hep_drug)[:30],
                          _safe_int(data.get("hep_bolus")),
                          _safe_int(data.get("hep_maintenance")),
                          _safe_int(g.get("hd_user_id"))))
                    cur.execute("RELEASE SAVEPOINT sp_heparin")
                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_heparin")
                    logger.warning("heparin save error: %s", e)

            # 16 พ.ย. 2569: เก็บข้อมูล Pre-HD ทั้งหมดไว้กับรอบนี้ (เดิม pre BP/HR/อาการ/ตัวกรอง ไม่ถูกบันทึก
            # → เปิดดูย้อนหลังแล้วว่าง). แต่ละส่วนอยู่ใน SAVEPOINT — ล้มแล้วไม่กระทบการเริ่มรอบ
            if not is_schedule:
                _save_pre_hd(cur, session_id, data)

            # Update VA + VA Location on patient record if provided
            # ⚠️ MUST use SAVEPOINT: if va_location column doesn't exist, a bare
            # UPDATE would abort the entire transaction and roll back the session INSERT.
            va = data.get("vascular_access")
            va_loc = data.get("va_location")
            if va or va_loc:
                cur.execute("SAVEPOINT sp_va_update")
                try:
                    updates = []
                    params = []
                    if va:
                        updates.append("vascular_access = %s")
                        params.append(va)
                    if va_loc:
                        updates.append("va_location = %s")
                        params.append(va_loc)
                    params.append(patient_id)
                    cur.execute(f"""
                        UPDATE hd_patient_mapping
                        SET {', '.join(updates)} WHERE id = %s
                    """, params)
                    cur.execute("RELEASE SAVEPOINT sp_va_update")
                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_va_update")
                    logger.warning("VA update error (rolled back savepoint): %s", e)

        conn.commit()
        conn.close()

        log_action(
            user_id=g.get("hd_user_id"),
            action="session.create",
            resource_type="session",
            resource_id=session_id,
            new_values=data,
            ip_address=request.remote_addr or "",
        )

        # Fetch ESA drug for the UI session page
        _esa_drug = None
        try:
            import psycopg2 as _pg2
            _ec = _pg2.connect(db_url, connect_timeout=3)
            with _ec.cursor() as _ecur:
                _ecur.execute("SELECT esa_drug FROM hd_patient_mapping WHERE id = %s", (patient_id,))
                _er = _ecur.fetchone()
                if _er:
                    _esa_drug = _er[0]
            _ec.close()
        except Exception:
            pass

        return jsonify({
            "status": "created",
            "session_id": session_id,
            "bed_number": bed,
            "patient_hn": data["patient_hn"],
            "esa_drug": _esa_drug,
        }), 201

    except Exception as e:
        logger.error("create_session error: %s", e)
        return jsonify({"error": str(e)}), 500


def _session_record_extras(cur, session_id, patient_id):
    """ข้อมูลประกอบการดูรอบฟอกย้อนหลัง (16 พ.ย. 2569) — แต่ละส่วนล้มได้โดยไม่กระทบส่วนอื่น"""
    out = {}

    def q(name, sql, args, fn):
        cur.execute(f"SAVEPOINT sp_x_{name}")
        try:
            cur.execute(sql, args)
            fn(cur)
            cur.execute(f"RELEASE SAVEPOINT sp_x_{name}")
        except Exception as e:
            cur.execute(f"ROLLBACK TO SAVEPOINT sp_x_{name}")
            logger.debug("session extras %s: %s", name, e)

    def _sess(c):
        r = c.fetchone()
        if r:
            out.update({"shift": r[0], "dialyzer_type": r[1], "dialyzer_reuse": r[2], "access_type": r[3],
                        "heparin_dose": r[4], "pre_symptoms": r[5], "post_symptoms": r[6],
                        "esa_given": r[7], "esa_drug_given": r[8], "esa_dose_given": r[9], "esa_route": r[10],
                        "esa_skip_reason": r[11], "hb_value": float(r[12]) if r[12] is not None else None,
                        "hct_value": float(r[13]) if r[13] is not None else None,
                        "kt_v": float(r[14]) if r[14] is not None else None,
                        "urr": float(r[15]) if r[15] is not None else None})
    q("sess", """SELECT shift, dialyzer_type, dialyzer_reuse, access_type, heparin_dose, pre_symptoms,
                        post_symptoms, esa_given, esa_drug_given, esa_dose_given, esa_route, esa_skip_reason,
                        hb_value, hct_value, kt_v, urr
                 FROM hd_sessions WHERE id = %s""", (session_id,), _sess)

    def _ph(c):
        for r in c.fetchall():
            out.setdefault(f"{r[0]}_vitals", {
                "sbp": r[1], "dbp": r[2], "hr": r[3], "temp": float(r[4]) if r[4] is not None else None,
                "spo2": r[5], "symptoms": r[6], "recorded_at": (r[7].isoformat() + "Z") if r[7] else None})
    # pre = แถวแรกของ phase pre, post = แถวล่าสุดของ phase post
    q("pre", """SELECT phase, sbp, dbp, heart_rate, temperature, spo2, symptoms, recorded_at FROM hd_vital_signs
                WHERE session_id = %s AND phase = 'pre' ORDER BY recorded_at ASC LIMIT 1""", (session_id,), _ph)
    q("post", """SELECT phase, sbp, dbp, heart_rate, temperature, spo2, symptoms, recorded_at FROM hd_vital_signs
                 WHERE session_id = %s AND phase = 'post' ORDER BY recorded_at DESC LIMIT 1""", (session_id,), _ph)

    def _hep(c):
        r = c.fetchone()
        if r:
            out["heparin"] = {"protocol": r[0], "bolus": r[1], "maintenance": r[2]}
    q("hep", """SELECT protocol, loading_dose, maintenance_dose FROM hd_anticoagulation
                WHERE session_id = %s ORDER BY id DESC LIMIT 1""", (session_id,), _hep)

    def _dz(c):
        r = c.fetchone()
        if r:
            if not out.get("dialyzer_type"):
                out["dialyzer_type"] = r[0]
            if not out.get("dialyzer_reuse"):
                out["dialyzer_reuse"] = r[1]
            out["dialyzer_max"] = r[2]
    q("dz", """SELECT dialyzer_type, reuse_count, max_reuse FROM hd_dialyzer_tracking
               WHERE last_used_session = %s ORDER BY id DESC LIMIT 1""", (session_id,), _dz)

    def _va(c):
        r = c.fetchone()
        if r:
            out["va_location"] = r[0]
    q("va", "SELECT va_location FROM hd_patient_mapping WHERE id = %s", (patient_id,), _va)

    def _machine(c):
        r = c.fetchone()
        if r:
            out["machine"] = {"id": r[0], "brand": r[1], "model": r[2], "serial": r[3], "status": r[4]}
    q("machine", """SELECT e.id, e.brand, e.model, e.serial_number, e.status
                    FROM hd_equipment e JOIN hd_sessions s ON e.id = s.equipment_id
                    WHERE s.id = %s""", (session_id,), _machine)
    if "machine" not in out:
        # fallback: ดึงจาก bed_number mapping (session เก่าที่ยังไม่มี equipment_id)
        def _machine_bed(c):
            r = c.fetchone()
            if r:
                out["machine"] = {"id": r[0], "brand": r[1], "model": r[2], "serial": r[3], "status": r[4]}
        q("machine_bed", """SELECT e.id, e.brand, e.model, e.serial_number, e.status
                            FROM hd_equipment e, hd_sessions s
                            WHERE s.id = %s AND e.bed_number = s.bed_number
                              AND e.equipment_type = 'dialysis_machine' AND e.status = 'active'
                            LIMIT 1""", (session_id,), _machine_bed)

    def _nurse(c):
        r = c.fetchone()
        if r:
            out["nurse_name"] = r[0]
    q("nurse", """SELECT COALESCE(u.full_name, u.username) FROM hd_sessions s JOIN hd_users u ON u.id = s.nurse_id
                  WHERE s.id = %s""", (session_id,), _nurse)
    return out


@hd_bp.route("/sessions/<int:session_id>", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def get_session(session_id):
    """Get details of a specific HD session."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"session_id": session_id, "status": "not_found"}), 404
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.bed_number, s.session_date, s.status,
                       s.pre_weight, s.post_weight, s.actual_uf, s.uf_goal,
                       s.started_at, s.ended_at, s.notes,
                       p.hn, p.patient_name, p.vascular_access, p.schedule_days,
                       p.dry_weight, p.esa_drug, p.id
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE s.id = %s
            """, (session_id,))
            row = cur.fetchone()
            extra = _session_record_extras(cur, session_id, row[17]) if row else {}
        conn.close()
        if not row:
            return jsonify({"session_id": session_id, "status": "not_found"}), 404
        return jsonify({
            **extra,
            "session_id": row[0], "bed_number": row[1],
            "session_date": row[2].isoformat() if row[2] else None,
            "status": row[3], "pre_weight": float(row[4]) if row[4] else None,
            "post_weight": float(row[5]) if row[5] else None,
            "actual_uf": float(row[6]) if row[6] else None,
            "uf_goal": float(row[7]) if row[7] else 2000,
            "started_at": (row[8].isoformat() + "Z") if row[8] else None,
            "ended_at": (row[9].isoformat() + "Z") if row[9] else None,
            "notes": row[10],
            "patient_hn": row[11], "patient_name": row[12],
            "vascular_access": row[13], "schedule_days": row[14],
            "dry_weight": float(row[15]) if row[15] else None,
            "esa_drug": row[16], "duration_hours": 4, "patient_id": row[17],
        })
    except Exception as e:
        logger.error("get_session error: %s", e)
        return jsonify({"session_id": session_id, "error": str(e)}), 500

@hd_bp.route("/sessions/<int:session_id>/detail", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def get_session_detail(session_id):
    """Alias for get_session — used by JS loadExistingSession."""
    return get_session(session_id)


@hd_bp.route("/sessions/<int:session_id>", methods=["PUT"])
@require_role("physician", "nurse", "admin")
def update_session(session_id):
    """Update an HD session (e.g. end dialysis, record post-weight)."""
    data = request.get_json(silent=True) or {}

    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if db_url:
        try:
            import psycopg2
            conn = psycopg2.connect(db_url, connect_timeout=5)
            with conn.cursor() as cur:
                updates = []
                params = []
                if data.get("status") == "completed":
                    updates.append("status = 'completed'")
                    updates.append("ended_at = NOW()")
                elif data.get("status") == "cancelled":
                    # (3) กันยกเลิกพลาด: มีบันทึก vital/UF แล้ว → ต้องจบรอบก่อนกำหนดแทน ไม่ให้ข้อมูลการฟอกหาย
                    cur.execute("""
                        SELECT (SELECT COUNT(*) FROM hd_vital_signs WHERE session_id = %s)
                             + (SELECT COUNT(*) FROM hd_uf_records WHERE session_id = %s)
                    """, (session_id, session_id))
                    _nrec = cur.fetchone()[0] or 0
                    if _nrec > 0:
                        conn.close()
                        return jsonify({"error": f"รอบนี้มีบันทึกระหว่างฟอกแล้ว {_nrec} รายการ — ยกเลิกเฉยๆ ไม่ได้ ให้ใช้ 'จบรอบก่อนกำหนด' ที่แท็บ Post-HD แทน",
                                        "has_records": int(_nrec), "use_early_end": True}), 409
                    updates.append("status = 'cancelled'")
                    updates.append("ended_at = NOW()")
                    if data.get("cancel_reason"):
                        updates.append("cancel_reason = %s")
                        params.append(str(data["cancel_reason"])[:200])
                if data.get("post_weight") is not None:
                    updates.append("post_weight = %s")
                    params.append(data["post_weight"])
                    # Auto-calculate actual_uf = (pre_weight - post_weight) × 1000
                    # เมื่อกรอก post_weight แต่ไม่ได้ส่ง actual_uf มาด้วย
                    if data.get("actual_uf") is None:
                        try:
                            cur.execute("SELECT pre_weight FROM hd_sessions WHERE id = %s", (session_id,))
                            _pw_row = cur.fetchone()
                            if _pw_row and _pw_row[0] is not None:
                                _auto_uf = round((float(_pw_row[0]) - float(data["post_weight"])) * 1000)
                                if _auto_uf > 0:
                                    updates.append("actual_uf = %s")
                                    params.append(_auto_uf)
                                    logger.info(f"[UF-AUTO] session {session_id}: pre={_pw_row[0]} post={data['post_weight']} → UF={_auto_uf} mL")
                        except Exception as _uf_e:
                            logger.warning(f"[UF-AUTO] calc failed: {_uf_e}")
                if data.get("actual_uf") is not None:
                    updates.append("actual_uf = %s")
                    params.append(data["actual_uf"])
                if data.get("notes"):
                    updates.append("notes = %s")
                    params.append(data["notes"])
                if data.get("shift") and data["shift"] in ("morning", "afternoon", "evening"):
                    updates.append("shift = %s")
                    params.append(data["shift"])
                if data.get("bed_number"):
                    updates.append("bed_number = %s")
                    params.append(int(data["bed_number"]))
                # ESA injection tracking
                if "esa_given" in data:
                    updates.append("esa_given = %s")
                    params.append(bool(data["esa_given"]))
                if data.get("esa_drug_given"):
                    updates.append("esa_drug_given = %s")
                    params.append(data["esa_drug_given"])
                if data.get("esa_dose_given"):
                    updates.append("esa_dose_given = %s")
                    params.append(data["esa_dose_given"])
                if data.get("esa_route"):
                    updates.append("esa_route = %s")
                    params.append(data["esa_route"])
                if data.get("esa_skip_reason"):
                    updates.append("esa_skip_reason = %s")
                    params.append(data["esa_skip_reason"])
                # Lab values
                if data.get("hb_value") is not None:
                    updates.append("hb_value = %s")
                    params.append(data["hb_value"])
                if data.get("hct_value") is not None:
                    updates.append("hct_value = %s")
                    params.append(data["hct_value"])
                if updates:
                    updates.append("updated_at = NOW()")
                    params.append(session_id)
                    cur.execute(
                        f"UPDATE hd_sessions SET {', '.join(updates)} WHERE id = %s",
                        params
                    )
                # อาการหลังฟอก — แยก SAVEPOINT ไม่ให้กระทบการปิดรอบ
                if isinstance(data.get("post_symptoms"), list):
                    cur.execute("SAVEPOINT sp_post_sym")
                    try:
                        cur.execute("UPDATE hd_sessions SET post_symptoms = %s WHERE id = %s",
                                    (", ".join(str(x)[:40] for x in data["post_symptoms"])[:500] or None, session_id))
                        cur.execute("RELEASE SAVEPOINT sp_post_sym")
                    except Exception as e:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_post_sym")
                        logger.warning("post symptoms save error: %s", e)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("update_session error: %s", e)

    log_action(
        user_id=g.get("hd_user_id"),
        action="session.update",
        resource_type="session",
        resource_id=session_id,
        new_values=data,
        ip_address=request.remote_addr or "",
    )
    result = {"session_id": session_id, "updated": True}

    # Generate summary if requested
    if data.get("generate_summary") and data.get("status") == "completed":
        result["summary"] = _build_session_summary(session_id, data)

    # Check ESA alert: patient has prescribed ESA but didn't get it and no reason given
    if data.get("status") == "completed":
        _check_esa_missing_alert(session_id)
        # Auto-generate Work Sheet PDF + upload GCS + LINE notify
        try:
            result["worksheet"] = _auto_worksheet_on_complete(session_id)
        except Exception as e:
            logger.warning("auto worksheet: %s", e)
        # B8: when this was the last session of the shift → shift summary to the nurse group
        try:
            result["shift_summary"] = _maybe_send_shift_summary(session_id)
        except Exception as e:
            logger.warning("shift summary: %s", e)
        # ว้าว #7: post-HD summary to the patient's LINE (toggle: patient_summary_auto)
        try:
            if (get_setting_value("patient_summary_auto", "on") or "on").lower() != "off":
                result["patient_summary"] = send_patient_summary(session_id, trigger="auto")
        except Exception as e:
            logger.warning("patient summary: %s", e)
        # Auto Kt/V calculation if pre/post BUN available
        try:
            result["kt_v"] = _auto_kt_v_on_complete(session_id)
        except Exception as e:
            logger.warning("auto kt_v: %s", e)

    return jsonify(result)


def _auto_kt_v_on_complete(session_id):
    """Auto-calculate Kt/V when session completes and BUN data available."""
    import os, psycopg2
    from .kt_v_calculator import calculate_kt_v
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return None
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT s.pre_weight, s.post_weight, s.patient_id,
                                  s.started_at, s.ended_at
                           FROM hd_sessions s WHERE s.id = %s""", (session_id,))
            s = cur.fetchone()
            if not s or not s[0] or not s[1]:
                return None
            pre_w, post_w, pid = float(s[0]), float(s[1]), s[2]
            # Calculate duration
            duration = 4.0  # default
            if s[3] and s[4]:
                dur = (s[4] - s[3]).total_seconds() / 3600
                if 1 <= dur <= 8:
                    duration = round(dur, 1)
            # Look for latest BUN from unified lab VIEW
            cur.execute("""SELECT bun FROM hd_labs
                           WHERE patient_id = %s AND bun IS NOT NULL
                           ORDER BY lab_date DESC LIMIT 1""", (pid,))
            pre_bun_row = cur.fetchone()
            # Post-HD BUN not stored separately — Kt/V auto-calc skips
            post_bun_row = None
            if not pre_bun_row or not post_bun_row or not pre_bun_row[0] or not post_bun_row[0]:
                return {"skipped": "no pre/post BUN data"}
            result = calculate_kt_v(
                pre_bun=float(pre_bun_row[0]),
                post_bun=float(post_bun_row[0]),
                duration_hours=duration,
                pre_weight=pre_w,
                post_weight=post_w,
            )
            if result.get("kt_v"):
                # Save to session
                cur.execute("SAVEPOINT sp_ktv")
                try:
                    cur.execute("""UPDATE hd_sessions SET kt_v = %s, urr = %s WHERE id = %s""",
                                (result["kt_v"], result.get("urr"), session_id))
                    cur.execute("RELEASE SAVEPOINT sp_ktv")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_ktv")
            conn.commit()
            return result
    except Exception as e:
        logger.warning("auto_kt_v error: %s", e)
        return None
    finally:
        conn.close()


def _auto_worksheet_on_complete(session_id):
    """Auto-generate Work Sheet PDF, upload to GCS, and LINE notify nurse group."""
    import os
    import psycopg2
    from .hd_worksheet import load_and_build

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return {"ok": False, "error": "no db"}

    conn = psycopg2.connect(db_url, connect_timeout=5)
    conn.autocommit = True
    try:
        clinic = get_setting_value("clinic_name", "") or "ระนอง รักษ์ไต"
        pdf_bytes, fname = load_and_build(conn, session_id, clinic, mode="draw")
    finally:
        conn.close()

    if not pdf_bytes:
        return {"ok": False, "error": "no data"}

    result = {"ok": True, "filename": fname}

    # Upload to GCS (HD-Records/WorkSheets/YYYY-MM/)
    try:
        from . import _gcs_upload_bytes
        now = _thai_now()
        gcs_path = f"HD-Records/WorkSheets/{now.strftime('%Y-%m')}/{fname}"
        url = _gcs_upload_bytes(pdf_bytes, gcs_path, content_type="application/pdf")
        if url:
            result["gcs_url"] = url
            logger.info("worksheet uploaded: %s", gcs_path)
    except Exception as e:
        logger.warning("worksheet GCS upload: %s", e)

    # LINE notify nurse group (ข้อ 4)
    try:
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://ranong-raktai-ai-cloud.onrender.com")
        ws_url = f"{base_url}/hd/sessions/{session_id}/worksheet.pdf"
        from . import _send_line_to_nurse_group
        _send_line_to_nurse_group(f"📋 Work Sheet พร้อมแล้ว\n{fname}\n{ws_url}")
        result["line_sent"] = True
    except Exception as e:
        logger.warning("worksheet LINE notify: %s", e)

    return result


def _maybe_send_shift_summary(session_id):
    """If no session of the same date+shift is still scheduled/in_progress,
    push the shift handover summary to the nurse LINE group once
    (GCS marker per date+shift). Returns a small status dict."""
    import os
    import psycopg2
    from flask import current_app
    from .shift_handover import generate_handover_summary
    from . import _claim_once
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return {"sent": False, "reason": "no db"}
    conn = psycopg2.connect(db_url, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT session_date, COALESCE(shift,'morning') FROM hd_sessions WHERE id = %s", (session_id,))
            r = cur.fetchone()
            if not r:
                return {"sent": False, "reason": "session not found"}
            day, shift = r
            cur.execute("""SELECT COUNT(*) FILTER (WHERE status IN ('scheduled','in_progress')),
                                  COUNT(*) FILTER (WHERE status = 'completed')
                           FROM hd_sessions WHERE session_date = %s AND COALESCE(shift,'morning') = %s""", (day, shift))
            open_n, done_n = cur.fetchone()
    finally:
        conn.close()
    if open_n or not done_n:
        return {"sent": False, "reason": f"{open_n} session(s) still open"}
    if not _claim_once(current_app, f"shift_summary_{shift}", day):
        return {"sent": False, "reason": "already sent"}
    summary = generate_handover_summary(db_url, shift)
    msg = summary.get("line_message") or ""
    if not msg:
        return {"sent": False, "reason": "empty summary"}
    r = push_nurse_group("📋 สรุปเวรอัตโนมัติ (รอบสุดท้ายฟอกเสร็จ)\n" + msg)
    log_action(user_id=g.get("hd_user_id"), action="shift.summary_auto", resource_type="shift",
               new_values={"date": str(day), "shift": shift, "sent": r["sent"], "method": r["method"]}, ip_address="")
    return {"sent": r["sent"], "method": r["method"], "shift": shift}


@hd_bp.route("/sessions/<int:session_id>/send-summary", methods=["POST"])
@require_role("physician", "nurse", "admin")
def send_session_summary(session_id):
    """Send session summary via LINE to patient or physician."""
    data = request.get_json(silent=True) or {}
    target = data.get("target", "physician")

    summary = _build_session_summary(session_id, {})
    if not summary:
        return jsonify({"sent": False, "message": "ไม่พบข้อมูล session"}), 404

    from flask import current_app
    push_fn = current_app.config.get("LINE_PUSH_FN")
    if not push_fn:
        return jsonify({"sent": False, "message": "LINE Push ไม่พร้อมใช้งาน"})

    if target == "patient":
        if data.get("note"):
            summary["doctor_note"] = str(data["note"]).strip()[:500]
        r = send_patient_summary(session_id, summary=summary, trigger="manual")
        return jsonify(r), (200 if r.get("sent") or r.get("reason") else 500)

    else:
        # Send to physician — check DB settings first, then ENV fallback
        alert_ids = get_setting_value("alert_recipients", "")
        if not alert_ids:
            alert_ids = get_setting_value("physician_line_id", "")
        recipients = [r.strip() for r in alert_ids.split(",") if r.strip()] if alert_ids else []
        if not recipients:
            return jsonify({"sent": False, "message": "ไม่พบ LINE ของแพทย์ — ตั้งค่าที่ ⚙️ ตั้งค่าระบบ"})
        msg = _format_doctor_summary_line(summary)
        sent_count = 0
        for r in recipients:
            r = r.strip()
            if r:
                try:
                    push_fn(r, msg)
                    sent_count += 1
                except Exception:
                    pass
        return jsonify({"sent": sent_count > 0, "count": sent_count})


def _check_esa_missing_alert(session_id):
    """Create alert if patient has prescribed ESA but session has no ESA record."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.esa_given, s.esa_skip_reason, p.esa_drug, p.patient_name, p.hn
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE s.id = %s
            """, (session_id,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return
            esa_given, skip_reason, prescribed_esa, name, hn = row
            # If patient has prescribed ESA but esa_given is NULL (not recorded at all)
            if prescribed_esa and esa_given is None:
                cur.execute("""
                    INSERT INTO hd_clinical_alerts (session_id, severity, title, message, acknowledged)
                    VALUES (%s, 'warning', %s, %s, FALSE)
                """, (
                    session_id,
                    f"ไม่มีการบันทึก ESA — {name} (HN {hn})",
                    f"ผู้ป่วยมี ESA สั่ง ({prescribed_esa}) แต่ยังไม่ได้บันทึกการฉีด/ไม่ฉีดในรอบนี้ กรุณาตรวจสอบ",
                ))
            # If prescribed ESA but explicitly not given and no reason
            elif prescribed_esa and esa_given is False and not skip_reason:
                cur.execute("""
                    INSERT INTO hd_clinical_alerts (session_id, severity, title, message, acknowledged)
                    VALUES (%s, 'info', %s, %s, FALSE)
                """, (
                    session_id,
                    f"งด ESA ไม่ระบุเหตุผล — {name} (HN {hn})",
                    f"ผู้ป่วยมี ESA สั่ง ({prescribed_esa}) แต่งดฉีดครั้งนี้โดยไม่ระบุเหตุผล",
                ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("_check_esa_missing_alert error: %s", e)


def _resolve_patient_line_id(hn):
    from flask import current_app
    fn = current_app.config.get("PATIENT_LINE_ID_FN")
    if not fn:
        reg = current_app.config.get("MASTER_REGISTRY")
        if reg:
            for p in reg.list_all():
                if getattr(p, "hn", "") == hn or getattr(p, "clinic_hn", "") == hn:
                    return getattr(p, "line_user_id", None) or ""
        return ""
    try:
        return fn(hn) or ""
    except Exception:
        return ""


def _add_card_extras(summary):
    """ข้อ 10: น้ำหนักขึ้นระหว่างรอบเทียบเกณฑ์ (รายคน/⚙️/4%) + ข้อความจากแพทย์ (⚙️ patient_card_note)"""
    import os
    try:
        import psycopg2
        from .uf_coach import idwg, limit_pct
        conn = psycopg2.connect(os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", ""), connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT p.idwg_limit_pct, s.pre_weight,
                                      (SELECT s2.post_weight FROM hd_sessions s2 WHERE s2.patient_id = s.patient_id
                                         AND s2.status = 'completed' AND s2.post_weight IS NOT NULL
                                         AND (s2.session_date < s.session_date OR (s2.session_date = s.session_date AND s2.id < s.id))
                                       ORDER BY s2.session_date DESC, s2.id DESC LIMIT 1),
                                      p.dry_weight
                               FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                               WHERE s.id = %s""", (summary.get("session_id"),))
                r = cur.fetchone()
        finally:
            conn.close()
        if r:
            pct = limit_pct(r[0], get_setting_value("idwg_limit_pct", ""))
            res = idwg(r[1], r[2], r[3], pct) if (r[1] and r[2] and r[3]) else None
            summary["idwg_limit_pct"] = pct
            if res:
                summary["idwg"] = res
    except Exception as e:
        logger.warning("card extras: %s", e)
    note = (get_setting_value("patient_card_note", "") or "").strip()
    if summary.get("doctor_note"):
        note = (summary["doctor_note"] + ("\n" + note if note else "")).strip()
    summary["card_note"] = note[:600]
    return summary


def send_patient_summary(session_id, summary=None, trigger="auto"):
    """ว้าว #7 — Flex summary to the patient's own LINE. Returns {sent, reason?}.
    Never raises; nothing is written except the audit log."""
    import os
    from flask import current_app
    from .patient_summary import build_flex, build_text, enrich_from_db
    summary = summary or _build_session_summary(session_id, {})
    if not summary:
        return {"sent": False, "reason": "no_session"}
    if not summary.get("post_weight") and not summary.get("actual_uf"):
        return {"sent": False, "reason": "incomplete", "message": "ยังไม่มีน้ำหนักหลังฟอก/UF"}
    hn = summary.get("patient_hn") or ""
    uid = _resolve_patient_line_id(hn)
    if not uid:
        return {"sent": False, "reason": "not_linked", "message": "ผู้ป่วยยังไม่ได้ผูก LINE"}
    # PDPA: respect a withdrawn consent always; require a recorded consent only when ⚙️ says so
    try:
        import psycopg2
        from .pdpa import status_for
        _c = psycopg2.connect(os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", ""), connect_timeout=5)
        try:
            with _c.cursor() as cur:
                st = status_for(cur, hn).get("line_messaging")
        finally:
            _c.close()
        if st and st["status"] == "withdrawn":
            return {"sent": False, "reason": "consent_withdrawn", "message": "ผู้ป่วยถอนความยินยอมรับข้อความ LINE"}
        if (get_setting_value("patient_push_requires_consent", "off") or "off").lower() == "on" and not (st and st["status"] == "given"):
            return {"sent": False, "reason": "no_consent", "message": "ยังไม่มีบันทึกความยินยอมรับข้อความ LINE (PDPA)"}
    except Exception as e:
        logger.warning("consent check: %s", e)
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    history = []
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                history = enrich_from_db(cur, session_id, summary)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("patient summary enrich: %s", e)
    _add_card_extras(summary)
    flex = build_flex(summary, history)
    push_multi = current_app.config.get("LINE_PUSH_MESSAGES_FN")
    push_fn = current_app.config.get("LINE_PUSH_FN")
    ok = False
    try:
        if push_multi:
            ok = push_multi(uid, [flex]) is not False
        if not ok and push_fn:
            ok = push_fn(uid, build_text(summary, history)) is not False
    except Exception as e:
        logger.warning("patient summary push: %s", e)
    log_action(user_id=g.get("hd_user_id") if trigger == "manual" else None, action="session.patient_summary_sent",
               resource_type="session", resource_id=session_id,
               new_values={"hn": hn, "trigger": trigger, "ok": ok}, ip_address=(request.remote_addr or "") if trigger == "manual" else "")
    return {"sent": ok, "reason": None if ok else "push_failed"}


@hd_bp.route("/sessions/<int:session_id>/patient-summary/preview", methods=["GET"])
@require_role("physician", "nurse", "admin")
def patient_summary_preview(session_id):
    import os
    from .patient_summary import build_text, enrich_from_db
    summary = _build_session_summary(session_id, {})
    if not summary:
        return jsonify({"error": "not found"}), 404
    history = []
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                history = enrich_from_db(cur, session_id, summary)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("patient summary preview: %s", e)
    linked = bool(_resolve_patient_line_id(summary.get("patient_hn") or ""))
    _add_card_extras(summary)
    return jsonify({"text": build_text(summary, history), "linked": linked,
                    "card_note": summary.get("card_note") or "",
                    "auto": (get_setting_value("patient_summary_auto", "on") or "on").lower() != "off"})


def _build_session_summary(session_id, post_data):
    """Build a summary dict for a completed session."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.bed_number, s.pre_weight, s.post_weight,
                       s.actual_uf, s.uf_goal, s.started_at, s.ended_at, s.notes,
                       p.hn, p.patient_name, p.vascular_access, p.dry_weight,
                       p.schedule_days, p.esa_drug,
                       s.esa_given, s.esa_drug_given, s.esa_dose_given,
                       s.esa_route, s.esa_skip_reason,
                       s.hb_value, s.hct_value
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE s.id = %s
            """, (session_id,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return None

            # Get vital records
            cur.execute("""
                SELECT phase, sbp, dbp, heart_rate, temperature, spo2, recorded_at
                FROM hd_vital_signs WHERE session_id = %s AND sbp IS NOT NULL
                ORDER BY recorded_at
            """, (session_id,))
            vitals = cur.fetchall()

            # Get alerts
            cur.execute("""
                SELECT severity, title, message, created_at
                FROM hd_clinical_alerts WHERE session_id = %s ORDER BY created_at
            """, (session_id,))
            alerts = cur.fetchall()
        conn.close()

        pre_w = float(row[2]) if row[2] else float(post_data.get("pre_weight", 0) or 0)
        post_w = float(row[3]) if row[3] else float(post_data.get("post_weight", 0) or 0)
        dry_w = float(row[12]) if row[12] else 0
        actual_uf = float(row[4]) if row[4] else (pre_w - post_w) * 1000
        duration_h = 4
        if row[6] and row[7]:
            duration_h = round((row[7] - row[6]).total_seconds() / 3600, 1)

        alert_list = [{"severity": a[0], "title": a[1], "message": a[2]} for a in alerts]
        vital_list = [{"phase": v[0], "sbp": v[1], "dbp": v[2], "hr": v[3]} for v in vitals]

        # Build assessment
        issues = []
        if any(a[0] == "critical" for a in alerts):
            issues.append("🔴 มี Critical Alert ระหว่างฟอก")
        if any(a[0] == "warning" for a in alerts):
            issues.append("🟡 มี Warning Alert ระหว่างฟอก")
        if pre_w and dry_w and (pre_w - dry_w) > dry_w * 0.05:
            issues.append(f"⚠️ IDWG สูง: {pre_w - dry_w:.1f} kg")
        if post_w and dry_w and post_w > dry_w + 0.5:
            issues.append(f"ℹ️ Post-weight ยังมากกว่า DW {post_w - dry_w:.1f} kg")

        assessment = "✅ ฟอกเลือดเสร็จสมบูรณ์ ไม่มีปัญหา"
        if issues:
            assessment = f"⚠️ ฟอกเสร็จ — มีข้อสังเกต {len(issues)} ข้อ"

        return {
            "session_id": session_id,
            "bed_number": row[1],
            "patient_hn": row[9],
            "patient_name": row[10],
            "vascular_access": row[11],
            "dry_weight": dry_w,
            "schedule_days": row[13],
            "esa_drug": row[14],
            "pre_weight": pre_w,
            "post_weight": post_w,
            "actual_uf": actual_uf,
            "uf_goal": float(row[5]) if row[5] else 2000,
            "duration_hours": duration_h,
            "vital_count": len(vitals),
            "alert_count": len(alerts),
            "alerts": alert_list,
            "vitals": vital_list,
            "issues": issues,
            "assessment": assessment,
            "notes": row[8] or post_data.get("notes", ""),
            # ESA injection
            "esa_given": row[15],
            "esa_drug_given": row[16],
            "esa_dose_given": row[17],
            "esa_route": row[18],
            "esa_skip_reason": row[19],
            "hb_value": float(row[20]) if row[20] else None,
            "hct_value": float(row[21]) if row[21] else None,
        }
    except Exception as e:
        logger.error("_build_session_summary error: %s", e)
        return None


def _format_patient_summary_line(s):
    """Format summary as LINE text message for patient."""
    issues_text = "\n".join(s.get("issues", [])) or "✅ ไม่มีปัญหา"
    return (
        f"📋 สรุปผลฟอกเลือด\n"
        f"🏥 คลินิกระนอง รักษ์ไต\n"
        f"━━━━━━━━━━━━\n"
        f"👤 {s['patient_name']}\n"
        f"🛏 เตียง {s['bed_number']}\n"
        f"⏱ ระยะเวลา: {s['duration_hours']} ชม.\n"
        f"━━━━━━━━━━━━\n"
        f"⚖️ น้ำหนัก: {s['pre_weight']:.1f} → {s['post_weight']:.1f} kg\n"
        f"💧 UF: {s['actual_uf']:.0f} mL\n"
        f"━━━━━━━━━━━━\n"
        f"{s['assessment']}\n"
        f"{issues_text}\n"
        f"━━━━━━━━━━━━\n"
        f"📞 หากมีอาการผิดปกติ กรุณาติดต่อ รพ.ระนอง"
    )


def _format_doctor_summary_line(s):
    """Format summary as LINE text message for physician."""
    alert_text = ""
    if s.get("alerts"):
        alert_text = "\n🚨 Alerts:\n" + "\n".join(
            f"  {'🔴' if a['severity']=='critical' else '🟡'} {a['title']}"
            for a in s["alerts"]
        )
    issues_text = ""
    if s.get("issues"):
        issues_text = "\n📌 ข้อสังเกต:\n" + "\n".join(f"  {i}" for i in s["issues"])

    return (
        f"👨‍⚕️ สรุปรอบฟอก — เตียง {s['bed_number']}\n"
        f"━━━━━━━━━━━━\n"
        f"👤 {s['patient_name']} (HN {s['patient_hn']})\n"
        f"🩸 VA: {s['vascular_access'] or '-'} | ESA: {s['esa_drug'] or '-'}\n"
        f"⏱ {s['duration_hours']} ชม. | Vital บันทึก {s['vital_count']} ครั้ง\n"
        f"━━━━━━━━━━━━\n"
        f"⚖️ {s['pre_weight']:.1f} → {s['post_weight']:.1f} kg "
        f"(DW {s['dry_weight']:.1f})\n"
        f"💧 UF: {s['actual_uf']:.0f}/{s['uf_goal']:.0f} mL\n"
        f"━━━━━━━━━━━━\n"
        f"{s['assessment']}"
        f"{alert_text}"
        f"{issues_text}"
        f"{chr(10)+'📝 '+s['notes'] if s.get('notes') else ''}"
    )


@hd_bp.route("/sessions/<int:session_id>/export-summary", methods=["GET"])
@require_role("physician", "nurse", "admin")
def export_session_summary(session_id):
    """Export session summary as printable HTML (can Save-as-PDF from browser)."""
    summary = _build_session_summary(session_id, {})
    if not summary:
        return jsonify({"error": "ไม่พบข้อมูล session"}), 404

    # Build a self-contained printable HTML page
    vitals_rows = ""
    for i, v in enumerate(summary.get("vitals", []), 1):
        vitals_rows += (
            f"<tr><td>{i}</td><td>{v.get('phase','')}</td>"
            f"<td>{v.get('sbp','')}/{v.get('dbp','')}</td>"
            f"<td>{v.get('hr','')}</td></tr>\n"
        )
    alerts_html = ""
    for a in summary.get("alerts", []):
        color = "#e74c3c" if a.get("severity") == "critical" else "#f39c12"
        alerts_html += f'<div style="padding:4px 8px;background:{color}22;border-left:3px solid {color};margin:4px 0;font-size:13px;">{a.get("title","")}</div>'
    if not alerts_html:
        alerts_html = '<div style="color:#27ae60;font-weight:600;">✅ ไม่มี alert ระหว่างฟอก</div>'

    issues_html = ""
    for iss in summary.get("issues", []):
        issues_html += f"<div style='font-size:13px;margin:2px 0;'>{iss}</div>"
    if not issues_html:
        issues_html = '<div style="color:#27ae60;">✅ ไม่มีปัญหา</div>'

    pre_w = summary.get("pre_weight", 0) or 0
    post_w = summary.get("post_weight", 0) or 0
    dry_w = summary.get("dry_weight", 0) or 0
    idwg = pre_w - dry_w if pre_w and dry_w else 0
    idwg_pct = (idwg / dry_w * 100) if dry_w else 0

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<title>สรุปรอบฟอก — {summary.get('patient_name','')}</title>
<style>
@page {{ margin: 15mm; }}
body {{ font-family: 'Sarabun', 'TH SarabunPSK', sans-serif; font-size: 14px; color: #333; max-width: 700px; margin: 0 auto; padding: 20px; }}
h1 {{ font-size: 18px; margin: 0 0 4px 0; }}
h2 {{ font-size: 15px; margin: 16px 0 8px 0; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
.header {{ border-bottom: 2px solid #2980b9; padding-bottom: 12px; margin-bottom: 16px; }}
.logo {{ color: #2980b9; font-weight: 700; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ padding: 6px 8px; border: 1px solid #ddd; text-align: left; }}
th {{ background: #f8f9fa; font-weight: 600; }}
.info-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 4px 20px; font-size: 13px; }}
.info-grid .label {{ color: #666; }}
.info-grid .value {{ font-weight: 600; }}
.assessment {{ font-size: 15px; font-weight: 700; margin: 12px 0; padding: 10px; background: #f8f9fa; border-radius: 6px; }}
.footer {{ margin-top: 24px; border-top: 1px solid #ccc; padding-top: 8px; font-size: 11px; color: #999; text-align: center; }}
@media print {{ body {{ padding: 0; }} .no-print {{ display: none !important; }} }}
</style>
</head>
<body>
<div class="no-print" style="text-align:right;margin-bottom:12px;">
  <button onclick="window.print()" style="padding:10px 24px;background:#2980b9;color:#fff;border:none;border-radius:6px;font-size:14px;cursor:pointer;">🖨 พิมพ์ / บันทึก PDF</button>
</div>
<div class="header">
  <h1 class="logo">🏥 HD Smart Monitoring — คลินิกระนอง รักษ์ไต</h1>
  <div style="font-size:12px;color:#666;">สรุปผลฟอกเลือด | Session #{summary.get('session_id','')}</div>
</div>

<h2>👤 ข้อมูลผู้ป่วย</h2>
<div class="info-grid">
  <span class="label">ชื่อ-สกุล</span><span class="value">{summary.get('patient_name','')}</span>
  <span class="label">HN</span><span class="value">{summary.get('patient_hn','')}</span>
  <span class="label">Vascular Access</span><span class="value">{summary.get('vascular_access','') or '-'}</span>
  <span class="label">ESA</span><span class="value">{summary.get('esa_drug','') or '-'}</span>
  <span class="label">ตารางฟอก</span><span class="value">{summary.get('schedule_days','') or '-'}</span>
  <span class="label">เตียง</span><span class="value">{summary.get('bed_number','')}</span>
</div>

<h2>📊 ผลการฟอกเลือด</h2>
<div class="info-grid">
  <span class="label">ระยะเวลาฟอก</span><span class="value">{summary.get('duration_hours',4)} ชม.</span>
  <span class="label">Dry Weight</span><span class="value">{dry_w:.1f} kg</span>
  <span class="label">น้ำหนักก่อนฟอก</span><span class="value">{pre_w:.1f} kg</span>
  <span class="label">น้ำหนักหลังฟอก</span><span class="value">{post_w:.1f} kg</span>
  <span class="label">IDWG</span><span class="value" style="{'color:#e74c3c;' if idwg_pct>4 else ''}">{idwg:.1f} kg ({idwg_pct:.1f}% DW)</span>
  <span class="label">Actual UF</span><span class="value">{summary.get('actual_uf',0):.0f} mL (Goal: {summary.get('uf_goal',2000):.0f})</span>
</div>

<h2>❤️ Vital Signs ({summary.get('vital_count',0)} ครั้ง)</h2>
<table>
  <tr><th>#</th><th>Phase</th><th>BP (mmHg)</th><th>HR (bpm)</th></tr>
  {vitals_rows if vitals_rows else '<tr><td colspan="4" style="text-align:center;color:#999;">ไม่มีข้อมูล</td></tr>'}
</table>

<h2>🚨 Alerts</h2>
{alerts_html}

<h2>📋 ประเมินผล</h2>
<div class="assessment">{summary.get('assessment','')}</div>
{issues_html}
{'<h2>📝 หมายเหตุ</h2><div style="font-size:13px;">'+summary.get('notes','')+'</div>' if summary.get('notes') else ''}

<div class="footer">
  RANONG-RAKTAI-AI INTELLIGENT — HD Smart Monitoring v2.2<br>
  เอกสารประกอบการพิจารณาทางการแพทย์ ไม่ใช่การวินิจฉัยหรือสั่งการรักษา
</div>
</body>
</html>"""
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


# ── Vital Signs ───────────────────────────────────────────


def _process_vital_alerts(cur, session_id, sbp=None, dbp=None, hr=None, spo2=None, temp=None):
    """Evaluate a vital reading, store alerts, push LINE to physician.

    Runs inside a SAVEPOINT so a failure here never loses the vital itself.
    Returns the list of alerts that were newly created.
    """
    from flask import current_app
    from .vital_alerts import evaluate, session_context, store_alerts, notify_physician
    created = []
    cur.execute("SAVEPOINT sp_alerts")
    try:
        ctx = session_context(cur, session_id)
        if not ctx:
            cur.execute("RELEASE SAVEPOINT sp_alerts")
            return created
        alerts = evaluate(
            sbp=sbp, dbp=dbp, hr=hr, spo2=spo2, temp=temp,
            pre_sbp=ctx["pre_sbp"], prev_sbp=ctx["prev_sbp"],
            severe_sbp=get_setting_value("alert_severe_sbp", "90") or 90,
            sbp_drop=get_setting_value("alert_sbp_drop", "20") or 20,
        )
        created = store_alerts(cur, session_id, ctx["patient_id"], alerts)
        cur.execute("RELEASE SAVEPOINT sp_alerts")
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT sp_alerts")
        logger.warning("vital alert evaluation failed (vital still saved): %s", e)
        return created
    if created:
        try:
            push_fn = current_app.config.get("LINE_PUSH_FN")
            ids = get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")
            recipients = [r.strip() for r in ids.split(",") if r.strip()]
            base = (request.host_url or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
            n = notify_physician(push_fn, recipients, ctx, created, base)
            logger.info("vital alerts: session=%s created=%d line_sent=%d", session_id, len(created), n)
        except Exception as e:
            logger.warning("vital alert LINE push failed: %s", e)
    return created

@hd_bp.route("/sessions/<int:session_id>/vitals", methods=["POST"])
@require_role("physician", "nurse", "admin")
def record_vital(session_id):
    """Record vital signs for a session."""
    data = request.get_json(silent=True) or {}

    required = ["phase"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400

    phase = data.get("phase")
    if phase not in ("pre", "intra", "post"):
        return jsonify({"error": "phase must be pre, intra, or post"}), 400

    log_action(
        user_id=g.get("hd_user_id"),
        action="vital.record",
        resource_type="vital_sign",
        new_values=data,
        ip_address=request.remote_addr or "",
    )

    # Get patient_id from session
    patient_id = data.get("patient_id", 0)
    if not patient_id:
        import os
        db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        if db_url:
            try:
                import psycopg2
                conn = psycopg2.connect(db_url, connect_timeout=5)
                with conn.cursor() as cur:
                    cur.execute("SELECT patient_id FROM hd_sessions WHERE id = %s", (session_id,))
                    row = cur.fetchone()
                    if row:
                        patient_id = row[0]
                conn.close()
            except Exception:
                pass

    # Use VitalRecorder for real-time recording + alert evaluation
    from flask import current_app
    recorder = current_app.config.get("HD_VITAL_RECORDER")
    if recorder:
        result = recorder.record_vitals(
            session_id=session_id,
            patient_id=patient_id,
            phase=phase,
            sbp=data.get("sbp"),
            dbp=data.get("dbp"),
            hr=data.get("hr"),
            temp=data.get("temp"),
            spo2=data.get("spo2"),
            uf_rate=data.get("uf_rate"),
            weight=data.get("weight"),
            notes=data.get("notes", ""),
            recorded_by=str(g.get("hd_user_id", "")),
        )
        return jsonify({
            "session_id": session_id,
            "phase": phase,
            "recorded": True,
            "vital_id": result.get("vital_id"),
            "alerts": result.get("alerts", []),
            "alert_count": result.get("alert_count", 0),
        }), 201

    # Fallback: direct INSERT if no VitalRecorder
    import os
    db_url_fb = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if db_url_fb:
        try:
            import psycopg2
            conn_fb = psycopg2.connect(db_url_fb, connect_timeout=5)
            with conn_fb.cursor() as cur_fb:
                try:
                    cur_fb.execute("""
                        INSERT INTO hd_vital_signs
                            (session_id, phase, sbp, dbp, heart_rate, temperature, spo2, recorded_at)
                        VALUES (%s, %s::hd_vital_phase, %s, %s, %s, %s, %s, NOW())
                        RETURNING id
                    """, (session_id, phase,
                          data.get("sbp"), data.get("dbp"), data.get("hr"),
                          data.get("temp"), data.get("spo2")))
                except Exception:
                    conn_fb.rollback()
                    cur_fb.execute("""
                        INSERT INTO hd_vital_signs
                            (session_id, phase, sbp, dbp, heart_rate, temperature, spo2, recorded_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                        RETURNING id
                    """, (session_id, phase,
                          data.get("sbp"), data.get("dbp"), data.get("hr"),
                          data.get("temp"), data.get("spo2")))
                vid = cur_fb.fetchone()[0]
                new_alerts = _process_vital_alerts(
                    cur_fb, session_id, sbp=data.get("sbp"), dbp=data.get("dbp"),
                    hr=data.get("hr"), spo2=data.get("spo2"), temp=data.get("temp"))
                conn_fb.commit()
            conn_fb.close()
            return jsonify({"session_id": session_id, "phase": phase, "recorded": True,
                            "vital_id": vid, "alerts": new_alerts,
                            "alert_count": len(new_alerts)}), 201
        except Exception as ve:
            logger.error("vital fallback error: %s", ve)
    return jsonify({"session_id": session_id, "phase": phase, "recorded": True}), 201


@hd_bp.route("/sessions/<int:session_id>/vitals", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def list_vitals(session_id):
    """List vital signs for a session."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"session_id": session_id, "vitals": []})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phase, sbp, dbp, heart_rate, temperature, spo2, recorded_at
                FROM hd_vital_signs WHERE session_id = %s
                ORDER BY recorded_at
            """, (session_id,))
            vitals = [{
                "id": r[0], "phase": r[1],
                "sbp": r[2], "dbp": r[3], "heart_rate": r[4], "hr": r[4],
                "temp": float(r[5]) if r[5] else None, "spo2": r[6],
                "recorded_at": (r[7].isoformat() + "Z") if r[7] else None,
            } for r in cur.fetchall()]
        conn.close()
        return jsonify({"session_id": session_id, "vitals": vitals, "count": len(vitals)})
    except Exception as e:
        logger.error("list_vitals error: %s", e)
        return jsonify({"session_id": session_id, "vitals": [], "error": str(e)})


# ── UF Records ────────────────────────────────────────────

def _uf_watch_limits():
    from .uf_watch import DEFAULTS
    out = dict(DEFAULTS)
    try:
        lim = _dw_limits()
        out["uf_max"] = float(lim.get("uf_rate_max", out["uf_max"]))
        out["uf_caution"] = float(lim.get("uf_rate_caution", out["uf_caution"]))
        v = get_setting_value("uf_record_every_min", "")
        if v:
            out["record_every_min"] = int(v)
    except Exception:
        pass
    return out


def _uf_idh_alert(cur, session_id, uf):
    """ข้อ 3: อัตรา UF ที่ต้องใช้เกินเพดาน + ผู้ป่วยเสี่ยง IDH สูง → clinical alert (critical) + LINE หัวหน้าเวร/กลุ่ม
    ครั้งเดียวต่อรอบ. คืน True เมื่อส่ง."""
    if not uf or uf.get("flag") != "high_rate":
        return False
    try:
        from .idh_predictor import features_for, score as idh_score
        feats = features_for(cur, session_id)
        risk = idh_score(feats) if feats else None
    except Exception as e:
        logger.debug("uf idh alert features: %s", e)
        risk = None
    if not risk or risk.get("level") != "high":
        return False
    title_prefix = "UF เกินเพดาน + เสี่ยง IDH สูง"
    cur.execute("SELECT 1 FROM hd_clinical_alerts WHERE session_id = %s AND title LIKE %s LIMIT 1", (session_id, title_prefix + "%"))
    if cur.fetchone():
        return False
    cur.execute("""SELECT p.patient_name, p.hn, s.bed_number FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                   WHERE s.id = %s""", (session_id,))
    r = cur.fetchone()
    name, hn, bed = (r[0], r[1], r[2]) if r else ("", "", None)
    msg = (f"{uf['text']} · คะแนนเสี่ยง IDH {risk['score']} (สูง) — พิจารณายืดเวลา/ลดเป้า UF ก่อนเร่งอัตรา "
           f"และวัด BP ทุก 15 นาที (ข้อเสนอจากระบบ แพทย์ตัดสินใจ)")
    cur.execute("""INSERT INTO hd_clinical_alerts (session_id, severity, title, message, acknowledged)
                   VALUES (%s, 'critical', %s, %s, FALSE)""", (session_id, f"{title_prefix} — {name} (HN {hn}) เตียง {bed}", msg))
    try:
        from . import ops_status as ops
        line = f"🚨 เตียง {bed} {name} (HN {hn})\n{title_prefix}\n{msg}"
        with ops.critical():
            push_nurse_group(line)
    except Exception as e:
        logger.warning("uf idh alert push: %s", e)
    return True


@hd_bp.route("/sessions/<int:session_id>/uf", methods=["GET", "POST"])
@require_role("physician", "nurse", "admin")
def record_uf(session_id):
    """Record or list UF data for a session."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503

    if request.method == "GET":
        # Return all UF records for this session
        try:
            import psycopg2
            conn = psycopg2.connect(db_url, connect_timeout=5)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, recorded_at, slot_time, uf_removed, uf_rate,
                           uf_remaining, notes
                    FROM hd_uf_records WHERE session_id = %s
                    ORDER BY recorded_at
                """, (session_id,))
                rows = cur.fetchall()
                # Also get session's uf_goal
                cur.execute("SELECT uf_goal, status FROM hd_sessions WHERE id = %s", (session_id,))
                srow = cur.fetchone()
                # 16 พ.ย. 2569: สถานะ UF Watch + เป้า/เวลาที่ปรับบนเครื่อง ตั้งแต่เปิดหน้า (ไม่ต้องรอบันทึกใหม่)
                watch = None
                try:
                    if not srow or srow[1] != "in_progress":
                        raise LookupError("not running")  # รอบย้อนหลัง/จบแล้ว — ไม่ประเมินเทียบเวลาปัจจุบัน
                    from .uf_watch import for_session as _ufw
                    watch = _ufw(cur, session_id, _uf_watch_limits())
                except Exception as we:
                    logger.debug("uf watch (GET): %s", we)
            conn.close()
            records = [{
                "id": r[0],
                "recorded_at": (r[1].isoformat() + "Z") if r[1] else None,
                "slot_time": r[2],
                # 17 พ.ย. 2569: NUMERIC → Decimal → JSON เป็น string ("800.0") ทำให้หน้าเว็บเทียบแบบตัวอักษร
                # ('800.0' > '2400.0') → UF Progress แสดง 800 แทน 2400 → ส่งเป็นตัวเลขเสมอ
                "uf_removed": float(r[3]) if r[3] is not None else None,
                "uf_rate": float(r[4]) if r[4] is not None else None,
                "uf_remaining": float(r[5]) if r[5] is not None else None, "notes": r[6],
            } for r in rows]
            return jsonify({
                "records": records,
                "uf_goal": int(srow[0]) if srow and srow[0] else 2000,
                "watch": watch,
            })
        except Exception as e:
            logger.error("get UF records error: %s", e)
            return jsonify({"records": [], "error": str(e)})

    # POST: Save new UF record
    data = request.get_json(silent=True) or {}
    uf_removed = data.get("uf_removed", 0)
    uf_rate = data.get("uf_rate", 0)
    uf_remaining = data.get("uf_remaining", 0)
    slot_time = data.get("slot_time", "")
    notes = (data.get("notes") or "")[:120]
    # เป้า/เวลาที่พยาบาลตั้งบนเครื่องหลังปรึกษาแพทย์ (ปุ่ม "คงอัตราปลอดภัย" / "ยืดเวลา") — บันทึกไว้ให้
    # UF Watch ประเมินตามจริง ระบบไม่ปรับเครื่องหรือแก้ใบสั่งเอง
    try:
        _mt = int(float(data.get("machine_target") or 0))
        _ex = int(float(data.get("extend_min") or 0))
    except (TypeError, ValueError):
        _mt, _ex = 0, 0
    _adj = ";".join(x for x in (f"target={_mt}" if 0 < _mt <= 10000 else "",
                                f"extend={_ex}" if 0 < _ex <= 240 else "") if x)
    if _adj:
        notes = f"{_adj};{notes}" if notes else _adj

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Insert UF record
            cur.execute("""
                INSERT INTO hd_uf_records
                    (session_id, slot_time, uf_removed, uf_rate, uf_remaining, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (session_id, slot_time, uf_removed, uf_rate, uf_remaining, notes))
            rec_id = cur.fetchone()[0]

            # Update session's actual_uf to latest cumulative value
            cur.execute("""
                UPDATE hd_sessions SET actual_uf = %s WHERE id = %s
            """, (uf_removed, session_id))
            # UF Watch + ข้อ 3: เชื่อม IDH Predictor
            watch, alerted = None, False
            try:
                from .uf_watch import for_session as _ufw
                watch = _ufw(cur, session_id, _uf_watch_limits())
                alerted = _uf_idh_alert(cur, session_id, watch)
            except Exception as we:
                logger.warning("uf watch: %s", we)
        conn.commit()
        conn.close()

        log_action(
            user_id=g.get("hd_user_id"),
            action="uf.record",
            resource_type="uf_record",
            resource_id=rec_id,
            new_values=data,
            ip_address=request.remote_addr or "",
        )
        return jsonify({
            "id": rec_id,
            "session_id": session_id,
            "uf_removed": uf_removed,
            "uf_rate": uf_rate,
            "uf_recorded": True,
            "watch": watch,
            "idh_alert_sent": alerted,
        }), 201
    except Exception as e:
        logger.error("record_uf error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Alerts ────────────────────────────────────────────────

@hd_bp.route("/alerts", methods=["GET"])
@require_role("physician", "nurse", "admin")
def list_alerts():
    """List clinical alerts from DB."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"alerts": [], "total": 0})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            only_active = request.args.get("active", "true") == "true"
            if only_active:
                cur.execute("""
                    SELECT a.id, a.session_id, a.severity, a.title, a.message,
                           a.acknowledged, a.created_at,
                           p.hn, p.patient_name, s.bed_number
                    FROM hd_clinical_alerts a
                    JOIN hd_sessions s ON s.id = a.session_id
                    JOIN hd_patient_mapping p ON p.id = s.patient_id
                    WHERE a.acknowledged = FALSE
                    ORDER BY a.created_at DESC LIMIT 50
                """)
            else:
                cur.execute("""
                    SELECT a.id, a.session_id, a.severity, a.title, a.message,
                           a.acknowledged, a.created_at,
                           p.hn, p.patient_name, s.bed_number
                    FROM hd_clinical_alerts a
                    JOIN hd_sessions s ON s.id = a.session_id
                    JOIN hd_patient_mapping p ON p.id = s.patient_id
                    ORDER BY a.created_at DESC LIMIT 100
                """)
            rows = cur.fetchall()
        conn.close()
        alerts = [{
            "id": r[0], "session_id": r[1], "severity": r[2],
            "title": r[3], "message": r[4], "acknowledged": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
            "hn": r[7], "patient_name": r[8], "bed_number": r[9],
        } for r in rows]
        return jsonify({"alerts": alerts, "total": len(alerts)})
    except Exception as e:
        logger.error("list_alerts error: %s", e)
        return jsonify({"alerts": [], "total": 0, "error": str(e)})



@hd_bp.route("/sessions/<int:session_id>/alerts", methods=["GET"])
@require_role("physician", "nurse", "admin")
def session_alerts(session_id):
    """Alerts for one session with physician orders (for the session page panel)."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"alerts": []})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.id, a.severity::text, a.title, a.message, a.acknowledged, a.created_at,
                       a.physician_order, a.order_by, a.order_at, p.patient_name, p.hn, s.bed_number
                FROM hd_clinical_alerts a
                JOIN hd_sessions s ON s.id = a.session_id
                LEFT JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE a.session_id = %s
                ORDER BY a.acknowledged ASC,
                         CASE a.severity::text WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                         a.created_at DESC
                LIMIT 30
            """, (session_id,))
            rows = cur.fetchall()
        conn.close()
        return jsonify({"alerts": [{
            "id": r[0], "severity": r[1], "title": r[2], "message": r[3], "acknowledged": r[4],
            "created_at": (r[5].isoformat() + "Z") if r[5] else None,
            "physician_order": r[6], "order_by": r[7],
            "order_at": (r[8].isoformat() + "Z") if r[8] else None,
            "patient_name": r[9], "hn": r[10], "bed_number": r[11],
        } for r in rows]})
    except Exception as e:
        logger.warning("session_alerts: %s", e)
        return jsonify({"alerts": [], "error": str(e)})


@hd_bp.route("/alerts/<int:alert_id>/order", methods=["POST"])
@require_role("physician")
def physician_order(alert_id):
    """Physician records a treatment order on an alert (free text, by a human).

    The system never generates orders itself — it only stores what the
    physician typed and relays it to the nursing team.
    """
    import os, psycopg2
    data = request.get_json(silent=True) or {}
    order = (data.get("order") or "").strip()
    if not order:
        return jsonify({"error": "กรุณาพิมพ์คำสั่งการรักษา"}), 400
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 503
    who = g.get("hd_username") or str(g.get("hd_user_id") or "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE hd_clinical_alerts
                SET physician_order = %s, order_by = %s, order_at = NOW()
                WHERE id = %s
                RETURNING session_id, title
            """, (order[:1000], who[:100], alert_id))
            r = cur.fetchone()
            if not r:
                conn.close()
                return jsonify({"error": "ไม่พบ alert"}), 404
            session_id, title = r
            cur.execute("""
                SELECT p.patient_name, p.hn, s.bed_number FROM hd_sessions s
                LEFT JOIN hd_patient_mapping p ON p.id = s.patient_id WHERE s.id = %s
            """, (session_id,))
            pr = cur.fetchone() or ("", "", None)
            conn.commit()
        conn.close()
        log_action(user_id=g.get("hd_user_id"), action="alert.physician_order",
                   resource_type="alert", resource_id=alert_id,
                   new_values={"order": order[:200]}, ip_address=request.remote_addr or "")

        # Relay to the nursing team LINE group (Messaging API); never fatal
        relayed = False
        relay_method = None
        try:
            msg = (f"✍️ คำสั่งแพทย์ ({who})\n👤 {pr[0]} (HN {pr[1]}) เตียง {pr[2] or '-'}\n"
                   f"⚠️ {title}\n📝 {order}\n"
                   f"👉 รับทราบใน HD Smart Monitoring → เตียง {pr[2] or '-'}")
            r = push_nurse_group(msg, fallback_recipients=False)
            relayed, relay_method = r["sent"], r["method"]
        except Exception as e:
            logger.warning("order relay: %s", e)
        return jsonify({"ok": True, "alert_id": alert_id, "relayed_to_nurses": relayed, "relay_method": relay_method})
    except Exception as e:
        logger.error("physician_order: %s", e)
        return jsonify({"error": str(e)}), 500

@hd_bp.route("/alerts/<int:alert_id>/acknowledge", methods=["POST"])
@require_role("physician", "nurse", "admin")
def acknowledge_alert(alert_id):
    """Acknowledge a clinical alert — update DB."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    data = request.get_json(silent=True) or {}
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE hd_clinical_alerts SET acknowledged = TRUE
                WHERE id = %s RETURNING id
            """, (alert_id,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "Alert not found"}), 404
        conn.commit()
        conn.close()
        log_action(
            user_id=g.get("hd_user_id"),
            action="alert.acknowledge",
            resource_type="alert",
            resource_id=alert_id,
            new_values=data,
            ip_address=request.remote_addr or "",
        )
        return jsonify({"alert_id": alert_id, "acknowledged": True})
    except Exception as e:
        logger.error("acknowledge_alert error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Import DOB + BFR from Excel ────────────────────────────

@hd_bp.route("/patients/import-dob-bfr", methods=["POST"])
@require_role("physician", "admin")
def import_dob_bfr():
    """Batch update date_of_birth + bfr for patients from Excel data."""
    import os, psycopg2
    from datetime import date as _date
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "Database not configured"}), 500
    body = request.get_json(silent=True) or {}
    patients = body.get("patients", [])
    if not patients:
        return jsonify({"error": "No patients data"}), 400

    conn = psycopg2.connect(db_url, connect_timeout=10)
    conn.autocommit = True
    updated = 0
    skipped = 0
    errors = []
    try:
        with conn.cursor() as cur:
            # Ensure bfr column exists
            try:
                cur.execute("ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS bfr INTEGER DEFAULT 0")
            except Exception:
                pass
            for p in patients:
                hn = str(p.get("hn", "")).strip()
                if not hn:
                    skipped += 1
                    continue
                # Parse DOB: "18/08/2515" (Buddhist Era) → 1972-08-18
                dob = None
                dob_raw = str(p.get("dob", "")).strip()
                if dob_raw:
                    try:
                        parts = dob_raw.split("/")
                        if len(parts) == 3:
                            d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
                            if y > 2400:
                                y -= 543  # Buddhist Era → CE
                            dob = _date(y, m, d)
                    except Exception:
                        pass
                bfr = p.get("bfr")
                if bfr:
                    try:
                        bfr = int(bfr)
                    except Exception:
                        bfr = None
                try:
                    sets = []
                    vals = []
                    if dob:
                        sets.append("date_of_birth = %s")
                        vals.append(dob)
                    if bfr:
                        sets.append("bfr = %s")
                        vals.append(bfr)
                    if not sets:
                        skipped += 1
                        continue
                    vals.append(hn)
                    cur.execute(f"UPDATE hd_patient_mapping SET {', '.join(sets)} WHERE hn = %s", vals)
                    if cur.rowcount > 0:
                        updated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    errors.append(f"HN {hn}: {e}")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

    log_action(user_id=g.get("hd_user_id"), action="patients.import_dob_bfr",
               resource_type="batch", resource_id=None,
               ip_address=request.remote_addr or "",
               details=f"updated={updated} skipped={skipped}")
    return jsonify({"updated": updated, "skipped": skipped, "errors": errors[:10]})


# ── Patients ──────────────────────────────────────────────

@hd_bp.route("/patients", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def list_patients():
    """List HD patients with last HD date and alert count."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"patients": [], "total": 0})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        search = request.args.get("q", "").strip()
        schedule = request.args.get("schedule", "").strip()
        access = request.args.get("access", "").strip()
        shift_q = request.args.get("shift", "").strip()
        with conn.cursor() as cur:
            # Join with hd_sessions to get last_hd date + unacked alert count
            sql = """SELECT p.id, p.hn, p.hospital_hn, p.patient_name,
                            p.vascular_access, p.dry_weight, p.schedule_days,
                            p.esa_drug, p.esa_dose, p.primary_diagnosis, p.is_active,
                            MAX(s.started_at)::date AS last_hd,
                            COALESCE(SUM(CASE WHEN a.acknowledged = FALSE THEN 1 ELSE 0 END), 0) AS unacked_alerts,
                            p.default_shift
                     FROM hd_patient_mapping p
                     LEFT JOIN hd_sessions s ON s.patient_id = p.id
                     LEFT JOIN hd_clinical_alerts a ON a.session_id = s.id AND a.acknowledged = FALSE
                     WHERE p.is_active = TRUE"""
            params = []
            if search:
                sql += " AND (p.patient_name ILIKE %s OR p.hn ILIKE %s OR p.hospital_hn ILIKE %s)"
                params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
            if schedule:
                sql += " AND p.schedule_days ILIKE %s"
                params.append(f"%{schedule}%")
            if access:
                sql += " AND UPPER(p.vascular_access) LIKE UPPER(%s)"
                params.append(f"%{access}%")
            if shift_q:
                sql += " AND p.default_shift = %s"
                params.append(shift_q)
            sql += """ GROUP BY p.id, p.hn, p.hospital_hn, p.patient_name,
                               p.vascular_access, p.dry_weight, p.schedule_days,
                               p.esa_drug, p.esa_dose, p.primary_diagnosis, p.is_active, p.default_shift
                       ORDER BY p.hn LIMIT 300"""
            cur.execute(sql, params)
            rows = cur.fetchall()
            try:
                from .serology import latest_for as _sero_latest
                sero = _sero_latest(cur, [r[0] for r in rows])
            except Exception as se:
                logger.warning("list_patients serology: %s", se)
                sero = {}
        conn.close()
        patients = []
        alert_count = 0
        for r in rows:
            unacked = int(r[12]) if r[12] else 0
            if unacked > 0:
                alert_count += 1
            patients.append({
                "id": r[0], "hn": r[1], "hospital_hn": r[2],
                "patient_name": r[3], "vascular_access": r[4],
                "dry_weight": float(r[5]) if r[5] else None,
                "schedule_days": r[6], "esa_drug": r[7], "esa_dose": r[8],
                "primary_diagnosis": r[9], "is_active": r[10],
                "last_hd": str(r[11]) if r[11] else None,
                "unacked_alerts": unacked,
                "default_shift": r[13] if len(r) > 13 else None,
                "serology": sero.get(r[0]),
            })
        # Collect unique schedule and access values for dynamic filters
        schedules = sorted(set(p["schedule_days"] for p in patients if p.get("schedule_days")))
        accesses = sorted(set(p["vascular_access"] for p in patients if p.get("vascular_access")))
        return jsonify({
            "patients": patients, "total": len(patients),
            "alert_count": alert_count,
            "filter_options": {"schedules": schedules, "accesses": accesses},
        })
    except Exception as e:
        logger.error("list_patients error: %s", e)
        return jsonify({"patients": [], "total": 0, "error": str(e)})


# ── Patient Management: CRUD + Lab Summary ────────────────

@hd_bp.route("/patients/<hn>/deactivate", methods=["POST"])
@require_role("physician", "admin")
def deactivate_patient(hn):
    """Soft-delete: is_active = FALSE. ข้อมูลยังอยู่ กู้คืนได้."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    with conn.cursor() as cur:
        cur.execute("UPDATE hd_patient_mapping SET is_active = FALSE WHERE hn = %s RETURNING id, patient_name", (hn,))
        r = cur.fetchone()
        if not r:
            conn.close()
            return jsonify({"error": "ไม่พบ HN"}), 404
        conn.commit()
    conn.close()
    log_action(user_id=g.get("hd_user_id"), action="patient.deactivate", resource_type="patient", resource_id=hn,
               new_values={"name": r[1]}, ip_address=request.remote_addr)
    return jsonify({"ok": True, "hn": hn, "name": r[1], "message": f"ระงับ {r[1]} (HN {hn}) แล้ว"})


@hd_bp.route("/patients/<hn>/reactivate", methods=["POST"])
@require_role("physician", "admin")
def reactivate_patient(hn):
    """กู้คืนคนไข้ที่ระงับ."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    with conn.cursor() as cur:
        cur.execute("UPDATE hd_patient_mapping SET is_active = TRUE WHERE hn = %s RETURNING id, patient_name", (hn,))
        r = cur.fetchone()
        if not r:
            conn.close()
            return jsonify({"error": "ไม่พบ HN"}), 404
        conn.commit()
    conn.close()
    log_action(user_id=g.get("hd_user_id"), action="patient.reactivate", resource_type="patient", resource_id=hn,
               new_values={"name": r[1]}, ip_address=request.remote_addr)
    return jsonify({"ok": True, "hn": hn, "name": r[1], "message": f"กู้คืน {r[1]} (HN {hn}) แล้ว"})


@hd_bp.route("/patients/<hn>/edit", methods=["POST"])
@require_role("physician", "admin")
def edit_patient(hn):
    """แก้ไขชื่อ, hospital_hn, vascular_access, schedule, DW, shift."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    data = request.get_json(silent=True) or {}
    allowed = {"patient_name", "hospital_hn", "vascular_access", "schedule_days",
               "dry_weight", "default_shift", "esa_drug", "esa_dose", "primary_diagnosis"}
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}
    if not updates:
        return jsonify({"error": "ไม่มีข้อมูลที่จะแก้"}), 400
    conn = psycopg2.connect(db_url, connect_timeout=5)
    with conn.cursor() as cur:
        sets = ", ".join(f"{k} = %s" for k in updates)
        vals = list(updates.values()) + [hn]
        cur.execute(f"UPDATE hd_patient_mapping SET {sets} WHERE hn = %s RETURNING id, patient_name", vals)
        r = cur.fetchone()
        if not r:
            conn.close()
            return jsonify({"error": "ไม่พบ HN"}), 404
        conn.commit()
    conn.close()
    log_action(user_id=g.get("hd_user_id"), action="patient.edit", resource_type="patient", resource_id=hn,
               new_values=updates, ip_address=request.remote_addr)
    return jsonify({"ok": True, "hn": hn, "updated": updates})


@hd_bp.route("/patients/create", methods=["POST"])
@require_role("physician", "nurse", "admin")
def create_patient():
    """เพิ่มคนไข้ใหม่."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    data = request.get_json(silent=True) or {}
    hn = (data.get("hn") or "").strip()
    name = (data.get("patient_name") or "").strip()
    if not hn or not name:
        return jsonify({"error": "ต้องระบุ HN และชื่อ-สกุล"}), 400
    conn = psycopg2.connect(db_url, connect_timeout=5)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM hd_patient_mapping WHERE hn = %s", (hn,))
        if cur.fetchone():
            conn.close()
            return jsonify({"error": f"HN {hn} มีอยู่แล้ว"}), 409
        cur.execute("""INSERT INTO hd_patient_mapping (hn, patient_name, hospital_hn, vascular_access,
                       schedule_days, dry_weight, default_shift, is_active)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE) RETURNING id""",
                    (hn, name, data.get("hospital_hn", ""), data.get("vascular_access", ""),
                     data.get("schedule_days", ""), data.get("dry_weight"), data.get("default_shift", "morning")))
        pid = cur.fetchone()[0]
        conn.commit()
    conn.close()
    log_action(user_id=g.get("hd_user_id"), action="patient.create", resource_type="patient", resource_id=hn,
               new_values={"name": name, "id": pid}, ip_address=request.remote_addr)
    return jsonify({"ok": True, "id": pid, "hn": hn, "patient_name": name}), 201


@hd_bp.route("/patients/inactive", methods=["GET"])
@require_role("physician", "admin")
def list_inactive_patients():
    """รายชื่อคนไข้ที่ระงับ — สำหรับกู้คืน."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    with conn.cursor() as cur:
        cur.execute("""SELECT hn, patient_name, hospital_hn, vascular_access, schedule_days
                       FROM hd_patient_mapping WHERE is_active = FALSE ORDER BY patient_name""")
        rows = cur.fetchall()
    conn.close()
    return jsonify({"patients": [{"hn": r[0], "patient_name": r[1], "hospital_hn": r[2],
                                   "vascular_access": r[3], "schedule_days": r[4]} for r in rows],
                    "total": len(rows)})


@hd_bp.route("/patients/simple-list", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def patients_simple_list():
    """รายชื่อคนไข้แบบง่ายที่สุด — ไม่ JOIN, ไม่ใช้ VIEW, ไม่มีทาง crash."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"patients": []})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT hn, patient_name, vascular_access, schedule_days FROM hd_patient_mapping WHERE is_active = TRUE ORDER BY patient_name LIMIT 300")
            rows = cur.fetchall()
        conn.close()
        return jsonify({"patients": [{"hn": r[0], "name": r[1], "va": r[2], "sched": r[3]} for r in rows]})
    except Exception as e:
        logger.error("patients_simple_list: %s", e)
        return jsonify({"patients": [], "error": str(e)})


@hd_bp.route("/patients/with-lab-summary", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def patients_with_lab_summary():
    """รายชื่อคนไข้ + ค่า Lab ล่าสุด (Hb, K+, Kt/V) สำหรับหน้า Lab Index."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"patients": []})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        conn.autocommit = True  # each query independent — one fail doesn't break the rest
        with conn.cursor() as cur:
            # Try full query with hd_labs VIEW first
            rows = None
            try:
                cur.execute("""SELECT p.hn, p.patient_name, p.vascular_access, p.schedule_days,
                                      p.is_active, p.default_shift,
                                      lb.lab_date, lb.hb, lb.k, lb.ktv
                               FROM hd_patient_mapping p
                               LEFT JOIN LATERAL (
                                   SELECT lab_date, hb, k, ktv FROM hd_labs
                                   WHERE patient_id = p.id ORDER BY lab_date DESC NULLS LAST LIMIT 1
                               ) lb ON TRUE
                               WHERE p.is_active = TRUE
                               ORDER BY lb.lab_date ASC NULLS FIRST, p.patient_name""")
                rows = cur.fetchall()
            except Exception as view_err:
                logger.warning("with-lab-summary: hd_labs VIEW failed (%s), falling back to basic query", view_err)
            # Fallback: just patient list without lab data
            if rows is None:
                try:
                    cur.execute("""SELECT hn, patient_name, vascular_access, schedule_days,
                                         is_active, NULL::VARCHAR,
                                         NULL::DATE, NULL::NUMERIC, NULL::NUMERIC, NULL::NUMERIC
                                  FROM hd_patient_mapping WHERE is_active = TRUE
                                  ORDER BY patient_name""")
                    rows = cur.fetchall()
                except Exception as e2:
                    logger.error("with-lab-summary fallback also failed: %s", e2)
                    rows = []
        conn.close()
        patients = []
        for r in rows:
            lab_date = r[6].isoformat() if r[6] else None
            lab_age = (_thai_today() - r[6]).days if r[6] else None
            patients.append({
                "hn": r[0], "patient_name": r[1], "vascular_access": r[2],
                "schedule_days": r[3], "is_active": r[4], "default_shift": r[5],
                "lab_date": lab_date, "lab_age_days": lab_age,
                "hb": float(r[7]) if r[7] else None,
                "k": float(r[8]) if r[8] else None,
                "ktv": float(r[9]) if r[9] else None,
            })
        return jsonify({"patients": patients, "total": len(patients)})
    except Exception as e:
        logger.error("patients_with_lab_summary: %s", e)
        return jsonify({"patients": [], "error": str(e)})


@hd_bp.route("/labs/snapshot", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def labs_snapshot():
    """ผล Lab ของทุกคน ณ วันที่เลือก (?date=YYYY-MM-DD) — ค่า Lab ล่าสุดที่ไม่เกินวันนั้น
    + รายการวันที่ที่มีผล Lab (สำหรับ dropdown เลือกย้อนหลัง). NEW endpoint — ไม่แตะของเดิม."""
    import os, psycopg2
    from datetime import date as _date
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"patients": [], "dates": []})
    q_date = (request.args.get("date") or "").strip()
    try:
        as_of = _date.fromisoformat(q_date) if q_date else _thai_today()
    except ValueError:
        return jsonify({"error": f"รูปแบบวันที่ไม่ถูกต้อง: {q_date} (ใช้ YYYY-MM-DD)"}), 400
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        conn.autocommit = True
        dates, rows = [], []
        with conn.cursor() as cur:
            try:
                cur.execute("""SELECT DISTINCT lab_date FROM hd_labs
                               WHERE lab_date IS NOT NULL ORDER BY lab_date DESC LIMIT 48""")
                dates = [r[0].isoformat() for r in cur.fetchall()]
            except Exception as e:
                logger.warning("labs_snapshot dates: %s", e)
            try:
                cur.execute("""SELECT p.hn, p.patient_name, p.vascular_access, p.schedule_days,
                                      lb.lab_date, lb.hb, lb.k, lb.ktv
                               FROM hd_patient_mapping p
                               LEFT JOIN LATERAL (
                                   SELECT lab_date, hb, k, ktv FROM hd_labs
                                   WHERE patient_id = p.id AND lab_date <= %s
                                   ORDER BY lab_date DESC NULLS LAST LIMIT 1
                               ) lb ON TRUE
                               WHERE p.is_active = TRUE
                               ORDER BY p.patient_name""", (as_of,))
                rows = cur.fetchall()
            except Exception as e:
                logger.error("labs_snapshot query: %s", e)
        conn.close()
        patients = [{
            "hn": r[0], "patient_name": r[1], "vascular_access": r[2], "schedule_days": r[3],
            "lab_date": r[4].isoformat() if r[4] else None,
            "lab_age_days": (as_of - r[4]).days if r[4] else None,
            "hb": float(r[5]) if r[5] is not None else None,
            "k": float(r[6]) if r[6] is not None else None,
            "ktv": float(r[7]) if r[7] is not None else None,
        } for r in rows]
        return jsonify({"as_of": as_of.isoformat(), "patients": patients,
                        "total": len(patients), "dates": dates})
    except Exception as e:
        logger.error("labs_snapshot: %s", e)
        return jsonify({"patients": [], "dates": [], "error": str(e)})


WEEK_DAYS = ["จ", "อ", "พ", "พฤ", "ศ", "ส", "อา"]
SHIFT_ORDER = ["morning", "afternoon", "evening"]


@hd_bp.route("/patients/weekly-grid", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor", "display")
def patients_weekly_grid():
    """Who dialyses on which day and round: 7 days × 3 shifts from the roster
    (hd_patient_mapping.schedule_days / default_shift). Read-only; capacity = TOTAL_BEDS."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    grid = {d: {sh: [] for sh in SHIFT_ORDER + ["unknown"]} for d in WEEK_DAYS}
    no_days, total = [], 0
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""SELECT hn, patient_name, schedule_days, default_shift, vascular_access, hd_per_week, id
                           FROM hd_patient_mapping WHERE is_active = TRUE ORDER BY patient_name""")
            prow = cur.fetchall()
            from .serology import latest_for as _sero_latest
            sero = _sero_latest(cur, [r[6] for r in prow])
            for hn, name, days, shift, va, hdw, pid in prow:
                total += 1
                sr = sero.get(pid) or {}
                item = {"hn": hn, "name": name, "va": va or "", "shift": shift or "", "hd_per_week": hdw,
                        "hbsag": sr.get("hbsag"), "antihcv": sr.get("antihcv")}
                parts = [x.strip() for x in str(days or "").replace("/", ",").split(",") if x.strip()]
                parts = [x for x in parts if x in WEEK_DAYS]
                if not parts:
                    no_days.append(item)
                    continue
                for d in parts:
                    grid[d][shift if shift in SHIFT_ORDER else "unknown"].append(item)
        conn.close()
    except Exception as e:
        logger.error("weekly_grid error: %s", e)
        return jsonify({"error": str(e)}), 500
    counts = {d: {sh: len(grid[d][sh]) for sh in grid[d]} for d in WEEK_DAYS}
    return jsonify({"days": WEEK_DAYS, "shifts": SHIFT_ORDER, "capacity": HdConfig.TOTAL_BEDS, "grid": grid,
                    "counts": counts, "no_days": no_days, "total": total,
                    "generated_at": (datetime.now(timezone.utc) + timedelta(hours=7)).isoformat(timespec="minutes")})


def notify_patients_new_results(hns, kind="ผลเลือด"):
    """ว้าว: push "ผลใหม่มาแล้ว" to LINE-linked patients after a confirmed import.

    Quota-guarded by LINE_PUSH_FN (monthly-quota check lives inside push_message),
    PDPA-aware (withdrawn always blocks; required consent per ⚙️), never raises.
    → {"notified": n, "no_line": n, "blocked": n, "off": bool}"""
    import os
    out = {"notified": 0, "no_line": 0, "blocked": 0, "off": False}
    try:
        if (get_setting_value("patient_lab_notify", "on") or "on").lower() in ("off", "0", "false"):
            out["off"] = True
            return out
        push_fn = current_app.config.get("LINE_PUSH_FN")
        if not push_fn:
            return out
        require_consent = (get_setting_value("patient_push_requires_consent", "off") or "off").lower() == "on"
        consent = {}
        try:
            import psycopg2
            from .pdpa import status_for
            _c = psycopg2.connect(os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", ""), connect_timeout=5)
            try:
                with _c.cursor() as cur:
                    for hn in set(hns):
                        consent[hn] = (status_for(cur, hn).get("line_messaging") or {}).get("status")
            finally:
                _c.close()
        except Exception as e:
            logger.warning("notify consent lookup: %s", e)
        liff_id = os.environ.get("LIFF_ID", "")
        base = (os.environ.get("RENDER_EXTERNAL_URL") or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
        url = f"https://liff.line.me/{liff_id}/hd-labs" if liff_id else f"{base}/liff/hd-labs"
        msg = ("🩸 " + kind + "ใหม่ของคุณมาแล้ว\n"
               "━━━━━━━━━━━━\n"
               "แตะดูค่าเลือด กราฟแนวโน้ม และคำแนะนำ:\n" + url +
               "\n\n(ข้อความอัตโนมัติจากคลินิกระนอง รักษ์ไต)")
        for hn in sorted(set(hns)):
            st = consent.get(hn)
            if st == "withdrawn" or (require_consent and st != "given"):
                out["blocked"] += 1
                continue
            lid = _resolve_patient_line_id(hn)
            if not lid:
                out["no_line"] += 1
                continue
            try:
                if push_fn(lid, msg) is not False:
                    out["notified"] += 1
            except Exception as e:
                logger.warning("notify patient %s: %s", hn, e)
        if out["notified"]:
            log_action(user_id=g.get("hd_user_id"), action="labs.patient_notify", resource_type="labs",
                       new_values={"kind": kind, **out}, ip_address=(request.remote_addr or "") if request else "")
    except Exception as e:
        logger.warning("notify_patients_new_results: %s", e)
    return out


@hd_bp.route("/patients/<hn>/goal", methods=["GET", "POST"])
@require_role("physician", "nurse", "admin")
def patient_goal(hn):
    """🎯 เป้าหมายจากแพทย์ รายคน — shown on the patient's own portal.

    GET → current goal. POST {goal} (physician only) sets it; empty goal clears.
    Setting a goal pushes one LINE message to the linked patient (quota/PDPA-guarded)."""
    import os
    import psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        if request.method == "GET":
            with conn.cursor() as cur:
                cur.execute("SELECT patient_goal, goal_set_by, goal_set_at FROM hd_patient_mapping WHERE hn = %s", (hn,))
                r = cur.fetchone()
            if not r:
                return jsonify({"error": "ไม่พบผู้ป่วย"}), 404
            return jsonify({"hn": hn, "goal": r[0] or "", "set_by": r[1],
                            "set_at": r[2].isoformat() if r[2] else None})
        if g.get("hd_role") != "physician" and g.get("hd_role") != "admin":
            return jsonify({"error": "เฉพาะแพทย์เท่านั้นที่ตั้งเป้าหมายได้"}), 403
        data = request.get_json(silent=True) or {}
        goal = (data.get("goal") or "").strip()[:300]
        who = g.get("hd_username") or str(g.get("hd_user_id") or "")
        with conn.cursor() as cur:
            cur.execute("""UPDATE hd_patient_mapping SET patient_goal = %s,
                           goal_set_by = CASE WHEN %s <> '' THEN %s ELSE NULL END,
                           goal_set_at = CASE WHEN %s <> '' THEN NOW() ELSE NULL END,
                           updated_at = NOW() WHERE hn = %s RETURNING id""",
                        (goal or None, goal, who, goal, hn))
            if not cur.fetchone():
                return jsonify({"error": "ไม่พบผู้ป่วย"}), 404
        conn.commit()
        log_action(user_id=g.get("hd_user_id"), action="patient.goal_set", resource_type="patient", resource_id=hn,
                   new_values={"goal": goal}, ip_address=request.remote_addr or "")
        pushed = {}
        if goal:
            pushed = notify_patients_new_results([hn], kind="🎯 เป้าหมายจากแพทย์")
        return jsonify({"ok": True, "goal": goal, "pushed": pushed.get("notified", 0)})
    finally:
        conn.close()


@hd_bp.route("/patients/<hn>/bookings", methods=["GET"])
@require_role("physician", "nurse", "admin")
def patient_bookings(hn):
    """รอบที่จองไว้ล่วงหน้าของผู้ป่วย (วันนี้เป็นต้นไป) — ใช้ในหน้าต่างจองรอบ."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"items": []})
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT s.id, s.session_date, COALESCE(s.shift,'morning'), s.bed_number
                           FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                           WHERE p.hn = %s AND s.status = 'scheduled' AND s.session_date >= %s
                           ORDER BY s.session_date, s.shift LIMIT 20""", (hn, _thai_today().isoformat()))
            items = [{"session_id": r[0], "date": r[1].isoformat(), "shift": r[2], "bed": r[3]} for r in cur.fetchall()]
    finally:
        conn.close()
    return jsonify({"items": items})


@hd_bp.route("/schedule/bed-availability", methods=["GET"])
@require_role("physician", "nurse", "admin")
def bed_availability():
    """เตียงที่ถูกใช้/จองแล้วในวัน+รอบที่ระบุ — ?date=YYYY-MM-DD&shift=morning."""
    import os, psycopg2
    from datetime import date as _date
    try:
        day = _date.fromisoformat((request.args.get("date") or _thai_today().isoformat())[:10])
    except ValueError:
        return jsonify({"error": "รูปแบบวันที่ไม่ถูกต้อง"}), 400
    shift = request.args.get("shift", "morning")
    if shift not in ("morning", "afternoon", "evening"):
        shift = "morning"
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    taken = {}
    if db_url:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT s.bed_number, p.patient_name FROM hd_sessions s
                               JOIN hd_patient_mapping p ON p.id = s.patient_id
                               WHERE s.session_date = %s AND COALESCE(s.shift,'morning') = %s
                                 AND s.status IN ('scheduled', 'in_progress')""", (day.isoformat(), shift))
                taken = {int(r[0]): r[1] for r in cur.fetchall() if r[0]}
        finally:
            conn.close()
    return jsonify({"date": day.isoformat(), "shift": shift, "total_beds": HdConfig.TOTAL_BEDS,
                    "taken": [{"bed": b, "name": n} for b, n in sorted(taken.items())]})


@hd_bp.route("/patients/<hn>/portal-link", methods=["GET"])
@require_role("physician", "nurse", "admin")
def patient_portal_link(hn):
    """Signed Patient-Portal link for one patient (staff hands it to the patient / prints QR)."""
    import os
    base = (os.environ.get("RENDER_EXTERNAL_URL") or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
    url = f"{base}/hd/my/{portal_token(hn)}"
    qr = None
    try:
        import io as _io

        import segno
        buf = _io.BytesIO()
        segno.make(url).save(buf, kind="png", scale=5)
        import base64 as _b64
        qr = "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    log_action(user_id=g.get("hd_user_id"), action="patient.portal_link", resource_type="patient", resource_id=hn,
               ip_address=request.remote_addr or "")
    return jsonify({"hn": hn, "url": url, "qr": qr})


@hd_bp.route("/patients/<hn>/card.pdf", methods=["GET"])
@require_role("physician", "nurse", "admin")
def patient_card_pdf(hn):
    """บัตรผู้ป่วย + QR เป็น PDF — พิมพ์ได้จากมือถือทุกเครื่อง (window.print ใช้ไม่ได้บนมือถือหลายรุ่น)."""
    import os, psycopg2
    from flask import Response
    from .patient_card_pdf import card_pdf
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no database"}), 503
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT patient_name, hospital_hn, schedule_days, default_shift, vascular_access, dry_weight
                           FROM hd_patient_mapping WHERE hn = %s""", (hn,))
            r = cur.fetchone()
    finally:
        conn.close()
    if not r:
        return jsonify({"error": "ไม่พบผู้ป่วย"}), 404
    p = {"hn": hn, "name": r[0], "hospital_hn": r[1], "schedule_days": r[2], "default_shift": r[3],
         "vascular_access": r[4], "dry_weight": r[5]}
    base = (os.environ.get("RENDER_EXTERNAL_URL") or "https://ranong-raktai-ai-cloud.onrender.com").rstrip("/")
    pdf = card_pdf(p, f"{base}/hd/my/{portal_token(hn)}")
    log_action(user_id=g.get("hd_user_id"), action="patient.card_pdf", resource_type="patient", resource_id=hn,
               ip_address=request.remote_addr or "")
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="patient-card-{hn}.pdf"',
                             "Cache-Control": "no-store"})


@hd_bp.route("/labs/serology/import", methods=["POST"])
@require_role("physician", "nurse", "admin")
def serology_import():
    """Upload CSV/XLSX of HN · ชื่อ · HBsAg · Anti-HCV · วันที่. form confirm=1 writes; otherwise preview only.
    Rows are matched by HN and the first name must agree with the registry — mismatches are listed, never written."""
    import os
    import psycopg2
    from .lab_import import parse_date as _pd
    from .serology import apply as _sero_apply, parse_import
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "ไม่มีไฟล์"}), 400
    confirm = request.form.get("confirm") == "1"
    default_date = _pd(request.form.get("date", ""), None)
    try:
        rows, errors = parse_import(f.filename, f.read(), default_date)
    except Exception as e:
        return jsonify({"error": f"อ่านไฟล์ไม่ได้: {e}"}), 400
    if not rows:
        return jsonify({"error": "ไม่พบแถวข้อมูล", "errors": errors}), 400
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=10)
        with conn.cursor() as cur:
            res = _sero_apply(cur, rows, confirm=confirm, source="import:" + (f.filename or "serology")[:40])
        if confirm:
            conn.commit()
        conn.close()
    except Exception as e:
        logger.error("serology import: %s", e)
        return jsonify({"error": str(e)}), 500
    res["errors"] = errors
    res["confirmed"] = confirm
    if confirm:
        log_action(user_id=g.get("hd_user_id"), action="labs.import_serology", resource_type="labs",
                   new_values={"written": res["written"], "pos_hbsag": res["pos_hbsag"], "pos_antihcv": res["pos_antihcv"],
                               "file": (f.filename or "")[:80]}, ip_address=request.remote_addr or "")
        if res.get("written"):
            res["patient_notify"] = notify_patients_new_results(
                [m["hn"] for m in res.get("matched", []) if not m.get("duplicate")])
    return jsonify(res)


@hd_bp.route("/patients/import-roster", methods=["POST"])
@require_role("physician", "admin")
def import_patient_roster():
    """Import the annual Excel roster (VA / BFR / HD-wk / schedule / ESA).

    multipart: file=<xlsx>, confirm=0|1. confirm=0 (default) only previews.
    """
    import os, psycopg2
    from .registry_import import parse_roster, apply_roster
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "กรุณาเลือกไฟล์ Excel (.xlsx)"}), 400
    data = f.read()
    if len(data) > 5 * 1024 * 1024:
        return jsonify({"error": "ไฟล์ใหญ่เกิน 5 MB"}), 400
    confirm = request.form.get("confirm", "0") == "1"
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "Database not configured"}), 503
    try:
        rows, skipped = parse_roster(data)
    except Exception as e:
        return jsonify({"error": f"อ่านไฟล์ไม่ได้: {e}"}), 400
    # วันที่ของคอลัมน์ Hematocrit ในไฟล์ (ค่าเริ่มต้น = วันนี้) — ผล iPTH/Ferritin ใช้เดือนจากหัวคอลัมน์
    from .lab_import import parse_date as _pd
    hct_date = _pd(request.form.get("hct_date", ""), None) or date.today()
    try:
        conn = psycopg2.connect(db_url, connect_timeout=10)
        summary = apply_roster(conn, rows, confirm=confirm, hct_date=hct_date, source="import:" + f.filename[:40])
        if confirm:
            try:
                from .dw_autopilot import CREATE_SQL as _DW_SQL, supersede_stale
                with conn.cursor() as cur:
                    cur.execute(_DW_SQL)
                    summary["dw_proposals_superseded"] = supersede_stale(cur)
                conn.commit()
            except Exception as e:  # never fail the import over this
                logger.warning("roster import supersede dw: %s", e)
        conn.close()
    except Exception as e:
        logger.error("roster import: %s", e)
        return jsonify({"error": str(e)}), 500
    summary["skipped"] = skipped
    if confirm:
        log_action(user_id=g.get("hd_user_id"), action="patients.import_roster",
                   resource_type="patient",
                   new_values={"file": f.filename, "matched": summary["matched"],
                               "written": summary["written"], "unmatched": len(summary["unmatched"]),
                               "rx_written": summary["rx_written"], "rx_created": summary["rx_created"],
                               "labs_written": summary["labs_written"]},
                   ip_address=request.remote_addr or "")
        logger.info("roster import by %s: matched=%d written=%d unmatched=%d",
                    g.get("hd_username"), summary["matched"], summary["written"],
                    len(summary["unmatched"]))
        if summary.get("labs_written"):
            summary["patient_notify"] = notify_patients_new_results(
                [d["hn"] for d in summary.get("details", []) if d.get("labs")])
    return jsonify(summary)


# ── 17 พ.ย. 2569: UF coach (ข้อ 1, 9) + อ่านหน้าจอเครื่องฟอก (ข้อ 8) ──

@hd_bp.route("/patients/<hn>/uf-coach", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def patient_uf_coach(hn):
    """เพดาน UF เฉพาะราย + เกณฑ์น้ำหนักขึ้นระหว่างรอบ + น้ำหนักที่ผู้ป่วยส่งจากบ้าน (read-only)"""
    import os
    import psycopg2
    from .uf_coach import (limit_pct, patient_context, recent_home_weights, sessions_for_tolerance,
                           tolerance_from_sessions)
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    try:
        ceiling = float(get_setting_value("uf_rate_max", "13") or 13)
    except ValueError:
        ceiling = 13.0
    conn = psycopg2.connect(db_url, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            pc = patient_context(cur, hn)
            if not pc:
                return jsonify({"error": "ไม่พบผู้ป่วย"}), 404
            tol = tolerance_from_sessions(sessions_for_tolerance(cur, hn), ceiling=ceiling)
            setting_pct = get_setting_value("idwg_limit_pct", "")
            pct = limit_pct(pc["patient_pct"], setting_pct)
            home = recent_home_weights(cur, hn)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"hn": hn, "tolerance": tol, "idwg_limit_pct": pct, "idwg_patient_pct": pc["patient_pct"],
                    "idwg_default_pct": limit_pct(None, setting_pct), "dry_weight": pc["dw"],
                    "idwg_limit_kg": round(pc["dw"] * pct / 100, 1) if pc["dw"] else None,
                    "last_post_weight": pc["prev_post"], "home_weights": home})


def _preprocess_machine_image(img_bytes):
    """ปรับภาพเครื่องฟอกให้ OCR อ่านง่ายขึ้น:
    1. EXIF transpose
    2. Resize ถ้าใหญ่เกิน
    3. Enhance contrast (CLAHE) สำหรับหน้าจอเรืองแสง
    4. Sharpen ถ้าเบลอ
    5. Return enhanced bytes (PNG) + PIL image for Tesseract
    """
    import io
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes)))
    # Resize if too large (keep aspect)
    if img.width > 2000:
        ratio = 2000 / img.width
        img = img.resize((2000, int(img.height * ratio)), Image.LANCZOS)
    # Sharpen slightly (helps blurry photos)
    img = img.filter(ImageFilter.SHARPEN)
    # Enhance contrast (machine screens are often washed out in photos)
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.5)
    # Enhance brightness slightly
    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(1.1)
    # Convert to PNG bytes for Vision API
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    enhanced_bytes = buf.getvalue()
    # Grayscale + threshold for Tesseract
    gray = img.convert("L")
    # Adaptive-like threshold: boost contrast more
    enhancer = ImageEnhance.Contrast(gray)
    gray = enhancer.enhance(2.0)
    return enhanced_bytes, gray


def _ocr_words(img_bytes):
    """รูป → [{t,x,y,w,h}] : Google Vision (มีตำแหน่งคำ) → สำรอง Tesseract image_to_data
    รูปหน้าจอเครื่องไม่ถูกเก็บ — ประมวลผลในหน่วยความจำแล้วทิ้ง"""
    words = []
    # Preprocess image for better OCR
    try:
        enhanced_bytes, gray_img = _preprocess_machine_image(img_bytes)
    except Exception as e:
        logger.info("machine-screen preprocess: %s", type(e).__name__)
        enhanced_bytes, gray_img = img_bytes, None
    # 1. Google Vision (best accuracy)
    try:
        from google.cloud import vision
        client = vision.ImageAnnotatorClient()
        resp = client.document_text_detection(image=vision.Image(content=enhanced_bytes))
        if not resp.error.message and resp.full_text_annotation:
            for page in resp.full_text_annotation.pages:
                for block in page.blocks:
                    for para in block.paragraphs:
                        for w in para.words:
                            t = "".join(sym.text for sym in w.symbols)
                            xs = [v.x for v in w.bounding_box.vertices]
                            ys = [v.y for v in w.bounding_box.vertices]
                            words.append({"t": t, "x": min(xs), "y": min(ys), "w": max(xs) - min(xs), "h": max(ys) - min(ys)})
            if words:
                return words, "vision"
    except Exception as e:
        logger.info("machine-screen vision: %s", type(e).__name__)
    # 2. Tesseract fallback with preprocessed grayscale
    try:
        import pytesseract
        ocr_img = gray_img
        if ocr_img is None:
            import io
            from PIL import Image, ImageOps
            ocr_img = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes))).convert("L")
        if ocr_img.width > 1600:
            ocr_img = ocr_img.resize((1600, int(ocr_img.height * 1600 / ocr_img.width)))
        # Try multiple PSM modes: 11 (sparse) then 6 (block) — pick whichever finds more
        best_words = []
        for psm in ("11", "6"):
            try:
                d = pytesseract.image_to_data(ocr_img, config=f"--psm {psm}", output_type=pytesseract.Output.DICT, timeout=20)
                wds = []
                for i, t in enumerate(d["text"]):
                    if t and t.strip():
                        wds.append({"t": t.strip(), "x": d["left"][i], "y": d["top"][i], "w": d["width"][i], "h": d["height"][i]})
                if len(wds) > len(best_words):
                    best_words = wds
            except Exception:
                pass
        words = best_words
    except Exception as e:
        logger.info("machine-screen tesseract: %s", type(e).__name__)
    return words, "tesseract"


def _machine_ai_read(img_bytes):
    """อ่านหน้าจอเครื่องฟอกด้วย Claude Vision — ตัวเลข 7-segment ที่ OCR ปกติอ่านไม่ออก.
    คืน dict ค่าที่อ่านได้ หรือ None เมื่อไม่มี key/อ่านไม่ได้. รูปไม่ถูกเก็บ."""
    import os, json as _json, base64, io
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    # ย่อรูปให้พอดีกับ API (ด้านยาว ≤1568px, JPEG) — เร็วขึ้นและไม่เกิน limit
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes))).convert("RGB")
        if max(im.size) > 1568:
            im.thumbnail((1568, 1568))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        img_bytes = buf.getvalue()
    except Exception:
        pass
    import requests as _rq
    prompt = (
        "นี่คือรูปหน้าจอเครื่องฟอกไต (Nikkiso/Fresenius) อ่านตัวเลขบนจอแล้วตอบเป็น JSON เท่านั้น:\n"
        '{"uf_removed": <mL หรือ null>, "uf_rate": <mL/hr หรือ null>, "uf_goal": <mL หรือ null>, '
        '"time_left_min": <นาทีที่เหลือ หรือ null>, "sbp": <หรือ null>, "dbp": <หรือ null>, "bfr": <mL/min หรือ null>}\n'
        "กติกา: UF REMOVED/VOLUME คือที่ดึงไปแล้ว, UF GOAL/TARGET คือเป้า, UF RATE คืออัตรา — "
        "ถ้าจอแสดง UF เป็นลิตร (เช่น 2.50) ให้คูณ 1000 เป็น mL. เวลาที่เหลือรูป H:MM ให้แปลงเป็นนาที. "
        "อ่านไม่ชัดให้ใส่ null ห้ามเดา. ตอบ JSON อย่างเดียว"
    )
    body = _json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(img_bytes).decode()}},
            {"type": "text", "text": prompt},
        ]}],
    }).encode("utf-8")
    resp = _rq.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    data=body, timeout=45)
    if resp.status_code != 200:
        logger.info("machine-screen claude HTTP %s", resp.status_code)
        return None
    txt = resp.json()["content"][0]["text"].strip()
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    raw = _json.loads(txt)
    # sanity-check ขอบเขตที่เป็นไปได้ — ค่าเพี้ยนตัดทิ้ง
    sane = {"uf_removed": (0, 8000), "uf_rate": (0, 3000), "uf_goal": (0, 8000),
            "time_left_min": (0, 600), "sbp": (50, 260), "dbp": (20, 160), "bfr": (50, 500)}
    out = {}
    for k, (lo, hi) in sane.items():
        v = raw.get(k)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if lo <= v <= hi:
            out[k] = int(v) if k != "uf_rate" or v == int(v) else v
    return out or None


@hd_bp.route("/machine-screen/read", methods=["POST"])
@require_role("physician", "nurse", "admin")
def read_machine_screen():
    """ข้อ 8: รูปหน้าจอเครื่องฟอก → UF removed / UF rate / UF goal / เวลาที่เหลือ / BP
    ผลเป็นค่าแนะนำให้พยาบาลตรวจก่อนกดบันทึกเสมอ — ไม่บันทึกอะไรเอง"""
    from .uf_coach import parse_machine_text, parse_machine_words
    f = request.files.get("image")
    if not f:
        return jsonify({"error": "ไม่พบรูป"}), 400
    data = f.read()
    if not data or len(data) > 12 * 1024 * 1024:
        return jsonify({"error": "รูปว่างหรือใหญ่เกิน 12 MB"}), 400

    # 1) Claude Vision ก่อน — อ่านจอ 7-segment ได้จริง (OCR เดิมเป็น fallback)
    try:
        ai = _machine_ai_read(data)
    except Exception as e:
        logger.info("machine-screen claude: %s", type(e).__name__)
        ai = None
    if ai:
        found = [k for k in ("uf_removed", "uf_rate", "uf_goal", "time_left_min", "sbp") if k in ai]
        if found:
            log_action(user_id=g.get("hd_user_id"), action="machine_screen.read", resource_type="session",
                       resource_id=_safe_int(request.form.get("session_id")),
                       new_values={"engine": "claude-vision", "found": found}, ip_address=request.remote_addr or "")
            return jsonify({"values": ai, "engine": "claude-vision", "found": found, "words": 0})

    words, engine = _ocr_words(data)
    vals = parse_machine_words(words)
    if not any(k in vals for k in ("uf_removed", "uf_rate", "uf_goal")):
        alt = parse_machine_text(" ".join(w["t"] for w in words))
        vals = {**alt, **{k: v for k, v in vals.items() if k != "raw"}}
    found = [k for k in ("uf_removed", "uf_rate", "uf_goal", "time_left_min", "sbp") if k in vals]
    log_action(user_id=g.get("hd_user_id"), action="machine_screen.read", resource_type="session",
               resource_id=_safe_int(request.form.get("session_id")),
               new_values={"engine": engine, "found": found}, ip_address=request.remote_addr or "")
    return jsonify({"values": vals, "engine": engine, "found": found, "words": len(words)})


@hd_bp.route("/patients/<hn>", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def get_patient(hn):
    """Get HD patient details from PostgreSQL."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"hn": hn, "status": "not_found"}), 404
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, hn, hospital_hn, patient_name, date_of_birth,
                       gender, blood_type, allergies, vascular_access,
                       dry_weight, schedule_days, esa_drug, esa_dose,
                       primary_diagnosis, is_active, created_at,
                       COALESCE(bfr, 0) as bfr
                FROM hd_patient_mapping WHERE hn = %s
            """, (hn,))
            row = cur.fetchone()
            # Get va_location separately (column may not exist yet)
            va_loc = None
            sero = None
            dshift = None
            if row:
                try:
                    cur.execute("SELECT va_location, default_shift FROM hd_patient_mapping WHERE hn = %s", (hn,))
                    vr = cur.fetchone()
                    if vr:
                        va_loc, dshift = vr[0], vr[1]
                except Exception:
                    pass  # column doesn't exist yet
                try:
                    from .serology import latest_for as _sero_latest
                    sero = _sero_latest(cur, [row[0]]).get(row[0])
                except Exception:
                    sero = None
        conn.close()
        if not row:
            return jsonify({"hn": hn, "status": "not_found"}), 404
        return jsonify({
            "id": row[0], "hn": row[1], "hospital_hn": row[2],
            "patient_name": row[3],
            "date_of_birth": row[4].isoformat() if row[4] else None,
            "gender": row[5], "blood_type": row[6], "allergies": row[7],
            "vascular_access": row[8],
            "va_location": va_loc,
            "dry_weight": float(row[9]) if row[9] else None,
            "schedule_days": row[10], "esa_drug": row[11], "esa_dose": row[12],
            "primary_diagnosis": row[13], "is_active": row[14],
            "created_at": row[15].isoformat() if row[15] else None,
            "bfr": row[16] if row[16] else None,
            "serology": sero,
            "default_shift": dshift,
        })
    except Exception as e:
        logger.error("get_patient error: %s", e)
        return jsonify({"hn": hn, "error": str(e)}), 500


@hd_bp.route("/patients/<hn>/last-clinical", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_patient_last_clinical(hn):
    """Get last session's dialyzer, heparin, VA for auto-fill."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    result = {}
    if not db_url:
        return jsonify(result)
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Get patient_id
            cur.execute("SELECT id FROM hd_patient_mapping WHERE hn = %s", (hn,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify(result)
            pid = row[0]

            # Last active dialyzer
            cur.execute("""
                SELECT dialyzer_type, reuse_count, max_reuse
                FROM hd_dialyzer_tracking
                WHERE patient_id = %s AND status = 'active'
                ORDER BY id DESC LIMIT 1
            """, (pid,))
            d = cur.fetchone()
            if d:
                result["dialyzer_type"] = d[0]
                result["dialyzer_reuse"] = d[1]
                result["dialyzer_max"] = d[2]

            # Last heparin protocol from most recent session
            cur.execute("""
                SELECT a.protocol, a.loading_dose, a.maintenance_dose
                FROM hd_anticoagulation a
                JOIN hd_sessions s ON s.id = a.session_id
                WHERE s.patient_id = %s
                ORDER BY a.recorded_at DESC LIMIT 1
            """, (pid,))
            h = cur.fetchone()
            if h:
                result["hep_drug"] = h[0]
                result["hep_bolus"] = h[1]
                result["hep_maintenance"] = h[2]

            # VA + ตำแหน่ง + วันที่ + น้ำหนักหลังฟอกครั้งก่อน + เครื่องฟอก
            cur.execute("SAVEPOINT sp_lc_va")
            try:
                cur.execute("SELECT vascular_access, va_location FROM hd_patient_mapping WHERE id = %s", (pid,))
                v = cur.fetchone()
                if v:
                    result["access_type"], result["va_location"] = v[0], v[1]
                cur.execute("""SELECT session_date, post_weight, bed_number FROM hd_sessions WHERE patient_id = %s
                               AND status = 'completed' AND post_weight IS NOT NULL
                               ORDER BY session_date DESC, id DESC LIMIT 1""",
                            (pid,))
                ld = cur.fetchone()
                if ld:
                    if ld[0]:
                        result["last_date"] = ld[0].isoformat()
                    if ld[1] is not None:
                        result["prev_post_weight"] = float(ld[1])
                else:
                    # ไม่มีรอบ completed → ดูรอบล่าสุดแทน (อาจเป็น in_progress)
                    cur.execute("""SELECT session_date FROM hd_sessions WHERE patient_id = %s
                                   AND status IN ('completed', 'in_progress')
                                   ORDER BY session_date DESC, id DESC LIMIT 1""", (pid,))
                    ld2 = cur.fetchone()
                    if ld2 and ld2[0]:
                        result["last_date"] = ld2[0].isoformat()
                cur.execute("RELEASE SAVEPOINT sp_lc_va")
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT sp_lc_va")
                logger.debug("last-clinical va: %s", e)

            # BFR default from patient mapping
            cur.execute("SAVEPOINT sp_lc_bfr")
            try:
                cur.execute("SELECT bfr FROM hd_patient_mapping WHERE id = %s", (pid,))
                bfr_row = cur.fetchone()
                if bfr_row and bfr_row[0]:
                    result["default_bfr"] = bfr_row[0]
                cur.execute("RELEASE SAVEPOINT sp_lc_bfr")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_lc_bfr")

            # Dialysate defaults from latest prescription
            cur.execute("SAVEPOINT sp_lc_rx")
            try:
                cur.execute("""SELECT duration_hours, dialysate_k, dialysate_ca, dialysate_hco3,
                                      dialysate_na, dialysate_temp, dialysate_flow, blood_flow
                               FROM hd_prescriptions
                               WHERE patient_id = %s AND is_active = TRUE
                               ORDER BY prescribed_date DESC LIMIT 1""", (pid,))
                rx = cur.fetchone()
                if rx:
                    result["rx_duration"] = float(rx[0]) if rx[0] else None
                    result["rx_dialysate_k"] = float(rx[1]) if rx[1] else None
                    result["rx_dialysate_ca"] = float(rx[2]) if rx[2] else None
                    result["rx_dialysate_hco3"] = float(rx[3]) if rx[3] else None
                    result["rx_dialysate_na"] = float(rx[4]) if rx[4] else None
                    result["rx_dialysate_temp"] = float(rx[5]) if rx[5] else None
                    result["rx_dialysate_flow"] = float(rx[6]) if rx[6] else None
                    result["rx_blood_flow"] = float(rx[7]) if rx[7] else None
                    # Use rx blood_flow as BFR default if patient mapping BFR is empty
                    if not result.get("default_bfr") and rx[7]:
                        result["default_bfr"] = int(rx[7])
                cur.execute("RELEASE SAVEPOINT sp_lc_rx")
            except Exception as rx_e:
                cur.execute("ROLLBACK TO SAVEPOINT sp_lc_rx")
                logger.debug("last-clinical rx: %s", rx_e)

            # เครื่องฟอก (Machine) ที่ผูกเตียง — ดึงจาก bed_number ล่าสุดหรือเตียงที่เลือก
            cur.execute("SAVEPOINT sp_lc_machine")
            try:
                bed_param = request.args.get("bed")
                if bed_param:
                    cur.execute("""SELECT e.id, e.brand, e.model, e.serial_number, e.status
                                   FROM hd_equipment e
                                   WHERE e.bed_number = %s AND e.equipment_type = 'dialysis_machine'
                                     AND e.status = 'active' LIMIT 1""", (int(bed_param),))
                    mr = cur.fetchone()
                    if mr:
                        result["machine"] = {"id": mr[0], "brand": mr[1], "model": mr[2], "serial": mr[3], "status": mr[4]}
                cur.execute("RELEASE SAVEPOINT sp_lc_machine")
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT sp_lc_machine")
                logger.debug("last-clinical machine: %s", e)

        conn.close()
    except Exception as e:
        logger.warning("last-clinical error: %s", e)
    return jsonify(result)


@hd_bp.route("/patients/<hn>/history", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def get_patient_history(hn):
    """Get patient's HD session history + vital trends."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"sessions": [], "vitals": []})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM hd_patient_mapping WHERE hn = %s", (hn,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"sessions": [], "vitals": [],
                                "error": "patient not found"}), 404
            pid = row[0]

        # Last 30 sessions
        sessions = []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, bed_number, status, started_at, ended_at,
                       pre_weight, post_weight, actual_uf, notes
                FROM hd_sessions
                WHERE patient_id = %s
                ORDER BY started_at DESC LIMIT 30
            """, (pid,))
            for r in cur.fetchall():
                sessions.append({
                    "id": r[0], "bed": r[1], "status": r[2],
                    "started_at": (r[3].isoformat() + "Z") if r[3] else None,
                    "ended_at": (r[4].isoformat() + "Z") if r[4] else None,
                    "pre_weight": float(r[5]) if r[5] else None,
                    "post_weight": float(r[6]) if r[6] else None,
                    "actual_uf": float(r[7]) if r[7] else None,
                    "notes": r[8],
                })

        # Last 100 vital signs across all sessions
        vitals = []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.id, v.session_id, v.phase, v.sbp, v.dbp,
                       v.heart_rate, v.temperature, v.spo2, v.recorded_at
                FROM hd_vital_signs v
                JOIN hd_sessions s ON s.id = v.session_id
                WHERE s.patient_id = %s AND v.sbp IS NOT NULL
                ORDER BY v.recorded_at DESC LIMIT 100
            """, (pid,))
            for r in cur.fetchall():
                vitals.append({
                    "id": r[0], "session_id": r[1], "phase": r[2],
                    "sbp": r[3], "dbp": r[4], "hr": r[5],
                    "temp": float(r[6]) if r[6] else None,
                    "spo2": r[7],
                    "recorded_at": (r[8].isoformat() + "Z") if r[8] else None,
                })
        conn.close()
        return jsonify({"sessions": sessions, "vitals": vitals})
    except Exception as e:
        logger.error("patient_history error: %s", e)
        return jsonify({"sessions": [], "vitals": [], "error": str(e)})


# ── Dashboard ─────────────────────────────────────────────

@hd_bp.route("/dashboard/beds", methods=["GET"])
@require_role("physician", "nurse", "admin", "display")
def bed_dashboard():
    """25-bed real-time dashboard with active sessions, filtered by shift."""
    import os
    from .config import HdConfig
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    current_shift = request.args.get("shift", "morning")
    # Allow date override for day-of-week navigation (YYYY-MM-DD)
    date_str = request.args.get("date", "")
    if date_str:
        try:
            from datetime import date as _dt_date
            target_date = _dt_date.fromisoformat(date_str)
        except (ValueError, TypeError):
            target_date = _thai_today()
    else:
        target_date = _thai_today()

    beds = []
    completed_sessions = []
    cancelled_sessions = []
    for i in range(1, HdConfig.TOTAL_BEDS + 1):
        beds.append({
            "bed_number": i,
            "status": "available",
            "patient": None,
            "current_vitals": None,
            "alerts": [],
        })

    # Fill beds from active sessions
    _dash_error = ""
    if db_url:
        try:
            import psycopg2
            conn = psycopg2.connect(db_url, connect_timeout=5)
            conn.autocommit = True   # read-only: one failed lookup must not abort the rest
            with conn.cursor() as cur:
                # Show ALL active sessions (in_progress, scheduled, completed)
                # Priority: in_progress > completed > scheduled on same bed
                # กฎแสดงเตียง:
                # - รอบเดียวกัน: แสดงทุก status (scheduled, in_progress, completed)
                # - in_progress จากรอบก่อนหน้า: แสดงด้วย (เตียงยังไม่ว่าง)
                #   เช้า→ดูบ่าย=เห็นเช้าที่ค้าง, บ่าย→ดูค่ำ=เห็นบ่ายที่ค้าง
                # - in_progress จากรอบหลัง: ไม่แสดง (ดูบ่าย ไม่เห็นค่ำ)
                _SHIFT_ORDER = {'morning': 0, 'afternoon': 1, 'evening': 2}
                _cur_ord = _SHIFT_ORDER.get(current_shift, 0)
                # Shifts ที่จะแสดง in_progress: รอบปัจจุบัน + รอบก่อนหน้า
                _show_ip_shifts = [current_shift]
                if current_shift == 'afternoon':
                    _show_ip_shifts.append('morning')
                elif current_shift == 'evening':
                    _show_ip_shifts.extend(['morning', 'afternoon'])

                cur.execute("""
                    SELECT s.bed_number, s.id as session_id, s.status,
                           p.hn, p.patient_name, p.vascular_access,
                           p.dry_weight, p.esa_drug,
                           s.started_at, 4 as duration_hours,
                           s.esa_given, s.esa_drug_given,
                           s.shift as saved_shift,
                           s.ended_at, s.pre_weight, s.post_weight, s.actual_uf,
                           (SELECT COUNT(*) FROM hd_clinical_alerts a WHERE a.session_id = s.id) AS n_alerts,
                           p.default_shift, p.schedule_days, p.esa_dose,
                           s.cancel_reason
                    FROM hd_sessions s
                    JOIN hd_patient_mapping p ON p.id = s.patient_id
                    WHERE s.session_date = %s
                      AND (
                        -- รอบเดียวกัน: ทุก status
                        (s.shift = %s OR (s.shift IS NULL AND %s = 'morning'))
                        OR
                        -- in_progress จากรอบก่อนหน้า (ฟอกยังไม่เสร็จ)
                        (s.status = 'in_progress' AND COALESCE(s.shift,'morning') = ANY(%s))
                      )
                    ORDER BY s.bed_number,
                             CASE s.status
                               WHEN 'scheduled' THEN 0
                               WHEN 'completed' THEN 1
                               WHEN 'in_progress' THEN 2
                             END
                """, (target_date.isoformat(), current_shift, current_shift, _show_ip_shifts))
                rows = cur.fetchall()
                logger.info("bed_dashboard: date=%s shift=%s rows=%d",
                            target_date.isoformat(), current_shift, len(rows))
                session_ids = []
                for row in rows:
                    bed_num = row[0]
                    if 1 <= bed_num <= HdConfig.TOTAL_BEDS:
                        db_status = row[2]
                        if db_status == "completed":
                            # Finished sessions release the bed so the next
                            # round can start; they are listed separately and
                            # stay in the daily log / session page forever.
                            completed_sessions.append({
                                "session_id": row[1], "bed_number": bed_num,
                                "patient_hn": row[3], "patient_name": row[4],
                                "vascular_access": row[5],
                                "start_time": (row[8].isoformat() + "Z") if row[8] else None,
                                "end_time": (row[13].isoformat() + "Z") if row[13] else None,
                                "pre_weight": float(row[14]) if row[14] else None,
                                "post_weight": float(row[15]) if row[15] else None,
                                "actual_uf": float(row[16]) if row[16] else None,
                                "esa_given": row[10], "esa_drug_given": row[11],
                                "alert_count": int(row[17] or 0),
                            })
                            continue
                        if db_status == "cancelled":
                            # ยกเลิกแล้ว = ปล่อยเตียงสมบูรณ์ — แยกไปแถบ "ยกเลิกวันนี้" (กดเอากลับได้)
                            cancelled_sessions.append({
                                "session_id": row[1], "bed_number": bed_num,
                                "patient_hn": row[3], "patient_name": row[4],
                                "reason": row[21] if len(row) > 21 else None,
                            })
                            continue
                        js_status = {"in_progress": "active", "scheduled": "pre_hd", "completed": "done"}.get(db_status, db_status)
                        # Priority: in_progress > completed > scheduled on same bed
                        current_bed = beds[bed_num - 1]
                        cur_st = current_bed.get("status")
                        if cur_st == "active" and js_status in ("pre_hd", "done"):
                            # Bed already active — mark next_scheduled instead
                            beds[bed_num - 1]["next_scheduled"] = row[4]  # patient_name
                            beds[bed_num - 1]["next_scheduled_hn"] = row[3]
                            continue
                        beds[bed_num - 1] = {
                            "bed_number": bed_num,
                            "status": js_status,
                            "session_id": row[1],
                            "patient_name": row[4],
                            "patient_hn": row[3],
                            "vascular_access": row[5],
                            "patient": {
                                "hn": row[3],
                                "name": row[4],
                                "vascular_access": row[5],
                                "dry_weight": float(row[6]) if row[6] else None,
                                "esa_drug": row[7],
                                "esa_dose": row[20] if len(row) > 20 else None,
                            },
                            "start_time": (row[8].isoformat() + "Z") if row[8] else None,
                            "duration_hours": float(row[9]) if row[9] else 4,
                            "esa_given": row[10],
                            "esa_drug_given": row[11],
                            "current_vitals": None,
                            "alerts": [],
                            # regular shift/days from the roster — flag when today's round differs
                            "usual_shift": row[18] if len(row) > 18 else None,
                            "schedule_days": row[19] if len(row) > 19 else None,
                            "shift_mismatch": bool(len(row) > 18 and row[18] and row[18] != current_shift),
                        }
                        session_ids.append((row[1], bed_num))

                # Fetch latest vitals for active sessions
                for sid, bnum in session_ids:
                    try:
                        cur.execute("""
                            SELECT sbp, dbp, heart_rate, temperature, spo2, recorded_at
                            FROM hd_vital_signs
                            WHERE session_id = %s AND sbp IS NOT NULL
                            ORDER BY recorded_at DESC LIMIT 1
                        """, (sid,))
                        vrow = cur.fetchone()
                        if vrow and 1 <= bnum <= len(beds):
                            beds[bnum - 1]["current_vitals"] = {
                                "sbp": vrow[0], "dbp": vrow[1],
                                "hr": vrow[2], "temp": vrow[3], "spo2": vrow[4],
                            }
                            beds[bnum - 1]["last_vital_at"] = (vrow[5].isoformat() + "Z") if vrow[5] else None
                        # SBP trend (oldest → newest, last 6) for the mini sparkline
                        cur.execute("""
                            SELECT sbp FROM (
                                SELECT sbp, recorded_at FROM hd_vital_signs
                                WHERE session_id = %s AND sbp IS NOT NULL
                                ORDER BY recorded_at DESC LIMIT 6
                            ) t ORDER BY recorded_at ASC
                        """, (sid,))
                        if 1 <= bnum <= len(beds):
                            beds[bnum - 1]["sbp_trend"] = [r[0] for r in cur.fetchall()]
                    except Exception:
                        pass

                # UF progress + pre-HD checklist flags (what the nurse decides on)
                for sid, bnum in session_ids:
                    if not (1 <= bnum <= len(beds)):
                        continue
                    b = beds[bnum - 1]
                    try:
                        cur.execute("SELECT uf_goal, pre_weight, post_weight FROM hd_sessions WHERE id = %s", (sid,))
                        srow = cur.fetchone()
                        if srow:
                            b["uf_goal"] = float(srow[0]) if srow[0] is not None else None
                            b["pre_weight_recorded"] = srow[1] is not None
                            b["post_weight_recorded"] = srow[2] is not None
                        cur.execute("""SELECT MAX(uf_removed) FROM hd_uf_records WHERE session_id = %s""", (sid,))
                        urow = cur.fetchone()
                        b["uf_removed"] = float(urow[0]) if urow and urow[0] is not None else None
                        # UF Watch (14 พ.ย. 2569): ตามแผน / ช้า / ยังไม่บันทึก / อัตราเกินเพดาน — แสดงบนการ์ดเตียงและ TV
                        if b.get("status") in ("active", "warning", "critical"):
                            from .uf_watch import for_session as _ufw
                            b["uf_watch"] = _ufw(cur, sid, _uf_watch_limits())
                    except Exception:
                        try:
                            conn.rollback()
                        except Exception:
                            pass

                # ว้าว #1: IDH risk score for active sessions (advisory only)
                try:
                    from .idh_predictor import features_for, score as idh_score
                    for sid, bnum in session_ids:
                        if 1 <= bnum <= len(beds) and beds[bnum - 1].get("status") in ("active", "warning", "critical"):
                            feats = features_for(cur, sid)
                            if feats:
                                beds[bnum - 1]["idh_risk"] = idh_score(feats)
                except Exception as ie:
                    logger.warning("idh predictor: %s", ie)
                    try:
                        conn.rollback()
                    except Exception:
                        pass

                # B6: latest lab per patient on a bed (from hd_lab_results)
                try:
                    from .lab_sync import latest_for_patients
                    cur.execute("SELECT id, patient_id FROM hd_sessions WHERE id = ANY(%s)", ([sid for sid, _ in session_ids],))
                    pid_by_sid = {r[0]: r[1] for r in cur.fetchall()}
                    labs = latest_for_patients(cur, list({p for p in pid_by_sid.values() if p}))
                    for sid, bnum in session_ids:
                        lab = labs.get(pid_by_sid.get(sid))
                        if lab and 1 <= bnum <= len(beds):
                            beds[bnum - 1]["labs"] = lab
                    from .serology import latest_for as _sero_latest
                    sero = _sero_latest(cur, list({p for p in pid_by_sid.values() if p}))
                    for sid, bnum in session_ids:
                        sr = sero.get(pid_by_sid.get(sid))
                        if sr and 1 <= bnum <= len(beds):
                            beds[bnum - 1]["serology"] = sr
                except Exception as le:
                    logger.warning("bed labs: %s", le)
                    try:
                        conn.rollback()
                    except Exception:
                        pass

                # Attach open (unacknowledged) alerts → bed status warning/critical
                for sid, bnum in session_ids:
                    try:
                        cur.execute("""
                            SELECT id, severity::text, title, message, created_at, physician_order
                            FROM hd_clinical_alerts
                            WHERE session_id = %s AND acknowledged = FALSE
                            ORDER BY CASE severity::text WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                                     created_at DESC
                            LIMIT 5
                        """, (sid,))
                        arows = cur.fetchall()
                        if arows and 1 <= bnum <= len(beds):
                            b = beds[bnum - 1]
                            b["alerts"] = [{
                                "id": r[0], "severity": r[1], "title": r[2], "message": r[3],
                                "created_at": (r[4].isoformat() + "Z") if r[4] else None,
                                "physician_order": r[5],
                            } for r in arows]
                            worst = arows[0][1]
                            if b.get("status") == "active" and worst in ("critical", "warning"):
                                b["status"] = worst
                                b["alert_reason"] = arows[0][2]
                    except Exception as ae:
                        logger.warning("bed alerts fetch: %s", ae)

                # Fetch dialyzer info for each patient on a bed
                try:
                    if conn.get_transaction_status() == 3:  # INERROR → เคลียร์ก่อน (GET อ่านอย่างเดียว)
                        conn.rollback()
                except Exception:
                    pass
                for sid, bnum in session_ids:
                    try:
                        patient_id_q = """
                            SELECT patient_id FROM hd_sessions WHERE id = %s
                        """
                        cur.execute(patient_id_q, (sid,))
                        pid_row = cur.fetchone()
                        if pid_row and pid_row[0]:
                            cur.execute("""
                                SELECT dialyzer_type, reuse_count, max_reuse
                                FROM hd_dialyzer_tracking
                                WHERE patient_id = %s AND status = 'active'
                                ORDER BY id DESC LIMIT 1
                            """, (pid_row[0],))
                            drow = cur.fetchone()
                            if drow and 1 <= bnum <= len(beds):
                                beds[bnum - 1]["dialyzer_type"] = drow[0]
                                beds[bnum - 1]["dialyzer_reuse"] = drow[1]
                                beds[bnum - 1]["dialyzer_max"] = drow[2]
                    except Exception:
                        pass

                # Fetch anticoagulation protocol for active sessions
                # 16 พ.ย. 2569: SAVEPOINT ต่อเตียง (เดิม query ก่อนหน้าล้ม → transaction ค้าง → Heparin ไม่ขึ้นทุกเตียง)
                # + สำรองจาก hd_sessions.heparin_dose เมื่อไม่มีแถวใน hd_anticoagulation
                try:
                    if conn.get_transaction_status() == 3:  # INERROR (query ตัวกรองล้ม)
                        conn.rollback()
                except Exception:
                    pass
                for sid, bnum in session_ids:
                    if not (1 <= bnum <= len(beds)):
                        continue
                    try:   # conn เป็น autocommit — ห้ามใช้ SAVEPOINT (ล้มทั้ง loop เมื่อ 16 พ.ย. 2569)
                        cur.execute("""
                            SELECT protocol, loading_dose, maintenance_dose
                            FROM hd_anticoagulation
                            WHERE session_id = %s
                            ORDER BY recorded_at DESC, id DESC LIMIT 1
                        """, (sid,))
                        arow = cur.fetchone()
                        if arow:
                            beds[bnum - 1]["anticoag_drug"] = arow[0]
                            beds[bnum - 1]["anticoag_bolus"] = arow[1]
                            beds[bnum - 1]["anticoag_maintenance"] = arow[2]
                        else:
                            cur.execute("SELECT heparin_dose FROM hd_sessions WHERE id = %s", (sid,))
                            hrow = cur.fetchone()
                            if hrow and hrow[0]:
                                beds[bnum - 1]["anticoag_text"] = hrow[0]
                    except Exception as he:
                        logger.warning("bed heparin fetch: %s", he)

                # Machine-Bed Mapping: เครื่องฟอกที่ผูกแต่ละเตียง
                try:
                    cur.execute("""SELECT id, brand, model, serial_number, bed_number, status
                                   FROM hd_equipment
                                   WHERE equipment_type = 'dialysis_machine' AND bed_number IS NOT NULL""")
                    for mr in cur.fetchall():
                        mb = mr[4]
                        if 1 <= mb <= len(beds):
                            beds[mb - 1]["machine"] = {
                                "id": mr[0], "brand": mr[1], "model": mr[2],
                                "serial": mr[3], "status": mr[5],
                            }
                            if mr[5] in ("repair", "retired"):
                                beds[mb - 1].setdefault("machine_warning", f"เครื่อง {mr[5]}")
                except Exception as me:
                    logger.warning("bed machine map: %s", me)

            conn.close()
        except Exception as e:
            import traceback
            _dash_error = f"DB: {e}"
            logger.error("bed_dashboard DB error: %s\n%s", e, traceback.format_exc())

    # Get today's scheduled patients from DB
    today_count = 0
    today_patients = []
    if db_url:
        try:
            import psycopg2
            day_map = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}
            today_th = day_map.get(target_date.weekday(), "")
            if today_th:
                conn2 = psycopg2.connect(db_url, connect_timeout=5)
                with conn2.cursor() as cur:
                    cur.execute("""
                        SELECT hn, patient_name, vascular_access, schedule_days
                        FROM hd_patient_mapping
                        WHERE is_active = TRUE AND schedule_days ILIKE %s
                        ORDER BY hn
                    """, (f"%{today_th}%",))
                    for row in cur.fetchall():
                        today_patients.append({
                            "hn": row[0], "name": row[1],
                            "va": row[2], "schedule": row[3],
                        })
                    # 🙅 ป้ายขาดฟอกบ่อย: no-show 90 วันย้อนหลังต่อคน (additive — ล้มได้ไม่กระทบ)
                    try:
                        cur.execute("""
                            SELECT p.hn, COUNT(*) FROM hd_sessions s
                            JOIN hd_patient_mapping p ON p.id = s.patient_id
                            WHERE s.status = 'cancelled' AND s.cancel_reason LIKE %s
                              AND s.session_date >= CURRENT_DATE - 90
                            GROUP BY p.hn
                        """, ('%ไม่มา%',))
                        _ns = dict(cur.fetchall())
                        for tp in today_patients:
                            if _ns.get(tp["hn"]):
                                tp["noshow_90d"] = _ns[tp["hn"]]
                    except Exception as _ns_e:
                        logger.warning("noshow badge query: %s", _ns_e)
                conn2.close()
                today_count = len(today_patients)
        except Exception as e:
            logger.warning("today_patients query error: %s", e)

    # Fallback: count from master registry if DB had no results
    if not today_patients and current_app.config.get("MASTER_REGISTRY"):
        try:
            day_map = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}
            today_th = day_map.get(target_date.weekday(), "")
            reg = current_app.config["MASTER_REGISTRY"]
            for p in reg.list_all():
                sched = getattr(p, "dialysis_schedule", "") or ""
                if today_th and today_th in sched:
                    today_count += 1
        except Exception:
            pass

    active_count = sum(1 for b in beds if b.get("status") not in ("available", None))

    # DEBUG: Also count total sessions today (all shifts) to confirm DB has data
    debug_total = 0
    debug_detail = ''
    if db_url:
        try:
            import psycopg2
            dconn = psycopg2.connect(db_url, connect_timeout=5)
            with dconn.cursor() as dcur:
                dcur.execute("""
                    SELECT COUNT(*), string_agg(
                        COALESCE(shift,'NULL') || ':' || status || ':bed' || COALESCE(bed_number::text,'?'), ', '
                    )
                    FROM hd_sessions WHERE session_date = %s
                """, (target_date.isoformat(),))
                drow = dcur.fetchone()
                debug_total = drow[0] if drow else 0
                debug_detail = drow[1] if drow and drow[1] else ''
            dconn.close()
        except Exception as de:
            debug_detail = 'db_error: ' + str(de)

    return jsonify({
        "beds": beds,
        "total": HdConfig.TOTAL_BEDS,
        "shift": current_shift,
        "date": target_date.isoformat(),
        "active_in_shift": active_count,
        "completed_sessions": completed_sessions,
        "cancelled_sessions": cancelled_sessions,
        "completed_in_shift": len(completed_sessions),
        "today_scheduled": today_count,
        "today_patients": today_patients,
        "_debug_sessions_today": debug_total,
        "_debug_detail": debug_detail,
        "_debug_error": _dash_error,
    })



# ── AI UF Coach — Real-time Recommendation ───────────────

@hd_bp.route("/sessions/<int:session_id>/uf-coach", methods=["GET"])
@require_role("physician", "nurse", "admin")
def session_uf_coach(session_id):
    """AI UF Coach: วิเคราะห์ UF + BP trend → แนะนำปรับ UF rate.

    ⚠️ ข้อเสนอแนะเท่านั้น — ไม่ปรับเครื่องอัตโนมัติ แพทย์ตัดสินใจ
    """
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Session info
            cur.execute("""SELECT s.patient_id, s.uf_goal, s.started_at, s.pre_weight, s.actual_uf,
                                  p.dry_weight, p.hn, p.patient_name
                           FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                           WHERE s.id = %s""", (session_id,))
            s = cur.fetchone()
            if not s:
                conn.close()
                return jsonify({"error": "Session not found"}), 404
            pid, uf_goal, started, pre_w, actual_uf, dry_w, hn, name = s

            # Latest vitals (last 3 readings)
            cur.execute("""SELECT sbp, dbp, heart_rate, recorded_at
                           FROM hd_vital_signs WHERE session_id = %s AND sbp > 0
                           ORDER BY recorded_at DESC LIMIT 5""", (session_id,))
            vitals = [{"sbp": r[0], "dbp": r[1], "hr": r[2],
                       "time": r[3].strftime("%H:%M") if r[3] else ""} for r in cur.fetchall()]
            vitals.reverse()  # oldest first

            # Latest flowsheet UF
            cur.execute("""SELECT bfr, ufr, total_uf, slot_time
                           FROM hd_flowsheet WHERE session_id = %s
                           ORDER BY recorded_at DESC LIMIT 1""", (session_id,))
            flow = cur.fetchone()
            current_bfr = flow[0] if flow else None
            current_ufr = flow[1] if flow else None
            current_total_uf = flow[2] if flow else actual_uf

            # UF watch evaluation
            try:
                from .uf_watch import for_session
                uf_eval = for_session(cur, session_id)
            except Exception:
                uf_eval = None

            # IDH predictor
            try:
                from .idh_predictor import predict_idh_risk
                idh = predict_idh_risk(cur, session_id)
            except Exception:
                idh = None

            # Personal UF tolerance
            tol = None
            try:
                from .uf_coach import sessions_for_tolerance, tolerance_from_sessions
                _ss = sessions_for_tolerance(cur, hn)
                if _ss:
                    tol = tolerance_from_sessions(_ss)
            except Exception:
                pass

        conn.close()

        # === AI Analysis (rule-based, no external AI) ===
        recommendations = []
        risk_level = "normal"  # normal, caution, warning, danger

        # 1. BP trend analysis
        if len(vitals) >= 2:
            latest_sbp = vitals[-1]["sbp"]
            prev_sbp = vitals[-2]["sbp"]
            sbp_drop = prev_sbp - latest_sbp

            if latest_sbp < 90:
                risk_level = "danger"
                recommendations.append({
                    "icon": "🔴", "priority": 1,
                    "action": "ลด UF rate ทันที หรือหยุด UF ชั่วคราว",
                    "reason": f"SBP ต่ำมาก ({latest_sbp} mmHg) — เสี่ยง IDH",
                })
            elif latest_sbp < 100 or sbp_drop > 30:
                risk_level = "warning"
                recommendations.append({
                    "icon": "🟠", "priority": 2,
                    "action": "ลด UF rate 200-300 mL/hr",
                    "reason": f"SBP ลดลง {sbp_drop} mmHg ({prev_sbp}→{latest_sbp})",
                })
            elif sbp_drop > 20:
                risk_level = "caution"
                recommendations.append({
                    "icon": "🟡", "priority": 3,
                    "action": "เฝ้าระวัง — วัด BP ถี่ขึ้น ทุก 15 นาที",
                    "reason": f"SBP ลดลง {sbp_drop} mmHg — แนวโน้มลดต่อ",
                })

        # 2. UF pace analysis
        if uf_eval and isinstance(uf_eval, dict):
            pace = uf_eval.get("pace")
            if pace == "behind":
                recommendations.append({
                    "icon": "⏱", "priority": 4,
                    "action": "เพิ่ม UF rate เล็กน้อย (ถ้า BP ยังดี)",
                    "reason": "UF ช้ากว่ากำหนด — อาจดึงน้ำไม่ครบ",
                })
            elif pace == "ahead":
                recommendations.append({
                    "icon": "💧", "priority": 4,
                    "action": "ลด UF rate ลงเล็กน้อย",
                    "reason": "UF เร็วกว่ากำหนด — เสี่ยง IDH",
                })

        # 3. IDH risk
        if idh and isinstance(idh, dict) and idh.get("score", 0) >= 60:
            if risk_level == "normal":
                risk_level = "caution"
            recommendations.append({
                "icon": "🩸", "priority": 2,
                "action": "เตรียมพร้อม — เสี่ยงความดันตก",
                "reason": f"IDH score {idh.get('score')}/100 ({idh.get('label', '')})",
            })

        # 4. Personal tolerance
        if tol and isinstance(tol, dict) and tol.get("ceiling") and current_ufr:
            try:
                if float(current_ufr) > float(tol["ceiling"]) * 1000:
                    recommendations.append({
                        "icon": "⚡", "priority": 2,
                        "action": f"UFR ปัจจุบันเกินเพดานส่วนตัว ({tol['ceiling']} L/hr)",
                        "reason": "คนไข้คนนี้เคยความดันตกเมื่อ UFR สูงกว่านี้",
                    })
            except (ValueError, TypeError):
                pass

        # Default: all good
        if not recommendations:
            recommendations.append({
                "icon": "✅", "priority": 10,
                "action": "ทุกอย่างปกติ — ดำเนินการตามปกติ",
                "reason": "BP คงที่, UF ตามเป้า",
            })

        recommendations.sort(key=lambda r: r["priority"])

        return jsonify({
            "session_id": session_id,
            "patient": {"hn": hn, "name": name},
            "risk_level": risk_level,
            "recommendations": recommendations,
            "vitals_summary": {
                "latest_sbp": vitals[-1]["sbp"] if vitals else None,
                "trend": "dropping" if len(vitals) >= 2 and vitals[-1]["sbp"] < vitals[-2]["sbp"] else "stable",
                "readings": len(vitals),
            },
            "uf_summary": {
                "goal": uf_goal,
                "removed": current_total_uf,
                "current_ufr": current_ufr,
                "pace": uf_eval.get("pace") if isinstance(uf_eval, dict) else None,
            },
            "idh_score": idh.get("score") if isinstance(idh, dict) else None,
            "tolerance": tol,
            "disclaimer": "⚠️ ข้อเสนอแนะจาก AI เท่านั้น — แพทย์ตัดสินใจ ไม่ปรับเครื่องอัตโนมัติ",
        })
    except Exception as e:
        logger.error("uf-coach error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Upgrade #3: AI Predictive Alert — เตือนก่อนเกิด ───────

@hd_bp.route("/sessions/<int:session_id>/predict-alert", methods=["GET"])
@require_role("physician", "nurse", "admin")
def predict_alert(session_id):
    """AI Predictive Alert — ทำนาย BP drop ก่อนเกิด 15-30 นาที.

    วิเคราะห์ BP trend ของ session ปัจจุบัน + pattern จาก Digital Twin
    → แจ้งเตือน BEFORE the event, not after.
    """
    import os, psycopg2
    from datetime import timedelta
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        predictions = []
        with conn.cursor() as cur:
            cur.execute("""SELECT s.patient_id, s.started_at, s.uf_goal, s.pre_weight,
                                  p.dry_weight, p.hn
                           FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                           WHERE s.id = %s""", (session_id,))
            s = cur.fetchone()
            if not s:
                conn.close()
                return jsonify({"predictions": [], "risk": "unknown"})
            pid, started, uf_goal, pre_w, dw, hn = s

            # Current session vitals (chronological)
            cur.execute("""SELECT sbp, dbp, heart_rate, recorded_at
                           FROM hd_vital_signs WHERE session_id = %s AND sbp > 0
                           ORDER BY recorded_at ASC""", (session_id,))
            vitals = cur.fetchall()

            if len(vitals) < 2:
                conn.close()
                return jsonify({"predictions": [{"type": "info", "icon": "ℹ️",
                    "message": "ต้องมี vital signs อย่างน้อย 2 ครั้งเพื่อทำนาย"}], "risk": "insufficient_data"})

            now = _thai_now()
            elapsed_min = (now.replace(tzinfo=None) - started.replace(tzinfo=None)).total_seconds() / 60 if started else 0

            # 1. LINEAR TREND: extrapolate SBP 15-30 min ahead
            sbps = [(v[3], v[0]) for v in vitals]
            recent = sbps[-3:]  # last 3 readings
            if len(recent) >= 2:
                # Simple linear regression on last readings
                times = [(r[0] - recent[0][0]).total_seconds() / 60 for r in recent]
                vals = [r[1] for r in recent]
                n = len(times)
                if n >= 2 and (max(times) - min(times)) > 0:
                    x_mean = sum(times) / n
                    y_mean = sum(vals) / n
                    num = sum((times[i] - x_mean) * (vals[i] - y_mean) for i in range(n))
                    den = sum((times[i] - x_mean) ** 2 for i in range(n))
                    if den > 0:
                        slope = num / den  # mmHg per minute
                        current_sbp = vals[-1]

                        # Predict 15 and 30 min ahead
                        pred_15 = round(current_sbp + slope * 15)
                        pred_30 = round(current_sbp + slope * 30)

                        if pred_15 < 90:
                            predictions.append({"type": "critical", "icon": "🔴",
                                "time": "15 นาที", "predicted_sbp": pred_15,
                                "message": f"⚠️ คาดว่า SBP จะลงถึง {pred_15} mmHg ใน 15 นาที",
                                "action": "ลด UF rate ทันที / เตรียม NSS / วัด BP ทุก 5 นาที",
                                "slope": round(slope, 2)})
                        elif pred_15 < 100:
                            predictions.append({"type": "warning", "icon": "🟠",
                                "time": "15 นาที", "predicted_sbp": pred_15,
                                "message": f"แนวโน้ม SBP จะลงถึง {pred_15} mmHg ใน 15 นาที",
                                "action": "เฝ้าระวังใกล้ชิด วัด BP ทุก 10 นาที",
                                "slope": round(slope, 2)})

                        if pred_30 < 90 and pred_15 >= 90:
                            predictions.append({"type": "warning", "icon": "🟡",
                                "time": "30 นาที", "predicted_sbp": pred_30,
                                "message": f"แนวโน้ม SBP จะลงถึง {pred_30} mmHg ใน 30 นาที",
                                "action": "พิจารณาลด UF rate ล่วงหน้า",
                                "slope": round(slope, 2)})

            # 2. PATTERN MATCH: Digital Twin — ชั่วโมงนี้คนไข้คนนี้เคย BP ตกไหม
            if started and elapsed_min > 30:
                cur.execute("""SELECT s.id FROM hd_sessions s
                               WHERE s.patient_id = %s AND s.status = 'completed' AND s.id != %s
                               ORDER BY s.session_date DESC LIMIT 10""", (pid, session_id))
                past_ids = [r[0] for r in cur.fetchall()]
                drop_at_this_hour = 0
                total_past = len(past_ids)
                current_hour = elapsed_min / 60

                for psid in past_ids:
                    cur.execute("""SELECT sbp, recorded_at FROM hd_vital_signs
                                   WHERE session_id = %s AND sbp > 0 ORDER BY recorded_at""", (psid,))
                    pvitals = cur.fetchall()
                    if len(pvitals) < 3:
                        continue
                    # Find if BP dropped below 100 at similar elapsed time
                    cur.execute("SELECT started_at FROM hd_sessions WHERE id = %s", (psid,))
                    ps = cur.fetchone()
                    if not ps or not ps[0]:
                        continue
                    for pv in pvitals:
                        pv_elapsed = (pv[1] - ps[0]).total_seconds() / 3600
                        if abs(pv_elapsed - current_hour) < 0.5 and pv[0] < 100:
                            drop_at_this_hour += 1
                            break

                if total_past >= 3 and drop_at_this_hour > 0:
                    pct = round(drop_at_this_hour / total_past * 100)
                    if pct >= 50:
                        predictions.append({"type": "pattern", "icon": "🧬",
                            "message": f"Digital Twin: คนไข้คนนี้เคย BP ตก ณ ชั่วโมงที่ {current_hour:.1f} ใน {pct}% ของรอบที่ผ่านมา ({drop_at_this_hour}/{total_past})",
                            "action": "เตรียมพร้อม — pattern ซ้ำจากประวัติ",
                            "confidence": pct})

            # 3. HR TREND: ถ้า HR เพิ่มขึ้นเร็ว → compensatory → BP กำลังจะตก
            hrs = [v[2] for v in vitals if v[2] and v[2] > 0]
            if len(hrs) >= 3:
                hr_rise = hrs[-1] - hrs[-3]
                if hr_rise > 15:
                    predictions.append({"type": "warning", "icon": "💓",
                        "message": f"HR เพิ่มขึ้น {hr_rise} bpm ใน 3 readings ({hrs[-3]}→{hrs[-1]}) — ร่างกายชดเชยความดันตก",
                        "action": "ตรวจ BP ทันที — HR สูงขึ้นมักนำหน้า BP drop"})

        conn.close()

        if not predictions:
            predictions.append({"type": "safe", "icon": "✅",
                "message": "ไม่พบสัญญาณเตือน — BP trend คงที่"})

        risk = "critical" if any(p["type"] == "critical" for p in predictions) else \
               "warning" if any(p["type"] in ("warning", "pattern") for p in predictions) else "safe"

        return jsonify({
            "session_id": session_id,
            "predictions": predictions,
            "risk": risk,
            "analyzed_at": now.strftime("%H:%M:%S"),
            "vitals_count": len(vitals),
            "elapsed_min": round(elapsed_min),
        })
    except Exception as e:
        logger.error("predict-alert error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── AI Dialysis Twin — ฝาแฝดดิจิทัล ─────────────────────

@hd_bp.route("/patients/<hn>/twin", methods=["GET"])
@require_role("physician", "nurse", "admin")
def patient_dialysis_twin(hn):
    """AI Dialysis Twin — ทำนายจาก pattern ย้อนหลังของคนไข้คนนี้.

    วิเคราะห์:
    1. BP drop pattern — ชั่วโมงที่กี่มักตก + ปัจจัยเสี่ยง
    2. IDWG pattern — น้ำหนักมักขึ้นเท่าไร
    3. Best UF rate — อัตราที่ไม่เคย IDH
    4. Optimal duration — เวลาที่ Kt/V ถึงเป้า
    5. Risk prediction สำหรับรอบถัดไป
    """
    import os, psycopg2
    from datetime import timedelta
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT id, patient_name, dry_weight, vascular_access FROM hd_patient_mapping WHERE hn = %s", (hn,))
            p = cur.fetchone()
            if not p:
                conn.close()
                return jsonify({"error": "ไม่พบผู้ป่วย"}), 404
            pid, name, dw, va = p

            # Collect last 20 completed sessions
            cur.execute("""SELECT s.id, s.session_date, s.pre_weight, s.post_weight,
                                  s.actual_uf, s.started_at, s.ended_at, s.kt_v
                           FROM hd_sessions s WHERE s.patient_id = %s AND s.status = 'completed'
                           ORDER BY s.session_date DESC, s.id DESC LIMIT 20""", (pid,))
            sessions = cur.fetchall()
            n = len(sessions)

            twin = {
                "hn": hn, "name": name, "sessions_analyzed": n,
                "predictions": [], "insights": [], "risk_score": 0,
            }

            if n < 3:
                twin["message"] = f"ต้องมีข้อมูลอย่างน้อย 3 รอบ (มี {n} รอบ) เพื่อสร้าง Digital Twin"
                conn.close()
                return jsonify(twin)

            # 1. IDWG Pattern
            idwg_list = []
            for s in sessions:
                if s[2] and dw and dw > 0:
                    idwg_kg = float(s[2]) - float(dw)
                    idwg_pct = idwg_kg / float(dw) * 100
                    idwg_list.append({"kg": round(idwg_kg, 1), "pct": round(idwg_pct, 1)})
            if idwg_list:
                avg_idwg = round(sum(x["kg"] for x in idwg_list) / len(idwg_list), 1)
                max_idwg = max(x["kg"] for x in idwg_list)
                twin["idwg_pattern"] = {
                    "avg_kg": avg_idwg, "max_kg": max_idwg,
                    "avg_pct": round(sum(x["pct"] for x in idwg_list) / len(idwg_list), 1),
                    "over_5pct_count": sum(1 for x in idwg_list if x["pct"] > 5),
                }
                if avg_idwg > 3:
                    twin["insights"].append({"icon": "⚖️", "text": f"น้ำหนักมักขึ้นเฉลี่ย {avg_idwg} kg — แนะนำควบคุมน้ำ/เกลือ"})
                twin["predictions"].append({"icon": "⚖️", "label": "น้ำหนักรอบหน้า",
                    "value": f"~{float(dw) + avg_idwg:.1f} kg (ขึ้น ~{avg_idwg} kg)", "confidence": min(n*5, 85)})

            # 2. BP Drop Pattern
            bp_drops = []
            idh_sessions = 0
            for s in sessions:
                cur.execute("""SELECT sbp, dbp, recorded_at FROM hd_vital_signs
                               WHERE session_id = %s AND sbp > 0 ORDER BY recorded_at""", (s[0],))
                vitals = cur.fetchall()
                if len(vitals) >= 2:
                    first_sbp = vitals[0][0]
                    min_sbp = min(v[0] for v in vitals)
                    drop = first_sbp - min_sbp
                    if drop > 20:
                        # Find which hour the drop happened
                        start = s[5]
                        min_v = min(vitals, key=lambda v: v[0])
                        if start and min_v[2]:
                            hour = (min_v[2] - start).total_seconds() / 3600
                            bp_drops.append({"drop": drop, "hour": round(hour, 1), "nadir": min_sbp})
                    if min_sbp < 90:
                        idh_sessions += 1

            if bp_drops:
                avg_hour = round(sum(d["hour"] for d in bp_drops) / len(bp_drops), 1)
                avg_drop = round(sum(d["drop"] for d in bp_drops) / len(bp_drops))
                twin["bp_pattern"] = {
                    "drops_in_sessions": len(bp_drops), "total_sessions": n,
                    "avg_drop_hour": avg_hour, "avg_drop_mmhg": avg_drop,
                    "idh_sessions": idh_sessions, "idh_rate": round(idh_sessions/n*100, 1),
                }
                twin["predictions"].append({"icon": "💓", "label": "BP มักตก",
                    "value": f"ชั่วโมงที่ {avg_hour} (ลง ~{avg_drop} mmHg)", "confidence": min(len(bp_drops)*10, 80)})
                if idh_sessions > 0:
                    twin["insights"].append({"icon": "🔴",
                        "text": f"IDH เกิด {idh_sessions}/{n} รอบ ({round(idh_sessions/n*100)}%) — ระวังชั่วโมงที่ {avg_hour}"})

            # 3. Best UF rate (rate ที่ไม่เคย IDH)
            uf_rates = []
            for s in sessions:
                if s[4] and s[5] and s[6]:
                    dur = (s[6] - s[5]).total_seconds() / 3600
                    if dur > 0:
                        rate = float(s[4]) / dur / 1000  # L/hr
                        cur.execute("SELECT MIN(sbp) FROM hd_vital_signs WHERE session_id = %s AND sbp > 0", (s[0],))
                        min_sbp = (cur.fetchone() or [None])[0]
                        uf_rates.append({"rate": round(rate, 1), "min_sbp": min_sbp, "idh": min_sbp and min_sbp < 90})
            safe_rates = [r["rate"] for r in uf_rates if not r["idh"]]
            if safe_rates:
                max_safe = max(safe_rates)
                avg_safe = round(sum(safe_rates) / len(safe_rates), 1)
                twin["uf_pattern"] = {"max_safe_rate": max_safe, "avg_safe_rate": avg_safe}
                twin["predictions"].append({"icon": "💧", "label": "UF rate ปลอดภัย",
                    "value": f"≤{max_safe} L/hr (เฉลี่ย {avg_safe})", "confidence": min(len(safe_rates)*8, 85)})

            # 4. Kt/V pattern
            ktvs = [float(s[7]) for s in sessions if s[7]]
            if ktvs:
                avg_ktv = round(sum(ktvs) / len(ktvs), 2)
                twin["ktv_pattern"] = {"avg": avg_ktv, "min": round(min(ktvs), 2), "max": round(max(ktvs), 2)}
                if avg_ktv < 1.2:
                    twin["insights"].append({"icon": "🧮", "text": f"Kt/V เฉลี่ย {avg_ktv} (ต่ำกว่าเป้า 1.2) — พิจารณาเพิ่มเวลาฟอกหรือ BFR"})

            # 5. Risk score (0-100)
            risk = 0
            if twin.get("bp_pattern", {}).get("idh_rate", 0) > 20:
                risk += 30
            elif twin.get("bp_pattern", {}).get("idh_rate", 0) > 10:
                risk += 15
            if twin.get("idwg_pattern", {}).get("avg_pct", 0) > 5:
                risk += 20
            elif twin.get("idwg_pattern", {}).get("avg_pct", 0) > 3:
                risk += 10
            if ktvs and sum(ktvs)/len(ktvs) < 1.0:
                risk += 25
            elif ktvs and sum(ktvs)/len(ktvs) < 1.2:
                risk += 10
            twin["risk_score"] = min(risk, 100)
            twin["risk_level"] = "high" if risk >= 50 else ("moderate" if risk >= 25 else "low")

            if not twin["insights"]:
                twin["insights"].append({"icon": "✅", "text": "ผู้ป่วยรายนี้ฟอกสม่ำเสมอ ไม่มีปัญหาที่ต้องระวังเป็นพิเศษ"})

        conn.close()
        return jsonify(twin)
    except Exception as e:
        logger.error("twin error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── AI Assistant (Chat + LINE) ─────────────────────────────

@hd_bp.route("/ai/ask", methods=["POST"])
@require_role("physician", "nurse", "admin")
def ai_assistant_ask():
    """AI Assistant — ถามอะไรก็ตอบจาก DB (rule-based, no external AI).

    รองรับ:
    - สรุปคนไข้ HN/ชื่อ
    - ใครKt/Vไม่ถึง / Hbต่ำ / ขาดนัด
    - เตียงว่าง / วันนี้ / พรุ่งนี้
    - เปรียบเทียบ / สถิติ
    """
    import os, psycopg2, re
    from .group_assistant import answer as ga_answer, match as ga_match
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "กรุณาพิมพ์คำถาม"}), 400

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503

    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        # Try group_assistant first (handles: เตียง, วันนี้, alert, esa, น้ำ, HN/name)
        reply = ga_answer(conn, question)

        # Extended queries not in group_assistant
        low = question.lower()
        extended = None

        if any(w in low for w in ("kt/v ไม่ถึง", "ktv ต่ำ", "adequacy", "ใครยัง")):
            with conn.cursor() as cur:
                cur.execute("""SELECT p.patient_name, p.hn, s.kt_v, s.session_date
                               FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                               WHERE s.status = 'completed' AND s.kt_v IS NOT NULL AND s.kt_v < 1.2
                               AND s.session_date >= CURRENT_DATE - INTERVAL '30 days'
                               ORDER BY s.kt_v ASC LIMIT 15""")
                rows = cur.fetchall()
            if rows:
                extended = f"🧮 Kt/V < 1.2 (30 วัน) — {len(rows)} คน:\n" + \
                    "\n".join(f"• {r[0]} (HN {r[1]}) Kt/V={float(r[2]):.2f} ({r[3].strftime('%d/%m')})" for r in rows)
            else:
                extended = "✅ ทุกคน Kt/V ≥ 1.2 ใน 30 วันที่ผ่านมา"

        elif any(w in low for w in ("hb ต่ำ", "ซีด", "anemia", "โลหิตจาง")):
            with conn.cursor() as cur:
                cur.execute("""SELECT p.patient_name, p.hn, l.hb, l.lab_date
                               FROM hd_labs l JOIN hd_patient_mapping p ON p.id = l.patient_id
                               WHERE l.hb IS NOT NULL AND l.hb < 10
                               AND l.lab_date >= CURRENT_DATE - INTERVAL '60 days'
                               ORDER BY l.hb ASC LIMIT 15""")
                rows = cur.fetchall()
            if rows:
                extended = f"🩸 Hb < 10 (60 วัน) — {len(rows)} คน:\n" + \
                    "\n".join(f"• {r[0]} (HN {r[1]}) Hb={float(r[2]):.1f} ({r[3].strftime('%d/%m')})" for r in rows)
            else:
                extended = "✅ ทุกคน Hb ≥ 10 ใน 60 วันที่ผ่านมา"

        elif any(w in low for w in ("ขาดนัด", "ไม่มาฟอก", "no show", "miss")):
            with conn.cursor() as cur:
                cur.execute("""SELECT p.patient_name, p.hn, s.session_date
                               FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                               WHERE s.status = 'cancelled'
                               AND s.session_date >= CURRENT_DATE - INTERVAL '14 days'
                               ORDER BY s.session_date DESC LIMIT 15""")
                rows = cur.fetchall()
            if rows:
                extended = f"❌ ขาดนัด/ยกเลิก (14 วัน) — {len(rows)} รอบ:\n" + \
                    "\n".join(f"• {r[0]} (HN {r[1]}) วันที่ {r[2].strftime('%d/%m')}" for r in rows)
            else:
                extended = "✅ ไม่มีผู้ขาดนัดใน 14 วัน"

        elif any(w in low for w in ("สรุป", "สถิติ", "ภาพรวม", "overview", "summary")):
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM hd_patient_mapping WHERE is_active = TRUE")
                total = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM hd_sessions WHERE session_date = CURRENT_DATE")
                today_sessions = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM hd_sessions WHERE status = 'in_progress'")
                active = cur.fetchone()[0]
                cur.execute("""SELECT COUNT(*) FROM hd_clinical_alerts a
                               JOIN hd_sessions s ON s.id = a.session_id
                               WHERE a.acknowledged = FALSE AND s.session_date >= CURRENT_DATE - INTERVAL '1 day'""")
                alerts = cur.fetchone()[0]
            extended = f"📊 สรุประบบ HD\n• ผู้ป่วยทั้งหมด: {total} ราย\n• รอบวันนี้: {today_sessions} รอบ\n• กำลังฟอก: {active} เตียง\n• Alert รอตอบรับ: {alerts} รายการ"

        elif any(w in low for w in ("น้ำหนักเกิน", "idwg เกิน", "น้ำหนักขึ้นเกิน")):
            with conn.cursor() as cur:
                cur.execute("""SELECT p.patient_name, p.hn,
                                      s.pre_weight, p.dry_weight,
                                      ROUND(((s.pre_weight - p.dry_weight) / p.dry_weight * 100)::numeric, 1) as pct
                               FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                               WHERE s.status = 'completed' AND s.pre_weight IS NOT NULL AND p.dry_weight > 0
                                 AND s.session_date >= CURRENT_DATE - INTERVAL '7 days'
                                 AND ((s.pre_weight - p.dry_weight) / p.dry_weight * 100) > 5
                               ORDER BY pct DESC LIMIT 15""")
                rows = cur.fetchall()
            if rows:
                extended = f"⚖️ IDWG > 5% (7 วัน) — {len(rows)} คน:\n" + \
                    "\n".join(f"• {r[0]} (HN {r[1]}) {float(r[2]):.1f}/{float(r[3]):.1f} kg (+{r[4]}%)" for r in rows)
            else:
                extended = "✅ ไม่มีผู้ป่วย IDWG > 5% ใน 7 วัน"

        conn.close()

        if extended:
            reply = extended

        log_action(user_id=g.get("hd_user_id"), action="ai.ask", resource_type="ai_assistant",
                   new_values={"q": question[:100]}, ip_address=request.remote_addr or "")

        return jsonify({"question": question, "answer": reply, "source": "rule-based (no external AI)"})
    except Exception as e:
        logger.error("ai ask error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── ทีเด็ด #1: AI Clinical Narrator — เล่าเรื่องคนไข้ ──

@hd_bp.route("/patients/<hn>/narrator", methods=["GET"])
@require_role("physician", "nurse", "admin")
def patient_narrator(hn):
    """AI Clinical Narrator — สรุปสถานะคนไข้เป็น 'เรื่องเล่า' ภาษาไทย.

    ไม่ใช่แค่ตัวเลข — เขียนเป็นข้อความที่หมออ่านแล้วเข้าใจทันที 1 นาที
    เหมือนมีแพทย์ประจำบ้านสรุปให้ทุกเช้า

    Rule-based narrative generation (no external AI).
    """
    import os, psycopg2
    from datetime import timedelta
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        today = _thai_today()
        with conn.cursor() as cur:
            cur.execute("""SELECT id, patient_name, hn, dry_weight, vascular_access, schedule_days,
                                  esa_drug, esa_dose, date_of_birth, gender, allergies
                           FROM hd_patient_mapping WHERE hn = %s""", (hn,))
            p = cur.fetchone()
            if not p:
                conn.close()
                return jsonify({"error": "ไม่พบผู้ป่วย"}), 404
            pid, name, _, dw, va, sched, esa_drug, esa_dose, dob, gender, allergies = p
            age = None
            if dob:
                age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

            # Collect data
            # Last 5 sessions
            cur.execute("""SELECT session_date, pre_weight, post_weight, actual_uf, kt_v,
                                  started_at, ended_at, hb_value
                           FROM hd_sessions WHERE patient_id = %s AND status = 'completed'
                           ORDER BY session_date DESC, id DESC LIMIT 5""", (pid,))
            sessions = cur.fetchall()

            # Latest labs
            cur.execute("""SELECT hb, hct, bun, creatinine, k, albumin, lab_date
                           FROM hd_labs WHERE patient_id = %s
                           ORDER BY lab_date DESC LIMIT 1""", (pid,))
            lab = cur.fetchone()

            # Unack alerts
            cur.execute("""SELECT COUNT(*) FROM hd_clinical_alerts a
                           JOIN hd_sessions s ON s.id = a.session_id
                           WHERE s.patient_id = %s AND a.acknowledged = FALSE""", (pid,))
            alert_count = cur.fetchone()[0]

            # ESA proposals pending
            cur.execute("""SELECT action, proposed_dose FROM hd_esa_proposals
                           WHERE patient_id = %s AND status = 'pending' LIMIT 1""", (pid,))
            esa_pending = cur.fetchone()

            # BP trend (last 5 sessions pre-HD)
            bp_list = []
            for s in sessions[:5]:
                cur.execute("""SELECT sbp, dbp FROM hd_vital_signs
                               WHERE session_id IN (SELECT id FROM hd_sessions WHERE patient_id = %s AND session_date = %s LIMIT 1)
                               AND phase = 'pre' AND sbp > 0 ORDER BY recorded_at LIMIT 1""",
                            (pid, s[0].isoformat() if s[0] else today.isoformat()))
                bp = cur.fetchone()
                if bp:
                    bp_list.append({"sbp": bp[0], "dbp": bp[1]})

        conn.close()

        # === Generate Narrative ===
        paras = []
        emoji_risk = "🟢"

        # Opening
        gender_th = "ผู้ป่วยหญิง" if gender == "F" else "ผู้ป่วยชาย" if gender == "M" else "ผู้ป่วย"
        opening = f"{gender_th} {name}"
        if age:
            opening += f" อายุ {age} ปี"
        if va:
            opening += f" เส้นฟอก {va}"
        if sched:
            opening += f" ฟอกวัน {sched}"
        paras.append(opening)

        # Sessions summary
        if sessions:
            n = len(sessions)
            last_date = sessions[0][0]
            days_ago = (today - last_date).days if last_date else None
            if days_ago is not None:
                if days_ago == 0:
                    paras.append(f"ฟอกครั้งล่าสุด: วันนี้")
                elif days_ago == 1:
                    paras.append(f"ฟอกครั้งล่าสุด: เมื่อวาน")
                elif days_ago > 7:
                    paras.append(f"⚠️ ฟอกครั้งล่าสุด {days_ago} วันก่อน — ห่างเกินไป")
                    emoji_risk = "🟡"
                else:
                    paras.append(f"ฟอกครั้งล่าสุด: {days_ago} วันก่อน")

            # Weight & UF
            weights = [float(s[1]) for s in sessions if s[1]]
            post_weights = [float(s[2]) for s in sessions if s[2]]
            if weights and dw:
                avg_pre = round(sum(weights)/len(weights), 1)
                avg_idwg = round(avg_pre - float(dw), 1)
                idwg_pct = round(avg_idwg / float(dw) * 100, 1)
                txt = f"น้ำหนักก่อนฟอกเฉลี่ย {avg_pre} kg (DW {dw} kg, IDWG เฉลี่ย {avg_idwg} kg = {idwg_pct}%)"
                if idwg_pct > 5:
                    txt += " — ⚠️ เกิน 5% ควรแนะนำควบคุมน้ำ/เกลือ"
                    emoji_risk = "🟡"
                elif idwg_pct > 3:
                    txt += " — อยู่ในเกณฑ์ที่ต้องเฝ้าระวัง"
                else:
                    txt += " — ✅ อยู่ในเกณฑ์ดี"
                paras.append(txt)

            # Kt/V
            ktvs = [float(s[4]) for s in sessions if s[4]]
            if ktvs:
                avg_ktv = round(sum(ktvs)/len(ktvs), 2)
                if avg_ktv >= 1.2:
                    paras.append(f"Kt/V เฉลี่ย {avg_ktv} — ✅ ประสิทธิภาพการฟอกเพียงพอ (เป้า ≥1.2)")
                else:
                    paras.append(f"Kt/V เฉลี่ย {avg_ktv} — ❌ ไม่ถึงเป้า 1.2 ควรพิจารณาเพิ่ม BFR หรือเวลาฟอก")
                    emoji_risk = "🔴"
        else:
            paras.append("ยังไม่มีประวัติฟอกในระบบ")

        # Lab interpretation
        if lab:
            hb, hct, bun, cr, k, alb, lab_date = lab
            lab_parts = []
            if hb is not None:
                hb = float(hb)
                if hb < 10:
                    lab_parts.append(f"Hb {hb:.1f} ⬇️ (ต่ำกว่าเป้า 10-12)")
                    emoji_risk = "🟡" if emoji_risk == "🟢" else emoji_risk
                elif hb > 13:
                    lab_parts.append(f"Hb {hb:.1f} ⬆️ (สูงเกิน — พิจารณาลด/งด ESA)")
                else:
                    lab_parts.append(f"Hb {hb:.1f} ✅")
            if k is not None:
                k = float(k)
                if k > 5.5:
                    lab_parts.append(f"K+ {k:.1f} ⬆️ (สูง — ระวังหัวใจเต้นผิดจังหวะ)")
                    emoji_risk = "🔴"
                elif k < 3.5:
                    lab_parts.append(f"K+ {k:.1f} ⬇️ (ต่ำ)")
                else:
                    lab_parts.append(f"K+ {k:.1f} ✅")
            if alb is not None:
                alb = float(alb)
                if alb < 3.5:
                    lab_parts.append(f"Albumin {alb:.1f} ⬇️ (ภาวะโภชนาการบกพร่อง)")
                else:
                    lab_parts.append(f"Albumin {alb:.1f} ✅")
            if lab_parts:
                days = (today - lab_date).days if lab_date else None
                prefix = f"ผลเลือดล่าสุด"
                if days is not None:
                    prefix += f" ({days} วันก่อน)"
                    if days > 30:
                        prefix += " ⚠️ เก่าเกิน 1 เดือน"
                paras.append(prefix + ": " + " · ".join(lab_parts))

        # BP
        if bp_list:
            avg_sbp = round(sum(b["sbp"] for b in bp_list) / len(bp_list))
            avg_dbp = round(sum(b["dbp"] for b in bp_list) / len(bp_list))
            bp_txt = f"BP ก่อนฟอกเฉลี่ย {avg_sbp}/{avg_dbp} mmHg"
            if avg_sbp > 160:
                bp_txt += " — ⚠️ ความดันสูง ควรทบทวนยาลดความดัน"
            elif avg_sbp < 100:
                bp_txt += " — ⚠️ ความดันต่ำ ระวัง IDH"
            else:
                bp_txt += " — อยู่ในเกณฑ์"
            paras.append(bp_txt)

        # ESA
        if esa_drug:
            esa_txt = f"ยา ESA: {esa_drug} {esa_dose or ''}"
            if esa_pending:
                act = {"increase": "เพิ่มขนาด", "decrease": "ลดขนาด", "hold": "งดชั่วคราว", "resume": "กลับมาให้"}.get(esa_pending[0], esa_pending[0])
                esa_txt += f" — 💊 รออนุมัติ: {act} {esa_pending[1] or ''}"
            paras.append(esa_txt)

        # Allergies
        if allergies:
            paras.append(f"⚠️ แพ้: {allergies}")

        # Alerts
        if alert_count > 0:
            paras.append(f"🚨 มี {alert_count} alert ที่ยังไม่ได้ตอบรับ")
            emoji_risk = "🔴" if alert_count >= 3 else ("🟡" if emoji_risk == "🟢" else emoji_risk)

        # Closing recommendation
        if emoji_risk == "🔴":
            paras.append("\n📌 สรุป: ต้องเฝ้าระวังใกล้ชิด — มีประเด็นที่ต้องแก้ไข")
        elif emoji_risk == "🟡":
            paras.append("\n📌 สรุป: มีบางจุดที่ต้องติดตาม แต่ภาพรวมยังไม่วิกฤต")
        else:
            paras.append("\n📌 สรุป: สถานะดี ฟอกสม่ำเสมอ ค่าแล็บอยู่ในเกณฑ์")

        narrative = "\n\n".join(paras)

        return jsonify({
            "hn": hn, "name": name,
            "risk_emoji": emoji_risk,
            "narrative": narrative,
            "generated_at": _thai_now().strftime("%d/%m/%Y %H:%M"),
            "disclaimer": "สร้างจากข้อมูลในระบบ — ไม่ใช่การวินิจฉัย",
        })
    except Exception as e:
        logger.error("narrator error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── ทีเด็ด #2: Live Clinic Pulse — ชีพจรคลินิก real-time ──

@hd_bp.route("/clinic-pulse", methods=["GET"])
@require_role("physician", "nurse", "admin")
def clinic_pulse():
    """Live Clinic Pulse — สถานะทุกอย่างในคลินิกแบบ real-time snapshot."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    today = _thai_today()
    now = _thai_now()
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        pulse = {"timestamp": now.strftime("%H:%M:%S"), "date": today.isoformat()}
        with conn.cursor() as cur:
            # Active beds
            cur.execute("""SELECT s.bed_number, p.patient_name, p.hn, s.started_at, s.uf_goal, s.actual_uf,
                                  s.id as session_id
                           FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                           WHERE s.status = 'in_progress' ORDER BY s.bed_number""")
            active_beds = []
            critical_beds = []
            for r in cur.fetchall():
                bed = {"bed": r[0], "name": r[1], "hn": r[2], "session_id": r[6]}
                # Elapsed time
                if r[3]:
                    mins = (now.replace(tzinfo=None) - r[3].replace(tzinfo=None)).total_seconds() / 60
                    bed["elapsed_min"] = int(mins)
                    bed["elapsed"] = f"{int(mins//60)}:{int(mins%60):02d}"
                # UF progress
                bed["uf_goal"] = r[4]
                bed["uf_removed"] = r[5] or 0
                if r[4] and r[4] > 0:
                    bed["uf_pct"] = round((r[5] or 0) / r[4] * 100)
                # Latest BP
                cur.execute("""SELECT sbp, dbp, heart_rate, recorded_at
                               FROM hd_vital_signs WHERE session_id = %s AND sbp > 0
                               ORDER BY recorded_at DESC LIMIT 1""", (r[6],))
                bp = cur.fetchone()
                if bp:
                    bed["sbp"] = bp[0]
                    bed["dbp"] = bp[1]
                    bed["hr"] = bp[2]
                    bp_age = (now.replace(tzinfo=None) - bp[3].replace(tzinfo=None)).total_seconds() / 60 if bp[3] else 999
                    bed["bp_age_min"] = int(bp_age)
                    # Critical detection
                    if bp[0] < 90:
                        bed["critical"] = "🔴 SBP < 90"
                        critical_beds.append(bed)
                    elif bp[0] < 100:
                        bed["warning"] = "🟠 SBP < 100"
                    if bp_age > 45:
                        bed["stale_bp"] = True  # BP ไม่ได้วัดเกิน 45 นาที

                active_beds.append(bed)

            pulse["active_beds"] = active_beds
            pulse["active_count"] = len(active_beds)
            pulse["critical_beds"] = critical_beds
            pulse["critical_count"] = len(critical_beds)

            # Stale BP (ไม่ได้วัดเกิน 30 นาที)
            pulse["stale_bp_count"] = sum(1 for b in active_beds if b.get("stale_bp"))

            # Today stats
            cur.execute("""SELECT
                COUNT(*) FILTER (WHERE status = 'completed'),
                COUNT(*) FILTER (WHERE status = 'scheduled'),
                COUNT(*) FILTER (WHERE status = 'cancelled')
                FROM hd_sessions WHERE session_date = %s""", (today.isoformat(),))
            st = cur.fetchone()
            pulse["today"] = {"completed": st[0], "scheduled": st[1], "cancelled": st[2]}

            # Unack alerts
            cur.execute("""SELECT COUNT(*), COUNT(*) FILTER (WHERE a.severity::text = 'critical')
                           FROM hd_clinical_alerts a JOIN hd_sessions s ON s.id = a.session_id
                           WHERE s.session_date = %s AND a.acknowledged = FALSE""", (today.isoformat(),))
            al = cur.fetchone()
            pulse["alerts"] = {"unack": al[0], "critical_unack": al[1]}

            # Overall health score
            score = 100
            if len(critical_beds) > 0:
                score -= 30 * len(critical_beds)
            if pulse["stale_bp_count"] > 0:
                score -= 10 * pulse["stale_bp_count"]
            if al[1] > 0:
                score -= 20
            pulse["health_score"] = max(0, min(100, score))
            pulse["health_level"] = "critical" if score < 40 else ("warning" if score < 70 else "healthy")

        conn.close()
        return jsonify(pulse)
    except Exception as e:
        logger.error("clinic pulse error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Medication Reconciliation ─────────────────────────────

@hd_bp.route("/patients/<hn>/med-check", methods=["GET"])
@require_role("physician", "nurse", "admin")
def patient_med_check(hn):
    """ตรวจยาคนไข้ไตเทียม — ยาที่ต้องปรับ/ห้าม/ระวัง."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT id, patient_name, vascular_access FROM hd_patient_mapping WHERE hn = %s", (hn,))
            p = cur.fetchone()
            if not p:
                conn.close()
                return jsonify({"error": "ไม่พบผู้ป่วย"}), 404
            pid, name = p[0], p[1]

            # Get latest lab values
            cur.execute("""SELECT k, creatinine, bun, hb, albumin
                           FROM hd_labs WHERE patient_id = %s
                           ORDER BY lab_date DESC LIMIT 1""", (pid,))
            lab = cur.fetchone()
            k_val = float(lab[0]) if lab and lab[0] else None
            cr_val = float(lab[1]) if lab and lab[1] else None

            # Get prescriptions/medications from notes
            cur.execute("""SELECT notes FROM hd_sessions WHERE patient_id = %s
                           AND notes IS NOT NULL AND notes != ''
                           ORDER BY session_date DESC LIMIT 5""", (pid,))
            notes = [r[0] for r in cur.fetchall()]

            # Get ESA info
            cur.execute("""SELECT esa_drug_given, esa_dose_given FROM hd_sessions
                           WHERE patient_id = %s AND esa_given = TRUE
                           ORDER BY session_date DESC LIMIT 1""", (pid,))
            esa = cur.fetchone()

            # Heparin info
            cur.execute("""SELECT protocol, loading_dose, maintenance_dose
                           FROM hd_anticoagulation a JOIN hd_sessions s ON s.id = a.session_id
                           WHERE s.patient_id = %s ORDER BY a.recorded_at DESC LIMIT 1""", (pid,))
            hep = cur.fetchone()

        conn.close()

        # HD-specific medication alerts (rule-based)
        alerts = []

        # 1. K+ related
        if k_val:
            if k_val > 6.0:
                alerts.append({"level": "danger", "icon": "🔴", "drug": "Potassium",
                    "message": f"K+ = {k_val} mEq/L (สูงมาก)",
                    "action": "ห้าม KCl supplement, ระวัง ACEi/ARB/Spironolactone, แนะนำ Kayexalate"})
            elif k_val > 5.5:
                alerts.append({"level": "warning", "icon": "🟠", "drug": "Potassium",
                    "message": f"K+ = {k_val} mEq/L (สูง)",
                    "action": "ทบทวนยาที่เพิ่ม K+: ACEi, ARB, Spironolactone, NSAIDs"})

        # 2. ESA check
        if esa:
            alerts.append({"level": "info", "icon": "💉", "drug": f"ESA ({esa[0] or 'EPO'})",
                "message": f"Dose: {esa[1] or '-'}",
                "action": "ให้หลังฟอก (ไม่ถูกดึงออก) · ตรวจ Hb ทุก 2-4 สัปดาห์"})

        # 3. Heparin check
        if hep:
            proto = hep[0] or "Heparin"
            alerts.append({"level": "info", "icon": "💊", "drug": proto,
                "message": f"Loading {hep[1] or '-'} / Maint {hep[2] or '-'}",
                "action": "หยุด Heparin 30 นาทีก่อนถอดเข็ม" + (" · ระวัง HIT" if "heparin" in proto.lower() else "")})

        # 4. Dialysis-specific drug warnings
        hd_drug_rules = [
            {"drug": "NSAIDs (Ibuprofen, Diclofenac, Naproxen)", "level": "danger", "icon": "🔴",
             "message": "ห้ามใช้ในคนไข้ไตเทียม", "action": "เปลี่ยนเป็น Paracetamol · NSAIDs ทำไตเสื่อมเพิ่ม + เลือดออก GI"},
            {"drug": "Metformin", "level": "danger", "icon": "🔴",
             "message": "ห้ามใช้ใน GFR <30 / ไตเทียม", "action": "เสี่ยง Lactic acidosis · ถูกดึงออกบางส่วนตอนฟอก"},
            {"drug": "Gabapentin / Pregabalin", "level": "warning", "icon": "🟠",
             "message": "ต้องลดขนาด 50-75%", "action": "ให้หลังฟอก (ถูกดึงออก) · Gabapentin 100-300mg หลังฟอก"},
            {"drug": "Vancomycin", "level": "warning", "icon": "🟠",
             "message": "ให้ระหว่างฟอกชั่วโมงสุดท้าย", "action": "ไม่ต้อง dose ทุกวัน · ตรวจ trough level ก่อนฟอกครั้งต่อไป"},
            {"drug": "Gentamicin / Amikacin", "level": "warning", "icon": "🟠",
             "message": "ให้หลังฟอก · ลดขนาด", "action": "ตรวจ level · ถูกดึงออกตอนฟอก"},
            {"drug": "Enoxaparin (LMWH)", "level": "warning", "icon": "🟠",
             "message": "ห้ามใช้ใน CrCl <30", "action": "ใช้ Unfractionated Heparin แทน · LMWH สะสมในไตวาย"},
            {"drug": "Spironolactone", "level": "warning", "icon": "🟠",
             "message": "ระวัง Hyperkalemia", "action": "ตรวจ K+ ทุกสัปดาห์ · หยุดถ้า K+ >5.5"},
            {"drug": "ACEi / ARB", "level": "info", "icon": "🟡",
             "message": "ใช้ได้แต่ระวัง K+ + ความดันตก", "action": "ไม่ให้ก่อนฟอก (เสี่ยง IDH) · ตรวจ K+ สม่ำเสมอ"},
            {"drug": "Phosphate Binders (CaCO3, Sevelamer)", "level": "info", "icon": "💊",
             "message": "ให้พร้อมอาหาร", "action": "CaCO3: ระวัง Ca สูง ถ้าใช้ Calcitriol ด้วย"},
            {"drug": "Calcitriol / Alfacalcidol", "level": "info", "icon": "💊",
             "message": "Vitamin D active form", "action": "ตรวจ Ca, PO4, iPTH ทุก 1-3 เดือน"},
        ]
        alerts.extend(hd_drug_rules)

        return jsonify({
            "hn": hn, "name": name,
            "lab": {"k": k_val, "cr": cr_val, "hb": float(lab[3]) if lab and lab[3] else None} if lab else None,
            "alerts": alerts,
            "total_alerts": len(alerts),
            "danger_count": sum(1 for a in alerts if a["level"] == "danger"),
            "warning_count": sum(1 for a in alerts if a["level"] == "warning"),
        })
    except Exception as e:
        logger.error("med-check error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Upgrade #4: AI Drug Advisor — ครบทุกยาไต ─────────────

@hd_bp.route("/patients/<hn>/drug-advisor", methods=["GET"])
@require_role("physician", "nurse", "admin")
def patient_drug_advisor(hn):
    """AI Drug Advisor — วิเคราะห์ยาครบทุกตัวสำหรับคนไข้ไตเทียม.

    ใช้ lab จริง (Hb, Ferritin, TSAT, iPTH, PO4, Ca, K+, Alb) + prescription
    → แนะนำ: ESA, IV Iron, Phosphate binder, Calcitriol, Anti-HT, Anti-K+
    """
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT id, patient_name FROM hd_patient_mapping WHERE hn = %s", (hn,))
            p = cur.fetchone()
            if not p:
                conn.close()
                return jsonify({"error": "ไม่พบผู้ป่วย"}), 404
            pid, name = p

            # Latest labs
            cur.execute("""SELECT hb, hct, k, phos, ca, albumin,
                                  ferritin, tsat, pth, lab_date
                           FROM hd_labs WHERE patient_id = %s
                           ORDER BY lab_date DESC LIMIT 1""", (pid,))
            lab = cur.fetchone()

            # ESA info
            cur.execute("""SELECT esa_drug_given, esa_dose_given FROM hd_sessions
                           WHERE patient_id = %s AND esa_given = TRUE
                           ORDER BY session_date DESC LIMIT 1""", (pid,))
            esa = cur.fetchone()

        conn.close()

        advisories = []
        if not lab:
            return jsonify({"hn": hn, "name": name, "advisories": [
                {"category": "info", "icon": "ℹ️", "drug": "Lab", "message": "ยังไม่มีผลเลือด — ต้องมี lab ก่อนแนะนำยา"}
            ]})

        hb = float(lab[0]) if lab[0] else None
        k = float(lab[2]) if lab[2] else None
        po4 = float(lab[3]) if lab[3] else None
        ca = float(lab[4]) if lab[4] else None
        alb = float(lab[5]) if lab[5] else None
        ferritin = float(lab[6]) if lab[6] else None
        tsat = float(lab[7]) if lab[7] else None
        pth = float(lab[8]) if lab[8] else None
        lab_date = lab[9]
        lab_age = (_thai_today() - lab_date).days if lab_date else None

        # === 1. ESA (EPO/Darbepoetin) ===
        if hb is not None:
            if hb < 10:
                dose_txt = "เริ่ม/เพิ่ม ESA"
                if esa:
                    advisories.append({"category": "esa", "icon": "💉", "drug": f"ESA ({esa[0] or 'EPO'})",
                        "current": f"Dose {esa[1] or '-'}", "lab": f"Hb {hb:.1f}",
                        "advice": "⬆️ เพิ่มขนาด 25% (Hb ต่ำกว่าเป้า 10-12)",
                        "target": "Hb 10-12 g/dL", "priority": "high"})
                else:
                    advisories.append({"category": "esa", "icon": "💉", "drug": "ESA",
                        "advice": "🆕 เริ่มให้ ESA — Hb ต่ำ ({:.1f})".format(hb),
                        "target": "Hb 10-12 g/dL", "priority": "high"})
            elif hb > 13:
                advisories.append({"category": "esa", "icon": "💉", "drug": "ESA",
                    "lab": f"Hb {hb:.1f}", "advice": "⏸ งด ESA ชั่วคราว (Hb > 13)",
                    "target": "กลับมาให้เมื่อ Hb < 11", "priority": "high"})
            elif hb >= 10:
                advisories.append({"category": "esa", "icon": "💉", "drug": "ESA",
                    "lab": f"Hb {hb:.1f}", "advice": "✅ Hb ในเป้า — คงขนาด ESA เดิม",
                    "priority": "low"})

        # === 2. IV Iron ===
        if ferritin is not None or tsat is not None:
            iron_needed = False
            reason = []
            if ferritin is not None and ferritin < 200:
                iron_needed = True
                reason.append(f"Ferritin {ferritin:.0f} (ต่ำ <200)")
            if tsat is not None and tsat < 20:
                iron_needed = True
                reason.append(f"TSAT {tsat:.0f}% (ต่ำ <20%)")
            if iron_needed:
                advisories.append({"category": "iron", "icon": "🩸", "drug": "IV Iron (Ferric carboxymaltose / Iron sucrose)",
                    "lab": " · ".join(reason),
                    "advice": "💊 ให้ IV Iron — KDIGO: Ferritin 200-500, TSAT 20-30%",
                    "dosing": "Iron sucrose 100mg IV ระหว่างฟอกชั่วโมงสุดท้าย × 10 ครั้ง หรือ Ferinject 500-1000mg",
                    "priority": "high"})
            elif ferritin and ferritin > 800:
                advisories.append({"category": "iron", "icon": "🩸", "drug": "IV Iron",
                    "lab": f"Ferritin {ferritin:.0f}",
                    "advice": "⏸ งด IV Iron — Ferritin สูง >800 (เสี่ยง Iron overload)",
                    "priority": "medium"})
            else:
                advisories.append({"category": "iron", "icon": "🩸", "drug": "IV Iron",
                    "lab": f"Ferritin {ferritin or '-'} · TSAT {tsat or '-'}%",
                    "advice": "✅ เหล็กเพียงพอ — ตรวจซ้ำ 3 เดือน",
                    "priority": "low"})

        # === 3. Phosphate Binder ===
        if po4 is not None:
            if po4 > 5.5:
                advisories.append({"category": "phosphate", "icon": "💊", "drug": "Phosphate Binder",
                    "lab": f"PO4 {po4:.1f} mg/dL (สูง)",
                    "advice": "⬆️ เพิ่ม/เริ่ม Phosphate binder — กินพร้อมอาหารทุกมื้อ",
                    "options": "CaCO3 500-1000mg หรือ Sevelamer 800mg ก่อนอาหาร" + (" · ⚠️ ระวัง Ca สูงถ้าใช้ CaCO3 + Calcitriol" if ca and ca > 10 else ""),
                    "target": "PO4 3.5-5.5 mg/dL", "priority": "high"})
            elif po4 < 3.5:
                advisories.append({"category": "phosphate", "icon": "💊", "drug": "Phosphate Binder",
                    "lab": f"PO4 {po4:.1f}", "advice": "⬇️ ลด/หยุด Phosphate binder — PO4 ต่ำ",
                    "priority": "medium"})
            else:
                advisories.append({"category": "phosphate", "icon": "💊", "drug": "Phosphate",
                    "lab": f"PO4 {po4:.1f}", "advice": "✅ PO4 ในเป้า",
                    "priority": "low"})

        # === 4. Calcitriol / Vitamin D ===
        if pth is not None:
            if pth > 600:
                advisories.append({"category": "vitd", "icon": "☀️", "drug": "Calcitriol",
                    "lab": f"iPTH {pth:.0f} pg/mL (สูงมาก)",
                    "advice": "⬆️ เพิ่ม Calcitriol 0.25-0.5 mcg/วัน หรือหลังฟอก",
                    "target": "iPTH 150-600 pg/mL (KDIGO)" + (f" · ⚠️ Ca {ca:.1f} สูง — ใช้ Cinacalcet แทน" if ca and ca > 10.2 else ""),
                    "priority": "high"})
            elif pth > 300:
                advisories.append({"category": "vitd", "icon": "☀️", "drug": "Calcitriol",
                    "lab": f"iPTH {pth:.0f}", "advice": "คงขนาด Calcitriol — iPTH อยู่ในเกณฑ์สูง",
                    "priority": "medium"})
            elif pth < 150:
                advisories.append({"category": "vitd", "icon": "☀️", "drug": "Calcitriol",
                    "lab": f"iPTH {pth:.0f}", "advice": "⬇️ ลด/หยุด Calcitriol — iPTH ต่ำ (เสี่ยง Adynamic bone)",
                    "priority": "medium"})

        # === 5. Potassium Management ===
        if k is not None:
            if k > 6.0:
                advisories.append({"category": "potassium", "icon": "⚡", "drug": "K+ Management",
                    "lab": f"K+ {k:.1f} mEq/L (อันตราย!)",
                    "advice": "🚨 K+ สูงมาก — Kayexalate 15-30g + ตรวจ ECG + ฟอก urgent",
                    "priority": "critical"})
            elif k > 5.5:
                advisories.append({"category": "potassium", "icon": "⚡", "drug": "K+ Management",
                    "lab": f"K+ {k:.1f}", "advice": "⬆️ Kayexalate + ทบทวนยาที่เพิ่ม K+ (ACEi/ARB/Spironolactone)",
                    "priority": "high"})

        # === 6. Albumin / Nutrition ===
        if alb is not None and alb < 3.5:
            advisories.append({"category": "nutrition", "icon": "🥩", "drug": "Nutrition",
                "lab": f"Albumin {alb:.1f} g/dL (ต่ำ)",
                "advice": "แนะนำเพิ่มโปรตีน 1.0-1.2 g/kg/วัน · พิจารณา Oral supplement",
                "priority": "medium"})

        # Lab age warning
        if lab_age and lab_age > 30:
            advisories.append({"category": "info", "icon": "⏰", "drug": "Lab",
                "advice": f"ผลเลือดเก่า {lab_age} วัน — ควรตรวจซ้ำเพื่อปรับยาให้แม่นยำ",
                "priority": "medium"})

        # Sort by priority
        pri_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        advisories.sort(key=lambda a: pri_order.get(a.get("priority", "low"), 9))

        return jsonify({
            "hn": hn, "name": name,
            "advisories": advisories,
            "lab_summary": {
                "hb": hb, "ferritin": ferritin, "tsat": tsat, "pth": pth,
                "po4": po4, "ca": ca, "k": k, "alb": alb,
                "date": lab_date.isoformat() if lab_date else None, "age_days": lab_age,
            },
            "total": len(advisories),
            "critical_count": sum(1 for a in advisories if a.get("priority") == "critical"),
            "high_count": sum(1 for a in advisories if a.get("priority") == "high"),
        })
    except Exception as e:
        logger.error("drug-advisor error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Patient Trend Dashboard ───────────────────────────────

@hd_bp.route("/patients/<hn>/trends", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def patient_trends(hn):
    """6-month trend data: Hb, Kt/V, weight, BP, UF for charts."""
    import os, psycopg2
    from datetime import timedelta
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    months = min(int(request.args.get("months", 6) or 6), 24)
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM hd_patient_mapping WHERE hn = %s", (hn,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "patient not found"}), 404
            pid = row[0]
            cutoff = (_thai_today() - timedelta(days=months * 30)).isoformat()

            # Sessions: weight, UF, Kt/V, BP
            cur.execute("""SELECT session_date, pre_weight, post_weight, uf_goal, actual_uf,
                                  kt_v, urr
                           FROM hd_sessions WHERE patient_id = %s AND session_date >= %s
                           ORDER BY session_date, id""", (pid, cutoff))
            sessions = []
            for r in cur.fetchall():
                sessions.append({
                    "date": r[0].isoformat() if r[0] else None,
                    "pre_weight": float(r[1]) if r[1] else None,
                    "post_weight": float(r[2]) if r[2] else None,
                    "uf_goal": r[3], "actual_uf": r[4],
                    "kt_v": float(r[5]) if r[5] else None,
                    "urr": float(r[6]) if r[6] else None,
                })

            # BP trends from vital signs (pre-HD)
            cur.execute("""SELECT v.recorded_at::date, v.sbp, v.dbp, v.heart_rate
                           FROM hd_vital_signs v
                           JOIN hd_sessions s ON s.id = v.session_id
                           WHERE s.patient_id = %s AND v.phase = 'pre'
                             AND v.recorded_at::date >= %s
                           ORDER BY v.recorded_at""", (pid, cutoff))
            bp_data = [{"date": r[0].isoformat(), "sbp": r[1], "dbp": r[2], "hr": r[3]}
                       for r in cur.fetchall()]

            # Lab trends: Hb, Hct, BUN, Cr, K, Albumin
            cur.execute("""SELECT lab_date, hb, hct, bun, creatinine, k, albumin
                           FROM hd_labs WHERE patient_id = %s AND lab_date >= %s
                           ORDER BY lab_date""", (pid, cutoff))
            labs = [{"date": r[0].isoformat() if r[0] else None,
                     "hb": float(r[1]) if r[1] else None,
                     "hct": float(r[2]) if r[2] else None,
                     "bun": float(r[3]) if r[3] else None,
                     "cr": float(r[4]) if r[4] else None,
                     "k": float(r[5]) if r[5] else None,
                     "alb": float(r[6]) if r[6] else None}
                    for r in cur.fetchall()]

            # ESA history
            cur.execute("""SELECT s.session_date, s.esa_drug_given, s.esa_dose_given
                           FROM hd_sessions s
                           WHERE s.patient_id = %s AND s.session_date >= %s
                             AND s.esa_given = TRUE
                           ORDER BY s.session_date""", (pid, cutoff))
            esa = [{"date": r[0].isoformat() if r[0] else None,
                    "drug": r[1], "dose": r[2]}
                   for r in cur.fetchall()]

        conn.close()
        return jsonify({
            "hn": hn, "months": months,
            "sessions": sessions, "bp": bp_data,
            "labs": labs, "esa": esa,
        })
    except Exception as e:
        logger.error("patient trends error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Shift Handover ────────────────────────────────────────

@hd_bp.route("/handover", methods=["GET"])
@require_role("physician", "nurse", "admin")
def handover_summary():
    """สรุปส่งต่อเวร — ข้อมูลทุกเตียง + alerts."""
    import os
    from .shift_handover import generate_handover_summary
    shift = request.args.get("shift", "morning")
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    result = generate_handover_summary(db_url, shift)
    return jsonify(result)


@hd_bp.route("/handover/push", methods=["POST"])
@require_role("physician", "nurse", "admin")
def handover_push():
    """ส่งสรุปส่งต่อเวรผ่าน LINE."""
    import os
    from .shift_handover import generate_handover_summary
    data = request.get_json(silent=True) or {}
    shift = data.get("shift", "morning")
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    result = generate_handover_summary(db_url, shift)
    msg = result.get("line_message", "")
    if msg:
        r = push_nurse_group(msg)
        result["line_sent"] = r.get("ok", False) if isinstance(r, dict) else bool(r)
    return jsonify(result)


# ── KPI Dashboard ─────────────────────────────────────────

@hd_bp.route("/kpi", methods=["GET"])
@require_role("physician", "admin", "auditor")
def hd_kpi():
    """HD KPI Dashboard API."""
    import os
    from .kpi_engine import KpiEngine

    period = request.args.get("period", "monthly")
    days = {"weekly": 7, "monthly": 30, "quarterly": 90}.get(period, 30)

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    registry = current_app.config.get("MASTER_REGISTRY")
    engine = KpiEngine(master_registry=registry, db_url=db_url)
    report = engine.generate_report(period=period, days=days)

    log_action(user_id=g.get("hd_user_id"), action="kpi.view",
               resource_type="kpi", ip_address=request.remote_addr or "")

    return jsonify(report.to_dict())


# ── Monthly Report ────────────────────────────────────────

@hd_bp.route("/report/monthly", methods=["GET"])
@require_role("physician", "admin")
def hd_monthly_report():
    """Redirect to monthly-summary (F8) — consolidated endpoint."""
    return redirect(url_for("hd.monthly_summary_report",
                            month=request.args.get("month", ""),
                            year=request.args.get("year", "")))


# ── BI Reports ────────────────────────────────────────────

@hd_bp.route("/report/ha", methods=["GET"])
@require_role("physician", "admin")
def hd_ha_report():
    """Redirect to quality-indicators (F13) — consolidated endpoint."""
    return redirect(url_for("hd.quality_indicators_report",
                            month=request.args.get("month", ""),
                            year=request.args.get("year", "")))


@hd_bp.route("/report/trt", methods=["GET"])
@require_role("physician", "admin")
def hd_trt_report():
    """TRT Registry export (anonymized)."""
    import os
    from .bi_reporting import BiReportEngine

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    registry = current_app.config.get("MASTER_REGISTRY")
    engine = BiReportEngine(master_registry=registry, db_url=db_url)
    records = engine.generate_trt_records()

    log_action(user_id=g.get("hd_user_id"), action="report.trt",
               resource_type="report", ip_address=request.remote_addr or "")
    return jsonify({"records": records, "total": len(records), "format": "TRT_Registry"})


@hd_bp.route("/export/patients", methods=["GET"])
@require_role("physician", "admin")
def hd_export_patients():
    """Export patient list as JSON (for Excel/CSV conversion)."""
    import os
    from .bi_reporting import BiReportEngine

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    registry = current_app.config.get("MASTER_REGISTRY")
    engine = BiReportEngine(master_registry=registry, db_url=db_url)
    rows = engine.export_patient_list_json()

    log_action(user_id=g.get("hd_user_id"), action="export.patients",
               resource_type="export", ip_address=request.remote_addr or "")
    return jsonify({"patients": rows, "total": len(rows)})


# ── HIS Integration Webhook ──────────────────────────────

@hd_bp.route("/his/webhook", methods=["POST"])
def his_webhook():
    """Receive data push from Hospital HIS (FHIR R4)."""
    import os
    from .his_adapter import HisConfig, handle_his_webhook

    config = HisConfig.from_env()
    if not config.enabled:
        return jsonify({"error": "HIS integration disabled"}), 503

    # Verify webhook source (API key in header)
    api_key = request.headers.get("X-HIS-API-Key", "")
    if api_key != config.api_key:
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Empty payload"}), 400

    result = handle_his_webhook(payload, request.remote_addr or "")
    return jsonify(result)


@hd_bp.route("/his/status", methods=["GET"])
@require_role("admin")
def his_status():
    """Check HIS integration status."""
    from .his_adapter import HisAdapter
    adapter = HisAdapter()
    return jsonify(adapter.status)


# ── Audit ─────────────────────────────────────────────────

@hd_bp.route("/audit", methods=["GET"])
@require_role("admin", "auditor")
def list_audit_logs():
    """Query audit trail."""
    from .audit import query_audit_log
    logs = query_audit_log(
        user_id=request.args.get("user_id", type=int),
        action=request.args.get("action"),
        limit=request.args.get("limit", 50, type=int),
    )
    return jsonify({"logs": logs, "total": len(logs)})


# ── Settings (runtime configuration via DB) ──────────────

@hd_bp.route("/settings", methods=["GET"])
@require_role("physician", "admin")
def get_settings():
    """Get all HD settings from database."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"settings": {}})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT key, value, description, updated_at
                FROM hd_settings ORDER BY key
            """)
            settings = {}
            for r in cur.fetchall():
                settings[r[0]] = {
                    "value": r[1], "description": r[2],
                    "updated_at": r[3].isoformat() if r[3] else None,
                }
        conn.close()
        if "drive_refresh_token" in settings:   # secret — never return it to the page
            settings["drive_refresh_token"]["value"] = "••••••••" if settings["drive_refresh_token"]["value"] else ""
        # Merge with ENV fallbacks
        env_alert = os.environ.get("HD_ALERT_RECIPIENTS", "")
        if "alert_recipients" not in settings and env_alert:
            settings["alert_recipients"] = {
                "value": env_alert, "description": "จาก ENV",
                "updated_at": None,
            }
        return jsonify({"settings": settings})
    except Exception as e:
        logger.error("get_settings error: %s", e)
        return jsonify({"settings": {}, "error": str(e)})


@hd_bp.route("/settings/test-line", methods=["POST"])
@require_role("physician", "admin")
def test_line_push():
    """Send a test LINE push to the alert recipients so the physician can
    verify the whole chain (token → Messaging API → userId) without entering
    a vital on a real patient."""
    import os
    from flask import current_app
    push_fn = current_app.config.get("LINE_PUSH_FN")
    ids = get_setting_value("alert_recipients", "") or get_setting_value("physician_line_id", "")
    recipients = [r.strip() for r in ids.split(",") if r.strip()]
    checks = {
        "push_fn": bool(push_fn),
        "channel_token": bool(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")),
        "recipients": len(recipients),
    }
    if not push_fn:
        return jsonify({"ok": False, "checks": checks, "error": "LINE_PUSH_FN ไม่ได้ตั้งค่าใน server"}), 500
    if not checks["channel_token"]:
        return jsonify({"ok": False, "checks": checks, "error": "ไม่มี LINE_CHANNEL_ACCESS_TOKEN บน Render"}), 500
    if not recipients:
        return jsonify({"ok": False, "checks": checks, "error": "ยังไม่ตั้งค่า alert_recipients"}), 400
    msg = (f"🧪 ทดสอบระบบแจ้งเตือน HD Smart Monitoring\n"
           f"ส่งโดย {g.get('hd_username') or 'HD'} · {_thai_now().strftime('%d/%m %H:%M')} น.\n"
           f"ถ้าเห็นข้อความนี้ = alert ความดันตก/วิกฤตจะส่งถึงคุณได้")
    results = {}
    for uid in recipients:
        try:
            results[uid[:6] + "…"] = push_fn(uid, msg) is not False
        except Exception as e:
            results[uid[:6] + "…"] = f"error: {e}"
    ok = any(v is True for v in results.values())
    return jsonify({"ok": ok, "checks": checks, "results": results,
                    "hint": None if ok else "LINE API ปฏิเสธ — ดู Render log บรรทัด [PUSH] (token หมดอายุ / userId ไม่ได้ add เพื่อน OA)"})


@hd_bp.route("/settings", methods=["POST"])
@require_role("physician", "admin")
def update_settings():
    """Update HD settings in database."""
    import os
    data = request.get_json(silent=True) or {}
    key = data.get("key", "").strip()
    value = data.get("value", "").strip()

    allowed_keys = [
        "alert_recipients",        # LINE userId for alerts
        "physician_line_id",       # physician LINE userId (raw, for push)
        "nurse_group_id",          # LINE groupId of the nurse group (Messaging API push; 'groupid' command)
        "line_notify_token",       # legacy — LINE Notify discontinued 31 Mar 2025 (ignored)
        "nurse_group_token",       # legacy — ignored
        "clinic_name",             # clinic display name
        "default_duration_hours",  # default dialysis duration
        "alert_sbp_drop",          # IDH SBP drop threshold
        "alert_severe_sbp",        # severe IDH threshold
        "patient_summary_auto",    # ว้าว #7: on/off auto LINE summary to patient
        "patient_lab_notify",      # push "ผลเลือดใหม่" to linked patients after an import (on/off)
        "water_total_cl_max", "water_free_cl_max", "water_hardness_max",   # RO water daily check limits
        "water_interval_days", "water_sample_point",
        "uf_rate_max", "uf_rate_caution", "dw_review_every", "dw_step_kg", "dw_min_interval_days",   # DW Autopilot
        "anemia_ferritin_low", "anemia_tsat_low", "anemia_ferritin_high", "anemia_eri_high", "anemia_eri_mod",
        "hbv_beds", "hcv_beds", "scheduler_push_1800",   # Smart Bed Scheduler
        "patient_push_requires_consent", "clinic_phone",   # PDPA
        "esa_target_low", "esa_target_high", "esa_hold_above",   # ESA Autopilot protocol
        "esa_step_pct", "esa_min_interval_days", "esa_resume_below",
        "monthly_lab_summary",       # push สรุปผลเลือดรายเดือนให้ผู้ป่วย (on/off)
        "weekly_health_tips",        # push tip สุขภาพไตรายสัปดาห์ให้ผู้ป่วย (on/off)
        "idwg_limit_pct",            # 17 พ.ย. 2569: เกณฑ์น้ำหนักขึ้นระหว่างรอบ (% DW) ค่าเริ่มต้น 4
        "patient_weight_coach",      # ตอบผู้ป่วยที่ส่ง "น้ำหนัก xx" ทาง LINE (on/off)
        "tpl_weight_reply",          # ข้อความตอบน้ำหนัก (แก้ได้ มีตัวแปร {weight} {gain} ...)
        "patient_card_note",         # ข้อความจากแพทย์ต่อท้ายการ์ดสรุปหลังฟอกทุกใบ
    ]
    if key not in allowed_keys:
        return jsonify({"error": f"Invalid setting key: {key}"}), 400

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "Database not configured"}), 500

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO hd_settings (key, value, description, updated_at, updated_by)
                VALUES (%s, %s, %s, NOW(), %s)
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = NOW(),
                    updated_by = EXCLUDED.updated_by
            """, (key, value, data.get("description", ""), g.get("hd_user_id")))
        conn.commit()
        conn.close()
        log_action(
            user_id=g.get("hd_user_id"), action="settings.update",
            resource_type="settings", new_values={"key": key, "value": value},
            ip_address=request.remote_addr or "",
        )
        return jsonify({"status": "saved", "key": key})
    except Exception as e:
        logger.error("update_settings error: %s", e)
        return jsonify({"error": str(e)}), 500


def get_setting_value(key, default=""):
    """Helper: get a setting from DB, fallback to ENV, then default."""
    import os
    env_map = {
        "alert_recipients": "HD_ALERT_RECIPIENTS",
        "physician_line_id": "HD_PHYSICIAN_LINE_ID",
    }
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if db_url:
        try:
            import psycopg2
            conn = psycopg2.connect(db_url, connect_timeout=3)
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM hd_settings WHERE key = %s", (key,))
                row = cur.fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
    # Fallback to ENV
    env_key = env_map.get(key)
    if env_key:
        return os.environ.get(env_key, default)
    return default


# ── Monitoring API ────────────────────────────────────────

@hd_bp.route("/monitoring/errors", methods=["GET"])
@require_role("physician", "admin")
def get_error_summary():
    """ดู error summary — สำหรับ admin."""
    from .monitoring import error_tracker, get_uptime
    return jsonify({
        "uptime": get_uptime(),
        "errors": error_tracker.get_summary(),
        "recent_errors": error_tracker.get_recent_errors(20),
    })


# ── PDPA API ──────────────────────────────────────────────

@hd_bp.route("/pdpa/status", methods=["GET"])
@require_role("physician", "admin")
def pdpa_status():
    """สรุปสถานะ PDPA compliance."""
    from .pdpa import get_pdpa_status
    return jsonify(get_pdpa_status())


@hd_bp.route("/pdpa/patient-report/<hn>", methods=["GET"])
@require_role("physician", "admin")
def pdpa_patient_report(hn):
    """สร้างรายงานข้อมูลผู้ป่วย (Right to Access)."""
    import os
    from .pdpa import generate_data_access_report
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    report = generate_data_access_report(db_url, hn)
    return jsonify(report)


# ── N4: Lab Extract API ───────────────────────────────────

@hd_bp.route("/lab/extract", methods=["POST"])
@require_role("physician", "nurse", "admin")
def extract_lab_from_text():
    """ดึงค่าแล็บจาก OCR text อัตโนมัติ."""
    from .lab_extractor import extract_lab_values, format_lab_summary_th, format_lab_summary_en
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    lang = data.get("lang", "th")
    if not text:
        return jsonify({"error": "text required"}), 400
    lab_data = extract_lab_values(text)
    summary = format_lab_summary_en(lab_data) if lang == "en" else format_lab_summary_th(lab_data)
    return jsonify({**lab_data, "summary": summary, "lang": lang})


# ── N5: Language API ──────────────────────────────────────

@hd_bp.route("/i18n/translate", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def get_translations():
    """ดึง translations ทั้งหมดสำหรับ frontend."""
    from .i18n import TRANSLATIONS
    lang = request.args.get("lang", "th")
    result = {}
    for key, entry in TRANSLATIONS.items():
        result[key] = entry.get(lang, entry.get("th", key))
    return jsonify({"lang": lang, "translations": result})


# ── N1: Analytics API — UF goal vs actual, BP trend ───────

@hd_bp.route("/analytics/clinic", methods=["GET"])
@require_role("physician", "admin", "auditor")
def clinic_analytics():
    """Clinic-wide analytics: UF achievement, BP trends, session stats."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    days = int(request.args.get("days", "30"))
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        result = {}

        # Session stats
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), COUNT(CASE WHEN status='completed' THEN 1 END),
                       AVG(actual_uf) FILTER (WHERE actual_uf > 0),
                       AVG(uf_goal) FILTER (WHERE uf_goal > 0)
                FROM hd_sessions
                WHERE started_at > NOW() - INTERVAL '%s days'
            """ % days)
            r = cur.fetchone()
            result["sessions"] = {
                "total": r[0], "completed": r[1],
                "avg_actual_uf": round(r[2] or 0), "avg_uf_goal": round(r[3] or 0),
                "uf_achievement_pct": round((r[2] or 0) / (r[3] or 1) * 100, 1),
            }

        # Daily BP trends (avg SBP per day, last N days)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DATE(recorded_at) as d, AVG(sbp)::int, AVG(dbp)::int,
                       AVG(heart_rate)::int, COUNT(*)
                FROM hd_vital_signs
                WHERE recorded_at > NOW() - INTERVAL '%s days' AND sbp > 0
                GROUP BY DATE(recorded_at) ORDER BY d
            """ % days)
            bp_trend = []
            for r in cur.fetchall():
                bp_trend.append({
                    "date": r[0].isoformat(), "avg_sbp": r[1],
                    "avg_dbp": r[2], "avg_hr": r[3], "readings": r[4],
                })
            result["bp_trend"] = bp_trend

        # UF goal vs actual per day
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DATE(started_at) as d,
                       AVG(uf_goal)::int, AVG(actual_uf)::int, COUNT(*)
                FROM hd_sessions
                WHERE started_at > NOW() - INTERVAL '%s days'
                  AND status = 'completed' AND uf_goal > 0
                GROUP BY DATE(started_at) ORDER BY d
            """ % days)
            uf_trend = []
            for r in cur.fetchall():
                uf_trend.append({
                    "date": r[0].isoformat(), "avg_goal": r[1],
                    "avg_actual": r[2], "sessions": r[3],
                })
            result["uf_trend"] = uf_trend

        # Alert summary
        with conn.cursor() as cur:
            cur.execute("""
                SELECT severity, COUNT(*) FROM hd_clinical_alerts
                WHERE created_at > NOW() - INTERVAL '%s days'
                GROUP BY severity
            """ % days)
            alert_counts = {}
            for r in cur.fetchall():
                alert_counts[r[0]] = r[1]
            result["alerts"] = alert_counts

        conn.close()
        return jsonify(result)
    except Exception as e:
        logger.error("clinic_analytics error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── N3: Auto-schedule — จัดคิวเตียงอัตโนมัติตามตาราง ──────

@hd_bp.route("/schedule/auto-assign", methods=["POST"])
@require_role("physician", "nurse", "admin")
def auto_assign_beds():
    """Auto-assign today's scheduled patients to beds.

    ดึงผู้ป่วยที่มีตารางฟอกวันนี้ → จัดเตียงอัตโนมัติ → สร้าง scheduled sessions
    Shift: "morning" (เช้า 07-15), "afternoon" (บ่าย 13-21), "evening" (ค่ำ 17-01)
    """
    import os
    data = request.get_json(silent=True) or {}
    shift = data.get("shift", "morning")
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)

        # วันนี้ภาษาไทย
        day_map = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}
        today_th = day_map.get(_thai_today().weekday(), "")
        if not today_th:
            conn.close()
            return jsonify({"assigned": 0, "message": "ไม่มีรอบฟอกวันนี้"})

        # ดึงผู้ป่วยที่มีตารางวันนี้
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, hn, patient_name, vascular_access
                FROM hd_patient_mapping
                WHERE is_active = TRUE AND schedule_days ILIKE %s
                ORDER BY hn
            """, (f"%{today_th}%",))
            patients = cur.fetchall()

        # ดึงเตียงที่ถูกใช้แล้ว
        with conn.cursor() as cur:
            cur.execute("""
                SELECT bed_number FROM hd_sessions
                WHERE status IN ('scheduled', 'in_progress') AND session_date = CURRENT_DATE
            """)
            used_beds = {r[0] for r in cur.fetchall()}

        # จัดเตียง
        available_beds = [b for b in range(1, HdConfig.TOTAL_BEDS + 1) if b not in used_beds]
        assigned = []
        skipped = []

        for pid, hn, name, va in patients:
            # เช็คว่ามี session อยู่แล้วหรือไม่
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM hd_sessions
                    WHERE patient_id = %s AND status IN ('scheduled', 'in_progress')
                      AND session_date = CURRENT_DATE
                """, (pid,))
                if cur.fetchone():
                    skipped.append({"hn": hn, "name": name, "reason": "มี session อยู่แล้ว"})
                    continue

            if not available_beds:
                skipped.append({"hn": hn, "name": name, "reason": "เตียงเต็ม"})
                continue

            bed = available_beds.pop(0)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO hd_sessions (patient_id, bed_number, status,
                                             session_date, shift, uf_goal)
                    VALUES (%s, %s, 'scheduled', CURRENT_DATE, %s, 2000)
                    RETURNING id
                """, (pid, bed, shift))
                sid = cur.fetchone()[0]
                assigned.append({"hn": hn, "name": name, "bed": bed, "session_id": sid})

        conn.commit()
        conn.close()

        return jsonify({
            "assigned": len(assigned),
            "skipped": len(skipped),
            "shift": shift,
            "today": today_th,
            "patients": assigned,
            "skipped_details": skipped,
        })
    except Exception as e:
        logger.error("auto_assign_beds error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── N2: LINE Group Notify — ส่ง alert กลุ่มพยาบาล ────────

@hd_bp.route("/alerts/notify-group", methods=["POST"])
@require_role("physician", "nurse", "admin")
def notify_nurse_group():
    """Send a message to the nurse LINE group (Messaging API push to
    hd_settings.nurse_group_id); falls back to alert_recipients one by one."""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    if not message:
        return jsonify({"error": "message required"}), 400
    r = push_nurse_group(f"🚨 แจ้งเตือน HD\n{message}")
    status = 200 if r["sent"] else 400
    return jsonify({"sent": r["sent"], "method": r["method"], "count": r["count"],
                    "message": r["error"] or "ส่งแล้ว"}), status


# ── F2: Shift Handover ────────────────────────────────────

@hd_bp.route("/stats/cancellations", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def stats_cancellations():
    """📊 สถิติขาดฟอก/ยกเลิก/ส่งต่อ — รายเดือนแยกเหตุผล + Top no-show 90 วัน. NEW — อ่านอย่างเดียว."""
    import os, psycopg2
    months = max(1, min(12, _safe_int(request.args.get("months")) or 3))
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"months": [], "top_noshow": []})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        conn.autocommit = True
        out_months, top_noshow = [], []
        c90 = {"noshow": 0, "referral": 0, "postpone": 0, "other": 0, "total": 0}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT to_char(date_trunc('month', session_date), 'YYYY-MM') AS ym,
                       COALESCE(NULLIF(TRIM(cancel_reason), ''), 'ไม่ระบุเหตุผล') AS reason,
                       COUNT(*)
                FROM hd_sessions
                WHERE status = 'cancelled'
                  AND session_date >= date_trunc('month', CURRENT_DATE) - (%s || ' months')::interval
                GROUP BY 1, 2 ORDER BY 1 DESC, 3 DESC
            """, (months - 1,))
            bym = {}
            for ym, reason, n in cur.fetchall():
                bym.setdefault(ym, {"month": ym, "by_reason": {}, "total": 0})
                bym[ym]["by_reason"][reason] = n
                bym[ym]["total"] += n
            out_months = sorted(bym.values(), key=lambda x: x["month"], reverse=True)

            cur.execute("""
                SELECT COALESCE(NULLIF(TRIM(cancel_reason), ''), 'ไม่ระบุเหตุผล'), COUNT(*)
                FROM hd_sessions
                WHERE status = 'cancelled' AND session_date >= CURRENT_DATE - 90
                GROUP BY 1
            """)
            for reason, n in cur.fetchall():
                c90["total"] += n
                if "ไม่มา" in reason: c90["noshow"] += n
                elif "ส่งต่อ" in reason: c90["referral"] += n
                elif "เลื่อน" in reason: c90["postpone"] += n
                else: c90["other"] += n

            cur.execute("""
                SELECT p.hn, p.patient_name, COUNT(*) AS n, MAX(s.session_date)
                FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE s.status = 'cancelled' AND s.cancel_reason LIKE %s
                  AND s.session_date >= CURRENT_DATE - 90
                GROUP BY p.hn, p.patient_name
                ORDER BY n DESC, 4 DESC LIMIT 10
            """, ('%ไม่มา%',))
            top_noshow = [{"hn": r[0], "name": r[1], "count": r[2],
                           "last_date": r[3].isoformat() if r[3] else None} for r in cur.fetchall()]
        conn.close()
        return jsonify({"months": out_months, "top_noshow": top_noshow, "last_90d": c90})
    except Exception as e:
        logger.error("stats_cancellations: %s", e)
        return jsonify({"months": [], "top_noshow": [], "error": str(e)})


@hd_bp.route("/sessions/<int:session_id>/restore", methods=["POST"])
@require_role("physician", "nurse", "admin")
def restore_cancelled_session(session_id):
    """↩️ เอากลับ: คืน session ที่ยกเลิกไปเป็น 'พร้อมฟอก' (กรณีกดยกเลิกผิดคน). NEW — ไม่แตะของเดิม."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            try:
                cur.execute("""UPDATE hd_sessions
                               SET status = 'scheduled', ended_at = NULL, cancel_reason = NULL
                               WHERE id = %s AND status = 'cancelled'
                               RETURNING bed_number""", (session_id,))
                row = cur.fetchone()
            except Exception as e:
                conn.rollback()
                conn.close()
                if "ux_hd_sessions_bed_active" in str(e) or "duplicate key" in str(e).lower():
                    return jsonify({"error": "เตียงนี้มีคนไข้รายอื่นลงไปแล้ว — เอากลับไม่ได้ ให้ลงเตียงใหม่"}), 409
                raise
            if not row:
                conn.close()
                return jsonify({"error": "ไม่พบรายการที่ยกเลิก หรือถูกเอากลับไปแล้ว"}), 404
            conn.commit()
        conn.close()
        log_action(user_id=g.get("hd_user_id"), action="session.restore", resource_type="session",
                   resource_id=session_id, new_values={"bed": row[0]}, ip_address=request.remote_addr or "")
        return jsonify({"ok": True, "bed_number": row[0], "message": f"คืนสถานะสำเร็จ — เตียง {row[0]} พร้อมฟอก"})
    except Exception as e:
        logger.error("restore session: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/shift/handover", methods=["POST"])
@require_role("physician", "nurse", "admin")
def shift_handover():
    """สร้างสรุปส่งต่อเวร + ส่ง LINE (optional)."""
    import os
    from .shift_handover import generate_handover_summary
    data = request.get_json(silent=True) or {}
    shift = data.get("shift", "morning")
    send_line = data.get("send_line", False)

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    summary = generate_handover_summary(db_url, shift)

    if send_line and summary.get("line_message"):
        recipients = get_setting_value("alert_recipients", "")
        push_fn = current_app.config.get("LINE_PUSH_FN")
        if push_fn and recipients:
            for uid in recipients.split(","):
                uid = uid.strip()
                if uid:
                    try:
                        push_fn(uid, summary["line_message"])
                    except Exception as e:
                        logger.warning("handover LINE push failed: %s", e)
            summary["line_sent"] = True

    return jsonify(summary)


# ── Patient Edit (update info) ────────────────────────────

@hd_bp.route("/patients/<hn>", methods=["PUT", "PATCH"])
@require_role("physician", "nurse", "admin")
def update_patient(hn):
    """Update patient info (schedule, VA, dry weight, ESA, etc.)."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No data"}), 400

    allowed = {"patient_name", "schedule_days", "vascular_access", "dry_weight",
               "esa_drug", "esa_dose", "primary_diagnosis", "needs_review", "is_active", "default_shift",
               "idwg_limit_pct"}
    if "idwg_limit_pct" in data and data["idwg_limit_pct"] not in ("", None):
        try:
            if not (1.0 <= float(data["idwg_limit_pct"]) <= 10.0):
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "เกณฑ์น้ำหนักขึ้นต้องอยู่ระหว่าง 1–10 % ของ DW"}), 400
    if "default_shift" in data and data["default_shift"] not in ("", None, "morning", "afternoon", "evening"):
        return jsonify({"error": "default_shift ต้องเป็น morning / afternoon / evening"}), 400
    updates = []
    params = []
    for key in allowed:
        if key in data:
            if key in ("dry_weight", "idwg_limit_pct"):
                updates.append(f"{key} = %s")
                params.append(float(data[key]) if data[key] not in ("", None) else None)
            elif key in ("needs_review", "is_active"):
                updates.append(f"{key} = %s")
                params.append(bool(data[key]))
            else:
                updates.append(f"{key} = %s")
                params.append(data[key])

    if not updates:
        return jsonify({"error": "No valid fields to update"}), 400

    # If schedule_days is being set, clear needs_review
    if "schedule_days" in data and data["schedule_days"]:
        if "needs_review" not in data:
            updates.append("needs_review = FALSE")

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            sql = f"UPDATE hd_patient_mapping SET {', '.join(updates)} WHERE hn = %s RETURNING id"
            params.append(hn)
            cur.execute(sql, params)
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": f"ไม่พบ HN {hn}"}), 404
            if "dry_weight" in data:
                # keep the active prescription in step and retire DW proposals made against the old value
                try:
                    cur.execute("UPDATE hd_prescriptions SET dry_weight_target = %s, updated_at = NOW() WHERE patient_id = %s AND is_active = TRUE",
                                (float(data["dry_weight"]) if data["dry_weight"] else None, row[0]))
                    from .dw_autopilot import CREATE_SQL as _DW_SQL, supersede_stale
                    cur.execute(_DW_SQL)
                    supersede_stale(cur)
                except Exception as e:
                    logger.warning("patient.update dw sync: %s", e)
        conn.commit()
        conn.close()

        log_action(user_id=g.get("hd_user_id"), action="patient.update",
                   resource_type="patient", resource_id=hn,
                   ip_address=request.remote_addr or "")
        return jsonify({"success": True, "hn": hn, "updated": list(data.keys())})
    except Exception as e:
        logger.error("update_patient error: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/patients/needs-review", methods=["GET"])
@require_role("physician", "nurse", "admin")
def patients_needs_review():
    """List patients that need data review (incomplete info)."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"patients": []})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT hn, patient_name, schedule_days, vascular_access,
                       dry_weight, esa_drug, registered_by, needs_review
                FROM hd_patient_mapping
                WHERE is_active = TRUE
                  AND (needs_review = TRUE
                       OR schedule_days IS NULL OR schedule_days = ''
                       OR vascular_access IS NULL OR vascular_access = '')
                ORDER BY hn
            """)
            rows = cur.fetchall()
        conn.close()

        patients = []
        for r in rows:
            missing = []
            if not r[2]:
                missing.append("ตารางฟอก")
            if not r[3]:
                missing.append("Vascular Access")
            if not r[4]:
                missing.append("Dry Weight")
            if not r[5]:
                missing.append("ESA")
            patients.append({
                "hn": r[0], "name": r[1],
                "schedule_days": r[2], "vascular_access": r[3],
                "dry_weight": float(r[4]) if r[4] else None,
                "esa_drug": r[5],
                "registered_by": r[6],
                "missing_fields": missing,
            })
        return jsonify({"patients": patients, "total": len(patients)})
    except Exception as e:
        logger.error("patients_needs_review error: %s", e)
        return jsonify({"patients": [], "error": str(e)})


# ── ESA History for Patient ───────────────────────────────

@hd_bp.route("/patients/<hn>/esa-history", methods=["GET"])
@require_role("physician", "nurse", "admin")
def patient_esa_history(hn):
    """Get last 10 ESA injection + Hb/Hct records for a patient."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"records": [], "prescribed": {}})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Get prescribed ESA info
            cur.execute(
                "SELECT esa_drug, esa_dose FROM hd_patient_mapping WHERE hn = %s",
                (hn,)
            )
            prow = cur.fetchone()
            prescribed = {"esa_drug": prow[0] if prow else None,
                          "esa_dose": prow[1] if prow else None}

            # Get last 10 sessions with ESA/lab data
            cur.execute("""
                SELECT s.session_date, s.esa_given, s.esa_drug_given,
                       s.esa_dose_given, s.esa_route, s.esa_skip_reason,
                       s.hb_value, s.hct_value, s.shift
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE p.hn = %s AND s.status = 'completed'
                ORDER BY s.session_date DESC, s.started_at DESC
                LIMIT 10
            """, (hn,))
            rows = cur.fetchall()

            # Hb/Hct ล่าสุดจากระบบแล็บรวมทุกแหล่ง (hd_labs VIEW) — สำหรับ auto-fill (additive, ล้มได้ไม่กระทบของเดิม)
            latest_lab = None
            try:
                cur.execute("""SELECT l.lab_date, l.hb, l.hct
                               FROM hd_labs l JOIN hd_patient_mapping p ON p.id = l.patient_id
                               WHERE p.hn = %s AND (l.hb IS NOT NULL OR l.hct IS NOT NULL)
                               ORDER BY l.lab_date DESC NULLS LAST LIMIT 1""", (hn,))
                lr = cur.fetchone()
                if lr:
                    latest_lab = {"date": lr[0].isoformat() if lr[0] else None,
                                  "hb": float(lr[1]) if lr[1] is not None else None,
                                  "hct": float(lr[2]) if lr[2] is not None else None}
            except Exception as _ll_err:
                logger.warning("esa-history latest_lab: %s", _ll_err)

            # ข้อเสนอปรับ dose ที่ค้างอยู่จาก ESA Autopilot (additive — ล้มได้ไม่กระทบของเดิม)
            pending_proposal = None
            try:
                cur.execute("""SELECT e.action, e.proposed_dose, e.current_dose, e.rationale, e.hb, e.hb_date
                               FROM hd_esa_proposals e JOIN hd_patient_mapping p ON p.id = e.patient_id
                               WHERE p.hn = %s AND e.status = 'pending'
                               ORDER BY e.created_at DESC LIMIT 1""", (hn,))
                pp = cur.fetchone()
                if pp:
                    pending_proposal = {"action": pp[0], "proposed_dose": pp[1], "current_dose": pp[2],
                                        "rationale": pp[3],
                                        "hb": float(pp[4]) if pp[4] is not None else None,
                                        "hb_date": pp[5].isoformat() if pp[5] else None}
            except Exception as _pp_err:
                logger.warning("esa-history pending_proposal: %s", _pp_err)
        conn.close()

        records = []
        for r in rows:
            records.append({
                "date": str(r[0]) if r[0] else None,
                "esa_given": r[1],
                "esa_drug": r[2], "esa_dose": r[3],
                "esa_route": r[4], "esa_skip_reason": r[5],
                "hb": float(r[6]) if r[6] else None,
                "hct": float(r[7]) if r[7] else None,
                "shift": r[8],
            })
        return jsonify({"records": records, "prescribed": prescribed, "latest_lab": latest_lab,
                        "pending_proposal": pending_proposal})
    except Exception as e:
        logger.error("patient_esa_history error: %s", e)
        return jsonify({"records": [], "prescribed": {}, "error": str(e)})


# ══════════════════════════════════════════════════════════
# HD FLOWSHEET — Full HD Monitoring Sheet
# ══════════════════════════════════════════════════════════

def flowsheet_entries(conn, session_id):
    """Sheet rows + vitals/UF recorded elsewhere, merged into 30-min slots.

    Shared by GET /flowsheet and the daily-record PDF. Returns (entries, srow)
    where srow = (uf_goal, pre_hd_note, started_at, patient_name, hn, va).
    """
    if True:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, slot_time, recorded_at,
                       sbp, dbp, pr, rr, temp, spo2,
                       bfr, vp, tmp, ap, conductivity, dialysate_temp,
                       ufr, total_uf, remark
                FROM hd_flowsheet
                WHERE session_id = %s
                ORDER BY slot_time, recorded_at
            """, (session_id,))
            rows = cur.fetchall()
            # Get session info
            cur.execute("""
                SELECT s.uf_goal, s.pre_hd_note, s.started_at,
                       p.patient_name, p.hn, p.vascular_access
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE s.id = %s
            """, (session_id,))
            srow = cur.fetchone()
            # Everything recorded elsewhere for this session (session page,
            # quick "วัด BP" on the dashboard, UF button) — merged into slots below
            cur.execute("""SELECT sbp, dbp, heart_rate, temperature, spo2, recorded_at
                           FROM hd_vital_signs WHERE session_id = %s AND recorded_at IS NOT NULL
                           ORDER BY recorded_at""", (session_id,))
            vrows = cur.fetchall()
            try:
                cur.execute("""SELECT uf_removed, uf_rate, recorded_at FROM hd_uf_records
                               WHERE session_id = %s AND recorded_at IS NOT NULL ORDER BY recorded_at""",
                            (session_id,))
                urows = cur.fetchall()
            except Exception:
                conn.rollback()
                urows = []

        entries = []
        for r in rows:
            entries.append({
                "id": r[0], "slot_time": r[1],
                "recorded_at": (r[2].isoformat() + "Z") if r[2] else None,
                "sbp": r[3], "dbp": r[4], "pr": r[5], "rr": r[6],
                "temp": float(r[7]) if r[7] else None,
                "spo2": r[8],
                "bfr": r[9], "vp": r[10], "tmp": r[11], "ap": r[12],
                "conductivity": float(r[13]) if r[13] else None,
                "dialysate_temp": float(r[14]) if r[14] else None,
                "ufr": r[15], "total_uf": r[16],
                "remark": r[17],
            })

        # ── Merge: vitals / UF recorded on the session page land in the sheet ──
        started = srow[2] if srow else None
        if started:
            from datetime import timedelta
            by_slot = {e["slot_time"]: e for e in entries}

            def slot_for(ts):
                """Same 30-min grid the sheet page draws: start rounded (<15 → :00, else :30)."""
                thai_start = started + timedelta(hours=7)
                base = thai_start.replace(second=0, microsecond=0,
                                          minute=0 if thai_start.minute < 15 else 30)
                delta = (ts + timedelta(hours=7)) - base
                idx = int(round(delta.total_seconds() / 1800.0))
                idx = max(0, min(idx, 11))
                t = base + timedelta(minutes=30 * idx)
                return t.strftime("%H:%M")

            for sbp, dbp, hr, temp, spo2, rec in vrows:
                st = slot_for(rec)
                e = by_slot.get(st)
                if e is None:
                    e = {"id": None, "slot_time": st, "recorded_at": rec.isoformat() + "Z",
                         "sbp": None, "dbp": None, "pr": None, "rr": None, "temp": None, "spo2": None,
                         "bfr": None, "vp": None, "tmp": None, "ap": None, "conductivity": None,
                         "dialysate_temp": None, "ufr": None, "total_uf": None, "remark": None}
                    by_slot[st] = e
                    entries.append(e)
                # latest reading in the slot wins for fields the sheet row doesn't have
                if sbp is not None:
                    e["sbp"], e["dbp"] = sbp, dbp
                if hr is not None:
                    e["pr"] = hr
                if temp is not None and e.get("temp") is None:
                    e["temp"] = float(temp)
                if spo2 is not None and e.get("spo2") is None:
                    e["spo2"] = spo2
                e["synced"] = True
            for uf_removed, uf_rate, rec in urows:
                st = slot_for(rec)
                e = by_slot.get(st)
                if e is None:
                    e = {"id": None, "slot_time": st, "recorded_at": rec.isoformat() + "Z",
                         "sbp": None, "dbp": None, "pr": None, "rr": None, "temp": None, "spo2": None,
                         "bfr": None, "vp": None, "tmp": None, "ap": None, "conductivity": None,
                         "dialysate_temp": None, "ufr": None, "total_uf": None, "remark": None}
                    by_slot[st] = e
                    entries.append(e)
                if uf_removed is not None and (e.get("total_uf") is None or uf_removed > (e.get("total_uf") or 0)):
                    e["total_uf"] = int(uf_removed)
                if uf_rate is not None and e.get("ufr") is None:
                    e["ufr"] = int(uf_rate)
                e["synced"] = True
            entries.sort(key=lambda e: e["slot_time"])
        return entries, srow


@hd_bp.route("/sessions/<int:session_id>/flowsheet", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def get_flowsheet(session_id):
    """Get all flowsheet entries for a session."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"rows": []})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        try:
            entries, srow = flowsheet_entries(conn, session_id)
        finally:
            conn.close()

        return jsonify({
            "rows": entries,
            "session": {
                "uf_goal": int(srow[0]) if srow and srow[0] else 2000,
                "pre_hd_note": srow[1] if srow else None,
                "started_at": srow[2].isoformat() if srow and srow[2] else None,
                "patient_name": srow[3] if srow else None,
                "hn": srow[4] if srow else None,
                "va": srow[5] if srow else None,
            } if srow else {},
        })
    except Exception as e:
        logger.error("get_flowsheet error: %s", e)
        return jsonify({"rows": [], "error": str(e)})


@hd_bp.route("/sessions/<int:session_id>/flowsheet", methods=["POST"])
@require_role("physician", "nurse", "admin")
def save_flowsheet_row(session_id):
    """Save one flowsheet row (insert or update by slot_time)."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503

    data = request.get_json(silent=True) or {}
    slot_time = data.get("slot_time", "")
    if not slot_time:
        return jsonify({"error": "slot_time required"}), 400

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Check if row exists for this slot_time
            cur.execute("""
                SELECT id FROM hd_flowsheet
                WHERE session_id = %s AND slot_time = %s
            """, (session_id, slot_time))
            existing = cur.fetchone()

            params = {
                "sbp": data.get("sbp"), "dbp": data.get("dbp"),
                "pr": data.get("pr"), "rr": data.get("rr"),
                "temp": data.get("temp"), "spo2": data.get("spo2"),
                "bfr": data.get("bfr"), "vp": data.get("vp"),
                "tmp": data.get("tmp"), "ap": data.get("ap"),
                "conductivity": data.get("conductivity"),
                "dialysate_temp": data.get("dialysate_temp"),
                "ufr": data.get("ufr"), "total_uf": data.get("total_uf"),
                "remark": data.get("remark"),
            }

            if existing:
                # Update — only non-null fields
                sets = []
                vals = []
                for k, v in params.items():
                    if v is not None:
                        sets.append(f"{k} = %s")
                        vals.append(v)
                if sets:
                    sets.append("recorded_at = NOW()")
                    vals.append(existing[0])
                    cur.execute(
                        f"UPDATE hd_flowsheet SET {', '.join(sets)} WHERE id = %s",
                        vals
                    )
                row_id = existing[0]
            else:
                # Insert
                cur.execute("""
                    INSERT INTO hd_flowsheet
                        (session_id, slot_time, sbp, dbp, pr, rr, temp, spo2,
                         bfr, vp, tmp, ap, conductivity, dialysate_temp,
                         ufr, total_uf, remark, nurse_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (
                    session_id, slot_time,
                    params["sbp"], params["dbp"], params["pr"], params["rr"],
                    params["temp"], params["spo2"],
                    params["bfr"], params["vp"], params["tmp"], params["ap"],
                    params["conductivity"], params["dialysate_temp"],
                    params["ufr"], params["total_uf"],
                    params["remark"], g.get("hd_user_id"),
                ))
                row_id = cur.fetchone()[0]

            # Also update session actual_uf to latest total_uf
            if params["total_uf"]:
                cur.execute(
                    "UPDATE hd_sessions SET actual_uf = %s WHERE id = %s",
                    (params["total_uf"], session_id)
                )

            # Mirror to hd_vital_signs so the session page timeline + dashboard see it
            alert = None
            if params["sbp"]:
                cur.execute("SAVEPOINT sp_sheet_vital")
                try:
                    cur.execute("""
                        INSERT INTO hd_vital_signs
                            (session_id, phase, sbp, dbp, heart_rate, temperature, spo2, recorded_at)
                        VALUES (%s, 'intra', %s, %s, %s, %s, %s, NOW())
                    """, (session_id, params["sbp"], params["dbp"], params["pr"],
                          params["temp"], params["spo2"]))
                    cur.execute("RELEASE SAVEPOINT sp_sheet_vital")
                except Exception as ve:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_sheet_vital")
                    logger.warning("sheet→vital mirror failed: %s", ve)
                # Same rules + LINE as the session page / quick BP
                new_alerts = _process_vital_alerts(cur, session_id, sbp=params["sbp"], dbp=params["dbp"],
                                                   hr=params["pr"], spo2=params["spo2"], temp=params["temp"])
                if new_alerts:
                    worst = sorted(new_alerts, key=lambda a: 0 if a["severity"] == "critical" else 1)[0]
                    alert = {"severity": worst["severity"], "title": worst["title"]}

            # Mirror UF to hd_uf_records so the session page UF progress updates
            # 16 พ.ย. 2569: ใช้ค่ารวมของแถวช่องเวลานั้น (ไม่ใช่เฉพาะช่องที่เพิ่งพิมพ์) และ 1 ช่องเวลา = 1 record
            # เดิมบันทึกแค่ UFR → แทรก UF removed = 0, แก้ช่องเดิมซ้ำ → แทรกเพิ่ม → กราฟขึ้นลง + เตือนอัตราเกินผิด
            if params["total_uf"] is not None or params["ufr"] is not None:
                cur.execute("SAVEPOINT sp_sheet_uf")
                try:
                    cur.execute("SELECT total_uf, ufr FROM hd_flowsheet WHERE id = %s", (row_id,))
                    fr = cur.fetchone()
                    tot = fr[0] if fr else None
                    if tot is not None and float(tot) > 0:
                        removed = int(float(tot))
                        rate = int(float(fr[1])) if fr[1] is not None else 0
                        cur.execute("SELECT uf_goal FROM hd_sessions WHERE id = %s", (session_id,))
                        gr = cur.fetchone()
                        goal = int(gr[0]) if gr and gr[0] else 0
                        rem_ml = max(goal - removed, 0) if goal else 0
                        cur.execute("""SELECT id FROM hd_uf_records WHERE session_id = %s AND slot_time = %s
                                       AND notes = 'sheet' ORDER BY id DESC LIMIT 1""", (session_id, slot_time))
                        ex = cur.fetchone()
                        if ex:
                            cur.execute("""UPDATE hd_uf_records SET uf_removed = %s, uf_rate = %s, uf_remaining = %s,
                                           recorded_at = NOW() WHERE id = %s""", (removed, rate, rem_ml, ex[0]))
                        else:
                            cur.execute("""
                                INSERT INTO hd_uf_records (session_id, slot_time, uf_removed, uf_rate, uf_remaining,
                                                           notes, recorded_at)
                                VALUES (%s, %s, %s, %s, %s, 'sheet', NOW())
                            """, (session_id, slot_time, removed, rate, rem_ml))
                    cur.execute("RELEASE SAVEPOINT sp_sheet_uf")
                except Exception as ue:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_sheet_uf")
                    logger.warning("sheet→uf mirror failed: %s", ue)

        conn.commit()
        conn.close()

        # Return the previous row for auto-fill
        prev_row = None
        try:
            conn2 = psycopg2.connect(db_url, connect_timeout=5)
            with conn2.cursor() as cur2:
                cur2.execute("""
                    SELECT bfr, vp, tmp, ap, conductivity, dialysate_temp, ufr
                    FROM hd_flowsheet
                    WHERE session_id = %s AND slot_time < %s
                    ORDER BY slot_time DESC LIMIT 1
                """, (session_id, slot_time))
                pr = cur2.fetchone()
                if pr:
                    prev_row = {"bfr": pr[0], "vp": pr[1], "tmp": pr[2],
                                "ap": pr[3], "conductivity": float(pr[4]) if pr[4] else None,
                                "dialysate_temp": float(pr[5]) if pr[5] else None,
                                "ufr": pr[6]}
            conn2.close()
        except Exception:
            pass

        log_action(user_id=g.get("hd_user_id"), action="flowsheet.save",
                   resource_type="flowsheet", resource_id=row_id,
                   ip_address=request.remote_addr or "")

        return jsonify({
            "id": row_id, "slot_time": slot_time,
            "saved": True, "alert": alert,
            "prev_row": prev_row,
        }), 201
    except Exception as e:
        logger.error("save_flowsheet error: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/sessions/<int:session_id>/pre-hd-note", methods=["PUT"])
@require_role("physician", "nurse", "admin")
def save_pre_hd_note(session_id):
    """Save Pre-HD Note."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    data = request.get_json(silent=True) or {}
    note = data.get("note", "")
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("UPDATE hd_sessions SET pre_hd_note = %s WHERE id = %s",
                        (note, session_id))
        conn.commit()
        conn.close()
        return jsonify({"saved": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── F3: Kt/V Calculator ──────────────────────────────────

@hd_bp.route("/calculator/kt-v", methods=["POST"])
@require_role("physician", "nurse", "admin")
def calculate_kt_v_endpoint():
    """คำนวณ Kt/V + URR จาก Pre/Post BUN + weights."""
    from .kt_v_calculator import calculate_kt_v
    data = request.get_json(silent=True) or {}
    result = calculate_kt_v(
        pre_bun=float(data.get("pre_bun", 0)),
        post_bun=float(data.get("post_bun", 0)),
        duration_hours=float(data.get("duration_hours", 4)),
        pre_weight=float(data.get("pre_weight", 0)),
        post_weight=float(data.get("post_weight", 0)),
    )
    return jsonify(result)


# ── F4: EPO Response — redirects to full epo_dashboard ───

@hd_bp.route("/epo/response/<hn>", methods=["GET"])
@require_role("physician", "admin")
def epo_response(hn):
    """Redirect to full EPO dashboard (F10)."""
    return redirect(url_for("hd.epo_dashboard", hn=hn))


# ── F5: QR Check-in ──────────────────────────────────────

@hd_bp.route("/qr/beds", methods=["GET"])
def qr_all_beds():
    """สร้างหน้าพิมพ์ QR Code ทุกเตียง — no auth needed (page is for printing)."""
    from .qr_checkin import generate_all_bed_qr_page
    html = generate_all_bed_qr_page(HdConfig.TOTAL_BEDS)
    return make_response(html)


@hd_bp.route("/qr/bed/<int:bed_number>", methods=["GET"])
@require_role("physician", "nurse", "admin")
def qr_single_bed(bed_number):
    """สร้าง QR data สำหรับเตียงเดียว."""
    from .qr_checkin import generate_bed_qr_data
    return jsonify(generate_bed_qr_data(bed_number))


@hd_bp.route("/qr/checkin-data/<int:bed_number>", methods=["GET"])
@require_role("physician", "nurse", "admin")
def qr_checkin_data(bed_number):
    """QR Check-in V2: ดึงข้อมูลคนไข้ที่นัดเตียงนี้วันนี้ + ประวัติฟอกครั้งก่อน."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    today = _thai_today()
    shift = request.args.get("shift", "")
    if not shift:
        h = _thai_now().hour
        shift = "morning" if h < 12 else ("afternoon" if h < 18 else "evening")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        result = {"bed": bed_number, "today": today.isoformat(), "shift": shift, "patient": None, "last_session": None}
        with conn.cursor() as cur:
            # 1. Find scheduled/in_progress session for this bed today
            cur.execute("""SELECT s.id, s.status, s.patient_id, p.hn, p.patient_name,
                                  p.vascular_access, p.dry_weight, p.date_of_birth, p.schedule_days,
                                  p.gender, p.allergies
                           FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                           WHERE s.bed_number = %s AND s.session_date = %s
                             AND (s.shift = %s OR s.shift IS NULL)
                             AND s.status IN ('scheduled', 'in_progress')
                           ORDER BY CASE s.status WHEN 'in_progress' THEN 0 ELSE 1 END
                           LIMIT 1""", (bed_number, today.isoformat(), shift))
            row = cur.fetchone()

            if not row:
                # Try any shift
                cur.execute("""SELECT s.id, s.status, s.patient_id, p.hn, p.patient_name,
                                      p.vascular_access, p.dry_weight, p.date_of_birth, p.schedule_days,
                                      p.gender, p.allergies
                               FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                               WHERE s.bed_number = %s AND s.session_date = %s
                                 AND s.status IN ('scheduled', 'in_progress')
                               LIMIT 1""", (bed_number, today.isoformat()))
                row = cur.fetchone()

            if row:
                sid, status, pid, hn, name = row[0], row[1], row[2], row[3], row[4]
                age = None
                if row[7] and today:
                    dob = row[7]
                    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

                result["patient"] = {
                    "session_id": sid, "status": status, "hn": hn, "name": name,
                    "va": row[5], "dry_weight": float(row[6]) if row[6] else None,
                    "age": age, "gender": row[9], "schedule": row[8], "allergies": row[10],
                }

                # 2. Last completed session for this patient
                cur.execute("""SELECT s.session_date, s.pre_weight, s.post_weight, s.actual_uf,
                                      s.hb_value, s.kt_v, s.bed_number,
                                      (SELECT sbp||'/'||dbp FROM hd_vital_signs
                                       WHERE session_id=s.id AND phase='pre' AND sbp>0
                                       ORDER BY recorded_at LIMIT 1) as pre_bp
                               FROM hd_sessions s
                               WHERE s.patient_id = %s AND s.status = 'completed'
                               ORDER BY s.session_date DESC, s.id DESC LIMIT 1""", (pid,))
                last = cur.fetchone()
                if last:
                    result["last_session"] = {
                        "date": last[0].isoformat() if last[0] else None,
                        "pre_weight": float(last[1]) if last[1] else None,
                        "post_weight": float(last[2]) if last[2] else None,
                        "actual_uf": last[3], "hb": float(last[4]) if last[4] else None,
                        "kt_v": float(last[5]) if last[5] else None,
                        "bed": last[6], "pre_bp": last[7],
                    }

                # 3. Active dialyzer
                cur.execute("""SELECT dialyzer_type, reuse_count, max_reuse
                               FROM hd_dialyzer_tracking
                               WHERE patient_id = %s AND status = 'active'
                               ORDER BY id DESC LIMIT 1""", (pid,))
                dz = cur.fetchone()
                if dz:
                    result["dialyzer"] = {"type": dz[0], "reuse": dz[1], "max": dz[2]}

                # 4. Last heparin
                cur.execute("""SELECT a.protocol, a.loading_dose, a.maintenance_dose
                               FROM hd_anticoagulation a
                               JOIN hd_sessions s ON s.id = a.session_id
                               WHERE s.patient_id = %s ORDER BY a.recorded_at DESC LIMIT 1""", (pid,))
                hep = cur.fetchone()
                if hep:
                    result["heparin"] = {"protocol": hep[0], "loading": hep[1], "maintenance": hep[2]}

                # 5. Machine on this bed
                cur.execute("""SELECT brand, model, serial_number
                               FROM hd_equipment WHERE bed_number = %s
                                 AND equipment_type = 'dialysis_machine' AND status = 'active'
                               LIMIT 1""", (bed_number,))
                mc = cur.fetchone()
                if mc:
                    result["machine"] = {"brand": mc[0], "model": mc[1], "serial": mc[2]}

        conn.close()
        return jsonify(result)
    except Exception as e:
        logger.error("checkin-data error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── F8: Monthly Report ───────────────────────────────────

@hd_bp.route("/report/monthly-summary", methods=["GET"])
@require_role("physician", "admin")
def monthly_summary_report():
    """สร้างรายงานประจำเดือน."""
    import os
    from datetime import timedelta
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503

    month = request.args.get("month")  # YYYY-MM format
    if not month:
        # Default: last month
        now = datetime.now(timezone.utc)
        first_of_month = now.replace(day=1)
        last_month = first_of_month - timedelta(days=1)
        month = last_month.strftime("%Y-%m")

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        report = {"month": month}

        # Total sessions
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*),
                       COUNT(CASE WHEN status='completed' THEN 1 END),
                       COUNT(CASE WHEN status='cancelled' THEN 1 END),
                       AVG(actual_uf) FILTER (WHERE actual_uf>0),
                       COUNT(DISTINCT patient_id)
                FROM hd_sessions
                WHERE TO_CHAR(started_at, 'YYYY-MM') = %s
            """, (month,))
            r = cur.fetchone()
            report["sessions"] = {
                "total": r[0], "completed": r[1], "cancelled": r[2],
                "avg_uf": round(r[3] or 0), "unique_patients": r[4],
            }

        # Alert stats
        with conn.cursor() as cur:
            cur.execute("""
                SELECT severity, COUNT(*)
                FROM hd_clinical_alerts
                WHERE TO_CHAR(created_at, 'YYYY-MM') = %s
                GROUP BY severity
            """, (month,))
            alerts = {}
            for r in cur.fetchall():
                alerts[r[0]] = r[1]
            report["alerts"] = alerts

        # Vital signs summary
        with conn.cursor() as cur:
            cur.execute("""
                SELECT AVG(sbp)::int, AVG(dbp)::int, AVG(heart_rate)::int,
                       MIN(sbp), MAX(sbp), COUNT(*)
                FROM hd_vital_signs v
                JOIN hd_sessions s ON s.id = v.session_id
                WHERE TO_CHAR(s.started_at, 'YYYY-MM') = %s AND sbp > 0
            """, (month,))
            r = cur.fetchone()
            report["vitals"] = {
                "avg_sbp": r[0], "avg_dbp": r[1], "avg_hr": r[2],
                "min_sbp": r[3], "max_sbp": r[4], "total_readings": r[5],
            }

        # IDH rate (SBP < 90 during session)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT v.session_id)
                FROM hd_vital_signs v
                JOIN hd_sessions s ON s.id = v.session_id
                WHERE TO_CHAR(s.started_at, 'YYYY-MM') = %s
                  AND v.phase = 'intra' AND v.sbp < 90
            """, (month,))
            idh_sessions = cur.fetchone()[0]
            total = report["sessions"]["total"] or 1
            report["idh_rate"] = {
                "sessions_with_idh": idh_sessions,
                "rate_pct": round(idh_sessions / total * 100, 1),
            }

        conn.close()

        # LINE message format
        s = report["sessions"]
        a = report["alerts"]
        v = report["vitals"]
        total_alerts = sum(a.values())
        report["line_message"] = (
            f"📊 รายงานประจำเดือน {month}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏥 รอบฟอก: {s['total']} ({s['completed']} สำเร็จ)\n"
            f"👥 ผู้ป่วย: {s['unique_patients']} คน\n"
            f"💧 UF เฉลี่ย: {s['avg_uf']} mL\n"
            f"❤️ BP เฉลี่ย: {v['avg_sbp']}/{v['avg_dbp']} mmHg\n"
            f"🚨 Alerts: {total_alerts}"
            f" (🔴{a.get('critical',0)} ⚠️{a.get('warning',0)})\n"
            f"📉 IDH rate: {report['idh_rate']['rate_pct']}%"
            f" ({idh_sessions} รอบ)\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"HD Smart Monitoring — คลินิกระนอง รักษ์ไต"
        )

        return jsonify(report)
    except Exception as e:
        logger.error("monthly_summary error: %s", e)
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════
# F15: Medication Orders
# ══════════════════════════════════════════════════════════

@hd_bp.route("/patients/<hn>/medications", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_patient_medications(hn):
    """Get standing medication orders for a patient."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"orders": []})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.id, o.drug_name, o.drug_category, o.dose, o.route,
                       o.frequency, o.is_standing, o.is_active, o.notes, o.ordered_at
                FROM hd_medication_orders o
                JOIN hd_patient_mapping p ON p.id = o.patient_id
                WHERE p.hn = %s AND o.is_active = TRUE
                ORDER BY o.drug_category, o.drug_name
            """, (hn,))
            rows = cur.fetchall()
        conn.close()
        orders = [{"id": r[0], "drug": r[1], "category": r[2], "dose": r[3],
                   "route": r[4], "frequency": r[5], "standing": r[6],
                   "active": r[7], "notes": r[8],
                   "ordered_at": r[9].isoformat() if r[9] else None} for r in rows]
        return jsonify({"orders": orders})
    except Exception as e:
        logger.error("get_medications error: %s", e)
        return jsonify({"orders": [], "error": str(e)})


@hd_bp.route("/patients/<hn>/medications", methods=["POST"])
@require_role("physician")
def create_medication_order(hn):
    """Create a new medication order."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    data = request.get_json(silent=True) or {}
    drug = data.get("drug_name", "").strip()
    if not drug:
        return jsonify({"error": "ต้องระบุชื่อยา"}), 400
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM hd_patient_mapping WHERE hn = %s", (hn,))
            pr = cur.fetchone()
            if not pr:
                conn.close()
                return jsonify({"error": f"ไม่พบ HN {hn}"}), 404
            cur.execute("""
                INSERT INTO hd_medication_orders
                    (patient_id, drug_name, drug_category, dose, route, frequency,
                     is_standing, ordered_by, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (pr[0], drug, data.get("category", "other"),
                  data.get("dose"), data.get("route", "IV"),
                  data.get("frequency"), data.get("standing", False),
                  g.get("hd_user_id"), data.get("notes")))
            oid = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": oid, "drug": drug}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/sessions/<int:sid>/medications", methods=["GET", "POST"])
@require_role("physician", "nurse", "admin")
def session_medications(sid):
    """GET: list meds given this session. POST: record a med given."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"given": []} if request.method == "GET" else {"error": "No DB"}), 200 if request.method == "GET" else 503
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        if request.method == "GET":
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, drug_name, dose_given, route, given_at, slot_time, notes
                    FROM hd_medication_given WHERE session_id = %s ORDER BY given_at
                """, (sid,))
                rows = cur.fetchall()
            conn.close()
            return jsonify({"given": [{"id": r[0], "drug": r[1], "dose": r[2],
                "route": r[3], "time": r[4].isoformat() if r[4] else None,
                "slot": r[5], "notes": r[6]} for r in rows]})
        # POST
        data = request.get_json(silent=True) or {}
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO hd_medication_given
                    (session_id, order_id, drug_name, dose_given, route, given_by, slot_time, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (sid, data.get("order_id"), data.get("drug_name", ""),
                  data.get("dose"), data.get("route", "IV"),
                  g.get("hd_user_id"), data.get("slot_time"), data.get("notes")))
            mid = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": mid, "recorded": True}), 201
    except Exception as e:
        logger.error("session_medications error: %s", e)
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════
# F16: Lab Results Viewer
# ══════════════════════════════════════════════════════════

_LAB_NORMAL_RANGES = {
    "bun": (7, 20, "mg/dL"), "creatinine": (0.6, 1.2, "mg/dL"),
    "potassium": (3.5, 5.0, "mEq/L"), "sodium": (136, 145, "mEq/L"),
    "calcium": (8.5, 10.5, "mg/dL"), "phosphate": (2.5, 4.5, "mg/dL"),
    "albumin": (3.5, 5.0, "g/dL"), "ipth": (150, 600, "pg/mL"),
    "hemoglobin": (10, 12, "g/dL"), "hematocrit": (30, 36, "%"),
    "ferritin": (200, 500, "ng/mL"), "tsat": (20, 50, "%"),
}


@hd_bp.route("/patients/hn/<hn>/labs", methods=["GET"])
@hd_bp.route("/patients/<hn>/labs", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_patient_labs(hn):
    """Get lab results history with abnormal flags.

    Two URL patterns:
      /patients/hn/<hn>/labs   — explicit HN lookup (preferred)
      /patients/<hn>/labs      — legacy (caution: numeric HN may match <int:patient_id> route)
    """
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"labs": []})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT l.id, l.lab_date, l.bun, l.creatinine, l.potassium, l.sodium,
                       l.calcium, l.phosphate, l.albumin, l.ipth,
                       l.hemoglobin, l.hematocrit, l.ferritin, l.tsat,
                       l.iron, l.tibc, l.wbc, l.platelet, l.hba1c,
                       l.uric_acid, l.ast, l.alt,
                       l.hepatitis_b, l.hepatitis_c, l.hiv,
                       l.notes, l.source, l.kt_v, l.urr
                FROM hd_lab_results l
                JOIN hd_patient_mapping p ON p.id = l.patient_id
                WHERE p.hn = %s
                ORDER BY l.lab_date DESC LIMIT 20
            """, (hn,))
            rows = cur.fetchall()
        conn.close()
        cols = ["id","date","bun","creatinine","potassium","sodium","calcium",
                "phosphate","albumin","ipth","hemoglobin","hematocrit","ferritin",
                "tsat","iron","tibc","wbc","platelet","hba1c","uric_acid",
                "ast","alt","hep_b","hep_c","hiv","notes","source","kt_v","urr"]
        labs = []
        for r in rows:
            entry = {}
            for i, c in enumerate(cols):
                v = r[i]
                if c == "date" and v:
                    v = v.isoformat()
                elif isinstance(v, (int, float)):
                    v = float(v) if not isinstance(v, int) else v
                entry[c] = v
            # Add flags
            flags = {}
            for key, (lo, hi, unit) in _LAB_NORMAL_RANGES.items():
                val = entry.get(key)
                if val is not None:
                    if val < lo:
                        flags[key] = "low"
                    elif val > hi:
                        flags[key] = "high"
            entry["flags"] = flags
            labs.append(entry)
        return jsonify({"labs": labs, "normal_ranges": _LAB_NORMAL_RANGES})
    except Exception as e:
        logger.error("get_labs error: %s", e)
        return jsonify({"labs": [], "error": str(e)})


@hd_bp.route("/patients/hn/<hn>/labs", methods=["POST"])
@hd_bp.route("/patients/<hn>/labs", methods=["POST"])
@require_role("physician", "nurse", "admin")
def save_lab_result(hn):
    """Save a lab result entry."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    data = request.get_json(silent=True) or {}
    if not data.get("lab_date"):
        return jsonify({"error": "ต้องระบุวันที่ lab"}), 400
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM hd_patient_mapping WHERE hn = %s", (hn,))
            pr = cur.fetchone()
            if not pr:
                conn.close()
                return jsonify({"error": f"ไม่พบ HN {hn}"}), 404
            lab_cols = ["bun","creatinine","potassium","sodium","calcium","phosphate",
                        "albumin","ipth","hemoglobin","hematocrit","ferritin","tsat",
                        "iron","tibc","wbc","platelet","hba1c","uric_acid","ast","alt",
                        "hepatitis_b","hepatitis_c","hiv","notes","source","kt_v","urr"]
            vals = [data.get(c) for c in lab_cols]
            placeholders = ", ".join(["%s"] * (len(lab_cols) + 2))
            col_str = ", ".join(["patient_id", "lab_date"] + lab_cols)
            cur.execute(
                f"INSERT INTO hd_lab_results ({col_str}) VALUES ({placeholders}) RETURNING id",
                [pr[0], data["lab_date"]] + vals
            )
            lid = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": lid, "saved": True}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════
# F17: Vascular Access Log
# ══════════════════════════════════════════════════════════

@hd_bp.route("/sessions/<int:sid>/va-assessment", methods=["GET", "POST"])
@require_role("physician", "nurse", "admin")
def va_assessment(sid):
    """VA assessment per session."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"assessment": None} if request.method == "GET" else {"error": "No DB"}), 200 if request.method == "GET" else 503
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        if request.method == "GET":
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT va_type, site, thrill, bruit, needle_gauge,
                           needle_site_a, needle_site_v, cannulation_ease,
                           complications, appearance, notes
                    FROM hd_va_assessments WHERE session_id = %s
                    ORDER BY assessed_at DESC LIMIT 1
                """, (sid,))
                r = cur.fetchone()
            conn.close()
            if not r:
                return jsonify({"assessment": None})
            return jsonify({"assessment": {
                "va_type": r[0], "site": r[1], "thrill": r[2], "bruit": r[3],
                "needle_gauge": r[4], "needle_a": r[5], "needle_v": r[6],
                "ease": r[7], "complications": r[8], "appearance": r[9], "notes": r[10],
            }})
        # POST
        data = request.get_json(silent=True) or {}
        with conn.cursor() as cur:
            # Get patient_id from session
            cur.execute("SELECT patient_id FROM hd_sessions WHERE id = %s", (sid,))
            sr = cur.fetchone()
            if not sr:
                conn.close()
                return jsonify({"error": "Session not found"}), 404
            cur.execute("""
                INSERT INTO hd_va_assessments
                    (session_id, patient_id, va_type, site, thrill, bruit,
                     needle_gauge, needle_site_a, needle_site_v, cannulation_ease,
                     complications, appearance, assessed_by, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (sid, sr[0], data.get("va_type"), data.get("site"),
                  data.get("thrill"), data.get("bruit"), data.get("needle_gauge"),
                  data.get("needle_a"), data.get("needle_v"), data.get("ease"),
                  data.get("complications"), data.get("appearance"),
                  g.get("hd_user_id"), data.get("notes")))
            vid = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": vid, "saved": True}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/patients/<hn>/va-history", methods=["GET"])
@require_role("physician", "nurse", "admin")
def va_history(hn):
    """VA assessment history for a patient."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"history": []})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.assessed_at, v.va_type, v.thrill, v.bruit,
                       v.complications, v.needle_gauge, v.cannulation_ease, v.notes
                FROM hd_va_assessments v
                JOIN hd_patient_mapping p ON p.id = v.patient_id
                WHERE p.hn = %s
                ORDER BY v.assessed_at DESC LIMIT 30
            """, (hn,))
            rows = cur.fetchall()
        conn.close()
        return jsonify({"history": [{"date": r[0].isoformat() if r[0] else None,
            "va_type": r[1], "thrill": r[2], "bruit": r[3],
            "complications": r[4], "gauge": r[5], "ease": r[6], "notes": r[7]}
            for r in rows]})
    except Exception as e:
        return jsonify({"history": [], "error": str(e)})


# ══════════════════════════════════════════════════════════
# F18: Anticoagulation Protocol
# ══════════════════════════════════════════════════════════

@hd_bp.route("/sessions/<int:sid>/anticoagulation", methods=["GET", "POST"])
@require_role("physician", "nurse", "admin")
def session_anticoagulation(sid):
    """Anticoagulation protocol per session."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"protocol": None} if request.method == "GET" else {"error": "No DB"}), 200 if request.method == "GET" else 503
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        if request.method == "GET":
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT protocol, loading_dose, loading_unit, maintenance_dose,
                           maintenance_unit, total_dose, heparin_free_reason,
                           act_pre, act_post, notes
                    FROM hd_anticoagulation WHERE session_id = %s
                    ORDER BY recorded_at DESC LIMIT 1
                """, (sid,))
                r = cur.fetchone()
            conn.close()
            if not r:
                return jsonify({"protocol": None})
            return jsonify({"protocol": {
                "type": r[0], "loading": r[1], "loading_unit": r[2],
                "maintenance": r[3], "maintenance_unit": r[4],
                "total": r[5], "hep_free_reason": r[6],
                "act_pre": r[7], "act_post": r[8], "notes": r[9],
            }})
        # POST
        data = request.get_json(silent=True) or {}
        protocol = data.get("protocol", "standard_heparin")
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO hd_anticoagulation
                    (session_id, protocol, loading_dose, loading_unit,
                     maintenance_dose, maintenance_unit, total_dose,
                     heparin_free_reason, act_pre, act_post, notes, recorded_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (sid, protocol, data.get("loading_dose"), data.get("loading_unit", "IU"),
                  data.get("maintenance_dose"), data.get("maintenance_unit", "IU/hr"),
                  data.get("total_dose"), data.get("hep_free_reason"),
                  data.get("act_pre"), data.get("act_post"),
                  data.get("notes"), g.get("hd_user_id")))
            aid = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": aid, "saved": True}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════
# F19: Dialyzer Reuse Tracking
# ══════════════════════════════════════════════════════════

@hd_bp.route("/patients/<hn>/dialyzer", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_dialyzer(hn):
    """Get active dialyzer for a patient."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"dialyzer": None})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.id, d.dialyzer_type, d.dialyzer_serial, d.first_use_date,
                       d.reuse_count, d.max_reuse, d.tcv_initial, d.tcv_current,
                       d.status, d.visual_pass, d.notes
                FROM hd_dialyzer_tracking d
                JOIN hd_patient_mapping p ON p.id = d.patient_id
                WHERE p.hn = %s AND d.status = 'active'
                ORDER BY d.first_use_date DESC LIMIT 1
            """, (hn,))
            r = cur.fetchone()
        conn.close()
        if not r:
            return jsonify({"dialyzer": None})
        pct_used = round(r[4] / r[5] * 100) if r[5] else 0
        return jsonify({"dialyzer": {
            "id": r[0], "type": r[1], "serial": r[2],
            "first_use": r[3].isoformat() if r[3] else None,
            "reuse_count": r[4], "max_reuse": r[5],
            "tcv_initial": float(r[6]) if r[6] else None,
            "tcv_current": float(r[7]) if r[7] else None,
            "status": r[8], "visual_pass": r[9], "notes": r[10],
            "pct_used": pct_used,
            "near_limit": r[4] >= r[5] - 2 if r[5] else False,
        }})
    except Exception as e:
        return jsonify({"dialyzer": None, "error": str(e)})


@hd_bp.route("/patients/<hn>/dialyzer", methods=["POST"])
@require_role("physician", "nurse", "admin")
def register_dialyzer(hn):
    """Register a new dialyzer or record reuse."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    data = request.get_json(silent=True) or {}
    action = data.get("action", "new")  # "new" or "reuse"
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM hd_patient_mapping WHERE hn = %s", (hn,))
            pr = cur.fetchone()
            if not pr:
                conn.close()
                return jsonify({"error": f"ไม่พบ HN {hn}"}), 404
            if action == "reuse":
                # Increment reuse count of active dialyzer
                cur.execute("""
                    UPDATE hd_dialyzer_tracking
                    SET reuse_count = reuse_count + 1,
                        tcv_current = COALESCE(%s, tcv_current),
                        visual_pass = %s,
                        last_used_session = %s,
                        last_reprocessed_at = NOW()
                    WHERE patient_id = %s AND status = 'active'
                    RETURNING id, reuse_count, max_reuse
                """, (data.get("tcv"), data.get("visual_pass", True),
                      data.get("session_id"), pr[0]))
                r = cur.fetchone()
                if not r:
                    conn.close()
                    return jsonify({"error": "ไม่พบ dialyzer active"}), 404
                alert = None
                if r[1] >= r[2]:
                    alert = f"⚠️ Dialyzer ครบ {r[2]} ครั้ง — ต้องเปลี่ยนตัวใหม่"
                    cur.execute("UPDATE hd_dialyzer_tracking SET status = 'expired' WHERE id = %s", (r[0],))
                conn.commit()
                conn.close()
                return jsonify({"reuse_count": r[1], "max": r[2], "alert": alert})
            else:
                # Deactivate old dialyzer
                cur.execute("""
                    UPDATE hd_dialyzer_tracking SET status = 'retired'
                    WHERE patient_id = %s AND status = 'active'
                """, (pr[0],))
                # Create new
                cur.execute("""
                    INSERT INTO hd_dialyzer_tracking
                        (patient_id, dialyzer_type, dialyzer_serial, first_use_date,
                         reuse_count, max_reuse, tcv_initial, tcv_current, visual_pass)
                    VALUES (%s,%s,%s,CURRENT_DATE,1,%s,%s,%s,TRUE) RETURNING id
                """, (pr[0], data.get("type", ""), data.get("serial"),
                      data.get("max_reuse", 20),
                      data.get("tcv"), data.get("tcv")))
                did = cur.fetchone()[0]
            conn.commit()
            conn.close()
            return jsonify({"id": did, "action": action}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════
# Session Shift Management
# ══════════════════════════════════════════════════════════

@hd_bp.route("/sessions/<int:session_id>/change-shift", methods=["POST"])
@require_role("physician", "nurse", "admin")
def change_session_shift(session_id):
    """Change shift of a scheduled/active session (e.g. morning → afternoon)."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    data = request.get_json(silent=True) or {}
    new_shift = data.get("shift", "")
    if new_shift not in ("morning", "afternoon", "evening"):
        return jsonify({"error": "shift ต้องเป็น morning/afternoon/evening"}), 400

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE hd_sessions SET shift = %s
                WHERE id = %s AND status IN ('scheduled', 'in_progress')
                RETURNING id, bed_number, shift
            """, (new_shift, session_id))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "ไม่พบ session หรือ session จบแล้ว"}), 404
        conn.commit()
        conn.close()
        shift_names = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}
        return jsonify({
            "session_id": row[0], "bed": row[1], "shift": row[2],
            "message": f"เปลี่ยนเป็นรอบ{shift_names.get(new_shift, new_shift)}สำเร็จ",
        })
    except Exception as e:
        logger.error("change_session_shift error: %s", e)
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════
# F9: Pre-HD LINE Reminder
# ══════════════════════════════════════════════════════════

@hd_bp.route("/reminder/pre-hd", methods=["POST"])
@require_role("physician", "nurse", "admin")
def trigger_pre_hd_reminder():
    """Manually trigger pre-HD reminders (also called by daily scheduler)."""
    import os
    from .pre_hd_reminder import send_pre_hd_reminders, format_nurse_summary

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503

    push_fn = current_app.config.get("LINE_PUSH_FN")
    registry = current_app.config.get("MASTER_REGISTRY")

    result = send_pre_hd_reminders(push_fn, db_url, registry)

    # Also send nurse group summary
    if result.get("total_scheduled", 0) > 0:
        nurse_msg = format_nurse_summary(result)
        try:
            from .blueprint import notify_nurse_group as _nng
        except ImportError:
            pass
        # Nurse group via Messaging API (group id from the 'groupid' command)
        try:
            result["nurse_group"] = push_nurse_group(nurse_msg, fallback_recipients=False)
        except Exception as e:
            logger.warning("Nurse summary group push failed: %s", e)

    log_action(user_id=g.get("hd_user_id"), action="reminder.pre_hd",
               resource_type="reminder", ip_address=request.remote_addr or "")
    return jsonify(result)


@hd_bp.route("/reminder/pre-hd/preview", methods=["GET"])
@require_role("physician", "nurse", "admin")
def preview_pre_hd_reminder():
    """Preview tomorrow's scheduled patients without sending."""
    import os
    from .pre_hd_reminder import get_tomorrow_patients

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"patients": [], "error": "No database"})

    try:
        patients, tomorrow = get_tomorrow_patients(db_url)
        return jsonify({"patients": patients, "tomorrow": str(tomorrow),
                        "total": len(patients)})
    except Exception as e:
        return jsonify({"patients": [], "error": str(e)})


# ══════════════════════════════════════════════════════════
# F10: EPO Response Dashboard
# ══════════════════════════════════════════════════════════

@hd_bp.route("/epo/dashboard/<hn>", methods=["GET"])
@require_role("physician", "nurse", "admin")
def epo_dashboard(hn):
    """Hb/Hct trend + ESA dose history for a patient (last 6 months)."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Patient info
            cur.execute("""
                SELECT patient_name, esa_drug, esa_dose, vascular_access, dry_weight
                FROM hd_patient_mapping WHERE hn = %s
            """, (hn,))
            prow = cur.fetchone()
            if not prow:
                conn.close()
                return jsonify({"error": f"ไม่พบ HN {hn}"}), 404

            # Last 6 months of sessions with Hb/Hct + ESA
            cur.execute("""
                SELECT session_date, hb_value, hct_value,
                       esa_given, esa_drug_given, esa_dose_given,
                       esa_route, esa_skip_reason, shift
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE p.hn = %s AND s.status = 'completed'
                  AND s.session_date >= CURRENT_DATE - INTERVAL '180 days'
                ORDER BY s.session_date, s.started_at
            """, (hn,))
            rows = cur.fetchall()
        conn.close()

        # Build trend data
        hb_trend = []
        esa_history = []
        for r in rows:
            entry = {"date": str(r[0]) if r[0] else None, "shift": r[8]}
            if r[1]:
                hb_trend.append({
                    "date": entry["date"], "hb": float(r[1]),
                    "hct": float(r[2]) if r[2] else None,
                })
            if r[3] is not None:
                esa_history.append({
                    "date": entry["date"], "given": r[3],
                    "drug": r[4], "dose": r[5], "route": r[6],
                    "skip_reason": r[7],
                })

        # Calculate stats
        hb_values = [h["hb"] for h in hb_trend]
        avg_hb = round(sum(hb_values) / len(hb_values), 1) if hb_values else None
        in_target = sum(1 for h in hb_values if 10 <= h <= 12)
        target_pct = round(in_target / len(hb_values) * 100) if hb_values else 0

        esa_given_count = sum(1 for e in esa_history if e["given"])
        esa_skip_count = sum(1 for e in esa_history if not e["given"])
        skip_reasons = {}
        for e in esa_history:
            if not e["given"] and e.get("skip_reason"):
                skip_reasons[e["skip_reason"]] = skip_reasons.get(e["skip_reason"], 0) + 1

        return jsonify({
            "patient": {
                "hn": hn, "name": prow[0],
                "prescribed_esa": prow[1], "prescribed_dose": prow[2],
                "va": prow[3], "dw": float(prow[4]) if prow[4] else None,
            },
            "hb_trend": hb_trend,
            "esa_history": esa_history,
            "stats": {
                "avg_hb": avg_hb,
                "hb_in_target_pct": target_pct,
                "total_sessions": len(rows),
                "esa_given": esa_given_count,
                "esa_skipped": esa_skip_count,
                "top_skip_reasons": sorted(skip_reasons.items(), key=lambda x: -x[1])[:3],
            },
        })
    except Exception as e:
        logger.error("epo_dashboard error: %s", e)
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════
# F11: Monthly ESA Report
# ══════════════════════════════════════════════════════════

@hd_bp.route("/report/esa-monthly", methods=["GET"])
@require_role("physician", "admin")
def esa_monthly_report():
    """Monthly ESA usage and Hb summary."""
    import os
    from datetime import date as dt_date
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503

    month = request.args.get("month", type=int, default=_thai_today().month)
    year = request.args.get("year", type=int, default=_thai_today().year)

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Sessions in month with ESA data
            cur.execute("""
                SELECT p.hn, p.patient_name, p.esa_drug, p.esa_dose,
                       s.esa_given, s.esa_drug_given, s.esa_dose_given,
                       s.esa_skip_reason, s.hb_value, s.hct_value,
                       s.session_date
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE s.status = 'completed'
                  AND EXTRACT(MONTH FROM s.session_date) = %s
                  AND EXTRACT(YEAR FROM s.session_date) = %s
                ORDER BY p.hn, s.session_date
            """, (month, year))
            rows = cur.fetchall()

            # Count active patients with ESA prescribed
            cur.execute("""
                SELECT COUNT(*) FROM hd_patient_mapping
                WHERE is_active = TRUE AND esa_drug IS NOT NULL AND esa_drug != ''
            """)
            total_esa_patients = cur.fetchone()[0]
        conn.close()

        # Aggregate per patient
        patient_data = {}
        all_hb = []
        skip_reasons_all = {}
        total_given = 0
        total_skipped = 0
        total_no_record = 0

        for r in rows:
            hn = r[0]
            if hn not in patient_data:
                patient_data[hn] = {
                    "hn": hn, "name": r[1],
                    "prescribed": r[2], "prescribed_dose": r[3],
                    "sessions": 0, "esa_given": 0, "esa_skipped": 0,
                    "no_record": 0, "hb_values": [], "hb_latest": None,
                }
            pd = patient_data[hn]
            pd["sessions"] += 1
            if r[4] is True:
                pd["esa_given"] += 1
                total_given += 1
            elif r[4] is False:
                pd["esa_skipped"] += 1
                total_skipped += 1
                reason = r[7] or "ไม่ระบุเหตุผล"
                skip_reasons_all[reason] = skip_reasons_all.get(reason, 0) + 1
            else:
                pd["no_record"] += 1
                total_no_record += 1
            if r[8]:
                hb = float(r[8])
                pd["hb_values"].append(hb)
                pd["hb_latest"] = hb
                all_hb.append(hb)

        # Summary
        avg_hb = round(sum(all_hb) / len(all_hb), 1) if all_hb else None
        hb_in_target = sum(1 for h in all_hb if 10 <= h <= 12)
        hb_target_pct = round(hb_in_target / len(all_hb) * 100) if all_hb else 0

        # Per-patient summary
        patients_list = []
        for pd in sorted(patient_data.values(), key=lambda x: x["hn"]):
            avg = round(sum(pd["hb_values"]) / len(pd["hb_values"]), 1) if pd["hb_values"] else None
            patients_list.append({
                "hn": pd["hn"], "name": pd["name"],
                "prescribed_esa": pd["prescribed"],
                "sessions": pd["sessions"],
                "esa_given": pd["esa_given"],
                "esa_skipped": pd["esa_skipped"],
                "no_record": pd["no_record"],
                "avg_hb": avg,
                "latest_hb": pd["hb_latest"],
            })

        top_skip = sorted(skip_reasons_all.items(), key=lambda x: -x[1])[:5]

        report = {
            "month": month, "year": year,
            "total_esa_patients": total_esa_patients,
            "patients_with_sessions": len(patient_data),
            "summary": {
                "total_sessions": len(rows),
                "esa_given": total_given,
                "esa_skipped": total_skipped,
                "no_esa_record": total_no_record,
                "avg_hb": avg_hb,
                "hb_in_target_pct": hb_target_pct,
                "top_skip_reasons": top_skip,
            },
            "patients": patients_list,
        }

        # Generate LINE message if requested
        if request.args.get("send_line") == "true":
            line_msg = _format_esa_monthly_line(report)
            push_fn = current_app.config.get("LINE_PUSH_FN")
            physician_id = _get_physician_line_id(db_url)
            if push_fn and physician_id:
                try:
                    push_fn(physician_id, line_msg)
                    report["line_sent"] = True
                except Exception as e:
                    report["line_sent"] = False
                    report["line_error"] = str(e)

        return jsonify(report)
    except Exception as e:
        logger.error("esa_monthly_report error: %s", e)
        return jsonify({"error": str(e)}), 500


def _format_esa_monthly_line(report):
    """Format ESA monthly report for LINE."""
    s = report["summary"]
    thai_months = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                   "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    month_name = thai_months[report["month"]] if 1 <= report["month"] <= 12 else str(report["month"])

    # Top skip reasons text
    skip_text = ""
    if s["top_skip_reasons"]:
        skip_text = "\n".join(f"  • {r}: {c} ครั้ง" for r, c in s["top_skip_reasons"][:3])

    return (
        f"📊 รายงาน ESA ประจำเดือน {month_name} {report['year'] + 543}\n"
        f"━━━━━━━━━━━━\n"
        f"👥 ผู้ป่วยมี ESA: {report['total_esa_patients']} คน\n"
        f"📋 Sessions ทั้งหมด: {s['total_sessions']} รอบ\n"
        f"━━━━━━━━━━━━\n"
        f"💉 ฉีด ESA: {s['esa_given']} ครั้ง\n"
        f"❌ งด ESA: {s['esa_skipped']} ครั้ง\n"
        f"⚠️ ไม่บันทึก: {s['no_esa_record']} ครั้ง\n"
        f"━━━━━━━━━━━━\n"
        f"🔬 Hb เฉลี่ย: {s['avg_hb'] or '-'} g/dL\n"
        f"🎯 Hb ในเป้า (10-12): {s['hb_in_target_pct']}%\n"
        f"━━━━━━━━━━━━\n"
        f"📌 เหตุผลงด ESA:\n{skip_text or '  ไม่มี'}\n"
        f"━━━━━━━━━━━━\n"
        f"HD Smart Monitoring — คลินิกระนอง รักษ์ไต"
    )


def _get_physician_line_id(db_url):
    """Get physician LINE ID from settings."""
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM hd_settings WHERE key='physician_line_id'")
            r = cur.fetchone()
        conn.close()
        return r[0] if r else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════
# F12: Quick Vital Entry from Dashboard
# ══════════════════════════════════════════════════════════

@hd_bp.route("/sessions/<int:session_id>/quick-vital", methods=["POST"])
@require_role("physician", "nurse", "admin")
def quick_vital_entry(session_id):
    """Quick BP/HR entry without opening the full session page."""
    data = request.get_json(silent=True) or {}
    sbp = data.get("sbp")
    dbp = data.get("dbp")
    hr = data.get("hr")

    if not sbp:
        return jsonify({"error": "ต้องระบุ SBP"}), 400

    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Verify session exists and is active
            cur.execute("SELECT status FROM hd_sessions WHERE id = %s", (session_id,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "ไม่พบ session"}), 404
            if row[0] != "in_progress":
                conn.close()
                return jsonify({"error": "Session ไม่อยู่ระหว่างฟอก"}), 400

            cur.execute("""
                INSERT INTO hd_vital_signs (session_id, phase, sbp, dbp, heart_rate, recorded_at)
                VALUES (%s, 'intra', %s, %s, %s, NOW())
                RETURNING id
            """, (session_id, sbp, dbp, hr))
            vital_id = cur.fetchone()[0]

            # Evaluate alert rules (shared with the session page) + LINE physician
            new_alerts = _process_vital_alerts(cur, session_id, sbp=sbp, dbp=dbp, hr=hr)
            alert = None
            if new_alerts:
                worst = sorted(new_alerts, key=lambda a: 0 if a["severity"] == "critical" else 1)[0]
                alert = {"severity": worst["severity"], "title": worst["title"]}

        conn.commit()
        conn.close()

        log_action(user_id=g.get("hd_user_id"), action="vital.quick_entry",
                   resource_type="vital", resource_id=vital_id,
                   ip_address=request.remote_addr or "")

        return jsonify({
            "vital_id": vital_id,
            "sbp": sbp, "dbp": dbp, "hr": hr,
            "alert": alert,
            "alerts": new_alerts,
            "message": f"บันทึก BP {sbp}/{dbp or '-'} HR {hr or '-'} สำเร็จ",
        })
    except Exception as e:
        logger.error("quick_vital_entry error: %s", e)
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════
# F13: HA/สปสช Quality Indicators
# ══════════════════════════════════════════════════════════

@hd_bp.route("/report/quality-indicators", methods=["GET"])
@require_role("physician", "admin")
def quality_indicators_report():
    """Auto-generate HA/สปสช quality indicators."""
    import os
    from datetime import date as dt_date
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503

    month = request.args.get("month", type=int, default=_thai_today().month)
    year = request.args.get("year", type=int, default=_thai_today().year)

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # 1. Kt/V adequacy (≥1.2)
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE kt_v >= 1.2),
                       COUNT(*) FILTER (WHERE kt_v IS NOT NULL AND kt_v > 0),
                       COUNT(*)
                FROM hd_sessions
                WHERE status = 'completed'
                  AND EXTRACT(MONTH FROM session_date) = %s
                  AND EXTRACT(YEAR FROM session_date) = %s
            """, (month, year))
            kt_row = cur.fetchone()
            ktv_adequate = kt_row[0] or 0
            ktv_measured = kt_row[1] or 0
            total_sessions = kt_row[2] or 0

            # 2. Hb in target (10-12 g/dL)
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE hb_value >= 10 AND hb_value <= 12),
                       COUNT(*) FILTER (WHERE hb_value IS NOT NULL)
                FROM hd_sessions
                WHERE status = 'completed'
                  AND EXTRACT(MONTH FROM session_date) = %s
                  AND EXTRACT(YEAR FROM session_date) = %s
            """, (month, year))
            hb_row = cur.fetchone()
            hb_target = hb_row[0] or 0
            hb_measured = hb_row[1] or 0

            # 3. ESA coverage
            cur.execute("""
                SELECT COUNT(DISTINCT s.patient_id)
                       FILTER (WHERE s.esa_given = TRUE),
                       COUNT(DISTINCT s.patient_id)
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE s.status = 'completed'
                  AND p.esa_drug IS NOT NULL AND p.esa_drug != ''
                  AND EXTRACT(MONTH FROM s.session_date) = %s
                  AND EXTRACT(YEAR FROM s.session_date) = %s
            """, (month, year))
            esa_row = cur.fetchone()
            esa_received = esa_row[0] or 0
            esa_prescribed = esa_row[1] or 0

            # 4. Intradialytic Hypotension (SBP < 90)
            cur.execute("""
                SELECT COUNT(DISTINCT v.session_id), %s
                FROM hd_vital_signs v
                JOIN hd_sessions s ON s.id = v.session_id
                WHERE s.status = 'completed'
                  AND v.phase = 'intra' AND v.sbp < 90
                  AND EXTRACT(MONTH FROM s.session_date) = %s
                  AND EXTRACT(YEAR FROM s.session_date) = %s
            """, (total_sessions, month, year))
            idh_row = cur.fetchone()
            idh_sessions = idh_row[0] or 0

            # 5. VA infection (from alerts with infection keyword)
            cur.execute("""
                SELECT COUNT(DISTINCT a.session_id)
                FROM hd_clinical_alerts a
                JOIN hd_sessions s ON s.id = a.session_id
                WHERE s.status = 'completed'
                  AND (a.title ILIKE '%%infection%%' OR a.title ILIKE '%%ติดเชื้อ%%'
                       OR a.message ILIKE '%%infection%%')
                  AND EXTRACT(MONTH FROM s.session_date) = %s
                  AND EXTRACT(YEAR FROM s.session_date) = %s
            """, (month, year))
            infection_row = cur.fetchone()
            infection_count = infection_row[0] or 0

            # 6. Unique patients
            cur.execute("""
                SELECT COUNT(DISTINCT patient_id) FROM hd_sessions
                WHERE status = 'completed'
                  AND EXTRACT(MONTH FROM session_date) = %s
                  AND EXTRACT(YEAR FROM session_date) = %s
            """, (month, year))
            unique_patients = cur.fetchone()[0] or 0

        conn.close()

        def pct(a, b):
            return round(a / b * 100, 1) if b > 0 else 0

        indicators = {
            "month": month, "year": year,
            "total_sessions": total_sessions,
            "unique_patients": unique_patients,
            "indicators": [
                {
                    "code": "HD-KTV",
                    "name": "Kt/V Adequacy (≥1.2)",
                    "numerator": ktv_adequate, "denominator": ktv_measured,
                    "value_pct": pct(ktv_adequate, ktv_measured),
                    "target_pct": 65, "unit": "%",
                    "status": "pass" if pct(ktv_adequate, ktv_measured) >= 65 else "fail",
                },
                {
                    "code": "HD-HB",
                    "name": "Hb in Target (10-12 g/dL)",
                    "numerator": hb_target, "denominator": hb_measured,
                    "value_pct": pct(hb_target, hb_measured),
                    "target_pct": 50, "unit": "%",
                    "status": "pass" if pct(hb_target, hb_measured) >= 50 else "fail",
                },
                {
                    "code": "HD-ESA",
                    "name": "ESA Coverage",
                    "numerator": esa_received, "denominator": esa_prescribed,
                    "value_pct": pct(esa_received, esa_prescribed),
                    "target_pct": 80, "unit": "%",
                    "status": "pass" if pct(esa_received, esa_prescribed) >= 80 else "fail",
                },
                {
                    "code": "HD-IDH",
                    "name": "Intradialytic Hypotension Rate",
                    "numerator": idh_sessions, "denominator": total_sessions,
                    "value_pct": pct(idh_sessions, total_sessions),
                    "target_pct": 10, "unit": "%",
                    "status": "pass" if pct(idh_sessions, total_sessions) <= 10 else "fail",
                    "lower_is_better": True,
                },
                {
                    "code": "HD-INF",
                    "name": "VA Infection Rate",
                    "numerator": infection_count, "denominator": total_sessions,
                    "value_pct": pct(infection_count, total_sessions),
                    "target_pct": 2, "unit": "%",
                    "status": "pass" if pct(infection_count, total_sessions) <= 2 else "fail",
                    "lower_is_better": True,
                },
            ],
        }

        # LINE summary if requested
        if request.args.get("send_line") == "true":
            line_msg = _format_qi_line(indicators)
            push_fn = current_app.config.get("LINE_PUSH_FN")
            physician_id = _get_physician_line_id(db_url)
            if push_fn and physician_id:
                try:
                    push_fn(physician_id, line_msg)
                    indicators["line_sent"] = True
                except Exception:
                    indicators["line_sent"] = False

        return jsonify(indicators)
    except Exception as e:
        logger.error("quality_indicators error: %s", e)
        return jsonify({"error": str(e)}), 500


def _format_qi_line(qi):
    """Format quality indicators for LINE."""
    thai_months = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                   "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    month_name = thai_months[qi["month"]] if 1 <= qi["month"] <= 12 else str(qi["month"])
    lines = [
        f"📊 ตัวชี้วัดคุณภาพ HD — {month_name} {qi['year'] + 543}",
        "━━━━━━━━━━━━",
        f"👥 ผู้ป่วย: {qi['unique_patients']} คน | Sessions: {qi['total_sessions']} รอบ",
        "━━━━━━━━━━━━",
    ]
    for ind in qi["indicators"]:
        icon = "✅" if ind["status"] == "pass" else "❌"
        lower = ind.get("lower_is_better", False)
        target_label = f"≤{ind['target_pct']}%" if lower else f"≥{ind['target_pct']}%"
        lines.append(f"{icon} {ind['name']}: {ind['value_pct']}% (เป้า {target_label})")
        lines.append(f"   ({ind['numerator']}/{ind['denominator']})")
    lines.extend([
        "━━━━━━━━━━━━",
        "HD Smart Monitoring — คลินิกระนอง รักษ์ไต",
    ])
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# F14: Patient Self-View (ผลฟอก + Hb trend)
# ══════════════════════════════════════════════════════════

def _portal_sign(hn):
    """HMAC signature binding a portal link to one HN (no expiry — revoke by rotating JWT_SECRET)."""
    import hashlib
    import hmac as _hmac
    key = (HdConfig.JWT_SECRET or "hd-portal").encode()
    return _hmac.new(key, ("portal:" + str(hn).upper()).encode(), hashlib.sha256).hexdigest()[:16]


def portal_token(hn):
    """Signed portal token for one patient: '<HN>.<sig>'."""
    return f"{hn}.{_portal_sign(hn)}"


def _portal_hn_from_token(token):
    """'<HN>.<sig>' → HN when the signature matches, else None. A bare HN is rejected."""
    import hmac as _hmac
    if "." not in (token or ""):
        return None
    hn, sig = token.rsplit(".", 1)
    if hn and sig and _hmac.compare_digest(sig, _portal_sign(hn)):
        return hn
    return None


@hd_bp.route("/my/<token>", methods=["GET"])
def patient_portal_api(token):
    """Patient Portal JSON API — full data for the patient-facing web page.

    token is the HMAC-signed '<HN>.<sig>' from portal_token(); a bare HN 404s,
    so the link staff hand out is the only way in (same data as the LIFF flow,
    which authenticates with LINE instead)."""
    import os
    import psycopg2
    from .patient_portal import portal_data
    hn = _portal_hn_from_token(token)
    if not hn:
        return jsonify({"error": "ลิงก์ไม่ถูกต้อง — ขอลิงก์ใหม่จากเจ้าหน้าที่คลินิก"}), 404
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "ระบบไม่พร้อม"}), 503
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        data = portal_data(conn, hn)
        conn.close()
        if not data:
            return jsonify({"error": "ไม่พบข้อมูลผู้ป่วย"}), 404
        return jsonify(data)
    except Exception as e:
        logger.error("patient_portal_api: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Upgrade #5: AI Health Coach — Patient Chat API ───────────

@hd_bp.route("/my/<token>/ask", methods=["POST"])
def patient_health_coach(token):
    """AI Health Coach — ผู้ป่วยถามเรื่องสุขภาพผ่าน Portal.

    Rule-based (ไม่ใช้ External AI): วิเคราะห์จาก lab + sessions + profile
    ตอบเป็นภาษาไทยง่ายๆ ไม่เปิดเผยข้อมูลทางการแพทย์ที่ซับซ้อน
    """
    import os, psycopg2
    from datetime import datetime, timedelta, timezone

    hn = _portal_hn_from_token(token)
    if not hn:
        return jsonify({"error": "ลิงก์ไม่ถูกต้อง"}), 404

    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question or len(question) > 500:
        return jsonify({"answer": "กรุณาพิมพ์คำถามสั้นๆ (ไม่เกิน 500 ตัวอักษร) ค่ะ"})

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"answer": "ระบบไม่พร้อมชั่วคราว กรุณาลองใหม่ค่ะ"})

    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        from .patient_portal import portal_data
        pd = portal_data(conn, hn)
        conn.close()
        if not pd:
            return jsonify({"answer": "ไม่พบข้อมูลของท่านในระบบค่ะ"})

        answer = _health_coach_answer(question, pd)
        return jsonify({"answer": answer})
    except Exception as e:
        logger.error("health-coach: %s", e)
        return jsonify({"answer": "ขออภัย ระบบขัดข้องชั่วคราว กรุณาลองใหม่ค่ะ"})


def _health_coach_answer(q, pd):
    """Rule-based AI Health Coach — ตอบคำถามผู้ป่วยจาก portal data."""
    q_lower = q.lower().replace(" ", "")
    profile = pd.get("profile", {})
    latest = pd.get("latest", {})
    interp = pd.get("interpretations", {})
    sessions = pd.get("sessions", [])
    tips = pd.get("tips", [])
    fluid = pd.get("fluid")
    idwg_list = pd.get("idwg", [])
    name = profile.get("name", "คุณ")
    # Short name
    short = name.split(" ")[0] if name else "คุณ"

    # ── Category: นัดฟอก / ตารางฟอก ──
    if any(k in q_lower for k in ["นัด", "ฟอกครั้งถัดไป", "ฟอกวันไหน", "ตารางฟอก", "วันฟอก", "รอบฟอก", "เมื่อไหร่"]):
        nd = profile.get("next_dialysis")
        sched = profile.get("schedule") or "-"
        shift = profile.get("shift") or ""
        if nd:
            return (f"📅 นัดฟอกครั้งถัดไปของ{short}: {nd['text']}\n"
                    f"ตารางประจำ: {sched} {shift}\n\n"
                    f"💡 ก่อนฟอก: จำกัดน้ำ, หลีกเลี่ยงเกลือ\n"
                    f"อย่าลืมชั่งน้ำหนักก่อนมาฟอกด้วยนะคะ")
        return f"ตารางฟอกของ{short}: {sched} {shift}"

    # ── Category: ผลเลือด / lab / เลือดจาง / Hb ──
    if any(k in q_lower for k in ["ผลเลือด", "แล็บ", "lab", "เลือดจาง", "hb", "hemoglobin", "ค่าเลือด", "โลหิตจาง"]):
        parts = []
        if "hb" in interp:
            parts.append(interp["hb"]["text"])
        if "ktv" in interp:
            parts.append(interp["ktv"]["text"])
        if "ferritin" in interp:
            parts.append(interp["ferritin"]["text"])
        if parts:
            header = f"🩺 สรุปผลเลือดล่าสุดของ{short}:\n\n"
            return header + "\n".join(f"• {p}" for p in parts) + "\n\n💡 ถ้ามีข้อสงสัย แจ้งแพทย์ในรอบฟอกถัดไปได้เลยค่ะ"
        return "ยังไม่มีผลเลือดในระบบค่ะ แจ้งเจ้าหน้าที่เพื่อบันทึกผลเลือดได้"

    # ── Category: น้ำหนัก / IDWG / DW ──
    if any(k in q_lower for k in ["น้ำหนัก", "idwg", "dw", "น้ำหนักแห้ง", "น้ำหนักขึ้น", "อ้วน", "ผอม"]):
        dw = profile.get("dry_weight")
        if idwg_list:
            last_idwg = idwg_list[-1]
            msg = f"⚖️ น้ำหนักแห้ง (DW) ของ{short}: {dw:.1f} kg\n\n" if dw else ""
            msg += f"น้ำหนักที่ขึ้นระหว่างรอบฟอกล่าสุด: {last_idwg['kg']:.1f} kg"
            if last_idwg.get("pct") is not None:
                msg += f" ({last_idwg['pct']:.0f}% ของ DW)"
                if last_idwg["pct"] > 5:
                    msg += "\n\n⚠️ เกินเป้า 5% — ควรจำกัดน้ำ < 1 ลิตร/วัน ลดเกลือ และหลีกเลี่ยงผลไม้ที่มีน้ำมาก"
                elif last_idwg["pct"] <= 3:
                    msg += "\n\n✅ ดีมาก! ควบคุมน้ำได้ดี"
                else:
                    msg += "\n\n👍 อยู่ในเกณฑ์ — พยายามรักษาไว้นะคะ"
            return msg
        if dw:
            return f"⚖️ น้ำหนักแห้ง (DW) ของ{short}: {dw:.1f} kg\nยังไม่มีข้อมูล IDWG ค่ะ"
        return "ยังไม่มีข้อมูลน้ำหนักในระบบค่ะ"

    # ── Category: น้ำ / สารน้ำ / BCM / บวม ──
    if any(k in q_lower for k in ["น้ำ", "สารน้ำ", "bcm", "บวม", "fluid", "ดื่มน้ำ", "กินน้ำ"]):
        if fluid:
            oh = fluid.get("oh", 0)
            msg = f"💧 สถานะน้ำในร่างกายของ{short}:\n{fluid['text']}\n(OH {'+' if oh>0 else ''}{oh:.1f} L)\n\n"
            if oh > 2:
                msg += "⚠️ มีน้ำเกินมาก:\n• จำกัดน้ำ < 500-750 mL/วัน\n• ลดเกลือให้มากที่สุด\n• อมน้ำแข็งแทนดื่มน้ำ"
            elif oh > 1:
                msg += "คำแนะนำ:\n• จำกัดน้ำ < 1 ลิตร/วัน\n• ลดเกลือ, หลีกเลี่ยงน้ำจิ้ม, ของหมักดอง"
            else:
                msg += "✅ น้ำในร่างกายอยู่ในเกณฑ์ดี — รักษาระดับนี้ไว้นะคะ"
            return msg
        return "💧 คำแนะนำทั่วไป:\n• จำกัดน้ำ < 1 ลิตร/วัน ในวันหยุดฟอก\n• ลดเกลือช่วยลดการกระหายน้ำ\n• อมน้ำแข็งก้อนเล็กแทนดื่มน้ำ\n• ชั่งน้ำหนักทุกวันเช้า"

    # ── Category: อาหาร / กิน / ทาน / diet ──
    if any(k in q_lower for k in ["อาหาร", "กิน", "ทาน", "diet", "โปรตีน", "ผลไม้", "ห้ามกิน", "กินอะไร"]):
        msg = f"🍽️ คำแนะนำอาหารสำหรับ{short}:\n\n"
        msg += "✅ ควรทาน:\n• โปรตีนสูง: ไข่ขาว, เนื้อปลา, อกไก่\n• ผักต้ม: ผักบุ้ง, แตงกวา, ฟักทอง\n\n"
        msg += "❌ หลีกเลี่ยง:\n• ผลไม้โพแทสเซียมสูง: กล้วย, ส้ม, มะละกอ, ทุเรียน\n• ของเค็ม: ปลาเค็ม, น้ำปลา, ซีอิ๊ว\n• ถั่ว, ช็อกโกแลต, โกโก้ (ฟอสฟอรัสสูง)\n"
        # Personalize from labs
        if latest.get("hb") and float(latest["hb"]) < 10:
            msg += "\n💡 Hb ต่ำ: เพิ่มเนื้อแดง, ตับ เพื่อเสริมธาตุเหล็ก"
        return msg

    # ── Category: ยา / ESA / เม็ดเลือด ──
    if any(k in q_lower for k in ["ยา", "esa", "epo", "เม็ดเลือด", "ฉีดยา", "เสริมเหล็ก", "iron"]):
        esa_drug = profile.get("esa_drug") or "-"
        esa_dose = profile.get("esa_dose") or ""
        msg = f"💊 ยาของ{short}:\n• กระตุ้นเม็ดเลือดแดง: {esa_drug} {esa_dose}\n\n"
        msg += "⚠️ สิ่งสำคัญ:\n• ฉีดยาตามแพทย์สั่ง อย่าปรับขนาดเอง\n• ถ้ามีอาการข้างเคียง (ปวดหัว, ความดันสูง) แจ้งแพทย์\n"
        msg += "• ไม่ควรหยุดยาเองเพราะอาจทำให้เลือดจางกลับมา"
        return msg

    # ── Category: ออกกำลังกาย ──
    if any(k in q_lower for k in ["ออกกำลัง", "exercise", "เดิน", "วิ่ง", "กีฬา", "สุขภาพ"]):
        return (f"🏃 การออกกำลังกายสำหรับผู้ป่วยฟอกเลือดอย่าง{short}:\n\n"
                "✅ แนะนำ:\n• เดินเร็ว 20-30 นาที/วัน ในวันที่ไม่ฟอก\n• ยืดเหยียดกล้ามเนื้อ\n• ปั่นจักรยาน (เบาๆ)\n\n"
                "❌ หลีกเลี่ยง:\n• ออกกำลังกายหนักในวันฟอก\n• ยกน้ำหนักด้วยแขนที่มี AVF/AVG\n• ว่ายน้ำถ้ายังมีสายฟอก (catheter)\n\n"
                "💡 เริ่มจากน้อยๆ เพิ่มทีละนิด สม่ำเสมอดีกว่าหนักแล้วหยุด")

    # ── Category: ผลฟอก / session ──
    if any(k in q_lower for k in ["ผลฟอก", "รอบฟอก", "session", "ฟอกล่าสุด", "ครั้งล่าสุด"]):
        if sessions:
            s = sessions[0]
            msg = f"📋 ผลฟอกล่าสุดของ{short} ({s['date']}):\n\n"
            if s.get("pre_weight"):
                msg += f"• น้ำหนักก่อนฟอก: {s['pre_weight']:.1f} kg\n"
            if s.get("post_weight"):
                msg += f"• น้ำหนักหลังฟอก: {s['post_weight']:.1f} kg\n"
            if s.get("uf"):
                msg += f"• UF (น้ำที่ดึงออก): {s['uf']:.0f} mL\n"
            if s.get("duration"):
                msg += f"• เวลาฟอก: {s['duration']:.1f} ชั่วโมง\n"
            msg += f"\nรวมฟอกในระบบ: {len(sessions)} รอบล่าสุด"
            return msg
        return "ยังไม่มีข้อมูลผลฟอกในระบบค่ะ"

    # ── Category: เส้นฟอก / AVF / catheter ──
    if any(k in q_lower for k in ["เส้นฟอก", "avf", "avg", "catheter", "สายฟอก", "เส้นเลือด", "fistula"]):
        va = profile.get("va") or "ไม่ระบุ"
        msg = f"🩸 เส้นฟอกเลือดของ{short}: {va}\n\n"
        if "avf" in va.lower() or "fistula" in va.lower():
            msg += "ดูแล AVF:\n• คลำ thrill (สั่น) ทุกวัน\n• ไม่วัดความดัน/เจาะเลือดแขนข้างนี้\n• ไม่สวมนาฬิกา/สร้อยรัด\n• ถ้าเลือดไม่หยุดหลังฟอก กดนาน 15 นาที"
        elif "catheter" in va.lower() or "perm" in va.lower():
            msg += "ดูแลสายฟอก:\n• อย่าเปิดผ้าปิดแผลเอง\n• ไม่แช่น้ำ/ว่ายน้ำ\n• ถ้าปวด บวม แดง มีไข้ แจ้งทันที"
        return msg

    # ── Category: สวัสดี / ทักทาย ──
    if any(k in q_lower for k in ["สวัสดี", "หวัดดี", "hello", "hi", "ดีจ้า", "สวัสดีค่ะ", "สวัสดีครับ"]):
        nd = profile.get("next_dialysis")
        greeting = f"สวัสดีค่ะ {short} 😊\n\n"
        if nd:
            greeting += f"📅 นัดฟอกถัดไป: {nd['text']}\n"
        if tips:
            greeting += f"\n💡 {tips[0].get('text', '')}"
        greeting += "\n\nพิมพ์ถามได้เลยค่ะ เช่น:\n• ผลเลือด\n• นัดฟอก\n• น้ำหนัก\n• อาหาร\n• ออกกำลังกาย"
        return greeting

    # ── Category: ขอบคุณ ──
    if any(k in q_lower for k in ["ขอบคุณ", "ขอบใจ", "thankyou", "thanks"]):
        return f"ยินดีค่ะ {short} 😊 ถ้ามีอะไรสงสัยถามได้ตลอดนะคะ ดูแลสุขภาพด้วยค่ะ 💪"

    # ── Default: แนะนำสิ่งที่ถามได้ ──
    return (f"ขอโทษค่ะ {short} ไม่แน่ใจว่าถามเรื่องอะไร 😅\n\n"
            "ลองถามเรื่องเหล่านี้ได้ค่ะ:\n"
            "📅 \"นัดฟอกครั้งถัดไป\"\n"
            "🩺 \"ผลเลือดล่าสุด\"\n"
            "⚖️ \"น้ำหนักของฉัน\"\n"
            "💧 \"ดื่มน้ำเท่าไหร่\"\n"
            "🍽️ \"กินอะไรดี\"\n"
            "💊 \"ยาของฉัน\"\n"
            "🏃 \"ออกกำลังกาย\"\n"
            "📋 \"ผลฟอกล่าสุด\"\n"
            "🩸 \"ดูแลเส้นฟอก\"")


@hd_bp.route("/my-results/<token>", methods=["GET"])
def patient_self_view(token):
    """Patient-facing page to view own HD results.

    Token = HN encrypted or simple HMAC-based token linked to LINE userId.
    For now: token = HN (will be upgraded to HMAC in production).
    """
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return "<h2>ระบบไม่พร้อมใช้งาน</h2>", 503

    # Decode token → HN (simple for now, upgrade to HMAC later)
    hn = token

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Patient info
            cur.execute("""
                SELECT patient_name, vascular_access, schedule_days, dry_weight
                FROM hd_patient_mapping WHERE hn = %s AND is_active = TRUE
            """, (hn,))
            prow = cur.fetchone()
            if not prow:
                conn.close()
                return "<h2>ไม่พบข้อมูล</h2>", 404

            # Last 10 sessions
            cur.execute("""
                SELECT session_date, pre_weight, post_weight, actual_uf,
                       hb_value, hct_value, esa_given, esa_drug_given,
                       notes, started_at, ended_at
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE p.hn = %s AND s.status = 'completed'
                ORDER BY s.session_date DESC, s.started_at DESC
                LIMIT 10
            """, (hn,))
            sessions = cur.fetchall()

            # Hb trend (last 6 months)
            cur.execute("""
                SELECT session_date, hb_value
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE p.hn = %s AND s.status = 'completed' AND s.hb_value IS NOT NULL
                ORDER BY s.session_date
                LIMIT 20
            """, (hn,))
            hb_data = cur.fetchall()
        conn.close()

        # Build SVG Hb trend chart
        hb_svg = _build_hb_trend_svg(hb_data)

        # Session rows
        session_rows = ""
        for s in sessions:
            dt = s[0].strftime("%d/%m") if s[0] else "-"
            duration = "-"
            if s[9] and s[10]:
                h = (s[10] - s[9]).total_seconds() / 3600
                duration = f"{h:.1f} ชม."
            uf = f"{int(s[3])} mL" if s[3] else "-"
            hb_str = f"{float(s[4]):.1f}" if s[4] else "-"
            esa_str = "💉" if s[6] else ("❌" if s[6] is False else "-")
            session_rows += f"""
            <tr>
              <td>{dt}</td>
              <td>{s[1] or '-'}→{s[2] or '-'}</td>
              <td>{uf}</td>
              <td>{hb_str}</td>
              <td>{esa_str}</td>
            </tr>"""

        return f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ผลฟอกเลือด — {prow[0]}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,sans-serif;background:#f5f7fa;color:#2c3e50;padding:16px;max-width:480px;margin:0 auto}}
  .card{{background:#fff;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
  .header{{text-align:center;padding:20px 0;}}
  .header h1{{font-size:1.2rem;color:#2980b9}}
  .header .name{{font-size:1.4rem;font-weight:800;margin-top:4px}}
  .info-row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eee;font-size:.9rem}}
  .info-row:last-child{{border:none}}
  .label{{color:#7f8c8d}}.value{{font-weight:600}}
  table{{width:100%;border-collapse:collapse;font-size:.8rem}}
  th{{background:#2980b9;color:#fff;padding:8px 6px;text-align:left}}
  td{{padding:8px 6px;border-bottom:1px solid #eee}}
  .chart-title{{font-size:.9rem;font-weight:700;margin-bottom:8px;color:#2c3e50}}
  .target-band{{font-size:.75rem;color:#27ae60;margin-top:4px}}
  .footer{{text-align:center;font-size:.75rem;color:#bdc3c7;margin-top:20px;padding:12px}}
</style>
</head>
<body>
  <div class="header">
    <h1>🏥 คลินิกระนอง รักษ์ไต</h1>
    <div class="name">{prow[0]}</div>
  </div>

  <div class="card">
    <div class="info-row"><span class="label">HN</span><span class="value">{hn}</span></div>
    <div class="info-row"><span class="label">ตารางฟอก</span><span class="value">{prow[2] or '-'}</span></div>
    <div class="info-row"><span class="label">Vascular Access</span><span class="value">{prow[1] or '-'}</span></div>
    <div class="info-row"><span class="label">Dry Weight</span><span class="value">{float(prow[3]):.1f} kg</span></div>
  </div>

  <div class="card">
    <div class="chart-title">📈 Hb Trend (เป้าหมาย 10-12 g/dL)</div>
    {hb_svg}
    <div class="target-band">🟢 เขียว = อยู่ในเป้าหมาย KDOQI</div>
  </div>

  <div class="card">
    <div class="chart-title">📋 ผลฟอกเลือดล่าสุด</div>
    <table>
      <tr><th>วันที่</th><th>น้ำหนัก</th><th>UF</th><th>Hb</th><th>ESA</th></tr>
      {session_rows}
    </table>
  </div>

  <div class="footer">
    HD Smart Monitoring v2.3<br>
    ข้อมูลนี้เป็นข้อมูลส่วนบุคคล กรุณาไม่เผยแพร่
  </div>
</body>
</html>"""
    except Exception as e:
        logger.error("patient_self_view error: %s", e)
        return f"<h2>เกิดข้อผิดพลาด</h2><p>{e}</p>", 500


def _build_hb_trend_svg(hb_data):
    """Build inline SVG Hb trend chart."""
    if not hb_data:
        return '<p style="color:#bdc3c7;text-align:center;padding:20px;">ยังไม่มีข้อมูล Hb</p>'

    w, h = 340, 160
    margin = {"t": 20, "r": 10, "b": 30, "l": 35}
    pw = w - margin["l"] - margin["r"]
    ph = h - margin["t"] - margin["b"]

    values = [float(r[1]) for r in hb_data]
    dates = [r[0].strftime("%d/%m") for r in hb_data]
    min_v = min(min(values) - 1, 8)
    max_v = max(max(values) + 1, 14)
    rng = max_v - min_v or 1

    # Target band (10-12)
    y_10 = margin["t"] + ph - ((10 - min_v) / rng * ph)
    y_12 = margin["t"] + ph - ((12 - min_v) / rng * ph)

    points = []
    dots = []
    for i, v in enumerate(values):
        x = margin["l"] + (i / max(len(values) - 1, 1)) * pw
        y = margin["t"] + ph - ((v - min_v) / rng * ph)
        points.append(f"{x:.1f},{y:.1f}")
        color = "#27ae60" if 10 <= v <= 12 else ("#e74c3c" if v < 10 else "#f39c12")
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" stroke="#fff" stroke-width="1.5"/>')
        if i % max(1, len(values) // 5) == 0 or i == len(values) - 1:
            dots.append(f'<text x="{x:.1f}" y="{h - 5}" text-anchor="middle" font-size="9" fill="#7f8c8d">{dates[i]}</text>')
            dots.append(f'<text x="{x:.1f}" y="{y - 8:.1f}" text-anchor="middle" font-size="9" fill="{color}" font-weight="600">{v:.1f}</text>')

    polyline = " ".join(points)

    # Y-axis labels
    y_labels = ""
    for v in range(int(min_v), int(max_v) + 1, 2):
        y = margin["t"] + ph - ((v - min_v) / rng * ph)
        y_labels += f'<text x="{margin["l"]-5}" y="{y + 3:.1f}" text-anchor="end" font-size="9" fill="#7f8c8d">{v}</text>'
        y_labels += f'<line x1="{margin["l"]}" y1="{y:.1f}" x2="{w-margin["r"]}" y2="{y:.1f}" stroke="#ecf0f1" stroke-dasharray="3"/>'

    return f"""<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;">
  {y_labels}
  <rect x="{margin["l"]}" y="{y_12:.1f}" width="{pw}" height="{y_10 - y_12:.1f}" fill="rgba(39,174,96,0.08)" rx="2"/>
  <line x1="{margin["l"]}" y1="{y_10:.1f}" x2="{w-margin["r"]}" y2="{y_10:.1f}" stroke="#27ae60" stroke-dasharray="4" opacity=".4"/>
  <line x1="{margin["l"]}" y1="{y_12:.1f}" x2="{w-margin["r"]}" y2="{y_12:.1f}" stroke="#27ae60" stroke-dasharray="4" opacity=".4"/>
  <polyline points="{polyline}" fill="none" stroke="#3498db" stroke-width="2" stroke-linejoin="round"/>
  {"".join(dots)}
</svg>"""


# ── Daily Checklist ──────────────────────────────────────

# Standard checklist items
_CHECKLIST_ITEMS = {
    "pre_shift": [
        ("check_water_system", "ตรวจระบบน้ำ RO/DI"),
        ("check_dialysate", "ตรวจน้ำยาไตเทียม Dialysate"),
        ("check_machines", "ตรวจเครื่องฟอกเลือดทุกเครื่อง"),
        ("prepare_supplies", "เตรียมอุปกรณ์/เวชภัณฑ์"),
        ("check_emergency", "ตรวจเครื่องมือฉุกเฉิน (Defibrillator/O2)"),
        ("check_medications", "เตรียมยาประจำรอบ"),
        ("patient_list_ready", "ตรวจสอบรายชื่อผู้ป่วยนัดฟอก"),
    ],
    "during_shift": [
        ("vital_signs_recorded", "บันทึก Vital Signs ครบทุกราย"),
        ("uf_monitoring", "ติดตาม UF Goal ตามแผน"),
        ("machine_alarms_checked", "ตอบสนอง Machine Alarm ทันเวลา"),
        ("anticoag_given", "ให้ Anticoagulant ตามแผน"),
        ("esa_given_per_plan", "ฉีด ESA ตามแผนการรักษา"),
        ("complications_documented", "บันทึกภาวะแทรกซ้อน (ถ้ามี)"),
    ],
    "post_shift": [
        ("machines_cleaned", "ทำความสะอาดเครื่องฟอก"),
        ("supplies_restocked", "เติมอุปกรณ์/เวชภัณฑ์"),
        ("waste_disposed", "กำจัดขยะติดเชื้อ"),
        ("documentation_complete", "บันทึกข้อมูลครบถ้วน"),
        ("handover_done", "ส่งเวรเรียบร้อย"),
        ("area_cleaned", "ทำความสะอาดพื้นที่"),
    ],
}


@hd_bp.route("/checklist", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_checklist():
    """Get daily checklist status for a date+shift."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    current_shift = request.args.get("shift", "morning")
    date_str = request.args.get("date", _thai_today().isoformat())

    items = {}
    for cat, cat_items in _CHECKLIST_ITEMS.items():
        items[cat] = []
        for key, label in cat_items:
            items[cat].append({
                "key": key, "label": label,
                "checked": False, "checked_by": None, "checked_at": None, "notes": "",
            })

    if db_url:
        try:
            conn = psycopg2.connect(db_url, connect_timeout=5)
            with conn.cursor() as cur:
                # Auto-create table if not exists
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS hd_daily_checklist (
                        id SERIAL PRIMARY KEY,
                        checklist_date DATE NOT NULL,
                        shift VARCHAR(20) NOT NULL DEFAULT 'morning',
                        category VARCHAR(30) NOT NULL,
                        item_key VARCHAR(60) NOT NULL,
                        item_label VARCHAR(200) NOT NULL,
                        checked BOOLEAN NOT NULL DEFAULT FALSE,
                        checked_by INTEGER REFERENCES hd_users(id),
                        checked_at TIMESTAMP,
                        notes VARCHAR(300),
                        created_at TIMESTAMP DEFAULT NOW(),
                        UNIQUE(checklist_date, shift, item_key)
                    )
                """)
                conn.commit()

                cur.execute("""
                    SELECT item_key, checked, checked_at, notes
                    FROM hd_daily_checklist
                    WHERE checklist_date = %s AND shift = %s
                """, (date_str, current_shift))
                db_items = {r[0]: {"checked": r[1], "checked_at": (r[2].isoformat() + "Z") if r[2] else None, "notes": r[3]} for r in cur.fetchall()}

                for cat in items:
                    for item in items[cat]:
                        if item["key"] in db_items:
                            item["checked"] = db_items[item["key"]]["checked"]
                            item["checked_at"] = db_items[item["key"]]["checked_at"]
                            item["notes"] = db_items[item["key"]]["notes"] or ""
            conn.close()
        except Exception as e:
            logger.warning("checklist GET error: %s", e)

    total = sum(len(v) for v in items.values())
    done = sum(1 for cat in items.values() for it in cat if it["checked"])

    return jsonify({
        "date": date_str, "shift": current_shift,
        "items": items, "total": total, "done": done,
        "progress": round(done / total * 100) if total else 0,
    })


@hd_bp.route("/checklist", methods=["POST"])
@require_role("physician", "nurse", "admin")
def update_checklist():
    """Toggle a checklist item."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database configured"}), 503

    data = request.get_json(force=True, silent=True) or {}
    item_key = data.get("item_key")
    checked = data.get("checked", True)
    shift = data.get("shift", "morning")
    date_str = data.get("date", _thai_today().isoformat())
    notes = data.get("notes", "")

    if not item_key:
        return jsonify({"error": "item_key required"}), 400

    # Find the label
    label = item_key
    category = ""
    for cat, cat_items in _CHECKLIST_ITEMS.items():
        for k, l in cat_items:
            if k == item_key:
                label = l
                category = cat
                break

    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO hd_daily_checklist (checklist_date, shift, category, item_key, item_label, checked, checked_by, checked_at, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                ON CONFLICT (checklist_date, shift, item_key) DO UPDATE
                SET checked = EXCLUDED.checked, checked_by = EXCLUDED.checked_by,
                    checked_at = NOW(), notes = EXCLUDED.notes
            """, (date_str, shift, category, item_key, label, checked, g.get("hd_user_id"), notes))
        conn.commit()
        conn.close()
        return jsonify({"saved": True, "item_key": item_key, "checked": checked})
    except Exception as e:
        logger.error("checklist POST error: %s", e)
        return jsonify({"error": str(e)}), 500


# Page routes are on hd_web (prefix /hd), not hd_bp (prefix /hd/api/v1)
# See below — checklist_page and morning_brief_page registered on hd_web


@hd_bp.route("/morning-brief/data", methods=["GET"])
@require_role("physician", "nurse", "admin")
def morning_brief_data():
    """Morning Brief JSON: today's summary for all shifts."""
    import os, psycopg2
    from .config import HdConfig
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    today = _thai_today()

    result = {
        "date": today.isoformat(),
        "date_thai": "",
        "shifts": {"morning": 0, "afternoon": 0, "evening": 0},
        "total_beds": HdConfig.TOTAL_BEDS,
        "today_patients": 0,
        "alerts": [],
        "dialyzer_warnings": [],
        "lab_abnormal": [],
        "checklist_progress": {},
    }

    # Thai date format
    thai_days = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
    thai_months = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                   "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    result["date_thai"] = f"วัน{thai_days[today.weekday()]}ที่ {today.day} {thai_months[today.month]} {today.year + 543}"

    if not db_url:
        return jsonify(result)

    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Count sessions per shift today
            for shift in ["morning", "afternoon", "evening"]:
                cur.execute("""
                    SELECT COUNT(*) FROM hd_sessions
                    WHERE session_date = %s AND COALESCE(shift, 'morning') = %s
                      AND status IN ('scheduled', 'in_progress', 'completed')
                """, (today.isoformat(), shift))
                result["shifts"][shift] = cur.fetchone()[0]

            # Count today's scheduled patients
            day_map = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}
            today_th = day_map.get(today.weekday(), "")
            if today_th:
                cur.execute("""
                    SELECT COUNT(*) FROM hd_patient_mapping
                    WHERE is_active = TRUE AND schedule_days ILIKE %s
                """, (f"%{today_th}%",))
                result["today_patients"] = cur.fetchone()[0]

            # Pending alerts
            try:
                cur.execute("""
                    SELECT title, severity, created_at
                    FROM hd_clinical_alerts
                    WHERE DATE(created_at) = %s AND acknowledged = FALSE
                    ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END
                    LIMIT 10
                """, (today.isoformat(),))
                result["alerts"] = [{"title": r[0], "severity": r[1],
                                     "time": r[2].strftime("%H:%M") if r[2] else ""} for r in cur.fetchall()]
            except Exception:
                pass

            # Dialyzers near max reuse
            try:
                cur.execute("""
                    SELECT d.dialyzer_type, d.reuse_count, d.max_reuse, p.patient_name, p.hn
                    FROM hd_dialyzer_tracking d
                    JOIN hd_patient_mapping p ON p.id = d.patient_id
                    WHERE d.status = 'active' AND d.reuse_count >= d.max_reuse * 0.8
                    ORDER BY d.reuse_count DESC LIMIT 10
                """)
                result["dialyzer_warnings"] = [
                    {"patient": r[3], "hn": r[4], "type": r[0], "use": r[1], "max": r[2]}
                    for r in cur.fetchall()
                ]
            except Exception:
                pass

            # Checklist progress per shift
            for shift in ["morning", "afternoon", "evening"]:
                try:
                    cur.execute("""
                        SELECT COUNT(*) FILTER (WHERE checked = TRUE), COUNT(*)
                        FROM hd_daily_checklist
                        WHERE checklist_date = %s AND shift = %s
                    """, (today.isoformat(), shift))
                    row = cur.fetchone()
                    if row and row[1]:
                        result["checklist_progress"][shift] = {
                            "done": row[0], "total": row[1],
                            "pct": round(row[0] / row[1] * 100),
                        }
                except Exception:
                    pass

        conn.close()
    except Exception as e:
        logger.warning("morning_brief_data error: %s", e)

    return jsonify(result)


# ── Clinic Profile API ────────────────────────────────────

@hd_bp.route("/clinic-profile", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_clinic_profile():
    """Get all clinic profile data grouped by section."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    sections = {}
    if not db_url:
        return jsonify({"sections": sections})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Table created by migration; schema fixed by boot script
            cur.execute("SELECT section, item_key, item_value, file_url, expires_at FROM hd_clinic_profile ORDER BY section, id")
            for r in cur.fetchall():
                sec = r[0]
                if sec not in sections:
                    sections[sec] = []
                sections[sec].append({
                    "key": r[1], "value": r[2], "file_url": r[3],
                    "expires_at": r[4].isoformat() if r[4] else None,
                })
        conn.close()
    except Exception as e:
        logger.warning("clinic_profile GET: %s", e)
    return jsonify({"sections": sections})


@hd_bp.route("/clinic-profile", methods=["POST"])
@require_role("physician", "admin")
def save_clinic_profile():
    """Save/update a clinic profile item."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    data = request.get_json(force=True, silent=True) or {}
    section = data.get("section")
    key = data.get("key")
    value = data.get("value", "")
    file_url = data.get("file_url")
    expires = data.get("expires_at")
    if not section or not key:
        return jsonify({"error": "section and key required"}), 400
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO hd_clinic_profile (section, item_key, item_value, file_url, expires_at, updated_by, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (section, item_key) DO UPDATE
                SET item_value = EXCLUDED.item_value, file_url = EXCLUDED.file_url,
                    expires_at = EXCLUDED.expires_at, updated_by = EXCLUDED.updated_by, updated_at = NOW()
            """, (section, key, value, file_url, expires, g.get("hd_username") or str(g.get("hd_user_id") or "")))
        conn.commit()
        conn.close()
        return jsonify({"saved": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/staff", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_staff_registry():
    """Get staff registry."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    staff = []
    if not db_url:
        return jsonify({"staff": staff})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS hd_staff_registry (
                id SERIAL PRIMARY KEY, user_id INTEGER, full_name VARCHAR(200) NOT NULL,
                role VARCHAR(30) NOT NULL, license_number VARCHAR(50), license_expiry DATE,
                specialty VARCHAR(100), hd_training_course VARCHAR(200), hd_training_year INTEGER,
                cme_hours_this_year NUMERIC(5,1) DEFAULT 0, hbv_vaccine_status VARCHAR(30),
                phone VARCHAR(20), is_active BOOLEAN DEFAULT TRUE, created_at TIMESTAMP DEFAULT NOW())""")
            conn.commit()
            cur.execute("""SELECT id, full_name, role, license_number, license_expiry, specialty,
                           hd_training_course, hd_training_year, cme_hours_this_year, hbv_vaccine_status, phone, is_active
                    FROM hd_staff_registry WHERE is_active = TRUE ORDER BY role, full_name""")
            for r in cur.fetchall():
                staff.append({
                    "id": r[0], "name": r[1], "role": r[2], "license": r[3],
                    "license_expiry": r[4].isoformat() if r[4] else None,
                    "specialty": r[5], "training": r[6], "training_year": r[7],
                    "cme_hours": float(r[8]) if r[8] else 0, "hbv_status": r[9],
                    "phone": r[10], "active": r[11],
                })
        conn.close()
    except Exception as e:
        logger.warning("staff_registry GET: %s", e)
    return jsonify({"staff": staff})


@hd_bp.route("/clinic-profile/staff", methods=["POST"])
@require_role("physician", "admin")
def save_staff():
    """Add/update staff member."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "No database"}), 503
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name")
    role = data.get("role")
    if not name or not role:
        return jsonify({"error": "name and role required"}), 400
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            staff_id = data.get("id")
            if staff_id:
                cur.execute("""UPDATE hd_staff_registry SET full_name=%s, role=%s, license_number=%s,
                    license_expiry=%s, specialty=%s, hd_training_course=%s, hd_training_year=%s,
                    cme_hours_this_year=%s, hbv_vaccine_status=%s, phone=%s WHERE id=%s""",
                    (name, role, data.get("license"), data.get("license_expiry"),
                     data.get("specialty"), data.get("training"), data.get("training_year"),
                     data.get("cme_hours", 0), data.get("hbv_status"), data.get("phone"), staff_id))
            else:
                cur.execute("""INSERT INTO hd_staff_registry (full_name, role, license_number, license_expiry,
                    specialty, hd_training_course, hd_training_year, cme_hours_this_year, hbv_vaccine_status, phone)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (name, role, data.get("license"), data.get("license_expiry"),
                     data.get("specialty"), data.get("training"), data.get("training_year"),
                     data.get("cme_hours", 0), data.get("hbv_status"), data.get("phone")))
                staff_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"saved": True, "id": staff_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/staff/<int:staff_id>", methods=["DELETE"])
@require_role("physician", "admin")
def delete_staff(staff_id):
    """Delete staff member by ID."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM hd_staff_registry WHERE id = %s", (staff_id,))
            conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        logger.warning("staff DELETE: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/equipment", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_equipment():
    """Get equipment registry."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    equipment = []
    if not db_url:
        return jsonify({"equipment": equipment})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS hd_equipment (
                id SERIAL PRIMARY KEY, equipment_type VARCHAR(30) NOT NULL, brand VARCHAR(100),
                model VARCHAR(100), serial_number VARCHAR(100), bed_number INTEGER,
                purchase_date DATE, status VARCHAR(20) DEFAULT 'active', last_pm_date DATE,
                next_pm_date DATE, pm_interval_days INTEGER DEFAULT 90, notes VARCHAR(300),
                created_at TIMESTAMP DEFAULT NOW())""")
            conn.commit()
            cur.execute("""SELECT id, equipment_type, brand, model, serial_number, bed_number,
                    purchase_date, status, last_pm_date, next_pm_date, notes, purchase_year, contract_end, contract_vendor
                    FROM hd_equipment ORDER BY equipment_type, bed_number""")
            today = (datetime.now(timezone.utc) + timedelta(hours=7)).date()
            for r in cur.fetchall():
                ce = r[12]
                equipment.append({
                    "id": r[0], "type": r[1], "brand": r[2], "model": r[3], "serial": r[4],
                    "bed": r[5], "purchase_date": r[6].isoformat() if r[6] else None,
                    "status": r[7], "last_pm": r[8].isoformat() if r[8] else None,
                    "next_pm": r[9].isoformat() if r[9] else None, "notes": r[10],
                    "purchase_year": r[11], "contract_end": ce.isoformat() if ce else None, "contract_vendor": r[13],
                    "contract_days_left": (ce - today).days if ce else None,
                })
        conn.close()
    except Exception as e:
        logger.warning("equipment GET: %s", e)
    return jsonify({"equipment": equipment})


@hd_bp.route("/clinic-profile/equipment", methods=["POST"])
@require_role("physician", "nurse", "admin")
def save_equipment():
    """Add or update equipment."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    data = request.get_json(force=True)
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS hd_equipment (
                id SERIAL PRIMARY KEY, equipment_type VARCHAR(30) NOT NULL, brand VARCHAR(100),
                model VARCHAR(100), serial_number VARCHAR(100), bed_number INTEGER,
                purchase_date DATE, status VARCHAR(20) DEFAULT 'active', last_pm_date DATE,
                next_pm_date DATE, pm_interval_days INTEGER DEFAULT 90, notes VARCHAR(300),
                created_at TIMESTAMP DEFAULT NOW())""")
            eid = data.get("id")
            py = data.get("purchase_year")
            try:
                py = int(py) if py not in (None, "") else None
                if py and py > 2400:
                    py -= 543          # พ.ศ. → ค.ศ.
            except (TypeError, ValueError):
                py = None
            if eid:
                cur.execute("""UPDATE hd_equipment SET equipment_type=%s, brand=%s, model=%s,
                    serial_number=%s, bed_number=%s, status=%s, last_pm_date=%s, next_pm_date=%s, notes=%s,
                    purchase_year=%s, contract_end=%s, contract_vendor=%s
                    WHERE id=%s""",
                    (data.get("type","dialysis_machine"), data.get("brand"), data.get("model"),
                     data.get("serial"), data.get("bed"), data.get("status","active"),
                     data.get("last_pm") or None, data.get("next_pm") or None,
                     data.get("notes"), py, data.get("contract_end") or None, data.get("contract_vendor") or None, eid))
            else:
                cur.execute("""INSERT INTO hd_equipment (equipment_type, brand, model, serial_number,
                    bed_number, status, last_pm_date, next_pm_date, notes, purchase_year, contract_end, contract_vendor)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (data.get("type","dialysis_machine"), data.get("brand"), data.get("model"),
                     data.get("serial"), data.get("bed"), data.get("status","active"),
                     data.get("last_pm") or None, data.get("next_pm") or None,
                     data.get("notes"), py, data.get("contract_end") or None, data.get("contract_vendor") or None))
                eid = cur.fetchone()[0]
            conn.commit()
        conn.close()
        return jsonify({"ok": True, "id": eid})
    except Exception as e:
        logger.warning("equipment POST: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/equipment/import", methods=["POST"])
@require_role("physician", "admin")
def import_equipment():
    """นำเข้าทะเบียนเครื่องจากไฟล์ CSV/XLSX (คอลัมน์: เครื่องที่/เตียง, ยี่ห้อ, รุ่น, รหัสเครื่อง/S/N, ปีที่ซื้อ, สัญญาสิ้นสุด[, ประเภท, สถานะ]).
    จับคู่ด้วย Serial Number: มีอยู่แล้ว → อัปเดต, ไม่มี → เพิ่ม · ?dry_run=1 แสดงผลก่อน"""
    import os
    import psycopg2
    from .equipment_import import parse_file
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "ไม่มีไฟล์"}), 400
    try:
        rows, errors = parse_file(f.filename, f.read())
    except Exception as e:
        return jsonify({"error": f"อ่านไฟล์ไม่ได้: {e}"}), 400
    if not rows:
        return jsonify({"error": "; ".join(errors) or "ไม่พบแถวเครื่องในไฟล์"}), 400
    dry = request.args.get("dry_run") == "1"
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    conn = psycopg2.connect(db_url, connect_timeout=5)
    ins = upd = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, serial_number FROM hd_equipment WHERE serial_number IS NOT NULL")
            by_sn = {str(sn).strip().upper(): eid for eid, sn in cur.fetchall()}
            for r in rows:
                eid = by_sn.get(r["serial"].upper()) if r.get("serial") else None
                r["action"] = "update" if eid else "insert"
                if dry:
                    continue
                if eid:
                    cur.execute("""UPDATE hd_equipment SET brand=%s, model=%s, bed_number=%s, purchase_year=%s, contract_end=%s,
                                   contract_vendor=COALESCE(%s, contract_vendor), equipment_type=%s,
                                   status=COALESCE(%s, status), notes=COALESCE(%s, notes) WHERE id=%s""",
                                (r["brand"], r["model"], r["bed"], r["purchase_year"], r["contract_end"], r.get("vendor"),
                                 r["type"], r.get("status"), r.get("notes"), eid))
                    upd += 1
                else:
                    cur.execute("""INSERT INTO hd_equipment (equipment_type, brand, model, serial_number, bed_number, status,
                                   purchase_year, contract_end, contract_vendor, notes)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                (r["type"], r["brand"], r["model"], r["serial"], r["bed"], r.get("status") or "active",
                                 r["purchase_year"], r["contract_end"], r.get("vendor"), r.get("notes")))
                    ins += 1
            if not dry:
                conn.commit()
    finally:
        conn.close()
    if not dry:
        log_action(user_id=g.get("hd_user_id"), action="equipment.import", resource_type="equipment", resource_id=None,
                   new_values={"file": f.filename, "inserted": ins, "updated": upd}, ip_address=request.remote_addr or "")
    return jsonify({"ok": True, "dry_run": dry, "rows": rows, "inserted": ins, "updated": upd,
                    "to_insert": sum(1 for r in rows if r["action"] == "insert"), "to_update": sum(1 for r in rows if r["action"] == "update"),
                    "errors": errors})


@hd_bp.route("/clinic-profile/equipment/<int:equip_id>", methods=["DELETE"])
@require_role("physician", "admin")
def delete_equipment(equip_id):
    """Delete equipment by ID."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM hd_equipment WHERE id = %s", (equip_id,))
            conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        logger.warning("equipment DELETE: %s", e)
        return jsonify({"error": str(e)}), 500


# ═══ Machine-Bed Mapping API (18 พ.ย. 2569) ═══════════════════

@hd_bp.route("/bed-machines", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_bed_machines():
    """ดึง mapping เตียง ↔ เครื่องฟอก (เฉพาะ dialysis_machine ที่ active)."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    mapping = {}  # {bed_number: {id, brand, model, serial, status}}
    if not db_url:
        return jsonify({"mapping": mapping, "total_beds": HdConfig.TOTAL_BEDS})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""SELECT id, brand, model, serial_number, bed_number, status
                           FROM hd_equipment
                           WHERE equipment_type = 'dialysis_machine' AND bed_number IS NOT NULL
                           ORDER BY bed_number""")
            for r in cur.fetchall():
                mapping[str(r[4])] = {"id": r[0], "brand": r[1], "model": r[2],
                                      "serial": r[3], "bed": r[4], "status": r[5]}
        conn.close()
    except Exception as e:
        logger.warning("bed-machines GET: %s", e)
    return jsonify({"mapping": mapping, "total_beds": HdConfig.TOTAL_BEDS})


@hd_bp.route("/bed-machines/<int:bed_number>", methods=["PUT"])
@require_role("physician", "admin")
def set_bed_machine(bed_number):
    """ผูก/ถอดเครื่องฟอกกับเตียง. Body: {equipment_id: N} หรือ {equipment_id: null} เพื่อถอด."""
    import os, psycopg2
    if bed_number < 1 or bed_number > HdConfig.TOTAL_BEDS:
        return jsonify({"error": f"เตียงต้องอยู่ระหว่าง 1-{HdConfig.TOTAL_BEDS}"}), 400
    data = request.get_json(silent=True) or {}
    eq_id = data.get("equipment_id")
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            if eq_id is None:
                # ถอดเครื่องออกจากเตียง
                cur.execute("UPDATE hd_equipment SET bed_number = NULL WHERE bed_number = %s AND equipment_type = 'dialysis_machine'", (bed_number,))
            else:
                # ตรวจว่าเครื่องมีอยู่จริง
                cur.execute("SELECT id, bed_number FROM hd_equipment WHERE id = %s AND equipment_type = 'dialysis_machine'", (eq_id,))
                eq = cur.fetchone()
                if not eq:
                    conn.close()
                    return jsonify({"error": "ไม่พบเครื่องฟอกนี้ในทะเบียน"}), 404
                # ถอดเครื่องเดิมออกจากเตียงนี้
                cur.execute("UPDATE hd_equipment SET bed_number = NULL WHERE bed_number = %s AND equipment_type = 'dialysis_machine'", (bed_number,))
                # ย้ายเครื่องใหม่มาเตียงนี้
                cur.execute("UPDATE hd_equipment SET bed_number = %s WHERE id = %s", (bed_number, eq_id))
            conn.commit()
        conn.close()
        log_action(user_id=g.get("hd_user_id"), action="bed_machine.set",
                   resource_type="bed", resource_id=bed_number,
                   new_values={"equipment_id": eq_id}, ip_address=request.remote_addr or "")
        return jsonify({"ok": True, "bed": bed_number, "equipment_id": eq_id})
    except Exception as e:
        logger.warning("bed-machine PUT: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/water-quality/<int:wq_id>", methods=["DELETE"])
@require_role("physician", "admin")
def delete_water_quality(wq_id):
    """Delete water quality log by ID."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM hd_water_quality WHERE id = %s", (wq_id,))
            # B12: the attached result file lives in hd_sop_files (sop_key water_quality_<id>) — remove it too
            try:
                cur.execute("SAVEPOINT wq_files")
                cur.execute("DELETE FROM hd_sop_files WHERE sop_key = %s", (f"water_quality_{wq_id}",))
                removed = cur.rowcount
                cur.execute("RELEASE SAVEPOINT wq_files")
            except Exception as fe:
                cur.execute("ROLLBACK TO SAVEPOINT wq_files")
                removed = 0
                logger.warning("water_quality DELETE files: %s", fe)
            conn.commit()
        conn.close()
        log_action(user_id=g.get("hd_user_id"), action="water_quality.delete", resource_type="water_quality",
                   resource_id=wq_id, new_values={"files_removed": removed}, ip_address=request.remote_addr or "")
        return jsonify({"ok": True, "files_removed": removed})
    except Exception as e:
        logger.warning("water_quality DELETE: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/incidents/<int:inc_id>", methods=["DELETE"])
@require_role("physician", "admin")
def delete_incident(inc_id):
    """Delete incident by ID."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM hd_incidents WHERE id = %s", (inc_id,))
            conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        logger.warning("incidents DELETE: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/water-quality", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_water_quality():
    """Get water quality test logs."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    logs = []
    if not db_url:
        return jsonify({"logs": logs})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS hd_water_quality (
                id SERIAL PRIMARY KEY, test_date DATE NOT NULL, test_type VARCHAR(30) NOT NULL,
                parameter VARCHAR(100), result_value VARCHAR(100), unit VARCHAR(30),
                pass_fail VARCHAR(10) DEFAULT 'pass', method VARCHAR(100),
                tested_by VARCHAR(100), notes VARCHAR(300),
                created_at TIMESTAMP DEFAULT NOW())""")
            conn.commit()
            # Check which columns exist
            cur.execute("""SELECT column_name FROM information_schema.columns
                WHERE table_name='hd_water_quality'""")
            existing_cols = {r[0] for r in cur.fetchall()}

            # Build SELECT dynamically from what actually exists
            col_list = ['id', 'test_date', 'test_type']
            # These may or may not exist depending on migration version
            for c in ['parameter','result_value','unit','pass_fail','method','tested_by',
                       'lab_name','notes','send_date','file_name']:
                if c in existing_cols:
                    col_list.append(c)

            cur.execute(f"SELECT {','.join(col_list)} FROM hd_water_quality ORDER BY test_date DESC LIMIT 50")
            wq_rows = cur.fetchall()

            # Check which water_quality IDs have files in hd_sop_files
            file_map = {}
            try:
                cur.execute("""SELECT sop_key, filename FROM hd_sop_files WHERE sop_key LIKE 'water_quality_%'""")
                for fr in cur.fetchall():
                    wq_id_str = fr[0].replace('water_quality_', '')
                    try:
                        file_map[int(wq_id_str)] = fr[1]
                    except ValueError:
                        pass
            except Exception:
                pass

            for r in wq_rows:
                row = dict(zip(col_list, r))
                wq_id = row.get('id')
                entry = {
                    "id": wq_id,
                    "date": row['test_date'].isoformat() if row.get('test_date') else None,
                    "type": row.get('test_type'),
                    "parameter": row.get('parameter',''),
                    "value": row.get('result_value',''),
                    "unit": row.get('unit',''),
                    "pass_fail": row.get('pass_fail','pass'),
                    "method": row.get('method',''),
                    "tested_by": row.get('tested_by') or row.get('lab_name',''),
                    "notes": row.get('notes',''),
                    "send_date": row['send_date'].isoformat() if row.get('send_date') else None,
                    "file_id": wq_id if wq_id in file_map else None,
                    "file_name": file_map.get(wq_id),
                }
                logs.append(entry)
        conn.close()
    except Exception as e:
        logger.warning("water_quality GET: %s", e)
    return jsonify({"logs": logs})


@hd_bp.route("/clinic-profile/water-quality", methods=["POST"])
@require_role("physician", "nurse", "admin")
def save_water_quality():
    """Add water quality test result (supports JSON or multipart with file)."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500

    # Handle both JSON and FormData
    if request.content_type and 'multipart' in request.content_type:
        data = request.form.to_dict()
    else:
        data = request.get_json(force=True) or {}

    uploaded_file = request.files.get("file")
    has_file = False

    try:
        # Schema fixed by boot script in __init__.py
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Check actual columns
            cur.execute("""SELECT column_name FROM information_schema.columns
                WHERE table_name='hd_water_quality'""")
            existing = {r[0] for r in cur.fetchall()}
            logger.info("water_quality columns: %s", existing)

            # Build INSERT with only existing columns
            col_map = [
                ('test_date', data.get("date")),
                ('test_type', data.get("type", "bacteria")),
            ]
            # Use whatever columns exist
            if 'result_value' in existing:
                col_map.append(('result_value', data.get("value")))
            if 'unit' in existing:
                col_map.append(('unit', data.get("unit")))
            if 'pass_fail' in existing:
                col_map.append(('pass_fail', data.get("pass_fail", "pass")))
            if 'lab_name' in existing:
                col_map.append(('lab_name', data.get("tested_by")))
            if 'notes' in existing:
                col_map.append(('notes', data.get("notes")))
            # These columns may or may not exist
            if 'parameter' in existing:
                col_map.append(('parameter', data.get("parameter")))
            if 'method' in existing:
                col_map.append(('method', data.get("method")))
            if 'tested_by' in existing:
                col_map.append(('tested_by', data.get("tested_by")))
            if 'send_date' in existing:
                col_map.append(('send_date', data.get("send_date") or None))

            cols_str = ', '.join(c[0] for c in col_map)
            vals_str = ', '.join(['%s'] * len(col_map))
            vals = tuple(c[1] for c in col_map)

            cur.execute(f"INSERT INTO hd_water_quality ({cols_str}) VALUES ({vals_str}) RETURNING id", vals)
            new_id = cur.fetchone()[0]
            conn.commit()

            # Save file separately in hd_sop_files (reuse as general file storage)
            if uploaded_file and uploaded_file.filename:
                file_data_bin = uploaded_file.read()
                if len(file_data_bin) > 10 * 1024 * 1024:
                    return jsonify({"error": "ไฟล์ใหญ่เกิน 10 MB"}), 400
                cur.execute("""CREATE TABLE IF NOT EXISTS hd_sop_files (
                    id SERIAL PRIMARY KEY, sop_key VARCHAR(80) NOT NULL,
                    filename VARCHAR(255) NOT NULL, content_type VARCHAR(100),
                    file_size INTEGER, file_data BYTEA NOT NULL,
                    uploaded_by VARCHAR(100), uploaded_at TIMESTAMP DEFAULT NOW())""")
                cur.execute("""INSERT INTO hd_sop_files (sop_key, filename, content_type, file_size, file_data, uploaded_by)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (f"water_quality_{new_id}", uploaded_file.filename, uploaded_file.content_type,
                     len(file_data_bin), psycopg2.Binary(file_data_bin), data.get("tested_by","")))
                file_id = cur.fetchone()[0]
                conn.commit()
                has_file = True
                logger.info("water_quality file saved: wq_id=%d file_id=%d name=%s", new_id, file_id, uploaded_file.filename)

            logger.info("water_quality saved: id=%d has_file=%s", new_id, has_file)
        conn.close()
        return jsonify({"ok": True, "id": new_id, "has_file": has_file})
    except Exception as e:
        logger.warning("water_quality POST: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/water-quality/<int:wq_id>/file", methods=["GET"])
def download_water_quality_file(wq_id):
    """Download water quality file — no auth required (clinic admin docs, not patient data)."""
    import os, psycopg2
    from flask import Response
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            # Files stored in hd_sop_files with sop_key = 'water_quality_<id>'
            cur.execute("""SELECT filename, content_type, file_data FROM hd_sop_files
                WHERE sop_key = %s ORDER BY id DESC LIMIT 1""", (f"water_quality_{wq_id}",))
            r = cur.fetchone()
        conn.close()
        if not r or not r[2]:
            return jsonify({"error": "ไม่พบไฟล์"}), 404
        resp = Response(bytes(r[2]), content_type=r[1] or 'application/octet-stream')
        resp.headers["Content-Disposition"] = _content_disposition(r[0], "water_quality")
        resp.headers["Content-Length"] = str(len(bytes(r[2])))
        return resp
    except Exception as e:
        logger.warning("water_quality file download: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/incidents", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_incidents():
    """Get incident reports."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    incidents = []
    if not db_url:
        return jsonify({"incidents": incidents})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS hd_incidents (
                id SERIAL PRIMARY KEY, incident_date TIMESTAMP NOT NULL,
                incident_type VARCHAR(30) NOT NULL, severity VARCHAR(20) DEFAULT 'minor',
                description TEXT, action_taken TEXT, reported_by VARCHAR(100),
                status VARCHAR(20) DEFAULT 'open', resolved_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW())""")
            conn.commit()
            cur.execute("""SELECT id, incident_date, incident_type, severity, description,
                action_taken, reported_by, status, resolved_at
                FROM hd_incidents ORDER BY incident_date DESC LIMIT 50""")
            for r in cur.fetchall():
                incidents.append({
                    "id": r[0], "date": r[1].isoformat()+"Z" if r[1] else None,
                    "type": r[2], "severity": r[3], "description": r[4],
                    "action": r[5], "reported_by": r[6], "status": r[7],
                    "resolved_at": r[8].isoformat()+"Z" if r[8] else None,
                })
        conn.close()
    except Exception as e:
        logger.warning("incidents GET: %s", e)
    return jsonify({"incidents": incidents})


@hd_bp.route("/clinic-profile/incidents", methods=["POST"])
@require_role("physician", "nurse", "admin")
def save_incident():
    """Report new incident."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    data = request.get_json(force=True)
    try:
        inc_date = data.get("date") or _thai_today().isoformat()
        reported_name = data.get("reported_by", "")

        # Use only columns that exist (boot script adds missing ones)
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""SELECT column_name FROM information_schema.columns
                WHERE table_name='hd_incidents'""")
            cols = {r[0] for r in cur.fetchall()}

            col_map = [('incident_date', inc_date),
                       ('incident_type', data.get("type", "other")),
                       ('severity', data.get("severity", "minor")),
                       ('status', 'open')]
            if 'description' in cols:
                col_map.append(('description', data.get("description")))
            if 'action_taken' in cols:
                col_map.append(('action_taken', data.get("action")))
            if 'reported_by' in cols:
                col_map.append(('reported_by', reported_name))

            col_names = ', '.join(c[0] for c in col_map)
            placeholders = ', '.join(['%s'] * len(col_map))
            values = tuple(c[1] for c in col_map)
            cur.execute(f"INSERT INTO hd_incidents ({col_names}) VALUES ({placeholders}) RETURNING id", values)
            new_id = cur.fetchone()[0]
            conn.commit()
        conn.close()

        # ── Send LINE alert ──
        try:
            sev_map = {"minor": "🟢 เล็กน้อย", "moderate": "🟡 ปานกลาง",
                       "major": "🔴 รุนแรง", "sentinel": "🚨 Sentinel"}
            type_map = {"needle_stick": "เข็มตำ", "blood_spill": "เลือดหก",
                        "idh": "IDH", "fall": "พลัดตก", "air_embolism": "Air Embolism",
                        "dialyzer_rupture": "Dialyzer Rupture", "blood_leak": "Blood Leak",
                        "clotting": "Clotting", "allergic": "แพ้ยา", "infection": "ติดเชื้อ"}
            msg = (f"\n🛡 รายงานอุบัติการณ์ HD\n"
                   f"━━━━━━━━━━━\n"
                   f"ประเภท: {type_map.get(data.get('type'), data.get('type','อื่นๆ'))}\n"
                   f"ความรุนแรง: {sev_map.get(data.get('severity'), data.get('severity'))}\n"
                   f"รายละเอียด: {data.get('description','')[:100]}\n"
                   f"จัดการ: {data.get('action','')[:100]}\n"
                   f"ผู้รายงาน: {data.get('reported_by','')}\n"
                   f"━━━━━━━━━━━")
            sent = False
            line_err = ""
            # Method 1: LINE Push (if configured via LINE Messaging API)
            from flask import current_app
            push_fn = current_app.config.get("LINE_PUSH_FN")
            if push_fn:
                alert_ids = get_setting_value("alert_recipients", "")
                if not alert_ids:
                    alert_ids = get_setting_value("physician_line_id", "")
                if not alert_ids:
                    # Hardcoded fallback — Dr. Kamphon
                    alert_ids = "U1b1068cde88a06e88e53c014d1b83824"
                recipients = [r.strip() for r in alert_ids.split(",") if r.strip()] if alert_ids else []
                logger.info("incident LINE push: push_fn=%s, recipients=%d", bool(push_fn), len(recipients))
                for r in recipients:
                    try:
                        result = push_fn(r, msg)
                        logger.info("incident LINE push to %s: result=%s", r[:10], result)
                        sent = True
                    except Exception as pe:
                        line_err = str(pe)
                        logger.warning("incident LINE push failed: %s", pe)
            else:
                line_err = "LINE_PUSH_FN not configured"
                logger.warning("incident LINE: push_fn not available")
            # Also post to the nurse group (Messaging API) when configured
            try:
                gr = push_nurse_group(msg, fallback_recipients=False)
                if gr["sent"]:
                    sent = True
            except Exception as ne:
                logger.warning("incident nurse group: %s", ne)
            logger.info("incident LINE alert sent=%s", sent)
        except Exception as e2:
            logger.warning("incident LINE notify: %s", e2)

        return jsonify({"ok": True, "id": new_id, "line_sent": sent, "line_err": line_err if not sent else ""})
    except Exception as e:
        logger.warning("incidents POST: %s", e)
        return jsonify({"error": str(e)}), 500


# ── SOP Files (upload/download) ─────────────────────────

@hd_bp.route("/clinic-profile/sop-files", methods=["GET"])
@require_role("physician", "nurse", "admin")
def list_sop_files():
    """List SOP files, optionally filtered by sop_key."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    files = []
    if not db_url:
        return jsonify({"files": files})
    sop_key = request.args.get("sop_key", "")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS hd_sop_files (
                id SERIAL PRIMARY KEY, sop_key VARCHAR(80) NOT NULL,
                filename VARCHAR(255) NOT NULL, content_type VARCHAR(100),
                file_size INTEGER, file_data BYTEA NOT NULL,
                uploaded_by VARCHAR(100), uploaded_at TIMESTAMP DEFAULT NOW())""")
            conn.commit()
            if sop_key:
                cur.execute("""SELECT id, sop_key, filename, content_type, file_size, uploaded_by, uploaded_at
                    FROM hd_sop_files WHERE sop_key = %s ORDER BY uploaded_at DESC""", (sop_key,))
            else:
                cur.execute("""SELECT id, sop_key, filename, content_type, file_size, uploaded_by, uploaded_at
                    FROM hd_sop_files ORDER BY uploaded_at DESC""")
            for r in cur.fetchall():
                files.append({
                    "id": r[0], "sop_key": r[1], "filename": r[2],
                    "content_type": r[3], "size": r[4], "uploaded_by": r[5],
                    "uploaded_at": r[6].isoformat() + "Z" if r[6] else None,
                })
        conn.close()
    except Exception as e:
        logger.warning("sop_files GET: %s", e)
    return jsonify({"files": files})


@hd_bp.route("/clinic-profile/sop-files", methods=["POST"])
@require_role("physician", "admin")
def upload_sop_file():
    """Upload a file for an SOP."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500

    sop_key = request.form.get("sop_key", "")
    uploaded_by = request.form.get("uploaded_by", "")
    f = request.files.get("file")
    if not f or not sop_key:
        return jsonify({"error": "sop_key and file required"}), 400

    filename = f.filename or "unknown"
    content_type = f.content_type or "application/octet-stream"
    file_data = f.read()
    file_size = len(file_data)

    # Limit 10 MB
    if file_size > 10 * 1024 * 1024:
        return jsonify({"error": "ไฟล์ใหญ่เกิน 10 MB"}), 400

    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS hd_sop_files (
                id SERIAL PRIMARY KEY, sop_key VARCHAR(80) NOT NULL,
                filename VARCHAR(255) NOT NULL, content_type VARCHAR(100),
                file_size INTEGER, file_data BYTEA NOT NULL,
                uploaded_by VARCHAR(100), uploaded_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""INSERT INTO hd_sop_files (sop_key, filename, content_type, file_size, file_data, uploaded_by)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (sop_key, filename, content_type, file_size, psycopg2.Binary(file_data), uploaded_by))
            new_id = cur.fetchone()[0]
            conn.commit()
        conn.close()
        return jsonify({"ok": True, "id": new_id, "filename": filename, "size": file_size})
    except Exception as e:
        logger.warning("sop_files POST: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/sop-files/<int:file_id>/download", methods=["GET"])
def download_sop_file(file_id):
    """Download SOP file — no auth required (clinic admin docs, not patient data)."""
    import os, psycopg2
    from flask import Response
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT filename, content_type, file_data FROM hd_sop_files WHERE id = %s", (file_id,))
            r = cur.fetchone()
        conn.close()
        if not r:
            return jsonify({"error": "not found"}), 404
        filename, ctype, data = r[0], r[1], bytes(r[2])
        resp = Response(data, content_type=ctype)
        resp.headers["Content-Disposition"] = _content_disposition(filename, "sop_file")
        resp.headers["Content-Length"] = str(len(data))
        return resp
    except Exception as e:
        logger.warning("sop_files download: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/sop-files/<int:file_id>", methods=["DELETE"])
@require_role("physician", "admin")
def delete_sop_file(file_id):
    """Delete a SOP file."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM hd_sop_files WHERE id = %s", (file_id,))
            conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        logger.warning("sop_files DELETE: %s", e)
        return jsonify({"error": str(e)}), 500


@hd_bp.route("/clinic-profile/self-assessment", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_self_assessment():
    """Get self-assessment checklist data."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    items = []
    if not db_url:
        return jsonify({"items": items})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS hd_self_assessment (
                id SERIAL PRIMARY KEY, category VARCHAR(100) NOT NULL,
                item_key VARCHAR(100) NOT NULL, checked BOOLEAN DEFAULT FALSE,
                evidence TEXT, assessed_by VARCHAR(100), assessed_at TIMESTAMP,
                UNIQUE(category, item_key))""")
            conn.commit()
            cur.execute("SELECT category, item_key, checked, evidence, assessed_by, assessed_at FROM hd_self_assessment ORDER BY id")
            for r in cur.fetchall():
                items.append({"category": r[0], "key": r[1], "checked": r[2], "evidence": r[3], "assessed_by": r[4],
                              "assessed_at": (r[5].isoformat() + "Z") if r[5] else None})
        conn.close()
    except Exception as e:
        logger.warning("self_assessment GET: %s", e)
    return jsonify({"items": items})


@hd_bp.route("/clinic-profile/self-assessment", methods=["POST"])
@require_role("physician", "nurse", "admin")
def save_self_assessment():
    """Save self-assessment checklist."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no db"}), 500
    data = request.get_json(force=True)
    items = data.get("items", [])
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS hd_self_assessment (
                id SERIAL PRIMARY KEY, category VARCHAR(100) NOT NULL,
                item_key VARCHAR(100) NOT NULL, checked BOOLEAN DEFAULT FALSE,
                evidence TEXT, assessed_by VARCHAR(100), assessed_at TIMESTAMP,
                UNIQUE(category, item_key))""")
            who = g.get("hd_username") or str(g.get("hd_user_id") or "")
            for item in items:
                cur.execute("""INSERT INTO hd_self_assessment (category, item_key, checked, assessed_by, assessed_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (category, item_key) DO UPDATE SET checked = EXCLUDED.checked,
                        assessed_by = EXCLUDED.assessed_by, assessed_at = NOW()""",
                    (item.get("category"), item.get("key"), bool(item.get("checked", False)), who))
            conn.commit()
        conn.close()
        log_action(user_id=g.get("hd_user_id"), action="self_assessment.save", resource_type="clinic_profile",
                   new_values={"items": len(items), "checked": sum(1 for i in items if i.get("checked"))},
                   ip_address=request.remote_addr or "")
        return jsonify({"ok": True, "saved": len(items)})
    except Exception as e:
        logger.warning("self_assessment POST: %s", e)
        return jsonify({"error": str(e)}), 500
