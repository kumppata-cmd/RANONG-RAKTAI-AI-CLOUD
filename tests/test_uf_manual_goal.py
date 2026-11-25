"""25 พ.ย. 2569 — เป้า UF ที่พยาบาลตั้งเองใน UF Planner ต้องชนะสูตร pre − DW และไปถึงหน้า Intra/Sheet.

เคสจริง: DW 60 มา 65 (เกิน 5 kg) คนไข้มีธุระ ฟอก 4 ชม. → ตั้ง 4.0 L เอง แต่ระบบบันทึก 5000.
"""
import os
from datetime import datetime

import hd_module.blueprint as bp
from hd_module.blueprint import _notes_with_plan_hours, _plan_hours_from_notes, _plan_hours_tag, _session_plan

SESS = os.path.join(os.path.dirname(__file__), "..", "hd_module", "templates", "hd_session.html")


class TestPlanHoursTag:
    def test_roundtrip(self):
        assert _plan_hours_tag(4) == "[แผนฟอก 4 ชม.]"
        assert _plan_hours_tag("4.5") == "[แผนฟอก 4.5 ชม.]"
        assert _plan_hours_tag(1) is None and _plan_hours_tag(9) is None and _plan_hours_tag("x") is None
        assert _plan_hours_from_notes("[แผนฟอก 4.5 ชม.]") == 4.5
        assert _plan_hours_from_notes("อื่น ๆ\n[แผนฟอก 3 ชม.]\n[ปิดรอบโดยข้าม: x]") == 3
        assert _plan_hours_from_notes(None) is None and _plan_hours_from_notes("[แผนฟอก 12 ชม.]") is None

    def test_notes_merge_replaces_or_appends(self):
        assert _notes_with_plan_hours(None, 4) == "[แผนฟอก 4 ชม.]"
        assert _notes_with_plan_hours("note A", 5) == "note A\n[แผนฟอก 5 ชม.]"
        assert _notes_with_plan_hours("x\n[แผนฟอก 4 ชม.]\ny", 3.5) == "x\n[แผนฟอก 3.5 ชม.]\ny"
        assert _notes_with_plan_hours("x", 99) is None


class TestSessionPlanHonoursPlannerHours:
    def test_plan_hours_override_prescription(self):
        st = datetime(2026, 11, 24, 22, 39)

        class Cur:
            def execute(self, *a): pass
            def fetchone(self):
                #        started, ended, released, status, date, pid, rx_hours, extend_notes, notes, bed
                return (st, None, None, "in_progress", st.date(), 7, 5.0, None, "[แผนฟอก 4 ชม.]", 3)
        p = _session_plan(Cur(), 1)
        assert p["duration_hours"] == 4.0
        assert (p["planned_end"] - st).total_seconds() == 4 * 3600

    def test_without_tag_uses_prescription(self):
        st = datetime(2026, 11, 24, 22, 39)

        class Cur:
            def execute(self, *a): pass
            def fetchone(self): return (st, None, None, "in_progress", st.date(), 7, 5.0, None, None, 3)
        assert _session_plan(Cur(), 1)["duration_hours"] == 5.0


class TestWiring:
    def test_create_and_pre_hd_persist_plan_hours_and_dashboard_sheet_read_it(self):
        import inspect
        src = inspect.getsource(bp)
        assert src.count("_plan_hours_tag(data.get(\"plan_hours\"))") == 1
        assert "_notes_with_plan_hours(plan.get(\"notes\"), data.get(\"plan_hours\"))" in src
        assert "s.notes AS session_notes" in src and "_plan_hours_from_notes(row[23])" in src
        assert "SELECT notes FROM hd_sessions WHERE id = %s" in src   # flowsheet

    def test_session_page(self):
        with open(SESS, encoding="utf-8") as f:
            s = f.read()
        for needle in ("function ufPlannerManualMl(", "function ufPlannerHours(", "function ufPlannerSyncFromSession(",
                       'id="ufPlanManual"', "plan_hours: _planHours", "const _manualUf = ufPlannerManualMl();",
                       "✏️ ตั้งเป้า UF เอง"):
            assert needle in s, needle
        # calcIDWG ต้องไม่ทับเป้าที่ตั้งเองของรอบที่เริ่มแล้ว
        assert "!_simTouched && (!sessionData.session_id || _preEditOn)" in s
