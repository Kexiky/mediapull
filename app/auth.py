import os
import hmac
import hashlib
import time
import uuid
import re
from typing import Dict, Any, Optional
from fastapi import Request

import secrets
from app.database import get_user_by_username, get_user_by_id, ROLE_CONFIGS, DATA_DIR


def _get_secret_key() -> str:
    env_secret = os.getenv("MEDIAPULL_SECRET_KEY")
    if env_secret and env_secret.strip() and env_secret != "mediapull_kexiky_master_key_2026":
        return env_secret.strip()

    secret_file = DATA_DIR / ".secret_key"
    if secret_file.exists():
        try:
            key = secret_file.read_text(encoding="utf-8").strip()
            if key:
                return key
        except Exception:
            pass

    new_secret = secrets.token_hex(32)
    try:
        secret_file.write_text(new_secret, encoding="utf-8")
        print("[Security] Generated and stored persistent SECRET_KEY in data/.secret_key")
    except Exception:
        pass
    return new_secret


SECRET_KEY = _get_secret_key()
TOKEN_MAX_AGE_SECONDS = int(os.getenv("MEDIAPULL_TOKEN_MAX_AGE", 60 * 86400))  # 60 days default


def _sign_payload(payload: str) -> str:
    sig = hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_signature(signed_token: str) -> Optional[str]:
    if not signed_token or "." not in signed_token:
        return None
    payload, sig = signed_token.rsplit(".", 1)
    expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected_sig):
        return payload
    return None


def create_user_token(user_id: int, username: str, role: str) -> str:
    """Generate a signed long-lived authentication token for a user."""
    payload = f"mp:{user_id}:{username}:{role}:{int(time.time())}"
    return _sign_payload(payload)


def verify_user_token(token: str) -> Optional[Dict[str, Any]]:
    payload = _verify_signature(token)
    if not payload:
        return None

    parts = payload.split(":")
    # Support format: mp:{user_id}:{username}:{role}:{timestamp}
    if len(parts) >= 5 and parts[0] == "mp":
        try:
            token_ts = int(parts[4])
            if time.time() - token_ts > TOKEN_MAX_AGE_SECONDS:
                return None  # Token expired
        except (ValueError, IndexError):
            return None

        username = parts[2]
        user = get_user_by_username(username)
        return user

    # Backward compatibility with creator token: creator:kexiky:{timestamp}
    if len(parts) >= 3 and parts[0] == "creator" and parts[1] == "kexiky":
        try:
            token_ts = int(parts[2])
            if time.time() - token_ts > TOKEN_MAX_AGE_SECONDS:
                return None
        except (ValueError, IndexError):
            return None
        return get_user_by_username("kexiky")

    return None


def get_current_user(request: Request) -> Dict[str, Any]:
    """
    Extract user info from cookies or headers.
    Returns a unified user dict with role, storage quota, and TTL info.
    """
    # 1. Check Authenticated User Token
    auth_cookie = request.cookies.get("mediapull_auth")
    auth_header = request.headers.get("X-MediaPull-Token")
    if not auth_header:
        auth_bearer = request.headers.get("Authorization", "")
        if auth_bearer.startswith("Bearer "):
            auth_header = auth_bearer[7:].strip()

    token = auth_cookie or auth_header
    if token:
        user_record = verify_user_token(token)
        if user_record:
            role = user_record.get("role", "user")
            role_cfg = ROLE_CONFIGS.get(role, ROLE_CONFIGS["user"])
            username = user_record["username"]
            is_admin = (role == "admin") or (username == "kexiky")
            return {
                "user_id": username,
                "db_id": user_record["id"],
                "username": username,
                "role": role,
                "role_title": role_cfg["title"],
                "role_icon": role_cfg["icon"],
                "role_badge_class": role_cfg["badge_class"],
                "is_admin": is_admin,
                "is_creator": (username == "kexiky"),
                "is_permanent": (role_cfg["ttl_seconds"] is None),
                "ttl_seconds": role_cfg["ttl_seconds"],
                "ttl_days": role_cfg["ttl_days"],
                "ttl_display": role_cfg["ttl_display"],
                "storage_limit_bytes": role_cfg["storage_limit_bytes"],
                "storage_limit_display": role_cfg["storage_limit_display"],
                "created_at": user_record.get("created_at"),
                "is_new_session": False,
            }

    # 2. Check Guest Session
    session_cookie = request.cookies.get("mediapull_session")
    session_header = request.headers.get("X-MediaPull-Session")
    raw_session = session_cookie or session_header

    guest_cfg = ROLE_CONFIGS["guest"]
    if raw_session and re.match(r"^guest_[a-zA-Z0-9_-]{6,32}$", raw_session):
        guest_id = raw_session
        short_id = guest_id[-5:]
        return {
            "user_id": guest_id,
            "db_id": None,
            "username": f"Гость (#{short_id})",
            "role": "guest",
            "role_title": guest_cfg["title"],
            "role_icon": guest_cfg["icon"],
            "role_badge_class": guest_cfg["badge_class"],
            "is_admin": False,
            "is_creator": False,
            "is_permanent": False,
            "ttl_seconds": guest_cfg["ttl_seconds"],
            "ttl_days": guest_cfg["ttl_days"],
            "ttl_display": guest_cfg["ttl_display"],
            "storage_limit_bytes": guest_cfg["storage_limit_bytes"],
            "storage_limit_display": guest_cfg["storage_limit_display"],
            "created_at": None,
            "is_new_session": False,
        }

    # 3. Issue new guest session
    new_guest_id = f"guest_{uuid.uuid4().hex[:10]}"
    short_id = new_guest_id[-5:]
    return {
        "user_id": new_guest_id,
        "db_id": None,
        "username": f"Гость (#{short_id})",
        "role": "guest",
        "role_title": guest_cfg["title"],
        "role_icon": guest_cfg["icon"],
        "role_badge_class": guest_cfg["badge_class"],
        "is_admin": False,
        "is_creator": False,
        "is_permanent": False,
        "ttl_seconds": guest_cfg["ttl_seconds"],
        "ttl_days": guest_cfg["ttl_days"],
        "ttl_display": guest_cfg["ttl_display"],
        "storage_limit_bytes": guest_cfg["storage_limit_bytes"],
        "storage_limit_display": guest_cfg["storage_limit_display"],
        "created_at": None,
        "is_new_session": True,
    }
