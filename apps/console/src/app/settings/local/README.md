# Local settings integration

The `/settings/local` route renders after `GET /local/settings/mode` reports
`{ "local_mode": true }`, or when the console sets `SUPYMEM_MODE=local` or
`NEXT_PUBLIC_SUPYMEM_MODE=local`. The shared local-mode boundary makes this
choice once. The route layout reuses `AppShell` and your existing session and
CSRF protection. Hosted purchases, referrals, invitation flows and operator
screens are hidden locally. No Mac bridge commands or headers are added.

The backend mounts `app.api.local_settings` only in local mode. The existing
console `api` client uses `/api/local/...` through the authenticated gateway,
including its existing Mac transport. The local Next server also rewrites these
paths to your loopback core service. Its account-page link opens these settings.

## Provider setup

Before workspace screens mount, `LocalModeBoundary` uses `LocalProviderGate`.
First-run provider requests use relative `/api/local/providers` on your current
HTTP origin, including the local Tauri sidecar, without a hosted login. Bundled
webviews reuse the existing desktop API bridge. The core still requires a
loopback caller and loopback origin, and JSON for mutations. Hosted session and
CSRF handling is unchanged. Provider addresses and models are prefilled; Azure
needs your own resource endpoint and deployment name. Only **Test connection**
contacts your provider, with one bounded, metered request and no settings write.
Local mode always offers **Try without a key (sample mode)** with clearly labelled
sample data, without changing runtime flags or contacting your provider. Saving
your service details in Settings switches back to your real service. Hosted mode
does not offer sample entry.
The gate uses the shared console API client at `/api/local/providers`, including
the existing desktop transport. A 15-second timeout shows an error with Retry.
`GET /local/providers` on the core service returns `configured`, non-secret `values` for `provider`,
`model` and `base_url`, and boolean `secrets_set.api_key`. `POST` accepts the
setup form's `provider`, `model`, `api_key` and optional `base_url`; `PUT` accepts
the settings form's same fields. Omitted keys are preserved. Changing an endpoint
without a replacement key clears that endpoint's previous key association.
Keys are never returned. Loopback caller, host and origin checks remain intact.
Missing or unavailable endpoints show a next step instead of starting work.

`GET` and `PUT /local/llm-settings` retain the provider-and-role API for `chat`,
`planning`, `extraction`, `vision` and `realtime`. Both APIs use the same encrypted
model store: `vault/providers/settings.enc` and `machine.secret`. The machine
secret is private and persistent. Do not replace either store's key with the
settings store's key. Environment overrides still take precedence.
`GET /setup/status` reports your local AI and mail modes without credentials.

## Section API and storage

Sections are `smtp`, `publishing`, `search`, `composio`, `images`, `spend` and `data`.
`GET /local/settings/catalog` is the console's field and help-text source. It
includes connection readiness, service links, data-sharing explanations and the
data-directory `.env` path. Both settings and optional first-run connections
render this catalog; first run asks for your model service before optional setup.
Each connection has one help link plus credential hints. Fields follow your
selected service, and hidden fields are left unchanged when you save. Gmail and
Outlook presets fill SMTP and IMAP hosts and ports without replacing your address
or app password. Saving refreshes readiness; connection test results appear next
to the test button. The collapsed `.env` guide includes your configured file's
full path, a Copy button and variable names. Imported fields say **Set from your
.env file**; other saved fields say **Saved in the app**.
`GET /local/settings/{section}` returns non-secret `values` and boolean
`secrets_set`. `PUT` accepts flat partial updates. Omitted fields remain unchanged;
blank replacement secrets, unknown fields and invalid values are rejected.
Validation and storage errors never echo request values. A null
`monthly_spend_cap` removes the saved preference, while zero saves a zero limit.
Amounts are USD decimal strings. The data view returns the running backend's
`data_folder` as a read-only value: moving databases without their keys risks
losing access to your data.

`app.core.local_secrets` uses `app.config.data_root()` and its existing
`COMPANY_OS_DATA_DIR` override, with no new dependencies.
`local-settings.json` holds section values and AES-GCM encrypted credentials;
`local-key.bin` holds the private 256-bit key. Files are mode 0600. Locked atomic
replacement preserves concurrent edits and prior data on write failures.
Missing keys and damaged files fail closed.

## Runtime settings and backups

Local consumers use `read_settings(section)["values"]` and `get_secret(section,
name)`. Search defaults to the built-in keyless provider and reader; provider
accounts and self-hosted Firecrawl are optional upgrades. Your saved monthly cap
is checked by the local meter, with holds and accounting retained.

Opening Keys and connections imports new fields from the data-directory `.env`
into encrypted settings. Fields already saved in the app take priority. Invalid
file entries leave existing settings intact and show a correction step.
`import_env_settings()` also supports startup import for core and worker.

`POST /local/settings/{section}/test` checks the saved search, SMTP, publishing or
Composio account. `POST /local/settings/imap/test` checks incoming-mail login using
the SMTP section's mailbox credentials. Search performs one bounded metered query;
SMTP and IMAP only connect and sign in. Publishing checks the token or S3 bucket
credentials; Composio checks project-key authorization. Tests never send mail,
publish a page or run a connected-app action, and never return credentials.

Saving a backup directory does not run a backup or move data. Stop your app,
copy the entire data folder privately, including every encryption key, then
restart. To restore, stop your app and restore that complete copy first. The
[self-hosting configuration](../../../../../../docs/self-hosting/configuration.md)
and [maintenance guide](../../../../../../docs/self-hosting/backups-security.md)
cover the working runtime settings and backup, restore, upgrade, doctor and demo
commands. Saving settings never authorizes a send, publication, network probe,
provider call or unrelated work. Keep consent, tenant checks, suppression,
disclosure, opt-out, caps and accounting intact.
