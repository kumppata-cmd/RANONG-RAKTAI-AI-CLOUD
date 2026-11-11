"""Tests for hd_module.registry_import — annual roster (Excel) import."""
import io
import unittest

import openpyxl

from hd_module.registry_import import (
    apply_roster, normalize_bfr, normalize_esa, normalize_va, parse_roster,
)


def _xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None] * 12)
    ws.append([None, "รายชื่อผู้ป่วยประจำปี 2569"])
    ws.append(["ลำดับ", "ชื่อ-สกุล", None, "HN รพ", " HN clinic", "ID", "ยาฉีด",
               "รอบการฟอก", "ที่อยู่", "จำนวน HD/wk", "Vascular Access", "BFR/min"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class FakeCursor:
    def __init__(self, patients):
        self.patients = patients
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.last_sql = sql
        if sql.strip().startswith("UPDATE"):
            self.executed.append(params)

    def fetchall(self):
        return self.patients


class FakeConn:
    def __init__(self, patients):
        self.cur = FakeCursor(patients)
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
        # legend rows at the bottom of the sheet must not become drugs
        self.assertEqual(normalize_esa("Eposis 3000………… Eposis 10000……"), ("", ""))

    def test_bfr(self):
        self.assertEqual(normalize_bfr("300"), 300)
        self.assertEqual(normalize_bfr("250-300"), 250)
        self.assertIsNone(normalize_bfr(""))


class ParseTests(unittest.TestCase):
    def test_parse_skips_legend_and_never_keeps_national_id(self):
        data = _xlsx([
            [1, "นาย", "ปิยะ ทดสอบ", "00007567", "56001", 3859900063616, "E4", "จ/พ/ศ", None, 3, "AVF", 350],
            [2, "นาง", "มณี ทดสอบ", "00007562", 57051, 3859900163629, "hypercrit", "จ/พ/ศ", None, 3, "PC", "300-350"],
            [None, "Hemax 4000……………..", None, None, None, None, None, None, None, None, None, None],
        ])
        rows, skipped = parse_roster(data)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(rows[0]["hn_clinic"], "56001")
        self.assertEqual(rows[0]["hn_hospital"], "7567")
        self.assertEqual(rows[1]["vascular_access"], "Perm Cath")
        self.assertEqual(rows[1]["bfr"], 300)
        self.assertNotIn("3859900063616", repr(rows))
        self.assertNotIn("3859900163629", repr(rows))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        # id, hn, hospital_hn, name, va, bfr, hd_per_week, schedule_days, esa_drug
        self.db = [
            (1, "56001", "00007567", "ปิยะ", "", 0, 3, "", ""),
            (2, "57051", "7562", "มณี", "AVF", 300, 3, "จ/พ/ศ", "Hypercrit"),
            (3, "99999", None, "คนอื่น", "AVG", 250, 2, "อ/ส", ""),
        ]
        self.rows, _ = parse_roster(_xlsx([
            [1, "นาย", "ปิยะ ทดสอบ", "00007567", "56001", 1, "E4", "จ/พ/ศ", None, 3, "AVF", 350],
            [2, "นาง", "มณี ทดสอบ", "00007562", 57051, 2, "hypercrit", "จ/พ/ศ", None, 3, "AVF", 300],
            [3, "นาย", "ไม่มี ในระบบ", "00000001", 11111, 3, "H4", "อ/พฤ/ส", None, 3, "DLC", 250],
            [4, "นาย", "จับคู่ ด้วยHNรพ", "00000003", None, 4, "HM", "ส", None, 1, "PC", 200],
        ]))

    def test_dry_run_matches_but_writes_nothing(self):
        conn = FakeConn(self.db)
        s = apply_roster(conn, self.rows, confirm=False)
        self.assertEqual(s["total_rows"], 4)
        self.assertEqual(s["matched"], 2)           # 56001 by clinic HN, 57051 by clinic HN
        self.assertEqual(len(s["unmatched"]), 2)    # 11111 and the hospital-HN-only row (no hhn '3' in db)
        self.assertEqual(s["to_update"], 1)         # only ปิยะ changes; มณี already identical
        self.assertEqual(s["unchanged"], 1)
        self.assertEqual(s["written"], 0)
        self.assertFalse(conn.committed)
        self.assertEqual(conn.cur.executed, [])
        self.assertEqual(s["details"][0]["changes"]["vascular_access"], ["", "AVF"])
        self.assertEqual(s["details"][0]["changes"]["bfr"], [0, 350])
        self.assertNotIn("_id", s["details"][0])

    def test_confirm_writes_only_changed_rows(self):
        conn = FakeConn(self.db)
        s = apply_roster(conn, self.rows, confirm=True)
        self.assertEqual(s["written"], 1)
        self.assertTrue(conn.committed)
        params = conn.cur.executed[0]
        self.assertEqual(params["id"], 1)
        self.assertEqual(params["vascular_access"], "AVF")
        self.assertEqual(params["esa_drug"], "Epiao")
        self.assertEqual(params["esa_dose"], "4000 IU")

    def test_hospital_hn_fallback_ignores_zero_padding(self):
        db = [(9, "C-1", "00000003", "ใครสักคน", "", 0, 3, "", "")]
        conn = FakeConn(db)
        s = apply_roster(conn, self.rows, confirm=False)
        hit = [d for d in s["details"] if d["hn"] == "C-1"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["matched_by"], "HN รพ")
        self.assertEqual(hit[0]["changes"]["vascular_access"], ["", "Perm Cath"])


if __name__ == "__main__":
    unittest.main()
