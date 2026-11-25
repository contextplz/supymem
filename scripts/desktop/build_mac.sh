#!/bin/bash
set -euo pipefail

fail() { printf '%s\n' "$1" >&2; exit 1; }
cd "$(cd "$(dirname "$0")/../.." && pwd)"
if [[ "${1:-}" == --plan ]]; then
  app=$(python3 -c 'import json; print(json.load(open("src-tauri/tauri.local.json"))["productName"])')
  python3 scripts/desktop/prepare_local_release.py sign-mac "src-tauri/target/aarch64-apple-darwin/release/bundle/macos/$app.app" --dmg "src-tauri/target/aarch64-apple-darwin/release/bundle/dmg/$app.dmg" --plan
  exit
fi
target=aarch64-apple-darwin
if [[ "${1:-}" == --universal ]]; then target=universal-apple-darwin; shift; fi
[[ $# == 0 ]] || fail "Run scripts/desktop/build_mac.sh with --universal or --plan, then retry."
(
unset APPLE_CERTIFICATE APPLE_CERTIFICATE_PASSWORD APPLE_SIGNING_IDENTITY APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID APPLE_API_KEY APPLE_API_ISSUER APPLE_API_KEY_PATH TAURI_SIGNING_PRIVATE_KEY TAURI_SIGNING_PRIVATE_KEY_PASSWORD
[[ "$(uname -s)" == Darwin ]] || fail "Run your Mac build on an Apple Silicon Mac, then retry."
for tool in node pnpm uv cargo rustup xcrun otool codesign hdiutil lipo ditto; do
  command -v "$tool" >/dev/null || fail "Install $tool on your Mac, then retry your build."
done
[[ "$(node -p 'process.versions.node.split(".")[0]')" == 22 ]] || fail "Install Node 22 for your Mac, then retry."
[[ "$(node -p 'process.arch')" == arm64 ]] || fail "Install the arm64 Node runtime and run your build outside Rosetta, then retry."
node_libraries=$(otool -L "$(command -v node)") || fail "Check your Node runtime with otool -L, then install the official arm64 Node 22 runtime and retry."
[[ "$node_libraries" == *$'\n'* ]] || fail "Install the official standalone arm64 Node 22 runtime, then retry. Your Node libraries could not be inspected."
while read -r library rest; do
  case "$library" in /usr/lib/*|/System/Library/*) ;; *) fail "Install the official standalone arm64 Node 22 runtime, then retry. Your Node runtime depends on libraries outside macOS." ;; esac
done < <(printf '%s\n' "$node_libraries" | tail -n +2)
xcrun --find clang >/dev/null || fail "Install your Xcode command-line tools with xcode-select --install, then retry."
python=$(uv python find --managed-python cpython-3.12-macos-aarch64-none) || fail "Install your bundled Python with uv python install cpython-3.12-macos-aarch64-none, then retry."
version=$("$python" -c 'import tomllib; print(next(package["version"] for package in tomllib.load(open("src-tauri/Cargo.lock", "rb"))["package"] if package["name"] == "tauri"))')
[[ "$(cargo tauri --version)" == "tauri-cli $version" ]] || fail "Install your matching CLI with cargo install tauri-cli --version $version --locked, then retry."
[[ "$(rustup target list --installed)" == *aarch64-apple-darwin* ]] || fail "Install your Mac target with rustup target add aarch64-apple-darwin, then retry."
trap 'printf "%s\n" "Your build stopped. Fix the reported prerequisite or build error, then rerun scripts/desktop/build_mac.sh. Your saved work is retained." >&2' ERR
if [[ ! -x .desktop-python/bin/python ]]; then uv venv .desktop-python --python "$python"; fi
.desktop-python/bin/python -c 'import platform, sys; sys.exit(0 if platform.machine() == "arm64" and sys.version_info[:2] == (3, 12) else "Recreate your .desktop-python environment using arm64 Python 3.12, then retry.")'
uv pip install --python .desktop-python/bin/python --require-hashes -r services/company_os/requirements.runtime.lock
uv pip install --python .desktop-python/bin/python --require-hashes -r services/company_os/requirements.build.lock
uv pip install --python .desktop-python/bin/python --require-hashes -r packages/desktop_runtime/requirements.local.lock
export PATH="$PWD/.desktop-python/bin:$PATH"
pnpm --dir apps/console install --frozen-lockfile --node-linker=hoisted
if [[ "$target" == universal-apple-darwin ]]; then
  [[ "$(rustup target list --installed)" == *x86_64-apple-darwin* ]] || fail "Run rustup target add x86_64-apple-darwin, then retry your universal build."
  [[ -x .desktop-intel/bin/node ]] || fail "Place your official standalone x86_64 Node 22 executable at .desktop-intel/bin/node, then retry your universal build."
  intel_python=$(uv python find --managed-python cpython-3.12-macos-x86_64-none) || fail "Run uv python install cpython-3.12-macos-x86_64-none, then retry your universal build."
  [[ -x .desktop-python-x86_64/bin/python ]] || uv venv .desktop-python-x86_64 --python "$intel_python"
  uv pip install --python .desktop-python-x86_64/bin/python --require-hashes -r services/company_os/requirements.runtime.lock
  uv pip install --python .desktop-python-x86_64/bin/python --require-hashes -r services/company_os/requirements.build.lock
  uv pip install --python .desktop-python-x86_64/bin/python --require-hashes -r packages/desktop_runtime/requirements.local.lock
  PATH="$PWD/.desktop-intel/bin:$PATH" arch -x86_64 .desktop-python-x86_64/bin/python scripts/desktop/prepare_local_stack.py --runtime-only
fi
node scripts/desktop/prepare_local_stack.cjs
.desktop-python/bin/python scripts/desktop/prepare_local_release.py assert-payloads src-tauri/resources --target "$target"
cd src-tauri
cargo tauri build --features local-stack --config tauri.local.json --target "$target" --bundles app,dmg
)
bundle="src-tauri/target/$target/release/bundle"
app=$(find "$bundle/macos" -maxdepth 1 -name '*.app' -type d -print -quit)
dmg=$(find "$bundle/dmg" -maxdepth 1 -name '*.dmg' -type f -print -quit)
[[ -n "$app" && -n "$dmg" ]] || fail "Check your app and DMG build output, then rerun scripts/desktop/build_mac.sh."
.desktop-python/bin/python scripts/desktop/prepare_local_release.py sign-mac "$app" --dmg "$dmg"
printf '%s\n' "Your app and installer are in $bundle. Open your installed app to complete setup."
