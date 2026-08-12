from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Iterable
from urllib.parse import unquote, urlsplit

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdown", ".mkd"})
IGNORED_DIRECTORIES = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__"})


class WorkspaceError(ValueError):
    pass


class LocalDocumentLinkError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LocalDocumentLink:
    path: Path
    fragment: str


def parse_local_document_uri(uri: str) -> LocalDocumentLink:
    """Parse a local Markdown link without accepting remote file authorities."""

    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "file":
        raise LocalDocumentLinkError("链接不是本地文件")
    if parsed.netloc:
        raise LocalDocumentLinkError("不支持带主机名的本地文件链接")
    if parsed.query:
        raise LocalDocumentLinkError("本地文档链接不能包含查询参数")

    raw_path = unquote(parsed.path)
    if not raw_path or not Path(raw_path).is_absolute():
        raise LocalDocumentLinkError("本地文档链接必须使用绝对路径")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw_path):
        raise LocalDocumentLinkError("本地文档路径包含无效字符")

    fragment = unquote(parsed.fragment)
    if len(fragment) > 512 or any(
        ord(character) < 32 or ord(character) == 127 for character in fragment
    ):
        raise LocalDocumentLinkError("文档锚点无效")
    return LocalDocumentLink(Path(raw_path), fragment)


@dataclass(frozen=True, slots=True)
class FileEntry:
    name: str
    relative_path: Path
    is_directory: bool
    children: tuple["FileEntry", ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    relative_path: Path
    identity: tuple[int, int]
    fingerprint: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    entries: tuple[FileEntry, ...]
    directories: tuple[Path, ...]
    documents: tuple[DocumentSnapshot, ...]


class WorkspaceService:
    def __init__(self, root: str | Path) -> None:
        candidate = Path(root).expanduser()
        if not candidate.exists():
            raise WorkspaceError(f"工作区不存在：{candidate}")
        if not candidate.is_dir():
            raise WorkspaceError(f"工作区不是文件夹：{candidate}")
        self.root = candidate.resolve(strict=True)

    def resolve_relative(self, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute():
            raise WorkspaceError("工作区路径必须是相对路径")
        candidate = (self.root / relative).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise WorkspaceError(f"路径超出工作区：{relative}")
        return candidate

    def relative_path(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser().resolve(strict=False)
        try:
            return candidate.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError(f"路径位于工作区之外：{candidate}") from error

    def validate_document(self, path: str | Path) -> Path:
        candidate = self.resolve_relative(path)
        if candidate.suffix.lower() not in MARKDOWN_SUFFIXES:
            raise WorkspaceError(f"不是 Markdown 文档：{path}")
        if not candidate.is_file():
            raise WorkspaceError(f"文档不存在：{path}")
        return candidate

    def snapshot_document(self, path: str | Path) -> DocumentSnapshot:
        candidate = self.validate_document(path)
        try:
            status = candidate.stat()
        except OSError as error:
            raise WorkspaceError(f"无法读取文档状态：{path}：{error}") from error
        return self._document_snapshot(candidate, status)

    def scan(self) -> tuple[FileEntry, ...]:
        return self.scan_snapshot().entries

    def scan_snapshot(self) -> WorkspaceSnapshot:
        directories: list[Path] = []
        documents: list[DocumentSnapshot] = []
        entries = self._scan_directory(
            self.root,
            required=True,
            directories=directories,
            documents=documents,
        )
        return WorkspaceSnapshot(
            entries=entries,
            directories=tuple(directories),
            documents=tuple(documents),
        )

    def _scan_directory(
        self,
        directory: Path,
        *,
        required: bool = False,
        directories: list[Path] | None = None,
        documents: list[DocumentSnapshot] | None = None,
    ) -> tuple[FileEntry, ...]:
        entries: list[FileEntry] = []
        try:
            children: Iterable[Path] = sorted(
                directory.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.casefold()),
            )
        except OSError as error:
            if required:
                raise WorkspaceError(f"无法读取 {directory}：{error}") from error
            # A single unreadable descendant must not make an otherwise valid
            # document or workspace unusable. Omit that branch from the tree;
            # the canonical root itself remains a hard failure above.
            return ()

        if directories is not None:
            directories.append(directory)

        for child in children:
            if child.name.startswith(".") or child.name in IGNORED_DIRECTORIES:
                continue
            if child.is_symlink():
                # Symlink traversal is deliberately excluded from the first version.
                continue
            if child.is_dir():
                nested = self._scan_directory(
                    child,
                    directories=directories,
                    documents=documents,
                )
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
                if documents is not None:
                    try:
                        status = child.stat()
                    except OSError:
                        continue
                    documents.append(self._document_snapshot(child, status))
                entries.append(
                    FileEntry(
                        name=child.name,
                        relative_path=child.relative_to(self.root),
                        is_directory=False,
                    )
                )
        return tuple(entries)

    def _document_snapshot(self, path: Path, status: object) -> DocumentSnapshot:
        device = int(getattr(status, "st_dev"))
        inode = int(getattr(status, "st_ino"))
        return DocumentSnapshot(
            relative_path=path.relative_to(self.root),
            identity=(device, inode),
            fingerprint=(
                device,
                inode,
                int(getattr(status, "st_size")),
                int(getattr(status, "st_mtime_ns")),
            ),
        )


class ScanCoordinator:
    """Track whether a completed scan must schedule one more refresh.

    The window keeps the previous watcher alive while a background scan runs
    so relevant events are still counted. Replacing the watcher cancels any
    pending debounce on the old one; a serial mismatch at completion time is
    therefore the reliable signal that an event may have been observed but
    not yet turned into a refresh.
    """

    def __init__(self, watcher: WorkspaceWatcher | None, serial_at_start: int) -> None:
        self._watcher = watcher
        self._serial_at_start = serial_at_start

    def should_schedule_followup(self) -> bool:
        return (
            self._watcher is not None
            and self._watcher.change_serial != self._serial_at_start
        )


class WorkspaceWatcher:
    """Watch Markdown-relevant workspace changes and debounce one callback."""

    def __init__(
        self,
        root: Path,
        callback: Callable[[], None],
        debounce_ms: int = 300,
        *,
        directories: Iterable[Path] | None = None,
    ) -> None:
        self.root = root
        self.callback = callback
        self.debounce_ms = debounce_ms
        self._monitors: list[Gio.FileMonitor] = []
        self._timeout_id = 0
        self._closed = False
        self._change_serial = 0
        self._install_monitors(directories)

    def close(self) -> None:
        self._closed = True
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0
        for monitor in self._monitors:
            monitor.cancel()
        self._monitors.clear()

    @property
    def change_serial(self) -> int:
        """Count of relevant events observed since this watcher was created.

        A scan that captures this value at start can tell afterwards whether
        the workspace changed while the scan was running, even when replacing
        the watcher cancelled the pending debounce for the last event.
        """
        return self._change_serial

    @property
    def has_monitors(self) -> bool:
        """True when at least one directory monitor was installed successfully."""
        return bool(self._monitors)

    def _install_monitors(self, directories: Iterable[Path] | None) -> None:
        if directories is None:
            monitored_directories = (self.root,)
        else:
            monitored_directories = tuple(dict.fromkeys(directories)) or (self.root,)

        for directory in monitored_directories:
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
        event: Gio.FileMonitorEvent,
    ) -> None:
        if self._closed:
            return
        paths = (file.get_path(), other_file.get_path() if other_file else None)
        structural_events = {
            value
            for name in (
                "CREATED",
                "DELETED",
                "MOVED",
                "MOVED_IN",
                "MOVED_OUT",
                "RENAMED",
            )
            if (value := getattr(Gio.FileMonitorEvent, name, None)) is not None
        }
        relevant = event in structural_events or any(
            raw_path and Path(raw_path).suffix.lower() in MARKDOWN_SUFFIXES
            for raw_path in paths
        )
        if not relevant:
            return
        self._change_serial += 1
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
        self._timeout_id = GLib.timeout_add(self.debounce_ms, self._emit_change)

    def _emit_change(self) -> bool:
        self._timeout_id = 0
        if not self._closed:
            self.callback()
        return GLib.SOURCE_REMOVE
