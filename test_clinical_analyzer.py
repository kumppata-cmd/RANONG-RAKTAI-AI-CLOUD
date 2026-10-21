#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test Suite — Clinical Analyzer
Kidney Clinical Intelligence Platform 2.0

ทดสอบ:
  1. CKD Staging (KDIGO 2012)
  2. Anemia Assessment
  3. Mineral-Bone Disease Assessment
  4. Electrolyte Assessment
  5. Dialysis Adequacy Assessment
  6. Nutrition Assessment
  7. Glycemic Assessment
  8. Full Clinical Analysis Pipeline
  9. Safety & Security Tests

ใช้ข้อมูล DEMO/สังเคราะห์เท่านั้น — ไม่ใช้ข้อมูลผู้ป่วยจริง
"""

import json
import unittest
from datetime import datetime

from lab_models import LabResult, LabReport, ReviewStatus, ExtractionMethod
from clinical_analyzer import (
    CKDStage,
    Severity,
    AssessmentArea,
    ClinicalFinding,
    ClinicalAnalysis,
    classify_ckd_stage,
    analyze_lab_report,
    CKD_STAGE_DESCRIPTIONS,
    CKD_STAGE_RECOMMENDATIONS,
    _assess_ckd,
    _assess_anemia,
    _assess_mineral_bone,
    _assess_electrolytes,
    _assess_dialysis,
    _assess_nutrition,
    _assess_glycemic,
    _generate_summary,
)


# ═══════════════════════════════════════════════════════════
# Helper: สร้าง LabResult สำหรับทดสอบ
# ═══════════════════════════════════════════════════════════

def make_result(analyte: str, value: float, unit: str = "",
                ref: str = "", confidence: float = 0.9) -> LabResult:
    """สร้าง LabResult สำหรับทดสอบ."""
    return LabResult(
        analyte=analyte,
        value=value,
        unit=unit,
        reference_range=ref,
        confidence=confidence,
        extraction_method=ExtractionMethod.REGEX_PDF_TEXT.value,
    )


def make_report(results: list[LabResult], doc_id: str = "TEST-DOC-001",
                specimen_date: str = "2024-01-15") -> LabReport:
    """สร้าง LabReport สำหรับทดสอบ."""
    return LabReport(
        source_document_id=doc_id,
        specimen_date=specimen_date,
        extraction_method=ExtractionMethod.REGEX_PDF_TEXT.value,
        results=results,
    )


def results_dict(results: list[LabResult]) -> dict[str, LabResult]:
    """สร้าง results dict เหมือนที่ analyzer ใช้."""
    d = {}
    for r in results:
        if r.analyte not in d:
            d[r.analyte] = r
    return d


# ═══════════════════════════════════════════════════════════
# Test: CKD Staging
# ═══════════════════════════════════════════════════════════

class TestCKDStaging(unittest.TestCase):
    """ทดสอบ CKD staging ตาม KDIGO 2012."""

    def test_stage_g1(self):
        """eGFR ≥90 = G1."""
        self.assertEqual(classify_ckd_stage(95.0), CKDStage.G1)
        self.assertEqual(classify_ckd_stage(90.0), CKDStage.G1)
        self.assertEqual(classify_ckd_stage(120.0), CKDStage.G1)

    def test_stage_g2(self):
        """eGFR 60-89 = G2."""
        self.assertEqual(classify_ckd_stage(89.0), CKDStage.G2)
        self.assertEqual(classify_ckd_stage(60.0), CKDStage.G2)
        self.assertEqual(classify_ckd_stage(75.0), CKDStage.G2)

    def test_stage_g3a(self):
        """eGFR 45-59 = G3a."""
        self.assertEqual(classify_ckd_stage(59.0), CKDStage.G3a)
        self.assertEqual(classify_ckd_stage(45.0), CKDStage.G3a)

    def test_stage_g3b(self):
        """eGFR 30-44 = G3b."""
        self.assertEqual(classify_ckd_stage(44.0), CKDStage.G3b)
        self.assertEqual(classify_ckd_stage(30.0), CKDStage.G3b)

    def test_stage_g4(self):
        """eGFR 15-29 = G4."""
        self.assertEqual(classify_ckd_stage(29.0), CKDStage.G4)
        self.assertEqual(classify_ckd_stage(15.0), CKDStage.G4)

    def test_stage_g5(self):
        """eGFR <15 = G5 (ไตวายระยะสุดท้าย)."""
        self.assertEqual(classify_ckd_stage(14.9), CKDStage.G5)
        self.assertEqual(classify_ckd_stage(5.0), CKDStage.G5)
        self.assertEqual(classify_ckd_stage(0.0), CKDStage.G5)

    def test_boundary_values(self):
        """ทดสอบค่าขอบเขต."""
        self.assertEqual(classify_ckd_stage(90.0), CKDStage.G1)
        self.assertEqual(classify_ckd_stage(89.9), CKDStage.G2)
        self.assertEqual(classify_ckd_stage(60.0), CKDStage.G2)
        self.assertEqual(classify_ckd_stage(59.9), CKDStage.G3a)
        self.assertEqual(classify_ckd_stage(45.0), CKDStage.G3a)
        self.assertEqual(classify_ckd_stage(44.9), CKDStage.G3b)
        self.assertEqual(classify_ckd_stage(30.0), CKDStage.G3b)
        self.assertEqual(classify_ckd_stage(29.9), CKDStage.G4)
        self.assertEqual(classify_ckd_stage(15.0), CKDStage.G4)
        self.assertEqual(classify_ckd_stage(14.9), CKDStage.G5)

    def test_descriptions_complete(self):
        """ทุก CKD stage ต้องมี description."""
        for stage in CKDStage:
            self.assertIn(stage, CKD_STAGE_DESCRIPTIONS)

    def test_recommendations_complete(self):
        """ทุก CKD stage (ยกเว้น UNKNOWN) ต้องมี recommendation."""
        for stage in CKDStage:
            if stage != CKDStage.UNKNOWN:
                self.assertIn(stage, CKD_STAGE_RECOMMENDATIONS)


class TestCKDAssessment(unittest.TestCase):
    """ทดสอบ _assess_ckd."""

    def test_egfr_g5_critical(self):
        """eGFR < 15 → critical severity."""
        r = results_dict([make_result("eGFR", 8.0, "mL/min/1.73m2")])
        findings = _assess_ckd(r)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.CRITICAL.value)
        self.assertIn("G5", findings[0].finding)

    def test_egfr_g3a_warning(self):
        """eGFR 45-59 → warning severity."""
        r = results_dict([make_result("eGFR", 50.0, "mL/min/1.73m2")])
        findings = _assess_ckd(r)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.WARNING.value)
        self.assertIn("G3a", findings[0].finding)

    def test_egfr_normal(self):
        """eGFR ≥90 → normal severity."""
        r = results_dict([make_result("eGFR", 95.0, "mL/min/1.73m2")])
        findings = _assess_ckd(r)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.NORMAL.value)

    def test_creatinine_only_high(self):
        """ไม่มี eGFR แต่ Creatinine สูง → warning."""
        r = results_dict([make_result("Creatinine", 6.0, "mg/dL")])
        findings = _assess_ckd(r)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.WARNING.value)

    def test_creatinine_critical(self):
        """Creatinine >10 → critical."""
        r = results_dict([make_result("Creatinine", 12.0, "mg/dL")])
        findings = _assess_ckd(r)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.CRITICAL.value)

    def test_creatinine_normal_no_finding(self):
        """Creatinine ปกติ ไม่มี eGFR → ไม่มี finding."""
        r = results_dict([make_result("Creatinine", 1.0, "mg/dL")])
        findings = _assess_ckd(r)
        self.assertEqual(len(findings), 0)

    def test_egfr_includes_bun(self):
        """ถ้ามี BUN ด้วย ต้องรวมใน based_on."""
        r = results_dict([
            make_result("eGFR", 25.0, "mL/min/1.73m2"),
            make_result("BUN", 60.0, "mg/dL"),
        ])
        findings = _assess_ckd(r)
        self.assertIn("BUN", findings[0].based_on)
        self.assertIn("BUN", findings[0].values)

    def test_no_renal_data(self):
        """ไม่มี eGFR หรือ Creatinine → ไม่มี finding."""
        r = results_dict([make_result("Sodium", 140.0, "mmol/L")])
        findings = _assess_ckd(r)
        self.assertEqual(len(findings), 0)


# ═══════════════════════════════════════════════════════════
# Test: Anemia Assessment
# ═══════════════════════════════════════════════════════════

class TestAnemiaAssessment(unittest.TestCase):
    """ทดสอบ Anemia Assessment."""

    def test_severe_anemia(self):
        """Hb <7 → critical."""
        r = results_dict([make_result("Hemoglobin", 6.5, "g/dL")])
        findings = _assess_anemia(r)
        self.assertTrue(any(f.severity == Severity.CRITICAL.value for f in findings))

    def test_moderate_anemia(self):
        """Hb 7-10 → warning."""
        r = results_dict([make_result("Hemoglobin", 9.0, "g/dL")])
        findings = _assess_anemia(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))

    def test_mild_anemia(self):
        """Hb 10-12 → mild."""
        r = results_dict([make_result("Hemoglobin", 11.0, "g/dL")])
        findings = _assess_anemia(r)
        self.assertTrue(any(f.severity == Severity.MILD.value for f in findings))

    def test_normal_hb_no_finding(self):
        """Hb 12-13 → ปกติ ไม่มี finding."""
        r = results_dict([make_result("Hemoglobin", 12.5, "g/dL")])
        findings = _assess_anemia(r)
        self.assertEqual(len(findings), 0)

    def test_iron_deficiency(self):
        """Ferritin <100 → warning."""
        r = results_dict([make_result("Ferritin", 50.0, "ng/mL")])
        findings = _assess_anemia(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))

    def test_low_tsat(self):
        """TSAT <20% → warning."""
        r = results_dict([make_result("TSAT", 15.0, "%")])
        findings = _assess_anemia(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))

    def test_high_ferritin(self):
        """Ferritin >800 → warning."""
        r = results_dict([make_result("Ferritin", 1000.0, "ng/mL")])
        findings = _assess_anemia(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))


# ═══════════════════════════════════════════════════════════
# Test: Mineral-Bone Assessment
# ═══════════════════════════════════════════════════════════

class TestMineralBoneAssessment(unittest.TestCase):
    """ทดสอบ CKD-MBD Assessment."""

    def test_severe_hypocalcemia(self):
        """Ca <7 → critical."""
        r = results_dict([make_result("Calcium", 6.5, "mg/dL")])
        findings = _assess_mineral_bone(r)
        self.assertTrue(any(f.severity == Severity.CRITICAL.value for f in findings))

    def test_hypocalcemia(self):
        """Ca <8.5 → warning."""
        r = results_dict([make_result("Calcium", 8.0, "mg/dL")])
        findings = _assess_mineral_bone(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))

    def test_hypercalcemia(self):
        """Ca >10.5 → warning."""
        r = results_dict([make_result("Calcium", 11.0, "mg/dL")])
        findings = _assess_mineral_bone(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))

    def test_high_phosphate(self):
        """PO4 >4.5 → warning."""
        r = results_dict([make_result("Phosphate", 5.5, "mg/dL")])
        findings = _assess_mineral_bone(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))

    def test_very_high_phosphate(self):
        """PO4 >6.5 → critical."""
        r = results_dict([make_result("Phosphate", 7.0, "mg/dL")])
        findings = _assess_mineral_bone(r)
        self.assertTrue(any(f.severity == Severity.CRITICAL.value for f in findings))

    def test_high_pth(self):
        """PTH >600 → warning (secondary hyperparathyroidism)."""
        r = results_dict([make_result("PTH", 700.0, "pg/mL")])
        findings = _assess_mineral_bone(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))

    def test_ca_po4_product(self):
        """Ca×PO₄ >55 → critical."""
        r = results_dict([
            make_result("Calcium", 10.0, "mg/dL"),
            make_result("Phosphate", 6.0, "mg/dL"),
        ])
        findings = _assess_mineral_bone(r)
        ca_po4_findings = [
            f for f in findings if "Ca×PO" in f.finding
        ]
        self.assertTrue(len(ca_po4_findings) > 0)
        self.assertEqual(ca_po4_findings[0].severity, Severity.CRITICAL.value)

    def test_ca_po4_product_normal(self):
        """Ca×PO₄ ≤55 → ไม่มี Ca×PO₄ finding."""
        r = results_dict([
            make_result("Calcium", 9.0, "mg/dL"),
            make_result("Phosphate", 4.0, "mg/dL"),
        ])
        findings = _assess_mineral_bone(r)
        ca_po4_findings = [
            f for f in findings if "Ca×PO" in f.finding
        ]
        self.assertEqual(len(ca_po4_findings), 0)

    def test_normal_calcium(self):
        """Ca 8.5-10.5 → ไม่มี calcium finding."""
        r = results_dict([make_result("Calcium", 9.5, "mg/dL")])
        findings = _assess_mineral_bone(r)
        ca_findings = [f for f in findings if "Calcium" in f.finding]
        self.assertEqual(len(ca_findings), 0)


# ═══════════════════════════════════════════════════════════
# Test: Electrolyte Assessment
# ═══════════════════════════════════════════════════════════

class TestElectrolyteAssessment(unittest.TestCase):
    """ทดสอบ Electrolyte Assessment."""

    def test_critical_hyperkalemia(self):
        """K >6.5 → critical."""
        r = results_dict([make_result("Potassium", 7.0, "mmol/L")])
        findings = _assess_electrolytes(r)
        self.assertTrue(any(f.severity == Severity.CRITICAL.value for f in findings))

    def test_hyperkalemia_warning(self):
        """K 5.5-6.5 → warning."""
        r = results_dict([make_result("Potassium", 6.0, "mmol/L")])
        findings = _assess_electrolytes(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))

    def test_hypokalemia(self):
        """K <3.0 → critical."""
        r = results_dict([make_result("Potassium", 2.5, "mmol/L")])
        findings = _assess_electrolytes(r)
        self.assertTrue(any(f.severity == Severity.CRITICAL.value for f in findings))

    def test_normal_potassium(self):
        """K 3.5-5.0 → ไม่มี finding."""
        r = results_dict([make_result("Potassium", 4.5, "mmol/L")])
        findings = _assess_electrolytes(r)
        k_findings = [f for f in findings if "Potassium" in f.finding]
        self.assertEqual(len(k_findings), 0)

    def test_severe_hyponatremia(self):
        """Na <125 → critical."""
        r = results_dict([make_result("Sodium", 120.0, "mmol/L")])
        findings = _assess_electrolytes(r)
        self.assertTrue(any(f.severity == Severity.CRITICAL.value for f in findings))

    def test_metabolic_acidosis(self):
        """HCO3 <22 → warning."""
        r = results_dict([make_result("Bicarbonate", 18.0, "mmol/L")])
        findings = _assess_electrolytes(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))

    def test_severe_acidosis(self):
        """HCO3 <15 → critical."""
        r = results_dict([make_result("Bicarbonate", 12.0, "mmol/L")])
        findings = _assess_electrolytes(r)
        self.assertTrue(any(f.severity == Severity.CRITICAL.value for f in findings))


# ═══════════════════════════════════════════════════════════
# Test: Dialysis Adequacy
# ═══════════════════════════════════════════════════════════

class TestDialysisAdequacy(unittest.TestCase):
    """ทดสอบ Dialysis Adequacy Assessment."""

    def test_adequate_ktv(self):
        """Kt/V ≥1.2 → normal."""
        r = results_dict([make_result("Kt/V", 1.4, "")])
        findings = _assess_dialysis(r)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.NORMAL.value)

    def test_borderline_ktv(self):
        """Kt/V 1.0-1.2 → warning."""
        r = results_dict([make_result("Kt/V", 1.1, "")])
        findings = _assess_dialysis(r)
        self.assertEqual(findings[0].severity, Severity.WARNING.value)

    def test_inadequate_ktv(self):
        """Kt/V <1.0 → critical."""
        r = results_dict([make_result("Kt/V", 0.8, "")])
        findings = _assess_dialysis(r)
        self.assertEqual(findings[0].severity, Severity.CRITICAL.value)

    def test_no_ktv(self):
        """ไม่มี Kt/V → ไม่มี finding."""
        r = results_dict([make_result("Sodium", 140.0, "mmol/L")])
        findings = _assess_dialysis(r)
        self.assertEqual(len(findings), 0)


# ═══════════════════════════════════════════════════════════
# Test: Nutrition Assessment
# ═══════════════════════════════════════════════════════════

class TestNutritionAssessment(unittest.TestCase):
    """ทดสอบ Nutrition Assessment."""

    def test_severe_malnutrition(self):
        """Albumin <2.5 → critical."""
        r = results_dict([make_result("Albumin", 2.0, "g/dL")])
        findings = _assess_nutrition(r)
        self.assertTrue(any(f.severity == Severity.CRITICAL.value for f in findings))

    def test_low_albumin(self):
        """Albumin 2.5-3.5 → warning."""
        r = results_dict([make_result("Albumin", 3.0, "g/dL")])
        findings = _assess_nutrition(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))

    def test_normal_albumin(self):
        """Albumin ≥3.8 → ไม่มี finding."""
        r = results_dict([make_result("Albumin", 4.0, "g/dL")])
        findings = _assess_nutrition(r)
        self.assertEqual(len(findings), 0)


# ═══════════════════════════════════════════════════════════
# Test: Glycemic Assessment
# ═══════════════════════════════════════════════════════════

class TestGlycemicAssessment(unittest.TestCase):
    """ทดสอบ Glycemic Assessment."""

    def test_severe_hypoglycemia(self):
        """FBS <50 → critical."""
        r = results_dict([make_result("FBS", 40.0, "mg/dL")])
        findings = _assess_glycemic(r)
        self.assertTrue(any(f.severity == Severity.CRITICAL.value for f in findings))

    def test_hyperglycemia(self):
        """FBS >180 → warning."""
        r = results_dict([make_result("FBS", 250.0, "mg/dL")])
        findings = _assess_glycemic(r)
        self.assertTrue(any(f.severity == Severity.WARNING.value for f in findings))

    def test_severe_hyperglycemia(self):
        """FBS >300 → critical."""
        r = results_dict([make_result("FBS", 400.0, "mg/dL")])
        findings = _assess_glycemic(r)
        self.assertTrue(any(f.severity == Severity.CRITICAL.value for f in findings))

    def test_glucose_alias(self):
        """Glucose ใช้แทน FBS ได้."""
        r = results_dict([make_result("Glucose", 250.0, "mg/dL")])
        findings = _assess_glycemic(r)
        self.assertTrue(len(findings) > 0)
        self.assertIn("Glucose", findings[0].based_on)

    def test_normal_fbs(self):
        """FBS 70-100 → ไม่มี finding."""
        r = results_dict([make_result("FBS", 85.0, "mg/dL")])
        findings = _assess_glycemic(r)
        self.assertEqual(len(findings), 0)


# ═══════════════════════════════════════════════════════════
# Test: Full Clinical Analysis Pipeline
# ═══════════════════════════════════════════════════════════

class TestClinicalAnalysisPipeline(unittest.TestCase):
    """ทดสอบ analyze_lab_report — full pipeline."""

    def _ckd5_dialysis_report(self) -> LabReport:
        """สร้าง LabReport จำลอง: CKD Stage 5 ผู้ป่วยฟอกเลือด."""
        return make_report([
            make_result("eGFR", 8.0, "mL/min/1.73m2", "90-120"),
            make_result("Creatinine", 8.5, "mg/dL", "0.6-1.2"),
            make_result("BUN", 80.0, "mg/dL", "7-20"),
            make_result("Hemoglobin", 9.5, "g/dL", "12-17"),
            make_result("Potassium", 5.8, "mmol/L", "3.5-5.0"),
            make_result("Sodium", 138.0, "mmol/L", "136-145"),
            make_result("Calcium", 8.2, "mg/dL", "8.5-10.5"),
            make_result("Phosphate", 6.0, "mg/dL", "2.5-4.5"),
            make_result("PTH", 450.0, "pg/mL", "15-65"),
            make_result("Albumin", 3.2, "g/dL", "3.5-5.0"),
            make_result("Bicarbonate", 18.0, "mmol/L", "22-29"),
            make_result("Kt/V", 1.3, "", "1.2-2.0"),
            make_result("Ferritin", 180.0, "ng/mL", "20-250"),
            make_result("TSAT", 25.0, "%", "20-50"),
            make_result("FBS", 130.0, "mg/dL", "70-100"),
        ])

    def test_ckd5_staging(self):
        """CKD Stage 5 ต้องถูกจัดระดับ."""
        report = self._ckd5_dialysis_report()
        analysis = analyze_lab_report(report)
        self.assertEqual(analysis.ckd_stage, CKDStage.G5.value)

    def test_has_critical_findings(self):
        """CKD5 ต้องมี critical findings."""
        report = self._ckd5_dialysis_report()
        analysis = analyze_lab_report(report)
        self.assertTrue(analysis.has_critical())

    def test_multiple_assessment_areas(self):
        """ต้องมีผลวิเคราะห์จากหลาย area."""
        report = self._ckd5_dialysis_report()
        analysis = analyze_lab_report(report)
        areas = set(f.area for f in analysis.findings)
        self.assertIn(AssessmentArea.CKD_STAGING.value, areas)
        self.assertIn(AssessmentArea.ELECTROLYTE.value, areas)
        self.assertIn(AssessmentArea.MINERAL_BONE.value, areas)

    def test_summary_generated(self):
        """ต้องสร้างข้อความสรุปภาษาไทย."""
        report = self._ckd5_dialysis_report()
        analysis = analyze_lab_report(report)
        self.assertTrue(len(analysis.summary_text_th) > 0)
        self.assertIn("CKD Stage", analysis.summary_text_th)
        self.assertIn("G5", analysis.summary_text_th)

    def test_disclaimer_present(self):
        """ต้องมี disclaimer ทุกครั้ง."""
        report = self._ckd5_dialysis_report()
        analysis = analyze_lab_report(report)
        self.assertIn("ข้อมูลประกอบ", analysis.disclaimer)
        self.assertIn("แพทย์", analysis.disclaimer)

    def test_to_dict(self):
        """ClinicalAnalysis.to_dict ต้องทำงานถูกต้อง."""
        report = self._ckd5_dialysis_report()
        analysis = analyze_lab_report(report)
        d = analysis.to_dict()
        self.assertEqual(d["source_document_id"], "TEST-DOC-001")
        self.assertEqual(d["ckd_stage"], "G5")
        self.assertIsInstance(d["findings"], list)
        self.assertIsInstance(d["finding_count"], int)
        self.assertIsInstance(d["critical_count"], int)
        self.assertIsInstance(d["warning_count"], int)

    def test_json_serializable(self):
        """ผลวิเคราะห์ต้อง serialize เป็น JSON ได้."""
        report = self._ckd5_dialysis_report()
        analysis = analyze_lab_report(report)
        d = analysis.to_dict()
        json_str = json.dumps(d, ensure_ascii=False)
        parsed = json.loads(json_str)
        self.assertEqual(parsed["ckd_stage"], "G5")

    def test_normal_results(self):
        """ผล Lab ปกติ → ไม่มี critical/warning."""
        report = make_report([
            make_result("eGFR", 95.0, "mL/min/1.73m2", "90-120"),
            make_result("Creatinine", 1.0, "mg/dL", "0.6-1.2"),
            make_result("Potassium", 4.2, "mmol/L", "3.5-5.0"),
            make_result("Sodium", 140.0, "mmol/L", "136-145"),
            make_result("Hemoglobin", 14.0, "g/dL", "12-17"),
            make_result("Albumin", 4.2, "g/dL", "3.5-5.0"),
            make_result("FBS", 90.0, "mg/dL", "70-100"),
        ])
        analysis = analyze_lab_report(report)
        self.assertEqual(analysis.ckd_stage, CKDStage.G1.value)
        self.assertFalse(analysis.has_critical())
        # summary should indicate normal
        self.assertIn("ปกติ", analysis.summary_text_th)

    def test_empty_report(self):
        """LabReport เปล่า → ไม่มี findings."""
        report = make_report([])
        analysis = analyze_lab_report(report)
        self.assertEqual(analysis.ckd_stage, CKDStage.UNKNOWN.value)
        self.assertEqual(len(analysis.findings), 0)

    def test_document_id_preserved(self):
        """source_document_id ต้องถูกส่งต่อ."""
        report = make_report(
            [make_result("eGFR", 50.0, "mL/min/1.73m2")],
            doc_id="MY-DOC-123",
        )
        analysis = analyze_lab_report(report)
        self.assertEqual(analysis.source_document_id, "MY-DOC-123")

    def test_specimen_date_preserved(self):
        """specimen_date ต้องถูกส่งต่อ."""
        report = make_report(
            [make_result("eGFR", 50.0, "mL/min/1.73m2")],
            specimen_date="2024-06-15",
        )
        analysis = analyze_lab_report(report)
        self.assertEqual(analysis.specimen_date, "2024-06-15")

    def test_ktv_adequate(self):
        """Kt/V ≥1.2 ต้องรายงานว่าเพียงพอ."""
        report = make_report([make_result("Kt/V", 1.5, "")])
        analysis = analyze_lab_report(report)
        ktv_findings = [
            f for f in analysis.findings
            if f.area == AssessmentArea.DIALYSIS_ADEQUACY.value
        ]
        self.assertEqual(len(ktv_findings), 1)
        self.assertIn("เพียงพอ", ktv_findings[0].finding)


# ═══════════════════════════════════════════════════════════
# Test: User Requirement — CKD5 Dialysis Patient
# ═══════════════════════════════════════════════════════════

class TestUserRequirement(unittest.TestCase):
    """ทดสอบตาม requirement ของเจ้าของโครงการ:
    ผู้ป่วย CKD Stage 5 ฟอกเลือด — ค่า Creatinine 5.2, K 4.5, Hb 10.5
    """

    def test_ckd5_patient(self):
        """ข้อมูลจำลองผู้ป่วย CKD5 ต้องวิเคราะห์ได้ถูกต้อง."""
        report = make_report([
            make_result("Creatinine", 5.2, "mg/dL", "0.6-1.2"),
            make_result("eGFR", 10.0, "mL/min/1.73m2", "90-120"),
            make_result("Potassium", 4.5, "mmol/L", "3.5-5.0"),
            make_result("Hemoglobin", 10.5, "g/dL", "12-17"),
            make_result("Kt/V", 1.3, "", "1.2-2.0"),
        ])
        analysis = analyze_lab_report(report)

        # CKD Stage 5
        self.assertEqual(analysis.ckd_stage, CKDStage.G5.value)

        # ต้องมี critical finding สำหรับ CKD staging
        ckd_findings = [
            f for f in analysis.findings
            if f.area == AssessmentArea.CKD_STAGING.value
        ]
        self.assertTrue(len(ckd_findings) > 0)
        self.assertEqual(ckd_findings[0].severity, Severity.CRITICAL.value)

        # Potassium 4.5 อยู่ในเกณฑ์ปกติ → ไม่มี K finding
        k_findings = [
            f for f in analysis.findings
            if "Potassium" in f.finding
        ]
        self.assertEqual(len(k_findings), 0)

        # Hemoglobin 10.5 → mild anemia (10-12)
        hb_findings = [
            f for f in analysis.findings
            if f.area == AssessmentArea.ANEMIA.value
        ]
        self.assertTrue(len(hb_findings) > 0)
        self.assertEqual(hb_findings[0].severity, Severity.MILD.value)

        # Kt/V 1.3 → adequate
        ktv_findings = [
            f for f in analysis.findings
            if f.area == AssessmentArea.DIALYSIS_ADEQUACY.value
        ]
        self.assertTrue(len(ktv_findings) > 0)
        self.assertEqual(ktv_findings[0].severity, Severity.NORMAL.value)


# ═══════════════════════════════════════════════════════════
# Test: Safety & Security
# ═══════════════════════════════════════════════════════════

class TestSafety(unittest.TestCase):
    """ทดสอบความปลอดภัย."""

    def test_no_external_ai(self):
        """clinical_analyzer ต้องไม่ใช้ AI ภายนอก."""
        import clinical_analyzer
        with open(clinical_analyzer.__file__, "r") as f:
            source = f.read()
        # ไม่มี API calls
        self.assertNotIn("openai", source.lower())
        self.assertNotIn("anthropic", source.lower())
        self.assertNotIn("api_key", source.lower())
        self.assertNotIn("requests.post", source)
        self.assertNotIn("httpx", source)

    def test_no_patient_identity(self):
        """ClinicalAnalysis ต้องไม่มี patient_id."""
        report = make_report([make_result("eGFR", 50.0, "mL/min/1.73m2")])
        analysis = analyze_lab_report(report)
        d = analysis.to_dict()
        json_str = json.dumps(d)
        self.assertNotIn("patient_id", json_str)
        self.assertNotIn("patient_name", json_str)
        self.assertNotIn("hn", json_str.lower().split('"'))

    def test_disclaimer_always_present(self):
        """ต้องมี disclaimer ในทุก analysis."""
        report = make_report([])
        analysis = analyze_lab_report(report)
        self.assertTrue(len(analysis.disclaimer) > 0)
        self.assertIn("ข้อมูลประกอบ", analysis.disclaimer)

    def test_no_treatment_orders(self):
        """recommendation ต้องเป็น "พิจารณา" ไม่ใช่ "สั่ง"."""
        report = make_report([
            make_result("eGFR", 8.0, "mL/min/1.73m2"),
            make_result("Hemoglobin", 6.5, "g/dL"),
            make_result("Potassium", 7.0, "mmol/L"),
        ])
        analysis = analyze_lab_report(report)
        for f in analysis.findings:
            # Recommendation ไม่ควรเป็นคำสั่งตรง
            self.assertNotIn("สั่งยา", f.recommendation)
            self.assertNotIn("เปลี่ยนยา", f.recommendation)

    def test_analyzer_version(self):
        """ต้องมี analyzer version."""
        report = make_report([])
        analysis = analyze_lab_report(report)
        self.assertTrue(len(analysis.analyzer_version) > 0)

    def test_analyzed_at_timestamp(self):
        """ต้องมี analyzed_at timestamp."""
        report = make_report([])
        analysis = analyze_lab_report(report)
        self.assertTrue(len(analysis.analyzed_at) > 0)
        # ต้อง parse ได้
        datetime.fromisoformat(analysis.analyzed_at)


# ═══════════════════════════════════════════════════════════
# Test: Summary Text Generation
# ═══════════════════════════════════════════════════════════

class TestSummaryGeneration(unittest.TestCase):
    """ทดสอบการสร้างข้อความสรุป."""

    def test_critical_section(self):
        """summary ต้องแสดง critical findings ด้วยสี 🔴."""
        report = make_report([
            make_result("eGFR", 8.0, "mL/min/1.73m2"),
        ])
        analysis = analyze_lab_report(report)
        self.assertIn("🔴", analysis.summary_text_th)

    def test_warning_section(self):
        """summary ต้องแสดง warning findings ด้วยสี 🟡."""
        report = make_report([
            make_result("Potassium", 5.8, "mmol/L"),
        ])
        analysis = analyze_lab_report(report)
        self.assertIn("🟡", analysis.summary_text_th)

    def test_normal_summary(self):
        """ผลปกติ ต้องแสดง ✅."""
        report = make_report([
            make_result("eGFR", 95.0, "mL/min/1.73m2"),
            make_result("Potassium", 4.2, "mmol/L"),
        ])
        analysis = analyze_lab_report(report)
        # eGFR G1 is normal severity, K is normal → only normal/mild findings
        # Check that summary includes either green or check mark
        has_normal_indicator = ("✅" in analysis.summary_text_th or
                                "ปกติ" in analysis.summary_text_th)
        self.assertTrue(has_normal_indicator)

    def test_disclaimer_in_summary(self):
        """disclaimer ต้องอยู่ใน summary."""
        report = make_report([make_result("eGFR", 50.0, "mL/min/1.73m2")])
        analysis = analyze_lab_report(report)
        self.assertIn("ข้อมูลประกอบ", analysis.summary_text_th)


if __name__ == "__main__":
    unittest.main()
