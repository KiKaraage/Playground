#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Playground on a system without Luma's platform: its own copy of LumaUI goes first on the library, typelib
# and module paths (apps it previews inherit them too). LumaUI's off-Luma launcher registers its Figtree for
# this process and makes it the interface face, as Luma's settings do.
LIB=/usr/lib/playground
export LD_LIBRARY_PATH="$LIB/lumaui/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="$LIB/lumaui/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
export PYTHONPATH="$LIB/app:$LIB/lumaui/appkit${PYTHONPATH:+:$PYTHONPATH}"
export LUMA_FONT_DIRS="$LIB/lumaui/fonts${LUMA_FONT_DIRS:+:$LUMA_FONT_DIRS}"
export LUMA_FONT_NAME="${LUMA_FONT_NAME:-Figtree 11}"
exec python3 -m luma_appkit.platform_boot luma_playground "$@"
