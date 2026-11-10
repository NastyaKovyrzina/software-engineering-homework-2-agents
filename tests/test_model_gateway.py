"""Unit tests for the restricted gateway and Sandboxie profile planner."""

from __future__ import annotations

import json
import threading
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import configure_sandbox
from lecture_assistant.llm_client import ModelError
from model_gateway import Gateway, GatewayError, GatewayServer, OPTIONS


class FakeUpstream:
    def __init__(self):
        self.specs = {
            role: SimpleNamespace(name=f"{role}-model:latest", disable_thinking=role in {"simple", "complex"})
            for role in ("search", "simple", "complex", "review")
        }
        self.tags = [{"name": spec.name, "digest": f"digest-{role}"}
                     for role, spec in self.specs.items()]
        self.tags.append({"name": "unapproved:latest", "digest": "outside"})
        self.ps: list[dict] = []
        self.calls: list[tuple] = []
        self.chat_error: ModelError | None = None

    def _models(self, endpoint):
        self.calls.append(("models", endpoint))
        return list(self.tags if endpoint == "tags" else self.ps)

    def _request(self, endpoint, payload=None, timeout=30):
        self.calls.append((endpoint, payload, timeout))
        if endpoint == "show":
            return {"details": {"family": "fake"}, "capabilities": ["completion"]}
        if endpoint == "chat":
            if self.chat_error:
                raise self.chat_error
            return {"message": {"content": '{"ok":true}'}, "done": True}
        if endpoint == "generate":
            return {"done": True, "done_reason": "unload"}
        raise AssertionError(f"Unexpected upstream endpoint {endpoint}")


def chat_payload(model: str, *, options=None, schema=None):
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "format": schema if schema is not None else {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        "stream": False,
        "keep_alive": 0,
        "options": dict(OPTIONS) if options is None else options,
    }


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.gateway = Gateway(self.upstream)

    def test_four_configured_models_and_allowed_operations(self):
        for spec in self.upstream.specs.values():
            self.assertEqual(self.gateway.dispatch("POST", "/api/show", {"model": spec.name})["details"]["family"], "fake")
            self.assertEqual(self.gateway.dispatch("POST", "/api/chat", chat_payload(spec.name))["message"]["content"], '{"ok":true}')
            self.assertEqual(self.gateway.dispatch("POST", "/api/generate", {"model": spec.name, "keep_alive": 0})["done_reason"], "unload")
        listed = self.gateway.dispatch("GET", "/api/tags")["models"]
        self.assertEqual({item["name"] for item in listed}, self.gateway.allowed)
        self.assertNotIn("unapproved:latest", {item["name"] for item in listed})
        self.assertEqual(self.gateway.dispatch("GET", "/api/ps"), {"models": []})

    def test_denies_unknown_models_and_mutating_endpoints(self):
        for path in ("/api/show", "/api/chat", "/api/generate"):
            with self.subTest(path=path), self.assertRaises(GatewayError) as caught:
                self.gateway.dispatch("POST", path, {"model": "unknown:latest", "keep_alive": 0})
            self.assertEqual(caught.exception.status, 403)
        for path in ("/api/pull", "/api/create", "/api/delete", "/api/unknown"):
            with self.subTest(path=path), self.assertRaises(GatewayError) as caught:
                self.gateway.dispatch("POST", path, {"model": "simple-model:latest"})
            self.assertEqual(caught.exception.code, "GATEWAY_ENDPOINT_NOT_ALLOWED")

    def test_rejects_image_and_tool_parameters(self):
        for extra in ({"images": ["data"]}, {"tools": [{"type": "function"}]}):
            payload = chat_payload("simple-model:latest")
            payload.update(extra)
            with self.subTest(extra=extra), self.assertRaises(GatewayError) as caught:
                self.gateway.dispatch("POST", "/api/chat", payload)
            self.assertEqual(caught.exception.code, "GATEWAY_INVALID_REQUEST")
        self.assertFalse(any(call[0] == "chat" for call in self.upstream.calls))

    def test_requires_fixed_cpu_options(self):
        payload = chat_payload("simple-model:latest")
        payload["options"]["num_gpu"] = 1
        with self.assertRaises(GatewayError):
            self.gateway.dispatch("POST", "/api/chat", payload)
        accepted = self.gateway.dispatch("POST", "/api/chat", chat_payload("simple-model:latest"))
        self.assertEqual(accepted["message"]["content"], '{"ok":true}')
        sent = next(call[1] for call in self.upstream.calls if call[0] == "chat")
        self.assertEqual(sent["options"], OPTIONS)

    def test_busy_and_foreign_loaded_models_are_never_unloaded(self):
        self.gateway.lock.acquire()
        try:
            with self.assertRaises(GatewayError) as caught:
                self.gateway.dispatch("POST", "/api/chat", chat_payload("simple-model:latest"))
            self.assertEqual(caught.exception.code, "GATEWAY_BUSY")
        finally:
            self.gateway.lock.release()
        self.upstream.ps = [{"name": "someone-elses-model:latest"}]
        with self.assertRaises(GatewayError) as caught:
            self.gateway.dispatch("POST", "/api/chat", chat_payload("simple-model:latest"))
        self.assertEqual(caught.exception.code, "MODEL_BUSY")
        self.assertFalse(any(call[0] == "generate" for call in self.upstream.calls))

    def test_unloads_gateway_model_after_success_and_model_error(self):
        self.gateway.dispatch("POST", "/api/chat", chat_payload("simple-model:latest"))
        unloads = [call for call in self.upstream.calls if call[0] == "generate"]
        self.assertEqual(len(unloads), 1)
        self.assertEqual(unloads[0][1], {"model": "simple-model:latest", "keep_alive": 0})

        self.upstream.chat_error = ModelError("MODEL_TIMEOUT", "fake timeout")
        with self.assertRaises(ModelError) as caught:
            self.gateway.dispatch("POST", "/api/chat", chat_payload("complex-model:latest"))
        self.assertEqual(caught.exception.code, "MODEL_TIMEOUT")
        unloads = [call for call in self.upstream.calls if call[0] == "generate"]
        self.assertEqual(len(unloads), 2)
        self.assertIsNone(self.gateway.active_model)
        self.assertFalse(self.gateway.lock.locked())

    def test_rejects_json_schema_with_external_ref(self):
        schema = {"type": "object", "properties": {"value": {"$ref": "https://example.invalid/schema.json"}}}
        with self.assertRaises(GatewayError) as caught:
            self.gateway.dispatch("POST", "/api/chat", chat_payload("simple-model:latest", schema=schema))
        self.assertEqual(caught.exception.code, "GATEWAY_INVALID_REQUEST")
        self.assertFalse(any(call[0] == "chat" for call in self.upstream.calls))

    def test_http_auth_endpoint_restrictions_and_filtered_tags(self):
        token = "t" * 64
        server = GatewayServer(("127.0.0.1", 0), self.gateway, token, "i" * 32)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path, *, method="GET", auth=True, body=None):
            data = None if body is None else json.dumps(body).encode("utf-8")
            headers = {}
            if auth:
                headers["Authorization"] = f"Bearer {token}"
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = Request(base + path, data=data, headers=headers, method=method)
            try:
                with urlopen(req, timeout=3) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as exc:
                return exc.code, json.loads(exc.read())

        try:
            self.assertEqual(request("/api/tags", auth=False)[0], 401)
            status, _ = request("/api/pull", method="POST", body={"model": "x"})
            self.assertEqual(status, 403)
            status, body = request("/api/tags")
            self.assertEqual(status, 200)
            self.assertEqual({item["name"] for item in body["models"]}, self.gateway.allowed)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_sandbox_plan_is_idempotent_and_rejects_extra_network_allow(self):
        rules = configure_sandbox.required_rules(root=configure_sandbox.ROOT, user=configure_sandbox.Path("C:/Users/test"), python_home=configure_sandbox.Path("C:/Python"))
        initial = [f"[{configure_sandbox.BOX_NAME}]"]
        additions = configure_sandbox.plan_changes(initial, rules)
        self.assertEqual(len(additions), len(rules))
        updated = initial + [f"{item['key']}={item['value']}" for item in additions]
        self.assertEqual(configure_sandbox.plan_changes(updated, rules), [])
        with self.assertRaisesRegex(RuntimeError, "дополнительные сетевые разрешения"):
            configure_sandbox.plan_changes(updated + ["NetworkAccess=python.exe,Allow"], rules)


if __name__ == "__main__":
    unittest.main()
