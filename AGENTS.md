# MD Reader project instructions

This file is the durable source of truth for coding agents working in this
repository. Read it before changing code, then read the documents linked
below. Do not replace recorded decisions with a new stack merely because a
new session has no conversational context.

## Product contract

MD Reader is a native, read-only Markdown reading workspace for Linux. Its
primary job is to make local Markdown pleasant to read for long periods. The
application itself does not expose a text editor. A right-hand AI assistant
may propose local file changes, but a change is only written after the user
reviews and accepts a diff.

The required product features are:

- A Markdown-aware workspace file tree and a heading outline.
- High-quality, warm-toned Markdown rendering with document-only zoom.
- A right-side AI conversation panel backed by OpenCode.
- Selection-aware AI context: file, heading, source line range and quote.
- Safe AI changes constrained to the open workspace, with preview and undo.
- Native Wayland behavior and deliberate layouts for Niri column presets.

## Required reading order

1. `docs/SESSION_PROMPT.md`
2. `docs/ARCHITECTURE.md`
3. `docs/DESIGN_SPEC.md`
4. `docs/IMPLEMENTATION_PLAN.md`
5. `git status --short` and the latest commit

The implementation plan is live state. Update its checkboxes and handoff
section whenever a meaningful unit of work is completed.

## Non-negotiable decisions

- GTK 4 + libadwaita shell, WebKitGTK 6 reading surface.
- Python 3 + PyGObject for the first implementation.
- Meson, Blueprint and GResource for build and packaged assets.
- Markdown is rendered locally with markdown-it-py and Pygments. Raw embedded
  HTML is disabled by default.
- The application has no runtime dependency on Niri IPC. Niri IPC is only a
  development and screenshot-validation tool.
- No network-hosted fonts, scripts or styles. Reader assets are bundled.
- Do not implement direct, silent AI writes. OpenCode runs without `--auto`;
  mutations go through the app-owned diff approval boundary.
- Do not make Flatpak the first packaging target. Native installation comes
  first because OpenCode and workspace access need to work predictably.

## Development rules

- Use the `frontend-design`, `niri-gtk-design`, `designing-gnome-ui` and
  `developing-gtk-apps` skills for UI implementation and review.
- Inspect the active Niri output and presets before changing breakpoints.
- Keep GTK calls on the main thread. File scans, rendering and AI processes
  must not block GTK's main loop.
- Use Gio async APIs or cancellable background work and return to GTK through
  the main context.
- Keep domain logic outside widgets. Widgets consume typed models/services.
- Resolve and validate paths against the canonical workspace root. Treat
  symlinks that escape the workspace as out of scope for AI writes.
- Preserve keyboard navigation, accessible names, high contrast and 200%
  text scaling.
- Never commit generated build directories or compiled schemas/resources.

## Verification floor

Before reporting an implementation complete:

- Run unit tests and a Meson compile.
- Launch the real application, not a mockup.
- Check 640, 960, 1280 and 1920 logical-pixel widths under Niri.
- Inspect screenshots for clipping, accidental dead space and reading width.
- Verify the app still starts when OpenCode is missing; AI should degrade to a
  clear unavailable state without breaking reading.
