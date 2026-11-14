"""Fluid Status Center — quarterly bioimpedance (Fresenius BCM) results next to Dry Weight.

Design: project doc HD-FLUID-STATUS-DW-DESIGN (14 Nov 2026).

Input today: the BCM "Fluid Management Tool" one-page PDF per patient (the
nurses export ~30 per quarter, Thai name = file name, romanised name + device
ID inside) — uploaded as a .zip, several PDFs, one PDF, or the reviewed .xlsx
table ("BCM … จับคู่HN") whose first column is the clinic HN.

Matching, in order: device ID already linked to a patient (hd_fluid_device_map)
→ clinic HN in the row (xlsx) → Thai file name = registry name (title stripped,
spelling variants tolerated) → romanised name vs English names in the
registry. Nothing is written until the preview is confirmed; the confirmed
device ID ↔ patient link is remembered so the next quarter matches itself.

Everything here proposes — DW changes still go through the physician.
"""
import difflib
import io
import logging
import re
import zipfile
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hd_fluid_assessments (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES hd_patient_mapping(id),
    hn VARCHAR(20) NOT NULL,
    assessed_at DATE NOT NULL,
    device VARCHAR(20) DEFAULT 'BCM',
    device_id VARCHAR(40),
    measured_name VARCHAR(100),
    weight_kg NUMERIC(5,1),
    height_cm NUMERIC(5,1),
    bmi NUMERIC(4,1),
    oh_pre_l NUMERIC(4,1),
    rel_oh_pct NUMERIC(5,1),
    oh_post_l NUMERIC(4,1),
    ufv_ml INTEGER,
    lti NUMERIC(4,1),
    fti NUMERIC(4,1),
    v_urea_l NUMERIC(5,2),
    bp_pre VARCHAR(10),
    source_file VARCHAR(120),
    confirmed_by VARCHAR(100),
    notes VARCHAR(200),
    created_at TIMESTAMP DEFAULT NOW()
)
"""
CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS ix_hd_fluid_patient_date ON hd_fluid_assessments(patient_id, assessed_at DESC)"
CREATE_MAP_SQL = """
CREATE TABLE IF NOT EXISTS hd_fluid_device_map (
    device_id VARCHAR(40) PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES hd_patient_mapping(id),
    hn VARCHAR(20),
    measured_name VARCHAR(100),
    updated_at TIMESTAMP DEFAULT NOW()
)
"""

DEFAULT_LIMITS = {"oh_high": 2.5, "rel_oh_high": 15.0, "oh_mild": 1.0, "oh_low": -1.0,
                  "lti_low_m": 10.0, "lti_low_f": 9.0, "due_days": 90, "dw_diff": 0.5}
FIELDS = ("weight_kg", "height_cm", "bmi", "oh_pre_l", "rel_oh_pct", "oh_post_l", "ufv_ml", "lti", "fti", "v_urea_l")
TITLES = r"^(นาย|นางสาว|นาง|น\.ส\.|ด\.ช\.|ด\.ญ\.|พระ|Mr\.?|Mrs\.?|Miss)\s*"


def _thai_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)


def limits_from(get_setting):
    lim = dict(DEFAULT_LIMITS)
    for k in lim:
        try:
            v = get_setting(f"fluid_{k}", "")
            if v not in (None, ""):
                lim[k] = float(v)
        except Exception:
            pass
    return lim


def norm_name(s):
    s = re.sub(TITLES, "", str(s or "").strip(), flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


def norm_eng(s):
    return re.sub(r"[^a-z]", "", str(s or "").lower())


def _num(pat, text):
    m = re.search(pat, text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_date(s):
    """'16/9/2026' or '16/9/2569' → date."""
    if not s:
        return None
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(s).strip())
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y > 2400:
        y -= 543
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_bcm_text(text):
    """Text of a Fluid Management Tool measurement report → dict (None when not a BCM report)."""
    if "Fluid Management Tool" not in text and "OH pre" not in text:
        return None
    rec = {
        "device": "BCM",
        "assessed_at": parse_date((re.search(r"Measurement Report (\d+/\d+/\d+)", text) or [None, None])[1]),
        "measured_name": ((re.search(r"Comments:\s*\n(.+)\nID:", text) or [None, ""])[1] or "").strip()[:100],
        "device_id": ((re.search(r"\nID:\s*(\S+)", text) or [None, ""])[1] or "").strip()[:40] or None,
        "sex": (re.search(r"Gender:\s*([MF])", text) or [None, None])[1],
        "dob": (re.search(r"Date of birth:\s*(\S+)", text) or [None, None])[1],
        "height_cm": _num(r"Height:\s*([\d.]+)", text), "weight_kg": _num(r"Weight:\s*([\d.]+)", text),
        "bmi": _num(r"BMI:\s*([\d.]+)", text), "lti": _num(r"LTI:\s*([\d.]+)", text), "fti": _num(r"FTI:\s*([\d.]+)", text),
        "oh_pre_l": _num(r"OH pre:\s*(-?[\d.]+)\s*L", text),
        "rel_oh_pct": _num(r"OH pre:\s*-?[\d.]+\s*L\s*\n\(%ECW\):\s*(-?[\d.]+)", text),
        "oh_post_l": _num(r"OH post:\s*(-?[\d.]+)\s*L", text), "ufv_ml": _num(r"UFV:\s*([\d.]+)", text),
        "v_urea_l": _num(r"V urea:\s*([\d.]+)", text),
        "bp_pre": (re.search(r"Pre BP:\s*(\d+/\d+)", text) or [None, None])[1],
    }
    if rec["ufv_ml"] is not None:
        rec["ufv_ml"] = int(rec["ufv_ml"])
    return rec


def _pdf_text(data):
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in r.pages[:2])


def _decode_zip_name(n):
    # Windows zips store Thai names either as UTF-8 (flag 0x800), cp874, or "#Uxxxx" escapes
    if "#U" in n:
        n = re.sub(r"#U([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), n)
    return n


def read_upload(filename, data):
    """→ list of raw records {file_name, **parsed} from .zip / .pdf / .xlsx. Raises ValueError."""
    name = (filename or "").lower()
    out = []
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".pdf") or info.file_size > 5 * 1024 * 1024:
                    continue
                raw = info.filename
                if not (info.flag_bits & 0x800):
                    try:
                        raw = info.filename.encode("cp437").decode("cp874")
                    except Exception:
                        pass
                fn = _decode_zip_name(raw).split("/")[-1]
                try:
                    rec = parse_bcm_text(_pdf_text(z.read(info)))
                except Exception as e:
                    logger.warning("bcm pdf %s: %s", fn[:40], e)
                    rec = None
                if rec:
                    rec["file_name"] = fn[:-4]
                    out.append(rec)
        if not out:
            raise ValueError("ไม่พบรายงาน BCM (PDF) ใน zip")
        return out
    if name.endswith(".pdf"):
        rec = parse_bcm_text(_pdf_text(data))
        if not rec:
            raise ValueError("ไม่ใช่รายงาน BCM / อ่านข้อความใน PDF ไม่ได้ (ถ้าเป็นสแกน ให้ส่งไฟล์จากเครื่องโดยตรง)")
        rec["file_name"] = filename.rsplit("/", 1)[-1][:-4]
        return [rec]
    if name.endswith(".xlsx"):
        return _read_xlsx(data)
    raise ValueError("รองรับ .zip ของ PDF, .pdf หรือ .xlsx (ตารางที่ตรวจทานแล้ว)")


XLSX_COLS = {"hnclinic": "hn", "hn": "hn", "ชื่อไฟล์bcm": "file_name", "ชื่อในเครื่องbcm": "measured_name", "bcmid": "device_id",
             "วันที่ตรวจ": "assessed_at", "เพศ": "sex", "น้ำหนัก(kg)": "weight_kg", "ส่วนสูง(cm)": "height_cm", "bmi": "bmi",
             "ohpre(l)": "oh_pre_l", "rel.oh(%ecw)": "rel_oh_pct", "lti": "lti", "fti": "fti", "vurea(l)": "v_urea_l", "bppre": "bp_pre"}


def _read_xlsx(data):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    ws = wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    hdr = None
    for i, r in enumerate(rows[:5]):
        m = {ci: XLSX_COLS[re.sub(r"\s+", "", str(h or "")).lower()] for ci, h in enumerate(r)
             if re.sub(r"\s+", "", str(h or "")).lower() in XLSX_COLS}
        if "hn" in m.values() and "oh_pre_l" in m.values():
            hdr, start = m, i + 1
            break
    if not hdr:
        raise ValueError("ไม่พบหัวตาราง (ต้องมี HN clinic และ OH pre (L))")
    out = []
    for r in rows[start:]:
        rec = {"device": "BCM"}
        for ci, f in hdr.items():
            v = r[ci] if ci < len(r) else None
            if f == "assessed_at":
                rec[f] = v.date() if isinstance(v, datetime) else (v if isinstance(v, date) else parse_date(v))
            elif f in FIELDS:
                try:
                    rec[f] = float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    rec[f] = None
            else:
                rec[f] = str(v).strip() if v not in (None, "") else None
        if not rec.get("assessed_at") or rec.get("oh_pre_l") is None and rec.get("lti") is None:
            continue
        if rec.get("hn"):
            rec["hn"] = re.sub(r"\.0$", "", rec["hn"]).upper()
        out.append(rec)
    if not out:
        raise ValueError("ไม่มีแถวข้อมูลในไฟล์")
    return out


def flags(rec, lim=None):
    lim = lim or DEFAULT_LIMITS
    f = []
    oh, rel, lti, sex = rec.get("oh_pre_l"), rec.get("rel_oh_pct"), rec.get("lti"), rec.get("sex")
    if oh is not None:
        if oh > lim["oh_high"] or (rel or 0) > lim["rel_oh_high"]:
            f.append("over_severe")
        elif oh > lim["oh_mild"]:
            f.append("over")
        elif oh < lim["oh_low"]:
            f.append("under")
    if lti is not None and ((sex == "F" and lti < lim["lti_low_f"]) or (sex != "F" and lti < lim["lti_low_m"])):
        f.append("lti_low")
    if lti is None and (oh in (None, 0.0)):
        f.append("incomplete")
    return f


FLAG_TH = {"over_severe": "🔴 น้ำเกินมาก", "over": "🟠 น้ำเกิน", "under": "🔵 น้ำขาด", "lti_low": "🍗 LTI ต่ำ",
           "incomplete": "⚠️ ค่าไม่ครบ", "due": "⚪ ครบกำหนดตรวจ"}


def match(cur, records):
    """Attach patient_id / hn / registry name / how / duplicate to each record (no writes)."""
    cur.execute("SELECT id, hn, patient_name, gender FROM hd_patient_mapping WHERE is_active = TRUE")
    pats = cur.fetchall()
    by_hn = {str(hn).upper(): (pid, name, g) for pid, hn, name, g in pats}
    by_name = {}
    by_eng = {}
    for pid, hn, name, g in pats:
        by_name.setdefault(norm_name(name), (pid, hn, name, g))
        if re.search(r"[A-Za-z]", str(name or "")):
            by_eng.setdefault(norm_eng(re.sub(TITLES, "", str(name))), (pid, hn, name, g))
    try:
        cur.execute("SELECT device_id, patient_id, hn FROM hd_fluid_device_map")
        dev = {d: (pid, hn) for d, pid, hn in cur.fetchall()}
    except Exception:
        dev = {}
    pid_name = {pid: (hn, name, g) for pid, hn, name, g in pats}
    names = list(by_name)
    out = []
    for r in records:
        r = dict(r)
        pid = how = ratio = None
        if r.get("device_id") and r["device_id"] in dev:
            pid, how = dev[r["device_id"]][0], "device_id"
        if pid is None and r.get("hn") and r["hn"].upper() in by_hn:
            pid, how = by_hn[r["hn"].upper()][0], "hn"
        if pid is None and r.get("file_name"):
            n = norm_name(r["file_name"])
            if n in by_name:
                pid, how = by_name[n][0], "name"
            else:
                c = difflib.get_close_matches(n, names, n=1, cutoff=0.8)
                if c:
                    pid, how, ratio = by_name[c[0]][0], "name_fuzzy", round(difflib.SequenceMatcher(None, n, c[0]).ratio(), 2)
        if pid is None and r.get("measured_name") and by_eng:
            k = norm_eng(r["measured_name"])
            c = difflib.get_close_matches(k, list(by_eng), n=1, cutoff=0.75)
            if c:
                pid, how, ratio = by_eng[c[0]][0], "eng_name", round(difflib.SequenceMatcher(None, k, c[0]).ratio(), 2)
        if pid is not None:
            hn, name, g = pid_name[pid]
            r.update({"patient_id": pid, "hn": hn, "registry_name": name, "how": how, "ratio": ratio})
            if not r.get("sex") and g:
                r["sex"] = "F" if str(g).upper().startswith(("F", "ห")) else "M"
        else:
            r.update({"patient_id": None, "hn": r.get("hn") or "", "registry_name": None, "how": "none", "ratio": None})
        if isinstance(r.get("assessed_at"), date):
            r["assessed_at"] = r["assessed_at"].isoformat()
        out.append(r)
    # duplicates (same patient + date already stored)
    pids = [r["patient_id"] for r in out if r["patient_id"]]
    existing = set()
    if pids:
        cur.execute("SELECT patient_id, assessed_at FROM hd_fluid_assessments WHERE patient_id = ANY(%s)", (pids,))
        existing = {(p, d.isoformat()) for p, d in cur.fetchall()}
    for r in out:
        r["duplicate"] = bool(r["patient_id"] and (r["patient_id"], r.get("assessed_at")) in existing)
        r["flags"] = flags(r)
    return out


def insert(cur, rows, confirmed_by="", source_file=""):
    """Insert confirmed rows (skips duplicates / unmatched) and remember device_id ↔ patient. → counts"""
    ins = skip = 0
    for r in rows:
        if not r.get("patient_id") or not r.get("assessed_at") or r.get("duplicate"):
            skip += 1
            continue
        cur.execute("""INSERT INTO hd_fluid_assessments (patient_id, hn, assessed_at, device, device_id, measured_name,
                       weight_kg, height_cm, bmi, oh_pre_l, rel_oh_pct, oh_post_l, ufv_ml, lti, fti, v_urea_l, bp_pre,
                       source_file, confirmed_by, notes)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (r["patient_id"], str(r.get("hn") or "").upper(), r["assessed_at"], r.get("device") or "BCM",
                     r.get("device_id"), (r.get("measured_name") or "")[:100] or None,
                     r.get("weight_kg"), r.get("height_cm"), r.get("bmi"), r.get("oh_pre_l"), r.get("rel_oh_pct"),
                     r.get("oh_post_l"), r.get("ufv_ml"), r.get("lti"), r.get("fti"), r.get("v_urea_l"), r.get("bp_pre"),
                     (source_file or r.get("file_name") or "")[:120] or None, confirmed_by[:100] or None,
                     ("จับคู่โดย " + str(r.get("how")) + (f" ({r['ratio']})" if r.get("ratio") else ""))[:200]))
        ins += 1
        if r.get("device_id"):
            cur.execute("""INSERT INTO hd_fluid_device_map (device_id, patient_id, hn, measured_name, updated_at)
                           VALUES (%s,%s,%s,%s,NOW()) ON CONFLICT (device_id) DO UPDATE
                           SET patient_id = EXCLUDED.patient_id, hn = EXCLUDED.hn, measured_name = EXCLUDED.measured_name, updated_at = NOW()""",
                        (r["device_id"], r["patient_id"], str(r.get("hn") or "").upper(), (r.get("measured_name") or "")[:100] or None))
    return {"inserted": ins, "skipped": skip}


def _f(v):
    return float(v) if v is not None else None


def overview(cur, lim=None, today=None):
    """Per active patient: latest + previous assessment, DW vs BCM-normohydration weight, flags, due."""
    lim = lim or DEFAULT_LIMITS
    today = today or _thai_now().date()
    cur.execute("""SELECT id, hn, patient_name, gender, dry_weight FROM hd_patient_mapping WHERE is_active = TRUE ORDER BY patient_name""")
    pats = cur.fetchall()
    cur.execute("""SELECT patient_id, assessed_at, weight_kg, oh_pre_l, rel_oh_pct, lti, fti, v_urea_l, bp_pre
                   FROM hd_fluid_assessments ORDER BY patient_id, assessed_at DESC, id DESC""")
    by = {}
    for row in cur.fetchall():
        by.setdefault(row[0], []).append(row)
    out, stats = [], {"n": len(pats), "assessed": 0, "due": 0, "over_severe": 0, "over": 0, "under": 0, "lti_low": 0, "dw_diff": 0}
    for pid, hn, name, g, dw in pats:
        hist = by.get(pid, [])
        sex = "F" if str(g or "").upper().startswith(("F", "ห")) else "M"
        item = {"patient_id": pid, "hn": hn, "name": name, "dry_weight": _f(dw), "latest": None, "previous": None, "flags": [],
                "days_since": None, "dw_bcm": None, "dw_diff": None}
        if hist:
            la = hist[0]
            item["latest"] = {"date": la[1].isoformat(), "weight": _f(la[2]), "oh": _f(la[3]), "rel_oh": _f(la[4]), "lti": _f(la[5]),
                              "fti": _f(la[6]), "v_urea": _f(la[7]), "bp_pre": la[8]}
            if len(hist) > 1:
                pr = hist[1]
                item["previous"] = {"date": pr[1].isoformat(), "oh": _f(pr[3]), "rel_oh": _f(pr[4]), "lti": _f(pr[5])}
            item["days_since"] = (today - la[1]).days
            fl = flags({"oh_pre_l": _f(la[3]), "rel_oh_pct": _f(la[4]), "lti": _f(la[5]), "sex": sex}, lim)
            if item["days_since"] > lim["due_days"]:
                fl.append("due")
            if la[2] is not None and la[3] is not None:
                item["dw_bcm"] = round(float(la[2]) - float(la[3]), 1)
                if dw is not None:
                    item["dw_diff"] = round(item["dw_bcm"] - float(dw), 1)
                    if abs(item["dw_diff"]) >= lim["dw_diff"]:
                        stats["dw_diff"] += 1
            item["flags"] = fl
            stats["assessed"] += 1
            for k in ("over_severe", "over", "under", "lti_low", "due"):
                if k in fl:
                    stats[k] += 1
        else:
            item["flags"] = ["due"]
            stats["due"] += 1
        out.append(item)
    order = {"over_severe": 0, "under": 1, "over": 2, "lti_low": 3, "due": 4}
    out.sort(key=lambda x: (min([order.get(f, 9) for f in x["flags"]] or [9]), -(x["latest"] or {}).get("oh") if x["latest"] and x["latest"].get("oh") is not None else 0))
    return {"patients": out, "stats": stats, "limits": lim, "today": today.isoformat()}


def latest_for(cur, patient_id, lim=None):
    """Latest assessment for one patient (for UF Planner / bed card) or None."""
    lim = lim or DEFAULT_LIMITS
    cur.execute("""SELECT assessed_at, weight_kg, oh_pre_l, rel_oh_pct, lti, fti, v_urea_l FROM hd_fluid_assessments
                   WHERE patient_id = %s ORDER BY assessed_at DESC, id DESC LIMIT 1""", (patient_id,))
    r = cur.fetchone()
    if not r:
        return None
    d = {"date": r[0].isoformat(), "days_ago": (_thai_now().date() - r[0]).days, "weight": _f(r[1]), "oh": _f(r[2]),
         "rel_oh": _f(r[3]), "lti": _f(r[4]), "fti": _f(r[5]), "v_urea": _f(r[6])}
    d["flags"] = flags({"oh_pre_l": d["oh"], "rel_oh_pct": d["rel_oh"], "lti": d["lti"]}, lim)
    d["dw_bcm"] = round(d["weight"] - d["oh"], 1) if d["weight"] is not None and d["oh"] is not None else None
    return d


def series_for(cur, patient_id):
    cur.execute("""SELECT assessed_at, weight_kg, oh_pre_l, rel_oh_pct, lti, fti, v_urea_l, bp_pre, source_file
                   FROM hd_fluid_assessments WHERE patient_id = %s ORDER BY assessed_at ASC, id ASC""", (patient_id,))
    return [{"date": r[0].isoformat(), "weight": _f(r[1]), "oh": _f(r[2]), "rel_oh": _f(r[3]), "lti": _f(r[4]), "fti": _f(r[5]),
             "v_urea": _f(r[6]), "bp_pre": r[7], "source": r[8]} for r in cur.fetchall()]


def uf_hint(latest, lim=None):
    """One plain-Thai line for the UF Planner from the latest BCM (or None)."""
    if not latest or latest.get("oh") is None:
        return None
    lim = lim or DEFAULT_LIMITS
    oh, ago = latest["oh"], latest["days_ago"]
    when = f"{ago} วันก่อน" if ago < 60 else f"{ago // 30} เดือนก่อน"
    if "over_severe" in latest["flags"]:
        return f"💧 BCM {when}: น้ำสะสม +{oh:.1f} L ({latest.get('rel_oh') or 0:.0f}% ECW) — ยังมีน้ำเกินมาก ควรทยอยลด DW/เพิ่ม UF ทีละรอบ (≤0.5 kg/รอบ) และทบทวนจำกัดน้ำ-เกลือ"
    if "over" in latest["flags"]:
        return f"💧 BCM {when}: น้ำสะสม +{oh:.1f} L — ดึงถึง DW ได้ตามแผน พิจารณาลด DW เล็กน้อยในรอบถัด ๆ ไป"
    if "under" in latest["flags"]:
        return f"💧 BCM {when}: น้ำขาด {oh:.1f} L — เสี่ยง IDH/ตะคริว ไม่ควรดึงถึง DW เดิม พิจารณาเพิ่ม DW"
    return f"💧 BCM {when}: สารน้ำปกติ ({oh:+.1f} L) — DW ปัจจุบันเหมาะสม"


def line_text(ov):
    s = ov["stats"]
    L = [f"💧 สรุปประเมินสารน้ำ (BCM) — ตรวจแล้ว {s['assessed']}/{s['n']} คน",
         f"🔴 น้ำเกินมาก {s['over_severe']} · 🟠 น้ำเกิน {s['over']} · 🔵 น้ำขาด {s['under']} · 🍗 LTI ต่ำ {s['lti_low']}",
         f"DW ต่างจาก BCM ≥{ov['limits']['dw_diff']} kg: {s['dw_diff']} คน → ดูใน Autopilot/Fluid Status",
         f"⚪ ครบกำหนดตรวจ/ยังไม่เคยตรวจ: {s['due']} คน"]
    sev = [p for p in ov["patients"] if "over_severe" in p["flags"]][:6]
    if sev:
        L.append("น้ำเกินมาก: " + ", ".join(f"{p['hn']} {p['name'].split()[0] if p['name'] else ''} +{p['latest']['oh']:.1f}L" for p in sev))
    return "\n".join(L)
