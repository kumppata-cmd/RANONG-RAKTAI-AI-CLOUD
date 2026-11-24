"""⏰ เตือนใกล้ครบเวลาฟอก 30/15/5 นาที (24 พ.ย. 2569 — ข้อเสนอพยาบาล)."""
import os
import re

from hd_module.blueprint import _extend_min_from_notes

TPL = os.path.join(os.path.dirname(__file__), "..", "hd_module", "templates", "hd_dashboard.html")


class TestExtendMinFromNotes:
    def test_parse(self):
        assert _extend_min_from_notes("target=1777;extend=75") == 75
        assert _extend_min_from_notes("sheet;extend=30") == 30
        assert _extend_min_from_notes("sheet") == 0
        assert _extend_min_from_notes(None) == 0
        assert _extend_min_from_notes("extend=999") == 0   # เกินช่วงที่ยอมรับ → ไม่ใช้


class TestDashboardWiring:
    def test_three_stages_three_colors_three_sounds(self):
        with open(TPL, encoding="utf-8") as f:
            s = f.read()
        for cls, color in (("endwarn-30", "#2ecc71"), ("endwarn-15", "#f1c40f"), ("endwarn-5", "#e74c3c")):
            assert re.search(rf"\.bed-card\.{cls}\s*\{{\s*--ew:{color}", s), cls
        assert "{min:30" in s and "{min:15" in s and "{min:5" in s
        for snd in ("'chime'", "'triple'", "'urgent'"):
            assert snd in s, snd
        assert 'id="endwarnBtn"' in s and "function checkEndWarnings(" in s
        assert "extend_min" in s   # ยืดเวลาบนเครื่อง → เวลาครบเลื่อนตาม
