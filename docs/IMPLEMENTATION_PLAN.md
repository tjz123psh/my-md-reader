# Implementation plan and handoff

This is the live project state. Update it after each coherent milestone.

## Milestone 0 — durable context and environment

- [x] Record product contract and fresh-session prompt.
- [x] Record architecture, security boundary and module ownership.
- [x] Inspect real Niri output, presets and `prefer-no-csd`.
- [x] Define visual tokens and wireframes for every preset.
- [x] Install build/runtime development dependencies.

## Milestone 1 — native application shell

- [x] Add Meson project, package metadata and GResource layout.
- [x] Add `Adw.Application` lifecycle and global actions.
- [x] Add adaptive Files / Reader / AI composition.
- [x] Add native empty state, primary menu and keyboard shortcuts.
- [x] Open either one Markdown document or a folder workspace from the UI.
- [x] Add GSettings for window/session/zoom state.
- [x] Build and launch the real binary.

## Milestone 2 — workspace navigation

- [x] Implement canonical workspace root and safe relative paths.
- [x] Scan `.md`, `.markdown`, directories and supported local assets.
- [x] Implement lazy `GtkTreeListModel` file navigation.
- [x] Implement workspace file monitoring and debounced refresh.
- [x] Add file-tree empty/error states.

## Milestone 3 — high-quality reader

- [x] Parse Markdown with raw HTML disabled.
- [x] Extract stable headings and source line metadata.
- [x] Render fenced code with Pygments.
- [x] Bundle reader HTML/CSS/selection bridge in GResource.
- [x] Load local images safely and open external links outside WebKit.
- [x] Resolve unique workspace-local Obsidian image embeds safely.
- [x] Implement outline navigation.
- [x] Track and highlight the active outline heading while scrolling.
- [x] Implement document search and 75–200% zoom.
- [x] Add five unified GTK/WebKit reading themes with legacy migration.
- [x] Commit wheel zoom once per frame with source-block anchoring.
- [x] Add mixed-content renderer fixtures and unit tests.

## Milestone 4 — selection-aware AI

- [x] Detect OpenCode and show a non-blocking unavailable state.
- [x] Verify the installed OpenCode 1.18.2 JSON protocol.
- [x] Implement cancellable streaming `OpenCodeGateway`.
- [x] Build file/heading/excerpt-lines/selection context envelopes.
- [x] Implement the editorial context rail and source jump-back.
- [x] Add model selection without storing credentials.
- [x] Render assistant Markdown with syntax color and a Thinking state.
- [x] Preserve Fcitx/Chinese IME preedit in the AI composer.
- [x] Use native Wayland IME preedit without composer state churn.
- [x] Translate primary navigation, AI controls, dialogs and errors to Chinese.

## Milestone 5 — safe AI file changes

- [x] Inject an app-owned, tool-denied agent outside the user workspace.
- [x] Parse proposed replacements without applying them.
- [x] Validate workspace containment and symlink policy.
- [x] Display selected-range unified diffs.
- [x] Apply accepted changes atomically and provide persistent Undo.
- [x] Detect stale proposals and external conflicts.

## Milestone 6 — validation and release

- [x] Run unit/integration/accessibility checks.
- [x] Capture and inspect 640/960/1280/1920 Niri screenshots.
- [x] Verify light, dark, high-contrast and 200% text states.
- [x] Add AppStream metadata and native build/install docs.
- [x] Add a release-quality application icon.
- [x] Document later Flatpak/OpenCode portal constraints.
- [x] Add process-level GTK smoke coverage with and without OpenCode.
- [x] Add a curl-based user installer for native `~/.local` installs.

## Current handoff

Updated 2026-07-19. The repository is initialized on `main` with the GitHub
`origin` configured. The native reader, workspace navigation, selection-aware
OpenCode conversation and selected-line diff workflow are functional. There are
64 passing unit tests plus successful Meson compile/test runs.

OpenCode 1.18.2 must remain isolated through both the injected deny-all agent
and the private temporary runtime directory. Do not change it back to
`--dir WORKSPACE`, and never add `--auto`. Real acceptance covered a 640px diff,
Apply, watcher rerender, `Ctrl+Z` Undo, cancellation, and the missing-OpenCode
state. The temporary and repository fixtures ended with SHA-256
`19b19bf7fb9195012fcfe46b841e4da53c01c88a6abc54d6db7d5922738bfe19`.

Active-outline tracking now uses a request-animation-frame scroll bridge,
validated heading IDs and native `GtkListBox` selection. Long outlines follow
the active row without stealing focus, and the document-end case selects the
last heading. `MDREADER_TEST_HEADING=<slug>` opens the outline and scrolls to a
heading for real-app smoke checks. Acceptance covered 640, 960, 1280 and 1920px
with `docs/ARCHITECTURE.md`; screenshots are in `/tmp/mdreader-outline-*.png`.

The AI header now shows the selected model and exposes a native radio menu fed
asynchronously by `opencode models opencode --pure`. The service accepts only
the current free OpenCode identifiers, resets the session when switching and
persists only the model ID. `MDREADER_TEST_MODEL_MENU=1` opens the AI pane and
model menu for real-app smoke checks. Acceptance covered model switching,
isolated keyfile persistence and 640, 960, 1280 and 1920px layouts; screenshots
are in `/tmp/mdreader-model-*.png`.

The release icon now includes full-color scalable and symbolic hicolor assets.
Desktop and AppStream validation are Meson tests, and an isolated DESTDIR install
plus the real About dialog confirmed GTK resolves the icon. The source render is
in `/tmp/mdreader-icon-512.png` and the About acceptance screenshot is
`/tmp/mdreader-icon-about-960.png`.

There are now 64 passing unit tests plus successful compileall, JavaScript
syntax and four Meson tests. The GTK smoke waits for a real WebKit
`document-presented` signal and covers both normal and missing-OpenCode startup;
it skips only when no graphical D-Bus session is available or another MD Reader
instance already owns the application ID. Preserve the 760sp and 1120sp
breakpoints unless later Niri screenshots provide evidence to change them.

The repository now provides `scripts/install.sh` as a curl-based installer.
It validates dependencies, downloads the requested GitHub ref and installs to
`~/.local` without invoking sudo. The installed launcher carries its Meson
prefix so Python modules, GResource assets and GSettings data also work from a
custom `MDREADER_PREFIX`.

Installed Markdown rendering now reads `reader.css` and `bridge.js` from the
registered application GResource instead of assuming the source-tree layout.
Meson tests include a regression check that forbids filesystem asset fallback
while rendering from the built resource bundle.

The renderer now tolerates empty-language fenced code and falls back to escaped
source if a Markdown extension fails, so unusual syntax cannot blank the
document. Workspace-contained standard image paths and unique Obsidian
`![[image|width]]` embeds are supported; the real Arch installation notes
rendered both documents and all 23 referenced attachments per document.

AI responses now use a safe native Markdown block renderer for headings,
lists, tables, quotes, links and Pygments-colored code. An accessible
`AdwSpinner` shows “Thinking…” immediately after send. Real Niri screenshots at
640, 960, 1280 and 1920px found no clipping; the inspected files are
`/tmp/mdreader-ai-640-open3.png`, `/tmp/mdreader-ai-960-final.png`,
`/tmp/mdreader-ai-1280.png` and `/tmp/mdreader-ai-1920.png`, with Thinking and
Obsidian image checks in `/tmp/mdreader-ai-thinking-960.png` and
`/tmp/mdreader-obsidian-image-final.png`.

Long-document white tiles were traced to WebKitGTK DMA-BUF compositing rather
than Markdown, CSS or images. The same source line failed with default
acceleration and rendered completely with software compositing, so the reader
now sets `WebKit.HardwareAccelerationPolicy.NEVER`. Dark-mode checks at all
four Niri widths are in `/tmp/mdreader-software-{640,960,1280,1920}.png`;
`/tmp/mdreader-software-image.png` confirms local images still render.

Reader scrolling keeps native WebKit smooth scrolling while the selection
bridge caches headings, limits active-outline work to roughly every 72 ms and
uses binary search instead of scanning every heading on every frame.
`Ctrl+mouse wheel` remains the only zoom shortcut: 5-point changes are queued
by animation frame, preserve the pointer-relative position and debounce
GSettings persistence. A real WebKit wheel event now verifies 100% → 105% in
the GTK process smoke test. AI streaming still renders its first text
immediately, throttles full Markdown rebuilds and coalesces scroll-to-bottom
work.

The AI panel now disables nested window title buttons and uses an explicit
Hide action, verified to preserve the active application window. Ask and Edit
are visible exclusive modes. Edit can be selected without a source selection,
shows an inline requirement and enables send once lines are selected, while
continuing through the reviewed diff boundary. The composer now uses a native
GTK entry and has no key or shortcut controller.
Clean Niri acceptance at 640, 960, 1280 and 1920px is in
`/tmp/mdreader-ime-search-final/`; the settled 1280 transition check is
`settled-1280.png` in the same directory.

The full-width `GtkSearchBar` and its window-level key capture were removed;
document search now opens from a compact popover button at the left side of the
main header. Discrete mouse wheels use a 150 ms cubic interpolation for ordinary
scrolling while small touchpad deltas remain native. Ctrl+wheel commits one
final zoom per animation frame, anchors the source block under the pointer and
keeps each discrete wheel event to one 5-point step. The real WebKit smoke
requires and receives the final 105% value.

The 2026-07-18 reader-input pass adds five product-wide themes: Warm Paper,
Mist Blue, Sage Leaf, Midnight Ink and Plum Night. One `ReaderTheme` token set
now drives the libadwaita shell, library/outline, AI panel and WebKit reader;
legacy `system`, `warm-light` and `warm-dark` settings migrate to visible
choices. A D-Bus activation check changed Warm Paper to Plum Night in the real
window, updated the stateful radio action and persisted the new GSettings value.
Five 960px theme screenshots are in `/tmp/mdreader-four-fixes/theme-windows/`.
Assistant Markdown now consumes the same theme syntax tokens instead of a
separate light/dark palette, and theme changes re-render both the current reply
and earlier assistant messages so inline code cannot retain stale colors.

The AI composer no longer owns a key or shortcut controller. It uses the same
native `GtkEntry` input path as document search, with Enter activating send
only after the IME has handled candidate confirmation. On the recorded
Niri/Fcitx5 session,
the GTK4 probe selects the native `wayland` input path, so `bootstrap.py` no
longer forces the direct Fcitx module on Wayland; it retains that fallback for
X11 and preserves explicit user overrides.

The open flow now exposes a Markdown `GtkFileDialog`, keeps folder opening as a
separate action, and maps `Ctrl+O` to a single document plus `Ctrl+Shift+O` to a
folder. A selected file still uses its parent as the canonical workspace root,
so the existing tree, AI context and patch containment rules remain intact.

Wheel zoom now avoids the visible 2% + 2% + 1% reflow sequence. The JavaScript
bridge coalesces events into one animation-frame commit and repositions the
source-mapped block under the pointer after layout. A real smoke regression
caught and fixed discrete `deltaY=-100` being misread as four impulses; the
final implementation treats it as exactly 100% → 105%. The full suite is 64
unit tests, four passing Meson tests including GTK startup with and without
OpenCode, plus Blueprint, schema, JavaScript and Python syntax checks. Fresh
Niri acceptance covers 634, 954, 1273 and 1912px tiles, all five themes, a 960px
AI overlay, high contrast and 200% text scaling; screenshots are under
`/tmp/mdreader-four-fixes/`.

The 2026-07-19 Chinese-input pass replaces the problematic multiline
`GtkTextView` with the same native entry path already proven by document search.
The composer has no custom text buffer, overlay placeholder or keyboard
controller, eliminating the widget-specific Fcitx failure and input shaking.
The primary application UI, status pages, AI controls, theme names,
file dialogs, diff approval and user-facing service errors now use Simplified
Chinese; desktop and AppStream metadata include Chinese copy as well.

The native entry was launched in the real application and inspected at Niri's
634, 954, 1273 and 1912px tile widths. Direct Wayland Unicode input remained
visible and stable with no composer clipping or layout movement; screenshots
are in `/tmp/mdreader-ime-entry/entry-ai-{640,960,1280,1920}.png`. Virtual
keyboard events do not traverse Fcitx5's physical-key preedit path, so final
Pinyin candidate acceptance was repeated in the normal desktop D-Bus/Wayland
session with a physical keyboard. Fcitx5 switched normally and committed
Chinese text in the AI entry without the previous input shaking.
