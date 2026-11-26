"""📄 Lab Sheet Reader (26 พ.ย. 2569) — ฟอร์มใบแล็บ รพ.ระนอง · ตัวอย่างเลียนแบบใบจริง (HN/ชื่อสมมติ)"""
import json
import unittest
from unittest import mock

from hd_module import lab_sheet_reader as lsr


def _hdr(hn="00012345", name="นายทดสอบ ระบบ", form="Chemistry"):
    return {"hn": hn, "an": "691119080434", "name": name, "request_form": form, "request_by": "นพ.ก",
            "request_date": "19/11/2026", "request_time": "08:04:34", "request_number": "2979186",
            "visit_type": "OPD", "receive_date": "19/11/2026", "receive_time": "07:12:46"}


def _row(no, p, r, u="", ref="", conf=0.98):
    return {"no": no, "parameter": p, "result": r, "unit": u, "reference": ref, "conf": conf}


# ใบที่ 1 ของตัวอย่าง (Chemistry: iron studies + serology + TSAT เขียนมือ)
RAW_CHEM = {"blocks": [{"header": _hdr(), "rows": [
    _row(1, "Ferritin", "226.1", "ng/ml"), _row(2, "PTH;intact", "1227.0", "pg/ml", "18.5-88.0"),
    _row(3, "Iron", "54", "ug/dl"), _row(4, "TIBC", "198", "ug/dl", "250-425"),
    _row(5, "Vitamin D total", "46.17", "ng/ml", "30-100"), _row(6, "HBs Ag (ELISA)", "Negative"),
    _row(7, "HBs Ab (ELISA)", "Positive"), _row(8, "HBc Ab (ELISA)", "Positive"),
    _row(9, "HCV Ab (ELISA)", "Negative"), _row(10, "Anti HIV (ELISA)", "Negative")]}],
    "handwritten": ["TSAT=27.27%"], "quality": {"blur": False, "cut": False, "note": ""}}

# สแกน 1 หน้า 2 บล็อก: Routine Lab 1–20 + หน้าต่อ 21–23 ไม่มีหัวใบ (ตัวอย่าง 222)
RAW_ROUTINE_2BLOCK = {"blocks": [
    {"header": _hdr("00325486", "นายแสน ทดสอบ", "Routine Lab"), "rows": [
        _row(1, "BUN", "45", "mg/dl"), _row(2, "Sodium ;Na.", "139", "mmol/L"), _row(3, "Potassium; K.", "4.17", "mmol/L"),
        _row(4, "Chloride; Cl.", "99", "mmol/L"), _row(5, "Bicarbonate; HCO3", "29", "mmol/L"), _row(6, "Calcium", "8.3", "mg/dl"),
        _row(7, "Phosphorus", "3.40", "mg/dl"), _row(8, "WBC", "5.00", "10^3/mm^3"), _row(9, "RBC...", "3.82", "10^6/mm^3"),
        _row(10, "HGB", "8.3", "g/dl"), _row(11, "HCT.", "26.7", "%"), _row(12, "MCV.", "69.9", "fl"), _row(13, "MCH.", "21.7", "pg"),
        _row(14, "MCHC.", "31.1", "g/dl"), _row(15, "PLT", "153", "10^3/mm^3"), _row(16, "RDW.", "16.9", "%"),
        _row(17, "Neutrophil", "57", "%"), _row(18, "Lymphocyte", "28", "%"), _row(19, "Monocyte", "6", "%"), _row(20, "Eosinophil", "9", "%")]},
    {"header": None, "rows": [_row(21, "Basophil", "0", "%"), _row(22, "GFR", "4.66"), _row(23, "Creatinine.", "10.41", "mg/dl")]}],
    "handwritten": [], "quality": {"blur": False, "cut": False}}


class TestCanonical(unittest.TestCase):
    def test_form_names(self):
        for p, k in [("PTH;intact", "pth"), ("Sodium ;Na.", "na"), ("Potassium; K.", "k"), ("Chloride; Cl.", "cl"),
                     ("Bicarbonate; HCO3", "hco3"), ("HBs Ag (ELISA)", "hbsag"), ("HBs Ab (ELISA)", "antihbs"),
                     ("HBc Ab (ELISA)", "antihbc"), ("HCV Ab (ELISA)", "antihcv"), ("Anti HIV (ELISA)", "antihiv"),
                     ("Vitamin D total", "vitd"), ("RBC...", "rbc"), ("HGB", "hb"), ("HCT.", "hct"), ("MCH.", "mch"),
                     ("MCHC.", "mchc"), ("Creatinine.", "creatinine"), ("Glucose (POCT)", "glucose"), ("FBS", "glucose"),
                     ("SGOT (AST)", "ast"), ("SGPT (ALT)", "alt"), ("Alkaline phosphatase", "alp"), ("Total Bilirubin", "tbili"),
                     ("Direct Bilirubin", "dbili"), ("Uric acid", "uric"), ("Iron", "iron"), ("TIBC", "tibc"), ("GFR", "gfr")]:
            self.assertEqual(lsr.canonical(p), k, p)
        self.assertIsNone(lsr.canonical("Something new"))
        self.assertIsNone(lsr.canonical(""))

    def test_mch_not_mchc(self):
        self.assertEqual(lsr.canonical("MCHC."), "mchc")
        self.assertEqual(lsr.canonical("MCH."), "mch")


class TestParseValue(unittest.TestCase):
    def test_numeric_and_range(self):
        self.assertEqual(lsr.parse_value("pth", "1227.0"), (1227.0, None))
        self.assertEqual(lsr.parse_value("pth", "2681.4")[0], 2681.4)       # ค่าจริงในตัวอย่าง
        self.assertEqual(lsr.parse_value("alt", "1450")[0], 1450.0)
        v, note = lsr.parse_value("k", "41.7")                                 # จุดทศนิยมหาย
        self.assertIsNone(v); self.assertIn("นอกช่วง", note)
        v, note = lsr.parse_value("hb", "Negative")
        self.assertIsNone(v); self.assertIn("ไม่ใช่ตัวเลข", note)

    def test_qualitative(self):
        self.assertEqual(lsr.parse_value("hbsag", "Negative"), ("negative", None))
        self.assertEqual(lsr.parse_value("antihbs", "Positive"), ("positive", None))
        self.assertEqual(lsr.parse_value("antihcv", "Non-reactive")[0], "negative")
        self.assertIsNone(lsr.parse_value("antihiv", "12.3")[0])


class TestHnAndName(unittest.TestCase):
    def test_hn_leading_zeros(self):
        self.assertEqual(lsr.normalize_hn("00038358"), "38358")
        self.assertEqual(lsr.normalize_hn("HN 00013373"), "13373")
        self.assertEqual(lsr.normalize_hn(""), "")

    def test_names(self):
        self.assertTrue(lsr.names_match("นายSOE HTET เมียนมา(CC7349319)", "SOE HTET"))
        self.assertTrue(lsr.names_match("น.ส.ยุพิน โพธิ์ยิ้ม", "นางยุพิน โพธิ์ยิ้ม"))
        self.assertTrue(lsr.names_match("นายPAING SOE(ไป่โซ) เมียนมา(MG13243", "Paing Soe"))
        self.assertFalse(lsr.names_match("นายสมาน ปิสาด", "นางตรองใจ พิพัฒน์"))
        self.assertIsNone(lsr.names_match("", "นางตรองใจ"))


class TestAnalyze(unittest.TestCase):
    def test_chem_sheet_full(self):
        r = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        self.assertEqual(r["header"]["hn_norm"], "12345")
        v = r["values"]
        self.assertEqual(v["ferritin"]["value"], 226.1)
        self.assertEqual(v["pth"]["value"], 1227.0)
        self.assertEqual(v["iron"]["value"], 54.0)
        self.assertEqual(v["tibc"]["value"], 198.0)
        self.assertEqual(v["vitd"]["value"], 46.17)
        self.assertEqual(v["hbsag"]["value"], "negative")
        self.assertEqual(v["antihbs"]["value"], "positive")
        self.assertEqual(v["antihbc"]["value"], "positive")
        self.assertEqual(v["antihcv"]["value"], "negative")
        self.assertEqual(v["antihiv"]["value"], "negative")
        self.assertEqual(r["computed"]["tsat"], 27.27)
        self.assertEqual(r["computed"]["tsat_check"], "ตรงกับที่เขียนมือ")
        self.assertEqual(r["rows_seen"], list(range(1, 11)))
        self.assertEqual(r["missing_rows"], [])
        self.assertEqual(r["problems"], [])
        self.assertEqual(r["uncertain"], [])

    def test_tsat_mismatch_flags_iron(self):
        raw = json.loads(json.dumps(RAW_CHEM))
        raw["blocks"][0]["rows"][2]["result"] = "84"     # Iron อ่านผิด 54→84
        r = lsr.analyze(raw)
        self.assertEqual(r["computed"]["tsat"], 42.42)
        self.assertTrue(any("TSAT คำนวณ" in p for p in r["problems"]))

    def test_two_block_continuation(self):
        r = lsr.analyze(json.loads(json.dumps(RAW_ROUTINE_2BLOCK)))
        self.assertEqual(r["blocks"], 2)
        self.assertEqual(r["header"]["hn_norm"], "325486")
        self.assertEqual(r["rows_seen"], list(range(1, 24)))
        self.assertEqual(r["values"]["creatinine"]["value"], 10.41)
        self.assertEqual(r["values"]["baso"]["value"], 0.0)
        self.assertEqual(r["values"]["hb"]["value"], 8.3)
        self.assertEqual(r["problems"], [])
        self.assertEqual(len(r["values"]), 23)

    def test_missing_rows_and_start_not_one(self):
        raw = json.loads(json.dumps(RAW_ROUTINE_2BLOCK))
        raw["blocks"][0]["rows"] = raw["blocks"][0]["rows"][1:]          # แถว 1 (BUN) หาย → เริ่มแถว 2 (ตัวอย่าง 444)
        del raw["blocks"][0]["rows"][5]                                   # แถว 7 หาย
        r = lsr.analyze(raw)
        self.assertEqual(r["missing_rows"], [7])
        self.assertTrue(any("เริ่มที่แถว 2" in p for p in r["problems"]))
        self.assertTrue(any("แถวหาย: 7" in p for p in r["problems"]))

    def test_low_conf_and_out_of_range_go_to_uncertain(self):
        raw = json.loads(json.dumps(RAW_CHEM))
        raw["blocks"][0]["rows"][0]["conf"] = 0.5          # Ferritin ไม่มั่นใจ
        raw["blocks"][0]["rows"][1]["result"] = "12270.0"  # PTH นอกช่วง
        r = lsr.analyze(raw)
        self.assertNotIn("ferritin", r["values"])
        self.assertNotIn("pth", r["values"])
        keys = {u["key"] for u in r["uncertain"]}
        self.assertEqual(keys, {"ferritin", "pth"})

    def test_two_patients_on_one_page(self):
        raw = json.loads(json.dumps(RAW_CHEM))
        raw["blocks"].append({"header": _hdr("00099999", "นางอื่น"), "rows": [_row(1, "BUN", "20", "mg/dl")]})
        r = lsr.analyze(raw)
        self.assertTrue(any("2 คน" in p for p in r["problems"]))

    def test_cbc_consistency(self):
        raw = json.loads(json.dumps(RAW_ROUTINE_2BLOCK))
        raw["blocks"][0]["rows"][9]["result"] = "13.3"     # HGB 8.3 → 13.3 ขณะ HCT 26.7
        r = lsr.analyze(raw)
        self.assertTrue(any("Hct/Hb" in p for p in r["problems"]))

    def test_unknown_row_kept_aside(self):
        raw = json.loads(json.dumps(RAW_CHEM))
        raw["blocks"][0]["rows"].append(_row(11, "Zinc", "80", "ug/dl"))
        r = lsr.analyze(raw)
        self.assertEqual(r["unknown_rows"][0]["parameter"], "Zinc")
        self.assertEqual(r["problems"], [])


class TestMergePasses(unittest.TestCase):
    def test_agree_and_disagree(self):
        p1 = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        raw2 = json.loads(json.dumps(RAW_CHEM))
        raw2["blocks"][0]["rows"][0]["result"] = "228.1"   # Ferritin ต่าง
        p2 = lsr.analyze(raw2)
        m = lsr.merge_passes(p1, p2)
        self.assertTrue(m["double_checked"])
        self.assertNotIn("ferritin", m["values"])
        u = [x for x in m["uncertain"] if x["key"] == "ferritin"][0]
        self.assertEqual(u["alt"], 228.1)
        self.assertIn("pth", m["values"])
        self.assertEqual(m["computed"]["tsat"], 27.27)

    def test_hn_conflict_blocks_matching(self):
        p1 = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        raw2 = json.loads(json.dumps(RAW_CHEM)); raw2["blocks"][0]["header"]["hn"] = "00012346"
        m = lsr.merge_passes(p1, lsr.analyze(raw2))
        self.assertTrue(m["header"].get("hn_conflict"))
        self.assertTrue(any("HN อ่าน 2 รอบไม่ตรง" in p for p in m["problems"]))

    def test_single_pass(self):
        p1 = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        self.assertFalse(lsr.merge_passes(p1, None)["double_checked"])


class TestReadSheet(unittest.TestCase):
    def _resp(self, raw):
        r = mock.Mock(); r.status_code = 200
        r.json.return_value = {"content": [{"type": "text", "text": "```json\n" + json.dumps(raw, ensure_ascii=False) + "\n```"}], "stop_reason": "end_turn"}
        return r

    def test_read_sheet_double(self):
        calls = []
        def post(b):
            calls.append(json.loads(b)["messages"][0]["content"][1]["text"])
            return self._resp(RAW_CHEM)
        res = lsr.read_sheet(b"not-an-image", "key", "claude-x", double=True, post_fn=post)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])        # prompt รอบ 2 ต่างจากรอบแรก
        self.assertTrue(res["double_checked"])
        self.assertEqual(res["values"]["ferritin"]["value"], 226.1)

    def test_read_sheet_no_json(self):
        r = mock.Mock(); r.status_code = 200; r.json.return_value = {"content": [{"type": "text", "text": "ขออภัย"}]}
        self.assertIsNone(lsr.read_sheet(b"x", "key", "m", double=False, post_fn=lambda b: r))

    def test_busy_propagates(self):
        from hd_module import machine_ocr as mo
        held = [mo.VISION_SLOTS.acquire(blocking=False) for _ in range(2)]
        try:
            with self.assertRaises(RuntimeError):
                lsr.read_once(b"x", "key", "m", 0, post_fn=lambda b: None)
        finally:
            for h in held:
                if h:
                    mo.VISION_SLOTS.release()


class TestSummaryAndMatch(unittest.TestCase):
    def test_summary_hides_hiv(self):
        r = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        lines = lsr.summary_lines(r, {"status": "matched", "patient_name": "นายทดสอบ ระบบ"})
        txt = "\n".join(lines)
        self.assertIn("Ferritin 226.1", txt)
        self.assertIn("TSAT (คำนวณ) 27.27", txt)
        self.assertIn("✅ ตรงทะเบียน", txt)
        self.assertNotIn("HIV", txt)

    def test_match_patient(self):
        cur = mock.Mock()
        cur.fetchall.return_value = [(7, "12345", "นายทดสอบ ระบบ")]
        self.assertEqual(lsr.match_patient(cur, {"hn_norm": "12345", "hn": "00012345", "name": "นายทดสอบ ระบบ"})["status"], "matched")
        self.assertEqual(lsr.match_patient(cur, {"hn_norm": "12345", "hn": "00012345", "name": "นางคนอื่น ไกล"})["status"], "name_mismatch")
        cur.fetchall.return_value = []
        self.assertEqual(lsr.match_patient(cur, {"hn_norm": "12345", "hn": "00012345"})["status"], "not_found")
        self.assertEqual(lsr.match_patient(cur, {"hn_norm": ""})["status"], "no_hn")


if __name__ == "__main__":
    unittest.main()


class TestEndpoint(unittest.TestCase):
    """POST /hd/api/v1/lab-sheet/read — เรียกฟังก์ชันตรง (ข้าม auth) · mock ตัวอ่าน"""

    def _call(self, data, filename, read_side_effect, double="1"):
        import io
        from flask import Flask
        from hd_module import blueprint as bp, lab_sheet_reader as lsr
        app = Flask(__name__)
        fn = bp.lab_sheet_read.__wrapped__
        with app.test_request_context(method="POST", data={"file": (io.BytesIO(data), filename), "double": double},
                                      content_type="multipart/form-data"), \
             mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k", "HD_DATABASE_URL": ""}), \
             mock.patch.object(lsr, "read_sheet", side_effect=read_side_effect) as rs, \
             mock.patch.object(bp, "log_action"), \
             mock.patch("hd_module.patient_engage.ai_model", return_value="claude-x"):
            out = fn()
            return out, rs

    def test_image_ok(self):
        res = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        out, rs = self._call(b"\xff\xd8jpeg", "a.jpg", lambda *a, **k: res)
        j = out.get_json()
        self.assertEqual(len(j["pages"]), 1)
        self.assertEqual(j["pages"][0]["values"]["ferritin"]["value"], 226.1)
        self.assertIn("summary", j["pages"][0])
        self.assertTrue(rs.call_args.kwargs["double"])

    def test_busy_429(self):
        out, _ = self._call(b"\xff\xd8", "a.jpg", RuntimeError("busy"))
        self.assertEqual(out[1], 429)

    def test_ai_failed_page_marked(self):
        out, _ = self._call(b"\xff\xd8", "a.jpg", lambda *a, **k: None, double="0")
        self.assertTrue(out.get_json()["pages"][0]["failed"])

    def test_pdf_rendered_per_page(self):
        from hd_module import lab_sheet_reader as lsr2
        res = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        with mock.patch.object(lsr2, "pdf_pages_to_jpeg", return_value=[b"p1", b"p2"]):
            out, rs = self._call(b"%PDF-1.4 ...", "scan.pdf", lambda *a, **k: res)
        self.assertEqual(len(out.get_json()["pages"]), 2)
        self.assertEqual(rs.call_count, 2)
