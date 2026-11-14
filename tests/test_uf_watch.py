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
