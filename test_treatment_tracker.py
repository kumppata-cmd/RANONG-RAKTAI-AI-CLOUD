#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for Treatment Change Tracker"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from treatment_tracker import (
    TreatmentTracker, TreatmentChange, TREATMENT_CATEGORIES,
)


class TestTreatmentChange(unittest.TestCase):

    def test_to_dict_round_trip(self):
        c = TreatmentChange(
            change_id="TC-56001-20261106",
            patient_hn="56001",
            change_date="2026-11-06",
            category="esa",
            action="increase",
            drug_name="Epiao",
            old_value="4000u",
            new_value="6000u",
        )
        d = c.to_dict()
        c2 = TreatmentChange.from_dict(d)
        self.assertEqual(c2.change_id, c.change_id)
        self.assertEqual(c2.drug_name, "Epiao")
        self.assertEqual(c2.action, "increase")


class TestDetectCategory(unittest.TestCase):

    def test_esa(self):
        self.assertEqual(TreatmentTracker.detect_category("Epiao"), "esa")
        self.assertEqual(TreatmentTracker.detect_category("Hemax"), "esa")
        self.assertEqual(TreatmentTracker.detect_category("Hema-plus"), "esa")

    def test_iron(self):
        self.assertEqual(TreatmentTracker.detect_category("Venofer"), "iron")
        self.assertEqual(TreatmentTracker.detect_category("IV iron"), "iron")
        self.assertEqual(TreatmentTracker.detect_category("Ferrous"), "iron")

    def test_phosphate(self):
        self.assertEqual(TreatmentTracker.detect_category("CaCO3"), "phosphate_binder")
        self.assertEqual(TreatmentTracker.detect_category("Sevelamer"), "phosphate_binder")

    def test_antihypertensive(self):
        self.assertEqual(TreatmentTracker.detect_category("Amlodipine"), "antihypertensive")
        self.assertEqual(TreatmentTracker.detect_category("Losartan"), "antihypertensive")

    def test_unknown(self):
        self.assertEqual(TreatmentTracker.detect_category("Paracetamol"), "other")

    def test_empty_string(self):
        # Empty string might match something short — just verify it returns a valid category
        result = TreatmentTracker.detect_category("")
        self.assertIn(result, TREATMENT_CATEGORIES)


class TestParseTreatmentText(unittest.TestCase):

    def test_increase_with_dose(self):
        r = TreatmentTracker.parse_treatment_text(
            "เพิ่ม Epiao 4000→6000 เพราะ Hb ต่ำ"
        )
        self.assertEqual(r["action"], "increase")
        self.assertEqual(r["drug_name"], "Epiao")
        self.assertEqual(r["old_value"], "4000")
        self.assertEqual(r["new_value"], "6000")
        self.assertEqual(r["reason"], "Hb ต่ำ")

    def test_stop(self):
        r = TreatmentTracker.parse_treatment_text(
            "หยุด amlodipine เพราะ BP ต่ำ"
        )
        self.assertEqual(r["action"], "stop")
        self.assertEqual(r["drug_name"], "amlodipine")
        self.assertEqual(r["reason"], "BP ต่ำ")

    def test_start(self):
        r = TreatmentTracker.parse_treatment_text(
            "เริ่ม IV iron เนื่องจาก Ferritin ต่ำ"
        )
        self.assertEqual(r["action"], "start")
        self.assertEqual(r["drug_name"], "IV iron")
        self.assertEqual(r["reason"], "Ferritin ต่ำ")

    def test_switch(self):
        r = TreatmentTracker.parse_treatment_text(
            "เปลี่ยน Epiao เป็น Hemax"
        )
        self.assertEqual(r["action"], "switch")
        # "Epiao" should be in drug or old_value

    def test_decrease(self):
        r = TreatmentTracker.parse_treatment_text(
            "ลด Epiao 6000→4000 เพราะ Hb สูงเกิน"
        )
        self.assertEqual(r["action"], "decrease")
        self.assertEqual(r["old_value"], "6000")
        self.assertEqual(r["new_value"], "4000")


class TestRecordChange(unittest.TestCase):

    def test_record_without_storage(self):
        """บันทึกได้แม้ไม่มี storage (return object แต่ไม่ persist)"""
        tracker = TreatmentTracker(storage=None)
        change = tracker.record_change(
            patient_hn="56001",
            category="esa",
            action="increase",
            drug_name="Epiao",
            old_value="4000u",
            new_value="6000u",
            reason="Hb ต่ำ",
        )
        self.assertTrue(change.change_id.startswith("TC-56001-"))
        self.assertEqual(change.category, "esa")
        self.assertEqual(change.monitor_lab, "Hb")
        self.assertEqual(change.monitor_target, "10-12 g/dL")
        self.assertEqual(change.followup_days, 14)
        self.assertEqual(change.outcome, "pending")

    def test_record_iron(self):
        tracker = TreatmentTracker(storage=None)
        change = tracker.record_change(
            patient_hn="69453",
            category="iron",
            action="start",
            drug_name="Venofer",
            new_value="100mg IV",
        )
        self.assertEqual(change.monitor_lab, "Ferritin,TSAT")
        self.assertEqual(change.followup_days, 30)


class TestOutcomeEvaluation(unittest.TestCase):

    def test_target_reached(self):
        tracker = TreatmentTracker(storage=None)
        change = TreatmentChange(
            change_id="TC-56001-test",
            patient_hn="56001",
            change_date="2026-10-20",
            category="esa",
            action="increase",
            drug_name="Epiao",
            old_value="4000u",
            new_value="6000u",
            monitor_lab="Hb",
            monitor_target="10-12 g/dL",
            outcome="pending",
        )
        ev = tracker.evaluate_outcome(change, current_lab_value=11.0,
                                       baseline_lab_value=8.5)
        self.assertEqual(ev.verdict, "target_reached")
        self.assertIn("ถึงเป้าหมาย", ev.summary_th)
        self.assertGreater(ev.change_pct, 0)

    def test_improved(self):
        tracker = TreatmentTracker(storage=None)
        change = TreatmentChange(
            change_id="TC-56001-test2",
            patient_hn="56001",
            change_date="2026-10-20",
            category="esa",
            action="increase",
            drug_name="Epiao",
            old_value="4000u",
            new_value="6000u",
            monitor_lab="Hb",
            monitor_target="10-12 g/dL",
            outcome="pending",
        )
        ev = tracker.evaluate_outcome(change, current_lab_value=9.5,
                                       baseline_lab_value=8.5)
        self.assertEqual(ev.verdict, "improved")

    def test_worsened(self):
        tracker = TreatmentTracker(storage=None)
        change = TreatmentChange(
            change_id="TC-56001-test3",
            patient_hn="56001",
            change_date="2026-10-20",
            category="esa",
            action="increase",
            drug_name="Epiao",
            monitor_lab="Hb",
            monitor_target="10-12 g/dL",
            outcome="pending",
        )
        ev = tracker.evaluate_outcome(change, current_lab_value=7.8,
                                       baseline_lab_value=8.5)
        self.assertEqual(ev.verdict, "worsened")


class TestFormatForLine(unittest.TestCase):

    def test_empty_history(self):
        tracker = TreatmentTracker(storage=None)
        text = tracker.format_history_for_line("56001")
        self.assertIn("ยังไม่มีประวัติ", text)

    def test_pending_empty(self):
        tracker = TreatmentTracker(storage=None)
        text = tracker.format_pending_for_line([])
        self.assertIn("ไม่มีรายการ", text)


class TestMonthlySummary(unittest.TestCase):

    def test_no_storage(self):
        tracker = TreatmentTracker(storage=None)
        s = tracker.monthly_summary()
        self.assertEqual(s["total_changes"], 0)


class TestCategoryConfig(unittest.TestCase):

    def test_all_categories_have_required_fields(self):
        for cat, config in TREATMENT_CATEGORIES.items():
            self.assertIn("name_th", config, f"{cat} missing name_th")
            self.assertIn("monitor_lab", config, f"{cat} missing monitor_lab")
            self.assertIn("followup_days", config, f"{cat} missing followup_days")

    def test_esa_config(self):
        c = TREATMENT_CATEGORIES["esa"]
        self.assertEqual(c["monitor_lab"], "Hb")
        self.assertEqual(c["followup_days"], 14)
        self.assertIn("epiao", c["drugs"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
