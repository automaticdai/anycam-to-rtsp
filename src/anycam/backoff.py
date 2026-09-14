from __future__ import annotations

import math

from .config import BackoffConfig


class Backoff:
    """Exponential backoff with a ceiling, reset on success."""

    def __init__(self, config: BackoffConfig) -> None:
        self._config = config
        self._attempts = 0

        # Compute the maximum exponent to prevent overflow in factor ** attempts.
        # We want: initial_s * (factor ** max_exponent) <= max_s
        # So: factor ** max_exponent <= max_s / initial_s
        # So: max_exponent = ceil(log(max_s / initial_s) / log(factor))
        # Guard degenerate cases: factor <= 1, initial >= max, or initial <= 0
        if config.factor <= 1.0 or config.initial_s >= config.max_s or config.initial_s <= 0:
            self._max_exponent = 0
        else:
            ratio = config.max_s / config.initial_s
            self._max_exponent = math.ceil(math.log(ratio) / math.log(config.factor))

    def next_delay(self) -> float:
        # Use min(attempts, max_exponent) to avoid overflow while still counting attempts
        exponent = min(self._attempts, self._max_exponent)
        delay = self._config.initial_s * (self._config.factor ** exponent)
        self._attempts += 1
        return min(delay, self._config.max_s)

    def reset(self) -> None:
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts
