#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for quarterly_compare.py and patient_hn_registry.py
RANONG RAKTAI AI 2.0 — Feature Testing

ไม่ใช้ข้อมูลผู้ป่วยจริง ทดสอบด้วย synthetic data เท่านั้น
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from patient_hn_registry import (
    PatientHN,
    HNRegistry,
    _validate_hn,
    get_quarter,
    get_quarter_range,
    get_month_range,
    quarter_label_thai,
    month_label_thai,
)
from quarterly_compare import (
    build_quarterly_path,
    build_monthly_patient_path,
    build_legacy_path,
    LabComparison,
    LabComparisonResult,
    RxComparison,
    RxComparisonResult,
)


# ═══════════════════════════════════════════════════════════
# PatientHN Tests
# ═══════════════════════════════════════════════════════════

class TestPatientHN(unittest.TestCase):
    def test_create_patient(self):
        p = PatientHN(
            owner_hash="abc12345678",
            clinic_hn="1234",
            hospital_hn="56789",
        )
        self.assertEqual(p.clinic_hn, "1234")
        self.assertEqual(p.hospital_hn, "56789")
        self.assertEqual(p.hn_pair, "C-1234/H-56789")
        self.assertEqual(p.folder_name, "1234_56789")

    def test_hn_pair_clinic_only(self):
        p = PatientHN(owner_hash="abc", clinic_hn="1234")
        self.assertEqual(p.hn_pair, "C-1234")
        self.assertEqual(p.folder_name, "1234")

    def test_hn_pair_hospital_only(self):
        p = PatientHN(owner_hash="abc", hospital_hn="H999")
        self.assertEqual(p.hn_pair, "H-H999")
        self.assertEqual(p.folder_name, "H999")

    def test_hn_pair_none(self):
        p = PatientHN(owner_hash="abcdef12")
        self.assertEqual(p.hn_pair, "ID-abcdef")
        self.assertEqual(p.folder_name, "abcdef12")

    def test_to_dict_roundtrip(self):
        p = PatientHN(
            owner_hash="abc123",
            clinic_hn="C001",
            hospital_hn="H001",
            display_name="Test",
            registered_by="admin",
        )
        d = p.to_dict()
        p2 = PatientHN.from_dict(d)
        self.assertEqual(p2.clinic_hn, "C001")
        self.assertEqual(p2.hospital_hn, "H001")
        self.assertEqual(p2.display_name, "Test")

    def test_hn_uppercase(self):
        p = PatientHN(owner_hash="abc", clinic_hn="abc", hospital_hn="def")
        self.assertEqual(p.clinic_hn, "ABC")
        self.assertEqual(p.hospital_hn, "DEF")


class TestValidateHN(unittest.TestCase):
    def test_valid_numeric(self):
        self.assertTrue(_validate_hn("12345"))

    def test_valid_alphanumeric(self):
        self.assertTrue(_validate_hn("HN-001"))

    def test_invalid_special_chars(self):
        self.assertFalse(_validate_hn("HN@001"))

    def test_invalid_too_long(self):
        self.assertFalse(_validate_hn("A" * 21))

    def test_invalid_empty(self):
        self.assertFalse(_validate_hn(""))


class TestQuarterHelpers(unittest.TestCase):
    def test_get_quarter_q1(self):
        d = datetime(2026, 2, 15, tzinfo=timezone.utc)
        self.assertEqual(get_quarter(d), "2026-Q1")

    def test_get_quarter_q4(self):
        d = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.assertEqual(get_quarter(d), "2026-Q4")

    def test_quarter_range_q4(self):
        start, end = get_quarter_range("2026-Q4")
        self.assertEqual(start.month, 10)
        self.assertEqual(end.month, 1)
        self.assertEqual(end.year, 2027)

    def test_quarter_range_q1(self):
        start, end = get_quarter_range("2026-Q1")
        self.assertEqual(start.month, 1)
        self.assertEqual(end.month, 4)

    def test_month_range(self):
        start, end = get_month_range(2026, 12)
        self.assertEqual(start.month, 12)
        self.assertEqual(end.month, 1)
        self.assertEqual(end.year, 2027)

    def test_quarter_label_thai(self):
        label = quarter_label_thai("2026-Q4")
        self.assertIn("ไตรมาส 4", label)
        self.assertIn("2569", label)

    def test_month_label_thai(self):
        label = month_label_thai(2026, 10)
        self.assertIn("ต.ค.", label)
        self.assertIn("2569", label)


class TestHNRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = HNRegistry()
        self.registry._loaded = True  # skip GCS load

    def test_register_new_no_storage(self):
        success, msg = self.registry.register(
            owner_hash="test123",
            clinic_hn="C001",
            hospital_hn="H001",
        )
        # No storage → save fails
        self.assertFalse(success)

    def test_register_invalid_hn(self):
        success, msg = self.registry.register(
            owner_hash="test123",
            clinic_hn="HN@INVALID",
        )
        self.assertFalse(success)
        self.assertIn("ไม่ถูกต้อง", msg)

    def test_get_folder_name_not_found(self):
        name = self.registry.get_folder_name("nonexistent")
        self.assertEqual(name, "nonexist")  # first 8 chars


# ═══════════════════════════════════════════════════════════
# Path Builder Tests
# ═══════════════════════════════════════════════════════════

class TestPathBuilders(unittest.TestCase):
    def test_quarterly_path(self):
        path = build_quarterly_path("04_LAB_RESULTS", "test.pdf")
        # Format: FOLDER/YYYY-QN/YYYY-MM/filename
        parts = path.split("/")
        self.assertEqual(parts[0], "04_LAB_RESULTS")
        self.assertRegex(parts[1], r"\d{4}-Q[1-4]")
        self.assertRegex(parts[2], r"\d{4}-\d{2}")
        self.assertEqual(parts[3], "test.pdf")

    def test_monthly_patient_path_with_folder(self):
        path = build_monthly_patient_path(
            "05_DIALYSIS_RECORDS", "test.pdf", "1234_56789"
        )
        parts = path.split("/")
        self.assertEqual(parts[0], "05_DIALYSIS_RECORDS")
        self.assertRegex(parts[1], r"\d{4}-\d{2}")
        self.assertEqual(parts[2], "1234_56789")
        self.assertEqual(parts[3], "test.pdf")

    def test_monthly_patient_path_no_folder(self):
        path = build_monthly_patient_path(
            "05_DIALYSIS_RECORDS", "test.pdf", ""
        )
        parts = path.split("/")
        self.assertEqual(len(parts), 3)  # no patient subfolder

    def test_legacy_path(self):
        path = build_legacy_path("01_LINE_INBOX", "test.pdf")
        parts = path.split("/")
        self.assertEqual(parts[0], "01_LINE_INBOX")
        self.assertRegex(parts[1], r"\d{4}-\d{2}")
        self.assertEqual(parts[2], "test.pdf")

    def test_legacy_path_with_subfolder(self):
        path = build_legacy_path("01_LINE_INBOX", "test.pdf", "sub")
        parts = path.split("/")
        self.assertEqual(parts[0], "01_LINE_INBOX")
        self.assertEqual(parts[1], "sub")
        # subfolder path: FOLDER/sub/YYYY-MM/filename
        self.assertRegex(parts[2], r"\d{4}-\d{2}")
        self.assertEqual(parts[3], "test.pdf")


# ═══════════════════════════════════════════════════════════
# Lab Comparison Tests
# ═══════════════════════════════════════════════════════════

class TestLabComparison(unittest.TestCase):
    def test_comparison_result_direction_up(self):
        r = LabComparisonResult(
            analyte="Creatinine",
            old_value=1.2,
            new_value=1.8,
            unit="mg/dL",
        )
        self.assertEqual(r.direction, "↑")
        self.assertAlmostEqual(r.change_pct, 50.0, places=1)

    def test_comparison_result_direction_down(self):
        r = LabComparisonResult(
            analyte="BUN",
            old_value=30.0,
            new_value=20.0,
            unit="mg/dL",
        )
        self.assertEqual(r.direction, "↓")

    def test_comparison_result_stable(self):
        r = LabComparisonResult(
            analyte="Na",
            old_value=140.0,
            new_value=140.0,
            unit="mEq/L",
        )
        self.assertEqual(r.direction, "→")

    def test_comparison_result_unknown(self):
        r = LabComparisonResult(
            analyte="K",
            old_value=None,
            new_value=4.5,
            unit="mEq/L",
        )
        self.assertEqual(r.direction, "?")
        self.assertEqual(r.status_change, "new")

    def test_compare_two_reports(self):
        old_results = [
            {"analyte_name": "Creatinine", "value": 1.2, "unit": "mg/dL"},
            {"analyte_name": "BUN", "value": 30, "unit": "mg/dL"},
        ]
        new_results = [
            {"analyte_name": "Creatinine", "value": 1.8, "unit": "mg/dL"},
            {"analyte_name": "BUN", "value": 25, "unit": "mg/dL"},
            {"analyte_name": "K", "value": 4.5, "unit": "mEq/L"},
        ]
        cmp = LabComparison("old_doc", "new_doc")
        cmp.compare(old_results, new_results)
        items = cmp.results
        self.assertEqual(len(items), 3)
        # Creatinine went up (uppercased keys)
        cr = next(i for i in items if i.analyte == "CREATININE")
        self.assertEqual(cr.direction, "↑")
        # BUN went down
        bun = next(i for i in items if i.analyte == "BUN")
        self.assertEqual(bun.direction, "↓")
        # K is new
        k = next(i for i in items if i.analyte == "K")
        self.assertEqual(k.status_change, "new")

    def test_format_thai_text(self):
        old_results = [
            {"analyte_name": "Creatinine", "value": 0.8, "unit": "mg/dL",
             "ref_min": 0.6, "ref_max": 1.2},
        ]
        new_results = [
            {"analyte_name": "Creatinine", "value": 1.8, "unit": "mg/dL",
             "ref_min": 0.6, "ref_max": 1.2},
        ]
        cmp = LabComparison("old_doc", "new_doc")
        cmp.compare(old_results, new_results)
        text = cmp.format_thai_text()
        self.assertIn("เปรียบเทียบ", text)
        # Creatinine was normal (0.8) -> abnormal (1.8), so it's "worsened"
        self.assertIn("CREATININE", text)


# ═══════════════════════════════════════════════════════════
# Rx Comparison Tests
# ═══════════════════════════════════════════════════════════

class TestRxComparison(unittest.TestCase):
    def test_compare_prescriptions(self):
        old_drugs = ["Amlodipine 5mg", "Enalapril 10mg", "Metformin 500mg"]
        new_drugs = ["Amlodipine 10mg", "Enalapril 10mg", "Losartan 50mg"]
        cmp = RxComparison("old_rx", "new_rx")
        cmp.compare(old_drugs, new_drugs)
        items = cmp.results
        # Uses uppercased set comparison, so "AMLODIPINE 5MG" != "AMLODIPINE 10MG"
        added = [i for i in items if i.status == "added"]
        removed = [i for i in items if i.status == "removed"]
        unchanged = [i for i in items if i.status == "unchanged"]
        self.assertTrue(len(added) >= 1)  # Losartan + Amlodipine 10mg
        self.assertTrue(len(removed) >= 1)  # Metformin + Amlodipine 5mg


if __name__ == "__main__":
    unittest.main()
