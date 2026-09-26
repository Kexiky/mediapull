#!/usr/bin/env python3
"""
Launcher for MediaPull Telegram Bot
Usage:
  python run_bot.py
  python run_bot.py --test-pipeline "https://youtu.be/..."
  python run_bot.py --token "123456789:ABCDef..."
"""
import sys
import os
import asyncio

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from app.bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n👋 Бот остановлен.")
