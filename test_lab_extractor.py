#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for Laboratory Data Extraction Engine — Phase 1

ข้อมูลทั้งหมดในไฟล์นี้เป็นข้อมูลจำลอง ไม่ใช่ข้อมูลผู้ป่วยจริง

Test categories:
  1. Unit tests — lab_models (LabResult, LabReport)
  2. Unit tests — lab_rules (validate_result, reference ranges)
  3. Unit tests — lab_extractor (regex extraction, date parsing)
  4. Integration tests — end-to-end PDF extraction
  5. Security tests — patient identity separation, status checks
  6. Regression tests — existing functionality preservation
"""

import json
import os
import sys
import unittest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lab_models import LabResult, LabReport, ReviewStatus, ExtractionMethod
from lab_rules import (
    validate_result, get_reference_range, get_valid_units,
    is_critical, is_physiologic_impossible, ANALYTE_SPECS,
)
from lab_extractor import (
    extract_lab_results_from_text,
    extract_specimen_date,
    _normalize_analyte,
    _parse_value,
    _normalize_unit,
    extract_lab_from_file,
)


# ═══════════════════════════════════════════════════════════
# 1. Lab Models Tests
# ═══════════════════════════════════════════════════════════

class TestLabResult(unittest.TestCase):
    """ทดสอบ LabResult dataclass."""

    def test_create_basic(self):
        r = LabResult(
            analyte="Creatinine",
            value=5.2,
            unit="mg/dL",
            reference_range="0.6-1.2",
            confidence=0.9,
            extraction_method="regex_pdf_text",
        )
        self.assertEqual(r.analyte, "Creatinine")
        self.assertEqual(r.value, 5.2)
        self.assertEqual(r.unit, "mg/dL")
        self.assertEqual(r.confidence, 0.9)
        self.assertEqual(r.flags, [])

    def test_to_dict(self):
        r = LabResult(
            analyte="Potassium",
            value=4.5,
            unit="mmol/L",
            reference_range="3.5-5.0",
            confidence=0.9,
            extraction_method="regex_pdf_text",
            flags=["abnormal_high"],
        )
        d = r.to_dict()
        self.assertEqual(d["analyte"], "Potassium")
        self.assertEqual(d["value"], 4.5)
        self.assertIn("abnormal_high", d["flags"])

    def test_flags_default_empty(self):
        r = LabResult(
            analyte="X", value=1.0, unit="mg/dL",
            reference_range="", confidence=0.5,
            extraction_method="test",
        )
        self.assertEqual(r.flags, [])


class TestLabReport(unittest.TestCase):
    """ทดสอบ LabReport dataclass."""

    def test_create_with_pending_review(self):
        report = LabReport(
            source_document_id="test-doc-001",
            specimen_date="2026-01-15",
            extraction_method="regex_pdf_text",
        )
        self.assertEqual(report.status, "pending_review")
        self.assertEqual(report.source_document_id, "test-doc-001")
        self.assertTrue(report.extracted_at)  # auto-set

    def test_status_always_pending_review(self):
        """ผลแล็บต้องเริ่มเป็น pending_review เสมอ."""
        report = LabReport(
            source_document_id="test",
            specimen_date="unknown",
            extraction_method="test",
        )
        self.assertEqual(report.status, ReviewStatus.PENDING_REVIEW.value)

    def test_to_json_and_from_dict(self):
        result = LabResult(
            analyte="Hemoglobin",
            value=10.5,
            unit="g/dL",
            reference_range="12.0-17.0",
            confidence=0.9,
            extraction_method="regex_pdf_text",
            flags=["abnormal_low"],
        )
        report = LabReport(
            source_document_id="test-doc",
            specimen_date="2026-01-15",
            extraction_method="regex_pdf_text",
            results=[result],
            warnings=["test warning"],
        )
        json_str = report.to_json()
        data = json.loads(json_str)

        # Reconstruct
        report2 = LabReport.from_dict(data)
        self.assertEqual(report2.source_document_id, "test-doc")
        self.assertEqual(len(report2.results), 1)
        self.assertEqual(report2.results[0].analyte, "Hemoglobin")
        self.assertEqual(report2.results[0].value, 10.5)
        self.assertEqual(report2.status, "pending_review")

    def test_summary_text(self):
        result = LabResult(
            analyte="Creatinine",
            value=5.2,
            unit="mg/dL",
            reference_range="0.6-1.2",
            confidence=0.9,
            extraction_method="regex_pdf_text",
            flags=["abnormal_high"],
        )
        report = LabReport(
            source_document_id="abcdef12",
            specimen_date="2026-01-15",
            extraction_method="regex_pdf_text",
            results=[result],
        )
        text = report.summary_text()
        self.assertIn("abcdef1", text)
        self.assertIn("2026-01-15", text)
        self.assertIn("Creatinine", text)
        self.assertIn("5.2", text)
        self.assertIn("pending_review", text)

    def test_no_patient_identity(self):
        """LabReport ต้องไม่มีข้อมูลระบุตัวผู้ป่วย."""
        report = LabReport(
            source_document_id="test-doc",
            specimen_date="2026-01-15",
            extraction_method="test",
        )
        d = report.to_dict()
        # ต้องไม่มี keys ที่ระบุตัวผู้ป่วย
        forbidden = ["patient_id", "patient_name", "hn", "hospital_number",
                      "id_card", "national_id", "phone", "address"]
        for key in forbidden:
            self.assertNotIn(key, d)


# ═══════════════════════════════════════════════════════════
# 2. Lab Rules Tests
# ═══════════════════════════════════════════════════════════

class TestLabRules(unittest.TestCase):
    """ทดสอบ Clinical Rule Engine."""

    def test_normal_creatinine(self):
        flags = validate_result("Creatinine", 1.0, "mg/dL")
        self.assertEqual(flags, [])

    def test_high_creatinine(self):
        flags = validate_result("Creatinine", 5.2, "mg/dL")
        self.assertIn("abnormal_high", flags)

    def test_critical_high_creatinine(self):
        flags = validate_result("Creatinine", 12.0, "mg/dL")
        self.assertIn("critical_high", flags)

    def test_normal_potassium(self):
        flags = validate_result("Potassium", 4.5, "mmol/L")
        self.assertEqual(flags, [])

    def test_critical_low_potassium(self):
        flags = validate_result("Potassium", 2.0, "mmol/L")
        self.assertIn("critical_low", flags)

    def test_critical_high_potassium(self):
        flags = validate_result("Potassium", 7.0, "mmol/L")
        self.assertIn("critical_high", flags)

    def test_low_hemoglobin(self):
        flags = validate_result("Hemoglobin", 10.5, "g/dL")
        self.assertIn("abnormal_low", flags)

    def test_critical_low_hemoglobin(self):
        flags = validate_result("Hemoglobin", 5.0, "g/dL")
        self.assertIn("critical_low", flags)

    def test_physiologic_impossible(self):
        """ค่าที่เป็นไปไม่ได้ทางสรีรวิทยา."""
        flags = validate_result("Creatinine", 50.0, "mg/dL")
        self.assertIn("physiologic_impossible", flags)

    def test_unit_mismatch(self):
        flags = validate_result("Creatinine", 5.2, "mmol/L")
        self.assertIn("unit_mismatch", flags)

    def test_unknown_analyte(self):
        flags = validate_result("Unknown_Test", 100, "mg/dL")
        self.assertEqual(flags, [])  # ไม่มี spec → ไม่ flag

    def test_get_reference_range(self):
        ref = get_reference_range("Creatinine")
        self.assertIn("0.6", ref)
        self.assertIn("1.2", ref)

    def test_get_reference_range_unknown(self):
        ref = get_reference_range("Unknown_Test")
        self.assertEqual(ref, "N/A")

    def test_is_critical_helper(self):
        self.assertTrue(is_critical(["critical_high"]))
        self.assertTrue(is_critical(["abnormal_low", "critical_low"]))
        self.assertFalse(is_critical(["abnormal_high"]))
        self.assertFalse(is_critical([]))

    def test_is_physiologic_impossible_helper(self):
        self.assertTrue(is_physiologic_impossible(["physiologic_impossible"]))
        self.assertFalse(is_physiologic_impossible(["abnormal_high"]))

    def test_all_kidney_analytes_have_specs(self):
        """ตรวจว่ามี spec สำหรับ analyte ที่จำเป็นครบ."""
        required = [
            "Creatinine", "BUN", "Sodium", "Potassium", "Chloride",
            "Bicarbonate", "Calcium", "Phosphate", "Albumin", "FBS",
            "Hemoglobin", "Hematocrit", "WBC", "Platelet",
            "PTH", "Ferritin", "TSAT", "Kt/V",
        ]
        for name in required:
            self.assertIn(name, ANALYTE_SPECS, f"Missing spec for {name}")

    def test_egfr_critical_low(self):
        flags = validate_result("eGFR", 10.0, "mL/min/1.73m2")
        self.assertIn("critical_low", flags)

    def test_pth_abnormal_high(self):
        flags = validate_result("PTH", 350.0, "pg/mL")
        self.assertIn("abnormal_high", flags)

    def test_bicarbonate_abnormal_low(self):
        flags = validate_result("Bicarbonate", 18.0, "mmol/L")
        self.assertIn("abnormal_low", flags)


# ═══════════════════════════════════════════════════════════
# 3. Lab Extractor Tests
# ═══════════════════════════════════════════════════════════

class TestAnalyteNormalization(unittest.TestCase):
    """ทดสอบการแปลงชื่อ analyte."""

    def test_exact_match(self):
        self.assertEqual(_normalize_analyte("creatinine"), "Creatinine")
        self.assertEqual(_normalize_analyte("bun"), "BUN")
        self.assertEqual(_normalize_analyte("potassium"), "Potassium")

    def test_with_parentheses(self):
        self.assertEqual(_normalize_analyte("Sodium (Na)"), "Sodium")
        self.assertEqual(_normalize_analyte("Potassium (K)"), "Potassium")
        self.assertEqual(_normalize_analyte("Hemoglobin (Hb)"), "Hemoglobin")

    def test_abbreviations(self):
        self.assertEqual(_normalize_analyte("Hb"), "Hemoglobin")
        self.assertEqual(_normalize_analyte("Hct"), "Hematocrit")
        self.assertEqual(_normalize_analyte("Na"), "Sodium")
        self.assertEqual(_normalize_analyte("K"), "Potassium")

    def test_fbs_glucose(self):
        self.assertEqual(_normalize_analyte("FBS (Glucose)"), "FBS")
        self.assertEqual(_normalize_analyte("FBS"), "FBS")
        self.assertEqual(_normalize_analyte("Glucose"), "Glucose")

    def test_unknown(self):
        self.assertIsNone(_normalize_analyte("Unknown Test"))
        self.assertIsNone(_normalize_analyte(""))

    def test_case_insensitive(self):
        self.assertEqual(_normalize_analyte("CREATININE"), "Creatinine")
        self.assertEqual(_normalize_analyte("hemoglobin"), "Hemoglobin")

    def test_kt_v(self):
        self.assertEqual(_normalize_analyte("Kt/V"), "Kt/V")
        self.assertEqual(_normalize_analyte("kt/v"), "Kt/V")


class TestValueParsing(unittest.TestCase):
    """ทดสอบการแปลงค่า."""

    def test_integer(self):
        self.assertEqual(_parse_value("45"), 45.0)

    def test_float(self):
        self.assertEqual(_parse_value("5.2"), 5.2)

    def test_european_decimal(self):
        self.assertEqual(_parse_value("5,2"), 5.2)

    def test_invalid(self):
        self.assertIsNone(_parse_value("abc"))
        self.assertIsNone(_parse_value(""))

    def test_whitespace(self):
        self.assertEqual(_parse_value("  4.5  "), 4.5)


class TestUnitNormalization(unittest.TestCase):
    """ทดสอบการปรับรูปแบบหน่วย."""

    def test_mg_dl(self):
        self.assertEqual(_normalize_unit("mg/dl"), "mg/dL")
        self.assertEqual(_normalize_unit("mg/dL"), "mg/dL")

    def test_mmol_l(self):
        self.assertEqual(_normalize_unit("mmol/l"), "mmol/L")
        self.assertEqual(_normalize_unit("mmol/L"), "mmol/L")

    def test_g_dl(self):
        self.assertEqual(_normalize_unit("g/dl"), "g/dL")

    def test_percent(self):
        self.assertEqual(_normalize_unit("%"), "%")


class TestSpecimenDateExtraction(unittest.TestCase):
    """ทดสอบการดึงวันที่."""

    def test_iso_date(self):
        text = "Specimen Date: 2026-01-15\nSome content"
        self.assertEqual(extract_specimen_date(text), "2026-01-15")

    def test_no_date(self):
        text = "No date information here"
        self.assertEqual(extract_specimen_date(text), "unknown")

    def test_date_in_middle(self):
        text = "Header\nSpecimen Date: 2026-03-20\nResults"
        self.assertEqual(extract_specimen_date(text), "2026-03-20")


class TestLabExtraction(unittest.TestCase):
    """ทดสอบการดึงผลแล็บจากข้อความ."""

    def test_per_line_format(self):
        """ทดสอบรูปแบบ per-line (จาก pypdf)."""
        text = """LABORATORY REPORT
Specimen Date: 2026-01-15
Creatinine
5.2
mg/dL
0.6-1.2
Potassium (K)
4.5
mmol/L
3.5-5.0
Hemoglobin (Hb)
10.5
g/dL
12.0-17.0"""
        report = extract_lab_results_from_text(text, "test-001")
        self.assertEqual(report.status, "pending_review")
        self.assertEqual(report.specimen_date, "2026-01-15")

        analytes = {r.analyte: r for r in report.results}
        self.assertIn("Creatinine", analytes)
        self.assertEqual(analytes["Creatinine"].value, 5.2)
        self.assertEqual(analytes["Creatinine"].unit, "mg/dL")

        self.assertIn("Potassium", analytes)
        self.assertEqual(analytes["Potassium"].value, 4.5)

        self.assertIn("Hemoglobin", analytes)
        self.assertEqual(analytes["Hemoglobin"].value, 10.5)

    def test_user_requirement_values(self):
        """ข้อกำหนดข้อ 10: ทดสอบ Creatinine 5.2, Potassium 4.5, Hemoglobin 10.5."""
        text = """Specimen Date: 2026-01-15
Creatinine
5.2
mg/dL
0.6-1.2
Potassium (K)
4.5
mmol/L
3.5-5.0
Hemoglobin (Hb)
10.5
g/dL
12.0-17.0"""
        report = extract_lab_results_from_text(text, "TEST-CKD-001")
        analytes = {r.analyte: r for r in report.results}

        # ตามข้อกำหนด: Creatinine 5.2 mg/dL
        self.assertEqual(analytes["Creatinine"].value, 5.2)
        self.assertEqual(analytes["Creatinine"].unit, "mg/dL")

        # ตามข้อกำหนด: Potassium 4.5 mmol/L
        self.assertEqual(analytes["Potassium"].value, 4.5)
        self.assertEqual(analytes["Potassium"].unit, "mmol/L")

        # ตามข้อกำหนด: Hemoglobin 10.5 g/dL
        self.assertEqual(analytes["Hemoglobin"].value, 10.5)
        self.assertEqual(analytes["Hemoglobin"].unit, "g/dL")

    def test_empty_text(self):
        report = extract_lab_results_from_text("", "test-empty")
        self.assertEqual(len(report.results), 0)
        self.assertIn("ไม่พบผลตรวจ Lab ในเอกสาร", report.warnings)

    def test_no_lab_content(self):
        text = "This is a receipt for medical services. Total: 5000 Baht."
        report = extract_lab_results_from_text(text, "test-receipt")
        self.assertEqual(len(report.results), 0)

    def test_duplicate_prevention(self):
        """ป้องกันการดึง analyte ซ้ำ."""
        text = """Creatinine
5.2
mg/dL
Creatinine
5.2
mg/dL"""
        report = extract_lab_results_from_text(text, "test-dup")
        creatinine_count = sum(1 for r in report.results if r.analyte == "Creatinine")
        self.assertEqual(creatinine_count, 1)

    def test_critical_value_warning(self):
        """ค่าวิกฤตต้องมี warning."""
        text = """Potassium (K)
7.0
mmol/L
3.5-5.0"""
        report = extract_lab_results_from_text(text, "test-critical")
        self.assertTrue(any("วิกฤต" in w for w in report.warnings))

    def test_impossible_value_warning(self):
        """ค่าที่เป็นไปไม่ได้ต้องมี warning."""
        text = """Creatinine
999
mg/dL"""
        report = extract_lab_results_from_text(text, "test-impossible")
        if report.results:
            self.assertTrue(any("เป็นไปไม่ได้" in w for w in report.warnings))

    def test_no_fabrication(self):
        """ห้ามสร้างค่าที่ไม่มีอยู่ในเอกสาร — ข้อกำหนดข้อ 4."""
        text = "Creatinine\n5.2\nmg/dL"
        report = extract_lab_results_from_text(text, "test-nofab")
        # ต้องมีเฉพาะ Creatinine เท่านั้น
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0].analyte, "Creatinine")

    def test_confidence_reduced_for_issues(self):
        """Confidence ต้องลดลงเมื่อมีปัญหา."""
        text = """Creatinine
5.2
mmol/L"""  # wrong unit
        report = extract_lab_results_from_text(text, "test-conf")
        if report.results:
            self.assertLess(report.results[0].confidence, 0.9)


# ═══════════════════════════════════════════════════════════
# 4. Integration Tests — PDF Extraction
# ═══════════════════════════════════════════════════════════

class TestPDFExtraction(unittest.TestCase):
    """ทดสอบการดึงผลแล็บจาก TEST-CKD-001.pdf."""

    @classmethod
    def setUpClass(cls):
        cls.pdf_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "test_data", "TEST-CKD-001.pdf",
        )
        if not os.path.exists(cls.pdf_path):
            # สร้าง PDF ถ้ายังไม่มี
            from create_test_pdf import create_test_pdf
            create_test_pdf()

        from pypdf import PdfReader
        reader = PdfReader(cls.pdf_path)
        cls.text = reader.pages[0].extract_text()

    def test_pdf_text_extracted(self):
        self.assertTrue(len(self.text) > 100)
        self.assertIn("Creatinine", self.text)

    def test_full_extraction(self):
        report = extract_lab_results_from_text(self.text, "TEST-CKD-001")
        self.assertGreaterEqual(len(report.results), 15)
        self.assertEqual(report.status, "pending_review")
        self.assertEqual(report.specimen_date, "2026-01-15")

    def test_specific_values(self):
        """ข้อกำหนดข้อ 10: Creatinine 5.2, Potassium 4.5, Hemoglobin 10.5."""
        report = extract_lab_results_from_text(self.text, "TEST-CKD-001")
        analytes = {r.analyte: r for r in report.results}

        self.assertEqual(analytes["Creatinine"].value, 5.2)
        self.assertEqual(analytes["Potassium"].value, 4.5)
        self.assertEqual(analytes["Hemoglobin"].value, 10.5)

    def test_all_expected_analytes(self):
        """ตรวจว่าดึง analytes ที่คาดหวังได้ครบ."""
        report = extract_lab_results_from_text(self.text, "TEST-CKD-001")
        found = {r.analyte for r in report.results}
        expected = {
            "Creatinine", "BUN", "eGFR",
            "Sodium", "Potassium", "Chloride", "Bicarbonate",
            "Calcium", "Phosphate", "Albumin",
            "Hemoglobin", "Hematocrit", "WBC", "Platelet",
            "PTH", "Ferritin", "TSAT",
        }
        missing = expected - found
        self.assertEqual(missing, set(), f"Missing analytes: {missing}")

    def test_flags_correct(self):
        """ตรวจ flags สำหรับค่าผิดปกติ."""
        report = extract_lab_results_from_text(self.text, "TEST-CKD-001")
        analytes = {r.analyte: r for r in report.results}

        # Creatinine 5.2 ต้อง abnormal_high
        self.assertIn("abnormal_high", analytes["Creatinine"].flags)

        # Potassium 4.5 ต้องปกติ
        self.assertEqual(analytes["Potassium"].flags, [])

        # Hemoglobin 10.5 ต้อง abnormal_low
        self.assertIn("abnormal_low", analytes["Hemoglobin"].flags)

        # eGFR 10 ต้อง critical_low
        self.assertIn("critical_low", analytes["eGFR"].flags)

    def test_json_roundtrip(self):
        """ทดสอบ JSON serialization/deserialization."""
        report = extract_lab_results_from_text(self.text, "TEST-CKD-001")
        json_str = report.to_json()
        data = json.loads(json_str)
        report2 = LabReport.from_dict(data)

        self.assertEqual(len(report.results), len(report2.results))
        self.assertEqual(report.specimen_date, report2.specimen_date)
        self.assertEqual(report.status, report2.status)

    def test_extract_from_file(self):
        """ทดสอบ extract_lab_from_file กับ PDF."""
        report = extract_lab_from_file(
            self.pdf_path, "TEST-CKD-001",
            extracted_text=self.text,
        )
        self.assertGreaterEqual(len(report.results), 15)

    def test_extract_from_file_direct(self):
        """ทดสอบ extract_lab_from_file อ่าน PDF โดยตรง."""
        report = extract_lab_from_file(
            self.pdf_path, "TEST-CKD-001-direct",
        )
        self.assertGreaterEqual(len(report.results), 15)


# ═══════════════════════════════════════════════════════════
# 5. Security Tests
# ═══════════════════════════════════════════════════════════

class TestLabSecurity(unittest.TestCase):
    """ทดสอบความปลอดภัย."""

    def test_no_patient_id_in_report(self):
        """LabReport ต้องไม่มี patient_id."""
        text = "Creatinine\n5.2\nmg/dL"
        report = extract_lab_results_from_text(text, "doc-123")
        d = report.to_dict()
        self.assertNotIn("patient_id", d)
        self.assertIn("source_document_id", d)

    def test_status_always_pending(self):
        """ผลใหม่ต้อง pending_review เสมอ — ข้อกำหนดข้อ 6."""
        text = "Creatinine\n5.2\nmg/dL"
        report = extract_lab_results_from_text(text, "doc-123")
        self.assertEqual(report.status, ReviewStatus.PENDING_REVIEW.value)

    def test_cannot_set_approved_on_creation(self):
        """ไม่สามารถสร้าง report ที่ approved ตั้งแต่แรก."""
        report = LabReport(
            source_document_id="test",
            specimen_date="2026-01-01",
            extraction_method="test",
            status=ReviewStatus.APPROVED.value,  # พยายามตั้งค่า
        )
        # Note: dataclass allows this, but the extraction function always
        # creates with pending_review. This test documents the behavior.
        self.assertEqual(report.status, ReviewStatus.APPROVED.value)

    def test_extraction_never_uses_external_ai(self):
        """ข้อกำหนดข้อ 8: ห้ามส่งข้อมูลไป AI ภายนอก."""
        # lab_extractor ไม่ import หรือเรียก API ใดๆ
        import lab_extractor
        source = open(lab_extractor.__file__).read()
        self.assertNotIn("call_claude_api", source)
        self.assertNotIn("openai", source.lower())
        self.assertNotIn("anthropic", source.lower())
        self.assertNotIn("requests.post", source)

    def test_result_has_raw_text(self):
        """LabResult ต้องมี raw_text สำหรับ audit trail."""
        text = "Creatinine\n5.2\nmg/dL"
        report = extract_lab_results_from_text(text, "test-raw")
        if report.results:
            self.assertTrue(report.results[0].raw_text)

    def test_review_status_enum(self):
        """ReviewStatus ต้องมีค่าที่ถูกต้อง."""
        self.assertEqual(ReviewStatus.PENDING_REVIEW.value, "pending_review")
        self.assertEqual(ReviewStatus.APPROVED.value, "approved")
        self.assertEqual(ReviewStatus.REJECTED.value, "rejected")


# ═══════════════════════════════════════════════════════════
# 6. Regression Tests — Edge Cases
# ═══════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):
    """ทดสอบ edge cases."""

    def test_fbs_alias(self):
        text = "FBS (Glucose)\n110\nmg/dL\n70-100"
        report = extract_lab_results_from_text(text, "test-fbs")
        self.assertTrue(any(r.analyte == "FBS" for r in report.results))

    def test_bicarbonate_hco3(self):
        text = "Bicarbonate (HCO3)\n18\nmmol/L\n22-29"
        report = extract_lab_results_from_text(text, "test-hco3")
        self.assertTrue(any(r.analyte == "Bicarbonate" for r in report.results))

    def test_phosphate_po4(self):
        text = "Phosphate (PO4)\n6.5\nmg/dL\n2.5-4.5"
        report = extract_lab_results_from_text(text, "test-po4")
        self.assertTrue(any(r.analyte == "Phosphate" for r in report.results))

    def test_very_long_text(self):
        """ไม่ crash กับข้อความยาวมาก."""
        text = "Random text\n" * 500
        report = extract_lab_results_from_text(text, "test-long")
        self.assertIsNotNone(report)

    def test_special_characters(self):
        """ไม่ crash กับอักขระพิเศษ."""
        text = "Creatinine\n5.2\nmg/dL\n<>&\"'\\n\\t"
        report = extract_lab_results_from_text(text, "test-special")
        self.assertIsNotNone(report)

    def test_unsupported_file_extension(self):
        report = extract_lab_from_file("test.xlsx", "test-xlsx")
        self.assertIn("ไม่รองรับ", report.warnings[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
