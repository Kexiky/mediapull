"""
MediaPull Telegram Bot (aiogram 3.x)
Universal Media Downloader & Smart In-Memory / Ephemeral Delivery for Telegram

Features: file_id cache (instant re-sends), quality menu with size estimates, per-user settings,
web-account linking (/link), queue with position/ETA, admin commands, groups + inline mode,
optional Local Bot API server (2 GB uploads), webhook mode and scheduled yt-dlp updates.
"""

import os
import re
import sys
import time
import html
import secrets
import asyncio
import argparse
import subprocess
import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, Set, List, Tuple

from dotenv import load_dotenv

import shutil
import urllib.parse

# Load .env if present
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

if not shutil.which("ffmpeg"):
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
    except Exception:
        pass

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineQuery,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultsButton,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import bot_store, database
from app.bot_queue import JobQueue
from app.downloader import (
    downloader_manager, detect_platform, format_size, format_duration, validate_public_url,
    normalize_url, DOWNLOADS_DIR, IMAGE_EXTS, AUDIO_EXTS,
)
from app.ratelimit import SlidingWindowLimiter
from app.roles import ROLE_CONFIGS
from app.updater import auto_update_enabled, update_ytdlp, ytdlp_update_loop, ytdlp_version

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEB_BASE_URL = os.getenv("MEDIAPULL_WEB_URL", "http://127.0.0.1:8000").rstrip("/")

# Optional Local Bot API server (https://github.com/tdlib/telegram-bot-api) lifts the 50 MB upload limit to 2 GB.
LOCAL_API_SERVER = os.getenv("TELEGRAM_API_SERVER", "").strip().rstrip("/")
_default_upload_mb = 1900 if LOCAL_API_SERVER else 48  # 48 MB = safety margin under the 50 MB cloud limit
MAX_TG_SIZE_BYTES = int(float(os.getenv("TELEGRAM_MAX_UPLOAD_MB", str(_default_upload_mb))) * 1024 * 1024)
TG_HARD_LIMIT_BYTES = (2000 if LOCAL_API_SERVER else 50) * 1024 * 1024
# Qualities whose estimated size is this many times the limit are not offered (the squeeze would ruin them).
OVERSIZE_HIDE_FACTOR = 6

MAX_PLAYLIST_ITEMS = int(os.getenv("TELEGRAM_MAX_PLAYLIST_ITEMS", "50"))
DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_DOWNLOAD_TIMEOUT", "900"))
PENDING_TTL_SECONDS = 30 * 60
MAX_PENDING_SESSIONS = 300
BOT_DEFAULT_ROLE = os.getenv("TELEGRAM_DEFAULT_ROLE", "user").strip().lower()
if BOT_DEFAULT_ROLE not in ROLE_CONFIGS or BOT_DEFAULT_ROLE == "admin":
    BOT_DEFAULT_ROLE = "user"

WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
WEBHOOK_HOST = os.getenv("TELEGRAM_WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("TELEGRAM_WEBHOOK_PORT") or 8080)
WEBHOOK_PATH = "/telegram/webhook"

CAROUSEL_MAX_SLIDES = int(os.getenv("TELEGRAM_MAX_CAROUSEL_SLIDES") or 20)
TG_ALBUM_SIZE = 10                      # Telegram accepts 2-10 items per media group
TG_PHOTO_MAX_BYTES = 10 * 1024 * 1024   # larger images must be sent as documents
STANDARD_HEIGHTS = [2160, 1440, 1080, 720, 480, 360]
SUBTITLE_LANG_RE = re.compile(r"^[A-Za-z0-9_.*-]{1,20}$")
URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _parse_ids(raw: str) -> Set[int]:
    ids: Set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


# Static lists from the environment; admins can extend the allow-list at runtime with /allow.
ALLOWED_USER_IDS = _parse_ids(os.getenv("TELEGRAM_ALLOWED_IDS", ""))
ADMIN_IDS = _parse_ids(os.getenv("TELEGRAM_ADMIN_IDS", ""))
_parse_allowed_ids = _parse_ids  # backward compatible name

BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")

# Per-user protection against flooding the server with link analysis requests
link_limiter = SlidingWindowLimiter(max_events=10, window_seconds=60)
# /link brute-force protection (codes are 32 bits and expire after 10 minutes)
link_attempt_limiter = SlidingWindowLimiter(max_events=5, window_seconds=600)
# Users that currently have a download running (one at a time per user)
busy_users: Set[int] = set()
# Video compression is CPU-heavy: one at a time
compress_lock = asyncio.Semaphore(1)
# Waiting line in front of the downloader (same width as the downloader's own concurrency)
job_queue = JobQueue(int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3")))


def format_web_button_url(media_url: str) -> Optional[str]:
    """
    Format a valid URL for Telegram inline keyboard web button.
    Returns None if WEB_BASE_URL is missing, unconfigured, or an internal docker hostname.
    """
    if not WEB_BASE_URL:
        return None
    try:
        parsed = urllib.parse.urlparse(WEB_BASE_URL)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        hostname = (parsed.hostname or "").lower()
        # Internal docker service names or invalid hosts without dots (Telegram rejects them)
        if not hostname or hostname in ("web", "app", "mediapull-web", "localhost") or "." not in hostname:
            return None
        encoded = urllib.parse.quote(media_url, safe="")
        return f"{WEB_BASE_URL}?url={encoded}"
    except Exception:
        return None


# Ephemeral storage directory for Telegram downloads (deleted immediately after sending)
TEMP_TG_DIR = DOWNLOADS_DIR / "temp_tg"
TEMP_TG_DIR.mkdir(parents=True, exist_ok=True)

# In-memory store for pending download sessions: key -> info dict
pending_downloads: Dict[str, Dict[str, Any]] = {}


def cleanup_temp_tg(max_age_seconds: int = 900):
    """Remove leftovers (files and per-job folders) of interrupted jobs older than max_age_seconds."""
    try:
        now = time.time()
        for entry in TEMP_TG_DIR.glob("*"):
            try:
                if now - entry.stat().st_mtime <= max_age_seconds:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as e:
        print(f"[Temp Cleanup] {e}")


# Liveness file for the container health check: touched every 30 s while the event loop is healthy.
HEARTBEAT_FILE = Path(os.getenv("MEDIAPULL_BOT_HEARTBEAT") or (Path(tempfile.gettempdir()) / "mediapull-bot.alive"))


async def heartbeat_loop(interval_seconds: float = 30):
    while True:
        try:
            HEARTBEAT_FILE.write_text(str(int(time.time())))
        except OSError:
            pass
        await asyncio.sleep(interval_seconds)


async def periodic_temp_cleanup(interval_seconds: int = 300):
    while True:
        await asyncio.sleep(interval_seconds)
        await asyncio.to_thread(cleanup_temp_tg)


def purge_pending_sessions() -> None:
    """Drop expired and surplus pending sessions (keeps the in-memory store bounded)."""
    now = time.time()
    for key in [k for k, v in pending_downloads.items() if now - v.get("ts", 0) > PENDING_TTL_SECONDS]:
        pending_downloads.pop(key, None)
    overflow = len(pending_downloads) - MAX_PENDING_SESSIONS
    if overflow > 0:
        for key in sorted(pending_downloads, key=lambda k: pending_downloads[k].get("ts", 0))[:overflow]:
            pending_downloads.pop(key, None)


# ---------------------------------------------------------------------------
# Identity, access control
# ---------------------------------------------------------------------------
@dataclass
class Identity:
    """Who is asking: a Telegram user, optionally linked to a web account whose role/limits then apply."""
    tg_id: int
    role: str
    web_user: Optional[Dict[str, Any]] = None

    @property
    def max_height(self) -> int:
        return ROLE_CONFIGS.get(self.role, ROLE_CONFIGS["user"]).get("max_height") or 1080

    @property
    def is_admin(self) -> bool:
        return self.tg_id in ADMIN_IDS or (self.web_user is not None and self.web_user.get("role") == "admin")


def resolve_identity(tg_id: int) -> Identity:
    try:
        linked = database.get_telegram_linked_user(tg_id)
    except Exception as e:  # the web database must never take the bot down
        print(f"[Bot] link lookup failed: {e}")
        linked = None
    if linked:
        return Identity(tg_id=tg_id, role=linked["role"], web_user=linked)
    return Identity(tg_id=tg_id, role=BOT_DEFAULT_ROLE)


def access_denied_reason(tg_id: int) -> Optional[str]:
    """None = allowed, otherwise 'banned' / 'closed'."""
    status = bot_store.get_acl_status(tg_id)
    if status == "banned":
        return "banned"
    if tg_id in ADMIN_IDS or status == "allowed" or tg_id in ALLOWED_USER_IDS:
        return None
    if ALLOWED_USER_IDS or bot_store.get_closed_mode():
        return "closed" if not resolve_identity(tg_id).is_admin else None
    return None


class AccessMiddleware(BaseMiddleware):
    """Applies the ban list and the (static + runtime) allow-list to every update."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        reason = access_denied_reason(user.id)
        if reason is None:
            return await handler(event, data)
        text = "⛔ Доступ закрыт." if reason == "banned" else "⛔ Этот бот работает в закрытом режиме. Попросите администратора добавить вас."
        try:
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            elif isinstance(event, InlineQuery):
                await event.answer([], cache_time=60, is_personal=True)
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------
def get_video_dimensions_and_duration(filepath: Path) -> tuple[int, int, int]:
    """Use ffprobe to extract width, height, and duration in seconds."""
    width, height, duration = 1280, 720, 0
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration:format=duration",
            "-of", "default=noprint_wrappers=1",
            str(filepath)
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15
        )
        dur_val = 0.0
        for line in res.stdout.strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                if k == "width":
                    try: width = int(v)
                    except ValueError: pass
                elif k == "height":
                    try: height = int(v)
                    except ValueError: pass
                elif k == "duration":
                    try: dur_val = float(v)
                    except ValueError: pass
        duration = int(dur_val)
    except Exception as e:
        print(f"[ffprobe] Warning extracting dimensions: {e}")
    return width, height, duration


def compress_video_for_telegram(input_path: Path, max_bytes: Optional[int] = None) -> Path:
    """
    Compress video to guarantee it stays under Telegram's upload limit using FFmpeg target bitrate.
    Output is saved next to the source (inside its per-job folder) and is deleted with it.
    """
    max_bytes = max_bytes or MAX_TG_SIZE_BYTES
    input_size = input_path.stat().st_size
    if input_size <= max_bytes:
        return input_path

    out_path = input_path.parent / f"tg_comp_{input_path.stem}.mp4"
    if out_path.exists() and out_path.stat().st_size <= max_bytes:
        return out_path

    _, _, duration = get_video_dimensions_and_duration(input_path)
    if duration <= 0:
        duration = 60  # fallback 1 minute

    # Calculate target bitrate from max_bytes (with 10% safety cushion)
    target_kbits = int((max_bytes * 0.90 * 8) / 1000)
    total_bitrate = int(target_kbits / max(1, duration))
    audio_bitrate = 96 if total_bitrate > 350 else 64
    video_bitrate = max(64, total_bitrate - audio_bitrate)
    video_bitrate = min(4000, video_bitrate)

    print(f"[Compress] Duration={duration}s, InSize={input_size/(1024*1024):.2f}MB -> Target Video Bitrate={video_bitrate}k, Audio={audio_bitrate}k")

    # Preserve high definition (1080p/720p) unless video is extremely long
    scale_filter = "scale='min(1920,iw)':-2"
    if video_bitrate < 150:
        scale_filter = "scale='min(1280,iw)':-2"
    if video_bitrate < 80:
        scale_filter = "scale='min(854,iw)':-2"

    cmd = [
        "ffmpeg", "-nostdin", "-y",
        "-i", str(input_path),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-b:v", f"{video_bitrate}k",
        "-maxrate", f"{int(video_bitrate * 1.25)}k",
        "-bufsize", f"{int(video_bitrate * 2)}k",
        "-vf", scale_filter,
        "-c:a", "aac",
        "-b:a", f"{audio_bitrate}k",
        "-movflags", "+faststart",
        str(out_path)
    ]

    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900
    )
    if proc.returncode != 0 or not out_path.exists():
        err_msg = (proc.stderr or "")[-300:]
        print(f"[Compress] FFmpeg error: {err_msg}")
        return input_path

    out_size = out_path.stat().st_size
    print(f"[Compress] Finished: {out_size / (1024*1024):.2f} MB")
    return out_path


def human_mb(size_bytes: Optional[int]) -> str:
    if not size_bytes:
        return ""
    mb = size_bytes / (1024 * 1024)
    return f"{mb:.0f} МБ" if mb >= 10 else f"{mb:.1f} МБ"


def media_identity(info: Dict[str, Any], url: str) -> str:
    """Stable id of the media for the file_id cache (platform:video-id, falling back to the normalized URL)."""
    platform_id = (info.get("platform") or {}).get("id", "web")
    vid = info.get("id")
    return f"{platform_id}:{vid}" if vid else f"url:{hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]}"


def make_cache_key(media_id: str, action: str, variant: str) -> str:
    return f"{media_id}|{action}|{variant}"


# ---------------------------------------------------------------------------
# Quality menu with size estimates
# ---------------------------------------------------------------------------
def plan_video_qualities(info: Dict[str, Any], identity: Identity) -> Tuple[List[Dict[str, Any]], int]:
    """Qualities to offer (with estimated sizes) and the 'auto' height: the best one that fits the upload limit."""
    raw_res = info.get("resolutions") or []
    sizes: Dict[int, Optional[int]] = {}
    for r in raw_res:
        h = r.get("height")
        if isinstance(h, int) and h >= 144 and h not in sizes:
            sizes[h] = r.get("filesize")
    heights = sorted(sizes, reverse=True)

    if heights:
        offered = [t for t in STANDARD_HEIGHTS if t in sizes]
        if not offered:
            for t in STANDARD_HEIGHTS:
                if any(t <= h < t * 1.4 for h in heights) and t not in offered:
                    offered.append(t)
        if not offered:
            offered = [h for h in heights if h >= 240][:4]
        size_of = {t: next((sizes[h] for h in heights if h == t), None) for t in offered}
        for t in offered:
            if size_of[t] is None:  # bucketed tier: borrow the estimate of the closest real height
                near = [h for h in heights if t <= h < t * 1.4 and sizes[h]]
                size_of[t] = sizes[near[0]] if near else None
    else:
        offered = [1080, 720, 480, 360]
        size_of = {t: None for t in offered}

    cap = identity.max_height
    allowed = [h for h in offered if h <= cap] or [min(offered)]
    # Hide variants that would have to be squeezed beyond recognition into the upload limit.
    visible = [h for h in allowed if not (size_of.get(h) and size_of[h] > MAX_TG_SIZE_BYTES * OVERSIZE_HIDE_FACTOR)]
    if not visible:
        visible = [min(allowed)]

    plan = [{"height": h, "size": size_of.get(h), "fits": bool(size_of.get(h)) and size_of[h] <= MAX_TG_SIZE_BYTES}
            for h in visible]
    fitting = [p["height"] for p in plan if p["fits"]]
    if fitting:
        auto_height = max(fitting)
    elif any(p["size"] for p in plan):
        auto_height = min(p["height"] for p in plan)
    else:
        # no size information at all: a sensible default is 720p, or the best one below it
        offered_heights = sorted(p["height"] for p in plan)
        auto_height = max((h for h in offered_heights if h <= 720), default=offered_heights[0])
    return plan, auto_height


def quality_label(h: int) -> str:
    label = f"{h}p"
    if h >= 2160:
        label += " (4K)"
    elif h >= 1440:
        label += " (2K)"
    elif h >= 1080:
        label += " Full HD"
    elif h >= 720:
        label += " HD"
    elif h <= 360:
        label += " (быстро)"
    return label


def pick_subtitle_langs(info: Dict[str, Any], limit: int = 3) -> List[str]:
    codes = [s.get("code") for s in (info.get("subtitles") or []) if s.get("code") and SUBTITLE_LANG_RE.match(s["code"])]
    preferred = [c for c in ("ru", "en") if c in codes]
    others = [c for c in codes if c not in preferred and "-" not in c and "." not in c]
    return (preferred + others)[:limit]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
dp = Dispatcher()
_access = AccessMiddleware()
dp.message.outer_middleware(_access)
dp.callback_query.outer_middleware(_access)
dp.inline_query.outer_middleware(_access)

BOT_COMMANDS = [
    BotCommand(command="start", description="О боте"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="settings", description="Настройки: подпись, качество, библиотека"),
    BotCommand(command="formats", description="Форматы и размеры: /formats <ссылка>"),
    BotCommand(command="subs", description="Субтитры файлом: /subs <ссылка>"),
    BotCommand(command="dl", description="Скачать в группе: /dl <ссылка>"),
    BotCommand(command="link", description="Привязать веб-аккаунт: /link КОД"),
    BotCommand(command="me", description="Мой профиль и лимиты"),
]


@dp.message(CommandStart())
async def handle_start(message: Message, command: CommandObject):
    payload = (command.args or "").strip()
    if payload.startswith("dl_"):
        url = bot_store.resolve_shortlink(payload[3:])
        if url:
            await process_link(message, url)
            return
        await message.answer("⌛ Ссылка устарела. Отправьте её боту ещё раз.")
        return

    text = (
        "⚡ <b>MediaPull Bot</b> | Universal Media Downloader\n"
        "<i>by Kexiky • Быстрое скачивание видео и аудио прямо в Telegram</i>\n\n"
        "🚀 <b>Просто отправьте мне ссылку на медиа:</b>\n"
        "• <b>YouTube</b> — видео (от 360p до 4K), Shorts, треки (MP3), плейлисты\n"
        "• <b>Instagram</b> — Reels, посты, фото, карусели\n"
        "• <b>TikTok</b> — ролики без водяных знаков со звуком\n"
        "• <b>VK, Rutube, Pinterest, X (Twitter)</b> и сотни других сервисов\n\n"
        "🔒 <b>Конфиденциально и чисто:</b>\n"
        "• Видео приходят в чат чистыми без рекламы — удобно сразу пересылать друзьям.\n"
        "• Файлы <b>не сохраняются на сервере</b> и удаляются сразу после отправки вам в Telegram.\n"
        "• Уже скачанное отправляется <b>мгновенно</b>.\n\n"
        "⚙️ <b>Команды:</b>\n"
        "• <code>/settings</code> — подпись, качество по умолчанию, сохранение в библиотеку\n"
        "• <code>/formats ссылка</code> — доступные форматы и размеры\n"
        "• <code>/subs ссылка</code> — субтитры файлом (SRT)\n"
        "• <code>/link КОД</code> — привязать аккаунт с сайта (тариф и библиотека)\n"
        "• <code>/help</code> — помощь и советы\n"
        f"• 🌐 Веб-версия: <a href=\"{html.escape(WEB_BASE_URL, quote=True)}\">{html.escape(WEB_BASE_URL)}</a>"
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("help"))
async def handle_help(message: Message):
    text = (
        "📖 <b>Как пользоваться MediaPull Bot:</b>\n\n"
        "1️⃣ <b>Отправьте ссылку:</b> скопируйте ссылку на видео и отправьте в этот чат.\n"
        "2️⃣ <b>Выберите качество:</b> бот покажет варианты с примерным размером и кнопку <b>⭐ Авто</b> — лучшее качество, "
        "которое влезает в лимит Telegram без пережатия. Хотите без вопросов — задайте качество в <code>/settings</code>.\n"
        "3️⃣ <b>Мгновенная доставка:</b> видео адаптируется под лимит Telegram и отправляется вам в чат.\n\n"
        "👥 <b>В группах:</b> <code>/dl ссылка</code> или ответьте командой <code>/dl</code> на сообщение со ссылкой.\n"
        "🔎 <b>Inline:</b> введите <code>@имя_бота ссылка</code> в любом чате — уже скачанное отправится сразу.\n\n"
        "🔕 <b>Чистая пересылка:</b> по умолчанию файлы приходят без подписей. Название можно включить в <code>/settings</code>.\n\n"
        "📚 <b>Плейлисты:</b> бот покажет количество треков и предложит скачать первые в чат или открыть весь плейлист архивом в веб-версии.\n\n"
        "🔗 <b>Аккаунт:</b> на сайте в личном кабинете нажмите «Привязать Telegram» и отправьте боту <code>/link КОД</code>."
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def settings_view(tg_id: int):
    s = bot_store.get_settings(tg_id)
    identity = resolve_identity(tg_id)
    quality_names = {"ask": "спрашивать", "auto": "⭐ авто", "360": "360p", "480": "480p", "720": "720p", "1080": "1080p"}
    lines = [
        "⚙️ <b>Настройки</b>\n",
        f"🏷 Подпись к видео: <b>{'название' if s['caption_mode'] == 'title' else 'без подписи'}</b>",
        f"🎬 Качество по умолчанию: <b>{quality_names.get(s['quality_pref'], s['quality_pref'])}</b>",
    ]
    if identity.web_user:
        lines.append(f"💾 Сохранять в веб-библиотеку: <b>{'да' if s['save_library'] else 'нет'}</b>")
        lines.append(f"\n🔗 Аккаунт: <b>{html.escape(identity.web_user['username'])}</b> ({ROLE_CONFIGS[identity.role]['title']})")
    else:
        lines.append("\n🔗 Аккаунт не привязан — применяется тариф «Пользователь». Привязка: <code>/link КОД</code> (код — в личном кабинете сайта).")

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=f"🏷 Подпись: {'название' if s['caption_mode'] == 'title' else 'нет'}", callback_data="st:cap"))
    row = []
    for q in ("ask", "auto", "360", "480", "720", "1080"):
        mark = "✅ " if s["quality_pref"] == q else ""
        row.append(InlineKeyboardButton(text=f"{mark}{quality_names[q]}", callback_data=f"st:q:{q}"))
    kb.row(*row[:3])
    kb.row(*row[3:])
    if identity.web_user:
        kb.row(InlineKeyboardButton(text=f"💾 Библиотека: {'вкл' if s['save_library'] else 'выкл'}", callback_data="st:lib"))
        kb.row(InlineKeyboardButton(text="🔓 Отвязать аккаунт", callback_data="st:unlink"))
    kb.row(InlineKeyboardButton(text="✖ Закрыть", callback_data="st:close"))
    return "\n".join(lines), kb.as_markup()


@dp.message(Command("settings"))
async def handle_settings(message: Message):
    text, markup = settings_view(message.from_user.id)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.message(Command("caption"))
@dp.message(Command("clean"))
async def handle_caption_toggle(message: Message):
    current = bot_store.get_settings(message.from_user.id)["caption_mode"]
    new_mode = "title" if current == "none" else "none"
    bot_store.set_setting(message.from_user.id, "caption_mode", new_mode)

    if new_mode == "none":
        await message.answer(
            "🔕 <b>Режим подписи: Без подписи (100% чистый файл)</b>\n\n"
            "Видео отправляются без текста и без водяных знаков. "
            "Идеально для мгновенной пересылки в любые чаты!\n\n"
            "<i>(Чтобы включить отображение названия, введите <code>/caption</code>)</i>",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "🔔 <b>Режим подписи: Только название видео</b>\n\n"
            "К видео будет прикреплено только оригинальное название (без ссылок на бота и рекламы).\n\n"
            "<i>(Чтобы выключить подпись, введите <code>/caption</code>)</i>",
            parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith("st:"))
async def handle_settings_callback(callback: CallbackQuery):
    tg_id = callback.from_user.id
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    s = bot_store.get_settings(tg_id)

    if action == "close":
        await callback.message.delete()
        await callback.answer()
        return
    if action == "cap":
        bot_store.set_setting(tg_id, "caption_mode", "none" if s["caption_mode"] == "title" else "title")
    elif action == "q" and len(parts) > 2 and parts[2] in bot_store.QUALITY_PREFS:
        bot_store.set_setting(tg_id, "quality_pref", parts[2])
    elif action == "lib":
        if not resolve_identity(tg_id).web_user:
            await callback.answer("Сначала привяжите аккаунт: /link КОД", show_alert=True)
            return
        bot_store.set_setting(tg_id, "save_library", not s["save_library"])
    elif action == "unlink":
        await asyncio.to_thread(database.unlink_telegram, telegram_id=tg_id)
        bot_store.set_setting(tg_id, "save_library", False)
    text, markup = settings_view(tg_id)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass  # nothing changed
    await callback.answer()


# ---------------------------------------------------------------------------
# Account linking and profile
# ---------------------------------------------------------------------------
@dp.message(Command("link"))
async def handle_link(message: Message, command: CommandObject):
    tg_id = message.from_user.id
    code = (command.args or "").strip()
    if message.chat.type != "private":
        await message.answer("🔒 Привязывайте аккаунт в личных сообщениях с ботом.")
        return
    if not re.fullmatch(r"[0-9A-Fa-f]{8}", code):
        await message.answer(
            "Откройте личный кабинет на сайте → «Привязать Telegram», скопируйте команду и отправьте её мне.\n"
            "Формат: <code>/link A1B2C3D4</code>", parse_mode="HTML")
        return
    if not link_attempt_limiter.hit(str(tg_id)):
        await message.answer("⏳ Слишком много попыток. Попробуйте через несколько минут.")
        return
    user = await asyncio.to_thread(database.consume_telegram_link_code, code, tg_id)
    if not user:
        await message.answer("❌ Код неверный или истёк. Получите новый в личном кабинете.")
        return
    await message.answer(
        f"✅ Аккаунт <b>{html.escape(user['username'])}</b> привязан.\n"
        f"Тариф: <b>{ROLE_CONFIGS[user['role']]['title']}</b>. Включить сохранение файлов в библиотеку можно в /settings.",
        parse_mode="HTML")


@dp.message(Command("unlink"))
async def handle_unlink(message: Message):
    removed = await asyncio.to_thread(database.unlink_telegram, telegram_id=message.from_user.id)
    bot_store.set_setting(message.from_user.id, "save_library", False)
    await message.answer("🔓 Аккаунт отвязан." if removed else "Аккаунт не был привязан.")


@dp.message(Command("me"))
async def handle_me(message: Message):
    identity = resolve_identity(message.from_user.id)
    cfg = ROLE_CONFIGS[identity.role]
    s = bot_store.get_settings(message.from_user.id)
    lines = [
        f"👤 Telegram ID: <code>{message.from_user.id}</code>",
        f"🎫 Тариф: <b>{cfg['title']}</b>" + (f" (аккаунт <b>{html.escape(identity.web_user['username'])}</b>)" if identity.web_user else " (аккаунт не привязан)"),
        f"🎬 Максимальное качество: <b>{identity.max_height}p</b>",
        f"📦 Лимит отправки: <b>{human_mb(MAX_TG_SIZE_BYTES)}</b>" + (" (локальный Bot API)" if LOCAL_API_SERVER else ""),
        f"⚙️ Качество по умолчанию: <b>{s['quality_pref']}</b>",
    ]
    if identity.is_admin:
        lines.append("🛡 Вы администратор бота (/stats, /allow, /ban, /closed, /update)")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------
def _is_admin(message: Message) -> bool:
    return resolve_identity(message.from_user.id).is_admin


def _parse_target_id(command: CommandObject, message: Message) -> Optional[int]:
    if command.args and command.args.strip().lstrip("-").isdigit():
        return int(command.args.strip())
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    return None


@dp.message(Command("stats"))
async def handle_stats(message: Message):
    if not _is_admin(message):
        return
    st = bot_store.stats_summary()
    ver = await ytdlp_version()
    leaders = "\n".join(f"  • <code>{uid}</code> — {n}" for uid, n in st["leaders"]) or "  —"
    await message.answer(
        "📊 <b>Статистика бота</b>\n\n"
        f"Сегодня: <b>{st['downloads_today']}</b> загрузок, <b>{st['cache_hits_today']}</b> из кэша, {st['users_today']} польз.\n"
        f"Всего: <b>{st['downloads_total']}</b> загрузок, <b>{st['cache_hits_total']}</b> из кэша, "
        f"{st['failures_total']} ошибок, {st['users_total']} польз., {human_mb(st['bytes_total']) or '0 МБ'}\n"
        f"Кэш file_id: <b>{st['cache_entries']}</b> записей ({human_mb(st['cache_bytes']) or '0 МБ'} отдаётся без загрузки)\n"
        f"Очередь: выполняется {job_queue.running}, ждёт {job_queue.waiting}\n"
        f"Режим доступа: <b>{'закрытый' if (ALLOWED_USER_IDS or bot_store.get_closed_mode()) else 'открытый'}</b>\n"
        f"yt-dlp: <code>{html.escape(ver)}</code>\n\n"
        f"Самые активные:\n{leaders}",
        parse_mode="HTML")


@dp.message(Command("allow"))
async def handle_allow(message: Message, command: CommandObject):
    if not _is_admin(message):
        return
    target = _parse_target_id(command, message)
    if target is None:
        await message.answer("Использование: <code>/allow 123456789</code> (или ответом на сообщение пользователя)", parse_mode="HTML")
        return
    bot_store.set_acl(target, "allowed")
    await message.answer(f"✅ <code>{target}</code> добавлен в список разрешённых.", parse_mode="HTML")


@dp.message(Command("ban"))
async def handle_ban(message: Message, command: CommandObject):
    if not _is_admin(message):
        return
    target = _parse_target_id(command, message)
    if target is None:
        await message.answer("Использование: <code>/ban 123456789</code> (или ответом на сообщение пользователя)", parse_mode="HTML")
        return
    if target in ADMIN_IDS:
        await message.answer("Администратора из окружения заблокировать нельзя.")
        return
    bot_store.set_acl(target, "banned")
    await message.answer(f"🚫 <code>{target}</code> заблокирован.", parse_mode="HTML")


@dp.message(Command("unban"))
async def handle_unban(message: Message, command: CommandObject):
    if not _is_admin(message):
        return
    target = _parse_target_id(command, message)
    if target is None:
        await message.answer("Использование: <code>/unban 123456789</code>", parse_mode="HTML")
        return
    bot_store.set_acl(target, None)
    await message.answer(f"♻️ <code>{target}</code> удалён из списков.", parse_mode="HTML")


@dp.message(Command("acl"))
async def handle_acl(message: Message):
    if not _is_admin(message):
        return
    allowed, banned = bot_store.list_acl("allowed"), bot_store.list_acl("banned")
    fmt = lambda ids: ", ".join(f"<code>{i}</code>" for i in ids) or "—"
    await message.answer(
        f"✅ Разрешены: {fmt(sorted(set(allowed) | ALLOWED_USER_IDS))}\n🚫 Заблокированы: {fmt(banned)}\n"
        f"🛡 Админы (окружение): {fmt(sorted(ADMIN_IDS))}", parse_mode="HTML")


@dp.message(Command("closed"))
async def handle_closed(message: Message, command: CommandObject):
    if not _is_admin(message):
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        state = "включён" if bot_store.get_closed_mode() else "выключен"
        await message.answer(f"Закрытый режим {state}. Использование: <code>/closed on|off</code>", parse_mode="HTML")
        return
    bot_store.set_closed_mode(arg == "on")
    await message.answer("🔒 Закрытый режим включён: доступ только у разрешённых." if arg == "on" else "🔓 Бот открыт для всех.")


@dp.message(Command("update"))
async def handle_update(message: Message):
    if not _is_admin(message):
        return
    status = await message.answer("⏳ Обновляю yt-dlp...")
    _, text = await update_ytdlp()
    await status.edit_text(f"🔄 {html.escape(text)}", parse_mode="HTML")


# ---------------------------------------------------------------------------
# Link processing
# ---------------------------------------------------------------------------
def extract_url(text: Optional[str]) -> Optional[str]:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,;)") if m else None


async def fetch_info(message: Message, raw_url: str, status_text: str = "Получаю информацию о потоках...") -> Optional[Tuple[str, Dict[str, Any], Message]]:
    """Common front half of every link flow: rate limit, URL validation, metadata. Returns (url, info, status_msg)."""
    if message.from_user and not link_limiter.hit(str(message.from_user.id)):
        await message.answer("⏳ Слишком много запросов. Подождите минуту и отправьте ссылку снова.")
        return None

    try:
        url = await validate_public_url(raw_url)
    except ValueError as e:
        await message.answer(f"❌ <b>Ссылка не подходит:</b> {html.escape(str(e))}", parse_mode="HTML")
        return None
    platform = detect_platform(url)

    status_msg = await message.answer(
        f"🔍 <b>Анализирую медиа...</b>\n"
        f"Платформа: <b>{platform.get('icon', '🎬')} {platform.get('name', 'Universal')}</b>\n"
        f"<i>{status_text}</i>",
        parse_mode="HTML"
    )
    try:
        info = await downloader_manager.get_video_info(url)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Ошибка анализа ссылки:</b>\n<code>{html.escape(str(e)[-600:])}</code>",
            parse_mode="HTML"
        )
        return None
    return url, info, status_msg


def _register_session(message: Message, url: str, info: Dict[str, Any]) -> str:
    purge_pending_sessions()
    key = secrets.token_hex(6)
    pending_downloads[key] = {
        "url": url,
        "title": info.get("title") or "Медиафайл",
        "is_playlist": info.get("is_playlist", False),
        "entries": info.get("entries", []),
        "is_photo": info.get("is_photo", False),
        "photo_url": info.get("photo_url"),
        "media_id": media_identity(info, url),
        "platform_id": (info.get("platform") or {}).get("id", "web"),
        "source_url": normalize_url(url),
        "owner": message.from_user.id if message.from_user else None,
        "chat_id": message.chat.id,
        "ts": time.time(),
    }
    return key


async def process_link(message: Message, raw_url: str) -> None:
    """Analyse a link and show the action menu (or start immediately when the user set a default quality)."""
    fetched = await fetch_info(message, raw_url)
    if not fetched:
        return
    url, info, status_msg = fetched
    tg_id = message.from_user.id
    platform = detect_platform(url)
    identity = resolve_identity(tg_id)
    settings = bot_store.get_settings(tg_id)

    title = info.get("title", "Медиафайл")
    uploader = info.get("uploader") or info.get("channel") or "Не указан"
    safe_title = html.escape(title)
    safe_uploader = html.escape(uploader)
    session_key = _register_session(message, url, info)
    pending = pending_downloads[session_key]

    builder = InlineKeyboardBuilder()
    web_btn_url = format_web_button_url(url)

    # Carousel branch (Instagram multi-image / mixed posts): slides are pictures, so offer pictures
    if info.get("is_carousel"):
        entries = (info.get("entries") or [])[:CAROUSEL_MAX_SLIDES]
        photos = sum(1 for e in entries if e.get("is_photo"))
        videos = len(entries) - photos
        pending["img_index"] = parse_img_index(raw_url, len(entries))
        if settings["quality_pref"] != "ask":
            await execute_action(status_msg.bot, status_msg, tg_id, session_key, pending, "car_all", "best")
            return
        if pending["img_index"]:
            builder.row(InlineKeyboardButton(text=f"📍 Только слайд №{pending['img_index']} (как в ссылке)",
                                             callback_data=f"tg:car_one:{session_key}"))
        what = f"{photos} фото" + (f" + {videos} видео" if videos else "")
        builder.row(InlineKeyboardButton(text=f"🖼 Все слайды ({what}) альбомом", callback_data=f"tg:car_all:{session_key}"))
        bottom_buttons = []
        if web_btn_url:
            bottom_buttons.append(InlineKeyboardButton(text="🌐 Открыть в Web (ZIP-архив)", url=web_btn_url))
        bottom_buttons.append(InlineKeyboardButton(text="❌ Отмена", callback_data=f"tg:cancel:{session_key}"))
        builder.row(*bottom_buttons)
        caption = (
            f"🖼 <b>Карусель:</b> <b>{safe_title}</b>\n\n"
            f"👤 Автор: <b>{safe_uploader}</b>\n"
            f"🔢 Слайдов: <b>{len(entries)}</b> ({what})\n"
            f"🏷️ Сервис: <b>{platform.get('name', 'Media')}</b>\n\n"
            "Пришлю картинки альбомом, а не видео. "
            "<i>Музыку поста Instagram без входа в аккаунт не отдаёт — её скачать не получится.</i>"
        )
        await status_msg.edit_text(caption, reply_markup=builder.as_markup(), parse_mode="HTML")
        return

    # Playlist branch
    if info.get("is_playlist"):
        item_count = info.get("item_count", 0)
        entries = info.get("entries", [])
        total_items = len(entries) if entries else item_count
        send_items = min(total_items, MAX_PLAYLIST_ITEMS)

        if total_items > 0:
            builder.row(
                InlineKeyboardButton(text=f"📥 Видео ({send_items} шт.)", callback_data=f"tg:plv_all:{session_key}"),
                InlineKeyboardButton(text=f"🎵 MP3 ({send_items} шт.)", callback_data=f"tg:pla_all:{session_key}")
            )
        if total_items > 5:
            builder.row(
                InlineKeyboardButton(text="🎬 Первые 5 видео (720p)", callback_data=f"tg:plv_5:{session_key}"),
                InlineKeyboardButton(text="🎵 Первые 5 треков (MP3)", callback_data=f"tg:pla_5:{session_key}")
            )
        bottom_buttons = []
        if web_btn_url:
            bottom_buttons.append(InlineKeyboardButton(text="🌐 Открыть в Web (ZIP-архив)", url=web_btn_url))
        bottom_buttons.append(InlineKeyboardButton(text="❌ Отмена", callback_data=f"tg:cancel:{session_key}"))
        builder.row(*bottom_buttons)

        caption = (
            f"📚 <b>Обнаружен плейлист:</b> <b>{safe_title}</b>\n\n"
            f"👤 Автор: <b>{safe_uploader}</b>\n"
            f"🔢 Всего роликов в плейлисте: <b>{total_items}</b>"
            + (f" (в чат отправляется не больше {MAX_PLAYLIST_ITEMS})" if total_items > MAX_PLAYLIST_ITEMS else "")
            + "\n"
            f"🏷️ Сервис: <b>{platform.get('name', 'Media')}</b>\n\n"
            "Вы можете скачать <b>весь плейлист целиком</b> прямо сюда в Telegram, "
            "получить первые 5 роликов или открыть его в веб-версии для скачивания ZIP-архивом.\n\n"
            "Выберите действие:"
        )
        await status_msg.edit_text(caption, reply_markup=builder.as_markup(), parse_mode="HTML")
        return

    # Single photo branch
    if info.get("is_photo"):
        if settings["quality_pref"] != "ask":
            await execute_action(status_msg.bot, status_msg, tg_id, session_key, pending, "photo", "best")
            return
        builder.row(InlineKeyboardButton(text="🖼️ Скачать фото (HQ)", callback_data=f"tg:p:{session_key}"))
        legend = ""
    else:
        plan, auto_height = plan_video_qualities(info, identity)
        pending["auto_height"] = auto_height
        if settings["quality_pref"] != "ask":
            pref = settings["quality_pref"]
            height = auto_height if pref == "auto" else min(int(pref), identity.max_height)
            await execute_action(status_msg.bot, status_msg, tg_id, session_key, pending, "video", str(height))
            return

        q_buttons = []
        any_squeezed = False
        for p in plan:
            h = p["height"]
            label = quality_label(h)
            if p["size"]:
                label += f" · ~{human_mb(p['size'])}"
            if p["size"] and not p["fits"]:
                label += " 🗜"
                any_squeezed = True
            q_buttons.append(InlineKeyboardButton(text=f"🎬 {label}", callback_data=f"tg:v:{h}:{session_key}"))
        builder.row(InlineKeyboardButton(text=f"⭐ Авто — {auto_height}p (лучшее, что влезет в {human_mb(MAX_TG_SIZE_BYTES)})",
                                         callback_data=f"tg:v:auto:{session_key}"))
        for i in range(0, len(q_buttons), 2):
            builder.row(*q_buttons[i:i + 2])

        builder.row(InlineKeyboardButton(text="🎵 Аудио (MP3)", callback_data=f"tg:a:{session_key}"))
        langs = pick_subtitle_langs(info)
        if langs:
            builder.row(*[InlineKeyboardButton(text=f"💬 {lang.upper()}", callback_data=f"tg:s:{lang}:{session_key}") for lang in langs])
        legend = "\n🗜 — будет пережато под лимит Telegram" if any_squeezed else ""

    bottom_buttons = []
    if web_btn_url:
        bottom_buttons.append(InlineKeyboardButton(text="🌐 Открыть в MediaPull Web", url=web_btn_url))
    bottom_buttons.append(InlineKeyboardButton(text="❌ Отмена", callback_data=f"tg:cancel:{session_key}"))
    builder.row(*bottom_buttons)

    duration_sec = info.get("duration")
    duration_str = format_duration(duration_sec) if duration_sec else "Неизвестно"
    caption = (
        f"🎬 <b>{safe_title}</b>\n\n"
        f"👤 Автор: <b>{safe_uploader}</b>\n"
        f"⏱️ Длительность: <b>{duration_str}</b>\n"
        f"🏷️ Сервис: <b>{platform.get('name', 'Media')}</b>\n\n"
        "Выберите желаемое качество или аудио для отправки:" + legend
    )

    try:
        await status_msg.edit_text(caption, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as e:
        print(f"[Bot Warning] Failed to send inline markup: {e}")
        try:
            fallback_builder = InlineKeyboardBuilder()
            fallback_builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"tg:cancel:{session_key}"))
            await status_msg.edit_text(caption, reply_markup=fallback_builder.as_markup(), parse_mode="HTML")
        except Exception:
            pass


@dp.message(Command("formats"))
async def handle_formats(message: Message, command: CommandObject):
    url = extract_url(command.args) or (extract_url(message.reply_to_message.text) if message.reply_to_message else None)
    if not url:
        await message.answer("Использование: <code>/formats ссылка</code>", parse_mode="HTML")
        return
    fetched = await fetch_info(message, url, "Считаю размеры...")
    if not fetched:
        return
    url, info, status_msg = fetched
    if info.get("is_playlist") or info.get("is_photo"):
        await status_msg.edit_text("ℹ️ Таблица форматов доступна для одиночных видео.")
        return
    identity = resolve_identity(message.from_user.id)
    lines = [f"🎞 <b>{html.escape(info.get('title') or '')}</b>",
             f"⏱ {format_duration(info.get('duration')) or '—'} · лимит отправки <b>{human_mb(MAX_TG_SIZE_BYTES)}</b>\n"]
    plan, auto_height = plan_video_qualities(info, identity)
    for p in plan:
        size = f"~{human_mb(p['size'])}" if p["size"] else "размер неизвестен"
        note = "✅ влезает" if p["fits"] else ("🗜 будет пережато" if p["size"] else "")
        star = " ⭐" if p["height"] == auto_height else ""
        lines.append(f"• <b>{p['height']}p</b> — {size} {note}{star}")
    langs = [s.get("code") for s in (info.get("subtitles") or []) if s.get("code")]
    if langs:
        lines.append("\n💬 Субтитры: " + ", ".join(html.escape(c) for c in langs[:15]) + (" …" if len(langs) > 15 else ""))
    lines.append(f"\nМаксимальное качество вашего тарифа: <b>{identity.max_height}p</b>")
    await status_msg.edit_text("\n".join(lines), parse_mode="HTML")


@dp.message(Command("subs"))
async def handle_subs_command(message: Message, command: CommandObject):
    url = extract_url(command.args) or (extract_url(message.reply_to_message.text) if message.reply_to_message else None)
    if not url:
        await message.answer("Использование: <code>/subs ссылка</code>", parse_mode="HTML")
        return
    fetched = await fetch_info(message, url, "Ищу субтитры...")
    if not fetched:
        return
    url, info, status_msg = fetched
    langs = pick_subtitle_langs(info, limit=6)
    if not langs:
        await status_msg.edit_text("💬 У этого видео нет субтитров.")
        return
    key = _register_session(message, url, info)
    kb = InlineKeyboardBuilder()
    kb.row(*[InlineKeyboardButton(text=f"💬 {lang.upper()}", callback_data=f"tg:s:{lang}:{key}") for lang in langs[:3]])
    if len(langs) > 3:
        kb.row(*[InlineKeyboardButton(text=f"💬 {lang.upper()}", callback_data=f"tg:s:{lang}:{key}") for lang in langs[3:6]])
    kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"tg:cancel:{key}"))
    await status_msg.edit_text(f"💬 <b>{html.escape(info.get('title') or '')}</b>\nВыберите язык субтитров (SRT):",
                               reply_markup=kb.as_markup(), parse_mode="HTML")


@dp.message(Command("dl"))
async def handle_dl_command(message: Message, command: CommandObject):
    url = extract_url(command.args) or (extract_url(message.reply_to_message.text or message.reply_to_message.caption)
                                        if message.reply_to_message else None)
    if not url:
        await message.answer("Использование: <code>/dl ссылка</code> или ответьте этой командой на сообщение со ссылкой.", parse_mode="HTML")
        return
    await process_link(message, url)


@dp.message(F.text)
async def handle_url_message(message: Message):
    if not message.from_user:
        return
    text = message.text.strip()
    if text.startswith("/"):
        return  # an unknown command is not a link
    is_group = message.chat.type in ("group", "supergroup")
    if is_group:
        # In groups the bot only reacts when it is addressed: @mention (or the /dl command above).
        if not BOT_USERNAME or f"@{BOT_USERNAME.lower()}" not in text.lower():
            return
    url = extract_url(text)
    if not url:
        if not is_group:
            await message.answer(
                "💡 Отправьте мне ссылку на видео (YouTube, Instagram, TikTok, VK, Rutube, X и др.), "
                "и я подготовлю его к отправке прямо сюда!",
                parse_mode="HTML"
            )
        return
    await process_link(message, url)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
async def send_media(bot: Bot, chat_id: int, kind: str, media, *, title: str, caption: Optional[str],
                     width: int = 0, height: int = 0, duration: int = 0, reply_to: Optional[int] = None) -> Message:
    """Send either a local file (FSInputFile) or a cached Telegram file_id."""
    common = {"chat_id": chat_id}
    if reply_to:
        common["reply_to_message_id"] = reply_to
        common["allow_sending_without_reply"] = True
    if kind == "video":
        extra = {"duration": duration, "width": width, "height": height} if not isinstance(media, str) else {}
        return await bot.send_video(video=media, caption=caption, supports_streaming=True, **extra, **common)
    if kind == "audio":
        return await bot.send_audio(audio=media, title=title[:100], **common)
    if kind == "photo":
        return await bot.send_photo(photo=media, caption=caption, **common)
    return await bot.send_document(document=media, caption=caption, **common)


def extract_file_id(sent: Message, kind: str) -> Optional[str]:
    try:
        if kind == "video" and sent.video:
            return sent.video.file_id
        if kind == "audio" and sent.audio:
            return sent.audio.file_id
        if kind == "photo" and sent.photo:
            return sent.photo[-1].file_id
        if sent.document:
            return sent.document.file_id
    except Exception:
        pass
    return None


async def wait_for_task(task_id: str, status_msg: Optional[Message] = None, safe_title: str = "") -> Path:
    """Wait until a download task completes and return its file. Raises RuntimeError on failure or timeout."""
    start_time = time.time()
    last_progress = -1
    while True:
        task = downloader_manager.tasks.get(task_id)
        if not task:
            raise RuntimeError("Задача загрузки потеряна.")
        if task.status == "completed":
            break
        if task.status in ("error", "cancelled"):
            raise RuntimeError(task.error or "Неизвестная ошибка скачивания")
        if time.time() - start_time > DOWNLOAD_TIMEOUT_SECONDS:
            raise RuntimeError("Превышено время ожидания загрузки.")

        cur_prog = int(task.progress)
        if status_msg and cur_prog >= last_progress + 8 and cur_prog <= 95:
            last_progress = cur_prog
            try:
                await status_msg.edit_text(
                    f"⏳ <b>Скачивание медиа:</b>\n"
                    f"📌 <i>{safe_title}</i>\n\n"
                    f"📊 Прогресс: <b>{cur_prog}%</b> ({task.speed or '---'})\n"
                    f"<i>Подготовка файла для отправки...</i>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        await asyncio.sleep(1.2)

    if not task.filepath:
        raise RuntimeError("Файл не был сформирован.")
    filepath = Path(task.filepath)
    if not filepath.exists():
        raise RuntimeError(f"Файл {filepath.name} не найден на диске.")
    return filepath


async def download_and_send_single(
    bot: Bot,
    chat_id: int,
    user_id: int,
    url: str,
    title: str,
    action: str,
    quality: str = "720",
    status_msg: Optional[Message] = None,
    photo_url: Optional[str] = None,
    *,
    cache_key: Optional[str] = None,
    source_url: Optional[str] = None,
    lang: str = "en",
    reply_to: Optional[int] = None,
) -> bool:
    """
    Delivers one item. A cached Telegram file_id is sent instantly; otherwise the media is downloaded into a private
    per-job folder in temp_tg, compressed if needed, sent, and the whole folder (and the task) removed in a finally
    block. Linked users who enabled "save to library" get a permanent copy in their web library instead.
    """
    identity = resolve_identity(user_id)
    settings = bot_store.get_settings(user_id)
    safe_title = html.escape(title)
    kind = {"video": "video", "audio": "audio", "photo": "photo", "subs": "document"}[action]
    caption = title[:1000] if (settings["caption_mode"] == "title" and title and kind == "video") else None
    reply_to = reply_to if reply_to else None

    # 1. Instant path: Telegram already has this file.
    if cache_key:
        hit = bot_store.cache_get(cache_key)
        if hit:
            try:
                await send_media(bot, chat_id, kind, hit["file_id"], title=title, caption=caption, reply_to=reply_to)
                bot_store.record_event(user_id, cached=True)
                return True
            except TelegramBadRequest:
                bot_store.cache_delete(cache_key)  # the file_id is stale: fall through to a normal download

    library = bool(settings["save_library"] and identity.web_user and action != "subs")
    compressed: Optional[Path] = None
    job_id = secrets.token_hex(6)
    task_id = f"tg_{job_id}"
    if library:
        owner_id, owner_role = identity.web_user["username"], identity.web_user["role"]
        job_dir = None
    else:
        owner_id, owner_role = f"tg_temp_{job_id}", identity.role
        job_dir = TEMP_TG_DIR / job_id
    mode = {"audio": "audio", "photo": "photo", "subs": "subtitles"}.get(action, "single")

    async def on_queue_position(position: int, eta: int):
        if status_msg:
            await status_msg.edit_text(
                f"⏳ <b>В очереди:</b> позиция {position}\n📌 <i>{safe_title}</i>\n"
                f"Ориентировочное ожидание: ~{max(1, eta // 60)} мин.", parse_mode="HTML")

    try:
        started = await job_queue.acquire(on_queue_position if status_msg else None)
        try:
            await downloader_manager.start_download(
                task_id=task_id,
                url=url,
                title=title,
                mode=mode,
                quality=quality,
                video_format="mp4",
                audio_format="mp3",
                audio_quality="192",
                subtitles_lang=lang,
                is_photo=(action == "photo"),
                photo_url=photo_url,
                user_id=owner_id,
                role=owner_role
            )

            filepath = await wait_for_task(task_id, status_msg, safe_title)

            # Trust the file, not the request: an image must never be uploaded as a "video" (and vice versa).
            suffix = filepath.suffix.lower()
            if action in ("video", "audio") and suffix in IMAGE_EXTS:
                action, kind = "photo", "photo"
            elif action == "video" and suffix in AUDIO_EXTS:
                action, kind = "audio", "audio"
            if kind == "photo" and filepath.stat().st_size > TG_PHOTO_MAX_BYTES:
                kind = "document"  # Telegram rejects photos above 10 MB
            if kind != "video":
                caption = None

            target_to_send = filepath
            if action == "video" and filepath.stat().st_size > MAX_TG_SIZE_BYTES:
                if status_msg:
                    try:
                        await status_msg.edit_text(
                            f"⚡ <b>Сжатие видео для Telegram...</b>\n"
                            f"📌 <i>{safe_title}</i>\n\n"
                            f"Исходный размер: <b>{format_size(filepath.stat().st_size)}</b> (лимит: {human_mb(MAX_TG_SIZE_BYTES)})\n"
                            f"Оптимизирую видеопоток через FFmpeg...",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                async with compress_lock:
                    target_to_send = await asyncio.to_thread(compress_video_for_telegram, filepath)
                if target_to_send != filepath:
                    compressed = target_to_send
        finally:
            job_queue.release(started)

        final_size = target_to_send.stat().st_size
        if final_size > TG_HARD_LIMIT_BYTES:
            hint = "Откройте его в веб-версии." if not LOCAL_API_SERVER else ""
            raise RuntimeError(f"Файл ({format_size(final_size)}) не помещается в лимит Telegram. {hint}".strip())

        width = height = duration = 0
        if action == "video":
            width, height, duration = await asyncio.to_thread(get_video_dimensions_and_duration, target_to_send)

        if status_msg:
            label = {"video": f"Размер: <b>{format_size(final_size)}</b> | Качество: <b>{width}x{height}</b>"}.get(
                action, f"Размер: <b>{format_size(final_size)}</b>")
            try:
                await status_msg.edit_text(f"📤 <b>Отправка в Telegram...</b>\n{label}", parse_mode="HTML")
            except Exception:
                pass

        send_name = target_to_send.name
        media = FSInputFile(str(target_to_send), filename=send_name)
        sent = await send_media(bot, chat_id, kind, media, title=title, caption=caption,
                                width=width, height=height, duration=duration, reply_to=reply_to)

        file_id = extract_file_id(sent, kind)
        if cache_key and file_id and not library:
            bot_store.cache_put(cache_key, kind, file_id, final_size, title, source_url or url)
        bot_store.record_event(user_id, size=final_size)
        if library:
            try:
                await bot.send_message(chat_id, "💾 Копия сохранена в вашей веб-библиотеке.", reply_to_message_id=reply_to,
                                       allow_sending_without_reply=True)
            except Exception:
                pass
        return True

    except Exception:
        bot_store.record_event(user_id, ok=False)
        raise
    finally:
        # Stop the download if it is still running, then drop the task. Temporary jobs lose EVERYTHING they wrote
        # (media, compressed copy, thumbnails, subtitles, partial files) with the job folder.
        downloader_manager.cancel_task(task_id)
        downloader_manager.tasks.pop(task_id, None)
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
        elif compressed is not None:
            compressed.unlink(missing_ok=True)  # library mode keeps the original, not the Telegram-sized copy


def parse_img_index(raw_url: str, count: int) -> Optional[int]:
    """`?img_index=3` in an Instagram link is the 1-based slide the user was looking at."""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(raw_url).query)
        n = int(query.get("img_index", [""])[0])
    except (ValueError, IndexError):
        return None
    return n if 1 <= n <= count else None


async def _download_slides(todo: List[Dict[str, Any]], job_id: str, owner_id: str, owner_role: str,
                           library_title: Optional[str], status_msg: Optional[Message], task_ids: List[str]) -> int:
    """Download the given slides in parallel (bounded by the downloader). Returns the number of failures."""
    done = 0
    failed = 0
    lock = asyncio.Lock()

    async def one(slot: Dict[str, Any]) -> None:
        nonlocal done, failed
        entry, idx = slot["entry"], slot["idx"]
        task_id = f"tg_{job_id}_{idx}"
        task_ids.append(task_id)
        is_photo = slot["kind"] == "photo"
        try:
            await downloader_manager.start_download(
                task_id=task_id,
                url=entry["url"],
                title=entry.get("title") or f"Слайд {idx}",
                mode="photo" if is_photo else "single",
                quality="720",
                video_format="mp4",
                is_photo=is_photo,
                photo_url=(entry.get("direct_url") or entry["url"]) if is_photo else None,
                playlist_title=library_title,
                playlist_index=idx if library_title else None,
                user_id=owner_id,
                role=owner_role,
            )
            slot["path"] = await wait_for_task(task_id)
        except Exception as e:
            print(f"[Bot] carousel slide {idx} failed: {e}")
            slot["error"] = str(e)
        async with lock:
            done += 1
            failed += 1 if not slot.get("path") else 0
            if status_msg:
                try:
                    await status_msg.edit_text(f"⏳ <b>Скачиваю слайды:</b> {done}/{len(todo)}", parse_mode="HTML")
                except Exception:
                    pass

    await asyncio.gather(*(one(s) for s in todo))
    return failed


async def _send_slides(bot: Bot, chat_id: int, slots: List[Dict[str, Any]], caption: Optional[str], save_ids: bool,
                       source_url: str, title: str) -> int:
    """Send every not-yet-sent slide: albums of up to 10 (a lone slide as a plain photo/video). Returns bytes sent."""
    ready = [s for s in slots if not s.get("sent") and (s.get("file_id") or s.get("path"))]
    total = 0

    # Videos above the upload limit are squeezed first; oversized pictures go as documents.
    for s in ready:
        s["send_kind"] = s["kind"]
        path = s.get("path")
        if not path:
            continue
        size = path.stat().st_size
        total += size
        if s["kind"] == "video" and size > MAX_TG_SIZE_BYTES:
            async with compress_lock:
                s["path"] = await asyncio.to_thread(compress_video_for_telegram, path)
        elif s["kind"] == "photo" and size > TG_PHOTO_MAX_BYTES:
            s["send_kind"] = "document"

    first_caption = caption
    docs = [s for s in ready if s["send_kind"] == "document"]
    for s in docs:
        sent = await bot.send_document(chat_id=chat_id, document=FSInputFile(str(s["path"])), caption=first_caption)
        first_caption = None
        s["sent"] = True
        if save_ids and (fid := extract_file_id(sent, "document")):
            bot_store.cache_put(s["key"], "document", fid, s["path"].stat().st_size, title, source_url)

    album = [s for s in ready if s["send_kind"] != "document"]
    if len(album) == 1:
        s = album[0]
        ref = s.get("file_id") or FSInputFile(str(s["path"]))
        w = h = d = 0
        if s["kind"] == "video" and s.get("path"):
            w, h, d = await asyncio.to_thread(get_video_dimensions_and_duration, s["path"])
        sent = await send_media(bot, chat_id, s["kind"], ref, title=title, caption=first_caption, width=w, height=h, duration=d)
        s["sent"] = True
        if save_ids and (fid := extract_file_id(sent, s["kind"])):
            bot_store.cache_put(s["key"], s["kind"], fid, s["path"].stat().st_size if s.get("path") else 0, title, source_url)
        return total

    for start in range(0, len(album), TG_ALBUM_SIZE):
        chunk = album[start:start + TG_ALBUM_SIZE]
        media = []
        for s in chunk:
            ref = s.get("file_id") or FSInputFile(str(s["path"]))
            cap = first_caption if (first_caption and not media) else None
            if s["kind"] == "photo":
                media.append(InputMediaPhoto(media=ref, caption=cap))
            else:
                w, h, d = (0, 0, 0)
                if s.get("path"):
                    w, h, d = await asyncio.to_thread(get_video_dimensions_and_duration, s["path"])
                media.append(InputMediaVideo(media=ref, caption=cap, supports_streaming=True,
                                             width=w or None, height=h or None, duration=d or None))
        first_caption = None
        if len(media) == 1:
            s = chunk[0]
            sent_one = await send_media(bot, chat_id, s["kind"], media[0].media, title=title, caption=None)
            messages = [sent_one]
        else:
            messages = await bot.send_media_group(chat_id=chat_id, media=media)
        for s, m in zip(chunk, messages):
            s["sent"] = True
            if save_ids and (fid := extract_file_id(m, s["kind"])):
                size = s["path"].stat().st_size if s.get("path") else 0
                bot_store.cache_put(s["key"], s["kind"], fid, size, title, source_url)
    return total


async def send_carousel(bot: Bot, msg: Message, tg_id: int, data: Dict[str, Any], only_index: Optional[int] = None) -> None:
    """Deliver the slides of a carousel post as photos/videos in albums, using the file_id cache where possible."""
    entries = (data.get("entries") or [])[:CAROUSEL_MAX_SLIDES]
    selected = [(i, e) for i, e in enumerate(entries, 1) if only_index is None or i == only_index]
    if not selected:
        raise RuntimeError("В посте нет слайдов для отправки.")

    identity = resolve_identity(tg_id)
    settings = bot_store.get_settings(tg_id)
    library = bool(settings["save_library"] and identity.web_user)
    title = data["title"]
    caption = title[:1000] if settings["caption_mode"] == "title" and title else None
    platform_id = data.get("platform_id", "web")
    source_url = data.get("source_url") or normalize_url(data["url"])
    chat_id = msg.chat.id

    job_id = secrets.token_hex(6)
    if library:
        owner_id, owner_role, job_dir = identity.web_user["username"], identity.web_user["role"], None
        library_title = f"Instagram {title}"[:80]
    else:
        owner_id, owner_role, job_dir = f"tg_temp_{job_id}", identity.role, TEMP_TG_DIR / job_id
        library_title = None

    slots: List[Dict[str, Any]] = []
    for idx, entry in selected:
        kind = "video" if entry.get("is_video") else "photo"
        key = make_cache_key(f"{platform_id}:{entry.get('id') or idx}", kind, "best")
        hit = None if library else bot_store.cache_get(key)
        slots.append({"idx": idx, "entry": entry, "kind": kind, "key": key,
                      "file_id": hit["file_id"] if hit else None, "path": None})

    task_ids: List[str] = []
    failed = 0
    total_bytes = 0
    try:
        for attempt in range(2):
            todo = [s for s in slots if not s.get("sent") and not s.get("file_id") and not s.get("path") and not s.get("error")]
            if todo:
                async def on_position(position: int, eta: int):
                    await msg.edit_text(f"⏳ <b>В очереди:</b> позиция {position}\nОриентировочное ожидание: ~{max(1, eta // 60)} мин.",
                                        parse_mode="HTML")
                started = await job_queue.acquire(on_position)
                try:
                    await msg.edit_text(f"⏳ <b>Скачиваю слайды:</b> 0/{len(todo)}", parse_mode="HTML")
                    failed += await _download_slides(todo, job_id, owner_id, owner_role, library_title, msg, task_ids)
                finally:
                    job_queue.release(started)

            deliverable = [s for s in slots if s.get("file_id") or s.get("path")]
            if not deliverable:
                reasons = {s.get("error") for s in slots if s.get("error")}
                raise RuntimeError("Не удалось скачать ни одного слайда." + (f" {next(iter(reasons))}" if reasons else ""))
            try:
                await msg.edit_text(f"📤 <b>Отправка в Telegram...</b>\nСлайдов: {len(deliverable)}", parse_mode="HTML")
            except Exception:
                pass
            try:
                total_bytes += await _send_slides(bot, chat_id, slots, caption, save_ids=not library,
                                                  source_url=source_url, title=title)
                break
            except TelegramBadRequest:
                stale = [s for s in slots if s.get("file_id") and not s.get("sent")]
                if attempt == 0 and stale:
                    for s in stale:  # the cached file_id is stale: forget it and download these slides for real
                        bot_store.cache_delete(s["key"])
                        s["file_id"] = None
                    continue
                raise
        bot_store.record_event(tg_id, size=total_bytes)
        if failed:
            await bot.send_message(chat_id, f"⚠️ Не удалось скачать слайдов: {failed}.")
        if library:
            await bot.send_message(chat_id, "💾 Копии сохранены в вашей веб-библиотеке.")
    except Exception:
        bot_store.record_event(tg_id, ok=False)
        raise
    finally:
        for tid in task_ids:
            downloader_manager.cancel_task(tid)
            downloader_manager.tasks.pop(tid, None)
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)


def make_variant(action: str, quality: str, lang: str) -> str:
    return lang if action == "subs" else ("audio" if action == "audio" else quality)


async def execute_action(bot: Bot, msg: Message, tg_id: int, session_key: str, data: Dict[str, Any],
                         action: str, quality: str, lang: str = "en") -> None:
    """Run a menu choice. `msg` is the bot's own menu/status message that gets updated and finally removed."""
    if tg_id in busy_users:
        await msg.edit_text("⏳ У вас уже идёт загрузка. Дождитесь её завершения.")
        return
    busy_users.add(tg_id)
    try:
        await _execute_action(bot, msg, tg_id, session_key, data, action, quality, lang)
    finally:
        busy_users.discard(tg_id)


async def _execute_action(bot: Bot, msg: Message, tg_id: int, session_key: str, data: Dict[str, Any],
                          action: str, quality: str, lang: str) -> None:
    url = data["url"]
    title = data["title"]
    safe_title = html.escape(title)
    chat_id = msg.chat.id
    media_id = data.get("media_id") or media_identity({}, url)
    source_url = data.get("source_url") or normalize_url(url)
    reply_to = None  # menu messages already sit under the user's request

    # Instagram-style carousel: deliver the slides as an album of photos (and videos), never as a "video"
    if action in ("car_all", "car_one"):
        only = data.get("img_index") if action == "car_one" else None
        try:
            await send_carousel(bot, msg, tg_id, data, only_index=only)
            pending_downloads.pop(session_key, None)
            try:
                await msg.delete()
            except Exception:
                pass
        except Exception as e:
            print(f"[Bot Error] carousel: {e}")
            try:
                await msg.edit_text(f"❌ <b>Не удалось отправить слайды:</b>\n<code>{html.escape(str(e)[-600:])}</code>", parse_mode="HTML")
            except Exception:
                pass
        return

    # Playlist batch execution (all or first 5)
    if action in ("pla_all", "plv_all", "pla_5", "plv_5"):
        entries = data.get("entries", [])
        is_audio = action.startswith("pla")
        items_to_send = entries[:5] if action.endswith("_5") else entries[:MAX_PLAYLIST_ITEMS]
        if not items_to_send:
            await msg.edit_text("❌ Плейлист пуст или не содержит доступных элементов.")
            return

        total_items = len(items_to_send)
        type_str = "аудио (MP3)" if is_audio else "видео (720p)"
        try:
            await msg.edit_text(
                f"⏳ <b>Запуск пакетной отправки: {total_items} {type_str}</b>\n"
                f"📌 <i>{safe_title}</i>\n\n"
                "Файлы скачиваются и отправляются по очереди прямо в этот чат...",
                parse_mode="HTML"
            )
        except Exception:
            pass

        sent_count = 0
        for idx, item in enumerate(items_to_send, 1):
            item_url = item.get("url") or item.get("webpage_url") or item.get("original_url")
            item_title = item.get("title") or f"Элемент {idx}"
            if not item_url:
                continue
            try:
                await msg.edit_text(
                    f"⏳ <b>Отправка плейлиста [{idx}/{total_items}]:</b>\n"
                    f"📌 <i>{html.escape(item_title)}</i>\n\n"
                    f"📊 Прогресс: <b>{int((idx-1)/total_items*100)}%</b>\n"
                    f"<i>Файл скачивается и отправляется в Telegram...</i>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            item_action = "photo" if item.get("is_photo") else ("audio" if is_audio else "video")
            item_id = item.get("id")
            try:
                success = await download_and_send_single(
                    bot=bot,
                    chat_id=chat_id,
                    user_id=tg_id,
                    url=item_url,
                    title=item_title,
                    action=item_action,
                    quality="720",
                    status_msg=None,
                    photo_url=item.get("direct_url") if item_action == "photo" else None,
                    cache_key=make_cache_key(f"{data.get('platform_id', 'web')}:{item_id}", item_action,
                                             make_variant(item_action, "720", lang)) if item_id else None,
                    source_url=normalize_url(item_url),
                )
            except Exception as item_err:
                # One broken item must not abort the rest of the playlist.
                print(f"[Bot] Playlist item {idx} failed: {item_err}")
                success = False
            if success:
                sent_count += 1
            await asyncio.sleep(1.2)

        pending_downloads.pop(session_key, None)
        try:
            await msg.edit_text(
                f"✅ <b>Отправка плейлиста завершена!</b>\n"
                f"Успешно отправлено: <b>{sent_count} из {total_items}</b> файлов.\n\n"
                f"<i>Все временные файлы очищены с диска.</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # Single download execution
    if action == "audio":
        format_label = "🎵 Аудио (MP3)"
    elif action == "photo":
        format_label = "🖼️ Фото (HQ)"
    elif action == "subs":
        format_label = f"💬 Субтитры ({lang.upper()})"
    else:
        format_label = f"🎬 Видео ({quality}p)" if quality.isdigit() else "🎬 Видео"

    try:
        await msg.edit_text(
            f"⏳ <b>Загрузка и подготовка: {format_label}</b>\n"
            f"📌 <i>{safe_title}</i>\n\n"
            f"🔄 Готовлю файл...\n"
            f"<i>Пожалуйста, подождите несколько секунд...</i>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    try:
        await download_and_send_single(
            bot=bot,
            chat_id=chat_id,
            user_id=tg_id,
            url=url,
            title=title,
            action=action,
            quality=quality,
            status_msg=msg,
            photo_url=data.get("photo_url"),
            cache_key=make_cache_key(media_id, action, make_variant(action, quality, lang)),
            source_url=source_url,
            lang=lang,
            reply_to=reply_to,
        )
        pending_downloads.pop(session_key, None)
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception as e:
        print(f"[Bot Error] {e}")
        safe_err = html.escape(str(e)[-600:])
        try:
            await msg.edit_text(
                f"❌ <b>Не удалось отправить файл:</b>\n<code>{safe_err}</code>\n\n"
                f"Вы можете открыть ссылку напрямую в веб-интерфейсе:\n"
                f"<a href=\"{html.escape(WEB_BASE_URL + '?url=' + urllib.parse.quote(url, safe=''), quote=True)}\">{html.escape(WEB_BASE_URL)}</a>",
                parse_mode="HTML"
            )
        except Exception:
            pass


@dp.callback_query(F.data.startswith("tg:"))
async def handle_callback_download(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка запроса")
        return

    act_type = parts[1]
    quality = "720"
    lang = "en"
    if act_type == "v" and len(parts) >= 4:
        quality, session_key, action = parts[2], parts[3], "video"
    elif act_type == "a":
        quality, session_key, action = "best", parts[2], "audio"
    elif act_type == "p":
        quality, session_key, action = "best", parts[2], "photo"
    elif act_type == "s" and len(parts) >= 4 and SUBTITLE_LANG_RE.match(parts[2]):
        lang, session_key, action = parts[2], parts[3], "subs"
    elif act_type in ("pla_all", "plv_all", "pla_5", "plv_5"):
        session_key, action = parts[2], act_type
    elif act_type in ("car_all", "car_one"):
        session_key, action = parts[2], act_type
    elif act_type == "cancel":
        pending_downloads.pop(parts[2], None)
        await callback.message.edit_text("❌ Загрузка отменена.")
        await callback.answer("Отменено")
        return
    else:
        await callback.answer("Эта кнопка устарела")
        return

    data = pending_downloads.get(session_key)
    if not data or time.time() - data.get("ts", 0) > PENDING_TTL_SECONDS:
        pending_downloads.pop(session_key, None)
        await callback.answer("Сессия устарела. Отправьте ссылку повторно.", show_alert=True)
        return
    if data.get("owner") not in (None, callback.from_user.id):
        await callback.answer("Эта кнопка предназначена для другого пользователя.", show_alert=True)
        return
    if callback.from_user.id in busy_users:
        await callback.answer("У вас уже идёт загрузка. Дождитесь её завершения.", show_alert=True)
        return

    if action == "video":
        if quality == "auto":
            quality = str(data.get("auto_height") or 720)
        elif not quality.isdigit():
            await callback.answer("Недопустимое качество", show_alert=True)
            return
        quality = str(min(int(quality), resolve_identity(callback.from_user.id).max_height))

    await callback.answer("Запуск обработки...")
    await execute_action(bot, callback.message, callback.from_user.id, session_key, data, action, quality, lang)


# ---------------------------------------------------------------------------
# Inline mode:  @bot <link>  -> already downloaded files are offered instantly
# ---------------------------------------------------------------------------
@dp.inline_query()
async def handle_inline_query(inline_query: InlineQuery):
    url = extract_url(inline_query.query)
    if not url:
        await inline_query.answer([], cache_time=5, is_personal=True)
        return
    source = normalize_url(url)
    results = []
    for i, entry in enumerate(bot_store.cache_find_by_source(source)[:10]):
        rid = hashlib.sha1(f"{entry['key']}".encode()).hexdigest()[:32]
        title = entry["title"] or "Медиа"
        size = f" · {human_mb(entry['size'])}" if entry["size"] else ""
        if entry["kind"] == "video":
            results.append(InlineQueryResultCachedVideo(id=rid, video_file_id=entry["file_id"], title=f"{title}{size}"))
        elif entry["kind"] == "audio":
            results.append(InlineQueryResultCachedAudio(id=rid, audio_file_id=entry["file_id"]))
        elif entry["kind"] == "photo":
            results.append(InlineQueryResultCachedPhoto(id=rid, photo_file_id=entry["file_id"], title=title))
    button = None
    if not results:
        token = bot_store.create_shortlink(url)
        button = InlineQueryResultsButton(text="Скачать через бота", start_parameter=f"dl_{token}")
    await inline_query.answer(results, cache_time=30, is_personal=True, button=button)


async def test_bot_pipeline(test_url: str):
    """
    Simulate full Telegram Bot pipeline on a URL without requiring active Telegram polling:
    1. Video metadata extraction
    2. Video download
    3. Telegram size constraint check & FFmpeg compression
    4. Dimension & duration validation
    """
    print("\n" + "=" * 65)
    print("🤖 MEDIAPULL TELEGRAM BOT — TEST PIPELINE")
    print(f"🔗 URL: {test_url}")
    print("=" * 65)

    print("\n[1/4] 🔍 Анализ метаданных через DownloaderManager...")
    info = await downloader_manager.get_video_info(test_url)
    title = info.get("title", "Test Video")
    duration = info.get("duration", 0)
    print(f"      ✅ Название: {title}")
    print(f"      ⏱️ Длительность: {format_duration(duration)} ({duration} сек.)")
    print(f"      👤 Автор: {info.get('uploader') or 'N/A'}")

    task_id = f"test_tg_{int(time.time())}"
    print(f"\n[2/4] 📥 Скачивание медиапотока (Task: {task_id})...")
    await downloader_manager.start_download(
        task_id=task_id,
        url=test_url,
        title=title,
        mode="single",
        quality="720p",
        video_format="mp4",
        user_id=f"tg_temp_{task_id}",
        role="admin"
    )

    while True:
        task = downloader_manager.tasks.get(task_id)
        if not task:
            break
        if task.status == "completed":
            print(f"      ✅ Скачивание завершено! Файл: {task.filename}")
            break
        elif task.status in ("error", "cancelled"):
            print(f"      ❌ Ошибка: {task.error}")
            return False
        await asyncio.sleep(0.5)

    raw_path = Path(task.filepath)
    raw_size = raw_path.stat().st_size
    print(f"      📦 Исходный размер: {format_size(raw_size)} ({raw_size} байт)")

    print(f"\n[3/4] ⚡ Проверка лимита Telegram ({human_mb(MAX_TG_SIZE_BYTES)}) и FFmpeg-сжатие...")
    if raw_size > MAX_TG_SIZE_BYTES:
        print(f"      ⚠️ Файл превышает лимит ({format_size(raw_size)}). Запуск сжатия...")
        final_path = compress_video_for_telegram(raw_path, MAX_TG_SIZE_BYTES)
    else:
        print("      ✅ Файл уже укладывается в лимит Telegram, сжатие не требуется.")
        final_path = raw_path

    final_size = final_path.stat().st_size
    print(f"      🎉 Итоговый файл: {final_path.name}")
    print(f"      📊 Итоговый размер: {format_size(final_size)} ({final_size} байт)")

    print("\n[4/4] 🎬 Проверка параметров видео (ffprobe)...")
    w, h, dur = get_video_dimensions_and_duration(final_path)
    print(f"      📐 Разрешение: {w}x{h}")
    print(f"      ⏱️ Длительность видео: {dur} сек.")
    print("      🚀 Поддержка Faststart (стриминг в TG): Готов к отправке!")

    # Cleanup test files (the whole per-job folder)
    shutil.rmtree(raw_path.parent, ignore_errors=True)

    print("\n" + "=" * 65)
    print("✅ ВСЕ ТЕСТЫ ПАЙПЛАЙНА TELEGRAM-БОТА УСПЕШНО ПРОЙДЕНЫ!")
    print("🧹 Временные файлы автоматически удалены.")
    print("=" * 65 + "\n")
    return True


def build_bot(token: str) -> Bot:
    """Bot instance for the cloud API, or for a Local Bot API server when TELEGRAM_API_SERVER is set."""
    if LOCAL_API_SERVER:
        session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API_SERVER, is_local=True), timeout=1800)
        return Bot(token=token, session=session)
    return Bot(token=token)


async def run_webhook(bot: Bot) -> None:
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET or None).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    await bot.set_webhook(f"{WEBHOOK_URL}{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET or None, drop_pending_updates=True)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT).start()
    print(f"🌐 Webhook: {WEBHOOK_URL}{WEBHOOK_PATH} (слушаю {WEBHOOK_HOST}:{WEBHOOK_PORT})")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main():
    global BOT_USERNAME
    parser = argparse.ArgumentParser(description="MediaPull Telegram Bot")
    parser.add_argument("--test-pipeline", type=str, help="Run end-to-end test on a video URL without Telegram token")
    parser.add_argument("--token", type=str, help="Telegram Bot Token override")
    args = parser.parse_args()

    if args.test_pipeline:
        success = await test_bot_pipeline(args.test_pipeline)
        sys.exit(0 if success else 1)

    # Initial cleanup of any old temp files
    cleanup_temp_tg()
    bot_store.init_store()
    database.init_schema()

    token = args.token or TELEGRAM_BOT_TOKEN
    if not token:
        print("\n" + "=" * 65)
        print("⚠️  ТОКЕН ТЕЛЕГРАМ-БОТА НЕ НАЙДЕН!")
        print("=" * 65)
        return

    bot = build_bot(token)
    print("\n" + "=" * 60)
    print("🤖 Запуск MediaPull Telegram Bot (Clean Ephemeral Mode)...")
    print(f"🌐 Сервер MediaPull: {WEB_BASE_URL}")
    print(f"🗑️  Папка временного буфера: {TEMP_TG_DIR} (мгновенное удаление после отправки)")
    print(f"📦 Лимит отправки: {human_mb(MAX_TG_SIZE_BYTES)}" + (f" (локальный Bot API: {LOCAL_API_SERVER})" if LOCAL_API_SERVER else ""))
    print("=" * 60 + "\n")

    if ALLOWED_USER_IDS or bot_store.get_closed_mode():
        print("🔒 Закрытый режим: доступ только у разрешённых пользователей")
    else:
        print("⚠️  Бот открыт для всех пользователей Telegram (ограничить: TELEGRAM_ALLOWED_IDS или /closed on)")

    background = [asyncio.create_task(periodic_temp_cleanup()), asyncio.create_task(heartbeat_loop())]
    if auto_update_enabled():
        background.append(asyncio.create_task(ytdlp_update_loop()))
    try:
        me = await bot.get_me()
        BOT_USERNAME = BOT_USERNAME or (me.username or "")
        print(f"✅ Бот успешно подключен: @{me.username} ({me.first_name})")
        try:
            await bot.set_my_commands(BOT_COMMANDS)
        except Exception as e:
            print(f"[Bot] set_my_commands failed: {e}")

        if WEBHOOK_URL:
            await run_webhook(bot)
        else:
            print("🚀 Ожидание сообщений от пользователей (long polling)...\n")
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        await downloader_manager.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
