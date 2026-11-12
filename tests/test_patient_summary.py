"""Post-HD summary to the patient (ว้าว #7) — content rules, Flex validity, schedule."""
import json
import unittest
from datetime import date

from hd_module.patient_summary import build_flex, build_text, gentle_notes, next_schedule_day

S = {"patient_name": "นาย ก", "patient_hn": "100", "pre_weight": 57.6, "post_weight": 55.3, "dry_weight": 55.0,
     "actual_uf": 2300, "duration_hours": 4, "esa_given": True, "esa_drug_given": "Eprex", "esa_drug": "Eprex",
     "schedule_days": "จ,พ,ศ", "bp_pre": "150/90", "bp_post": "128/78",
     "alerts": [{"severity": "critical", "title": "SBP < 90", "message": "SBP 84"}], "issues": ["🔴 มี Critical Alert"],
     "assessment": "⚠️ ฟอกเสร็จ — มีข้อสังเกต", "notes": "ให้ NSS 200 mL"}
H = [{"d": f"2026-10-{d:02d}", "pre": w, "post": w - 2} for d, w in ((1, 57.0), (3, 57.4), (6, 58.2), (8, 57.1))]


class ContentTests(unittest.TestCase):
    def test_text_has_numbers_not_clinical_details(self):
        t = build_text(S, H, today=date(2026, 11, 12))
        for must in ("57.6 → 55.3", "2.3 ลิตร", "150/90 → 128/78", "Eprex", "นัดครั้งถัดไป วันศุกร์ที่ 13 พ.ย. 2569"):
            self.assertIn(must, t)
        for never in ("SBP < 90", "Critical", "NSS", "ข้อสังเกต", "HN"):
            self.assertNotIn(never, t)

    def test_gentle_notes(self):
        n = gentle_notes(S)
        self.assertTrue(any("จำกัดน้ำ" in x for x in n))
        self.assertTrue(any("ไม่ได้ฉีด" in x for x in gentle_notes(dict(S, esa_given=False, pre_weight=55.5))))
        self.assertEqual(len(gentle_notes({"pre_weight": 55.5, "post_weight": 55.0, "dry_weight": 55.0})), 1)

    def test_flex_structure(self):
        f = build_flex(S, H, today=date(2026, 11, 12))
        self.assertEqual(f["type"], "flex")
        self.assertLessEqual(len(f["altText"]), 400)
        b = f["contents"]
        self.assertEqual(b["type"], "bubble")
        js = json.dumps(b, ensure_ascii=False)
        self.assertIn("น้ำหนักก่อนฟอก 8 ครั้งล่าสุด", js)     # chart present with 4 points
        self.assertNotIn("SBP < 90", js)
        self.assertNotIn("http", js)                            # no image / public URL

    def test_flex_without_history(self):
        js = json.dumps(build_flex(dict(S, post_weight=None), [], today=date(2026, 11, 12)), ensure_ascii=False)
        self.assertNotIn("8 ครั้งล่าสุด", js)
        self.assertNotIn("57.6 →", js)


class ScheduleTests(unittest.TestCase):
    def test_next_day(self):
        self.assertEqual(next_schedule_day("จ,พ,ศ", date(2026, 11, 12)), date(2026, 11, 13))   # Thu → Fri
        self.assertEqual(next_schedule_day("อ พฤ ส", date(2026, 11, 12)), date(2026, 11, 14))  # Thu → Sat
        self.assertIsNone(next_schedule_day("", date(2026, 11, 12)))


if __name__ == "__main__":
    unittest.main()
