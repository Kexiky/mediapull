import asyncio
import json
import os
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import cleanup, database, downloader
from app.downloader import DOWNLOADS_DIR, DownloaderManager, get_user_root


def _age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def test_expired_guest_files_are_removed_and_fresh_ones_kept():
    root = get_user_root("guest_" + "a" * 16)
    old, new = root / "old.mp4", root / "new.mp4"
    old.write_bytes(b"x" * 10)
    new.write_bytes(b"x" * 10)
    _age(old, 3 * 86400)
    res = cleanup.cleanup_expired_files()
    assert res["deleted_files"] == 1
    assert not old.exists() and new.exists()


def test_empty_playlist_folder_with_hidden_helpers_is_removed():
    root = get_user_root("guest_" + "b" * 16)
    pl = root / "Playlist"
    pl.mkdir()
    (pl / "1.mp4").write_bytes(b"x")
    (pl / ".cover.jpg").write_bytes(b"x" * 600)
    (pl / ".playlist_meta.json").write_text("{}")
    _age(pl / "1.mp4", 3 * 86400)
    _age(pl, 3600)
    cleanup.cleanup_expired_files()
    assert not pl.exists()


def test_fresh_playlist_folder_being_created_survives():
    root = get_user_root("guest_" + "c" * 16)
    pl = root / "Brand New"
    pl.mkdir()
    (pl / ".playlist_meta.json").write_text("{}")
    cleanup.cleanup_expired_files()
    assert pl.exists()


def test_admin_files_are_permanent_and_user_ttl_follows_role():
    database.register_user("boss", "secret123")
    database.update_user_role("boss", "admin")
    database.register_user("usr", "secret123")
    for name in ("boss", "usr"):
        f = get_user_root(name) / "v.mp4"
        f.write_bytes(b"x")
        _age(f, 10 * 86400)  # older than the 7 day user TTL
    cleanup.cleanup_expired_files()
    assert (get_user_root("boss") / "v.mp4").exists()
    assert not (get_user_root("usr") / "v.mp4").exists()


def test_cleanup_removes_stale_scratch_zip_and_telegram_jobs_and_cache_entries():
    temp = DOWNLOADS_DIR / ".temp"
    temp.mkdir(exist_ok=True)
    stale_zip, fresh_zip = temp / "old.zip", temp / "new.zip"
    stale_zip.write_bytes(b"x")
    fresh_zip.write_bytes(b"x")
    _age(stale_zip, 7200)
    tg = DOWNLOADS_DIR / "temp_tg" / "job1"
    tg.mkdir(parents=True)
    (tg / "x.mp4").write_bytes(b"x")
    _age(tg, 7200)

    with downloader._meta_lock:
        downloader._meta_cache["ghost/file.mp4"] = {"mtime": 1}
    cleanup.cleanup_expired_files()

    assert not stale_zip.exists() and fresh_zip.exists()
    assert not tg.exists()
    assert "ghost/file.mp4" not in downloader._meta_cache


def test_thumbnail_is_removed_with_its_expired_file():
    root = get_user_root("guest_" + "d" * 16)
    media = root / "v.mp4"
    media.write_bytes(b"x")
    thumb = downloader.THUMBNAILS_DIR / downloader.thumbnail_cache_name(root.name, "v.mp4")
    thumb.write_bytes(b"t" * 2000)
    _age(media, 3 * 86400)
    cleanup.cleanup_expired_files()
    assert not thumb.exists()


def test_delete_file_removes_matching_thumbnail_and_cache():
    mgr = downloader.downloader_manager
    uid = "guest_" + "e" * 16
    root = get_user_root(uid)
    (root / "v.mp4").write_bytes(b"x" * 10)
    meta = downloader.get_file_metadata(root / "v.mp4", base_dir=root)
    thumb = downloader.THUMBNAILS_DIR / downloader.thumbnail_cache_name(uid, "v.mp4")
    thumb.write_bytes(b"t" * 2000)
    assert mgr.delete_file("v.mp4", user_id=uid) is True
    assert not thumb.exists()
    assert downloader._cache_key(root / "v.mp4") not in downloader._meta_cache
    assert meta["filename"] == "v.mp4"


def test_meta_cache_is_saved_atomically():
    root = get_user_root("guest_" + "f" * 16)
    for i in range(5):
        (root / f"{i}.mp4").write_bytes(b"x")
        downloader.get_file_metadata(root / f"{i}.mp4", base_dir=root)
    downloader.flush_meta_cache()
    data = json.loads(downloader.META_CACHE_FILE.read_text(encoding="utf-8"))
    assert len([k for k in data if k.endswith(".mp4")]) == 5
    assert not downloader.META_CACHE_FILE.with_suffix(".json.tmp").exists()


# ---------------------------------------------------------------------------
# Task bookkeeping
# ---------------------------------------------------------------------------
def test_task_logs_are_bounded_and_prune_uses_finish_time():
    st = downloader.TaskState("t1", "https://example.com/x", user_id="u")
    for i in range(1000):
        st.logs.append(str(i))
    assert len(st.logs) == 200 and st.to_dict()["logs"][-1] == "999"

    mgr = DownloaderManager()
    mgr.tasks["t1"] = st
    st.created_at = time.time() - 10 * 3600  # started long ago ...
    st.status = "completed"                  # ... but finished just now
    mgr.prune_old_tasks()
    assert "t1" in mgr.tasks
    st.finished_at = time.time() - 10 * 3600
    mgr.prune_old_tasks()
    assert "t1" not in mgr.tasks


def test_per_user_active_task_limit():
    async def scenario():
        mgr = DownloaderManager()

        async def never(state, **kw):
            await asyncio.sleep(3600)

        mgr._execute_download = never
        uid = "guest_" + "5" * 16
        for i in range(10):
            await mgr.start_download(f"t{i}", f"https://x.example/{i}", "t", user_id=uid, role="guest")
        with pytest.raises(downloader.DownloadLimitError):
            await mgr.start_download("t11", "https://x.example/11", "t", user_id=uid, role="guest")
        await mgr.shutdown()

    asyncio.run(scenario())


def test_download_rejects_option_like_url_and_bad_options():
    async def scenario():
        mgr = DownloaderManager()
        for bad in ({"url": "--exec=calc"}, {"url": "https://x.example/a", "quality": "../../etc"},
                    {"url": "https://x.example/a", "video_format": "mp4;rm"}):
            kwargs = dict(task_id="x", title="t", user_id="guest_" + "4" * 16, role="guest")
            kwargs.update(bad)
            with pytest.raises(ValueError):
                await mgr.start_download(**kwargs)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# End-to-end with a real yt-dlp / ffmpeg against a local HTTP server
# ---------------------------------------------------------------------------
pytest.importorskip("yt_dlp")


@pytest.fixture
def media_server(tmp_path):
    video = tmp_path / "srv" / "clip.mp4"
    video.parent.mkdir()
    res = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(video)],
        capture_output=True, timeout=120,
    )
    if res.returncode != 0:
        pytest.skip("ffmpeg cannot generate the sample clip")
    handler = partial(SimpleHTTPRequestHandler, directory=str(video.parent))
    handler.log_message = lambda *a, **k: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/clip.mp4"
    server.shutdown()


def test_real_download_completes_and_is_indexed(media_server):
    async def scenario():
        mgr = DownloaderManager()
        uid = "guest_" + "1" * 16
        state = await mgr.start_download("job1", media_server, "Clip", mode="video", quality="best",
                                         embed_thumbnail=False, user_id=uid, role="guest")
        await asyncio.wait_for(state.async_task, timeout=180)
        return state, uid

    state, uid = asyncio.run(scenario())
    assert state.status == "completed", (state.error, list(state.logs)[-5:])
    root = get_user_root(uid)
    assert state.filename and (root / state.filename).exists()
    assert state.progress == 100.0
    assert downloader._cache_key(root / state.filename) in downloader._meta_cache
    assert "\\" not in state.filename


def test_quota_is_enforced_when_the_download_starts(media_server, monkeypatch):
    monkeypatch.setitem(downloader.ROLE_CONFIGS["guest"], "storage_limit_bytes", 1000)

    async def scenario():
        mgr = DownloaderManager()
        uid = "guest_" + "2" * 16
        (get_user_root(uid) / "big.bin").write_bytes(b"x" * 5000)
        downloader.invalidate_usage()
        state = await mgr.start_download("job2", media_server, "Clip", embed_thumbnail=False, user_id=uid, role="guest")
        await asyncio.wait_for(state.async_task, timeout=60)
        return state

    state = asyncio.run(scenario())
    assert state.status == "error" and "квот" in state.error.lower()


def test_cancel_stops_the_process_and_leaves_no_partials(media_server):
    async def scenario():
        mgr = DownloaderManager()
        uid = "guest_" + "3" * 16
        state = await mgr.start_download("job3", media_server, "Clip", embed_thumbnail=False, user_id=uid, role="guest")
        for _ in range(200):
            if state.process is not None:
                break
            await asyncio.sleep(0.02)
        mgr.cancel_task("job3")
        await asyncio.wait_for(state.async_task, timeout=30)
        return state, uid

    state, uid = asyncio.run(scenario())
    assert state.status == "cancelled"
    assert state.process is None or state.process.returncode is not None
    assert [p.name for p in get_user_root(uid).glob("*") if p.suffix in (".part", ".ytdl")] == []
