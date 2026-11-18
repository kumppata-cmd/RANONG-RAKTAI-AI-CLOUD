"""Hemodialysis Work Sheet PDF — ใบบันทึกรอบฟอก ตามแบบฟอร์มคลินิกระนอง รักษ์ไต.

สร้าง PDF ที่เติมข้อมูลจาก HD session จริง ลงในแบบฟอร์มมาตรฐาน
เพื่อพิมพ์ / เก็บสำเนาดิจิทัล / ส่งให้ ศ.ร.ต.

ใช้ reportlab canvas (low-level) เพื่อวาง layout ตรงตามแบบฟอร์มกระดาษ
"""
import io
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

# ── field positions & constants ──────────────────────────

_CKD_STAGE = "5"  # default for HD patients
_DURATION_HR = 4

_PRE_DX_ITEMS = [
    "Anxiety", "Sleep disturbance", "Dyspnea", "Orthopnea", "Crepitation",
    "JVP engorged", "Arrhythmia", "Fever", "Headache", "Nausea/Vomiting",
    "Anorexia", "Weakness", "Prolong bleeding", "Pale", "Edema",
    "Itching", "constipation", "Urine",
]

_VA_TYPES = {
    "AVF": "AVF", "AVG": "AVG", "DLC": "DLC",
    "PC": "Perm Cath", "Perm Cath": "Perm Cath",
    "Subclavian": "Subclavian", "Jugular": "jugular", "Femoral": "Femoral",
}

_COMPLICATION_ITEMS = [
    "Hypotension", "Hypertension", "Muscle cramp", "Chest pain",
    "Headache", "Fever/chill", "Arrhythmia", "VA problem",
    "Dialyzer", "Cardiac arrest", "DDS", "Air embolism",
    "No Complication",
]

_TECH_COMPLICATION = [
    "Blood leak", "Clot dialyzer", "clotted bloodline",
    "machine problem", ">2 needle", "No complication",
]


def _thai_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)


def _thai_date(d):
    if not d:
        return ""
    months = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
              "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    return f"{d.day} {months[d.month - 1]} {d.year + 543}"


def _thai_time(ts):
    if not ts:
        return ""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", ""))
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return (ts + timedelta(hours=7)).strftime("%H:%M")


def _f(v, nd=1):
    """Format number or return empty string."""
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.fonts import addMapping
    try:
        pdfmetrics.getFont("Loma")
    except KeyError:
        pdfmetrics.registerFont(TTFont("Loma", os.path.join(_FONT_DIR, "Loma.ttf")))
        pdfmetrics.registerFont(TTFont("Loma-Bold", os.path.join(_FONT_DIR, "Loma-Bold.ttf")))
        addMapping("Loma", 0, 0, "Loma")
        addMapping("Loma", 1, 0, "Loma-Bold")


# ── canvas helpers ───────────────────────────────────────

def _box(c, x, y, w, h, text="", font="Loma", size=7, bold=False, align="left", fill=False, bg=None):
    """Draw a cell box and optionally write text in it."""
    if bg:
        c.setFillColor(bg)
        c.rect(x, y, w, h, stroke=1, fill=1)
        c.setFillColor("black")
    else:
        c.rect(x, y, w, h, stroke=1, fill=0)
    if text:
        fn = "Loma-Bold" if bold else font
        c.setFont(fn, size)
        pad = 2
        ty = y + h / 2 - size * 0.35
        if align == "center":
            c.drawCentredString(x + w / 2, ty, str(text))
        elif align == "right":
            c.drawRightString(x + w - pad, ty, str(text))
        else:
            c.drawString(x + pad, ty, str(text))


def _checkbox(c, x, y, checked=False, label="", size=6):
    """Draw a small checkbox."""
    c.rect(x, y, 7, 7, stroke=1, fill=0)
    if checked:
        c.setFont("Loma-Bold", 7)
        c.drawString(x + 1, y + 0.5, "✓")
    if label:
        c.setFont("Loma", size)
        c.drawString(x + 9, y + 0.5, label)


def _label_value(c, x, y, label, value, label_w=60, val_w=60, size=7):
    """Draw 'Label: ____value____' pattern."""
    c.setFont("Loma", size)
    c.drawString(x, y, label)
    c.line(x + label_w, y - 1, x + label_w + val_w, y - 1)
    if value:
        c.setFont("Loma-Bold", size)
        c.drawString(x + label_w + 2, y, str(value))


# ── main builder ─────────────────────────────────────────

def build_worksheet_pdf(rec, extras=None, clinic_name="ระนอง รักษ์ไต"):
    """Build a Hemodialysis Work Sheet PDF matching the clinic's standard form.

    rec: dict from daily_records.load_session_record()
    extras: dict from blueprint._session_record_extras() (optional — adds dialyzer, heparin etc.)
    Returns PDF bytes.
    """
    _register_fonts()
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as canvasmod
    from reportlab.lib.colors import HexColor

    buf = io.BytesIO()
    W, H = A4  # 595.28 × 841.89 pt
    c = canvasmod.Canvas(buf, pagesize=A4)
    c.setTitle(f"HD Work Sheet - {rec.get('hn', '')} - {rec.get('date', '')}")
    c.setAuthor(clinic_name)

    ex = extras or {}
    LM = 20  # left margin
    RM = W - 20  # right margin
    CW = RM - LM  # content width

    # ═══ HEADER ═══
    y = H - 30
    c.setFont("Loma-Bold", 14)
    c.drawCentredString(W / 2, y, "Hemodialysis work sheet")
    y -= 14
    c.setFont("Loma", 9)
    c.drawString(LM, y, clinic_name)

    # HD No. and Date
    c.drawRightString(RM, y, f"HD No. ________    Date {_thai_date(rec.get('date'))}")

    # Patient line
    y -= 16
    c.setFont("Loma-Bold", 8)
    c.drawString(LM, y, "Patient's name")
    c.setFont("Loma", 8)
    name = rec.get("name", "")
    c.drawString(LM + 70, y, f" {name}")
    c.drawString(LM + 250, y, f"Age ______  Yrs  HN. {rec.get('hn', '')}")
    c.drawString(LM + 420, y, f"Dx CKD stage {_CKD_STAGE}")
    c.line(LM + 70, y - 1, LM + 245, y - 1)

    # ═══ PRE-DIALYSIS + PRESCRIPTION (side by side) ═══
    y -= 18
    mid_x = LM + CW * 0.42  # divider
    top_y = y

    # ── Left: Pre-dialysis assessment ──
    c.setFont("Loma-Bold", 7)
    _box(c, LM, y - 10, CW * 0.42, 10, "Pre-dialysis assesment", bold=True, size=7, bg=HexColor("#e8e8e8"))
    y_left = top_y - 12

    # Assessment items with OY/ON + weight fields
    weight_fields = {
        "Anxiety": ("Last wt", _f(None)),
        "Sleep disturbance": ("Pre BW", _f(rec.get("pre_weight"))),
        "Dyspnea": ("Dry BW", _f(rec.get("dry_weight"))),
        "Orthopnea": ("Wt Gain", _f(
            round(float(rec["pre_weight"]) - float(rec["dry_weight"]), 1)
            if rec.get("pre_weight") and rec.get("dry_weight") else None)),
        "Crepitation": ("Meal/drink", ""),
        "JVP engorged": ("Target UF", _f(rec.get("uf_goal"), 0)),
        "Arrhythmia": ("Post BW", _f(rec.get("post_weight"))),
        "Fever": ("Wt lose", _f(
            round(float(rec["pre_weight"]) - float(rec["post_weight"]), 1)
            if rec.get("pre_weight") and rec.get("post_weight") else None)),
    }

    pre_symptoms = ex.get("pre_symptoms") or ""
    c.setFont("Loma", 6)
    for i, item in enumerate(_PRE_DX_ITEMS):
        iy = y_left - i * 9
        c.drawString(LM + 2, iy, item)
        # OY/ON checkboxes
        is_yes = item.lower() in pre_symptoms.lower() if pre_symptoms else False
        _checkbox(c, LM + 68, iy - 1, checked=is_yes, label="OY", size=5.5)
        _checkbox(c, LM + 92, iy - 1, checked=not is_yes, label="ON", size=5.5)
        # Weight fields on the right of left panel
        wf = weight_fields.get(item)
        if wf:
            c.setFont("Loma", 6)
            c.drawString(LM + 112, iy, wf[0])
            c.setFont("Loma-Bold", 7)
            c.drawString(LM + 148, iy, str(wf[1]))
            c.setFont("Loma", 5.5)
            c.drawString(LM + 178, iy, "kg")
            c.setFont("Loma", 6)

    # ── Right: Hemodialysis Prescription ──
    rx_x = mid_x + 5
    rx_w = RM - rx_x
    _box(c, rx_x, top_y - 10, rx_w, 10, "Hemodialysis Prescription", bold=True, size=7, bg=HexColor("#e8e8e8"))
    y_right = top_y - 12

    dialyzer = ex.get("dialyzer_type", "") or ""
    reuse = ex.get("dialyzer_reuse", "") or ""
    hep = ex.get("heparin", {}) or {}
    hep_bolus = hep.get("bolus", "") or ""
    hep_maint = hep.get("maintenance", "") or ""

    # Machine info from equipment registry
    machine = ex.get("machine", {}) or {}
    machine_label = f"{machine.get('brand', '')} {machine.get('model', '')}".strip() if machine else ""
    machine_sn = (machine.get("serial", "") or "") if machine else ""

    rx_items = [
        ("Machine", machine_label, "S/N", machine_sn),
        ("Dialyzer", dialyzer, "Surface area", "m2"),
        ("Use No.", str(reuse) if reuse else "", "Last TCV", ""),
        (f"Loading    {hep_bolus or '____'}", "", f"Maintenance    {hep_maint or '____'}", ""),
    ]
    c.setFont("Loma", 6.5)
    for i, item in enumerate(rx_items):
        iy = y_right - i * 10
        c.drawString(rx_x + 2, iy, item[0])
        if item[1]:
            c.setFont("Loma-Bold", 7)
            c.drawString(rx_x + 55, iy, item[1])
            c.setFont("Loma", 6.5)
        c.drawString(rx_x + rx_w * 0.5, iy, item[2])
        if item[3]:
            c.drawString(rx_x + rx_w * 0.9, iy, item[3])

    # Anticoagulant
    y_right -= 50
    c.setFont("Loma-Bold", 6.5)
    c.drawString(rx_x + 2, y_right, "Anticoagulant")
    c.setFont("Loma", 6)
    hep_protocol = (hep.get("protocol", "") or "").lower()
    _checkbox(c, rx_x + 65, y_right - 1, checked="heparin" in hep_protocol and "free" not in hep_protocol, label="Heparin", size=5.5)
    _checkbox(c, rx_x + 110, y_right - 1, checked="lmwh" in hep_protocol, label="LMWH", size=5.5)
    _checkbox(c, rx_x + 150, y_right - 1, checked="free" in hep_protocol, label="Heparin free technique", size=5.5)

    # Duration, Hct, Hb
    y_right -= 12
    c.setFont("Loma", 6.5)
    c.drawString(rx_x + 2, y_right, f"Duration  {_DURATION_HR}  hr")
    c.drawString(rx_x + 60, y_right, f"Time start {_thai_time(rec.get('started_at'))}")
    c.drawString(rx_x + 140, y_right, f"Time off {_thai_time(rec.get('ended_at'))}")
    y_right -= 10
    hct = _f(rec.get("hct") or ex.get("hct_value"))
    hb = _f(rec.get("hb") or ex.get("hb_value"))
    c.drawString(rx_x + 2, y_right, f"Hct= {hct} %")
    c.drawString(rx_x + 80, y_right, f"Hb = {hb} g/dl")

    # Vascular access
    y_right -= 14
    c.setFont("Loma-Bold", 6.5)
    c.drawString(rx_x + 2, y_right, "Vascular access")
    c.setFont("Loma", 5.5)
    va = rec.get("va", "") or ex.get("va_location", "") or ""
    va_up = va.upper()
    for i, (key, label) in enumerate(_VA_TYPES.items()):
        col = i % 3
        row = i // 3
        cx = rx_x + 10 + col * 65
        cy = y_right - 11 - row * 9
        _checkbox(c, cx, cy, checked=key.upper() in va_up, label=label, size=5)

    # Dialysate section
    y_right -= 50
    c.setFont("Loma-Bold", 6.5)
    c.drawString(rx_x + 2, y_right, "Dialysate")
    c.setFont("Loma", 6)
    dial_items = [
        ("K+", "2", "mEq/L"), ("Ca", "3.5", "mEq/L"), ("Hco3", "32", "mEq/L"),
        ("Dialysate Temp", "36.5", "°C"), ("Dialysate flow", "600", "cc/min"),
    ]
    for i, (k, v, u) in enumerate(dial_items):
        dy = y_right - 10 - i * 8
        c.drawString(rx_x + 5, dy, f"{k}  {v}  {u}")

    # Medication (ESA)
    y_right -= 60
    c.setFont("Loma-Bold", 6.5)
    c.drawString(rx_x + rx_w * 0.5, y_right, "Medication")
    c.setFont("Loma", 6)
    esa_drug = ex.get("esa_drug_given") or rec.get("esa_drug") or rec.get("esa_rx") or ""
    esa_route = ex.get("esa_route") or rec.get("esa_route") or ""
    y_right -= 10
    c.drawString(rx_x + rx_w * 0.5 + 2, y_right, f"EPO: {esa_drug}")
    c.drawString(rx_x + rx_w * 0.5 + 2, y_right - 9, f"Route: {esa_route or 'IV SC'}")

    # ═══ NURSING DIAGNOSIS (narrow band) ═══
    y_nurs = min(y_left - len(_PRE_DX_ITEMS) * 9, y_right - 10) - 5
    _box(c, LM, y_nurs, CW / 3, 10, "Nursing Diagnosis", bold=True, size=6.5, bg=HexColor("#e8e8e8"))
    _box(c, LM + CW / 3, y_nurs, CW / 3, 10, "Nursing care plan", bold=True, size=6.5, bg=HexColor("#e8e8e8"))
    _box(c, LM + 2 * CW / 3, y_nurs, CW / 3, 10, "Expective outcome", bold=True, size=6.5, bg=HexColor("#e8e8e8"))

    # Pre HD Note
    y_pre = y_nurs - 15
    c.setFont("Loma-Bold", 7)
    c.drawString(LM, y_pre, "Pre HD Note")
    c.setFont("Loma", 6.5)
    pre_note = rec.get("pre_hd_note", "") or ""
    if pre_note:
        c.drawString(LM + 65, y_pre, pre_note[:80])

    # ═══ MONITORING TABLE ═══
    y_tbl = y_pre - 16
    headers = ["TIME", "BP\nmm/Hg", "PR\n/min", "RR\n/min", "BFR\nml/min", "VP\nmm/Hg",
               "TMP\nmm/Hg", "UFR\nml/hr", "Conduct\nmS/cm", "Total UF\nml", "Remark"]
    n_cols = len(headers)
    # Column widths
    cws = [CW * x for x in (0.07, 0.085, 0.055, 0.055, 0.065, 0.065, 0.065, 0.065, 0.065, 0.075, 0.23)]
    row_h = 12
    hdr_h = 18

    # Header
    c.setFont("Loma-Bold", 6)
    cx = LM
    for i, hdr in enumerate(headers):
        _box(c, cx, y_tbl - hdr_h, cws[i], hdr_h, "", bg=HexColor("#e8e8e8"))
        lines = hdr.split("\n")
        for j, line in enumerate(lines):
            c.setFont("Loma-Bold", 5.5)
            c.drawCentredString(cx + cws[i] / 2, y_tbl - 7 - j * 7, line)
        cx += cws[i]

    # Data rows: Pre-HD, Start HD, then flowsheet entries, then empty rows
    y_row = y_tbl - hdr_h
    sheet = rec.get("sheet", [])

    # Pre-HD row
    pre_v = ex.get("pre_vitals", {}) or {}
    pre_bp = f"{pre_v.get('sbp', '')}/{pre_v.get('dbp', '')}" if pre_v.get("sbp") else ""
    pre_data = ["", pre_bp, str(pre_v.get("hr", "") or ""), "", "", "", "", "", "", "", "Pre-HD"]
    y_row -= row_h
    cx = LM
    for i, val in enumerate(pre_data):
        _box(c, cx, y_row, cws[i], row_h, str(val), size=5.5, align="center" if i < 10 else "left")
        cx += cws[i]

    # Start HD row
    start_data = [_thai_time(rec.get("started_at")), "", "", "", "", "", "", "", "", "", "Start HD"]
    y_row -= row_h
    cx = LM
    for i, val in enumerate(start_data):
        _box(c, cx, y_row, cws[i], row_h, str(val), size=5.5, align="center" if i < 10 else "left")
        cx += cws[i]

    # Flowsheet entries (ค่าที่บันทึกจริง)
    for entry in sheet[:12]:  # max 12 rows
        y_row -= row_h
        if y_row < 160:  # safety margin
            break
        row_data = [
            entry.get("slot_time", "") or "",
            f"{entry.get('sbp', '')}/{entry.get('dbp', '')}" if entry.get("sbp") else "",
            _f(entry.get("pr"), 0), _f(entry.get("rr"), 0),
            _f(entry.get("bfr"), 0), _f(entry.get("vp"), 0), _f(entry.get("tmp"), 0),
            _f(entry.get("ufr"), 0), _f(entry.get("conductivity"), 1),
            _f(entry.get("total_uf"), 0),
            (entry.get("remark", "") or "")[:30],
        ]
        cx = LM
        for i, val in enumerate(row_data):
            _box(c, cx, y_row, cws[i], row_h, str(val), size=5.5, align="center" if i < 10 else "left")
            cx += cws[i]

    # Fill remaining empty rows until y_row reaches ~160
    while y_row > 170:
        y_row -= row_h
        cx = LM
        for i in range(n_cols):
            _box(c, cx, y_row, cws[i], row_h, "", size=5.5)
            cx += cws[i]

    # ═══ POST-HD SECTION ═══
    y_post = y_row - 5
    c.setFont("Loma", 6)
    c.drawRightString(RM, y_post, "Nurse Off")
    y_post -= 12

    # NSS / 50%glucose / Extra fluid / meal and drink / Total UF / Net UF balance
    post_items = ["Nss", "50%glucose", "Extra fluid", "meal and drink", "Total repletement",
                  "Total UF", "", "Net UF balance"]
    pw = CW / len(post_items)
    for i, item in enumerate(post_items):
        _box(c, LM + i * pw, y_post, pw, 10, item, size=5.5, bold=bool(item))
    y_post -= 10
    actual_uf = _f(rec.get("actual_uf"), 0) if rec.get("actual_uf") else ""
    net_uf = ""
    if rec.get("actual_uf") is not None:
        net_uf = _f(rec["actual_uf"], 0)
    post_vals = ["ml", "ml", "ml", "ml", "ml", f"{actual_uf} ml", "", f"{net_uf} ml"]
    for i, val in enumerate(post_vals):
        _box(c, LM + i * pw, y_post, pw, 10, val, size=5.5, align="center")

    # ═══ BOTTOM SECTION: Complication / Doctor note / Signatures ═══
    y_bot = y_post - 15
    third = CW / 3

    # Complication
    c.setFont("Loma-Bold", 6)
    _box(c, LM, y_bot, third, 10, "Complication", bold=True, size=6, bg=HexColor("#e8e8e8"))
    c.setFont("Loma", 5.5)
    for i, item in enumerate(_COMPLICATION_ITEMS[:8]):
        cy = y_bot - 10 - i * 8
        _checkbox(c, LM + 3, cy, label=item, size=5)

    # Doctor note
    _box(c, LM + 2 * third, y_bot, third, 10, "Docter note", bold=True, size=6, bg=HexColor("#e8e8e8"))
    doctor_items = ["ไข้", "เหนื่อย", "แน่นหน้าอก", "ปวดหัว", "ตะคริว"]
    post_symptoms = ex.get("post_symptoms", "") or ""
    c.setFont("Loma", 6)
    for i, item in enumerate(doctor_items):
        cy = y_bot - 10 - i * 10
        c.drawString(LM + 2 * third + 5, cy, f"- {item}")
        has = item.lower() in post_symptoms.lower() if post_symptoms else False
        _checkbox(c, LM + 2 * third + 55, cy - 1, checked=not has, label="ไม่มี", size=5)
        _checkbox(c, LM + 2 * third + 95, cy - 1, checked=has, label="มี", size=5)

    # Post HD note
    post_note_y = y_bot - 10 - len(doctor_items) * 10 - 5
    c.setFont("Loma-Bold", 6)
    c.drawString(LM + 2 * third + 5, post_note_y, "Post HD note")
    post_hd_items = ["เหนื่อย", "เลือดออก"]
    c.setFont("Loma", 6)
    for i, item in enumerate(post_hd_items):
        cy = post_note_y - 10 - i * 10
        c.drawString(LM + 2 * third + 5, cy, item)
        _checkbox(c, LM + 2 * third + 55, cy - 1, checked=False, label="ไม่มี", size=5)
        _checkbox(c, LM + 2 * third + 95, cy - 1, checked=False, label="มี", size=5)

    # BP before discharge
    post_v = ex.get("post_vitals", {}) or {}
    post_bp = f"{post_v.get('sbp', '')}/{post_v.get('dbp', '')}" if post_v.get("sbp") else "____/____"
    bp_y = post_note_y - 35
    c.setFont("Loma", 6.5)
    c.drawString(LM + 2 * third + 5, bp_y, f"BP ก่อนกลับ  {post_bp}  mm/Hg")

    # Discharge
    bp_y -= 12
    c.drawString(LM + 2 * third + 5, bp_y, "Discharge by")
    _checkbox(c, LM + 2 * third + 60, bp_y - 1, checked=True, label="Home", size=5.5)

    # ═══ SIGNATURES ═══
    sig_y = 30
    c.setFont("Loma", 7)
    c.drawString(LM, sig_y, f"Nephrologist  __________________________")
    nurse_name = ex.get("nurse_name", "") or ""
    c.drawString(LM + CW * 0.45, sig_y, f"Nurse  {nurse_name}  __________________________")

    # Footer
    c.setFont("Loma", 5)
    c.drawRightString(RM, 15, f"พิมพ์จาก HD Smart Monitoring · {_thai_date(_thai_now().date())} {_thai_time(_thai_now())}")

    c.showPage()
    c.save()
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════
# แบบ A: Overlay บน PDF ต้นฉบับ
# ═══════════════════════════════════════════════════════════

_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "hd_worksheet_template.pdf")

def _build_overlay(rec, extras=None):
    """สร้าง transparent PDF overlay — พิกัดวัดจาก grid 20pt บน template จริง.
    PDF coordinate: origin = bottom-left, y increases upward."""
    _register_fonts()
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as canvasmod

    buf = io.BytesIO()
    W, H = A4  # 595.28 x 841.89
    c = canvasmod.Canvas(buf, pagesize=A4)
    ex = extras or {}

    # ── Helpers ──
    def txt(x, y, text, size=7, bold=False):
        if not text and text != 0:
            return
        c.setFont("Loma-Bold" if bold else "Loma", size)
        c.drawString(x, y, str(text))

    def txtr(x, y, text, size=7, bold=False):
        """Right-aligned text."""
        if not text and text != 0:
            return
        c.setFont("Loma-Bold" if bold else "Loma", size)
        c.drawRightString(x, y, str(text))

    def tick(x, y):
        c.setFont("Loma-Bold", 9)
        c.drawString(x, y, "✓")

    # ════════════════════════════════════════════════════════
    # HEADER ROW — จาก grid: y≈805 (ชื่อคลินิก), y≈790 (Patient's name)
    # ════════════════════════════════════════════════════════
    # Patient's name: x≈108, y≈790
    txt(108, 790, rec.get("name", ""), size=8, bold=True)
    # Age: หลังคำว่า Age — x≈340, y≈790
    age = rec.get("age")
    txt(340, 790, str(age) if age else "", size=8)
    # Yrs: x≈362
    # HN: หลังคำว่า HN — x≈400, y≈790
    txt(400, 790, rec.get("hn", ""), size=8, bold=True)
    # Dx CKD stage 5: x≈490, y≈790
    txt(492, 790, "5", size=7)
    # HD No.: x≈550, y≈805 (บรรทัดบนสุดขวา)
    hd_no = rec.get("hd_no")
    txt(555, 805, str(hd_no) if hd_no else "", size=8, bold=True)
    # Date: x≈550, y≈790
    txt(545, 790, _thai_date(rec.get("date")), size=7)
    # Shift (รอบเช้า/บ่าย/ค่ำ): x≈490, y≈805
    shift_th = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}.get(rec.get("shift", ""), "")
    txt(490, 805, f"รอบ{shift_th}", size=7)

    # ════════════════════════════════════════════════════════
    # PRE-DIALYSIS ASSESSMENT — ฝั่งซ้าย
    # grid: แถว Last wt y≈768, Pre BW y≈756, Dry BW y≈744 ...
    # ค่า kg อยู่หลังช่อง OY ON ≈ x=228
    # ════════════════════════════════════════════════════════
    pre_w = rec.get("pre_weight")
    post_w = rec.get("post_weight")
    dw = rec.get("dry_weight")

    # Last wt: y≈770
    prev_post = rec.get("prev_post_weight") or ex.get("prev_post_weight")
    txtr(258, 770, _f(prev_post), size=7)
    # Pre BW: y≈758
    txtr(258, 758, _f(pre_w), size=7, bold=True)
    # Dry BW: y≈746
    txtr(258, 746, _f(dw), size=7, bold=True)
    # Wt Gain (pre - dw): y≈722
    if pre_w and dw:
        txtr(258, 722, _f(round(float(pre_w) - float(dw), 1)), size=7)
    # Meal/drink: y≈710 — skip
    # Target UF: y≈698
    txtr(258, 698, _f(rec.get("uf_goal"), 0), size=7)
    # Post BW: y≈686
    txtr(258, 686, _f(post_w), size=7, bold=True)
    # Wt lose (pre - post): y≈674
    if pre_w and post_w:
        txtr(258, 674, _f(round(float(pre_w) - float(post_w), 1)), size=7)

    # ════════════════════════════════════════════════════════
    # HEMODIALYSIS PRESCRIPTION — ฝั่งขวา
    # grid: Machine y≈768, Dialyzer y≈756, Surface area y≈744 ...
    # ════════════════════════════════════════════════════════
    machine = ex.get("machine", {}) or {}
    # Machine: หลังคำว่า Machine x≈350, y≈772
    machine_label = f"{machine.get('brand', '')} {machine.get('model', '')}".strip() if machine else ""
    txt(350, 772, machine_label, size=6, bold=True)
    # No. (serial): หลัง No. x≈432, y≈772
    txt(432, 772, machine.get("serial", "") if machine else "", size=5.5)
    # Dialyzer: x≈350, y≈760
    dialyzer = ex.get("dialyzer_type", "") or ""
    txt(350, 760, dialyzer, size=6, bold=True)
    # Surface area / m2: y≈748 — skip
    # Use No. (reuse): y≈736
    reuse = ex.get("dialyzer_reuse")
    txt(350, 736, str(reuse) if reuse else "", size=7)

    # Anticoagulant checkboxes: y≈768 area ฝั่งขวาสุด
    hep = ex.get("heparin", {}) or {}
    hep_protocol = (hep.get("protocol", "") or "").lower()

    # Duration: ช่อง Duration x≈560, y≈772
    duration = rec.get("duration_hr") or _DURATION_HR
    txt(560, 772, str(duration), size=7)
    # Time start: x≈548, y≈760
    txt(548, 760, _thai_time(rec.get("started_at")), size=6)
    # Time off: x≈548, y≈748
    txt(548, 748, _thai_time(rec.get("ended_at")), size=6)

    # Heparin checkbox: ☐ Heparin y≈760
    if "heparin" in hep_protocol and "free" not in hep_protocol:
        tick(468, 760)
    elif "lmwh" in hep_protocol:
        tick(468, 748)

    # Loading: x≈450, y≈712
    txt(450, 712, str(hep.get("bolus", "")) or "", size=7)
    # Maintenance: x≈450, y≈700
    txt(450, 700, str(hep.get("maintenance", "")) or "", size=7)

    # No Heparin free technique checkbox: y≈688
    if "free" in hep_protocol:
        tick(468, 688)

    # Hct=: x≈560, y≈700
    hct = rec.get("hct") or ex.get("hct_value")
    hb = rec.get("hb") or ex.get("hb_value")
    txt(560, 700, _f(hct), size=7, bold=True)
    # Hb=: x≈560, y≈688
    txt(560, 688, _f(hb), size=7, bold=True)

    # ════════════════════════════════════════════════════════
    # VASCULAR ACCESS — y≈652 area
    # ════════════════════════════════════════════════════════
    va = (rec.get("va", "") or "").upper()
    # O AVF y≈630, O AVG same line
    if "AVF" in va:
        tick(305, 630)
    elif "AVG" in va:
        tick(360, 630)
    # O DLC y≈618, O Perm Cath same line
    elif "DLC" in va:
        tick(305, 618)
    elif "PERM" in va or "PC" in va:
        tick(370, 618)

    # ════════════════════════════════════════════════════════
    # MEDICATION (EPO) — y≈632 ฝั่งขวา
    # ════════════════════════════════════════════════════════
    esa_drug = ex.get("esa_drug_given") or rec.get("esa_drug") or rec.get("esa_rx") or ""
    esa_route = ex.get("esa_route") or rec.get("esa_route") or ""
    # Medication EPO: x≈470, y≈632
    txt(470, 632, esa_drug, size=6)
    # Route: x≈545, y≈632
    if "IV" in esa_route.upper():
        txt(545, 632, "IV", size=6, bold=True)
    elif "SC" in esa_route.upper():
        txt(560, 632, "SC", size=6, bold=True)

    # ════════════════════════════════════════════════════════
    # STICKER EPO — x≈550, y≈510 area (ขวาบน Nursing Diagnosis)
    # ════════════════════════════════════════════════════════
    # Sticker EPO label already on form

    # ════════════════════════════════════════════════════════
    # PRE HD NOTE — y≈472
    # ════════════════════════════════════════════════════════
    pre_note = rec.get("pre_hd_note", "") or ""
    txt(80, 472, pre_note[:70], size=6)

    # ════════════════════════════════════════════════════════
    # MONITORING TABLE
    # จาก grid: header TIME/BP/PR... y≈448
    # Pre-HD row y≈435, Start HD y≈420
    # แถวข้อมูล: y ลดทีละ ~15pt
    # คอลัมน์ x จาก grid:
    #   TIME≈22, BP≈72, PR≈122, RR≈152, BFR≈182
    #   VP≈222, TMP≈262, UFR≈302, Conduct≈352, TotalUF≈412, Remark≈475
    # ════════════════════════════════════════════════════════
    sheet = rec.get("sheet", [])
    # column x positions (left edge of each column)
    COL_TIME = 22
    COL_BP = 73
    COL_PR = 127
    COL_RR = 157
    COL_BFR = 190
    COL_VP = 230
    COL_TMP = 270
    COL_UFR = 318
    COL_COND = 360
    COL_TUF = 420
    COL_REM = 478
    ROW_H = 15.0
    SZ = 5.5  # font size for table

    # Pre-HD row: y≈406
    y_pre = 406
    pre_v = ex.get("pre_vitals", {}) or {}
    if pre_v.get("sbp"):
        txt(COL_BP, y_pre, f"{pre_v['sbp']}/{pre_v.get('dbp','')}", size=SZ)
        txt(COL_PR, y_pre, str(pre_v.get("hr", "")), size=SZ)

    # Start HD row: y≈391
    y_start_hd = y_pre - ROW_H
    txt(COL_TIME, y_start_hd, _thai_time(rec.get("started_at")), size=SZ)

    # Data rows: เริ่มหลัง Start HD
    for i, entry in enumerate(sheet[:12]):
        y_row = y_start_hd - (i + 1) * ROW_H
        if y_row < 275:
            break
        txt(COL_TIME, y_row, entry.get("slot_time", ""), size=SZ)
        if entry.get("sbp"):
            txt(COL_BP, y_row, f"{entry['sbp']}/{entry.get('dbp','')}", size=SZ)
        txt(COL_PR, y_row, _f(entry.get("pr"), 0), size=SZ)
        txt(COL_RR, y_row, _f(entry.get("rr"), 0), size=SZ)
        txt(COL_BFR, y_row, _f(entry.get("bfr"), 0), size=SZ)
        txt(COL_VP, y_row, _f(entry.get("vp"), 0), size=SZ)
        txt(COL_TMP, y_row, _f(entry.get("tmp"), 0), size=SZ)
        txt(COL_UFR, y_row, _f(entry.get("ufr"), 0), size=SZ)
        txt(COL_COND, y_row, _f(entry.get("conductivity"), 1), size=SZ)
        txt(COL_TUF, y_row, _f(entry.get("total_uf"), 0), size=SZ)
        remark = (entry.get("remark", "") or "")[:25]
        if remark:
            txt(COL_REM, y_row, remark, size=4.5)

    # ════════════════════════════════════════════════════════
    # POST-HD SECTION
    # Nurse Off: y≈270
    # Total repletcement / Total UF / Net UF balance: y≈253
    # ════════════════════════════════════════════════════════
    actual_uf = rec.get("actual_uf")
    if actual_uf is not None:
        # Total UF: x≈455, y≈253
        txt(458, 253, str(int(actual_uf)), size=6, bold=True)
        # Net UF balance: x≈540, y≈253
        txt(540, 253, str(int(actual_uf)), size=6, bold=True)

    # ════════════════════════════════════════════════════════
    # BP ก่อนกลับ: y≈75 area
    # ════════════════════════════════════════════════════════
    post_v = ex.get("post_vitals", {}) or {}
    if post_v.get("sbp"):
        # BP ก่อนกลับ: x≈430, y≈72
        txt(432, 72, f"{post_v['sbp']}/{post_v.get('dbp','')}", size=7, bold=True)

    # ════════════════════════════════════════════════════════
    # DISCHARGE BY: y≈40
    # ════════════════════════════════════════════════════════
    # ( ) Home: x≈410, y≈40
    tick(412, 38)

    # ════════════════════════════════════════════════════════
    # SIGNATURES — y≈18 (bottom)
    # ════════════════════════════════════════════════════════
    # Nephrologist: x≈85, y≈18 — มีชื่อแพทย์อยู่แล้วในฟอร์ม
    # Nurse: x≈370, y≈18
    nurse = ex.get("nurse_name", "") or ""
    txt(370, 18, nurse, size=7)

    c.showPage()
    c.save()
    return buf.getvalue()


def build_overlay_pdf(rec, extras=None):
    """สร้าง PDF แบบ A: overlay ข้อมูลบน template ฟอร์มจริง. Returns bytes."""
    from pypdf import PdfReader, PdfWriter

    # 1. สร้าง overlay (ตัวอักษร/ติ๊กเท่านั้น บน transparent background)
    overlay_bytes = _build_overlay(rec, extras)

    # 2. Merge overlay ทับ template
    template = PdfReader(_TEMPLATE_PATH)
    overlay = PdfReader(io.BytesIO(overlay_bytes))

    writer = PdfWriter()
    page = template.pages[0]
    page.merge_page(overlay.pages[0])
    writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def load_and_build(conn, session_id, clinic_name="ระนอง รักษ์ไต", mode="overlay"):
    """Load session data from DB and build the work sheet PDF.

    mode='overlay' (default) = แบบ A — เติมข้อมูลบน template ฟอร์มจริง
    mode='draw'              = แบบ B — วาด layout ใหม่ทั้งหมด
    Returns (bytes, filename) or (None, None).
    """
    from .daily_records import load_session_record, SHIFT_TH, safe_name
    from .blueprint import _session_record_extras

    rec = load_session_record(conn, session_id)
    if not rec:
        return None, None

    # Load extras (dialyzer, heparin, VA, nurse name, pre/post vitals, machine)
    extras = {}
    try:
        with conn.cursor() as cur:
            pid = rec.get("patient_id")
            if not pid:
                cur.execute("SELECT patient_id FROM hd_sessions WHERE id = %s", (session_id,))
                pid_row = cur.fetchone()
                pid = pid_row[0] if pid_row else None
            if pid:
                extras = _session_record_extras(cur, session_id, pid)
    except Exception as e:
        logger.warning("hd_worksheet extras: %s", e)

    if mode == "overlay" and os.path.exists(_TEMPLATE_PATH):
        try:
            pdf = build_overlay_pdf(rec, extras)
        except Exception as e:
            logger.warning("overlay PDF failed (%s) — fallback to draw mode", e)
            pdf = build_worksheet_pdf(rec, extras, clinic_name)
    else:
        pdf = build_worksheet_pdf(rec, extras, clinic_name)

    shift_th = SHIFT_TH.get(rec["shift"], rec["shift"])
    fname = f"HD-WorkSheet-{safe_name(rec['hn'], 12)}-{safe_name(rec['name'])}-{rec['date'].isoformat()}-{shift_th}.pdf"
    return pdf, fname
