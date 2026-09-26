import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

import sys

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

import requests


def get_ytdlp_cmd() -> List[str]:
    """Resolve yt-dlp command safely across virtualenvs and global PATH."""
    scripts_dir = Path(sys.executable).parent
    venv_exe = scripts_dir / ("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
    if venv_exe.exists():
        return [str(venv_exe)]
    which = shutil.which("yt-dlp")
    if which:
        return [which]
    return [sys.executable, "-m", "yt_dlp"]

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
THUMBNAILS_DIR = DOWNLOADS_DIR / ".thumbnails"
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
META_CACHE_FILE = THUMBNAILS_DIR / "meta_cache.json"

_meta_cache: Dict[str, Any] = {}

def _load_meta_cache():
    global _meta_cache
    if META_CACHE_FILE.exists():
        try:
            with open(META_CACHE_FILE, "r", encoding="utf-8") as f:
                _meta_cache = json.load(f)
        except Exception:
            _meta_cache = {}

def _save_meta_cache():
    try:
        with open(META_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_meta_cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

_load_meta_cache()

# Helper for human-readable sizes
def format_size(bytes_val: Optional[int]) -> str:
    if not bytes_val or bytes_val <= 0:
        return "Unknown"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} PB"

def parse_bytes_str(s: Optional[str]) -> Optional[float]:
    if not s or "N/A" in s or "NA" in s:
        return None
    cleaned = s.strip().replace("iB", "B")
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    m = re.match(r"^([\d.]+)\s*([A-Za-z]+)$", cleaned)
    if m:
        val = float(m.group(1))
        unit = m.group(2).upper()
        if unit in units:
            return val * units[unit]
    return None

def format_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return ""
    total = int(round(seconds))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
def normalize_url(url: str) -> str:
    """Normalize URLs across platforms (RuTube, Instagram, TikTok, etc.)."""
    url = (url or "").strip()
    # Instagram: strip tracking tokens (?stkn=, ?igsh=, ?utm_source=)
    if "instagram.com" in url.lower():
        url = re.sub(r'(\/(?:p|reel|reels|tv)\/[A-Za-z0-9_-]+)\/?\?.*$', r'\1/', url)

    # RuTube playlist normalization:
    # 1. URL with query parameter ?playlist=12345 or &playlist=12345
    # 2. URL like rutube.ru/playlists/12345 or rutube.ru/playlist/12345
    rutube_match = re.search(r'rutube\.ru/(?:video/[a-zA-Z0-9_-]+/?\?[^#]*\bplaylist=(\d+)|(?:plst|playlists?)/(\d+))', url)
    if rutube_match:
        plst_id = rutube_match.group(1) or rutube_match.group(2)
        if plst_id:
            return f"https://rutube.ru/plst/{plst_id}"
    return url

def detect_platform(url: str) -> Dict[str, str]:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return {"id": "youtube", "name": "YouTube", "icon": "fa-brands fa-youtube", "color": "#ff0033"}
    if "vk.com" in u or "vkvideo.ru" in u:
        return {"id": "vk", "name": "VK Видео", "icon": "fa-brands fa-vk", "color": "#0077ff"}
    if "rutube.ru" in u:
        return {"id": "rutube", "name": "RuTube", "icon": "fa-solid fa-play", "color": "#00aaff"}
    if "tiktok.com" in u:
        return {"id": "tiktok", "name": "TikTok", "icon": "fa-brands fa-tiktok", "color": "#00f2fe"}
    if "twitch.tv" in u:
        return {"id": "twitch", "name": "Twitch", "icon": "fa-brands fa-twitch", "color": "#9146ff"}
    if "twitter.com" in u or "x.com" in u:
        return {"id": "x", "name": "X / Twitter", "icon": "fa-brands fa-x-twitter", "color": "#ffffff"}
    if "instagram.com" in u:
        return {"id": "instagram", "name": "Instagram", "icon": "fa-brands fa-instagram", "color": "#e1306c"}
    if "soundcloud.com" in u:
        return {"id": "soundcloud", "name": "SoundCloud", "icon": "fa-brands fa-soundcloud", "color": "#ff5500"}
    if "vimeo.com" in u:
        return {"id": "vimeo", "name": "Vimeo", "icon": "fa-brands fa-vimeo-v", "color": "#1ab7ea"}
    return {"id": "generic", "name": "Медиа ресурс", "icon": "fa-solid fa-globe", "color": "#a855f7"}

def instagram_shortcode_to_id(shortcode: str) -> int:
    """Convert Instagram shortcode to media pk ID."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    media_id = 0
    for char in shortcode:
        media_id = media_id * 64 + alphabet.index(char)
    return media_id

def extract_instagram_post(url: str) -> Optional[Dict[str, Any]]:
    """Extract Instagram Post, Reel, Photo, or Carousel via GraphQL with impersonation."""
    m = re.search(r"(?:instagram\.com/(?:p|reel|reels|tv)/)([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    shortcode = m.group(1)
    try:
        media_id = str(instagram_shortcode_to_id(shortcode))
    except Exception:
        return None

    if not cffi_requests:
        return None

    try:
        s = cffi_requests.Session(impersonate="chrome124")
        r_home = s.get("https://www.instagram.com/", headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }, timeout=8)
        lsd_match = re.search(r"\"LSD\",\[\],\{\"token\":\"([^\"]+)\"\}", r_home.text)
        lsd_token = lsd_match.group(1) if lsd_match else ""
        csrf_token = s.cookies.get("csrftoken", "")

        # Call ruling endpoint to establish Instagram session access
        s.get(f"https://www.instagram.com/api/v1/web/get_ruling_for_content/?content_type=MEDIA&target_id={media_id}", headers={
            "X-IG-App-ID": "936619743392459",
            "X-CSRFToken": csrf_token,
        }, timeout=8)
        csrf_token = s.cookies.get("csrftoken", csrf_token)

        post_data = {
            "lsd": lsd_token,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
            "server_timestamps": "true",
            "variables": json.dumps({"media_id": media_id}, separators=(",", ":")),
            "doc_id": "27130156389949648",
        }
        headers = {
            "X-IG-App-ID": "936619743392459",
            "X-FB-Friendly-Name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
            "X-CSRFToken": csrf_token,
            "X-FB-LSD": lsd_token,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/p/{shortcode}/",
        }
        r = s.post("https://www.instagram.com/api/graphql", data=post_data, headers=headers, timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
        target = data.get("data", {}).get("xig_polaris_media", {}).get("if_not_gated_logged_out")
        return target
    except Exception:
        return None

def get_user_root(user_id: Optional[str] = None, role: Optional[str] = None) -> Path:
    """Return root directory for a given user. Creator 'kexiky' or admin maps to DOWNLOADS_DIR."""
    if user_id == "tg_temp":
        temp_root = DOWNLOADS_DIR / "temp_tg"
        temp_root.mkdir(parents=True, exist_ok=True)
        return temp_root
    if not user_id or user_id == "kexiky" or role == "admin":
        return DOWNLOADS_DIR
    clean_uid = re.sub(r"[^a-zA-Z0-9_-]", "", user_id)
    if clean_uid.startswith("guest_"):
        guest_root = DOWNLOADS_DIR / "guests" / clean_uid
        guest_root.mkdir(parents=True, exist_ok=True)
        return guest_root
    else:
        user_root = DOWNLOADS_DIR / "users" / clean_uid
        user_root.mkdir(parents=True, exist_ok=True)
        return user_root


def get_user_disk_usage(user_id: Optional[str] = "kexiky", role: Optional[str] = None) -> Dict[str, Any]:
    """Calculate total bytes used and file count for a user."""
    if user_id == "tg_temp":
        return {"total_bytes": 0, "file_count": 0, "total_size_str": "0 B"}
    user_root = get_user_root(user_id, role)
    if not user_root.exists():
        return {"total_bytes": 0, "file_count": 0, "total_size_str": "0 B"}

    is_kexiky = (not user_id or user_id == "kexiky" or role == "admin")
    total_bytes = 0
    file_count = 0
    for p in user_root.rglob("*"):
        if is_kexiky and any(part in ["guests", "users"] for part in p.parts):
            continue
        if p.is_file() and not p.name.startswith("."):
            try:
                total_bytes += p.stat().st_size
                file_count += 1
            except Exception:
                pass

    return {
        "total_bytes": total_bytes,
        "file_count": file_count,
        "total_size_str": format_size(total_bytes)
    }


def get_resolution_tier(w: Optional[int], h: Optional[int]) -> int:
    """
    Classify video dimensions into standard resolution tiers (supporting standard 16:9,
    cinematic widescreen 2:1 / 21:9, and vertical 9:16 / 4:5 reels).
    """
    if not h:
        return 0
    w = w or 0
    max_d = max(w, h)
    min_d = min(w, h) if w else h
    if min_d >= 1800 or max_d >= 3600:
        return 2160
    if min_d >= 1200 or max_d >= 2400:
        return 1440
    if min_d >= 800 or max_d >= 1800:
        return 1080
    if min_d >= 540 or max_d >= 1200:
        return 720
    if min_d >= 400 or max_d >= 800:
        return 480
    if min_d >= 270 or max_d >= 580:
        return 360
    return min_d


def resolve_download_path(rel_path: Optional[str], user_id: Optional[str] = None) -> Optional[Path]:
    """Safely resolve a relative path inside the user's root directory, preventing directory traversal."""
    if not rel_path:
        return None
    import urllib.parse
    user_root = get_user_root(user_id).resolve()

    # Generate candidate paths (raw, unquoted, double-unquoted)
    candidates = [rel_path]
    unq = urllib.parse.unquote(rel_path)
    if unq != rel_path:
        candidates.append(unq)
    unq2 = urllib.parse.unquote(unq)
    if unq2 not in candidates:
        candidates.append(unq2)

    for cand in candidates:
        safe_rel = cand.strip("/\\")
        try:
            resolved = (user_root / safe_rel).resolve()
            try:
                is_safe = resolved.is_relative_to(user_root)
            except AttributeError:
                is_safe = (user_root in resolved.parents or resolved == user_root)

            if is_safe and resolved.exists():
                return resolved
        except Exception:
            pass

    # Fallback to unquoted resolved path even if not existing yet (e.g. for new output files)
    try:
        resolved = (user_root / unq.strip("/\\")).resolve()
        try:
            is_safe = resolved.is_relative_to(user_root)
        except AttributeError:
            is_safe = (user_root in resolved.parents or resolved == user_root)

        if is_safe:
            return resolved
    except Exception:
        pass
    return None


def get_file_metadata(filepath: Path, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    global _meta_cache
    if base_dir is None:
        base_dir = DOWNLOADS_DIR
    try:
        rel_path = str(filepath.relative_to(base_dir))
    except Exception:
        rel_path = filepath.name

    fn = rel_path
    try:
        stat = filepath.stat()
        mtime = stat.st_mtime
        size_bytes = stat.st_size
    except Exception:
        mtime = 0
        size_bytes = 0

    if fn in _meta_cache and _meta_cache[fn].get("mtime") == mtime:
        cached = dict(_meta_cache[fn])
        cached["size_bytes"] = size_bytes
        cached["size_str"] = format_size(size_bytes)
        if "playlist_index" not in cached or cached["playlist_index"] is None:
            p_idx = None
            if filepath.parent != base_dir:
                m_idx = re.match(r"^(\d+)[\.\s_-]", filepath.name)
                if m_idx:
                    try:
                        p_idx = int(m_idx.group(1))
                    except ValueError:
                        pass
            cached["playlist_index"] = p_idx
            _meta_cache[fn]["playlist_index"] = p_idx
        return cached

    # Parse title and bracketed tag from filename
    m = re.search(r"^(.*?)(?:\s*\[([^\]]+)\])?\.[^.]+$", filepath.name)
    clean_title = m.group(1).strip() if m else filepath.stem
    bracket_tag = m.group(2).strip() if (m and m.group(2)) else ""

    video_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".gif"}
    audio_exts = {".mp3", ".m4a", ".wav", ".flac", ".opus", ".aac", ".ogg"}
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}
    ext = filepath.suffix.lower()
    is_video = ext in video_exts
    is_audio = ext in audio_exts
    is_image = ext in image_exts

    duration = 0.0
    height = None
    width = None

    if is_image:
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(filepath)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5
            )
            if proc.returncode == 0 and proc.stdout:
                data = json.loads(proc.stdout)
                streams = data.get("streams", [])
                if streams:
                    width = streams[0].get("width")
                    height = streams[0].get("height")
        except Exception:
            pass
    elif is_video or is_audio:
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height,codec_type", "-of", "json", str(filepath)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8
            )
            if proc.returncode == 0 and proc.stdout:
                data = json.loads(proc.stdout)
                dur_val = data.get("format", {}).get("duration")
                if dur_val:
                    duration = float(dur_val)
                for st in data.get("streams", []):
                    if st.get("codec_type") == "video" and st.get("height"):
                        height = int(st["height"])
                        width = int(st.get("width") or 0) or None
                        break
        except Exception:
            pass

    # Determine quality tag
    if is_image:
        quality_tag = f"{width}×{height}" if width and height else ext.replace(".", "").upper()
    elif ext == ".gif":
        quality_tag = "GIF"
    elif is_audio:
        if "kbps" in bracket_tag or "k" in bracket_tag:
            quality_tag = bracket_tag
        else:
            quality_tag = ext.replace(".", "").upper()
    else:
        if bracket_tag and ("p" in bracket_tag.lower() or "k" in bracket_tag.lower()):
            quality_tag = "4K" if "2160" in bracket_tag else bracket_tag
        elif height:
            if height >= 2160:
                quality_tag = "4K"
            elif height >= 1440:
                quality_tag = "1440p"
            elif height >= 1080:
                quality_tag = "1080p"
            elif height >= 720:
                quality_tag = "720p"
            elif height >= 480:
                quality_tag = "480p"
            else:
                quality_tag = f"{height}p"
        else:
            quality_tag = ext.replace(".", "").upper()

    duration_str = "ФОТО" if is_image else format_duration(duration)

    playlist_title = None
    playlist_folder = None
    is_in_playlist = False
    playlist_index = None
    if filepath.parent != DOWNLOADS_DIR:
        is_in_playlist = True
        playlist_folder = filepath.parent.name
        playlist_title = playlist_folder
        idx_match = re.match(r"^(\d+)[\.\s_-]", filepath.name)
        if idx_match:
            try:
                playlist_index = int(idx_match.group(1))
            except ValueError:
                pass
        meta_file = filepath.parent / ".playlist_meta.json"
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as pf:
                    pdata = json.load(pf)
                    if pdata.get("title"):
                        playlist_title = pdata["title"]
            except Exception:
                pass

    info = {
        "filename": rel_path,
        "basename": filepath.name,
        "title": clean_title or filepath.name,
        "quality_tag": quality_tag,
        "duration_seconds": round(duration, 1),
        "duration_str": duration_str,
        "thumbnail_url": f"/api/thumbnail/{rel_path}",
        "size_bytes": size_bytes,
        "size_str": format_size(size_bytes),
        "created_at": mtime,
        "ext": ext.replace(".", "").upper(),
        "is_video": is_video,
        "is_audio": is_audio,
        "is_image": is_image,
        "is_photo": is_image,
        "width": width,
        "height": height,
        "mtime": mtime,
        "playlist_name": playlist_title,
        "playlist_folder": playlist_folder,
        "is_in_playlist": is_in_playlist,
        "playlist_index": playlist_index
    }
    _meta_cache[fn] = info
    _save_meta_cache()
    return info

def get_or_create_thumbnail(filename: str, user_id: Optional[str] = None) -> Optional[Path]:
    filepath = resolve_download_path(filename, user_id)
    if not filepath or not filepath.exists():
        return None

    # 1. Directory requested (e.g. playlist folder thumbnail)
    if filepath.is_dir():
        cover_path = filepath / ".cover.jpg"
        if cover_path.exists() and cover_path.stat().st_size > 500:
            return cover_path
        # Look for companion images or existing images
        for cand in sorted(filepath.glob("*")):
            if cand.is_file() and not cand.name.startswith(".") and cand.suffix.lower() in [".webp", ".jpg", ".png"]:
                try:
                    subprocess.run(["ffmpeg", "-y", "-i", str(cand), "-vf", "scale=640:-1", str(cover_path)], capture_output=True, timeout=5)
                    if cover_path.exists() and cover_path.stat().st_size > 500:
                        return cover_path
                except Exception:
                    pass
        # Fallback to first video thumbnail
        user_root = get_user_root(user_id)
        for v in sorted(filepath.glob("*")):
            if v.is_file() and not v.name.startswith(".") and v.suffix.lower() in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv"}:
                t = get_or_create_thumbnail(str(v.relative_to(user_root)), user_id=user_id)
                if t and t.exists():
                    try:
                        shutil.copyfile(t, cover_path)
                        return cover_path
                    except Exception:
                        pass
        return None

    user_prefix = user_id or "kexiky"
    safe_key = hashlib.md5(f"{user_prefix}:{filename.strip('/\\')}".encode("utf-8")).hexdigest()
    thumb_path = THUMBNAILS_DIR / f"{safe_key}.jpg"
    if thumb_path.exists() and thumb_path.stat().st_size > 1500:
        return thumb_path

    # If it is an explicit cover file inside a subfolder
    if filepath.name.startswith(".cover"):
        return filepath

    # Check companion webp or jpg in the same folder as the media
    stem = filepath.stem
    parent_dir = filepath.parent
    for cand in parent_dir.iterdir():
        if cand.is_file() and cand.name.startswith(stem) and cand.suffix.lower() in [".webp", ".jpg", ".png"] and cand != filepath:
            if cand.stat().st_size > 2000:
                try:
                    subprocess.run(["ffmpeg", "-y", "-i", str(cand), "-vf", "scale=640:-1", str(thumb_path)], capture_output=True, timeout=5)
                    if thumb_path.exists() and thumb_path.stat().st_size > 1500:
                        return thumb_path
                except Exception:
                    pass

    ext = filepath.suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}
    video_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv"}
    audio_exts = {".mp3", ".m4a", ".wav", ".flac", ".opus", ".aac", ".ogg"}

    if ext in image_exts:
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(filepath), "-vf", "scale='min(640,iw)':-1", str(thumb_path)],
                capture_output=True,
                timeout=5
            )
            if thumb_path.exists() and thumb_path.stat().st_size > 0:
                return thumb_path
        except Exception:
            pass
        return filepath

    if ext in video_exts:
        dur = 0.0
        meta = _meta_cache.get(filename) or _meta_cache.get(filepath.name)
        if meta and meta.get("duration_seconds"):
            dur = float(meta["duration_seconds"])
        else:
            try:
                proc = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(filepath)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5
                )
                if proc.stdout.strip():
                    dur = float(proc.stdout.strip())
            except Exception:
                pass

        seek = min(5.0, dur * 0.15) if dur > 5.0 else max(0.2, dur * 0.4)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{seek:.2f}", "-i", str(filepath), "-vframes", "1", "-vf", "scale=640:-1", str(thumb_path)],
                capture_output=True,
                timeout=5
            )
            if not thumb_path.exists() or thumb_path.stat().st_size < 1500:
                seek2 = min(15.0, dur * 0.25) if dur > 10 else 0.5
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", f"{seek2:.2f}", "-i", str(filepath), "-vframes", "1", "-vf", "scale=640:-1", str(thumb_path)],
                    capture_output=True,
                    timeout=5
                )
            if thumb_path.exists() and thumb_path.stat().st_size > 0:
                return thumb_path
        except Exception:
            pass

    elif ext in audio_exts:
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(filepath), "-an", "-vcodec", "copy", str(thumb_path)],
                capture_output=True,
                timeout=5
            )
            if thumb_path.exists() and thumb_path.stat().st_size > 1000:
                return thumb_path
            elif thumb_path.exists():
                thumb_path.unlink(missing_ok=True)
        except Exception:
            pass

    return None


class TaskState:
    def __init__(self, task_id: str, url: str, title: str = "", format_tag: str = "", playlist_title: Optional[str] = None, user_id: Optional[str] = "kexiky", role: str = "guest"):
        self.id = task_id
        self.url = url
        self.title = title
        self.format_tag = format_tag
        self.playlist_title = playlist_title
        self.user_id = user_id or "kexiky"
        self.role = role or "guest"
        self.status = "queued"  # queued, downloading, converting, completed, error, cancelled
        self.progress = 0.0
        self.speed = "0 B/s"
        self.eta = "--:--"
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.downloaded_str = "0 B"
        self.total_str = ""
        self.filename: Optional[str] = None
        self.filepath: Optional[str] = None
        self.error: Optional[str] = None
        self.logs: List[str] = []
        self.created_at = time.time()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.async_task: Optional[asyncio.Task] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "format_tag": self.format_tag,
            "status": self.status,
            "progress": round(self.progress, 1),
            "speed": self.speed,
            "eta": self.eta,
            "downloaded_str": self.downloaded_str,
            "total_str": self.total_str,
            "filename": self.filename,
            "error": self.error,
            "logs": self.logs[-20:],  # last 20 log lines
            "created_at": self.created_at,
            "user_id": self.user_id,
        }


class DownloaderManager:
    def __init__(self):
        self.tasks: Dict[str, TaskState] = {}
        self._semaphore = None

    @property
    def semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            max_c = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
            self._semaphore = asyncio.Semaphore(max(1, max_c))
        return self._semaphore

    def prune_old_tasks(self, max_age_seconds: int = 7200):
        now = time.time()
        to_del = [
            tid for tid, t in self.tasks.items()
            if t.status in ["completed", "error", "cancelled"] and (now - t.created_at > max_age_seconds)
        ]
        for tid in to_del:
            self.tasks.pop(tid, None)

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        self.prune_old_tasks()
        return [task.to_dict() for task in sorted(self.tasks.values(), key=lambda t: t.created_at, reverse=True)]

    def clear_finished_tasks(self) -> int:
        to_delete = [
            tid for tid, task in self.tasks.items()
            if task.status in ["completed", "error", "cancelled"]
        ]
        for tid in to_delete:
            del self.tasks[tid]
        return len(to_delete)

    async def get_video_info(self, url: str) -> Dict[str, Any]:
        """Extract rich video, photo, carousel, or playlist metadata using yt-dlp and GraphQL."""
        normalized_url = normalize_url(url)
        platform_info = detect_platform(url)

        # 1. Instagram extraction for Photos, Carousels, and Reels
        if "instagram.com" in url.lower():
            try:
                ig_data = await asyncio.to_thread(extract_instagram_post, url)
                if ig_data:
                    user = ig_data.get("user") or {}
                    username = user.get("username", "instagram")
                    caption_obj = ig_data.get("caption") or {}
                    caption = caption_obj.get("text", "").strip()
                    shortcode = ig_data.get("code") or "instagram"
                    first_line = caption.split("\n")[0][:100].strip() if caption else ""

                    # Carousel / Album
                    carousel_slides = ig_data.get("carousel_media") or []
                    if carousel_slides:
                        entries = []
                        for idx, slide in enumerate(carousel_slides, 1):
                            stype = slide.get("media_type")
                            is_slide_photo = stype == 1 or not slide.get("video_versions")
                            cands = slide.get("image_versions2", {}).get("candidates", [])
                            slide_thumb = cands[0]["url"] if cands else ""
                            if is_slide_photo:
                                direct_url = slide_thumb
                            else:
                                vids = slide.get("video_versions") or []
                                direct_url = vids[0]["url"] if vids else slide_thumb

                            entries.append({
                                "id": f"{shortcode}_{idx}",
                                "title": f"Слайд {idx}: " + ("Фото" if is_slide_photo else "Видео"),
                                "duration": slide.get("video_duration"),
                                "duration_str": format_duration(slide.get("video_duration")) if not is_slide_photo else "ФОТО",
                                "url": direct_url,
                                "direct_url": direct_url,
                                "thumbnail": slide_thumb,
                                "uploader": f"@{username}",
                                "is_photo": is_slide_photo,
                                "is_video": not is_slide_photo,
                                "width": slide.get("original_width"),
                                "height": slide.get("original_height"),
                            })

                        first_thumb = entries[0]["thumbnail"] if entries else ""
                        return {
                            "is_playlist": True,
                            "is_carousel": True,
                            "is_photo": False,
                            "platform": platform_info,
                            "title": f"Instagram: @{username}" + (f" — {first_line}" if first_line else ""),
                            "uploader": f"@{username}",
                            "thumbnail": first_thumb,
                            "item_count": len(entries),
                            "entries": entries
                        }

                    # Single Photo
                    media_type = ig_data.get("media_type")
                    has_video = bool(ig_data.get("video_versions"))
                    if media_type == 1 or not has_video:
                        cands = ig_data.get("image_versions2", {}).get("candidates", [])
                        photo_url = cands[0]["url"] if cands else ig_data.get("display_uri", "")
                        w = ig_data.get("original_width") or (cands[0].get("width") if cands else 1080)
                        h = ig_data.get("original_height") or (cands[0].get("height") if cands else 1080)
                        title = first_line or f"Фото @{username} [{shortcode}]"

                        return {
                            "is_playlist": False,
                            "is_photo": True,
                            "is_video": False,
                            "platform": platform_info,
                            "id": shortcode,
                            "title": title,
                            "uploader": f"@{username}",
                            "duration": 0,
                            "duration_str": "ФОТО",
                            "thumbnail": photo_url,
                            "photo_url": photo_url,
                            "width": w,
                            "height": h,
                            "resolution_str": f"{w} × {h} (Оригинал)" if w and h else "Высокое разрешение",
                            "description": caption[:300] if caption else "",
                            "view_count": ig_data.get("like_count") or 0,
                            "original_url": url,
                            "resolutions": [],
                            "chapters": [],
                            "subtitles": []
                        }
            except Exception:
                pass

        cmd = get_ytdlp_cmd() + [
            "--dump-single-json",
            "--no-warnings",
            "--no-update",
            "--geo-bypass",
            "--flat-playlist",
            normalized_url
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            # If Instagram video and yt-dlp failed, try direct video stream from GraphQL
            if "instagram.com" in url.lower():
                try:
                    ig_data = await asyncio.to_thread(extract_instagram_post, url)
                    if ig_data and ig_data.get("video_versions"):
                        vids = ig_data["video_versions"]
                        cands = ig_data.get("image_versions2", {}).get("candidates", [])
                        thumb = cands[0]["url"] if cands else ""
                        username = (ig_data.get("user") or {}).get("username", "instagram")
                        caption = (ig_data.get("caption") or {}).get("text", "").strip()
                        first_line = caption.split("\n")[0][:100].strip() if caption else ""
                        shortcode = ig_data.get("code") or "reel"
                        h = ig_data.get("original_height") or 1080
                        return {
                            "is_playlist": False,
                            "is_photo": False,
                            "is_video": True,
                            "platform": platform_info,
                            "id": shortcode,
                            "title": first_line or f"Reel @{username} [{shortcode}]",
                            "uploader": f"@{username}",
                            "duration": ig_data.get("video_duration") or 0,
                            "duration_str": format_duration(ig_data.get("video_duration")),
                            "thumbnail": thumb,
                            "video_url": vids[0]["url"],
                            "description": caption[:300] if caption else "",
                            "resolutions": [{"height": h, "label": f"{h}p (Instagram HD)"}],
                            "chapters": [],
                            "subtitles": [],
                            "original_url": url
                        }
                except Exception:
                    pass
            raise ValueError(err_msg or "Failed to extract video information")

        try:
            raw_data = json.loads(stdout.decode(errors="replace"))
        except Exception as e:
            raise ValueError(f"Failed to parse video info: {str(e)}")

        is_playlist = raw_data.get("_type") == "playlist" or "entries" in raw_data

        if is_playlist:
            entries = []
            is_rutube = "rutube.ru" in normalized_url or "rutube.ru" in url
            is_vk = "vk.com" in normalized_url or "vkvideo.ru" in normalized_url

            for item in (raw_data.get("entries") or []):
                if not item:
                    continue
                item_url = item.get("url")
                item_id = item.get("id")
                if not item_url and item_id:
                    if is_rutube:
                        item_url = f"https://rutube.ru/video/{item_id}/"
                    elif is_vk:
                        item_url = f"https://vk.com/video{item_id}"
                    else:
                        item_url = f"https://www.youtube.com/watch?v={item_id}"

                # RuTube flat playlist entries return 'thumbnail' string directly
                thumb = item.get("thumbnail")
                if not thumb and item.get("thumbnails"):
                    thumb = (item.get("thumbnails") or [{}])[-1].get("url")

                entries.append({
                    "id": item_id,
                    "title": item.get("title") or "Без названия",
                    "duration": item.get("duration"),
                    "duration_str": format_duration(item.get("duration")),
                    "url": item_url or url,
                    "thumbnail": thumb or "",
                    "uploader": item.get("uploader") or item.get("channel") or "Unknown"
                })

            playlist_title = raw_data.get("title")
            if not playlist_title or playlist_title == "None":
                if entries and entries[0].get("uploader") and entries[0]["uploader"] != "Unknown":
                    playlist_title = f"Плейлист: {entries[0]['uploader']}"
                elif is_rutube:
                    playlist_title = "Плейлист RuTube"
                else:
                    playlist_title = "Плейлист"

            playlist_thumb = raw_data.get("thumbnail")
            if not playlist_thumb and raw_data.get("thumbnails"):
                playlist_thumb = raw_data.get("thumbnails")[-1].get("url")
            if not playlist_thumb and entries:
                playlist_thumb = entries[0]["thumbnail"]

            return {
                "is_playlist": True,
                "platform": platform_info,
                "title": playlist_title,
                "uploader": raw_data.get("uploader") or raw_data.get("channel") or (entries[0]["uploader"] if entries else ""),
                "thumbnail": playlist_thumb or "",
                "item_count": len(entries),
                "entries": entries[:200]  # Return up to first 200 items for UI
            }

        # Regular single video
        thumbnails = raw_data.get("thumbnails") or []
        thumbnail_url = thumbnails[-1].get("url") if thumbnails else raw_data.get("thumbnail", "")

        # Extract available heights / resolutions (supporting 16:9, widescreen 2:1/21:9 & vertical)
        detected_tiers = set()
        formats = raw_data.get("formats") or []
        for f in formats:
            h = f.get("height")
            w = f.get("width")
            vcodec = f.get("vcodec", "")
            ext = f.get("ext", "")
            if h and isinstance(h, int) and h >= 144 and vcodec and vcodec != "none" and ext != "mhtml":
                tier = get_resolution_tier(w, h)
                if tier:
                    detected_tiers.add(tier)

        sorted_heights = sorted(list(detected_tiers), reverse=True)

        # Standard preset resolutions
        resolution_options = []
        for h in sorted_heights:
            label = f"{h}p"
            if h >= 4320:
                label += " (8K Ultra HD)"
            elif h >= 2160:
                label += " (4K Ultra HD)"
            elif h >= 1440:
                label += " (2K Quad HD)"
            elif h >= 1080:
                label += " (Full HD 1080p)"
            elif h >= 720:
                label += " (HD 720p)"
            resolution_options.append({"height": h, "label": label})

        # Extract subtitles
        subs_list = []
        subtitles = raw_data.get("subtitles") or {}
        auto_subs = raw_data.get("automatic_captions") or {}
        all_sub_keys = set(list(subtitles.keys()) + list(auto_subs.keys()))
        for lang in sorted(all_sub_keys):
            name = lang
            if lang in subtitles and subtitles[lang]:
                name = subtitles[lang][0].get("name", lang)
            elif lang in auto_subs and auto_subs[lang]:
                name = auto_subs[lang][0].get("name", f"{lang} (авто)")
            subs_list.append({"code": lang, "name": name})

        # Extract chapters
        raw_chapters = raw_data.get("chapters") or []
        formatted_chapters = []
        for idx, ch in enumerate(raw_chapters, 1):
            s = ch.get("start_time", 0.0)
            e = ch.get("end_time", 0.0)
            formatted_chapters.append({
                "title": ch.get("title") or f"Глава {idx}",
                "start_time": s,
                "start_str": format_duration(s),
                "end_time": e,
                "end_str": format_duration(e)
            })

        return {
            "is_playlist": False,
            "platform": platform_info,
            "id": raw_data.get("id"),
            "title": raw_data.get("title"),
            "uploader": raw_data.get("uploader") or raw_data.get("channel") or "Неизвестный канал",
            "duration": raw_data.get("duration", 0),
            "duration_str": format_duration(raw_data.get("duration")),
            "view_count": raw_data.get("view_count", 0),
            "upload_date": raw_data.get("upload_date"),
            "thumbnail": thumbnail_url,
            "description": (raw_data.get("description") or "")[:300],
            "resolutions": resolution_options,
            "chapters": formatted_chapters,
            "subtitles": subs_list[:30],  # popular subtitles
            "original_url": raw_data.get("webpage_url") or url
        }

    async def _save_playlist_cover(self, target_dir: Path, thumb_url: str):
        cover_file = target_dir / ".cover.jpg"
        if cover_file.exists() and cover_file.stat().st_size > 1000:
            return
        def download_cover():
            try:
                content = None
                s = cffi_requests.Session(impersonate="chrome124") if cffi_requests else None
                if s:
                    r = s.get(thumb_url, timeout=12)
                    if r.status_code == 200:
                        content = r.content
                if not content:
                    import urllib.request
                    req = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        content = resp.read()
                if content and len(content) > 500:
                    with open(cover_file, "wb") as f:
                        f.write(content)
            except Exception:
                pass
        await asyncio.to_thread(download_cover)

    async def start_download(
        self,
        task_id: str,
        url: str,
        title: str,
        mode: str = "video",  # "video", "audio", or "photo"
        quality: str = "best",  # "best", "2160", "1440", "1080", "720", "480", "360"
        video_format: str = "mp4",  # mp4, mkv, webm, gif
        audio_format: str = "mp3",  # mp3, m4a, wav, flac, opus
        audio_quality: str = "320",  # 320, 256, 192, 128
        trim_start: Optional[str] = None,  # "00:01:00"
        trim_end: Optional[str] = None,    # "00:02:30"
        download_subtitles: bool = False,
        subtitles_lang: Optional[str] = "en",
        embed_subtitles: bool = False,
        embed_thumbnail: bool = True,
        split_chapters: bool = False,
        playlist_title: Optional[str] = None,
        playlist_index: Optional[int] = None,
        playlist_thumbnail: Optional[str] = None,
        is_photo: bool = False,
        photo_url: Optional[str] = None,
        user_id: Optional[str] = "kexiky",
        role: str = "guest"
    ) -> TaskState:
        if is_photo or mode == "photo":
            format_tag = "JPG Фото"
        elif mode == "video":
            if video_format == "gif":
                format_tag = "GIF Анимация"
            else:
                q_label = f"{quality}p" if quality.isdigit() else ("Best" if quality == "best" else quality.upper())
                format_tag = f"{q_label} {video_format.upper()}"
        else:
            format_tag = f"{audio_quality}kbps {audio_format.upper()}"

        state = TaskState(task_id, url, title, format_tag=format_tag, playlist_title=playlist_title, user_id=user_id, role=role)
        self.tasks[task_id] = state

        # Run background async download task
        state.async_task = asyncio.create_task(
            self._execute_download(
                state,
                mode=mode,
                quality=quality,
                video_format=video_format,
                audio_format=audio_format,
                audio_quality=audio_quality,
                trim_start=trim_start,
                trim_end=trim_end,
                download_subtitles=download_subtitles,
                subtitles_lang=subtitles_lang,
                embed_subtitles=embed_subtitles,
                embed_thumbnail=embed_thumbnail,
                split_chapters=split_chapters,
                playlist_title=playlist_title,
                playlist_index=playlist_index,
                playlist_thumbnail=playlist_thumbnail,
                is_photo=is_photo,
                photo_url=photo_url
            )
        )
        return state

    async def _execute_download(
        self,
        state: TaskState,
        mode: str,
        quality: str,
        video_format: str,
        audio_format: str,
        audio_quality: str,
        trim_start: Optional[str],
        trim_end: Optional[str],
        download_subtitles: bool,
        subtitles_lang: Optional[str],
        embed_subtitles: bool,
        embed_thumbnail: bool,
        split_chapters: bool = False,
        playlist_title: Optional[str] = None,
        playlist_index: Optional[int] = None,
        playlist_thumbnail: Optional[str] = None,
        is_photo: bool = False,
        photo_url: Optional[str] = None
    ):
        from app.database import ROLE_CONFIGS
        role = getattr(state, "role", "guest")
        role_cfg = ROLE_CONFIGS.get(role, ROLE_CONFIGS["guest"])
        limit_bytes = role_cfg.get("storage_limit_bytes")
        if limit_bytes:
            current_usage = get_user_disk_usage(state.user_id, role)["total_bytes"]
            if current_usage >= limit_bytes:
                state.status = "error"
                state.error = f"Превышена квота хранилища для тарифа {role_cfg['title']} ({role_cfg['storage_limit_display']}). Освободите место в файлах."
                state.logs.append(f"[Ошибка] Превышена квота хранилища: {role_cfg['storage_limit_display']}")
                return

        user_root = get_user_root(state.user_id, role)
        target_dir = user_root
        prefix = ""
        if playlist_title:
            clean_folder = re.sub(r'[\\/*?:"<>|]', "", playlist_title).strip()[:80] or "Плейлист"
            target_dir = user_root / clean_folder
            target_dir.mkdir(parents=True, exist_ok=True)
            cover_file = target_dir / ".cover.jpg"
            meta_file = target_dir / ".playlist_meta.json"
            if not meta_file.exists():
                try:
                    with open(meta_file, "w", encoding="utf-8") as f:
                        json.dump({
                            "title": playlist_title,
                            "created_at": time.time(),
                            "cover_file": ".cover.jpg"
                        }, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            if playlist_thumbnail and not cover_file.exists():
                asyncio.create_task(self._save_playlist_cover(target_dir, playlist_thumbnail))

            if playlist_index is not None:
                prefix = f"{playlist_index:02d}. "

        # Photo download branch
        if is_photo or mode == "photo":
            state.status = "downloading"
            state.logs.append("Скачивание фотографии в оригинальном разрешении...")
            img_target_url = photo_url
            if not img_target_url or "instagram.com/p/" in img_target_url or "instagram.com/reel/" in img_target_url:
                try:
                    ig_info = extract_instagram_post(state.url)
                    if ig_info:
                        cands = ig_info.get("image_versions2", {}).get("candidates", [])
                        if cands:
                            img_target_url = cands[0]["url"]
                        elif ig_info.get("display_uri"):
                            img_target_url = ig_info["display_uri"]
                except Exception:
                    pass

            if not img_target_url:
                img_target_url = state.url

            clean_name = re.sub(r'[\\/*?:"<>|]', "", state.title).strip()[:120] or "photo"
            out_file = target_dir / f"{prefix}{clean_name} [JPG].jpg"

            def fetch_img():
                content = None
                s = cffi_requests.Session(impersonate="chrome124") if cffi_requests else None
                if s:
                    r = s.get(img_target_url, timeout=20)
                    if r.status_code == 200:
                        content = r.content
                if not content:
                    import urllib.request
                    req = urllib.request.Request(img_target_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        content = resp.read()
                if not content or content.startswith(b"<!DOCTYPE") or content.startswith(b"<html"):
                    raise ValueError("Не удалось получить валидное изображение (получен HTML вместо картинки)")
                with open(out_file, "wb") as f:
                    f.write(content)

            try:
                await asyncio.to_thread(fetch_img)
                state.status = "completed"
                state.progress = 100.0
                state.filepath = str(out_file)
                try:
                    state.filename = str(out_file.relative_to(user_root))
                except Exception:
                    state.filename = out_file.name
                state.downloaded_str = format_size(out_file.stat().st_size)
                get_file_metadata(out_file, base_dir=user_root)
                get_or_create_thumbnail(state.filename, user_id=state.user_id)
                state.logs.append(f"Фото успешно сохранено: {state.filename}")
                return
            except Exception as e_img:
                state.status = "error"
                state.error = f"Ошибка скачивания фото: {str(e_img)}"
                state.logs.append(state.error)
                return

        state.status = "queued"
        state.logs.append("В очереди загрузок...")

        try:
            async with self.semaphore:
                if state.status == "cancelled":
                    return

                state.status = "downloading"
                state.logs.append(f"Инициализация скачивания: {state.url}")

                # Format filename template with quality / bitrate
                if mode == "audio":
                    out_template = str(target_dir / f"{prefix}%(title).150B [{audio_quality}kbps].%(ext)s")
                else:
                    if video_format == "gif":
                        out_template = str(target_dir / f"{prefix}%(title).150B [GIF_TEMP].%(ext)s")
                    elif quality != "best":
                        out_template = str(target_dir / f"{prefix}%(title).150B [{quality}p].%(ext)s")
                    else:
                        out_template = str(target_dir / f"{prefix}%(title).150B [%(height)sp].%(ext)s")

                cmd = get_ytdlp_cmd() + [
                    "--no-update",
                    "--no-warnings",
                    "--newline",
                    "--progress",
                    "--geo-bypass",
                    "-o", out_template,
                    "--progress-template", "%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s|%(progress._downloaded_bytes_str)s|%(progress._total_bytes_estimate_str)s",
                    "--write-thumbnail",
                    "--no-playlist",
                    "--retries", "10",
                    "--fragment-retries", "10",
                    "--file-access-retries", "3"
                ]

                # Mode setup
                if mode == "audio":
                    cmd.extend([
                        "-x",
                        "--audio-format", audio_format,
                        "--audio-quality", "0" if audio_quality == "320" else f"{audio_quality}K",
                    ])
                    if embed_thumbnail:
                        cmd.extend(["--embed-thumbnail", "--add-metadata"])
                else:
                    # Video mode
                    if quality == "best":
                        if video_format == "mp4":
                            format_spec = (
                                "bestvideo[vcodec^=avc]+bestaudio[acodec^=mp4a]/"
                                "bestvideo[vcodec^=avc]+bestaudio/"
                                "bestvideo+bestaudio[acodec^=mp4a]/"
                                "bestvideo+bestaudio/best"
                            )
                        else:
                            format_spec = "bestvideo+bestaudio/best"
                    else:
                        try:
                            h = int(str(quality).lower().replace("p", "").strip())
                            w = int(h * 16 / 9) + 60
                            if video_format == "mp4":
                                format_spec = (
                                    f"bestvideo[height<={h}][vcodec^=avc]+bestaudio[acodec^=mp4a]/"
                                    f"bestvideo[height<={h}][vcodec^=avc]+bestaudio/"
                                    f"bestvideo[height<={h}]+bestaudio[acodec^=mp4a]/"
                                    f"bestvideo[height<={h}]+bestaudio/"
                                    f"bestvideo[width<={w}][vcodec^=avc]+bestaudio/"
                                    f"bestvideo[width<={w}]+bestaudio/"
                                    f"best[height<={h}]/best"
                                )
                            else:
                                format_spec = (
                                    f"bestvideo[height<={h}]+bestaudio/"
                                    f"bestvideo[width<={w}]+bestaudio/"
                                    f"best[height<={h}]/best"
                                )
                        except ValueError:
                            format_spec = "bestvideo+bestaudio/best"

                    cmd.extend(["-f", format_spec])

                    # Container format
                    if video_format in ["mp4", "mkv", "webm"]:
                        cmd.extend(["--merge-output-format", video_format])
                    elif video_format == "gif":
                        cmd.extend(["--merge-output-format", "mp4"])

                    if embed_thumbnail and video_format != "gif":
                        cmd.extend(["--embed-thumbnail", "--add-metadata"])

                # Split chapters
                if split_chapters:
                    cmd.append("--split-chapters")
                    state.logs.append("Включено разделение по главам (--split-chapters)")

                # Trim / section selection
                if trim_start or trim_end:
                    start_str = trim_start.strip() if trim_start else "00:00:00"
                    end_str = trim_end.strip() if trim_end else "inf"
                    cmd.extend([
                        "--download-sections", f"*{start_str}-{end_str}",
                        "--force-keyframes-at-cuts"
                    ])
                    state.logs.append(f"Обрезка фрагмента: с {start_str} по {end_str}")

                # Subtitles
                if download_subtitles:
                    lang = subtitles_lang or "en"
                    cmd.extend([
                        "--write-subs",
                        "--write-auto-subs",
                        "--sub-langs", lang,
                        "--convert-subs", "vtt"
                    ])
                    if embed_subtitles and mode == "video" and video_format != "gif":
                        cmd.append("--embed-subs")
                    state.logs.append(f"Субтитры запрошены для языка: {lang} (формат WebVTT)")

                # Target URL
                cmd.append(state.url)

                state.logs.append("Команда yt-dlp запущена...")
                sub_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=sub_env
                )
                state.process = proc

                final_filepath = None
                last_error = None
                progress_regex = re.compile(r"(\d+(?:\.\d+)?)%\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|\n]+)")
                merger_regex = re.compile(r'Merging formats into "([^"]+)"')

                # Stream stdout line by line
                while True:
                    line_bytes = await proc.stdout.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode(errors="replace").strip()
                    if not line:
                        continue

                    # Check for progress template: 14.2%| 737.57KiB/s|00:00| 31.00KiB| N/A
                    match = progress_regex.search(line)
                    if match:
                        pct_str, speed_str, eta_str, down_str, tot_str = match.groups()
                        try:
                            state.progress = float(pct_str)
                        except ValueError:
                            pass
                        state.speed = speed_str.strip() or state.speed
                        state.eta = eta_str.strip() or state.eta

                        clean_down = down_str.strip() if down_str and "NA" not in down_str and "N/A" not in down_str else ""
                        clean_tot = tot_str.strip() if tot_str and "NA" not in tot_str and "N/A" not in tot_str else ""

                        if clean_down:
                            state.downloaded_str = clean_down.replace("iB", "B")

                        if clean_tot:
                            state.total_str = clean_tot.replace("iB", "B")
                        else:
                            down_bytes = parse_bytes_str(state.downloaded_str)
                            if down_bytes and state.progress > 2.0:
                                est_total = down_bytes / (state.progress / 100.0)
                                state.total_str = f"~{format_size(int(est_total))}"
                            else:
                                state.total_str = ""
                        continue

                    # Check for merge / audio conversion / destinations
                    merger_match = merger_regex.search(line)
                    if merger_match:
                        final_filepath = merger_match.group(1)
                        state.status = "converting"
                        state.logs.append(line)
                    elif "Destination:" in line:
                        dest = line.split("Destination:", 1)[1].strip()
                        if not dest.endswith((".part", ".ytdl", ".temp", ".webp")):
                            final_filepath = dest
                        state.logs.append(line)
                    elif "has already been downloaded" in line:
                        dest = line.replace("[download]", "").replace("has already been downloaded", "").strip()
                        if not dest.endswith((".part", ".ytdl", ".temp", ".webp")):
                            final_filepath = dest
                        state.logs.append(line)
                    elif "[ExtractAudio]" in line or "[EmbedThumbnail]" in line or "[Metadata]" in line:
                        state.status = "converting"
                        state.logs.append(line)
                    elif "ERROR:" in line or "Error" in line:
                        last_error = line
                        state.logs.append(line)
                    else:
                        state.logs.append(line)

                await proc.wait()

                if proc.returncode == 0:
                    # Determine initial downloaded file
                    if final_filepath and os.path.exists(final_filepath):
                        state.filepath = final_filepath
                        try:
                            state.filename = str(Path(final_filepath).relative_to(user_root))
                        except Exception:
                            state.filename = os.path.basename(final_filepath)
                    else:
                        candidates = [
                            p for p in target_dir.glob("*")
                            if p.is_file() and not p.name.endswith((".part", ".ytdl", ".webp", ".temp", ".json"))
                        ]
                        recent = [c for c in candidates if c.stat().st_mtime >= state.created_at - 10]
                        if recent:
                            newest = max(recent, key=lambda f: f.stat().st_mtime)
                        elif candidates:
                            newest = max(candidates, key=lambda f: f.stat().st_mtime)
                        else:
                            newest = None

                        if newest:
                            state.filepath = str(newest)
                            try:
                                state.filename = str(newest.relative_to(user_root))
                            except Exception:
                                state.filename = newest.name

                    # Check if GIF conversion is required
                    if video_format == "gif" and state.filepath and os.path.exists(state.filepath):
                        state.status = "converting"
                        state.logs.append("Создание плавной GIF-анимации через 2-pass FFmpeg palette...")
                        src_p = Path(state.filepath)
                        gif_name = src_p.stem.replace(" [GIF_TEMP]", "") + " [GIF].gif"
                        gif_path = target_dir / gif_name
                        gif_cmd = [
                            "ffmpeg", "-y", "-i", str(src_p),
                            "-vf", "fps=15,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                            str(gif_path)
                        ]
                        try:
                            p_gif = await asyncio.create_subprocess_exec(*gif_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                            await p_gif.communicate()
                            if gif_path.exists() and gif_path.stat().st_size > 0:
                                src_p.unlink(missing_ok=True)
                                state.filepath = str(gif_path)
                                try:
                                    state.filename = str(gif_path.relative_to(user_root))
                                except Exception:
                                    state.filename = gif_path.name
                                state.logs.append(f"GIF успешно сгенерирован: {state.filename}")
                        except Exception as e_gif:
                            state.logs.append(f"Ошибка при создании GIF: {str(e_gif)}")

                    state.status = "completed"
                    state.progress = 100.0

                    if state.filepath and os.path.exists(state.filepath):
                        try:
                            state.downloaded_str = format_size(os.path.getsize(state.filepath))
                            # Pre-cache metadata and thumbnail immediately
                            p_file = Path(state.filepath)
                            get_file_metadata(p_file, base_dir=user_root)
                            get_or_create_thumbnail(state.filename, user_id=state.user_id)
                        except Exception:
                            pass

                    state.logs.append(f"Загрузка успешно завершена: {state.filename}")
                else:
                    if state.status != "cancelled":
                        state.status = "error"
                        state.error = last_error or f"Процесс завершился с кодом {proc.returncode}"
                        state.logs.append(f"Ошибка скачивания: {state.error}")

        except asyncio.CancelledError:
            state.status = "cancelled"
            state.logs.append("Загрузка отменена пользователем.")
            if state.process and state.process.returncode is None:
                try:
                    state.process.terminate()
                except Exception:
                    pass
        except Exception as ex:
            state.status = "error"
            state.error = str(ex)
            state.logs.append(f"Исключение: {str(ex)}")

    def cancel_task(self, task_id: str) -> bool:
        if task_id in self.tasks:
            task = self.tasks[task_id]
            task.status = "cancelled"
            if task.async_task and not task.async_task.done():
                try:
                    task.async_task.cancel()
                except Exception:
                    pass
            if task.process and task.process.returncode is None:
                try:
                    task.process.terminate()
                except Exception:
                    pass
            return True
        return False

    def list_history(self, user_id: Optional[str] = "kexiky", role: str = "guest") -> Dict[str, Any]:
        """List files and structured playlists for a specific user according to their role."""
        user_root = get_user_root(user_id, role)
        if not user_root.exists():
            return {"files": [], "playlists": []}

        video_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".gif"}
        audio_exts = {".mp3", ".m4a", ".wav", ".flac", ".opus", ".aac", ".ogg"}
        image_exts = {".jpg", ".jpeg", ".png", ".webp"}
        valid_exts = video_exts | audio_exts | image_exts

        is_admin_or_kexiky = (not user_id or user_id == "kexiky" or role == "admin")
        now = time.time()

        from app.database import ROLE_CONFIGS
        role_cfg = ROLE_CONFIGS.get(role, ROLE_CONFIGS["guest"])
        role_ttl = role_cfg.get("ttl_seconds")
        if is_admin_or_kexiky:
            role_ttl = None
        is_permanent = (role_ttl is None)

        all_files = []
        playlists_dict: Dict[str, Dict[str, Any]] = {}

        def is_companion_thumbnail(img_p: Path, d: Path) -> bool:
            if img_p.suffix.lower() not in image_exts:
                return False
            stem = img_p.stem
            clean_stem = re.sub(r"\s*\[.*?\]", "", stem).strip().lower()
            for other in d.iterdir():
                if other.is_file() and other != img_p:
                    other_ext = other.suffix.lower()
                    if other_ext in video_exts or other_ext in audio_exts or other_ext in [".part", ".ytdl"]:
                        other_stem = other.stem
                        other_clean = re.sub(r"\s*\[.*?\]", "", other_stem).strip().lower()
                        if stem == other_stem or (clean_stem and clean_stem == other_clean):
                            return True
            return False

        # 1. Scan subdirectories (playlist folders)
        for folder in sorted(user_root.iterdir()):
            if is_admin_or_kexiky and folder.name in ["guests", "users"]:
                continue
            if folder.is_dir() and not folder.name.startswith("."):
                folder_files = []
                meta_file = folder / ".playlist_meta.json"
                playlist_title = folder.name
                if meta_file.exists():
                    try:
                        with open(meta_file, "r", encoding="utf-8") as f:
                            pdata = json.load(f)
                            if pdata.get("title"):
                                playlist_title = pdata["title"]
                    except Exception:
                        pass

                def _nat_key(item):
                    idx = item.get("playlist_index")
                    if idx is not None:
                        return (0, idx, item.get("basename", ""))
                    bname = item.get("basename", "")
                    m_num = re.match(r"^(\d+)", bname)
                    if m_num:
                        return (0, int(m_num.group(1)), bname)
                    return (1, 0, bname.lower())

                for p in sorted(folder.glob("*"), key=lambda f: f.name):
                    if p.is_file() and not p.name.startswith("."):
                        if p.suffix.lower() in valid_exts and not is_companion_thumbnail(p, folder):
                            meta = get_file_metadata(p, base_dir=user_root)
                            meta["playlist_name"] = playlist_title
                            meta["owner"] = "kexiky" if is_admin_or_kexiky else user_id
                            meta["is_permanent"] = is_permanent
                            if is_permanent:
                                meta["expires_at"] = None
                                meta["ttl_remaining_seconds"] = None
                                meta["ttl_display"] = "♾️ Вечно"
                            else:
                                created = meta.get("created_at", now)
                                expires_at = created + role_ttl
                                meta["expires_at"] = expires_at
                                meta["ttl_remaining_seconds"] = max(0, int(expires_at - now))
                                meta["ttl_display"] = role_cfg.get("ttl_display", "⏳")
                            folder_files.append(meta)
                            all_files.append(meta)

                folder_files.sort(key=_nat_key)

                if folder_files:
                    total_bytes = sum(f.get("size_bytes", 0) for f in folder_files)
                    total_duration = sum(f.get("duration_seconds", 0) for f in folder_files)

                    # Ensure .cover.jpg exists for this playlist folder
                    cover_file = folder / ".cover.jpg"
                    if not cover_file.exists() or cover_file.stat().st_size < 500:
                        # Try to copy companion webp or frame from first media file
                        for cand in folder.iterdir():
                            if cand.is_file() and not cand.name.startswith(".") and cand.suffix.lower() in [".webp", ".jpg", ".png"]:
                                try:
                                    subprocess.run(["ffmpeg", "-y", "-i", str(cand), "-vf", "scale=640:-1", str(cover_file)], capture_output=True, timeout=5)
                                    if cover_file.exists() and cover_file.stat().st_size > 500:
                                        break
                                except Exception:
                                    pass
                        if not cover_file.exists():
                            first_vid = next((f for f in folder_files if f.get("is_video")), None)
                            if first_vid:
                                t = get_or_create_thumbnail(first_vid["filename"], user_id=user_id)
                                if t and t.exists():
                                    try:
                                        shutil.copyfile(t, cover_file)
                                    except Exception:
                                        pass

                    thumb_url = f"/api/thumbnail/{folder.name}/.cover.jpg" if (cover_file.exists() and cover_file.stat().st_size > 0) else (folder_files[0].get("thumbnail_url", "") if folder_files else "")
                    first_file_name = folder_files[0]["filename"] if folder_files else ""

                    playlists_dict[folder.name] = {
                        "id": folder.name,
                        "title": playlist_title,
                        "folder_name": folder.name,
                        "item_count": len(folder_files),
                        "video_count": len(folder_files),
                        "total_size_bytes": total_bytes,
                        "total_size_str": format_size(total_bytes),
                        "total_duration_seconds": total_duration,
                        "total_duration_str": format_duration(total_duration),
                        "thumbnail_url": thumb_url,
                        "first_thumbnail_file": first_file_name,
                        "first_video_file": first_file_name,
                        "created_at": max((f.get("created_at", 0) for f in folder_files), default=0),
                        "is_permanent": is_permanent,
                        "ttl_display": "♾️ Вечно" if is_permanent else role_cfg.get("ttl_display", "⏳"),
                        "owner": "kexiky" if is_admin_or_kexiky else user_id,
                        "files": folder_files
                    }

        # 2. Scan root files (standalone downloads)
        for p in user_root.glob("*"):
            if is_admin_or_kexiky and p.name in ["guests", "users"]:
                continue
            if p.is_file() and not p.name.startswith("."):
                if p.suffix.lower() in valid_exts and not is_companion_thumbnail(p, user_root):
                    meta = get_file_metadata(p, base_dir=user_root)
                    meta["owner"] = "kexiky" if is_admin_or_kexiky else user_id
                    meta["is_permanent"] = is_permanent
                    if is_permanent:
                        meta["expires_at"] = None
                        meta["ttl_remaining_seconds"] = None
                        meta["ttl_display"] = "♾️ Вечно"
                    else:
                        created = meta.get("created_at", now)
                        expires_at = created + role_ttl
                        meta["expires_at"] = expires_at
                        meta["ttl_remaining_seconds"] = max(0, int(expires_at - now))
                        meta["ttl_display"] = role_cfg.get("ttl_display", "⏳")
                    all_files.append(meta)

        # Sort all files by mtime descending
        all_files.sort(key=lambda f: f.get("created_at", 0), reverse=True)
        playlists_list = sorted(playlists_dict.values(), key=lambda pl: pl.get("created_at", 0), reverse=True)

        return {
            "files": all_files,
            "playlists": playlists_list
        }

    def get_or_create_thumbnail(self, filename: str, user_id: Optional[str] = None) -> Optional[Path]:
        return get_or_create_thumbnail(filename, user_id=user_id)

    def delete_file(self, filename: str, user_id: Optional[str] = None) -> bool:
        target = resolve_download_path(filename, user_id)
        user_root = get_user_root(user_id)
        if not target or not target.exists() or not target.is_file():
            # Try finding by clean basename in case relative folder wasn't provided or mismatch occurred
            import urllib.parse
            base_cand = urllib.parse.unquote(Path(filename).name)
            for p in user_root.rglob("*"):
                if p.is_file() and p.name == base_cand:
                    target = p
                    break

        if not target or not target.exists() or not target.is_file():
            return False

        parent_dir = target.parent
        target.unlink(missing_ok=True)

        # Remove cached thumbnail
        safe_key = hashlib.md5(filename.strip("/\\").encode("utf-8")).hexdigest()
        (THUMBNAILS_DIR / f"{safe_key}.jpg").unlink(missing_ok=True)
        (THUMBNAILS_DIR / f"{target.name}.jpg").unlink(missing_ok=True)
        try:
            rel_name = str(target.relative_to(user_root))
            safe_rel_key = hashlib.md5(rel_name.strip("/\\").encode("utf-8")).hexdigest()
            (THUMBNAILS_DIR / f"{safe_rel_key}.jpg").unlink(missing_ok=True)
        except Exception:
            rel_name = filename

        # Clean companion webp, vtt, srt, jpg, part in parent_dir
        stem = target.stem
        if parent_dir.exists():
            for cand in parent_dir.iterdir():
                if cand.is_file() and cand != target:
                    if cand.name.startswith(stem) and cand.suffix.lower() in [
                        ".webp", ".jpg", ".jpeg", ".png", ".part", ".ytdl",
                        ".vtt", ".srt", ".ass"
                    ]:
                        cand.unlink(missing_ok=True)

        # Clear from meta cache
        global _meta_cache
        for k in [filename, target.name, rel_name]:
            if k in _meta_cache:
                del _meta_cache[k]
        _save_meta_cache()

        # If inside a playlist folder and no more media files remain, remove folder
        if parent_dir != user_root and parent_dir.exists():
            remaining_media = [
                f for f in parent_dir.glob("*")
                if f.is_file() and not f.name.startswith(".") and f.suffix.lower() not in [".json", ".webp", ".jpg", ".part", ".vtt", ".srt"]
            ]
            if not remaining_media:
                shutil.rmtree(parent_dir, ignore_errors=True)

        return True

    def delete_files_batch(self, filenames: List[str], user_id: Optional[str] = None) -> Dict[str, Any]:
        deleted_count = 0
        for fn in filenames:
            if self.delete_file(fn, user_id=user_id):
                deleted_count += 1
        return {"deleted_count": deleted_count}

    def delete_playlist(self, folder_name: str, user_id: Optional[str] = None) -> bool:
        target = resolve_download_path(folder_name, user_id)
        user_root = get_user_root(user_id)
        if not target or not target.exists() or not target.is_dir() or target == user_root:
            return False

        global _meta_cache
        for f in target.glob("*"):
            if f.is_file():
                try:
                    rel = str(f.relative_to(user_root))
                    key = hashlib.md5(rel.encode("utf-8")).hexdigest()
                    (THUMBNAILS_DIR / f"{key}.jpg").unlink(missing_ok=True)
                    (THUMBNAILS_DIR / f"{f.name}.jpg").unlink(missing_ok=True)
                    if rel in _meta_cache:
                        del _meta_cache[rel]
                    if f.name in _meta_cache:
                        del _meta_cache[f.name]
                except Exception:
                    pass

        _save_meta_cache()
        shutil.rmtree(target, ignore_errors=True)
        return True

    def get_file_subtitles(self, filename: str, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        target = resolve_download_path(filename, user_id)
        if not target and user_id:
            target = resolve_download_path(filename, "kexiky")
        if not target or not target.exists() or not target.is_file():
            return []

        subs = []
        # 1. Embedded subtitles via ffprobe
        try:
            proc = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "s",
                    "-show_entries", "stream=index,codec_name:stream_tags=language,title",
                    "-of", "json",
                    str(target)
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8
            )
            if proc.returncode == 0 and proc.stdout:
                data = json.loads(proc.stdout)
                for st in data.get("streams", []):
                    idx = st.get("index")
                    tags = st.get("tags") or {}
                    lang = tags.get("language") or "und"
                    title = tags.get("title") or lang.upper()
                    subs.append({
                        "id": str(idx),
                        "lang": lang,
                        "label": f"{title} ({lang})",
                        "type": "embedded",
                        "url": f"/api/subtitles-track/{filename}/{idx}.vtt"
                    })
        except Exception:
            pass

        # 2. Companion external subtitle files
        stem = target.stem
        parent_dir = target.parent
        try:
            for cand in parent_dir.iterdir():
                if cand.is_file() and cand.name.startswith(stem) and cand.suffix.lower() in [".vtt", ".srt"]:
                    sub_lang = "sub"
                    parts = cand.stem.split(".")
                    if len(parts) > 1:
                        sub_lang = parts[-1]
                    subs.append({
                        "id": cand.name,
                        "lang": sub_lang,
                        "label": f"{sub_lang.upper()} (файл)",
                        "type": "companion",
                        "url": f"/api/subtitles-track/{filename}/{cand.name}.vtt"
                    })
        except Exception:
            pass

        return subs

    def extract_subtitle_vtt(self, filename: str, track_id: str, user_id: Optional[str] = None) -> Optional[str]:
        target = resolve_download_path(filename, user_id)
        if not target and user_id:
            target = resolve_download_path(filename, "kexiky")
        if not target or not target.exists() or not target.is_file():
            return None

        # Check companion file in same folder or downloads
        companion_target = target.parent / track_id
        if not companion_target.exists():
            companion_target = resolve_download_path(track_id, user_id)

        if companion_target and companion_target.exists() and companion_target.is_file():
            if companion_target.suffix.lower() == ".vtt":
                try:
                    return companion_target.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            elif companion_target.suffix.lower() == ".srt":
                try:
                    proc = subprocess.run(
                        ["ffmpeg", "-y", "-i", str(companion_target), "-f", "webvtt", "-"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=8
                    )
                    if proc.returncode == 0:
                        return proc.stdout
                except Exception:
                    pass

        # Extract embedded track via stream index
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-i", str(target), "-map", f"0:{track_id}", "-f", "webvtt", "-"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8
            )
            if proc.returncode == 0:
                return proc.stdout
        except Exception:
            pass
        return None

    def reveal_in_finder(self, filename: str, user_id: Optional[str] = None) -> bool:
        target = resolve_download_path(filename, user_id)
        if not target or not target.exists():
            return False
        try:
            import platform
            sys_name = platform.system().lower()
            if sys_name == "windows":
                subprocess.Popen(["explorer", f"/select,{str(target.resolve())}"])
                return True
            elif sys_name == "darwin":
                subprocess.run(["open", "-R", str(target)], check=True)
                return True
            else:
                subprocess.run(["xdg-open", str(target.parent)], check=True)
                return True
        except Exception as e:
            print(f"[Reveal] Error opening file: {e}")
            return False

    def open_downloads_folder(self, subfolder: Optional[str] = None, user_id: Optional[str] = None) -> bool:
        target = resolve_download_path(subfolder, user_id) if subfolder else get_user_root(user_id)
        if not target or not target.exists():
            target = get_user_root(user_id)
        try:
            import platform
            sys_name = platform.system().lower()
            if sys_name == "windows":
                os.startfile(str(target.resolve()))
                return True
            elif sys_name == "darwin":
                subprocess.run(["open", str(target)], check=True)
                return True
            else:
                subprocess.run(["xdg-open", str(target)], check=True)
                return True
        except Exception as e:
            print(f"[OpenFolder] Error opening folder: {e}")
            return False


# Global singleton instance
downloader_manager = DownloaderManager()
