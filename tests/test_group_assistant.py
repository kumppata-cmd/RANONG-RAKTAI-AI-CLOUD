"""Group assistant (ว้าว #8) — prefix matching + routing with a fake DB."""
import unittest
from datetime import date, datetime

from hd_module import group_assistant as ga


class FakeCur:
    """Returns canned rows per SQL fragment; records queries."""
    def __init__(self, table):
        self.table = table; self.rows = []; self.queries = []
    def execute(self, sql, params=()):
        self.queries.append(sql)
        for frag, rows in self.table.items():
            if frag in sql:
                self.rows = rows; return
        if sql.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK")):
            return
        self.rows = []
    def fetchall(self): return self.rows
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeConn:
    def __init__(self, table): self.cur = FakeCur(table)
    def cursor(self): return self.cur


class MatchTests(unittest.TestCase):
    def test_prefix_variants(self):
        self.assertEqual(ga.match("@รักษ์ไต เตียง"), "เตียง")
        self.assertEqual(ga.match("รักษ์ไต: HN 123"), "HN 123")
        self.assertEqual(ga.match("@raktai help"), "help")
        self.assertEqual(ga.match("@รักษ์ไต"), "")

    def test_not_addressed(self):
        self.assertIsNone(ga.match("สวัสดีครับ"))
        self.assertIsNone(ga.match("คลินิกรักษ์ไตเปิดกี่โมง"))
        self.assertIsNone(ga.match(""))


class AnswerTests(unittest.TestCase):
    def test_help(self):
        self.assertIn("เตียง", ga.answer(FakeConn({}), ""))
        self.assertIn("ไม่สั่งการรักษา", ga.answer(FakeConn({}), "help"))

    def test_beds_empty_and_full(self):
        self.assertIn("ไม่มีผู้ป่วยบนเตียง", ga.answer(FakeConn({}), "เตียง"))
        c = FakeConn({"FROM hd_sessions s LEFT JOIN hd_patient_mapping p ON p.id = s.patient_id\n                      WHERE s.session_date = %s AND s.status IN":
                      [(1, "นาย ก", "100", "in_progress", datetime(2026, 11, 12, 1, 0), "morning"),
                       (2, "นาง ข", "101", "scheduled", None, "morning")]})
        out = ga.answer(c, "เตียงว่างไหม")
        self.assertIn("กำลังฟอก 1", out); self.assertIn("เตียง 2: นาง ข", out); self.assertIn("08:00", out)

    def test_esa(self):
        c = FakeConn({"hd_esa_proposals e": [("นาย ก", "100", 9.1, "increase", "5000 IU")]})
        out = ga.answer(c, "ESA")
        self.assertIn("รอแพทย์อนุมัติ 1", out); self.assertIn("เพิ่ม 5000 IU", out)

    def test_water_overdue(self):
        c = FakeConn({"hd_water_quality": [(date(2026, 9, 1), "bacteria", "<1", "CFU/mL", "pass")]})
        out = ga.answer(c, "น้ำ")
        self.assertIn("bacteria <1 CFU/mL ✓", out); self.assertIn("เกิน 30 วัน", out)

    def test_patient_by_hn_and_unknown(self):
        c = FakeConn({"WHERE UPPER(hn) = %s": [(1, "100", "นาย ก", "AVF ซ้าย", 55.0, "Eprex", "4000 IU", "จ,พ,ศ")]})
        out = ga.answer(c, "HN 100")
        self.assertIn("นาย ก (HN 100)", out); self.assertIn("AVF ซ้าย", out); self.assertIn("Eprex 4000 IU", out)
        self.assertIn("ไม่พบ", ga.answer(FakeConn({}), "xyz"))

    def test_patient_ambiguous(self):
        c = FakeConn({"patient_name ILIKE %s": [(1, "100", "นาย ก", None, None, None, None, None),
                                               (2, "101", "นาย กข", None, None, None, None, None)]})
        self.assertIn("พบหลายคน", ga.answer(c, "นาย ก"))


if __name__ == "__main__":
    unittest.main()
