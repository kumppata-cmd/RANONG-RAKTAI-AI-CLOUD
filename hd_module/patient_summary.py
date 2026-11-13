"""Post-HD summary → the patient's own LINE (ว้าว #7).

Unlocked by the physician on 12 Nov 2026 for this feature only. What the
patient receives is deliberately plain-language and limited to their own
numbers: weight before/after vs dry weight, fluid removed, blood pressure
before/after, time on dialysis, which ESA was given (name only), the next
scheduled day and a small weight-trend bar chart drawn with Flex boxes
(no image, no public URL). Alerts, diagnoses, orders and lab values are
NOT sent. Nothing here changes any record.

Resolution of the LINE user id goes through the existing patient link
(HN registry → enrolled id); this module never stores LINE ids itself.
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DAY_LETTER = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}
DAY_TH = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
MONTH_TH = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
CLINIC_NAME = "คลินิกระนอง รักษ์ไต"


def _thai_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)


def _d(d):
    return f"{d.day} {MONTH_TH[d.month]} {d.year + 543}"


def next_schedule_day(schedule_days, today=None):
    """'จ,พ,ศ' → next date (after today) whose Thai letter is in the schedule, or None."""
    if not schedule_days:
        return None
    today = today or _thai_now().date()
    letters = [x.strip() for x in str(schedule_days).replace(" ", ",").replace("/", ",").split(",") if x.strip()]
    if not letters:
        return None
    for i in range(1, 8):
        d = today + timedelta(days=i)
        if DAY_LETTER[d.weekday()] in letters:
            return d
    return None


def gentle_notes(summary):
    """Plain-language, non-medical lifestyle notes (never diagnosis or orders)."""
    n = []
    pre, post, dw = summary.get("pre_weight"), summary.get("post_weight"), summary.get("dry_weight")
    try:
        if pre and dw and (pre - dw) / dw * 100 > 3.5:
            n.append("น้ำหนักก่อนฟอกครั้งนี้ขึ้นจากน้ำหนักแห้งค่อนข้างมาก ลองจำกัดน้ำดื่มและอาหารเค็มระหว่างรอบ")
        if post and dw and post > dw + 0.5:
            n.append("หลังฟอกยังมีน้ำเหลืออยู่บ้าง สังเกตอาการบวมหรือหายใจเหนื่อย")
        if (summary.get("esa_prescribed") or summary.get("esa_drug")) and not summary.get("esa_given"):
            n.append("ครั้งนี้ไม่ได้ฉีดยากระตุ้นเม็ดเลือด หากสงสัยสอบถามพยาบาลได้")
    except (TypeError, ZeroDivisionError):
        pass
    if not n:
        n.append("การฟอกครั้งนี้เรียบร้อยดี ดูแลเรื่องน้ำดื่มและอาหารต่อเนื่องนะคะ/ครับ")
    return n[:3]


def build_text(summary, history=None, today=None):
    """Fallback plain text (also used as Flex altText)."""
    s = summary
    today = today or _thai_now().date()
    L = [f"📋 สรุปการฟอกเลือดวันนี้ ({_d(today)})", f"🏥 {CLINIC_NAME}", "━━━━━━━━━━━━"]
    if s.get("pre_weight") and s.get("post_weight"):
        L.append(f"⚖️ น้ำหนัก {s['pre_weight']:.1f} → {s['post_weight']:.1f} kg" + (f" (น้ำหนักแห้ง {s['dry_weight']:.1f})" if s.get("dry_weight") else ""))
    if s.get("actual_uf"):
        L.append(f"💧 ดึงน้ำออก {s['actual_uf'] / 1000:.1f} ลิตร")
    if s.get("bp_pre") or s.get("bp_post"):
        L.append(f"❤️ ความดัน {s.get('bp_pre') or '-'} → {s.get('bp_post') or '-'}")
    L.append(f"⏱ ฟอก {s.get('duration_hours', 4)} ชั่วโมง")
    if s.get("esa_given"):
        L.append(f"💉 ยากระตุ้นเม็ดเลือด: {s.get('esa_drug_given') or 'ได้รับแล้ว'}")
    L.append("━━━━━━━━━━━━")
    for n in gentle_notes(s):
        L.append(f"💡 {n}")
    nxt = next_schedule_day(s.get("schedule_days"), today)
    if nxt:
        L.append(f"📅 นัดครั้งถัดไป วัน{DAY_TH[nxt.weekday()]}ที่ {_d(nxt)}")
    L.append("📞 มีอาการผิดปกติ ติดต่อคลินิกได้ทันที")
    return "\n".join(L)


def _bar_chart(history, dw):
    """Weight-trend bars from Flex boxes: history = [{d, pre, post}] oldest→newest."""
    pts = [h for h in (history or []) if h.get("pre")][-8:]
    if len(pts) < 2:
        return None
    vals = [h["pre"] for h in pts] + ([dw] if dw else [])
    lo, hi = min(vals) - 0.5, max(vals) + 0.5
    bars = []
    for h in pts:
        pct = int(max(8, min(100, (h["pre"] - lo) / (hi - lo) * 100)))
        over = bool(dw and h["pre"] > dw * 1.035)
        bars.append({"type": "box", "layout": "vertical", "flex": 1, "spacing": "xs", "contents": [
            {"type": "box", "layout": "vertical", "height": "60px", "justifyContent": "flex-end", "contents": [
                {"type": "box", "layout": "vertical", "height": f"{pct}%", "backgroundColor": "#E67E22" if over else "#3B82F6",
                 "cornerRadius": "2px", "contents": []}]},
            {"type": "text", "text": h["d"][5:10].replace("-", "/") if h.get("d") else "", "size": "xxs", "color": "#8A9BC0", "align": "center"}]})
    return {"type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm", "contents": [
        {"type": "text", "text": "น้ำหนักก่อนฟอก 8 ครั้งล่าสุด" + (f" · น้ำหนักแห้ง {dw:.1f} kg" if dw else ""), "size": "xs", "color": "#8A9BC0"},
        {"type": "box", "layout": "horizontal", "spacing": "xs", "contents": bars},
        {"type": "text", "text": "สีส้ม = ขึ้นเกิน 3.5% ของน้ำหนักแห้ง", "size": "xxs", "color": "#8A9BC0"}]}


def _row(label, value, bold=False):
    return {"type": "box", "layout": "horizontal", "contents": [
        {"type": "text", "text": label, "size": "sm", "color": "#8A9BC0", "flex": 4},
        {"type": "text", "text": str(value), "size": "sm", "flex": 6, "align": "end", "weight": "bold" if bold else "regular", "wrap": True}]}


def build_flex(summary, history=None, today=None):
    """LINE Flex message dict (type=flex) for the patient."""
    s = summary
    today = today or _thai_now().date()
    rows = []
    if s.get("pre_weight") and s.get("post_weight"):
        rows.append(_row("น้ำหนัก ก่อน → หลัง", f"{s['pre_weight']:.1f} → {s['post_weight']:.1f} kg", True))
    if s.get("dry_weight"):
        rows.append(_row("น้ำหนักแห้ง", f"{s['dry_weight']:.1f} kg"))
    if s.get("actual_uf"):
        rows.append(_row("ดึงน้ำออก", f"{s['actual_uf'] / 1000:.1f} ลิตร", True))
    if s.get("bp_pre") or s.get("bp_post"):
        rows.append(_row("ความดัน ก่อน → หลัง", f"{s.get('bp_pre') or '-'} → {s.get('bp_post') or '-'}"))
    rows.append(_row("เวลาฟอก", f"{s.get('duration_hours', 4)} ชั่วโมง"))
    if s.get("esa_given"):
        rows.append(_row("ยากระตุ้นเม็ดเลือด", s.get("esa_drug_given") or "ได้รับแล้ว"))
    body = [{"type": "text", "text": s.get("patient_name") or "", "weight": "bold", "size": "md"},
            {"type": "text", "text": _d(today), "size": "xs", "color": "#8A9BC0"},
            {"type": "separator", "margin": "md"},
            {"type": "box", "layout": "vertical", "margin": "md", "spacing": "sm", "contents": rows}]
    chart = _bar_chart(history, s.get("dry_weight"))
    if chart:
        body.append(chart)
    body.append({"type": "separator", "margin": "lg"})
    for n in gentle_notes(s):
        body.append({"type": "text", "text": f"💡 {n}", "size": "xs", "wrap": True, "margin": "sm", "color": "#334155"})
    nxt = next_schedule_day(s.get("schedule_days"), today)
    footer = []
    if nxt:
        footer.append({"type": "text", "text": f"📅 นัดครั้งถัดไป วัน{DAY_TH[nxt.weekday()]}ที่ {_d(nxt)}", "size": "sm", "weight": "bold", "wrap": True})
    footer.append({"type": "text", "text": "📞 มีอาการผิดปกติ ติดต่อคลินิกได้ทันที", "size": "xs", "color": "#8A9BC0", "wrap": True})
    footer.append({"type": "text", "text": "ไม่ต้องการรับข้อความ พิมพ์ \"ถอนความยินยอม\"", "size": "xxs", "color": "#B0BCCB", "wrap": True})
    bubble = {"type": "bubble", "size": "mega",
              "header": {"type": "box", "layout": "vertical", "backgroundColor": "#0F766E", "paddingAll": "14px", "contents": [
                  {"type": "text", "text": "📋 สรุปการฟอกเลือดวันนี้", "color": "#FFFFFF", "weight": "bold", "size": "md"},
                  {"type": "text", "text": CLINIC_NAME, "color": "#CCFBF1", "size": "xs"}]},
              "body": {"type": "box", "layout": "vertical", "contents": body},
              "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer}}
    return {"type": "flex", "altText": build_text(s, history, today)[:400], "contents": bubble}


def enrich_from_db(cur, session_id, summary):
    """Add bp_pre/bp_post, esa_prescribed and weight history to a summary dict (read-only)."""
    cur.execute("""SELECT sbp, dbp FROM hd_vital_signs WHERE session_id = %s AND sbp IS NOT NULL ORDER BY recorded_at ASC LIMIT 1""", (session_id,))
    r = cur.fetchone()
    summary["bp_pre"] = f"{r[0]}/{r[1]}" if r else None
    cur.execute("""SELECT sbp, dbp FROM hd_vital_signs WHERE session_id = %s AND sbp IS NOT NULL ORDER BY recorded_at DESC LIMIT 1""", (session_id,))
    r = cur.fetchone()
    summary["bp_post"] = f"{r[0]}/{r[1]}" if r else None
    summary["esa_prescribed"] = bool(summary.get("esa_drug"))
    cur.execute("""SELECT s.session_date, s.pre_weight, s.post_weight FROM hd_sessions s
                   WHERE s.patient_id = (SELECT patient_id FROM hd_sessions WHERE id = %s)
                     AND s.status = 'completed' AND s.pre_weight IS NOT NULL
                   ORDER BY s.session_date DESC, s.id DESC LIMIT 8""", (session_id,))
    hist = [{"d": r[0].isoformat(), "pre": float(r[1]), "post": float(r[2]) if r[2] else None} for r in cur.fetchall()]
    hist.reverse()
    return hist
