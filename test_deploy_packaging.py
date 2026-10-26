"""Dockerfile ต้องคัดลอก module ที่แอปต้องใช้ครบทุกตัว

ที่มา: 26 ต.ค. 2026 deploy dep-dbfmtvbphbkc73ajme0g ล้มด้วย
    File "/app/server.py", line 57, in <module>
      from vendor_registry import (
    ModuleNotFoundError: No module named 'vendor_registry'
    Reason: Worker failed to boot.

ขณะนั้น CI เขียวทุก job — py_compile, unittest 1,358 ข้อ, flake8,
security checks ผ่านหมด เพราะทุกด่านรันในโฟลเดอร์ repo ที่มีไฟล์อยู่
ส่วน Dockerfile ใช้ COPY รายไฟล์ 31 บรรทัดที่ต้องแก้มือ และไฟล์ใหม่
ไม่ได้ถูกเพิ่มเข้าไป production รอดเพราะ Render คง build เดิมไว้
ไม่ใช่เพราะมีด่านไหนจับได้

เทสต์นี้ทำให้ความผิดพลาดแบบเดียวกันกลายเป็น test แดงในเครื่อง
ตั้งแต่ก่อน push — ไม่ต้องรอ CI และไม่ต้องรอ deploy ล้ม
"""
import pathlib
import subprocess
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent
_CHECKER = _ROOT / "scripts" / "check_dockerfile_modules.py"


class TestDockerfilePackaging(unittest.TestCase):

    def test_checker_มีอยู่จริง(self):
        self.assertTrue(
            _CHECKER.exists(),
            "ไม่พบ scripts/check_dockerfile_modules.py — "
            "ด่านกัน ModuleNotFoundError ตอน deploy หายไป",
        )

    def test_dockerfile_คัดลอก_module_ครบ(self):
        proc = subprocess.run(
            [sys.executable, "-I", str(_CHECKER)],
            cwd=str(_ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            proc.returncode, 0,
            "Dockerfile คัดลอก module ไม่ครบ — "
            "gunicorn จะ boot ไม่ขึ้นตอน deploy\n\n"
            + proc.stdout + proc.stderr,
        )

    def test_ci_รัน_ด่านนี้ทุก_push(self):
        """ด่านนี้ต้องอยู่ใน ci.yml ไม่ใช่แค่ deploy-check.yml

        deploy-check.yml มี job ตรวจ docker build อยู่แล้ว และจะจับ
        ความล้มของ 26 ต.ค. ได้ แต่รันเฉพาะ workflow_dispatch (สั่งมือ)
        จึงไม่เคยรันตอน merge
        """
        ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        self.assertIn("check_dockerfile_modules.py", ci)
        self.assertIn("docker build", ci)
        self.assertIn("/health", ci)


if __name__ == "__main__":
    unittest.main()
