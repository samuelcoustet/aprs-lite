"""
Filtre anti-doublons packets APRS — adapté de eusef/eusef-aprs-tui (MIT).
Supprime les packets identiques reçus dans une fenêtre glissante.
"""
from __future__ import annotations

import hashlib
import time


class DeduplicationFilter:
    """Supprime les duplicats dans une fenêtre temporelle configurable."""

    def __init__(self, window: float = 30.0) -> None:
        self._window = window
        self._seen: dict[str, float] = {}   # hash → timestamp monotonic

    def is_duplicate(self, raw: str) -> bool:
        """Retourne True si ce paquet est un doublon à ignorer."""
        key = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()
        now = time.monotonic()
        if len(self._seen) > 1000:
            self._cleanup(now)
        if key in self._seen and now - self._seen[key] < self._window:
            return True
        self._seen[key] = now
        return False

    def _cleanup(self, now: float) -> None:
        expired = [k for k, t in self._seen.items() if now - t >= self._window]
        for k in expired:
            del self._seen[k]

    def reset(self) -> None:
        self._seen.clear()
