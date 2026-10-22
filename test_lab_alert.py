#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for lab_alert module — Phase 7."""

import unittest
from unittest.mock import MagicMock

from lab_alert import (
    AlertLevel,
    AlertSummary,
    LabAlert,
    ALERT_EMOJI,
    FLAG_MESSAGES_TH,
    check_lab_alerts,
    format_user_alert_th,
    format_admin_alert_th,
    _flag_to_alert_level,
)


def _make_lab_result(analyte="Creatinine", value=1.0, unit="mg/dL",
                     reference_range="0.6-1.2", confidence=0.95,
                     flags=None):
    """สร้าง mock LabResult."""
    r = MagicMock()
    r.analyte = analyte
    r.value = value
    r.unit = unit
    r.reference_range = reference_range
    r.confidence = confidence
    r.flags = flags or []
    return r


def _make_lab_report(results=None, doc_id="abcdef12345678",
                     specimen_date="2026-01-15"):
    """สร้าง mock LabReport."""
    report = MagicMock()
    report.source_document_id = doc_id
    report.specimen_date = specimen_date
    report.results = results or []
    return report


class TestAlertLevel(unittest.TestCase):
    """ทดสอบ AlertLevel enum."""

    def test_values(self):
        self.assertEqual(AlertLevel.CRITICAL.value, "critical")
        self.assertEqual(AlertLevel.WARNING.value, "warning")
        self.assertEqual(AlertLevel.INFO.value, "info")

    def test_emoji_mapping(self):
        self.assertIn(AlertLevel.CRITICAL, ALERT_EMOJI)
        self.assertIn(AlertLevel.WARNING, ALERT_EMOJI)
        self.assertEqual(ALERT_EMOJI[AlertLevel.CRITICAL], "🔴")
        self.assertEqual(ALERT_EMOJI[AlertLevel.WARNING], "🟡")


class TestFlagToAlertLevel(unittest.TestCase):
    """ทดสอบการแปลง flag เป็น alert level."""

    def test_critical_high(self):
        self.assertEqual(_flag_to_alert_level("critical_high"), AlertLevel.CRITICAL)

    def test_critical_low(self):
        self.assertEqual(_flag_to_alert_level("critical_low"), AlertLevel.CRITICAL)

    def test_abnormal_high(self):
        self.assertEqual(_flag_to_alert_level("abnormal_high"), AlertLevel.WARNING)

    def test_abnormal_low(self):
        self.assertEqual(_flag_to_alert_level("abnormal_low"), AlertLevel.WARNING)

    def test_unit_mismatch_returns_none(self):
        """unit_mismatch ไม่ควรเป็น alert."""
        self.assertIsNone(_flag_to_alert_level("unit_mismatch"))

    def test_physiologic_impossible_returns_none(self):
        self.assertIsNone(_flag_to_alert_level("physiologic_impossible"))

    def test_low_confidence_returns_none(self):
        self.assertIsNone(_flag_to_alert_level("low_confidence"))

    def test_unknown_flag(self):
        self.assertIsNone(_flag_to_alert_level("something_else"))


class TestFlagMessagesTh(unittest.TestCase):
    """ทดสอบคำอธิบายภาษาไทย."""

    def test_all_flags_have_messages(self):
        expected_flags = [
            "critical_high", "critical_low",
            "abnormal_high", "abnormal_low",
            "unit_mismatch", "physiologic_impossible",
            "low_confidence",
        ]
        for flag in expected_flags:
            self.assertIn(flag, FLAG_MESSAGES_TH, f"Missing message for {flag}")


class TestCheckLabAlerts(unittest.TestCase):
    """ทดสอบการตรวจสอบผลแล็บ."""

    def test_no_results(self):
        report = _make_lab_report(results=[])
        summary = check_lab_alerts(report)
        self.assertEqual(len(summary.alerts), 0)
        self.assertFalse(summary.should_notify_user)
        self.assertFalse(summary.should_notify_admin)

    def test_normal_results(self):
        """ผลตรวจปกติ — ไม่มี flag ไม่แจ้งเตือน."""
        results = [
            _make_lab_result("Creatinine", 1.0, "mg/dL", "0.6-1.2", flags=[]),
            _make_lab_result("BUN", 15.0, "mg/dL", "7-20", flags=[]),
        ]
        report = _make_lab_report(results=results)
        summary = check_lab_alerts(report)
        self.assertEqual(len(summary.alerts), 0)
        self.assertFalse(summary.should_notify_user)

    def test_critical_high(self):
        """ค่าวิกฤตสูง — ต้องแจ้งเตือนทั้ง user และ admin."""
        results = [
            _make_lab_result("Potassium", 7.0, "mmol/L", "3.5-5.0",
                             flags=["critical_high"]),
        ]
        report = _make_lab_report(results=results)
        summary = check_lab_alerts(report)
        self.assertEqual(len(summary.alerts), 1)
        self.assertEqual(summary.alerts[0].level, AlertLevel.CRITICAL)
        self.assertTrue(summary.should_notify_user)
        self.assertTrue(summary.should_notify_admin)

    def test_critical_low(self):
        results = [
            _make_lab_result("Hemoglobin", 5.0, "g/dL", "12-16",
                             flags=["critical_low"]),
        ]
        report = _make_lab_report(results=results)
        summary = check_lab_alerts(report)
        self.assertEqual(summary.alerts[0].level, AlertLevel.CRITICAL)
        self.assertTrue(summary.has_critical)

    def test_abnormal_high(self):
        """ค่าผิดปกติ — แจ้ง user แต่ไม่แจ้ง admin."""
        results = [
            _make_lab_result("Creatinine", 1.8, "mg/dL", "0.6-1.2",
                             flags=["abnormal_high"]),
        ]
        report = _make_lab_report(results=results)
        summary = check_lab_alerts(report)
        self.assertEqual(len(summary.alerts), 1)
        self.assertEqual(summary.alerts[0].level, AlertLevel.WARNING)
        self.assertTrue(summary.should_notify_user)
        self.assertFalse(summary.should_notify_admin)

    def test_mixed_critical_and_warning(self):
        """ค่าผสม — critical เรียงก่อน warning."""
        results = [
            _make_lab_result("BUN", 25.0, "mg/dL", "7-20",
                             flags=["abnormal_high"]),
            _make_lab_result("Potassium", 7.5, "mmol/L", "3.5-5.0",
                             flags=["critical_high"]),
            _make_lab_result("Creatinine", 1.0, "mg/dL", "0.6-1.2",
                             flags=[]),
        ]
        report = _make_lab_report(results=results)
        summary = check_lab_alerts(report)
        self.assertEqual(len(summary.alerts), 2)
        self.assertEqual(summary.alerts[0].level, AlertLevel.CRITICAL)
        self.assertEqual(summary.alerts[1].level, AlertLevel.WARNING)
        self.assertEqual(summary.critical_count, 1)
        self.assertEqual(summary.warning_count, 1)
        self.assertEqual(summary.total_results, 3)
        self.assertTrue(summary.should_notify_admin)

    def test_multiple_flags_on_one_result(self):
        """ผลตรวจเดียวมีหลาย flags — สร้าง alert แต่ละ flag."""
        results = [
            _make_lab_result("TestAnalyte", 999.0, "U/L", "0-40",
                             flags=["critical_high", "abnormal_high"]),
        ]
        report = _make_lab_report(results=results)
        summary = check_lab_alerts(report)
        self.assertEqual(len(summary.alerts), 2)

    def test_skips_non_actionable_flags(self):
        """unit_mismatch, physiologic_impossible, low_confidence ไม่สร้าง alert."""
        results = [
            _make_lab_result("Test", 5.0, "??", "1-10",
                             flags=["unit_mismatch", "physiologic_impossible",
                                    "low_confidence"]),
        ]
        report = _make_lab_report(results=results)
        summary = check_lab_alerts(report)
        self.assertEqual(len(summary.alerts), 0)

    def test_document_id_truncated(self):
        report = _make_lab_report(doc_id="abcdefghijklmnop")
        summary = check_lab_alerts(report)
        self.assertEqual(summary.document_id, "abcdefgh")

    def test_specimen_date_preserved(self):
        report = _make_lab_report(specimen_date="2026-03-20")
        summary = check_lab_alerts(report)
        self.assertEqual(summary.specimen_date, "2026-03-20")


class TestAlertSummaryProperties(unittest.TestCase):
    """ทดสอบ AlertSummary properties."""

    def test_empty_alerts(self):
        s = AlertSummary(document_id="test1234", specimen_date="2026-01-01")
        self.assertFalse(s.has_critical)
        self.assertFalse(s.has_warning)
        self.assertEqual(s.critical_count, 0)
        self.assertEqual(s.warning_count, 0)
        self.assertFalse(s.should_notify_user)
        self.assertFalse(s.should_notify_admin)

    def test_with_critical(self):
        s = AlertSummary(
            document_id="test1234",
            specimen_date="2026-01-01",
            alerts=[
                LabAlert("K", 7.0, "mmol/L", "3.5-5.0",
                         AlertLevel.CRITICAL, "critical_high", "สูงระดับวิกฤต"),
            ],
        )
        self.assertTrue(s.has_critical)
        self.assertTrue(s.should_notify_admin)

    def test_warning_only(self):
        s = AlertSummary(
            document_id="test1234",
            specimen_date="2026-01-01",
            alerts=[
                LabAlert("Cr", 1.8, "mg/dL", "0.6-1.2",
                         AlertLevel.WARNING, "abnormal_high", "สูงกว่าปกติ"),
            ],
        )
        self.assertTrue(s.has_warning)
        self.assertFalse(s.has_critical)
        self.assertTrue(s.should_notify_user)
        self.assertFalse(s.should_notify_admin)


class TestFormatUserAlert(unittest.TestCase):
    """ทดสอบข้อความแจ้งเตือนสำหรับผู้ใช้."""

    def test_empty_alerts(self):
        s = AlertSummary(document_id="test1234", specimen_date="2026-01-01")
        self.assertEqual(format_user_alert_th(s), "")

    def test_critical_header(self):
        s = AlertSummary(
            document_id="test1234",
            specimen_date="2026-01-15",
            total_results=5,
            alerts=[
                LabAlert("Potassium", 7.0, "mmol/L", "3.5-5.0",
                         AlertLevel.CRITICAL, "critical_high", "สูงระดับวิกฤต"),
            ],
        )
        text = format_user_alert_th(s)
        self.assertIn("🔴", text)
        self.assertIn("วิกฤต", text)
        self.assertIn("Potassium", text)
        self.assertIn("7.0", text)
        self.assertIn("3.5-5.0", text)
        self.assertIn("test1234", text)
        self.assertIn("2026-01-15", text)
        self.assertIn("รอการยืนยันโดยแพทย์", text)

    def test_warning_header(self):
        s = AlertSummary(
            document_id="abcd5678",
            specimen_date="2026-02-01",
            total_results=3,
            alerts=[
                LabAlert("Creatinine", 1.8, "mg/dL", "0.6-1.2",
                         AlertLevel.WARNING, "abnormal_high", "สูงกว่าปกติ"),
            ],
        )
        text = format_user_alert_th(s)
        self.assertIn("🟡", text)
        self.assertIn("ผิดปกติ", text)
        self.assertNotIn("วิกฤต", text.split("\n")[0])  # หัวข้อไม่ใช่ critical

    def test_unknown_date_hidden(self):
        s = AlertSummary(
            document_id="test1234",
            specimen_date="unknown",
            alerts=[
                LabAlert("K", 7.0, "mmol/L", "3.5-5.0",
                         AlertLevel.CRITICAL, "critical_high", "สูงระดับวิกฤต"),
            ],
        )
        text = format_user_alert_th(s)
        self.assertNotIn("unknown", text)

    def test_summary_counts(self):
        s = AlertSummary(
            document_id="test1234",
            specimen_date="2026-01-01",
            total_results=10,
            alerts=[
                LabAlert("K", 7.0, "mmol/L", "3.5-5.0",
                         AlertLevel.CRITICAL, "critical_high", "สูงระดับวิกฤต"),
                LabAlert("BUN", 25.0, "mg/dL", "7-20",
                         AlertLevel.WARNING, "abnormal_high", "สูงกว่าปกติ"),
                LabAlert("Hgb", 10.0, "g/dL", "12-16",
                         AlertLevel.WARNING, "abnormal_low", "ต่ำกว่าปกติ"),
            ],
        )
        text = format_user_alert_th(s)
        self.assertIn("วิกฤต 1 รายการ", text)
        self.assertIn("ผิดปกติ 2 รายการ", text)
        self.assertIn("ทั้งหมด 10 รายการ", text)

    def test_lab_command_hint(self):
        s = AlertSummary(
            document_id="test1234",
            specimen_date="2026-01-01",
            alerts=[
                LabAlert("K", 7.0, "mmol/L", "3.5-5.0",
                         AlertLevel.CRITICAL, "critical_high", "สูงระดับวิกฤต"),
            ],
        )
        text = format_user_alert_th(s)
        self.assertIn("ผลแล็บ test1234", text)

    def test_no_patient_data(self):
        """ข้อความต้องไม่มีข้อมูลผู้ป่วย."""
        s = AlertSummary(
            document_id="test1234",
            specimen_date="2026-01-01",
            alerts=[
                LabAlert("K", 7.0, "mmol/L", "3.5-5.0",
                         AlertLevel.CRITICAL, "critical_high", "สูงระดับวิกฤต"),
            ],
        )
        text = format_user_alert_th(s)
        # ต้องไม่มีชื่อ, HN, userId
        self.assertNotIn("patient", text.lower())
        self.assertNotIn("hn", text.lower().split())


class TestFormatAdminAlert(unittest.TestCase):
    """ทดสอบข้อความแจ้งเตือนสำหรับ admin."""

    def test_no_critical_returns_empty(self):
        """ไม่มี critical → ไม่แจ้ง admin."""
        s = AlertSummary(
            document_id="test1234",
            specimen_date="2026-01-01",
            alerts=[
                LabAlert("BUN", 25.0, "mg/dL", "7-20",
                         AlertLevel.WARNING, "abnormal_high", "สูงกว่าปกติ"),
            ],
        )
        self.assertEqual(format_admin_alert_th(s), "")

    def test_critical_alert(self):
        s = AlertSummary(
            document_id="test1234",
            specimen_date="2026-01-15",
            alerts=[
                LabAlert("Potassium", 7.0, "mmol/L", "3.5-5.0",
                         AlertLevel.CRITICAL, "critical_high", "สูงระดับวิกฤต"),
                LabAlert("BUN", 25.0, "mg/dL", "7-20",
                         AlertLevel.WARNING, "abnormal_high", "สูงกว่าปกติ"),
            ],
        )
        text = format_admin_alert_th(s, owner_hash_prefix="abc12345")
        self.assertIn("[ADMIN]", text)
        self.assertIn("Potassium", text)
        self.assertIn("abc12345", text)
        # admin alert ต้องไม่แสดง warning items
        self.assertNotIn("BUN", text)

    def test_approve_command_hint(self):
        s = AlertSummary(
            document_id="test1234",
            specimen_date="2026-01-01",
            alerts=[
                LabAlert("K", 7.0, "mmol/L", "3.5-5.0",
                         AlertLevel.CRITICAL, "critical_high", "สูงระดับวิกฤต"),
            ],
        )
        text = format_admin_alert_th(s)
        self.assertIn("ยืนยันผลแล็บ test1234", text)
        self.assertIn("ผลแล็บ test1234", text)

    def test_no_owner_hash(self):
        s = AlertSummary(
            document_id="test1234",
            specimen_date="2026-01-01",
            alerts=[
                LabAlert("K", 7.0, "mmol/L", "3.5-5.0",
                         AlertLevel.CRITICAL, "critical_high", "สูงระดับวิกฤต"),
            ],
        )
        text = format_admin_alert_th(s, owner_hash_prefix="")
        self.assertNotIn("ผู้ส่ง:", text)


class TestEdgeCases(unittest.TestCase):
    """ทดสอบกรณีพิเศษ."""

    def test_empty_flags_list(self):
        results = [_make_lab_result(flags=[])]
        report = _make_lab_report(results=results)
        summary = check_lab_alerts(report)
        self.assertEqual(len(summary.alerts), 0)

    def test_all_critical(self):
        """ผลตรวจทุกตัวเป็น critical."""
        results = [
            _make_lab_result("K", 7.5, "mmol/L", "3.5-5.0",
                             flags=["critical_high"]),
            _make_lab_result("Na", 115.0, "mmol/L", "136-145",
                             flags=["critical_low"]),
            _make_lab_result("Ca", 15.0, "mg/dL", "8.5-10.5",
                             flags=["critical_high"]),
        ]
        report = _make_lab_report(results=results)
        summary = check_lab_alerts(report)
        self.assertEqual(summary.critical_count, 3)
        self.assertEqual(summary.warning_count, 0)
        self.assertTrue(summary.should_notify_admin)

    def test_very_long_document_id(self):
        report = _make_lab_report(doc_id="a" * 100)
        summary = check_lab_alerts(report)
        self.assertEqual(len(summary.document_id), 8)

    def test_short_document_id(self):
        report = _make_lab_report(doc_id="abc")
        summary = check_lab_alerts(report)
        self.assertEqual(summary.document_id, "abc")

    def test_format_handles_special_chars(self):
        """ค่าที่มีตัวอักษรพิเศษ."""
        s = AlertSummary(
            document_id="test<>\"'",
            specimen_date="2026-01-01",
            alerts=[
                LabAlert("K+", 7.0, "mmol/L", "3.5-5.0",
                         AlertLevel.CRITICAL, "critical_high", "สูงระดับวิกฤต"),
            ],
        )
        text = format_user_alert_th(s)
        self.assertIn("K+", text)


if __name__ == "__main__":
    unittest.main()
