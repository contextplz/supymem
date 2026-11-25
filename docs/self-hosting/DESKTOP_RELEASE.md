# Your desktop release contract

Use [the desktop guide](DESKTOP.md) to build and [Releasing](RELEASING.md) for release gates. This contract describes the local configuration, not hosted bridge compatibility.

## Build and launch switches

`./start` opens the source desktop. `./start --build` builds your platform's installer; `./start --web` opens the browser path; `./start --check` inspects desktop prerequisites. Use `.\start.ps1` with those same options in Windows PowerShell. The local Tauri configuration is `src-tauri/tauri.local.json`, product **Supymem Local**. Run `python3 scripts/desktop_version.py check` for the app version.

## Exact payload per target

The build hook downloads checksum-verified standalone Python and Node and packages the existing API, worker, migrations, and Next.js standalone server. [The resource table](DESKTOP.md#payload-and-startup) gives paths. Mac universal packages keep separate ARM and Intel Python/Node/native-library runtime directories; each Rust slice selects its matching target. Windows uses native executable and library paths. Linux retains system GTK, WebKit, and glibc requirements.

## Process and health contract

The supervisor reserves free loopback ports, checks instance-specific API and worker readiness, then opens the console. It owns all three processes and stops them on quit or startup failure. Data and logs live outside the bundle. The owner credential, exact-origin boundary, keychain or key-file storage, and saved connection consumers are part of [the security model](SECURITY_MODEL.md).

## Local updates and release publication

The local config disables hosted updater endpoints and updater artifacts. You always install updates manually. If your build has a public release repository configured, open Check for updates and allow GitHub update checks to look for stable releases. The check does not offer release candidates or install anything. For a release candidate, download the installer from your release page. Database preparation creates a backup before migrations; a failed backup or migration stops readiness. Full backups retain encrypted settings but exclude the local encryption key file; preserve recovery material separately. [Recovery](backups-security.md) stages replacement and keeps the previous folder.

Build public installers from the scanned public export. Ad-hoc signing does not notarize a Mac app. Follow [release gates](RELEASING.md#release-gates) before publishing.

## Startup diagnostics

Private logs live under your data folder's `logs/`. Use [troubleshooting](troubleshooting.md) for loading, bundled-runtime, authorization, and settings failures. Never attach raw logs or key files to a report.

## Known issues, November 18, 2026

Customer search and requested scans are available. Both completed in offline fixture checks on November 18, 2026, but did not complete in live checks on November 15, 2026. Follow [Known issues](../../README.md#known-issues) to retry with your settings and verify completion. Earlier failed pages are not publishable; check new builds through your connected coding tool. Installed GUI, native keyring, and clean-machine acceptance still need release checks.
