import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from lecture_assistant.telemetry_recorder import NoopRecorder, TelemetryRecorder


class TelemetryRecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, attrs=None):
        return TelemetryRecorder(self.root, uuid.uuid4().hex, attributes=attrs)

    def records(self, recorder):
        return [json.loads(line) for line in recorder.path.read_text(encoding="utf-8").splitlines()]

    def test_trace_and_nested_span_parentage_and_finish_summary(self):
        rec = self.make({"session_id": "s1"})
        self.assertIsNone(rec.error)
        with rec.span("outer", "TOOL", {"n": 1}) as outer:
            with rec.span("inner") as inner:
                inner.update({"done": True})
                inner.event("inside")
            outer.update({"ok": True})
        rec.finish("success", {"corrections_used": 2, "routing": {"route": "fast"}})
        rec.finish("ignored")
        rows = self.records(rec)
        self.assertEqual(rows[0]["event"], "trace_start")
        self.assertEqual(rows[0]["trace_id"], rec.run_id)
        self.assertEqual(rows[-1]["event"], "trace_end")
        self.assertEqual(rows[-1]["attributes"]["app.revisions"], 2)
        self.assertEqual(rows[-1]["attributes"]["app.route"], "fast")
        spans = [r for r in rows if r["event"] == "span_start"]
        self.assertEqual(spans[0]["parent_id"], rec.root_span_id)
        self.assertEqual(spans[1]["parent_id"], spans[0]["span_id"])
        self.assertTrue(all(len(r["span_id"]) == 16 for r in spans))

    def test_exception_marks_error_and_reraises(self):
        rec = self.make()
        with self.assertRaisesRegex(RuntimeError, "failed"):
            with rec.span("bad") as span:
                span.set_error("failed", "E7")
                raise RuntimeError("failed")
        rec.finish("error")
        rows = self.records(rec)
        end = next(r for r in rows if r["event"] == "span_end")
        self.assertEqual(end["status"], "ERROR")
        self.assertEqual(end["attributes"]["error.code"], "E7")
        self.assertTrue(any(r["event"] == "log" and r["level"] == "ERROR" for r in rows))

    def test_sensitive_keys_redacted_and_large_values_stay_valid_json(self):
        rec = self.make({"Authorization": "Bearer abc", "nested": {"api_key": "secret"}})
        with rec.span("payload", attributes={"text": "x" * 800000}):
            pass
        rec.finish("ok")
        data = rec.path.read_bytes()
        rows = [json.loads(line) for line in data.splitlines()]
        self.assertTrue(all(len(line) <= 512 * 1024 for line in data.splitlines()))
        self.assertEqual(rows[0]["attributes"]["Authorization"], "[REDACTED]")
        self.assertEqual(rows[0]["attributes"]["nested"]["api_key"], "[REDACTED]")

    def test_duplicate_run_id_fails_open_without_overwriting(self):
        run_id = uuid.uuid4().hex
        first = TelemetryRecorder(self.root, run_id)
        first.finish("ok")
        original = first.path.read_bytes()
        second = TelemetryRecorder(self.root, run_id)
        self.assertIsNotNone(second.error)
        second.event("ignored")
        self.assertEqual(first.path.read_bytes(), original)

    def test_bad_run_id_and_unwritable_project_disable_recording(self):
        bad = TelemetryRecorder(self.root, "../bad")
        self.assertIsNotNone(bad.error)
        with bad.span("noop"):
            pass
        broken = TelemetryRecorder(self.root / "missing", uuid.uuid4().hex)
        self.assertIsNotNone(broken.error)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlinked_runs_escape_is_rejected(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        try:
            (self.root / "runs").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        rec = TelemetryRecorder(self.root, uuid.uuid4().hex)
        self.assertIsNotNone(rec.error)
        self.assertFalse(list(outside.rglob("*.jsonl")))

    def test_noop_api(self):
        rec = NoopRecorder()
        with rec.span("anything") as span:
            span.update({"ok": True})
            span.event("ignored")
        rec.event("ignored")
        rec.finish("ok")
        self.assertIsNone(rec.error)
        self.assertIsNone(rec.path)


if __name__ == "__main__":
    unittest.main()
