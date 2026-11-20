"""VA Watch — vascular-access surveillance from the flowsheet (ว้าว #2).

Every 30-minute sheet row already records BFR, venous pressure (VP) and
arterial pressure (AP). Per completed session we take the median of those
values, then look at the trend over the patient's last 10 sessions and at
the newest Kt/V / URR. Rules below are deliberately simple and transparent
(KDOQI-style surveillance); the output is "ควรตรวจ access" — never an order.

Session-level signals (each session summarised by medians):
    VP > 200 mmHg with BFR ≤ 300          outflow stenosis suspicion
    VP rising ≥ 25 % vs the patient's own baseline (first 3 of last 10)
    AP < −250 mmHg (more negative)         inflow problem / needle position
    BFR achieved < 85 % of prescribed in ≥ 2 of last 3 sessions
    Kt/V < 1.2 or URR < 65 % (newest lab)  under-dialysis — recirculation?

Levels: alert ≥ 50 · watch 25–49 · ok < 25
"""
import logging
from statistics import median

logger = logging.getLogger(__name__)


def _med(xs):
    xs = [float(x) for x in xs if x is not None]
    return median(xs) if xs else None


def summarize_session(rows):
    """rows: iterable of (bfr, vp, ap). Returns {"bfr","vp","ap","n"} medians."""
    rows = list(rows)
    return {"bfr": _med(r[0] for r in rows), "vp": _med(r[1] for r in rows),
            "ap": _med(r[2] for r in rows), "n": len(rows)}


def assess(sessions, prescribed_bfr=None, ktv=None, urr=None, access_type=""):
    """sessions: list oldest→newest of {"date", "bfr", "vp", "ap"} (session medians).
    Returns {"score", "level", "signals": [{"text","points"}], "latest": {...}, "baseline_vp"}"""
    pts = 0
    sig = []
    ss = [s for s in sessions if s.get("vp") is not None or s.get("bfr") is not None][-10:]
    latest = ss[-1] if ss else {}
    is_catheter = "cath" in (access_type or "").lower() or "perm" in (access_type or "").lower() \
        or "dlc" in (access_type or "").lower() or "สาย" in (access_type or "")

    vp, bfr, ap = latest.get("vp"), latest.get("bfr"), latest.get("ap")
    if vp is not None and vp > 200 and (bfr is None or bfr <= 300) and not is_catheter:
        pts += 25; sig.append({"text": f"VP {vp:.0f} mmHg ที่ BFR {bfr or '?'} (>200) — สงสัย outflow stenosis", "points": 25})
    elif vp is not None and vp > 250:
        pts += 15; sig.append({"text": f"VP {vp:.0f} mmHg (>250)", "points": 15})

    base_vals = [s["vp"] for s in ss[:3] if s.get("vp") is not None]
    base = _med(base_vals) if len(base_vals) >= 2 else None
    if base and vp is not None and len(ss) >= 5:
        rise = (vp - base) / base * 100
        recent = [s["vp"] for s in ss[-3:] if s.get("vp") is not None]
        if rise >= 25 and len(recent) >= 2 and min(recent) > base * 1.15:
            pts += 25; sig.append({"text": f"VP เพิ่มขึ้น {rise:.0f}% จาก baseline {base:.0f} → {vp:.0f} (ต่อเนื่อง 3 รอบ)", "points": 25})
        elif rise >= 15 and len(recent) >= 2 and min(recent) > base * 1.10:
            pts += 12; sig.append({"text": f"VP มีแนวโน้มสูงขึ้น {rise:.0f}% จาก baseline {base:.0f}", "points": 12})

    if ap is not None and ap < -250:
        pts += 20; sig.append({"text": f"AP {ap:.0f} mmHg (< −250) — inflow ไม่พอ/ตำแหน่งเข็ม", "points": 20})
    elif ap is not None and ap < -200:
        pts += 10; sig.append({"text": f"AP {ap:.0f} mmHg (< −200)", "points": 10})

    if prescribed_bfr:
        last3 = [s["bfr"] for s in ss[-3:] if s.get("bfr") is not None]
        low = [b for b in last3 if b < 0.85 * float(prescribed_bfr)]
        if len(low) >= 2:
            pts += 20; sig.append({"text": f"BFR ได้จริง {', '.join(f'{b:.0f}' for b in last3)} < 85% ของที่สั่ง {prescribed_bfr} ({len(low)}/3 รอบ)", "points": 20})
        elif len(low) == 1 and len(last3) == 1:
            pts += 8; sig.append({"text": f"BFR ได้จริง {low[0]:.0f} < 85% ของที่สั่ง {prescribed_bfr}", "points": 8})

    try:
        if ktv is not None and float(ktv) < 1.2:
            pts += 15; sig.append({"text": f"Kt/V {float(ktv):.2f} < 1.2 — ฟอกไม่พอ/recirculation?", "points": 15})
        elif urr is not None and float(urr) < 65:
            pts += 15; sig.append({"text": f"URR {float(urr):.0f}% < 65", "points": 15})
    except (TypeError, ValueError):
        pass

    pts = min(100, pts)
    level = "alert" if pts >= 50 else ("watch" if pts >= 25 else "ok")
    advice = {"alert": "ตรวจ access (thrill/bruit, แขนบวม, เลือดออกนานหลังถอดเข็ม) · พิจารณา Doppler/fistulogram",
              "watch": "สังเกต VP/AP รอบถัดไป · ตรวจตำแหน่งเข็มและขนาดเข็ม", "ok": ""}[level]
    return {"score": pts, "level": level, "signals": sig, "latest": latest, "baseline_vp": base,
            "sessions": ss, "advice": advice, "is_catheter": is_catheter}


def collect(conn, days=120):
    """Assess every active patient. Returns list sorted by score desc."""
    out = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.patient_id, s.id, s.session_date,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY f.bfr) FILTER (WHERE f.bfr IS NOT NULL),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY f.vp)  FILTER (WHERE f.vp IS NOT NULL),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY f.ap)  FILTER (WHERE f.ap IS NOT NULL)
            FROM hd_sessions s JOIN hd_flowsheet f ON f.session_id = s.id
            WHERE s.session_date >= CURRENT_DATE - %s AND s.status IN ('completed', 'in_progress')
            GROUP BY s.patient_id, s.id, s.session_date
            ORDER BY s.patient_id, s.session_date, s.id""", (days,))
        per = {}
        for pid, sid, d, bfr, vp, ap in cur.fetchall():
            per.setdefault(pid, []).append({"session_id": sid, "date": d.isoformat(), "bfr": bfr, "vp": vp, "ap": ap})
        if not per:
            return out
        cur.execute("""
            SELECT p.id, p.hn, p.patient_name, p.vascular_access,
                   (SELECT blood_flow_rate FROM hd_prescriptions r WHERE r.patient_id = p.id ORDER BY r.id DESC LIMIT 1)
            FROM hd_patient_mapping p WHERE p.id = ANY(%s)""", (list(per.keys()),))
        pats = {r[0]: r[1:] for r in cur.fetchall()}
        labs = {}
        try:
            cur.execute("SAVEPOINT va_lab")
            cur.execute("""SELECT DISTINCT ON (patient_id) patient_id, ktv, urr, lab_date
                           FROM hd_labs WHERE patient_id = ANY(%s)
                           ORDER BY patient_id, lab_date DESC NULLS LAST""", (list(per.keys()),))
            labs = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
            cur.execute("RELEASE SAVEPOINT va_lab")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT va_lab")
        # fall back to session-recorded kt_v / urr
        cur.execute("""SELECT DISTINCT ON (patient_id) patient_id, kt_v, urr FROM hd_sessions
                       WHERE patient_id = ANY(%s) AND (kt_v IS NOT NULL OR urr IS NOT NULL)
                       ORDER BY patient_id, session_date DESC, id DESC""", (list(per.keys()),))
        for pid, k, u in cur.fetchall():
            labs.setdefault(pid, (k, u, None))
    for pid, sess in per.items():
        hn, name, va, rx_bfr = pats.get(pid, (None, None, None, None))
        k, u, _ = labs.get(pid, (None, None, None))
        a = assess(sess, prescribed_bfr=rx_bfr, ktv=k, urr=u, access_type=va or "")
        a.update({"patient_id": pid, "hn": hn, "name": name, "access": va, "prescribed_bfr": rx_bfr,
                  "ktv": float(k) if k is not None else None, "urr": float(u) if u is not None else None})
        out.append(a)
    out.sort(key=lambda x: (-x["score"], x["name"] or ""))
    return out


LEVEL_TH = {"alert": "🔴 ควรตรวจ access", "watch": "🟠 เฝ้าระวัง", "ok": "🟢 ปกติ"}


def brief_lines(items, limit=5):
    """Short block for the Morning Brief (alert + watch only)."""
    hot = [i for i in items if i["level"] != "ok"]
    if not hot:
        return []
    L = [f"\n🩸 VA Watch — {len(hot)} คน ควรดู access"]
    for i in hot[:limit]:
        first = i["signals"][0]["text"].split(" — ")[0] if i["signals"] else ""
        L.append(f"  {'🔴' if i['level'] == 'alert' else '🟠'} {i['name']} (HN {i['hn']}) {first}")
    if len(hot) > limit:
        L.append(f"  … อีก {len(hot) - limit} คน")
    return L
