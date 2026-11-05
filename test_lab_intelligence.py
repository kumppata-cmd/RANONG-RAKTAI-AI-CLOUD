#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for Lab Intelligence Agent + ESA-Hb Optimization"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from lab_intelligence_agent import (
    analyze_trend, analyze_esa_response, check_cross_correlations,
    suggest_next_labs, generate_intelligence_report, format_for_line,
    HB_TARGET_LOW, HB_TARGET_HIGH, HB_CRITICAL_LOW, HB_AVOID_ABOVE,
)


class TestTrendAnalysis(unittest.TestCase):

    def test_rising_trend(self):
        values = [{"value": 8.0, "date": "2026-10-01"},
                  {"value": 9.0, "date": "2026-10-15"},
                  {"value": 10.5, "date": "2026-11-01"}]
        t = analyze_trend(values, "Hb")
        self.assertEqual(t.direction, "rising")
        self.assertAlmostEqual(t.current_value, 10.5)
        self.assertGreater(t.change, 0)

    def test_falling_trend(self):
        values = [{"value": 11.0, "date": "2026-10-01"},
                  {"value": 9.5, "date": "2026-10-15"},
                  {"value": 7.8, "date": "2026-11-01"}]
        t = analyze_trend(values, "Hb")
        self.assertEqual(t.direction, "falling")

    def test_stable_trend(self):
        values = [{"value": 10.5, "date": "2026-10-01"},
                  {"value": 10.6, "date": "2026-11-01"}]
        t = analyze_trend(values, "Hb")
        self.assertEqual(t.direction, "stable")

    def test_single_value(self):
        values = [{"value": 9.5, "date": "2026-11-01"}]
        t = analyze_trend(values, "Hb")
        self.assertEqual(t.direction, "insufficient")

    def test_empty_values(self):
        t = analyze_trend([], "Hb")
        self.assertEqual(t.direction, "insufficient")

    def test_three_point_consecutive_rise(self):
        values = [{"value": 8.0}, {"value": 9.0}, {"value": 10.5}]
        t = analyze_trend(values, "Hb")
        self.assertIn("ต่อเนื่อง", t.assessment)


class TestESAOptimization(unittest.TestCase):

    def test_critical_low_hb(self):
        """Hb < 7 → transfuse"""
        r = analyze_esa_response(
            [{"value": 6.5, "date": "2026-11-01"}],
            esa_code="E4",
        )
        self.assertEqual(r.action, "transfuse")
        self.assertIn("ให้เลือด", r.recommendation)

    def test_low_hb_no_esa(self):
        """Hb < 10 + no ESA → start ESA"""
        r = analyze_esa_response(
            [{"value": 8.5, "date": "2026-11-01"}],
            esa_code="",
        )
        self.assertEqual(r.action, "start")

    def test_low_hb_iron_deficient(self):
        """Hb low + Ferritin low → iron first"""
        r = analyze_esa_response(
            [{"value": 9.0, "date": "2026-11-01"}],
            esa_code="E4", ferritin=100,
        )
        self.assertEqual(r.action, "iron_first")
        self.assertIn("IV iron", r.recommendation)

    def test_low_hb_falling_with_esa(self):
        """Hb falling despite ESA → increase"""
        r = analyze_esa_response(
            [{"value": 9.5, "date": "2026-10-01"},
             {"value": 8.8, "date": "2026-11-01"}],
            esa_code="E4",
        )
        self.assertEqual(r.action, "increase")

    def test_hb_in_target(self):
        """Hb 10-12 → maintain"""
        r = analyze_esa_response(
            [{"value": 10.5, "date": "2026-10-01"},
             {"value": 11.0, "date": "2026-11-01"}],
            esa_code="H4",
        )
        self.assertEqual(r.action, "maintain")
        self.assertIn("เป้าหมาย", r.recommendation)

    def test_hb_too_high(self):
        """Hb > 13 → decrease"""
        r = analyze_esa_response(
            [{"value": 12.0, "date": "2026-10-01"},
             {"value": 13.5, "date": "2026-11-01"}],
            esa_code="E4",
        )
        self.assertEqual(r.action, "decrease")

    def test_no_hb_data(self):
        """No Hb data → check"""
        r = analyze_esa_response([], esa_code="E4")
        self.assertEqual(r.action, "check")

    def test_hb_approaching_ceiling(self):
        """12 < Hb <= 13 + rising → decrease"""
        r = analyze_esa_response(
            [{"value": 11.5, "date": "2026-10-01"},
             {"value": 12.5, "date": "2026-11-01"}],
            esa_code="H4",
        )
        self.assertEqual(r.action, "decrease")

    def test_iron_status_assessment(self):
        """Iron status included in recommendation"""
        r = analyze_esa_response(
            [{"value": 9.0, "date": "2026-11-01"}],
            esa_code="E4", ferritin=350, tsat=25,
        )
        self.assertIn("Ferritin", r.iron_status)
        self.assertIn("TSAT", r.iron_status)


class TestCrossCorrelations(unittest.TestCase):

    def test_anemia_with_iron_deficiency(self):
        flags = check_cross_correlations({"hb": 8.5, "ferritin": 100})
        self.assertTrue(any("Iron" in f for f in flags))

    def test_hyperkalemia(self):
        flags = check_cross_correlations({"k": 6.8})
        self.assertTrue(any("K+" in f and "วิกฤต" in f for f in flags))

    def test_high_ca_po4_product(self):
        flags = check_cross_correlations({"ca": 10.5, "po4": 6.0})
        self.assertTrue(any("Ca×PO4" in f for f in flags))

    def test_malnutrition(self):
        flags = check_cross_correlations({"albumin": 2.8})
        self.assertTrue(any("Albumin" in f for f in flags))

    def test_normal_labs_no_flags(self):
        flags = check_cross_correlations({"hb": 11.0, "k": 4.5, "albumin": 4.0})
        self.assertEqual(len(flags), 0)


class TestNextLabSuggestions(unittest.TestCase):

    def test_hd_patient(self):
        suggestions = suggest_next_labs({"hd_per_week": 3, "esa_code": "E4"})
        self.assertGreater(len(suggestions), 0)
        texts = " ".join(suggestions)
        self.assertIn("CBC", texts)
        self.assertIn("Kt/V", texts)
        self.assertIn("ESA", texts)

    def test_non_hd_patient(self):
        suggestions = suggest_next_labs({"hd_per_week": 0})
        texts = " ".join(suggestions)
        self.assertIn("UACR", texts)


class TestIntelligenceReport(unittest.TestCase):

    def test_full_report(self):
        report = generate_intelligence_report(
            patient_hn="56001",
            patient_data={
                "patient_name": "นาย ปิยะ อะนะบุหรง",
                "esa_code": "E4",
                "esa_medication": "Epiao",
                "esa_dose": 4000,
                "hd_per_week": 3,
            },
            lab_history=[
                {"value": 9.0, "date": "2026-09-01"},
                {"value": 9.5, "date": "2026-10-01"},
                {"value": 10.2, "date": "2026-11-01"},
            ],
            latest_labs={"hb": 10.2, "k": 5.0, "albumin": 3.8},
        )
        self.assertEqual(report.patient_hn, "56001")
        self.assertGreater(len(report.trends), 0)
        self.assertIsNotNone(report.esa_recommendation)
        self.assertEqual(report.esa_recommendation.action, "maintain")
        self.assertGreater(len(report.summary_th), 100)

    def test_format_for_line(self):
        report = generate_intelligence_report(
            patient_hn="56001",
            patient_data={"esa_code": "E4", "hd_per_week": 3},
            lab_history=[{"value": 9.5, "date": "2026-11-01"}],
        )
        text = format_for_line(report)
        self.assertLessEqual(len(text), 2000)
        self.assertIn("56001", text)


class TestCKDProgression(unittest.TestCase):

    def test_rapid_decline(self):
        from lab_intelligence_agent import analyze_ckd_progression
        r = analyze_ckd_progression([
            {"date": "2026-01-01", "value": 40},
            {"date": "2026-07-01", "value": 35},
            {"date": "2027-01-01", "value": 28},
        ])
        self.assertEqual(r.decline_rate, "rapid")
        self.assertIsNotNone(r.egfr_slope)
        self.assertLess(r.egfr_slope, -5)

    def test_stable(self):
        from lab_intelligence_agent import analyze_ckd_progression
        r = analyze_ckd_progression([
            {"date": "2026-01-01", "value": 45},
            {"date": "2026-07-01", "value": 44},
            {"date": "2027-01-01", "value": 43},
        ])
        self.assertEqual(r.decline_rate, "stable")

    def test_improving(self):
        from lab_intelligence_agent import analyze_ckd_progression
        r = analyze_ckd_progression([
            {"date": "2026-01-01", "value": 30},
            {"date": "2027-01-01", "value": 35},
        ])
        self.assertEqual(r.decline_rate, "improving")

    def test_single_value(self):
        from lab_intelligence_agent import analyze_ckd_progression
        r = analyze_ckd_progression([{"date": "2026-01-01", "value": 25}])
        self.assertEqual(r.decline_rate, "insufficient")
        self.assertEqual(r.ckd_stage, "G4")

    def test_months_to_dialysis(self):
        from lab_intelligence_agent import analyze_ckd_progression
        r = analyze_ckd_progression([
            {"date": "2026-01-01", "value": 25},
            {"date": "2027-01-01", "value": 18},
        ])
        self.assertIsNotNone(r.months_to_dialysis)
        self.assertGreater(r.months_to_dialysis, 0)

    def test_ckd_stage_classification(self):
        from lab_intelligence_agent import _classify_ckd
        self.assertEqual(_classify_ckd(95), "G1")
        self.assertEqual(_classify_ckd(65), "G2")
        self.assertEqual(_classify_ckd(50), "G3a")
        self.assertEqual(_classify_ckd(35), "G3b")
        self.assertEqual(_classify_ckd(20), "G4")
        self.assertEqual(_classify_ckd(8), "G5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
