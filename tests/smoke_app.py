from __future__ import annotations

import os
import select
import shutil
import subprocess
import tempfile
import sys
import time
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


def run_case(
    launcher: Path,
    fixture: Path,
    *,
    selection_scroll: bool = False,
) -> bool:
    environment = os.environ.copy()
    environment.update(
        {
            "GSETTINGS_BACKEND": "memory",
            "MDREADER_TEST_QUIT_ON_PRESENT": "1",
            "MDREADER_TEST_SELECT_FIRST": "1",
            "MDREADER_TEST_EXPECT_DOCUMENT": str(fixture),
        }
    )
    if selection_scroll:
        environment["MDREADER_TEST_SELECTION_SCROLL"] = "1"
        environment.pop("MDREADER_TEST_CTRL_WHEEL", None)
    else:
        environment["MDREADER_TEST_CTRL_WHEEL"] = "1"
        environment.pop("MDREADER_TEST_SELECTION_SCROLL", None)

    label = "selection-scroll" if selection_scroll else "default"
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
    if "MDREADER_TEST_DOCUMENT_OK=" not in completed.stdout:
        print(
            f"MD Reader smoke {label} did not open the requested document",
            file=sys.stderr,
        )
        if completed.stdout:
            print(completed.stdout, file=sys.stderr)
        return False
    if selection_scroll:
        if "MDREADER_TEST_SELECTION_SCROLL_OK=" not in completed.stdout:
            print(
                f"MD Reader smoke {label} kept scrolling after pointer selection",
                file=sys.stderr,
            )
            if completed.stdout:
                print(completed.stdout, file=sys.stderr)
            return False
    elif "MDREADER_TEST_CTRL_WHEEL_OK=105" not in completed.stdout:
        print(
            f"MD Reader smoke {label} did not receive Ctrl+wheel zoom",
            file=sys.stderr,
        )
        if completed.stdout:
            print(completed.stdout, file=sys.stderr)
        return False
    print(f"MD Reader smoke {label}: document presented")
    return True


def run_restore_case(launcher: Path, root: Path) -> bool:
    workspace = root / "restore-workspace"
    nested = workspace / "docs" / "nested.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("# Restored nested document\n", encoding="utf-8")
    config_home = root / "restore-config"
    config_home.mkdir()

    environment = os.environ.copy()
    environment.update(
        {
            "GSETTINGS_BACKEND": "keyfile",
            "XDG_CONFIG_HOME": str(config_home),
            "MDREADER_TEST_QUIT_ON_PRESENT": "1",
        }
    )
    try:
        first = subprocess.run(
            [str(launcher), str(workspace)],
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
    except subprocess.TimeoutExpired:
        print("MD Reader restore smoke setup timed out", file=sys.stderr)
        return False
    if first.returncode != 0:
        print(
            f"MD Reader restore smoke setup exited with {first.returncode}",
            file=sys.stderr,
        )
        if first.stderr:
            print(first.stderr, file=sys.stderr)
        return False

    environment["MDREADER_TEST_EXPECT_DOCUMENT"] = str(nested)
    try:
        restored = subprocess.run(
            [str(launcher)],
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
    except subprocess.TimeoutExpired:
        print("MD Reader restore smoke timed out", file=sys.stderr)
        return False
    if restored.returncode != 0:
        print(
            f"MD Reader restore smoke exited with {restored.returncode}",
            file=sys.stderr,
        )
        if restored.stderr:
            print(restored.stderr, file=sys.stderr)
        return False
    if "MDREADER_TEST_DOCUMENT_OK=" not in restored.stdout:
        print("MD Reader restore smoke did not reopen the nested document", file=sys.stderr)
        if restored.stdout:
            print(restored.stdout, file=sys.stderr)
        return False

    settings_file = config_home / "glib-2.0" / "settings" / "keyfile"
    try:
        stored = settings_file.read_text(encoding="utf-8")
    except OSError as error:
        print(f"MD Reader restore smoke could not read settings: {error}", file=sys.stderr)
        return False
    if f"last-workspace='{workspace}'" not in stored:
        print(
            "MD Reader restore smoke changed the workspace to the document parent",
            file=sys.stderr,
        )
        return False
    if "last-document='docs/nested.md'" not in stored:
        print("MD Reader restore smoke lost the relative document path", file=sys.stderr)
        return False
    print("MD Reader restore smoke: nested document and workspace restored")
    return True


class MarkerReader:
    """Read MDREADER_TEST_* markers from a live application process."""

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process

    def wait_for(self, marker: str, timeout: float = 15.0) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.process.stdout], [], [], 0.2)
            if ready:
                line = self.process.stdout.readline()
                if not line:
                    return None
                if marker in line:
                    return line.strip()
        return None

    def wait_quiet(self, marker: str, window: float = 2.0) -> bool:
        """Return True when no line containing marker arrives within window."""
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.process.stdout], [], [], 0.2)
            if ready:
                line = self.process.stdout.readline()
                if not line:
                    return True
                if marker in line:
                    return False
        return True

    def collect(self, timeout: float = 20.0) -> list[str]:
        """Read every marker line until the process exits or the timeout."""
        lines: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.process.stdout], [], [], 0.2)
            if ready:
                line = self.process.stdout.readline()
                if not line:
                    break
                lines.append(line.strip())
            elif self.process.poll() is not None:
                break
        return lines


def _long_document() -> str:
    paragraph = (
        "这是一个用于真实应用验收的段落，包含足够长的中文和英文内容，"
        "确保每个标题之间都有充足的滚动距离，以便验证文件变化后的阅读位置恢复。"
        "This paragraph repeats to give every heading a comfortable scroll "
        "distance from the previous one, so a restore to the active heading "
        "is observable through the heading report. "
    )
    body: list[str] = ["# One", ""]
    for _ in range(6):
        body.extend((paragraph, ""))
    body.extend(("## Two", ""))
    for _ in range(6):
        body.extend((paragraph, ""))
    body.extend(("## Three", ""))
    for _ in range(8):
        body.extend((paragraph, ""))
    return "\n".join(body)


def _fragment_document() -> str:
    paragraph = "fragment 验收段落。" + "content " * 24
    return "\n".join(
        [
            "# One",
            "",
            paragraph,
            "",
            "## Two",
            "",
            paragraph,
            "",
            "## Three",
            "",
            paragraph,
            "",
            "[章节三](#three)",
            "",
            "[其他文档](other.md#three)",
            "",
            "[不存在的锚点](#missing)",
            "",
            "[本文档](guide.md)",
            "",
            "[普通文本](notes.txt)",
            "",
            "[外部文档](../outside.md)",
            "",
        ]
    )


def run_watcher_case(launcher: Path, root: Path) -> bool:
    """Drive the real application through watcher reload, rename and delete.

    Verifies that an unrelated change never reloads the current document,
    that a content change reloads it and restores the active heading, that a
    rename follows the document with its reading position, and that deleting
    the current document clears the reader.
    """
    workspace = root / "watcher-workspace"
    guide_dir = workspace / "docs"
    guide_dir.mkdir(parents=True)
    guide = guide_dir / "guide.md"
    guide.write_text(_long_document(), encoding="utf-8")
    (workspace / "other.md").write_text("# Other\n\ncontent\n", encoding="utf-8")

    environment = os.environ.copy()
    environment.update(
        {
            "GSETTINGS_BACKEND": "memory",
            "MDREADER_TEST_WATCHER_ACCEPT": "1",
            "MDREADER_TEST_HEADING": "three",
        }
    )
    process = subprocess.Popen(
        [str(launcher), str(workspace)],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    reader = MarkerReader(process)
    try:
        if reader.wait_for("MDREADER_TEST_WATCHER_PRESENT=1") is None:
            print("MD Reader watcher smoke: initial document was not presented", file=sys.stderr)
            return False
        if reader.wait_for("MDREADER_TEST_WATCHER_HEADING=three") is None:
            print("MD Reader watcher smoke: did not scroll to heading three", file=sys.stderr)
            return False

        (workspace / "other.md").write_text(
            "# Other\n\ncontent\n\nappended by harness\n", encoding="utf-8"
        )
        if not reader.wait_quiet("MDREADER_TEST_WATCHER_PRESENT=", 2.5):
            print(
                "MD Reader watcher smoke: unrelated change reloaded the document",
                file=sys.stderr,
            )
            return False

        with guide.open("a", encoding="utf-8") as stream:
            stream.write("\n## Four\n\nappended by harness\n")
        if reader.wait_for("MDREADER_TEST_WATCHER_PRESENT=2") is None:
            print(
                "MD Reader watcher smoke: current document change did not reload",
                file=sys.stderr,
            )
            return False
        if reader.wait_for("MDREADER_TEST_WATCHER_HEADING=three") is None:
            print(
                "MD Reader watcher smoke: reading position was not restored after reload",
                file=sys.stderr,
            )
            return False

        renamed = guide_dir / "renamed.md"
        guide.rename(renamed)
        if reader.wait_for("MDREADER_TEST_WATCHER_PRESENT=3") is None:
            print(
                "MD Reader watcher smoke: rename did not follow the document",
                file=sys.stderr,
            )
            return False
        if reader.wait_for("MDREADER_TEST_WATCHER_HEADING=three") is None:
            print(
                "MD Reader watcher smoke: reading position was not restored after rename",
                file=sys.stderr,
            )
            return False

        renamed.unlink()
        if reader.wait_for("MDREADER_TEST_WATCHER_CLEARED") is None:
            print(
                "MD Reader watcher smoke: deleted document did not clear the reader",
                file=sys.stderr,
            )
            return False
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(
                "MD Reader watcher smoke: application did not quit after clearing",
                file=sys.stderr,
            )
            return False
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    if process.returncode != 0:
        print(
            f"MD Reader watcher smoke exited with {process.returncode}",
            file=sys.stderr,
        )
        if process.stderr:
            print(process.stderr.read(), file=sys.stderr)
        return False
    stderr_text = process.stderr.read() if process.stderr else ""
    if "Traceback" in stderr_text:
        print("MD Reader watcher smoke stderr contained a Traceback", file=sys.stderr)
        print(stderr_text, file=sys.stderr)
        return False
    print("MD Reader watcher smoke: reload/rename/delete and position restore verified")
    return True


def run_fragment_case(
    launcher: Path,
    root: Path,
    href: str,
    *,
    expect_reload: bool,
    expect_heading: str | None,
) -> bool:
    """Click one local link in the real application and verify the outcome."""
    workspace = root / "fragment-workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "guide.md").write_text(_fragment_document(), encoding="utf-8")
    (workspace / "other.md").write_text(
        "# Other\n\n## Three\n\ncontent\n", encoding="utf-8"
    )
    (workspace / "notes.txt").write_text("not markdown\n", encoding="utf-8")

    environment = os.environ.copy()
    environment.update(
        {
            "GSETTINGS_BACKEND": "memory",
            "MDREADER_TEST_WATCHER_ACCEPT": "1",
            "MDREADER_TEST_FRAGMENT_CLICK": href,
        }
    )
    process = subprocess.Popen(
        [str(launcher), str(workspace)],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    reader = MarkerReader(process)
    try:
        lines = reader.collect(timeout=20)
        joined = "\n".join(lines)
        if "MDREADER_TEST_WATCHER_PRESENT=1" not in joined:
            print(
                f"MD Reader fragment smoke {href}: initial document was not presented",
                file=sys.stderr,
            )
            return False
        if expect_reload:
            if "MDREADER_TEST_WATCHER_PRESENT=2" not in joined:
                print(
                    f"MD Reader fragment smoke {href}: cross-document link did not switch",
                    file=sys.stderr,
                )
                return False
        elif "MDREADER_TEST_WATCHER_PRESENT=2" in joined:
            print(
                f"MD Reader fragment smoke {href}: link reloaded the current page",
                file=sys.stderr,
            )
            return False
        if expect_heading is not None:
            heading_marker = f"MDREADER_TEST_WATCHER_HEADING={expect_heading}"
            if heading_marker not in joined:
                print(
                    f"MD Reader fragment smoke {href}: did not reach the fragment target",
                    file=sys.stderr,
                )
                return False
            if joined.find(heading_marker) < joined.find(
                "MDREADER_TEST_WATCHER_PRESENT=1"
            ):
                print(
                    f"MD Reader fragment smoke {href}: fragment target was reported "
                    "before the document was presented",
                    file=sys.stderr,
                )
                return False
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(
                f"MD Reader fragment smoke {href}: application did not quit",
                file=sys.stderr,
            )
            return False
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    if process.returncode != 0:
        print(
            f"MD Reader fragment smoke {href} exited with {process.returncode}",
            file=sys.stderr,
        )
        if process.stderr:
            print(process.stderr.read(), file=sys.stderr)
        return False
    stderr_text = process.stderr.read() if process.stderr else ""
    if "Traceback" in stderr_text:
        print(f"MD Reader fragment smoke {href} stderr contained a Traceback", file=sys.stderr)
        print(stderr_text, file=sys.stderr)
        return False
    print(f"MD Reader fragment smoke {href}: ok")
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
    source_fixture = Path(sys.argv[2]).resolve()
    with tempfile.TemporaryDirectory(prefix="mdreader-direct-open-") as temporary:
        workspace = Path(temporary)
        (workspace / "a-first.md").write_text("# Wrong document\n", encoding="utf-8")
        fixture = workspace / "z-requested.md"
        shutil.copyfile(source_fixture, fixture)
        blocked = workspace / "blocked"
        blocked.mkdir()
        blocked.chmod(0)
        try:
            if not run_case(launcher, fixture, selection_scroll=True):
                return 1
            if not run_case(launcher, fixture):
                return 1
            if not run_restore_case(launcher, workspace):
                return 1
            if not run_watcher_case(launcher, workspace):
                return 1
            for href, expect_reload, expect_heading in (
                ("#three", False, "three"),
                ("#missing", False, None),
                ("guide.md", False, None),
                ("notes.txt", False, None),
                ("../outside.md", False, None),
                ("other.md#three", True, "three"),
            ):
                if not run_fragment_case(
                    launcher,
                    workspace,
                    href,
                    expect_reload=expect_reload,
                    expect_heading=expect_heading,
                ):
                    return 1
        finally:
            blocked.chmod(0o700)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
