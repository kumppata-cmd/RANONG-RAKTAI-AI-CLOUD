#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Integration Tests — Kidney Clinical Intelligence Platform 2.0

ทดสอบการทำงานร่วมกันระหว่าง modules:
  - lab_models → lab_alert pipeline
  - lab_models → clinical_analyzer pipeline
  - lab_models → lab_tracker (collect_trend_points)
  - lab_models → medication_review
  - clinical_analyzer → clinical_reports
  - line_bot_enhancer command routing
  - lab_alert → format → push message criteria

ใช้ข้อมูล DEMO เท่านั้น — ไม่ใช้ข้อมูลผู้ป่วยจริง
"""

import os
import sys
import unittest

# ── Setup test environment ──
os.environ.setdefault("LINE_CHANNEL_SECRET", "test_secret_for_unit_test")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test_token_for_unit_test")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("ENABLE_AI_CLASSIFY", "false")
os.environ.setdefault("STORAGE_BACKEND", "local")

from lab_models import LabReport, LabResult
from lab_alert import (
    check_lab_alerts, format_user_alert_th, format_admin_alert_th, AlertLevel,
)
from clinical_analyzer import analyze_lab_report, ClinicalAnalysis


# ══════════════════════════════════════════════════════════════
# Helper
# ══════════════════════════════════════════════════════════════

def _result(analyte, value, unit, ref="N/A", flags=None, conf=0.95):
    return LabResult(
        analyte=analyte, value=value, unit=unit,
        reference_range=ref, flags=flags or [], confidence=conf,
        extraction_method="test",
    )


def _report(results, doc_id="demo-int-001", date="2025-03-15"):
    return LabReport(
        source_document_id=doc_id, specimen_date=date, results=results,
        extraction_method="test",
    )


# ══════════════════════════════════════════════════════════════
# 1. Lab → Alert Pipeline
# ══════════════════════════════════════════════════════════════

class TestLabToAlertPipeline(unittest.TestCase):
    """LabReport → check_lab_alerts → format."""

    def test_normal_report_no_alerts(self):
        report = _report([
            _result("Creatinine", 1.0, "mg/dL", "0.7-1.3"),
            _result("BUN", 15.0, "mg/dL", "7-20"),
        ])
        s = check_lab_alerts(report)
        self.assertEqual(len(s.alerts), 0)
        self.assertFalse(s.should_notify_user)
        self.assertFalse(s.should_notify_admin)

    def test_critical_triggers_admin_notify(self):
        report = _report([
            _result("Potassium", 7.5, "mmol/L", "3.5-5.0", ["critical_high"]),
        ])
        s = check_lab_alerts(report)
        self.assertTrue(s.has_critical)
        self.assertTrue(s.should_notify_admin)
        self.assertEqual(s.alerts[0].level, AlertLevel.CRITICAL)

    def test_warning_does_not_notify_admin(self):
        report = _report([
            _result("Creatinine", 2.5, "mg/dL", "0.7-1.3", ["abnormal_high"]),
        ])
        s = check_lab_alerts(report)
        self.assertTrue(s.should_notify_user)
        self.assertFalse(s.should_notify_admin)

    def test_mixed_sorted_critical_first(self):
        report = _report([
            _result("Creatinine", 2.5, "mg/dL", "0.7-1.3", ["abnormal_high"]),
            _result("Potassium", 7.5, "mmol/L", "3.5-5.0", ["critical_high"]),
            _result("BUN", 45.0, "mg/dL", "7-20", ["abnormal_high"]),
        ])
        s = check_lab_alerts(report)
        self.assertEqual(len(s.alerts), 3)
        self.assertEqual(s.alerts[0].level, AlertLevel.CRITICAL)
        self.assertEqual(s.critical_count, 1)
        self.assertEqual(s.warning_count, 2)

    def test_non_actionable_flags_skipped(self):
        report = _report([
            _result("Creatinine", 1.0, "mg/dL", "0.7-1.3",
                    ["unit_mismatch", "low_confidence"]),
        ])
        s = check_lab_alerts(report)
        self.assertEqual(len(s.alerts), 0)


# ══════════════════════════════════════════════════════════════
# 2. Alert → Format Pipeline
# ══════════════════════════════════════════════════════════════

class TestAlertFormatPipeline(unittest.TestCase):

    def test_user_alert_has_required_elements(self):
        report = _report([
            _result("Potassium", 7.5, "mmol/L", "3.5-5.0", ["critical_high"]),
            _result("Creatinine", 2.5, "mg/dL", "0.7-1.3", ["abnormal_high"]),
        ], doc_id="abcdef1234567890")
        s = check_lab_alerts(report)
        text = format_user_alert_th(s)

        self.assertIn("🔴", text)
        self.assertIn("Potassium", text)
        self.assertIn("7.5", text)
        self.assertIn("ค่าปกติ", text)
        self.assertIn("รอการยืนยันโดยแพทย์", text)
        self.assertIn("abcdef12", text)
        # ไม่มีข้อมูลผู้ป่วย
        self.assertNotIn("HN", text)

    def test_admin_alert_only_critical(self):
        report = _report([
            _result("Potassium", 7.5, "mmol/L", "3.5-5.0", ["critical_high"]),
            _result("Creatinine", 2.5, "mg/dL", "0.7-1.3", ["abnormal_high"]),
        ])
        s = check_lab_alerts(report)
        text = format_admin_alert_th(s, "abc12345")
        self.assertIn("[ADMIN]", text)
        self.assertIn("Potassium", text)
        self.assertNotIn("Creatinine", text)

    def test_empty_alerts_empty_format(self):
        report = _report([_result("BUN", 15.0, "mg/dL", "7-20")])
        s = check_lab_alerts(report)
        self.assertEqual(format_user_alert_th(s), "")
        self.assertEqual(format_admin_alert_th(s), "")

    def test_user_alert_under_line_limit(self):
        """ข้อความต้องไม่เกิน 5000 ตัวอักษร (LINE limit)."""
        results = [
            _result(f"Analyte{i}", 999.0, "unit", "0-1", ["critical_high"])
            for i in range(20)
        ]
        report = _report(results)
        s = check_lab_alerts(report)
        text = format_user_alert_th(s)
        self.assertLessEqual(len(text), 5000)


# ══════════════════════════════════════════════════════════════
# 3. Lab → Clinical Analyzer Pipeline
# ══════════════════════════════════════════════════════════════

class TestLabToClinicalPipeline(unittest.TestCase):

    def test_ckd_analysis(self):
        report = _report([
            _result("Creatinine", 3.5, "mg/dL", "0.7-1.3", ["abnormal_high"]),
            _result("BUN", 45.0, "mg/dL", "7-20", ["abnormal_high"]),
        ])
        analysis = analyze_lab_report(report)
        self.assertIsInstance(analysis, ClinicalAnalysis)
        self.assertEqual(analysis.source_document_id, "demo-int-001")

    def test_electrolyte_analysis(self):
        report = _report([
            _result("Potassium", 5.8, "mmol/L", "3.5-5.0", ["abnormal_high"]),
            _result("Sodium", 128.0, "mmol/L", "136-145", ["abnormal_low"]),
        ])
        analysis = analyze_lab_report(report)
        self.assertIsInstance(analysis, ClinicalAnalysis)
        # ต้องมี findings สำหรับ electrolyte
        self.assertGreater(len(analysis.findings), 0)

    def test_empty_report_no_crash(self):
        report = _report([])
        analysis = analyze_lab_report(report)
        self.assertIsInstance(analysis, ClinicalAnalysis)

    def test_analysis_findings_for_abnormal(self):
        """ผลแล็บผิดปกติ → ต้องมี findings."""
        report = _report([
            _result("Creatinine", 3.5, "mg/dL", "0.7-1.3", ["abnormal_high"]),
        ])
        analysis = analyze_lab_report(report)
        self.assertIsInstance(analysis, ClinicalAnalysis)
        # CKD findings ต้องมีเพราะ Creatinine สูง
        self.assertGreater(len(analysis.findings), 0)


# ══════════════════════════════════════════════════════════════
# 4. Clinical Analyzer → Clinical Reports Pipeline
# ══════════════════════════════════════════════════════════════

class TestClinicalToReportsPipeline(unittest.TestCase):

    def test_patient_report_generation(self):
        from clinical_reports import generate_patient_report, patient_report_text_th

        # สร้าง lab report dicts จำลอง
        lab_dicts = [{
            "source_document_id": "demo-001",
            "specimen_date": "2025-03-15",
            "results": [
                {"analyte": "Creatinine", "value": 3.5, "unit": "mg/dL",
                 "reference_range": "0.7-1.3", "flags": ["abnormal_high"]},
            ],
        }]
        # สร้าง clinical analysis dicts จำลอง
        analysis_dicts = [{
            "source_document_id": "demo-001",
            "specimen_date": "2025-03-15",
            "ckd_stage": "G4",
            "findings": [],
        }]

        kpi = generate_patient_report(lab_dicts, analysis_dicts, doc_count=5,
                                       owner_hash="testhash")
        self.assertIsNotNone(kpi)
        self.assertEqual(kpi.total_documents, 5)
        self.assertEqual(kpi.total_lab_reports, 1)

        # Generate text
        text = patient_report_text_th(kpi)
        self.assertIsInstance(text, str)
        self.assertGreater(len(text), 10)


# ══════════════════════════════════════════════════════════════
# 5. Lab → Tracker Pipeline
# ══════════════════════════════════════════════════════════════

class TestLabToTrackerPipeline(unittest.TestCase):

    def test_collect_trend_points(self):
        from lab_tracker import collect_trend_points, calculate_trend_direction

        reports = [
            {"source_document_id": "t001", "specimen_date": "2025-01-15",
             "results": [{"analyte": "Creatinine", "value": 1.5, "unit": "mg/dL"}]},
            {"source_document_id": "t002", "specimen_date": "2025-02-15",
             "results": [{"analyte": "Creatinine", "value": 1.8, "unit": "mg/dL"}]},
            {"source_document_id": "t003", "specimen_date": "2025-03-15",
             "results": [{"analyte": "Creatinine", "value": 2.2, "unit": "mg/dL"}]},
        ]
        points = collect_trend_points(reports, "Creatinine")
        self.assertEqual(len(points), 3)
        self.assertEqual(points[0].value, 1.5)
        self.assertEqual(points[2].value, 2.2)

        # ตรวจสอบ trend direction
        direction, change = calculate_trend_direction(points)
        self.assertIn(direction, ["increasing", "stable", "decreasing"])

    def test_empty_reports_no_crash(self):
        from lab_tracker import collect_trend_points
        points = collect_trend_points([], "Creatinine")
        self.assertEqual(len(points), 0)

    def test_analyte_not_found(self):
        from lab_tracker import collect_trend_points
        reports = [
            {"source_document_id": "t001", "specimen_date": "2025-01-15",
             "results": [{"analyte": "BUN", "value": 15.0, "unit": "mg/dL"}]},
        ]
        points = collect_trend_points(reports, "Creatinine")
        self.assertEqual(len(points), 0)


# ══════════════════════════════════════════════════════════════
# 6. Lab → Medication Review Pipeline
# ══════════════════════════════════════════════════════════════

class TestLabToMedicationPipeline(unittest.TestCase):

    def test_ckd_medication_review(self):
        from medication_review import review_medications, MedicationReviewResult

        result = review_medications(
            medications=["Metformin", "Ibuprofen"],
            lab_values={"Creatinine": 3.5, "Potassium": 5.8, "eGFR": 18.0},
            ckd_stage="G4",
            egfr=18.0,
        )
        self.assertIsInstance(result, MedicationReviewResult)
        # ต้องมี alert สำหรับ Ibuprofen (nephrotoxic) ใน CKD
        self.assertGreater(len(result.alerts), 0)

    def test_no_medications_no_crash(self):
        from medication_review import review_medications
        result = review_medications(
            medications=[],
            lab_values={"Creatinine": 1.0},
        )
        self.assertIsNotNone(result)

    def test_medication_review_text(self):
        from medication_review import review_medications, review_text_th
        result = review_medications(
            medications=["Metformin"],
            lab_values={"Creatinine": 3.5},
            ckd_stage="G4",
            egfr=18.0,
        )
        text = review_text_th(result)
        self.assertIsInstance(text, str)


# ══════════════════════════════════════════════════════════════
# 7. Command Routing Integration
# ══════════════════════════════════════════════════════════════

class TestCommandRoutingIntegration(unittest.TestCase):

    def test_all_command_variants_parse(self):
        from line_bot_enhancer import parse_command, CommandType

        cases = [
            ("ค้นหา CBC", CommandType.SEARCH),
            ("หา ผลเลือด", CommandType.SEARCH),
            ("search lab", CommandType.SEARCH),
            ("ผลแล็บล่าสุด", CommandType.LAB_RESULT),
            ("ผลแล็บ abc123", CommandType.LAB_RESULT),
            ("ช่วยเหลือ", CommandType.HELP),
            ("help", CommandType.HELP),
            ("สถิติ", CommandType.MY_STATS),
            ("ยืนยันผลแล็บ abc123", CommandType.APPROVE_LAB),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                p = parse_command(text)
                self.assertEqual(p.command_type, expected,
                                 f"'{text}' → {p.command_type}, expected {expected}")

    def test_smart_search_and_logic(self):
        from line_bot_enhancer import smart_search, parse_search_query

        docs = [
            {"doc_id": "1", "category": "lab_report",
             "original_filename": "CBC_result.pdf",
             "upload_date": "2025-03-15", "owner_hash": "test"},
            {"doc_id": "2", "category": "prescription",
             "original_filename": "drug_list.pdf",
             "upload_date": "2025-03-15", "owner_hash": "test"},
        ]
        # ค้นหาด้วย "CBC" ซึ่งตรงกับ filename
        search_filter = parse_search_query("CBC")
        results = smart_search(docs, search_filter, owner_hash="test")
        lab_ids = [r["doc_id"] for r in results]
        self.assertIn("1", lab_ids)
        # "drug" ต้องเจอ doc_id 2
        search_filter2 = parse_search_query("drug")
        results2 = smart_search(docs, search_filter2, owner_hash="test")
        lab_ids2 = [r["doc_id"] for r in results2]
        self.assertIn("2", lab_ids2)

    def test_help_admin_vs_user(self):
        from line_bot_enhancer import generate_help_text

        user_help = generate_help_text(is_admin=False, has_docs=True)
        admin_help = generate_help_text(is_admin=True, has_docs=True)
        self.assertIsInstance(user_help, str)
        self.assertIsInstance(admin_help, str)
        self.assertGreater(len(admin_help), len(user_help))


# ══════════════════════════════════════════════════════════════
# 8. End-to-End: Full Critical Lab Pipeline
# ══════════════════════════════════════════════════════════════

class TestEndToEndCriticalLab(unittest.TestCase):

    def test_full_critical_pipeline(self):
        """ผลแล็บวิกฤต → alert + format + clinical analysis ทั้งหมด."""
        results = [
            _result("Potassium", 7.8, "mmol/L", "3.5-5.0", ["critical_high"]),
            _result("Creatinine", 5.0, "mg/dL", "0.7-1.3", ["abnormal_high"]),
        ]
        report = _report(results, doc_id="e2e-crit-001")

        # Alert check
        summary = check_lab_alerts(report)
        self.assertTrue(summary.should_notify_user)
        self.assertTrue(summary.should_notify_admin)

        # Format
        user_text = format_user_alert_th(summary)
        self.assertIn("วิกฤต", user_text)
        self.assertLessEqual(len(user_text), 5000)

        admin_text = format_admin_alert_th(summary, "testhash")
        self.assertIn("ADMIN", admin_text)

        # Clinical analysis
        analysis = analyze_lab_report(report)
        self.assertIsInstance(analysis, ClinicalAnalysis)

    def test_full_normal_pipeline(self):
        """ผลแล็บปกติ → no alert, still has analysis."""
        report = _report([
            _result("Creatinine", 0.9, "mg/dL", "0.7-1.3"),
            _result("BUN", 14.0, "mg/dL", "7-20"),
        ], doc_id="e2e-norm-001")

        summary = check_lab_alerts(report)
        self.assertFalse(summary.should_notify_user)

        analysis = analyze_lab_report(report)
        self.assertIsInstance(analysis, ClinicalAnalysis)


if __name__ == "__main__":
    unittest.main()
