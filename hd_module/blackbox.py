"""📼 Kidney Black Box — "ย้อนรอบฟอก" (Clinical Time Machine) — 24 พ.ย. 2569 (แพทย์อนุมัติเริ่ม)

หลักการ: กล่องดำไม่ใช่ที่เก็บข้อมูลใหม่ — เป็น **มุมมองอ่านอย่างเดียว** ของตารางเดิม 14 ตาราง
ร้อยเป็นเส้นเวลาเดียว ทุกเหตุการณ์มี ts + แหล่งที่มา (ตาราง/แถว) + ผู้ทำ จากนั้นใช้กฎ (rule-based,
ไม่ใช่ AI) หา "สัญญาณก่อน alert", "ช่องว่างข้อมูล" และ "จุดที่ควรทบทวน" (near-miss)

แยกชั้นชัดเจน (ตามเอกสาร): FACT = มาจาก SQL ตรง ๆ · MISSING = ไม่มีข้อมูล · HYPOTHESIS = ข้อสันนิษฐานจากกฎ
ไม่มีการเขียนตารางใด ๆ · ไม่สรุปสาเหตุ · แพทย์ตัดสิน

ส่วนที่ไม่แตะ DB (`assemble`, `analyze`, กฎทั้งหมด) ทดสอบได้ด้วยข้อมูลสังเคราะห์
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

# ── ค่าตั้งต้นของกฎ (ปรับได้ผ่าน ⚙️ settings ชื่อเดียวกัน) ──
RULE_DEFAULTS = {
    "bb_sbp_low": 100,          # SBP ต่ำกว่านี้ = ความดันตก
    "bb_sbp_drop": 20,          # SBP ลดลงจากครั้งก่อน ≥ นี้ = สัญญาณ
    "bb_uf_rate_high": 13.0,    # mL/kg/h เพดาน
    "bb_vital_gap_min": 60,     # ไม่มี vital นานกว่านี้ (นาที) ขณะฟอก = ช่องว่าง
    "bb_ack_min": 15,           # alert ไม่ถูกรับทราบนานกว่านี้ = ควรทบทวน
    "bb_prewindow_min": 60,     # มองย้อนก่อน alert กี่นาที
    "bb_post_over_dw_kg": 1.0,  # น้ำหนักหลังฟอกสูงกว่า DW เกินนี้ = ควรทบทวน
}

_TH = timedelta(hours=7)


def enabled():
    """flag ปิดเป็นค่าเริ่มต้น: เปิดด้วย ENV HD_BLACKBOX_ENABLED=true หรือ ⚙️ setting blackbox_enabled=1"""
    if os.environ.get("HD_BLACKBOX_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        from .blueprint import get_setting_value
        return str(get_setting_value("blackbox_enabled", "") or "").strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return False


def rule_limits(get_setting=None):
    out = dict(RULE_DEFAULTS)
    if get_setting:
        for k, dv in RULE_DEFAULTS.items():
            try:
                v = get_setting(k, "")
                if v not in ("", None):
                    out[k] = type(dv)(float(v)) if isinstance(dv, (int, float)) else v
            except Exception:
                pass
    return out


# ───────────────────────── helpers ─────────────────────────

def _iso(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    s = dt.replace(tzinfo=None).isoformat(timespec="seconds")
    return s + "Z"


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ev(ts, kind, text, table, rid, actor=None, **data):
    """เหตุการณ์ 1 รายการ: ทุกอันต้องมี ts + แหล่งที่มา"""
    return {"ts": ts, "kind": kind, "text": text, "source": {"table": table, "id": rid}, "actor": actor,
            "data": {k: v for k, v in data.items() if v is not None}}


# ───────────────────────── 1) ดึงข้อมูลดิบ (DB) ─────────────────────────

def load_raw(conn, session_id):
    """อ่านทุกตารางที่เกี่ยวกับรอบนี้ (อ่านอย่างเดียว) → dict ของ list แถวดิบ
    ตารางที่ไม่มี/คิวรีล้ม = [] และบันทึกชื่อไว้ใน raw["_errors"] (ไม่ทำให้ทั้งหน้าล้ม)"""
    raw = {"_errors": []}

    def q(name, sql, params):
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                raw[name] = [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception as e:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:
                pass
            raw[name] = []
            raw["_errors"].append(f"{name}: {type(e).__name__}")

    q("session", """SELECT s.id, s.patient_id, s.bed_number, s.session_date, s.shift, s.status,
                           s.started_at, s.ended_at, s.bed_released_at, s.released_by,
                           s.pre_weight, s.post_weight, s.uf_goal, s.actual_uf, s.kt_v, s.urr,
                           s.pre_symptoms, s.post_symptoms, s.notes, s.cancel_reason,
                           p.hn, p.patient_name, p.dry_weight, p.vascular_access,
                           COALESCE((SELECT r.duration_hours FROM hd_prescriptions r WHERE r.patient_id = s.patient_id
                                     AND r.is_active = TRUE ORDER BY r.id DESC LIMIT 1), 4) AS duration_hours
                    FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id WHERE s.id = %s""",
      (session_id,))
    q("vitals", """SELECT id, phase, recorded_at, sbp, dbp, heart_rate, temperature, spo2, respiratory_rate, symptoms, nurse_id
                   FROM hd_vital_signs WHERE session_id = %s ORDER BY recorded_at, id""", (session_id,))
    q("uf", """SELECT id, recorded_at, slot_time, uf_removed, uf_rate, uf_remaining, notes
               FROM hd_uf_records WHERE session_id = %s ORDER BY recorded_at, id""", (session_id,))
    q("flowsheet", """SELECT id, slot_time, recorded_at, bfr, dfr, vp, ap, tmp, conductivity, dialysate_temp, ufr, total_uf, remark
                      FROM hd_flowsheet WHERE session_id = %s ORDER BY slot_time""", (session_id,))
    q("alerts", """SELECT id, severity, title, message, rule_id, details, COALESCE(triggered_at, created_at) AS ts,
                          acknowledged, acknowledged_at, physician_order, order_by, order_at
                   FROM hd_clinical_alerts WHERE session_id = %s ORDER BY COALESCE(triggered_at, created_at), id""",
      (session_id,))
    q("acks", """SELECT a.id, a.alert_id, a.acknowledged_by, a.acknowledged_at, a.action_taken, a.resolution_notes, a.escalated_to
                 FROM hd_alert_acknowledgements a JOIN hd_clinical_alerts c ON c.id = a.alert_id
                 WHERE c.session_id = %s ORDER BY a.acknowledged_at""", (session_id,))
    q("meds", """SELECT id, drug_name, dose_given, route, given_at, given_by, slot_time, notes
                 FROM hd_medication_given WHERE session_id = %s ORDER BY given_at, id""", (session_id,))
    q("machine", """SELECT id, recorded_at, observation_type, severity, description, action_taken, nurse_id
                    FROM hd_machine_observations WHERE session_id = %s ORDER BY recorded_at""", (session_id,))
    q("anticoag", """SELECT id, protocol, loading_dose, loading_unit, maintenance_dose, maintenance_unit, total_dose,
                            heparin_free_reason, recorded_at, recorded_by
                     FROM hd_anticoagulation WHERE session_id = %s ORDER BY recorded_at""", (session_id,))
    srow = raw.get("session") or []
    if srow:
        s = srow[0]
        q("checkin", """SELECT id, checkin_date, weight_home, symptoms, meds_taken, flagged, created_at
                        FROM hd_prehd_checkins WHERE hn = %s AND checkin_date = %s""", (s["hn"], s["session_date"]))
        q("home_weights", """SELECT id, weight, idwg_kg, over_limit, via, recorded_at FROM hd_home_weights
                             WHERE hn = %s AND recorded_at >= %s - INTERVAL '3 days' AND recorded_at <= %s + INTERVAL '1 day'
                             ORDER BY recorded_at""", (s["hn"], s["session_date"], s["session_date"]))
        q("incidents", """SELECT id, incident_date, incident_type, severity, description, action_taken, reported_by, status, created_at
                          FROM hd_incidents WHERE patient_hn = %s AND incident_date = %s""", (s["hn"], s["session_date"]))
        # การแก้ไขย้อนหลัง: audit ของรอบนี้ + ของแถว vital/UF/flowsheet/alert ที่อยู่ในรอบนี้
        vid = [r["id"] for r in raw.get("vitals", [])] or [-1]
        uid = [r["id"] for r in raw.get("uf", [])] or [-1]
        fid = [r["id"] for r in raw.get("flowsheet", [])] or [-1]
        aid = [r["id"] for r in raw.get("alerts", [])] or [-1]
        q("audit", """SELECT id, user_id, action, resource_type, resource_id, old_values, new_values, created_at
                      FROM hd_audit_logs
                      WHERE (resource_type = 'session' AND resource_id = %s)
                         OR (resource_type IN ('vital','vital_sign','vital_signs') AND resource_id = ANY(%s))
                         OR (resource_type = 'uf_record' AND resource_id = ANY(%s))
                         OR (resource_type = 'flowsheet' AND resource_id = ANY(%s))
                         OR (resource_type IN ('alert','clinical_alert') AND resource_id = ANY(%s))
                      ORDER BY created_at, id""", (session_id, vid, uid, fid, aid))
    return raw


# ───────────────────────── 2) ร้อยเป็นเส้นเวลา (ไม่แตะ DB) ─────────────────────────

def assemble(raw):
    """raw → {session, events[], meta}  ทุก event: ts(UTC iso), kind, text, source, actor, data"""
    srows = raw.get("session") or []
    if not srows:
        return None
    s = srows[0]
    ev = []
    dw = _f(s.get("dry_weight"))
    if s.get("started_at"):
        ev.append(_ev(_iso(s["started_at"]), "start", "เริ่มฟอก", "hd_sessions", s["id"],
                      bed=s.get("bed_number"), pre_weight=_f(s.get("pre_weight")), uf_goal=_f(s.get("uf_goal")),
                      pre_symptoms=s.get("pre_symptoms")))
    for r in raw.get("checkin", []):
        ev.append(_ev(_iso(r.get("created_at")), "line", "เช็คอินเช้าทาง LINE", "hd_prehd_checkins", r["id"],
                      weight_home=_f(r.get("weight_home")), symptoms=r.get("symptoms"), flagged=r.get("flagged")))
    for r in raw.get("home_weights", []):
        ev.append(_ev(_iso(r.get("recorded_at")), "line", "น้ำหนักบ้านทาง LINE", "hd_home_weights", r["id"],
                      weight=_f(r.get("weight")), idwg_kg=_f(r.get("idwg_kg")), over_limit=r.get("over_limit")))
    for r in raw.get("vitals", []):
        parts = []
        if r.get("sbp") is not None:
            parts.append(f"BP {r['sbp']}/{r.get('dbp') if r.get('dbp') is not None else '-'}")
        if r.get("heart_rate") is not None:
            parts.append(f"PR {r['heart_rate']}")
        if r.get("spo2") is not None:
            parts.append(f"SpO₂ {r['spo2']}")
        if r.get("temperature") is not None:
            parts.append(f"T {r['temperature']}")
        if r.get("respiratory_rate") is not None:
            parts.append(f"RR {r['respiratory_rate']}")
        ev.append(_ev(_iso(r.get("recorded_at")), "vital", " ".join(parts) or "vital", "hd_vital_signs", r["id"],
                      actor=r.get("nurse_id"), phase=r.get("phase"), sbp=r.get("sbp"), dbp=r.get("dbp"),
                      hr=r.get("heart_rate"), spo2=r.get("spo2"), temp=_f(r.get("temperature")),
                      rr=r.get("respiratory_rate"), symptoms=r.get("symptoms")))
    for r in raw.get("uf", []):
        rate = _f(r.get("uf_rate"))
        kg = round(rate / dw, 1) if (rate and dw) else None
        ev.append(_ev(_iso(r.get("recorded_at")), "uf", f"UF {_f(r.get('uf_removed')) or 0:.0f} mL" + (f" · {rate:.0f} mL/ชม." if rate else "")
                      + (f" ({kg} mL/kg/ชม.)" if kg else ""), "hd_uf_records", r["id"],
                      removed=_f(r.get("uf_removed")), rate=rate, rate_kg=kg, remaining=_f(r.get("uf_remaining")),
                      notes=r.get("notes"), slot=r.get("slot_time")))
    for r in raw.get("flowsheet", []):
        m = {k: r.get(k) for k in ("bfr", "dfr", "vp", "ap", "tmp", "conductivity", "dialysate_temp", "ufr", "total_uf")}
        if any(v is not None for v in m.values()) or r.get("remark"):
            txt = " ".join(f"{k.upper()} {v}" for k, v in (("bfr", r.get("bfr")), ("vp", r.get("vp")), ("ap", r.get("ap")),
                                                           ("tmp", r.get("tmp"))) if v is not None)
            ev.append(_ev(_iso(r.get("recorded_at")), "machine", (txt or "ค่าเครื่อง") + (f" · {r['remark']}" if r.get("remark") else ""),
                          "hd_flowsheet", r["id"], slot=r.get("slot_time"), **{k: _f(v) for k, v in m.items()}, remark=r.get("remark")))
    for r in raw.get("machine", []):
        ev.append(_ev(_iso(r.get("recorded_at")), "machine_obs", f"{r.get('observation_type') or 'เครื่อง'}: {r.get('description') or ''}",
                      "hd_machine_observations", r["id"], actor=r.get("nurse_id"), severity=r.get("severity"),
                      action=r.get("action_taken")))
    for r in raw.get("anticoag", []):
        ev.append(_ev(_iso(r.get("recorded_at")), "med", f"Anticoagulation: {r.get('protocol') or ''} "
                      f"{r.get('loading_dose') or ''}{r.get('loading_unit') or ''}", "hd_anticoagulation", r["id"],
                      actor=r.get("recorded_by")))
    for r in raw.get("meds", []):
        ev.append(_ev(_iso(r.get("given_at")), "med", f"ให้ยา {r.get('drug_name')} {r.get('dose_given') or ''} {r.get('route') or ''}".strip(),
                      "hd_medication_given", r["id"], actor=r.get("given_by"), notes=r.get("notes")))
    acks_by_alert = {}
    for a in raw.get("acks", []):
        acks_by_alert.setdefault(a["alert_id"], []).append(a)
    for r in raw.get("alerts", []):
        ev.append(_ev(_iso(r.get("ts")), "alert", f"[{(r.get('severity') or '').upper()}] {r.get('title') or r.get('message') or 'alert'}",
                      "hd_clinical_alerts", r["id"], severity=r.get("severity"), rule_id=r.get("rule_id"),
                      message=r.get("message"), acknowledged=bool(r.get("acknowledged")),
                      acknowledged_at=_iso(r.get("acknowledged_at"))))
        for a in acks_by_alert.get(r["id"], []):
            ev.append(_ev(_iso(a.get("acknowledged_at")), "ack", f"รับทราบ alert #{r['id']}" + (f": {a['action_taken']}" if a.get("action_taken") else ""),
                          "hd_alert_acknowledgements", a["id"], actor=a.get("acknowledged_by"), alert_id=r["id"],
                          escalated_to=a.get("escalated_to")))
        if r.get("physician_order"):
            ev.append(_ev(_iso(r.get("order_at")) or _iso(r.get("ts")), "order", f"คำสั่งแพทย์: {r['physician_order']}",
                          "hd_clinical_alerts", r["id"], actor=r.get("order_by"), alert_id=r["id"]))
    for r in raw.get("audit", []):
        act = r.get("action") or ""
        if any(k in act for k in ("time_edit", "edit", "update", "delete", "release", "cancel", "reopen")):
            ev.append(_ev(_iso(r.get("created_at")), "edit", f"แก้ไขย้อนหลัง: {act}", "hd_audit_logs", r["id"],
                          actor=r.get("user_id"), resource=f"{r.get('resource_type')}#{r.get('resource_id')}",
                          old=r.get("old_values"), new=r.get("new_values")))
    for r in raw.get("incidents", []):
        ev.append(_ev(_iso(r.get("created_at")), "incident", f"Incident [{r.get('severity')}] {r.get('incident_type')}: {r.get('description') or ''}",
                      "hd_incidents", r["id"], actor=r.get("reported_by"), status=r.get("status")))
    if s.get("bed_released_at"):
        ev.append(_ev(_iso(s["bed_released_at"]), "release", "ปล่อยเตียง (รอสรุป Post-HD)", "hd_sessions", s["id"], actor=s.get("released_by")))
    if s.get("ended_at"):
        ev.append(_ev(_iso(s["ended_at"]), "end", "จบรอบฟอก", "hd_sessions", s["id"],
                      post_weight=_f(s.get("post_weight")), actual_uf=_f(s.get("actual_uf")), post_symptoms=s.get("post_symptoms"),
                      kt_v=_f(s.get("kt_v")), urr=_f(s.get("urr"))))
    ev = [e for e in ev if e["ts"]]
    ev.sort(key=lambda e: e["ts"])
    for i, e in enumerate(ev, 1):
        e["n"] = i
    session = {
        "id": s["id"], "hn": s.get("hn"), "patient_name": s.get("patient_name"), "bed": s.get("bed_number"),
        "date": str(s.get("session_date")), "shift": s.get("shift"), "status": s.get("status"),
        "started_at": _iso(s.get("started_at")), "ended_at": _iso(s.get("ended_at")),
        "bed_released_at": _iso(s.get("bed_released_at")),
        "pre_weight": _f(s.get("pre_weight")), "post_weight": _f(s.get("post_weight")), "dry_weight": dw,
        "uf_goal": _f(s.get("uf_goal")), "actual_uf": _f(s.get("actual_uf")), "duration_hours": _f(s.get("duration_hours")) or 4,
        "kt_v": _f(s.get("kt_v")), "urr": _f(s.get("urr")), "va": s.get("vascular_access"),
        "pre_symptoms": s.get("pre_symptoms"), "post_symptoms": s.get("post_symptoms"), "notes": s.get("notes"),
    }
    return {"session": session, "events": ev, "errors": raw.get("_errors", [])}


# ───────────────────────── 3) กฎ: ช่องว่าง / สัญญาณก่อน alert / จุดที่ควรทบทวน ─────────────────────────

def _dt(iso):
    return datetime.fromisoformat(iso.replace("Z", "")) if iso else None


def _th(iso):
    d = _dt(iso)
    return (d + _TH).strftime("%H:%M") if d else "-"


def analyze(tl, limits=None):
    """tl จาก assemble() → เพิ่ม facts[], missing[], hypotheses[], gaps[], alert_reviews[] (ไม่แตะ DB)"""
    L = dict(RULE_DEFAULTS)
    L.update(limits or {})
    s, ev = tl["session"], tl["events"]
    facts, missing, hyp, gaps, reviews = [], [], [], [], []
    start, end = _dt(s["started_at"]), _dt(s["ended_at"]) or _dt(s["bed_released_at"])
    vitals = [e for e in ev if e["kind"] == "vital" and e["data"].get("sbp") is not None]
    ufs = [e for e in ev if e["kind"] == "uf"]
    alerts = [e for e in ev if e["kind"] == "alert"]
    dw = s.get("dry_weight")

    # ── FACTS ──
    if start:
        facts.append(f"เริ่มฟอก {_th(s['started_at'])} เตียง {s['bed']}" + (f" · จบ {_th(s['ended_at'])}" if s.get("ended_at") else " · ยังไม่ปิดรอบ"))
    if s.get("pre_weight") is not None:
        facts.append(f"น้ำหนักก่อน {s['pre_weight']} kg" + (f" · DW {dw} kg · IDWG {round(s['pre_weight'] - dw, 1)} kg" if dw else "")
                     + (f" · เป้า UF {s['uf_goal']:.0f} mL" if s.get("uf_goal") else ""))
    if s.get("post_weight") is not None:
        facts.append(f"น้ำหนักหลัง {s['post_weight']} kg" + (f" · UF จริง {s['actual_uf']:.0f} mL" if s.get("actual_uf") else ""))
    if vitals:
        sb = [v["data"]["sbp"] for v in vitals]
        facts.append(f"วัด BP {len(vitals)} ครั้ง · SBP ต่ำสุด {min(sb)} ({_th(min(vitals, key=lambda v: v['data']['sbp'])['ts'])}) · สูงสุด {max(sb)}")
    if ufs:
        mx = max(ufs, key=lambda u: u["data"].get("removed") or 0)
        facts.append(f"บันทึก UF {len(ufs)} ครั้ง · สะสมสูงสุด {mx['data'].get('removed') or 0:.0f} mL")
    facts.append(f"แจ้งเตือน {len(alerts)} รายการ · รับทราบแล้ว {sum(1 for a in alerts if a['data'].get('acknowledged'))}")
    edits = [e for e in ev if e["kind"] == "edit"]
    if edits:
        facts.append(f"มีการแก้ไขย้อนหลัง {len(edits)} ครั้ง (ดูในเส้นเวลา)")

    # ── MISSING ──
    if not s.get("pre_weight"):
        missing.append("ไม่มีน้ำหนักก่อนฟอก")
    if not dw:
        missing.append("ไม่มีน้ำหนักแห้ง (DW) → คำนวณ mL/kg/ชม. ไม่ได้")
    if s.get("ended_at") and s.get("post_weight") is None:
        missing.append("ปิดรอบแล้วแต่ไม่มีน้ำหนักหลังฟอก")
    if not vitals:
        missing.append("ไม่มี vital sign ระหว่างฟอกเลย")
    if not ufs:
        missing.append("ไม่มีการบันทึก UF ระหว่างฟอก")
    if s.get("bed_released_at") and not s.get("ended_at"):
        missing.append("ปล่อยเตียงแล้ว ยังไม่สรุป Post-HD")
    if tl.get("errors"):
        missing.append("อ่านบางตารางไม่สำเร็จ: " + ", ".join(tl["errors"]))

    # ── GAPS: ไม่มี vital นานเกินกำหนดขณะฟอก ──
    if start:
        pts = [start] + [_dt(v["ts"]) for v in vitals] + ([end] if end else [datetime.utcnow()])
        pts = sorted(p for p in pts if p)
        for a, b in zip(pts, pts[1:]):
            m = (b - a).total_seconds() / 60
            if m > L["bb_vital_gap_min"]:
                gaps.append({"from": _iso(a), "to": _iso(b), "minutes": int(m),
                             "text": f"ไม่มี vital {int(m)} นาที ({_th(_iso(a))}–{_th(_iso(b))})"})
    for g in gaps:
        missing.append(g["text"])

    # ── HYPOTHESES (กฎ — ข้อสันนิษฐานให้แพทย์ทบทวน ไม่ใช่ข้อสรุป) ──
    # R1 ความดันตก/ลดเร็ว ที่ไม่มี alert ภายใน ±30 นาที
    prev = None
    for v in vitals:
        sbp = v["data"]["sbp"]
        low = sbp < L["bb_sbp_low"]
        drop = prev is not None and (prev - sbp) >= L["bb_sbp_drop"]
        if low or drop:
            t = _dt(v["ts"])
            near = [a for a in alerts if abs((_dt(a["ts"]) - t).total_seconds()) <= 1800]
            if not near:
                hyp.append({"rule": "R1", "n": v["n"], "ts": v["ts"],
                            "text": f"{_th(v['ts'])} SBP {sbp}" + (f" (ลดจาก {prev})" if drop else "") + " — ไม่มี alert/การรับทราบใกล้เคียง → ตรวจว่ากฎเตือนครอบคลุมหรือไม่ / มีการดูแลที่ไม่ได้บันทึก"})
        prev = sbp
    # R2 อัตรา UF ต้องใช้สูงเกินเพดาน และไม่มีการปรับเป้า/ยืดเวลาหลังจากนั้น
    for u in ufs:
        kg = u["data"].get("rate_kg")
        if kg and kg > L["bb_uf_rate_high"]:
            later_adj = any(("target=" in (x["data"].get("notes") or "") or "extend=" in (x["data"].get("notes") or ""))
                            for x in ufs if x["ts"] >= u["ts"])
            if not later_adj:
                hyp.append({"rule": "R2", "n": u["n"], "ts": u["ts"],
                            "text": f"{_th(u['ts'])} อัตรา UF {kg} mL/kg/ชม. เกินเพดาน {L['bb_uf_rate_high']} และไม่พบการตั้งเป้าใหม่/ยืดเวลาหลังจากนั้น"})
    # R3 alert รับทราบช้า
    for a in alerts:
        at = a["data"].get("acknowledged_at")
        if a["data"].get("acknowledged") and at:
            m = (_dt(at) - _dt(a["ts"])).total_seconds() / 60
            if m > L["bb_ack_min"]:
                hyp.append({"rule": "R3", "n": a["n"], "ts": a["ts"], "text": f"alert #{a['source']['id']} รับทราบหลัง {int(m)} นาที (เกณฑ์ {L['bb_ack_min']})"})
        elif not a["data"].get("acknowledged") and a["data"].get("severity") in ("critical", "warning"):
            hyp.append({"rule": "R3", "n": a["n"], "ts": a["ts"], "text": f"alert #{a['source']['id']} ({a['data'].get('severity')}) ยังไม่มีการรับทราบ"})
    # R4 น้ำหนักหลังฟอกสูงกว่า DW มาก
    if dw and s.get("post_weight") is not None and s["post_weight"] - dw > L["bb_post_over_dw_kg"]:
        hyp.append({"rule": "R4", "n": None, "ts": s.get("ended_at"),
                    "text": f"น้ำหนักหลังฟอก {s['post_weight']} kg สูงกว่า DW {dw} kg อยู่ {round(s['post_weight'] - dw, 1)} kg"
                            + ("" if s.get("post_symptoms") or s.get("notes") else " และไม่มีหมายเหตุอธิบาย")})

    # ── ALERT REVIEW: สัญญาณ 60 นาทีก่อน alert + ระบบเตือนทัน/ช้า ──
    for a in alerts:
        t = _dt(a["ts"])
        w0 = t - timedelta(minutes=L["bb_prewindow_min"])
        pre_v = [v for v in vitals if w0 <= _dt(v["ts"]) <= t]
        if len(pre_v) < 2:   # ให้มีค่าอ้างอิงก่อนหน้าต่าง: vital ล่าสุดก่อนหน้าต่าง (ถ้ามี)
            before = [v for v in vitals if _dt(v["ts"]) < w0]
            if before:
                pre_v = [before[-1]] + pre_v
        pre_u = [u for u in ufs if w0 <= _dt(u["ts"]) <= t]
        signals = []
        if len(pre_v) >= 2:
            d = pre_v[-1]["data"]["sbp"] - pre_v[0]["data"]["sbp"]
            signals.append(f"SBP {pre_v[0]['data']['sbp']}→{pre_v[-1]['data']['sbp']} ({'+' if d >= 0 else ''}{d}) ใน {L['bb_prewindow_min']} นาที")
        elif len(pre_v) == 1:
            signals.append(f"มี vital 1 ครั้งก่อน alert (SBP {pre_v[0]['data']['sbp']})")
        else:
            signals.append("ไม่มี vital ในช่วงก่อน alert")
        if pre_u:
            last = pre_u[-1]["data"]
            signals.append(f"UF ล่าสุดก่อน alert {last.get('removed') or 0:.0f} mL" + (f" · {last.get('rate_kg')} mL/kg/ชม." if last.get("rate_kg") else ""))
        else:
            signals.append("ไม่มีบันทึก UF ในช่วงก่อน alert")
        # เตือนทันไหม: vital แรกที่ SBP ต่ำกว่าเกณฑ์ เทียบเวลา alert
        first_low = next((v for v in vitals if v["data"]["sbp"] < L["bb_sbp_low"]), None)
        timing = None
        if first_low:
            lag = (t - _dt(first_low["ts"])).total_seconds() / 60
            timing = "เตือนทันที" if abs(lag) <= 5 else (f"เตือนหลังค่าแรกที่ต่ำ {int(lag)} นาที" if lag > 0 else f"เตือนก่อนค่าต่ำแรก {int(-lag)} นาที")
        reviews.append({"alert_id": a["source"]["id"], "n": a["n"], "ts": a["ts"], "title": a["text"], "signals": signals,
                        "timing": timing, "acknowledged": a["data"].get("acknowledged")})

    tl.update({"facts": facts, "missing": missing, "hypotheses": hyp, "gaps": gaps, "alert_reviews": reviews,
               "limits": {k: L[k] for k in RULE_DEFAULTS}})
    return tl


def build(conn, session_id, limits=None):
    raw = load_raw(conn, session_id)
    tl = assemble(raw)
    if tl is None:
        return None
    return analyze(tl, limits)


def line_summary(tl, base_url="", max_lines=12, focus_alerts=False):
    """สรุปสั้นสำหรับ LINE (ข้อเท็จจริงล้วน) — ส่งถึงแพทย์ 1:1 เท่านั้น · focus_alerts = "ก่อนความดันตก": ใส่สัญญาณก่อน alert"""
    s = tl["session"]
    L = [f"📼 ย้อนรอบฟอก {s['date']} เตียง {s['bed']} — {s.get('patient_name') or ''} (HN {s.get('hn')})"]
    L += [f"• {f}" for f in tl["facts"][:5]]
    if focus_alerts:
        for r in tl.get("alert_reviews", [])[:2]:
            L.append(f"🔎 {_th(r['ts'])} {r['title']}: " + "; ".join(r["signals"]) + (f" · {r['timing']}" if r.get("timing") else ""))
        if not tl.get("alert_reviews"):
            L.append("🔎 รอบนี้ไม่มี alert — ดูจุดที่ควรทบทวนแทน")
    if tl["hypotheses"]:
        L.append(f"⚠️ จุดที่ควรทบทวน {len(tl['hypotheses'])}: " + " / ".join(h["text"] for h in tl["hypotheses"][:2]))
    if tl["missing"]:
        L.append("❔ ข้อมูลที่ขาด: " + "; ".join(tl["missing"][:2]))
    if base_url:
        L.append(f"🔗 {base_url}/hd/blackbox/{s['id']}")
    L.append("(ข้อเท็จจริงจากระบบ — ข้อสันนิษฐานต้องให้แพทย์ทบทวน)")
    return "\n".join(L[:max_lines + 2])


# ───────────────────────── 4) คำสั่ง LINE: "ย้อน เตียง 12 เมื่อวาน" / "ย้อน HN 1234 ล่าสุด" ─────────────────────────

def _find_session(cur, body, today):
    """คืน (session_id, note) จากข้อความ: เตียง N [วันนี้|เมื่อวาน|YYYY-MM-DD] · HN xxxx [ล่าสุด]"""
    import re
    low = body.lower()
    day = today
    if "เมื่อวาน" in low:
        day = today - timedelta(days=1)
    m_date = re.search(r"(\d{4}-\d{2}-\d{2})", low)
    if m_date:
        try:
            day = datetime.strptime(m_date.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    m_bed = re.search(r"เตียง\s*(\d{1,2})", low)
    m_hn = re.search(r"hn\s*([0-9]{3,12})", low)
    if m_bed:
        cur.execute("""SELECT id FROM hd_sessions WHERE bed_number = %s AND session_date = %s
                       AND status IN ('in_progress','completed') ORDER BY started_at DESC NULLS LAST, id DESC LIMIT 1""",
                    (int(m_bed.group(1)), day))
        r = cur.fetchone()
        return (r[0] if r else None), f"เตียง {m_bed.group(1)} วันที่ {day}"
    if m_hn:
        cur.execute("""SELECT s.id FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                       WHERE p.hn = %s AND s.status IN ('in_progress','completed')
                       ORDER BY s.session_date DESC, s.id DESC LIMIT 1""", (m_hn.group(1),))
        r = cur.fetchone()
        return (r[0] if r else None), f"HN {m_hn.group(1)} รอบล่าสุด"
    return None, ""


def line_command(cur, body, today, base_url="", push_fn=None, physician_ids=None, get_setting=None):
    """ตอบในกลุ่มพยาบาล: สรุปย่อ "ไม่มีชื่อ/HN" + ลิงก์ · ส่งสรุปเต็มถึง LINE แพทย์ 1:1 (ถ้าตั้งค่าไว้)"""
    if not base_url:
        try:
            from .patient_engage import _base_url
            base_url = _base_url()
        except Exception:
            base_url = ""
    if not enabled():
        return "📼 ย้อนรอบฟอก ยังไม่เปิดใช้งาน (ตั้งค่า blackbox_enabled=1 ใน ⚙️ หรือ HD_BLACKBOX_ENABLED=true)"
    sid, note = _find_session(cur, body, today)
    if not note:
        return "พิมพ์: ย้อน เตียง 12 เมื่อวาน · ย้อน เตียง 5 วันนี้ · ย้อน HN 1234 ล่าสุด · ย้อน เตียง 3 2026-11-20"
    if not sid:
        return f"ไม่พบรอบฟอกของ {note}"
    tl = build(cur.connection, sid, rule_limits(get_setting))
    if not tl:
        return f"ไม่พบรอบฟอกของ {note}"
    s = tl["session"]
    pushed = 0
    if push_fn and physician_ids:
        full = line_summary(tl, base_url, focus_alerts=("ก่อน" in body))
        for uid in physician_ids:
            try:
                if push_fn(uid, full) is not False:
                    pushed += 1
            except Exception:
                pass
    brief = (f"📼 ย้อนรอบฟอก {note} (รอบ #{sid})\n"
             f"• vital {sum(1 for e in tl['events'] if e['kind']=='vital')} · UF {sum(1 for e in tl['events'] if e['kind']=='uf')} · alert {sum(1 for e in tl['events'] if e['kind']=='alert')}\n"
             f"• จุดที่ควรทบทวน {len(tl['hypotheses'])} · ข้อมูลที่ขาด {len(tl['missing'])}\n"
             + (f"🔗 {base_url}/hd/blackbox/{sid} (ต้อง login)\n" if base_url else "")
             + (f"📨 ส่งสรุปเต็มถึงแพทย์ทาง LINE ส่วนตัวแล้ว ({pushed})" if pushed else "(รายละเอียดผู้ป่วยดูในหน้าเว็บ — ไม่แสดงในกลุ่ม)"))
    return brief


# ───────────────────────── 5) ร่างรายงาน incident จากเส้นเวลา (ไม่บันทึกเอง) ─────────────────────────

def incident_draft(tl, alert_id=None):
    """คืน dict สำหรับเติมฟอร์ม incident ของ clinic-profile — พยาบาลแก้/ยืนยันก่อนบันทึก"""
    s = tl["session"]
    alerts = [e for e in tl["events"] if e["kind"] == "alert"]
    focus = next((a for a in alerts if alert_id and a["source"]["id"] == alert_id), alerts[0] if alerts else None)
    sev_map = {"critical": "major", "warning": "moderate", "info": "minor"}
    lines = [f"[ร่างจาก 📼 ย้อนรอบฟอก รอบ #{s['id']} — {s['date']} เตียง {s['bed']}]"]
    if focus:
        lines.append(f"เหตุการณ์: {focus['text']} เวลา {_th(focus['ts'])}")
        rv = next((r for r in tl.get("alert_reviews", []) if r["alert_id"] == focus["source"]["id"]), None)
        if rv:
            lines.append("สัญญาณก่อนหน้า: " + "; ".join(rv["signals"]) + (f" · {rv['timing']}" if rv.get("timing") else ""))
    lines += [f"- {f}" for f in tl["facts"][:6]]
    if tl["hypotheses"]:
        lines.append("จุดที่ควรทบทวน: " + " | ".join(f"{h['rule']} {h['text']}" for h in tl["hypotheses"][:4]))
    if tl["missing"]:
        lines.append("ข้อมูลที่ขาด: " + "; ".join(tl["missing"][:4]))
    lines.append("(ข้อเท็จจริงจากระบบ — ส่วนสาเหตุ/การแก้ไขให้ผู้รายงานเขียนเอง)")
    itype = "idh" if (focus and ("ความดัน" in focus["text"] or "idh" in (focus["data"].get("rule_id") or "").lower())) else "other"
    return {"type": itype, "severity": sev_map.get((focus or {}).get("data", {}).get("severity"), "minor"),
            "date": (s.get("ended_at") or s.get("started_at") or "")[:16], "hn": s.get("hn"),
            "description": "\n".join(lines), "session_id": s["id"], "alert_id": focus["source"]["id"] if focus else None}


# ───────────────────────── 6) การ์ดคุณภาพรายเดือน (SQL ล้วน — เบา) ─────────────────────────

def quality_metrics(conn, year, month, limits=None):
    """ตัวชี้วัดระดับหน่วย (ไม่ระบุตัวบุคคล) สำหรับ QI — คืน dict พร้อม 'insufficient' เมื่อรอบน้อย"""
    L = dict(RULE_DEFAULTS)
    L.update(limits or {})
    from datetime import date as _date
    d0 = _date(year, month, 1)
    d1 = _date(year + (month // 12), month % 12 + 1, 1)
    out = {"month": f"{year}-{month:02d}", "sessions": 0}

    def q(sql, params):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    try:
        n = q("""SELECT COUNT(*), COUNT(*) FILTER (WHERE post_weight IS NOT NULL), COUNT(*) FILTER (WHERE bed_released_at IS NOT NULL)
                 FROM hd_sessions WHERE session_date >= %s AND session_date < %s AND status IN ('completed','in_progress')""", (d0, d1))[0]
        out.update({"sessions": int(n[0]), "with_post_weight": int(n[1]), "released_bed": int(n[2])})
        if out["sessions"] == 0:
            out["insufficient"] = True
            return out
        r = q("""SELECT COUNT(DISTINCT v.session_id) FROM hd_vital_signs v JOIN hd_sessions s ON s.id = v.session_id
                 WHERE s.session_date >= %s AND s.session_date < %s AND v.sbp IS NOT NULL AND v.sbp < %s""", (d0, d1, L["bb_sbp_low"]))[0]
        out["sessions_sbp_low"] = int(r[0])
        r = q("""SELECT COUNT(*), COUNT(*) FILTER (WHERE a.acknowledged),
                        percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (a.acknowledged_at - COALESCE(a.triggered_at, a.created_at)))/60)
                 FROM hd_clinical_alerts a JOIN hd_sessions s ON s.id = a.session_id
                 WHERE s.session_date >= %s AND s.session_date < %s""", (d0, d1))[0]
        out.update({"alerts": int(r[0]), "alerts_acked": int(r[1]), "ack_median_min": round(float(r[2]), 1) if r[2] is not None else None})
        r = q("""SELECT COUNT(*) FROM hd_clinical_alerts a JOIN hd_sessions s ON s.id = a.session_id
                 WHERE s.session_date >= %s AND s.session_date < %s AND a.acknowledged
                   AND EXTRACT(EPOCH FROM (a.acknowledged_at - COALESCE(a.triggered_at, a.created_at)))/60 > %s""", (d0, d1, L["bb_ack_min"]))[0]
        out["alerts_ack_late"] = int(r[0])
        # ช่องว่าง vital > N นาที ขณะฟอก (window function)
        r = q("""WITH v AS (
                   SELECT v.session_id, v.recorded_at,
                          LAG(v.recorded_at) OVER (PARTITION BY v.session_id ORDER BY v.recorded_at) AS prev
                   FROM hd_vital_signs v JOIN hd_sessions s ON s.id = v.session_id
                   WHERE s.session_date >= %s AND s.session_date < %s AND v.recorded_at IS NOT NULL)
                 SELECT COUNT(DISTINCT session_id) FROM v WHERE prev IS NOT NULL
                   AND EXTRACT(EPOCH FROM (recorded_at - prev))/60 > %s""", (d0, d1, L["bb_vital_gap_min"]))[0]
        out["sessions_with_vital_gap"] = int(r[0])
        r = q("""SELECT COUNT(DISTINCT u.session_id) FROM hd_uf_records u JOIN hd_sessions s ON s.id = u.session_id
                 JOIN hd_patient_mapping p ON p.id = s.patient_id
                 WHERE s.session_date >= %s AND s.session_date < %s AND p.dry_weight > 0 AND u.uf_rate > 0
                   AND u.uf_rate / p.dry_weight > %s""", (d0, d1, L["bb_uf_rate_high"]))[0]
        out["sessions_uf_rate_high"] = int(r[0])
        r = q("""SELECT COUNT(*) FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                 WHERE s.session_date >= %s AND s.session_date < %s AND s.post_weight IS NOT NULL AND p.dry_weight > 0
                   AND s.post_weight - p.dry_weight > %s""", (d0, d1, L["bb_post_over_dw_kg"]))[0]
        out["sessions_post_over_dw"] = int(r[0])
        r = q("""SELECT COUNT(*) FROM hd_audit_logs WHERE created_at >= %s AND created_at < %s
                 AND (action LIKE '%%time_edit%%' OR action LIKE '%%edit%%')""", (d0, d1))[0]
        out["edits"] = int(r[0])
        r = q("""SELECT COUNT(*) FROM hd_incidents WHERE incident_date >= %s AND incident_date < %s""", (d0, d1))[0]
        out["incidents"] = int(r[0])
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        out["error"] = type(e).__name__
    n = out["sessions"] or 1
    out["rates"] = {k: round(out.get(k, 0) * 100.0 / n, 1) for k in
                    ("sessions_sbp_low", "sessions_with_vital_gap", "sessions_uf_rate_high", "sessions_post_over_dw")}
    out["insufficient"] = out["sessions"] < 20
    return out


# ───────────────────────── 7) hash รายวันของ audit log (tamper-evident, ต้นทุนต่ำ) ─────────────────────────

def audit_day_hash(conn, day, prev_hash=""):
    """sha256 ของทุกแถว audit ของวันนั้น (เรียงตาม id) ต่อสายกับ hash วันก่อน → {date, rows, hash, prev}"""
    import hashlib
    with conn.cursor() as cur:
        cur.execute("""SELECT id, user_id, action, resource_type, resource_id, old_values, new_values, created_at
                       FROM hd_audit_logs WHERE created_at::date = %s ORDER BY id""", (day,))
        rows = cur.fetchall()
    h = hashlib.sha256()
    h.update((prev_hash or "").encode("utf-8"))
    for r in rows:
        h.update("|".join("" if v is None else str(v) for v in r).encode("utf-8"))
        h.update(b"\n")
    return {"date": day.isoformat(), "rows": len(rows), "hash": h.hexdigest(), "prev": prev_hash or ""}


def audit_hash_store(conn, rec, bucket=None):
    """เก็บใน hd_settings (bb_audit_hash_<date>) + GCS _audit_hash/<date>.json (create-only) — ไม่ทับของเดิม"""
    import json as _json
    key = f"bb_audit_hash_{rec['date']}"
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO hd_settings (key, value, description, updated_at)
                       VALUES (%s, %s, %s, NOW()) ON CONFLICT (key) DO NOTHING""",
                    (key, _json.dumps(rec), "📼 audit log daily hash (tamper-evident)"))
    try:
        conn.commit()
    except Exception:
        pass
    gcs_ok = False
    if bucket is not None:
        try:
            bucket.blob(f"_audit_hash/{rec['date']}.json").upload_from_string(
                _json.dumps(rec), content_type="application/json", if_generation_match=0)
            gcs_ok = True
        except Exception as e:  # noqa: BLE001
            gcs_ok = getattr(e, "code", None) == 412   # มีอยู่แล้ว = ถือว่าเก็บแล้ว
    return gcs_ok


def audit_hash_run(conn, day, bucket=None):
    """งาน 22:40: ต่อสายจาก hash เมื่อวาน (ถ้ามี) แล้วเก็บ"""
    import json as _json
    prev = ""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM hd_settings WHERE key = %s", (f"bb_audit_hash_{(day - timedelta(days=1)).isoformat()}",))
        r = cur.fetchone()
        if r and r[0]:
            try:
                prev = _json.loads(r[0]).get("hash", "")
            except Exception:
                prev = ""
    rec = audit_day_hash(conn, day, prev)
    rec["gcs"] = audit_hash_store(conn, rec, bucket)
    return rec


def audit_hash_verify(conn, day):
    """คำนวณใหม่เทียบกับที่เก็บไว้ → {stored, recomputed, match}"""
    import json as _json
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM hd_settings WHERE key = %s", (f"bb_audit_hash_{day.isoformat()}",))
        r = cur.fetchone()
    if not r or not r[0]:
        return {"date": day.isoformat(), "stored": None, "match": None, "note": "ยังไม่มี hash ของวันนี้ (รันตอน 22:40)"}
    stored = _json.loads(r[0])
    rec = audit_day_hash(conn, day, stored.get("prev", ""))
    return {"date": day.isoformat(), "stored": stored, "recomputed": rec, "match": rec["hash"] == stored.get("hash"),
            "rows_now": rec["rows"], "rows_then": stored.get("rows")}


# ───────────────────────── 8) ชั้น AI เรียบเรียง "ข้อสันนิษฐาน" (flag แยก, ไม่ส่งชื่อ/HN) ─────────────────────────

def ai_enabled():
    if os.environ.get("HD_BLACKBOX_AI", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        from .blueprint import get_setting_value
        return str(get_setting_value("blackbox_ai_enabled", "") or "").strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return False


def deidentified_payload(tl):
    """ตัดชื่อ/HN/ผู้ทำ ออก — ส่งเฉพาะตัวเลขและข้อความเหตุการณ์"""
    s = tl["session"]
    return {
        "session": {k: s.get(k) for k in ("date", "shift", "started_at", "ended_at", "pre_weight", "post_weight", "dry_weight",
                                           "uf_goal", "actual_uf", "duration_hours")},
        "events": [{"n": e["n"], "t": _th(e["ts"]), "kind": e["kind"], "text": e["text"],
                    "data": {k: v for k, v in e["data"].items() if k not in ("old", "new")}} for e in tl["events"]],
        "facts": tl["facts"], "missing": tl["missing"],
        "hypotheses": [f"{h['rule']} {h['text']}" for h in tl["hypotheses"]],
        "alert_reviews": [{"n": r["n"], "title": r["title"], "signals": r["signals"], "timing": r["timing"]} for r in tl["alert_reviews"]],
    }


AI_PROMPT = (
    "คุณช่วยแพทย์โรคไตทบทวนรอบฟอกเลือด 1 รอบจากข้อมูลเหตุการณ์ที่ให้ (JSON) "
    "เขียนภาษาไทย ≤ 12 บรรทัด แบ่ง 3 ส่วน: (1) ลำดับเหตุการณ์สำคัญ — อ้างหมายเลข #n ทุกข้อ "
    "(2) สัญญาณที่เห็นก่อน alert (ถ้ามี) (3) ข้อสันนิษฐานให้แพทย์ทบทวน — ขึ้นต้นทุกข้อด้วย 'ข้อสันนิษฐาน:' "
    "ห้ามสรุปสาเหตุเป็นข้อเท็จจริง ห้ามแนะนำการรักษา/ปรับเครื่อง/ยา ห้ามเติมข้อมูลที่ไม่มีใน JSON "
    "ถ้าข้อมูลไม่พอให้เขียน 'ข้อมูลไม่พอ' ตรง ๆ"
)


def ai_narrative(tl, timeout=25):
    """คืน {text, model} หรือ {error}; ใช้ Claude ผ่าน API key เดิม — ไม่ส่งชื่อ/HN"""
    import json as _json
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return {"error": "ไม่มี ANTHROPIC_API_KEY"}
    try:
        import requests as _rq
        from .patient_engage import ai_model
        body = _json.dumps({
            "model": ai_model(), "max_tokens": 900,
            "messages": [{"role": "user", "content": AI_PROMPT + "\n\n" + _json.dumps(deidentified_payload(tl), ensure_ascii=False, default=str)}],
        })
        resp = _rq.post("https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                        data=body, timeout=timeout)
        if resp.status_code != 200:
            return {"error": f"AI HTTP {resp.status_code}"}
        txt = "".join(x.get("text", "") for x in (resp.json().get("content") or []) if x.get("type") == "text").strip()
        return {"text": txt or "ข้อมูลไม่พอ", "model": ai_model()}
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__}


# ───────────────────────── 9) คิว "รอบที่ควรทบทวน" ของวัน (แดชบอร์ด/ตอนเช้า) ─────────────────────────

_RQ_CACHE = {}


def review_queue(conn, day, limits=None, ttl=600):
    """รอบของวันนั้นที่มีจุดที่ควรทบทวน/ช่องว่างข้อมูล — cache ในหน่วยความจำ 10 นาที"""
    import time as _t
    ck = (day.isoformat(), tuple(sorted((limits or {}).items())))
    hit = _RQ_CACHE.get(ck)
    if hit and _t.time() - hit[0] < ttl:
        return hit[1]
    with conn.cursor() as cur:
        cur.execute("""SELECT id FROM hd_sessions WHERE session_date = %s AND status IN ('completed','in_progress')
                       ORDER BY bed_number, id""", (day,))
        ids = [r[0] for r in cur.fetchall()]
    items = []
    for sid in ids[:60]:
        try:
            tl = build(conn, sid, limits)
        except Exception:
            tl = None
        if not tl:
            continue
        if tl["hypotheses"] or tl["gaps"]:
            s = tl["session"]
            items.append({"session_id": sid, "bed": s["bed"], "hn": s["hn"], "patient_name": s["patient_name"],
                          "hypotheses": len(tl["hypotheses"]), "gaps": len(tl["gaps"]), "alerts": sum(1 for e in tl["events"] if e["kind"] == "alert"),
                          "top": (tl["hypotheses"][0]["text"] if tl["hypotheses"] else tl["gaps"][0]["text"])})
    out = {"date": day.isoformat(), "sessions": len(ids), "to_review": items}
    _RQ_CACHE[ck] = (_t.time(), out)
    return out
