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


class TestShiftForThaiHour:
    """เกณฑ์รอบฟอก (24 พ.ย. 2569): หน่วยเริ่มรอบแรกได้ตั้งแต่ ~05:00"""

    def test_early_morning_is_morning(self):
        from hd_module.blueprint import _shift_for_thai_hour as f
        # บั๊กจริง: 05:30 เคยถูกจัดเป็นรอบค่ำ
        for h in (3, 4, 5, 6, 7, 10, 12):
            assert f(h) == "morning", h

    def test_afternoon(self):
        from hd_module.blueprint import _shift_for_thai_hour as f
        for h in (13, 14, 16):
            assert f(h) == "afternoon", h

    def test_evening_and_night(self):
        from hd_module.blueprint import _shift_for_thai_hour as f
        for h in (17, 19, 22, 23, 0, 1, 2):
            assert f(h) == "evening", h
