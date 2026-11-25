#!/bin/sh
# Start the dev agent-cursor overlay for trial runs, then:  export SUPYMEM_AGENT_CURSOR_SOCK=<printed path>
# Stop with: pkill -f cu_cursor_host; pkill -f "http.server 3118"
set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${CU_CURSOR_DIR:-${TMPDIR:-/tmp}/supymem-cu-cursor}"
mkdir -p "$OUT"
[ -x "$OUT/cu_cursor_host" ] || swiftc -O "$ROOT/scripts/desktop/cu_cursor_host.swift" -o "$OUT/cu_cursor_host"
pkill -f "http.server 3118" 2>/dev/null || true
pkill -f "$OUT/cu_cursor_host" 2>/dev/null || true
(cd "$ROOT/src-tauri/splashscreen" && nohup /usr/bin/python3 -m http.server 3118 --bind 127.0.0.1 >/dev/null 2>&1 &)
sleep 1
nohup "$OUT/cu_cursor_host" "$OUT/cursor.sock" "http://127.0.0.1:3118/agent-cursor/index.html" >"$OUT/host.log" 2>&1 &
sleep 2
echo "$OUT/cursor.sock"
