#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Stage Playground for a Linux system without Luma's platform, into DESTDIR:
#   /usr/lib/playground/app      Playground itself
#   /usr/lib/playground/lumaui   a private build of LumaUI (libraries, typelibs, the Python kit, style sheets)
#   /usr/bin/playground          the launcher, which puts that LumaUI first on the library and module paths
#   /usr/share/...               desktop entry, icon, AppStream data
# Nothing outside /usr/lib/playground is shared with other programs, so Luma's own platform can be installed
# beside it without a clash.
#
# usage: stage.sh PLAYGROUND_SOURCE LUMA_PLATFORM_SOURCE DESTDIR
# LUMA_PLATFORM_SOURCE is src/luma-platform of a Luma checkout. From the same checkout come what Luma's own
# packages provide on Luma: LumaUI's glyphs (assets/icon-theme/Prairie) and Figtree, the interface face
# (assets/fonts/figtree, with the hyphen mapping Luma's font package applies; needs fontTools).
set -eu
APP_SRC="$1"; KIT_SRC="$2"; DEST="$3"
LIB=/usr/lib/playground
BUILD="${BUILD_DIR:-$(mktemp -d)}"
ID=org.projectluma.Playground

# LumaUI: the three libraries and their typelibs, built against this system's GTK and libadwaita.
meson setup "$BUILD" "$KIT_SRC" -Dtests=false --prefix="$LIB/lumaui" --libdir=lib --buildtype=release
ninja -C "$BUILD"
DESTDIR="$DEST" ninja -C "$BUILD" install
rm -rf "$DEST$LIB/lumaui/share" "$DEST$LIB/lumaui/include" "$DEST$LIB/lumaui/lib/pkgconfig" \
       "$DEST$LIB/lumaui/lib/libluma-app-prepare.so" "$DEST$LIB/lumaui/bin"
find "$DEST$LIB/lumaui/lib" -maxdepth 1 -name '*.so' -type l -delete
# The Python kit finds its style sheets beside its package, as in the source tree.
mkdir -p "$DEST$LIB/lumaui/appkit" "$DEST$LIB/lumaui/ui"
cp -R "$KIT_SRC/appkit/luma_appkit" "$KIT_SRC/appkit/fonts" "$DEST$LIB/lumaui/appkit/"
cp "$KIT_SRC"/appkit/*.css "$DEST$LIB/lumaui/appkit/"
cp "$KIT_SRC/ui/luma-ui.css" "$DEST$LIB/lumaui/ui/"
# LumaUI's glyphs (window controls, chevrons, every lumaui-* icon), where the kit adds them to GTK's icon theme.
mkdir -p "$DEST$LIB/lumaui/appkit/luma_appkit/icons"
PRAIRIE="$KIT_SRC/../../assets/icon-theme/Prairie"
cp "$PRAIRIE/symbolic/actions"/lumaui-*.svg "$DEST$LIB/lumaui/appkit/luma_appkit/icons/"
cp "$PRAIRIE/upstream/lucide/lumaui/LICENSE.txt" "$DEST$LIB/lumaui/appkit/luma_appkit/icons/LICENSE.txt"
# Figtree, registered for Playground's process by luma_appkit.platform_boot (see the launcher).
LUMA_SRC="$KIT_SRC/../.."
mkdir -p "$DEST$LIB/lumaui/fonts/figtree"
python3 "$LUMA_SRC/scripts/fonts/adjust-figtree-ui-metrics.py" --hyphens-only \
    "$LUMA_SRC/assets/fonts/figtree/Figtree.ttf" "$DEST$LIB/lumaui/fonts/figtree/Figtree.ttf"
cp "$LUMA_SRC/assets/fonts/figtree/OFL.txt" "$DEST$LIB/lumaui/fonts/figtree/OFL.txt"
cp "$APP_SRC/LICENSE" "$DEST$LIB/lumaui/LICENSE"          # LumaUI is Apache-2.0, as Playground is

# Playground.
mkdir -p "$DEST$LIB/app"
cp -R "$APP_SRC/luma_playground" "$APP_SRC/bin" "$APP_SRC/data" "$APP_SRC/luma-app.toml" "$DEST$LIB/app/"
find "$DEST$LIB" -name __pycache__ -prune -exec rm -rf {} +
python3 -m compileall -q "$DEST$LIB/app/luma_playground" "$DEST$LIB/lumaui/appkit/luma_appkit" || true
chmod 0755 "$DEST$LIB/app/bin/playground-mcp"

install -Dm 0755 "$APP_SRC/packaging/linux/playground.sh" "$DEST/usr/bin/playground"
mkdir -p "$DEST/usr/share/applications"
sed -e "s|^Exec=.*|Exec=/usr/bin/playground %U|" -e "/^DBusActivatable=/d" \
    "$APP_SRC/data/$ID.desktop" > "$DEST/usr/share/applications/$ID.desktop"
install -Dm 0644 "$APP_SRC/data/$ID.svg" "$DEST/usr/share/icons/hicolor/scalable/apps/$ID.svg"
install -Dm 0644 "$APP_SRC/data/$ID.metainfo.xml" "$DEST/usr/share/metainfo/$ID.metainfo.xml"
