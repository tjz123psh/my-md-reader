from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GObject

if TYPE_CHECKING:
    from mdreader.services.workspace import FileEntry


class FileNode(GObject.Object):
    def __init__(self, entry: FileEntry) -> None:
        super().__init__()
        self.entry = entry
        self._children_model: Gio.ListStore | None = None

    @GObject.Property(type=str, flags=GObject.ParamFlags.READABLE)
    def name(self) -> str:
        return self.entry.name

    @GObject.Property(type=str, flags=GObject.ParamFlags.READABLE)
    def relative_path(self) -> str:
        return str(self.entry.relative_path)

    @GObject.Property(type=bool, default=False, flags=GObject.ParamFlags.READABLE)
    def is_directory(self) -> bool:
        return self.entry.is_directory

    @property
    def path(self) -> Path:
        return self.entry.relative_path

    def child_model(self) -> Gio.ListModel | None:
        if not self.entry.is_directory:
            return None
        if self._children_model is None:
            model = Gio.ListStore.new(FileNode)
            for child in self.entry.children:
                model.append(FileNode(child))
            self._children_model = model
        return self._children_model


def file_node_store(entries: tuple[FileEntry, ...]) -> Gio.ListStore:
    store = Gio.ListStore.new(FileNode)
    for entry in entries:
        store.append(FileNode(entry))
    return store
