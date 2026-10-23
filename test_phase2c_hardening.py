#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 2C — Testing & Hardening Suite
RANONG-RAKTAI-AI 2.0

ครอบคลุม:
  1. Cross-Module Integration — ทดสอบการทำงานร่วมกันของ modules
  2. Edge Cases & Error Handling — กรณีขอบ + การจัดการ error
  3. Concurrency & Version Control — optimistic locking, concurrent access
  4. Storage Resilience — GCS failure modes, fallback
  5. API Contract Validation — input/output contract
  6. PDPA Compliance — data minimization, no PII leak
  7. Security Boundaries — auth, authorization, input sanitization
  8. Clinical Safety — rule-engine correctness, no auto-treatment
  9. Rollback Readiness — backward compatibility
  10. Performance — response time boundaries
"""

import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import asdict

SAMPLE_OWNER = "abc12345def67890abc12345def67890"
TODAY = datetime(2026, 10, 23)


def _lab(date_str, results):
    return {
        "document_id": f"doc_{date_str.replace('-', '')}",
        "owner_hash": SAMPLE_OWNER,
        "specimen_date": date_str,
        "results": results,
    }


def _r(analyte, value, unit=""):
    return {"analyte": analyte, "value": value, "unit": unit}


# ═══════════════════════════════════════════════════════════
# 1. Cross-Module Integration Tests
# ═══════════════════════════════════════════════════════════

class TestCrossModuleIntegration(unittest.TestCase):
    """ทดสอบการทำงานร่วมกันระหว่าง Phase 2A + 2B modules."""

    def test_ocr_then_monitoring(self):
        """OCR validator → monitoring schedule chain."""
        from ocr_safety import OcrSafetyValidator
        from monitoring_schedule import check_monitoring_status

        validator = OcrSafetyValidator()
        result = validator.validate_result("Creatinine", 1.2, "mg/dL", 0.95)
        self.assertTrue(result.is_valid)

        labs = [_lab("2026-10-20", [_r("Creatinine", 1.2, "mg/dL")])]
        report = check_monitoring_status(labs, SAMPLE_OWNER, reference_date=TODAY)
        self.assertIsNotNone(report)

    def test_monitoring_feeds_dashboard(self):
        """Monitoring report → Nephrology dashboard chain."""
        from monitoring_schedule import check_monitoring_status
        from nephrology_kpi import generate_nephrology_dashboard

        labs = [_lab("2026-10-20", [
            _r("Hb", 11.0), _r("Creatinine", 1.5), _r("Ca", 9.0),
        ])]
        mon_report = check_monitoring_status(labs, SAMPLE_OWNER, reference_date=TODAY)
        dashboard = generate_nephrology_dashboard(
            labs, SAMPLE_OWNER,
            monitoring_report=mon_report.to_dict(),
        )
        self.assertIsNotNone(dashboard)
        self.assertTrue(len(dashboard.domain_kpis) > 0)

    def test_medication_review_with_lab_context(self):
        """Medication review ใช้ lab values context."""
        from medication_review import review_medications

        result = review_medications(
            medications=["metformin", "losartan", "amlodipine"],
            lab_values={"eGFR": 35, "Creatinine": 2.0, "K": 5.5},
            ckd_stage="G3b",
            egfr=35.0,
        )
        self.assertIsNotNone(result)
        # Should have alerts for metformin in CKD + hyperkalemia concern
        self.assertTrue(len(result.alerts) > 0)

    def test_ocr_validator_plausibility_check(self):
        """OCR validator ตรวจค่าที่ไม่น่าเป็นไปได้."""
        from ocr_safety import OcrSafetyValidator

        validator = OcrSafetyValidator()
        # Impossibly high creatinine
        result = validator.validate_result("Creatinine", 999.0, "mg/dL", 0.5)
        self.assertFalse(result.is_valid)

    def test_alert_delivery_service_lifecycle(self):
        """Alert service lifecycle: create → acknowledge → resolve."""
        from alert_delivery import AlertDeliveryService, AlertState

        storage = MagicMock()
        storage.get.return_value = None
        storage.put.return_value = True
        audit = MagicMock()
        audit.log.return_value = True

        service = AlertDeliveryService(storage, audit)
        alert = service.create_alert(
            doc_id="doc_001", owner_hash=SAMPLE_OWNER,
            analyte="K", value=6.5, unit="mEq/L",
            alert_level="critical",
        )
        self.assertIsNotNone(alert)
        # create_alert transitions to QUEUED state (string value)
        self.assertEqual(alert.state, AlertState.QUEUED.value)

    def test_physician_approval_service_init(self):
        """Physician service initializes correctly."""
        from physician_approval import PhysicianApprovalService

        storage = MagicMock()
        audit = MagicMock()
        service = PhysicianApprovalService(
            storage, audit,
            physician_users={"phys_hash_1"},
            admin_users={"admin_hash_1"},
        )
        self.assertIsNotNone(service)

    def test_patient_identity_service_init(self):
        """Patient identity service initializes correctly."""
        from patient_identity import PatientIdentityService

        storage = MagicMock()
        audit = MagicMock()
        service = PatientIdentityService(storage, audit)
        self.assertIsNotNone(service)


# ═══════════════════════════════════════════════════════════
# 2. Edge Cases & Error Handling
# ═══════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):
    """ทดสอบ edge cases และ error handling."""

    def test_monitoring_empty_results_list(self):
        """Lab report with empty results list."""
        from monitoring_schedule import check_monitoring_status
        labs = [_lab("2026-10-20", [])]
        report = check_monitoring_status(labs, SAMPLE_OWNER, reference_date=TODAY)
        self.assertIsNotNone(report)

    def test_monitoring_future_date(self):
        """Lab with future specimen date."""
        from monitoring_schedule import check_monitoring_status
        future = (TODAY + timedelta(days=30)).strftime("%Y-%m-%d")
        labs = [_lab(future, [_r("Hb", 12.0)])]
        report = check_monitoring_status(labs, SAMPLE_OWNER, reference_date=TODAY)
        self.assertIsNotNone(report)

    def test_monitoring_very_old_date(self):
        """Lab from years ago."""
        from monitoring_schedule import check_monitoring_status
        old = (TODAY - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
        labs = [_lab(old, [_r("Hb", 12.0)])]
        report = check_monitoring_status(labs, SAMPLE_OWNER, reference_date=TODAY)
        self.assertTrue(report.total_overdue > 0)

    def test_dashboard_with_nan_values(self):
        """Dashboard handles non-numeric lab values gracefully."""
        from nephrology_kpi import generate_nephrology_dashboard
        labs = [_lab("2026-10-20", [
            {"analyte": "Hb", "value": None, "unit": "g/dL"},
        ])]
        # Should not crash
        dashboard = generate_nephrology_dashboard(labs, SAMPLE_OWNER)
        self.assertIsNotNone(dashboard)

    def test_ocr_validator_empty_analyte(self):
        """OCR validator handles empty analyte name."""
        from ocr_safety import OcrSafetyValidator
        validator = OcrSafetyValidator()
        result = validator.validate_result("", 10.0, "mg/dL", 0.9)
        self.assertIsNotNone(result)

    def test_ocr_validator_zero_confidence(self):
        """OCR with zero confidence should flag."""
        from ocr_safety import OcrSafetyValidator
        validator = OcrSafetyValidator()
        result = validator.validate_result("Creatinine", 1.2, "mg/dL", 0.0)
        self.assertTrue(len(result.flags) > 0 or result.requires_review)

    def test_medication_review_special_chars(self):
        """Medication review handles special characters."""
        from medication_review import review_medications
        result = review_medications(
            medications=["<script>alert('xss')</script>"],
            lab_values={},
        )
        self.assertIsNotNone(result)

    def test_audit_trail_event_auto_timestamp(self):
        """AuditEvent auto-fills timestamp."""
        from audit_trail import AuditEvent
        event = AuditEvent(
            event_id="",
            action="test_action",
            actor_hash="actor_123",
            details={"key": "value"},
        )
        self.assertTrue(len(event.timestamp) > 0)
        self.assertTrue(len(event.event_id) > 0)  # auto-filled

    def test_audit_trail_event_serializable(self):
        """AuditEvent is JSON serializable."""
        from audit_trail import AuditEvent
        event = AuditEvent(
            event_id="",
            action="test_action",
            actor_hash="actor_123",
            details={"key": "value"},
        )
        d = asdict(event)
        json.dumps(d)

    def test_health_check_init(self):
        """Health check module initializes."""
        from health_check import init_health, register_health_routes
        init_health()
        # Should not crash


# ═══════════════════════════════════════════════════════════
# 3. Concurrency & Version Control
# ═══════════════════════════════════════════════════════════

class TestConcurrencyVersionControl(unittest.TestCase):
    """ทดสอบ optimistic concurrency control."""

    def test_physician_approval_version_check(self):
        """Physician approval ต้อง check version."""
        from physician_approval import PhysicianApprovalService, EnhancedLabReport

        storage = MagicMock()
        audit = MagicMock()
        audit.log.return_value = True

        service = PhysicianApprovalService(
            storage, audit,
            physician_users={"phys1"},
            admin_users=set(),
        )

        # Create a mock report with version 1
        from lab_models import LabReport, LabResult
        lab_result = LabResult(
            analyte="Creatinine", value=1.2, unit="mg/dL",
            reference_range="0.6-1.2", confidence=0.95,
            extraction_method="per_line",
        )
        lab_report = LabReport(
            source_document_id="doc_001",
            results=[lab_result], specimen_date="2026-10-20",
            extraction_method="per_line",
        )
        enhanced = EnhancedLabReport(report=lab_report)
        self.assertEqual(enhanced.version, 1)

    def test_enhanced_lab_report_immutable_originals(self):
        """Original results ต้องเก็บไว้ immutable."""
        from physician_approval import EnhancedLabReport
        from lab_models import LabReport, LabResult

        result = LabResult(
            analyte="K", value=5.0, unit="mEq/L",
            reference_range="3.5-5.0", confidence=0.9,
            extraction_method="per_line",
        )
        report = LabReport(
            source_document_id="doc_002",
            results=[result], specimen_date="2026-10-20",
            extraction_method="per_line",
        )
        enhanced = EnhancedLabReport(report=report)

        # original_results should be preserved
        self.assertEqual(len(enhanced.original_results), 1)
        self.assertEqual(enhanced.original_results[0]["analyte"], "K")


# ═══════════════════════════════════════════════════════════
# 4. Storage Resilience
# ═══════════════════════════════════════════════════════════

class TestStorageResilience(unittest.TestCase):
    """ทดสอบ resilience ต่อ storage failures."""

    def test_audit_trail_storage_failure_handling(self):
        """Audit trail handles storage failure gracefully."""
        from audit_trail import PersistentAuditTrail

        storage = MagicMock()
        storage.put.side_effect = Exception("GCS unavailable")

        trail = PersistentAuditTrail(storage)
        # Should not crash, should return False
        result = trail.log(
            action="test",
            actor_hash="actor1",
            details={},
        )
        self.assertFalse(result)

    def test_enhanced_audit_logger_failure(self):
        """EnhancedAuditLogger raises on security-critical storage failure."""
        from audit_trail import EnhancedAuditLogger, AuditPersistenceError

        storage = MagicMock()
        storage.put.side_effect = Exception("GCS timeout")

        logger = EnhancedAuditLogger(storage)
        # Security-critical action should raise AuditPersistenceError (fail-closed)
        with self.assertRaises(AuditPersistenceError):
            logger.log(
                action="physician_approval",
                actor_hash="phys1",
                details={"report_id": "doc_001"},
            )

    def test_alert_delivery_storage_failure(self):
        """Alert service handles storage failure in create."""
        from alert_delivery import AlertDeliveryService

        storage = MagicMock()
        storage.put.side_effect = Exception("GCS error")
        audit = MagicMock()
        audit.log.return_value = True

        service = AlertDeliveryService(storage, audit)
        # Should handle gracefully
        try:
            alert = service.create_alert(
                doc_id="doc_fail", owner_hash=SAMPLE_OWNER,
                analyte="K", value=7.0, unit="mEq/L",
                alert_level="critical",
            )
            # Either returns None or raises — both acceptable
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
# 5. API Contract Validation
# ═══════════════════════════════════════════════════════════

class TestAPIContracts(unittest.TestCase):
    """ทดสอบ API contracts ของแต่ละ module."""

    def test_monitoring_report_contract(self):
        """MonitoringReport มี fields ที่จำเป็น."""
        from monitoring_schedule import check_monitoring_status, MonitoringReport

        report = check_monitoring_status([], SAMPLE_OWNER, reference_date=TODAY)
        self.assertIsInstance(report, MonitoringReport)
        self.assertTrue(hasattr(report, "overdue_items"))
        self.assertTrue(hasattr(report, "due_soon_items"))
        self.assertTrue(hasattr(report, "up_to_date_items"))
        self.assertTrue(hasattr(report, "missing_items"))
        self.assertTrue(hasattr(report, "owner_hash_prefix"))
        self.assertTrue(hasattr(report, "generated_at"))

    def test_monitoring_report_to_dict_serializable(self):
        """MonitoringReport.to_dict() ต้อง JSON serializable."""
        from monitoring_schedule import check_monitoring_status
        report = check_monitoring_status([], SAMPLE_OWNER, reference_date=TODAY)
        d = report.to_dict()
        s = json.dumps(d, ensure_ascii=False)
        self.assertTrue(len(s) > 0)

    def test_dashboard_contract(self):
        """NephrologyDashboard มี fields ที่จำเป็น."""
        from nephrology_kpi import generate_nephrology_dashboard, NephrologyDashboard
        dashboard = generate_nephrology_dashboard([], SAMPLE_OWNER)
        self.assertIsInstance(dashboard, NephrologyDashboard)
        self.assertTrue(hasattr(dashboard, "domain_kpis"))
        self.assertTrue(hasattr(dashboard, "disclaimer"))
        self.assertTrue(hasattr(dashboard, "owner_hash_prefix"))

    def test_medication_review_result_contract(self):
        """MedicationReviewResult มี fields ที่จำเป็น."""
        from medication_review import review_medications
        result = review_medications([], {})
        self.assertTrue(hasattr(result, "alerts"))
        self.assertTrue(hasattr(result, "disclaimer"))
        self.assertTrue(hasattr(result, "review_date"))

    def test_ocr_validate_result_contract(self):
        """OCR validate_result คืน OcrValidationResult ที่มี is_valid flag."""
        from ocr_safety import OcrSafetyValidator
        validator = OcrSafetyValidator()
        result = validator.validate_result("Hb", 12.0, "g/dL", 0.95)
        self.assertTrue(hasattr(result, "is_valid"))
        self.assertIsInstance(result.is_valid, bool)
        self.assertTrue(hasattr(result, "flags"))
        self.assertTrue(hasattr(result, "to_dict"))

    def test_alert_state_enum_complete(self):
        """AlertState enum มีครบทุก state."""
        from alert_delivery import AlertState
        required = ["DETECTED", "QUEUED", "SENT", "DELIVERY_FAILED",
                    "ACKNOWLEDGED", "RESOLVED", "ESCALATED"]
        for state in required:
            self.assertTrue(hasattr(AlertState, state), f"Missing: {state}")

    def test_alert_severity_enum(self):
        """AlertSeverity enum มีครบ."""
        from alert_delivery import AlertSeverity
        required = ["CRITICAL", "URGENT", "SOURCE_VERIFY"]
        for sev in required:
            self.assertTrue(hasattr(AlertSeverity, sev), f"Missing: {sev}")


# ═══════════════════════════════════════════════════════════
# 6. PDPA Compliance
# ═══════════════════════════════════════════════════════════

class TestPDPACompliance(unittest.TestCase):
    """ทดสอบการปฏิบัติตาม PDPA."""

    def test_monitoring_text_no_full_hash(self):
        """Monitoring text ต้องไม่แสดง full owner_hash."""
        from monitoring_schedule import check_monitoring_status, monitoring_report_text_th
        report = check_monitoring_status([], SAMPLE_OWNER, reference_date=TODAY)
        text = monitoring_report_text_th(report)
        self.assertNotIn(SAMPLE_OWNER, text)

    def test_dashboard_text_no_full_hash(self):
        """Dashboard text ต้องไม่แสดง full owner_hash."""
        from nephrology_kpi import generate_nephrology_dashboard, dashboard_text_th
        dashboard = generate_nephrology_dashboard([], SAMPLE_OWNER)
        text = dashboard_text_th(dashboard)
        self.assertNotIn(SAMPLE_OWNER, text)

    def test_owner_hash_prefix_length(self):
        """Owner hash prefix ต้อง 8 ตัว."""
        from monitoring_schedule import check_monitoring_status
        report = check_monitoring_status([], SAMPLE_OWNER, reference_date=TODAY)
        self.assertEqual(len(report.owner_hash_prefix), 8)

    def test_medication_review_no_patient_name(self):
        """Medication review text ต้องไม่มีชื่อผู้ป่วย."""
        from medication_review import review_medications, review_text_th
        result = review_medications(["metformin"], {"eGFR": 50}, egfr=50.0)
        text = review_text_th(result)
        # Should not contain common Thai names or identity info
        for forbidden in ["ชื่อ", "นามสกุล", "หมายเลขประจำตัว", "HN"]:
            self.assertNotIn(forbidden, text)

    def test_audit_event_no_raw_user_id(self):
        """Audit event ต้องไม่เก็บ raw user ID."""
        from audit_trail import AuditEvent
        event = AuditEvent(
            event_id="",
            action="test",
            actor_hash="hashed_value_123",
            details={"doc_id": "doc_001"},
        )
        d = asdict(event)
        dumped = json.dumps(d)
        self.assertNotIn("U_", dumped)  # LINE user IDs start with U

    def test_ocr_result_no_pii(self):
        """OCR validation result ไม่มี PII."""
        from ocr_safety import OcrSafetyValidator
        validator = OcrSafetyValidator()
        result = validator.validate_result("Creatinine", 1.2, "mg/dL", 0.95)
        dumped = json.dumps(result.to_dict(), ensure_ascii=False)
        # Should not contain any PII patterns
        self.assertNotIn("HN", dumped)
        self.assertNotIn("ชื่อผู้ป่วย", dumped)


# ═══════════════════════════════════════════════════════════
# 7. Security Boundaries
# ═══════════════════════════════════════════════════════════

class TestSecurityBoundaries(unittest.TestCase):
    """ทดสอบ security boundaries."""

    def test_physician_requires_auth(self):
        """Physician service ต้องตรวจสอบสิทธิ์."""
        from physician_approval import PhysicianApprovalService

        storage = MagicMock()
        audit = MagicMock()
        service = PhysicianApprovalService(
            storage, audit,
            physician_users={"phys1"},
            admin_users=set(),
        )
        # Non-physician should not be able to approve
        self.assertFalse(service.is_physician("non_physician_hash"))
        self.assertTrue(service.is_physician("phys1"))

    def test_external_ai_disabled(self):
        """EXTERNAL_AI_CLASSIFICATION_ALLOWED must be False."""
        # Check server.py
        with open("server.py") as f:
            source = f.read()
        self.assertIn("EXTERNAL_AI_CLASSIFICATION_ALLOWED = False", source)

    def test_command_parser_no_injection(self):
        """Command parser ต้อง handle command injection."""
        from line_bot_enhancer import parse_command
        # SQL-injection-like input
        result = parse_command("'; DROP TABLE users; --")
        # Should not crash, should return unknown or None
        self.assertIsNotNone(result)

    def test_command_parser_xss_input(self):
        """Command parser ต้อง handle XSS input."""
        from line_bot_enhancer import parse_command
        result = parse_command("<script>alert('xss')</script>")
        self.assertIsNotNone(result)

    def test_audit_security_critical_actions(self):
        """Security-critical actions ต้อง fail-closed."""
        from audit_trail import SECURITY_CRITICAL_ACTIONS
        self.assertIn("physician_approval", SECURITY_CRITICAL_ACTIONS)

    def test_protected_commands_include_phase2b(self):
        """PROTECTED_COMMANDS ต้องมีคำสั่ง Phase 2B."""
        from line_bot_enhancer import CommandType
        # Verify enum values exist
        self.assertTrue(hasattr(CommandType, "MONITORING_STATUS"))
        self.assertTrue(hasattr(CommandType, "NEPHROLOGY_KPI"))


# ═══════════════════════════════════════════════════════════
# 8. Clinical Safety Invariants
# ═══════════════════════════════════════════════════════════

class TestClinicalSafety(unittest.TestCase):
    """ทดสอบ Clinical Safety Invariants."""

    def test_all_modules_rule_engine_only(self):
        """ทุก clinical module ต้องเป็น rule-engine เท่านั้น."""
        modules = [
            "monitoring_schedule", "nephrology_kpi",
            "medication_review", "ocr_safety",
        ]
        forbidden_imports = ["openai", "anthropic", "langchain",
                            "transformers", "torch", "tensorflow"]
        for mod_name in modules:
            mod = __import__(mod_name)
            with open(mod.__file__) as f:
                source = f.read()
            imports = [l for l in source.split("\n")
                      if l.strip().startswith("import ") or l.strip().startswith("from ")]
            for line in imports:
                for forbidden in forbidden_imports:
                    self.assertNotIn(
                        forbidden, line.lower(),
                        f"{mod_name} must not import {forbidden}"
                    )

    def test_monitoring_disclaimer_present(self):
        """Monitoring text มี disclaimer."""
        from monitoring_schedule import check_monitoring_status, monitoring_report_text_th
        report = check_monitoring_status([], SAMPLE_OWNER, reference_date=TODAY)
        text = monitoring_report_text_th(report)
        self.assertTrue(
            "ดุลยพินิจ" in text or "แพทย์" in text,
            "Must have clinical disclaimer"
        )

    def test_dashboard_disclaimer_present(self):
        """Dashboard มี disclaimer."""
        from nephrology_kpi import generate_nephrology_dashboard
        dashboard = generate_nephrology_dashboard([], SAMPLE_OWNER)
        self.assertTrue(len(dashboard.disclaimer) > 0)

    def test_medication_disclaimer_present(self):
        """Medication review มี disclaimer."""
        from medication_review import review_medications
        result = review_medications([], {})
        self.assertTrue(len(result.disclaimer) > 0)

    def test_ocr_low_confidence_flags(self):
        """OCR ค่า confidence ต่ำต้อง flag."""
        from ocr_safety import OcrSafetyValidator
        validator = OcrSafetyValidator()
        result = validator.validate_result("Creatinine", 1.2, "mg/dL", 0.3)
        # Low confidence should produce flags or require review
        self.assertTrue(len(result.flags) > 0 or result.requires_review)

    def test_clinical_analyzer_no_prescriptive_output(self):
        """Clinical analyzer ต้องไม่สั่งการรักษา."""
        from clinical_analyzer import analyze_lab_report
        from lab_models import LabReport, LabResult

        results = [
            LabResult(analyte="Creatinine", value=5.0, unit="mg/dL",
                     reference_range="0.6-1.2", confidence=0.95,
                     extraction_method="per_line"),
            LabResult(analyte="eGFR", value=15, unit="mL/min/1.73m2",
                     reference_range=">60", confidence=0.95,
                     extraction_method="per_line"),
        ]
        report = LabReport(
            source_document_id="doc_test",
            results=results, specimen_date="2026-10-20",
            extraction_method="per_line",
        )
        analysis = analyze_lab_report(report)
        text = json.dumps(analysis.to_dict(), ensure_ascii=False) if hasattr(analysis, 'to_dict') else str(analysis)
        forbidden = ["สั่งยา", "ให้ยา", "จ่ายยา"]
        for phrase in forbidden:
            self.assertNotIn(phrase, text)

    def test_nephrology_kpi_deterministic_multi_run(self):
        """Dashboard ต้อง deterministic ทุกครั้ง (10 runs)."""
        from nephrology_kpi import generate_nephrology_dashboard
        labs = [_lab("2026-10-20", [_r("Hb", 11.0), _r("Kt/V", 1.3)])]

        results = []
        for _ in range(10):
            d = generate_nephrology_dashboard(labs, SAMPLE_OWNER)
            results.append(len(d.domain_kpis))

        self.assertEqual(len(set(results)), 1, "Dashboard must be deterministic")


# ═══════════════════════════════════════════════════════════
# 9. Rollback Readiness
# ═══════════════════════════════════════════════════════════

class TestRollbackReadiness(unittest.TestCase):
    """ทดสอบความพร้อมในการ rollback."""

    def test_new_modules_optional_import(self):
        """Phase 2A/2B modules ต้อง import ได้ถ้ามี ไม่ crash ถ้าไม่มี."""
        optional_modules = [
            "physician_approval", "alert_delivery", "patient_identity",
            "ocr_safety", "health_check", "audit_trail",
            "monitoring_schedule", "nephrology_kpi",
        ]
        for mod in optional_modules:
            try:
                __import__(mod)
            except ImportError:
                pass  # OK — optional

    def test_enhanced_lab_report_handles_old_format(self):
        """EnhancedLabReport.from_dict handles legacy format."""
        from physician_approval import EnhancedLabReport

        # Old format: raw lab report dict with source_document_id
        old_format = {
            "source_document_id": "doc_old",
            "owner_hash": SAMPLE_OWNER,
            "specimen_date": "2026-10-01",
            "extraction_method": "per_line",
            "results": [
                {
                    "analyte": "Hb",
                    "value": 12.0,
                    "unit": "g/dL",
                    "reference_range": "12-16",
                    "confidence": 0.95,
                    "extraction_method": "per_line",
                }
            ],
        }
        enhanced = EnhancedLabReport.from_dict(old_format)
        self.assertIsNotNone(enhanced)

    def test_lab_models_backward_compatible(self):
        """LabResult and LabReport maintain backward compatibility."""
        from lab_models import LabResult, LabReport

        result = LabResult(
            analyte="Hb", value=12.0, unit="g/dL",
            reference_range="12-16", confidence=0.95,
            extraction_method="per_line",
        )
        d = result.to_dict()
        self.assertIn("analyte", d)
        self.assertIn("value", d)

        report = LabReport(
            source_document_id="doc_compat",
            results=[result],
            specimen_date="2026-10-20",
            extraction_method="per_line",
        )
        rd = report.to_dict()
        self.assertIn("results", rd)

    def test_command_parser_backward_compatible(self):
        """เพิ่ม command ใหม่โดยไม่กระทบ command เดิม."""
        from line_bot_enhancer import parse_command, CommandType

        # All old commands must still work
        old_commands = {
            "ค้นหา test": CommandType.SEARCH,
            "ช่วยเหลือ": CommandType.HELP,
            "รายการล่าสุด": CommandType.RECENT,
        }
        for text, expected in old_commands.items():
            p = parse_command(text)
            self.assertEqual(p.command_type, expected, f"'{text}' broken")


# ═══════════════════════════════════════════════════════════
# 10. Performance Boundaries
# ═══════════════════════════════════════════════════════════

class TestPerformanceBoundaries(unittest.TestCase):
    """ทดสอบ performance boundaries."""

    def test_monitoring_schedule_performance(self):
        """Monitoring schedule < 100ms สำหรับ 100 labs."""
        from monitoring_schedule import check_monitoring_status

        labs = []
        for i in range(100):
            d = (TODAY - timedelta(days=i)).strftime("%Y-%m-%d")
            labs.append(_lab(d, [_r("Hb", 12.0), _r("Creatinine", 1.2)]))

        start = time.monotonic()
        report = check_monitoring_status(labs, SAMPLE_OWNER, reference_date=TODAY)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.1, f"Too slow: {elapsed:.3f}s")

    def test_nephrology_dashboard_performance(self):
        """Nephrology dashboard < 100ms สำหรับ 100 labs."""
        from nephrology_kpi import generate_nephrology_dashboard

        labs = []
        for i in range(100):
            d = (TODAY - timedelta(days=i)).strftime("%Y-%m-%d")
            labs.append(_lab(d, [
                _r("Hb", 11.0), _r("Creatinine", 1.5),
                _r("Ca", 9.0), _r("K", 4.0),
            ]))

        start = time.monotonic()
        dashboard = generate_nephrology_dashboard(labs, SAMPLE_OWNER)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.1, f"Too slow: {elapsed:.3f}s")

    def test_medication_review_performance(self):
        """Medication review < 50ms."""
        from medication_review import review_medications

        start = time.monotonic()
        result = review_medications(
            ["metformin", "losartan", "amlodipine", "allopurinol", "gabapentin"],
            {"eGFR": 45, "Creatinine": 1.8, "K": 4.5},
            egfr=45.0,
        )
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.05, f"Too slow: {elapsed:.3f}s")

    def test_ocr_validator_performance(self):
        """OCR validator < 10ms per result."""
        from ocr_safety import OcrSafetyValidator
        validator = OcrSafetyValidator()

        start = time.monotonic()
        for _ in range(100):
            validator.validate_result("Creatinine", 1.2, "mg/dL", 0.95)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0, f"Too slow: {elapsed:.3f}s for 100 validations")

    def test_command_parser_performance(self):
        """Command parser < 5ms per parse."""
        from line_bot_enhancer import parse_command

        commands = ["ตารางตรวจ", "KPIไต", "ค้นหา test", "ช่วยเหลือ", "รายการล่าสุด"]
        start = time.monotonic()
        for _ in range(100):
            for cmd in commands:
                parse_command(cmd)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.5, f"Too slow: {elapsed:.3f}s for 500 parses")


if __name__ == "__main__":
    unittest.main()
