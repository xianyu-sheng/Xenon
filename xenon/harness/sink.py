"""JSONL event sink for the headless harness."""

from __future__ import annotations

import sys
from typing import TextIO

from xenon.ledger.events import Event, canonical_json


class JsonlSink:
    """Write events to a stream (stdout or a file), one JSON object per line."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def emit(self, event: Event) -> None:
        self._stream.write(canonical_json(event.to_dict()))
        self._stream.write("\n")
        self._stream.flush()


__all__ = ["JsonlSink"]
