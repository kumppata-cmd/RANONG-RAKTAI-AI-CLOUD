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
    """Build HD Work Sheet PDF — แบบ B: วาด layout เหมือนฟอร์มกระดาษจริง 100%.

    ข้อมูลตรงช่องทุกจุดเพราะช่องกับตัวเลขสร้างพร้อมกัน
    """
    _register_fonts()
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as canvasmod
    from reportlab.lib.colors import HexColor

    buf = io.BytesIO()
    W, H = A4
    c = canvasmod.Canvas(buf, pagesize=A4)
    c.setTitle(f"HD Work Sheet - {rec.get('hn', '')} - {rec.get('date', '')}")

    ex = extras or {}
    LM, RM = 15, W - 15
    CW = RM - LM
    GRAY = HexColor("#e0e0e0")
    RH = 11  # row height for assessment/prescription

    # ── helpers ──
    def cell(x, y, w, h, text="", sz=6, bold=False, align="left", bg=None):
        """Draw a bordered cell with text."""
        if bg:
            c.setFillColor(bg)
            c.rect(x, y, w, h, fill=1, stroke=0)
        c.setStrokeColor(HexColor("#000000"))
        c.setLineWidth(0.3)
        c.rect(x, y, w, h, fill=0, stroke=1)
        if text:
            c.setFillColor(HexColor("#000000"))
            c.setFont("Loma-Bold" if bold else "Loma", sz)
            if align == "center":
                c.drawCentredString(x + w / 2, y + (h - sz) / 2, str(text))
            elif align == "right":
                c.drawRightString(x + w - 2, y + (h - sz) / 2, str(text))
            else:
                c.drawString(x + 2, y + (h - sz) / 2, str(text))

    def label_val(x, y, w, h, label, val, lw=55, sz=6, val_bold=True):
        """Label on left, value on right inside one cell."""
        cell(x, y, w, h)
        c.setFont("Loma", sz)
        c.drawString(x + 2, y + (h - sz) / 2, label)
        c.setFont("Loma-Bold" if val_bold else "Loma", sz + 1)
        c.drawRightString(x + w - 3, y + (h - sz) / 2, str(val) if val else "")

    # ════════════════════════════════════════════════════════════
    # HEADER
    # ════════════════════════════════════════════════════════════
    y = H - 25
    c.setFont("Loma-Bold", 13)
    c.drawCentredString(W / 2, y, "Hemodialysis work sheet")
    y -= 15
    c.setFont("Loma", 8)
    c.drawString(LM, y, clinic_name)
    hd_no = rec.get("hd_no")
    c.setFont("Loma-Bold", 10)
    c.drawRightString(RM - 80, y, f"HD No.  {hd_no or '____'}")
    c.drawRightString(RM, y, f"{_thai_date(rec.get('date'))}")

    # Patient line
    y -= 16
    age = rec.get("age") or ""
    hn = rec.get("hn", "")
    name = rec.get("name", "")
    c.setFont("Loma-Bold", 8)
    c.drawString(LM, y, "Patient's name")
    c.setFont("Loma-Bold", 10)
    c.drawString(LM + 72, y - 1, name)
    c.setFont("Loma-Bold", 10)
    c.drawString(LM + 240, y - 1, f"Age  {age}")
    c.drawString(LM + 300, y - 1, f"Yrs  HN. {hn}")
    c.setFont("Loma", 8)
    c.drawString(LM + 420, y, f"Dx CKD stage {_CKD_STAGE}")

    # ════════════════════════════════════════════════════════════
    # PRE-DIALYSIS ASSESSMENT (left) + PRESCRIPTION (right)
    # ════════════════════════════════════════════════════════════
    y -= 14
    LEFT_W = CW * 0.44  # left panel width
    RX_X = LM + LEFT_W  # right panel start
    RX_W = CW - LEFT_W  # right panel width

    # Header bars
    cell(LM, y - RH, LEFT_W, RH, "Pre-dialysis assesment", sz=7, bold=True, align="center", bg=GRAY)
    cell(RX_X, y - RH, RX_W, RH, "Hemodialysis Prescription", sz=7, bold=True, align="center", bg=GRAY)

    top_y = y - RH

    # ── LEFT: Assessment rows ──
    # Weight data
    pre_w = rec.get("pre_weight")
    post_w = rec.get("post_weight")
    dw = rec.get("dry_weight")
    wt_gain = round(float(pre_w) - float(dw), 1) if pre_w and dw else None
    wt_lose = round(float(pre_w) - float(post_w), 1) if pre_w and post_w else None
    prev_post = rec.get("prev_post_weight") or ex.get("prev_post_weight")

    weight_map = {
        0: ("Last wt", _f(prev_post), "kg"),
        1: ("Pre BW", _f(pre_w), "kg"),
        2: ("Dry BW", _f(dw), "kg"),
        3: ("Wt Gain", _f(wt_gain), "kg"),
        4: ("Meal/drink", "", "kg"),
        5: ("Target UF", _f(rec.get("uf_goal"), 0), "kg"),
        6: ("Post BW", _f(post_w), "kg"),
        7: ("Wt lose", _f(wt_lose), "kg"),
    }

    assess_x = LM
    oy_x = LM + 70  # OY column
    on_x = oy_x + 18  # ON column
    wt_lbl_x = on_x + 18  # weight label
    wt_val_x = wt_lbl_x + 50  # weight value
    kg_x = wt_val_x + 32  # kg

    for i, item in enumerate(_PRE_DX_ITEMS):
        iy = top_y - i * RH
        # Assessment name
        cell(assess_x, iy - RH, 70, RH, item, sz=5.5)
        # OY / ON
        cell(oy_x, iy - RH, 18, RH, "OY", sz=5, align="center")
        cell(on_x, iy - RH, 18, RH, "ON", sz=5, align="center")
        # Weight field
        wf = weight_map.get(i)
        if wf:
            cell(wt_lbl_x, iy - RH, 50, RH, wf[0], sz=5.5)
            cell(wt_val_x, iy - RH, 32, RH, str(wf[1]) if wf[1] else "", sz=7, bold=True, align="center")
            cell(kg_x, iy - RH, 18, RH, wf[2], sz=5, align="center")
        else:
            cell(wt_lbl_x, iy - RH, 100, RH)  # empty weight area

    left_bottom = top_y - len(_PRE_DX_ITEMS) * RH

    # ── RIGHT: Prescription rows ──
    machine = ex.get("machine", {}) or {}
    machine_label = f"{machine.get('brand', '')} {machine.get('model', '')}".strip() if machine else ""
    machine_sn = machine.get("serial", "") if machine else ""
    dialyzer = ex.get("dialyzer_type", "") or ""
    reuse = ex.get("dialyzer_reuse")
    hep = ex.get("heparin", {}) or {}
    hep_bolus = hep.get("bolus", "")
    hep_maint = hep.get("maintenance", "")
    hep_protocol = (hep.get("protocol", "") or "").lower()
    duration = rec.get("duration_hr") or _DURATION_HR

    # Row definitions: (label, value, label2, value2)
    rx_half = RX_W / 2
    ry = top_y

    # Row 0: Machine | Anticoagulant | Duration
    rx_rows = [
        # (left_label, left_val, right_label, right_val)
        ("Machine", f"{machine_label}", "Anticoagulant", ""),
        ("Dialyzer", dialyzer, "☐ Heparin", ""),
        ("Surface area", "", "☐ LMWH", ""),
        ("Use No.", str(reuse) if reuse else "", "☐ Other", ""),
        ("Last TCV", "", f"Loading  {hep_bolus or '____'}  start", ""),
        ("เปิดใช้เมื่อ", "", f"Maintenance  {hep_maint or '____'}  In  hr", ""),
        ("Tesk leak  O Y  O N", "", "☐ No Heparin free technique", ""),
        ("☐ Disinfectant ☐ Paracetic", "", "☐ Heparin", ""),
        ("☐ Formalin ☐ Other", "", f"☐ Flush Nss  cc q  min", ""),
    ]

    # Duration column (far right)
    dur_w = 80
    rx_mid = RX_W - dur_w
    dur_x = RX_X + rx_mid

    dur_rows = [
        f"Duration  {duration}  hr",
        f"Time start  {_thai_time(rec.get('started_at'))}",
        f"Time off  {_thai_time(rec.get('ended_at'))}",
        "O2 sat            %",
        f"BT  {_f(rec.get('bt') or ex.get('bt_value'))}  'C",
        f"Hct=  {_f(rec.get('hct') or ex.get('hct_value'))}  %",
        f"Hb =  {_f(rec.get('hb') or ex.get('hb_value'))}  g/dl",
    ]

    for i, row in enumerate(rx_rows):
        iy = ry - i * RH
        # Left half
        cell(RX_X, iy - RH, rx_mid / 2, RH, row[0], sz=5.5)
        cell(RX_X + rx_mid / 2, iy - RH, rx_mid / 2, RH, row[1], sz=6, bold=bool(row[1]))
        # Duration column (right)
        if i < len(dur_rows):
            cell(dur_x, iy - RH, dur_w, RH, dur_rows[i], sz=5.5, bold=True)
        else:
            cell(dur_x, iy - RH, dur_w, RH)

    rx_bottom = ry - len(rx_rows) * RH

    # Vascular access + Dialysate + Medication (remaining rows on right)
    va = (rec.get("va", "") or "").upper()
    va_items = [("☐ Arm", "☐ Leg"), ("☐ Lt", "☐ Rt"),
                (f"{'☑' if 'AVF' in va else '☐'} AVF", f"{'☑' if 'AVG' in va else '☐'} AVG"),
                (f"{'☑' if 'DLC' in va else '☐'} DLC", f"{'☑' if 'PERM' in va else '☐'} Perm Cath"),
                ("☐ Subclavian ☐ jugular", ""), ("☐ Femoral", "")]

    cell(LM, rx_bottom - RH, LEFT_W, RH, "Vascular access", sz=6, bold=True, align="center", bg=GRAY)
    for i, (l, r) in enumerate(va_items):
        iy = rx_bottom - (i + 1) * RH - RH
        cell(LM, iy, LEFT_W / 2, RH, l, sz=5.5)
        cell(LM + LEFT_W / 2, iy, LEFT_W / 2, RH, r, sz=5.5)

    # Dialysate
    dial_y = rx_bottom - RH
    cell(RX_X, dial_y - RH, RX_W, RH, "Dialysate", sz=6, bold=True, align="center", bg=GRAY)
    dial_items = ["K+  2  mEq/L", "Ca  3.5  mEq/L", "Hco3  32  mEq/L",
                  "Dialysate Temp 36.5  'C", "Dialysate flow  600  cc/min",
                  "Na/UF Variation Pro", "☐ Na start       mEq/L",
                  "☐ Na conc off     mEq/L", "☐ UF Profile"]
    for i, txt_d in enumerate(dial_items):
        iy = dial_y - (i + 2) * RH
        cell(RX_X, iy, RX_W * 0.55, RH, txt_d, sz=5.5)

    # Medication + Signature
    med_x = RX_X + RX_W * 0.55
    med_w = RX_W * 0.45
    cell(med_x, dial_y - 2 * RH, med_w, RH, "Medication", sz=6, bold=True, align="center", bg=GRAY)
    esa_drug = ex.get("esa_drug_given") or rec.get("esa_drug") or ""
    esa_route = ex.get("esa_route") or rec.get("esa_route") or ""
    cell(med_x, dial_y - 3 * RH, med_w, RH, f"EPO  {esa_drug}", sz=5.5)
    cell(med_x, dial_y - 4 * RH, med_w, RH, "Route  Time", sz=5.5, bold=True)
    cell(med_x, dial_y - 5 * RH, med_w, RH, f"IV  SC  {esa_route}", sz=5.5)
    cell(med_x, dial_y - 6 * RH, med_w, RH, "Signature", sz=5.5)

    # ════════════════════════════════════════════════════════════
    # NURSING DIAGNOSIS
    # ════════════════════════════════════════════════════════════
    nurs_y = min(left_bottom - 7 * RH, dial_y - 12 * RH) - 2
    third = CW / 3
    cell(LM, nurs_y, third, RH, "Nursing Diagnosis", sz=6, bold=True, align="center", bg=GRAY)
    cell(LM + third, nurs_y, third, RH, "Nursing care plan", sz=6, bold=True, align="center", bg=GRAY)
    cell(LM + 2 * third, nurs_y, third, RH, "Expective outcome", sz=6, bold=True, align="center", bg=GRAY)
    # 3 blank rows
    for r in range(3):
        for col in range(3):
            cell(LM + col * third, nurs_y - (r + 1) * RH, third, RH)

    # Sticker EPO area
    cell(RM - 70, nurs_y - RH, 70, 3 * RH, "Sticker EPO", sz=6, align="center")

    # Pre HD Note
    pre_y = nurs_y - 4 * RH
    cell(LM, pre_y, CW, RH, f"Pre HD Note  {rec.get('pre_hd_note', '') or ''}", sz=6)

    # ════════════════════════════════════════════════════════════
    # MONITORING TABLE
    # ════════════════════════════════════════════════════════════
    tbl_y = pre_y - 4

    # Nurse start label
    c.setFont("Loma", 6)
    c.drawRightString(RM, tbl_y + 2, "Nurse start")

    headers = ["TIME", "BP\nmm/Hg", "PR\n/min", "RR\n/min", "BFR\nml/min", "VP\nmm/Hg",
               "TMP\nmm/Hg", "UFR\nml/hr", "Conduct\nmS/cm", "Total UF\nml", "Remark"]
    # Column widths proportional
    col_pct = [0.065, 0.085, 0.052, 0.052, 0.062, 0.062, 0.062, 0.062, 0.062, 0.070, 0.296]
    cws = [CW * p for p in col_pct]
    tbl_rh = 13  # table row height
    hdr_h = 20

    # Header
    cx = LM
    for i, hdr in enumerate(headers):
        cell(cx, tbl_y - hdr_h, cws[i], hdr_h, "", bg=GRAY)
        lines = hdr.split("\n")
        for j, line in enumerate(lines):
            c.setFont("Loma-Bold", 5.5)
            c.drawCentredString(cx + cws[i] / 2, tbl_y - 8 - j * 8, line)
        cx += cws[i]

    y_row = tbl_y - hdr_h
    sheet = rec.get("sheet", [])

    def _draw_row(data):
        nonlocal y_row
        y_row -= tbl_rh
        cx = LM
        for i, val in enumerate(data):
            al = "center" if i < 10 else "left"
            bld = i == 0  # bold time
            cell(cx, y_row, cws[i], tbl_rh, str(val), sz=6, bold=bld, align=al)
            cx += cws[i]

    # Pre-HD row
    pre_v = ex.get("pre_vitals", {}) or {}
    pre_bp = f"{pre_v.get('sbp', '')}/{pre_v.get('dbp', '')}" if pre_v.get("sbp") else ""
    _draw_row(["", pre_bp, str(pre_v.get("hr", "") or ""), "", "", "", "", "", "", "", "Pre-HD"])
    # Start HD row
    _draw_row([_thai_time(rec.get("started_at")), "", "", "", "", "", "", "", "", "", "Start HD"])

    # Data rows
    for entry in sheet[:12]:
        if y_row < 155:
            break
        _draw_row([
            entry.get("slot_time", ""),
            f"{entry.get('sbp', '')}/{entry.get('dbp', '')}" if entry.get("sbp") else "",
            _f(entry.get("pr"), 0), _f(entry.get("rr"), 0),
            _f(entry.get("bfr"), 0), _f(entry.get("vp"), 0), _f(entry.get("tmp"), 0),
            _f(entry.get("ufr"), 0), _f(entry.get("conductivity"), 1),
            _f(entry.get("total_uf"), 0),
            (entry.get("remark", "") or "")[:30],
        ])

    # Empty rows to fill space (ลดเหลือพอดีกับช่องลายเซ็น)
    while y_row > 220:
        _draw_row([""] * 11)

    # ════════════════════════════════════════════════════════════
    # POST-HD: Nurse Off / Total UF / Net UF
    # ════════════════════════════════════════════════════════════
    y_post = y_row - 2
    c.setFont("Loma", 6)
    c.drawRightString(RM, y_post + 3, "Nurse Off")
    y_post -= 10
    post_labels = ["Nss", "50%glucose", "Extra fluid", "meal and drink",
                   "Total repletcement", "Total UF", "Net UF balance"]
    pw = CW / len(post_labels)
    for i, lbl in enumerate(post_labels):
        cell(LM + i * pw, y_post, pw, 10, lbl, sz=5.5, bold=True, align="center")
    y_post -= 10
    actual_uf = _f(rec.get("actual_uf"), 0) if rec.get("actual_uf") else ""
    post_vals = ["ml", "ml", "ml", "ml", "ml", f"{actual_uf} ml", f"{actual_uf} ml"]
    for i, val in enumerate(post_vals):
        cell(LM + i * pw, y_post, pw, 10, val, sz=6, bold=True, align="center")

    # ════════════════════════════════════════════════════════════
    # BOTTOM: Complication / Technical / Nursing / Doctor note
    # ════════════════════════════════════════════════════════════
    y_bot = y_post - 12
    q = CW / 4
    cell(LM, y_bot, q, 10, "Complication", sz=6, bold=True, align="center", bg=GRAY)
    cell(LM + q, y_bot, q, 10, "Technical complication", sz=6, bold=True, align="center", bg=GRAY)
    cell(LM + 2 * q, y_bot, q, 10, "Nursing managment", sz=6, bold=True, align="center", bg=GRAY)
    cell(LM + 3 * q, y_bot, q, 10, "Docter note", sz=6, bold=True, align="center", bg=GRAY)

    comp_items = ["Hypotenstion", "Hypertension", "Muscle cramp", "Chest pain",
                  "Headache", "Fever/chill", "Arrhythmia", "VA problem",
                  "Dialyzer", "Cardiac arrest", "DDS", "Air embolism",
                  "No Complication", "other"]
    tech_items = ["Blood leak", "Clot dialyzer", "clotted bloodline",
                  "machine problem", ">2 needle", "No complication"]
    nurs_items = ["Monitor V/S", "Psycho support", "Oxygen therapy",
                  "Trendelenburg position", "Decrease UFR", "Decrease BFR",
                  "Hypertonic solution", "moniter access flow",
                  "Hot compress", "Cold compress", "Aware Aspirate",
                  "Chang dialyzer", "Chang bloodline", "Post HD care"]
    doctor_items = ["ไข้", "เหนื่อย", "แน่นหน้าอก", "ปวดหัว", "ตะคริว"]

    max_rows = max(len(comp_items), len(nurs_items))
    for r in range(max_rows):
        ry = y_bot - (r + 1) * 8
        # Complication
        txt_c = f"☐ {comp_items[r]}" if r < len(comp_items) else ""
        cell(LM, ry, q, 8, txt_c, sz=5)
        # Technical
        txt_t = f"☐ {tech_items[r]}" if r < len(tech_items) else ""
        cell(LM + q, ry, q, 8, txt_t, sz=5)
        # Nursing
        txt_n = f"☐ {nurs_items[r]}" if r < len(nurs_items) else ""
        cell(LM + 2 * q, ry, q, 8, txt_n, sz=5)
        # Doctor note
        if r < len(doctor_items):
            cell(LM + 3 * q, ry, q, 8, f"- {doctor_items[r]}     ☐ ไม่มี     ☐ มี", sz=5)
        elif r == len(doctor_items):
            cell(LM + 3 * q, ry, q, 8, "Post HD note", sz=5.5, bold=True)
        elif r == len(doctor_items) + 1:
            cell(LM + 3 * q, ry, q, 8, "เหนื่อย     ☐ ไม่มี     ☐ มี", sz=5)
        elif r == len(doctor_items) + 2:
            cell(LM + 3 * q, ry, q, 8, "เลือดออก    ☐ ไม่มี     ☐ มี", sz=5)
        elif r == len(doctor_items) + 4:
            post_v = ex.get("post_vitals", {}) or {}
            bp = f"{post_v['sbp']}/{post_v.get('dbp','')}" if post_v.get("sbp") else "____/____"
            cell(LM + 3 * q, ry, q, 8, f"BP ก่อนกลับ  {bp}  mm/Hg", sz=6, bold=True)
        elif r == len(doctor_items) + 5:
            cell(LM + 3 * q, ry, q, 8, "Discharge by", sz=5.5, bold=True)
        elif r == len(doctor_items) + 6:
            cell(LM + 3 * q, ry, q, 8, "( ) Home   ( ) Admit", sz=5.5)
        elif r == len(doctor_items) + 7:
            cell(LM + 3 * q, ry, q, 8, "( ) เดินกลับ ( ) รถเข็น ( ) เปลนอน", sz=5)
        else:
            cell(LM + 3 * q, ry, q, 8)

    # ════════════════════════════════════════════════════════════
    # SIGNATURES — ใต้ตาราง Complication
    # ════════════════════════════════════════════════════════════
    sig_y = y_bot - (max_rows + 1) * 8 - 4
    nurse_name = ex.get("nurse_name", "") or ""

    # Nephrologist (ซ้าย)
    c.setFont("Loma-Bold", 7)
    c.drawString(LM, sig_y, "Nephrologist")
    c.setFont("Loma", 7)
    c.line(LM + 60, sig_y - 1, LM + 200, sig_y - 1)

    # Nurse (กลาง-ขวา) พร้อมชื่อ + เส้นเซ็น
    c.setFont("Loma-Bold", 7)
    c.drawString(LM + CW * 0.42, sig_y, "Nurse")
    c.setFont("Loma", 7)
    if nurse_name:
        c.drawString(LM + CW * 0.42 + 32, sig_y, nurse_name)
    # รายชื่อพยาบาล (เว้นช่องไว้สำหรับเซ็นหลายคน)
    c.line(LM + CW * 0.42 + 32, sig_y - 1, RM, sig_y - 1)

    # Footer
    c.setFont("Loma", 4.5)
    c.drawRightString(RM, sig_y - 12, f"HD Smart Monitoring · {_thai_date(_thai_now().date())} {_thai_time(_thai_now())}")

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

    # ── Global X offset (pt) — ตั้งเป็น 0 แล้ววัดทีละช่องแทน ──
    DX = 0

    # ── Helpers (DX applied automatically) ──
    def txt(x, y, text, size=7, bold=False):
        if not text and text != 0:
            return
        c.setFont("Loma-Bold" if bold else "Loma", size)
        c.drawString(x + DX, y, str(text))

    def txtc(x, y, text, width, size=7, bold=False):
        """Center-aligned text within a box starting at x with given width."""
        if not text and text != 0:
            return
        c.setFont("Loma-Bold" if bold else "Loma", size)
        tw = c.stringWidth(str(text), "Loma-Bold" if bold else "Loma", size)
        c.drawString(x + DX + (width - tw) / 2, y, str(text))

    def txtr(x, y, text, size=7, bold=False):
        """Right-aligned text."""
        if not text and text != 0:
            return
        c.setFont("Loma-Bold" if bold else "Loma", size)
        c.drawRightString(x + DX, y, str(text))

    def tick(x, y):
        c.setFont("Loma-Bold", 9)
        c.drawString(x + DX, y, "✓")

    # ════════════════════════════════════════════════════════
    # HEADER ROW — จาก grid: y≈805 (ชื่อคลินิก), y≈790 (Patient's name)
    # ════════════════════════════════════════════════════════
    # Patient's name: x≈108, y≈788
    txt(108, 788, rec.get("name", ""), size=9, bold=True)
    # Age: ตัวหนาใหญ่ 2 เท่า — x≈340, y≈786
    age = rec.get("age")
    txt(338, 786, str(age) if age else "", size=14, bold=True)
    # HN: ตัวหนาใหญ่ 2 เท่า — x≈400, y≈786
    txt(398, 786, rec.get("hn", ""), size=14, bold=True)
    # Dx CKD stage 5: x≈492, y≈790
    txt(492, 790, "5", size=7)
    # HD No.: x≈555, y≈805
    hd_no = rec.get("hd_no")
    txt(555, 803, str(hd_no) if hd_no else "", size=10, bold=True)
    # Date: ตัวหนาใหญ่ 2 เท่า — x≈538, y≈786
    txt(538, 786, _thai_date(rec.get("date")), size=14, bold=True)
    # Shift (รอบเช้า/บ่าย/ค่ำ): x≈490, y≈805
    shift_th = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}.get(rec.get("shift", ""), "")
    txt(490, 805, f"รอบ{shift_th}", size=8, bold=True)

    # ════════════════════════════════════════════════════════
    # PRE-DIALYSIS ASSESSMENT — ฝั่งซ้าย
    # grid: แถว Last wt y≈768, Pre BW y≈756, Dry BW y≈744 ...
    # ค่า kg อยู่หลังช่อง OY ON ≈ x=228
    # ════════════════════════════════════════════════════════
    pre_w = rec.get("pre_weight")
    post_w = rec.get("post_weight")
    dw = rec.get("dry_weight")

    # ════════════════════════════════════════════════════════
    # WEIGHT + PRESCRIPTION — วัดจาก precise grid (DX=0)
    # Row Y positions (จาก 4pt grid): ทุกแถวห่างกัน 11.5pt
    # Row 0 (Anxiety/Last wt/Machine)      y = 770
    # Row 1 (Sleep/Pre BW/Dialyzer)        y = 758
    # Row 2 (Dyspnea/Dry BW/Surface)       y = 747
    # Row 3 (Orthopnea/Wt Gain/Use No)     y = 735
    # Row 4 (Crepitation/Meal/Last TCV)     y = 724
    # Row 5 (JVP/Target UF/เปิดใช้เมื่อ)    y = 712
    # Row 6 (Arrhythmia/Post BW/Tesk leak)  y = 701
    # Row 7 (Fever/Wt lose/Disinfectant)    y = 689
    # ════════════════════════════════════════════════════════
    RY = [770, 758, 747, 735, 724, 712, 701, 689]  # y ของแต่ละแถว

    # ── WEIGHT (ช่องว่าง x=222-252, w=30, center) ──
    WT_X, WT_W = 222, 30
    prev_post = rec.get("prev_post_weight") or ex.get("prev_post_weight")
    txtc(WT_X, RY[0], _f(prev_post), WT_W, size=8, bold=True)    # Last wt
    txtc(WT_X, RY[1], _f(pre_w), WT_W, size=8, bold=True)        # Pre BW
    txtc(WT_X, RY[2], _f(dw), WT_W, size=8, bold=True)           # Dry BW
    if pre_w and dw:                                                # Wt Gain
        txtc(WT_X, RY[3], _f(round(float(pre_w) - float(dw), 1)), WT_W, size=8)
    # Row 4 (Meal/drink) — skip
    txtc(WT_X, RY[5], _f(rec.get("uf_goal"), 0), WT_W, size=8, bold=True)  # Target UF
    txtc(WT_X, RY[6], _f(post_w), WT_W, size=8, bold=True)       # Post BW
    if pre_w and post_w:                                            # Wt lose
        txtc(WT_X, RY[7], _f(round(float(pre_w) - float(post_w), 1)), WT_W, size=8)

    # ── MACHINE / DIALYZER (หลัง label: x=375) ──
    machine = ex.get("machine", {}) or {}
    machine_label = f"{machine.get('brand', '')} {machine.get('model', '')}".strip() if machine else ""
    txt(375, RY[0], machine_label, size=7, bold=True)              # Machine
    txt(440, RY[0], machine.get("serial", "") if machine else "", size=5.5)  # S/N
    dialyzer = ex.get("dialyzer_type", "") or ""
    txt(375, RY[1], dialyzer, size=7, bold=True)                   # Dialyzer
    surface = ex.get("surface_area")
    if surface:
        txt(395, RY[2], str(surface), size=7)                      # Surface area
    reuse = ex.get("dialyzer_reuse")
    if reuse:
        txt(375, RY[3], str(reuse), size=7)                        # Use No.

    # ── HEPARIN (Loading x=460, Maintenance x=460) ──
    hep = ex.get("heparin", {}) or {}
    hep_protocol = (hep.get("protocol", "") or "").lower()

    if "heparin" in hep_protocol and "free" not in hep_protocol:
        tick(495, RY[0])   # ☐ Heparin
    elif "lmwh" in hep_protocol:
        tick(495, RY[1])   # ☐ LMWH

    txt(460, RY[4], str(hep.get("bolus", "")) or "", size=7, bold=True)        # Loading
    txt(460, RY[5], str(hep.get("maintenance", "")) or "", size=7, bold=True)   # Maintenance

    if "free" in hep_protocol:
        tick(495, RY[6])

    # ── DURATION / TIME (ช่อง x=555-590, w=35) ──
    DUR_X, DUR_W = 555, 35
    duration = rec.get("duration_hr") or _DURATION_HR
    txtc(DUR_X, RY[0], str(duration), DUR_W, size=8, bold=True)                  # Duration
    txtc(DUR_X, RY[1], _thai_time(rec.get("started_at")), DUR_W, size=7, bold=True)  # Time start
    txtc(DUR_X, RY[2], _thai_time(rec.get("ended_at")), DUR_W, size=7, bold=True)    # Time off

    # ── O2 sat / BT / Hct / Hb (ช่อง x=555-590) ──
    bt = rec.get("bt") or ex.get("bt_value")
    txtc(DUR_X, RY[4], _f(bt), DUR_W, size=7, bold=True)          # BT
    hct = rec.get("hct") or ex.get("hct_value")
    hb = rec.get("hb") or ex.get("hb_value")
    txtc(DUR_X, RY[5], _f(hct), DUR_W, size=7, bold=True)         # Hct
    txtc(DUR_X, RY[6], _f(hb), DUR_W, size=7, bold=True)          # Hb

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
    # Column definitions: (x_start, width) for center alignment
    # จาก grid: TIME 15-65, BP 65-120, PR 120-150, RR 150-183, BFR 183-222
    #           VP 222-262, TMP 262-305, UFR 305-350, Conduct 350-412, TotalUF 412-468, Remark 468-580
    # จาก field-map V2 (verified with colored boxes)
    CX_TIME = (15, 48)
    CX_BP = (63, 56)
    CX_PR = (119, 32)
    CX_RR = (151, 30)
    CX_BFR = (181, 42)
    CX_VP = (223, 38)
    CX_TMP = (261, 42)
    CX_UFR = (303, 48)
    CX_COND = (351, 60)
    CX_TUF = (411, 58)
    CX_REM = (469, 110)
    ROW_H = 15.0
    SZ = 6  # font size for table

    # Pre-HD row: y≈406
    y_pre = 406
    pre_v = ex.get("pre_vitals", {}) or {}
    if pre_v.get("sbp"):
        txtc(CX_BP[0], y_pre, f"{pre_v['sbp']}/{pre_v.get('dbp','')}", CX_BP[1], size=SZ)
        txtc(CX_PR[0], y_pre, str(pre_v.get("hr", "")), CX_PR[1], size=SZ)

    # Start HD row: y≈391
    y_start_hd = y_pre - ROW_H
    txtc(CX_TIME[0], y_start_hd, _thai_time(rec.get("started_at")), CX_TIME[1], size=SZ)

    # Data rows: เริ่มหลัง Start HD
    for i, entry in enumerate(sheet[:12]):
        y_row = y_start_hd - (i + 1) * ROW_H
        if y_row < 275:
            break
        txtc(CX_TIME[0], y_row, entry.get("slot_time", ""), CX_TIME[1], size=SZ)
        if entry.get("sbp"):
            txtc(CX_BP[0], y_row, f"{entry['sbp']}/{entry.get('dbp','')}", CX_BP[1], size=SZ)
        txtc(CX_PR[0], y_row, _f(entry.get("pr"), 0), CX_PR[1], size=SZ)
        txtc(CX_RR[0], y_row, _f(entry.get("rr"), 0), CX_RR[1], size=SZ)
        txtc(CX_BFR[0], y_row, _f(entry.get("bfr"), 0), CX_BFR[1], size=SZ)
        txtc(CX_VP[0], y_row, _f(entry.get("vp"), 0), CX_VP[1], size=SZ)
        txtc(CX_TMP[0], y_row, _f(entry.get("tmp"), 0), CX_TMP[1], size=SZ)
        txtc(CX_UFR[0], y_row, _f(entry.get("ufr"), 0), CX_UFR[1], size=SZ)
        txtc(CX_COND[0], y_row, _f(entry.get("conductivity"), 1), CX_COND[1], size=SZ)
        txtc(CX_TUF[0], y_row, _f(entry.get("total_uf"), 0), CX_TUF[1], size=SZ)
        remark = (entry.get("remark", "") or "")[:25]
        if remark:
            txt(CX_REM[0] + 2, y_row, remark, size=5)

    # ════════════════════════════════════════════════════════
    # POST-HD SECTION
    # Nurse Off: y≈270
    # Total repletcement / Total UF / Net UF balance: y≈253
    # ════════════════════════════════════════════════════════
    actual_uf = rec.get("actual_uf")
    if actual_uf is not None:
        # Total UF: field-map box(448,248,55,14)
        txtc(448, 250, str(int(actual_uf)), 55, size=7, bold=True)
        # Net UF balance: box(533,248,58,14)
        txtc(533, 250, str(int(actual_uf)), 58, size=7, bold=True)

    # ════════════════════════════════════════════════════════
    # BP ก่อนกลับ: y≈75 area
    # ════════════════════════════════════════════════════════
    post_v = ex.get("post_vitals", {}) or {}
    if post_v.get("sbp"):
        # BP ก่อนกลับ: field-map box(395,62,90,16) — center + bold
        txtc(395, 65, f"{post_v['sbp']}/{post_v.get('dbp','')}", 90, size=10, bold=True)

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


def load_and_build(conn, session_id, clinic_name="ระนอง รักษ์ไต", mode="draw"):
    """Load session data from DB and build the work sheet PDF.

    mode='draw'    (default) = แบบ B — วาด layout ใหม่ทั้งหมด (ตรง 100%)
    mode='overlay'           = แบบ A — เติมข้อมูลบน template ฟอร์มจริง
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
