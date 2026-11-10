import json
import unittest
from uuid import uuid4
from unittest.mock import patch

import test_lecture_graph as fixtures
from lecture_assistant.lecture_graph import build_graph, initial_state
from lecture_assistant.llm_client import ModelError
from lecture_assistant.telemetry_recorder import TelemetryRecorder
from lecture_assistant.telemetry_store import TelemetryStore


class TelemetryGraphTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown

    def test_real_graph_records_review_revision_and_imports_every_event(self):
        self.client.reject_reviews = 1
        run_id = uuid4().hex
        recorder = TelemetryRecorder(self.root, run_id, attributes={"question": "Что такое изоляция?"})
        state = build_graph(self.tools, self.client, telemetry=recorder).invoke(initial_state("Что такое изоляция?"))
        recorder.finish(state["status"], {"corrections_used": state["revisions"]})
        events = [json.loads(line) for line in recorder.path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(event["run_id"] == run_id for event in events))
        store = TelemetryStore(self.root)
        self.assertEqual(store.scan_spool(), len(events))
        self.assertEqual(store.scan_spool(), 0)
        detail = store.detail(run_id)
        self.assertEqual(detail["trace"]["status"], "answered")
        self.assertEqual(detail["trace"]["attributes"]["app.revisions"], 1)
        self.assertTrue(any(log["name"] == "review_feedback" for log in detail["logs"]))
        spans = {span["span_id"]: span for span in detail["spans"]}
        models = [span for span in spans.values() if span["kind"] == "LLM"]
        self.assertEqual(len(models), 5)
        self.assertTrue(all(spans[span["parent_id"]]["kind"] == "CHAIN" for span in models))
        self.assertFalse(any("llm.token_count.prompt" in span["attributes"] for span in models))

    def test_failed_model_preserves_available_token_counts(self):
        original = self.client.chat
        def chat(role, *args, **kwargs):
            if role == "simple":
                raise ModelError("MODEL_INVALID_OUTPUT", "Неверный JSON", {"kind": "model", "role": role, "status": "error", "prompt_tokens": 5, "output_tokens": 3})
            return original(role, *args, **kwargs)
        recorder = TelemetryRecorder(self.root, uuid4().hex)
        with patch.object(self.client, "chat", side_effect=chat):
            state = build_graph(self.tools, self.client, telemetry=recorder).invoke(initial_state("Что такое изоляция?"))
        recorder.finish(state["status"])
        store = TelemetryStore(self.root)
        store.scan_spool()
        failed = next(span for span in store.detail(recorder.trace_id)["spans"] if span["name"] == "simple:answer")
        self.assertEqual(state["status"], "technical_error")
        self.assertEqual(failed["status"], "ERROR")
        self.assertEqual(failed["attributes"]["llm.token_count.prompt"], 5)
        self.assertEqual(failed["attributes"]["llm.token_count.completion"], 3)

    def test_unavailable_telemetry_does_not_stop_graph(self):
        recorder = TelemetryRecorder(self.root / "missing-directory", uuid4().hex)
        self.assertTrue(recorder.error)
        state = build_graph(self.tools, self.client, telemetry=recorder).invoke(initial_state("Что такое изоляция?"))
        recorder.finish(state["status"])
        self.assertEqual(state["status"], "answered")
