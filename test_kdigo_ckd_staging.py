"""KDIGO CKD staging — เกณฑ์ที่คุณหมอกำหนด (CS-6)

ครอบ 3 ส่วนตามตารางที่ได้รับ:
  1. หมวดค่า eGFR G1–G5
  2. ความเรื้อรัง ≥3 เดือน — ห้ามวินิจฉัย CKD จากผลตรวจครั้งเดียว
  3. albuminuria A1/A2/A3 จาก UACR และตารางความเสี่ยง KDIGO

หลักสำคัญจากเกณฑ์:
  "ค่า eGFR ตั้งแต่ 60 ขึ้นไป ไม่ได้แปลว่าเป็นโรคไตเรื้อรังเสมอไป
   ต้องมีหลักฐานความผิดปกติของไตร่วมด้วย เช่น albuminuria หรือ
   ความผิดปกติของโครงสร้างไต นานอย่างน้อย 3 เดือน
   ส่วน eGFR ต่ำกว่า 60 ต้องประเมินความเรื้อรังเช่นกัน
   ไม่ควรวินิจฉัย CKD จากผลตรวจครั้งเดียว"

ข้อมูลทั้งหมดเป็นค่าสังเคราะห์ ไม่มีข้อมูลผู้ป่วยจริง
"""
import unittest
from datetime import datetime, timedelta

from clinical_analyzer import (
    AlbuminuriaCategory,
    CKDStage,
    KDIGORisk,
    assess_ckd,
    classify_albuminuria,
    classify_ckd_stage,
    classify_egfr_category,
    kdigo_risk,
)

NOW = datetime(2026, 10, 26)


def days_ago(n):
    return NOW - timedelta(days=n)


# ═══════════════════════════════════════════════════════════
# 1. หมวดค่า eGFR — ขอบเขตตามตาราง
# ═══════════════════════════════════════════════════════════

class TestEGFRCategory(unittest.TestCase):

    def test_boundaries_match_kdigo_table(self):
        """ขอบเขตต้องตรงตารางที่คุณหมอให้มาทุกช่อง"""
        cases = [
            (120, CKDStage.G1), (90, CKDStage.G1),      # ≥90
            (89, CKDStage.G2), (60, CKDStage.G2),       # 60–89
            (59, CKDStage.G3a), (45, CKDStage.G3a),     # 45–59
            (44, CKDStage.G3b), (30, CKDStage.G3b),     # 30–44
            (29, CKDStage.G4), (15, CKDStage.G4),       # 15–29
            (14.9, CKDStage.G5), (0, CKDStage.G5),      # <15
        ]
        for egfr, expected in cases:
            with self.subTest(egfr=egfr):
                self.assertEqual(classify_egfr_category(egfr), expected)

    def test_legacy_alias_preserved(self):
        """classify_ckd_stage เดิมต้องยังทำงานได้ (backward compat)"""
        for egfr in (95, 75, 50, 35, 20, 10):
            self.assertEqual(
                classify_ckd_stage(egfr), classify_egfr_category(egfr)
            )


# ═══════════════════════════════════════════════════════════
# 2. Albuminuria A1/A2/A3
# ═══════════════════════════════════════════════════════════

class TestAlbuminuria(unittest.TestCase):

    def test_uacr_boundaries(self):
        cases = [
            (0, AlbuminuriaCategory.A1), (29.9, AlbuminuriaCategory.A1),
            (30, AlbuminuriaCategory.A2), (299, AlbuminuriaCategory.A2),
            (300, AlbuminuriaCategory.A3), (2500, AlbuminuriaCategory.A3),
        ]
        for uacr, expected in cases:
            with self.subTest(uacr=uacr):
                self.assertEqual(classify_albuminuria(uacr), expected)

    def test_missing_uacr_is_unknown_not_normal(self):
        """ไม่มีผล UACR ต้องเป็น UNKNOWN ไม่ใช่เดาว่าปกติ"""
        self.assertEqual(
            classify_albuminuria(None), AlbuminuriaCategory.UNKNOWN
        )
        self.assertEqual(
            classify_albuminuria("ไม่ใช่ตัวเลข"), AlbuminuriaCategory.UNKNOWN
        )


# ═══════════════════════════════════════════════════════════
# 3. ตารางความเสี่ยง KDIGO (eGFR × albuminuria)
# ═══════════════════════════════════════════════════════════

class TestKDIGORiskMatrix(unittest.TestCase):

    def test_matrix_matches_reference_table(self):
        """ทุกช่องต้องตรงกับตารางที่คุณหมอให้มา"""
        A1, A2, A3 = (AlbuminuriaCategory.A1, AlbuminuriaCategory.A2,
                      AlbuminuriaCategory.A3)
        L, M, H, V = (KDIGORisk.LOW, KDIGORisk.MODERATE,
                      KDIGORisk.HIGH, KDIGORisk.VERY_HIGH)
        table = {
            CKDStage.G1:  {A1: L, A2: M, A3: H},
            CKDStage.G2:  {A1: L, A2: M, A3: H},
            CKDStage.G3a: {A1: M, A2: H, A3: V},
            CKDStage.G3b: {A1: H, A2: V, A3: V},
            CKDStage.G4:  {A1: V, A2: V, A3: V},
            CKDStage.G5:  {A1: V, A2: V, A3: V},
        }
        for stage, row in table.items():
            for alb, expected in row.items():
                with self.subTest(stage=stage.value, alb=alb.value):
                    self.assertEqual(kdigo_risk(stage, alb), expected)

    def test_unknown_albuminuria_gives_unknown_risk(self):
        """ขาด UACR → ไม่เดาความเสี่ยง"""
        self.assertEqual(
            kdigo_risk(CKDStage.G3a, AlbuminuriaCategory.UNKNOWN),
            KDIGORisk.UNKNOWN,
        )


# ═══════════════════════════════════════════════════════════
# 4. หัวใจของ CS-6 — eGFR ≥60 ไม่ใช่ CKD โดยอัตโนมัติ
# ═══════════════════════════════════════════════════════════

class TestNormalKidneyIsNotCKD(unittest.TestCase):

    def test_normal_egfr_with_normal_uacr_is_not_ckd(self):
        """[CS-6] เดิมผู้ที่ไตปกติถูกติดป้าย CKD G1 — ต้องไม่เกิดอีก"""
        a = assess_ckd(egfr=95, uacr=10, now=NOW)
        self.assertIs(a.is_ckd, False)
        self.assertEqual(a.egfr_category, CKDStage.G1)
        self.assertNotIn("CKD G1", a.stage_label_th)
        self.assertIn("ไม่เข้าเกณฑ์", a.stage_label_th)

    def test_g2_with_normal_uacr_is_not_ckd(self):
        """eGFR 60–89 + A1 ไม่มี damage marker → ไม่ใช่ CKD"""
        a = assess_ckd(egfr=75, uacr=10, now=NOW)
        self.assertIs(a.is_ckd, False)
        self.assertEqual(a.egfr_category, CKDStage.G2)

    def test_normal_egfr_without_uacr_cannot_conclude(self):
        """ไม่มี UACR → สรุปไม่ได้ ต้องบอกว่าขาดอะไร ไม่ใช่เดา"""
        a = assess_ckd(egfr=95, now=NOW)
        self.assertIsNone(a.is_ckd)
        self.assertIn("UACR", a.missing)

    def test_label_never_claims_ckd_when_not_established(self):
        """ป้ายแสดงผลต้องไม่ขึ้นต้นว่า CKD เมื่อยังไม่เข้าเกณฑ์"""
        for kw in (dict(egfr=95, uacr=10), dict(egfr=95),
                   dict(egfr=40), dict(egfr=75, uacr=120)):
            a = assess_ckd(now=NOW, **kw)
            with self.subTest(**kw):
                self.assertFalse(
                    a.stage_label_th.startswith("CKD "),
                    f"ต้องไม่ประกาศ CKD: {a.stage_label_th}",
                )


# ═══════════════════════════════════════════════════════════
# 5. ความเรื้อรัง ≥3 เดือน — ห้ามวินิจฉัยจากผลครั้งเดียว
# ═══════════════════════════════════════════════════════════

class TestChronicityRequirement(unittest.TestCase):

    def test_single_low_egfr_cannot_diagnose_ckd(self):
        """eGFR <60 ครั้งเดียว อาจเป็น AKI — ต้องไม่สรุปว่าเป็น CKD"""
        a = assess_ckd(egfr=40, now=NOW)
        self.assertIsNone(a.is_ckd)
        self.assertIn("ผลติดตาม ≥90 วัน", a.missing)
        self.assertIn("AKI", a.reason_th)

    def test_low_egfr_sustained_over_90_days_is_ckd(self):
        """eGFR <60 ต่อเนื่อง ≥90 วัน → เข้าเกณฑ์ CKD"""
        a = assess_ckd(
            egfr=40, prior_egfr_dates=[(42, days_ago(200))], now=NOW,
        )
        self.assertIs(a.is_ckd, True)
        self.assertEqual(a.egfr_category, CKDStage.G3b)
        self.assertGreaterEqual(a.chronicity_days, 90)

    def test_prior_result_under_90_days_does_not_establish_chronicity(self):
        """ผลเก่าแต่ห่างไม่ถึง 90 วัน ยังพิสูจน์ความเรื้อรังไม่ได้"""
        a = assess_ckd(
            egfr=40, prior_egfr_dates=[(42, days_ago(30))], now=NOW,
        )
        self.assertIsNone(a.is_ckd)
        self.assertFalse(a.chronicity_met)

    def test_prior_normal_egfr_does_not_establish_chronicity(self):
        """ผลเก่าปกติ (eGFR 95) แล้วตอนนี้ต่ำ → น่าจะเฉียบพลัน ไม่ใช่เรื้อรัง"""
        a = assess_ckd(
            egfr=40, prior_egfr_dates=[(95, days_ago(200))], now=NOW,
        )
        self.assertIsNone(a.is_ckd)
        self.assertFalse(a.chronicity_met)

    def test_albuminuria_single_test_cannot_diagnose(self):
        """A2 ครั้งเดียว ยังไม่พอวินิจฉัย"""
        a = assess_ckd(egfr=75, uacr=120, now=NOW)
        self.assertIsNone(a.is_ckd)
        self.assertIn("ผลติดตาม ≥90 วัน", a.missing)

    def test_albuminuria_sustained_is_ckd(self):
        """A2 + ติดตามเกิน 90 วัน → CKD G2A2"""
        a = assess_ckd(
            egfr=75, uacr=120,
            prior_egfr_dates=[(80, days_ago(200))], now=NOW,
        )
        self.assertIs(a.is_ckd, True)
        self.assertEqual(a.stage_label_th, "CKD G2A2")

    def test_documented_damage_marker_duration_establishes_chronicity(self):
        """มีบันทึกว่าพบความผิดปกติมาแล้ว >3 เดือน ก็ใช้ได้"""
        a = assess_ckd(
            egfr=75, uacr=120,
            damage_marker_since=days_ago(150), now=NOW,
        )
        self.assertIs(a.is_ckd, True)

    def test_structural_abnormality_counts_as_damage_marker(self):
        """ความผิดปกติของโครงสร้างไตเป็นหลักฐานได้ แม้ UACR ปกติ"""
        a = assess_ckd(
            egfr=95, uacr=10, has_structural_abnormality=True,
            damage_marker_since=days_ago(200), now=NOW,
        )
        self.assertTrue(a.has_damage_marker)
        self.assertIs(a.is_ckd, True)

    def test_exactly_90_days_meets_threshold(self):
        """ขอบเขตพอดี 90 วัน ต้องนับว่าเข้าเกณฑ์"""
        a = assess_ckd(
            egfr=40, prior_egfr_dates=[(45, days_ago(90))], now=NOW,
        )
        self.assertTrue(a.chronicity_met)
        self.assertIs(a.is_ckd, True)


# ═══════════════════════════════════════════════════════════
# 6. ความปลอดภัยของผลลัพธ์
# ═══════════════════════════════════════════════════════════

class TestAssessmentSafety(unittest.TestCase):

    def test_missing_egfr_is_handled(self):
        a = assess_ckd(egfr=None, now=NOW)
        self.assertIsNone(a.is_ckd)
        self.assertIn("eGFR", a.missing)

    def test_severity_still_driven_by_egfr_value(self):
        """ความเร่งด่วนต้องมาจากค่า eGFR ไม่ใช่สถานะการวินิจฉัย

        eGFR 10 แม้ยังสรุปไม่ได้ว่าเป็น CKD ก็ยังต้องอยู่หมวด G5
        เพื่อให้เส้นทางแจ้งเตือนทำงานตามปกติ
        """
        a = assess_ckd(egfr=10, now=NOW)
        self.assertEqual(a.egfr_category, CKDStage.G5)
        self.assertIsNone(a.is_ckd)  # ยังสรุปไม่ได้ แต่หมวดยังถูกต้อง

    def test_to_dict_is_serializable(self):
        import json
        a = assess_ckd(egfr=40, prior_egfr_dates=[(42, days_ago(200))],
                       now=NOW)
        d = a.to_dict()
        json.dumps(d, ensure_ascii=False)  # ต้องไม่ throw
        self.assertIn("is_ckd", d)
        self.assertIn("stage_label_th", d)
        self.assertIn("reason_th", d)

    def test_reason_always_explains_the_conclusion(self):
        """ทุกผลลัพธ์ต้องมีเหตุผลกำกับ ให้แพทย์ตรวจสอบได้"""
        for kw in (dict(egfr=95, uacr=10), dict(egfr=40),
                   dict(egfr=75, uacr=120), dict(egfr=None)):
            a = assess_ckd(now=NOW, **kw)
            with self.subTest(**kw):
                self.assertTrue(a.reason_th.strip(), "ต้องมีเหตุผลเสมอ")


# ═══════════════════════════════════════════════════════════
# 7. การผสานเข้ากับการวิเคราะห์ผล Lab
# ═══════════════════════════════════════════════════════════

class TestLabAnalysisIntegration(unittest.TestCase):

    def _report(self, **analytes):
        from lab_models import LabReport, LabResult
        results = [
            LabResult(
                analyte=k, value=v, unit="", reference_range="",
                confidence=1.0, extraction_method="test",
            )
            for k, v in analytes.items()
        ]
        return LabReport(
            source_document_id="DOC_KDIGO_T1",
            specimen_date="2026-10-26",
            results=results,
            extraction_method="test",
        )

    def test_analysis_does_not_announce_ckd_from_one_report(self):
        """ผล Lab ชุดเดียวต้องไม่ประกาศว่าเป็น CKD Stage"""
        from clinical_analyzer import analyze_lab_report
        analysis = analyze_lab_report(self._report(eGFR=95.0, UACR=10.0))
        ckd_findings = [
            f for f in analysis.findings if "eGFR" in f.finding or "CKD" in f.finding
        ]
        self.assertTrue(ckd_findings, "ต้องมี finding เรื่องไต")
        joined = "\n".join(f.finding for f in ckd_findings)
        self.assertNotIn("CKD Stage G1", joined)

    def test_analysis_exposes_assessment_detail(self):
        from clinical_analyzer import analyze_lab_report
        analysis = analyze_lab_report(self._report(eGFR=95.0, UACR=10.0))
        self.assertTrue(analysis.ckd_assessment)
        self.assertIs(analysis.ckd_assessment.get("is_ckd"), False)

    def test_ckd_stage_field_still_holds_egfr_category(self):
        """ฟิลด์เดิมยังใช้ได้ (backward compat) แต่เก็บหมวด eGFR"""
        from clinical_analyzer import analyze_lab_report
        analysis = analyze_lab_report(self._report(eGFR=40.0))
        self.assertEqual(analysis.ckd_stage, CKDStage.G3b.value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
