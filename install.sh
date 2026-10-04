#!/bin/sh
# Portable source installation. Read-only unless --apply is explicitly supplied.
set -eu
PLUGIN_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 "$PLUGIN_ROOT/src/install.py" --root "$PLUGIN_ROOT" "$@"
