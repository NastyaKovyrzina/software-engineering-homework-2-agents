import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from lecture_assistant.lecture_tools import PROJECT_DIR
from lecture_assistant.result_format import make_outcome, render_outcome


def answered_state():
    quote = "Изоляция ограничивает доступ."
    return {
        "question": "Что такое изоляция?", "status": "answered", "revisions": 0,
        "draft": {"unapproved_text": "НЕПОКАЗЫВАТЬ: непроверенный пересказ"},
        "review": {"approved": True},
        "result": {
            "status": "answered", "answer": f"{quote} [1]",
            "citations": [{"claim_id": "c1", "quote": quote, "source": {
                "file": "lecture.md", "section": "Изоляция", "line_start": 3, "line_end": 3,
                "document_version": "test-version", "fragment_id": "test:0001",
            }}],
            "semantic_verification": {"status": "model_approved", "model": "gemma3:test"},
        },
    }


class ResultFormatTests(unittest.TestCase):
    def test_answer_has_provenance_without_duplicate_quote_or_draft(self):
        outcome = make_outcome(answered_state(), "lecture.md")
        text = render_outcome(outcome)
        self.assertEqual(outcome["status"], "answered")
        self.assertEqual(outcome["sources"][0]["document_version"], "test-version")
        self.assertEqual(text.count("Изоляция ограничивает доступ."), 1)
        self.assertIn("строка 3", text)
        self.assertNotIn("НЕПОКАЗЫВАТЬ", text)
        self.assertTrue(outcome["verification"]["exact_quotes"])
        self.assertTrue(outcome["verification"]["model_review"])
        json.loads(json.dumps(outcome, ensure_ascii=False))

    def test_missing_approval_or_broken_source_is_not_a_ready_answer(self):
        cases = []
        state = answered_state()
        state["review"]["approved"] = False
        cases.append(state)
        state = answered_state()
        state["result"]["citations"][0]["source"]["line_end"] = 0
        cases.append(state)
        state = answered_state()
        state["result"]["citations"] = [None]
        cases.append(state)
        for state in cases:
            with self.subTest(state=state):
                outcome = make_outcome(state, "lecture.md")
                self.assertEqual(outcome["status"], "technical_error")
                self.assertEqual(outcome["reason_code"], "INVALID_FINAL_RESULT")
                self.assertIsNone(outcome["answer"])
                self.assertEqual(outcome["sources"], [])

    def test_all_insufficient_reasons_hide_candidates_and_have_actions(self):
        messages = []
        for reason in ("no_matching_fragments", "writer_abstained", "verification_failed_after_revision"):
            with self.subTest(reason=reason):
                state = answered_state()
                state.update(status="insufficient_information", reason=reason)
                state["result"] = {"status": "insufficient_information", "reason": reason, "answer": "НЕПОКАЗЫВАТЬ: черновик", "citations": state["result"]["citations"]}
                outcome = make_outcome(state, "lecture.md")
                self.assertIsNone(outcome["answer"])
                self.assertEqual(outcome["sources"], [])
                self.assertTrue(outcome["next_steps"])
                self.assertNotIn("НЕПОКАЗЫВАТЬ", render_outcome(outcome))
                self.assertFalse(outcome["verification"]["model_review"])
                messages.append(outcome["message"])
        self.assertEqual(len(set(messages)), 3)

    def test_technical_details_are_only_printed_in_verbose_mode(self):
        state = {"question": "Вопрос", "status": "technical_error", "result": {
            "status": "technical_error", "error": {"code": "MODEL_UNAVAILABLE", "message": "RAW_NETWORK_DETAIL"},
        }}
        outcome = make_outcome(state, "lecture.md")
        self.assertIn("Запустите приложение Ollama", render_outcome(outcome))
        self.assertNotIn("RAW_NETWORK_DETAIL", render_outcome(outcome))
        self.assertIn("RAW_NETWORK_DETAIL", render_outcome(outcome, verbose=True))
        self.assertIn("MODEL_UNAVAILABLE", render_outcome(outcome, verbose=True))

    def test_result_fields_are_the_same_for_all_three_statuses(self):
        success = make_outcome(answered_state(), "lecture.md")
        insufficient = make_outcome({"status": "insufficient_information", "result": {"status": "insufficient_information", "reason": "no_matching_fragments"}}, "lecture.md")
        failed = make_outcome({"status": "technical_error", "result": {"status": "technical_error"}}, "lecture.md")
        self.assertEqual(set(success), set(insufficient))
        self.assertEqual(set(success), set(failed))
        self.assertEqual(success["schema_version"], 1)

    def test_one_revision_is_explained_without_another_success_status(self):
        state = answered_state()
        state["revisions"] = 1
        outcome = make_outcome(state, "lecture.md")
        self.assertEqual(outcome["status"], "answered")
        self.assertIn("одна попытка исправления", render_outcome(outcome))

    def test_inconsistent_or_unfinished_state_is_technical_error(self):
        for state in ({"status": "running"}, {**answered_state(), "status": "technical_error"}):
            self.assertEqual(make_outcome(state, "lecture.md")["reason_code"], "INVALID_FINAL_RESULT")


class CommandOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".format-test-", dir=PROJECT_DIR)
        self.root = Path(self.temporary.name).resolve()
        for name in ("app.py", "lecture_assistant/lecture_graph.py", "lecture_assistant/llm_client.py", "lecture_assistant/lecture_tools.py", "lecture_assistant/result_format.py", "lecture_assistant/sandbox_runtime.py", "lecture_assistant/gateway_config.py", "lecture_assistant/instruction_loader.py", "models.json", "lecture_assistant/router_runner.py", "router_worker.py", "router_settings.json", "lecture_assistant/memory_store.py", "lecture_assistant/telemetry_recorder.py"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture", encoding="utf-8")

    def tearDown(self):
        if self.root == PROJECT_DIR or not self.root.resolve().is_relative_to(PROJECT_DIR):
            raise RuntimeError("Небезопасный путь очистки")
        self.temporary.cleanup()

    def invoke(self, state, save_failure=False):
        graph = SimpleNamespace(stream=lambda *args, **kwargs: iter([copy.deepcopy(state)]))
        output = io.StringIO()
        with patch.object(app, "PROJECT_DIR", self.root), patch.object(app, "OllamaClient"), patch.object(app, "build_graph", return_value=graph), patch("sys.argv", ["app.py", "--lecture", "lecture.md", "--question", state["question"], "--no-memory", "--no-observability"]), redirect_stdout(output):
            if save_failure:
                with patch.object(app, "save_run", side_effect=OSError("RAW_WRITE_FAILURE")):
                    code = app.main()
            else:
                code = app.main()
        return code, output.getvalue()

    def test_saved_json_contains_the_same_public_answer_as_console(self):
        code, output = self.invoke(answered_state())
        self.assertEqual(code, 0)
        records = list((self.root / "runs").glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertIn(record["outcome"]["answer"], output)
        self.assertEqual(record["outcome"]["status"], "answered")

    def test_failed_run_save_is_not_reported_as_success(self):
        code, output = self.invoke(answered_state(), save_failure=True)
        self.assertEqual(code, 1)
        self.assertIn("Техническая ошибка", output)
        self.assertIn("Запись запроса не сохранена", output)
        self.assertNotIn("Результат: Ответ по лекции", output)
        self.assertNotIn("RAW_WRITE_FAILURE", output)
