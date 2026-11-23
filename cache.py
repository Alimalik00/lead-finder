"""A tiny on-disk JSON cache.

Places Details and PageSpeed Insights are the two expensive calls in this
pipeline -- Details is billed per request, and PSI takes 10-30 seconds per URL.
Caching their responses makes iterating on scoring or export logic essentially
free, and stops a re-run from re-billing the same 60 places.

The cache is deliberately dumb: one JSON file per key, named by a hash of the
namespace plus key. That makes it trivially inspectable, and safe to delete at
any time.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from logging_setup import get_logger

logger = get_logger("cache")


class JsonCache:
    """A namespaced, TTL'd JSON cache backed by files on disk.

    Args:
        directory: Root cache directory. Created on demand.
        ttl_hours: Entries older than this are treated as misses.
        enabled: When ``False`` every method becomes a no-op, so callers do not
            need to branch on whether caching is on.
    """

    def __init__(
        self,
        directory: Path,
        ttl_hours: float = 168.0,
        enabled: bool = True,
    ) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = max(ttl_hours, 0.0) * 3600.0
        self.enabled = enabled
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

        if self.enabled:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover - read-only filesystem
                logger.warning("Cache disabled: cannot create %s (%s)", self.directory, exc)
                self.enabled = False

    # -- internals ----------------------------------------------------------

    def _path_for(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha1(f"{namespace}::{key}".encode("utf-8")).hexdigest()
        return self.directory / namespace / f"{digest}.json"

    # -- public API ---------------------------------------------------------

    def get(self, namespace: str, key: str) -> Optional[Any]:
        """Return the cached payload for *key*, or ``None`` on a miss."""
        if not self.enabled:
            return None

        path = self._path_for(namespace, key)
        if not path.exists():
            with self._lock:
                self.misses += 1
            return None

        try:
            with path.open("r", encoding="utf-8") as handle:
                envelope = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.debug("Discarding unreadable cache entry %s (%s)", path, exc)
            self._safe_unlink(path)
            with self._lock:
                self.misses += 1
            return None

        stored_at = float(envelope.get("stored_at", 0.0))
        if self.ttl_seconds and (time.time() - stored_at) > self.ttl_seconds:
            logger.debug("Cache entry expired: %s/%s", namespace, key[:60])
            self._safe_unlink(path)
            with self._lock:
                self.misses += 1
            return None

        with self._lock:
            self.hits += 1
        return envelope.get("payload")

    def set(self, namespace: str, key: str, payload: Any) -> None:
        """Store *payload* under *key*. Failures are logged, never raised."""
        if not self.enabled:
            return

        path = self._path_for(namespace, key)
        envelope = {"stored_at": time.time(), "key": key, "payload": payload}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file then rename, so a crash mid-write cannot
            # leave a truncated entry behind.
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(envelope, handle)
            tmp.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            logger.debug("Could not write cache entry %s (%s)", path, exc)

    def clear(self, namespace: Optional[str] = None) -> int:
        """Delete cached entries. Returns the number of files removed."""
        if not self.directory.exists():
            return 0
        root = self.directory / namespace if namespace else self.directory
        if not root.exists():
            return 0
        removed = 0
        for path in root.rglob("*.json"):
            if self._safe_unlink(path):
                removed += 1
        return removed

    @staticmethod
    def _safe_unlink(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError:
            return False

    def stats(self) -> str:
        """Human-readable hit/miss summary for the end-of-run log line."""
        total = self.hits + self.misses
        if not self.enabled:
            return "cache disabled"
        if total == 0:
            return "cache: no lookups"
        rate = 100.0 * self.hits / total
        return f"cache: {self.hits} hits / {self.misses} misses ({rate:.0f}% hit rate)"
