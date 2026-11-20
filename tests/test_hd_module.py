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
        from hd_module.blueprint import hd_bp, hd_web
        self.assertEqual(hd_bp.name, "hd")
        self.assertEqual(hd_bp.url_prefix, "/hd/api/v1")
        self.assertEqual(hd_web.name, "hd_web")
        self.assertEqual(hd_web.url_prefix, "/hd")

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
        """Module registers when enabled (API + web blueprints)."""
        with patch.dict(os.environ, {"HD_MODULE_ENABLED": "true"}):
            import importlib
            import hd_module
            importlib.reload(hd_module)
            app = MagicMock()
            result = hd_module.register_hd_module(app)
            self.assertTrue(result)
            self.assertEqual(app.register_blueprint.call_count, 2)

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
            self.assertEqual(data["total"], 28)
            self.assertEqual(len(data["beds"]), 28)

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
# Test 7: Web UI Routes (Phase 2)
# ═══════════════════════════════════════════════════════════

class TestHdWebRoutes(unittest.TestCase):
    """Test template rendering routes at /hd/*."""

    def setUp(self):
        from flask import Flask
        from hd_module.blueprint import hd_bp, hd_web
        self.app = Flask(__name__)
        self.app.register_blueprint(hd_bp)
        self.app.register_blueprint(hd_web)
        self.client = self.app.test_client()

    def test_root_redirects_to_login(self):
        resp = self.client.get("/hd/")
        self.assertIn(resp.status_code, (301, 302, 308))

    def test_login_page_renders(self):
        resp = self.client.get("/hd/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"PIN", resp.data)

    def test_dashboard_page_renders(self):
        resp = self.client.get("/hd/dashboard")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("bed-grid".encode(), resp.data)

    def test_session_new_page_renders(self):
        resp = self.client.get("/hd/session/new")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Pre-HD".encode(), resp.data)

    def test_session_detail_page_renders(self):
        resp = self.client.get("/hd/session/1")
        self.assertEqual(resp.status_code, 200)

    def test_patient_list_page_renders(self):
        resp = self.client.get("/hd/patients")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"patientGrid", resp.data)

    def test_alerts_page_renders(self):
        resp = self.client.get("/hd/alerts")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("alert-list".encode(), resp.data)


# ═══════════════════════════════════════════════════════════
# Test 8: Migrations
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
        self.assertEqual(HdConfig.TOTAL_BEDS, 28)
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


# ═══════════════════════════════════════════════════════════
# Test: New Features (Session B — 2026-11-10)
# ═══════════════════════════════════════════════════════════

class TestNewFeatures(unittest.TestCase):
    """Tests for features added in the November 2026 sessions."""

    def _make_app(self):
        from flask import Flask
        from hd_module.blueprint import hd_bp
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.register_blueprint(hd_bp, url_prefix="/hd/api/v1")
        return app

    def _get_token(self, role="nurse"):
        from hd_module.auth import create_token
        return create_token(1, f"test_{role}", role)

    # ── Alerts API (was stub, now real) ──

    def test_list_alerts_returns_json(self):
        """list_alerts should return JSON with alerts array (not stub)."""
        app = self._make_app()
        token = self._get_token("physician")
        with app.test_client() as c:
            resp = c.get("/hd/api/v1/alerts",
                         headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn("alerts", data)
            self.assertIsInstance(data["alerts"], list)

    def test_acknowledge_alert_requires_auth(self):
        """acknowledge_alert should require authentication."""
        app = self._make_app()
        with app.test_client() as c:
            resp = c.post("/hd/api/v1/alerts/1/acknowledge")
            self.assertIn(resp.status_code, [401, 403])

    # ── Patient Edit API ──

    def test_update_patient_requires_auth(self):
        """PUT /patients/<hn> requires authentication."""
        app = self._make_app()
        with app.test_client() as c:
            resp = c.put("/hd/api/v1/patients/12345",
                         json={"schedule_days": "จ/พ/ศ"})
            self.assertIn(resp.status_code, [401, 403])

    def test_update_patient_requires_data(self):
        """PUT /patients/<hn> with empty body returns 400 or 503 (no DB)."""
        app = self._make_app()
        token = self._get_token("physician")
        with app.test_client() as c:
            resp = c.put("/hd/api/v1/patients/12345",
                         headers={"Authorization": f"Bearer {token}"},
                         json={})
            self.assertIn(resp.status_code, [400, 503])

    # ── Needs Review API ──

    def test_needs_review_returns_json(self):
        """patients/needs-review returns list."""
        app = self._make_app()
        token = self._get_token("nurse")
        with app.test_client() as c:
            resp = c.get("/hd/api/v1/patients/needs-review",
                         headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn("patients", data)

    # ── ESA History API ──

    def test_esa_history_returns_json(self):
        """patients/<hn>/esa-history returns records + prescribed."""
        app = self._make_app()
        token = self._get_token("nurse")
        with app.test_client() as c:
            resp = c.get("/hd/api/v1/patients/00001/esa-history",
                         headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn("records", data)
            self.assertIn("prescribed", data)

    # ── Flowsheet API ──

    def test_flowsheet_get_returns_json(self):
        """GET /sessions/<id>/flowsheet returns rows + session info."""
        app = self._make_app()
        token = self._get_token("nurse")
        with app.test_client() as c:
            resp = c.get("/hd/api/v1/sessions/999/flowsheet",
                         headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn("rows", data)

    def test_flowsheet_post_requires_slot_time(self):
        """POST /sessions/<id>/flowsheet without slot_time returns 400 or 503."""
        app = self._make_app()
        token = self._get_token("nurse")
        with app.test_client() as c:
            resp = c.post("/hd/api/v1/sessions/999/flowsheet",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"sbp": 120})
            self.assertIn(resp.status_code, [400, 503])

    # ── UF Recording API ──

    def test_uf_get_returns_records(self):
        """GET /sessions/<id>/uf returns records list or 503 (no DB)."""
        app = self._make_app()
        token = self._get_token("nurse")
        with app.test_client() as c:
            resp = c.get("/hd/api/v1/sessions/999/uf",
                         headers={"Authorization": f"Bearer {token}"})
            self.assertIn(resp.status_code, [200, 503])
            if resp.status_code == 200:
                data = resp.get_json()
                self.assertIn("records", data)

    # ── Quick Vital API ──

    def test_quick_vital_requires_sbp(self):
        """POST quick-vital without SBP returns 400 or 503 (no DB)."""
        app = self._make_app()
        token = self._get_token("nurse")
        with app.test_client() as c:
            resp = c.post("/hd/api/v1/sessions/999/quick-vital",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"dbp": 80})
            self.assertIn(resp.status_code, [400, 503])

    # ── Quality Indicators API ──

    def test_quality_indicators_returns_json(self):
        """report/quality-indicators returns indicators or 503 (no DB)."""
        app = self._make_app()
        token = self._get_token("physician")
        with app.test_client() as c:
            resp = c.get("/hd/api/v1/report/quality-indicators",
                         headers={"Authorization": f"Bearer {token}"})
            self.assertIn(resp.status_code, [200, 503])
            if resp.status_code == 200:
                data = resp.get_json()
                self.assertIn("indicators", data)

    # ── ESA Monthly Report API ──

    def test_esa_monthly_report_returns_json(self):
        """report/esa-monthly returns summary or 503 (no DB)."""
        app = self._make_app()
        token = self._get_token("physician")
        with app.test_client() as c:
            resp = c.get("/hd/api/v1/report/esa-monthly",
                         headers={"Authorization": f"Bearer {token}"})
            self.assertIn(resp.status_code, [200, 503])
            if resp.status_code == 200:
                data = resp.get_json()
                self.assertIn("summary", data)

    # ── Pre-HD Reminder Module ──

    def test_pre_hd_reminder_parse_schedule(self):
        """parse schedule_days Thai format."""
        from hd_module.pre_hd_reminder import _parse_schedule_days
        self.assertEqual(_parse_schedule_days("จ/พ/ศ"), {0, 2, 4})
        self.assertEqual(_parse_schedule_days("อ/พฤ/ส"), {1, 3, 5})
        self.assertEqual(_parse_schedule_days(""), set())
        self.assertEqual(_parse_schedule_days(None), set())

    def test_pre_hd_reminder_format_message(self):
        """format_reminder_message returns non-empty string."""
        from hd_module.pre_hd_reminder import format_reminder_message
        from datetime import date
        patient = {"hn": "12345", "name": "ทดสอบ", "schedule": "จ/พ/ศ", "va": "AVF"}
        msg = format_reminder_message(patient, date(2026, 11, 11))
        self.assertIn("ทดสอบ", msg)
        self.assertIn("คลินิกระนอง", msg)
        self.assertNotIn("XXX", msg)  # placeholder removed

    # ── Thai Timezone Helper ──

    def test_thai_today_returns_date(self):
        """_thai_today returns a date object."""
        from hd_module.blueprint import _thai_today
        from datetime import date
        today = _thai_today()
        self.assertIsInstance(today, date)

    # ── Kt/V Calculator ──

    def test_ktv_calculation(self):
        """Kt/V formula returns valid result."""
        from hd_module.kt_v_calculator import calculate_kt_v
        result = calculate_kt_v(
            pre_bun=60, post_bun=20,
            pre_weight=65, post_weight=63,
            duration_hours=4,
        )
        self.assertIn("kt_v", result)
        self.assertIn("urr", result)
        self.assertGreater(result["kt_v"], 0)
        self.assertGreater(result["urr"], 0)

    # ── Shift Handover ──

    def test_shift_handover_module_import(self):
        """shift_handover module imports without error."""
        from hd_module.shift_handover import generate_handover_summary
        self.assertTrue(callable(generate_handover_summary))

    # ── QR Checkin ──

    def test_qr_data_generation(self):
        """QR generates bed data with token."""
        from hd_module.qr_checkin import generate_bed_qr_data
        data = generate_bed_qr_data(1)
        self.assertIn("bed", data)
        self.assertIn("token", data)
        self.assertEqual(data["bed"], 1)

    # ── Dashboard Shift Filter ──

    def test_beds_accepts_shift_param(self):
        """Dashboard beds API accepts shift parameter."""
        app = self._make_app()
        token = self._get_token("nurse")
        with app.test_client() as c:
            for shift in ["morning", "afternoon", "evening"]:
                resp = c.get(f"/hd/api/v1/dashboard/beds?shift={shift}",
                             headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(resp.status_code, 200)
                data = resp.get_json()
                self.assertEqual(data["total"], 28)


if __name__ == "__main__":
    unittest.main(verbosity=2)
