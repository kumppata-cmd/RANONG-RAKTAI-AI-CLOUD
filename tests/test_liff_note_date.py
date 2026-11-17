"""คำแนะนำแพทย์ในหน้า LIFF ต้องมีวันที่อ่านออก (17 พ.ย. 2569: เดิมขึ้น '20261117_1')"""
import unittest

import liff_app


class NoteDateTests(unittest.TestCase):
    def test_utc_stamp_to_thai(self):
        self.assertEqual(liff_app._thai_note_date("20261117_071500"), "17 พ.ย. 2569 · 14:15 น.")

    def test_crosses_midnight(self):
        self.assertEqual(liff_app._thai_note_date("20261231_200000"), "1 ม.ค. 2570 · 03:00 น.")

    def test_plain_date_and_empty(self):
        self.assertEqual(liff_app._thai_note_date("2026-11-03"), "3 พ.ย. 2569")
        self.assertEqual(liff_app._thai_note_date(""), "")

    def test_page_has_zoom_viewer_and_advice(self):
        h = liff_app.get_liff_html("")
        for k in ("function zoomBy", "closeViewer()", "var ADVICE", "noteDateLine", "_esc(doc.doctor_note)"):
            self.assertIn(k, h)
        # ห้ามแตะตรงไหนก็ปิดรูป (ทำให้ซูมไม่ได้)
        self.assertNotIn("onclick=\"this.style.display='none'\"", h)
