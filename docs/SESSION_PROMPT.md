# Session continuation prompt

Copy the prompt below into a fresh coding session when work needs to continue.
`AGENTS.md` and the linked documents remain authoritative if this prompt and
the repository ever disagree.

```text
You are continuing the MD Reader project in this repository.

First, do not write code. Read AGENTS.md, then read docs/ARCHITECTURE.md,
docs/DESIGN_SPEC.md and docs/IMPLEMENTATION_PLAN.md completely. Inspect
git status and the latest commit so you preserve unfinished user changes.

The product is a native Linux, read-only Markdown reader built with GTK 4,
libadwaita, Python/PyGObject and WebKitGTK 6. It must work well in Niri at the
user's real 640/960/1280/1920 logical-pixel column widths, but it must not
depend on Niri at runtime. The central priority is excellent long-form
Markdown rendering. The app also has a right-side OpenCode AI panel that
receives the active file, heading, visible range and selected source lines.
The user cannot manually edit the document. AI file changes must be shown as
a diff, explicitly accepted, constrained to the workspace and undoable.

Use the frontend-design, niri-gtk-design, designing-gnome-ui and
developing-gtk-apps skills before UI work. Use niri-ipc only for read-only
environment inspection and visual validation. Do not casually redesign the
recorded palette, layout modes, security boundary or technology stack.

Resume from the first incomplete item in docs/IMPLEMENTATION_PLAN.md. Keep
GTK's main loop non-blocking, keep domain logic out of widgets, bundle all
reader assets, disable raw Markdown HTML by default, and make the reader work
even when OpenCode is absent. After each coherent milestone, run relevant
tests, update the implementation plan and record exact next steps in its
handoff section.
```

## Short recovery prompt

Use this only when the agent already knows how to inspect repository files:

```text
Continue MD Reader. Treat AGENTS.md and docs/ as durable memory. Read them and
git status first, then resume the first unchecked implementation-plan item.
Do not bypass the AI diff-approval boundary or the Niri width checks.
```
