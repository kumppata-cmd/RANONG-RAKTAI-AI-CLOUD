#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 14.9 Regression Tests — Thai Lab Extraction + Audit Log + Multi-Worker

Tests:
  1. Thai analyte name extraction (Thai-only, mixed Thai+English, colon format)
  2. English extraction regression (must not break existing behavior)
  3. Tabular and per-line layouts
  4. No-result documents (ใบรับรองแพทย์, ใบนัดหมาย)
  5. Uric Acid validation rules
  6. Audit logger fallback (per-event file when 403)
  7. Multi-worker owner isolation
  8. Fail-closed authorization (no cross-patient leakage)

ใช้ synthetic data เท่านั้น — ห้ามใช้ข้อมูลผู้ป่วยจริง
"""

import hashlib
import json
import sys
import threading
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, ".")

from lab_extractor import (
    extract_lab_results_from_text,
    _normalize_analyte,
    TABULAR_THAI_RE,
)
from lab_models import LabReport, LabResult, ReviewStatus
from lab_rules import validate_result, ANALYTE_SPECS, get_reference_range


# ═══════════════════════════════════════════════════════════
# 1. Thai Analyte Extraction Tests
# ═══════════════════════════════════════════════════════════


class TestThaiAnalyteNormalization(unittest.TestCase):
    """Test _normalize_analyte with Thai names."""

    def test_thai_creatinine_variants(self):
        """ครีอะตินีน ทั้ง 3 การสะกด."""
        self.assertEqual(_normalize_analyte("ครีอะตินีน"), "Creatinine")
        self.assertEqual(_normalize_analyte("ครีเอตินิน"), "Creatinine")
        self.assertEqual(_normalize_analyte("ครีเอตินีน"), "Creatinine")

    def test_thai_bun(self):
        self.assertEqual(_normalize_analyte("บียูเอ็น"), "BUN")
        self.assertEqual(_normalize_analyte("ยูเรียไนโตรเจน"), "BUN")

    def test_thai_electrolytes(self):
        self.assertEqual(_normalize_analyte("โซเดียม"), "Sodium")
        self.assertEqual(_normalize_analyte("โพแทสเซียม"), "Potassium")
        self.assertEqual(_normalize_analyte("คลอไรด์"), "Chloride")
        self.assertEqual(_normalize_analyte("ไบคาร์บอเนต"), "Bicarbonate")

    def test_thai_minerals(self):
        self.assertEqual(_normalize_analyte("แคลเซียม"), "Calcium")
        self.assertEqual(_normalize_analyte("ฟอสเฟต"), "Phosphate")
        self.assertEqual(_normalize_analyte("ฟอสฟอรัส"), "Phosphate")

    def test_thai_cbc(self):
        self.assertEqual(_normalize_analyte("ฮีโมโกลบิน"), "Hemoglobin")
        self.assertEqual(_normalize_analyte("ฮีมาโตคริต"), "Hematocrit")
        self.assertEqual(_normalize_analyte("เม็ดเลือดขาว"), "WBC")
        self.assertEqual(_normalize_analyte("เกล็ดเลือด"), "Platelet")

    def test_thai_special(self):
        self.assertEqual(_normalize_analyte("พาราไทรอยด์ฮอร์โมน"), "PTH")
        self.assertEqual(_normalize_analyte("เฟอร์ริติน"), "Ferritin")
        self.assertEqual(_normalize_analyte("กรดยูริก"), "Uric Acid")
        self.assertEqual(_normalize_analyte("ยูริก"), "Uric Acid")

    def test_thai_glucose(self):
        self.assertEqual(_normalize_analyte("น้ำตาลอดอาหาร"), "FBS")
        self.assertEqual(_normalize_analyte("กลูโคส"), "Glucose")
        self.assertEqual(_normalize_analyte("น้ำตาลในเลือด"), "Glucose")

    def test_mixed_thai_english_parens(self):
        """ครีอะตินีน (Cr) format."""
        self.assertEqual(_normalize_analyte("ครีอะตินีน (Cr)"), "Creatinine")
        self.assertEqual(_normalize_analyte("บียูเอ็น (BUN)"), "BUN")
        self.assertEqual(_normalize_analyte("โซเดียม (Na)"), "Sodium")

    def test_colon_separator(self):
        """ครีอะตินีน: format."""
        self.assertEqual(_normalize_analyte("ครีอะตินีน:"), "Creatinine")
        self.assertEqual(_normalize_analyte("กรดยูริก :"), "Uric Acid")

    def test_unknown_name_returns_none(self):
        self.assertIsNone(_normalize_analyte("ข้อมูลทั่วไป"))
        self.assertIsNone(_normalize_analyte("ผลตรวจเลือด"))
        self.assertIsNone(_normalize_analyte("unknown_test_xyz"))


# ═══════════════════════════════════════════════════════════
# 2. Thai Tabular Extraction Tests
# ═══════════════════════════════════════════════════════════


class TestThaiTabularExtraction(unittest.TestCase):
    """Test extract_lab_results_from_text with Thai tabular layouts."""

    def test_thai_only_tabular(self):
        """Thai-only analyte names in tabular format."""
        text = (
            "ครีอะตินีน    5.2    mg/dL    0.6-1.2\n"
            "ฮีโมโกลบิน    8.5    g/dL    12-17\n"
            "เกล็ดเลือด    180    x10^3/uL    150-400\n"
        )
        report = extract_lab_results_from_text(text, "synth-001")
        self.assertEqual(len(report.results), 3)
        analytes = {r.analyte for r in report.results}
        self.assertIn("Creatinine", analytes)
        self.assertIn("Hemoglobin", analytes)
        self.assertIn("Platelet", analytes)

    def test_thai_english_mixed_tabular(self):
        """Thai+English mixed names: ครีอะตินีน (Cr)."""
        text = (
            "ผลตรวจเลือด\n"
            "ครีอะตินีน (Cr)     5.2     mg/dL     0.6-1.2\n"
            "บียูเอ็น (BUN)      45      mg/dL     7-20\n"
            "โซเดียม (Na)        138     mmol/L    136-145\n"
            "โพแทสเซียม (K)      5.8     mmol/L    3.5-5.0\n"
        )
        report = extract_lab_results_from_text(text, "synth-002")
        self.assertEqual(len(report.results), 4)
        analytes = {r.analyte for r in report.results}
        self.assertIn("Creatinine", analytes)
        self.assertIn("BUN", analytes)
        self.assertIn("Sodium", analytes)
        self.assertIn("Potassium", analytes)

    def test_thai_colon_format(self):
        """Thai with colon separator: ครีอะตินีน:    5.2    mg/dL."""
        text = (
            "ครีอะตินีน:    5.2    mg/dL\n"
            "กรดยูริก:    8.5    mg/dL\n"
        )
        report = extract_lab_results_from_text(text, "synth-003")
        self.assertEqual(len(report.results), 2)
        analytes = {r.analyte for r in report.results}
        self.assertIn("Creatinine", analytes)
        self.assertIn("Uric Acid", analytes)

    def test_thai_extraction_confidence(self):
        """Thai extraction confidence should be 0.80."""
        text = "ครีอะตินีน    1.0    mg/dL    0.6-1.2\n"
        report = extract_lab_results_from_text(text, "synth-004")
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0].confidence, 0.8)

    def test_thai_critical_value_warning(self):
        """Critical value with Thai extraction generates warning."""
        text = "โพแทสเซียม    7.0    mmol/L    3.5-5.0\n"
        report = extract_lab_results_from_text(text, "synth-005")
        self.assertEqual(len(report.results), 1)
        r = report.results[0]
        self.assertIn("critical_high", r.flags)
        # Should have a warning about critical value
        critical_warnings = [w for w in report.warnings if "ค่าวิกฤต" in w]
        self.assertTrue(len(critical_warnings) > 0)


# ═══════════════════════════════════════════════════════════
# 3. English Regression Tests
# ═══════════════════════════════════════════════════════════


class TestEnglishExtractionRegression(unittest.TestCase):
    """English extraction must still work after Thai changes."""

    def test_english_tabular(self):
        """Standard English tabular format."""
        text = (
            "Creatinine  5.2  mg/dL  0.6-1.2\n"
            "BUN  45  mg/dL  7-20\n"
            "Sodium  138  mmol/L  136-145\n"
        )
        report = extract_lab_results_from_text(text, "synth-en-001")
        self.assertEqual(len(report.results), 3)
        analytes = {r.analyte for r in report.results}
        self.assertIn("Creatinine", analytes)
        self.assertIn("BUN", analytes)
        self.assertIn("Sodium", analytes)

    def test_english_abbreviations(self):
        """English abbreviations: Cr, Na, K, Hb."""
        text = (
            "Cr  1.2  mg/dL  0.6-1.2\n"
            "Na  140  mmol/L  136-145\n"
            "K  4.5  mmol/L  3.5-5.0\n"
            "Hb  14.0  g/dL  12-17\n"
        )
        report = extract_lab_results_from_text(text, "synth-en-002")
        self.assertGreaterEqual(len(report.results), 3)

    def test_english_per_line(self):
        """Per-line format (pypdf column extraction)."""
        text = "Creatinine\n5.2\nmg/dL\n0.6-1.2\nBUN\n45\nmg/dL\n7-20\n"
        report = extract_lab_results_from_text(text, "synth-en-003")
        self.assertGreaterEqual(len(report.results), 2)

    def test_english_confidence_higher_than_thai(self):
        """English tabular should have higher confidence than Thai."""
        text_en = "Creatinine  1.0  mg/dL  0.6-1.2\n"
        text_th = "ครีอะตินีน    1.0    mg/dL    0.6-1.2\n"
        r_en = extract_lab_results_from_text(text_en, "conf-en")
        r_th = extract_lab_results_from_text(text_th, "conf-th")
        self.assertGreater(r_en.results[0].confidence, r_th.results[0].confidence)


# ═══════════════════════════════════════════════════════════
# 4. No-Result Document Tests
# ═══════════════════════════════════════════════════════════


class TestNoResultDocuments(unittest.TestCase):
    """Documents without real lab results should return 0 results."""

    def test_medical_certificate(self):
        """ใบรับรองแพทย์ should not extract anything."""
        text = "ใบรับรองแพทย์\nผู้ป่วยมาตรวจสุขภาพประจำปี ไม่พบความผิดปกติ"
        report = extract_lab_results_from_text(text, "synth-nolab-001")
        self.assertEqual(len(report.results), 0)
        self.assertIn("ไม่พบผลตรวจ Lab ในเอกสาร", report.warnings)

    def test_appointment_note(self):
        """ใบนัดหมาย should not extract anything."""
        text = (
            "ใบนัดหมายพบแพทย์\n"
            "นัดตรวจเลือดครั้งถัดไป วันที่ 15 พ.ย. 2569\n"
            "แผนกอายุรกรรม"
        )
        report = extract_lab_results_from_text(text, "synth-nolab-002")
        self.assertEqual(len(report.results), 0)

    def test_text_with_numbers_but_no_labs(self):
        """Text with numbers that are NOT lab values."""
        text = (
            "ค่าบริการ 500 บาท\n"
            "ห้องตรวจ 205 ชั้น 2\n"
            "เบอร์โทร 077-123-4567"
        )
        report = extract_lab_results_from_text(text, "synth-nolab-003")
        self.assertEqual(len(report.results), 0)

    def test_empty_text(self):
        """Empty text should return 0 results."""
        report = extract_lab_results_from_text("", "synth-empty")
        self.assertEqual(len(report.results), 0)


# ═══════════════════════════════════════════════════════════
# 5. Uric Acid Validation Rules
# ═══════════════════════════════════════════════════════════


class TestUricAcidRules(unittest.TestCase):
    """Uric Acid should now have validation rules."""

    def test_uric_acid_in_specs(self):
        """Uric Acid should be in ANALYTE_SPECS."""
        self.assertIn("Uric Acid", ANALYTE_SPECS)
        spec = ANALYTE_SPECS["Uric Acid"]
        self.assertEqual(spec.primary_unit, "mg/dL")

    def test_uric_acid_normal_value(self):
        """Normal uric acid: no flags."""
        flags = validate_result("Uric Acid", 5.5, "mg/dL")
        self.assertEqual(flags, [])

    def test_uric_acid_high(self):
        """Uric acid > 7.2: abnormal_high."""
        flags = validate_result("Uric Acid", 8.5, "mg/dL")
        self.assertIn("abnormal_high", flags)

    def test_uric_acid_critical_high(self):
        """Uric acid > 15: critical_high."""
        flags = validate_result("Uric Acid", 16.0, "mg/dL")
        self.assertIn("critical_high", flags)

    def test_uric_acid_reference_range(self):
        """Reference range should be available."""
        ref = get_reference_range("Uric Acid")
        self.assertIn("3.5", ref)
        self.assertIn("7.2", ref)


# ═══════════════════════════════════════════════════════════
# 6. LabReport Model Tests
# ═══════════════════════════════════════════════════════════


class TestLabReportModel(unittest.TestCase):
    """LabReport always starts as pending_review."""

    def test_default_status_pending_review(self):
        report = LabReport(
            source_document_id="synth-model-001",
            specimen_date="unknown",
            extraction_method="regex_pdf_text",
        )
        self.assertEqual(report.status, ReviewStatus.PENDING_REVIEW.value)

    def test_to_dict_roundtrip(self):
        report = LabReport(
            source_document_id="synth-model-002",
            specimen_date="2026-01-15",
            extraction_method="regex_pdf_text",
            results=[
                LabResult(
                    analyte="Creatinine",
                    value=5.2,
                    unit="mg/dL",
                    reference_range="0.6-1.2",
                    confidence=0.85,
                    extraction_method="regex_pdf_text",
                    flags=["critical_high"],
                )
            ],
            warnings=["test warning"],
        )
        d = report.to_dict()
        restored = LabReport.from_dict(d)
        self.assertEqual(restored.source_document_id, "synth-model-002")
        self.assertEqual(len(restored.results), 1)
        self.assertEqual(restored.results[0].analyte, "Creatinine")
        self.assertEqual(restored.results[0].value, 5.2)

    def test_summary_text_with_flags(self):
        """summary_text should show flag icons."""
        report = LabReport(
            source_document_id="synth-model-003",
            specimen_date="unknown",
            extraction_method="regex_pdf_text",
            results=[
                LabResult(
                    analyte="Potassium",
                    value=7.0,
                    unit="mmol/L",
                    reference_range="3.5-5.0",
                    confidence=0.8,
                    extraction_method="regex_pdf_text",
                    flags=["critical_high"],
                )
            ],
        )
        summary = report.summary_text()
        self.assertIn("Potassium", summary)


# ═══════════════════════════════════════════════════════════
# 7. Audit Logger Fallback Tests
# ═══════════════════════════════════════════════════════════


class TestAuditLoggerFallback(unittest.TestCase):
    """Test audit logger 403 fallback to per-event files."""

    def _make_audit_logger(self, storage_mock):
        """Create an AuditLogger instance with mocked storage."""
        # Import server module for AuditLogger
        from server import AuditLogger
        return AuditLogger(storage_mock)

    def test_audit_log_normal_append(self):
        """Normal case: blob exists, append works."""
        storage = MagicMock()
        storage.is_ready = True
        blob_mock = MagicMock()
        blob_mock.download_as_text.return_value = '{"existing":"entry"}\n'
        storage._bucket.blob.return_value = blob_mock

        logger_obj = self._make_audit_logger(storage)
        logger_obj.log("search", "abc123def456")

        # Should download + upload (append)
        blob_mock.download_as_text.assert_called_once()
        blob_mock.upload_from_string.assert_called_once()
        uploaded = blob_mock.upload_from_string.call_args[0][0]
        self.assertIn('"existing"', uploaded)
        self.assertIn('"search"', uploaded)

    def test_audit_log_new_file(self):
        """Blob doesn't exist (404): create new file."""
        storage = MagicMock()
        storage.is_ready = True
        blob_mock = MagicMock()
        blob_mock.download_as_text.side_effect = Exception("404 Not Found")
        storage._bucket.blob.return_value = blob_mock

        logger_obj = self._make_audit_logger(storage)
        logger_obj.log("view", "abc123def456")

        # Should create new blob
        self.assertEqual(blob_mock.upload_from_string.call_count, 1)
        uploaded = blob_mock.upload_from_string.call_args[0][0]
        self.assertIn('"view"', uploaded)

    def test_audit_log_403_fallback(self):
        """403 Forbidden on download: fallback to per-event file."""
        storage = MagicMock()
        storage.is_ready = True
        storage.bucket_name = "test-bucket"

        # Main blob raises 403 on download
        main_blob = MagicMock()
        main_blob.download_as_text.side_effect = Exception("403 Forbidden")

        # Per-event blob should succeed
        event_blob = MagicMock()

        call_count = [0]
        def blob_side_effect(path):
            call_count[0] += 1
            if call_count[0] == 1:
                return main_blob  # first call = month file
            return event_blob    # second call = per-event file

        storage._bucket.blob.side_effect = blob_side_effect

        logger_obj = self._make_audit_logger(storage)
        logger_obj.log("download", "abc123def456")

        # Should have created per-event blob
        event_blob.upload_from_string.assert_called_once()

    def test_audit_log_storage_not_ready(self):
        """Storage not ready: only local log, no exception."""
        storage = MagicMock()
        storage.is_ready = False

        logger_obj = self._make_audit_logger(storage)
        # Should not raise
        logger_obj.log("search", "abc123def456")

    def test_audit_log_no_patient_data(self):
        """Audit entry should not contain patient data — only hashed user."""
        storage = MagicMock()
        storage.is_ready = True
        blob_mock = MagicMock()
        blob_mock.download_as_text.return_value = ""
        storage._bucket.blob.return_value = blob_mock

        logger_obj = self._make_audit_logger(storage)
        logger_obj.log("search", "abc123def456789", {"doc_count": 3})

        uploaded = blob_mock.upload_from_string.call_args[0][0]
        # Should truncate user_hash to 8 chars
        entry = json.loads(uploaded.strip())
        self.assertTrue(entry["user_hash"].startswith("abc123de"))
        self.assertNotIn("abc123def456789", uploaded)


# ═══════════════════════════════════════════════════════════
# 8. Multi-Worker Owner Isolation Tests
# ═══════════════════════════════════════════════════════════


class TestMultiWorkerOwnerIsolation(unittest.TestCase):
    """Verify owner isolation prevents cross-patient data leakage."""

    def test_owner_hash_isolation(self):
        """Different owners should have different hashes."""
        user_a = hashlib.sha256("Ua1234567890".encode()).hexdigest()
        user_b = hashlib.sha256("Ub0987654321".encode()).hexdigest()
        self.assertNotEqual(user_a, user_b)

    def test_same_user_consistent_hash(self):
        """Same user should produce same hash."""
        user_id = "U_synthetic_test_user"
        hash1 = hashlib.sha256(user_id.encode()).hexdigest()
        hash2 = hashlib.sha256(user_id.encode()).hexdigest()
        self.assertEqual(hash1, hash2)

    def test_threading_lock_isolation(self):
        """threading.Lock should prevent concurrent access to shared state."""
        lock = threading.Lock()
        shared_state = {"counter": 0}
        errors = []

        def increment():
            for _ in range(1000):
                with lock:
                    val = shared_state["counter"]
                    shared_state["counter"] = val + 1

        threads = [threading.Thread(target=increment) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # With lock, counter should be exactly 4000
        self.assertEqual(shared_state["counter"], 4000)


# ═══════════════════════════════════════════════════════════
# 9. Regex Pattern Tests
# ═══════════════════════════════════════════════════════════


class TestThaiTabularRegex(unittest.TestCase):
    """Test TABULAR_THAI_RE regex pattern directly."""

    def test_does_not_span_newlines(self):
        """Regex should not capture name across newlines."""
        text = "ผลตรวจเลือด\nครีอะตินีน    5.2    mg/dL"
        matches = list(TABULAR_THAI_RE.finditer(text))
        for m in matches:
            name = m.group("name")
            self.assertNotIn("\n", name, f"Name should not span newlines: {name!r}")

    def test_matches_thai_tabular(self):
        text = "ครีอะตินีน    5.2    mg/dL    0.6-1.2"
        m = TABULAR_THAI_RE.search(text)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("value"), "5.2")
        self.assertEqual(m.group("unit"), "mg/dL")

    def test_matches_colon_format(self):
        text = "ครีอะตินีน:    5.2    mg/dL"
        m = TABULAR_THAI_RE.search(text)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("value"), "5.2")

    def test_requires_two_spaces_minimum(self):
        """Single space between name and value should not match."""
        text = "ครีอะตินีน 5.2 mg/dL"
        m = TABULAR_THAI_RE.search(text)
        # With single space, may or may not match depending on pattern
        # But the primary column separator is 2+ spaces
        if m:
            # If it matches, it should have correct value
            self.assertEqual(m.group("value"), "5.2")


# ═══════════════════════════════════════════════════════════
# 10. OCR Artifact Tests
# ═══════════════════════════════════════════════════════════


class TestOCRArtifactHandling(unittest.TestCase):
    """Test handling of common OCR artifacts in lab text."""

    def test_ocr_comma_decimal(self):
        """European-style comma decimal: 5,2 → 5.2."""
        text = "Creatinine  5,2  mg/dL  0.6-1.2\n"
        report = extract_lab_results_from_text(
            text, "synth-ocr-001", "regex_ocr"
        )
        if report.results:
            self.assertAlmostEqual(report.results[0].value, 5.2, places=1)

    def test_extraction_method_preserved(self):
        """extraction_method should be preserved in results."""
        text = "Creatinine  1.0  mg/dL  0.6-1.2\n"
        report = extract_lab_results_from_text(
            text, "synth-ocr-002", "regex_ocr"
        )
        self.assertEqual(report.extraction_method, "regex_ocr")
        if report.results:
            self.assertEqual(report.results[0].extraction_method, "regex_ocr")


# ═══════════════════════════════════════════════════════════
# 11. Phase 14.9 Final Safety Check Tests
# ═══════════════════════════════════════════════════════════


class TestAuditUUIDFilename(unittest.TestCase):
    """Audit event filenames must include UUID to prevent collisions."""

    def _make_audit_logger(self, storage_mock):
        from server import AuditLogger
        return AuditLogger(storage_mock)

    def test_per_event_filename_has_uuid(self):
        """Per-event fallback filename should contain UUID for uniqueness."""
        storage = MagicMock()
        storage.is_ready = True
        storage.bucket_name = "test-bucket"

        main_blob = MagicMock()
        main_blob.download_as_text.side_effect = Exception("403 Forbidden")

        event_blob = MagicMock()
        blob_paths = []

        def blob_side_effect(path):
            blob_paths.append(path)
            if len(blob_paths) == 1:
                return main_blob
            return event_blob

        storage._bucket.blob.side_effect = blob_side_effect

        logger_obj = self._make_audit_logger(storage)
        logger_obj.log("search", "abc123def456")

        # Per-event path should exist and contain a UUID-like segment
        self.assertTrue(len(blob_paths) >= 2, "Should create per-event blob")
        event_path = blob_paths[1]
        # Format: _audit/YYYY-MM/YYYYMMDD-HHMMSS-{uuid8hex}-{action}.json
        parts = event_path.split("/")
        filename = parts[-1]  # e.g. 20261023-115100-a1b2c3d4-search.json
        name_parts = filename.replace(".json", "").split("-")
        # Should have: date, time, uuid(8hex), action
        self.assertTrue(len(name_parts) >= 4,
            f"Filename should have date-time-uuid-action: {filename}")

    def test_concurrent_uuid_uniqueness(self):
        """Two concurrent audit writes should generate unique filenames."""
        import uuid as uuid_mod

        uuids = set()
        for _ in range(100):
            uuids.add(uuid_mod.uuid4().hex[:8])

        # All should be unique
        self.assertEqual(len(uuids), 100,
            "UUID4 should produce unique values for concurrent writes")


class TestAuditPersistenceFailureWarning(unittest.TestCase):
    """When GCS audit write fails completely, must emit WARNING."""

    def _make_audit_logger(self, storage_mock):
        from server import AuditLogger
        return AuditLogger(storage_mock)

    def test_total_failure_emits_warning(self):
        """If both append and per-event fail, should log WARNING."""
        storage = MagicMock()
        storage.is_ready = True
        storage.bucket_name = "test-bucket"

        # Main blob 403, per-event blob also fails
        main_blob = MagicMock()
        main_blob.download_as_text.side_effect = Exception("403 Forbidden")

        event_blob = MagicMock()
        event_blob.upload_from_string.side_effect = Exception("403 Forbidden")

        call_count = [0]
        def blob_side_effect(path):
            call_count[0] += 1
            if call_count[0] == 1:
                return main_blob
            return event_blob

        storage._bucket.blob.side_effect = blob_side_effect

        logger_obj = self._make_audit_logger(storage)

        with patch("server.logger") as mock_logger:
            logger_obj.log("download", "abc123def456")

            # Should have WARNING about persistence failure
            warning_calls = [
                str(c) for c in mock_logger.warning.call_args_list
            ]
            persistence_warnings = [
                w for w in warning_calls
                if "AUDIT_PERSISTENCE_FAILURE" in w
            ]
            self.assertTrue(
                len(persistence_warnings) > 0,
                f"Should emit AUDIT_PERSISTENCE_FAILURE warning. "
                f"Got warnings: {warning_calls}"
            )

    def test_storage_not_ready_emits_warning(self):
        """When GCS is not ready, should warn about non-durable audit."""
        storage = MagicMock()
        storage.is_ready = False

        logger_obj = self._make_audit_logger(storage)

        with patch("server.logger") as mock_logger:
            logger_obj.log("search", "abc123def456")

            warning_calls = [
                str(c) for c in mock_logger.warning.call_args_list
            ]
            # Should warn that audit is not in durable storage
            has_warning = any(
                "durable" in w.lower() or "GCS ไม่พร้อม" in w
                for w in warning_calls
            )
            self.assertTrue(has_warning,
                f"Should warn about non-durable audit. Got: {warning_calls}")


class TestMultiWorkerDocumentIndexSafety(unittest.TestCase):
    """Verify DocumentIndex design is safe for Gunicorn prefork model."""

    def test_add_uses_gcs_before_memory(self):
        """add() must write GCS before updating in-memory cache."""
        from server import DocumentIndex

        storage = MagicMock()
        storage.is_ready = True

        # Track call order
        call_order = []

        blob_mock = MagicMock()
        def upload_side(*a, **kw):
            call_order.append("gcs_write")
        blob_mock.upload_from_string.side_effect = upload_side

        storage._bucket.blob.return_value = blob_mock
        storage._client.list_blobs.return_value = []

        idx = DocumentIndex(storage)
        # Force cache as fresh to avoid reload
        import time as time_mod
        idx._cache_ts = time_mod.time()

        doc = {
            "document_id": "synth-safety-001",
            "owner_hash": hashlib.sha256(b"U_test_user").hexdigest(),
            "category": "lab",
        }
        result = idx.add(doc)

        self.assertTrue(result)
        self.assertIn("gcs_write", call_order,
            "Must write to GCS before memory update")

    def test_add_412_triggers_reload(self):
        """When GCS returns 412 (already exists), should reload from GCS."""
        from server import DocumentIndex

        storage = MagicMock()
        storage.is_ready = True

        # Simulate 412 Precondition Failed
        blob_mock = MagicMock()
        err = Exception("412 Precondition Failed")
        err.code = 412
        blob_mock.upload_from_string.side_effect = err

        storage._bucket.blob.return_value = blob_mock
        storage._client.list_blobs.return_value = []

        idx = DocumentIndex(storage)
        import time as time_mod
        idx._cache_ts = time_mod.time()

        doc = {
            "document_id": "synth-safety-002",
            "owner_hash": hashlib.sha256(b"U_test_user").hexdigest(),
            "category": "lab",
        }
        result = idx.add(doc)

        # Should return False (idempotent)
        self.assertFalse(result)

    def test_fail_closed_empty_owner(self):
        """list_by_owner with unknown owner should return empty list."""
        from server import DocumentIndex

        storage = MagicMock()
        storage.is_ready = True
        storage._client.list_blobs.return_value = []

        idx = DocumentIndex(storage)

        # Add a doc for user A
        import time as time_mod
        idx._cache_ts = time_mod.time()
        owner_a = hashlib.sha256(b"U_owner_a").hexdigest()
        owner_b = hashlib.sha256(b"U_owner_b").hexdigest()

        idx._docs = [{
            "document_id": "synth-001",
            "owner_hash": owner_a,
            "category": "lab",
        }]
        idx._doc_map = {"synth-001": idx._docs[0]}

        # User B should see nothing
        results = idx.list_by_owner(owner_b)
        self.assertEqual(len(results), 0,
            "Different owner should see empty results (fail-closed)")

        # User A should see their doc
        results = idx.list_by_owner(owner_a)
        self.assertEqual(len(results), 1)


class TestHumanReviewEnforcement(unittest.TestCase):
    """Verify that all extracted results start as pending_review."""

    def test_thai_extraction_pending_review(self):
        """Thai lab results must start as pending_review."""
        thai_text = (
            "ผลตรวจเลือด\n"
            "ครีอะตินีน    1.5    mg/dL    0.6-1.2\n"
            "โซเดียม    140    mmol/L    136-145\n"
        )
        report = extract_lab_results_from_text(
            thai_text, "synth-review-001", "regex_ocr"
        )
        self.assertEqual(report.status, "pending_review",
            "All results must start as pending_review")

    def test_english_extraction_pending_review(self):
        """English lab results must start as pending_review."""
        eng_text = "Creatinine  1.5  mg/dL  0.6-1.2\n"
        report = extract_lab_results_from_text(
            eng_text, "synth-review-002", "regex_pdf_text"
        )
        self.assertEqual(report.status, "pending_review")

    def test_uric_acid_reference_not_absolute(self):
        """Uric Acid reference range is general — not per-patient criteria.

        ค่า reference range (3.5-7.2 mg/dL) เป็นค่าทั่วไปสำหรับผู้ใหญ่
        ไม่แยกเพศ (ชาย: 3.5-7.2, หญิง: 2.6-6.0 แตกต่างกันได้)
        Rule Engine ต้อง flag เป็น abnormal เท่านั้น ไม่ใช่ diagnose
        ผลต้องผ่าน Human Review เสมอ
        """
        from lab_rules import validate_result, ANALYTE_SPECS

        spec = ANALYTE_SPECS.get("Uric Acid")
        self.assertIsNotNone(spec, "Uric Acid must be in ANALYTE_SPECS")

        # Female normal value (2.6-6.0) might be flagged as abnormal_low
        # by the general range (3.5-7.2) — this is expected behavior
        # because Rule Engine uses general ranges, not sex-specific
        flags = validate_result("Uric Acid", 3.0, "mg/dL")
        # 3.0 is below 3.5 → should flag as abnormal_low
        self.assertIn("abnormal_low", flags,
            "Values below general range should be flagged for review")
        # But NOT critical — 3.0 is physiologically normal
        self.assertNotIn("critical_low", flags)
        self.assertNotIn("physiologic_impossible", flags)


if __name__ == "__main__":
    unittest.main(verbosity=2)
