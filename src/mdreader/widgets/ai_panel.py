from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from mdreader.models import DocumentSelection


class AiPanel(Gtk.Box):
    def __init__(
        self,
        on_jump_to_selection: Callable[[int], None],
        on_send: Callable[[str, bool], None],
        on_cancel: Callable[[], None],
        *,
        current_model: str,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("ai-pane")
        self._on_jump_to_selection = on_jump_to_selection
        self._on_send = on_send
        self._on_cancel = on_cancel
        self._selection = DocumentSelection()
        self._document: Path | None = None
        self._assistant_label: Gtk.Label | None = None
        self._assistant_text = ""
        self._running = False
        self._available = (
            os.environ.get("MDREADER_TEST_OPENCODE_MISSING") != "1"
            and shutil.which("opencode") is not None
        )
        self._models: tuple[str, ...] = ()

        header_title = Gtk.Label(label="AI Assistant")
        header_title.add_css_class("heading")
        self._model_label = Gtk.Label(
            ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=24,
            single_line_mode=True,
        )
        self._model_label.add_css_class("caption")
        self._model_label.add_css_class("dimmed")
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title_box.append(header_title)
        title_box.append(self._model_label)
        header = Adw.HeaderBar(title_widget=title_box)

        self._model_menu = Gio.Menu()
        self._model_button = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            menu_model=self._model_menu,
            sensitive=False,
        )
        self._model_button.set_tooltip_text("Choose OpenCode model")
        self._model_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Choose OpenCode model"]
        )
        header.pack_end(self._model_button)
        self.set_current_model(current_model)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)

        self._context_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            reveal_child=False,
        )
        context_button = Gtk.Button(has_frame=False)
        context_button.set_tooltip_text("Return to selected text")
        context_button.connect("clicked", self._on_context_clicked)
        context_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        context_box.add_css_class("context-rail")
        self._context_meta = Gtk.Label(xalign=0)
        self._context_meta.add_css_class("caption")
        self._context_meta.add_css_class("dimmed")
        self._context_quote = Gtk.Label(
            xalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            lines=4,
            ellipsize=Pango.EllipsizeMode.END,
        )
        self._context_quote.add_css_class("context-quote")
        context_box.append(self._context_meta)
        context_box.append(self._context_quote)
        context_button.set_child(context_box)
        self._context_revealer.set_child(context_button)
        content.append(self._context_revealer)

        self._transcript = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._transcript.set_margin_end(4)
        self._transcript.set_valign(Gtk.Align.START)
        self._scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._scroll.set_child(self._transcript)
        content.append(self._scroll)

        self._status = Adw.StatusPage(
            icon_name="chat-symbolic" if self._available else "network-offline-symbolic",
            title="Discuss the current document" if self._available else "OpenCode unavailable",
            description=(
                "Ask about the current section or select text for precise context"
                if self._available
                else "Install and configure OpenCode; reading remains fully available"
            ),
        )
        self._transcript.append(self._status)

        composer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._edit_toggle = Gtk.ToggleButton(icon_name="document-edit-symbolic")
        self._edit_toggle.set_tooltip_text("Propose a change to the selected lines")
        self._edit_toggle.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Propose an edit to selected lines"]
        )
        self._edit_toggle.connect("toggled", self._on_edit_toggled)
        composer.append(self._edit_toggle)
        self._entry = Gtk.Entry(
            hexpand=True,
            placeholder_text="Ask about this document…",
            sensitive=False,
        )
        self._entry.connect("activate", self._on_send_requested)
        composer.append(self._entry)

        self._send_button = Gtk.Button(icon_name="mail-send-symbolic", sensitive=False)
        self._send_button.add_css_class("suggested-action")
        self._send_button.set_tooltip_text("Send message")
        self._send_button.update_property([Gtk.AccessibleProperty.LABEL], ["Send message"])
        self._send_button.connect("clicked", self._on_send_requested)
        composer.append(self._send_button)

        self._cancel_button = Gtk.Button(icon_name="process-stop-symbolic", visible=False)
        self._cancel_button.set_tooltip_text("Cancel response")
        self._cancel_button.update_property([Gtk.AccessibleProperty.LABEL], ["Cancel response"])
        self._cancel_button.connect("clicked", lambda *_: self._on_cancel())
        composer.append(self._cancel_button)
        content.append(composer)

        toolbar = Adw.ToolbarView(content=content)
        toolbar.add_top_bar(header)
        self.append(toolbar)

    def set_document(self, relative_path: Path | None) -> None:
        self._document = relative_path
        self._update_composer()

    def set_model_options(self, models: tuple[str, ...], current_model: str) -> None:
        self._models = models
        self._model_menu.remove_all()
        for model in models:
            item = Gio.MenuItem.new(self._short_model_name(model), None)
            item.set_action_and_target_value(
                "win.select-model",
                GLib.Variant.new_string(model),
            )
            self._model_menu.append_item(item)
        self.set_current_model(current_model)
        self._update_composer()

    def set_current_model(self, model: str, *, new_conversation: bool = False) -> None:
        label = self._short_model_name(model)
        self._model_label.set_label(label)
        self._model_label.set_tooltip_text(model)
        if not new_conversation:
            return
        self._assistant_label = None
        self._assistant_text = ""
        self._clear_box(self._transcript)
        self._status.set_icon_name("chat-symbolic")
        self._status.set_title(f"Using {label}")
        self._status.set_description("Start a new conversation with this model")
        self._transcript.append(self._status)

    def set_selection(self, selection: DocumentSelection) -> None:
        self._selection = selection
        if selection.is_empty:
            self._edit_toggle.set_active(False)
            self._context_revealer.set_reveal_child(False)
            return

        location = str(self._document) if self._document else "Current document"
        if selection.start_line > 0:
            location += f" · lines {selection.start_line}–{selection.end_line}"
        if selection.heading_title:
            location += f" · {selection.heading_title}"
        self._context_meta.set_label(location)
        self._context_quote.set_label(selection.text)
        self._context_revealer.set_reveal_child(True)

    def append_user(self, text: str) -> None:
        self._hide_status()
        self._transcript.append(self._message_block("You", text, "user-message"))
        self._scroll_to_bottom()

    def begin_assistant(self, *, edit_mode: bool = False) -> None:
        self._hide_status()
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        label = Gtk.Label(label="OpenCode", xalign=0)
        label.add_css_class("caption")
        label.add_css_class("dimmed")
        block.append(label)
        self._assistant_label = Gtk.Label(
            xalign=0,
            yalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
        )
        self._assistant_label.add_css_class("assistant-message")
        if edit_mode:
            self._assistant_label.set_label("Preparing a change for review…")
            self._assistant_label.add_css_class("dimmed")
        block.append(self._assistant_label)
        self._transcript.append(block)
        self._assistant_text = ""
        self._set_running(True)
        self._scroll_to_bottom()

    def append_assistant_text(self, text: str) -> None:
        if self._assistant_label is None:
            self.begin_assistant()
        self._assistant_text += text
        self._assistant_label.set_label(self._assistant_text)
        self._scroll_to_bottom()

    def finish_assistant(self, message: str = "") -> None:
        if message and self._assistant_label is not None:
            self._assistant_label.set_label(message)
            self._assistant_label.remove_css_class("dimmed")
        self._set_running(False)

    def show_error(self, message: str) -> None:
        if self._assistant_label is not None and not self._assistant_text:
            self._assistant_label.set_label(message)
            self._assistant_label.add_css_class("error")
        else:
            self._transcript.append(self._message_block("OpenCode", message, "error"))
        self._set_running(False)
        self._scroll_to_bottom()

    def focus_composer(self) -> None:
        self._entry.grab_focus()

    def popup_model_menu(self) -> None:
        self._model_button.popup()

    def _on_send_requested(self, _widget: Gtk.Widget) -> None:
        text = self._entry.get_text().strip()
        if not text or self._running or not self._document:
            return
        edit_mode = self._edit_toggle.get_active()
        self._entry.set_text("")
        if edit_mode:
            self._edit_toggle.set_active(False)
        self._on_send(text, edit_mode)

    def _on_edit_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active() and self._selection.is_empty:
            button.set_active(False)
            self.show_error("Select the lines to change before proposing an edit")
            return
        self._entry.set_placeholder_text(
            "Describe the selected-line change…"
            if button.get_active()
            else "Ask about this document…"
        )

    def _on_context_clicked(self, _button: Gtk.Button) -> None:
        if self._selection.start_line:
            self._on_jump_to_selection(self._selection.start_line)

    def _set_running(self, running: bool) -> None:
        self._running = running
        self._send_button.set_visible(not running)
        self._cancel_button.set_visible(running)
        self._update_composer()

    def _update_composer(self) -> None:
        enabled = self._available and self._document is not None and not self._running
        self._entry.set_sensitive(enabled)
        self._send_button.set_sensitive(enabled)
        self._edit_toggle.set_sensitive(enabled)
        self._model_button.set_sensitive(
            self._available and bool(self._models) and not self._running
        )

    def _hide_status(self) -> None:
        if self._status.get_parent() is self._transcript:
            self._transcript.remove(self._status)

    @staticmethod
    def _message_block(role: str, text: str, css_class: str) -> Gtk.Widget:
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        role_label = Gtk.Label(label=role, xalign=0)
        role_label.add_css_class("caption")
        role_label.add_css_class("dimmed")
        block.append(role_label)
        body = Gtk.Label(
            label=text,
            xalign=0,
            yalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
        )
        body.add_css_class(css_class)
        block.append(body)
        return block

    @staticmethod
    def _short_model_name(model: str) -> str:
        return model.removeprefix("opencode/")

    @staticmethod
    def _clear_box(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            box.remove(child)
            child = following

    def _scroll_to_bottom(self) -> None:
        def scroll() -> bool:
            adjustment = self._scroll.get_vadjustment()
            adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))
            return False

        GLib.idle_add(scroll)
