"""Precision guards for OCR lab values (lab_sync.precision_flags).

กฎความแม่นยำ 3 ชั้นก่อนเชื่อค่าแล็บจากรูปถ่าย:
  1. ช่วงที่เป็นไปได้ทางสรีรวิทยา (PLAUSIBLE_RANGE)
  2. ความสอดคล้องภายใน Hb/Hct (Hct ≈ 3×Hb)
  3. การเปลี่ยนแปลงจากแล็บครั้งก่อน (DELTA_LIMIT)
ทุกกฎ = แจ้งเตือนให้คนตรวจรูปซ้ำ ไม่ใช่การตัดสินแทนแพทย์
"""
import pytest

from hd_module.lab_sync import precision_flags, PLAUSIBLE_RANGE, DELTA_LIMIT


class TestPlausibleRange:
    def test_normal_row_no_flags(self):
        row = {"hb": 10.5, "hct": 32, "k": 4.8, "na": 138, "ca": 9.1,
               "phos": 4.5, "albumin": 3.8, "glucose": 110,
               "creatinine": 8.2, "bun": 55}
        assert precision_flags(row) == []

    def test_k_too_high_flagged(self):
        flags = precision_flags({"k": 12.0})
        assert len(flags) == 1
        assert "K" in flags[0] and "OCR" in flags[0]

    def test_k_too_low_flagged(self):
        assert len(precision_flags({"k": 0.8})) == 1

    def test_hb_impossible_flagged(self):
        assert len(precision_flags({"hb": 25.0})) == 1
        assert len(precision_flags({"hb": 1.2})) == 1

    def test_boundary_values_not_flagged(self):
        # ค่าขอบพอดี = ยังเป็นไปได้ ไม่ติดธง
        for col, (lo, hi) in PLAUSIBLE_RANGE.items():
            assert precision_flags({col: lo}) == [], col
            assert precision_flags({col: hi}) == [], col

    def test_none_values_ignored(self):
        assert precision_flags({"hb": None, "k": None}) == []

    def test_multiple_bad_values_all_flagged(self):
        flags = precision_flags({"k": 15, "na": 90, "hb": 30})
        assert len(flags) == 3


class TestHbHctConsistency:
    def test_consistent_pair_ok(self):
        assert precision_flags({"hb": 10.0, "hct": 30.0}) == []
        assert precision_flags({"hb": 10.0, "hct": 33.0}) == []  # ต่าง 3 ยังรับได้

    def test_inconsistent_pair_flagged(self):
        # Hb 10 → Hct ควร ~30 แต่ได้ 45 (เช่น OCR อ่าน Hct ของคนอื่น/แถวอื่น)
        flags = precision_flags({"hb": 10.0, "hct": 45.0})
        assert any("ไม่สอดคล้อง" in f for f in flags)

    def test_only_one_of_pair_no_check(self):
        assert precision_flags({"hb": 10.0, "hct": None}) == []
        assert precision_flags({"hb": None, "hct": 30.0}) == []


class TestDeltaVsPrevious:
    def test_small_change_ok(self):
        prev = {"k": 4.5, "hb": 10.0}
        assert precision_flags({"k": 5.0, "hb": 10.8}, prev) == []

    def test_k_jump_flagged(self):
        # K 3.8 → 6.5 = ต่าง 2.7 ≥ 1.5 → ตรวจรูปซ้ำก่อนเชื่อ (เคสที่หมอยกตัวอย่าง)
        flags = precision_flags({"k": 6.5}, {"k": 3.8})
        assert len(flags) == 1 and "K" in flags[0] and "ตรวจรูปซ้ำ" in flags[0]

    def test_hb_jump_flagged(self):
        flags = precision_flags({"hb": 13.0}, {"hb": 8.0})
        assert any("Hb" in f for f in flags)

    def test_delta_exactly_at_limit_flagged(self):
        for col, lim in DELTA_LIMIT.items():
            base = {"k": 4.0, "hb": 10.0, "hct": 30.0, "ca": 9.0,
                    "phos": 4.0, "na": 138.0, "albumin": 3.5}[col]
            flags = precision_flags({col: base + lim}, {col: base})
            assert any(f for f in flags), f"{col} delta {lim} should flag"

    def test_no_previous_no_delta_check(self):
        assert precision_flags({"k": 5.0}, None) == []
        assert precision_flags({"k": 5.0}, {}) == []

    def test_missing_analyte_in_prev_skipped(self):
        assert precision_flags({"k": 5.0}, {"hb": 10.0}) == []

    def test_decimal_strings_from_db_handled(self):
        # ค่าจากฐานข้อมูลอาจมาเป็น Decimal — ฟังก์ชันต้อง float ให้เอง
        from decimal import Decimal
        flags = precision_flags({"k": Decimal("6.5")}, {"k": Decimal("3.8")})
        assert len(flags) == 1


class TestCombined:
    def test_ocr_misread_scenario(self):
        """สถานการณ์จริง: OCR อ่าน K 4.1 เป็น 41 — ต้องติดทั้งธงช่วง"""
        flags = precision_flags({"k": 41.0}, {"k": 4.0})
        assert len(flags) >= 1
        assert any("เกินช่วง" in f for f in flags)

    def test_all_flags_are_thai_readable(self):
        flags = precision_flags({"k": 12, "hb": 10, "hct": 45}, {"k": 4})
        for f in flags:
            assert isinstance(f, str) and len(f) > 10


class TestNewAnalytes2026_11_22:
    """PLT / HCO3 / WBC / Anti-HIV — แพทย์สั่งเพิ่ม 22 พ.ย. 2569"""

    def test_new_numeric_cols_registered(self):
        from hd_module.lab_sync import NUMERIC_COLS, TEXT_COLS, ANALYTE_COL
        for c in ("plt", "hco3", "wbc"):
            assert c in NUMERIC_COLS
        assert "antihiv" in TEXT_COLS
        assert ANALYTE_COL["Platelet"] == "plt"
        assert ANALYTE_COL["Bicarbonate"] == "hco3"
        assert ANALYTE_COL["Anti-HIV"] == "antihiv"

    def test_plt_hco3_plausibility(self):
        assert precision_flags({"plt": 250, "hco3": 22, "wbc": 7.5}) == []
        assert len(precision_flags({"plt": 2500})) == 1   # OCR อ่าน /uL ดิบ
        assert len(precision_flags({"hco3": 220})) == 1   # จุดทศนิยมหาย

    def test_report_to_row_serology(self):
        from hd_module.lab_sync import report_to_row
        rep = {"results": [
            {"analyte": "Anti-HIV", "value": 0.0, "raw_text": "Anti HIV : Non-reactive"},
            {"analyte": "HBsAg", "value": 1.0, "raw_text": "HBsAg : Reactive"},
            {"analyte": "Platelet", "value": 245.0},
            {"analyte": "Bicarbonate", "value": 21.5},
        ]}
        row = report_to_row(rep)
        assert row["antihiv"] == "negative"
        assert row["hbsag"] == "positive"
        assert row["plt"] == 245.0
        assert row["hco3"] == 21.5


class TestSerologyExtractor:
    """Strategy 5 ใน lab_extractor (root) — อ่านผลเชิงคุณภาพจากใบจริง"""

    def _extract(self, text):
        import lab_extractor
        rep = lab_extractor.extract_lab_results_from_text(text, "testdoc1")
        return {r.analyte: r.value for r in rep.results}, rep

    def test_full_serology_block(self):
        got, _ = self._extract(
            "HBsAg : Non-reactive\nAnti-HBs : Reactive\n"
            "Anti HCV : Non-reactive\nAnti-HIV : Non-reactive\n")
        assert got.get("HBsAg") == 0.0
        assert got.get("Anti-HBs") == 1.0
        assert got.get("Anti-HCV") == 0.0
        assert got.get("Anti-HIV") == 0.0

    def test_positive_negative_words(self):
        got, _ = self._extract("HBsAg: Positive\nAnti HIV Ab: Negative")
        assert got.get("HBsAg") == 1.0
        assert got.get("Anti-HIV") == 0.0

    def test_thai_results(self):
        got, _ = self._extract("HBsAg ผลลบ\nAnti-HCV ไม่พบ")
        assert got.get("HBsAg") == 0.0
        assert got.get("Anti-HCV") == 0.0

    def test_result_on_next_line(self):
        got, _ = self._extract("Anti-HCV\nNon-reactive")
        assert got.get("Anti-HCV") == 0.0

    def test_no_result_word_not_captured(self):
        # มีแต่ชื่อการตรวจ ไม่มีคำบอกผล → ไม่เดา ไม่เก็บ
        got, _ = self._extract("ส่งตรวจ Anti-HIV และ HBsAg พรุ่งนี้เช้า")
        # "พบ" ไม่ได้ตามหลังชื่อใน 30 ตัวอักษร → ต้องไม่มีผล หรือถ้าจับได้ต้องไม่ใช่ positive มั่ว
        assert "HBsAg" not in got or got.get("HBsAg") in (0.0,)

    def test_numeric_labs_unaffected(self):
        got, rep = self._extract(
            "Creatinine 5.2 mg/dL\nPotassium 4.8 mEq/L\nHBsAg : Non-reactive")
        assert got.get("Creatinine") == 5.2
        assert got.get("Potassium") == 4.8
        assert got.get("HBsAg") == 0.0

    def test_plt_hco3_already_extracted_by_core(self):
        got, _ = self._extract("Platelet 245 x10^3/uL\nHCO3 21.5 mEq/L")
        assert got.get("Platelet") == 245.0
        assert got.get("Bicarbonate") == 21.5


class TestSerologyClassify:
    """จัดกลุ่ม serology ทั้งหน่วย (serology.classify) — 22 พ.ย. 2569"""

    def _c(self, rec, today="2026-11-22"):
        from datetime import date
        from hd_module.serology import classify
        return classify(dict(rec), date.fromisoformat(today))

    def test_hbsag_positive_grouped(self):
        g = self._c({"hbsag": "positive", "hbsag_date": "2026-10-01"})
        assert "hbsag_pos" in g

    def test_hiv_hcv_positive_grouped(self):
        g = self._c({"antihcv": "positive", "antihiv": "positive",
                     "antihcv_date": "2026-10-01", "antihiv_date": "2026-10-01",
                     "antihbs": "positive"})
        assert "antihcv_pos" in g and "antihiv_pos" in g

    def test_no_immunity_when_antihbs_negative_or_missing(self):
        assert "no_immunity" in self._c({"hbsag": "negative", "antihbs": "negative",
                                         "hbsag_date": "2026-10-01"})
        assert "no_immunity" in self._c({"hbsag": "negative", "hbsag_date": "2026-10-01"})

    def test_carrier_not_flagged_for_vaccine(self):
        # HBsAg+ = พาหะ — วัคซีนไม่ช่วย ไม่ควรติดหมวดภูมิไม่ขึ้น
        assert "no_immunity" not in self._c({"hbsag": "positive", "hbsag_date": "2026-10-01"})

    def test_immune_patient_clean(self):
        g = self._c({"hbsag": "negative", "antihbs": "positive", "antihcv": "negative",
                     "antihiv": "negative", "hbsag_date": "2026-06-01",
                     "antihbs_date": "2026-06-01", "antihcv_date": "2026-06-01",
                     "antihiv_date": "2026-06-01"})
        assert g == []

    def test_annual_due_at_365_days(self):
        g = self._c({"hbsag": "negative", "antihbs": "positive",
                     "hbsag_date": "2025-11-22", "antihbs_date": "2025-11-22"})
        assert "annual_due" in g
        g2 = self._c({"hbsag": "negative", "antihbs": "positive",
                      "hbsag_date": "2025-11-23", "antihbs_date": "2025-11-23"})
        assert "annual_due" not in g2

    def test_never_tested(self):
        g = self._c({})
        assert "never_tested" in g and "no_immunity" in g

    def test_newest_date_across_fields_used(self):
        # HBsAg เก่าเกินปี แต่ Anti-HBs เพิ่งตรวจ → ยังไม่ครบกำหนด
        g = self._c({"hbsag": "negative", "hbsag_date": "2024-01-01",
                     "antihbs": "positive", "antihbs_date": "2026-09-01"})
        assert "annual_due" not in g


class TestUnitSummaryAutocommit:
    """Regression: SAVEPOINT ใน autocommit เคยทำให้ serology ทั้งหน่วยเป็น '?' (22 พ.ย.)"""

    class FakeCur:
        """cursor แบบ autocommit: SAVEPOINT/RELEASE/ROLLBACK ล้มเสมอ แต่ SELECT ทำงานปกติ"""
        def __init__(self):
            self._rows = []
        def execute(self, sql, params=None):
            s = sql.strip().upper()
            if s.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK")):
                raise RuntimeError("SAVEPOINT can only be used in transaction blocks")
            if "FROM HD_PATIENT_MAPPING" in s:
                self._rows = [(1, "67385", "นาย ก"), (2, "67383", "นาย ข")]
            elif "FROM HD_LABS" in s:
                from datetime import date
                self._rows = [(1, date(2026, 11, 19), "negative", None, "positive", None)]
            else:
                self._rows = []
        def fetchall(self):
            return self._rows

    def test_summary_survives_autocommit(self):
        from datetime import date
        from hd_module.serology import unit_summary
        out = unit_summary(self.FakeCur(), today=date(2026, 11, 22))
        assert out["counts"]["total"] == 2
        by_hn = {p["hn"]: p for p in out["patients"]}
        assert by_hn["67385"]["hbsag"] == "negative"
        assert by_hn["67385"]["antihbs"] == "positive"
        assert "never_tested" not in by_hn["67385"]["groups"]
        assert "never_tested" in by_hn["67383"]["groups"]
