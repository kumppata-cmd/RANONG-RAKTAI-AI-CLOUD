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
        # 24 พ.ย. 2569: รันครั้งเดียวต่อ instance (worker ตัวถัดไป/ที่สร้างใหม่ข้าม) → worker บูตเร็ว
        from .boot_once import run_once_per_instance as _once
        try:
            from .migrations import SCHEMA_VERSION as _sv
        except Exception:
            _sv = "0"
        _once("hd_migrations", _run_hd_migrations, version=str(_sv))

        # Auto-complete stale sessions from previous day (safety reset) — query เดียว เร็ว คงไว้ทุกบูตตามเดิม
        _auto_complete_stale_sessions()

        # Auto-backup HD tables to GCS now runs from the scheduler thread
        # ~90 s after boot, once server.py has put its authenticated storage
        # object in app.config["STORAGE_OBJ"] (a bare Client() got 403).

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


_BACKUP_STORAGE_OBJ = None


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
                # hd_uf_records is defined twice in migrations (v1.0.0 shape won):
                # make it match what the UF endpoints insert
                "ALTER TABLE hd_uf_records ADD COLUMN IF NOT EXISTS slot_time VARCHAR(5)",
                # 16 พ.ย. 2569: ดูรอบฟอกย้อนหลังให้ครบ — อาการก่อน/หลังฟอก + reuse ของรอบนั้น
                "ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS pre_symptoms TEXT",
                "ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS post_symptoms TEXT",
                "ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS dialyzer_reuse INTEGER",
                # 17 พ.ย. 2569: เกณฑ์น้ำหนักขึ้นระหว่างรอบรายคน (% ของ DW; ว่าง = ใช้ค่าใน ⚙️ / 4%)
                "ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS idwg_limit_pct NUMERIC(4,1)",
                """CREATE TABLE IF NOT EXISTS hd_home_weights (id SERIAL PRIMARY KEY, hn VARCHAR(20) NOT NULL,
                   weight NUMERIC(5,1) NOT NULL, idwg_kg NUMERIC(4,1), over_limit BOOLEAN, via VARCHAR(20) DEFAULT 'line',
                   recorded_at TIMESTAMP DEFAULT NOW())""",
                "ALTER TABLE hd_uf_records ADD COLUMN IF NOT EXISTS notes VARCHAR(200)",
                "ALTER TABLE hd_uf_records ALTER COLUMN recorded_at SET DEFAULT NOW()",
                "ALTER TABLE hd_uf_records ALTER COLUMN recorded_at DROP NOT NULL",
                # Create SOP files table
                """CREATE TABLE IF NOT EXISTS hd_sop_files (
                    id SERIAL PRIMARY KEY, sop_key VARCHAR(80) NOT NULL,
                    filename VARCHAR(255) NOT NULL, content_type VARCHAR(100),
                    file_size INTEGER, file_data BYTEA NOT NULL,
                    uploaded_by VARCHAR(100), uploaded_at TIMESTAMP DEFAULT NOW())""",
                # B6: lab results copied from the document system (hd_module/lab_sync.py)
                """CREATE TABLE IF NOT EXISTS hd_lab_docs (
                    id SERIAL PRIMARY KEY, patient_id INTEGER REFERENCES hd_patient_mapping(id),
                    hn VARCHAR(20) NOT NULL, doc_id VARCHAR(80) UNIQUE NOT NULL,
                    specimen_date DATE, doc_date DATE, status VARCHAR(30),
                    hb NUMERIC(5,1), hct NUMERIC(5,1), k NUMERIC(4,2), na NUMERIC(5,1), ca NUMERIC(5,2),
                    phos NUMERIC(5,2), albumin NUMERIC(4,2), pth NUMERIC(8,1), ferritin NUMERIC(8,1),
                    tsat NUMERIC(5,1), ktv NUMERIC(4,2), urr NUMERIC(5,1), creatinine NUMERIC(6,2),
                    bun NUMERIC(6,1), glucose NUMERIC(6,1),
                    hbsag VARCHAR(20), antihcv VARCHAR(20), antihbs VARCHAR(20),
                    synced_at TIMESTAMP DEFAULT NOW())""",
                "CREATE INDEX IF NOT EXISTS ix_hd_lab_docs_hn_date ON hd_lab_docs(hn, specimen_date DESC)",
                # manual lab form: adequacy values
                "ALTER TABLE hd_lab_results ADD COLUMN IF NOT EXISTS kt_v NUMERIC(4,2)",
                "ALTER TABLE hd_lab_results ADD COLUMN IF NOT EXISTS urr NUMERIC(5,1)",
                # equipment registry: purchase year + service contract
                "ALTER TABLE hd_equipment ADD COLUMN IF NOT EXISTS purchase_year INTEGER",
                "ALTER TABLE hd_equipment ADD COLUMN IF NOT EXISTS contract_end DATE",
                "ALTER TABLE hd_equipment ADD COLUMN IF NOT EXISTS contract_vendor VARCHAR(100)",
                # annual roster import 2569: ESA injections/week + usual shift
                "ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS esa_per_week NUMERIC(4,2)",
                "ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS default_shift VARCHAR(20)",
                    "ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS patient_goal TEXT",
                    "ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS goal_set_by VARCHAR(100)",
                    "ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS goal_set_at TIMESTAMP",
                # Fluid Status (BCM)
                __import__("hd_module.fluid_status", fromlist=["CREATE_SQL"]).CREATE_SQL,
                __import__("hd_module.fluid_status", fromlist=["CREATE_INDEX_SQL"]).CREATE_INDEX_SQL,
                __import__("hd_module.fluid_status", fromlist=["CREATE_MAP_SQL"]).CREATE_MAP_SQL,
                # PDPA consents
                __import__("hd_module.pdpa", fromlist=["CREATE_SQL"]).CREATE_SQL,
                __import__("hd_module.pdpa", fromlist=["CREATE_INDEX_SQL"]).CREATE_INDEX_SQL,
                # Dry Weight Autopilot proposals
                __import__("hd_module.dw_autopilot", fromlist=["CREATE_SQL"]).CREATE_SQL,
                # ตรวจระบบน้ำ RO ประจำวัน (Hardness / Free Cl / Total Cl)
                *[stmt.strip() for stmt in __import__("hd_module.water_check", fromlist=["CREATE_SQL"]).CREATE_SQL.split(";") if stmt.strip()],
                # 18 พ.ย. 2569: Machine-Bed Link — เตียง 28 เตียง + ผูกเครื่องฟอกกับ session
                "ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS equipment_id INTEGER REFERENCES hd_equipment(id)",
                # แบบ C (24 พ.ย. 2569): ปล่อยเตียงก่อน สรุป Post-HD ทีหลัง
                "ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS bed_released_at TIMESTAMP",
                "ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS released_by VARCHAR(80)",
                # index กันเตียงซ้อนต้องไม่นับรอบที่ปล่อยเตียงแล้ว — ถ้า index เก่า
                # (ไม่มีเงื่อนไข bed_released_at) ยังอยู่ ให้ถอนก่อนแล้วสร้างใหม่
                """DO $$ BEGIN
                     IF EXISTS (SELECT 1 FROM pg_indexes
                                WHERE indexname = 'ux_hd_sessions_bed_active'
                                  AND indexdef NOT LIKE '%bed_released_at%') THEN
                       DROP INDEX ux_hd_sessions_bed_active;
                     END IF;
                   END $$""",
                """CREATE UNIQUE INDEX IF NOT EXISTS ux_hd_sessions_bed_active
                   ON hd_sessions (bed_number, session_date, COALESCE(shift, 'morning'))
                   WHERE status IN ('scheduled', 'in_progress') AND bed_released_at IS NULL""",
                # 18 พ.ย. 2569: วันเกิด + เพศ สำหรับ Work Sheet PDF
                "ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS date_of_birth DATE",
                "ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS gender VARCHAR(10)",
                # Set alert_recipients
                """INSERT INTO hd_settings (key, value, description, updated_at)
                    VALUES ('alert_recipients', 'U1b1068cde88a06e88e53c014d1b83824', 'LINE userId แพทย์', NOW())
                    ON CONFLICT (key) DO NOTHING""",
                # F22: HD Work Sheet Interactive Data
                """CREATE TABLE IF NOT EXISTS hd_worksheet_data (
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
                )""",
                # Doctor Note interactive fields
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
            ]
            # ── Pre-HD Check-in: คำตอบ 3 ข้อจากผู้ป่วยก่อนมาฟอก (ทาง LINE) ──
            fixes.append("""
                CREATE TABLE IF NOT EXISTS hd_prehd_checkins (
                    id SERIAL PRIMARY KEY,
                    hn TEXT NOT NULL,
                    checkin_date DATE NOT NULL,
                    weight_home NUMERIC,
                    symptoms TEXT,
                    meds_taken TEXT,
                    flagged BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (hn, checkin_date)
                )
            """)
            # ── Family Link: ญาติรับสรุปหลังฟอกทาง LINE (ผูกด้วยรหัสจากเจ้าหน้าที่) ──
            fixes.append("""
                CREATE TABLE IF NOT EXISTS hd_family_codes (
                    code TEXT PRIMARY KEY,
                    hn TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    expires_at TIMESTAMPTZ,
                    used_at TIMESTAMPTZ
                )
            """)
            fixes.append("""
                CREATE TABLE IF NOT EXISTS hd_family_links (
                    id SERIAL PRIMARY KEY,
                    hn TEXT NOT NULL,
                    line_user_id TEXT NOT NULL,
                    label TEXT,
                    display_name TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (hn, line_user_id)
                )
            """)
            # ── เหตุผลการยกเลิกรอบฟอก (สถิติ no-show + audit) ──
            fixes.append("ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS cancel_reason TEXT")
            # ── 22 พ.ย. 2569 ระบบยาครบวงจร (แพทย์สั่ง): สถานะยืนยัน/ยาระหว่างฟอก/แพ้ยา ──
            fixes.append("ALTER TABLE hd_medication_orders ADD COLUMN IF NOT EXISTS verify_status VARCHAR(20) DEFAULT 'confirmed'")
            fixes.append("ALTER TABLE hd_medication_orders ADD COLUMN IF NOT EXISTS intra_hd BOOLEAN DEFAULT FALSE")
            fixes.append("ALTER TABLE hd_medication_orders ADD COLUMN IF NOT EXISTS stop_reason VARCHAR(200)")
            fixes.append("ALTER TABLE hd_medication_orders ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'manual'")
            fixes.append("ALTER TABLE hd_medication_orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP")
            fixes.append("ALTER TABLE hd_medication_orders ADD COLUMN IF NOT EXISTS updated_by INTEGER")
            fixes.append("ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS drug_allergies TEXT")
            # 22 พ.ย. 2569 (บ่าย): อ้างอิงรูปต้นฉบับของร่างยาจาก LINE (เทียบตอนยืนยัน)
            fixes.append("ALTER TABLE hd_medication_orders ADD COLUMN IF NOT EXISTS source_doc TEXT")
            # ── กันเตียงซ้อนระดับ DB: 1 เตียง/วัน/รอบ มี session ทำงานได้ 1 เดียว ──
            # (ปิดช่องโหว่ 2 คนกด submit พร้อมกันเสี้ยววินาที — app เช็คอยู่แล้ว นี่คือชั้นสุดท้าย)
            fixes.append("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_hd_sessions_bed_active
                ON hd_sessions (bed_number, session_date, COALESCE(shift, 'morning'))
                WHERE status IN ('scheduled', 'in_progress')
            """)
            # ── Unified Lab VIEW: hd_labs ──────────────────────────
            # Merge hd_lab_results (manual) + hd_lab_docs (OCR) into one VIEW
            # with standardised column names so every query uses one source.
            # Unified intra-HD record + idempotency (24 พ.ย. 2569, แพทย์สั่ง — บั๊กแถวซ้ำจาก retry/กดซ้ำ)
            fixes.append("ALTER TABLE hd_flowsheet ADD COLUMN IF NOT EXISTS dfr INTEGER")
            fixes.append("""
                CREATE TABLE IF NOT EXISTS hd_intra_submissions (
                    id SERIAL PRIMARY KEY,
                    session_id INTEGER NOT NULL,
                    idem_key VARCHAR(64) NOT NULL,
                    kind VARCHAR(20) NOT NULL,
                    result_json TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (session_id, idem_key)
                )
            """)
            # Lab verify gate (2026-11-22): verified_status/by/at + delta_flags
            fixes.append("ALTER TABLE hd_lab_docs ADD COLUMN IF NOT EXISTS verified_status VARCHAR(20) DEFAULT 'pending'")
            fixes.append("ALTER TABLE hd_lab_docs ADD COLUMN IF NOT EXISTS verified_by VARCHAR(80)")
            fixes.append("ALTER TABLE hd_lab_docs ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP")
            fixes.append("ALTER TABLE hd_lab_docs ADD COLUMN IF NOT EXISTS delta_flags TEXT")
            # Analyte expansion (2026-11-22): PLT / HCO3 / WBC / Anti-HIV
            fixes.append("ALTER TABLE hd_lab_docs ADD COLUMN IF NOT EXISTS plt NUMERIC(7,1)")
            fixes.append("ALTER TABLE hd_lab_docs ADD COLUMN IF NOT EXISTS hco3 NUMERIC(4,1)")
            fixes.append("ALTER TABLE hd_lab_docs ADD COLUMN IF NOT EXISTS wbc NUMERIC(6,2)")
            fixes.append("ALTER TABLE hd_lab_docs ADD COLUMN IF NOT EXISTS antihiv VARCHAR(20)")
            # one-time seed for rows that existed before the gate (idempotent):
            # doc-system "approved" means the physician already reviewed them there
            fixes.append("""
                UPDATE hd_lab_docs
                SET verified_status = CASE WHEN status = 'approved' THEN 'verified' ELSE 'pending' END,
                    verified_by     = CASE WHEN status = 'approved' THEN 'doc_system' ELSE NULL END
                WHERE verified_status IS NULL
            """)
            fixes.append("""
                CREATE OR REPLACE VIEW hd_labs AS
                SELECT patient_id, lab_date,
                       hemoglobin AS hb, hematocrit AS hct,
                       potassium AS k, sodium AS na,
                       calcium AS ca, phosphate AS phos,
                       albumin, ipth AS pth,
                       ferritin, tsat,
                       kt_v AS ktv, urr,
                       bun, creatinine,
                       NULL::NUMERIC AS glucose,
                       hepatitis_b AS hbsag, hepatitis_c AS antihcv, NULL::VARCHAR AS antihbs,
                       source, 'lab_results' AS _table,
                       'manual'::VARCHAR AS verified_status, NULL::TEXT AS delta_flags,
                       NULL::INTEGER AS doc_row_id,
                       NULL::NUMERIC AS plt, NULL::NUMERIC AS hco3,
                       NULL::NUMERIC AS wbc, NULL::VARCHAR AS antihiv
                FROM hd_lab_results
                UNION ALL
                SELECT patient_id, COALESCE(specimen_date, doc_date) AS lab_date,
                       hb, hct,
                       k, na,
                       ca, phos,
                       albumin, pth,
                       ferritin, tsat,
                       ktv, urr,
                       bun, creatinine,
                       glucose,
                       hbsag, antihcv, antihbs,
                       'ocr' AS source, 'lab_docs' AS _table,
                       COALESCE(verified_status, 'pending') AS verified_status, delta_flags,
                       id AS doc_row_id,
                       plt, hco3, wbc, antihiv
                FROM hd_lab_docs
            """)

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

        # Import DOB + BFR from Excel data (safe to run repeatedly)
        try:
            from .patient_dob_bfr import apply_dob_bfr
            apply_dob_bfr(conn)
        except Exception as dob_err:
            logger.warning("DOB/BFR import: %s", dob_err)

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
                # One-time password reset migration (v13): reset default accounts
                # to known passwords so login works after bcrypt hash mismatch fix
                cur.execute("SELECT 1 FROM hd_settings WHERE key = 'pw_reset_v13'")
                if not cur.fetchone():
                    logger.info("HD users: one-time password reset (v13)")
                    reset_map = {
                        "dr.kamphon": os.environ.get("HD_PHYSICIAN_PW", "1234"),
                        "nurse1": os.environ.get("HD_NURSE_PW", "1234"),
                        "hd_admin": os.environ.get("HD_ADMIN_PW", "admin1234"),
                    }
                    for uname, pw in reset_map.items():
                        cur.execute("UPDATE hd_users SET password_hash = %s, failed_attempts = 0, locked_until = NULL WHERE username = %s",
                                    (hash_password(pw), uname))
                    try:
                        cur.execute("INSERT INTO hd_settings (key, value) VALUES ('pw_reset_v13', 'done') ON CONFLICT DO NOTHING")
                    except Exception:
                        pass  # hd_settings might not exist yet
                    conn.commit()
                    logger.info("HD users: passwords reset to defaults")
                else:
                    # Normal boot — just clear lockouts
                    cur.execute("UPDATE hd_users SET failed_attempts = 0, locked_until = NULL WHERE failed_attempts > 0 OR locked_until IS NOT NULL")
                    conn.commit()
                logger.info("HD users: %d exist", count)
                conn.close()
                return

        # Default accounts — passwords from ENV or generated
        admin_secret = os.environ.get("ADMIN_SECRET", "changeme")
        default_users = [
            {
                "username": "dr.kamphon",
                "password": os.environ.get("HD_PHYSICIAN_PW", "1234"),
                "full_name": "นพ.กัมพล พัฒนสมบัติสกุล",
                "role": "physician",
                "license_number": "ว.26015",
            },
            {
                "username": "nurse1",
                "password": os.environ.get("HD_NURSE_PW", "1234"),
                "full_name": "พยาบาลไตเทียม 1",
                "role": "nurse",
                "license_number": "",
            },
            {
                "username": "hd_admin",
                "password": os.environ.get("HD_ADMIN_PW", "admin1234"),
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
            # 24 พ.ย. 2569 (แพทย์อนุมัติ แบบ ก): รอบที่ "ปล่อยเตียงรอสรุป Post-HD" (bed_released_at
            # ไม่ว่าง) ไม่ถูกปิดอัตโนมัติ — เตียงว่างอยู่แล้ว และบันทึกทางการแพทย์ต้องปิดโดยคน
            # (LINE 21:00 เตือนจนกว่าจะสรุป) รอบที่ค้างโดยไม่ได้กดอะไรเลยยังถูกปิดตามเดิม
            cur.execute("""
                UPDATE hd_sessions
                SET status = 'completed',
                    ended_at = NOW(),
                    notes = COALESCE(notes, '') || ' [ปิดอัตโนมัติ: รอบค้างเกิน 8 ชม. — ไม่มีข้อมูล Post-HD]'
                WHERE status = 'in_progress'
                  AND started_at < NOW() - INTERVAL '8 hours'
                  AND bed_released_at IS NULL
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

        # Upload to GCS — reuse the server's authenticated StorageGCS bucket
        # (GCS_CREDENTIALS_BASE64) when available; a bare Client() has no
        # credentials on Render and was the cause of the 403.
        try:
            bucket = None
            storage_obj = _BACKUP_STORAGE_OBJ
            if storage_obj is not None:
                bucket = getattr(storage_obj, "_bucket", None)
            if bucket is None:
                from google.cloud import storage as gcs_storage
                client = gcs_storage.Client()
                bucket = client.bucket(bucket_name)
            # Object Creator role cannot overwrite → unique name per boot
            blob_path = f"backups/hd_backup_{today}_{datetime.now().strftime('%H%M%S')}.json"
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


def _claim_once(app, job, day):
    """Cross-worker / cross-restart 'run once per day' guard using a GCS
    create-only marker (_markers/hd_<job>_<date>.json). Falls back to
    allowing the run when GCS is unavailable."""
    import json as _json
    bucket = getattr(app.config.get("STORAGE_OBJ"), "_bucket", None)
    if bucket is None:
        return True
    try:
        bucket.blob(f"_markers/hd_{job}_{day.isoformat()}.json").upload_from_string(
            _json.dumps({"job": job, "date": day.isoformat()}),
            content_type="application/json", if_generation_match=0)
        return True
    except Exception as e:
        if getattr(e, "code", None) == 412:
            return False
        logger.warning("HD once-marker %s: %s", job, type(e).__name__)
        return True


def _start_daily_reminder_scheduler(app):
    """Start the background scheduler thread (every 5 min) + a watchdog that
    restarts it if it stops ticking (ops_status, 14 Nov 2026)."""
    import threading
    import time
    from datetime import datetime
    from . import ops_status as ops

    def _boot_tasks():
        try:
            global _BACKUP_STORAGE_OBJ
            _BACKUP_STORAGE_OBJ = app.config.get("STORAGE_OBJ")
            _auto_backup_hd_tables()
        except Exception as e:
            logger.error("HD boot backup error: %s", e)
        # B12: remove files whose water-quality record was deleted earlier
        try:
            import os as _os
            import psycopg2 as _pg
            _db = _os.environ.get("HD_DATABASE_URL", "") or _os.environ.get("DATABASE_URL", "")
            if _db:
                from .blueprint import cleanup_orphan_files
                _c = _pg.connect(_db, connect_timeout=5)
                try:
                    _n = cleanup_orphan_files(_c)
                    if _n:
                        logger.info("HD orphan files removed: %d", _n)
                finally:
                    _c.close()
        except Exception as e:
            logger.warning("HD orphan cleanup: %s", e)
        # B11: positive confirmation that this commit is live (once per commit)
        try:
            with app.app_context():
                from .blueprint import announce_deploy_once
                ops.run_guarded("deploy_announce", announce_deploy_once, timeout=60)
        except Exception as e:
            logger.warning("deploy announce: %s", e)

    def _loop(first=True):
        from datetime import timezone as _tz, timedelta as _td
        _thai_tz = _tz(_td(hours=7))
        last_sent_date = None
        last_export_date = None
        last_brief_date = None
        last_checkin_date = None
        if first:
            time.sleep(90)   # let server.py finish wiring storage / LINE
            _boot_tasks()
        lab_tick = 0
        while True:
            try:
                ops.tick()
                now = datetime.now(_thai_tz)
                # 🕓 25 พ.ย. 2569: ทุก 5 นาที (05:00–23:00) — รอบที่ปล่อยเตียงแล้ว ≥30/60 นาทียังไม่สรุป → เตือนกลุ่มพยาบาล
                # (marker GCS ต่อรอบ/stage กันส่งซ้ำข้าม restart; ล้มได้ไม่กระทบงานอื่น)
                if 5 <= now.hour <= 23:
                    with app.app_context():
                        try:
                            from .blueprint import run_pending_summary_reminders
                            _sr = ops.run_guarded("summary_reminders", run_pending_summary_reminders,
                                                  claim_fn=lambda job, day: _claim_once(app, job, day), timeout=60) or {}
                            if _sr.get("sent"):
                                logger.info("Pending-summary reminders: %s", _sr)
                        except Exception as _sre:
                            logger.warning("summary reminders: %s", _sre)
                # Every 30 min (first pass right away): lab documents → hd_lab_docs
                if lab_tick % 6 == 0:
                    with app.app_context():
                        from .blueprint import run_lab_sync
                        st = ops.run_guarded("lab_sync", run_lab_sync, timeout=180) or {}
                        if st.get("inserted"):
                            logger.info("Lab sync: %s", st)
                        # 🛡 22 พ.ย. 2569: ใบแล็บใหม่มีผลบวก/ค่าติดธง → แจ้ง LINE ทันที
                        _lab_alerts = st.get("alerts") or []
                        if _lab_alerts:
                            _lp = app.config.get("LINE_PUSH_FN")
                            if _lp:
                                try:
                                    _lp("🧪 ใบแล็บใหม่จาก LINE ต้องดู:\n" + "\n".join(_lab_alerts[:10])
                                        + ("\n…และอีก " + str(len(_lab_alerts) - 10) + " รายการ" if len(_lab_alerts) > 10 else "")
                                        + "\n👉 เปิด 🗂 กล่องรอหมอ เพื่อยืนยัน")
                                except Exception as _le:
                                    logger.warning("lab alert push: %s", _le)
                # 💊 ทุก 15 นาที: รูปยาจาก LINE → AI อ่าน → ร่างยารอแพทย์ยืนยัน (22 พ.ย. 2569)
                if lab_tick % 3 == 0:
                    with app.app_context():
                        from .blueprint import run_med_photo_sync
                        ms = ops.run_guarded("med_photo_sync", run_med_photo_sync, timeout=300) or {}
                        if ms.get("drafted"):
                            logger.info("Med photo sync: %s", ms)
                        # ว้าว #3: new Hb → ESA proposals (once per hour window, 06:00–20:00 Thai)
                        if lab_tick % 12 == 0 and 6 <= now.hour <= 20:
                            from .blueprint import run_esa_autopilot
                            r = ops.run_guarded("esa_autopilot", run_esa_autopilot, timeout=180) or {}
                            if r.get("created"):
                                logger.info("ESA autopilot: %s proposals", r["created"])
                        # ว้าว รอบ 2 #1: Dry Weight review — once a day at the 07:00 tick
                        if lab_tick % 12 == 0 and now.hour == 7:
                            from .blueprint import run_dw_autopilot
                            r = ops.run_guarded("dw_autopilot", run_dw_autopilot, timeout=180) or {}
                            if r.get("created"):
                                logger.info("DW autopilot: %s proposals", r["created"])
                        # LINE monthly quota — once an hour (and on the first pass)
                        if lab_tick % 12 == 0:
                            import os as _os
                            try:
                                from .blueprint import get_setting_value as _gsv
                                ops.set_reserve(int(_gsv("line_quota_reserve", "") or ops.QUOTA_RESERVE_DEFAULT))
                            except Exception:
                                pass
                            ops.check_line_quota(_os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip())
                lab_tick += 1
                # 🌅 06:30: Pre-HD Check-in — ลิงก์ถาม 3 ข้อถึงคนไข้ที่มีคิวฟอกวันนี้ (WOW 21 พ.ย. 2026)
                if now.hour == 6 and now.minute >= 30 and last_checkin_date != now.date():
                    last_checkin_date = now.date()
                    if _claim_once(app, "prehd_checkin", now.date()):
                        with app.app_context():
                            import os as _os_ci
                            _dbu_ci = _os_ci.environ.get("HD_DATABASE_URL", "") or _os_ci.environ.get("DATABASE_URL", "")
                            _pf_ci = app.config.get("LINE_PUSH_FN")
                            if _dbu_ci and _pf_ci:
                                from .patient_engage import send_prehd_checkins
                                _r_ci = ops.run_guarded("prehd_checkin", send_prehd_checkins, _pf_ci, _dbu_ci, timeout=300)
                                logger.info("Pre-HD checkin: %s", _r_ci)
                # แบบ C (24 พ.ย. 2569): 21:00 เตือนรอบที่ยังไม่ปิด/ค้างสรุป Post-HD
                if now.hour == 21 and now.minute < 5:
                    if _claim_once(app, "pending_summary_reminder", now.date()):
                        try:
                            import os as _os_ps
                            import psycopg2 as _pg_ps
                            _dbu_ps = _os_ps.environ.get("HD_DATABASE_URL", "") or _os_ps.environ.get("DATABASE_URL", "")
                            _pf_ps = app.config.get("LINE_PUSH_FN")
                            if _dbu_ps and _pf_ps:
                                _c_ps = _pg_ps.connect(_dbu_ps, connect_timeout=5)
                                with _c_ps.cursor() as _cu_ps:
                                    _cu_ps.execute("""SELECT p.hn, p.patient_name, s.bed_number,
                                                             s.bed_released_at IS NOT NULL
                                                      FROM hd_sessions s
                                                      JOIN hd_patient_mapping p ON p.id = s.patient_id
                                                      WHERE s.status = 'in_progress'
                                                        AND (s.bed_released_at IS NOT NULL
                                                             OR s.started_at < NOW() - INTERVAL '6 hours')
                                                      ORDER BY s.bed_released_at NULLS LAST""")
                                    _rows_ps = _cu_ps.fetchall()
                                _c_ps.close()
                                if _rows_ps:
                                    _ls_ps = "\n".join(
                                        f"• HN {r[0]} {r[1] or ''} (เตียง {r[2]}"
                                        + (" · ปล่อยเตียงแล้ว รอสรุป" if r[3] else " · ยังไม่ปิดรอบ") + ")"
                                        for r in _rows_ps[:12])
                                    _pf_ps("🕓 รอบค้างสรุป Post-HD " + str(len(_rows_ps)) + " ราย — ปิดรอบให้เอกสารครบ 100% ด้วยครับ:\n"
                                           + _ls_ps + "\n👉 เปิด Dashboard ดูถาด 🕓 รอสรุป")
                        except Exception as _ps_e:
                            logger.warning("pending summary reminder: %s", _ps_e)

                # Send at 18:00 Thai time
                if now.hour == 18 and now.minute < 5 and last_sent_date != now.date():
                    last_sent_date = now.date()
                    if _claim_once(app, "pre_hd_reminder", now.date()):
                        with app.app_context():
                            import os
                            db_url = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
                            push_fn = app.config.get("LINE_PUSH_FN")
                            registry = app.config.get("MASTER_REGISTRY")
                            if db_url and push_fn:
                                from .pre_hd_reminder import send_pre_hd_reminders
                                result = ops.run_guarded("pre_hd_reminder", send_pre_hd_reminders, push_fn, db_url, registry, timeout=300)
                                logger.info("Daily pre-HD reminder: %s", result)
                            # ว้าว รอบ 2 #3: tomorrow's bed plan → nurse group
                            from .blueprint import push_tomorrow_schedule
                            logger.info("Tomorrow bed plan pushed: %s", ops.run_guarded("bed_plan_push", push_tomorrow_schedule, timeout=120))
                # 07:30 Thai time: Morning Brief → physician LINE (B7)
                if now.hour == 7 and now.minute >= 30 and last_brief_date != now.date():
                    last_brief_date = now.date()
                    if _claim_once(app, "morning_brief", now.date()):
                        with app.app_context():
                            from .blueprint import run_morning_brief
                            logger.info("Morning brief: %s", ops.run_guarded("morning_brief", run_morning_brief, trigger="scheduled", timeout=180))
                # Go-Live R1: 23:00 Thai time — full DB backup (+ restore test on Mondays)
                # Runs in its own thread with a timeout so a slow dump/upload can never block the loop.
                if now.hour == 23 and now.minute < 30 and _claim_once(app, "db_backup", now.date()):
                    with app.app_context():
                        from .blueprint import run_db_backup
                        st = ops.run_guarded("db_backup", run_db_backup, trigger="scheduled",
                                             restore_test=(now.weekday() == 0), timeout=900) or {}
                        logger.info("DB backup: %s tables %s rows gcs=%s drive=%s verify=%s",
                                    st.get("tables"), st.get("rows"), bool(st.get("gcs")), st.get("drive"), st.get("verify"))
                # 22:30 Thai time: export today's records (CSV + PDFs) to GCS
                if now.hour == 22 and now.minute >= 30 and last_export_date != now.date():
                    last_export_date = now.date()
                    if _claim_once(app, "daily_export", now.date()):
                        with app.app_context():
                            from .blueprint import run_daily_export
                            res = ops.run_guarded("daily_export", run_daily_export, now.date(), trigger="scheduled", timeout=900) or {}
                            logger.info("Daily record export: ok=%s failed=%s folder=%s",
                                        res.get("ok"), res.get("failed"), res.get("folder"))
                    # 📼 Black Box (25 พ.ย. 2569): hash รายวันของ audit log → hd_settings + GCS (tamper-evident) — เบา, อ่านอย่างเดียว
                    if now.minute >= 40 and _claim_once(app, "audit_hash", now.date()):
                        try:
                            import psycopg2 as _pg2
                            from .blackbox import audit_hash_run as _ahr
                            _dbu = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
                            _c2 = _pg2.connect(_dbu, connect_timeout=5)
                            try:
                                _rec = _ahr(_c2, now.date(), getattr(app.config.get("STORAGE_OBJ"), "_bucket", None))
                            finally:
                                _c2.close()
                            logger.info("audit hash %s: rows=%s gcs=%s", _rec["date"], _rec["rows"], _rec.get("gcs"))
                        except Exception as _ahe:
                            logger.warning("audit hash: %s", type(_ahe).__name__)
                    # ว้าว รอบ 2 #2: 1st of the month → quality report for the previous month
                    if now.day == 1 and _claim_once(app, "quality_report", now.date()):
                        with app.app_context():
                            from .blueprint import run_quality_report
                            res = ops.run_guarded("quality_report", run_quality_report, trigger="scheduled", timeout=600) or {}
                            logger.info("Quality report %s: drive=%s line=%s", res.get("month"), res.get("drive"), res.get("line_sent"))
                    # Patient Education: weekly tips every Monday 10:00
                    if now.weekday() == 0 and now.hour == 10 and _claim_once(app, "patient_education", now.date()):
                        with app.app_context():
                            try:
                                from .patient_education import run_weekly_tips
                                from .blueprint import push_nurse_group, get_setting_value
                                from flask import current_app
                                _pe_push = current_app.config.get("LINE_PUSH_FN")
                                _pe_db = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
                                if _pe_db and _pe_push:
                                    import psycopg2 as _pg
                                    _pe_conn = _pg.connect(_pe_db, connect_timeout=10)
                                    _pe_res = run_weekly_tips(_pe_conn, _pe_push, get_setting_value)
                                    _pe_conn.close()
                                    logger.info("Patient education tip: %s", _pe_res)
                            except Exception as pe_err:
                                logger.warning("Patient education: %s", pe_err)
            except Exception as e:
                logger.error("Reminder scheduler error: %s", e)
            time.sleep(300)  # Check every 5 minutes

    def _spawn(first=True):
        t = threading.Thread(target=_loop, kwargs={"first": first}, daemon=True, name="hd-reminder")
        t.start()
        return t

    # ── เสถียรภาพ (24 พ.ย. 2569, 502 ตอนพยาบาลใช้พร้อมกัน) ──
    # gunicorn มี 2 worker แต่ละตัว import app → scheduler เคยรัน 2 ชุดซ้อน
    # (งานหนัก sync ใบยา/แล็บ/สำเนา วิ่งพร้อมกัน 2 เท่า แย่ง CPU/RAM กับคำขอพยาบาล)
    # ใช้ file lock บน RAM ให้มี worker เดียวที่รัน — worker ถูกรีไซเคิลเมื่อไหร่
    # lock จะหลุดเอง และ worker ใหม่ที่เกิดมาจะรับช่วงต่อ
    try:
        import fcntl
        import os as _os_lk
        _lk_dir = "/dev/shm" if _os_lk.path.isdir("/dev/shm") else "/tmp"
        _lk_fh = open(_os_lk.path.join(_lk_dir, "hd_scheduler.lock"), "w")
        fcntl.flock(_lk_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app.config["_HD_SCHED_LOCK_FH"] = _lk_fh  # ถือ handle ไว้ตลอดอายุ process
    except (OSError, BlockingIOError):
        logger.info("HD scheduler: worker อื่นถือ lock อยู่ — worker นี้ไม่รัน scheduler")
        return
    except Exception as _lk_e:
        logger.warning("HD scheduler lock unavailable (%s) — run without lock", _lk_e)

    _spawn(True)
    ops.start_watchdog(lambda: _spawn(False))
    logger.info("HD scheduler started (5-min loop + watchdog)")
