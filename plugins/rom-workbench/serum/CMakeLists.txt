cmake_minimum_required(VERSION 3.25)
project(pac2serum LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 99)

# ── heatshrink ────────────────────────────────────────────────────────────────
include(FetchContent)
FetchContent_Declare(heatshrink
    GIT_REPOSITORY https://github.com/atomicobject/heatshrink.git
    GIT_TAG        7d419e1fa4830d0b919b9b6a91fe2fb786cf3280  # pinned (was master)
)
FetchContent_MakeAvailable(heatshrink)

# heatshrink has no CMakeLists so we wire it up manually
add_library(heatshrink STATIC
    ${heatshrink_SOURCE_DIR}/heatshrink_decoder.c
    ${heatshrink_SOURCE_DIR}/heatshrink_encoder.c
)
target_include_directories(heatshrink PUBLIC ${heatshrink_SOURCE_DIR})
# Dynamic alloc lets us set window/lookahead at runtime
target_compile_definitions(heatshrink PUBLIC HEATSHRINK_DYNAMIC_ALLOC=1)

# ── libserum (static) ─────────────────────────────────────────────────────────
# Pinned to the EXACT release bundled in Visual Pinball (VPinballX_GL ships
# libserum 2.6.0, built from PPUC/libserum @ tag v2.6.0).  Writer (pac2serum)
# and the VPX runtime reader must be the same libserum so the cereal-serialised
# SerumData layout in the .cROMc matches byte-for-byte.  Verified: a cROMc
# written here loads in the bundled dylib with palettes byte-identical.
#
# Local changes (applied from vendor/patches/ so a fresh checkout reproduces both
# the exact writer AND the runtime dylib we deploy into the VPX bundle):
#   0001 — additive setAtIndex() helper (no data members / serialize() change →
#          on-disk format identical to stock v2.6.0; needed by the writer).
#   0002 — default maxFramesToSkip non-zero so UNCOLORIZED screens pass through
#          (show the original DMD) instead of freezing on the last colorized frame.
#          Only matters for the runtime reader (serum_shared, below); the BGFX
#          plugin-serum never calls Serum_SetMaximumUnknownFramesToSkip itself.
set(LIBSERUM_PATCHES
    ${CMAKE_CURRENT_SOURCE_DIR}/vendor/patches/0001-sparse-vector-setAtIndex.patch
    ${CMAKE_CURRENT_SOURCE_DIR}/vendor/patches/0002-passthrough-unknown-frames.patch)
FetchContent_Declare(libserum
    GIT_REPOSITORY https://github.com/PPUC/libserum.git
    GIT_TAG        21b28325c4272724e719ab2d17481d851eaf9fd8  # tag v2.6.0
    # Idempotent: for each patch, skip if already applied else apply.
    PATCH_COMMAND  ${CMAKE_COMMAND} -DPATCHES=${LIBSERUM_PATCHES} -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/apply_patches.cmake
)
# Download only — we compile the sources we need into our own serum_static
# target below (NOT FetchContent_MakeAvailable, which would run libserum's own
# CMakeLists and clash with our target / mis-detect the platform).
FetchContent_GetProperties(libserum)
if(NOT libserum_POPULATED)
    FetchContent_Populate(libserum)
endif()
set(SERUM_DIR ${libserum_SOURCE_DIR})

add_library(serum_static STATIC
    ${SERUM_DIR}/src/serum-decode.cpp
    ${SERUM_DIR}/src/SerumData.cpp
    ${SERUM_DIR}/src/SceneGenerator.cpp
    ${SERUM_DIR}/third-party/include/miniz/miniz.c
    ${SERUM_DIR}/third-party/include/lz4/lz4.c
    ${SERUM_DIR}/third-party/include/lz4/lz4hc.c
)

target_include_directories(serum_static PUBLIC
    ${SERUM_DIR}/src
    ${SERUM_DIR}/third-party/include
)

target_compile_features(serum_static PUBLIC cxx_std_20)

if(APPLE)
    target_compile_options(serum_static PRIVATE -Wno-deprecated-declarations)
endif()

# ── libserum (shared dylib for the VPX bundle) ────────────────────────────────
# Same pinned + patched sources as serum_static, built as a dylib so we deploy a
# runtime libserum whose SerumData layout matches the writer byte-for-byte AND
# whose maxFramesToSkip default (patch 0002) makes uncolorized frames pass through.
# Self-contained (miniz/lz4 compiled in; only libc++/libSystem). Install name
# matches what the bundle's libdmdutil resolves (@rpath/libserum.2.6.0.dylib); the
# setup skill copies it over the bundle's libserum.2.2.0.dylib file.
if(APPLE)
    add_library(serum_shared SHARED
        ${SERUM_DIR}/src/serum-decode.cpp
        ${SERUM_DIR}/src/SerumData.cpp
        ${SERUM_DIR}/src/SceneGenerator.cpp
        ${SERUM_DIR}/third-party/include/miniz/miniz.c
        ${SERUM_DIR}/third-party/include/lz4/lz4.c
        ${SERUM_DIR}/third-party/include/lz4/lz4hc.c
    )
    target_include_directories(serum_shared PRIVATE
        ${SERUM_DIR}/src ${SERUM_DIR}/third-party/include)
    target_compile_features(serum_shared PUBLIC cxx_std_20)
    target_compile_options(serum_shared PRIVATE -Wno-deprecated-declarations)
    set_target_properties(serum_shared PROPERTIES
        OUTPUT_NAME serum
        LINK_FLAGS "-Wl,-install_name,@rpath/libserum.2.6.0.dylib")
    # Refresh the prebuilt the setup skill deploys into the VPX bundle.
    add_custom_command(TARGET serum_shared POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy $<TARGET_FILE:serum_shared>
                ${CMAKE_CURRENT_SOURCE_DIR}/../lib/libserum.dylib
        COMMENT "Updating lib/libserum.dylib (passthrough-enabled runtime reader)")
endif()

# ── pac2serum ─────────────────────────────────────────────────────────────────
add_executable(pac2serum src/pac2serum.cpp)

target_link_libraries(pac2serum PRIVATE serum_static heatshrink)
target_include_directories(pac2serum PRIVATE
    ${SERUM_DIR}/src
    ${SERUM_DIR}/third-party/include
    ${heatshrink_SOURCE_DIR}
)
target_compile_options(pac2serum PRIVATE -Wno-deprecated-declarations)

install(TARGETS pac2serum DESTINATION ${CMAKE_INSTALL_PREFIX}/bin)
