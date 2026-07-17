from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from mdreader.models import DocumentSelection, RenderedDocument
from mdreader.services import MarkdownRenderer

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
except (ImportError, ValueError):
    WebKit = None


class DocumentView(Gtk.Box):
    __gsignals__ = {
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "active-heading-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "document-presented": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "open-local-document": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "zoom-requested": (GObject.SignalFlags.RUN_FIRST, None, (int, float)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._renderer = MarkdownRenderer()
        self._load_generation = 0
        self._zoom = 100
        self._document_path: Path | None = None
        self._web_view = None

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_vexpand(True)
        self.append(self._stack)

        empty = Adw.StatusPage(
            icon_name="text-x-generic-symbolic",
            title="Open a folder of Markdown files",
            description="Choose a folder, then select a document to start reading",
        )
        open_button = Gtk.Button(label="Open Folder", action_name="win.open-folder")
        open_button.add_css_class("pill")
        open_button.add_css_class("suggested-action")
        open_button.set_halign(Gtk.Align.CENTER)
        empty.set_child(open_button)
        self._stack.add_named(empty, "empty")

        self._loading = Adw.StatusPage(
            icon_name="text-x-generic-symbolic",
            title="Rendering document",
            description="Preparing typography, outline and source locations…",
        )
        self._stack.add_named(self._loading, "loading")

        self._error = Adw.StatusPage(icon_name="dialog-error-symbolic", title="Could not render document")
        self._stack.add_named(self._error, "error")

        if WebKit is None:
            unavailable = Adw.StatusPage(
                icon_name="dialog-warning-symbolic",
                title="WebKitGTK 6 is required",
                description=(
                    "Install webkitgtk-6.0 to enable the high-quality Markdown reading surface"
                ),
            )
            unavailable.add_css_class("reader-fallback")
            self._stack.add_named(unavailable, "unavailable")
        else:
            self._create_web_view()

        self._stack.set_visible_child_name("empty")

    @property
    def webkit_available(self) -> bool:
        return self._web_view is not None

    def load_document(
        self,
        path: Path,
        *,
        zoom: int,
        workspace_root: Path | None = None,
        on_loaded: Callable[[RenderedDocument], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        self._load_generation += 1
        generation = self._load_generation
        self._document_path = path
        self._zoom = max(75, min(200, zoom))
        self._stack.set_visible_child_name("loading")

        def worker() -> None:
            try:
                with path.open("r", encoding="utf-8", newline="") as stream:
                    source = stream.read()
                rendered = self._renderer.render(
                    source,
                    title=path.name,
                    zoom=self._zoom,
                    document_path=path,
                    workspace_root=workspace_root,
                )
            except Exception as error:  # Returned to GTK as a precise error state.
                GLib.idle_add(self._finish_error, generation, error, on_error)
                return
            GLib.idle_add(self._finish_load, generation, path, rendered, on_loaded)

        threading.Thread(target=worker, name="mdreader-render", daemon=True).start()

    def set_zoom(self, zoom: int, anchor_y: float | None = None) -> None:
        self._zoom = max(75, min(200, zoom))
        if self._web_view is not None:
            anchor = "null" if anchor_y is None else json.dumps(float(anchor_y))
            self._evaluate(f"window.mdReader?.setZoom({self._zoom}, {anchor});")

    def find(self, text: str) -> None:
        if self._web_view is None:
            return
        controller = self._web_view.get_find_controller()
        if not text:
            controller.search_finish()
            return
        controller.search(
            text,
            WebKit.FindOptions.CASE_INSENSITIVE | WebKit.FindOptions.WRAP_AROUND,
            1000,
        )

    def scroll_to_heading(self, slug: str) -> None:
        self._evaluate(f"window.mdReader?.scrollToHeading({json.dumps(slug)});")

    def scroll_to_source(self, line: int) -> None:
        self._evaluate(f"window.mdReader?.scrollToSource({int(line)});")

    def dispatch_ctrl_wheel_for_test(self) -> None:
        self._evaluate(
            "window.dispatchEvent(new WheelEvent('wheel',"
            "{ctrlKey:true,deltaY:-100,clientY:240,cancelable:true}));"
        )

    def _create_web_view(self) -> None:
        manager = WebKit.UserContentManager()
        manager.register_script_message_handler("selection", None)
        manager.connect("script-message-received::selection", self._on_selection_message)
        manager.register_script_message_handler("outline", None)
        manager.connect("script-message-received::outline", self._on_outline_message)
        manager.register_script_message_handler("zoom", None)
        manager.connect("script-message-received::zoom", self._on_zoom_message)
        self._web_view = WebKit.WebView(user_content_manager=manager)
        settings = self._web_view.get_settings()
        settings.set_enable_javascript(True)
        settings.set_javascript_can_open_windows_automatically(False)
        settings.set_enable_developer_extras(False)
        settings.set_enable_smooth_scrolling(True)
        # WebKitGTK's DMA-BUF renderer can leave unpainted white tiles on
        # Wayland/NVIDIA while scrolling long documents. A reading surface
        # favors reliable text painting over accelerated compositing.
        settings.set_hardware_acceleration_policy(
            WebKit.HardwareAccelerationPolicy.NEVER
        )
        self._web_view.connect("decide-policy", self._on_decide_policy)
        self._web_view.connect("load-changed", self._on_load_changed)
        self._stack.add_named(self._web_view, "reader")

    def _on_load_changed(self, _view: object, event: object) -> None:
        if event != WebKit.LoadEvent.FINISHED:
            return
        self.emit("document-presented")
        if os.environ.get("MDREADER_TEST_SELECT_FIRST") != "1":
            return
        self._evaluate(
            "const node=document.querySelector('p');"
            "if(node){const r=document.createRange();r.selectNodeContents(node);"
            "const s=window.getSelection();s.removeAllRanges();s.addRange(r);}"
        )

    def _finish_load(
        self,
        generation: int,
        path: Path,
        rendered: RenderedDocument,
        on_loaded: Callable[[RenderedDocument], None],
    ) -> bool:
        if generation != self._load_generation:
            return GLib.SOURCE_REMOVE
        if self._web_view is None:
            self._stack.set_visible_child_name("unavailable")
        else:
            base_uri = path.parent.as_uri().rstrip("/") + "/"
            self._web_view.load_html(rendered.html, base_uri)
            self._stack.set_visible_child_name("reader")
        on_loaded(rendered)
        return GLib.SOURCE_REMOVE

    def _finish_error(
        self,
        generation: int,
        error: Exception,
        on_error: Callable[[Exception], None],
    ) -> bool:
        if generation != self._load_generation:
            return GLib.SOURCE_REMOVE
        self._error.set_description(str(error))
        self._stack.set_visible_child_name("error")
        on_error(error)
        return GLib.SOURCE_REMOVE

    def _on_selection_message(self, _manager: object, message: object) -> None:
        try:
            value = message.get_js_value() if hasattr(message, "get_js_value") else message
            if hasattr(value, "is_string") and value.is_string():
                raw = value.to_string()
            else:
                raw = value.to_json(0) if hasattr(value, "to_json") else str(value)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return
            heading = payload.get("heading") or {}
            selection = DocumentSelection(
                text=str(payload.get("text", ""))[:12000],
                start_line=max(0, int(payload.get("startLine", 0))),
                end_line=max(0, int(payload.get("endLine", 0))),
                heading_id=str(heading.get("id", "")),
                heading_title=str(heading.get("title", "")),
            )
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            return
        self.emit("selection-changed", selection)

    def _on_outline_message(self, _manager: object, message: object) -> None:
        try:
            value = message.get_js_value() if hasattr(message, "get_js_value") else message
            if hasattr(value, "is_string") and value.is_string():
                raw = value.to_string()
            else:
                raw = value.to_json(0) if hasattr(value, "to_json") else str(value)
            payload = json.loads(raw)
            heading_id = payload.get("id", "") if isinstance(payload, dict) else ""
            if not isinstance(heading_id, str) or len(heading_id) > 512:
                return
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            return
        self.emit("active-heading-changed", heading_id)

    def _on_zoom_message(self, _manager: object, message: object) -> None:
        try:
            value = message.get_js_value() if hasattr(message, "get_js_value") else message
            if hasattr(value, "is_string") and value.is_string():
                raw = value.to_string()
            else:
                raw = value.to_json(0) if hasattr(value, "to_json") else str(value)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return
            percent = max(75, min(200, int(payload.get("percent", 100))))
            anchor_y = float(payload.get("anchorY", 0))
            if not math.isfinite(anchor_y):
                return
            anchor_y = max(0.0, min(10000.0, anchor_y))
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            return
        self._zoom = percent
        self.emit("zoom-requested", percent, anchor_y)

    def _on_decide_policy(self, _view: object, decision: object, decision_type: object) -> bool:
        if decision_type != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        request = decision.get_navigation_action().get_request()
        uri = request.get_uri()
        if uri.startswith(("http://", "https://", "mailto:")):
            decision.ignore()
            launcher = Gtk.UriLauncher.new(uri)
            launcher.launch(self.get_root(), None, None, None)
            return True
        if uri.startswith("file:") and uri.partition("#")[0].lower().endswith(
            (".md", ".markdown", ".mdown", ".mkd")
        ):
            decision.ignore()
            self.emit("open-local-document", uri.partition("#")[0])
            return True
        return False

    def _evaluate(self, script: str) -> None:
        if self._web_view is None:
            return
        self._web_view.evaluate_javascript(script, -1, None, None, None, None, None)
