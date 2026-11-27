"""🧪→💊 Lab-to-Order Autopilot ระยะ 1 (27 พ.ย. 2569) — กติกา, กันซ้ำ, ข้อความ, wiring"""
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hd_module import lab_autopilot as lap  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _domains(props):
    return sorted((p["domain"], p["action"]) for p in props)


class TestRules:
    def test_iron_deficient_and_overload(self):
        assert _domains(lap.evaluate({"ferritin": 150, "tsat": 25})) == [("iron", "iron_iv")]
        assert _domains(lap.evaluate({"ferritin": 300, "tsat": 15})) == [("iron", "iron_iv")]
        assert _domains(lap.evaluate({"ferritin": 900, "tsat": 30})) == [("iron", "iron_hold")]
        assert lap.evaluate({"ferritin": 400, "tsat": 30}) == []
        p = lap.evaluate({"ferritin": 150, "tsat": 25})[0]
        assert "Iron sucrose" in p["proposed"] and "Ferritin 150" in p["trigger_text"]

    def test_mbd_rules_with_corrected_calcium(self):
        props = lap.evaluate({"phos": 6.2, "ca": 8.0, "albumin": 3.0, "pth": 700})
        d = _domains(props)
        assert ("mbd", "binder_up") in d and ("mbd", "pth_treat") in d
        # Ca 8.0 + 0.8*(4-3) = 8.8 → ไม่ต่ำ
        assert ("mbd", "ca_review") not in d
        hi = lap.evaluate({"ca": 10.8, "pth": 800})
        assert any(x["action"] == "ca_review" for x in hi) and any("cinacalcet" in x["proposed"] for x in hi if x["action"] == "pth_treat")
        low = lap.evaluate({"pth": 90, "phos": 2.0})
        assert _domains(low) == [("mbd", "binder_hold"), ("mbd", "vitd_active_hold")]

    def test_vitd_hbv_hcv(self):
        assert _domains(lap.evaluate({"vitd": 18})) == [("vitd", "vitd_supp")]
        assert _domains(lap.evaluate({"hbsag": "Negative", "antihbs": 4})) == [("hbv", "hbv_booster")]
        assert lap.evaluate({"hbsag": "Negative", "antihbs": 120}) == []
        new_pos = lap.evaluate({"hbsag": "Positive"})
        assert new_pos[0]["action"] == "hbsag_pos" and new_pos[0]["priority"] == "urgent"
        assert lap.evaluate({"hbsag": "Positive"}, context={"prev_hbsag_pos": True}) == []
        assert lap.evaluate({"antihcv": "Reactive"})[0]["action"] == "hcv_pos"
        assert lap.evaluate({"antihcv": "Non-reactive"}) == []

    def test_no_hiv_anywhere(self):
        src = open(os.path.join(ROOT, "hd_module", "lab_autopilot.py"), encoding="utf-8").read()
        assert "antihiv" not in src.lower().replace("anti-hiv ไม่เกี่ยวข้อง", "") and "hiv," not in src.lower()

    def test_protocol_overrides_and_text(self):
        p = lap.protocol(lambda k, d="": {"lab_ap_p_high": "6.0", "lab_ap_iron_regimen": "Venofer 200 mg × 5"}.get(k, d))
        assert p["lab_ap_p_high"] == 6.0 and p["lab_ap_iron_regimen"] == "Venofer 200 mg × 5" and p["lab_ap_pth_high"] == 600.0
        props = lap.evaluate({"phos": 5.8, "ferritin": 100}, p)
        assert not any(x["action"] == "binder_up" for x in props) and props[0]["proposed"] == "Venofer 200 mg × 5"
        assert lap.is_enabled(lambda k, d="": "on") and not lap.is_enabled(lambda k, d="": "")


class FakeCur:
    def __init__(self, answers):
        self.answers = list(answers)
        self.sql = []

    def execute(self, q, params=None):
        self.sql.append((" ".join(q.split()), params))

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


class TestGenerate:
    def test_generate_creates_and_dedups(self):
        today = date.today()
        labs_rows = [(today, 120, 15, None, None, None, None, None, None, None, None)]   # ferritin 120 / tsat 15 → iron
        cur = FakeCur([
            [(7, "6526", "นาย ก")],   # patients
            labs_rows,                 # _latest_labs
            None,                      # last proposal for iron → none
            None,                      # existing same lab_date → none
            (41,),                     # RETURNING id
        ])
        created = lap.generate(FakeConn(cur))
        assert len(created) == 1 and created[0]["domain"] == "iron" and created[0]["id"] == 41 and created[0]["hn"] == "6526"
        ins = [p for q, p in cur.sql if q.startswith("INSERT INTO hd_lab_proposals")][0]
        assert ins[2] == "iron" and ins[5] == "iron_iv"
        # มี pending อยู่แล้ว → ไม่สร้างซ้ำ
        cur2 = FakeCur([[(7, "6526", "นาย ก")], labs_rows, ("pending", None)])
        assert lap.generate(FakeConn(cur2)) == []
        # เพิ่งตัดสินไป 10 วัน → เว้น
        cur3 = FakeCur([[(7, "6526", "นาย ก")], labs_rows, ("approved", datetime.now() - timedelta(days=10))])
        assert lap.generate(FakeConn(cur3)) == []

    def test_generate_urgent_ignores_interval(self):
        today = date.today()
        rows = [(today, None, None, None, None, None, None, None, "Positive", None, None)]
        cur = FakeCur([[(7, "6526", "นาย ก")], rows, [], ("approved", datetime.now() - timedelta(days=3)), None, (5,)])
        created = lap.generate(FakeConn(cur))
        assert len(created) == 1 and created[0]["priority"] == "urgent" and created[0]["action"] == "hbsag_pos"


class TestMessages:
    def test_line_summary_and_nurse_text(self):
        created = [{"id": 1, "hn": "6526", "name": "นาย ก", "domain": "iron", "action": "iron_iv", "priority": "routine", "trigger_text": "Ferritin 120 / TSAT 15%"},
                   {"id": 2, "hn": "7001", "name": "นาง ข", "domain": "hbv", "action": "hbsag_pos", "priority": "urgent", "trigger_text": "HBsAg Positive"}]
        msg = lap.line_summary(created, "https://x")
        assert "2 ข้อเสนอ" in msg and "🚨 ด่วน 1" in msg and msg.index("นาง ข") < msg.index("นาย ก") and "/hd/esa" in msg
        txt = lap.nurse_order_text({"name": "นาย ก", "hn": "6526", "trigger_text": "TSAT 15%", "proposed": "Iron IV", "rationale": "ขาดเหล็ก"}, "dr_k")
        assert "HN 6526" in txt and "Iron IV" in txt and "dr_k" in txt
        assert lap.line_summary([], "x") == ""


class TestWiring:
    def test_routes_settings_hooks(self):
        bp = open(os.path.join(ROOT, "hd_module", "blueprint.py"), encoding="utf-8").read()
        for needle in ('"/lab-ap/proposals", methods=["GET"]', '"/lab-ap/proposals/<int:pid>/decide"', '"/lab-ap/run"',
                       'def run_lab_autopilot', '_lab_ap_kick("verify")', '"lab_autopilot",', '"lab_ap_iron_regimen"',
                       'push_nurse_group(txt, fallback_recipients=False)'):
            assert needle in bp, needle
        init = open(os.path.join(ROOT, "hd_module", "__init__.py"), encoding="utf-8").read()
        assert 'run_guarded("lab_autopilot", run_lab_autopilot' in init
        tpl = open(os.path.join(ROOT, "hd_module", "templates", "hd_esa.html"), encoding="utf-8").read()
        assert "loadLab()" in tpl and "decideLab(" in tpl and "/hd/api/v1/lab-ap/run" in tpl
        st = open(os.path.join(ROOT, "hd_module", "templates", "hd_settings.html"), encoding="utf-8").read()
        assert 'id="setting_lab_autopilot"' in st and "'lab_ap_iron_regimen'" in st and "'lab_ap_pth_high'" in st
