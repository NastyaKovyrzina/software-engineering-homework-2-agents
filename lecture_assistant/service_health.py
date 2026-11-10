"""Bounded, read-only health checks for the local lecture assistant services.

The monitor is deliberately caller-driven: invoke :meth:`check` after a
configuration change or from an external monitoring tick. It does not start
services, load models, or run the router inference model.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from lecture_assistant.gateway_config import GATEWAY_URL, PROTOCOL_VERSION, load_gateway_runtime


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 2.0
OLLAMA_URL = "http://127.0.0.1:11434"
CHECK_LABELS = {
    "gateway": "шлюз",
    "ollama_api": "API Ollama",
    "required_models": "обязательные модели Ollama",
    "router_artifact": "артефакт маршрутизатора",
}


class ServiceHealthMonitor:
    """Check gateway, direct Ollama availability, configured tags and router files.

    ``probe`` is an optional test seam with the signature
    ``probe(url, *, headers=None, timeout=2.0) -> dict``. Production requests
    always bypass proxy settings and are capped at two MiB.
    """

    def __init__(self, project_dir, settings, store, probe: Callable | None = None):
        self.project_dir = Path(project_dir).resolve()
        self.settings = dict(settings or {})
        self.store = store
        self.probe = probe or self._probe_json
        self.failure_threshold = self._threshold(self.settings)
        self.timeout = self._timeout(self.settings)
        self.runs_dir = self.project_dir / "runs"
        self.telemetry_dir = self.runs_dir / "telemetry"
        self.state_path = self.telemetry_dir / "health.json"
        self._lock = threading.Lock()

    @staticmethod
    def _threshold(settings):
        value = settings.get("health_failures_before_alert", settings.get("service_health_failure_threshold", 2))
        if type(value) is not int or not 1 <= value <= 100:
            return 2
        return value

    @staticmethod
    def _timeout(settings):
        value = settings.get("health_timeout_seconds", REQUEST_TIMEOUT_SECONDS)
        if type(value) not in (int, float) or value <= 0:
            return REQUEST_TIMEOUT_SECONDS
        return min(float(value), REQUEST_TIMEOUT_SECONDS)

    @staticmethod
    def _probe_json(url, *, headers=None, timeout=REQUEST_TIMEOUT_SECONDS):
        request = urllib.request.Request(url, headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=min(float(timeout), REQUEST_TIMEOUT_SECONDS)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise ValueError("unreachable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid_json") from exc
        if not isinstance(value, dict):
            raise ValueError("invalid_response")
        return value

    def _gateway_runtime_and_health(self):
        try:
            runtime = load_gateway_runtime(self.project_dir)
        except (OSError, ValueError):
            return False, "runtime_unavailable"
        try:
            health = self.probe(f"{GATEWAY_URL}/health", timeout=self.timeout)
        except Exception:
            return False, "health_unreachable"
        valid = (
            health.get("service") == "lecture-model-gateway"
            and health.get("version") == PROTOCOL_VERSION
            and health.get("instance_id") == runtime.get("instance_id")
        )
        return (True, None) if valid else (False, "instance_mismatch")

    def _ollama_version(self):
        try:
            value = self.probe(f"{OLLAMA_URL}/api/version", timeout=self.timeout)
        except Exception:
            return False, "ollama_unreachable"
        if not isinstance(value.get("version"), str) or not value["version"]:
            return False, "invalid_version_response"
        return True, None

    def _required_models(self):
        try:
            records = json.loads((self.project_dir / "models.json").read_text(encoding="utf-8"))
            if not isinstance(records, list) or len(records) != 4:
                return False, "invalid_model_configuration"
            expected = {item["name"] for item in records if isinstance(item, dict) and isinstance(item.get("name"), str)}
            if len(expected) != 4:
                return False, "invalid_model_configuration"
            tags = self.probe(f"{OLLAMA_URL}/api/tags", timeout=self.timeout)
        except Exception:
            return False, "tags_unavailable"
        models = tags.get("models")
        if not isinstance(models, list):
            return False, "invalid_tags_response"
        available = {item.get("name") for item in models if isinstance(item, dict) and isinstance(item.get("name"), str)}
        missing = sorted(expected - available)
        if missing:
            # Do not emit the model names in alerts or health logs.
            return False, "required_model_missing"
        return True, None

    def _router_artifact(self):
        try:
            settings_path = self.project_dir / "router_settings.json"
            router_settings = json.loads(settings_path.read_text(encoding="utf-8"))
            directory = Path(router_settings["directory"])
            manifest = directory / "manifest.json"
            if not manifest.is_file():
                return False, "router_manifest_missing"
            # Validate that this is a readable manifest for the configured source.
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("model_id") != router_settings.get("model_id") or data.get("revision") != router_settings.get("revision"):
                return False, "router_manifest_mismatch"
            files = data.get("files")
            artifact_name = "model.int8.onnx"
            if not isinstance(files, dict) or artifact_name not in files:
                return False, "router_artifact_missing"
            for name in files:
                if not isinstance(name, str) or Path(name).name != name or not (directory / name).is_file():
                    return False, "router_artifact_missing"
        except Exception:
            return False, "router_artifact_unavailable"
        return True, None

    def _collect(self):
        return {
            "gateway": self._gateway_runtime_and_health(),
            "ollama_api": self._ollama_version(),
            "required_models": self._required_models(),
            "router_artifact": self._router_artifact(),
        }

    def _read_state(self):
        try:
            if (self.runs_dir.is_symlink() or not self.runs_dir.resolve().is_relative_to(self.project_dir)
                    or self.telemetry_dir.is_symlink() or not self.telemetry_dir.resolve().is_relative_to(self.runs_dir.resolve())
                    or self.state_path.is_symlink() or not self.state_path.resolve().is_relative_to(self.telemetry_dir.resolve())):
                return {"checks": {}}
            raw = self.state_path.read_bytes()
            if len(raw) > MAX_RESPONSE_BYTES:
                return {"checks": {}}
            state = json.loads(raw)
            return state if isinstance(state, dict) and isinstance(state.get("checks"), dict) else {"checks": {}}
        except (OSError, ValueError, json.JSONDecodeError):
            return {"checks": {}}

    def _write_state(self, state):
        self.telemetry_dir.mkdir(parents=True, exist_ok=True)
        if (self.runs_dir.is_symlink() or not self.runs_dir.resolve().is_relative_to(self.project_dir)
                or self.telemetry_dir.is_symlink() or not self.telemetry_dir.resolve().is_relative_to(self.runs_dir.resolve())):
            raise ValueError("health state path escapes runs/telemetry")
        if self.state_path.is_symlink():
            raise ValueError("health state file must not be a symlink")
        encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(prefix=".health-", suffix=".tmp", dir=self.telemetry_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.state_path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def check(self) -> dict:
        """Perform one bounded monitoring pass, persist it, and return its snapshot."""
        with self._lock:
            raw_checks = self._collect()
            prior = self._read_state()
            prior_checks = prior["checks"]
            pending_alerts = prior.get("pending_alerts", [])
            if not isinstance(pending_alerts, list):
                pending_alerts = []
            pending_alerts = [item for item in pending_alerts if isinstance(item, dict)]
            now = time.time()
            timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")
            next_checks = {}
            public_checks = {}
            transitions = []

            for name, (ok, error) in raw_checks.items():
                old = prior_checks.get(name, {})
                old_state = old.get("state", "up") if isinstance(old, dict) else "up"
                old_failures = old.get("consecutive_failures", 0) if isinstance(old, dict) else 0
                old_transition = old.get("transition_id", 0) if isinstance(old, dict) else 0
                failures = 0 if ok else (old_failures + 1 if type(old_failures) is int else 1)
                state = "up" if ok else ("down" if failures >= self.failure_threshold else "degraded")
                transition_id = old_transition if type(old_transition) is int and old_transition >= 0 else 0
                if state == "down" and old_state != "down":
                    transition_id += 1
                    transitions.append({"name": name, "transition": "down", "transition_id": transition_id})
                elif state == "up" and old_state == "down":
                    transition_id += 1
                    transitions.append({"name": name, "transition": "recovered", "transition_id": transition_id})
                next_checks[name] = {"state": state, "consecutive_failures": failures, "transition_id": transition_id}
                public_checks[name] = {"ok": bool(ok), "state": state, "consecutive_failures": failures}
                if error:
                    public_checks[name]["error"] = error

            states = [item["state"] for item in public_checks.values()]
            overall = "down" if "down" in states else ("degraded" if "degraded" in states else "healthy")
            snapshot = {"timestamp": timestamp, "status": overall, "checks": public_checks}
            # Keep pending alert work durable so a temporary telemetry-store failure
            # cannot consume the only transition notification.
            pending_alerts.extend(transitions)
            state_to_write = {"current": snapshot, "checks": next_checks, "pending_alerts": pending_alerts}
            self._write_state(state_to_write)

            remaining = []
            for pending in pending_alerts:
                name = pending.get("name")
                transition = pending.get("transition")
                transition_id = pending.get("transition_id")
                if name not in CHECK_LABELS or transition not in {"down", "recovered"} or type(transition_id) is not int:
                    continue
                if transition == "down":
                    code = "service_health_down"
                    message = f"Проверка недоступности: {CHECK_LABELS[name]}."
                    severity = "ERROR"
                else:
                    code = "service_health_recovered"
                    message = f"Работа восстановлена: {CHECK_LABELS[name]}."
                    severity = "INFO"
                try:
                    dedup_key = f"{name}:{transition}:{transition_id}"
                    sent = self.store.alert(
                        code, message, severity=severity,
                        dedup_key=dedup_key,
                    )
                    if not sent and hasattr(self.store, "alert_exists"):
                        sent = self.store.alert_exists(code, dedup_key)
                except Exception:
                    sent = False
                if not sent:
                    remaining.append(pending)
            if len(remaining) != len(pending_alerts):
                state_to_write["pending_alerts"] = remaining
                self._write_state(state_to_write)
            return snapshot
