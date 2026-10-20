import os
import re
import json
import uuid
import time
import asyncio
import secrets
import shutil
import urllib.parse
import zipfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.datastructures import MutableHeaders

from app.downloader import (
    downloader_manager, DOWNLOADS_DIR, TEMP_DIR, DownloadLimitError, resolve_download_path,
    get_user_disk_usage, format_size, remove_user_files, assert_public_host, validate_media_url,
)
from app.auth import (
    create_user_token, current_user, require_admin, require_registered, public_user, TOKEN_MAX_AGE_SECONDS,
)
from app.database import (
    init_db, register_user, authenticate_user, update_user_password,
    update_user_role, list_users, delete_user, ROLE_CONFIGS, CREATOR_USERNAME, DATA_DIR,
    create_telegram_link_code, get_user_telegram_id, unlink_telegram, LINK_CODE_TTL_SECONDS,
)
from app.updater import auto_update_enabled, ytdlp_update_loop
from app.cleanup import cleanup_loop, cleanup_expired_files
from app.ratelimit import SlidingWindowLimiter, client_ip, is_local_request, request_is_https

MAX_BATCH_ITEMS = 200
MAX_ZIP_FILES = 500

# Brute-force / abuse protection (per client address and per target account)
_login_ip_limiter = SlidingWindowLimiter(max_events=30, window_seconds=300)
_login_user_limiter = SlidingWindowLimiter(max_events=10, window_seconds=300)
_register_limiter = SlidingWindowLimiter(max_events=5, window_seconds=3600)
_info_limiter = SlidingWindowLimiter(max_events=30, window_seconds=60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    cleanup_task = asyncio.create_task(cleanup_loop(interval_seconds=900))
    updater_task = asyncio.create_task(ytdlp_update_loop()) if auto_update_enabled() else None
    try:
        yield
    finally:
        for task in (cleanup_task, updater_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await downloader_manager.shutdown()


app = FastAPI(title="MediaPull — Universal Media Downloader by Kexiky", lifespan=lifespan)

allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
if allowed_origins_env:
    allowed_origins = [orig.strip() for orig in allowed_origins_env.split(",") if orig.strip()]
else:
    web_url = os.getenv("MEDIAPULL_WEB_URL", "http://127.0.0.1:8000").rstrip("/")
    allowed_origins = list({
        web_url,
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    })

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware (does not interfere with streaming responses or client-disconnect handling)."""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "same-origin")
                headers.setdefault("Content-Security-Policy", "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
                # Static files carry ETag / Last-Modified: "no-cache" = always revalidate, but answer 304 when unchanged.
                if path == "/" or path.endswith((".html", ".js", ".css")):
                    headers["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(SecurityHeadersMiddleware)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def json_response(request: Request, user: Dict[str, Any], payload: Any, status_code: int = 200) -> JSONResponse:
    """JSONResponse that also issues the signed guest-session cookie for brand-new visitors."""
    res = JSONResponse(payload, status_code=status_code)
    if user.get("is_new_session") and user.get("session_token"):
        res.set_cookie(
            "mediapull_session", user["session_token"], max_age=30 * 86400,
            httponly=True, samesite="lax", secure=request_is_https(request),
        )
    return res


def set_auth_cookie(request: Request, res: Response, token: str) -> None:
    res.set_cookie(
        "mediapull_auth", token, max_age=TOKEN_MAX_AGE_SECONDS,
        httponly=True, samesite="lax", secure=request_is_https(request),
    )


def auth_payload(user_rec: Dict[str, Any], token: str) -> Dict[str, Any]:
    role_cfg = ROLE_CONFIGS.get(user_rec["role"], ROLE_CONFIGS["user"])
    return {
        "status": "ok",
        "user_id": user_rec["username"],
        "username": user_rec["username"],
        "role": user_rec["role"],
        "role_title": role_cfg["title"],
        "role_icon": role_cfg["icon"],
        "role_badge_class": role_cfg["badge_class"],
        "is_admin": user_rec["role"] == "admin",
        "is_creator": user_rec["username"] == CREATOR_USERNAME,
        "token": token,
    }


def too_many_requests(limiter: SlidingWindowLimiter, key: str, message: str) -> HTTPException:
    return HTTPException(status_code=429, detail=message, headers={"Retry-After": str(limiter.retry_after(key))})


def can_access_task(user: Dict[str, Any], task) -> bool:
    return user.get("is_admin") or task.user_id == user["user_id"]


def get_owned_task(task_id: str, user: Dict[str, Any]):
    task = downloader_manager.get_task(task_id)
    # 404 for foreign tasks as well, so task ids cannot be probed.
    if not task or not can_access_task(user, task):
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return task


def visible_tasks(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    return downloader_manager.get_all_tasks(user_id=None if user.get("is_admin") else user["user_id"])


async def validate_public_urls(urls: List[str]) -> List[str]:
    """Syntax-check every URL and make sure each distinct host resolves to a public address."""
    clean = [validate_media_url(u) for u in urls]
    hosts = {urllib.parse.urlsplit(u).hostname for u in clean}
    await asyncio.gather(*(asyncio.to_thread(assert_public_host, h) for h in hosts))
    return clean


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class LoginRequest(BaseModel):
    username: Optional[str] = Field(default=None, max_length=64)
    password: str = Field(max_length=256)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class UpdateRoleRequest(BaseModel):
    role: str = Field(max_length=32)


class InfoRequest(BaseModel):
    url: str = Field(max_length=2048)


class DownloadRequest(BaseModel):
    url: str = Field(max_length=2048)
    title: Optional[str] = Field(default="Media Download", max_length=500)
    mode: str = Field(default="video", max_length=16)  # "video", "audio", or "photo"
    quality: str = Field(default="best", max_length=16)
    video_format: str = Field(default="mp4", max_length=16)
    audio_format: str = Field(default="mp3", max_length=16)
    audio_quality: str = Field(default="320", max_length=16)
    trim_start: Optional[str] = Field(default=None, max_length=16)
    trim_end: Optional[str] = Field(default=None, max_length=16)
    download_subtitles: bool = False
    subtitles_lang: Optional[str] = Field(default="en", max_length=64)
    embed_subtitles: bool = False
    embed_thumbnail: bool = True
    split_chapters: bool = False
    is_photo: bool = False
    photo_url: Optional[str] = Field(default=None, max_length=2048)


class BatchItem(BaseModel):
    url: str = Field(max_length=2048)
    title: Optional[str] = Field(default="", max_length=500)
    index: Optional[int] = None
    is_photo: bool = False
    photo_url: Optional[str] = Field(default=None, max_length=2048)


class BatchDownloadRequest(BaseModel):
    items: List[BatchItem] = Field(max_length=MAX_BATCH_ITEMS)
    mode: str = Field(default="video", max_length=16)
    quality: str = Field(default="best", max_length=16)
    video_format: str = Field(default="mp4", max_length=16)
    audio_format: str = Field(default="mp3", max_length=16)
    audio_quality: str = Field(default="320", max_length=16)
    download_subtitles: bool = False
    subtitles_lang: Optional[str] = Field(default="en", max_length=64)
    embed_subtitles: bool = False
    embed_thumbnail: bool = True
    playlist_title: Optional[str] = Field(default=None, max_length=300)
    playlist_thumbnail: Optional[str] = Field(default=None, max_length=2048)


class BatchFilesRequest(BaseModel):
    filenames: List[str] = Field(max_length=MAX_ZIP_FILES)


class OpenFolderRequest(BaseModel):
    subfolder: Optional[str] = Field(default=None, max_length=1024)


# ---------------------------------------------------------------------------
# Health & auth
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    """Lightweight liveness probe (does not create sessions)."""
    return {"status": "ok"}


@app.get("/api/auth/me")
async def get_auth_me(request: Request, user: Dict[str, Any] = Depends(current_user)):
    usage = await asyncio.to_thread(get_user_disk_usage, user["user_id"], user["role"])
    payload = public_user(user)
    payload["used_bytes"] = usage["total_bytes"]
    payload["file_count"] = usage["file_count"]
    payload["used_size_str"] = usage["total_size_str"]
    return json_response(request, user, payload)


@app.post("/api/auth/register")
async def auth_register(req: RegisterRequest, request: Request):
    ip = client_ip(request)
    if not _register_limiter.hit(ip):
        raise too_many_requests(_register_limiter, ip, "Слишком много регистраций с вашего адреса. Попробуйте позже.")
    try:
        user_rec = await asyncio.to_thread(register_user, req.username, req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    token = create_user_token(user_rec["id"], user_rec["username"], user_rec["role"])
    res = JSONResponse(auth_payload(user_rec, token))
    set_auth_cookie(request, res, token)
    return res


@app.post("/api/auth/login")
async def auth_login(req: LoginRequest, request: Request):
    # Master-password-only login maps to the creator account.
    username = (req.username or "").strip() or CREATOR_USERNAME
    ip_key, user_key = f"ip:{client_ip(request)}", f"user:{username.lower()}"
    if not _login_ip_limiter.hit(ip_key):
        raise too_many_requests(_login_ip_limiter, ip_key, "Слишком много попыток входа. Попробуйте позже.")
    if not _login_user_limiter.hit(user_key):
        raise too_many_requests(_login_user_limiter, user_key, "Слишком много попыток входа в этот аккаунт. Попробуйте позже.")

    user_rec = await asyncio.to_thread(authenticate_user, username, req.password)
    if not user_rec:
        detail = "Неверный пароль Создателя" if not (req.username or "").strip() else "Неверный логин или пароль"
        raise HTTPException(status_code=401, detail=detail)

    _login_user_limiter.reset(user_key)
    token = create_user_token(user_rec["id"], user_rec["username"], user_rec["role"])
    res = JSONResponse(auth_payload(user_rec, token))
    set_auth_cookie(request, res, token)
    return res


@app.post("/api/auth/logout")
async def auth_logout():
    res = JSONResponse({"status": "ok", "message": "Logged out"})
    res.delete_cookie("mediapull_auth")
    return res


@app.post("/api/auth/change-password")
async def auth_change_password(req: ChangePasswordRequest, user: Dict[str, Any] = Depends(require_registered)):
    key = f"user:{user['username'].lower()}"
    if not _login_user_limiter.hit(key):
        raise too_many_requests(_login_user_limiter, key, "Слишком много попыток. Попробуйте позже.")
    try:
        await asyncio.to_thread(update_user_password, user["username"], req.old_password, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "message": "Пароль успешно изменен"}


# ---------------------------------------------------------------------------
# Telegram account linking
# ---------------------------------------------------------------------------
_link_limiter = SlidingWindowLimiter(max_events=10, window_seconds=3600)


@app.get("/api/auth/telegram-link")
async def telegram_link_status(user: Dict[str, Any] = Depends(require_registered)):
    telegram_id = await asyncio.to_thread(get_user_telegram_id, user["db_id"])
    return {
        "linked": telegram_id is not None,
        "telegram_id": telegram_id,
        "bot_username": os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@") or None,
    }


@app.post("/api/auth/telegram-link")
async def telegram_link_create(user: Dict[str, Any] = Depends(require_registered)):
    """Issue a one-time code; the user sends it to the bot as `/link CODE`."""
    key = f"user:{user['db_id']}"
    if not _link_limiter.hit(key):
        raise too_many_requests(_link_limiter, key, "Слишком много запросов кода. Попробуйте позже.")
    code = await asyncio.to_thread(create_telegram_link_code, user["db_id"])
    return {
        "code": code,
        "expires_in": LINK_CODE_TTL_SECONDS,
        "bot_username": os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@") or None,
    }


@app.delete("/api/auth/telegram-link")
async def telegram_link_remove(user: Dict[str, Any] = Depends(require_registered)):
    await asyncio.to_thread(unlink_telegram, user_id=user["db_id"])
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.get("/api/admin/users")
async def admin_get_users(_admin: Dict[str, Any] = Depends(require_admin)):
    users = await asyncio.to_thread(list_users)

    def add_usage():
        for u in users:
            usage = get_user_disk_usage(u["username"], u["role"])
            u["used_bytes"] = usage["total_bytes"]
            u["file_count"] = usage["file_count"]
            u["used_size_str"] = usage["total_size_str"]

    await asyncio.to_thread(add_usage)
    return {"users": users, "roles": ROLE_CONFIGS}


@app.post("/api/admin/users/{username}/role")
async def admin_set_user_role(username: str, req: UpdateRoleRequest, _admin: Dict[str, Any] = Depends(require_admin)):
    try:
        await asyncio.to_thread(update_user_role, username, req.role)
        return {"status": "ok", "message": f"Роль пользователя '{username}' изменена на '{req.role}'"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/admin/users/{username}")
async def admin_delete_user(username: str, _admin: Dict[str, Any] = Depends(require_admin)):
    try:
        deleted = await asyncio.to_thread(delete_user, username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if deleted:
        # Do not let a later registration with the same name inherit the old files.
        await asyncio.to_thread(remove_user_files, username)
    return {"status": "ok", "message": f"Пользователь '{username}' удален"}


@app.post("/api/admin/cleanup")
async def admin_trigger_cleanup(_admin: Dict[str, Any] = Depends(require_admin)):
    res = await asyncio.to_thread(cleanup_expired_files)
    return {"status": "ok", "result": res}


@app.get("/api/admin/system-stats")
async def admin_system_stats(_admin: Dict[str, Any] = Depends(require_admin)):
    total, used, free = await asyncio.to_thread(shutil.disk_usage, str(DOWNLOADS_DIR))
    db_file = DATA_DIR / "mediapull.db"
    db_size = db_file.stat().st_size if db_file.exists() else 0
    all_users = await asyncio.to_thread(list_users)
    return {
        "disk_total_bytes": total,
        "disk_total_str": format_size(total),
        "disk_used_bytes": used,
        "disk_used_str": format_size(used),
        "disk_free_bytes": free,
        "disk_free_str": format_size(free),
        "db_size_bytes": db_size,
        "db_size_str": format_size(db_size),
        "total_users": len(all_users)
    }


# ---------------------------------------------------------------------------
# Info & downloads
# ---------------------------------------------------------------------------
@app.post("/api/info")
async def get_info(req: InfoRequest, request: Request):
    ip = client_ip(request)
    if not _info_limiter.hit(ip):
        raise too_many_requests(_info_limiter, ip, "Слишком много запросов. Подождите минуту.")
    try:
        url = (await validate_public_urls([req.url.strip()]))[0]
        return await downloader_manager.get_video_info(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/download")
async def start_download(req: DownloadRequest, request: Request, user: Dict[str, Any] = Depends(current_user)):
    task_id = secrets.token_hex(8)
    try:
        url = (await validate_public_urls([req.url]))[0]
        await downloader_manager.start_download(
            task_id=task_id,
            url=url,
            title=req.title or "Media Download",
            mode=req.mode,
            quality=req.quality,
            video_format=req.video_format,
            audio_format=req.audio_format,
            audio_quality=req.audio_quality,
            trim_start=req.trim_start,
            trim_end=req.trim_end,
            download_subtitles=req.download_subtitles,
            subtitles_lang=req.subtitles_lang,
            embed_subtitles=req.embed_subtitles,
            embed_thumbnail=req.embed_thumbnail,
            split_chapters=req.split_chapters,
            is_photo=req.is_photo,
            photo_url=req.photo_url,
            user_id=user["user_id"],
            role=user["role"]
        )
    except DownloadLimitError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return json_response(request, user, {"task_id": task_id, "status": "queued", "user_id": user["user_id"]})


@app.post("/api/download-batch")
async def download_batch(req: BatchDownloadRequest, request: Request, user: Dict[str, Any] = Depends(current_user)):
    if not req.items:
        raise HTTPException(status_code=400, detail="Список элементов пуст")

    capacity = downloader_manager.active_task_capacity(user["user_id"], user["role"])
    if len(req.items) > capacity:
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много загрузок сразу: для вашего тарифа сейчас доступно ещё {capacity}. Выберите меньше элементов."
        )

    try:
        # Validate everything first so a bad item does not leave the batch half-queued.
        urls = await validate_public_urls([item.url for item in req.items])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    task_ids = []
    try:
        for idx, (item, url) in enumerate(zip(req.items, urls), start=1):
            tid = secrets.token_hex(8)
            item_index = item.index if item.index is not None else idx
            item_mode = "photo" if item.is_photo else req.mode
            await downloader_manager.start_download(
                task_id=tid,
                url=url,
                title=item.title or "Media Download",
                mode=item_mode,
                quality=req.quality,
                video_format=req.video_format,
                audio_format=req.audio_format,
                audio_quality=req.audio_quality,
                download_subtitles=req.download_subtitles,
                subtitles_lang=req.subtitles_lang,
                embed_subtitles=req.embed_subtitles,
                embed_thumbnail=req.embed_thumbnail,
                split_chapters=False,
                playlist_title=req.playlist_title,
                playlist_index=item_index,
                playlist_thumbnail=req.playlist_thumbnail,
                is_photo=item.is_photo,
                photo_url=item.photo_url,
                user_id=user["user_id"],
                role=user["role"]
            )
            task_ids.append(tid)
    except DownloadLimitError as e:
        if not task_ids:
            raise HTTPException(status_code=429, detail=str(e))
    except ValueError as e:
        if not task_ids:
            raise HTTPException(status_code=400, detail=str(e))

    return json_response(request, user, {"queued_count": len(task_ids), "task_ids": task_ids, "user_id": user["user_id"]})


@app.get("/api/task/{task_id}")
async def get_task_status(task_id: str, user: Dict[str, Any] = Depends(current_user)):
    return get_owned_task(task_id, user).to_dict()


@app.get("/api/progress/{task_id}")
async def stream_progress(task_id: str, request: Request, user: Dict[str, Any] = Depends(current_user)):
    get_owned_task(task_id, user)

    async def event_generator():
        while True:
            task = downloader_manager.get_task(task_id)
            if task is None:
                yield 'event: error\ndata: {"error": "Задача удалена"}\n\n'
                break

            yield f"data: {json.dumps(task.to_dict(), ensure_ascii=False)}\n\n"

            if task.status in ("completed", "error", "cancelled"):
                break
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/progress-stream")
async def stream_all_progress(request: Request, user: Dict[str, Any] = Depends(current_user)):
    async def event_generator():
        last_payload = None
        last_sent = 0.0
        while True:
            tasks_data = visible_tasks(user)
            payload = json.dumps({"tasks": tasks_data}, ensure_ascii=False)
            now = time.monotonic()
            if payload != last_payload:
                last_payload, last_sent = payload, now
                yield f"data: {payload}\n\n"
            elif now - last_sent > 15:
                last_sent = now
                yield ": keep-alive\n\n"  # keeps proxies from closing an idle stream

            if await request.is_disconnected():
                break
            has_active = any(t.get("status") in ("queued", "downloading", "converting") for t in tasks_data)
            await asyncio.sleep(0.5 if has_active else 2.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/tasks")
async def get_all_tasks(user: Dict[str, Any] = Depends(current_user)):
    return {"tasks": visible_tasks(user)}


@app.post("/api/tasks/clear-finished")
async def clear_finished_tasks(user: Dict[str, Any] = Depends(current_user)):
    cleared = downloader_manager.clear_finished_tasks(user_id=None if user.get("is_admin") else user["user_id"])
    return {"status": "cleared", "count": cleared}


@app.post("/api/cancel/{task_id}")
async def cancel_task(task_id: str, user: Dict[str, Any] = Depends(current_user)):
    get_owned_task(task_id, user)
    downloader_manager.cancel_task(task_id)
    return {"status": "cancelled"}


# ---------------------------------------------------------------------------
# Library: history, thumbnails, files
# ---------------------------------------------------------------------------
AUDIO_SVG_FALLBACK = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" width="640" height="360">
  <defs>
    <linearGradient id="bgAudio" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#141419"/>
      <stop offset="50%" stop-color="#1e1828"/>
      <stop offset="100%" stop-color="#0f0f13"/>
    </linearGradient>
    <linearGradient id="waveAudio" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#ff0033"/>
      <stop offset="50%" stop-color="#ff3b69"/>
      <stop offset="100%" stop-color="#ff8c00"/>
    </linearGradient>
  </defs>
  <rect width="640" height="360" fill="url(#bgAudio)"/>
  <circle cx="320" cy="160" r="65" fill="rgba(255, 255, 255, 0.04)" stroke="rgba(255, 255, 255, 0.08)" stroke-width="2"/>
  <circle cx="320" cy="160" r="42" fill="rgba(255, 255, 255, 0.06)"/>
  <circle cx="320" cy="160" r="16" fill="#ff0033"/>
  <g fill="url(#waveAudio)" opacity="0.85">
    <rect x="210" y="145" width="6" height="30" rx="3"/>
    <rect x="228" y="130" width="6" height="60" rx="3"/>
    <rect x="246" y="115" width="6" height="90" rx="3"/>
    <rect x="388" y="115" width="6" height="90" rx="3"/>
    <rect x="406" y="130" width="6" height="60" rx="3"/>
    <rect x="424" y="145" width="6" height="30" rx="3"/>
  </g>
  <text x="320" y="270" fill="#f1f1f1" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="15" font-weight="700" text-anchor="middle" letter-spacing="2">AUDIO TRACK</text>
  <text x="320" y="295" fill="#888899" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="12" text-anchor="middle">MediaPull by Kexiky</text>
</svg>"""

VIDEO_SVG_FALLBACK = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" width="640" height="360">
  <defs>
    <linearGradient id="bgVideo" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#141416"/>
      <stop offset="100%" stop-color="#0a0a0c"/>
    </linearGradient>
  </defs>
  <rect width="640" height="360" fill="url(#bgVideo)"/>
  <circle cx="320" cy="165" r="48" fill="rgba(255, 0, 51, 0.12)" stroke="#ff0033" stroke-width="2"/>
  <polygon points="312,145 336,165 312,185" fill="#ff0033"/>
  <text x="320" y="255" fill="#f1f1f1" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="15" font-weight="600" text-anchor="middle">VIDEO PREVIEW</text>
</svg>"""

PRIVATE_CACHE = {"Cache-Control": "private, max-age=86400"}


@app.get("/api/history")
async def get_history(request: Request, user: Dict[str, Any] = Depends(current_user)):
    data = await asyncio.to_thread(downloader_manager.list_history, user_id=user["user_id"], role=user["role"])
    return json_response(request, user, {
        "files": data["files"],
        "playlists": data["playlists"],
        "count": len(data["files"]),
        "playlist_count": len(data["playlists"]),
        "user": public_user(user)
    })


@app.api_route("/api/thumbnail/{filename:path}", methods=["GET", "HEAD"])
async def get_thumbnail(filename: str, user: Dict[str, Any] = Depends(current_user)):
    thumb_path = await asyncio.to_thread(downloader_manager.get_or_create_thumbnail, filename, user_id=user["user_id"])

    if thumb_path and thumb_path.exists() and thumb_path.stat().st_size > 0:
        return FileResponse(path=str(thumb_path), media_type="image/jpeg", headers=PRIVATE_CACHE)

    ext = Path(os.path.basename(filename)).suffix.lower()
    audio_exts = {".mp3", ".m4a", ".wav", ".flac", ".opus", ".aac", ".ogg"}
    svg = AUDIO_SVG_FALLBACK if ext in audio_exts else VIDEO_SVG_FALLBACK
    return Response(content=svg, media_type="image/svg+xml", headers=PRIVATE_CACHE)


@app.delete("/api/history/{filename:path}")
async def delete_history_item(filename: str, user: Dict[str, Any] = Depends(current_user)):
    success = await asyncio.to_thread(downloader_manager.delete_file, filename, user_id=user["user_id"])
    if not success:
        raise HTTPException(status_code=404, detail="Файл не найден на диске")
    return {"deleted": filename}


@app.delete("/api/playlist/{folder_name:path}")
async def delete_playlist(folder_name: str, user: Dict[str, Any] = Depends(current_user)):
    success = await asyncio.to_thread(downloader_manager.delete_playlist, folder_name, user_id=user["user_id"])
    if not success:
        raise HTTPException(status_code=404, detail="Плейлист не найден")
    return {"deleted": folder_name}


def _unique_arcname(name: str, used: set) -> str:
    candidate, n = name, 1
    stem, suffix = os.path.splitext(name)
    while candidate in used:
        n += 1
        candidate = f"{stem} ({n}){suffix}"
    used.add(candidate)
    return candidate


def _build_zip(files: List[Path]) -> Path:
    """Write `files` into a fresh ZIP under .temp (media is already compressed, so STORED is used)."""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = TEMP_DIR / f"archive_{uuid.uuid4().hex[:12]}.zip"
    used: set = set()
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zipf:
            for f in files:
                zipf.write(f, arcname=_unique_arcname(f.name, used))
    except BaseException:
        zip_path.unlink(missing_ok=True)
        raise
    return zip_path


def _zip_response(zip_path: Path, download_name: str) -> FileResponse:
    if not zip_path.exists() or zip_path.stat().st_size == 0:
        zip_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Не удалось создать ZIP архив")
    # The archive only exists for this response: delete it as soon as it has been sent.
    return FileResponse(
        path=str(zip_path), filename=download_name, media_type="application/zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@app.api_route("/api/playlist/zip/{folder_name:path}", methods=["GET", "HEAD"])
async def download_playlist_zip(folder_name: str, user: Dict[str, Any] = Depends(current_user)):
    folder_path = resolve_download_path(folder_name, user_id=user["user_id"])
    if not folder_path or not folder_path.is_dir():
        raise HTTPException(status_code=404, detail="Папка плейлиста не найдена")

    files = [f for f in sorted(folder_path.glob("*")) if f.is_file() and not f.name.startswith(".")]
    if not files:
        raise HTTPException(status_code=404, detail="В плейлисте нет файлов")
    clean_title = re.sub(r'[\\/*?:"<>|]', "", folder_path.name).strip() or "playlist"
    zip_path = await asyncio.to_thread(_build_zip, files)
    return _zip_response(zip_path, f"{clean_title}.zip")


@app.post("/api/batch/zip")
async def batch_zip_download(req: BatchFilesRequest, user: Dict[str, Any] = Depends(current_user)):
    if not req.filenames:
        raise HTTPException(status_code=400, detail="Не выбраны файлы для архивации")

    files = []
    for fn in req.filenames:
        target = resolve_download_path(fn, user_id=user["user_id"])
        if target and target.is_file():
            files.append(target)
    if not files:
        raise HTTPException(status_code=400, detail="Не удалось создать ZIP архив")

    zip_path = await asyncio.to_thread(_build_zip, files)
    return _zip_response(zip_path, "media_downloads.zip")


@app.api_route("/api/files/{filename:path}", methods=["GET", "HEAD"])
async def download_file(filename: str, user: Dict[str, Any] = Depends(current_user)):
    target = resolve_download_path(filename, user_id=user["user_id"])
    if not target or not target.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    return FileResponse(path=str(target), filename=target.name, media_type="application/octet-stream")


STREAM_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def parse_range_header(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """Parse a single `bytes=` range. None = serve the whole file; 416 when the range cannot be satisfied."""
    if not header or size <= 0 or not header.lower().startswith("bytes="):
        return None
    spec = header[6:].strip()
    if "," in spec:
        return None  # multi-range requests are answered with the full file
    start_s, sep, end_s = spec.partition("-")
    if not sep:
        return None
    try:
        if start_s == "":
            suffix = int(end_s)
            if suffix <= 0:
                raise ValueError
            start, end = max(0, size - suffix), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start < 0:
        return None
    if start >= size:
        raise HTTPException(status_code=416, detail="Range Not Satisfiable", headers={"Content-Range": f"bytes */{size}"})
    if end < start:
        return None  # syntactically invalid range: ignore it and serve the whole file
    return start, min(end, size - 1)


@app.api_route("/api/stream/{filename:path}", methods=["GET", "HEAD"])
async def stream_file(filename: str, request: Request, user: Dict[str, Any] = Depends(current_user)):
    target = resolve_download_path(filename, user_id=user["user_id"])
    if not target or not target.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    content_type = STREAM_CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
    file_size = target.stat().st_size
    byte_range = parse_range_header(request.headers.get("range"), file_size)

    if byte_range is None:
        return FileResponse(str(target), media_type=content_type, headers={"Accept-Ranges": "bytes"})

    start, end = byte_range
    chunk_size = end - start + 1

    def iter_chunk():
        with open(target, "rb") as f:
            f.seek(start)
            remaining = chunk_size
            while remaining > 0:
                data = f.read(min(remaining, 256 * 1024))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_size),
    }
    return StreamingResponse(iter_chunk(), status_code=206, headers=headers, media_type=content_type)


@app.post("/api/batch/delete")
async def batch_delete_files(req: BatchFilesRequest, user: Dict[str, Any] = Depends(current_user)):
    if not req.filenames:
        raise HTTPException(status_code=400, detail="Не выбраны файлы")
    return await asyncio.to_thread(downloader_manager.delete_files_batch, req.filenames, user_id=user["user_id"])


# ---------------------------------------------------------------------------
# Server-side desktop actions (only for a registered user sitting at the machine running the server)
# ---------------------------------------------------------------------------
def _require_local_desktop(request: Request, user: Dict[str, Any]) -> None:
    if user.get("role") == "guest":
        raise HTTPException(status_code=403, detail="Доступ запрещен в гостевом режиме")
    if not is_local_request(request):
        raise HTTPException(status_code=403, detail="Действие доступно только при работе с локального компьютера сервера")


@app.post("/api/reveal/{filename:path}")
async def reveal_file(filename: str, request: Request, user: Dict[str, Any] = Depends(current_user)):
    _require_local_desktop(request, user)
    success = await asyncio.to_thread(downloader_manager.reveal_in_finder, filename, user_id=user["user_id"])
    if not success:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return {"status": "revealed", "filename": filename}


@app.post("/api/open-folder")
async def open_folder(request: Request, req: Optional[OpenFolderRequest] = None, user: Dict[str, Any] = Depends(current_user)):
    _require_local_desktop(request, user)
    sub = req.subfolder if req else None
    success = await asyncio.to_thread(downloader_manager.open_downloads_folder, sub, user_id=user["user_id"])
    return {"status": "opened" if success else "failed"}


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------
@app.get("/api/subtitles/{filename:path}")
async def get_subtitles(filename: str, user: Dict[str, Any] = Depends(current_user)):
    subs = await asyncio.to_thread(downloader_manager.get_file_subtitles, filename, user_id=user["user_id"])
    return {"subtitles": subs}


async def _subtitle_vtt_response(filename: str, track_id: str, user: Dict[str, Any]) -> Response:
    vtt_content = await asyncio.to_thread(downloader_manager.extract_subtitle_vtt, filename, track_id, user_id=user["user_id"])
    if not vtt_content:
        raise HTTPException(status_code=404, detail="Субтитры не найдены")
    return Response(content=vtt_content, media_type="text/vtt; charset=utf-8")


@app.get("/api/subtitles-track")
async def get_subtitle_vtt_param(filename: str, track_id: str, user: Dict[str, Any] = Depends(current_user)):
    return await _subtitle_vtt_response(filename, track_id, user)


@app.get("/api/subtitles-track/{filename}/{track_id:path}")
async def get_subtitle_vtt(filename: str, track_id: str, user: Dict[str, Any] = Depends(current_user)):
    return await _subtitle_vtt_response(filename, track_id, user)


# Mount static frontend
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
