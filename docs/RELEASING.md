# Release Supymem Local

## Prepare the public source

From a clean, reviewed private checkout, export to a fresh directory outside the checkout. Use Python 3.12 and Gitleaks 8.30 or later. The exporter alone can run on Python 3.11, but the complete gate imports the app and its tests. Create `.venv` with Python 3.12 and follow **Install locked ./start source dependencies and test tools** in [Public CI](../.github/workflows/public-ci.yml), including `pytest` and `pytest-asyncio`. Use that environment's interpreter for `PUBLIC_CHECK_PYTHON`. Never publish the private repository, its branches, tags, or history.

```sh
export PUBLIC_CHECK_PYTHON="$PWD/.venv/bin/python"
public_dir=$(mktemp -d) &&
bash scripts/make_public_tree.sh "$public_dir" &&
cd "$public_dir" &&
bash scripts/check_public.sh "$PWD"
```

Stop if the export or scan fails. Review the exact file list, notices, dependency inventory, assets, and installer payload for redistribution rights. If you distribute Docker images, check `LICENSE` and `NOTICE` in `/workspace` in the API and worker images and in `/app` in the console image. Verify the full upstream notices for the contents of each actual image, including any separately supplied Firecrawl images. A passing source scan does not grant redistribution rights.

## Test and tag the candidate

On your workstation, run the source scan and version check, and validate the default Compose configuration with `docker compose --env-file /dev/null -f docker-compose.yml config --quiet`. Run [Public CI](../.github/workflows/public-ci.yml) on GitHub Actions from a separate, reviewed candidate repository initialized from this export. In particular, the Playwright provisioning step requires a GitHub-hosted Ubuntu runner; do not run it on your workstation. Keep the results for all 16 local SQLite shards, the offline value journey, console checks, Compose validation, and the locked Tauri check. For both candidate and public remotes, use fresh repositories, not clones or forks of the private checkout.

```sh
VERSION=$(python3 scripts/desktop_version.py check) &&
git init -b main &&
git add . &&
git diff --cached --stat
```

Match `VERSION` to the top release in [CHANGELOG.md](../CHANGELOG.md). Review the staged file list before committing; never include local build or test output.

```sh
git commit -m "Supymem Local $VERSION" &&
bash scripts/check_public.sh &&
git tag -a "v$VERSION" -m "Supymem Local $VERSION" &&
bash scripts/check_public.sh
```

Stop if the version check, a test, or either scan fails. Scan the candidate and every public ref with complete history. Push only the reviewed commit to the candidate repository and wait for Public CI to pass. Then push that same commit and version tag to the public repository and wait for its Public CI to pass. The private packaging and publishing workflows are not part of the export.

## Build and attach installers

On each supported target, build from a clean checkout of that public tag: run `./start --check --build` and `./start --build` on macOS or Linux, or `.\start.ps1 --check` and `.\start.ps1 --build` in Windows PowerShell. See the [desktop guide](self-hosting/DESKTOP.md) for the target requirements and bundle paths. Sign and notarize as required for distribution. Do not treat ad-hoc signing as notarization. Set `SUPYMEM_UPDATE_REPO=<owner>/<public-repo>` when building release installers so the opt-in update check points at the public repository; without it, update checks stay off and say so. Release candidates (`-rc` tags) are never offered by the update check, so RC users download new builds manually.

On Linux, run `python3 scripts/desktop/bundle_linux.py --appimage /absolute/path/YourApp.AppImage` for each AppImage. Keep the generated `.AppImage.sh` beside its matching AppImage and keep both filenames unchanged. Check the pair using the [Linux guide](self-hosting/linux-service.md).

Run the [release gates](self-hosting/RELEASING.md#release-gates) and [installed acceptance](self-hosting/ACCEPTANCE.md) on the exact installers. Generate SHA-256 checksums for the final release files, including both the `.AppImage` and its `.AppImage.sh` (`shasum -a 256` on macOS, `sha256sum` on Linux, or `Get-FileHash -Algorithm SHA256` in PowerShell). Attach the installers, Linux companion launchers, and their checksums to the draft release for `v$VERSION`. Verify the uploaded files against the checksums, record the evidence in the [release checklist](self-hosting/PUBLIC_RELEASE_CHECKLIST.md), then publish only once the gates pass.
