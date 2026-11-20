"""UF coach — personal UF tolerance, IDWG limits, home-weight coaching, machine-screen reading.

17 พ.ย. 2569 (หมออนุมัติข้อ 1, 8, 9 + ข้อ 10 เสริม):
    1  personal_tolerance()   ผู้ป่วยแต่ละคนเริ่มความดันตกที่ UF rate เท่าไร (จากรอบที่ผ่านมา)
    8  parse_machine_words()  อ่านค่าจากรูปหน้าจอเครื่องฟอก (Nikkiso DBB-07/27/27C, Fresenius 4008S/5008)
    9  weight_coach()         ผู้ป่วยส่ง "น้ำหนัก 62.4" ทาง LINE → ตอบโควตาน้ำหนักที่เหลือถึงรอบถัดไป
    10 idwg()                 น้ำหนักขึ้นระหว่างรอบเทียบเกณฑ์ (ค่าเริ่มต้น 4% ของ DW ปรับรายคนได้)

ทุกอย่างเป็น "ข้อมูลประกอบ/ข้อเสนอ" — ไม่ปรับเครื่อง ไม่เปลี่ยนใบสั่งแพทย์
"""
import re
from datetime import datetime, timedelta, timezone

DEFAULT_IDWG_PCT = 4.0
MIN_SESSIONS = 10          # ต่ำกว่านี้ยังไม่สรุปเพดานเฉพาะราย
STANDARD_CEILING = 13.0


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


# ═══════════════════════ 10 — IDWG ═══════════════════════

def limit_pct(patient_pct=None, setting_pct=None):
    """เกณฑ์น้ำหนักขึ้นระหว่างรอบ (% ของ DW): รายคน > ค่าใน ⚙️ > 4%"""
    for v in (patient_pct, setting_pct):
        try:
            f = float(v)
            if 1.0 <= f <= 10.0:
                return round(f, 1)
        except (TypeError, ValueError):
            continue
    return DEFAULT_IDWG_PCT


def idwg(weight_now, prev_post_weight, dry_weight, pct):
    """→ dict(idwg_kg, idwg_pct, limit_kg, over_kg, over) — None ถ้าข้อมูลไม่พอ"""
    try:
        w, pp, dw = float(weight_now), float(prev_post_weight), float(dry_weight)
    except (TypeError, ValueError):
        return None
    if dw <= 0:
        return None
    gain = round(w - pp, 1)
    lim = round(dw * pct / 100.0, 1)
    return {"idwg_kg": gain, "idwg_pct": round(gain / dw * 100, 1), "limit_kg": lim, "limit_pct": pct,
            "over_kg": round(gain - lim, 1), "over": gain > lim}


# ═══════════════════════ 9 — Home-weight coach ═══════════════════════

def _norm_thai(s):
    """Normalize Thai ◌ํ◌า → ◌ำ (some LINE keyboards send decomposed form)."""
    return (s or "").replace("ํา", "ำ")

_WEIGHT_RE = re.compile(r"^\s*(?:น้ำหนัก|นน\.?|ชั่งน้ำหนัก|weight|wt\.?)\s*[:=]?\s*(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:กก\.?|กิโล(?:กรัม)?|kg\.?)?\s*$",
                        re.IGNORECASE)

DEFAULT_WEIGHT_TEMPLATE = (
    "⚖️ บันทึกน้ำหนัก {weight} กก. แล้ว ({date})\n"
    "━━━━━━━━━━━━\n"
    "หลังฟอกครั้งล่าสุด {prev_post} กก. → ขึ้นมา {gain} กก.\n"
    "เกณฑ์ไม่ควรเกิน {limit_kg} กก. ({limit_pct}% ของน้ำหนักแห้ง {dw} กก.)\n"
    "{status}\n"
    "{next_line}"
)


def parse_weight_message(text):
    """'น้ำหนัก 62.4' / 'นน 62,4 กก' → 62.4 (30–250 kg) หรือ None

    Handles both composed ◌ำ and decomposed ◌ํ◌า Thai encodings.
    """
    normed = _norm_thai(text)
    m = _WEIGHT_RE.match(normed or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v if 30 <= v <= 250 else None


class _Safe(dict):
    def __missing__(self, k):
        return ""


def render(template, ctx):
    try:
        return (template or "").format_map(_Safe(ctx)).strip()
    except (ValueError, IndexError):
        return DEFAULT_WEIGHT_TEMPLATE.format_map(_Safe(ctx)).strip()


def weight_reply_ctx(weight, prev_post, dw, pct, next_day=None, today=None):
    """ข้อความตอบผู้ป่วย (ภาษาง่าย ไม่วินิจฉัย ไม่สั่งยา)"""
    from .patient_summary import DAY_TH, _d
    today = today or _thai_today()
    r = idwg(weight, prev_post, dw, pct)
    ctx = {"weight": f"{weight:.1f}", "date": _d(today), "prev_post": f"{float(prev_post):.1f}" if prev_post else "-",
           "dw": f"{float(dw):.1f}" if dw else "-", "limit_pct": f"{pct:g}"}
    if not r:
        ctx.update(gain="-", limit_kg="-", status="ℹ️ ยังไม่มีข้อมูลน้ำหนักหลังฟอก/น้ำหนักแห้งในระบบ — เจ้าหน้าที่จะดูให้ในรอบฟอกถัดไป")
    else:
        ctx.update(gain=f"{r['idwg_kg']:+.1f}", limit_kg=f"{r['limit_kg']:.1f}")
        left = round(r["limit_kg"] - r["idwg_kg"], 1)
        if r["over"]:
            ctx["status"] = (f"🟠 ขึ้นเกินเกณฑ์แล้ว {r['over_kg']:.1f} กก. — งดน้ำ/อาหารเค็มเพิ่ม และสังเกตอาการบวม เหนื่อย นอนราบไม่ได้ "
                             "ถ้ามีอาการให้ติดต่อคลินิกทันที")
        elif left <= 0.5:
            ctx["status"] = f"🟡 เหลือโควตาอีกเพียง {left:.1f} กก. ควรจำกัดน้ำเข้มงวดจนถึงวันฟอก"
        else:
            ctx["status"] = f"🟢 ยังอยู่ในเกณฑ์ เหลือโควตาอีก {left:.1f} กก. จนถึงวันฟอกครั้งถัดไป"
        ctx.update(idwg=r)
    ctx["next_line"] = (f"📅 ฟอกครั้งถัดไป วัน{DAY_TH[next_day.weekday()]}ที่ {_d(next_day)}" if next_day else "")
    return ctx


HOME_WEIGHT_SQL = """
CREATE TABLE IF NOT EXISTS hd_home_weights (
    id SERIAL PRIMARY KEY,
    hn VARCHAR(20) NOT NULL,
    weight NUMERIC(5,1) NOT NULL,
    idwg_kg NUMERIC(4,1),
    over_limit BOOLEAN,
    via VARCHAR(20) DEFAULT 'line',
    recorded_at TIMESTAMP DEFAULT NOW()
)"""


def patient_context(cur, hn):
    """→ dict(id, name, dw, schedule_days, pct, prev_post, prev_date) หรือ None"""
    cur.execute("""SELECT id, patient_name, dry_weight, schedule_days, idwg_limit_pct
                   FROM hd_patient_mapping WHERE UPPER(hn) = %s""", (str(hn).upper(),))
    p = cur.fetchone()
    if not p:
        return None
    cur.execute("""SELECT post_weight, session_date FROM hd_sessions WHERE patient_id = %s AND status = 'completed'
                   AND post_weight IS NOT NULL ORDER BY session_date DESC, id DESC LIMIT 1""", (p[0],))
    s = cur.fetchone()
    return {"id": p[0], "name": p[1], "dw": float(p[2]) if p[2] else None, "schedule_days": p[3],
            "patient_pct": float(p[4]) if p[4] is not None else None,
            "prev_post": float(s[0]) if s and s[0] else None, "prev_date": s[1] if s else None}


def weight_coach(cur, hn, weight, setting_pct=None, template=None, today=None):
    """บันทึกน้ำหนักจากบ้าน + สร้างข้อความตอบ → (reply_text, ctx) ; ctx=None ถ้าไม่พบผู้ป่วย"""
    from .patient_summary import next_schedule_day
    pc = patient_context(cur, hn)
    if not pc:
        return None, None
    pct = limit_pct(pc["patient_pct"], setting_pct)
    nxt = next_schedule_day(pc["schedule_days"], today or _thai_today())
    ctx = weight_reply_ctx(weight, pc["prev_post"], pc["dw"], pct, nxt, today)
    r = ctx.get("idwg")
    cur.execute(HOME_WEIGHT_SQL)
    cur.execute("INSERT INTO hd_home_weights (hn, weight, idwg_kg, over_limit) VALUES (%s, %s, %s, %s)",
                (str(hn).upper(), weight, r["idwg_kg"] if r else None, bool(r and r["over"])))
    ctx["name"] = pc["name"] or ""
    return render(template or DEFAULT_WEIGHT_TEMPLATE, ctx), ctx


def recent_home_weights(cur, hn, limit=14):
    cur.execute(HOME_WEIGHT_SQL)
    cur.execute("""SELECT weight, idwg_kg, over_limit, recorded_at FROM hd_home_weights WHERE hn = %s
                   ORDER BY recorded_at DESC LIMIT %s""", (str(hn).upper(), limit))
    return [{"weight": float(r[0]), "idwg_kg": float(r[1]) if r[1] is not None else None, "over": bool(r[2]),
             "at": r[3].isoformat() + "Z" if r[3] else None} for r in cur.fetchall()]


# ═══════════════════════ 1 — Personal UF tolerance ═══════════════════════

def tolerance_from_sessions(sessions, ceiling=STANDARD_CEILING):
    """sessions: [{rate_kg, idh, date}] (rate_kg = UF เฉลี่ยทั้งรอบ mL/kg/ชม.)

    → dict(status, n, n_idh, personal_ceiling, bands, text)
    status: insufficient (<10 รอบ) / no_pattern (IDH < 2 รอบ) / personal
    เพดานเฉพาะราย = อัตราต่ำสุดที่เคยเกิด IDH ซ้ำ (ค่ากลางล่างของรอบที่มี IDH) ไม่เกิน 13"""
    ss = [s for s in sessions if s.get("rate_kg") is not None and s["rate_kg"] > 0]
    n = len(ss)
    idh = sorted(s["rate_kg"] for s in ss if s.get("idh"))
    out = {"n": n, "n_idh": len(idh), "personal_ceiling": None, "standard_ceiling": ceiling, "bands": []}
    # แบ่งช่วงอัตรา เพื่อให้แพทย์เห็นภาพ
    for lo, hi in ((0, 8), (8, 10), (10, 13), (13, 99)):
        b = [s for s in ss if lo <= s["rate_kg"] < hi]
        if b:
            k = sum(1 for s in b if s.get("idh"))
            out["bands"].append({"range": f"{lo}–{hi if hi < 99 else ''}".rstrip("–") if hi < 99 else f"≥{lo}",
                                 "n": len(b), "idh": k, "pct": round(k * 100 / len(b))})
    if n < MIN_SESSIONS:
        out.update(status="insufficient", text=f"ข้อมูลยังไม่พอ ({n}/{MIN_SESSIONS} รอบ) — ใช้เพดานมาตรฐาน {ceiling:g} mL/kg/ชม.")
        return out
    if len(idh) < 2:
        out.update(status="no_pattern",
                   text=f"{n} รอบล่าสุด ไม่พบความดันตกซ้ำ ({len(idh)} รอบ) — ใช้เพดานมาตรฐาน {ceiling:g} mL/kg/ชม.")
        return out
    q = idh[max(0, (len(idh) - 1) // 4)]              # ควอร์ไทล์ล่างของอัตราในรอบที่มี IDH
    pc = round(min(ceiling, max(5.0, q - 0.5)) * 2) / 2
    above = [s for s in ss if s["rate_kg"] >= pc]
    below = [s for s in ss if s["rate_kg"] < pc]
    ia = sum(1 for s in above if s.get("idh"))
    ib = sum(1 for s in below if s.get("idh"))
    out.update(status="personal", personal_ceiling=pc,
               text=(f"ทนได้ราว {pc:g} mL/kg/ชม. — รอบที่ ≥{pc:g}: ความดันตก {ia}/{len(above)} รอบ · "
                     f"<{pc:g}: {ib}/{len(below)} รอบ (จาก {n} รอบล่าสุด)"))
    return out


def sessions_for_tolerance(cur, hn, limit=20):
    """อ่านรอบฟอกที่จบแล้ว → [{rate_kg, idh, date}] (read-only)

    rate_kg = actual_uf ÷ ชั่วโมงฟอกจริง ÷ DW
    idh     = SBP ระหว่างฟอก < 90 หรือลดลง ≥ 20 จาก SBP แรกของรอบ"""
    cur.execute("""SELECT s.id, s.session_date, s.actual_uf, s.started_at, s.ended_at, p.dry_weight
                   FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                   WHERE UPPER(p.hn) = %s AND s.status = 'completed' AND s.actual_uf > 0
                   ORDER BY s.session_date DESC, s.id DESC LIMIT %s""", (str(hn).upper(), limit))
    rows = cur.fetchall()
    out = []
    for sid, d, uf, st, en, dw in rows:
        hrs = None
        if st and en:
            hrs = (en - st).total_seconds() / 3600.0
        if not hrs or hrs < 1 or hrs > 8 or not dw:
            continue
        cur.execute("""SELECT sbp FROM hd_vital_signs WHERE session_id = %s AND sbp IS NOT NULL
                       ORDER BY recorded_at ASC""", (sid,))
        sbps = [r[0] for r in cur.fetchall()]
        idh_flag = False
        if sbps:
            pre = sbps[0]
            idh_flag = any(v < 90 or (pre - v) >= 20 for v in sbps[1:])
        out.append({"rate_kg": round(float(uf) / hrs / float(dw), 1), "idh": idh_flag,
                    "date": d.isoformat() if d else None})
    return out


# ═══════════════════════ 8 — Machine screen ═══════════════════════

_LABELS = {
    "uf_volume": ("uf volume", "ufvolume", "uf vol"),
    "uf_goal": ("uf goal", "ufgoal"),
    "uf_rate": ("uf rate", "ufrate"),
    "time_left": ("treat.time remaining", "treat. time remaining", "treat time remaining", "treat.time",
                  "remain time", "uf time left", "remain"),
    "bp": ("bld. press", "bld press", "bld.press", "blood press"),
    "blood_flow": ("blood flow", "b. flw", "b.flw", "b flw", "eff. blood flow", "bp rate", "bp1"),
    "sys": ("sys",), "dia": ("dia",), "pulse": ("pulse",),
}
_NUM = re.compile(r"^-?\d{1,4}(?:[.,]\d{1,2})?$")
_TIME = re.compile(r"^\d{1,2}:\d{2}$")
_BP = re.compile(r"^(\d{2,3})\s*/\s*(\d{2,3})$")


def _norm(t):
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def _merge_lines(words):
    """words: [{t, x, y, w, h}] → phrases ที่อยู่บรรทัดเดียวกันติดกัน (สำหรับจับป้าย 2–3 คำ)"""
    ws = sorted(words, key=lambda w: (round(w["y"] / max(8, w["h"])), w["x"]))
    phrases = []
    for i, w in enumerate(ws):
        cur = dict(w)
        phrases.append(dict(cur))
        for j in range(i + 1, min(i + 4, len(ws))):
            n = ws[j]
            same_line = abs((n["y"] + n["h"] / 2) - (w["y"] + w["h"] / 2)) < max(w["h"], n["h"]) * 0.6
            gap = n["x"] - (cur["x"] + cur["w"])
            if not same_line or gap > max(w["h"], n["h"]) * 2.2 or gap < -w["h"]:
                break
            cur = {"t": cur["t"] + " " + n["t"], "x": cur["x"], "y": min(cur["y"], n["y"]),
                   "w": n["x"] + n["w"] - cur["x"], "h": max(cur["h"], n["h"])}
            phrases.append(dict(cur))
    return phrases


def _join_values(words):
    """รวมตัวเลขที่ OCR แยกเป็นหลายคำ: '3' ',' '91' / '133' '/' '69' / '0' ':' '06'"""
    ws = sorted(words, key=lambda w: (round(w["y"] / max(8, w["h"])), w["x"]))
    out, i = [], 0
    while i < len(ws):
        cur = dict(ws[i])
        j = i + 1
        while j < len(ws):
            n = ws[j]
            same = abs((n["y"] + n["h"] / 2) - (cur["y"] + cur["h"] / 2)) < max(cur["h"], n["h"]) * 0.5
            gap = n["x"] - (cur["x"] + cur["w"])
            frag = (cur["t"][-1:] in ".,:/" or n["t"][:1] in ".,:/")   # ต่อเฉพาะเศษตัวเลข เช่น '3' ',' '91'
            if same and frag and gap < cur["h"] * 0.9 and re.fullmatch(r"[\d.,:/]+", n["t"]) and re.fullmatch(r"[\d.,:/-]+", cur["t"]):
                cur = {"t": cur["t"] + n["t"], "x": cur["x"], "y": min(cur["y"], n["y"]),
                       "w": n["x"] + n["w"] - cur["x"], "h": max(cur["h"], n["h"])}
                j += 1
            else:
                break
        out.append(cur)
        i = j
    return out


def _value_kind(t):
    t = t.strip()
    if _BP.match(t):
        return "bp"
    if _TIME.match(t):
        return "time"
    if _NUM.match(t):
        return "num"
    return None


def parse_machine_words(words):
    """words จาก OCR พร้อมตำแหน่ง → ค่าบนหน้าจอเครื่องฟอก (เป็น mL, mL/h, นาที)

    หาป้าย (UF volume/UF rate/UF goal/เวลาที่เหลือ/BP) แล้วเลือกตัวเลขที่ใกล้ที่สุด
    ใต้ป้าย (Nikkiso, Fresenius 5008) หรือด้านขวาบรรทัดเดียวกัน (Fresenius 4008S)"""
    if not words:
        return {}
    phrases = _merge_lines(words)
    vals = [v for v in _join_values(words) if _value_kind(v["t"])]
    found = {}
    for key, names in _LABELS.items():
        best = None
        for ph in phrases:
            nt = _norm(ph["t"])
            if not any(nt == n or nt.startswith(n) for n in names):
                continue
            for v in vals:
                kind = _value_kind(v["t"])
                if key == "time_left" and kind != "time":
                    continue
                if key == "bp" and kind != "bp":
                    continue
                if key not in ("time_left", "bp") and kind != "num":
                    continue
                lcx, vcx = ph["x"] + ph["w"] / 2, v["x"] + v["w"] / 2
                dy = v["y"] - (ph["y"] + ph["h"])
                below = -ph["h"] * 0.3 <= dy <= ph["h"] * 7 and abs(vcx - lcx) <= max(ph["w"], v["w"]) * 0.9
                dx = v["x"] - (ph["x"] + ph["w"])
                right = abs((v["y"] + v["h"] / 2) - (ph["y"] + ph["h"] / 2)) <= max(ph["h"], v["h"]) * 0.9 and 0 <= dx <= ph["h"] * 14
                if not (below or right):
                    continue
                score = (dy if below else dx * 0.7) + abs(vcx - lcx) * (0.3 if below else 0)
                if best is None or score < best[0]:
                    best = (score, v["t"])
        if best:
            found[key] = best[1]
    return normalize_machine_values(found)


def _to_float(t):
    return float(t.replace(",", "."))


def normalize_machine_values(raw):
    """ข้อความดิบ → หน่วยของระบบ: UF (mL), UF rate (mL/h), เวลาที่เหลือ (นาที)
    Nikkiso แสดงเป็นลิตร (3.91 L, 1.01 L/h) · Fresenius แสดง mL (2405, 333)"""
    out = {"raw": dict(raw)}

    def ml(t, is_rate=False):
        t = t.strip()
        if re.fullmatch(r"0\d{2}", t):            # Nikkiso '0 41' อ่านติดกันเป็น '041' = 0.41 L/h
            return int(round(int(t) * 10))
        v = _to_float(t)
        if ("." in t or "," in t) and v < 15:     # ลิตร
            return int(round(v * 1000))
        if is_rate and v < 15:                    # '1' L/h
            return int(round(v * 1000))
        return int(round(v))

    try:
        if "uf_volume" in raw:
            out["uf_removed"] = ml(raw["uf_volume"])
        if "uf_goal" in raw:
            out["uf_goal"] = ml(raw["uf_goal"])
        if "uf_rate" in raw:
            out["uf_rate"] = ml(raw["uf_rate"], is_rate=True)
        if "time_left" in raw:
            h, m = raw["time_left"].split(":")
            out["time_left_min"] = int(h) * 60 + int(m)
        if "bp" in raw:
            m = _BP.match(raw["bp"].replace(" ", ""))
            if m:
                out["sbp"], out["dbp"] = int(m.group(1)), int(m.group(2))
        if "sbp" not in out and "sys" in raw and "dia" in raw:
            out["sbp"], out["dbp"] = int(_to_float(raw["sys"])), int(_to_float(raw["dia"]))
        if "pulse" in raw:
            out["hr"] = int(_to_float(raw["pulse"]))
        if "blood_flow" in raw:
            out["blood_flow"] = int(_to_float(raw["blood_flow"]))
    except (ValueError, TypeError):
        pass
    if out.get("uf_goal") and out.get("uf_removed") is not None:
        out["uf_remaining"] = max(0, out["uf_goal"] - out["uf_removed"])
    # ค่าที่ไม่สมเหตุสมผล → ทิ้ง (ให้พยาบาลกรอกเอง)
    if out.get("uf_removed") is not None and not (0 <= out["uf_removed"] <= 8000):
        out.pop("uf_removed")
    if out.get("uf_rate") is not None and not (0 <= out["uf_rate"] <= 3000):
        out.pop("uf_rate")
    if out.get("sbp") is not None and not (50 <= out["sbp"] <= 260):
        out.pop("sbp", None)
        out.pop("dbp", None)
    return out


def parse_machine_text(text):
    """สำรองเมื่อ OCR ไม่มีตำแหน่งคำ: 'UF volume 3.91' / 'UF goal 2500' ในข้อความเรียง"""
    t = " ".join((text or "").split())
    raw = {}
    pats = {
        "uf_volume": r"uf\s*vol(?:ume)?\D{0,12}?(\d{1,4}(?:[.,]\d{1,2})?)",
        "uf_goal": r"uf\s*goal\D{0,12}?(\d{1,4}(?:[.,]\d{1,2})?)",
        "uf_rate": r"uf\s*rate\D{0,12}?(\d{1,4}(?:[.,]\d{1,2})?)",
        "time_left": r"(?:remain(?:ing)?\s*time|time\s*left|time\s*remaining)\D{0,12}?(\d{1,2}:\d{2})",
        "bp": r"bld\.?\s*press\.?\D{0,6}?(\d{2,3}\s*/\s*\d{2,3})",
    }
    for k, p in pats.items():
        m = re.search(p, t, re.IGNORECASE)
        if m:
            raw[k] = m.group(1)
    return normalize_machine_values(raw)
