"""เพิ่มผู้รับการแจ้งเตือนได้โดยไม่เปิดเผย LINE user ID ของใคร

ปัญหาที่ระบบนี้แก้
──────────────────
`ALERT_RECIPIENTS` ใน Render ต้องใช้ LINE user ID ดิบ แต่คำสั่ง
ลงทะเบียนของระบบตั้งใจไว้ว่า "ไม่แสดง LINE userId จริง" (เขียนกำกับไว้
ในโค้ด เป็นมาตรการลดการเปิดเผยตัวระบุตัวตนตาม PDPA)

สองอย่างนี้ขัดกันตรง ๆ และเป็นเหตุที่การแจ้งเตือนค่าวิกฤตไปถึงแพทย์
คนเดียว เพิ่มพยาบาลไม่ได้เลย — ยืนยันจาก production 26 ต.ค. 2026

ทางออกที่เลือก: เก็บคู่ hash -> LINE ID ไว้ในระบบเอง ผู้ดูแลเพิ่มผู้รับ
ด้วย hash ที่เจ้าตัวได้รับตอนลงทะเบียน ระบบไปหา ID ดิบเองตอนส่ง
ผลคือไม่มีใครเห็น ID ดิบของใคร และ ID ไม่ต้องเดินทางผ่านแชท

เทสต์ชุดนี้ล็อกสิ่งที่พลาดไม่ได้
    LINE user ID ต้องไม่ปรากฏในผลลัพธ์ของ API ใดเลย
    เพิ่มผู้รับที่ยังไม่ลงทะเบียนต้องถูกปฏิเสธ (ไม่เพิ่มแล้วส่งไม่ได้เงียบ)
    การเพิ่ม/ลบต้องมีผลทันที ไม่ต้อง redeploy
    ผู้รับเดิมจาก ENV ต้องไม่หลุดหายเมื่อมีทะเบียน

ID ทั้งหมดในไฟล์นี้เป็นค่าสังเคราะห์
"""
import json
import os
import unittest

import alert_delivery as ad

# hash_user_id คืน SHA-256 ตัด 32 ตัวแรก ไม่ใช่ 64 ตัวเต็ม
_HASH_A = "a" * 32
_HASH_B = "b" * 32
_HASH_C = "c" * 32
_UID_A = "U" + "1" * 32
_UID_B = "U" + "2" * 32
_UID_ENV = "U" + "9" * 32


class FakeStorage:
    """storage จำลองเท่าที่โมดูลนี้ใช้ — put/get ด้วย bytes"""

    def __init__(self):
        self.objects = {}
        self.put_calls = []

    @property
    def is_ready(self):
        return True

    def put(self, path, data, content_type=None, create_only=False):
        self.put_calls.append(path)
        if create_only and path in self.objects:
            raise RuntimeError("exists")
        self.objects[path] = data
        return True

    def get(self, path):
        return self.objects.get(path)


class _Base(unittest.TestCase):

    def setUp(self):
        self.s = FakeStorage()
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

    def _enroll(self, user_hash, uid):
        return ad.store_enrolled_line_id(self.s, user_hash, uid)


# ─────────────────────────────────────────────────────────────
# การเก็บคู่ hash -> LINE ID
# ─────────────────────────────────────────────────────────────

class TestStoreEnrolledId(_Base):

    def test_เก็บได้และหากลับมาได้(self):
        self.assertTrue(self._enroll(_HASH_A, _UID_A))
        self.assertEqual(
            ad._lookup_enrolled_line_id(self.s, _HASH_A), _UID_A,
        )

    def test_ปฏิเสธ_id_ที่ไม่ใช่รูปแบบ_line(self):
        for bad in ("", "notanid", "e" * 32, "U123"):
            with self.subTest(uid=bad):
                self.assertFalse(self._enroll(_HASH_B, bad))
        self.assertEqual(ad._lookup_enrolled_line_id(self.s, _HASH_B), "")

    def test_ปฏิเสธ_hash_ที่ผิดรูปแบบ(self):
        """hash_user_id คืน hex 32 ตัว — ความยาวหรืออักขระอื่นต้องไม่ผ่าน"""
        for bad in ("abc", "a" * 64, "a" * 31, "z" * 32, ""):
            with self.subTest(h=bad):
                self.assertFalse(self._enroll(bad, _UID_A))

    def test_รับตัวพิมพ์ใหญ่โดยปรับเป็นพิมพ์เล็ก(self):
        """คนอาจคัดลอก hash มาด้วยตัวพิมพ์ต่างกัน — ต้องชี้ไปที่คนเดียวกัน"""
        self.assertTrue(self._enroll(_HASH_A.upper(), _UID_A))
        self.assertEqual(
            ad._lookup_enrolled_line_id(self.s, _HASH_A), _UID_A,
            "เก็บด้วยตัวพิมพ์ใหญ่แล้วหาด้วยตัวพิมพ์เล็กต้องเจอ",
        )

    def test_หา_hash_ที่ไม่มีคืนค่าว่าง(self):
        self.assertEqual(ad._lookup_enrolled_line_id(self.s, _HASH_C), "")

    def test_เก็บไว้คนละที่กับทะเบียนผู้รับ(self):
        """ID ดิบต้องไม่อยู่ในไฟล์ทะเบียนที่ API คืนออกไป"""
        self._enroll(_HASH_A, _UID_A)
        ad.add_recipient(self.s, _HASH_A, "nurse", "admin")
        registry = self.s.objects[ad.RECIPIENT_REGISTRY_PATH].decode("utf-8")
        self.assertNotIn(_UID_A, registry)


# ─────────────────────────────────────────────────────────────
# เพิ่ม / ลบ ผู้รับ
# ─────────────────────────────────────────────────────────────

class TestAddRemoveRecipient(_Base):

    def test_เพิ่มได้เมื่อลงทะเบียนแล้ว(self):
        self._enroll(_HASH_A, _UID_A)
        r = ad.add_recipient(self.s, _HASH_A, "nurse", "admin")
        self.assertEqual(r.get("status"), "added", r)

    def test_ปฏิเสธถ้ายังไม่ลงทะเบียน(self):
        """สำคัญ: ถ้าเพิ่มได้แต่ส่งไม่ได้ จะล้มเงียบเหมือนเดิม"""
        r = ad.add_recipient(self.s, _HASH_C, "nurse", "admin")
        self.assertIn("error", r)
        self.assertIn("ยืนยัน", r["error"])
        self.assertEqual(ad.list_recipients(self.s), [])

    def test_ปฏิเสธ_hash_รูปแบบผิด(self):
        for bad in ("", "xyz", "Z" * 32, "a" * 31, "a" * 64):
            with self.subTest(h=bad):
                self.assertIn(
                    "error", ad.add_recipient(self.s, bad, "nurse", "admin"),
                )

    def test_ปฏิเสธบทบาทที่ไม่รู้จัก(self):
        self._enroll(_HASH_A, _UID_A)
        self.assertIn(
            "error", ad.add_recipient(self.s, _HASH_A, "ceo", "admin"),
        )

    def test_เพิ่มซ้ำคือการแก้บทบาท_ไม่เกิดสองรายการ(self):
        self._enroll(_HASH_A, _UID_A)
        ad.add_recipient(self.s, _HASH_A, "nurse", "admin")
        r = ad.add_recipient(self.s, _HASH_A, "physician", "admin")
        self.assertEqual(r.get("status"), "updated")
        lst = ad.list_recipients(self.s)
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["role"], "physician")

    def test_ลบคือปิดการใช้งาน_ไม่ลบประวัติ(self):
        self._enroll(_HASH_A, _UID_A)
        ad.add_recipient(self.s, _HASH_A, "nurse", "admin")
        r = ad.remove_recipient(self.s, _HASH_A, "admin")
        self.assertEqual(r.get("status"), "removed")
        lst = ad.list_recipients(self.s)
        self.assertEqual(len(lst), 1, "ประวัติต้องยังอยู่")
        self.assertFalse(lst[0]["active"])

    def test_ลบคนที่ไม่มีในทะเบียน(self):
        self.assertIn("error", ad.remove_recipient(self.s, _HASH_C, "admin"))


# ─────────────────────────────────────────────────────────────
# การ resolve ไป LINE ID ตอนส่ง
# ─────────────────────────────────────────────────────────────

class TestResolveRecipients(_Base):

    def test_รวม_env_กับทะเบียน(self):
        os.environ["ALERT_RECIPIENTS"] = _UID_ENV
        self._enroll(_HASH_A, _UID_A)
        ad.add_recipient(self.s, _HASH_A, "nurse", "admin")

        ids = ad.resolve_alert_recipients(self.s).get("physician", [])
        self.assertIn(_UID_ENV, ids, "ผู้รับเดิมจาก ENV ต้องไม่หลุดหาย")
        self.assertIn(_UID_A, ids)
        self.assertEqual(len(ids), 2)

    def test_ทะเบียนทำงานได้แม้ไม่ตั้ง_env(self):
        self._enroll(_HASH_A, _UID_A)
        ad.add_recipient(self.s, _HASH_A, "nurse", "admin")
        ids = ad.resolve_alert_recipients(self.s).get("physician", [])
        self.assertEqual(ids, [_UID_A])

    def test_ไม่ซ้ำเมื่อคนเดียวกันอยู่ทั้งสองที่(self):
        os.environ["ALERT_RECIPIENTS"] = _UID_A
        self._enroll(_HASH_A, _UID_A)
        ad.add_recipient(self.s, _HASH_A, "physician", "admin")
        ids = ad.resolve_alert_recipients(self.s).get("physician", [])
        self.assertEqual(ids, [_UID_A])

    def test_คนที่ปิดการใช้งานไม่ถูกส่ง(self):
        self._enroll(_HASH_A, _UID_A)
        self._enroll(_HASH_B, _UID_B)
        ad.add_recipient(self.s, _HASH_A, "nurse", "admin")
        ad.add_recipient(self.s, _HASH_B, "nurse", "admin")
        ad.remove_recipient(self.s, _HASH_A, "admin")
        ids = ad.resolve_alert_recipients(self.s).get("physician", [])
        self.assertEqual(ids, [_UID_B])

    def test_ทะเบียนเสียหายต้องไม่ทำให้_env_หลุด(self):
        os.environ["ALERT_RECIPIENTS"] = _UID_ENV
        self.s.objects[ad.RECIPIENT_REGISTRY_PATH] = b"{not json"
        ids = ad.resolve_alert_recipients(self.s).get("physician", [])
        self.assertEqual(ids, [_UID_ENV])


# ─────────────────────────────────────────────────────────────
# ต้องไม่มี LINE user ID หลุดออกทาง API หรือ log
# ─────────────────────────────────────────────────────────────

class TestNoIdLeak(_Base):

    def setUp(self):
        super().setUp()
        self._enroll(_HASH_A, _UID_A)
        ad.add_recipient(self.s, _HASH_A, "nurse", "admin", note="พยาบาล A")

    def test_list_recipients_ไม่มี_id(self):
        blob = json.dumps(ad.list_recipients(self.s), ensure_ascii=False)
        self.assertNotIn(_UID_A, blob)

    def test_list_recipients_ไม่คืน_hash_เต็ม(self):
        """hash เต็มคือสิ่งที่ใช้เพิ่ม/ลบ — เจ้าตัวมีอยู่แล้ว ไม่ต้องคืน"""
        blob = json.dumps(ad.list_recipients(self.s))
        self.assertNotIn(_HASH_A, blob)
        self.assertIn(_HASH_A[:12], blob)

    def test_describe_ไม่มี_id(self):
        blob = json.dumps(ad.describe_alert_recipients(self.s))
        self.assertNotIn(_UID_A, blob)

    def test_log_ไม่มี_id(self):
        import io
        import logging
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        ad.logger.addHandler(handler)
        prev = ad.logger.level
        ad.logger.setLevel(logging.DEBUG)
        try:
            ad.resolve_alert_recipients(self.s)
            ad.add_recipient(self.s, _HASH_A, "nurse", "admin")
            ad.store_enrolled_line_id(self.s, _HASH_B, _UID_B)
        finally:
            ad.logger.removeHandler(handler)
            ad.logger.setLevel(prev)
        out = stream.getvalue()
        self.assertNotIn(_UID_A, out)
        self.assertNotIn(_UID_B, out)


# ─────────────────────────────────────────────────────────────
# /health รายงานรวม
# ─────────────────────────────────────────────────────────────

class TestHealthCombined(_Base):

    def test_นับรวม_env_กับทะเบียน(self):
        os.environ["ALERT_RECIPIENTS"] = _UID_ENV
        self._enroll(_HASH_A, _UID_A)
        ad.add_recipient(self.s, _HASH_A, "nurse", "admin")
        d = ad.describe_alert_recipients(self.s)
        self.assertTrue(d["can_deliver"])
        self.assertEqual(d["recipients"], 2)
        self.assertEqual(d["from_env"], 1)
        self.assertEqual(d["from_registry"], 1)
        self.assertEqual(d["problem"], "")

    def test_บอกเมื่อคนในทะเบียนยังไม่ลงทะเบียน(self):
        """เกิดได้ถ้าไฟล์ _enrolled_ids หาย — ต้องไม่เงียบ"""
        self._enroll(_HASH_A, _UID_A)
        ad.add_recipient(self.s, _HASH_A, "nurse", "admin")
        self.s.objects.pop(f"{ad.ENROLLED_IDS_PREFIX}{_HASH_A}.json")
        d = ad.describe_alert_recipients(self.s)
        self.assertFalse(d["can_deliver"])
        self.assertEqual(d["registry_unresolved"], 1)
        self.assertIn("ลงทะเบียน", d["problem"])

    def test_ไม่มีใครเลยบอกวิธีแก้(self):
        d = ad.describe_alert_recipients(self.s)
        self.assertFalse(d["can_deliver"])
        self.assertIn("ALERT_RECIPIENTS", d["problem"])


# ─────────────────────────────────────────────────────────────
# การเปลี่ยนต้องมีผลทันที
# ─────────────────────────────────────────────────────────────

class TestImmediateEffect(_Base):

    def test_set_recipients_เปลี่ยนได้โดยไม่สร้าง_service_ใหม่(self):
        """service ถูกสร้างครั้งเดียวตอน import — ถ้าไม่มีตัวนี้
        การเพิ่มพยาบาลจะไม่มีผลจนกว่าจะ redeploy และคนเพิ่มจะไม่รู้"""
        svc = ad.AlertDeliveryService(
            self.s, None, clinician_recipients={"physician": [_UID_ENV]},
        )
        self.assertEqual(svc.recipient_count(), 1)
        svc.set_recipients({"physician": [_UID_ENV, _UID_A]})
        self.assertEqual(svc.recipient_count(), 2)

    def test_service_มีเมธอดที่_endpoint_เรียกใช้(self):
        self.assertTrue(hasattr(ad.AlertDeliveryService, "set_recipients"))
        self.assertTrue(hasattr(ad.AlertDeliveryService, "recipient_count"))

    def test_endpoint_โหลดผู้รับใหม่หลังแก้ทะเบียน(self):
        import pathlib
        src = pathlib.Path("server.py").read_text(encoding="utf-8")
        i = src.index('def api_alert_recipients')
        block = src[i:i + 4000]
        self.assertIn("set_recipients", block)
        self.assertIn("resolve_alert_recipients", block)
        self.assertIn("_admin_api_auth", block)
        self.assertLess(
            block.index("_admin_api_auth"), block.index("add_recipient"),
            "ตรวจสิทธิ์ต้องมาก่อนการแก้ทะเบียน",
        )


if __name__ == "__main__":
    unittest.main()
