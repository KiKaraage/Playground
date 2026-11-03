#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Install Playground for the current user, from this source tree.
#
# Everything goes under $PREFIX (default ~/.local): the app in lib/playground, the launcher in
# bin/playground, the desktop entry and icon in share/. No root, no container, no Flatpak sandbox:
# Playground runs agents, a project's own box (podman) and GTK previews against this system, so it
# runs as an ordinary program of the user. Run it again to update. ./uninstall.sh removes it.
set -eu

PREFIX="${PREFIX:-$HOME/.local}"
APP="$PREFIX/lib/playground"
SRC="$(cd "$(dirname "$0")" && pwd)"
ID=org.projectluma.Playground

missing=""
python3 - <<'PY' 2>/dev/null || missing=yes
import gi
for name, version in (("Gtk", "4.0"), ("Adw", "1"), ("WebKit", "6.0")):
    gi.require_version(name, version)
from gi.repository import Gtk, Adw, WebKit  # noqa: F401
import luma_appkit  # noqa: F401
import cryptography  # noqa: F401
PY
if [ -n "$missing" ]; then
    cat >&2 <<'MSG'
Playground needs Luma's platform (LumaUI), GTK 4, libadwaita, WebKitGTK 6 and python3-cryptography
(GtkSourceView 5 and VTE for the code view and terminal). Luma includes them. On another Fedora-based system, install
luma-developer-platform from Luma's repository first. Installing anyway.
MSG
fi
command -v gtk4-broadwayd >/dev/null 2>&1 || echo "Note: gtk4-broadwayd isn't here, so desktop apps preview in their own window." >&2

rm -rf "$APP.new"
mkdir -p "$APP.new"
cp -R "$SRC/luma_playground" "$SRC/bin" "$SRC/data" "$SRC/luma-app.toml" "$APP.new/"
find "$APP.new" -name __pycache__ -prune -exec rm -rf {} +
chmod 0755 "$APP.new/bin/playground-mcp"
rm -rf "$APP"
mv "$APP.new" "$APP"

mkdir -p "$PREFIX/bin"
cat > "$PREFIX/bin/playground" <<SH
#!/bin/sh
# Playground, installed for this user by install.sh.
export PYTHONPATH="$APP\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m luma_playground "\$@"
SH
chmod 0755 "$PREFIX/bin/playground"

mkdir -p "$PREFIX/share/applications" "$PREFIX/share/icons/hicolor/scalable/apps"
sed -e "s|^Exec=.*|Exec=$PREFIX/bin/playground %U|" -e "/^DBusActivatable=/d" \
    "$SRC/data/$ID.desktop" > "$PREFIX/share/applications/$ID.desktop"
cp "$SRC/data/$ID.svg" "$PREFIX/share/icons/hicolor/scalable/apps/$ID.svg"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$PREFIX/share/applications" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -f -t "$PREFIX/share/icons/hicolor" || true

echo "Playground is installed. Open it from your apps, or run: $PREFIX/bin/playground"
