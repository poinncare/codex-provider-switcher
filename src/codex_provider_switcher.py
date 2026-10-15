#!/usr/bin/env python3
"""GUI helper backend for switching Codex providers, models, and accounts."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
NATIVE_CODEX_HOME = HOME / ".codex"
NATIVE_AUTH_PATH = NATIVE_CODEX_HOME / "auth.json"
NATIVE_CONFIG_PATH = NATIVE_CODEX_HOME / "config.toml"
ORCA_USER_DATA = Path(
    os.environ.get("ORCA_USER_DATA_PATH", HOME / "Library/Application Support/orca")
).expanduser()
ACCOUNTS_ROOT = ORCA_USER_DATA / "codex-accounts"
ORCA_DATA_PATHS = [
    ORCA_USER_DATA / "orca-data.json",
    ORCA_USER_DATA / "profiles/local-default/orca-data.json",
]
OPENROUTER_TOKEN_HELPER = PROJECT_ROOT / "scripts/openrouter-token.sh"
SWITCHER_STATE_PATH = Path(
    os.environ.get("CODEX_PROVIDER_SWITCHER_STATE", HOME / ".codex/codex-provider-switcher-state.json")
).expanduser()
OPENROUTER_PROVIDER_TABLE = """
[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"
request_max_retries = 3
stream_max_retries = 3

[model_providers.openrouter.auth]
command = {token_helper}
timeout_ms = 5000
refresh_interval_ms = 0
"""

OPENCODE_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")
# Providers that must never appear as separate launcher entries.  Copilot is
# not used by this project, so a provider that identifies itself as Copilot is
# skipped during discovery.  Copilot models exposed by another provider (for
# example through the shared OpenCode gateway) are left untouched.
EXCLUDED_OPENCODE_PROVIDER_MARKER = "copilot"
# Provider ids owned by the launcher.  These are the only sections the launcher
# creates, refreshes and removes in Codex config.toml.
MANAGED_PROVIDER_KEYS = ("openrouter",)
MANAGED_PROVIDER_PREFIX = "opencode-"
# Comment written above the launcher-managed provider section.
EXTERNAL_PROVIDERS_MARKER = "# External model providers"


def run_json(command: list[str]) -> dict:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    return payload.get("result", payload)


def orca_command() -> str | None:
    candidates = [
        os.environ.get("ORCA_CLI_COMMAND"),
        shutil.which("orca"),
        "/Applications/Orca.app/Contents/Resources/bin/orca",
        "/usr/local/bin/orca",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def codex_command() -> str | None:
    candidates = [
        os.environ.get("CODEX_CLI_COMMAND"),
        shutil.which("codex"),
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def jsonc_paths() -> list[Path]:
    paths = []
    configured = os.environ.get("OPENCODE_CONFIG")
    config_dir = os.environ.get("OPENCODE_CONFIG_DIR")
    if configured:
        paths.append(Path(configured).expanduser())
    if config_dir:
        paths.extend(Path(config_dir).expanduser() / name for name in OPENCODE_CONFIG_NAMES)
    paths.extend(
        [
            HOME / ".config/opencode/opencode.json",
            HOME / ".config/opencode/opencode.jsonc",
            HOME / "Library/Application Support/opencode/opencode.json",
            HOME / "Library/Application Support/opencode/opencode.jsonc",
        ]
    )
    return list(dict.fromkeys(paths))


def load_jsonc(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(^|\s)//.*$", r"\1", text, flags=re.M)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def opencode_provider_auth(provider: dict) -> str | None:
    options = provider.get("options") or {}
    api_key = options.get("apiKey") or options.get("api_key")
    if isinstance(api_key, str) and api_key and not api_key.startswith("$"):
        return api_key
    env = options.get("env", options.get("envs"))
    if isinstance(env, dict):
        for key in ("API_KEY", "OPENAI_API_KEY", "api_key", "apiKey"):
            if key in env:
                env = env[key]
                break
        else:
            env = None
    if isinstance(env, str):
        if env.startswith("$"):
            env = env[1:]
        return os.environ.get(env)
    return None


def is_excluded_provider(provider_id: str, provider_name: str) -> bool:
    haystack = f"{provider_id} {provider_name}".lower()
    return EXCLUDED_OPENCODE_PROVIDER_MARKER in haystack


def is_managed_provider_key(key: str) -> bool:
    """True when the launcher owns this [model_providers.*] section."""
    return key in MANAGED_PROVIDER_KEYS or key.startswith(MANAGED_PROVIDER_PREFIX)


def opencode_provider_configs() -> list[dict]:
    for path in jsonc_paths():
        if not path.is_file():
            continue
        try:
            data = load_jsonc(path)
        except (OSError, json.JSONDecodeError):
            continue
        providers = data.get("provider") or data.get("providers") or {}
        if not isinstance(providers, dict):
            continue
        discovered = []
        for provider_id, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            options = provider.get("options") or {}
            base_url = options.get("baseURL") or options.get("base_url") or provider.get("baseURL")
            if not base_url:
                continue
            if is_excluded_provider(str(provider_id), str(provider.get("name") or provider_id)):
                continue
            models = provider.get("models") or {}
            model_rows = []
            if isinstance(models, dict):
                for model_id, model in models.items():
                    display_name = model.get("name") if isinstance(model, dict) else None
                    model_rows.append((str(model_id), str(display_name or model_id)))
            discovered.append(
                {
                    "id": str(provider_id),
                    "key": MANAGED_PROVIDER_PREFIX
                    + re.sub(r"[^A-Za-z0-9_-]", "-", str(provider_id)),
                    "name": str(provider.get("name") or provider_id),
                    "base_url": str(base_url),
                    "models": model_rows,
                    "api_key": opencode_provider_auth(provider),
                }
            )
        return discovered
    return []


def opencode_provider(provider_key: str) -> dict | None:
    return next((item for item in opencode_provider_configs() if item["key"] == provider_key), None)


def account_from_auth(auth_path: Path) -> dict:
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = data.get("tokens", {})
        if not isinstance(tokens, dict):
            return {}
        account_id = tokens.get("account_id")
        access_token = tokens.get("access_token", "")
        if not isinstance(access_token, str):
            return {}
        payload_part = access_token.split(".")[1]
        payload_part += "=" * (-len(payload_part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_part))
        profile = payload.get("https://api.openai.com/profile", {})
        if not isinstance(profile, dict):
            profile = {}
        email = profile.get("email") or payload.get("email")
        if account_id and email:
            return {"providerAccountId": account_id, "email": email, "authKind": "oauth"}
    except (OSError, IndexError, KeyError, ValueError, json.JSONDecodeError):
        pass
    return {}


def local_system_account() -> dict:
    return account_from_auth(NATIVE_AUTH_PATH)


def auth_account_id(path: Path = NATIVE_AUTH_PATH) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        account_id = data.get("tokens", {}).get("account_id")
        return str(account_id) if account_id else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def managed_account_for_auth_id(auth_id: str | None) -> dict | None:
    if not auth_id:
        return None
    return next(
        (
            account
            for account in managed_accounts()
            if auth_id in {account.get("id"), account.get("providerAccountId")}
        ),
        None,
    )


def backup_and_copy(source: Path, target: Path) -> Path | None:
    backup = None
    if target.is_file():
        backup = target.with_name(
            target.name + "." + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak"
        )
        shutil.copy2(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(0o600)
    return backup


def switch_native_auth(account_id: str) -> None:
    selected = next(
        (account for account in managed_accounts() if account.get("id") == account_id),
        None,
    )
    if not selected:
        raise RuntimeError("Этот аккаунт ещё не зарегистрирован в Orca.")

    target_home = Path(selected.get("managedHomePath") or ACCOUNTS_ROOT / account_id / "home")
    target_auth = target_home / "auth.json"
    if not target_auth.is_file():
        raise RuntimeError("Для выбранного аккаунта не найдена сохранённая Codex-сессия.")

    current = managed_account_for_auth_id(auth_account_id())
    if current and current.get("id") == account_id:
        # Codex refreshes OAuth tokens in the native auth file while it is
        # running.  Re-copying the managed snapshot for the already-active
        # account would roll those tokens back and can force a new login.
        return

    if current:
        current_home = Path(
            current.get("managedHomePath")
            or ACCOUNTS_ROOT / str(current.get("id")) / "home"
        )
        current_auth = current_home / "auth.json"
        backup_and_copy(NATIVE_AUTH_PATH, current_auth)

    backup_and_copy(target_auth, NATIVE_AUTH_PATH)


def orca_accounts() -> dict:
    command = orca_command()
    if not command:
        return {
            "accounts": [],
            "activeAccountId": None,
            "systemDefault": local_system_account(),
        }
    try:
        return run_json([command, "account", "list", "--json"]).get("codex", {})
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {
            "accounts": stored_managed_accounts(),
            "activeAccountId": stored_active_account_id(),
            "systemDefault": local_system_account(),
        }


def stored_managed_accounts() -> list[dict]:
    for path in ORCA_DATA_PATHS:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            accounts = data.get("settings", {}).get("codexManagedAccounts")
            if isinstance(accounts, list) and accounts:
                return accounts
        except (OSError, json.JSONDecodeError):
            pass
    return []


def managed_accounts() -> list[dict]:
    accounts = stored_managed_accounts()
    return accounts if accounts else list(orca_accounts().get("accounts", []))


def stored_active_account_id() -> str | None:
    for path in ORCA_DATA_PATHS:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            active = data.get("settings", {}).get("activeCodexManagedAccountId")
            if active:
                return str(active)
        except (OSError, json.JSONDecodeError):
            pass
    return None


def active_account_id() -> str | None:
    stored_active = stored_active_account_id()
    if stored_active:
        return stored_active
    try:
        return orca_accounts().get("activeAccountId")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def config_for_account(account_id: str | None = None) -> Path:
    account_id = account_id or active_account_id()
    if account_id:
        path = ACCOUNTS_ROOT / account_id / "home/config.toml"
        if path.is_file():
            return path

    configured_home = os.environ.get("CODEX_HOME")
    if configured_home and (Path(configured_home) / "config.toml").is_file():
        return Path(configured_home) / "config.toml"

    fallback = HOME / ".codex/config.toml"
    if fallback.is_file():
        return fallback
    raise RuntimeError("Не найден config.toml Codex.")


def set_root_key(text: str, key: str, value: str) -> str:
    first_table = re.search(r"(?m)^\s*\[", text)
    split_at = first_table.start() if first_table else len(text)
    root, tables = text[:split_at], text[split_at:]
    line = f'{key} = {json.dumps(value, ensure_ascii=False)}'
    pattern = re.compile(rf"(?m)^\s*{re.escape(key)}\s*=.*$")
    if pattern.search(root):
        root = pattern.sub(line, root, count=1)
    else:
        model_match = re.search(r"(?m)^\s*model\s*=.*$", root)
        if model_match:
            end = model_match.end()
            root = root[:end] + "\n" + line + root[end:]
        else:
            root = line + "\n" + root
    return root + tables


def root_value(text: str, key: str) -> str | None:
    first_table = re.search(r"(?m)^\s*\[", text)
    root = text[: first_table.start() if first_table else len(text)]
    match = re.search(rf'(?m)^\s*{re.escape(key)}\s*=\s*"([^"]*)"\s*$', root)
    return match.group(1) if match else None


def load_switcher_state() -> dict:
    try:
        data = json.loads(SWITCHER_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_switcher_state(data: dict) -> None:
    SWITCHER_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remember_model(provider: str | None, model: str | None) -> None:
    if not provider or not model:
        return
    state = load_switcher_state()
    state.setdefault("last_models", {})[provider] = model
    save_switcher_state(state)


def default_model_for(provider_key: str, current_config: str) -> str:
    provider = {"openrouter-free": "openrouter"}.get(provider_key, provider_key)
    current_provider = root_value(current_config, "model_provider")
    current_model = root_value(current_config, "model")
    if current_provider == provider and current_model:
        return current_model
    # The model is picked later inside the Codex interface, so the switcher
    # only needs a valid starting point.  Prefer whatever the provider
    # currently advertises instead of a hard-coded list that goes stale.
    available = models_for(provider_key)
    if available:
        return available[0][0]
    state_model = load_switcher_state().get("last_models", {}).get(provider)
    if state_model:
        return state_model
    return "gpt-5.6-luna"


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def provider_tables() -> str:
    blocks = [
        OPENROUTER_PROVIDER_TABLE.format(token_helper=toml_string(str(OPENROUTER_TOKEN_HELPER)))
    ]
    for provider in opencode_provider_configs():
        # Codex only supports the Responses wire API; the legacy "chat" value is
        # rejected outright.  OpenCode gateways speak the OpenAI-compatible
        # Responses API, so every generated provider uses it.
        lines = [
            f"[model_providers.{provider['key']}]",
            f"name = {toml_string(provider['name'])}",
            f"base_url = {toml_string(provider['base_url'])}",
            'wire_api = "responses"',
        ]
        api_key = provider.get("api_key")
        if api_key:
            # Codex's ModelProviderInfo denies unknown fields inside [.auth], so
            # a static bearer key belongs at provider level.
            lines.append(f"experimental_bearer_token = {toml_string(api_key)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def strip_managed_provider_tables(text: str) -> str:
    """Remove every launcher-owned [model_providers.*] section and its marker.

    Managed sections can go stale between releases (renamed providers, removed
    entries, changed wire_api).  Dropping them wholesale lets
    ensure_external_provider_tables rewrite one canonical set instead of
    patching whatever happens to be present.
    """
    kept = []
    skipping = False
    for line in text.splitlines():
        header = re.match(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?", line)
        if header:
            parts = header.group(1).strip().split(".")
            skipping = (
                len(parts) >= 2
                and parts[0] == "model_providers"
                and is_managed_provider_key(parts[1])
            )
        if skipping:
            continue
        if line.strip() == EXTERNAL_PROVIDERS_MARKER:
            continue
        kept.append(line)
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return result + "\n" if result else ""


def ensure_external_provider_tables(text: str) -> str:
    stripped = strip_managed_provider_tables(text).rstrip()
    tables = provider_tables()
    if not tables:
        return stripped + "\n" if stripped else ""
    prefix = stripped + "\n\n" if stripped else ""
    return f"{prefix}{EXTERNAL_PROVIDERS_MARKER}\n{tables}\n"


def backup_and_write(path: Path, text: str) -> Path:
    backup = path.with_name(path.name + "." + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    return backup


def stop_apps(apps: list[str]) -> None:
    commands = "\n".join(f'tell application "{app}" to quit' for app in apps)
    subprocess.run(["/usr/bin/osascript", "-e", commands], capture_output=True, text=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        alive = False
        for app in apps:
            if subprocess.run(["/usr/bin/pgrep", "-x", app], capture_output=True).returncode == 0:
                alive = True
                break
        if not alive:
            return
        time.sleep(0.25)
    still_running = [
        app for app in apps
        if subprocess.run(["/usr/bin/pgrep", "-x", app], capture_output=True).returncode == 0
    ]
    if still_running:
        raise RuntimeError("Не удалось корректно закрыть: " + ", ".join(still_running))


def desktop_codex_server_pids() -> list[int]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="],
        check=False,
        capture_output=True,
        text=True,
    )
    pids = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(.+)$", line)
        if match and "/Applications/ChatGPT.app/Contents/Resources/codex" in match.group(2):
            if re.search(r"\bapp-server\b", match.group(2)):
                pids.append(int(match.group(1)))
    return pids


def stop_desktop_codex_server() -> None:
    pids = desktop_codex_server_pids()
    for pid in pids:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline and desktop_codex_server_pids():
        time.sleep(0.25)
    for pid in desktop_codex_server_pids():
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def launch_environment(account_home: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ORCA_") or key in {
            "CODEX_HOME",
            "CODEX_THREAD_ID",
            "ORCA_PANE_KEY",
            "ORCA_TERMINAL_HANDLE",
            "OPENCODE_CONFIG",
            "OPENCODE_CONFIG_DIR",
            "TERM_PROGRAM",
            "TERM_PROGRAM_VERSION",
        }:
            env.pop(key, None)
    if account_home:
        env["CODEX_HOME"] = str(account_home)
        env["ORCA_CODEX_HOME"] = str(account_home)
        env["ORCA_USER_DATA_PATH"] = str(ORCA_USER_DATA)
    else:
        # Orca can inject a managed-home path into PATH.  A native ChatGPT
        # launch must not inherit that runtime context.
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
    return env


def open_apps(apps: list[str], account_home: Path | None = None) -> None:
    env = launch_environment(account_home)
    for app in apps:
        subprocess.run(["/usr/bin/open", "-a", app], check=True, env=env)
        time.sleep(1)


def opencode_gateway_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:20128/v1/models", timeout=2) as response:
            payload = json.load(response)
            return response.status == 200 and isinstance(payload.get("data"), list)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def require_opencode_gateway() -> None:
    if not opencode_gateway_ready():
        raise RuntimeError(
            "Не найден OpenAI-compatible OpenCode gateway на localhost:20128. "
            "Запустите ваш gateway из OpenCode; обычная команда `opencode serve` для Codex не подходит."
        )


def opencode_gateway_ready_for(base_url: str) -> bool:
    try:
        models_url = base_url.rstrip("/") + "/models"
        with urllib.request.urlopen(models_url, timeout=2) as response:
            payload = json.load(response)
            return response.status == 200 and isinstance(payload.get("data"), list)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def require_opencode_gateway_for(provider: dict) -> None:
    if not opencode_gateway_ready_for(provider["base_url"]):
        raise RuntimeError(
            f"Не найден OpenAI-compatible gateway OpenCode: {provider['base_url']}. "
            "Запустите совместимый gateway из OpenCode."
        )


def fetch_openrouter_free_models() -> list[tuple[str, str]]:
    fallback = [
        ("nvidia/nemotron-3.5-lightning:free", "NVIDIA Nemotron 3.5 Lightning"),
        ("openrouter/free", "OpenRouter Free Models Router"),
        ("z-ai/glm-5.2:free", "Z.ai GLM 5.2"),
        ("cohere/north-mini-code:free", "Cohere North Mini Code"),
    ]
    try:
        token = subprocess.check_output([str(OPENROUTER_TOKEN_HELPER)], text=True).strip()
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
        models = []
        for model in data.get("data", []):
            pricing = model.get("pricing", {})
            architecture = model.get("architecture", {})
            try:
                is_free = float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0
            except (TypeError, ValueError):
                is_free = False
            input_modalities = architecture.get("input_modalities") or ["text"]
            output_modalities = architecture.get("output_modalities") or ["text"]
            if (
                is_free
                and "text" in input_modalities
                and "text" in output_modalities
                and (model.get("id", "").endswith(":free") or model.get("id") == "openrouter/free")
            ):
                models.append((model.get("id", ""), model.get("name") or model.get("id", "")))
        preferred = {model_id: index for index, (model_id, _) in enumerate(fallback)}
        models.sort(key=lambda item: (preferred.get(item[0], 1000), item[1].lower()))
        return models or fallback
    except (OSError, subprocess.SubprocessError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return fallback


def fetch_opencode_models(base_url: str) -> list[tuple[str, str]]:
    try:
        models_url = base_url.rstrip("/") + "/models"
        with urllib.request.urlopen(models_url, timeout=3) as response:
            payload = json.load(response)
        if not isinstance(payload.get("data"), list):
            return []
        models = []
        for entry in payload["data"]:
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("id") or entry.get("name") or "")
            if model_id:
                models.append((model_id, entry.get("name") or model_id))
        return models
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return []


def models_for(provider_key: str) -> list[tuple[str, str]]:
    if provider_key == "openrouter-free":
        return fetch_openrouter_free_models()
    provider = opencode_provider(provider_key)
    if provider:
        # The model is selected later inside Codex, so when picking the
        # starting model prefer what the live gateway actually serves.  Only
        # fall back to the model list declared in opencode.jsonc (which can go
        # stale) when the gateway does not answer.
        models = fetch_opencode_models(provider["base_url"])
        if not models:
            models = provider.get("models") or []
        return models
    return [("gpt-5.6-luna", "GPT-5.6 Luna — лимит подписки")]


def provider_rows() -> list[tuple[str, str]]:
    rows = [("openai", "Codex"), ("openrouter-free", "OpenRouter")]
    rows.extend(
        (provider["key"], provider["name"])
        for provider in opencode_provider_configs()
    )
    return rows


def list_provider_rows() -> None:
    for provider_key, display_name in provider_rows():
        print(f"{provider_key}\t{display_name}")


def switch_provider(provider_key: str, model: str | None = None) -> None:
    config = NATIVE_CONFIG_PATH
    if not config.is_file():
        raise RuntimeError("Не найден штатный конфиг Codex в ~/.codex.")
    original = config.read_text(encoding="utf-8")
    previous_provider = root_value(original, "model_provider")
    previous_model = root_value(original, "model")
    remember_model(previous_provider, previous_model)

    selected_opencode = opencode_provider(provider_key)
    if provider_key == "openai":
        provider = "openai"
    elif provider_key == "openrouter-free":
        provider = "openrouter"
    elif selected_opencode:
        provider = provider_key
    else:
        raise RuntimeError("Неизвестный провайдер.")

    # The real model is chosen later inside the Codex interface.  The launcher
    # only plants a valid starting model so Codex opens on the requested
    # provider instead of silently falling back to its bundled OpenAI default.
    selected_model = model or default_model_for(provider_key, original)

    if selected_opencode:
        require_opencode_gateway_for(selected_opencode)
    stop_apps(["ChatGPT"])
    updated = ensure_external_provider_tables(original)
    updated = set_root_key(updated, "model_provider", provider)
    updated = set_root_key(updated, "model", selected_model)
    backup_and_write(config, updated)
    remember_model(provider, selected_model)
    open_apps(["ChatGPT"])


def list_account_rows() -> None:
    for account in managed_accounts():
        account_id = account.get("id", "")
        email = account.get("email") or account_id
        if email:
            print(f"{account_id}\t{email}")


def register_existing_system_account() -> None:
    system_default = orca_accounts().get("systemDefault", {})
    provider_id = system_default.get("providerAccountId")
    email = system_default.get("email")
    if not provider_id or not email:
        raise RuntimeError("Вторая авторизованная сессия Codex не найдена.")
    if any(account.get("providerAccountId") == provider_id for account in managed_accounts()):
        return

    source_auth = HOME / ".codex/auth.json"
    if not source_auth.is_file():
        raise RuntimeError("Не найдена локальная авторизация второго Codex-аккаунта.")
    try:
        auth_data = json.loads(source_auth.read_text(encoding="utf-8"))
        source_account_id = auth_data.get("tokens", {}).get("account_id")
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Не удалось прочитать локальную авторизацию Codex.") from error
    if source_account_id and source_account_id != provider_id:
        raise RuntimeError("Локальная авторизация Codex не совпадает со второй сессией Orca.")

    register_codex_home(
        NATIVE_CODEX_HOME,
        expected_provider_id=provider_id,
        workspace_label=system_default.get("workspaceLabel"),
    )


def register_codex_home(
    source_home: Path,
    expected_provider_id: str | None = None,
    workspace_label: str | None = None,
) -> dict:
    """Store a freshly authenticated Codex home without changing the active account."""
    source_home = Path(source_home)
    source_auth = source_home / "auth.json"
    identity = account_from_auth(source_auth)
    provider_id = str(identity.get("providerAccountId") or "")
    email = identity.get("email")
    if not provider_id or not email:
        raise RuntimeError("После входа не удалось определить аккаунт Codex из auth.json.")
    if expected_provider_id and provider_id != str(expected_provider_id):
        raise RuntimeError("Локальная авторизация Codex не совпадает с ожидаемым аккаунтом.")

    existing = next(
        (
            account
            for account in managed_accounts()
            if provider_id
            in {
                str(account.get("id") or ""),
                str(account.get("providerAccountId") or ""),
            }
        ),
        None,
    )
    now = int(time.time() * 1000)
    account_id = str((existing or {}).get("id") or provider_id)
    target_home = Path(
        (existing or {}).get("managedHomePath") or ACCOUNTS_ROOT / account_id / "home"
    )
    target_home.mkdir(parents=True, exist_ok=True)
    target_auth = target_home / "auth.json"
    if source_auth.resolve() != target_auth.resolve():
        backup_and_copy(source_auth, target_auth)
    target_auth.chmod(0o600)
    (target_home / ".orca-managed-home").write_text(account_id, encoding="utf-8")

    active_id = stored_active_account_id()
    try:
        source_config = config_for_account(active_id)
    except RuntimeError:
        source_config = NATIVE_CONFIG_PATH if NATIVE_CONFIG_PATH.is_file() else None
    if source_config and source_config.is_file():
        target_config = target_home / "config.toml"
        if source_config.resolve() != target_config.resolve():
            config_text = source_config.read_text(encoding="utf-8")
            config_text = config_text.replace(str(source_config.parent), str(target_home))
            if target_config.is_file():
                backup_and_write(target_config, config_text)
            else:
                target_config.write_text(config_text, encoding="utf-8")

    account = dict(existing or {})
    account.update(
        {
            "id": account_id,
            "email": email,
            "managedHomePath": str(target_home),
            "managedHomeRuntime": "host",
            "wslDistro": account.get("wslDistro"),
            "wslLinuxHomePath": account.get("wslLinuxHomePath"),
            "providerAccountId": provider_id,
            "workspaceAccountId": account.get("workspaceAccountId") or provider_id,
            "workspaceLabel": account.get("workspaceLabel") or workspace_label,
            "createdAt": account.get("createdAt") or now,
            "updatedAt": now,
            "lastAuthenticatedAt": now,
        }
    )

    def add_account(data: dict) -> dict:
        settings = data.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = {}
            data["settings"] = settings
        accounts = settings.setdefault("codexManagedAccounts", [])
        if not isinstance(accounts, list):
            accounts = []
            settings["codexManagedAccounts"] = accounts
        match_index = next(
            (
                index
                for index, item in enumerate(accounts)
                if isinstance(item, dict)
                and provider_id
                in {
                    str(item.get("id") or ""),
                    str(item.get("providerAccountId") or ""),
                }
            ),
            None,
        )
        if match_index is None:
            accounts.append(account)
        else:
            accounts[match_index] = account
        settings.setdefault("activeCodexManagedAccountId", active_id)
        selection = settings.setdefault("activeCodexManagedAccountIdsByRuntime", {})
        if not isinstance(selection, dict):
            selection = {}
            settings["activeCodexManagedAccountIdsByRuntime"] = selection
        selection.setdefault("host", active_id)
        selection.setdefault("wsl", {})
        return data

    existing_paths = [path for path in ORCA_DATA_PATHS if path.is_file()]
    if existing_paths:
        for path in existing_paths:
            replace_json_with_backup(path, add_account)
    else:
        path = ORCA_DATA_PATHS[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(add_account({}), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return account


def prepare_accounts() -> None:
    if len(managed_accounts()) < 2:
        stop_apps(["ChatGPT", "Orca"])
        register_existing_system_account()
        open_apps(["Orca", "ChatGPT"], ACCOUNTS_ROOT / (active_account_id() or "") / "home")
    list_account_rows()


def add_account() -> None:
    command = " ".join(
        [
            shlex.quote(str(Path(sys.executable).resolve())),
            shlex.quote(str(Path(__file__).resolve())),
            "login-account",
        ]
    )
    script = (
        'tell application "Terminal" to activate\n'
        f'tell application "Terminal" to do script {json.dumps(command)}'
    )
    subprocess.run(["/usr/bin/osascript", "-e", script], check=True)


def login_account() -> None:
    command_path = codex_command()
    if not command_path:
        raise RuntimeError("Не найден Codex CLI. Установите Codex или добавьте команду codex в PATH.")

    temporary_home = Path(tempfile.mkdtemp(prefix="codex-provider-switcher-login-"))
    try:
        environment = launch_environment()
        environment["CODEX_HOME"] = str(temporary_home)
        result = subprocess.run(
            [command_path, "login", "-c", 'cli_auth_credentials_store="file"'],
            env=environment,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Вход Codex не завершён (код {result.returncode}).")
        account = register_codex_home(temporary_home)
        print(f"Аккаунт Codex добавлен: {account['email']}")
    finally:
        shutil.rmtree(temporary_home, ignore_errors=True)


def replace_json_with_backup(path: Path, transform) -> None:
    if not path.is_file():
        return
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)
    updated = transform(data)
    backup_and_write(path, json.dumps(updated, ensure_ascii=False, indent=2) + "\n")


def select_account(account_id: str) -> None:
    accounts = {account.get("id") for account in managed_accounts()}
    if account_id not in accounts:
        raise RuntimeError("Этот аккаунт ещё не зарегистрирован в Orca.")

    stop_apps(["ChatGPT"])
    stop_desktop_codex_server()
    switch_native_auth(account_id)

    if not NATIVE_CONFIG_PATH.is_file():
        raise RuntimeError("Не найден штатный конфиг Codex в ~/.codex.")
    original = NATIVE_CONFIG_PATH.read_text(encoding="utf-8")
    updated = ensure_external_provider_tables(original)
    updated = set_root_key(updated, "model_provider", "openai")
    updated = set_root_key(updated, "model", "gpt-5.6-luna")
    if updated != original:
        backup_and_write(NATIVE_CONFIG_PATH, updated)
    open_apps(["ChatGPT"])


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "commands: list-providers, switch-provider, list-accounts, "
            "switch-account, add-account, login-account",
            file=sys.stderr,
        )
        return 2
    command = sys.argv[1]
    if command == "list-providers" and len(sys.argv) == 2:
        list_provider_rows()
    elif command == "switch-provider" and len(sys.argv) in (3, 4):
        switch_provider(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    elif command == "list-accounts" and len(sys.argv) == 2:
        list_account_rows()
    elif command == "prepare-accounts" and len(sys.argv) == 2:
        prepare_accounts()
    elif command == "switch-account" and len(sys.argv) == 3:
        select_account(sys.argv[2])
    elif command == "add-account" and len(sys.argv) == 2:
        add_account()
    elif command == "login-account" and len(sys.argv) == 2:
        login_account()
    else:
        print("Неверная команда или параметры.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        raise SystemExit(1)
