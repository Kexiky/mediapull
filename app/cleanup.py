import os
import shutil
import time
import asyncio
from pathlib import Path
from typing import Dict, Any

from app.downloader import (
    DOWNLOADS_DIR, THUMBNAILS_DIR, TEMP_DIR, thumbnail_cache_name, prune_meta_cache,
    flush_meta_cache, invalidate_usage, _meta_cache, _meta_lock, _cache_key,
)
from app.database import get_user_by_username, ROLE_CONFIGS

DEFAULT_USER_TTL_SECONDS = ROLE_CONFIGS["user"]["ttl_seconds"]
TEMP_FILE_MAX_AGE = 3600          # generated ZIPs / scratch files
TELEGRAM_TEMP_MAX_AGE = 3600      # leftovers of interrupted bot jobs
ORPHAN_THUMB_MAX_AGE = 30 * 86400


def _clean_directory_by_ttl(target_dir: Path, ttl_seconds: int, now: float) -> tuple[int, int]:
    """Recursively delete files older than ttl_seconds inside an owner's folder."""
    if not target_dir.exists() or not target_dir.is_dir():
        return 0, 0

    deleted_count = 0
    freed_bytes = 0
    owner = target_dir.name
    emptied_by_us = set()

    for dirpath, dirnames, filenames in os.walk(target_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            file_path = Path(dirpath) / name
            try:
                st = file_path.stat()
                if now - st.st_mtime < ttl_seconds:
                    continue
                file_path.unlink(missing_ok=True)
                deleted_count += 1
                emptied_by_us.add(file_path.parent)
                freed_bytes += st.st_size
                try:
                    rel = file_path.relative_to(target_dir).as_posix()
                    (THUMBNAILS_DIR / thumbnail_cache_name(owner, rel)).unlink(missing_ok=True)
                except Exception:
                    pass
                with _meta_lock:
                    _meta_cache.pop(_cache_key(file_path), None)
            except Exception as e:
                print(f"[Cleanup] Error removing {file_path}: {e}")

    # Remove sub-folders that hold no user-visible files any more. Playlist folders keep hidden
    # helper files (.cover.jpg, .playlist_meta.json) which must not keep an empty playlist alive.
    for sub_dir in sorted((p for p in target_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            if (sub_dir.exists() and (sub_dir in emptied_by_us or now - sub_dir.stat().st_mtime > 600)  # spare a folder a download is just creating
                    and all(child.name.startswith(".") and child.is_file() for child in sub_dir.iterdir())):
                shutil.rmtree(sub_dir, ignore_errors=True)
        except Exception:
            pass

    return deleted_count, freed_bytes


def _remove_old_entries(directory: Path, max_age: float, now: float) -> int:
    """Delete files / folders directly inside `directory` that were not modified for `max_age` seconds."""
    removed = 0
    if not directory.exists():
        return 0
    for entry in list(directory.iterdir()):
        try:
            if now - entry.stat().st_mtime <= max_age:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except Exception:
            pass
    return removed


def cleanup_expired_files() -> Dict[str, Any]:
    """
    Run garbage collection:
    1. downloads/guests/*: delete files older than the guest TTL
    2. downloads/users/<username>/*: delete files older than the role TTL (admin: never)
    3. Root downloads/* (creator 'kexiky') is NEVER touched.
    4. Generated ZIPs, interrupted Telegram jobs, orphan thumbnails, stale metadata cache entries.
    """
    now = time.time()
    total_deleted = 0
    total_freed = 0

    # 1. Guest folders
    guests_dir = DOWNLOADS_DIR / "guests"
    guest_ttl = ROLE_CONFIGS["guest"]["ttl_seconds"]
    if guests_dir.is_dir():
        for guest_folder in list(guests_dir.iterdir()):
            if guest_folder.is_dir():
                d_count, f_bytes = _clean_directory_by_ttl(guest_folder, guest_ttl, now)
                total_deleted += d_count
                total_freed += f_bytes
                try:
                    if not any(guest_folder.iterdir()):
                        guest_folder.rmdir()
                except Exception:
                    pass

    # 2. Registered users, by their role TTL
    users_dir = DOWNLOADS_DIR / "users"
    if users_dir.is_dir():
        for user_folder in list(users_dir.iterdir()):
            if not user_folder.is_dir():
                continue
            user_rec = get_user_by_username(user_folder.name)
            role = user_rec.get("role", "user") if user_rec else "user"
            role_cfg = ROLE_CONFIGS.get(role, ROLE_CONFIGS["user"])
            if role_cfg.get("ttl_seconds") is None:
                continue  # permanent storage (admin)

            d_count, f_bytes = _clean_directory_by_ttl(user_folder, role_cfg["ttl_seconds"], now)
            total_deleted += d_count
            total_freed += f_bytes

    # 3. Scratch data: generated archives and abandoned Telegram jobs
    removed_tmp = _remove_old_entries(TEMP_DIR, TEMP_FILE_MAX_AGE, now)
    removed_tmp += _remove_old_entries(DOWNLOADS_DIR / "temp_tg", TELEGRAM_TEMP_MAX_AGE, now)

    # 4. Orphan thumbnails
    if THUMBNAILS_DIR.exists():
        for thumb_f in list(THUMBNAILS_DIR.glob("*.jpg")):
            try:
                if now - thumb_f.stat().st_mtime > ORPHAN_THUMB_MAX_AGE:
                    thumb_f.unlink(missing_ok=True)
            except Exception:
                pass

    # 5. Metadata cache entries of files that no longer exist
    pruned = prune_meta_cache()
    flush_meta_cache()
    invalidate_usage()

    if total_deleted > 0 or removed_tmp or pruned:
        mb = total_freed / (1024 * 1024)
        print(f"[Cleanup] Removed {total_deleted} expired files ({mb:.2f} MB freed), "
              f"{removed_tmp} scratch entries, {pruned} stale cache records")

    return {
        "deleted_files": total_deleted,
        "freed_bytes": total_freed,
        "freed_mb": round(total_freed / (1024 * 1024), 2)
    }


async def cleanup_loop(interval_seconds: int = 900, initial_delay: int = 30):
    """Background task running every interval_seconds (default 15 mins). Work happens in a worker thread."""
    delay = initial_delay
    while True:
        try:
            await asyncio.sleep(delay)
            delay = interval_seconds
            await asyncio.to_thread(cleanup_expired_files)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Cleanup] Error in cleanup loop: {e}")
            delay = 60
