from __future__ import annotations

import os
import sys
from pathlib import Path

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
