#!/usr/bin/env bash
# setup-pinball.sh — macOS one-time installer for the record-pinball toolchain.
#
# Installs:
#   1. Checks Python 3.10+ and build prerequisites (cmake, git, clang).
#   2. Downloads and installs Visual Pinball X (macOS build).
#   3. Builds the patched libpinmame.dylib from source and installs it.
#   4. Writes VPINBALL_DIR, PINMAME_DIR, PYTHON_FOR_RP to ~/.zshenv
#      (and ~/.bash_profile for bash users).
#
# Usage:
#   bash <skill-dir>/setup-pinball.sh [--force] [--pinmame-src <path>]
#
# --force       Re-download and rebuild everything even if already present.
# --pinmame-src Path to a pinmame clone with the switch-recorder branch checked
#               out and patches applied. Defaults to ../pinmame relative to
#               this script's repo root (two directories up from the skill dir).
#
# Idempotent: re-running with no changes skips completed steps.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$SCRIPT_DIR"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

step()  { echo -e "\n${YELLOW}==> $*${NC}"; }
ok()    { echo -e "    ${GREEN}ok:${NC} $*"; }
warn()  { echo -e "    ${YELLOW}warn:${NC} $*"; }
die()   { echo -e "    ${RED}error:${NC} $*" >&2; exit 1; }

FORCE=0
PINMAME_SRC=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)        FORCE=1; shift ;;
        --pinmame-src)  PINMAME_SRC="$2"; shift 2 ;;
        *) die "Unknown argument: $1" ;;
    esac
done

# Default pinmame source: ../pinmame relative to the repo root.
if [[ -z "$PINMAME_SRC" ]]; then
    PINMAME_SRC="$(cd "$REPO_DIR/.." && pwd)/pinmame"
fi

# ---------------------------------------------------------------------------
# Env-var persistence helpers
# ---------------------------------------------------------------------------

set_shell_env() {
    local name="$1" value="$2"
    local export_line="export ${name}=\"${value}\""
    for rc in "$HOME/.zshenv" "$HOME/.bash_profile"; do
        if [[ ! -f "$rc" ]]; then touch "$rc"; fi
        # Remove any previous line setting this var, then append.
        grep -v "^export ${name}=" "$rc" > "${rc}.tmp" && mv "${rc}.tmp" "$rc"
        echo "$export_line" >> "$rc"
    done
    # Export into current session so subsequent steps see it.
    export "$name"="$value"
}

# ---------------------------------------------------------------------------
# Step 1: Python 3.10+
# ---------------------------------------------------------------------------

step "Checking Python >= 3.10"

MIN_PYTHON_MINOR=10
PYTHON_EXE=""

for candidate in python3 python; do
    if ! command -v "$candidate" &>/dev/null; then continue; fi
    version_str=$("$candidate" -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))" 2>/dev/null || true)
    if [[ -z "$version_str" ]]; then continue; fi
    major="${version_str%%.*}"; minor="${version_str#*.}"
    if (( major > 3 || (major == 3 && minor >= MIN_PYTHON_MINOR) )); then
        PYTHON_EXE="$(command -v "$candidate")"
        ok "Python ${version_str} at ${PYTHON_EXE}"
        break
    else
        warn "Found ${candidate} ${version_str} — too old, skipping."
    fi
done

if [[ -z "$PYTHON_EXE" ]]; then
    die "Python 3.${MIN_PYTHON_MINOR}+ not found. Install from https://www.python.org/downloads/ or via Homebrew: brew install python3"
fi

set_shell_env PYTHON_FOR_RP "$PYTHON_EXE"

# ---------------------------------------------------------------------------
# Step 2: Build prerequisites
# ---------------------------------------------------------------------------

step "Checking build prerequisites (cmake, git, clang)"

for tool in cmake git; do
    if ! command -v "$tool" &>/dev/null; then
        die "'${tool}' not found. Install via Homebrew: brew install ${tool}"
    fi
    ok "${tool} at $(command -v "$tool")"
done

# clang ships with Xcode Command Line Tools; check for it.
if ! command -v clang &>/dev/null; then
    die "clang not found. Install Xcode Command Line Tools: xcode-select --install"
fi
ok "clang at $(command -v clang)"

CMAKE_VER=$(cmake --version | head -1)
ok "cmake: ${CMAKE_VER}"

# ---------------------------------------------------------------------------
# Step 3: Visual Pinball X (macOS)
# ---------------------------------------------------------------------------

# VPX release to install. 10.8.0 is the current stable; 10.8.1+ are pre-release.
# The tag and the asset filename can differ slightly (GitHub quirk on some releases),
# so they are pinned separately.
# Newer (10.8.1+) asset format: VPinballX_GL-<ver>-macos-<arch>-Release.zip
# Stable (10.8.0)  asset format: VPinballX_GL-<ver>-Release-macos-<arch>.zip
VPX_TAG="10.8.0-2051-28dd6c3"
ARCH=$(uname -m)   # arm64 or x86_64
VPX_ASSET="VPinballX_GL-10.8.0-2052-5a81d4e-Release-macos-${ARCH}.zip"
VPX_ASSET_ALT=""   # no alternate name for this release
VPX_URL="https://github.com/vpinball/vpinball/releases/download/v${VPX_TAG}/${VPX_ASSET}"
VPX_URL_ALT=""
VPX_VERSION="$VPX_TAG"  # used only for install-dir labelling below
VPX_INSTALL_DIR="$HOME/Library/Application Support/VPinball"

step "Visual Pinball X at ${VPX_INSTALL_DIR}"

# VPX ships as a .app bundle; the executable lives inside Contents/MacOS/.
VPX_APP="$VPX_INSTALL_DIR/VPinballX_GL.app"
VPX_EXE="$VPX_APP/Contents/MacOS/VPinballX_GL"
if [[ -f "$VPX_EXE" && "$FORCE" == "0" ]]; then
    ok "VPinballX_GL.app present; skipping."
else
    CACHE_DIR="$HOME/Library/Caches/record-pinball"
    mkdir -p "$CACHE_DIR"
    VPX_ZIP="$CACHE_DIR/$VPX_ASSET"

    if [[ ! -f "$VPX_ZIP" || "$FORCE" == "1" ]]; then
        echo "    Downloading ${VPX_URL} ..."
        if ! curl -fL --progress-bar -o "$VPX_ZIP" "$VPX_URL" 2>/dev/null; then
            echo "    Trying alternate asset name (${VPX_ASSET_ALT}) ..."
            VPX_ASSET="$VPX_ASSET_ALT"; VPX_ZIP="$CACHE_DIR/$VPX_ASSET"
            if ! curl -fL --progress-bar -o "$VPX_ZIP" "$VPX_URL_ALT" 2>/dev/null; then
                warn "VPX macOS download failed — this release may not have a macOS asset yet."
                warn "Check https://github.com/vpinball/vpinball/releases for a mac/macos ${ARCH} build."
                warn "Skipping VPX install; replay.py does not require VPX."
                VPX_INSTALL_DIR=""
            fi
        fi
    fi

    if [[ -n "$VPX_INSTALL_DIR" && -f "$VPX_ZIP" ]]; then
        mkdir -p "$VPX_INSTALL_DIR"
        # The zip may contain a .dmg (newer releases) or a flat archive (older).
        # Detect by listing the zip contents.
        FIRST_ENTRY=$(unzip -Z1 "$VPX_ZIP" 2>/dev/null | head -1)
        if [[ "$FIRST_ENTRY" == *.dmg ]]; then
            echo "    Extracting DMG from zip ..."
            unzip -q -o "$VPX_ZIP" -d "$VPX_INSTALL_DIR"
            DMG_FILE=$(find "$VPX_INSTALL_DIR" -maxdepth 1 -name "*.dmg" | head -1)
            if [[ -z "$DMG_FILE" ]]; then
                warn "No .dmg found after extraction — check the zip layout."
                VPX_INSTALL_DIR=""
            else
                echo "    Mounting ${DMG_FILE} ..."
                MOUNT_POINT=$(mktemp -d)
                hdiutil attach "$DMG_FILE" -mountpoint "$MOUNT_POINT" -nobrowse -quiet
                if [[ -d "$MOUNT_POINT/VPinballX_GL.app" ]]; then
                    echo "    Copying VPinballX_GL.app ..."
                    cp -a "$MOUNT_POINT/VPinballX_GL.app" "$VPX_INSTALL_DIR/"
                fi
                hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null || true
                rmdir "$MOUNT_POINT" 2>/dev/null || true
                rm -f "$DMG_FILE"
            fi
        else
            echo "    Extracting to ${VPX_INSTALL_DIR} ..."
            unzip -q -o "$VPX_ZIP" -d "$VPX_INSTALL_DIR"
            # Flatten a single top-level directory if the .app isn't at the root.
            nested=$(find "$VPX_INSTALL_DIR" -maxdepth 1 -mindepth 1 -type d ! -name "*.app" | head -1)
            if [[ -n "$nested" && ! -d "$VPX_APP" ]]; then
                mv "$nested"/* "$VPX_INSTALL_DIR/" 2>/dev/null || true
                rmdir "$nested" 2>/dev/null || true
            fi
        fi
        if [[ -f "$VPX_EXE" ]]; then
            chmod +x "$VPX_EXE"
            ok "Visual Pinball X installed at ${VPX_INSTALL_DIR}"
        else
            warn "Extraction did not produce VPinballX_GL.app — check the zip layout."
            VPX_INSTALL_DIR=""
        fi
    fi
fi

if [[ -n "$VPX_INSTALL_DIR" ]]; then
    set_shell_env VPINBALL_DIR "$VPX_INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
# Step 4: Install patched libpinmame.dylib
# ---------------------------------------------------------------------------
# Common case: use the pre-built dylib from record-pinball/bin/libpinmame.dylib.
# Fallback: build from source when the pre-built is missing (e.g. after an arch
# mismatch) or --force is passed.

step "Installing patched libpinmame.dylib"

PINMAME_INSTALL_DIR="$HOME/.pinmame"
mkdir -p "$PINMAME_INSTALL_DIR"

DYLIB_TARGET="$PINMAME_INSTALL_DIR/libpinmame.dylib"
SKILL_BIN="$(cd "$SCRIPT_DIR/../record-pinball/bin" && pwd)"
PREBUILT="$SKILL_BIN/libpinmame.dylib"

install_dylib() {
    local src="$1"
    cp "$src" "$DYLIB_TARGET"
    ok "Installed to ${DYLIB_TARGET}"
}

if [[ -f "$DYLIB_TARGET" && "$FORCE" == "0" ]]; then
    ok "libpinmame.dylib present at ${DYLIB_TARGET}; skipping."
elif [[ -f "$PREBUILT" && "$FORCE" == "0" ]]; then
    # Verify architecture matches this machine before using the pre-built.
    PREBUILT_ARCH=$(file "$PREBUILT" | grep -o "arm64\|x86_64" | head -1 || true)
    if [[ "$PREBUILT_ARCH" == "$ARCH" ]]; then
        install_dylib "$PREBUILT"
        ok "Deployed from bin/libpinmame.dylib (pre-built ${ARCH})."
    else
        warn "bin/libpinmame.dylib is ${PREBUILT_ARCH:-unknown arch}, machine is ${ARCH} — will build from source."
        FORCE=1  # fall through to source build
    fi
fi

# Source build (only reached when no usable dylib is already installed).
if [[ ! -f "$DYLIB_TARGET" ]]; then
    if [[ ! -d "$PINMAME_SRC" ]]; then
        die "PinMAME source not found at ${PINMAME_SRC}.\nClone it: git clone https://github.com/vpinball/pinmame.git ${PINMAME_SRC}"
    fi

    # Apply patches if the switch-recorder branch + Debug API aren't present.
    if ! grep -q "PinmameDebugAttach" "$PINMAME_SRC/src/libpinmame/libpinmame.h" 2>/dev/null; then
        echo "    Patches not yet applied to ${PINMAME_SRC}. Applying now ..."
        PATCHES_DIR="$SCRIPT_DIR/../record-pinball/pinmame-patches"
        (
            cd "$PINMAME_SRC"
            if ! git rev-parse --verify switch-recorder &>/dev/null; then
                BASE="3ef424b0a560b08b563a345d1ecd0fa733533eef"
                if ! git cat-file -e "${BASE}^{commit}" 2>/dev/null; then
                    die "Base commit ${BASE} not in ${PINMAME_SRC}. Fetch: git -C ${PINMAME_SRC} fetch origin"
                fi
                git checkout -b switch-recorder "$BASE"
            else
                git checkout switch-recorder
            fi
            git am --3way "$PATCHES_DIR"/0001-*.patch \
                          "$PATCHES_DIR"/0002-*.patch \
                          "$PATCHES_DIR"/0003-*.patch
        )
        ok "Patches applied."
    fi

    # cmake -S requires a file named CMakeLists.txt at the source root.
    # CMakeLists_libpinmame.txt (added by patch 1) is that file — copy it
    # temporarily so cmake can find it, then remove after the build.
    CMAKE_SRC="$PINMAME_SRC/CMakeLists_libpinmame.txt"
    CMAKE_WRAPPER="$PINMAME_SRC/CMakeLists.txt"
    CLEANUP_CMAKE_WRAPPER=0
    if [[ ! -f "$CMAKE_WRAPPER" ]]; then
        [[ -f "$CMAKE_SRC" ]] || die "CMakeLists_libpinmame.txt missing from ${PINMAME_SRC} — did the patches apply?"
        cp "$CMAKE_SRC" "$CMAKE_WRAPPER"
        CLEANUP_CMAKE_WRAPPER=1
    fi

    BUILD_DIR="$PINMAME_SRC/build_macos_${ARCH}"
    echo "    cmake configure (PLATFORM=macos ARCH=${ARCH}) ..."
    cmake -S "$PINMAME_SRC" -B "$BUILD_DIR" \
        -DPLATFORM=macos -DARCH="$ARCH" \
        -DBUILD_SHARED=ON -DBUILD_STATIC=OFF \
        -DCMAKE_BUILD_TYPE=Release -Wno-dev > /dev/null

    NPROC=$(sysctl -n hw.logicalcpu)
    echo "    cmake build (${NPROC} jobs) ..."
    cmake --build "$BUILD_DIR" --target pinmame_shared -j "$NPROC"

    [[ "$CLEANUP_CMAKE_WRAPPER" == "1" ]] && rm -f "$CMAKE_WRAPPER"

    BUILT_DYLIB=$(find "$BUILD_DIR" -maxdepth 1 -name "libpinmame*.dylib" ! -type l | head -1)
    [[ -n "$BUILT_DYLIB" ]] || die "libpinmame*.dylib not found under ${BUILD_DIR} after build."

    nm -gU "$BUILT_DYLIB" 2>/dev/null | grep -q "_PinmameDebugAttach" \
        || die "PinmameDebugAttach missing from ${BUILT_DYLIB} — patches may not have applied."

    install_dylib "$BUILT_DYLIB"
    # Refresh the bundled copy so future installs on the same arch can use it.
    cp "$BUILT_DYLIB" "$PREBUILT"
    ok "Updated bin/libpinmame.dylib from source build."
fi

set_shell_env PINMAME_DIR "$PINMAME_INSTALL_DIR"

# ---------------------------------------------------------------------------
# Step 5: Install libpinmame into Visual Pinball (if VPX is installed)
# ---------------------------------------------------------------------------

step "Deploying libpinmame into Visual Pinball bundle"

# On macOS, VPX bundles its own libpinmame inside the app directory.
# Replacing it with the patched build lets VPX itself use our debug API
# (though for replay.py, only PINMAME_DIR matters).
deploy_to_bundle() {
    local dylib_path="$1"
    local BACKUP="${dylib_path}.orig"
    if [[ ! -f "$BACKUP" ]]; then
        cp "$dylib_path" "$BACKUP"
        ok "Backed up original to $(basename "$BACKUP")"
    fi
    cp "$DYLIB_TARGET" "$dylib_path"
    ok "Patched libpinmame deployed to ${dylib_path}"

    # Replacing a dylib inside the bundle breaks its code-signature seal —
    # macOS Gatekeeper will refuse to launch the app ("damaged"). Re-sign.
    local VPX_APP FRAMEWORKS_DIR SIGN_ERRORS=0
    VPX_APP="$(dirname "$(dirname "$(dirname "$dylib_path")")")"
    FRAMEWORKS_DIR="$(dirname "$dylib_path")"
    if [[ "$VPX_APP" == *.app ]]; then
        step "Re-signing VPX bundle (ad-hoc): $(basename "$VPX_APP")"
        while IFS= read -r -d '' f; do
            codesign --force --sign - "$f" 2>/dev/null || { warn "Could not sign $(basename "$f")"; SIGN_ERRORS=$((SIGN_ERRORS+1)); }
        done < <(find "$FRAMEWORKS_DIR" -type f \( -name "*.dylib" -o -name "*.so" \) -print0)
        codesign --force --sign - "$VPX_APP/Contents/MacOS/VPinballX_GL" 2>/dev/null \
            || warn "Could not sign VPinballX_GL executable."
        if codesign --force --sign - "$VPX_APP" 2>/dev/null; then
            ok "Bundle re-signed: $VPX_APP"
        else
            warn "Bundle re-sign failed — VPX may show 'damaged' on launch. Try: codesign --force --deep --sign - '$VPX_APP'"
        fi
        (( SIGN_ERRORS == 0 )) || warn "$SIGN_ERRORS framework(s) could not be signed (likely pre-existing issues)."
    fi
}

FOUND_ANY=0
# Collect all libpinmame dylibs across both install locations (versioned or not).
while IFS= read -r candidate; do
    deploy_to_bundle "$candidate"
    FOUND_ANY=1
done < <(find \
    "$HOME/Library/Application Support/VPinball/VPinballX_GL.app" \
    "/Applications/VPinballX_GL.app" \
    -name "libpinmame*.dylib" ! -name "*.orig" -type f 2>/dev/null | sort -u)

if (( FOUND_ANY == 0 )); then
    warn "VPX bundled libpinmame not found — skipping VPX bundle deploy."
    warn "(replay.py only needs PINMAME_DIR; this step only matters if using VPX directly.)"
fi

# ---------------------------------------------------------------------------
# Step 6: Gatekeeper check — trial launch VPX
# ---------------------------------------------------------------------------
# On macOS 15+ (Sequoia / macOS 26) Gatekeeper blocks apps downloaded from the
# internet even after ad-hoc re-signing, because the original notarisation
# ticket's CDHash no longer matches. The user must explicitly allow the app
# once via System Settings → Privacy & Security → Security.
#
# We do a brief trial launch here so that:
#   a) the block shows up immediately (not later when the user tries to record),
#   b) we can open the exact System Settings pane and wait while they click Allow.

if [[ -n "${VPX_INSTALL_DIR:-}" ]]; then
    step "Testing VPX launch (Gatekeeper check)"
    VPX_TEST_EXE="$VPX_INSTALL_DIR/VPinballX_GL.app/Contents/MacOS/VPinballX_GL"

    gatekeeper_trial() {
        "$VPX_TEST_EXE" &>/dev/null &
        local pid=$!
        sleep 2
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
            return 0   # still alive after 2s — Gatekeeper allowed it
        fi
        wait "$pid" 2>/dev/null
        return 1       # died immediately — Gatekeeper blocked it
    }

    if gatekeeper_trial; then
        ok "VPX launches successfully — Gatekeeper is happy."
    else
        warn "Gatekeeper blocked VPX. Opening System Settings..."
        echo ""
        echo "    macOS blocked the app because we replaced a dylib inside the"
        echo "    notarised bundle. You need to allow it once:"
        echo ""
        echo "      System Settings → Privacy & Security → Security"
        echo "      → look for 'VPinballX_GL was blocked' and click 'Open Anyway'"
        echo ""
        # Open directly to the Security section of Privacy & Security.
        open "x-apple.systempreferences:com.apple.preference.security?General" 2>/dev/null || true
        read -rp "    Press Enter here once you have clicked 'Open Anyway'... "
        echo ""

        if gatekeeper_trial; then
            ok "VPX launches successfully after authorization."
        else
            warn "VPX is still being blocked. You may need to run:"
            warn "  sudo spctl --master-disable"
            warn "confirm in System Settings, then re-run this script."
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}Toolchain setup complete.${NC}"
echo "  PINMAME_DIR   = ${PINMAME_INSTALL_DIR}"
echo "  PYTHON_FOR_RP = ${PYTHON_EXE}"
[[ -n "${VPX_INSTALL_DIR:-}" ]] && echo "  VPINBALL_DIR  = ${VPX_INSTALL_DIR}"
echo ""
echo "Env vars written to ~/.zshenv and ~/.bash_profile."
echo "Restart your shell or run:  source ~/.zshenv"
echo ""
echo "Next: register a ROM with add-rom.sh, e.g."
echo "  bash $(realpath "$SKILL_DIR")/add-rom.sh --rom-zip ./orig/congo_21.zip"
