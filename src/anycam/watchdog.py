from __future__ import annotations

import time
from collections.abc import Callable


class Watchdog:
    """Declares failure when frames stop arriving, regardless of socket state.

    This is not redundant with reconnect logic. A wedged USB camera holds a
    healthy TCP connection indefinitely, so nothing raises and nothing closes;
    the only observable symptom is that frames stopped.
    """

    def __init__(self, timeout_s: float,
                 clock: Callable[[], int] = time.monotonic_ns) -> None:
        self._timeout_ns = int(timeout_s * 1e9)
        self._clock = clock
        self._last_ns = clock()

    def beat(self) -> None:
        self._last_ns = self._clock()

    def expired(self) -> bool:
        return (self._clock() - self._last_ns) > self._timeout_ns

    def age_s(self) -> float:
        return (self._clock() - self._last_ns) / 1e9
