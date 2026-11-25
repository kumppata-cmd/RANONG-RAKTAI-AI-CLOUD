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


# ───────── phase 2: incident draft / audit hash / de-identification / quality / review queue ─────────

def test_incident_draft_prefills_from_focus_alert():
    tl = bb.analyze(bb.assemble(_raw()))
    d = bb.incident_draft(tl)
    assert d["type"] == "idh" and d["severity"] == "major" and d["hn"] == "1234" and d["session_id"] == 7
    assert "ความดันตก" in d["description"] and "สัญญาณก่อนหน้า" in d["description"]
    assert "ส่วนสาเหตุ/การแก้ไขให้ผู้รายงานเขียนเอง" in d["description"]
    d2 = bb.incident_draft(bb.analyze(bb.assemble(_raw(alerts=[], acks=[]))))
    assert d2["type"] == "other" and d2["severity"] == "minor" and d2["alert_id"] is None


def test_deidentified_payload_has_no_name_hn_or_actor():
    import json
    tl = bb.analyze(bb.assemble(_raw()))
    txt = json.dumps(bb.deidentified_payload(tl), ensure_ascii=False, default=str)
    assert "ทดสอบ" not in txt and "1234" not in txt and "nurse1" not in txt and '"actor"' not in txt
    assert '"n": 1' in txt and "hypotheses" in txt


class _ConnStub:
    """fake psycopg2 connection: queue of fetchall/fetchone results"""
    def __init__(self, results):
        self._results = list(results)
        self.sql = []
        self.committed = False
    def cursor(self):
        return _CursorStub(self)
    def commit(self):
        self.committed = True
    def rollback(self):
        pass


class _CursorStub:
    def __init__(self, conn):
        self.c = conn
        self._cur = None
        self.description = None
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, sql, params=None):
        self.c.sql.append((sql, params))
        self._cur = self.c._results.pop(0) if self.c._results else []
    def fetchall(self):
        return self._cur if isinstance(self._cur, list) else [self._cur]
    def fetchone(self):
        return self._cur[0] if isinstance(self._cur, list) and self._cur else (self._cur if not isinstance(self._cur, list) else None)


def test_audit_day_hash_is_deterministic_and_chained():
    from datetime import date
    rows = [(1, 5, "vital.time_edit", "vital", 3, None, None, datetime(2026, 11, 24, 5, 0)),
            (2, 5, "flowsheet.save", "flowsheet", 9, None, "{}", datetime(2026, 11, 24, 6, 0))]
    a = bb.audit_day_hash(_ConnStub([rows]), date(2026, 11, 24), "")
    b = bb.audit_day_hash(_ConnStub([rows]), date(2026, 11, 24), "")
    c = bb.audit_day_hash(_ConnStub([rows]), date(2026, 11, 24), "prevhash")
    d = bb.audit_day_hash(_ConnStub([rows[:1]]), date(2026, 11, 24), "")
    assert a["hash"] == b["hash"] and a["rows"] == 2
    assert a["hash"] != c["hash"]          # ต่อสายกับวันก่อน
    assert a["hash"] != d["hash"]          # แถวหาย = hash เปลี่ยน


def test_audit_hash_verify_detects_tamper():
    from datetime import date
    import json
    rows = [(1, 5, "vital.time_edit", "vital", 3, None, None, datetime(2026, 11, 24, 5, 0))]
    stored = bb.audit_day_hash(_ConnStub([rows]), date(2026, 11, 24), "")
    ok = bb.audit_hash_verify(_ConnStub([(json.dumps(stored),), rows]), date(2026, 11, 24))
    assert ok["match"] is True
    tampered = [(1, 5, "vital.time_edit", "vital", 3, None, '{"sbp": 120}', datetime(2026, 11, 24, 5, 0))]
    bad = bb.audit_hash_verify(_ConnStub([(json.dumps(stored),), tampered]), date(2026, 11, 24))
    assert bad["match"] is False
    none = bb.audit_hash_verify(_ConnStub([None]), date(2026, 11, 24))
    assert none["match"] is None


def test_quality_metrics_rates_and_insufficient_flag():
    conn = _ConnStub([
        [(12, 10, 2)],      # sessions, with_post_weight, released
        [(3,)],             # sbp low
        [(5, 4, 7.5)],      # alerts, acked, median
        [(1,)],             # late
        [(2,)],             # vital gap
        [(1,)],             # uf high
        [(2,)],             # post over dw
        [(4,)],             # edits
        [(0,)],             # incidents
    ])
    q = bb.quality_metrics(conn, 2026, 11)
    assert q["sessions"] == 12 and q["insufficient"] is True
    assert q["rates"]["sessions_sbp_low"] == 25.0 and q["ack_median_min"] == 7.5 and q["alerts_ack_late"] == 1
    q0 = bb.quality_metrics(_ConnStub([[(0, 0, 0)]]), 2026, 10)
    assert q0["insufficient"] is True and q0["sessions"] == 0


def test_review_queue_uses_cache(monkeypatch):
    from datetime import date
    calls = []
    def fake_build(conn, sid, limits=None):
        calls.append(sid)
        return {"session": {"bed": 1, "hn": "1", "patient_name": "x"}, "events": [], "hypotheses": [{"text": "t"}], "gaps": []}
    monkeypatch.setattr(bb, "build", fake_build)
    bb._RQ_CACHE.clear()
    conn = _ConnStub([[(1,), (2,)]])
    out = bb.review_queue(conn, date(2026, 11, 24))
    assert out["sessions"] == 2 and len(out["to_review"]) == 2 and calls == [1, 2]
    out2 = bb.review_queue(_ConnStub([]), date(2026, 11, 24))     # cache hit — ไม่ query ซ้ำ
    assert out2 is out and calls == [1, 2]


def test_line_summary_focus_alert_signals():
    tl = bb.analyze(bb.assemble(_raw()))
    txt = bb.line_summary(tl, focus_alerts=True)
    assert "🔎" in txt and "SBP 135→92" in txt and "เตือนทันที" in txt


def test_beds_cache_helpers_exist_and_bust():
    from hd_module import blueprint as bp
    bp._BEDS_CACHE[("morning", "")] = (0, {"beds": []})
    bp._beds_cache_bust()
    assert bp._BEDS_CACHE == {}
    assert bp._beds_cache_ttl() > 0
