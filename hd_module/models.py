"""HD Smart Monitoring — SQLAlchemy Models (10 tables).

All tables use the ``hd_`` prefix so they coexist safely with any future
tables in the same PostgreSQL database.
"""

from datetime import datetime, timezone

try:
    from sqlalchemy import (
        Column, Integer, String, Text, Boolean, Float,
        DateTime, Date, ForeignKey, Index, Enum, Numeric,
        CheckConstraint, UniqueConstraint,
    )
    from sqlalchemy.orm import declarative_base, relationship
    HAS_SQLALCHEMY = True
    Base = declarative_base()
except ImportError:
    HAS_SQLALCHEMY = False
    # Stubs so module can be imported without SQLAlchemy

    class _Stub:
        def __init__(self, *a, **kw): pass
        def __call__(self, *a, **kw): return self
    Column = Integer = String = Text = Boolean = Float = _Stub  # type: ignore
    DateTime = Date = ForeignKey = Index = Enum = Numeric = _Stub  # type: ignore
    CheckConstraint = UniqueConstraint = _Stub  # type: ignore
    relationship = _Stub  # type: ignore

    class _BaseMeta(type):
        metadata = type("M", (), {"tables": {}})()

    class Base(metaclass=_BaseMeta):  # type: ignore
        __tablename__ = ""
        __table_args__: tuple = ()
        __table__ = type("T", (), {"columns": []})()

    def declarative_base(): return Base  # type: ignore  # noqa: E704


def _utcnow():
    return datetime.now(timezone.utc)


# ───────────────────────────────────────────────────────────
# 1. hd_patient_mapping
# ───────────────────────────────────────────────────────────
class HdPatientMapping(Base):
    __tablename__ = "hd_patient_mapping"

    id = Column(Integer, primary_key=True, autoincrement=True)
    hn = Column(String(20), unique=True, nullable=False, index=True)
    hospital_hn = Column(String(20))
    patient_name = Column(String(100))
    date_of_birth = Column(Date)
    gender = Column(String(10))
    blood_type = Column(String(5))
    allergies = Column(Text)
    primary_diagnosis = Column(String(100))
    vascular_access = Column(String(50))
    dry_weight = Column(Numeric(5, 1))
    schedule_days = Column(String(20))  # e.g. "mon,wed,fri"
    esa_drug = Column(String(50))
    esa_dose = Column(String(50))
    is_active = Column(Boolean, default=True, nullable=False)
    source_registry_hash = Column(String(64))  # SHA-256 of source record
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    sessions = relationship("HdSession", back_populates="patient")
    prescriptions = relationship("HdPrescription", back_populates="patient")

    def __repr__(self):
        return f"<HdPatientMapping hn={self.hn!r}>"


# ───────────────────────────────────────────────────────────
# 2. hd_sessions
# ───────────────────────────────────────────────────────────
class HdSession(Base):
    __tablename__ = "hd_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(Integer, ForeignKey("hd_patient_mapping.id"), nullable=False, index=True)
    bed_number = Column(Integer, nullable=False)
    session_date = Column(Date, nullable=False, index=True)
    shift = Column(String(10))  # morning, afternoon, evening
    dialyzer_type = Column(String(50))
    blood_flow_rate = Column(Integer)   # mL/min
    dialysate_flow_rate = Column(Integer)  # mL/min
    uf_goal = Column(Numeric(6, 1))     # mL
    pre_weight = Column(Numeric(5, 1))  # kg
    post_weight = Column(Numeric(5, 1))
    actual_uf = Column(Numeric(6, 1))
    kt_v = Column(Numeric(4, 2))
    urr = Column(Numeric(5, 2))
    heparin_dose = Column(String(50))
    access_type = Column(String(30))    # AVF, AVG, catheter
    status = Column(
        Enum("scheduled", "in_progress", "completed", "cancelled",
             name="hd_session_status"),
        default="scheduled", nullable=False,
    )
    nurse_id = Column(Integer, ForeignKey("hd_users.id"))
    physician_id = Column(Integer, ForeignKey("hd_users.id"))
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    notes = Column(Text)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    patient = relationship("HdPatientMapping", back_populates="sessions")
    vitals = relationship("HdVitalSign", back_populates="session", cascade="all, delete-orphan")
    uf_records = relationship("HdUfRecord", back_populates="session", cascade="all, delete-orphan")
    machine_obs = relationship("HdMachineObservation", back_populates="session", cascade="all, delete-orphan")
    alerts = relationship("HdClinicalAlert", back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_hd_sessions_date_bed", "session_date", "bed_number"),
        CheckConstraint("bed_number >= 1 AND bed_number <= 50", name="ck_bed_range"),
    )


# ───────────────────────────────────────────────────────────
# 3. hd_prescriptions
# ───────────────────────────────────────────────────────────
class HdPrescription(Base):
    __tablename__ = "hd_prescriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(Integer, ForeignKey("hd_patient_mapping.id"), nullable=False, index=True)
    frequency_per_week = Column(Integer, default=3)
    duration_hours = Column(Numeric(3, 1), default=4.0)
    dialyzer_type = Column(String(50))
    blood_flow_rate = Column(Integer)
    dialysate_flow_rate = Column(Integer)
    uf_max = Column(Numeric(6, 1))
    heparin_protocol = Column(String(100))
    access_type = Column(String(30))
    dry_weight_target = Column(Numeric(5, 1))
    prescribed_by = Column(Integer, ForeignKey("hd_users.id"))
    effective_date = Column(Date, nullable=False)
    discontinued_date = Column(Date)
    is_active = Column(Boolean, default=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    patient = relationship("HdPatientMapping", back_populates="prescriptions")


# ───────────────────────────────────────────────────────────
# 4. hd_vital_signs
# ───────────────────────────────────────────────────────────
class HdVitalSign(Base):
    __tablename__ = "hd_vital_signs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("hd_sessions.id"), nullable=False, index=True)
    phase = Column(
        Enum("pre", "intra", "post", name="hd_vital_phase"),
        nullable=False,
    )
    recorded_at = Column(DateTime, nullable=False, index=True)
    sbp = Column(Integer)              # mmHg
    dbp = Column(Integer)
    heart_rate = Column(Integer)       # bpm
    temperature = Column(Numeric(4, 1))  # °C
    spo2 = Column(Integer)             # %
    respiratory_rate = Column(Integer)
    symptoms = Column(Text)
    nurse_id = Column(Integer, ForeignKey("hd_users.id"))
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    session = relationship("HdSession", back_populates="vitals")

    __table_args__ = (
        CheckConstraint("sbp IS NULL OR (sbp >= 30 AND sbp <= 300)", name="ck_sbp_range"),
        CheckConstraint("dbp IS NULL OR (dbp >= 20 AND dbp <= 200)", name="ck_dbp_range"),
        CheckConstraint("heart_rate IS NULL OR (heart_rate >= 20 AND heart_rate <= 250)", name="ck_hr_range"),
    )


# ───────────────────────────────────────────────────────────
# 5. hd_uf_records
# ───────────────────────────────────────────────────────────
class HdUfRecord(Base):
    __tablename__ = "hd_uf_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("hd_sessions.id"), nullable=False, index=True)
    recorded_at = Column(DateTime, nullable=False)
    uf_removed = Column(Numeric(6, 1))      # mL
    uf_remaining = Column(Numeric(6, 1))
    uf_rate = Column(Numeric(6, 1))          # mL/hr
    nurse_id = Column(Integer, ForeignKey("hd_users.id"))
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    session = relationship("HdSession", back_populates="uf_records")


# ───────────────────────────────────────────────────────────
# 6. hd_machine_observations
# ───────────────────────────────────────────────────────────
class HdMachineObservation(Base):
    __tablename__ = "hd_machine_observations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("hd_sessions.id"), nullable=False, index=True)
    recorded_at = Column(DateTime, nullable=False)
    observation_type = Column(String(50), nullable=False)  # alarm, blood_leak, air_detect, clot
    severity = Column(String(20))
    description = Column(Text)
    action_taken = Column(Text)
    nurse_id = Column(Integer, ForeignKey("hd_users.id"))
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    session = relationship("HdSession", back_populates="machine_obs")


# ───────────────────────────────────────────────────────────
# 7. hd_clinical_alerts
# ───────────────────────────────────────────────────────────
class HdClinicalAlert(Base):
    __tablename__ = "hd_clinical_alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("hd_sessions.id"), nullable=True, index=True)
    patient_id = Column(Integer, ForeignKey("hd_patient_mapping.id"), nullable=True)
    alert_type = Column(String(50), nullable=False, index=True)
    severity = Column(
        Enum("critical", "warning", "info", name="hd_alert_severity"),
        nullable=False,
    )
    message = Column(Text, nullable=False)
    auto_generated = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False, index=True)

    session = relationship("HdSession", back_populates="alerts")
    acknowledgements = relationship("HdAlertAcknowledgement", back_populates="alert")

    __table_args__ = (
        Index("ix_hd_alerts_active", "is_active", "created_at"),
    )


# ───────────────────────────────────────────────────────────
# 8. hd_alert_acknowledgements
# ───────────────────────────────────────────────────────────
class HdAlertAcknowledgement(Base):
    __tablename__ = "hd_alert_acknowledgements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_id = Column(Integer, ForeignKey("hd_clinical_alerts.id"), nullable=False, index=True)
    acknowledged_by = Column(Integer, ForeignKey("hd_users.id"), nullable=False)
    acknowledged_at = Column(DateTime, default=_utcnow, nullable=False)
    action_taken = Column(Text)
    resolution_notes = Column(Text)
    escalated_to = Column(Integer, ForeignKey("hd_users.id"))
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    alert = relationship("HdClinicalAlert", back_populates="acknowledgements")


# ───────────────────────────────────────────────────────────
# 9. hd_users
# ───────────────────────────────────────────────────────────
class HdUser(Base):
    __tablename__ = "hd_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(
        Enum("physician", "nurse", "admin", "auditor", name="hd_user_role"),
        nullable=False,
    )
    license_number = Column(String(30))  # เลขใบอนุญาต
    is_active = Column(Boolean, default=True, nullable=False)
    mfa_secret = Column(String(64))       # TOTP secret (encrypted)
    mfa_enabled = Column(Boolean, default=False)
    last_login = Column(DateTime)
    failed_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        CheckConstraint("failed_attempts >= 0", name="ck_failed_attempts"),
    )


# ───────────────────────────────────────────────────────────
# 10. hd_audit_logs
# ───────────────────────────────────────────────────────────
class HdAuditLog(Base):
    __tablename__ = "hd_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("hd_users.id"), nullable=True)
    action = Column(String(100), nullable=False, index=True)
    resource_type = Column(String(50))   # session, vital, alert, user ...
    resource_id = Column(Integer)
    old_values = Column(Text)            # JSON
    new_values = Column(Text)            # JSON
    ip_address = Column(String(45))      # IPv6 max
    user_agent = Column(String(200))
    created_at = Column(DateTime, default=_utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_audit_user_action", "user_id", "action"),
    )
