"""จำกัดตัวถอดรหัสภาพของ Pillow — ด่านที่นามสกุลไฟล์กันไม่ได้

ที่มา: pip-audit (26 ต.ค. 2026) รายงานช่องโหว่ 5 รายการใน Pillow 11.1.0
ทั้งหมดอยู่ในตัวถอดรหัสที่ระบบนี้ไม่ได้ใช้เลย
    CVE-2026-42311 / CVE-2026-25990   PSD   memory corruption / OOB write
    CVE-2026-40192                    FITS  decompression bomb (OOM)
    CVE-2026-42310                    PDF   ค้างกิน CPU 100% ไม่สิ้นสุด

ประเด็นสำคัญ: โค้ด OCR กรองด้วยนามสกุล (.jpg/.jpeg/.png) แต่ Pillow
เลือกตัวถอดรหัสจาก "เนื้อหาไฟล์" ไฟล์ที่ชื่อ photo.png แต่ข้างในเป็น
PSD จะถูกส่งเข้าตัวถอดรหัส PSD — ด่านนามสกุลกันไม่ได้เลย

เทสต์ชุดนี้ล็อกสองอย่าง
    1. รูปแบบนอกรายการที่อนุญาต ต้องเปิดไม่ได้ แม้ Pillow รองรับเองก็ตาม
    2. ภาพจริงที่นามสกุลไม่ตรงเนื้อหา (jpeg ในไฟล์ .png) ต้องยังเปิดได้
       — กันไม่ให้การแข็งขึ้นนี้ทำให้ภาพจากคลินิกที่ใช้งานจริงพัง

ไฟล์ทดสอบทั้งหมดสร้างขึ้นสด ๆ ในเทสต์ ไม่มีไฟล์ของผู้ป่วย
ไม่มีไฟล์มุ่งร้าย — ใช้ PPM ซึ่ง Pillow รองรับแต่ไม่อยู่ในรายการ
เป็นตัวแทนของ "รูปแบบที่ต้องไม่ถูกถอดรหัส"
"""
import os
import tempfile
import unittest

try:
    from PIL import Image, UnidentifiedImageError
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def _safe_formats():
    """อ่านรายการจาก server.py โดยไม่ต้องตั้ง env ครบทุกตัว"""
    os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-dummy")
    os.environ.setdefault("LINE_CHANNEL_SECRET", "test-dummy")
    os.environ.setdefault("GCS_BUCKET_NAME", "test-bucket")
    import server
    return server.SAFE_IMAGE_FORMATS


@unittest.skipUnless(HAS_PIL, "ต้องมี Pillow")
class TestSafeImageFormats(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.formats = _safe_formats()

    def _path(self, name):
        return os.path.join(self.tmp.name, name)

    # ── รายการที่อนุญาต ────────────────────────────────────

    def test_รายการไม่มีรูปแบบที่มีช่องโหว่(self):
        """PSD, FITS, PDF, EPS ต้องไม่อยู่ในรายการที่อนุญาต"""
        for bad in ("PSD", "FITS", "PDF", "EPS", "PPM", "TGA", "DDS"):
            self.assertNotIn(
                bad, self.formats,
                f"{bad} อยู่ในรายการที่อนุญาต — เป็นตัวถอดรหัสที่ระบบไม่ใช้",
            )

    def test_รายการครอบคลุมรูปแบบที่ใช้จริง(self):
        """ภาพจาก LINE และกล้องมือถือต้องอยู่ในรายการ"""
        for need in ("JPEG", "PNG"):
            self.assertIn(need, self.formats)

    # ── รูปแบบนอกรายการต้องเปิดไม่ได้ ──────────────────────

    def test_ppm_ที่ตั้งชื่อเป็น_png_ต้องเปิดไม่ได้(self):
        """แกนหลักของการแข็งขึ้น: เนื้อหานอกรายการต้องถูกปฏิเสธ

        PPM เป็นตัวแทนของทุกรูปแบบที่ Pillow รองรับแต่ระบบไม่ใช้
        (PSD, FITS, PDF ก็ถูกกันด้วยกลไกเดียวกันนี้) ที่ใช้ PPM
        เพราะสร้างไฟล์ถูกต้องได้โดยไม่ต้องมีไฟล์มุ่งร้าย
        """
        p = self._path("looks_like_a_photo.png")
        Image.new("RGB", (8, 8), (10, 20, 30)).save(p, format="PPM")

        # ไม่จำกัด → Pillow ถอดรหัสได้ตามเนื้อหา ไม่สนนามสกุล
        with Image.open(p) as im:
            self.assertEqual(im.format, "PPM")

        # จำกัด → ต้องปฏิเสธ
        with self.assertRaises(UnidentifiedImageError):
            Image.open(p, formats=self.formats)

    def test_ไฟล์ขยะที่ตั้งชื่อเป็น_jpg_ต้องเปิดไม่ได้(self):
        p = self._path("garbage.jpg")
        with open(p, "wb") as fh:
            fh.write(b"\x00\x01\x02not an image at all\xff" * 16)
        with self.assertRaises(UnidentifiedImageError):
            Image.open(p, formats=self.formats)

    # ── ภาพจริงต้องไม่พัง ──────────────────────────────────

    def test_jpeg_ในไฟล์นามสกุล_png_ต้องยังเปิดได้(self):
        """นามสกุลไม่ตรงเนื้อหาเกิดขึ้นจริงกับภาพที่ส่งทาง LINE

        การแข็งขึ้นนี้ต้องไม่ทำให้ภาพที่ใช้งานได้อยู่แล้วพัง
        """
        p = self._path("mismatched.png")
        Image.new("RGB", (12, 12), (200, 100, 50)).save(p, format="JPEG")
        with Image.open(p, formats=self.formats) as im:
            self.assertEqual(im.format, "JPEG")
            self.assertEqual(im.size, (12, 12))

    def test_ทุกรูปแบบที่อนุญาตเปิดได้จริง(self):
        """รายการที่อนุญาตต้องเป็นชื่อที่ Pillow รู้จักทั้งหมด

        ชื่อที่พิมพ์ผิดจะไม่ error แต่จะทำให้รูปแบบนั้นถูกปฏิเสธเงียบ ๆ
        """
        for fmt in self.formats:
            ext = {"JPEG": ".jpg", "TIFF": ".tif"}.get(fmt, "." + fmt.lower())
            p = self._path(f"sample{ext}")
            img = Image.new("RGB", (8, 8), (90, 90, 90))
            if fmt == "GIF":
                img = img.convert("P")
            img.save(p, format=fmt)
            with Image.open(p, formats=self.formats) as im:
                self.assertEqual(
                    im.format, fmt,
                    f"{fmt} อยู่ในรายการแต่เปิดกลับมาไม่ตรง",
                )

    # ── ยืนยันว่าโค้ดจริงใช้ข้อจำกัดนี้ ────────────────────

    def test_ทุกจุดที่เรียก_Image_open_ต้องส่ง_formats(self):
        """กันการเพิ่มจุดเรียกใหม่ที่ลืมจำกัดรูปแบบ"""
        import re
        with open("server.py", encoding="utf-8") as fh:
            src = fh.read()
        calls = re.findall(r"Image\.open\(([^)]*)\)", src)
        self.assertTrue(calls, "ไม่พบการเรียก Image.open เลย — เทสต์ล้าสมัย")
        for args in calls:
            self.assertIn(
                "formats=", args,
                f"Image.open({args}) ไม่ได้จำกัดรูปแบบ — "
                "ต้องส่ง formats=SAFE_IMAGE_FORMATS",
            )


if __name__ == "__main__":
    unittest.main()
