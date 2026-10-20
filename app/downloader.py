import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Any

from app.roles import ROLE_CONFIGS, CREATOR_USERNAME

if not shutil.which("ffmpeg"):
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

def get_ytdlp_global_args() -> List[str]:
    """Server-wide yt-dlp options from the environment: cookies (age-restricted / private media) and proxy.

    Only the operator can set these (YTDLP_COOKIES_FILE, YTDLP_PROXY); nothing here is user-controlled.
    """
    args: List[str] = []
    cookies = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if cookies and os.path.isfile(cookies):
        args += ["--cookies", cookies]
    proxy = os.getenv("YTDLP_PROXY", "").strip()
    if proxy:
        args += ["--proxy", proxy]
    return args


BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = Path(os.getenv("MEDIAPULL_DOWNLOADS_DIR") or (BASE_DIR / "downloads"))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
THUMBNAILS_DIR = DOWNLOADS_DIR / ".thumbnails"
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR = DOWNLOADS_DIR / ".temp"
META_CACHE_FILE = THUMBNAILS_DIR / "meta_cache.json"

# Top-level folders of DOWNLOADS_DIR that belong to other owners (never part of the creator's own library).
RESERVED_ROOT_DIRS = {"guests", "users", "temp_tg"}
TEMP_USER_PREFIX = "tg_temp"

MIN_FREE_DISK_BYTES = int(float(os.getenv("MEDIAPULL_MIN_FREE_DISK_GB", "1")) * 1024 ** 3)
MAX_IMAGE_BYTES = 25 * 1024 * 1024
ALLOW_PRIVATE_URLS = os.getenv("MEDIAPULL_ALLOW_PRIVATE_URLS", "").strip().lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Metadata cache (thread-safe, atomically written, debounced)
# ---------------------------------------------------------------------------
_meta_cache: Dict[str, Any] = {}
_meta_lock = threading.RLock()
_meta_dirty = False
_meta_last_save = 0.0
META_SAVE_INTERVAL = 5.0


def _load_meta_cache():
    global _meta_cache
    if META_CACHE_FILE.exists():
        try:
            with open(META_CACHE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            _meta_cache = loaded if isinstance(loaded, dict) else {}
        except Exception:
            _meta_cache = {}


def _write_meta_cache_locked():
    global _meta_dirty, _meta_last_save
    tmp = META_CACHE_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_meta_cache, f, ensure_ascii=False)
        os.replace(tmp, META_CACHE_FILE)
        _meta_dirty = False
    except Exception as e:
        print(f"[MetaCache] Could not save cache: {e}")
    finally:
        _meta_last_save = time.monotonic()


def _save_meta_cache(force: bool = False):
    """Mark the cache dirty and write it at most once per META_SAVE_INTERVAL."""
    global _meta_dirty
    with _meta_lock:
        _meta_dirty = True
        if force or time.monotonic() - _meta_last_save >= META_SAVE_INTERVAL:
            _write_meta_cache_locked()


def flush_meta_cache():
    with _meta_lock:
        if _meta_dirty:
            _write_meta_cache_locked()


def prune_meta_cache() -> int:
    """Drop cache entries whose media file no longer exists. Returns number of removed entries."""
    with _meta_lock:
        keys = list(_meta_cache.keys())
    stale = [k for k in keys if not (DOWNLOADS_DIR / k).exists()]
    if stale:
        with _meta_lock:
            for k in stale:
                _meta_cache.pop(k, None)
        _save_meta_cache(force=True)
    return len(stale)


def _cache_key(filepath: Path) -> str:
    """Cache key unique across all owners: path relative to DOWNLOADS_DIR."""
    try:
        return filepath.relative_to(DOWNLOADS_DIR).as_posix()
    except ValueError:
        return filepath.as_posix()


_load_meta_cache()


def thumbnail_cache_name(user_prefix: Optional[str], rel_posix: str) -> str:
    """File name of the cached thumbnail for `rel_posix` (path relative to the owner's root)."""
    key = f"{user_prefix or CREATOR_USERNAME}:{rel_posix.strip('/')}"
    return hashlib.md5(key.encode("utf-8"), usedforsecurity=False).hexdigest() + ".jpg"

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


# ---------------------------------------------------------------------------
# URL validation and safe remote fetching (SSRF / local file protection)
# ---------------------------------------------------------------------------
MAX_URL_LENGTH = 2048
_FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"}


def validate_media_url(url: Optional[str]) -> str:
    """Syntax check: a single http(s) URL. Rejects option-like strings, other schemes and control characters."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise ValueError("Введите корректную ссылку")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("Ссылка содержит недопустимые символы")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
    except ValueError:
        raise ValueError("Введите корректную ссылку")
    if parsed.scheme.lower() not in ("http", "https") or not host:
        raise ValueError("Поддерживаются только ссылки http:// и https://")
    return url


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def assert_public_host(hostname: str) -> None:
    """Raise ValueError when `hostname` resolves to a loopback / private / link-local address."""
    if ALLOW_PRIVATE_URLS:
        return
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError("Не удалось определить адрес сервера")
    if not infos:
        raise ValueError("Не удалось определить адрес сервера")
    for info in infos:
        if not _is_public_ip(info[4][0]):
            raise ValueError("Ссылки на внутреннюю сеть и локальные адреса запрещены")


async def validate_public_url(url: Optional[str]) -> str:
    """validate_media_url + DNS check, without blocking the event loop."""
    clean = validate_media_url(url)
    await asyncio.to_thread(assert_public_host, urllib.parse.urlsplit(clean).hostname)
    return clean


def _fetch_once(url: str, max_bytes: int, timeout: float):
    """One HTTP GET without following redirects. Returns (body, redirect_location)."""
    last_error = "ошибка соединения"
    try:
        with requests.get(url, headers=_FETCH_HEADERS, timeout=timeout, stream=True, allow_redirects=False) as r:
            if 300 <= r.status_code < 400 and r.headers.get("Location"):
                return None, r.headers["Location"]
            if r.status_code == 200:
                declared = r.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ValueError("Файл слишком большой")
                chunks, total = [], 0
                for chunk in r.iter_content(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Файл слишком большой")
                    chunks.append(chunk)
                return b"".join(chunks), None
            last_error = f"HTTP {r.status_code}"
            if r.status_code not in (401, 403, 429):
                raise ValueError(last_error)
    except requests.RequestException as e:
        last_error = str(e)

    if cffi_requests is None:
        raise ValueError(f"Не удалось загрузить файл: {last_error}")

    # Anti-bot fallback: browser TLS impersonation.
    try:
        r = cffi_requests.get(url, impersonate="chrome124", timeout=timeout, allow_redirects=False)
    except Exception as e:
        raise ValueError(f"Не удалось загрузить файл: {e}")
    if 300 <= r.status_code < 400 and r.headers.get("Location"):
        return None, r.headers["Location"]
    if r.status_code != 200:
        raise ValueError(f"Не удалось загрузить файл: HTTP {r.status_code}")
    declared = r.headers.get("Content-Length")
    if (declared and declared.isdigit() and int(declared) > max_bytes) or len(r.content) > max_bytes:
        raise ValueError("Файл слишком большой")
    return r.content, None


def safe_fetch_bytes(url: str, max_bytes: int = MAX_IMAGE_BYTES, timeout: float = 20, max_redirects: int = 5) -> bytes:
    """Download a small remote file. Only http(s), every redirect hop is re-validated against private networks."""
    current = validate_media_url(url)
    for _ in range(max_redirects + 1):
        assert_public_host(urllib.parse.urlsplit(current).hostname)
        body, location = _fetch_once(current, max_bytes, timeout)
        if location is None:
            return body
        current = validate_media_url(urllib.parse.urljoin(current, location))
    raise ValueError("Слишком много перенаправлений")


def looks_like_image(data: bytes) -> bool:
    head = data[:16]
    return (
        head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"GIF8")
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        or head[4:8] == b"ftyp"  # AVIF / HEIC
    )


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

    s = None
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
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Per-owner storage layout
# ---------------------------------------------------------------------------
def is_temp_user(user_id: Optional[str]) -> bool:
    return bool(user_id) and str(user_id).startswith(TEMP_USER_PREFIX)


def _safe_uid(user_id: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]", "", user_id)
    if clean != user_id or not clean:
        # Legacy / unusual ids: keep the folder unique instead of colliding with other users.
        clean = (clean or "u") + "_" + hashlib.sha1(user_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return clean


def get_user_root(user_id: Optional[str] = None, role: Optional[str] = None) -> Path:
    """Return the root directory of a storage owner.

    Only the creator ('kexiky') maps to DOWNLOADS_DIR itself; every other account (admins included)
    gets its own folder. `role` is accepted for backward compatibility and no longer influences the path,
    so reading and writing always agree on the location.
    """
    if is_temp_user(user_id):
        sub = re.sub(r"[^a-zA-Z0-9_-]", "", str(user_id)[len(TEMP_USER_PREFIX):]).lstrip("_-")
        temp_root = DOWNLOADS_DIR / "temp_tg" / sub if sub else DOWNLOADS_DIR / "temp_tg"
        temp_root.mkdir(parents=True, exist_ok=True)
        return temp_root
    if not user_id or user_id == CREATOR_USERNAME:
        return DOWNLOADS_DIR
    clean_uid = _safe_uid(user_id)
    if clean_uid.startswith("guest_"):
        guest_root = DOWNLOADS_DIR / "guests" / clean_uid
        guest_root.mkdir(parents=True, exist_ok=True)
        return guest_root
    user_root = DOWNLOADS_DIR / "users" / clean_uid
    user_root.mkdir(parents=True, exist_ok=True)
    return user_root


def remove_user_files(user_id: str) -> None:
    """Delete everything a (deleted) user owns, so a re-registered name cannot inherit old files."""
    if not user_id or user_id == CREATOR_USERNAME or is_temp_user(user_id):
        return
    clean_uid = _safe_uid(user_id)
    base = DOWNLOADS_DIR / ("guests" if clean_uid.startswith("guest_") else "users")
    shutil.rmtree(base / clean_uid, ignore_errors=True)
    invalidate_usage(user_id)


_usage_cache: Dict[str, tuple] = {}
_usage_lock = threading.Lock()
USAGE_CACHE_TTL = 30.0


def invalidate_usage(user_id: Optional[str] = None) -> None:
    with _usage_lock:
        if user_id is None:
            _usage_cache.clear()
        else:
            _usage_cache.pop(user_id or CREATOR_USERNAME, None)


def _scan_usage(root: Path, skip_reserved: bool) -> tuple:
    total_bytes = 0
    file_count = 0
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str):
        if dirpath == root_str and skip_reserved:
            dirnames[:] = [d for d in dirnames if d not in RESERVED_ROOT_DIRS]
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            try:
                total_bytes += os.stat(os.path.join(dirpath, name)).st_size
                file_count += 1
            except OSError:
                pass
    return total_bytes, file_count


def get_user_disk_usage(user_id: Optional[str] = CREATOR_USERNAME, role: Optional[str] = None, use_cache: bool = True) -> Dict[str, Any]:
    """Calculate total bytes used and file count for a user (cached for a short time)."""
    if is_temp_user(user_id):
        return {"total_bytes": 0, "file_count": 0, "total_size_str": "0 B"}

    cache_key = user_id or CREATOR_USERNAME
    now = time.monotonic()
    if use_cache:
        with _usage_lock:
            hit = _usage_cache.get(cache_key)
            if hit and hit[0] > now:
                return dict(hit[1])

    user_root = get_user_root(user_id)
    if user_root.exists():
        total_bytes, file_count = _scan_usage(user_root, skip_reserved=(user_root == DOWNLOADS_DIR))
    else:
        total_bytes, file_count = 0, 0

    result = {
        "total_bytes": total_bytes,
        "file_count": file_count,
        "total_size_str": format_size(total_bytes) if total_bytes else "0 B",
    }
    with _usage_lock:
        _usage_cache[cache_key] = (now + USAGE_CACHE_TTL, result)
    return dict(result)


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

def _resolve_inside(user_root: Path, candidate: str) -> Optional[Path]:
    """Resolve `candidate` under `user_root`; None when it escapes the root or touches hidden/internal paths."""
    if "\x00" in candidate:
        return None
    safe_rel = candidate.strip("/\\")
    if not safe_rel:
        return None
    parts = [p for p in re.split(r"[\\/]+", safe_rel) if p]
    for part in parts:
        # Hidden entries (.thumbnails, .temp, .playlist_meta.json, ...) and '..' are internal; only covers are public.
        if part.startswith(".") and part != ".cover.jpg":
            return None
    if user_root == DOWNLOADS_DIR.resolve() and parts and parts[0] == "temp_tg":
        return None
    try:
        resolved = (user_root / safe_rel).resolve()
    except (OSError, ValueError):
        return None
    try:
        if not resolved.is_relative_to(user_root):
            return None
    except AttributeError:  # Python < 3.9
        if user_root not in resolved.parents and resolved != user_root:
            return None
    return resolved


def resolve_download_path(rel_path: Optional[str], user_id: Optional[str] = None, role: Optional[str] = None) -> Optional[Path]:
    """Safely resolve a relative path inside the owner's root directory, preventing directory traversal."""
    if not rel_path:
        return None
    user_root = get_user_root(user_id).resolve()

    # Candidate spellings: as sent and percent-decoded (clients may double-encode names containing '%').
    candidates = [rel_path]
    unq = urllib.parse.unquote(rel_path)
    if unq != rel_path:
        candidates.append(unq)

    first_valid = None
    for cand in candidates:
        resolved = _resolve_inside(user_root, cand)
        if resolved is None:
            continue
        if resolved.exists():
            return resolved
        if first_valid is None:
            first_valid = resolved

    # Safe location even if it does not exist yet (e.g. for new output files)
    return first_valid


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".gif"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".opus", ".aac", ".ogg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS


def build_resolution_options(formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Resolution tiers found in yt-dlp `formats`, each with an estimated download size (video + best audio)."""
    detected_tiers = set()
    video_only_size: Dict[int, int] = {}
    combined_size: Dict[int, int] = {}
    audio_best_size = 0
    for f in formats:
        h = f.get("height")
        w = f.get("width")
        vcodec = f.get("vcodec", "")
        acodec = f.get("acodec", "")
        ext = f.get("ext", "")
        size = int(f.get("filesize") or f.get("filesize_approx") or 0)
        if vcodec == "none" and acodec and acodec != "none":
            audio_best_size = max(audio_best_size, size)
        if h and isinstance(h, int) and h >= 144 and vcodec and vcodec != "none" and ext != "mhtml":
            tier = get_resolution_tier(w, h)
            if tier:
                detected_tiers.add(tier)
                if acodec and acodec != "none":
                    combined_size[tier] = max(combined_size.get(tier, 0), size)
                else:
                    video_only_size[tier] = max(video_only_size.get(tier, 0), size)

    options = []
    for h in sorted(detected_tiers, reverse=True):
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
        video_part = video_only_size.get(h, 0)
        estimated = max(combined_size.get(h, 0), (video_part + audio_best_size) if video_part else 0)
        options.append({"height": h, "label": label, "filesize": estimated or None})
    return options


def _thumb_url(rel_posix: str) -> str:
    return "/api/thumbnail/" + urllib.parse.quote(rel_posix, safe="/")


def _rel_posix(path: Path, base_dir: Path) -> str:
    try:
        return path.relative_to(base_dir).as_posix()
    except ValueError:
        return path.name


def get_file_metadata(filepath: Path, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    if base_dir is None:
        base_dir = DOWNLOADS_DIR
    rel_path = _rel_posix(filepath, base_dir)
    cache_key = _cache_key(filepath)

    try:
        stat = filepath.stat()
        mtime = stat.st_mtime
        size_bytes = stat.st_size
    except Exception:
        mtime = 0
        size_bytes = 0

    in_playlist = filepath.parent != base_dir

    with _meta_lock:
        cached_entry = _meta_cache.get(cache_key)
        if cached_entry and cached_entry.get("mtime") == mtime:
            cached = dict(cached_entry)
        else:
            cached = None

    if cached is not None:
        cached["filename"] = rel_path
        cached["size_bytes"] = size_bytes
        cached["size_str"] = format_size(size_bytes)
        cached["thumbnail_url"] = _thumb_url(rel_path)
        cached["is_in_playlist"] = in_playlist
        if cached.get("playlist_index") is None and in_playlist:
            m_idx = re.match(r"^(\d+)[\.\s_-]", filepath.name)
            if m_idx:
                cached["playlist_index"] = int(m_idx.group(1))
        return cached

    # Parse title and bracketed tag from filename
    m = re.search(r"^(.*?)(?:\s*\[([^\]]+)\])?\.[^.]+$", filepath.name)
    clean_title = m.group(1).strip() if m else filepath.stem
    bracket_tag = m.group(2).strip() if (m and m.group(2)) else ""

    ext = filepath.suffix.lower()
    is_video = ext in VIDEO_EXTS
    is_audio = ext in AUDIO_EXTS
    is_image = ext in IMAGE_EXTS

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
    playlist_index = None
    if in_playlist:
        playlist_folder = filepath.parent.name
        playlist_title = playlist_folder
        idx_match = re.match(r"^(\d+)[\.\s_-]", filepath.name)
        if idx_match:
            playlist_index = int(idx_match.group(1))
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
        "thumbnail_url": _thumb_url(rel_path),
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
        "is_in_playlist": in_playlist,
        "playlist_index": playlist_index
    }
    with _meta_lock:
        _meta_cache[cache_key] = info
    _save_meta_cache()
    return dict(info)


_thumb_slots = threading.BoundedSemaphore(max(1, int(os.getenv("MEDIAPULL_THUMB_WORKERS", "2"))))


def _ffmpeg_to_file(input_args: List[str], out_path: Path, min_size: int = 1, timeout: float = 15) -> bool:
    """Run ffmpeg into a temp file and atomically move it into place (no half-written thumbnails)."""
    tmp = out_path.with_name(f"{out_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp{out_path.suffix}")
    try:
        with _thumb_slots:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-y", *input_args, str(tmp)],
                capture_output=True,
                timeout=timeout,
            )
        if tmp.exists() and tmp.stat().st_size >= min_size:
            os.replace(tmp, out_path)
            return True
    except Exception:
        pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return False


def _probe_duration(filepath: Path) -> float:
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
            return float(proc.stdout.strip())
    except Exception:
        pass
    return 0.0


def get_or_create_thumbnail(filename: str, user_id: Optional[str] = None) -> Optional[Path]:
    filepath = resolve_download_path(filename, user_id)
    if not filepath or not filepath.exists():
        return None
    user_root = get_user_root(user_id).resolve()

    # 1. Directory requested (e.g. playlist folder thumbnail)
    if filepath.is_dir():
        cover_path = filepath / ".cover.jpg"
        if cover_path.exists() and cover_path.stat().st_size > 500:
            return cover_path
        entries = sorted(filepath.glob("*"))
        # Look for companion images or existing images
        for cand in entries:
            if cand.is_file() and not cand.name.startswith(".") and cand.suffix.lower() in [".webp", ".jpg", ".png"]:
                if _ffmpeg_to_file(["-i", str(cand), "-vf", "scale=640:-1"], cover_path, min_size=501):
                    return cover_path
        # Fallback to first video thumbnail
        for v in entries:
            if v.is_file() and not v.name.startswith(".") and v.suffix.lower() in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv"}:
                t = get_or_create_thumbnail(_rel_posix(v, user_root), user_id=user_id)
                if t and t.exists():
                    try:
                        shutil.copyfile(t, cover_path)
                        return cover_path
                    except Exception:
                        pass
        return None

    # If it is an explicit cover file inside a subfolder
    if filepath.name.startswith(".cover"):
        return filepath

    rel_posix = _rel_posix(filepath, user_root)
    thumb_path = THUMBNAILS_DIR / thumbnail_cache_name(user_id, rel_posix)
    if thumb_path.exists() and thumb_path.stat().st_size > 1500:
        return thumb_path

    # Check companion webp or jpg written by yt-dlp next to the media (same stem)
    stem = filepath.stem
    for cand in filepath.parent.iterdir():
        if (cand.is_file() and cand != filepath and cand.stem == stem
                and cand.suffix.lower() in [".webp", ".jpg", ".png"] and cand.stat().st_size > 2000):
            if _ffmpeg_to_file(["-i", str(cand), "-vf", "scale=640:-1"], thumb_path, min_size=1501):
                return thumb_path

    ext = filepath.suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}
    video_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv"}
    audio_exts = {".mp3", ".m4a", ".wav", ".flac", ".opus", ".aac", ".ogg"}

    if ext in image_exts:
        if _ffmpeg_to_file(["-i", str(filepath), "-vf", "scale='min(640,iw)':-1"], thumb_path):
            return thumb_path
        return filepath

    if ext in video_exts:
        dur = 0.0
        with _meta_lock:
            meta = _meta_cache.get(_cache_key(filepath))
            if meta and meta.get("duration_seconds"):
                dur = float(meta["duration_seconds"])
        if not dur:
            dur = _probe_duration(filepath)

        seek = min(5.0, dur * 0.15) if dur > 5.0 else max(0.2, dur * 0.4)
        if _ffmpeg_to_file(["-ss", f"{seek:.2f}", "-i", str(filepath), "-vframes", "1", "-vf", "scale=640:-1"], thumb_path, min_size=1501):
            return thumb_path
        seek2 = min(15.0, dur * 0.25) if dur > 10 else 0.5
        if _ffmpeg_to_file(["-ss", f"{seek2:.2f}", "-i", str(filepath), "-vframes", "1", "-vf", "scale=640:-1"], thumb_path):
            return thumb_path

    elif ext in audio_exts:
        if _ffmpeg_to_file(["-i", str(filepath), "-an", "-vcodec", "copy"], thumb_path, min_size=1000):
            return thumb_path

    return None


TERMINAL_STATUSES = ("completed", "error", "cancelled")
ACTIVE_STATUSES = ("queued", "downloading", "converting")

ALLOWED_MODES = {"video", "audio", "photo", "single", "subtitles"}
ALLOWED_VIDEO_FORMATS = {"mp4", "mkv", "webm", "gif"}
ALLOWED_AUDIO_FORMATS = {"mp3", "m4a", "wav", "flac", "opus", "aac"}
ALLOWED_AUDIO_BITRATES = {"320", "256", "192", "160", "128", "96", "64"}
_TIME_RE = re.compile(r"^\d{1,2}(:\d{1,2}){0,2}(\.\d{1,3})?$|^\d+(\.\d{1,3})?$")
_LANG_RE = re.compile(r"^[A-Za-z0-9_.*-]{1,20}(,[A-Za-z0-9_.*-]{1,20}){0,9}$")


class DownloadLimitError(Exception):
    """Raised when a user or the server has no capacity for another download."""


def _clean_text(value: Optional[str], limit: int, default: str = "") -> str:
    text = "".join(ch for ch in (value or "") if ch.isprintable())
    return text.strip()[:limit] or default


def normalize_download_options(
    *, mode: str, quality: str, video_format: str, audio_format: str, audio_quality: str,
    trim_start: Optional[str], trim_end: Optional[str], subtitles_lang: Optional[str], role: str,
) -> Dict[str, Any]:
    """Validate user-controlled download options against whitelists and apply role limits.

    These values end up in yt-dlp arguments and in output file names, so nothing free-form passes through.
    """
    mode = (mode or "video").strip().lower()
    if mode not in ALLOWED_MODES:
        raise ValueError("Недопустимый режим загрузки")
    if mode == "single":
        mode = "video"

    video_format = (video_format or "mp4").strip().lower()
    if video_format not in ALLOWED_VIDEO_FORMATS:
        raise ValueError("Недопустимый формат видео")
    audio_format = (audio_format or "mp3").strip().lower()
    if audio_format not in ALLOWED_AUDIO_FORMATS:
        raise ValueError("Недопустимый формат аудио")
    audio_quality = str(audio_quality or "320").strip().lower().replace("kbps", "").replace("k", "")
    if audio_quality not in ALLOWED_AUDIO_BITRATES:
        raise ValueError("Недопустимый битрейт аудио")

    role_cfg = ROLE_CONFIGS.get(role, ROLE_CONFIGS["guest"])
    max_height = role_cfg.get("max_height") or 4320

    quality_raw = str(quality or "best").strip().lower()
    if quality_raw == "best":
        # "Best" is capped by the role's resolution limit.
        quality = "best" if max_height >= 4320 else str(max_height)
    else:
        digits = quality_raw[:-1] if quality_raw.endswith("p") else quality_raw
        if not digits.isdigit() or not (144 <= int(digits) <= 4320):
            raise ValueError("Недопустимое качество видео")
        quality = str(min(int(digits), max_height))

    for label, value in (("начала", trim_start), ("конца", trim_end)):
        if value and not _TIME_RE.match(value.strip()):
            raise ValueError(f"Недопустимое время {label} фрагмента")
    trim_start = trim_start.strip() if trim_start else None
    trim_end = trim_end.strip() if trim_end else None

    lang = (subtitles_lang or "en").strip()
    if not _LANG_RE.match(lang):
        raise ValueError("Недопустимый язык субтитров")

    return {
        "mode": mode, "quality": quality, "video_format": video_format, "audio_format": audio_format,
        "audio_quality": audio_quality, "trim_start": trim_start, "trim_end": trim_end, "subtitles_lang": lang,
    }


async def terminate_process(proc: Optional["asyncio.subprocess.Process"], grace: float = 5.0) -> None:
    """Stop a subprocess and its children, wait for it and reap it (no zombies / orphaned ffmpeg)."""
    if proc is None or proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            # Kill the whole tree: yt-dlp spawns ffmpeg children that outlive a plain terminate().
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            proc.terminate()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            pass


def _safe_name(text: Optional[str], limit: int, default: str) -> str:
    """File/folder name component: no path separators or reserved characters, no leading dots."""
    cleaned = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", text or "").strip().lstrip(". ")[:limit].strip()
    return cleaned or default


class TaskState:
    def __init__(self, task_id: str, url: str, title: str = "", format_tag: str = "", playlist_title: Optional[str] = None, user_id: Optional[str] = CREATOR_USERNAME, role: str = "guest"):
        self.id = task_id
        self.url = url
        self.title = title
        self.format_tag = format_tag
        self.playlist_title = playlist_title
        self.user_id = user_id or CREATOR_USERNAME
        self.role = role or "guest"
        self._status = "queued"  # queued, downloading, converting, completed, error, cancelled
        self.finished_at: Optional[float] = None
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
        self.logs = deque(maxlen=200)
        self.created_at = time.time()
        self.target_dir: Optional[Path] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self.async_task: Optional[asyncio.Task] = None

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status = value
        if value in TERMINAL_STATUSES:
            if self.finished_at is None:
                self.finished_at = time.time()
        else:
            self.finished_at = None

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
            "logs": list(self.logs)[-20:],  # last 20 log lines
            "created_at": self.created_at,
            "user_id": self.user_id,
        }


class DownloaderManager:
    def __init__(self):
        self.tasks: Dict[str, TaskState] = {}
        self._semaphore = None
        self._info_semaphore = None
        self._bg_tasks: set = set()

    @property
    def semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            max_c = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
            self._semaphore = asyncio.Semaphore(max(1, max_c))
        return self._semaphore

    @property
    def info_semaphore(self) -> asyncio.Semaphore:
        if self._info_semaphore is None:
            self._info_semaphore = asyncio.Semaphore(max(1, int(os.getenv("MAX_CONCURRENT_INFO", "4"))))
        return self._info_semaphore

    def _spawn(self, coro) -> asyncio.Task:
        """create_task that keeps a strong reference (the loop only holds weak ones)."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def prune_old_tasks(self, max_age_seconds: int = 7200):
        now = time.time()
        to_del = [
            tid for tid, t in self.tasks.items()
            if t.status in TERMINAL_STATUSES and (now - (t.finished_at or t.created_at) > max_age_seconds)
        ]
        for tid in to_del:
            self.tasks.pop(tid, None)

    def get_task(self, task_id: str) -> Optional[TaskState]:
        return self.tasks.get(task_id)

    def get_all_tasks(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """All tasks, newest first. With `user_id` only that owner's tasks are returned."""
        self.prune_old_tasks()
        tasks = [t for t in list(self.tasks.values()) if user_id is None or t.user_id == user_id]
        return [task.to_dict() for task in sorted(tasks, key=lambda t: t.created_at, reverse=True)]

    def clear_finished_tasks(self, user_id: Optional[str] = None) -> int:
        to_delete = [
            tid for tid, task in list(self.tasks.items())
            if task.status in TERMINAL_STATUSES and (user_id is None or task.user_id == user_id)
        ]
        for tid in to_delete:
            self.tasks.pop(tid, None)
        return len(to_delete)

    def count_active(self, user_id: str) -> int:
        return sum(1 for t in list(self.tasks.values()) if t.user_id == user_id and t.status in ACTIVE_STATUSES)

    def active_task_capacity(self, user_id: str, role: str) -> int:
        """How many more tasks this user may enqueue right now."""
        limit = ROLE_CONFIGS.get(role, ROLE_CONFIGS["guest"]).get("max_active_tasks") or 10
        return max(0, limit - self.count_active(user_id))

    async def shutdown(self) -> None:
        """Stop every running download (called on server shutdown)."""
        for task in list(self.tasks.values()):
            if task.status in ACTIVE_STATUSES:
                self.cancel_task(task.id)
        pending = [t for t in list(self._bg_tasks) if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=10)
        flush_meta_cache()

    # --- metadata extraction ---
    async def get_video_info(self, url: str) -> Dict[str, Any]:
        """Extract rich video, photo, carousel, or playlist metadata using yt-dlp and GraphQL."""
        url = validate_media_url(url)
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

        cmd = get_ytdlp_cmd() + get_ytdlp_global_args() + [
            "--dump-single-json",
            "--no-warnings",
            "--no-update",
            "--geo-bypass",
            "--flat-playlist",
            "--socket-timeout", "30",
            "--", normalized_url
        ]

        info_timeout = float(os.getenv("MEDIAPULL_INFO_TIMEOUT", "90"))
        async with self.info_semaphore:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=info_timeout)
            except asyncio.TimeoutError:
                await terminate_process(proc)
                raise ValueError("Сервис не ответил вовремя. Попробуйте позже.")
            except asyncio.CancelledError:
                await terminate_process(proc)
                raise

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()[-800:]
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
        resolution_options = build_resolution_options(raw_data.get("formats") or [])

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
                content = safe_fetch_bytes(thumb_url, max_bytes=MAX_IMAGE_BYTES, timeout=12)
                if len(content) > 500 and looks_like_image(content):
                    tmp = cover_file.with_name(".cover.tmp")
                    tmp.write_bytes(content)
                    os.replace(tmp, cover_file)
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
        user_id: Optional[str] = CREATOR_USERNAME,
        role: str = "guest"
    ) -> TaskState:
        """Validate the request and queue a download. Raises ValueError (bad input) or DownloadLimitError."""
        url = validate_media_url(url)
        opts = normalize_download_options(
            mode=mode, quality=quality, video_format=video_format, audio_format=audio_format,
            audio_quality=audio_quality, trim_start=trim_start, trim_end=trim_end,
            subtitles_lang=subtitles_lang, role=role,
        )
        mode = opts["mode"]
        is_photo = bool(is_photo or mode == "photo")
        if is_photo:
            mode = "photo"
        photo_url = validate_media_url(photo_url) if photo_url else None
        try:
            playlist_thumbnail = validate_media_url(playlist_thumbnail) if playlist_thumbnail else None
        except ValueError:
            playlist_thumbnail = None  # a bad cover must not fail the download itself
        title = _clean_text(title, 200, "Media Download")
        playlist_title = _clean_text(playlist_title, 120) or None
        if playlist_index is not None:
            playlist_index = max(0, min(int(playlist_index), 9999))

        if task_id in self.tasks:
            raise ValueError("Задача с таким идентификатором уже существует")
        if not is_temp_user(user_id) and self.active_task_capacity(user_id or CREATOR_USERNAME, role) <= 0:
            raise DownloadLimitError("Достигнут лимит одновременных загрузок для вашего тарифа. Дождитесь завершения текущих.")
        try:
            if shutil.disk_usage(DOWNLOADS_DIR).free < MIN_FREE_DISK_BYTES:
                raise DownloadLimitError("На сервере недостаточно свободного места. Попробуйте позже.")
        except OSError:
            pass

        if is_photo:
            format_tag = "JPG Фото"
        elif mode == "subtitles":
            format_tag = f"Субтитры {opts['subtitles_lang']}"
        elif mode == "video":
            if opts["video_format"] == "gif":
                format_tag = "GIF Анимация"
            else:
                q = opts["quality"]
                q_label = f"{q}p" if q.isdigit() else "Best"
                format_tag = f"{q_label} {opts['video_format'].upper()}"
        else:
            format_tag = f"{opts['audio_quality']}kbps {opts['audio_format'].upper()}"

        state = TaskState(task_id, url, title, format_tag=format_tag, playlist_title=playlist_title, user_id=user_id, role=role)
        self.tasks[task_id] = state

        # Run background async download task
        state.async_task = self._spawn(
            self._execute_download(
                state,
                mode=mode,
                quality=opts["quality"],
                video_format=opts["video_format"],
                audio_format=opts["audio_format"],
                audio_quality=opts["audio_quality"],
                trim_start=opts["trim_start"],
                trim_end=opts["trim_end"],
                download_subtitles=download_subtitles,
                subtitles_lang=opts["subtitles_lang"],
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

    def _fail(self, state: TaskState, message: str) -> None:
        state.status = "error"
        state.error = message
        state.logs.append(f"[Ошибка] {message}")

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
        role = state.role
        role_cfg = ROLE_CONFIGS.get(role, ROLE_CONFIGS["guest"])
        limit_bytes = role_cfg.get("storage_limit_bytes")
        user_root = get_user_root(state.user_id, role)
        state.logs.append("В очереди загрузок...")

        try:
            async with self.semaphore:
                if state.status == "cancelled":
                    return
                state.status = "downloading"

                # Quota is checked when the download actually starts (a batch queued all at once
                # would otherwise pass the check together) and caps the size of this very download.
                max_bytes = None
                if limit_bytes:
                    usage = await asyncio.to_thread(get_user_disk_usage, state.user_id, role, False)
                    max_bytes = limit_bytes - usage["total_bytes"]
                    if max_bytes <= 0:
                        self._fail(state, f"Превышена квота хранилища для тарифа {role_cfg['title']} ({role_cfg['storage_limit_display']}). Освободите место в файлах.")
                        return

                target_dir = user_root
                prefix = ""
                if playlist_title:
                    clean_folder = _safe_name(playlist_title, 80, "Плейлист")
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
                        self._spawn(self._save_playlist_cover(target_dir, playlist_thumbnail))

                    if playlist_index is not None:
                        prefix = f"{playlist_index:02d}. "
                state.target_dir = target_dir

                if is_photo or mode == "photo":
                    await self._download_photo(state, user_root, target_dir, prefix, photo_url, min(max_bytes, MAX_IMAGE_BYTES) if max_bytes else MAX_IMAGE_BYTES)
                    return

                await self._run_ytdlp(
                    state, user_root, target_dir, prefix, max_bytes,
                    mode=mode, quality=quality, video_format=video_format, audio_format=audio_format,
                    audio_quality=audio_quality, trim_start=trim_start, trim_end=trim_end,
                    download_subtitles=download_subtitles, subtitles_lang=subtitles_lang,
                    embed_subtitles=embed_subtitles, embed_thumbnail=embed_thumbnail, split_chapters=split_chapters,
                )

        except asyncio.CancelledError:
            state.status = "cancelled"
            state.logs.append("Загрузка отменена пользователем.")
        except Exception as ex:
            self._fail(state, str(ex))
        finally:
            # Never leave a child process behind, whatever path got us here.
            if state.process is not None and state.process.returncode is None:
                await terminate_process(state.process)
            if state.status in ("cancelled", "error"):
                self._cleanup_partials(state)
            invalidate_usage(state.user_id)

    def _cleanup_partials(self, state: TaskState) -> None:
        """Remove .part / fragment / intermediate-format leftovers of a cancelled or failed download."""
        target_dir = state.target_dir
        if not target_dir or not target_dir.exists():
            return
        for other in list(self.tasks.values()):
            if other is not state and other.target_dir == target_dir and other.status in ("downloading", "converting"):
                return  # another task is writing into the same folder
        pattern = re.compile(r"(\.(part|ytdl|temp)$)|(\.part-Frag\d+$)|(\.f\d+\.[A-Za-z0-9]+(\.part)?$)")
        since = state.created_at - 5
        try:
            for p in target_dir.iterdir():
                if p.is_file() and pattern.search(p.name) and p.stat().st_mtime >= since:
                    p.unlink(missing_ok=True)
        except OSError:
            pass

    async def _download_photo(self, state: TaskState, user_root: Path, target_dir: Path, prefix: str, photo_url: Optional[str], max_bytes: int) -> None:
        state.logs.append("Скачивание фотографии в оригинальном разрешении...")
        img_target_url = photo_url
        if not img_target_url or "instagram.com/p/" in img_target_url or "instagram.com/reel/" in img_target_url:
            try:
                ig_info = await asyncio.to_thread(extract_instagram_post, state.url)
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

        clean_name = _safe_name(state.title, 120, "photo")
        out_file = target_dir / f"{prefix}{clean_name} [JPG].jpg"

        def fetch_img():
            content = safe_fetch_bytes(img_target_url, max_bytes=max_bytes, timeout=20)
            if not looks_like_image(content):
                raise ValueError("Не удалось получить валидное изображение (получен не файл изображения)")
            tmp = out_file.with_name(out_file.name + ".part")
            tmp.write_bytes(content)
            os.replace(tmp, out_file)

        try:
            await asyncio.to_thread(fetch_img)
        except Exception as e_img:
            self._fail(state, f"Ошибка скачивания фото: {e_img}")
            return

        state.status = "completed"
        state.progress = 100.0
        state.filepath = str(out_file)
        state.filename = _rel_posix(out_file, user_root)
        state.downloaded_str = format_size(out_file.stat().st_size)
        await asyncio.to_thread(self._index_file, out_file, user_root, state.user_id, state.filename)
        state.logs.append(f"Фото успешно сохранено: {state.filename}")

    @staticmethod
    def _index_file(path: Path, user_root: Path, user_id: Optional[str], filename: Optional[str]) -> None:
        """Pre-cache metadata and thumbnail right after a download (runs in a worker thread)."""
        try:
            get_file_metadata(path, base_dir=user_root)
            if filename:
                get_or_create_thumbnail(filename, user_id=user_id)
        except Exception:
            pass

    async def _run_ytdlp(
        self, state: TaskState, user_root: Path, target_dir: Path, prefix: str, max_bytes: Optional[int], *,
        mode: str, quality: str, video_format: str, audio_format: str, audio_quality: str,
        trim_start: Optional[str], trim_end: Optional[str], download_subtitles: bool,
        subtitles_lang: Optional[str], embed_subtitles: bool, embed_thumbnail: bool, split_chapters: bool,
    ) -> None:
        state.logs.append(f"Инициализация скачивания: {state.url}")

        # '%' in the destination path would be read as a yt-dlp template field.
        base = str(target_dir).replace("%", "%%")
        sep = os.sep
        pfx = prefix.replace("%", "%%")
        if mode == "subtitles":
            out_template = f"{base}{sep}{pfx}%(title).150B [subs].%(ext)s"
        elif mode == "audio":
            out_template = f"{base}{sep}{pfx}%(title).150B [{audio_quality}kbps].%(ext)s"
        elif video_format == "gif":
            out_template = f"{base}{sep}{pfx}%(title).150B [GIF_TEMP].%(ext)s"
        elif quality != "best":
            out_template = f"{base}{sep}{pfx}%(title).150B [{quality}p].%(ext)s"
        else:
            out_template = f"{base}{sep}{pfx}%(title).150B [%(height)sp].%(ext)s"

        cmd = get_ytdlp_cmd() + get_ytdlp_global_args() + [
            "--no-update",
            "--no-warnings",
            "--no-mtime",
            "--newline",
            "--progress",
            "--geo-bypass",
            "-o", out_template,
            "--progress-template", "%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s|%(progress._downloaded_bytes_str)s|%(progress._total_bytes_estimate_str)s",
            "--no-playlist",
            "--retries", "10",
            "--fragment-retries", "10",
            "--file-access-retries", "3",
            "--socket-timeout", "30",
        ]
        if max_bytes:
            cmd.extend(["--max-filesize", str(max_bytes)])

        # Mode setup
        if mode == "subtitles":
            cmd.extend([
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs", subtitles_lang or "en",
                "--convert-subs", "srt",
            ])
        elif mode == "audio":
            cmd.extend(["--write-thumbnail"])
            cmd.extend([
                "-x",
                "--audio-format", audio_format,
                "--audio-quality", "0" if audio_quality == "320" else f"{audio_quality}K",
            ])
            if embed_thumbnail:
                cmd.extend(["--embed-thumbnail", "--add-metadata"])
        else:
            cmd.append("--write-thumbnail")
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
                h = int(quality)
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
            start_str = trim_start or "00:00:00"
            end_str = trim_end or "inf"
            cmd.extend([
                "--download-sections", f"*{start_str}-{end_str}",
                "--force-keyframes-at-cuts"
            ])
            state.logs.append(f"Обрезка фрагмента: с {start_str} по {end_str}")

        # Subtitles
        if download_subtitles and mode != "subtitles":
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

        # Target URL. '--' ends option parsing, so a URL can never be read as a yt-dlp option.
        cmd.extend(["--", state.url])

        state.logs.append("Команда yt-dlp запущена...")
        sub_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=sub_env,
            limit=1024 * 1024,
        )
        state.process = proc

        final_filepath = None
        last_error = None
        progress_regex = re.compile(r"(\d+(?:\.\d+)?)%\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|\n]+)")
        merger_regex = re.compile(r'Merging formats into "([^"]+)"')
        idle_timeout = float(os.getenv("MEDIAPULL_DOWNLOAD_IDLE_TIMEOUT", "900"))

        # Stream stdout line by line
        while True:
            try:
                line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                await terminate_process(proc)
                self._fail(state, f"yt-dlp не подавал признаков активности {int(idle_timeout)} сек. — загрузка остановлена")
                return
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

        if proc.returncode != 0:
            if state.status != "cancelled":
                self._fail(state, last_error or f"Процесс завершился с кодом {proc.returncode}")
            return

        if mode == "subtitles":
            found = [
                p for p in target_dir.glob("*")
                if p.is_file() and p.suffix.lower() in (".srt", ".vtt") and p.stat().st_mtime >= state.created_at - 10
            ]
            found.sort(key=lambda p: (p.suffix.lower() != ".srt", -p.stat().st_mtime))
            if not found:
                self._fail(state, "Субтитры для выбранного языка не найдены")
                return
            state.filepath = str(found[0])
            state.filename = _rel_posix(found[0], user_root)
            state.status = "completed"
            state.progress = 100.0
            state.downloaded_str = format_size(found[0].stat().st_size)
            state.logs.append(f"Субтитры сохранены: {state.filename}")
            return

        # Determine the downloaded file
        if final_filepath and os.path.exists(final_filepath):
            state.filepath = final_filepath
            state.filename = _rel_posix(Path(final_filepath), user_root)
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
                state.filename = _rel_posix(newest, user_root)

        # Check if GIF conversion is required
        if video_format == "gif" and state.filepath and os.path.exists(state.filepath):
            state.status = "converting"
            state.logs.append("Создание плавной GIF-анимации через 2-pass FFmpeg palette...")
            src_p = Path(state.filepath)
            gif_name = src_p.stem.replace(" [GIF_TEMP]", "") + " [GIF].gif"
            gif_path = target_dir / gif_name
            gif_cmd = [
                "ffmpeg", "-nostdin", "-y", "-i", str(src_p),
                "-vf", "fps=15,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                str(gif_path)
            ]
            try:
                p_gif = await asyncio.create_subprocess_exec(*gif_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                state.process = p_gif
                try:
                    await asyncio.wait_for(p_gif.wait(), timeout=600)
                except asyncio.TimeoutError:
                    await terminate_process(p_gif)
                    state.logs.append("Создание GIF прервано по таймауту")
                if gif_path.exists() and gif_path.stat().st_size > 0 and p_gif.returncode == 0:
                    src_p.unlink(missing_ok=True)
                    state.filepath = str(gif_path)
                    state.filename = _rel_posix(gif_path, user_root)
                    state.logs.append(f"GIF успешно сгенерирован: {state.filename}")
            except Exception as e_gif:
                state.logs.append(f"Ошибка при создании GIF: {e_gif}")

        state.status = "completed"
        state.progress = 100.0

        if state.filepath and os.path.exists(state.filepath):
            try:
                state.downloaded_str = format_size(os.path.getsize(state.filepath))
            except OSError:
                pass
            await asyncio.to_thread(self._index_file, Path(state.filepath), user_root, state.user_id, state.filename)

        state.logs.append(f"Загрузка успешно завершена: {state.filename}")

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a task. Returns False only when the task does not exist."""
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task.status in ACTIVE_STATUSES:
            task.status = "cancelled"
            if task.async_task and not task.async_task.done():
                # The task's own handler stops the subprocess tree and removes partial files.
                task.async_task.cancel()
        return True

    def list_history(self, user_id: Optional[str] = CREATOR_USERNAME, role: str = "guest") -> Dict[str, Any]:
        """List files and structured playlists for a specific user according to their role."""
        user_root = get_user_root(user_id, role)
        if not user_root.exists():
            return {"files": [], "playlists": []}

        is_creator_root = (user_root == DOWNLOADS_DIR)
        owner = CREATOR_USERNAME if is_creator_root else user_id
        now = time.time()

        role_cfg = ROLE_CONFIGS.get(role, ROLE_CONFIGS["guest"])
        role_ttl = None if is_creator_root else role_cfg.get("ttl_seconds")
        is_permanent = (role_ttl is None)

        all_files = []
        playlists_dict: Dict[str, Dict[str, Any]] = {}

        def media_stems(entries: List[Path]) -> tuple:
            """Stems of media files in a folder, computed once per folder (not once per image)."""
            stems, cleaned = set(), set()
            for other in entries:
                if other.is_file() and (other.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS or other.suffix.lower() in (".part", ".ytdl")):
                    stems.add(other.stem)
                    cleaned.add(re.sub(r"\s*\[.*?\]", "", other.stem).strip().lower())
            return stems, cleaned

        def is_companion_thumbnail(img_p: Path, stems: set, cleaned: set) -> bool:
            if img_p.suffix.lower() not in IMAGE_EXTS:
                return False
            clean_stem = re.sub(r"\s*\[.*?\]", "", img_p.stem).strip().lower()
            return img_p.stem in stems or bool(clean_stem and clean_stem in cleaned)

        def decorate(meta: Dict[str, Any]) -> Dict[str, Any]:
            meta["owner"] = owner
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
            return meta

        def _nat_key(item):
            idx = item.get("playlist_index")
            if idx is not None:
                return (0, idx, item.get("basename", ""))
            bname = item.get("basename", "")
            m_num = re.match(r"^(\d+)", bname)
            if m_num:
                return (0, int(m_num.group(1)), bname)
            return (1, 0, bname.lower())

        root_entries = sorted(user_root.iterdir())

        # 1. Scan subdirectories (playlist folders)
        for folder in root_entries:
            if is_creator_root and folder.name in RESERVED_ROOT_DIRS:
                continue
            if not folder.is_dir() or folder.name.startswith("."):
                continue

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

            entries = sorted(folder.glob("*"), key=lambda f: f.name)
            stems, cleaned = media_stems(entries)
            for p in entries:
                if p.is_file() and not p.name.startswith("."):
                    if p.suffix.lower() in MEDIA_EXTS and not is_companion_thumbnail(p, stems, cleaned):
                        meta = decorate(get_file_metadata(p, base_dir=user_root))
                        meta["playlist_name"] = playlist_title
                        folder_files.append(meta)
                        all_files.append(meta)

            folder_files.sort(key=_nat_key)
            if not folder_files:
                continue

            total_bytes = sum(f.get("size_bytes", 0) for f in folder_files)
            total_duration = sum(f.get("duration_seconds", 0) for f in folder_files)

            # Ensure .cover.jpg exists for this playlist folder
            cover_file = folder / ".cover.jpg"
            if not cover_file.exists() or cover_file.stat().st_size < 500:
                try:
                    get_or_create_thumbnail(_rel_posix(folder, user_root), user_id=user_id)
                except Exception:
                    pass

            has_cover = cover_file.exists() and cover_file.stat().st_size > 0
            thumb_url = (
                _thumb_url(_rel_posix(cover_file, user_root)) if has_cover
                else folder_files[0].get("thumbnail_url", "")
            )
            first_file_name = folder_files[0]["filename"]

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
                "owner": owner,
                "files": folder_files
            }

        # 2. Scan root files (standalone downloads)
        root_stems, root_cleaned = media_stems(root_entries)
        for p in root_entries:
            if p.is_file() and not p.name.startswith("."):
                if p.suffix.lower() in MEDIA_EXTS and not is_companion_thumbnail(p, root_stems, root_cleaned):
                    all_files.append(decorate(get_file_metadata(p, base_dir=user_root)))

        # Sort all files by mtime descending
        all_files.sort(key=lambda f: f.get("created_at", 0), reverse=True)
        playlists_list = sorted(playlists_dict.values(), key=lambda pl: pl.get("created_at", 0), reverse=True)

        return {
            "files": all_files,
            "playlists": playlists_list
        }

    def get_or_create_thumbnail(self, filename: str, user_id: Optional[str] = None) -> Optional[Path]:
        return get_or_create_thumbnail(filename, user_id=user_id)

    @staticmethod
    def _find_by_basename(user_root: Path, filename: str) -> Optional[Path]:
        """Fallback when the client sent only a base name: look in the root and its direct sub-folders."""
        name = Path(urllib.parse.unquote(filename)).name
        if not name or name.startswith("."):
            return None
        folders = [user_root] + [
            d for d in user_root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and not (user_root == DOWNLOADS_DIR and d.name in RESERVED_ROOT_DIRS)
        ]
        matches = [d / name for d in folders if (d / name).is_file()]
        return matches[0] if len(matches) == 1 else None

    def delete_file(self, filename: str, user_id: Optional[str] = None) -> bool:
        user_root = get_user_root(user_id).resolve()
        target = resolve_download_path(filename, user_id)
        if not target or not target.is_file():
            target = self._find_by_basename(user_root, filename)
        if not target or not target.is_file():
            return False

        parent_dir = target.parent
        rel_name = _rel_posix(target, user_root)
        target.unlink(missing_ok=True)

        # Remove cached thumbnail and metadata
        (THUMBNAILS_DIR / thumbnail_cache_name(user_id, rel_name)).unlink(missing_ok=True)
        with _meta_lock:
            _meta_cache.pop(_cache_key(target), None)
        _save_meta_cache()

        # Clean companion webp, vtt, srt, jpg, part next to the file ("<stem>.ext" / "<stem>.<lang>.vtt")
        stem = target.stem
        if parent_dir.exists():
            for cand in parent_dir.iterdir():
                if cand.is_file() and cand != target and (cand.stem == stem or cand.name.startswith(stem + ".")):
                    if cand.suffix.lower() in [".webp", ".jpg", ".jpeg", ".png", ".part", ".ytdl", ".vtt", ".srt", ".ass"]:
                        cand.unlink(missing_ok=True)

        # If inside a playlist folder and no more media files remain, remove folder
        if parent_dir != user_root and parent_dir.exists():
            remaining_media = [
                f for f in parent_dir.glob("*")
                if f.is_file() and not f.name.startswith(".") and f.suffix.lower() not in [".json", ".webp", ".jpg", ".part", ".vtt", ".srt"]
            ]
            if not remaining_media:
                shutil.rmtree(parent_dir, ignore_errors=True)

        invalidate_usage()
        return True

    def delete_files_batch(self, filenames: List[str], user_id: Optional[str] = None) -> Dict[str, Any]:
        deleted_count = 0
        for fn in filenames:
            if self.delete_file(fn, user_id=user_id):
                deleted_count += 1
        return {"deleted_count": deleted_count}

    def delete_playlist(self, folder_name: str, user_id: Optional[str] = None) -> bool:
        user_root = get_user_root(user_id).resolve()
        target = resolve_download_path(folder_name, user_id)
        # Only direct, non-internal sub-folders of the owner's root are playlists.
        if (not target or not target.is_dir() or target == user_root or target.parent != user_root
                or (user_root == DOWNLOADS_DIR.resolve() and target.name in RESERVED_ROOT_DIRS)):
            return False

        for f in target.glob("*"):
            if f.is_file():
                try:
                    (THUMBNAILS_DIR / thumbnail_cache_name(user_id, _rel_posix(f, user_root))).unlink(missing_ok=True)
                    with _meta_lock:
                        _meta_cache.pop(_cache_key(f), None)
                except Exception:
                    pass

        _save_meta_cache()
        shutil.rmtree(target, ignore_errors=True)
        invalidate_usage()
        return True

    def get_file_subtitles(self, filename: str, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        target = resolve_download_path(filename, user_id)
        if not target or not target.exists() or not target.is_file():
            return []

        quoted_name = urllib.parse.quote(filename, safe="")
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
                        "url": f"/api/subtitles-track?filename={quoted_name}&track_id={idx}"
                    })
        except Exception:
            pass

        # 2. Companion external subtitle files ("<stem>.<lang>.vtt")
        stem = target.stem
        parent_dir = target.parent
        try:
            for cand in parent_dir.iterdir():
                if cand.is_file() and cand.name.startswith(stem + ".") and cand.suffix.lower() in [".vtt", ".srt"]:
                    sub_lang = "sub"
                    parts = cand.stem.split(".")
                    if len(parts) > 1:
                        sub_lang = parts[-1]
                    subs.append({
                        "id": cand.name,
                        "lang": sub_lang,
                        "label": f"{sub_lang.upper()} (файл)",
                        "type": "companion",
                        "url": f"/api/subtitles-track?filename={quoted_name}&track_id={urllib.parse.quote(cand.name, safe='')}"
                    })
        except Exception:
            pass

        return subs

    def extract_subtitle_vtt(self, filename: str, track_id: str, user_id: Optional[str] = None) -> Optional[str]:
        target = resolve_download_path(filename, user_id)
        if not target or not target.exists() or not target.is_file():
            return None

        track_id = (track_id or "").strip()
        if track_id.lower().endswith(".vtt.vtt"):
            track_id = track_id[:-4]

        # Embedded track: a plain stream index
        if re.fullmatch(r"\d{1,3}", track_id):
            try:
                proc = subprocess.run(
                    ["ffmpeg", "-nostdin", "-y", "-i", str(target), "-map", f"0:{track_id}", "-f", "webvtt", "-"],
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

        # Companion file: must be a bare file name that sits next to the media file
        if (not track_id or track_id != Path(track_id).name or "/" in track_id or "\\" in track_id
                or track_id.startswith(".")):
            return None
        companion = target.parent / track_id
        if not companion.is_file():
            return None
        suffix = companion.suffix.lower()
        if suffix == ".vtt":
            try:
                with open(companion, "r", encoding="utf-8", errors="replace") as fh:
                    return fh.read(5 * 1024 * 1024)
            except Exception:
                return None
        if suffix == ".srt":
            try:
                proc = subprocess.run(
                    ["ffmpeg", "-nostdin", "-y", "-i", str(companion), "-f", "webvtt", "-"],
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
        if not target or not target.is_dir():
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
