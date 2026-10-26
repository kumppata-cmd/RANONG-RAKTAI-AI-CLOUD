#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Integration Tests V2 — RANONG-RAKTAI-AI 2.0 (Task 5: Verify Real Integration)

ทดสอบการทำงานร่วมกันระหว่าง modules หลักของระบบ:
  1. Alert Delivery Integration (with LINE push mock)
  2. Physician Approval Integration
  3. Health/Readiness endpoints
  4. Audit Trail persistence
  5. OCR Safety Validation
  6. Docker/Container (BLOCKED — needs Docker)

กฎ:
  - ใช้ข้อมูล synthetic/demo เท่านั้น — ไม่ใช้ข้อมูลผู้ป่วยจริง
  - ไม่มีการเรียก network จริง (GCS, LINE, Render)
  - ใช้ MockStorage (in-memory) แทน StorageGCS
  - ใช้ unittest เท่านั้น (ไม่ใช้ pytest)
"""

import os
import sys
import json
import time
import uuid
import hashlib
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

# ── Setup test environment before any app imports ──
os.environ.setdefault("LINE_CHANNEL_SECRET", "test_secret_for_unit_test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test_token_for_unit_test")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("ENABLE_AI_CLASSIFY", "false")
os.environ.setdefault("STORAGE_BACKEND", "local")

from alert_delivery import (
    AlertDeliveryService, AlertSeverity, AlertState, CriticalAlert,
    create_line_push_fn, build_alert_recipients_from_env,
    AlertRetryManager, rebuild_dedup_cache, ALERTS_PREFIX,
)
from physician_approval import (
    PhysicianApprovalService, EnhancedLabReport, ReviewHistoryEntry,
    ReviewAction, UnauthorizedPhysicianError, StaleVersionError,
    InvalidStateTransitionError, ReviewNotesRequiredError,
    DuplicateApprovalError,
)
from audit_trail import (
    PersistentAuditTrail, EnhancedAuditLogger, AuditPersistenceError,
    SECURITY_CRITICAL_ACTIONS,
)
from lab_models import LabReport, LabResult, ReviewStatus
from ocr_safety import OcrSafetyValidator, OcrValidationResult, PLAUSIBILITY_RANGES
from health_check import init_health, _check_storage


# ══════════════════════════════════════════════════════════════
# Shared Test Helpers
# ══════════════════════════════════════════════════════════════

class MockStorage:
    """In-memory storage that mimics StorageLocal/StorageGCS interface."""

    def __init__(self, should_fail=False):
        self._data: dict[str, bytes] = {}
        self.should_fail = should_fail

    def put(self, path: str, data: bytes, content_type: str = "",
            create_only: bool = False, **kwargs) -> None:
        if self.should_fail:
            raise IOError("Storage write failed (simulated)")
        if create_only and path in self._data:
            raise FileExistsError(f"Already exists: {path}")
        self._data[path] = data

    def get(self, path: str) -> bytes | None:
        if self.should_fail:
            raise IOError("Storage read failed (simulated)")
        return self._data.get(path)

    def list_files(self, prefix: str = "") -> list[str]:
        if self.should_fail:
            raise IOError("Storage list failed (simulated)")
        return sorted(k for k in self._data if k.startswith(prefix))

    def save(self, path: str, data: bytes) -> None:
        self.put(path, data)

    def clear(self):
        self._data.clear()


def _make_lab_result(analyte="Creatinine", value=1.0, unit="mg/dL",
                     ref="0.6-1.2", confidence=0.95, flags=None):
    """Helper: create a synthetic LabResult."""
    return LabResult(
        analyte=analyte, value=value, unit=unit,
        reference_range=ref, confidence=confidence,
        extraction_method="test", flags=flags or [],
    )


def _make_lab_report(doc_id="demo-v2-001", date="2025-03-15",
                     results=None):
    """Helper: create a synthetic LabReport."""
    if results is None:
        results = [
            _make_lab_result("Creatinine", 1.2, "mg/dL", "0.6-1.2"),
            _make_lab_result("BUN", 18.0, "mg/dL", "7-20"),
            _make_lab_result("Potassium", 4.5, "mmol/L", "3.5-5.0"),
        ]
    return LabReport(
        source_document_id=doc_id, specimen_date=date,
        results=results, extraction_method="test",
    )


def _make_enhanced_report(doc_id="demo-v2-001", status="pending_review",
                          results=None, version=1):
    """Helper: create an EnhancedLabReport stored as JSON."""
    report = _make_lab_report(doc_id=doc_id, results=results)
    report.status = status
    enhanced = EnhancedLabReport(report=report)
    enhanced.version = version
    return enhanced


def _store_enhanced_report(storage, lab_path, enhanced):
    """Helper: save an EnhancedLabReport to mock storage."""
    storage.put(lab_path, enhanced.to_json().encode("utf-8"),
                content_type="application/json")


PHYSICIAN_HASH = hashlib.sha256(b"dr_demo_physician").hexdigest()
ADMIN_HASH = hashlib.sha256(b"admin_demo_user").hexdigest()
NON_PHYSICIAN_HASH = hashlib.sha256(b"random_user").hexdigest()
LAB_RESULTS_PREFIX = "_lab_results/"


# ══════════════════════════════════════════════════════════════
# 1. Alert Delivery Integration
# ══════════════════════════════════════════════════════════════

class TestAlertDeliveryIntegration(unittest.TestCase):
    """Test alert creation, delivery attempt, retry, and escalation
    using a mock push function instead of real LINE API."""

    def setUp(self):
        self.storage = MockStorage()
        self.audit = EnhancedAuditLogger(self.storage)
        self.recipients = {"physician": ["U_demo_doc_001", "U_demo_doc_002"]}
        self.service = AlertDeliveryService(
            self.storage, self.audit,
            clinician_recipients=self.recipients,
            # ทดสอบเรียก attempt_delivery โดยไม่มี send_push_fn
            # production fail-closed โดยค่าเริ่มต้น (CS-4)
            allow_simulated_delivery=True,
        )

    def test_create_alert_and_attempt_delivery_success(self):
        """create_alert -> attempt_delivery with successful mock push."""
        push_calls = []

        def mock_push(user_id, message):
            push_calls.append((user_id, message))
            return True

        alert = self.service.create_alert(
            doc_id="doc-demo-001", owner_hash="owner_hash_demo",
            analyte="Potassium", value=7.5, unit="mmol/L",
            alert_level="critical_high",
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.state, AlertState.QUEUED.value)
        self.assertEqual(alert.severity, AlertSeverity.CRITICAL.value)

        result = self.service.attempt_delivery(alert.alert_id,
                                                send_push_fn=mock_push)
        self.assertTrue(result["success"])
        self.assertEqual(result["state"], AlertState.SENT.value)
        # ส่งถึงแพทย์ "ทุกคน" ที่ตั้งไว้ ไม่ใช่หยุดที่คนแรกที่สำเร็จ
        # (เดิม assert == 1 ซึ่งเป็นการยืนยันพฤติกรรมที่ไม่มีสำรอง —
        #  ผลวิกฤตถึงแพทย์คนเดียว ถ้าคนนั้นไม่ว่างก็ไม่มีใครรู้)
        self.assertEqual(len(push_calls), len(self.recipients["physician"]))
        self.assertEqual(
            [c[0] for c in push_calls], self.recipients["physician"]
        )
        # Notification should NOT contain PHI
        for _uid, _msg in push_calls:
            self.assertNotIn("owner_hash_demo", _msg)

    def test_create_alert_deduplication(self):
        """Duplicate alert for same doc+analyte+level is deduplicated."""
        alert1 = self.service.create_alert(
            doc_id="doc-demo-dup", owner_hash="owner_dup",
            analyte="Potassium", value=7.5, unit="mmol/L",
            alert_level="critical_high",
        )
        self.assertIsNotNone(alert1)

        alert2 = self.service.create_alert(
            doc_id="doc-demo-dup", owner_hash="owner_dup",
            analyte="Potassium", value=7.5, unit="mmol/L",
            alert_level="critical_high",
        )
        self.assertIsNone(alert2, "Duplicate alert should return None")

    def test_delivery_failure_and_retry(self):
        """Failed delivery transitions to delivery_failed, then retry succeeds."""
        # Push function that fails for ALL recipients on first round,
        # then succeeds on second round
        call_round = [0]

        def mock_push_fail_all_then_succeed(user_id, message):
            # attempt_delivery tries recipients in order; it breaks on first
            # success. We need ALL recipients to fail on first round.
            call_round[0] += 1
            # First 2 calls fail (one per recipient), then succeed
            if call_round[0] <= len(self.recipients["physician"]):
                return False
            return True

        alert = self.service.create_alert(
            doc_id="doc-retry-001", owner_hash="owner_retry",
            analyte="Creatinine", value=12.0, unit="mg/dL",
            alert_level="critical_high",
        )

        # First attempt: all recipients fail
        result1 = self.service.attempt_delivery(
            alert.alert_id, send_push_fn=mock_push_fail_all_then_succeed,
        )
        self.assertFalse(result1["success"])
        self.assertEqual(result1["state"], AlertState.DELIVERY_FAILED.value)

        # Second attempt succeeds
        result2 = self.service.attempt_delivery(
            alert.alert_id, send_push_fn=mock_push_fail_all_then_succeed,
        )
        self.assertTrue(result2["success"])
        self.assertEqual(result2["state"], AlertState.SENT.value)

    def test_escalation_after_max_attempts(self):
        """Alert escalates after max delivery attempts."""
        def mock_push_always_fail(user_id, message):
            return False

        alert = self.service.create_alert(
            doc_id="doc-esc-001", owner_hash="owner_esc",
            analyte="Sodium", value=115.0, unit="mEq/L",
            alert_level="critical_low",
        )
        alert.max_delivery_attempts = 2  # Lower for faster test
        self.service._save_alert(alert)

        # Exhaust delivery attempts
        for _ in range(2):
            self.service.attempt_delivery(
                alert.alert_id, send_push_fn=mock_push_always_fail,
            )

        # Reload to check state
        loaded = self.service._load_alert(alert.alert_id)
        self.assertEqual(loaded.state, AlertState.ESCALATED.value)

    def test_low_ocr_confidence_severity(self):
        """Low OCR confidence sets severity to source_verify."""
        alert = self.service.create_alert(
            doc_id="doc-ocr-001", owner_hash="owner_ocr",
            analyte="Hemoglobin", value=3.5, unit="g/dL",
            alert_level="critical_low", ocr_confidence=0.55,
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, AlertSeverity.SOURCE_VERIFY.value)

    def test_dialysis_patient_egfr_urgent_not_critical(self):
        """eGFR<15 in maintenance dialysis patient is urgent, not critical."""
        alert = self.service.create_alert(
            doc_id="doc-dial-001", owner_hash="owner_dial",
            analyte="eGFR", value=8.0, unit="mL/min/1.73m2",
            alert_level="critical_low",
            patient_context={"is_dialysis_patient": True},
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, AlertSeverity.URGENT.value)

    def test_acknowledge_and_resolve(self):
        """Full lifecycle: create -> deliver -> acknowledge -> resolve."""
        alert = self.service.create_alert(
            doc_id="doc-ack-001", owner_hash="owner_ack",
            analyte="Potassium", value=7.8, unit="mmol/L",
            alert_level="critical_high",
        )

        # Deliver (no push fn = test mode success)
        self.service.attempt_delivery(alert.alert_id)

        # Acknowledge
        ack_result = self.service.acknowledge_alert(
            alert.alert_id, PHYSICIAN_HASH,
        )
        self.assertEqual(ack_result["status"], "acknowledged")

        # Resolve
        resolve_result = self.service.resolve_alert(
            alert.alert_id, PHYSICIAN_HASH,
            resolution_notes="Patient treated",
        )
        self.assertEqual(resolve_result["status"], "resolved")


class TestAlertRetryManager(unittest.TestCase):
    """Test AlertRetryManager.check_and_retry logic."""

    def setUp(self):
        self.storage = MockStorage()
        self.audit = EnhancedAuditLogger(self.storage)
        self.recipients = {"physician": ["U_retry_doc_001"]}
        self.service = AlertDeliveryService(
            self.storage, self.audit,
            clinician_recipients=self.recipients,
            # ทดสอบเรียก attempt_delivery โดยไม่มี send_push_fn
            # production fail-closed โดยค่าเริ่มต้น (CS-4)
            allow_simulated_delivery=True,
        )
        self.retry_mgr = AlertRetryManager(self.service)
        # Set retry interval to 0 so tests don't get throttled
        self.retry_mgr._retry_interval = 0

    def test_retry_manager_retries_failed_alerts(self):
        """RetryManager picks up delivery_failed alerts and retries."""
        # Create alert and fail delivery
        alert = self.service.create_alert(
            doc_id="doc-rm-001", owner_hash="owner_rm",
            analyte="Potassium", value=7.2, unit="mmol/L",
            alert_level="critical_high",
        )

        def always_fail(uid, msg):
            return False

        self.service.attempt_delivery(alert.alert_id,
                                       send_push_fn=always_fail)

        # Force timestamp so retry delay is satisfied
        loaded = self.service._load_alert(alert.alert_id)
        old_time = "2020-01-01T00:00:00"
        loaded.created_at = old_time
        loaded.sent_at = ""
        self.service._save_alert(loaded)

        # Now retry with a working push
        def now_works(uid, msg):
            return True

        results = self.retry_mgr.check_and_retry(send_push_fn=now_works)
        self.assertTrue(len(results) > 0)


class TestRebuildDedupCache(unittest.TestCase):
    """Test rebuild_dedup_cache restores state from storage."""

    def test_rebuild_from_storage(self):
        """Dedup cache is rebuilt from persisted alerts on restart."""
        storage = MockStorage()
        audit = EnhancedAuditLogger(storage)
        service = AlertDeliveryService(storage, audit)

        # Create and persist an alert
        alert = CriticalAlert(
            alert_id="test_dedup_001", doc_id="doc-dedup",
            owner_hash="owner_dd", analyte="K", value=7.0,
            unit="mmol/L", alert_level="critical_high",
            state=AlertState.QUEUED.value,
        )
        storage.put(
            f"{ALERTS_PREFIX}{alert.alert_id}.json",
            alert.to_json().encode("utf-8"),
        )

        # Simulate new service instance (empty dedup cache)
        service2 = AlertDeliveryService(storage, audit)
        self.assertEqual(len(service2._dedup_cache), 0)

        count = rebuild_dedup_cache(service2)
        self.assertEqual(count, 1)
        self.assertIn(alert.dedup_key, service2._dedup_cache)


class TestBuildAlertRecipients(unittest.TestCase):
    """Test build_alert_recipients_from_env."""

    # ID ต้องเป็นรูปแบบ LINE จริง (U/C/R + hex 32 ตัว)
    #
    # เดิมเทสต์นี้ใช้ค่าอย่าง "uid_a" ซึ่งผ่านได้เพราะโค้ดไม่ตรวจรูปแบบ
    # เลย — และนั่นคือสาเหตุที่การตั้ง PHYSICIAN_USERS (ซึ่งเก็บ hash)
    # ดูเหมือนตั้งค่าสำเร็จ แต่ push ล้มทุกครั้งแบบเงียบ ๆ
    # เทสต์ที่รับค่าปลอมจึงยืนยันพฤติกรรมที่ทำให้เกิดปัญหา
    GOOD_A = "Uaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    GOOD_B = "Ubbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    GOOD_C = "Ucccccccccccccccccccccccccccccccc"
    GOOD_X = "Udddddddddddddddddddddddddddddddd"
    GOOD_Y = "Ueeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

    def test_from_alert_recipients_env(self):
        """ALERT_RECIPIENTS env var is used first."""
        with patch.dict(os.environ, {
            "ALERT_RECIPIENTS": f"{self.GOOD_A},{self.GOOD_B}",
            "PHYSICIAN_USERS": self.GOOD_C,
        }):
            recipients = build_alert_recipients_from_env()
            self.assertEqual(
                recipients["physician"], [self.GOOD_A, self.GOOD_B],
            )

    def test_fallback_to_physician_users(self):
        """Falls back to PHYSICIAN_USERS when ALERT_RECIPIENTS is empty."""
        with patch.dict(os.environ, {
            "ALERT_RECIPIENTS": "",
            "PHYSICIAN_USERS": f"{self.GOOD_X},{self.GOOD_Y}",
        }):
            recipients = build_alert_recipients_from_env()
            self.assertEqual(
                recipients["physician"], [self.GOOD_X, self.GOOD_Y],
            )

    def test_hashed_values_are_rejected(self):
        """hash จาก PHYSICIAN_USERS push ไม่ได้ — ต้องถูกคัดออก"""
        with patch.dict(os.environ, {
            "ALERT_RECIPIENTS": "",
            "PHYSICIAN_USERS": "e" * 64,
        }):
            self.assertEqual(build_alert_recipients_from_env(), {})

    def test_no_env_vars_set(self):
        """Returns empty dict when neither env var is set."""
        with patch.dict(os.environ, {
            "ALERT_RECIPIENTS": "",
            "PHYSICIAN_USERS": "",
        }):
            recipients = build_alert_recipients_from_env()
            self.assertEqual(recipients, {})


class TestCreateLinePushFn(unittest.TestCase):
    """Test create_line_push_fn creates a valid callable."""

    def test_returns_callable(self):
        """create_line_push_fn returns a function."""
        push_fn = create_line_push_fn("fake_token_for_test")
        self.assertTrue(callable(push_fn))

    def test_rejects_empty_args(self):
        """Push function returns False for empty user_id or message."""
        push_fn = create_line_push_fn("fake_token_for_test")
        self.assertFalse(push_fn("", "test message"))
        self.assertFalse(push_fn("uid", ""))

    @unittest.skip("BLOCKED: requires real LINE API network access")
    def test_actual_line_push(self):
        """Would test real LINE push delivery."""
        pass


# ══════════════════════════════════════════════════════════════
# 2. Physician Approval Integration
# ══════════════════════════════════════════════════════════════

class TestPhysicianApprovalIntegration(unittest.TestCase):
    """Test physician approve/reject workflow with audit trail."""

    def setUp(self):
        self.storage = MockStorage()
        self.audit = EnhancedAuditLogger(self.storage)
        self.service = PhysicianApprovalService(
            self.storage, self.audit,
            physician_users={PHYSICIAN_HASH},
            admin_users={ADMIN_HASH},
        )
        self.lab_path = f"{LAB_RESULTS_PREFIX}demo-v2-001.json"

    def _store_pending_report(self, doc_id="demo-v2-001", version=1):
        enhanced = _make_enhanced_report(doc_id=doc_id, version=version)
        _store_enhanced_report(self.storage, self.lab_path, enhanced)
        return enhanced

    def test_approve_records_audit_trail(self):
        """Approve transitions status and records audit event."""
        self._store_pending_report()

        result = self.service.approve_report(
            self.lab_path, PHYSICIAN_HASH,
            expected_version=1, notes="Verified",
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["new_status"], ReviewStatus.APPROVED.value)

        # Verify audit was recorded in storage
        audit_files = self.storage.list_files("_audit/")
        self.assertTrue(len(audit_files) > 0,
                        "Audit events should be persisted")

    def test_reject_requires_notes_and_records_audit(self):
        """Reject requires notes and records audit event."""
        self._store_pending_report()

        # Missing notes should raise
        with self.assertRaises(ReviewNotesRequiredError):
            self.service.reject_report(
                self.lab_path, PHYSICIAN_HASH,
                expected_version=1, notes="",
            )

        # With notes should succeed
        result = self.service.reject_report(
            self.lab_path, PHYSICIAN_HASH,
            expected_version=1,
            notes="OCR values look incorrect",
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["new_status"], ReviewStatus.REJECTED.value)

    def test_unauthorized_access_rejected(self):
        """Non-physician cannot approve or reject."""
        self._store_pending_report()

        with self.assertRaises(UnauthorizedPhysicianError):
            self.service.approve_report(
                self.lab_path, NON_PHYSICIAN_HASH,
                expected_version=1,
            )

        with self.assertRaises(UnauthorizedPhysicianError):
            self.service.reject_report(
                self.lab_path, NON_PHYSICIAN_HASH,
                expected_version=1, notes="test",
            )

    def test_admin_is_not_physician(self):
        """Admin role does not automatically grant physician privileges."""
        self._store_pending_report()

        with self.assertRaises(UnauthorizedPhysicianError):
            self.service.approve_report(
                self.lab_path, ADMIN_HASH,
                expected_version=1,
            )

    def test_concurrent_approval_version_conflict(self):
        """Stale version raises StaleVersionError."""
        self._store_pending_report(version=1)

        # First approval succeeds
        result = self.service.approve_report(
            self.lab_path, PHYSICIAN_HASH,
            expected_version=1,
        )
        self.assertEqual(result["status"], "success")

        # Re-store as pending but at version 2
        self._store_pending_report(version=2)

        # Try to approve with stale version 1
        with self.assertRaises(StaleVersionError):
            self.service.approve_report(
                self.lab_path, PHYSICIAN_HASH,
                expected_version=1,
            )

    def test_duplicate_approval_rejected(self):
        """Cannot approve an already-approved report."""
        enhanced = _make_enhanced_report(doc_id="demo-v2-001")
        enhanced.report.status = ReviewStatus.APPROVED.value
        _store_enhanced_report(self.storage, self.lab_path, enhanced)

        with self.assertRaises(DuplicateApprovalError):
            self.service.approve_report(
                self.lab_path, PHYSICIAN_HASH,
                expected_version=1,
            )

    def test_review_history_is_immutable_append_only(self):
        """Each action appends to review_history without modifying prior entries."""
        self._store_pending_report()

        # Approve
        self.service.approve_report(
            self.lab_path, PHYSICIAN_HASH,
            expected_version=1, notes="First review",
        )

        # Load and check history
        data = json.loads(self.storage.get(self.lab_path))
        enhanced = EnhancedLabReport.from_dict(data)
        self.assertEqual(len(enhanced.review_history), 1)
        self.assertEqual(enhanced.review_history[0].action,
                         ReviewAction.APPROVED.value)
        self.assertEqual(enhanced.review_history[0].notes, "First review")


# ══════════════════════════════════════════════════════════════
# 3. Health/Readiness Endpoints
# ══════════════════════════════════════════════════════════════

class TestHealthEndpoints(unittest.TestCase):
    """Test /health/live and /health/ready endpoints."""

    def setUp(self):
        from flask import Flask
        from health_check import register_health_routes
        self.app = Flask(__name__)
        self.storage = MockStorage()
        init_health()
        register_health_routes(self.app, storage=self.storage)
        self.client = self.app.test_client()

    def test_liveness_returns_200(self):
        """/health/live returns 200 with 'alive' status."""
        resp = self.client.get("/health/live")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "alive")

    def test_readiness_with_healthy_storage(self):
        """/health/ready returns 200 when storage is available."""
        # Ensure cache is stale so actual check runs
        from health_check import _health_state
        _health_state["last_gcs_check"] = 0
        _health_state["gcs_available"] = None

        resp = self.client.get("/health/ready")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["checks"]["storage"], "ok")

    def test_readiness_with_failed_storage(self):
        """/health/ready returns 503 when storage is unavailable."""
        self.storage.should_fail = True
        from health_check import _health_state
        _health_state["last_gcs_check"] = 0
        _health_state["gcs_available"] = None

        resp = self.client.get("/health/ready")
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["status"], "not_ready")

    def test_check_storage_function(self):
        """_check_storage returns True for working storage, False for broken."""
        from health_check import _health_state
        _health_state["last_gcs_check"] = 0
        _health_state["gcs_available"] = None

        self.assertTrue(_check_storage(self.storage))

        self.storage.should_fail = True
        _health_state["last_gcs_check"] = 0
        _health_state["gcs_available"] = None
        self.assertFalse(_check_storage(self.storage))

    def test_legacy_health_endpoint(self):
        """/health returns same as /health/live."""
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "alive")


# ══════════════════════════════════════════════════════════════
# 4. Audit Trail Persistence
# ══════════════════════════════════════════════════════════════

class TestAuditTrailPersistence(unittest.TestCase):
    """Test audit trail persistence and fail-closed behavior."""

    def test_events_survive_service_restart(self):
        """Audit events written by one instance are readable by another."""
        storage = MockStorage()
        trail1 = PersistentAuditTrail(storage)

        # Write event
        event = trail1.log(
            action="physician_approval",
            actor_hash=PHYSICIAN_HASH,
            details={"doc_id": "demo-001"},
        )
        self.assertIsNotNone(event)

        # "Restart" — new trail instance, same storage
        trail2 = PersistentAuditTrail(storage)
        month = datetime.now().strftime("%Y-%m")
        events = trail2.query_events(month=month)
        self.assertTrue(len(events) > 0)
        found = any(e.get("action") == "physician_approval" for e in events)
        self.assertTrue(found, "Audit event should survive restart")

    def test_fail_closed_for_security_critical_action(self):
        """Security-critical action fails when audit cannot persist."""
        storage = MockStorage(should_fail=True)
        trail = PersistentAuditTrail(storage, fail_closed=True)

        with self.assertRaises(AuditPersistenceError):
            trail.log(
                action="physician_approval",
                actor_hash=PHYSICIAN_HASH,
                details={"doc_id": "demo-fail"},
            )

    def test_non_critical_action_degrades_gracefully(self):
        """Non-critical action returns None when audit fails, does not raise."""
        storage = MockStorage(should_fail=True)
        trail = PersistentAuditTrail(storage, fail_closed=True)

        result = trail.log(
            action="document_viewed",
            actor_hash="viewer_hash",
            details={"doc_id": "demo-view"},
        )
        self.assertIsNone(result)
        self.assertEqual(trail.get_write_failure_count(), 1)

    def test_enhanced_audit_logger_backward_compatible(self):
        """EnhancedAuditLogger provides backward-compatible log() method."""
        storage = MockStorage()
        logger = EnhancedAuditLogger(storage)

        success = logger.log("test_action", "actor_hash",
                             {"detail": "value"})
        self.assertTrue(success)

    def test_audit_sanitizes_phi(self):
        """Audit trail redacts PHI-like fields from details."""
        storage = MockStorage()
        trail = PersistentAuditTrail(storage)

        event = trail.log(
            action="test_sanitize",
            actor_hash="actor_test",
            details={
                "doc_id": "demo-001",
                "patient_name": "SHOULD BE REDACTED",
                "api_key": "SHOULD BE REDACTED",
                "analyte": "Creatinine",
            },
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.details.get("patient_name"), "[REDACTED]")
        self.assertEqual(event.details.get("api_key"), "[REDACTED]")
        self.assertEqual(event.details.get("analyte"), "Creatinine")

    def test_audit_monthly_partitioning(self):
        """Events are stored under monthly partition paths."""
        storage = MockStorage()
        trail = PersistentAuditTrail(storage)

        trail.log("test_partition", "actor", {"key": "value"})

        month = datetime.now().strftime("%Y-%m")
        expected_prefix = f"_audit/{month}/"
        files = storage.list_files(expected_prefix)
        self.assertTrue(len(files) > 0,
                        f"Should have files under {expected_prefix}")


# ══════════════════════════════════════════════════════════════
# 5. OCR Safety Validation
# ══════════════════════════════════════════════════════════════

class TestOcrSafetyValidation(unittest.TestCase):
    """Test OCR extraction validation with synthetic Thai lab data."""

    def setUp(self):
        self.validator = OcrSafetyValidator()

    def test_valid_creatinine_passes(self):
        """Normal creatinine value passes validation."""
        result = self.validator.validate_result(
            analyte="creatinine", value=1.2,
            unit="mg/dL", confidence=0.95,
        )
        self.assertTrue(result.is_valid)
        self.assertFalse(result.requires_review)
        self.assertEqual(len(result.flags), 0)

    def test_implausible_creatinine_flagged(self):
        """Creatinine value outside plausibility range is flagged."""
        result = self.validator.validate_result(
            analyte="creatinine", value=999.0,
            unit="mg/dL", confidence=0.90,
        )
        self.assertFalse(result.is_valid)
        self.assertTrue(result.requires_review)
        self.assertIn("implausible_value", result.flags)

    def test_low_confidence_requires_review(self):
        """Low OCR confidence flags result for review."""
        result = self.validator.validate_result(
            analyte="potassium", value=4.5,
            unit="mmol/L", confidence=0.60,
        )
        self.assertTrue(result.requires_review)
        self.assertIn("low_confidence", result.flags)

    def test_very_low_confidence_invalid(self):
        """Very low confidence (<0.5) marks result as invalid."""
        result = self.validator.validate_result(
            analyte="sodium", value=140.0,
            unit="mEq/L", confidence=0.40,
        )
        self.assertFalse(result.is_valid)

    def test_unit_normalization(self):
        """Unit aliases are normalized correctly."""
        self.assertEqual(self.validator.normalize_unit("mg/dl"), "mg/dL")
        self.assertEqual(self.validator.normalize_unit("meq/l"), "mEq/L")
        self.assertEqual(self.validator.normalize_unit("pg/ml"), "pg/mL")

    def test_validate_report_with_synthetic_thai_data(self):
        """Validate a synthetic Thai lab report with mixed results."""
        results = [
            {"analyte": "creatinine", "value": 2.5, "unit": "mg/dL",
             "confidence": 0.92},
            {"analyte": "potassium", "value": 5.8, "unit": "mmol/L",
             "confidence": 0.88},
            {"analyte": "hemoglobin", "value": 9.5, "unit": "g/dL",
             "confidence": 0.95},
            {"analyte": "sodium", "value": 138.0, "unit": "mEq/L",
             "confidence": 0.91},
        ]
        report_result = self.validator.validate_report(
            results, specimen_date="2025-03-15",
        )
        self.assertIn("validations", report_result)
        self.assertEqual(len(report_result["validations"]), 4)
        # All values are clinically plausible
        for v in report_result["validations"]:
            # validate_report returns OcrValidationResult objects
            if isinstance(v, dict):
                self.assertTrue(v.get("is_valid", True),
                                f"{v.get('analyte')} should be valid")
            else:
                self.assertTrue(v.is_valid,
                                f"{v.analyte} should be valid")

    def test_duplicate_analyte_detection(self):
        """Duplicate analyte entries are flagged."""
        results = [
            {"analyte": "creatinine", "value": 1.2, "unit": "mg/dL",
             "confidence": 0.95},
            {"analyte": "creatinine", "value": 1.5, "unit": "mg/dL",
             "confidence": 0.90},
        ]
        report_result = self.validator.validate_report(results)
        self.assertTrue(report_result["requires_review"])
        self.assertIn("duplicate_analyte", report_result["report_flags"])

    def test_plausibility_ranges_cover_key_analytes(self):
        """All key nephrology analytes have plausibility ranges defined."""
        key_analytes = [
            "creatinine", "bun", "egfr", "potassium", "sodium",
            "hemoglobin", "albumin", "calcium", "phosphate", "pth",
        ]
        for analyte in key_analytes:
            self.assertIn(analyte, PLAUSIBILITY_RANGES,
                          f"Missing plausibility range for {analyte}")

    def test_missing_specimen_date_flagged(self):
        """Missing specimen date generates a report-level flag."""
        results = [
            {"analyte": "creatinine", "value": 1.0, "unit": "mg/dL",
             "confidence": 0.95},
        ]
        report_result = self.validator.validate_report(
            results, specimen_date=None,
        )
        self.assertIn("specimen_date_missing",
                       report_result["report_flags"])


# ══════════════════════════════════════════════════════════════
# 6. Docker/Container Tests (BLOCKED)
# ══════════════════════════════════════════════════════════════

class TestDockerContainer(unittest.TestCase):
    """Docker container validation tests.

    These tests require an actual Docker build environment and are
    BLOCKED in the current test environment (no Docker daemon).
    """

    @unittest.skip("BLOCKED: requires Docker build environment")
    def test_container_runs_as_non_root(self):
        """Verify container process runs as non-root user.

        Would run:
          docker build -t raktai-test .
          docker run --rm raktai-test id -u
        and verify UID != 0.
        """
        pass

    @unittest.skip("BLOCKED: requires Docker build environment")
    def test_container_health_endpoint(self):
        """Verify /health/live responds inside container.

        Would run:
          docker run -d -p 8080:8080 raktai-test
          curl http://localhost:8080/health/live
        and verify 200 response.
        """
        pass

    @unittest.skip("BLOCKED: requires real GCS credentials in container")
    def test_container_gcs_connectivity(self):
        """Verify container can connect to GCS with service account."""
        pass


# ══════════════════════════════════════════════════════════════
# 7. Cross-Module Integration: Alert + Audit + Approval
# ══════════════════════════════════════════════════════════════

class TestCrossModuleIntegration(unittest.TestCase):
    """Test integration across alert delivery, physician approval,
    and audit trail modules using shared storage."""

    def setUp(self):
        self.storage = MockStorage()
        self.audit = EnhancedAuditLogger(self.storage)
        self.alert_service = AlertDeliveryService(
            self.storage, self.audit,
            clinician_recipients={"physician": ["U_cross_001"]},
            # ทดสอบเรียก attempt_delivery โดยไม่มี send_push_fn
            # production fail-closed โดยค่าเริ่มต้น (CS-4)
            allow_simulated_delivery=True,
        )
        self.approval_service = PhysicianApprovalService(
            self.storage, self.audit,
            physician_users={PHYSICIAN_HASH},
            admin_users={ADMIN_HASH},
        )

    def test_alert_then_approve_full_workflow(self):
        """Alert is created, delivered, acknowledged; then lab is approved.
        All actions have audit trail in shared storage."""
        # 1. Create and deliver alert
        alert = self.alert_service.create_alert(
            doc_id="doc-cross-001", owner_hash="owner_cross",
            analyte="Potassium", value=7.0, unit="mmol/L",
            alert_level="critical_high",
        )
        self.assertIsNotNone(alert)
        self.alert_service.attempt_delivery(alert.alert_id)

        # 2. Physician acknowledges alert
        self.alert_service.acknowledge_alert(alert.alert_id, PHYSICIAN_HASH)

        # 3. Physician approves the lab report
        lab_path = f"{LAB_RESULTS_PREFIX}doc-cross-001.json"
        enhanced = _make_enhanced_report(doc_id="doc-cross-001")
        _store_enhanced_report(self.storage, lab_path, enhanced)

        result = self.approval_service.approve_report(
            lab_path, PHYSICIAN_HASH, expected_version=1,
        )
        self.assertEqual(result["status"], "success")

        # 4. Verify audit trail has events from both services
        audit_files = self.storage.list_files("_audit/")
        self.assertTrue(len(audit_files) >= 3,
                        "Should have audit events from alert + approval")

    def test_audit_failure_blocks_approval(self):
        """When audit storage fails, physician approval is blocked
        (fail-closed behavior)."""
        # Use a special storage that only fails on audit writes
        class AuditFailStorage:
            """Storage that fails writes to _audit/ paths only."""
            def __init__(self):
                self._data: dict[str, bytes] = {}

            def put(self, path, data, content_type="", **kw):
                if path.startswith("_audit/"):
                    raise IOError("Audit write failed (simulated)")
                self._data[path] = data

            def get(self, path):
                return self._data.get(path)

            def list_files(self, prefix=""):
                return sorted(k for k in self._data if k.startswith(prefix))

        fail_storage = AuditFailStorage()
        fail_audit = EnhancedAuditLogger(fail_storage)
        fail_service = PhysicianApprovalService(
            fail_storage, fail_audit,
            physician_users={PHYSICIAN_HASH},
            admin_users={ADMIN_HASH},
        )

        lab_path = f"{LAB_RESULTS_PREFIX}doc-failaudit-001.json"
        enhanced = _make_enhanced_report(doc_id="doc-failaudit-001")
        _store_enhanced_report(fail_storage, lab_path, enhanced)

        with self.assertRaises(AuditPersistenceError):
            fail_service.approve_report(
                lab_path, PHYSICIAN_HASH, expected_version=1,
            )


# ══════════════════════════════════════════════════════════════
# 8. Network-Dependent Tests (BLOCKED)
# ══════════════════════════════════════════════════════════════

class TestExternalServiceIntegration(unittest.TestCase):
    """Tests that require real external service connectivity.

    These are BLOCKED because the test environment has no network
    access to GCS, LINE Messaging API, or Render.
    """

    @unittest.skip("BLOCKED: requires real GCS bucket access")
    def test_gcs_storage_put_get_roundtrip(self):
        """Verify StorageGCS can write and read back data."""
        pass

    @unittest.skip("BLOCKED: requires real LINE channel access token")
    def test_line_push_delivery(self):
        """Verify LINE push message reaches test user."""
        pass

    @unittest.skip("BLOCKED: requires Render deployment")
    def test_render_health_endpoint(self):
        """Verify deployed service responds to health check."""
        pass


if __name__ == "__main__":
    unittest.main()
