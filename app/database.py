import os
import sqlite3
import hashlib
import secrets
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mediapull.db"

# Role configurations: TTL in seconds (None = permanent), Storage Quota in bytes (None = unlimited)
ROLE_CONFIGS = {
    "admin": {
        "title": "Создатель / Админ",
        "icon": "👑",
        "badge_class": "admin",
        "ttl_seconds": None,
        "ttl_days": None,
        "ttl_display": "♾️ Вечно (Без TTL)",
        "storage_limit_bytes": None,  # Unlimited
        "storage_limit_display": "Без ограничений",
        "max_quality": "8k",
        "priority": "highest",
    },
    "premium": {
        "title": "Премиум",
        "icon": "⭐",
        "badge_class": "premium",
        "ttl_seconds": 30 * 86400,
        "ttl_days": 30,
        "ttl_display": "⏳ 30 дней",
        "storage_limit_bytes": 50 * 1024 * 1024 * 1024,  # 50 GB
        "storage_limit_display": "50 ГБ",
        "max_quality": "4k",
        "priority": "high",
    },
    "user": {
        "title": "Пользователь",
        "icon": "👤",
        "badge_class": "user",
        "ttl_seconds": 7 * 86400,
        "ttl_days": 7,
        "ttl_display": "⏳ 7 дней",
        "storage_limit_bytes": 10 * 1024 * 1024 * 1024,  # 10 GB
        "storage_limit_display": "10 ГБ",
        "max_quality": "1080p",
        "priority": "normal",
    },
    "guest": {
        "title": "Гость",
        "icon": "⏳",
        "badge_class": "guest",
        "ttl_seconds": 86400,
        "ttl_days": 1,
        "ttl_display": "⏳ 24 часа",
        "storage_limit_bytes": 2 * 1024 * 1024 * 1024,  # 2 GB
        "storage_limit_display": "2 ГБ",
        "max_quality": "1080p",
        "priority": "low",
    },
}


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def hash_password(password: str, salt_hex: Optional[str] = None) -> tuple[str, str]:
    """Generate PBKDF2-HMAC-SHA256 hash with salt."""
    if not salt_hex:
        salt = secrets.token_bytes(16)
        salt_hex = salt.hex()
    else:
        salt = bytes.fromhex(salt_hex)

    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return derived.hex(), salt_hex


def verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    derived, _ = hash_password(password, salt_hex)
    return secrets.compare_digest(derived, expected_hash_hex)


def init_db():
    """Create database tables and seed default creator account 'kexiky'."""
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at REAL NOT NULL,
                last_login_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                token TEXT,
                linked_at REAL NOT NULL
            )
        """)
        conn.commit()

        # Seed creator kexiky if not exists
        cur = conn.cursor()
        cur.execute("SELECT id, role FROM users WHERE username = 'kexiky'")
        row = cur.fetchone()
        if not row:
            master_pass = os.getenv("KEXIKY_PASSWORD")
            if not master_pass or master_pass.strip() == "kexiky":
                pass_file = DATA_DIR / ".creator_password"
                if pass_file.exists():
                    try:
                        master_pass = pass_file.read_text(encoding="utf-8").strip()
                    except Exception:
                        master_pass = ""
                if not master_pass:
                    master_pass = secrets.token_urlsafe(12)
                    try:
                        pass_file.write_text(master_pass, encoding="utf-8")
                    except Exception:
                        pass
                print("=" * 64)
                print(f"[Security] Admin 'kexiky' initialized.")
                print(f"[Security] Generated Password: {master_pass}")
                print(f"[Security] Saved to data/.creator_password (set KEXIKY_PASSWORD in .env to override)")
                print("=" * 64)
            else:
                master_pass = master_pass.strip()

            pw_hash, salt = hash_password(master_pass)
            cur.execute("""
                INSERT INTO users (username, password_hash, salt, role, created_at)
                VALUES ('kexiky', ?, ?, 'admin', ?)
            """, (pw_hash, salt, time.time()))
            conn.commit()
            print("[Database] Seeded creator account 'kexiky' with role 'admin'")
        else:
            # Ensure kexiky always retains admin role
            if row["role"] != "admin":
                cur.execute("UPDATE users SET role = 'admin' WHERE username = 'kexiky'")
                conn.commit()


def register_user(username: str, password: str) -> Dict[str, Any]:
    """Register a new user with 'user' role."""
    clean_user = username.strip()
    clean_pass = password.strip()

    if len(clean_user) < 3 or len(clean_user) > 32:
        raise ValueError("Имя пользователя должно содержать от 3 до 32 символов")
    if not clean_user.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Имя пользователя может содержать только латинские буквы, цифры, дефис и подчеркивание")
    if len(clean_pass) < 4:
        raise ValueError("Пароль должен содержать не менее 4 символов")

    pw_hash, salt = hash_password(clean_pass)
    now = time.time()

    with get_db_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO users (username, password_hash, salt, role, created_at, last_login_at)
                VALUES (?, ?, ?, 'user', ?, ?)
            """, (clean_user, pw_hash, salt, now, now))
            conn.commit()
            user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"Пользователь с именем '{clean_user}' уже существует")

    return get_user_by_id(user_id)


def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Validate username and password, update last_login_at."""
    clean_user = username.strip()
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (clean_user,))
        row = cur.fetchone()
        if not row:
            return None

        if not verify_password(password, row["salt"], row["password_hash"]):
            return None

        now = time.time()
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"]))
        conn.commit()

        user_dict = dict(row)
        user_dict["last_login_at"] = now
        del user_dict["password_hash"]
        del user_dict["salt"]
        return user_dict


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, role, created_at, last_login_at FROM users WHERE LOWER(username) = LOWER(?)", (username.strip(),))
        row = cur.fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, role, created_at, last_login_at FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_user_password(username: str, old_password: str, new_password: str) -> bool:
    if len(new_password.strip()) < 4:
        raise ValueError("Новый пароль должен содержать не менее 4 символов")

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username.strip(),))
        row = cur.fetchone()
        if not row:
            raise ValueError("Пользователь не найден")

        if not verify_password(old_password, row["salt"], row["password_hash"]):
            raise ValueError("Неверный текущий пароль")

        new_hash, new_salt = hash_password(new_password.strip())
        conn.execute("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?", (new_hash, new_salt, row["id"]))
        conn.commit()
        return True


def update_user_role(username: str, new_role: str) -> bool:
    if new_role not in ROLE_CONFIGS or new_role == "guest":
        raise ValueError(f"Недопустимая роль '{new_role}'. Доступны: admin, premium, user")

    clean_user = username.strip()
    if clean_user == "kexiky" and new_role != "admin":
        raise ValueError("Роль Создателя kexiky не может быть понижена")

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (clean_user,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Пользователь '{clean_user}' не найден")

        conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, row["id"]))
        conn.commit()
        return True


def list_users() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, role, created_at, last_login_at FROM users ORDER BY id ASC")
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def delete_user(username: str) -> bool:
    clean_user = username.strip()
    if clean_user == "kexiky":
        raise ValueError("Аккаунт Создателя kexiky не может быть удален")

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE LOWER(username) = LOWER(?)", (clean_user,))
        conn.commit()
        return cur.rowcount > 0


def link_telegram_user(telegram_id: int, username: str, role: str, token: Optional[str] = None) -> Dict[str, Any]:
    now = time.time()
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO telegram_users (telegram_id, username, role, token, linked_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                role = excluded.role,
                token = excluded.token,
                linked_at = excluded.linked_at
        """, (telegram_id, username, role, token, now))
        conn.commit()
    return get_telegram_user(telegram_id)


def get_telegram_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT telegram_id, username, role, token, linked_at FROM telegram_users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def unlink_telegram_user(telegram_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM telegram_users WHERE telegram_id = ?", (telegram_id,))
        conn.commit()
        return cur.rowcount > 0


# Initialize database automatically on module import
init_db()
