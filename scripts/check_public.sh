#!/usr/bin/env bash
set -euo pipefail
exec "${PUBLIC_CHECK_PYTHON:-python3}" "$(dirname -- "${BASH_SOURCE[0]}")/check_public.py" "$@"
