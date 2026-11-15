"""Tests for hd_module.monthly_summary."""
import unittest
from datetime import date
from hd_module.monthly_summary import (
    _f, _interpret_hb, _interpret_ktv, _trend_arrow,
    build_message, collect_monthly_data, MONTH_TH,
)


class InterpretTests(unittest.TestCase):
    def test_hb_ranges(self):
        self.assertIn("ต่ำมาก", _interpret_hb(6.5))
        self.assertIn("ต่ำกว่าเป้า", _interpret_hb(9.0))
        self.assertIn("อยู่ในเป้า", _interpret_hb(11.0))
        self.assertIn("สูงกว่าเป้า", _interpret_hb(13.0))
        self.assertEqual("", _interpret_hb(None))

    def test_ktv_ranges(self):
        self.assertIn("ดีมาก", _interpret_ktv(1.5))
        self.assertIn("ผ่านเกณฑ์", _interpret_ktv(1.2))
        self.assertIn("ต่ำกว่าเกณฑ์", _interpret_ktv(1.0))

    def test_trend_arrow(self):
        self.assertEqual("↑", _trend_arrow(11.0, 10.0))
        self.assertEqual("↓", _trend_arrow(9.0, 10.0))
        self.assertEqual("→", _trend_arrow(10.0, 10.1))
        self.assertEqual("", _trend_arrow(None, 10.0))
        self.assertEqual("", _trend_arrow(10.0, None))


class BuildMessageTests(unittest.TestCase):
    def test_basic_message(self):
        rec = {
            "name": "นายสมชาย รักดี",
            "hn": "12345",
            "dw": 60.0,
            "goal": "คุมน้ำหนักระหว่างรอบ <2.5 กก.",
            "this": {"hb": 10.5, "hct": 32.0, "ktv": 1.3, "ferritin": 450.0, "ipth": 250.0},
            "prev": {"hb": 9.8, "ktv": 1.1},
            "sessions": 12,
            "avg_uf": 2.8,
            "idwg_over": 2,
        }
        msg = build_message(rec, 2026, 10)
        self.assertIn("สรุปผลเลือดประจำเดือน ต.ค. 2569", msg)
        self.assertIn("นายสมชาย รักดี", msg)
        self.assertIn("Hb: 10.5", msg)
        self.assertIn("↑", msg)  # Hb went up
        self.assertIn("Kt/V: 1.3", msg)
        self.assertIn("Ferritin: 450.0", msg)
        self.assertIn("iPTH: 250.0", msg)
        self.assertIn("12 ครั้ง", msg)
        self.assertIn("2 ครั้ง", msg)  # IDWG over
        self.assertIn("คุมน้ำหนัก", msg)  # Goal
        self.assertIn("พิมพ์ 'ผลเลือด'", msg)

    def test_minimal_data(self):
        rec = {
            "name": "นางสมหญิง",
            "hn": "99999",
            "dw": None,
            "goal": None,
            "this": {"hb": 8.0},
            "prev": {},
            "sessions": 4,
            "avg_uf": None,
            "idwg_over": 0,
        }
        msg = build_message(rec, 2026, 11)
        self.assertIn("พ.ย. 2569", msg)
        self.assertIn("Hb: 8.0", msg)
        self.assertNotIn("Kt/V", msg)  # No ktv data
        self.assertNotIn("เป้าหมาย", msg)  # No goal


class MonthThTests(unittest.TestCase):
    def test_all_months(self):
        self.assertEqual(len(MONTH_TH), 12)
        self.assertEqual(MONTH_TH[1], "ม.ค.")
        self.assertEqual(MONTH_TH[12], "ธ.ค.")


if __name__ == "__main__":
    unittest.main()
