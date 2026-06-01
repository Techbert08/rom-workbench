# Patched PinMAME source (for rebuilding the bundled libraries)

The prebuilt libraries in `../bin/` (`libpinmame.dylib` for macOS,
`pinmame64.dll`/`libpinmame.dll` for Windows) are built from upstream PinMAME
**plus** the three patches in this directory. **You do not need these to *use*
the tools** — the bundled libraries are game-generic and sufficient for replay +
debug of any WPC title. They're here only so the source of the patch is
persisted off-machine and the bundle can rebuild itself.

## Coordinates

- Upstream: `https://github.com/vpinball/pinmame.git`
- Base commit the patches apply onto: **`3ef424b0a560b08b563a345d1ecd0fa733533eef`**
  (on `origin/master`)
- Exported from the maintainer's local `switch-recorder` branch
  (HEAD `e6dc2fa1`) via `git format-patch origin/master..switch-recorder`.

## The patches

| # | Commit | What it adds |
|---|---|---|
| 0001 | Add event-driven debugger: m6809 hooks + PinmameDebug* API + CMakeLists | the `PinmameDebug*` API + m6809 dispatch-loop / RM/WM hooks; `CMakeLists_libpinmame.txt` for `cmake -S .` builds |
| 0002 | Event-driven switch recorder at `vp_putSwitch` | the `VPINMAME_SWITCHLOG` recorder (the replayable switch-edge stream) |
| 0003 | Cross-thread emulation-clock + fence-reached query | `PinmameGetEmulationTime` / `PinmameTimeFenceReached` (closed-loop replay pacing) |

Touched files: `src/libpinmame/libpinmame.{cpp,h}`, `src/cpu/m6809/m6809.c`,
`src/cpuexec.c`, `src/wpc/vpintf.{c,h}`, `src/win32com/Alias.cpp`,
`CMakeLists_libpinmame.txt`.

**Not included:** the `VPINMAME_RECORD`/`VPINMAME_PLAYBACK` `.inp` recorder
that was in the original working tree. That code only recorded the MAME
input-port plane, which VP sessions never write to, making the `.inp` files
unplayable. The switch-log recorder (patch 0002) is the correct approach.
The removed code also relied on `strcpy_s` (MSVC-only), causing compile
failures on macOS/clang.

## Rebuild — macOS (arm64)

```bash
cd /path/to/pinmame
git checkout 3ef424b0a560b08b563a345d1ecd0fa733533eef -b switch-recorder
git am <repo>/.claude/skills/record-pinball/pinmame-patches/*.patch

# cmake -S . requires a CMakeLists.txt at the source root.
# CMakeLists_libpinmame.txt (added by patch 0001) is the correct entry point.
cp CMakeLists_libpinmame.txt CMakeLists.txt

cmake -S . -B build_macos -DPLATFORM=macos -DARCH=arm64 \
      -DBUILD_SHARED=ON -DBUILD_STATIC=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build_macos --target pinmame_shared -j$(sysctl -n hw.logicalcpu)

# Copy result into skill bin/ (the canonical pre-built for this arch)
cp build_macos/libpinmame.3.*.dylib <repo>/.claude/skills/record-pinball/bin/libpinmame.dylib
```

`setup-pinball.sh` does all of the above automatically (including the CMakeLists.txt copy/remove) when no pre-built dylib is in `bin/` for the current arch.

## Rebuild — Windows (x64, MSVC)

```powershell
$PinmameSrc = 'C:\path\to\your\pinmame'
& 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\amd64\MSBuild.exe' `
    "$PinmameSrc\build\libpinmame\pinmame_shared.vcxproj" `
    /p:Configuration=Release /p:Platform=x64 /m /nologo

Copy-Item "$PinmameSrc\build\libpinmame\Release\pinmame64.dll" `
          <repo>\.claude\skills\record-pinball\bin\pinmame64.dll -Force
```

## Status / cleanup

These are a raw export of a local-only branch (the branch was never pushed).
Expect to tidy before publishing: squash/relabel commits, re-confirm they
apply cleanly on the pinned base. Once published as a proper fork, the skills
can point at that fork URL instead of these vendored patches.
