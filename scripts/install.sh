#!/bin/sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app_path="$root_dir/dist/Codex.app"
install_dir="$HOME/Applications"
install_path="$install_dir/Codex.app"

"$root_dir/scripts/build-app.sh" >/dev/null
mkdir -p "$install_dir"

if [ -e "$install_path" ]; then
    backup_path="$HOME/.Trash/Codex.previous.$(date +%Y%m%d-%H%M%S).app"
    mkdir -p "$HOME/.Trash"
    mv "$install_path" "$backup_path"
fi

/usr/bin/ditto "$app_path" "$install_path"
echo "Installed: $install_path"
