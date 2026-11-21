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


def _first_vital(rec, idx):
    """ค่าจาก vital signs ของรอบนี้: ค่าแรกที่ไม่ว่าง (fallback ค่าสุดท้าย = เผื่อกรอกช้า).
    rec["vitals"] tuple: (phase, sbp, dbp, heart_rate, temperature, spo2, recorded_at)."""
    vals = [v[idx] for v in (rec.get("vitals") or []) if len(v) > idx and v[idx] is not None]
    return vals[0] if vals else None


def _sheet_total_uf(rec):
    """Total UF จากเครื่องจริง: ค่าสูงสุดของช่อง UF รวมใน Monitoring Sheet
    (รวมที่ sync มาจากหน้ารอบฟอก/ปุ่มบันทึก UF) — ตรงกว่า actual_uf ที่กรอกมือ."""
    best = None
    for e in (rec.get("sheet") or []):
        t = e.get("total_uf")
        try:
            t = int(float(t)) if t not in (None, "") else None
        except (TypeError, ValueError):
            t = None
        if t:
            best = t if best is None else max(best, t)
    return best


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

def build_worksheet_pdf(rec, extras=None, clinic_name="ระนอง รักษ์ไต", ws_data=None):
    """Build HD Work Sheet PDF — แบบ B: วาด layout เหมือนฟอร์มกระดาษจริง 100%.

    ข้อมูลตรงช่องทุกจุดเพราะช่องกับตัวเลขสร้างพร้อมกัน
    v11: +Interactive checklist from hd_worksheet_data (ws_data)
         Pre-dialysis OY/ON, Complications, Nursing Dx, Notes
    """
    ws = ws_data or {}
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
    def draw_checkbox(x, y, checked=False, size=6):
        """Draw a checkbox at (x, y). If checked, draw a ✓ checkmark inside."""
        c.saveState()
        c.setStrokeColor(HexColor("#000000"))
        c.setLineWidth(0.5)
        c.rect(x, y, size, size, fill=0, stroke=1)
        if checked:
            # Draw bold checkmark ✓ inside the box
            c.setStrokeColor(HexColor("#0066cc"))
            c.setLineWidth(1.2)
            # Checkmark: short line down-right, then long line up-right
            c.line(x + 1, y + size * 0.45, x + size * 0.35, y + 1.5)
            c.line(x + size * 0.35, y + 1.5, x + size - 1, y + size - 1.5)
        c.restoreState()

    def ck_text(checked, label, x, y, sz=5.5):
        """Draw checkbox + label text at position."""
        draw_checkbox(x + 1, y + 1.5, checked=checked, size=sz)
        c.setFont("Loma", sz)
        c.setFillColor(HexColor("#000000"))
        c.drawString(x + sz + 3, y + 1.5, label)

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

    # ข้อ 7: DRAFT watermark ถ้ายังฟอกไม่เสร็จ
    session_status = rec.get("status", "")
    if session_status and session_status != "completed":
        c.saveState()
        c.setFillColor(HexColor("#cccccc"))
        c.setFont("Loma-Bold", 60)
        c.translate(W / 2, H / 2)
        c.rotate(45)
        c.drawCentredString(0, 0, "DRAFT")
        c.restoreState()

    # ════════════════════════════════════════════════════════════
    # HEADER
    # ════════════════════════════════════════════════════════════
    y = H - 25
    c.setFont("Loma-Bold", 13)
    c.drawCentredString(W / 2, y, "Hemodialysis work sheet")
    y -= 15
    c.setFont("Loma", 8)
    c.drawString(LM, y, clinic_name)
    # ข้อ 5: HD No. ใหญ่เด่นชัด
    hd_no = rec.get("hd_no")
    c.setFont("Loma-Bold", 12)
    c.drawRightString(RM - 80, y, f"HD No.  {hd_no or '____'}")
    c.setFont("Loma-Bold", 10)
    c.drawRightString(RM, y, f"{_thai_date(rec.get('date'))}")

    # ข้อ 8: Running number (เลขที่ใบ = WS-{session_id})
    session_id = rec.get("session_id") or rec.get("id") or ""
    if session_id:
        c.setFont("Loma", 6)
        c.drawRightString(RM, H - 10, f"เลขที่ WS-{session_id}")

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

    # Map assessment item names → ws_data keys
    _ASSESS_KEY_MAP = {
        "Anxiety": "assess_anxiety", "Sleep disturbance": "assess_sleep_disturbance",
        "Dyspnea": "assess_dyspnea", "Orthopnea": "assess_orthopnea",
        "Crepitation": "assess_crepitation", "JVP engorged": "assess_jvp_engorged",
        "Arrhythmia": "assess_arrhythmia", "Fever": "assess_fever",
        "Headache": "assess_headache", "Nausea/Vomiting": "assess_nausea_vomiting",
        "Anorexia": "assess_anorexia", "Weakness": "assess_weakness",
        "Prolong bleeding": "assess_prolong_bleeding", "Pale": "assess_pale",
        "Edema": "assess_edema", "Itching": "assess_itching",
        "constipation": "assess_constipation", "Urine": "assess_urine",
    }

    for i, item in enumerate(_PRE_DX_ITEMS):
        iy = top_y - i * RH
        # Assessment name
        cell(assess_x, iy - RH, 70, RH, item, sz=5.5)
        # OY / ON — draw checkbox with canvas (font doesn't support [X])
        ws_key = _ASSESS_KEY_MAP.get(item)
        ws_val = ws.get(ws_key) if ws_key else None
        # OY column: checkbox + "OY" label
        cell(oy_x, iy - RH, 18, RH)  # empty bordered cell
        draw_checkbox(oy_x + 2, iy - RH + 2, checked=(ws_val is True), size=6)
        c.setFont("Loma", 4.5)
        c.drawString(oy_x + 10, iy - RH + 3, "OY")
        # ON column: checkbox + "ON" label
        cell(on_x, iy - RH, 18, RH)
        draw_checkbox(on_x + 2, iy - RH + 2, checked=(ws_val is False), size=6)
        c.setFont("Loma", 4.5)
        c.drawString(on_x + 10, iy - RH + 3, "ON")
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
    dialyzer = ex.get("dialyzer_type", "") or rec.get("dialyzer_type", "") or ""
    reuse = ex.get("dialyzer_reuse") or rec.get("dialyzer_reuse")
    # Fallback machine from rec if extras didn't have it
    if not machine_label:
        machine_label = ex.get("machine_name", "") or ""
    hep = ex.get("heparin", {}) or {}
    hep_bolus = hep.get("bolus", "") or ""
    hep_maint = hep.get("maintenance", "") or ""
    hep_protocol = (hep.get("protocol", "") or "").lower()
    # Fallback: parse heparin_dose from hd_sessions (e.g. "Heparin 1500/1500")
    if not hep_bolus and not hep_maint:
        import re as _re
        _hd_raw = str(ex.get("heparin_dose") or rec.get("heparin_dose") or "")
        _nums = _re.findall(r'(\d+)', _hd_raw)
        if len(_nums) >= 2:
            hep_bolus, hep_maint = _nums[0], _nums[1]
        elif len(_nums) == 1:
            hep_bolus = _nums[0]
        if _nums and not hep_protocol:
            hep_protocol = "heparin"
    duration = rec.get("duration_hr") or _DURATION_HR

    # Row definitions: (label, value, label2, value2)
    rx_half = RX_W / 2
    ry = top_y

    # ── 3 columns: Machine/Dialyzer | Anticoagulant | Duration ──
    dur_w = 80
    rx_mid = (RX_W - dur_w) / 2  # แบ่ง 2 ช่องเท่ากัน
    dur_x = RX_X + RX_W - dur_w
    anti_x = RX_X + rx_mid  # Anticoagulant column

    # ข้อ 7: Checkbox — ใช้ [X]/[ ] แทน [X]/[  ] เพราะ Loma font ไม่มี [X]
    _X = "[X]"  # checked
    _O = "[  ]"  # unchecked
    _has_hep = bool(hep_bolus or hep_maint or hep_protocol)
    _hep_ck = _X if (_has_hep and "free" not in hep_protocol and "lmwh" not in hep_protocol) else _O
    _lmwh_ck = _X if "lmwh" in hep_protocol else _O
    _free_ck = _X if "free" in hep_protocol else _O
    if not hep_protocol and _has_hep:
        _hep_ck = _X

    # Test Leak จาก ws_data
    _tl = ws.get("test_leak")
    _tl_txt = f"Tesk leak  {_X+' Y  '+_O+' N' if _tl is True else (_O+' Y  '+_X+' N' if _tl is False else 'O Y  O N')}"
    # Disinfectant จาก ws_data
    _dis = (ws.get("disinfectant") or "").lower()
    _dis_ck = lambda k: _X if k in _dis else _O

    # Column 1: Machine/Dialyzer/Tesk leak
    # col1 items: (label, value, checkbox_data)
    # checkbox_data: None=no checkbox, list of (checked, label) pairs for inline checkboxes
    col1 = [
        ("Machine", machine_label, None),
        ("Dialyzer", dialyzer, None),
        ("Surface area", "m2", None),
        ("Use No.", str(reuse) if reuse else "", None),
        ("Last TCV", "", None),
        ("เปิดใช้เมื่อ", "", None),
        ("Tesk leak", "", [(_tl is True, "Y"), (_tl is False, "N")]),
        ("", "", [("disinfect" in _dis, "Disinfectant"), ("paracetic" in _dis, "Paracetic")]),
        ("", "", [("formalin" in _dis, "Formalin"), ("other" in _dis, "Other")]),
    ]
    # Column 2: Anticoagulant / Heparin — will be drawn with draw_checkbox below
    _hep_checked = (_hep_ck == _X)
    _lmwh_checked = (_lmwh_ck == _X)
    _free_checked = (_free_ck == _X)
    col2 = [
        ("Anticoagulant", "", None),  # header — no checkbox
        ("Heparin", "", _hep_checked),
        ("LMWH", "", _lmwh_checked),
        ("Other", "", False),
        (f"Loading  {hep_bolus or '____'}", "start", None),
        (f"Maintenance  {hep_maint or '____'}", "In  hr", None),
        ("No Heparin free technique", "", _free_checked),
        ("Heparin ........................", "", False),
        ("Flush Nss   cc q   min", "", False),
    ]
    # Column 3: Duration / Time / BT / Hct / Hb
    # Hb/Hct: ดึงจาก session → extras → rec (ทุกทาง)
    _hct = _f(ex.get("hct_value") or rec.get("hct"))
    _hb = _f(ex.get("hb_value") or rec.get("hb"))
    # 21 พ.ย. 2569: O2 sat / BT ลงอัตโนมัติจาก vital signs ของรอบนี้
    # (ค่าแรกที่บันทึก = ก่อน/เริ่มฟอก ตามที่แพทย์กำหนด "ครั้งแรกหรือครั้งสุดท้ายก็ได้")
    _o2 = rec.get("o2sat") or ex.get("o2sat_value") or _first_vital(rec, 5)
    _bt = rec.get("bt") or ex.get("bt_value") or _first_vital(rec, 4)
    col3 = [
        f"Duration  {duration}  hr",
        f"Time start  {_thai_time(rec.get('started_at'))}",
        f"Time off  {_thai_time(rec.get('ended_at'))}",
        f"O2 sat  {_f(_o2)}  %" if _o2 is not None else "O2 sat              %",
        f"BT  {_f(_bt)}  'C",
        f"Hct=  {_hct}  %",
        f"Hb =  {_hb}  g/dl",
    ]

    n_rx = max(len(col1), len(col2), len(col3))
    for i in range(n_rx):
        iy = ry - i * RH
        # Col 1: Machine/Dialyzer/Test Leak/Disinfectant
        if i < len(col1):
            lbl, val, ck_data = col1[i]
            if ck_data is not None:
                # Row with inline checkboxes
                cell(RX_X, iy - RH, rx_mid, RH)
                cx_pos = RX_X + 2
                if lbl:
                    c.setFont("Loma", 5.5)
                    c.setFillColor(HexColor("#000000"))
                    c.drawString(cx_pos, iy - RH + 3, lbl)
                    cx_pos += c.stringWidth(lbl, "Loma", 5.5) + 4
                for ck_val, ck_lbl in ck_data:
                    draw_checkbox(cx_pos, iy - RH + 2, checked=ck_val, size=5)
                    c.setFont("Loma", 5)
                    c.setFillColor(HexColor("#000000"))
                    c.drawString(cx_pos + 7, iy - RH + 2.5, ck_lbl)
                    cx_pos += c.stringWidth(ck_lbl, "Loma", 5) + 12
            else:
                cell(RX_X, iy - RH, rx_mid, RH, f"{lbl}  {val}" if val else lbl, sz=5.5, bold=bool(val))
        else:
            cell(RX_X, iy - RH, rx_mid, RH)
        # Col 2: Anticoagulant — with draw_checkbox
        if i < len(col2):
            lbl, val, ck_flag = col2[i]
            if ck_flag is not None:  # has checkbox
                cell(anti_x, iy - RH, rx_mid, RH)
                draw_checkbox(anti_x + 2, iy - RH + 2, checked=ck_flag, size=6)
                c.setFont("Loma-Bold" if (i == 0) else "Loma", 5.5)
                c.setFillColor(HexColor("#000000"))
                c.drawString(anti_x + 10, iy - RH + 3, lbl)
            else:  # no checkbox — plain text
                txt_a = f"{lbl}  {val}" if val else lbl
                cell(anti_x, iy - RH, rx_mid, RH, txt_a, sz=5.5, bold=(i == 0))
        else:
            cell(anti_x, iy - RH, rx_mid, RH)
        # Col 3: Duration
        if i < len(col3):
            cell(dur_x, iy - RH, dur_w, RH, col3[i], sz=5.5, bold=True)
        else:
            cell(dur_x, iy - RH, dur_w, RH)

    rx_bottom = ry - n_rx * RH

    # Vascular access — อยู่ฝั่งซ้าย ใต้ Pre-dialysis assessment
    # ดึงจาก ws_data (interactive) ก่อน, fallback ไป rec (patient mapping)
    va = (ws.get("va_type") or rec.get("va", "") or ex.get("access_type", "") or "").upper()
    va_y = left_bottom  # เริ่มต่อจาก assessment items
    cell(LM, va_y - RH, LEFT_W, RH, "Vascular access", sz=6, bold=True, align="center", bg=GRAY)
    # 2 คอลัมน์: ซ้าย-ขวา
    # VA rows — draw_checkbox for each type
    va_types = [
        [("AVF", "AVF" in va), ("AVG", "AVG" in va)],
        [("DLC", "DLC" in va), ("Perm Cath", "PERM" in va)],
        [("Subclavian", False), ("jugular", False)],
        [("Femoral", False), ("", False)],
    ]
    for i, row_pair in enumerate(va_types):
        iy = va_y - (i + 2) * RH
        half = LEFT_W / 2
        for j, (lbl, ck_val) in enumerate(row_pair):
            cx = LM + j * half
            cell(cx, iy, half, RH)
            if lbl:
                draw_checkbox(cx + 2, iy + 2, checked=ck_val, size=6)
                c.setFont("Loma", 5.5)
                c.setFillColor(HexColor("#000000"))
                c.drawString(cx + 10, iy + 3, lbl)

    # Dialysate
    dial_y = rx_bottom - RH
    cell(RX_X, dial_y - RH, RX_W, RH, "Dialysate", sz=6, bold=True, align="center", bg=GRAY)
    dial_items = [
        ("K+  2  mEq/L", False), ("Ca  3.5  mEq/L", False), ("Hco3  32  mEq/L", False),
        ("Dialysate Temp 36.5  'C", False), ("Dialysate flow  600  cc/min", False),
        ("Na/UF Variation Pro", False), ("Na start       mEq/L", True),
        ("Na conc off     mEq/L", True), ("UF Profile", True),
    ]
    for i, (txt_d, has_ck) in enumerate(dial_items):
        iy = dial_y - (i + 2) * RH
        if has_ck:
            cell(RX_X, iy, RX_W * 0.55, RH)
            draw_checkbox(RX_X + 2, iy + 2, checked=False, size=5)
            c.setFont("Loma", 5.5); c.setFillColor(HexColor("#000000"))
            c.drawString(RX_X + 9, iy + 3, txt_d)
        else:
            cell(RX_X, iy, RX_W * 0.55, RH, txt_d, sz=5.5)

    # Medication + Signature
    med_x = RX_X + RX_W * 0.55
    med_w = RX_W * 0.45
    cell(med_x, dial_y - 2 * RH, med_w, RH, "Medication", sz=6, bold=True, align="center", bg=GRAY)
    esa_drug = ex.get("esa_drug_given") or rec.get("esa_drug") or rec.get("esa_rx") or ""
    esa_dose = ex.get("esa_dose_given") or rec.get("esa_dose") or ""
    esa_route = ex.get("esa_route") or rec.get("esa_route") or ""
    esa_given = rec.get("esa_given")
    esa_txt = f"EPO  {esa_drug}" + (f"  {esa_dose}" if esa_dose else "")
    if esa_given is False:
        esa_txt += "  (งด)"
    cell(med_x, dial_y - 3 * RH, med_w, RH, esa_txt, sz=5.5)
    cell(med_x, dial_y - 4 * RH, med_w, RH, "Route  Time", sz=5.5, bold=True)
    _iv_on = bool(esa_route and "iv" in esa_route.lower())
    _sc_on = bool(esa_route and "sc" in esa_route.lower())
    cell(med_x, dial_y - 5 * RH, med_w, RH)
    draw_checkbox(med_x + 2, dial_y - 5 * RH + 2, checked=_iv_on, size=6)
    c.setFont("Loma", 5.5); c.setFillColor(HexColor("#000000"))
    c.drawString(med_x + 10, dial_y - 5 * RH + 3, "IV")
    draw_checkbox(med_x + 28, dial_y - 5 * RH + 2, checked=_sc_on, size=6)
    c.drawString(med_x + 36, dial_y - 5 * RH + 3, "SC")
    cell(med_x, dial_y - 6 * RH, med_w, RH, "Signature", sz=5.5)

    # ════════════════════════════════════════════════════════════
    # NURSING DIAGNOSIS
    # ════════════════════════════════════════════════════════════
    nurs_y = min(left_bottom - 7 * RH, dial_y - 12 * RH) - 2
    third = CW / 3
    cell(LM, nurs_y, third, RH, "Nursing Diagnosis", sz=6, bold=True, align="center", bg=GRAY)
    cell(LM + third, nurs_y, third, RH, "Nursing care plan", sz=6, bold=True, align="center", bg=GRAY)
    cell(LM + 2 * third, nurs_y, third, RH, "Expective outcome", sz=6, bold=True, align="center", bg=GRAY)
    # ข้อ 2: Nursing Diagnosis 3 รายการตามฟอร์มจริง
    # Nursing Dx rows — each cell with checkbox drawn via canvas
    nurs_dx = [
        [("มีภาวะ Azotemia และ E'lyte imbalance", False), ("OVHDและลดUFตามแผนการรักษา", None), ("ลดภาวะ Azotemia ลด E'lyte", False)],
        [("เสี่ยงต่อ complication ระหว่าง HD", False), ("obs v/s และ Complication", False), ("No complication", False)],
        [("Bleeding precaution", False), ("Haparin free technique", False), ("No bleeding", False)],
    ]
    for r, row_items in enumerate(nurs_dx):
        for ci, (txt, ck_val) in enumerate(row_items):
            cx = LM + ci * third
            cy = nurs_y - (r + 1) * RH
            cell(cx, cy, third, RH)
            if ck_val is not None:
                draw_checkbox(cx + 2, cy + 2, checked=ck_val, size=5)
                c.setFont("Loma", 4.5); c.setFillColor(HexColor("#000000"))
                c.drawString(cx + 9, cy + 3, txt)
            else:
                c.setFont("Loma", 4.5); c.setFillColor(HexColor("#000000"))
                c.drawString(cx + 2, cy + 3, txt)

    # Sticker EPO area — อยู่เหนือ Nursing Diagnosis ในช่องว่างด้านขวา
    sticker_h = nurs_y + RH - (nurs_y + RH)  # คำนวณช่องว่างที่เหลือ
    # วาง Sticker EPO ตรงช่องว่างเหนือ Nursing Diagnosis
    sticker_top = nurs_y + 31  # เหนือ header Nursing +1cm
    sticker_box_h = 40  # สูง 40pt พอติด sticker
    cell(RM - 100, sticker_top, 100, sticker_box_h, "Sticker EPO", sz=7, bold=True, align="center")

    # Pre HD Note
    pre_y = nurs_y - 4 * RH
    cell(LM, pre_y, CW, RH, f"Pre HD Note  {rec.get('pre_hd_note', '') or ''}", sz=6)

    # ════════════════════════════════════════════════════════════
    # MONITORING TABLE
    # ════════════════════════════════════════════════════════════
    tbl_y = pre_y - 4

    # Header แบบ 2 แถว: ชื่อ (บน) + หน่วย (ล่าง) เหมือนฟอร์มจริง
    hdr_names = ["TIME", "BP", "PR", "RR", "BFR", "VP", "TMP", "UFR", "Conduct", "Total UF", "Remark"]
    hdr_units = ["", "mm/Hg", "/min", "/min", "ml/min", "mm/Hg", "mm/Hg", "ml/hr", "mS/cm", "ml", ""]
    col_pct = [0.065, 0.085, 0.052, 0.052, 0.062, 0.062, 0.062, 0.062, 0.062, 0.070, 0.296]
    cws = [CW * p for p in col_pct]
    tbl_rh = 13
    hdr_row_h = 12  # แต่ละแถว header
    hdr_h = hdr_row_h * 2  # 2 แถว

    # แถวบน: ชื่อคอลัมน์
    cx = LM
    for i, name in enumerate(hdr_names):
        cell(cx, tbl_y - hdr_row_h, cws[i], hdr_row_h, name, sz=6, bold=True, align="center", bg=GRAY)
        cx += cws[i]
    # แถวล่าง: หน่วย
    cx = LM
    for i, unit in enumerate(hdr_units):
        cell(cx, tbl_y - hdr_h, cws[i], hdr_row_h, unit, sz=5.5, align="center", bg=GRAY)
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

    # Empty rows with time slots (ให้พยาบาลเห็นเวลาที่ต้องบันทึก)
    # คำนวณ time slots ต่อจาก data rows
    from datetime import timedelta as _td
    last_time = None
    if sheet:
        # หา slot_time สุดท้าย
        last_slot = sheet[-1].get("slot_time", "")
        if last_slot and ":" in last_slot:
            parts = last_slot.split(":")
            last_time = datetime(2000, 1, 1, int(parts[0]), int(parts[1]))
    elif rec.get("started_at"):
        last_time = rec["started_at"].replace(second=0, microsecond=0)

    slot_idx = 0
    while y_row > 220:
        time_label = ""
        if last_time:
            next_time = last_time + _td(minutes=30 * (slot_idx + 1))
            # หยุดถ้าเลย ended_at
            if rec.get("ended_at") and next_time > rec["ended_at"]:
                time_label = ""
            else:
                time_label = f"{next_time.hour:02d}:{next_time.minute:02d}"
        _draw_row([time_label] + [""] * 10)
        slot_idx += 1

    # ════════════════════════════════════════════════════════════
    # POST-HD: Nurse Off / Total UF / Net UF
    # ════════════════════════════════════════════════════════════
    y_post = y_row - 2
    y_post -= 10
    post_labels = ["Nss", "50%glucose", "Extra fluid", "meal and drink",
                   "Total repletcement", "Total UF", "Net UF balance"]
    pw = CW / len(post_labels)
    for i, lbl in enumerate(post_labels):
        cell(LM + i * pw, y_post, pw, 10, lbl, sz=5.5, bold=True, align="center")
    y_post -= 10
    # 21 พ.ย. 2569: Total UF ลิงก์กับค่าเครื่องจริงจาก Sheet (ไม่ใช่ actual_uf ที่กรอกมือ)
    # Net UF balance = Total UF − สารน้ำทดแทน (ระบบยังไม่เก็บช่องทดแทน → เท่ากัน)
    _uf_tot = _sheet_total_uf(rec) or (int(rec["actual_uf"]) if rec.get("actual_uf") else None)
    actual_uf = str(_uf_tot) if _uf_tot else ""
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

    # Complication items + ws_data key mapping
    comp_items = ["Hypotenstion", "Hypertension", "Muscle cramp", "Chest pain",
                  "Headache", "Fever/chill", "Arrhythmia", "VA problem",
                  "Dialyzer", "Cardiac arrest", "DDS", "Air embolism",
                  "No Complication", "other"]
    _COMP_KEY_MAP = {
        "Hypotenstion": "comp_hypotension", "Hypertension": "comp_hypertension",
        "Muscle cramp": "comp_muscle_cramp", "Chest pain": "comp_chest_pain",
        "Headache": "comp_headache", "Fever/chill": "comp_fever_chill",
        "Arrhythmia": "comp_arrhythmia", "VA problem": "comp_va_problem",
        "Dialyzer": "comp_dialyzer", "Cardiac arrest": "comp_cardiac_arrest",
        "DDS": "comp_dds", "Air embolism": "comp_air_embolism",
        "No Complication": "comp_no_complication",
    }
    # ข้อ 3: Technical complication ครบ + ข้อ 1: Health Education
    tech_items = ["Blood leak", "Clot dialyzer", "clotted bloodline",
                  "machine problem", ">2 needle", "No complication"]
    _TECH_KEY_MAP = {
        "Blood leak": "tech_blood_leak", "Clot dialyzer": "tech_clot_dialyzer",
        "clotted bloodline": "tech_clotted_bloodline",
        "machine problem": "tech_machine_problem", ">2 needle": "tech_needle_gt2",
        "No complication": "tech_no_complication",
    }
    health_ed = ["Nutrition", "Vascular access", "Exercise",
                 "Personal hygiene", "Medication", "Fluid control", "KT", "Other"]
    _HE_KEY_MAP = {"Nutrition": "he_nutrition", "Vascular access": "he_vascular_access",
                   "Exercise": "he_exercise", "Personal hygiene": "he_personal_hygiene",
                   "Medication": "he_medication", "Fluid control": "he_fluid_control", "KT": "he_kt"}
    nurs_items = ["Monitor V/S", "Psycho support", "Oxygen therapy",
                  "Trendelenburg position", "Decrease UFR", "Decrease BFR",
                  "Hypertonic solution", "moniter access flow",
                  "Hot compress", "Cold compress", "Aware Aspirate",
                  "Chang dialyzer", "Chang bloodline", "Post HD care"]
    _NM_KEY_MAP = {"Monitor V/S": "nm_monitor_vs", "Psycho support": "nm_psycho_support",
                   "Oxygen therapy": "nm_oxygen", "Trendelenburg position": "nm_trendelenburg",
                   "Decrease UFR": "nm_decrease_ufr", "Decrease BFR": "nm_decrease_bfr",
                   "Hypertonic solution": "nm_hypertonic", "moniter access flow": "nm_access_flow",
                   "Hot compress": "nm_hot_compress", "Cold compress": "nm_cold_compress",
                   "Aware Aspirate": "nm_aspirate", "Chang dialyzer": "nm_chang_dialyzer",
                   "Chang bloodline": "nm_chang_bloodline", "Post HD care": "nm_post_hd_care"}
    doctor_items = ["ไข้", "เหนื่อย", "แน่นหน้าอก", "ปวดหัว", "ตะคริว"]
    _DX_KEY_MAP = {"ไข้": "dx_fever", "เหนื่อย": "dx_dyspnea", "แน่นหน้าอก": "dx_chest_pain",
                    "ปวดหัว": "dx_headache", "ตะคริว": "dx_cramp"}

    max_rows = max(len(comp_items), len(nurs_items))
    _cb_sz = 5  # checkbox size for bottom section
    for r in range(max_rows):
        ry = y_bot - (r + 1) * 8
        # Complication — draw_checkbox from ws_data
        if r < len(comp_items):
            ck = _COMP_KEY_MAP.get(comp_items[r])
            is_ck = bool(ck and ws.get(ck))
            cell(LM, ry, q, 8)
            draw_checkbox(LM + 2, ry + 1, checked=is_ck, size=_cb_sz)
            c.setFont("Loma", 5)
            c.setFillColor(HexColor("#000000"))
            c.drawString(LM + 9, ry + 2, comp_items[r])
        else:
            cell(LM, ry, q, 8, "", sz=5)
        # Technical + Health Education — draw_checkbox from ws_data
        if r < len(tech_items):
            tk = _TECH_KEY_MAP.get(tech_items[r])
            is_tk = bool(tk and ws.get(tk))
            cell(LM + q, ry, q, 8)
            draw_checkbox(LM + q + 2, ry + 1, checked=is_tk, size=_cb_sz)
            c.setFont("Loma", 5)
            c.setFillColor(HexColor("#000000"))
            c.drawString(LM + q + 9, ry + 2, tech_items[r])
        elif r == len(tech_items):
            cell(LM + q, ry, q, 8, "Health Education", sz=5.5, bold=True, bg=GRAY)
        elif r - len(tech_items) - 1 < len(health_ed):
            he_name = health_ed[r - len(tech_items) - 1]
            hk = _HE_KEY_MAP.get(he_name)
            is_hk = bool(hk and ws.get(hk))
            cell(LM + q, ry, q, 8)
            draw_checkbox(LM + q + 2, ry + 1, checked=is_hk, size=_cb_sz)
            c.setFont("Loma", 5)
            c.setFillColor(HexColor("#000000"))
            c.drawString(LM + q + 9, ry + 2, he_name)
        else:
            cell(LM + q, ry, q, 8)
        # Nursing management — draw_checkbox from ws_data
        if r < len(nurs_items):
            nk = _NM_KEY_MAP.get(nurs_items[r])
            is_nk = bool(nk and ws.get(nk))
            cell(LM + 2 * q, ry, q, 8)
            draw_checkbox(LM + 2 * q + 2, ry + 1, checked=is_nk, size=_cb_sz)
            c.setFont("Loma", 5)
            c.setFillColor(HexColor("#000000"))
            c.drawString(LM + 2 * q + 9, ry + 2, nurs_items[r])
        else:
            cell(LM + 2 * q, ry, q, 8)
        # Doctor note — ดึง ไม่มี/มี จาก ws_data
        if r < len(doctor_items):
            dk = _DX_KEY_MAP.get(doctor_items[r])
            dk_val = ws.get(dk) if dk else None  # "yes"/"no"/None
            cell(LM + 3 * q, ry, q, 8, f"- {doctor_items[r]}", sz=5)
            draw_checkbox(LM + 3 * q + 50, ry + 1, checked=(dk_val == "no"), size=_cb_sz)
            c.setFont("Loma", 4)
            c.drawString(LM + 3 * q + 57, ry + 2, "ไม่มี")
            draw_checkbox(LM + 3 * q + 78, ry + 1, checked=(dk_val == "yes"), size=_cb_sz)
            c.drawString(LM + 3 * q + 85, ry + 2, "มี")
        elif r == len(doctor_items):
            cell(LM + 3 * q, ry, q, 8, "Post HD note", sz=5.5, bold=True)
        elif r == len(doctor_items) + 1:
            _pd = ws.get("post_dyspnea")
            cell(LM + 3 * q, ry, q, 8, "เหนื่อย", sz=5)
            draw_checkbox(LM + 3 * q + 35, ry + 1, checked=(_pd == "no"), size=_cb_sz)
            c.setFont("Loma", 4); c.drawString(LM + 3 * q + 42, ry + 2, "ไม่มี")
            draw_checkbox(LM + 3 * q + 63, ry + 1, checked=(_pd == "yes"), size=_cb_sz)
            c.drawString(LM + 3 * q + 70, ry + 2, "มี")
        elif r == len(doctor_items) + 2:
            _pb = ws.get("post_bleeding")
            cell(LM + 3 * q, ry, q, 8, "เลือดออก", sz=5)
            draw_checkbox(LM + 3 * q + 35, ry + 1, checked=(_pb == "no"), size=_cb_sz)
            c.setFont("Loma", 4); c.drawString(LM + 3 * q + 42, ry + 2, "ไม่มี")
            draw_checkbox(LM + 3 * q + 63, ry + 1, checked=(_pb == "yes"), size=_cb_sz)
            c.drawString(LM + 3 * q + 70, ry + 2, "มี")
        elif r == len(doctor_items) + 4:
            # BP ก่อนกลับ — from ws_data or post_vitals
            _bp_sbp = ws.get("bp_discharge_sbp") or (ex.get("post_vitals", {}) or {}).get("sbp")
            _bp_dbp = ws.get("bp_discharge_dbp") or (ex.get("post_vitals", {}) or {}).get("dbp")
            bp = f"{_bp_sbp}/{_bp_dbp}" if _bp_sbp else "____/____"
            cell(LM + 3 * q, ry, q, 8, f"BP ก่อนกลับ  {bp}  mm/Hg", sz=6, bold=True)
        elif r == len(doctor_items) + 5:
            cell(LM + 3 * q, ry, q, 8, "Discharge by", sz=5.5, bold=True)
        elif r == len(doctor_items) + 6:
            _dt = ws.get("discharge_type") or ""
            cell(LM + 3 * q, ry, q, 8)
            draw_checkbox(LM + 3 * q + 2, ry + 1, checked=(_dt == "Home"), size=_cb_sz)
            c.setFont("Loma", 5); c.drawString(LM + 3 * q + 9, ry + 2, "Home")
            draw_checkbox(LM + 3 * q + 42, ry + 1, checked=(_dt == "Admit"), size=_cb_sz)
            c.drawString(LM + 3 * q + 49, ry + 2, "Admit")
        elif r == len(doctor_items) + 7:
            _dm = ws.get("discharge_mobility") or ""
            cell(LM + 3 * q, ry, q, 8)
            draw_checkbox(LM + 3 * q + 2, ry + 1, checked=("เดินกลับ" == _dm), size=_cb_sz)
            c.setFont("Loma", 4.5); c.drawString(LM + 3 * q + 9, ry + 2, "เดินกลับ")
            draw_checkbox(LM + 3 * q + 38, ry + 1, checked=("รถเข็น" == _dm), size=_cb_sz)
            c.drawString(LM + 3 * q + 45, ry + 2, "รถเข็น")
            draw_checkbox(LM + 3 * q + 70, ry + 1, checked=("เปลนอน" == _dm), size=_cb_sz)
            c.drawString(LM + 3 * q + 77, ry + 2, "เปลนอน")
        else:
            cell(LM + 3 * q, ry, q, 8)

    # ════════════════════════════════════════════════════════════
    # SIGNATURES — ใต้ตาราง Complication
    # ════════════════════════════════════════════════════════════
    sig_y = y_bot - (max_rows + 1) * 8 - 4
    # Nurse name: ws_data (interactive) → extras → empty
    nurse_start = ws.get("nurse_start_name") or ex.get("nurse_name", "") or ""
    nurse_end = ws.get("nurse_end_name") or nurse_start or ""
    # ข้อ 6: ชื่อแพทย์อัตโนมัติ (default = แพทย์เจ้าของคลินิก)
    _DEFAULT_DOCTOR = "นายแพทย์กัมพล พัฒนสมบัติสกุล ว.26015"
    doctor_name = ex.get("doctor_name", "") or rec.get("doctor_name", "") or _DEFAULT_DOCTOR

    # Nephrologist (ซ้าย) + ชื่อแพทย์
    c.setFont("Loma-Bold", 7)
    c.drawString(LM, sig_y, "Nephrologist")
    c.setFont("Loma", 7)
    if doctor_name:
        c.drawString(LM + 62, sig_y, doctor_name)
    c.line(LM + 60, sig_y - 1, LM + 210, sig_y - 1)

    # Nurse (กลาง-ขวา) พร้อมชื่อพยาบาล + เส้นเซ็น
    c.setFont("Loma-Bold", 7)
    c.drawString(LM + CW * 0.42, sig_y, "Nurse")
    c.setFont("Loma", 7)
    if nurse_end:
        c.drawString(LM + CW * 0.42 + 32, sig_y, nurse_end)
    c.line(LM + CW * 0.42 + 32, sig_y - 1, RM - 55, sig_y - 1)

    # ข้อ 8: QR Code — Smart QR: สแกนแล้วเปิด interactive session + trend + twin
    try:
        import segno
        session_id = rec.get("session_id") or rec.get("id")
        hn = rec.get("hn") or ""
        if session_id:
            base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://ranong-raktai-ai-cloud.onrender.com")
            # 21 พ.ย. 2569: QR เฉพาะใบ — token HMAC ผูกกับรอบฟอกนี้ใบเดียว เดา URL ไม่ได้
            # ผู้ถือใบ (ผู้ป่วย) สแกนเห็นใบฟอกดิจิทัลของตัวเองเท่านั้น · เจ้าหน้าที่มีลิงก์
            # ต่อไปหน้า Real-time ในหน้านั้น (ต้อง login) — ดูได้ทุกใบตามสิทธิ์เดิม
            try:
                from .patient_engage import make_token
                _d = rec.get("date")
                _day = _d.strftime("%Y%m%d") if _d else datetime.now().strftime("%Y%m%d")
                qr_url = f"{base_url}/hd/r/{make_token('rec', str(session_id), _day)}"
            except Exception:
                qr_url = f"{base_url}/hd/session/{session_id}"
            qr_obj = segno.make(qr_url)
            qr_buf = io.BytesIO()
            qr_obj.save(qr_buf, kind="png", scale=2, border=1)
            qr_buf.seek(0)
            from reportlab.lib.utils import ImageReader
            c.drawImage(ImageReader(qr_buf), RM - 50, sig_y - 8, width=42, height=42)
            c.setFont("Loma", 3.5)
            c.drawCentredString(RM - 29, sig_y - 11, "สแกนดูใบฟอกดิจิทัล")
    except ImportError:
        pass  # segno not installed — skip silently

    # Footer — with version + ws debug info
    c.setFont("Loma", 4.5)
    _ws_keys = [k for k, v in ws.items() if v is True and k.startswith(('assess_', 'comp_', 'tech_'))]
    _ws_debug = f"ws={len(_ws_keys)}ck" if _ws_keys else "ws=empty"
    c.drawRightString(RM, sig_y - 15, f"HD Smart Monitoring v12 · {_thai_date(_thai_now().date())} {_thai_time(_thai_now())} · {_ws_debug}")

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
        tick(495, RY[0])   # [  ] Heparin
    elif "lmwh" in hep_protocol:
        tick(495, RY[1])   # [  ] LMWH

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
    actual_uf = _sheet_total_uf(rec) or rec.get("actual_uf")   # 21 พ.ย. 2569: ลิงก์ค่าเครื่องจริงจาก Sheet
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
    # _session_record_extras uses SAVEPOINTs which require autocommit=False
    extras = {}
    try:
        was_ac = conn.autocommit
        conn.autocommit = False  # SAVEPOINTs need transaction mode
        with conn.cursor() as cur:
            pid = rec.get("patient_id")
            if not pid:
                cur.execute("SELECT patient_id FROM hd_sessions WHERE id = %s", (session_id,))
                pid_row = cur.fetchone()
                pid = pid_row[0] if pid_row else None
            if pid:
                extras = _session_record_extras(cur, session_id, pid)
        conn.commit()
        conn.autocommit = was_ac
        logger.info("hd_worksheet extras loaded: %s", [k for k in extras.keys() if extras.get(k)])
    except Exception as e:
        logger.warning("hd_worksheet extras FAILED: %s", e)
        try:
            conn.rollback()
            conn.autocommit = True
        except Exception:
            pass

    # Load interactive worksheet data — NEW connection to guarantee clean state
    ws_data = {}
    _ALTER_SQLS = [
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS dx_fever VARCHAR(10)",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS dx_dyspnea VARCHAR(10)",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS dx_chest_pain VARCHAR(10)",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS dx_headache VARCHAR(10)",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS dx_cramp VARCHAR(10)",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS post_dyspnea VARCHAR(10)",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS post_bleeding VARCHAR(10)",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS discharge_type VARCHAR(20)",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS discharge_mobility VARCHAR(20)",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS bp_discharge_sbp INTEGER",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS bp_discharge_dbp INTEGER",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_monitor_vs BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_psycho_support BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_oxygen BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_trendelenburg BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_decrease_ufr BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_decrease_bfr BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_hypertonic BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_access_flow BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_hot_compress BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_cold_compress BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_aspirate BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_chang_dialyzer BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_chang_bloodline BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nm_post_hd_care BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_nutrition BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_vascular_access BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_exercise BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_personal_hygiene BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_medication BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_fluid_control BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS he_kt BOOLEAN",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nurse_start_name VARCHAR(100)",
        "ALTER TABLE hd_worksheet_data ADD COLUMN IF NOT EXISTS nurse_end_name VARCHAR(100)",
    ]
    try:
        import psycopg2 as _pg2
        db_url2 = os.environ.get("HD_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
        conn2 = _pg2.connect(db_url2, connect_timeout=5)
        conn2.autocommit = True
        with conn2.cursor() as cur2:
            # Ensure all columns exist before SELECT *
            for _asql in _ALTER_SQLS:
                try:
                    cur2.execute(_asql)
                except Exception:
                    pass
            cur2.execute("SELECT * FROM hd_worksheet_data WHERE session_id = %s", (session_id,))
            ws_row = cur2.fetchone()
            if ws_row:
                col_names = [desc[0] for desc in cur2.description]
                ws_data = dict(zip(col_names, ws_row))
                logger.info("hd_worksheet: ws_data session %s — %d non-null",
                            session_id, sum(1 for v in ws_data.values() if v is not None))
            else:
                logger.info("hd_worksheet: no ws_data for session %s", session_id)
        conn2.close()
    except Exception as ws_err:
        logger.warning("hd_worksheet ws_data FAILED: %s", ws_err)

    if mode == "overlay" and os.path.exists(_TEMPLATE_PATH):
        try:
            pdf = build_overlay_pdf(rec, extras)
        except Exception as e:
            logger.warning("overlay PDF failed (%s) — fallback to draw mode", e)
            pdf = build_worksheet_pdf(rec, extras, clinic_name, ws_data)
    else:
        pdf = build_worksheet_pdf(rec, extras, clinic_name, ws_data)

    shift_th = SHIFT_TH.get(rec["shift"], rec["shift"])
    fname = f"HD-WorkSheet-{safe_name(rec['hn'], 12)}-{safe_name(rec['name'])}-{rec['date'].isoformat()}-{shift_th}.pdf"
    return pdf, fname
