import contextlib
import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).parents[1] / "src/opencode_router.py"
SPEC = importlib.util.spec_from_file_location("opencode_router", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        type(self).received = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(self.rfile.read(length) or b"{}"),
        }
        body = b'data: {"type": "response.completed"}\n\n'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StubIndex:
    def __init__(self, targets):
        self._targets = targets

    def resolve(self, slug):
        return self._targets.get(slug)


@contextlib.contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port, path, payload=None, token="tok"):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )


class RouterModelTests(unittest.TestCase):
    def test_variant_ids_stay_distinct_slugs(self):
        variants = [
            {"providerID": "openai", "id": "gpt-6-pro", "modelID": "gpt-6"},
            {"providerID": "openai", "id": "gpt-6-fast", "modelID": "gpt-6"},
        ]
        self.assertEqual(
            [MODULE.model_slug(model) for model in variants],
            ["openai/gpt-6-pro", "openai/gpt-6-fast"],
        )

    def test_local_gateway_endpoints_are_recognised(self):
        self.assertTrue(MODULE.is_loopback_endpoint("http://localhost:20128/v1"))
        self.assertTrue(MODULE.is_loopback_endpoint("http://127.0.0.1:20130/v1"))
        self.assertTrue(MODULE.is_loopback_endpoint("http://[::1]:8080/v1"))
        self.assertFalse(MODULE.is_loopback_endpoint("https://api.openai.com/v1"))
        self.assertFalse(MODULE.is_loopback_endpoint("https://opencode.ai/inference/v1"))

    def test_local_gateway_providers_are_not_routed(self):
        model = {
            "providerID": "kiro",
            "id": "kiro/claude-sonnet-4.5",
            "modelID": "kiro/claude-sonnet-4.5",
            "settings": {"baseURL": "http://localhost:20128/v1"},
        }
        self.assertIsNone(MODULE.upstream_for(model))

    def test_disabled_models_are_not_offered(self):
        model = {
            "providerID": "openai",
            "id": "gpt-6",
            "modelID": "gpt-6",
            "enabled": False,
            "cost": [{"input": 0, "output": 0}],
            "limit": {"context": 400000},
        }
        self.assertFalse(MODULE.is_offered_model(model))

    def test_paid_models_are_not_offered(self):
        model = {
            "providerID": "openai",
            "id": "gpt-6",
            "modelID": "gpt-6",
            "cost": [{"input": 2, "output": 10}, {"tier": {"type": "context", "size": 272000}, "input": 4, "output": 15}],
            "limit": {"context": 400000},
        }
        self.assertFalse(MODULE.is_offered_model(model))

    def test_models_without_a_context_window_are_not_offered(self):
        model = {
            "providerID": "openai",
            "id": "gpt-image-1.5",
            "modelID": "gpt-image-1.5",
            "cost": [{"input": 0, "output": 0}],
            "limit": {"context": 0},
        }
        self.assertFalse(MODULE.is_offered_model(model))

    def test_free_models_with_context_are_offered(self):
        model = {
            "providerID": "opencode",
            "id": "free-model",
            "modelID": "free-model",
            "cost": [{"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}],
            "limit": {"context": 200000},
            "settings": {"baseURL": "https://opencode.ai/inference/v1", "apiKey": "public"},
        }
        self.assertTrue(MODULE.is_offered_model(model))

    def test_models_without_pricing_are_not_offered(self):
        model = {"providerID": "opencode", "id": "free-model", "modelID": "free-model"}
        self.assertFalse(MODULE.is_offered_model(model))

    def test_provider_defaults_and_stored_credentials_are_used(self):
        model = {"providerID": "openai", "id": "gpt-6", "modelID": "gpt-6", "settings": {}}
        with patch.object(MODULE, "credentials", return_value={"openai": "sk-stored"}):
            upstream = MODULE.upstream_for(model)

        self.assertEqual(upstream["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(upstream["headers"]["Authorization"], "Bearer sk-stored")
        self.assertEqual(upstream["model"], "gpt-6")

    def test_model_settings_win_over_provider_defaults(self):
        model = {
            "providerID": "opencode",
            "id": "free-model",
            "modelID": "free-model",
            "settings": {"baseURL": "https://opencode.ai/inference/v1", "apiKey": "public"},
        }
        upstream = MODULE.upstream_for(model)

        self.assertEqual(upstream["url"], "https://opencode.ai/inference/v1/responses")
        self.assertEqual(upstream["headers"]["Authorization"], "Bearer public")

    def test_provider_headers_are_forwarded_to_the_upstream(self):
        model = {
            "providerID": "cerebras",
            "id": "qwen-3.8-27b",
            "modelID": "qwen-3.8-27b",
            "headers": {"X-Cerebras-3rd-Party-Integration": "opencode"},
        }
        with patch.object(MODULE, "credentials", return_value={"cerebras": "csk-key"}):
            upstream = MODULE.upstream_for(model)

        self.assertEqual(
            upstream["headers"]["X-Cerebras-3rd-Party-Integration"], "opencode"
        )
        self.assertEqual(upstream["headers"]["Authorization"], "Bearer csk-key")

    def test_variant_body_is_merged_without_dropping_request_fields(self):
        payload = {"model": "openai/gpt-6-pro", "reasoning": {"effort": "high"}, "input": "hi"}

        merged = MODULE.apply_variant(
            payload, {"reasoning": {"mode": "pro"}, "service_tier": "priority"}
        )

        self.assertEqual(merged["reasoning"], {"effort": "high", "mode": "pro"})
        self.assertEqual(merged["service_tier"], "priority")
        self.assertEqual(merged["input"], "hi")

    def test_catalog_is_ordered_by_recent_use_and_skips_paid_and_local_models(self):
        models = [
            {
                "providerID": "openai",
                "id": "gpt-6",
                "modelID": "gpt-6",
                "name": "GPT-6",
                "cost": [{"input": 2, "output": 10}],
                "limit": {"context": 400000},
            },
            {
                "providerID": "kiro",
                "id": "kiro/claude-sonnet-4.5",
                "modelID": "kiro/claude-sonnet-4.5",
                "settings": {"baseURL": "http://localhost:20128/v1"},
            },
            {
                "providerID": "opencode",
                "id": "free-model",
                "modelID": "free-model",
                "name": "Free Model",
                "cost": [{"input": 0, "output": 0}],
                "limit": {"context": 200000},
                "settings": {"baseURL": "https://opencode.ai/inference/v1", "apiKey": "public"},
            },
            {
                "providerID": "opencode",
                "id": "other-free",
                "modelID": "other-free",
                "name": "Other Free",
                "cost": [{"input": 0, "output": 0}],
                "limit": {"context": 128000},
                "settings": {"baseURL": "https://opencode.ai/inference/v1", "apiKey": "public"},
            },
        ]
        with (
            patch.object(MODULE, "service_models", return_value=models),
            patch.object(MODULE, "credentials", return_value={"openai": "sk-stored"}),
            patch.object(MODULE, "recent_slugs", return_value=["opencode/free-model"]),
        ):
            entries = MODULE.catalog_entries()

        self.assertEqual(
            [entry["id"] for entry in entries],
            ["opencode/free-model", "opencode/other-free"],
        )
        self.assertEqual(entries[0]["name"], "Free Model")
        self.assertEqual(entries[1]["context_length"], 128000)


class RouterHttpTests(unittest.TestCase):
    def router(self, index=None, catalog=None, token="tok"):
        handler = type("Handler", (MODULE.RouterHandler,), {"token": token})
        patches = [patch.object(MODULE, "MODELS", StubIndex(index or {}))]
        if catalog is not None:
            patches.append(patch.object(MODULE, "catalog_entries", return_value=catalog))
        return patches, ThreadingHTTPServer(("127.0.0.1", 0), handler)

    def test_responses_request_is_forwarded_with_model_and_variant(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
        with running(upstream) as upstream_server:
            port = upstream_server.server_address[1]
            index = {
                "openai/gpt-6-pro": {
                    "url": f"http://127.0.0.1:{port}/v1/responses",
                    "headers": {"Authorization": "Bearer upstream-key"},
                    "model": "gpt-6",
                    "body": {"service_tier": "priority"},
                }
            }
            patches, server = self.router(index)
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with running(server) as router_server:
                    with urllib.request.urlopen(
                        request(
                            router_server.server_address[1],
                            "/v1/responses",
                            {"model": "openai/gpt-6-pro", "input": "hi", "stream": True},
                        ),
                        timeout=10,
                    ) as response:
                        body = response.read()

        self.assertEqual(body, b'data: {"type": "response.completed"}\n\n')
        received = FakeUpstreamHandler.received
        self.assertEqual(received["path"], "/v1/responses")
        self.assertEqual(received["authorization"], "Bearer upstream-key")
        self.assertEqual(received["body"]["model"], "gpt-6")
        self.assertEqual(received["body"]["service_tier"], "priority")
        self.assertEqual(received["body"]["input"], "hi")

    def test_models_are_listed_with_names_and_context_windows(self):
        catalog = [
            {"id": "opencode/free-model", "name": "Free Model", "context_length": 200000}
        ]
        patches, server = self.router(catalog=catalog)
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with running(server) as router_server:
                with urllib.request.urlopen(
                    request(router_server.server_address[1], "/v1/models"), timeout=10
                ) as response:
                    payload = json.load(response)

        self.assertEqual(payload["data"][0]["id"], "opencode/free-model")
        self.assertEqual(payload["data"][0]["name"], "Free Model")
        self.assertEqual(payload["data"][0]["context_length"], 200000)

    def test_unrouted_models_get_a_clear_error(self):
        patches, server = self.router({})
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with running(server) as router_server:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(
                        request(
                            router_server.server_address[1],
                            "/v1/responses",
                            {"model": "kiro/kiro/claude-sonnet-4.5", "input": "hi"},
                        ),
                        timeout=10,
                    )
                payload = json.load(caught.exception)
                code = caught.exception.code
                caught.exception.close()

        self.assertEqual(code, 404)
        self.assertEqual(payload["error"]["code"], "model_not_found")

    def test_requests_without_the_launcher_token_are_rejected(self):
        patches, server = self.router(catalog=[])
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with running(server) as router_server:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(
                        request(router_server.server_address[1], "/v1/models", token=None),
                        timeout=10,
                    )
                code = caught.exception.code
                caught.exception.close()

        self.assertEqual(code, 401)

    def test_health_endpoint_is_open_and_reports_the_startup_stamp(self):
        patches, server = self.router(catalog=[])
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            stack.enter_context(patch.object(MODULE, "SOURCE_STAMP", "startup-stamp"))
            with running(server) as router_server:
                with urllib.request.urlopen(
                    request(router_server.server_address[1], "/health", token=None), timeout=10
                ) as response:
                    payload = json.load(response)

        self.assertEqual(payload["service"], MODULE.HEALTH_SERVICE)
        # The stamp is frozen at startup: a running daemon must not claim to be
        # newer than the code it loaded, or the launcher would never restart it.
        self.assertEqual(payload["stamp"], "startup-stamp")


if __name__ == "__main__":
    unittest.main()
