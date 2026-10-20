"""Persistent state of the Telegram bot (its own SQLite file, so the bot never touches the user database).

Calls are single-statement SQLite operations on a WAL database (well under a millisecond), so they are used
directly from the bot's async handlers.
"""
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from app.database import DATA_DIR

BOT_DB_PATH = DATA_DIR / "bot.db"

CAPTION_MODES = ("none", "title")
# "ask" = show the quality menu, "auto" = best quality that fits Telegram, or a fixed height
QUALITY_PREFS = ("ask", "auto", "360", "480", "720", "1080")
SETTING_KEYS = ("caption_mode", "quality_pref", "save_library")
SHORTLINK_TTL = 3600


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(BOT_DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_store() -> None:
    with _conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS file_cache (
                key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                file_id TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_file_cache_source ON file_cache(source_url);
            CREATE TABLE IF NOT EXISTS settings (
                tg_id INTEGER PRIMARY KEY,
                caption_mode TEXT NOT NULL DEFAULT 'none',
                quality_pref TEXT NOT NULL DEFAULT 'ask',
                save_library INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS acl (
                tg_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                changed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stats (
                day TEXT NOT NULL,
                tg_id INTEGER NOT NULL,
                downloads INTEGER NOT NULL DEFAULT 0,
                cache_hits INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                bytes INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, tg_id)
            );
            CREATE TABLE IF NOT EXISTS shortlinks (
                token TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
        """)


# ---------------------------------------------------------------------------
# Per-user settings
# ---------------------------------------------------------------------------
def get_settings(tg_id: int) -> Dict[str, Any]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM settings WHERE tg_id = ?", (tg_id,)).fetchone()
    if row:
        return {"caption_mode": row["caption_mode"], "quality_pref": row["quality_pref"], "save_library": bool(row["save_library"])}
    return {"caption_mode": "none", "quality_pref": "ask", "save_library": False}


def set_setting(tg_id: int, key: str, value: Any) -> None:
    if key not in SETTING_KEYS:
        raise ValueError(f"unknown setting {key}")
    if key == "caption_mode" and value not in CAPTION_MODES:
        raise ValueError("bad caption mode")
    if key == "quality_pref" and value not in QUALITY_PREFS:
        raise ValueError("bad quality preference")
    if key == "save_library":
        value = 1 if value else 0
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO settings (tg_id) VALUES (?)", (tg_id,))
        conn.execute(f"UPDATE settings SET {key} = ? WHERE tg_id = ?", (value, tg_id))


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
def get_acl_status(tg_id: int) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute("SELECT status FROM acl WHERE tg_id = ?", (tg_id,)).fetchone()
    return row["status"] if row else None


def set_acl(tg_id: int, status: Optional[str]) -> None:
    """status: 'allowed', 'banned' or None (remove the entry)."""
    if status not in ("allowed", "banned", None):
        raise ValueError("bad status")
    with _conn() as conn:
        if status is None:
            conn.execute("DELETE FROM acl WHERE tg_id = ?", (tg_id,))
        else:
            conn.execute(
                "INSERT INTO acl (tg_id, status, changed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(tg_id) DO UPDATE SET status = excluded.status, changed_at = excluded.changed_at",
                (tg_id, status, time.time()),
            )


def list_acl(status: str) -> List[int]:
    with _conn() as conn:
        return [r["tg_id"] for r in conn.execute("SELECT tg_id FROM acl WHERE status = ? ORDER BY changed_at", (status,))]


def get_closed_mode() -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'closed_mode'").fetchone()
    return bool(row and row["value"] == "1")


def set_closed_mode(enabled: bool) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('closed_mode', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if enabled else "0",),
        )


# ---------------------------------------------------------------------------
# Telegram file_id cache
# ---------------------------------------------------------------------------
def cache_get(key: str) -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM file_cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE file_cache SET hits = hits + 1 WHERE key = ?", (key,))
    return dict(row)


def cache_put(key: str, kind: str, file_id: str, size: int, title: str, source_url: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO file_cache (key, kind, file_id, size, title, source_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET file_id = excluded.file_id, size = excluded.size, title = excluded.title, "
            "created_at = excluded.created_at",
            (key, kind, file_id, size, title[:300], source_url[:2048], time.time()),
        )


def cache_delete(key: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM file_cache WHERE key = ?", (key,))


def cache_find_by_source(source_url: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    with _conn() as conn:
        if kind:
            rows = conn.execute(
                "SELECT * FROM file_cache WHERE source_url = ? AND kind = ? ORDER BY size DESC", (source_url, kind)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM file_cache WHERE source_url = ? ORDER BY size DESC", (source_url,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def _today() -> str:
    return time.strftime("%Y-%m-%d")


def record_event(tg_id: int, *, cached: bool = False, ok: bool = True, size: int = 0) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO stats (day, tg_id) VALUES (?, ?)", (_today(), tg_id))
        if not ok:
            conn.execute("UPDATE stats SET failures = failures + 1 WHERE day = ? AND tg_id = ?", (_today(), tg_id))
        elif cached:
            conn.execute("UPDATE stats SET cache_hits = cache_hits + 1 WHERE day = ? AND tg_id = ?", (_today(), tg_id))
        else:
            conn.execute(
                "UPDATE stats SET downloads = downloads + 1, bytes = bytes + ? WHERE day = ? AND tg_id = ?",
                (size, _today(), tg_id),
            )


def stats_summary(top: int = 5) -> Dict[str, Any]:
    with _conn() as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(downloads),0) d, COALESCE(SUM(cache_hits),0) c, COALESCE(SUM(failures),0) f, "
            "COALESCE(SUM(bytes),0) b, COUNT(DISTINCT tg_id) u FROM stats"
        ).fetchone()
        today = conn.execute(
            "SELECT COALESCE(SUM(downloads),0) d, COALESCE(SUM(cache_hits),0) c, COUNT(DISTINCT tg_id) u FROM stats WHERE day = ?",
            (_today(),),
        ).fetchone()
        leaders = conn.execute(
            "SELECT tg_id, SUM(downloads + cache_hits) n FROM stats GROUP BY tg_id ORDER BY n DESC LIMIT ?", (top,)
        ).fetchall()
        cache_rows = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM file_cache").fetchone()
    return {
        "downloads_total": total["d"], "cache_hits_total": total["c"], "failures_total": total["f"],
        "bytes_total": total["b"], "users_total": total["u"],
        "downloads_today": today["d"], "cache_hits_today": today["c"], "users_today": today["u"],
        "leaders": [(r["tg_id"], r["n"]) for r in leaders],
        "cache_entries": cache_rows["n"], "cache_bytes": cache_rows["s"],
    }


# ---------------------------------------------------------------------------
# Deep-link tokens (Telegram start parameters are limited to 64 chars of [A-Za-z0-9_-])
# ---------------------------------------------------------------------------
def create_shortlink(url: str) -> str:
    token = secrets.token_urlsafe(9)
    now = time.time()
    with _conn() as conn:
        conn.execute("DELETE FROM shortlinks WHERE expires_at < ?", (now,))
        conn.execute("INSERT INTO shortlinks (token, url, expires_at) VALUES (?, ?, ?)", (token, url, now + SHORTLINK_TTL))
    return token


def resolve_shortlink(token: str) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute("SELECT url FROM shortlinks WHERE token = ? AND expires_at >= ?", (token, time.time())).fetchone()
    return row["url"] if row else None
