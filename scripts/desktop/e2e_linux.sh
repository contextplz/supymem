#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Usage: E2E_RECORD=1 e2e_linux.sh [AppImage] [new evidence directory] records a demo.
# E2E_SCENARIO selects an idea from tests/fixtures/desktop_e2e_linux.json.
# E2E_MODE=sample answers every question via the bundled app APIs, without test flags.
# Sample mode also checks draft safety, consent, saved work, Settings and page links.
# E2E_MODE=live checks your private E2E_ENV_FILE, then repeats through setup fields.
# The packaged window is read through its loopback WebKit inspector, not AT-SPI.
# E2E_OCCUPY_PORT holds a loopback port to check your app's free-port fallback.
# Prerequisites match .github/workflows/public-ci.yml; no downloads happen here.
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
output=${2:-"$root/artifacts/desktop-e2e/$(date -u +%Y%m%dT%H%M%S)-$$"}
mkdir -p -- "$output"
output=$(cd -- "$output" && pwd)
trap 'status=$?; printf "FAIL harness: Your desktop check stopped at scripts/desktop/e2e_linux.sh:%s. Review %s/00_splash.png if launch started, fix that prerequisite or rebuild your AppImage, then rerun.\n" "$LINENO" "$output" >&2; printf "SUMMARY PASS=0 FAIL=1 SKIP=0 artifacts=%s\n" "$output"; exit "$status"' ERR
for tool in python3 unshare ip xvfb-run Xvfb xauth dbus-run-session timeout strace; do
  if ! command -v "$tool" >/dev/null; then
    printf 'FAIL prerequisites: Your missing tool is %s. Install Ubuntu packages python3 util-linux iproute2 xvfb xauth dbus strace, then rerun.\n' "$tool" >&2
    printf 'SUMMARY PASS=0 FAIL=1 SKIP=0 artifacts=%s\n' "$output"
    exit 1
  fi
done
if [[ ${E2E_MODE:-sample} != sample && ${E2E_MODE:-sample} != replay && ${E2E_MODE:-sample} != live ]]; then
  printf 'FAIL arguments: Choose E2E_MODE=sample, replay or live, then rerun your desktop check.\n' >&2
  printf 'SUMMARY PASS=0 FAIL=1 SKIP=0 artifacts=%s\n' "$output"
  exit 1
fi
export E2E_MODE=${E2E_MODE:-sample}
export GSETTINGS_BACKEND=memory
if [[ $E2E_MODE == replay ]]; then
  command -v xdotool >/dev/null
  if command -v import >/dev/null; then
    export E2E_SCREENSHOT_TOOL=import
  else
    command -v import-im6.q16 >/dev/null
    export E2E_SCREENSHOT_TOOL=import-im6.q16
  fi
fi
python3 -c 'import gi; gi.require_version("Soup", "3.0"); gi.require_version("Gdk", "3.0"); from gi.repository import Soup, Gdk' || {
  printf 'FAIL prerequisites: Install Ubuntu packages python3-gi gir1.2-soup-3.0 gir1.2-gtk-3.0, then rerun. AT-SPI and webkit2gtk-driver are not needed.\n' >&2
  printf 'SUMMARY PASS=0 FAIL=1 SKIP=0 artifacts=%s\n' "$output"
  exit 1
}
if [[ -n ${1:-} ]]; then
  appimage=$(realpath -- "$1")
else
  (
    cd -- "$root/src-tauri"
    ../apps/desktop/node_modules/.bin/tauri build --features local-stack --config tauri.local.json \
      --target x86_64-unknown-linux-gnu --bundles appimage
  ) >"$output/build.log" 2>&1 || {
    printf 'FAIL build: Your AppImage build failed. Open %s/build.log, fix the reported build error, then rerun.\n' "$output" >&2
    printf 'SUMMARY PASS=0 FAIL=1 SKIP=0 artifacts=%s\n' "$output"
    exit 1
  }
  shopt -s nullglob
  images=("$root"/src-tauri/target/x86_64-unknown-linux-gnu/release/bundle/appimage/*.AppImage)
  [[ ${#images[@]} == 1 ]]
  appimage=${images[0]}
fi
[[ -f $appimage && -x $appimage ]]
screen=1440x1000x24
if [[ ${E2E_RECORD:-0} == 1 ]]; then
  command -v ffmpeg >/dev/null
  screen=1440x900x24
fi
# Keep synthetic journeys offline without touching other sessions.
trap - ERR
if [[ $E2E_MODE == live ]]; then
  if [[ ! -f ${E2E_ENV_FILE:-} ]]; then
    printf 'FAIL setup: Set E2E_ENV_FILE to your private model .env file, then rerun live mode.\n' >&2
    printf 'SUMMARY PASS=0 FAIL=1 SKIP=0 artifacts=%s\n' "$output"
    exit 1
  fi
  status=0
  failed_entries=()
  for entry in env screens; do
    mkdir -p "$output/$entry"
    E2E_ENTRY=$entry timeout --foreground 900 xvfb-run -a -s "-screen 0 $screen -nolisten tcp" \
      dbus-run-session -- python3 "$root/scripts/desktop/e2e_linux.py" "$appimage" "$output/$entry" || {
        status=1
        failed_entries+=("$entry")
      }
  done
  python3 - "$output" "${failed_entries[@]}" <<'PY'
import json
import sys
from pathlib import Path
output = Path(sys.argv[1])
results = []
for entry in ('env', 'screens'):
    path = output / entry / 'results.json'
    items = json.loads(path.read_text()) if path.exists() else []
    if not items or entry in sys.argv[2:] and not any(item['passed'] is False for item in items):
        message = 'Your journey process stopped before recording its failure. Review scripts/desktop/e2e_linux.py and this run\'s app.log, fix the reported error, then rerun.'
        items.append({'step': 'journey', 'passed': False, 'message': message})
        path.write_text(json.dumps(items, indent=2) + '\n')
        print(f'FAIL {entry} journey: {message}', flush=True)
    results.extend(items)
print(f'SUMMARY PASS={sum(item["passed"] is True for item in results)} FAIL={sum(item["passed"] is False for item in results)} SKIP={sum(item["passed"] is None for item in results)} artifacts={output}')
PY
  exit "$status"
fi
if timeout --foreground 600 unshare --user --map-current-user --keep-caps --net \
  bash -c 'ip link set lo up || exit 1; xvfb-run -a -s "-screen 0 $4 -nolisten tcp" \
    dbus-run-session -- python3 "$1/scripts/desktop/e2e_linux.py" "$2" "$3"; status=$?; sleep 1; exit "$status"' -- "$root" "$appimage" "$output" "$screen"; then
  exit 0
fi
if [[ ! -f $output/results.json ]]; then
  printf 'FAIL harness: Your display or isolation could not start. Check scripts/desktop/e2e_linux.sh and your Linux prerequisites, then rerun into a new evidence folder. Expected screenshot: %s/00_splash.png.\n' "$output" >&2
  printf 'SUMMARY PASS=0 FAIL=1 SKIP=0 artifacts=%s\n' "$output"
else
  python3 - "$output" <<'PY'
import json
import sys
from pathlib import Path
output = Path(sys.argv[1])
path = output / 'results.json'
results = json.loads(path.read_text())
if not any(item['passed'] is False for item in results):
    message = 'Your journey process stopped without recording its failure. Review scripts/desktop/e2e_linux.py and app.log, fix the reported error, then rerun.'
    results.append({'step': 'journey', 'passed': False, 'message': message})
    path.write_text(json.dumps(results, indent=2) + '\n')
    print(f'FAIL journey: {message}', flush=True)
    print(f'SUMMARY PASS={sum(item["passed"] is True for item in results)} FAIL=1 SKIP={sum(item["passed"] is None for item in results)} artifacts={output}', flush=True)
PY
fi
exit 1
