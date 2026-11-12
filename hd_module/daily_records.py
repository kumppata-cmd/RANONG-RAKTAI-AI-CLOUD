"""Daily HD records — PDF per session / per shift, and the day export bundle.

Folder standard (same on GCS, Google Drive and the physician's PC):

    HD-Records/<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/
        HD-<YYYY-MM-DD>.csv
        HD-<YYYY-MM-DD>-เช้า.pdf           (one per shift that had sessions)
        sessions/<HN>-<name>-เตียง<N>-รอบ<shift>.pdf   (summary page + Monitoring Sheet page)

Nothing here changes medical data; it only reads and renders. PostgreSQL stays
the source of truth — these files are copies for backup/inspection.
"""
import io
import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SHIFT_TH = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}
STATUS_TH = {"completed": "เสร็จ", "in_progress": "กำลังฟอก", "scheduled": "รอฟอก", "cancelled": "ยกเลิก"}
_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
_FONTS_READY = False


# ── helpers ──────────────────────────────────────────────

def thai_time(ts):
    """UTC datetime (naive or aware) or ISO string → 'HH:MM' Thai time."""
    if not ts:
        return "-"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", ""))
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return (ts + timedelta(hours=7)).strftime("%H:%M")


def thai_date_long(d):
    months = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    return f"{d.day} {months[d.month - 1]} {d.year + 543}"


def safe_name(s, limit=40):
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", str(s or "")).strip()
    s = re.sub(r"\s+", "-", s)
    return s[:limit] or "ไม่ระบุ"


def record_folder(day):
    return f"HD-Records/{day.year}/{day.strftime('%Y-%m')}/{day.isoformat()}"


def session_filename(hn, name, bed, shift):
    return f"{safe_name(hn, 12)}-{safe_name(name)}-เตียง{bed}-รอบ{SHIFT_TH.get(shift, shift)}.pdf"


def _fmt(v, nd=None, unit=""):
    if v is None or v == "":
        return "-"
    try:
        if nd is not None:
            return f"{float(v):.{nd}f}{unit}"
        if isinstance(v, float) and v.is_integer():
            return f"{int(v)}{unit}"
    except (TypeError, ValueError):
        pass
    return f"{v}{unit}"


def register_fonts():
    """Register the bundled Thai TrueType font with reportlab (idempotent)."""
    global _FONTS_READY
    if _FONTS_READY:
        return True
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.lib.fonts import addMapping
        pdfmetrics.registerFont(TTFont("Loma", os.path.join(_FONT_DIR, "Loma.ttf")))
        pdfmetrics.registerFont(TTFont("Loma-Bold", os.path.join(_FONT_DIR, "Loma-Bold.ttf")))
        addMapping("Loma", 0, 0, "Loma")
        addMapping("Loma", 1, 0, "Loma-Bold")
        _FONTS_READY = True
    except Exception as e:  # pragma: no cover
        logger.error("daily_records: font registration failed: %s", e)
        _FONTS_READY = False
    return _FONTS_READY


def _styles():
    from reportlab.lib.styles import ParagraphStyle
    base = ParagraphStyle("base", fontName="Loma", fontSize=9.5, leading=13)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10.5),
        "h1": ParagraphStyle("h1", parent=base, fontName="Loma-Bold", fontSize=15, leading=19, spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=base, fontName="Loma-Bold", fontSize=11, leading=14, spaceBefore=8, spaceAfter=3),
        "muted": ParagraphStyle("muted", parent=base, fontSize=8, leading=10.5, textColor="#666666"),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8.2, leading=10.5),
    }


def _table(data, col_widths, header=True, font_size=8.2, zebra=True):
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib import colors
    t = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)
    st = [
        ("FONTNAME", (0, 0), (-1, -1), "Loma"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("LEADING", (0, 0), (-1, -1), font_size + 2.5),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#b8c2cc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    if header:
        st += [("FONTNAME", (0, 0), (-1, 0), "Loma-Bold"),
               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e3ecf4"))]
    if zebra:
        for i in range(2 if header else 1, len(data), 2):
            st.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f6f8fa")))
    t.setStyle(TableStyle(st))
    return t


# ── data access ───────────────────────────────────────────

def load_session_record(conn, session_id):
    """Everything the per-session PDF needs. Returns dict or None."""
    from .blueprint import flowsheet_entries
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.id, s.session_date, s.shift, s.bed_number, s.status,
                   s.started_at, s.ended_at, s.pre_weight, s.post_weight, s.uf_goal, s.actual_uf,
                   s.esa_given, s.esa_drug_given, s.esa_dose_given, s.esa_route, s.esa_skip_reason,
                   s.hb_value, s.hct_value, s.notes, s.pre_hd_note,
                   p.hn, p.patient_name, p.vascular_access, p.dry_weight, p.esa_drug, p.schedule_days
            FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
            WHERE s.id = %s
        """, (session_id,))
        r = cur.fetchone()
        if not r:
            return None
        rec = {
            "session_id": r[0], "date": r[1], "shift": r[2] or "morning", "bed": r[3], "status": r[4],
            "started_at": r[5], "ended_at": r[6],
            "pre_weight": r[7], "post_weight": r[8], "uf_goal": r[9], "actual_uf": r[10],
            "esa_given": r[11], "esa_drug": r[12], "esa_dose": r[13], "esa_route": r[14], "esa_skip": r[15],
            "hb": r[16], "hct": r[17], "notes": r[18], "pre_hd_note": r[19],
            "hn": r[20], "name": r[21], "va": r[22], "dry_weight": r[23], "esa_rx": r[24], "schedule": r[25],
        }
        cur.execute("""SELECT phase::text, sbp, dbp, heart_rate, temperature, spo2, recorded_at
                       FROM hd_vital_signs WHERE session_id = %s ORDER BY recorded_at""", (session_id,))
        rec["vitals"] = cur.fetchall()
        cur.execute("""SELECT severity::text, title, message, created_at, acknowledged,
                              physician_order, order_by, order_at
                       FROM hd_clinical_alerts WHERE session_id = %s ORDER BY created_at""", (session_id,))
        rec["alerts"] = cur.fetchall()
    try:
        rec["sheet"], _ = flowsheet_entries(conn, session_id)
    except Exception as e:
        logger.warning("daily_records: flowsheet for %s failed: %s", session_id, e)
        try:
            conn.rollback()
        except Exception:
            pass
        rec["sheet"] = []
    if rec["actual_uf"] is None and rec["pre_weight"] and rec["post_weight"]:
        rec["actual_uf"] = round((float(rec["pre_weight"]) - float(rec["post_weight"])) * 1000)
    return rec


# ── PDF builders ──────────────────────────────────────────

def session_pdf(rec, clinic_name="คลินิกระนอง รักษ์ไต"):
    """Page 1 = summary, page 2 = Monitoring Sheet. Returns bytes."""
    register_fonts()
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    S = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"HD record {rec['hn']} {rec['date']}", author=clinic_name)
    W = A4[0] - 28 * mm
    el = []
    shift_th = SHIFT_TH.get(rec["shift"], rec["shift"])
    el.append(Paragraph(f"บันทึกการฟอกเลือด (Hemodialysis Record) — {clinic_name}", S["h1"]))
    el.append(Paragraph(f"วันที่ {thai_date_long(rec['date'])} · รอบ{shift_th} · เตียง {rec['bed']} · "
                        f"สถานะ {STATUS_TH.get(rec['status'], rec['status'])}", S["muted"]))
    el.append(Spacer(1, 4))

    # Patient block
    el.append(_table([
        ["ผู้ป่วย", f"{rec['name']}", "HN", f"{rec['hn']}"],
        ["Vascular access", rec["va"] or "-", "ตารางฟอก", rec["schedule"] or "-"],
        ["เวลาเริ่ม – สิ้นสุด", f"{thai_time(rec['started_at'])} – {thai_time(rec['ended_at'])}",
         "ระยะเวลา",
         (f"{(rec['ended_at'] - rec['started_at']).total_seconds() / 3600:.1f} ชม."
          if rec["started_at"] and rec["ended_at"] else "-")],
    ], [W * 0.18, W * 0.37, W * 0.15, W * 0.30], header=False, zebra=False, font_size=9))

    # Weights / UF / BP
    el.append(Paragraph("น้ำหนัก · UF · ความดัน", S["h2"]))
    pre_bp = next((f"{v[1]}/{v[2]}" for v in rec["vitals"] if v[1]), "-")
    post_bp = next((f"{v[1]}/{v[2]}" for v in reversed(rec["vitals"]) if v[1]), "-")
    min_sbp = min((v[1] for v in rec["vitals"] if v[1]), default=None)
    idwg = (float(rec["pre_weight"]) - float(rec["dry_weight"])) if rec["pre_weight"] and rec["dry_weight"] else None
    el.append(_table([
        ["นน.ก่อนฟอก", "นน.หลังฟอก", "Dry weight", "IDWG", "UF เป้า", "UF จริง", "BP ก่อน", "BP หลัง", "SBP ต่ำสุด"],
        [_fmt(rec["pre_weight"], 1, " kg"), _fmt(rec["post_weight"], 1, " kg"), _fmt(rec["dry_weight"], 1, " kg"),
         _fmt(idwg, 1, " kg"), _fmt(rec["uf_goal"], 0, " mL"), _fmt(rec["actual_uf"], 0, " mL"),
         pre_bp, post_bp, _fmt(min_sbp)],
    ], [W / 9] * 9, font_size=8.5))

    # ESA / labs
    el.append(Paragraph("ยากระตุ้นเม็ดเลือดแดง (ESA) · ผลแล็บ", S["h2"]))
    if rec["esa_given"] is True:
        esa = f"ฉีด {rec['esa_drug'] or 'ESA'} {rec['esa_dose'] or ''} {rec['esa_route'] or ''}".strip()
    elif rec["esa_given"] is False:
        esa = f"งด — {rec['esa_skip'] or 'ไม่ระบุเหตุผล'}"
    else:
        esa = "ไม่ได้บันทึก"
    el.append(_table([
        ["ESA สั่ง", rec["esa_rx"] or "-", "ESA ครั้งนี้", esa],
        ["Hb", _fmt(rec["hb"], 1, " g/dL"), "Hct", _fmt(rec["hct"], 1, " %")],
    ], [W * 0.15, W * 0.30, W * 0.15, W * 0.40], header=False, zebra=False, font_size=9))

    # Vitals
    el.append(Paragraph(f"Vital signs ({len(rec['vitals'])} ครั้ง)", S["h2"]))
    if rec["vitals"]:
        rows = [["เวลา", "ระยะ", "BP", "HR", "Temp", "SpO2"]]
        ph = {"pre": "ก่อน", "intra": "ระหว่าง", "post": "หลัง"}
        for v in rec["vitals"]:
            rows.append([thai_time(v[6]), ph.get(v[0], v[0] or "-"),
                         f"{v[1]}/{v[2]}" if v[1] else "-", _fmt(v[3]), _fmt(v[4], 1), _fmt(v[5])])
        el.append(_table(rows, [W * 0.14, W * 0.16, W * 0.2, W * 0.15, W * 0.15, W * 0.2]))
    else:
        el.append(Paragraph("ไม่มีการบันทึก", S["muted"]))

    # Alerts & physician orders
    el.append(Paragraph(f"การแจ้งเตือนและคำสั่งแพทย์ ({len(rec['alerts'])} รายการ)", S["h2"]))
    if rec["alerts"]:
        rows = [["เวลา", "ระดับ", "รายการ", "คำสั่งแพทย์", "รับทราบ"]]
        sev = {"critical": "วิกฤต", "warning": "เฝ้าระวัง", "info": "ข้อมูล"}
        for a in rec["alerts"]:
            order = a[5] or "-"
            if a[5]:
                order = f"{a[5]} ({a[6] or ''} {thai_time(a[7])})"
            rows.append([thai_time(a[3]), sev.get(a[0], a[0] or "-"),
                         Paragraph(a[1] or a[2] or "-", S["cell"]), Paragraph(order, S["cell"]),
                         "รับทราบ" if a[4] else "-"])
        el.append(_table(rows, [W * 0.1, W * 0.12, W * 0.36, W * 0.32, W * 0.1]))
    else:
        el.append(Paragraph("ไม่มีการแจ้งเตือนระหว่างฟอก", S["muted"]))

    if rec["pre_hd_note"] or rec["notes"]:
        el.append(Paragraph("หมายเหตุ", S["h2"]))
        if rec["pre_hd_note"]:
            el.append(Paragraph("ก่อนฟอก: " + str(rec["pre_hd_note"]), S["base"]))
        if rec["notes"]:
            el.append(Paragraph("หลังฟอก: " + str(rec["notes"]), S["base"]))

    el.append(Spacer(1, 10))
    el.append(Paragraph(f"พิมพ์จาก HD Smart Monitoring เมื่อ {thai_date_long(_thai_today())} "
                        f"{thai_time(datetime.now(timezone.utc))} น. · session #{rec['session_id']}", S["muted"]))

    # ── Page 2: Monitoring Sheet ──
    el.append(PageBreak())
    el.append(Paragraph(f"HD Monitoring Sheet — {rec['name']} (HN {rec['hn']}) · "
                        f"{thai_date_long(rec['date'])} รอบ{shift_th} เตียง {rec['bed']}", S["h1"]))
    el.append(Paragraph(f"UF goal {_fmt(rec['uf_goal'], 0, ' mL')} · VA {rec['va'] or '-'}", S["muted"]))
    el.append(Spacer(1, 4))
    hdr = ["เวลา", "BP", "PR", "RR", "Temp", "SpO2", "BFR", "VP", "TMP", "AP", "Cond", "Dial °C", "UFR", "UF รวม", "หมายเหตุ"]
    rows = [hdr]
    for e in rec["sheet"]:
        rows.append([
            e.get("slot_time") or "-", f"{e['sbp']}/{e['dbp']}" if e.get("sbp") else "-",
            _fmt(e.get("pr")), _fmt(e.get("rr")), _fmt(e.get("temp"), 1), _fmt(e.get("spo2")),
            _fmt(e.get("bfr")), _fmt(e.get("vp")), _fmt(e.get("tmp")), _fmt(e.get("ap")),
            _fmt(e.get("conductivity"), 1), _fmt(e.get("dialysate_temp"), 1),
            _fmt(e.get("ufr")), _fmt(e.get("total_uf")),
            Paragraph((e.get("remark") or "") + (" *" if e.get("synced") else ""), S["small"]),
        ])
    if len(rows) == 1:
        rows.append(["-"] * 15)
    cw = [W * x for x in (0.07, 0.085, 0.05, 0.045, 0.06, 0.055, 0.055, 0.05, 0.05, 0.05, 0.055, 0.06, 0.055, 0.065, 0.165)]
    el.append(_table(rows, cw, font_size=7.6))
    el.append(Spacer(1, 6))
    el.append(Paragraph("* = ค่าที่บันทึกจากหน้า session/ปุ่มวัด BP (รวมเข้าช่อง 30 นาทีอัตโนมัติ) · "
                        "ลงชื่อพยาบาล ________________  แพทย์ ________________", S["muted"]))
    doc.build(el)
    return buf.getvalue()


def shift_pdf(day, shift, rows, clinic_name="คลินิกระนอง รักษ์ไต"):
    """Landscape table of every session of one shift (rows = daily-log dicts)."""
    register_fonts()
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    S = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=10 * mm, bottomMargin=10 * mm,
                            title=f"HD {day.isoformat()} {shift}", author=clinic_name)
    W = landscape(A4)[0] - 20 * mm
    el = [Paragraph(f"ทะเบียนฟอกเลือดประจำวัน — {clinic_name}", S["h1"]),
          Paragraph(f"วันที่ {thai_date_long(day)} · รอบ{SHIFT_TH.get(shift, shift)} · {len(rows)} ราย", S["muted"]),
          Spacer(1, 5)]
    hdr = ["เตียง", "HN", "ชื่อ-สกุล", "VA", "เวลา", "นน.ก่อน", "นน.หลัง", "UF เป้า", "UF จริง",
           "BP ก่อน", "BP หลัง", "SBP ต่ำ", "ESA", "Hb", "Vital", "Alert", "สถานะ"]
    data = [hdr]
    for r in rows:
        if r.get("esa_given") is True:
            esa = f"{r.get('esa_drug') or 'ESA'} {r.get('esa_dose') or ''}".strip()
        elif r.get("esa_given") is False:
            esa = "งด"
        else:
            esa = "-"
        data.append([
            r["bed"], r["hn"], Paragraph(r["patient_name"] or "", S["cell"]), r.get("vascular_access") or "-",
            f"{thai_time(r['started_at'])}-{thai_time(r['ended_at'])}",
            _fmt(r.get("pre_weight"), 1), _fmt(r.get("post_weight"), 1),
            _fmt(r.get("uf_goal"), 0), _fmt(r.get("actual_uf"), 0),
            r.get("pre_bp") or "-", r.get("post_bp") or "-", _fmt(r.get("min_sbp")),
            Paragraph(esa, S["cell"]), _fmt(r.get("hb"), 1), r.get("vital_count", 0),
            (f"{r.get('alert_count')}" + ("!" if r.get("critical_count") else "")) if r.get("alert_count") else "-",
            STATUS_TH.get(r["status"], r["status"]),
        ])
    cw = [W * x for x in (0.04, 0.06, 0.16, 0.05, 0.085, 0.05, 0.05, 0.05, 0.05, 0.06, 0.06, 0.05, 0.09, 0.04, 0.04, 0.045, 0.06)]
    el.append(_table(data, cw, font_size=7.8))
    done = [r for r in rows if r["status"] == "completed"]
    tot_uf = sum((r.get("actual_uf") or 0) for r in done)
    el.append(Spacer(1, 6))
    el.append(Paragraph(f"สรุป: เสร็จ {len(done)} ราย · ฉีด ESA {sum(1 for r in done if r.get('esa_given') is True)} ราย · "
                        f"UF รวม {tot_uf / 1000:.1f} L · มี alert {sum(1 for r in rows if r.get('alert_count'))} ราย · "
                        f"พิมพ์เมื่อ {thai_date_long(_thai_today())} {thai_time(datetime.now(timezone.utc))} น.", S["muted"]))
    doc.build(el)
    return buf.getvalue()


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


# ── Day export ────────────────────────────────────────────

def export_day(conn, day, rows, csv_bytes, upload, clinic_name="คลินิกระนอง รักษ์ไต"):
    """Render every file for one day and hand each to upload(path, data, content_type).

    rows = daily-log dicts (from blueprint._daily_log_rows). Returns a manifest
    {folder, files:[{path, bytes, ok, error}], ok, failed}.
    """
    folder = record_folder(day)
    files = []

    def put(path, data, ctype):
        entry = {"path": path, "bytes": len(data), "ok": False, "error": None}
        try:
            upload(path, data, ctype)
            entry["ok"] = True
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            logger.warning("export_day: upload %s failed: %s", path, entry["error"])
        files.append(entry)

    put(f"{folder}/HD-{day.isoformat()}.csv", csv_bytes, "text/csv; charset=utf-8")
    by_shift = {}
    for r in rows:
        by_shift.setdefault(r["shift"], []).append(r)
    for shift, srows in by_shift.items():
        try:
            put(f"{folder}/HD-{day.isoformat()}-{SHIFT_TH.get(shift, shift)}.pdf",
                shift_pdf(day, shift, srows, clinic_name), "application/pdf")
        except Exception as e:
            files.append({"path": f"{folder}/HD-{day.isoformat()}-{shift}.pdf", "bytes": 0, "ok": False,
                          "error": f"render: {e}"})
    for r in rows:
        if r["status"] not in ("completed", "cancelled", "in_progress"):
            continue
        try:
            rec = load_session_record(conn, r["session_id"])
            if not rec:
                continue
            put(f"{folder}/sessions/{session_filename(rec['hn'], rec['name'], rec['bed'], rec['shift'])}",
                session_pdf(rec, clinic_name), "application/pdf")
        except Exception as e:
            files.append({"path": f"{folder}/sessions/{r['session_id']}.pdf", "bytes": 0, "ok": False,
                          "error": f"render: {e}"})
            try:
                conn.rollback()
            except Exception:
                pass
    ok = sum(1 for f in files if f["ok"])
    return {"folder": folder, "date": day.isoformat(), "files": files, "ok": ok, "failed": len(files) - ok}


def gcs_upload_fn(storage_obj):
    """Build upload(path, data, ctype) from the server's authenticated StorageGCS
    (the one that already writes LINE documents) — never a fresh default client."""
    bucket = getattr(storage_obj, "_bucket", None)
    if bucket is None:
        return None

    def upload(path, data, ctype):
        blob = bucket.blob(path)
        blob.upload_from_string(data, content_type=ctype)
    return upload
