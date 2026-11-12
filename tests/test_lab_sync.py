"""B6: lab JSON → row, flags, sync with fake storage/index."""
import unittest
from datetime import date

from hd_module import lab_sync as ls


class RowAndFlagsTests(unittest.TestCase):
    def test_report_to_row(self):
        rep = {"results": [{"analyte": "Hemoglobin", "value": 8.4}, {"analyte": "Potassium", "value": 6.2},
                           {"analyte": "Phosphate", "value": 7.5}, {"analyte": "HBsAg", "value": None, "raw_text": "HBsAg Negative"},
                           {"analyte": "Unknown", "value": 1}]}
        row = ls.report_to_row(rep)
        self.assertEqual((row["hb"], row["k"], row["phos"], row["hbsag"]), (8.4, 6.2, 7.5, "negative"))

    def test_flags(self):
        f = ls.flags_for({"hb": 8.4, "k": 6.2, "phos": 7.5, "ca": 7.5, "hbsag": "positive"})
        codes = {x["code"]: x["sev"] for x in f}
        self.assertEqual(codes, {"k_high": "critical", "hb_low": "warning", "phos_high": "warning", "ca_low": "warning", "hbsag": "info"})
        self.assertEqual(ls.flags_for({"hb": 11, "k": 4.5}), [])
        self.assertEqual(ls.flags_for({"hb": 6.9})[0]["sev"], "critical")

    def test_doc_hn(self):
        self.assertEqual(ls._doc_hn({"patient_hn_folder": "67398_นางจารึก"}), "67398")
        self.assertEqual(ls._doc_hn({"gcs_object_path": "04_LAB_RESULTS/2026-Q4/2026-11/57057_x/abc.pdf"}), "57057")
        self.assertEqual(ls._doc_hn({}), "")


class _Cur:
    def __init__(self): self.sql = []; self.inserted = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.sql.append(sql)
        if sql.startswith("INSERT"):
            self.inserted.append(params)
    def fetchall(self):
        last = self.sql[-1]
        if "SELECT doc_id" in last: return [("old-doc",)]
        if "SELECT id, hn" in last: return [(9, "67398")]
        return []


class _Conn:
    def __init__(self): self.cur = _Cur()
    def cursor(self): return self.cur
    def commit(self): pass


class _Storage:
    def get(self, path):
        import json
        if "new-doc" in path:
            return json.dumps({"specimen_date": "2026-11-10", "status": "approved",
                               "results": [{"analyte": "Potassium", "value": 6.1}]})
        return None


class _Index:
    _docs = [{"document_id": "old-doc", "category": "lab", "patient_hn_folder": "67398_x", "received_at": "2026-11-11"},
             {"document_id": "new-doc", "category": "lab", "patient_hn_folder": "67398_x", "received_at": "2026-11-11"},
             {"document_id": "no-hn", "category": "lab", "received_at": "2026-11-11"},
             {"document_id": "rx", "category": "prescription", "patient_hn_folder": "67398_x"}]
    def _ensure_fresh(self): pass


class SyncTests(unittest.TestCase):
    def test_sync_inserts_only_new_matched_docs(self):
        conn = _Conn()
        st = ls.sync_labs(conn, _Storage(), _Index())
        self.assertEqual(st["inserted"], 1)
        self.assertEqual(st["skipped"], 1)      # old-doc already in table
        self.assertEqual(st["no_hn"], 1)
        vals = conn.cur.inserted[0]
        self.assertEqual(list(vals[:3]), [9, "67398", "new-doc"])
        self.assertEqual(vals[3], date(2026, 11, 10))
        self.assertIn(6.1, vals)

    def test_no_index_is_noop(self):
        self.assertEqual(ls.sync_labs(_Conn(), None, None)["inserted"], 0)


if __name__ == "__main__":
    unittest.main()
