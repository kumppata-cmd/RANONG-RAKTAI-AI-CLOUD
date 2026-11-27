"""🧬 IDH Fingerprint — "จุดหัก" ส่วนตัวของผู้ป่วยแต่ละราย เรียนจากรอบฟอกของตัวเอง (ว้าว พิเศษ, 27 พ.ย. 2569)

โปรแกรมไตทั่วไปใช้เกณฑ์เดียวกับทุกคน (UF ≤13 mL/kg/h). ของจริงคือแต่ละคนทนไม่เท่ากัน —
คนหนึ่งความดันตกทุกครั้งที่ดึงเกิน 2.1 L ตอนนาทีที่ 150, อีกคนดึง 3.5 L สบาย ๆ.
โมดูลนี้อ่าน flowsheet ของ 12 รอบล่าสุดของ *ผู้ป่วยรายนี้* แล้วสรุปเป็น "ลายนิ้วมือ":

    uf_break_ml      ค่ากลางของ UF สะสม ณ จุดที่ SBP เริ่มตก (รอบที่เกิด IDH)
    time_break_min   ค่ากลางของนาทีที่เริ่มตก
    safe_uf_ml       UF ที่ทนได้โดยไม่เกิด IDH (percentile 25 ของรอบที่ไม่เกิด) — "ซองปลอดภัย"
    rate_ok / rate_bad  mL/kg/h ค่ากลางของรอบที่ไม่เกิด / เกิด
    pattern          early (ตกก่อนครึ่งรอบ) · late (ตกช่วงท้าย) · none

ใช้ที่: (1) การ์ด IDH บนหน้า session (2) Kidney Twin เพิ่มปัจจัย "ใกล้จุดหักส่วนตัว" (3) what-if เทียบเป้า UF กับซองปลอดภัย
ทุกตัวเลขมาจากข้อมูลผู้ป่วยเอง อธิบายได้ ไม่มีโมเดลดำ · ต้องมี ≥3 รอบที่มีข้อมูล BP+UF จึงจะสรุป
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MIN_SESSIONS = 3
DROP_SBP_ABS = 100     # SBP < 100 ถือว่าเริ่มตก
DROP_SBP_REL = 20      # หรือตก ≥20 จากค่าแรกของรอบ
IDH_SBP = 90           # นิยามเกิด IDH (ตรงกับ idh_twin / uf-coach)
IDH_DROP = 30


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def analyze_session(rows: list[dict], dw: float | None, hours: float | None) -> dict | None:
    """rows เรียงเวลา: [{min, sbp, total_uf}] (min = นาทีตั้งแต่เริ่มรอบ, total_uf mL สะสม)
    → {idh, break_min, break_uf, max_uf, first_sbp, min_sbp, rate_kg_h}"""
    rows = [r for r in rows if r.get("sbp") is not None]
    if len(rows) < 2:
        return None
    first = rows[0]["sbp"]
    min_sbp = min(r["sbp"] for r in rows)
    idh = min_sbp < IDH_SBP or (first - min_sbp) >= IDH_DROP
    brk = None
    for r in rows[1:]:
        if r["sbp"] < DROP_SBP_ABS or (first - r["sbp"]) >= DROP_SBP_REL:
            brk = r
            break
    ufs = [r["total_uf"] for r in rows if r.get("total_uf") is not None]
    max_uf = max(ufs) if ufs else None
    out = {"idh": idh, "first_sbp": first, "min_sbp": min_sbp, "max_uf": max_uf,
           "break_min": brk["min"] if brk else None,
           "break_uf": (brk.get("total_uf") if brk else None)}
    if brk and brk.get("total_uf") is None and ufs:
        # ไม่มี UF ที่แถวนั้น → ประมาณจากแถวก่อนหน้าที่มี
        prev = [r["total_uf"] for r in rows if r.get("total_uf") is not None and r["min"] <= brk["min"]]
        out["break_uf"] = prev[-1] if prev else None
    if max_uf and dw and hours:
        out["rate_kg_h"] = max_uf / dw / hours
    return out


def fingerprint(sessions: list[dict]) -> dict:
    """sessions: [{date, dw, hours, rows}] → ลายนิ้วมือ (หรือ {"enough": False})"""
    an = []
    for s in sessions:
        a = analyze_session(s.get("rows") or [], _f(s.get("dw")), _f(s.get("hours")))
        if a:
            a["date"] = s.get("date")
            an.append(a)
    n = len(an)
    if n < MIN_SESSIONS:
        return {"enough": False, "n_sessions": n, "min_required": MIN_SESSIONS}
    idh = [a for a in an if a["idh"]]
    ok = [a for a in an if not a["idh"]]
    out = {"enough": True, "n_sessions": n, "n_idh": len(idh), "idh_rate_pct": round(len(idh) / n * 100)}
    b_uf = [a["break_uf"] for a in idh if a.get("break_uf")]
    b_t = [a["break_min"] for a in idh if a.get("break_min") is not None]
    if b_uf:
        out["uf_break_ml"] = int(statistics.median(b_uf))
    if b_t:
        out["time_break_min"] = int(statistics.median(b_t))
    ok_uf = sorted(a["max_uf"] for a in ok if a.get("max_uf"))
    if ok_uf:
        k = max(0, int(len(ok_uf) * 0.25) - (1 if len(ok_uf) > 1 else 0))
        out["safe_uf_ml"] = int(ok_uf[k])          # percentile ~25 ของรอบที่ไม่เกิด
        out["max_uf_tolerated_ml"] = int(ok_uf[-1])
    r_ok = [a["rate_kg_h"] for a in ok if a.get("rate_kg_h")]
    r_bad = [a["rate_kg_h"] for a in idh if a.get("rate_kg_h")]
    if r_ok:
        out["rate_ok_kg_h"] = round(statistics.median(r_ok), 1)
    if r_bad:
        out["rate_bad_kg_h"] = round(statistics.median(r_bad), 1)
    if idh and b_t:
        hrs = [s for s in sessions if s.get("hours")]
        total_min = (_f(hrs[0]["hours"]) if hrs else 4.0) * 60.0
        out["pattern"] = "early" if out["time_break_min"] < total_min * 0.5 else "late"
    else:
        out["pattern"] = "none"
    out["dates"] = [str(a["date"]) for a in an if a.get("date")][:12]
    return out


def summary_text(fp: dict) -> str:
    if not fp.get("enough"):
        return f"ยังไม่พอสรุป (มีข้อมูล {fp.get('n_sessions', 0)} รอบ ต้อง ≥{fp.get('min_required', MIN_SESSIONS)})"
    bits = [f"{fp['n_sessions']} รอบล่าสุด เกิด IDH {fp['n_idh']} ครั้ง ({fp['idh_rate_pct']}%)"]
    if fp.get("uf_break_ml"):
        bits.append(f"จุดหักส่วนตัว ≈ UF {fp['uf_break_ml']:,} mL" + (f" ที่นาทีที่ {fp['time_break_min']}" if fp.get("time_break_min") else ""))
    if fp.get("safe_uf_ml"):
        bits.append(f"ซองปลอดภัย ≤ {fp['safe_uf_ml']:,} mL (เคยทนสูงสุด {fp.get('max_uf_tolerated_ml', 0):,})")
    if fp.get("rate_ok_kg_h") and fp.get("rate_bad_kg_h"):
        bits.append(f"อัตราที่ทนได้ ~{fp['rate_ok_kg_h']} vs ที่ตก ~{fp['rate_bad_kg_h']} mL/kg/h")
    if fp.get("pattern") == "early":
        bits.append("แบบแผน: ตกเร็วตั้งแต่ครึ่งแรก — ระวัง UF profile ช่วงต้น")
    elif fp.get("pattern") == "late":
        bits.append("แบบแผน: ตกช่วงท้ายรอบ — ลด UF ใน 60 นาทีสุดท้าย/ยืดเวลา")
    return " · ".join(bits)


def plan_check(fp: dict, uf_goal: float | None) -> dict | None:
    """เทียบเป้า UF ของรอบนี้กับซองปลอดภัยส่วนตัว → ข้อความแนะนำ (None ถ้าสรุปไม่ได้)"""
    if not fp.get("enough") or not uf_goal:
        return None
    safe, brk = fp.get("safe_uf_ml"), fp.get("uf_break_ml")
    # "สูง" = เกินทั้งจุดที่เคยเริ่มตก และเกินซองที่เคยทนได้ · "ปานกลาง" = เกินอย่างใดอย่างหนึ่ง
    if brk and uf_goal > brk and (not safe or uf_goal > safe):
        ref = max(brk, safe or 0)
        return {"level": "high", "text": f"เป้า UF {uf_goal:,.0f} mL เกินจุดหักส่วนตัว ({brk:,} mL) และซองปลอดภัย ({safe:,} mL) — พิจารณาลดเป้า/ยืดเวลา/UF profile" if safe
                else f"เป้า UF {uf_goal:,.0f} mL เกินจุดหักส่วนตัว ({brk:,} mL) — พิจารณาลดเป้า/ยืดเวลา/UF profile",
                "over_ml": int(uf_goal - ref)}
    if brk and uf_goal > brk and safe:
        return {"level": "moderate", "text": f"เป้า UF {uf_goal:,.0f} mL เกินจุดที่เคยเริ่มตก ({brk:,} mL) แต่ยังในซองที่เคยทนได้ (≤ {safe:,} mL) — เฝ้า BP ถี่ขึ้นหลัง UF {brk:,} mL",
                "over_ml": int(uf_goal - brk)}
    if safe and uf_goal > safe:
        return {"level": "moderate", "text": f"เป้า UF {uf_goal:,.0f} mL สูงกว่าซองปลอดภัยส่วนตัว ({safe:,} mL) — เคยทนได้ถึง {fp.get('max_uf_tolerated_ml', 0):,} แต่ไม่ทุกครั้ง",
                "over_ml": int(uf_goal - safe)}
    if safe:
        return {"level": "low", "text": f"เป้า UF {uf_goal:,.0f} mL อยู่ในซองปลอดภัยส่วนตัว (≤ {safe:,} mL)", "over_ml": 0}
    return None


def live_factor(fp: dict, uf_removed_now: float | None, elapsed_min: float | None) -> list[dict]:
    """ปัจจัยเพิ่มให้ Kidney Twin: ใกล้จุดหักส่วนตัว"""
    out = []
    if not fp.get("enough") or fp.get("n_idh", 0) < 2:
        return out
    brk, t = fp.get("uf_break_ml"), fp.get("time_break_min")
    if brk and uf_removed_now and uf_removed_now >= 0.9 * brk:
        out.append({"text": f"ใกล้จุดหักส่วนตัว: UF {uf_removed_now:,.0f} / จุดหัก {brk:,} mL (เคยตก {fp['n_idh']} ใน {fp['n_sessions']} รอบ)", "points": 15})
    if t and elapsed_min and elapsed_min >= 0.9 * t and not out:
        out.append({"text": f"ถึงช่วงเวลาที่เคยตก (นาทีที่ ~{t}) — รอบนี้ {elapsed_min:.0f} นาที", "points": 8})
    return out


def fingerprint_for(cur, patient_id: int, limit: int = 12, exclude_session: int | None = None) -> dict:
    """อ่าน flowsheet ของรอบที่จบแล้วของผู้ป่วย → fingerprint()"""
    cur.execute("""SELECT s.id, s.session_date, s.started_at, p.dry_weight,
                          COALESCE((SELECT r.duration_hours FROM hd_prescriptions r WHERE r.patient_id = s.patient_id
                                    AND r.is_active = TRUE ORDER BY r.id DESC LIMIT 1), 4.0)
                   FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                   WHERE s.patient_id = %s AND s.status = 'completed' AND (%s IS NULL OR s.id <> %s)
                   ORDER BY s.session_date DESC, s.id DESC LIMIT %s""", (patient_id, exclude_session, exclude_session, limit))
    sess = cur.fetchall()
    sessions = []
    for sid, d, started, dw, hours in sess:
        cur.execute("""SELECT recorded_at, sbp, total_uf FROM hd_flowsheet WHERE session_id = %s AND sbp IS NOT NULL
                       ORDER BY COALESCE(recorded_at, NOW()) ASC, id ASC""", (sid,))
        rows = cur.fetchall()
        if len(rows) < 2:
            # ถอยไป vital_signs + uf_records (ไม่มี UF สะสมต่อแถว → ใช้ UF record ใกล้เวลาที่สุด)
            cur.execute("SELECT recorded_at, sbp FROM hd_vital_signs WHERE session_id = %s AND sbp IS NOT NULL ORDER BY recorded_at", (sid,))
            vs = cur.fetchall()
            cur.execute("SELECT recorded_at, uf_removed FROM hd_uf_records WHERE session_id = %s AND uf_removed IS NOT NULL ORDER BY recorded_at", (sid,))
            ufr = cur.fetchall()
            rows = []
            for t, sbp in vs:
                uf = None
                for tu, u in ufr:
                    if tu and t and tu <= t:
                        uf = u
                rows.append((t, sbp, uf))
            if len(rows) < 2:
                continue
        t0 = started or rows[0][0]
        rr = []
        for t, sbp, uf in rows:
            try:
                mins = max(0.0, (t - t0).total_seconds() / 60.0) if (t and t0) else 0.0
            except TypeError:
                mins = 0.0
            rr.append({"min": mins, "sbp": int(sbp), "total_uf": _f(uf)})
        sessions.append({"date": d, "dw": _f(dw), "hours": _f(hours), "rows": rr})
    return fingerprint(sessions)
