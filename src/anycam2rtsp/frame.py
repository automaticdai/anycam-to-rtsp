from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Frame:
    """One decoded frame plus the identity and timing we can obtain for free.

    `est_capture_ns` is an estimate derived from the sender clock and is
    documented as such in the spec; it is never treated as ground truth.
    """

    camera_id: str
    frame_id: int
    pts: int | None
    recv_ns: int
    decoded_ns: int
    image: Any  # numpy.ndarray, typed loosely to avoid importing numpy here
    est_capture_ns: int | None = None
