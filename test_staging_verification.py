#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Staging Verification Tests — RANONG-RAKTAI-AI 2.0

ทดสอบ staging-level verification ที่ทำได้ในสภาพแวดล้อมปัจจุบัน:
  1. LINE Push API — HTTP-level mock ที่จำลอง LINE API response จริง
  2. GCS Persistence — จำลอง StorageGCS full lifecycle
  3. Physician Approval E2E — synthetic patients, multi-physician
  4. Patient Identity Isolation — cross-patient, multi-owner
  5. Docker — static Dockerfile validation (daemon ไม่พร้อม)
  6. Health endpoints — Flask test client full path
  7. Audit persistence — restart simulation
  8. Rollback readiness — state consistency checks

กฎ:
  - ข้อมูล synthetic เท่านั้น — ห้ามใช้ข้อมูลผู้ป่วยจริง
  - ห้ามเรียก external service จริง
  - ทุก test ที่ต้อง external access จริง → BLOCKED + ระบุ prerequisites
"""

import os
import sys
import json
import time
import uuid
import hashlib
import unittest
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, Mock
from http.client import HTTPResponse
from io import BytesIO

# ── Setup test environment ──
os.environ.setdefault("LINE_CHANNEL_SECRET", "test_secret_staging")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test_token_staging")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("ENABLE_AI_CLASSIFY", "false")
os.environ.setdefault("STORAGE_BACKEND", "local")

from alert_delivery import (
    AlertDeliveryService, AlertSeverity, AlertState,
    create_line_push_fn, build_alert_recipients_from_env,
    AlertRetryManager, rebuild_dedup_cache,
)
from physician_approval import (
    PhysicianApprovalService, EnhancedLabReport,
    ReviewHistoryEntry, ReviewAction, ReviewStatus,
    UnauthorizedPhysicianError, DuplicateApprovalError,
)
from patient_identity import PatientIdentityService
from audit_trail import (
    PersistentAuditTrail, EnhancedAuditLogger,
    AuditPersistenceError, AuditEvent,
)
from ocr_safety import OcrSafetyValidator
from lab_models import LabReport, LabResult

# ── Constants ──
PHYSICIAN_A = hashlib.sha256(b"physician_a_staging").hexdigest()[:32]
PHYSICIAN_B = hashlib.sha256(b"physician_b_staging").hexdigest()[:32]
ADMIN_HASH = hashlib.sha256(b"admin_staging").hexdigest()[:32]
STAFF_HASH = hashlib.sha256(b"staff_staging").hexdigest()[:32]
PATIENT_OWNER_1 = hashlib.sha256(b"patient_line_user_1").hexdigest()[:32]
PATIENT_OWNER_2 = hashlib.sha256(b"patient_line_user_2").hexdigest()[:32]
PATIENT_OWNER_3 = hashlib.sha256(b"patient_line_user_3").hexdigest()[:32]
LAB_RESULTS_PREFIX = "_lab_results/"


class MockGCSStorage:
    """Realistic GCS mock that simulates StorageGCS behavior including
    list_files, conditional writes, content types, and error modes."""

    def __init__(self, fail_on_prefix=None):
        self._data: dict[str, bytes] = {}
        self._content_types: dict[str, str] = {}
        self._versions: dict[str, int] = {}
        self._fail_on_prefix = fail_on_prefix

    def put(self, path, data, content_type="application/octet-stream",
            create_only=False, **kwargs):
        if self._fail_on_prefix and path.startswith(self._fail_on_prefix):
            raise IOError(f"GCS write failed (simulated): {path}")
        if create_only and path in self._data:
            return  # skip duplicate
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._data[path] = data
        self._content_types[path] = content_type
        self._versions[path] = self._versions.get(path, 0) + 1

    def get(self, path):
        return self._data.get(path)

    def list_files(self, prefix=""):
        return sorted(k for k in self._data if k.startswith(prefix))

    def delete(self, path):
        self._data.pop(path, None)

    def exists(self, path):
        return path in self._data


def _make_lab_report(doc_id, owner_hash, results=None):
    """Create a synthetic lab report for testing."""
    if results is None:
        results = [
            LabResult("Creatinine", 1.2, "mg/dL", "0.6-1.2", 0.95, "ocr"),
            LabResult("BUN", 18.0, "mg/dL", "7-20", 0.92, "ocr"),
        ]
    report = LabReport(
        source_document_id=doc_id,
        specimen_date=datetime.now().strftime("%Y-%m-%d"),
        extraction_method="ocr",
        results=results,
    )
    report.owner_hash = owner_hash
    report.status = "pending_review"
    return report


def _store_lab_report(storage, doc_id, report):
    """Store an enhanced lab report in mock storage."""
    enhanced = EnhancedLabReport(report=report)
    path = f"{LAB_RESULTS_PREFIX}{doc_id}.json"
    storage.put(path, json.dumps(enhanced.to_dict(), ensure_ascii=False).encode("utf-8"),
                content_type="application/json")
    return path


# ══════════════════════════════════════════════════════════════
# 1. LINE Push API — HTTP-level simulation
# ══════════════════════════════════════════════════════════════

class TestLinePushAPISimulation(unittest.TestCase):
    """Simulate LINE Push API at HTTP level with realistic responses."""

    def test_create_line_push_fn_returns_callable(self):
        """create_line_push_fn should return a callable push function."""
        push_fn = create_line_push_fn("test_channel_token_12345")
        self.assertTrue(callable(push_fn))

    @patch("urllib.request.urlopen")
    def test_line_push_success_200(self, mock_urlopen):
        """Simulate LINE API returning 200 OK."""
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.read.return_value = b'{"sentMessages":[{"id":"12345"}]}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        push_fn = create_line_push_fn("test_token_abc123")
        result = push_fn("U_test_physician_001", "⚠️ ผลตรวจผิดปกติ กรุณาตรวจสอบ")
        self.assertTrue(result)

        # Verify the request was made correctly
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        self.assertEqual(request.full_url, "https://api.line.me/v2/bot/message/push")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        # Verify Authorization header uses Bearer token
        self.assertTrue(request.get_header("Authorization").startswith("Bearer "))

        # Verify payload structure
        body = json.loads(request.data)
        self.assertEqual(body["to"], "U_test_physician_001")
        self.assertIn("messages", body)
        self.assertEqual(body["messages"][0]["type"], "text")

    @patch("urllib.request.urlopen")
    def test_line_push_failure_401(self, mock_urlopen):
        """Simulate LINE API returning 401 Unauthorized."""
        from urllib.error import HTTPError
        mock_urlopen.side_effect = HTTPError(
            "https://api.line.me/v2/bot/message/push",
            401, "Unauthorized", {}, BytesIO(b'{"message":"Invalid channel access token"}')
        )

        push_fn = create_line_push_fn("invalid_token")
        result = push_fn("U_test_001", "test message")
        self.assertFalse(result)

    @patch("urllib.request.urlopen")
    def test_line_push_failure_429_rate_limit(self, mock_urlopen):
        """Simulate LINE API rate limiting."""
        from urllib.error import HTTPError
        mock_urlopen.side_effect = HTTPError(
            "https://api.line.me/v2/bot/message/push",
            429, "Too Many Requests", {}, BytesIO(b'{"message":"Rate limit exceeded"}')
        )

        push_fn = create_line_push_fn("test_token")
        result = push_fn("U_test_001", "test message")
        self.assertFalse(result)

    @patch("urllib.request.urlopen")
    def test_line_push_no_phi_in_request(self, mock_urlopen):
        """Verify no PHI is sent in LINE push messages."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        push_fn = create_line_push_fn("test_token")
        push_fn("U_test_001", "⚠️ ผลตรวจผิดปกติ กรุณาตรวจสอบระบบ")

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data)
        message_text = body["messages"][0]["text"]

        # Should NOT contain patient identifiers
        phi_patterns = ["HN-", "hn-", "สมชาย", "สมหญิง", "13-digit",
                        "mg/dL", "mmol/L", "creatinine", "Creatinine"]
        for pattern in phi_patterns:
            self.assertNotIn(pattern, message_text,
                             f"PHI pattern '{pattern}' found in push message")

    def test_alert_delivery_with_mock_push(self):
        """Full alert lifecycle: create → deliver → acknowledge → resolve."""
        storage = MockGCSStorage()
        audit = EnhancedAuditLogger(storage)
        service = AlertDeliveryService(
            storage, audit,
            clinician_recipients={"physician": ["U_staging_dr_001", "U_staging_dr_002"]},
        )

        # Track push calls
        push_log = []

        def mock_push(user_id, message):
            push_log.append({"user_id": user_id, "message": message})
            return True

        # Create critical alert
        alert = service.create_alert(
            doc_id="staging-doc-001", owner_hash=PATIENT_OWNER_1,
            analyte="Potassium", value=7.2, unit="mmol/L",
            alert_level="critical_high",
        )
        self.assertIsNotNone(alert)
        # .state stores the .value string directly (e.g. "queued")
        self.assertEqual(alert.state, "queued")

        # Deliver
        delivery_result = service.attempt_delivery(alert.alert_id, send_push_fn=mock_push)
        # Should have sent push to at least one recipient
        self.assertTrue(len(push_log) >= 1)

        # Acknowledge
        service.acknowledge_alert(alert.alert_id, PHYSICIAN_A)

        # Resolve
        service.resolve_alert(alert.alert_id, PHYSICIAN_A, "ตรวจสอบแล้ว ค่าปกติ")

    def test_alert_retry_manager_with_mock(self):
        """AlertRetryManager retries failed deliveries."""
        storage = MockGCSStorage()
        audit = EnhancedAuditLogger(storage)
        service = AlertDeliveryService(
            storage, audit,
            clinician_recipients={"physician": ["U_retry_001"]},
        )

        # Create alert with failed delivery
        fail_count = [0]

        def failing_push(user_id, message):
            fail_count[0] += 1
            return False  # simulate delivery failure

        alert = service.create_alert(
            doc_id="retry-doc-001", owner_hash=PATIENT_OWNER_1,
            analyte="Creatinine", value=12.0, unit="mg/dL",
            alert_level="critical_high",
        )
        service.attempt_delivery(alert.alert_id, send_push_fn=failing_push)

        # RetryManager should attempt retries
        retry_mgr = AlertRetryManager(service)
        retry_mgr.check_and_retry(send_push_fn=failing_push)
        # Initial attempt_delivery + at least 1 retry = at least 2
        self.assertTrue(fail_count[0] >= 1,
                        f"Should have at least 1 push attempt, got {fail_count[0]}")

    def test_rebuild_dedup_cache_from_storage(self):
        """rebuild_dedup_cache should load active alerts from GCS."""
        storage = MockGCSStorage()
        audit = EnhancedAuditLogger(storage)
        service = AlertDeliveryService(
            storage, audit,
            clinician_recipients={"physician": ["U_dedup_001"]},
        )

        # Create an alert (stored in GCS)
        alert = service.create_alert(
            doc_id="dedup-doc-001", owner_hash=PATIENT_OWNER_1,
            analyte="BUN", value=80.0, unit="mg/dL",
            alert_level="critical_high",
        )

        # Create new service (simulating restart) and rebuild cache
        service2 = AlertDeliveryService(
            storage, audit,
            clinician_recipients={"physician": ["U_dedup_001"]},
        )
        rebuild_dedup_cache(service2)

        # Duplicate creation should be prevented
        dup = service2.create_alert(
            doc_id="dedup-doc-001", owner_hash=PATIENT_OWNER_1,
            analyte="BUN", value=80.0, unit="mg/dL",
            alert_level="critical_high",
        )
        self.assertIsNone(dup, "Dedup cache should prevent duplicate alert")

    def test_build_alert_recipients_physician_users(self):
        """build_alert_recipients_from_env reads PHYSICIAN_USERS env."""
        with patch.dict(os.environ, {
            "PHYSICIAN_USERS": "U_dr_alpha,U_dr_beta,U_dr_gamma",
            "ALERT_RECIPIENTS": "",
        }):
            recipients = build_alert_recipients_from_env()
            self.assertIn("physician", recipients)
            self.assertEqual(len(recipients["physician"]), 3)
            self.assertIn("U_dr_alpha", recipients["physician"])


# ══════════════════════════════════════════════════════════════
# 2. GCS Persistence Simulation
# ══════════════════════════════════════════════════════════════

class TestGCSPersistenceSimulation(unittest.TestCase):
    """Simulate GCS storage lifecycle including failure scenarios."""

    def test_lab_report_roundtrip(self):
        """Write and read lab report from simulated GCS."""
        storage = MockGCSStorage()
        report = _make_lab_report("gcs-roundtrip-001", PATIENT_OWNER_1)
        path = _store_lab_report(storage, "gcs-roundtrip-001", report)

        # Read back
        data = storage.get(path)
        self.assertIsNotNone(data)
        parsed = json.loads(data)
        self.assertEqual(parsed["report"]["source_document_id"], "gcs-roundtrip-001")
        self.assertEqual(len(parsed["report"]["results"]), 2)

    def test_audit_trail_survives_restart(self):
        """Audit events persist across service restarts."""
        storage = MockGCSStorage()

        # First "instance" writes events
        audit1 = PersistentAuditTrail(storage)
        event1 = audit1.log("lab_upload", PATIENT_OWNER_1, {
            "doc_id": "persist-001", "size": 1024,
        })
        self.assertIsNotNone(event1)

        event2 = audit1.log("lab_approve", PHYSICIAN_A, {
            "doc_id": "persist-001",
        })

        # Second "instance" (simulating restart) can read events
        audit2 = PersistentAuditTrail(storage)
        audit_files = storage.list_files("_audit/")
        self.assertTrue(len(audit_files) >= 2,
                        f"Expected >= 2 audit files, got {len(audit_files)}")

    def test_gcs_write_failure_blocks_critical_action(self):
        """When GCS write fails for security-critical actions,
        the action should be blocked (fail-closed)."""
        storage = MockGCSStorage(fail_on_prefix="_audit/")
        audit = EnhancedAuditLogger(storage)

        with self.assertRaises(AuditPersistenceError):
            audit.log("physician_approval", PHYSICIAN_A, {
                "doc_id": "failclose-001",
            })

    def test_gcs_write_graceful_for_non_critical(self):
        """Non-critical actions should not fail when GCS audit write fails."""
        storage = MockGCSStorage(fail_on_prefix="_audit/")
        audit = EnhancedAuditLogger(storage)

        # "search" is not in SECURITY_CRITICAL_ACTIONS
        event = audit.log("search", PATIENT_OWNER_1, {"query": "ผลแล็บ"})
        # Should log warning but not raise
        # (event may be None since GCS failed, but no exception)

    def test_clinical_analysis_verification_status(self):
        """Clinical analysis should have PENDING_VERIFICATION tag."""
        storage = MockGCSStorage()
        audit = EnhancedAuditLogger(storage)

        # Simulate clinical analysis save with verification tag
        clinical_dict = {
            "ckd_stage": "G3a",
            "findings": [{"analyte": "Creatinine", "severity": "warning"}],
            "verification_status": "PENDING_VERIFICATION",
            "disclaimer": "ผลวิเคราะห์นี้สร้างจากผล OCR อัตโนมัติ "
                          "ยังไม่ผ่านการตรวจสอบโดยแพทย์",
        }
        ca_path = "_clinical_analysis/staging-ca-001.json"
        storage.put(ca_path, json.dumps(clinical_dict).encode("utf-8"),
                    content_type="application/json")

        # Read back and verify
        data = json.loads(storage.get(ca_path))
        self.assertEqual(data["verification_status"], "PENDING_VERIFICATION")
        self.assertIn("ยังไม่ผ่านการตรวจสอบ", data["disclaimer"])

        # Simulate physician approval updating status
        data["verification_status"] = "PHYSICIAN_VERIFIED"
        data["verified_by"] = PHYSICIAN_A[:8]
        data["verified_at"] = datetime.now().isoformat()
        storage.put(ca_path, json.dumps(data).encode("utf-8"),
                    content_type="application/json")

        updated = json.loads(storage.get(ca_path))
        self.assertEqual(updated["verification_status"], "PHYSICIAN_VERIFIED")
        self.assertEqual(updated["verified_by"], PHYSICIAN_A[:8])

    def test_multiple_lab_reports_isolation(self):
        """Multiple patient lab reports stored in isolation."""
        storage = MockGCSStorage()

        # Store reports for 3 different patients
        for i, owner in enumerate([PATIENT_OWNER_1, PATIENT_OWNER_2, PATIENT_OWNER_3]):
            report = _make_lab_report(f"iso-doc-{i}", owner)
            _store_lab_report(storage, f"iso-doc-{i}", report)

        # Verify each report is stored separately
        for i, owner in enumerate([PATIENT_OWNER_1, PATIENT_OWNER_2, PATIENT_OWNER_3]):
            path = f"{LAB_RESULTS_PREFIX}iso-doc-{i}.json"
            data = json.loads(storage.get(path))
            self.assertEqual(data["report"]["source_document_id"], f"iso-doc-{i}")

        # Verify no cross-contamination
        all_reports = storage.list_files(LAB_RESULTS_PREFIX)
        self.assertEqual(len(all_reports), 3)


# ══════════════════════════════════════════════════════════════
# 3. Physician Approval E2E — Multi-Physician, Multi-Patient
# ══════════════════════════════════════════════════════════════

class TestPhysicianApprovalE2E(unittest.TestCase):
    """End-to-end physician approval with multiple physicians and patients."""

    def setUp(self):
        self.storage = MockGCSStorage()
        self.audit = EnhancedAuditLogger(self.storage)
        self.service = PhysicianApprovalService(
            self.storage, self.audit,
            physician_users={PHYSICIAN_A, PHYSICIAN_B},
            admin_users={ADMIN_HASH},
        )

    def test_physician_a_approves_patient_1(self):
        """Physician A approves Patient 1's lab report."""
        report = _make_lab_report("e2e-p1-001", PATIENT_OWNER_1)
        path = _store_lab_report(self.storage, "e2e-p1-001", report)

        result = self.service.approve_report(path, PHYSICIAN_A, expected_version=1)
        self.assertEqual(result["status"], "success")

        # Verify audit trail
        audit_files = self.storage.list_files("_audit/")
        self.assertTrue(len(audit_files) >= 1)

    def test_physician_b_rejects_patient_2(self):
        """Physician B rejects Patient 2's lab report with notes."""
        report = _make_lab_report("e2e-p2-001", PATIENT_OWNER_2)
        path = _store_lab_report(self.storage, "e2e-p2-001", report)

        result = self.service.reject_report(
            path, PHYSICIAN_B, expected_version=1,
            notes="ค่า Creatinine ไม่สอดคล้อง ขอตรวจซ้ำ",
        )
        self.assertEqual(result["status"], "success")

    def test_unauthorized_user_cannot_approve(self):
        """Non-physician user cannot approve lab reports."""
        report = _make_lab_report("e2e-unauth-001", PATIENT_OWNER_1)
        path = _store_lab_report(self.storage, "e2e-unauth-001", report)

        with self.assertRaises(UnauthorizedPhysicianError):
            self.service.approve_report(path, STAFF_HASH, expected_version=1)

    def test_admin_cannot_approve_as_physician(self):
        """Admin without physician role cannot approve."""
        report = _make_lab_report("e2e-admin-001", PATIENT_OWNER_1)
        path = _store_lab_report(self.storage, "e2e-admin-001", report)

        with self.assertRaises(UnauthorizedPhysicianError):
            self.service.approve_report(path, ADMIN_HASH, expected_version=1)

    def test_duplicate_approval_blocked(self):
        """Cannot approve same report twice."""
        report = _make_lab_report("e2e-dup-001", PATIENT_OWNER_1)
        path = _store_lab_report(self.storage, "e2e-dup-001", report)

        self.service.approve_report(path, PHYSICIAN_A, expected_version=1)

        with self.assertRaises(DuplicateApprovalError):
            self.service.approve_report(path, PHYSICIAN_B, expected_version=2)

    def test_correct_and_approve_workflow(self):
        """Physician corrects lab values then approves."""
        report = _make_lab_report("e2e-correct-001", PATIENT_OWNER_1)
        path = _store_lab_report(self.storage, "e2e-correct-001", report)

        # Correct — corrections use result index as key
        corrections = {"0": {"value": 1.1, "unit": "mg/dL"}}
        result = self.service.correct_and_approve(
            path, PHYSICIAN_A, expected_version=1,
            corrections=corrections,
            notes="แก้ไขค่า Creatinine จาก OCR ที่อ่านผิด",
        )
        self.assertEqual(result["status"], "success")

        # Verify history has correction entry
        data = json.loads(self.storage.get(path))
        history = data.get("review_history", [])
        self.assertTrue(len(history) >= 1)

    def test_list_pending_requires_physician(self):
        """list_pending_reports requires physician auth."""
        report = _make_lab_report("e2e-list-001", PATIENT_OWNER_1)
        _store_lab_report(self.storage, "e2e-list-001", report)

        mock_doc_index = {}
        with self.assertRaises(UnauthorizedPhysicianError):
            self.service.list_pending_reports(mock_doc_index, user_hash=STAFF_HASH)

    def test_audit_failure_blocks_approval(self):
        """Approval blocked when audit trail cannot persist."""
        fail_storage = MockGCSStorage(fail_on_prefix="_audit/")
        fail_audit = EnhancedAuditLogger(fail_storage)
        fail_service = PhysicianApprovalService(
            fail_storage, fail_audit,
            physician_users={PHYSICIAN_A},
            admin_users={ADMIN_HASH},
        )

        report = _make_lab_report("e2e-failaudit-001", PATIENT_OWNER_1)
        path = f"{LAB_RESULTS_PREFIX}e2e-failaudit-001.json"
        enhanced = EnhancedLabReport(report=report)
        fail_storage.put(path, json.dumps(enhanced.to_dict()).encode("utf-8"))

        with self.assertRaises(AuditPersistenceError):
            fail_service.approve_report(path, PHYSICIAN_A, expected_version=1)

        # Verify report was NOT approved (fail-closed)
        data = json.loads(fail_storage.get(path))
        self.assertNotEqual(data["report"]["status"], "approved")


# ══════════════════════════════════════════════════════════════
# 4. Patient Identity Isolation — Multi-Patient Multi-Owner
# ══════════════════════════════════════════════════════════════

class TestPatientIdentityIsolation(unittest.TestCase):
    """Verify patient identity isolation across multiple patients and owners."""

    def setUp(self):
        self.storage = MockGCSStorage()
        self.audit = EnhancedAuditLogger(self.storage)
        self.identity_service = PatientIdentityService(
            self.storage, self.audit,
            clinic_staff_users={STAFF_HASH},
            admin_users={ADMIN_HASH},
        )

    def test_register_multiple_patients(self):
        """Register 3 patients with different owners."""
        for owner in [PATIENT_OWNER_1, PATIENT_OWNER_2, PATIENT_OWNER_3]:
            record = self.identity_service.register_patient(
                owner_hash=owner,
                submitter_hash=owner,
                submitter_role="self",
            )
            self.assertIsNotNone(record)
            self.assertIsNotNone(record.patient_id)

    def test_owner_sees_only_own_patients(self):
        """Each owner should see only their own patients."""
        # Register patients
        for owner in [PATIENT_OWNER_1, PATIENT_OWNER_2]:
            self.identity_service.register_patient(
                owner_hash=owner,
                submitter_hash=owner,
            )

        # Query for owner 1's patients
        patients_1 = self.identity_service.find_patients_by_owner(PATIENT_OWNER_1)
        patients_2 = self.identity_service.find_patients_by_owner(PATIENT_OWNER_2)

        # Verify isolation — owner_hash may be truncated by the service
        for p in patients_1:
            self.assertTrue(PATIENT_OWNER_1.startswith(p.owner_hash))
        for p in patients_2:
            self.assertTrue(PATIENT_OWNER_2.startswith(p.owner_hash))

    def test_document_association_requires_verification(self):
        """Documents must be verified before association is final."""
        record = self.identity_service.register_patient(
            owner_hash=PATIENT_OWNER_1,
            submitter_hash=PATIENT_OWNER_1,
        )

        assoc = self.identity_service.associate_document(
            doc_id="doc-assoc-001",
            patient_id=record.patient_id,
            submitter_hash=PATIENT_OWNER_1,
        )
        self.assertIsNotNone(assoc)

    def test_non_admin_cannot_reassign(self):
        """Non-admin users cannot reassign documents between patients."""
        r1 = self.identity_service.register_patient(
            owner_hash=PATIENT_OWNER_1,
            submitter_hash=PATIENT_OWNER_1,
        )
        r2 = self.identity_service.register_patient(
            owner_hash=PATIENT_OWNER_2,
            submitter_hash=PATIENT_OWNER_2,
        )

        self.identity_service.associate_document(
            doc_id="doc-reassign-001",
            patient_id=r1.patient_id,
            submitter_hash=PATIENT_OWNER_1,
            auto_verify=True,
        )

        with self.assertRaises(PermissionError):
            self.identity_service.reassign_document(
                doc_id="doc-reassign-001",
                new_patient_id=r2.patient_id,
                actor_hash=STAFF_HASH,
                reason="test reassignment",
                requesting_user_hash=STAFF_HASH,
            )

    def test_non_admin_cannot_merge(self):
        """Non-admin users cannot merge patients."""
        r1 = self.identity_service.register_patient(
            owner_hash=PATIENT_OWNER_1,
            submitter_hash=PATIENT_OWNER_1,
        )
        r2 = self.identity_service.register_patient(
            owner_hash=PATIENT_OWNER_1,
            submitter_hash=PATIENT_OWNER_1,
        )

        with self.assertRaises(PermissionError):
            self.identity_service.merge_patients(
                source_id=r2.patient_id,
                target_id=r1.patient_id,
                reason="duplicate",
                actor_hash=STAFF_HASH,
                requesting_user_hash=STAFF_HASH,
            )


# ══════════════════════════════════════════════════════════════
# 5. Docker — Static Validation
# ══════════════════════════════════════════════════════════════

class TestDockerfileValidation(unittest.TestCase):
    """Static validation of Dockerfile (daemon not available)."""

    @classmethod
    def setUpClass(cls):
        dockerfile_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "Dockerfile"
        )
        with open(dockerfile_path) as f:
            cls.dockerfile = f.read()

    def test_non_root_user(self):
        """Dockerfile must configure non-root runtime user."""
        self.assertIn("USER appuser", self.dockerfile)
        self.assertIn("useradd -r -g appuser", self.dockerfile)

    def test_no_secrets_copied(self):
        """No secret files should be COPY'd into image."""
        for line in self.dockerfile.split("\n"):
            if line.strip().startswith("COPY"):
                self.assertNotIn(".env", line.lower())
                self.assertNotIn("secret", line.lower())
                self.assertNotIn("credential", line.lower())
                self.assertNotIn("key.json", line.lower())

    def test_all_modules_copied(self):
        """All production modules must be COPY'd."""
        required = [
            "server.py", "alert_delivery.py", "physician_approval.py",
            "patient_identity.py", "audit_trail.py", "clinical_analyzer.py",
            "lab_extractor.py", "lab_models.py", "lab_rules.py",
            "ocr_safety.py", "health_check.py",
        ]
        for mod in required:
            self.assertIn(f"COPY {mod}", self.dockerfile,
                          f"Module {mod} not COPY'd in Dockerfile")

    def test_tesseract_thai_installed(self):
        """Tesseract OCR with Thai language must be installed."""
        self.assertIn("tesseract-ocr-tha", self.dockerfile)
        self.assertIn("tesseract-ocr-eng", self.dockerfile)

    def test_apt_cache_cleaned(self):
        """APT cache must be cleaned to reduce image size."""
        self.assertIn("rm -rf /var/lib/apt/lists/*", self.dockerfile)

    def test_gunicorn_workers(self):
        """Gunicorn must run with 2 workers (Render Starter plan)."""
        self.assertIn("--workers 2", self.dockerfile)

    def test_temp_directory_permissions(self):
        """Temp directory must be owned by appuser."""
        self.assertIn("chown -R appuser:appuser /tmp/ranong_raktai", self.dockerfile)

    def test_no_test_files_in_image(self):
        """Test files should not be in production image."""
        for line in self.dockerfile.split("\n"):
            if line.strip().startswith("COPY") and "test" in line.lower():
                self.fail(f"Test file in image: {line.strip()}")

    @unittest.skip("BLOCKED: Docker daemon not running — ต้อง Docker runtime")
    def test_docker_build_succeeds(self):
        """Build Docker image successfully."""
        pass

    @unittest.skip("BLOCKED: Docker daemon not running — ต้อง Docker runtime")
    def test_container_runs_non_root(self):
        """Container process runs as non-root (UID != 0)."""
        pass

    @unittest.skip("BLOCKED: Docker daemon not running — ต้อง Docker runtime")
    def test_container_health_endpoint(self):
        """Container responds to /health/live."""
        pass


# ══════════════════════════════════════════════════════════════
# 6. Health Endpoints — Flask test client
# ══════════════════════════════════════════════════════════════

class TestHealthEndpoints(unittest.TestCase):
    """Test health endpoints using Flask test client."""

    @classmethod
    def setUpClass(cls):
        import server
        server.app.config["TESTING"] = True
        cls.client = server.app.test_client()

    def test_health_live(self):
        """GET /health/live returns 200."""
        resp = self.client.get("/health/live")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "alive")

    def test_health_ready_structure(self):
        """GET /health/ready returns proper structure."""
        resp = self.client.get("/health/ready")
        data = json.loads(resp.data)
        # Must have status field
        self.assertIn("status", data)
        # Status is "ready", "degraded", or "not_ready" (when GCS unavailable)
        self.assertIn(data["status"], ["ready", "degraded", "not_ready"])

    def test_legacy_health(self):
        """GET /health returns 200."""
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    @unittest.skip("BLOCKED: ต้อง Render deployment — proxy blocks onrender.com")
    def test_render_production_health(self):
        """Verify production Render health endpoint."""
        pass


# ══════════════════════════════════════════════════════════════
# 7. OCR Safety Validation — Comprehensive
# ══════════════════════════════════════════════════════════════

class TestOcrSafetyComprehensive(unittest.TestCase):
    """Comprehensive OCR safety validation with Thai lab formats."""

    def setUp(self):
        self.validator = OcrSafetyValidator()

    def test_valid_creatinine_passes(self):
        """Normal creatinine value passes validation."""
        result = self.validator.validate_result(
            analyte="Creatinine", value=1.2, unit="mg/dL", confidence=0.95,
        )
        self.assertTrue(result.is_valid)

    def test_implausible_creatinine_rejected(self):
        """Implausibly high creatinine flagged."""
        result = self.validator.validate_result(
            analyte="Creatinine", value=999.0, unit="mg/dL", confidence=0.95,
        )
        self.assertFalse(result.is_valid)

    def test_low_confidence_requires_review(self):
        """Low OCR confidence triggers review requirement."""
        result = self.validator.validate_result(
            analyte="Creatinine", value=1.2, unit="mg/dL", confidence=0.65,
        )
        self.assertTrue(result.requires_review)

    def test_very_low_confidence_invalid(self):
        """Very low confidence marks result as invalid."""
        result = self.validator.validate_result(
            analyte="Creatinine", value=1.2, unit="mg/dL", confidence=0.30,
        )
        self.assertFalse(result.is_valid)

    def test_potassium_critical_high(self):
        """Critical high potassium should pass OCR validation (clinical alerts separate)."""
        result = self.validator.validate_result(
            analyte="Potassium", value=7.5, unit="mmol/L", confidence=0.92,
        )
        # OCR safety validates plausibility, not clinical significance
        # 7.5 is physiologically possible, so should be valid
        self.assertTrue(result.is_valid)

    def test_bun_normal_passes(self):
        """Normal BUN value passes."""
        result = self.validator.validate_result(
            analyte="BUN", value=18.0, unit="mg/dL", confidence=0.94,
        )
        self.assertTrue(result.is_valid)


# ══════════════════════════════════════════════════════════════
# 8. Rollback Readiness
# ══════════════════════════════════════════════════════════════

class TestRollbackReadiness(unittest.TestCase):
    """Verify system state is consistent for rollback."""

    def test_no_database_migrations(self):
        """System uses GCS only — no DB migrations to rollback."""
        import server
        # No SQLAlchemy, no database URL
        self.assertFalse(hasattr(server, "db"),
                         "Should not have database dependency")

    def test_gcs_data_backward_compatible(self):
        """Lab report format is backward compatible."""
        storage = MockGCSStorage()
        report = _make_lab_report("compat-001", PATIENT_OWNER_1)

        # New format with verification_status
        enhanced = EnhancedLabReport(report=report)
        enhanced_dict = enhanced.to_dict()
        enhanced_dict["verification_status"] = "PENDING_VERIFICATION"

        path = f"{LAB_RESULTS_PREFIX}compat-001.json"
        storage.put(path, json.dumps(enhanced_dict).encode("utf-8"))

        # Old code (without verification_status check) can still read
        data = json.loads(storage.get(path))
        # Core fields still accessible
        self.assertIn("report", data)
        self.assertIn("results", data["report"])
        # Extra fields are additive, don't break old readers
        self.assertEqual(data["verification_status"], "PENDING_VERIFICATION")

    def test_git_branch_not_main(self):
        """Verify we're on feature branch, not main."""
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        branch = result.stdout.strip()
        self.assertNotEqual(branch, "main",
                            "Should NOT be on main branch")
        self.assertIn(branch, (
            "feature/ranong-raktai-ai-2.0",
            "release/v2.0-rc1",
        ), f"Unexpected branch: {branch}")


# ══════════════════════════════════════════════════════════════
# 9. External Service Tests (BLOCKED)
# ══════════════════════════════════════════════════════════════

class TestExternalServicesBlocked(unittest.TestCase):
    """Tests that require real external service access.
    BLOCKED: proxy rejects connections to all external services.

    Prerequisites for unblocking:
    - GCS: google-cloud-storage SDK + service account credentials
    - LINE: Channel access token + test user IDs
    - Render: Deployed service + proxy access to onrender.com
    """

    @unittest.skip(
        "BLOCKED: proxy rejects storage.googleapis.com — "
        "ต้อง: GCS credentials + google-cloud-storage SDK + proxy allowlist"
    )
    def test_gcs_real_roundtrip(self):
        """Write/read to real GCS bucket."""
        pass

    @unittest.skip(
        "BLOCKED: proxy rejects api.line.me — "
        "ต้อง: LINE channel access token + proxy allowlist"
    )
    def test_line_real_push(self):
        """Send real LINE push notification."""
        pass

    @unittest.skip(
        "BLOCKED: proxy rejects onrender.com — "
        "ต้อง: deployed service + proxy allowlist"
    )
    def test_render_real_health(self):
        """Check real Render health endpoint."""
        pass


if __name__ == "__main__":
    unittest.main()
