"""25 พ.ย. 2569 — เวลาสิ้นสุดที่ถูกต้อง, แก้ Pre-HD หลังเริ่มฟอก, เตือนรอสรุปค้าง, ถาดรอสรุป.

ตรวจตรรกะที่แยกออกมาเป็น helper (ไม่ต้องใช้ DB) + wiring ในหน้าเว็บ.
"""
import inspect
import os
from datetime import datetime, timedelta

import hd_module.blueprint as bp
from hd_module.blueprint import (TIME_EDIT_REASONS, _pre_hd_missing, _resolve_end_time,
                                 run_pending_summary_reminders)

SESS = os.path.join(os.path.dirname(__file__), "..", "hd_module", "templates", "hd_session.html")
DASH = os.path.join(os.path.dirname(__file__), "..", "hd_module", "templates", "hd_dashboard.html")


def _plan(start, **kw):
    p = {"started_at": start, "ended_at": None, "bed_released_at": None, "status": "in_progress",
         "planned_end": start + timedelta(hours=4), "duration_hours": 4.0, "extend_min": 0}
    p.update(kw)
    return p


class TestResolveEndTime:
    """ลำดับ: ช่องที่พยาบาลระบุ → เวลาปล่อยเตียง → ครบตามแผน (ถ้าสรุปช้ากว่า grace) → ตอนนี้"""

    def test_bed_release_wins_over_click_time(self, monkeypatch):
        monkeypatch.setattr(bp, "_end_grace_min", lambda: 15)
        st = datetime(2026, 11, 24, 22, 39)                      # 05:39 ไทย
        rel = st + timedelta(hours=4, minutes=2)                 # ปล่อยเตียง 09:41
        now = st + timedelta(hours=5, minutes=18)                # สรุป 10:57 (เคสจริง)
        t, src, note = _resolve_end_time(_plan(st, bed_released_at=rel), None, now=now)
        assert (t, src, note) == (rel, "bed_release", None)

    def test_late_summary_caps_at_planned_end_with_note(self, monkeypatch):
        monkeypatch.setattr(bp, "_end_grace_min", lambda: 15)
        st = datetime(2026, 11, 24, 22, 39)
        now = st + timedelta(hours=5, minutes=18)
        t, src, note = _resolve_end_time(_plan(st), None, now=now)
        assert t == st + timedelta(hours=4) and src == "planned_end"
        assert "09:39" in note and "สรุปล่าช้า 78 นาที" in note

    def test_within_grace_uses_now(self, monkeypatch):
        monkeypatch.setattr(bp, "_end_grace_min", lambda: 15)
        st = datetime(2026, 11, 24, 22, 39)
        now = st + timedelta(hours=4, minutes=10)
        t, src, _ = _resolve_end_time(_plan(st), None, now=now)
        assert (t, src) == (now, "now")

    def test_extension_moves_planned_end(self, monkeypatch):
        monkeypatch.setattr(bp, "_end_grace_min", lambda: 15)
        st = datetime(2026, 11, 24, 22, 39)
        plan = _plan(st, planned_end=st + timedelta(hours=4, minutes=45), extend_min=45)
        now = st + timedelta(hours=4, minutes=50)                # ขยาย 45 นาที สรุปช้า 5 นาที → ตอนนี้
        t, src, _ = _resolve_end_time(plan, None, now=now)
        assert src == "now"

    def test_nurse_time_wins_and_is_validated(self, monkeypatch):
        monkeypatch.setattr(bp, "_end_grace_min", lambda: 15)
        st = datetime(2026, 11, 24, 22, 39)
        now = st + timedelta(hours=5)
        t, src, _ = _resolve_end_time(_plan(st, bed_released_at=st + timedelta(hours=4)), "09:30", now=now)
        assert src == "nurse" and (t + timedelta(hours=7)).strftime("%H:%M") == "09:30"
        # ก่อนเวลาเริ่ม → ไม่รับ ตกไปใช้เวลาปล่อยเตียง
        t2, src2, _ = _resolve_end_time(_plan(st, bed_released_at=st + timedelta(hours=4)), "05:00", now=now)
        assert src2 == "bed_release"
        # ผิดรูปแบบ → ตกชั้นถัดไป
        _, src3, _ = _resolve_end_time(_plan(st), "abc", now=st + timedelta(hours=1))
        assert src3 == "now"


class TestPreHdMissing:
    def test_lists_only_important_gaps(self):
        assert _pre_hd_missing({"pre_vitals": {"sbp": 130}, "pre_weight": 60, "dialyzer_type": "F8", "access_type": "AVF"}) == []
        miss = _pre_hd_missing({"pre_vitals": {}, "pre_weight": None, "dialyzer_type": None, "vascular_access": "AVF"})
        assert miss == ["BP ก่อนฟอก", "น้ำหนักก่อนฟอก", "ตัวกรอง"]


class TestEndpointsExist:
    def test_routes_and_reasons(self):
        rules = {r.rule for r in bp.hd_bp.url_map.iter_rules()} if hasattr(bp.hd_bp, "url_map") else set()
        src = inspect.getsource(bp)
        assert '"/sessions/<int:session_id>/pre-hd", methods=["PUT"]' in src
        assert '"/sessions/<int:session_id>/times", methods=["PUT"]' in src
        assert set(TIME_EDIT_REASONS) == {"late_start", "late_summary", "early_off", "other"}
        # ปิดรอบ: ไม่ใช่ ended_at = NOW() อย่างเดียวอีกต่อไป
        us = inspect.getsource(bp.update_session)
        assert "_resolve_end_time(_plan, data.get(\"ended_time\"))" in us
        # รอบที่ปิดแล้วแก้ได้เฉพาะเวลา (Pre-HD ไม่ได้)
        assert "แก้ไข Pre-HD ได้เฉพาะรอบที่กำลังฟอก" in inspect.getsource(bp.update_pre_hd)
        assert "7 วัน" in inspect.getsource(bp.session_times_edit)


class TestPendingSummaryReminders:
    def test_no_db_is_noop(self, monkeypatch):
        monkeypatch.delenv("HD_DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert run_pending_summary_reminders() == {"checked": 0, "sent": 0}

    def test_stages_claim_and_message(self, monkeypatch):
        now = datetime(2026, 11, 25, 3, 0)   # 10:00 ไทย
        rows = [(301, 5, "สมชาย ใจดี", now - timedelta(minutes=35), now.date()),
                (302, 7, "สมหญิง", now - timedelta(minutes=75), now.date()),
                (303, 9, "เก่ามาก", now - timedelta(hours=7), now.date())]   # >6 ชม. → ปล่อยให้ปิดอัตโนมัติ

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a): pass
            def fetchall(self): return rows

        class Conn:
            def cursor(self): return Cur()
            def close(self): pass

        import types
        fake_pg = types.SimpleNamespace(connect=lambda *a, **k: Conn())
        monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake_pg)
        monkeypatch.setenv("HD_DATABASE_URL", "postgres://x")
        bp._SUMMARY_REMIND_SENT.clear()
        sent, claimed = [], []
        res = run_pending_summary_reminders(claim_fn=lambda job, day: claimed.append(job) or True, now_utc=now,
                                            push_fn=lambda m: sent.append(m) or True, base_url="https://x.test")
        assert res["checked"] == 3 and res["sent"] == 2
        assert claimed == ["sumrem_301_30", "sumrem_302_60"]
        assert "เตียง 5 (สมชาย)" in sent[0] and "35 นาที" in sent[0] and "/hd/session/301" in sent[0]
        assert "ปิดรอบอัตโนมัติ" in sent[1] and "ปิดรอบอัตโนมัติ" not in sent[0]
        # รอบเดิม stage เดิม → ไม่ส่งซ้ำ
        res2 = run_pending_summary_reminders(claim_fn=lambda job, day: True, now_utc=now + timedelta(minutes=5),
                                             push_fn=lambda m: sent.append(m) or True)
        assert res2["sent"] == 0

    def test_scheduler_wired_daytime_only(self):
        import hd_module
        src = inspect.getsource(hd_module._start_daily_reminder_scheduler)
        assert "run_pending_summary_reminders" in src and "5 <= now.hour <= 23" in src


class TestPageWiring:
    def test_session_page(self):
        with open(SESS, encoding="utf-8") as f:
            s = f.read()
        for needle in ('id="preEditBar"', "function preEditStart(", "function preEditSave(", "'/pre-hd'",
                       'id="postEndTime"', "ended_time:", "function postEndTimeSuggest(", "function timerFreezeIfDone(",
                       'id="preMissingBanner"', "pre_hd_missing", "function timeEditRender(", "'/times'",
                       "sessionData.bed_released_at = new Date().toISOString()"):
            assert needle in s, needle
        # ผู้ป่วย/เตียงต้องล็อกเสมอแม้ตอนแก้ไข
        assert "PRE_EDIT_LOCKED = ['patientSelect','bedSelect'" in s

    def test_dashboard_tray(self):
        with open(DASH, encoding="utf-8") as f:
            s = f.read()
        assert "waited_min" in s and "รอ ' + waitTxt(w)" in s
        assert "m > 60" in s and "m >= 30" in s
