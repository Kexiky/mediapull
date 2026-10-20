import io
import zipfile

import pytest
from fastapi import HTTPException

from app import database, downloader, main
from app.downloader import (
    DOWNLOADS_DIR, normalize_download_options, resolve_download_path, safe_fetch_bytes, validate_media_url,
    get_user_root,
)
from conftest import register


# ---------------------------------------------------------------------------
# URL / SSRF
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "--exec=calc.exe", "-o /etc/passwd", "file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)",
    "http://", "https://exa mple.com", "http://example.com/\nX", "", "   ", "a" * 3000,
])
def test_validate_media_url_rejects_bad_input(url):
    with pytest.raises(ValueError):
        validate_media_url(url)


def test_validate_media_url_accepts_http_and_https():
    assert validate_media_url(" https://youtu.be/abc ") == "https://youtu.be/abc"
    assert validate_media_url("http://example.com/a?b=1")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "file:///C:/Windows/win.ini", "ftp://x/y", "http://127.0.0.1:8000/api/health",
    "http://localhost/x", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/a", "http://[::1]/x",
    "http://192.168.1.1/",
])
def test_safe_fetch_never_reads_local_or_private_targets(url):
    with pytest.raises(ValueError):
        safe_fetch_bytes(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "--exec=touch /tmp/x", "http://127.0.0.1/admin", "http://169.254.169.254/",
])
def test_download_api_rejects_dangerous_urls(client, url):
    r = client.post("/api/download", json={"url": url})
    assert r.status_code == 400, r.text
    r = client.post("/api/info", json={"url": url})
    assert r.status_code == 400, r.text


def test_photo_url_with_file_scheme_is_rejected(client, no_dns, stub_downloads):
    r = client.post("/api/download", json={"url": "https://example.com/p", "mode": "photo", "photo_url": "file:///etc/passwd"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Option whitelists
# ---------------------------------------------------------------------------
def opts(**kw):
    base = dict(mode="video", quality="best", video_format="mp4", audio_format="mp3", audio_quality="320",
                trim_start=None, trim_end=None, subtitles_lang="en", role="admin")
    base.update(kw)
    return normalize_download_options(**base)


@pytest.mark.parametrize("field,value", [
    ("quality", "../../../etc/cron.d/x"), ("quality", "720p]/../../x"), ("quality", "99999"), ("quality", "10"),
    ("video_format", "mp4;rm"), ("audio_format", "../x"), ("audio_quality", "%(id)s"), ("mode", "rm"),
    ("trim_start", "00:00:00 --exec"), ("subtitles_lang", "en --exec x"),
])
def test_normalize_rejects_free_form_values(field, value):
    with pytest.raises(ValueError):
        opts(**{field: value})


def test_normalize_accepts_normal_values():
    o = opts(quality="1080p", trim_start="00:01:30", trim_end="95.5", subtitles_lang="en,ru")
    assert o["quality"] == "1080" and o["trim_start"] == "00:01:30" and o["subtitles_lang"] == "en,ru"
    assert opts(mode="single")["mode"] == "video"


def test_role_resolution_cap_is_enforced():
    assert opts(role="guest", quality="best")["quality"] == "1080"
    assert opts(role="guest", quality="2160")["quality"] == "1080"
    assert opts(role="user", quality="1440")["quality"] == "1080"
    assert opts(role="premium", quality="4320")["quality"] == "2160"
    assert opts(role="admin", quality="best")["quality"] == "best"


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------
def test_resolve_download_path_blocks_traversal_and_hidden():
    (DOWNLOADS_DIR / "users" / "alice").mkdir(parents=True)
    (DOWNLOADS_DIR / "users" / "alice" / "a.mp4").write_bytes(b"x")
    (DOWNLOADS_DIR / "secret.mp4").write_bytes(b"x")
    (DOWNLOADS_DIR / ".thumbnails").mkdir(exist_ok=True)
    (DOWNLOADS_DIR / ".thumbnails" / "meta_cache.json").write_text("{}")

    assert resolve_download_path("a.mp4", "alice") is not None
    alice_root = str(get_user_root("alice").resolve())
    for evil in ["../../secret.mp4", "..%2F..%2Fsecret.mp4", "%2e%2e/%2e%2e/secret.mp4",
                 "a.mp4\x00.jpg", "/etc/passwd", "..\\..\\secret.mp4"]:
        res = resolve_download_path(evil, "alice")
        assert res is None or str(res).startswith(alice_root), evil
        assert res is None or not res.exists(), evil  # a literal in-root name that does not exist is harmless

    # hidden/internal entries are never reachable, even for the creator
    assert resolve_download_path(".thumbnails/meta_cache.json", "kexiky") is None
    assert resolve_download_path("temp_tg", "kexiky") is None
    assert resolve_download_path("secret.mp4", "kexiky") is not None


def test_users_are_isolated_from_each_other():
    (DOWNLOADS_DIR / "users" / "alice").mkdir(parents=True)
    (DOWNLOADS_DIR / "users" / "alice" / "a.mp4").write_bytes(b"x")
    for attempt in ("../alice/a.mp4", "a.mp4"):
        res = resolve_download_path(attempt, "bob")
        assert res is None or not res.exists()


def test_non_creator_admin_gets_own_folder_and_can_read_it_back():
    root = get_user_root("boss")
    assert root != DOWNLOADS_DIR
    (root / "v.mp4").write_bytes(b"data")
    assert resolve_download_path("v.mp4", "boss").exists()
    hist = downloader.downloader_manager.list_history(user_id="boss", role="admin")
    assert [f["filename"] for f in hist["files"]] == ["v.mp4"]
    assert hist["files"][0]["is_in_playlist"] is False


def test_usage_ignores_other_owners_folders():
    (DOWNLOADS_DIR / "mine.mp4").write_bytes(b"x" * 100)
    (DOWNLOADS_DIR / "users" / "alice").mkdir(parents=True)
    (DOWNLOADS_DIR / "users" / "alice" / "a.mp4").write_bytes(b"x" * 1000)
    usage = downloader.get_user_disk_usage("kexiky", use_cache=False)
    assert usage["total_bytes"] == 100 and usage["file_count"] == 1
    assert downloader.get_user_disk_usage("alice", use_cache=False)["total_bytes"] == 1000


# ---------------------------------------------------------------------------
# Task ownership & limits
# ---------------------------------------------------------------------------
def test_tasks_belong_to_their_owner(make_client, no_dns, stub_downloads):
    a, b = make_client(), make_client()
    a.get("/api/auth/me")
    b.get("/api/auth/me")
    tid = a.post("/api/download", json={"url": "https://example.com/v"}).json()["task_id"]

    assert a.get(f"/api/task/{tid}").status_code == 200
    assert b.get(f"/api/task/{tid}").status_code == 404
    assert b.post(f"/api/cancel/{tid}").status_code == 404
    assert b.get(f"/api/progress/{tid}").status_code == 404
    assert b.get("/api/tasks").json()["tasks"] == []
    assert len(a.get("/api/tasks").json()["tasks"]) == 1

    # B cannot wipe A's finished tasks either
    downloader.downloader_manager.tasks[tid].status = "completed"
    assert b.post("/api/tasks/clear-finished").json()["count"] == 0
    assert a.post("/api/tasks/clear-finished").json()["count"] == 1


def test_admin_can_manage_foreign_tasks(make_client, no_dns, stub_downloads):
    guest, admin = make_client(), make_client()
    guest.get("/api/auth/me")
    tid = guest.post("/api/download", json={"url": "https://example.com/v"}).json()["task_id"]
    database.register_user("boss", "secret123")
    database.update_user_role("boss", "admin")
    assert admin.post("/api/auth/login", json={"username": "boss", "password": "secret123"}).status_code == 200
    assert admin.get(f"/api/task/{tid}").status_code == 200
    assert admin.post(f"/api/cancel/{tid}").status_code == 200


def test_cancel_does_not_rewrite_finished_tasks(client, no_dns, stub_downloads):
    client.get("/api/auth/me")
    tid = client.post("/api/download", json={"url": "https://example.com/v"}).json()["task_id"]
    downloader.downloader_manager.tasks[tid].status = "completed"
    client.post(f"/api/cancel/{tid}")
    assert downloader.downloader_manager.tasks[tid].status == "completed"


def test_batch_size_is_limited_by_role(client, no_dns, stub_downloads):
    client.get("/api/auth/me")
    items = [{"url": f"https://example.com/v{i}", "title": f"v{i}"} for i in range(11)]  # guest limit = 10
    r = client.post("/api/download-batch", json={"items": items})
    assert r.status_code == 429
    ok = client.post("/api/download-batch", json={"items": items[:10]})
    assert ok.status_code == 200 and ok.json()["queued_count"] == 10
    # no capacity left
    assert client.post("/api/download", json={"url": "https://example.com/more"}).status_code == 429


def test_batch_hard_cap_and_all_or_nothing_validation(client, no_dns, stub_downloads):
    client.get("/api/auth/me")
    too_many = [{"url": "https://example.com/v"}] * 201
    assert client.post("/api/download-batch", json={"items": too_many}).status_code == 422
    bad = [{"url": "https://example.com/ok"}, {"url": "file:///etc/passwd"}]
    assert client.post("/api/download-batch", json={"items": bad}).status_code == 400
    assert client.get("/api/tasks").json()["tasks"] == []


def test_admin_endpoints_require_admin(client):
    client.get("/api/auth/me")
    for method, url in [("get", "/api/admin/users"), ("get", "/api/admin/system-stats"), ("post", "/api/admin/cleanup"),
                        ("post", "/api/admin/users/x/role"), ("delete", "/api/admin/users/x")]:
        kwargs = {"json": {"role": "user"}} if url.endswith("/role") else {}
        assert getattr(client, method)(url, **kwargs).status_code == 403, url


def test_desktop_actions_are_local_only(client):
    register(client, "alice")
    assert client.post("/api/open-folder").status_code == 403
    assert client.post("/api/reveal/x.mp4").status_code == 403


# ---------------------------------------------------------------------------
# Files: ZIP lifecycle, Range, subtitles
# ---------------------------------------------------------------------------
def _guest_with_file(client, name="a.mp4", content=b"0123456789" * 20):
    me = client.get("/api/auth/me").json()
    root = get_user_root(me["user_id"])
    (root / name).write_bytes(content)
    return me, root


def test_zip_is_deleted_after_sending_and_names_are_unique(client):
    me, root = _guest_with_file(client)
    (root / "sub").mkdir()
    (root / "sub" / "a.mp4").write_bytes(b"other")
    r = client.post("/api/batch/zip", json={"filenames": ["a.mp4", "sub/a.mp4"]})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(names) == ["a (2).mp4", "a.mp4"]
    assert list((DOWNLOADS_DIR / ".temp").glob("*.zip")) == []


def test_playlist_zip_cleans_up(client):
    me, root = _guest_with_file(client)
    (root / "My List").mkdir()
    (root / "My List" / "1.mp4").write_bytes(b"x")
    r = client.get("/api/playlist/zip/My List")
    assert r.status_code == 200
    assert list((DOWNLOADS_DIR / ".temp").glob("*.zip")) == []
    assert client.get("/api/playlist/zip/../../users").status_code in (404, 400)


def test_range_requests(client):
    me, root = _guest_with_file(client, content=bytes(range(100)))
    url = "/api/stream/a.mp4"
    full = client.get(url)
    assert full.status_code == 200 and len(full.content) == 100

    r = client.get(url, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == bytes(range(10, 20))
    assert r.headers["content-range"] == "bytes 10-19/100"

    r = client.get(url, headers={"Range": "bytes=-5"})  # suffix range used to crash the old parser
    assert r.status_code == 206 and r.content == bytes(range(95, 100))

    r = client.get(url, headers={"Range": "bytes=90-"})
    assert r.status_code == 206 and r.content == bytes(range(90, 100))

    r = client.get(url, headers={"Range": "bytes=50-9999"})  # end is clamped
    assert r.status_code == 206 and len(r.content) == 50

    assert client.get(url, headers={"Range": "bytes=500-"}).status_code == 416
    assert client.get(url, headers={"Range": "bytes=abc"}).status_code in (200, 400)  # ignored or rejected, never a crash
    assert client.get(url, headers={"Range": "bytes=0-1,5-6"}).status_code in (200, 206)


def test_parse_range_header_unit():
    p = main.parse_range_header
    assert p(None, 10) is None and p("items=1-2", 10) is None
    assert p("bytes=2-4", 10) == (2, 4)
    assert p("bytes=-3", 10) == (7, 9)
    assert p("bytes=8-", 10) == (8, 9)
    assert p("bytes=0-100", 10) == (0, 9)
    assert p("bytes=5-2", 10) is None
    with pytest.raises(HTTPException):
        p("bytes=10-", 10)


def test_subtitle_track_cannot_read_arbitrary_files(client):
    me, root = _guest_with_file(client)
    (DOWNLOADS_DIR / "secret.vtt").write_text("WEBVTT\n\nsecret")
    for track in ["../../secret.vtt", "..%2F..%2Fsecret.vtt", "/etc/hosts.vtt", "..\\..\\secret.vtt", ".hidden.vtt"]:
        r = client.get("/api/subtitles-track", params={"filename": "a.mp4", "track_id": track})
        assert r.status_code == 404, track
    # a legit companion file next to the media works
    (root / "a.en.vtt").write_text("WEBVTT\n\nhello")
    r = client.get("/api/subtitles-track", params={"filename": "a.mp4", "track_id": "a.en.vtt.vtt"})
    assert r.status_code == 200 and "hello" in r.text
    subs = client.get("/api/subtitles/a.mp4").json()["subtitles"]
    assert subs and subs[0]["url"].startswith("/api/subtitles-track?filename=a.mp4")


def test_subtitles_do_not_fall_back_to_creator_root(client):
    client.get("/api/auth/me")
    (DOWNLOADS_DIR / "creators.mp4").write_bytes(b"x")
    (DOWNLOADS_DIR / "creators.en.vtt").write_text("WEBVTT\n\nprivate")
    r = client.get("/api/subtitles-track", params={"filename": "creators.mp4", "track_id": "creators.en.vtt"})
    assert r.status_code == 404
    assert client.get("/api/subtitles/creators.mp4").json()["subtitles"] == []


def test_thumbnail_cache_is_private(client):
    _guest_with_file(client)
    r = client.get("/api/thumbnail/a.mp4")
    assert r.status_code == 200
    assert "private" in r.headers["cache-control"]


def test_security_headers_and_revalidation(client):
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    idx = client.get("/")
    assert idx.headers["cache-control"] == "no-cache"
    assert "etag" in {k.lower() for k in idx.headers}


def test_delete_user_removes_files(client):
    database.register_user("zed", "secret123")
    root = get_user_root("zed")
    (root / "x.mp4").write_bytes(b"x")
    downloader.remove_user_files("zed")
    assert not root.exists()


def test_delete_playlist_only_direct_folders(client):
    me, root = _guest_with_file(client)
    (root / "PL").mkdir()
    (root / "PL" / "1.mp4").write_bytes(b"x")
    assert client.delete("/api/playlist/PL").status_code == 200
    assert not (root / "PL").exists()
    (root / "A" / "B").mkdir(parents=True)
    assert client.delete("/api/playlist/A/B").status_code == 404
    assert client.delete("/api/playlist/%2E%2E").status_code in (404, 405)
    assert root.exists()
