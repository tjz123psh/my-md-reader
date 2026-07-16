<p align="center">
  <img src="data/icons/hicolor/scalable/apps/io.github.pang.mdreader.svg" width="128" alt="MD Reader 图标">
</p>

# MD Reader

MD Reader 是一款面向 Linux 的本地只读 Markdown 阅读工作区。它使用 GTK 4、
libadwaita 和 WebKitGTK 6 构建，提供暖色长文排版、文件树、标题大纲，以及能够
理解当前文件和选区的 OpenCode AI 助手。

应用本身不提供文本编辑器。AI 只能针对用户选中的源码行提出修改，所有变更都
必须先经过应用生成的 diff 审阅，再由用户明确确认；已应用的修改可以撤销。
即使没有安装 OpenCode，Markdown 阅读功能也能完整使用。

## 核心功能

- 浏览工作区中的 `.md` 和 `.markdown` 文件，并按目录层级显示文件树。
- 自动提取标题大纲，正文滚动时同步高亮当前章节。
- 本地渲染 Markdown、代码高亮、表格、图片和中英文混排，默认禁用原始 HTML。
- 支持文档搜索，以及 75% 到 200% 的文档独立缩放。
- AI 自动获取当前文件、标题、源码行号和选区引用，可切换 OpenCode 免费模型。
- AI 修改仅限选中的原始行范围，支持 diff 审批、冲突检测、原子写入和撤销。
- 针对 Niri 的 640、960、1280 和 1920 逻辑像素列宽进行自适应设计。
- 原生支持 Wayland，但运行时不依赖 Niri IPC。

## 一键安装

安装好下方列出的系统依赖后，执行：

```bash
curl -fsSL https://raw.githubusercontent.com/tjz123psh/my-md-reader/main/scripts/install.sh | bash
```

脚本会从 GitHub 下载最新源码，构建并安装到 `~/.local`。重复执行即可覆盖升级；
它不会调用 `sudo` 或自动安装系统软件包。若要使用其他用户级前缀，可设置
`MDREADER_PREFIX`：

```bash
curl -fsSL https://raw.githubusercontent.com/tjz123psh/my-md-reader/main/scripts/install.sh | \
  MDREADER_PREFIX="$HOME/Applications/md-reader" bash
```

安装完成后运行 `md-reader`。如果终端找不到该命令，请按照脚本提示把
`~/.local/bin` 加入 `PATH`。

## 依赖

Arch Linux：

```bash
sudo pacman -S gtk4 libadwaita webkitgtk-6.0 python-gobject \
  python-markdown-it-py python-linkify-it-py python-pygments \
  meson ninja blueprint-compiler
```

OpenCode 只用于 AI 功能，并不是阅读器的必需依赖。安装并配置 OpenCode 后，
AI 面板会列出当前可用的免费模型，并且只保存所选模型的 ID。Provider 凭据始终
由 OpenCode 自己管理，MD Reader 不读取也不存储凭据。

## 构建与运行

```bash
meson setup builddir
meson compile -C builddir
meson devenv -C builddir ./src/md-reader /path/to/file-or-folder
```

运行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
meson test -C builddir --print-errorlogs
```

也可以不使用一键脚本，直接通过 Meson 安装到当前用户：

```bash
meson setup build-install --prefix="$HOME/.local"
meson install -C build-install
```

## 快捷键

- `Ctrl+O`：打开 Markdown 文件夹
- `Ctrl+F`：在当前文档中查找
- `Ctrl++`、`Ctrl+-`、`Ctrl+0`：调整或重置文档缩放
- `Ctrl+Shift+A`：打开或聚焦 AI 面板
- `Ctrl+Z`：撤销最近一次已接受的 AI 修改

## 安全边界

OpenCode 使用应用注入的 deny-all agent，并在独立的临时目录中运行，不会获得
工作区路径或文件工具。模型返回的修改只会被解析为提案，不能直接写入文件。
更完整的实现和安全决策见架构文档。

## 项目文档

- [架构与安全边界](docs/ARCHITECTURE.md)
- [界面设计规范](docs/DESIGN_SPEC.md)
- [实施计划与交接状态](docs/IMPLEMENTATION_PLAN.md)
- [Flatpak 与 OpenCode 约束](docs/FLATPAK_CONSTRAINTS.md)
- [新会话恢复提示词](docs/SESSION_PROMPT.md)

## 许可证

本项目采用 GPL-3.0-or-later 许可证，详见 [LICENSE](LICENSE)。
