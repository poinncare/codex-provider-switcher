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

    def test_launcher_offers_codex_openrouter_and_opencode(self):
        self.assertEqual(
            MODULE.provider_rows(),
            [
                ("openai", "Codex"),
                ("openrouter-free", "OpenRouter"),
                ("opencode", "OpenCode"),
            ],
        )

    def test_provider_table_points_codex_at_the_local_router(self):
        opencode = {
            "key": "opencode",
            "name": "OpenCode",
            "base_url": "http://127.0.0.1:20130/v1",
            "api_key": "router-token",
            "models": [("opencode/free-model", "Free Model", 200000)],
        }
        tables = MODULE.provider_tables(opencode)

        self.assertIn("[model_providers.opencode]", tables)
        self.assertIn('name = "OpenCode"', tables)
        self.assertIn('base_url = "http://127.0.0.1:20130/v1"', tables)
        self.assertIn('wire_api = "responses"', tables)
        self.assertNotIn('wire_api = "chat"', tables)
        self.assertIn('experimental_bearer_token = "router-token"', tables)
        self.assertNotIn("api_key =", tables)
        self.assertNotIn("[model_providers.opencode.auth]", tables)

    def test_provider_tables_without_opencode_keep_only_openrouter(self):
        tables = MODULE.provider_tables()

        self.assertIn("[model_providers.openrouter]", tables)
        self.assertNotIn("[model_providers.opencode", tables)

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
            "[model_providers.opencode-kiro]\n"
            'name = "OpenCode"\n'
            'base_url = "http://localhost:20128/v1"\n\n'
            "[other]\n"
            'keep = "yes"\n'
        )
        opencode = {
            "key": "opencode",
            "name": "OpenCode",
            "base_url": "http://127.0.0.1:20130/v1",
            "api_key": "router-token",
            "models": [("opencode/free-model", "Free Model", 200000)],
        }
        updated = MODULE.ensure_external_provider_tables(stale, opencode)

        self.assertIn('model = "gpt-5.6"', updated)
        self.assertIn("[other]\nkeep = \"yes\"", updated)
        # The OmniRoute-backed table written by earlier releases is gone.
        self.assertNotIn("localhost:20128", updated)
        self.assertNotIn("opencode-kiro", updated)
        self.assertEqual(updated.count("[model_providers.openrouter]"), 1)
        self.assertEqual(updated.count("[model_providers.opencode]"), 1)
        self.assertIn('wire_api = "responses"', updated)
        self.assertNotIn('wire_api = "chat"', updated)
        self.assertEqual(updated.count("# External model providers"), 1)

    def test_managed_provider_tables_are_dropped_for_bundled_providers(self):
        stale = (
            'model_provider = "opencode"\n\n'
            "# External model providers\n"
            "[model_providers.openrouter]\n"
            'name = "OpenRouter"\n\n'
            "[model_providers.opencode]\n"
            'name = "OpenCode"\n'
            'base_url = "http://127.0.0.1:20130/v1"\n'
        )
        updated = MODULE.ensure_external_provider_tables(stale)

        self.assertIn("[model_providers.openrouter]", updated)
        self.assertNotIn("[model_providers.opencode]", updated)

    def test_router_is_reused_when_it_runs_current_code(self):
        with (
            patch.object(MODULE, "opencode_router_health", return_value={"stamp": "7"}),
            patch.object(MODULE, "opencode_router_source_stamp", return_value="7"),
            patch.object(MODULE, "opencode_router_state", return_value={"token": "router-token"}),
            patch.object(MODULE, "start_opencode_router") as start,
            patch.object(MODULE, "stop_opencode_router") as stop,
        ):
            base_url, token = MODULE.ensure_opencode_router()

        self.assertEqual(base_url, MODULE.opencode_router_base())
        self.assertEqual(token, "router-token")
        start.assert_not_called()
        stop.assert_not_called()

    def test_router_is_restarted_when_it_runs_older_code(self):
        stamps = {"value": "6"}

        def fake_start():
            stamps["value"] = "7"
            return True

        with (
            patch.object(
                MODULE,
                "opencode_router_health",
                side_effect=lambda _url: {"stamp": stamps["value"]},
            ),
            patch.object(MODULE, "opencode_router_source_stamp", return_value="7"),
            patch.object(MODULE, "opencode_router_state", return_value={"token": "router-token"}),
            patch.object(MODULE, "start_opencode_router", side_effect=fake_start) as start,
            patch.object(MODULE, "stop_opencode_router") as stop,
        ):
            _, token = MODULE.ensure_opencode_router()

        stop.assert_called_once()
        start.assert_called_once()
        self.assertEqual(token, "router-token")

    def test_router_failure_is_reported_with_the_log_path(self):
        with (
            patch.object(MODULE, "opencode_router_health", return_value=None),
            patch.object(MODULE, "opencode_router_source_stamp", return_value="7"),
            patch.object(MODULE, "opencode_router_state", return_value={}),
            patch.object(MODULE, "start_opencode_router", return_value=False),
            patch.object(MODULE, "OPENCODE_ROUTER_READY_TIMEOUT", 0.0),
        ):
            with self.assertRaises(RuntimeError) as caught:
                MODULE.ensure_opencode_router()

        self.assertIn("роутер OpenCode", str(caught.exception))
        self.assertIn(str(MODULE.OPENCODE_ROUTER_LOG_PATH), str(caught.exception))

    def test_router_models_are_read_with_names_and_context_windows(self):
        class FakeResponse:
            status = 200

            def read(self, *args):
                return json.dumps(
                    {
                        "data": [
                            {"id": "opencode/free-model", "name": "Free Model", "context_length": 200000},
                            {"id": "openai/gpt-6", "context_length": 0},
                        ]
                    }
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.object(MODULE.urllib.request, "urlopen", return_value=FakeResponse()):
            models = MODULE.opencode_router_models("http://127.0.0.1:20130", "router-token")

        self.assertEqual(
            models,
            [
                ("opencode/free-model", "Free Model", 200000),
                ("openai/gpt-6", "openai/gpt-6", None),
            ],
        )

    def test_router_models_are_empty_when_the_router_is_unreachable(self):
        with patch.object(MODULE.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(MODULE.opencode_router_models("http://127.0.0.1:20130", "t"), [])

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
    def test_model_catalog_builds_codex_entries(self):
        with patch.object(
            MODULE, "cached_model_messages", return_value={"instructions_template": "prompt"}
        ):
            catalog = MODULE.build_model_catalog(
                [("opencode/a", "Model A", 400000), ("openai/b", "Model B", None)]
            )

        self.assertEqual([model["slug"] for model in catalog["models"]], ["opencode/a", "openai/b"])
        self.assertEqual(
            [model["display_name"] for model in catalog["models"]], ["Model A", "Model B"]
        )
        self.assertEqual([model["priority"] for model in catalog["models"]], [1, 2])
        self.assertEqual(
            [model["context_window"] for model in catalog["models"]],
            [400000, MODULE.DEFAULT_CATALOG_CONTEXT_WINDOW],
        )
        for model in catalog["models"]:
            self.assertEqual(model["visibility"], "list")
            self.assertTrue(model["supported_in_api"])
            self.assertEqual(model["model_messages"], {"instructions_template": "prompt"})

    def test_model_catalog_falls_back_to_builtin_instructions(self):
        with tempfile.TemporaryDirectory() as directory:
            empty_home = Path(directory) / "home"
            empty_home.mkdir()
            with (
                patch.object(
                    MODULE, "model_catalog_path", return_value=Path(directory) / "catalog.json"
                ),
                patch.object(MODULE, "NATIVE_CODEX_HOME", empty_home),
            ):
                messages = MODULE.cached_model_messages()

        self.assertEqual(messages, {"instructions_template": MODULE.FALLBACK_MODEL_INSTRUCTIONS})

    def test_model_catalog_prefers_codex_cached_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "models_cache.json").write_text(
                json.dumps({"models": [{"model_messages": {"instructions_template": "cached"}}]}),
                encoding="utf-8",
            )
            with (
                patch.object(MODULE, "model_catalog_path", return_value=home / "catalog.json"),
                patch.object(MODULE, "NATIVE_CODEX_HOME", home),
            ):
                messages = MODULE.cached_model_messages()

        self.assertEqual(messages, {"instructions_template": "cached"})

    def test_sync_model_catalog_writes_provider_models(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "catalog.json"
            with (
                patch.dict(MODULE.os.environ, {MODULE.MODEL_CATALOG_ENV: str(target)}),
                patch.object(
                    MODULE, "cached_model_messages", return_value={"instructions_template": "prompt"}
                ),
            ):
                path = MODULE.sync_model_catalog([("opencode/a", "Model A", 128000)])

            self.assertEqual(path, target)
            payload = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual([model["slug"] for model in payload["models"]], ["opencode/a"])
        self.assertEqual(payload["models"][0]["context_window"], 128000)

    def test_sync_model_catalog_skips_providers_without_models(self):
        self.assertIsNone(MODULE.sync_model_catalog([]))

    def test_default_model_ignores_model_from_another_provider(self):
        with patch.object(MODULE, "load_switcher_state", return_value={}):
            model = MODULE.default_model_for(
                "opencode",
                'model = "gpt-5.5"\nmodel_provider = "opencode"\n',
                ["opencode/a"],
            )

        self.assertEqual(model, "opencode/a")

    def test_default_model_keeps_current_model_when_provider_serves_it(self):
        with patch.object(MODULE, "load_switcher_state", return_value={}):
            model = MODULE.default_model_for(
                "opencode",
                'model = "opencode/a"\nmodel_provider = "opencode"\n',
                ["opencode/a"],
            )

        self.assertEqual(model, "opencode/a")

    def test_switching_pins_catalog_for_opencode_and_drops_it_for_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('model = "gpt-5.6-luna"\nmodel_provider = "openai"\n', encoding="utf-8")
            catalog = root / "models.json"
            with (
                patch.object(MODULE, "NATIVE_CONFIG_PATH", config),
                patch.object(MODULE, "SWITCHER_STATE_PATH", root / "state.json"),
                patch.object(MODULE, "stop_apps"),
                patch.object(MODULE, "open_apps"),
                patch.object(MODULE, "ensure_opencode_service"),
                patch.object(
                    MODULE,
                    "ensure_opencode_router",
                    return_value=("http://127.0.0.1:20130", "router-token"),
                ),
                patch.object(
                    MODULE,
                    "opencode_router_models",
                    return_value=[("opencode/a", "Model A", 200000)],
                ),
                patch.object(
                    MODULE, "cached_model_messages", return_value={"instructions_template": "prompt"}
                ),
                patch.dict(MODULE.os.environ, {MODULE.MODEL_CATALOG_ENV: str(catalog)}),
            ):
                MODULE.switch_provider("opencode")
                pinned = config.read_text(encoding="utf-8")
                MODULE.switch_provider("openai")
                released = config.read_text(encoding="utf-8")

        self.assertIn(f'model_catalog_json = "{catalog}"', pinned)
        self.assertIn('model_provider = "opencode"', pinned)
        self.assertIn('model = "opencode/a"', pinned)
        self.assertIn("[model_providers.opencode]", pinned)
        self.assertIn('base_url = "http://127.0.0.1:20130/v1"', pinned)
        self.assertEqual(pinned.count("model_catalog_json ="), 1)
        self.assertNotIn("model_catalog_json", released)
        self.assertNotIn("[model_providers.opencode]", released)
        self.assertIn('model_provider = "openai"', released)

    def test_switching_to_opencode_without_models_fails_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('model = "gpt-5.6-luna"\nmodel_provider = "openai"\n', encoding="utf-8")
            with (
                patch.object(MODULE, "NATIVE_CONFIG_PATH", config),
                patch.object(MODULE, "SWITCHER_STATE_PATH", root / "state.json"),
                patch.object(MODULE, "ensure_opencode_service"),
                patch.object(MODULE, "ensure_opencode_router", return_value=("http://127.0.0.1:20130", "t")),
                patch.object(MODULE, "opencode_router_models", return_value=[]),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    MODULE.switch_provider("opencode")

                self.assertIn("OpenCode", str(caught.exception))
                self.assertIn('model_provider = "openai"', config.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
