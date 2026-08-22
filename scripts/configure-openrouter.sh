#!/bin/sh
set -eu

service_name=$(printenv CODEX_PROVIDER_SWITCHER_OPENROUTER_SERVICE 2>/dev/null || true)
[ -n "$service_name" ] || service_name="codex-provider-switcher.openrouter"
account_name=$(/usr/bin/id -un)

printf 'OpenRouter API key (input is hidden): '
stty -echo
IFS= read -r token
stty echo
printf '\n'

if [ -z "$token" ]; then
    echo "No key supplied." >&2
    exit 1
fi

/usr/bin/security add-generic-password \
    -U \
    -s "$service_name" \
    -a "$account_name" \
    -w "$token"

unset token
echo "OpenRouter key saved in macOS Keychain."
