#!/usr/bin/env bash
# Build the pac2serum converter. Fetches + pins libserum (PPUC v2.6.0, the exact
# release VPinballX_GL bundles) and heatshrink via CMake FetchContent, applies
# the additive setAtIndex patch, and links a static libserum into pac2serum.
#
# Requires: CMake >= 3.25, a C++20 compiler, network access (first build only).
# A prebuilt macOS-arm64 binary is in bin/pac2serum.macos-arm64 if you can't build.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cmake -B "$here/build" -S "$here" -DCMAKE_BUILD_TYPE=Release
cmake --build "$here/build" --parallel
echo "Built: $here/build/pac2serum"
