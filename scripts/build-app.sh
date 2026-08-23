#!/bin/sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app_path="$root_dir/dist/Codex.app"
template_path="$root_dir/src/Codex Provider Switcher.applescript.template"
helper_path="$root_dir/src/codex_provider_switcher.py"
tmp_dir=$(printenv TMPDIR 2>/dev/null || printf '/tmp')
rendered_script="$(mktemp "$tmp_dir/codex-provider-switcher.XXXXXX.applescript")"

cleanup() {
    rm -f "$rendered_script"
}
trap cleanup EXIT INT TERM

mkdir -p "$root_dir/dist"
rm -rf "$app_path"

helper_literal=$(printf '%s' "$helper_path" | sed 's/[\\&|]/\\&/g')
sed "s|__HELPER_PATH__|$helper_literal|g" "$template_path" > "$rendered_script"
/usr/bin/osacompile -o "$app_path" "$rendered_script" >/dev/null

# Reuse the Codex icon shipped with the official ChatGPT desktop app when it
# is installed. Independent installations keep the AppleScript default icon.
codex_icon_source="/Applications/ChatGPT.app/Contents/Resources/app.icns"
if [ -f "$codex_icon_source" ]; then
    cp "$codex_icon_source" "$app_path/Contents/Resources/applet.icns"
fi

chmod +x "$helper_path"
/usr/bin/codesign --force --deep --sign - "$app_path" >/dev/null 2>&1 || true

printf '%s\n' "$app_path"
