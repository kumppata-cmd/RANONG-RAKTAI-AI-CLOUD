"""🪪 Dialysis Passport (27 พ.ย. 2569) — token lifecycle, data assembly (no Anti-HIV), access warning, wiring"""
import os
import sys
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hd_module import passport as pp  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeCur:
    """บันทึก SQL ที่ถูกเรียก และตอบตามคิว"""
    def __init__(self, answers=None):
        self.sql = []
        self.answers = list(answers or [])
        self.rowcount = 1

    def execute(self, q, params=None):
        self.sql.append((" ".join(q.split()), params))

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []


NOW = datetime.now(timezone.utc)


def _tok_row(i=1, hn="6526", via="staff", exp_h=72, revoked=None, views=0):
    return (i, hn, "tok" * 11, NOW, NOW + timedelta(hours=exp_h), "hd_user:1", via, revoked, views, None)


class TestSwitchAndToken:
    def test_switch_and_ttl(self):
        assert pp.is_enabled(lambda k, d="": "on") is True
        assert pp.is_enabled(lambda k, d="": "off") is False
        assert pp.ttl_hours(lambda k, d: "48") == 48
        assert pp.ttl_hours(lambda k, d: "99999") == 24 * 30
        assert pp.ttl_hours(lambda k, d: "bad") == pp.DEFAULT_TTL_HOURS

    def test_new_token_random_urlsafe(self):
        a, b = pp.new_token(), pp.new_token()
        assert a != b and len(a) >= 30 and all(c.isalnum() or c in "-_" for c in a)

    def test_lookup_states(self):
        assert pp.lookup(FakeCur(), "short")[0] == "missing"
        assert pp.lookup(FakeCur([None]), "x" * 32)[0] == "missing"
        assert pp.lookup(FakeCur([_tok_row(revoked=NOW)]), "x" * 32)[0] == "revoked"
        assert pp.lookup(FakeCur([_tok_row(exp_h=-1)]), "x" * 32)[0] == "expired"
        st, row = pp.lookup(FakeCur([_tok_row()]), "x" * 32)
        assert st == "active" and row["hn"] == "6526"

    def test_issue_reuses_active_same_via(self):
        cur = FakeCur([[_tok_row(via="portal", exp_h=50)]])
        row = pp.issue(cur, "6526", "patient_portal", via="portal", hours=72)
        assert row["id"] == 1 and not any("INSERT" in q for q, _ in cur.sql)

    def test_issue_inserts_and_caps_active(self):
        act = [_tok_row(3, via="staff"), _tok_row(2, via="staff"), _tok_row(1, via="staff")]
        cur = FakeCur([act, _tok_row(9, via="portal")])
        row = pp.issue(cur, "6526", "patient_portal", via="portal", hours=72)
        assert row["id"] == 9
        assert any("UPDATE hd_passport_tokens SET revoked_at" in q and p == (1,) for q, p in cur.sql)   # ตัวเก่าสุดถูกปิด
        ins = [p for q, p in cur.sql if q.startswith("INSERT")][0]
        assert ins[0] == "6526" and ins[2] == "72" and ins[4] == "portal"

    def test_revoke_scoped_to_hn(self):
        cur = FakeCur()
        assert pp.revoke(cur, 5, "6526")
        assert cur.sql[-1][1] == (5, "6526")


class TestAssemble:
    def test_assemble_never_shows_hiv_and_maps_fields(self):
        dob = date(1960, 5, 1)
        patient = (10, "นาย ก", dob, "ชาย", "O+", "Penicillin", "ESRD", "AVF left arm", 62.5, "จ พ ศ", "Epo", "4000 u")
        sess = (date(2026, 11, 25), "FX80", 300, 500, "2000 u", "AVF", NOW - timedelta(hours=4), NOW, 2500, 63.0)
        labs = [(date(2026, 11, 20), 5.1, 10.2, "Negative", 120, "Negative", 9.1, 4.8, 8.2)]
        meds = [("Calcium carbonate", "1 g", "PO", "tid")]
        cur = FakeCur([patient, sess, labs, meds])
        d = pp.assemble(cur, "6526", get_setting=lambda k, dflt="": {"clinic_name": "คลินิก X", "clinic_phone": "077-000"}.get(k, dflt))
        assert d["profile"]["name"] == "นาย ก" and d["profile"]["dry_weight"] == 62.5 and d["profile"]["age"] >= 65
        assert d["prescription"]["dialyzer"] == "FX80" and d["prescription"]["hours"] == 4.0 and d["prescription"]["bfr"] == 300
        assert d["labs"]["k"]["value"] == 5.1 and d["labs"]["hbsag"]["value"] == "Negative" and d["labs"]["antihbs"]["value"] == 120
        assert not any(k in d["labs"] for k in pp.NEVER_SHOW)
        lab_sql = [q for q, _ in cur.sql if "FROM hd_labs" in q][0]
        assert "hiv" not in lab_sql.lower()
        assert d["medications"][0]["name"] == "Calcium carbonate" and d["clinic"]["phone"] == "077-000"

    def test_assemble_unknown_patient(self):
        assert pp.assemble(FakeCur([None]), "0000", get_setting=lambda k, d="": d) is None

    def test_access_warning(self):
        w = pp.access_warning("AVF ซ้าย")
        assert w and "ซ้าย" in w["th"] and "LEFT" in w["en"]
        assert "RIGHT" in pp.access_warning("Right AVG")["en"]
        assert pp.access_warning("Perm cath") is None and pp.access_warning(None) is None


class TestWiring:
    def test_routes_settings_templates(self):
        bp = open(os.path.join(ROOT, "hd_module", "blueprint.py"), encoding="utf-8").read()
        for needle in ('"/patients/hn/<hn>/passport", methods=["POST"]', '"/my/<token>/passport", methods=["POST"]',
                       '@hd_web.route("/passport/<token>")', '"passport_enabled",', '"passport_ttl_hours",',
                       'X-Robots-Tag', 'action="passport.view"'):
            assert needle in bp, needle
        # หน้าสาธารณะต้องไม่ใช้ require_role และต้องนับ/ตรวจสวิตช์
        i = bp.index('@hd_web.route("/passport/<token>")')
        assert "require_role" not in bp[i:i + 200] and "pp.is_enabled()" in bp[i:i + 1500]
        tpl = open(os.path.join(ROOT, "hd_module", "templates", "hd_passport.html"), encoding="utf-8").read()
        assert 'name="robots" content="noindex' in tpl and "<script src=" not in tpl and "hiv" not in tpl.lower()
        st = open(os.path.join(ROOT, "hd_module", "templates", "hd_settings.html"), encoding="utf-8").read()
        assert 'id="setting_passport_enabled"' in st and 'id="setting_passport_ttl_hours"' in st
        portal = open(os.path.join(ROOT, "hd_module", "templates", "hd_patient_portal.html"), encoding="utf-8").read()
        assert "issueMyPassport" in portal and "/passport/revoke" in portal
        detail = open(os.path.join(ROOT, "hd_module", "templates", "hd_patient_detail.html"), encoding="utf-8").read()
        assert "issuePassport()" in detail

    def test_public_template_renders_all_states(self):
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "hd_module", "templates")))
        t = env.get_template("hd_passport.html")
        for st in ("expired", "revoked", "rate", "disabled", "unavailable", "missing"):
            html = t.render(status=st, lang="th", data=None, token="x")
            assert "<html" in html and "🪪" not in html.split("<body")[1][:50]
        data = {"profile": {"hn": "6526", "name": "นาย ก", "age": 66, "gender": "ชาย", "blood_type": "O+", "allergies": "Penicillin",
                            "diagnosis": "ESRD", "vascular_access": "AVF ซ้าย", "dry_weight": 62.5, "schedule_days": "จ พ ศ", "esa": "Epo 4000 u"},
                "prescription": {"date": "2026-11-25", "dialyzer": "FX80", "bfr": 300, "dfr": 500, "heparin": "2000 u", "hours": 4.0,
                                 "uf_goal": 2500, "post_weight": 63.0, "access": "AVF"},
                "labs": {"k": {"value": 5.1, "date": "2026-11-20"}, "hbsag": {"value": "Negative", "date": "2026-11-20"}},
                "medications": [{"name": "CaCO3", "dose": "1 g", "route": "PO", "freq": "tid"}],
                "clinic": {"name": "คลินิก X", "phone": "077-000"}, "generated_at": "2026-11-27 20:00"}
        for lang in ("th", "en"):
            html = t.render(status="active", lang=lang, data=data, token="x", access_warn=pp.access_warning("AVF ซ้าย"), expires_at="30/11/2569 20:00")
            assert "นาย ก" in html and "62.5 kg" in html and "FX80" in html and "Penicillin" in html and "Negative" in html
            assert ("NO BP cuff" in html) == (lang == "en")
