import asyncio
import time
from types import SimpleNamespace

import pytest

from app import database, downloader, updater
from app.downloader import DownloaderManager
from conftest import register


# ---------------------------------------------------------------------------
# Telegram account linking (web side)
# ---------------------------------------------------------------------------
def test_link_endpoints_require_a_registered_user(client):
    client.get("/api/auth/me")
    assert client.get("/api/auth/telegram-link").status_code == 401
    assert client.post("/api/auth/telegram-link").status_code == 401
    assert client.delete("/api/auth/telegram-link").status_code == 401


def test_link_code_flow(client):
    register(client, "linky")
    assert client.get("/api/auth/telegram-link").json()["linked"] is False

    code = client.post("/api/auth/telegram-link").json()["code"]
    assert len(code) == 8
    user = database.consume_telegram_link_code(code, 4242)
    assert user["username"] == "linky"
    assert client.get("/api/auth/telegram-link").json() == {"linked": True, "telegram_id": 4242, "bot_username": None}

    assert database.consume_telegram_link_code(code, 4243) is None            # one-time
    assert client.delete("/api/auth/telegram-link").status_code == 200
    assert database.get_telegram_linked_user(4242) is None


def test_codes_are_stored_hashed_and_expire(client):
    rec = database.register_user("hashy", "secret123")
    code = database.create_telegram_link_code(rec["id"])
    with database.get_db_connection() as conn:
        stored = [r["code_hash"] for r in conn.execute("SELECT code_hash FROM telegram_link_codes")]
        assert code not in stored and all(len(h) == 64 for h in stored)
        conn.execute("UPDATE telegram_link_codes SET expires_at = ?", (time.time() - 1,))
    assert database.consume_telegram_link_code(code, 1) is None
    assert database.consume_telegram_link_code("not-a-code", 1) is None


def test_relinking_replaces_the_old_binding_in_both_directions():
    a = database.register_user("usera", "secret123")
    b = database.register_user("userb", "secret123")
    database.consume_telegram_link_code(database.create_telegram_link_code(a["id"]), 100)
    # the same Telegram account moves to another web account
    database.consume_telegram_link_code(database.create_telegram_link_code(b["id"]), 100)
    assert database.get_telegram_linked_user(100)["username"] == "userb"
    assert database.get_user_telegram_id(a["id"]) is None
    # a web account moves to another Telegram account
    database.consume_telegram_link_code(database.create_telegram_link_code(b["id"]), 101)
    assert database.get_telegram_linked_user(100) is None and database.get_telegram_linked_user(101)["username"] == "userb"


def test_deleting_a_user_drops_the_link():
    rec = database.register_user("gone", "secret123")
    database.consume_telegram_link_code(database.create_telegram_link_code(rec["id"]), 55)
    database.delete_user("gone")
    assert database.get_telegram_linked_user(55) is None


def test_link_role_follows_the_web_account():
    rec = database.register_user("roley", "secret123")
    database.consume_telegram_link_code(database.create_telegram_link_code(rec["id"]), 66)
    assert database.get_telegram_linked_user(66)["role"] == "user"
    database.update_user_role("roley", "premium")
    assert database.get_telegram_linked_user(66)["role"] == "premium"         # no stale cache


def test_link_code_requests_are_rate_limited(client):
    register(client, "spammy")
    codes = [client.post("/api/auth/telegram-link").status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and 429 in codes[10:]


# ---------------------------------------------------------------------------
# yt-dlp options from the environment, subtitles mode, updater
# ---------------------------------------------------------------------------
def test_cookies_and_proxy_come_only_from_the_environment(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("YTDLP_COOKIES_FILE", str(cookies))
    monkeypatch.setenv("YTDLP_PROXY", "socks5://127.0.0.1:9050")
    assert downloader.get_ytdlp_global_args() == ["--cookies", str(cookies), "--proxy", "socks5://127.0.0.1:9050"]
    monkeypatch.setenv("YTDLP_COOKIES_FILE", str(tmp_path / "missing.txt"))
    monkeypatch.delenv("YTDLP_PROXY")
    assert downloader.get_ytdlp_global_args() == []


def test_subtitles_mode_is_validated_like_everything_else():
    ok = downloader.normalize_download_options(
        mode="subtitles", quality="best", video_format="mp4", audio_format="mp3", audio_quality="192",
        trim_start=None, trim_end=None, subtitles_lang="ru", role="user")
    assert ok["mode"] == "subtitles" and ok["subtitles_lang"] == "ru"
    with pytest.raises(ValueError):
        downloader.normalize_download_options(
            mode="subtitles", quality="best", video_format="mp4", audio_format="mp3", audio_quality="192",
            trim_start=None, trim_end=None, subtitles_lang="ru --exec x", role="user")


def test_subtitles_download_command_skips_the_media(monkeypatch):
    """The yt-dlp command for a subtitles request must not download video or thumbnails."""
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = SimpleNamespace(readline=lambda: asyncio.sleep(0, result=b""))

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kw):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def scenario():
        mgr = DownloaderManager()
        uid = "guest_" + "9" * 16
        state = await mgr.start_download("subs1", "https://example.com/v", "T", mode="subtitles", subtitles_lang="ru",
                                         user_id=uid, role="guest")
        await asyncio.wait_for(state.async_task, timeout=10)
        return state

    state = asyncio.run(scenario())
    cmd = captured["cmd"]
    assert "--skip-download" in cmd and "--write-subs" in cmd and "--write-auto-subs" in cmd
    assert cmd[cmd.index("--sub-langs") + 1] == "ru" and cmd[cmd.index("--convert-subs") + 1] == "srt"
    assert "--write-thumbnail" not in cmd and "-f" not in cmd
    assert cmd[-2:] == ["--", "https://example.com/v"]
    assert state.status == "error" and "Субтитры" in state.error          # nothing was written -> clear message


def test_updater_reports_version_change(monkeypatch):
    versions = iter(["2024.01.01", "2025.06.30"])

    async def fake_version():
        return next(versions)

    async def fake_run(cmd, timeout):
        assert cmd[1:4] == ["-m", "pip", "install"] and "yt-dlp" in cmd
        return 0, ""

    monkeypatch.setattr(updater, "ytdlp_version", fake_version)
    monkeypatch.setattr(updater, "_run", fake_run)
    updated, message = asyncio.run(updater.update_ytdlp())
    assert updated is True and "2024.01.01" in message and "2025.06.30" in message


def test_updater_reports_pip_failure(monkeypatch):
    async def fake_version():
        return "1"

    async def fake_run(cmd, timeout):
        return 1, "boom"

    monkeypatch.setattr(updater, "ytdlp_version", fake_version)
    monkeypatch.setattr(updater, "_run", fake_run)
    updated, message = asyncio.run(updater.update_ytdlp())
    assert updated is False and "pip failed" in message


def test_auto_update_is_opt_in(monkeypatch):
    monkeypatch.delenv("MEDIAPULL_YTDLP_AUTO_UPDATE", raising=False)
    assert updater.auto_update_enabled() is False
    monkeypatch.setenv("MEDIAPULL_YTDLP_AUTO_UPDATE", "true")
    assert updater.auto_update_enabled() is True


def test_health_endpoint_and_lifespan_start_with_updater_enabled(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    started = []

    async def fake_loop(*a, **kw):
        started.append(True)
        await asyncio.sleep(3600)

    monkeypatch.setenv("MEDIAPULL_YTDLP_AUTO_UPDATE", "true")
    monkeypatch.setattr(main, "ytdlp_update_loop", fake_loop)
    with TestClient(main.app) as c:
        assert c.get("/api/health").status_code == 200
    assert started == [True]
