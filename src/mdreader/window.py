from __future__ import annotations

import os
import threading
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

from mdreader.models import DocumentSelection, OutlineItem, RenderedDocument
from mdreader.services import (
    ContextBuilder,
    DocumentSnapshot,
    LocalDocumentLinkError,
    OpenCodeError,
    OpenCodeGateway,
    PatchError,
    PatchService,
    ScanCoordinator,
    WorkspaceError,
    WorkspaceService,
    WorkspaceSnapshot,
    WorkspaceWatcher,
    apply_color_scheme,
    get_theme,
    normalize_theme_id,
    parse_local_document_uri,
)
from mdreader.services.restore import (
    RestoreRequest,
    make_restore,
    resolve_presented_action,
)
from mdreader.services.settings import SettingsStore
from mdreader.services.patches import PatchProposal
from mdreader.widgets import AiPanel, DocumentView, LibrarySidebar, SidebarResizeHandle


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
        theme_id = normalize_theme_id(settings.get_string("color-scheme"))
        self._theme = get_theme(theme_id)
        self._settings.set_string("color-scheme", theme_id)
        apply_color_scheme(self._theme)
        self.add_css_class(self._theme.css_class)
        self._workspace: WorkspaceService | None = None
        self._watcher: WorkspaceWatcher | None = None
        self._watcher_unavailable_reported = False
        self._closed = False
        self._current_relative_path: Path | None = None
        self._current_document_path: Path | None = None
        self._current_document_identity: tuple[int, int] | None = None
        self._current_document_fingerprint: tuple[int, int, int, int] | None = None
        self._current_source: str | None = None
        self._pending_fragment: tuple[Path, str] | None = None
        self._pending_restore: RestoreRequest | None = None
        self._active_heading_slug = ""
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
        self._ai_request_generation = 0
        self._test_source_line = 0
        self._test_ctrl_wheel = False
        self._zoom = settings.get_int("document-zoom")
        self._zoom_save_source_id = 0
        self._sidebar_resize_specs: dict[str, dict[str, object]] = {}
        self._sidebar_resize_state: dict[str, int] = {}
        self._sidebar_resize_handles: dict[str, SidebarResizeHandle] = {}
        self._sidebar_resize_apply_sources: dict[str, int] = {}
        self._library_sidebar_width = settings.get_sidebar_width(
            "library-sidebar-width", 260
        )
        self._ai_sidebar_width = settings.get_sidebar_width(
            "ai-sidebar-width", 360
        )

        self.set_default_size(
            settings.get_int("window-width"), settings.get_int("window-height")
        )
        if settings.get_boolean("window-maximized"):
            self.maximize()

        self.files_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["文件与大纲"]
        )
        self.search_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["在文档中查找"]
        )
        self.ai_button.update_property([Gtk.AccessibleProperty.LABEL], ["AI 助手"])
        self.menu_button.update_property([Gtk.AccessibleProperty.LABEL], ["主菜单"])

        self._library = LibrarySidebar(self._on_document_selected)
        self._library.set_outline_callback(self._on_outline_selected)
        self._library_sidebar = self._make_resizable_sidebar(
            self._library, "start", "library"
        )
        self._document = DocumentView(theme=self._theme)
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
            theme=self._theme,
        )
        self._ai_sidebar = self._make_resizable_sidebar(self._ai, "end", "ai")

        self._ai_split = Adw.OverlaySplitView(
            content=self._document,
            sidebar=self._ai_sidebar,
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
            sidebar=self._library_sidebar,
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
        self._register_sidebar_resizers()
        self.connect("realize", self._on_window_realized)

        self._create_search_popover()
        self._setup_actions()
        self._setup_breakpoints()

        if initial_path:
            self.open_path(initial_path)
        else:
            self._restore_last_session()

    def do_close_request(self) -> bool:
        if self._closed:
            return False
        self._closed = True
        self._scan_generation += 1
        self._model_load_generation += 1
        self._ai_request_generation += 1
        self._document.invalidate()
        self._document.close()
        self._ai.close()
        self._flush_zoom_setting()
        for source_id in self._sidebar_resize_apply_sources.values():
            GLib.source_remove(source_id)
        self._sidebar_resize_apply_sources.clear()
        if self._opencode is not None:
            self._opencode.close()
            self._opencode = None
        if self._watcher is not None:
            self._watcher.close()
            self._watcher = None
        width, height = self.get_default_size()
        if not self.is_maximized():
            self._settings.set_int("window-width", max(360, width))
            self._settings.set_int("window-height", max(480, height))
        self._settings.set_boolean("window-maximized", self.is_maximized())
        return False

    def _restore_last_session(self) -> None:
        last_workspace = self._settings.get_string("last-workspace")
        if not last_workspace:
            return
        root = Path(last_workspace)
        if not root.is_dir():
            return

        stored_document = self._settings.get_string("last-document")
        if stored_document:
            try:
                workspace = WorkspaceService(root)
                document = workspace.validate_document(Path(stored_document))
            except WorkspaceError:
                pass
            else:
                self.open_document(document, workspace_root=root)
                return
        self.open_workspace(root)

    def open_path(self, path: Path) -> None:
        if self._closed:
            return
        path = path.expanduser().resolve(strict=False)
        if path.is_dir():
            self.open_workspace(path)
        elif path.is_file():
            self.open_document(path)
        else:
            self._show_toast(f"路径不存在：{path}")

    def open_document(
        self, path: Path, *, workspace_root: Path | None = None
    ) -> None:
        if self._closed:
            return
        path = path.expanduser().resolve(strict=False)
        root = workspace_root if workspace_root is not None else path.parent
        try:
            workspace = WorkspaceService(root)
            relative = workspace.relative_path(path)
            workspace.validate_document(relative)
        except WorkspaceError as error:
            self._show_toast(f"无法打开文档：{error}")
            return

        self._scan_generation += 1
        generation = self._scan_generation
        self._library.show_loading()
        self._activate_workspace(workspace, install_watcher=False)
        if self._watcher is None:
            # A root-only monitor starts immediately so changes that arrive
            # while the recursive parent scan runs are not lost; the finished
            # scan then extends monitoring to every discovered directory.
            self._install_workspace_watcher(workspace, (workspace.root,))
        previous_watcher = self._watcher
        previous_serial = (
            previous_watcher.change_serial if previous_watcher is not None else 0
        )
        # Reading the requested file must not wait for a recursive scan of its
        # parent directory. The tree is populated independently below.
        self._on_document_selected(relative)

        def worker() -> None:
            try:
                snapshot = workspace.scan_snapshot()
            except Exception as error:
                GLib.idle_add(
                    self._finish_direct_document_scan_error,
                    generation,
                    workspace,
                    error,
                    previous_watcher,
                    previous_serial,
                )
                return
            GLib.idle_add(
                self._finish_direct_document_scan,
                generation,
                workspace,
                snapshot,
                previous_watcher,
                previous_serial,
            )

        threading.Thread(
            target=worker,
            name="mdreader-document-workspace-scan",
            daemon=True,
        ).start()

    def open_workspace(self, root: Path, preferred_document: Path | None = None) -> None:
        if self._closed:
            return
        try:
            workspace = WorkspaceService(root)
        except WorkspaceError as error:
            self._show_toast(f"无法打开文件夹：{error}")
            return

        self._scan_generation += 1
        generation = self._scan_generation
        self._library.show_loading()
        self.title_label.set_label(workspace.root.name or str(workspace.root))
        changing_workspace = (
            self._workspace is None or self._workspace.root != workspace.root
        )
        self._activate_workspace(workspace, install_watcher=False)
        if self._watcher is None:
            self._install_workspace_watcher(workspace, (workspace.root,))
        previous_watcher = self._watcher
        previous_serial = (
            previous_watcher.change_serial if previous_watcher is not None else 0
        )
        if changing_workspace:
            self._clear_current_document()

        def worker() -> None:
            try:
                snapshot = workspace.scan_snapshot()
            except Exception as error:
                GLib.idle_add(
                    self._finish_workspace_error,
                    generation,
                    workspace,
                    error,
                )
                return
            GLib.idle_add(
                self._finish_workspace_scan,
                generation,
                workspace,
                snapshot,
                preferred_document,
                previous_watcher,
                previous_serial,
            )

        threading.Thread(target=worker, name="mdreader-workspace-scan", daemon=True).start()

    def _create_search_popover(self) -> None:
        self._search_entry = Gtk.SearchEntry(
            width_chars=28,
            placeholder_text="在文档中查找",
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
            "open-document": self._on_open_document,
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

        self._theme_action = Gio.SimpleAction.new_stateful(
            "select-theme",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(self._theme.id),
        )
        self._theme_action.connect("activate", self._on_theme_selected)
        self.add_action(self._theme_action)

        self._model_action = Gio.SimpleAction.new_stateful(
            "select-model",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(self._selected_model),
        )
        self._model_action.connect("activate", self._on_model_selected)
        self._model_action.set_enabled(False)
        self.add_action(self._model_action)

    def _make_resizable_sidebar(
        self, child: Gtk.Widget, edge: str, name: str
    ) -> Gtk.Overlay:
        overlay = Gtk.Overlay()
        overlay.set_child(child)
        handle = SidebarResizeHandle(
            lambda: self._begin_sidebar_resize(name),
            lambda delta: self._update_sidebar_resize(name, delta),
            lambda: self._finish_sidebar_resize(name),
            tooltip=(
                "拖动调整目录宽度"
                if name == "library"
                else "拖动调整 AI 助手宽度"
            ),
        )
        # The handle belongs on the edge facing the document: the library
        # sidebar is at the start but resizes from its end, while the AI
        # sidebar is at the end but resizes from its start.
        handle.set_halign(Gtk.Align.END if edge == "start" else Gtk.Align.START)
        handle.set_valign(Gtk.Align.FILL)
        overlay.add_overlay(handle)
        self._sidebar_resize_handles[name] = handle
        return overlay

    def _register_sidebar_resizers(self) -> None:
        self._sidebar_resize_specs = {
            "library": {
                "split": self._library_split,
                "sidebar": self._library_sidebar,
                "edge": "start",
                "setting": "library-sidebar-width",
                "preferred": self._library_sidebar_width,
                "minimum": 230,
                "maximum": 520,
                "minimum_content": 640,
            },
            "ai": {
                "split": self._ai_split,
                "sidebar": self._ai_sidebar,
                "edge": "end",
                "setting": "ai-sidebar-width",
                "preferred": self._ai_sidebar_width,
                "minimum": 320,
                "maximum": 680,
                "minimum_content": 640,
            },
        }
        for name, spec in self._sidebar_resize_specs.items():
            split = spec["split"]
            assert isinstance(split, Adw.OverlaySplitView)
            split.set_sidebar_width_unit(Adw.LengthUnit.PX)
            self._apply_sidebar_width(name)
            split.connect("notify::width", self._on_sidebar_split_width_changed, name)
            split.connect("notify::collapsed", self._on_sidebar_split_state_changed, name)
            split.connect("notify::show-sidebar", self._on_sidebar_split_state_changed, name)
            split.connect("notify::pin-sidebar", self._on_sidebar_split_state_changed, name)
            self._update_sidebar_handle_visibility(name)

    def _apply_sidebar_width(self, name: str) -> None:
        if name not in self._sidebar_resize_specs:
            return
        specs = self._sidebar_resize_specs
        library_split = specs["library"]["split"]
        ai_split = specs["ai"]["split"]
        assert isinstance(library_split, Adw.OverlaySplitView)
        assert isinstance(ai_split, Adw.OverlaySplitView)

        if library_split.get_collapsed() or ai_split.get_collapsed():
            # In overlay mode the breakpoint owns the drawer width. The mouse
            # handle is hidden, so do not replace the adaptive fraction with a
            # fixed width while moving between Niri presets.
            return

        available_width = self._available_window_width()
        if available_width <= 0:
            # Do not install a large persisted width before the first
            # allocation. Breakpoints must be able to restore their safe
            # defaults without a one-frame nested-sidebar overflow.
            return

        targets = self._sidebar_targets(available_width)
        for pane_name, target in targets.items():
            split = specs[pane_name]["split"]
            assert isinstance(split, Adw.OverlaySplitView)
            split.set_min_sidebar_width(float(target))
            split.set_max_sidebar_width(float(target))

    def _sidebar_targets(self, available_width: int) -> dict[str, int]:
        """Return pane widths that leave the document its minimum reading area.

        The sidebars are nested OverlaySplitViews, so each split can otherwise
        see a stale or over-allocated child width while a Niri tile changes.
        Calculate both widths from the window as one budget instead of letting
        the second pane consume the document area after the first pane clamps.
        """
        library = self._sidebar_resize_specs["library"]
        ai = self._sidebar_resize_specs["ai"]
        library_min = int(library["minimum"])
        ai_min = int(ai["minimum"])
        library_max = int(library["maximum"])
        ai_max = int(ai["maximum"])
        minimum_reader = max(
            int(library["minimum_content"]), int(ai["minimum_content"])
        )

        if available_width <= 0:
            return {
                "library": max(library_min, min(library_max, int(library["preferred"]))),
                "ai": max(ai_min, min(ai_max, int(ai["preferred"]))),
            }

        # Reserve a readable document area before choosing either pane. This
        # makes a large persisted pair safe when a user returns from a 1920px
        # tile to a 1280px tile, while preserving both preferences whenever
        # they fit together.
        sidebar_budget = max(
            library_min + ai_min, available_width - minimum_reader
        )
        effective_library_max = min(
            library_max, max(library_min, sidebar_budget - ai_min)
        )
        library_target = max(
            library_min,
            min(effective_library_max, int(library["preferred"])),
        )

        ai_budget = sidebar_budget - library_target
        effective_ai_max = min(ai_max, max(ai_min, ai_budget))
        ai_target = max(ai_min, min(effective_ai_max, int(ai["preferred"])))
        return {"library": library_target, "ai": ai_target}

    def _available_window_width(self) -> int:
        surface = self.get_surface()
        if surface is not None:
            width = surface.get_width()
            if width > 0:
                return width
        width = self.get_width()
        if width > 0:
            return width
        return self.content_slot.get_allocated_width()

    def _on_sidebar_split_width_changed(
        self, split: Adw.OverlaySplitView, _param: object, name: str
    ) -> None:
        if split.get_collapsed() or name in self._sidebar_resize_state:
            return
        self._queue_sidebar_width_apply(name)

    def _on_window_realized(self, _window: Gtk.Window) -> None:
        surface = self.get_surface()
        if surface is None:
            return
        surface.connect("notify::width", self._on_window_surface_width_changed)
        for name in self._sidebar_resize_specs:
            self._queue_sidebar_width_apply(name)

    def _on_window_surface_width_changed(
        self, _surface: object, _param: object
    ) -> None:
        # The surface has already received Niri's new tile width, while child
        # allocations still reflect the previous frame. Clamp immediately so
        # a narrow tile cannot briefly lay out both persisted panes at their
        # wide widths; the delayed pass below then restores the preference
        # once nested allocations settle.
        if self._sidebar_resize_specs:
            self._apply_sidebar_width("library")
        for name in self._sidebar_resize_specs:
            self._queue_sidebar_width_apply(name)

    def _queue_sidebar_width_apply(self, name: str) -> None:
        if name in self._sidebar_resize_apply_sources:
            return
        # Width notifications arrive while GTK is still negotiating the
        # nested split allocations. Apply on the next settled frame rather
        # than reading the previous tile width and leaving a temporarily
        # clamped preference stuck after the window expands again.
        self._sidebar_resize_apply_sources[name] = GLib.timeout_add(
            32,
            self._apply_sidebar_width_idle, name
        )

    def _apply_sidebar_width_idle(self, name: str) -> bool:
        self._sidebar_resize_apply_sources.pop(name, None)
        self._apply_sidebar_width(name)
        return GLib.SOURCE_REMOVE

    def _on_sidebar_split_state_changed(
        self, _split: Adw.OverlaySplitView, _param: object, name: str
    ) -> None:
        self._update_sidebar_handle_visibility(name)
        if name not in self._sidebar_resize_state:
            self._queue_sidebar_width_apply(name)

    def _update_sidebar_handle_visibility(self, name: str) -> None:
        spec = self._sidebar_resize_specs.get(name)
        if not spec:
            return
        split = spec["split"]
        assert isinstance(split, Adw.OverlaySplitView)
        handle = self._sidebar_resize_handles.get(name)
        if handle is not None:
            handle.set_visible(split.get_show_sidebar() and not split.get_collapsed())

    def _begin_sidebar_resize(self, name: str) -> None:
        spec = self._sidebar_resize_specs.get(name)
        if not spec:
            return
        sidebar = spec["sidebar"]
        assert isinstance(sidebar, Gtk.Widget)
        self._sidebar_resize_state[name] = max(
            1, sidebar.get_allocated_width()
        )

    def _update_sidebar_resize(self, name: str, offset_x: float) -> None:
        spec = self._sidebar_resize_specs.get(name)
        start_width = self._sidebar_resize_state.get(name)
        if not spec or start_width is None:
            return
        split = spec["split"]
        assert isinstance(split, Adw.OverlaySplitView)
        direction = 1 if spec["edge"] == "start" else -1
        target = int(round(start_width + direction * offset_x))
        minimum = int(spec["minimum"])
        maximum = int(spec["maximum"])
        split_width = split.get_allocated_width()
        if split_width > 0:
            available_max = (
                int(split_width * 0.92)
                if split.get_collapsed()
                else split_width - int(spec["minimum_content"])
            )
            maximum = min(maximum, max(minimum, available_max))
        target = max(minimum, min(maximum, target))
        spec["preferred"] = target
        split.set_sidebar_width_unit(Adw.LengthUnit.PX)
        split.set_min_sidebar_width(float(target))
        split.set_max_sidebar_width(float(target))

    def _finish_sidebar_resize(self, name: str) -> None:
        spec = self._sidebar_resize_specs.get(name)
        if spec:
            preferred = int(spec["preferred"])
            self._settings.set_sidebar_width(str(spec["setting"]), preferred)
        self._sidebar_resize_state.pop(name, None)

    def _setup_breakpoints(self) -> None:
        standard = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1120sp"))
        standard.add_setter(self._library_split, "collapsed", True)
        standard.add_setter(self._library_split, "pin-sidebar", False)
        standard.add_setter(self._library_split, "show-sidebar", False)
        standard.add_setter(self._library_split, "min-sidebar-width", 230.0)
        standard.add_setter(self._library_split, "max-sidebar-width", 290.0)
        standard.add_setter(self._library_split, "sidebar-width-fraction", 0.20)
        standard.add_setter(self._ai_split, "collapsed", True)
        standard.add_setter(self._ai_split, "pin-sidebar", False)
        standard.add_setter(self._ai_split, "show-sidebar", False)
        standard.add_setter(self._ai_split, "min-sidebar-width", 320.0)
        standard.add_setter(self._ai_split, "max-sidebar-width", 400.0)
        standard.add_setter(self._ai_split, "sidebar-width-fraction", 0.28)
        standard.add_setter(self.files_button, "visible", True)
        self.add_breakpoint(standard)

        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        # Adw activates the narrowest matching breakpoint, so this mode must be
        # self-contained rather than relying on the 1120sp setters above.
        compact.add_setter(self._library_split, "collapsed", True)
        compact.add_setter(self._library_split, "pin-sidebar", False)
        compact.add_setter(self._library_split, "show-sidebar", False)
        compact.add_setter(self._library_split, "min-sidebar-width", 230.0)
        compact.add_setter(self._library_split, "max-sidebar-width", 520.0)
        compact.add_setter(self._library_split, "sidebar-width-fraction", 0.90)
        compact.add_setter(self._ai_split, "collapsed", True)
        compact.add_setter(self._ai_split, "pin-sidebar", False)
        compact.add_setter(self._ai_split, "show-sidebar", False)
        compact.add_setter(self._ai_split, "min-sidebar-width", 320.0)
        compact.add_setter(self._ai_split, "max-sidebar-width", 600.0)
        compact.add_setter(self._ai_split, "sidebar-width-fraction", 0.94)
        compact.add_setter(self.files_button, "visible", True)
        self.add_breakpoint(compact)

        # Wide mode starts with both panes pinned; breakpoints reveal their buttons.
        self.files_button.set_visible(False)
        self._sync_ai_button_state()

    def _on_open_document(
        self, _action: Gio.SimpleAction, _parameter: object
    ) -> None:
        markdown_filter = Gtk.FileFilter(name="Markdown 文档")
        for suffix in ("md", "markdown", "mdown", "mkd"):
            markdown_filter.add_suffix(suffix)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(markdown_filter)
        dialog = Gtk.FileDialog(
            title="打开 Markdown 文档",
            modal=True,
            filters=filters,
            default_filter=markdown_filter,
        )
        dialog.open(self, None, self._on_document_file_selected)

    def _on_document_file_selected(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        try:
            document = dialog.open_finish(result)
        except GLib.Error:
            return
        path = document.get_path()
        if path:
            self.open_path(Path(path))

    def _on_open_folder(self, _action: Gio.SimpleAction, _parameter: object) -> None:
        dialog = Gtk.FileDialog(title="打开 Markdown 文件夹", modal=True)
        dialog.select_folder(self, None, self._on_folder_selected)

    def _on_folder_selected(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = folder.get_path()
        if path:
            self.open_workspace(Path(path))

    def _on_theme_selected(
        self, action: Gio.SimpleAction, parameter: GLib.Variant | None
    ) -> None:
        if parameter is None:
            return
        theme_id = normalize_theme_id(parameter.get_string())
        theme = get_theme(theme_id)
        if theme.id == self._theme.id:
            action.set_state(GLib.Variant.new_string(theme.id))
            return
        self.remove_css_class(self._theme.css_class)
        self._theme = theme
        self.add_css_class(theme.css_class)
        apply_color_scheme(theme)
        self._document.set_theme(theme)
        self._ai.refresh_theme(theme)
        self._settings.set_string("color-scheme", theme.id)
        action.set_state(GLib.Variant.new_string(theme.id))
        self._show_toast(f"主题：{theme.name}")

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

    def _activate_workspace(
        self, workspace: WorkspaceService, *, install_watcher: bool
    ) -> None:
        if self._closed:
            return
        previous_root = self._workspace.root if self._workspace is not None else None
        changing_workspace = previous_root != workspace.root
        if changing_workspace and self._opencode is not None:
            self._opencode.close()
            self._opencode = None
        if changing_workspace:
            self._cancel_ai_request(reset_conversation=True)
            self._watcher_unavailable_reported = False
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

        if self._watcher is not None and (changing_workspace or install_watcher):
            self._watcher.close()
            self._watcher = None
        if install_watcher:
            self._install_workspace_watcher(workspace)
        self._settings.set_string("last-workspace", str(workspace.root))

    def _install_workspace_watcher(
        self,
        workspace: WorkspaceService,
        directories: tuple[Path, ...] | None = None,
    ) -> WorkspaceWatcher | None:
        if self._closed or self._workspace is not workspace:
            return None
        # Build the new watcher before closing the old one so there is no
        # window in which the workspace is not being monitored at all. The
        # old watcher stays alive through a background scan and its events
        # are reconciled by ScanCoordinator in the completion handler.
        watcher = WorkspaceWatcher(
            workspace.root,
            lambda: self._refresh_workspace(workspace),
            directories=directories,
        )
        previous = self._watcher
        self._watcher = watcher
        if previous is not None and previous is not watcher:
            previous.close()
        if not watcher.has_monitors and not self._watcher_unavailable_reported:
            self._watcher_unavailable_reported = True
            self._show_toast("无法监控工作区文件变化")
        return watcher

    def _finish_direct_document_scan(
        self,
        generation: int,
        workspace: WorkspaceService,
        snapshot: WorkspaceSnapshot,
        previous_watcher: WorkspaceWatcher | None,
        previous_serial: int,
    ) -> bool:
        if (
            self._closed
            or generation != self._scan_generation
            or self._workspace is not workspace
        ):
            return GLib.SOURCE_REMOVE
        self._library.set_entries(snapshot.entries)
        self._install_workspace_watcher(workspace, snapshot.directories)
        if ScanCoordinator(previous_watcher, previous_serial).should_schedule_followup():
            GLib.idle_add(self._on_workspace_changed)
        return GLib.SOURCE_REMOVE

    def _finish_direct_document_scan_error(
        self,
        generation: int,
        workspace: WorkspaceService,
        error: Exception,
        previous_watcher: WorkspaceWatcher | None,
        previous_serial: int,
    ) -> bool:
        if (
            self._closed
            or generation != self._scan_generation
            or self._workspace is not workspace
        ):
            return GLib.SOURCE_REMOVE
        self._library.show_workspace_error(
            f"文档已打开，但无法浏览所在文件夹：{error}"
        )
        self._install_workspace_watcher(workspace, (workspace.root,))
        return GLib.SOURCE_REMOVE

    def _finish_workspace_scan(
        self,
        generation: int,
        workspace: WorkspaceService,
        snapshot: WorkspaceSnapshot,
        preferred_document: Path | None,
        previous_watcher: WorkspaceWatcher | None,
        previous_serial: int,
    ) -> bool:
        if (
            self._closed
            or generation != self._scan_generation
            or self._workspace is not workspace
        ):
            return GLib.SOURCE_REMOVE
        self._library.set_entries(snapshot.entries)
        self._install_workspace_watcher(workspace, snapshot.directories)
        self.title_label.set_label(workspace.root.name)

        relative = preferred_document
        if relative is None:
            stored = self._settings.get_string("last-document")
            if stored:
                relative = Path(stored)
        if relative is None or not self._is_valid_relative_document(relative):
            relative = self._first_document(snapshot.entries)
        if relative is not None:
            self._on_document_selected(relative)
        else:
            self._clear_current_document()
        if ScanCoordinator(previous_watcher, previous_serial).should_schedule_followup():
            GLib.idle_add(self._on_workspace_changed)
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
        if (
            self._closed
            or generation != self._model_load_generation
            or gateway is not self._opencode
        ):
            return GLib.SOURCE_REMOVE
        self._available_models = models
        self._apply_model_options(gateway, models)
        if os.environ.pop("MDREADER_TEST_MODEL_MENU", "") == "1":
            self._ai_split.set_show_sidebar(True)
            GLib.timeout_add(250, self._popup_model_menu_smoke)
        return GLib.SOURCE_REMOVE

    def _popup_model_menu_smoke(self) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
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
        if (
            self._closed
            or generation != self._model_load_generation
            or gateway is not self._opencode
        ):
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
            self._show_toast("此 OpenCode 模型已不可用")
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
        if self._workspace is None or self._closed:
            return
        self._refresh_workspace(self._workspace)

    def _refresh_workspace(self, workspace: WorkspaceService) -> None:
        """Refresh the tree without treating a watcher event as a new open."""

        if self._closed or self._workspace is not workspace:
            return
        # Keep the current watcher alive while the scan runs. Closing it here
        # would drop every event observed during the scan; instead the serial
        # captured below lets the completion handler schedule a follow-up
        # refresh when anything changed in the meantime.
        previous_watcher = self._watcher
        previous_serial = (
            previous_watcher.change_serial if previous_watcher is not None else 0
        )
        self._scan_generation += 1
        generation = self._scan_generation

        def worker() -> None:
            try:
                snapshot = workspace.scan_snapshot()
            except Exception as error:
                GLib.idle_add(
                    self._finish_workspace_error,
                    generation,
                    workspace,
                    error,
                    True,
                )
                return
            GLib.idle_add(
                self._finish_workspace_refresh,
                generation,
                workspace,
                snapshot,
                previous_watcher,
                previous_serial,
            )

        threading.Thread(
            target=worker,
            name="mdreader-workspace-refresh",
            daemon=True,
        ).start()

    def _finish_workspace_refresh(
        self,
        generation: int,
        workspace: WorkspaceService,
        snapshot: WorkspaceSnapshot,
        previous_watcher: WorkspaceWatcher | None,
        previous_serial: int,
    ) -> bool:
        if (
            self._closed
            or generation != self._scan_generation
            or self._workspace is not workspace
        ):
            return GLib.SOURCE_REMOVE

        self._library.set_entries(snapshot.entries)
        current_relative = self._current_relative_path
        if current_relative is not None:
            by_relative = {
                document.relative_path: document
                for document in snapshot.documents
            }
            current = by_relative.get(current_relative)
            if current is not None:
                if current.fingerprint == self._current_document_fingerprint:
                    self._on_document_selected(
                        current.relative_path,
                        reload_document=False,
                    )
                else:
                    # The current document changed on disk: record the active
                    # heading before the reload so the reader can return to
                    # the section the user was reading instead of jumping to
                    # the top of the new render.
                    self._pending_restore = make_restore(
                        current.relative_path,
                        current.identity,
                        self._active_heading_slug,
                    )
                    self._on_document_selected(
                        current.relative_path,
                        preserve_restore=True,
                    )
            elif self._current_document_identity is not None:
                renamed = tuple(
                    document
                    for document in snapshot.documents
                    if document.identity == self._current_document_identity
                )
                if len(renamed) == 1:
                    self._pending_restore = make_restore(
                        renamed[0].relative_path,
                        renamed[0].identity,
                        self._active_heading_slug,
                    )
                    self._on_document_selected(
                        renamed[0].relative_path,
                        preserve_restore=True,
                    )
                else:
                    self._clear_current_document()
            else:
                self._clear_current_document()

        self._install_workspace_watcher(workspace, snapshot.directories)
        if ScanCoordinator(previous_watcher, previous_serial).should_schedule_followup():
            GLib.idle_add(self._on_workspace_changed)
        return GLib.SOURCE_REMOVE

    def _finish_workspace_error(
        self,
        generation: int,
        workspace: WorkspaceService,
        error: Exception,
        refresh: bool = False,
    ) -> bool:
        if (
            self._closed
            or generation != self._scan_generation
            or self._workspace is not workspace
        ):
            return GLib.SOURCE_REMOVE
        if refresh:
            self._show_toast(f"无法刷新文件列表：{error}")
        else:
            self._library.show_workspace_error(str(error))
            self._show_toast("无法打开文件夹")
        self._install_workspace_watcher(workspace, (workspace.root,))
        return GLib.SOURCE_REMOVE

    def _on_document_selected(
        self,
        relative_path: Path,
        *,
        reload_document: bool = True,
        preserve_restore: bool = False,
    ) -> None:
        if self._workspace is None or self._closed:
            return
        workspace = self._workspace
        try:
            document = workspace.snapshot_document(relative_path)
            document_path = workspace.validate_document(relative_path)
        except WorkspaceError as error:
            self._show_toast(str(error))
            return

        self._current_relative_path = document.relative_path
        self._current_document_path = document_path
        self._current_document_identity = document.identity
        self._current_document_fingerprint = document.fingerprint
        self._settings.set_string("last-document", str(document.relative_path))
        self.title_label.set_label(document_path.name)
        self._ai.set_document(document.relative_path)
        if not reload_document:
            # Metadata-only update: the content is already current, so any
            # in-flight reload keeps its restore while no new one is needed.
            return

        # A fresh load invalidates the heading recorded for the previous
        # document; the bridge will report the new document's heading again.
        self._active_heading_slug = ""
        if not preserve_restore:
            # A user-initiated selection must never restore the previous
            # document's reading position. Only the watcher-triggered reload
            # and rename paths pass preserve_restore=True after recording the
            # restore request themselves.
            self._pending_restore = None

        if (
            self._pending_fragment is not None
            and self._pending_fragment[0] != document.relative_path
        ):
            self._pending_fragment = None
        self._cancel_ai_request(reset_conversation=True)
        self._current_source = None
        self._selection = DocumentSelection()
        self._ai.set_selection(self._selection)
        self.ai_button.remove_css_class("accent")
        self._library.set_outline(())
        if self._library_split.get_collapsed():
            self._library_split.set_show_sidebar(False)

        self._document.load_document(
            document_path,
            zoom=self._zoom,
            workspace_root=workspace.root,
            on_loaded=lambda rendered: self._on_document_loaded(
                workspace,
                document,
                rendered,
            ),
            on_error=lambda error: self._on_document_error(
                workspace,
                document,
                error,
            ),
        )

    def _clear_current_document(self) -> None:
        was_current = self._current_relative_path is not None
        self._pending_fragment = None
        self._pending_restore = None
        self._active_heading_slug = ""
        self._cancel_ai_request(reset_conversation=True)
        self._current_relative_path = None
        self._current_document_path = None
        self._current_document_identity = None
        self._current_document_fingerprint = None
        self._current_source = None
        self._selection = DocumentSelection()
        self._clear_pending_edit()
        self._settings.set_string("last-document", "")
        self._library.set_outline(())
        self._ai.set_document(None)
        self._ai.set_selection(self._selection)
        self.ai_button.remove_css_class("accent")
        self._document.invalidate(show_empty=True)
        if self._workspace is None:
            self.title_label.set_label("MD Reader")
        else:
            self.title_label.set_label(
                self._workspace.root.name or str(self._workspace.root)
            )
        if was_current and os.environ.get("MDREADER_TEST_WATCHER_ACCEPT") == "1":
            print("MDREADER_TEST_WATCHER_CLEARED", flush=True)
            GLib.timeout_add(1000, self._quit_presented_smoke)
    def _on_document_loaded(
        self,
        workspace: WorkspaceService,
        document: DocumentSnapshot,
        rendered: RenderedDocument,
    ) -> None:
        if (
            self._closed
            or self._workspace is not workspace
            or self._current_relative_path != document.relative_path
            or self._current_document_identity != document.identity
        ):
            return
        self._current_source = rendered.source
        self._library.set_outline(rendered.outline)
        expected_document = os.environ.pop(
            "MDREADER_TEST_EXPECT_DOCUMENT", ""
        ).strip()
        if expected_document and self._current_document_path is not None:
            actual = str(self._current_document_path.resolve(strict=False))
            expected = str(Path(expected_document).resolve(strict=False))
            marker = "OK" if actual == expected else "FAIL"
            print(f"MDREADER_TEST_DOCUMENT_{marker}={actual}", flush=True)
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

    def _fragment_click_smoke(self, href: str) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._document.click_link_for_test(href)
        GLib.timeout_add(2500, self._quit_presented_smoke)
        return GLib.SOURCE_REMOVE

    def _scroll_smoke_source(self, line: int) -> bool:
        self._document.scroll_to_source(line)
        return GLib.SOURCE_REMOVE

    def _send_smoke_question(self, question: str) -> bool:
        edit_mode = os.environ.pop("MDREADER_TEST_EDIT", "") == "1"
        self._on_ai_send(question, edit_mode)
        return GLib.SOURCE_REMOVE

    def _on_document_error(
        self,
        workspace: WorkspaceService,
        document: DocumentSnapshot,
        error: Exception,
    ) -> None:
        if (
            self._closed
            or self._workspace is not workspace
            or self._current_relative_path != document.relative_path
            or self._current_document_identity != document.identity
        ):
            return
        if (
            self._pending_fragment is not None
            and self._pending_fragment[0] == document.relative_path
        ):
            self._pending_fragment = None
        # A failed render cannot restore a reading position; drop the request
        # so a later successful load does not scroll to a stale heading.
        self._pending_restore = None
        self._show_toast(f"无法渲染文档：{error}")

    def _on_outline_selected(self, item: OutlineItem) -> None:
        self._document.scroll_to_heading(item.slug)
        if self._library_split.get_collapsed():
            self._library_split.set_show_sidebar(False)

    def _on_active_heading_changed(self, _view: DocumentView, slug: str) -> None:
        self._active_heading_slug = slug
        self._library.set_active_outline(slug)
        if os.environ.get("MDREADER_TEST_WATCHER_ACCEPT") == "1":
            print(f"MDREADER_TEST_WATCHER_HEADING={slug}", flush=True)

    def _on_document_presented(self, _view: DocumentView) -> None:
        if self._closed:
            return
        action = resolve_presented_action(
            self._pending_fragment,
            self._pending_restore,
            self._current_relative_path,
            self._current_document_identity,
        )
        if action.kind == "fragment":
            # An explicit #fragment always wins over automatic position
            # restore and must not be skipped by a smooth heading scroll.
            self._pending_fragment = None
            self._pending_restore = None
            self._document.scroll_to_heading(action.slug)
        elif action.kind == "restore":
            # Watcher-triggered reloads and renames return to the section the
            # user was reading. Use an instant scroll so the page does not
            # visibly glide from the top after every external change.
            self._pending_restore = None
            self._document.scroll_to_heading(action.slug, smooth=False)
        if os.environ.get("MDREADER_TEST_WATCHER_ACCEPT") == "1":
            self._watcher_accept_presents = (
                getattr(self, "_watcher_accept_presents", 0) + 1
            )
            print(
                f"MDREADER_TEST_WATCHER_PRESENT={self._watcher_accept_presents}",
                flush=True,
            )
        fragment_click = os.environ.pop("MDREADER_TEST_FRAGMENT_CLICK", "").strip()
        if fragment_click:
            # The click only fires after the first document-presented, so the
            # harness always observes PRESENT=1 before the link is activated.
            GLib.timeout_add(400, self._fragment_click_smoke, fragment_click)
        if self._test_source_line:
            source_line = self._test_source_line
            self._test_source_line = 0
            GLib.timeout_add(200, self._scroll_smoke_source, source_line)
        if os.environ.pop("MDREADER_TEST_CTRL_WHEEL", "") == "1":
            self._test_ctrl_wheel = True
            self._document.dispatch_ctrl_wheel_for_test()
        if os.environ.pop("MDREADER_TEST_QUIT_ON_PRESENT", "") == "1":
            selection_scroll_test = (
                os.environ.get("MDREADER_TEST_SELECTION_SCROLL") == "1"
            )
            GLib.timeout_add(
                600 if self._test_ctrl_wheel or selection_scroll_test else 200,
                self._quit_presented_smoke,
            )

    def _quit_presented_smoke(self) -> bool:
        application = self.get_application()
        if application is not None:
            application.quit()
        return GLib.SOURCE_REMOVE

    def _on_selection_changed(self, _view: DocumentView, selection: DocumentSelection) -> None:
        if self._closed:
            return
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
            self._ai.show_error("请先打开文档，再开始对话")
            return
        if self._current_source is None:
            self._ai.show_error("请等待文档渲染完成")
            return
        if edit_mode and self._selection.is_empty:
            self._ai.show_error("请先选择需要修改的文档行")
            return
        try:
            gateway = self._opencode
            self._ai_request_generation += 1
            request_generation = self._ai_request_generation
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
            gateway.send(
                prompt,
                on_text=lambda text: self._on_ai_text(
                    request_generation,
                    gateway,
                    text,
                ),
                on_done=lambda event: self._on_ai_done(
                    request_generation,
                    gateway,
                    event,
                ),
                on_error=lambda error: self._on_ai_error(
                    request_generation,
                    gateway,
                    error,
                ),
            )
        except (OSError, OpenCodeError) as error:
            self._clear_pending_edit()
            self._ai.show_error(str(error))

    def _is_current_ai_request(
        self,
        request_generation: int,
        gateway: OpenCodeGateway,
    ) -> bool:
        return (
            not self._closed
            and request_generation == self._ai_request_generation
            and gateway is self._opencode
        )

    def _on_ai_text(
        self,
        request_generation: int,
        gateway: OpenCodeGateway,
        text: str,
    ) -> bool:
        if not self._is_current_ai_request(request_generation, gateway):
            return GLib.SOURCE_REMOVE
        if self._pending_edit:
            self._pending_edit_text += text
        else:
            self._ai.append_assistant_text(text)
        return GLib.SOURCE_REMOVE

    def _on_ai_done(
        self,
        request_generation: int,
        gateway: OpenCodeGateway,
        _event: dict,
    ) -> bool:
        if not self._is_current_ai_request(request_generation, gateway):
            return GLib.SOURCE_REMOVE
        if self._pending_edit:
            self._finish_edit_proposal()
        else:
            self._ai.finish_assistant()
        return GLib.SOURCE_REMOVE

    def _on_ai_error(
        self,
        request_generation: int,
        gateway: OpenCodeGateway,
        error: Exception,
    ) -> bool:
        if not self._is_current_ai_request(request_generation, gateway):
            return GLib.SOURCE_REMOVE
        self._ai.show_error(str(error))
        self._clear_pending_edit()
        return GLib.SOURCE_REMOVE

    def _on_ai_cancel(self) -> None:
        self._cancel_ai_request(reset_conversation=False)

    def _cancel_ai_request(self, *, reset_conversation: bool) -> None:
        self._ai_request_generation += 1
        if self._opencode is not None:
            self._opencode.reset_session()
        self._clear_pending_edit()
        if reset_conversation:
            self._ai.reset_conversation()

    def _finish_edit_proposal(self) -> None:
        selection = self._pending_edit_selection
        path = self._pending_edit_path
        base_hash = self._pending_edit_base_hash
        payload = self._pending_edit_text
        self._clear_pending_edit()
        if path is None or self._patches is None:
            self._ai.show_error("源文档已不可用")
            return
        if self._current_document_path != path:
            self._ai.show_error("修改建议生成前，文档内容已发生变化")
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
        self._ai.finish_assistant("修改建议已生成，请审核")
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
            "应用 AI 修改？",
            f"只会修改 {proposal.path.name} 的第 {proposal.start_line}–{proposal.end_line} 行。",
        )
        dialog.set_extra_child(scrolled)
        dialog.add_response("cancel", "取消")
        dialog.add_response("apply", "应用修改")
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
            self._show_toast("打开的工作区已变化，未应用此修改建议")
            return
        try:
            self._patches.apply(proposal)
        except (OSError, PatchError) as error:
            self._show_toast(str(error))
            return
        toast = Adw.Toast(title="已应用 AI 修改", button_label="撤销")
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
                self._show_toast("已撤销 AI 修改")
        except (OSError, PatchError) as error:
            self._undo_action.set_enabled(self._patches.can_undo)
            self._show_toast(str(error))

    def _on_local_document(self, _view: DocumentView, uri: str) -> None:
        if self._workspace is None or self._closed:
            return
        try:
            link = parse_local_document_uri(uri)
        except LocalDocumentLinkError as error:
            self._show_toast(f"无法打开链接：{error}")
            return

        try:
            relative = self._workspace.relative_path(link.path)
        except WorkspaceError:
            self._show_toast("链接的文档位于当前工作区之外")
            return
        try:
            self._workspace.validate_document(relative)
        except WorkspaceError as error:
            self._show_toast(str(error))
            return

        if relative == self._current_relative_path:
            if link.fragment:
                self._document.scroll_to_heading(link.fragment)
            return

        self._pending_fragment = (relative, link.fragment) if link.fragment else None
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
            self._show_toast(f"文档缩放：{self._zoom}%")

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
