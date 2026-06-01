#!/usr/bin/env bash
# record.sh — macOS session recorder for record-pinball.
#
# Launches Visual Pinball X GL with VPINMAME_SWITCHLOG set so the patched
# libpinmame.dylib logs every externally-driven switch edge to a JSONL file,
# then wraps the captured stream into sessions/<utc>/session.jsonl in the same
# format that replay.py consumes.
#
# Usage:
#   bash record.sh [--rom <name>] [--table <path>] [--max-sec <n>]
#                  [--out-dir <dir>] [--config <path>]
#
# Defaults:
#   --rom     congo_21
#   --table   read from --config (default: ./config.json)
#   --max-sec 600
#   --config  ./config.json (written by pinball-setup/add-rom.sh)
#
# Requires: VPINBALL_DIR and PINMAME_DIR set (run pinball-setup/setup-pinball.sh).
# ROM zip must be staged under $PINMAME_DIR/roms/ (run pinball-setup/add-rom.sh).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()  { echo -e "\n${YELLOW}==> $*${NC}"; }
ok()    { echo -e "    ${GREEN}ok:${NC} $*"; }
warn()  { echo -e "    ${YELLOW}warn:${NC} $*"; }
die()   { echo -e "    ${RED}error:${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

ROM="congo_21"
TABLE=""
MAX_SEC=600
OUT_DIR=""
CONFIG="./config.json"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rom)      ROM="$2";     shift 2 ;;
        --table)    TABLE="$2";   shift 2 ;;
        --max-sec)  MAX_SEC="$2"; shift 2 ;;
        --out-dir)  OUT_DIR="$2"; shift 2 ;;
        --config)   CONFIG="$2";  shift 2 ;;
        *) die "Unknown argument: $1" ;;
    esac
done

# ---------------------------------------------------------------------------
# Env / path resolution
# ---------------------------------------------------------------------------

PINMAME_DIR="${PINMAME_DIR:-}"
VPINBALL_DIR="${VPINBALL_DIR:-}"

[[ -n "$PINMAME_DIR" ]]   || die "PINMAME_DIR not set. Run pinball-setup/setup-pinball.sh and source ~/.zshenv."
[[ -n "$VPINBALL_DIR" ]]  || die "VPINBALL_DIR not set. Run pinball-setup/setup-pinball.sh and source ~/.zshenv."
[[ -d "$PINMAME_DIR" ]]   || die "PINMAME_DIR=$PINMAME_DIR does not exist."
[[ -d "$VPINBALL_DIR" ]]  || die "VPINBALL_DIR=$VPINBALL_DIR does not exist."

VPX_EXE="/Applications/VPinballX_GL.app/Contents/MacOS/VPinballX_GL"
[[ -f "$VPX_EXE" ]] || die "VPinballX_GL not found at $VPX_EXE. Install it from pinball-setup/setup-pinball.sh or drag VPinballX_GL.app to /Applications."

ROM_ZIP="$PINMAME_DIR/roms/$ROM.zip"
[[ -f "$ROM_ZIP" ]] || die "ROM zip not staged at $ROM_ZIP. Run pinball-setup/add-rom.sh --rom-zip <path>."

# ---------------------------------------------------------------------------
# Resolve table
# ---------------------------------------------------------------------------

if [[ -z "$TABLE" ]]; then
    [[ -f "$CONFIG" ]] || die "Config not found at $CONFIG. Run pinball-setup/add-rom.sh or pass --table."
    TABLE=$(python3 -c "
import json, sys
cfg = json.load(open('$CONFIG'))
tables = cfg.get('tables', {})
t = tables.get('$ROM')
if not t:
    sys.exit(1)
print(t)
" 2>/dev/null) || die "No table registered for ROM '$ROM' in $CONFIG. Run add-rom.sh or pass --table."
fi
[[ -f "$TABLE" ]] || die "Table not found: $TABLE"

# ---------------------------------------------------------------------------
# Session output dir
# ---------------------------------------------------------------------------

if [[ -z "$OUT_DIR" ]]; then
    STAMP=$(date -u +%Y%m%dT%H%M%SZ)
    OUT_DIR="$(pwd)/sessions/$STAMP"
fi
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

SESSION_JSONL="$OUT_DIR/session.jsonl"
META_JSON="$OUT_DIR/session.meta.json"
SWITCH_LOG="$OUT_DIR/switchlog.jsonl"

step "Output: $OUT_DIR"
echo "    ROM:   $ROM ($ROM_ZIP)"
echo "    Table: $TABLE"

# ---------------------------------------------------------------------------
# SHA-256 helpers
# ---------------------------------------------------------------------------

sha256() { shasum -a 256 "$1" | awk '{print $1}'; }

ROM_SHA=$(sha256 "$ROM_ZIP")
TABLE_SHA=$(sha256 "$TABLE")

# ---------------------------------------------------------------------------
# Write meta line (first line of session.jsonl)
# ---------------------------------------------------------------------------

START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HOST=$(hostname -s)

python3 - <<PYEOF > "$SESSION_JSONL"
import json, sys
meta = {
    "v": 1,
    "kind": "meta",
    "rom": "$ROM",
    "rom_zip_sha256": "$ROM_SHA",
    "table_path": "$TABLE",
    "table_sha256": "$TABLE_SHA",
    "mode": "VpRecord",
    "start_ts": "$START_TS",
    "end_ts": None,
    "host": "$HOST",
    "pinmame_version": None,
    "vpm_version": None,
    "comment": "macOS record.sh VpRecord session",
    "session_jsonl": "session.jsonl",
}
print(json.dumps(meta))
PYEOF

# ---------------------------------------------------------------------------
# Launch Visual Pinball
# ---------------------------------------------------------------------------

step "Launching Visual Pinball X (VpRecord mode)"
echo "    Close the VPX window when done to stop recording."
warn "Play the table, then close the window to finish."

export VPINMAME_SWITCHLOG="$SWITCH_LOG"
rm -f "$SWITCH_LOG"

# VPX on macOS needs PINMAME_DIR so libpinmame can find the ROM zip.
export PINMAME_DIR

TIMED_OUT=0
# VPX texture cache causes a deadlock at 50% on subsequent loads — wipe it before launch.
VPX_CACHE_DIR="$HOME/.vpinball/Cache"
TABLE_BASENAME="$(basename "$TABLE" .vpx)"
if [[ -d "$VPX_CACHE_DIR/$TABLE_BASENAME" ]]; then
    rm -rf "${VPX_CACHE_DIR:?}/$TABLE_BASENAME"
    ok "Cleared texture cache for $TABLE_BASENAME"
fi

# Must launch via 'open -a' so macOS registers the app with the window server.
# Direct binary exec skips LaunchServices and the SDL window never gets focus.
VPX_APP="/Applications/VPinballX_GL.app"
open -a "$VPX_APP" --args -DisableTrueFullscreen -play "$TABLE"

# Give LaunchServices a moment, then find the PID.
VPX_PID=""
for _ in {1..10}; do
    VPX_PID=$(pgrep -f "VPinballX_GL" | head -1)
    [[ -n "$VPX_PID" ]] && break
    sleep 1
done
[[ -n "$VPX_PID" ]] || die "VPX failed to launch (no process found after 10s)."
ok "VPX running (pid $VPX_PID)"

# Wait for VPX to exit, up to MAX_SEC.
ELAPSED=0
while kill -0 "$VPX_PID" 2>/dev/null; do
    sleep 1
    ELAPSED=$((ELAPSED + 1))
    if (( ELAPSED >= MAX_SEC )); then
        warn "MaxSeconds ($MAX_SEC) reached; sending SIGTERM to VPX."
        kill "$VPX_PID" 2>/dev/null || true
        TIMED_OUT=1
        break
    fi
done
wait "$VPX_PID" 2>/dev/null || true

unset VPINMAME_SWITCHLOG

# ---------------------------------------------------------------------------
# Fold switch stream into session.jsonl
# ---------------------------------------------------------------------------

SW_COUNT=0
if [[ -f "$SWITCH_LOG" ]]; then
    # Filter empty lines and append switch records after the meta line.
    while IFS= read -r line; do
        [[ -z "${line// }" ]] && continue
        echo "$line" >> "$SESSION_JSONL"
        SW_COUNT=$((SW_COUNT + 1))
    done < "$SWITCH_LOG"
    ok "Switch stream: $SW_COUNT events folded into session.jsonl"
else
    warn "No switchlog.jsonl produced — libpinmame may not have loaded, or no switches were driven."
    warn "Check that VPX opened the table and started the ROM."
fi

# ---------------------------------------------------------------------------
# Write session.meta.json
# ---------------------------------------------------------------------------

python3 - <<PYEOF > "$META_JSON"
import json
meta = {
    "rom": "$ROM",
    "rom_zip_sha256": "$ROM_SHA",
    "mode": "VpRecord",
    "table_path": "$TABLE",
    "table_sha256": "$TABLE_SHA",
    "session_jsonl": "session.jsonl",
    "inp": None,
    "labels": [],
    "notes": "",
}
print(json.dumps(meta, indent=2))
PYEOF

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}Recording complete (VpRecord / macOS).${NC}"
echo "  Session: $SESSION_JSONL"
echo "  Switches: $SW_COUNT"
if (( TIMED_OUT )); then
    warn "Session timed out after ${MAX_SEC}s."
fi
echo ""
echo "Next: init NVRAM (if needed) and replay:"
echo "  python3 $SCRIPT_DIR/replay.py \\"
echo "      --rom $ROM --rom-zip '$ROM_ZIP' \\"
echo "      --session '$OUT_DIR' \\"
echo "      --nvram orig/${ROM}.nv --trace state,dmd"
