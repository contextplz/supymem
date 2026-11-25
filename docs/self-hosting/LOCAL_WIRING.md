# Local documentation evidence

This reference maps user instructions to their current source, not an agreed future contract. It does not perform provider calls or certify release acceptance.

| Documented behavior | Source |
|---|---|
| Launcher modes and prerequisites | `start`, `start.ps1`, `scripts/run_local.sh`, `scripts/run_local.ps1` |
| Standalone Node/Python downloads and payload | `scripts/desktop/prepare_local_stack.py`, `src-tauri/tauri.local.json` |
| Key import names and precedence | `services/company_os/app/core/local_env_import.py`, `providers_local.py`, `local_secrets.py` |
| Catalog fields and connection tests | `services/company_os/app/api/local_settings.py` |
| Capability names, initial switches, and missing settings | `services/company_os/app/core/local_capabilities.py` |
| Your connected tool builds; Charlie briefs, watches and checks | `services/company_os/app/build/device_orders.py`, `services/company_os/app/api/device_orders_api.py`, `packages/desktop_runtime/owned_coding.py`, `packages/desktop_runtime/coding_loop.py` |
| Saved search, mailbox, publishing, and Composio settings | `app/research/local_providers.py`, `app/gtm/local_mail.py`, `app/pagebuilder/local_publish.py`, `app/connections/local_config.py` under `services/company_os` |
| Owner credential and exact-origin checks | `packages/core/local_security.py`, `apps/console/src/middleware.ts`, `apps/console/src/app/api/[...path]/route.ts` |
| Backups, key-file exclusion, and staged replacement | `services/company_os/app/cli_local.py`, `app/core/local_data.py`, and `src-tauri/src/local_stack.rs` |

`tests/unit/infra/test_docs_match_code.py` reads the importer and catalog declarations. It rejects unread variable/field references in README and keys docs and requires the complete importer and catalog in the keys reference. It checks capability defaults and known-issue copy; it does not exercise transports.

## Known issues

See [supported coding tools](../../README.md#build-with-your-own-coding-tool), [Known issues](../../README.md#known-issues), and [release acceptance](ACCEPTANCE.md). Source inspection does not supersede failing live results.

## Stopped-app recovery

If the app cannot open, quit it and stop all launchers and workers. Check out the source for the same app release and run from the checkout root. No `.env` is needed.

First identify your **exact** data folder. Use the last path shown in **Settings > Keys and connections > Your data folder and backup**, your configured `COMPANY_OS_DATA_DIR`, or the [default for your installation](backups-security.md#what-to-keep) if you never changed it.

| How you start the app | Default main SQLite file | Default location |
|---|---|---|
| Desktop | `database.sqlite3` | Your desktop data folder |
| Browser from source | `local.db` | `data/runtime/local/` in your checkout |
| Docker | `local.db` | The `local-data` Docker volume, mounted at `/workspace/data` |

Use the file in your original database URL if you configured one. Without an override, the browser launcher uses an existing `database.sqlite3` when that is the only main database, and refuses to choose when both files exist. If both files are present, confirm which one your installation used before continuing; do not guess. For backup, confirm the selected file exists. If it is missing and you are restoring, still use its original path. Do not create an empty data folder or guess between installations. If you customized `COMPANY_OS_SECRETS_ENV`, keep it pointing at the same file inside that folder.

The host commands below are for desktop and source-browser folders, not a Docker volume. For Docker, use [Your data and recovery](backups-security.md#restore-and-verify) while the app opens. If it cannot open, run `docker compose stop` from your original checkout and use Docker's volume tools to preserve the whole `local-data` volume privately. Keep that original volume and check recovery on a separate copy. Do not run the host commands against your checkout's `data` folder or run `docker compose down -v`.

If you installed the app from a package and this is a fresh checkout, create the source `.venv` with the [browser launcher](install.md#start-with-one-command). Run it in a separate terminal against a temporary folder, not your original data. On macOS or Linux:

```sh
recovery_setup=$(mktemp -d) && \
  COMPANY_OS_DATA_DIR="$recovery_setup" COMPANY_OS_DB_URL="sqlite:///$recovery_setup/local.db" ./start --web
```

On Windows, with Python 3.12 and Node 22 or newer installed, run in PowerShell:

```powershell
$env:COMPANY_OS_DATA_DIR = Join-Path ([System.IO.Path]::GetTempPath()) ('supymem-recovery-' + [guid]::NewGuid())
$setupDatabase = (Join-Path $env:COMPANY_OS_DATA_DIR 'local.db').Replace('\', '/')
$env:COMPANY_OS_DB_URL = "sqlite:///$setupDatabase"
.\start.ps1 --web
```

When the launcher prints its setup URL, press Ctrl+C and close that terminal. You do not need to open the URL or enter any keys. Leave `.venv` in the checkout and use a new terminal for the actual recovery below.

On macOS or Linux, replace the paths and `REPLACE_WITH_SQLITE_FILENAME` with the selected filename (or its relative path inside the data folder). Run only the backup or restore line you need:

```sh
export SUPYMEM_MODE=local APP_ENV=local
export COMPANY_OS_DATA_DIR="/absolute/path/to/your data folder"
export COMPANY_OS_DB_URL="sqlite:///$COMPANY_OS_DATA_DIR/REPLACE_WITH_SQLITE_FILENAME"
export PYTHONPATH="$PWD/packages:$PWD/services/company_os"
.venv/bin/python -m app.cli_local backup --output "$HOME/supymem-backups"
.venv/bin/python -m app.cli_local restore "/absolute/path/to/your full backup.tar.gz"
```

In Windows PowerShell, use the same data folder and database selection:

```powershell
$env:SUPYMEM_MODE = 'local'
$env:APP_ENV = 'local'
$env:COMPANY_OS_DATA_DIR = 'C:\absolute\path\to\your data folder'
$database = (Join-Path $env:COMPANY_OS_DATA_DIR 'REPLACE_WITH_SQLITE_FILENAME').Replace('\', '/')
$env:COMPANY_OS_DB_URL = "sqlite:///$database"
$env:PYTHONPATH = "$PWD\packages;$PWD\services\company_os"
.\.venv\Scripts\python.exe -m app.cli_local backup --output "$HOME\supymem-backups"
.\.venv\Scripts\python.exe -m app.cli_local restore 'C:\absolute\path\to\your full backup.tar.gz'
```

Run only the command you need. Backup creates a new timestamped archive. Restore validates it and retains your previous data folder. This maintenance path requires file-backed SQLite with its database inside the data folder. Never delete your healthy copy to make a restore succeed. For a diagnostic with the app running, use `make doctor DATA_DIR="<your actual data folder>"` on macOS or Linux, or `.\.venv\Scripts\python.exe scripts/doctor.py --data-dir 'C:\absolute\path\to\your data folder'` in PowerShell. Follow the printed next steps.
