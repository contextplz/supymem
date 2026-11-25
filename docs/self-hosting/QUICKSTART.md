# Your local Supymem app

## Start with one command

Run `./start` from your Mac or Linux source checkout to prepare the app and open your desktop. Unix setup lists missing tools and asks before installing them. `./start --yes` accepts that prompt. `./start --check` checks desktop prerequisites without installing or launching; `./start --build` builds an installer with checksum-verified standalone Node and Python downloads; `./start --web` starts the browser interface without Rust or desktop webview tools.

On Windows, run `.\start.ps1` for the desktop or `.\start.ps1 --web` for the browser. Browser mode needs only Python 3.12 and Node 22 or newer. Use `.\start.ps1 --check` to list desktop prerequisites or `.\start.ps1 --build` to build an installer. Use **Keys and connections** to save keys or import your private data-folder `.env`. See [installation](install.md) for downloads and data locations.

## Choose your model

Choose **Try without a key (sample mode)** for fictional results, or save your model provider, model name, endpoint, and key. For a model already running on your laptop, choose OpenAI-compatible, its loopback `/v1` URL, and installed model name. A keyless loopback model needs no cloud key. Choose **Test your connection** before using real generated answers.

Keys belong on **Settings > Keys and connections**, not in chat. [The connection reference](keys.md) lists every accepted import name and per-field precedence. Sample mode does not require developer environment flags.

## Do one inspectable task

Create a venture and describe your idea. Ask Charlie to summarize it, then request one narrow research question. Built-in search and reading use the public web without a key. Check sources yourself. Ask for a small experiment plan and record your result.

Your request authorizes that work; saving a connection or enabling a switch does not authorize unrelated work. Outreach keeps disclosure, opt-out, postal address, suppression, permission checks, and caps; guessed emails stay off.

## Connect your tool before building

In **Settings > Keys and connections > Coding tools**, follow the [coding-tool setup and supported builds](../../README.md#build-with-your-own-coding-tool) using your own login.

Charlie offers a build when it is your next useful step. When you ask, Charlie writes the brief, starts your tool, watches it, checks the result and shows it. Your tool builds your landing pages, product, fixes and small tools on your laptop. Start with a static page or small local web app; ask Charlie to split a larger product into smaller steps.

Watch or stop work in **Build activity** and choose **Open result folder** in your desktop app. If no tool is connected, Charlie offers [setup](keys.md#coding-tools), not another builder. Nothing is published unless you ask.

## Known issues

You can ask Charlie to find customers or run a scan. See [Known issues](../../README.md#known-issues) for what completed offline, what remains to check live, and where to retry or rebuild a failed page.

For a desktop stuck on loading, quit the app and run `./start --web` on Mac or Linux, or `.\start.ps1 --web` in Windows PowerShell. Open the exact printed loopback URL. See [troubleshooting](troubleshooting.md) if it still stalls. The browser launcher has a different default data folder. [Protect a backup](backups-security.md) before changing paths.
