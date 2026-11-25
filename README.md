# Supymem

Supymem is a local desktop app for working with Charlie on your idea, research, and experiments. Your workspace lives on your computer. You bring your own model key and accounts, or try the app with sample data.

## Start from source

From your source checkout, run `./start` on macOS or Linux, or `.\start.ps1` in Windows PowerShell.

| Command | What it does |
|---|---|
| `./start` | Opens the desktop app and starts its local services. |
| `./start --web` | Runs the local services in your browser without Rust or desktop libraries. Open the printed URL, normally `http://127.0.0.1:3100/setup`. Press Ctrl+C to stop. |
| `./start --build` | Builds an `.app` and `.dmg` on macOS or an `.AppImage` on Linux (plus a `.deb` with `apt-get`). Find them under `src-tauri/target/release/bundle/` by default. |
| `./start --check` | Checks desktop prerequisites without downloading or installing anything. Use `./start --check --web` for browser prerequisites or `./start --check --build` for packaging prerequisites. Add `--yes` to a launch or build command to accept the listed installs. |
| `.\start.ps1` | Opens the Windows desktop app. `.\start.ps1 --check` checks desktop prerequisites; `.\start.ps1 --build` creates an NSIS installer. |
| `.\start.ps1 --web` | Runs the app in your browser without Rust, WebView2, or Visual Studio build tools. Open the printed URL, normally `http://127.0.0.1:3100/setup`. Press Ctrl+C to stop. |

The first source setup needs an internet connection and trusted CA certificates for package downloads. Run from a checkout you can write to.

- **macOS and Linux (arm64 or x64):** The launcher checks for Python 3.12, Node 22 or newer, and pnpm 10.11.0 (also available through Corepack). Desktop launch and `--build` also need Rust and the matching Tauri CLI, plus Xcode Command Line Tools on macOS or WebKitGTK and native build libraries on Linux. The launcher offers to install missing tools, then installs the locked app packages. Finish Apple's tools installer if prompted. You may need administrator access for Homebrew or Linux system packages. Automatic Linux package setup supports Ubuntu, Debian, Fedora, and Arch.
- **Windows (x64):** Use PowerShell 5.1 or newer. Install Python 3.12 and Node 22 or newer with npm as directed by the launcher. For desktop builds or launch, also install Rust, the matching Tauri CLI, Visual Studio C++ Build Tools, the Windows SDK, and WebView2. Open a new PowerShell window after installing them, then rerun your command. The launcher uses Corepack for pnpm and installs app packages, not desktop prerequisite tools.
- **If you already have an installer:** Install it through your OS and open **Supymem Local**. Python and Node are bundled, and you do not need Rust. On Linux, install the [system webview libraries](docs/self-hosting/linux-service.md) if missing.

## Connect your accounts

On the setup screen, connect your model or choose **Try without a key (sample mode)**. Sample results are fictional. You can also connect an OpenAI-compatible model running on your computer without a cloud key.

- In **Settings > Keys and connections**, save and test your model, mail, or other accounts as needed. Search and page reading work without a key by default. Tavily, Exa, OpenAI search using your saved model key, and your own Firecrawl server are optional. In **Settings > Capabilities**, turn features on or off and check what each one needs.
- For a model on your computer, open **Settings > Keys and connections > Your AI provider**. Choose **Ollama (on your machine)** with service URL `http://localhost:11434/v1`, or **LM Studio (on your machine)** with `http://localhost:1234/v1`. Start your server, enter the exact installed model ID (including its tag for Ollama) as **Chat model**, and leave the API key blank. Set the server's context window to at least `8192` tokens and use the same **Context window** in Supymem. Save, then choose **Test connection**.
- To import from a file, start with [`.env.example`](.env.example). Save it privately as `.env` at the path shown in **Keys and connections**, fill in the fields you need, and restart. The source browser launchers can also read a checkout `.env`. The desktop reads only its data-folder `.env`. Settings you save in the app take precedence. To stop keeping a plaintext copy, save the imported settings in the app before removing the secret entries from the file. Never commit the file. See the [accepted key names](docs/self-hosting/keys.md#your-ai-provider).

## Build with your own coding tool

In **Settings > Keys and connections > Coding tools**, follow **How to sign in** with your own subscription login. Choose **Check connection**, then **Allow Charlie to use** your tool. Review the detected location and approve it once. Codex, Claude Code, Cursor and OpenCode support builds. Cursor requires a CLI that supports its sandbox.

On Windows, builds also need git.exe on `PATH`. Install Git for Windows in a protected folder (such as `C:\Program Files\Git`), add its `cmd` folder to `PATH`, restart Supymem, and choose **Check connection** again. You do not need Git just to open the installed app.

Use OpenCode 2.2.5 or newer in the 2.x series. Sign in with built-in OpenAI, Anthropic, or GitHub Copilot OAuth. An API-key login blocks builds even if you also have a subscription login. Run `opencode auth logout` to remove each API-key login, then use `opencode auth login` with the default data home and select **Check connection** again. See [OpenCode sign-in commands and build restrictions](docs/self-hosting/SECURITY_MODEL.md#connected-coding-tools), including what to do if you use a custom data home or need your tool to run shell commands.

Charlie decides what to build, writes the brief, starts your tool when you ask, watches it, checks the result, and shows it to you. Your tool does the actual work for landing pages, products, fixes, scrapers, and other code. Without a connected tool, Charlie offers to connect one. Supymem does not fall back to its own builder. Keep the desktop app open to follow or stop work in **Build activity**, then choose **Open result folder** to see the files. Nothing is published unless you ask.

## How to use Supymem

Open **How to use Supymem** in the app for example prompts and links to the right screens.

1. Start with your idea. Answer the clarification questions you can, or continue with what you shared. Ask Charlie to research the market with built-in keyless search and page reading. Add your own Tavily or Exa key in **Settings > Keys and connections > Your search and page reader** for better results.
2. Ask Charlie to find customers. Keyless search finds companies and public contact pages without guessing addresses. Drafts include the source, opt-out and your postal address. Review the drafts before sending. To check SMTP without contacting a lead, set up **Your mail account** and send a test to yourself from **Customers**.
3. Ask Charlie to design an experiment. Run the test you choose, tell Charlie what actually happened and review it in **Experiments**.
4. Ask Charlie to build your landing page or product with your connected coding tool. In **Website**, review the private page or export its ZIP. Publish only after setting up **Your page publishing account** in **Keys and connections** and asking to go live.

## How it runs

The desktop starts and stops the local API, worker, and interface together. Browser mode runs those services from your checkout instead and listens only on your computer.

## Your data

| Where you run Supymem | Default data folder |
|---|---|
| macOS desktop | `~/Library/Application Support/Supymem Local` |
| Linux desktop | `~/.local/share/Supymem Local`, or `Supymem Local` inside your configured XDG data folder |
| Windows desktop | `%LOCALAPPDATA%\Supymem Local` |
| Browser from source | `data/runtime/local/` in the checkout |

To export your data, create a backup, or restore a backup, open **Settings > Keys and connections > Your data and recovery**. Importing a ZIP replaces your workspace; it does not merge it. Your current data is backed up first. To see your data folder or change where backups are saved, open **Your data folder and backup**.

Your workspace and SQLite data stay local. Supymem sends no telemetry, and automatic updates are off by default. The model, coding tool, search, mail, or other service you use may receive inputs needed for the work. Keyless web search still sends your query to the web. Use Ollama, LM Studio or another OpenAI-compatible server on your laptop to keep model requests local.

## Startup troubleshooting

| What you see | What to do |
|---|---|
| Prerequisite still missing | On macOS, finish the Apple tools installer if it opened. On Linux, follow the package command `./start --check` prints. On Windows, install the listed tools and open a new PowerShell window. Then rerun your start command. |
| Node download failed verification or dependency setup stopped | Check your internet connection, trusted CA certificates, and free disk space. Fix the first install error in the terminal, then rerun the same start command. Do not bypass checksum or certificate checks. |
| Desktop runtime or build could not start | Rerun `./start` from source or reinstall your local installer. For a Windows build failure, check Visual Studio C++ Build Tools and the Windows SDK, then rerun `.\start.ps1`. |
| Invalid ports or address already in use | For the desktop, quit and reopen the app. For browser mode, stop the other local service or choose unused, distinct port numbers in your checkout `.env` using [the browser examples](.env.local.example), then restart. |
| Data folder or database could not start | Close other copies of the app, check that your account can write to the data folder, and free up disk space. For browser mode, check the SQLite settings in [the browser examples](.env.local.example). Keep your data and backups, then select **Retry** or rerun your start command. |

## Known issues

Customer search and requested scans are available. Both completed in offline fixture checks on November 18, 2026, but customer searches and requested scans did not complete in live checks on November 15, 2026. Live acceptance is still pending. If your request stalls, open **Settings > Capabilities** and check **Research**, plus **Find customers** for a search or **Run your experiments** for a scan. Follow the setup steps shown, then retry and verify that the work finishes. Partial leads or a scan plan alone are not completed results.

A failed page build cannot be published. Connect a supported tool in **Settings > Keys and connections > Coding tools**, then request a new page. A failed check leaves your previous saved page in place.

For Docker setup, see [installation](docs/self-hosting/install.md#docker-mac-linux-and-windows). Before moving your data, read [backups and security](docs/self-hosting/backups-security.md). For clean-machine and installer checks, see [release acceptance](docs/self-hosting/ACCEPTANCE.md).

Read [Contributing](CONTRIBUTING.md) to work on the app and [Security](SECURITY.md) to report a vulnerability privately. The code is licensed under [Apache-2.0](LICENSE). Keep [NOTICE](NOTICE) and check the [third-party inventory](docs/self-hosting/THIRD_PARTY.md) before redistributing.
