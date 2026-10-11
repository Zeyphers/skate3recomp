#!/bin/zsh
# Builds Skate 3 Recomp with the music mod for Apple Silicon Macs and packages
# dist/skate3recomp.app.
#
#   packaging/build-macos.sh /path/to/game            build dist/skate3recomp.app
#   packaging/build-macos.sh /path/to/game --install  ...and put it next to the game folder
#
# The game folder is the "game" folder the official release created when you installed from
# your ISO; start the game once first so it applies the title update (adds default.xexp).
# --install keeps the app it replaces in backup/skate3recomp.app.orig (only the first time).
#
# One-time setup:  brew install llvm cmake ninja molten-vk ffmpeg
set -euo pipefail

REPO="${0:A:h:h}"
GAME="${1:-}"
[[ -n "$GAME" && -f "$GAME/default.xex" ]] || {
  echo "usage: $0 /path/to/game [--install]   (the folder holding default.xex)" >&2
  exit 1
}
GAME="${GAME:A}"
INSTALL="${2:-}"

for tool in cmake ninja /opt/homebrew/opt/llvm/bin/clang; do
  command -v "$tool" >/dev/null || { echo "missing $tool - run: brew install llvm cmake ninja molten-vk" >&2; exit 1; }
done
if [[ ! -f "$GAME/default.xexp" || ! -f "$GAME/data/webkit/EAWebkit.xexp" ]]; then
  echo "the title update isn't in $GAME yet - start the game once with the official release first" >&2
  exit 1
fi

cd "$REPO"
[[ -f third_party/rexglue-sdk/thirdparty/imgui/imgui.h ]] || git submodule update --init --recursive --jobs 8

ARGS=(-DSKATE3_GAME_DATA_ROOT="$GAME" -DSKATE3_TITLE_UPDATE_STAGED_ROOT="$GAME")
cmake --preset macos-release "${ARGS[@]}"
if [[ ! -f generated/sources.cmake ]]; then
  cmake --build --preset macos-release --target generate-all --parallel
  cmake --preset macos-release "${ARGS[@]}"
fi
cmake --build --preset macos-release --parallel

BUILD="$REPO/out/build/macos-release"
APP="$REPO/dist/skate3recomp.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp -f "$BUILD/skate3" "$BUILD/librexruntime.dylib" "$BUILD/libMoltenVK.dylib" "$APP/Contents/MacOS/"
chmod u+w "$APP/Contents/MacOS/"*  # Homebrew's MoltenVK is read-only
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>skate3recomp</string>
  <key>CFBundleDisplayName</key><string>skate3recomp</string>
  <key>CFBundleIdentifier</key><string>io.github.mchughalex.skate3recomp</string>
  <key>CFBundleVersion</key><string>2.0.2</string>
  <key>CFBundleShortVersionString</key><string>2.0.2</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>skate3</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.games</string>
  <key>GCSupportsGameMode</key><true/>
</dict>
</plist>
EOF
xattr -cr "$APP"
codesign --force --deep --sign - "$APP"
echo "built $APP"

if [[ "$INSTALL" == "--install" ]]; then
  pgrep -x skate3 >/dev/null && { echo "Skate 3 is running - close it first." >&2; exit 1; }
  ROOT="${GAME:h}"
  if [[ -d "$ROOT/skate3recomp.app" && ! -d "$ROOT/backup/skate3recomp.app.orig" ]]; then
    mkdir -p "$ROOT/backup"
    cp -R "$ROOT/skate3recomp.app" "$ROOT/backup/skate3recomp.app.orig"
  fi
  # Keep the MoltenVK the game already runs with (the release ships its own build of it).
  OLD_MVK="$ROOT/skate3recomp.app/Contents/MacOS/libMoltenVK.dylib"
  [[ -f "$OLD_MVK" ]] || OLD_MVK="$ROOT/backup/skate3recomp.app.orig/Contents/MacOS/libMoltenVK.dylib"
  if [[ -f "$OLD_MVK" ]]; then
    cp -f "$OLD_MVK" "$APP/Contents/MacOS/libMoltenVK.dylib"
    chmod u+w "$APP/Contents/MacOS/libMoltenVK.dylib"
    xattr -cr "$APP"
    codesign --force --deep --sign - "$APP"
  fi
  rm -rf "$ROOT/skate3recomp.app"
  cp -R "$APP" "$ROOT/skate3recomp.app"
  echo "installed $ROOT/skate3recomp.app (the previous one is in backup/skate3recomp.app.orig)"
fi
