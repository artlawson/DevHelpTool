import time
from collections.abc import Callable
from typing import Any


class TTLCache:
    def __init__(
        self, ttl_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic
    ):
        self._ttl = ttl_seconds
        self._clock = clock
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (self._clock() + self._ttl, value)
