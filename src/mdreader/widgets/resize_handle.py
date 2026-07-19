from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk


class SidebarResizeHandle(Gtk.Box):
    """A small, keyboard-safe mouse target for resizing a side pane."""

    def __init__(
        self,
        on_begin: Callable[[], None],
        on_update: Callable[[float], None],
        on_end: Callable[[], None],
        *,
        tooltip: str,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_size_request(10, -1)
        self.set_hexpand(False)
        self.set_vexpand(True)
        self.set_can_focus(False)
        self.set_cursor(Gdk.Cursor.new_from_name("ew-resize", None))
        self.set_tooltip_text(tooltip)
        self.add_css_class("sidebar-resize-handle")

        gesture = Gtk.GestureDrag()
        gesture.set_button(1)
        gesture.connect("drag-begin", self._on_drag_begin, on_begin)
        gesture.connect("drag-update", self._on_drag_update, on_update)
        gesture.connect("drag-end", self._on_drag_end, on_end)
        self.add_controller(gesture)
        self._gesture = gesture

    def _on_drag_begin(
        self,
        _gesture: Gtk.GestureDrag,
        _start_x: float,
        _start_y: float,
        callback: Callable[[], None],
    ) -> None:
        self.add_css_class("dragging")
        callback()

    def _on_drag_update(
        self,
        _gesture: Gtk.GestureDrag,
        offset_x: float,
        _offset_y: float,
        callback: Callable[[float], None],
    ) -> None:
        callback(offset_x)

    def _on_drag_end(
        self,
        _gesture: Gtk.GestureDrag,
        _offset_x: float,
        _offset_y: float,
        callback: Callable[[], None],
    ) -> None:
        self.remove_css_class("dragging")
        callback()
