from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from mdreader.models import FileNode, OutlineItem, file_node_store
from mdreader.services import FileEntry


class LibrarySidebar(Gtk.Box):
    def __init__(self, on_document_selected: Callable[[Path], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("library-pane")
        self._on_document_selected = on_document_selected

        self._view_stack = Adw.ViewStack()
        self._view_stack.set_vexpand(True)

        self._files_stack = Gtk.Stack()
        self._files_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._file_selection = Gtk.SingleSelection(autoselect=False, can_unselect=True)
        file_factory = Gtk.SignalListItemFactory()
        file_factory.connect("setup", self._setup_file_item)
        file_factory.connect("bind", self._bind_file_item)
        file_factory.connect("unbind", self._unbind_file_item)
        self._file_list = Gtk.ListView(model=self._file_selection, factory=file_factory)
        self._file_list.add_css_class("navigation-sidebar")
        self._file_list.set_single_click_activate(True)
        self._file_list.connect("activate", self._on_file_activated)
        file_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        file_scroll.set_child(self._file_list)
        self._files_stack.add_named(file_scroll, "files")

        self._file_empty = Adw.StatusPage(
            icon_name="folder-symbolic",
            title="打开 Markdown 文件夹",
            description="选择文件夹以浏览其中的 Markdown 文档",
        )
        open_button = Gtk.Button(label="打开文件夹", action_name="win.open-folder")
        open_button.add_css_class("pill")
        open_button.add_css_class("suggested-action")
        open_button.set_halign(Gtk.Align.CENTER)
        self._file_empty.set_child(open_button)
        self._files_stack.add_named(self._file_empty, "empty")

        self._file_loading = Adw.StatusPage(
            icon_name="folder-symbolic",
            title="正在读取文件夹",
            description="正在查找 Markdown 文档…",
        )
        self._files_stack.add_named(self._file_loading, "loading")
        self._files_stack.set_visible_child_name("empty")

        self._outline_stack = Gtk.Stack()
        self._outline_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._outline_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._outline_list.add_css_class("navigation-sidebar")
        self._outline_list.connect("row-activated", self._on_outline_row_activated)
        self._outline_rows: dict[str, Gtk.ListBoxRow] = {}
        self._active_outline_slug = ""
        self._outline_scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        self._outline_scroll.set_child(self._outline_list)
        self._outline_stack.add_named(self._outline_scroll, "outline")
        self._outline_empty = Adw.StatusPage(
            icon_name="view-list-symbolic",
            title="暂无大纲",
            description="当前文档中的标题会显示在这里",
        )
        self._outline_stack.add_named(self._outline_empty, "empty")
        self._outline_stack.set_visible_child_name("empty")

        self._view_stack.add_titled_with_icon(
            self._files_stack, "files", "文件", "folder-symbolic"
        )
        self._view_stack.add_titled_with_icon(
            self._outline_stack, "outline", "大纲", "view-list-symbolic"
        )
        self._view_stack.connect("notify::visible-child-name", self._on_view_changed)

        switcher = Adw.ViewSwitcher(stack=self._view_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar(title_widget=switcher)
        toolbar = Adw.ToolbarView(content=self._view_stack)
        toolbar.add_top_bar(header)
        self.append(toolbar)

        self._on_outline_selected: Callable[[OutlineItem], None] | None = None

    def set_outline_callback(self, callback: Callable[[OutlineItem], None]) -> None:
        self._on_outline_selected = callback

    def show_outline(self) -> None:
        self._view_stack.set_visible_child_name("outline")

    def show_loading(self) -> None:
        self._files_stack.set_visible_child_name("loading")

    def show_workspace_error(self, message: str) -> None:
        self._file_empty.set_title("无法打开文件夹")
        self._file_empty.set_description(message)
        self._files_stack.set_visible_child_name("empty")

    def set_entries(self, entries: Iterable[FileEntry]) -> None:
        entries = tuple(entries)
        if not entries:
            self._file_selection.set_model(None)
            self._file_empty.set_title("没有 Markdown 文档")
            self._file_empty.set_description(
                "此文件夹中没有受支持的 Markdown 文档"
            )
            self._files_stack.set_visible_child_name("empty")
            return
        root = file_node_store(entries)
        self._tree_model = Gtk.TreeListModel.new(
            root,
            False,
            False,
            self._create_child_model,
            None,
        )
        self._file_selection.set_model(self._tree_model)
        self._files_stack.set_visible_child_name("files")

    def set_outline(self, outline: Iterable[OutlineItem]) -> None:
        self._clear_list(self._outline_list)
        self._outline_rows.clear()
        outline = tuple(outline)
        if not outline:
            self._active_outline_slug = ""
            self._outline_stack.set_visible_child_name("empty")
            return

        for item in outline:
            row = Gtk.ListBoxRow(activatable=True, selectable=True)
            row.outline_item = item
            label = Gtk.Label(
                label=item.title,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
                single_line_mode=True,
            )
            label.set_margin_start(12 + max(0, item.level - 1) * 14)
            label.set_margin_end(12)
            label.set_margin_top(7)
            label.set_margin_bottom(7)
            row.set_child(label)
            self._outline_list.append(row)
            self._outline_rows[item.slug] = row
        self._outline_stack.set_visible_child_name("outline")

    def set_active_outline(self, slug: str) -> None:
        self._active_outline_slug = slug
        row = self._outline_rows.get(slug)
        if row is None:
            self._outline_list.unselect_all()
            return
        self._outline_list.select_row(row)
        GLib.idle_add(self._scroll_active_outline_into_view)

    def _on_view_changed(self, _stack: Adw.ViewStack, _parameter: object) -> None:
        if self._view_stack.get_visible_child_name() == "outline":
            GLib.idle_add(self._scroll_active_outline_into_view)

    def _scroll_active_outline_into_view(self) -> bool:
        row = self._outline_rows.get(self._active_outline_slug)
        if row is None or not row.get_mapped():
            return GLib.SOURCE_REMOVE
        allocation = row.get_allocation()
        adjustment = self._outline_scroll.get_vadjustment()
        top = float(allocation.y)
        bottom = top + float(allocation.height)
        visible_top = adjustment.get_value()
        visible_bottom = visible_top + adjustment.get_page_size()
        if top < visible_top:
            adjustment.set_value(top)
        elif bottom > visible_bottom:
            adjustment.set_value(bottom - adjustment.get_page_size())
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _setup_file_item(_factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        expander = Gtk.TreeExpander()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        content.set_margin_end(10)
        content.set_margin_top(6)
        content.set_margin_bottom(6)
        icon = Gtk.Image()
        content.append(icon)
        label = Gtk.Label(
            xalign=0,
            hexpand=True,
            ellipsize=Pango.EllipsizeMode.END,
            single_line_mode=True,
        )
        content.append(label)
        expander.set_child(content)
        list_item.set_child(expander)
        list_item.file_icon = icon
        list_item.file_label = label

    @staticmethod
    def _bind_file_item(_factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        tree_row = list_item.get_item()
        node = tree_row.get_item()
        expander = list_item.get_child()
        expander.set_list_row(tree_row)
        list_item.file_icon.set_from_icon_name(
            "folder-symbolic" if node.is_directory else "text-x-generic-symbolic"
        )
        list_item.file_label.set_label(node.name)

    @staticmethod
    def _unbind_file_item(_factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        expander = list_item.get_child()
        expander.set_list_row(None)

    @staticmethod
    def _create_child_model(node: FileNode, _user_data: object) -> Gio.ListModel | None:
        return node.child_model()

    def _on_file_activated(self, _list_view: Gtk.ListView, position: int) -> None:
        tree_row = self._file_selection.get_item(position)
        if tree_row is None:
            return
        node = tree_row.get_item()
        if node.is_directory:
            tree_row.set_expanded(not tree_row.get_expanded())
        else:
            self._on_document_selected(node.path)

    def _on_outline_row_activated(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        item = getattr(row, "outline_item", None)
        if item and self._on_outline_selected:
            self._on_outline_selected(item)

    @staticmethod
    def _clear_list(list_box: Gtk.ListBox) -> None:
        child = list_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            list_box.remove(child)
            child = following
