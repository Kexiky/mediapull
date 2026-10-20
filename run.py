import sys
import os

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import uvicorn
import argparse
import getpass

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MediaPull Server")
    parser.add_argument("--reset-admin", action="store_true", help="Сбросить пароль администратора (kexiky)")
    parser.add_argument("--port", type=int, default=None, help="Порт сервера (по умолчанию из env PORT или 8000)")
    parser.add_argument("--host", type=str, default=None, help="Хост сервера (по умолчанию из env HOST или 127.0.0.1)")
    parser.add_argument("--reload", dest="reload", action="store_true", default=None, help="Включить автоперезагрузку при изменении кода")
    parser.add_argument("--no-reload", dest="reload", action="store_false", help="Отключить автоперезагрузку (рекомендуется для Docker/production)")
    args = parser.parse_args()

    if args.reset_admin:
        from app.database import reset_admin_password
        print("\n=== Сброс пароля администратора (kexiky) ===")
        try:
            pwd = getpass.getpass("Введите новый пароль (не менее 6 символов): ")
            pwd_confirm = getpass.getpass("Повторите новый пароль: ")
            if pwd != pwd_confirm:
                print("Ошибка: Пароли не совпадают!")
                sys.exit(1)
            reset_admin_password(pwd)
            print("Пароль администратора 'kexiky' успешно обновлен в базе данных.")
            sys.exit(0)
        except Exception as e:
            print(f"Ошибка: {e}")
            sys.exit(1)

    port = args.port or int(os.environ.get("PORT", 8000))
    host = args.host or os.environ.get("HOST", "127.0.0.1")

    if args.reload is not None:
        reload_enabled = args.reload
    else:
        reload_env = os.environ.get("RELOAD", "").strip().lower()
        if reload_env in ("false", "0", "no", "off"):
            reload_enabled = False
        elif reload_env in ("true", "1", "yes", "on"):
            reload_enabled = True
        else:
            # Auto-reload is a development feature: only enable it by default for a loopback-only server.
            reload_enabled = host in ("127.0.0.1", "localhost", "::1")

    print("\n" + "="*60)
    print("MediaPull — Universal Media Downloader")
    print("by Kexiky | Fast • Multiplatform • Permanent Storage")
    print(f"Server running at: http://{host}:{port}")
    print(f"Auto-reload: {'Enabled' if reload_enabled else 'Disabled'}")
    print("="*60 + "\n")
    uvicorn.run("app.main:app", host=host, port=port, reload=reload_enabled, log_level="info")
