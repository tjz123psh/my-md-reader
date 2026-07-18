from __future__ import annotations

import os
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from mdreader import __version__
from mdreader.services import build_gtk_theme_css
from mdreader.services.settings import SettingsStore
from mdreader.window import MdReaderWindow


class MdReaderApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="io.github.pang.mdreader",
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.settings = SettingsStore()

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self._load_css()
        self._setup_actions()
        self._setup_accelerators()

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = MdReaderWindow(application=self, settings=self.settings)
        window.present()
        self._schedule_smoke_exit()

    def do_open(self, files: list[Gio.File], _count: int, _hint: str) -> None:
        initial_path = Path(files[0].get_path()) if files and files[0].get_path() else None
        window = self.props.active_window
        if window is None:
            window = MdReaderWindow(
                application=self,
                settings=self.settings,
                initial_path=initial_path,
            )
        elif initial_path:
            window.open_path(initial_path)
        window.present()
        self._schedule_smoke_exit()

    def _schedule_smoke_exit(self) -> None:
        raw_timeout = os.environ.get("MDREADER_TEST_QUIT_MS", "")
        if not raw_timeout:
            return
        try:
            timeout = max(100, int(raw_timeout))
        except ValueError:
            return
        GLib.timeout_add(timeout, self._quit_smoke_test)

    def _quit_smoke_test(self) -> bool:
        self.quit()
        return GLib.SOURCE_REMOVE

    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_resource("/io/github/pang/mdreader/style.css")
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        theme_provider = Gtk.CssProvider()
        theme_provider.load_from_string(build_gtk_theme_css())
        Gtk.StyleContext.add_provider_for_display(
            display,
            theme_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
        )

    def _setup_actions(self) -> None:
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about)
        self.add_action(about)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)

    def _setup_accelerators(self) -> None:
        accelerators = {
            "app.quit": ["<Control>q"],
            "win.open-document": ["<Control>o"],
            "win.open-folder": ["<Control><Shift>o"],
            "win.find": ["<Control>f"],
            "win.toggle-ai": ["<Control><Shift>a"],
            "win.undo-ai-change": ["<Control>z"],
        }
        for action, keys in accelerators.items():
            self.set_accels_for_action(action, keys)

    def _on_about(self, _action: Gio.SimpleAction, _parameter: object) -> None:
        dialog = Adw.AboutDialog(
            application_name="MD Reader",
            application_icon="io.github.pang.mdreader",
            version=__version__,
            developer_name="MD Reader contributors",
            license_type=Gtk.License.GPL_3_0,
            comments="A focused, read-only Markdown workspace for Linux",
            website="https://github.com/tjz123psh/my-md-reader",
            issue_url="https://github.com/tjz123psh/my-md-reader/issues",
        )
        dialog.present(self.props.active_window)
