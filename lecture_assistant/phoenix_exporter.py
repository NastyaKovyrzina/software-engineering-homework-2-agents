"""Перенос завершённых спанов в локальный Phoenix через стандартный OTLP/HTTP."""
import json
import logging

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags, TraceState


class PhoenixExporter:
    def __init__(self):
        logging.getLogger("opentelemetry.exporter").setLevel(logging.CRITICAL)
        self.exporter = OTLPSpanExporter(endpoint="http://127.0.0.1:6006/v1/traces", timeout=3)
        self.resource = Resource.create({"service.name": "lecture-assistant", "openinference.project.name": "lecture-assistant"})

    @staticmethod
    def context(trace_id, span_id):
        return SpanContext(int(trace_id, 16), int(span_id, 16), False, TraceFlags(1), TraceState())

    def export(self, rows):
        spans = []
        for row in rows:
            attributes = {}
            for key, value in row["attributes"].items():
                if value is None:
                    continue
                attributes[key] = value if type(value) in (str, int, float, bool) else json.dumps(value, ensure_ascii=False)
            kind = row["kind"]
            attributes["openinference.span.kind"] = kind if kind in {"LLM", "TOOL", "CHAIN", "AGENT", "RETRIEVER", "RERANKER", "EMBEDDING", "GUARDRAIL", "EVALUATOR"} else "CHAIN"
            attributes["app.span_kind"] = kind
            attributes["app.run_id"] = row["run_id"]
            if row.get("parent_id"):
                parent = self.context(row["trace_id"], row["parent_id"])
            else:
                parent = None
                attributes["input.value"] = attributes.get("question", "")
                attributes["session.id"] = attributes.get("session_id", row["run_id"])
            error = row["status"] in {"ERROR", "technical_error", "interrupted"}
            spans.append(ReadableSpan(
                name=row["name"], context=self.context(row["trace_id"], row["span_id"]), parent=parent,
                resource=self.resource, attributes=attributes, kind=SpanKind.INTERNAL,
                status=Status(StatusCode.ERROR if error else StatusCode.OK),
                start_time=int(row["start_ns"]), end_time=int(row["end_ns"]),
                instrumentation_scope=InstrumentationScope("lecture-assistant.manual", "1.0.0"),
            ))
        if not spans:
            return True
        return self.exporter.export(spans) == SpanExportResult.SUCCESS

    def close(self):
        self.exporter.shutdown()
