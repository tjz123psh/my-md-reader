# Architecture

## 1. Scope and quality bar

MD Reader is a local-first, read-only Markdown workspace for Linux. It targets
people who spend long periods reading project notes, specifications and
technical documentation. The single primary job is to open a local Markdown
document directly, or open its folder as a workspace, and make the content
comfortable to navigate, understand and discuss.

The application must remain useful with no account, no network and no AI
service configured. AI is an additive panel, not the application shell.

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
- OpenCode 1.18.2 was installed at `/usr/bin/opencode` at inspection time.
  The direct-LLM migration removes the runtime dependency, so this entry is a
  legacy record of the pre-migration environment.

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

### Direct LLM boundary

AI access is hidden behind a provider-neutral gateway so UI and session state
do not depend on one transport:

```text
AiPanel → window coordinator → ContextBuilder + ConversationState
                           └─→ AiSecretStore
                           └─→ OpenAICompatibleGateway
                                  ├── EndpointPolicy
                                  ├── ModelCatalogClient → GET /models
                                  └── ChatCompletionsClient → POST /chat/completions
                           └─→ PatchService → diff approval → atomic write
```

The transport is a direct OpenAI-compatible HTTP gateway over libsoup 3 that
replaced the legacy OpenCode subprocess (migration complete, commit 222501b).
Connection is configured in-app: an API base URL, an auth mode and an API key.
The key is stored in the Secret Service and never persists in GSettings, plain
files, logs, command lines, screenshots or test snapshots; it is only attached
to the HTTP `Authorization` header of outgoing requests. A strict endpoint
policy normalizes the URL, allows HTTP only for exact loopback hosts (TLS
verification can never be disabled), constructs `/chat/completions` and
`/models` from parsed URI components, and follows redirects manually: no
automatic follow, every hop revalidated, at most 3 hops, same origin only and
no HTTPS downgrade, so the Authorization header never crosses origins.

The model catalog is fetched from `GET {base}/models` on demand with the
current form draft and held only in memory; the app never connects at startup.
The user picks from a searchable model list or types a model ID manually. The
legacy free-model filter and `opencode/` prefix rules are removed.

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

ConversationState keeps a bounded, in-memory, success-only Ask history: at most
12 messages and 48,000 characters, cleared on document, model or connection
switch. Edit is a one-shot request with its own system prompt and never carries
the Ask history.

Assistant Markdown is parsed locally with raw HTML disabled and mapped to
native GTK labels, grids and code blocks. Headings, emphasis, links, lists,
quotes and tables retain structure; fenced code uses Pygments colors. An
`AdwSpinner` labeled “Thinking…” appears as soon as a request is accepted and
is removed at completion. It indicates process state and never exposes or
fabricates hidden model reasoning.

The AI composer exposes explicit Ask and Edit modes. Ask is read-only. Edit can
be selected before source lines exist, shows an inline selection requirement,
and enables sending only after lines are selected; it can still only produce
the existing reviewed diff proposal. The prompt uses a native `GtkEntry`, the
same input path as the working document search field. Enter confirms an active
IME candidate first and sends only after composition has completed. The nested
AI header disables window title buttons and owns a separate Hide action,
so closing the panel cannot close the application window.

The direct transport has no tool interface and the model is never given the
workspace root: prompts contain only a relative document path and a bounded
excerpt. Responses are bounded by layered size limits — decoded pre-parser
response bytes, individual SSE events, single `data:` lines, Ask/Edit UTF-8
text, model lists and error bodies — and exceeding any of them cancels the
request. Every asynchronous operation carries a generation/request token and a
cancellable handle, so stale callbacks can never overwrite newer state, and
network, secret and parsing work never blocks the GTK main thread.

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
├── bootstrap.py            pre-GTK environment and resource registration
├── application.py          app lifecycle, global actions, dependency checks
├── window.py               composition, adaptive breakpoints, coordinator
├── models/
│   ├── document.py         document/outline/selection value objects
│   ├── file_node.py        tree-list GObject model
│   ├── conversation.py     messages, context, patch proposals
│   └── ai.py               AiProfile, AiModel, AiErrorCode, AiRequest
├── services/
│   ├── workspace.py        scan, monitor, canonical path policy
│   ├── markdown.py         token parsing, outline and safe HTML
│   ├── settings.py         typed GSettings facade, ai-profile migration
│   ├── themes.py           five shared GTK/WebKit theme token sets
│   ├── context.py          AI context envelope construction
│   ├── ai_endpoints.py     URL validation, normalization, endpoint construction, same-origin policy
│   ├── ai_secrets.py       Secret Service (libsecret) save/read/clear
│   ├── ai_models.py        /models request and response parsing
│   ├── ai_stream.py        incremental UTF-8, SSE and JSON completion parsing
│   ├── llm.py              provider-neutral gateway + OpenAI-compatible implementation
│   └── patches.py          validate, preview, apply and undo
├── widgets/
│   ├── library_sidebar.py  file tree / outline
│   ├── document_view.py    WebKit view and selection bridge
│   ├── ai_panel.py         messages, quote rail, composer, typed state
│   ├── ai_connection_dialog.py  connection config, model fetch/search/select
│   └── empty_state.py      no-folder/no-document states
└── resources/
    ├── ui/                 Blueprint templates
    ├── style.css           restrained native-shell styling
    └── reader/             HTML template, CSS and selection JS
```

The tree above is the current direct-LLM layout (migration complete). The
legacy `services/opencode.py` and `tests/test_opencode.py` were removed when
the gateway landed; no new code extends them. Network, parsing and secret
responsibilities stay in separate services and must not be collapsed into
`window.py` or `ai_panel.py`.

Rules:

- Widgets do not issue network requests, touch the Secret Service, read raw
  GSettings details or write files directly.
- Services expose cancellable operations and plain/GObject models.
- Only the window coordinator combines service and UI state; it does not
  implement URL joining, SSE parsing or secret storage.
- WebKit script-message handlers accept JSON and validate types/lengths.
- File monitoring is debounced; a save event must not trigger render storms.

## 5. Runtime state

GSettings stores preferences and lightweight session state:

- window size and maximized state outside compositor overrides;
- last workspace URI and last document URI;
- document zoom (default 100, range 75–200, 5-point wheel steps);
- one unified reading theme ID: Warm Paper, Mist Blue, Sage Leaf, Midnight
  Ink or Plum Night; legacy system/warm values migrate to a visible theme;
- a single `ai-profile` key (type `a{ss}`) holding the non-secret connection
  metadata: profile id, provider kind (`openai-compatible`), normalized API
  base URL, optional explicit models URL, current model id and `auth-mode`.
  The API key is never part of this value.

The legacy `opencode-model` key remains in the schema marked deprecated; new
code never writes it and its value is not migrated into the new provider
profile. After a first upgrade the AI panel simply shows an unconfigured state.

The API key lives in the Secret Service (`libsecret`) under an app-owned
schema, never in GSettings or files. The model catalog is fetched on demand and
kept only in memory; startup never triggers network access or keyring
unlocking. Chat transcripts containing document text are not persisted until a
clear retention policy exists.

Document zoom is initiated by `Ctrl+mouse wheel` inside the WebKit surface.
Each animation frame commits at most one final percentage, so a 5-point wheel
step causes one layout pass instead of several visible reflows. The bridge
anchors the source-mapped block under the pointer and corrects its post-layout
position; only if no mapped block exists does it fall back to document-height
ratio correction. GTK debounces GSettings persistence until the gesture
settles. Small high-resolution touchpad deltas accumulate to a threshold, while
each discrete mouse-wheel event remains exactly one 5-point step. Ordinary
discrete scrolling keeps its short time-based interpolation.

Document search is a compact popover anchored to the left side of the main
header. It does not install a window-level key-capture widget. The AI composer
also has no key or shortcut controller and uses the same native `GtkEntry`
input path as the working search field. On Wayland, `bootstrap.py` leaves
`GTK_IM_MODULE` unset so
GTK 4 uses the compositor's native text-input protocol, matching the result of
the Fcitx5 GTK4 probe under Niri. The direct Fcitx GTK4 module remains an X11
fallback, and an explicit user override is always preserved. Prompt edits only
update the send button and do not rebuild or restyle the focused editor.

## 6. Failure behavior

- No document: show an `AdwStatusPage` with direct “Open Document” and
  secondary “Open Folder” actions.
- Unsupported/binary file: keep navigation usable and explain the failure.
- Render failure: show source filename and a retry action, never a blank view.
- AI not configured, runtime dependencies missing or keyring/network failure:
  only the AI feature degrades; reading, search, zoom and outline stay fully
  functional. The AI panel shows the matching typed state — unconfigured, auth,
  network or secret error — with a configure action, never an install hint.
- Model/network failure: preserve the transcript and context quote, show a
  bounded error and allow a new request; active responses are cancellable.
- External file change: automatically rerender if there is no pending patch;
  otherwise mark the proposal stale.

## 7. Testing strategy

- Unit tests: path containment, slug generation, outline extraction, source
  line metadata, HTML escaping, context trimming and patch conflict checks.
- Pure logic tests with no GTK or network: URL policy (normalization, strict
  loopback HTTP, same-origin, endpoint construction, manual redirect rules),
  model catalog parsing, incremental SSE/JSON completion parsing and bounded
  ConversationState history.
- Integration tests against a local loopback stub server: model fetch success
  and error classes (auth, 404/405, 429, timeout, malformed JSON), redirect
  handling that proves cross-origin listeners never receive Authorization,
  streaming with split Unicode chunks, cancellation and layered size limits.
- Renderer fixtures: mixed Chinese/Latin text, nested lists, tables, task
  lists, long code, quotes, images, broken links and very large documents.
- GTK smoke test: app starts with no AI configured and with Soup/Secret
  typelibs missing, opens a single fixture document and receives a real
  100% → 105% WebKit wheel zoom message.
- Visual acceptance: real binary screenshots at 640, 960, 1280 and 1920 under
  Niri; all five themes, empty, long-title and AI-context states.
- Accessibility: keyboard-only navigation, visible focus, high contrast and
  200% text scale.

## 8. Packaging

The first deliverable is a native Meson install runnable from the build tree.
Desktop/AppStream metadata, a full-color scalable app icon and a symbolic icon
are installed into the hicolor theme. Flatpak comes later, after its file
portal, network and Secret Service strategy is validated. The permission
boundary, rejected shortcuts and release gates are recorded in
`docs/FLATPAK_CONSTRAINTS.md`.
