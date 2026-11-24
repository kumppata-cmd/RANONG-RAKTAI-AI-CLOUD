"""ระบบ UF เดียวกันทั้ง 3 หน้า (24 พ.ย. 2569).

หน้ารอบฟอก (บันทึก UF), ฟอร์มรวม 📝 และ HD Monitoring Sheet ต้องใช้ "สมอง UF" ตัวเดียว
(_uf_brain.html) และข้อมูล UF จาก Sheet ต้องไหลกลับไป hd_uf_records ด้วยเวลาของช่อง
"""
import os
import re
from datetime import datetime

from hd_module.blueprint import _parse_entry_time

TPL = os.path.join(os.path.dirname(__file__), "..", "hd_module", "templates")


def _read(name):
    with open(os.path.join(TPL, name), encoding="utf-8") as f:
        return f.read()


class TestSharedBrainInclude:
    def test_brain_has_no_script_tag_and_defines_core(self):
        b = _read("_uf_brain.html")
        assert "<script" not in b
        for fn in ("function _ufCtx(", "function ufAutoCalc(", "function _ufRenderPlan(",
                   "function ufApplyOpt(", "function _ufSafetyWarn(", "function _ufAdjPayload("):
            assert fn in b, fn
        for ids in ("_UF_IDS_LEGACY", "_UF_IDS_UNIFIED", "_UF_IDS_SHEET"):
            assert ids in b, ids
        # ไม่มี */ ซ้อนใน comment (เคยทำให้ JS ทั้งหน้าพัง)
        assert b.count("/*") == b.count("*/")

    def test_both_pages_include_brain_once_and_do_not_redefine(self):
        for page in ("hd_session.html", "hd_sheet.html"):
            s = _read(page)
            assert s.count('{% include "_uf_brain.html" %}') == 1, page
            for sym in ("function ufAutoCalc(", "function _ufCtx(", "let _lastUfRemoved", "let _ufCoach",
                        "let _ufSaveConfirm", "const _UF_IDS_LEGACY"):
                assert sym not in s, f"{page} redefines {sym}"

    def test_session_page_keeps_page_specific_functions(self):
        s = _read("hd_session.html")
        for fn in ("function openUfModal(", "function saveUfRecord(", "function readMachineScreen(",
                   "function openIntraModal(", "function saveIntraRecord("):
            assert fn in s, fn

    def test_sheet_markup_matches_sheet_id_map(self):
        s = _read("hd_sheet.html")
        b = _read("_uf_brain.html")
        m = re.search(r"const _UF_IDS_SHEET\s*=\s*\{([^}]*)\}", b)
        assert m
        ids = re.findall(r"\w+:'([A-Za-z0-9_]+)'", m.group(1))
        assert ids
        for i in ids:
            assert f'id="{i}"' in s, f"sheet missing #{i}"
        # ช่องกรอกส่งผ่านสมองเดียวกัน
        assert "ufSheetCalc('total')" in s and "ufSheetCalc('ufr')" in s and "ufSheetCalc('remaining')" in s
        assert "_ufSafetyWarn(" in s and "_ufAdjPayload(" in s and "uf_remaining" in s


class TestSheetSlotTimestamp:
    """Sheet บันทึกช่อง HH:MM → recorded_at = เวลาของช่องบนวันที่เริ่มฟอก (anchor = started_at)."""

    def test_slot_after_start_same_day(self):
        started = datetime.fromisoformat("2026-11-24T08:30:00")   # 15:30 ไทย
        assert _parse_entry_time("19:30", started) == datetime.fromisoformat("2026-11-24T12:30:00")

    def test_slot_wraps_past_midnight(self):
        started = datetime.fromisoformat("2026-11-24T15:30:00")   # 22:30 ไทย
        assert _parse_entry_time("01:00", started) == datetime.fromisoformat("2026-11-24T18:00:00")

    def test_bad_slot_falls_back_to_none(self):
        assert _parse_entry_time("now", datetime.fromisoformat("2026-11-24T08:30:00")) is None
