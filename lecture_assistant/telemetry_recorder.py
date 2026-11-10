"""Best-effort, local JSONL telemetry for lecture-assistant runs."""
from __future__ import annotations

import atexit
import contextvars
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_MAX_LINE = 512 * 1024
_SENSITIVE = re.compile(r"authorization|api.?key|access.?token|refresh.?token|client.?secret|password|^token$", re.I)
_CURRENT = contextvars.ContextVar("telemetry_current_span", default=None)


def _safe(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value
    return str(value)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class Span:
    def __init__(self, recorder=None, span_id=None, parent_id=None, name="", kind="CHAIN", attributes=None):
        self._recorder, self.span_id, self.parent_id = recorder, span_id, parent_id
        self.name, self.kind = name, kind
        self.attributes = dict(attributes or {})
        self.status = "OK"

    def update(self, attributes: dict) -> None:
        if isinstance(attributes, dict):
            self.attributes.update(attributes)

    def event(self, name, attributes=None, level="INFO") -> None:
        if self._recorder:
            self._recorder.event(name, attributes, level)

    def set_error(self, message, code=None) -> None:
        self.status = "ERROR"
        self.attributes["error.message"] = str(message)
        if code is not None:
            self.attributes["error.code"] = str(code)
        if self._recorder:
            attrs = {"error.message": str(message)}
            if code is not None:
                attrs["error.code"] = str(code)
            self._recorder.event("span_error", attrs, "ERROR")


class TelemetryRecorder:
    def __init__(self, project_dir, run_id, *, attributes=None):
        self.run_id = run_id
        self.trace_id = run_id if isinstance(run_id, str) and re.fullmatch(r"[0-9a-fA-F]{32}", run_id) else None
        self.root_span_id = hashlib.sha256((str(run_id) + ":root").encode()).hexdigest()[:16]
        self.path = None
        self.error = None
        self._lock = threading.RLock()
        self._stream = None
        self._finished = False
        self._warned = False
        self._resource_stop = threading.Event()
        self._resource_thread = None
        self._resource = {"memory.peak_rss_mib": None, "memory.system_used_mib_start": None,
                          "memory.system_used_mib_end": None, "cpu.seconds": None}
        self._process = None
        self._peak_rss = None
        self._cpu_start = None
        self._root_attributes = dict(attributes or {})
        try:
            if not self.trace_id:
                raise ValueError("run_id must be 32 hexadecimal characters")
            root = Path(project_dir).resolve(strict=True)
            runs = root / "runs"
            if not _inside(runs.resolve(), root):
                raise ValueError("telemetry runs path escapes project directory")
            runs.mkdir(exist_ok=True)
            spool = runs / "telemetry" / "spool"
            if not _inside(spool.resolve(), runs.resolve(strict=True)):
                raise ValueError("telemetry spool path escapes project runs directory")
            spool.mkdir(parents=True, exist_ok=True)
            resolved_runs, resolved_spool = runs.resolve(strict=True), spool.resolve(strict=True)
            if not _inside(resolved_runs, root) or not _inside(resolved_spool, resolved_runs):
                raise ValueError("telemetry spool path escapes project runs directory")
            self.path = resolved_spool / (run_id + ".jsonl")
            # Exclusive creation prevents accidental trace overwrite.
            self._stream = self.path.open("x", encoding="utf-8", newline="\n")
            self._start_resources()
            self._write({"v": 1, "event": "trace_start", "run_id": run_id,
                         "trace_id": self.trace_id, "span_id": self.root_span_id,
                         "name": "lecture_request", "kind": "CHAIN", "ts_ns": time.time_ns(),
                         "pid": os.getpid(), "attributes": self._root_attributes})
            atexit.register(self._atexit_finish)
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc):
        self.error = str(exc)
        with self._lock:
            if self._stream:
                try: self._stream.close()
                except Exception: pass
                self._stream = None
        self._resource_stop.set()
        if not self._warned:
            self._warned = True
            try: print(f"telemetry disabled: {self.error}", file=sys.stderr)
            except Exception: pass

    def _write(self, record):
        if self._stream is None:
            return
        try:
            record.setdefault("run_id", self.run_id)
            record = _safe(record)
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
            if len(line.encode("utf-8")) > _MAX_LINE:
                # Retain identifiers and schema; clip text values in attributes.
                attrs = record.get("attributes")
                if isinstance(attrs, dict):
                    clipped = {}
                    for key, value in attrs.items():
                        clipped[key] = (value[:2048] + "…[truncated]") if isinstance(value, str) and len(value) > 2048 else value
                    record["attributes"] = clipped
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
                if len(line.encode("utf-8")) > _MAX_LINE:
                    record["attributes"] = {"telemetry.truncated": True}
                    line = json.dumps(record, separators=(",", ":"))
            with self._lock:
                if self._stream is not None:
                    self._stream.write(line + "\n")
                    self._stream.flush()
        except Exception as exc:
            self._fail(exc)

    def _start_resources(self):
        try:
            import psutil  # optional dependency
            self._process = psutil.Process(os.getpid())
            self._cpu_start = self._cpu_seconds()
            self._sample_resources("start")
            self._resource_thread = threading.Thread(target=self._sample_loop, name="telemetry-sampler", daemon=True)
            self._resource_thread.start()
        except Exception:
            self._process = None

    def _cpu_seconds(self):
        try:
            t = self._process.cpu_times()
            return float(t.user + t.system)
        except Exception:
            return None

    def _sample_resources(self, phase):
        try:
            import psutil
            vm = psutil.virtual_memory()
            if phase == "start": self._resource["memory.system_used_mib_start"] = (vm.total - vm.available) / 1048576
            else: self._resource["memory.system_used_mib_end"] = (vm.total - vm.available) / 1048576
            rss = self._process.memory_info().rss / 1048576
            self._peak_rss = max(rss, self._peak_rss or rss)
        except Exception:
            pass

    def _sample_loop(self):
        while not self._resource_stop.wait(0.5):
            self._sample_resources("sample")

    def _parent(self):
        current = _CURRENT.get()
        if isinstance(current, tuple) and current[0] is self:
            return current[1]
        return self.root_span_id

    @contextmanager
    def span(self, name, kind="CHAIN", attributes=None):
        if self._stream is None:
            yield Span()
            return
        span_id, parent_id = uuid.uuid4().hex[:16], self._parent()
        span = Span(self, span_id, parent_id, name, kind, attributes)
        self._write({"v": 1, "event": "span_start", "trace_id": self.trace_id, "span_id": span_id,
                     "parent_id": parent_id, "name": name, "kind": kind, "ts_ns": time.time_ns(),
                     "attributes": span.attributes})
        token = _CURRENT.set((self, span_id))
        try:
            yield span
        except BaseException as exc:
            span.status = "ERROR"
            span.attributes["exception.type"] = type(exc).__name__
            span.attributes.setdefault("error.message", str(exc))
            span.event("span_error", {"exception.type": type(exc).__name__, "error.message": str(exc),
                                       **({"error.code": getattr(exc, "code")} if getattr(exc, "code", None) is not None else {})}, "ERROR")
            raise
        finally:
            _CURRENT.reset(token)
            self._write({"v": 1, "event": "span_end", "trace_id": self.trace_id, "span_id": span_id,
                         "ts_ns": time.time_ns(), "status": span.status, "attributes": span.attributes})

    def event(self, name, attributes=None, level="INFO"):
        self._write({"v": 1, "event": "log", "trace_id": self.trace_id,
                     "span_id": self._parent(), "ts_ns": time.time_ns(), "name": name,
                     "level": level, "attributes": attributes or {}})

    def finish(self, status, outcome=None):
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self._resource_stop.set()
        if self._resource_thread and self._resource_thread is not threading.current_thread():
            self._resource_thread.join(timeout=0.75)
        self._sample_resources("end")
        cpu_now = self._cpu_seconds()
        if self._cpu_start is not None and cpu_now is not None:
            self._resource["cpu.seconds"] = max(0.0, cpu_now - self._cpu_start)
        self._resource["memory.peak_rss_mib"] = self._peak_rss
        attrs = {"app.status": status}
        if isinstance(outcome, dict):
            if "corrections_used" in outcome: attrs["app.revisions"] = outcome["corrections_used"]
            routing = outcome.get("routing")
            if isinstance(routing, dict): attrs["app.route"] = routing.get("route")
            elif routing is not None: attrs["app.route"] = getattr(routing, "route", None)
            code = outcome.get("reason_code")
            if code is None and isinstance(outcome.get("error"), dict): code = outcome["error"].get("code")
            if code is not None: attrs["app.error_code"] = code
            writer = outcome.get("writer_model")
            if writer is not None: attrs["app.writer_model"] = writer
            attrs["outcome"] = outcome
        attrs.update(self._resource)
        self._write({"v": 1, "event": "trace_end", "trace_id": self.trace_id,
                     "span_id": self.root_span_id, "ts_ns": time.time_ns(),
                     "status": status, "attributes": attrs})
        with self._lock:
            if self._stream:
                try: self._stream.close()
                except Exception as exc: self._fail(exc)
                self._stream = None

    def _atexit_finish(self):
        if not self._finished:
            self.finish("interrupted")


class NoopRecorder:
    trace_id = None
    root_span_id = None
    path = None
    error = None

    @contextmanager
    def span(self, name, kind="CHAIN", attributes=None):
        yield Span()

    def event(self, name, attributes=None, level="INFO"):
        return None

    def finish(self, status, outcome=None):
        return None
