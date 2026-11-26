"""🧬 Lab Merge (26 พ.ย. 2569) — 1 ชุดต่อ 1 วัน (LINE OA + HD) · ค่าล่าสุดที่มีจริง · รวมเข้าชุดจากรูป"""
import unittest
from unittest import mock

from hd_module import lab_merge as lm

E = [
    {"date": "2026-11-14", "source_type": "manual", "source": "manual", "verified_status": "manual", "doc_row_id": None,
     "hemoglobin": None, "ferritin": None, "iron": 34.0, "tibc": 346.0, "anti_hbs": "negative"},
    {"date": "2026-10-28", "source_type": "manual", "source": "lab_sheet_ai", "verified_status": "manual", "doc_row_id": None,
     "hemoglobin": None, "ferritin": 27.9, "iron": None, "tibc": None, "anti_hbs": None},
    {"date": "2026-10-28", "source_type": "photo", "source": "ocr", "verified_status": "pending", "doc_row_id": 55,
     "hemoglobin": 8.5, "ferritin": 28.0, "iron": None, "tibc": None, "anti_hbs": None},
    {"date": "2026-09-04", "source_type": "photo", "source": "ocr", "verified_status": "rejected", "doc_row_id": 40,
     "hemoglobin": 99.0, "ferritin": None, "iron": None, "tibc": None, "anti_hbs": None},
]


class TestMerge(unittest.TestCase):
    def test_one_row_per_date_manual_wins_over_pending(self):
        m = lm.merge_by_date(E)
        self.assertEqual([r["date"] for r in m], ["2026-11-14", "2026-10-28", "2026-09-04"])
        r = m[1]
        self.assertEqual(r["ferritin"], 27.9)          # กรอกมือ/อ่านใบที่พยาบาลยืนยัน ชนะ OCR pending (28.0)
        self.assertEqual(r["hemoglobin"], 8.5)         # ค่าที่มีแต่ในรูป ยังถูกเติม
        self.assertEqual(sorted(r["sources"]), ["manual", "photo"])
        self.assertTrue(r["pending"]); self.assertEqual(r["doc_row_id"], 55); self.assertEqual(r["rows"], 2)
        self.assertFalse(m[0]["pending"]); self.assertEqual(m[0]["iron"], 34.0)

    def test_verified_photo_outranks_pending_photo(self):
        rows = [dict(E[2], verified_status="verified", ferritin=30.0, doc_row_id=56), dict(E[2])]
        m = lm.merge_by_date(rows)
        self.assertEqual(m[0]["ferritin"], 30.0); self.assertEqual(m[0]["doc_row_id"], 56)

    def test_latest_by_analyte_skips_blank_and_rejected(self):
        lat = lm.latest_by_analyte(E)
        self.assertEqual(lat["iron"]["date"], "2026-11-14")
        self.assertEqual(lat["ferritin"]["value"], 27.9)        # แถว 11-14 ว่าง → ข้ามไปวันก่อน
        self.assertEqual(lat["hemoglobin"]["value"], 8.5)       # 09-04 (rejected, 99.0) ถูกข้าม
        self.assertTrue(lat["hemoglobin"]["pending"])
        self.assertEqual(lat["anti_hbs"]["value"], "negative")

    def test_row_is_blank(self):
        m = lm.merge_by_date(E)
        self.assertEqual(sorted(lm.row_is_blank(m[0], ["hemoglobin", "ferritin"])), ["anti_hbs", "iron", "tibc"])
        self.assertEqual(lm.row_is_blank(m[1], ["hemoglobin", "ferritin"]), [])


class TestMergeEndpoint(unittest.TestCase):
    def test_merge_into_doc_updates_row_and_verifies(self):
        import sys
        from flask import Flask
        from hd_module import blueprint as bp
        fake_pg = mock.MagicMock()
        cur = fake_pg.connect.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (55,)
        cur.fetchall.return_value = [("hb",), ("ferritin",), ("iron",), ("antihbs",)]   # ไม่มี vitd ในตาราง
        app = Flask(__name__)
        body = {"doc_row_id": 55, "values": {"ferritin": 27.9, "iron": 34, "vitamin_d": 21.87, "anti_hbs": "Negative", "bun": None},
                "sheet_read_id": "LSq-1", "sheet_ai": {"ferritin": 27.9}, "sheet_uncertain": []}
        with app.test_request_context(json=body), mock.patch.dict(sys.modules, {"psycopg2": fake_pg}), \
             mock.patch.dict("os.environ", {"HD_DATABASE_URL": "postgresql://x"}), mock.patch.object(bp, "log_action"):
            out = bp.labs_merge_into_doc.__wrapped__("67379")
        j = out.get_json() if not isinstance(out, tuple) else out[0].get_json()
        self.assertTrue(j["ok"]); self.assertEqual(sorted(j["applied"]), ["anti_hbs", "ferritin", "iron"]); self.assertEqual(j["skipped"], ["vitamin_d"])
        upd = [c for c in cur.execute.call_args_list if "UPDATE hd_lab_docs" in str(c.args[0])][0]
        self.assertIn("verified_status = 'verified'", upd.args[0]); self.assertIn("ferritin = %s", upd.args[0]); self.assertNotIn("vitd", upd.args[0])
        self.assertIn("negative", upd.args[1])
        self.assertTrue(any("hd_lab_sheet_samples" in str(c.args[0]) for c in cur.execute.call_args_list))

    def test_merge_wrong_patient_404(self):
        import sys
        from flask import Flask
        from hd_module import blueprint as bp
        fake_pg = mock.MagicMock()
        cur = fake_pg.connect.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None
        with Flask(__name__).test_request_context(json={"doc_row_id": 55, "values": {}}), \
             mock.patch.dict(sys.modules, {"psycopg2": fake_pg}), mock.patch.dict("os.environ", {"HD_DATABASE_URL": "postgresql://x"}):
            out = bp.labs_merge_into_doc.__wrapped__("67379")
        self.assertEqual(out[1], 404)


class TestLatestForPatientsCoalesce(unittest.TestCase):
    def test_newest_blank_row_does_not_hide_values(self):
        from datetime import date
        from hd_module import lab_sync
        cols = lab_sync.NUMERIC_COLS + lab_sync.TEXT_COLS
        def row(pid, d, **kv):
            base = {c: None for c in cols}; base.update(kv)
            return (pid, d, "manual") + tuple(base[c] for c in cols) + ("manual", None, None)
        cur = mock.Mock()
        cur.fetchall.return_value = [
            row(7, date(2026, 11, 14), antihbs="negative"),                 # แถวใหม่สุด แต่ไม่มี Hb/K
            row(7, date(2026, 10, 28), hb=8.5, k=4.2, ferritin=27.9),
            row(7, date(2026, 9, 4), hb=10.0, bun=51.0),
        ]
        out = lab_sync.latest_for_patients(cur, [7])
        r = out[7]
        self.assertEqual(r["hb"], 8.5); self.assertEqual(r["k"], 4.2); self.assertEqual(r["ferritin"], 27.9)
        self.assertEqual(r["bun"], 51.0)                      # เติมจากแถวเก่ากว่าเมื่อแถวใหม่ไม่มี
        self.assertEqual(r["antihbs"], "negative")
        self.assertEqual(r["date"], "2026-10-28")             # วันที่หลัก = แถวใหม่สุดที่มี Hb/K
        self.assertIn("flags", r); self.assertNotIn("_core_dated", r)
        self.assertIn("rn <= 8", cur.execute.call_args.args[0])
