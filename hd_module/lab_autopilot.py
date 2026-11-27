"""🧪→💊 Lab-to-Order Autopilot — ผลแล็บยืนยันแล้ว → ร่าง "ข้อเสนอคำสั่ง" ตามโปรโตคอลคลินิก (ว้าว B ระยะ 1, 27 พ.ย. 2569)

ต่อยอดจาก esa_autopilot (Hb → ESA) ให้ครอบคลุมแล็บอื่น — ระบบ **เสนอ** แพทย์ **ตัดสิน** ไม่มีอะไรถูกสั่งเอง:
    iron  : TSAT / Ferritin  → เหล็ก IV ตาม regimen ที่ตั้ง / งดเหล็กเมื่อ ferritin สูง
    mbd   : P, Ca (corrected ด้วย albumin ถ้ามี), PTH → binder / active vit D / cinacalcet
    vitd  : 25-OH vitamin D ต่ำ → เสริม
    hbv   : anti-HBs < 10 (HBsAg ลบ) → booster · HBsAg บวกใหม่ → ทบทวน/แยกโซน
    hcv   : anti-HCV บวกใหม่ → ส่งต่อ/ยืนยัน RNA
(K สูง ไม่ซ้ำที่นี่ — มีระบบ lab alert วิกฤตส่ง LINE ทันทีอยู่แล้ว)

กติกาปลอดภัย: สวิตช์ ⚙️ `lab_autopilot` default off · เกณฑ์ทุกตัวแก้ได้ใน ⚙️ (คีย์ lab_ap_*) ·
กันซ้ำ: 1 ข้อเสนอ/ผู้ป่วย/หมวด/วันที่แล็บ, ไม่เสนอซ้ำถ้ามีรายการรออยู่, เว้น min_interval หลังตัดสิน ·
Anti-HIV ไม่เกี่ยวข้องและไม่ถูกอ่าน
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SETTING_KEY = "lab_autopilot"

DEFAULTS = {
    # iron (ใช้คีย์เดียวกับ anemia.py เพื่อให้เกณฑ์ตรงกันทั้งระบบ)
    "anemia_ferritin_low": 200.0, "anemia_tsat_low": 20.0, "anemia_ferritin_high": 800.0, "lab_ap_tsat_high": 50.0,
    # mbd
    "lab_ap_p_high": 5.5, "lab_ap_p_low": 2.5, "lab_ap_ca_high": 10.2, "lab_ap_ca_low": 8.4,
    "lab_ap_pth_high": 600.0, "lab_ap_pth_low": 130.0,
    # vit D
    "lab_ap_vitd_low": 30.0,
    # hbv
    "lab_ap_antihbs_low": 10.0,
    # cadence
    "lab_ap_min_interval_days": 28.0, "lab_ap_lookback_days": 45.0,
}
TEXT_DEFAULTS = {
    "lab_ap_iron_regimen": "Iron sucrose 100 mg IV หลังฟอก × 5 ครั้งติดต่อกัน แล้วตรวจ TSAT/Ferritin ซ้ำใน 4–8 สัปดาห์",
    "lab_ap_binder_regimen": "เริ่ม/เพิ่ม phosphate binder (เช่น CaCO3 1 g tid กับอาหาร) + ทบทวนอาหารฟอสเฟตสูง",
    "lab_ap_pth_regimen": "เริ่ม/เพิ่ม calcitriol 0.25–0.5 mcg หลังฟอก หรือ cinacalcet 25 mg/วัน (ถ้า Ca ไม่ต่ำ)",
    "lab_ap_vitd_regimen": "Vitamin D2 20,000 IU สัปดาห์ละครั้ง × 8 สัปดาห์ แล้วตรวจซ้ำ",
    "lab_ap_hbv_regimen": "HBV vaccine booster 40 mcg IM 1 เข็ม แล้วตรวจ anti-HBs ซ้ำใน 1–2 เดือน",
}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hd_lab_proposals (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER NOT NULL,
    hn VARCHAR(20),
    domain VARCHAR(20) NOT NULL,
    lab_date DATE,
    trigger_text TEXT,
    action VARCHAR(30) NOT NULL,
    proposed TEXT,
    rationale TEXT,
    priority VARCHAR(10) DEFAULT 'routine',
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW(),
    decided_by VARCHAR(100),
    decided_at TIMESTAMP,
    note TEXT,
    relayed BOOLEAN DEFAULT FALSE,
    UNIQUE (patient_id, domain, lab_date)
);
CREATE INDEX IF NOT EXISTS idx_lab_proposals_status ON hd_lab_proposals (status);
"""

DOMAIN_TH = {"iron": "เหล็ก", "mbd": "Ca/P/PTH", "vitd": "วิตามินดี", "hbv": "ไวรัสตับอักเสบบี", "hcv": "ไวรัสตับอักเสบซี"}
ACTION_TH = {
    "iron_iv": "ให้เหล็ก IV", "iron_hold": "งดเหล็ก", "binder_up": "เพิ่ม/เริ่ม binder", "binder_hold": "ลด/งด binder",
    "ca_review": "ทบทวน Ca", "pth_treat": "รักษา PTH สูง", "vitd_active_hold": "ลด/งด active vit D",
    "vitd_supp": "เสริมวิตามินดี", "hbv_booster": "ฉีด HBV booster", "hbsag_pos": "HBsAg บวก — ทบทวน", "hcv_pos": "anti-HCV บวก — ส่งต่อ",
}


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def _positive(v) -> bool | None:
    """'Positive' / 'Reactive' / '+' → True · 'Negative'/'Non-reactive'/'-' → False · อื่น ๆ None"""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    if any(w in s for w in ("non", "neg", "ลบ", "-", "nr")):
        return False
    if any(w in s for w in ("pos", "react", "บวก", "+", "detected")):
        return True
    return None


def protocol(get_setting=None) -> dict:
    """เกณฑ์ตัวเลข + ข้อความ regimen จาก ⚙️ (คืนค่าเริ่มต้นเมื่อว่าง)"""
    out = {}
    for k, dv in DEFAULTS.items():
        v = None
        if get_setting:
            try:
                v = get_setting(k, "")
            except Exception:
                v = None
        try:
            out[k] = float(v) if v not in ("", None) else dv
        except (TypeError, ValueError):
            out[k] = dv
    for k, dv in TEXT_DEFAULTS.items():
        v = None
        if get_setting:
            try:
                v = get_setting(k, "")
            except Exception:
                v = None
        out[k] = (str(v).strip() if v not in ("", None) else dv)
    return out


def is_enabled(get_setting=None) -> bool:
    try:
        if get_setting is None:
            from .blueprint import get_setting_value as get_setting
        return str(get_setting(SETTING_KEY, "off") or "off").strip().lower() == "on"
    except Exception:
        return False


# ───────────────────────── pure rules ─────────────────────────

def evaluate(labs: dict, p: dict | None = None, context: dict | None = None) -> list[dict]:
    """labs: {ferritin, tsat, phos, ca, albumin, pth, vitd, hbsag, antihbs, antihcv} (ค่าว่างได้)
    context: {prev_hbsag_pos: bool|None, prev_antihcv_pos: bool|None}
    → รายการข้อเสนอ [{"domain","action","proposed","rationale","priority","trigger_text"}] (ว่าง = ไม่มีอะไรต้องทำ)"""
    p = {**DEFAULTS, **TEXT_DEFAULTS, **(p or {})}
    ctx = context or {}
    out = []
    fer, tsat = _f(labs.get("ferritin")), _f(labs.get("tsat"))
    phos, ca, alb, pth = _f(labs.get("phos")), _f(labs.get("ca")), _f(labs.get("albumin")), _f(labs.get("pth"))
    vitd = _f(labs.get("vitd"))
    hbsag, antihbs, antihcv = _positive(labs.get("hbsag")), _f(labs.get("antihbs")), _positive(labs.get("antihcv"))

    # ── iron ──
    if fer is not None or tsat is not None:
        trig = " / ".join(x for x in (f"Ferritin {fer:g}" if fer is not None else "", f"TSAT {tsat:g}%" if tsat is not None else "") if x)
        if (fer is not None and fer > p["anemia_ferritin_high"]) or (tsat is not None and tsat > p["lab_ap_tsat_high"]):
            out.append({"domain": "iron", "action": "iron_hold", "proposed": "งดเหล็ก IV จนกว่าจะตรวจซ้ำ",
                        "rationale": f"{trig} — เกินเกณฑ์ (ferritin > {p['anemia_ferritin_high']:g} หรือ TSAT > {p['lab_ap_tsat_high']:g}%) เสี่ยงเหล็กเกิน",
                        "priority": "routine", "trigger_text": trig})
        elif (fer is not None and fer < p["anemia_ferritin_low"]) or (tsat is not None and tsat < p["anemia_tsat_low"]):
            out.append({"domain": "iron", "action": "iron_iv", "proposed": p["lab_ap_iron_regimen"],
                        "rationale": f"{trig} — ขาดเหล็ก (ferritin < {p['anemia_ferritin_low']:g} หรือ TSAT < {p['anemia_tsat_low']:g}%)",
                        "priority": "routine", "trigger_text": trig})

    # ── mbd ──
    ca_c = ca
    if ca is not None and alb is not None and alb < 4.0:
        ca_c = round(ca + 0.8 * (4.0 - alb), 2)
    mbd_bits = [x for x in (f"P {phos:g}" if phos is not None else "", f"Ca {ca:g}" + (f" (corr {ca_c:g})" if ca_c != ca else "") if ca is not None else "",
                            f"PTH {pth:g}" if pth is not None else "") if x]
    trig_m = " / ".join(mbd_bits)
    if phos is not None and phos > p["lab_ap_p_high"]:
        out.append({"domain": "mbd", "action": "binder_up", "proposed": p["lab_ap_binder_regimen"],
                    "rationale": f"{trig_m} — ฟอสเฟตสูง (> {p['lab_ap_p_high']:g})", "priority": "routine", "trigger_text": trig_m})
    elif phos is not None and phos < p["lab_ap_p_low"]:
        out.append({"domain": "mbd", "action": "binder_hold", "proposed": "ลด/งด phosphate binder + ประเมินภาวะทุพโภชนาการ",
                    "rationale": f"{trig_m} — ฟอสเฟตต่ำ (< {p['lab_ap_p_low']:g})", "priority": "routine", "trigger_text": trig_m})
    if ca_c is not None and ca_c > p["lab_ap_ca_high"]:
        out.append({"domain": "mbd", "action": "ca_review", "proposed": "ลด/หยุด calcium-based binder และ active vitamin D · พิจารณา dialysate Ca ต่ำ",
                    "rationale": f"{trig_m} — แคลเซียมสูง (> {p['lab_ap_ca_high']:g})", "priority": "routine", "trigger_text": trig_m})
    elif ca_c is not None and ca_c < p["lab_ap_ca_low"]:
        out.append({"domain": "mbd", "action": "ca_review", "proposed": "ทบทวน calcium / active vitamin D (แคลเซียมต่ำ)",
                    "rationale": f"{trig_m} — แคลเซียมต่ำ (< {p['lab_ap_ca_low']:g})", "priority": "routine", "trigger_text": trig_m})
    if pth is not None and pth > p["lab_ap_pth_high"]:
        hi_ca = ca_c is not None and ca_c > p["lab_ap_ca_high"]
        out.append({"domain": "mbd", "action": "pth_treat",
                    "proposed": ("cinacalcet 25 mg/วัน (Ca สูง — งด active vit D)" if hi_ca else p["lab_ap_pth_regimen"]),
                    "rationale": f"{trig_m} — PTH สูง (> {p['lab_ap_pth_high']:g})", "priority": "routine", "trigger_text": trig_m})
    elif pth is not None and pth < p["lab_ap_pth_low"]:
        out.append({"domain": "mbd", "action": "vitd_active_hold", "proposed": "ลด/งด active vitamin D และ cinacalcet (ระวัง adynamic bone)",
                    "rationale": f"{trig_m} — PTH ต่ำ (< {p['lab_ap_pth_low']:g})", "priority": "routine", "trigger_text": trig_m})

    # ── vit D ──
    if vitd is not None and vitd < p["lab_ap_vitd_low"]:
        out.append({"domain": "vitd", "action": "vitd_supp", "proposed": p["lab_ap_vitd_regimen"],
                    "rationale": f"25-OH vitamin D {vitd:g} ng/mL — ต่ำ (< {p['lab_ap_vitd_low']:g})", "priority": "routine",
                    "trigger_text": f"VitD {vitd:g}"})

    # ── hepatitis ──
    if hbsag is True and not ctx.get("prev_hbsag_pos"):
        out.append({"domain": "hbv", "action": "hbsag_pos", "proposed": "ยืนยัน HBsAg ซ้ำ + HBV DNA · จัดเครื่อง/โซนแยก · แจ้งทีมควบคุมการติดเชื้อ",
                    "rationale": "HBsAg บวก (ครั้งแรกในระบบ)", "priority": "urgent", "trigger_text": "HBsAg Positive"})
    elif hbsag is False and antihbs is not None and antihbs < p["lab_ap_antihbs_low"]:
        out.append({"domain": "hbv", "action": "hbv_booster", "proposed": p["lab_ap_hbv_regimen"],
                    "rationale": f"anti-HBs {antihbs:g} < {p['lab_ap_antihbs_low']:g} mIU/mL (HBsAg ลบ) — ภูมิไม่พอ",
                    "priority": "routine", "trigger_text": f"anti-HBs {antihbs:g}"})
    if antihcv is True and not ctx.get("prev_antihcv_pos"):
        out.append({"domain": "hcv", "action": "hcv_pos", "proposed": "ส่งตรวจ HCV RNA · ส่งต่ออายุรแพทย์ทางเดินอาหาร/ตับ · จัดเครื่องแยก",
                    "rationale": "anti-HCV บวก (ครั้งแรกในระบบ)", "priority": "urgent", "trigger_text": "anti-HCV Positive"})
    return out


# ───────────────────────── generate (DB) ─────────────────────────

def _latest_labs(cur, patient_id, since):
    """ค่าล่าสุดที่ไม่ว่างของแต่ละตัว (ภายใน lookback) + วันที่ของค่าที่ใหม่ที่สุด"""
    cur.execute("""SELECT lab_date, ferritin, tsat, phos, ca, albumin, pth, vitd, hbsag, antihbs, antihcv
                   FROM hd_labs WHERE patient_id = %s AND lab_date >= %s AND COALESCE(verified_status,'manual') <> 'rejected'
                   ORDER BY lab_date DESC LIMIT 10""", (patient_id, since))
    keys = ("ferritin", "tsat", "phos", "ca", "albumin", "pth", "vitd", "hbsag", "antihbs", "antihcv")
    labs, dates = {}, {}
    for r in cur.fetchall():
        for i, k in enumerate(keys, start=1):
            if k not in labs and r[i] not in (None, ""):
                labs[k] = r[i]
                dates[k] = r[0]
    return labs, dates


def _prev_positive(cur, patient_id, col, before):
    try:
        cur.execute(f"SELECT {col} FROM hd_labs WHERE patient_id = %s AND lab_date < %s AND {col} IS NOT NULL ORDER BY lab_date DESC LIMIT 3",
                    (patient_id, before))
        return any(_positive(r[0]) for r in cur.fetchall())
    except Exception:
        return False


def generate(conn, proto: dict | None = None) -> list[dict]:
    """สแกนผู้ป่วย active ทุกคน → สร้างข้อเสนอ pending ใหม่ (กันซ้ำ) → คืนรายการที่สร้าง"""
    p = {**DEFAULTS, **TEXT_DEFAULTS, **(proto or {})}
    created = []
    since = (_thai_today() - timedelta(days=int(p["lab_ap_lookback_days"]))).isoformat()
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        cur.execute("SELECT id, hn, patient_name FROM hd_patient_mapping WHERE is_active = TRUE ORDER BY id")
        patients = cur.fetchall()
        for pid, hn, name in patients:
            try:
                labs, dates = _latest_labs(cur, pid, since)
            except Exception as e:  # noqa: BLE001 — hd_labs อาจยังไม่มีคอลัมน์บางตัวในบางสภาพแวดล้อม
                conn.rollback()
                logger.info("lab autopilot: labs unavailable for %s: %s", hn, e)
                continue
            if not labs:
                continue
            hb_date = max(dates.values()) if dates else None
            ctx = {}
            if labs.get("hbsag") is not None and hb_date:
                ctx["prev_hbsag_pos"] = _prev_positive(cur, pid, "hbsag", dates.get("hbsag"))
            if labs.get("antihcv") is not None and hb_date:
                ctx["prev_antihcv_pos"] = _prev_positive(cur, pid, "antihcv", dates.get("antihcv"))
            for prop in evaluate(labs, p, ctx):
                dom = prop["domain"]
                lab_date = max((dates[k] for k in dates if k in _DOMAIN_KEYS[dom]), default=hb_date)
                cur.execute("""SELECT status, decided_at FROM hd_lab_proposals WHERE patient_id = %s AND domain = %s
                               ORDER BY id DESC LIMIT 1""", (pid, dom))
                last = cur.fetchone()
                if last and last[0] == "pending":
                    continue
                if last and last[1] and (_thai_today() - last[1].date()).days < int(p["lab_ap_min_interval_days"]) and prop["priority"] != "urgent":
                    continue
                cur.execute("SELECT 1 FROM hd_lab_proposals WHERE patient_id = %s AND domain = %s AND lab_date = %s", (pid, dom, lab_date))
                if cur.fetchone():
                    continue
                cur.execute("""INSERT INTO hd_lab_proposals (patient_id, hn, domain, lab_date, trigger_text, action, proposed, rationale, priority)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                            (pid, hn, dom, lab_date, prop["trigger_text"], prop["action"], prop["proposed"], prop["rationale"], prop["priority"]))
                created.append({"id": cur.fetchone()[0], "hn": hn, "name": name, "domain": dom, "action": prop["action"],
                                "proposed": prop["proposed"], "rationale": prop["rationale"], "priority": prop["priority"],
                                "trigger_text": prop["trigger_text"], "lab_date": lab_date.isoformat() if lab_date else None})
    conn.commit()
    return created


_DOMAIN_KEYS = {"iron": ("ferritin", "tsat"), "mbd": ("phos", "ca", "albumin", "pth"), "vitd": ("vitd",),
                "hbv": ("hbsag", "antihbs"), "hcv": ("antihcv",)}


# ───────────────────────── messages ─────────────────────────

def line_summary(created: list[dict], base_url: str) -> str:
    if not created:
        return ""
    urgent = [c for c in created if c["priority"] == "urgent"]
    L = [f"🧪→💊 Lab Autopilot — {len(created)} ข้อเสนอจากผลแล็บ (รออนุมัติ)" + (f" · 🚨 ด่วน {len(urgent)}" if urgent else ""), "━━━━━━━━━━━━"]
    for c in sorted(created, key=lambda x: (x["priority"] != "urgent", x["hn"]))[:10]:
        L.append(f"{'🚨' if c['priority'] == 'urgent' else '•'} {c['name']} (HN {c['hn']}) {c['trigger_text']} → {ACTION_TH.get(c['action'], c['action'])}")
    if len(created) > 10:
        L.append(f"… อีก {len(created) - 10} รายการ")
    L.append("━━━━━━━━━━━━")
    L.append(f"✍️ อนุมัติ/แก้/ไม่ปรับ ทีละรายการ: {base_url}/hd/esa (แท็บ 🧪 Lab→Order)")
    return "\n".join(L)


def nurse_order_text(row: dict, physician_name: str) -> str:
    """ข้อความส่งเข้ากลุ่มพยาบาลเมื่อแพทย์อนุมัติ (ใช้ท่อ physician_order เดิม)"""
    return (f"💊 คำสั่งแพทย์ (จากผลแล็บ) — {row.get('name') or ''} HN {row.get('hn')}\n"
            f"🧪 {row.get('trigger_text') or ''}\n"
            f"📝 {row.get('proposed') or ''}\n"
            f"เหตุผล: {row.get('rationale') or ''}\n"
            f"👨‍⚕️ {physician_name} · {(datetime.now(timezone.utc) + timedelta(hours=7)).strftime('%d/%m %H:%M')}")
