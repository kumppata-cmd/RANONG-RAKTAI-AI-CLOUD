"""ปิดรอบค้างอัตโนมัติ (>8 ชม.) ต้องยกเว้นรอบที่ปล่อยเตียงรอสรุป Post-HD (แบบ ก — แพทย์อนุมัติ 24 พ.ย. 2569)."""
import inspect
import re

from hd_module import _auto_complete_stale_sessions


def test_released_sessions_are_excluded_and_note_is_thai():
    src = inspect.getsource(_auto_complete_stale_sessions)
    m = re.search(r"UPDATE hd_sessions.*?RETURNING", src, re.S)
    assert m, "UPDATE statement not found"
    sql = m.group(0)
    assert "status = 'in_progress'" in sql
    assert "INTERVAL '8 hours'" in sql
    assert "bed_released_at IS NULL" in sql          # แบบ ก: ปล่อยเตียงแล้ว = ปิดโดยคนเท่านั้น
    assert "ไม่มีข้อมูล Post-HD" in sql              # note ภาษาไทย บอกชัดว่าช่องว่างมาจากการปิดอัตโนมัติ
    assert "auto-completed: stale" not in sql
