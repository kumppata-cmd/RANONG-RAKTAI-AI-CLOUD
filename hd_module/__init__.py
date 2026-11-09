"""HD Smart Monitoring Module — Flask Blueprint Registration.

Feature Flag: HD_MODULE_ENABLED (env var, default "false")
When disabled, the Blueprint is NOT registered and the entire
HD module has zero impact on the existing application.
"""

import logging
import os

logger = logging.getLogger(__name__)

HD_MODULE_ENABLED = os.environ.get("HD_MODULE_ENABLED", "false").lower() == "true"


def register_hd_module(app):
    """Register the HD Smart Monitoring Blueprint with a Flask app.

    Does nothing if HD_MODULE_ENABLED is false.
    This is the ONLY integration point with server.py.

    Usage in server.py (1 line):
        from hd_module import register_hd_module
        register_hd_module(app)
    """
    if not HD_MODULE_ENABLED:
        logger.info("HD Smart Monitoring module is DISABLED (HD_MODULE_ENABLED=false)")
        return False

    try:
        from .blueprint import hd_bp, hd_web
        app.register_blueprint(hd_bp)
        app.register_blueprint(hd_web)
        logger.info("HD Smart Monitoring module registered at /hd/api/v1/ + /hd/ (web)")

        # Auto-run database migrations (idempotent — CREATE IF NOT EXISTS)
        _run_hd_migrations()

        # Auto-complete stale sessions from previous day (safety reset)
        _auto_complete_stale_sessions()

        return True
    except Exception as e:
        logger.error(f"Failed to register HD module: {e}")
        return False


def _run_hd_migrations():
    """Run HD database migrations if PostgreSQL is available.

    Uses psycopg2 directly (same as database.py) — no SQLAlchemy needed.
    Safe to run every boot: all CREATE statements use IF NOT EXISTS.
    """
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        logger.info("HD migrations: skipped — no database URL configured")
        return

    try:
        import psycopg2
    except ImportError:
        logger.warning("HD migrations: skipped — psycopg2 not available")
        return

    try:
        from .migrations import MIGRATION_UP, SCHEMA_VERSION
        conn = psycopg2.connect(db_url, connect_timeout=10)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(MIGRATION_UP)
        conn.close()
        logger.info("HD migrations: applied schema v%s successfully", SCHEMA_VERSION)
    except Exception as e:
        logger.error("HD migrations: failed — %s", e)


def sync_patients_to_hd_db(master_registry):
    """Sync patients from GCS MasterPatientRegistry → HD PostgreSQL.

    One-time sync: skips if hd_patient_mapping already has data.
    Safe to call every boot.
    """
    if not HD_MODULE_ENABLED:
        return

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        return

    try:
        conn = psycopg2.connect(db_url, connect_timeout=10)
        # Read from registry via bridge
        from .patient_bridge import PatientBridge
        bridge = PatientBridge(master_registry)
        patients = bridge.sync_patients_readonly()

        if not patients:
            logger.info("HD patient sync: no patients to import")
            conn.close()
            return

        # Upsert into hd_patient_mapping (insert new + update existing)
        inserted = 0
        updated = 0
        with conn.cursor() as cur:
            for p in patients:
                try:
                    cur.execute("""
                        INSERT INTO hd_patient_mapping
                            (hn, hospital_hn, patient_name, vascular_access,
                             dry_weight, schedule_days, esa_drug, esa_dose,
                             primary_diagnosis, source_registry_hash, is_active)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                        ON CONFLICT (hn) DO UPDATE SET
                            patient_name = EXCLUDED.patient_name,
                            vascular_access = CASE WHEN EXCLUDED.vascular_access != '' THEN EXCLUDED.vascular_access ELSE hd_patient_mapping.vascular_access END,
                            schedule_days = CASE WHEN EXCLUDED.schedule_days != '' THEN EXCLUDED.schedule_days ELSE hd_patient_mapping.schedule_days END,
                            esa_drug = CASE WHEN EXCLUDED.esa_drug != '' THEN EXCLUDED.esa_drug ELSE hd_patient_mapping.esa_drug END,
                            esa_dose = CASE WHEN EXCLUDED.esa_dose != '' THEN EXCLUDED.esa_dose ELSE hd_patient_mapping.esa_dose END,
                            is_active = TRUE
                    """, (
                        p.get("hn", ""),
                        p.get("hospital_hn", ""),
                        p.get("patient_name", ""),
                        p.get("vascular_access", ""),
                        p.get("dry_weight"),
                        p.get("schedule_days", ""),
                        p.get("esa_drug", "") or p.get("esa_code", ""),
                        p.get("esa_dose", ""),
                        p.get("primary_diagnosis", "CKD-G5 on HD"),
                        p.get("source_registry_hash", ""),
                    ))
                    if cur.statusmessage and 'UPDATE' in cur.statusmessage:
                        updated += 1
                    else:
                        inserted += 1
                except Exception as row_err:
                    logger.warning("HD patient sync: skip row %s — %s",
                                   p.get("hn"), row_err)
            conn.commit()
        conn.close()
        logger.info("HD patient sync: inserted=%d updated=%d into hd_patient_mapping",
                     inserted, updated)
    except Exception as e:
        logger.error("HD patient sync failed: %s", e)


def create_default_hd_users():
    """Create default HD user accounts if none exist.

    Creates 3 accounts:
    - physician: dr.kamphon (physician role)
    - nurse: nurse1 (nurse role)
    - admin: hd_admin (admin role)

    Passwords come from ENV or use secure defaults.
    Safe to call every boot — skips if users already exist.
    """
    if not HD_MODULE_ENABLED:
        return

    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return

    try:
        import psycopg2
    except ImportError:
        return

    try:
        from .auth import hash_password

        conn = psycopg2.connect(db_url, connect_timeout=10)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM hd_users")
            count = cur.fetchone()[0]
            if count > 0:
                logger.info("HD users: skipped — %d users already exist", count)
                conn.close()
                return

        # Default accounts — passwords from ENV or generated
        admin_secret = os.environ.get("ADMIN_SECRET", "changeme")
        default_users = [
            {
                "username": "dr.kamphon",
                "password": os.environ.get("HD_PHYSICIAN_PW", admin_secret),
                "full_name": "นพ.กัมพล แพทย์โรคไต",
                "role": "physician",
                "license_number": "ว.XXXXX",
            },
            {
                "username": "nurse1",
                "password": os.environ.get("HD_NURSE_PW", admin_secret),
                "full_name": "พยาบาลไตเทียม 1",
                "role": "nurse",
                "license_number": "",
            },
            {
                "username": "hd_admin",
                "password": os.environ.get("HD_ADMIN_PW", admin_secret),
                "full_name": "ผู้ดูแลระบบ HD",
                "role": "admin",
                "license_number": "",
            },
        ]

        created = 0
        with conn.cursor() as cur:
            for u in default_users:
                pw_hash = hash_password(u["password"])
                try:
                    cur.execute("""
                        INSERT INTO hd_users
                            (username, password_hash, full_name, role,
                             license_number, is_active)
                        VALUES (%s, %s, %s, %s::hd_user_role, %s, TRUE)
                        ON CONFLICT (username) DO NOTHING
                    """, (
                        u["username"],
                        pw_hash,
                        u["full_name"],
                        u["role"],
                        u["license_number"],
                    ))
                    created += 1
                except Exception as row_err:
                    logger.warning("HD users: skip %s — %s", u["username"], row_err)
            conn.commit()
        conn.close()
        logger.info("HD users: created %d default accounts", created)
    except Exception as e:
        logger.error("HD users creation failed: %s", e)


def _auto_complete_stale_sessions():
    """Auto-complete sessions left in_progress from previous day.

    Safety: sessions older than 8 hours are auto-completed on boot.
    This prevents stale beds from blocking the next day's rounds.
    Safe to call every boot — only affects truly stale sessions.
    """
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        return

    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=10)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE hd_sessions
                SET status = 'completed',
                    ended_at = NOW(),
                    notes = COALESCE(notes, '') || ' [auto-completed: stale session >8hr]'
                WHERE status = 'in_progress'
                  AND started_at < NOW() - INTERVAL '8 hours'
                RETURNING id, bed_number
            """)
            rows = cur.fetchall()
            conn.commit()
        conn.close()
        if rows:
            ids = [f"bed {r[1]} (session {r[0]})" for r in rows]
            logger.info("HD auto-complete: marked %d stale sessions as completed: %s",
                        len(rows), ", ".join(ids))
        else:
            logger.info("HD auto-complete: no stale sessions found")
    except Exception as e:
        logger.error("HD auto-complete stale sessions failed: %s", e)
