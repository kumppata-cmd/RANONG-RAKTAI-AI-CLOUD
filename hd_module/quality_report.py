"""Adequacy Guardian + monthly quality report (ว้าว รอบ 2 — #2).

Adequacy
    Measured Kt/V / URR per patient (lab documents → hd_lab_docs.ktv/urr,
    manual session values → hd_sessions.kt_v/urr). When nothing is measured
    in the window we show an *estimate* (clearly labelled) from the
    prescription: K ≈ 0.68 × BFR (high-flux, Qb 250–350), V ≈ 0.55 × weight,
    spKt/V ≈ K·t/V. Under-dialysed patients get the two levers a physician
    normally pulls — "+30 min" or "BFR +50" — with the resulting estimate.
    Advisory only: nothing changes the prescription.

Monthly quality report (PDF + LINE summary, 1st of the month 22:30)
    sessions, adequacy, anemia (Hb 10–12), iron, IDH rate, vascular access,
    VA Watch, RO water checks, monthly water lab, incidents, alerts/orders,
    ESA/iron use, Autopilot decisions. Built from PostgreSQL only.
"""
import io
import logging
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

KTV_TARGET = 1.2
URR_TARGET = 65.0
MONTH_TH = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม", "สิงหาคม",
            "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def _q(cur, sql, params=()):
    try:
        cur.execute("SAVEPOINT qr")
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT qr")
        return rows
    except Exception as e:
        logger.warning("quality_report block skipped: %s", str(e)[:120])
        try:
            cur.execute("ROLLBACK TO SAVEPOINT qr")
        except Exception:
            pass
        return []


# ───────────────────────── Adequacy ─────────────────────────

def estimate_ktv(bfr, hours, weight):
    """spKt/V estimate from prescription (rough; labelled as estimate)."""
    b, h, w = _f(bfr), _f(hours), _f(weight)
    if not (b and h and w):
        return None
    k = 0.68 * b                       # mL/min effective urea clearance
    v = 0.55 * w * 1000                # mL
    return round(k * h * 60 / v, 2)


def adequacy_options(bfr, hours, weight, target=KTV_TARGET):
    """What would get the estimate to target: +30 min and/or BFR +50."""
    opts = []
    b, h, w = _f(bfr) or 300, _f(hours) or 4.0, _f(weight)
    if not w:
        return opts
    for dh in (0.5, 1.0):
        e = estimate_ktv(b, h + dh, w)
        opts.append({"type": "time", "hours": h + dh, "est": e, "text": f"เพิ่มเวลา +{int(dh*60)} นาที → ≈{e}"})
        if e and e >= target:
            break
    for db in (50, 100):
        e = estimate_ktv(b + db, h, w)
        opts.append({"type": "bfr", "bfr": b + db, "est": e, "text": f"BFR {int(b)}→{int(b+db)} → ≈{e}"})
        if e and e >= target:
            break
    return opts


def adequacy(conn, days=90):
    """Per active patient: measured Kt/V/URR in the window, else estimate; flag < target."""
    since = (_thai_today() - timedelta(days=days)).isoformat()
    out = []
    with conn.cursor() as cur:
        pats = _q(cur, """SELECT p.id, p.hn, p.patient_name, p.dry_weight, p.vascular_access,
                                 r.blood_flow_rate, r.duration_hours, p.bfr, COALESCE(p.hd_per_week, 3)
                          FROM hd_patient_mapping p
                          LEFT JOIN LATERAL (SELECT blood_flow_rate, duration_hours FROM hd_prescriptions
                                             WHERE patient_id = p.id AND is_active = TRUE ORDER BY id DESC LIMIT 1) r ON TRUE
                          WHERE p.is_active = TRUE ORDER BY p.patient_name""")
        ids = [p[0] for p in pats]
        meas = {}
        for sql in ("""SELECT patient_id, lab_date, ktv, urr FROM hd_labs
                       WHERE patient_id = ANY(%s) AND (ktv IS NOT NULL OR urr IS NOT NULL) AND lab_date >= %s""",
                    """SELECT patient_id, session_date, kt_v, urr FROM hd_sessions
                       WHERE patient_id = ANY(%s) AND (kt_v IS NOT NULL OR urr IS NOT NULL) AND session_date >= %s"""):
            for pid, d, k, u in _q(cur, sql, (ids, since)):
                cur_d = meas.get(pid, (None,))[0]
                if d and (cur_d is None or d > cur_d):
                    meas[pid] = (d, _f(k), _f(u))
        # actual BFR achieved (median of flowsheet) in the window
        bfr_act = {r[0]: _f(r[1]) for r in _q(cur, """
            SELECT s.patient_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY f.bfr)
            FROM hd_sessions s JOIN hd_flowsheet f ON f.session_id = s.id
            WHERE s.patient_id = ANY(%s) AND s.session_date >= %s AND f.bfr IS NOT NULL GROUP BY s.patient_id""", (ids, since))}
    for pid, hn, name, dw, va, rx_bfr, rx_h, p_bfr, per_week in pats:
        d, k, u = meas.get(pid, (None, None, None))
        bfr = _f(rx_bfr) or _f(p_bfr) or 300
        hours = _f(rx_h) or 4.0
        est = estimate_ktv(bfr_act.get(pid) or bfr, hours, dw)
        if k is not None or u is not None:
            ok = (k is not None and k >= KTV_TARGET) or (k is None and u is not None and u >= URR_TARGET)
            src = "measured"
        elif est is not None:
            ok = est >= KTV_TARGET
            src = "estimate"
        else:
            ok, src = None, "none"
        item = {"patient_id": pid, "hn": hn, "name": name, "access": va, "ktv": k, "urr": u,
                "measured_date": d.isoformat() if d else None, "est": est, "source": src, "adequate": ok,
                "bfr_rx": bfr, "bfr_actual": bfr_act.get(pid), "hours": hours, "per_week": per_week, "weight": _f(dw),
                "options": adequacy_options(bfr_act.get(pid) or bfr, hours, dw) if ok is False else []}
        if bfr_act.get(pid) and bfr and bfr_act[pid] < 0.9 * bfr:
            item["note"] = f"BFR ได้จริง ~{bfr_act[pid]:.0f} ต่ำกว่าที่สั่ง {bfr:.0f} — ตรวจ access/เข็ม"
        out.append(item)
    out.sort(key=lambda x: (0 if x["adequate"] is False else (1 if x["adequate"] is None else 2), x["ktv"] or x["est"] or 0))
    return out


# ───────────────────────── Monthly quality data ─────────────────────────

def month_bounds(year, month):
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    return first, last


def collect_month(conn, year, month):
    first, last = month_bounds(year, month)
    f, l = first.isoformat(), last.isoformat()
    D = {"year": year, "month": month, "first": f, "last": l, "label": f"{MONTH_TH[month]} {year + 543}"}
    with conn.cursor() as cur:
        r = _q(cur, "SELECT COUNT(*) FROM hd_patient_mapping WHERE is_active = TRUE")
        D["active_patients"] = r[0][0] if r else 0
        r = _q(cur, """SELECT COUNT(*) FILTER (WHERE status='completed'), COUNT(*) FILTER (WHERE status='cancelled'),
                              COUNT(DISTINCT patient_id) FILTER (WHERE status='completed'),
                              COUNT(*) FILTER (WHERE status='completed' AND COALESCE(shift,'morning')='morning'),
                              COUNT(*) FILTER (WHERE status='completed' AND shift='afternoon'),
                              COUNT(*) FILTER (WHERE status='completed' AND shift='evening')
                       FROM hd_sessions WHERE session_date BETWEEN %s AND %s""", (f, l))
        D["sessions"] = dict(zip(("completed", "cancelled", "patients", "morning", "afternoon", "evening"), r[0])) if r else {}
        # ยกเลิก/ขาดฟอก แยกเหตุผล (additive — cancel_reason เริ่มเก็บ 21 พ.ย. 2026)
        r = _q(cur, """SELECT COALESCE(NULLIF(TRIM(cancel_reason), ''), 'ไม่ระบุเหตุผล'), COUNT(*)
                       FROM hd_sessions WHERE status='cancelled' AND session_date BETWEEN %s AND %s
                       GROUP BY 1 ORDER BY 2 DESC""", (f, l))
        D["cancel_reasons"] = {row[0]: row[1] for row in (r or [])}
        # IDH
        r = _q(cur, """SELECT COUNT(DISTINCT a.session_id) FROM hd_clinical_alerts a JOIN hd_sessions s ON s.id = a.session_id
                       WHERE s.session_date BETWEEN %s AND %s AND a.alert_type IN ('idh','hypotension')""", (f, l))
        D["idh_sessions"] = r[0][0] if r else 0
        # alerts / orders
        r = _q(cur, """SELECT COUNT(*), COUNT(*) FILTER (WHERE a.severity::text='critical'),
                              COUNT(*) FILTER (WHERE a.physician_order IS NOT NULL),
                              COUNT(*) FILTER (WHERE a.severity::text IN ('critical','warning') AND a.physician_order IS NULL)
                       FROM hd_clinical_alerts a JOIN hd_sessions s ON s.id = a.session_id WHERE s.session_date BETWEEN %s AND %s""", (f, l))
        D["alerts"] = dict(zip(("total", "critical", "with_order", "pending"), r[0])) if r else {}
        # anemia: latest Hb per patient inside the month (all sources)
        hb = {}
        for sql in ("""SELECT patient_id, lab_date, hb FROM hd_labs WHERE hb IS NOT NULL AND lab_date BETWEEN %s AND %s""",
                    """SELECT patient_id, session_date, hb_value FROM hd_sessions WHERE hb_value IS NOT NULL AND session_date BETWEEN %s AND %s"""):
            for pid, d, v in _q(cur, sql, (f, l)):
                if d and (pid not in hb or d > hb[pid][0]):
                    hb[pid] = (d, float(v))
        vals = [v for _, v in hb.values()]
        D["anemia"] = {"checked": len(vals), "in_target": sum(1 for v in vals if 10 <= v <= 12),
                       "below": sum(1 for v in vals if v < 10), "above": sum(1 for v in vals if v > 12),
                       "mean": round(sum(vals) / len(vals), 1) if vals else None}
        # iron studies in month
        r = _q(cur, """SELECT COUNT(DISTINCT patient_id), COUNT(*) FILTER (WHERE ferritin < 200 OR tsat < 20)
                       FROM hd_labs WHERE (ferritin IS NOT NULL OR tsat IS NOT NULL) AND lab_date BETWEEN %s AND %s""", (f, l))
        D["iron"] = {"checked": r[0][0], "deficient": r[0][1]} if r else {}
        # adequacy in month
        kt = {}
        for sql in ("""SELECT patient_id, lab_date, ktv, urr FROM hd_labs WHERE (ktv IS NOT NULL OR urr IS NOT NULL) AND lab_date BETWEEN %s AND %s""",
                    """SELECT patient_id, session_date, kt_v, urr FROM hd_sessions WHERE (kt_v IS NOT NULL OR urr IS NOT NULL) AND session_date BETWEEN %s AND %s"""):
            for pid, d, k, u in _q(cur, sql, (f, l)):
                if d and (pid not in kt or d > kt[pid][0]):
                    kt[pid] = (d, _f(k), _f(u))
        ks = [k for _, k, u in kt.values() if k is not None]
        us = [u for _, k, u in kt.values() if k is None and u is not None]
        D["adequacy"] = {"checked": len(kt), "adequate": sum(1 for k in ks if k >= KTV_TARGET) + sum(1 for u in us if u >= URR_TARGET),
                         "mean_ktv": round(sum(ks) / len(ks), 2) if ks else None}
        # vascular access distribution + VA incidents
        D["access"] = {r[0] or "ไม่ระบุ": r[1] for r in _q(cur, """SELECT COALESCE(NULLIF(split_part(vascular_access,' ',1),''),'ไม่ระบุ'), COUNT(*)
                                                               FROM hd_patient_mapping WHERE is_active = TRUE GROUP BY 1 ORDER BY 2 DESC""")}
        # ESA / iron given
        r = _q(cur, """SELECT COUNT(*) FILTER (WHERE esa_given = TRUE), COUNT(*) FROM hd_sessions WHERE status='completed' AND session_date BETWEEN %s AND %s""", (f, l))
        D["esa"] = {"given": r[0][0], "sessions": r[0][1]} if r else {}
        r = _q(cur, """SELECT COUNT(*) FROM hd_medication_given g JOIN hd_sessions s ON s.id = g.session_id
                       WHERE s.session_date BETWEEN %s AND %s AND (g.drug_name ILIKE '%%iron%%' OR g.drug_name ILIKE '%%venofer%%' OR g.drug_name ILIKE '%%ferric%%' OR g.drug_name ILIKE '%%เหล็ก%%')""", (f, l))
        D["iron_doses"] = r[0][0] if r else 0
        # water
        r = _q(cur, """SELECT COUNT(*), COUNT(*) FILTER (WHERE status='pass'), COUNT(*) FILTER (WHERE status='fail')
                       FROM hd_water_daily WHERE check_date BETWEEN %s AND %s AND deleted_at IS NULL""", (f, l))
        D["water_daily"] = dict(zip(("checks", "pass", "fail"), r[0])) if r else {}
        r = _q(cur, """SELECT COUNT(*), COUNT(*) FILTER (WHERE pass_fail='pass') FROM hd_water_quality WHERE test_date BETWEEN %s AND %s""", (f, l))
        D["water_lab"] = {"tests": r[0][0], "pass": r[0][1]} if r else {}
        # incidents
        r = _q(cur, """SELECT COUNT(*), COUNT(*) FILTER (WHERE status <> 'closed' AND status <> 'resolved') FROM hd_incidents WHERE incident_date BETWEEN %s AND %s""", (f, l))
        D["incidents"] = {"total": r[0][0], "open": r[0][1]} if r else {}
        # autopilot decisions
        r = _q(cur, """SELECT COUNT(*), COUNT(*) FILTER (WHERE status='approved'), COUNT(*) FILTER (WHERE status='rejected')
                       FROM hd_esa_proposals WHERE created_at::date BETWEEN %s AND %s""", (f, l))
        D["esa_autopilot"] = dict(zip(("proposed", "approved", "rejected"), r[0])) if r else {}
        r = _q(cur, """SELECT COUNT(*), COUNT(*) FILTER (WHERE status='approved') FROM hd_dw_proposals WHERE created_at::date BETWEEN %s AND %s""", (f, l))
        D["dw_autopilot"] = {"proposed": r[0][0], "approved": r[0][1]} if r else {}
        # under-dialysed list for the appendix
        D["adequacy_list"] = [a for a in adequacy(conn, 90) if a["adequate"] is False][:15]
        # dialyzer reuse
        r = _q(cur, "SELECT COUNT(*), COALESCE(AVG(reuse_count),0) FROM hd_dialyzer_tracking WHERE status='active'")
        D["dialyzer"] = {"active": r[0][0], "avg_reuse": round(float(r[0][1]), 1)} if r else {}
        # ── ว้าว #3 additions ──
        # Fluid Status / BCM: how many assessed in the month, how many OH>2L
        r = _q(cur, """SELECT COUNT(DISTINCT patient_id),
                              COUNT(DISTINCT patient_id) FILTER (WHERE oh_pre_l > 2),
                              COUNT(DISTINCT patient_id) FILTER (WHERE oh_pre_l < -1),
                              ROUND(AVG(oh_pre_l)::numeric, 2)
                       FROM hd_fluid_assessments WHERE assessed_at BETWEEN %s AND %s""", (f, l))
        D["fluid"] = {"assessed": r[0][0], "oh_high": r[0][1], "oh_low": r[0][2], "mean_oh": float(r[0][3]) if r[0][3] else None} if r else {}
        # Serology screening coverage
        r = _q(cur, """SELECT COUNT(*) FILTER (WHERE hbsag IS NOT NULL OR antihcv IS NOT NULL),
                              COUNT(*) FILTER (WHERE hbsag = 'positive'),
                              COUNT(*) FILTER (WHERE antihcv = 'positive')
                       FROM (SELECT DISTINCT ON (patient_id) patient_id, hbsag, antihcv FROM hd_labs
                             WHERE patient_id IN (SELECT id FROM hd_patient_mapping WHERE is_active = TRUE)
                               AND (hbsag IS NOT NULL OR antihcv IS NOT NULL) ORDER BY patient_id, lab_date DESC) sub""")
        D["serology"] = {"screened": r[0][0], "hbsag_pos": r[0][1], "antihcv_pos": r[0][2]} if r else {}
        D["serology"]["total"] = D["active_patients"]
        # IDWG: % of sessions with IDWG > 5% DW
        r = _q(cur, """SELECT COUNT(*),
                              COUNT(*) FILTER (WHERE p.dry_weight > 0 AND s.pre_weight > p.dry_weight AND (s.pre_weight - p.dry_weight) / p.dry_weight > 0.05),
                              COUNT(*) FILTER (WHERE p.dry_weight > 0 AND s.pre_weight > p.dry_weight AND (s.pre_weight - p.dry_weight) / p.dry_weight > 0.03)
                       FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
                       WHERE s.status = 'completed' AND s.session_date BETWEEN %s AND %s AND s.pre_weight IS NOT NULL""", (f, l))
        D["idwg"] = {"sessions": r[0][0], "over_5pct": r[0][1], "over_3pct": r[0][2]} if r else {}
        # Equipment contracts
        r = _q(cur, """SELECT COUNT(*),
                              COUNT(*) FILTER (WHERE contract_end IS NOT NULL AND contract_end < CURRENT_DATE),
                              COUNT(*) FILTER (WHERE contract_end IS NOT NULL AND contract_end >= CURRENT_DATE AND contract_end < CURRENT_DATE + 90)
                       FROM hd_equipment WHERE equipment_type = 'dialysis_machine'""")
        D["equipment"] = {"total": r[0][0], "expired": r[0][1], "expiring_soon": r[0][2]} if r else {}
    s = D["sessions"]
    D["idh_rate"] = round(D["idh_sessions"] / s["completed"] * 100, 1) if s.get("completed") else None
    return D


def _pct(n, d):
    return f"{n / d * 100:.0f}%" if d else "—"


def line_text(D, base_url=""):
    s, a, k, al = D["sessions"], D["anemia"], D["adequacy"], D["alerts"]
    L = [f"📊 รายงานคุณภาพประจำเดือน {D['label']}", "━━━━━━━━━━━━",
         f"🛏 รอบฟอก {s.get('completed', 0)} รอบ · ผู้ป่วย {s.get('patients', 0)} คน (active {D['active_patients']})",
         f"🩸 Hb 10–12: {_pct(a['in_target'], a['checked'])} ({a['in_target']}/{a['checked']}) · ต่ำ {a['below']} · สูง {a['above']}",
         f"🧪 Kt/V ≥1.2: {_pct(k['adequate'], k['checked'])} ({k['adequate']}/{k['checked']})" + (f" · เฉลี่ย {k['mean_ktv']}" if k.get('mean_ktv') else ""),
         f"📉 IDH {D['idh_sessions']} รอบ ({D['idh_rate'] if D['idh_rate'] is not None else '—'}%)",
         f"🚨 Alert {al.get('total', 0)} (วิกฤต {al.get('critical', 0)}) · มีคำสั่งแพทย์ {al.get('with_order', 0)} · ค้าง {al.get('pending', 0)}",
         f"💧 ตรวจน้ำ RO {D['water_daily'].get('checks', 0)} ครั้ง (ไม่ผ่าน {D['water_daily'].get('fail', 0)}) · แล็บน้ำ {D['water_lab'].get('tests', 0)}",
         f"⚠️ อุบัติการณ์ {D['incidents'].get('total', 0)} (เปิดอยู่ {D['incidents'].get('open', 0)})",
         f"🤖 ESA Autopilot เสนอ {D['esa_autopilot'].get('proposed', 0)} อนุมัติ {D['esa_autopilot'].get('approved', 0)} · DW เสนอ {D['dw_autopilot'].get('proposed', 0)}"]
    fl = D.get("fluid", {})
    if fl.get("assessed"):
        L.append(f"💧 BCM ตรวจ {fl['assessed']} คน · OH > 2L: {fl.get('oh_high', 0)} · OH < -1L: {fl.get('oh_low', 0)} · เฉลี่ย OH {fl.get('mean_oh', '—')} L")
    sr = D.get("serology", {})
    if sr.get("total"):
        L.append(f"🩸 Serology screening {sr.get('screened', 0)}/{sr['total']} คน · HBsAg+ {sr.get('hbsag_pos', 0)} · Anti-HCV+ {sr.get('antihcv_pos', 0)}")
    idwg = D.get("idwg", {})
    if idwg.get("sessions"):
        L.append(f"💧 IDWG > 5%DW: {idwg['over_5pct']}/{idwg['sessions']} รอบ ({idwg['over_5pct'] / idwg['sessions'] * 100:.0f}%) · > 3%: {idwg['over_3pct']}")
    cr = D.get("cancel_reasons", {})
    if cr:
        _ns = sum(n for k, n in cr.items() if "ไม่มา" in k)
        _rf = sum(n for k, n in cr.items() if "ส่งต่อ" in k)
        _pp = sum(n for k, n in cr.items() if "เลื่อน" in k)
        _tot = sum(cr.values())
        L.append(f"🚫 ยกเลิกรอบฟอก {_tot} ครั้ง · no-show {_ns} · เลื่อนนัด {_pp} · ส่งต่อ รพ. {_rf}")
    eq = D.get("equipment", {})
    if eq.get("expired") or eq.get("expiring_soon"):
        L.append(f"🔧 เครื่องฟอก: สัญญาหมด {eq.get('expired', 0)} · หมดใน 90 วัน {eq.get('expiring_soon', 0)}")
    if D["adequacy_list"]:
        L.append(f"🔎 ฟอกไม่พอ (Kt/V <1.2) {len(D['adequacy_list'])} คน — ดูข้อเสนอที่ {base_url}/hd/adequacy")
    L.append("━━━━━━━━━━━━")
    L.append("📄 PDF ฉบับเต็มอยู่ใน Google Drive › รายงานคุณภาพ")
    return "\n".join(L)


def pdf_bytes(D, clinic_name="คลินิกระนอง รักษ์ไต"):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from .daily_records import _styles, register_fonts
    register_fonts()
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"รายงานคุณภาพ {D['label']}")
    s, a, k, al = D["sessions"], D["anemia"], D["adequacy"], D["alerts"]
    el = [Paragraph(f"รายงานคุณภาพหน่วยไตเทียม — {D['label']}", st["h1"]),
          Paragraph(f"{clinic_name} · จัดทำอัตโนมัติ {_thai_today().day}/{_thai_today().month}/{_thai_today().year + 543} จากข้อมูลในระบบ HD Smart Monitoring", st["base"]),
          Spacer(1, 6)]

    def tbl(rows, widths):
        t = Table(rows, colWidths=widths)
        t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "Loma"), ("FONTSIZE", (0, 0), (-1, -1), 9),
                               ("FONTNAME", (0, 0), (-1, 0), "Loma-Bold"), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF7")),
                               ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B0BCCB")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8FB")])]))
        return t

    el.append(Paragraph("1. ตัวชี้วัดหลัก", st["h2"]))
    el.append(tbl([["ตัวชี้วัด", "ผล", "เป้าหมาย/หมายเหตุ"],
                   ["รอบฟอกที่เสร็จ", f"{s.get('completed', 0)} รอบ (เช้า {s.get('morning', 0)} / บ่าย {s.get('afternoon', 0)} / ค่ำ {s.get('evening', 0)})", f"ยกเลิก {s.get('cancelled', 0)}"],
                   ["ผู้ป่วยที่ฟอกในเดือน", f"{s.get('patients', 0)} คน", f"active ทั้งหมด {D['active_patients']}"],
                   ["Hb อยู่ในเป้า 10–12 g/dL", f"{_pct(a['in_target'], a['checked'])} ({a['in_target']}/{a['checked']})", f"ต่ำ {a['below']} · สูง {a['above']} · เฉลี่ย {a['mean'] or '—'}"],
                   ["Kt/V ≥ 1.2 (หรือ URR ≥ 65%)", f"{_pct(k['adequate'], k['checked'])} ({k['adequate']}/{k['checked']})", f"เฉลี่ย {k['mean_ktv'] or '—'} · ตรวจ {k['checked']} คน"],
                   ["IDH (ความดันตกระหว่างฟอก)", f"{D['idh_sessions']} รอบ ({D['idh_rate'] if D['idh_rate'] is not None else '—'}%)", "เป้า < 10% ของรอบ"],
                   ["ตรวจเหล็ก (ferritin/TSAT)", f"{D['iron'].get('checked', 0)} คน", f"ขาดเหล็ก {D['iron'].get('deficient', 0)}"],
                   ["ESA ที่ให้", f"{D['esa'].get('given', 0)}/{D['esa'].get('sessions', 0)} รอบ", f"เหล็ก IV {D['iron_doses']} ครั้ง"],
                   ["Alert / คำสั่งแพทย์", f"{al.get('total', 0)} (วิกฤต {al.get('critical', 0)})", f"มีคำสั่ง {al.get('with_order', 0)} · ค้าง {al.get('pending', 0)}"],
                   ["ตรวจน้ำ RO ประจำวัน", f"{D['water_daily'].get('checks', 0)} ครั้ง", f"ผ่าน {D['water_daily'].get('pass', 0)} · ไม่ผ่าน {D['water_daily'].get('fail', 0)}"],
                   ["ผลแล็บน้ำ (เพาะเชื้อ/endotoxin)", f"{D['water_lab'].get('tests', 0)} รายการ", f"ผ่าน {D['water_lab'].get('pass', 0)}"],
                   ["อุบัติการณ์", f"{D['incidents'].get('total', 0)}", f"ยังเปิด {D['incidents'].get('open', 0)}"],
                   ["Dialyzer reuse", f"{D['dialyzer'].get('active', 0)} ชิ้นใช้งาน", f"เฉลี่ย {D['dialyzer'].get('avg_reuse', 0)} ครั้ง"],
                   ["Autopilot (เสนอ-แพทย์ตัดสิน)", f"ESA เสนอ {D['esa_autopilot'].get('proposed', 0)} / อนุมัติ {D['esa_autopilot'].get('approved', 0)}", f"DW เสนอ {D['dw_autopilot'].get('proposed', 0)} / อนุมัติ {D['dw_autopilot'].get('approved', 0)}"],
                   ["BCM / Fluid Status", f"ตรวจ {D.get('fluid',{}).get('assessed',0)} คน", f"OH>2L: {D.get('fluid',{}).get('oh_high',0)} · OH<-1L: {D.get('fluid',{}).get('oh_low',0)} · เฉลี่ย OH {D.get('fluid',{}).get('mean_oh','—')} L"],
                   ["HBsAg / Anti-HCV", f"ผลครบ {D.get('serology',{}).get('screened',0)}/{D.get('serology',{}).get('total',0)} คน", f"HBsAg+ {D.get('serology',{}).get('hbsag_pos',0)} · HCV+ {D.get('serology',{}).get('antihcv_pos',0)}"],
                   ["IDWG > 5% DW", f"{D.get('idwg',{}).get('over_5pct',0)}/{D.get('idwg',{}).get('sessions',0)} รอบ", f"เป้า < 5% (> 3%: {D.get('idwg',{}).get('over_3pct',0)})"],
                   ["สัญญาเครื่องฟอก", f"ทั้งหมด {D.get('equipment',{}).get('total',0)} เครื่อง", f"สัญญาหมดอายุ {D.get('equipment',{}).get('expired',0)} · จะหมดใน 90 วัน {D.get('equipment',{}).get('expiring_soon',0)}"]],
                  [62 * mm, 62 * mm, 58 * mm]))
    el.append(Paragraph("2. เส้นฟอกเลือด (ผู้ป่วย active)", st["h2"]))
    acc = D["access"] or {}
    tot = sum(acc.values()) or 1
    el.append(tbl([["ชนิด", "จำนวน", "สัดส่วน"]] + [[kk, str(v), f"{v / tot * 100:.0f}%"] for kk, v in acc.items()], [62 * mm, 40 * mm, 40 * mm]))
    if D["adequacy_list"]:
        el.append(Paragraph("3. ผู้ป่วยที่ Kt/V ต่ำกว่าเป้า (90 วันล่าสุด) — ข้อเสนอเพื่อพิจารณา", st["h2"]))
        rows = [["HN", "ชื่อ", "Kt/V / URR", "ที่มา", "BFR สั่ง/ได้จริง", "ข้อเสนอ (ประมาณการ)"]]
        for x in D["adequacy_list"]:
            val = f"{x['ktv']}" if x["ktv"] is not None else (f"URR {x['urr']:.0f}%" if x["urr"] is not None else f"≈{x['est']}")
            rows.append([x["hn"], x["name"][:22], val, "วัด" if x["source"] == "measured" else "ประมาณ",
                         f"{x['bfr_rx']:.0f}/{x['bfr_actual']:.0f}" if x["bfr_actual"] else f"{x['bfr_rx']:.0f}",
                         " · ".join(o["text"] for o in x["options"][:2])])
        el.append(tbl(rows, [16 * mm, 42 * mm, 22 * mm, 16 * mm, 26 * mm, 60 * mm]))
        el.append(Paragraph("ค่าประมาณจาก K≈0.68×BFR, V≈0.55×น้ำหนัก — ใช้ชี้เป้าเท่านั้น การปรับใบสั่งฟอกเป็นดุลยพินิจแพทย์", st["base"]))
    doc.build(el)
    return buf.getvalue()
