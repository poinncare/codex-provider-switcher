#!/bin/sh
set -eu

service_name=$(printenv CODEX_PROVIDER_SWITCHER_OPENROUTER_SERVICE 2>/dev/null || true)
[ -n "$service_name" ] || service_name="codex-provider-switcher.openrouter"
account_name=$(/usr/bin/id -un)

if token=$(/usr/bin/security find-generic-password -s "$service_name" -a "$account_name" -w 2>/dev/null); then
    printf '%s\n' "$token"
    exit 0
fi

# Backward-compatible lookup for the original local installation.
if token=$(/usr/bin/security find-generic-password -s "com.openai.codex.openrouter" -a "$account_name" -w 2>/dev/null); then
    printf '%s\n' "$token"
    exit 0
fi

echo "OpenRouter API key is not configured in macOS Keychain." >&2
exit 1
