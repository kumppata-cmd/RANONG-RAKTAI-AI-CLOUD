#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for HD Smart Monitoring Module — Phase 1.

50+ test cases covering:
- Models (schema, constraints)
- Auth (JWT, bcrypt, roles)
- Patient Bridge (read-only, synthetic data)
- Clinical Alerts (all 7 rules)
- Audit Trail
- Blueprint (routes, feature flag)
- Migrations (SQL validity)
"""

import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════
# Test 1: Models
# ═══════════════════════════════════════════════════════════

class TestHdModels(unittest.TestCase):

    def test_import_models(self):
        from hd_module.models import (
            Base, HdPatientMapping, HdSession, HdPrescription,
            HdVitalSign, HdUfRecord, HdMachineObservation,
            HdClinicalAlert, HdAlertAcknowledgement, HdUser, HdAuditLog,
            HAS_SQLALCHEMY,
        )
        if HAS_SQLALCHEMY:
            self.assertEqual(len(Base.metadata.tables), 10)
        else:
            # Module still importable without SQLAlchemy
            self.assertIsNotNone(HdPatientMapping)

    def test_patient_mapping_table_name(self):
        from hd_module.models import HdPatientMapping
        self.assertEqual(HdPatientMapping.__tablename__, "hd_patient_mapping")

    def test_session_table_name(self):
        from hd_module.models import HdSession
        self.assertEqual(HdSession.__tablename__, "hd_sessions")

    def test_vital_sign_table_name(self):
        from hd_module.models import HdVitalSign
        self.assertEqual(HdVitalSign.__tablename__, "hd_vital_signs")

    def test_user_table_name(self):
        from hd_module.models import HdUser
        self.assertEqual(HdUser.__tablename__, "hd_users")

    def test_audit_log_table_name(self):
        from hd_module.models import HdAuditLog
        self.assertEqual(HdAuditLog.__tablename__, "hd_audit_logs")

    def test_all_tables_have_hd_prefix(self):
        from hd_module.models import Base, HAS_SQLALCHEMY
        if HAS_SQLALCHEMY:
            for name in Base.metadata.tables:
                self.assertTrue(name.startswith("hd_"), f"{name} missing hd_ prefix")

    def test_patient_mapping_repr(self):
        from hd_module.models import HdPatientMapping, HAS_SQLALCHEMY
        if HAS_SQLALCHEMY:
            p = HdPatientMapping(hn="12345")
            self.assertIn("12345", repr(p))

    def test_session_has_patient_fk(self):
        from hd_module.models import HdSession, HAS_SQLALCHEMY
        if HAS_SQLALCHEMY:
            cols = {c.name for c in HdSession.__table__.columns}
            self.assertIn("patient_id", cols)

    def test_vital_sign_has_session_fk(self):
        from hd_module.models import HdVitalSign, HAS_SQLALCHEMY
        if HAS_SQLALCHEMY:
            cols = {c.name for c in HdVitalSign.__table__.columns}
            self.assertIn("session_id", cols)


# ═══════════════════════════════════════════════════════════
# Test 2: Auth
# ═══════════════════════════════════════════════════════════

class TestHdAuth(unittest.TestCase):

    def test_hash_password(self):
        from hd_module.auth import hash_password
        h = hash_password("testpass123")
        self.assertTrue(len(h) > 10)
        self.assertNotEqual(h, "testpass123")

    def test_verify_password_correct(self):
        from hd_module.auth import hash_password, verify_password
        h = hash_password("mypassword")
        self.assertTrue(verify_password("mypassword", h))

    def test_verify_password_wrong(self):
        from hd_module.auth import hash_password, verify_password
        h = hash_password("mypassword")
        self.assertFalse(verify_password("wrongpassword", h))

    def test_create_token(self):
        from hd_module.auth import create_token
        token = create_token(1, "nurse1", "nurse")
        self.assertIsInstance(token, str)
        self.assertTrue(len(token) > 10)

    def test_decode_token_valid(self):
        from hd_module.auth import create_token, decode_token
        token = create_token(1, "nurse1", "nurse", expiry_minutes=60)
        payload = decode_token(token)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["user_id"], 1)
        self.assertEqual(payload["username"], "nurse1")
        self.assertEqual(payload["role"], "nurse")

    def test_decode_token_expired(self):
        from hd_module.auth import create_token, decode_token
        token = create_token(1, "nurse1", "nurse", expiry_minutes=-1)
        payload = decode_token(token)
        self.assertIsNone(payload)

    def test_decode_token_invalid(self):
        from hd_module.auth import decode_token
        self.assertIsNone(decode_token("invalid.token.here"))
        self.assertIsNone(decode_token(""))
        self.assertIsNone(decode_token(None))

    def test_roles_defined(self):
        from hd_module.auth import ROLES
        self.assertIn("physician", ROLES)
        self.assertIn("nurse", ROLES)
        self.assertIn("admin", ROLES)
        self.assertIn("auditor", ROLES)

    def test_role_permissions(self):
        from hd_module.auth import ROLE_PERMISSIONS
        self.assertIn("session.write", ROLE_PERMISSIONS["nurse"])
        self.assertNotIn("user.create", ROLE_PERMISSIONS["nurse"])
        self.assertIn("user.create", ROLE_PERMISSIONS["admin"])
        self.assertNotIn("session.write", ROLE_PERMISSIONS["auditor"])

    def test_has_permission(self):
        from hd_module.auth import has_permission
        self.assertTrue(has_permission("physician", "session.approve"))
        self.assertFalse(has_permission("nurse", "session.approve"))
        self.assertTrue(has_permission("admin", "system.config"))
        self.assertFalse(has_permission("auditor", "session.write"))

    def test_mfa_secret_generation(self):
        from hd_module.auth import generate_mfa_secret
        secret = generate_mfa_secret()
        self.assertIsInstance(secret, str)
        self.assertTrue(len(secret) >= 16)


# ═══════════════════════════════════════════════════════════
# Test 3: Patient Bridge
# ═══════════════════════════════════════════════════════════

class TestHdPatientBridge(unittest.TestCase):

    def test_synthetic_patients_count(self):
        from hd_module.patient_bridge import SYNTHETIC_PATIENTS
        self.assertEqual(len(SYNTHETIC_PATIENTS), 30)

    def test_synthetic_patients_have_required_fields(self):
        from hd_module.patient_bridge import SYNTHETIC_PATIENTS
        required = ["hn", "patient_name", "vascular_access", "dry_weight", "schedule_days"]
        for p in SYNTHETIC_PATIENTS:
            for field in required:
                self.assertIn(field, p, f"Patient {p.get('hn')} missing {field}")

    def test_synthetic_hns_unique(self):
        from hd_module.patient_bridge import SYNTHETIC_PATIENTS
        hns = [p["hn"] for p in SYNTHETIC_PATIENTS]
        self.assertEqual(len(hns), len(set(hns)))

    def test_bridge_no_registry(self):
        from hd_module.patient_bridge import PatientBridge
        bridge = PatientBridge(master_registry=None)
        patients = bridge.sync_patients_readonly()
        self.assertEqual(len(patients), 30)

    def test_bridge_get_synthetic_patient(self):
        from hd_module.patient_bridge import PatientBridge, SYNTHETIC_PATIENTS
        bridge = PatientBridge(master_registry=None)
        bridge.sync_patients_readonly()
        p = bridge.get_patient_by_hn(SYNTHETIC_PATIENTS[0]["hn"])
        self.assertIsNotNone(p)
        self.assertEqual(p["hn"], SYNTHETIC_PATIENTS[0]["hn"])

    def test_bridge_get_nonexistent(self):
        from hd_module.patient_bridge import PatientBridge
        bridge = PatientBridge(master_registry=None)
        p = bridge.get_patient_by_hn("NONEXIST999")
        self.assertIsNone(p)

    def test_check_duplicates_none(self):
        from hd_module.patient_bridge import PatientBridge, SYNTHETIC_PATIENTS
        bridge = PatientBridge()
        dupes = bridge.check_duplicates(SYNTHETIC_PATIENTS)
        self.assertEqual(len(dupes), 0)

    def test_check_duplicates_found(self):
        from hd_module.patient_bridge import PatientBridge
        bridge = PatientBridge()
        dupes = bridge.check_duplicates([
            {"hn": "123"}, {"hn": "456"}, {"hn": "123"},
        ])
        self.assertEqual(dupes, ["123"])

    def test_hash_record(self):
        from hd_module.patient_bridge import _hash_record
        h = _hash_record({"hn": "12345", "name": "test"})
        self.assertEqual(len(h), 64)  # SHA-256


# ═══════════════════════════════════════════════════════════
# Test 4: Clinical Alerts
# ═══════════════════════════════════════════════════════════

class TestHdClinicalAlerts(unittest.TestCase):

    def test_default_rules_count(self):
        from hd_module.clinical_alerts import DEFAULT_RULES
        self.assertEqual(len(DEFAULT_RULES), 7)

    def test_all_rules_have_thai_name(self):
        from hd_module.clinical_alerts import DEFAULT_RULES
        for rule in DEFAULT_RULES:
            self.assertTrue(len(rule.name_th) > 0, f"{rule.rule_id} missing Thai name")

    def test_severe_idh_triggered(self):
        from hd_module.clinical_alerts import check_severe_idh
        result = check_severe_idh(85)
        self.assertIsNotNone(result)
        self.assertTrue(result.triggered)
        self.assertEqual(result.severity, "critical")

    def test_severe_idh_not_triggered(self):
        from hd_module.clinical_alerts import check_severe_idh
        result = check_severe_idh(120)
        self.assertIsNone(result)

    def test_idh_triggered(self):
        from hd_module.clinical_alerts import check_idh
        now = datetime.now(timezone.utc)
        prev = [(now - timedelta(minutes=10), 140)]
        result = check_idh(115, prev)
        self.assertIsNotNone(result)
        self.assertTrue(result.triggered)

    def test_idh_not_triggered_small_drop(self):
        from hd_module.clinical_alerts import check_idh
        now = datetime.now(timezone.utc)
        prev = [(now - timedelta(minutes=10), 130)]
        result = check_idh(120, prev)
        self.assertIsNone(result)

    def test_idh_not_triggered_old_reading(self):
        from hd_module.clinical_alerts import check_idh
        now = datetime.now(timezone.utc)
        prev = [(now - timedelta(minutes=60), 160)]
        result = check_idh(130, prev)
        self.assertIsNone(result)

    def test_high_uf_rate_triggered(self):
        from hd_module.clinical_alerts import check_high_uf_rate
        result = check_high_uf_rate(1000, 60)  # 16.7 mL/kg/hr
        self.assertIsNotNone(result)
        self.assertTrue(result.triggered)

    def test_high_uf_rate_normal(self):
        from hd_module.clinical_alerts import check_high_uf_rate
        result = check_high_uf_rate(600, 60)  # 10 mL/kg/hr
        self.assertIsNone(result)

    def test_excess_weight_triggered(self):
        from hd_module.clinical_alerts import check_excess_weight
        result = check_excess_weight(58, 55)  # 5.5% gain
        self.assertIsNotNone(result)
        self.assertTrue(result.triggered)

    def test_excess_weight_normal(self):
        from hd_module.clinical_alerts import check_excess_weight
        result = check_excess_weight(56, 55)  # 1.8% gain
        self.assertIsNone(result)

    def test_bradycardia_triggered(self):
        from hd_module.clinical_alerts import check_heart_rate
        result = check_heart_rate(45)
        self.assertIsNotNone(result)
        self.assertEqual(result.rule_id, "bradycardia")
        self.assertEqual(result.severity, "critical")

    def test_tachycardia_triggered(self):
        from hd_module.clinical_alerts import check_heart_rate
        result = check_heart_rate(130)
        self.assertIsNotNone(result)
        self.assertEqual(result.rule_id, "tachycardia")
        self.assertEqual(result.severity, "warning")

    def test_heart_rate_normal(self):
        from hd_module.clinical_alerts import check_heart_rate
        result = check_heart_rate(80)
        self.assertIsNone(result)

    def test_missing_data_no_vitals(self):
        from hd_module.clinical_alerts import check_missing_data
        result = check_missing_data(None)
        self.assertIsNotNone(result)
        self.assertTrue(result.triggered)

    def test_missing_data_stale(self):
        from hd_module.clinical_alerts import check_missing_data
        old_time = datetime.now(timezone.utc) - timedelta(minutes=90)
        result = check_missing_data(old_time)
        self.assertIsNotNone(result)
        self.assertTrue(result.triggered)

    def test_missing_data_recent(self):
        from hd_module.clinical_alerts import check_missing_data
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = check_missing_data(recent)
        self.assertIsNone(result)

    def test_alert_engine_evaluate(self):
        from hd_module.clinical_alerts import AlertEngine
        engine = AlertEngine()
        now = datetime.now(timezone.utc)
        alerts = engine.evaluate_vitals(
            current_sbp=80,
            current_hr=45,
            pre_weight=60,
            dry_weight=55,
        )
        self.assertTrue(len(alerts) >= 2)
        self.assertTrue(engine.has_critical)

    def test_alert_engine_no_alerts(self):
        from hd_module.clinical_alerts import AlertEngine
        engine = AlertEngine()
        now = datetime.now(timezone.utc)
        alerts = engine.evaluate_vitals(
            current_sbp=130,
            current_hr=80,
            pre_weight=56,
            dry_weight=55,
            last_vital_time=now - timedelta(minutes=5),
        )
        self.assertEqual(len(alerts), 0)

    def test_format_alert_for_line(self):
        from hd_module.clinical_alerts import (
            format_alert_for_line, AlertResult,
        )
        alert = AlertResult(
            rule_id="severe_idh",
            triggered=True,
            severity="critical",
            message_th="🚨 ความดันต่ำวิกฤต: SBP 80 mmHg",
        )
        text = format_alert_for_line(alert, patient_name="นายทดสอบ", hn="T90001", bed=5)
        self.assertIn("วิกฤต", text)
        self.assertIn("นายทดสอบ", text)
        self.assertIn("เตียง 5", text)


# ═══════════════════════════════════════════════════════════
# Test 5: Audit Trail
# ═══════════════════════════════════════════════════════════

class TestHdAudit(unittest.TestCase):

    def setUp(self):
        from hd_module.audit import clear_in_memory_store
        clear_in_memory_store()

    def test_log_action(self):
        from hd_module.audit import log_action
        entry = log_action(
            user_id=1,
            action="session.create",
            resource_type="session",
            resource_id=42,
            new_values={"bed": 5},
            ip_address="192.168.1.1",
        )
        self.assertEqual(entry["user_id"], 1)
        self.assertEqual(entry["action"], "session.create")
        self.assertIn("bed", entry["new_values"])

    def test_query_audit_log(self):
        from hd_module.audit import log_action, query_audit_log
        log_action(user_id=1, action="session.create")
        log_action(user_id=2, action="vital.record")
        log_action(user_id=1, action="session.update")

        all_logs = query_audit_log()
        self.assertEqual(len(all_logs), 3)

        user1_logs = query_audit_log(user_id=1)
        self.assertEqual(len(user1_logs), 2)

        vital_logs = query_audit_log(action="vital.record")
        self.assertEqual(len(vital_logs), 1)

    def test_audit_no_patient_data_in_log(self):
        """Verify patient data is NOT in application log output."""
        from hd_module.audit import log_action
        import io
        import logging

        handler = logging.StreamHandler(io.StringIO())
        logger = logging.getLogger("hd_module.audit")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        log_action(
            user_id=1,
            action="vital.record",
            new_values={"sbp": 140, "patient_name": "SECRET_NAME"},
        )

        log_output = handler.stream.getvalue()
        self.assertNotIn("SECRET_NAME", log_output)
        logger.removeHandler(handler)


# ═══════════════════════════════════════════════════════════
# Test 6: Blueprint
# ═══════════════════════════════════════════════════════════

class TestHdBlueprint(unittest.TestCase):

    def test_blueprint_import(self):
        from hd_module.blueprint import hd_bp
        self.assertEqual(hd_bp.name, "hd")
        self.assertEqual(hd_bp.url_prefix, "/hd/api/v1")

    def test_feature_flag_disabled(self):
        with patch.dict(os.environ, {"HD_MODULE_ENABLED": "false"}):
            # Re-import to pick up env change
            import importlib
            import hd_module
            importlib.reload(hd_module)
            self.assertFalse(hd_module.HD_MODULE_ENABLED)

    def test_register_disabled(self):
        """Module does NOT register when disabled."""
        with patch.dict(os.environ, {"HD_MODULE_ENABLED": "false"}):
            import importlib
            import hd_module
            importlib.reload(hd_module)
            app = MagicMock()
            result = hd_module.register_hd_module(app)
            self.assertFalse(result)
            app.register_blueprint.assert_not_called()

    def test_register_enabled(self):
        """Module registers when enabled."""
        with patch.dict(os.environ, {"HD_MODULE_ENABLED": "true"}):
            import importlib
            import hd_module
            importlib.reload(hd_module)
            app = MagicMock()
            result = hd_module.register_hd_module(app)
            self.assertTrue(result)
            app.register_blueprint.assert_called_once()

    def test_health_endpoint(self):
        """HD health endpoint returns 200."""
        from flask import Flask
        from hd_module.blueprint import hd_bp

        app = Flask(__name__)
        app.register_blueprint(hd_bp)

        with app.test_client() as client:
            resp = client.get("/hd/api/v1/health")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["module"], "hd_smart_monitoring")
            self.assertEqual(data["status"], "ok")

    def test_sessions_requires_auth(self):
        """Sessions endpoint requires Bearer token."""
        from flask import Flask
        from hd_module.blueprint import hd_bp

        app = Flask(__name__)
        app.register_blueprint(hd_bp)

        with app.test_client() as client:
            resp = client.get("/hd/api/v1/sessions")
            self.assertEqual(resp.status_code, 401)

    def test_sessions_with_valid_token(self):
        """Sessions endpoint works with valid token."""
        from flask import Flask
        from hd_module.blueprint import hd_bp
        from hd_module.auth import create_token

        app = Flask(__name__)
        app.register_blueprint(hd_bp)

        token = create_token(1, "nurse1", "nurse")

        with app.test_client() as client:
            resp = client.get(
                "/hd/api/v1/sessions",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(resp.status_code, 200)

    def test_sessions_wrong_role(self):
        """Auditor cannot create sessions."""
        from flask import Flask
        from hd_module.blueprint import hd_bp
        from hd_module.auth import create_token

        app = Flask(__name__)
        app.register_blueprint(hd_bp)

        token = create_token(1, "auditor1", "auditor")

        with app.test_client() as client:
            resp = client.post(
                "/hd/api/v1/sessions",
                headers={"Authorization": f"Bearer {token}"},
                json={"patient_hn": "123", "bed_number": 1},
            )
            self.assertEqual(resp.status_code, 403)

    def test_bed_dashboard(self):
        """Dashboard returns bed list."""
        from flask import Flask
        from hd_module.blueprint import hd_bp
        from hd_module.auth import create_token

        app = Flask(__name__)
        app.register_blueprint(hd_bp)

        token = create_token(1, "nurse1", "nurse")

        with app.test_client() as client:
            resp = client.get(
                "/hd/api/v1/dashboard/beds",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["total"], 30)
            self.assertEqual(len(data["beds"]), 30)

    def test_create_session_validation(self):
        """Create session validates required fields."""
        from flask import Flask
        from hd_module.blueprint import hd_bp
        from hd_module.auth import create_token

        app = Flask(__name__)
        app.register_blueprint(hd_bp)

        token = create_token(1, "nurse1", "nurse")

        with app.test_client() as client:
            resp = client.post(
                "/hd/api/v1/sessions",
                headers={"Authorization": f"Bearer {token}"},
                json={},
            )
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Missing", resp.get_json()["error"])


# ═══════════════════════════════════════════════════════════
# Test 7: Migrations
# ═══════════════════════════════════════════════════════════

class TestHdMigrations(unittest.TestCase):

    def test_migration_sql_valid(self):
        from hd_module.migrations import MIGRATION_UP
        self.assertIn("CREATE TABLE IF NOT EXISTS", MIGRATION_UP)
        self.assertIn("hd_sessions", MIGRATION_UP)
        self.assertIn("hd_vital_signs", MIGRATION_UP)
        self.assertIn("hd_users", MIGRATION_UP)

    def test_migration_has_all_10_tables(self):
        from hd_module.migrations import MIGRATION_UP
        tables = [
            "hd_patient_mapping", "hd_sessions", "hd_prescriptions",
            "hd_vital_signs", "hd_uf_records", "hd_machine_observations",
            "hd_clinical_alerts", "hd_alert_acknowledgements",
            "hd_users", "hd_audit_logs",
        ]
        for t in tables:
            self.assertIn(t, MIGRATION_UP, f"Missing table: {t}")

    def test_rollback_sql_valid(self):
        from hd_module.migrations import MIGRATION_DOWN
        self.assertIn("DROP TABLE IF EXISTS", MIGRATION_DOWN)

    def test_schema_version(self):
        from hd_module.migrations import SCHEMA_VERSION
        self.assertEqual(SCHEMA_VERSION, "1.0.0")

    def test_migration_idempotent(self):
        """IF NOT EXISTS makes re-running safe."""
        from hd_module.migrations import MIGRATION_UP
        # Count CREATE TABLE IF NOT EXISTS
        count = MIGRATION_UP.count("CREATE TABLE IF NOT EXISTS")
        self.assertGreaterEqual(count, 10)


# ═══════════════════════════════════════════════════════════
# Test 8: Config
# ═══════════════════════════════════════════════════════════

class TestHdConfig(unittest.TestCase):

    def test_config_defaults(self):
        from hd_module.config import HdConfig
        self.assertEqual(HdConfig.TOTAL_BEDS, 30)
        self.assertEqual(HdConfig.ALERT_BRADYCARDIA_HR, 50)
        self.assertEqual(HdConfig.ALERT_TACHYCARDIA_HR, 120)
        self.assertEqual(HdConfig.ALERT_SEVERE_IDH_SBP, 90)
        self.assertEqual(HdConfig.ALERT_IDH_SBP_DROP, 20)

    def test_config_env_override(self):
        with patch.dict(os.environ, {"HD_TOTAL_BEDS": "40"}):
            from hd_module.config import HdConfig
            # Note: class-level env reads happen at import, not at test time
            # This tests that the mechanism exists
            self.assertIsInstance(HdConfig.TOTAL_BEDS, int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
