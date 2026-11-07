"""HD Smart Monitoring — Authentication & Authorization.

Per-user JWT authentication with bcrypt password hashing,
role-based access control, and session management.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional

from .config import HdConfig

logger = logging.getLogger(__name__)

# ── Optional imports (graceful fallback for test envs) ────

try:
    import jwt as pyjwt
except ImportError:
    pyjwt = None  # type: ignore[assignment]

try:
    import bcrypt as _bcrypt
except ImportError:
    _bcrypt = None  # type: ignore[assignment]

try:
    import pyotp
except ImportError:
    pyotp = None  # type: ignore[assignment]

# ── Roles ─────────────────────────────────────────────────

ROLES = ("physician", "nurse", "admin", "auditor")

ROLE_PERMISSIONS = {
    "physician": {
        "session.read", "session.write", "session.approve",
        "vital.read", "vital.write",
        "alert.read", "alert.acknowledge",
        "prescription.read", "prescription.write",
        "patient.read", "patient.write",
        "report.read",
        "user.read",
    },
    "nurse": {
        "session.read", "session.write",
        "vital.read", "vital.write",
        "alert.read", "alert.acknowledge",
        "prescription.read",
        "patient.read",
        "report.read",
    },
    "admin": {
        "session.read", "session.write",
        "vital.read", "vital.write",
        "alert.read", "alert.acknowledge",
        "prescription.read",
        "patient.read", "patient.write",
        "report.read",
        "user.read", "user.write", "user.create",
        "system.config", "system.audit",
    },
    "auditor": {
        "session.read",
        "vital.read",
        "alert.read",
        "patient.read",
        "report.read",
        "system.audit",
    },
}


# ── Password Hashing ─────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    if _bcrypt is None:
        # Fallback: SHA-256 (test only, not for production)
        return "sha256:" + hashlib.sha256(password.encode()).hexdigest()
    salt = _bcrypt.gensalt(rounds=12)
    return _bcrypt.hashpw(password.encode(), salt).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash."""
    if hashed.startswith("sha256:"):
        return hmac.compare_digest(
            hashed, "sha256:" + hashlib.sha256(password.encode()).hexdigest()
        )
    if _bcrypt is None:
        return False
    return _bcrypt.checkpw(password.encode(), hashed.encode())


# ── JWT Token Management ─────────────────────────────────

def create_token(user_id: int, username: str, role: str,
                 expiry_minutes: Optional[int] = None) -> str:
    """Create a JWT token."""
    if pyjwt is None:
        # Fallback: simple base64 token for testing
        import base64
        payload = json.dumps({
            "user_id": user_id, "username": username, "role": role,
            "exp": time.time() + (expiry_minutes or HdConfig.JWT_EXPIRY_MINUTES) * 60,
        })
        return "test." + base64.urlsafe_b64encode(payload.encode()).decode()

    exp = datetime.now(timezone.utc) + timedelta(
        minutes=expiry_minutes or HdConfig.JWT_EXPIRY_MINUTES
    )
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "exp": exp,
        "iat": datetime.now(timezone.utc),
    }
    return pyjwt.encode(payload, HdConfig.JWT_SECRET, algorithm=HdConfig.JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token.  Returns payload or None."""
    if not token:
        return None

    # Test fallback
    if token.startswith("test."):
        import base64
        try:
            payload = json.loads(base64.urlsafe_b64decode(token[5:]))
            if payload.get("exp", 0) < time.time():
                return None
            return payload
        except Exception:
            return None

    if pyjwt is None:
        return None
    try:
        return pyjwt.decode(
            token, HdConfig.JWT_SECRET,
            algorithms=[HdConfig.JWT_ALGORITHM],
        )
    except pyjwt.ExpiredSignatureError:
        logger.info("JWT expired")
        return None
    except pyjwt.InvalidTokenError as e:
        logger.warning(f"Invalid JWT: {e}")
        return None


# ── Role-Based Decorator ─────────────────────────────────

def require_role(*allowed_roles):
    """Decorator: require the request bearer to have one of the allowed roles.

    Usage::

        @app.route("/hd/api/v1/sessions")
        @require_role("physician", "nurse")
        def list_sessions():
            ...
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            from flask import request, jsonify, g

            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return jsonify({"error": "Missing or invalid Authorization header"}), 401

            token = auth_header[7:]
            payload = decode_token(token)
            if payload is None:
                return jsonify({"error": "Invalid or expired token"}), 401

            role = payload.get("role", "")
            if role not in allowed_roles:
                return jsonify({"error": f"Role '{role}' not permitted"}), 403

            # Attach user info to Flask g
            g.hd_user_id = payload.get("user_id")
            g.hd_username = payload.get("username")
            g.hd_role = role

            return fn(*args, **kwargs)
        return wrapper
    return decorator


def has_permission(role: str, permission: str) -> bool:
    """Check if a role has a specific permission."""
    return permission in ROLE_PERMISSIONS.get(role, set())


# ── MFA (TOTP) — Structure only ──────────────────────────

def generate_mfa_secret() -> str:
    """Generate a new TOTP secret."""
    if pyotp is None:
        return os.urandom(20).hex()
    return pyotp.random_base32()


def verify_mfa_token(secret: str, token: str) -> bool:
    """Verify a TOTP token."""
    if pyotp is None:
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(token)


def get_mfa_provisioning_uri(secret: str, username: str) -> str:
    """Get the provisioning URI for QR code generation."""
    if pyotp is None:
        return ""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=username, issuer_name="RANONG-RAKTAI-AI HD")
