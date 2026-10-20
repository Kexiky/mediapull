"""Keeps yt-dlp current: extractors break every few weeks when sites change, so a stale yt-dlp is the most
common reason downloads suddenly fail."""
import asyncio
import os
import sys
from typing import Tuple

from app.downloader import get_ytdlp_cmd

UPDATE_INTERVAL_HOURS = float(os.getenv("MEDIAPULL_YTDLP_UPDATE_HOURS", "24"))


def auto_update_enabled() -> bool:
    return os.getenv("MEDIAPULL_YTDLP_AUTO_UPDATE", "").strip().lower() in ("1", "true", "yes", "on")


async def _run(cmd, timeout: float) -> Tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, "timeout"
    return proc.returncode, out.decode(errors="replace").strip()


async def ytdlp_version() -> str:
    code, out = await _run(get_ytdlp_cmd() + ["--version"], timeout=30)
    return out.splitlines()[-1].strip() if code == 0 and out else "unknown"


async def update_ytdlp() -> Tuple[bool, str]:
    """Upgrade yt-dlp with pip. Returns (updated, human readable message)."""
    before = await ytdlp_version()
    cmd = [sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", "-q", "yt-dlp"]
    if sys.prefix == sys.base_prefix:
        cmd.append("--user")  # system interpreter (e.g. Docker): the site-packages are not writable for a non-root user
    code, out = await _run(cmd, timeout=600)
    if code != 0:
        return False, f"pip failed: {out[-300:]}"
    after = await ytdlp_version()
    return after != before, f"yt-dlp {before} → {after}" if after != before else f"yt-dlp {after} (актуальная версия)"


async def ytdlp_update_loop(interval_hours: float = UPDATE_INTERVAL_HOURS, initial_delay: float = 120) -> None:
    """Background task: update yt-dlp now and then every `interval_hours`."""
    delay = initial_delay
    while True:
        try:
            await asyncio.sleep(delay)
            delay = max(1.0, interval_hours) * 3600
            _, message = await update_ytdlp()
            print(f"[Updater] {message}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Updater] update failed: {e}")
            delay = 3600
