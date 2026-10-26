"""หน้า LIFF ต้องไม่ยิง API ก่อนมี ID token

ที่มา: ทดสอบหน้า LIFF บน production จริงเมื่อ 26 ต.ค. 2026 พบ
#loadingLabs แสดงข้อความ
    ❌ ไม่สามารถโหลดข้อมูลได้
    HTTP 400
ขณะที่ตัวแปร idToken เป็น null และ liff.isLoggedIn() เป็น false

สาเหตุในโค้ด: acceptConsent() เรียก initApp() ทันทีโดยไม่ตรวจว่า
idToken ถูกตั้งค่าแล้วหรือยัง ถ้าผู้ป่วยเปิดลิงก์จาก browser (ไม่ได้
เปิดจากแอป LINE) initLiff() จะไปถึง liff.login() แล้ว return ทิ้ง
idToken เป็น null พอกดยอมรับ ระบบ POST {id_token: null} ไป
/liff/api/my-labs ซึ่งตอบ 400 "id_token required" ตัวจัดการ error
เดิมดักเฉพาะ 401 ส่วน 400 ตกไปเข้า throw ทั่วไป ผู้ป่วยจึงเห็นแค่
รหัส HTTP ที่ไม่บอกว่าต้องทำอะไรต่อ

เทสต์ชุดนี้รัน JavaScript ของหน้าจริงด้วย node พร้อม stub ของ liff,
document, localStorage และ fetch จึงพิสูจน์ "พฤติกรรม" ไม่ใช่แค่ว่า
มีข้อความบางคำอยู่ในไฟล์

ข้ามอัตโนมัติถ้าเครื่องไม่มี node — ไม่ทำให้ชุดทดสอบล้มในสภาพแวดล้อม
ที่ไม่มี JS runtime
"""
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent
_NODE = shutil.which("node")

_STUBS = r"""
// ── stub สภาพแวดล้อมเบราว์เซอร์เท่าที่หน้านี้ใช้ ──
const CALLS = {fetch: [], problems: [], loginCalled: 0};

function mkEl(id) {
  return {id, style: {}, innerHTML: "", textContent: "",
          parentElement: null, _display: "block"};
}
const ELS = {
  consentOverlay: mkEl("consentOverlay"),
  mainApp: mkEl("mainApp"),
  loadingLabs: mkEl("loadingLabs"),
  patientInfo: mkEl("patientInfo"),
  labList: mkEl("labList"),
};
const CARD = mkEl("consent-card");

globalThis.document = {
  getElementById: (id) => ELS[id] || mkEl(id),
  querySelector: (sel) => (sel === ".consent-card" ? CARD : mkEl(sel)),
  querySelectorAll: () => [],
  addEventListener: () => {},
};
globalThis.getComputedStyle = (el) => ({
  display: el && el.style && el.style.display ? el.style.display : "block",
});
const _store = {};
globalThis.localStorage = {
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
};
const _sess = {};
globalThis.sessionStorage = {
  getItem: (k) => (k in _sess ? _sess[k] : null),
  setItem: (k, v) => { _sess[k] = String(v); },
};
globalThis.fetch = async (url, opts) => {
  CALLS.fetch.push({url, body: opts && opts.body});
  const body = JSON.parse((opts && opts.body) || "{}");
  if (!body.id_token) {
    return {status: 400, ok: false, json: async () => ({error: "id_token required"})};
  }
  return {status: 200, ok: true,
          json: async () => ({labs: [], trends: {}, ckd: {}, last_visit: null})};
};
globalThis.Chart = function () { return {destroy() {}}; };
globalThis.console = {log(){}, error(...a){ CALLS.problems.push(String(a[0])); },
                      warn(){}, info(){}, debug(){}};
"""

_SCENARIOS = {
    # ยังไม่ได้ login และ liff.init ยังไม่ถูกเรียก — กรณีที่พบบน production
    "no_token_not_logged_in": r"""
globalThis.liff = {
  init: async () => {},
  isLoggedIn: () => false,
  isInClient: () => false,
  getIDToken: () => null,
  getOS: () => "web",
  getVersion: () => "2.31.2",
  login: () => { CALLS.loginCalled++; },
  closeWindow: () => {},
};
(async () => {
  await initLiff();
  // ผู้ป่วยกดยอมรับหลัง initLiff คืนค่าแล้ว
  await acceptConsent();
  console.log("");
  process.stdout.write(JSON.stringify({
    fetch_count: CALLS.fetch.length,
    login_called: CALLS.loginCalled,
    loading_html: ELS.loadingLabs.innerHTML,
    card_html: CARD.innerHTML,
  }));
})();
""",
    # login แล้วและมี token — ต้องยิง API พร้อม token จริง
    "has_token": r"""
globalThis.liff = {
  init: async () => {},
  isLoggedIn: () => true,
  isInClient: () => true,
  getIDToken: () => "real-id-token-xyz",
  getOS: () => "ios",
  getVersion: () => "2.31.2",
  login: () => { CALLS.loginCalled++; },
  closeWindow: () => {},
};
(async () => {
  await initLiff();
  await acceptConsent();
  process.stdout.write(JSON.stringify({
    fetch_count: CALLS.fetch.length,
    bodies: CALLS.fetch.map(c => c.body),
    loading_html: ELS.loadingLabs.innerHTML,
  }));
})();
""",
}


_TEST_LIFF_ID = "2012225688-testonly"


def _rendered_html() -> str:
    """HTML ที่ server ส่งออกจริง — ผ่านเส้นทางแทนค่า placeholder จริง

    ต้องเรียก get_liff_html() ไม่ใช่อ่าน LIFF_HTML ดิบ เพราะบั๊กที่พบ
    อยู่ใน "ขั้นตอนแทนค่า" ไม่ใช่ใน template
    """
    import liff_app
    liff_app.LIFF_ID = _TEST_LIFF_ID
    return liff_app.get_liff_html(api_base="")


def _page_js() -> str:
    m = re.search(r"<script>\n(.*?)\n</script>", _rendered_html(), re.S)
    if not m:
        raise AssertionError("ไม่พบบล็อก <script> ในหน้า LIFF")
    return m.group(1)


def _run(scenario: str) -> dict:
    js = _STUBS + "\n" + _page_js() + "\n" + _SCENARIOS[scenario]
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "harness.mjs"
        path.write_text(js, encoding="utf-8")
        proc = subprocess.run(
            [_NODE, str(path)], capture_output=True, text=True, timeout=60,
        )
    if proc.returncode != 0:
        raise AssertionError(
            f"harness ล้ม (rc={proc.returncode})\n{proc.stdout}\n{proc.stderr}"
        )
    out = proc.stdout.strip().split("\n")[-1]
    return json.loads(out)


class TestLiffIdSubstitution(unittest.TestCase):
    """การแทนค่า LIFF_ID ต้องไม่ทำลายด่านตรวจ "ยังไม่ได้ตั้งค่า"

    บั๊กที่พบบน production 26 ต.ค. 2026: get_liff_html ใช้
        html.replace("__LIFF_ID__", LIFF_ID)
    ซึ่ง str.replace ของ Python แทนค่าทุกจุด รวมถึงบรรทัดที่หน้าเว็บ
    ใช้ตรวจว่ายังไม่ได้ตั้งค่า หน้าที่ส่งออกไปจึงเป็น
        const LIFF_ID = '2012225688-Qu71sdil';
        if (!LIFF_ID || LIFF_ID === '2012225688-Qu71sdil') {   // จริงเสมอ
    ทำให้ liff.init() ไม่ถูกเรียกเลย หน้าผลแล็บใช้งานไม่ได้กับผู้ป่วย
    ทุกคน แม้ env จะถูกต้องครบ และข้อความที่ขึ้นยังชี้ให้ไปแก้ env
    ซึ่งไม่ใช่สาเหตุ

    เทสต์ชุดนี้ไม่ต้องใช้ node จึงรันได้ทุกที่
    """

    def test_ค่าที่แทนต้องอยู่เฉพาะบรรทัดประกาศ(self):
        html = _rendered_html()
        decl = f"const LIFF_ID = \"{_TEST_LIFF_ID}\""
        self.assertIn(
            decl, html,
            "ไม่พบบรรทัดประกาศ LIFF_ID ที่แทนค่าแล้ว",
        )
        self.assertEqual(
            html.count(_TEST_LIFF_ID), 1,
            "ค่า LIFF_ID ปรากฏมากกว่าหนึ่งจุด — "
            "แปลว่าไปแทนที่บรรทัดตรวจ sentinel ด้วย "
            "ซึ่งทำให้ liff.init() ไม่ถูกเรียกเลย",
        )

    def test_ด่านตรวจ_sentinel_ต้องยังอยู่ครบ(self):
        html = _rendered_html()
        self.assertIn(
            "LIFF_ID === '__LIFF_ID__'", html,
            "ด่านตรวจ 'ยังไม่ได้ตั้งค่า LIFF' ถูกแทนค่าทับ",
        )

    def test_ไม่มี_placeholder_ค้าง(self):
        html = _rendered_html()
        self.assertNotIn("const LIFF_ID = '__LIFF_ID__'", html)
        self.assertNotIn("'__API_BASE__'", html)

    def test_env_ว่างต้องได้ค่าว่างไม่ใช่_placeholder(self):
        """ถ้าไม่ตั้ง env ต้องได้ '' เพื่อให้ !LIFF_ID จับได้"""
        import liff_app
        prev = liff_app.LIFF_ID
        try:
            liff_app.LIFF_ID = ""
            html = liff_app.get_liff_html(api_base="")
        finally:
            liff_app.LIFF_ID = prev
        self.assertIn('const LIFF_ID = "";', html)

    def test_ค่าที่มีอักขระพิเศษต้องไม่หลุดออกนอก_string(self):
        """json.dumps ต้องกันค่าที่มี quote หรือ backslash"""
        import liff_app
        prev = liff_app.LIFF_ID
        try:
            liff_app.LIFF_ID = "abc';alert(1);//"
            html = liff_app.get_liff_html(api_base="")
        finally:
            liff_app.LIFF_ID = prev
        self.assertIn('const LIFF_ID = "abc\';alert(1);//";', html)
        self.assertNotIn("';alert(1);//';", html)


@unittest.skipUnless(_NODE, "ต้องมี node เพื่อรัน JavaScript ของหน้า LIFF")
class TestLiffTokenGuard(unittest.TestCase):

    def test_ไม่มี_token_ต้องไม่ยิง_api(self):
        """กรณีที่พบบน production — กดยอมรับทั้งที่ยังไม่มี token"""
        r = _run("no_token_not_logged_in")
        self.assertEqual(
            r["fetch_count"], 0,
            "ยิง /liff/api/my-labs โดยไม่มี token — server จะตอบ 400 "
            "และผู้ป่วยเห็นแค่รหัส HTTP",
        )

    def test_ไม่มี_token_ต้องบอกผู้ป่วยให้ชัด(self):
        r = _run("no_token_not_logged_in")
        shown = (r["loading_html"] or "") + (r["card_html"] or "")
        self.assertNotIn("HTTP 400", shown)
        self.assertIn(
            "LINE", shown,
            "ข้อความต้องบอกว่าต้องเปิดจาก LINE ไม่ใช่รหัส HTTP เปล่า ๆ",
        )

    def test_ไม่มี_token_ต้องพาไป_login(self):
        """ต้องพยายาม login ให้ ไม่ใช่ปล่อยให้ผู้ป่วยติดอยู่เฉย ๆ"""
        r = _run("no_token_not_logged_in")
        self.assertGreaterEqual(r["login_called"], 1)

    def test_มี_token_ต้องยิง_api_พร้อม_token(self):
        """การแข็งขึ้นนี้ต้องไม่ทำให้เส้นทางปกติพัง"""
        r = _run("has_token")
        self.assertEqual(r["fetch_count"], 1)
        body = json.loads(r["bodies"][0])
        self.assertEqual(body["id_token"], "real-id-token-xyz")
        self.assertNotIn("HTTP", r["loading_html"] or "")

    def test_ไม่ส่ง_user_id_ดิบไปกรองข้อมูลผู้ป่วย(self):
        """[SEC-1] ตัวระบุตัวตนที่ client กำหนดเองต้องไม่ถูกใช้กรองข้อมูล"""
        r = _run("has_token")
        body = json.loads(r["bodies"][0])
        for forbidden in ("user_id", "userId", "line_user_id", "owner_hash"):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
