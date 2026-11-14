"""UF Watch — is ultrafiltration on plan? (14 Nov 2026, approved items 1–3)

Pure evaluation used by: bed cards + TV (badge), the session page (chart),
shift handover (summary line) and record_uf (IDH-linked alert).

Plan = linear: expected_removed(t) = goal × elapsed / duration.
Flags, by priority:
    high_rate   rate needed for the rest of the session > uf_max mL/kg/h (default 13)
    no_record   session running > record_every min and no UF recorded yet
    overdue     last UF record older than record_every min (default 65)
    behind      removed < expected − max(150 mL, 10 % of goal)
    caution     needed rate 10–13 mL/kg/h
    ok
    no_goal     nothing to compare against
Advisory only — the nurse/physician decides what to change on the machine.
"""
from datetime import datetime, timedelta, timezone

DEFAULTS = {"uf_max": 13.0, "uf_caution": 10.0, "record_every_min": 65, "behind_min_ml": 150, "behind_pct": 10}
FLAG_TH = {"high_rate": "⛔ UF ต้องใช้อัตราเกินเพดาน", "no_record": "⏰ ยังไม่บันทึก UF", "overdue": "⏰ ไม่ได้บันทึก UF เกิน 1 ชม.",
           "behind": "🐢 UF ช้ากว่าแผน", "caution": "⚠️ อัตรา UF ที่ต้องใช้สูง", "ok": "✅ UF ตามแผน", "no_goal": ""}
PRIORITY = {"high_rate": 3, "no_record": 2, "overdue": 2, "behind": 2, "caution": 1, "ok": 0, "no_goal": 0}


def _utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def evaluate(goal, removed, started_at, duration_h, dry_weight=None, last_record_at=None, now=None, limits=None):
    """→ dict(flag, flag_th, pct, expected, behind_ml, elapsed_min, left_min, need_rate, need_rate_kg,
              avg_rate, since_record_min, text). Never raises on missing data."""
    lim = dict(DEFAULTS, **(limits or {}))
    now = _utc(now) or datetime.now(timezone.utc)
    out = {"flag": "no_goal", "pct": None, "expected": None, "behind_ml": None, "elapsed_min": None, "left_min": None,
           "need_rate": None, "need_rate_kg": None, "avg_rate": None, "since_record_min": None, "text": ""}
    try:
        goal = float(goal) if goal else 0.0
        dur = float(duration_h) if duration_h else 4.0
        dw = float(dry_weight) if dry_weight else None
        rem = float(removed) if removed is not None else None
    except (TypeError, ValueError):
        return dict(out, flag_th="")
    st = _utc(started_at)
    if st:
        elapsed = max(0.0, (now - st).total_seconds() / 3600.0)
        out["elapsed_min"] = int(round(elapsed * 60))
        out["left_min"] = int(round(max(0.0, dur - elapsed) * 60))
    else:
        elapsed = None
    if last_record_at:
        out["since_record_min"] = int(round((now - _utc(last_record_at)).total_seconds() / 60))
    if goal <= 0 or elapsed is None:
        return dict(out, flag_th="")
    el = min(elapsed, dur)
    left = max(0.0, dur - elapsed)
    out["expected"] = int(round(goal * el / dur))
    flag = "ok"
    if rem is None:
        if elapsed * 60 > lim["record_every_min"]:
            flag = "no_record"
        out["flag"] = flag
        out["flag_th"] = FLAG_TH[flag]
        out["text"] = f"ตามแผนควรดึงได้แล้ว {out['expected']} mL" + (" — ยังไม่มีการบันทึก UF" if flag == "no_record" else "")
        return out
    out["pct"] = int(round(rem / goal * 100))
    out["behind_ml"] = int(round(out["expected"] - rem))
    if el > 0.1:
        out["avg_rate"] = int(round(rem / el))
    remaining = max(0.0, goal - rem)
    if left > 0.05:
        out["need_rate"] = int(round(remaining / left))
        if dw:
            out["need_rate_kg"] = round(out["need_rate"] / dw, 1)
    nrk = out["need_rate_kg"]
    if nrk is not None and nrk > lim["uf_max"] and remaining > 0:
        flag = "high_rate"
    elif out["since_record_min"] is not None and out["since_record_min"] > lim["record_every_min"] and left > 0.05:
        flag = "overdue"
    elif out["behind_ml"] > max(lim["behind_min_ml"], goal * lim["behind_pct"] / 100.0):
        flag = "behind"
    elif nrk is not None and nrk > lim["uf_caution"] and remaining > 0:
        flag = "caution"
    out["flag"] = flag
    out["flag_th"] = FLAG_TH[flag]
    parts = [f"UF {int(rem)}/{int(goal)} mL ({out['pct']}%)"]
    if out["behind_ml"] > 100:
        parts.append(f"ช้ากว่าแผน {out['behind_ml']} mL")
    elif out["behind_ml"] < -100:
        parts.append(f"เร็วกว่าแผน {-out['behind_ml']} mL")
    if out["need_rate"] is not None and remaining > 0:
        parts.append(f"ต้องใช้ {out['need_rate']} mL/ชม." + (f" = {nrk} mL/kg/ชม." if nrk is not None else ""))
    if flag == "overdue":
        parts.append(f"บันทึกล่าสุด {out['since_record_min']} นาทีก่อน")
    out["text"] = " · ".join(parts)
    return out


def latest_record(cur, session_id):
    """(uf_removed, recorded_at) of the latest UF record, or (None, None)."""
    cur.execute("""SELECT uf_removed, recorded_at FROM hd_uf_records WHERE session_id = %s
                   ORDER BY recorded_at DESC, id DESC LIMIT 1""", (session_id,))
    r = cur.fetchone()
    return (float(r[0]), r[1]) if r and r[0] is not None else (None, (r[1] if r else None))


def for_session(cur, session_id, limits=None, now=None):
    """Evaluate a live session straight from the DB (read-only)."""
    cur.execute("""SELECT s.uf_goal, s.started_at, p.dry_weight,
                          COALESCE((SELECT r.duration_hours FROM hd_prescriptions r WHERE r.patient_id = s.patient_id
                                    AND r.is_active = TRUE ORDER BY r.id DESC LIMIT 1), 4.0)
                   FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id WHERE s.id = %s""", (session_id,))
    r = cur.fetchone()
    if not r:
        return None
    removed, at = latest_record(cur, session_id)
    return evaluate(r[0], removed, r[1], r[3], r[2], at, now=now, limits=limits)


def handover_lines(items):
    """items: [{"bed", "name", "uf": evaluate()}] → Thai lines for the shift handover (worst first)."""
    bad = [i for i in items if i.get("uf") and PRIORITY.get(i["uf"]["flag"], 0) >= 1]
    if not bad:
        return ["💧 UF: ทุกเตียงตามแผน"] if items else []
    bad.sort(key=lambda i: -PRIORITY.get(i["uf"]["flag"], 0))
    L = [f"💧 UF ต้องดู {len(bad)} เตียง:"]
    for i in bad[:6]:
        u = i["uf"]
        L.append(f"  เตียง {i['bed']}: {FLAG_TH.get(u['flag'], u['flag'])} — {u['text']}")
    return L
