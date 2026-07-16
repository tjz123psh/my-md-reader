from __future__ import annotations

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio


class SettingsStore:
    """Small typed facade that also tolerates an uninstalled dev schema."""

    DEFAULTS: dict[str, object] = {
        "window-width": 1200,
        "window-height": 800,
        "window-maximized": False,
        "last-workspace": "",
        "last-document": "",
        "document-zoom": 100,
        "color-scheme": "system",
        "opencode-model": "",
    }

    def __init__(self) -> None:
        source = Gio.SettingsSchemaSource.get_default()
        schema = source.lookup("io.github.pang.mdreader", True) if source else None
        self._settings = Gio.Settings.new_full(schema, None, None) if schema else None
        self._memory = dict(self.DEFAULTS)

    def get_int(self, key: str) -> int:
        if self._settings:
            return self._settings.get_int(key)
        return int(self._memory[key])

    def set_int(self, key: str, value: int) -> None:
        if self._settings:
            self._settings.set_int(key, value)
        else:
            self._memory[key] = value

    def get_boolean(self, key: str) -> bool:
        if self._settings:
            return self._settings.get_boolean(key)
        return bool(self._memory[key])

    def set_boolean(self, key: str, value: bool) -> None:
        if self._settings:
            self._settings.set_boolean(key, value)
        else:
            self._memory[key] = value

    def get_string(self, key: str) -> str:
        if self._settings:
            return self._settings.get_string(key)
        return str(self._memory[key])

    def set_string(self, key: str, value: str) -> None:
        if self._settings:
            self._settings.set_string(key, value)
        else:
            self._memory[key] = value
