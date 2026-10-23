#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 2A — Comprehensive Safety Tests
RANONG-RAKTAI-AI 2.0

ทดสอบ Tasks 1-7:
  1. Physician Approval Workflow
  2. Critical Lab Alert Delivery
  3. Patient Identity Verification
  4. OCR Clinical Safety
  5. Container Security (Dockerfile validation)
  6. Health Monitoring
  7. Persistent Audit Trail

ใช้ synthetic data เท่านั้น — ห้ามใช้ข้อมูลผู้ป่วยจริง
"""

import json
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

# ═══════════════════════════════════════════════════════════
# Test Storage Helper
# ═══════════════════════════════════════════════════════════

class MockStorage:
    """In-memory storage สำหรับ testing."""

    def __init__(self):
        self._data: dict[str, bytes] = {}

    def put(self, path: str, data: bytes, content_type: str = "",
            **kwargs) -> None:
        self._data[path] = data

    def get(self, path: str) -> bytes | None:
        return self._data.get(path)

    def list_files(self, prefix: str = "") -> list[str]:
        return [k for k in self._data.keys() if k.startswith(prefix)]

    def save(self, path: str, data: bytes) -> None:
        self._data[path] = data


class MockAuditLogger:
    """Mock audit logger for testing."""

    def __init__(self):
        self.events: list[dict] = []
        self.should_fail = False

    def log(self, action: str, actor: str,
            details: dict | None = None, **kwargs) -> bool:
        if self.should_fail:
            raise Exception("Audit persistence failed")
        self.events.append({
            "action": action,
            "actor": actor,
            "details": details or {},
            "timestamp": datetime.now().isoformat(),
        })
        return True


# ═══════════════════════════════════════════════════════════
# Task 1 — Physician Approval Tests
# ═══════════════════════════════════════════════════════════

class TestPhysicianApproval(unittest.TestCase):
    """ทดสอบ physician approval workflow."""

    @classmethod
    def setUpClass(cls):
        from physician_approval import (
            PhysicianApprovalService, EnhancedLabReport,
            PhysicianRole, ReviewAction,
            UnauthorizedPhysicianError, StaleVersionError,
            InvalidStateTransitionError, ReviewNotesRequiredError,
            DuplicateApprovalError,
        )
        cls.PhysicianApprovalService = PhysicianApprovalService
        cls.EnhancedLabReport = EnhancedLabReport
        cls.PhysicianRole = PhysicianRole
        cls.ReviewAction = ReviewAction
        cls.UnauthorizedPhysicianError = UnauthorizedPhysicianError
        cls.StaleVersionError = StaleVersionError
        cls.InvalidStateTransitionError = InvalidStateTransitionError
        cls.ReviewNotesRequiredError = ReviewNotesRequiredError
        cls.DuplicateApprovalError = DuplicateApprovalError

    def setUp(self):
        self.storage = MockStorage()
        self.audit = MockAuditLogger()
        self.physician_users = {"phys001a", "phys002b"}
        self.admin_users = {"admin001"}
        self.service = self.PhysicianApprovalService(
            storage=self.storage,
            audit_logger=self.audit,
            physician_users=self.physician_users,
            admin_users=self.admin_users,
        )
        # Seed a pending lab report
        self._seed_pending_report()

    def _seed_pending_report(self):
        from lab_models import LabReport, LabResult, ReviewStatus
        lab = LabReport(
            source_document_id="DOC_TEST_001",
            specimen_date="2026-01-15",
            extraction_method="per_line",
            results=[
                LabResult(
                    analyte="Creatinine",
                    value=2.5,
                    unit="mg/dL",
                    reference_range="0.6-1.2",
                    confidence=0.92,
                    extraction_method="per_line",
                ),
            ],
            status=ReviewStatus.PENDING_REVIEW.value,
        )
        self.lab_path = "_lab_results/owner1/DOC_TEST_001.json"
        self.storage.put(
            self.lab_path,
            lab.to_json().encode("utf-8"),
            content_type="application/json",
        )

    def test_physician_authorization(self):
        """แพทย์ที่ authorized สามารถ approve ได้."""
        self.assertTrue(self.service.is_physician("phys001a"))
        self.assertFalse(self.service.is_physician("random_user"))

    def test_unauthorized_approval_rejected(self):
        """ผู้ที่ไม่ใช่แพทย์ ถูกปฏิเสธ."""
        with self.assertRaises(self.UnauthorizedPhysicianError):
            self.service.approve_report(
                self.lab_path, "random_user", 1, "test"
            )

    def test_approve_pending_report(self):
        """Approve pending report สำเร็จ."""
        result = self.service.approve_report(
            self.lab_path, "phys001a", 1, "ค่าถูกต้อง"
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["new_status"], "approved")
        self.assertTrue(len(self.audit.events) > 0)

    def test_reject_requires_notes(self):
        """Reject ต้องมี notes."""
        with self.assertRaises(self.ReviewNotesRequiredError):
            self.service.reject_report(
                self.lab_path, "phys001a", 1, ""
            )

    def test_reject_with_notes(self):
        """Reject พร้อม notes สำเร็จ."""
        result = self.service.reject_report(
            self.lab_path, "phys001a", 1, "ค่าไม่น่าเชื่อถือ"
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["new_status"], "rejected")

    def test_correct_and_approve(self):
        """Correct OCR error แล้ว approve."""
        # corrections key = result index (string), not analyte name
        corrections = {"0": {"value": 2.3}}
        result = self.service.correct_and_approve(
            self.lab_path, "phys001a", 1,
            corrections, "แก้ไขค่า OCR"
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["new_status"], "approved")

    def test_duplicate_approval_prevented(self):
        """ป้องกัน duplicate approval."""
        self.service.approve_report(
            self.lab_path, "phys001a", 1, "ครั้งแรก"
        )
        with self.assertRaises((self.DuplicateApprovalError,
                                 self.InvalidStateTransitionError)):
            self.service.approve_report(
                self.lab_path, "phys002b", 2, "ครั้งที่สอง"
            )

    def test_stale_version_rejected(self):
        """Optimistic concurrency — stale version ถูกปฏิเสธ."""
        self.service.approve_report(
            self.lab_path, "phys001a", 1, "test"
        )
        with self.assertRaises((self.StaleVersionError,
                                 self.InvalidStateTransitionError,
                                 self.DuplicateApprovalError)):
            self.service.approve_report(
                self.lab_path, "phys002b", 1, "stale"
            )

    def test_review_history_immutable(self):
        """Review history ต้อง append-only."""
        self.service.approve_report(
            self.lab_path, "phys001a", 1, "approved"
        )
        history = self.service.get_review_history(
            self.lab_path, "phys001a"
        )
        self.assertTrue(len(history) > 0)

    def test_original_results_preserved(self):
        """ค่าเดิมต้องถูกเก็บไว้หลังแก้ไข."""
        # corrections key = result index (string)
        corrections = {"0": {"value": 2.3}}
        self.service.correct_and_approve(
            self.lab_path, "phys001a", 1,
            corrections, "แก้ไข"
        )
        # EnhancedLabReport stores original_results at top level
        stored = self.storage.get(self.lab_path)
        data = json.loads(stored)
        self.assertIn("original_results", data)
        self.assertTrue(len(data["original_results"]) > 0)


# ═══════════════════════════════════════════════════════════
# Task 2 — Critical Lab Alert Tests
# ═══════════════════════════════════════════════════════════

class TestAlertDelivery(unittest.TestCase):
    """ทดสอบ critical alert delivery."""

    @classmethod
    def setUpClass(cls):
        from alert_delivery import (
            AlertDeliveryService, CriticalAlert,
            AlertState, AlertSeverity,
        )
        cls.AlertDeliveryService = AlertDeliveryService
        cls.CriticalAlert = CriticalAlert
        cls.AlertState = AlertState
        cls.AlertSeverity = AlertSeverity

    def setUp(self):
        self.storage = MockStorage()
        self.audit = MockAuditLogger()
        self.service = self.AlertDeliveryService(
            storage=self.storage,
            audit_logger=self.audit,
            clinician_recipients={"physician": ["U_PHYS_001"]},
        )

    def test_create_critical_alert(self):
        """สร้าง critical alert."""
        alert = self.service.create_alert(
            doc_id="DOC001", owner_hash="owner123",
            analyte="Potassium", value=7.5, unit="mEq/L",
            alert_level="critical_high",
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.state, self.AlertState.QUEUED.value)
        self.assertEqual(alert.severity, self.AlertSeverity.CRITICAL.value)

    def test_no_phi_in_notification(self):
        """Notification ไม่มี PHI."""
        alert = self.service.create_alert(
            doc_id="DOC001", owner_hash="owner123",
            analyte="Potassium", value=7.5, unit="mEq/L",
            alert_level="critical_high",
        )
        # ไม่มีชื่อ, วันเกิด, เลขบัตร
        self.assertNotIn("owner123", alert.notification_text)
        self.assertIn("Potassium", alert.notification_text)

    def test_low_ocr_confidence_source_verify(self):
        """Low OCR confidence → source verification severity."""
        alert = self.service.create_alert(
            doc_id="DOC002", owner_hash="owner123",
            analyte="Creatinine", value=5.0, unit="mg/dL",
            alert_level="critical_high",
            ocr_confidence=0.5,
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, self.AlertSeverity.SOURCE_VERIFY.value)

    def test_egfr_dialysis_not_acute_emergency(self):
        """eGFR<15 ในผู้ป่วย maintenance dialysis ไม่ใช่ acute emergency."""
        alert = self.service.create_alert(
            doc_id="DOC003", owner_hash="owner123",
            analyte="eGFR", value=8.0, unit="mL/min/1.73m²",
            alert_level="critical_low",
            patient_context={"is_dialysis_patient": True},
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, self.AlertSeverity.URGENT.value)

    def test_deduplication(self):
        """Deduplicate same alert."""
        alert1 = self.service.create_alert(
            doc_id="DOC004", owner_hash="owner123",
            analyte="Potassium", value=7.5, unit="mEq/L",
            alert_level="critical_high",
        )
        alert2 = self.service.create_alert(
            doc_id="DOC004", owner_hash="owner123",
            analyte="Potassium", value=7.5, unit="mEq/L",
            alert_level="critical_high",
        )
        self.assertIsNotNone(alert1)
        self.assertIsNone(alert2)  # deduplicated

    def test_delivery_attempt_tracking(self):
        """Tracking delivery attempts."""
        alert = self.service.create_alert(
            doc_id="DOC005", owner_hash="owner123",
            analyte="Potassium", value=7.5, unit="mEq/L",
            alert_level="critical_high",
        )
        result = self.service.attempt_delivery(alert.alert_id)
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("delivery_attempts"), 1)

    def test_delivery_failure_escalation(self):
        """Failed delivery → escalation after max attempts."""
        alert = self.service.create_alert(
            doc_id="DOC006", owner_hash="owner123",
            analyte="Sodium", value=115, unit="mEq/L",
            alert_level="critical_low",
        )
        # Simulate max failures
        def fail_push(uid, msg):
            return False

        for _ in range(5):
            self.service.attempt_delivery(
                alert.alert_id, send_push_fn=fail_push
            )

        updated = self.service._load_alert(alert.alert_id)
        self.assertEqual(updated.state, self.AlertState.ESCALATED.value)

    def test_acknowledge_requires_physician(self):
        """API acceptance ≠ clinician acknowledgement."""
        alert = self.service.create_alert(
            doc_id="DOC007", owner_hash="owner123",
            analyte="Potassium", value=7.5, unit="mEq/L",
            alert_level="critical_high",
        )
        self.service.attempt_delivery(alert.alert_id)
        result = self.service.acknowledge_alert(
            alert.alert_id, "phys001a"
        )
        self.assertEqual(result.get("status"), "acknowledged")

    def test_resolve_requires_acknowledge_first(self):
        """ต้อง acknowledge ก่อน resolve."""
        alert = self.service.create_alert(
            doc_id="DOC008", owner_hash="owner123",
            analyte="Potassium", value=7.5, unit="mEq/L",
            alert_level="critical_high",
        )
        self.service.attempt_delivery(alert.alert_id)
        result = self.service.resolve_alert(alert.alert_id, "phys001a")
        self.assertIn("error", result)  # ยังไม่ได้ acknowledge

    def test_exponential_backoff(self):
        """Retry delay increases exponentially."""
        alert = self.CriticalAlert(
            alert_id="test",
            doc_id="DOC",
            owner_hash="owner",
            analyte="K",
            value=7.5,
            unit="mEq/L",
            alert_level="high",
        )
        alert.delivery_attempts = 1
        delay1 = alert.get_retry_delay()
        alert.delivery_attempts = 3
        delay3 = alert.get_retry_delay()
        self.assertGreater(delay3, delay1)

    def test_alert_state_transitions(self):
        """Valid state transitions."""
        alert = self.CriticalAlert(
            alert_id="test",
            doc_id="DOC",
            owner_hash="owner",
            analyte="K",
            value=7.5,
            unit="mEq/L",
            alert_level="high",
            state=self.AlertState.DETECTED.value,
        )
        self.assertTrue(alert.can_transition_to(self.AlertState.QUEUED.value))
        self.assertFalse(alert.can_transition_to(self.AlertState.RESOLVED.value))


# ═══════════════════════════════════════════════════════════
# Task 3 — Patient Identity Tests
# ═══════════════════════════════════════════════════════════

class TestPatientIdentity(unittest.TestCase):
    """ทดสอบ patient identity verification."""

    @classmethod
    def setUpClass(cls):
        from patient_identity import (
            PatientIdentityService, PatientRecord,
            DocumentAssociation, DocumentAssociationStatus,
            PatientStatus, SubmitterRole,
        )
        cls.PatientIdentityService = PatientIdentityService
        cls.PatientRecord = PatientRecord
        cls.DocumentAssociation = DocumentAssociation
        cls.DocumentAssociationStatus = DocumentAssociationStatus
        cls.PatientStatus = PatientStatus
        cls.SubmitterRole = SubmitterRole

    def setUp(self):
        self.storage = MockStorage()
        self.audit = MockAuditLogger()
        self.service = self.PatientIdentityService(
            storage=self.storage,
            audit_logger=self.audit,
            clinic_staff_users={"staff001"},
        )

    def test_register_patient(self):
        """ลงทะเบียนผู้ป่วย."""
        patient = self.service.register_patient(
            owner_hash="owner123", submitter_hash="owner123"
        )
        self.assertIsNotNone(patient.patient_id)
        self.assertEqual(patient.status, self.PatientStatus.ACTIVE.value)

    def test_clinic_staff_register_patient(self):
        """Clinic staff ลงทะเบียนผู้ป่วย."""
        patient = self.service.register_patient(
            owner_hash="patient456",
            submitter_hash="staff001",
            submitter_role=self.SubmitterRole.CLINIC_STAFF.value,
        )
        self.assertIsNotNone(patient.patient_id)

    def test_unauthorized_staff_rejected(self):
        """ผู้ที่ไม่ใช่ clinic staff ถูกปฏิเสธ."""
        with self.assertRaises(PermissionError):
            self.service.register_patient(
                owner_hash="patient789",
                submitter_hash="random_user",
                submitter_role=self.SubmitterRole.CLINIC_STAFF.value,
            )

    def test_associate_document(self):
        """เชื่อมโยงเอกสารกับผู้ป่วย."""
        patient = self.service.register_patient(
            owner_hash="owner123", submitter_hash="owner123"
        )
        assoc = self.service.associate_document(
            doc_id="DOC001",
            patient_id=patient.patient_id,
            submitter_hash="owner123",
            auto_verify=True,
        )
        self.assertEqual(
            assoc.status,
            self.DocumentAssociationStatus.VERIFIED.value
        )

    def test_unverified_blocks_aggregation(self):
        """เอกสารที่ยังไม่ verified ไม่ถูก aggregate."""
        patient = self.service.register_patient(
            owner_hash="owner123", submitter_hash="owner123"
        )
        self.service.associate_document(
            doc_id="DOC002",
            patient_id=patient.patient_id,
            submitter_hash="owner123",
            auto_verify=False,
        )
        self.assertFalse(
            self.service.is_verified_for_aggregation("DOC002")
        )

    def test_verify_then_aggregate(self):
        """Verify แล้วจึง aggregate ได้."""
        patient = self.service.register_patient(
            owner_hash="owner123", submitter_hash="owner123"
        )
        self.service.associate_document(
            doc_id="DOC003",
            patient_id=patient.patient_id,
            submitter_hash="owner123",
        )
        self.service.verify_association("DOC003", "phys001a")
        self.assertTrue(
            self.service.is_verified_for_aggregation("DOC003")
        )

    def test_quarantine_document(self):
        """Quarantine เอกสารที่ไม่แน่ใจ."""
        assoc = self.service.quarantine_document(
            doc_id="DOC_UNKNOWN",
            reason="ไม่ทราบว่าเป็นของผู้ป่วยใด",
            actor_hash="staff001",
        )
        self.assertIsNotNone(assoc)
        self.assertEqual(
            assoc.status,
            self.DocumentAssociationStatus.QUARANTINED.value,
        )

    def test_reassign_document(self):
        """ย้ายเอกสารไปยังผู้ป่วยอื่น."""
        p1 = self.service.register_patient(
            owner_hash="owner1", submitter_hash="owner1"
        )
        p2 = self.service.register_patient(
            owner_hash="owner2", submitter_hash="owner2"
        )
        self.service.associate_document(
            doc_id="DOC_MOVE",
            patient_id=p1.patient_id,
            submitter_hash="owner1",
        )
        assoc = self.service.reassign_document(
            doc_id="DOC_MOVE",
            new_patient_id=p2.patient_id,
            actor_hash="staff001",
            reason="ส่งผิดคน",
        )
        self.assertEqual(
            assoc.status,
            self.DocumentAssociationStatus.REASSIGNED.value,
        )
        self.assertEqual(assoc.patient_id, p2.patient_id)

    def test_reassign_requires_reason(self):
        """Reassign ต้องมีเหตุผล."""
        p1 = self.service.register_patient(
            owner_hash="owner1", submitter_hash="owner1"
        )
        p2 = self.service.register_patient(
            owner_hash="owner2", submitter_hash="owner2"
        )
        self.service.associate_document(
            doc_id="DOC_X",
            patient_id=p1.patient_id,
            submitter_hash="owner1",
        )
        with self.assertRaises(ValueError):
            self.service.reassign_document(
                doc_id="DOC_X",
                new_patient_id=p2.patient_id,
                actor_hash="staff001",
                reason="",
            )

    def test_merge_patients(self):
        """Merge patient records."""
        p1 = self.service.register_patient(
            owner_hash="owner1", submitter_hash="owner1"
        )
        p2 = self.service.register_patient(
            owner_hash="owner1", submitter_hash="owner1",
            notes="duplicate"
        )
        self.service.associate_document(
            doc_id="DOC_A", patient_id=p1.patient_id,
            submitter_hash="owner1",
        )
        target = self.service.merge_patients(
            source_id=p2.patient_id,
            target_id=p1.patient_id,
            actor_hash="staff001",
            reason="duplicate record",
        )
        source = self.service.get_patient(p2.patient_id)
        self.assertEqual(source.status, self.PatientStatus.MERGED.value)

    def test_never_infer_from_line_sender(self):
        """ห้าม infer patient identity จาก LINE sender.

        ทดสอบว่า associate_document ต้องมี patient_id ชัดเจน
        """
        # ไม่มี method ที่ infer patient จาก sender
        self.assertFalse(
            hasattr(self.service, 'infer_patient_from_sender')
        )
        # ต้อง register ก่อน associate
        with self.assertRaises(ValueError):
            self.service.associate_document(
                doc_id="DOC_NO_PATIENT",
                patient_id="nonexistent_id",
                submitter_hash="random",
            )

    def test_cross_patient_isolation(self):
        """ป้องกัน cross-patient data mixing."""
        p1 = self.service.register_patient(
            owner_hash="owner1", submitter_hash="owner1"
        )
        p2 = self.service.register_patient(
            owner_hash="owner2", submitter_hash="owner2"
        )
        self.service.associate_document(
            doc_id="DOC_P1", patient_id=p1.patient_id,
            submitter_hash="owner1", auto_verify=True,
        )
        # P2 ไม่ได้รับเอกสารของ P1
        p2_docs = self.service.get_patient_documents(p2.patient_id)
        self.assertNotIn("DOC_P1", p2_docs)


# ═══════════════════════════════════════════════════════════
# Task 4 — OCR Safety Tests
# ═══════════════════════════════════════════════════════════

class TestOcrSafety(unittest.TestCase):
    """ทดสอบ OCR clinical safety."""

    @classmethod
    def setUpClass(cls):
        from ocr_safety import (
            OcrSafetyValidator, OcrValidationResult, OcrFlag,
        )
        cls.OcrSafetyValidator = OcrSafetyValidator
        cls.OcrValidationResult = OcrValidationResult
        cls.OcrFlag = OcrFlag

    def setUp(self):
        self.validator = self.OcrSafetyValidator()

    def test_high_confidence_valid(self):
        """High confidence result passes."""
        result = self.validator.validate_result(
            "creatinine", 1.2, "mg/dL", confidence=0.95
        )
        self.assertTrue(result.is_valid)
        self.assertFalse(result.requires_review)

    def test_low_confidence_requires_review(self):
        """Low confidence ต้อง review."""
        result = self.validator.validate_result(
            "creatinine", 1.2, "mg/dL", confidence=0.65
        )
        self.assertTrue(result.requires_review)
        self.assertTrue(result.requires_source_verification)
        self.assertIn(self.OcrFlag.LOW_CONFIDENCE, result.flags)

    def test_very_low_confidence_invalid(self):
        """Very low confidence ไม่ valid."""
        result = self.validator.validate_result(
            "creatinine", 1.2, "mg/dL", confidence=0.30
        )
        self.assertFalse(result.is_valid)
        self.assertIn(self.OcrFlag.VERY_LOW_CONFIDENCE, result.flags)

    def test_low_confidence_never_auto_approved(self):
        """Low-confidence results must NEVER be automatically approved."""
        result = self.validator.validate_result(
            "potassium", 5.0, "mEq/L", confidence=0.75
        )
        self.assertTrue(result.requires_review)

    def test_unit_mismatch(self):
        """Unit mismatch detected."""
        result = self.validator.validate_result(
            "creatinine", 1.2, "mmol/L", confidence=0.95
        )
        self.assertIn(self.OcrFlag.UNIT_MISMATCH, result.flags)

    def test_unit_missing(self):
        """Missing unit flagged."""
        result = self.validator.validate_result(
            "creatinine", 1.2, "", confidence=0.95
        )
        self.assertIn(self.OcrFlag.UNIT_MISSING, result.flags)

    def test_implausible_value(self):
        """Implausible value — likely OCR error."""
        result = self.validator.validate_result(
            "creatinine", 500, "mg/dL", confidence=0.95
        )
        self.assertFalse(result.is_valid)
        self.assertIn(self.OcrFlag.IMPLAUSIBLE_VALUE, result.flags)

    def test_duplicate_analyte_detected(self):
        """Duplicate analytes detected in report."""
        results = [
            {"analyte": "creatinine", "value": 1.2, "unit": "mg/dL",
             "confidence": 0.95},
            {"analyte": "creatinine", "value": 1.5, "unit": "mg/dL",
             "confidence": 0.90},
        ]
        report = self.validator.validate_report(results)
        self.assertIn(self.OcrFlag.DUPLICATE_ANALYTE, report["report_flags"])

    def test_specimen_date_missing(self):
        """Specimen date missing flagged."""
        results = [
            {"analyte": "hemoglobin", "value": 12.0, "unit": "g/dL",
             "confidence": 0.95},
        ]
        report = self.validator.validate_report(results, specimen_date=None)
        self.assertIn(self.OcrFlag.SPECIMEN_DATE_MISSING,
                      report["report_flags"])

    def test_specimen_date_future(self):
        """Future specimen date flagged."""
        future = (datetime.now() + timedelta(days=30)).isoformat()
        results = [
            {"analyte": "hemoglobin", "value": 12.0, "unit": "g/dL",
             "confidence": 0.95},
        ]
        report = self.validator.validate_report(results, specimen_date=future)
        self.assertIn(self.OcrFlag.SPECIMEN_DATE_FUTURE,
                      report["report_flags"])

    def test_ambiguous_value(self):
        """Non-numeric value flagged."""
        results = [
            {"analyte": "creatinine", "value": "N/A", "unit": "mg/dL",
             "confidence": 0.90},
        ]
        report = self.validator.validate_report(results)
        self.assertTrue(report["requires_review"])

    def test_unit_normalization(self):
        """Unit aliases normalized correctly."""
        self.assertEqual(
            self.validator.normalize_unit("mg/dl"), "mg/dL"
        )
        self.assertEqual(
            self.validator.normalize_unit("meq/l"), "mEq/L"
        )

    def test_plausible_value_passes(self):
        """Plausible value within range passes."""
        result = self.validator.validate_result(
            "hemoglobin", 12.5, "g/dL", confidence=0.95
        )
        self.assertTrue(result.is_valid)
        self.assertNotIn(self.OcrFlag.IMPLAUSIBLE_VALUE, result.flags)

    def test_multi_page_detection(self):
        """Multi-page results flagged."""
        results = [
            {"analyte": "hemoglobin", "value": 12.0, "unit": "g/dL",
             "confidence": 0.95, "source_page": 1},
            {"analyte": "creatinine", "value": 1.2, "unit": "mg/dL",
             "confidence": 0.95, "source_page": 2},
        ]
        report = self.validator.validate_report(
            results, specimen_date=datetime.now().isoformat(),
            total_pages=2,
        )
        self.assertIn(self.OcrFlag.MULTI_PAGE_MISMATCH,
                      report["report_flags"])

    def test_report_summary(self):
        """Report summary generated."""
        results = [
            {"analyte": "hemoglobin", "value": 12.0, "unit": "g/dL",
             "confidence": 0.95},
        ]
        report = self.validator.validate_report(
            results, specimen_date=datetime.now().isoformat()
        )
        self.assertIn("summary", report)
        self.assertIn("stats", report)


# ═══════════════════════════════════════════════════════════
# Task 5 — Container Security Tests
# ═══════════════════════════════════════════════════════════

class TestContainerSecurity(unittest.TestCase):
    """ทดสอบ Dockerfile security."""

    def test_non_root_user(self):
        """Dockerfile ใช้ non-root user."""
        with open("Dockerfile", "r") as f:
            content = f.read()
        self.assertIn("USER appuser", content)
        self.assertIn("groupadd", content)
        self.assertIn("useradd", content)

    def test_no_embedded_credentials(self):
        """ไม่มี credentials ใน Dockerfile."""
        with open("Dockerfile", "r") as f:
            content = f.read().lower()
        for secret_word in ["password", "secret", "api_key", "token",
                            "credential"]:
            self.assertNotIn(secret_word, content)

    def test_minimal_dependencies(self):
        """ใช้ --no-install-recommends."""
        with open("Dockerfile", "r") as f:
            content = f.read()
        self.assertIn("--no-install-recommends", content)

    def test_ocr_support_present(self):
        """Tesseract OCR + Thai ยังคงอยู่."""
        with open("Dockerfile", "r") as f:
            content = f.read()
        self.assertIn("tesseract-ocr", content)
        self.assertIn("tesseract-ocr-tha", content)

    def test_new_modules_copied(self):
        """New modules ถูก COPY ใน Dockerfile."""
        with open("Dockerfile", "r") as f:
            content = f.read()
        for module in ["physician_approval.py", "alert_delivery.py",
                       "patient_identity.py", "ocr_safety.py",
                       "health_check.py", "audit_trail.py"]:
            self.assertIn(module, content,
                          f"Missing COPY for {module}")


# ═══════════════════════════════════════════════════════════
# Task 6 — Health Monitoring Tests
# ═══════════════════════════════════════════════════════════

class TestHealthMonitoring(unittest.TestCase):
    """ทดสอบ health endpoints."""

    def setUp(self):
        from flask import Flask
        from health_check import (
            register_health_routes, init_health, _health_state,
        )
        # Reset health state cache between tests
        _health_state["last_gcs_check"] = None
        _health_state["gcs_available"] = None
        _health_state["last_error"] = None

        self.app = Flask(__name__)
        self.storage = MockStorage()
        init_health()
        register_health_routes(self.app, self.storage)
        self.client = self.app.test_client()

    def test_liveness_endpoint(self):
        """/health/live returns 200."""
        resp = self.client.get("/health/live")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "alive")

    def test_readiness_endpoint(self):
        """/health/ready returns 200 with storage available."""
        resp = self.client.get("/health/ready")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "ready")

    def test_readiness_no_credentials_exposed(self):
        """/health/ready ไม่เปิดเผย credentials."""
        resp = self.client.get("/health/ready")
        data = resp.data.decode("utf-8").lower()
        for secret in ["password", "secret", "token", "key",
                        "bucket", "service_account"]:
            self.assertNotIn(secret, data)

    def test_legacy_health_endpoint(self):
        """/health ยังใช้งานได้."""
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_readiness_storage_down(self):
        """/health/ready returns 503 when storage fails."""
        class FailStorage:
            def list_files(self, prefix):
                raise Exception("GCS unavailable")

        from health_check import register_health_routes, init_health
        app = __import__("flask").Flask(__name__)
        init_health()
        register_health_routes(app, FailStorage())
        client = app.test_client()

        # Need to reset cache first
        from health_check import _health_state
        _health_state["last_gcs_check"] = None
        _health_state["gcs_available"] = None

        resp = client.get("/health/ready")
        self.assertEqual(resp.status_code, 503)

    def test_proper_http_status_codes(self):
        """ใช้ HTTP status codes ที่เหมาะสม."""
        resp = self.client.get("/health/live")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content_type, "application/json")


# ═══════════════════════════════════════════════════════════
# Task 7 — Audit Trail Tests
# ═══════════════════════════════════════════════════════════

class TestAuditTrail(unittest.TestCase):
    """ทดสอบ persistent audit trail."""

    @classmethod
    def setUpClass(cls):
        from audit_trail import (
            PersistentAuditTrail, EnhancedAuditLogger,
            AuditPersistenceError, AuditEvent,
        )
        cls.PersistentAuditTrail = PersistentAuditTrail
        cls.EnhancedAuditLogger = EnhancedAuditLogger
        cls.AuditPersistenceError = AuditPersistenceError
        cls.AuditEvent = AuditEvent

    def setUp(self):
        self.storage = MockStorage()
        self.trail = self.PersistentAuditTrail(
            self.storage, fail_closed=True
        )

    def test_log_event(self):
        """บันทึก audit event."""
        event = self.trail.log(
            action="test_action",
            actor_hash="actor123",
            details={"key": "value"},
        )
        self.assertIsNotNone(event)
        self.assertTrue(len(event.event_id) > 0)

    def test_event_immutability(self):
        """Event records are immutable (append-only)."""
        event = self.trail.log(
            action="immutable_test",
            actor_hash="actor123",
        )
        # Verify stored in GCS
        month = event.timestamp[:7]
        path = f"_audit/{month}/{event.event_id}.json"
        data = self.storage.get(path)
        self.assertIsNotNone(data)
        stored = json.loads(data)
        self.assertEqual(stored["action"], "immutable_test")

    def test_uuid_event_ids(self):
        """Event IDs are UUIDs."""
        event = self.trail.log(
            action="uuid_test", actor_hash="actor123"
        )
        self.assertEqual(len(event.event_id), 32)  # hex UUID

    def test_actor_pseudonymous(self):
        """Actor identity is pseudonymous (truncated hash)."""
        event = self.trail.log(
            action="actor_test",
            actor_hash="very_long_user_hash_string",
        )
        self.assertEqual(len(event.actor_hash), 8)

    def test_fail_closed_security_critical(self):
        """Security-critical actions fail-closed when audit fails."""
        class FailStorage:
            def put(self, *args, **kwargs):
                raise Exception("GCS write failed")
            def get(self, *args, **kwargs):
                return None
            def list_files(self, *args, **kwargs):
                return []

        trail = self.PersistentAuditTrail(
            FailStorage(), fail_closed=True
        )
        with self.assertRaises(self.AuditPersistenceError):
            trail.log(
                action="physician_approval",
                actor_hash="phys001",
            )

    def test_non_critical_continues_on_failure(self):
        """Non-critical actions continue when audit fails."""
        class FailStorage:
            def put(self, *args, **kwargs):
                raise Exception("GCS write failed")
            def get(self, *args, **kwargs):
                return None
            def list_files(self, *args, **kwargs):
                return []

        trail = self.PersistentAuditTrail(
            FailStorage(), fail_closed=True
        )
        result = trail.log(
            action="non_critical_action",
            actor_hash="user123",
        )
        self.assertIsNone(result)  # Failed but didn't raise

    def test_sanitize_phi(self):
        """PHI ไม่ถูกเก็บใน audit log."""
        event = self.trail.log(
            action="test",
            actor_hash="actor123",
            details={
                "safe_key": "safe_value",
                "patient_name": "ข้อมูลลับ",
                "password": "secret123",
                "token": "tok_xxx",
            },
        )
        stored = json.loads(
            self.storage.get(
                f"_audit/{event.timestamp[:7]}/{event.event_id}.json"
            )
        )
        self.assertEqual(stored["details"]["safe_key"], "safe_value")
        self.assertEqual(stored["details"]["patient_name"], "[REDACTED]")
        self.assertEqual(stored["details"]["password"], "[REDACTED]")

    def test_monthly_partitioning(self):
        """Events partitioned by month."""
        event = self.trail.log(
            action="partition_test", actor_hash="actor"
        )
        month = datetime.now().strftime("%Y-%m")
        files = self.storage.list_files(f"_audit/{month}/")
        self.assertTrue(len(files) > 0)

    def test_concurrency_safe_uuid_filenames(self):
        """UUID filenames prevent collision."""
        events = []
        for i in range(10):
            event = self.trail.log(
                action=f"concurrent_{i}", actor_hash="actor"
            )
            events.append(event)
        # All events should have unique IDs
        ids = [e.event_id for e in events]
        self.assertEqual(len(ids), len(set(ids)))

    def test_query_events(self):
        """Query events by month."""
        for i in range(3):
            self.trail.log(action=f"query_test_{i}", actor_hash="actor")

        events = self.trail.query_events()
        self.assertEqual(len(events), 3)

    def test_category_auto_detection(self):
        """Category auto-detected from action."""
        event = self.trail.log(
            action="physician_approval", actor_hash="phys"
        )
        self.assertEqual(event.category, "clinical")

        event2 = self.trail.log(
            action="auth_failure", actor_hash="user"
        )
        self.assertEqual(event2.category, "security")

    def test_enhanced_logger_backward_compatible(self):
        """EnhancedAuditLogger backward compatible."""
        logger = self.EnhancedAuditLogger(self.storage)
        result = logger.log("test", "actor", {"key": "val"})
        self.assertTrue(result)

    def test_failure_tracking(self):
        """Track write failures."""
        initial = self.trail.get_write_failure_count()
        self.assertEqual(initial, 0)

    def test_retention_support(self):
        """Monthly partitioning supports retention policy."""
        event = self.trail.log(action="retention_test", actor_hash="a")
        month = event.timestamp[:7]
        # Events are organized by month — can delete old months
        files = self.storage.list_files(f"_audit/{month}/")
        self.assertTrue(all(month in f for f in files))


# ═══════════════════════════════════════════════════════════
# Cross-Module Integration Tests
# ═══════════════════════════════════════════════════════════

class TestCrossModuleIntegration(unittest.TestCase):
    """ทดสอบการทำงานร่วมกันระหว่าง modules."""

    def test_ocr_to_approval_flow(self):
        """OCR validation → physician approval flow."""
        from ocr_safety import OcrSafetyValidator
        from physician_approval import PhysicianApprovalService

        validator = OcrSafetyValidator()
        results = [
            {"analyte": "creatinine", "value": 2.5, "unit": "mg/dL",
             "confidence": 0.65},
        ]
        report = validator.validate_report(
            results, specimen_date=datetime.now().isoformat()
        )
        # Low confidence → requires review
        self.assertTrue(report["requires_review"])
        # Physician must review — never auto-approve

    def test_alert_with_ocr_confidence(self):
        """Alert severity depends on OCR confidence."""
        from alert_delivery import AlertDeliveryService, AlertSeverity

        storage = MockStorage()
        audit = MockAuditLogger()
        service = AlertDeliveryService(storage, audit)

        # High confidence → critical
        alert1 = service.create_alert(
            "DOC1", "owner1", "K", 7.5, "mEq/L",
            "critical_high", ocr_confidence=0.95,
        )
        self.assertEqual(alert1.severity, AlertSeverity.CRITICAL.value)

        # Low confidence → source verify
        alert2 = service.create_alert(
            "DOC2", "owner2", "K", 7.5, "mEq/L",
            "critical_high", ocr_confidence=0.50,
        )
        self.assertEqual(alert2.severity, AlertSeverity.SOURCE_VERIFY.value)

    def test_patient_identity_before_aggregation(self):
        """Patient identity must be verified before longitudinal aggregation."""
        from patient_identity import PatientIdentityService

        storage = MockStorage()
        audit = MockAuditLogger()
        service = PatientIdentityService(storage, audit)

        patient = service.register_patient("owner1", "owner1")
        service.associate_document(
            "DOC1", patient.patient_id, "owner1"
        )
        # Not verified yet
        docs = service.get_patient_documents(patient.patient_id)
        self.assertEqual(len(docs), 0)  # No verified docs

        # After verification
        service.verify_association("DOC1", "phys001")
        docs = service.get_patient_documents(patient.patient_id)
        self.assertEqual(len(docs), 1)

    def test_audit_trail_for_all_modules(self):
        """All critical actions produce audit events."""
        from audit_trail import PersistentAuditTrail

        storage = MockStorage()
        trail = PersistentAuditTrail(storage)

        # Various actions from different modules
        actions = [
            "physician_approval",
            "alert_created",
            "patient_registered",
            "document_quarantined",
        ]
        for action in actions:
            event = trail.log(action, "actor")
            self.assertIsNotNone(event)

        events = trail.query_events()
        self.assertEqual(len(events), len(actions))


if __name__ == "__main__":
    unittest.main()
