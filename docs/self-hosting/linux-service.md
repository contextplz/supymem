# Linux desktop packages and optional developer service

## Install your desktop app

Use the `.deb` on Ubuntu 22.04, Ubuntu 24.04 or Debian 12. Install it through
your package manager so its runtime libraries are installed too:

```sh
sudo apt install ./YourApp.deb
```

Open **Supymem Local** in your applications menu, or run
`company-os-desktop-launch` from a terminal in your desktop session. Installation
needs administrator permission; running your app does not. Never launch it with
`sudo`. Python and Node are bundled, so you do not install them separately.

The package requires WebKitGTK 4.1, GTK 3, Ayatana AppIndicator and librsvg.
`libsecret-1-0` and `libdbus-1-3` are recommendations, not requirements. Your app
can keep keys in its encrypted-file fallback when Secret Service is unavailable.
If you prefer your desktop keyring, install and unlock GNOME Keyring or another
Secret Service provider, then reopen **Keys and connections** to save your keys.
Do not put keys in a desktop entry or command line.

Use the AppImage on Fedora 40, or when you do not want a system installation on
Ubuntu or Debian. Keep the image and its matching `.AppImage.sh` launcher in the
same folder, with their downloaded filenames unchanged:

```sh
chmod +x ./YourApp.AppImage
sh ./YourApp.AppImage.sh
```

The matching launcher checks access to `/dev/fuse`, `fusermount` and the FUSE 2 client library.
It automatically uses extraction when any of them is unavailable. You do not need to
install FUSE. You can also run the image directly without FUSE:

```sh
./YourApp.AppImage --appimage-extract-and-run
```

Extraction needs temporary disk space for the bundled runtimes. If your temporary
folder is full or mounted `noexec`, choose a writable executable folder with
`TMPDIR=/your/folder ./YourApp.AppImage --appimage-extract-and-run`. This changes
temporary extraction only, not your saved data. The image contains its desktop
entry and icon. The Debian package also registers the AppImage MIME type and
icon; it does not claim to import arbitrary files into your app.

Both Wayland and X11 sessions are accepted. Your launcher leaves your chosen
display backend alone and sets `WEBKIT_DISABLE_DMABUF_RENDERER=1` only when no
readable, writable DRM render device is present. If your driver still produces a
blank window, close your app and run
`WEBKIT_DISABLE_DMABUF_RENDERER=1 company-os-desktop-launch`, or set that variable
before your AppImage launcher. An explicit value you set is never overwritten.

Your installed desktop data stays under
`${XDG_DATA_HOME:-$HOME/.local/share}/Supymem Local`, not beside the executable or
inside the AppImage. Logs stay in that data folder. Full backups default to its sibling `<data-folder>-backups` folder or your saved backup destination; the local key file is excluded, so preserve recovery material separately. Uninstalling the
package does not remove it. Changing a data path does not migrate existing data.
If you have no graphical session, the launcher asks you to use
`./start --web` from your source checkout instead.

### Build and check your packages

Build on Ubuntu 22.04 to keep the bundled native runtimes compatible with the
oldest supported desktop. Use the local Tauri configuration for your build.
Set `umask 022` before building so system-installed runtime files are readable
and executable by your desktop user, not just the account that built them.
After Tauri produces your AppImage, create its matching launcher:

```sh
python3 scripts/desktop/bundle_linux.py --appimage /absolute/path/YourApp.AppImage
dpkg-deb -I /absolute/path/YourApp.deb
dpkg-deb -c /absolute/path/YourApp.deb
```

Publish the `.AppImage.sh` beside the `.AppImage`. Inspect `Depends` and
`Recommends`, the application entry, icon, MIME metadata, launcher and bundled
resources before distribution. To inspect an AppImage without mounting it, run
`/absolute/path/YourApp.AppImage --appimage-extract` in a disposable directory.
This produces `squashfs-root`; do not use it as your data directory.

## Optional Linux developer service

Your desktop app is the main path. Use this only when you want source services
without a desktop window. It reuses `scripts/run_local.sh`, which starts core,
worker, bridge and console on loopback. It does not start a gateway, warden or builder.

## Prepare your checkout

Follow [the Unix launcher setup](install.md#minimal-unix-launcher) first. You need
Python 3.12, Node.js 22+ and pnpm 10.11.0 or Corepack. Run the launcher once,
verify `/setup`, then stop it with Ctrl-C before enabling this service.
Put your checkout at `$HOME/supymem`, or replace that path below.

Save your keys through **Keys and connections**. Use the checkout's `.env` for
non-secret launcher settings such as ports and data paths, not for new keys.
The launcher reads `.env`, not a separate systemd environment file. Its default data root is
`data/runtime/local`; set your absolute `COMPANY_OS_DATA_DIR` and matching database
URL before moving an installation. Changing paths does not migrate your data.
Keep your vault secret, existing credentials and safety defaults.

## Service file

Create `$HOME/.config/systemd/user` if it is missing. Save this as
`$HOME/.config/systemd/user/supymem-local.service`:

```ini
[Unit]
Description=Your local Supymem developer services

[Service]
Type=exec
WorkingDirectory=%h/supymem
ExecStart=/bin/bash "%h/supymem/scripts/run_local.sh"
Restart=on-failure
RestartSec=5
TimeoutStopSec=150
KillMode=mixed
UMask=0077

[Install]
WantedBy=default.target
```

User services do not load your interactive shell profile. If you installed tools
through a version manager, add `Environment="PATH=..."` using the absolute tool
directories from your machine, not shell variables. Check that `python3` is 3.12
and `node` is 22+ on that path. The launcher checks them before installing.
It reuses unchanged locked dependencies and installs them when their lock inputs change. An uncached install needs network access or your approved package cache. This is a source service, not an installed desktop payload.

`KillMode=mixed` signals the launcher first, letting its existing handler drain
and stop its child process groups. The timeout leaves room for four 30-second
waits (core, worker, bridge and console) and final cleanup.
Stop any older five-service setup before starting this one; do not run both.

## Start, stop, and diagnose

```bash
systemctl --user daemon-reload
systemctl --user enable --now supymem-local.service
systemctl --user status supymem-local.service
```

Open `http://127.0.0.1:3100/setup`, unless you changed the launcher ports in `.env`.
If startup fails, inspect logs privately, fix the reported prerequisite or setting,
then restart. Do not share your environment file or unredacted logs.

```bash
journalctl --user -u supymem-local.service -n 50
systemctl --user restart supymem-local.service
systemctl --user disable --now supymem-local.service
```

Stopping the service does not delete saved data. Stop it and separately launched
writing tools before [backup or restore](backups-security.md).
To keep it running after logout, explicitly opt in with
`loginctl enable-linger "$USER"`; undo with `loginctl disable-linger "$USER"`.
If your system refuses, ask your administrator or stay logged in while it runs.
Never disable authorization, consent, charging or outreach checks to fix startup.
