"""Folder-watch intake.

Deliberately server-side. A browser has no capability to continuously watch an
arbitrary filesystem path — a plain `<input type=file>` reads a file once at
selection time, and the File System Access API (Chrome-only, no continuous
push-notification watching) doesn't provide an always-on background watch either.
The dashboard's "select a folder" control is a text path input; the watching itself
runs here, in the Python process, via `watchdog`.

A watched JSON file's exact shape is not yet specified (no sample exists at the time
this was written) — accepts several reasonable shapes rather than one rigid schema,
and logs what it could not parse instead of crashing the watcher.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from corpus.web.seeds import ResolvedChannel, SeedInputError, append_seed, resolve_input

log = structlog.get_logger(__name__)

WatchEventSink = Callable[[dict[str, Any]], None]


def _extract_candidates(payload: Any) -> list[str]:
    """Accept a few reasonable shapes for 'a list of channels to add':
    a bare list of URLs/handles, or a list of objects with a url/handle/channel key.
    Anything else is reported as unparseable rather than silently ignored.
    """
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("channels"), list):
        items = payload["channels"]
    else:
        return []

    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            candidate = item.get("url") or item.get("handle") or item.get("channel")
            if candidate:
                out.append(str(candidate))
    return out


class _Handler(FileSystemEventHandler):
    def __init__(self, on_event: WatchEventSink) -> None:
        self._on_event = on_event
        self._seen_mtimes: dict[str, float] = {}

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_process(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe_process(event)

    def _maybe_process(self, event: FileSystemEvent) -> None:
        if event.is_directory or not str(event.src_path).endswith(".json"):
            return
        path = Path(str(event.src_path))
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return
        # Both a create and a near-simultaneous modify event commonly fire for the
        # same write; skip a file already processed at this exact mtime.
        if self._seen_mtimes.get(str(path)) == mtime:
            return
        self._seen_mtimes[str(path)] = mtime
        self._process(path)

    def _process(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("watch_file_unparseable", path=str(path), error=str(exc))
            self._on_event({"kind": "watch_error", "path": str(path), "detail": str(exc)})
            return

        candidates = _extract_candidates(payload)
        if not candidates:
            log.warning("watch_file_no_candidates", path=str(path))
            self._on_event(
                {"kind": "watch_error", "path": str(path), "detail": "no recognizable channel list"}
            )
            return

        for raw in candidates:
            self._add_one(raw, path)

    def _add_one(self, raw: str, source_path: Path) -> None:
        try:
            resolved: ResolvedChannel = resolve_input(raw)
            row = append_seed(resolved)
            log.info("watch_added_channel", handle=resolved.handle, source=str(source_path))
            self._on_event({"kind": "watch_added", "path": str(source_path), "row": row})
        except SeedInputError as exc:
            log.info("watch_skip", raw=raw, reason=str(exc))
            self._on_event({"kind": "watch_skipped", "path": str(source_path), "detail": str(exc)})


class FolderWatcher:
    def __init__(self) -> None:
        self._observer: Observer | None = None
        self._watched_path: str | None = None
        self._lock = threading.Lock()

    @property
    def watched_path(self) -> str | None:
        return self._watched_path

    def start(self, path: str, on_event: WatchEventSink) -> None:
        target = Path(path).expanduser().resolve()
        if not target.is_dir():
            raise ValueError(f"{target} is not a directory")

        with self._lock:
            self.stop()
            observer = Observer()
            observer.schedule(_Handler(on_event), str(target), recursive=False)
            observer.start()
            self._observer = observer
            self._watched_path = str(target)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            self._watched_path = None


# One instance for the process, same reasoning as web.runs.manager.
watcher = FolderWatcher()
