from __future__ import annotations

import os
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

from mdreader.models import DocumentSelection, OutlineItem, RenderedDocument
from mdreader.services import (
    ContextBuilder,
    OpenCodeError,
    OpenCodeGateway,
    PatchError,
    PatchService,
    WorkspaceError,
    WorkspaceService,
    WorkspaceWatcher,
)
from mdreader.services.settings import SettingsStore
from mdreader.services.patches import PatchProposal
from mdreader.widgets import AiPanel, DocumentView, LibrarySidebar


@Gtk.Template(resource_path="/io/github/pang/mdreader/ui/window.ui")
class MdReaderWindow(Adw.ApplicationWindow):
    __gtype_name__ = "MdReaderWindow"

    toast_overlay = Gtk.Template.Child()
    toolbar_view = Gtk.Template.Child()
    header_bar = Gtk.Template.Child()
    files_button = Gtk.Template.Child()
    search_button = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    ai_button = Gtk.Template.Child()
    menu_button = Gtk.Template.Child()
    content_slot = Gtk.Template.Child()

    def __init__(self, *, settings: SettingsStore, initial_path: Path | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._settings = settings
        self._workspace: WorkspaceService | None = None
        self._watcher: WorkspaceWatcher | None = None
        self._current_relative_path: Path | None = None
        self._current_document_path: Path | None = None
        self._current_source: str | None = None
        self._selection = DocumentSelection()
        self._context_builder = ContextBuilder()
        self._patches: PatchService | None = None
        self._pending_edit = False
        self._pending_edit_selection = DocumentSelection()
        self._pending_edit_text = ""
        self._pending_edit_path: Path | None = None
        self._pending_edit_base_hash = ""
        self._opencode: OpenCodeGateway | None = None
        self._selected_model = OpenCodeGateway.normalize_model(
            settings.get_string("opencode-model")
        )
        self._available_models: tuple[str, ...] = ()
        self._model_load_generation = 0
        self._scan_generation = 0
        self._test_source_line = 0
        self._test_ctrl_wheel = False
        self._zoom = settings.get_int("document-zoom")
        self._zoom_save_source_id = 0

        self.set_default_size(
            settings.get_int("window-width"), settings.get_int("window-height")
        )
        if settings.get_boolean("window-maximized"):
            self.maximize()

        self.files_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Files and outline"]
        )
        self.search_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Find in document"]
        )
        self.ai_button.update_property([Gtk.AccessibleProperty.LABEL], ["AI assistant"])
        self.menu_button.update_property([Gtk.AccessibleProperty.LABEL], ["Main menu"])

        self._library = LibrarySidebar(self._on_document_selected)
        self._library.set_outline_callback(self._on_outline_selected)
        self._document = DocumentView()
        self._document.connect("selection-changed", self._on_selection_changed)
        self._document.connect("active-heading-changed", self._on_active_heading_changed)
        self._document.connect("document-presented", self._on_document_presented)
        self._document.connect("open-local-document", self._on_local_document)
        self._document.connect("zoom-requested", self._on_zoom_requested)
        self._ai = AiPanel(
            self._document.scroll_to_source,
            self._on_ai_send,
            self._on_ai_cancel,
            current_model=self._selected_model,
        )

        self._ai_split = Adw.OverlaySplitView(
            content=self._document,
            sidebar=self._ai,
            sidebar_position=Gtk.PackType.END,
            collapsed=False,
            pin_sidebar=True,
            show_sidebar=True,
            min_sidebar_width=320,
            max_sidebar_width=400,
            sidebar_width_fraction=0.28,
            enable_show_gesture=True,
            enable_hide_gesture=True,
        )
        self._ai_split.connect("notify::collapsed", self._sync_ai_button_state)
        self._ai_split.connect("notify::show-sidebar", self._sync_ai_button_state)
        self._library_split = Adw.OverlaySplitView(
            content=self._ai_split,
            sidebar=self._library,
            collapsed=False,
            pin_sidebar=True,
            show_sidebar=True,
            min_sidebar_width=230,
            max_sidebar_width=290,
            sidebar_width_fraction=0.20,
            enable_show_gesture=True,
            enable_hide_gesture=True,
        )
        self.content_slot.set_child(self._library_split)

        self._create_search_popover()
        self._setup_actions()
        self._setup_breakpoints()

        if initial_path:
            self.open_path(initial_path)
        else:
            last_workspace = settings.get_string("last-workspace")
            if last_workspace and Path(last_workspace).is_dir():
                self.open_workspace(Path(last_workspace))

    def do_close_request(self) -> bool:
        self._model_load_generation += 1
        self._flush_zoom_setting()
        if self._opencode is not None:
            self._opencode.close()
        if self._watcher is not None:
            self._watcher.close()
            self._watcher = None
        width, height = self.get_default_size()
        if not self.is_maximized():
            self._settings.set_int("window-width", max(360, width))
            self._settings.set_int("window-height", max(480, height))
        self._settings.set_boolean("window-maximized", self.is_maximized())
        return False

    def open_path(self, path: Path) -> None:
        path = path.expanduser().resolve(strict=False)
        if path.is_dir():
            self.open_workspace(path)
        elif path.is_file():
            self.open_workspace(path.parent, preferred_document=Path(path.name))
        else:
            self._show_toast(f"Path does not exist: {path}")

    def open_workspace(self, root: Path, preferred_document: Path | None = None) -> None:
        self._scan_generation += 1
        generation = self._scan_generation
        self._library.show_loading()
        self.title_label.set_label(root.name or str(root))

        def worker() -> None:
            try:
                workspace = WorkspaceService(root)
                entries = workspace.scan()
            except Exception as error:
                GLib.idle_add(self._finish_workspace_error, generation, error)
                return
            GLib.idle_add(
                self._finish_workspace_scan,
                generation,
                workspace,
                entries,
                preferred_document,
            )

        threading.Thread(target=worker, name="mdreader-workspace-scan", daemon=True).start()

    def _create_search_popover(self) -> None:
        self._search_entry = Gtk.SearchEntry(
            width_chars=28,
            placeholder_text="Find in document",
        )
        self._search_entry.connect("search-changed", self._on_search_changed)
        search_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        search_box.set_margin_start(10)
        search_box.set_margin_end(10)
        search_box.set_margin_top(10)
        search_box.set_margin_bottom(10)
        search_box.append(self._search_entry)
        popover = Gtk.Popover(child=search_box)
        self.search_button.set_popover(popover)

    def _setup_actions(self) -> None:
        actions = {
            "open-folder": self._on_open_folder,
            "toggle-library": self._on_toggle_library,
            "toggle-ai": self._on_toggle_ai,
            "hide-ai": self._on_hide_ai,
            "find": self._on_find,
            "zoom-in": lambda *_: self._change_zoom(10),
            "zoom-out": lambda *_: self._change_zoom(-10),
            "zoom-reset": lambda *_: self._set_zoom(100),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        self._undo_action = Gio.SimpleAction.new("undo-ai-change", None)
        self._undo_action.connect("activate", self._on_patch_undo_action)
        self._undo_action.set_enabled(False)
        self.add_action(self._undo_action)

        self._model_action = Gio.SimpleAction.new_stateful(
            "select-model",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(self._selected_model),
        )
        self._model_action.connect("activate", self._on_model_selected)
        self._model_action.set_enabled(False)
        self.add_action(self._model_action)

    def _setup_breakpoints(self) -> None:
        standard = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1120sp"))
        standard.add_setter(self._library_split, "collapsed", True)
        standard.add_setter(self._library_split, "pin-sidebar", False)
        standard.add_setter(self._library_split, "show-sidebar", False)
        standard.add_setter(self._ai_split, "collapsed", True)
        standard.add_setter(self._ai_split, "pin-sidebar", False)
        standard.add_setter(self._ai_split, "show-sidebar", False)
        standard.add_setter(self.files_button, "visible", True)
        self.add_breakpoint(standard)

        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        # Adw activates the narrowest matching breakpoint, so this mode must be
        # self-contained rather than relying on the 1120sp setters above.
        compact.add_setter(self._library_split, "collapsed", True)
        compact.add_setter(self._library_split, "pin-sidebar", False)
        compact.add_setter(self._library_split, "show-sidebar", False)
        compact.add_setter(self._ai_split, "collapsed", True)
        compact.add_setter(self._ai_split, "pin-sidebar", False)
        compact.add_setter(self._ai_split, "show-sidebar", False)
        compact.add_setter(self.files_button, "visible", True)
        compact.add_setter(self._library_split, "max-sidebar-width", 520.0)
        compact.add_setter(self._library_split, "sidebar-width-fraction", 0.90)
        compact.add_setter(self._ai_split, "max-sidebar-width", 600.0)
        compact.add_setter(self._ai_split, "sidebar-width-fraction", 0.94)
        self.add_breakpoint(compact)

        # Wide mode starts with both panes pinned; breakpoints reveal their buttons.
        self.files_button.set_visible(False)
        self._sync_ai_button_state()

    def _on_open_folder(self, _action: Gio.SimpleAction, _parameter: object) -> None:
        dialog = Gtk.FileDialog(title="Open Markdown Folder", modal=True)
        dialog.select_folder(self, None, self._on_folder_selected)

    def _on_folder_selected(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = folder.get_path()
        if path:
            self.open_workspace(Path(path))

    def _on_toggle_library(self, _action: Gio.SimpleAction, _parameter: object) -> None:
        self._library_split.set_show_sidebar(not self._library_split.get_show_sidebar())

    def _on_toggle_ai(self, _action: Gio.SimpleAction, _parameter: object) -> None:
        showing = not self._ai_split.get_show_sidebar()
        self._ai_split.set_show_sidebar(showing)
        if showing:
            self._ai.focus_composer()

    def _on_hide_ai(self, _action: Gio.SimpleAction, _parameter: object) -> None:
        self._ai_split.set_show_sidebar(False)

    def _sync_ai_button_state(self, *_args: object) -> None:
        self.ai_button.set_visible(
            self._ai_split.get_collapsed() or not self._ai_split.get_show_sidebar()
        )

    def _on_find(self, _action: Gio.SimpleAction, _parameter: object) -> None:
        self.search_button.popup()
        GLib.idle_add(self._focus_search_entry)

    def _focus_search_entry(self) -> bool:
        self._search_entry.grab_focus()
        return GLib.SOURCE_REMOVE

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._document.find(entry.get_text())

    def _finish_workspace_scan(
        self,
        generation: int,
        workspace: WorkspaceService,
        entries: tuple,
        preferred_document: Path | None,
    ) -> bool:
        if generation != self._scan_generation:
            return GLib.SOURCE_REMOVE
        previous_root = self._workspace.root if self._workspace is not None else None
        changing_workspace = previous_root != workspace.root
        if changing_workspace and self._opencode is not None:
            self._opencode.close()
            self._opencode = None
        self._workspace = workspace
        if changing_workspace or self._patches is None:
            self._patches = PatchService(workspace.root)
            self._undo_action.set_enabled(False)
        if self._opencode is None:
            if os.environ.get("MDREADER_TEST_OPENCODE_MISSING") != "1":
                try:
                    self._opencode = OpenCodeGateway(
                        workspace.root,
                        model=self._selected_model,
                    )
                except OpenCodeError:
                    self._opencode = None
        if self._opencode is None:
            self._model_action.set_enabled(False)
            self._ai.set_model_options((), self._selected_model)
        elif self._available_models:
            self._apply_model_options(self._opencode, self._available_models)
        else:
            self._load_model_options(self._opencode)
        if self._watcher is not None:
            self._watcher.close()
        self._watcher = WorkspaceWatcher(workspace.root, self._on_workspace_changed)
        self._settings.set_string("last-workspace", str(workspace.root))
        self._library.set_entries(entries)
        self.title_label.set_label(workspace.root.name)

        relative = preferred_document
        if relative is None:
            stored = self._settings.get_string("last-document")
            if stored:
                relative = Path(stored)
        if relative is None or not self._is_valid_relative_document(relative):
            relative = self._first_document(entries)
        if relative is not None:
            self._on_document_selected(relative)
        return GLib.SOURCE_REMOVE

    def _load_model_options(self, gateway: OpenCodeGateway) -> None:
        self._model_load_generation += 1
        generation = self._model_load_generation
        self._model_action.set_enabled(False)

        def worker() -> None:
            try:
                models = gateway.available_models()
            except OpenCodeError as error:
                GLib.idle_add(
                    self._finish_model_options_error,
                    generation,
                    gateway,
                    error,
                )
                return
            GLib.idle_add(
                self._finish_model_options,
                generation,
                gateway,
                models,
            )

        threading.Thread(
            target=worker,
            name="mdreader-opencode-models",
            daemon=True,
        ).start()

    def _finish_model_options(
        self,
        generation: int,
        gateway: OpenCodeGateway,
        models: tuple[str, ...],
    ) -> bool:
        if generation != self._model_load_generation or gateway is not self._opencode:
            return GLib.SOURCE_REMOVE
        self._available_models = models
        self._apply_model_options(gateway, models)
        if os.environ.pop("MDREADER_TEST_MODEL_MENU", "") == "1":
            self._ai_split.set_show_sidebar(True)
            GLib.timeout_add(250, self._popup_model_menu_smoke)
        return GLib.SOURCE_REMOVE

    def _popup_model_menu_smoke(self) -> bool:
        if not self.is_active():
            return GLib.SOURCE_CONTINUE
        self._ai.popup_model_menu()
        return GLib.SOURCE_REMOVE

    def _finish_model_options_error(
        self,
        generation: int,
        gateway: OpenCodeGateway,
        _error: OpenCodeError,
    ) -> bool:
        if generation != self._model_load_generation or gateway is not self._opencode:
            return GLib.SOURCE_REMOVE
        self._model_action.set_enabled(False)
        self._ai.set_model_options((), gateway.model)
        return GLib.SOURCE_REMOVE

    def _apply_model_options(
        self,
        gateway: OpenCodeGateway,
        models: tuple[str, ...],
    ) -> None:
        selected = self._selected_model
        if selected not in models:
            selected = (
                OpenCodeGateway.DEFAULT_MODEL
                if OpenCodeGateway.DEFAULT_MODEL in models
                else models[0]
            )
            gateway.set_model(selected)
            self._selected_model = selected
            self._settings.set_string("opencode-model", selected)
        self._model_action.set_state(GLib.Variant.new_string(selected))
        self._model_action.set_enabled(True)
        self._ai.set_model_options(models, selected)

    def _on_model_selected(
        self,
        action: Gio.SimpleAction,
        parameter: GLib.Variant | None,
    ) -> None:
        if parameter is None or self._opencode is None:
            return
        model = parameter.get_string()
        if model not in self._available_models:
            self._show_toast("That OpenCode model is no longer available")
            return
        if model == self._selected_model:
            action.set_state(parameter)
            return
        try:
            self._opencode.set_model(model)
        except OpenCodeError as error:
            self._show_toast(str(error))
            return
        self._selected_model = model
        self._settings.set_string("opencode-model", model)
        action.set_state(parameter)
        self._ai.set_current_model(model, new_conversation=True)

    def _on_workspace_changed(self) -> None:
        if self._workspace is None:
            return
        self.open_workspace(self._workspace.root, preferred_document=self._current_relative_path)

    def _finish_workspace_error(self, generation: int, error: Exception) -> bool:
        if generation != self._scan_generation:
            return GLib.SOURCE_REMOVE
        self._library.show_workspace_error(str(error))
        self._show_toast("Could not open folder")
        return GLib.SOURCE_REMOVE

    def _on_document_selected(self, relative_path: Path) -> None:
        if self._workspace is None:
            return
        try:
            document_path = self._workspace.validate_document(relative_path)
        except WorkspaceError as error:
            self._show_toast(str(error))
            return

        self._current_relative_path = relative_path
        self._current_document_path = document_path
        self._current_source = None
        self._settings.set_string("last-document", str(relative_path))
        self.title_label.set_label(document_path.name)
        self._ai.set_document(relative_path)
        self._selection = DocumentSelection()
        self._ai.set_selection(self._selection)
        self._library.set_outline(())
        if self._library_split.get_collapsed():
            self._library_split.set_show_sidebar(False)

        self._document.load_document(
            document_path,
            zoom=self._zoom,
            workspace_root=self._workspace.root,
            on_loaded=self._on_document_loaded,
            on_error=self._on_document_error,
        )

    def _on_document_loaded(self, rendered: RenderedDocument) -> None:
        self._current_source = rendered.source
        self._library.set_outline(rendered.outline)
        preview_ai = os.environ.pop("MDREADER_TEST_AI_PREVIEW", "") == "1"
        preview_thinking = os.environ.pop("MDREADER_TEST_AI_THINKING", "") == "1"
        if preview_ai or preview_thinking:
            GLib.timeout_add(250, self._show_ai_preview_smoke, preview_thinking)
        test_source_line = os.environ.pop("MDREADER_TEST_SOURCE_LINE", "").strip()
        if test_source_line.isdigit():
            self._test_source_line = int(test_source_line)
        test_heading = os.environ.pop("MDREADER_TEST_HEADING", "").strip()
        if test_heading:
            self._library.show_outline()
            self._library_split.set_show_sidebar(True)
            GLib.timeout_add(400, self._scroll_smoke_heading, test_heading)
        question = os.environ.pop("MDREADER_TEST_QUESTION", "").strip()
        if question:
            GLib.timeout_add(400, self._send_smoke_question, question)

    def _show_ai_preview_smoke(self, thinking_only: bool) -> bool:
        self._ai_split.set_show_sidebar(True)
        self._ai.append_user("为什么这么多人选择手动安装？")
        self._ai.begin_assistant()
        if not thinking_only:
            self._ai.append_assistant_text(
                """## 为什么保留手动安装

`archinstall` 适合快速进入系统；手动安装更适合以下情况：

- **分区更灵活**：自定义子卷、加密、LVM 和独立 `/var`
- **更容易排错**：逐步理解 `pacstrap`、`chroot` 与引导器

| 方式 | 更适合 |
| --- | --- |
| archinstall | 常规安装与快速部署 |
| 手动安装 | 特殊布局和学习过程 |

> 文档同时给出两条路线是合理的，选择取决于目标。

```bash
pacstrap -K /mnt base linux linux-firmware
```
"""
            )
            self._ai.finish_assistant()
        return GLib.SOURCE_REMOVE

    def _scroll_smoke_heading(self, slug: str) -> bool:
        self._document.scroll_to_heading(slug)
        return GLib.SOURCE_REMOVE

    def _scroll_smoke_source(self, line: int) -> bool:
        self._document.scroll_to_source(line)
        return GLib.SOURCE_REMOVE

    def _send_smoke_question(self, question: str) -> bool:
        edit_mode = os.environ.pop("MDREADER_TEST_EDIT", "") == "1"
        self._on_ai_send(question, edit_mode)
        return GLib.SOURCE_REMOVE

    def _on_document_error(self, error: Exception) -> None:
        self._show_toast(f"Could not render document: {error}")

    def _on_outline_selected(self, item: OutlineItem) -> None:
        self._document.scroll_to_heading(item.slug)
        if self._library_split.get_collapsed():
            self._library_split.set_show_sidebar(False)

    def _on_active_heading_changed(self, _view: DocumentView, slug: str) -> None:
        self._library.set_active_outline(slug)

    def _on_document_presented(self, _view: DocumentView) -> None:
        if self._test_source_line:
            source_line = self._test_source_line
            self._test_source_line = 0
            GLib.timeout_add(200, self._scroll_smoke_source, source_line)
        if os.environ.pop("MDREADER_TEST_CTRL_WHEEL", "") == "1":
            self._test_ctrl_wheel = True
            self._document.dispatch_ctrl_wheel_for_test()
        if os.environ.pop("MDREADER_TEST_QUIT_ON_PRESENT", "") == "1":
            GLib.timeout_add(
                600 if self._test_ctrl_wheel else 200,
                self._quit_presented_smoke,
            )

    def _quit_presented_smoke(self) -> bool:
        application = self.get_application()
        if application is not None:
            application.quit()
        return GLib.SOURCE_REMOVE

    def _on_selection_changed(self, _view: DocumentView, selection: DocumentSelection) -> None:
        self._selection = selection
        self._ai.set_selection(selection)
        if selection.is_empty:
            self.ai_button.remove_css_class("accent")
        else:
            self.ai_button.add_css_class("accent")

    def _on_ai_send(self, question: str, edit_mode: bool = False) -> None:
        if (
            self._workspace is None
            or self._opencode is None
            or self._current_document_path is None
            or self._current_relative_path is None
        ):
            self._ai.show_error("Open a document before starting a conversation")
            return
        if self._current_source is None:
            self._ai.show_error("Wait for the document to finish rendering")
            return
        if edit_mode and self._selection.is_empty:
            self._ai.show_error("Select the lines to change before proposing an edit")
            return
        try:
            context = self._context_builder.from_source(
                self._current_source,
                self._current_relative_path,
                self._selection,
            )
            effective_question = (
                "EDIT REQUEST: Replace exactly the selected source lines according to this request: "
                + question
                if edit_mode
                else question
            )
            prompt = self._context_builder.prompt(effective_question, context)
            self._pending_edit = edit_mode
            self._pending_edit_selection = self._selection
            self._pending_edit_text = ""
            self._pending_edit_path = self._current_document_path if edit_mode else None
            self._pending_edit_base_hash = context.source_hash if edit_mode else ""
            self._ai.append_user(question)
            self._ai.begin_assistant(edit_mode=edit_mode)
            self._opencode.send(
                prompt,
                on_text=self._on_ai_text,
                on_done=self._on_ai_done,
                on_error=self._on_ai_error,
            )
        except (OSError, OpenCodeError) as error:
            self._clear_pending_edit()
            self._ai.show_error(str(error))

    def _on_ai_text(self, text: str) -> bool:
        if self._pending_edit:
            self._pending_edit_text += text
        else:
            self._ai.append_assistant_text(text)
        return GLib.SOURCE_REMOVE

    def _on_ai_done(self, _event: dict) -> bool:
        if self._pending_edit:
            self._finish_edit_proposal()
        else:
            self._ai.finish_assistant()
        return GLib.SOURCE_REMOVE

    def _on_ai_error(self, error: Exception) -> bool:
        self._ai.show_error(str(error))
        self._clear_pending_edit()
        return GLib.SOURCE_REMOVE

    def _on_ai_cancel(self) -> None:
        if self._opencode is not None:
            self._opencode.cancel()

    def _finish_edit_proposal(self) -> None:
        selection = self._pending_edit_selection
        path = self._pending_edit_path
        base_hash = self._pending_edit_base_hash
        payload = self._pending_edit_text
        self._clear_pending_edit()
        if path is None or self._patches is None:
            self._ai.show_error("The source document is no longer available")
            return
        if self._current_document_path != path:
            self._ai.show_error("The document changed before the edit proposal was ready")
            return
        try:
            proposal = self._patches.propose(
                path,
                expected_start=selection.start_line,
                expected_end=selection.end_line,
                expected_base_hash=base_hash,
                payload=payload,
            )
        except (OSError, PatchError) as error:
            self._ai.show_error(str(error))
            return
        self._ai.finish_assistant("Change ready for review")
        self._show_patch_dialog(proposal)

    def _clear_pending_edit(self) -> None:
        self._pending_edit = False
        self._pending_edit_selection = DocumentSelection()
        self._pending_edit_text = ""
        self._pending_edit_path = None
        self._pending_edit_base_hash = ""

    def _show_patch_dialog(self, proposal: PatchProposal) -> None:
        buffer = Gtk.TextBuffer(text=proposal.diff)
        view = Gtk.TextView(
            buffer=buffer,
            editable=False,
            cursor_visible=False,
            monospace=True,
            wrap_mode=Gtk.WrapMode.NONE,
        )
        scrolled = Gtk.ScrolledWindow(
            min_content_height=280,
            max_content_height=560,
            min_content_width=320,
            max_content_width=760,
            propagate_natural_height=True,
            propagate_natural_width=True,
        )
        scrolled.set_child(view)
        dialog = Adw.AlertDialog.new(
            "Apply AI change?",
            f"Only lines {proposal.start_line}–{proposal.end_line} of {proposal.path.name} will change.",
        )
        dialog.set_extra_child(scrolled)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply Change")
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_patch_response, proposal)
        dialog.present(self)

    def _on_patch_response(
        self, _dialog: Adw.AlertDialog, response: str, proposal: PatchProposal
    ) -> None:
        if response != "apply":
            return
        if self._patches is None:
            self._show_toast("The open workspace changed; the proposal was not applied")
            return
        try:
            self._patches.apply(proposal)
        except (OSError, PatchError) as error:
            self._show_toast(str(error))
            return
        toast = Adw.Toast(title="AI change applied", button_label="Undo")
        toast.connect("button-clicked", self._on_patch_undo)
        self.toast_overlay.add_toast(toast)
        self._undo_action.set_enabled(True)

    def _on_patch_undo(self, _toast: Adw.Toast) -> None:
        self._undo_last_patch()

    def _on_patch_undo_action(
        self, _action: Gio.SimpleAction, _parameter: object
    ) -> None:
        self._undo_last_patch()

    def _undo_last_patch(self) -> None:
        if self._patches is None:
            return
        try:
            if self._patches.undo():
                self._undo_action.set_enabled(False)
                self._show_toast("AI change undone")
        except (OSError, PatchError) as error:
            self._undo_action.set_enabled(self._patches.can_undo)
            self._show_toast(str(error))

    def _on_local_document(self, _view: DocumentView, uri: str) -> None:
        if self._workspace is None:
            return
        path = Path(unquote(urlparse(uri).path))
        try:
            relative = self._workspace.relative_path(path)
            self._workspace.validate_document(relative)
        except WorkspaceError:
            self._show_toast("Linked document is outside the open workspace")
            return
        self._on_document_selected(relative)

    def _change_zoom(self, amount: int) -> None:
        self._set_zoom(self._zoom + amount)

    def _on_zoom_requested(
        self, _view: DocumentView, zoom: int, _anchor_y: float
    ) -> None:
        self._set_zoom(
            zoom,
            update_document=False,
            announce=False,
            defer_persist=True,
        )
        if self._test_ctrl_wheel and self._zoom == 105:
            self._test_ctrl_wheel = False
            print(f"MDREADER_TEST_CTRL_WHEEL_OK={self._zoom}", flush=True)

    def _set_zoom(
        self,
        zoom: int,
        *,
        update_document: bool = True,
        announce: bool = True,
        defer_persist: bool = False,
    ) -> None:
        self._zoom = max(75, min(200, zoom))
        if defer_persist:
            if self._zoom_save_source_id:
                GLib.source_remove(self._zoom_save_source_id)
            self._zoom_save_source_id = GLib.timeout_add(
                180, self._save_zoom_setting
            )
        else:
            self._flush_zoom_setting()
        if update_document:
            self._document.set_zoom(self._zoom)
        if announce:
            self._show_toast(f"Document zoom: {self._zoom}%")

    def _save_zoom_setting(self) -> bool:
        self._zoom_save_source_id = 0
        self._settings.set_int("document-zoom", self._zoom)
        return GLib.SOURCE_REMOVE

    def _flush_zoom_setting(self) -> None:
        if self._zoom_save_source_id:
            GLib.source_remove(self._zoom_save_source_id)
            self._zoom_save_source_id = 0
        self._settings.set_int("document-zoom", self._zoom)

    def _show_toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message, timeout=3))

    def _is_valid_relative_document(self, relative: Path) -> bool:
        if self._workspace is None:
            return False
        try:
            self._workspace.validate_document(relative)
        except WorkspaceError:
            return False
        return True

    @classmethod
    def _first_document(cls, entries: tuple) -> Path | None:
        for entry in entries:
            if entry.is_directory:
                nested = cls._first_document(entry.children)
                if nested is not None:
                    return nested
            else:
                return entry.relative_path
        return None
