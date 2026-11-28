"""🩺 ฟังเสียงเส้นฟอกด้วยมือถือ (AVF Bruit Check) — 28 พ.ย. 2569.

The patient holds the phone microphone on the fistula for ~10 s once a week
(signed link from LINE). The *browser* computes acoustic features (Web Audio
FFT) — no audio file ever leaves the phone — and posts a small JSON:
duration, RMS, spectral centroid, peak frequency, band-energy fractions,
spectral flatness, pulse rate from the envelope, pulsatility, clipping.

The server keeps every good-quality record, builds a personal BASELINE from the
first 3 good recordings, and scores later ones against it:
  centroid shift up, more energy above 800 Hz (higher-pitched), stronger
  pulsatility (systolic-only bruit), weaker RMS (fading thrill) — the classic
  stenosis pattern — plus VA Watch (VP/AP from Magic Camera) as cross-check.

Honest limits: this is a screening trend *relative to the same patient's own
baseline*, not a diagnosis; it needs ≥3 good baseline recordings and must be
validated against Doppler/fistulogram before anyone trusts it. Levels are
explained with numbers; alerts go to the nurse group as text only.

Switch ⚙️ bruit_check (default off). Token kind "bruit", 45 days.
"""
import json
import logging
import re
import statistics
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SETTING_KEY = "bruit_check"
TOKEN_KIND = "bruit"
TOKEN_DAYS = 45
BASELINE_N = 3
MIN_DURATION = 6.0

TABLE_SQL = """CREATE TABLE IF NOT EXISTS hd_bruit_records (
    id SERIAL PRIMARY KEY, hn VARCHAR(20) NOT NULL, features JSONB NOT NULL, quality VARCHAR(10),
    is_baseline BOOLEAN DEFAULT FALSE, score INTEGER, level VARCHAR(10), signals JSONB, baseline_n INTEGER,
    notified BOOLEAN DEFAULT FALSE, source VARCHAR(10) DEFAULT 'patient', note VARCHAR(300),
    created_at TIMESTAMPTZ DEFAULT NOW())"""

FEATURE_KEYS = ("duration_s", "rms", "centroid_hz", "peak_hz", "flatness", "pulse_bpm", "pulsatility", "clipping_pct",
                "band_lo", "band_mid", "band_hi", "band_vhi")
LEVEL_TH = {"ok": "🟢 ใกล้เคียงค่าปกติของคุณ", "watch": "🟠 เปลี่ยนไปเล็กน้อย", "alert": "🔴 เปลี่ยนจากเดิมชัด", "baseline": "📊 กำลังสร้างค่าปกติของคุณ"}


def _thai_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)


def is_enabled(get_setting=None):
    try:
        if get_setting is None:
            from .blueprint import get_setting_value as get_setting
        return str(get_setting(SETTING_KEY, "off") or "off").strip().lower() in ("on", "1", "true", "yes")
    except Exception:
        return False


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def clean_features(raw):
    """Keep known numeric keys only; → dict or None."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for k in FEATURE_KEYS:
        v = _f(raw.get(k))
        if v is not None and v == v and abs(v) < 1e7:
            out[k] = round(v, 4)
    return out or None


def quality_of(f):
    """→ (quality 'good'|'poor', reason)."""
    if not f:
        return "poor", "ไม่มีข้อมูลเสียง"
    if (f.get("duration_s") or 0) < MIN_DURATION:
        return "poor", f"สั้นเกินไป ({f.get('duration_s', 0):.0f} วิ) ต้องอย่างน้อย {MIN_DURATION:.0f} วิ"
    if (f.get("clipping_pct") or 0) > 3:
        return "poor", "เสียงแตก/ดังเกิน — ถือเบาลงหรือเลื่อนออกเล็กน้อย"
    if (f.get("rms") or 0) < 0.003:
        return "poor", "เสียงเบามาก — แนบไมโครโฟนให้ชิดเส้น (ตรงที่คลำได้สั่น)"
    pb = f.get("pulse_bpm")
    if pb is None or not (35 <= pb <= 160):
        return "poor", "ไม่พบจังหวะชีพจรในเสียง — ขยับหาตำแหน่งที่คลำได้ thrill ชัดที่สุด"
    return "good", ""


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def baseline_from(records):
    """records: list of feature dicts (good quality, baseline) → median feature dict or None."""
    if len(records) < BASELINE_N:
        return None
    return {k: _median([r.get(k) for r in records]) for k in FEATURE_KEYS}


def score(f, base, va_level=None):
    """Compare f to baseline. → {score, level, signals[]}."""
    sig = []
    pts = 0
    if not base:
        return {"score": None, "level": "baseline", "signals": []}

    def rel(k):
        a, b = f.get(k), base.get(k)
        return (a - b) / b if (a is not None and b not in (None, 0)) else None

    c = rel("centroid_hz")
    if c is not None:
        if c >= 0.25:
            pts += 30; sig.append({"text": f"เสียงแหลมขึ้น (centroid +{c * 100:.0f}%)", "points": 30})
        elif c >= 0.15:
            pts += 15; sig.append({"text": f"เสียงแหลมขึ้นเล็กน้อย (centroid +{c * 100:.0f}%)", "points": 15})
    hi_now = (f.get("band_hi") or 0) + (f.get("band_vhi") or 0)
    hi_base = (base.get("band_hi") or 0) + (base.get("band_vhi") or 0)
    if hi_base >= 0 and (hi_now - hi_base >= 0.10 or (hi_base > 0 and hi_now / hi_base >= 1.5 and hi_now - hi_base >= 0.05)):
        pts += 25; sig.append({"text": f"พลังงานย่านสูง >800 Hz เพิ่ม ({hi_base * 100:.0f}% → {hi_now * 100:.0f}%)", "points": 25})
    p = rel("pulsatility")
    if p is not None and p >= 0.40:
        pts += 20; sig.append({"text": f"เสียงขาดเป็นจังหวะมากขึ้น (pulsatility +{p * 100:.0f}%) — systolic-only?", "points": 20})
    r = rel("rms")
    if r is not None and r <= -0.5:
        pts += 15; sig.append({"text": f"เสียงเบาลง {abs(r) * 100:.0f}% จากเดิม", "points": 15})
    fl = rel("flatness")
    if fl is not None and fl <= -0.4:
        pts += 10; sig.append({"text": "เสียงเป็นโทนเดี่ยวมากขึ้น (flatness ลด)", "points": 10})
    if va_level == "alert":
        pts += 30; sig.append({"text": "VA Watch: VP/AP จากเครื่องฟอกผิดปกติด้วย", "points": 30})
    elif va_level == "watch":
        pts += 15; sig.append({"text": "VA Watch: VP มีแนวโน้มสูงขึ้น", "points": 15})
    pts = min(100, pts)
    level = "alert" if pts >= 50 else ("watch" if pts >= 25 else "ok")
    return {"score": pts, "level": level, "signals": sig}


def _va_level(cur, hn):
    try:
        cur.execute("SAVEPOINT br_va")
        from .va_watch import collect as va_collect
        cur.execute("SELECT id FROM hd_patient_mapping WHERE hn = %s", (hn,))
        r = cur.fetchone()
        lvl = None
        if r:
            for it in va_collect(cur.connection):
                if it.get("patient_id") == r[0]:
                    lvl = it.get("level")
                    break
        cur.execute("RELEASE SAVEPOINT br_va")
        return lvl
    except Exception:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT br_va")
        except Exception:
            pass
        return None


def record(cur, hn, raw_features, source="patient", note=""):
    """Store + score one recording. Returns dict for the patient page / API."""
    cur.execute(TABLE_SQL)
    f = clean_features(raw_features)
    q, why = quality_of(f)
    if q != "good":
        cur.execute("INSERT INTO hd_bruit_records (hn, features, quality, source, note) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                    (hn, json.dumps(f or {}), q, source, (why or note or "")[:300]))
        return {"id": cur.fetchone()[0], "quality": q, "reason": why, "level": None, "text": "⚠️ อัดใหม่อีกครั้ง: " + why}
    cur.execute("SELECT features FROM hd_bruit_records WHERE hn = %s AND quality = 'good' AND is_baseline = TRUE ORDER BY id", (hn,))
    base_recs = [r[0] for r in cur.fetchall()]
    is_base = len(base_recs) < BASELINE_N
    base = baseline_from(base_recs) if not is_base else None
    va = _va_level(cur, hn) if base else None
    sc = score(f, base, va)
    cur.execute("""INSERT INTO hd_bruit_records (hn, features, quality, is_baseline, score, level, signals, baseline_n, source, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (hn, json.dumps(f), q, is_base, sc["score"], sc["level"], json.dumps(sc["signals"], ensure_ascii=False),
                 len(base_recs), source, (note or "")[:300]))
    rid = cur.fetchone()[0]
    n_after = len(base_recs) + (1 if is_base else 0)
    # consecutive watch → notify as well
    prev_level = None
    cur.execute("SELECT level FROM hd_bruit_records WHERE hn = %s AND quality = 'good' AND id < %s ORDER BY id DESC LIMIT 1", (hn, rid))
    r = cur.fetchone()
    if r:
        prev_level = r[0]
    notify = sc["level"] == "alert" or (sc["level"] == "watch" and prev_level == "watch")
    return {"id": rid, "quality": q, "is_baseline": is_base, "baseline_n": n_after, "score": sc["score"], "level": sc["level"],
            "signals": sc["signals"], "va_level": va, "notify": notify, "text": patient_text(sc, is_base, n_after)}


def patient_text(sc, is_base, n_after):
    if is_base:
        return (f"📊 บันทึกแล้ว — กำลังสร้าง 'เสียงปกติของคุณ' ({n_after}/{BASELINE_N})\n"
                "อัดอีก" + (f" {BASELINE_N - n_after} ครั้ง" if BASELINE_N - n_after > 0 else "") + " ในวันต่าง ๆ แล้วระบบจะเริ่มเทียบให้ได้")
    lvl = sc.get("level")
    head = LEVEL_TH.get(lvl, lvl)
    lines = [head]
    if sc.get("signals"):
        lines += ["• " + s["text"] for s in sc["signals"][:3]]
    if lvl == "alert":
        lines.append("แนะนำกด 'แจ้งพยาบาล' และให้คลำ thrill ทุกวัน ถ้าสั่นหาย/ปวด/บวม ให้ติดต่อคลินิกทันที")
    elif lvl == "watch":
        lines.append("อัดซ้ำอีกครั้งพรุ่งนี้ ถ้ายังเปลี่ยนอยู่ระบบจะแจ้งพยาบาลให้")
    else:
        lines.append("อัดสัปดาห์ละครั้ง (วันเดียวกัน เวลาใกล้เคียงกัน) จะเทียบได้แม่นที่สุด")
    lines.append("ℹ️ เทียบกับเสียงของคุณเองในอดีต ไม่ใช่การวินิจฉัย")
    return "\n".join(lines)


def history(cur, hn, limit=30):
    cur.execute(TABLE_SQL)
    cur.execute("""SELECT id, created_at, quality, is_baseline, score, level, signals, features, source FROM hd_bruit_records
                   WHERE hn = %s ORDER BY id DESC LIMIT %s""", (hn, limit))
    return [{"id": r[0], "at": r[1].isoformat() if r[1] else None, "quality": r[2], "is_baseline": r[3], "score": r[4], "level": r[5],
             "signals": r[6] or [], "features": r[7] or {}, "source": r[8]} for r in cur.fetchall()]


def board(cur, days=60):
    """Staff overview: latest record per patient (good quality)."""
    cur.execute(TABLE_SQL)
    cur.execute("""SELECT DISTINCT ON (b.hn) b.hn, p.patient_name, b.created_at, b.level, b.score, b.is_baseline, b.baseline_n, b.signals,
                          (SELECT COUNT(*) FROM hd_bruit_records x WHERE x.hn = b.hn AND x.quality = 'good'),
                          (SELECT COUNT(*) FROM hd_bruit_records x WHERE x.hn = b.hn AND x.quality = 'good' AND x.is_baseline)
                   FROM hd_bruit_records b LEFT JOIN hd_patient_mapping p ON p.hn = b.hn
                   WHERE b.quality = 'good' AND b.created_at >= NOW() - (%s || ' days')::interval
                   ORDER BY b.hn, b.id DESC""", (str(days),))
    rows = [{"hn": r[0], "name": r[1], "at": r[2].isoformat() if r[2] else None, "level": r[3] or "baseline", "score": r[4],
             "is_baseline": r[5], "baseline_n": r[9], "signals": r[7] or [], "n_good": r[8]} for r in cur.fetchall()]
    order = {"alert": 0, "watch": 1, "ok": 2, "baseline": 3}
    rows.sort(key=lambda x: (order.get(x["level"], 9), x["hn"]))
    return rows


def reset_baseline(cur, hn):
    """Staff: start a new baseline (e.g. after PTA/new access). Old records stay for history."""
    cur.execute(TABLE_SQL)
    cur.execute("UPDATE hd_bruit_records SET is_baseline = FALSE WHERE hn = %s", (hn,))
    return cur.rowcount


def nurse_text(hn, name, res, base_url=""):
    s = "; ".join(x["text"] for x in (res.get("signals") or [])[:3])
    return (f"🩺 เสียงเส้นฟอก {name or ''} (HN {hn}) — {LEVEL_TH.get(res.get('level'), res.get('level'))} (คะแนน {res.get('score')})\n"
            f"{s}\nผู้ป่วยอัดจากบ้าน · ตรวจ thrill/bruit และ VP/AP รอบหน้า · " + (f"{base_url}/hd/bruit" if base_url else "/hd/bruit"))


def link_for(hn, base_url):
    from .patient_engage import make_token
    day = _thai_now().strftime("%Y%m%d")
    return f"{base_url.rstrip('/')}/hd/bruit/{make_token(TOKEN_KIND, hn, day)}?openExternalBrowser=1"


def link_command(cur, body, base_url):
    m = re.search(r"(\d{3,9})", body or "")
    if not m:
        return "ใช้: ลิงก์เส้น <HN> เช่น ลิงก์เส้น 68443"
    hn = m.group(1)
    cur.execute("SELECT patient_name, vascular_access FROM hd_patient_mapping WHERE hn = %s", (hn,))
    r = cur.fetchone()
    if not r:
        return f"ไม่พบ HN {hn}"
    acc = (r[1] or "").lower()
    warn = "\n⚠️ ผู้ป่วยรายนี้บันทึกว่าใช้สายสวน — ฟีเจอร์นี้สำหรับ AVF/AVG" if any(w in acc for w in ("cath", "perm", "dlc", "สาย")) else ""
    return (f"🩺 ลิงก์ฟังเสียงเส้นฟอกของ {r[0] or hn} (HN {hn})\n{link_for(hn, base_url)}\n\n"
            f"ให้ผู้ป่วยอัด 10 วินาทีสัปดาห์ละครั้ง (3 ครั้งแรก = ค่าปกติของตัวเอง) ใช้ได้ {TOKEN_DAYS} วัน · เสียงไม่ถูกส่งออกจากมือถือ ส่งเฉพาะตัวเลขลักษณะเสียง{warn}")
