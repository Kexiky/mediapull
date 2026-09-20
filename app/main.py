import os
import re
import uuid
import asyncio
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.downloader import (
    downloader_manager, DOWNLOADS_DIR, resolve_download_path, get_user_root,
    get_user_disk_usage, format_size
)
from app.auth import get_current_user, create_user_token
from app.database import (
    register_user, authenticate_user, update_user_password,
    update_user_role, list_users, delete_user, ROLE_CONFIGS, DATA_DIR
)
from app.cleanup import cleanup_loop, cleanup_expired_files

app = FastAPI(title="MediaPull — Universal Media Downloader by Kexiky")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(cleanup_loop(interval_seconds=900))

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.endswith((".html", ".js", ".css")) or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: Optional[str] = None
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class UpdateRoleRequest(BaseModel):
    role: str


@app.get("/api/auth/me")
async def get_auth_me(request: Request):
    user = get_current_user(request)
    usage = get_user_disk_usage(user["user_id"], user["role"])
    user["used_bytes"] = usage["total_bytes"]
    user["file_count"] = usage["file_count"]
    user["used_size_str"] = usage["total_size_str"]

    res = JSONResponse(user)
    if user.get("is_new_session"):
        res.set_cookie("mediapull_session", user["user_id"], max_age=30 * 86400, httponly=False, samesite="lax")
    return res


@app.post("/api/auth/register")
async def auth_register(req: RegisterRequest):
    try:
        user_rec = register_user(req.username, req.password)
        token = create_user_token(user_rec["id"], user_rec["username"], user_rec["role"])
        role_cfg = ROLE_CONFIGS.get(user_rec["role"], ROLE_CONFIGS["user"])
        res = JSONResponse({
            "status": "ok",
            "user_id": user_rec["username"],
            "username": user_rec["username"],
            "role": user_rec["role"],
            "role_title": role_cfg["title"],
            "role_icon": role_cfg["icon"],
            "role_badge_class": role_cfg["badge_class"],
            "token": token
        })
        res.set_cookie("mediapull_auth", token, max_age=365 * 86400, httponly=True, samesite="lax")
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/login")
async def auth_login(req: LoginRequest):
    username = (req.username or "").strip()
    if not username:
        # Default to kexiky if only master password is provided
        user_rec = authenticate_user("kexiky", req.password)
        if not user_rec:
            raise HTTPException(status_code=401, detail="Неверный пароль Создателя")
    else:
        user_rec = authenticate_user(username, req.password)
        if not user_rec:
            raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    token = create_user_token(user_rec["id"], user_rec["username"], user_rec["role"])
    role_cfg = ROLE_CONFIGS.get(user_rec["role"], ROLE_CONFIGS["user"])
    res = JSONResponse({
        "status": "ok",
        "user_id": user_rec["username"],
        "username": user_rec["username"],
        "role": user_rec["role"],
        "role_title": role_cfg["title"],
        "role_icon": role_cfg["icon"],
        "role_badge_class": role_cfg["badge_class"],
        "is_admin": (user_rec["role"] == "admin" or user_rec["username"] == "kexiky"),
        "is_creator": (user_rec["username"] == "kexiky"),
        "token": token
    })
    res.set_cookie("mediapull_auth", token, max_age=365 * 86400, httponly=True, samesite="lax")
    return res


@app.post("/api/auth/logout")
async def auth_logout():
    res = JSONResponse({"status": "ok", "message": "Logged out"})
    res.delete_cookie("mediapull_auth")
    return res


@app.post("/api/auth/change-password")
async def auth_change_password(req: ChangePasswordRequest, request: Request):
    user = get_current_user(request)
    if user.get("role") == "guest":
        raise HTTPException(status_code=401, detail="Требуется авторизация для смены пароля")
    try:
        update_user_password(user["username"], req.old_password, req.new_password)
        return {"status": "ok", "message": "Пароль успешно изменен"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Admin Endpoints
@app.get("/api/admin/users")
async def admin_get_users(request: Request):
    user = get_current_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен. Требуются права администратора.")
    users = list_users()
    for u in users:
        usage = get_user_disk_usage(u["username"], u["role"])
        u["used_bytes"] = usage["total_bytes"]
        u["file_count"] = usage["file_count"]
        u["used_size_str"] = usage["total_size_str"]
    return {"users": users, "roles": ROLE_CONFIGS}


@app.post("/api/admin/users/{username}/role")
async def admin_set_user_role(username: str, req: UpdateRoleRequest, request: Request):
    user = get_current_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен. Требуются права администратора.")
    try:
        update_user_role(username, req.role)
        return {"status": "ok", "message": f"Роль пользователя '{username}' изменена на '{req.role}'"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/admin/users/{username}")
async def admin_delete_user(username: str, request: Request):
    user = get_current_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен. Требуются права администратора.")
    try:
        delete_user(username)
        return {"status": "ok", "message": f"Пользователь '{username}' удален"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/admin/cleanup")
async def admin_trigger_cleanup(request: Request):
    user = get_current_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен. Требуются права администратора.")
    res = cleanup_expired_files()
    return {"status": "ok", "result": res}


@app.get("/api/admin/system-stats")
async def admin_system_stats(request: Request):
    user = get_current_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен. Требуются права администратора.")
    import shutil
    total, used, free = shutil.disk_usage(str(DOWNLOADS_DIR))
    db_file = DATA_DIR / "mediapull.db"
    db_size = db_file.stat().st_size if db_file.exists() else 0
    all_users = list_users()
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


class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    title: Optional[str] = "Media Download"
    mode: str = "video"  # "video", "audio", or "photo"
    quality: str = "best"
    video_format: str = "mp4"
    audio_format: str = "mp3"
    audio_quality: str = "320"
    trim_start: Optional[str] = None
    trim_end: Optional[str] = None
    download_subtitles: bool = False
    subtitles_lang: Optional[str] = "en"
    embed_subtitles: bool = False
    embed_thumbnail: bool = True
    split_chapters: bool = False
    is_photo: bool = False
    photo_url: Optional[str] = None


class BatchItem(BaseModel):
    url: str
    title: Optional[str] = ""
    index: Optional[int] = None
    is_photo: bool = False
    photo_url: Optional[str] = None


class BatchDownloadRequest(BaseModel):
    items: List[BatchItem]
    mode: str = "video"
    quality: str = "best"
    video_format: str = "mp4"
    audio_format: str = "mp3"
    audio_quality: str = "320"
    download_subtitles: bool = False
    subtitles_lang: Optional[str] = "en"
    embed_subtitles: bool = False
    embed_thumbnail: bool = True
    playlist_title: Optional[str] = None
    playlist_thumbnail: Optional[str] = None


class BatchFilesRequest(BaseModel):
    filenames: List[str]


@app.post("/api/info")
async def get_info(req: InfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Введите корректную ссылку")

    try:
        data = await downloader_manager.get_video_info(url)
        return data
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/download")
async def start_download(req: DownloadRequest, request: Request):
    user = get_current_user(request)
    task_id = str(uuid.uuid4())[:8]
    await downloader_manager.start_download(
        task_id=task_id,
        url=req.url,
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
    res = JSONResponse({"task_id": task_id, "status": "queued", "user_id": user["user_id"]})
    if user.get("is_new_session"):
        res.set_cookie("mediapull_session", user["user_id"], max_age=30 * 86400, httponly=False, samesite="lax")
    return res


@app.post("/api/download-batch")
async def download_batch(req: BatchDownloadRequest, request: Request):
    if not req.items:
        raise HTTPException(status_code=400, detail="Список элементов пуст")

    user = get_current_user(request)
    task_ids = []
    for idx, item in enumerate(req.items, start=1):
        tid = str(uuid.uuid4())[:8]
        item_index = item.index if item.index is not None else idx
        item_mode = "photo" if item.is_photo else req.mode
        await downloader_manager.start_download(
            task_id=tid,
            url=item.url,
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

    res = JSONResponse({"queued_count": len(task_ids), "task_ids": task_ids, "user_id": user["user_id"]})
    if user.get("is_new_session"):
        res.set_cookie("mediapull_session", user["user_id"], max_age=30 * 86400, httponly=False, samesite="lax")
    return res


@app.get("/api/task/{task_id}")
async def get_task_status(task_id: str):
    if task_id not in downloader_manager.tasks:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return downloader_manager.tasks[task_id].to_dict()


@app.get("/api/progress/{task_id}")
async def stream_progress(task_id: str):
    if task_id not in downloader_manager.tasks:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    async def event_generator():
        while True:
            if task_id not in downloader_manager.tasks:
                yield 'event: error\ndata: {"error": "Задача удалена"}\n\n'
                break

            task = downloader_manager.tasks[task_id]
            data = task.to_dict()
            import json
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

            if task.status in ["completed", "error", "cancelled"]:
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.get("/api/progress-stream")
async def stream_all_progress(request: Request):
    user = get_current_user(request)
    is_creator = user.get("is_creator", False)
    uid = user.get("user_id")

    async def event_generator():
        while True:
            all_tasks = downloader_manager.get_all_tasks()
            if is_creator:
                tasks_data = all_tasks
            else:
                tasks_data = [t for t in all_tasks if t.get("user_id") in (uid, None)]

            import json
            payload = json.dumps({"tasks": tasks_data}, ensure_ascii=False)
            yield f"data: {payload}\n\n"

            has_active = any(
                t.get("status") in ["queued", "downloading", "converting"]
                for t in tasks_data
            )
            await asyncio.sleep(0.4 if has_active else 2.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.get("/api/tasks")
async def get_all_tasks(request: Request):
    user = get_current_user(request)
    all_tasks = downloader_manager.get_all_tasks()
    if user.get("is_creator"):
        return {"tasks": all_tasks}
    return {"tasks": [t for t in all_tasks if t.get("user_id") in (user.get("user_id"), None)]}


@app.post("/api/tasks/clear-finished")
async def clear_finished_tasks():
    cleared = downloader_manager.clear_finished_tasks()
    return {"status": "cleared", "count": cleared}


@app.post("/api/cancel/{task_id}")
async def cancel_task(task_id: str):
    success = downloader_manager.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return {"status": "cancelled"}


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


@app.get("/api/history")
async def get_history(request: Request):
    user = get_current_user(request)
    data = downloader_manager.list_history(user_id=user["user_id"], role=user["role"])
    res = JSONResponse({
        "files": data["files"],
        "playlists": data["playlists"],
        "count": len(data["files"]),
        "playlist_count": len(data["playlists"]),
        "user": user
    })
    if user.get("is_new_session"):
        res.set_cookie("mediapull_session", user["user_id"], max_age=30 * 86400, httponly=False, samesite="lax")
    return res


@app.api_route("/api/thumbnail/{filename:path}", methods=["GET", "HEAD"])
async def get_thumbnail(filename: str, request: Request):
    user = get_current_user(request)
    thumb_path = downloader_manager.get_or_create_thumbnail(filename, user_id=user["user_id"])
    if not thumb_path and user.get("is_creator"):
        thumb_path = downloader_manager.get_or_create_thumbnail(filename, user_id="kexiky")

    if thumb_path and thumb_path.exists() and thumb_path.stat().st_size > 0:
        return FileResponse(
            path=str(thumb_path),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"}
        )

    safe_name = os.path.basename(filename)
    ext = Path(safe_name).suffix.lower()
    audio_exts = {".mp3", ".m4a", ".wav", ".flac", ".opus", ".aac", ".ogg"}
    if ext in audio_exts:
        return Response(content=AUDIO_SVG_FALLBACK, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})
    return Response(content=VIDEO_SVG_FALLBACK, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.delete("/api/history/{filename:path}")
async def delete_history_item(filename: str, request: Request):
    import urllib.parse
    user = get_current_user(request)
    success = downloader_manager.delete_file(filename, user_id=user["user_id"])
    if not success:
        unquoted = urllib.parse.unquote(filename)
        if unquoted != filename:
            success = downloader_manager.delete_file(unquoted, user_id=user["user_id"])
    if not success:
        raise HTTPException(status_code=404, detail="Файл не найден на диске")
    return {"deleted": filename}


@app.delete("/api/playlist/{folder_name:path}")
async def delete_playlist(folder_name: str, request: Request):
    user = get_current_user(request)
    success = downloader_manager.delete_playlist(folder_name, user_id=user["user_id"])
    if not success:
        raise HTTPException(status_code=404, detail="Плейлист не найден")
    return {"deleted": folder_name}


@app.api_route("/api/playlist/zip/{folder_name:path}", methods=["GET", "HEAD"])
async def download_playlist_zip(folder_name: str, request: Request):
    user = get_current_user(request)
    folder_path = resolve_download_path(folder_name, user_id=user["user_id"])
    if not folder_path or not folder_path.exists() or not folder_path.is_dir():
        if user.get("is_creator"):
            folder_path = resolve_download_path(folder_name, user_id="kexiky")
    if not folder_path or not folder_path.exists() or not folder_path.is_dir():
        raise HTTPException(status_code=404, detail="Папка плейлиста не найдена")

    temp_dir = DOWNLOADS_DIR / ".temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    clean_title = re.sub(r'[\\/*?:"<>|]', "", folder_name).strip() or "playlist"
    zip_path = temp_dir / f"playlist_{uuid.uuid4().hex[:8]}.zip"

    import zipfile
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zipf:
        for f in sorted(folder_path.glob("*")):
            if f.is_file() and not f.name.startswith("."):
                zipf.write(f, arcname=f.name)

    if not zip_path.exists() or zip_path.stat().st_size == 0:
        raise HTTPException(status_code=400, detail="Не удалось создать архив плейлиста")

    return FileResponse(
        path=str(zip_path),
        filename=f"{clean_title}.zip",
        media_type="application/zip"
    )


@app.api_route("/api/files/{filename:path}", methods=["GET", "HEAD"])
async def download_file(filename: str, request: Request):
    user = get_current_user(request)
    target = resolve_download_path(filename, user_id=user["user_id"])
    if not target or not target.exists() or not target.is_file():
        if user.get("is_creator"):
            target = resolve_download_path(filename, user_id="kexiky")
    if not target or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type="application/octet-stream"
    )


@app.api_route("/api/stream/{filename:path}", methods=["GET", "HEAD"])
async def stream_file(filename: str, request: Request):
    user = get_current_user(request)
    target = resolve_download_path(filename, user_id=user["user_id"])
    if not target or not target.exists() or not target.is_file():
        if user.get("is_creator"):
            target = resolve_download_path(filename, user_id="kexiky")
    if not target or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    ext = target.suffix.lower()
    content_types = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".opus": "audio/ogg",
        ".gif": "image/gif",
    }
    content_type = content_types.get(ext, "application/octet-stream")

    file_size = target.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        byte_range = range_header.replace("bytes=", "").split("-")
        start = int(byte_range[0])
        end = int(byte_range[1]) if byte_range[1] else file_size - 1
        chunk_size = (end - start) + 1

        def iter_chunk():
            with open(target, "rb") as f:
                f.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    read_size = min(remaining, 64 * 1024)
                    data = f.read(read_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(chunk_size),
            "Content-Type": content_type,
        }
        return StreamingResponse(iter_chunk(), status_code=206, headers=headers)

    return FileResponse(str(target), media_type=content_type)


@app.post("/api/batch/zip")
async def batch_zip_download(req: BatchFilesRequest, request: Request):
    if not req.filenames:
        raise HTTPException(status_code=400, detail="Не выбраны файлы для архивации")

    user = get_current_user(request)
    temp_dir = DOWNLOADS_DIR / ".temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    archive_id = str(uuid.uuid4())[:8]
    zip_path = temp_dir / f"media_archive_{archive_id}.zip"

    import zipfile
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for fn in req.filenames:
            target = resolve_download_path(fn, user_id=user["user_id"])
            if not target and user.get("is_creator"):
                target = resolve_download_path(fn, user_id="kexiky")
            if target and target.exists() and target.is_file():
                zipf.write(target, arcname=target.name)

    if not zip_path.exists() or zip_path.stat().st_size == 0:
        raise HTTPException(status_code=400, detail="Не удалось создать ZIP архив")

    return FileResponse(
        path=str(zip_path),
        filename="media_downloads.zip",
        media_type="application/zip"
    )


@app.post("/api/batch/delete")
async def batch_delete_files(req: BatchFilesRequest, request: Request):
    if not req.filenames:
        raise HTTPException(status_code=400, detail="Не выбраны файлы")
    user = get_current_user(request)
    result = downloader_manager.delete_files_batch(req.filenames, user_id=user["user_id"])
    return result


@app.post("/api/reveal/{filename:path}")
async def reveal_file(filename: str):
    success = downloader_manager.reveal_in_finder(filename)
    if not success:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return {"status": "revealed", "filename": filename}


class OpenFolderRequest(BaseModel):
    subfolder: Optional[str] = None

@app.post("/api/open-folder")
async def open_folder(req: Optional[OpenFolderRequest] = None):
    sub = req.subfolder if req else None
    success = downloader_manager.open_downloads_folder(sub)
    return {"status": "opened" if success else "failed"}


@app.get("/api/subtitles/{filename:path}")
async def get_subtitles(filename: str):
    subs = downloader_manager.get_file_subtitles(filename)
    return {"subtitles": subs}


@app.get("/api/subtitles-track")
async def get_subtitle_vtt_param(filename: str, track_id: str):
    return await get_subtitle_vtt(filename=filename, track_id=track_id)


@app.get("/api/subtitles-track/{filename}/{track_id:path}")
async def get_subtitle_vtt(filename: str, track_id: str):
    raw_track_id = track_id
    if raw_track_id.endswith(".vtt") and not raw_track_id.endswith(".vtt.vtt"):
        raw_track_id = raw_track_id[:-4]

    vtt_content = downloader_manager.extract_subtitle_vtt(filename, raw_track_id)
    if not vtt_content:
        vtt_content = downloader_manager.extract_subtitle_vtt(filename, track_id)

    if not vtt_content:
        raise HTTPException(status_code=404, detail="Субтитры не найдены")

    return Response(content=vtt_content, media_type="text/vtt; charset=utf-8")


# Mount static frontend
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
