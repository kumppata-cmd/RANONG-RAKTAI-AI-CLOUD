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
import os
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



class TestPipAuditTargetVersion(unittest.TestCase):
    """สคริปต์รายงาน CVE ต้องบอก "เวอร์ชันเป้าหมาย" ไม่ใช่ตัวแรกที่เจอ

    ที่มา: 26 ต.ค. 2026 ต้อง upgrade pypdf สี่รอบในวันเดียว
    (5.1.0 -> 6.7.1 -> 6.10.2 -> 6.13.1 -> 6.15.0) เพราะ advisory
    แต่ละรายการบอกเวอร์ชันที่แก้ "รายการนั้น" ซึ่งไม่ใช่ตัวเดียวกัน
    การ upgrade ตามตัวแรกที่เห็นจึงยังเหลือ CVE ค้างทุกครั้ง
    """

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_paa", str(_ROOT / "scripts" / "pip_audit_annotate.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_เรียงเวอร์ชันแบบตัวเลขไม่ใช่สตริง(self):
        """6.9.1 ต้องน้อยกว่า 6.10.0 — เทียบสตริงจะได้ผลกลับกัน"""
        k = self.mod._ver_key
        self.assertLess(k("6.9.1"), k("6.10.0"))
        self.assertLess(k("6.2.0"), k("6.19.0"))
        self.assertEqual(max(["6.9.1", "6.10.0", "6.2.0"], key=k), "6.10.0")

    def test_ทนเวอร์ชันที่มีตัวอักษร(self):
        k = self.mod._ver_key
        self.assertIsInstance(k("6.1.0rc1"), tuple)
        self.assertIsInstance(k("6.1.0.post1"), tuple)

    def test_รายงานเวอร์ชันสูงสุดต่อแพ็กเกจ(self):
        import json as _json
        import tempfile
        import io
        import contextlib

        report = {"dependencies": [{
            "name": "pkg", "version": "1.0", "vulns": [
                {"id": "A", "aliases": [], "fix_versions": ["1.9.1"],
                 "description": ""},
                {"id": "B", "aliases": [], "fix_versions": ["1.10.0"],
                 "description": ""},
                {"id": "C", "aliases": [], "fix_versions": ["1.2.0"],
                 "description": ""},
            ]}]}
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8",
        ) as fh:
            _json.dump(report, fh)
            path = fh.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mod.main_argv = None
            import sys as _sys
            prev = _sys.argv
            _sys.argv = ["x", path, "1"]
            try:
                self.mod.main()
            finally:
                _sys.argv = prev
        out = buf.getvalue()
        self.assertIn("1.10.0", out, "ต้องรายงานเวอร์ชันสูงสุด")
        self.assertIn("pip-audit target", out)

if __name__ == "__main__":
    unittest.main()
