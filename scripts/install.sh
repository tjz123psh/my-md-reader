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
[[ -f "$source_dir/meson.build" ]] || fail "下载的源码归档不完整。"

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
if [[ ":${PATH:-}:" != *":$PREFIX/bin:"* ]]; then
    printf '\n当前 PATH 尚未包含 %s/bin。请将下面一行加入 shell 配置：\n\n' "$PREFIX"
    printf '  export PATH="%s/bin:%s"\n\n' "$PREFIX" "\$PATH"
fi
