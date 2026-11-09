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

logger = logging.getLogger(__name__)

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
    resp = make_response(render_template("hd_dashboard.html"))
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


@hd_web.route("/settings")
def hd_settings_page():
    """Render admin settings page."""
    return render_template("hd_settings.html")


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

    status = "ok" if db_ok else "degraded"
    code = 200 if db_ok else 503
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
            "alert_recipients": "✅" if alert_configured else "⚠️ ยังไม่ตั้ง LINE alert → ตั้งค่าที่ ⚙️",
        },
    }), code


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
    session_date = request.args.get("date", date.today().isoformat())
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
    if not isinstance(bed, int) or bed < 1 or bed > 50:
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

            cur.execute("""
                SELECT id, status FROM hd_sessions
                WHERE bed_number = %s
                  AND status IN ('scheduled', 'in_progress')
                ORDER BY status
            """, (bed,))
            existing = cur.fetchall()
            for ex_id, ex_status in existing:
                if init_status == "in_progress" and ex_status == "in_progress":
                    conn.close()
                    return jsonify({"error": f"เตียง {bed} มีคนไข้กำลังฟอกอยู่"}), 409
                if init_status == "scheduled" and ex_status == "scheduled":
                    conn.close()
                    return jsonify({"error": f"เตียง {bed} มีการจองรอบถัดไปแล้ว"}), 409

            # Insert session
            started = "NULL" if is_schedule else "NOW()"
            cur.execute(f"""
                INSERT INTO hd_sessions
                    (patient_id, bed_number, session_date, status,
                     pre_weight, post_weight, uf_goal,
                     started_at, nurse_id)
                VALUES (%s, %s, CURRENT_DATE, '{init_status}',
                        %s, NULL, %s,
                        {started}, %s)
                RETURNING id
            """, (
                patient_id, bed,
                data.get("pre_weight"),
                data.get("uf_goal", 2000),
                g.get("hd_user_id"),
            ))
            session_id = cur.fetchone()[0]
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
                       p.schedule_days, p.esa_drug
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
        f"📞 หากมีอาการผิดปกติ โทร 077-812-XXX"
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
                "recorded_at": r[7].isoformat() if r[7] else None,
            } for r in cur.fetchall()]
        conn.close()
        return jsonify({"session_id": session_id, "vitals": vitals, "count": len(vitals)})
    except Exception as e:
        logger.error("list_vitals error: %s", e)
        return jsonify({"session_id": session_id, "vitals": [], "error": str(e)})


# ── UF Records ────────────────────────────────────────────

@hd_bp.route("/sessions/<int:session_id>/uf", methods=["POST"])
@require_role("physician", "nurse")
def record_uf(session_id):
    """Record UF data for a session."""
    data = request.get_json(silent=True) or {}
    log_action(
        user_id=g.get("hd_user_id"),
        action="uf.record",
        resource_type="uf_record",
        new_values=data,
        ip_address=request.remote_addr or "",
    )
    return jsonify({"session_id": session_id, "uf_recorded": True}), 201


# ── Alerts ────────────────────────────────────────────────

@hd_bp.route("/alerts", methods=["GET"])
@require_role("physician", "nurse", "admin")
def list_alerts():
    """List active clinical alerts."""
    return jsonify({"alerts": [], "total": 0})


@hd_bp.route("/alerts/<int:alert_id>/acknowledge", methods=["POST"])
@require_role("physician", "nurse")
def acknowledge_alert(alert_id):
    """Acknowledge a clinical alert."""
    data = request.get_json(silent=True) or {}
    log_action(
        user_id=g.get("hd_user_id"),
        action="alert.acknowledge",
        resource_type="alert",
        resource_id=alert_id,
        new_values=data,
        ip_address=request.remote_addr or "",
    )
    return jsonify({"alert_id": alert_id, "acknowledged": True})


# ── Patients ──────────────────────────────────────────────

@hd_bp.route("/patients", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def list_patients():
    """List HD patients from PostgreSQL."""
    import os
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return jsonify({"patients": [], "total": 0})
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=5)
        search = request.args.get("q", "").strip()
        schedule = request.args.get("schedule", "").strip()
        with conn.cursor() as cur:
            sql = """SELECT id, hn, hospital_hn, patient_name, vascular_access,
                            dry_weight, schedule_days, esa_drug, esa_dose,
                            primary_diagnosis, is_active
                     FROM hd_patient_mapping WHERE is_active = TRUE"""
            params = []
            if search:
                sql += " AND (patient_name ILIKE %s OR hn ILIKE %s)"
                params.extend([f"%{search}%", f"%{search}%"])
            if schedule:
                sql += " AND schedule_days ILIKE %s"
                params.append(f"%{schedule}%")
            sql += " ORDER BY hn LIMIT 200"
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.close()
        patients = []
        for r in rows:
            patients.append({
                "id": r[0], "hn": r[1], "hospital_hn": r[2],
                "patient_name": r[3], "vascular_access": r[4],
                "dry_weight": float(r[5]) if r[5] else None,
                "schedule_days": r[6], "esa_drug": r[7], "esa_dose": r[8],
                "primary_diagnosis": r[9], "is_active": r[10],
            })
        return jsonify({"patients": patients, "total": len(patients)})
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
        conn.close()
        if not row:
            return jsonify({"hn": hn, "status": "not_found"}), 404
        return jsonify({
            "id": row[0], "hn": row[1], "hospital_hn": row[2],
            "patient_name": row[3],
            "date_of_birth": row[4].isoformat() if row[4] else None,
            "gender": row[5], "blood_type": row[6], "allergies": row[7],
            "vascular_access": row[8],
            "dry_weight": float(row[9]) if row[9] else None,
            "schedule_days": row[10], "esa_drug": row[11], "esa_dose": row[12],
            "primary_diagnosis": row[13], "is_active": row[14],
            "created_at": row[15].isoformat() if row[15] else None,
        })
    except Exception as e:
        logger.error("get_patient error: %s", e)
        return jsonify({"hn": hn, "error": str(e)}), 500


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
    """30-bed real-time dashboard with active sessions."""
    import os
    from .config import HdConfig
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")

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
                cur.execute("""
                    SELECT s.bed_number, s.id as session_id, s.status,
                           p.hn, p.patient_name, p.vascular_access,
                           p.dry_weight, p.esa_drug,
                           s.started_at, 4 as duration_hours
                    FROM hd_sessions s
                    JOIN hd_patient_mapping p ON p.id = s.patient_id
                    WHERE s.status IN ('scheduled', 'in_progress')
                    ORDER BY s.bed_number,
                             CASE s.status
                               WHEN 'scheduled' THEN 0
                               WHEN 'in_progress' THEN 1
                             END
                """)
                session_ids = []
                for row in cur.fetchall():
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
            conn.close()
        except Exception as e:
            logger.warning("bed_dashboard DB error: %s", e)

    # Get today's scheduled patients from DB
    today_count = 0
    today_patients = []
    if db_url:
        try:
            import psycopg2
            day_map = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}
            today_th = day_map.get(date.today().weekday(), "")
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
            today_th = day_map.get(date.today().weekday(), "")
            reg = current_app.config["MASTER_REGISTRY"]
            for p in reg.list_all():
                sched = getattr(p, "dialysis_schedule", "") or ""
                if today_th and today_th in sched:
                    today_count += 1
        except Exception:
            pass

    return jsonify({
        "beds": beds,
        "total": HdConfig.TOTAL_BEDS,
        "today_scheduled": today_count,
        "today_patients": today_patients,
    })


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
    """Generate monthly HD report."""
    import os
    from .monthly_report import generate_monthly_report

    month = request.args.get("month", type=int)
    year = request.args.get("year", type=int)
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    registry = current_app.config.get("MASTER_REGISTRY")

    report = generate_monthly_report(registry, db_url, month, year)
    log_action(user_id=g.get("hd_user_id"), action="report.monthly",
               resource_type="report", ip_address=request.remote_addr or "")
    return jsonify(report)


# ── BI Reports ────────────────────────────────────────────

@hd_bp.route("/report/ha", methods=["GET"])
@require_role("physician", "admin")
def hd_ha_report():
    """HA Quality Indicators report."""
    import os
    from .bi_reporting import BiReportEngine

    month = request.args.get("month", type=int, default=0)
    year = request.args.get("year", type=int, default=0)
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    registry = current_app.config.get("MASTER_REGISTRY")

    engine = BiReportEngine(master_registry=registry, db_url=db_url)
    report = engine.generate_ha_report(month, year)

    log_action(user_id=g.get("hd_user_id"), action="report.ha",
               resource_type="report", ip_address=request.remote_addr or "")
    return jsonify(report.to_dict())


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
