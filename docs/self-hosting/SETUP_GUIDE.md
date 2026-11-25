# Supymem setup guide

This guide takes you from nothing to a working Supymem on your own computer, one step at a time. Each step links to the detailed reference if you need more.

**About 20–40 minutes the first time**, most of it waiting for downloads. You need an internet connection for the first setup.

---

## 1. Pick how you want to run it

| Option | Best for | What you need |
|---|---|---|
| **Desktop app from source** (`./start`) | Most people on Mac or Linux | Python 3.12, Node 22+, Rust. The launcher offers to install them. |
| **Browser mode** (`./start --web`) | A quick try, headless Linux, or no Rust | Python 3.12 and Node 22+ only |
| **Windows** (`.\start.ps1`) | Windows 10/11 (developer preview) | Python 3.12, Node 22+, and for the desktop also Rust, Visual Studio C++ Build Tools, Windows SDK, WebView2 |
| **Docker** (`docker compose up`) | Running it as a service | Docker with Compose v2.24+. Your signed-in coding tools are **not** visible inside Docker. |
| **Installer** (`.dmg`, `.AppImage`, `.deb`) | Not ready yet | Installers are not published yet. Build one yourself with `./start --build` (step 2). |

If you're unsure, use **Desktop app from source** on a Mac, or **Browser mode** on anything else.

---

## 2. Get the code and start it

```sh
git clone https://github.com/contextplz/supymem.git
cd supymem
```

**Mac or Linux:**

```sh
./start --check   # optional: lists what's missing, installs nothing
./start           # installs what's missing (after asking), then opens the desktop app
```

- Add `--yes` to accept the install prompts automatically.
- On a Mac, finish Apple's command-line tools installer if it pops up, then run `./start` again.
- On Linux you may be asked for your password (sudo) to install system packages. Ubuntu, Debian, Fedora and Arch are supported automatically.

**Browser mode instead (no Rust needed):**

```sh
./start --web
```

Open the address it prints (normally `http://127.0.0.1:3100/setup`). Keep the terminal open; press Ctrl+C to stop.

**Windows (PowerShell):**

```powershell
.\start.ps1 --check
.\start.ps1          # desktop
.\start.ps1 --web    # browser mode
```

If PowerShell blocks the script, run `Set-ExecutionPolicy -Scope Process Bypass` in that window and try again.

**Docker:**

```sh
docker compose up --build -d
```

Open `http://127.0.0.1:3100/setup`. Stop with `docker compose down`. **Never** add `--volumes`, which deletes your data.

**Build your own installer (optional):**

```sh
./start --build
```

Find the `.app`/`.dmg` (Mac) or `.AppImage`/`.deb` (Linux) under `src-tauri/target/release/bundle/`. Self-built Mac apps are not notarized: approve them in System Settings > Privacy & Security, and never turn off Gatekeeper.

More detail: [install guide](install.md).

---

## 3. Connect an AI model (required for real results)

When the app opens you see the setup screen. You have three choices:

1. **Try without a key (sample mode).** Good for looking around. All results are made up, so never use them as evidence.
2. **Your own cloud AI account.** Go to **Settings > Keys and connections > Your AI provider**:
   - **OpenAI:** a key from [platform.openai.com/api-keys](https://platform.openai.com/api-keys) and a model name.
   - **Azure OpenAI:** your resource endpoint as **Service URL**, your *deployment name* as **Chat model**, and your key.
   - **Anthropic:** a key from [console.anthropic.com](https://console.anthropic.com/settings/keys) and a model name.
   - **OpenRouter or another OpenAI-compatible service:** choose **OpenAI-compatible**, then enter its URL, model and key.
3. **A model on your own computer (fully private).** Start Ollama or LM Studio first, then choose:
   - **Ollama (on your machine)**, URL `http://localhost:11434/v1`, or **LM Studio (on your machine)**, URL `http://localhost:1234/v1`.
   - Enter the exact installed model name (for Ollama, include the tag, e.g. `llama3.1:8b`) and leave the API key blank.
   - Set the server's context window to at least **8192** and enter the same number in **Context window**.

Then choose **Save** and **Test your connection**. The test makes one real model call, which your provider may charge for.

Keys go in this screen, **never in the chat**. They are stored in your OS keychain or an encrypted file.

More detail: [keys and connections](keys.md#your-ai-provider).

---

## 4. Set a spending limit (recommended)

**Settings > Keys and connections > Your monthly spend cap.** Enter a monthly limit in USD. Supymem checks it before every paid call. Set a budget with your AI provider too, because this cap is a safety net, not a billing guarantee.

---

## 5. Optional connections: add only what you need

All of these are under **Settings > Keys and connections**. After saving each one, choose **Test your connection**, then switch the matching feature on in **Settings > Capabilities**.

| Connection | What it unlocks | What you need |
|---|---|---|
| **Your search and page reader** | Better research and customer search | Nothing by default: built-in search works without a key. Optionally a [Tavily](https://app.tavily.com/) or [Exa](https://dashboard.exa.ai/) key, OpenAI search (reuses your OpenAI key), or your own Firecrawl server. |
| **Coding tools** | Charlie can build your landing page or product | Codex, Claude Code, Cursor or OpenCode, signed in with **your own subscription**. See step 6. |
| **Your mail account** | Sending outreach and reading replies | SMTP (and IMAP for replies) details from your mail provider, an **app password** (not your main password), a sender name and a postal address. For Gmail, turn on 2-Step Verification and create an [app password](https://support.google.com/accounts/answer/185833). |
| **Your page publishing account** | Publishing a page to the web | A Vercel token and project, a Netlify token and site ID, or an S3 bucket with an HTTPS site URL. |
| **Your connected apps** | Actions in other apps through Composio | Your Composio project and authorization IDs. |

Safety rules always stay on for outreach: every email says where the address was found, includes an opt-out and your postal address, and respects suppression lists and daily caps. Supymem never guesses email addresses.

**Test your mail safely:** after setting it up, send a test to yourself from **Customers** before contacting anyone.

---

## 6. Connect your coding tool (to build pages and products)

Charlie writes the plan; **your** coding tool does the actual building, on your computer, with your own subscription.

1. Install and sign in to one of the supported tools the normal way:
   - **Codex** or **Claude Code**: sign in with your subscription.
   - **Cursor**: needs a CLI version that supports its sandbox.
   - **OpenCode 2.2.5+ (2.x)**: sign in with the built-in OpenAI, Anthropic or GitHub Copilot login. An API-key login blocks builds: run `opencode auth logout` to remove it, then `opencode auth login`.
2. In Supymem, open **Settings > Keys and connections > Coding tools** and follow **How to sign in**.
3. Choose **Check connection**, then **Allow Charlie to use** your tool, and approve the detected location once.
4. **Windows only:** install Git for Windows in a protected folder (e.g. `C:\Program Files\Git`), add its `cmd` folder to `PATH`, restart Supymem, and check the connection again.

Coding tools only work in the desktop app or browser mode, **not in Docker**. While a build runs, keep the app open, follow it in **Build activity**, and choose **Open result folder** to see the files. Nothing is published unless you ask.

More detail: [coding tools and their safety limits](SECURITY_MODEL.md#connected-coding-tools).

---

## 7. Your first venture (about 10 minutes)

1. Choose **New venture** and describe your idea in plain words: what it is, who it's for, the problem and any constraints. You don't need a company name.
2. Answer Charlie's clarifying questions, or skip the ones you can't answer yet.
3. Ask one narrow research question, e.g. *"Who are the main competitors for X, and what do customers complain about?"* Check the sources it links.
4. Ask Charlie to **find customers**. Review the list: each contact shows where it was found.
5. Ask for **a small experiment** with a measurable result. Run it, then tell Charlie what actually happened.
6. When you're ready, ask Charlie to **build a landing page**. Review it in **Website**, export the ZIP, or publish it once a publishing account is set up.

Open **How to use Supymem** in the app for more example prompts. More detail: [your first venture](first-venture.md).

---

## 8. Using a `.env` file instead of the screens (optional)

If you prefer a file:

1. Copy [`.env.example`](../../.env.example) to `.env` in **the data folder shown in Keys and connections**. The desktop app reads only that file; the source browser launchers can also read a `.env` in the checkout.
2. Fill in only the values you need, for example `SUPYMEM_LLM_PROVIDER`, `SUPYMEM_MODEL_CHAT`, and `OPENAI_API_KEY` or `AZURE_OPENAI_API_KEY` with `AZURE_OPENAI_ENDPOINT`.
3. Restart the app. Settings saved in the app win over the file.
4. Make the file readable only by you, and **never commit it**. Once the values are saved in the app, you can delete the secrets from the file.

Every accepted name: [keys reference](keys.md).

---

## 9. Your data, backups and updates

| Where you run it | Your data folder |
|---|---|
| Mac desktop | `~/Library/Application Support/Supymem Local` |
| Linux desktop | `~/.local/share/Supymem Local` |
| Windows desktop | `%LOCALAPPDATA%\Supymem Local` |
| Browser mode from source | `data/runtime/local/` in the checkout |
| Docker | the `local-data` volume |

- **Back up:** **Settings > Keys and connections > Your data and recovery > create a backup.** Keep the separate local key file safe too, because full backups leave it out on purpose.
- **Restore:** importing a backup ZIP **replaces** your workspace (your current data is backed up first).
- **Privacy:** no telemetry. Your workspace and database stay on your computer. Only the AI provider, search, mail or coding tool you choose receives what it needs for the work you ask for.
- **Update:** `git pull`, then run `./start` again. Update checks inside the app are off unless you turn them on.

More detail: [backups and security](backups-security.md).

---

## 10. If something goes wrong

| What you see | What to do |
|---|---|
| A tool is still missing | Run `./start --check` (or `.\start.ps1 --check`), install what it lists, open a new terminal, and retry. |
| Downloads fail | Check your internet connection, proxy certificates and free disk space. Never turn off certificate or checksum checks. |
| The desktop app is stuck loading | Quit it and run `./start --web` instead, then open the printed address. |
| "Address already in use" | Quit other copies of the app. For browser mode, set unused `CORE_PORT`, `WORKER_HEALTH_PORT` or `CONSOLE_PORT` values in your checkout `.env`. |
| Results look made up | You are in sample mode. Connect a real model (step 3) and test it. |
| A feature says **Off** or **Needs setup** | Open **Settings > Capabilities**. It names the missing setting. |
| Charlie can't start a build | Connect a coding tool (step 6). Docker can't see your signed-in tools. |
| Customer search or a scan stalls | Check **Research** and **Find customers** in **Settings > Capabilities**, then retry. See [known issues](../../README.md#known-issues). |

Full list: [troubleshooting](troubleshooting.md). Found a security problem? Report it privately as described in [SECURITY.md](../../SECURITY.md).

---

## 11. Stop or remove Supymem

- **Stop:** quit the desktop app, press Ctrl+C in the browser-mode terminal, or run `docker compose down`.
- **Remove:** delete the checkout folder. Your data stays in the data folder (step 9) until you delete that yourself, so back it up first if you might want it later.
