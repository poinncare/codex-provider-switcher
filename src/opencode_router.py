#!/usr/bin/env python3
"""Local Responses-API router that exposes OpenCode's free models to Codex.

Codex speaks only the OpenAI Responses API and accepts a single provider base
URL, while OpenCode spreads its models over several upstreams that each come
with their own endpoint and credentials.  This module hosts the small loopback
service that closes that gap: it publishes the free models OpenCode itself
offers and forwards every request to the endpoint OpenCode configured for that
model, using OpenCode's own credentials.

Nothing here talks to OmniRoute or any other third-party router; the upstream
of every model is taken from `GET /api/model` on the OpenCode service.

Usage:
    python3 src/opencode_router.py serve [--port 20130]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOME = Path.home()
# Always the user's real Codex home: the launcher strips CODEX_HOME before
# switching providers, and the router must keep running across accounts.
CODEX_HOME = HOME / ".codex"
STATE_PATH = CODEX_HOME / "codex-provider-switcher-router.json"
LOG_PATH = CODEX_HOME / "codex-provider-switcher-router.log"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 20130
HEALTH_SERVICE = "codex-provider-switcher-router"
# Endpoints of the providers OpenCode configures without an explicit base URL.
PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "cerebras": "https://api.cerebras.ai/v1",
}
# Model metadata is re-read at most this often; a miss always triggers a reload.
METADATA_TTL = 30.0
# Loopback hosts are treated as another local gateway (OmniRoute and friends)
# and are never routed by this service.
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


def opencode_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base).expanduser() if base else HOME / ".local/state") / "opencode"


def opencode_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base).expanduser() if base else HOME / ".local/share") / "opencode"


def service_info() -> dict | None:
    """URL and password of the running OpenCode service, if any."""
    try:
        payload = json.loads((opencode_state_dir() / "service.json").read_text(encoding="utf-8"))
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
            # A stale state file outlives a crashed service; its pid is gone.
            return None
    return {"url": url.rstrip("/"), "password": str(payload.get("password") or "")}


def credentials() -> dict:
    """Provider credentials OpenCode stores for its own providers."""
    try:
        payload = json.loads((opencode_data_dir() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result = {}
    for provider_id, entry in (payload or {}).items():
        if isinstance(entry, dict):
            key = entry.get("key") or entry.get("token") or entry.get("access")
            if isinstance(key, str) and key:
                result[str(provider_id)] = key
    return result


def service_get(path: str, timeout: float = 10.0) -> tuple[int, object]:
    info = service_info()
    if not info:
        return 0, None
    request = urllib.request.Request(info["url"] + path)
    if info["password"]:
        token = base64.b64encode(f"opencode:{info['password']}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return 0, None


def service_models() -> list[dict]:
    """Every model OpenCode offers, as reported by the OpenCode service."""
    status, payload = service_get("/api/model")
    if status != 200 or not isinstance(payload, dict):
        return []
    models = (payload.get("data") or {}).get("models") if isinstance(payload.get("data"), dict) else None
    if models is None:
        models = payload.get("data")
    return [model for model in models or [] if isinstance(model, dict)]


def recent_slugs() -> list[str]:
    """Models most recently used in OpenCode, most recent first."""
    try:
        payload = json.loads(
            (opencode_state_dir() / "model.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return []
    slugs = []
    for entry in payload.get("recent") or []:
        if not isinstance(entry, dict):
            continue
        provider_id = entry.get("providerID")
        model_id = entry.get("modelID")
        if provider_id and model_id:
            slugs.append(model_slug({"providerID": provider_id, "modelID": model_id}))
    return slugs


def model_slug(model: dict) -> str:
    """Id OpenCode uses for a model; variants like `-fast` keep their own id."""
    return f"{model.get('providerID')}/{model.get('id') or model.get('modelID')}"


def is_loopback_endpoint(url: str) -> bool:
    host = url.split("//", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1]
    if host.startswith("["):
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    return host.lower() in LOOPBACK_HOSTS


def is_free_model(model: dict) -> bool:
    """True when OpenCode reports zero cost on every pricing tier.

    Codex has no credentials of its own, so a paid model can only fail at
    request time; the list therefore stays on the free tier.
    """
    cost = model.get("cost")
    if not isinstance(cost, list) or not cost:
        return False
    for tier in cost:
        if not isinstance(tier, dict):
            return False
        try:
            if float(tier.get("input", 1)) != 0 or float(tier.get("output", 1)) != 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def has_context_window(model: dict) -> bool:
    """Image-only entries report no context and cannot serve a Codex turn."""
    limit = model.get("limit")
    return (
        isinstance(limit, dict)
        and isinstance(limit.get("context"), int)
        and limit["context"] > 0
    )


def is_offered_model(model: dict) -> bool:
    """Only free models Codex can actually run are published and routed."""
    return (
        model.get("enabled") is not False
        and is_free_model(model)
        and has_context_window(model)
    )


def upstream_for(model: dict) -> dict | None:
    """Where and how to reach one OpenCode model, or None when unavailable."""
    settings = model.get("settings") or {}
    provider_id = str(model.get("providerID") or "")
    base_url = settings.get("baseURL") or PROVIDER_BASE_URLS.get(provider_id)
    if not isinstance(base_url, str) or not base_url:
        return None
    if is_loopback_endpoint(base_url):
        # Another local gateway owns this provider; this service stays out of it.
        return None
    api_key = settings.get("apiKey") or credentials().get(provider_id)
    headers = {
        str(name): str(value)
        for name, value in (model.get("headers") or {}).items()
        if isinstance(name, str)
    }
    if isinstance(api_key, str) and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    variant = model.get("body")
    return {
        "url": base_url.rstrip("/") + "/responses",
        "headers": headers,
        # OpenCode exposes several picker entries per upstream model (`-fast`,
        # `-pro`, ...) that differ only by request body additions.
        "model": str(model.get("modelID") or model.get("id") or ""),
        "body": dict(variant) if isinstance(variant, dict) else {},
    }


def apply_variant(payload: dict, variant: dict) -> dict:
    """Add the variant's request body without dropping the caller's values."""
    result = dict(payload)
    for key, value in variant.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = {**current, **value}
        else:
            result[key] = value
    return result


def catalog_models() -> list[dict]:
    """The model list Codex should offer, ordered like the user's OpenCode."""
    models = service_models()
    routable = {}
    for model in models:
        slug = model_slug(model)
        if not slug or "/" not in slug:
            continue
        if not is_offered_model(model):
            continue
        if upstream_for(model) is None:
            continue
        routable[slug] = model
    ordered = []
    for slug in recent_slugs():
        if slug in routable:
            ordered.append(routable.pop(slug))
    ordered.extend(routable[slug] for slug in sorted(routable))
    return ordered


def catalog_entries() -> list[dict]:
    entries = []
    for model in catalog_models():
        limit = model.get("limit") or {}
        context = limit.get("context") if isinstance(limit, dict) else None
        entries.append(
            {
                "id": model_slug(model),
                "name": str(model.get("name") or model_slug(model)),
                "context_length": context if isinstance(context, int) else None,
            }
        )
    return entries


class ModelIndex:
    """Slug -> upstream mapping, refreshed from OpenCode on demand."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._targets: dict[str, dict] = {}

    def targets(self, refresh: bool = False) -> dict[str, dict]:
        with self._lock:
            stale = time.time() - self._loaded_at > METADATA_TTL
            if refresh or stale or not self._targets:
                targets = {}
                for model in catalog_models():
                    upstream = upstream_for(model)
                    if upstream:
                        targets[model_slug(model)] = upstream
                self._targets = targets
                self._loaded_at = time.time()
            return self._targets

    def resolve(self, slug: str) -> dict | None:
        target = self.targets().get(slug)
        if target is None:
            target = self.targets(refresh=True).get(slug)
        return target


MODELS = ModelIndex()


def source_stamp() -> str:
    """Timestamp of this file, captured when the process starts.

    The launcher compares it with the file on disk and restarts the daemon when
    they differ, so a long-running router never mixes versions.  Reading the
    file per request would defeat that: a stale daemon would keep reporting the
    fresh timestamp.
    """
    try:
        return str(int(Path(__file__).resolve().stat().st_mtime))
    except OSError:
        return ""


SOURCE_STAMP = source_stamp()


def error_payload(message: str, code: str) -> dict:
    return {"error": {"message": message, "type": "invalid_request_error", "code": code}}


class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = HEALTH_SERVICE
    token = ""

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))
        sys.stderr.flush()

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self.reply_json(
                200,
                {"service": HEALTH_SERVICE, "status": "ok", "stamp": SOURCE_STAMP},
            )
            return
        if not self.authorized():
            return
        if path in ("/v1/models", "/models"):
            self.reply_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": entry["id"],
                            "object": "model",
                            "name": entry["name"],
                            "context_length": entry["context_length"],
                            "owned_by": entry["id"].split("/", 1)[0],
                        }
                        for entry in catalog_entries()
                    ],
                },
            )
            return
        self.reply_json(404, error_payload(f"Unknown path: {path}", "not_found"))

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        path = self.path.split("?", 1)[0]
        if not self.authorized():
            return
        if path not in ("/v1/responses", "/responses"):
            self.reply_json(404, error_payload(f"Unknown path: {path}", "not_found"))
            return
        self.forward_responses()

    def authorized(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization") or ""
        if header.strip() == f"Bearer {self.token}":
            return True
        self.reply_json(401, error_payload("Authentication required", "unauthorized"))
        return False

    def read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def reply_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def forward_responses(self) -> None:
        raw = self.read_body()
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self.reply_json(400, error_payload("Request body is not valid JSON", "invalid_json"))
            return
        if not isinstance(payload, dict):
            self.reply_json(400, error_payload("Request body must be an object", "invalid_json"))
            return
        slug = str(payload.get("model") or "")
        target = MODELS.resolve(slug) if slug else None
        if not target:
            self.reply_json(
                404,
                error_payload(
                    f"Model '{slug}' is not routed by OpenCode. Pick a model from the "
                    "/v1/models list.",
                    "model_not_found",
                ),
            )
            return
        payload = apply_variant(payload, target["body"])
        payload["model"] = target["model"]
        headers = {"Content-Type": "application/json", "Accept": self.headers.get("Accept") or "*/*"}
        headers.update(target["headers"])
        request = urllib.request.Request(
            target["url"], data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            upstream = urllib.request.urlopen(request, timeout=600)
        except urllib.error.HTTPError as error:
            self.stream_back(error.code, error.headers.get("Content-Type"), error)
            return
        except (OSError, urllib.error.URLError) as error:
            self.reply_json(502, error_payload(f"Upstream request failed: {error}", "upstream_error"))
            return
        self.stream_back(upstream.status, upstream.headers.get("Content-Type"), upstream)

    def stream_back(self, status: int, content_type: str | None, source) -> None:
        """Relay an upstream response as-is, flushing each chunk."""
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        reader = getattr(source, "read1", None) or source.read
        try:
            while True:
                chunk = reader(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as error:
            self.log_message("upstream stream aborted: %s", error)
        finally:
            source.close()


def router_token() -> str:
    """Reuse the running router's token so Codex config stays valid."""
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        token = payload.get("token")
        if isinstance(token, str) and token:
            return token
    except (OSError, json.JSONDecodeError):
        pass
    return secrets.token_urlsafe(24)


def write_state(server: ThreadingHTTPServer, token: str) -> None:
    host, port = server.server_address[0], server.server_address[1]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {
                "service": HEALTH_SERVICE,
                "url": f"http://{host}:{port}/v1",
                "host": host,
                "port": port,
                "pid": os.getpid(),
                "token": token,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def read_state() -> dict | None:
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def serve(port: int, host: str = DEFAULT_HOST) -> None:
    token = router_token()
    RouterHandler.token = token
    server = ThreadingHTTPServer((host, port), RouterHandler)
    server.daemon_threads = True
    write_state(server, token)
    sys.stderr.write(f"{HEALTH_SERVICE} listening on {server.server_address}\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve", help="Run the router")
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    subparsers.add_parser("catalog", help="Print the model list Codex would offer")
    arguments = parser.parse_args()
    if arguments.command == "catalog":
        print(json.dumps(catalog_entries(), ensure_ascii=False, indent=2))
        return 0
    serve(arguments.port, arguments.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
