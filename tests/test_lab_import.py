import unittest
from datetime import date

from hd_module import lab_import as li

TITLE = "ระนอง รักษ์ไต รายงานผลตรวจ Hematocrit วันที่ 2 พฤศจิกายน พ.ศ. 2569 ถึง 2 พฤศจิกายน พ.ศ. 2569"
RAW = [
    [None] * 15,
    [None, TITLE] + [None] * 13,
    [None, None, None, "Hct ≥ 40%", None, None, "3", None, None, "คน", None, "(2.1%)", None, None, None],
    [None] * 15,
    [None, "ลำดับ", "ชื่อ - สกุล", None, "HN", None, None, "Hct(%)", None, None, None, None, "HB(g/dL)", None, "วันที่ตรวจ"],
    [None, "1", "khin Mar Cho พม่า", None, "68443", None, None, "24", None, None, None, None, "8", None, "2/11/2026"],
    [None, "2", "สมชาย ใจดี", None, "12345", None, None, "33", None, None, None, None, "11.0", None, "2/11/2569"],
    [None, "3", "ไม่มีค่า", None, "99999", None, None, None, None, None, None, None, None, None, None],
    [None, "4", "คนไม่มีวันที่", None, "55555", None, None, "28", None, None, None, None, "9.3", None, None],
    [None, "รวม", None, None, None, None, None, None, None, None, None, None, None, None, None],
]


class FakeCur:
    def __init__(self):
        self.inserts = []

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        if sql.lstrip().startswith("INSERT"):
            self.inserts.append(params)

    def fetchall(self):
        if "hd_patient_mapping" in self.sql:
            return [(7, "68443", "KHIN MAR CHO"), (8, "12345", "สมชาย ใจดี"), (9, "55555", "ไม่มีวันที่")]
        if "hd_lab_results" in self.sql:
            return [(8, date(2026, 11, 2), 11.0, 33.0, None, None, None)]
        return []


class ParseTests(unittest.TestCase):
    def test_clinic_hct_report(self):
        res = li.parse_rows(RAW)
        self.assertEqual(res["default_date"], "2026-11-02")
        self.assertEqual(sorted(res["fields"]), ["hematocrit", "hemoglobin"])
        hns = [r["hn"] for r in res["rows"]]
        self.assertEqual(hns, ["68443", "12345", "55555"])   # empty row + 'รวม' skipped
        r0 = res["rows"][0]
        self.assertEqual((r0["hemoglobin"], r0["hematocrit"], r0["lab_date"]), (8.0, 24.0, "2026-11-02"))
        self.assertEqual(res["rows"][1]["lab_date"], "2026-11-02")   # พ.ศ. 2569 → 2026
        self.assertEqual(res["rows"][2]["lab_date"], "2026-11-02")   # missing date → title date
        self.assertEqual(res["errors"], [])

    def test_no_header(self):
        res = li.parse_rows([["a", "b"], ["1", "2"]])
        self.assertEqual(res["rows"], [])
        self.assertTrue(res["errors"])

    def test_generic_header_and_dates(self):
        raw = [["HN", "Hb", "Ferritin", "TSAT", "Date"], ["A1", "9.5", "150", "18", date(2026, 10, 1)], ["A2", "-", "", "", "01-10-2026"]]
        res = li.parse_rows(raw)
        self.assertEqual(len(res["rows"]), 1)
        self.assertEqual(res["rows"][0]["ferritin"], 150.0)
        self.assertEqual(res["rows"][0]["lab_date"], "2026-10-01")
        self.assertEqual(li.parse_date("15/01/2569"), date(2026, 1, 15))
        self.assertIsNone(li.parse_date("junk"))


class MatchInsertTests(unittest.TestCase):
    def test_match_duplicates_and_insert(self):
        rows = li.parse_rows(RAW)["rows"]
        cur = FakeCur()
        matched, unmatched = li.match_patients(cur, rows)
        self.assertEqual([m["hn"] for m in matched], ["68443", "12345", "55555"])
        self.assertEqual(unmatched, [])
        self.assertFalse(matched[0]["duplicate"])
        self.assertTrue(matched[1]["duplicate"])     # same date + same Hb/Hct already stored
        self.assertEqual(matched[0]["registered_name"], "KHIN MAR CHO")
        res = li.insert_rows(cur, matched, "import:hct.xlsx")
        self.assertEqual(res, {"inserted": 2, "skipped": 1})
        self.assertEqual(cur.inserts[0][0], 7)
        self.assertIn("hemoglobin", cur.sql)
        self.assertEqual(cur.inserts[-1][-1], "import:hct.xlsx")

    def test_summary(self):
        s = li.summary([{"hemoglobin": 7.9}, {"hemoglobin": 9.0}, {"hemoglobin": 11.0}, {"hemoglobin": 12.5}, {}])
        self.assertEqual((s["n"], s["lt8"], s["lt10"], s["in_range"], s["gt12"]), (4, 1, 2, 1, 1))


if __name__ == "__main__":
    unittest.main()
