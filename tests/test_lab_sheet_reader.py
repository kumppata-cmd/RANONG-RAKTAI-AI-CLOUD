"""📄 Lab Sheet Reader (26 พ.ย. 2569) — ฟอร์มใบแล็บ รพ.ระนอง · ตัวอย่างเลียนแบบใบจริง (HN/ชื่อสมมติ)"""
import json
import unittest
from unittest import mock

from hd_module import lab_sheet_reader as lsr


from datetime import date as _date
TODAY_STR = _date.today().strftime("%d/%m/%Y")   # ฟิกซ์เจอร์ใช้วันนี้ กัน test พังเมื่อเวลาผ่าน (เช็ค "เก่ากว่า 2 ปี")


def _hdr(hn="00012345", name="นายทดสอบ ระบบ", form="Chemistry"):
    return {"hn": hn, "an": "691119080434", "name": name, "request_form": form, "request_by": "นพ.ก",
            "request_date": TODAY_STR, "request_time": "08:04:34", "request_number": "2979186",
            "visit_type": "OPD", "receive_date": TODAY_STR, "receive_time": "07:12:46"}


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

    def test_agreeing_value_with_low_conf_in_one_pass_is_accepted(self):
        """ผลจริง 26 พ.ย.: PTH/RDW/Cr/K ถูกรายงาน conf 0.6 เพราะ reference ถูกตัดขอบ แม้ค่าตรงกันทั้ง 2 รอบ"""
        p1 = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        raw2 = json.loads(json.dumps(RAW_CHEM)); raw2["blocks"][0]["rows"][1]["conf"] = 0.6     # PTH รอบ 2 ไม่มั่นใจ
        m = lsr.merge_passes(p1, lsr.analyze(raw2))
        self.assertEqual(m["values"]["pth"]["value"], 1227.0)
        self.assertTrue(m["values"]["pth"].get("agree_low_conf"))
        # ไม่มั่นใจทั้ง 2 รอบ แต่ค่าตรงกัน → ผ่าน
        raw1 = json.loads(json.dumps(RAW_CHEM)); raw1["blocks"][0]["rows"][1]["conf"] = 0.6
        m2 = lsr.merge_passes(lsr.analyze(raw1), lsr.analyze(raw2))
        self.assertEqual(m2["values"]["pth"]["value"], 1227.0)
        # ไม่มั่นใจรอบ 2 และค่าต่าง → ไม่แน่ใจ พร้อม alt
        raw3 = json.loads(json.dumps(RAW_CHEM)); raw3["blocks"][0]["rows"][1].update(conf=0.6, result="1277.0")
        m3 = lsr.merge_passes(p1, lsr.analyze(raw3))
        self.assertNotIn("pth", m3["values"])
        u = [x for x in m3["uncertain"] if x["key"] == "pth"][0]
        self.assertEqual(u["alt"], 1277.0)
        # ค่านอกช่วงของรอบแรกยังคงอยู่ใน uncertain
        raw4 = json.loads(json.dumps(RAW_CHEM)); raw4["blocks"][0]["rows"][1]["result"] = "12270"
        m4 = lsr.merge_passes(lsr.analyze(raw4), lsr.analyze(raw4))
        self.assertTrue(any(x["key"] == "pth" and "นอกช่วง" in x["why"] for x in m4["uncertain"]))

    def test_header_date_mismatch_between_passes(self):
        """ผลจริง 26 พ.ย. ใบ 444: ปี 2026 ถูกอ่านเป็น 2025 ในรอบหนึ่ง"""
        p1 = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        raw2 = json.loads(json.dumps(RAW_CHEM)); raw2["blocks"][0]["header"]["request_date"] = "19/11/2025"
        m = lsr.merge_passes(p1, lsr.analyze(raw2))
        self.assertTrue(any("วันที่ขอตรวจ 2 รอบอ่านไม่ตรง" in p for p in m["problems"]))
        self.assertEqual(m["header"]["request_date_alt"], "19/11/2025")

    def test_sheet_date_parse_and_sanity(self):
        from datetime import date, timedelta
        self.assertEqual(lsr.parse_sheet_date("19/11/2026"), date(2026, 11, 19))
        self.assertEqual(lsr.parse_sheet_date("19/11/2569"), date(2026, 11, 19))
        self.assertIsNone(lsr.parse_sheet_date("xx"))
        raw = json.loads(json.dumps(RAW_CHEM))
        raw["blocks"][0]["header"]["request_date"] = (date.today() + timedelta(days=40)).strftime("%d/%m/%Y")
        self.assertTrue(any("อนาคต" in p for p in lsr.analyze(raw)["problems"]))
        raw["blocks"][0]["header"]["request_date"] = "01/01/2019"
        self.assertTrue(any("เก่ากว่า 2 ปี" in p for p in lsr.analyze(raw)["problems"]))
        r = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        self.assertEqual(r["header"]["lab_date"], _date.today().isoformat())

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

    def test_match_patient_by_hospital_hn(self):
        cur = mock.Mock()
        cur.fetchall.return_value = [(7, "67379", "นางยุพิน โพธิ์ยิ้ม", "00013373")]
        r = lsr.match_patient(cur, {"hn_norm": "13373", "hn": "00013373", "name": "น.ส.ยุพิน โพธิ์ยิ้ม"})
        self.assertEqual(r["status"], "matched"); self.assertEqual(r["hn"], "67379"); self.assertEqual(r["by"], "hospital_hn")
        self.assertEqual(lsr.match_patient(cur, {"hn_norm": "13373", "hn": "00013373", "name": "นางคนอื่น ไกล"})["status"], "name_mismatch")
        self.assertEqual(lsr.match_patient(cur, {"hn_norm": ""})["status"], "no_hn")

    def test_match_patient_by_name_when_hospital_hn_unknown(self):
        cur = mock.Mock()
        cur.fetchall.side_effect = [[], [(7, "67379", "นางยุพิน โพธิ์ยิ้ม", None), (8, "60001", "นายยุพล อื่น", None)]]
        r = lsr.match_patient(cur, {"hn_norm": "13373", "hn": "00013373", "name": "น.ส.ยุพิน โพธิ์ยิ้ม"})
        self.assertEqual(r["status"], "matched_by_name"); self.assertEqual(r["hn"], "67379"); self.assertEqual(r["sheet_hn"], "13373")
        cur.fetchall.side_effect = [[], []]
        self.assertEqual(lsr.match_patient(cur, {"hn_norm": "13373", "hn": "00013373", "name": "นายไม่มี ในทะเบียน"})["status"], "not_found")
        cur.fetchall.side_effect = [[], [(7, "1", "นายสมชาย ก", None), (8, "2", "นายสมชาย ข", None)]]
        self.assertEqual(lsr.match_patient(cur, {"hn_norm": "9", "hn": "9", "name": "นายสมชาย"})["status"], "ambiguous_name")


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


class TestStage2(unittest.TestCase):
    def test_form_payload_and_units(self):
        r = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        f = lsr.form_payload(r)
        self.assertEqual(f["ferritin"], 226.1); self.assertEqual(f["ipth"], 1227.0); self.assertEqual(f["iron"], 54.0)
        self.assertEqual(f["tibc"], 198.0); self.assertEqual(f["vitamin_d"], 46.17); self.assertEqual(f["tsat"], 27.27)
        self.assertEqual(f["hepatitis_b"], "negative"); self.assertEqual(f["anti_hbs"], "positive")
        self.assertEqual(f["anti_hbc"], "positive"); self.assertEqual(f["hiv"], "negative")
        # หน่วยมาตรฐานแทนหน่วยที่พิมพ์เหลื่อมบนใบ (ใบ 444: GFR มี mg/dl, Creatinine ไม่มี)
        raw = json.loads(json.dumps(RAW_ROUTINE_2BLOCK))
        raw["blocks"][1]["rows"][1]["unit"] = "mg/dl"; raw["blocks"][1]["rows"][2]["unit"] = ""
        r2 = lsr.analyze(raw)
        self.assertEqual(r2["values"]["gfr"]["unit"], "mL/min"); self.assertEqual(r2["values"]["creatinine"]["unit"], "mg/dl")
        self.assertEqual(r2["values"]["gfr"]["unit_sheet"], "mg/dl")

    def test_outcome_and_record_samples(self):
        self.assertEqual(lsr.outcome_of("ferritin", 226.1, 226), "match")
        self.assertEqual(lsr.outcome_of("ferritin", 226.1, 262), "mismatch")
        self.assertEqual(lsr.outcome_of("hepatitis_b", "negative", "Negative"), "match")
        self.assertEqual(lsr.outcome_of("tsat", 27.27, None), "blank")
        self.assertEqual(lsr.outcome_of("ipth", None, 1227, uncertain=True), "manual")
        self.assertEqual(lsr.outcome_of("ipth", None, None), "unused")
        cur = mock.Mock()
        st = lsr.record_samples(cur, "LSx-1", {"ferritin": 226.1, "iron": 54, "tsat": 27.27, "hepatitis_b": "negative"},
                                ["ipth"], {"ferritin": 226.1, "iron": 45, "tsat": None, "hepatitis_b": "negative", "ipth": 1227}, nurse_id=3)
        self.assertEqual(st, {"match": 2, "mismatch": 1, "blank": 1, "manual": 1})
        self.assertEqual(cur.execute.call_count, 5)
        args = [c.args[1] for c in cur.execute.call_args_list]
        self.assertTrue(any(a[1] == "iron" and a[5] == "mismatch" for a in args))
        self.assertTrue(any(a[1] == "ipth" and a[3] is True and a[5] == "manual" for a in args))

    def test_accuracy_report(self):
        cur = mock.Mock()
        cur.fetchall.return_value = [("ferritin", "match", 10), ("ferritin", "mismatch", 1), ("tsat", "blank", 2)]
        cur.fetchone.return_value = (12,)
        rep = lsr.accuracy_report(cur, 30)
        self.assertEqual(rep["fields"]["ferritin"]["match"], 10)
        self.assertEqual(rep["total"]["mismatch"], 1)
        self.assertFalse(rep["gate_ok"])
        cur.fetchall.return_value = [("ferritin", "match", 60)]; cur.fetchone.return_value = (55,)
        self.assertTrue(lsr.accuracy_report(cur, 30)["gate_ok"])

    def test_read_endpoint_includes_form(self):
        import io
        from flask import Flask
        from hd_module import blueprint as bp
        res = lsr.analyze(json.loads(json.dumps(RAW_CHEM)))
        app = Flask(__name__)
        with app.test_request_context(method="POST", data={"file": (io.BytesIO(b"\xff\xd8"), "a.jpg")}, content_type="multipart/form-data"), \
             mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k", "HD_DATABASE_URL": ""}), \
             mock.patch.object(lsr, "read_sheet", return_value=res), mock.patch.object(bp, "log_action"), \
             mock.patch("hd_module.patient_engage.ai_model", return_value="m"):
            j = bp.lab_sheet_read.__wrapped__().get_json()
        pg = j["pages"][0]
        self.assertEqual(pg["form"]["ferritin"], 226.1)
        self.assertEqual(pg["form_uncertain"], [])
        self.assertTrue(pg["read_id"].startswith("LS") and pg["read_id"].endswith("-1"))
        self.assertEqual(j["read_id"], pg["read_id"][:-2])

    def test_save_lab_with_sheet_meta(self):
        """POST labs: คอลัมน์ใหม่เข้าเฉพาะเมื่อส่งมา · sheet meta → samples + hospital_hn confirm"""
        import sys
        from flask import Flask
        from hd_module import blueprint as bp
        fake_pg = mock.MagicMock()
        cur = fake_pg.connect.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [(7,), (99,)]
        cur.rowcount = 1
        app = Flask(__name__)
        body = {"lab_date": "2026-11-26", "ferritin": 226.1, "vitamin_d": 46.17, "anti_hbs": "positive",
                "sheet_read_id": "LSx-1", "sheet_ai": {"ferritin": 226.1, "vitamin_d": 46.17}, "sheet_uncertain": [],
                "confirm_hospital_hn": "00012345"}
        with app.test_request_context(json=body), mock.patch.dict(sys.modules, {"psycopg2": fake_pg}), \
             mock.patch.dict("os.environ", {"HD_DATABASE_URL": "postgresql://x"}), mock.patch.object(bp, "log_action"):
            out = bp.save_lab_result.__wrapped__("67379")
        resp = out[0]; self.assertEqual(out[1], 201)
        j = resp.get_json(); self.assertTrue(j["saved"]); self.assertEqual(j["sheet_stats"], {"match": 2, "mismatch": 0, "blank": 0, "manual": 0})
        sqls = [str(c.args[0]) for c in cur.execute.call_args_list]
        ins = [q for q in sqls if "INSERT INTO hd_lab_results" in q][0]
        self.assertIn("vitamin_d", ins); self.assertIn("anti_hbs", ins); self.assertIn("sheet_read_id", ins); self.assertNotIn("anti_hbc", ins)
        self.assertTrue(any("hd_lab_sheet_samples" in q for q in sqls))
        self.assertTrue(any("SET hospital_hn" in q for q in sqls))
        # ฟอร์มเก่า (ไม่มี sheet meta) → ไม่มีคอลัมน์ใหม่ ไม่แตะ samples/hospital_hn
        cur.reset_mock(); cur.fetchone.side_effect = [(7,), (100,)]
        with app.test_request_context(json={"lab_date": "2026-11-26", "ferritin": 200}), mock.patch.dict(sys.modules, {"psycopg2": fake_pg}), \
             mock.patch.dict("os.environ", {"HD_DATABASE_URL": "postgresql://x"}), mock.patch.object(bp, "log_action"):
            out = bp.save_lab_result.__wrapped__("67379")
        sqls = [str(c.args[0]) for c in cur.execute.call_args_list]
        ins = [q for q in sqls if "INSERT INTO hd_lab_results" in q][0]
        self.assertNotIn("vitamin_d", ins); self.assertFalse(any("hd_lab_sheet_samples" in q or "SET hospital_hn" in q for q in sqls))
