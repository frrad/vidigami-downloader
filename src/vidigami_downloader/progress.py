"""Count-only sync progress and periodic CLI heartbeats on stderr."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import typer


@dataclass(frozen=True)
class ProgressEvent:
    stage: Literal[
        "Starting sync",
        "Discovering pages",
        "Discovering albums",
        "Discovering tagged media",
        "Checking metadata",
        "Fetching download links",
        "Downloading files",
    ]
    completed: int = 0
    total: int = 0
    failed: int = 0
    reused: int = 0


ProgressCallback = Callable[[ProgressEvent], None]


class ProgressReporter:
    """Keep slow requests visibly alive without flooding logs or changing stdout."""

    def __init__(self, interval: float = 10.0) -> None:
        self.interval = interval
        self._event = ProgressEvent("Starting sync")
        self._started = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)

    def __enter__(self) -> ProgressReporter:
        with self._lock:
            self._write()
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join()

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            previous = self._event
            self._event = event
            if event.stage != previous.stage or event.completed == event.total:
                self._write()

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                self._write()

    def _write(self) -> None:
        event = self._event
        counts = f" {event.completed}/{event.total}" if event.total else ""
        details = (
            f" ({event.reused} reused, {event.failed} failed)"
            if event.stage == "Downloading files"
            else f" ({event.failed} failed)"
            if event.failed
            else ""
        )
        elapsed = int(time.monotonic() - self._started)
        typer.echo(f"[{elapsed}s] {event.stage}{counts}{details}", err=True)
