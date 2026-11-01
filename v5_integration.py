"""V5.0 Integration — เชื่อม 3 features เข้า server.py โดยแตะ server.py ให้น้อยที่สุด

  Feature 1: AgentOrchestrator   (agent_orchestrator.py)
  Feature 9: ThaiIntentParser    (thai_intent_parser.py)
  Feature 4: WorkflowEngine + UI (workflow_engine.py, workflow_ui.py)

server.py ใช้แค่:
    import v5_integration
    v5 = v5_integration.init(storage=..., doc_index=..., ...)         # หลัง V4.0 init
    v5_integration.register_routes(app, _admin_api_auth, v5)           # หลัง app สร้าง
    reply = v5.handle_line_text(text, user_hash)                       # ต้น handle_search_command
    v5.fire("document_indexed", {...})                                 # หลัง doc_index.add

หลักความปลอดภัย:
  - ไม่ใช้ External AI
  - LINE notify ส่งเฉพาะผู้รับ physician/admin ที่ลงทะเบียนในระบบ alert เดิม
  - ทุก exception ถูกจับ — ไม่ทำให้ webhook/upload ล้ม
  - Conversational layer ตอบเฉพาะ intent ที่ Agent จัดการเองได้
    ที่เหลือ passthrough ให้ legacy if/elif chain (พฤติกรรมเดิมคงอยู่ 100%)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from flask import jsonify, request

from agent_orchestrator import AgentOrchestrator
from thai_intent_parser import ThaiIntentParser
from workflow_engine import WorkflowEngine, EXAMPLE_WORKFLOWS

logger = logging.getLogger(__name__)


class V5:
    def __init__(self):
        self.parser = ThaiIntentParser()
        self.orchestrator = AgentOrchestrator()
        self.workflows = WorkflowEngine()
        self.enabled = os.getenv("V5_CONVERSATIONAL_ENABLED", "true").lower() in ("1", "true", "yes")
        self._deps: dict[str, Any] = {}

    # ── LINE entry point ──
    def handle_line_text(self, text: str, user_hash: str) -> Optional[str]:
        """ลอง parse + route ผ่าน Agent.

        คืน str = ตอบเลย, None = ให้ legacy chain จัดการ (passthrough)
        """
        if not self.enabled:
            return None
        try:
            parsed = self.parser.parse(text)
            if parsed.is_unknown:
                return None  # ให้ legacy ลองก่อน; ถ้า legacy ก็ไม่รู้จัก จะไป fallback เอง
            if not parsed.is_confident:
                # match pattern แต่ขาด entity (เช่น "ผลแล็บ" ไม่มี HN) → แนะนำ
                if parsed.suggestions:
                    return "💡 " + parsed.suggestions[0]
                return None
            result = self.orchestrator.route(parsed.intent, parsed.entities, user_hash)
            if result.data.get("passthrough") or not result.message:
                return None
            return result.message
        except Exception as e:
            logger.warning("v5.handle_line_text: %s", type(e).__name__)
            return None

    def fallback_suggestion(self, text: str) -> Optional[str]:
        """เรียกเมื่อ legacy chain ไม่รู้จักคำสั่ง — ให้คำแนะนำแทน 'ไม่เข้าใจ'."""
        try:
            parsed = self.parser.parse(text)
            if parsed.suggestions:
                return "🤔 ไม่เข้าใจคำสั่ง — ลองพิมพ์:\n• " + "\n• ".join(parsed.suggestions[:3])
        except Exception:
            pass
        return None

    # ── Workflow entry point ──
    def fire(self, trigger: str, ctx: dict) -> None:
        try:
            recs = self.workflows.fire(trigger, ctx)
            if recs:
                m = sum(1 for r in recs if r.matched)
                if m:
                    logger.info("workflow %s: %d matched", trigger, m)
        except Exception as e:
            logger.warning("v5.fire(%s): %s", trigger, type(e).__name__)


def init(**deps) -> V5:
    """สร้าง V5 + wire dependencies + register action handlers.

    deps ที่รองรับ: storage, doc_index, state_store, backup_manager, approval_engine,
                    field_encryption, rbac, policy_engine, expense_tracker,
                    push_message (fn(user_id, text)->bool), line_token (str),
                    event_logger, master_registry
    """
    v5 = V5()
    v5._deps = deps
    storage = deps.get("storage")

    try:
        v5.orchestrator.set_dependencies(**deps)
    except Exception as e:
        logger.warning("v5 orchestrator deps: %s", type(e).__name__)

    try:
        if storage:
            v5.workflows.set_storage(storage)
        if deps.get("policy_engine"):
            v5.workflows.set_policy_engine(deps["policy_engine"])
    except Exception as e:
        logger.warning("v5 workflow deps: %s", type(e).__name__)

    _register_action_handlers(v5, deps)
    logger.info(
        "V5.0 initialized: conversational=%s, workflows=%d, actions=%s",
        v5.enabled, len(v5.workflows.list()), sorted(v5.workflows._handlers.keys()),
    )
    return v5


def _register_action_handlers(v5: V5, deps: dict) -> None:
    wf = v5.workflows
    storage = deps.get("storage")
    push = deps.get("push_message")
    event_logger = deps.get("event_logger")

    # notify_line — ส่งเฉพาะผู้รับ physician/admin ที่ระบบ alert รู้จัก (ไม่รับ LINE ID จาก UI)
    def h_notify_line(params: dict, ctx: dict) -> None:
        if not push:
            raise RuntimeError("push_message not wired")
        msg = (params.get("message") or "").strip()
        if not msg:
            return
        target = (params.get("target") or "admin").strip()
        ids: list[str] = []
        try:
            from alert_delivery import resolve_alert_recipients
            rec = resolve_alert_recipients(storage)
            ids = list(rec.get("physician", []))
        except Exception as e:
            logger.warning("notify_line resolve: %s", type(e).__name__)
        if not ids:
            raise RuntimeError("no recipients")
        if target not in ("physician", "admin", "all_staff"):
            target = "admin"
        sent = 0
        for uid in ids[:10]:  # จำกัด
            try:
                if push(uid, f"⚙️ [Workflow]\n{msg[:900]}"):
                    sent += 1
            except Exception:
                pass
        if event_logger:
            try:
                event_logger.log("workflow_notify_line", data={"sent": sent, "target": target})
            except Exception:
                pass
    wf.register_action("notify_line", h_notify_line)

    # log_event — ผ่าน event_logger ถ้ามี
    def h_log_event(params: dict, ctx: dict) -> None:
        name = (params.get("event") or "workflow_event")[:40]
        msg = (params.get("message") or "")[:200]
        if event_logger:
            event_logger.log(f"wf_{name}", data={"message": msg, "category": ctx.get("category", "")})
        else:
            logger.info("[workflow] %s: %s", name, msg)
    wf.register_action("log_event", h_log_event)

    # tag_document / add_note / flag_review — เขียน sidecar JSON ใน GCS (ไม่แตะไฟล์ต้นฉบับ)
    def _sidecar(ctx: dict, key: str, value: Any) -> None:
        doc_id = ctx.get("doc_id") or ctx.get("id")
        if not storage or not doc_id:
            raise RuntimeError("no storage/doc_id")
        import json
        from datetime import datetime, timezone
        path = f"_workflow_tags/{doc_id}.json"
        data: dict = {}
        try:
            raw = storage.get(path)
            if raw:
                data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception:
            data = {}
        data.setdefault(key, [])
        if isinstance(data[key], list):
            data[key].append({"value": value, "at": datetime.now(timezone.utc).isoformat()})
        storage.put(path, json.dumps(data, ensure_ascii=False).encode("utf-8"), content_type="application/json")

    wf.register_action("tag_document", lambda p, c: _sidecar(c, "tags", (p.get("tag") or "")[:50]))
    wf.register_action("add_note", lambda p, c: _sidecar(c, "notes", (p.get("note") or "")[:500]))
    wf.register_action("flag_review", lambda p, c: _sidecar(c, "review_flags", (p.get("reason") or "")[:200]))

    # request_approval — ผ่าน ApprovalEngine เดิม
    ae = deps.get("approval_engine")
    if ae:
        def h_request_approval(params: dict, ctx: dict) -> None:
            at = (params.get("action_type") or "config_change").strip()
            if at not in ("config_change", "bulk_operation", "export_data"):
                at = "config_change"
            reason = (params.get("reason") or "workflow")[:200]
            ae.request_approval(
                action_type=at,
                requester_hash="workflow",
                description=f"[workflow] {reason}",
                metadata={"doc_id": str(ctx.get("doc_id", ""))[:40], "category": ctx.get("category", "")},
            )
        wf.register_action("request_approval", h_request_approval)

    # sheets_sync / send_email — ผ่าน modules เดิม (optional)
    def h_sheets_sync(params: dict, ctx: dict) -> None:
        import google_sheets_sync as gs
        if not gs.is_available():
            raise RuntimeError("sheets not available")
        mr = deps.get("master_registry")
        if mr:
            gs.sync_patient_registry(mr)
    wf.register_action("sheets_sync", h_sheets_sync)

    def h_send_email(params: dict, ctx: dict) -> None:
        import gmail_report as gr
        if not gr.is_available():
            raise RuntimeError("gmail not available")
        subj = (params.get("subject") or "[Workflow] แจ้งเตือน")[:120]
        body = (params.get("body") or "")[:2000]
        if hasattr(gr, "send_custom"):
            gr.send_custom(subject=subj, body=body)
        elif hasattr(gr, "send_report"):
            gr.send_report(subject=subj, body=body)
        else:
            raise RuntimeError("gmail send fn not found")
    wf.register_action("send_email", h_send_email)


# ─── Routes ─────────────────────────────────────────────

def register_routes(app, admin_auth: Callable, v5: V5) -> None:
    """ลงทะเบียน API + UI ทั้งหมดของ V5.0."""
    import hmac
    from workflow_ui import WORKFLOW_UI_HTML

    P = "/admin/api/v2"

    def _auth_or_cookie():
        err = admin_auth()
        if not err:
            return None
        secret = os.getenv("ADMIN_SECRET", "").strip()
        ck = request.cookies.get("admin_secret", "")
        if secret and ck and hmac.compare_digest(ck.encode(), secret.encode()):
            return None
        return err

    # ── Agents ──
    @app.route(f"{P}/agents", methods=["GET"])
    def v5_agents():
        err = admin_auth()
        if err:
            return err
        return jsonify({"agents": v5.orchestrator.get_agents_info(), "stats": v5.orchestrator.get_stats()})

    @app.route(f"{P}/agents/history", methods=["GET"])
    def v5_agents_history():
        err = admin_auth()
        if err:
            return err
        return jsonify({"history": v5.orchestrator.get_history(int(request.args.get("limit", 20)))})

    # ── Intent parser ──
    @app.route(f"{P}/intent/parse", methods=["POST"])
    def v5_intent_parse():
        err = admin_auth()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        text = str(body.get("text", ""))[:500]
        return jsonify(v5.parser.parse(text).to_dict())

    @app.route(f"{P}/intent/stats", methods=["GET"])
    def v5_intent_stats():
        err = admin_auth()
        if err:
            return err
        return jsonify({"stats": v5.parser.get_stats(), "intents": v5.parser.list_intents()})

    # ── Workflows ──
    W = f"{P}/workflows"

    @app.route(W, methods=["GET"])
    def v5_wf_list():
        err = _auth_or_cookie()
        if err:
            return err
        return jsonify({"workflows": v5.workflows.list()})

    @app.route(f"{W}/catalog", methods=["GET"])
    def v5_wf_catalog():
        err = _auth_or_cookie()
        if err:
            return err
        return jsonify(WorkflowEngine.catalog())

    @app.route(f"{W}/stats", methods=["GET"])
    def v5_wf_stats():
        err = _auth_or_cookie()
        if err:
            return err
        return jsonify(v5.workflows.get_stats())

    @app.route(f"{W}/history", methods=["GET"])
    def v5_wf_history():
        err = _auth_or_cookie()
        if err:
            return err
        return jsonify({"history": v5.workflows.get_history(int(request.args.get("limit", 50)))})

    @app.route(W, methods=["POST"])
    def v5_wf_create():
        err = _auth_or_cookie()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        wf, errs = v5.workflows.create(body, created_by="admin")
        if errs:
            return jsonify({"errors": errs}), 400
        return jsonify({"workflow": wf}), 201

    @app.route(f"{W}/seed", methods=["POST"])
    def v5_wf_seed():
        err = _auth_or_cookie()
        if err:
            return err
        existing = {w["name"] for w in v5.workflows.list()}
        n = 0
        for ex in EXAMPLE_WORKFLOWS:
            if ex["name"] in existing:
                continue
            wf, errs = v5.workflows.create(ex, created_by="seed")
            if not errs:
                n += 1
        return jsonify({"created": n})

    @app.route(f"{W}/<wf_id>", methods=["GET"])
    def v5_wf_get(wf_id):
        err = _auth_or_cookie()
        if err:
            return err
        wf = v5.workflows.get(wf_id)
        if not wf:
            return jsonify({"error": "not found"}), 404
        return jsonify(wf)

    @app.route(f"{W}/<wf_id>", methods=["PUT"])
    def v5_wf_update(wf_id):
        err = _auth_or_cookie()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        wf, errs = v5.workflows.update(wf_id, body)
        if errs:
            return jsonify({"errors": errs}), 400
        return jsonify({"workflow": wf})

    @app.route(f"{W}/<wf_id>/toggle", methods=["POST"])
    def v5_wf_toggle(wf_id):
        err = _auth_or_cookie()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        ok = v5.workflows.toggle(wf_id, bool(body.get("enabled", True)))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not found"}), 404))

    @app.route(f"{W}/<wf_id>/test", methods=["POST"])
    def v5_wf_test(wf_id):
        err = _auth_or_cookie()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        r = v5.workflows.test(wf_id, body.get("ctx") or {})
        if r is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({"result": r})

    @app.route(f"{W}/<wf_id>", methods=["DELETE"])
    def v5_wf_delete(wf_id):
        err = _auth_or_cookie()
        if err:
            return err
        ok = v5.workflows.delete(wf_id)
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not found"}), 404))

    @app.route(f"{W}/fire", methods=["POST"])
    def v5_wf_fire():
        """ยิง trigger ด้วยมือ (manual / ทดสอบ) — header auth เท่านั้น."""
        err = admin_auth()
        if err:
            return err
        body = request.get_json(silent=True) or {}
        trig = str(body.get("trigger", "manual"))
        recs = v5.workflows.fire(trig, body.get("ctx") or {}, dry_run=bool(body.get("dry_run", False)))
        return jsonify({"results": [r.to_dict() for r in recs]})

    # ── UI ──
    @app.route("/admin/workflows", methods=["GET"])
    def v5_wf_ui():
        err = _auth_or_cookie()
        if err:
            return "Unauthorized", 401
        return WORKFLOW_UI_HTML

    logger.info("V5.0 routes registered: agents, intent, workflows (+UI /admin/workflows)")
