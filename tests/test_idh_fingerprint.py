"""🧬 IDH Fingerprint (27 พ.ย. 2569) — จุดหักส่วนตัวจากรอบของผู้ป่วยเอง"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hd_module import idh_fingerprint as fp  # noqa: E402
from hd_module import idh_twin as twin  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rows(sbps, ufs, step=30):
    return [{"min": i * step, "sbp": s, "total_uf": u} for i, (s, u) in enumerate(zip(sbps, ufs))]


def _sess(sbps, ufs, d="2026-11-01", dw=60.0, hours=4.0):
    return {"date": d, "dw": dw, "hours": hours, "rows": _rows(sbps, ufs)}


IDH_A = _sess([150, 140, 128, 98, 85, 95, 110, 115], [0, 400, 800, 1200, 1600, 1800, 2000, 2200])   # เริ่มตก (−22) นาที 60 ที่ UF 800 · IDH (min 85)
IDH_B = _sess([145, 135, 120, 96, 88, 100, 110, 112], [0, 300, 700, 1100, 1500, 1800, 2000, 2100])  # เริ่มตก (−25) นาที 60 ที่ UF 700
OK_1 = _sess([140, 138, 135, 130, 128, 126, 125, 124], [0, 300, 600, 900, 1200, 1500, 1700, 1800])
OK_2 = _sess([150, 146, 142, 140, 136, 134, 132, 130], [0, 300, 600, 900, 1200, 1500, 1800, 2000])
OK_3 = _sess([138, 136, 133, 131, 130, 128, 127, 125], [0, 250, 500, 800, 1100, 1300, 1500, 1600])


class TestAnalyze:
    def test_single_session_break_and_idh(self):
        a = fp.analyze_session(IDH_A["rows"], 60.0, 4.0)
        assert a["idh"] and a["break_min"] == 60 and a["break_uf"] == 800 and a["max_uf"] == 2200 and round(a["rate_kg_h"], 1) == 9.2
        b = fp.analyze_session(OK_1["rows"], 60.0, 4.0)
        assert not b["idh"] and b["break_min"] is None
        assert fp.analyze_session([{"min": 0, "sbp": 120, "total_uf": 0}], 60, 4) is None

    def test_fingerprint_summary(self):
        f = fp.fingerprint([IDH_A, IDH_B, OK_1, OK_2, OK_3])
        assert f["enough"] and f["n_sessions"] == 5 and f["n_idh"] == 2 and f["idh_rate_pct"] == 40
        assert f["uf_break_ml"] == 750 and f["time_break_min"] == 60 and f["pattern"] == "early"
        assert f["safe_uf_ml"] == 1600 and f["max_uf_tolerated_ml"] == 2000
        assert f["rate_ok_kg_h"] and f["rate_bad_kg_h"]
        t = fp.summary_text(f)
        assert "จุดหักส่วนตัว ≈ UF 750" in t and "ซองปลอดภัย ≤ 1,600" in t and "ครึ่งแรก" in t

    def test_not_enough(self):
        f = fp.fingerprint([IDH_A, OK_1])
        assert not f["enough"] and "ยังไม่พอ" in fp.summary_text(f)


class TestPlanAndLive:
    def test_plan_check_levels(self):
        f = fp.fingerprint([IDH_A, IDH_B, OK_1, OK_2, OK_3])
        assert fp.plan_check(f, 2500)["level"] == "high" and fp.plan_check(f, 2500)["over_ml"] == 900
        assert fp.plan_check(f, 1700)["level"] == "high"            # เกินทั้งจุดหัก (750) และซอง (1600)
        assert fp.plan_check(f, 1500)["level"] == "moderate"        # เกินจุดหักแต่ยังในซอง
        assert fp.plan_check(f, 700)["level"] == "low"
        assert fp.plan_check({"enough": False}, 2000) is None

    def test_live_factor_and_twin_integration(self):
        f = fp.fingerprint([IDH_A, IDH_B, OK_1, OK_2, OK_3])
        assert fp.live_factor(f, 1100, 30)[0]["points"] == 15          # ≥90% ของจุดหัก UF
        assert fp.live_factor(f, 500, 85)[0]["points"] == 8            # ถึงเวลาที่เคยตก
        assert fp.live_factor(f, 500, 30) == []
        base = {"uf_goal": 2000, "dry_weight": 60, "duration_hours": 4, "sbp_series": [140, 138, 136], "uf_removed_now": 1100, "elapsed_min": 60}
        r0 = twin.score_live(dict(base))
        r1 = twin.score_live(dict(base, fingerprint=f))
        assert r1["score"] == r0["score"] + 15 and any("จุดหักส่วนตัว" in x["text"] for x in r1["factors"])


class TestWiring:
    def test_routes_and_template(self):
        bp = open(os.path.join(ROOT, "hd_module", "blueprint.py"), encoding="utf-8").read()
        assert '"/patients/hn/<hn>/idh-fingerprint"' in bp and 'out["plan_check"] = plan_check(fp, f.get("uf_goal"))' in bp
        tpl = open(os.path.join(ROOT, "hd_module", "templates", "hd_session.html"), encoding="utf-8").read()
        assert 'id="idhFp"' in tpl and 'id="idhPlan"' in tpl and "fingerprint_text" in tpl
        tw = open(os.path.join(ROOT, "hd_module", "idh_twin.py"), encoding="utf-8").read()
        assert "fingerprint_for(cur, r[1], exclude_session=session_id)" in tw
