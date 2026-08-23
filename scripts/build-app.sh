#!/bin/sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app_path="$root_dir/dist/Codex.app"
template_path="$root_dir/src/Codex Provider Switcher.applescript.template"
helper_path="$root_dir/src/codex_provider_switcher.py"
tmp_dir=$(printenv TMPDIR 2>/dev/null || printf '/tmp')
rendered_script="$(mktemp "$tmp_dir/codex-provider-switcher.XXXXXX.applescript")"
iconset_dir="$(mktemp -d "$tmp_dir/codex-iconset.XXXXXX")"
iconset_path="$iconset_dir.iconset"
mv "$iconset_dir" "$iconset_path"

cleanup() {
    rm -f "$rendered_script"
    rm -rf "$iconset_path"
}
trap cleanup EXIT INT TERM

mkdir -p "$root_dir/dist"
rm -rf "$app_path"

helper_literal=$(printf '%s' "$helper_path" | sed 's/[\\&|]/\\&/g')
sed "s|__HELPER_PATH__|$helper_literal|g" "$template_path" > "$rendered_script"
/usr/bin/osacompile -o "$app_path" "$rendered_script" >/dev/null

# Reuse the rounded Codex artwork shipped with the official ChatGPT desktop
# app and package all macOS icon sizes so the launcher stays sharp everywhere.
codex_icon_source="/Applications/ChatGPT.app/Contents/Resources/icon-codex-dark-color.png"
if [ -f "$codex_icon_source" ]; then
    /usr/bin/sips -z 16 16 "$codex_icon_source" --out "$iconset_path/icon_16x16.png" >/dev/null
    /usr/bin/sips -z 32 32 "$codex_icon_source" --out "$iconset_path/icon_16x16@2x.png" >/dev/null
    /usr/bin/sips -z 32 32 "$codex_icon_source" --out "$iconset_path/icon_32x32.png" >/dev/null
    /usr/bin/sips -z 64 64 "$codex_icon_source" --out "$iconset_path/icon_32x32@2x.png" >/dev/null
    /usr/bin/sips -z 128 128 "$codex_icon_source" --out "$iconset_path/icon_128x128.png" >/dev/null
    /usr/bin/sips -z 256 256 "$codex_icon_source" --out "$iconset_path/icon_128x128@2x.png" >/dev/null
    /usr/bin/sips -z 256 256 "$codex_icon_source" --out "$iconset_path/icon_256x256.png" >/dev/null
    /usr/bin/sips -z 512 512 "$codex_icon_source" --out "$iconset_path/icon_256x256@2x.png" >/dev/null
    /usr/bin/sips -z 512 512 "$codex_icon_source" --out "$iconset_path/icon_512x512.png" >/dev/null
    /usr/bin/sips -z 1024 1024 "$codex_icon_source" --out "$iconset_path/icon_512x512@2x.png" >/dev/null
    /usr/bin/iconutil --convert icns --output "$app_path/Contents/Resources/applet.icns" "$iconset_path" >/dev/null
fi

chmod +x "$helper_path"
/usr/bin/codesign --force --deep --sign - "$app_path" >/dev/null 2>&1 || true

printf '%s\n' "$app_path"
