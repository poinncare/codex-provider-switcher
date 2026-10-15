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

# Providers the launcher registers in Codex.  Codex and OpenRouter are fixed
# entries; "OpenCode" is one entry that mirrors every model OpenCode itself
# offers.  OpenCode spreads those models over several upstreams with their own
# endpoints and credentials, so a small loopback router (see
# src/opencode_router.py) sits between Codex and OpenCode and forwards each
# request to the upstream OpenCode configured for that model.
OPENCODE_PROVIDER_KEY = "opencode"
OPENCODE_PROVIDER_NAME = "OpenCode"
OPENCODE_ROUTER_MODULE = PROJECT_ROOT / "src/opencode_router.py"
OPENCODE_ROUTER_HOST = "127.0.0.1"
OPENCODE_ROUTER_PORT = 20130
OPENCODE_ROUTER_SERVICE = "codex-provider-switcher-router"
OPENCODE_ROUTER_STATE_PATH = NATIVE_CODEX_HOME / "codex-provider-switcher-router.json"
OPENCODE_ROUTER_LOG_PATH = NATIVE_CODEX_HOME / "codex-provider-switcher-router.log"
OPENCODE_ROUTER_READY_TIMEOUT = 30.0
# OpenCode's own state: where its background service listens and which models
# were used last.  Both are read-only inputs for the launcher.
OPENCODE_STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME") or HOME / ".local/state"
).expanduser() / "opencode"
OPENCODE_SERVICE_PATH = OPENCODE_STATE_DIR / "service.json"
OPENCODE_CLI_CANDIDATES = (
    os.environ.get("OPENCODE_CLI_COMMAND"),
    HOME / ".opencode/bin/opencode",
    "/opt/homebrew/bin/opencode",
    "/usr/local/bin/opencode",
)
# Provider ids owned by the launcher.  These are the only sections the launcher
# creates, refreshes and removes in Codex config.toml.  The `opencode-` prefix
# is kept so tables written by earlier releases are still cleaned up.
MANAGED_PROVIDER_KEYS = ("openrouter", OPENCODE_PROVIDER_KEY)
MANAGED_PROVIDER_PREFIX = "opencode-"
# Comment written above the launcher-managed provider section.
EXTERNAL_PROVIDERS_MARKER = "# External model providers"
# Codex replaces its entire model picker with whatever catalog this root config
# key points at, and that is the only way to make it list models it does not
# ship itself.  The launcher rewrites the catalog from the model list OpenCode
# itself reports on every switch, so Codex and OpenCode always offer the same
# list.  The key exists only while OpenCode is selected and is removed again for
# Codex/OpenRouter.
MODEL_CATALOG_ENV = "CODEX_MODEL_CATALOG_JSON"
MODEL_CATALOG_FILENAME = "codex-provider-switcher-models.json"
MODEL_CATALOG_ROOT_KEY = "model_catalog_json"
# Used when a provider reports no context length for a model.
DEFAULT_CATALOG_CONTEXT_WINDOW = 200000
# Codex rejects catalog entries without model instructions.  Reusing the prompt
# Codex already caches for its own models keeps third-party models behaving like
# first-party ones; this short text is only used when no cache exists yet.
FALLBACK_MODEL_INSTRUCTIONS = (
    "You are Codex, an agent working in the user's workspace. Use the provided "
    "tools to inspect the repository, run commands and make edits, then report "
    "what you changed."
)


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


def opencode_cli_path() -> str | None:
    """Path to the OpenCode CLI, used to wake its background service."""
    for candidate in OPENCODE_CLI_CANDIDATES:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which("opencode")


def opencode_service_info() -> dict | None:
    """URL and password of the running OpenCode service, if it is alive."""
    try:
        payload = json.loads(OPENCODE_SERVICE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    url = payload.get("url")
    pid = payload.get("pid")
    if not isinstance(url, str) or not url:
        return None
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
        except OSError:
            # A stale state file survives a crashed service; its pid is gone.
            return None
    return {"url": url.rstrip("/"), "password": str(payload.get("password") or "")}


def opencode_service_ready() -> bool:
    info = opencode_service_info()
    if not info:
        return False
    request = urllib.request.Request(info["url"] + "/api/info")
    token = base64.b64encode(f"opencode:{info['password']}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def ensure_opencode_service() -> None:
    """Wake OpenCode's background service, which answers the model list.

    The service starts on demand when the CLI runs; running `opencode models`
    is the cheapest way to trigger that without opening a window.
    """
    if opencode_service_ready():
        return
    cli = opencode_cli_path()
    if not cli:
        return
    try:
        subprocess.run([cli, "models"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        pass


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


def remove_root_key(text: str, key: str) -> str:
    """Drop a top-level key so a stale value cannot survive a switch."""
    first_table = re.search(r"(?m)^\s*\[", text)
    split_at = first_table.start() if first_table else len(text)
    root, tables = text[:split_at], text[split_at:]
    root = re.sub(rf"(?m)^\s*{re.escape(key)}\s*=.*\n?", "", root)
    return root + tables


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


def default_model_for(provider_key: str, current_config: str, available: list[str]) -> str:
    provider = {"openrouter-free": "openrouter"}.get(provider_key, provider_key)
    current_provider = root_value(current_config, "model_provider")
    current_model = root_value(current_config, "model")
    # The model is picked later inside the Codex interface, so the switcher only
    # needs a valid starting point.  Prefer whatever the provider currently
    # advertises instead of a hard-coded list that goes stale, and never keep a
    # model that belongs to a previous provider (for example a bundled Codex
    # model left over from before the switch).
    if current_provider == provider and current_model in available:
        return current_model
    if available:
        return available[0]
    state_model = load_switcher_state().get("last_models", {}).get(provider)
    if state_model:
        return state_model
    return "gpt-5.6-luna"


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def provider_tables(opencode: dict | None = None) -> str:
    blocks = [
        OPENROUTER_PROVIDER_TABLE.format(token_helper=toml_string(str(OPENROUTER_TOKEN_HELPER)))
    ]
    if opencode:
        # Codex only supports the Responses wire API; the legacy "chat" value is
        # rejected outright.  The router speaks Responses and translates nothing,
        # it just forwards to the upstream OpenCode configured per model.
        lines = [
            f"[model_providers.{opencode['key']}]",
            f"name = {toml_string(opencode['name'])}",
            f"base_url = {toml_string(opencode['base_url'])}",
            'wire_api = "responses"',
        ]
        api_key = opencode.get("api_key")
        if api_key:
            # Codex's ModelProviderInfo denies unknown fields inside [.auth], so
            # a static bearer key belongs at provider level.
            lines.append(f"experimental_bearer_token = {toml_string(api_key)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def is_managed_provider_key(key: str) -> bool:
    """True when the launcher owns this [model_providers.*] section."""
    return key in MANAGED_PROVIDER_KEYS or key.startswith(MANAGED_PROVIDER_PREFIX)


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


def ensure_external_provider_tables(text: str, opencode: dict | None = None) -> str:
    stripped = strip_managed_provider_tables(text).rstrip()
    tables = provider_tables(opencode)
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


def opencode_router_base() -> str:
    return f"http://{OPENCODE_ROUTER_HOST}:{OPENCODE_ROUTER_PORT}"


def opencode_router_source_stamp() -> str:
    """Timestamp of the router source the launcher expects to be running.

    A long-lived daemon would otherwise keep serving old code after an update,
    so a mismatch restarts it instead of silently mixing versions.
    """
    try:
        return str(int(OPENCODE_ROUTER_MODULE.stat().st_mtime))
    except OSError:
        return ""


def opencode_router_state() -> dict:
    try:
        payload = json.loads(OPENCODE_ROUTER_STATE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def opencode_router_health(base_url: str) -> dict | None:
    """Health payload of *our* router; None when something else owns the port."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=2) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(payload, dict) and payload.get("service") == OPENCODE_ROUTER_SERVICE:
        return payload
    return None


def opencode_router_pids() -> list[int]:
    result = subprocess.run(
        ["/usr/sbin/lsof", "-ti", f"tcp:{OPENCODE_ROUTER_PORT}", "-sTCP:LISTEN"],
        check=False,
        capture_output=True,
        text=True,
    )
    pids = []
    for line in result.stdout.split():
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def stop_opencode_router() -> None:
    for pid in opencode_router_pids():
        try:
            os.kill(pid, 15)
        except OSError:
            continue
    deadline = time.time() + 5
    while time.time() < deadline and opencode_router_pids():
        time.sleep(0.25)


def start_opencode_router() -> bool:
    """Start the router detached so it survives the launcher's exit."""
    if not OPENCODE_ROUTER_MODULE.is_file():
        return False
    try:
        OPENCODE_ROUTER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_handle = OPENCODE_ROUTER_LOG_PATH.open("a", encoding="utf-8")
    except OSError:
        log_handle = subprocess.DEVNULL
    command = [
        sys.executable or "python3",
        str(OPENCODE_ROUTER_MODULE),
        "serve",
        "--host",
        OPENCODE_ROUTER_HOST,
        "--port",
        str(OPENCODE_ROUTER_PORT),
    ]
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(PROJECT_ROOT),
            env=launch_environment(),
        )
    except (OSError, ValueError):
        if log_handle is not subprocess.DEVNULL:
            log_handle.close()
        return False
    return True


def ensure_opencode_router() -> tuple[str, str | None]:
    """Run the OpenCode router and return its base url and access token.

    Codex always talks to this single loopback endpoint; which upstream and
    which credentials serve a model is decided per request, from the model list
    OpenCode itself reports.
    """
    base_url = opencode_router_base()
    expected_stamp = opencode_router_source_stamp()
    for _ in range(2):
        health = opencode_router_health(base_url)
        token = opencode_router_state().get("token")
        if health and isinstance(token, str) and token:
            if health.get("stamp") == expected_stamp:
                return base_url, token
            # The daemon predates the current router source; restart it.
        if health:
            stop_opencode_router()
        start_opencode_router()
        deadline = time.time() + OPENCODE_ROUTER_READY_TIMEOUT
        while time.time() < deadline:
            health = opencode_router_health(base_url)
            token = opencode_router_state().get("token")
            if health and isinstance(token, str) and token:
                if health.get("stamp") == expected_stamp:
                    return base_url, token
            time.sleep(0.25)
    raise RuntimeError(
        "Не удалось запустить локальный роутер OpenCode "
        f"({base_url}). Смотрите лог: {OPENCODE_ROUTER_LOG_PATH}"
    )


def opencode_router_models(base_url: str, token: str | None) -> list[tuple[str, str, int | None]]:
    """Models the router offers: (model id, display name, context window)."""
    request = urllib.request.Request(base_url.rstrip("/") + "/v1/models")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return []
    models: list[tuple[str, str, int | None]] = []
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "")
        if not model_id:
            continue
        display_name = str(entry.get("name") or model_id)
        context_window = entry.get("context_length")
        models.append(
            (
                model_id,
                display_name,
                context_window if isinstance(context_window, int) and context_window > 0 else None,
            )
        )
    return models


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


def models_for(provider_key: str) -> list[tuple[str, str]]:
    """Models for the providers that do not go through OpenCode."""
    if provider_key == "openrouter-free":
        return fetch_openrouter_free_models()
    return [("gpt-5.6-luna", "GPT-5.6 Luna — лимит подписки")]


def model_catalog_path() -> Path:
    override = os.environ.get(MODEL_CATALOG_ENV)
    if override:
        return Path(override).expanduser()
    # Keep the catalog next to the config.toml the launcher rewrites: that is
    # always the native ~/.codex home, even when a CODEX_HOME is exported by the
    # caller (the app itself is launched without one).
    return NATIVE_CONFIG_PATH.parent / MODEL_CATALOG_FILENAME


def cached_model_messages() -> dict:
    """Instructions Codex already caches for its own models.

    Codex refuses catalog entries that carry no model instructions, and its own
    prompt keeps approval/permission behaviour intact, so prefer it over the
    built-in fallback text.
    """
    for path in dict.fromkeys(
        (
            model_catalog_path().parent / "models_cache.json",
            NATIVE_CODEX_HOME / "models_cache.json",
        )
    ):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for model in payload.get("models") or []:
            if not isinstance(model, dict):
                continue
            messages = model.get("model_messages")
            if isinstance(messages, dict) and messages.get("instructions_template"):
                return messages
    return {"instructions_template": FALLBACK_MODEL_INSTRUCTIONS}


def catalog_model_entry(
    model_id: str,
    display_name: str,
    priority: int,
    messages: dict,
    context_window: int | None = None,
) -> dict:
    """One Codex catalog entry.

    Only the fields Codex requires are set; reasoning levels, tool modes and
    Responses-lite flags stay unset so a third-party model keeps the exact
    request shape Codex already uses for model slugs it does not know.
    """
    return {
        "slug": model_id,
        "display_name": display_name,
        "description": display_name,
        "supported_reasoning_levels": [],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "availability_nux": None,
        "upgrade": None,
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": None,
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "experimental_supported_tools": [],
        "context_window": context_window or DEFAULT_CATALOG_CONTEXT_WINDOW,
        "model_messages": messages,
    }


def build_model_catalog(models: list[tuple[str, str, int | None]]) -> dict:
    messages = cached_model_messages()
    return {
        "models": [
            catalog_model_entry(model_id, display_name, index + 1, messages, context_window)
            for index, (model_id, display_name, context_window) in enumerate(models)
        ]
    }


def sync_model_catalog(models: list[tuple[str, str, int | None]]) -> Path | None:
    """Refresh the catalog Codex reads for the selected provider.

    Returns the written path, or None when there are no models to write or the
    file cannot be written (Codex then falls back to its bundled list).
    """
    if not models:
        return None
    path = model_catalog_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(build_model_catalog(models), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return None
    return path


def provider_rows() -> list[tuple[str, str]]:
    """The three entries the launcher offers.

    Codex is the bundled subscription entry, OpenRouter brings its own free
    models, and OpenCode mirrors everything OpenCode itself serves.
    """
    return [
        ("openai", "Codex"),
        ("openrouter-free", "OpenRouter"),
        (OPENCODE_PROVIDER_KEY, OPENCODE_PROVIDER_NAME),
    ]


def available_models_for(provider_key: str, opencode: dict | None = None) -> list[str]:
    if opencode:
        return [model_id for model_id, _, _ in opencode["models"]]
    return [model_id for model_id, _ in models_for(provider_key)]


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

    opencode: dict | None = None
    if provider_key == "openai":
        provider = "openai"
    elif provider_key == "openrouter-free":
        provider = "openrouter"
    elif provider_key == OPENCODE_PROVIDER_KEY:
        # Codex gets one provider that mirrors OpenCode.  Everything the router
        # needs (upstream endpoint, credentials, per-model request extras) comes
        # from OpenCode's own model list, so Codex and OpenCode stay in sync.
        ensure_opencode_service()
        base_url, token = ensure_opencode_router()
        models = opencode_router_models(base_url, token)
        if not models:
            raise RuntimeError(
                "OpenCode не отдал список моделей. Запустите OpenCode "
                f"и повторите (лог роутера: {OPENCODE_ROUTER_LOG_PATH})."
            )
        opencode = {
            "key": OPENCODE_PROVIDER_KEY,
            "name": OPENCODE_PROVIDER_NAME,
            "base_url": base_url.rstrip("/") + "/v1",
            "api_key": token,
            "models": models,
        }
        provider = OPENCODE_PROVIDER_KEY
    else:
        raise RuntimeError("Неизвестный провайдер.")

    # The real model is chosen later inside the Codex interface.  The launcher
    # only plants a valid starting model so Codex opens on the requested
    # provider instead of silently falling back to its bundled OpenAI default.
    available = available_models_for(provider_key, opencode)
    selected_model = model or default_model_for(provider_key, original, available)

    # Codex reads the catalog once at startup, so refresh it before relaunching.
    catalog = sync_model_catalog(opencode["models"]) if opencode else None
    stop_apps(["ChatGPT"])
    updated = ensure_external_provider_tables(original, opencode)
    updated = set_root_key(updated, "model_provider", provider)
    updated = set_root_key(updated, "model", selected_model)
    updated = remove_root_key(updated, MODEL_CATALOG_ROOT_KEY)
    if catalog:
        updated = set_root_key(updated, MODEL_CATALOG_ROOT_KEY, str(catalog))
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
    updated = remove_root_key(updated, MODEL_CATALOG_ROOT_KEY)
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
