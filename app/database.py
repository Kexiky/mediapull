import os
import re
import sqlite3
import hashlib
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Dict, Any, List, Iterator

from app.roles import ROLE_CONFIGS, CREATOR_USERNAME  # noqa: F401  (re-exported for backward compatibility)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("MEDIAPULL_DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mediapull.db"

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
RESERVED_USERNAME_PREFIXES = ("guest_", "tg_")
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 128
PBKDF2_ITERATIONS = 100_000

_USER_COLUMNS = "id, username, role, created_at, last_login_at, tokens_valid_after"
_USER_CACHE_TTL = 10.0
_user_cache: Dict[int, tuple] = {}
_user_cache_lock = threading.Lock()


@contextmanager
def get_db_connection() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on error and ALWAYS close it.

    (`with sqlite3.connect(...)` only manages the transaction and leaks the handle.)
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def invalidate_user_cache(user_id: Optional[int] = None) -> None:
    with _user_cache_lock:
        if user_id is None:
            _user_cache.clear()
        else:
            _user_cache.pop(user_id, None)


def hash_password(password: str, salt_hex: Optional[str] = None) -> tuple[str, str]:
    """Generate PBKDF2-HMAC-SHA256 hash with salt."""
    if not salt_hex:
        salt = secrets.token_bytes(16)
        salt_hex = salt.hex()
    else:
        salt = bytes.fromhex(salt_hex)

    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return derived.hex(), salt_hex


def verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    derived, _ = hash_password(password, salt_hex)
    return secrets.compare_digest(derived, expected_hash_hex)


# Used to keep login timing flat for unknown usernames (avoids user enumeration).
_DUMMY_HASH, _DUMMY_SALT = hash_password(secrets.token_urlsafe(16))


def _validate_password(password: str, field: str = "Пароль") -> str:
    clean = (password or "").strip()
    if len(clean) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"{field} должен содержать не менее {MIN_PASSWORD_LENGTH} символов")
    if len(clean) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"{field} не должен быть длиннее {MAX_PASSWORD_LENGTH} символов")
    return clean


def init_schema() -> None:
    """Create tables and migrate older schemas. Side-effect free apart from the schema (no seeding),
    so the Telegram bot can call it without creating accounts."""
    with get_db_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at REAL NOT NULL,
                last_login_at REAL,
                tokens_valid_after REAL NOT NULL DEFAULT 0
            )
        """)

        columns = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "tokens_valid_after" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN tokens_valid_after REAL NOT NULL DEFAULT 0")

        # `username TEXT UNIQUE` is case-sensitive, while every lookup is case-insensitive.
        # Enforce case-insensitive uniqueness so "KEXIKY" can never alias "kexiky".
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase ON users(LOWER(username))")
        except sqlite3.IntegrityError:
            dupes = conn.execute(
                "SELECT GROUP_CONCAT(username, ', ') AS names FROM users GROUP BY LOWER(username) HAVING COUNT(*) > 1"
            ).fetchall()
            groups = "; ".join(r["names"] for r in dupes)
            print("[Security] WARNING: usernames that differ only by case exist in the database "
                  f"({groups}). Tokens are bound to the exact account, so they cannot alias each other, "
                  "but delete the unwanted accounts in the admin panel to enforce case-insensitive uniqueness.")

        # Telegram <-> web account links, and short-lived one-time codes used to create a link.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_links (
                telegram_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                linked_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_link_codes (
                code_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at REAL NOT NULL
            )
        """)


def init_db() -> None:
    """Create the schema and seed the default creator account."""
    init_schema()
    with get_db_connection() as conn:
        row = conn.execute("SELECT id, role FROM users WHERE LOWER(username) = LOWER(?)", (CREATOR_USERNAME,)).fetchone()
        if not row:
            generated_pass = secrets.token_urlsafe(12)
            pw_hash, salt = hash_password(generated_pass)
            conn.execute("""
                INSERT INTO users (username, password_hash, salt, role, created_at)
                VALUES (?, ?, ?, 'admin', ?)
            """, (CREATOR_USERNAME, pw_hash, salt, time.time()))
            print("=" * 64)
            print("[Security] Initial admin ('kexiky') created in database.")
            print(f"[Security] Generated Password: {generated_pass}")
            print("[Security] Run 'python run.py --reset-admin' to change it.")
            print("=" * 64)
        elif row["role"] != "admin":
            conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],))
    invalidate_user_cache()


def reset_admin_password(new_password: str) -> None:
    """Safely create or reset admin ('kexiky') password in database without plaintext storage."""
    clean_pass = new_password.strip()
    if len(clean_pass) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Пароль администратора должен содержать не менее {MIN_PASSWORD_LENGTH} символов")
    if len(clean_pass) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Пароль не должен быть длиннее {MAX_PASSWORD_LENGTH} символов")

    init_db()
    pw_hash, salt = hash_password(clean_pass)
    now = time.time()
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ?, role = 'admin', tokens_valid_after = ? "
            "WHERE LOWER(username) = LOWER(?)",
            (pw_hash, salt, now, CREATOR_USERNAME),
        )
    invalidate_user_cache()


def validate_username(username: str) -> str:
    clean_user = (username or "").strip()
    if not USERNAME_RE.match(clean_user):
        raise ValueError("Имя пользователя: 3–32 символа, только латинские буквы, цифры, дефис и подчеркивание")
    lowered = clean_user.lower()
    if lowered.startswith(RESERVED_USERNAME_PREFIXES) or lowered == CREATOR_USERNAME:
        raise ValueError("Это имя пользователя зарезервировано")
    return clean_user


def register_user(username: str, password: str) -> Dict[str, Any]:
    """Register a new user with 'user' role."""
    clean_user = validate_username(username)
    clean_pass = _validate_password(password)

    pw_hash, salt = hash_password(clean_pass)
    now = time.time()

    with get_db_connection() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE LOWER(username) = LOWER(?)", (clean_user,)).fetchone()
        if exists:
            raise ValueError(f"Пользователь с именем '{clean_user}' уже существует")
        try:
            cur = conn.execute("""
                INSERT INTO users (username, password_hash, salt, role, created_at, last_login_at)
                VALUES (?, ?, ?, 'user', ?, ?)
            """, (clean_user, pw_hash, salt, now, now))
        except sqlite3.IntegrityError:
            raise ValueError(f"Пользователь с именем '{clean_user}' уже существует")
        user_id = cur.lastrowid

    return get_user_by_id(user_id)


def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Validate username and password, update last_login_at."""
    clean_user = (username or "").strip()
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (clean_user,)).fetchone()
        if not row:
            verify_password(password, _DUMMY_SALT, _DUMMY_HASH)
            return None

        if not verify_password(password, row["salt"], row["password_hash"]):
            return None

        now = time.time()
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"]))

        user_dict = dict(row)
        user_dict["last_login_at"] = now
        del user_dict["password_hash"]
        del user_dict["salt"]
    invalidate_user_cache(user_dict["id"])
    return user_dict


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        row = conn.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE LOWER(username) = LOWER(?)", ((username or "").strip(),)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    with _user_cache_lock:
        hit = _user_cache.get(user_id)
        if hit and hit[0] > now:
            return dict(hit[1]) if hit[1] else None

    with get_db_connection() as conn:
        row = conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()
    record = dict(row) if row else None

    with _user_cache_lock:
        if len(_user_cache) > 1024:
            _user_cache.clear()
        _user_cache[user_id] = (now + _USER_CACHE_TTL, record)
    return dict(record) if record else None


def update_user_password(username: str, old_password: str, new_password: str) -> bool:
    clean_new = _validate_password(new_password, "Новый пароль")

    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", ((username or "").strip(),)).fetchone()
        if not row:
            raise ValueError("Пользователь не найден")

        if not verify_password(old_password, row["salt"], row["password_hash"]):
            raise ValueError("Неверный текущий пароль")

        new_hash, new_salt = hash_password(clean_new)
        # Revoke every token issued before this moment.
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ?, tokens_valid_after = ? WHERE id = ?",
            (new_hash, new_salt, time.time(), row["id"]),
        )
    invalidate_user_cache(row["id"])
    return True


def update_user_role(username: str, new_role: str) -> bool:
    if new_role not in ROLE_CONFIGS or new_role == "guest":
        raise ValueError(f"Недопустимая роль '{new_role}'. Доступны: admin, premium, user")

    clean_user = (username or "").strip()
    if clean_user == CREATOR_USERNAME and new_role != "admin":
        raise ValueError("Роль Создателя kexiky не может быть понижена")

    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, username FROM users WHERE LOWER(username) = LOWER(?) ORDER BY username = ? DESC",
            (clean_user, clean_user),
        ).fetchone()
        if row and row["username"] == CREATOR_USERNAME and new_role != "admin":
            raise ValueError("Роль Создателя kexiky не может быть понижена")
        if not row:
            raise ValueError(f"Пользователь '{clean_user}' не найден")
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, row["id"]))
    invalidate_user_cache(row["id"])
    return True


def list_users() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, username, role, created_at, last_login_at FROM users ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_user(username: str) -> bool:
    clean_user = (username or "").strip()
    if clean_user == CREATOR_USERNAME:
        raise ValueError("Аккаунт Создателя kexiky не может быть удален")

    with get_db_connection() as conn:
        # Exact match first, so a legacy look-alike such as "KEXIKY" can be removed without touching the creator.
        cur = conn.execute("DELETE FROM users WHERE username = ?", (clean_user,))
        if cur.rowcount == 0:
            cur = conn.execute(
                "DELETE FROM users WHERE LOWER(username) = LOWER(?) AND username != ?", (clean_user, CREATOR_USERNAME)
            )
        deleted = cur.rowcount > 0
    invalidate_user_cache()
    return deleted


# ---------------------------------------------------------------------------
# Telegram account linking
# ---------------------------------------------------------------------------
LINK_CODE_TTL_SECONDS = 600


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def create_telegram_link_code(user_id: int) -> str:
    """One-time code (valid 10 minutes) that the user sends to the bot as `/link CODE`."""
    code = secrets.token_hex(4).upper()
    now = time.time()
    with get_db_connection() as conn:
        conn.execute("DELETE FROM telegram_link_codes WHERE expires_at < ? OR user_id = ?", (now, user_id))
        conn.execute(
            "INSERT INTO telegram_link_codes (code_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (_hash_code(code), user_id, now + LINK_CODE_TTL_SECONDS),
        )
    return code


def consume_telegram_link_code(code: str, telegram_id: int) -> Optional[Dict[str, Any]]:
    """Link `telegram_id` to the account that issued `code`. Returns that user, or None for a bad/expired code."""
    now = time.time()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM telegram_link_codes WHERE code_hash = ? AND expires_at >= ?", (_hash_code(code), now)
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM telegram_link_codes WHERE code_hash = ?", (_hash_code(code),))
        # one Telegram account <-> one web account, in both directions
        conn.execute("DELETE FROM telegram_links WHERE telegram_id = ? OR user_id = ?", (telegram_id, row["user_id"]))
        conn.execute(
            "INSERT INTO telegram_links (telegram_id, user_id, linked_at) VALUES (?, ?, ?)",
            (telegram_id, row["user_id"], now),
        )
        user_id = row["user_id"]
    return get_user_by_id(user_id)


def get_telegram_linked_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    """Web account linked to a Telegram id (fresh read, no cache: role changes apply immediately)."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT u.id, u.username, u.role, u.created_at, u.last_login_at, u.tokens_valid_after "
            "FROM telegram_links l JOIN users u ON u.id = l.user_id WHERE l.telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return dict(row) if row else None


def get_user_telegram_id(user_id: int) -> Optional[int]:
    with get_db_connection() as conn:
        row = conn.execute("SELECT telegram_id FROM telegram_links WHERE user_id = ?", (user_id,)).fetchone()
        return row["telegram_id"] if row else None


def unlink_telegram(*, telegram_id: Optional[int] = None, user_id: Optional[int] = None) -> bool:
    with get_db_connection() as conn:
        if telegram_id is not None:
            cur = conn.execute("DELETE FROM telegram_links WHERE telegram_id = ?", (telegram_id,))
        else:
            cur = conn.execute("DELETE FROM telegram_links WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0
