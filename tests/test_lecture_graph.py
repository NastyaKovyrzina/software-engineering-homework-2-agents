import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lecture_assistant.lecture_graph import DraftOutput, ReviewOutput, SearchOutput, build_graph, initial_state
from lecture_assistant.lecture_tools import PROJECT_DIR, LectureTools, failure
from lecture_assistant.llm_client import ModelError, ModelReply, OllamaClient


class FakeClient:
    """Управляемые ответы для проверки переходов; это не тест качества LLM."""
    def __init__(self):
        self.calls = []
        self.reject_reviews = 0
        self.bad_quotes = 0
        self.abstain = False
        self.query = "изоляция"
        self.review_family = "gemma3"
        self.bad_coverage = False
        self.on_draft = None
        self.heading_first = 0

    def model_name(self, role):
        return f"fake-{role}"

    def family(self, role):
        return self.review_family if role == "review" else "qwen3"

    def route_question(self, question):
        return {"route": "complex" if "объясни" in question.casefold() else "simple", "uncertain": False, "scores": {"simple": 0.8, "complex": 0.2}}

    def chat(self, role, schema, system, context, json_schema=None):
        self.calls.append(role)
        if role == "search":
            data = {"query": self.query}
        elif role in {"simple", "complex"}:
            fragment = next(part for part in context["fragments"] if "Изоляция ограничивает доступ." in part["text"])
            quote = "Изоляция разрешает всё." if self.bad_quotes else "Изоляция ограничивает доступ."
            if self.bad_quotes:
                self.bad_quotes -= 1
            # Приложение должно показать цитату, даже если предложенный пересказ не годится.
            data = {"has_answer": not self.abstain, "claims": [] if self.abstain else [{"text": "Неподтверждённый свободный пересказ.", "fragment_id": fragment["fragment_id"], "quote": quote}]}
            if self.heading_first:
                data["claims"][0]["quote"] = "# Изоляция"
                self.heading_first -= 1
            if self.on_draft:
                self.on_draft()
        else:
            rejected = self.reject_reviews > 0
            self.reject_reviews = max(0, self.reject_reviews - 1)
            data = {"answers_question": True, "reason": "", "evaluations": [{"claim_id": "unknown" if self.bad_coverage else claim["claim_id"], "supported": not rejected, "reason": "Нет подтверждения" if rejected else ""} for claim in context["claims"]]}
        return ModelReply(schema.model_validate(data), {"kind": "model", "role": role, "model": self.model_name(role), "family": self.family(role), "status": "completed"})


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".graph-test-", dir=PROJECT_DIR)
        self.root = Path(self.temporary.name).resolve()
        directory = self.root / "data" / "lectures"
        directory.mkdir(parents=True)
        self.path = directory / "lecture.md"
        self.path.write_text("# Изоляция\nИзоляция ограничивает доступ.\n", encoding="utf-8")
        self.tools = LectureTools("lecture.md", self.root)
        self.client = FakeClient()

    def tearDown(self):
        if self.root == PROJECT_DIR or not self.root.resolve().is_relative_to(PROJECT_DIR):
            raise RuntimeError("Небезопасный путь очистки")
        self.temporary.cleanup()

    def run_graph(self, question="Что такое изоляция?", route="auto"):
        return build_graph(self.tools, self.client).invoke(initial_state(question, route), config={"recursion_limit": 30})

    def test_success_uses_all_tools_and_independent_reviewer(self):
        state = self.run_graph()
        self.assertEqual(state["result"]["status"], "answered")
        self.assertEqual(state["revisions"], 0)
        self.assertEqual(self.client.calls, ["search", "simple", "review"])
        self.assertEqual(state["result"]["citations"][0]["source"]["line_start"], 2)
        self.assertEqual(state["result"]["answer"], "Изоляция ограничивает доступ. [1]")
        self.assertEqual({event["tool"] for event in state["events"] if event["kind"] == "tool"}, set(self.tools.TOOL_NAMES))

    def test_empty_search_abstains_without_writer(self):
        self.client.query = "фотосинтез"
        state = self.run_graph("Что такое фотосинтез?")
        self.assertEqual(state["result"]["status"], "insufficient_information")
        self.assertEqual(self.client.calls, ["search"])
        self.assertFalse((self.root / "results").exists())

    def test_writer_can_abstain(self):
        self.client.abstain = True
        self.assertEqual(self.run_graph()["result"]["status"], "insufficient_information")
        self.assertNotIn("review", self.client.calls)

    def test_bad_quote_is_revised_before_review(self):
        self.client.bad_quotes = 1
        state = self.run_graph()
        self.assertEqual(state["status"], "answered")
        self.assertEqual(state["revisions"], 1)
        self.assertEqual(self.client.calls, ["search", "simple", "simple", "review"])

    def test_reviewer_can_request_one_revision(self):
        self.client.reject_reviews = 1
        state = self.run_graph()
        self.assertEqual(state["status"], "answered")
        self.assertEqual(state["revisions"], 1)
        self.assertEqual(self.client.calls.count("review"), 2)

    def test_heading_only_answer_is_revised_before_review(self):
        self.client.heading_first = 1
        state = self.run_graph()
        self.assertEqual(state["status"], "answered")
        self.assertEqual(state["revisions"], 1)
        self.assertEqual(self.client.calls.count("review"), 1)
        self.assertNotIn("##", state["result"]["answer"])

    def test_repeated_rejection_ends_without_saving_answer(self):
        self.client.reject_reviews = 20
        state = self.run_graph()
        self.assertEqual(state["status"], "insufficient_information")
        self.assertEqual(state["revisions"], 1)
        self.assertEqual(self.client.calls.count("simple"), 2)
        self.assertFalse((self.root / "results").exists())

    def test_repeated_bad_citations_end_without_review(self):
        self.client.bad_quotes = 20
        self.assertEqual(self.run_graph()["status"], "insufficient_information")
        self.assertNotIn("review", self.client.calls)

    def test_network_error_is_not_missing_information(self):
        with patch.object(self.client, "chat", side_effect=ModelError("MODEL_UNAVAILABLE", "Нет связи")):
            state = self.run_graph()
        self.assertEqual(state["status"], "technical_error")
        self.assertEqual(state["result"]["error"]["code"], "MODEL_UNAVAILABLE")

    def test_family_conflict_rejected(self):
        for family in ("qwen3", "qwen35", "qwen2"):
            with self.subTest(family=family):
                self.client.review_family = family
                state = self.run_graph()
                self.assertEqual(state["error"]["code"], "REVIEWER_FAMILY_CONFLICT")
                self.assertNotIn("review", self.client.calls)

    def test_review_must_cover_all_claim_ids(self):
        self.client.bad_coverage = True
        self.assertEqual(self.run_graph()["error"]["code"], "MODEL_INVALID_OUTPUT")

    def test_changed_lecture_is_technical_error(self):
        self.client.on_draft = lambda: self.path.write_text("Другой текст", encoding="utf-8")
        state = self.run_graph()
        self.assertEqual(state["error"]["code"], "STALE_DOCUMENT")
        self.assertNotIn("review", self.client.calls)

    def test_save_failure_does_not_report_approved_answer(self):
        original = self.tools.call_tool
        with patch.object(self.tools, "call_tool", side_effect=lambda name, args: failure("IO_ERROR", "Ошибка записи") if name == "save_result" else original(name, args)):
            state = self.run_graph()
        self.assertEqual(state["status"], "technical_error")
        self.assertEqual(state["result"]["citations"], [])

    def test_manual_and_automatic_complex_route(self):
        self.assertEqual(self.run_graph(route="complex")["writer_role"], "complex")
        self.assertEqual(self.run_graph("Объясни, что такое изоляция?")["writer_role"], "complex")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = OllamaClient(PROJECT_DIR / "models.json")
        self.client._info[self.client.model_name("search")] = {"digest": "test", "details": {"family": "qwen2", "quantization_level": "Q4_K_M"}}

    def run_response(self, response):
        endpoints = []

        def request(endpoint, payload=None, timeout=30):
            endpoints.append(endpoint)
            if endpoint == "ps":
                return {"models": []}
            if endpoint == "chat":
                if isinstance(response, Exception):
                    raise response
                return response
            return {"done": True}

        with patch.object(self.client, "_request", side_effect=request):
            try:
                return self.client.chat("search", SearchOutput, "system", {"question": "вопрос"})
            finally:
                self.assertIn("generate", endpoints)

    def test_valid_structured_response(self):
        reply = self.run_response({"done": True, "done_reason": "stop", "message": {"content": json.dumps({"query": "изоляция"})}})
        self.assertEqual(reply.parsed.query, "изоляция")
        self.assertTrue(reply.event["unloaded"])

    def test_invalid_json_wrong_shape_and_truncation_are_errors(self):
        cases = [
            ({"done": True, "done_reason": "stop", "message": {"content": "not JSON"}}, "MODEL_INVALID_OUTPUT"),
            ({"done": True, "done_reason": "stop", "message": None}, "MODEL_INVALID_RESPONSE"),
            ({"done": True, "done_reason": "length", "message": {"content": "{}"}}, "MODEL_INCOMPLETE_OUTPUT"),
        ]
        for response, code in cases:
            with self.subTest(code=code), self.assertRaises(ModelError) as caught:
                self.run_response(response)
            self.assertEqual(caught.exception.code, code)

    def test_timeout_still_unloads(self):
        with self.assertRaises(ModelError) as caught:
            self.run_response(ModelError("MODEL_TIMEOUT", "Тайм-аут"))
        self.assertEqual(caught.exception.code, "MODEL_TIMEOUT")

    def test_unload_failure_stops_further_work(self):
        def request(endpoint, payload=None, timeout=30):
            if endpoint == "ps":
                return {"models": []}
            if endpoint == "generate":
                raise ModelError("MODEL_TIMEOUT", "Ошибка выгрузки")
            return {"done": True, "done_reason": "stop", "message": {"content": '{"query":"изоляция"}'}}
        with patch.object(self.client, "_request", side_effect=request), self.assertRaises(ModelError) as caught:
            self.client.chat("search", SearchOutput, "system", {})
        self.assertEqual(caught.exception.code, "MODEL_UNLOAD_FAILED")
        self.assertFalse(caught.exception.event["unloaded"])

    def test_busy_model_is_not_unloaded_by_client(self):
        with patch.object(self.client, "_request", return_value={"models": [{"name": "user-model"}]}) as request:
            with self.assertRaises(ModelError) as caught:
                self.client.chat("search", SearchOutput, "system", {})
        self.assertEqual(caught.exception.code, "MODEL_BUSY")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["ps"])

    def test_invalid_model_list_is_normalized_to_model_error(self):
        with patch.object(self.client, "_request", return_value={"models": None}), self.assertRaises(ModelError) as caught:
            self.client.chat("search", SearchOutput, "system", {})
        self.assertEqual(caught.exception.code, "MODEL_INVALID_RESPONSE")


if __name__ == "__main__":
    unittest.main()
