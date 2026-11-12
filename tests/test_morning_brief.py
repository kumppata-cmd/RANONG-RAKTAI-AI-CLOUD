"""Morning Brief (B7): message content from collected data; query failures never fatal."""
import unittest
from datetime import date

from hd_module import morning_brief as mb


class _Cur:
    """Cursor that fails the dialyzer query to prove savepoint recovery."""
    def __init__(self):
        self.rows = []; self.calls = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.calls.append(sql.strip()[:30])
        if "hd_dialyzer_tracking" in sql:
            raise RuntimeError("relation does not exist")
        if "COUNT(*) FROM hd_patient_mapping" in sql:
            self.rows = [(59,)]
        elif "GROUP BY 1" in sql:
            self.rows = [("morning", 20), ("afternoon", 18)]
        elif "hd_clinical_alerts a" in sql:
            self.rows = [("critical", "ความดันต่ำวิกฤต — BP 80/40 mmHg", 3, "นางจารึก", "67398", None, True),
                         ("warning", "ความดันตก", 5, "นายก", "1", "ลด UF", True)]
        elif "hb_value" in sql:
            self.rows = [("นายข", "2", 8.4, date(2026, 11, 10)), ("นายค", "3", 11.2, date(2026, 11, 10))]
        elif "hd_water_quality" in sql:
            self.rows = [(date(2026, 9, 1),)]
        elif "hd_clinic_profile" in sql:
            self.rows = [("license", "ใบอนุญาตสถานพยาบาล", date(2026, 12, 15))]
        elif "hd_incidents" in sql:
            self.rows = [(1,)]
        else:
            self.rows = []
    def fetchall(self): return self.rows


class _Conn:
    def cursor(self): return _Cur()


class MorningBriefTests(unittest.TestCase):
    def test_collect_and_message(self):
        data = mb.collect(_Conn(), date(2026, 11, 12))
        self.assertEqual(data["today_patients"], 59)
        self.assertEqual(data["dialyzer"], [])                 # failed query → skipped, not fatal
        self.assertEqual(len(data["no_order"]), 1)            # critical without order
        self.assertEqual([r[1] for r in data["low_hb"]], ["2"])
        self.assertEqual(data["water_overdue"][1], 72)
        msg = mb.build_message(data, "https://x.test")
        for part in ["Morning Brief", "12 พ.ย. 2569", "ฟอกวันนี้ 59 คน", "เช้า 20", "Alert เมื่อวาน 2",
                     "วิกฤต 1", "เตียง 3 นางจารึก", "ยังไม่มีคำสั่งแพทย์", "Hb < 9 (1 คน)", "Hb 8.4",
                     "ผลตรวจน้ำเกินกำหนด", "72 วัน", "ใบอนุญาตสถานพยาบาล", "อุบัติการณ์ยังไม่ปิด 1",
                     "https://x.test/hd/dashboard", "date=2026-11-11"]:
            self.assertIn(part, msg)
        self.assertNotIn("Dialyzer", msg)

    def test_send_counts_false_as_not_sent(self):
        sent, msg = mb.send_morning_brief(_Conn(), lambda u, m: u != "bad", ["ok1", "bad", "ok2"], "", date(2026, 11, 12))
        self.assertEqual(sent, 2)


if __name__ == "__main__":
    unittest.main()
