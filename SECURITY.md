# Security

Do not report or commit API keys, OAuth tokens, auth.json, screenshots with account data, or private Orca/Codex state.

OpenRouter credentials are intentionally read from macOS Keychain. The project never writes the key to its repository or to a provider configuration file.

If a secret was exposed, revoke it at the provider immediately and remove it from Git history before publishing.
