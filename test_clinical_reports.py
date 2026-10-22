#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for Clinical Reports & KPI Dashboard.
ใช้ข้อมูล DEMO/สังเคราะห์เท่านั้น — ไม่ใช้ข้อมูลผู้ป่วยจริง
"""

import unittest
from datetime import datetime

from clinical_reports import (
    AnalyteStats,
    ClinicKPI,
    PatientKPI,
    clinic_summary_text_th,
    generate_clinic_summary,
    generate_patient_report,
    patient_report_text_th,
    NORMAL_RANGES,
)


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

def make_lab_report(doc_id, date, results, status="pending_review"):
    return {
        "source_document_id": doc_id,
        "specimen_date": date,
        "status": status,
        "results": results,
    }


def make_result(analyte, value, unit="mg/dL"):
    return {"analyte": analyte, "value": value, "unit": unit}


def make_clinical_analysis(doc_id, date, ckd_stage="G3a", findings=None):
    return {
        "source_document_id": doc_id,
        "specimen_date": date,
        "ckd_stage": ckd_stage,
        "findings": findings or [],
    }


def make_finding(severity="warning", area="ckd_staging"):
    return {
        "area": area,
        "finding": "Test finding",
        "severity": severity,
        "recommendation": "ปรึกษาแพทย์",
    }


# ═══════════════════════════════════════════════════════════
# AnalyteStats
# ═══════════════════════════════════════════════════════════

class TestAnalyteStats(unittest.TestCase):

    def test_basic_creation(self):
        s = AnalyteStats(analyte="Creatinine", latest_value=1.2, count=3)
        self.assertEqual(s.analyte, "Creatinine")
        self.assertEqual(s.latest_value, 1.2)
        self.assertTrue(s.in_range)

    def test_to_dict(self):
        s = AnalyteStats(
            analyte="eGFR", latest_value=45, latest_date="2025-03-01",
            latest_unit="mL/min", min_value=40, max_value=60, count=5,
            in_range=False,
        )
        d = s.to_dict()
        self.assertEqual(d["analyte"], "eGFR")
        self.assertEqual(d["latest_value"], 45)
        self.assertFalse(d["in_range"])


# ═══════════════════════════════════════════════════════════
# PatientKPI
# ═══════════════════════════════════════════════════════════

class TestPatientKPI(unittest.TestCase):

    def test_basic_creation(self):
        kpi = PatientKPI(owner_hash="abc123")
        self.assertEqual(kpi.owner_hash, "abc123")
        self.assertTrue(kpi.report_date)  # auto-set
        self.assertIn("ปรึกษาแพทย์", kpi.disclaimer)

    def test_to_dict_no_identity(self):
        kpi = PatientKPI(owner_hash="full_hash_value_here")
        d = kpi.to_dict()
        self.assertEqual(d["owner_hash_prefix"], "full_has")
        self.assertNotIn("owner_hash", d)
        self.assertNotIn("patient_id", d)
        self.assertNotIn("patient_name", d)


# ═══════════════════════════════════════════════════════════
# Generate Patient Report
# ═══════════════════════════════════════════════════════════

class TestGeneratePatientReport(unittest.TestCase):

    def test_empty_data(self):
        kpi = generate_patient_report([], [], 0, "h1")
        self.assertEqual(kpi.total_documents, 0)
        self.assertEqual(kpi.total_lab_reports, 0)
        self.assertEqual(kpi.latest_ckd_stage, "UNKNOWN")
        self.assertEqual(len(kpi.analyte_stats), 0)

    def test_with_lab_reports(self):
        reports = [
            make_lab_report("d1", "2025-01-01", [
                make_result("Creatinine", 1.2),
                make_result("eGFR", 55, "mL/min"),
            ]),
            make_lab_report("d2", "2025-02-01", [
                make_result("Creatinine", 1.4),
                make_result("eGFR", 50, "mL/min"),
            ]),
        ]
        kpi = generate_patient_report(reports, [], 5, "h1")
        self.assertEqual(kpi.total_documents, 5)
        self.assertEqual(kpi.total_lab_reports, 2)
        self.assertEqual(len(kpi.analyte_stats), 2)

        cr = [s for s in kpi.analyte_stats if s.analyte == "Creatinine"]
        self.assertEqual(len(cr), 1)
        self.assertEqual(cr[0].latest_value, 1.4)  # latest by date
        self.assertEqual(cr[0].min_value, 1.2)
        self.assertEqual(cr[0].max_value, 1.4)
        self.assertEqual(cr[0].count, 2)

    def test_in_range_check(self):
        reports = [
            make_lab_report("d1", "2025-01-01", [
                make_result("Creatinine", 0.8),   # in range (0.6-1.2)
                make_result("eGFR", 45, "mL/min"),  # out of range (<60)
            ]),
        ]
        kpi = generate_patient_report(reports, [], 1, "h1")

        cr = [s for s in kpi.analyte_stats if s.analyte == "Creatinine"][0]
        self.assertTrue(cr.in_range)

        egfr = [s for s in kpi.analyte_stats if s.analyte == "eGFR"][0]
        self.assertFalse(egfr.in_range)

    def test_with_clinical_analyses(self):
        analyses = [
            make_clinical_analysis("d1", "2025-01-01", "G3a", [
                make_finding("critical"),
                make_finding("warning"),
            ]),
            make_clinical_analysis("d2", "2025-03-01", "G3b", [
                make_finding("warning"),
            ]),
        ]
        kpi = generate_patient_report([], analyses, 2, "h1")
        self.assertEqual(kpi.latest_ckd_stage, "G3b")  # latest date
        self.assertEqual(kpi.critical_count, 1)
        self.assertEqual(kpi.warning_count, 2)

    def test_ckd_stage_latest_by_date(self):
        analyses = [
            make_clinical_analysis("d1", "2025-03-01", "G4"),
            make_clinical_analysis("d2", "2025-01-01", "G2"),
        ]
        kpi = generate_patient_report([], analyses, 0, "h1")
        self.assertEqual(kpi.latest_ckd_stage, "G4")  # 2025-03

    def test_invalid_value_skipped(self):
        reports = [
            make_lab_report("d1", "2025-01-01", [
                {"analyte": "Creatinine", "value": "N/A", "unit": "mg/dL"},
                make_result("eGFR", 60, "mL/min"),
            ]),
        ]
        kpi = generate_patient_report(reports, [], 1, "h1")
        self.assertEqual(len(kpi.analyte_stats), 1)  # only eGFR

    def test_disclaimer(self):
        kpi = generate_patient_report([], [], 0, "h1")
        self.assertIn("ปรึกษาแพทย์", kpi.disclaimer)


# ═══════════════════════════════════════════════════════════
# Patient Report Text
# ═══════════════════════════════════════════════════════════

class TestPatientReportText(unittest.TestCase):

    def test_empty_report(self):
        kpi = generate_patient_report([], [], 0, "h1")
        text = patient_report_text_th(kpi)
        self.assertIn("รายงานสรุปผลตรวจ", text)
        self.assertIn("ปรึกษาแพทย์", text)

    def test_with_data(self):
        reports = [
            make_lab_report("d1", "2025-01-01", [
                make_result("Creatinine", 1.5),
            ]),
        ]
        analyses = [
            make_clinical_analysis("d1", "2025-01-01", "G3a", [
                make_finding("critical"),
            ]),
        ]
        kpi = generate_patient_report(reports, analyses, 3, "h1")
        text = patient_report_text_th(kpi)
        self.assertIn("Creatinine", text)
        self.assertIn("G3a", text)
        self.assertIn("Critical", text)
        self.assertIn("3", text)  # total_documents

    def test_ckd_stage_emoji(self):
        analyses = [make_clinical_analysis("d1", "2025-01-01", "G1")]
        kpi = generate_patient_report([], analyses, 0, "h1")
        text = patient_report_text_th(kpi)
        self.assertIn("🟢", text)

    def test_abnormal_indicator(self):
        reports = [
            make_lab_report("d1", "2025-01-01", [
                make_result("eGFR", 30, "mL/min"),  # below 60 → abnormal
            ]),
        ]
        kpi = generate_patient_report(reports, [], 1, "h1")
        text = patient_report_text_th(kpi)
        self.assertIn("⚠️", text)


# ═══════════════════════════════════════════════════════════
# Clinic Summary (Admin)
# ═══════════════════════════════════════════════════════════

class TestGenerateClinicSummary(unittest.TestCase):

    def test_empty(self):
        clinic = generate_clinic_summary([], 0)
        self.assertEqual(clinic.total_patients, 0)
        self.assertEqual(clinic.total_documents, 0)

    def test_aggregate_counts(self):
        p1 = PatientKPI(owner_hash="h1", total_lab_reports=3,
                        total_clinical_analyses=2, latest_ckd_stage="G3a")
        p2 = PatientKPI(owner_hash="h2", total_lab_reports=5,
                        total_clinical_analyses=4, latest_ckd_stage="G4")
        clinic = generate_clinic_summary([p1, p2], 20)
        self.assertEqual(clinic.total_patients, 2)
        self.assertEqual(clinic.total_documents, 20)
        self.assertEqual(clinic.total_lab_reports, 8)
        self.assertEqual(clinic.total_clinical_analyses, 6)

    def test_ckd_distribution(self):
        p1 = PatientKPI(owner_hash="h1", latest_ckd_stage="G3a")
        p2 = PatientKPI(owner_hash="h2", latest_ckd_stage="G3a")
        p3 = PatientKPI(owner_hash="h3", latest_ckd_stage="G5")
        clinic = generate_clinic_summary([p1, p2, p3], 0)
        self.assertEqual(clinic.ckd_stage_distribution["G3a"], 2)
        self.assertEqual(clinic.ckd_stage_distribution["G5"], 1)

    def test_critical_patients(self):
        p1 = PatientKPI(owner_hash="h1", critical_count=3)
        p2 = PatientKPI(owner_hash="h2", critical_count=0)
        p3 = PatientKPI(owner_hash="h3", critical_count=1)
        clinic = generate_clinic_summary([p1, p2, p3], 0)
        self.assertEqual(clinic.critical_alert_patients, 2)

    def test_abnormal_counts(self):
        p1 = PatientKPI(owner_hash="h1", analyte_stats=[
            AnalyteStats("Creatinine", in_range=False),
            AnalyteStats("eGFR", in_range=False),
        ])
        p2 = PatientKPI(owner_hash="h2", analyte_stats=[
            AnalyteStats("Creatinine", in_range=False),
        ])
        clinic = generate_clinic_summary([p1, p2], 0)
        self.assertEqual(clinic.analyte_abnormal_counts["Creatinine"], 2)
        self.assertEqual(clinic.analyte_abnormal_counts["eGFR"], 1)

    def test_no_patient_data(self):
        """ClinicKPI ต้องไม่มีข้อมูลเฉพาะบุคคล."""
        p1 = PatientKPI(owner_hash="h1", latest_ckd_stage="G3a")
        clinic = generate_clinic_summary([p1], 0)
        d = clinic.to_dict()
        self.assertNotIn("owner_hash", d)
        self.assertNotIn("patient_id", d)
        self.assertNotIn("h1", str(d))


# ═══════════════════════════════════════════════════════════
# Clinic Summary Text
# ═══════════════════════════════════════════════════════════

class TestClinicSummaryText(unittest.TestCase):

    def test_empty(self):
        clinic = generate_clinic_summary([], 0)
        text = clinic_summary_text_th(clinic)
        self.assertIn("KPI", text)
        self.assertIn("0 ราย", text)

    def test_with_data(self):
        p1 = PatientKPI(owner_hash="h1", total_lab_reports=3,
                        latest_ckd_stage="G3a", critical_count=1,
                        analyte_stats=[
                            AnalyteStats("Creatinine", in_range=False),
                        ])
        p2 = PatientKPI(owner_hash="h2", total_lab_reports=2,
                        latest_ckd_stage="G4")
        clinic = generate_clinic_summary([p1, p2], 10)
        text = clinic_summary_text_th(clinic)
        self.assertIn("2 ราย", text)  # patients
        self.assertIn("G3a", text)
        self.assertIn("G4", text)
        self.assertIn("Critical Alert", text)
        self.assertIn("Creatinine", text)

    def test_disclaimer(self):
        clinic = generate_clinic_summary([], 0)
        text = clinic_summary_text_th(clinic)
        self.assertIn("ข้อมูลรวมของคลินิก", text)


# ═══════════════════════════════════════════════════════════
# Normal Ranges
# ═══════════════════════════════════════════════════════════

class TestNormalRanges(unittest.TestCase):

    def test_key_analytes_defined(self):
        expected = [
            "Creatinine", "eGFR", "Hemoglobin", "Potassium",
            "Phosphate", "Albumin", "HbA1c",
        ]
        for a in expected:
            self.assertIn(a, NORMAL_RANGES, f"{a} missing from NORMAL_RANGES")

    def test_ranges_valid(self):
        for analyte, (low, high) in NORMAL_RANGES.items():
            self.assertLess(low, high, f"{analyte}: low >= high")


# ═══════════════════════════════════════════════════════════
# Safety
# ═══════════════════════════════════════════════════════════

class TestSafety(unittest.TestCase):

    def test_patient_kpi_no_identity(self):
        kpi = PatientKPI(owner_hash="secret_hash_value")
        d = kpi.to_dict()
        # Only prefix shown
        self.assertEqual(len(d["owner_hash_prefix"]), 8)
        self.assertNotIn("secret_hash_value", str(d))

    def test_clinic_kpi_no_individual_data(self):
        p1 = PatientKPI(owner_hash="hash1", latest_ckd_stage="G3a")
        clinic = generate_clinic_summary([p1], 5)
        d = clinic.to_dict()
        self.assertNotIn("hash1", str(d))

    def test_no_treatment_in_text(self):
        reports = [
            make_lab_report("d1", "2025-01-01", [make_result("Creatinine", 5.0)]),
        ]
        analyses = [
            make_clinical_analysis("d1", "2025-01-01", "G5", [
                make_finding("critical"),
            ]),
        ]
        kpi = generate_patient_report(reports, analyses, 1, "h1")
        text = patient_report_text_th(kpi)
        self.assertNotIn("สั่งยา", text)
        self.assertNotIn("เปลี่ยนยา", text)

    def test_disclaimer_always_present(self):
        kpi = generate_patient_report([], [], 0, "h1")
        text = patient_report_text_th(kpi)
        self.assertIn("ปรึกษาแพทย์", text)


if __name__ == "__main__":
    unittest.main()
