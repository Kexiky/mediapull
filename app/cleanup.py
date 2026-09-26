import os
import shutil
import time
import asyncio
from pathlib import Path
from typing import Dict, Any

from app.downloader import DOWNLOADS_DIR, THUMBNAILS_DIR
from app.database import get_user_by_username, ROLE_CONFIGS

GUEST_TTL_SECONDS = 24 * 3600  # 24 hours
USER_TTL_SECONDS = 7 * 86400   # 7 days
PREMIUM_TTL_SECONDS = 30 * 86400 # 30 days


import hashlib


def _clean_directory_by_ttl(target_dir: Path, ttl_seconds: int, now: float) -> tuple[int, int]:
    """Recursively clean files older than ttl_seconds in target_dir."""
    if not target_dir.exists() or not target_dir.is_dir():
        return 0, 0

    deleted_count = 0
    freed_bytes = 0
    user_prefix = target_dir.name

    for file_path in list(target_dir.rglob("*")):
        if not file_path.is_file() or file_path.name.startswith("."):
            continue

        try:
            st = file_path.stat()
            file_age = now - st.st_mtime
            if file_age >= ttl_seconds:
                f_size = st.st_size
                rel_str = str(file_path.relative_to(target_dir))
                file_path.unlink(missing_ok=True)
                deleted_count += 1
                freed_bytes += f_size

                # Remove matching thumbnails in .thumbnails (MD5 hashed keys)
                try:
                    k1 = hashlib.md5(f"{user_prefix}:{rel_str.strip('/\\')}".encode("utf-8")).hexdigest()
                    k2 = hashlib.md5(rel_str.strip('/\\').encode("utf-8")).hexdigest()
                    k3 = hashlib.md5(file_path.name.encode("utf-8")).hexdigest()
                    for cand_name in [f"{k1}.jpg", f"{k2}.jpg", f"{k3}.jpg", f"{file_path.name}.jpg"]:
                        (THUMBNAILS_DIR / cand_name).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            print(f"[Cleanup] Error removing {file_path}: {e}")

    # Remove empty subfolders
    for sub_dir in sorted(target_dir.rglob("*"), key=lambda p: len(str(p)), reverse=True):
        if sub_dir.is_dir() and not any(sub_dir.iterdir()):
            try:
                sub_dir.rmdir()
            except Exception:
                pass

    return deleted_count, freed_bytes


def cleanup_expired_files() -> Dict[str, Any]:
    """
    Run garbage collection:
    1. downloads/guests/*: delete files older than 24h
    2. downloads/users/<username>/*: delete files older than role TTL (user: 7d, premium: 30d, admin: never)
    3. Root downloads/* (kexiky / admin) is NEVER touched.
    """
    now = time.time()
    total_deleted = 0
    total_freed = 0

    # 1. Clean guest folders (24h TTL)
    guests_dir = DOWNLOADS_DIR / "guests"
    if guests_dir.exists() and guests_dir.is_dir():
        for guest_folder in list(guests_dir.iterdir()):
            if guest_folder.is_dir():
                d_count, f_bytes = _clean_directory_by_ttl(guest_folder, GUEST_TTL_SECONDS, now)
                total_deleted += d_count
                total_freed += f_bytes
                if not any(guest_folder.iterdir()):
                    try:
                        guest_folder.rmdir()
                    except Exception:
                        pass

    # 2. Clean registered user folders based on their role TTL
    users_dir = DOWNLOADS_DIR / "users"
    if users_dir.exists() and users_dir.is_dir():
        for user_folder in list(users_dir.iterdir()):
            if not user_folder.is_dir():
                continue
            username = user_folder.name
            user_rec = get_user_by_username(username)
            role = user_rec.get("role", "user") if user_rec else "user"
            
            # Admin files in user folder are permanent
            if role == "admin":
                continue

            role_cfg = ROLE_CONFIGS.get(role, ROLE_CONFIGS["user"])
            user_ttl = role_cfg.get("ttl_seconds") or USER_TTL_SECONDS

            d_count, f_bytes = _clean_directory_by_ttl(user_folder, user_ttl, now)
            total_deleted += d_count
            total_freed += f_bytes

    # 3. Clean orphan thumbnails older than 30 days
    if THUMBNAILS_DIR.exists():
        thumb_ttl = 30 * 86400
        for thumb_f in list(THUMBNAILS_DIR.glob("*.jpg")):
            if thumb_f.is_file():
                try:
                    if now - thumb_f.stat().st_mtime > thumb_ttl:
                        thumb_f.unlink(missing_ok=True)
                except Exception:
                    pass

    if total_deleted > 0:
        mb = total_freed / (1024 * 1024)
        print(f"[Cleanup] Multi-user GC: removed {total_deleted} expired files ({mb:.2f} MB freed)")

    return {
        "deleted_files": total_deleted,
        "freed_bytes": total_freed,
        "freed_mb": round(total_freed / (1024 * 1024), 2)
    }


def cleanup_expired_guest_files(ttl_seconds: int = GUEST_TTL_SECONDS) -> Dict[str, Any]:
    """Compatibility alias."""
    return cleanup_expired_files()


async def cleanup_loop(interval_seconds: int = 900):
    """Background task running every interval_seconds (default 15 mins)."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            cleanup_expired_files()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Cleanup] Error in cleanup loop: {e}")
            await asyncio.sleep(60)
