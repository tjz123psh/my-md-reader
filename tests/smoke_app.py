from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


APP_ID = "io.github.pang.mdreader"


def app_is_running() -> bool:
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (APP_ID,)),
            GLib.VariantType.new("(b)"),
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
    except GLib.Error:
        return False
    return bool(result.unpack()[0])


def run_case(launcher: Path, fixture: Path, *, opencode_missing: bool) -> bool:
    environment = os.environ.copy()
    environment.update(
        {
            "GSETTINGS_BACKEND": "memory",
            "MDREADER_TEST_QUIT_ON_PRESENT": "1",
            "MDREADER_TEST_SELECT_FIRST": "1",
        }
    )
    if opencode_missing:
        environment["MDREADER_TEST_OPENCODE_MISSING"] = "1"
    else:
        environment.pop("MDREADER_TEST_OPENCODE_MISSING", None)

    label = "without OpenCode" if opencode_missing else "with OpenCode"
    try:
        completed = subprocess.run(
            [str(launcher), str(fixture)],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        print(f"MD Reader smoke {label} timed out", file=sys.stderr)
        if error.stderr:
            print(error.stderr, file=sys.stderr)
        return False
    if completed.returncode != 0:
        print(
            f"MD Reader smoke {label} exited with {completed.returncode}",
            file=sys.stderr,
        )
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        return False
    print(f"MD Reader smoke {label}: document presented")
    return True


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: smoke_app.py LAUNCHER FIXTURE", file=sys.stderr)
        return 2
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") or not any(
        os.environ.get(name) for name in ("WAYLAND_DISPLAY", "DISPLAY", "BROADWAY_DISPLAY")
    ):
        print("MD Reader GTK smoke skipped: no graphical D-Bus session")
        return 77
    if app_is_running():
        print("MD Reader GTK smoke skipped: application is already running")
        return 77

    launcher = Path(sys.argv[1]).resolve()
    fixture = Path(sys.argv[2]).resolve()
    if not run_case(launcher, fixture, opencode_missing=False):
        return 1
    if not run_case(launcher, fixture, opencode_missing=True):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
