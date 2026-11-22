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
