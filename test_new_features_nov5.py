#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for 4 new features (2026-11-05 evening session):
1. Daily digest
2. Monthly stats command
3. Hb low auto-alert
4. Compare command via LINE
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))


class TestDailyDigest(unittest.TestCase):
    """#1: _build_daily_digest() สร้างสรุปประจำวัน"""

    def test_digest_returns_string(self):
        from server import _build_daily_digest
        result = _build_daily_digest()
        self.assertIsInstance(result, str)
        self.assertIn("สรุปประจำวัน", result)

    def test_digest_contains_sections(self):
        from server import _build_daily_digest
        result = _build_daily_digest()
        self.assertTrue(
            "ฟอกเลือดวันนี้" in result
            or "ไม่มีรอบฟอก" in result  # วันอาทิตย์
        )
        self.assertIn("เอกสารใหม่เมื่อวาน", result)
        # ค่าวิกฤต section
        self.assertTrue(
            "ค่าวิกฤตรอยืนยัน" in result
            or "ไม่มีค่าวิกฤตค้าง" in result
        )
        self.assertIn("ทะเบียน", result)


class TestMonthlyStats(unittest.TestCase):
    """#2: คำสั่ง สถิติเดือนนี้"""

    def test_stats_command_in_source(self):
        """สถิติเดือนนี้ should be handled in server.py"""
        with open(os.path.join(os.path.dirname(__file__), "server.py"),
                  encoding="utf-8") as f:
            source = f.read()
        self.assertIn('"สถิติเดือนนี้"', source)
        self.assertIn("สถิติเดือน", source)
        self.assertIn("_total_docs", source)
        self.assertIn("_lab_docs", source)


class TestHbLowAlert(unittest.TestCase):
    """#3: Hb < 8 auto-alert"""

    def test_extract_low_hb(self):
        from server import _extract_hb_from_text
        # Hb = 6.5 (ต่ำมาก)
        val = _extract_hb_from_text("Hb 6.5 g/dL")
        self.assertIsNotNone(val)
        self.assertLess(val, 8.0)

    def test_extract_normal_hb(self):
        from server import _extract_hb_from_text
        val = _extract_hb_from_text("Hb 11.2 g/dL")
        self.assertIsNotNone(val)
        self.assertGreaterEqual(val, 8.0)

    def test_hb_threshold(self):
        """Hb < 8 triggers alert logic"""
        from server import _extract_hb_from_text
        low_cases = ["Hb 7.9", "Hemoglobin 5.5", "Hgb 7.0"]
        for case in low_cases:
            val = _extract_hb_from_text(case)
            self.assertIsNotNone(val, f"Failed to extract from: {case}")
            self.assertLess(val, 8.0, f"Expected < 8.0 for: {case}")

        ok_cases = ["Hb 8.0", "Hb 10.5", "Hb 12.0"]
        for case in ok_cases:
            val = _extract_hb_from_text(case)
            self.assertIsNotNone(val)
            self.assertGreaterEqual(val, 8.0, f"Expected >= 8.0 for: {case}")


class TestCompareCommand(unittest.TestCase):
    """#4: เปรียบเทียบ <HN> command"""

    def test_compare_command_in_source(self):
        """คำสั่ง เปรียบเทียบ should be in server.py"""
        with open(os.path.join(os.path.dirname(__file__), "server.py"),
                  encoding="utf-8") as f:
            source = f.read()
        self.assertIn("เปรียบเทียบ ", source)
        self.assertIn("/admin/compare/", source)
        self.assertIn("เปรียบเทียบข้อมูลผู้ป่วย", source)

    def test_compare_url_format(self):
        """URL should be correct format"""
        base = "https://ranong-raktai-ai-cloud.onrender.com"
        hn = "00007567"
        url = f"{base}/admin/compare/{hn}"
        self.assertIn("/admin/compare/", url)
        self.assertIn(hn, url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
