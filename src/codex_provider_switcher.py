#!/usr/bin/env python3
"""GUI helper backend for switching Codex providers, models, and accounts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
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
            models = provider.get("models") or {}
            if not base_url or not isinstance(models, dict):
                continue
            model_rows = []
            for model_id, model in models.items():
                display_name = model.get("name") if isinstance(model, dict) else None
                model_rows.append((str(model_id), str(display_name or model_id)))
            if model_rows:
                discovered.append(
                    {
                        "id": str(provider_id),
                        "key": "opencode-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(provider_id)),
                        "name": str(provider.get("name") or provider_id),
                        "base_url": str(base_url),
                        "models": model_rows,
                    }
                )
        return discovered
    return []


def opencode_provider(provider_key: str) -> dict | None:
    return next((item for item in opencode_provider_configs() if item["key"] == provider_key), None)


def local_system_account() -> dict:
    auth_path = NATIVE_AUTH_PATH
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = data.get("tokens", {})
        account_id = tokens.get("account_id")
        access_token = tokens.get("access_token", "")
        payload_part = access_token.split(".")[1]
        payload_part += "=" * (-len(payload_part) % 4)
        payload = json.loads(__import__("base64").urlsafe_b64decode(payload_part))
        profile = payload.get("https://api.openai.com/profile", {})
        email = profile.get("email") or payload.get("email")
        if account_id and email:
            return {"providerAccountId": account_id, "email": email, "authKind": "oauth"}
    except (OSError, IndexError, KeyError, ValueError, json.JSONDecodeError):
        pass
    return {}


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
    if current and current.get("id") != account_id:
        current_home = Path(
            current.get("managedHomePath")
            or ACCOUNTS_ROOT / str(current.get("id")) / "home"
        )
        current_auth = current_home / "auth.json"
        if current_auth.is_file():
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
            "accounts": managed_accounts(),
            "activeAccountId": active_account_id(),
            "systemDefault": local_system_account(),
        }


def managed_accounts() -> list[dict]:
    for path in ORCA_DATA_PATHS:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            accounts = data.get("settings", {}).get("codexManagedAccounts")
            if isinstance(accounts, list) and accounts:
                return accounts
        except (OSError, json.JSONDecodeError):
            pass
    return list(orca_accounts().get("accounts", []))


def active_account_id() -> str | None:
    for path in ORCA_DATA_PATHS:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            active = data.get("settings", {}).get("activeCodexManagedAccountId")
            if active:
                return active
        except (OSError, json.JSONDecodeError):
            pass
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
    state_model = load_switcher_state().get("last_models", {}).get(provider)
    if state_model:
        return state_model
    return {
        "openrouter": "nvidia/nemotron-3.5-lightning:free",
        "opencode-copilot": "gh/gpt-5-mini",
        "opencode-kiro": "kiro/claude-haiku-4.5",
    }.get(provider, "gpt-5.6-luna")


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def provider_tables() -> str:
    blocks = [
        OPENROUTER_PROVIDER_TABLE.format(token_helper=toml_string(str(OPENROUTER_TOKEN_HELPER)))
    ]
    for provider in opencode_provider_configs():
        blocks.append(
            "\n".join(
                [
                    f"[model_providers.{provider['key']}]",
                    f"name = {toml_string('OpenCode · ' + provider['name'])}",
                    f"base_url = {toml_string(provider['base_url'])}",
                    'wire_api = "responses"',
                ]
            )
        )
    return "\n\n".join(blocks)


def ensure_external_provider_tables(text: str) -> str:
    tables = provider_tables()
    token_command = f"command = {toml_string(str(OPENROUTER_TOKEN_HELPER))}"
    if "[model_providers.openrouter]" in text:
        text = re.sub(
            r"(?ms)(\[model_providers\.openrouter\.auth\].*?)(?=\n\s*\[|\Z)",
            lambda match: re.sub(
                r"(?m)^\s*command\s*=.*$",
                token_command,
                match.group(1),
                count=1,
            ),
            text,
            count=1,
        )
    else:
        text = text.rstrip() + "\n\n# External model providers\n" + tables
    for provider in opencode_provider_configs():
        marker = f"[model_providers.{provider['key']}]"
        if marker not in text:
            text = text.rstrip() + "\n\n" + next(
                block for block in tables.split("\n\n") if block.startswith(marker)
            )
    return text.rstrip() + "\n"


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


def models_for(provider_key: str) -> list[tuple[str, str]]:
    if provider_key == "openrouter-free":
        return fetch_openrouter_free_models()
    provider = opencode_provider(provider_key)
    if provider:
        return provider["models"]
    return [("gpt-5.6-luna", "GPT-5.6 Luna — лимит подписки")]


def provider_rows() -> list[tuple[str, str]]:
    rows = [("openai", "Codex"), ("openrouter-free", "OpenRouter")]
    rows.extend(
        (provider["key"], f"OpenCode · {provider['name']}")
        for provider in opencode_provider_configs()
    )
    return rows


def list_provider_rows() -> None:
    for provider_key, display_name in provider_rows():
        print(f"{provider_key}\t{display_name}")


def switch_provider(provider_key: str, model: str | None) -> None:
    config = NATIVE_CONFIG_PATH
    if not config.is_file():
        raise RuntimeError("Не найден штатный конфиг Codex в ~/.codex.")
    original = config.read_text(encoding="utf-8")
    previous_provider = root_value(original, "model_provider")
    previous_model = root_value(original, "model")
    remember_model(previous_provider, previous_model)

    if provider_key == "openai":
        provider, selected_model = "openai", "gpt-5.6-luna"
    elif provider_key == "openrouter-free":
        provider, selected_model = "openrouter", model or default_model_for(provider_key, original)
    elif opencode_provider(provider_key):
        provider, selected_model = provider_key, model or default_model_for(provider_key, original)
    else:
        raise RuntimeError("Неизвестный провайдер.")

    selected_opencode = opencode_provider(provider_key)
    if selected_opencode:
        require_opencode_gateway_for(selected_opencode)
    stop_apps(["ChatGPT"])
    updated = ensure_external_provider_tables(original)
    updated = set_root_key(updated, "model_provider", provider)
    updated = set_root_key(updated, "model", selected_model)
    backup_and_write(config, updated)
    remember_model(provider, selected_model)
    open_apps(["ChatGPT"])


def list_model_rows(provider_key: str) -> None:
    for model_id, display_name in models_for(provider_key):
        print(f"{model_id}\t{display_name}")


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

    account_id = provider_id
    target_home = ACCOUNTS_ROOT / account_id / "home"
    target_home.mkdir(parents=True, exist_ok=True)
    target_auth = target_home / "auth.json"
    shutil.copy2(source_auth, target_auth)
    target_auth.chmod(0o600)
    (target_home / ".orca-managed-home").write_text(account_id, encoding="utf-8")

    source_config = config_for_account(active_account_id())
    target_config = target_home / "config.toml"
    config_text = source_config.read_text(encoding="utf-8")
    config_text = config_text.replace(str(source_config.parent), str(target_home))
    target_config.write_text(config_text, encoding="utf-8")

    now = int(time.time() * 1000)
    account = {
        "id": account_id,
        "email": email,
        "managedHomePath": str(target_home),
        "managedHomeRuntime": "host",
        "wslDistro": None,
        "wslLinuxHomePath": None,
        "providerAccountId": provider_id,
        "workspaceLabel": system_default.get("workspaceLabel"),
        "workspaceAccountId": provider_id,
        "createdAt": now,
        "updatedAt": now,
        "lastAuthenticatedAt": now,
    }

    def add_account(data: dict) -> dict:
        settings = data.setdefault("settings", {})
        accounts = settings.setdefault("codexManagedAccounts", [])
        if not any(item.get("id") == account_id for item in accounts):
            accounts.append(account)
        settings.setdefault("activeCodexManagedAccountId", active_account_id())
        selection = settings.setdefault("activeCodexManagedAccountIdsByRuntime", {})
        selection.setdefault("host", active_account_id())
        selection.setdefault("wsl", {})
        return data

    for path in ORCA_DATA_PATHS:
        replace_json_with_backup(path, add_account)


def prepare_accounts() -> None:
    if len(managed_accounts()) < 2:
        stop_apps(["ChatGPT", "Orca"])
        register_existing_system_account()
        open_apps(["Orca", "ChatGPT"], ACCOUNTS_ROOT / (active_account_id() or "") / "home")
    list_account_rows()


def add_account() -> None:
    command_path = orca_command()
    if not command_path:
        raise RuntimeError("Не найден Orca CLI. Установите Orca или добавьте команду orca в PATH.")
    command = f"{command_path} account add --agent codex"
    script = (
        'tell application "Terminal" to activate\n'
        f'tell application "Terminal" to do script {json.dumps(command)}'
    )
    subprocess.run(["/usr/bin/osascript", "-e", script], check=True)


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
        print("commands: list-providers, list-models, switch-provider, list-accounts, switch-account, add-account", file=sys.stderr)
        return 2
    command = sys.argv[1]
    if command == "list-providers" and len(sys.argv) == 2:
        list_provider_rows()
    elif command == "list-models" and len(sys.argv) == 3:
        list_model_rows(sys.argv[2])
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
