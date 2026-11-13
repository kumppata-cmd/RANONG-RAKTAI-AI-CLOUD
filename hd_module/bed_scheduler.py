"""Smart Bed Scheduler (ว้าว รอบ 2 — #3).

Builds a week of bed assignments from what the system already knows and
enforces the isolation rules that today live in people's memory:

    who comes when   schedule_days ('จ,พ,ศ') + the patient's usual shift and
                     usual bed (mode of the last 12 sessions)
    infection zone   HBsAg+ → only beds listed in ⚙️ hbv_beds (and nobody else
                     on them); anti-HCV+ → hcv_beds when configured. Source:
                     newest lab document, hd_lab_results serology, or the
                     words HBV/HCV in primary_diagnosis.
    machines         hd_equipment dialysis_machine on a bed with status
                     'repair'/'retired' or PM overdue → bed unavailable
    capacity         TOTAL_BEDS per shift; overflow listed with a reason

Pure planning: nothing is written until a nurse/physician presses
"สร้างรอบจากตาราง", which inserts *scheduled* sessions (status 'scheduled')
for one day — the same rows the dashboard pre-assign already creates.
"""
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DAY_LETTER = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}
DAY_TH = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
SHIFTS = ("morning", "afternoon", "evening")
SHIFT_TH = {"morning": "เช้า", "afternoon": "บ่าย", "evening": "ค่ำ"}


def _thai_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def parse_beds(text):
    out = []
    for part in str(text or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                out.extend(range(int(a), int(b) + 1))
            except ValueError:
                pass
        else:
            try:
                out.append(int(part))
            except ValueError:
                pass
    return sorted(set(out))


def letters(schedule_days):
    s = str(schedule_days or "").replace(" ", ",").replace("/", ",").replace("-", ",")
    return [x for x in s.split(",") if x]


def load_context(conn, total_beds, hbv_beds="", hcv_beds=""):
    """Everything the planner needs, read-only."""
    ctx = {"total_beds": int(total_beds), "hbv_beds": parse_beds(hbv_beds), "hcv_beds": parse_beds(hcv_beds),
           "patients": [], "bad_beds": {}}
    with conn.cursor() as cur:
        cur.execute("""SELECT id, hn, patient_name, schedule_days, vascular_access, COALESCE(hd_per_week,3), primary_diagnosis
                       FROM hd_patient_mapping WHERE is_active = TRUE""")
        pats = cur.fetchall()
        ids = [p[0] for p in pats]
        # usual shift / bed
        usual = {}
        if ids:
            cur.execute("""SELECT patient_id, COALESCE(shift,'morning'), bed_number FROM (
                               SELECT patient_id, shift, bed_number,
                                      ROW_NUMBER() OVER (PARTITION BY patient_id ORDER BY session_date DESC, id DESC) rn
                               FROM hd_sessions WHERE patient_id = ANY(%s) AND status IN ('completed','in_progress','scheduled')) t
                           WHERE rn <= 12""", (ids,))
            for pid, sh, bed in cur.fetchall():
                u = usual.setdefault(pid, {"shift": Counter(), "bed": Counter()})
                u["shift"][sh] += 1
                if bed:
                    u["bed"][bed] += 1
        # infection status
        inf = {}
        for sql in ("""SELECT DISTINCT ON (patient_id) patient_id, hbsag, antihcv FROM hd_lab_docs
                       WHERE patient_id = ANY(%s) AND (hbsag IS NOT NULL OR antihcv IS NOT NULL)
                       ORDER BY patient_id, COALESCE(specimen_date, doc_date) DESC NULLS LAST, id DESC""",
                    """SELECT DISTINCT ON (patient_id) patient_id, hepatitis_b, hepatitis_c FROM hd_lab_results
                       WHERE patient_id = ANY(%s) AND (hepatitis_b IS NOT NULL OR hepatitis_c IS NOT NULL)
                       ORDER BY patient_id, lab_date DESC, id DESC"""):
            try:
                cur.execute("SAVEPOINT bs")
                cur.execute(sql, (ids,))
                for pid, b, c in cur.fetchall():
                    cur_v = inf.setdefault(pid, {"hbv": False, "hcv": False})
                    cur_v["hbv"] = cur_v["hbv"] or str(b or "").lower().startswith(("pos", "react", "+"))
                    cur_v["hcv"] = cur_v["hcv"] or str(c or "").lower().startswith(("pos", "react", "+"))
                cur.execute("RELEASE SAVEPOINT bs")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT bs")
        # machines
        try:
            cur.execute("SAVEPOINT bs2")
            cur.execute("""SELECT bed_number, status, next_pm_date, brand, model FROM hd_equipment
                           WHERE equipment_type = 'dialysis_machine' AND bed_number IS NOT NULL""")
            for bed, st, pm, brand, model in cur.fetchall():
                if st in ("repair", "retired"):
                    ctx["bad_beds"][bed] = f"เครื่อง{'ซ่อม' if st == 'repair' else 'ปลดระวาง'} ({brand or ''} {model or ''})".strip()
                elif pm and pm < _thai_today():
                    ctx["bad_beds"][bed] = f"เลยกำหนด PM ({pm.day}/{pm.month})"
            cur.execute("RELEASE SAVEPOINT bs2")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT bs2")
    for pid, hn, name, sd, va, pw, dx in pats:
        u = usual.get(pid, {"shift": Counter(), "bed": Counter()})
        dxl = (dx or "").upper()
        i = inf.get(pid, {"hbv": False, "hcv": False})
        hbv = i["hbv"] or "HBV" in dxl or "HBSAG" in dxl
        hcv = i["hcv"] or "HCV" in dxl
        ctx["patients"].append({"id": pid, "hn": hn, "name": name, "days": letters(sd), "access": va, "per_week": pw,
                                "usual_shift": (u["shift"].most_common(1)[0][0] if u["shift"] else "morning"),
                                "usual_bed": (u["bed"].most_common(1)[0][0] if u["bed"] else None),
                                "hbv": hbv, "hcv": hcv})
    return ctx


def zone_of(ctx, bed):
    if bed in ctx["hbv_beds"]:
        return "hbv"
    if bed in ctx["hcv_beds"]:
        return "hcv"
    return "normal"


def check_bed(ctx, patient, bed, occupied=None, hbv_in_shift=True):
    """→ list of warnings (empty = fine). Hard rules are prefixed '⛔'.
    hbv_in_shift: an HBsAg+ patient dialyses in this shift → the HBV bed is reserved for them;
    when the shift has no HBsAg+ patient the bed may be used by others (clinic policy 13 Nov 2026)."""
    w = []
    if bed in ctx["bad_beds"]:
        w.append(f"⛔ เตียง {bed}: {ctx['bad_beds'][bed]}")
    z = zone_of(ctx, bed)
    if patient["hbv"] and ctx["hbv_beds"] and z != "hbv":
        w.append(f"⛔ ผู้ป่วย HBsAg+ ต้องใช้เตียงโซน HBV ({', '.join(map(str, ctx['hbv_beds']))})")
    if not patient["hbv"] and z == "hbv":
        if hbv_in_shift:
            w.append("⛔ เตียงโซน HBV — รอบนี้มีผู้ป่วย HBsAg+ ห้ามใช้กับผู้ป่วยอื่น")
        else:
            w.append("ℹ️ เตียงโซน HBV — รอบนี้ไม่มีผู้ป่วย HBsAg+ ใช้ได้ (ล้างเครื่องตามมาตรฐาน)")
    if patient["hcv"] and ctx["hcv_beds"] and z != "hcv":
        w.append(f"⚠️ ผู้ป่วย anti-HCV+ ควรใช้เตียงโซน HCV ({', '.join(map(str, ctx['hcv_beds']))})")
    if not patient["hcv"] and z == "hcv" and ctx["hcv_beds"]:
        w.append("⚠️ เตียงโซน HCV")
    if occupied and bed in occupied:
        w.append(f"⛔ เตียง {bed} ถูกจองแล้ว ({occupied[bed]})")
    return w


def plan_day(ctx, day):
    """→ {"date", "shifts": {shift: [assignment]}, "unassigned": [...], "load": {shift: n}}"""
    letter = DAY_LETTER[day.weekday()]
    due = [p for p in ctx["patients"] if letter in p["days"]]
    out = {"date": day.isoformat(), "day_th": DAY_TH[day.weekday()], "shifts": {s: [] for s in SHIFTS}, "unassigned": [], "load": {}}
    for sh in SHIFTS:
        group = sorted([p for p in due if p["usual_shift"] == sh], key=lambda p: (p["usual_bed"] or 999, p["name"]))
        occupied = {}
        hbv_in_shift = any(p["hbv"] for p in group)
        free_normal = [b for b in range(1, ctx["total_beds"] + 1) if b not in ctx["bad_beds"] and zone_of(ctx, b) == "normal"]
        free_hbv = [b for b in ctx["hbv_beds"] if b not in ctx["bad_beds"]]
        free_hcv = [b for b in ctx["hcv_beds"] if b not in ctx["bad_beds"]]
        if not hbv_in_shift:
            # no HBsAg+ patient this shift → the HBV bed is open to everyone (used last, so it stays free as long as possible)
            free_normal = free_normal + free_hbv
        # HBV first (scarce beds), then HCV, then the rest
        for p in sorted(group, key=lambda p: (0 if p["hbv"] else (1 if p["hcv"] else 2))):
            pool = free_hbv if (p["hbv"] and ctx["hbv_beds"]) else (free_hcv if (p["hcv"] and ctx["hcv_beds"]) else free_normal)
            bed = None
            if p["usual_bed"] in pool and p["usual_bed"] not in occupied and (hbv_in_shift or p["hbv"] or zone_of(ctx, p["usual_bed"]) != "hbv" or len(group) >= len(free_normal)):
                bed = p["usual_bed"]
            else:
                for b in pool:
                    if b not in occupied:
                        bed = b
                        break
            if bed is None:
                reason = "เตียงโซน HBV เต็ม" if (p["hbv"] and ctx["hbv_beds"]) else ("เตียงโซน HCV เต็ม" if (p["hcv"] and ctx["hcv_beds"]) else f"เตียงเวร{SHIFT_TH[sh]}เต็ม")
                out["unassigned"].append({"hn": p["hn"], "name": p["name"], "shift": sh, "reason": reason, "hbv": p["hbv"], "hcv": p["hcv"]})
                continue
            occupied[bed] = p["name"]
            out["shifts"][sh].append({"bed": bed, "patient_id": p["id"], "hn": p["hn"], "name": p["name"], "access": p["access"],
                                      "hbv": p["hbv"], "hcv": p["hcv"], "zone": zone_of(ctx, bed),
                                      "moved": bool(p["usual_bed"] and p["usual_bed"] != bed),
                                      "warnings": check_bed(ctx, p, bed, hbv_in_shift=hbv_in_shift)})
        out["shifts"][sh].sort(key=lambda a: a["bed"])
        out["load"][sh] = len(out["shifts"][sh])
        out.setdefault("hbv_in_shift", {})[sh] = hbv_in_shift
    return out


def plan_week(ctx, start):
    days = [plan_day(ctx, start + timedelta(days=i)) for i in range(7)]
    warnings = []
    if any(p["hbv"] for p in ctx["patients"]) and not ctx["hbv_beds"]:
        warnings.append("มีผู้ป่วย HBsAg+ แต่ยังไม่ได้ตั้งเตียงโซน HBV ใน ⚙️ (hbv_beds) — ระบบจัดรวมกับเตียงปกติ")
    for b, why in ctx["bad_beds"].items():
        warnings.append(f"เตียง {b} ไม่ใช้งาน: {why}")
    return {"start": start.isoformat(), "days": days, "warnings": warnings,
            "zones": {"hbv": ctx["hbv_beds"], "hcv": ctx["hcv_beds"]}, "total_beds": ctx["total_beds"],
            "patients": len(ctx["patients"]), "hbv_patients": sum(1 for p in ctx["patients"] if p["hbv"]),
            "hcv_patients": sum(1 for p in ctx["patients"] if p["hcv"])}


def apply_day(conn, day_plan, shift, nurse_id=None, only_beds=None):
    """Insert scheduled sessions for one date+shift (skip beds/patients that already have a session). → stats"""
    created, skipped = 0, 0
    day = day_plan["date"]
    with conn.cursor() as cur:
        for a in day_plan["shifts"].get(shift, []):
            if only_beds and a["bed"] not in only_beds:
                continue
            if any(w.startswith("⛔") for w in a.get("warnings", [])):
                skipped += 1
                continue
            cur.execute("""SELECT 1 FROM hd_sessions WHERE session_date = %s AND status IN ('scheduled','in_progress','completed')
                           AND (patient_id = %s OR (bed_number = %s AND COALESCE(shift,'morning') = %s)) LIMIT 1""",
                        (day, a["patient_id"], a["bed"], shift))
            if cur.fetchone():
                skipped += 1
                continue
            cur.execute("""INSERT INTO hd_sessions (patient_id, bed_number, session_date, status, uf_goal, nurse_id, shift)
                           VALUES (%s, %s, %s, 'scheduled', 2000, %s, %s)""", (a["patient_id"], a["bed"], day, nurse_id, shift))
            created += 1
    conn.commit()
    return {"created": created, "skipped": skipped}


def line_text(day_plan, base_url=""):
    d = date.fromisoformat(day_plan["date"])
    L = [f"🗓 ตารางเตียง วัน{day_plan['day_th']}ที่ {d.day}/{d.month}/{d.year + 543}", "━━━━━━━━━━━━"]
    for sh in SHIFTS:
        rows = day_plan["shifts"].get(sh, [])
        if not rows:
            continue
        L.append(f"{SHIFT_TH[sh]} ({len(rows)} คน)")
        L.append("  " + " · ".join(f"{a['bed']}:{a['name'].split()[0] if a['name'] else a['hn']}{'🅱' if a['hbv'] else ''}{'🅲' if a['hcv'] else ''}" for a in rows))
    if day_plan["unassigned"]:
        L.append("⚠️ ยังไม่มีเตียง: " + ", ".join(f"{u['name']} ({u['reason']})" for u in day_plan["unassigned"][:8]))
    L.append("━━━━━━━━━━━━")
    if base_url:
        L.append(f"แก้ไข/ยืนยัน: {base_url}/hd/scheduler")
    return "\n".join(L)
