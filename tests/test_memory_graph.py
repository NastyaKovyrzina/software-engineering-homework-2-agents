import unittest
from unittest.mock import patch

import test_lecture_graph as fixtures
from lecture_assistant.lecture_graph import build_graph, initial_state
from lecture_assistant.memory_store import MemoryStore


class MemoryGraphTests(unittest.TestCase):
    def setUp(self):
        fixtures.GraphTests.setUp(self)
        self.memory = MemoryStore(self.root)
        self.run_number = 0

    def tearDown(self):
        fixtures.GraphTests.tearDown(self)

    def run_memory_graph(self, question="Что такое изоляция?", session="study"):
        self.run_number += 1
        run_id = f"memory-{self.run_number}"
        state = build_graph(self.tools, self.client, memory=self.memory,
                            session_id=session, run_id=run_id).invoke(
            initial_state(question), config={"recursion_limit": 30})
        return run_id, state

    def promote(self, run_id, state):
        citations = []
        for citation in state["result"]["citations"]:
            checked = self.tools.call_tool("get_quote", {
                "fragment_id": citation["source"]["fragment_id"], "quote": citation["quote"]})
            self.assertTrue(checked["ok"])
            citations.append({"quote": checked["data"]["quote"], "source": checked["data"]["source"]})
        self.memory.complete(run_id, state, validated_citations=citations)

    def test_recent_questions_are_session_and_version_scoped_and_facts_cross_sessions(self):
        run_id, first = self.run_memory_graph()
        self.assertEqual(first["result"]["status"], "answered")
        self.promote(run_id, first)

        _, other_session = self.run_memory_graph(session="other")
        load = next(e for e in other_session["events"] if e.get("kind") == "memory" and e.get("action") == "load_session")
        retrieve = next(e for e in other_session["events"] if e.get("kind") == "memory" and e.get("action") == "retrieve")
        self.assertEqual(load["recent_questions"], 0)
        self.assertGreaterEqual(retrieve["facts_revalidated"], 1)

        self.path.write_text("# Изоляция\nИзоляция ограничивает доступ. Новая редакция.\n", encoding="utf-8")
        _, changed = self.run_memory_graph(session="study")
        load = next(e for e in changed["events"] if e.get("kind") == "memory" and e.get("action") == "load_session")
        retrieve = next(e for e in changed["events"] if e.get("kind") == "memory" and e.get("action") == "retrieve")
        self.assertEqual(load["recent_questions"], 0)
        self.assertFalse(retrieve["cache_hit"])
        self.assertEqual(retrieve["facts_revalidated"], 0)

    def test_recent_questions_are_passed_to_search_writer_and_reviewer(self):
        run_id, first = self.run_memory_graph("Что такое изоляция?")
        self.promote(run_id, first)
        contexts = {}
        original_chat = self.client.chat

        def capture_chat(role, schema, system, context, json_schema=None):
            contexts[role] = context
            return original_chat(role, schema, system, context, json_schema=json_schema)

        with patch.object(self.client, "chat", side_effect=capture_chat):
            _, second = self.run_memory_graph("Как работает изоляция?")
        self.assertEqual(second["result"]["status"], "answered")
        for role in ("search", "simple", "review"):
            self.assertEqual(contexts[role]["previous_questions"], ["Что такое изоляция?"])

    def test_repeated_question_uses_cache_but_still_revalidates_and_calls_models(self):
        _, first = self.run_memory_graph()
        self.assertEqual(first["result"]["status"], "answered")
        self.client.calls.clear()
        _, repeated = self.run_memory_graph()
        retrieve = next(e for e in repeated["events"] if e.get("kind") == "memory" and e.get("action") == "retrieve")
        self.assertTrue(retrieve["cache_hit"])
        self.assertTrue(any(e.get("kind") == "tool" and e.get("tool") == "get_quote" for e in repeated["events"]))
        self.assertEqual(self.client.calls, ["search", "simple", "review"])

    def test_abstained_answer_is_not_promoted(self):
        self.client.abstain = True
        run_id, state = self.run_memory_graph()
        self.assertEqual(state["result"]["status"], "insufficient_information")
        self.memory.complete(run_id, state, validated_citations=[])
        document = self.tools.call_tool("read_lecture", {})["data"]
        self.assertEqual(self.memory.recall("Что такое изоляция?", document), [])

    def test_only_last_three_questions_enter_session_memory(self):
        for i in range(4):
            run_id, state = self.run_memory_graph(f"Вопрос изоляция номер {i}?")
            self.assertEqual(state["result"]["status"], "answered")
            self.promote(run_id, state)
        _, latest = self.run_memory_graph("Новый вопрос об изоляции?")
        load = next(e for e in latest["events"] if e.get("kind") == "memory" and e.get("action") == "load_session")
        self.assertEqual(load["recent_questions"], 3)


if __name__ == "__main__":
    unittest.main()
