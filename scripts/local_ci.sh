#!/usr/bin/env bash
set -uo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT" || exit 1
PY=${PY:-"$HOME/venv/bin/python"}
cores=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || printf '7')
workers=${1:-$((cores > 6 ? cores - 6 : 1))}
if (( $# > 1 )) || [[ ! "$workers" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Usage: PY=/path/to/python bash scripts/local_ci.sh [positive worker count]\n' >&2
    exit 2
fi

results=$(mktemp -d "${TMPDIR:-/tmp}/local-ci.XXXXXX") || exit 1
export ROOT PY results
unset COMPANY_OS_DB_URL DATABASE_URL DB_URL COMPANY_OS_WORKING_MEMORY_PATH
export GUARDRAILS_MODE=deterministic_only LLM_OFFLINE=1 LLM_CASSETTE_MODE=replay
export CHANNELS_DRY_RUN=true COS_TEST_APP_REUSE=1
export PYTHONPATH="$ROOT/packages:$ROOT/services/company_os"

run_file() {
    local mode=$1 file=$2 status log
    log="$results/$mode/${file#"$ROOT/"}.log"
    mkdir -p -- "$(dirname -- "$log")" || return 1
    export COMPANY_OS_DATA_DIR="$log.data"
    export COMPANY_OS_DB_URL="sqlite:///$log.data/app.db"
    mkdir -p -- "$COMPANY_OS_DATA_DIR" || return 1
    if [[ "$mode" == local ]]; then
        env SUPYMEM_MODE=local "$PY" -m pytest -q -p no:cacheprovider -m 'not hosted' "$file" >"$log" 2>&1
    else
        env -u SUPYMEM_MODE "$PY" -m pytest -q -p no:cacheprovider "$file" >"$log" 2>&1
    fi
    status=$?
    if [[ "$mode" == local && "$status" == 5 ]]; then
        status=0
    fi
    printf '%s\n' "$status" >"$log.status"
}
export -f run_file

failed=0
if "$PY" -m ruff check packages/llm_gateway packages/guardrails >"$results/ruff.log" 2>&1; then
    printf 'ruff: passed\n'
else
    printf 'ruff: FAILED (see %s/ruff.log)\n' "$results"
    failed=1
fi

modes=(local)
[[ ! -f services/company_os/app/admin/router.py ]] || modes+=(unset)
for mode in "${modes[@]}"; do
    printf 'Unit mode %s: %s parallel workers, one process per file\n' "$mode" "$workers"
    mkdir -p "$results/$mode"
    if ! find "$ROOT/tests/unit" -type f -name 'test_*.py' -print0 \
        | xargs -0 -r -P "$workers" -I '{}' bash -c 'run_file "$1" "$2"' bash "$mode" '{}'; then
        printf '%s: runner failed before all files completed\n' "$mode"
        failed=1
    fi
    total=0
    failures=0
    while IFS= read -r -d '' status_file; do
        ((total+=1))
        read -r status <"$status_file"
        if [[ "$status" != 0 ]]; then
            ((failures+=1))
            file=${status_file#"$results/$mode/"}
            printf '  FAILED %s (exit %s)\n' "${file%.log.status}" "$status"
        fi
    done < <(find "$results/$mode" -name '*.log.status' -print0)
    printf '%s: %s files, %s failed\n' "$mode" "$total" "$failures"
    if (( failures > 0 || total == 0 )); then
        failed=1
    fi
done

for check in console tsc; do
    if (
        cd "$ROOT/apps/console" || exit 1
        export NODE_USE_SYSTEM_CA=0
        export SUPYMEM_MODE=local
        if [[ "$check" == console ]]; then
            node scripts/test.cjs
        else
            node node_modules/typescript/bin/tsc --noEmit
        fi
    ) >"$results/$check.log" 2>&1; then
        printf '%s: passed\n' "$check"
    else
        printf '%s: FAILED (see %s/%s.log)\n' "$check" "$results" "$check"
        failed=1
    fi
done

printf 'Logs: %s\n' "$results"
exit "$failed"
