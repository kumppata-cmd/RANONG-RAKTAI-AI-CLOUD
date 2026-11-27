"""🩸 Kidney Twin (27 พ.ย. 2569) — live score factors, alert decision, outcome, wiring"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hd_module import idh_twin as twin  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime.now(timezone.utc)


class TestScoreLive:
    def test_live_factors_add_on_top_of_base(self):
        base = {"uf_goal": 2000, "dry_weight": 60, "duration_hours": 4, "sbp_series": [150, 148, 146]}
        r0 = twin.score_live(dict(base))
        live = dict(base, uf_rate_now=900, uf_removed_now=1500, elapsed_min=120, hr_first=70, hr_last=95)
        r1 = twin.score_live(live)
        assert r1["score"] > r0["score"]
        texts = " | ".join(x["text"] for x in r1["factors"])
        assert "UF rate ตอนนี้" in texts and "ดึงน้ำเร็วกว่าเวลา" in texts and "HR ขึ้น" in texts
        assert r1["score"] <= 100 and r1["level"] in ("low", "moderate", "high")

    def test_high_level_has_advice_and_recs(self):
        f = {"uf_goal": 3500, "dry_weight": 55, "duration_hours": 4, "pre_weight": 58.0,
             "sbp_series": [140, 125, 110, 98], "idh_history": 3, "uf_rate_now": 900, "elapsed_min": 170}
        r = twin.score_live(f)
        assert r["level"] == "high" and r["advice"] and r["recommendations"]

    def test_quiet_session_stays_low(self):
        f = {"uf_goal": 1500, "dry_weight": 70, "duration_hours": 4, "sbp_series": [130, 132, 128], "uf_rate_now": 375, "elapsed_min": 60}
        r = twin.score_live(f)
        assert r["level"] == "low"


class TestAlertDecision:
    cfg = dict(twin.DEFAULTS, channel="group")

    def test_first_then_suppressed_then_realert(self):
        assert twin.should_alert(None, 45, self.cfg, NOW) == (False, "below")
        assert twin.should_alert(None, 62, self.cfg, NOW) == (True, "first")
        st = {"alerted_at": NOW - timedelta(minutes=10), "alert_score": 62}
        assert twin.should_alert(st, 70, self.cfg, NOW)[0] is False
        st = {"alerted_at": NOW - timedelta(minutes=50), "alert_score": 62}
        assert twin.should_alert(st, 70, self.cfg, NOW)[0] is False          # ขึ้น 8 < 15
        assert twin.should_alert(st, 80, self.cfg, NOW) == (True, "re-alert")

    def test_alert_text_mentions_bed_and_factors(self):
        res = {"score": 72, "level": "high", "factors": [{"text": "UF 14.0 mL/kg/h (≥13)", "points": 25}],
               "recommendations": [{"icon": "⏱", "text": "ยืดเวลา", "priority": "high"}], "advice": "วัด BP ทุก 15 นาที"}
        t = twin.alert_text(3, "นาย ก", "6526", res, "first")
        assert "เตียง 3" in t and "72/100" in t and "UF 14.0" in t and "ยืดเวลา" in t and "ข้อเสนอจากระบบ" in t
        assert "ซ้ำ" in twin.alert_text(3, "นาย ก", "6526", res, "re-alert")

    def test_config_reads_settings(self):
        c = twin.config(lambda k, d="": {"idh_twin_high": "55", "idh_twin_channel": "card"}.get(k, d))
        assert c["idh_twin_high"] == 55.0 and c["channel"] == "card" and c["idh_twin_realert_min"] == 45.0
        assert twin.is_enabled(lambda k, d="": "on") and not twin.is_enabled(lambda k, d="": "off")


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
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class TestOutcome:
    def test_record_outcomes_idh_and_no_idh(self):
        cur = FakeCur([
            [(11,), (12,)],                       # sessions needing outcome
            [(130,), (120,), (85,)],              # s11 SBP → min 85 → idh
            (False,),                             # worksheet comp
            [(140,), (138,), (135,)],             # s12 → no idh
            (False,),
        ])
        n = twin.record_outcomes(FakeConn(cur))
        assert n == 2
        ups = [p for q, p in cur.sql if q.startswith("UPDATE hd_idh_twin_state SET outcome")]
        assert ups[0][0] == "idh" and "min SBP 85" in ups[0][1] and ups[1][0] == "no_idh"

    def test_record_outcome_drop_rule_and_worksheet(self):
        cur = FakeCur([[(13,), (14,)], [(160,), (140,), (128,)], (False,), [(120,), (118,)], (True,)])
        twin.record_outcomes(FakeConn(cur))
        ups = [p for q, p in cur.sql if q.startswith("UPDATE hd_idh_twin_state SET outcome")]
        assert ups[0][0] == "idh" and ups[1][0] == "idh" and "worksheet" in ups[1][1]

    def test_stats_math(self):
        cur = FakeCur([[("idh", True, 3), ("idh", False, 1), ("no_idh", True, 2), ("no_idh", False, 10)]])
        st = twin.stats(FakeConn(cur), 30)
        assert st["sensitivity_pct"] == 75.0 and st["precision_pct"] == 60.0 and st["sessions"] == 16


class TestWiring:
    def test_hook_routes_settings(self):
        bp = open(os.path.join(ROOT, "hd_module", "blueprint.py"), encoding="utf-8").read()
        for needle in ("@hd_bp.after_request", "_TWIN_PATH_RE", '"/sessions/<int:session_id>/idh-twin"', '"/idh-twin/stats"',
                       "def run_idh_twin_outcomes", '"idh_twin", "idh_twin_channel"', "_twin.score_live(feats)"):
            assert needle in bp, needle
        import re
        m = re.search(r'_TWIN_PATH_RE = _re_twin\.compile\(r"([^"]+)"\)', bp)
        rx = re.compile(m.group(1))
        assert rx.match("/hd/api/v1/sessions/42/vitals") and rx.match("/hd/api/v1/sessions/42/intra-record")
        assert not rx.match("/hd/api/v1/sessions/42/vitals/7/time") and not rx.match("/hd/api/v1/sessions/42/idh-twin")
        init = open(os.path.join(ROOT, "hd_module", "__init__.py"), encoding="utf-8").read()
        assert 'run_guarded("idh_twin_outcomes"' in init
        st = open(os.path.join(ROOT, "hd_module", "templates", "hd_settings.html"), encoding="utf-8").read()
        assert 'id="setting_idh_twin"' in st and 'id="setting_idh_twin_channel"' in st
