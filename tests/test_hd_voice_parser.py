"""Voice → flowsheet parser (ว้าว #4) — runs the real JS under node."""
import json
import os
import shutil
import subprocess
import unittest

JS = os.path.join(os.path.dirname(__file__), "..", "hd_module", "static", "hd_voice.js")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class VoiceParserTests(unittest.TestCase):
    def parse(self, text):
        out = subprocess.run(["node", "-e", "const m=require(process.argv[1]);console.log(JSON.stringify(m.parseVoice(process.argv[2]).fields))",
                              os.path.abspath(JS), text], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_thai_sentence(self):
        f = self.parse("ความดัน 130 ทับ 80 ชีพจร 72 BFR 300 VP 120 AP ลบ 150 UF 1200 หมายเหตุ เวียนหัวเล็กน้อย")
        self.assertEqual((f["sbp"], f["dbp"], f["pr"], f["bfr"], f["vp"], f["ap"], f["total_uf"]), (130, 80, 72, 300, 120, -150, 1200))
        self.assertEqual(f["remark"], "เวียนหัวเล็กน้อย")

    def test_english_short(self):
        f = self.parse("bp 110/70 pr 88 tmp 40 ufr 800 temp 36.5")
        self.assertEqual(f, {"sbp": 110, "dbp": 70, "pr": 88, "temp": 36.5, "tmp": 40, "ufr": 800})

    def test_nothing(self):
        self.assertEqual(self.parse("สวัสดี"), {})


if __name__ == "__main__":
    unittest.main()
