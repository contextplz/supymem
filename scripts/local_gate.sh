#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
status=0
"${SUPYMEM_PYTHON:-python3}" scripts/local_value_journey.py --gate "$@" || status=$?
[[ "$status" != 130 && "$status" != 143 ]] || exit "$status"
"${SUPYMEM_PYTHON:-python3}" scripts/local_browser_journey.py --gate "$@" || status=$?
exit "$status"
