"""ข้อ 1, 8, 9, 10 (17 พ.ย. 2569) — เพดาน UF เฉพาะราย, อ่านหน้าจอเครื่อง, โค้ชน้ำหนัก, IDWG"""
import unittest
from datetime import date

from hd_module import uf_coach as uc


def W(t, x, y, w=None, h=20):
    return {"t": t, "x": x, "y": y, "w": w if w is not None else max(10, len(t) * 11), "h": h}


class MachineScreenTests(unittest.TestCase):
    def test_nikkiso_dbb07_labels_above_liters(self):
        words = [W("Treat.time", 110, 465), W("remaining", 110, 490), W("UF", 262, 465), W("volume", 290, 465),
                 W("UF", 410, 465), W("rate", 440, 465), W("BP1", 700, 465),
                 W("0:06", 140, 525, 90, 40), W("3,91", 300, 525, 80, 40), W("1.01", 450, 525, 80, 40), W("300", 730, 525, 80, 40)]
        r = uc.parse_machine_words(words)
        self.assertEqual(r["uf_removed"], 3910)
        self.assertEqual(r["uf_rate"], 1010)
        self.assertEqual(r["time_left_min"], 6)

    def test_fresenius_5008_ml_and_bp(self):
        words = [W("UF", 300, 400), W("goal", 330, 400), W("UF", 480, 400), W("rate", 510, 400), W("UF", 660, 400), W("volume", 690, 400),
                 W("2500", 300, 455, 70, 30), W("333", 500, 455, 60, 30), W("2405", 700, 455, 70, 30),
                 W("0:17", 500, 800, 70, 30), W("Bld.", 880, 1005), W("press.", 930, 1005), W("133", 880, 1060, 50, 26), W("/", 935, 1060, 10, 26), W("69", 950, 1060, 30, 26)]
        r = uc.parse_machine_words(words)
        self.assertEqual((r["uf_goal"], r["uf_rate"], r["uf_removed"]), (2500, 333, 2405))
        self.assertEqual(r["uf_remaining"], 95)
        self.assertEqual((r["sbp"], r["dbp"]), (133, 69))

    def test_fresenius_4008s_value_right_of_label(self):
        words = [W("UF", 760, 580), W("Volume", 790, 580), W("2000", 900, 575, 60, 30),
                 W("UF", 760, 640), W("Time", 790, 640), W("Left", 840, 640), W("0:00", 900, 635, 60, 30),
                 W("UF", 760, 700), W("Rate", 790, 700), W("0", 940, 695, 15, 30),
                 W("UF", 760, 760), W("Goal", 790, 760), W("2000", 900, 755, 60, 30)]
        r = uc.parse_machine_words(words)
        self.assertEqual((r["uf_removed"], r["uf_goal"], r["uf_rate"], r["time_left_min"]), (2000, 2000, 0, 0))

    def test_nikkiso_dbb27_and_new_model(self):
        words = [W("UF", 455, 185), W("VOLUME", 480, 185), W("UF", 590, 185), W("GOAL", 615, 185), W("UF", 690, 185), W("RATE", 715, 185),
                 W("4.19", 460, 225, 70, 50), W("4.20", 600, 225, 70, 50), W("041", 700, 225, 70, 50),
                 W("REMAIN", 760, 790), W("TIME", 830, 790), W("0:02", 790, 810, 40, 18)]
        r = uc.parse_machine_words(words)
        self.assertEqual((r["uf_removed"], r["uf_goal"], r["uf_rate"]), (4190, 4200, 410))
        self.assertEqual(r["time_left_min"], 2)
        new = [W("UF", 400, 95, 20, 12), W("volume", 422, 95, 40, 12), W("UF", 480, 95, 20, 12), W("rate", 502, 95, 30, 12),
               W("0,07", 410, 115, 40, 22), W("0,38", 485, 115, 40, 22),
               W("SYS", 60, 160, 30, 12), W("127", 60, 180, 50, 30), W("DIA", 60, 230, 30, 12), W("83", 60, 250, 40, 30),
               W("Pulse", 60, 300, 40, 12), W("85", 60, 320, 40, 30)]
        r2 = uc.parse_machine_words(new)
        self.assertEqual((r2["uf_removed"], r2["uf_rate"], r2["sbp"], r2["dbp"], r2["hr"]), (70, 380, 127, 83, 85))

    def test_text_fallback_and_garbage(self):
        r = uc.parse_machine_text("UF goal 2500 UF rate 333 UF volume 2405 Bld. press 133/69")
        self.assertEqual((r["uf_goal"], r["uf_rate"], r["uf_removed"], r["sbp"]), (2500, 333, 2405, 133))
        self.assertEqual({k for k in uc.parse_machine_words([W("hello", 0, 0)]) if k != "raw"}, set())


class ToleranceTests(unittest.TestCase):
    def test_insufficient(self):
        r = uc.tolerance_from_sessions([{"rate_kg": 9, "idh": False}] * 5)
        self.assertEqual(r["status"], "insufficient")

    def test_no_pattern(self):
        r = uc.tolerance_from_sessions([{"rate_kg": 11, "idh": False}] * 12 + [{"rate_kg": 12, "idh": True}])
        self.assertEqual(r["status"], "no_pattern")

    def test_personal_ceiling_below_standard(self):
        ss = [{"rate_kg": 7.5, "idh": False}] * 8 + [{"rate_kg": 9.5, "idh": True}, {"rate_kg": 10.2, "idh": True},
                                                   {"rate_kg": 11.0, "idh": True}, {"rate_kg": 9.0, "idh": False}]
        r = uc.tolerance_from_sessions(ss)
        self.assertEqual(r["status"], "personal")
        self.assertEqual(r["personal_ceiling"], 9.0)
        self.assertIn("ทนได้ราว 9", r["text"])


class IdwgAndCoachTests(unittest.TestCase):
    def test_limit_pct(self):
        self.assertEqual(uc.limit_pct(None, None), 4.0)
        self.assertEqual(uc.limit_pct(None, "3.5"), 3.5)
        self.assertEqual(uc.limit_pct(5, "3.5"), 5.0)
        self.assertEqual(uc.limit_pct("abc", "99"), 4.0)

    def test_idwg(self):
        r = uc.idwg(62.9, 60.4, 60, 4)
        self.assertEqual((r["idwg_kg"], r["limit_kg"], r["over"]), (2.5, 2.4, True))

    def test_parse_weight(self):
        self.assertEqual(uc.parse_weight_message("น้ำหนัก 62.4"), 62.4)
        self.assertEqual(uc.parse_weight_message("นน 62,4 กก."), 62.4)
        self.assertEqual(uc.parse_weight_message("weight: 70 kg"), 70.0)
        self.assertIsNone(uc.parse_weight_message("น้ำหนัก 5"))
        self.assertIsNone(uc.parse_weight_message("ผลแล็บ 62.4"))

    def test_reply_text(self):
        ctx = uc.weight_reply_ctx(61.2, 60.0, 60.0, 4.0, date(2026, 11, 18), today=date(2026, 11, 17))
        txt = uc.render(uc.DEFAULT_WEIGHT_TEMPLATE, ctx)
        self.assertIn("+1.2", txt)
        self.assertIn("เหลือโควตาอีก 1.2", txt)
        self.assertIn("วันพุธ", txt)
        over = uc.render(uc.DEFAULT_WEIGHT_TEMPLATE, uc.weight_reply_ctx(63.0, 60.0, 60.0, 4.0, None, today=date(2026, 11, 17)))
        self.assertIn("เกินเกณฑ์แล้ว 0.6", over)

    def test_template_editable_and_safe(self):
        ctx = uc.weight_reply_ctx(61.0, 60.0, 60.0, 4.0, None, today=date(2026, 11, 17))
        self.assertEqual(uc.render("น้ำหนัก {weight} {unknown}", ctx), "น้ำหนัก 61.0")
        self.assertIn("บันทึกน้ำหนัก", uc.render("{broken", ctx))


if __name__ == "__main__":
    unittest.main()


class SplitTokenTests(unittest.TestCase):
    def test_ocr_split_number(self):
        words = [W("UF", 262, 465), W("volume", 290, 465), W("3", 300, 525, 20, 40), W(",", 322, 525, 6, 40), W("91", 330, 525, 40, 40)]
        self.assertEqual(uc.parse_machine_words(words)["uf_removed"], 3910)


class ProjectionAndCardTests(unittest.TestCase):
    def test_projection(self):
        from datetime import datetime, timedelta, timezone
        from hd_module import uf_watch as uw
        t0 = datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc)
        r = uw.evaluate(2500, 500, t0, 4, 60, t0 + timedelta(minutes=111), now=t0 + timedelta(minutes=112),
                        current_rate=600, pre_weight=62.5)
        self.assertEqual(r["projected_final"], 1780)
        self.assertEqual(r["projected_post_weight"], 60.7)
        self.assertEqual(r["projected_vs_dw"], 0.7)
        self.assertIn("เกิน DW 0.7", r["projection_text"])
        # ไม่มีอัตรา/น้ำหนัก → ไม่พัง
        r2 = uw.evaluate(2500, 500, t0, 4, None, None, now=t0 + timedelta(minutes=60))
        self.assertIn("flag", r2)

    def test_patient_card_idwg_and_doctor_note(self):
        from hd_module.patient_summary import build_flex, build_text
        s = {"patient_name": "ทดสอบ", "pre_weight": 62.9, "post_weight": 60.5, "dry_weight": 60.0, "actual_uf": 2400,
             "idwg": {"idwg_kg": 2.5, "limit_kg": 2.4, "over": True, "over_kg": 0.1}, "card_note": "ลดน้ำดื่มอีกนิด"}
        t = build_text(s, [], today=date(2026, 11, 17))
        self.assertIn("+2.5", t)
        self.assertIn("เกิน", t)
        self.assertIn("จากแพทย์: ลดน้ำดื่มอีกนิด", t)
        self.assertIn("ลดน้ำดื่มอีกนิด", str(build_flex(s, [], today=date(2026, 11, 17))))


class RouteTests(unittest.TestCase):
    def test_routes_registered(self):
        import os
        from flask import Flask
        from hd_module import blueprint as bp
        app = Flask("t", template_folder=os.path.abspath("hd_module/templates"))
        app.register_blueprint(bp.hd_bp)
        app.register_blueprint(bp.hd_web)
        rules = {r.rule for r in app.url_map.iter_rules()}
        for r in ("/hd/radar", "/hd/api/v1/machine-screen/read", "/hd/api/v1/patients/<hn>/uf-coach"):
            self.assertIn(r, rules)
        self.assertEqual(app.test_client().get("/hd/radar").status_code, 200)


class _Cur:
    def __init__(self, answers):
        self.answers, self.sql = list(answers), []

    def execute(self, sql, args=None):
        self.sql.append((sql, args))

    def fetchone(self):
        return self.answers.pop(0)

    def fetchall(self):
        return self.answers.pop(0)


class DbPathTests(unittest.TestCase):
    def test_weight_coach_saves_and_replies(self):
        cur = _Cur([(7, "นายทดสอบ", 60.0, "จ/พ/ศ", None), (60.2, date(2026, 11, 16))])
        txt, ctx = uc.weight_coach(cur, "99999", 61.5, setting_pct="4", today=date(2026, 11, 17))
        self.assertIn("+1.3", txt)
        self.assertIn("วันพุธ", txt)
        ins = [a for s, a in cur.sql if s.startswith("INSERT INTO hd_home_weights")][0]
        self.assertEqual(ins, ("99999", 61.5, 1.3, False))

    def test_weight_coach_unknown_patient(self):
        self.assertEqual(uc.weight_coach(_Cur([None]), "000", 61.5), (None, None))

    def test_sessions_for_tolerance(self):
        from datetime import datetime
        st, en = datetime(2026, 11, 1, 8), datetime(2026, 11, 1, 12)
        cur = _Cur([[(1, date(2026, 11, 1), 2400, st, en, 60.0), (2, date(2026, 11, 3), 1200, st, en, 60.0)],
                    [(130,), (120,), (105,)],      # รอบ 1: ลด 25 → IDH
                    [(130,), (125,)]])             # รอบ 2: ปกติ
        ss = uc.sessions_for_tolerance(cur, "99999")
        self.assertEqual([(s["rate_kg"], s["idh"]) for s in ss], [(10.0, True), (5.0, False)])
