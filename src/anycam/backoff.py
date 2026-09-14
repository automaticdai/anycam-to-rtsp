from __future__ import annotations

from .config import BackoffConfig


class Backoff:
    """Exponential backoff with a ceiling, reset on success."""

    def __init__(self, config: BackoffConfig) -> None:
        self._config = config
        self._attempts = 0

    def next_delay(self) -> float:
        delay = self._config.initial_s * (self._config.factor ** self._attempts)
        self._attempts += 1
        return min(delay, self._config.max_s)

    def reset(self) -> None:
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts
