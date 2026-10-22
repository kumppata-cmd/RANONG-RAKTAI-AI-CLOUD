#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for Lab Tracking & Trend System.
ใช้ข้อมูล DEMO/สังเคราะห์เท่านั้น — ไม่ใช้ข้อมูลผู้ป่วยจริง
"""

import unittest
from datetime import datetime, timedelta

from lab_tracker import (
    AlertSeverity,
    LabTrend,
    TrendAlert,
    TrendAnalysis,
    TrendDirection,
    TrendPoint,
    TrackingPeriod,
    analyze_trends,
    calculate_trend_direction,
    check_trend_alerts,
    collect_trend_points,
)


# ═══════════════════════════════════════════════════════════
# Helper — สร้างข้อมูลจำลอง
# ═══════════════════════════════════════════════════════════

def make_lab_report(
    doc_id: str,
    date: str,
    results: list[dict],
    status: str = "pending_review",
) -> dict:
    """สร้าง lab report dict จำลอง."""
    return {
        "source_document_id": doc_id,
        "specimen_date": date,
        "status": status,
        "results": results,
    }


def make_result(analyte: str, value: float, unit: str = "mg/dL") -> dict:
    return {"analyte": analyte, "value": value, "unit": unit}


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════
# TrendPoint
# ═══════════════════════════════════════════════════════════

class TestTrendPoint(unittest.TestCase):

    def test_basic_creation(self):
        p = TrendPoint("2025-01-15", 1.2, "mg/dL", "doc1")
        self.assertEqual(p.date, "2025-01-15")
        self.assertEqual(p.value, 1.2)
        self.assertEqual(p.unit, "mg/dL")
        self.assertEqual(p.source_document_id, "doc1")
        self.assertEqual(p.status, "pending_review")

    def test_to_dict(self):
        p = TrendPoint("2025-01-15", 1.2, "mg/dL", "doc1", "approved")
        d = p.to_dict()
        self.assertEqual(d["date"], "2025-01-15")
        self.assertEqual(d["value"], 1.2)
        self.assertEqual(d["status"], "approved")


# ═══════════════════════════════════════════════════════════
# Collect Trend Points
# ═══════════════════════════════════════════════════════════

class TestCollectTrendPoints(unittest.TestCase):

    def test_basic_collection(self):
        reports = [
            make_lab_report("d1", "2025-01-01", [make_result("Creatinine", 1.2)]),
            make_lab_report("d2", "2025-02-01", [make_result("Creatinine", 1.5)]),
        ]
        points = collect_trend_points(reports, "Creatinine")
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].value, 1.2)
        self.assertEqual(points[1].value, 1.5)

    def test_sorted_by_date(self):
        reports = [
            make_lab_report("d2", "2025-03-01", [make_result("eGFR", 50)]),
            make_lab_report("d1", "2025-01-01", [make_result("eGFR", 60)]),
            make_lab_report("d3", "2025-02-01", [make_result("eGFR", 55)]),
        ]
        points = collect_trend_points(reports, "eGFR")
        self.assertEqual(len(points), 3)
        self.assertEqual(points[0].date, "2025-01-01")
        self.assertEqual(points[1].date, "2025-02-01")
        self.assertEqual(points[2].date, "2025-03-01")

    def test_filters_correct_analyte(self):
        reports = [
            make_lab_report("d1", "2025-01-01", [
                make_result("Creatinine", 1.2),
                make_result("eGFR", 60),
            ]),
        ]
        cr_points = collect_trend_points(reports, "Creatinine")
        self.assertEqual(len(cr_points), 1)
        self.assertEqual(cr_points[0].value, 1.2)

        egfr_points = collect_trend_points(reports, "eGFR")
        self.assertEqual(len(egfr_points), 1)
        self.assertEqual(egfr_points[0].value, 60)

    def test_no_matching_analyte(self):
        reports = [
            make_lab_report("d1", "2025-01-01", [make_result("Creatinine", 1.2)]),
        ]
        points = collect_trend_points(reports, "Albumin")
        self.assertEqual(len(points), 0)

    def test_invalid_value_skipped(self):
        reports = [
            make_lab_report("d1", "2025-01-01", [
                {"analyte": "Creatinine", "value": "N/A", "unit": "mg/dL"},
            ]),
        ]
        points = collect_trend_points(reports, "Creatinine")
        self.assertEqual(len(points), 0)

    def test_empty_reports(self):
        points = collect_trend_points([], "Creatinine")
        self.assertEqual(len(points), 0)

    def test_preserves_doc_id_and_status(self):
        reports = [
            make_lab_report("doc-abc", "2025-01-01",
                            [make_result("Creatinine", 1.2)], "approved"),
        ]
        points = collect_trend_points(reports, "Creatinine")
        self.assertEqual(points[0].source_document_id, "doc-abc")
        self.assertEqual(points[0].status, "approved")


# ═══════════════════════════════════════════════════════════
# Calculate Trend Direction
# ═══════════════════════════════════════════════════════════

class TestCalculateTrendDirection(unittest.TestCase):

    def test_insufficient_data_zero_points(self):
        direction, pct = calculate_trend_direction([])
        self.assertEqual(direction, TrendDirection.INSUFFICIENT_DATA.value)
        self.assertEqual(pct, 0.0)

    def test_insufficient_data_one_point(self):
        points = [TrendPoint("2025-01-01", 1.2, "mg/dL", "d1")]
        direction, pct = calculate_trend_direction(points)
        self.assertEqual(direction, TrendDirection.INSUFFICIENT_DATA.value)

    def test_stable_no_change(self):
        points = [
            TrendPoint("2025-01-01", 1.2, "mg/dL", "d1"),
            TrendPoint("2025-02-01", 1.2, "mg/dL", "d2"),
        ]
        direction, pct = calculate_trend_direction(points)
        self.assertEqual(direction, TrendDirection.STABLE.value)
        self.assertAlmostEqual(pct, 0.0)

    def test_stable_small_change(self):
        points = [
            TrendPoint("2025-01-01", 1.20, "mg/dL", "d1"),
            TrendPoint("2025-02-01", 1.24, "mg/dL", "d2"),
        ]
        direction, pct = calculate_trend_direction(points)
        self.assertEqual(direction, TrendDirection.STABLE.value)

    def test_increasing(self):
        points = [
            TrendPoint("2025-01-01", 1.0, "mg/dL", "d1"),
            TrendPoint("2025-02-01", 1.5, "mg/dL", "d2"),
        ]
        direction, pct = calculate_trend_direction(points)
        self.assertEqual(direction, TrendDirection.INCREASING.value)
        self.assertAlmostEqual(pct, 50.0)

    def test_decreasing(self):
        points = [
            TrendPoint("2025-01-01", 2.0, "mg/dL", "d1"),
            TrendPoint("2025-02-01", 1.0, "mg/dL", "d2"),
        ]
        direction, pct = calculate_trend_direction(points)
        self.assertEqual(direction, TrendDirection.DECREASING.value)
        self.assertAlmostEqual(pct, -50.0)

    def test_first_value_zero(self):
        points = [
            TrendPoint("2025-01-01", 0.0, "mg/dL", "d1"),
            TrendPoint("2025-02-01", 1.0, "mg/dL", "d2"),
        ]
        direction, pct = calculate_trend_direction(points)
        self.assertEqual(direction, TrendDirection.INCREASING.value)

    def test_both_zero(self):
        points = [
            TrendPoint("2025-01-01", 0.0, "mg/dL", "d1"),
            TrendPoint("2025-02-01", 0.0, "mg/dL", "d2"),
        ]
        direction, pct = calculate_trend_direction(points)
        self.assertEqual(direction, TrendDirection.STABLE.value)

    def test_custom_threshold(self):
        points = [
            TrendPoint("2025-01-01", 1.0, "mg/dL", "d1"),
            TrendPoint("2025-02-01", 1.08, "mg/dL", "d2"),
        ]
        # Default 5% threshold → stable (8%)
        direction, _ = calculate_trend_direction(points)
        self.assertEqual(direction, TrendDirection.INCREASING.value)

        # Higher threshold → stable
        direction, _ = calculate_trend_direction(points, threshold_percent=10.0)
        self.assertEqual(direction, TrendDirection.STABLE.value)

    def test_uses_first_and_last(self):
        """ใช้จุดแรกและจุดสุดท้ายเท่านั้น ไม่ใช่จุดกลาง."""
        points = [
            TrendPoint("2025-01-01", 1.0, "mg/dL", "d1"),
            TrendPoint("2025-02-01", 5.0, "mg/dL", "d2"),  # spike กลาง
            TrendPoint("2025-03-01", 1.2, "mg/dL", "d3"),
        ]
        direction, pct = calculate_trend_direction(points)
        self.assertEqual(direction, TrendDirection.INCREASING.value)
        self.assertAlmostEqual(pct, 20.0)


# ═══════════════════════════════════════════════════════════
# Check Trend Alerts
# ═══════════════════════════════════════════════════════════

class TestCheckTrendAlerts(unittest.TestCase):

    def test_no_alert_stable(self):
        alerts = check_trend_alerts(
            "Creatinine", TrendDirection.STABLE.value, 3.0, "quarterly"
        )
        self.assertEqual(len(alerts), 0)

    def test_no_alert_unknown_analyte(self):
        alerts = check_trend_alerts(
            "UnknownTest", TrendDirection.INCREASING.value, 50.0, "quarterly"
        )
        self.assertEqual(len(alerts), 0)

    def test_creatinine_warning(self):
        # Cr increase 35% → warning (threshold 30%)
        alerts = check_trend_alerts(
            "Creatinine", TrendDirection.INCREASING.value, 35.0, "quarterly"
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.WARNING.value)
        self.assertIn("Creatinine", alerts[0].message)

    def test_creatinine_critical(self):
        # Cr increase 55% → critical (threshold 50%)
        alerts = check_trend_alerts(
            "Creatinine", TrendDirection.INCREASING.value, 55.0, "quarterly"
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.CRITICAL.value)

    def test_creatinine_decrease_no_alert(self):
        # Cr decreasing is not concerning
        alerts = check_trend_alerts(
            "Creatinine", TrendDirection.DECREASING.value, -40.0, "quarterly"
        )
        self.assertEqual(len(alerts), 0)

    def test_egfr_warning(self):
        # eGFR decrease 30% → warning (threshold 25%)
        alerts = check_trend_alerts(
            "eGFR", TrendDirection.DECREASING.value, -30.0, "quarterly"
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.WARNING.value)

    def test_egfr_critical(self):
        # eGFR decrease 45% → critical (threshold 40%)
        alerts = check_trend_alerts(
            "eGFR", TrendDirection.DECREASING.value, -45.0, "quarterly"
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.CRITICAL.value)

    def test_egfr_increase_no_alert(self):
        # eGFR increasing is not concerning
        alerts = check_trend_alerts(
            "eGFR", TrendDirection.INCREASING.value, 30.0, "quarterly"
        )
        self.assertEqual(len(alerts), 0)

    def test_hemoglobin_warning(self):
        alerts = check_trend_alerts(
            "Hemoglobin", TrendDirection.DECREASING.value, -22.0, "quarterly"
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.WARNING.value)

    def test_potassium_critical(self):
        alerts = check_trend_alerts(
            "Potassium", TrendDirection.INCREASING.value, 45.0, "quarterly"
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.CRITICAL.value)

    def test_albumin_warning(self):
        alerts = check_trend_alerts(
            "Albumin", TrendDirection.DECREASING.value, -25.0, "quarterly"
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.WARNING.value)

    def test_ktv_critical(self):
        alerts = check_trend_alerts(
            "Kt/V", TrendDirection.DECREASING.value, -30.0, "quarterly"
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, AlertSeverity.CRITICAL.value)

    def test_below_threshold_no_alert(self):
        # Cr increase only 20% → below 30% threshold → no alert
        alerts = check_trend_alerts(
            "Creatinine", TrendDirection.INCREASING.value, 20.0, "quarterly"
        )
        self.assertEqual(len(alerts), 0)

    def test_alert_has_recommendation(self):
        alerts = check_trend_alerts(
            "Creatinine", TrendDirection.INCREASING.value, 35.0, "quarterly"
        )
        self.assertTrue(alerts[0].recommendation)

    def test_alert_preserves_period(self):
        alerts = check_trend_alerts(
            "Creatinine", TrendDirection.INCREASING.value, 35.0, "monthly"
        )
        self.assertEqual(alerts[0].period, "monthly")


# ═══════════════════════════════════════════════════════════
# Analyze Trends (Full Pipeline)
# ═══════════════════════════════════════════════════════════

class TestAnalyzeTrends(unittest.TestCase):

    def test_empty_reports(self):
        result = analyze_trends([], period="quarterly", owner_hash="hash123")
        self.assertEqual(len(result.trends), 0)
        self.assertEqual(len(result.alerts), 0)
        self.assertEqual(result.owner_hash, "hash123")

    def test_single_report_single_analyte(self):
        reports = [
            make_lab_report("d1", days_ago(10), [make_result("Creatinine", 1.2)]),
        ]
        result = analyze_trends(reports, period="monthly", owner_hash="h1",
                                reference_date=today_str())
        self.assertEqual(len(result.trends), 1)
        self.assertEqual(result.trends[0].analyte, "Creatinine")
        self.assertEqual(result.trends[0].direction,
                         TrendDirection.INSUFFICIENT_DATA.value)

    def test_two_reports_increasing(self):
        reports = [
            make_lab_report("d1", days_ago(20), [make_result("Creatinine", 1.0)]),
            make_lab_report("d2", days_ago(5), [make_result("Creatinine", 1.5)]),
        ]
        result = analyze_trends(reports, period="monthly", owner_hash="h1",
                                reference_date=today_str())
        cr = [t for t in result.trends if t.analyte == "Creatinine"]
        self.assertEqual(len(cr), 1)
        self.assertEqual(cr[0].direction, TrendDirection.INCREASING.value)
        self.assertAlmostEqual(cr[0].change_percent, 50.0)

    def test_multiple_analytes(self):
        reports = [
            make_lab_report("d1", days_ago(20), [
                make_result("Creatinine", 1.0),
                make_result("eGFR", 60, "mL/min"),
            ]),
            make_lab_report("d2", days_ago(5), [
                make_result("Creatinine", 1.0),
                make_result("eGFR", 60, "mL/min"),
            ]),
        ]
        result = analyze_trends(reports, period="monthly", owner_hash="h1",
                                reference_date=today_str())
        self.assertEqual(len(result.trends), 2)
        analytes = {t.analyte for t in result.trends}
        self.assertIn("Creatinine", analytes)
        self.assertIn("eGFR", analytes)

    def test_filters_by_period(self):
        # Report 200 days ago — outside monthly window
        reports = [
            make_lab_report("d1", days_ago(200), [make_result("Creatinine", 1.0)]),
            make_lab_report("d2", days_ago(5), [make_result("Creatinine", 1.5)]),
        ]
        result = analyze_trends(reports, period="monthly", owner_hash="h1",
                                reference_date=today_str())
        # Only 1 point within monthly window → insufficient data
        cr = [t for t in result.trends if t.analyte == "Creatinine"]
        self.assertEqual(len(cr), 1)
        self.assertEqual(len(cr[0].points), 1)

    def test_quarterly_includes_90_days(self):
        reports = [
            make_lab_report("d1", days_ago(85), [make_result("Creatinine", 1.0)]),
            make_lab_report("d2", days_ago(5), [make_result("Creatinine", 1.3)]),
        ]
        result = analyze_trends(reports, period="quarterly", owner_hash="h1",
                                reference_date=today_str())
        cr = [t for t in result.trends if t.analyte == "Creatinine"]
        self.assertEqual(len(cr[0].points), 2)

    def test_annual_period(self):
        reports = [
            make_lab_report("d1", days_ago(300), [make_result("Creatinine", 1.0)]),
            make_lab_report("d2", days_ago(5), [make_result("Creatinine", 2.0)]),
        ]
        result = analyze_trends(reports, period="annual", owner_hash="h1",
                                reference_date=today_str())
        cr = [t for t in result.trends if t.analyte == "Creatinine"]
        self.assertEqual(len(cr[0].points), 2)
        self.assertEqual(cr[0].direction, TrendDirection.INCREASING.value)

    def test_alerts_collected(self):
        reports = [
            make_lab_report("d1", days_ago(60), [make_result("Creatinine", 1.0)]),
            make_lab_report("d2", days_ago(5), [make_result("Creatinine", 1.6)]),
        ]
        result = analyze_trends(reports, period="quarterly", owner_hash="h1",
                                reference_date=today_str())
        # Cr increased 60% → critical
        self.assertTrue(result.has_alerts())
        self.assertTrue(len(result.critical_alerts()) > 0)

    def test_no_alerts_stable(self):
        reports = [
            make_lab_report("d1", days_ago(60), [make_result("Creatinine", 1.2)]),
            make_lab_report("d2", days_ago(5), [make_result("Creatinine", 1.2)]),
        ]
        result = analyze_trends(reports, period="quarterly", owner_hash="h1",
                                reference_date=today_str())
        self.assertFalse(result.has_alerts())

    def test_to_dict(self):
        reports = [
            make_lab_report("d1", days_ago(20), [make_result("Creatinine", 1.2)]),
        ]
        result = analyze_trends(reports, period="monthly", owner_hash="hash123",
                                reference_date=today_str())
        d = result.to_dict()
        self.assertIn("trends", d)
        self.assertIn("alerts", d)
        self.assertIn("period", d)
        self.assertEqual(d["owner_hash_prefix"], "hash123"[:8])
        self.assertIn("disclaimer", d)

    def test_disclaimer_always_present(self):
        result = analyze_trends([], period="monthly", owner_hash="h1")
        self.assertIn("ปรึกษาแพทย์", result.disclaimer)


# ═══════════════════════════════════════════════════════════
# Summary Text Generation
# ═══════════════════════════════════════════════════════════

class TestSummaryGeneration(unittest.TestCase):

    def test_empty_summary(self):
        result = analyze_trends([], period="quarterly", owner_hash="h1",
                                reference_date="2025-06-01")
        text = result.summary_text_th()
        self.assertIn("ไม่พบข้อมูล", text)
        self.assertIn("ปรึกษาแพทย์", text)

    def test_summary_with_data(self):
        reports = [
            make_lab_report("d1", days_ago(60), [
                make_result("Creatinine", 1.0),
                make_result("eGFR", 60, "mL/min"),
            ]),
            make_lab_report("d2", days_ago(5), [
                make_result("Creatinine", 1.0),
                make_result("eGFR", 60, "mL/min"),
            ]),
        ]
        result = analyze_trends(reports, period="quarterly", owner_hash="h1",
                                reference_date=today_str())
        text = result.summary_text_th()
        self.assertIn("สรุปแนวโน้ม", text)
        self.assertIn("แนวโน้มรายตัว", text)
        self.assertIn("Creatinine", text)
        self.assertIn("eGFR", text)

    def test_summary_with_critical_alert(self):
        reports = [
            make_lab_report("d1", days_ago(60), [make_result("Creatinine", 1.0)]),
            make_lab_report("d2", days_ago(5), [make_result("Creatinine", 1.6)]),
        ]
        result = analyze_trends(reports, period="quarterly", owner_hash="h1",
                                reference_date=today_str())
        text = result.summary_text_th()
        self.assertIn("🔴", text)  # critical section
        self.assertIn("Creatinine", text)

    def test_summary_with_warning(self):
        reports = [
            make_lab_report("d1", days_ago(60), [make_result("Creatinine", 1.0)]),
            make_lab_report("d2", days_ago(5), [make_result("Creatinine", 1.35)]),
        ]
        result = analyze_trends(reports, period="quarterly", owner_hash="h1",
                                reference_date=today_str())
        text = result.summary_text_th()
        self.assertIn("🟡", text)  # warning section

    def test_summary_period_name_th(self):
        result = analyze_trends([], period="monthly", owner_hash="h1",
                                reference_date="2025-06-01")
        text = result.summary_text_th()
        self.assertIn("รายเดือน", text)

    def test_summary_single_point(self):
        reports = [
            make_lab_report("d1", days_ago(10), [make_result("Creatinine", 1.2)]),
        ]
        result = analyze_trends(reports, period="monthly", owner_hash="h1",
                                reference_date=today_str())
        text = result.summary_text_th()
        self.assertIn("ไม่มีข้อมูลเปรียบเทียบ", text)


# ═══════════════════════════════════════════════════════════
# TrendAlert serialization
# ═══════════════════════════════════════════════════════════

class TestTrendAlertSerialization(unittest.TestCase):

    def test_to_dict(self):
        alert = TrendAlert(
            analyte="Creatinine",
            severity="critical",
            message="ค่า Creatinine เพิ่มขึ้น",
            direction="increasing",
            change_percent=55.123,
            period="quarterly",
            recommendation="ปรึกษาแพทย์",
        )
        d = alert.to_dict()
        self.assertEqual(d["analyte"], "Creatinine")
        self.assertEqual(d["severity"], "critical")
        self.assertEqual(d["change_percent"], 55.1)  # rounded


# ═══════════════════════════════════════════════════════════
# LabTrend serialization
# ═══════════════════════════════════════════════════════════

class TestLabTrendSerialization(unittest.TestCase):

    def test_to_dict(self):
        trend = LabTrend(
            analyte="Creatinine",
            unit="mg/dL",
            points=[TrendPoint("2025-01-01", 1.2, "mg/dL", "d1")],
            direction="increasing",
            change_percent=25.678,
        )
        d = trend.to_dict()
        self.assertEqual(d["analyte"], "Creatinine")
        self.assertEqual(d["point_count"], 1)
        self.assertEqual(d["change_percent"], 25.7)


# ═══════════════════════════════════════════════════════════
# Safety & Privacy Tests
# ═══════════════════════════════════════════════════════════

class TestSafety(unittest.TestCase):

    def test_no_patient_identity(self):
        """TrendAnalysis ต้องไม่เก็บ patient identity — ใช้ owner_hash เท่านั้น."""
        result = analyze_trends([], period="monthly", owner_hash="abc123")
        d = result.to_dict()
        # owner_hash_prefix only — never full identity
        self.assertEqual(d["owner_hash_prefix"], "abc123")
        self.assertNotIn("patient_id", d)
        self.assertNotIn("patient_name", d)
        self.assertNotIn("hn", d)

    def test_disclaimer_present(self):
        """ผลวิเคราะห์ต้องมี disclaimer."""
        result = analyze_trends([], period="monthly", owner_hash="h1")
        self.assertIn("ปรึกษาแพทย์", result.disclaimer)

    def test_points_start_pending_review(self):
        """TrendPoint ค่าเริ่มต้นเป็น pending_review."""
        p = TrendPoint("2025-01-01", 1.2, "mg/dL", "d1")
        self.assertEqual(p.status, "pending_review")

    def test_no_treatment_recommendation(self):
        """Alerts ต้องไม่สั่งยาหรือสั่งการรักษา."""
        reports = [
            make_lab_report("d1", days_ago(60), [make_result("Creatinine", 1.0)]),
            make_lab_report("d2", days_ago(5), [make_result("Creatinine", 1.6)]),
        ]
        result = analyze_trends(reports, period="quarterly", owner_hash="h1",
                                reference_date=today_str())
        for alert in result.alerts:
            self.assertNotIn("สั่งยา", alert.recommendation)
            self.assertNotIn("เปลี่ยนยา", alert.recommendation)

    def test_summary_has_disclaimer(self):
        reports = [
            make_lab_report("d1", days_ago(60), [make_result("Creatinine", 1.0)]),
            make_lab_report("d2", days_ago(5), [make_result("Creatinine", 1.6)]),
        ]
        result = analyze_trends(reports, period="quarterly", owner_hash="h1",
                                reference_date=today_str())
        text = result.summary_text_th()
        self.assertIn("ไม่ใช่การวินิจฉัย", text)


# ═══════════════════════════════════════════════════════════
# Edge Cases
# ═══════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def test_all_periods_defined(self):
        for period in TrackingPeriod:
            result = analyze_trends([], period=period.value, owner_hash="h1")
            self.assertIsNotNone(result)

    def test_negative_values(self):
        # Some lab values can be negative (rare but possible)
        points = [
            TrendPoint("2025-01-01", -1.0, "mEq/L", "d1"),
            TrendPoint("2025-02-01", -2.0, "mEq/L", "d2"),
        ]
        direction, pct = calculate_trend_direction(points)
        # -1 to -2 = -100% change
        self.assertEqual(direction, TrendDirection.DECREASING.value)

    def test_very_large_values(self):
        points = [
            TrendPoint("2025-01-01", 1000.0, "mg/dL", "d1"),
            TrendPoint("2025-02-01", 2000.0, "mg/dL", "d2"),
        ]
        direction, pct = calculate_trend_direction(points)
        self.assertEqual(direction, TrendDirection.INCREASING.value)
        self.assertAlmostEqual(pct, 100.0)

    def test_many_points(self):
        reports = []
        for i in range(30):
            reports.append(make_lab_report(
                f"d{i}", days_ago(30 - i),
                [make_result("Creatinine", 1.0 + i * 0.01)]
            ))
        result = analyze_trends(reports, period="monthly", owner_hash="h1",
                                reference_date=today_str())
        cr = [t for t in result.trends if t.analyte == "Creatinine"]
        self.assertEqual(len(cr), 1)
        self.assertGreater(len(cr[0].points), 1)

    def test_reference_date_parameter(self):
        reports = [
            make_lab_report("d1", "2025-01-01", [make_result("Creatinine", 1.0)]),
            make_lab_report("d2", "2025-03-01", [make_result("Creatinine", 1.5)]),
        ]
        # Use reference date of 2025-03-15 with quarterly period
        result = analyze_trends(reports, period="quarterly", owner_hash="h1",
                                reference_date="2025-03-15")
        self.assertEqual(result.to_date, "2025-03-15")
        cr = [t for t in result.trends if t.analyte == "Creatinine"]
        self.assertEqual(len(cr[0].points), 2)


if __name__ == "__main__":
    unittest.main()
