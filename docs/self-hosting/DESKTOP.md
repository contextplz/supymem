# Your local desktop app

You build and run the local Tauri configuration, not the hosted desktop. [Installation](install.md) is the user setup guide; [Known issues](../../README.md#known-issues) lists unfinished live paths as of November 15, 2026.

## Build

From your checkout, run `./start --check`, then `./start` for your desktop window or `./start --build` for your machine's installer. On Windows use `.\start.ps1 --check`, install the tools it lists, then run `.\start.ps1` or `.\start.ps1 --build`. Your launcher sets up pnpm through Corepack.

The preparation hook is `scripts/desktop/prepare_local_stack.cjs`, which runs `prepare_local_stack.py`. It downloads pinned standalone Node 22.23.4 and CPython 3.12.16, verifies repository checksums, installs locked packages, and builds the Next.js standalone server. Do not copy a Homebrew runtime into the installed payload. Source browser mode uses its separate `.venv` and source Node.

## Payload and startup

| Resource under `src-tauri/resources` | Purpose |
|---|---|
| `local/runtime/<Rust target>/` | Standalone Python, Node, installed dependencies, runtime licenses, and dependency inventory. Unix executables are in `bin/`; Windows uses `python.exe` and `node.exe`. |
| `local/packages/`, `local/services/`, `local/config/`, `local/third_party/` | Shared packages, API, worker, migrations, and required resources. |
| `local/console.cjs` | Console process wrapper and parent supervision. |
| `console/apps/console/server.js`, `.next/`, `node_modules/`, `public/` | Next.js standalone server, traced dependencies, and browser assets. |

`src-tauri/tauri.local.json` selects local-stack and custom-protocol features, bundle identity `com.supymem.local`, product name **Supymem Local**, local resources, and no hosted updater endpoints or bridge sidecars. Your source launcher selects this configuration. Run `python3 scripts/desktop_version.py check` for the app version.

The supervisor runs API, worker, and console, reserving free loopback ports and checking instance-specific readiness before opening the interface. Core and worker use `desktop_runtime.__main__`. Data lives outside the bundle at the [OS-specific paths](install.md#your-data-folder); quitting stops owned services. No Redis, Firecrawl, or external database is required for the default local stack. Built-in reading and keyless search are default; [saved settings](keys.md) configure optional accounts.

## Headless packaged startup check

With a built or unpacked executable and its resource folder, run:

```sh
python3 scripts/desktop/smoke_local_stack.py /absolute/path/to/company-os-desktop /absolute/path/to/resources
```

The script invokes the app's local-stack smoke path. It checks two disposable data folders, API/worker/console readiness, setup, and clean shutdown without keys or provider calls. It is not a GUI, real-model, mailbox, or full-feature acceptance test. Do not use your real data folder for smoke testing.

## Releases

In the public checkout, CI uses the locked `./start` source dependencies to run local unit and console checks, then checks the Tauri code on Linux without bundling. To build an installer on your machine, run `./start --build` (or `.\start.ps1 --build` on Windows). Follow [Releasing](RELEASING.md) and [acceptance](ACCEPTANCE.md) to verify the installed app before sharing it.
