"""Channel interface — adapters that submit Envelopes and consume reply topics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from matrix.core.harness import Harness


class Channel(Protocol):
    name: str

    async def start(self, harness: "Harness") -> None: ...
    async def stop(self) -> None: ...
