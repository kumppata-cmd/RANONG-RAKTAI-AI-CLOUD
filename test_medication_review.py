#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit Tests — Medication Review & Volume Assessment

ใช้ข้อมูล DEMO/สังเคราะห์เท่านั้น — ไม่มีข้อมูลผู้ป่วยจริง
"""

import unittest
from datetime import datetime

from medication_review import (
    AlertLevel,
    VolumeStatus,
    DrugCategory,
    MedicationAlert,
    VolumeAssessment,
    MedicationReviewResult,
    normalize_drug_name,
    screen_nephrotoxic_drugs,
    check_dose_adjustments,
    check_drug_lab_interactions,
    assess_volume_status,
    review_medications,
    review_text_th,
    NEPHROTOXIC_DRUGS,
    POTASSIUM_RAISING_DRUGS,
    CKD_DOSE_RULES,
)


# ═══════════════════════════════════════════════════════════
# Test Helpers
# ═══════════════════════════════════════════════════════════

class TestNormalizeDrugName(unittest.TestCase):
    def test_lowercase_and_strip(self):
        self.assertEqual(normalize_drug_name("  Ibuprofen  "), "ibuprofen")

    def test_empty(self):
        self.assertEqual(normalize_drug_name(""), "")

    def test_thai_name(self):
        self.assertEqual(normalize_drug_name("เมทฟอร์มิน"), "เมทฟอร์มิน")


# ═══════════════════════════════════════════════════════════
# Nephrotoxic Drug Screening
# ═══════════════════════════════════════════════════════════

class TestScreenNephrotoxicDrugs(unittest.TestCase):
    def test_nsaid_detected(self):
        alerts = screen_nephrotoxic_drugs(["Ibuprofen 400mg"])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].alert_level, AlertLevel.WARNING.value)
        self.assertIn("NSAIDs", alerts[0].message_th)

    def test_aminoglycoside_critical(self):
        alerts = screen_nephrotoxic_drugs(["Gentamicin 80mg IV"])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].alert_level, AlertLevel.CRITICAL.value)

    def test_contrast_critical(self):
        alerts = screen_nephrotoxic_drugs(["Iodinated Contrast"])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].alert_level, AlertLevel.CRITICAL.value)

    def test_acei_info_level(self):
        alerts = screen_nephrotoxic_drugs(["Enalapril 10mg"])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].alert_level, AlertLevel.INFO.value)

    def test_acei_upgrade_in_advanced_ckd(self):
        alerts = screen_nephrotoxic_drugs(["Losartan 50mg"], ckd_stage="G4")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].alert_level, AlertLevel.WARNING.value)
        self.assertIn("CKD ระยะรุนแรง", alerts[0].message_th)

    def test_metformin_upgrade_g5(self):
        alerts = screen_nephrotoxic_drugs(["Metformin 500mg"], ckd_stage="G5")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].alert_level, AlertLevel.CRITICAL.value)
        self.assertIn("ห้ามใช้", alerts[0].message_th)

    def test_multiple_drugs(self):
        alerts = screen_nephrotoxic_drugs([
            "Ibuprofen 400mg",
            "Gentamicin 80mg",
            "Enalapril 10mg",
        ])
        self.assertEqual(len(alerts), 3)
        # Critical should be first after sorting in review
        categories = {a.drug_name for a in alerts}
        self.assertIn("Ibuprofen 400mg", categories)
        self.assertIn("Gentamicin 80mg", categories)

    def test_no_match(self):
        alerts = screen_nephrotoxic_drugs(["Paracetamol 500mg"])
        self.assertEqual(len(alerts), 0)

    def test_empty_list(self):
        alerts = screen_nephrotoxic_drugs([])
        self.assertEqual(len(alerts), 0)

    def test_thai_name_match(self):
        alerts = screen_nephrotoxic_drugs(["ไอบูโพรเฟน"])
        self.assertEqual(len(alerts), 1)
        self.assertIn("NSAIDs", alerts[0].message_th)

    def test_no_duplicate_category(self):
        """Two NSAIDs should produce only one alert."""
        alerts = screen_nephrotoxic_drugs(["Ibuprofen", "Diclofenac"])
        self.assertEqual(len(alerts), 1)

    def test_lithium_detected(self):
        alerts = screen_nephrotoxic_drugs(["Lithium 300mg"])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].alert_level, AlertLevel.WARNING.value)


# ═══════════════════════════════════════════════════════════
# CKD Dose Adjustment
# ═══════════════════════════════════════════════════════════

class TestCheckDoseAdjustments(unittest.TestCase):
    def test_metformin_egfr_below_30(self):
        alerts = check_dose_adjustments(["Metformin 500mg"], egfr=25.0)
        # Should trigger both <30 (critical) and <45 (warning) rules
        critical = [a for a in alerts
                    if a.alert_level == AlertLevel.CRITICAL.value]
        self.assertGreaterEqual(len(critical), 1)

    def test_metformin_egfr_35(self):
        alerts = check_dose_adjustments(["Metformin 500mg"], egfr=35.0)
        self.assertTrue(any(a.alert_level == AlertLevel.WARNING.value
                            for a in alerts))

    def test_metformin_egfr_normal(self):
        alerts = check_dose_adjustments(["Metformin 500mg"], egfr=60.0)
        self.assertEqual(len(alerts), 0)

    def test_no_egfr(self):
        alerts = check_dose_adjustments(["Metformin 500mg"], egfr=None)
        self.assertEqual(len(alerts), 0)

    def test_allopurinol_low_egfr(self):
        alerts = check_dose_adjustments(["Allopurinol 300mg"], egfr=20.0)
        self.assertEqual(len(alerts), 1)
        self.assertIn("100 mg/d", alerts[0].recommendation_th)

    def test_gabapentin_low_egfr(self):
        alerts = check_dose_adjustments(["Gabapentin 300mg"], egfr=25.0)
        self.assertEqual(len(alerts), 1)

    def test_anticoagulant_low_egfr(self):
        alerts = check_dose_adjustments(["Rivaroxaban 20mg"], egfr=20.0)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].alert_level, AlertLevel.CRITICAL.value)

    def test_digoxin_low_egfr(self):
        alerts = check_dose_adjustments(["Digoxin 0.125mg"], egfr=25.0)
        self.assertEqual(len(alerts), 1)

    def test_safe_drug_no_alert(self):
        alerts = check_dose_adjustments(["Amlodipine 5mg"], egfr=25.0)
        self.assertEqual(len(alerts), 0)


# ═══════════════════════════════════════════════════════════
# Drug-Lab Interactions
# ═══════════════════════════════════════════════════════════

class TestCheckDrugLabInteractions(unittest.TestCase):
    def test_spironolactone_high_k(self):
        alerts = check_drug_lab_interactions(
            ["Spironolactone 25mg"],
            {"Potassium": 5.3},
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].alert_level, AlertLevel.WARNING.value)

    def test_spironolactone_very_high_k(self):
        alerts = check_drug_lab_interactions(
            ["Spironolactone 25mg"],
            {"Potassium": 5.8},
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].alert_level, AlertLevel.CRITICAL.value)

    def test_acei_with_hyperkalemia(self):
        alerts = check_drug_lab_interactions(
            ["Enalapril 10mg"],
            {"Potassium": 5.8},
        )
        self.assertTrue(any("ACEI/ARB" in a.message_th for a in alerts))

    def test_normal_k_no_alert(self):
        alerts = check_drug_lab_interactions(
            ["Spironolactone 25mg"],
            {"Potassium": 4.5},
        )
        self.assertEqual(len(alerts), 0)

    def test_no_potassium_value(self):
        alerts = check_drug_lab_interactions(
            ["Spironolactone 25mg"],
            {"Sodium": 140},
        )
        self.assertEqual(len(alerts), 0)

    def test_kcl_high_k(self):
        alerts = check_drug_lab_interactions(
            ["KCl supplement"],
            {"Potassium": 5.2},
        )
        self.assertEqual(len(alerts), 1)

    def test_trimethoprim_high_k(self):
        alerts = check_drug_lab_interactions(
            ["Trimethoprim 160mg"],
            {"Potassium": 5.1},
        )
        self.assertEqual(len(alerts), 1)


# ═══════════════════════════════════════════════════════════
# Volume Assessment
# ═══════════════════════════════════════════════════════════

class TestAssessVolumeStatus(unittest.TestCase):
    def test_fluid_overload_by_weight(self):
        va = assess_volume_status(
            lab_values={},
            pre_weight_kg=65.0,
            dry_weight_kg=60.0,
        )
        self.assertEqual(va.status, VolumeStatus.OVERLOADED.value)
        self.assertAlmostEqual(va.interdialytic_weight_gain_kg, 5.0)

    def test_moderate_overload(self):
        va = assess_volume_status(
            lab_values={},
            pre_weight_kg=62.0,
            dry_weight_kg=60.0,
        )
        # 2kg / 60kg = 3.3% → moderate
        self.assertEqual(va.status, VolumeStatus.OVERLOADED.value)

    def test_euvolemic(self):
        va = assess_volume_status(
            lab_values={"Sodium": 140},
            pre_weight_kg=60.5,
            dry_weight_kg=60.0,
        )
        # 0.5/60 = 0.8% → normal
        self.assertEqual(va.status, VolumeStatus.EUVOLEMIC.value)

    def test_dehydration_by_weight(self):
        va = assess_volume_status(
            lab_values={},
            pre_weight_kg=59.0,
            dry_weight_kg=60.0,
        )
        self.assertEqual(va.status, VolumeStatus.DEHYDRATED.value)

    def test_dehydration_by_sodium(self):
        va = assess_volume_status(
            lab_values={"Sodium": 150},
        )
        self.assertEqual(va.status, VolumeStatus.DEHYDRATED.value)

    def test_dilutional_hyponatremia(self):
        va = assess_volume_status(
            lab_values={"Sodium": 128},
        )
        self.assertEqual(va.status, VolumeStatus.OVERLOADED.value)

    def test_high_bun_cr_ratio(self):
        va = assess_volume_status(
            lab_values={"BUN": 50, "Creatinine": 2.0},
        )
        # BUN/Cr = 25 > 20 → prerenal/dehydration
        self.assertEqual(va.status, VolumeStatus.DEHYDRATED.value)

    def test_low_albumin_indicator(self):
        va = assess_volume_status(
            lab_values={"Albumin": 2.5, "Sodium": 140},
        )
        self.assertTrue(any("Albumin" in i for i in va.indicators))

    def test_uf_rate_calculation(self):
        va = assess_volume_status(
            lab_values={},
            pre_weight_kg=65.0,
            post_weight_kg=61.0,
            dry_weight_kg=60.0,
            dialysis_hours=4.0,
        )
        # UF = (65-61)*1000 / 60 / 4 = 16.7
        self.assertIsNotNone(va.ultrafiltration_rate)
        self.assertGreater(va.ultrafiltration_rate, 13)
        self.assertTrue(any("UF rate" in i for i in va.indicators))

    def test_unknown_with_no_data(self):
        va = assess_volume_status(lab_values={})
        self.assertEqual(va.status, VolumeStatus.UNKNOWN.value)

    def test_euvolemic_with_normal_labs(self):
        va = assess_volume_status(
            lab_values={"Sodium": 140, "Albumin": 4.0},
        )
        self.assertEqual(va.status, VolumeStatus.EUVOLEMIC.value)


# ═══════════════════════════════════════════════════════════
# Comprehensive Review
# ═══════════════════════════════════════════════════════════

class TestReviewMedications(unittest.TestCase):
    def test_comprehensive_review(self):
        result = review_medications(
            medications=["Ibuprofen 400mg", "Spironolactone 25mg"],
            lab_values={"Potassium": 5.5, "Sodium": 140},
            ckd_stage="G3",
            egfr=40.0,
        )
        self.assertIsInstance(result, MedicationReviewResult)
        self.assertGreater(len(result.alerts), 0)
        self.assertIsNotNone(result.volume_assessment)

    def test_deduplication(self):
        """Same drug should not appear twice for same category."""
        result = review_medications(
            medications=["Metformin 500mg"],
            lab_values={},
            ckd_stage="G5",
            egfr=25.0,
        )
        metformin_alerts = [a for a in result.alerts
                           if "metformin" in a.drug_name.lower()]
        # Should have nephrotoxic + dose adjust but deduplicated per category
        categories = {a.category for a in metformin_alerts}
        self.assertEqual(len(metformin_alerts), len(categories))

    def test_sorting_critical_first(self):
        result = review_medications(
            medications=[
                "Enalapril 10mg",     # INFO
                "Gentamicin 80mg",    # CRITICAL
                "Ibuprofen 400mg",    # WARNING
            ],
            lab_values={},
        )
        if len(result.alerts) >= 2:
            self.assertEqual(result.alerts[0].alert_level,
                             AlertLevel.CRITICAL.value)

    def test_empty_medications(self):
        result = review_medications(
            medications=[],
            lab_values={"Sodium": 140},
        )
        self.assertEqual(len(result.alerts), 0)
        self.assertIsNotNone(result.volume_assessment)

    def test_review_date_set(self):
        result = review_medications(
            medications=[],
            lab_values={},
        )
        self.assertEqual(result.review_date,
                         datetime.now().strftime("%Y-%m-%d"))

    def test_disclaimer_present(self):
        result = review_medications(
            medications=[],
            lab_values={},
        )
        self.assertIn("ปรึกษาแพทย์", result.disclaimer)

    def test_with_volume_data(self):
        result = review_medications(
            medications=["Enalapril 10mg"],
            lab_values={"Sodium": 128},
            pre_weight_kg=65.0,
            dry_weight_kg=60.0,
        )
        self.assertIsNotNone(result.volume_assessment)
        self.assertEqual(result.volume_assessment.status,
                         VolumeStatus.OVERLOADED.value)


# ═══════════════════════════════════════════════════════════
# Thai Text Output
# ═══════════════════════════════════════════════════════════

class TestReviewTextTh(unittest.TestCase):
    def test_no_alerts_message(self):
        result = MedicationReviewResult()
        text = review_text_th(result)
        self.assertIn("ไม่พบข้อควรระวัง", text)

    def test_critical_alert_format(self):
        result = MedicationReviewResult(alerts=[
            MedicationAlert(
                drug_name="Gentamicin",
                alert_level=AlertLevel.CRITICAL.value,
                category=DrugCategory.NEPHROTOXIC.value,
                message_th="ยามีพิษต่อไต",
                recommendation_th="ปรึกษาแพทย์",
            )
        ])
        text = review_text_th(result)
        self.assertIn("🔴", text)
        self.assertIn("Gentamicin", text)
        self.assertIn("ปรึกษาแพทย์", text)

    def test_warning_alert_format(self):
        result = MedicationReviewResult(alerts=[
            MedicationAlert(
                drug_name="Ibuprofen",
                alert_level=AlertLevel.WARNING.value,
                category=DrugCategory.NEPHROTOXIC.value,
                message_th="NSAIDs อาจเป็นพิษ",
                recommendation_th="ทบทวนความจำเป็น",
            )
        ])
        text = review_text_th(result)
        self.assertIn("🟡", text)

    def test_info_alert_format(self):
        result = MedicationReviewResult(alerts=[
            MedicationAlert(
                drug_name="Enalapril",
                alert_level=AlertLevel.INFO.value,
                category=DrugCategory.NEPHROTOXIC.value,
                message_th="ACEI ต้องติดตาม",
                recommendation_th="ติดตาม Cr",
            )
        ])
        text = review_text_th(result)
        self.assertIn("ℹ️", text)

    def test_volume_overloaded_format(self):
        result = MedicationReviewResult(
            volume_assessment=VolumeAssessment(
                status=VolumeStatus.OVERLOADED.value,
                indicators=["น้ำหนักเพิ่มระหว่างฟอก > 5%"],
                interdialytic_weight_gain_kg=5.0,
            )
        )
        text = review_text_th(result)
        self.assertIn("น้ำเกิน", text)
        self.assertIn("5.0 kg", text)

    def test_volume_euvolemic_format(self):
        result = MedicationReviewResult(
            volume_assessment=VolumeAssessment(
                status=VolumeStatus.EUVOLEMIC.value,
            )
        )
        text = review_text_th(result)
        self.assertIn("น้ำปกติ", text)

    def test_uf_rate_in_text(self):
        result = MedicationReviewResult(
            volume_assessment=VolumeAssessment(
                status=VolumeStatus.OVERLOADED.value,
                indicators=["UF rate สูง"],
                ultrafiltration_rate=16.7,
            )
        )
        text = review_text_th(result)
        self.assertIn("16.7 mL/kg/hr", text)

    def test_disclaimer_always_present(self):
        result = review_medications(
            medications=["Gentamicin"],
            lab_values={"Potassium": 5.8},
        )
        text = review_text_th(result)
        self.assertIn("⚠️", text)
        self.assertIn("ปรึกษาแพทย์หรือเภสัชกร", text)

    def test_unknown_volume_with_indicators(self):
        result = MedicationReviewResult(
            volume_assessment=VolumeAssessment(
                status=VolumeStatus.UNKNOWN.value,
                indicators=["Albumin ต่ำ (2.5)"],
            )
        )
        text = review_text_th(result)
        self.assertIn("Albumin", text)


# ═══════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════

class TestMedicationAlert(unittest.TestCase):
    def test_to_dict(self):
        alert = MedicationAlert(
            drug_name="Ibuprofen",
            alert_level=AlertLevel.WARNING.value,
            category=DrugCategory.NEPHROTOXIC.value,
            message_th="ยามีพิษ",
            recommendation_th="ปรึกษาแพทย์",
        )
        d = alert.to_dict()
        self.assertEqual(d["drug_name"], "Ibuprofen")
        self.assertEqual(d["alert_level"], "warning")


class TestVolumeAssessment(unittest.TestCase):
    def test_to_dict_minimal(self):
        va = VolumeAssessment(status=VolumeStatus.UNKNOWN.value)
        d = va.to_dict()
        self.assertEqual(d["status"], "unknown")
        self.assertNotIn("pre_weight_kg", d)

    def test_to_dict_full(self):
        va = VolumeAssessment(
            status=VolumeStatus.OVERLOADED.value,
            indicators=["test"],
            pre_weight_kg=65.0,
            post_weight_kg=61.0,
            dry_weight_kg=60.0,
            interdialytic_weight_gain_kg=5.0,
            ultrafiltration_rate=16.7,
        )
        d = va.to_dict()
        self.assertEqual(d["pre_weight_kg"], 65.0)
        self.assertEqual(d["ultrafiltration_rate"], 16.7)


class TestMedicationReviewResult(unittest.TestCase):
    def test_critical_and_warning_counts(self):
        result = MedicationReviewResult(alerts=[
            MedicationAlert("A", AlertLevel.CRITICAL.value, "x", "", ""),
            MedicationAlert("B", AlertLevel.WARNING.value, "x", "", ""),
            MedicationAlert("C", AlertLevel.CRITICAL.value, "x", "", ""),
        ])
        self.assertEqual(len(result.critical_alerts()), 2)
        self.assertEqual(len(result.warning_alerts()), 1)

    def test_to_dict(self):
        result = MedicationReviewResult()
        d = result.to_dict()
        self.assertIn("review_date", d)
        self.assertIn("alerts", d)
        self.assertIn("disclaimer", d)
        self.assertEqual(d["critical_count"], 0)


# ═══════════════════════════════════════════════════════════
# Safety Tests
# ═══════════════════════════════════════════════════════════

class TestSafety(unittest.TestCase):
    def test_disclaimer_never_empty(self):
        result = MedicationReviewResult()
        self.assertTrue(len(result.disclaimer) > 0)

    def test_all_recommendations_mention_consult(self):
        """Every nephrotoxic drug recommendation should say ปรึกษาแพทย์."""
        for cat_data in NEPHROTOXIC_DRUGS.values():
            self.assertIn("ปรึกษาแพทย์", cat_data["recommendation_th"],
                          f"Missing consult advice in {cat_data}")

    def test_all_dose_rules_mention_consult(self):
        """Every dose adjustment rule should say ปรึกษาแพทย์."""
        for rule in CKD_DOSE_RULES:
            self.assertIn("ปรึกษาแพทย์", rule["recommendation_th"],
                          f"Missing consult advice in rule for "
                          f"{rule['drug_pattern']}")

    def test_no_prescribing_language(self):
        """Output text should never prescribe — only recommend consulting."""
        result = review_medications(
            medications=["Gentamicin", "Metformin", "Ibuprofen"],
            lab_values={"Potassium": 5.8},
            ckd_stage="G5",
            egfr=20.0,
        )
        text = review_text_th(result)
        # Should not contain prescribing verbs
        prescribing = ["สั่งยา", "ให้ยา", "จ่ายยา"]
        for verb in prescribing:
            self.assertNotIn(verb, text,
                             f"Output contains prescribing verb: {verb}")


# ═══════════════════════════════════════════════════════════
# Reference Data Integrity
# ═══════════════════════════════════════════════════════════

class TestReferenceData(unittest.TestCase):
    def test_nephrotoxic_drugs_have_names(self):
        for key, data in NEPHROTOXIC_DRUGS.items():
            self.assertTrue(len(data["names"]) > 0,
                            f"{key} has no drug names")

    def test_nephrotoxic_drugs_have_valid_levels(self):
        valid = {v.value for v in AlertLevel}
        for key, data in NEPHROTOXIC_DRUGS.items():
            self.assertIn(data["level"], valid,
                          f"{key} has invalid level: {data['level']}")

    def test_dose_rules_have_thresholds(self):
        for rule in CKD_DOSE_RULES:
            self.assertIsInstance(rule["egfr_threshold"], (int, float))
            self.assertGreater(rule["egfr_threshold"], 0)

    def test_potassium_drugs_not_empty(self):
        self.assertGreater(len(POTASSIUM_RAISING_DRUGS), 0)


if __name__ == "__main__":
    unittest.main()
