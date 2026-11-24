"""_parse_entry_time — แก้/ระบุเวลาวัดจริง (23 พ.ย. 2569).

กติกา: 'HH:MM' คือเวลาไทยที่ตั้งใจ เลือก "วันที่" ให้ใกล้ anchor ที่สุด
เพื่อรองรับรอบฟอกคร่อมเที่ยงคืน และบันทึกเป็น UTC (−7 ชม.)
"""
from datetime import datetime

from hd_module.blueprint import _parse_entry_time


def _u(s):
    return datetime.fromisoformat(s)


class TestParseEntryTime:
    def test_none_and_invalid(self):
        anchor = _u("2026-11-23T11:00:00")
        assert _parse_entry_time(None, anchor) is None
        assert _parse_entry_time("", anchor) is None
        assert _parse_entry_time("25:00", anchor) is None
        assert _parse_entry_time("17:65", anchor) is None
        assert _parse_entry_time("abc", anchor) is None
        assert _parse_entry_time("17.30", anchor) is None

    def test_same_day_simple(self):
        # anchor 18:00 ไทย (11:00Z) ตั้ง 17:30 → 10:30Z วันเดียวกัน
        out = _parse_entry_time("17:30", _u("2026-11-23T11:00:00"))
        assert out == _u("2026-11-23T10:30:00")

    def test_single_digit_hour(self):
        out = _parse_entry_time("7:05", _u("2026-11-23T01:00:00"))  # anchor 08:00 ไทย
        assert out == _u("2026-11-23T00:05:00")

    def test_just_after_midnight_backdates(self):
        # anchor 00:10 ไทย (= 17:10Z วันก่อน) ตั้ง 23:45 → ต้องเป็น 23:45 ของคืนก่อน
        out = _parse_entry_time("23:45", _u("2026-11-23T17:10:00"))
        assert out == _u("2026-11-23T16:45:00")  # 23:45 ไทย = 16:45Z

    def test_before_midnight_forward(self):
        # anchor 23:50 ไทย (16:50Z) ตั้ง 00:05 → เที่ยงคืนถัดไป
        out = _parse_entry_time("00:05", _u("2026-11-23T16:50:00"))
        assert out == _u("2026-11-23T17:05:00")  # 00:05 ไทยวันถัดไป

    def test_roundtrip_thai_display(self):
        from datetime import timedelta
        anchor = _u("2026-11-23T11:00:00")
        for hhmm in ("00:00", "06:30", "12:00", "17:49", "23:59"):
            out = _parse_entry_time(hhmm, anchor)
            assert (out + timedelta(hours=7)).strftime("%H:%M") == hhmm


class TestShiftForThaiTime:
    """เกณฑ์รอบฟอก — สเปคแพทย์ 24 พ.ย. 2569:
    เช้า 05:00–09:29 · บ่าย 09:30–13:59 · ค่ำ 14:00 เป็นต้นไป"""

    def test_morning_window(self):
        from hd_module.blueprint import _shift_for_thai_time as f
        for h, m in ((5, 0), (5, 30), (7, 0), (9, 0), (9, 29)):
            assert f(h, m) == "morning", (h, m)

    def test_afternoon_window(self):
        from hd_module.blueprint import _shift_for_thai_time as f
        for h, m in ((9, 30), (10, 0), (12, 0), (13, 59)):
            assert f(h, m) == "afternoon", (h, m)

    def test_evening_window_incl_night(self):
        from hd_module.blueprint import _shift_for_thai_time as f
        for h, m in ((14, 0), (16, 30), (18, 30), (20, 0), (23, 59), (0, 0), (3, 0), (4, 59)):
            assert f(h, m) == "evening", (h, m)

    def test_exact_boundaries(self):
        from hd_module.blueprint import _shift_for_thai_time as f
        assert f(9, 29) == "morning"
        assert f(9, 30) == "afternoon"
        assert f(13, 59) == "afternoon"
        assert f(14, 0) == "evening"
        assert f(4, 59) == "evening"
        assert f(5, 0) == "morning"

    def test_hour_wrapper_compat(self):
        from hd_module.blueprint import _shift_for_thai_hour as f
        assert f(6) == "morning" and f(10) == "afternoon" and f(15) == "evening"


class TestUnifiedIntraHelpers:
    """ปุ่มเดียวครบทุกค่า (24 พ.ย. 2569): ช่อง sheet + กุญแจกันซ้ำ"""

    def test_slot_for_started_matches_sheet_grid(self):
        from hd_module.blueprint import _slot_for_started
        started = _u("2026-11-24T10:35:00")  # 17:35 ไทย → base 17:30
        assert _slot_for_started(started, _u("2026-11-24T10:49:00")) == "18:00"  # 17:49 → ใกล้ 18:00
        assert _slot_for_started(started, _u("2026-11-24T10:40:00")) == "17:30"  # 17:40 → 17:30
        assert _slot_for_started(started, _u("2026-11-24T14:36:00")) == "21:30"
        # ไม่เกิน 12 ช่อง (base + 5.5 ชม.)
        assert _slot_for_started(started, _u("2026-11-24T23:00:00")) == "23:00"

    def test_slot_for_started_rounds_start_down_before_15(self):
        from hd_module.blueprint import _slot_for_started
        started = _u("2026-11-24T22:10:00")  # 05:10 ไทย → base 05:00
        assert _slot_for_started(started, started) == "05:00"

    def test_idem_key_normalisation(self):
        from hd_module.blueprint import _idem_key
        assert _idem_key({}) is None
        assert _idem_key({"idempotency_key": "  "}) is None
        assert _idem_key({"idempotency_key": " abc "}) == "abc"
        assert len(_idem_key({"idempotency_key": "x" * 100})) == 64

    def test_idem_lookup_tolerates_missing_table(self):
        from hd_module.blueprint import _idem_lookup
        class Cur:
            def execute(self, sql, params=None):
                if "hd_intra_submissions" in sql:
                    raise RuntimeError("relation does not exist")
            def fetchone(self):
                return None
        assert _idem_lookup(Cur(), 1, "k") is None
