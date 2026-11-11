"""HD Smart Monitoring — Flask Blueprint.

All routes under /hd/api/v1/.  Completely isolated from server.py.
"""

import logging
from datetime import date, datetime, timezone  # noqa: F401
from flask import (  # noqa: F401
    Blueprint, request, jsonify, g, render_template, redirect, url_for,
    current_app, make_response,
)

from .auth import require_role  # noqa: F401
from .audit import log_action
from .config import HdConfig

logger = logging.getLogger(__name__)

# ── Thai Timezone Helper ─────────────────────────────────
# Render server runs in UTC. All "today" calculations must use Thai time.
_THAI_TZ = timezone(offset=__import__('datetime').timedelta(hours=7))


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
    resp = make_response(render_template("hd_dashboard.html", config=HdConfig))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


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
    """QR Check-in handler — redirect to session."""
    from .qr_checkin import verify_qr_token
    bed = request.args.get("bed", type=int)
    token = request.args.get("token", "")
    if bed and token and verify_qr_token(bed, token):
        return redirect(f"/hd/session/new?bed={bed}")
    return "<h2>❌ QR Code ไม่ถูกต้องหรือหมดอายุ</h2><a href='/hd/dashboard'>กลับ Dashboard</a>", 400


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


@hd_web.route("/labs/<hn>")
def hd_labs_page(hn):
    """Lab Results page for a patient."""
    return render_template("hd_labs.html", patient_hn=hn)


@hd_web.route("/sheet/<int:session_id>")
def hd_sheet_page(session_id):
    """HD Monitoring Sheet page."""
    return render_template("hd_sheet.html", session_id=session_id)


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
    """Login and receive JWT token."""
    import os
    from .auth import verify_password, create_token
    from .config import HdConfig

    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "Database not configured"}), 503

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
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

        if not verify_password(password, pw_hash):
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
        logger.error("HD login error: %s", e)
        return jsonify({"error": "ระบบขัดข้อง กรุณาลองใหม่"}), 500


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


@hd_bp.route("/sessions", methods=["POST"])
@require_role("physician", "nurse")
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
            cur.execute("""
                SELECT id, status FROM hd_sessions
                WHERE bed_number = %s
                  AND session_date = %s
                  AND (shift = %s OR (shift IS NULL AND %s = 'morning'))
                  AND status IN ('scheduled', 'in_progress')
                ORDER BY status
            """, (bed, _thai_today().isoformat(), shift, shift))
            existing = cur.fetchall()
            for ex_id, ex_status in existing:
                if init_status == "in_progress" and ex_status == "in_progress":
                    conn.close()
                    return jsonify({"error": f"เตียง {bed} มีคนไข้กำลังฟอกอยู่"}), 409
                if init_status == "scheduled" and ex_status == "scheduled":
                    conn.close()
                    return jsonify({"error": f"เตียง {bed} มีการจองรอบถัดไปแล้ว"}), 409

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

            cur.execute(f"""
                INSERT INTO hd_sessions
                    (patient_id, bed_number, session_date, status,
                     pre_weight, post_weight, uf_goal,
                     started_at, nurse_id, shift)
                VALUES (%s, %s, %s, '{init_status}',
                        %s, NULL, %s,
                        {started}, %s, %s)
                RETURNING id
            """, (
                patient_id, bed,
                _thai_today().isoformat(),
                pre_w,
                uf_goal,
                g.get("hd_user_id"),
                shift,
            ))
            session_id = cur.fetchone()[0]

            # Save Dialyzer tracking (upsert active dialyzer)
            d_type = data.get("dialyzer_type")
            if d_type:
                d_reuse = data.get("dialyzer_reuse", 1)
                d_max = data.get("dialyzer_max", 20)
                try:
                    # Check if active dialyzer exists for this patient
                    cur.execute("""
                        SELECT id FROM hd_dialyzer_tracking
                        WHERE patient_id = %s AND status = 'active'
                        ORDER BY registered_at DESC LIMIT 1
                    """, (patient_id,))
                    existing = cur.fetchone()
                    if existing:
                        cur.execute("""
                            UPDATE hd_dialyzer_tracking
                            SET dialyzer_type = %s, use_count = %s, max_uses = %s,
                                last_used_session = %s
                            WHERE id = %s
                        """, (d_type, d_reuse, d_max, session_id, existing[0]))
                    else:
                        cur.execute("""
                            INSERT INTO hd_dialyzer_tracking
                                (patient_id, dialyzer_type, use_count, max_uses,
                                 status, last_used_session, registered_at)
                            VALUES (%s, %s, %s, %s, 'active', %s, NOW())
                        """, (patient_id, d_type, d_reuse, d_max, session_id))
                except Exception as e:
                    logger.warning("dialyzer save error: %s", e)

            # Save Heparin protocol
            hep_drug = data.get("hep_drug")
            if hep_drug:
                try:
                    cur.execute("""
                        INSERT INTO hd_anticoagulation
                            (session_id, drug, bolus_dose, maintenance_dose,
                             recorded_by, recorded_at)
                        VALUES (%s, %s, %s, %s, %s, NOW())
                    """, (session_id, hep_drug,
                          data.get("hep_bolus"),
                          data.get("hep_maintenance"),
                          g.get("hd_user_id")))
                except Exception as e:
                    logger.warning("heparin save error: %s", e)

            # Update VA + VA Location on patient record if provided
            va = data.get("vascular_access")
            va_loc = data.get("va_location")
            if va or va_loc:
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
                except Exception as e:
                    logger.warning("VA update error: %s", e)

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

        return jsonify({
            "status": "created",
            "session_id": session_id,
            "bed_number": bed,
            "patient_hn": data["patient_hn"],
        }), 201

    except Exception as e:
        logger.error("create_session error: %s", e)
        return jsonify({"error": str(e)}), 500


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
                       p.dry_weight, p.esa_drug
                FROM hd_sessions s
                JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE s.id = %s
            """, (session_id,))
            row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"session_id": session_id, "status": "not_found"}), 404
        return jsonify({
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
            "esa_drug": row[16], "duration_hours": 4,
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
@require_role("physician", "nurse")
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
                    updates.append("status = 'cancelled'")
                    updates.append("ended_at = NOW()")
                if data.get("post_weight") is not None:
                    updates.append("post_weight = %s")
                    params.append(data["post_weight"])
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

    return jsonify(result)


@hd_bp.route("/sessions/<int:session_id>/send-summary", methods=["POST"])
@require_role("physician", "nurse")
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
        # Find patient LINE ID from master registry
        patient_line_id = None
        reg = current_app.config.get("MASTER_REGISTRY")
        if reg:
            for p in reg.list_all():
                if getattr(p, "hn", "") == summary.get("patient_hn") or \
                   getattr(p, "clinic_hn", "") == summary.get("patient_hn"):
                    patient_line_id = getattr(p, "line_user_id", None)
                    break
        if not patient_line_id:
            return jsonify({"sent": False, "message": "ผู้ป่วยยังไม่ได้ผูก LINE"})
        msg = _format_patient_summary_line(summary)
        try:
            push_fn(patient_line_id, msg)
            return jsonify({"sent": True})
        except Exception as e:
            return jsonify({"sent": False, "message": str(e)})

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

@hd_bp.route("/sessions/<int:session_id>/vitals", methods=["POST"])
@require_role("physician", "nurse")
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
                conn_fb.commit()
            conn_fb.close()
            return jsonify({"session_id": session_id, "phase": phase, "recorded": True, "vital_id": vid}), 201
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

@hd_bp.route("/sessions/<int:session_id>/uf", methods=["GET", "POST"])
@require_role("physician", "nurse")
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
                cur.execute("SELECT uf_goal FROM hd_sessions WHERE id = %s", (session_id,))
                srow = cur.fetchone()
            conn.close()
            records = [{
                "id": r[0],
                "recorded_at": (r[1].isoformat() + "Z") if r[1] else None,
                "slot_time": r[2],
                "uf_removed": r[3], "uf_rate": r[4],
                "uf_remaining": r[5], "notes": r[6],
            } for r in rows]
            return jsonify({
                "records": records,
                "uf_goal": int(srow[0]) if srow and srow[0] else 2000,
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
    notes = data.get("notes", "")

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


@hd_bp.route("/alerts/<int:alert_id>/acknowledge", methods=["POST"])
@require_role("physician", "nurse")
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
        with conn.cursor() as cur:
            # Join with hd_sessions to get last_hd date + unacked alert count
            sql = """SELECT p.id, p.hn, p.hospital_hn, p.patient_name,
                            p.vascular_access, p.dry_weight, p.schedule_days,
                            p.esa_drug, p.esa_dose, p.primary_diagnosis, p.is_active,
                            MAX(s.started_at)::date AS last_hd,
                            COALESCE(SUM(CASE WHEN a.acknowledged = FALSE THEN 1 ELSE 0 END), 0) AS unacked_alerts
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
            sql += """ GROUP BY p.id, p.hn, p.hospital_hn, p.patient_name,
                               p.vascular_access, p.dry_weight, p.schedule_days,
                               p.esa_drug, p.esa_dose, p.primary_diagnosis, p.is_active
                       ORDER BY p.hn LIMIT 300"""
            cur.execute(sql, params)
            rows = cur.fetchall()
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
                       primary_diagnosis, is_active, created_at
                FROM hd_patient_mapping WHERE hn = %s
            """, (hn,))
            row = cur.fetchone()
            # Get va_location separately (column may not exist yet)
            va_loc = None
            if row:
                try:
                    cur.execute("SELECT va_location FROM hd_patient_mapping WHERE hn = %s", (hn,))
                    vr = cur.fetchone()
                    if vr:
                        va_loc = vr[0]
                except Exception:
                    pass  # column doesn't exist yet
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
                SELECT dialyzer_type, use_count, max_uses
                FROM hd_dialyzer_tracking
                WHERE patient_id = %s AND status = 'active'
                ORDER BY registered_at DESC LIMIT 1
            """, (pid,))
            d = cur.fetchone()
            if d:
                result["dialyzer_type"] = d[0]
                result["dialyzer_reuse"] = d[1]
                result["dialyzer_max"] = d[2]

            # Last heparin protocol from most recent session
            cur.execute("""
                SELECT a.drug, a.bolus_dose, a.maintenance_dose
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
@require_role("physician", "nurse", "admin")
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
    for i in range(1, HdConfig.TOTAL_BEDS + 1):
        beds.append({
            "bed_number": i,
            "status": "available",
            "patient": None,
            "current_vitals": None,
            "alerts": [],
        })

    # Fill beds from active sessions
    if db_url:
        try:
            import psycopg2
            conn = psycopg2.connect(db_url, connect_timeout=5)
            with conn.cursor() as cur:
                # Show ALL active sessions (in_progress, scheduled)
                # Priority: in_progress > scheduled (so active session
                # is never overwritten by a scheduled one on the same bed)
                # ORDER BY: 'in_progress' sorts AFTER 'scheduled' so it
                # overwrites in the loop — last write wins
                # Query sessions: match shift exactly, OR include NULL-shift
                # sessions when viewing morning (legacy sessions before shift feature)
                cur.execute("""
                    SELECT s.bed_number, s.id as session_id, s.status,
                           p.hn, p.patient_name, p.vascular_access,
                           p.dry_weight, p.esa_drug,
                           s.started_at, 4 as duration_hours,
                           s.esa_given, s.esa_drug_given,
                           s.shift as saved_shift
                    FROM hd_sessions s
                    JOIN hd_patient_mapping p ON p.id = s.patient_id
                    WHERE s.status IN ('scheduled', 'in_progress')
                      AND s.session_date = %s
                      AND (s.shift = %s OR (s.shift IS NULL AND %s = 'morning'))
                    ORDER BY s.bed_number,
                             CASE s.status
                               WHEN 'scheduled' THEN 0
                               WHEN 'in_progress' THEN 1
                             END
                """, (target_date.isoformat(), current_shift, current_shift))
                rows = cur.fetchall()
                logger.info("bed_dashboard: date=%s shift=%s rows=%d",
                            target_date.isoformat(), current_shift, len(rows))
                session_ids = []
                for row in rows:
                    bed_num = row[0]
                    if 1 <= bed_num <= HdConfig.TOTAL_BEDS:
                        db_status = row[2]
                        js_status = {"in_progress": "active", "scheduled": "pre_hd", "completed": "done"}.get(db_status, db_status)
                        # Priority: in_progress > scheduled on same bed
                        current_bed = beds[bed_num - 1]
                        if current_bed.get("status") == "active" and js_status == "pre_hd":
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
                            },
                            "start_time": (row[8].isoformat() + "Z") if row[8] else None,
                            "duration_hours": float(row[9]) if row[9] else 4,
                            "esa_given": row[10],
                            "esa_drug_given": row[11],
                            "current_vitals": None,
                            "alerts": [],
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
                    except Exception:
                        pass

                # Fetch dialyzer info for each patient on a bed
                for sid, bnum in session_ids:
                    try:
                        patient_id_q = """
                            SELECT patient_id FROM hd_sessions WHERE id = %s
                        """
                        cur.execute(patient_id_q, (sid,))
                        pid_row = cur.fetchone()
                        if pid_row and pid_row[0]:
                            cur.execute("""
                                SELECT dialyzer_type, use_count, max_uses
                                FROM hd_dialyzer_tracking
                                WHERE patient_id = %s AND status = 'active'
                                ORDER BY registered_at DESC LIMIT 1
                            """, (pid_row[0],))
                            drow = cur.fetchone()
                            if drow and 1 <= bnum <= len(beds):
                                beds[bnum - 1]["dialyzer_type"] = drow[0]
                                beds[bnum - 1]["dialyzer_reuse"] = drow[1]
                                beds[bnum - 1]["dialyzer_max"] = drow[2]
                    except Exception:
                        pass

                # Fetch anticoagulation protocol for active sessions
                for sid, bnum in session_ids:
                    try:
                        cur.execute("""
                            SELECT drug, bolus_dose, maintenance_dose
                            FROM hd_anticoagulation
                            WHERE session_id = %s
                            ORDER BY recorded_at DESC LIMIT 1
                        """, (sid,))
                        arow = cur.fetchone()
                        if arow and 1 <= bnum <= len(beds):
                            beds[bnum - 1]["anticoag_drug"] = arow[0]
                            beds[bnum - 1]["anticoag_bolus"] = arow[1]
                            beds[bnum - 1]["anticoag_maintenance"] = arow[2]
                    except Exception:
                        pass

            conn.close()
        except Exception as e:
            import traceback
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
        "today_scheduled": today_count,
        "today_patients": today_patients,
        "_debug_sessions_today": debug_total,
        "_debug_detail": debug_detail,
    })


@hd_bp.route("/debug/sessions-today", methods=["GET"])
@require_role("physician", "admin")
def debug_sessions_today():
    """DEBUG: Show all sessions for today with shift info — for troubleshooting."""
    import os, psycopg2
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"error": "no DB", "sessions": []})
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        today = _thai_today()
        sessions = []
        with conn.cursor() as cur:
            # Get ALL sessions for today — no shift filter
            cur.execute("""
                SELECT s.id, s.bed_number, s.status, s.shift,
                       s.session_date, s.started_at, s.ended_at,
                       p.hn, p.patient_name, s.uf_goal, s.pre_weight
                FROM hd_sessions s
                LEFT JOIN hd_patient_mapping p ON p.id = s.patient_id
                WHERE s.session_date = %s
                ORDER BY s.id DESC
            """, (today.isoformat(),))
            for r in cur.fetchall():
                sessions.append({
                    "session_id": r[0], "bed": r[1], "status": r[2],
                    "shift": r[3], "shift_is_null": r[3] is None,
                    "session_date": r[4].isoformat() if r[4] else None,
                    "started_at": (r[5].isoformat() + "Z") if r[5] else None,
                    "ended_at": (r[6].isoformat() + "Z") if r[6] else None,
                    "hn": r[7], "patient_name": r[8],
                    "uf_goal": r[9], "pre_weight": float(r[10]) if r[10] else None,
                })
            # Also check: what does the dashboard query return for each shift?
            shifts_debug = {}
            for shift in ["morning", "afternoon", "evening"]:
                cur.execute("""
                    SELECT COUNT(*) FROM hd_sessions s
                    WHERE s.status IN ('scheduled', 'in_progress')
                      AND s.session_date = %s
                      AND (s.shift = %s OR (s.shift IS NULL AND %s = 'morning'))
                """, (today.isoformat(), shift, shift))
                shifts_debug[shift] = cur.fetchone()[0]
        conn.close()
        return jsonify({
            "today": today.isoformat(),
            "thai_now": _thai_now().isoformat(),
            "total_sessions_today": len(sessions),
            "dashboard_would_show": shifts_debug,
            "sessions": sessions,
        })
    except Exception as e:
        return jsonify({"error": str(e), "sessions": []})


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
        "clinic_name",             # clinic display name
        "default_duration_hours",  # default dialysis duration
        "alert_sbp_drop",          # IDH SBP drop threshold
        "alert_severe_sbp",        # severe IDH threshold
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
                WHERE status IN ('scheduled', 'in_progress')
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
    """Send alert summary to nurse LINE group.

    ส่งสรุป alerts ไปยัง LINE group ของพยาบาล
    ต้องตั้ง 'nurse_group_token' ใน hd_settings (LINE Notify token ของกลุ่ม)
    """
    import os, urllib.request, urllib.parse
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")

    if not message:
        return jsonify({"error": "message required"}), 400

    # ดึง LINE Notify token จาก settings
    group_token = get_setting_value("nurse_group_token", "")
    if not group_token:
        # Fallback: ส่ง LINE push ไปหา alert_recipients ทุกคน
        recipients = get_setting_value("alert_recipients", "")
        if not recipients:
            return jsonify({"sent": False, "message": "ไม่พบ token/recipients — ตั้งค่าที่ ⚙️"}), 400

        push_fn = current_app.config.get("LINE_PUSH_FN")
        if not push_fn:
            return jsonify({"sent": False, "message": "LINE push not configured"}), 500

        sent_count = 0
        for uid in recipients.split(","):
            uid = uid.strip()
            if uid:
                try:
                    push_fn(uid, f"🚨 แจ้งเตือน HD\n{message}")
                    sent_count += 1
                except Exception as e:
                    logger.warning("notify_nurse_group push failed for %s: %s", uid[:8], e)

        return jsonify({"sent": True, "method": "push", "count": sent_count})

    # ใช้ LINE Notify API
    try:
        req = urllib.request.Request(
            "https://notify-api.line.me/api/notify",
            data=urllib.parse.urlencode({"message": f"\n🚨 HD Alert\n{message}"}).encode(),
            headers={"Authorization": f"Bearer {group_token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return jsonify({"sent": True, "method": "notify", "status": resp.status})
    except Exception as e:
        logger.error("LINE Notify failed: %s", e)
        return jsonify({"sent": False, "error": str(e)}), 500


# ── F2: Shift Handover ────────────────────────────────────

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
               "esa_drug", "esa_dose", "primary_diagnosis", "needs_review", "is_active"}
    updates = []
    params = []
    for key in allowed:
        if key in data:
            if key == "dry_weight":
                updates.append(f"{key} = %s")
                params.append(float(data[key]) if data[key] else None)
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
        return jsonify({"records": records, "prescribed": prescribed})
    except Exception as e:
        logger.error("patient_esa_history error: %s", e)
        return jsonify({"records": [], "prescribed": {}, "error": str(e)})


# ══════════════════════════════════════════════════════════
# HD FLOWSHEET — Full HD Monitoring Sheet
# ══════════════════════════════════════════════════════════

@hd_bp.route("/sessions/<int:session_id>/flowsheet", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_flowsheet(session_id):
    """Get all flowsheet entries for a session."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"rows": []})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
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
        conn.close()

        entries = []
        for r in rows:
            entries.append({
                "id": r[0], "slot_time": r[1],
                "recorded_at": r[2].isoformat() if r[2] else None,
                "sbp": r[3], "dbp": r[4], "pr": r[5], "rr": r[6],
                "temp": float(r[7]) if r[7] else None,
                "spo2": r[8],
                "bfr": r[9], "vp": r[10], "tmp": r[11], "ap": r[12],
                "conductivity": float(r[13]) if r[13] else None,
                "dialysate_temp": float(r[14]) if r[14] else None,
                "ufr": r[15], "total_uf": r[16],
                "remark": r[17],
            })
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
@require_role("physician", "nurse")
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

            # Sync vitals to hd_vital_signs for timeline display
            if params["sbp"]:
                cur.execute("""
                    INSERT INTO hd_vital_signs
                        (session_id, phase, sbp, dbp, heart_rate, recorded_at)
                    VALUES (%s, 'intra', %s, %s, %s, NOW())
                """, (session_id, params["sbp"], params["dbp"], params["pr"]))

            # Generate alert if BP abnormal
            alert = None
            if params["sbp"] and (int(params["sbp"]) < 90 or int(params["sbp"]) > 180):
                severity = "critical" if int(params["sbp"]) < 80 or int(params["sbp"]) > 200 else "warning"
                title = f"BP ผิดปกติ: {params['sbp']}/{params['dbp'] or '-'} ({slot_time})"
                cur.execute("""
                    INSERT INTO hd_clinical_alerts (session_id, severity, title, message, acknowledged)
                    VALUES (%s, %s, %s, %s, FALSE)
                """, (session_id, severity, title,
                      f"BP {params['sbp']}/{params['dbp'] or '-'} at {slot_time}"))
                alert = {"severity": severity, "title": title}

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
@require_role("physician", "nurse")
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
@require_role("physician", "nurse", "admin")
def qr_all_beds():
    """สร้างหน้าพิมพ์ QR Code ทุกเตียง."""
    from .qr_checkin import generate_all_bed_qr_page
    html = generate_all_bed_qr_page(HdConfig.TOTAL_BEDS)
    return make_response(html)


@hd_bp.route("/qr/bed/<int:bed_number>", methods=["GET"])
@require_role("physician", "nurse", "admin")
def qr_single_bed(bed_number):
    """สร้าง QR data สำหรับเตียงเดียว."""
    from .qr_checkin import generate_bed_qr_data
    return jsonify(generate_bed_qr_data(bed_number))


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
@require_role("physician", "nurse")
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


@hd_bp.route("/patients/<hn>/labs", methods=["GET"])
@require_role("physician", "nurse", "admin")
def get_patient_labs(hn):
    """Get lab results history with abnormal flags."""
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
                       l.notes, l.source
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
                "ast","alt","hep_b","hep_c","hiv","notes","source"]
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
                        "hepatitis_b","hepatitis_c","hiv","notes","source"]
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
@require_role("physician", "nurse")
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
@require_role("physician", "nurse")
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
        # Use LINE Notify if available
        notify_token = None
        if db_url:
            try:
                import psycopg2
                conn = psycopg2.connect(db_url, connect_timeout=5)
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM hd_settings WHERE key='line_notify_token'")
                    r = cur.fetchone()
                    if r:
                        notify_token = r[0]
                conn.close()
            except Exception:
                pass
        if notify_token:
            try:
                import requests as req
                req.post("https://notify-api.line.me/api/notify",
                         headers={"Authorization": f"Bearer {notify_token}"},
                         data={"message": "\n" + nurse_msg}, timeout=10)
            except Exception as e:
                logger.warning("Nurse summary notify failed: %s", e)

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
@require_role("physician", "nurse")
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

            # Check alert thresholds
            alert = None
            if sbp and (int(sbp) < 90 or int(sbp) > 180):
                severity = "critical" if int(sbp) < 80 or int(sbp) > 200 else "warning"
                title = f"BP ผิดปกติ: {sbp}/{dbp or '-'} mmHg"
                msg = f"BP {sbp}/{dbp or '-'} mmHg (Quick Entry)"
                cur.execute("""
                    INSERT INTO hd_clinical_alerts (session_id, severity, title, message, acknowledged)
                    VALUES (%s, %s, %s, %s, FALSE) RETURNING id
                """, (session_id, severity, title, msg))
                alert = {"severity": severity, "title": title}

        conn.commit()
        conn.close()

        log_action(user_id=g.get("hd_user_id"), action="vital.quick_entry",
                   resource_type="vital", resource_id=vital_id,
                   ip_address=request.remote_addr or "")

        return jsonify({
            "vital_id": vital_id,
            "sbp": sbp, "dbp": dbp, "hr": hr,
            "alert": alert,
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
                    SELECT d.dialyzer_type, d.use_count, d.max_uses, p.patient_name, p.hn
                    FROM hd_dialyzer_tracking d
                    JOIN hd_patient_mapping p ON p.id = d.patient_id
                    WHERE d.status = 'active' AND d.use_count >= d.max_uses * 0.8
                    ORDER BY d.use_count DESC LIMIT 10
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
