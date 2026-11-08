"""HD Smart Monitoring — Flask Blueprint.

All routes under /hd/api/v1/.  Completely isolated from server.py.
"""

import logging
from datetime import date, datetime, timezone  # noqa: F401
from flask import (  # noqa: F401
    Blueprint, request, jsonify, g, render_template, redirect, url_for,
    current_app,
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
    return render_template("hd_dashboard.html")


@hd_web.route("/session/new")
def hd_session_new():
    """Render new HD session form (Pre/Intra/Post tabs)."""
    return render_template("hd_session.html")


@hd_web.route("/session/<int:session_id>")
def hd_session_detail(session_id):
    """Render existing HD session."""
    return render_template("hd_session.html", session_id=session_id)


@hd_web.route("/patients")
def hd_patient_list_page():
    """Render patient list."""
    return render_template("hd_patient_list.html")


@hd_web.route("/alerts")
def hd_alerts_page():
    """Render clinical alerts page."""
    return render_template("hd_alerts.html")


# ── Health / Status ───────────────────────────────────────

@hd_bp.route("/health", methods=["GET"])
def hd_health():
    """HD module health check."""
    return jsonify({
        "module": "hd_smart_monitoring",
        "status": "ok",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
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

    log_action(
        user_id=g.get("hd_user_id"),
        action="session.create",
        resource_type="session",
        new_values=data,
        ip_address=request.remote_addr or "",
    )

    return jsonify({
        "status": "created",
        "message": "Session created (database not connected)",
        "data": data,
    }), 201


@hd_bp.route("/sessions/<int:session_id>", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def get_session(session_id):
    """Get details of a specific HD session."""
    return jsonify({"session_id": session_id, "status": "not_found"}), 404


@hd_bp.route("/sessions/<int:session_id>", methods=["PUT"])
@require_role("physician", "nurse")
def update_session(session_id):
    """Update an HD session (e.g. end dialysis, record post-weight)."""
    data = request.get_json(silent=True) or {}
    log_action(
        user_id=g.get("hd_user_id"),
        action="session.update",
        resource_type="session",
        resource_id=session_id,
        new_values=data,
        ip_address=request.remote_addr or "",
    )
    return jsonify({"session_id": session_id, "updated": True})


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

    # Use VitalRecorder for real-time recording + alert evaluation
    from flask import current_app
    recorder = current_app.config.get("HD_VITAL_RECORDER")
    if recorder:
        result = recorder.record_vitals(
            session_id=session_id,
            patient_id=data.get("patient_id", 0),
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

    return jsonify({"session_id": session_id, "phase": phase, "recorded": True}), 201


@hd_bp.route("/sessions/<int:session_id>/vitals", methods=["GET"])
@require_role("physician", "nurse", "admin", "auditor")
def list_vitals(session_id):
    """List vital signs for a session."""
    return jsonify({"session_id": session_id, "vitals": []})


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
                cur.execute("""
                    SELECT s.bed_number, s.id as session_id, s.status,
                           p.hn, p.patient_name, p.vascular_access,
                           p.dry_weight, p.esa_drug
                    FROM hd_sessions s
                    JOIN hd_patient_mapping p ON p.id = s.patient_id
                    WHERE s.session_date = CURRENT_DATE
                      AND s.status IN ('scheduled', 'in_progress')
                    ORDER BY s.bed_number
                """)
                for row in cur.fetchall():
                    bed_num = row[0]
                    if 1 <= bed_num <= HdConfig.TOTAL_BEDS:
                        beds[bed_num - 1] = {
                            "bed_number": bed_num,
                            "status": row[2],
                            "session_id": row[1],
                            "patient": {
                                "hn": row[3],
                                "name": row[4],
                                "vascular_access": row[5],
                                "dry_weight": float(row[6]) if row[6] else None,
                                "esa_drug": row[7],
                            },
                            "current_vitals": None,
                            "alerts": [],
                        }
            conn.close()
        except Exception as e:
            logger.warning("bed_dashboard DB error: %s", e)

    return jsonify({"beds": beds, "total": HdConfig.TOTAL_BEDS})


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


@hd_web.route("/kpi")
def hd_kpi_page():
    """HD KPI Dashboard web page."""
    return render_template("hd_kpi.html")


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
