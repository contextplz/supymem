#!/usr/bin/env bash
# Curl smoke for Company OS. Same script is reused for staging/production:
# set GATEWAY_URL / CONSOLE_URL (and optionally WARDEN_URL / COMPANY_OS_URL).
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:3200}"
CONSOLE_URL="${CONSOLE_URL:-http://localhost:3100}"
COMPANY_OS_URL="${COMPANY_OS_URL:-}"
WARDEN_URL="${WARDEN_URL:-}"
CONSOLE_PASSPHRASE="${CONSOLE_PASSPHRASE:-}"
export CONSOLE_PASSPHRASE

fail() { echo "smoke FAIL: $*" >&2; exit 1; }
ok() { echo "smoke ok: $*"; }

check_health() {
  local name="$1" url="$2"
  local body
  body="$(curl -sS -f --max-time 8 "${url}/health")" || fail "${name} /health at ${url}"
  echo "$body" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("status")=="ok", d' \
    || fail "${name} /health body ${body}"
  ok "${name} /health"
}

# --- liveness ---
check_health "gateway" "$GATEWAY_URL"

ready="$(curl -sS -f --max-time 8 "${GATEWAY_URL}/ready")" \
  || fail "gateway /ready"
echo "$ready" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if d.get("ready") is not True:
    raise SystemExit("gateway not ready: " + json.dumps(d))
checks = d.get("checks") or {}
core = checks.get("company_os") or checks.get("core") or {}
if isinstance(core, dict) and core.get("ok") is False:
    raise SystemExit("company_os check failed: " + json.dumps(d))
' || fail "gateway /ready body: ${ready}"
ok "gateway /ready"

if [[ -n "$COMPANY_OS_URL" ]]; then
  check_health "company_os" "$COMPANY_OS_URL"
fi
if [[ -n "$WARDEN_URL" ]]; then
  check_health "warden" "$WARDEN_URL"
fi

console_code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 8 "${CONSOLE_URL}/")" \
  || fail "console unreachable at ${CONSOLE_URL}"
[[ "$console_code" == "200" ]] || fail "console GET / -> ${console_code}"
ok "console GET / (${console_code})"

# --- login + authenticated venture read ---
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cookie="${tmp}/cookies"

login="$(curl -sS -f --max-time 10 -c "$cookie" -b "$cookie" \
  -H "Content-Type: application/json" \
  -d "{\"passphrase\":$(python3 -c 'import json,os; print(json.dumps(os.environ["CONSOLE_PASSPHRASE"]))')}" \
  "${GATEWAY_URL}/auth/login")" \
  || fail "Open your local app, or set your hosted operator passphrase, then rerun the smoke check."
echo "$login" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("authenticated") is True, d
' || fail "login not authenticated: ${login}"
ok "owner access"

ventures="$(curl -sS -f --max-time 10 -c "$cookie" -b "$cookie" \
  "${GATEWAY_URL}/api/ventures")" \
  || fail "GET /api/ventures"
vid="$(echo "$ventures" | python3 -c '
import json, sys
d = json.load(sys.stdin)
items = d.get("items") if isinstance(d, dict) else d
if not isinstance(items, list):
    raise SystemExit("unexpected /api/ventures shape")
if not items:
    print("")
    raise SystemExit(0)
row = items[0]
if isinstance(row, dict):
    print(row.get("id") or (row.get("venture") or {}).get("id") or "")
else:
    print(row)
')" || fail "parse /api/ventures: ${ventures}"
ok "GET /api/ventures"

if [[ -n "$vid" ]]; then
  curl -sS -f --max-time 10 -c "$cookie" -b "$cookie" \
    "${GATEWAY_URL}/api/ventures/${vid}" >/dev/null \
    || fail "GET /api/ventures/${vid}"
  ok "GET /api/ventures/${vid}"
else
  echo "smoke note: no ventures yet (run: make local-seed)"
fi

echo "smoke PASS"
