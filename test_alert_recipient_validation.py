"""ผู้รับการแจ้งเตือนค่าวิกฤต — ต้องรู้ทันทีว่าส่งถึงใครได้หรือไม่

ที่มา (ตรวจ production 26 ต.ค. 2026)
    log ตอน startup: "ALERT_RECIPIENTS and PHYSICIAN_USERS not configured"
    ผล: alert ค่าวิกฤตทุกใบถูกตีตรา delivery_failed แบบเงียบ ๆ และ
    ไม่มีทางรู้เลยนอกจากไปอ่าน log ซึ่งไม่มีใครอ่าน

กับดักที่ซ้อนอยู่: build_alert_recipients_from_env fallback ไปใช้
PHYSICIAN_USERS เมื่อไม่ได้ตั้ง ALERT_RECIPIENTS แต่ PHYSICIAN_USERS
เก็บ "SHA-256 hash" ของ LINE userId (ตั้งใจให้เป็น hash เพื่อไม่เก็บ
ID ดิบไว้ตรวจสิทธิ์) ขณะที่ LINE push API ต้องใช้ ID ดิบ
การตั้งแต่ PHYSICIAN_USERS จึงดูเหมือนตั้งค่าแล้ว แต่ push ไม่ได้เลย
— อาการเหมือนกับไม่ได้ตั้งค่า แต่ log บอกว่า "configured"

เทสต์ชุดนี้ล็อกไว้ว่า
    ค่าที่ไม่ใช่รูปแบบ LINE ID ต้องถูกคัดออก ไม่ส่งเข้า push
    /health ต้องบอกได้ว่าส่งถึงใครได้หรือไม่ และติดที่อะไร
    log และ /health ต้องไม่มี LINE user ID หลุดออกมา

ID ทั้งหมดในไฟล์นี้เป็นค่าสังเคราะห์
"""
import io
import logging
import os
import unittest

import alert_delivery as ad

_GOOD_A = "U" + "a" * 32
_GOOD_B = "U" + "b" * 32
_GROUP = "C" + "c" * 32
_HASH = "e" * 64          # รูปแบบเดียวกับ PHYSICIAN_USERS


class _EnvFixture(unittest.TestCase):

    def setUp(self):
        self._prev = {
            k: os.environ.get(k)
            for k in ("ALERT_RECIPIENTS", "PHYSICIAN_USERS")
        }
        for k in self._prev:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set(self, **env):
        for k, v in env.items():
            os.environ[k] = v


class TestRecipientFiltering(_EnvFixture):

    def test_id_ที่ถูกต้องผ่านครบ(self):
        self._set(ALERT_RECIPIENTS=f"{_GOOD_A},{_GOOD_B}")
        got = ad.build_alert_recipients_from_env()
        self.assertEqual(got.get("physician"), [_GOOD_A, _GOOD_B])

    def test_รับ_group_id_ด้วย(self):
        """LINE push ส่งเข้ากลุ่มได้ — คลินิกอาจใช้กลุ่มพยาบาล"""
        self._set(ALERT_RECIPIENTS=_GROUP)
        got = ad.build_alert_recipients_from_env()
        self.assertEqual(got.get("physician"), [_GROUP])

    def test_hash_ต้องถูกคัดออก(self):
        """แกนหลัก: hash จาก PHYSICIAN_USERS push ไม่ได้

        ถ้าไม่คัดออก ระบบจะคิดว่ามีผู้รับแล้ว แต่ push ล้มทุกครั้ง
        """
        self._set(PHYSICIAN_USERS=_HASH)
        got = ad.build_alert_recipients_from_env()
        self.assertEqual(got, {})

    def test_ปนกันต้องเหลือเฉพาะที่ใช้ได้(self):
        self._set(ALERT_RECIPIENTS=f"{_GOOD_A},{_HASH},notanid")
        got = ad.build_alert_recipients_from_env()
        self.assertEqual(got.get("physician"), [_GOOD_A])

    def test_ไม่ตั้งค่าเลยต้องได้ว่าง(self):
        self.assertEqual(ad.build_alert_recipients_from_env(), {})

    def test_fallback_ไป_physician_users_ยังใช้ได้ถ้าเป็น_id_ดิบ(self):
        """ถ้ามีใครตั้ง PHYSICIAN_USERS เป็น ID ดิบ ต้องยังใช้ได้"""
        self._set(PHYSICIAN_USERS=_GOOD_A)
        got = ad.build_alert_recipients_from_env()
        self.assertEqual(got.get("physician"), [_GOOD_A])


class TestHealthDescription(_EnvFixture):

    def test_ตั้งค่าถูกต้องรายงานว่าส่งได้(self):
        self._set(ALERT_RECIPIENTS=f"{_GOOD_A},{_GOOD_B}")
        d = ad.describe_alert_recipients()
        self.assertTrue(d["can_deliver"])
        self.assertEqual(d["recipients"], 2)
        self.assertEqual(d["problem"], "")
        self.assertEqual(d["source"], "ALERT_RECIPIENTS")

    def test_ไม่ตั้งค่ารายงานว่าส่งไม่ได้พร้อมสาเหตุ(self):
        d = ad.describe_alert_recipients()
        self.assertFalse(d["can_deliver"])
        self.assertEqual(d["recipients"], 0)
        self.assertIn("ALERT_RECIPIENTS", d["problem"])

    def test_ตั้งเป็น_hash_รายงานสาเหตุที่ชี้ตรงจุด(self):
        """สาเหตุต้องบอกว่าต้องใช้ ID ดิบ ไม่ใช่ hash"""
        self._set(PHYSICIAN_USERS=_HASH)
        d = ad.describe_alert_recipients()
        self.assertFalse(d["can_deliver"])
        self.assertEqual(d["configured_values"], 1)
        self.assertIn("hash", d["problem"])

    def test_ปนกันรายงานจำนวนที่ใช้ได้(self):
        self._set(ALERT_RECIPIENTS=f"{_GOOD_A},{_HASH}")
        d = ad.describe_alert_recipients()
        self.assertTrue(d["can_deliver"])
        self.assertEqual(d["recipients"], 1)
        self.assertEqual(d["configured_values"], 2)
        self.assertIn("1", d["problem"])


class TestNoIdLeak(_EnvFixture):
    """LINE user ID ต้องไม่หลุดลง log หรือ /health"""

    def test_log_ไม่มี_id(self):
        self._set(ALERT_RECIPIENTS=f"{_GOOD_A},{_HASH}")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        ad.logger.addHandler(handler)
        prev_level = ad.logger.level
        ad.logger.setLevel(logging.DEBUG)
        try:
            ad.build_alert_recipients_from_env()
        finally:
            ad.logger.removeHandler(handler)
            ad.logger.setLevel(prev_level)

        out = stream.getvalue()
        self.assertNotIn(_GOOD_A, out, "LINE user ID หลุดลง log")
        self.assertNotIn(_HASH, out, "hash หลุดลง log")
        # ต้องยังบอกจำนวนได้
        self.assertIn("1", out)

    def test_health_ไม่มี_id(self):
        self._set(ALERT_RECIPIENTS=f"{_GOOD_A},{_GROUP}")
        blob = repr(ad.describe_alert_recipients())
        for ident in (_GOOD_A, _GROUP):
            self.assertNotIn(ident, blob)


class TestHealthEndpointExposesIt(_EnvFixture):
    """/health ต้องรายงานความพร้อมนี้จริง ไม่ใช่มีแต่ฟังก์ชัน"""

    def test_health_มีฟิลด์ความพร้อมการแจ้งเตือน(self):
        os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-dummy")
        os.environ.setdefault("LINE_CHANNEL_SECRET", "test-dummy")
        os.environ.setdefault("GCS_BUCKET_NAME", "test-bucket")
        import server
        data = server.app.test_client().get("/health").get_json()
        self.assertIn("critical_alert_delivery", data)
        self.assertIn("can_deliver", data["critical_alert_delivery"])
        self.assertIn("alert_push_configured", data)


if __name__ == "__main__":
    unittest.main()
