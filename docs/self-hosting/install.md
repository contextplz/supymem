# Install Supymem

## Start with one command

From your source checkout, run `./start` on Mac or Linux, or `.\start.ps1` in Windows PowerShell. `./start --check` checks desktop prerequisites; use `./start --check --web` for browser prerequisites or `./start --check --build` for installer prerequisites. `./start --yes` accepts the Unix prerequisite installs without a prompt. Enter keys on **Keys and connections** or in the data-folder `.env` shown there, never chat or terminal commands.

Unix setup uses Bash and your OS base tools. Use a checkout and home directory you can write to. On Linux you may need `sudo` to install system packages; ask your administrator to run the printed package command if you lack permission. On Mac, complete Apple's tools installer and approve Homebrew installation if prompted. Use `./start --web` on a headless Linux machine.

Setup lists missing prerequisites and asks before installing them. A successful `--check` means the prerequisites are ready, not that anything was built. For a first run behind a proxy, configure the approved system proxy and trust its CA certificate before retrying. If you are offline and the dependencies are not cached, reconnect to finish setup. Do not disable certificate verification.

| Platform | Prerequisite installation |
|---|---|
| macOS | After you accept: Homebrew if needed, Apple command-line tools if missing, Python 3.12, Node 22 or newer, pnpm 10.11.0 through Corepack, Rust through rustup, and the Tauri CLI version in `src-tauri/Cargo.lock`. Finish Apple's installer when it opens. |
| Debian/Ubuntu, Fedora, Arch | After you accept: download tools and distribution Tauri build libraries, Python 3.12 through uv if missing, Node 22 or newer, pnpm 10.11.0 through Corepack, Rust through rustup, and the matching Tauri CLI. Listed package-manager commands use administrator permission where needed. |
| Windows | `.\start.ps1 --check` lists missing Python 3.12, Node 22 or newer, Visual Studio C++ Build Tools, Windows SDK, WebView2 Runtime, Rust, and Tauri CLI, with steps for WinGet or direct downloads. Install what it lists, open a new PowerShell window, then retry. Your launcher sets up pnpm 10.11.0 through Corepack. |

If PowerShell blocks the script, run `Set-ExecutionPolicy -Scope Process Bypass` in your current PowerShell window, then rerun your command there. This only changes the policy for that window.

Browser mode skips Rust, Tauri, and native desktop checks. On Windows, run `.\start.ps1 --web` with Python 3.12 and Node 22 or newer, then open the printed loopback URL. Press Ctrl+C in PowerShell to stop it.

## What setup downloads

The source browser launcher creates a Python 3.12 `.venv`, installs the hashed runtime, build, and desktop package locks, and installs repository packages without resolving new dependencies. Source setup installs the interface packages from the frozen pnpm lock. Desktop setup also runs the desktop preparation hook.

Desktop preparation downloads **Node 22.23.4** from nodejs.org and **CPython 3.12.16**, build **20261026**, from Astral's python-build-standalone release. It verifies repository-pinned SHA-256 checksums and builds a separate Python environment from that standalone interpreter. The payload includes Python, Node, API, worker, migrations, the Next.js standalone server, and browser assets. It does not bundle your Homebrew Python. These downloads happen for desktop development as well as `--build`; source `--web` uses your source Python environment and Node instead.

The Unix prerequisite Node download also verifies the official release checksum. Desktop runtime archives are cached under your user cache's `desktop-runtime/node` and `desktop-runtime/python` folders. Rust and native build tooling are not installed-app Python or Node requirements.

## Desktop window and installer

`./start` runs the local desktop app from source. `./start --build` creates an `.app` and `.dmg` on Mac, `.deb` and `.AppImage` on Debian/Ubuntu, and `.AppImage` on Fedora or Arch under `src-tauri/target/release/bundle`. Source setup supports x64 and arm64 on Mac and Linux. Run the Windows NSIS source build with `.\start.ps1 --build`. For distribution, follow the [manual public build](../RELEASING.md#build-and-attach-installers). A source build is not proof of publication or clean-machine testing.

If you have a package, install it through your OS and open **Supymem Local**. On Mac, copy the app into Applications. Ad-hoc builds are not notarized; use explicit OS approval, never disable Gatekeeper. Python and Node are in the payload. Linux still needs system WebKitGTK, GTK, AppIndicator, and librsvg; see [Linux packages](linux-service.md). Windows bundles an offline WebView2 installer.

## Your data folder

| Run | Default folder | Default main SQLite file |
|---|---|---|
| macOS desktop | `~/Library/Application Support/Supymem Local` | `database.sqlite3` |
| Linux desktop | `$XDG_DATA_HOME/Supymem Local` when the XDG data path is absolute; otherwise `~/.local/share/Supymem Local` | `database.sqlite3` |
| Windows desktop | `%LOCALAPPDATA%\Supymem Local` | `database.sqlite3` |
| Source browser launcher | `<checkout>/data/runtime/local` | `local.db` |
| Docker | Persistent `local-data` volume mounted at `/workspace/data` | `local.db` |

`working_memory.sqlite3` is separate; desktop logs are under `logs/`. Full backup archives default to a sibling folder named `<data-folder>-backups`. Check your actual folder on **Settings > Keys and connections > Your data folder and backup** before copying anything. If the app cannot open, follow [stopped-app recovery](LOCAL_WIRING.md#stopped-app-recovery).

For `./start --web`, set `COMPANY_OS_DATA_DIR` in your checkout `.env` or terminal environment to use an existing data folder. The launcher opens SQLite only. Remove any hosted database settings it names before retrying. If your folder contains two databases, set `COMPANY_OS_DB_URL` to the SQLite file you want. Changing the path does not move your data; [back up and recover](backups-security.md) before moving it. If a port is occupied, change the named `CORE_PORT`, `WORKER_HEALTH_PORT`, or `CONSOLE_PORT` setting before restarting.

## Docker: Mac, Linux and Windows

Coding tools you signed in to on your computer are not visible inside Docker. To build with your existing login, use the [desktop app](#desktop-window-and-installer) or run `./start --web` on your computer (`.\start.ps1 --web` on Windows) instead of Docker. Then choose **Check connection** under **Settings > Keys and connections > Coding tools**. Do not mount your coding-tool credentials into Docker.

Docker is optional, not a desktop prerequisite. Use Compose v2.24 or newer and a running Docker engine. From the source root:

```sh
docker compose up --build -d
```

The default Compose configuration selects `docker-compose.local.yml`. Open `http://127.0.0.1:3100/setup` and use the console proxy, not a direct API call to 3300. Default services are core, worker, and console with persistent SQLite; built-in research needs no Firecrawl profile. Save keys in **Settings > Keys and connections**. A checkout `.env` is optional for Compose overrides. If you need one, copy `.env.example` to `.env`, restrict file access to your account, and never commit it. Stop with:

```sh
docker compose down
```

Do not add `--volumes`: it deletes your persistent workspace.

If an older Windows checkout fails to start the Linux entry script, preserve any edits to `docker/entrypoint.sh`. With no edits to keep, in PowerShell run `Remove-Item -LiteralPath docker/entrypoint.sh`, then `git restore --worktree -- docker/entrypoint.sh` from your updated checkout and retry `docker compose up --build -d`. Do not remove your Docker volumes.

### Optional Firecrawl for Docker

If you already run Firecrawl, leave the Compose profile off. Choose **Firecrawl** in **Settings > Keys and connections > Your search and page reader**, enter the server URL and its key if required, then save and test the connection. In Docker, use a URL reachable from the API container. Keep **Built-in** selected if you do not want to set up a server.

To start Firecrawl with Compose, first supply your own upstream Firecrawl `v2.11.0` checkout; the public source does not include `vendor/firecrawl`. In your private `.env`, point `FIRECRAWL_SOURCE_DIR` to that checkout and `FIRECRAWL_SEARCH_CONFIG_DIR` to a directory containing SearXNG `settings.yml` with JSON enabled in `search.formats`. Set `FIRECRAWL_POSTGRES_PASSWORD` and `FIRECRAWL_BULL_AUTH_KEY` there before running `docker compose --profile firecrawl up --build -d`. Then use `http://127.0.0.1:3002` as the Firecrawl URL in the Docker app and test the connection there.

## Minimal Unix launcher

Run `./start --web`, then open its printed loopback URL. API defaults to 3300, worker health to 3301, and console to 3100. These ports must be distinct and available. Keep the terminal open; Ctrl+C stops its services. Follow [your first venture](first-venture.md) after setup, or see [Known issues](../../README.md#known-issues) if work stalls.
