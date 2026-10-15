import importlib.util
import base64
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "src/codex_provider_switcher.py"
SPEC = importlib.util.spec_from_file_location("codex_provider_switcher", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ProviderDiscoveryTests(unittest.TestCase):
    @staticmethod
    def fake_auth(account_id, email):
        payload = {
            "https://api.openai.com/profile": {"email": email},
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return json.dumps(
            {
                "tokens": {
                    "account_id": account_id,
                    "access_token": f"header.{encoded}.signature",
                }
            }
        )

    def test_plain_login_home_is_registered_and_switchable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_home = root / "login-home"
            source_home.mkdir()
            source_auth = source_home / "auth.json"
            source_auth.write_text(self.fake_auth("provider-b", "new@example.test"), encoding="utf-8")

            native_auth = root / "native" / "auth.json"
            native_auth.parent.mkdir()
            current_auth_text = self.fake_auth("provider-a", "current@example.test")
            native_auth.write_text(current_auth_text, encoding="utf-8")
            native_config = root / "native" / "config.toml"
            native_config.write_text('model = "gpt-5.6-luna"\nmodel_provider = "openai"\n', encoding="utf-8")

            current_home = root / "accounts" / "account-a" / "home"
            current_home.mkdir(parents=True)
            (current_home / "auth.json").write_text("stale-current", encoding="utf-8")
            data_path = root / "orca-data.json"
            data_path.write_text(
                json.dumps(
                    {
                        "settings": {
                            "codexManagedAccounts": [
                                {
                                    "id": "account-a",
                                    "providerAccountId": "provider-a",
                                    "email": "current@example.test",
                                    "managedHomePath": str(current_home),
                                }
                            ],
                            "activeCodexManagedAccountId": "account-a",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(MODULE, "ACCOUNTS_ROOT", root / "accounts"),
                patch.object(MODULE, "ORCA_DATA_PATHS", [data_path]),
                patch.object(MODULE, "NATIVE_AUTH_PATH", native_auth),
                patch.object(MODULE, "NATIVE_CONFIG_PATH", native_config),
                patch.object(MODULE, "auth_account_id", return_value="provider-a"),
            ):
                account = MODULE.register_codex_home(source_home)
                target_home = root / "accounts" / "provider-b" / "home"

                self.assertEqual(account["id"], "provider-b")
                self.assertEqual((target_home / "auth.json").read_text(encoding="utf-8"), source_auth.read_text(encoding="utf-8"))
                self.assertTrue((target_home / ".orca-managed-home").is_file())
                saved = json.loads(data_path.read_text(encoding="utf-8"))
                saved_accounts = saved["settings"]["codexManagedAccounts"]
                self.assertEqual(len(saved_accounts), 2)
                self.assertEqual(saved["settings"]["activeCodexManagedAccountId"], "account-a")

                MODULE.switch_native_auth("provider-b")

                self.assertEqual(native_auth.read_text(encoding="utf-8"), source_auth.read_text(encoding="utf-8"))
                self.assertEqual((current_home / "auth.json").read_text(encoding="utf-8"), current_auth_text)

    def test_plain_login_command_isolated_from_native_auth(self):
        captured = {}

        def fake_run(command, env, check):
            captured["command"] = command
            captured["env"] = env
            return MODULE.subprocess.CompletedProcess(command, 0)

        with (
            patch.object(MODULE, "codex_command", return_value="/fake/codex"),
            patch.object(MODULE.subprocess, "run", side_effect=fake_run),
            patch.object(MODULE, "register_codex_home", return_value={"email": "new@example.test"}),
        ):
            MODULE.login_account()

        self.assertEqual(
            captured["command"],
            ["/fake/codex", "login", "-c", 'cli_auth_credentials_store="file"'],
        )
        self.assertNotIn("--device-auth", captured["command"])
        self.assertNotIn("ORCA_CODEX_HOME", captured["env"])
        self.assertFalse(Path(captured["env"]["CODEX_HOME"]).exists())

    def test_add_account_launches_plain_login_helper(self):
        with patch.object(MODULE.subprocess, "run") as run:
            MODULE.add_account()

        command = run.call_args.args[0]
        script = command[2]
        self.assertIn("login-account", script)
        self.assertNotIn("account add --agent codex", script)

    def test_jsonc_provider_and_models_are_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "opencode.jsonc"
            config.write_text(
                """
                {
                  // comments are supported
                  "provider": {
                    "example": {
                      "name": "Example Gateway",
                      "models": {
                        "example/free-model": { "name": "Free Model" },
                      },
                      "options": { "baseURL": "http://127.0.0.1:20128/v1" },
                    },
                  },
                }
                """,
                encoding="utf-8",
            )
            original = MODULE.jsonc_paths
            MODULE.jsonc_paths = lambda: [config]
            try:
                providers = MODULE.opencode_provider_configs()
            finally:
                MODULE.jsonc_paths = original

        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0]["key"], "opencode-example")
        self.assertEqual(providers[0]["models"], [("example/free-model", "Free Model")])

    def test_provider_without_static_models_is_still_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "opencode.jsonc"
            config.write_text(
                """
                {
                  "provider": {
                    "local": {
                      "name": "Local Gateway",
                      "options": { "baseURL": "http://127.0.0.1:20128/v1" },
                    },
                  },
                }
                """,
                encoding="utf-8",
            )
            original = MODULE.jsonc_paths
            MODULE.jsonc_paths = lambda: [config]
            try:
                providers = MODULE.opencode_provider_configs()
            finally:
                MODULE.jsonc_paths = original

        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0]["key"], "opencode-local")
        self.assertEqual(providers[0]["models"], [])

    def test_provider_api_key_is_extracted_from_options(self):
        provider = {
            "name": "Local Gateway",
            "options": {"baseURL": "http://127.0.0.1:20128/v1", "apiKey": "secret-key"},
        }
        self.assertEqual(MODULE.opencode_provider_auth(provider), "secret-key")

    def test_provider_tables_use_responses_wire_api_and_bearer_token(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "opencode.jsonc"
            config.write_text(
                """
                {
                  "provider": {
                    "local": {
                      "name": "Kiro AI",
                      "options": { "baseURL": "http://127.0.0.1:20128/v1", "apiKey": "secret-key" },
                    },
                  },
                }
                """,
                encoding="utf-8",
            )
            original = MODULE.jsonc_paths
            MODULE.jsonc_paths = lambda: [config]
            try:
                tables = MODULE.provider_tables()
            finally:
                MODULE.jsonc_paths = original

        self.assertIn("[model_providers.opencode-local]", tables)
        self.assertIn('name = "Kiro AI"', tables)
        self.assertIn('wire_api = "responses"', tables)
        self.assertNotIn('wire_api = "chat"', tables)
        self.assertIn('experimental_bearer_token = "secret-key"', tables)
        self.assertNotIn("api_key =", tables)
        self.assertNotIn("[model_providers.opencode-local.auth]", tables)

    def test_copilot_provider_is_excluded_from_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "opencode.jsonc"
            config.write_text(
                """
                {
                  "provider": {
                    "copilot": {
                      "name": "OpenCode · Copilot",
                      "options": { "baseURL": "http://127.0.0.1:20128/v1" },
                    },
                    "kiro": {
                      "name": "Kiro AI",
                      "options": { "baseURL": "http://127.0.0.1:20128/v1" },
                    },
                  },
                }
                """,
                encoding="utf-8",
            )
            original = MODULE.jsonc_paths
            MODULE.jsonc_paths = lambda: [config]
            try:
                providers = MODULE.opencode_provider_configs()
                rows = MODULE.provider_rows()
            finally:
                MODULE.jsonc_paths = original

        self.assertEqual([provider["key"] for provider in providers], ["opencode-kiro"])
        self.assertEqual(
            [row for row in rows if row[0].startswith("opencode-")],
            [("opencode-kiro", "Kiro AI")],
        )

    def test_managed_provider_tables_are_regenerated(self):
        stale = (
            'model = "gpt-5.6"\n'
            'model_provider = "openai"\n\n'
            "# External model providers\n"
            "[model_providers.openrouter]\n"
            'name = "OpenRouter"\n'
            'base_url = "https://openrouter.ai/api/v1"\n'
            'wire_api = "chat"\n\n'
            "[model_providers.openrouter.auth]\n"
            'command = "/old/helper"\n\n'
            "[model_providers.opencode-copilot]\n"
            'name = "OpenCode · Copilot"\n\n'
            "[model_providers.opencode-kiro]\n"
            'name = "OpenCode · Kiro AI"\n'
            'wire_api = "chat"\n\n'
            "[other]\n"
            'keep = "yes"\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "opencode.jsonc"
            config.write_text(
                """
                {
                  "provider": {
                    "kiro": {
                      "name": "Kiro AI",
                      "options": { "baseURL": "http://127.0.0.1:20128/v1" },
                    },
                  },
                }
                """,
                encoding="utf-8",
            )
            original = MODULE.jsonc_paths
            MODULE.jsonc_paths = lambda: [config]
            try:
                updated = MODULE.ensure_external_provider_tables(stale)
            finally:
                MODULE.jsonc_paths = original

        self.assertIn('model = "gpt-5.6"', updated)
        self.assertIn("[other]\nkeep = \"yes\"", updated)
        self.assertNotIn("opencode-copilot", updated)
        self.assertNotIn("OpenCode · ", updated)
        self.assertEqual(updated.count("[model_providers.openrouter]"), 1)
        self.assertEqual(updated.count("[model_providers.opencode-kiro]"), 1)
        self.assertIn('name = "Kiro AI"', updated)
        self.assertIn('wire_api = "responses"', updated)
        self.assertNotIn('wire_api = "chat"', updated)
        self.assertEqual(updated.count("# External model providers"), 1)

    def test_opencode_falls_back_to_gateway_when_no_static_models(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "opencode.jsonc"
            config.write_text(
                """
                {
                  "provider": {
                    "local": {
                      "name": "Local Gateway",
                      "options": { "baseURL": "http://127.0.0.1:20128/v1" },
                    },
                  },
                }
                """,
                encoding="utf-8",
            )
            original_paths = MODULE.jsonc_paths
            original_fetch = MODULE.fetch_opencode_models
            MODULE.jsonc_paths = lambda: [config]
            MODULE.fetch_opencode_models = lambda base_url: [("local/model-a", "Model A")]
            try:
                models = MODULE.models_for("opencode-local")
            finally:
                MODULE.jsonc_paths = original_paths
                MODULE.fetch_opencode_models = original_fetch

        self.assertEqual(models, [("local/model-a", "Model A")])

    def test_local_paths_are_not_embedded_in_source(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("/Users/", source)
        self.assertNotIn("sk-or-", source)
        self.assertNotIn("@gmail.com", source)

    def test_native_launch_drops_managed_runtime_context(self):
        with patch.dict(
            MODULE.os.environ,
            {
                "CODEX_HOME": "/tmp/managed-codex",
                "ORCA_CODEX_HOME": "/tmp/managed-codex",
                "OPENCODE_CONFIG_DIR": "/tmp/orca-opencode",
                "PATH": "/tmp/managed-codex/bin:/usr/bin",
            },
            clear=False,
        ):
            environment = MODULE.launch_environment()

        self.assertNotIn("CODEX_HOME", environment)
        self.assertNotIn("ORCA_CODEX_HOME", environment)
        self.assertNotIn("OPENCODE_CONFIG_DIR", environment)
        self.assertEqual(
            environment["PATH"],
            "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
        )

    def test_selecting_active_account_keeps_refreshed_native_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native_auth = root / "native" / "auth.json"
            managed_home = root / "managed"
            native_auth.parent.mkdir()
            managed_home.mkdir()
            native_auth.write_text("refreshed", encoding="utf-8")
            managed_auth = managed_home / "auth.json"
            managed_auth.write_text("stale", encoding="utf-8")
            account = {
                "id": "account-a",
                "providerAccountId": "provider-a",
                "managedHomePath": str(managed_home),
            }
            with (
                patch.object(MODULE, "NATIVE_AUTH_PATH", native_auth),
                patch.object(MODULE, "managed_accounts", return_value=[account]),
                patch.object(MODULE, "auth_account_id", return_value="provider-a"),
            ):
                MODULE.switch_native_auth("account-a")

            self.assertEqual(native_auth.read_text(encoding="utf-8"), "refreshed")
            self.assertEqual(managed_auth.read_text(encoding="utf-8"), "stale")

    def test_switching_accounts_persists_current_refreshed_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native_auth = root / "native" / "auth.json"
            current_home = root / "current"
            selected_home = root / "selected"
            native_auth.parent.mkdir()
            current_home.mkdir()
            selected_home.mkdir()
            native_auth.write_text("refreshed-current", encoding="utf-8")
            (current_home / "auth.json").write_text("stale-current", encoding="utf-8")
            (selected_home / "auth.json").write_text("selected", encoding="utf-8")
            accounts = [
                {
                    "id": "account-a",
                    "providerAccountId": "provider-a",
                    "managedHomePath": str(current_home),
                },
                {
                    "id": "account-b",
                    "providerAccountId": "provider-b",
                    "managedHomePath": str(selected_home),
                },
            ]
            with (
                patch.object(MODULE, "NATIVE_AUTH_PATH", native_auth),
                patch.object(MODULE, "managed_accounts", return_value=accounts),
                patch.object(MODULE, "auth_account_id", return_value="provider-a"),
            ):
                MODULE.switch_native_auth("account-b")

            self.assertEqual(
                (current_home / "auth.json").read_text(encoding="utf-8"),
                "refreshed-current",
            )
            self.assertEqual(native_auth.read_text(encoding="utf-8"), "selected")


if __name__ == "__main__":
    unittest.main()
