# Local release acceptance

Use this checklist with a disposable data folder and synthetic business and consumer ideas. Do not run acceptance against your real workspace. Record the app version, OS, chosen connections, and the result of each check without recording keys, raw provider responses, or private configuration.

A source review or a passing fixture test is not a completed product journey. Run these checks on the integrated local build, including the installer you intend to distribute. Start with [installation](install.md); use [troubleshooting](troubleshooting.md) for an exact next step when a check fails.

## Known issues

See [Known issues](../../README.md#known-issues) for the offline and live results for customer search and requested scans, and how to retry a failed page build. These checklist items are release gates, not claims that today's workflows pass. Record a failed or unreached step as such; do not check it off based on source wiring or a Ready badge.

## Two audiences

Repeat the venture journey for unrelated audiences: for example, warehouse inventory software for business teams and meal planning for families. Use your own synthetic descriptions. Business discovery must find relevant roles and organizations; consumer discovery must find relevant people or communities rather than silently switching to business buyers. Do not treat synthetic or sample contacts as customer evidence.

## Start and saved settings

- [ ] Start from the installer or `./start`. The interface, API, worker, and SQLite storage start locally without a database server or Firecrawl requirement.
- [ ] Open sample mode without a key, inspect the labelled fictional workspace, and reopen it to check persistence. No outreach, publishing, or unrelated work starts.
- [ ] Import synthetic connection settings from the `.env` location shown in Keys and connections. Verify field statuses without displaying secrets, then save a different value in the app and verify that a later import does not overwrite it.
- [ ] Save and test a real model connection in Keys and connections, reopen the app, and request an answer about each synthetic idea. Sample text does not count as a real model response.
- [ ] Exercise every switch on Settings > Capabilities. Missing settings identify the exact field and screen; ready settings let you run requested work without hidden flags or administrator approval.

## Research and venture work

- [ ] With no search key and Search service set to Built-in, run a query and read a public page. Inspect sources and URL safety checks. A private-network URL remains blocked.
- [ ] With an optional search provider selected, verify that its saved key or saved model connection is used. Replace the saved key and confirm new work uses the replacement without an environment override.
- [ ] For each audience, save an idea summary, ask for one research task, inspect the sources, request an experiment, and record a result. Reopen the venture and check its conversation, sources, and documents.
- [ ] Find relevant potential customers or communities for each audience. Guessed addresses remain off. A missing contact channel gives a concrete safe next step rather than sending to an invented address.
- [ ] Run the experiment or background task you requested while the app stays open. Check status and persistence across restart. Schedule a routine deliberately and verify that no schedule or unrelated task starts simply because you enabled its switch.

## External actions you request

Only use accounts you own. For a live email, use a recipient you have permission to email, such as a second mailbox you control. Connection tests do not authorize live actions.

- [ ] Save SMTP and IMAP in Keys and connections, test both, and enable the email features. Request one send; verify source disclosure or the imported-contact disclosure, opt-out, sender address, suppression, permission checks, and send caps. Verify that a suppressed recipient is not sent mail.
- [ ] Reply from the test mailbox, request reply reading, and verify that the saved IMAP connection is used. Test an opt-out reply before further sending. Save a booking link and request an invitation that uses it.
- [ ] In Settings > Keys and connections > Coding tools, [connect and allow a tool that supports builds](../../README.md#build-with-your-own-coding-tool) using your own login. With no connected tool, verify Charlie offers setup rather than another builder.
- [ ] Request a landing page, product, fix or small local web tool. Verify your tool builds on your laptop, Charlie shows the brief and checks, and nothing starts or is published without your request. Watch, stop and open the result folder from your desktop app. Start with a static page or small local web app; split larger work into smaller steps.
- [ ] Preview your tool's checked page, then export its ZIP without a publishing account. Inspect that only intended public page files are included.
- [ ] Save a publishing destination and enable Publish pages. Request one deployment, open its public URL, and test the actual signup destination. A preview URL or an email link alone is not evidence of a saved signup.
- [ ] Enable Page measurement and verify its supported public-page measurement with a real test visit. Sample and preview traffic do not count.
- [ ] Save the Composio project key and app authorization map, enable Connected apps, authorize one account on Connections, and request one benign action. Saving a project key alone must not authorize an account or run an action.

## Spending, privacy, and recovery

- [ ] Set Monthly limit (USD) to zero and verify a paid call is blocked with the exact setting to change. Raise or remove it deliberately and retry. Provider accounting and limits remain enforced without exposing internal usage units or adding a second confirmation for requested work.
- [ ] Inspect network destinations for model, search, mail, publishing, and connected-app requests. Only necessary task inputs leave the laptop; product analytics and error-event reporting remain disabled.
- [ ] With synthetic credentials, inspect settings responses, application logs, worker errors, and exports for secret leakage. Secret values never appear in API responses or logs. Remove secret entries from the input `.env` after verifying import.
- [ ] Pause work, export a ZIP, and import it into disposable storage. Reopen the app and verify a saved venture, conversation, document, and source. Current credentials are retained, not replaced from the ZIP.
- [ ] Exercise an update backup and restore a listed full backup. Verify the old folder is retained, an unsafe archive is rejected, and a failed backup prevents migration. On another machine, re-enter keychain credentials and test the connection.

## Record the result

Count completed checks from this run, record each failure by symptom, and keep a redacted record of the next step. Do not claim a real send, deployment, source read, measurement, or restore from a mocked response. Do not count a queued job as completed work or a sample result as evidence. Re-run a failed check after its fix before releasing.

Compare [claims to re-verify](LOCAL_WIRING.md) with the integrated code and this run. Wider CI and platform packaging are coordinated separately; they do not replace this checklist.
