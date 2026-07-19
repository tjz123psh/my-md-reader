from __future__ import annotations

import unittest

from mdreader.services.settings import SettingsStore


class SettingsStoreTests(unittest.TestCase):
    def test_sidebar_widths_are_clamped(self) -> None:
        settings = SettingsStore()
        # Unit tests must not write the developer's real dconf database when
        # the build-tree schema is available in the test environment.
        settings._settings = None
        settings.set_sidebar_width("library-sidebar-width", 120)
        self.assertEqual(settings.get_sidebar_width("library-sidebar-width", 260), 180)
        settings.set_sidebar_width("ai-sidebar-width", 999)
        self.assertEqual(settings.get_sidebar_width("ai-sidebar-width", 360), 720)


if __name__ == "__main__":
    unittest.main()
