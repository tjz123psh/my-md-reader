# Design specification

## Subject, audience and primary job

- **Subject:** a quiet local reading desk for Markdown, not a mini IDE.
- **Audience:** Linux users reading project notes and technical documentation.
- **Primary job:** open a folder and read one Markdown document comfortably.

File navigation and AI conversation support that job. They must never make the
document feel like the center column of a generic dashboard.

## Visual direction

The metaphor is a well-used reading desk: warm paper, dark graphite ink,
muted cloth and colored editorial marks. The shell remains mostly native
libadwaita. The document surface is allowed a stronger reading identity.

### Core light tokens

| Token | Value | Use |
|---|---:|---|
| Linen | `#E8DDCB` | secondary shell/panel warmth |
| Paper | `#F8F3E9` | document surface |
| Graphite | `#2F2926` | primary document text |
| Faded ink | `#74685F` | metadata and secondary text |
| Mulberry | `#7A4651` | links, selection/context rail |
| Moss | `#68705A` | AI/context confirmation and focus accent |

Dark equivalents must preserve relationships rather than invert mechanically:
desk `#1F1C1A`, paper `#292522`, ink `#E9E0D2`, muted `#B6AA9D`, mulberry
`#C58A98`, moss `#AAB596`.

Native controls use libadwaita semantic colors. Hard-coded document colors are
scoped to the WebKit reader and have explicit light/dark/high-contrast rules.

### Typography

- Native UI: system sans-serif through libadwaita.
- Document headings and prose: system document font, falling back to
  `Noto Serif CJK SC`, `Source Han Serif SC`, then generic serif.
- Utility labels and AI metadata: system sans-serif.
- Code: system monospace, falling back to `JetBrains Mono` and
  `Noto Sans Mono CJK SC`.
- Body measure: 68–78 Latin characters; Chinese paragraphs target a visually
  comparable measure rather than stretching to fill the window.
- Default body size: 18px at 100% document zoom; line height 1.75 for mixed
  Chinese/Latin prose.

## Signature interaction

The single distinctive element is an **editorial context rail**.

When text is selected, its source block receives a thin mulberry line in the
reader margin. The AI panel shows the quote with the same line, filename,
heading and source range. Clicking the quote scrolls back to the source. This
is functional provenance, not decoration.

Do not add gradients, floating metric cards, animated backgrounds or a second
visual gimmick. AI messages use a quiet transcript, not colorful chat bubbles.

## Application icon

The application icon is an open warm-paper volume on a mulberry cover. Its
left-page editorial rail carries the selection-provenance signature into the
desktop, while a moss bookmark represents retained AI context. The icon uses no
letters, Markdown logo or chat bubble and has a reduced open-book symbolic
variant for monochrome system surfaces.

## Real Niri width modes

The compositor output is 1920 logical pixels with 1/3, 1/2 and 2/3 presets.
Breakpoints are initially 760sp and 1120sp.

### Compact — approximately 640px

```text
┌────────────────────────────────────────┐
│ [Files]  README.md        [AI] [Menu] │
├────────────────────────────────────────┤
│                                        │
│          Markdown reading view         │
│       comfortable edge padding only    │
│                                        │
└────────────────────────────────────────┘
```

- One primary pane at a time.
- Files pushes a hierarchical navigation page; AI opens a full-height overlay.
- The document remains the default destination.
- Do not squeeze a persistent sidebar beside the document.

### Standard — approximately 960px

```text
┌──────────────────────────────────────────────────────┐
│ [Files]  README.md              [Find] [AI] [Menu] │
├──────────────────────────────────────────────────────┤
│                                                      │
│               Markdown reading view                  │
│             centered readable measure                │
│                                                      │
└──────────────────────────────────────────────────────┘
```

- Reading stays focused and owns the width.
- File/outline and AI are restrained overlays invoked from the header.
- An active selection may leave a small mulberry context indicator on the AI
  button, but not a permanent empty panel.

### Expanded — approximately 1280px

```text
┌──────────────┬──────────────────────────┬─────────────────┐
│ Files/Outline│ README.md                 │ AI              │
│              ├──────────────────────────┤ Context quote   │
│ docs/        │                          │ ┃ lines 12–18   │
│  guide.md    │     Markdown reader      │                 │
│ README.md    │                          │ Conversation    │
│              │                          │                 │
│              │                          │ [Ask…]   [Send] │
└──────────────┴──────────────────────────┴─────────────────┘
```

- Three panes are viable: 230–260px navigation, flexible reader, 320–360px AI.
- Files and outline share a native view switcher rather than two cramped lists.
- Reader content still controls its own line length.

### Full output — approximately 1920px

```text
┌──────────────────┬────────────────────────────────────┬────────────────────┐
│ Workspace        │ README.md                           │ AI                 │
│ Files            ├────────────────────────────────────┤ Context quote      │
│                  │                                    │ ┃ docs/README.md   │
│ ───────────────  │       balanced document field      │ ┃ lines 12–18      │
│ Outline          │       68–78 character measure      │                    │
│                  │                                    │ Conversation       │
│                  │                                    │                    │
│                  │                                    │ [Ask…]      [Send]│
└──────────────────┴────────────────────────────────────┴────────────────────┘
```

- Keep the expanded information architecture.
- The left pane can show files and outline vertically instead of as tabs.
- Side panes gain modest width; document lines do not.
- Extra reader width becomes balanced breathing room, not a narrow strip for
  the entire application.

## Interaction rules

- `Ctrl+O`: open folder.
- `Ctrl+F`: find in document.
- `Ctrl++`, `Ctrl+-`, `Ctrl+0`: document zoom only.
- `Ctrl+Shift+A`: toggle/focus AI panel.
- `Escape`: close an overlay or clear selection context, depending on focus.
- Clicking an outline entry scrolls to the heading without reloading.
- The AI header shows the current model; its menu lists free OpenCode models.
- External links open outside the application.
- Zoom is visible as a compact percentage in the reader menu/status popover,
  not as a permanent slider.

## Empty and error states

- Empty app: folder symbol, “Open a folder of Markdown files”, one “Open
  Folder” suggested action.
- Folder with no Markdown: explain that no `.md`/`.markdown` files were found
  and allow choosing another folder.
- AI unavailable: small status page inside the AI pane; never cover the reader.
- Rendering error: retain the filename, show the precise error and Retry.

## Self-critique and revision

The first palette concept was cream paper plus terracotta, a common generated
design default. It was revised to mulberry editorial marks and moss context
state, with warmth concentrated in the reading surface. The layout also avoids
dashboard cards and treats AI as a transcript with source provenance. These
choices belong specifically to a document reader and give one memorable
interaction without competing with long-form text.

## Acceptance checklist

- No clipping or lost primary action at any real Niri preset.
- No forced minimum width larger than the compact preset.
- Focus order follows Files → Reader → AI in wide mode.
- All icon-only buttons have tooltips and accessible labels.
- High contrast does not depend on the warm palette.
- 200% text scaling remains navigable.
- Reader selection and AI quote clearly express the same source relationship.
- Empty AI space does not reduce the reading area at 640/960px.
