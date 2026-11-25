# Your local security model

This branch runs a single-owner app on your machine. Local storage does not make cloud requests local or turn the app into a shared-user server. Keep your OS account, disk, and services private.

## Owner credential and exact origins

Startup creates `owner-session.secret` in your data folder. It is a private per-install credential, not a model key. Your shell and console use it to establish your owner session. The browser proxy requires either that credential or the matching HttpOnly, SameSite=Strict session cookie and request proof. It strips caller-supplied forwarding, owner, and session headers before forwarding its authenticated request. It never returns the owner secret in an API response.

Private core requests require the owner credential. Being on loopback alone does not authenticate another process or OS user. Requests validate Host, forwarded Host, Origin, Referer, and browser request metadata. Private browser origins must match your configured console origin, including scheme and port, or one of the explicit Tauri origins. `localhost` and `127.0.0.1` are not interchangeable when an exact origin is configured. Malformed, duplicate, cross-site, and conflicting security headers are rejected. WebSockets require an explicit allowed origin. The source launcher disables proxy-header trust.

Your authenticated request uses the registered local owner; forged subject headers do not select another owner. Workspace ownership, file containment, consent, and send checks remain. Local logout does not revoke the installation credential; quit the app to stop its services. An OS administrator or malware with access to your private credential can act as you.

## Remote access

`SUPYMEM_LOCAL_ALLOW_REMOTE` defaults off. Enabling it removes the peer-loopback restriction and permits the configured `COMPANY_OS_PUBLIC_URL` host at the backend. It does not create a tunnel, change listening addresses, provision user accounts, remove owner authentication, or relax private browser origin checks. The console proxy still restricts private owner requests to its exact local address. Never copy your owner credential to a public page or remote caller.

Explicit public routes handle unsubscribe links, waitlist signup, pricing and survey links, selected OAuth callbacks, and page events without an owner identity. Their authorization, token, payload, and ownership checks remain. Page measurement also has a host-matched public collection endpoint. Configuring remote access does not grant these routes your private workspace or key store. Public endpoint availability still requires your own reachable host; saving a publishing account does not expose your app automatically.

Leave remote access off for ordinary laptop use. Do not publish your console or API as a website. Use your publishing destination for public files and an email-based waitlist when you do not operate a public collection endpoint.

## Where your keys are stored

Saved model and connection secrets are encrypted in `local-settings.json`; non-secret values and secret-set status are separate. The desktop shell supplies encryption material through a private pipe. It uses macOS Keychain, Windows Credential Manager, or a Linux Secret Service keyring. Where the keyring is unavailable, the app uses a private `local-key.bin` encryption key file. The Python-only path also uses its native keyring when available and otherwise the key file. Existing legacy provider and vault files are retained or migrated through the matching store.

Unix directories use owner-only permissions and private files; Windows uses native locks and owner-only ACLs. Corrupt settings or missing keys produce a recovery error, not silent replacement. Unlock your keyring and reopen Keys and connections. If you used the file-backed key, recover the matching key file and encrypted settings together. Do not create a new encryption key to repair old ciphertext.

Your `.env` input is plaintext and is not removed by import. Confirm that settings imported, then remove its secret entries or store it privately on encrypted storage. Never put keys in chat, URLs, screenshots, issue reports, or source control. API responses show secret status, not secret values. Logging redacts registered credentials; arbitrary prints, SDK wire tracing, and external tools are outside that filter. Review logs privately before sharing them.

Encryption does not protect against your OS account, an administrator, malware with that access, or an unencrypted stolen disk. Enable disk encryption and screen lock. [Full backup archives](backups-security.md) exclude `local-key.bin` and your owner-session credential but retain encrypted settings and older vault files. Preserve key-file recovery material separately; OS keyring material is not made portable by a data archive.

## Web and provider boundaries

Built-in search sends your query to DuckDuckGo. The built-in page reader validates public destinations, DNS results, redirects, robots permissions, and bounded response sizes. It rejects private, loopback, link-local, metadata, multicast, and reserved page destinations. Your configured loopback model or reader is a deliberate connection, not an arbitrary page URL.

Optional Firecrawl and search services own their outbound connections and redirects. Configure your Firecrawl server to reject private destinations and restrict its network access. The app cannot enforce an independently operated provider's egress policy. Cloud models receive prompts and task context; mail, publishing, and connected apps receive their requested inputs. Product analytics and error-event reporting are disabled locally; operational logs remain.

Imported pages, documents, and generated code are untrusted. They cannot authorize unrelated work or ask for your keys. Native coding tools can access files permitted to your OS account; use an isolated workspace or VM for untrusted code. Outreach keeps disclosure, opt-out, sender identity and postal address, suppression, permission checks, and caps. Guessed emails stay off.

## Connected coding tools

Your approval is required for the detected executable. Supymem checks it again when you start a build, uses the build folder as its working directory, and checks the result before showing it. You can stop the run from **Build activity**.

OpenCode 2.2.5 or newer in the 2.x series can build landing pages, products and fixes with your supported subscription login. Supymem checks a temporary OpenCode permission profile in the build folder without changing your global configuration. It allows native file reads and edits in the build folder, blocks external directories, shell commands, web fetching and search, and does not load your project or user extensions. OpenCode still needs the network for your signed-in model. Unlike the other tools' command sandboxes, these are OpenCode's own tool permissions, not an operating system sandbox. Charlie's checks run separately; OpenCode cannot execute tests itself. If the profile cannot be verified, the build does not start. Update OpenCode and select **Check connection** in **Settings > Keys and connections > Coding tools**, or connect another tool there if you need it to run commands.

Use built-in OpenAI, Anthropic, or GitHub Copilot OAuth. Any saved API-key login or unsupported sign-in in the profile blocks builds, even alongside a supported subscription. Supymem checks `~/.local/share/opencode`, not a custom `XDG_DATA_HOME`. If you signed in only under a custom location, sign in again at the default location.

On macOS or Linux, run `XDG_DATA_HOME="$HOME/.local/share" opencode auth logout` once for each API-key or unsupported sign-in there, selecting the provider to remove. Then run `XDG_DATA_HOME="$HOME/.local/share" opencode auth login` and choose a supported subscription sign-in. In Windows PowerShell, set `$env:XDG_DATA_HOME = "$HOME/.local/share"` for that terminal, then run `opencode auth logout` as needed and `opencode auth login`. Return to **Settings > Keys and connections > Coding tools** and select **Check connection** again.

## Verification and reporting

This is a source-verified model, not a certification of your OS, installer, or external provider. Native keyring behavior and clean-machine launch require release acceptance. November 15, 2026 live failures are listed in [Known issues](../../README.md#known-issues). For a suspected vulnerability, use [Security](../../SECURITY.md) without sharing keys or private data.
