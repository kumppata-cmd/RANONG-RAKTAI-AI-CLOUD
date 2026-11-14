"""Serology (HBsAg / Anti-HCV) — normalisation, merge of two sources, import matching."""
import unittest
from datetime import date

from hd_module.serology import apply, latest_for, normalize, parse_import, text


class NormalizeTests(unittest.TestCase):
    def test_values(self):
        for v in ("Positive", "POS", "reactive", "+", "บวก"):
            self.assertEqual(normalize(v), "positive", v)
        for v in ("Negative", "neg", "non-reactive", "-", "ลบ"):
            self.assertEqual(normalize(v), "negative", v)
        self.assertIsNone(normalize(""))
        self.assertIsNone(normalize("pending"))


class FakeCur:
    def __init__(self, results=(), docs=(), patients=()):
        self.results, self.docs, self.patients = list(results), list(docs), list(patients)
        self.inserts, self.sql = [], ""

    def execute(self, sql, params=None):
        self.sql = sql
        if sql.lstrip().startswith("INSERT INTO hd_lab_results"):
            self.inserts.append(params)

    def fetchall(self):
        if "FROM hd_lab_results" in self.sql:
            return self.results
        if "FROM hd_lab_docs" in self.sql:
            return self.docs
        if "FROM hd_patient_mapping" in self.sql:
            return self.patients
        return []


class LatestForTests(unittest.TestCase):
    def test_merges_both_sources_newest_wins(self):
        cur = FakeCur(results=[(1, "positive", None, date(2026, 1, 5)), (2, "negative", "negative", date(2026, 3, 1))],
                      docs=[(1, "negative", "negative", date(2025, 6, 1)), (2, None, "positive", date(2026, 9, 1))])
        out = latest_for(cur, [1, 2, 3])
        self.assertEqual(out[1]["hbsag"], "positive")           # 2026-01 beats 2025-06
        self.assertEqual(out[1]["antihcv"], "negative")         # only source for HCV
        self.assertEqual(out[2]["antihcv"], "positive")         # doc 2026-09 beats result 2026-03
        self.assertTrue(out[2]["any_positive"])
        self.assertNotIn(3, out)
        self.assertEqual(text(out[2]), "Anti-HCV+ · HBsAg−")

    def test_empty_ids(self):
        self.assertEqual(latest_for(FakeCur(), []), {})


CSV = ("HN,ชื่อ-สกุล,HBsAg,Anti-HCV,วันที่\n"
       "56034,นาย รัตน์ระวี จันทร์เดชะ,Positive,Negative,14/11/2569\n"
       "68445,นาง KHIN NAUNG,Negative,Positive,14/11/2569\n"
       "56001,นาย ปิยะ อะนะบุหรง,Negative,,14/11/2569\n"
       "99999,นาย คนละคน,Negative,,14/11/2569\n"
       "57052,นาย สมชาย ผิดคน,Positive,,14/11/2569\n").encode("utf-8")


class ImportTests(unittest.TestCase):
    def test_parse(self):
        rows, errors = parse_import("sero.csv", CSV)
        self.assertEqual(len(rows), 5)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0], {"row": 2, "hn": "56034", "name": "นาย รัตน์ระวี จันทร์เดชะ", "hbsag": "positive",
                                   "antihcv": "negative", "date": "2026-11-14"})
        self.assertIsNone(rows[2]["antihcv"])

    def test_apply_matches_hn_and_name(self):
        rows, _ = parse_import("sero.csv", CSV)
        cur = FakeCur(patients=[(1, "56034", "นาย รัตน์ระวี จันทร์เดชะ"), (2, "68445", "นาง KHIN NAUNG"),
                                (3, "56001", "นาย ปิยะ อะนะบุหรง"), (4, "57052", "นาย อนุวัตร ปรีดาสุทธ์")],
                      results=[(3, "negative", None, date(2026, 1, 1))])
        res = apply(cur, rows, confirm=True)
        self.assertEqual([m["hn"] for m in res["matched"]], ["56034", "68445", "56001"])
        self.assertEqual([u["hn"] for u in res["unmatched"]], ["99999"])
        self.assertEqual([u["hn"] for u in res["name_mismatch"]], ["57052"])     # wrong name → never written
        self.assertEqual(res["pos_hbsag"], 1)
        self.assertEqual(res["pos_antihcv"], 1)
        self.assertEqual(res["written"], 2)                                      # 56001 already negative → skipped
        self.assertEqual(res["skipped_same"], 1)
        self.assertEqual(cur.inserts[0][2:4], ("positive", "negative"))
        self.assertIn("import:serology", cur.inserts[0][4])

    def test_preview_writes_nothing(self):
        rows, _ = parse_import("sero.csv", CSV)
        cur = FakeCur(patients=[(1, "56034", "นาย รัตน์ระวี จันทร์เดชะ")])
        res = apply(cur, rows, confirm=False)
        self.assertEqual(res["to_write"], 1)
        self.assertEqual(cur.inserts, [])


if __name__ == "__main__":
    unittest.main()
