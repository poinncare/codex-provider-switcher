import importlib.util
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

    def test_provider_block_includes_auth_subsection(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "opencode.jsonc"
            config.write_text(
                """
                {
                  "provider": {
                    "local": {
                      "name": "Local Gateway",
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
        self.assertIn('api_key = "secret-key"', tables)
        block = MODULE.provider_block_for(tables, "[model_providers.opencode-local]")
        self.assertIn('api_key = "secret-key"', block)

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
