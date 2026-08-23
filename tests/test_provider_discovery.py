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


if __name__ == "__main__":
    unittest.main()
