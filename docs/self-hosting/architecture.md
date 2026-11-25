# Local architecture

Your Tauri desktop owns the Python API, worker, and Next.js standalone server. The source browser launcher starts the same API and worker with a Next.js development server. Both use SQLite, local files, and saved connections. See [the architecture diagram](../../README.md#how-it-runs).

## Startup and shutdown

The desktop reserves available loopback ports, checks instance-specific API and worker readiness, and opens the console after startup. It does not attach to unrelated services. Quitting stops its owned processes. `./start --web` defaults to API 3300, worker health 3301, and console 3100; those ports must be available. Keep its terminal open and use Ctrl+C to stop.

Requested work and schedules run while the app is open. Laptop sleep pauses execution. Inspect the existing job before repeating a send or deployment.

## Settings, identity, and storage

API and worker read the shared settings and secrets, importing at startup and on settings reads. [Keys](keys.md) gives exact precedence. Owner switches and connection readiness determine local capabilities, not hidden flags or administrator approvals.

Your installation creates a private `owner-session.secret`. Console and shell establish an owner session with that credential. Private API calls require it and exact allowed origins; loopback alone does not authenticate another process. [Remote access](SECURITY_MODEL.md#remote-access) does not grant owner authority.

Desktop data lives outside the bundle; source browser data defaults to `data/runtime/local` in the checkout. See [OS paths](install.md#your-data-folder). Keys use the OS keychain or encrypted files with private encryption material. Full backups exclude the local key file; preserve it separately. Operational logs and spending records remain; product analytics and error-event reporting are disabled.

## External calls and unfinished paths

Built-in reading requests public sites; built-in search sends queries to DuckDuckGo. Optional models, search, mail, publishing, and Composio receive their requested inputs. Your public page host is separate from your local console.

Your connected coding tool builds your landing pages, product, fixes and small tools on your laptop using your own subscription. Charlie writes the brief, starts the work you ask for, watches it, checks the result and shows it. Supymem does not write your code. In **Settings > Keys and connections > Coding tools**, follow the [coding-tool setup and supported builds](../../README.md#build-with-your-own-coding-tool) using your own login. Watch or stop work in **Build activity** and choose **Open result folder** in your desktop app. Nothing is published unless you ask.

See [Known issues](../../README.md#known-issues) for customer search and scan results and how to retry a failed page build. See [backup recovery](backups-security.md) before moving your data.
