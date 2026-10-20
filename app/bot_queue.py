import asyncio
import math
import time
from collections import deque
from typing import Awaitable, Callable, Deque, Optional

PositionCallback = Callable[[int, int], Awaitable[None]]


class JobQueue:
    """FIFO queue with a fixed number of parallel slots that tells waiting users where they are and how long to wait."""

    def __init__(self, concurrency: int, default_job_seconds: float = 45.0, poll_seconds: float = 1.0):
        self.concurrency = max(1, concurrency)
        self.poll_seconds = poll_seconds
        self._waiting: Deque[object] = deque()
        self._running = 0
        self._durations: Deque[float] = deque(maxlen=20)
        self._default = default_job_seconds

    @property
    def running(self) -> int:
        return self._running

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    def average_seconds(self) -> float:
        return sum(self._durations) / len(self._durations) if self._durations else self._default

    def eta_seconds(self, position: int) -> int:
        """Rough time until a job at `position` (1 = next) has been served."""
        return int(self.average_seconds() * math.ceil(position / self.concurrency))

    async def acquire(self, on_position: Optional[PositionCallback] = None) -> float:
        """Wait for a free slot. `on_position(position, eta_seconds)` is awaited whenever the position changes."""
        ticket = object()
        self._waiting.append(ticket)
        try:
            last_position = None
            while True:
                if self._running < self.concurrency and self._waiting[0] is ticket:
                    self._waiting.popleft()
                    self._running += 1
                    return time.monotonic()
                position = list(self._waiting).index(ticket) + 1
                if on_position is not None and position != last_position:
                    last_position = position
                    try:
                        await on_position(position, self.eta_seconds(position))
                    except Exception:
                        pass
                await asyncio.sleep(self.poll_seconds)
        except BaseException:
            if ticket in self._waiting:
                self._waiting.remove(ticket)
            raise

    def release(self, started_at: float) -> None:
        self._running = max(0, self._running - 1)
        self._durations.append(max(0.0, time.monotonic() - started_at))
