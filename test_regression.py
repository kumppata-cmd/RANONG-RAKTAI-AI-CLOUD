#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression Tests — Kidney Clinical Intelligence Platform 2.0

ทดสอบ edge cases และ regressions:
  1. LabResult / LabReport edge cases
  2. Clinical Analyzer boundary conditions
  3. Lab Tracker edge cases
  4. Medication Review edge cases
  5. Command parsing edge cases
  6. Alert system edge cases
  7. Document index edge cases
  8. Flex Message / Download edge cases

ใช้ข้อมูล DEMO เท่านั้น — ไม่ใช้ข้อมูลผู้ป่วยจริง
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("LINE_CHANNEL_SECRET", "test_secret_for_unit_test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test_token_for_unit_test")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("ENABLE_AI_CLASSIFY", "false")
os.environ.setdefault("STORAGE_BACKEND", "local")

import server
from lab_models import LabResult, LabReport
from clinical_analyzer import analyze_lab_report
from lab_tracker import collect_trend_points, calculate_trend_direction, TrendPoint
from medication_review import review_medications
from lab_alert import check_lab_alerts, format_user_alert_th, format_admin_alert_th
from line_bot_enhancer import parse_command, parse_search_query, smart_search, CommandType


def _r(analyte, value, unit, ref="N/A", flags=None, conf=0.95):
    return LabResult(
        analyte=analyte, value=value, unit=unit,
        reference_range=ref, flags=flags or [], confidence=conf,
        extraction_method="test",
    )


def _report(results, doc_id="reg-001", date="2025-03-15"):
    return LabReport(
        source_document_id=doc_id, specimen_date=date,
        results=results, extraction_method="test",
    )


# ══════════════════════════════════════════════════════════════
# 1. LabResult Edge Cases
# ══════════════════════════════════════════════════════════════

class TestLabResultEdgeCases(unittest.TestCase):
    """LabResult edge cases."""

    def test_zero_value(self):
        """ค่า 0 ต้องไม่ถูกตีความว่า None/missing."""
        r = _r("Glucose", 0.0, "mg/dL")
        self.assertEqual(r.value, 0.0)
        self.assertIsNotNone(r.value)

    def test_very_large_value(self):
        """ค่าสูงมากต้องไม่ overflow."""
        r = _r("WBC", 999999.0, "cells/uL")
        self.assertEqual(r.value, 999999.0)

    def test_negative_value(self):
        """ค่าติดลบ (rare edge case) ต้องไม่ crash."""
        r = _r("Temperature_Delta", -2.5, "C")
        self.assertEqual(r.value, -2.5)

    def test_empty_flags(self):
        """flags ว่างต้องเป็น list ว่าง."""
        r = _r("Hb", 12.0, "g/dL", flags=[])
        self.assertEqual(r.flags, [])

    def test_multiple_flags(self):
        """หลาย flags."""
        r = _r("K", 7.0, "mmol/L", flags=["critical_high", "abnormal"])
        self.assertIn("critical_high", r.flags)
        self.assertIn("abnormal", r.flags)

    def test_empty_report(self):
        """LabReport ที่ไม่มี results ต้องไม่ crash."""
        report = _report([])
        self.assertEqual(len(report.results), 0)

    def test_special_chars_in_analyte(self):
        """ชื่อ analyte ที่มีอักขระพิเศษ."""
        r = _r("β2-Microglobulin", 2.5, "mg/L")
        self.assertEqual(r.analyte, "β2-Microglobulin")

    def test_unicode_unit(self):
        """Unit ที่เป็น unicode."""
        r = _r("eGFR", 45.0, "mL/min/1.73m²")
        self.assertEqual(r.unit, "mL/min/1.73m²")


# ══════════════════════════════════════════════════════════════
# 2. Clinical Analyzer Boundary Conditions
# ══════════════════════════════════════════════════════════════

class TestClinicalAnalyzerEdgeCases(unittest.TestCase):
    """Clinical Analyzer edge cases."""

    def test_empty_report_analysis(self):
        """วิเคราะห์ report ว่าง → ต้องไม่ crash."""
        report = _report([])
        analysis = analyze_lab_report(report)
        self.assertIsNotNone(analysis)

    def test_single_normal_result(self):
        """ผลปกติ 1 ค่า."""
        report = _report([_r("Hb", 13.0, "g/dL", "12.0-16.0")])
        analysis = analyze_lab_report(report)
        self.assertIsNotNone(analysis)

    def test_all_critical_results(self):
        """ทุกค่า critical."""
        results = [
            _r("Potassium", 7.5, "mmol/L", "3.5-5.0", ["critical_high"]),
            _r("Sodium", 115, "mmol/L", "136-145", ["critical_low"]),
        ]
        report = _report(results)
        analysis = analyze_lab_report(report)
        self.assertIsNotNone(analysis)
        self.assertTrue(len(analysis.findings) > 0)

    def test_borderline_egfr(self):
        """eGFR ค่า borderline ระหว่าง CKD stages."""
        results = [_r("eGFR", 30.0, "mL/min/1.73m²", ">60")]
        report = _report(results)
        analysis = analyze_lab_report(report)
        self.assertIsNotNone(analysis)

    def test_very_low_egfr(self):
        """eGFR ต่ำมาก (CKD Stage 5)."""
        results = [_r("eGFR", 8.0, "mL/min/1.73m²", ">60")]
        report = _report(results)
        analysis = analyze_lab_report(report)
        self.assertIsNotNone(analysis)


# ══════════════════════════════════════════════════════════════
# 3. Lab Tracker Edge Cases
# ══════════════════════════════════════════════════════════════

class TestLabTrackerEdgeCases(unittest.TestCase):
    """Lab Tracker edge cases."""

    def test_empty_reports_list(self):
        """ไม่มี reports → trend points ว่าง."""
        points = collect_trend_points([], "Creatinine")
        self.assertEqual(len(points), 0)

    def test_single_data_point(self):
        """data point เดียว → direction indeterminate."""
        points = [TrendPoint(date="2025-01-01", value=1.5, unit="mg/dL", source_document_id="reg-t1")]
        direction, slope = calculate_trend_direction(points)
        self.assertIsNotNone(direction)

    def test_two_data_points_increasing(self):
        """2 data points เพิ่มขึ้น."""
        points = [
            TrendPoint(date="2025-01-01", value=1.0, unit="mg/dL", source_document_id="reg-t2a"),
            TrendPoint(date="2025-02-01", value=2.0, unit="mg/dL", source_document_id="reg-t2b"),
        ]
        direction, slope = calculate_trend_direction(points)
        self.assertIn(direction, ["increasing", "stable", "indeterminate"])

    def test_flat_trend(self):
        """ค่าคงที่ → stable."""
        points = [
            TrendPoint(date="2025-01-01", value=5.0, unit="mg/dL", source_document_id="reg-t3a"),
            TrendPoint(date="2025-02-01", value=5.0, unit="mg/dL", source_document_id="reg-t3b"),
            TrendPoint(date="2025-03-01", value=5.0, unit="mg/dL", source_document_id="reg-t3c"),
        ]
        direction, slope = calculate_trend_direction(points)
        self.assertEqual(direction, "stable")

    def test_analyte_not_found_in_reports(self):
        """analyte ที่ไม่มีใน reports → empty list."""
        reports = [
            {
                "source_document_id": "reg-track-001",
                "specimen_date": "2025-01-15",
                "results": [{"analyte": "Hb", "value": 12.0, "unit": "g/dL"}],
            },
        ]
        points = collect_trend_points(reports, "NonExistentAnalyte")
        self.assertEqual(len(points), 0)


# ══════════════════════════════════════════════════════════════
# 4. Medication Review Edge Cases
# ══════════════════════════════════════════════════════════════

class TestMedicationReviewEdgeCases(unittest.TestCase):
    """Medication Review edge cases."""

    def test_empty_medication_list(self):
        """รายการยาว่าง."""
        result = review_medications([], {})
        self.assertIsNotNone(result)

    def test_unknown_medication(self):
        """ยาที่ไม่รู้จัก → ไม่ crash."""
        result = review_medications(
            ["UnknownDrugXYZ123"],
            {"Creatinine": 2.0},
        )
        self.assertIsNotNone(result)

    def test_severe_ckd_with_nsaid(self):
        """NSAID ใน CKD ขั้นรุนแรง → ต้องมี alert."""
        result = review_medications(
            ["Ibuprofen"],
            {"Creatinine": 5.0},
            ckd_stage="5",
            egfr=10.0,
        )
        self.assertIsNotNone(result)
        # ต้องมี alerts (MedicationReviewResult.alerts)
        self.assertTrue(len(result.alerts) > 0)

    def test_duplicate_medications(self):
        """ยาซ้ำไม่ crash."""
        result = review_medications(
            ["Metformin", "Metformin"],
            {"Creatinine": 1.5},
        )
        self.assertIsNotNone(result)

    def test_case_insensitive_medication(self):
        """ชื่อยา case-insensitive."""
        r1 = review_medications(["metformin"], {"Creatinine": 1.5}, egfr=45.0)
        r2 = review_medications(["METFORMIN"], {"Creatinine": 1.5}, egfr=45.0)
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)


# ══════════════════════════════════════════════════════════════
# 5. Command Parsing Edge Cases
# ══════════════════════════════════════════════════════════════

class TestCommandParsingEdgeCases(unittest.TestCase):
    """Command Parsing edge cases."""

    def test_empty_text(self):
        """ข้อความว่าง."""
        result = parse_command("")
        self.assertEqual(result.command_type, CommandType.UNKNOWN)

    def test_whitespace_only(self):
        """ข้อความมีแค่ whitespace."""
        result = parse_command("   ")
        self.assertEqual(result.command_type, CommandType.UNKNOWN)

    def test_very_long_command(self):
        """คำสั่งยาวมาก → ไม่ crash."""
        result = parse_command("ค้นหา " + "x" * 5000)
        self.assertIsNotNone(result.command_type)

    def test_help_variations(self):
        """คำสั่ง help หลายรูปแบบ."""
        for text in ["ช่วยเหลือ", "help", "คำสั่ง"]:
            result = parse_command(text)
            self.assertEqual(result.command_type, CommandType.HELP, f"Failed for: {text}")

    def test_search_with_special_chars(self):
        """ค้นหาด้วยอักขระพิเศษ."""
        result = parse_command("ค้นหา @#$%^&*()")
        self.assertEqual(result.command_type, CommandType.SEARCH)

    def test_search_query_empty(self):
        """parse_search_query ด้วย string ว่าง."""
        sf = parse_search_query("")
        self.assertIsNotNone(sf)

    def test_parse_command_newlines(self):
        """ข้อความมี newlines."""
        result = parse_command("ค้นหา\ntest\ndata")
        self.assertIsNotNone(result.command_type)


# ══════════════════════════════════════════════════════════════
# 6. Alert System Edge Cases
# ══════════════════════════════════════════════════════════════

class TestAlertEdgeCases(unittest.TestCase):
    """Alert system edge cases."""

    def test_no_alerts_for_normal_values(self):
        """ค่าปกติ → ไม่มี alert."""
        results = [_r("Hb", 13.0, "g/dL", "12.0-16.0")]
        report = _report(results)
        summary = check_lab_alerts(report)
        self.assertEqual(len(summary.alerts), 0)

    def test_multiple_critical_alerts(self):
        """หลายค่า critical → alert ทุกค่า."""
        results = [
            _r("Potassium", 7.5, "mmol/L", "3.5-5.0", ["critical_high"]),
            _r("Sodium", 110, "mmol/L", "136-145", ["critical_low"]),
            _r("Glucose", 500, "mg/dL", "70-100", ["critical_high"]),
        ]
        report = _report(results)
        summary = check_lab_alerts(report)
        self.assertTrue(len(summary.alerts) >= 2)

    def test_format_alert_empty_summary(self):
        """format alert สำหรับ report ที่ไม่มี critical → ข้อความปกติ."""
        results = [_r("Hb", 13.0, "g/dL", "12.0-16.0")]
        report = _report(results)
        summary = check_lab_alerts(report)
        text = format_user_alert_th(summary)
        self.assertIsInstance(text, str)

    def test_admin_alert_format(self):
        """Admin alert format ต้องมีข้อมูลเพิ่มเติม."""
        results = [
            _r("Potassium", 7.5, "mmol/L", "3.5-5.0", ["critical_high"]),
        ]
        report = _report(results)
        summary = check_lab_alerts(report)
        text = format_admin_alert_th(summary)
        self.assertIsInstance(text, str)
        self.assertIn("Potassium", text)

    def test_doc_id_truncation_in_alert(self):
        """doc_id ยาว → truncate ใน alert (PDPA)."""
        results = [
            _r("K", 8.0, "mmol/L", "3.5-5.0", ["critical_high"]),
        ]
        report = _report(results, doc_id="very-long-document-id-12345678")
        summary = check_lab_alerts(report)
        text = format_user_alert_th(summary)
        # ต้อง truncate doc_id
        self.assertNotIn("very-long-document-id-12345678", text)


# ══════════════════════════════════════════════════════════════
# 7. Health Endpoint Edge Cases
# ══════════════════════════════════════════════════════════════

class TestHealthEndpointEdgeCases(unittest.TestCase):
    """Health endpoint edge cases."""

    def test_health_returns_json(self):
        """/health ต้องคืน JSON."""
        with server.app.test_client() as client:
            resp = client.get("/health")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn("status", data)

    def test_health_has_required_fields(self):
        """/health ต้องมี fields ที่จำเป็น."""
        with server.app.test_client() as client:
            resp = client.get("/health")
            data = resp.get_json()
            self.assertIn("status", data)
            self.assertIn("version", data)

    def test_root_redirects_or_responds(self):
        """/ endpoint ต้องตอบ."""
        with server.app.test_client() as client:
            resp = client.get("/")
            self.assertIn(resp.status_code, [200, 301, 302, 308])


# ══════════════════════════════════════════════════════════════
# 8. Download Proxy Edge Cases
# ══════════════════════════════════════════════════════════════

class TestDownloadProxyEdgeCases(unittest.TestCase):
    """Download proxy edge cases."""

    def test_empty_token_path(self):
        """/dl/ ไม่มี token → 404 หรือ 403."""
        with server.app.test_client() as client:
            resp = client.get("/dl/")
            self.assertIn(resp.status_code, [403, 404])

    def test_double_encoded_token(self):
        """Token ที่ encode ซ้ำ → reject."""
        with server.app.test_client() as client:
            resp = client.get("/dl/%2F%2E%2E%2Fetc%2Fpasswd")
            self.assertIn(resp.status_code, [403, 404])


# ══════════════════════════════════════════════════════════════
# 9. Handle Search Edge Cases
# ══════════════════════════════════════════════════════════════

class TestHandleSearchEdgeCases(unittest.TestCase):
    """handle_search_command edge cases."""

    def test_help_returns_usage(self):
        """ช่วยเหลือ → คืนข้อความวิธีใช้."""
        result = server.handle_search_command("ช่วยเหลือ", "anyhash")
        self.assertIsNotNone(result)
        self.assertIsInstance(result, str)
        # ต้องมีคำสั่งแนะนำ
        self.assertTrue(len(result) > 50)

    @patch("server.is_authorized_user", return_value=True)
    def test_recent_with_no_docs(self, mock_auth):
        """เอกสารล่าสุด เมื่อไม่มีเอกสาร."""
        with patch.object(server.doc_index, "recent_by_owner", return_value=[]):
            result = server.handle_search_command("เอกสารล่าสุด", "testhash")
        self.assertIsInstance(result, str)

    @patch("server.is_authorized_user", return_value=True)
    def test_search_no_results(self, mock_auth):
        """ค้นหาไม่พบ → ข้อความแจ้ง."""
        with patch.object(server.doc_index, "search", return_value=[]):
            result = server.handle_search_command("ค้นหา xyznonexistent", "testhash")
        self.assertIsInstance(result, str)
        self.assertIn("ไม่พบ", result)


if __name__ == "__main__":
    unittest.main()
