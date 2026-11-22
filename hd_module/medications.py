"""ระบบยาครบวงจร — 22 พ.ย. 2569 (แพทย์สั่ง: พยาบาลแก้ได้+รอแพทย์รับทราบ, ข้อเสริม 1–4)

หลักความปลอดภัย (ตามกติการะบบ): AI อ่านรูปยา = เสนอฉบับร่างเท่านั้น (verify_status='ai_draft')
ยาเข้ารายการจริงเมื่อแพทย์/แอดมินกดยืนยัน · การแก้ของพยาบาลมีผลทันทีแต่ติดป้าย 'pending'
รอแพทย์รับทราบ · ไม่มีการลบจริง — หยุดยา = is_active FALSE + เหตุผล
"""
import json
import logging
import os
import re

logger = logging.getLogger("hd.medications")

# สถานะการยืนยัน
ST_AI = "ai_draft"       # จาก AI อ่านรูป — ยังไม่ถือเป็นยาปัจจุบันจนกว่าแพทย์ยืนยัน
ST_PENDING = "pending"   # พยาบาลเพิ่ม/แก้ — มีผลแล้ว รอแพทย์รับทราบ
ST_OK = "confirmed"

# ข้อ 3: ยากลุ่มต้องระวังในโรคไต (rule-based — เตือน ไม่บล็อก)
RENAL_CAUTION = (
    ("nsaid", "NSAIDs — เลี่ยงในผู้ป่วยไต"),
    ("ibuprofen", "NSAIDs — เลี่ยงในผู้ป่วยไต"),
    ("diclofenac", "NSAIDs — เลี่ยงในผู้ป่วยไต"),
    ("naproxen", "NSAIDs — เลี่ยงในผู้ป่วยไต"),
    ("mefenamic", "NSAIDs — เลี่ยงในผู้ป่วยไต"),
    ("ponstan", "NSAIDs — เลี่ยงในผู้ป่วยไต"),
    ("metformin", "ระวังใน ESRD — สะสม/lactic acidosis"),
    ("nitrofurantoin", "ห้ามใน GFR ต่ำ"),
    ("spironolactone", "ระวัง K สูงในผู้ป่วยฟอกไต"),
    ("kcl", "โพแทสเซียมเสริม — ระวัง K สูง"),
    ("potassium chloride", "โพแทสเซียมเสริม — ระวัง K สูง"),
    ("gentamicin", "aminoglycoside — ปรับขนาดตามการฟอก"),
    ("amikacin", "aminoglycoside — ปรับขนาดตามการฟอก"),
    ("vancomycin", "ปรับขนาดตามการฟอก/ระดับยา"),
    ("enoxaparin", "LMWH — สะสมในไตวาย ระวังเลือดออก"),
    ("colchicine", "ลดขนาดใน ESRD"),
    ("baclofen", "สะสมในไตวาย — ซึม/สับสน"),
    ("gabapentin", "ปรับขนาดตามการฟอก"),
    ("pregabalin", "ปรับขนาดตามการฟอก"),
    ("digoxin", "ระวังพิษยา — ติดตามระดับ/K"),
    ("alum", "ยาลดกรดมีอะลูมิเนียม — เลี่ยงสะสมในผู้ป่วยไต"),
)


def _norm(s):
    return re.sub(r"[^a-z0-9ก-๙]+", "", (s or "").lower())


def _drugkey(s):
    """ชื่อยาแบบตัดขนาด/หน่วยทิ้ง — ให้ 'Amlodipine 5 mg' ชนกับ 'Amlodipine 10 mg' ได้."""
    s = (s or "").lower()
    s = re.sub(r"\d+(\.\d+)?", " ", s)
    s = re.sub(r"\b(mg|mcg|g|ml|iu|u|unit|units|tab|tabs|cap|caps)\b", " ", s)
    s = re.sub(r"(มก|มล|เม็ด|แคปซูล|หน่วย|ยูนิต)", " ", s)
    return re.sub(r"[^a-zก-๙]+", "", s)


def med_warnings(cur, patient_id, drug_name, exclude_id=None):
    """ตรวจ 3 อย่าง: ยาซ้ำซ้อน · ตรงกับยาที่แพ้ · กลุ่มต้องระวังในโรคไต. คืน list[str]."""
    warns = []
    nd = _norm(drug_name)
    nk = _drugkey(drug_name)
    if not nd:
        return warns
    try:
        cur.execute("""SELECT id, drug_name FROM hd_medication_orders
                       WHERE patient_id = %s AND is_active = TRUE""", (patient_id,))
        for oid, dn in cur.fetchall():
            if exclude_id and oid == exclude_id:
                continue
            k2 = _drugkey(dn)
            if nk and k2 and (k2 == nk or nk in k2 or k2 in nk):
                warns.append(f"⚠️ อาจซ้ำซ้อนกับรายการเดิม: {dn}")
                break
        cur.execute("SELECT drug_allergies FROM hd_patient_mapping WHERE id = %s", (patient_id,))
        r = cur.fetchone()
        allergies = (r[0] or "") if r else ""
        for a in re.split(r"[,;/\n]+", allergies):
            na = _norm(a)
            if na and (na in nd or nd in na):
                warns.append(f"🚨 ผู้ป่วยแพ้ยา: {a.strip()} — ตรวจก่อนสั่ง!")
                break
    except Exception as e:
        logger.warning("med_warnings: %s", e)
    low = (drug_name or "").lower()
    for key, msg in RENAL_CAUTION:
        if key in low:
            warns.append(f"⚠️ {msg}")
            break
    return warns


def read_meds_photo(img_bytes):
    """AI อ่านรูปซองยา/ใบรายการยา → list[{drug, dose, frequency, route}] + err.
    ใช้โครงเดียวกับ Food Coach (ai_model + retry + รวมบล็อก text)."""
    import base64
    import time as _time
    import requests as _rq
    from .patient_engage import ai_model

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None, "no_api_key"
    # ย่อรูปฝั่ง server กันพลาด (ฝั่งเว็บย่อมาแล้ว)
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(img_bytes))
        im.thumbnail((1400, 1400))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=88)
        img_bytes = buf.getvalue()
    except Exception:
        pass
    prompt = (
        "คุณคือเภสัชกรผู้ช่วยอ่านฉลาก/ซองยา/ใบรายการยาในรูป ให้ดึงรายการยาทั้งหมดที่อ่านได้\n"
        "ตอบเป็น JSON array เท่านั้น ไม่มีข้อความอื่น รูปแบบ:\n"
        '[{"drug":"ชื่อยา+ความแรง เช่น Amlodipine 10 mg","dose":"จำนวนต่อครั้ง เช่น 1 เม็ด",'
        '"frequency":"วิธี/เวลา ภาษาไทย เช่น วันละครั้ง เช้า ก่อนอาหาร","route":"PO/IV/SC"}]\n"'
        "กติกา: อ่านเฉพาะที่เห็นชัด ห้ามเดา — ถ้าอ่านช่องไหนไม่ออกให้ใส่ \"\" · "
        "ถ้าไม่ใช่รูปยา/ไม่มียาเลย ตอบ []"
    )
    body = json.dumps({
        "model": ai_model(), "max_tokens": 2500,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(img_bytes).decode()}},
            {"type": "text", "text": prompt}]}],
    }).encode("utf-8")
    err = "empty"
    for attempt in (1, 2):
        try:
            resp = _rq.post("https://api.anthropic.com/v1/messages",
                            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                     "content-type": "application/json"},
                            data=body, timeout=45)
        except Exception as e:
            err = f"net:{type(e).__name__}"
            if attempt == 1:
                _time.sleep(1.5)
                continue
            return None, err
        if resp.status_code != 200:
            logger.warning("meds ocr HTTP %s: %s", resp.status_code, resp.text[:150])
            if resp.status_code == 400 and "credit balance" in (resp.text or ""):
                return None, "no_credit"
            err = f"api:{resp.status_code}"
            if attempt == 1 and resp.status_code in (429, 500, 502, 503, 529):
                _time.sleep(1.5)
                continue
            return None, err
        txt = "".join(b.get("text", "") for b in (resp.json().get("content") or [])
                      if b.get("type") == "text").strip()
        if txt:
            m = re.search(r"\[.*\]", txt, re.S)
            if not m:
                return [], None
            try:
                items = json.loads(m.group(0))
            except Exception:
                return None, "parse"
            out = []
            for it in items[:25]:
                drug = str(it.get("drug", "")).strip()[:100]
                if drug:
                    out.append({"drug": drug,
                                "dose": str(it.get("dose", "")).strip()[:50],
                                "frequency": str(it.get("frequency", "")).strip()[:50],
                                "route": (str(it.get("route", "PO")).strip() or "PO")[:20]})
            return out, None
        if attempt == 1:
            _time.sleep(1.5)
    return None, err


MED_SEEN_SQL = """
CREATE TABLE IF NOT EXISTS hd_med_doc_seen (
    doc_id TEXT PRIMARY KEY,
    hn TEXT,
    n_items INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
)
"""


def sync_med_photos(conn, storage, doc_index, push_fn=None, days=3, max_docs=60):
    """รูปยาที่ส่งเข้า LINE OA (classifier จัดหมวด rx/medication แล้ว) → AI อ่าน
    → สร้างรายการ 'ai_draft' ของ HN นั้น รอแพทย์ยืนยัน → แจ้งกลุ่มพยาบาล.
    ทำงานใน hd scheduler ทุก 15 นาที — idempotent ด้วยตาราง hd_med_doc_seen.
    AI ล้มทั้งระบบ (เครดิต/คีย์) = ไม่ mark seen เพื่อให้รอบหน้าลองใหม่."""
    from datetime import timedelta
    from .lab_sync import _doc_hn, _thai_today
    stats = {"scanned": 0, "drafted": 0, "no_hn": 0, "skipped": 0, "errors": 0, "notified": 0}
    if doc_index is None or storage is None:
        return stats
    try:
        doc_index._ensure_fresh()
        docs = [d for d in list(doc_index._docs) if d.get("category") in ("rx", "medication")]
    except Exception as e:
        logger.warning("med_photo_sync: doc index unavailable: %s", e)
        return stats
    cutoff = (_thai_today() - timedelta(days=days)).isoformat()
    docs.sort(key=lambda d: (d.get("submitted_at") or d.get("received_at") or d.get("upload_date") or ""),
              reverse=True)
    with conn.cursor() as cur:
        cur.execute(MED_SEEN_SQL)
        cur.execute("SELECT doc_id FROM hd_med_doc_seen")
        seen = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT id, hn FROM hd_patient_mapping")
        pid_by_hn = {str(r[1]).strip().upper(): r[0] for r in cur.fetchall()}
        for d in docs[:max_docs]:
            doc_id = d.get("document_id") or ""
            if not doc_id or doc_id in seen:
                stats["skipped"] += 1
                continue
            ddate = (d.get("submitted_at") or d.get("received_at") or d.get("upload_date") or "")[:10]
            if ddate and ddate < cutoff:
                continue
            path = d.get("gcs_object_path") or ""
            if not path.lower().endswith((".jpg", ".jpeg", ".png")):
                cur.execute("INSERT INTO hd_med_doc_seen (doc_id, hn, n_items) VALUES (%s,%s,-2) "
                            "ON CONFLICT DO NOTHING", (doc_id, ""))
                continue
            stats["scanned"] += 1
            hn = _doc_hn(d)
            if not hn or hn not in pid_by_hn:
                stats["no_hn"] += 1
                cur.execute("INSERT INTO hd_med_doc_seen (doc_id, hn, n_items) VALUES (%s,%s,-1) "
                            "ON CONFLICT DO NOTHING", (doc_id, hn or ""))
                continue
            try:
                raw = storage.get(path)
            except Exception:
                raw = None
            if not raw:
                stats["errors"] += 1
                cur.execute("INSERT INTO hd_med_doc_seen (doc_id, hn, n_items) VALUES (%s,%s,-3) "
                            "ON CONFLICT DO NOTHING", (doc_id, hn))
                continue
            items, err = read_meds_photo(raw)
            if items is None:
                # ระบบ AI ล่มทั้งกระดาน — อย่า mark seen, เลิกทั้งรอบ รอรอบหน้า
                if err in ("no_api_key", "no_credit") or str(err).startswith(("net:", "api:")):
                    logger.warning("med_photo_sync: AI down (%s) — จะลองใหม่รอบหน้า", err)
                    break
                stats["errors"] += 1
                cur.execute("INSERT INTO hd_med_doc_seen (doc_id, hn, n_items) VALUES (%s,%s,-4) "
                            "ON CONFLICT DO NOTHING", (doc_id, hn))
                continue
            pid = pid_by_hn[hn]
            for it in items:
                warns = med_warnings(cur, pid, it["drug"])
                cur.execute("""
                    INSERT INTO hd_medication_orders
                        (patient_id, drug_name, dose, route, frequency, drug_category,
                         is_active, verify_status, source, notes, updated_at)
                    VALUES (%s,%s,%s,%s,%s,'other',FALSE,%s,'line_photo',%s,NOW())
                """, (pid, it["drug"], it["dose"], it["route"], it["frequency"],
                      ST_AI, ("; ".join(warns))[:300] or None))
            cur.execute("INSERT INTO hd_med_doc_seen (doc_id, hn, n_items) VALUES (%s,%s,%s) "
                        "ON CONFLICT DO NOTHING", (doc_id, hn, len(items)))
            seen.add(doc_id)
            stats["drafted"] += len(items)
            if push_fn and items:
                try:
                    names = " · ".join(i["drug"] for i in items[:5])
                    more = f" และอีก {len(items) - 5} ตัว" if len(items) > 5 else ""
                    push_fn(f"📸💊 รูปยาใหม่จาก LINE — HN {hn}\n"
                            f"AI อ่านได้ {len(items)} รายการ: {names}{more}\n"
                            f"👉 แดชบอร์ด → แท็บ 💊 ยา → จัดการยา → กด ✔ ยืนยันทีละรายการ (แพทย์)")
                    stats["notified"] += 1
                except Exception as pe:
                    logger.warning("med_photo_sync push: %s", pe)
    try:
        conn.commit()
    except Exception:
        pass
    return stats


def meds_list_command(cur, body):
    """คำสั่งกลุ่มพยาบาล: 'ยา <HN>' — รายการยาปัจจุบันแบบย่อ. คืนข้อความตอบเสมอ."""
    m = re.search(r"(\d{3,9})", body or "")
    if not m:
        return "ใช้: ยา <HN> เช่น ยา 68443"
    hn = m.group(1)
    cur.execute("""SELECT p.patient_name, p.drug_allergies, p.id
                   FROM hd_patient_mapping p WHERE p.hn = %s""", (hn,))
    pr = cur.fetchone()
    if not pr:
        return f"ไม่พบ HN {hn} ในทะเบียนผู้ป่วย"
    cur.execute("""SELECT drug_name, dose, frequency, intra_hd, COALESCE(verify_status,'confirmed')
                   FROM hd_medication_orders WHERE patient_id = %s AND is_active = TRUE
                   ORDER BY intra_hd DESC, drug_name""", (pr[2],))
    rows = cur.fetchall()
    lines = [f"💊 ยาปัจจุบัน — {pr[0] or hn} (HN {hn})"]
    if pr[1]:
        lines.append(f"🚨 แพ้ยา: {pr[1]}")
    if not rows:
        lines.append("— ยังไม่มีทะเบียนยา (เพิ่มที่แดชบอร์ด แท็บ 💊 ยา)")
    for r in rows[:25]:
        tag = " 💉ระหว่างฟอก" if r[3] else ""
        pend = " ⏳รอยืนยัน" if r[4] in (ST_PENDING, ST_AI) else ""
        lines.append(f"• {r[0]} {r[1] or ''} {r[2] or ''}{tag}{pend}".rstrip())
    if len(rows) > 25:
        lines.append(f"… และอีก {len(rows) - 25} รายการ")
    return "\n".join(lines)


def build_med_list_pdf(patient, orders, allergies):
    """ข้อ 4: ใบรายการยาปัจจุบัน (A4) ให้ผู้ป่วยพกไป รพ.อื่น. คืน bytes."""
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as _canvas
    from .hd_worksheet import _register_fonts, _thai_date
    from datetime import datetime, timezone, timedelta

    _register_fonts()
    buf = io.BytesIO()
    c = _canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    y = H - 50
    c.setFont("Loma-Bold", 16)
    c.drawCentredString(W / 2, y, "รายการยาปัจจุบัน — คลินิกระนอง รักษ์ไต")
    y -= 22
    now_th = datetime.now(timezone(timedelta(hours=7)))
    c.setFont("Loma", 9)
    c.drawCentredString(W / 2, y, f"พิมพ์เมื่อ {_thai_date(now_th.date())} {now_th.strftime('%H:%M')} น. · เอกสารนี้ไม่ใช่ใบสั่งยา")
    y -= 26
    c.setFont("Loma-Bold", 12)
    c.drawString(50, y, f"ผู้ป่วย: {patient.get('name', '-')}   HN: {patient.get('hn', '-')}")
    y -= 16
    if allergies:
        c.setFont("Loma-Bold", 11)
        c.setFillColorRGB(0.8, 0, 0)
        c.drawString(50, y, f"🚨 แพ้ยา: {allergies}")
        c.setFillColorRGB(0, 0, 0)
        y -= 16
    y -= 8
    c.setFont("Loma-Bold", 9)
    xs = (50, 230, 320, 470)
    for x, h in zip(xs, ("ชื่อยา", "ขนาด/ครั้ง", "วิธีรับประทาน/เวลา", "หมายเหตุ")):
        c.drawString(x, y, h)
    y -= 4
    c.line(50, y, W - 50, y)
    y -= 14
    c.setFont("Loma", 9)
    for o in orders:
        if y < 70:
            c.showPage()
            _register_fonts()
            c.setFont("Loma", 9)
            y = H - 60
        tag = " (ระหว่างฟอก)" if o.get("intra_hd") else ""
        c.setFont("Loma-Bold", 9)
        c.drawString(xs[0], y, (o.get("drug", "") + tag)[:42])
        c.setFont("Loma", 9)
        c.drawString(xs[1], y, (o.get("dose") or "")[:18])
        c.drawString(xs[2], y, (o.get("frequency") or "")[:30])
        c.drawString(xs[3], y, (o.get("notes") or "")[:22])
        y -= 14
    if not orders:
        c.drawString(50, y, "— ไม่มีรายการยาที่ยืนยันแล้ว —")
    c.setFont("Loma", 7)
    c.drawCentredString(W / 2, 40, "มีข้อสงสัยเรื่องยา กรุณาปรึกษาแพทย์/พยาบาลคลินิกระนอง รักษ์ไต")
    c.save()
    return buf.getvalue()
