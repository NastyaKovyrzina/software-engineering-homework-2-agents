import unittest
from unittest.mock import patch

import test_lecture_graph as fixtures
from lecture_assistant.lecture_graph import build_graph, initial_state, requests_calendar_year, contains_year_evidence, DraftOutput


class AnswerRequirementsTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown

    def test_explicit_year_request_without_date_stops_before_writer(self):
        state = build_graph(self.tools, self.client).invoke(initial_state("В каком году появилась изоляция?", "complex"))
        self.assertEqual(state["status"], "insufficient_information")
        self.assertEqual(state["reason"], "requested_year_not_found")
        self.assertEqual(self.client.calls, ["search"])

    def test_year_in_source_does_not_allow_answer_without_year(self):
        self.path.write_text("# Изоляция\nИзоляция ограничивает доступ.\nТехнология появилась в 1995 году.\n", encoding="utf-8")
        state = build_graph(self.tools, self.client).invoke(initial_state("В каком году появилась изоляция?", "complex"))
        self.assertEqual(state["status"], "insufficient_information")
        self.assertEqual(state["revisions"], 1)
        self.assertNotIn("review", self.client.calls)

    def test_answer_with_year_still_requires_independent_review(self):
        quote = "Технология появилась в 1995 году."
        self.path.write_text("# Изоляция\nИзоляция ограничивает доступ.\n" + quote + "\n", encoding="utf-8")
        original = self.client.chat
        def chat(role, schema, system, context, **kwargs):
            reply = original(role, schema, system, context, **kwargs)
            if role in {"simple", "complex"}:
                reply.parsed = DraftOutput.model_validate({"has_answer":True,"claims":[{"text":quote,"quote":quote,"fragment_id":context["fragments"][0]["fragment_id"]}]})
            return reply
        with patch.object(self.client,"chat",side_effect=chat):
            state = build_graph(self.tools,self.client).invoke(initial_state("В каком году появилась изоляция?","complex"))
        self.assertEqual(state["status"],"answered")
        self.assertIn("review",self.client.calls)

    def test_written_and_numeric_years_and_other_question_types(self):
        for text in ("В 962 году", "В 1995 году", "В 99-м году", "В две тысячи двадцать первом году"):
            self.assertTrue(contains_year_evidence(text), text)
        self.assertFalse(contains_year_evidence("Песочница ограничивает доступ к файлам."))
        self.assertTrue(requests_calendar_year("В каком году появилась технология?"))
        self.assertFalse(requests_calendar_year("Что запускает виртуальная машина?"))
