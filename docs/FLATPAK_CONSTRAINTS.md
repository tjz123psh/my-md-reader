# Flatpak network and Secret Service constraints

## Status

Flatpak is not the first packaging target. The native Meson install remains the
supported delivery path until workspace portal behavior, direct network access
and Secret Service access pass the gates below. The development host currently
has Flatpak 1.18.0 plus active Desktop and Documents portals.

## Permission principles

- Keep the reader useful without network access or a reachable Secret Service.
- Use `Gtk.FileDialog` and the Documents portal instead of granting
  `--filesystem=home` or `--filesystem=host`.
- Do not grant `org.freedesktop.Flatpak` access merely to call arbitrary host
  commands through `flatpak-spawn --host`.
- Never copy provider credentials into the sandbox. API keys stay in the host
  Secret Service and are read only over the reviewed keyring boundary.
- Preserve the existing app-owned diff, containment, conflict and Undo boundary
  regardless of which side of the sandbox sends AI requests.

## Workspace portal checks

`Gtk.FileDialog` should select a folder through the File Chooser/Documents
portals and return a sandbox-visible document path. Before a Flatpak release,
the following behavior must be verified against that path rather than assumed:

| Area | Required result |
|---|---|
| Session restore | A persisted folder grant can be reopened, or the app asks the user to grant it again without showing a broken workspace |
| Tree scan | Nested Markdown files and supported local images remain visible through the exported directory |
| WebKit | `file:` image loads from the granted tree work in the WebKit subprocess; remote resources remain blocked |
| Monitoring | `Gio.FileMonitor` receives host-side edits without render storms |
| AI apply | Same-directory temporary write plus `os.replace` works on the portal filesystem and preserves LF/CRLF |
| Undo/conflict | External edits and revoked grants fail closed with the existing conflict/error UI |
| Symlinks | Canonical containment still rejects links escaping the granted root |
| Links | `Gtk.UriLauncher` continues through the OpenURI portal |

The canonical workspace root may be a `/run/user/$UID/doc/...` path rather than
the host path. No prompt, transcript or AI request may translate it back to or
disclose an unrestricted host workspace path.

## Network and Secret Service boundary

AI requests go directly from the app to the user-configured OpenAI-compatible
endpoint, so a Flatpak sandbox faces two separate constraints:

1. **Network access.** Reaching `{api_base_url}` requires `--share=network`,
   a broad capability for an otherwise local reader. It must be justified and
   reviewed before release: prompts must never contain canonical workspace
   paths, and the Authorization header must never be sent to another origin
   after a redirect.
2. **Secret Service.** API keys are stored in the host keyring under the
   `io.github.pang.mdreader.ai` schema. The sandbox needs the session bus and
   `org.freedesktop.secrets` (or a reviewed keyring portal on the target
   runtime). When the keyring daemon is unreachable, the app must fail closed
   to the existing "AI not configured" state without breaking reading.

The model never gains a write API: AI responses still go through `PatchService`
inside the app, so no transport change can write files. If network or Secret
Service access is unavailable, the existing "AI unavailable" state is the
complete and acceptable fallback.

## Candidate manifest surface

The eventual manifest should start from the smallest graphical surface:

```text
--socket=wayland
--share=ipc
--device=dri
```

Do not add `--share=network` without a separately reviewed justification, and
do not add home/host filesystem access, background permission or
`--talk-name=org.freedesktop.Flatpak` without a separately reviewed need.
Secret Service access should be added as the narrow session-bus talk name
`org.freedesktop.secrets` (with `--socket=session-bus`) rather than a broad
D-Bus policy, and only after the keyring reachability gates below pass.

## Release gates

- Build against a pinned GNOME runtime with Python, markdown-it-py, Pygments and
  WebKitGTK dependencies reproduced offline.
- Run workspace scan, image, monitor, accepted patch, conflict and Undo tests on
  a portal-granted fixture folder.
- Revoke the grant while open and after restart; both cases must fail clearly.
- Verify prompts never contain canonical workspace paths and that no AI request
  carries an absolute host path or leaks the Authorization header across origins.
- Verify keyring reachability: unlock prompts work, and an unreachable keyring
  degrades to the "AI not configured" state without breaking reading.
- Verify reading with no network and no Secret Service access.
- Repeat 640, 960, 1280 and 1920 Niri screenshots, high contrast, keyboard-only
  navigation and 200% text scaling using the Flatpak application ID.

Only after these gates pass should a Flatpak manifest become a supported build
artifact.
