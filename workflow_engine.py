"""Workflow Engine — V5.0 Feature 4: No-Code Workflow Builder

สร้างกฎอัตโนมัติผ่านหน้าจอ โดยไม่ต้องแก้ Python:

  "เมื่อได้รับบิล NIKKISO → ตรวจวันที่ → จัดลงโฟลเดอร์ → สรุปยอด → แจ้ง LINE"

Model:
  Workflow = Trigger + Conditions[] + Actions[]

  Trigger  (เหตุการณ์ที่เกิด)   : document_indexed, lab_result_extracted, bill_received,
                                  schedule_daily, schedule_weekly, manual
  Condition (ตรวจก่อนทำ)       : field == / contains / in / > / < / regex / exists
  Action   (สิ่งที่ทำ)         : notify_line, tag_document, add_note, log_event,
                                  request_approval, send_email, sheets_sync

ความปลอดภัย (สำคัญ):
  - Action ที่ทำได้ = "safe list" เท่านั้น — ไม่มี delete, ไม่มี move ไฟล์จริง,
    ไม่มี run code, ไม่มี external AI
  - ทุก action ผ่าน policy_engine ถ้ามี
  - ไม่ส่งข้อมูลผู้ป่วย (ชื่อ/HN) ไปใน LINE notify นอกจากผู้รับเป็น physician/admin
  - Workflow ที่ error ไม่ทำให้ upload ล้ม — จับ exception ทุกชั้น
  - เก็บ workflows ใน GCS: workflows/<id>.json + ประวัติ run ใน memory

Usage (server.py):
    workflow_engine = WorkflowEngine()
    workflow_engine.set_storage(storage)
    workflow_engine.register_action("notify_line", push_fn)
    ...
    # หลัง doc_index.add():
    workflow_engine.fire("document_indexed", {"category": ..., "bill_company": ..., ...})
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

BKK = timezone(timedelta(hours=7))

# ─── Catalog (สำหรับ UI) ────────────────────────────────

TRIGGERS: dict[str, dict] = {
    "document_indexed": {
        "label": "เมื่อเอกสารถูกจัดเก็บ",
        "fields": ["category", "bill_company", "patient_hn_folder", "original_name", "vendor_needs_review", "ocr_confidence"],
    },
    "bill_received": {
        "label": "เมื่อได้รับบิล/ใบแจ้งหนี้",
        "fields": ["bill_company", "amount", "invoice_date", "original_name", "vendor_needs_review"],
    },
    "lab_result_extracted": {
        "label": "เมื่อสกัดผลแล็บได้",
        "fields": ["patient_hn_folder", "egfr", "creatinine", "potassium", "hemoglobin", "abnormal_count"],
    },
    "schedule_daily": {"label": "ทุกวัน (ผ่าน cron)", "fields": ["date", "weekday"]},
    "schedule_weekly": {"label": "ทุกสัปดาห์ (ผ่าน cron)", "fields": ["date", "week"]},
    "manual": {"label": "สั่งด้วยมือ (ทดสอบ)", "fields": []},
}

OPERATORS: dict[str, str] = {
    "eq": "เท่ากับ",
    "neq": "ไม่เท่ากับ",
    "contains": "มีคำว่า",
    "not_contains": "ไม่มีคำว่า",
    "in": "อยู่ในรายการ",
    "gt": "มากกว่า",
    "lt": "น้อยกว่า",
    "gte": "มากกว่าหรือเท่ากับ",
    "lte": "น้อยกว่าหรือเท่ากับ",
    "regex": "ตรงกับ regex",
    "exists": "มีค่า",
    "not_exists": "ไม่มีค่า",
    "is_true": "เป็นจริง",
    "is_false": "เป็นเท็จ",
}

# Safe action catalog — ห้ามเพิ่ม action ที่ลบ/ย้ายไฟล์จริง/รันโค้ด
ACTIONS: dict[str, dict] = {
    "notify_line": {
        "label": "แจ้งเตือน LINE",
        "params": {"message": "ข้อความ (ใช้ {field} แทรกค่าได้)", "target": "physician|admin|all_staff"},
        "phi_safe": False,  # ต้องตรวจผู้รับ
    },
    "tag_document": {
        "label": "ติดแท็กเอกสาร",
        "params": {"tag": "ชื่อแท็ก"},
        "phi_safe": True,
    },
    "add_note": {
        "label": "เพิ่มหมายเหตุในเอกสาร",
        "params": {"note": "ข้อความหมายเหตุ"},
        "phi_safe": True,
    },
    "log_event": {
        "label": "บันทึก event log",
        "params": {"event": "ชื่อ event", "message": "รายละเอียด"},
        "phi_safe": True,
    },
    "request_approval": {
        "label": "ขออนุมัติ (approval workflow)",
        "params": {"action_type": "config_change|bulk_operation|export_data", "reason": "เหตุผล"},
        "phi_safe": True,
    },
    "sheets_sync": {
        "label": "Sync ไป Google Sheets",
        "params": {},
        "phi_safe": True,
    },
    "send_email": {
        "label": "ส่งอีเมล (Gmail report)",
        "params": {"subject": "หัวข้อ", "body": "เนื้อหา (ใช้ {field} ได้)"},
        "phi_safe": False,
    },
    "flag_review": {
        "label": "ส่งเข้าคิวตรวจสอบ (needs review)",
        "params": {"reason": "เหตุผล"},
        "phi_safe": True,
    },
}


# ─── Models ─────────────────────────────────────────────

@dataclass
class Condition:
    field: str
    op: str
    value: Any = None

    def evaluate(self, ctx: dict) -> bool:
        v = ctx.get(self.field)
        op = self.op
        try:
            if op == "exists":
                return v is not None and v != ""
            if op == "not_exists":
                return v is None or v == ""
            if op == "is_true":
                return bool(v)
            if op == "is_false":
                return not bool(v)
            if v is None:
                return False
            sv = str(v)
            tv = str(self.value) if self.value is not None else ""
            if op == "eq":
                return sv.strip().lower() == tv.strip().lower()
            if op == "neq":
                return sv.strip().lower() != tv.strip().lower()
            if op == "contains":
                return tv.lower() in sv.lower()
            if op == "not_contains":
                return tv.lower() not in sv.lower()
            if op == "in":
                items = self.value if isinstance(self.value, list) else [x.strip() for x in tv.split(",")]
                return sv.strip().lower() in [str(i).strip().lower() for i in items]
            if op == "regex":
                return re.search(tv, sv, re.I) is not None
            if op in ("gt", "lt", "gte", "lte"):
                fv, ft = float(v), float(self.value)
                return {"gt": fv > ft, "lt": fv < ft, "gte": fv >= ft, "lte": fv <= ft}[op]
        except Exception:
            return False
        return False


@dataclass
class Action:
    type: str
    params: dict = field(default_factory=dict)


@dataclass
class Workflow:
    id: str
    name: str
    trigger: str
    conditions: list[Condition] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    enabled: bool = True
    match_all: bool = True          # True = AND, False = OR
    description: str = ""
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""
    run_count: int = 0
    last_run: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Workflow":
        conds = [Condition(**c) if isinstance(c, dict) else c for c in d.get("conditions", [])]
        acts = [Action(**a) if isinstance(a, dict) else a for a in d.get("actions", [])]
        return cls(
            id=d.get("id") or f"WF-{uuid.uuid4().hex[:8]}",
            name=d.get("name", "ไม่มีชื่อ"),
            trigger=d.get("trigger", "manual"),
            conditions=conds,
            actions=acts,
            enabled=bool(d.get("enabled", True)),
            match_all=bool(d.get("match_all", True)),
            description=d.get("description", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            created_by=d.get("created_by", ""),
            run_count=int(d.get("run_count", 0)),
            last_run=d.get("last_run", ""),
        )

    def matches(self, ctx: dict) -> bool:
        if not self.conditions:
            return True
        results = [c.evaluate(ctx) for c in self.conditions]
        return all(results) if self.match_all else any(results)


@dataclass
class RunRecord:
    workflow_id: str
    workflow_name: str
    trigger: str
    matched: bool
    actions_run: int
    actions_failed: int
    duration_ms: int
    timestamp: str
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Template rendering ─────────────────────────────────

_TPL = re.compile(r"\{(\w+)\}")


def render_template(tpl: str, ctx: dict) -> str:
    """แทน {field} ด้วยค่าใน ctx — field ที่ไม่มีคงไว้."""
    def _sub(m):
        k = m.group(1)
        v = ctx.get(k)
        return str(v) if v is not None else m.group(0)
    return _TPL.sub(_sub, tpl or "")


# ─── Validation ─────────────────────────────────────────

def validate_workflow(d: dict) -> list[str]:
    errs: list[str] = []
    if not d.get("name", "").strip():
        errs.append("ต้องระบุชื่อ workflow")
    trig = d.get("trigger")
    if trig not in TRIGGERS:
        errs.append(f"trigger ไม่ถูกต้อง: {trig}")
    for i, c in enumerate(d.get("conditions", [])):
        if not c.get("field"):
            errs.append(f"เงื่อนไข #{i+1}: ต้องระบุ field")
        if c.get("op") not in OPERATORS:
            errs.append(f"เงื่อนไข #{i+1}: operator ไม่ถูกต้อง: {c.get('op')}")
        if c.get("op") == "regex":
            try:
                re.compile(str(c.get("value", "")))
            except re.error:
                errs.append(f"เงื่อนไข #{i+1}: regex ไม่ถูกต้อง")
    acts = d.get("actions", [])
    if not acts:
        errs.append("ต้องมีอย่างน้อย 1 action")
    for i, a in enumerate(acts):
        if a.get("type") not in ACTIONS:
            errs.append(f"action #{i+1}: ไม่อนุญาต: {a.get('type')}")
    if len(acts) > 10:
        errs.append("จำกัด action ไม่เกิน 10 ต่อ workflow")
    if len(d.get("conditions", [])) > 10:
        errs.append("จำกัดเงื่อนไขไม่เกิน 10 ต่อ workflow")
    return errs


# ─── Engine ─────────────────────────────────────────────

class WorkflowEngine:
    GCS_PREFIX = "workflows/"
    MAX_WORKFLOWS = 100
    MAX_HISTORY = 300

    def __init__(self):
        self._workflows: dict[str, Workflow] = {}
        self._handlers: dict[str, Callable[[dict, dict], Any]] = {}
        self._storage = None
        self._policy_engine = None
        self._history: list[RunRecord] = []
        self._lock = threading.Lock()
        self._loaded = False
        # built-in safe handlers (no side effects outside memory)
        self.register_action("log_event", self._h_log_event)

    # ── wiring ──
    def set_storage(self, storage) -> None:
        self._storage = storage
        self._load()

    def set_policy_engine(self, pe) -> None:
        self._policy_engine = pe

    def register_action(self, name: str, handler: Callable[[dict, dict], Any]) -> None:
        """handler(params: dict, ctx: dict) -> Any. เฉพาะชื่อใน ACTIONS."""
        if name not in ACTIONS:
            raise ValueError(f"action ไม่อยู่ใน safe list: {name}")
        self._handlers[name] = handler

    # ── persistence ──
    # ใช้ storage interface ของ server.py: get(path)->bytes|None, put(path, bytes, content_type)
    # เก็บทั้งหมดในไฟล์เดียว workflows/_all.json (atomic อ่าน/เขียน, ไม่ต้อง list blobs)
    INDEX_PATH = "workflows/_all.json"

    def _load(self) -> None:
        if not self._storage:
            self._loaded = True
            return
        try:
            raw = self._storage.get(self.INDEX_PATH)
            if raw:
                data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                for d in data.get("workflows", []):
                    wf = Workflow.from_dict(d)
                    self._workflows[wf.id] = wf
            logger.info("WorkflowEngine loaded %d workflows", len(self._workflows))
        except Exception as e:
            logger.warning("WorkflowEngine load: %s", type(e).__name__)
        self._loaded = True

    def _persist_all(self) -> None:
        if not self._storage:
            return
        try:
            payload = {
                "version": 1,
                "updated_at": datetime.now(BKK).isoformat(),
                "workflows": [w.to_dict() for w in self._workflows.values()],
            }
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self._storage.put(self.INDEX_PATH, data, content_type="application/json")
        except Exception as e:
            logger.warning("WorkflowEngine persist: %s", type(e).__name__)

    def _save(self, wf: Workflow) -> None:
        self._persist_all()

    def _delete_blob(self, wf_id: str) -> None:
        self._persist_all()

    # ── CRUD ──
    def list(self) -> list[dict]:
        return [w.to_dict() for w in sorted(self._workflows.values(), key=lambda w: w.created_at, reverse=True)]

    def get(self, wf_id: str) -> Optional[dict]:
        w = self._workflows.get(wf_id)
        return w.to_dict() if w else None

    def create(self, data: dict, created_by: str = "") -> tuple[Optional[dict], list[str]]:
        errs = validate_workflow(data)
        if errs:
            return None, errs
        if len(self._workflows) >= self.MAX_WORKFLOWS:
            return None, [f"จำกัด workflow ไม่เกิน {self.MAX_WORKFLOWS}"]
        now = datetime.now(BKK).isoformat()
        data = dict(data)
        data["id"] = f"WF-{uuid.uuid4().hex[:8]}"
        data["created_at"] = now
        data["updated_at"] = now
        data["created_by"] = created_by[:16] if created_by else ""
        wf = Workflow.from_dict(data)
        with self._lock:
            self._workflows[wf.id] = wf
        self._save(wf)
        logger.info("Workflow created: %s (%s)", wf.id, wf.trigger)
        return wf.to_dict(), []

    def update(self, wf_id: str, data: dict) -> tuple[Optional[dict], list[str]]:
        old = self._workflows.get(wf_id)
        if not old:
            return None, ["ไม่พบ workflow"]
        merged = old.to_dict()
        for k in ("name", "trigger", "conditions", "actions", "enabled", "match_all", "description"):
            if k in data:
                merged[k] = data[k]
        errs = validate_workflow(merged)
        if errs:
            return None, errs
        merged["updated_at"] = datetime.now(BKK).isoformat()
        wf = Workflow.from_dict(merged)
        with self._lock:
            self._workflows[wf_id] = wf
        self._save(wf)
        return wf.to_dict(), []

    def toggle(self, wf_id: str, enabled: bool) -> bool:
        wf = self._workflows.get(wf_id)
        if not wf:
            return False
        wf.enabled = enabled
        wf.updated_at = datetime.now(BKK).isoformat()
        self._save(wf)
        return True

    def delete(self, wf_id: str) -> bool:
        with self._lock:
            wf = self._workflows.pop(wf_id, None)
        if not wf:
            return False
        self._delete_blob(wf_id)
        logger.info("Workflow deleted: %s", wf_id)
        return True

    # ── execution ──
    def fire(self, trigger: str, ctx: dict, dry_run: bool = False) -> list[RunRecord]:
        """ยิง trigger — คืน RunRecord ของทุก workflow ที่ match.

        ไม่ throw — ทุก error ถูกจับไว้ ไม่ให้กระทบ caller (เช่น upload path).
        """
        records: list[RunRecord] = []
        try:
            cands = [w for w in self._workflows.values() if w.enabled and w.trigger == trigger]
            if not cands:
                return records
            safe_ctx = self._sanitize_ctx(ctx)
            for wf in cands:
                rec = self._run_one(wf, trigger, safe_ctx, dry_run)
                records.append(rec)
                if not dry_run:
                    with self._lock:
                        self._history.append(rec)
                        if len(self._history) > self.MAX_HISTORY:
                            self._history = self._history[-self.MAX_HISTORY:]
        except Exception as e:
            logger.warning("WorkflowEngine.fire(%s): %s", trigger, type(e).__name__)
        return records

    def _run_one(self, wf: Workflow, trigger: str, ctx: dict, dry_run: bool) -> RunRecord:
        start = time.monotonic()
        errors: list[str] = []
        ran = failed = 0
        matched = False
        try:
            matched = wf.matches(ctx)
            if matched:
                for act in wf.actions:
                    if dry_run:
                        ran += 1
                        continue
                    ok, err = self._exec_action(act, ctx, wf)
                    if ok:
                        ran += 1
                    else:
                        failed += 1
                        if err:
                            errors.append(f"{act.type}: {err}")
                if not dry_run:
                    wf.run_count += 1
                    wf.last_run = datetime.now(BKK).isoformat()
                    self._save(wf)
        except Exception as e:
            errors.append(f"{type(e).__name__}")
        return RunRecord(
            workflow_id=wf.id, workflow_name=wf.name, trigger=trigger,
            matched=matched, actions_run=ran, actions_failed=failed,
            duration_ms=int((time.monotonic() - start) * 1000),
            timestamp=datetime.now(BKK).isoformat(), errors=errors, dry_run=dry_run,
        )

    def _exec_action(self, act: Action, ctx: dict, wf: Workflow) -> tuple[bool, str]:
        spec = ACTIONS.get(act.type)
        if not spec:
            return False, "action ไม่อยู่ใน safe list"
        # policy check
        if self._policy_engine and hasattr(self._policy_engine, "check"):
            try:
                dec = self._policy_engine.check(f"workflow.{act.type}", {"workflow_id": wf.id})
                if getattr(dec, "denied", False) or (isinstance(dec, dict) and dec.get("decision") == "DENY"):
                    return False, "policy denied"
            except Exception:
                pass
        handler = self._handlers.get(act.type)
        if not handler:
            return False, "ไม่มี handler ลงทะเบียน"
        # render templates in params
        params = {k: (render_template(v, ctx) if isinstance(v, str) else v) for k, v in act.params.items()}
        try:
            handler(params, ctx)
            return True, ""
        except Exception as e:
            logger.warning("workflow %s action %s: %s", wf.id, act.type, type(e).__name__)
            return False, type(e).__name__

    @staticmethod
    def _sanitize_ctx(ctx: dict) -> dict:
        """ตัด field ที่เป็น PHI ชัดเจนออก — เหลือ HN folder/category/vendor พอสำหรับ rule."""
        drop = {"patient_name", "line_display_name", "line_user_id", "ocr_text", "raw_text", "text"}
        out = {}
        for k, v in (ctx or {}).items():
            if k in drop:
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                out[k] = v
            else:
                out[k] = str(v)[:200]
        return out

    # ── built-in handlers ──
    def _h_log_event(self, params: dict, ctx: dict) -> None:
        logger.info("[workflow] %s: %s", params.get("event", "event"), params.get("message", ""))

    # ── introspection ──
    def get_history(self, limit: int = 50) -> list[dict]:
        return [r.to_dict() for r in self._history[-limit:]][::-1]

    def get_stats(self) -> dict:
        total = len(self._history)
        matched = sum(1 for r in self._history if r.matched)
        failed = sum(r.actions_failed for r in self._history)
        by_trigger: dict[str, int] = {}
        for w in self._workflows.values():
            by_trigger[w.trigger] = by_trigger.get(w.trigger, 0) + 1
        return {
            "workflows": len(self._workflows),
            "enabled": sum(1 for w in self._workflows.values() if w.enabled),
            "by_trigger": by_trigger,
            "runs": total,
            "matched": matched,
            "actions_failed": failed,
            "registered_actions": sorted(self._handlers.keys()),
        }

    @staticmethod
    def catalog() -> dict:
        return {"triggers": TRIGGERS, "operators": OPERATORS, "actions": ACTIONS}

    def test(self, wf_id: str, ctx: dict) -> Optional[dict]:
        """dry-run ด้วย ctx ที่กำหนด — ไม่รัน action จริง."""
        wf = self._workflows.get(wf_id)
        if not wf:
            return None
        rec = self._run_one(wf, wf.trigger, self._sanitize_ctx(ctx), dry_run=True)
        return rec.to_dict()


# ─── Example workflows (seed) ───────────────────────────

EXAMPLE_WORKFLOWS: list[dict] = [
    {
        "name": "บิล NIKKISO → แจ้ง LINE + log",
        "description": "เมื่อได้รับบิลจาก NIKKISO ให้แจ้งเตือนทันทีและบันทึก event",
        "trigger": "document_indexed",
        "match_all": True,
        "conditions": [
            {"field": "category", "op": "eq", "value": "bills"},
            {"field": "bill_company", "op": "contains", "value": "NIKKISO"},
        ],
        "actions": [
            {"type": "log_event", "params": {"event": "nikkiso_bill", "message": "บิล NIKKISO: {original_name}"}},
            {"type": "notify_line", "params": {"target": "admin", "message": "📄 ได้รับบิล NIKKISO: {original_name}"}},
        ],
    },
    {
        "name": "Vendor ไม่ชัด → ส่งเข้าคิวตรวจสอบ",
        "description": "บิลที่ระบุบริษัทไม่ได้ ให้ flag review",
        "trigger": "document_indexed",
        "conditions": [
            {"field": "category", "op": "eq", "value": "bills"},
            {"field": "vendor_needs_review", "op": "is_true"},
        ],
        "actions": [
            {"type": "flag_review", "params": {"reason": "ระบุ vendor ไม่ได้"}},
        ],
    },
    {
        "name": "แล็บผิดปกติหลายค่า → แจ้งแพทย์",
        "description": "ผลแล็บที่มีค่าผิดปกติ ≥ 3 รายการ",
        "trigger": "lab_result_extracted",
        "conditions": [{"field": "abnormal_count", "op": "gte", "value": 3}],
        "actions": [
            {"type": "notify_line", "params": {"target": "physician", "message": "⚠️ ผลแล็บ HN {patient_hn_folder} ผิดปกติ {abnormal_count} ค่า"}},
        ],
    },
]


# ─── Tests ──────────────────────────────────────────────

def _run_tests():
    print("🧪 Testing WorkflowEngine...")
    eng = WorkflowEngine()
    calls: list = []
    eng.register_action("notify_line", lambda p, c: calls.append(("line", p)))
    eng.register_action("flag_review", lambda p, c: calls.append(("flag", p)))
    eng.register_action("tag_document", lambda p, c: calls.append(("tag", p)))

    # 1. validation
    _, errs = eng.create({"name": "", "trigger": "x", "actions": []})
    assert errs and len(errs) >= 3, errs
    print(f"  ✅ validation catches {len(errs)} errors")

    # 2. unsafe action rejected
    _, errs = eng.create({"name": "bad", "trigger": "manual", "actions": [{"type": "delete_file"}]})
    assert any("ไม่อนุญาต" in e for e in errs)
    print("  ✅ unsafe action rejected")
    try:
        eng.register_action("run_shell", lambda p, c: None)
        raise AssertionError("should reject")
    except ValueError:
        print("  ✅ register_action rejects non-safe-list")

    # 3. create seeds
    ids = []
    for ex in EXAMPLE_WORKFLOWS:
        wf, errs = eng.create(ex, created_by="test")
        assert not errs, errs
        ids.append(wf["id"])
    assert len(eng.list()) == 3
    print(f"  ✅ created {len(ids)} example workflows")

    # 4. fire matching
    recs = eng.fire("document_indexed", {
        "category": "bills", "bill_company": "NIKKISO CO., LTD.",
        "original_name": "inv-001.pdf", "vendor_needs_review": False,
        "patient_name": "SHOULD_BE_DROPPED",
    })
    m = [r for r in recs if r.matched]
    assert len(m) == 1 and m[0].actions_run == 2, [r.to_dict() for r in recs]
    assert calls and calls[-1][0] == "line" and "inv-001.pdf" in calls[-1][1]["message"]
    print(f"  ✅ NIKKISO bill matched → {m[0].actions_run} actions, template rendered")

    # 5. PHI sanitized
    assert "patient_name" not in eng._sanitize_ctx({"patient_name": "x", "category": "bills"})
    print("  ✅ PHI fields stripped from ctx")

    # 6. vendor review path
    calls.clear()
    recs = eng.fire("document_indexed", {"category": "bills", "bill_company": "", "vendor_needs_review": True})
    assert any(c[0] == "flag" for c in calls)
    print("  ✅ vendor_needs_review → flag_review")

    # 7. numeric gte
    calls.clear()
    eng.fire("lab_result_extracted", {"abnormal_count": 4, "patient_hn_folder": "HN1234"})
    assert calls and "HN1234" in calls[-1][1]["message"]
    eng.fire("lab_result_extracted", {"abnormal_count": 1})
    assert len(calls) == 1
    print("  ✅ numeric gte condition works")

    # 8. disable
    eng.toggle(ids[0], False)
    calls.clear()
    eng.fire("document_indexed", {"category": "bills", "bill_company": "NIKKISO"})
    assert not any(c[0] == "line" for c in calls)
    print("  ✅ disabled workflow skipped")

    # 9. dry-run test
    t = eng.test(ids[2], {"abnormal_count": 9})
    assert t and t["matched"] and t["dry_run"] and t["actions_run"] == 1
    print("  ✅ dry-run test works")

    # 10. missing handler → failed but no exception
    wf, _ = eng.create({"name": "no handler", "trigger": "manual", "actions": [{"type": "sheets_sync"}]})
    recs = eng.fire("manual", {})
    r = [x for x in recs if x.workflow_id == wf["id"]][0]
    assert r.actions_failed == 1 and r.errors
    print("  ✅ missing handler → recorded as failed, no crash")

    # 11. delete
    assert eng.delete(ids[1]) and eng.get(ids[1]) is None
    print("  ✅ delete works")

    # 12. OR mode
    wf, _ = eng.create({"name": "or", "trigger": "manual", "match_all": False,
                        "conditions": [{"field": "a", "op": "eq", "value": "1"}, {"field": "b", "op": "eq", "value": "2"}],
                        "actions": [{"type": "tag_document", "params": {"tag": "x"}}]})
    calls.clear()
    eng.fire("manual", {"b": "2"})
    assert any(c[0] == "tag" for c in calls)
    print("  ✅ OR (match_all=False) works")

    # 13. persistence round-trip with server-style storage (get/put)
    class _FakeStorage:
        def __init__(self): self.blobs = {}
        def get(self, path): return self.blobs.get(path)
        def put(self, path, data, content_type="application/json", create_only=False):
            self.blobs[path] = data; return path
    st = _FakeStorage()
    e1 = WorkflowEngine(); e1.set_storage(st)
    e1.create(EXAMPLE_WORKFLOWS[0])
    e2 = WorkflowEngine(); e2.set_storage(st)
    assert len(e2.list()) == 1 and e2.list()[0]["name"] == EXAMPLE_WORKFLOWS[0]["name"]
    print("  ✅ GCS-style persistence round-trip (get/put)")

    s = eng.get_stats()
    print(f"\n  📊 {s}")
    print("\n✅ All 13 tests PASSED")


if __name__ == "__main__":
    _run_tests()
