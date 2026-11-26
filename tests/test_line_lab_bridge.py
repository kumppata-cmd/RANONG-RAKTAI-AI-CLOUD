"""📄➡️📱 Line Lab Bridge (ขั้นที่ 3, 26 พ.ย. 2569) — จัดกลุ่มตามผู้ป่วย · แปลงเป็น LabReport · ยืนยัน 1 แตะ
ข้อมูลทั้งหมดสมมติ (HN/ชื่อไม่ใช่ของจริง)"""
import json
import unittest
from unittest import mock

from hd_module import lab_sheet_reader as lsr
from hd_module import line_lab_bridge as llb
from hd_module import lab_sync


def _res(hn="00012345", name="นายทดสอบ ระบบ", rows_from=1, values=None, header=True):
    """ผลตัวอ่านแบบย่อ (โครงสร้างเดียวกับ lsr.analyze)"""
    vals = values or {"ferritin": {"value": 226.1, "unit": "ng/ml", "no": 1, "conf": 0.98, "param": "Ferritin", "raw": "226.1"},
                      "iron": {"value": 54.0, "unit": "ug/dl", "no": 3, "conf": 0.98, "param": "Iron", "raw": "54"},
                      "tibc": {"value": 198.0, "unit": "ug/dl", "no": 4, "conf": 0.98, "param": "TIBC", "raw": "198"},
                      "hbsag": {"value": "negative", "unit": "", "no": 6, "conf": 0.99, "param": "HBs Ag (ELISA)", "raw": "Negative"},
                      "antihiv": {"value": "negative", "unit": "", "no": 10, "conf": 0.99, "param": "Anti HIV (ELISA)", "raw": "Negative"}}
    nos = sorted(e["no"] for e in vals.values())
    r = {"header": ({"hn": hn, "hn_norm": lsr.normalize_hn(hn), "name": name, "request_form": "Chemistry",
                     "request_date": "19/11/2026", "lab_date": "2026-11-19", "an": "691"} if header else None),
         "values": vals, "uncertain": [], "problems": [], "rows_seen": list(range(rows_from, rows_from + len(nos))),
         "computed": {"tsat": 27.27} if ("iron" in vals and "tibc" in vals) else {}, "quality": {}}
    if rows_from > 1:
        r["problems"].append(f"เริ่มที่แถว {rows_from} — แถว 1–{rows_from-1} อยู่อีกหน้า")
    return r


class TestPlanGroups(unittest.TestCase):
    def test_three_patients_three_pages(self):
        """เคส 25 พ.ย.: แล็บ 3 คน 3 รูป → 3 กลุ่ม ไม่รวมกัน"""
        g = llb.plan_groups([_res("00011111", "นาย ก"), _res("00022222", "นาย ข"), _res("00033333", "นาง ค")])
        self.assertEqual([x["pages"] for x in g], [[0], [1], [2]])
        self.assertEqual([x["hn"] for x in g], ["11111", "22222", "33333"])

    def test_one_patient_three_pages_merged(self):
        g = llb.plan_groups([_res("00011111"), _res("00011111", rows_from=11), _res("00011111", rows_from=21)])
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["pages"], [0, 1, 2])

    def test_continuation_without_header_attaches_to_previous(self):
        cont = _res(header=False, rows_from=21, values={"creatinine": {"value": 10.41, "unit": "mg/dl", "no": 23, "conf": 0.98, "param": "Creatinine.", "raw": "10.41"}})
        g = llb.plan_groups([_res("00011111"), cont, _res("00022222")])
        self.assertEqual([x["pages"] for x in g], [[0, 1], [2]])
        self.assertIn("creatinine", g[0]["res"]["values"])
        self.assertIn("ferritin", g[0]["res"]["values"])
        self.assertFalse(any(p.startswith("เริ่มที่แถว") for p in g[0]["res"]["problems"]))

    def test_rx_plus_lab_same_patient(self):
        """ใบยา (ตัวอ่านคืน None) + ใบแล็บ คนเดียวกัน → 2 กลุ่ม 2 ประเภท"""
        g = llb.plan_groups([None, _res("00011111")])
        self.assertEqual([x["pages"] for x in g], [[0], [1]])
        self.assertFalse(g[0]["lab"]); self.assertTrue(g[1]["lab"])

    def test_continuation_first_page_stays_alone(self):
        cont = _res(header=False, rows_from=21)
        g = llb.plan_groups([cont])
        self.assertEqual(len(g), 1); self.assertEqual(g[0]["hn"], "")

    def test_non_lab_breaks_continuation_chain(self):
        cont = _res(header=False, rows_from=21)
        g = llb.plan_groups([_res("00011111"), None, cont])
        self.assertEqual([x["pages"] for x in g], [[0], [1], [2]])


class TestConvert(unittest.TestCase):
    def test_usable(self):
        self.assertTrue(llb.usable(_res()))
        self.assertFalse(llb.usable(None))
        r = _res(); r["values"] = {}
        self.assertFalse(llb.usable(r))
        r = _res(); r["problems"].append("หน้านี้มีใบของผู้ป่วย 2 คน (HN ต่างกัน)")
        self.assertFalse(llb.usable(r))

    def test_page_text_has_hn_name_and_rows(self):
        t = llb.page_text(_res(), 1, 2)
        self.assertIn("ห้องปฏิบัติการ", t); self.assertIn("HN 00012345", t); self.assertIn("นายทดสอบ ระบบ", t)
        self.assertIn("Ferritin 226.1 ng/ml", t); self.assertIn("HBs Ag (ELISA) Negative", t)
        self.assertIn("TSAT (คำนวณ) 27.27", t); self.assertIn("[หน้า 1/2]", t)

    def test_lab_report_canonical_and_sync_row(self):
        rep = llb.lab_report(_res(), "DOC1", "regex_ocr")
        names = {r.analyte: r.value for r in rep.results}
        self.assertEqual(names["Ferritin"], 226.1); self.assertEqual(names["Iron"], 54.0); self.assertEqual(names["TIBC"], 198.0)
        self.assertEqual(names["TSAT"], 27.27); self.assertEqual(names["HBsAg"], 0.0); self.assertEqual(names["Anti-HIV"], 0.0)
        self.assertEqual(rep.specimen_date, "2026-11-19"); self.assertEqual(rep.status, "pending_review")
        row = lab_sync.report_to_row(json.loads(rep.to_json()))
        self.assertEqual(row["ferritin"], 226.1); self.assertEqual(row["iron"], 54.0); self.assertEqual(row["tibc"], 198.0)
        self.assertEqual(row["tsat"], 27.27); self.assertEqual(row["hbsag"], "negative"); self.assertEqual(row["antihiv"], "negative")

    def test_lab_report_none_when_empty(self):
        r = _res(); r["values"] = {}; r["computed"] = {}
        self.assertIsNone(llb.lab_report(r, "D", "regex_ocr"))

    def test_value_summary_hides_hiv(self):
        s = llb.value_summary(_res())
        self.assertIn("Ferritin 226.1", s); self.assertIn("TSAT 27.27%", s); self.assertIn("HBsAg −", s)
        self.assertNotIn("HIV", s)
        r = _res(); r["uncertain"] = [{"key": "pth", "label": "PTH", "value": 1227}]
        self.assertIn("❓1", llb.value_summary(r))

    def test_approve_and_confirm(self):
        rep = llb.lab_report(_res(), "DOC1", "regex_ocr")
        j = llb.approve_report_json(rep.to_json(), "physician")
        d = json.loads(j)
        self.assertEqual(d["status"], "approved"); self.assertEqual(d["verified_via"], "line_quick_confirm")
        self.assertEqual(d["reviewed_by"], "line:physician")
        self.assertIsNone(llb.approve_report_json("not json", "x"))
        cur = mock.Mock(); cur.rowcount = 1
        self.assertEqual(llb.confirm_in_hd(cur, "DOC1", "physician"), 1)
        self.assertIn("verified", cur.execute.call_args.args[0])

    def test_confirm_quick_items(self):
        items = [{"n": 2, "doc8": "bbbbbbbb", "status": "ok", "labvals": "Hb 8"},
                 {"n": 1, "doc8": "aaaaaaaa", "status": "ok", "labvals": ""},
                 {"n": 3, "doc8": "cccccccc", "status": "ok", "labvals": "K 5"}]
        q = llb.confirm_quick_items(items)
        self.assertEqual([x["action"]["text"] for x in q], ["ยืนยันแล็บ bbbbbbbb", "ยืนยันแล็บ cccccccc"])
        self.assertTrue(all(len(x["action"]["label"]) <= 20 for x in q))

    def test_is_enabled_default_off(self):
        self.assertFalse(llb.is_enabled(lambda k, d="": ""))
        self.assertTrue(llb.is_enabled(lambda k, d="": "on"))
        self.assertFalse(llb.is_enabled(lambda k, d="": (_ for _ in ()).throw(RuntimeError())))

    def test_remember_recall(self):
        llb.remember("X1", _res()); self.assertIsNotNone(llb.recall("X1")); self.assertIsNone(llb.recall("nope"))


class TestLabSyncExtraCols(unittest.TestCase):
    def test_insert_uses_only_existing_extra_cols(self):
        """lab_sync ใส่ iron/tibc/vitd/antihbc ใน INSERT เฉพาะเมื่อตารางมีคอลัมน์ (ไม่ทำ sync ล้มถ้า migration ไม่ผ่าน)"""
        rep = llb.lab_report(_res(), "DOCX", "regex_ocr")
        storage = mock.Mock(); storage.get.return_value = rep.to_json()
        doc_index = mock.Mock(); doc_index._docs = [{"document_id": "DOCX", "category": "lab", "submitted_at": "2026-11-26",
                                                    "patient_hn_folder": "67379_ทดสอบ"}]
        for have_extra in (True, False):
            cur = mock.MagicMock()
            cols = [("hb",), ("iron",), ("tibc",), ("vitd",), ("antihbc",)] if have_extra else [("hb",)]
            cur.fetchall.side_effect = [[], [(7, "67379")], cols, []]   # have docs, patients, columns, prev row
            cur.fetchone.return_value = None
            conn = mock.MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
            st = lab_sync.sync_labs(conn, storage, doc_index)
            self.assertEqual(st["inserted"], 1, st)
            ins = [c.args[0] for c in cur.execute.call_args_list if "INSERT INTO hd_lab_docs" in str(c.args[0])][0]
            self.assertEqual("iron" in ins, have_extra)
            self.assertEqual("antihbc" in ins, have_extra)


class TestServerDispatch(unittest.TestCase):
    """server._lab_bridge_dispatch — จัดกลุ่มและส่งเข้า pipeline (mock ทุกอย่างที่แตะไฟล์/LINE)"""

    def _server(self):
        import server as _s
        return _s

    def test_disabled_returns_false(self):
        s = self._server()
        with mock.patch.object(s, "_lab_bridge_enabled", return_value=False), \
             mock.patch.object(s, "_lab_bridge_read_pages") as rp:
            self.assertFalse(s._lab_bridge_dispatch([(b"x", ".jpg", "s1", "doc1", "doc1.jpg")], "u", "U1", 0))
            rp.assert_not_called()

    def test_no_lab_pages_returns_false(self):
        s = self._server()
        with mock.patch.object(s, "_lab_bridge_enabled", return_value=True), \
             mock.patch.object(s, "_lab_bridge_read_pages", return_value=[None, None]):
            self.assertFalse(s._lab_bridge_dispatch([(b"x", ".jpg", "s1", "d1", "d1.jpg"), (b"y", ".jpg", "s2", "d2", "d2.jpg")], "u", "U1", 0))

    def test_three_patients_become_three_docs_one_batch(self):
        s = self._server()
        pages = [(b"a", ".jpg", "s1", "d1aaaaaa", "d1.jpg"), (b"b", ".jpg", "s2", "d2bbbbbb", "d2.jpg"), (b"c", ".jpg", "s3", "d3cccccc", "d3.jpg")]
        results = [_res("00011111", "นาย ก"), _res("00022222", "นาย ข"), _res("00033333", "นาง ค")]
        with mock.patch.object(s, "_lab_bridge_enabled", return_value=True), \
             mock.patch.object(s, "_lab_bridge_read_pages", return_value=results), \
             mock.patch.object(s, "_lab_bridge_folder", side_effect=lambda g: "F" + g["hn"]), \
             mock.patch.object(s, "save_to_inbox", side_effect=lambda d, f: "/tmp/" + f), \
             mock.patch.object(s, "process_file_async") as pfa, \
             mock.patch.object(s, "_batch_register") as br:
            self.assertTrue(s._lab_bridge_dispatch(pages, "uhash", "U1", 123))
        br.assert_called_once(); self.assertEqual(br.call_args.args[1], 3)
        self.assertEqual(pfa.call_count, 3)
        kw = [c.kwargs for c in pfa.call_args_list]
        self.assertEqual([k["batch_patient_folder"] for k in kw], ["F11111", "F22222", "F33333"])
        self.assertEqual([k["batch_index"] for k in kw], [1, 2, 3])
        self.assertTrue(all(k["merged_page_count"] == 0 and "HN 0002" in kw[1]["merged_ocr_text"] for k in kw[1:2]))
        self.assertIsNotNone(llb.recall("d2bbbbbb"))

    def test_one_patient_three_pages_one_pdf(self):
        s = self._server()
        pages = [(b"a", ".jpg", "s1", "p1aaaaaa", "p1.jpg"), (b"b", ".jpg", "s2", "p2bbbbbb", "p2.jpg"), (b"c", ".jpg", "s3", "p3cccccc", "p3.jpg")]
        results = [_res("00011111"), _res("00011111", rows_from=11), _res(header=False, rows_from=21)]
        with mock.patch.object(s, "_lab_bridge_enabled", return_value=True), \
             mock.patch.object(s, "_lab_bridge_read_pages", return_value=results), \
             mock.patch.object(s, "_lab_bridge_folder", return_value="F1"), \
             mock.patch.object(s, "_lab_bridge_pdf", return_value=b"%PDF"), \
             mock.patch.object(s, "save_to_inbox", side_effect=lambda d, f: "/tmp/" + f), \
             mock.patch.object(s, "process_file_async") as pfa, \
             mock.patch.object(s, "_batch_register") as br:
            self.assertTrue(s._lab_bridge_dispatch(pages, "uhash", "U1", 123))
        self.assertEqual(br.call_args.args[1], 1)
        self.assertEqual(pfa.call_count, 1)
        kw = pfa.call_args.kwargs
        self.assertEqual(kw["merged_page_count"], 3); self.assertEqual(kw["doc_id"], "p1aaaaaa")
        self.assertEqual(kw["page_sha256s"], ["s1", "s2", "s3"]); self.assertIn("[หน้า 3/3]", kw["merged_ocr_text"])
        self.assertTrue(pfa.call_args.args[0].endswith("p1aaaaaa.pdf"))

    def test_pdf_failure_splits_pages(self):
        s = self._server()
        pages = [(b"a", ".jpg", "s1", "q1aaaaaa", "q1.jpg"), (b"b", ".jpg", "s2", "q2bbbbbb", "q2.jpg")]
        with mock.patch.object(s, "_lab_bridge_enabled", return_value=True), \
             mock.patch.object(s, "_lab_bridge_read_pages", return_value=[_res("00011111"), _res("00011111", rows_from=11)]), \
             mock.patch.object(s, "_lab_bridge_folder", return_value="F1"), \
             mock.patch.object(s, "_lab_bridge_pdf", return_value=None), \
             mock.patch.object(s, "save_to_inbox", side_effect=lambda d, f: "/tmp/" + f), \
             mock.patch.object(s, "process_file_async") as pfa, \
             mock.patch.object(s, "_batch_register") as br:
            self.assertTrue(s._lab_bridge_dispatch(pages, "uhash", "U1", 0))
        self.assertEqual(br.call_args.args[1], 2); self.assertEqual(pfa.call_count, 2)

    def test_confirm_regex_and_report_hook(self):
        s = self._server()
        self.assertIsNotNone(s._LAB_CONFIRM_RE.match("ยืนยันแล็บ a1b2c3d4"))
        self.assertIsNotNone(s._LAB_CONFIRM_RE.match("  labok A1B2C3D4 "))
        self.assertIsNone(s._LAB_CONFIRM_RE.match("ยืนยันแล็บ"))
        llb.remember("docR", _res())
        rep = s._lab_bridge_report("docR", "regex_ocr")
        self.assertIsNotNone(rep); self.assertTrue(any(r.analyte == "Ferritin" for r in rep.results))
        self.assertIsNone(s._lab_bridge_report("missing", "regex_ocr"))
        self.assertIn("Ferritin", s._lab_bridge_values_line("docR"))

    def test_batch_summary_lists_values(self):
        s = self._server()
        col = {"total": 2, "items": [
            {"n": 1, "doc8": "aaaaaaaa", "cat": "ผลตรวจแล็บ", "emoji": "🔬", "storage": "x", "folder": "67379_ก", "clinical": True,
             "status": "ok", "category": "lab", "ambiguous": "", "labvals": "Hb 8.3 · K 4.2"},
            {"n": 2, "doc8": "bbbbbbbb", "cat": "ผลตรวจแล็บ", "emoji": "🔬", "storage": "x", "folder": "67380_ข", "clinical": True,
             "status": "ok", "category": "lab", "ambiguous": "", "labvals": "Ferritin 226"}]}
        t = s._batch_summary_text(col, False)
        self.assertIn("🧪 ①aaaaaaaa: Hb 8.3 · K 4.2", t); self.assertIn("🧪 ②bbbbbbbb: Ferritin 226", t)
        self.assertIn("ยืนยันแล็บ", t)


if __name__ == "__main__":
    unittest.main()


class TestConfirmCommand(unittest.TestCase):
    def test_confirm_happy_and_errors(self):
        import server as s
        rep = llb.lab_report(_res(), "DOCCONF1234", "regex_ocr")
        fake_index = mock.Mock(); fake_index._docs = [{"document_id": "DOCCONF1234", "category": "lab", "patient_hn_folder": "67379_ก"}]
        fake_storage = mock.Mock(); fake_storage.get.return_value = rep.to_json()
        with mock.patch.object(s, "doc_index", fake_index), mock.patch.object(s, "storage", fake_storage), \
             mock.patch.object(s, "audit_logger") as al, mock.patch.dict("os.environ", {"HD_DATABASE_URL": ""}), \
             mock.patch.object(s, "PHYSICIAN_USERS", {"phys"}):
            msg = s._lab_confirm_command("DOCCONF", "phys")
            self.assertTrue(msg.startswith("✅"), msg)
            put_json = json.loads(fake_storage.put.call_args.args[1].decode("utf-8"))
            self.assertEqual(put_json["status"], "approved"); self.assertEqual(put_json["reviewed_by"], "line:physician")
            self.assertEqual(al.log.call_args.args[0], "lab_confirmed_via_line")
            # ไม่พบ / ไม่ใช่แล็บ / ไม่มี JSON
            self.assertTrue(s._lab_confirm_command("ZZZZZZ", "phys").startswith("❌"))
            fake_index._docs[0]["category"] = "rx"
            self.assertIn("ไม่ใช่ผลแล็บ", s._lab_confirm_command("DOCCONF", "phys"))
            fake_index._docs[0]["category"] = "lab"; fake_storage.get.return_value = None
            self.assertIn("ยังไม่มีค่า", s._lab_confirm_command("DOCCONF", "phys"))
