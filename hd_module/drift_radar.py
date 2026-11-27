"""🔧 Machine Drift Radar — จอเครื่อง/flowsheet ทุกใบกลายเป็น telemetry ต่อเครื่อง (ว้าว D, 27 พ.ย. 2569)

ไม่เพิ่มงานพยาบาล: ใช้ค่า TMP / VP / AP / conductivity / dialysate temp ที่บันทึกใน hd_flowsheet
(พิมพ์เอง หรือจาก Magic Camera) แล้วจัดกลุ่มตามเครื่อง (ทะเบียน hd_equipment dialysis_machine ตามเตียง —
ถ้าไม่มีทะเบียน ใช้ "เตียง N") → หา drift ที่ "ค่อย ๆ แย่ลง" ก่อนเครื่องเสีย:

    TMP   : ค่ากลางต่อรอบ ขึ้นเรื่อย ๆ (slope > +15 mmHg/สัปดาห์, ≥6 รอบ) หรือ 3 รอบล่าสุด > 1.5× baseline
    VP    : ค่ากลางต่อรอบ ขึ้นเรื่อย ๆ (slope > +10/สัปดาห์)  — ผสมปัจจัยผู้ป่วย/เข็ม จึงเป็นระดับ "เฝ้าระวัง"
    AP    : ติดลบมากขึ้นเรื่อย ๆ (slope < −10/สัปดาห์)
    Cond  : SD ใน 7 วัน > 0.3 mS/cm หรือมีค่านอก 13.5–14.5 → ระบบผสมน้ำยา/RO
    Temp  : dialysate temp เบี่ยงจาก 36–37 เกิน 0.5 ซ้ำ ≥3 ครั้ง
    Water : ผล hd_water_quality ล่าสุดต่อชนิด + fail / ค้างนานเกินกำหนด

ทุกตัวเลขเป็น "สัญญาณให้ช่างดู" ไม่ใช่การวินิจฉัย · อ่านอย่างเดียว ไม่มีสวิตช์ (ไม่ส่ง LINE เอง)
"""
from __future__ import annotations

import logging
import statistics
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

TMP_SLOPE_WEEK = 15.0
VP_SLOPE_WEEK = 10.0
AP_SLOPE_WEEK = -10.0
COND_SD = 0.3
COND_RANGE = (13.5, 14.5)
TEMP_RANGE = (36.0, 37.0)
MIN_SESSIONS = 6
WATER_DUE_DAYS = {"bacteria": 35, "endotoxin": 35, "chemical": 400, "conductivity": 1}


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def slope_per_week(points: list[tuple[date, float]]) -> float | None:
    """least squares slope (หน่วย/สัปดาห์) · None ถ้าจุด < 3 หรือช่วงเวลา < 7 วัน"""
    pts = [(d, v) for d, v in points if d and v is not None]
    if len(pts) < 3:
        return None
    d0 = pts[0][0]
    xs = [(d - d0).days / 7.0 for d, _ in pts]
    ys = [v for _, v in pts]
    if (pts[-1][0] - d0).days < 7:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def analyze_machine(sessions: list[dict], rows_recent: list[dict]) -> dict:
    """sessions: [{date, tmp, vp, ap}] ค่ากลางต่อรอบ เรียงวันที่ · rows_recent: แถว 7 วันล่าสุด [{cond, dtemp}]
    → {"flags": [...], "level": ok|watch|alert, "series": {...}, "n_sessions"}"""
    flags = []
    n = len(sessions)
    series = {k: [(s["date"], s.get(k)) for s in sessions if s.get(k) is not None] for k in ("tmp", "vp", "ap")}

    def _trend(key, label, thr, direction):
        pts = series[key]
        if len(pts) < MIN_SESSIONS:
            return
        sl = slope_per_week(pts)
        if sl is None:
            return
        base = statistics.median(v for _, v in pts[:5])
        last3 = statistics.median(v for _, v in pts[-3:])
        if direction > 0 and (sl > thr or (base > 0 and last3 > 1.5 * base and last3 - base > 30)):
            flags.append({"metric": key, "level": "alert" if key == "tmp" else "watch",
                          "text": f"{label} ขึ้นเรื่อย ๆ {sl:+.0f}/สัปดาห์ (baseline {base:.0f} → ล่าสุด {last3:.0f})"})
        elif direction < 0 and sl < thr:
            flags.append({"metric": key, "level": "watch", "text": f"{label} ติดลบมากขึ้น {sl:+.0f}/สัปดาห์ (baseline {base:.0f} → ล่าสุด {last3:.0f})"})

    _trend("tmp", "TMP", TMP_SLOPE_WEEK, +1)
    _trend("vp", "VP", VP_SLOPE_WEEK, +1)
    _trend("ap", "AP", AP_SLOPE_WEEK, -1)

    conds = [r["cond"] for r in rows_recent if r.get("cond") is not None]
    if len(conds) >= 4:
        sd = statistics.pstdev(conds)
        out = [c for c in conds if not (COND_RANGE[0] <= c <= COND_RANGE[1])]
        if sd > COND_SD or out:
            flags.append({"metric": "cond", "level": "alert" if out else "watch",
                          "text": f"Conductivity แกว่ง SD {sd:.2f} mS/cm" + (f" · นอกช่วง {len(out)} ครั้ง (เช่น {out[0]:g})" if out else "")})
    temps = [r["dtemp"] for r in rows_recent if r.get("dtemp") is not None]
    bad_t = [t for t in temps if t < TEMP_RANGE[0] - 0.5 or t > TEMP_RANGE[1] + 0.5]
    if len(bad_t) >= 3:
        flags.append({"metric": "dtemp", "level": "watch", "text": f"Dialysate temp เบี่ยง {len(bad_t)} ครั้งใน 7 วัน (เช่น {bad_t[0]:g}°C)"})

    level = "ok"
    if any(f["level"] == "alert" for f in flags):
        level = "alert"
    elif flags:
        level = "watch"
    return {"flags": flags, "level": level, "n_sessions": n,
            "series": {k: [(d.isoformat(), round(v, 1)) for d, v in v_ if d] for k, v_ in series.items()},
            "enough_data": n >= MIN_SESSIONS}


def collect(conn, days: int = 28) -> dict:
    """→ {"machines": [...], "water": [...], "days", "generated_at"}"""
    since = (datetime.now(timezone.utc) + timedelta(hours=7)).date() - timedelta(days=days)
    with conn.cursor() as cur:
        cur.execute("""SELECT id, brand, model, serial_number, bed_number, status, next_pm_date
                       FROM hd_equipment WHERE equipment_type = 'dialysis_machine' AND status IN ('active','repair')
                       ORDER BY bed_number NULLS LAST, id""")
        eq = {}
        for r in cur.fetchall():
            if r[4] is not None:
                eq.setdefault(int(r[4]), {"id": r[0], "name": " ".join(x for x in (r[1], r[2]) if x) or f"เครื่อง #{r[0]}",
                                          "serial": r[3], "bed": int(r[4]), "status": r[5],
                                          "next_pm": r[6].isoformat() if r[6] else None})
        # ค่ากลางต่อรอบ ต่อเตียง
        cur.execute("""SELECT s.bed_number, s.session_date,
                              percentile_cont(0.5) WITHIN GROUP (ORDER BY f.tmp) FILTER (WHERE f.tmp IS NOT NULL),
                              percentile_cont(0.5) WITHIN GROUP (ORDER BY f.vp)  FILTER (WHERE f.vp IS NOT NULL),
                              percentile_cont(0.5) WITHIN GROUP (ORDER BY f.ap)  FILTER (WHERE f.ap IS NOT NULL)
                       FROM hd_flowsheet f JOIN hd_sessions s ON s.id = f.session_id
                       WHERE s.session_date >= %s AND s.bed_number IS NOT NULL
                       GROUP BY s.bed_number, s.session_date, s.id
                       ORDER BY s.bed_number, s.session_date, s.id""", (since,))
        per_bed = {}
        for bed, d, tmp, vp, ap in cur.fetchall():
            per_bed.setdefault(int(bed), []).append({"date": d, "tmp": _f(tmp), "vp": _f(vp), "ap": _f(ap)})
        cur.execute("""SELECT s.bed_number, f.conductivity, f.dialysate_temp
                       FROM hd_flowsheet f JOIN hd_sessions s ON s.id = f.session_id
                       WHERE s.session_date >= %s AND s.bed_number IS NOT NULL
                         AND (f.conductivity IS NOT NULL OR f.dialysate_temp IS NOT NULL)""",
                    ((datetime.now(timezone.utc) + timedelta(hours=7)).date() - timedelta(days=7),))
        recent = {}
        for bed, c, t in cur.fetchall():
            recent.setdefault(int(bed), []).append({"cond": _f(c), "dtemp": _f(t)})
        water = []
        try:
            cur.execute("""SELECT DISTINCT ON (test_type) test_type, test_date, result_value, unit, pass_fail
                           FROM hd_water_quality ORDER BY test_type, test_date DESC""")
            today = (datetime.now(timezone.utc) + timedelta(hours=7)).date()
            for tt, d, val, unit, pf in cur.fetchall():
                due = WATER_DUE_DAYS.get(tt, 90)
                age = (today - d).days if d else None
                water.append({"type": tt, "date": d.isoformat() if d else None, "value": val, "unit": unit, "pass_fail": pf,
                              "age_days": age, "overdue": bool(age is not None and age > due),
                              "level": "alert" if (pf or "").lower() == "fail" else ("watch" if (age is not None and age > due) else "ok")})
        except Exception as e:  # noqa: BLE001
            logger.debug("drift water: %s", e)
            conn.rollback()
    beds = sorted(set(per_bed) | set(eq))
    machines = []
    for bed in beds:
        info = eq.get(bed, {"id": None, "name": f"เตียง {bed} (ไม่มีทะเบียนเครื่อง)", "serial": None, "bed": bed, "status": None, "next_pm": None})
        a = analyze_machine(per_bed.get(bed, []), recent.get(bed, []))
        machines.append({**info, **a})
    order = {"alert": 0, "watch": 1, "ok": 2}
    machines.sort(key=lambda m: (order.get(m["level"], 3), -(m["n_sessions"] or 0), m["bed"]))
    return {"machines": machines, "water": water, "days": days,
            "generated_at": (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M"),
            "summary": {lv: sum(1 for m in machines if m["level"] == lv) for lv in ("alert", "watch", "ok")}}
