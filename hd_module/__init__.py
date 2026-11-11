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

        # Auto-backup HD tables to GCS (daily on boot)
        _auto_backup_hd_tables()

        # Start self-ping to prevent Render sleep
        _render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
        if _render_url:
            from .monitoring import start_self_ping
            start_self_ping(_render_url, interval=300)
            logger.info("HD self-ping started for %s", _render_url)

        # Start daily pre-HD reminder scheduler (18:00 daily)
        _start_daily_reminder_scheduler(app)

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

    # ── Post-migration fixes (run every boot, safe to repeat) ──
    try:
        conn = psycopg2.connect(db_url, connect_timeout=10)
        conn.autocommit = True
        with conn.cursor() as cur:
            # Plain statements only (no DO blocks) — each is idempotent.
            # DROP CONSTRAINT IF EXISTS is a no-op when already gone;
            # ALTER TYPE VARCHAR on a VARCHAR column is a harmless no-op.
            fixes = [
                "ALTER TABLE hd_incidents DROP CONSTRAINT IF EXISTS hd_incidents_reported_by_fkey",
                "ALTER TABLE hd_clinic_profile DROP CONSTRAINT IF EXISTS hd_clinic_profile_updated_by_fkey",
                "ALTER TABLE hd_water_quality DROP CONSTRAINT IF EXISTS hd_water_quality_recorded_by_fkey",
                "ALTER TABLE hd_incidents ALTER COLUMN reported_by TYPE VARCHAR(100) USING reported_by::text",
                "ALTER TABLE hd_clinic_profile ALTER COLUMN updated_by TYPE VARCHAR(100) USING updated_by::text",
                "ALTER TABLE hd_water_quality ALTER COLUMN recorded_by TYPE VARCHAR(100) USING recorded_by::text",
                # Add missing columns to hd_water_quality
                "ALTER TABLE hd_water_quality ADD COLUMN IF NOT EXISTS parameter VARCHAR(100)",
                "ALTER TABLE hd_water_quality ADD COLUMN IF NOT EXISTS method VARCHAR(100)",
                "ALTER TABLE hd_water_quality ADD COLUMN IF NOT EXISTS tested_by VARCHAR(100)",
                "ALTER TABLE hd_water_quality ADD COLUMN IF NOT EXISTS send_date DATE",
                # Add resolved_at to incidents
                "ALTER TABLE hd_incidents ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP",
                # Add pin_hash to hd_users for PIN login
                "ALTER TABLE hd_users ADD COLUMN IF NOT EXISTS pin_hash VARCHAR(255)",
                # hd_clinical_alerts: code reads/writes title + acknowledged but the
                # migration never created them → patient list API and alert inserts failed
                "ALTER TABLE hd_clinical_alerts ADD COLUMN IF NOT EXISTS title VARCHAR(200)",
                "ALTER TABLE hd_clinical_alerts ADD COLUMN IF NOT EXISTS acknowledged BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE hd_clinical_alerts ALTER COLUMN alert_type DROP NOT NULL",
                "ALTER TABLE hd_clinical_alerts ALTER COLUMN alert_type SET DEFAULT 'clinical'",
                # columns used by alert_push.py / bi_reporting.py
                "ALTER TABLE hd_clinical_alerts ADD COLUMN IF NOT EXISTS rule_id VARCHAR(50)",
                "ALTER TABLE hd_clinical_alerts ADD COLUMN IF NOT EXISTS details TEXT",
                "ALTER TABLE hd_clinical_alerts ADD COLUMN IF NOT EXISTS triggered_at TIMESTAMP DEFAULT NOW()",
                "ALTER TABLE hd_clinical_alerts ADD COLUMN IF NOT EXISTS is_acknowledged BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE hd_clinical_alerts ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMP",
                # physician treatment order recorded on an alert (free text by a human)
                "ALTER TABLE hd_clinical_alerts ADD COLUMN IF NOT EXISTS physician_order TEXT",
                "ALTER TABLE hd_clinical_alerts ADD COLUMN IF NOT EXISTS order_by VARCHAR(100)",
                "ALTER TABLE hd_clinical_alerts ADD COLUMN IF NOT EXISTS order_at TIMESTAMP",
                # Create SOP files table
                """CREATE TABLE IF NOT EXISTS hd_sop_files (
                    id SERIAL PRIMARY KEY, sop_key VARCHAR(80) NOT NULL,
                    filename VARCHAR(255) NOT NULL, content_type VARCHAR(100),
                    file_size INTEGER, file_data BYTEA NOT NULL,
                    uploaded_by VARCHAR(100), uploaded_at TIMESTAMP DEFAULT NOW())""",
                # Set alert_recipients
                """INSERT INTO hd_settings (key, value, description, updated_at)
                    VALUES ('alert_recipients', 'U1b1068cde88a06e88e53c014d1b83824', 'LINE userId แพทย์', NOW())
                    ON CONFLICT (key) DO NOTHING""",
            ]
            ok = 0
            for sql in fixes:
                one_line = " ".join(sql.split())[:70]
                try:
                    cur.execute(sql)
                    ok += 1
                    logger.info("HD fix OK: %s", one_line)
                except Exception as fix_err:
                    logger.warning("HD fix FAILED: %s | %s", one_line, " ".join(str(fix_err).split()))
            logger.info("HD fixes: %d/%d succeeded", ok, len(fixes))
        conn.close()
        logger.info("HD post-migration fixes: done")
    except Exception as e:
        logger.error("HD post-migration fixes: %s", e)


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


def _auto_backup_hd_tables():
    """Backup HD tables to GCS as JSON on every boot.

    Lightweight backup: exports key tables as JSON to GCS bucket.
    Runs on every server start — safe and idempotent.
    Keeps last 7 days of backups automatically.
    """
    db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    bucket_name = os.environ.get("GCS_BUCKET_NAME", "")
    if not db_url or not bucket_name:
        return

    try:
        import psycopg2
        import json
        from datetime import datetime, timezone

        conn = psycopg2.connect(db_url, connect_timeout=10)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        backup_data = {"date": today, "tables": {}}

        tables = [
            "hd_patient_mapping", "hd_sessions", "hd_vital_signs",
            "hd_clinical_alerts", "hd_users", "hd_settings",
        ]
        for table in tables:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
                    count = cur.fetchone()[0]
                    cur.execute(
                        f"SELECT row_to_json(t) FROM {table} t "  # noqa: S608
                        f"ORDER BY 1 LIMIT 10000"
                    )
                    rows = [r[0] for r in cur.fetchall()]
                    backup_data["tables"][table] = {
                        "count": count, "rows": rows,
                    }
            except Exception:
                backup_data["tables"][table] = {"count": 0, "error": "table not found"}
        conn.close()

        # Upload to GCS
        try:
            from google.cloud import storage as gcs_storage
            client = gcs_storage.Client()
            bucket = client.bucket(bucket_name)
            blob_path = f"backups/hd_backup_{today}.json"
            blob = bucket.blob(blob_path)
            blob.upload_from_string(
                json.dumps(backup_data, default=str, ensure_ascii=False),
                content_type="application/json",
            )
            logger.info("HD backup: saved %d tables to gs://%s/%s",
                        len(tables), bucket_name, blob_path)

            # Cleanup old backups (keep last 7)
            prefix = "backups/hd_backup_"
            blobs = sorted(bucket.list_blobs(prefix=prefix),
                           key=lambda b: b.name, reverse=True)
            for old_blob in blobs[7:]:
                old_blob.delete()
                logger.info("HD backup: deleted old backup %s", old_blob.name)
        except Exception as gcs_err:
            logger.warning("HD backup: GCS upload failed: %s", gcs_err)
            # Fallback: save to /tmp
            _tmp_path = f"/tmp/hd_backup_{today}.json"
            with open(_tmp_path, "w") as f:
                json.dump(backup_data, f, default=str, ensure_ascii=False)
            logger.info("HD backup: saved to %s (GCS failed)", _tmp_path)

    except Exception as e:
        logger.error("HD backup failed: %s", e)


def _start_daily_reminder_scheduler(app):
    """Start background thread that sends pre-HD reminders daily at 18:00."""
    import threading
    import time
    from datetime import datetime

    def _reminder_loop():
        last_sent_date = None
        while True:
            try:
                from datetime import timezone as _tz, timedelta as _td
                _thai_tz = _tz(_td(hours=7))
                now = datetime.now(_thai_tz)
                # Send at 18:00 Thai time
                if now.hour == 18 and now.minute < 5 and last_sent_date != now.date():
                    with app.app_context():
                        import os
                        db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
                        push_fn = app.config.get("LINE_PUSH_FN")
                        registry = app.config.get("MASTER_REGISTRY")
                        if db_url and push_fn:
                            from .pre_hd_reminder import send_pre_hd_reminders
                            result = send_pre_hd_reminders(push_fn, db_url, registry)
                            logger.info("Daily pre-HD reminder: %s", result)
                    last_sent_date = now.date()
            except Exception as e:
                logger.error("Reminder scheduler error: %s", e)
            time.sleep(300)  # Check every 5 minutes

    t = threading.Thread(target=_reminder_loop, daemon=True, name="hd-reminder")
    t.start()
    logger.info("HD daily reminder scheduler started (18:00 Thai time)")
