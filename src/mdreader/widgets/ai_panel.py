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
from mdreader.services.ai_markdown import AiMarkdownBlock, AiMarkdownRenderer
from mdreader.services.themes import ReaderTheme


class AiPanel(Gtk.Box):
    def __init__(
        self,
        on_jump_to_selection: Callable[[int], None],
        on_send: Callable[[str, bool], None],
        on_cancel: Callable[[], None],
        *,
        current_model: str,
        theme: ReaderTheme,
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
        self._assistant_history: list[tuple[Gtk.Box, str]] = []
        self._render_source_id = 0
        self._scroll_source_id = 0
        self._markdown = AiMarkdownRenderer()
        self._theme = theme
        self._running = False
        self._available = (
            os.environ.get("MDREADER_TEST_OPENCODE_MISSING") != "1"
            and shutil.which("opencode") is not None
        )
        self._models: tuple[str, ...] = ()

        header_title = Gtk.Label(label="AI 助手")
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
        close_button.set_tooltip_text("隐藏 AI 助手")
        close_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["隐藏 AI 助手"]
        )
        header.pack_end(close_button)

        self._model_menu = Gio.Menu()
        self._model_button = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            menu_model=self._model_menu,
            sensitive=False,
        )
        self._model_button.set_tooltip_text("选择 OpenCode 模型")
        self._model_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["选择 OpenCode 模型"]
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
            label="问答",
            icon_name="chat-symbolic",
            tooltip="讨论当前文档，不修改文件",
        )
        self._edit_mode = Adw.Toggle(
            name="edit",
            label="修改",
            icon_name="document-edit-symbolic",
            tooltip="针对选中的行提出可审核的修改",
        )
        self._mode_group.add(self._ask_mode)
        self._mode_group.add(self._edit_mode)
        self._mode_group.set_active_name("ask")
        self._mode_group.connect("notify::active-name", self._on_mode_changed)
        content.append(self._mode_group)

        self._edit_banner = Adw.Banner(
            title="请先在文档中选择文字，再提出修改要求"
        )
        self._edit_banner.set_revealed(False)
        content.append(self._edit_banner)

        self._context_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            reveal_child=False,
        )
        self._context_button = Gtk.Button(has_frame=False)
        self._context_button.add_css_class("context-status")
        self._context_button.set_tooltip_text("返回文档中的选中位置")
        self._context_button.connect("clicked", self._on_context_clicked)
        context_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        context_icon = Gtk.Image(icon_name="selection-mode-symbolic")
        context_icon.add_css_class("context-status-icon")
        self._context_meta = Gtk.Label(
            xalign=0,
            hexpand=True,
            ellipsize=Pango.EllipsizeMode.END,
            single_line_mode=True,
        )
        self._context_meta.add_css_class("caption")
        self._context_meta.add_css_class("dimmed")
        jump_icon = Gtk.Image(icon_name="go-jump-symbolic")
        jump_icon.add_css_class("dimmed")
        context_box.append(context_icon)
        context_box.append(self._context_meta)
        context_box.append(jump_icon)
        self._context_button.set_child(context_box)
        self._context_revealer.set_child(self._context_button)

        self._transcript = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._transcript.set_margin_end(4)
        self._transcript.set_valign(Gtk.Align.START)
        self._scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._scroll.set_child(self._transcript)
        content.append(self._scroll)

        self._status = Adw.StatusPage(
            icon_name="chat-symbolic" if self._available else "network-offline-symbolic",
            title="讨论当前文档" if self._available else "OpenCode 不可用",
            description=(
                "询问当前章节，或选择文字以提供精确上下文"
                if self._available
                else "请安装并配置 OpenCode；文档阅读功能仍可正常使用"
            ),
        )
        self._transcript.append(self._status)

        composer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        composer.append(self._context_revealer)
        self._prompt_entry = Gtk.Entry(
            hexpand=True,
            sensitive=False,
            placeholder_text="询问这篇文档，或补充具体要求…",
        )
        self._prompt_entry.set_size_request(-1, 48)
        self._prompt_entry.add_css_class("ai-prompt")
        self._prompt_entry.add_css_class("ai-prompt-frame")
        self._prompt_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], ["AI 输入框"]
        )
        self._prompt_entry.connect("changed", self._on_prompt_changed)
        self._prompt_entry.connect("activate", self._on_send_requested)
        composer.append(self._prompt_entry)

        composer_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        composer_hint = Gtk.Label(
            label="Enter 发送",
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
        self._send_button.set_tooltip_text("发送消息")
        self._send_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["发送消息"]
        )
        self._send_button.connect("clicked", self._on_send_requested)
        composer_actions.append(self._send_button)

        self._cancel_button = Gtk.Button(
            icon_name="process-stop-symbolic",
            visible=False,
        )
        self._cancel_button.set_tooltip_text("停止回答")
        self._cancel_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["停止回答"]
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
        self._update_context_summary()
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
        self._assistant_history.clear()
        self._clear_box(self._transcript)
        self._status.set_icon_name("chat-symbolic")
        self._status.set_title(f"正在使用 {label}")
        self._status.set_description("使用此模型开始新对话")
        self._transcript.append(self._status)

    def set_selection(self, selection: DocumentSelection) -> None:
        self._selection = selection
        self._update_context_summary()
        self._update_mode_hint()
        self._update_composer()

    def _update_context_summary(self) -> None:
        selection = self._selection
        if selection.is_empty:
            self._context_revealer.set_reveal_child(False)
            return

        mode = "修改范围" if self._mode_group.get_active_name() == "edit" else "问答上下文"
        details = [mode]
        if selection.start_line > 0 and selection.end_line >= selection.start_line:
            line_count = selection.end_line - selection.start_line + 1
            details.append(f"已选 {line_count} 行")
        else:
            details.append(f"已选 {len(selection.text)} 个字符")
        if self._document:
            details.append(str(self._document))
        if selection.start_line > 0:
            details.append(f"第 {selection.start_line}–{selection.end_line} 行")
        if selection.heading_title:
            details.append(selection.heading_title)

        summary = " · ".join(details)
        self._context_meta.set_label(summary)
        self._context_button.set_tooltip_text(f"{summary}\n点击返回文档中的选中位置")
        self._context_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [summary]
        )
        self._context_revealer.set_reveal_child(True)

    def append_user(self, text: str) -> None:
        self._hide_status()
        self._transcript.append(self._message_block("你", text, "user-message"))
        self._scroll_to_bottom()

    def begin_assistant(self, *, edit_mode: bool = False) -> None:
        self._archive_current_assistant()
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
        spinner.update_property([Gtk.AccessibleProperty.LABEL], ["OpenCode 正在思考"])
        thinking_label = Gtk.Label(
            label="正在准备选中内容的修改…" if edit_mode else "正在思考…",
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
        self._prompt_entry.grab_focus()

    def popup_model_menu(self) -> None:
        self._model_button.popup()

    def refresh_theme(self, theme: ReaderTheme) -> None:
        self._theme = theme
        for content, text in self._assistant_history:
            self._render_markdown_into(content, text)
        if self._assistant_text and self._assistant_content is not None:
            self._render_markdown_into(self._assistant_content, self._assistant_text)
            self._last_rendered_text = self._assistant_text

    def _on_send_requested(self, _widget: Gtk.Widget) -> None:
        text = self._prompt_entry.get_text().strip()
        if not text or self._running or not self._document:
            return
        edit_mode = self._mode_group.get_active_name() == "edit"
        self._prompt_entry.set_text("")
        self._on_send(text, edit_mode)

    def _on_mode_changed(self, group: Adw.ToggleGroup, _param: object) -> None:
        edit_mode = group.get_active_name() == "edit"
        self._prompt_entry.set_placeholder_text(
            "选择文档文字，然后描述你想要的修改…"
            if edit_mode
            else "询问这篇文档，或补充具体要求…"
        )
        self._update_context_summary()
        self._update_mode_hint()
        self._update_composer()

    def _update_mode_hint(self) -> None:
        self._edit_banner.set_revealed(
            self._mode_group.get_active_name() == "edit"
            and self._selection.is_empty
        )

    def _on_prompt_changed(self, _entry: Gtk.Entry) -> None:
        self._update_send_button()

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
        if self._prompt_entry.get_sensitive() != enabled:
            self._prompt_entry.set_sensitive(enabled)
        self._update_send_button(enabled)
        self._mode_group.set_sensitive(enabled)
        self._edit_mode.set_enabled(enabled)
        self._model_button.set_sensitive(
            self._available and bool(self._models) and not self._running
        )

    def _update_send_button(self, enabled: bool | None = None) -> None:
        if enabled is None:
            enabled = (
                self._available
                and self._document is not None
                and not self._running
            )
        edit_ready = (
            self._mode_group.get_active_name() != "edit"
            or not self._selection.is_empty
        )
        self._send_button.set_sensitive(
            enabled
            and edit_ready
            and bool(self._prompt_entry.get_text())
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
        self._render_markdown_into(self._assistant_content, self._assistant_text)
        self._last_rendered_text = self._assistant_text
        self._scroll_to_bottom()
        return GLib.SOURCE_REMOVE

    def _render_markdown_into(self, content: Gtk.Box, text: str) -> None:
        self._clear_box(content)
        for block in self._markdown.render(text, theme=self._theme):
            content.append(self._markdown_block(block))

    def _archive_current_assistant(self) -> None:
        if self._assistant_content is None or not self._assistant_text:
            return
        if self._assistant_history and self._assistant_history[-1][0] is self._assistant_content:
            return
        self._assistant_history.append((self._assistant_content, self._assistant_text))

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
