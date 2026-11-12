"""Patient 360 timeline builder (ว้าว #5) — fake DB, checks merge/sort/summary."""
import unittest
from datetime import date, datetime

from hd_module import patient360


class FakeCur:
    def __init__(self, table): self.table = table; self.rows = []
    def execute(self, sql, params=()):
        if sql.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK")): return
        for frag, rows in self.table.items():
            if frag in sql:
                self.rows = rows if not callable(rows) else rows(); return
        raise RuntimeError("no such table")   # simulates an older DB → block skipped
    def fetchall(self): return self.rows
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeConn:
    def __init__(self, table): self._c = FakeCur(table)
    def cursor(self): return self._c


PATIENT = [(7, "100", "นาย ก", "AVF ซ้าย", 55.0, "Eprex", "4000 IU", "จ,พ,ศ", date(1950, 1, 1), True)]
SESSIONS = [
    (1, date(2026, 11, 2), "morning", 3, "completed", datetime(2026, 11, 2, 0, 0), datetime(2026, 11, 2, 4, 0),
     57.5, 55.2, 2500, 2300, 9.4, 1.3, 70.0, "Eprex", "4000 IU", None, 140.0, 1, 88),
    (2, date(2026, 11, 4), "morning", 3, "completed", datetime(2026, 11, 4, 0, 0), datetime(2026, 11, 4, 4, 0),
     57.0, 55.0, 2000, 2000, None, None, None, None, None, "ดี", 150.0, 0, 120),
]
ALERTS = [(11, datetime(2026, 11, 2, 2, 0), "critical", "IDH", "SBP 88", True, "NSS 200 mL", "หมอ", datetime(2026, 11, 2, 2, 5), 1, 3)]


class BuildTests(unittest.TestCase):
    def test_not_found(self):
        self.assertIsNone(patient360.build(FakeConn({"FROM hd_patient_mapping WHERE id": []}), 7))

    def test_merge_and_summary(self):
        c = FakeConn({"FROM hd_patient_mapping WHERE id": PATIENT, "FROM hd_sessions s WHERE s.patient_id": SESSIONS,
                      "FROM hd_clinical_alerts a LEFT JOIN": ALERTS})
        d = patient360.build(c, 7, days=90)
        self.assertEqual(d["patient"]["name"], "นาย ก")
        self.assertIn("age", d["patient"])
        types = [e["type"] for e in d["events"]]
        self.assertEqual(types, ["session", "alert", "session"])          # newest first; alert 02:00 is after the 00:00 session start
        self.assertEqual(d["events"][0]["session_id"], 2)
        s = d["summary"]
        self.assertEqual((s["sessions"], s["completed"], s["idh_sessions"], s["alerts"], s["orders_pending"]), (2, 2, 1, 1, 0))
        self.assertEqual(s["last_hb"], 9.4)
        self.assertEqual(s["uf_achieved_pct"], 96)
        self.assertEqual(len(d["series"]["weight"]), 2)
        self.assertEqual(d["series"]["idwg"][0]["pct"], 4.5)
        self.assertEqual(d["series"]["vp"][-1]["v"], 150.0)
        self.assertEqual(d["series"]["hb"], [{"d": "2026-11-02", "v": 9.4, "src": "session"}])


if __name__ == "__main__":
    unittest.main()
