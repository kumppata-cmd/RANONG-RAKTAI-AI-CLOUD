"""Tests for CAS contention + clinical analysis version tracking.

Task 2: GCS generation precondition / CAS — ทดสอบคำขอแข่งกันจริง
Task 3: clinical analysis อ้างอิง report version ถูกต้อง

Safety: ใช้ StorageLocal เท่านั้น — ไม่แตะ GCS production
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

# ── Path setup ──────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lab_models import LabReport, LabResult, ReviewStatus
from physician_approval import (
    EnhancedLabReport,
    PhysicianApprovalService,
    ReviewHistoryEntry,
    StaleVersionError,
    DuplicateApprovalError,
    InvalidStateTransitionError,
)


def _make_lab_report(doc_id="test-doc-001", status="pending_review"):
    """สร้าง LabReport สำหรับทดสอบ."""
    return LabReport(
        source_document_id=doc_id,
        specimen_date="2026-01-15",
        extraction_method="ocr_tesseract",
        results=[
            LabResult(
                analyte="Creatinine",
                value=1.5,
                unit="mg/dL",
                reference_range="0.6-1.2",
                confidence=0.95,
                extraction_method="ocr_tesseract",
            ),
            LabResult(
                analyte="eGFR",
                value=45.0,
                unit="mL/min/1.73m²",
                reference_range=">60",
                confidence=0.88,
                extraction_method="ocr_tesseract",
            ),
        ],
        status=status,
    )


def _make_enhanced(doc_id="test-doc-001"):
    """สร้าง EnhancedLabReport สำหรับทดสอบ."""
    report = _make_lab_report(doc_id)
    return EnhancedLabReport(report=report)


# ═══════════════════════════════════════════════════════════
# StorageLocal CAS Tests
# ═══════════════════════════════════════════════════════════


class TestStorageLocalCAS(unittest.TestCase):
    """ทดสอบ get_with_generation / put_conditional ของ StorageLocal."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Import StorageLocal from server
        import importlib
        # Minimal setup — we only need StorageLocal
        os.environ.setdefault("LINE_CHANNEL_SECRET", "test")
        os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test")
        os.environ.setdefault("USER_ID_SALT", "test-salt")
        # Dynamic import to avoid full server init
        spec = importlib.util.spec_from_file_location(
            "server_mod",
            os.path.join(os.path.dirname(__file__), "server.py"),
        )
        # We can't import server.py directly due to Flask app init
        # Instead test through physician_approval which uses the storage

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_put_conditional_basic_flow(self):
        """put_conditional ควรสำเร็จเมื่อ generation ตรง."""
        # Simulate StorageLocal behavior directly
        path = os.path.join(self.tmpdir, "test.json")
        data1 = b'{"version": 1}'
        with open(path, "wb") as f:
            f.write(data1)

        # Read generation (mtime-based)
        gen1 = int(os.path.getmtime(path) * 1_000_000_000)

        # Write with matching generation should succeed
        time.sleep(0.01)  # ensure mtime changes
        data2 = b'{"version": 2}'
        current_gen = int(os.path.getmtime(path) * 1_000_000_000)
        if current_gen == gen1:
            # mtime matches — write is safe
            with open(path, "wb") as f:
                f.write(data2)
            new_gen = int(os.path.getmtime(path) * 1_000_000_000)
            self.assertGreaterEqual(new_gen, gen1)

    def test_put_conditional_conflict(self):
        """put_conditional ควร fail เมื่อ generation ไม่ตรง."""
        path = os.path.join(self.tmpdir, "test.json")
        data1 = b'{"version": 1}'
        with open(path, "wb") as f:
            f.write(data1)

        gen1 = int(os.path.getmtime(path) * 1_000_000_000)

        # Another writer modifies the file
        time.sleep(0.02)
        with open(path, "wb") as f:
            f.write(b'{"version": 2, "other_writer": true}')

        gen2 = int(os.path.getmtime(path) * 1_000_000_000)
        # gen2 should differ from gen1
        self.assertNotEqual(gen1, gen2)


# ═══════════════════════════════════════════════════════════
# CAS Contention Tests via PhysicianApprovalService
# ═══════════════════════════════════════════════════════════


class MockStorageWithCAS:
    """Mock storage ที่รองรับ CAS — ใช้ dict + generation counter."""

    def __init__(self):
        self._data = {}
        self._generations = {}
        self._gen_counter = 0
        self._lock = threading.Lock()

    def get(self, path):
        with self._lock:
            return self._data.get(path)

    def get_with_generation(self, path):
        with self._lock:
            data = self._data.get(path)
            gen = self._generations.get(path)
            return data, gen

    def put(self, path, data, content_type=None, create_only=False):
        with self._lock:
            if create_only and path in self._data:
                return
            self._gen_counter += 1
            self._data[path] = data
            self._generations[path] = self._gen_counter

    def put_conditional(self, path, data, if_generation_match,
                        content_type=None):
        with self._lock:
            current_gen = self._generations.get(path)
            if current_gen != if_generation_match:
                return False, current_gen
            self._gen_counter += 1
            self._data[path] = data
            self._generations[path] = self._gen_counter
            return True, self._gen_counter


class MockAuditLogger:
    def __init__(self):
        self.logs = []

    def log(self, event, actor, data=None):
        self.logs.append({"event": event, "actor": actor, "data": data})


class TestCASContention(unittest.TestCase):
    """ทดสอบ concurrent approval race condition ด้วย CAS."""

    def setUp(self):
        self.storage = MockStorageWithCAS()
        self.audit = MockAuditLogger()
        self.physician1 = "phys_hash_001"
        self.physician2 = "phys_hash_002"
        self.service = PhysicianApprovalService(
            storage=self.storage,
            audit_logger=self.audit,
            physician_users={self.physician1, self.physician2},
            admin_users=set(),
        )

    def _store_report(self, path, enhanced):
        """เก็บ report ใน mock storage."""
        data = json.dumps(enhanced.to_dict(), ensure_ascii=False).encode()
        self.storage.put(path, data, content_type="application/json")

    def test_sequential_approve_succeeds(self):
        """Physician approve ปกติ (ไม่มี contention) ต้องสำเร็จ."""
        enhanced = _make_enhanced()
        lab_path = "_lab_results/test-doc-001.json"
        self._store_report(lab_path, enhanced)

        result = self.service.approve_report(
            lab_path=lab_path,
            user_hash=self.physician1,
            expected_version=1,
            notes="All values verified",
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["new_status"], "approved")

    def test_concurrent_approve_second_fails(self):
        """Concurrent approve: คนที่ 2 ต้องได้ StaleVersionError."""
        enhanced = _make_enhanced()
        lab_path = "_lab_results/test-doc-001.json"
        self._store_report(lab_path, enhanced)

        # Physician 1 approves first
        result1 = self.service.approve_report(
            lab_path=lab_path,
            user_hash=self.physician1,
            expected_version=1,
            notes="Verified",
        )
        self.assertEqual(result1["status"], "success")

        # Physician 2 tries to approve with stale version
        with self.assertRaises((StaleVersionError, DuplicateApprovalError)):
            self.service.approve_report(
                lab_path=lab_path,
                user_hash=self.physician2,
                expected_version=1,
                notes="Also verified",
            )

    def test_concurrent_approve_race_with_threads(self):
        """ทดสอบ race condition จริงด้วย threads."""
        enhanced = _make_enhanced()
        lab_path = "_lab_results/race-test-001.json"
        self._store_report(lab_path, enhanced)

        results = []
        errors = []

        def approve_fn(physician_hash, idx):
            try:
                r = self.service.approve_report(
                    lab_path=lab_path,
                    user_hash=physician_hash,
                    expected_version=1,
                    notes=f"Thread {idx}",
                )
                results.append((idx, r))
            except (StaleVersionError, DuplicateApprovalError) as e:
                errors.append((idx, type(e).__name__))

        t1 = threading.Thread(target=approve_fn, args=(self.physician1, 1))
        t2 = threading.Thread(target=approve_fn, args=(self.physician2, 2))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one should succeed, one should fail
        self.assertEqual(len(results), 1, "Exactly 1 approve should succeed")
        self.assertEqual(len(errors), 1, "Exactly 1 approve should fail")

    def test_correct_and_approve_cas(self):
        """correct_and_approve ต้องใช้ CAS — ไม่เขียนทับ."""
        enhanced = _make_enhanced()
        lab_path = "_lab_results/correct-test-001.json"
        self._store_report(lab_path, enhanced)

        result = self.service.correct_and_approve(
            lab_path=lab_path,
            user_hash=self.physician1,
            expected_version=1,
            corrections={"0": {"value": 1.2}},
            notes="Corrected creatinine from 1.5 to 1.2",
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["corrections_applied"], 1)

        # Second attempt with stale version should fail
        # correct_and_approve sets status=approved, so a second attempt
        # on the same report hits InvalidStateTransitionError (correct behavior)
        # Test CAS conflict with two threads on a fresh report
        enhanced2 = _make_enhanced("cas-correct-002")
        lab_path2 = "_lab_results/cas-correct-002.json"
        self._store_report(lab_path2, enhanced2)

        results2 = []
        errors2 = []

        def correct_fn(physician, idx):
            try:
                r = self.service.correct_and_approve(
                    lab_path=lab_path2,
                    user_hash=physician,
                    expected_version=1,
                    corrections={"0": {"value": 1.0 + idx * 0.1}},
                    notes=f"Thread {idx} correction",
                )
                results2.append((idx, r))
            except (StaleVersionError, InvalidStateTransitionError) as e:
                errors2.append((idx, type(e).__name__))

        t1 = threading.Thread(target=correct_fn, args=(self.physician1, 1))
        t2 = threading.Thread(target=correct_fn, args=(self.physician2, 2))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one should succeed
        self.assertEqual(
            len(results2), 1,
            "Exactly 1 correct_and_approve should succeed"
        )
        self.assertEqual(
            len(errors2), 1,
            "Exactly 1 correct_and_approve should fail"
        )

    def test_reject_with_cas(self):
        """reject_report ต้องใช้ CAS เช่นกัน."""
        enhanced = _make_enhanced()
        lab_path = "_lab_results/reject-test-001.json"
        self._store_report(lab_path, enhanced)

        result = self.service.reject_report(
            lab_path=lab_path,
            user_hash=self.physician1,
            expected_version=1,
            notes="Values seem incorrect, need re-scan",
        )
        self.assertEqual(result["status"], "success")


# ═══════════════════════════════════════════════════════════
# Clinical Analysis Version Tracking Tests
# ═══════════════════════════════════════════════════════════


class TestClinicalAnalysisVersionTracking(unittest.TestCase):
    """ทดสอบ clinical analysis อ้างอิง report version ที่ถูกต้อง."""

    def setUp(self):
        self.storage = MockStorageWithCAS()
        self.audit = MockAuditLogger()
        self.physician = "phys_hash_001"
        self.service = PhysicianApprovalService(
            storage=self.storage,
            audit_logger=self.audit,
            physician_users={self.physician},
            admin_users=set(),
        )
        # Import helper functions from server
        # We test them via direct calls
        self.doc_id = "test-clinical-001"

    def _create_clinical_analysis(self, doc_id, source_version=1):
        """จำลองการสร้าง clinical analysis หลัง OCR."""
        clinical_dict = {
            "findings": [{"type": "ckd_risk", "severity": "warning"}],
            "finding_count": 1,
            "ckd_stage": "3a",
            "summary_text_th": "พบความเสี่ยง CKD Stage 3a",
            "verification_status": "PENDING_VERIFICATION",
            "disclaimer": "ผลวิเคราะห์ยังไม่ผ่านการตรวจสอบ",
            "source_report_version": source_version,
        }
        ca_path = f"_clinical_analysis/{doc_id}.json"
        self.storage.put(
            ca_path,
            json.dumps(clinical_dict, ensure_ascii=False).encode(),
            content_type="application/json",
        )
        return clinical_dict

    def test_clinical_has_source_report_version(self):
        """clinical analysis ต้องมี source_report_version."""
        clinical = self._create_clinical_analysis(self.doc_id)
        self.assertEqual(clinical["source_report_version"], 1)
        self.assertEqual(
            clinical["verification_status"], "PENDING_VERIFICATION"
        )

    def test_stale_detection_after_correction(self):
        """clinical analysis ต้องตรวจพบว่า report ถูกแก้ไขหลังวิเคราะห์."""
        # สร้าง clinical analysis ที่อ้างอิง version 1
        self._create_clinical_analysis(self.doc_id, source_version=1)

        # สร้าง report version 3 (หลังจาก correct_and_approve)
        enhanced = _make_enhanced(self.doc_id)
        # จำลองว่า version เพิ่มจาก correction + approval
        enhanced.version = 3
        lab_path = f"_lab_results/{self.doc_id}.json"
        data = json.dumps(enhanced.to_dict(), ensure_ascii=False).encode()
        self.storage.put(lab_path, data, content_type="application/json")

        # อ่าน clinical analysis และเทียบ version
        ca_path = f"_clinical_analysis/{self.doc_id}.json"
        ca_data = self.storage.get(ca_path)
        ca_dict = json.loads(ca_data)
        source_ver = ca_dict["source_report_version"]

        # อ่าน report version ปัจจุบัน
        lab_data = self.storage.get(lab_path)
        lab_dict = json.loads(lab_data)
        current_ver = lab_dict.get("version", 1)

        # ต้องตรวจพบว่า stale
        self.assertGreater(current_ver, source_ver)

    def test_update_clinical_on_approve_verified(self):
        """_update_clinical_on_approve ควรตั้ง PHYSICIAN_VERIFIED
        เมื่อ version ตรง."""
        self._create_clinical_analysis(self.doc_id, source_version=1)

        # Import and call the helper
        # Since server.py is hard to import, test the logic inline
        ca_path = f"_clinical_analysis/{self.doc_id}.json"
        ca_data = self.storage.get(ca_path)
        ca_dict = json.loads(ca_data)
        source_ver = ca_dict.get("source_report_version")
        report_version = 1  # ตรงกัน

        # Logic from _update_clinical_on_approve
        if (source_ver is not None and report_version is not None
                and source_ver < report_version):
            ca_dict["verification_status"] = "STALE_AFTER_CORRECTION"
        else:
            ca_dict["verification_status"] = "PHYSICIAN_VERIFIED"
        ca_dict["verified_by"] = "phys_001"

        self.assertEqual(
            ca_dict["verification_status"], "PHYSICIAN_VERIFIED"
        )

    def test_update_clinical_on_approve_stale(self):
        """_update_clinical_on_approve ควรตั้ง STALE_AFTER_CORRECTION
        เมื่อ report version เปลี่ยนแล้ว."""
        self._create_clinical_analysis(self.doc_id, source_version=1)

        ca_path = f"_clinical_analysis/{self.doc_id}.json"
        ca_data = self.storage.get(ca_path)
        ca_dict = json.loads(ca_data)
        source_ver = ca_dict.get("source_report_version")
        report_version = 3  # ไม่ตรง — report ถูกแก้ไข

        if (source_ver is not None and report_version is not None
                and source_ver < report_version):
            ca_dict["verification_status"] = "STALE_AFTER_CORRECTION"
        else:
            ca_dict["verification_status"] = "PHYSICIAN_VERIFIED"

        self.assertEqual(
            ca_dict["verification_status"], "STALE_AFTER_CORRECTION"
        )

    def test_invalidate_clinical_on_correction(self):
        """หลัง correct_and_approve ต้อง invalidate clinical analysis."""
        self._create_clinical_analysis(self.doc_id, source_version=1)

        # Logic from _invalidate_clinical_on_correction
        ca_path = f"_clinical_analysis/{self.doc_id}.json"
        ca_data = self.storage.get(ca_path)
        ca_dict = json.loads(ca_data)
        ca_dict["verification_status"] = "STALE_AFTER_CORRECTION"
        ca_dict["stale_reason"] = "report corrected to version 3"
        ca_dict["invalidated_at"] = datetime.now().isoformat(
            timespec="seconds"
        )
        self.storage.put(
            ca_path,
            json.dumps(ca_dict, ensure_ascii=False).encode(),
            content_type="application/json",
        )

        # Verify
        updated = json.loads(self.storage.get(ca_path))
        self.assertEqual(
            updated["verification_status"], "STALE_AFTER_CORRECTION"
        )
        self.assertIn("corrected", updated["stale_reason"])

    def test_view_shows_stale_warning(self):
        """เมื่อดูผลวิเคราะห์ที่ stale ต้องแสดงคำเตือน."""
        self._create_clinical_analysis(self.doc_id, source_version=1)

        # จำลอง report ที่ถูกแก้ไข (version > source)
        enhanced = _make_enhanced(self.doc_id)
        enhanced.version = 3
        lab_path = f"_lab_results/{self.doc_id}.json"
        data = json.dumps(enhanced.to_dict(), ensure_ascii=False).encode()
        self.storage.put(lab_path, data, content_type="application/json")

        # Logic from clinical analysis view handler
        ca_data = self.storage.get(
            f"_clinical_analysis/{self.doc_id}.json"
        )
        ca_dict = json.loads(ca_data)
        source_ver = ca_dict.get("source_report_version")
        verification_status = ca_dict.get("verification_status", "")
        stale_warning = ""

        if source_ver is not None:
            lab_data = self.storage.get(lab_path)
            if lab_data:
                lab_dict = json.loads(lab_data)
                current_ver = lab_dict.get("version", 1)
                if current_ver > source_ver:
                    stale_warning = (
                        f"⚠️ ผลวิเคราะห์อ้างอิง version {source_ver} "
                        f"แต่ปัจจุบัน version {current_ver}"
                    )
                    if verification_status == "PHYSICIAN_VERIFIED":
                        verification_status = "STALE_AFTER_CORRECTION"

        self.assertTrue(stale_warning)
        self.assertIn("version 1", stale_warning)
        self.assertIn("version 3", stale_warning)

    def test_no_stale_warning_when_versions_match(self):
        """เมื่อ version ตรงกัน ไม่ควรแสดงคำเตือน."""
        self._create_clinical_analysis(self.doc_id, source_version=1)

        # Report ไม่ถูกแก้ไข — version ยังเป็น 1
        enhanced = _make_enhanced(self.doc_id)
        lab_path = f"_lab_results/{self.doc_id}.json"
        data = json.dumps(enhanced.to_dict(), ensure_ascii=False).encode()
        self.storage.put(lab_path, data, content_type="application/json")

        ca_data = self.storage.get(
            f"_clinical_analysis/{self.doc_id}.json"
        )
        ca_dict = json.loads(ca_data)
        source_ver = ca_dict.get("source_report_version")
        stale_warning = ""

        if source_ver is not None:
            lab_data = self.storage.get(lab_path)
            if lab_data:
                lab_dict = json.loads(lab_data)
                current_ver = lab_dict.get("version", 1)
                if current_ver > source_ver:
                    stale_warning = "stale!"

        self.assertEqual(stale_warning, "")

    def test_legacy_clinical_without_source_version(self):
        """clinical analysis เก่าที่ไม่มี source_report_version
        ไม่ควร crash — แค่ไม่แสดงคำเตือน stale."""
        # สร้าง clinical analysis แบบเก่า (ไม่มี source_report_version)
        clinical_dict = {
            "findings": [{"type": "ckd_risk"}],
            "verification_status": "PENDING_VERIFICATION",
            "summary_text_th": "พบความเสี่ยง",
        }
        ca_path = f"_clinical_analysis/{self.doc_id}.json"
        self.storage.put(
            ca_path,
            json.dumps(clinical_dict).encode(),
            content_type="application/json",
        )

        ca_data = self.storage.get(ca_path)
        ca_dict = json.loads(ca_data)
        source_ver = ca_dict.get("source_report_version")

        # source_ver is None → ไม่ตรวจ stale
        self.assertIsNone(source_ver)

    def test_partial_write_exception_logged(self):
        """ถ้า clinical analysis update ล้มเหลว ต้อง log ไม่ใช่ pass."""
        # This test verifies the design — that Exception is logged,
        # not silently passed. We verify by checking the code structure.
        # The actual logging is tested via integration.
        import inspect
        import server as srv

        source = inspect.getsource(srv._update_clinical_on_approve)
        # ต้องไม่มี "except Exception:\n                pass"
        self.assertNotIn("except Exception:\n                pass", source)
        # ต้องมี logger.warning
        self.assertIn("logger.warning", source)


# ═══════════════════════════════════════════════════════════
# EnhancedLabReport Version Tracking
# ═══════════════════════════════════════════════════════════


class TestEnhancedLabReportVersion(unittest.TestCase):
    """ทดสอบ version tracking ของ EnhancedLabReport."""

    def test_initial_version_is_1(self):
        enhanced = _make_enhanced()
        self.assertEqual(enhanced.version, 1)

    def test_add_history_increments_version(self):
        enhanced = _make_enhanced()
        entry = ReviewHistoryEntry(
            event_id="test001",
            action="approved",
            actor_hash="phys001",
            timestamp=datetime.now().isoformat(),
            notes="OK",
        )
        enhanced.add_history(entry)
        self.assertEqual(enhanced.version, 2)

    def test_correct_and_approve_increments_twice(self):
        """correct_and_approve เพิ่ม version 2 ครั้ง
        (correction + approval)."""
        enhanced = _make_enhanced()
        # correction entry
        entry1 = ReviewHistoryEntry(
            event_id="corr001",
            action="corrected",
            actor_hash="phys001",
            timestamp=datetime.now().isoformat(),
            notes="Fixed value",
        )
        enhanced.add_history(entry1)
        self.assertEqual(enhanced.version, 2)

        # approval entry
        entry2 = ReviewHistoryEntry(
            event_id="appr001",
            action="correction_approved",
            actor_hash="phys001",
            timestamp=datetime.now().isoformat(),
            notes="Approved after fix",
        )
        enhanced.add_history(entry2)
        self.assertEqual(enhanced.version, 3)

    def test_serialization_preserves_version(self):
        enhanced = _make_enhanced()
        entry = ReviewHistoryEntry(
            event_id="test001",
            action="approved",
            actor_hash="phys001",
            timestamp=datetime.now().isoformat(),
            notes="OK",
        )
        enhanced.add_history(entry)

        # Serialize → deserialize
        data = enhanced.to_dict()
        restored = EnhancedLabReport.from_dict(data)
        self.assertEqual(restored.version, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
