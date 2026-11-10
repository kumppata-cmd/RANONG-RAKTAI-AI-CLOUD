"""HD Smart Monitoring — Database Migration Scripts.

Provides raw SQL for schema creation with IF NOT EXISTS guards,
so it is safe to run repeatedly.  Also includes rollback (drop) SQL.
"""

SCHEMA_VERSION = "1.0.0"

# ── Forward migration ─────────────────────────────────────

MIGRATION_UP = """
-- HD Smart Monitoring schema v1.0.0

-- Custom ENUM types
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'hd_session_status') THEN
        CREATE TYPE hd_session_status AS ENUM (
            'scheduled', 'in_progress', 'completed', 'cancelled'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'hd_vital_phase') THEN
        CREATE TYPE hd_vital_phase AS ENUM ('pre', 'intra', 'post');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'hd_alert_severity') THEN
        CREATE TYPE hd_alert_severity AS ENUM ('critical', 'warning', 'info');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'hd_user_role') THEN
        CREATE TYPE hd_user_role AS ENUM (
            'physician', 'nurse', 'admin', 'auditor'
        );
    END IF;
END $$;

-- 9. hd_users (must be before tables that reference it)
CREATE TABLE IF NOT EXISTS hd_users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(128) NOT NULL,
    full_name VARCHAR(100) NOT NULL,
    role hd_user_role NOT NULL,
    license_number VARCHAR(30),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    mfa_secret VARCHAR(64),
    mfa_enabled BOOLEAN DEFAULT FALSE,
    last_login TIMESTAMP,
    failed_attempts INTEGER DEFAULT 0 CHECK (failed_attempts >= 0),
    locked_until TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hd_users_username ON hd_users(username);

-- 1. hd_patient_mapping
CREATE TABLE IF NOT EXISTS hd_patient_mapping (
    id SERIAL PRIMARY KEY,
    hn VARCHAR(20) UNIQUE NOT NULL,
    hospital_hn VARCHAR(20),
    patient_name VARCHAR(100),
    date_of_birth DATE,
    gender VARCHAR(10),
    blood_type VARCHAR(5),
    allergies TEXT,
    primary_diagnosis VARCHAR(100),
    vascular_access VARCHAR(50),
    dry_weight NUMERIC(5,1),
    schedule_days VARCHAR(20),
    esa_drug VARCHAR(50),
    esa_dose VARCHAR(50),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    source_registry_hash VARCHAR(64),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hd_patient_hn ON hd_patient_mapping(hn);

-- 2. hd_sessions
CREATE TABLE IF NOT EXISTS hd_sessions (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES hd_patient_mapping(id),
    bed_number INTEGER NOT NULL CHECK (bed_number >= 1 AND bed_number <= 50),
    session_date DATE NOT NULL,
    shift VARCHAR(10),
    dialyzer_type VARCHAR(50),
    blood_flow_rate INTEGER,
    dialysate_flow_rate INTEGER,
    uf_goal NUMERIC(6,1),
    pre_weight NUMERIC(5,1),
    post_weight NUMERIC(5,1),
    actual_uf NUMERIC(6,1),
    kt_v NUMERIC(4,2),
    urr NUMERIC(5,2),
    heparin_dose VARCHAR(50),
    access_type VARCHAR(30),
    status hd_session_status NOT NULL DEFAULT 'scheduled',
    nurse_id INTEGER REFERENCES hd_users(id),
    physician_id INTEGER REFERENCES hd_users(id),
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hd_sessions_patient ON hd_sessions(patient_id);
CREATE INDEX IF NOT EXISTS ix_hd_sessions_date ON hd_sessions(session_date);
CREATE INDEX IF NOT EXISTS ix_hd_sessions_date_bed ON hd_sessions(session_date, bed_number);

-- 3. hd_prescriptions
CREATE TABLE IF NOT EXISTS hd_prescriptions (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES hd_patient_mapping(id),
    frequency_per_week INTEGER DEFAULT 3,
    duration_hours NUMERIC(3,1) DEFAULT 4.0,
    dialyzer_type VARCHAR(50),
    blood_flow_rate INTEGER,
    dialysate_flow_rate INTEGER,
    uf_max NUMERIC(6,1),
    heparin_protocol VARCHAR(100),
    access_type VARCHAR(30),
    dry_weight_target NUMERIC(5,1),
    prescribed_by INTEGER REFERENCES hd_users(id),
    effective_date DATE NOT NULL,
    discontinued_date DATE,
    is_active BOOLEAN DEFAULT TRUE,
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hd_rx_patient ON hd_prescriptions(patient_id);

-- 4. hd_vital_signs
CREATE TABLE IF NOT EXISTS hd_vital_signs (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES hd_sessions(id),
    phase hd_vital_phase NOT NULL,
    recorded_at TIMESTAMP NOT NULL,
    sbp INTEGER CHECK (sbp IS NULL OR (sbp >= 30 AND sbp <= 300)),
    dbp INTEGER CHECK (dbp IS NULL OR (dbp >= 20 AND dbp <= 200)),
    heart_rate INTEGER CHECK (heart_rate IS NULL OR (heart_rate >= 20 AND heart_rate <= 250)),
    temperature NUMERIC(4,1),
    spo2 INTEGER,
    respiratory_rate INTEGER,
    symptoms TEXT,
    nurse_id INTEGER REFERENCES hd_users(id),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hd_vitals_session ON hd_vital_signs(session_id);
CREATE INDEX IF NOT EXISTS ix_hd_vitals_time ON hd_vital_signs(recorded_at);

-- 5. hd_uf_records
CREATE TABLE IF NOT EXISTS hd_uf_records (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES hd_sessions(id),
    recorded_at TIMESTAMP NOT NULL,
    uf_removed NUMERIC(6,1),
    uf_remaining NUMERIC(6,1),
    uf_rate NUMERIC(6,1),
    nurse_id INTEGER REFERENCES hd_users(id),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hd_uf_session ON hd_uf_records(session_id);

-- 6. hd_machine_observations
CREATE TABLE IF NOT EXISTS hd_machine_observations (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES hd_sessions(id),
    recorded_at TIMESTAMP NOT NULL,
    observation_type VARCHAR(50) NOT NULL,
    severity VARCHAR(20),
    description TEXT,
    action_taken TEXT,
    nurse_id INTEGER REFERENCES hd_users(id),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hd_machine_session ON hd_machine_observations(session_id);

-- 7. hd_clinical_alerts
CREATE TABLE IF NOT EXISTS hd_clinical_alerts (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES hd_sessions(id),
    patient_id INTEGER REFERENCES hd_patient_mapping(id),
    alert_type VARCHAR(50) NOT NULL,
    severity hd_alert_severity NOT NULL,
    message TEXT NOT NULL,
    auto_generated BOOLEAN DEFAULT TRUE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hd_alerts_type ON hd_clinical_alerts(alert_type);
CREATE INDEX IF NOT EXISTS ix_hd_alerts_active ON hd_clinical_alerts(is_active, created_at);

-- 8. hd_alert_acknowledgements
CREATE TABLE IF NOT EXISTS hd_alert_acknowledgements (
    id SERIAL PRIMARY KEY,
    alert_id INTEGER NOT NULL REFERENCES hd_clinical_alerts(id),
    acknowledged_by INTEGER NOT NULL REFERENCES hd_users(id),
    acknowledged_at TIMESTAMP NOT NULL DEFAULT NOW(),
    action_taken TEXT,
    resolution_notes TEXT,
    escalated_to INTEGER REFERENCES hd_users(id),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hd_ack_alert ON hd_alert_acknowledgements(alert_id);

-- 10. hd_audit_logs
CREATE TABLE IF NOT EXISTS hd_audit_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES hd_users(id),
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(50),
    resource_id INTEGER,
    old_values TEXT,
    new_values TEXT,
    ip_address VARCHAR(45),
    user_agent VARCHAR(200),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hd_audit_action ON hd_audit_logs(action);
CREATE INDEX IF NOT EXISTS ix_hd_audit_time ON hd_audit_logs(created_at);
CREATE INDEX IF NOT EXISTS ix_hd_audit_user_action ON hd_audit_logs(user_id, action);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS hd_schema_version (
    version VARCHAR(20) PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT NOW(),
    description TEXT
);
INSERT INTO hd_schema_version (version, description)
VALUES ('1.0.0', 'Initial HD Smart Monitoring schema')
ON CONFLICT (version) DO NOTHING;

-- v1.1.0: Add bfr + hd_per_week to patient_mapping
DO $$ BEGIN
    ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS bfr INTEGER DEFAULT 0;
    ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS hd_per_week INTEGER DEFAULT 3;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;
INSERT INTO hd_schema_version (version, description)
VALUES ('1.1.0', 'Add bfr and hd_per_week to hd_patient_mapping')
ON CONFLICT (version) DO NOTHING;

-- v1.2.0: Settings table for runtime configuration (alert recipients etc.)
CREATE TABLE IF NOT EXISTS hd_settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    description TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_by INTEGER REFERENCES hd_users(id)
);
INSERT INTO hd_schema_version (version, description)
VALUES ('1.2.0', 'Add hd_settings key-value table')
ON CONFLICT (version) DO NOTHING;

-- v1.3.0: ESA injection tracking + Hb/Hct per session
DO $$ BEGIN
    ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS esa_given BOOLEAN;
    ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS esa_drug_given VARCHAR(50);
    ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS esa_dose_given VARCHAR(50);
    ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS esa_route VARCHAR(10) DEFAULT 'SC';
    ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS esa_skip_reason VARCHAR(200);
    ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS hb_value NUMERIC(4,1);
    ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS hct_value NUMERIC(4,1);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;
INSERT INTO hd_schema_version (version, description)
VALUES ('1.3.0', 'Add ESA injection tracking and Hb/Hct to hd_sessions')
ON CONFLICT (version) DO NOTHING;

-- v1.4.0: needs_review flag for incomplete patient data
DO $$ BEGIN
    ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS needs_review BOOLEAN DEFAULT FALSE;
    ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS registered_by VARCHAR(50);
    -- Mark auto-registered patients missing schedule as needs_review
    UPDATE hd_patient_mapping SET needs_review = TRUE
    WHERE is_active = TRUE
      AND (schedule_days IS NULL OR schedule_days = '')
      AND needs_review IS NOT TRUE;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;
INSERT INTO hd_schema_version (version, description)
VALUES ('1.4.0', 'Add needs_review flag for incomplete patient data')
ON CONFLICT (version) DO NOTHING;

-- v1.5.0: UF time-series + shift support
CREATE TABLE IF NOT EXISTS hd_uf_records (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES hd_sessions(id),
    recorded_at TIMESTAMP DEFAULT NOW(),
    slot_time VARCHAR(5),
    uf_removed INTEGER DEFAULT 0,
    uf_rate INTEGER DEFAULT 0,
    uf_remaining INTEGER DEFAULT 0,
    notes VARCHAR(200)
);
CREATE INDEX IF NOT EXISTS idx_hd_uf_session ON hd_uf_records(session_id);
DO $$ BEGIN
    ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS shift VARCHAR(20) DEFAULT 'morning';
EXCEPTION WHEN OTHERS THEN NULL;
END $$;
INSERT INTO hd_schema_version (version, description)
VALUES ('1.5.0', 'Add hd_uf_records table and shift to hd_sessions')
ON CONFLICT (version) DO NOTHING;

-- v1.6.0: Full HD Flowsheet (standard HD monitoring sheet)
CREATE TABLE IF NOT EXISTS hd_flowsheet (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES hd_sessions(id),
    slot_time VARCHAR(5) NOT NULL,
    recorded_at TIMESTAMP DEFAULT NOW(),
    -- Vital Signs
    sbp INTEGER,
    dbp INTEGER,
    pr INTEGER,
    rr INTEGER,
    temp NUMERIC(4,1),
    spo2 INTEGER,
    -- Machine Parameters
    bfr INTEGER,
    vp INTEGER,
    tmp INTEGER,
    ap INTEGER,
    conductivity NUMERIC(4,1),
    dialysate_temp NUMERIC(4,1),
    -- UF
    ufr INTEGER,
    total_uf INTEGER,
    -- Notes
    remark VARCHAR(500),
    nurse_id INTEGER REFERENCES hd_users(id)
);
CREATE INDEX IF NOT EXISTS idx_hd_flowsheet_session ON hd_flowsheet(session_id);
DO $$ BEGIN
    ALTER TABLE hd_sessions ADD COLUMN IF NOT EXISTS pre_hd_note TEXT;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;
INSERT INTO hd_schema_version (version, description)
VALUES ('1.6.0', 'Add hd_flowsheet full HD monitoring sheet')
ON CONFLICT (version) DO NOTHING;

-- v1.7.0: Clinical Upgrade — Medications, Labs, VA Log, Anticoag, Dialyzer

-- F15: Medication Orders (คำสั่งยาระหว่างฟอก)
CREATE TABLE IF NOT EXISTS hd_medication_orders (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES hd_patient_mapping(id),
    drug_name VARCHAR(100) NOT NULL,
    drug_category VARCHAR(50),
    dose VARCHAR(50),
    route VARCHAR(20) DEFAULT 'IV',
    frequency VARCHAR(50),
    is_standing BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    ordered_by INTEGER REFERENCES hd_users(id),
    ordered_at TIMESTAMP DEFAULT NOW(),
    notes VARCHAR(300)
);
CREATE TABLE IF NOT EXISTS hd_medication_given (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES hd_sessions(id),
    order_id INTEGER REFERENCES hd_medication_orders(id),
    drug_name VARCHAR(100) NOT NULL,
    dose_given VARCHAR(50),
    route VARCHAR(20),
    given_at TIMESTAMP DEFAULT NOW(),
    given_by INTEGER REFERENCES hd_users(id),
    slot_time VARCHAR(5),
    notes VARCHAR(200)
);
CREATE INDEX IF NOT EXISTS idx_hd_med_given_session ON hd_medication_given(session_id);

-- F16: Lab Results
CREATE TABLE IF NOT EXISTS hd_lab_results (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES hd_patient_mapping(id),
    lab_date DATE NOT NULL,
    bun NUMERIC(6,1),
    creatinine NUMERIC(5,2),
    potassium NUMERIC(4,2),
    sodium NUMERIC(5,1),
    calcium NUMERIC(5,2),
    phosphate NUMERIC(5,2),
    albumin NUMERIC(4,2),
    ipth NUMERIC(7,1),
    hemoglobin NUMERIC(4,1),
    hematocrit NUMERIC(4,1),
    ferritin NUMERIC(7,1),
    tsat NUMERIC(5,1),
    iron NUMERIC(6,1),
    tibc NUMERIC(6,1),
    wbc NUMERIC(6,2),
    platelet INTEGER,
    hba1c NUMERIC(4,1),
    uric_acid NUMERIC(5,2),
    ast NUMERIC(6,1),
    alt NUMERIC(6,1),
    hepatitis_b VARCHAR(20),
    hepatitis_c VARCHAR(20),
    hiv VARCHAR(20),
    notes VARCHAR(300),
    source VARCHAR(50) DEFAULT 'manual',
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hd_lab_patient ON hd_lab_results(patient_id, lab_date);

-- F17: Vascular Access Log
CREATE TABLE IF NOT EXISTS hd_va_assessments (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES hd_sessions(id),
    patient_id INTEGER NOT NULL REFERENCES hd_patient_mapping(id),
    va_type VARCHAR(20),
    site VARCHAR(50),
    thrill VARCHAR(20),
    bruit VARCHAR(20),
    needle_gauge VARCHAR(10),
    needle_site_a VARCHAR(50),
    needle_site_v VARCHAR(50),
    cannulation_ease VARCHAR(20),
    complications VARCHAR(200),
    appearance VARCHAR(100),
    assessed_by INTEGER REFERENCES hd_users(id),
    assessed_at TIMESTAMP DEFAULT NOW(),
    notes VARCHAR(300)
);
CREATE INDEX IF NOT EXISTS idx_hd_va_session ON hd_va_assessments(session_id);

-- F18: Anticoagulation
CREATE TABLE IF NOT EXISTS hd_anticoagulation (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES hd_sessions(id),
    protocol VARCHAR(30) DEFAULT 'standard_heparin',
    loading_dose INTEGER,
    loading_unit VARCHAR(10) DEFAULT 'IU',
    maintenance_dose INTEGER,
    maintenance_unit VARCHAR(20) DEFAULT 'IU/hr',
    total_dose INTEGER,
    heparin_free_reason VARCHAR(200),
    act_pre INTEGER,
    act_post INTEGER,
    notes VARCHAR(300),
    recorded_by INTEGER REFERENCES hd_users(id),
    recorded_at TIMESTAMP DEFAULT NOW()
);

-- F19: Dialyzer Reuse Tracking
CREATE TABLE IF NOT EXISTS hd_dialyzer_tracking (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES hd_patient_mapping(id),
    dialyzer_type VARCHAR(50) NOT NULL,
    dialyzer_serial VARCHAR(50),
    first_use_date DATE,
    reuse_count INTEGER DEFAULT 1,
    max_reuse INTEGER DEFAULT 20,
    tcv_initial NUMERIC(6,1),
    tcv_current NUMERIC(6,1),
    status VARCHAR(20) DEFAULT 'active',
    last_used_session INTEGER REFERENCES hd_sessions(id),
    last_reprocessed_at TIMESTAMP,
    visual_pass BOOLEAN,
    notes VARCHAR(300)
);
CREATE INDEX IF NOT EXISTS idx_hd_dialyzer_patient ON hd_dialyzer_tracking(patient_id);

INSERT INTO hd_schema_version (version, description)
VALUES ('1.7.0', 'Clinical upgrade: medications, labs, VA log, anticoagulation, dialyzer tracking')
ON CONFLICT (version) DO NOTHING;

-- v1.8.0: Daily Checklist for QA
CREATE TABLE IF NOT EXISTS hd_daily_checklist (
    id SERIAL PRIMARY KEY,
    checklist_date DATE NOT NULL,
    shift VARCHAR(20) NOT NULL DEFAULT 'morning',
    category VARCHAR(30) NOT NULL,  -- 'pre_shift', 'during_shift', 'post_shift'
    item_key VARCHAR(60) NOT NULL,
    item_label VARCHAR(200) NOT NULL,
    checked BOOLEAN NOT NULL DEFAULT FALSE,
    checked_by INTEGER REFERENCES hd_users(id),
    checked_at TIMESTAMP,
    notes VARCHAR(300),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(checklist_date, shift, item_key)
);
CREATE INDEX IF NOT EXISTS idx_hd_checklist_date ON hd_daily_checklist(checklist_date, shift);

INSERT INTO hd_schema_version (version, description)
VALUES ('1.8.0', 'Daily checklist for nursing QA')
ON CONFLICT (version) DO NOTHING;

-- v1.9.0: VA Location on patient record
ALTER TABLE hd_patient_mapping ADD COLUMN IF NOT EXISTS va_location VARCHAR(50);

INSERT INTO hd_schema_version (version, description)
VALUES ('1.9.0', 'VA location field on patient mapping')
ON CONFLICT (version) DO NOTHING;
"""

# ── Rollback migration ────────────────────────────────────

MIGRATION_DOWN = """
-- Rollback HD Smart Monitoring schema v1.0.0
DROP TABLE IF EXISTS hd_schema_version CASCADE;
DROP TABLE IF EXISTS hd_audit_logs CASCADE;
DROP TABLE IF EXISTS hd_alert_acknowledgements CASCADE;
DROP TABLE IF EXISTS hd_clinical_alerts CASCADE;
DROP TABLE IF EXISTS hd_machine_observations CASCADE;
DROP TABLE IF EXISTS hd_uf_records CASCADE;
DROP TABLE IF EXISTS hd_vital_signs CASCADE;
DROP TABLE IF EXISTS hd_prescriptions CASCADE;
DROP TABLE IF EXISTS hd_sessions CASCADE;
DROP TABLE IF EXISTS hd_patient_mapping CASCADE;
DROP TABLE IF EXISTS hd_users CASCADE;
DROP TYPE IF EXISTS hd_session_status;
DROP TYPE IF EXISTS hd_vital_phase;
DROP TYPE IF EXISTS hd_alert_severity;
DROP TYPE IF EXISTS hd_user_role;
"""


def apply_migration(engine):
    """Apply the forward migration."""
    with engine.connect() as conn:
        conn.execute(MIGRATION_UP)
        conn.commit()


def rollback_migration(engine):
    """Roll back the migration (destructive)."""
    with engine.connect() as conn:
        conn.execute(MIGRATION_DOWN)
        conn.commit()
