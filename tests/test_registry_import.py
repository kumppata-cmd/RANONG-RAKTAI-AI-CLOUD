"""Tests for hd_module.registry_import — annual roster (Excel) import (2568 + 2569 layouts)."""
import io
import unittest
from datetime import date

import openpyxl

from hd_module.registry_import import (
    apply_roster, lab_header, normalize_bfr, normalize_esa, normalize_schedule, normalize_shift,
    normalize_va, parse_regimen, parse_roster,
)

HDR_2568 = ["ลำดับ", "ชื่อ-สกุล", None, "HN รพ", " HN clinic", "ID", "ยาฉีด",
            "รอบการฟอก", "ที่อยู่", "จำนวน HD/wk", "Vascular Access", "BFR/min"]
HDR_2569 = ["ลำดับ", "ชื่อ-สกุล", None, "HN รพ", " HN clinic", "ID", "ยาฉีด", "Hematocrit", "จำนวนการฉีด",
            "วันฟอก", "รอบการฟอก", "จำนวน HD/wk", "Vascular Access", "BFR/min",
            "iPTH ม.ค.69", "iPTH ก.ค.69", "Ferritin ม.ค.69", "Ferritin ก.ค.69"]


def _xlsx(rows, header=HDR_2568):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None] * 12)
    ws.append([None, "รายชื่อผู้ป่วยประจำปี 2569"])
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class FakeCursor:
    """Answers the four read queries apply_roster makes; records writes."""
    def __init__(self, patients, rx=None, labs=None):
        self.patients, self.rx, self.labs = patients, rx or [], labs or []
        self.executed, self.rx_updates, self.rx_inserts, self.lab_inserts = [], [], [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.last_sql = sql
        s = sql.strip()
        if s.startswith("UPDATE hd_patient_mapping"):
            self.executed.append(params)
        elif s.startswith("UPDATE hd_prescriptions"):
            self.rx_updates.append(params)
        elif s.startswith("INSERT INTO hd_prescriptions"):
            self.rx_inserts.append(params)
        elif s.startswith("INSERT INTO hd_lab_results"):
            self.lab_inserts.append((sql, params))

    def fetchall(self):
        s = self.last_sql
        if "FROM hd_prescriptions" in s:
            return self.rx
        if "FROM hd_lab_results" in s:
            return self.labs
        if "UPPER(hn) = ANY" in s:   # lab_import.match_patients
            return [(p[0], str(p[1]).upper(), p[3]) for p in self.patients]
        return self.patients


class FakeConn:
    def __init__(self, patients, rx=None, labs=None):
        self.cur = FakeCursor(patients, rx, labs)
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True


class NormalizeTests(unittest.TestCase):
    def test_va(self):
        self.assertEqual(normalize_va("PC"), "Perm Cath")
        self.assertEqual(normalize_va("PC+AVF"), "Perm Cath+AVF")
        self.assertEqual(normalize_va(" avf "), "AVF")
        self.assertEqual(normalize_va(None), "")

    def test_esa(self):
        self.assertEqual(normalize_esa("E4"), ("Epiao", "4000 IU"))
        self.assertEqual(normalize_esa("H4+H10"), ("Hema-plus", "4000+10000 IU"))
        self.assertEqual(normalize_esa("HM"), ("Hemax", ""))
        self.assertEqual(normalize_esa("hypercrit"), ("Hypercrit", ""))
        self.assertEqual(normalize_esa("Eposis 3000………… Eposis 10000……"), ("", ""))

    def test_regimen(self):
        r = parse_regimen("8000ū 2time/wk")
        self.assertEqual((r["weekly_iu"], r["per_week"], r["dose_iu"]), (8000, 2.0, 4000))
        self.assertEqual(r["text"], "4000 IU x2/สัปดาห์")
        self.assertEqual(parse_regimen("4000ū 1time/wk")["dose_iu"], 4000)
        q = parse_regimen("10000ū q 2 wk")
        self.assertEqual((q["weekly_iu"], q["per_week"], q["dose_iu"]), (5000, 0.5, 10000))
        self.assertTrue(parse_regimen("hold")["hold"])
        self.assertIsNone(parse_regimen(""))

    def test_bfr_shift_schedule(self):
        self.assertEqual(normalize_bfr("300"), 300)
        self.assertEqual(normalize_bfr("250-300"), 300)      # prescribed target = upper bound
        self.assertIsNone(normalize_bfr("300150"))           # typo in the sheet → not imported
        self.assertIsNone(normalize_bfr(""))
        self.assertEqual(normalize_shift("รอบเช้า"), "morning")
        self.assertEqual(normalize_shift("รอบบ่าย/รอบเช้า"), "afternoon")
        self.assertEqual(normalize_shift("รอบ2"), "afternoon")
        self.assertEqual(normalize_shift("รอบค่ำ"), "evening")
        self.assertEqual(normalize_shift(""), "")
        self.assertEqual(normalize_schedule("จ/พ/ศ"), "จ,พ,ศ")

    def test_lab_header(self):
        self.assertEqual(lab_header("iPTH ม.ค.69"), ("ipth", date(2026, 1, 1)))
        self.assertEqual(lab_header("Ferritin ก.ค.69"), ("ferritin", date(2026, 7, 1)))
        self.assertEqual(lab_header("Hb กค 2569"), ("hemoglobin", date(2026, 7, 1)))
        self.assertIsNone(lab_header("Vascular Access"))
        self.assertIsNone(lab_header("BFR/min"))


class ParseTests(unittest.TestCase):
    def test_legacy_2568_layout(self):
        data = _xlsx([
            [1, "นาย", "ปิยะ ทดสอบ", "00007567", "56001", 3859900063616, "E4", "จ/พ/ศ", None, 3, "AVF", 350],
            [2, "นาง", "มณี ทดสอบ", "00007562", 57051, 3859900163629, "hypercrit", "จ/พ/ศ", None, 3, "PC", "300-350"],
            [None, "Hemax 4000……………..", None, None, None, None, None, None, None, None, None, None],
        ])
        rows, skipped = parse_roster(data)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(rows[0]["name"], "นาย ปิยะ ทดสอบ")
        self.assertEqual(rows[0]["hn_clinic"], "56001")
        self.assertEqual(rows[0]["hn_hospital"], "7567")
        self.assertEqual(rows[0]["schedule_days"], "จ,พ,ศ")   # "รอบการฟอก" held days in 2568
        self.assertEqual(rows[0]["shift"], "")
        self.assertEqual(rows[1]["vascular_access"], "Perm Cath")
        self.assertEqual(rows[1]["bfr"], 350)
        self.assertNotIn("3859900063616", repr(rows))
        self.assertNotIn("3859900163629", repr(rows))

    def test_2569_layout_with_labs(self):
        data = _xlsx([
            [1, "นาย", "ปิยะ ทดสอบ", "00007567", "56001", 3859900063616, "E4", 31, "8000ū 2time/wk", "จ/พ/ศ", "รอบเช้า", 3, "AVF", 350, 2085, 18.4, 1344, 796.8],
            [2, "นาง", "มณี ทดสอบ", "00007562", 57051, 3859900163629, "hypercrit", 29, "hold", "พ/ส", None, 2, "PC", "250-300", None, 43.5, None, 262.4],
        ], HDR_2569)
        rows, skipped = parse_roster(data)
        self.assertEqual(len(rows), 2)
        r = rows[0]
        self.assertEqual(r["name"], "นาย ปิยะ ทดสอบ")
        self.assertEqual((r["esa_drug"], r["esa_dose"], r["esa_per_week"]), ("Epiao", "4000 IU x2/สัปดาห์", 2.0))
        self.assertEqual((r["schedule_days"], r["shift"], r["hd_per_week"], r["bfr"], r["hct"]), ("จ,พ,ศ", "morning", 3, 350, 31.0))
        self.assertEqual(len(r["labs"]), 4)
        self.assertIn({"field": "ipth", "date": "2026-07-01", "value": 18.4}, r["labs"])
        self.assertEqual(rows[1]["esa_dose"], "hold")
        self.assertEqual(rows[1]["shift"], "")
        self.assertEqual(len(rows[1]["labs"]), 2)
        self.assertNotIn("3859900063616", repr(rows))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        # id, hn, hospital_hn, name, va, bfr, hd_per_week, schedule_days, esa_drug, esa_dose, esa_per_week, default_shift
        self.db = [
            (1, "56001", "00007567", "ปิยะ", "", 0, 3, "", "", "", None, None),
            (2, "57051", "7562", "มณี", "AVF", 300, 3, "จ,พ,ศ", "Hypercrit", "", None, None),
            (3, "99999", None, "คนอื่น", "AVG", 250, 2, "อ,ส", "", "", None, None),
        ]
        self.rows, _ = parse_roster(_xlsx([
            [1, "นาย", "ปิยะ ทดสอบ", "00007567", "56001", 1, "E4", "จ/พ/ศ", None, 3, "AVF", 350],
            [2, "นาง", "มณี ทดสอบ", "00007562", 57051, 2, "hypercrit", "จ/พ/ศ", None, 3, "AVF", 300],
            [3, "นาย", "ไม่มี ในระบบ", "00000001", 11111, 3, "H4", "อ/พฤ/ส", None, 3, "DLC", 250],
            [4, "นาย", "จับคู่ ด้วยHNรพ", "00000003", None, 4, "HM", "ส", None, 1, "PC", 200],
        ]))

    def test_dry_run_matches_but_writes_nothing(self):
        conn = FakeConn(self.db, rx=[(2, 20, 300, 3, "AVF")])
        s = apply_roster(conn, self.rows, confirm=False)
        self.assertEqual(s["total_rows"], 4)
        self.assertEqual(s["matched"], 2)
        self.assertEqual(len(s["unmatched"]), 2)
        self.assertEqual(s["to_update"], 1)         # only ปิยะ changes (mapping + new prescription); มณี identical incl. rx
        self.assertEqual(s["unchanged"], 1)
        self.assertEqual((s["written"], s["rx_written"], s["rx_created"], s["labs_written"]), (0, 0, 0, 0))
        self.assertEqual(s["rx_to_create"], 1)
        self.assertFalse(conn.committed)
        self.assertEqual(conn.cur.executed, [])
        d0 = s["details"][0]
        self.assertEqual(d0["changes"]["vascular_access"], ["", "AVF"])
        self.assertEqual(d0["changes"]["bfr"], [0, 350])
        self.assertEqual(d0["changes"]["prescription_new"]["blood_flow_rate"], [None, 350])
        self.assertNotIn("_id", d0)

    def test_confirm_writes_mapping_and_creates_prescription(self):
        conn = FakeConn(self.db, rx=[(2, 20, 300, 3, "AVF")])
        s = apply_roster(conn, self.rows, confirm=True)
        self.assertEqual((s["written"], s["rx_created"], s["rx_written"]), (1, 1, 0))
        self.assertTrue(conn.committed)
        params = conn.cur.executed[0]
        self.assertEqual((params["id"], params["vascular_access"], params["esa_drug"], params["esa_dose"]), (1, "AVF", "Epiao", "4000 IU"))
        self.assertEqual(conn.cur.rx_inserts[0][:4], (1, 3, 350, "AVF"))

    def test_2569_confirm_writes_labs_and_skips_duplicates(self):
        rows, _ = parse_roster(_xlsx([
            [1, "นาย", "ปิยะ ทดสอบ", "00007567", "56001", 1, "E4", 31, "8000ū 2time/wk", "จ/พ/ศ", "รอบเช้า", 3, "AVF", 350, 2085, 18.4, 1344, 796.8],
        ], HDR_2569))
        # Hct 31 on 2026-11-02 already stored → that lab row is a duplicate; the 2 iPTH/Ferritin months are new
        conn = FakeConn(self.db, labs=[(1, date(2026, 11, 2), None, 31.0, None, None, None)])
        s = apply_roster(conn, rows, confirm=True, hct_date=date(2026, 11, 2), source="import:test.xlsx")
        self.assertEqual(s["labs_total"], 3)
        self.assertEqual(s["labs_new"], 2)
        self.assertEqual(s["labs_written"], 2)
        sqls = " ".join(sql for sql, _ in conn.cur.lab_inserts)
        self.assertIn("ipth", sqls)
        self.assertIn("ferritin", sqls)
        self.assertIn("notes", sqls)
        params = conn.cur.executed[0]
        self.assertEqual((params["esa_per_week"], params["shift"], params["schedule_days"]), (2.0, "morning", "จ,พ,ศ"))

    def test_hospital_hn_fallback_ignores_zero_padding(self):
        db = [(9, "C-1", "00000003", "ใครสักคน", "", 0, 3, "", "", "", None, None)]
        conn = FakeConn(db)
        s = apply_roster(conn, self.rows, confirm=False)
        hit = [d for d in s["details"] if d["hn"] == "C-1"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["matched_by"], "HN รพ")
        self.assertEqual(hit[0]["changes"]["vascular_access"], ["", "Perm Cath"])


if __name__ == "__main__":
    unittest.main()
