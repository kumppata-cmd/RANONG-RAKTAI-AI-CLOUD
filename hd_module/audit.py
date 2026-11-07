"""HD Smart Monitoring — Audit Trail.

Every clinical action is logged with who, what, when, IP,
and before/after values.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# In-memory store for testing (replaced by PostgreSQL in production)
_audit_store: list[dict] = []


def log_action(
    user_id: Optional[int],
    action: str,
    resource_type: str = "",
    resource_id: Optional[int] = None,
    old_values: Any = None,
    new_values: Any = None,
    ip_address: str = "",
    user_agent: str = "",
    db_session=None,
) -> dict:
    """Log a clinical action to the audit trail.

    Args:
        user_id: ID of the user performing the action.
        action: Short description (e.g. "session.create", "vital.record").
        resource_type: Type of resource (e.g. "session", "vital_sign").
        resource_id: ID of the affected resource.
        old_values: Previous state (dict or None).
        new_values: New state (dict or None).
        ip_address: Client IP.
        user_agent: Client user-agent string.
        db_session: SQLAlchemy session (optional; in-memory fallback).

    Returns:
        The audit log entry as a dict.
    """
    entry = {
        "user_id": user_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "old_values": json.dumps(old_values, ensure_ascii=False, default=str) if old_values else None,
        "new_values": json.dumps(new_values, ensure_ascii=False, default=str) if new_values else None,
        "ip_address": ip_address,
        "user_agent": user_agent[:200] if user_agent else "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if db_session is not None:
        try:
            from .models import HdAuditLog
            log = HdAuditLog(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                old_values=entry["old_values"],
                new_values=entry["new_values"],
                ip_address=ip_address,
                user_agent=entry["user_agent"],
            )
            db_session.add(log)
            db_session.flush()
            entry["id"] = log.id
        except Exception as e:
            logger.error(f"Failed to write audit log to DB: {e}")
            _audit_store.append(entry)
    else:
        _audit_store.append(entry)
        entry["id"] = len(_audit_store)

    # Never log patient data or secrets in application logs
    logger.info(
        f"AUDIT: user={user_id} action={action} "
        f"resource={resource_type}:{resource_id}"
    )

    return entry


def query_audit_log(
    user_id: Optional[int] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 50,
    db_session=None,
) -> list[dict]:
    """Query the audit trail.

    Args:
        user_id: Filter by user.
        action: Filter by action type.
        resource_type: Filter by resource type.
        since: ISO datetime string — entries after this time.
        limit: Max entries to return.
        db_session: SQLAlchemy session (optional).

    Returns:
        List of audit log entries, newest first.
    """
    if db_session is not None:
        try:
            from .models import HdAuditLog
            q = db_session.query(HdAuditLog)
            if user_id is not None:
                q = q.filter(HdAuditLog.user_id == user_id)
            if action:
                q = q.filter(HdAuditLog.action == action)
            if resource_type:
                q = q.filter(HdAuditLog.resource_type == resource_type)
            if since:
                q = q.filter(HdAuditLog.created_at >= since)
            rows = q.order_by(HdAuditLog.created_at.desc()).limit(limit).all()
            return [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    "action": r.action,
                    "resource_type": r.resource_type,
                    "resource_id": r.resource_id,
                    "old_values": r.old_values,
                    "new_values": r.new_values,
                    "ip_address": r.ip_address,
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"DB audit query failed, using in-memory: {e}")

    # In-memory fallback
    results = list(_audit_store)
    if user_id is not None:
        results = [e for e in results if e.get("user_id") == user_id]
    if action:
        results = [e for e in results if e.get("action") == action]
    if resource_type:
        results = [e for e in results if e.get("resource_type") == resource_type]
    if since:
        results = [e for e in results if e.get("created_at", "") >= since]
    results.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return results[:limit]


def clear_in_memory_store():
    """Clear the in-memory audit store (test helper)."""
    _audit_store.clear()
