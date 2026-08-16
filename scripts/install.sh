#!/usr/bin/env bash

set -Eeuo pipefail

readonly REPOSITORY="tjz123psh/my-md-reader"
readonly REF="${MDREADER_REF:-main}"
readonly PREFIX="${MDREADER_PREFIX:-${HOME:-}/.local}"

say() {
    printf '[MD Reader] %s\n' "$*"
}

fail() {
    printf '[MD Reader] 错误：%s\n' "$*" >&2
    exit 1
}

if [[ "$(uname -s)" != "Linux" ]]; then
    fail "当前安装脚本仅支持 Linux。"
fi

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    fail "请不要使用 root 或 sudo 运行。应用默认安装到当前用户的 ~/.local。"
fi

if [[ -z "${HOME:-}" ]]; then
    fail "HOME 环境变量未设置，无法确定用户安装目录。"
fi

if [[ "$PREFIX" != /* ]]; then
    fail "MDREADER_PREFIX 必须是绝对路径，当前值为：$PREFIX"
fi

missing_commands=()
for command_name in curl tar python3 meson ninja blueprint-compiler; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        missing_commands+=("$command_name")
    fi
done

runtime_ok=true
if command -v python3 >/dev/null 2>&1; then
    if ! python3 -c '
import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gtk, WebKit  # noqa: F401
import linkify_it  # noqa: F401
import markdown_it  # noqa: F401
import pygments  # noqa: F401
' >/dev/null 2>&1; then
        runtime_ok=false
    fi
else
    runtime_ok=false
fi

if (( ${#missing_commands[@]} > 0 )) || [[ "$runtime_ok" != true ]]; then
    printf '[MD Reader] 缺少构建或运行依赖。\n' >&2
    if (( ${#missing_commands[@]} > 0 )); then
        printf '[MD Reader] 未找到命令：%s\n' "${missing_commands[*]}" >&2
    fi
    printf '\nArch Linux 请先运行：\n\n' >&2
    printf '  sudo pacman -S curl tar gtk4 libadwaita webkitgtk-6.0 python-gobject \\\n' >&2
    printf '    python-markdown-it-py python-linkify-it-py python-pygments \\\n' >&2
    printf '    meson ninja blueprint-compiler\n\n' >&2
    fail "依赖安装完成后，请重新执行一键安装命令。"
fi

# AI 问答所需的 GI typelib 是可选的：缺失只降级 AI，不阻塞安装或阅读。
ai_runtime_ok=true
if ! python3 -c '
import gi
gi.require_version("Soup", "3.0")
gi.require_version("Secret", "1")
from gi.repository import Soup, Secret  # noqa: F401
' >/dev/null 2>&1; then
    ai_runtime_ok=false
fi
if [[ "$ai_runtime_ok" != true ]]; then
    printf '[MD Reader] 未检测到 libsoup3/libsecret 的 GI 类型库，AI 问答功能将不可用。\n' >&2
    printf '[MD Reader] 缺少时不影响 Markdown 阅读；如需 AI 问答，请安装 libsoup3、libsecret 及 python-gobject 的对应绑定。\n' >&2
fi

work_dir="$(mktemp -d -t mdreader-install-XXXXXXXX)"
cleanup() {
    rm -rf "$work_dir"
}
trap cleanup EXIT

archive="$work_dir/source.tar.gz"
source_dir="$work_dir/source"
build_dir="$work_dir/build"
archive_url="https://github.com/$REPOSITORY/archive/$REF.tar.gz"

say "正在下载源码（$REF）..."
curl --proto '=https' --tlsv1.2 --fail --location --retry 3 \
    --proto-redir '=https' \
    --show-error --silent --output "$archive" "$archive_url"

mkdir -p "$source_dir"
tar -xzf "$archive" --strip-components=1 -C "$source_dir"
# 归档解压后立即删除，提前释放下载占用；整个临时目录在退出时仍由 trap 兜底。
rm -f "$archive"
[[ -f "$source_dir/meson.build" ]] || fail "下载的源码归档不完整。"

# 剔除构建/安装用不到的源码内容（docs/ 与 scripts/ 均不被 meson 引用；
# tests/ 必须保留，根 meson.build 有 subdir('tests')）。
rm -rf "$source_dir/docs" "$source_dir/scripts"

say "正在构建..."
meson setup "$build_dir" "$source_dir" \
    --prefix "$PREFIX" \
    --buildtype release \
    --wrap-mode nodownload
meson compile -C "$build_dir"

say "正在安装到 $PREFIX ..."
meson install -C "$build_dir"

[[ -x "$PREFIX/bin/md-reader" ]] || fail "安装完成，但未找到启动器。"

say "安装完成。运行：md-reader"

# 自动把 PREFIX/bin 加入当前 shell 的 PATH（幂等；识别不了 shell 时退回提示）。
if [[ ":${PATH:-}:" != *":$PREFIX/bin:"* ]]; then
    shell_name="$(basename "${SHELL:-}")"
    case "$shell_name" in
        fish)
            fish_config="${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish"
            mkdir -p "$(dirname "$fish_config")"
            if ! grep -qs "fish_add_path $PREFIX/bin" "$fish_config" 2>/dev/null; then
                printf '\n# Added by md-reader installer\nfish_add_path %s/bin\n' "$PREFIX" >> "$fish_config"
            fi
            say "已将 $PREFIX/bin 加入 fish 的 PATH（$fish_config），新终端即可运行 md-reader。"
            ;;
        bash)
            bash_config="$HOME/.bashrc"
            if ! grep -qs "export PATH=\"$PREFIX/bin" "$bash_config" 2>/dev/null; then
                printf '\n# Added by md-reader installer\nexport PATH="%s/bin:$PATH"\n' "$PREFIX" >> "$bash_config"
            fi
            say "已将 $PREFIX/bin 加入 bash 的 PATH（$bash_config），新终端即可运行 md-reader。"
            ;;
        zsh)
            zsh_config="$HOME/.zshrc"
            if ! grep -qs "export PATH=\"$PREFIX/bin" "$zsh_config" 2>/dev/null; then
                printf '\n# Added by md-reader installer\nexport PATH="%s/bin:$PATH"\n' "$PREFIX" >> "$zsh_config"
            fi
            say "已将 $PREFIX/bin 加入 zsh 的 PATH（$zsh_config），新终端即可运行 md-reader。"
            ;;
        *)
            printf '\n当前 PATH 尚未包含 %s/bin。请在 shell 配置中加入：\n\n' "$PREFIX"
            printf '  export PATH="%s/bin:$PATH"\n\n' "$PREFIX"
            ;;
    esac
fi
