import unittest
from datetime import datetime, timedelta, timezone

from hd_module import uf_watch as uw

T0 = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


class EvaluateTests(unittest.TestCase):
    def test_on_plan(self):
        r = uw.evaluate(2400, 1200, T0, 4, 60, at(115), now=at(120))
        self.assertEqual(r["flag"], "ok")
        self.assertEqual((r["expected"], r["pct"], r["behind_ml"]), (1200, 50, 0))
        self.assertEqual(r["need_rate"], 600)
        self.assertEqual(r["need_rate_kg"], 10.0)

    def test_behind_ordering(self):
        r = uw.evaluate(2400, 900, T0, 4, 60, at(115), now=at(120))
        self.assertIn(r["flag"], ("behind", "caution", "high_rate"))
        self.assertEqual(r["behind_ml"], 300)
        self.assertEqual(r["need_rate_kg"], 12.5)
        self.assertEqual(r["flag"], "behind")             # behind outranks caution

    def test_high_rate(self):
        r = uw.evaluate(3000, 600, T0, 4, 50, at(175), now=at(180))   # 2400 mL left in 1 h = 48 mL/kg/h
        self.assertEqual(r["flag"], "high_rate")
        self.assertIn("ต้องใช้", r["text"])

    def test_no_record_and_overdue(self):
        self.assertEqual(uw.evaluate(2400, None, T0, 4, 60, None, now=at(30))["flag"], "ok")
        self.assertEqual(uw.evaluate(2400, None, T0, 4, 60, None, now=at(70))["flag"], "no_record")
        self.assertEqual(uw.evaluate(2400, 1200, T0, 4, 60, at(40), now=at(120))["flag"], "overdue")

    def test_no_goal_or_start(self):
        self.assertEqual(uw.evaluate(None, 500, T0, 4, 60, None)["flag"], "no_goal")
        self.assertEqual(uw.evaluate(2400, 500, None, 4, 60, None)["flag"], "no_goal")

    def test_no_dw_still_works(self):
        r = uw.evaluate(2400, 1200, T0, 4, None, at(115), now=at(120))
        self.assertEqual(r["flag"], "ok")
        self.assertIsNone(r["need_rate_kg"])

    def test_handover_lines(self):
        items = [{"bed": 1, "uf": uw.evaluate(2400, 1200, T0, 4, 60, at(115), now=at(120))},
                 {"bed": 5, "uf": uw.evaluate(3000, 600, T0, 4, 50, at(175), now=at(180))}]
        L = uw.handover_lines(items)
        self.assertEqual(L[0], "💧 UF ต้องดู 1 เตียง:")
        self.assertIn("เตียง 5", L[1])
        self.assertEqual(uw.handover_lines([items[0]]), ["💧 UF: ทุกเตียงตามแผน"])


if __name__ == "__main__":
    unittest.main()


class AdjustedTargetTests(unittest.TestCase):
    """16 พ.ย. 2569 (ทดสอบ HN 99999): เป้า 2500 / 4 ชม. / DW 60 — 1:52 ชม. ดึงได้ 500"""

    def test_without_adjustment_is_high_rate(self):
        r = uw.evaluate(2500, 500, T0, 4, 60, at(111), now=at(112))
        self.assertEqual(r["flag"], "high_rate")
        self.assertGreater(r["need_rate_kg"], 13)

    def test_safe_rate_target_stops_repeat_alarm_but_still_flags(self):
        r = uw.evaluate(2500, 500, T0, 4, 60, at(111), now=at(112), target=1777)
        self.assertEqual(r["flag"], "target_reduced")
        self.assertEqual((r["target"], r["reduced_ml"]), (1777, 723))
        self.assertLessEqual(r["need_rate_kg"], 10.0)
        self.assertIn("เป้าบนเครื่อง 1777", r["text"])
        self.assertNotIn("ช้ากว่าแผน", r["text"])

    def test_extend_time(self):
        r = uw.evaluate(2500, 500, T0, 4, 60, at(111), now=at(112), extend_min=75)
        self.assertEqual(r["extend_min"], 75)
        self.assertLessEqual(r["need_rate_kg"], 10.0)
        self.assertIn("ยืดเวลา +75", r["text"])

    def test_target_above_goal_ignored(self):
        r = uw.evaluate(2400, 1200, T0, 4, 60, at(115), now=at(120), target=3000)
        self.assertEqual((r["flag"], r["reduced_ml"]), ("ok", 0))

    def test_parse_adjust(self):
        self.assertEqual(uw.parse_adjust("target=1777;extend=75;note"), (1777, 75))
        self.assertEqual(uw.parse_adjust("sheet"), (None, 0))
        self.assertEqual(uw.parse_adjust(None), (None, 0))


class _Cur:
    def __init__(self, rows):
        self.rows, self.q = list(rows), []

    def execute(self, sql, params=None):
        self.q.append(sql)

    def fetchone(self):
        return self.rows.pop(0)


class LatestRecordTests(unittest.TestCase):
    def test_uses_highest_cumulative_value(self):
        cur = _Cur([(820.0, at(150))])
        self.assertEqual(uw.latest_record(cur, 1), (820.0, at(150)))
        self.assertIn("MAX(uf_removed)", cur.q[0])
        self.assertIn("uf_removed > 0", cur.q[0])

    def test_only_zero_rows(self):
        cur = _Cur([(None, None), (at(30),)])
        self.assertEqual(uw.latest_record(cur, 1), (None, at(30)))
