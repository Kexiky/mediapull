import uvicorn
import sys
import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "127.0.0.1")
    print("\n" + "="*60)
    print("⚡ MediaPull — Universal Media Downloader")
    print("   by Kexiky | Fast • Multiplatform • Permanent Storage")
    print(f"🌐 Server running at: http://{host}:{port}")
    print("="*60 + "\n")
    uvicorn.run("app.main:app", host=host, port=port, reload=True, log_level="info")
