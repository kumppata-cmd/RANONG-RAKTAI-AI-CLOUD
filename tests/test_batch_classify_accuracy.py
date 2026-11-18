"""ชุดหลายรูปจัดประเภทใบฟอกเลือด/ใบยาสลับกัน (18 พ.ย. 2026, HN 68408)

ส่ง 4 รูป = ใบฟอกเลือด 3 + ใบยา 1 → ระบบสรุป ใบยา ×2 · ฟอกเลือด ×2

สาเหตุ:
  1. ชุดหลายรูปใช้ OCR คนละเส้นทางกับรูปเดี่ยว (Tesseract รอบเดียว ไม่มี Google
     Vision ไม่หมุนรูป) → ข้อความเพี้ยน
  2. ใบฟอกเลือดที่ถ่ายไม่ติดหัวกระดาษ เหลือแต่ส่วน Hemodialysis Prescription +
     Medication/Route/Heparin → pre-check ใบสั่งยา HD จับเป็นใบยา
แก้:
  - ชุดหลายรูปใช้ OCR เดียวกับรูปเดี่ยว (_ocr_batch_page)
  - เครื่องหมายโครงสร้างใบฟอกเลือด (Pre BW/Post BW/Target UF/…) + จับคำสะกดเพี้ยน
  - ธง "ไม่แน่ใจ" + สรุปชุดบอกรหัสตามลำดับรูป + ปุ่มแก้ประเภทกดครั้งเดียว
"""
import os
import sys
import types
import unittest
from unittest import mock

os.environ.setdefault("APP_ENV", "testing")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402
from test_classify_hd_worksheet import WORKSHEET, HD_RX_ORDER  # noqa: E402

_L = WORKSHEET.splitlines()
# ส่วนบนของใบฟอกเลือด ถ่ายไม่ติดหัวกระดาษ "Hemodialysis work sheet"
WS_UPPER_NO_TITLE = "\n".join(_L[3:10])
# OCR จากรูปเบลอ — สะกดเพี้ยน เหลือคำใบยาให้เห็น
WS_TYPO = ("Nursing diagnosls Nursing care pIan Expected outcorne\n"
           "Heparin Loading 2500 Medication EPO Route IV SC\nBFR 300 Dialyzer FX80")
CLINIC_HD_ORDER = (
    "Hemodialysis Order หน่วยไตเทียม ระนอง รักษ์ไต\n"
    "Order for one day  Order for continuation\n"
    "Dialyzer FX80 reuse Dry weight 55 kg BFR 300 DFR 500\n"
    "Dialysate Na 138 HCO3 35 K 2 Ca 2.5\nHeparin loading 2000 maintenance 1000\n"
    "Drug Dose Route\nEpiao 4000 u SC Venofer 100 mg IV Calcitriol 0.25 mcg oral"
)
OPD_NOTE = (
    "ใบตรวจรักษาผู้ป่วย Diag text CKD stage 5 on HD Visit note JVP engorged "
    "crepitation both lungs AVF good thrill bruit dyspnea orthopnea\n"
    "Losartan 50 mg 1x1 Amlodipine 10 mg"
)


def _cls(text):
    return server.classify_with_keywords("x.jpg", override_text=text)


class ClassifierTests(unittest.TestCase):
    def test_worksheet_without_title_is_dialysis(self):
        r = _cls(WS_UPPER_NO_TITLE)
        self.assertEqual(r["category"], "dialysis", r["reason"])
        self.assertIn("work sheet", r["reason"])

    def test_worksheet_ocr_typos_is_dialysis(self):
        r = _cls(WS_TYPO)
        self.assertEqual(r["category"], "dialysis", r["reason"])

    def test_full_worksheet_still_dialysis_not_flagged(self):
        r = _cls(WORKSHEET)
        self.assertEqual(r["category"], "dialysis")
        self.assertFalse(r.get("ambiguous_with"))

    def test_hd_order_forms_stay_rx(self):
        for txt in (HD_RX_ORDER, CLINIC_HD_ORDER):
            r = _cls(txt)
            self.assertEqual(r["category"], "rx", r["reason"])

    def test_opd_note_with_exam_words_stays_rx(self):
        # คำตรวจร่างกาย (JVP/crepitation/thrill/bruit) ห้ามดันใบตรวจรักษาเป็นใบฟอกเลือด
        r = _cls(OPD_NOTE)
        self.assertEqual(r["category"], "rx", r["reason"])

    def test_order_form_with_few_worksheet_words_is_rx_but_flagged(self):
        r = _cls(HD_RX_ORDER + "\nTarget UF 3000 Pre BW 55")
        self.assertEqual(r["category"], "rx", r["reason"])
        self.assertEqual(r.get("ambiguous_with"), "dialysis")

    def test_weak_mixed_evidence_is_flagged(self):
        r = _cls("Pre BW 56.2 Post BW 54.0\nHeparin 2000 Medication EPO 4000 Route IV SC\nDialyzer FX80")
        self.assertEqual(r["category"], "dialysis", r["reason"])
        self.assertEqual(r.get("ambiguous_with"), "rx")
        r2 = _cls("Pre BW 56.2 Post BW 54.0\nHeparin 2000 Medication EPO 4000 Route IV SC")
        self.assertIn(r2["category"], ("rx", "dialysis"))
        self.assertTrue(r2.get("ambiguous_with"), r2)

    def test_lab_and_bank_unaffected(self):
        self.assertEqual(_cls("ใบรายงานผลการตรวจ Laboratory BUN 60 mg/dL Creatinine 8.2 mg/dL "
                              "Potassium 5.1 mmol/L")["category"], "lab")
        self.assertEqual(_cls("โอนเงินสำเร็จ พร้อมเพย์ กสิกร 1,000 บาท")["category"], "bills")


class FuzzyMarkTests(unittest.TestCase):
    def test_catches_small_ocr_typos(self):
        hits = server._fuzzy_mark_hits(["nursing diagnosis", "nursing care plan"],
                                       "x nursing diagnosls y nursing care pian z")
        self.assertEqual(sorted(hits), ["nursing care plan", "nursing diagnosis"])

    def test_no_double_count_of_spelling_variants(self):
        marks = ["pre-dialysis assesment", "pre-dialysis assessment"]
        hits = server._fuzzy_mark_hits(marks, "pre-dialysis assesment ok",
                                       {"pre-dialysis assesment"})
        self.assertEqual(hits, [])

    def test_short_marks_never_fuzzy(self):
        self.assertEqual(server._fuzzy_mark_hits(["tmp", "ufr", "hd no"], "tnp ufp hd na"), [])

    def test_unrelated_text_no_hits(self):
        self.assertEqual(server._fuzzy_mark_hits(
            ["nursing diagnosis", "technical complication"],
            "losartan 50 mg oral once daily after meal"), [])


class BatchOcrParityTests(unittest.TestCase):
    def test_batch_page_uses_same_ocr_as_single_image(self):
        seen = {}

        def fake_ocr(path, *a, **k):
            seen["path"] = path
            seen["exists"] = os.path.exists(path)
            return "Hemodialysis work sheet"
        with mock.patch.object(server, "extract_text_with_ocr", side_effect=fake_ocr):
            out = server._ocr_batch_page(b"\xff\xd8fake", ".jpg")
        self.assertEqual(out, "Hemodialysis work sheet")
        self.assertTrue(seen["path"].endswith(".jpg"))
        self.assertTrue(seen["exists"])
        self.assertFalse(os.path.exists(seen["path"]), "ต้องลบไฟล์ชั่วคราว")

    def test_falls_back_to_old_ocr_when_empty(self):
        with mock.patch.object(server, "extract_text_with_ocr", return_value=""), \
             mock.patch.object(server, "HAS_TESSERACT", True), \
             mock.patch.object(server, "HAS_PIL", True), \
             mock.patch.object(server, "_ocr_from_bytes", return_value="old text") as old:
            self.assertEqual(server._ocr_batch_page(b"x", ".png"), "old text")
            old.assert_called_once()

    def test_exception_does_not_crash_batch(self):
        with mock.patch.object(server, "extract_text_with_ocr", side_effect=RuntimeError("x")), \
             mock.patch.object(server, "HAS_TESSERACT", False):
            self.assertEqual(server._ocr_batch_page(b"x", ".jpg"), "")

    def test_multipage_flush_uses_parity_helper(self):
        import inspect
        src = inspect.getsource(server._multipage_flush)
        self.assertIn("_ocr_batch_page(page_data", src)
        self.assertNotIn("_ocr_from_bytes(page_data", src)


def _col(items, owner_hash="ownerhash"):
    return {"items": items, "total": len(items), "bubbles": [],
            "line_user_id": "Usender", "owner_hash": owner_hash,
            "timer": types.SimpleNamespace(cancel=lambda: None)}


def _items():
    base = {"status": "ok", "clinical": True, "folder": "68408_นางสาวยุวรรณ"}
    return [
        dict(base, n=1, doc8="d3e80691", cat="เอกสารฟอกเลือด", emoji="🩸", category="dialysis", ambiguous=""),
        dict(base, n=2, doc8="b9d91a62", cat="ใบสั่งยา", emoji="💊", category="rx", ambiguous="dialysis"),
        dict(base, n=3, doc8="e597040a", cat="เอกสารฟอกเลือด", emoji="🩸", category="dialysis", ambiguous=""),
        dict(base, n=4, doc8="e70cf25a", cat="ใบสั่งยา", emoji="💊", category="rx", ambiguous=""),
    ]


class BatchSummaryTests(unittest.TestCase):
    def test_summary_maps_codes_to_image_order_and_flags_unsure(self):
        t = server._batch_summary_text(_col(_items()), False)
        self.assertIn("①🩸d3e80691 ②💊b9d91a62 ③🩸e597040a ④💊e70cf25a", t)
        self.assertIn("⚠️ ระบบไม่แน่ใจ", t)
        self.assertIn("②b9d91a62", t)
        self.assertIn("เปลี่ยนประเภท", t)

    def test_quick_fix_items(self):
        items = server._batch_fix_quick_items(_col(_items()))
        self.assertEqual(len(items), 4)
        # ใบที่ไม่แน่ใจขึ้นก่อน
        self.assertIn("b9d91a62", items[0]["action"]["text"])
        for it in items:
            self.assertLessEqual(len(it["action"]["label"]), 20, it)
            parsed = server._parse_change_category(it["action"]["text"])
            self.assertIsNotNone(parsed, it)
        by_code = {server._parse_change_category(i["action"]["text"])[0]:
                   server._parse_change_category(i["action"]["text"])[1] for i in items}
        self.assertEqual(by_code, {"d3e80691": "rx", "b9d91a62": "dialysis",
                                   "e597040a": "rx", "e70cf25a": "dialysis"})

    def test_quick_fix_label_limits_for_large_batch(self):
        base = {"status": "ok", "clinical": True, "folder": "f"}
        many = [dict(base, n=k, doc8=f"{k:08x}", cat="ใบสั่งยา", emoji="💊",
                     category="rx" if k % 2 else "dialysis", ambiguous="x" if k == 12 else "")
                for k in range(1, 21)]
        items = server._batch_fix_quick_items(_col(many))
        self.assertEqual(len(items), 13)
        for it in items:
            self.assertLessEqual(len(it["action"]["label"].encode("utf-16-le")) // 2, 20, it)

    def test_non_clinical_and_failed_items_have_no_fix_button(self):
        its = [{"status": "ok", "n": 1, "doc8": "aaaaaaaa", "category": "bills"},
               {"status": "error", "n": 2, "doc8": "bbbbbbbb", "category": "rx"},
               {"status": "ok", "n": 3, "doc8": "cccccccc", "category": "lab"}]
        self.assertEqual(server._batch_fix_quick_items(_col(its)), [])


class BatchFlushTests(unittest.TestCase):
    def setUp(self):
        self.pushed = []
        self.p1 = mock.patch.object(server, "push_message",
                                    side_effect=lambda uid, t: self.pushed.append(("text", uid, t)) or True)
        self.p2 = mock.patch.object(server, "push_messages",
                                    side_effect=self._pm)
        self.p3 = mock.patch.object(server, "_alert_recipients", {"physician": ["Urecip"]})
        self.fail_quick = False
        for p in (self.p1, self.p2, self.p3):
            p.start()
            self.addCleanup(p.stop)

    def _pm(self, uid, msgs):
        has_qr = any("quickReply" in m for m in msgs)
        if has_qr and self.fail_quick:
            return False
        self.pushed.append(("msgs", uid, msgs))
        return True

    def _flush(self, owner_hash):
        server._batch_collectors["B1"] = _col(_items(), owner_hash=owner_hash)
        server._batch_flush("B1", False)

    def test_physician_sender_gets_fix_buttons(self):
        with mock.patch.object(server, "PHYSICIAN_USERS", {"dochash"}):
            self._flush("dochash")
        sender = [p for p in self.pushed if p[1] == "Usender"]
        self.assertEqual(len(sender), 1)
        self.assertEqual(sender[0][0], "msgs")
        self.assertIn("quickReply", sender[0][2][0])

    def test_non_physician_sender_plain_text(self):
        with mock.patch.object(server, "PHYSICIAN_USERS", set()), \
             mock.patch.object(server, "ADMIN_USERS", set()):
            self._flush("nursehash")
        sender = [p for p in self.pushed if p[1] == "Usender"]
        self.assertEqual(sender[0][0], "text")

    def test_recipient_gets_buttons_when_allowed_and_fallback_when_rejected(self):
        rh = server.hash_user_id("Urecip")
        with mock.patch.object(server, "PHYSICIAN_USERS", {rh}):
            self._flush("x")
        rec = [p for p in self.pushed if p[1] == "Urecip"]
        self.assertEqual(len(rec), 1)
        self.assertIn("quickReply", rec[0][2][-1])
        # LINE ปฏิเสธข้อความที่มีปุ่ม → ต้องส่งสรุปแบบเดิมให้ถึงอยู่ดี
        self.pushed.clear()
        self.fail_quick = True
        with mock.patch.object(server, "PHYSICIAN_USERS", {rh}):
            self._flush("x")
        rec = [p for p in self.pushed if p[1] == "Urecip"]
        self.assertEqual(len(rec), 1)
        self.assertNotIn("quickReply", rec[0][2][-1])


class BubbleTests(unittest.TestCase):
    def _bubble(self, cat, **kw):
        with mock.patch.object(server, "get_download_url", return_value="https://x/y"):
            return server._build_doc_flex_bubble(cat, "ใบสั่งยา", "แฟ้ม", "b9d91a62ffff", "gs://b/p/f.jpg", **kw)

    def test_fix_button_and_number(self):
        b = self._bubble("rx", n=2, ambiguous="dialysis")
        btns = [c for c in b["footer"]["contents"] if c["action"]["type"] == "message"]
        self.assertEqual(len(btns), 1)
        self.assertLessEqual(len(btns[0]["action"]["label"].encode("utf-16-le")) // 2, 20)
        self.assertEqual(server._parse_change_category(btns[0]["action"]["text"]),
                         ("b9d91a62", "dialysis", ""))
        texts = [c.get("text", "") for c in b["body"]["contents"]]
        self.assertTrue(texts[0].startswith("②"))
        self.assertTrue(any("ไม่แน่ใจ" in t for t in texts))
        self.assertTrue(any("b9d91a62" in t for t in texts))

    def test_lab_and_bills_have_no_fix_button(self):
        for cat in ("lab", "bills"):
            b = self._bubble(cat)
            self.assertFalse([c for c in b["footer"]["contents"] if c["action"]["type"] == "message"])


if __name__ == "__main__":
    unittest.main()
