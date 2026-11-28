"""🕵️ หมอเงา (Shadow Nephrologist) — 28 พ.ย. 2569.

Every morning (default 06:30 Thai, before the Morning Brief) the system reads
*every* active patient the way a fellow prepares for rounds — latest labs,
active medications, the last 10 sessions, IDWG, IDH outcomes, access trend,
open alerts — and sends the physician ONE line per patient of "what you might
miss". Patients with nothing to say are not mentioned.

Design rules
- Deterministic, explainable rules (no AI needed; every line carries its numbers).
- Cross-signal: a rule fires on the *combination* (K rising AND RAAS drug;
  Hb high AND ESA unchanged 4 weeks; Kt/V falling AND VP rising …).
- Memory: hd_shadow_findings keeps one row per (patient, fingerprint); a finding
  already sent is not repeated for `quiet_days` unless its severity rises.
  When the rule stops firing the row auto-resolves.
- Switch ⚙️ shadow_doc (default off). Message goes to alert_recipients
  (physician LINE 1:1) only — never to patients or the nurse group.
- Observation only. Nothing here orders, changes or stops anything.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SETTING_KEY = "shadow_doc"
HOUR_KEY = "shadow_doc_hour"
QUIET_KEY = "shadow_doc_quiet_days"
DEFAULT_HOUR = 6
DEFAULT_QUIET_DAYS = 7
MAX_LINES_PER_PATIENT = 2
MAX_MESSAGE_LINES = 22

SEV_ORDER = {"high": 0, "med": 1, "low": 2}
SEV_ICON = {"high": "🔴", "med": "🟠", "low": "🟡"}

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS hd_shadow_findings (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER NOT NULL,
    hn VARCHAR(20),
    rule VARCHAR(40) NOT NULL,
    fingerprint VARCHAR(100) NOT NULL,
    severity VARCHAR(10) NOT NULL,
    text TEXT NOT NULL,
    evidence JSONB,
    first_seen DATE NOT NULL,
    last_seen DATE NOT NULL,
    last_pushed DATE,
    push_count INTEGER DEFAULT 0,
    status VARCHAR(12) DEFAULT 'open',
    ack_by VARCHAR(80),
    ack_at TIMESTAMPTZ,
    ack_note TEXT,
    UNIQUE (patient_id, fingerprint)
)"""

RAAS_WORDS = ("enalapril", "losartan", "valsartan", "candesartan", "telmisartan", "irbesartan", "spironolactone",
              "ramipril", "lisinopril", "perindopril", "olmesartan", "eplerenone", "อีนาลาพริล", "โลซาร์แทน")
BINDER_WORDS = ("sevelamer", "calcium carbonate", "caco3", "lanthanum", "fosrenol", "renvela", "calcium acetate", "แคลเซียม")
ALKALI_WORDS = ("sodium bicarbonate", "sodamint", "nahco3", "bicarb")
VITD_WORDS = ("calcitriol", "alfacalcidol", "one-alpha", "paricalcitol", "rocaltrol")
ESA_WORDS = ("epo", "epoetin", "eprex", "epiao", "hema", "erythropoietin", "darbepoetin", "aranesp", "mircera", "esa")


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def is_enabled(get_setting=None):
    try:
        if get_setting is None:
            from .blueprint import get_setting_value as get_setting
        return str(get_setting(SETTING_KEY, "off") or "off").strip().lower() in ("on", "1", "true", "yes")
    except Exception:
        return False


def _has(meds, words):
    names = []
    for m in meds or []:
        n = (m.get("drug") or "").lower()
        if any(w in n for w in words):
            names.append(m.get("drug"))
    return names


# ───────────────────────── rules ─────────────────────────

def _lab_series(labs, col, n=3):
    """labs newest-first → [(date, value)] newest-first for non-null col (max n)."""
    out = []
    for r in labs:
        v = _f(r.get(col))
        if v is not None:
            out.append((r.get("lab_date"), v))
        if len(out) >= n:
            break
    return out


def _days_since(d, today):
    try:
        return (today - d).days
    except Exception:
        return None


def rules(d, today=None):
    """d: {patient, labs(newest first), meds(active), sessions(newest first), idh_last5, va, alerts_no_order,
    esa_doses(newest first list of (date, dose str))} → list of findings."""
    today = today or _thai_today()
    out = []
    p = d.get("patient") or {}
    labs = d.get("labs") or []
    meds = d.get("meds") or []
    ses = [s for s in (d.get("sessions") or []) if s.get("status") in (None, "completed")]
    dw = _f(p.get("dry_weight"))

    def add(rule, sev, text, fp=None, **ev):
        out.append({"rule": rule, "severity": sev, "text": text, "fingerprint": fp or rule, "evidence": ev})

    # R1 potassium rising
    ks = _lab_series(labs, "k", 3)
    if ks and _days_since(ks[0][0], today) is not None and _days_since(ks[0][0], today) <= 60:
        k0 = ks[0][1]
        rising = len(ks) >= 2 and all(ks[i][1] > ks[i + 1][1] for i in range(len(ks) - 1))
        raas = _has(meds, RAAS_WORDS)
        if k0 >= 6.0:
            add("k_high", "high", f"K {k0:.1f} (ล่าสุด {ks[0][0]:%d/%m})" + (f" · มี {', '.join(raas)}" if raas else "") +
                (" · ขึ้นต่อเนื่อง " + "→".join(f"{v:.1f}" for _, v in reversed(ks)) if rising and len(ks) >= 2 else ""),
                fp="k_high", k=k0, raas=raas)
        elif k0 >= 5.5 and rising and len(ks) >= 2:
            add("k_rising", "med", f"K ขึ้นต่อเนื่อง {'→'.join(f'{v:.1f}' for _, v in reversed(ks))}" +
                (f" · มี {', '.join(raas)}" if raas else ""), fp="k_rising", k=k0, raas=raas)
        elif k0 >= 5.5 and raas:
            add("k_raas", "low", f"K {k0:.1f} และมี {', '.join(raas)} — ทบทวนขนาด/ความจำเป็น", fp="k_raas", k=k0, raas=raas)

    # R2/R3 Hb vs ESA
    hbs = _lab_series(labs, "hb", 2)
    if not hbs:
        hbs = [(s.get("date"), _f(s.get("hb"))) for s in ses if _f(s.get("hb")) is not None][:2]
    esa_doses = d.get("esa_doses") or []
    esa_on = bool(p.get("esa_dose")) or bool(_has(meds, ESA_WORDS)) or bool(esa_doses)
    esa_unchanged_days = None
    if esa_doses:
        last = esa_doses[0][1]
        first_same = esa_doses[0][0]
        for dt, dose in esa_doses:
            if dose == last:
                first_same = dt
            else:
                break
        esa_unchanged_days = _days_since(first_same, today)
    if hbs and _days_since(hbs[0][0], today) is not None and _days_since(hbs[0][0], today) <= 45:
        hb = hbs[0][1]
        unchanged = (esa_unchanged_days or 0) >= 28
        if hb >= 12.0 and esa_on:
            add("hb_high_esa", "high" if hb >= 13 else "med",
                f"Hb {hb:.1f} เกินเป้า" + (f" แต่ ESA ขนาดเดิม {esa_unchanged_days} วัน" if unchanged else " — ทบทวนขนาด ESA") +
                (f" ({p.get('esa_dose')})" if p.get("esa_dose") else ""), fp="hb_high_esa", hb=hb, unchanged_days=esa_unchanged_days)
        elif hb < 9.0:
            tsat = _lab_series(labs, "tsat", 1)
            fer = _lab_series(labs, "ferritin", 1)
            iron_low = (tsat and tsat[0][1] < 20) or (fer and fer[0][1] < 200)
            txt = f"Hb {hb:.1f}"
            if iron_low:
                txt += f" + เหล็กต่ำ (TSAT {tsat[0][1]:.0f}%" if tsat else " + ferritin ต่ำ ("
                txt += f", ferritin {fer[0][1]:.0f})" if fer else ")"
                txt += " — เติมเหล็กก่อนเพิ่ม ESA?"
            elif not esa_on:
                txt += " แต่ยังไม่มี ESA ในระบบ"
            elif unchanged:
                txt += f" แต่ ESA ขนาดเดิมมา {esa_unchanged_days} วัน"
            else:
                txt += " — ติดตาม ESA"
            add("hb_low", "high" if hb < 8 else "med", txt, fp="hb_low", hb=hb, iron_low=bool(iron_low))

    # R4 phosphate / Ca×P
    ph = _lab_series(labs, "phos", 2)
    if ph and _days_since(ph[0][0], today) is not None and _days_since(ph[0][0], today) <= 60:
        p0 = ph[0][1]
        binders = _has(meds, BINDER_WORDS)
        if p0 > 5.5:
            both = len(ph) >= 2 and ph[1][1] > 5.5
            add("phos_high", "high" if p0 > 7 else ("med" if both else "low"),
                f"Phos {p0:.1f}" + (f" (ครั้งก่อน {ph[1][1]:.1f})" if len(ph) >= 2 else "") +
                (f" · binder: {', '.join(binders)}" if binders else " · ไม่มี binder ในระบบ"),
                fp="phos_high", phos=p0, binders=binders)
        ca = _lab_series(labs, "ca", 1)
        if ca and ca[0][1] * p0 > 55:
            add("caxp", "high", f"Ca×P = {ca[0][1] * p0:.0f} (Ca {ca[0][1]:.1f} × P {p0:.1f})", fp="caxp", caxp=round(ca[0][1] * p0, 1))

    # R5 albumin falling
    al = _lab_series(labs, "albumin", 2)
    if al and al[0][1] < 3.5 and _days_since(al[0][0], today) is not None and _days_since(al[0][0], today) <= 90:
        if len(al) >= 2 and al[1][1] - al[0][1] >= 0.3:
            add("albumin_falling", "med", f"Albumin ตก {al[1][1]:.1f}→{al[0][1]:.1f} — โภชนาการ/การอักเสบ?", fp="albumin_falling", alb=al[0][1])
        elif al[0][1] < 3.0:
            add("albumin_low", "med", f"Albumin {al[0][1]:.1f}", fp="albumin_low", alb=al[0][1])

    # R6 bicarbonate
    hc = _lab_series(labs, "hco3", 1)
    if hc and hc[0][1] < 20 and _days_since(hc[0][0], today) is not None and _days_since(hc[0][0], today) <= 60:
        alk = _has(meds, ALKALI_WORDS)
        add("hco3_low", "med" if hc[0][1] < 18 else "low", f"HCO₃ {hc[0][1]:.0f}" + ("" if alk else " · ยังไม่มี bicarbonate"), fp="hco3_low", hco3=hc[0][1])

    # R7 adequacy vs access
    kt = _lab_series(labs, "ktv", 2) or [(s.get("date"), _f(s.get("ktv"))) for s in ses if _f(s.get("ktv")) is not None][:2]
    va = d.get("va") or {}
    if kt and kt[0][1] is not None and kt[0][1] < 1.2 and _days_since(kt[0][0], today) is not None and _days_since(kt[0][0], today) <= 90:
        if va.get("level") in ("watch", "alert"):
            add("ktv_access", "high", f"Kt/V {kt[0][1]:.2f} และ access มีสัญญาณ ({va.get('level')}: {(va.get('signals') or [{}])[0].get('text', '')[:60]}) — สงสัย recirculation/stenosis",
                fp="ktv_access", ktv=kt[0][1])
        else:
            fall = f" (เดิม {kt[1][1]:.2f})" if len(kt) >= 2 and kt[1][1] else ""
            add("ktv_low", "med", f"Kt/V {kt[0][1]:.2f}{fall} — ทบทวนเวลา/BFR/dialyzer", fp="ktv_low", ktv=kt[0][1])
    elif va.get("level") == "alert":
        add("va_alert", "high", "Access: " + "; ".join(s.get("text", "") for s in (va.get("signals") or [])[:2]), fp="va_alert", score=va.get("score"))

    # R8 IDWG
    if dw and ses:
        idwg = [(_f(s.get("pre")) - dw) for s in ses[:5] if _f(s.get("pre")) is not None]
        over = [x for x in idwg if x / dw * 100 > 4.0 or x > 3.5]
        if len(idwg) >= 3 and len(over) >= 3:
            add("idwg_high", "med", f"IDWG เกิน 4% DW {len(over)}/{len(idwg)} รอบ (เฉลี่ย +{sum(idwg) / len(idwg):.1f} kg) — น้ำ/เกลือ หรือ DW ต่ำไป?",
                fp="idwg_high", n=len(over), avg=round(sum(idwg) / len(idwg), 2))

    # R9 IDH cluster (Kidney Twin outcomes)
    idh5 = d.get("idh_last5")
    if idh5 is not None and idh5 >= 2:
        add("idh_cluster", "med" if idh5 < 4 else "high", f"IDH {idh5}/5 รอบล่าสุด — DW/UF goal/เวลาฟอก", fp="idh_cluster", n=idh5)

    # R10 dry-weight stale
    if dw and len(ses) >= 4:
        posts = [_f(s.get("post")) for s in ses[:4] if _f(s.get("post")) is not None]
        if len(posts) >= 4:
            if all(x >= dw + 0.5 for x in posts):
                add("dw_not_reached", "low", f"4 รอบติด น้ำหนักหลังฟอกเหนือ DW ≥0.5 kg (DW {dw:.1f}, ได้ {min(posts):.1f}–{max(posts):.1f}) — DW ยังเหมาะไหม",
                    fp="dw_not_reached", dw=dw)
            elif all(x <= dw - 0.5 for x in posts):
                add("dw_too_high", "low", f"4 รอบติด น้ำหนักหลังฟอกต่ำกว่า DW ≥0.5 kg (DW {dw:.1f}) — ปรับ DW ลง?", fp="dw_too_high", dw=dw)

    # R11 labs overdue
    last_lab = labs[0].get("lab_date") if labs else None
    if p.get("is_active", True):
        ds = _days_since(last_lab, today) if last_lab else None
        if ds is None:
            add("labs_none", "low", "ไม่มีผลแล็บในระบบเลย", fp="labs_none")
        elif ds > 75:
            add("labs_overdue", "med", f"แล็บล่าสุด {ds} วันก่อน", fp="labs_overdue")
        elif ds > 45:
            add("labs_overdue", "low", f"แล็บล่าสุด {ds} วันก่อน", fp="labs_overdue")

    # R12 anti-HBs
    ab = d.get("antihbs")
    if ab is not None:
        try:
            s = str(ab).strip().lower()
            val = float(s.replace("<", "").replace(">", "")) if any(c.isdigit() for c in s) else None
            low = (val is not None and ((val < 10 and not s.startswith(">")) or (s.startswith("<") and val <= 10)))
            if low or s in ("neg", "negative", "ลบ", "non-reactive", "nonreactive"):
                add("antihbs_low", "low", f"anti-HBs {ab} — พิจารณา booster", fp="antihbs_low")
        except Exception:
            pass

    # R13 PTH
    pth = _lab_series(labs, "pth", 1)
    if pth and _days_since(pth[0][0], today) is not None and _days_since(pth[0][0], today) <= 120:
        vitd = _has(meds, VITD_WORDS)
        if pth[0][1] > 600:
            add("pth_high", "med", f"PTH {pth[0][1]:.0f}" + (f" · มี {', '.join(vitd)}" if vitd else " · ไม่มี active vit D"), fp="pth_high", pth=pth[0][1])
        elif pth[0][1] < 100 and vitd:
            add("pth_low_vitd", "low", f"PTH {pth[0][1]:.0f} ต่ำ แต่ยังให้ {', '.join(vitd)} — adynamic?", fp="pth_low_vitd", pth=pth[0][1])

    # R14 alerts without physician order
    an = d.get("alerts_no_order") or 0
    if an:
        add("alerts_no_order", "med", f"alert วิกฤต/เตือน {an} รายการใน 7 วัน ยังไม่มีคำสั่งแพทย์", fp="alerts_no_order", n=an)

    # R16 missed sessions
    miss = d.get("cancelled_14d") or 0
    if miss >= 2:
        add("missed", "med", f"ยกเลิก/ขาดฟอก {miss} รอบใน 14 วัน", fp="missed", n=miss)

    out.sort(key=lambda x: SEV_ORDER.get(x["severity"], 9))
    return out


# ───────────────────────── data collection ─────────────────────────

def _q(cur, sql, params=()):
    try:
        cur.execute("SAVEPOINT sd")
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT sd")
        return rows
    except Exception as e:  # noqa: BLE001
        logger.info("shadow_doc query skipped: %s", str(e)[:120])
        try:
            cur.execute("ROLLBACK TO SAVEPOINT sd")
        except Exception:
            pass
        return []


def collect(conn, today=None):
    """Return list of per-patient dicts for rules()."""
    today = today or _thai_today()
    since_lab = (today - timedelta(days=200)).isoformat()
    since_ses = (today - timedelta(days=70)).isoformat()
    pts = {}
    with conn.cursor() as cur:
        for r in _q(cur, """SELECT id, hn, patient_name, dry_weight, esa_drug, esa_dose, vascular_access, is_active
                            FROM hd_patient_mapping WHERE is_active = TRUE ORDER BY hn"""):
            pts[r[0]] = {"patient": {"id": r[0], "hn": r[1], "name": r[2], "dry_weight": _f(r[3]), "esa_drug": r[4],
                                     "esa_dose": r[5], "access": r[6], "is_active": r[7]},
                         "labs": [], "meds": [], "sessions": [], "esa_doses": [], "idh_last5": None, "va": None,
                         "alerts_no_order": 0, "cancelled_14d": 0, "antihbs": None}
        if not pts:
            return []
        for r in _q(cur, f"""SELECT patient_id, lab_date, hb, k, phos, ca, albumin, pth, hco3, ferritin, tsat, ktv, urr, antihbs
                             FROM hd_labs WHERE lab_date >= %s AND patient_id IN ({','.join(map(str, pts))})
                               AND COALESCE(verified_status,'') <> 'rejected'
                             ORDER BY patient_id, lab_date DESC""", (since_lab,)):
            if r[0] in pts and len(pts[r[0]]["labs"]) < 8:
                pts[r[0]]["labs"].append({"lab_date": r[1], "hb": r[2], "k": r[3], "phos": r[4], "ca": r[5], "albumin": r[6],
                                          "pth": r[7], "hco3": r[8], "ferritin": r[9], "tsat": r[10], "ktv": r[11], "urr": r[12]})
                if r[13] and pts[r[0]]["antihbs"] is None:
                    pts[r[0]]["antihbs"] = r[13]
        for r in _q(cur, f"""SELECT patient_id, drug_name, dose, frequency, drug_category FROM hd_medication_orders
                             WHERE is_active = TRUE AND COALESCE(verify_status,'confirmed') IN ('confirmed','merged','pending')
                               AND patient_id IN ({','.join(map(str, pts))})"""):
            if r[0] in pts:
                pts[r[0]]["meds"].append({"drug": r[1], "dose": r[2], "freq": r[3], "cat": r[4]})
        for r in _q(cur, f"""SELECT s.patient_id, s.session_date, s.pre_weight, s.post_weight, s.kt_v, s.urr, s.hb_value,
                                    s.esa_dose_given, s.status::text,
                                    (SELECT outcome FROM hd_idh_twin_state t WHERE t.session_id = s.id)
                             FROM hd_sessions s WHERE s.session_date >= %s AND s.patient_id IN ({','.join(map(str, pts))})
                             ORDER BY s.patient_id, s.session_date DESC, s.id DESC""", (since_ses,)):
            d = pts.get(r[0])
            if d is None:
                continue
            if r[8] in ("completed", "in_progress") and len(d["sessions"]) < 10:
                d["sessions"].append({"date": r[1], "pre": r[2], "post": r[3], "ktv": r[4], "urr": r[5], "hb": r[6],
                                      "status": "completed" if r[8] == "completed" else r[8], "idh": r[9]})
                if r[7]:
                    d["esa_doses"].append((r[1], str(r[7]).strip()))
            if r[8] == "cancelled" and (today - r[1]).days <= 14:
                d["cancelled_14d"] += 1
        for d in pts.values():
            last5 = [s for s in d["sessions"] if s.get("status") == "completed"][:5]
            known = [s for s in last5 if s.get("idh") in ("idh", "no_idh")]
            d["idh_last5"] = sum(1 for s in known if s["idh"] == "idh") if len(known) >= 3 else None
        for r in _q(cur, """SELECT COALESCE(a.patient_id, s.patient_id), COUNT(*) FROM hd_clinical_alerts a
                            LEFT JOIN hd_sessions s ON s.id = a.session_id
                            WHERE a.created_at >= %s AND a.severity::text IN ('critical','warning')
                              AND COALESCE(a.physician_order,'') = ''
                            GROUP BY 1""", ((today - timedelta(days=7)).isoformat(),)):
            if r[0] in pts:
                pts[r[0]]["alerts_no_order"] = r[1]
        try:
            cur.execute("SAVEPOINT sd_va")
            from .va_watch import collect as va_collect
            for it in va_collect(conn):
                pid = it.get("patient_id")
                if pid in pts:
                    pts[pid]["va"] = {"level": it.get("level"), "score": it.get("score"), "signals": it.get("signals")}
            cur.execute("RELEASE SAVEPOINT sd_va")
        except Exception as e:  # noqa: BLE001
            logger.info("shadow_doc va_watch skipped: %s", str(e)[:100])
            try:
                cur.execute("ROLLBACK TO SAVEPOINT sd_va")
            except Exception:
                pass
    return list(pts.values())


# ───────────────────────── persistence + message ─────────────────────────

def _sev_rank(s):
    return SEV_ORDER.get(s, 9)


def persist(conn, per_patient, today=None, quiet_days=DEFAULT_QUIET_DAYS):
    """per_patient: [(patient_dict, findings)] → upsert rows; returns list of findings to push (dicts with name/hn)."""
    today = today or _thai_today()
    to_push = []
    active_keys = set()
    with conn.cursor() as cur:
        cur.execute(TABLE_SQL)
        for p, fs in per_patient:
            for f in fs:
                key = (p["id"], f["fingerprint"])
                active_keys.add(key)
                cur.execute("""SELECT id, severity, last_pushed, status, push_count FROM hd_shadow_findings
                               WHERE patient_id = %s AND fingerprint = %s""", key)
                row = cur.fetchone()
                push = False
                if row is None:
                    cur.execute("""INSERT INTO hd_shadow_findings (patient_id, hn, rule, fingerprint, severity, text, evidence, first_seen, last_seen)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                (p["id"], p.get("hn"), f["rule"], f["fingerprint"], f["severity"], f["text"],
                                 json.dumps(f.get("evidence") or {}, default=str), today, today))
                    fid = cur.fetchone()[0]
                    push = True
                else:
                    fid, old_sev, last_pushed, status, _pc = row
                    worse = _sev_rank(f["severity"]) < _sev_rank(old_sev)
                    quiet_over = last_pushed is None or (today - last_pushed).days >= quiet_days
                    if status == "dismissed" and not worse:
                        push = False
                    else:
                        push = worse or quiet_over
                    cur.execute("""UPDATE hd_shadow_findings SET severity = %s, text = %s, evidence = %s, last_seen = %s,
                                   status = CASE WHEN status = 'resolved' THEN 'open' WHEN %s THEN 'open' ELSE status END
                                   WHERE id = %s""",
                                (f["severity"], f["text"], json.dumps(f.get("evidence") or {}, default=str), today, worse, fid))
                if push:
                    cur.execute("UPDATE hd_shadow_findings SET last_pushed = %s, push_count = COALESCE(push_count,0)+1 WHERE id = %s", (today, fid))
                    to_push.append({**f, "id": fid, "name": p.get("name"), "hn": p.get("hn"), "patient_id": p["id"]})
        # auto-resolve findings that stopped firing
        cur.execute("SELECT id, patient_id, fingerprint FROM hd_shadow_findings WHERE status IN ('open','ack') AND last_seen < %s", (today,))
        for fid, pid, fp in cur.fetchall():
            if (pid, fp) not in active_keys:
                cur.execute("UPDATE hd_shadow_findings SET status = 'resolved' WHERE id = %s", (fid,))
    conn.commit()
    return to_push


def _short(name):
    return (name or "").replace("นาย", "").replace("นาง", "").replace("นางสาว", "").strip().split(" ")[0] or "?"


def build_message(to_push, n_patients, n_flagged, today=None, base_url=""):
    today = today or _thai_today()
    if not to_push:
        return None
    by = {}
    for f in to_push:
        by.setdefault((f["patient_id"], f["hn"], f["name"]), []).append(f)
    groups = sorted(by.items(), key=lambda kv: min(_sev_rank(x["severity"]) for x in kv[1]))
    lines = [f"🕵️ หมอเงา · {today:%d/%m} เช้านี้", f"ดูแล้ว {n_patients} คน · ควรคิดถึง {n_flagged} คน · มีประเด็นใหม่/ซ้ำครบกำหนด {len(groups)} คน", ""]
    extra = 0
    for (pid, hn, name), fs in groups:
        fs = sorted(fs, key=lambda x: _sev_rank(x["severity"]))[:MAX_LINES_PER_PATIENT]
        if len(lines) >= MAX_MESSAGE_LINES:
            extra += 1
            continue
        head = f"{SEV_ICON.get(fs[0]['severity'], '•')} {_short(name)} (HN {hn})"
        lines.append(head + " — " + fs[0]["text"])
        for f in fs[1:]:
            lines.append(f"   └ {f['text']}")
    if extra:
        lines.append(f"…และอีก {extra} คน")
    lines.append("")
    lines.append(f"ดูทั้งหมด/ปิดประเด็น: {base_url}/hd/shadow" if base_url else "ดูทั้งหมด: /hd/shadow")
    lines.append("ข้อสังเกตอัตโนมัติจากข้อมูลในระบบ — ไม่ใช่คำสั่งรักษา")
    return "\n".join(lines)


def run(conn, push_fn=None, recipients=None, base_url="", today=None, quiet_days=DEFAULT_QUIET_DAYS, dry_run=False):
    """Full cycle. Returns stats dict (never raises on push errors)."""
    today = today or _thai_today()
    data = collect(conn, today)
    per_patient = []
    n_flagged = 0
    for d in data:
        fs = rules(d, today)
        if fs:
            n_flagged += 1
        per_patient.append((d["patient"], fs))
    if dry_run:
        return {"patients": len(data), "flagged": n_flagged,
                "findings": [{"hn": p["hn"], "name": p["name"], **f} for p, fs in per_patient for f in fs]}
    to_push = persist(conn, per_patient, today, quiet_days)
    msg = build_message(to_push, len(data), n_flagged, today, base_url)
    sent = 0
    if msg and push_fn and recipients:
        for uid in recipients:
            try:
                if push_fn(uid, msg) is not False:
                    sent += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("shadow_doc push: %s", type(e).__name__)
    return {"patients": len(data), "flagged": n_flagged, "pushed_items": len(to_push), "sent": sent, "message": msg}


def list_findings(conn, status="open", limit=300):
    with conn.cursor() as cur:
        cur.execute(TABLE_SQL)
        conn.commit()
        where = "WHERE f.status IN ('open','ack')" if status == "open" else ("" if status == "all" else "WHERE f.status = %s")
        params = () if status in ("open", "all") else (status,)
        cur.execute(f"""SELECT f.id, f.patient_id, f.hn, p.patient_name, f.rule, f.severity, f.text, f.first_seen, f.last_seen,
                               f.last_pushed, f.push_count, f.status, f.ack_by, f.ack_note
                        FROM hd_shadow_findings f LEFT JOIN hd_patient_mapping p ON p.id = f.patient_id
                        {where} ORDER BY CASE f.severity WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END, f.last_seen DESC, f.hn
                        LIMIT %s""", params + (limit,))
        return [{"id": r[0], "patient_id": r[1], "hn": r[2], "name": r[3], "rule": r[4], "severity": r[5], "text": r[6],
                 "first_seen": r[7].isoformat() if r[7] else None, "last_seen": r[8].isoformat() if r[8] else None,
                 "last_pushed": r[9].isoformat() if r[9] else None, "push_count": r[10], "status": r[11],
                 "ack_by": r[12], "ack_note": r[13]} for r in cur.fetchall()]


def ack(conn, fid, status, who, note=""):
    if status not in ("ack", "dismissed", "open"):
        return False
    with conn.cursor() as cur:
        cur.execute("UPDATE hd_shadow_findings SET status = %s, ack_by = %s, ack_at = NOW(), ack_note = %s WHERE id = %s",
                    (status, (who or "")[:80], (note or "")[:300], fid))
        ok = cur.rowcount > 0
    conn.commit()
    return ok
