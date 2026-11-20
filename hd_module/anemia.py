"""Anemia Command Center — iron + ESA + Hb trajectory per patient (ว้าว รอบ 2 — #4).

Pulls together what the system already records:
    Hb        : lab documents (hd_lab_docs), manual labs (hd_lab_results), session hb_value
    Iron      : ferritin / TSAT from the same lab sources
    ESA       : actual doses given in the last 4 weeks (hd_sessions.esa_*), fallback
                standing dose × sessions/week; dose parsed with esa_autopilot.parse_dose
    Iron given: hd_medication_given (category iron / drug name)
and derives, per patient:
    ERI  (ESA resistance index) = weekly ESA IU / kg / Hb
    iron status (KDOQI-HD: ferritin < 200 or TSAT < 20 % → iron deficient)
    Hb slope (g/dL per 4 weeks) and a simple 4-week projection
    one plain-language recommendation the physician can act on (propose only)

Thresholds are deliberately conservative and overridable through limits.
"""
import logging
import re
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEFAULTS = {"anemia_ferritin_low": 200.0, "anemia_tsat_low": 20.0, "anemia_ferritin_high": 800.0,
            "anemia_eri_high": 15.0, "anemia_eri_mod": 8.0, "anemia_hb_low": 10.0, "anemia_hb_high": 12.0}


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def parse_iu(text):
    """'4000 IU' → 4000; '40 mcg' darbepoetin → ×200 IU-equivalent; None if unknown."""
    if not text:
        return None
    m = re.search(r"(\d[\d,]*\.?\d*)\s*(iu|unit|u|mcg|µg|ug)?", str(text), re.I)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit in ("mcg", "µg", "ug"):
        return val * 200.0
    return val


def hb_slope(points):
    """points: [(date, hb)] oldest→newest → g/dL per 28 days using least squares over ≤ 6 points, or None."""
    pts = [(d, _f(h)) for d, h in points if _f(h) is not None][-6:]
    if len(pts) < 2:
        return None
    d0 = pts[0][0]
    xs = [((d - d0).days) / 28.0 for d, _ in pts]
    ys = [h for _, h in pts]
    if max(xs) - min(xs) < 0.25:   # under a week of spread — slope meaningless
        return None
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx, 2)


def assess(p, limits=None):
    """p: {hb, hb_date, hb_points, ferritin, tsat, esa_weekly_iu, weight, iron_recent_mg, iron_last_date,
           esa_drug, esa_dose, transfusion}
    → {"eri", "iron_status", "slope", "hb_4w", "priority": 0-3, "tag", "recommendation", "reasons": []}"""
    lim = dict(DEFAULTS, **(limits or {}))
    hb, fer, tsat, w = _f(p.get("hb")), _f(p.get("ferritin")), _f(p.get("tsat")), _f(p.get("weight"))
    esa_wk = _f(p.get("esa_weekly_iu"))
    reasons, rec, tag, pri = [], "", "ok", 0
    eri = round(esa_wk / w / hb, 1) if (esa_wk and w and hb) else None

    if fer is None and tsat is None:
        iron = "unknown"
    elif (fer is not None and fer < float(lim["anemia_ferritin_low"])) or (tsat is not None and tsat < float(lim["anemia_tsat_low"])):
        iron = "deficient"
    elif fer is not None and fer > float(lim["anemia_ferritin_high"]) and (tsat is None or tsat < float(lim["anemia_tsat_low"])):
        iron = "functional"      # iron locked away — inflammation
    elif fer is not None and fer > float(lim["anemia_ferritin_high"]):
        iron = "overload"
    else:
        iron = "replete"

    slope = hb_slope(p.get("hb_points") or [])
    hb_4w = round(hb + slope, 1) if (hb is not None and slope is not None) else None
    lo, hi = float(lim["anemia_hb_low"]), float(lim["anemia_hb_high"])

    if hb is None:
        return {"eri": eri, "iron_status": iron, "slope": slope, "hb_4w": None, "priority": 1, "tag": "no_hb",
                "recommendation": "ยังไม่มี Hb ใน 90 วัน — ส่งตรวจ CBC", "reasons": ["ไม่มีผล Hb"]}

    if hb < lo:
        if iron == "deficient":
            pri, tag = 3, "iron_first"
            rec = "Hb ต่ำ + ขาดเหล็ก → ให้เหล็ก IV ก่อน (ยังไม่เพิ่ม ESA)"
            reasons.append(f"ferritin {fer:g}" if fer is not None else "ferritin ?")
            reasons.append(f"TSAT {tsat:g}%" if tsat is not None else "TSAT ?")
        elif iron == "unknown":
            pri, tag = 2, "check_iron"
            rec = "Hb ต่ำ แต่ไม่มี ferritin/TSAT → ส่ง iron study ก่อนปรับ ESA"
        elif eri is not None and eri >= float(lim["anemia_eri_high"]):
            pri, tag = 3, "hyporesponse"
            rec = f"ESA ไม่ตอบสนอง (ERI {eri}) → หาสาเหตุ: อักเสบ/ติดเชื้อ, hyperparathyroid (PTH), เลือดออกเรื้อรัง, ขาด B12/folate, ฟอกไม่พอ"
            reasons.append(f"ERI {eri} ≥ {lim['anemia_eri_high']:g}")
        elif iron == "functional":
            pri, tag = 2, "inflammation"
            rec = "Hb ต่ำ, ferritin สูงแต่ TSAT ต่ำ (functional iron deficiency) → หาแหล่งอักเสบ/ติดเชื้อ; ESA ตาม protocol"
        else:
            pri, tag = 2, "esa_up"
            rec = "Hb ต่ำ เหล็กพอ → ปรับ ESA ตาม protocol (ESA Autopilot เสนอให้)"
        if slope is not None and slope <= -1.0:
            reasons.append(f"Hb ลดลง {abs(slope):.1f} g/dL/4 สัปดาห์")
            if tag in ("esa_up",):
                rec += " · Hb ลดเร็ว — ตรวจเลือดออก/การได้รับ ESA จริง"
    elif hb > hi:
        pri, tag = 2 if hb > 13 else 1, "hb_high"
        rec = "Hb เกินเป้า → ESA Autopilot เสนอลด/งด; ถ้าได้เหล็ก IV อยู่ให้หยุด"
        if slope is not None and slope >= 2.0:
            reasons.append(f"Hb ขึ้นเร็ว +{slope:.1f} g/dL/4 สัปดาห์")
            pri = 2
    else:
        tag = "ok"
        if iron == "deficient":
            pri, tag = 1, "iron_maint"
            rec = "Hb ในเป้า แต่เหล็กต่ำ → เติมเหล็กเพื่อคง Hb และประหยัด ESA"
        elif hb_4w is not None and (hb_4w < lo or hb_4w > hi):
            pri, tag = 1, "drifting"
            rec = f"Hb ในเป้าแต่แนวโน้มจะ{'ต่ำกว่า' if hb_4w < lo else 'เกิน'}เป้าใน 4 สัปดาห์ (≈{hb_4w}) → เฝ้าระวัง/ปรับก่อน"
        else:
            rec = "คงแผนเดิม"
    if iron == "overload":
        reasons.append(f"ferritin {fer:g} สูง — งดเหล็ก")
        if tag in ("iron_first", "iron_maint"):
            tag, rec = "overload", "ferritin สูงมาก → งดเหล็ก ทบทวนสาเหตุ"
    if eri is not None and tag not in ("hyporesponse",) and eri >= float(lim["anemia_eri_mod"]):
        reasons.append(f"ERI {eri} (ปานกลาง)")
    return {"eri": eri, "iron_status": iron, "slope": slope, "hb_4w": hb_4w, "priority": pri, "tag": tag,
            "recommendation": rec, "reasons": reasons}


TAG_TH = {"iron_first": "🧲 ให้เหล็กก่อน", "check_iron": "🔬 ส่ง iron study", "hyporesponse": "⚠️ ESA ไม่ตอบสนอง",
          "inflammation": "🔥 functional iron def.", "esa_up": "💊 ปรับ ESA", "hb_high": "🔺 Hb สูง",
          "iron_maint": "🧲 เติมเหล็ก", "drifting": "📉 แนวโน้มออกนอกเป้า", "overload": "⛔ เหล็กเกิน",
          "no_hb": "❓ ไม่มี Hb", "ok": "✅ คงแผน"}


def collect(conn, limits=None, days=90):
    """Assess every active patient (read-only). Returns list sorted by priority desc."""
    since = (_thai_today() - timedelta(days=days)).isoformat()
    since_long = (_thai_today() - timedelta(days=180)).isoformat()
    out = []
    with conn.cursor() as cur:
        cur.execute("""SELECT id, hn, patient_name, dry_weight, esa_drug, esa_dose, COALESCE(esa_per_week, hd_per_week, 3)
                       FROM hd_patient_mapping WHERE is_active = TRUE ORDER BY patient_name""")
        pats = cur.fetchall()
        ids = [p[0] for p in pats]
        if not ids:
            return out
        # Hb points from all sources
        hbp = {}
        for sql in (
            """SELECT patient_id, lab_date, hb FROM hd_labs
               WHERE patient_id = ANY(%s) AND hb IS NOT NULL AND lab_date >= %s""",
            """SELECT patient_id, session_date, hb_value FROM hd_sessions
               WHERE patient_id = ANY(%s) AND hb_value IS NOT NULL AND session_date >= %s"""):
            try:
                cur.execute("SAVEPOINT an")
                cur.execute(sql, (ids, since_long))
                for pid, d, hb in cur.fetchall():
                    if d:
                        hbp.setdefault(pid, {})[d] = float(hb)
                cur.execute("RELEASE SAVEPOINT an")
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT an")
                logger.info("anemia hb source skipped: %s", str(e)[:80])
        # iron studies (newest)
        iron = {}
        for sql in (
            """SELECT DISTINCT ON (patient_id) patient_id, lab_date, ferritin, tsat FROM hd_labs
               WHERE patient_id = ANY(%s) AND (ferritin IS NOT NULL OR tsat IS NOT NULL)
               ORDER BY patient_id, lab_date DESC NULLS LAST""",):
            try:
                cur.execute("SAVEPOINT an2")
                cur.execute(sql, (ids,))
                for pid, d, fer, ts in cur.fetchall():
                    cur_d = iron.get(pid, (None,))[0]
                    if cur_d is None or (d and d > cur_d):
                        iron[pid] = (d, _f(fer), _f(ts))
                cur.execute("RELEASE SAVEPOINT an2")
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT an2")
                logger.info("anemia iron source skipped: %s", str(e)[:80])
        # ESA actually given in the last 28 days
        esa_given = {}
        try:
            cur.execute("SAVEPOINT an3")
            cur.execute("""SELECT patient_id, esa_dose_given, esa_drug_given, esa_given FROM hd_sessions
                           WHERE patient_id = ANY(%s) AND session_date >= CURRENT_DATE - 28 AND status = 'completed'""", (ids,))
            for pid, dose_txt, drug, given in cur.fetchall():
                esa_given.setdefault(pid, []).append((given, dose_txt, drug))
            cur.execute("RELEASE SAVEPOINT an3")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT an3")
        # iron given (90 days)
        iron_given = {}
        try:
            cur.execute("SAVEPOINT an4")
            cur.execute("""SELECT s.patient_id, g.given_at, g.drug_name, g.dose_given FROM hd_medication_given g
                           JOIN hd_sessions s ON s.id = g.session_id
                           LEFT JOIN hd_medication_orders o ON o.id = g.order_id
                           WHERE s.patient_id = ANY(%s) AND g.given_at >= %s
                             AND (o.drug_category = 'iron' OR g.drug_name ILIKE '%%iron%%' OR g.drug_name ILIKE '%%venofer%%'
                                  OR g.drug_name ILIKE '%%ferric%%' OR g.drug_name ILIKE '%%เหล็ก%%' OR g.drug_name ILIKE '%%sucrose%%')""",
                        (ids, since))
            for pid, at, name, dose in cur.fetchall():
                iron_given.setdefault(pid, []).append((at, name, dose))
            cur.execute("RELEASE SAVEPOINT an4")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT an4")

    for pid, hn, name, dw, esa_drug, esa_dose, per_week in pats:
        per_week = float(per_week) if per_week is not None else 3.0   # esa_per_week may be NUMERIC
        pts = sorted(hbp.get(pid, {}).items())
        recent = [(d, h) for d, h in pts if d.isoformat() >= since]
        hb, hb_date = (recent[-1][1], recent[-1][0]) if recent else (None, None)
        d_iron, fer, tsat = iron.get(pid, (None, None, None))
        # weekly ESA: actual doses in 28 days /4 ; fallback standing dose × per_week
        given = esa_given.get(pid, [])
        iu_given = [parse_iu(dose) or parse_iu(esa_dose) for g, dose, drug in given if g]
        if given:
            esa_wk = sum(x for x in iu_given if x) / 4.0
            sessions_4w = len(given)
            esa_rate = sum(1 for g, _, _ in given if g) / len(given)
        else:
            base = parse_iu(esa_dose)
            esa_wk = base * (per_week or 3) if base else None
            sessions_4w, esa_rate = 0, None
        ig = sorted(iron_given.get(pid, []))
        iron_mg = 0.0
        for _, _, dose in ig:
            m = re.search(r"(\d+(?:\.\d+)?)", str(dose or ""))
            if m:
                iron_mg += float(m.group(1))
        pdata = {"hb": hb, "hb_date": hb_date, "hb_points": pts, "ferritin": fer, "tsat": tsat, "iron_date": d_iron,
                 "esa_weekly_iu": esa_wk, "weight": _f(dw), "iron_recent_mg": iron_mg,
                 "iron_last_date": ig[-1][0] if ig else None, "esa_drug": esa_drug, "esa_dose": esa_dose}
        a = assess(pdata, limits)
        a.update({"patient_id": pid, "hn": hn, "name": name, "hb": hb, "hb_date": hb_date.isoformat() if hb_date else None,
                  "hb_points": [(d.isoformat(), h) for d, h in pts[-8:]], "ferritin": fer, "tsat": tsat,
                  "iron_date": d_iron.isoformat() if d_iron else None, "esa_drug": esa_drug, "esa_dose": esa_dose,
                  "esa_weekly_iu": round(esa_wk) if esa_wk else None, "esa_sessions_4w": sessions_4w,
                  "esa_given_rate": round(esa_rate * 100) if esa_rate is not None else None,
                  "iron_recent_mg": iron_mg, "iron_last_date": ig[-1][0].date().isoformat() if ig and hasattr(ig[-1][0], "date") else None,
                  "weight": _f(dw), "tag_th": TAG_TH.get(a["tag"], a["tag"])})
        out.append(a)
    out.sort(key=lambda x: (-x["priority"], x["hb"] if x["hb"] is not None else 0))
    return out


def iron_status_for(cur, patient_id, limits=None):
    """Quick helper for ESA Autopilot: (status, ferritin, tsat) from the newest iron study, any source."""
    lim = dict(DEFAULTS, **(limits or {}))
    best = (None, None, None)
    for sql in (
        """SELECT lab_date, ferritin, tsat FROM hd_labs
           WHERE patient_id = %s AND (ferritin IS NOT NULL OR tsat IS NOT NULL)
           ORDER BY lab_date DESC NULLS LAST LIMIT 1""",):
        try:
            cur.execute("SAVEPOINT ir")
            cur.execute(sql, (patient_id,))
            r = cur.fetchone()
            cur.execute("RELEASE SAVEPOINT ir")
            if r and r[0] and (best[0] is None or r[0] > best[0]):
                best = (r[0], _f(r[1]), _f(r[2]))
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT ir")
    d, fer, tsat = best
    if fer is None and tsat is None:
        return "unknown", None, None
    if (fer is not None and fer < float(lim["anemia_ferritin_low"])) or (tsat is not None and tsat < float(lim["anemia_tsat_low"])):
        return "deficient", fer, tsat
    return "replete", fer, tsat
