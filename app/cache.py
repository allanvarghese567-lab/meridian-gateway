"""Thread-safe in-process LRU cache with TTL.

Cache keys are SHA-256 digests of *scrubbed* inputs, so raw PII never
becomes a string stored in the process.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from app.security import deep_scrub


def build_cache_key(tool_name: str, arguments: Any) -> str:
    """Digest a tool call after redaction. The returned key contains no PII."""
    sanitized = deep_scrub({"tool": tool_name, "arguments": arguments})
    blob = json.dumps(sanitized, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class GatewayLRUCache:
    """Bounded LRU. `max_size` counts entries; expired entries count until touched."""

    def __init__(
        self,
        max_size: int = 10_000,
        default_ttl_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be > 0")
        self.max_size = max_size
        self.default_ttl_seconds = default_ttl_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            value, expires_at = item
            if self._clock() >= expires_at:
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return copy.deepcopy(value)

    def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise ValueError("ttl_seconds must be > 0")
        stored = copy.deepcopy(value)
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (stored, self._clock() + ttl)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def snapshot_counters(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._data), "hits": self.hits, "misses": self.misses}
