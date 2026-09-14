from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StreamStats:
    """Per-camera health. Observability is load-bearing here, not a nicety:
    every failure mode in this system is silent."""

    camera_id: str
    frames: int
    dropped: int
    reconnects: int
    age_s: float
    depth: int
    last_error: str | None

    def is_stalled(self, timeout_s: float) -> bool:
        return self.age_s > timeout_s

    def format_line(self, timeout_s: float = 2.0) -> str:
        state = "STALLED" if self.is_stalled(timeout_s) else "ok"
        line = (f"{self.camera_id:<8} {state:<8} "
                f"frames={self.frames:<8} dropped={self.dropped:<8} "
                f"reconnects={self.reconnects:<4} age={self.age_s:6.3f}s "
                f"depth={self.depth}")
        if self.last_error is not None:
            line += f" error={self.last_error}"
        return line
