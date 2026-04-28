"""Default-thread resolver: maps (agent, user_id) → session_id.

Phase 1 keeps a single ongoing thread per (agent, user_id). Persisted as
agents/<agent>/threads.json so the same thread continues across harness
restarts. Schema is forward-compatible with multi-thread support.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path


class Threads:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, str] = {}
        if path.is_file():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._data = {}

    def get_or_create(self, user_id: str) -> tuple[str, bool]:
        existing = self._data.get(user_id)
        if existing:
            return existing, False
        new_id = str(uuid.uuid4())
        self._data[user_id] = new_id
        self._save()
        return new_id, True

    def get(self, user_id: str) -> str | None:
        return self._data.get(user_id)

    def rotate(self, user_id: str) -> str:
        """Mint a fresh session_id and set it as default for ``user_id``."""
        new_id = str(uuid.uuid4())
        self._data[user_id] = new_id
        self._save()
        return new_id

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
