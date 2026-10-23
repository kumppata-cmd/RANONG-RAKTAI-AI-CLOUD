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


if __name__ == "__main__":
    unittest.main(verbosity=2)
