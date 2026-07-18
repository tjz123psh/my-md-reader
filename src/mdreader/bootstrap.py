from __future__ import annotations

import os
import sys
from pathlib import Path


def _fcitx_gtk4_module_available() -> bool:
    roots = (Path("/usr/lib"), Path("/usr/lib64"), Path("/usr/local/lib"))
    patterns = (
        "gtk-4.0/*/immodules/libim-fcitx5.so",
        "*/gtk-4.0/*/immodules/libim-fcitx5.so",
    )
    return any(
        any(root.glob(pattern))
        for root in roots
        if root.is_dir()
        for pattern in patterns
    )


def configure_gtk_input_method() -> None:
    """Select the installed Fcitx GTK 4 bridge before GTK is initialized."""
    if os.environ.get("GTK_IM_MODULE"):
        return
    hints = " ".join(
        (
            os.environ.get("XMODIFIERS", ""),
            os.environ.get("QT_IM_MODULE", ""),
        )
    ).casefold()
    if "fcitx" in hints and _fcitx_gtk4_module_available():
        os.environ["GTK_IM_MODULE"] = "fcitx"


configure_gtk_input_method()

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio


def _resource_candidates() -> tuple[Path, ...]:
    configured = os.environ.get("MDREADER_RESOURCE_FILE")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            Path("/usr/local/share/mdreader/mdreader.gresource"),
            Path("/usr/share/mdreader/mdreader.gresource"),
        ]
    )
    return tuple(candidates)


def _register_resources() -> None:
    for candidate in _resource_candidates():
        if candidate.is_file():
            resource = Gio.Resource.load(str(candidate))
            Gio.resources_register(resource)
            return
    raise RuntimeError(
        "MD Reader resources were not found. Run through `meson devenv -C build` "
        "or install the application."
    )


def main() -> int:
    _register_resources()
    from .application import MdReaderApplication

    return MdReaderApplication().run(sys.argv)
