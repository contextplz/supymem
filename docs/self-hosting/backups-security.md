# Backups and security

Your data lives on your laptop. Protect your data folder, your recovery archives, and your provider accounts before moving or replacing anything.

## What to keep

Open **Settings > Keys and connections > Your data folder and backup** to see the data folder this app uses. A source checkout is not a desktop backup. Keep the entire data folder for full recovery, including databases, documents, settings, local encryption material, and spending history.

Model and connection keys are encrypted in your saved local settings. Your desktop shell uses your OS keychain for encryption material or a private key-file fallback. Full backup archives exclude `local-key.bin` and `owner-session.secret`; preserve your matching key-file recovery material separately. OS keychain entries are not made portable by copying data. Keep a way to reconnect accounts on a new machine, and keep databases with their matching settings and recovery material.

Your local desktop uses these default folders:

- macOS: `~/Library/Application Support/Supymem Local`.
- Linux: `$XDG_DATA_HOME/Supymem Local`, or `~/.local/share/Supymem Local` when unset.
- Windows: `%LOCALAPPDATA%\Supymem Local`, usually under your user profile in `AppData\Local`.

The source browser launcher defaults to `<checkout>/data/runtime/local` instead. Full backup archives default to a sibling `<data-folder>-backups` folder or your saved backup destination. Trust the path shown in your app rather than guessing. On Mac or Linux, to move your data, back up, stop your app and worker, copy the entire folder including encrypted settings and key references, then launch with `COMPANY_OS_DATA_DIR` set to the new absolute path. Source commands also accept `SUPYMEM_DATA_DIR`; `COMPANY_OS_DATA_DIR` takes priority. Update any custom database URL or secrets-file path too. Copying a repository or changing a Compose project does not migrate existing data.

On Windows, to move your data, do not copy the workspace in Explorer: copied files may not retain their private permissions. Create a full backup under **Settings > Keys and connections > Your data and recovery**, keep your original folder and matching encryption recovery key separately, and quit the app. Set `COMPANY_OS_DATA_DIR` to the new absolute path and open your app. Under **Your data folder and backup**, set **Full backup folder path** to the folder holding the archive, then select the archive under **Your data and recovery** and follow [Restore and verify](#restore-and-verify).

Your app keeps a private data-folder record and relocates saved page, document, and asset file paths before starting. Recorded conversation text and immutable history remain unchanged; paths quoted in old messages are not necessarily current locations. Keep the original folder until you verify your restored work and saved connections. Default backups are created beside the new folder. A backup folder you chose in Keys and connections stays where you chose it; existing archives do not move.

Full backups contain private business and personal data, encrypted saved credentials, and older vault encryption material. The local encryption key file is excluded; keep its recovery copy separately on protected storage. The archive itself is not encrypted by the app. Keep it on encrypted storage with private permissions; never put it in source control or upload it to a public issue.

Full backups retain encrypted saved settings and vault files, but exclude plaintext `.env` files, legacy `secrets.json`, credential-tool folders, `local-key.bin`, and the owner-session credential. Save supported keys in Keys and connections before backing up; startup also imports supported `.env` keys into that store. Native keyring encryption material must remain available, or you must reconnect accounts on a new machine. Both exports and full backups clear `venture_resources.secrets_json` and `admin_users.totp_secret_enc` in copied databases without changing your live data.

## Choose a backup folder

Under **Your data folder and backup**, set **Full backup folder path** to an absolute path outside your data folder. Leave it blank for the default sibling backup folder. Save the setting. Future backups use that folder; existing backups stay where they were. Moving the setting does not move old recovery archives.

Your app does not upload backups. If you choose a folder synchronized by another tool, that tool can upload them; protect that destination yourself.

## Export your data

Pause background work and stop editing files. Open **Settings > Keys and connections > Your data and recovery** and choose **Export my data**. Keep the downloaded ZIP private and check that you can open it before relying on it.

A data export contains database snapshots, files, and non-secret saved settings, but excludes saved service keys and encryption material and removes known secret columns from copied databases. Imported settings use the same connection-field validation as Keys and connections. Original databases are not edited. Arbitrary documents or uploads can still contain sensitive information or secrets you placed in them, so review an export before sharing it. A data-only export does not replace a full backup for credential recovery.

## Local SQLite maintenance

### Updates and automatic backups

Before preparing an update, the desktop takes online snapshots of every SQLite store without live WAL sidecars and copies your files into a timestamped backup, then applies database migrations. A backup failure stops the update. A failed migration does not mark the new version ready. Keep enough disk space for the backup, working data, and recovery copies.

On Mac or Linux, for a separate full folder copy, first quit the desktop and stop all source launchers or tools writing to the same data. Copy the entire data folder and retain the app version used with that copy. Copying an actively written SQLite file or mixing database and file copies from different times is not a consistent recovery point. On Windows, use a full backup archive instead.

### Stopped-app recovery

On Mac or Linux, for a full folder copy, quit the app and stop all writers. Preserve the current data folder separately, then restore the entire known-good copy to its original location. Do not merge database files or restore only an encryption key. Reopen the app and complete the checks below before removing the old copy.

For an archive when the desktop or source-browser app cannot open, use the [stopped-app recovery commands](LOCAL_WIRING.md#stopped-app-recovery) from your matching source checkout. Use the exact data folder and SQLite file from your launch mode. If you use Docker, follow the volume instructions in that same guide instead. Keep all copies intact until you verify recovery.

## Restore and verify

1. Pause work. Keep your current folder and the archive intact. Check free disk space for staging, backups, and recovery copies.
2. Under **Your data and recovery**, choose a data-only ZIP, then choose **Replace workspace with this ZIP**. To restore a full backup, choose the listed backup instead. Both replace your workspace rather than merging it. A ZIP import keeps your current saved settings and keys.
3. The app validates the archive and backs up your current data before replacing it. If your desktop app does not restart, quit and reopen it. In browser mode, press Ctrl+C in the terminal running your app, then run `./start --web` on macOS or Linux, or `.\start.ps1 --web` in Windows PowerShell. Reloading the tab alone does not finish recovery.
4. Open a saved venture, conversation, and document. Test your model and mailbox connections on **Keys and connections**. Unlock your original keyring, restore your separately preserved matching local key file with the app stopped, or reconnect accounts on a new machine. Never create a new key to decrypt old settings.
5. Check suppression and opt-out records before sending again, and inspect any queued task before requesting it again. Keep your original copy until all recovery checks pass.

Archive format 2 records SHA-256 checksums for every file. Imports reject older archive formats, older database revisions, and unknown newer revisions before changing your data. Open old exports with their original app, update that app, then export again. Full backup restore accepts known older database revisions so you can recover a failed update, but rejects older archive formats. Unsafe archive paths, links, duplicates, damaged files, and SQLite journal files are rejected. Queued restores are checked again before replacement. Choose an intact compatible archive rather than weakening validation. If a restore fails, keep every copy and follow [the recovery symptom](troubleshooting.md#import-or-restore-fails-or-your-data-seems-missing).

If the app cannot start, use the linked stopped-app recovery command with its matching private settings or restore your entire known-good folder copy with all writers stopped. A broken or missing current database does not prevent a full restore; your previous whole folder is retained even when it cannot produce a healthy safety snapshot. Preserve the failed folder separately. Do not merge databases or replace only an encryption-key file.

## Your connections and security

Use your operating system's disk encryption, screen lock, keychain protection, and private file permissions. Keys entered in the app are encrypted locally with keychain or key-file encryption material and are not returned by the settings API. A `.env` input remains plaintext until you remove its secret entries; confirm the import first, then remove those entries or keep the file only in protected storage. Never paste keys into chat or include them in bug reports.

Keep the console, API, database, and local model server on loopback. Do not expose your app as a public website or disable local authentication. Publish only your chosen public page files through a publishing account. Your cloud model receives prompts and task context; search, mail, publishing, and connected apps receive their requested inputs. Product analytics and error-event reporting are disabled in local mode.

Imported documents, web pages, and generated code are untrusted. They cannot authorize unrelated work or request your keys. External actions must follow your request and valid permissions. Native coding tools can access files permitted to your user account; use a separate account or isolated workspace for untrusted code instead of giving it your personal credential folders.

Outreach keeps source disclosure, opt-out, your postal address, suppression, permission checks, and send caps. Guessed email addresses stay off. Honor opt-out replies before further outreach. Revoke retired provider keys and unused app authorizations in their provider accounts.

For security problems, follow [Security](../../SECURITY.md) without sharing credentials or private data.
