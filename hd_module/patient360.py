"""Patient 360 — one timeline per patient (ว้าว #5).

Read-only merge of everything the system already records about one patient
into a single chronological feed plus small series for charts:
    sessions (weights, UF, Hb, IDH alerts), clinical alerts + physician
    orders, lab documents (with flags), ESA proposals/decisions, drugs
    given, dialyzer changes, incidents.
Each block is queried inside its own savepoint so a missing table on an
older DB never breaks the page.
"""
import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _q(cur, sql, params=()):
    try:
        cur.execute("SAVEPOINT p360")
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT p360")
        return rows
    except Exception as e:
        logger.warning("patient360 block skipped: %s", str(e)[:120])
        try:
            cur.execute("ROLLBACK TO SAVEPOINT p360")
        except Exception:
            pass
        return []


def _f(v):
    return float(v) if v is not None else None


def _ts(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat() + ("" if v.tzinfo else "Z")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def build(conn, patient_id, days=365):
    """Return {"patient", "events": [...], "series": {...}, "summary": {...}}."""
    since = ((datetime.now(timezone.utc) + timedelta(hours=7)).date() - timedelta(days=days)).isoformat()
    ev = []
    series = {"weight": [], "hb": [], "vp": [], "idwg": [], "ktv": []}
    with conn.cursor() as cur:
        r = _q(cur, """SELECT id, hn, patient_name, vascular_access, dry_weight, esa_drug, esa_dose, schedule_days,
                              date_of_birth, is_active, default_shift FROM hd_patient_mapping WHERE id = %s""", (patient_id,))
        if not r:
            return None
        p = r[0]
        patient = {"id": p[0], "hn": p[1], "name": p[2], "access": p[3], "dry_weight": _f(p[4]), "esa_drug": p[5],
                   "esa_dose": p[6], "schedule_days": p[7], "is_active": p[9],
                   "default_shift": p[10] if len(p) > 10 else None}
        try:
            from .serology import latest_for as _sero_latest
            patient["serology"] = _sero_latest(cur, [p[0]]).get(p[0])
        except Exception:
            patient["serology"] = None
        if p[8]:
            today = (datetime.now(timezone.utc) + timedelta(hours=7)).date()
            patient["age"] = today.year - p[8].year - ((today.month, today.day) < (p[8].month, p[8].day))

        # sessions + per-session VP median + alert count
        for r in _q(cur, """
            SELECT s.id, s.session_date, s.shift, s.bed_number, s.status, s.started_at, s.ended_at,
                   s.pre_weight, s.post_weight, s.uf_goal, s.actual_uf, s.hb_value, s.kt_v, s.urr,
                   s.esa_drug_given, s.esa_dose_given, s.notes,
                   (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY f.vp) FROM hd_flowsheet f WHERE f.session_id = s.id AND f.vp IS NOT NULL),
                   (SELECT COUNT(*) FROM hd_clinical_alerts a WHERE a.session_id = s.id AND a.severity::text IN ('critical','warning')),
                   (SELECT MIN(v.sbp) FROM hd_vital_signs v WHERE v.session_id = s.id)
            FROM hd_sessions s WHERE s.patient_id = %s AND s.session_date >= %s
            ORDER BY s.session_date, s.id""", (patient_id, since)):
            d = r[1]
            pre, post, dw = _f(r[7]), _f(r[8]), patient["dry_weight"]
            e = {"type": "session", "date": _ts(d), "ts": _ts(r[5]) or _ts(d), "session_id": r[0], "shift": r[2],
                 "bed": r[3], "status": r[4], "pre_weight": pre, "post_weight": post, "uf_goal": _f(r[9]),
                 "actual_uf": _f(r[10]), "hb": _f(r[11]), "ktv": _f(r[12]), "urr": _f(r[13]),
                 "esa_given": (f"{r[14]} {r[15] or ''}".strip() if r[14] else None), "notes": r[16],
                 "vp_median": _f(r[17]), "alerts": r[18], "min_sbp": r[19]}
            ev.append(e)
            if pre is not None or post is not None:
                series["weight"].append({"d": _ts(d), "pre": pre, "post": post, "dw": dw})
            if pre is not None and dw:
                series["idwg"].append({"d": _ts(d), "v": round(pre - dw, 2), "pct": round((pre - dw) / dw * 100, 1)})
            if e["hb"] is not None:
                series["hb"].append({"d": _ts(d), "v": e["hb"], "src": "session"})
            if e["vp_median"] is not None:
                series["vp"].append({"d": _ts(d), "v": e["vp_median"]})
            if e["ktv"] is not None:
                series["ktv"].append({"d": _ts(d), "v": e["ktv"]})

        # alerts (+ physician orders)
        for r in _q(cur, """
            SELECT a.id, a.created_at, a.severity::text, a.title, a.message, a.acknowledged, a.physician_order,
                   a.order_by, a.order_at, a.session_id, s.bed_number
            FROM hd_clinical_alerts a LEFT JOIN hd_sessions s ON s.id = a.session_id
            WHERE (a.patient_id = %s OR s.patient_id = %s) AND a.created_at >= %s
            ORDER BY a.created_at""", (patient_id, patient_id, since)):
            ev.append({"type": "alert", "ts": _ts(r[1]), "date": _ts(r[1])[:10] if r[1] else None, "id": r[0],
                       "severity": r[2], "title": r[3], "message": r[4], "acknowledged": r[5],
                       "order": r[6], "order_by": r[7], "order_at": _ts(r[8]), "session_id": r[9], "bed": r[10]})

        # lab documents
        try:
            from .lab_sync import NUMERIC_COLS, TEXT_COLS, flags_for
            for r in _q(cur, f"""
                SELECT COALESCE(specimen_date, doc_date), doc_id, status, {', '.join(NUMERIC_COLS + TEXT_COLS)}
                FROM hd_lab_docs WHERE patient_id = %s AND COALESCE(specimen_date, doc_date) >= %s
                ORDER BY 1, id""", (patient_id, since)):
                row = dict(zip(NUMERIC_COLS + TEXT_COLS, r[3:]))
                for c in NUMERIC_COLS:
                    row[c] = _f(row[c])
                ev.append({"type": "lab", "date": _ts(r[0]), "ts": _ts(r[0]), "doc_id": r[1], "status": r[2],
                           "values": {k: v for k, v in row.items() if v is not None}, "flags": flags_for(row)})
                if row.get("hb") is not None:
                    series["hb"].append({"d": _ts(r[0]), "v": row["hb"], "src": "lab"})
                if row.get("ktv") is not None:
                    series["ktv"].append({"d": _ts(r[0]), "v": row["ktv"]})
        except Exception as e:
            logger.warning("patient360 labs: %s", e)

        # ESA proposals / decisions
        for r in _q(cur, """SELECT id, created_at, hb, action, current_dose, proposed_dose, rationale, status,
                                   decided_by, decided_at, note FROM hd_esa_proposals
                            WHERE patient_id = %s AND created_at >= %s ORDER BY created_at""", (patient_id, since)):
            ev.append({"type": "esa", "ts": _ts(r[9] or r[1]), "date": _ts((r[9] or r[1]))[:10] if (r[9] or r[1]) else None,
                       "id": r[0], "hb": _f(r[2]), "action": r[3], "current_dose": r[4], "proposed_dose": r[5],
                       "rationale": r[6], "status": r[7], "decided_by": r[8], "note": r[10]})

        # drugs given
        for r in _q(cur, """SELECT g.given_at, g.drug_name, g.dose_given, g.route, g.session_id FROM hd_medication_given g
                            JOIN hd_sessions s ON s.id = g.session_id WHERE s.patient_id = %s AND g.given_at >= %s
                            ORDER BY g.given_at""", (patient_id, since)):
            ev.append({"type": "drug", "ts": _ts(r[0]), "date": _ts(r[0])[:10] if r[0] else None, "drug": r[1],
                       "dose": r[2], "route": r[3], "session_id": r[4]})

        # dialyzer changes
        for r in _q(cur, """SELECT first_use_date, dialyzer_type, dialyzer_serial, reuse_count, max_reuse, status
                            FROM hd_dialyzer_tracking WHERE patient_id = %s AND first_use_date >= %s
                            ORDER BY first_use_date""", (patient_id, since)):
            ev.append({"type": "dialyzer", "ts": _ts(r[0]), "date": _ts(r[0]), "dialyzer": r[1], "serial": r[2],
                       "reuse": r[3], "max_reuse": r[4], "status": r[5]})

        # incidents (by HN)
        for r in _q(cur, """SELECT incident_date, incident_type, severity, description, status FROM hd_incidents
                            WHERE patient_hn = %s AND incident_date >= %s ORDER BY incident_date""", (patient["hn"], since)):
            ev.append({"type": "incident", "ts": _ts(r[0]), "date": _ts(r[0]), "incident_type": r[1], "severity": r[2],
                       "description": r[3], "status": r[4]})

    ev.sort(key=lambda e: (e.get("ts") or ""), reverse=True)
    for k in series:
        series[k].sort(key=lambda x: x["d"])
    sess = [e for e in ev if e["type"] == "session"]
    done = [e for e in sess if e["status"] == "completed"]
    summary = {"sessions": len(sess), "completed": len(done),
               "idh_sessions": sum(1 for e in sess if (e["alerts"] or 0) > 0),
               "alerts": sum(1 for e in ev if e["type"] == "alert"),
               "orders_pending": sum(1 for e in ev if e["type"] == "alert" and e["severity"] in ("critical", "warning") and not e["order"]),
               "labs": sum(1 for e in ev if e["type"] == "lab"),
               "last_hb": (series["hb"][-1]["v"] if series["hb"] else None),
               "last_session": (sess[-1]["date"] if sess else None),
               "avg_idwg_pct": (round(sum(x["pct"] for x in series["idwg"][-10:]) / len(series["idwg"][-10:]), 1) if series["idwg"] else None),
               "uf_achieved_pct": (round(sum(e["actual_uf"] for e in done if e["actual_uf"] and e["uf_goal"]) /
                                         max(1, sum(e["uf_goal"] for e in done if e["actual_uf"] and e["uf_goal"])) * 100) if done else None)}
    return {"patient": patient, "events": ev, "series": series, "summary": summary, "days": days}
