import unittest
from datetime import date

from hd_module import fluid_status as fs

BCM_TEXT = """Fluid Management Tool 4.1
Measurement Report 16/9/2026 12:15
Comments:
sarifa klasuek
ID: ID88lf
Gender: F
Date of birth: 1/1/1956
Groups:
Filter: All From: 23/1/2026 To: 16/9/2026
Body Composition
Height: 150 cm
Weight: 69.0 kg
BMI: 30.7 kg/m
2
LTI: 6.8 kg/m
2
FTI: 23.7 kg/m
2
Fluid Status
OH pre: -0.3 L
(%ECW): -2.3 %
OH post: L
(%ECW): %
UFV: ml
V urea: 22.6126 l
Blood pressure
Pre BP: 132/86 mmHg
BP post: / mmHg
"""


class FakeCur:
    def __init__(self, patients, devmap=None, existing=None):
        self.patients, self.devmap, self.existing = patients, devmap or [], existing or []
        self.inserts, self.maps = [], []

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params
        if sql.lstrip().startswith("INSERT INTO hd_fluid_assessments"):
            self.inserts.append(params)
        elif sql.lstrip().startswith("INSERT INTO hd_fluid_device_map"):
            self.maps.append(params)

    def fetchall(self):
        if "hd_fluid_device_map" in self.sql:
            return self.devmap
        if "FROM hd_fluid_assessments" in self.sql:
            return self.existing
        return self.patients

    def fetchone(self):
        return None


PATS = [(1, "67361", "นางสะรีฝ้า  กล้าศึก", "F"), (2, "68418", "นายOOI CHIN", "M"), (3, "65286", "นางสะเปี้ย   ทับสมุทร", "F"),
        (4, "99999", "นายคนอื่น ไกลมาก", "M")]


class ParseTests(unittest.TestCase):
    def test_bcm_text(self):
        r = fs.parse_bcm_text(BCM_TEXT)
        self.assertEqual(r["assessed_at"], date(2026, 9, 16))
        self.assertEqual((r["measured_name"], r["device_id"], r["sex"]), ("sarifa klasuek", "ID88lf", "F"))
        self.assertEqual((r["height_cm"], r["weight_kg"], r["bmi"], r["lti"], r["fti"]), (150.0, 69.0, 30.7, 6.8, 23.7))
        self.assertEqual((r["oh_pre_l"], r["rel_oh_pct"], r["oh_post_l"], r["ufv_ml"]), (-0.3, -2.3, None, None))
        self.assertAlmostEqual(r["v_urea_l"], 22.6126)
        self.assertEqual(r["bp_pre"], "132/86")
        self.assertIsNone(fs.parse_bcm_text("ใบแล็บธรรมดา Hb 10"))

    def test_dates_and_names(self):
        self.assertEqual(fs.parse_date("16/9/2569"), date(2026, 9, 16))
        self.assertIsNone(fs.parse_date("x"))
        self.assertEqual(fs.norm_name("นางสาว สะรีฝ้า  กล้าศึก "), "สะรีฝ้า กล้าศึก")
        self.assertEqual(fs.norm_eng("นาย OOI CHIN"), "ooichin")


class FlagTests(unittest.TestCase):
    def test_flags(self):
        self.assertEqual(fs.flags({"oh_pre_l": 3.7, "rel_oh_pct": 19.9, "lti": 14.1, "sex": "M"}), ["over_severe"])
        self.assertEqual(fs.flags({"oh_pre_l": 2.2, "rel_oh_pct": 16.4, "lti": 10.2, "sex": "F"}), ["over_severe"])  # rel OH
        self.assertEqual(fs.flags({"oh_pre_l": 1.6, "rel_oh_pct": 10.4, "lti": 9.4, "sex": "M"}), ["over", "lti_low"])
        self.assertEqual(fs.flags({"oh_pre_l": -4.3, "rel_oh_pct": -31.7, "lti": None, "sex": "M"}), ["under"])
        self.assertEqual(fs.flags({"oh_pre_l": 0.0, "rel_oh_pct": 0.0, "lti": None, "sex": "M"}), ["incomplete"])
        self.assertEqual(fs.flags({"oh_pre_l": 0.4, "rel_oh_pct": 3.0, "lti": 11.4, "sex": "F"}), [])


class MatchInsertTests(unittest.TestCase):
    def test_match_chain(self):
        recs = [
            {"file_name": "สะรีฝ้า กล้าศึก", "device_id": "ID88lf", "measured_name": "sarifa klasuek", "assessed_at": date(2026, 9, 16), "oh_pre_l": -0.3},
            {"file_name": "โอชินโห", "device_id": "ID2", "measured_name": "Oii Chin", "assessed_at": date(2026, 9, 8), "oh_pre_l": 2.8},
            {"file_name": "สะเปี๊ยะ ทับสมุทร", "device_id": "ID3", "measured_name": "sapia", "assessed_at": date(2026, 9, 8), "oh_pre_l": 0.5},
            {"file_name": "ยี่สุ่น ฝอยกลาง", "device_id": "ID4", "measured_name": "Yisun Foiklang", "assessed_at": date(2026, 9, 12), "oh_pre_l": 6.1},
            {"file_name": "ใครก็ได้", "device_id": "ID5", "measured_name": "x", "assessed_at": date(2026, 9, 12), "oh_pre_l": 1.0},
        ]
        cur = FakeCur(PATS, devmap=[("ID5", 4, "99999")], existing=[(1, date(2026, 9, 16))])
        out = fs.match(cur, recs)
        hows = [(r["hn"], r["how"]) for r in out]
        self.assertEqual(hows[0], ("67361", "name"))
        self.assertEqual(hows[1], ("68418", "eng_name"))
        self.assertEqual(hows[2], ("65286", "name_fuzzy"))
        self.assertEqual(hows[3][1], "none")
        self.assertEqual(hows[4], ("99999", "device_id"))
        self.assertTrue(out[0]["duplicate"])
        self.assertFalse(out[1]["duplicate"])
        self.assertEqual(out[0]["sex"], "F")   # from registry gender
        self.assertEqual(out[0]["assessed_at"], "2026-09-16")

    def test_insert_skips_dup_and_unmatched_and_remembers_device(self):
        rows = [{"patient_id": 1, "hn": "67361", "assessed_at": "2026-09-16", "duplicate": True, "device_id": "ID88lf"},
                {"patient_id": None, "hn": "", "assessed_at": "2026-09-12"},
                {"patient_id": 2, "hn": "68418", "assessed_at": "2026-09-08", "duplicate": False, "device_id": "ID2", "oh_pre_l": 2.8,
                 "weight_kg": 70.2, "how": "eng_name", "ratio": 0.9}]
        cur = FakeCur(PATS)
        res = fs.insert(cur, rows, confirmed_by="nurse1", source_file="BCM6.zip")
        self.assertEqual(res, {"inserted": 1, "skipped": 2})
        self.assertEqual(cur.inserts[0][0], 2)
        self.assertEqual(cur.inserts[0][-3], "BCM6.zip")
        self.assertEqual(cur.maps[0][:2], ("ID2", 2))


class OverviewTests(unittest.TestCase):
    def test_overview_and_hint(self):
        class C(FakeCur):
            def fetchall(self):
                if "FROM hd_fluid_assessments" in self.sql:
                    return [(1, date(2026, 9, 16), 69.0, 3.0, 19.0, 6.8, 23.7, 22.6, "132/86"),
                            (1, date(2026, 6, 10), 68.0, 1.2, 8.0, 7.0, 23.0, 22.0, "130/80"),
                            (2, date(2026, 5, 1), 70.0, -1.5, -10.0, 12.0, 10.0, 30.0, None)]
                return [(1, "67361", "A", "F", 67.0), (2, "68418", "B", "M", 70.0), (3, "65286", "C", "F", None)]
        ov = fs.overview(C([]), today=date(2026, 11, 14))
        by = {p["hn"]: p for p in ov["patients"]}
        a = by["67361"]
        self.assertEqual(a["dw_bcm"], 66.0)
        self.assertEqual(a["dw_diff"], -1.0)
        self.assertIn("over_severe", a["flags"])
        self.assertEqual(a["previous"]["oh"], 1.2)
        b = by["68418"]
        self.assertIn("under", b["flags"])
        self.assertIn("due", b["flags"])            # 197 days
        self.assertEqual(by["65286"]["flags"], ["due"])
        self.assertEqual(ov["stats"]["assessed"], 2)
        self.assertEqual(ov["stats"]["dw_diff"], 2)   # A: -1.0, B: +1.5
        self.assertEqual(ov["patients"][0]["hn"], "67361")   # severe first
        txt = fs.line_text(ov)
        self.assertIn("น้ำเกินมาก 1", txt)
        hint = fs.uf_hint({"oh": 3.0, "rel_oh": 19.0, "days_ago": 59, "flags": ["over_severe"]})
        self.assertIn("ทยอยลด", hint)
        self.assertIsNone(fs.uf_hint(None))


if __name__ == "__main__":
    unittest.main()
