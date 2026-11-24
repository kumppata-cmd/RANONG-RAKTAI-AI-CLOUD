"""📼 Kidney Black Box — assemble/analyze ด้วยข้อมูลสังเคราะห์ (ไม่แตะ DB)."""
from datetime import datetime, timedelta

from hd_module import blackbox as bb

T0 = datetime(2026, 11, 24, 1, 30)  # 08:30 ไทย


def _raw(**over):
    raw = {
        "_errors": [],
        "session": [{
            "id": 7, "patient_id": 1, "bed_number": 3, "session_date": "2026-11-24", "shift": "morning", "status": "completed",
            "started_at": T0, "ended_at": T0 + timedelta(hours=4), "bed_released_at": None, "released_by": None,
            "pre_weight": 62.0, "post_weight": 60.4, "uf_goal": 2500, "actual_uf": 1600, "kt_v": None, "urr": None,
            "pre_symptoms": None, "post_symptoms": None, "notes": None, "cancel_reason": None,
            "hn": "1234", "patient_name": "ทดสอบ", "dry_weight": 59.0, "vascular_access": "AVF", "duration_hours": 4,
        }],
        "vitals": [
            {"id": 1, "phase": "pre", "recorded_at": T0, "sbp": 150, "dbp": 85, "heart_rate": 72, "temperature": None, "spo2": 98, "respiratory_rate": None, "symptoms": None, "nurse_id": 5},
            {"id": 2, "phase": "intra", "recorded_at": T0 + timedelta(minutes=60), "sbp": 135, "dbp": 80, "heart_rate": 74, "temperature": None, "spo2": 98, "respiratory_rate": None, "symptoms": None, "nurse_id": 5},
            {"id": 3, "phase": "intra", "recorded_at": T0 + timedelta(minutes=150), "sbp": 92, "dbp": 55, "heart_rate": 95, "temperature": None, "spo2": 97, "respiratory_rate": None, "symptoms": "หน้ามืด", "nurse_id": 5},
            {"id": 4, "phase": "intra", "recorded_at": T0 + timedelta(minutes=180), "sbp": 110, "dbp": 65, "heart_rate": 80, "temperature": None, "spo2": 98, "respiratory_rate": None, "symptoms": None, "nurse_id": 5},
        ],
        "uf": [
            {"id": 10, "recorded_at": T0 + timedelta(minutes=60), "slot_time": "09:30", "uf_removed": 700, "uf_rate": 800, "uf_remaining": 1800, "notes": None},
            {"id": 11, "recorded_at": T0 + timedelta(minutes=120), "slot_time": "10:30", "uf_removed": 1400, "uf_rate": 850, "uf_remaining": 1100, "notes": None},
        ],
        "flowsheet": [{"id": 20, "slot_time": "09:00", "recorded_at": T0 + timedelta(minutes=30), "bfr": 300, "dfr": 500, "vp": 120, "ap": -80, "tmp": 40, "conductivity": 14.0, "dialysate_temp": 36.5, "ufr": None, "total_uf": None, "remark": None}],
        "alerts": [{"id": 30, "severity": "critical", "title": "ความดันตก", "message": "SBP 92", "rule_id": "idh", "details": None,
                    "ts": T0 + timedelta(minutes=151), "acknowledged": True, "acknowledged_at": T0 + timedelta(minutes=175),
                    "physician_order": "ลด UF rate, NSS 200 mL", "order_by": "dr", "order_at": T0 + timedelta(minutes=176)}],
        "acks": [{"id": 40, "alert_id": 30, "acknowledged_by": "nurse1", "acknowledged_at": T0 + timedelta(minutes=175), "action_taken": "ลด UF", "resolution_notes": None, "escalated_to": None}],
        "meds": [], "machine": [], "anticoag": [], "checkin": [], "home_weights": [], "incidents": [],
        "audit": [{"id": 50, "user_id": 5, "action": "vital.time_edit", "resource_type": "vital", "resource_id": 3, "old_values": None, "new_values": None, "created_at": T0 + timedelta(minutes=200)}],
    }
    raw.update(over)
    return raw


def test_assemble_orders_events_and_keeps_sources():
    tl = bb.assemble(_raw())
    kinds = [e["kind"] for e in tl["events"]]
    assert kinds[0] == "start" and kinds[-1] == "end"
    assert all(e["source"]["table"] and e["ts"] for e in tl["events"])
    assert [e["n"] for e in tl["events"]] == list(range(1, len(tl["events"]) + 1))
    uf = [e for e in tl["events"] if e["kind"] == "uf"][1]
    assert uf["data"]["rate_kg"] == round(850 / 59.0, 1)
    assert any(e["kind"] == "order" for e in tl["events"])
    assert any(e["kind"] == "edit" for e in tl["events"])


def test_analyze_facts_missing_hypotheses():
    tl = bb.analyze(bb.assemble(_raw()))
    assert any("SBP ต่ำสุด 92" in f for f in tl["facts"])
    assert any("แก้ไขย้อนหลัง 1" in f for f in tl["facts"])
    # gap 60→150 นาที = 90 นาที > 60
    assert any(g["minutes"] == 90 for g in tl["gaps"])
    rules = {h["rule"] for h in tl["hypotheses"]}
    assert "R3" in rules            # ack หลัง 24 นาที > 15
    assert "R1" not in rules        # SBP 92 มี alert ภายใน 30 นาที → ไม่ใช่ near-miss
    assert "R4" in rules            # post 60.4 − DW 59 = 1.4 > 1.0
    rv = tl["alert_reviews"][0]
    assert rv["timing"] == "เตือนทันที"
    assert any("SBP 135→92" in s for s in rv["signals"])


def test_r1_fires_when_no_alert_and_r2_when_rate_high_without_adjustment():
    raw = _raw(alerts=[], acks=[])
    raw["uf"].append({"id": 12, "recorded_at": T0 + timedelta(minutes=150), "slot_time": "11:00", "uf_removed": 1500, "uf_rate": 900, "uf_remaining": 1000, "notes": None})
    tl = bb.analyze(bb.assemble(raw))
    rules = [h["rule"] for h in tl["hypotheses"]]
    assert "R1" in rules and "R2" in rules
    # ปรับเป้าหลังจากนั้น → R2 หาย
    raw["uf"].append({"id": 13, "recorded_at": T0 + timedelta(minutes=160), "slot_time": "11:10", "uf_removed": 1550, "uf_rate": 400, "uf_remaining": 500, "notes": "target=2050"})
    tl2 = bb.analyze(bb.assemble(raw))
    assert "R2" not in [h["rule"] for h in tl2["hypotheses"]]


def test_missing_data_is_reported_not_guessed():
    raw = _raw(vitals=[], uf=[])
    raw["session"][0].update({"dry_weight": None, "post_weight": None})
    tl = bb.analyze(bb.assemble(raw))
    assert any("ไม่มี vital" in m for m in tl["missing"])
    assert any("ไม่มีการบันทึก UF" in m for m in tl["missing"])
    assert any("DW" in m for m in tl["missing"])
    assert any("ไม่มีน้ำหนักหลัง" in m for m in tl["missing"])
    assert tl["hypotheses"] == [] or all(h["rule"] != "R4" for h in tl["hypotheses"])


def test_assemble_none_without_session():
    assert bb.assemble({"session": []}) is None


def test_line_summary_short_and_factual():
    tl = bb.analyze(bb.assemble(_raw()))
    txt = bb.line_summary(tl, base_url="https://x")
    assert txt.startswith("📼") and "/hd/blackbox/7" in txt
    assert len(txt.split("\n")) <= 14
    assert "ข้อสันนิษฐาน" in txt


def test_limits_override():
    lim = bb.rule_limits(lambda k, d: "30" if k == "bb_ack_min" else "")
    assert lim["bb_ack_min"] == 30
    tl = bb.analyze(bb.assemble(_raw()), lim)
    assert "R3" not in {h["rule"] for h in tl["hypotheses"]}   # 24 นาที < 30


class _Cur:
    def __init__(self, row=(42,)):
        self.row = row
        self.sql = []
        self.connection = None
    def execute(self, sql, params=None):
        self.sql.append((sql, params))
    def fetchone(self):
        return self.row


def test_find_session_parses_bed_day_and_hn():
    from datetime import date
    today = date(2026, 11, 24)
    cur = _Cur()
    sid, note = bb._find_session(cur, "ย้อน เตียง 12 เมื่อวาน", today)
    assert sid == 42 and "เตียง 12" in note and "2026-11-23" in note
    assert cur.sql[-1][1] == (12, date(2026, 11, 23))
    sid, note = bb._find_session(cur, "ย้อน HN 1234 ล่าสุด", today)
    assert sid == 42 and note.startswith("HN 1234")
    sid, note = bb._find_session(cur, "ย้อน เตียง 3 2026-11-20", today)
    assert cur.sql[-1][1] == (3, date(2026, 11, 20))
    sid, note = bb._find_session(cur, "ย้อน อะไรก็ได้", today)
    assert sid is None and note == ""


def test_line_command_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HD_BLACKBOX_ENABLED", raising=False)
    monkeypatch.setattr(bb, "enabled", lambda: False)
    from datetime import date
    out = bb.line_command(_Cur(), "ย้อน เตียง 1 วันนี้", date(2026, 11, 24), base_url="https://x")
    assert "ยังไม่เปิดใช้งาน" in out
