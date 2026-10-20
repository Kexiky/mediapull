import asyncio
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

pytest.importorskip("aiogram")
pytest.importorskip("yt_dlp")

from aiogram.exceptions import TelegramBadRequest  # noqa: E402
from aiogram.methods import GetMe  # noqa: E402

from app import bot, bot_store, database, downloader  # noqa: E402


@pytest.fixture(autouse=True)
def _bot_state():
    bot_store.init_store()
    database.init_schema()
    with bot_store._conn() as conn:
        for table in ("file_cache", "settings", "acl", "meta", "stats", "shortlinks"):
            conn.execute(f"DELETE FROM {table}")
    with database.get_db_connection() as conn:
        conn.execute("DELETE FROM telegram_links")
        conn.execute("DELETE FROM telegram_link_codes")
    bot.pending_downloads.clear()
    bot.busy_users.clear()
    bot.link_limiter.reset()
    bot.link_attempt_limiter.reset()
    bot.ALLOWED_USER_IDS.clear()
    bot.ADMIN_IDS.clear()
    downloader.downloader_manager._semaphore = None  # the semaphore binds to the loop of the first test that uses it
    yield


def test_allowed_ids_parsing():
    assert bot._parse_allowed_ids("") == set()
    assert bot._parse_allowed_ids("1, 2;3, abc, -100") == {1, 2, 3, -100}


def test_pending_sessions_expire_and_are_bounded():
    bot.pending_downloads["old"] = {"ts": time.time() - bot.PENDING_TTL_SECONDS - 5}
    for i in range(bot.MAX_PENDING_SESSIONS + 20):
        bot.pending_downloads[f"k{i}"] = {"ts": time.time() + i / 1000}
    bot.purge_pending_sessions()
    assert "old" not in bot.pending_downloads
    assert len(bot.pending_downloads) == bot.MAX_PENDING_SESSIONS


def test_temp_cleanup_handles_files_and_job_folders():
    import os
    old_dir = bot.TEMP_TG_DIR / "oldjob"
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "x.mp4").write_bytes(b"x")
    old_file = bot.TEMP_TG_DIR / "old.mp4"
    old_file.write_bytes(b"x")
    fresh = bot.TEMP_TG_DIR / "freshjob"
    fresh.mkdir(exist_ok=True)
    ancient = time.time() - 7200
    for p in (old_dir, old_file):
        os.utime(p, (ancient, ancient))
    bot.cleanup_temp_tg(max_age_seconds=900)
    assert not old_dir.exists() and not old_file.exists() and fresh.exists()
    fresh.rmdir()


# ---------------------------------------------------------------------------
# Quality planning
# ---------------------------------------------------------------------------
MB = 1024 * 1024


def info_with(*pairs):
    return {"resolutions": [{"height": h, "label": f"{h}p", "filesize": size} for h, size in pairs]}


def user_identity(role="user"):
    return bot.Identity(tg_id=1, role=role)


def test_plan_hides_hopeless_sizes_and_picks_best_fitting_auto():
    info = info_with((2160, 900 * MB), (1080, 120 * MB), (720, 40 * MB), (480, 20 * MB))
    plan, auto = bot.plan_video_qualities(info, user_identity("admin"))
    heights = [p["height"] for p in plan]
    assert 2160 not in heights                      # 900 MB is > 6x the 48 MB limit: not offered
    assert heights == [1080, 720, 480]
    assert auto == 720                              # best one that fits in 48 MB
    assert [p["fits"] for p in plan] == [False, True, True]


def test_plan_respects_role_height_cap():
    info = info_with((2160, 30 * MB), (1440, 25 * MB), (1080, 20 * MB), (720, 10 * MB))
    plan, auto = bot.plan_video_qualities(info, user_identity("user"))   # user: 1080p max
    assert [p["height"] for p in plan] == [1080, 720] and auto == 1080
    plan, auto = bot.plan_video_qualities(info, user_identity("premium"))  # premium: 4K
    assert plan[0]["height"] == 2160 and auto == 2160


def test_plan_without_size_information_defaults_to_720():
    plan, auto = bot.plan_video_qualities(info_with((1080, None), (720, None), (480, None)), user_identity())
    assert auto == 720
    plan, auto = bot.plan_video_qualities({"resolutions": []}, user_identity())
    assert auto == 720 and [p["height"] for p in plan] == [1080, 720, 480, 360]


def test_plan_when_everything_is_too_big_picks_the_smallest_offered():
    plan, auto = bot.plan_video_qualities(info_with((1080, 200 * MB), (720, 100 * MB)), user_identity())
    assert auto == 720 and not any(p["fits"] for p in plan)


def test_resolution_estimates_come_from_formats():
    formats = [
        {"height": 1080, "width": 1920, "vcodec": "avc1", "acodec": "none", "filesize": 80 * MB, "ext": "mp4"},
        {"height": 720, "width": 1280, "vcodec": "avc1", "acodec": "none", "filesize": 30 * MB, "ext": "mp4"},
        {"height": 360, "width": 640, "vcodec": "avc1", "acodec": "mp4a", "filesize_approx": 12 * MB, "ext": "mp4"},
        {"vcodec": "none", "acodec": "mp4a", "filesize": 5 * MB, "ext": "m4a"},
        {"height": 90, "width": 160, "vcodec": "avc1", "ext": "mhtml"},
    ]
    opts = {o["height"]: o["filesize"] for o in downloader.build_resolution_options(formats)}
    assert opts == {1080: 85 * MB, 720: 35 * MB, 360: 12 * MB}


def test_subtitle_language_choice_prefers_ru_en():
    info = {"subtitles": [{"code": c} for c in ("de", "en", "ru", "fr", "pt-BR", "zh-Hans")]}
    assert bot.pick_subtitle_langs(info) == ["ru", "en", "de"]
    assert bot.pick_subtitle_langs({"subtitles": []}) == []


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
def test_access_rules():
    assert bot.access_denied_reason(10) is None                       # open by default
    bot_store.set_acl(10, "banned")
    assert bot.access_denied_reason(10) == "banned"
    bot_store.set_acl(10, None)

    bot_store.set_closed_mode(True)
    assert bot.access_denied_reason(10) == "closed"
    bot_store.set_acl(10, "allowed")
    assert bot.access_denied_reason(10) is None
    bot.ALLOWED_USER_IDS.add(11)
    assert bot.access_denied_reason(11) is None
    bot.ADMIN_IDS.add(12)
    assert bot.access_denied_reason(12) is None
    bot_store.set_acl(12, "banned")
    assert bot.access_denied_reason(12) == "banned"                   # even an admin can be banned by another admin's mistake: ban wins


def test_linked_web_admin_is_a_bot_admin_and_passes_closed_mode():
    rec = database.register_user("webadmin", "secret123")
    database.update_user_role("webadmin", "admin")
    code = database.create_telegram_link_code(rec["id"])
    assert database.consume_telegram_link_code(code, 777)["username"] == "webadmin"
    bot_store.set_closed_mode(True)
    assert bot.access_denied_reason(777) is None
    assert bot.resolve_identity(777).is_admin and bot.resolve_identity(777).max_height == 4320
    assert bot.access_denied_reason(778) == "closed"


class FakeMessage:
    def __init__(self, text="", user_id=5, chat_type="private", reply=None):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id, type=chat_type)
        self.reply_to_message = reply
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        return self


def run(coro):
    return asyncio.run(coro)


def test_admin_commands_require_admin_and_manage_lists():
    bot.ADMIN_IDS.add(1)
    cmd = lambda args: SimpleNamespace(args=args)

    stranger = FakeMessage(user_id=2)
    run(bot.handle_allow(stranger, cmd("50")))
    assert stranger.answers == [] and bot_store.get_acl_status(50) is None

    admin = FakeMessage(user_id=1)
    run(bot.handle_allow(admin, cmd("50")))
    run(bot.handle_ban(admin, cmd("51")))
    run(bot.handle_ban(admin, cmd("1")))                              # env admins cannot be banned
    assert bot_store.get_acl_status(50) == "allowed" and bot_store.get_acl_status(51) == "banned"
    assert bot_store.get_acl_status(1) is None
    run(bot.handle_unban(admin, cmd("51")))
    assert bot_store.get_acl_status(51) is None
    run(bot.handle_closed(admin, cmd("on")))
    assert bot_store.get_closed_mode() is True
    run(bot.handle_closed(admin, cmd("off")))
    assert bot_store.get_closed_mode() is False

    reply = FakeMessage(user_id=1, reply=SimpleNamespace(from_user=SimpleNamespace(id=99)))
    run(bot.handle_ban(reply, cmd(None)))
    assert bot_store.get_acl_status(99) == "banned"


def test_stats_command_for_admin():
    bot.ADMIN_IDS.add(1)
    bot_store.record_event(5, size=10 * MB)
    bot_store.record_event(5, cached=True)
    admin = FakeMessage(user_id=1)

    async def scenario():
        async def fake_version():
            return "2099.01.01"
        orig = bot.ytdlp_version
        bot.ytdlp_version = fake_version
        try:
            await bot.handle_stats(admin)
        finally:
            bot.ytdlp_version = orig

    run(scenario())
    assert "Сегодня: <b>1</b> загрузок, <b>1</b> из кэша" in admin.answers[0] and "2099.01.01" in admin.answers[0]


# ---------------------------------------------------------------------------
# /link, /settings
# ---------------------------------------------------------------------------
def test_link_command_flow():
    rec = database.register_user("linker", "secret123")
    code = database.create_telegram_link_code(rec["id"])

    bad = FakeMessage(user_id=8)
    run(bot.handle_link(bad, SimpleNamespace(args="DEADBEEF")))
    assert "неверный" in bad.answers[0]

    group = FakeMessage(user_id=8, chat_type="group")
    run(bot.handle_link(group, SimpleNamespace(args=code)))
    assert "личных сообщениях" in group.answers[0]
    assert database.get_telegram_linked_user(8) is None                # the code was not spent in a group

    ok = FakeMessage(user_id=8)
    run(bot.handle_link(ok, SimpleNamespace(args=code)))
    assert "привязан" in ok.answers[0] and bot.resolve_identity(8).web_user["username"] == "linker"

    reuse = FakeMessage(user_id=9)
    run(bot.handle_link(reuse, SimpleNamespace(args=code)))
    assert "неверный" in reuse.answers[0]                              # one-time code


def test_link_attempts_are_throttled():
    msg = FakeMessage(user_id=8)
    for _ in range(5):
        run(bot.handle_link(msg, SimpleNamespace(args="DEADBEEF")))
    run(bot.handle_link(msg, SimpleNamespace(args="DEADBEEF")))
    assert "Слишком много попыток" in msg.answers[-1]


class FakeCallback:
    def __init__(self, data, user_id=5):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(edit_text=self._edit, delete=self._delete, chat=SimpleNamespace(id=user_id))
        self.edits, self.answers, self.deleted = [], [], False

    async def _edit(self, text, **kw):
        self.edits.append(text)

    async def _delete(self):
        self.deleted = True

    async def answer(self, text=None, **kw):
        self.answers.append(text)


def test_settings_callbacks_update_the_store():
    cb = FakeCallback("st:cap")
    run(bot.handle_settings_callback(cb))
    assert bot_store.get_settings(5)["caption_mode"] == "title"
    run(bot.handle_settings_callback(FakeCallback("st:q:auto")))
    assert bot_store.get_settings(5)["quality_pref"] == "auto"
    run(bot.handle_settings_callback(FakeCallback("st:q:../../x")))   # unknown value is ignored
    assert bot_store.get_settings(5)["quality_pref"] == "auto"
    lib = FakeCallback("st:lib")
    run(bot.handle_settings_callback(lib))
    assert bot_store.get_settings(5)["save_library"] is False and lib.answers[0].startswith("Сначала привяжите")


def test_settings_persist_across_restarts_unlike_the_old_in_memory_dict():
    bot_store.set_setting(5, "caption_mode", "title")
    bot_store.set_setting(5, "quality_pref", "720")
    assert bot_store.get_settings(5) == {"caption_mode": "title", "quality_pref": "720", "save_library": False}
    with pytest.raises(ValueError):
        bot_store.set_setting(5, "quality_pref", "9999")
    with pytest.raises(ValueError):
        bot_store.set_setting(5, "tg_id; DROP TABLE settings", 1)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
class FakeBot:
    def __init__(self, stale_ids=False):
        self.sent = []
        self.stale_ids = stale_ids
        self.counter = 0

    def _result(self, kind):
        self.counter += 1
        fid = f"FILEID{self.counter}"
        if kind == "video":
            return SimpleNamespace(video=SimpleNamespace(file_id=fid))
        if kind == "audio":
            return SimpleNamespace(audio=SimpleNamespace(file_id=fid))
        if kind == "photo":
            return SimpleNamespace(photo=[SimpleNamespace(file_id=fid)])
        return SimpleNamespace(document=SimpleNamespace(file_id=fid))

    def _check(self, media):
        import os
        if isinstance(media, str):
            if self.stale_ids:
                raise TelegramBadRequest(method=GetMe(), message="Bad Request: wrong file identifier")
            return media
        assert os.path.exists(media.path)  # the file must exist while Telegram would be uploading it
        return media.path

    async def send_video(self, **kw):
        self.sent.append(("video", self._check(kw["video"]), kw.get("caption")))
        return self._result("video")

    async def send_audio(self, **kw):
        self.sent.append(("audio", self._check(kw["audio"]), None))
        return self._result("audio")

    async def send_photo(self, **kw):
        self.sent.append(("photo", self._check(kw["photo"]), None))
        return self._result("photo")

    async def send_document(self, **kw):
        self.sent.append(("document", self._check(kw["document"]), kw.get("caption")))
        return self._result("document")

    async def send_message(self, *a, **kw):
        self.sent.append(("message", a, None))


@pytest.fixture
def media_url(tmp_path):
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


def tg_tasks():
    return [t for t in downloader.downloader_manager.tasks if t.startswith("tg_")]


def test_concurrent_downloads_of_one_user_do_not_collide_and_leave_nothing(media_url):
    async def scenario():
        fake = FakeBot()
        results = await asyncio.gather(*(
            bot.download_and_send_single(fake, chat_id=1, user_id=1234567890, url=media_url, title="Clip", action="video", quality="720")
            for _ in range(2)
        ))
        return fake, results

    fake, results = run(scenario())
    assert results == [True, True]
    assert len(fake.sent) == 2 and fake.sent[0][1] != fake.sent[1][1]
    assert list(bot.TEMP_TG_DIR.glob("*")) == []
    assert tg_tasks() == []


def test_failed_download_still_cleans_up_and_is_counted(media_url):
    async def scenario():
        with pytest.raises(RuntimeError):
            await bot.download_and_send_single(
                FakeBot(), chat_id=1, user_id=42, url=media_url.replace("clip.mp4", "missing.mp4"),
                title="Nope", action="video", quality="720")

    run(scenario())
    assert list(bot.TEMP_TG_DIR.glob("*")) == [] and tg_tasks() == []
    assert bot_store.stats_summary()["failures_total"] == 1


def test_file_id_cache_makes_the_second_send_instant(media_url):
    key = "test:clip|video|720"

    async def scenario():
        fake = FakeBot()
        await bot.download_and_send_single(fake, 1, 7, media_url, "Clip", "video", "720", cache_key=key, source_url=media_url)
        first_tasks = len(downloader.downloader_manager.tasks)
        started = time.monotonic()
        await bot.download_and_send_single(fake, 1, 8, media_url, "Clip", "video", "720", cache_key=key, source_url=media_url)
        return fake, time.monotonic() - started, first_tasks

    fake, second_duration, _ = run(scenario())
    assert fake.sent[0][1].endswith(".mp4")           # uploaded the first time ...
    assert fake.sent[1][1] == "FILEID1"               # ... and re-sent by file_id for another user
    assert second_duration < 1.0 and tg_tasks() == []
    stats = bot_store.stats_summary()
    assert stats["downloads_total"] == 1 and stats["cache_hits_total"] == 1


def test_stale_file_id_falls_back_to_a_real_download(media_url):
    key = "test:stale|video|720"
    bot_store.cache_put(key, "video", "OLD_FILE_ID", 1, "Clip", media_url)

    async def scenario():
        fake = FakeBot(stale_ids=True)
        await bot.download_and_send_single(fake, 1, 7, media_url, "Clip", "video", "720", cache_key=key, source_url=media_url)
        return fake

    fake = run(scenario())
    assert fake.sent[0][1].endswith(".mp4")
    assert bot_store.cache_get(key)["file_id"] == "FILEID1"      # refreshed with the new id


def test_caption_setting_is_applied_also_to_cached_sends(media_url):
    key = "test:cap|video|720"
    bot_store.set_setting(7, "caption_mode", "title")

    async def scenario():
        fake = FakeBot()
        await bot.download_and_send_single(fake, 1, 7, media_url, "My title", "video", "720", cache_key=key)
        await bot.download_and_send_single(fake, 1, 7, media_url, "My title", "video", "720", cache_key=key)
        return fake

    fake = run(scenario())
    assert [s[2] for s in fake.sent] == ["My title", "My title"]


def test_library_mode_keeps_the_file_in_the_web_library(media_url):
    rec = database.register_user("libuser", "secret123")
    code = database.create_telegram_link_code(rec["id"])
    database.consume_telegram_link_code(code, 31)
    bot_store.set_setting(31, "save_library", True)

    async def scenario():
        fake = FakeBot()
        await bot.download_and_send_single(fake, 1, 31, media_url, "Clip", "video", "720", cache_key="lib:clip|video|720")
        return fake

    fake = run(scenario())
    assert fake.sent[0][0] == "video"
    saved = list(downloader.get_user_root("libuser").glob("*.mp4"))
    assert len(saved) == 1
    assert bot_store.cache_get("lib:clip|video|720") is None     # a library file is not a throw-away upload
    assert any(s[0] == "message" for s in fake.sent)             # the user is told about the saved copy
    assert list(bot.TEMP_TG_DIR.glob("*")) == []


def test_subtitles_request_without_subtitles_reports_a_clear_error(media_url):
    async def scenario():
        with pytest.raises(RuntimeError, match="Субтитры"):
            await bot.download_and_send_single(FakeBot(), 1, 9, media_url, "Clip", "subs", "best", lang="en")

    run(scenario())
    assert list(bot.TEMP_TG_DIR.glob("*")) == [] and tg_tasks() == []


# ---------------------------------------------------------------------------
# Inline mode, deep links, groups
# ---------------------------------------------------------------------------
class FakeInline:
    def __init__(self, query, user_id=5):
        self.query = query
        self.from_user = SimpleNamespace(id=user_id)
        self.result = None

    async def answer(self, results, **kw):
        self.result = (results, kw)


def test_inline_query_offers_cached_files_or_a_deep_link_button():
    url = "https://example.com/watch/abc"
    bot_store.cache_put("k1", "video", "VID1", 5 * MB, "Clip", downloader.normalize_url(url))
    q = FakeInline(f"look {url}")
    run(bot.handle_inline_query(q))
    results, kw = q.result
    assert len(results) == 1 and results[0].video_file_id == "VID1" and kw["button"] is None

    q2 = FakeInline("https://example.com/other")
    run(bot.handle_inline_query(q2))
    results, kw = q2.result
    assert results == [] and kw["button"].start_parameter.startswith("dl_")
    token = kw["button"].start_parameter[3:]
    assert bot_store.resolve_shortlink(token) == "https://example.com/other"

    q3 = FakeInline("no link here")
    run(bot.handle_inline_query(q3))
    assert q3.result[0] == []


def test_shortlinks_expire():
    token = bot_store.create_shortlink("https://example.com/x")
    assert bot_store.resolve_shortlink(token) == "https://example.com/x"
    with bot_store._conn() as conn:
        conn.execute("UPDATE shortlinks SET expires_at = 1")
    assert bot_store.resolve_shortlink(token) is None


def test_groups_react_only_when_addressed(monkeypatch):
    seen = []

    async def fake_process(message, url):
        seen.append(url)

    monkeypatch.setattr(bot, "process_link", fake_process)
    monkeypatch.setattr(bot, "BOT_USERNAME", "MediaPullBot")

    run(bot.handle_url_message(FakeMessage("https://example.com/a", chat_type="supergroup")))
    assert seen == []                                               # ignored: bot not addressed
    run(bot.handle_url_message(FakeMessage("@mediapullbot https://example.com/a", chat_type="supergroup")))
    assert seen == ["https://example.com/a"]
    run(bot.handle_url_message(FakeMessage("https://example.com/b", chat_type="private")))
    assert seen[-1] == "https://example.com/b"

    reply = FakeMessage(user_id=5, reply=SimpleNamespace(text="check https://example.com/c.", caption=None))
    run(bot.handle_dl_command(reply, SimpleNamespace(args=None)))
    assert seen[-1] == "https://example.com/c"                      # URL taken from the replied message, trailing dot stripped


# ---------------------------------------------------------------------------
# Local Bot API server / webhook
# ---------------------------------------------------------------------------
def test_local_bot_api_server_is_used_when_configured(monkeypatch):
    monkeypatch.setattr(bot, "LOCAL_API_SERVER", "http://telegram-bot-api:8081")
    b = bot.build_bot("123456:ABCDEF")
    assert b.session.api.is_local is True
    assert "telegram-bot-api:8081" in b.session.api.base
    run(b.session.close())
    monkeypatch.setattr(bot, "LOCAL_API_SERVER", "")
    assert bot.build_bot("123456:ABCDEF").session.api.is_local is False


def test_upload_limit_defaults():
    assert bot.MAX_TG_SIZE_BYTES == 48 * MB and bot.TG_HARD_LIMIT_BYTES == 50 * MB


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------
def test_job_queue_orders_jobs_reports_positions_and_eta():
    from app.bot_queue import JobQueue

    async def scenario():
        q = JobQueue(concurrency=1, default_job_seconds=60, poll_seconds=0.01)
        order, positions = [], []

        async def job(name, hold):
            async def on_pos(pos, eta):
                positions.append((name, pos, eta))
            started = await q.acquire(on_pos)
            order.append(name)
            await asyncio.sleep(hold)
            q.release(started)

        await asyncio.gather(job("a", 0.1), job("b", 0.05), job("c", 0.0))
        return order, positions, q

    order, positions, q = run(scenario())
    assert order == ["a", "b", "c"]
    assert ("b", 1, 60) in positions and ("c", 2, 120) in positions
    assert q.running == 0 and q.waiting == 0
    assert 0 < q.average_seconds() < 5                               # learned from real durations


def test_job_queue_cancelled_waiter_leaves_the_line():
    from app.bot_queue import JobQueue

    async def scenario():
        q = JobQueue(concurrency=1, poll_seconds=0.01)
        first = await q.acquire()
        waiter = asyncio.create_task(q.acquire())
        await asyncio.sleep(0.05)
        assert q.waiting == 1
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert q.waiting == 0
        q.release(first)
        again = await asyncio.wait_for(q.acquire(), timeout=1)
        q.release(again)

    run(scenario())
