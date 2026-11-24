"""Group assistant — "@รักษ์ไต …" in the nurse LINE group (ว้าว #8).

Rule-based, read-only answers from the HD database. No external AI, no
patient data leaves the system (the reply goes back into the same staff
group the question came from). It never changes anything.

Commands (prefix @รักษ์ไต / รักษ์ไต / @raktai, case-insensitive):
    เตียง | ว่าง                 bed status right now
    วันนี้ | ตาราง               today's sessions by shift
    พรุ่งนี้                      who is scheduled tomorrow (schedule_days)
    alert | แจ้งเตือน            today's alerts still without a physician order
    esa                          ESA proposals waiting for approval
    น้ำ                          last water-quality test
    HN 1234 | <ชื่อ>              patient card (access, DW, ESA, last HD, labs)
    help | ช่วย                  this list
"""
import re
from datetime import datetime, timedelta, timezone

PREFIX_RE = re.compile(r"^\s*@?\s*(รักษ์ไต|raktai)\s*[,:：]?\s*", re.I)
DAY_LETTER = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}
DAY_TH = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
SHIFT_TH = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}
ST_TH = {"scheduled": "รอ", "in_progress": "กำลังฟอก", "completed": "เสร็จ", "cancelled": "ยกเลิก"}

HELP = ("🤖 รักษ์ไต ช่วยตอบได้ (พิมพ์ @รักษ์ไต ตามด้วย)\n"
        "• เตียง — สถานะเตียงตอนนี้\n• วันนี้ — รอบฟอกวันนี้\n• พรุ่งนี้ — ใครมีนัดพรุ่งนี้\n"
        "• alert — แจ้งเตือนที่ยังไม่มีคำสั่งแพทย์\n• esa — รายการ ESA รออนุมัติ\n• น้ำ — ผลตรวจน้ำล่าสุด\n"
        "• HN 1234 หรือ ชื่อผู้ป่วย — ข้อมูลผู้ป่วย\n"
        "• รหัสญาติ <HN> — ออกรหัสให้ญาติรับสรุปหลังฟอก · ยกเลิกญาติ <HN>\n"
        "• ลิงก์อาหาร <HN> — ออกลิงก์โค้ชอาหาร (ถ่ายรูปถาม AI) ส่งต่อผู้ป่วย\n"
        "• แล็บบ้าน <HN> — ดูตัวอย่างแล็บภาษาบ้าน · ส่งแล็บบ้าน <HN> — ส่งเข้า LINE ผู้ป่วย\n"
        "• ยา <HN> — รายการยาปัจจุบัน + แพ้ยา\n"
        "• ย้อน เตียง 12 เมื่อวาน / ย้อน HN 1234 ล่าสุด — 📼 ย้อนรอบฟอก (ถ้าเปิดใช้)\n\n"
        "ตอบจากข้อมูลในระบบเท่านั้น ไม่สั่งการรักษา")


def _thai_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)


def match(text):
    """Return the command body if text is addressed to the assistant, else None."""
    if not text:
        return None
    m = PREFIX_RE.match(text)
    if not m:
        return None
    return text[m.end():].strip()


def _q(cur, sql, params=()):
    try:
        cur.execute("SAVEPOINT ga")
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT ga")
        return rows
    except Exception:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT ga")
        except Exception:
            pass
        return []


def _beds(cur, today):
    rows = _q(cur, """SELECT s.bed_number, p.patient_name, p.hn, s.status, s.started_at, COALESCE(s.shift,'')
                      FROM hd_sessions s LEFT JOIN hd_patient_mapping p ON p.id = s.patient_id
                      WHERE s.session_date = %s AND s.status IN ('scheduled','in_progress')
                      ORDER BY s.bed_number""", (today,))
    if not rows:
        return "🛏 ตอนนี้ไม่มีผู้ป่วยบนเตียง (ไม่มีรอบที่รอ/กำลังฟอก)"
    L = [f"🛏 เตียง ณ {_thai_now():%H:%M} — กำลังฟอก {sum(1 for r in rows if r[3]=='in_progress')} · รอ {sum(1 for r in rows if r[3]=='scheduled')}"]
    for b, name, hn, st, started, sh in rows[:20]:
        t = f" เริ่ม {(started + timedelta(hours=7)):%H:%M}" if started and st == "in_progress" else ""
        L.append(f"• เตียง {b}: {name or '-'} (HN {hn or '-'}) {ST_TH.get(st, st)}{t}")
    return "\n".join(L)


def _today(cur, day, label="วันนี้"):
    rows = _q(cur, """SELECT COALESCE(s.shift,'morning'), s.bed_number, p.patient_name, p.hn, s.status
                      FROM hd_sessions s LEFT JOIN hd_patient_mapping p ON p.id = s.patient_id
                      WHERE s.session_date = %s ORDER BY 1, s.bed_number""", (day,))
    if not rows:
        return f"📅 {label} ยังไม่มีรอบฟอกในระบบ"
    L = [f"📅 รอบฟอก{label} {day.day}/{day.month}/{day.year + 543} — {len(rows)} รอบ"]
    cur_shift = None
    for sh, b, name, hn, st in rows:
        if sh != cur_shift:
            cur_shift = sh
            L.append(f"\n{SHIFT_TH.get(sh, sh)}:")
        L.append(f"  เตียง {b} {name or '-'} ({ST_TH.get(st, st)})")
    return "\n".join(L)


def _tomorrow(cur, today):
    tm = today + timedelta(days=1)
    letter = DAY_LETTER[tm.weekday()]
    rows = _q(cur, """SELECT patient_name, hn, vascular_access FROM hd_patient_mapping
                      WHERE is_active = TRUE AND schedule_days ILIKE %s ORDER BY patient_name""", (f"%{letter}%",))
    if not rows:
        return f"📅 พรุ่งนี้ (วัน{DAY_TH[tm.weekday()]}) ยังไม่มีผู้ป่วยที่ตั้งตารางไว้"
    L = [f"📅 พรุ่งนี้ วัน{DAY_TH[tm.weekday()]} {tm.day}/{tm.month} — {len(rows)} คน (ตามตารางประจำ)"]
    for name, hn, va in rows[:30]:
        L.append(f"• {name} (HN {hn}){(' · ' + va) if va else ''}")
    if len(rows) > 30:
        L.append(f"… อีก {len(rows) - 30} คน")
    return "\n".join(L)


def _alerts(cur, today):
    rows = _q(cur, """SELECT a.severity::text, a.title, s.bed_number, p.patient_name, a.physician_order, a.created_at
                      FROM hd_clinical_alerts a JOIN hd_sessions s ON s.id = a.session_id
                      LEFT JOIN hd_patient_mapping p ON p.id = s.patient_id
                      WHERE s.session_date = %s ORDER BY a.created_at DESC""", (today,))
    if not rows:
        return "✅ วันนี้ยังไม่มี alert"
    pend = [r for r in rows if not r[4] and r[0] in ("critical", "warning")]
    L = [f"🚨 Alert วันนี้ {len(rows)} รายการ — ยังไม่มีคำสั่งแพทย์ {len(pend)}"]
    for sev, title, bed, name, order, ts in rows[:10]:
        ico = "🔴" if sev == "critical" else ("🟠" if sev == "warning" else "🔵")
        L.append(f"{ico} {(ts + timedelta(hours=7)):%H:%M} เตียง {bed} {name or ''} — {title}{' ✓' if order else ' ⏳'}")
    return "\n".join(L)


def _esa(cur):
    rows = _q(cur, """SELECT p.patient_name, e.hn, e.hb, e.action, e.proposed_dose FROM hd_esa_proposals e
                      JOIN hd_patient_mapping p ON p.id = e.patient_id WHERE e.status = 'pending' ORDER BY e.hb""")
    if not rows:
        return "💊 ไม่มี ESA รออนุมัติ"
    act = {"increase": "เพิ่ม", "decrease": "ลด", "hold": "งด", "resume": "กลับมาให้", "review": "ทบทวน"}
    L = [f"💊 ESA รอแพทย์อนุมัติ {len(rows)} คน"]
    for name, hn, hb, a, dose in rows[:10]:
        L.append(f"• {name} (HN {hn}) Hb {float(hb):.1f} → {act.get(a, a)} {dose or ''}".rstrip())
    return "\n".join(L)


def _water(cur, today):
    parts = []
    try:
        from .water_check import status_line
        cur.execute("SAVEPOINT ga_w")
        sl = status_line(cur, 7, today)
        cur.execute("RELEASE SAVEPOINT ga_w")
        if sl:
            parts.append(sl)
    except Exception:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT ga_w")
        except Exception:
            pass
    lab = _water_lab(cur, today)
    parts.append(lab)
    parts.append("บันทึกใหม่: ส่งรูปแล้วพิมพ์ น้ำRO H 0 F 0.1 T 0")
    return "\n".join(parts)


def _water_lab(cur, today):
    rows = _q(cur, """SELECT test_date, test_type, result_value, unit, pass_fail FROM hd_water_quality
                      ORDER BY test_date DESC, id DESC LIMIT 4""")
    if not rows:
        return "🧫 ผลแล็บน้ำ (รายเดือน): ยังไม่มีในระบบ"
    d = rows[0][0]
    days = (today - d).days
    res = "; ".join(f"{t} {v or ''}{(' ' + u) if u else ''}{' ✓' if pf == 'pass' else (' ✗' if pf == 'fail' else '')}".strip()
                    for _, t, v, u, pf in rows if _ == d)
    return f"🧫 ผลแล็บน้ำล่าสุด {d.day}/{d.month}/{d.year + 543} ({days} วันก่อน){' — ' + res if res else ''}" + \
        ("\n⚠️ เกิน 30 วันแล้ว" if days > 30 else "")


def _patient(cur, key, today):
    m = re.search(r"hn\s*[:：]?\s*([0-9A-Za-z-]+)", key, re.I)
    if m:
        rows = _q(cur, """SELECT id, hn, patient_name, vascular_access, dry_weight, esa_drug, esa_dose, schedule_days
                          FROM hd_patient_mapping WHERE UPPER(hn) = %s""", (m.group(1).upper(),))
    else:
        rows = _q(cur, """SELECT id, hn, patient_name, vascular_access, dry_weight, esa_drug, esa_dose, schedule_days
                          FROM hd_patient_mapping WHERE is_active = TRUE AND patient_name ILIKE %s
                          ORDER BY patient_name LIMIT 5""", (f"%{key}%",))
    if not rows:
        return None
    if len(rows) > 1:
        return "พบหลายคน ระบุให้ชัดขึ้นหรือใช้ HN:\n" + "\n".join(f"• {r[2]} (HN {r[1]})" for r in rows)
    pid, hn, name, va, dw, drug, dose, days = rows[0]
    L = [f"👤 {name} (HN {hn})", f"เส้นฟอก: {va or '-'} · DW {dw or '-'} kg · ตาราง {days or '-'}",
         f"ESA: {drug or '-'} {dose or ''}".rstrip()]
    last = _q(cur, """SELECT session_date, status, bed_number, post_weight, pre_weight FROM hd_sessions
                      WHERE patient_id = %s ORDER BY session_date DESC, id DESC LIMIT 1""", (pid,))
    if last:
        d, st, b, post, pre = last[0]
        w = f" · น้ำหนัก {pre or '-'}→{post or '-'}" if (pre or post) else ""
        L.append(f"ฟอกล่าสุด {d.day}/{d.month}/{d.year + 543} เตียง {b} ({ST_TH.get(st, st)}){w}")
    lab = _q(cur, """SELECT lab_date, hb, k, phos, ca, albumin, ktv FROM hd_labs
                     WHERE patient_id = %s ORDER BY lab_date DESC NULLS LAST LIMIT 1""", (pid,))
    if lab:
        d, hb, k, p, ca, alb, ktv = lab[0]
        vals = [f"Hb {float(hb):.1f}" if hb is not None else None, f"K {float(k):.1f}" if k is not None else None,
                f"P {float(p):.1f}" if p is not None else None, f"Ca {float(ca):.1f}" if ca is not None else None,
                f"Alb {float(alb):.1f}" if alb is not None else None, f"Kt/V {float(ktv):.2f}" if ktv is not None else None]
        L.append(f"แล็บ {d.day}/{d.month}/{d.year + 543}: " + " · ".join(v for v in vals if v))
    pend = _q(cur, """SELECT a.title FROM hd_clinical_alerts a JOIN hd_sessions s ON s.id = a.session_id
                      WHERE s.patient_id = %s AND a.physician_order IS NULL AND a.severity::text IN ('critical','warning')
                      AND s.session_date >= %s ORDER BY a.created_at DESC LIMIT 3""", (pid, (today - timedelta(days=7)).isoformat()))
    if pend:
        L.append("⏳ alert รอคำสั่ง: " + "; ".join(r[0] for r in pend))
    esa = _q(cur, "SELECT action, proposed_dose FROM hd_esa_proposals WHERE patient_id = %s AND status = 'pending' LIMIT 1", (pid,))
    if esa:
        L.append(f"💊 ESA รออนุมัติ: {esa[0][0]} {esa[0][1] or ''}".rstrip())
    return "\n".join(L)


def answer(conn, body, base_url=""):
    """Answer one command. Returns the reply text (always non-empty)."""
    today = _thai_now().date()
    b = (body or "").strip()
    low = b.lower()
    with conn.cursor() as cur:
        if not b or low in ("help", "ช่วย", "ช่วยด้วย", "?"):
            return HELP
        if low.startswith("รหัสญาติ") or low.startswith("ยกเลิกญาติ"):
            from .patient_engage import family_code_command
            return family_code_command(cur, conn, b)
        # link สะกดได้หลายแบบ: ลิงก์/ลิงค์/ลิ้งก์/ลิ้งค์/ลิ้ง/ลิง — รับทุกแบบ รวมถึง "อาหาร <HN>" เฉย ๆ
        if re.match(r"^(ลิ้?ง[กค]?์?\s*อาหาร|โค้ชอาหาร|อาหาร|food)", low):
            from .patient_engage import food_link_command
            return food_link_command(cur, b)
        if low.startswith("แล็บบ้าน") or low.startswith("ส่งแล็บบ้าน") or low.startswith("แลบบ้าน") or low.startswith("ส่งแลบบ้าน"):
            from .patient_engage import lab_friendly_command
            return lab_friendly_command(cur, b)
        if re.match(r"^ยา\s*\d", low):
            from .medications import meds_list_command
            return meds_list_command(cur, b)
        if any(w in low for w in ("เตียง", "ว่าง", "bed")):
            return _beds(cur, today)
        if any(w in low for w in ("พรุ่งนี้", "tomorrow")):
            return _tomorrow(cur, today)
        if any(w in low for w in ("วันนี้", "ตาราง", "today", "schedule")):
            return _today(cur, today)
        if any(w in low for w in ("alert", "แจ้งเตือน", "เตือน")):
            return _alerts(cur, today)
        if low.startswith("esa") or "อีเอสเอ" in low:
            return _esa(cur)
        if low in ("น้ำ", "ตรวจน้ำ", "water") or low.startswith("น้ำ"):
            return _water(cur, today)
        # 📼 ย้อนรอบฟอก (Kidney Black Box) — 24 พ.ย. 2569 · ปิดเป็นค่าเริ่มต้น (blackbox_enabled)
        if low.startswith("ย้อน") or low.startswith("กล่องดำ") or low.startswith("blackbox"):
            from .blackbox import line_command as _bb_cmd
            push_fn, ids, gsv = None, [], None
            try:
                from flask import current_app
                from .blueprint import get_setting_value as gsv
                push_fn = current_app.config.get("LINE_PUSH_FN")
                raw_ids = gsv("alert_recipients", "") or gsv("physician_line_id", "")
                ids = [x.strip() for x in (raw_ids or "").split(",") if x.strip()]
            except Exception:
                pass
            return _bb_cmd(cur, b, today, base_url=base_url, push_fn=push_fn, physician_ids=ids, get_setting=gsv)
        r = _patient(cur, b, today)
        if r:
            return r
    return f"ไม่พบ \"{b[:40]}\" — ลองพิมพ์ @รักษ์ไต help"
