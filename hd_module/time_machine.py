"""⏳ Kidney Time Machine — 28 พ.ย. 2569.

One page per patient: the whole dialysis life on a time axis (labs, ESA/meds,
sessions, weight vs DW, VP, IDH, alerts) that can be scrolled back, plus a
"next 12 months" band projected from the patient's OWN trajectory, and a
what-if panel ("ถ้าปรับ X ล่ะ") that redraws the future lines.

Method (deliberately simple, explainable)
- Trend = Theil–Sen slope (median of pairwise slopes) over the last 180 days,
  needs ≥3 points; band = ±1.5 × MAD of residuals, widening with horizon.
- Projections are clamped to physiologic ranges and flagged "low confidence"
  when few points / long gaps.
- "ดัชนีเฝ้าระวัง 90 วัน" is an additive 0–100 index with every component
  shown — an index for prioritising attention, not a probability.
- What-if uses coarse clinical heuristics (shown to the user) and
  idh_fingerprint.plan_check for UF targets. Nothing here is written to the DB.
"""
import logging
import statistics
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

HORIZONS = (90, 180, 365)
TARGETS = {
    "hb": {"lo": 10.0, "hi": 12.0, "clamp": (5.0, 17.0), "unit": "g/dL", "th": "Hb"},
    "k": {"lo": 3.5, "hi": 5.5, "clamp": (2.5, 8.0), "unit": "mmol/L", "th": "K"},
    "phos": {"lo": 3.5, "hi": 5.5, "clamp": (1.0, 12.0), "unit": "mg/dL", "th": "Phos"},
    "pth": {"lo": 150.0, "hi": 600.0, "clamp": (5.0, 3000.0), "unit": "pg/mL", "th": "PTH"},
    "albumin": {"lo": 3.5, "hi": 5.5, "clamp": (1.5, 5.5), "unit": "g/dL", "th": "Albumin"},
    "ca": {"lo": 8.4, "hi": 10.2, "clamp": (6.0, 13.0), "unit": "mg/dL", "th": "Ca"},
    "ktv": {"lo": 1.2, "hi": 2.5, "clamp": (0.5, 2.5), "unit": "", "th": "Kt/V"},
    "vp": {"lo": 0.0, "hi": 250.0, "clamp": (50.0, 400.0), "unit": "mmHg", "th": "VP"},
    "idwg_pct": {"lo": 0.0, "hi": 4.0, "clamp": (-2.0, 12.0), "unit": "%DW", "th": "IDWG"},
}


def _today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def _d(x):
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except Exception:
        return None


def theil_sen(points):
    """points: [(date, value)] → (slope_per_day, intercept_at_last, mad) or None."""
    pts = sorted([(d, float(v)) for d, v in points if d is not None and v is not None], key=lambda p: p[0])
    if len(pts) < 3:
        return None
    t0 = pts[-1][0]
    xs = [(p[0] - t0).days for p in pts]
    ys = [p[1] for p in pts]
    slopes = []
    for i in range(len(xs)):
        for j in range(i + 1, len(xs)):
            if xs[j] != xs[i]:
                slopes.append((ys[j] - ys[i]) / (xs[j] - xs[i]))
    if not slopes:
        return None
    m = statistics.median(slopes)
    b = statistics.median([y - m * x for x, y in zip(xs, ys)])
    resid = [abs(y - (m * x + b)) for x, y in zip(xs, ys)]
    mad = statistics.median(resid) if resid else 0.0
    return m, b, mad


def project(key, points, today=None, window_days=180):
    """→ {last, slope_per_month, points:[{d,v,lo,hi}], crossing:{...}|None, confidence}"""
    today = today or _today()
    t = TARGETS.get(key, {"lo": None, "hi": None, "clamp": (-1e9, 1e9)})
    recent = [(d, v) for d, v in points if d is not None and v is not None and (today - d).days <= window_days]
    if len(recent) < 3:
        recent = [(d, v) for d, v in points if d is not None and v is not None][-4:]
    fit = theil_sen(recent)
    last = sorted([(d, v) for d, v in points if d is not None and v is not None])[-1] if points else None
    if not fit or not last:
        return {"last": {"d": last[0].isoformat(), "v": last[1]} if last else None, "slope_per_month": None, "points": [],
                "crossing": None, "confidence": "none", "n": len(recent)}
    m, b, mad = fit
    span = (recent[-1][0] - recent[0][0]).days if len(recent) >= 2 else 0
    conf = "high" if (len(recent) >= 5 and span >= 90) else ("med" if len(recent) >= 3 and span >= 45 else "low")
    base_d = last[0]
    out = []
    lo_c, hi_c = t["clamp"]
    for h in (0, 30, 60, 90, 120, 180, 270, 365):
        v = b + m * h
        band = 1.5 * mad * (1 + h / 180.0) + abs(m) * h * 0.25
        out.append({"d": (base_d + timedelta(days=h)).isoformat(), "v": round(max(lo_c, min(hi_c, v)), 2),
                    "lo": round(max(lo_c, min(hi_c, v - band)), 2), "hi": round(max(lo_c, min(hi_c, v + band)), 2)})
    crossing = None
    if t.get("lo") is not None or t.get("hi") is not None:
        for bound, name in ((t.get("hi"), "hi"), (t.get("lo"), "lo")):
            if bound is None or m == 0:
                continue
            days = (bound - b) / m
            if 0 < days <= 365 and ((name == "hi" and m > 0 and b < bound) or (name == "lo" and m < 0 and b > bound)):
                crossing = {"bound": bound, "which": name, "in_days": int(days), "date": (base_d + timedelta(days=int(days))).isoformat()}
                break
    return {"last": {"d": last[0].isoformat(), "v": last[1]}, "slope_per_month": round(m * 30, 3), "points": out,
            "crossing": crossing, "confidence": conf, "n": len(recent), "mad": round(mad, 3)}


# ───────────────────────── assemble ─────────────────────────

def _q(cur, sql, params=()):
    try:
        cur.execute("SAVEPOINT tm")
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT tm")
        return rows
    except Exception as e:  # noqa: BLE001
        logger.info("time_machine query skipped: %s", str(e)[:120])
        try:
            cur.execute("ROLLBACK TO SAVEPOINT tm")
        except Exception:
            pass
        return []


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def build(conn, hn, days=548, today=None):
    today = today or _today()
    since = (today - timedelta(days=days)).isoformat()
    with conn.cursor() as cur:
        r = _q(cur, """SELECT id, hn, patient_name, dry_weight, vascular_access, esa_drug, esa_dose, date_of_birth, hd_per_week, is_active
                       FROM hd_patient_mapping WHERE hn = %s""", (hn,))
        if not r:
            return None
        p = r[0]
        pid = p[0]
        age = None
        if p[7]:
            age = today.year - p[7].year - ((today.month, today.day) < (p[7].month, p[7].day))
        patient = {"id": pid, "hn": p[1], "name": p[2], "dry_weight": _f(p[3]), "access": p[4], "esa_drug": p[5], "esa_dose": p[6],
                   "age": age, "hd_per_week": p[8], "is_active": p[9]}
        series = {k: [] for k in ("hb", "k", "phos", "pth", "albumin", "ca", "ktv", "ferritin", "tsat", "hco3")}
        for row in _q(cur, """SELECT lab_date, hb, k, phos, pth, albumin, ca, ktv, ferritin, tsat, hco3 FROM hd_labs
                              WHERE patient_id = %s AND lab_date >= %s AND COALESCE(verified_status,'') <> 'rejected'
                              ORDER BY lab_date""", (pid, since)):
            d = row[0]
            for i, k in enumerate(("hb", "k", "phos", "pth", "albumin", "ca", "ktv", "ferritin", "tsat", "hco3")):
                v = _f(row[i + 1])
                if v is not None:
                    series[k].append({"d": d.isoformat(), "v": v})
        sessions = []
        for row in _q(cur, """SELECT s.id, s.session_date, s.status::text, s.pre_weight, s.post_weight, s.uf_goal, s.actual_uf, s.hb_value,
                                     s.esa_drug_given, s.esa_dose_given, s.kt_v,
                                     (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY f.vp) FROM hd_flowsheet f WHERE f.session_id = s.id AND f.vp IS NOT NULL),
                                     (SELECT MIN(v.sbp) FROM hd_vital_signs v WHERE v.session_id = s.id),
                                     (SELECT outcome FROM hd_idh_twin_state t WHERE t.session_id = s.id),
                                     (SELECT COUNT(*) FROM hd_clinical_alerts a WHERE a.session_id = s.id AND a.severity::text IN ('critical','warning'))
                              FROM hd_sessions s WHERE s.patient_id = %s AND s.session_date >= %s ORDER BY s.session_date, s.id""", (pid, since)):
            sessions.append({"id": row[0], "d": row[1].isoformat(), "status": row[2], "pre": _f(row[3]), "post": _f(row[4]), "uf_goal": _f(row[5]),
                             "actual_uf": _f(row[6]), "hb": _f(row[7]), "esa": (f"{row[8]} {row[9] or ''}".strip() if row[8] else None),
                             "ktv": _f(row[10]), "vp": _f(row[11]), "min_sbp": row[12], "idh": row[13], "alerts": row[14]})
        if not series["hb"]:
            series["hb"] = [{"d": s["d"], "v": s["hb"]} for s in sessions if s["hb"] is not None]
        if not series["ktv"]:
            series["ktv"] = [{"d": s["d"], "v": s["ktv"]} for s in sessions if s["ktv"] is not None]
        series["vp"] = [{"d": s["d"], "v": s["vp"]} for s in sessions if s["vp"] is not None]
        dw = patient["dry_weight"]
        series["weight"] = [{"d": s["d"], "pre": s["pre"], "post": s["post"]} for s in sessions if s["pre"] is not None or s["post"] is not None]
        series["idwg_pct"] = [{"d": s["d"], "v": round((s["pre"] - dw) / dw * 100, 2)} for s in sessions if s["pre"] is not None and dw]
        meds = [{"drug": r_[0], "dose": r_[1], "freq": r_[2], "start": r_[3].isoformat() if r_[3] else None, "active": r_[4],
                 "stop": r_[5].isoformat() if r_[5] else None, "cat": r_[6]}
                for r_ in _q(cur, """SELECT drug_name, dose, frequency, ordered_at, is_active,
                                           CASE WHEN is_active THEN NULL ELSE COALESCE(stopped_at, ordered_at) END, drug_category
                                    FROM hd_medication_orders WHERE patient_id = %s
                                      AND COALESCE(verify_status,'confirmed') IN ('confirmed','merged')
                                    ORDER BY ordered_at""", (pid,))]
        if not meds:
            meds = [{"drug": r_[0], "dose": r_[1], "freq": r_[2], "start": r_[3].isoformat() if r_[3] else None, "active": r_[4], "stop": None, "cat": r_[5]}
                    for r_ in _q(cur, """SELECT drug_name, dose, frequency, ordered_at, is_active, drug_category FROM hd_medication_orders
                                        WHERE patient_id = %s AND COALESCE(verify_status,'confirmed') IN ('confirmed','merged') ORDER BY ordered_at""", (pid,))]
        esa_events = []
        last = None
        for s in sessions:
            if s["esa"] and s["esa"] != last:
                esa_events.append({"d": s["d"], "text": s["esa"]})
                last = s["esa"]
        alerts = [{"d": r_[0].date().isoformat() if hasattr(r_[0], "date") else str(r_[0])[:10], "sev": r_[1], "title": r_[2]}
                  for r_ in _q(cur, """SELECT a.created_at, a.severity::text, COALESCE(a.title, a.alert_type) FROM hd_clinical_alerts a
                                      LEFT JOIN hd_sessions s ON s.id = a.session_id
                                      WHERE (a.patient_id = %s OR s.patient_id = %s) AND a.created_at >= %s AND a.severity::text IN ('critical','warning')
                                      ORDER BY a.created_at""", (pid, pid, since))]
        shadow = [{"d": r_[0].isoformat(), "sev": r_[1], "text": r_[2], "status": r_[3]}
                  for r_ in _q(cur, "SELECT last_seen, severity, text, status FROM hd_shadow_findings WHERE patient_id = %s ORDER BY last_seen DESC LIMIT 20", (pid,))]
        fp = None
        try:
            cur.execute("SAVEPOINT tm_fp")
            from .idh_fingerprint import fingerprint_for
            fp = fingerprint_for(cur, pid)
            cur.execute("RELEASE SAVEPOINT tm_fp")
        except Exception:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT tm_fp")
            except Exception:
                pass
        va = None
        try:
            cur.execute("SAVEPOINT tm_va")
            from .va_watch import collect as va_collect
            for it in va_collect(conn):
                if it.get("patient_id") == pid:
                    va = {"level": it.get("level"), "score": it.get("score"), "signals": it.get("signals"), "baseline_vp": it.get("baseline_vp")}
            cur.execute("RELEASE SAVEPOINT tm_va")
        except Exception:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT tm_va")
            except Exception:
                pass

    forecast = {}
    for k in ("hb", "k", "phos", "pth", "albumin", "ca", "ktv", "vp", "idwg_pct"):
        pts = [(_d(x["d"]), x["v"]) for x in series.get(k, []) if x.get("v") is not None]
        forecast[k] = project(k, pts, today)
        forecast[k]["target"] = {kk: TARGETS[k].get(kk) for kk in ("lo", "hi", "unit", "th")}
    done = [s for s in sessions if s["status"] == "completed"]
    last10 = done[-10:]
    idh_known = [s for s in last10 if s["idh"] in ("idh", "no_idh")]
    idh_n = sum(1 for s in idh_known if s["idh"] == "idh")
    missed = sum(1 for s in sessions if s["status"] == "cancelled" and (today - _d(s["d"])).days <= 60)
    risk = risk_index(patient, forecast, idh_n, len(idh_known), missed, va)
    summary = {"sessions": len(sessions), "completed": len(done), "idh_last10": idh_n, "idh_known": len(idh_known), "missed_60d": missed,
               "last_session": done[-1]["d"] if done else None}
    return {"patient": patient, "series": series, "sessions": sessions[-120:], "meds": meds, "esa_events": esa_events, "alerts": alerts,
            "shadow": shadow, "forecast": forecast, "fingerprint": fp, "va": va, "risk": risk, "summary": summary,
            "generated": today.isoformat()}


def risk_index(patient, forecast, idh_n, idh_known, missed, va):
    """Additive 0–100 'ดัชนีเฝ้าระวัง 90 วัน' with every component shown."""
    comp = []

    def last(k):
        f = forecast.get(k) or {}
        return (f.get("last") or {}).get("v")

    alb = last("albumin")
    if alb is not None:
        if alb < 3.0:
            comp.append(("Albumin < 3.0", 25))
        elif alb < 3.5:
            comp.append(("Albumin 3.0–3.5", 12))
    hb = last("hb")
    if hb is not None and hb < 9:
        comp.append(("Hb < 9", 12))
    k = last("k")
    if k is not None and k >= 6:
        comp.append(("K ≥ 6", 12))
    ph = last("phos")
    if ph is not None and ph > 7:
        comp.append(("Phos > 7", 8))
    idwg = last("idwg_pct")
    if idwg is not None and idwg > 5:
        comp.append(("IDWG > 5% DW", 10))
    if idh_known >= 3 and idh_n >= 3:
        comp.append((f"IDH {idh_n}/{idh_known} รอบล่าสุด", 15))
    if missed >= 2:
        comp.append((f"ขาดฟอก {missed} รอบ/60 วัน", 15))
    if va and va.get("level") == "alert":
        comp.append(("Access alert (VA Watch)", 15))
    elif va and va.get("level") == "watch":
        comp.append(("Access watch", 7))
    age = patient.get("age")
    if age and age >= 75:
        comp.append(("อายุ ≥ 75", 8))
    ktv = last("ktv")
    if ktv is not None and ktv < 1.2:
        comp.append(("Kt/V < 1.2", 8))
    total = min(100, sum(p for _, p in comp))
    level = "high" if total >= 50 else ("med" if total >= 25 else "low")
    return {"index": total, "level": level, "components": [{"text": t, "points": p} for t, p in comp],
            "note": "ดัชนีจัดลำดับความสนใจจากข้อมูลในระบบ ไม่ใช่ความน่าจะเป็นที่ผ่านการสอบเทียบ"}


# ───────────────────────── what-if ─────────────────────────

WHATIF_RULES = {
    "esa_pct": "ESA ±25% ≈ Hb ±0.5 g/dL ต่อเดือน (3 เดือนแรก แล้วค่อย ๆ อิ่มตัว) — ถ้าเหล็กพอ",
    "binder": "เพิ่ม/ปรับ binder ≈ Phos −0.8 mg/dL ภายใน 2 เดือน (ถ้ากินตามนัด)",
    "vitd": "active vit D ≈ PTH −20% ใน 3 เดือน · Ca +0.2–0.4",
    "diet_k": "ให้คำปรึกษาอาหารโพแทสเซียม ≈ K −0.3–0.5",
    "dw_delta": "ปรับ DW ลง 0.5 kg ≈ IDWG% คงเดิมแต่ UF ต่อรอบเพิ่ม → เสี่ยง IDH ตาม fingerprint",
    "uf_goal": "เป้า UF เทียบกับซองปลอดภัยส่วนตัว (IDH Fingerprint)",
}


def what_if(data, changes):
    """changes: {esa_pct, binder(bool), vitd(bool), diet_k(bool), uf_goal_ml, dw_delta} → adjusted forecast subset + notes."""
    out = {"adjusted": {}, "notes": [], "rules": WHATIF_RULES}
    fc = data.get("forecast") or {}
    esa = _f(changes.get("esa_pct")) or 0.0
    if esa and fc.get("hb", {}).get("points"):
        per_month = 0.5 * (esa / 25.0)
        pts = []
        for p in fc["hb"]["points"]:
            h = (_d(p["d"]) - _d(fc["hb"]["points"][0]["d"])).days
            months = min(h / 30.0, 3.0) + max(0.0, (h / 30.0 - 3.0)) * 0.25
            delta = per_month * months
            pts.append({"d": p["d"], "v": round(max(5, min(17, p["v"] + delta)), 2), "lo": round(max(5, min(17, p["lo"] + delta)), 2), "hi": round(max(5, min(17, p["hi"] + delta)), 2)})
        out["adjusted"]["hb"] = pts
        out["notes"].append(f"ESA {esa:+.0f}% → Hb ≈ {per_month:+.1f} g/dL/เดือน (สมมติเหล็กเพียงพอ)")
    if changes.get("binder") and fc.get("phos", {}).get("points"):
        pts = []
        for p in fc["phos"]["points"]:
            h = (_d(p["d"]) - _d(fc["phos"]["points"][0]["d"])).days
            delta = -0.8 * min(1.0, h / 60.0)
            pts.append({"d": p["d"], "v": round(max(1, p["v"] + delta), 2), "lo": round(max(1, p["lo"] + delta), 2), "hi": round(max(1, p["hi"] + delta), 2)})
        out["adjusted"]["phos"] = pts
        out["notes"].append("binder → Phos −0.8 ภายใน 2 เดือน")
    if changes.get("vitd") and fc.get("pth", {}).get("points"):
        pts = []
        for p in fc["pth"]["points"]:
            h = (_d(p["d"]) - _d(fc["pth"]["points"][0]["d"])).days
            factor = 1 - 0.2 * min(1.0, h / 90.0)
            pts.append({"d": p["d"], "v": round(p["v"] * factor, 1), "lo": round(p["lo"] * factor, 1), "hi": round(p["hi"] * factor, 1)})
        out["adjusted"]["pth"] = pts
        out["notes"].append("active vit D → PTH −20% ใน 3 เดือน (เฝ้า Ca/P)")
    if changes.get("diet_k") and fc.get("k", {}).get("points"):
        pts = [{"d": p["d"], "v": round(p["v"] - 0.4, 2), "lo": round(p["lo"] - 0.4, 2), "hi": round(p["hi"] - 0.4, 2)} for p in fc["k"]["points"]]
        out["adjusted"]["k"] = pts
        out["notes"].append("คำปรึกษาอาหาร K → K −0.4")
    uf = _f(changes.get("uf_goal_ml"))
    if uf:
        try:
            from .idh_fingerprint import plan_check
            pc = plan_check(data.get("fingerprint") or {}, uf)
            out["uf_plan"] = pc
            if pc:
                out["notes"].append(pc["text"])
        except Exception:
            pass
    dwd = _f(changes.get("dw_delta"))
    if dwd:
        dw = (data.get("patient") or {}).get("dry_weight")
        idwg = (fc.get("idwg_pct") or {}).get("last") or {}
        if dw and idwg.get("v") is not None:
            new_dw = dw + dwd
            uf_add = -dwd * 1000
            out["notes"].append(f"DW {dw:.1f} → {new_dw:.1f} kg: UF ต่อรอบเปลี่ยน {uf_add:+.0f} mL ที่ IDWG เท่าเดิม ({idwg['v']:.1f}%)")
            out["dw"] = {"new_dw": round(new_dw, 1), "uf_delta_ml": int(uf_add)}
    return out
