# Flatpak and OpenCode constraints

## Status

Flatpak is not the first packaging target. The native Meson install remains the
supported delivery path until workspace portal behavior and a narrow OpenCode
host bridge pass the gates below. The development host currently has Flatpak
1.18.0 plus active Desktop and Documents portals.

## Permission principles

- Keep the reader useful without network access or an OpenCode bridge.
- Use `Gtk.FileDialog` and the Documents portal instead of granting
  `--filesystem=home` or `--filesystem=host`.
- Do not grant `org.freedesktop.Flatpak` access merely to call arbitrary host
  commands through `flatpak-spawn --host`.
- Never copy provider credentials into the sandbox. OpenCode continues to own
  credentials and provider configuration.
- Preserve the existing app-owned diff, containment, conflict and Undo boundary
  regardless of which side of the sandbox runs OpenCode.

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
the host path. No prompt, transcript or OpenCode command may translate it back
to or disclose an unrestricted host workspace path.

## OpenCode boundary

The host `/usr/bin/opencode` is not visible inside a normal Flatpak sandbox.
Three integration approaches have different costs:

1. **Narrow host helper, preferred for evaluation.** A separately installed
   native helper owns the OpenCode subprocess and exposes only model listing,
   bounded prompt streaming and cancellation over an authenticated D-Bus or
   Unix-socket interface. It runs OpenCode in the same private temporary
   directory and deny-all configuration used by the native app. The Flatpak
   never sends a workspace path or asks the helper to read/write files.
2. **OpenCode server or ACP transport, possible alternative.** Connect to an
   explicitly started host service with authenticated local transport. This
   needs a clear socket/network permission, lifecycle and origin policy before
   it can replace the subprocess gateway.
3. **Direct host spawn or bundled OpenCode, rejected as defaults.** Broad
   `flatpak-spawn --host` permission exposes arbitrary host command execution.
   Bundling OpenCode duplicates provider state and complicates credentials and
   updates. Neither is justified for the first Flatpak.

The bridge must return model text only. AI replacement JSON still goes through
`PatchService` inside the app, so the model/helper never gains a write API. If
the helper is absent, the existing “OpenCode unavailable” state is the complete
and acceptable fallback.

## Candidate manifest surface

The eventual manifest should start from the smallest graphical surface:

```text
--socket=wayland
--share=ipc
--device=dri
```

Do not add `--share=network` unless the selected OpenCode transport requires
the sandbox itself to connect to a local service. Do not add home/host
filesystem access, background permission, Secret Service access or
`--talk-name=org.freedesktop.Flatpak` without a separately reviewed need.

## Release gates

- Build against a pinned GNOME runtime with Python, markdown-it-py, Pygments and
  WebKitGTK dependencies reproduced offline.
- Run workspace scan, image, monitor, accepted patch, conflict and Undo tests on
  a portal-granted fixture folder.
- Revoke the grant while open and after restart; both cases must fail clearly.
- Verify the helper cannot receive absolute workspace paths or invoke tools.
- Verify reading with no helper and no network.
- Repeat 640, 960, 1280 and 1920 Niri screenshots, high contrast, keyboard-only
  navigation and 200% text scaling using the Flatpak application ID.

Only after these gates pass should a Flatpak manifest become a supported build
artifact.
