"""Ops watchdog — makes the background scheduler observable and self-healing.

Why: on 14 Nov 2026 no automatic LINE message arrived all morning and nobody
could tell whether the scheduler thread was dead, blocked in a long job, or
LINE itself was refusing pushes. This module keeps a small in-process status
board that every job and every LINE push reports to, exposes it on /health
(for the external monitor) and in ⚙️ settings (for the physician), and lets a
watchdog thread restart the scheduler when it stops ticking.

Everything here is best-effort: a failure inside the status board must never
break the job it is observing.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_STATE = {
    "started_at": None,          # scheduler loop start (Thai time ISO)
    "last_tick": None,           # last scheduler loop iteration
    "tick_count": 0,
    "restarts": 0,
    "jobs": {},                  # name → {last_run, ok, detail, duration_s, running_since}
    "line": {"last_ok": None, "last_error": None, "last_error_at": None, "ok_count": 0, "fail_count": 0,
             "quota": None},     # quota → {"type","value","used","checked_at"}
}

# A job that has been "running" longer than this is reported as stuck.
JOB_STUCK_SEC = 20 * 60
# Scheduler with no tick for this long → watchdog restarts it.
TICK_STALE_SEC = 20 * 60


_THAI = timezone(timedelta(hours=7))
_CRITICAL = threading.local()
# Keep this many messages of the monthly LINE quota for critical alerts
# (BP drop, equipment failure, backup failure). Informational pushes stop
# first when the quota runs low. Overridable via ⚙️ line_quota_reserve.
QUOTA_RESERVE_DEFAULT = 60


def _now():
    return datetime.now(_THAI)


def _iso(dt=None):
    return (dt or _now()).isoformat(timespec="seconds")


def tick():
    with _LOCK:
        if _STATE["started_at"] is None:
            _STATE["started_at"] = _iso()
        _STATE["last_tick"] = _iso()
        _STATE["tick_count"] += 1


def job_started(name):
    with _LOCK:
        _STATE["jobs"].setdefault(name, {})["running_since"] = _iso()


def job_done(name, ok=True, detail="", started=None):
    with _LOCK:
        j = _STATE["jobs"].setdefault(name, {})
        j.update({"last_run": _iso(), "ok": bool(ok), "detail": str(detail)[:200], "running_since": None,
                  "duration_s": round(time.time() - started, 1) if started else None})


def line_result(ok, status=None, body=""):
    """Called by the LINE push wrapper after every push attempt."""
    with _LOCK:
        L = _STATE["line"]
        if ok:
            L["last_ok"] = _iso()
            L["ok_count"] += 1
            q = L.get("quota")
            if q and q.get("used") is not None:
                q["used"] = int(q["used"]) + 1
        else:
            L["fail_count"] += 1
            L["last_error_at"] = _iso()
            L["last_error"] = f"HTTP {status}: {str(body)[:160]}" if status else str(body)[:200]


def line_quota(info):
    with _LOCK:
        _STATE["line"]["quota"] = dict(info or {}, checked_at=_iso())


def quota_remaining():
    q = _STATE["line"].get("quota") or {}
    if not q.get("value") or q.get("used") is None:
        return None
    return max(0, int(q["value"]) - int(q["used"]))


def set_reserve(n):
    with _LOCK:
        _STATE["reserve"] = int(n)


def quota_low():
    r = quota_remaining()
    return r is not None and r <= int(_STATE.get("reserve", QUOTA_RESERVE_DEFAULT))


class critical:
    """`with ops.critical():` — pushes inside are never suppressed by the quota guard."""
    def __enter__(self):
        _CRITICAL.depth = getattr(_CRITICAL, "depth", 0) + 1

    def __exit__(self, *a):
        _CRITICAL.depth = max(0, getattr(_CRITICAL, "depth", 1) - 1)


def allow_push():
    """False → informational push should be skipped (quota reserved for critical alerts)."""
    if getattr(_CRITICAL, "depth", 0) > 0:
        return True
    if quota_low():
        with _LOCK:
            _STATE["line"]["suppressed"] = _STATE["line"].get("suppressed", 0) + 1
        return False
    return True


def note_sent():
    """Count a successful push against the cached quota so the guard reacts between hourly checks."""
    with _LOCK:
        q = _STATE["line"].get("quota")
        if q and q.get("used") is not None:
            q["used"] = int(q["used"]) + 1


def snapshot():
    """Plain dict for JSON — includes derived health flags."""
    with _LOCK:
        s = {"started_at": _STATE["started_at"], "last_tick": _STATE["last_tick"], "tick_count": _STATE["tick_count"],
             "restarts": _STATE["restarts"], "jobs": {k: dict(v) for k, v in _STATE["jobs"].items()},
             "line": dict(_STATE["line"])}
    age = None
    if s["last_tick"]:
        try:
            age = int((_now() - datetime.fromisoformat(s["last_tick"])).total_seconds())
        except Exception:
            age = None
    s["tick_age_sec"] = age
    stuck = []
    for k, v in s["jobs"].items():
        rs = v.get("running_since")
        if rs:
            try:
                if (_now() - datetime.fromisoformat(rs)).total_seconds() > JOB_STUCK_SEC:
                    stuck.append(k)
            except Exception:
                pass
    s["stuck_jobs"] = stuck
    q = s["line"].get("quota") or {}
    s["line"]["quota_exhausted"] = bool(q.get("value") and q.get("used") is not None and q["used"] >= q["value"])
    s["line"]["quota_low"] = quota_low()
    s["line"]["suppressed"] = _STATE["line"].get("suppressed", 0)
    problems = []
    if age is None:
        problems.append("scheduler ยังไม่เริ่ม")
    elif age > TICK_STALE_SEC:
        problems.append(f"scheduler ไม่ทำงานมา {age // 60} นาที")
    if stuck:
        problems.append("งานค้าง: " + ", ".join(stuck))
    if s["line"]["quota_exhausted"]:
        problems.append("LINE ครบโควตาข้อความรายเดือน — ส่งไม่ได้ทุกข้อความจนกว่าจะขึ้นเดือนใหม่/อัปเกรดแพ็กเกจ")
    elif s["line"]["quota_low"]:
        problems.append(f"LINE โควตาใกล้หมด (เหลือ {quota_remaining()}) — ส่งเฉพาะแจ้งเตือนวิกฤต")
    le = s["line"].get("last_error")
    lo = s["line"].get("last_ok")
    if le and (not lo or (s["line"].get("last_error_at") or "") > lo):
        problems.append("LINE ส่งไม่สำเร็จล่าสุด: " + le[:80])
    s["problems"] = problems
    s["healthy"] = not problems
    return s


def run_guarded(name, fn, *args, timeout=None, **kwargs):
    """Run a job and record its result. With timeout, the job runs in a helper
    thread so a hung job can never block the scheduler loop; the helper keeps
    running in the background and records its own result when it finishes."""
    started = time.time()
    job_started(name)
    if timeout is None:
        try:
            r = fn(*args, **kwargs)
            job_done(name, True, _summ(r), started)
            return r
        except Exception as e:
            job_done(name, False, f"{type(e).__name__}: {e}", started)
            logger.error("job %s failed: %s", name, e)
            return None
    box = {}
    # The helper thread does not inherit Flask's app context (it is thread-local),
    # so jobs that use current_app / settings / LINE failed with "Working outside of
    # application context" (14–15 Nov 2026). Carry the caller's app into the thread.
    app = None
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            app = current_app._get_current_object()
    except Exception:
        app = None

    def _call():
        if app is not None:
            with app.app_context():
                return fn(*args, **kwargs)
        return fn(*args, **kwargs)

    def _runner():
        try:
            box["r"] = _call()
            job_done(name, True, _summ(box["r"]), started)
        except Exception as e:
            box["e"] = e
            job_done(name, False, f"{type(e).__name__}: {e}", started)
            logger.error("job %s failed: %s", name, e)

    t = threading.Thread(target=_runner, daemon=True, name=f"hd-job-{name}")
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.error("job %s still running after %ss — scheduler continues", name, timeout)
        return None
    return box.get("r")


def _summ(r):
    if isinstance(r, dict):
        keys = [k for k in ("sent", "created", "inserted", "ok", "failed", "tables", "rows", "gcs", "drive", "verify", "error") if k in r]
        return ", ".join(f"{k}={r[k]}" for k in keys)[:200] or "ok"
    return "ok" if r is None else str(r)[:120]


def check_line_quota(token, http=None):
    """GET quota + consumption from LINE; records and returns {"type","value","used"} or None."""
    if not token:
        return None
    try:
        import requests as _rq
        http = http or _rq
        h = {"Authorization": f"Bearer {token}"}
        q = http.get("https://api.line.me/v2/bot/message/quota", headers=h, timeout=10).json()
        c = http.get("https://api.line.me/v2/bot/message/quota/consumption", headers=h, timeout=10).json()
        info = {"type": q.get("type"), "value": q.get("value"), "used": c.get("totalUsage")}
        line_quota(info)
        return info
    except Exception as e:
        logger.warning("line quota check: %s", type(e).__name__)
        return None


def start_watchdog(start_loop_fn, interval=300):
    """Restart the scheduler loop when it has not ticked for TICK_STALE_SEC.
    start_loop_fn() must start a fresh scheduler thread and return it."""
    def _wd():
        time.sleep(interval)
        while True:
            try:
                s = snapshot()
                age = s["tick_age_sec"]
                if age is not None and age > TICK_STALE_SEC:
                    with _LOCK:
                        _STATE["restarts"] += 1
                        _STATE["last_tick"] = _iso()   # avoid restart storm
                    logger.error("HD scheduler stale (%ss) — restarting thread", age)
                    start_loop_fn()
            except Exception as e:
                logger.warning("watchdog: %s", e)
            time.sleep(interval)
    t = threading.Thread(target=_wd, daemon=True, name="hd-watchdog")
    t.start()
    return t


def line_text(s):
    """Short Thai status for LINE / settings page."""
    L = ["🩺 สถานะงานอัตโนมัติ " + ("✅ ปกติ" if s["healthy"] else "⚠️ มีปัญหา")]
    if s.get("tick_age_sec") is not None:
        L.append(f"scheduler เดินล่าสุด {s['tick_age_sec'] // 60} นาทีก่อน (รอบที่ {s['tick_count']}, restart {s['restarts']})")
    q = (s.get("line") or {}).get("quota") or {}
    if q.get("value"):
        L.append(f"LINE ใช้ไป {q.get('used', '?')}/{q['value']} ข้อความ/เดือน" + (f" · งดส่งข้อความทั่วไปแล้ว {s['line'].get('suppressed', 0)} ครั้ง" if s['line'].get('suppressed') else ""))
    for p in s.get("problems", []):
        L.append("• " + p)
    return "\n".join(L)
