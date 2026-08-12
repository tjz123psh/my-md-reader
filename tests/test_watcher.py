from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from mdreader.services.workspace import ScanCoordinator, WorkspaceWatcher


def _file(path: Path) -> Gio.File:
    return Gio.File.new_for_path(str(path))


class WorkspaceWatcherSerialTests(unittest.TestCase):
    """The change serial is the reliable signal for the scan event window.

    The window keeps the old watcher alive while a background scan runs and
    replaces it atomically afterwards; closing the old watcher cancels any
    pending debounce, so a serial mismatch at completion time is what tells
    the window that one more refresh is required.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.markdown = self.root / "doc.md"
        self.markdown.write_text("# Doc\n", encoding="utf-8")
        self.plain = self.root / "notes.txt"
        self.plain.write_text("plain\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_change_serial_counts_only_relevant_events(self) -> None:
        watcher = WorkspaceWatcher(self.root, lambda: None, debounce_ms=5000)
        try:
            self.assertEqual(watcher.change_serial, 0)
            watcher._on_changed(
                None, _file(self.markdown), None, Gio.FileMonitorEvent.CHANGED
            )
            self.assertEqual(watcher.change_serial, 1)
            # A plain-text content change does not affect the Markdown tree.
            watcher._on_changed(
                None, _file(self.plain), None, Gio.FileMonitorEvent.CHANGED
            )
            self.assertEqual(watcher.change_serial, 1)
            watcher._on_changed(
                None, _file(self.markdown), None, Gio.FileMonitorEvent.CREATED
            )
            self.assertEqual(watcher.change_serial, 2)
        finally:
            watcher.close()

    def test_debounced_callback_fires_once_for_a_burst(self) -> None:
        fired: list[int] = []
        watcher = WorkspaceWatcher(self.root, lambda: fired.append(1), debounce_ms=30)
        try:
            for _ in range(3):
                watcher._on_changed(
                    None, _file(self.markdown), None, Gio.FileMonitorEvent.CHANGED
                )
            deadline = time.monotonic() + 1.0
            context = GLib.MainContext.default()
            while not fired and time.monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                time.sleep(0.005)
            self.assertEqual(fired, [1], "a burst must collapse into one callback")
        finally:
            watcher.close()

    def test_monitor_install_failure_is_survived_and_reported(self) -> None:
        def failing_monitor(_file, _flags, _cancellable) -> None:
            raise GLib.Error.new_literal(
                Gio.io_error_quark(), "monitor failed", 0
            )

        with patch.object(Gio.File, "monitor_directory", failing_monitor):
            watcher = WorkspaceWatcher(self.root, lambda: None)
            try:
                self.assertFalse(
                    watcher.has_monitors,
                    "an all-failed install must be visible, not silent",
                )
            finally:
                watcher.close()


class ScanCoordinatorTests(unittest.TestCase):
    """Decide whether a completed scan must schedule one more refresh."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_no_followup_when_nothing_changed_during_scan(self) -> None:
        watcher = WorkspaceWatcher(self.root, lambda: None, debounce_ms=5000)
        try:
            serial = watcher.change_serial
            coordinator = ScanCoordinator(watcher, serial)
            self.assertFalse(coordinator.should_schedule_followup())
        finally:
            watcher.close()

    def test_followup_when_an_event_arrived_during_scan(self) -> None:
        watcher = WorkspaceWatcher(self.root, lambda: None, debounce_ms=5000)
        try:
            serial = watcher.change_serial
            watcher._on_changed(
                None, _file(self.root / "new.md"), None, Gio.FileMonitorEvent.CREATED
            )
            coordinator = ScanCoordinator(watcher, serial)
            self.assertTrue(coordinator.should_schedule_followup())
        finally:
            watcher.close()

    def test_missing_watcher_never_requests_followup(self) -> None:
        coordinator = ScanCoordinator(None, 0)
        self.assertFalse(coordinator.should_schedule_followup())


if __name__ == "__main__":
    unittest.main()
