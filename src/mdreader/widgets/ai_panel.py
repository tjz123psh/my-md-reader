from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from mdreader.models import DocumentSelection
from mdreader.services.ai_markdown import AiMarkdownBlock, AiMarkdownRenderer


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
        self._assistant_body: Gtk.Box | None = None
        self._assistant_content: Gtk.Box | None = None
        self._thinking_row: Gtk.Box | None = None
        self._assistant_text = ""
        self._last_rendered_text = ""
        self._render_source_id = 0
        self._scroll_source_id = 0
        self._markdown = AiMarkdownRenderer()
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
        header = Adw.HeaderBar(
            title_widget=title_box,
            show_start_title_buttons=False,
            show_end_title_buttons=False,
        )

        close_button = Gtk.Button(
            icon_name="window-close-symbolic",
            action_name="win.hide-ai",
        )
        close_button.set_tooltip_text("Hide AI assistant")
        close_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Hide AI assistant"]
        )
        header.pack_end(close_button)

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

        self._mode_group = Adw.ToggleGroup()
        self._ask_mode = Adw.Toggle(
            name="ask",
            label="Ask",
            icon_name="chat-symbolic",
            tooltip="Discuss the current document without changing files",
        )
        self._edit_mode = Adw.Toggle(
            name="edit",
            label="Edit",
            icon_name="document-edit-symbolic",
            tooltip="Propose a reviewed change to the selected lines",
        )
        self._mode_group.add(self._ask_mode)
        self._mode_group.add(self._edit_mode)
        self._mode_group.set_active_name("ask")
        self._mode_group.connect("notify::active-name", self._on_mode_changed)
        content.append(self._mode_group)

        self._edit_banner = Adw.Banner(
            title="Select text in the document before proposing a change"
        )
        self._edit_banner.set_revealed(False)
        content.append(self._edit_banner)

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

        composer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._prompt_buffer = Gtk.TextBuffer()
        self._prompt_buffer.connect("changed", self._on_prompt_changed)
        self._prompt_view = Gtk.TextView(
            buffer=self._prompt_buffer,
            hexpand=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            sensitive=False,
        )
        self._prompt_view.add_css_class("ai-prompt")
        self._prompt_view.set_top_margin(10)
        self._prompt_view.set_bottom_margin(10)
        self._prompt_view.set_left_margin(10)
        self._prompt_view.set_right_margin(10)
        self._prompt_view.update_property(
            [Gtk.AccessibleProperty.LABEL], ["AI prompt"]
        )
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_prompt_key_pressed)
        self._prompt_view.add_controller(key_controller)

        prompt_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            min_content_height=74,
            max_content_height=150,
            propagate_natural_height=True,
        )
        prompt_scroll.add_css_class("ai-prompt-frame")
        prompt_scroll.set_child(self._prompt_view)
        prompt_overlay = Gtk.Overlay()
        prompt_overlay.set_child(prompt_scroll)
        self._prompt_placeholder = Gtk.Label(
            label="Ask about this document or add instructions…",
            xalign=0,
            yalign=0,
            wrap=True,
        )
        self._prompt_placeholder.add_css_class("dimmed")
        self._prompt_placeholder.add_css_class("ai-prompt-placeholder")
        self._prompt_placeholder.set_can_target(False)
        self._prompt_placeholder.set_margin_start(12)
        self._prompt_placeholder.set_margin_end(12)
        self._prompt_placeholder.set_margin_top(11)
        prompt_overlay.add_overlay(self._prompt_placeholder)
        composer.append(prompt_overlay)

        composer_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        composer_hint = Gtk.Label(
            label="Ctrl+Enter to send",
            xalign=0,
            hexpand=True,
            ellipsize=Pango.EllipsizeMode.END,
        )
        composer_hint.add_css_class("caption")
        composer_hint.add_css_class("dimmed")
        composer_actions.append(composer_hint)

        self._send_button = Gtk.Button(
            icon_name="mail-send-symbolic",
            sensitive=False,
        )
        self._send_button.add_css_class("suggested-action")
        self._send_button.set_tooltip_text("Send message")
        self._send_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Send message"]
        )
        self._send_button.connect("clicked", self._on_send_requested)
        composer_actions.append(self._send_button)

        self._cancel_button = Gtk.Button(
            icon_name="process-stop-symbolic",
            visible=False,
        )
        self._cancel_button.set_tooltip_text("Cancel response")
        self._cancel_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Cancel response"]
        )
        self._cancel_button.connect("clicked", lambda *_: self._on_cancel())
        composer_actions.append(self._cancel_button)
        composer.append(composer_actions)
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
        self._assistant_body = None
        self._assistant_content = None
        self._thinking_row = None
        self._assistant_text = ""
        self._last_rendered_text = ""
        self._clear_box(self._transcript)
        self._status.set_icon_name("chat-symbolic")
        self._status.set_title(f"Using {label}")
        self._status.set_description("Start a new conversation with this model")
        self._transcript.append(self._status)

    def set_selection(self, selection: DocumentSelection) -> None:
        self._selection = selection
        if selection.is_empty:
            self._context_revealer.set_reveal_child(False)
            self._update_mode_hint()
            self._update_composer()
            return

        location = str(self._document) if self._document else "Current document"
        if selection.start_line > 0:
            location += f" · lines {selection.start_line}–{selection.end_line}"
        if selection.heading_title:
            location += f" · {selection.heading_title}"
        self._context_meta.set_label(location)
        self._context_quote.set_label(selection.text)
        self._context_revealer.set_reveal_child(True)
        self._update_mode_hint()
        self._update_composer()

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

        self._assistant_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._assistant_body.add_css_class("assistant-message")
        self._thinking_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self._thinking_row.add_css_class("ai-thinking")
        spinner = Adw.Spinner()
        spinner.update_property([Gtk.AccessibleProperty.LABEL], ["OpenCode is thinking"])
        thinking_label = Gtk.Label(
            label="Thinking about the selected change…" if edit_mode else "Thinking…",
            xalign=0,
        )
        thinking_label.add_css_class("caption")
        self._thinking_row.append(spinner)
        self._thinking_row.append(thinking_label)
        self._assistant_body.append(self._thinking_row)

        self._assistant_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._assistant_content.add_css_class("ai-markdown")
        self._assistant_body.append(self._assistant_content)
        block.append(self._assistant_body)
        self._transcript.append(block)
        self._assistant_text = ""
        self._last_rendered_text = ""
        self._set_running(True)
        self._scroll_to_bottom()

    def append_assistant_text(self, text: str) -> None:
        if self._assistant_body is None:
            self.begin_assistant()
        self._assistant_text += text
        if not self._render_source_id:
            if not self._last_rendered_text:
                self._render_source_id = GLib.idle_add(self._render_assistant_text)
            else:
                self._render_source_id = GLib.timeout_add(
                    140, self._render_assistant_text
                )

    def finish_assistant(self, message: str = "") -> None:
        self._cancel_render_timeout()
        if message:
            self._assistant_text = message
        self._render_assistant_text()
        self._remove_thinking_row()
        self._set_running(False)
        self._scroll_to_bottom()

    def show_error(self, message: str) -> None:
        self._cancel_render_timeout()
        if self._assistant_content is not None and not self._assistant_text:
            self._clear_box(self._assistant_content)
            error_label = self._new_markup_label(GLib.markup_escape_text(message))
            error_label.add_css_class("error")
            self._assistant_content.append(error_label)
            self._remove_thinking_row()
        else:
            self._render_assistant_text()
            self._remove_thinking_row()
            self._transcript.append(self._message_block("OpenCode", message, "error"))
        self._set_running(False)
        self._scroll_to_bottom()

    def focus_composer(self) -> None:
        self._prompt_view.grab_focus()

    def popup_model_menu(self) -> None:
        self._model_button.popup()

    def _on_send_requested(self, _widget: Gtk.Widget) -> None:
        text = self._prompt_buffer.get_text(
            self._prompt_buffer.get_start_iter(),
            self._prompt_buffer.get_end_iter(),
            False,
        ).strip()
        if not text or self._running or not self._document:
            return
        edit_mode = self._mode_group.get_active_name() == "edit"
        self._prompt_buffer.set_text("")
        self._on_send(text, edit_mode)

    def _on_mode_changed(self, group: Adw.ToggleGroup, _param: object) -> None:
        edit_mode = group.get_active_name() == "edit"
        self._prompt_placeholder.set_label(
            "Select text, then describe the change you want…"
            if edit_mode
            else "Ask about this document or add instructions…"
        )
        self._update_mode_hint()
        self._update_composer()

    def _update_mode_hint(self) -> None:
        self._edit_banner.set_revealed(
            self._mode_group.get_active_name() == "edit"
            and self._selection.is_empty
        )

    def _on_prompt_changed(self, buffer: Gtk.TextBuffer) -> None:
        self._prompt_placeholder.set_visible(buffer.get_char_count() == 0)
        self._update_composer()

    def _on_prompt_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        if not state & Gdk.ModifierType.CONTROL_MASK:
            return False
        self._on_send_requested(self._prompt_view)
        return True

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
        edit_ready = (
            self._mode_group.get_active_name() != "edit"
            or not self._selection.is_empty
        )
        self._prompt_view.set_sensitive(enabled)
        self._send_button.set_sensitive(
            enabled
            and edit_ready
            and self._prompt_buffer.get_char_count() > 0
        )
        self._mode_group.set_sensitive(enabled)
        self._edit_mode.set_enabled(enabled)
        self._model_button.set_sensitive(
            self._available and bool(self._models) and not self._running
        )

    def _hide_status(self) -> None:
        if self._status.get_parent() is self._transcript:
            self._transcript.remove(self._status)

    def _render_assistant_text(self) -> bool:
        self._render_source_id = 0
        if self._assistant_content is None:
            return GLib.SOURCE_REMOVE
        if self._assistant_text == self._last_rendered_text:
            return GLib.SOURCE_REMOVE
        self._clear_box(self._assistant_content)
        dark = Adw.StyleManager.get_default().get_dark()
        for block in self._markdown.render(self._assistant_text, dark=dark):
            self._assistant_content.append(self._markdown_block(block))
        self._last_rendered_text = self._assistant_text
        self._scroll_to_bottom()
        return GLib.SOURCE_REMOVE

    def _markdown_block(self, block: AiMarkdownBlock) -> Gtk.Widget:
        if block.kind == "separator":
            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            separator.add_css_class("ai-markdown-separator")
            return separator
        if block.kind == "table":
            return self._markdown_table(block)
        if block.kind == "code":
            code_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            code_box.add_css_class("ai-code-block")
            if block.language:
                language = Gtk.Label(label=block.language, xalign=0)
                language.add_css_class("caption")
                language.add_css_class("dimmed")
                code_box.append(language)
            code = self._new_markup_label(block.markup)
            code.add_css_class("monospace")
            code.add_css_class("ai-code-text")
            code_box.append(code)
            return code_box

        label = self._new_markup_label(block.markup)
        label.add_css_class(f"ai-markdown-{block.kind}")
        if block.kind == "heading":
            label.add_css_class("heading")
            label.add_css_class(f"ai-heading-{min(3, max(1, block.level))}")
        elif block.kind == "list-item":
            label.set_margin_start(min(36, block.level * 14))
        return label

    def _markdown_table(self, block: AiMarkdownBlock) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=0, row_spacing=0)
        grid.add_css_class("ai-table")
        grid.set_halign(Gtk.Align.START)
        grid.set_valign(Gtk.Align.START)
        column_count = max((len(row) for row in block.rows), default=1)
        cell_width = max(8, 38 // column_count)
        for row_index, row in enumerate(block.rows):
            for column_index, cell in enumerate(row):
                label = self._new_markup_label(cell.markup)
                label.set_max_width_chars(cell_width)
                label.add_css_class("ai-table-cell")
                if cell.header:
                    label.add_css_class("ai-table-header")
                grid.attach(label, column_index, row_index, 1, 1)
        return grid

    def _new_markup_label(self, markup: str) -> Gtk.Label:
        label = Gtk.Label(
            xalign=0,
            yalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
        )
        label.set_markup(markup)
        label.connect("activate-link", self._on_markdown_link)
        return label

    def _on_markdown_link(self, _label: Gtk.Label, uri: str) -> bool:
        launcher = Gtk.UriLauncher.new(uri)
        launcher.launch(self.get_root(), None, None, None)
        return True

    def _remove_thinking_row(self) -> None:
        if (
            self._assistant_body is not None
            and self._thinking_row is not None
            and self._thinking_row.get_parent() is self._assistant_body
        ):
            self._assistant_body.remove(self._thinking_row)
        self._thinking_row = None

    def _cancel_render_timeout(self) -> None:
        if self._render_source_id:
            GLib.source_remove(self._render_source_id)
            self._render_source_id = 0

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
        if self._scroll_source_id:
            return

        def scroll() -> bool:
            self._scroll_source_id = 0
            adjustment = self._scroll.get_vadjustment()
            adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))
            return GLib.SOURCE_REMOVE

        self._scroll_source_id = GLib.idle_add(scroll)
