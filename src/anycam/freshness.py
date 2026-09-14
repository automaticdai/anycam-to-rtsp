from __future__ import annotations

import threading

from .frame import Frame


class LatestFrameBuffer:
    """Holds at most one frame: the newest one produced.

    Freshness is structural rather than threshold-driven. A producer never
    blocks, and a slow consumer degrades to a lower effective frame rate
    instead of to growing latency, because there is no queue in which a
    backlog could accumulate.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._accepted = 0
        self._dropped = 0

    def put(self, frame: Frame) -> None:
        with self._lock:
            if self._frame is not None:
                self._dropped += 1
            self._frame = frame
            self._accepted += 1

    def get(self) -> Frame | None:
        """Newest frame, left in place."""
        with self._lock:
            return self._frame

    def take(self) -> Frame | None:
        """Newest frame, clearing the slot so it is not counted as dropped."""
        with self._lock:
            frame, self._frame = self._frame, None
            return frame

    @property
    def depth(self) -> int:
        with self._lock:
            return 0 if self._frame is None else 1

    @property
    def accepted(self) -> int:
        with self._lock:
            return self._accepted

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped
