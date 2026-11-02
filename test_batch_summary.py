"""ชุดหลายรูป → สรุปผลข้อความเดียว (2026-11-02 แพทย์อนุมัติ)

ผู้ส่งได้ 1 ข้อความสรุป · ผู้รับแจ้งเตือนได้ 1 push (ข้อความ + carousel)
ใบที่ล้มเหลว/รอคำตอบ ต้องปรากฏในสรุป · ถ้าใบใดไม่รายงาน ตัวจับเวลาส่ง
สรุปเท่าที่มี · รูปเดียว (ไม่มี batch_id) ไม่เปลี่ยน

ข้อมูลทั้งหมดเป็นค่าสังเคราะห์ · import server แบบ lazy
"""
import unittest


def _server():
    import server as _s
    return _s


class BatchFixture(unittest.TestCase):

    def setUp(self):
        self.s = _server()
        self.sent_single = []     # (to, text)
        self.sent_multi = []      # (to, [msgs])
        self._orig = (
            self.s.push_message, self.s.push_messages,
            dict(self.s._alert_recipients), self.s.is_authorized_user,
        )
        self.s.push_message = lambda to, text: self.sent_single.append((to, text)) or True
        self.s.push_messages = lambda to, msgs: self.sent_multi.append((to, msgs)) or True
        self.s._alert_recipients.clear()
        self.s._alert_recipients["physician"] = ["Udoctor", "Ustaff"]
        self.s.is_authorized_user = lambda h: True
        self.s._batch_collectors.clear()

    def tearDown(self):
        (self.s.push_message, self.s.push_messages,
         recips, self.s.is_authorized_user) = self._orig
        self.s._alert_recipients.clear()
        self.s._alert_recipients.update(recips)
        for col in list(self.s._batch_collectors.values()):
            col["timer"].cancel()
        self.s._batch_collectors.clear()

    @staticmethod
    def _item(n, status="ok", **kw):
        d = {"n": n, "doc8": f"doc{n:05d}", "cat": "เอกสารฟอกเลือด",
             "emoji": "🩸", "storage": "05_DIALYSIS_RECORDS/2026-Q4/2026-11",
             "folder": "63203_สมมติ", "clinical": True, "status": status}
        d.update(kw)
        return d

    @staticmethod
    def _bubble(n):
        return {"type": "bubble", "body": {"type": "box", "layout": "vertical",
                                            "contents": [{"type": "text", "text": str(n)}]}}


class TestBatchSummary(BatchFixture):

    def test_three_pages_one_summary_to_sender_one_push_to_recipients(self):
        s = self.s
        s._batch_register("B1", 3, "Udoctor", owner_hash="h")
        for n in (2, 1, 3):   # เสร็จไม่เรียงลำดับ
            self.assertTrue(s._batch_record("B1", self._item(n), self._bubble(n)))
        # ผู้ส่ง: 1 ข้อความ
        self.assertEqual(len(self.sent_single), 1)
        to, text = self.sent_single[0]
        self.assertEqual(to, "Udoctor")
        self.assertIn("สรุปชุด 3 รูป — เสร็จ 3/3", text)
        self.assertIn("เข้าระบบแล้ว 3 ใบ", text)
        self.assertLess(text.index(" 1. "), text.index(" 2. "))   # เรียงตามหน้า
        self.assertIn("63203_สมมติ", text)
        # ผู้รับแจ้ง: ไม่ส่งซ้ำหาผู้ส่ง (Udoctor) → เฉพาะ Ustaff, push เดียว
        self.assertEqual([t for t, _ in self.sent_multi], ["Ustaff"])
        msgs = self.sent_multi[0][1]
        self.assertEqual(msgs[0]["type"], "text")
        self.assertIn("จากเจ้าหน้าที่", msgs[0]["text"])
        self.assertEqual(msgs[1]["contents"]["type"], "carousel")
        self.assertEqual(len(msgs[1]["contents"]["contents"]), 3)
        # collector ถูกล้าง — บันทึกซ้ำไม่ได้แล้ว
        self.assertFalse(s._batch_record("B1", self._item(9)))

    def test_error_and_ask_items_listed(self):
        s = self.s
        s._batch_register("B2", 3, "Udoctor")
        s._batch_record("B2", self._item(1))
        s._batch_record("B2", self._item(2, status="ask", folder=""))
        s._batch_record("B2", self._item(3, status="error", reason="ประมวลผลล้มเหลว (X)"))
        text = self.sent_single[0][1]
        self.assertIn("เข้าระบบแล้ว 1 ใบ", text)
        self.assertIn("รอคำตอบประเภทเอกสาร 1 ใบ", text)
        self.assertIn("ล้มเหลว 1 ใบ", text)
        self.assertIn("doc00003", text)

    def test_unidentified_patient_hint(self):
        s = self.s
        s._batch_register("B3", 1, "Udoctor")
        s._batch_record("B3", self._item(1, folder=""))
        self.assertIn("ยังไม่ระบุผู้ป่วย", self.sent_single[0][1])

    def test_timer_sends_partial_summary(self):
        s = self.s
        s._batch_register("B4", 4, "Udoctor")
        s._batch_record("B4", self._item(1))
        s._batch_record("B4", self._item(2))
        self.assertEqual(self.sent_single, [])          # ยังไม่ครบ
        s._batch_flush("B4", True)                      # จำลองหมดเวลา
        text = self.sent_single[0][1]
        self.assertIn("เสร็จ 2/4", text)
        self.assertIn("ยังประมวลผลไม่เสร็จ", text)
        s._batch_flush("B4", True)                      # ซ้ำ → ไม่ส่งอีก
        self.assertEqual(len(self.sent_single), 1)

    def test_no_collector_means_legacy_path(self):
        self.assertFalse(self.s._batch_record("", self._item(1)))
        self.assertFalse(self.s._batch_record("UNKNOWN", self._item(1)))
        self.assertEqual(self.sent_single, [])

    def test_carousel_capped_at_ten(self):
        s = self.s
        s._batch_register("B5", 10, "Uother")
        for n in range(1, 11):
            s._batch_record("B5", self._item(n), self._bubble(n))
        msgs = [m for t, m in self.sent_multi if t == "Udoctor"][0]
        self.assertEqual(len(msgs[1]["contents"]["contents"]), 10)


class TestEndToEndTwoImageBatch(BatchFixture):
    """ขับ process_file_async จริง 2 ใบ (batch_id เดียว, ไม่มี reply_token)
    → ผู้ส่งต้องได้ข้อความสรุป 1 ข้อความ ไม่มี push รายใบหลุดออกมา"""

    def test_two_text_docs_in_batch(self):
        import os
        import threading
        s = self.s
        orig_copy, orig_add = s.copy_to_category, s.doc_index.add
        s.copy_to_category = (
            lambda src, cat, metadata=None, **kw:
            f"gs://test-bucket/{s.FOLDERS.get(cat, s.FOLDERS['review'])}/"
            f"{kw.get('subfolder') or ''}/x.txt"
        )
        s.doc_index.add = lambda meta: True
        try:
            s._batch_register("E2E", 2, "Udoctor", owner_hash="h")
            paths = []
            for n in (1, 2):
                fp = os.path.join(s.BASE_DIR, "00_INBOX", f"e2e_{n}.txt")
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, "w", encoding="utf-8") as f:
                    f.write("ใบเสร็จรับเงิน บริษัท นิคคิโซ เมดิคัล (ประเทศไทย) จำกัด "
                            "เลขประจำตัวผู้เสียภาษี 0105537100222 รวมเงิน 3,750.00 บาท")
                paths.append(fp)
                s.process_file_async(
                    fp, None, doc_id=f"e2edoc{n}000000", owner_hash="h",
                    line_user_id="Udoctor", batch_id="E2E", batch_index=n,
                )
            for t in threading.enumerate():
                if t.daemon and t is not threading.current_thread():
                    t.join(timeout=20)
        finally:
            s.copy_to_category, s.doc_index.add = orig_copy, orig_add
        texts = [t for to, t in self.sent_single if to == "Udoctor"]
        summaries = [t for t in texts if "สรุปชุด 2 รูป" in t]
        self.assertEqual(len(summaries), 1, texts)
        self.assertIn("เสร็จ 2/2", summaries[0])
        self.assertIn("เข้าระบบแล้ว 2 ใบ", summaries[0])
        self.assertNotIn("ล้มเหลว", summaries[0])
        self.assertIn("NIKKISO", summaries[0])          # บิลระบุบริษัทได้
        # ผู้รับแจ้ง (Ustaff) ได้ push เดียว: สรุป + carousel 2 ใบ
        staff = [m for to, m in self.sent_multi if to == "Ustaff"]
        self.assertEqual(len(staff), 1)
        self.assertIn("สรุปชุด 2 รูป", staff[0][0]["text"])
        # (carousel มีเฉพาะเมื่อสร้างลิงก์เปิดเอกสารได้ — local storage ไม่มี GCS URL)
        self.assertNotIn("รับเอกสารแล้ว (TXT)", "\n".join(texts))   # ไม่มี push รายใบ
        self.assertEqual(self.s._batch_collectors, {})


class TestRateLimitDefaults(unittest.TestCase):
    """เพดานใหม่ (แพทย์อนุมัติ 2026-11-02): ส่งชุด 8 รูปต้องไม่ถูกปฏิเสธ"""

    def test_defaults_allow_eight_images_per_minute(self):
        s = _server()
        self.assertGreaterEqual(s.RATE_LIMIT_PER_MINUTE, 8)
        self.assertGreaterEqual(s.RATE_LIMIT_PER_HOUR, 100)
        self.assertGreaterEqual(s.RATE_LIMIT_PER_DAY, 300)


if __name__ == "__main__":
    unittest.main()
