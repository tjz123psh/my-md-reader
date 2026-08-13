from __future__ import annotations

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

# Non-secret AI profile metadata allowlist (spec §9.1). No API-key field can
# ever be persisted here; secrets live in Secret Service only.
AI_PROFILE_FIELDS = frozenset(
    {
        "profile-id",
        "provider-kind",
        "api-base-url",
        "models-url",
        "model-id",
        "auth-mode",
    }
)


class SettingsStore:
    """Small typed facade that also tolerates an uninstalled dev schema."""

    DEFAULTS: dict[str, object] = {
        "window-width": 1200,
        "window-height": 800,
        "window-maximized": False,
        "last-workspace": "",
        "last-document": "",
        "document-zoom": 100,
        "color-scheme": "warm-paper",
        "opencode-model": "",
        "library-sidebar-width": 260,
        "ai-sidebar-width": 360,
        "ai-profile": {},
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

    def get_sidebar_width(self, key: str, default: int) -> int:
        try:
            value = self.get_int(key)
        except (KeyError, TypeError, ValueError):
            return default
        return max(180, min(720, value))

    def set_sidebar_width(self, key: str, value: int) -> None:
        self.set_int(key, max(180, min(720, int(value))))

    def _ai_profile_supported(self) -> bool:
        """True when the active schema knows the ``ai-profile`` key.

        A real GSettings object backed by an older installed schema (before
        the direct-LLM migration) does not contain the key; calling
        get/set_value on it aborts the process with a fatal GLib error, so the
        facade must probe with ``has_key`` first and degrade honestly. Test
        fakes that mimic Gio.Settings without a real ``props.settings_schema``
        take the lenient path.
        """
        if not self._settings:
            return True  # memory fallback always supports it
        schema = getattr(
            getattr(self._settings, "props", None), "settings_schema", None
        )
        if schema is None:
            return True
        return schema.has_key("ai-profile")

    def get_ai_profile(self) -> dict[str, str] | None:
        """AI 连接的非秘密元数据；空/未设置为 None（spec §9.1）。"""
        if self._ai_profile_supported():
            if self._settings:
                profile = self._settings.get_value("ai-profile").unpack()
            else:
                profile = dict(self._memory.get("ai-profile", {}))
            return profile if profile else None
        return None

    def set_ai_profile(self, values: dict[str, str]) -> bool:
        """单次写入完整 ai-profile，过滤到允许字段；返回底层写入结果。"""
        profile = {
            key: value
            for key, value in values.items()
            if key in AI_PROFILE_FIELDS and isinstance(value, str)
        }
        if not self._ai_profile_supported():
            return False
        if self._settings:
            return self._settings.set_value(
                "ai-profile", GLib.Variant("a{ss}", profile)
            )
        self._memory["ai-profile"] = profile
        return True

    def clear_ai_profile(self) -> bool:
        """清除 ai-profile，回到未配置状态；返回是否成功。"""
        if not self._ai_profile_supported():
            return True  # nothing was ever persisted
        if self._settings:
            return self._settings.set_value(
                "ai-profile", GLib.Variant("a{ss}", {})
            )
        self._memory["ai-profile"] = {}
        return True
