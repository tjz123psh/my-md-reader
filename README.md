# MD Reader

A native, read-only Markdown workspace for Linux with high-quality warm-toned
rendering, Niri-aware adaptive layouts and a selection-aware OpenCode assistant.
The document outline follows the active heading as the reader scrolls.

The reader never exposes an editor. AI edits are restricted to selected source
lines, shown as an app-owned diff, and written only after explicit approval.
Reading continues to work when OpenCode is missing.

## Dependencies

On Arch Linux:

```bash
sudo pacman -S gtk4 libadwaita webkitgtk-6.0 python-gobject \
  python-markdown-it-py python-linkify-it-py python-pygments \
  meson ninja blueprint-compiler
```

OpenCode is optional for reading. Install and configure OpenCode separately to
enable the AI panel. The panel lists OpenCode's current free models and remembers
only the selected model identifier; the default is
`opencode/deepseek-v4-flash-free`. Provider credentials remain owned by
OpenCode.

## Build and run

```bash
meson setup builddir
meson compile -C builddir
meson devenv -C builddir ./src/md-reader /path/to/file-or-folder
```

Run checks with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
meson test -C builddir --print-errorlogs
```

For a user-local native install, configure a separate build directory:

```bash
meson setup build-install --prefix="$HOME/.local"
meson install -C build-install
```

## Shortcuts

- `Ctrl+O`: open a Markdown folder
- `Ctrl+F`: find in the current document
- `Ctrl++`, `Ctrl+-`, `Ctrl+0`: document-only zoom
- `Ctrl+Shift+A`: toggle the AI panel
- `Ctrl+Z`: undo the last accepted AI change

The app adapts to Niri's 640, 960, 1280 and 1920 logical-pixel column widths,
but it has no runtime dependency on Niri IPC.

## Architecture

See:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/DESIGN_SPEC.md`](docs/DESIGN_SPEC.md)
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)
- [`docs/FLATPAK_CONSTRAINTS.md`](docs/FLATPAK_CONSTRAINTS.md)
- [`docs/SESSION_PROMPT.md`](docs/SESSION_PROMPT.md)

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
