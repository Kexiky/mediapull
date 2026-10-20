import os
import hmac
import hashlib
import time
import uuid
import re
import secrets
from typing import Dict, Any, Optional

from fastapi import Depends, HTTPException, Request

from app.database import get_user_by_id, ROLE_CONFIGS, CREATOR_USERNAME, DATA_DIR
from app.ratelimit import SlidingWindowLimiter, client_ip

GUEST_ID_RE = re.compile(r"^guest_[a-f0-9]{10,32}$")

# New anonymous sessions per client address per hour (guests are free to create, so cap them).
_new_guest_limiter = SlidingWindowLimiter(
    max_events=int(os.getenv("MEDIAPULL_GUEST_SESSIONS_PER_HOUR", "60")), window_seconds=3600
)


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
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        print("[Security] Generated and stored persistent SECRET_KEY in data/.secret_key")
    except Exception as e:
        print(f"[Security] WARNING: could not persist SECRET_KEY ({e}); sessions will reset on restart")
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
    """Generate a signed authentication token for a user."""
    payload = f"mp:{user_id}:{username}:{role}:{int(time.time())}"
    return _sign_payload(payload)


def verify_user_token(token: str) -> Optional[Dict[str, Any]]:
    """Resolve a token to a user record.

    The account is looked up by its numeric id and the username embedded in the token must match
    it exactly, so a token can never be redirected to another account through case folding.
    """
    payload = _verify_signature(token)
    if not payload:
        return None

    parts = payload.split(":")
    if len(parts) != 5 or parts[0] != "mp":
        return None
    try:
        user_id = int(parts[1])
        token_ts = int(parts[4])
    except ValueError:
        return None

    if time.time() - token_ts > TOKEN_MAX_AGE_SECONDS:
        return None

    user = get_user_by_id(user_id)
    if not user or user["username"] != parts[2]:
        return None
    if token_ts < int(user.get("tokens_valid_after") or 0):
        return None  # revoked by a password change / reset
    return user


def create_guest_session_token(guest_id: str) -> str:
    return _sign_payload(guest_id)


def verify_guest_session_token(token: Optional[str]) -> Optional[str]:
    payload = _verify_signature(token or "")
    if payload and GUEST_ID_RE.match(payload):
        return payload
    return None


def _build_user(
    *,
    user_id: str,
    db_id: Optional[int],
    username: str,
    role: str,
    created_at: Optional[float],
    is_new_session: bool = False,
    session_token: Optional[str] = None,
) -> Dict[str, Any]:
    role_cfg = ROLE_CONFIGS.get(role, ROLE_CONFIGS["user"])
    is_creator = (role != "guest" and username == CREATOR_USERNAME)
    return {
        "user_id": user_id,
        "db_id": db_id,
        "username": username,
        "role": role,
        "role_title": role_cfg["title"],
        "role_icon": role_cfg["icon"],
        "role_badge_class": role_cfg["badge_class"],
        "is_admin": role == "admin",
        "is_creator": is_creator,
        "is_permanent": role_cfg["ttl_seconds"] is None,
        "ttl_seconds": role_cfg["ttl_seconds"],
        "ttl_days": role_cfg["ttl_days"],
        "ttl_display": role_cfg["ttl_display"],
        "storage_limit_bytes": role_cfg["storage_limit_bytes"],
        "storage_limit_display": role_cfg["storage_limit_display"],
        "created_at": created_at,
        "is_new_session": is_new_session,
        "session_token": session_token,
    }


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    """User dict that is safe to return to the browser (no session secrets)."""
    return {k: v for k, v in user.items() if k != "session_token"}


def get_current_user(request: Request) -> Dict[str, Any]:
    """
    Extract user info from cookies or headers.
    Returns a unified user dict with role, storage quota, and TTL info.
    """
    # 1. Authenticated user token
    auth_cookie = request.cookies.get("mediapull_auth")
    auth_header = request.headers.get("X-MediaPull-Token")
    if not auth_header:
        auth_bearer = request.headers.get("Authorization", "")
        if auth_bearer.startswith("Bearer "):
            auth_header = auth_bearer[7:].strip()

    for token in (auth_cookie, auth_header):
        if not token:
            continue
        user_record = verify_user_token(token)
        if user_record:
            username = user_record["username"]
            return _build_user(
                user_id=username,
                db_id=user_record["id"],
                username=username,
                role=user_record.get("role", "user"),
                created_at=user_record.get("created_at"),
            )

    # 2. Existing guest session (signed, so a client cannot pick someone else's id)
    guest_id = verify_guest_session_token(
        request.cookies.get("mediapull_session") or request.headers.get("X-MediaPull-Session")
    )
    if guest_id:
        return _build_user(
            user_id=guest_id,
            db_id=None,
            username=f"Гость (#{guest_id[-5:]})",
            role="guest",
            created_at=None,
        )

    # 3. Issue a new guest session
    ip = client_ip(request)
    if not _new_guest_limiter.hit(ip):
        raise HTTPException(
            status_code=429,
            detail="Слишком много новых гостевых сессий. Попробуйте позже.",
            headers={"Retry-After": str(_new_guest_limiter.retry_after(ip))},
        )
    new_guest_id = f"guest_{uuid.uuid4().hex[:16]}"
    return _build_user(
        user_id=new_guest_id,
        db_id=None,
        username=f"Гость (#{new_guest_id[-5:]})",
        role="guest",
        created_at=None,
        is_new_session=True,
        session_token=create_guest_session_token(new_guest_id),
    )


def current_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency: any visitor (registered user or guest)."""
    return get_current_user(request)


def require_registered(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if user.get("role") == "guest":
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return user


def require_admin(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен. Требуются права администратора.")
    return user
