"""Daily record PDFs (session + shift) render Thai text and the folder standard."""
import unittest
from datetime import date, datetime, timedelta

from hd_module import daily_records as dr


def _rec():
    start = datetime(2026, 11, 11, 17, 12)
    return {
        "session_id": 57, "date": date(2026, 11, 12), "shift": "morning", "bed": 3, "status": "completed",
        "started_at": start, "ended_at": start + timedelta(hours=4),
        "pre_weight": 66.5, "post_weight": 64.6, "uf_goal": 2000, "actual_uf": 1900,
        "esa_given": True, "esa_drug": "Epiao", "esa_dose": "4000 IU", "esa_route": "IV", "esa_skip": None,
        "hb": 9.8, "hct": 30, "notes": "เวียนศีรษะ ลด UF", "pre_hd_note": None,
        "hn": "67398", "name": "นางจารึก แก้วมีศรี", "va": "AVF", "dry_weight": 64.5,
        "esa_rx": "Epiao 4000 IU", "schedule": "จ/พ/ศ",
        "vitals": [("pre", 134, 84, 76, 36.8, 98, start), ("intra", 80, 40, 60, None, None, start + timedelta(minutes=12))],
        "alerts": [("critical", "ความดันต่ำวิกฤต — BP 80/40 mmHg", "SBP 80 < 90", start, True,
                    "ลด UF", "dr.k", start + timedelta(minutes=20))],
        "sheet": [{"slot_time": "00:00", "sbp": 134, "dbp": 84, "pr": 76, "rr": 18, "temp": 36.8, "spo2": 98,
                   "bfr": 250, "vp": 120, "tmp": 40, "ap": -100, "conductivity": 14.0, "dialysate_temp": 36.5,
                   "ufr": 500, "total_uf": 0, "remark": "", "synced": True}],
    }


class DailyRecordsTests(unittest.TestCase):
    def test_fonts_register(self):
        self.assertTrue(dr.register_fonts())

    def test_session_pdf_two_pages(self):
        pdf = dr.session_pdf(_rec())
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page "), 2)   # summary + sheet
        self.assertIn(b"Loma", pdf)                                                       # Thai font embedded

    def test_shift_pdf(self):
        rows = [{"session_id": 1, "shift": "morning", "bed": 3, "status": "completed", "hn": "1", "patient_name": "ก",
                 "vascular_access": "AVF", "started_at": "2026-11-11T17:00:00Z", "ended_at": "2026-11-11T21:00:00Z",
                 "pre_weight": 60, "post_weight": 58, "uf_goal": 2000, "actual_uf": 2000, "esa_given": False,
                 "pre_bp": "120/80", "post_bp": "110/70", "min_sbp": 100, "hb": None, "vital_count": 3,
                 "alert_count": 0, "critical_count": 0}]
        self.assertTrue(dr.shift_pdf(date(2026, 11, 12), "morning", rows).startswith(b"%PDF"))

    def test_folder_and_filename_standard(self):
        self.assertEqual(dr.record_folder(date(2026, 11, 12)), "HD-Records/2026/2026-11/2026-11-12")
        self.assertEqual(dr.session_filename("67398", "นางจารึก แก้วมีศรี", 3, "morning"),
                         "67398-นางจารึก-แก้วมีศรี-เตียง3-รอบเช้า.pdf")
        self.assertNotIn("/", dr.safe_name("a/b\\c:d"))

    def test_export_day_manifest(self):
        sent = []
        def upload(path, data, ctype):
            if path.endswith("-บ่าย.pdf"):
                raise RuntimeError("403")
            sent.append(path)
        rows = [{"session_id": 1, "shift": "afternoon", "bed": 1, "status": "scheduled", "hn": "1", "patient_name": "x",
                 "vascular_access": None, "started_at": None, "ended_at": None, "pre_weight": None, "post_weight": None,
                 "uf_goal": None, "actual_uf": None, "esa_given": None, "pre_bp": None, "post_bp": None, "min_sbp": None,
                 "hb": None, "vital_count": 0, "alert_count": 0, "critical_count": 0}]
        m = dr.export_day(None, date(2026, 11, 12), rows, b"csv", upload)
        self.assertEqual(m["folder"], "HD-Records/2026/2026-11/2026-11-12")
        self.assertEqual(m["ok"], 1)       # csv
        self.assertEqual(m["failed"], 1)   # afternoon pdf upload refused
        self.assertEqual(sent, ["HD-Records/2026/2026-11/2026-11-12/HD-2026-11-12.csv"])

    def test_thai_time(self):
        self.assertEqual(dr.thai_time(datetime(2026, 11, 11, 17, 12)), "00:12")
        self.assertEqual(dr.thai_time("2026-11-11T17:12:00Z"), "00:12")
        self.assertEqual(dr.thai_time(None), "-")


if __name__ == "__main__":
    unittest.main()
