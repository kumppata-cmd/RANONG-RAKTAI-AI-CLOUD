"""🔧 Machine Drift Radar (27 พ.ย. 2569) — slope, flags, collect wiring"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hd_module import drift_radar as dr  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D0 = date(2026, 11, 1)


def _sessions(vals, key="tmp", step_days=2):
    return [{"date": D0 + timedelta(days=i * step_days), key: v} for i, v in enumerate(vals)]


class TestSlope:
    def test_slope_per_week(self):
        pts = [(D0 + timedelta(days=7 * i), 100 + 20 * i) for i in range(4)]
        assert abs(dr.slope_per_week(pts) - 20.0) < 1e-6
        assert dr.slope_per_week(pts[:2]) is None
        assert dr.slope_per_week([(D0, 1), (D0 + timedelta(days=1), 2), (D0 + timedelta(days=2), 3)]) is None   # ช่วง < 7 วัน


class TestFlags:
    def test_tmp_rising_is_alert(self):
        s = _sessions([100, 105, 110, 130, 150, 170, 190, 210])
        a = dr.analyze_machine(s, [])
        assert a["level"] == "alert" and a["flags"][0]["metric"] == "tmp" and "TMP ขึ้น" in a["flags"][0]["text"]
        assert a["enough_data"] and len(a["series"]["tmp"]) == 8

    def test_stable_is_ok_and_short_data_not_judged(self):
        assert dr.analyze_machine(_sessions([120, 118, 122, 119, 121, 120, 123, 118]), [])["level"] == "ok"
        a = dr.analyze_machine(_sessions([100, 200, 300]), [])
        assert a["level"] == "ok" and not a["enough_data"]

    def test_vp_ap_watch_and_cond_alert(self):
        s = [{"date": D0 + timedelta(days=2 * i), "vp": 100 + 8 * i, "ap": -100 - 8 * i} for i in range(8)]
        a = dr.analyze_machine(s, [{"cond": 14.0}, {"cond": 14.1}, {"cond": 13.2}, {"cond": 14.0}, {"dtemp": 37.9}, {"dtemp": 37.8}, {"dtemp": 38.0}])
        metrics = {f["metric"]: f["level"] for f in a["flags"]}
        assert metrics.get("vp") == "watch" and metrics.get("ap") == "watch" and metrics.get("cond") == "alert" and metrics.get("dtemp") == "watch"
        assert a["level"] == "alert"


class FakeCur:
    def __init__(self, answers):
        self.answers = list(answers)
        self.sql = []

    def execute(self, q, p=None):
        self.sql.append(" ".join(q.split()))

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeConn:
    def __init__(self, cur):
        self.c = cur

    def cursor(self):
        return self.c

    def rollback(self):
        pass


class TestCollect:
    def test_collect_groups_by_bed_and_sorts(self):
        eq = [(1, "NIKKISO", "DBB-27", "SN1", 3, "active", None)]
        sess = [(3, D0 + timedelta(days=2 * i), 100 + 20 * i, 120, -100) for i in range(8)] + [(5, D0, 110, 100, -90)]
        recent = [(3, 14.0, 36.5)]
        water = [("bacteria", D0, "<10", "CFU/mL", "pass"), ("endotoxin", date(2026, 1, 1), "0.1", "EU/mL", "fail")]
        d = dr.collect(FakeConn(FakeCur([eq, sess, recent, water])), 28)
        assert [m["bed"] for m in d["machines"]] == [3, 5]
        assert d["machines"][0]["name"] == "NIKKISO DBB-27" and d["machines"][0]["level"] == "alert"
        assert "ไม่มีทะเบียน" in d["machines"][1]["name"]
        assert d["summary"]["alert"] == 1 and any(w["level"] == "alert" for w in d["water"]) and any(w["overdue"] for w in d["water"])

    def test_wiring(self):
        bp = open(os.path.join(ROOT, "hd_module", "blueprint.py"), encoding="utf-8").read()
        assert '"/drift-radar", methods=["GET"]' in bp and '@hd_web.route("/drift-radar")' in bp
        base = open(os.path.join(ROOT, "hd_module", "templates", "hd_base.html"), encoding="utf-8").read()
        assert "/hd/drift-radar" in base
        assert os.path.exists(os.path.join(ROOT, "hd_module", "templates", "hd_drift_radar.html"))
