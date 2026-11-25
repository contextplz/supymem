# Your settings and capabilities

Open **Settings > Keys and connections** to save and test your model, search, mail, publishing, connected apps, image tools, spend cap, and backup folder. Use password fields for keys. [Keys and connections](keys.md) lists every field and import name.

Precedence is per field: app saves win; source imports otherwise prefer process environment, data-folder `.env`, then checkout `.env`. Desktop import reads only the data-folder `.env`. Blank imports do not erase saved fields. API and worker import at startup and on settings reads. Restart after a file edit to refresh both processes, then test the connection.

## Your capabilities

Open **Settings > Capabilities**, titled **Your capabilities**. You control each shipped feature. Local switches and connection readiness replace environment feature flags, database policy rows, and administrator approval. Ownership, validation, consent, suppression, and caps still apply.

**On** below is the initial switch, not a live execution result. **Ready** means the switch is on and required settings are present. **Needs setup** lists missing fields. **Off** means the switch is off. The registry returns **Not in this version** outside local mode; the known live failures remain unfinished even if their switches say Ready.

| Capability | Feature | Default | Required setup |
|---|---|---|---|
| `research` | Research | On | Built-in needs no key; optional search needs its selected settings. |
| `find_customers` | Find customers | On | Search and a configured model or sample mode; Research must be on. |
| `send_email` | Send email | Off | Mailbox address, password, outgoing host, valid port, sender name, and postal address. |
| `read_replies` | Read replies | Off | Mailbox address, password, incoming host, and valid IMAP port. |
| `landing_pages` | Draft landing pages | On | Build with your coding tool must be on, with a tool that supports builds. |
| `publish_pages` | Publish pages | Off | Vercel/Netlify project and token, or S3 bucket, region, prefix, HTTPS public URL, access key ID, and secret access key. |
| `page_measurement` | Page measurement | On | No setup in the readiness check. Public collection still needs a reachable configured endpoint; Ready does not provide one. |
| `background_work` | Background work | Off | No connection in the readiness check. Keep the app running for work you requested. |
| `run_for_me_experiments` | Run your experiments | Off | Configured model or sample mode. |
| `routines` | Routines | Off | No connection in the readiness check. Set your chosen schedule; switching it on creates none. |
| `connected_apps` | Connected apps | Off | Composio project key and at least one authorization ID; authorize each account on Connections. |
| `content` | Draft content | On | Configured model or sample mode. Image keys are optional. |
| `build_with_my_tool` | Build with your coding tool | On | A connected, allowed tool with a verified subscription login that [supports builds](../../README.md#build-with-your-own-coding-tool). |

## Connection choices

Research defaults to the built-in reader and DuckDuckGo search. Tavily, Exa, OpenAI search, and your own Firecrawl are optional. Select and test the service under **Your search and page reader**. Built-in is keyless, not offline.

SMTP and IMAP share your mailbox credentials. SMTP defaults to 587 with STARTTLS; 465 uses implicit TLS. IMAP defaults to 993 with TLS. Save sender details before outreach. **Test your connection** checks outgoing mail; **Test incoming mail** checks replies. Neither sends mail. Your booking link is an existing HTTPS scheduling page, not an automatic booking service.

Publishing sends an existing validated page's public files to your host; it does not expose your local console. An email-based waitlist opens the visitor's mail app, not a recorded signup. Test your visitor destination before sharing.

Follow [coding-tool setup](keys.md#coding-tools) in **Settings > Keys and connections > Coding tools** using your own login. Your tool builds your landing pages, product, fixes and small tools on your laptop. Charlie writes the brief, starts the work you ask for, watches it and checks the result. Watch or stop work in **Build activity** and choose **Open result folder** in your desktop app. Nothing is published unless you ask.

Set **Monthly limit (USD)** under **Your monthly spend cap** for the estimated-spend guard. Blank removes your local limit; zero blocks estimated paid calls. Set a provider-side budget too, especially for an account used elsewhere. This is not a provider billing guarantee.

Choose **Full backup folder path** outside your data folder, or leave it blank for the default sibling folder. This moves neither data nor older archives. Use [backup recovery](backups-security.md) before replacing data.

## Known issues

See [Known issues](../../README.md#known-issues) for what to check if a customer search or requested scan stalls, or a page build fails. Ready does not mean the work finished.
