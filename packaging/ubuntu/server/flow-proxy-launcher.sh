#!/bin/bash
# Installed as bin/google-chrome on Flow hosts with explicitly assigned proxies.
set -eu

CHROME_WRAPPER=$(readlink -f "$0")
HERE=$(dirname "$CHROME_WRAPPER")
proxy=$(/usr/bin/python3 -B "$HERE/../console/flow_proxy_guard.py" "$@")

export CHROME_WRAPPER
export CHROME_VERSION_EXTRA=stable
export GNOME_DISABLE_CRASH_DIALOG=SET_BY_GOOGLE_CHROME
xdg_app_dir="${XDG_DATA_HOME:-$HOME/.local/share/applications}"
mkdir -p "$xdg_app_dir"
[ -f "$xdg_app_dir/mimeapps.list" ] || touch "$xdg_app_dir/mimeapps.list"

exec -a "$0" "$HERE/chrome" "$proxy" "$@" </dev/null
