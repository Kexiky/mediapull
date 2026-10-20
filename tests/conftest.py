import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Everything the app writes goes to a throw-away directory; this must be set before importing `app`.
_TMP = Path(tempfile.mkdtemp(prefix="mediapull-tests-"))
os.environ["MEDIAPULL_DATA_DIR"] = str(_TMP / "data")
os.environ["MEDIAPULL_DOWNLOADS_DIR"] = str(_TMP / "downloads")
os.environ["MEDIAPULL_SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["MEDIAPULL_MIN_FREE_DISK_GB"] = "0"

import pytest  # noqa: E402

from app import auth, database, downloader, main  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_state():
    """Each test starts with a clean database, empty downloads, empty caches and reset rate limits."""
    import shutil

    database.init_db()
    with database.get_db_connection() as conn:
        conn.execute("DELETE FROM users WHERE LOWER(username) != 'kexiky'")
    database.invalidate_user_cache()

    for sub in ("guests", "users", "temp_tg", ".temp"):
        shutil.rmtree(downloader.DOWNLOADS_DIR / sub, ignore_errors=True)
    for f in downloader.DOWNLOADS_DIR.glob("*"):
        if f.is_file():
            f.unlink()
    for f in downloader.THUMBNAILS_DIR.glob("*"):
        f.unlink()
    with downloader._meta_lock:
        downloader._meta_cache.clear()
    downloader.invalidate_usage()
    downloader.downloader_manager.tasks.clear()

    for limiter in (main._login_ip_limiter, main._login_user_limiter, main._register_limiter,
                    main._info_limiter, auth._new_guest_limiter):
        limiter.reset()
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def make_client():
    """Factory for independent clients (separate cookie jars), e.g. two different users."""
    from fastapi.testclient import TestClient
    clients = []

    def _make():
        c = TestClient(main.app)
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def no_dns(monkeypatch):
    """Skip real DNS lookups for example hosts used in API tests."""
    monkeypatch.setattr(main, "assert_public_host", lambda host: None)


@pytest.fixture
def stub_downloads(monkeypatch):
    """Queue tasks without launching yt-dlp / network."""
    async def fake_execute(self, state, **kwargs):
        import asyncio
        await asyncio.sleep(3600)

    monkeypatch.setattr(downloader.DownloaderManager, "_execute_download", fake_execute)


def register(client, name="alice", password="secret123"):
    r = client.post("/api/auth/register", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return r.json()
