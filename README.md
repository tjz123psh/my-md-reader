<p align="center">
  <img src="data/icons/hicolor/scalable/apps/io.github.pang.mdreader.svg" width="128" alt="MD Reader 图标">
</p>

# MD Reader

MD Reader 是一款面向 Linux 的本地只读 Markdown 阅读工作区。它使用 GTK 4、
libadwaita 和 WebKitGTK 6 构建，提供五套统一阅读主题、文件树、标题大纲，以及能够
理解当前文件和选区的 AI 问答助手。

应用本身不提供文本编辑器。AI 只能针对用户选中的源码行提出修改，所有变更都
必须先经过应用生成的 diff 审阅，再由用户明确确认；已应用的修改可以撤销。
即使未配置 AI 或没有网络，Markdown 阅读功能也能完整使用。

## 核心功能

- 可直接打开单个 `.md`、`.markdown`、`.mdown`、`.mkd` 文件，也可打开文件夹工作区。
- 窗口、目录、AI 助手、对话框和状态提示默认使用简体中文。
- 浏览工作区中的 Markdown 文件，并按目录层级显示文件树。
- 自动提取标题大纲，正文滚动时同步高亮当前章节。
- 本地渲染 Markdown、代码高亮、表格、图片和中英文混排，默认禁用原始 HTML。
- 支持工作区内的标准图片路径和 Obsidian `![[图片.png|宽度]]` 附件写法。
- 提供暖纸、雾蓝、鼠尾草、午夜墨和梅夜五套主题，正文、目录与 AI 面板同步切换。
- 支持鼠标平滑滚动、触控板原生滚动，以及 75% 到 200% 的源码块指针锚定独立缩放。
- AI 提供明确的阅读问答和修改提案模式、支持中文输入法的原生提示词输入，并可切换用户配置的 OpenAI-compatible 服务提供的模型。
- AI 回答支持标题、列表、表格、引用、链接和带语法高亮的代码，并显示 Thinking 状态。
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

OpenCode 已不再使用。AI 问答功能需要用户配置一个 OpenAI-compatible 服务，
并不是阅读器的必需依赖。在连接设置中填写 API 基础地址与 API Key 后，AI 面板
会列出该服务提供的模型，并且只保存所选模型的 ID。API Key 由系统 Secret
Service（密钥环）保管，不会写入设置文件或日志。AI 问答还需要 libsoup3 与
libsecret 的 GI 类型库（Arch 包：libsoup3、libsecret）；缺少时只降级 AI，
不影响阅读。

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

- `Ctrl+O`：打开单个 Markdown 文档
- `Ctrl+Shift+O`：打开 Markdown 文件夹
- `Ctrl+F`：在当前文档中查找
- `Ctrl+鼠标滚轮`：缩放文档（75%–200%，保持指针附近的阅读位置）
- `Ctrl+Shift+A`：打开或聚焦 AI 面板
- `Enter`：在 AI 提示词框中发送（输入法候选确认优先）
- `Ctrl+Z`：撤销最近一次已接受的 AI 修改

## 安全边界

发送问题时，MD Reader 会把当前文档的受限摘录、选区、相对路径、行号和你的问题
发送到用户配置的 AI 服务，不会把完整工作区自动发送给模型。模型返回的修改只会被
解析为提案，不能直接写入文件。更完整的实现和安全决策见架构文档。

## 项目文档

- [架构与安全边界](docs/ARCHITECTURE.md)
- [界面设计规范](docs/DESIGN_SPEC.md)
- [Flatpak 网络与 Secret Service 约束](docs/FLATPAK_CONSTRAINTS.md)

## 许可证

本项目采用 GPL-3.0-or-later 许可证，详见 [LICENSE](LICENSE)。
