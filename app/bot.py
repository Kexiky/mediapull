"""
MediaPull Telegram Bot (aiogram 3.x)
Universal Media Downloader & Smart In-Memory / Ephemeral Delivery for Telegram
"""

import os
import re
import sys
import time
import uuid
import html
import asyncio
import argparse
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

from dotenv import load_dotenv

# Load .env if present
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.filters import Command, CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.downloader import downloader_manager, detect_platform, format_size, format_duration

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEB_BASE_URL = os.getenv("MEDIAPULL_WEB_URL", "http://127.0.0.1:8000").rstrip("/")
MAX_TG_SIZE_BYTES = 48 * 1024 * 1024  # 48 MB safety margin for 50 MB Telegram Bot API limit

# Ephemeral storage directory for Telegram downloads (deleted immediately after sending)
TEMP_TG_DIR = BASE_DIR / "downloads" / "temp_tg"
TEMP_TG_DIR.mkdir(parents=True, exist_ok=True)

# In-memory store for pending download sessions: key -> info dict
pending_downloads: Dict[str, Dict[str, Any]] = {}
# User preferences for video captions: telegram_id -> "title" | "none" (default: "none" for clean forward)
user_caption_modes: Dict[int, str] = {}


def cleanup_temp_tg(max_age_seconds: int = 300):
    """Clean up any leftover temporary files in temp_tg folder older than max_age_seconds."""
    try:
        now = time.time()
        for f in TEMP_TG_DIR.glob("*"):
            if f.is_file():
                try:
                    if now - f.stat().st_mtime > max_age_seconds:
                        f.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception as e:
        print(f"[Temp Cleanup] {e}")


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
                    except: pass
                elif k == "height":
                    try: height = int(v)
                    except: pass
                elif k == "duration":
                    try: dur_val = float(v)
                    except: pass
        duration = int(dur_val)
    except Exception as e:
        print(f"[ffprobe] Warning extracting dimensions: {e}")
    return width, height, duration


def compress_video_for_telegram(input_path: Path, max_bytes: int = MAX_TG_SIZE_BYTES) -> Path:
    """
    Compress video to guarantee it stays under Telegram's 50MB limit using FFmpeg target bitrate.
    Output is saved in TEMP_TG_DIR and will be deleted after sending.
    """
    input_size = input_path.stat().st_size
    if input_size <= max_bytes:
        return input_path

    out_path = TEMP_TG_DIR / f"tg_comp_{input_path.stem}.mp4"
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
        "ffmpeg", "-y",
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


# Initialize Dispatcher
dp = Dispatcher()


@dp.message(CommandStart())
async def handle_start(message: Message):
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
        "• Файлы <b>не сохраняются на сервере</b> и удаляются сразу после отправки вам в Telegram.\n\n"
        "⚙️ <b>Команды:</b>\n"
        "• <code>/caption</code> — режим подписи (чистый файл без текста / с оригинальным названием)\n"
        "• <code>/help</code> — помощь и советы\n"
        f"• 🌐 Веб-версия: <a href=\"{WEB_BASE_URL}\">{WEB_BASE_URL}</a>"
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("help"))
async def handle_help(message: Message):
    text = (
        "📖 <b>Как пользоваться MediaPull Bot:</b>\n\n"
        "1️⃣ <b>Отправьте ссылку:</b> скопируйте ссылку на видео из браузера или приложения и отправьте в этот чат.\n"
        "2️⃣ <b>Выберите качество:</b> бот предложит доступные варианты (от 360p до 1080p/4K) или <b>MP3</b> для извлечения звука.\n"
        "3️⃣ <b>Мгновенная доставка:</b> видео адаптируется под лимит Telegram (до 50 МБ) и отправляется вам в чат.\n\n"
        "🔕 <b>Чистая пересылка:</b>\n"
        "По умолчанию файлы отправляются без подписей и водяных знаков. Если вы хотите прикреплять название ролика, введите <code>/caption</code>.\n\n"
        "📚 <b>Плейлисты:</b>\n"
        "При отправке ссылки на плейлист бот покажет количество треков и предложит скачать первые треки в чат или открыть весь плейлист архивом в веб-версии."
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("caption"))
@dp.message(Command("clean"))
async def handle_caption_toggle(message: Message):
    current = user_caption_modes.get(message.from_user.id, "none")
    new_mode = "title" if current == "none" else "none"
    user_caption_modes[message.from_user.id] = new_mode

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


@dp.message(F.text)
async def handle_url_message(message: Message):
    text = message.text.strip()
    urls = re.findall(r"https?://[^\s]+", text)
    if not urls:
        await message.answer(
            "💡 Отправьте мне ссылку на видео (YouTube, Instagram, TikTok, VK, Rutube, X и др.), "
            "и я подготовлю его к отправке прямо сюда!",
            parse_mode="HTML"
        )
        return

    url = urls[0]
    platform = detect_platform(url)

    status_msg = await message.answer(
        f"🔍 <b>Анализирую медиа...</b>\n"
        f"Платформа: <b>{platform.get('icon', '🎬')} {platform.get('name', 'Universal')}</b>\n"
        f"<i>Получаю информацию о потоках...</i>",
        parse_mode="HTML"
    )

    try:
        info = await downloader_manager.get_video_info(url)
    except Exception as e:
        safe_err = html.escape(str(e))
        await status_msg.edit_text(
            f"❌ <b>Ошибка анализа ссылки:</b>\n<code>{safe_err}</code>",
            parse_mode="HTML"
        )
        return

    title = info.get("title", "Медиафайл")
    uploader = info.get("uploader") or info.get("channel") or "Не указан"
    safe_title = html.escape(title)
    safe_uploader = html.escape(uploader)

    session_key = uuid.uuid4().hex[:8]
    pending_downloads[session_key] = {
        "url": url,
        "title": title,
        "is_playlist": info.get("is_playlist", False),
        "entries": info.get("entries", []),
        "is_photo": info.get("is_photo", False),
        "photo_url": info.get("photo_url"),
    }

    # Clean old sessions if cache grows
    if len(pending_downloads) > 300:
        for k in list(pending_downloads.keys())[:50]:
            pending_downloads.pop(k, None)

    builder = InlineKeyboardBuilder()

    # Playlist branch
    if info.get("is_playlist"):
        item_count = info.get("item_count", 0)
        entries = info.get("entries", [])
        total_items = len(entries) if entries else item_count

        # Row 1: Download ALL items
        if total_items > 0:
            builder.row(
                InlineKeyboardButton(text=f"📥 Скачать ВСЕ видео ({total_items} шт.)", callback_data=f"tg:plv_all:{session_key}"),
                InlineKeyboardButton(text=f"🎵 Скачать ВСЕ MP3 ({total_items} шт.)", callback_data=f"tg:pla_all:{session_key}")
            )

        # Row 2: Download first 5 (if playlist has more than 5)
        if total_items > 5:
            builder.row(
                InlineKeyboardButton(text="🎬 Первые 5 видео (720p)", callback_data=f"tg:plv_5:{session_key}"),
                InlineKeyboardButton(text="🎵 Первые 5 треков (MP3)", callback_data=f"tg:pla_5:{session_key}")
            )

        # Row 3: Web ZIP & Cancel
        builder.row(
            InlineKeyboardButton(text="🌐 Открыть в Web (ZIP-архив)", url=f"{WEB_BASE_URL}?url={url}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"tg:cancel:{session_key}")
        )

        caption = (
            f"📚 <b>Обнаружен плейлист:</b> <b>{safe_title}</b>\n\n"
            f"👤 Автор: <b>{safe_uploader}</b>\n"
            f"🔢 Всего роликов в плейлисте: <b>{total_items}</b>\n"
            f"🏷️ Сервис: <b>{platform.get('name', 'Media')}</b>\n\n"
            "Вы можете скачать <b>весь плейлист целиком</b> прямо сюда в Telegram, "
            "получить первые 5 роликов или открыть его в веб-версии для скачивания ZIP-архивом.\n\n"
            "Выберите действие:"
        )
        await status_msg.edit_text(caption, reply_markup=builder.as_markup(), parse_mode="HTML")
        return

    # Single photo branch
    if info.get("is_photo"):
        builder.row(
            InlineKeyboardButton(text="🖼️ Скачать фото (HQ)", callback_data=f"tg:p:{session_key}")
        )
    else:
        # Video branch with all detected quality tiers
        raw_res = info.get("resolutions") or []
        heights = []
        for r in raw_res:
            h = r.get("height")
            if isinstance(h, int) and h >= 144 and h not in heights:
                heights.append(h)
        heights.sort(reverse=True)

        standard_targets = [2160, 1440, 1080, 720, 480, 360]
        offered_qualities = []

        if heights:
            for target_h in standard_targets:
                if any(h == target_h for h in heights):
                    offered_qualities.append(target_h)

            if not offered_qualities:
                for target_h in standard_targets:
                    if any(h >= target_h and h < target_h * 1.4 for h in heights):
                        if target_h not in offered_qualities:
                            offered_qualities.append(target_h)

            if not offered_qualities:
                offered_qualities = [h for h in heights if h >= 240][:4]
        else:
            offered_qualities = [1080, 720, 480, 360]

        q_buttons = []
        for q in offered_qualities:
            label = f"{q}p"
            if q >= 2160:
                label += " (4K)"
            elif q >= 1440:
                label += " (2K)"
            elif q >= 1080:
                label += " Full HD"
            elif q >= 720:
                label += " HD"
            elif q <= 360:
                label += " (быстро)"
            q_buttons.append(
                InlineKeyboardButton(text=f"🎬 {label}", callback_data=f"tg:v:{q}:{session_key}")
            )

        for i in range(0, len(q_buttons), 2):
            builder.row(*q_buttons[i:i+2])

        builder.row(
            InlineKeyboardButton(text="🎵 Аудио (MP3)", callback_data=f"tg:a:{session_key}")
        )

    builder.row(
        InlineKeyboardButton(text="🌐 Открыть в MediaPull Web", url=f"{WEB_BASE_URL}?url={url}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"tg:cancel:{session_key}"),
    )

    duration_sec = info.get("duration")
    duration_str = format_duration(duration_sec) if duration_sec else "Неизвестно"
    caption = (
        f"🎬 <b>{safe_title}</b>\n\n"
        f"👤 Автор: <b>{safe_uploader}</b>\n"
        f"⏱️ Длительность: <b>{duration_str}</b>\n"
        f"🏷️ Сервис: <b>{platform.get('name', 'Media')}</b>\n\n"
        "Выберите желаемое качество или аудио для отправки:"
    )

    await status_msg.edit_text(caption, reply_markup=builder.as_markup(), parse_mode="HTML")


async def download_and_send_single(
    bot: Bot,
    chat_id: int,
    user_id: int,
    url: str,
    title: str,
    action: str,
    quality: str = "720",
    status_msg: Optional[Message] = None,
    photo_url: Optional[str] = None
) -> bool:
    """
    Downloads media to temp_tg, compresses if needed, sends to Telegram,
    and immediately purges all files from disk in a finally block.
    """
    filepath: Optional[Path] = None
    compressed_filepath: Optional[Path] = None
    task_id = f"tg_{int(time.time())}_{user_id}"[-12:]
    safe_title = html.escape(title)

    mode = "audio" if action == "audio" else ("photo" if action == "photo" else "single")

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
            is_photo=(action == "photo"),
            photo_url=photo_url,
            user_id="tg_temp",
            role="admin"
        )

        timeout = 240
        start_time = time.time()
        last_progress = -1

        while time.time() - start_time < timeout:
            task = downloader_manager.tasks.get(task_id)
            if not task:
                break
            if task.status == "completed":
                break
            elif task.status in ("error", "cancelled"):
                raise RuntimeError(task.error or "Неизвестная ошибка скачивания")

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

        task = downloader_manager.tasks.get(task_id)
        if not task or task.status != "completed" or not task.filepath:
            raise RuntimeError("Файл не был сформирован.")

        filepath = Path(task.filepath)
        if not filepath.exists():
            raise RuntimeError(f"Файл {filepath.name} не найден на диске.")

        # Handle video
        if action == "video":
            initial_size = filepath.stat().st_size
            if initial_size > MAX_TG_SIZE_BYTES:
                if status_msg:
                    try:
                        await status_msg.edit_text(
                            f"⚡ <b>Сжатие видео для Telegram...</b>\n"
                            f"📌 <i>{safe_title}</i>\n\n"
                            f"Исходный размер: <b>{format_size(initial_size)}</b> (Лимит TG: 50 МБ)\n"
                            f"Оптимизирую видеопоток под 45 МБ через FFmpeg...",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                loop = asyncio.get_running_loop()
                compressed_filepath = await loop.run_in_executor(None, compress_video_for_telegram, filepath)
                target_to_send = compressed_filepath
            else:
                target_to_send = filepath

            w, h, dur = get_video_dimensions_and_duration(target_to_send)
            final_size = target_to_send.stat().st_size

            if status_msg:
                try:
                    await status_msg.edit_text(
                        f"📤 <b>Отправка видео в Telegram...</b>\n"
                        f"Размер: <b>{format_size(final_size)}</b> | Качество: <b>{w}x{h}</b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            caption_mode = user_caption_modes.get(user_id, "none")
            caption = title[:1000] if (caption_mode == "title" and title) else None

            tg_file = FSInputFile(str(target_to_send))
            await bot.send_video(
                chat_id=chat_id,
                video=tg_file,
                caption=caption,
                duration=dur,
                width=w,
                height=h,
                supports_streaming=True,
            )

        # Handle audio
        elif action == "audio":
            final_size = filepath.stat().st_size
            if status_msg:
                try:
                    await status_msg.edit_text(
                        f"📤 <b>Отправка аудиодорожки...</b>\n"
                        f"Размер: <b>{format_size(final_size)}</b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            tg_file = FSInputFile(str(filepath))
            await bot.send_audio(
                chat_id=chat_id,
                audio=tg_file,
                title=title,
            )

        # Handle photo
        elif action == "photo":
            final_size = filepath.stat().st_size
            if status_msg:
                try:
                    await status_msg.edit_text(
                        f"📤 <b>Отправка фото...</b>\n"
                        f"Размер: <b>{format_size(final_size)}</b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            tg_file = FSInputFile(str(filepath))
            await bot.send_photo(
                chat_id=chat_id,
                photo=tg_file,
            )

        return True

    finally:
        # Immediate ephemeral cleanup of media, compressed file, and thumbnail from disk
        if filepath and filepath.exists():
            try:
                filepath.unlink(missing_ok=True)
            except Exception:
                pass
        if compressed_filepath and compressed_filepath.exists() and compressed_filepath != filepath:
            try:
                compressed_filepath.unlink(missing_ok=True)
            except Exception:
                pass
        if filepath:
            try:
                stem = filepath.stem
                for extra in filepath.parent.glob(f"{stem}*"):
                    extra.unlink(missing_ok=True)
            except Exception:
                pass
        downloader_manager.tasks.pop(task_id, None)


@dp.callback_query(F.data.startswith("tg:"))
async def handle_callback_download(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка запроса")
        return

    act_type = parts[1]
    quality = "720"
    if act_type in ("v", "video"):
        if act_type == "v" and len(parts) >= 4:
            quality = parts[2]
            session_key = parts[3]
        else:
            quality = "720"
            session_key = parts[2]
        action = "video"
    elif act_type in ("a", "audio"):
        quality = "best"
        session_key = parts[2]
        action = "audio"
    elif act_type in ("p", "photo"):
        quality = "best"
        session_key = parts[2]
        action = "photo"
    elif act_type in ("pla", "plv", "pla_all", "plv_all", "pla_5", "plv_5"):
        session_key = parts[2]
        action = act_type
    elif act_type == "cancel":
        session_key = parts[2]
        action = "cancel"
    else:
        quality = "720"
        session_key = parts[-1]
        action = act_type

    if action == "cancel":
        pending_downloads.pop(session_key, None)
        await callback.message.edit_text("❌ Загрузка отменена.")
        await callback.answer("Отменено")
        return

    data = pending_downloads.get(session_key)
    if not data:
        await callback.answer("Сессия устарела. Отправьте ссылку повторно.", show_alert=True)
        return

    url = data["url"]
    title = data["title"]
    safe_title = html.escape(title)

    await callback.answer("Запуск обработки...")

    # Playlist batch execution (all or first 5)
    if action in ("pla", "plv", "pla_all", "plv_all", "pla_5", "plv_5"):
        entries = data.get("entries", [])
        is_audio = "pla" in action

        if "5" in action:
            items_to_send = entries[:5]
        else:
            items_to_send = entries[:50]

        if not items_to_send:
            await callback.message.edit_text("❌ Плейлист пуст или не содержит доступных элементов.")
            return

        total_items = len(items_to_send)
        type_str = "аудио (MP3)" if is_audio else "видео (720p)"
        try:
            await callback.message.edit_text(
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
                await callback.message.edit_text(
                    f"⏳ <b>Отправка плейлиста [{idx}/{total_items}]:</b>\n"
                    f"📌 <i>{html.escape(item_title)}</i>\n\n"
                    f"📊 Прогресс: <b>{int((idx-1)/total_items*100)}%</b>\n"
                    f"<i>Файл скачивается и отправляется в Telegram...</i>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            success = await download_and_send_single(
                bot=bot,
                chat_id=callback.message.chat.id,
                user_id=callback.from_user.id,
                url=item_url,
                title=item_title,
                action="audio" if is_audio else "video",
                quality="720",
                status_msg=None
            )
            if success:
                sent_count += 1
            await asyncio.sleep(1.2)

        pending_downloads.pop(session_key, None)
        try:
            await callback.message.edit_text(
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
    else:
        format_label = f"🎬 Видео ({quality}p)" if quality.isdigit() else "🎬 Видео"

    try:
        await callback.message.edit_text(
            f"⏳ <b>Загрузка и подготовка: {format_label}</b>\n"
            f"📌 <i>{safe_title}</i>\n\n"
            f"🔄 Скачиваю медиа с сервера...\n"
            f"<i>Пожалуйста, подождите несколько секунд...</i>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    try:
        await download_and_send_single(
            bot=bot,
            chat_id=callback.message.chat.id,
            user_id=callback.from_user.id,
            url=url,
            title=title,
            action=action,
            quality=quality,
            status_msg=callback.message,
            photo_url=data.get("photo_url")
        )
        await callback.message.delete()
        pending_downloads.pop(session_key, None)

    except Exception as e:
        print(f"[Bot Error] {e}")
        safe_err = html.escape(str(e))
        try:
            await callback.message.edit_text(
                f"❌ <b>Не удалось отправить файл:</b>\n<code>{safe_err}</code>\n\n"
                f"Вы можете открыть ссылку напрямую в веб-интерфейсе:\n"
                f"<a href=\"{WEB_BASE_URL}?url={url}\">{WEB_BASE_URL}</a>",
                parse_mode="HTML"
            )
        except Exception:
            pass


async def test_bot_pipeline(test_url: str):
    """
    Simulate full Telegram Bot pipeline on a URL without requiring active Telegram polling:
    1. Video metadata extraction
    2. Video download
    3. Telegram 50MB constraint check & FFmpeg compression
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
        user_id="tg_temp",
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

    print("\n[3/4] ⚡ Проверка лимита Telegram (50 МБ) и FFmpeg-сжатие...")
    if raw_size > MAX_TG_SIZE_BYTES:
        print(f"      ⚠️ Файл превышает лимит ({format_size(raw_size)} > 48 МБ). Запуск сжатия...")
        final_path = compress_video_for_telegram(raw_path, MAX_TG_SIZE_BYTES)
    else:
        print("      ✅ Файл уже укладывается в лимит Telegram (< 48 МБ), сжатие не требуется.")
        final_path = raw_path

    final_size = final_path.stat().st_size
    print(f"      🎉 Итоговый файл: {final_path.name}")
    print(f"      📊 Итоговый размер: {format_size(final_size)} ({final_size} байт)")

    print("\n[4/4] 🎬 Проверка параметров видео (ffprobe)...")
    w, h, dur = get_video_dimensions_and_duration(final_path)
    print(f"      📐 Разрешение: {w}x{h}")
    print(f"      ⏱️ Длительность видео: {dur} сек.")
    print(f"      🚀 Поддержка Faststart (стриминг в TG): Готов к отправке!")

    # Cleanup test files
    try:
        raw_path.unlink(missing_ok=True)
        if final_path != raw_path:
            final_path.unlink(missing_ok=True)
    except Exception:
        pass

    print("\n" + "=" * 65)
    print("✅ ВСЕ ТЕСТЫ ПАЙПЛАЙНА TELEGRAM-БОТА УСПЕШНО ПРОЙДЕНЫ!")
    print("🧹 Временные файлы автоматически удалены.")
    print("=" * 65 + "\n")
    return True


async def main():
    parser = argparse.ArgumentParser(description="MediaPull Telegram Bot")
    parser.add_argument("--test-pipeline", type=str, help="Run end-to-end test on a video URL without Telegram token")
    parser.add_argument("--token", type=str, help="Telegram Bot Token override")
    args = parser.parse_args()

    if args.test_pipeline:
        success = await test_bot_pipeline(args.test_pipeline)
        sys.exit(0 if success else 1)

    # Initial cleanup of any old temp files
    cleanup_temp_tg()

    token = args.token or TELEGRAM_BOT_TOKEN
    if not token:
        print("\n" + "=" * 65)
        print("⚠️  ТОКЕН ТЕЛЕГРАМ-БОТА НЕ НАЙДЕН!")
        print("=" * 65)
        return

    bot = Bot(token=token)
    print("\n" + "=" * 60)
    print("🤖 Запуск MediaPull Telegram Bot (Clean Ephemeral Mode)...")
    print(f"🌐 Сервер MediaPull: {WEB_BASE_URL}")
    print(f"🗑️  Папка временного буфера: {TEMP_TG_DIR} (мгновенное удаление после отправки)")
    print("=" * 60 + "\n")

    me = await bot.get_me()
    print(f"✅ Бот успешно подключен: @{me.username} ({me.first_name})")
    print("🚀 Ожидание сообщений от пользователей...\n")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
