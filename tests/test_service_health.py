import json
import tempfile
import unittest
from pathlib import Path

from lecture_assistant.gateway_config import GATEWAY_URL, PROTOCOL_VERSION
from lecture_assistant.service_health import OLLAMA_URL, ServiceHealthMonitor


class AlertStore:
    def __init__(self):
        self.events = []
        self.results = []

    def alert(self, code, message, run_id=None, severity="WARNING", dedup_key=None):
        result = self.results.pop(0) if self.results else True
        if result:
            self.events.append({"code": code, "message": message, "severity": severity, "dedup_key": dedup_key})
        return result


class ServiceHealthMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.models = [
            {"role_id": role, "name": f"{role}:latest"}
            for role in ("search", "simple", "complex", "review")
        ]
        (self.root / "models.json").write_text(json.dumps(self.models), encoding="utf-8")
        self.router_dir = self.root / "router"
        self.router_dir.mkdir()
        (self.router_dir / "model.int8.onnx").write_bytes(b"test artifact")
        (self.root / "router_settings.json").write_text(json.dumps({
            "directory": str(self.router_dir),
            "model_id": "local/router",
            "revision": "abc123",
        }), encoding="utf-8")
        (self.router_dir / "manifest.json").write_text(json.dumps({
            "model_id": "local/router", "revision": "abc123", "files": {"model.int8.onnx": "test"}
        }), encoding="utf-8")
        self.instance_id = "a" * 32
        (self.root / ".gateway.json").write_text(json.dumps({
            "version": PROTOCOL_VERSION,
            "base_url": GATEWAY_URL,
            "token": "b" * 64,
            "instance_id": self.instance_id,
            "pid": 42,
            "allowed_models": sorted(model["name"] for model in self.models),
        }), encoding="utf-8")
        self.responses = {
            f"{GATEWAY_URL}/health": {
                "service": "lecture-model-gateway",
                "version": PROTOCOL_VERSION,
                "instance_id": self.instance_id,
            },
            f"{OLLAMA_URL}/api/version": {"version": "0.5.0"},
            f"{OLLAMA_URL}/api/tags": {"models": [{"name": item["name"]} for item in self.models]},
        }
        self.store = AlertStore()
        self.monitor = ServiceHealthMonitor(self.root, {"health_failures_before_alert": 2}, self.store, self.probe)

    def tearDown(self):
        self.temp.cleanup()

    def probe(self, url, *, headers=None, timeout=2.0):
        self.assertLessEqual(timeout, 2.0)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response

    def test_healthy_check_persists_a_serializable_snapshot(self):
        result = self.monitor.check()

        self.assertEqual(result["status"], "healthy")
        self.assertTrue(all(check["ok"] for check in result["checks"].values()))
        state = json.loads((self.root / "runs" / "telemetry" / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(state["current"], result)
        self.assertEqual(self.store.events, [])

    def test_alerts_after_threshold_then_recovers_once(self):
        self.responses[f"{OLLAMA_URL}/api/version"] = OSError("private detail must not escape")
        first = self.monitor.check()
        self.assertEqual(first["checks"]["ollama_api"]["state"], "degraded")
        self.assertEqual(self.store.events, [])

        second = self.monitor.check()
        self.assertEqual(second["checks"]["ollama_api"]["state"], "down")
        self.assertEqual([event["code"] for event in self.store.events], ["service_health_down"])
        self.assertNotIn("private detail", json.dumps(second))

        self.monitor.check()
        self.assertEqual(len(self.store.events), 1)

        self.responses[f"{OLLAMA_URL}/api/version"] = {"version": "0.5.0"}
        recovered = self.monitor.check()
        self.assertEqual(recovered["checks"]["ollama_api"]["state"], "up")
        self.assertEqual([event["code"] for event in self.store.events], ["service_health_down", "service_health_recovered"])
        self.assertNotEqual(self.store.events[0]["dedup_key"], self.store.events[1]["dedup_key"])

        self.monitor.check()
        self.assertEqual(len(self.store.events), 2)

    def test_missing_required_ollama_tag_is_reported_without_model_name(self):
        self.responses[f"{OLLAMA_URL}/api/tags"] = {"models": [{"name": self.models[0]["name"]}]}

        result = self.monitor.check()

        check = result["checks"]["required_models"]
        self.assertFalse(check["ok"])
        self.assertEqual(check["error"], "required_model_missing")
        self.assertNotIn(self.models[-1]["name"], json.dumps(result))

    def test_configured_failure_threshold_controls_first_alert(self):
        monitor = ServiceHealthMonitor(self.root, {"health_failures_before_alert": 1}, self.store, self.probe)
        self.responses[f"{OLLAMA_URL}/api/version"] = OSError("offline")

        result = monitor.check()

        self.assertEqual(result["checks"]["ollama_api"]["state"], "down")
        self.assertEqual([event["code"] for event in self.store.events], ["service_health_down"])

    def test_failed_alert_delivery_is_retried_with_same_dedup_key(self):
        self.responses[f"{OLLAMA_URL}/api/version"] = OSError("offline")
        self.store.results = [False, True]
        self.monitor.check()
        self.monitor.check()
        persisted = json.loads((self.root / "runs" / "telemetry" / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(len(persisted["pending_alerts"]), 1)

        self.monitor.check()

        self.assertEqual(len(self.store.events), 1)
        self.assertEqual(self.store.events[0]["code"], "service_health_down")
        persisted = json.loads((self.root / "runs" / "telemetry" / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["pending_alerts"], [])

    def test_gateway_must_match_current_runtime_instance(self):
        self.responses[f"{GATEWAY_URL}/health"]["instance_id"] = "c" * 32

        result = self.monitor.check()

        self.assertFalse(result["checks"]["gateway"]["ok"])
        self.assertEqual(result["checks"]["gateway"]["error"], "instance_mismatch")


if __name__ == "__main__":
    unittest.main()
