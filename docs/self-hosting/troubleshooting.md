# Troubleshooting

Find your symptom, complete the next step, and retry. Keep your existing data. Never share keys, private configuration, customer data, or unredacted logs.

## The source launcher stops before opening the app

Run `./start --check` in your checkout and follow its prerequisite commands, then rerun `./start`. For the Windows desktop, use `.\start.ps1 --check`, install the listed tools, open a new PowerShell window, and run `.\start.ps1`. For the Windows browser, rerun `.\start.ps1 --web` and follow its Python or Node steps. If PowerShell blocks startup, see [installation](install.md). Uncached dependency downloads and the first compile require network access and disk space.

## The bundled runtime is not self-contained

An older Mac payload copied Homebrew Python or Node and depended on paths outside the bundle. Use the current checkout and rebuild with `./start --build`; desktop preparation downloads checksum-verified standalone Node and Python. Do not repair the installed app by adding symlinks to Homebrew libraries. Keep your data folder and install the rebuilt app only after its packaged startup check passes. Linux still needs the listed system webview libraries; see [installation](install.md).

## The app is stuck on loading

The November 15, 2026 ad-hoc Mac build showed repeated keychain prompts and a loading screen. Quit that app and any duplicate source launcher, unlock your login keychain, and rebuild from the current checkout with `./start --build`. If it still stalls, use `./start --web` and open its printed loopback URL. The source browser default folder differs from your desktop folder; check the path before expecting existing ventures. Keep your saved data and encryption files. Report the app version, OS, and a redacted startup error; do not delete databases to reset loading.

Startup logs are under your data folder's `logs/desktop-local-<instance>.log`. Review them privately. Older builds hid diagnostic detail; use a current build for actionable startup errors.

## You see 403: Open your app on your own machine

Quit stale app copies, rerun `./start --web` on Mac or Linux or `.\start.ps1 --web` in Windows PowerShell, and open the exact URL it prints, normally `http://127.0.0.1:3100/setup`. Do not substitute `localhost`, another port, or a network address. On desktop, quit and reopen the app to establish its owner session. Private routes require the per-install credential and exact allowed origin, not just loopback. Use the console interface rather than calling port 3300 directly. Do not enable remote access or disable authentication as a repair.

If the message says the owner-session file cannot be read, fix your data-folder permissions and restart. Only if that message explicitly asks for session regeneration, stop every app using the folder, remove `owner-session.secret`, and reopen. This file is not your encryption key. Never delete `local-key.bin` or replace encrypted settings to fix authorization.

## You see: Your settings could not be saved or checked

Open **Settings > Keys and connections** at the exact local console URL. Choose **Try again** to reload the screen. Check data-folder permissions, free disk space, and that your keyring is unlocked, then restart the app and retry saving. If only a connection test fails, correct the named key or endpoint in that connection section and test it again.

If encrypted settings cannot be read, keep them and restore the matching settings backup plus your separately saved encryption recovery key, or unlock the original keyring. See [recovery](backups-security.md#restore-and-verify). Do not create a new key file or erase settings to make the form load.

## Your .env values are ignored

Use the data-folder path under **Prefer a file?** on Keys and connections, not an arbitrary checkout file for desktop. Use [accepted names](keys.md), restart, and reopen the screen. App-saved fields win; change them in their connection section, save, and test. Blank imported values never erase saved fields. Source process values also take priority over file values. Use valid JSON for **App authorization IDs**. Keep your input file private.

Save your `.env` as UTF-8, not UTF-16. In Windows PowerShell 5.1, avoid plain `Out-File`, which defaults to UTF-16. For `.\start.ps1 --web`, also check the encoding of the `.env` in your source checkout.

## A feature is Off or Needs setup

Open **Settings > Capabilities**, titled **Your capabilities**, enable the feature, and fill the missing fields on **Keys and connections**. Save and test the connection, then retry the requested work. Send email, Publish pages, and Connected apps start off. Ready is a settings check, not a successful live task. The [known live failures](../../README.md#known-issues) remain not in this version despite Ready badges.

## Model connection fails or output looks like samples

Open **Keys and connections > Your AI provider**. Select your service, exact **Chat model** or Azure deployment, **Service URL**, and **Model API key**. Save and choose **Test your connection**. Sample mode produces fictional results. Remove replay from private startup configuration and restart before live calls. For a local server, start it separately, choose OpenAI-compatible, its loopback `/v1` URL, and an installed model name. A container's loopback is not your laptop's model server. Shorten the request or choose a smaller installed model if local inference times out.

## Research has no results or a page will not load

Under **Your search and page reader**, choose **Built-in**, save, test, and check internet access. Retry a narrower query or a public URL. For Tavily or Exa, correct **Search API key**. For OpenAI search, test **Your AI provider**. For your Firecrawl, correct **Firecrawl URL** and **Firecrawl key**, or switch to Built-in. For login-only or blocked pages, supply relevant text you have permission to use. Do not weaken public-address checks.

## Customer search stops, page QA fails, or a requested scan never runs

In live checks on November 15, 2026, customer search stopped around 75% with partial leads, a page failed its action checks, and a requested demand scan returned a plan instead of running. See [Known issues](../../README.md#known-issues) for newer offline results and the settings to check before retrying. Keep your reviewed page text and existing files until a new build passes its checks.

## Email does not send or replies are missing

Under **Your mail account**, check outgoing host and port, mailbox address and app password, sender name, and postal address. Save, test outgoing mail, and enable **Send email**. SMTP uses STARTTLS on 587 or implicit TLS on 465. For replies, set incoming host and port, normally TLS 993, and choose **Test incoming mail**. Both use the same credentials. Add your HTTPS booking link for invitations. If app passwords are prohibited, obtain an approved SMTP/IMAP credential from your mailbox administrator.

Before retrying an uncertain send, inspect its delivery record and your mailbox to avoid duplicates. Correct permission, disclosure, opt-out, postal address, suppression, and cap failures; never bypass them. A booking link does not prove a call was booked.

## Publishing or signup does not work

Under **Your page publishing account**, select Vercel, Netlify, or S3 and correct its listed fields, save, and test. Enable **Publish pages** on Your capabilities. Use your tool's page after it passes checks, not an earlier failed build. An email waitlist opens the visitor's mail app and does not record a signup automatically. Public measurement needs a reachable collection endpoint; a Ready badge or preview visit is not public visitor evidence. Test your chosen destination before sharing it.

## Charlie cannot start your build

Open **Settings > Keys and connections > Coding tools** and follow the [setup and recovery steps for your tool](../../README.md#build-with-your-own-coding-tool). On **Settings > Capabilities**, turn on **Build with your coding tool**, and **Draft landing pages** for a page, then request your build again. Supymem does not switch to another builder.

Keep your app open and watch the saved request in **Build activity** before retrying an uncertain start. You can stop it there and choose **Open result folder** in your desktop app. Charlie checks your tool's result; a failed check does not replace your saved page. Ask for the named fix, or ask Charlie to split a larger product into smaller steps. Nothing is published unless you ask.

## Import or restore fails or your data seems missing

Check your data folder and matching app version, preserve all copies, and choose an intact compatible archive under **Your data and recovery**. Check staging disk space, then retry. After staging, quit and reopen the app to finish replacement. A data-only ZIP contains no saved keys; full backups exclude the local encryption key file. Restore matching recovery material separately or reconnect your accounts. Verify a venture, document, and connection before deleting any old copy. See [backups](backups-security.md).

## A port or Docker startup fails

For desktop, quit and reopen so it selects free loopback ports. For source browser mode, close only your own competing launcher on 3100, 3300, or 3301, then retry; never kill unrelated services. For Docker, start the engine and use `docker compose -f docker-compose.local.yml ps -a` to check the local stack. Do not delete volumes, drop tables, or mark a failed migration applied.

## You still need help

Report your failing screen or command, OS, app version, and a redacted error through the repository issue tracker. Do not attach private configuration, raw logs, database archives, or customer data. Use [Security](../../SECURITY.md) for vulnerabilities.
