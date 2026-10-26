"""Acceptance tests — การจัดแฟ้มบิลตามบริษัทและเดือนที่ส่ง

ครอบคลุมข้อกำหนด P1 VENDOR/MONTH FOLDER ROUTING ข้อ 7 (14 ข้อ)
และข้อกำหนด DYNAMIC MULTI-VENDOR DOCUMENT ROUTING

ข้อมูลทดสอบทั้งหมดเป็นค่าสังเคราะห์ — เลขผู้เสียภาษีของบริษัทจริง
ที่ปรากฏเป็นข้อมูลนิติบุคคลสาธารณะ ไม่ใช่ข้อมูลผู้ป่วย

ข้อที่ทดสอบจากที่นี่ไม่ได้ (รายงาน BLOCKED ในรายงาน ไม่ใช่ PASS):
  - ข้อ 10 Windows Sync delay → ส่วน Windows Cloud Sync ไม่อยู่ใน repo นี้
    (ทำงานบนเครื่องคลินิก) จึงไม่มีโค้ดให้ทดสอบ
  - การยืนยันว่า object อยู่บน GCS จริง → ต้องมี credential

import server แบบ lazy เพื่อไม่รบกวนลำดับ import ของโมดูลทดสอบอื่น
"""
import os
import unittest
from datetime import datetime, timezone

# endpoint อ่าน ADMIN_SECRET จาก env ตอนถูกเรียก จึงตั้งค่าใน setUp
# ไม่ใช่ตอน import — โมดูลทดสอบอื่นลบตัวแปรนี้ออกจาก env เพื่อทดสอบ
# กรณียังไม่ตั้งค่า ถ้าพึ่งค่าตอน import ผลจะขึ้นกับลำดับไฟล์
_TEST_ADMIN_SECRET = "test-admin-secret-vendor-routing"

from quarterly_compare import build_document_path, to_bangkok
import vendor_registry as vr


def _server():
    import server as _s
    return _s


# ── เอกสารทดสอบ ────────────────────────────────────────────

NIKKISO_INVOICE = """NIKKISO MEDICAL (THAILAND) CO., LTD.
เลขประจำตัวผู้เสียภาษี 0105537100222
TAX INVOICE / DELIVERY ORDER
INVOICE NO. IV2610000440   DATE 14/10/2026
HEMODIALYSIS PACKAGE
ลูกค้า: คลินิกระนอง รักษ์ไต
รวมทั้งสิ้น 45,000.00 บาท
"""

MEDITOP_INVOICE = """บริษัท เมดิท็อป จำกัด
เลขประจำตัวผู้เสียภาษี 0105528039974
ใบกำกับภาษี / ใบส่งของ
BLOOD LINE TUBING  MDT-BL-VT
ลูกค้า คลินิกระนอง รักษ์ไต
รวมเงิน 12,500 บาท
"""

UNKNOWN_VENDOR_INVOICE = """บริษัท ซันไรส์ เมดิคัล ซัพพลาย จำกัด
เลขประจำตัวผู้เสียภาษี 0105560009999
ใบกำกับภาษี เลขที่ INV-5566
ลูกค้า คลินิกระนอง รักษ์ไต
รวมทั้งสิ้น 8,000 บาท
"""

# ชื่อขึ้นต้นเหมือน THAI_WATER_GENERATION แต่เป็นคนละบริษัท
SIMILAR_NAME_INVOICE = """THAI WATER SUPPLY CO., LTD.
ใบกำกับภาษี
ลูกค้า คลินิกระนอง รักษ์ไต
รวม 3,000 บาท
"""

# คลินิก (ผู้ซื้อ) ปรากฏเป็นชื่อเดียวในเอกสาร — ต้องไม่ถูกจับเป็นผู้ขาย
BUYER_ONLY_RECEIPT = """คลินิกระนอง รักษ์ไต
ใบเสร็จรับเงิน
ค่าฟอกเลือด 1,500 บาท
"""


class VendorFixture(unittest.TestCase):
    """ทะเบียนบริษัทที่ใช้หน่วยความจำ (ไม่แตะ GCS จริง)"""

    def setUp(self):
        self.registry = vr.VendorRegistry(storage=_FakeBucket())


class _FakeBlob:
    def __init__(self, name, bucket):
        self.name, self._b = name, bucket

    def exists(self):
        return self.name in self._b.objs

    def upload_from_string(self, data, content_type=None):
        self._b.objs[self.name] = data

    def download_as_text(self):
        return self._b.objs[self.name]


class _FakeBucket:
    def __init__(self):
        self.objs = {}

    def blob(self, name):
        return _FakeBlob(name, self)

    def list_blobs(self, prefix=""):
        return [_FakeBlob(k, self) for k in self.objs if k.startswith(prefix)]


# ═══════════════════════════════════════════════════════════
# ข้อ 1–3, 13: เดือนของแฟ้มต้องมาจากเวลาที่ "ส่ง" ตามปฏิทินไทย
# ═══════════════════════════════════════════════════════════

class TestSubmissionMonthRouting(unittest.TestCase):

    OCT = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    NOV = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)

    def _path(self, date):
        return build_document_path(
            "02_BILLS_RECEIPTS", "u.jpg", "bills",
            subfolder="NIKKISO", date=date,
        )

    def test_1_october_submission_goes_to_october(self):
        self.assertEqual(
            self._path(self.OCT), "02_BILLS_RECEIPTS/NIKKISO/2026-10/u.jpg"
        )

    def test_2_november_submission_goes_to_november(self):
        self.assertEqual(
            self._path(self.NOV), "02_BILLS_RECEIPTS/NIKKISO/2026-11/u.jpg"
        )

    def test_3_october_invoice_sent_in_november_uses_november(self):
        """ใบกำกับลงวันที่ ต.ค. แต่ส่งเข้ามา พ.ย. → ต้องอยู่แฟ้ม พ.ย.

        เดือนของแฟ้มยึดวันที่ "ส่ง" ไม่ใช่วันที่บนเอกสาร
        """
        self.assertIn("/2026-11/", self._path(self.NOV))
        self.assertNotIn("/2026-10/", self._path(self.NOV))

    def test_13_timestamp_near_midnight_uses_thai_calendar(self):
        """31 ต.ค. 18:30 UTC = 1 พ.ย. 01:30 ไทย → ต้องเป็นเดือน พ.ย.

        นี่คือจุดที่เดิมผิด: ระบบคิดเดือนจาก UTC เอกสารที่ส่งช่วง
        00:00–07:00 ตามเวลาไทยจึงถูกนับเป็นวันก่อนหน้า
        """
        near_midnight = datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc)
        self.assertEqual(
            to_bangkok(near_midnight).strftime("%Y-%m-%d"), "2026-11-01"
        )
        self.assertIn("/2026-11/", self._path(near_midnight))

    def test_13b_late_evening_thai_time_stays_in_same_month(self):
        """31 ต.ค. 16:00 ไทย (09:00 UTC) ต้องยังเป็น ต.ค."""
        same_month = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
        self.assertIn("/2026-10/", self._path(same_month))

    def test_naive_datetime_treated_as_utc(self):
        """datetime ที่ไม่มีเขตเวลาต้องตีความเป็น UTC เหมือนเดิม"""
        naive = datetime(2026, 10, 31, 18, 30)
        self.assertIn("/2026-11/", self._path(naive))

    def test_clinical_categories_use_same_calendar(self):
        """ผลแล็บต้องใช้ปฏิทินเดียวกัน ไม่ให้มีสองมาตรฐานในระบบ"""
        near_midnight = datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc)
        path = build_document_path(
            "04_LAB_RESULTS", "u.pdf", "lab",
            patient_folder="57042_ทดสอบ", date=near_midnight,
        )
        self.assertEqual(
            path, "04_LAB_RESULTS/2026-Q4/2026-11/57042_ทดสอบ/u.pdf"
        )


# ═══════════════════════════════════════════════════════════
# ข้อ 4–5: บริษัทที่อนุมัติแล้ว vs บริษัทที่ยังไม่รู้จัก
# ═══════════════════════════════════════════════════════════

class TestVendorIdentification(VendorFixture):

    def test_4_known_vendor_routes_to_own_folder(self):
        m = self.registry.match_document(MEDITOP_INVOICE)
        self.assertEqual(m.folder, "MEDITOP")
        self.assertTrue(m.auto_route)

    def test_4b_each_vendor_gets_a_separate_folder(self):
        a = self.registry.match_document(NIKKISO_INVOICE)
        b = self.registry.match_document(MEDITOP_INVOICE)
        self.assertEqual(a.folder, "NIKKISO")
        self.assertEqual(b.folder, "MEDITOP")
        self.assertNotEqual(a.folder, b.folder)

    def test_5_unknown_vendor_goes_to_review_not_a_guess(self):
        m = self.registry.match_document(UNKNOWN_VENDOR_INVOICE)
        self.assertFalse(m.auto_route)
        self.assertTrue(m.needs_review)
        self.assertEqual(m.folder, vr.REVIEW_VENDOR_FOLDER)
        # ต้องไม่ถูกยัดเข้าแฟ้มบริษัทใดที่มีอยู่
        for v in self.registry.list_all():
            self.assertNotEqual(m.folder, v.canonical_name)

    def test_5b_review_folder_keeps_extracted_name_for_admin(self):
        """ผู้ดูแลต้องเห็นว่าระบบอ่านชื่อบริษัทได้ว่าอย่างไร"""
        m = self.registry.match_document(UNKNOWN_VENDOR_INVOICE)
        self.assertIn("ซันไรส์", m.extracted_name)

    def test_6_ocr_failure_goes_to_review(self):
        for junk in ("", "   ", "\n\n", "�����", "123"):
            with self.subTest(text=junk):
                m = self.registry.match_document(junk)
                self.assertFalse(m.auto_route)
                self.assertIn(
                    m.folder,
                    (vr.REVIEW_VENDOR_FOLDER, vr.UNKNOWN_VENDOR_FOLDER),
                )

    def test_tax_id_takes_priority(self):
        m = self.registry.match_document(NIKKISO_INVOICE)
        self.assertEqual(m.method, vr.MATCH_TAX_ID)
        self.assertEqual(m.confidence, 1.0)

    def test_partial_tax_id_is_not_accepted(self):
        """เลขผู้เสียภาษีไม่ครบ 13 หลักต้องไม่ใช้จับคู่

        เลขขึ้นต้นเหมือนกันเป็นเรื่องปกติ การรับเลขบางส่วนจะจับผิดบริษัท
        """
        text = "บริษัทไม่ทราบชื่อ\nเลขผู้เสียภาษี 010553710\nใบกำกับภาษี"
        m = self.registry.match_document(text)
        self.assertNotEqual(m.folder, "NIKKISO")


# ═══════════════════════════════════════════════════════════
# ชื่อพ้อง / ผู้ซื้อ vs ผู้ขาย / ไม่รวมบริษัทผิด
# ═══════════════════════════════════════════════════════════

class TestVendorDisambiguation(VendorFixture):

    def test_thai_and_english_aliases_resolve_to_one_vendor(self):
        thai = self.registry.match_document(
            "บริษัท เมดิท็อป จำกัด\nใบกำกับภาษี\nรวม 100 บาท"
        )
        eng = self.registry.match_document(
            "MEDITOP CO., LTD.\nTAX INVOICE\ntotal 100 baht"
        )
        self.assertEqual(thai.folder, "MEDITOP")
        self.assertEqual(eng.folder, "MEDITOP")

    def test_legal_suffix_variants_resolve_to_one_vendor(self):
        """บริษัท X จำกัด / บจก. X / X ต้องเป็นแฟ้มเดียว ไม่แตกเป็นสามแฟ้ม"""
        seen = set()
        for name in (
            "บริษัท เมดิท็อป จำกัด",
            "บจก.เมดิท็อป",
            "บริษัท เมดิท็อป (ประเทศไทย) จำกัด",
        ):
            m = self.registry.match_document(f"{name}\nใบกำกับภาษี\n100 บาท")
            seen.add(m.folder)
        self.assertEqual(seen, {"MEDITOP"})

    def test_similar_company_name_is_not_merged(self):
        """ชื่อขึ้นต้นเหมือนกันต้องไม่ถูกรวมเป็นบริษัทเดียว

        'THAI WATER SUPPLY' ขึ้นต้นเหมือน 'THAI WATER GENERATION'
        แต่เป็นคนละนิติบุคคล — ต้องเข้าแฟ้มตรวจสอบ ไม่ใช่เดา
        """
        m = self.registry.match_document(SIMILAR_NAME_INVOICE)
        self.assertNotEqual(m.folder, "THAI_WATER_GENERATION")
        self.assertFalse(m.auto_route)

    def test_real_thai_water_still_matches(self):
        """การกันชื่อพ้องต้องไม่ทำให้บริษัทจริงจับไม่ได้"""
        m = self.registry.match_document(
            "บริษัท ไทย วอเตอร์ เจเนอเรชั่น จำกัด\n"
            "ใบกำกับภาษี  ระบบ RO\nรวม 20,000 บาท"
        )
        self.assertEqual(m.folder, "THAI_WATER_GENERATION")
        self.assertTrue(m.auto_route)

    def test_buyer_name_is_not_mistaken_for_supplier(self):
        """ชื่อคลินิกอยู่ใต้คำว่า 'ลูกค้า' — ต้องไม่ถูกจับเป็นผู้ขาย"""
        m = self.registry.match_document(NIKKISO_INVOICE)
        self.assertEqual(m.folder, "NIKKISO")

    def test_clinic_own_receipt_is_not_a_vendor(self):
        """ใบเสร็จของคลินิกเอง ไม่มีผู้ขาย → ต้องไม่สร้างแฟ้มบริษัท"""
        m = self.registry.match_document(BUYER_ONLY_RECEIPT)
        self.assertFalse(m.auto_route)

    def test_product_keyword_alone_does_not_decide_vendor(self):
        """ชื่อสินค้าอย่างเดียวไม่พอตัดสินบริษัท — เสนอให้คนยืนยันแทน

        สินค้ารุ่นเดียวกันอาจถูกอ้างในเอกสารของบริษัทอื่น
        """
        m = self.registry.match_document(
            "ใบส่งของ\nAGANI NEEDLE 17G จำนวน 200 ชิ้น\nรวม 8,000 บาท"
        )
        self.assertFalse(m.auto_route)
        self.assertIn("BJC", [c[0] for c in m.candidates])

    def test_fuzzy_match_never_auto_routes(self):
        """การจับคู่แบบคล้ายต้องไม่จัดเก็บเอง ไม่ว่าคล้ายแค่ไหน"""
        m = vr.VendorMatch(
            vendor=self.registry.get("NIKKISO"),
            method=vr.MATCH_FUZZY, confidence=0.99,
        )
        self.assertFalse(m.auto_route)

    def test_unverified_vendor_never_auto_routes(self):
        m = vr.VendorMatch(
            vendor=vr.VendorRecord(canonical_name="X", verified=False),
            method=vr.MATCH_TAX_ID, confidence=1.0,
        )
        self.assertFalse(m.auto_route)


# ═══════════════════════════════════════════════════════════
# เพิ่มบริษัทใหม่โดยไม่แก้โค้ด + ใช้แฟ้มเดิมซ้ำ
# ═══════════════════════════════════════════════════════════

class TestVendorRegistration(VendorFixture):

    NEW = dict(
        canonical_name="SUNRISE_MEDICAL",
        display_name="บริษัท ซันไรส์ เมดิคัล ซัพพลาย จำกัด",
        aliases=["sunrise medical supply", "ซันไรส์ เมดิคัล ซัพพลาย"],
        tax_id="0105560009999",
        verified=True,
    )

    def test_new_verified_company_creates_its_own_folder(self):
        ok, msg, rec = self.registry.register(**self.NEW)
        self.assertTrue(ok, msg)
        m = self.registry.match_document(UNKNOWN_VENDOR_INVOICE)
        self.assertEqual(m.folder, "SUNRISE_MEDICAL")
        self.assertTrue(m.auto_route)

    def test_repeat_submission_reuses_same_folder(self):
        self.registry.register(**self.NEW)
        folders = {
            self.registry.match_document(
                f"บริษัท ซันไรส์ เมดิคัล ซัพพลาย จำกัด\n"
                f"ใบกำกับภาษี INV-55{n}\nรวม 1,000 บาท"
            ).folder
            for n in range(66, 70)
        }
        self.assertEqual(folders, {"SUNRISE_MEDICAL"})

    def test_registration_survives_a_fresh_registry_instance(self):
        """บริษัทที่ลงทะเบียนต้องอยู่บน GCS ไม่ใช่แค่ในหน่วยความจำ worker"""
        bucket = _FakeBucket()
        first = vr.VendorRegistry(storage=bucket)
        self.assertTrue(first.register(**self.NEW)[0])

        second = vr.VendorRegistry(storage=bucket)
        self.assertIsNotNone(second.get("SUNRISE_MEDICAL"))
        self.assertTrue(
            second.match_document(UNKNOWN_VENDOR_INVOICE).auto_route
        )

    def test_seed_vendors_survive_registration(self):
        """เพิ่มบริษัทใหม่ต้องไม่ทำให้บริษัทเดิมหาย"""
        before = {v.canonical_name for v in self.registry.list_all()}
        self.registry.register(**self.NEW)
        after = {v.canonical_name for v in self.registry.list_all()}
        self.assertTrue(before.issubset(after))
        self.assertIn("NIKKISO", after)

    def test_duplicate_tax_id_is_refused(self):
        ok, msg, _ = self.registry.register(
            canonical_name="FAKE_NIKKISO", tax_id="0105537100222",
        )
        self.assertFalse(ok)
        self.assertIn("NIKKISO", msg)

    def test_duplicate_alias_is_refused(self):
        """กันแฟ้มซ้ำจากการสะกดต่างกัน"""
        ok, msg, _ = self.registry.register(
            canonical_name="NIKKISO_TH", aliases=["nikkiso medical"],
        )
        self.assertFalse(ok)

    def test_similar_name_needs_explicit_confirmation(self):
        ok, msg, _ = self.registry.register(
            canonical_name="MEDITOPP", aliases=["meditopp"],
        )
        self.assertFalse(ok)
        self.assertIn("คล้าย", msg)
        # ผู้ดูแลยืนยันว่าเป็นบริษัทใหม่จริง → ผ่านได้
        ok2, msg2, _ = self.registry.register(
            canonical_name="MEDITOPP", aliases=["meditopp"],
            allow_similar=True,
        )
        self.assertTrue(ok2, msg2)

    def test_thai_folder_name_is_allowed(self):
        """บริษัทชื่อไทยล้วนต้องตั้งชื่อแฟ้มเป็นไทยได้

        ระบบสร้างแฟ้มผู้ป่วยเป็นภาษาไทยอยู่แล้ว และ Windows รองรับ Unicode
        """
        proposed = vr.to_canonical_name("บริษัท ซันไรส์ เมดิคัล จำกัด")
        self.assertTrue(vr.is_valid_canonical_name(proposed))
        self.assertNotIn("บริษัท", proposed)
        self.assertNotIn("จำกัด", proposed)
        ok, msg, _ = self.registry.register(
            canonical_name=proposed, aliases=["ซันไรส์ เมดิคัล"],
            verified=True,
        )
        self.assertTrue(ok, msg)

    def test_deactivated_vendor_stops_auto_routing(self):
        self.registry.register(**self.NEW)
        self.assertTrue(self.registry.deactivate("SUNRISE_MEDICAL")[0])
        m = self.registry.match_document(UNKNOWN_VENDOR_INVOICE)
        self.assertFalse(m.auto_route)

    def test_registration_without_storage_fails_closed(self):
        """เขียนทะเบียนไม่ได้ ต้องไม่รายงานว่าสำเร็จ"""
        no_storage = vr.VendorRegistry(storage=None)
        ok, msg, rec = no_storage.register(**self.NEW)
        self.assertFalse(ok)
        self.assertIsNone(rec)
        # และต้องไม่ค้างอยู่ในหน่วยความจำ
        self.assertIsNone(no_storage.get("SUNRISE_MEDICAL"))


# ═══════════════════════════════════════════════════════════
# ข้อ 11: path traversal และชื่อแฟ้มที่ Windows ใช้ไม่ได้
# ═══════════════════════════════════════════════════════════

class TestPathSafety(VendorFixture):

    ATTACKS = (
        "../../etc/passwd", "..", "../", "a/b", "a\\b",
        "CON", "PRN", "NUL", "COM1", "LPT1",
        'bad<>:"|?*name', "\x00evil", "_UNIDENTIFIED",
        "_PENDING_REVIEW", "OTHER", "", " ", "x" * 200,
    )

    def test_11_registration_rejects_unsafe_folder_names(self):
        for bad in self.ATTACKS:
            with self.subTest(name=bad):
                ok, _msg, rec = self.registry.register(canonical_name=bad)
                self.assertFalse(ok, f"ต้องปฏิเสธ: {bad!r}")
                self.assertIsNone(rec)

    def test_11b_path_never_escapes_category_root(self):
        """แม้ส่งค่าที่เป็นอันตรายเข้า builder ตรงๆ path ต้องไม่หลุดออก"""
        for bad in self.ATTACKS:
            with self.subTest(name=bad):
                path = build_document_path(
                    "02_BILLS_RECEIPTS", "f.jpg", "bills", subfolder=bad,
                    date=datetime(2026, 10, 14, tzinfo=timezone.utc),
                )
                self.assertTrue(path.startswith("02_BILLS_RECEIPTS/"))
                self.assertNotIn("..", path)
                self.assertNotIn("//", path)
                self.assertEqual(path.count("/"), path.count("/"))
                for ch in '<>:"|?*':
                    self.assertNotIn(ch, path)

    def test_11c_ocr_text_is_never_used_as_a_path(self):
        """ชื่อที่ดึงจาก OCR ต้องไม่กลายเป็นชื่อแฟ้มเอง

        ต้องผ่านทะเบียนที่คนยืนยันแล้วเท่านั้น
        """
        hostile = (
            "บริษัท ../../../etc/passwd จำกัด\n"
            "ใบกำกับภาษี\nรวม 100 บาท"
        )
        m = self.registry.match_document(hostile)
        self.assertFalse(m.auto_route)
        self.assertIn(
            m.folder, (vr.REVIEW_VENDOR_FOLDER, vr.UNKNOWN_VENDOR_FOLDER)
        )
        self.assertNotIn("..", m.folder)

    def test_canonical_name_validator_accepts_only_safe_names(self):
        for good in ("NIKKISO", "THAI_WATER_GENERATION", "BJC", "ซันไรส์"):
            self.assertTrue(vr.is_valid_canonical_name(good), good)
        for bad in ("", "a", "_X", "A B", "A/B", "A..B", "CON", "x" * 41):
            self.assertFalse(vr.is_valid_canonical_name(bad), bad)


# ═══════════════════════════════════════════════════════════
# ข้อ 7: จัดประเภทด้วยมือต้องได้แฟ้มเดียวกับอัตโนมัติ
# ═══════════════════════════════════════════════════════════

class TestSingleRoutingService(unittest.TestCase):

    def test_7_manual_and_automatic_paths_agree(self):
        """เส้นทางจัดประเภทใหม่ต้องส่ง subfolder เหมือนเส้นทางอัตโนมัติ

        เดิมเส้นทางจัดประเภทใหม่ไม่ส่ง subfolder เลย บิลที่ผู้ดูแล
        จัดประเภทด้วยมือจึงลงที่ 02_BILLS_RECEIPTS/YYYY-MM/ ไม่มีแฟ้มบริษัท
        """
        date = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
        auto = build_document_path(
            "02_BILLS_RECEIPTS", "u.jpg", "bills",
            subfolder="NIKKISO", date=date,
        )
        manual = build_document_path(
            "02_BILLS_RECEIPTS", "u.jpg", "bills",
            patient_folder="", subfolder="NIKKISO", date=date,
        )
        self.assertEqual(auto, manual)

    def test_reclassify_uses_document_date_not_today(self):
        """จัดประเภทใหม่ต้องไม่ย้ายเอกสารไปเดือนที่กดจัดประเภท"""
        s = _server()
        doc = {"upload_date": "2026-08-20"}
        dt = s._doc_submitted_date(doc)
        self.assertIsNotNone(dt)
        path = build_document_path(
            "02_BILLS_RECEIPTS", "u.jpg", "bills",
            subfolder="NIKKISO", date=dt,
        )
        self.assertIn("/2026-08/", path)

    def test_date_only_value_keeps_its_month_in_thai_calendar(self):
        """วันที่ล้วนต้องให้เดือนเดิมเสมอ ไม่เลื่อนเพราะแปลงเขตเวลา

        ถ้าเลื่อน ระบบจะคิดว่าเอกสารเก่าทุกใบที่ลงวันที่ 1 ต้องย้ายแฟ้ม
        """
        s = _server()
        for day in ("2026-01-01", "2026-10-31", "2026-12-31", "2026-03-01"):
            with self.subTest(day=day):
                dt = s._doc_submitted_date({"upload_date": day})
                self.assertEqual(
                    to_bangkok(dt).strftime("%Y-%m"), day[:7]
                )

    def test_submitted_at_wins_over_upload_date(self):
        s = _server()
        dt = s._doc_submitted_date({
            "upload_date": "2026-08-20",
            "submitted_at": "2026-11-03T09:00:00+00:00",
        })
        self.assertEqual(to_bangkok(dt).strftime("%Y-%m"), "2026-11")


# ═══════════════════════════════════════════════════════════
# ข้อ 8–9, 12: event ซ้ำ / upload ล้มเหลว / เนื้อหาต่างกัน
# ═══════════════════════════════════════════════════════════

class TestReliability(unittest.TestCase):

    def test_9_classifier_output_is_never_used_as_folder_name(self):
        """ค่าที่ผู้จำแนกส่งกลับมาต้องผ่านทะเบียนก่อนใช้เป็นชื่อแฟ้ม

        ถ้าเปิดการจำแนกด้วย AI ค่านั้นคือข้อความอิสระจากโมเดล
        """
        s = _server()
        for claimed in ("../../etc", "EVIL_CO", "NIKKISO/../X", ""):
            with self.subTest(claimed=claimed):
                valid = (
                    s.is_valid_canonical_name(claimed.strip().upper())
                    and s.vendor_registry.get(claimed.strip().upper())
                    is not None
                )
                self.assertFalse(valid, f"ต้องไม่ยอมรับ: {claimed!r}")

    def test_line_timestamp_out_of_range_is_rejected(self):
        """timestamp ที่เพี้ยนต้องไม่ทำให้เอกสารไปอยู่เดือนที่ไม่มีจริง"""
        s = _server()
        self.assertIsNone(s.parse_line_timestamp(0))
        self.assertIsNone(s.parse_line_timestamp(-1))
        self.assertIsNone(s.parse_line_timestamp("abc"))
        self.assertIsNone(s.parse_line_timestamp(None))
        self.assertIsNone(s.parse_line_timestamp(1_000_000))      # ปี 1970
        self.assertIsNone(s.parse_line_timestamp(99_999_999_999_999))  # อนาคต

    def test_valid_line_timestamp_is_accepted(self):
        s = _server()
        # 14 ต.ค. 2026 09:00 UTC
        ms = int(
            datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        dt = s.parse_line_timestamp(ms)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.strftime("%Y-%m-%d"), "2026-10-14")

    def test_metadata_carries_submission_time_without_phi(self):
        s = _server()
        # ใช้เวลาในอดีต — parse_line_timestamp ปฏิเสธเวลาอนาคตโดยเจตนา
        ms = int(
            datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        meta = s.build_object_metadata(
            doc_id="d1", category="bills", ext=".jpg",
            size_bytes=100, sha256_hex="a" * 64, submitted_at_ms=ms,
        )
        self.assertIn("submitted_at", meta)
        self.assertTrue(meta["submitted_at"].startswith("2026-09-03"))
        # ต้องไม่มีข้อมูลส่วนบุคคลใน metadata
        blob = " ".join(str(v) for v in meta.values()).lower()
        for leak in ("nikkiso", "ผู้ป่วย", "userid", "line"):
            if leak == "line":
                continue  # "line_oa" เป็นชื่อแหล่งที่มา ไม่ใช่ตัวระบุบุคคล
            self.assertNotIn(leak, blob)

    def test_metadata_without_timestamp_omits_the_field(self):
        """ไม่มี timestamp → ไม่ใส่ค่าเดา ให้ผู้เรียก fallback เวลาปัจจุบัน"""
        s = _server()
        meta = s.build_object_metadata(
            doc_id="d1", category="bills", ext=".jpg",
            size_bytes=100, sha256_hex="a" * 64,
        )
        self.assertNotIn("submitted_at", meta)


# ═══════════════════════════════════════════════════════════
# ข้อ 14: ส่วนประกอบเดิมยังทำงาน
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# ข้อ 6 ของข้อกำหนด: แผนย้ายไฟล์เดิมแบบ dry-run
# ═══════════════════════════════════════════════════════════

class TestRelocationPlan(unittest.TestCase):

    NIKKISO_TEXT = (
        "NIKKISO MEDICAL (THAILAND) CO., LTD.\n"
        "เลขประจำตัวผู้เสียภาษี 0105537100222\n"
        "TAX INVOICE IV2610000440\nลูกค้า คลินิกระนอง รักษ์ไต"
    )

    def setUp(self):
        self.s = _server()
        self._backup_secret = os.environ.get("ADMIN_SECRET")
        os.environ["ADMIN_SECRET"] = _TEST_ADMIN_SECRET
        self._backup_docs = list(self.s.doc_index._docs)
        self._backup_fresh = self.s.doc_index._ensure_fresh
        self.s.doc_index._ensure_fresh = lambda: None
        bucket = self.s.STORAGE_BUCKET
        self.s.doc_index._docs = [
            {   # บิลเก่า ไม่มีแฟ้มบริษัท → ต้องอยู่ในแผน
                "document_id": "aaa11111", "category": "bills",
                "upload_date": "2026-10-14", "content_sha256": "c" * 64,
                "gcs_object_path":
                    f"gs://{bucket}/02_BILLS_RECEIPTS/2026-10/old1.jpg",
                "extracted_text": self.NIKKISO_TEXT,
            },
            {   # อยู่ถูกแฟ้มแล้ว → ต้องไม่อยู่ในแผน
                "document_id": "bbb22222", "category": "bills",
                "upload_date": "2026-10-20", "content_sha256": "d" * 64,
                "gcs_object_path": (
                    f"gs://{bucket}/02_BILLS_RECEIPTS/NIKKISO/2026-10/ok.jpg"
                ),
                "extracted_text": self.NIKKISO_TEXT,
            },
            {   # ระบุบริษัทไม่ได้ → ต้องไม่ถูกย้ายโดยเดา
                "document_id": "ccc33333", "category": "bills",
                "upload_date": "2026-10-21",
                "gcs_object_path":
                    f"gs://{bucket}/02_BILLS_RECEIPTS/2026-10/unknown.jpg",
                "extracted_text": "บริษัท ไม่เคยเจอ จำกัด\nใบกำกับภาษี",
            },
            {   # ไม่ใช่บิล → ต้องไม่ถูกแตะ
                "document_id": "ddd44444", "category": "lab",
                "upload_date": "2026-10-21",
                "gcs_object_path":
                    f"gs://{bucket}/04_LAB_RESULTS/2026-Q4/2026-10/x.pdf",
                "extracted_text": "Creatinine 2.5",
            },
        ]
        self.client = self.s.app.test_client()
        self.headers = {"X-Admin-Secret": _TEST_ADMIN_SECRET}

    def tearDown(self):
        self.s.doc_index._docs = self._backup_docs
        self.s.doc_index._ensure_fresh = self._backup_fresh
        if self._backup_secret is None:
            os.environ.pop("ADMIN_SECRET", None)
        else:
            os.environ["ADMIN_SECRET"] = self._backup_secret

    def _plan(self):
        r = self.client.get(
            "/admin/api/v2/vendors/relocation-plan", headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_plan_is_dry_run_and_changes_nothing(self):
        d = self._plan()
        self.assertTrue(d["dry_run"])
        self.assertFalse(d["applied"])
        # index ต้องไม่ถูกแก้
        self.assertEqual(
            self.s.doc_index._docs[0]["gcs_object_path"],
            f"gs://{self.s.STORAGE_BUCKET}/02_BILLS_RECEIPTS/2026-10/old1.jpg",
        )

    def test_misplaced_bill_appears_with_before_and_after_paths(self):
        d = self._plan()
        self.assertEqual(d["to_move"], 1)
        item = d["plan"][0]
        self.assertEqual(item["document_id"], "aaa11111")
        self.assertEqual(
            item["current_gcs_path"], "02_BILLS_RECEIPTS/2026-10/old1.jpg"
        )
        self.assertEqual(
            item["proposed_gcs_path"],
            "02_BILLS_RECEIPTS/NIKKISO/2026-10/old1.jpg",
        )
        self.assertEqual(item["vendor"], "NIKKISO")
        self.assertEqual(item["content_sha256"], "c" * 64)
        self.assertFalse(item["collision_risk"])

    def test_already_correct_bill_is_not_in_plan(self):
        d = self._plan()
        self.assertEqual(d["already_correct"], 1)
        self.assertNotIn(
            "bbb22222", [p["document_id"] for p in d["plan"]]
        )

    def test_unidentified_vendor_is_not_relocated_by_guessing(self):
        d = self._plan()
        self.assertEqual(d["needs_vendor_review"], 1)
        self.assertNotIn(
            "ccc33333", [p["document_id"] for p in d["plan"]]
        )

    def test_non_bill_documents_are_untouched(self):
        d = self._plan()
        self.assertNotIn(
            "ddd44444", [p["document_id"] for p in d["plan"]]
        )

    def test_plan_keeps_the_original_month(self):
        """ย้ายแฟ้มบริษัท ต้องไม่ย้ายข้ามเดือนไปด้วย"""
        d = self._plan()
        self.assertIn("/2026-10/", d["plan"][0]["proposed_gcs_path"])

    def test_windows_path_has_no_hardcoded_drive_letter(self):
        """sync root ตั้งที่เครื่องคลินิก — ต้องไม่เดา drive letter"""
        d = self._plan()
        win = d["plan"][0]["proposed_relative_windows_path"]
        self.assertNotIn(":", win)
        self.assertFalse(win.startswith("\\\\"))
        self.assertTrue(win.startswith("02_BILLS_RECEIPTS\\"))

    def test_plan_states_rollback_procedure(self):
        d = self._plan()
        self.assertTrue(d["rollback"].strip())

    def test_plan_requires_admin_auth(self):
        r = self.client.get("/admin/api/v2/vendors/relocation-plan")
        self.assertEqual(r.status_code, 401)


class TestNoRegression(unittest.TestCase):

    def test_all_seed_vendors_still_resolve(self):
        """บริษัททั้ง 9 รายที่ใช้งานบน production อยู่ต้องยังจับได้"""
        reg = vr.VendorRegistry(storage=_FakeBucket())
        expected = {e["canonical_name"] for e in vr.SEED_VENDORS}
        actual = {v.canonical_name for v in reg.list_all()}
        self.assertEqual(expected, actual)
        for v in reg.list_all():
            self.assertTrue(v.verified, f"{v.canonical_name} ต้องยืนยันแล้ว")

    def test_compat_view_still_returns_old_shape(self):
        """โค้ดเดิมที่อ่านรายชื่อบริษัทในรูปแบบ dict ต้องใช้งานต่อได้"""
        s = _server()
        view = s.get_bill_company_registry()
        self.assertIn("NIKKISO", view)
        self.assertIsInstance(view["NIKKISO"], list)
        self.assertIn("0105537100222", view["NIKKISO"])

    def test_vendor_names_feed_bill_classification(self):
        """ชื่อบริษัทในทะเบียนต้องถูกใช้เป็น keyword จัดประเภทเป็น bills"""
        s = _server()
        kws = s.get_vendor_name_keywords()
        self.assertIn("nikkiso", kws)
        self.assertIn("meditop", kws)

    def test_detect_bill_company_keeps_working(self):
        s = _server()
        self.assertEqual(s.detect_bill_company(NIKKISO_INVOICE), "NIKKISO")

    def test_nfkc_does_not_break_thai_sara_am(self):
        """'จำกัด' ต้องถูกตัดออกจริง

        NFKC แยก 'ำ' (U+0E33) ออกเป็นสองอักขระ ถ้า normalize แต่ข้อความ
        แล้วเทียบกับคำที่เขียนด้วย U+0E33 ตรงๆ คำนั้นจะไม่เคย match
        ทำให้ชื่อบริษัทไทยเทียบกันไม่ตรง
        """
        a = vr.normalize_company_name("บริษัท เมดิท็อป จำกัด")
        b = vr.normalize_company_name("เมดิท็อป")
        self.assertEqual(a, b)
        self.assertNotIn("จ", a[-3:] if len(a) >= 3 else a)


if __name__ == "__main__":
    unittest.main(verbosity=2)
