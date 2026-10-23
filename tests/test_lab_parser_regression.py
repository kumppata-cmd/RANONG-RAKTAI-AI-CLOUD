"""
Regression tests for lab_extractor.py parser — Phase 14.10

Tests TABULAR_THAI_RE fix: \\s{2,} -> [^\\S\\n]+ (single-space support)

Coverage:
  1. English colon format
  2. Thai single-space format (production root cause)
  3. Thai colon format
  4. Mixed Thai-English format
  5. Multiline column-split (pypdf)
  6. No-result documents
  7. Duplicate analyte prevention
  8. pending_review status
  9. 3-analyte PDF fixture
 10. Cross-line matching prevention

ห้ามใช้ข้อมูลผู้ป่วยจริง — ใช้เฉพาะ synthetic test data
"""

import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["APP_ENV"] = "testing"

import logging
logging.disable(logging.WARNING)


class TestLabParserRegression(unittest.TestCase):
    """Regression tests for lab extractor parser fix (Phase 14.10)."""

    @classmethod
    def setUpClass(cls):
        from lab_extractor import extract_lab_results_from_text
        from server import extract_pdf_text

        cls.extract = staticmethod(extract_lab_results_from_text)
        cls.extract_pdf = staticmethod(extract_pdf_text)

    def _extract(self, text, doc_id="test-regression"):
        return self.extract(text, doc_id, "regex_pdf_text")

    def _assert_analyte(self, report, name, value, unit):
        """Assert an analyte exists with correct value and unit."""
        by_name = {r.analyte: r for r in report.results}
        self.assertIn(name, by_name, f"{name} not found. Got: {list(by_name.keys())}")
        self.assertAlmostEqual(by_name[name].value, value, places=1,
            msg=f"{name}: expected {value}, got {by_name[name].value}")
        self.assertEqual(by_name[name].unit, unit,
            msg=f"{name}: expected unit '{unit}', got '{by_name[name].unit}'")

    # ──────────────────────────────────────────
    # 1. English colon format
    # ──────────────────────────────────────────

    def test_english_colon_via_strategy2(self):
        """English colon with double-space separators → Strategy 2 (TABULAR_RE)."""
        text = (
            "Lab Report\n"
            "Creatinine  5.2  mg/dL  0.6-1.2\n"
            "Potassium  4.5  mmol/L  3.5-5.0\n"
            "Hemoglobin  10.5  g/dL  12.0-17.0"
        )
        report = self._extract(text)
        self.assertGreaterEqual(len(report.results), 2,
            f"English tabular should extract ≥2 results, got {len(report.results)}")

    def test_english_colon_3_analytes(self):
        """Pure English colon format → MUST extract all 3 (Phase 14.11 fix)."""
        text = (
            "Lab Report\n"
            "Creatinine: 5.2 mg/dL 0.6-1.2\n"
            "Potassium: 4.5 mmol/L 3.5-5.0\n"
            "Hemoglobin: 10.5 g/dL 12.0-17.0"
        )
        report = self._extract(text)
        self.assertEqual(len(report.results), 3,
            f"English colon must extract 3 results, got {len(report.results)}")
        self._assert_analyte(report, "Creatinine", 5.2, "mg/dL")
        self._assert_analyte(report, "Potassium", 4.5, "mmol/L")
        self._assert_analyte(report, "Hemoglobin", 10.5, "g/dL")

    def test_english_colon_single_space(self):
        """English colon with single space → must still extract."""
        text = (
            "Creatinine: 5.2 mg/dL\n"
            "Potassium: 4.5 mmol/L\n"
            "Hemoglobin: 10.5 g/dL"
        )
        report = self._extract(text)
        self.assertEqual(len(report.results), 3,
            f"English colon single-space must extract 3, got {len(report.results)}")

    def test_english_colon_no_cross_line(self):
        """English colon regex must not match across newlines."""
        text = (
            "Patient Name:\n"
            "5.2 mg/dL\n"
            "Test Date:\n"
            "4.5 mmol/L"
        )
        report = self._extract(text)
        # "Patient Name" and "Test Date" are not in allowlist → 0 results
        self.assertEqual(len(report.results), 0,
            f"Cross-line non-analyte should produce 0 results, got {len(report.results)}")

    # ──────────────────────────────────────────
    # 2. Thai single-space format (ROOT CAUSE)
    # ──────────────────────────────────────────

    def test_thai_single_space_3_analytes(self):
        """Thai analyte names + single space before value → MUST extract all 3."""
        text = (
            "ใบรายงานผลตรวจ\n"
            "ครีเอตินิน (Creatinine) 5.2 mg/dL 0.6-1.2\n"
            "โพแทสเซียม (Potassium) 4.5 mmol/L 3.5-5.0\n"
            "ฮีโมโกลบิน (Hemoglobin) 10.5 g/dL 12.0-17.0"
        )
        report = self._extract(text)
        self.assertEqual(len(report.results), 3,
            f"Thai single-space must extract 3 results, got {len(report.results)}")
        self._assert_analyte(report, "Creatinine", 5.2, "mg/dL")
        self._assert_analyte(report, "Potassium", 4.5, "mmol/L")
        self._assert_analyte(report, "Hemoglobin", 10.5, "g/dL")

    def test_thai_only_single_space(self):
        """Thai-only names (no English in parens) + single space."""
        text = (
            "ผลตรวจ\n"
            "ครีเอตินิน 5.2 mg/dL 0.6-1.2\n"
            "โพแทสเซียม 4.5 mmol/L 3.5-5.0\n"
            "ฮีโมโกลบิน 10.5 g/dL 12.0-17.0"
        )
        report = self._extract(text)
        self.assertEqual(len(report.results), 3,
            f"Thai-only single-space must extract 3 results, got {len(report.results)}")

    # ──────────────────────────────────────────
    # 3. Thai colon format
    # ──────────────────────────────────────────

    def test_thai_colon_format(self):
        """Thai names with colon separator: 'ครีเอตินิน: 5.2 mg/dL'."""
        text = (
            "ผลตรวจ\n"
            "ครีเอตินิน: 5.2 mg/dL 0.6-1.2\n"
            "โพแทสเซียม: 4.5 mmol/L 3.5-5.0\n"
            "ฮีโมโกลบิน: 10.5 g/dL 12.0-17.0"
        )
        report = self._extract(text)
        self.assertEqual(len(report.results), 3)
        self._assert_analyte(report, "Creatinine", 5.2, "mg/dL")

    # ──────────────────────────────────────────
    # 4. Mixed Thai-English format
    # ──────────────────────────────────────────

    def test_mixed_thai_english(self):
        """Mix of pure English and Thai(+English) names."""
        text = (
            "Lab Results\n"
            "Creatinine 5.2 mg/dL\n"
            "โพแทสเซียม (Potassium) 4.5 mmol/L\n"
            "Hemoglobin 10.5 g/dL"
        )
        report = self._extract(text)
        self.assertEqual(len(report.results), 3,
            f"Mixed format must extract 3 results, got {len(report.results)}")
        self._assert_analyte(report, "Creatinine", 5.2, "mg/dL")
        self._assert_analyte(report, "Potassium", 4.5, "mmol/L")
        self._assert_analyte(report, "Hemoglobin", 10.5, "g/dL")

    # ──────────────────────────────────────────
    # 5. Multiline column-split (pypdf)
    # ──────────────────────────────────────────

    def test_multiline_column_split(self):
        """pypdf column-split: analyte on one line, value on next."""
        text = (
            "Creatinine\n"
            "5.2\n"
            "mg/dL\n"
            "Potassium\n"
            "4.5\n"
            "mmol/L\n"
            "Hemoglobin\n"
            "10.5\n"
            "g/dL"
        )
        report = self._extract(text)
        self.assertEqual(len(report.results), 3,
            "Column-split should extract all 3 via Strategy 1 per-line")
        self._assert_analyte(report, "Creatinine", 5.2, "mg/dL")
        self._assert_analyte(report, "Potassium", 4.5, "mmol/L")
        self._assert_analyte(report, "Hemoglobin", 10.5, "g/dL")

    # ──────────────────────────────────────────
    # 6. No-result documents
    # ──────────────────────────────────────────

    def test_no_result_header_only(self):
        """Document with no lab data → 0 results, 1 warning."""
        text = "ใบรายงานผลตรวจทางห้องปฏิบัติการ\nวันที่ 15 มกราคม 2569\nรอผลตรวจ"
        report = self._extract(text)
        self.assertEqual(len(report.results), 0)
        self.assertGreaterEqual(len(report.warnings), 1,
            "No-result document should produce at least 1 warning")
        self.assertIn("ไม่พบผลตรวจ", report.warnings[0])

    def test_empty_text(self):
        """Empty text → 0 results, 1 warning."""
        report = self._extract("")
        self.assertEqual(len(report.results), 0)
        self.assertGreaterEqual(len(report.warnings), 1)

    def test_non_lab_text(self):
        """Non-lab medical text → 0 results."""
        text = "ใบนัดหมายพบแพทย์\nนัดพบแพทย์วันที่ 20 มกราคม 2569\nแผนกอายุรกรรม"
        report = self._extract(text)
        self.assertEqual(len(report.results), 0)

    # ──────────────────────────────────────────
    # 7. Duplicate analyte prevention
    # ──────────────────────────────────────────

    def test_no_duplicate_analytes(self):
        """Same analyte appearing twice → only first should be kept."""
        text = (
            "ผลตรวจ\n"
            "ครีเอตินิน (Creatinine) 5.2 mg/dL\n"
            "ครีเอตินิน (Creatinine) 5.2 mg/dL\n"  # duplicate
            "โพแทสเซียม (Potassium) 4.5 mmol/L"
        )
        report = self._extract(text)
        analytes = [r.analyte for r in report.results]
        self.assertEqual(len(analytes), len(set(analytes)),
            f"Duplicate analytes found: {analytes}")

    # ──────────────────────────────────────────
    # 8. pending_review status
    # ──────────────────────────────────────────

    def test_all_results_pending_review(self):
        """All lab reports must have status = 'pending_review'."""
        text = (
            "ผลตรวจ\n"
            "ครีเอตินิน (Creatinine) 5.2 mg/dL\n"
            "โพแทสเซียม (Potassium) 4.5 mmol/L\n"
            "ฮีโมโกลบิน (Hemoglobin) 10.5 g/dL"
        )
        report = self._extract(text)
        self.assertEqual(report.status, "pending_review",
            f"Report status should be 'pending_review', got '{report.status}'")

    def test_pending_review_in_json(self):
        """JSON serialization preserves pending_review status."""
        text = (
            "ผลตรวจ\n"
            "ครีเอตินิน (Creatinine) 5.2 mg/dL"
        )
        report = self._extract(text)
        parsed = json.loads(report.to_json())
        self.assertEqual(parsed["status"], "pending_review")

    # ──────────────────────────────────────────
    # 9. 3-analyte PDF fixture
    # ──────────────────────────────────────────

    def test_3analyte_pdf_fixture(self):
        """TEST-CKD-001-3analytes.pdf → extracts exactly 3 analytes."""
        pdf_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "test_data", "TEST-CKD-001-3analytes.pdf"
        )
        if not os.path.exists(pdf_path):
            self.skipTest("3-analyte fixture not found")

        text = self.extract_pdf(pdf_path)
        self.assertGreaterEqual(len(text), 100,
            f"Extracted text too short: {len(text)} chars")

        report = self._extract(text, "test-ckd-001-3analytes")
        self.assertEqual(len(report.results), 3,
            f"3-analyte PDF must extract 3 results, got {len(report.results)}")
        self._assert_analyte(report, "Creatinine", 5.2, "mg/dL")
        self._assert_analyte(report, "Potassium", 4.5, "mmol/L")
        self._assert_analyte(report, "Hemoglobin", 10.5, "g/dL")

    def test_3analyte_pdf_sha256(self):
        """Verify SHA-256 of 3-analyte fixture file."""
        pdf_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "test_data", "TEST-CKD-001-3analytes.pdf"
        )
        if not os.path.exists(pdf_path):
            self.skipTest("3-analyte fixture not found")

        with open(pdf_path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(sha, "cd51d6e311ed283e5fbab7a531230856386b19778ed607d9c63537d685d7120d",
            f"SHA-256 mismatch — fixture may have been modified. Got: {sha}")

    # ──────────────────────────────────────────
    # 10. Cross-line matching prevention
    # ──────────────────────────────────────────

    def test_no_cross_line_matching(self):
        """Regex must NOT match across newlines (fabricated values)."""
        # If regex could cross lines, it might match "ครีเอตินิน\n5.2"
        # via TABULAR_THAI_RE. That should only happen via Strategy 1 (per-line).
        text = (
            "ผลตรวจ\n"
            "ครีเอตินิน\n"  # Name on its own line
            "5.2 mg/dL\n"   # Value on next line
        )
        report = self._extract(text)
        # Should extract via Strategy 1 (per-line lookahead), NOT via regex cross-line
        for r in report.results:
            # The raw_text should NOT contain a newline
            if r.extraction_method == "regex_pdf_text":
                self.assertNotIn("\n", r.raw_text,
                    f"Cross-line match detected in raw_text: {repr(r.raw_text)}")

    def test_no_false_positive_from_dates(self):
        """Date-like text should NOT be extracted as lab results."""
        text = (
            "ใบรายงานผลตรวจทางห้องปฏิบัติการ\n"
            "วันที่ 15 มกราคม 2569\n"
            "เวลา 10.30 น.\n"
            "ห้อง 5 ชั้น 3"
        )
        report = self._extract(text)
        self.assertEqual(len(report.results), 0,
            f"Date/time should not produce lab results: {[r.analyte for r in report.results]}")

    # ──────────────────────────────────────────
    # 11. Value correctness
    # ──────────────────────────────────────────

    def test_value_correctness_decimals(self):
        """Values with decimals must be parsed accurately."""
        text = "ผลตรวจ\nครีเอตินิน (Creatinine) 5.23 mg/dL"
        report = self._extract(text)
        self.assertEqual(len(report.results), 1)
        self.assertAlmostEqual(report.results[0].value, 5.23, places=2)

    def test_value_correctness_integer(self):
        """Integer values must parse correctly."""
        text = "ผลตรวจ\nบียูเอ็น (BUN) 45 mg/dL"
        report = self._extract(text)
        self.assertEqual(len(report.results), 1)
        self.assertAlmostEqual(report.results[0].value, 45.0, places=0)

    # ──────────────────────────────────────────
    # 12. Regression: Original 18-analyte PDF
    # ──────────────────────────────────────────

    def test_original_18analyte_pdf_no_regression(self):
        """Original TEST-CKD-001.pdf must still extract ≥15 analytes."""
        pdf_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "test_data", "TEST-CKD-001.pdf"
        )
        if not os.path.exists(pdf_path):
            self.skipTest("Original fixture not found")

        text = self.extract_pdf(pdf_path)
        report = self._extract(text, "test-original")
        self.assertGreaterEqual(len(report.results), 15,
            f"Original 18-analyte PDF should extract ≥15, got {len(report.results)}")


class TestE2EStorageRetrievalIsolation(unittest.TestCase):
    """E2E tests for lab storage, retrieval, and owner isolation (Phase 14.11).

    Uses synthetic data only — ห้ามใช้ข้อมูลผู้ป่วยจริง
    """

    @classmethod
    def setUpClass(cls):
        from lab_extractor import extract_lab_results_from_text
        from server import extract_pdf_text, DocumentIndex, StorageLocal
        from line_bot_enhancer import find_latest_lab_doc

        cls.extract = staticmethod(extract_lab_results_from_text)
        cls.extract_pdf = staticmethod(extract_pdf_text)
        cls.DocumentIndex = DocumentIndex
        cls.StorageLocal = StorageLocal
        cls.find_latest_lab_doc = staticmethod(find_latest_lab_doc)

    def test_e2e_pdf_to_lab_json_roundtrip(self):
        """Full roundtrip: PDF → extract text → parse → JSON → deserialize."""
        pdf_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "test_data", "TEST-CKD-001-3analytes.pdf"
        )
        if not os.path.exists(pdf_path):
            self.skipTest("3-analyte fixture not found")

        # Step 1: Extract text from PDF
        text = self.extract_pdf(pdf_path)
        self.assertGreaterEqual(len(text), 100)

        # Step 2: Parse lab results
        report = self.extract(text, "e2e-roundtrip-test", "regex_pdf_text")
        self.assertEqual(len(report.results), 3)
        self.assertEqual(report.status, "pending_review")

        # Step 3: Serialize to JSON
        json_str = report.to_json()
        parsed = json.loads(json_str)

        # Step 4: Verify JSON structure
        self.assertEqual(parsed["status"], "pending_review")
        self.assertEqual(len(parsed["results"]), 3)
        analyte_map = {r["analyte"]: r for r in parsed["results"]}
        self.assertIn("Creatinine", analyte_map)
        self.assertAlmostEqual(analyte_map["Creatinine"]["value"], 5.2)
        self.assertEqual(analyte_map["Creatinine"]["unit"], "mg/dL")
        self.assertIn("Potassium", analyte_map)
        self.assertAlmostEqual(analyte_map["Potassium"]["value"], 4.5)
        self.assertIn("Hemoglobin", analyte_map)
        self.assertAlmostEqual(analyte_map["Hemoglobin"]["value"], 10.5)

    def test_owner_isolation_different_hashes(self):
        """Different owner_hashes must not see each other's documents."""
        storage = self.StorageLocal()
        idx = self.DocumentIndex(storage)
        owner_a = "a" * 32  # synthetic hash
        owner_b = "b" * 32  # synthetic hash

        # Add docs directly to in-memory index (bypass GCS for unit test)
        doc_a1 = {"document_id": "doc-owner-a-1", "owner_hash": owner_a,
                  "category": "lab", "upload_date": "2025-01-01"}
        doc_a2 = {"document_id": "doc-owner-a-2", "owner_hash": owner_a,
                  "category": "lab", "upload_date": "2025-01-02"}
        doc_b1 = {"document_id": "doc-owner-b-1", "owner_hash": owner_b,
                  "category": "lab", "upload_date": "2025-01-01"}
        with idx._lock:
            idx._docs.extend([doc_a1, doc_a2, doc_b1])
            idx._doc_map["doc-owner-a-1"] = doc_a1
            idx._doc_map["doc-owner-a-2"] = doc_a2
            idx._doc_map["doc-owner-b-1"] = doc_b1

        # Owner A should only see their docs
        docs_a = idx.list_by_owner(owner_a)
        doc_ids_a = [d["document_id"] for d in docs_a]
        self.assertIn("doc-owner-a-1", doc_ids_a)
        self.assertIn("doc-owner-a-2", doc_ids_a)
        self.assertNotIn("doc-owner-b-1", doc_ids_a,
            "Owner A must NOT see Owner B's documents")

        # Owner B should only see their doc
        docs_b = idx.list_by_owner(owner_b)
        doc_ids_b = [d["document_id"] for d in docs_b]
        self.assertIn("doc-owner-b-1", doc_ids_b)
        self.assertNotIn("doc-owner-a-1", doc_ids_b,
            "Owner B must NOT see Owner A's documents")
        self.assertNotIn("doc-owner-a-2", doc_ids_b)

    def test_owner_isolation_empty_hash(self):
        """Empty owner_hash must return no documents."""
        storage = self.StorageLocal()
        idx = self.DocumentIndex(storage)
        owner = "c" * 32

        doc_et = {"document_id": "doc-empty-test", "owner_hash": owner,
                  "category": "lab", "upload_date": "2025-01-01"}
        with idx._lock:
            idx._docs.append(doc_et)
            idx._doc_map["doc-empty-test"] = doc_et

        docs = idx.list_by_owner("")
        doc_ids = [d["document_id"] for d in docs]
        self.assertNotIn("doc-empty-test", doc_ids,
            "Empty owner_hash must not return any documents")

    def test_storage_put_get_synthetic(self):
        """Synthetic storage put/get roundtrip for lab JSON."""
        # Build a synthetic lab report
        text = (
            "ผลตรวจ\n"
            "ครีเอตินิน (Creatinine) 5.2 mg/dL 0.6-1.2\n"
            "โพแทสเซียม (Potassium) 4.5 mmol/L 3.5-5.0\n"
            "ฮีโมโกลบิน (Hemoglobin) 10.5 g/dL 12.0-17.0"
        )
        report = self.extract(text, "synthetic-storage-test", "regex_pdf_text")
        json_str = report.to_json()

        # Verify it's valid JSON with correct structure
        parsed = json.loads(json_str)
        self.assertEqual(parsed["status"], "pending_review")
        self.assertEqual(len(parsed["results"]), 3)

        # Verify each result has required fields
        for r in parsed["results"]:
            self.assertIn("analyte", r)
            self.assertIn("value", r)
            self.assertIn("unit", r)
            self.assertIn("confidence", r)
            self.assertIn("extraction_method", r)
            self.assertIsInstance(r["value"], (int, float))
            self.assertGreater(r["confidence"], 0)

    def test_find_latest_lab_with_owner_isolation(self):
        """find_latest_lab_doc must respect owner isolation."""
        storage = self.StorageLocal()
        idx = self.DocumentIndex(storage)
        owner_x = "x" * 32
        owner_y = "y" * 32

        doc_xl = {"document_id": "doc-x-lab-old", "owner_hash": owner_x,
                  "category": "lab", "upload_date": "2025-01-01"}
        doc_xn = {"document_id": "doc-x-nonlab", "owner_hash": owner_x,
                  "category": "prescription", "upload_date": "2025-01-05"}
        doc_yl = {"document_id": "doc-y-lab-new", "owner_hash": owner_y,
                  "category": "lab", "upload_date": "2025-01-03"}
        with idx._lock:
            idx._docs.extend([doc_xl, doc_xn, doc_yl])
            idx._doc_map["doc-x-lab-old"] = doc_xl
            idx._doc_map["doc-x-nonlab"] = doc_xn
            idx._doc_map["doc-y-lab-new"] = doc_yl

        # find_latest_lab_doc for owner X should return X's lab, not Y's
        docs_x = idx.list_by_owner(owner_x)
        latest_x = self.find_latest_lab_doc(docs_x, owner_x)
        self.assertIsNotNone(latest_x, "Owner X should have a lab doc")
        self.assertEqual(latest_x["document_id"], "doc-x-lab-old")

        # find_latest_lab_doc for owner Y should return Y's lab
        docs_y = idx.list_by_owner(owner_y)
        latest_y = self.find_latest_lab_doc(docs_y, owner_y)
        self.assertIsNotNone(latest_y, "Owner Y should have a lab doc")
        self.assertEqual(latest_y["document_id"], "doc-y-lab-new")

    def test_english_colon_e2e_extraction_and_serialize(self):
        """E2E: English colon text → parse → JSON → verify values (Phase 14.11)."""
        text = (
            "Lab Report - Test Patient\n"
            "Date: 2025-01-15\n"
            "Creatinine: 5.2 mg/dL 0.6-1.2\n"
            "Potassium: 4.5 mmol/L 3.5-5.0\n"
            "Hemoglobin: 10.5 g/dL 12.0-17.0"
        )
        report = self.extract(text, "e2e-eng-colon", "regex_pdf_text")
        self.assertEqual(report.status, "pending_review")
        self.assertEqual(len(report.results), 3,
            f"English colon E2E must extract 3, got {len(report.results)}")

        parsed = json.loads(report.to_json())
        analytes = {r["analyte"]: r for r in parsed["results"]}
        self.assertAlmostEqual(analytes["Creatinine"]["value"], 5.2)
        self.assertEqual(analytes["Creatinine"]["unit"], "mg/dL")
        self.assertAlmostEqual(analytes["Potassium"]["value"], 4.5)
        self.assertAlmostEqual(analytes["Hemoglobin"]["value"], 10.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
