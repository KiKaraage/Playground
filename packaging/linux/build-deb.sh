#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Build Playground's .deb for Debian 13 / Ubuntu 25.04 and newer (GTK 4.18+, libadwaita 1.7+), with its own
# build of LumaUI. Run on that system, as root, from the Playground source tree:
#   packaging/linux/build-deb.sh LUMA_PLATFORM_SOURCE OUTPUT_DIR   (src/luma-platform of a Luma checkout)
set -eu
KIT_SRC="$1"; OUT="$2"
VERSION="${VERSION:-0.1.0~alpha2-1}"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential meson ninja-build pkg-config libglib2.0-dev gobject-introspection \
    libgirepository-2.0-dev libgtk-4-dev libadwaita-1-dev libwayland-dev python3 python3-fonttools >/dev/null
ROOT="$(mktemp -d)"
sh "$SRC/packaging/linux/stage.sh" "$SRC" "$KIT_SRC" "$ROOT"
ARCH="$(dpkg --print-architecture)"
mkdir -p "$ROOT/DEBIAN" "$ROOT/usr/share/doc/playground"
L=/usr/lib/playground
{ cat "$SRC/LICENSE"
  printf '\n\nIcons in %s/app/data/icons: Lucide, ISC\n\n' "$L"; cat "$SRC/data/icons/LUCIDE-LICENSE"
  printf '\n\nLumaUI glyphs in %s/lumaui/appkit/luma_appkit/icons: Lucide, ISC and MIT\n\n' "$L"
  cat "$ROOT$L/lumaui/appkit/luma_appkit/icons/LICENSE.txt"
  printf '\n\nFigtree in %s/lumaui/fonts/figtree: OFL-1.1\n\n' "$L"; cat "$ROOT$L/lumaui/fonts/figtree/OFL.txt"
} > "$ROOT/usr/share/doc/playground/copyright"
cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: playground
Version: $VERSION
Architecture: $ARCH
Maintainer: Project Luma <maintainers@projectluma.org>
Section: devel
Priority: optional
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, libgtk-4-1 (>= 4.18), libadwaita-1-0 (>= 1.7), gir1.2-webkit-6.0, gir1.2-gtksource-5, gir1.2-vte-3.91, adwaita-icon-theme, python3-cryptography, git, openssh-client, bubblewrap
Recommends: libgtk-4-bin, podman, nodejs
Homepage: https://github.com/SimplyLuma/Playground
Description: Make an app by describing it, with agents you choose
 Playground is an agentic development environment. Say what you want to make;
 it creates a real project folder, connects an agent of your choice, and shows
 the running app in a live preview at phone, tablet and desktop sizes. It
 carries its own copy of LumaUI, Luma's toolkit.
CONTROL
mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$ROOT" "$OUT/playground_${VERSION}_${ARCH}.deb"
