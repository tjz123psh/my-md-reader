from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Iterable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdown", ".mkd"})
IGNORED_DIRECTORIES = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__"})


class WorkspaceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FileEntry:
    name: str
    relative_path: Path
    is_directory: bool
    children: tuple["FileEntry", ...] = ()


class WorkspaceService:
    def __init__(self, root: str | Path) -> None:
        candidate = Path(root).expanduser()
        if not candidate.exists():
            raise WorkspaceError(f"Workspace does not exist: {candidate}")
        if not candidate.is_dir():
            raise WorkspaceError(f"Workspace is not a directory: {candidate}")
        self.root = candidate.resolve(strict=True)

    def resolve_relative(self, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute():
            raise WorkspaceError("Workspace paths must be relative")
        candidate = (self.root / relative).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise WorkspaceError(f"Path escapes workspace: {relative}")
        return candidate

    def relative_path(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser().resolve(strict=False)
        try:
            return candidate.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError(f"Path is outside workspace: {candidate}") from error

    def validate_document(self, path: str | Path) -> Path:
        candidate = self.resolve_relative(path)
        if candidate.suffix.lower() not in MARKDOWN_SUFFIXES:
            raise WorkspaceError(f"Not a Markdown document: {path}")
        if not candidate.is_file():
            raise WorkspaceError(f"Document does not exist: {path}")
        return candidate

    def scan(self) -> tuple[FileEntry, ...]:
        return self._scan_directory(self.root)

    def _scan_directory(self, directory: Path) -> tuple[FileEntry, ...]:
        entries: list[FileEntry] = []
        try:
            children: Iterable[Path] = sorted(
                directory.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.casefold()),
            )
        except OSError as error:
            raise WorkspaceError(f"Could not read {directory}: {error}") from error

        for child in children:
            if child.name.startswith(".") or child.name in IGNORED_DIRECTORIES:
                continue
            if child.is_symlink():
                # Symlink traversal is deliberately excluded from the first version.
                continue
            if child.is_dir():
                nested = self._scan_directory(child)
                if nested:
                    entries.append(
                        FileEntry(
                            name=child.name,
                            relative_path=child.relative_to(self.root),
                            is_directory=True,
                            children=nested,
                        )
                    )
            elif child.suffix.lower() in MARKDOWN_SUFFIXES:
                entries.append(
                    FileEntry(
                        name=child.name,
                        relative_path=child.relative_to(self.root),
                        is_directory=False,
                    )
                )
        return tuple(entries)


class WorkspaceWatcher:
    """Watch Markdown-relevant workspace changes and debounce one callback."""

    def __init__(self, root: Path, callback: Callable[[], None], debounce_ms: int = 300) -> None:
        self.root = root
        self.callback = callback
        self.debounce_ms = debounce_ms
        self._monitors: list[Gio.FileMonitor] = []
        self._timeout_id = 0
        self._closed = False
        self._install_monitors()

    def close(self) -> None:
        self._closed = True
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0
        for monitor in self._monitors:
            monitor.cancel()
        self._monitors.clear()

    def _install_monitors(self) -> None:
        directories = [self.root]
        try:
            directories.extend(
                path
                for path in self.root.rglob("*")
                if path.is_dir()
                and not path.is_symlink()
                and not any(part.startswith(".") or part in IGNORED_DIRECTORIES for part in path.parts[len(self.root.parts) :])
            )
        except OSError:
            pass

        for directory in directories:
            try:
                monitor = Gio.File.new_for_path(str(directory)).monitor_directory(
                    Gio.FileMonitorFlags.WATCH_MOVES, None
                )
            except GLib.Error:
                continue
            monitor.connect("changed", self._on_changed)
            self._monitors.append(monitor)

    def _on_changed(
        self,
        _monitor: Gio.FileMonitor,
        file: Gio.File,
        other_file: Gio.File | None,
        _event: Gio.FileMonitorEvent,
    ) -> None:
        if self._closed:
            return
        paths = [file.get_path(), other_file.get_path() if other_file else None]
        relevant = False
        for raw_path in paths:
            if not raw_path:
                continue
            path = Path(raw_path)
            if path.suffix.lower() in MARKDOWN_SUFFIXES or not path.suffix or path.is_dir():
                relevant = True
                break
        if not relevant:
            return
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
        self._timeout_id = GLib.timeout_add(self.debounce_ms, self._emit_change)

    def _emit_change(self) -> bool:
        self._timeout_id = 0
        if not self._closed:
            self.callback()
        return GLib.SOURCE_REMOVE
