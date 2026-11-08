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
