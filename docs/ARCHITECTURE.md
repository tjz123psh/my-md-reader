# Architecture

## 1. Scope and quality bar

MD Reader is a local-first, read-only Markdown workspace for Linux. It targets
people who spend long periods reading project notes, specifications and
technical documentation. The single primary job is to open a folder and make
its Markdown comfortable to navigate, understand and discuss.

The application must remain useful with no account, no network and no
OpenCode installation. AI is an additive panel, not the application shell.

## 2. Recorded environment

Environment inspected on 2026-07-15 and revalidated on 2026-07-16:

- Arch Linux on Wayland/Niri.
- Logical output: 1920 × 1080 at scale 1.0.
- Niri presets: 0.33333, 0.5 and 0.66667; default 0.5.
- Target tile widths after compositor gaps: roughly 640, 960, 1280 and 1920.
- Niri has `prefer-no-csd` enabled.
- GTK 4.22.4, libadwaita 1.9.2, PyGObject 3.56.3, WebKitGTK 6.0
  2.52.5, Meson 1.11.2, Ninja 1.13.2 and Blueprint Compiler 0.22.2 are
  installed.
- OpenCode 1.18.2 is installed at `/usr/bin/opencode`.

Breakpoints must sit between actual presets so resizing animations cannot
oscillate around a boundary. The initial thresholds are 760sp and 1120sp.

## 3. Technology decisions

### Native shell

- GTK 4 and libadwaita provide the application/window lifecycle, navigation,
  file tree, outline, actions, preferences, toasts and adaptive layout.
- Python 3 with PyGObject is selected for fast iteration and direct access to
  Gio, GLib, GTK, Adwaita and WebKitGTK APIs.
- Meson is the build system. Blueprint describes stable UI structure; Python
  creates dynamic models and behavior. GResource bundles UI, CSS, HTML and JS.
- Application ID is provisionally `io.github.pang.mdreader`. It must match the
  desktop file, metainfo, resources and GSettings path. Change it everywhere
  in one dedicated commit if the final reverse-domain owner changes.

### Markdown pipeline

```text
UTF-8 file
   │
   ├── WorkspaceService validates path and reads asynchronously
   │
   ├── MarkdownService parses markdown-it tokens
   │      ├── outline entries and stable heading slugs
   │      ├── source line metadata on block elements
   │      └── Pygments-highlighted fenced code
   │
   ├── safe HTML document + bundled reader CSS/JS
   │
   └── WebKitGTK 6 WebView (base URI = document directory)
```

Raw embedded HTML is disabled initially. This prevents a local document from
injecting scripts into the privileged selection bridge. Remote scripts,
styles and fonts are never loaded. External links open through the desktop
URI launcher rather than navigating the reading surface.

The reading WebView uses WebKitGTK's `HardwareAccelerationPolicy.NEVER`.
On the recorded Wayland/Niri and NVIDIA environment, the DMA-BUF renderer
reproducibly left large unpainted white tiles after scrolling long documents.
Software compositing kept the same layout and image support while rendering
every tested tile reliably at 640, 960, 1280 and 1920 logical pixels.

Standard relative image paths may traverse parent directories only while the
resolved target remains inside the canonical workspace. Obsidian image embeds
such as `![[Pasted image.png|617]]` are rewritten without changing source-line
counts when exactly one matching image exists inside the workspace. Missing,
ambiguous, non-image and escaping targets remain visible as source text rather
than being guessed or loaded.

The renderer owns stable source mappings. Block tokens receive
`data-source-start` and `data-source-end` attributes. Selection JavaScript
finds the first and last mapped ancestors and sends this structured payload:

```json
{
  "text": "selected text",
  "startLine": 137,
  "endLine": 142,
  "headingId": "security-policy"
}
```

The same bundled bridge caches the heading nodes, limits active-heading work to
roughly every 72 ms and locates the current heading with a binary search. It
reports only heading-ID changes. GTK validates the message, selects
the corresponding native outline row and scrolls long outlines without moving
keyboard focus away from the document. When the document is at its end, the
last heading remains reachable even if it cannot cross the normal viewport
probe.

### OpenCode boundary

OpenCode access is hidden behind an interface so UI and session state do not
depend on one transport:

```text
AiPanel → window coordinator → ContextBuilder → OpenCodeGateway → OpenCode
                           └─→ PatchService → diff approval → atomic write
```

The implemented transport consumes `opencode run --format json` as an
asynchronous subprocess. OpenCode 1.18.2 emits `step_start`, `text` and
`step_finish` records; their `sessionID` is retained for follow-up turns. A
later server or ACP transport can replace it without widget changes.

The model menu enumerates `opencode models opencode --pure` on a background
thread in the same private runtime and deny-all environment. The gateway only
accepts current `opencode/*-free` identifiers and the zero-cost
`opencode/big-pickle` model. Changing models is rejected while a response is
running and clears the retained OpenCode session before the next message.

Every user message carries an explicit context envelope:

```json
{
  "file": "docs/design.md",
  "heading": {"id": "security", "title": "Security"},
  "visibleExcerptLines": [120, 168],
  "selection": {"lines": [137, 142], "text": "..."}
}
```

Conversation context sends the selection and surrounding section first. It
does not resend an entire large file on every turn. The first version has no
repository-wide search tools; users open or select the additional document
context they want to discuss.

Assistant Markdown is parsed locally with raw HTML disabled and mapped to
native GTK labels, grids and code blocks. Headings, emphasis, links, lists,
quotes and tables retain structure; fenced code uses Pygments colors. An
`AdwSpinner` labeled “Thinking…” appears as soon as a request is accepted and
is removed at completion. It indicates process state and never exposes or
fabricates hidden model reasoning.

The AI composer exposes explicit Ask and Edit modes. Ask is read-only; Edit is
disabled until source lines are selected and can only produce the existing
reviewed diff proposal. A bounded multiline prompt field sends with Enter and
inserts a newline with Shift+Enter. The nested AI header disables window title
buttons and owns a separate Hide action, so closing the panel cannot close the
application window.

OpenCode never runs with `--auto`. The gateway injects an app-owned agent via
`OPENCODE_CONFIG_CONTENT`, sets `OPENCODE_PERMISSION={"*":"deny"}`, disables
project configuration, external skills and default plugins, and still uses
`--pure`. The resolved 1.18.2 agent has every known tool set to `false`.

The subprocess also runs in a private, empty `mdreader-opencode-*` temporary
directory rather than the user's workspace. The prompt contains only the
relative document path and bounded excerpt. This is a second boundary against
future OpenCode permission regressions: the model process is neither located
in nor told the canonical workspace path. `.opencode/agents/md-reader.md` is a
developer reference, not a runtime dependency.

An edit request may only return one JSON replacement for the exact selected
source range. The app binds the request to the original canonical path and
source hash, creates its own unified diff, and writes only after explicit
acceptance. The accepted edit is available through a Toast and the
`win.undo-ai-change` action (`Ctrl+Z`).

### File-change safety

For every proposed write:

1. Canonicalize the workspace root and target.
2. Reject absolute targets outside the root and `..` traversal.
3. Reject symlinks whose resolved destination leaves the root.
4. Compare the file's current content/hash with the proposal base.
5. Present a unified diff with affected file and line counts.
6. On acceptance, use atomic replacement and retain the old bytes for undo.
7. If an external change races the proposal, stop and show a conflict.

The request-time source hash is checked before creating the diff, not only at
apply time. Atomic replacement preserves the document's existing LF or CRLF
line endings.

The reader itself has no editable text surface and no Save action.

## 4. Module boundaries

```text
src/mdreader/
├── application.py          app lifecycle, global actions, dependency checks
├── window.py               composition and adaptive breakpoints
├── models/
│   ├── document.py         document/outline/selection value objects
│   ├── file_node.py        tree-list GObject model
│   └── conversation.py     messages, context, patch proposals
├── services/
│   ├── workspace.py        scan, monitor, canonical path policy
│   ├── markdown.py         token parsing, outline and safe HTML
│   ├── settings.py         typed GSettings facade
│   ├── context.py          AI context envelope construction
│   ├── opencode.py         model catalog, gateway and async event stream
│   └── patches.py          validate, preview, apply and undo
├── widgets/
│   ├── library_sidebar.py  file tree / outline
│   ├── document_view.py    WebKit view and selection bridge
│   ├── ai_panel.py         messages, quote rail, composer
│   └── empty_state.py      no-folder/no-document states
└── resources/
    ├── ui/                 Blueprint templates
    ├── style.css           restrained native-shell styling
    └── reader/             HTML template, CSS and selection JS
```

Rules:

- Widgets do not invoke OpenCode or write files directly.
- Services expose cancellable operations and plain/GObject models.
- Only the window coordinates selected file, current outline and AI context.
- WebKit script-message handlers accept JSON and validate types/lengths.
- File monitoring is debounced; a save event must not trigger render storms.

## 5. Runtime state

GSettings stores preferences and lightweight session state:

- window size and maximized state outside compositor overrides;
- last workspace URI and last document URI;
- document zoom (default 100, range 75–200, 5-point wheel steps);
- a reserved warm light / warm dark / follow-system choice (the current UI
  follows the system);
- last selected OpenCode model identifier, with no credentials.

The model menu is populated from OpenCode asynchronously. Secrets remain owned
by OpenCode/provider storage. Chat transcripts containing document text are not
persisted until a clear retention policy exists.

Document zoom is initiated by `Ctrl+mouse wheel` inside the WebKit surface.
The page queues 5-point changes by animation frame, applies the new body zoom
around the pointer, then sends the validated percentage to GTK. GTK debounces
GSettings persistence until the gesture settles. Plain wheel and touchpad
scrolling remain on WebKit's native smooth-scrolling path.

## 6. Failure behavior

- No workspace: show an `AdwStatusPage` with “Open Folder”.
- Unsupported/binary file: keep navigation usable and explain the failure.
- Render failure: show source filename and a retry action, never a blank view.
- Missing OpenCode: reading remains fully functional and the AI panel explains
  how to install/configure it.
- Model/network failure: preserve the transcript and context quote, show a
  bounded error and allow a new request; active responses are cancellable.
- External file change: automatically rerender if there is no pending patch;
  otherwise mark the proposal stale.

## 7. Testing strategy

- Unit tests: path containment, slug generation, outline extraction, source
  line metadata, HTML escaping, context trimming and patch conflict checks.
- Renderer fixtures: mixed Chinese/Latin text, nested lists, tables, task
  lists, long code, quotes, images, broken links and very large documents.
- GTK smoke test: app starts with and without WebKit/OpenCode and can open a
  fixture workspace.
- Visual acceptance: real binary screenshots at 640, 960, 1280 and 1920 under
  Niri; light, dark, empty, long-title and AI-context states.
- Accessibility: keyboard-only navigation, visible focus, high contrast and
  200% text scale.

## 8. Packaging

The first deliverable is a native Meson install runnable from the build tree.
Desktop/AppStream metadata, a full-color scalable app icon and a symbolic icon
are installed into the hicolor theme. Flatpak comes later, after its file portal
and OpenCode subprocess strategy is tested. The permission boundary, rejected
shortcuts and release gates are recorded in `docs/FLATPAK_CONSTRAINTS.md`.
