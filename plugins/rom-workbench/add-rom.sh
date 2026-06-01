#!/usr/bin/env bash
# add-rom.sh — macOS: register a WPC ROM (and optionally its VPX table) with
# the record-pinball skill.
#
# Stages the ROM zip into the PinMAME roms directory and records the .vpx path
# in config.json so replay.py can locate it by ROM name.
#
# Usage:
#   bash <skill-dir>/add-rom.sh --rom-zip <path> [--rom <name>]
#                               [--table <path-to-vpx>] [--skip-table]
#                               [--config <path>] [--force]
#
# Idempotent. Run once per game you want to replay.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()  { echo -e "\n${YELLOW}==> $*${NC}"; }
ok()    { echo -e "    ${GREEN}ok:${NC} $*"; }
warn()  { echo -e "    ${YELLOW}warn:${NC} $*"; }
die()   { echo -e "    ${RED}error:${NC} $*" >&2; exit 1; }

ROM_ZIP=""
ROM=""
TABLE=""
SKIP_TABLE=0
CONFIG_PATH="./config.json"
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rom-zip)    ROM_ZIP="$2"; shift 2 ;;
        --rom)        ROM="$2"; shift 2 ;;
        --table)      TABLE="$2"; shift 2 ;;
        --skip-table) SKIP_TABLE=1; shift ;;
        --config)     CONFIG_PATH="$2"; shift 2 ;;
        --force)      FORCE=1; shift ;;
        *) die "Unknown argument: $1" ;;
    esac
done

[[ -z "$ROM_ZIP" ]] && die "--rom-zip is required."
[[ -f "$ROM_ZIP" ]] || die "ROM zip not found: ${ROM_ZIP}"
ROM_ZIP="$(cd "$(dirname "$ROM_ZIP")" && pwd)/$(basename "$ROM_ZIP")"

if [[ -z "$ROM" ]]; then
    ROM="$(basename "$ROM_ZIP" .zip)"
fi
if ! [[ "$ROM" =~ ^[a-z0-9_]+$ ]]; then
    warn "ROM name '${ROM}' contains unusual characters (expected [a-z0-9_]+)."
fi

# ---------------------------------------------------------------------------
# Resolve PINMAME_DIR
# ---------------------------------------------------------------------------

PINMAME_DIR="${PINMAME_DIR:-}"
if [[ -z "$PINMAME_DIR" ]]; then
    # Try reading from shell env files.
    for rc in "$HOME/.zshenv" "$HOME/.bash_profile"; do
        if [[ -f "$rc" ]]; then
            val=$(grep "^export PINMAME_DIR=" "$rc" | tail -1 | sed 's/^export PINMAME_DIR="\(.*\)"$/\1/')
            if [[ -n "$val" ]]; then PINMAME_DIR="$val"; break; fi
        fi
    done
fi
[[ -z "$PINMAME_DIR" ]] && die "PINMAME_DIR not set. Run setup-pinball.sh first."

ROMS_DIR="$PINMAME_DIR/roms"
mkdir -p "$ROMS_DIR"

# ---------------------------------------------------------------------------
# Stage ROM zip
# ---------------------------------------------------------------------------

step "Staging ROM: ${ROM}"

STAGED_ZIP="$ROMS_DIR/${ROM}.zip"
if [[ -f "$STAGED_ZIP" && "$FORCE" == "0" ]]; then
    src_sha=$(shasum -a 256 "$ROM_ZIP" | awk '{print $1}')
    dst_sha=$(shasum -a 256 "$STAGED_ZIP" | awk '{print $1}')
    if [[ "$src_sha" == "$dst_sha" ]]; then
        ok "Already staged at ${STAGED_ZIP} (hash matches)."
    else
        warn "Already staged at ${STAGED_ZIP} but hash differs. Pass --force to replace."
    fi
else
    cp "$ROM_ZIP" "$STAGED_ZIP"
    ok "Copied ${ROM_ZIP} -> ${STAGED_ZIP}"
fi

# ---------------------------------------------------------------------------
# Register VPX table in config.json
# ---------------------------------------------------------------------------

VPINBALL_DIR="${VPINBALL_DIR:-}"
if [[ -z "$VPINBALL_DIR" ]]; then
    for rc in "$HOME/.zshenv" "$HOME/.bash_profile"; do
        if [[ -f "$rc" ]]; then
            val=$(grep "^export VPINBALL_DIR=" "$rc" | tail -1 | sed 's/^export VPINBALL_DIR="\(.*\)"$/\1/')
            if [[ -n "$val" ]]; then VPINBALL_DIR="$val"; break; fi
        fi
    done
fi

# Determine the tables directory (optional — may not be set if VPX isn't installed).
TABLES_DIR=""
if [[ -n "$VPINBALL_DIR" ]]; then
    TABLES_DIR="$VPINBALL_DIR/Tables"
    mkdir -p "$TABLES_DIR"
fi

step "VPX table for ${ROM}"

PYTHON_EXE="${PYTHON_FOR_RP:-python3}"

# Use Python to read/write config.json (handles missing file gracefully).
CONFIG_ABS="$(cd "$(dirname "$CONFIG_PATH")" 2>/dev/null && pwd)/$(basename "$CONFIG_PATH")" || CONFIG_ABS="$CONFIG_PATH"

resolve_table_in_config() {
    "$PYTHON_EXE" - "$CONFIG_ABS" "$ROM" <<'EOF'
import json, sys
cfg_path, rom = sys.argv[1], sys.argv[2]
try:
    cfg = json.loads(open(cfg_path).read())
except Exception:
    cfg = {}
tables = cfg.get("tables", {})
v = tables.get(rom, "")
print(v if v and __import__("os").path.isfile(v) else "")
EOF
}

write_table_to_config() {
    local table_path="$1"
    "$PYTHON_EXE" - "$CONFIG_ABS" "$ROM" "$table_path" <<'EOF'
import json, sys, os
cfg_path, rom, tbl = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    cfg = json.loads(open(cfg_path).read())
except Exception:
    cfg = {}
cfg.setdefault("tables", {})[rom] = tbl
os.makedirs(os.path.dirname(cfg_path) or ".", exist_ok=True)
open(cfg_path, "w").write(json.dumps(cfg, indent=2))
EOF
}

RESOLVED_TABLE=""

if [[ -n "$TABLE" ]]; then
    [[ -f "$TABLE" ]] || die "Table file not found: ${TABLE}"
    TABLE="$(cd "$(dirname "$TABLE")" && pwd)/$(basename "$TABLE")"
    if [[ -n "$TABLES_DIR" ]]; then
        TARGET="$TABLES_DIR/$(basename "$TABLE")"
        cp "$TABLE" "$TARGET"
        RESOLVED_TABLE="$TARGET"
    else
        RESOLVED_TABLE="$TABLE"
    fi
    ok "Table at ${RESOLVED_TABLE}"
else
    EXISTING="$(resolve_table_in_config)"
    if [[ -n "$EXISTING" && "$FORCE" == "0" ]]; then
        ok "Table already registered: ${EXISTING}"
        RESOLVED_TABLE="$EXISTING"
    elif [[ "$SKIP_TABLE" == "1" ]]; then
        warn "Skipping table registration. replay.py does not need a table; VPX recording only."
    else
        echo ""
        echo "  VPX tables are third-party community content (VPUniverse / VPForums)."
        echo "  Options:"
        echo "    [1] Paste a .vpx path now."
        echo "    [2] Skip (replay.py works without a table)."
        read -rp "  Choose [1/2]: " choice
        case "$choice" in
            1)
                read -rp "  Path to .vpx: " path
                path="${path//\"/}"  # strip quotes
                [[ -f "$path" ]] || die "Not a file: ${path}"
                path="$(cd "$(dirname "$path")" && pwd)/$(basename "$path")"
                if [[ -n "$TABLES_DIR" ]]; then
                    TARGET="$TABLES_DIR/$(basename "$path")"
                    cp "$path" "$TARGET"
                    RESOLVED_TABLE="$TARGET"
                else
                    RESOLVED_TABLE="$path"
                fi
                ok "Table at ${RESOLVED_TABLE}"
                ;;
            2)  warn "Skipping table." ;;
            *)  die "Cancelled." ;;
        esac
    fi
fi

if [[ -n "$RESOLVED_TABLE" ]]; then
    write_table_to_config "$RESOLVED_TABLE"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}Registered ${ROM}.${NC}"
echo "  Staged ROM:  ${STAGED_ZIP}"
if [[ -n "$RESOLVED_TABLE" ]]; then
    echo "  VPX table:   ${RESOLVED_TABLE}"
else
    echo -e "  VPX table:   ${YELLOW}(none)${NC}"
fi
echo ""
echo "Next: replay.py --rom ${ROM} --rom-zip ${ROM_ZIP} --session <session-dir> --nvram <nv-file>"
