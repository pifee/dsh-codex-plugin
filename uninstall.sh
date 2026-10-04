#!/bin/sh
# Remove only this package's registrations; read-only unless --apply is supplied.
set -eu
PLUGIN_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 "$PLUGIN_ROOT/src/uninstall.py" --root "$PLUGIN_ROOT" "$@"
