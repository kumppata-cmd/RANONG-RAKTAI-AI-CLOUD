"""Morning Brief → physician LINE at 07:30 Thai time (B7).

Read-only summary of what the physician needs before the first round:
today's load, yesterday's alerts still without an order, dialyzers near
max reuse, low Hb, overdue water tests, licences about to expire.
Every block is independent — a failing query is skipped, never fatal.
"""
import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

THAI_DAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
THAI_MONTHS = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
DAY_LETTER = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}
SHIFT_TH = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}


def _thai_date(d):
    return f"วัน{THAI_DAYS[d.weekday()]}ที่ {d.day} {THAI_MONTHS[d.month]} {d.year + 543}"


def _q(cur, sql, params=()):
    """Run one query inside a savepoint; return rows or [] on error."""
    try:
        cur.execute("SAVEPOINT mb")
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT mb")
        return rows
    except Exception as e:
        logger.warning("morning_brief query skipped: %s", str(e)[:120])
        try:
            cur.execute("ROLLBACK TO SAVEPOINT mb")
        except Exception:
            pass
        return []


def collect(conn, today=None):
    """Gather the brief's data. Returns a dict of lists/counts."""
    today = today or (datetime.now(timezone.utc) + timedelta(hours=7)).date()
    yesterday = today - timedelta(days=1)
    out = {"date": today, "today_patients": 0, "shifts": {}, "yesterday_alerts": [],
           "no_order": [], "dialyzer": [], "low_hb": [], "water_overdue": None,
           "expiring": [], "incidents_open": 0}
    with conn.cursor() as cur:
        letter = DAY_LETTER[today.weekday()]
        r = _q(cur, "SELECT COUNT(*) FROM hd_patient_mapping WHERE is_active = TRUE AND schedule_days ILIKE %s",
               (f"%{letter}%",))
        out["today_patients"] = r[0][0] if r else 0
        for row in _q(cur, """SELECT COALESCE(shift,'morning'), COUNT(*) FROM hd_sessions
                              WHERE session_date = %s AND status IN ('scheduled','in_progress','completed')
                              GROUP BY 1""", (today.isoformat(),)):
            out["shifts"][row[0]] = row[1]
        # Yesterday's alerts (all) + those still without a physician order
        out["yesterday_alerts"] = _q(cur, """
            SELECT a.severity::text, a.title, s.bed_number, p.patient_name, p.hn, a.physician_order, a.acknowledged
            FROM hd_clinical_alerts a
            JOIN hd_sessions s ON s.id = a.session_id
            LEFT JOIN hd_patient_mapping p ON p.id = s.patient_id
            WHERE s.session_date = %s
            ORDER BY CASE a.severity::text WHEN 'critical' THEN 0 ELSE 1 END, a.created_at""", (yesterday.isoformat(),))
        out["no_order"] = [a for a in out["yesterday_alerts"] if not a[5] and a[0] in ("critical", "warning")]
        out["dialyzer"] = _q(cur, """
            SELECT p.patient_name, p.hn, d.dialyzer_type, d.reuse_count, d.max_reuse
            FROM hd_dialyzer_tracking d JOIN hd_patient_mapping p ON p.id = d.patient_id
            WHERE d.status = 'active' AND d.max_reuse > 0 AND d.reuse_count >= d.max_reuse - 2
            ORDER BY (d.max_reuse - d.reuse_count), p.hn LIMIT 8""")
        out["low_hb"] = _q(cur, """
            SELECT DISTINCT ON (p.id) p.patient_name, p.hn, s.hb_value, s.session_date
            FROM hd_sessions s JOIN hd_patient_mapping p ON p.id = s.patient_id
            WHERE s.hb_value IS NOT NULL AND s.session_date >= %s
            ORDER BY p.id, s.session_date DESC""", ((today - timedelta(days=45)).isoformat(),))
        out["low_hb"] = sorted([r for r in out["low_hb"] if r[2] is not None and float(r[2]) < 9.0],
                               key=lambda r: float(r[2]))[:8]
        # Lab documents (hd_lab_docs): newest row per patient within 60 days
        out["lab_flags"] = []
        for r in _q(cur, """
            SELECT DISTINCT ON (l.patient_id) p.patient_name, p.hn, l.hb, l.k, l.phos, l.ca, l.hbsag,
                   l.lab_date
            FROM hd_labs l JOIN hd_patient_mapping p ON p.id = l.patient_id
            WHERE l.lab_date >= %s
            ORDER BY l.patient_id, l.lab_date DESC""",
                     ((today - timedelta(days=60)).isoformat(),)):
            from .lab_sync import flags_for
            fl = flags_for({"hb": r[2], "k": r[3], "phos": r[4], "ca": r[5], "hbsag": r[6]})
            fl = [f for f in fl if f["sev"] in ("critical", "warning")]
            if fl:
                out["lab_flags"].append((r[0], r[1], r[7], fl))
        out["lab_flags"].sort(key=lambda x: 0 if any(f["sev"] == "critical" for f in x[3]) else 1)
        # VA Watch (ว้าว #2): access trend from flowsheet medians
        out["va_watch"] = []
        try:
            cur.execute("SAVEPOINT mb_va")
            from .va_watch import collect as va_collect
            out["va_watch"] = va_collect(conn)
            cur.execute("RELEASE SAVEPOINT mb_va")
        except Exception as e:
            logger.warning("morning_brief va_watch skipped: %s", str(e)[:120])
            try:
                cur.execute("ROLLBACK TO SAVEPOINT mb_va")
            except Exception:
                pass
        # Daily RO water check (hardness / chlorine) — last entry + due?
        out["water_daily"] = None
        try:
            cur.execute("SAVEPOINT mb_wd")
            from .water_check import status_line
            interval = 7
            try:
                from .blueprint import get_setting_value
                interval = int(float(get_setting_value("water_interval_days", "7") or 7))
            except Exception:
                pass
            out["water_daily"] = status_line(cur, interval, today)
            cur.execute("RELEASE SAVEPOINT mb_wd")
        except Exception as e:
            logger.warning("morning_brief water_daily skipped: %s", str(e)[:120])
            try:
                cur.execute("ROLLBACK TO SAVEPOINT mb_wd")
            except Exception:
                pass
        r = _q(cur, "SELECT MAX(COALESCE(send_date, test_date)) FROM hd_water_quality")
        last_water = r[0][0] if r and r[0] else None
        if last_water is None:
            out["water_overdue"] = ("ไม่เคยบันทึก", None)
        else:
            days = (today - last_water).days
            if days > 30:
                out["water_overdue"] = (last_water, days)
        out["expiring"] = _q(cur, """
            SELECT section, item_key, expires_at FROM hd_clinic_profile
            WHERE expires_at IS NOT NULL AND expires_at <= %s
            ORDER BY expires_at LIMIT 8""", ((today + timedelta(days=60)).isoformat(),))
        r = _q(cur, "SELECT COUNT(*) FROM hd_incidents WHERE resolved_at IS NULL")
        out["incidents_open"] = r[0][0] if r else 0
    return out


def build_message(data, base_url=""):
    d = data["date"]
    L = [f"☀️ Morning Brief — {_thai_date(d)}", "━━━━━━━━━━━━"]
    sh = data["shifts"]
    sh_txt = " · ".join(f"{SHIFT_TH[k]} {sh.get(k, 0)}" for k in ("morning", "afternoon", "evening") if sh.get(k))
    L.append(f"🛏 ฟอกวันนี้ {data['today_patients']} คน" + (f" (จัดแล้ว: {sh_txt})" if sh_txt else " (ยังไม่จัดเตียง)"))

    ya = data["yesterday_alerts"]
    if ya:
        crit = sum(1 for a in ya if a[0] == "critical")
        L.append(f"\n🚨 Alert เมื่อวาน {len(ya)} รายการ (วิกฤต {crit})")
        for a in data["no_order"][:5]:
            L.append(f"  ⏳ เตียง {a[2]} {a[3] or ''} — {a[1]} (ยังไม่มีคำสั่งแพทย์)")
        if len(data["no_order"]) > 5:
            L.append(f"  … อีก {len(data['no_order']) - 5} รายการ")
    else:
        L.append("\n✅ เมื่อวานไม่มี alert")

    if data["low_hb"]:
        L.append(f"\n🩸 Hb < 9 ({len(data['low_hb'])} คน) — ทบทวน ESA")
        for r in data["low_hb"][:5]:
            L.append(f"  • {r[0]} (HN {r[1]}) Hb {float(r[2]):.1f}")

    if data.get("lab_flags"):
        L.append(f"\n🧪 ผลแล็บผิดปกติ ({len(data['lab_flags'])} คน) — จากเอกสารแล็บ")
        for name, hn, d0, fl in data["lab_flags"][:6]:
            L.append(f"  • {name} (HN {hn}) " + "; ".join(f["text"].split(" — ")[0] for f in fl))

    if data.get("va_watch"):
        from .va_watch import brief_lines
        L.extend(brief_lines(data["va_watch"]))

    if data["dialyzer"]:
        L.append(f"\n🔧 Dialyzer ใกล้ครบ reuse ({len(data['dialyzer'])})")
        for r in data["dialyzer"][:5]:
            L.append(f"  • {r[0]} (HN {r[1]}) {r[2] or ''} {r[3]}/{r[4]}")

    if data.get("water_daily"):
        L.append("\n" + data["water_daily"])

    wo = data["water_overdue"]
    if wo:
        if wo[1] is None:
            L.append("\n💧 ผลตรวจน้ำ: ยังไม่เคยบันทึกในระบบ")
        else:
            L.append(f"\n💧 ผลตรวจน้ำเกินกำหนด — ล่าสุด {wo[0].day}/{wo[0].month}/{wo[0].year + 543} ({wo[1]} วัน)")

    if data["expiring"]:
        L.append("\n📄 ใบอนุญาต/เอกสารใกล้หมดอายุ (60 วัน)")
        for r in data["expiring"][:5]:
            e = r[2]
            L.append(f"  • {r[1]} ({r[0]}) หมด {e.day}/{e.month}/{e.year + 543}")

    if data["incidents_open"]:
        L.append(f"\n⚠️ อุบัติการณ์ยังไม่ปิด {data['incidents_open']} เรื่อง")

    L.append("━━━━━━━━━━━━")
    if base_url:
        L.append(f"📊 Dashboard: {base_url}/hd/dashboard\n📒 ทะเบียนเมื่อวาน: {base_url}/hd/daily?date={(d - timedelta(days=1)).isoformat()}")
    return "\n".join(L)


def send_morning_brief(conn, push_fn, recipients, base_url="", today=None):
    """Build and push. Returns (sent_count, message)."""
    data = collect(conn, today)
    msg = build_message(data, base_url)
    sent = 0
    for uid in recipients:
        try:
            if push_fn(uid, msg) is not False:
                sent += 1
        except Exception as e:
            logger.warning("morning brief push failed: %s", e)
    return sent, msg
