"""Provider API-key rotation helpers."""

from __future__ import annotations

import threading
from typing import ClassVar

import httpx
import openai

from config.api_key_rotation import ApiKeyRotationMode
from providers.rate_limit import retryable_upstream_status


class ApiKeyRotationPool:
    """Thread-safe API-key selector shared by equivalent provider instances."""

    _shared_pools: ClassVar[dict[tuple[str, str, str], "ApiKeyRotationPool"]] = {}
    _shared_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, raw_value: str, mode: ApiKeyRotationMode | str) -> None:
        self._keys = tuple(
            part.strip() for part in raw_value.split(",") if part.strip()
        )
        if not self._keys:
            raise ValueError("ApiKeyRotationPool requires at least one non-empty key")
        self._mode = ApiKeyRotationMode(mode)
        self._next_index = 0
        self._lock = threading.Lock()

    @classmethod
    def shared(
        cls,
        scope: str,
        raw_value: str,
        mode: ApiKeyRotationMode | str,
    ) -> "ApiKeyRotationPool":
        key = (scope, ApiKeyRotationMode(mode).value, raw_value.strip())
        with cls._shared_lock:
            pool = cls._shared_pools.get(key)
            if pool is None:
                pool = cls(raw_value, mode)
                cls._shared_pools[key] = pool
            return pool

    @classmethod
    def reset_shared(cls) -> None:
        with cls._shared_lock:
            cls._shared_pools.clear()

    @property
    def mode(self) -> ApiKeyRotationMode:
        return self._mode

    @property
    def size(self) -> int:
        return len(self._keys)

    def key_for_new_request(self) -> str:
        """Return the key to use when opening a new upstream request."""
        if self._mode == ApiKeyRotationMode.FAILOVER_ON_LIMIT:
            return self._keys[0]
        with self._lock:
            key = self._keys[self._next_index]
            self._next_index = (self._next_index + 1) % len(self._keys)
            return key

    def next_key_after_limit(self, current_key: str) -> str | None:
        """Return a failover key after ``current_key`` hits a retryable upstream error."""
        try:
            index = self._keys.index(current_key)
        except ValueError:
            return self._keys[0]
        next_index = index + 1
        if next_index >= len(self._keys):
            return None
        return self._keys[next_index]


def is_retryable_upstream_error(error: BaseException) -> bool:
    """Return whether an upstream exception should fail over to another API key."""
    return retryable_upstream_status(error) is not None


def is_limit_error(error: BaseException) -> bool:
    """Backward-compatible alias for retryable key-failover upstream errors."""
    if isinstance(error, openai.RateLimitError):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code == 429
    return False
