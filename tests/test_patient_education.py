"""Tests for hd_module.patient_education."""
import unittest
from datetime import date
from hd_module.patient_education import (
    TIPS, get_tip_of_week, build_tip_message,
)


class TipsTests(unittest.TestCase):
    def test_52_tips(self):
        self.assertGreaterEqual(len(TIPS), 52)
        for t in TIPS:
            self.assertIn("icon", t)
            self.assertIn("title", t)
            self.assertIn("body", t)
            self.assertTrue(len(t["body"]) > 10)

    def test_get_tip_rotates(self):
        t1, i1 = get_tip_of_week(date(2026, 1, 5))   # week 1
        t2, i2 = get_tip_of_week(date(2026, 1, 12))  # week 2
        self.assertNotEqual(i1, i2)

    def test_build_message(self):
        tip = TIPS[0]
        msg = build_tip_message(tip, week_num=1)
        self.assertIn("สาระสุขภาพไต", msg)
        self.assertIn(tip["title"], msg)
        self.assertIn(tip["body"], msg)
        self.assertIn("คลินิกระนอง", msg)


if __name__ == "__main__":
    unittest.main()
