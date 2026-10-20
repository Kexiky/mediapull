import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from fastapi import Request

TRUST_PROXY = os.getenv("MEDIAPULL_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes", "on")


def client_ip(request: Request) -> str:
    """Best-effort client address. X-Forwarded-For is honoured only when MEDIAPULL_TRUST_PROXY is set."""
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def is_local_request(request: Request) -> bool:
    """True only for requests that really come from the machine running the server."""
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        return False
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def request_is_https(request: Request) -> bool:
    if os.getenv("MEDIAPULL_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if request.url.scheme == "https":
        return True
    return TRUST_PROXY and request.headers.get("x-forwarded-proto", "").lower() == "https"


class SlidingWindowLimiter:
    """In-memory sliding-window limiter: at most `max_events` per `window_seconds` per key."""

    def __init__(self, max_events: int, window_seconds: float):
        self.max_events = max_events
        self.window = window_seconds
        self._events: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_gc = time.monotonic()

    def _gc(self, now: float) -> None:
        if now - self._last_gc < self.window:
            return
        self._last_gc = now
        stale = [k for k, q in self._events.items() if not q or now - q[-1] > self.window]
        for k in stale:
            del self._events[k]

    def hit(self, key: str) -> bool:
        """Record an event. Returns False (and records nothing) when the key is over the limit."""
        now = time.monotonic()
        with self._lock:
            self._gc(now)
            q = self._events[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.max_events:
                return False
            q.append(now)
            return True

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            q: Optional[Deque[float]] = self._events.get(key)
            if not q:
                return 0
            return max(1, int(self.window - (now - q[0])) + 1)

    def reset(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)
