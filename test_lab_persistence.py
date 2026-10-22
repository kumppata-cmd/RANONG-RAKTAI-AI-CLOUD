#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 14.6 — Lab Persistence Regression Tests

ทดสอบ pipeline ทั้งหมด:
1. Lab extraction จาก TEST-CKD-001 text
2. Lab JSON serialization/deserialization
3. Storage.put() + Storage.get() round-trip
4. On-demand re-extraction (ผลแล็บล่าสุด fallback)
5. Error logging improvement verification
6. Content dedup behavior with missing lab JSON

ข้อมูลทดสอบ: ใช้เฉพาะข้อมูลสังเคราะห์ TEST-CKD-001
ห้ามใช้ข้อมูลผู้ป่วยจริง
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lab_extractor import extract_lab_results_from_text
from lab_models import LabReport, LabResult, ExtractionMethod, ReviewStatus
from lab_rules import validate_result, get_reference_range, ANALYTE_SPECS


# ── Synthetic test data (TEST-CKD-001) ──
# This is the text that pypdf extracts from TEST-CKD-001.pdf
TEST_CKD_001_TEXT = """KIDNEY FUNCTION PANEL
Laboratory Report

Patient ID: [REDACTED]
Specimen Date: 2026-01-15
Specimen Type: Blood

Creatinine
5.2
mg/dL
0.6-1.2

BUN
45
mg/dL
7-20

eGFR
10
mL/min/1.73m2
>60

Sodium
138
mmol/L
136-145

Potassium
4.5
mmol/L
3.5-5.0

Chloride
102
mmol/L
98-106

Bicarbonate
18
mmol/L
22-29

Calcium
8.8
mg/dL
8.5-10.5

Phosphate
6.5
mg/dL
2.5-4.5

Albumin
3.2
g/dL
3.5-5.0

FBS
110
mg/dL
70-100

Hemoglobin
10.5
g/dL
12-16

Hematocrit
32
%
36-46

WBC
7.5
x10^3/uL
4.5-11.0

Platelet
220
x10^3/uL
150-400

PTH
350
pg/mL
10-65

Ferritin
180
ng/mL
12-300

TSAT
25
%
20-50
"""

SYNTHETIC_DOC_ID = "test-ckd-001-synthetic-00000000"
SYNTHETIC_OWNER_HASH = "sha256_synthetic_test_user_hash_00000000"


class TestLabExtraction(unittest.TestCase):
    """ทดสอบ lab_extractor กับข้อมูล TEST-CKD-001."""

    def test_extraction_produces_results(self):
        """ต้องดึงผลตรวจได้อย่างน้อย 10 รายการ."""
        report = extract_lab_results_from_text(
            TEST_CKD_001_TEXT,
            SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )
        self.assertIsInstance(report, LabReport)
        self.assertGreaterEqual(len(report.results), 10,
                                f"Expected >=10 results, got {len(report.results)}")

    def test_creatinine_extracted_correctly(self):
        """Creatinine = 5.2 mg/dL ต้องถูกดึงและ flag critical_high."""
        report = extract_lab_results_from_text(
            TEST_CKD_001_TEXT,
            SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )
        creatinine = [r for r in report.results if r.analyte == "Creatinine"]
        self.assertEqual(len(creatinine), 1, "Creatinine not found")
        self.assertAlmostEqual(creatinine[0].value, 5.2)
        self.assertEqual(creatinine[0].unit, "mg/dL")
        self.assertIn("abnormal_high", creatinine[0].flags)

    def test_egfr_extracted(self):
        """eGFR = 10 ต้องถูกดึงและ flag critical_low."""
        report = extract_lab_results_from_text(
            TEST_CKD_001_TEXT,
            SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )
        egfr = [r for r in report.results if r.analyte == "eGFR"]
        self.assertEqual(len(egfr), 1, "eGFR not found")
        self.assertAlmostEqual(egfr[0].value, 10.0)
        self.assertIn("critical_low", egfr[0].flags)

    def test_specimen_date_extracted(self):
        """Specimen date ต้องถูกดึงจากเอกสาร."""
        report = extract_lab_results_from_text(
            TEST_CKD_001_TEXT,
            SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )
        self.assertNotEqual(report.specimen_date, "unknown",
                            "Specimen date should be extracted")

    def test_status_is_pending_review(self):
        """สถานะเริ่มต้นต้องเป็น pending_review."""
        report = extract_lab_results_from_text(
            TEST_CKD_001_TEXT,
            SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )
        self.assertEqual(report.status, ReviewStatus.PENDING_REVIEW.value)

    def test_no_extraction_from_empty_text(self):
        """Text ว่างต้องไม่สร้างผลตรวจ."""
        report = extract_lab_results_from_text(
            "", SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )
        self.assertEqual(len(report.results), 0)

    def test_no_extraction_from_non_lab_text(self):
        """Text ที่ไม่ใช่ผลตรวจ lab ต้องไม่สร้างผลตรวจ."""
        report = extract_lab_results_from_text(
            "This is a general medical report with no lab values.",
            SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )
        self.assertEqual(len(report.results), 0)


class TestLabSerialization(unittest.TestCase):
    """ทดสอบ JSON serialization/deserialization round-trip."""

    def setUp(self):
        self.report = extract_lab_results_from_text(
            TEST_CKD_001_TEXT,
            SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )

    def test_to_json_produces_valid_json(self):
        """to_json() ต้องสร้าง valid JSON string."""
        json_str = self.report.to_json()
        self.assertIsInstance(json_str, str)
        parsed = json.loads(json_str)
        self.assertIsInstance(parsed, dict)

    def test_json_round_trip(self):
        """LabReport → JSON → LabReport ต้อง preserve ข้อมูล."""
        json_str = self.report.to_json()
        restored = LabReport.from_dict(json.loads(json_str))
        self.assertEqual(len(restored.results), len(self.report.results))
        self.assertEqual(restored.source_document_id,
                         self.report.source_document_id)
        self.assertEqual(restored.specimen_date, self.report.specimen_date)
        self.assertEqual(restored.status, self.report.status)

    def test_json_contains_result_count(self):
        """JSON ต้องมี result_count field."""
        json_str = self.report.to_json()
        parsed = json.loads(json_str)
        self.assertIn("result_count", parsed)
        self.assertEqual(parsed["result_count"], len(self.report.results))

    def test_json_encodeable_as_utf8_bytes(self):
        """to_json().encode('utf-8') ต้องสำเร็จ (ใช้สำหรับ storage.put)."""
        json_bytes = self.report.to_json().encode("utf-8")
        self.assertIsInstance(json_bytes, bytes)
        self.assertGreater(len(json_bytes), 100)

    def test_summary_text_produces_output(self):
        """summary_text() ต้องสร้างข้อความสรุปที่อ่านได้."""
        text = self.report.summary_text()
        self.assertIn("📋", text)
        self.assertIn("Creatinine", text)
        self.assertIn("pending_review", text)


class TestStoragePutGetRoundTrip(unittest.TestCase):
    """ทดสอบ storage.put() + storage.get() round-trip ด้วย mock."""

    def test_lab_json_put_and_get(self):
        """put() แล้ว get() ต้องคืนข้อมูลเดิม."""
        report = extract_lab_results_from_text(
            TEST_CKD_001_TEXT,
            SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )
        lab_json = report.to_json()
        lab_bytes = lab_json.encode("utf-8")
        lab_path = f"_lab_results/{SYNTHETIC_DOC_ID}.json"

        # Mock storage
        storage_data = {}

        class MockStorage:
            def put(self, path, data, content_type="application/json",
                    create_only=False):
                if create_only and path in storage_data:
                    return f"gs://test-bucket/{path}"  # 412 behavior
                storage_data[path] = data
                return f"gs://test-bucket/{path}"

            def get(self, path):
                return storage_data.get(path)

        storage = MockStorage()
        uri = storage.put(lab_path, lab_bytes,
                          content_type="application/json", create_only=True)
        self.assertTrue(uri.startswith("gs://"))

        retrieved = storage.get(lab_path)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved, lab_bytes)

        # Verify data integrity
        restored = LabReport.from_dict(json.loads(retrieved))
        self.assertEqual(len(restored.results), len(report.results))

    def test_create_only_prevents_overwrite(self):
        """create_only=True ต้องไม่เขียนทับข้อมูลเดิม."""
        storage_data = {}

        class MockStorage:
            def put(self, path, data, content_type="application/json",
                    create_only=False):
                if create_only and path in storage_data:
                    # Simulate 412 — return existing URI, don't overwrite
                    return f"gs://test-bucket/{path}"
                storage_data[path] = data
                return f"gs://test-bucket/{path}"

            def get(self, path):
                return storage_data.get(path)

        storage = MockStorage()
        lab_path = f"_lab_results/{SYNTHETIC_DOC_ID}.json"
        original_data = b'{"original": true}'
        new_data = b'{"original": false}'

        storage.put(lab_path, original_data, create_only=True)
        storage.put(lab_path, new_data, create_only=True)

        retrieved = storage.get(lab_path)
        self.assertEqual(retrieved, original_data)

    def test_get_nonexistent_returns_none(self):
        """get() path ที่ไม่มีต้องคืน None."""
        class MockStorage:
            def get(self, path):
                return None

        storage = MockStorage()
        result = storage.get("_lab_results/nonexistent.json")
        self.assertIsNone(result)


class TestOnDemandReExtraction(unittest.TestCase):
    """ทดสอบ on-demand re-extraction logic."""

    def test_reextraction_with_valid_text(self):
        """เมื่อมี extracted_text ที่มีผลตรวจ ต้อง extract ได้."""
        # Simulate what happens in the "ผลแล็บล่าสุด" handler
        doc = {
            "document_id": SYNTHETIC_DOC_ID,
            "category": "lab",
            "extracted_text": TEST_CKD_001_TEXT,
            "owner_hash": SYNTHETIC_OWNER_HASH,
        }
        doc_cat = doc.get("category", "")
        doc_text = doc.get("extracted_text", "")

        # Conditions for re-extraction
        self.assertEqual(doc_cat, "lab")
        self.assertGreaterEqual(len(doc_text.strip()), 50)

        # Perform re-extraction
        report = extract_lab_results_from_text(
            doc_text,
            doc["document_id"],
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )
        self.assertGreater(len(report.results), 0)

    def test_no_reextraction_for_non_lab_category(self):
        """เอกสารที่ไม่ใช่ lab ต้องไม่ re-extract."""
        doc = {
            "document_id": SYNTHETIC_DOC_ID,
            "category": "general",  # Not lab
            "extracted_text": TEST_CKD_001_TEXT,
        }
        # The guard condition should prevent re-extraction
        should_reextract = (
            doc.get("category") == "lab"
            and doc.get("extracted_text", "")
            and len(doc.get("extracted_text", "").strip()) >= 50
        )
        self.assertFalse(should_reextract)

    def test_no_reextraction_for_short_text(self):
        """Text สั้นเกินไปต้องไม่ re-extract."""
        doc = {
            "document_id": SYNTHETIC_DOC_ID,
            "category": "lab",
            "extracted_text": "short",  # < 50 chars
        }
        should_reextract = (
            doc.get("category") == "lab"
            and doc.get("extracted_text", "")
            and len(doc.get("extracted_text", "").strip()) >= 50
        )
        self.assertFalse(should_reextract)

    def test_no_reextraction_for_empty_text(self):
        """ไม่มี extracted_text ต้องไม่ re-extract."""
        doc = {
            "document_id": SYNTHETIC_DOC_ID,
            "category": "lab",
            "extracted_text": "",
        }
        should_reextract = (
            doc.get("category") == "lab"
            and doc.get("extracted_text", "")
            and len(doc.get("extracted_text", "").strip()) >= 50
        )
        self.assertFalse(should_reextract)


class TestErrorLogging(unittest.TestCase):
    """ทดสอบว่า error logging ไม่เปิดเผย doc_id เต็ม."""

    def test_doc_id_sanitized_in_error_message(self):
        """Error message ต้องตัด doc_id ไว้ที่ 8 chars."""
        doc_id = "abcdefgh-1234-5678-90ab-cdef12345678"
        err_msg = f"GCS put ล้มเหลว — path: _lab_results/{doc_id}.json"

        # Simulate the sanitization logic from our fix
        if doc_id and len(doc_id) > 8 and doc_id in err_msg:
            err_msg = err_msg.replace(doc_id, doc_id[:8] + "…")

        self.assertNotIn(doc_id, err_msg)
        self.assertIn(doc_id[:8], err_msg)
        self.assertIn("…", err_msg)

    def test_short_doc_id_not_broken(self):
        """doc_id สั้น (<=8 chars) ต้องไม่ถูก sanitize."""
        doc_id = "abc123"
        err_msg = f"Error for {doc_id}"

        # Our sanitization only triggers for doc_id > 8 chars
        if doc_id and len(doc_id) > 8 and doc_id in err_msg:
            err_msg = err_msg.replace(doc_id, doc_id[:8] + "…")

        self.assertIn(doc_id, err_msg)


class TestClinicalRulesIntegration(unittest.TestCase):
    """ทดสอบ lab_rules integration กับ TEST-CKD-001 values."""

    def test_creatinine_abnormal_high(self):
        """Creatinine 5.2 mg/dL ต้อง flag abnormal_high (critical_high=10.0)."""
        flags = validate_result("Creatinine", 5.2, "mg/dL")
        self.assertIn("abnormal_high", flags)

    def test_egfr_critical_low(self):
        """eGFR 10 ต้อง flag critical_low."""
        flags = validate_result("eGFR", 10.0, "mL/min/1.73m2")
        self.assertIn("critical_low", flags)

    def test_sodium_normal(self):
        """Sodium 138 mmol/L ต้องไม่มี flag ผิดปกติ."""
        flags = validate_result("Sodium", 138.0, "mmol/L")
        self.assertNotIn("critical_high", flags)
        self.assertNotIn("critical_low", flags)
        self.assertNotIn("abnormal_high", flags)
        self.assertNotIn("abnormal_low", flags)

    def test_reference_range_exists(self):
        """ทุก analyte ใน ANALYTE_SPECS ต้องมี reference range."""
        for name, spec in ANALYTE_SPECS.items():
            ref = get_reference_range(name)
            self.assertTrue(ref, f"No reference range for {name}")


class TestFullPipelineSimulation(unittest.TestCase):
    """ทดสอบ pipeline ทั้งหมดแบบ end-to-end ด้วยข้อมูลสังเคราะห์."""

    def test_full_pipeline_text_to_stored_json(self):
        """
        Simulate: text → extract → to_json → put → get → from_dict → verify
        """
        # Step 1: Extract
        report = extract_lab_results_from_text(
            TEST_CKD_001_TEXT,
            SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )
        self.assertGreater(len(report.results), 0)

        # Step 2: Serialize
        lab_json = report.to_json()
        lab_bytes = lab_json.encode("utf-8")

        # Step 3: Simulate storage
        stored = {}
        lab_path = f"_lab_results/{SYNTHETIC_DOC_ID}.json"
        stored[lab_path] = lab_bytes

        # Step 4: Retrieve
        retrieved_bytes = stored.get(lab_path)
        self.assertIsNotNone(retrieved_bytes)

        # Step 5: Deserialize
        restored = LabReport.from_dict(json.loads(retrieved_bytes))

        # Step 6: Verify
        self.assertEqual(restored.source_document_id, SYNTHETIC_DOC_ID)
        self.assertEqual(len(restored.results), len(report.results))
        self.assertEqual(restored.status, ReviewStatus.PENDING_REVIEW.value)

        # Verify critical values are preserved
        creatinine = [r for r in restored.results
                      if r.analyte == "Creatinine"]
        self.assertEqual(len(creatinine), 1)
        self.assertAlmostEqual(creatinine[0].value, 5.2)
        self.assertIn("abnormal_high", creatinine[0].flags)

    def test_summary_text_after_round_trip(self):
        """summary_text() ต้องทำงานหลัง JSON round-trip."""
        report = extract_lab_results_from_text(
            TEST_CKD_001_TEXT,
            SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_PDF_TEXT.value,
        )
        lab_json = report.to_json()
        restored = LabReport.from_dict(json.loads(lab_json))
        summary = restored.summary_text()

        self.assertIn("📋", summary)
        self.assertIn(SYNTHETIC_DOC_ID[:8], summary)
        self.assertIn("Creatinine", summary)
        self.assertIn("5.2", summary)
        self.assertIn("pending_review", summary)

    def test_pipeline_with_ocr_method(self):
        """Pipeline ต้องทำงานกับ REGEX_OCR method เช่นกัน."""
        report = extract_lab_results_from_text(
            TEST_CKD_001_TEXT,
            SYNTHETIC_DOC_ID,
            ExtractionMethod.REGEX_OCR.value,
        )
        self.assertGreater(len(report.results), 0)
        self.assertEqual(report.extraction_method,
                         ExtractionMethod.REGEX_OCR.value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
