#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Remove what install.sh put under $PREFIX (default ~/.local). Projects, sign-ins and settings stay.
set -eu
PREFIX="${PREFIX:-$HOME/.local}"
ID=org.projectluma.Playground
rm -rf "$PREFIX/lib/playground" "$PREFIX/bin/playground" "$PREFIX/share/applications/$ID.desktop" \
       "$PREFIX/share/icons/hicolor/scalable/apps/$ID.svg"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$PREFIX/share/applications" || true
echo "Playground is removed. Your projects in ~/Documents/Playground Projects are untouched."
