#!/bin/sh
# Manual macOS shared launch; requires an existing Python 3 installation.
set -eu
launcher_root=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '需要可用的 Python 3；此入口不会安装解释器。' >&2
  exit 2
fi
if [ "$(uname -s)" != Darwin ]; then
  printf '%s\n' '此共享启动入口需要 macOS。' >&2
  exit 2
fi
if [ "$#" -eq 0 ]; then
  set -- --activate
fi
exec python3 -B "$launcher_root/src/launcher.py" "$@"
