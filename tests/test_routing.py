import subprocess
import unittest
from unittest.mock import patch

import test_lecture_graph as fixtures
from lecture_assistant.llm_client import ModelError
from model_gateway import Gateway, GatewayError
from test_model_gateway import FakeUpstream


class RoutingTests(unittest.TestCase):
    setUp = fixtures.GraphTests.setUp
    tearDown = fixtures.GraphTests.tearDown
    run_graph = fixtures.GraphTests.run_graph

    def test_auto_uses_model_decision_instead_of_old_keyword_rule(self):
        with patch.object(self.client, "route_question", return_value={"route": "complex", "uncertain": False}) as route:
            state = self.run_graph()
        route.assert_called_once_with("Что такое изоляция?")
        self.assertEqual(state["writer_role"], "complex")
        self.assertEqual(state["routing"]["source"], "nli_classifier")

    def test_manual_route_does_not_call_classifier(self):
        with patch.object(self.client, "route_question", side_effect=AssertionError("Не должен вызываться")):
            self.assertEqual(self.run_graph(route="simple")["routing"]["source"], "manual")

    def test_router_unavailable_and_uncertain_use_complex(self):
        with patch.object(self.client, "route_question", side_effect=ModelError("ROUTER_UNAVAILABLE", "Нет весов")):
            state = self.run_graph()
        self.assertEqual(state["writer_role"], "complex")
        self.assertEqual(state["routing"]["source"], "safe_fallback")
        with patch.object(self.client, "route_question", return_value={"route": "simple", "uncertain": True}):
            self.assertEqual(self.run_graph()["writer_role"], "complex")

    def test_missing_simple_model_falls_back_to_complex(self):
        original = self.client.chat
        def chat(role, *args, **kwargs):
            if role == "simple":
                raise ModelError("MODEL_NOT_INSTALLED", "Нет маленькой модели")
            return original(role, *args, **kwargs)
        with patch.object(self.client, "chat", side_effect=chat):
            state = self.run_graph()
        self.assertEqual(state["status"], "answered")
        self.assertEqual(state["writer_role"], "complex")
        self.assertEqual(state["routing"]["writer_fallback"], "simple_to_complex")

    def test_missing_complex_model_and_reviewer_stop_without_downgrade(self):
        original = self.client.chat
        for missing in ("complex", "review"):
            def chat(role, *args, **kwargs):
                if role == missing:
                    raise ModelError("MODEL_NOT_INSTALLED", "Нет модели")
                return original(role, *args, **kwargs)
            with self.subTest(missing=missing), patch.object(self.client, "chat", side_effect=chat):
                state = self.run_graph(route="complex")
                self.assertEqual(state["status"], "technical_error")
                self.assertEqual(state["result"]["citations"], [])
                self.assertNotIn("simple", self.client.calls)

    def test_missing_search_model_uses_original_question(self):
        original = self.client.chat
        def chat(role, *args, **kwargs):
            if role == "search":
                raise ModelError("MODEL_NOT_INSTALLED", "Нет поисковой модели")
            return original(role, *args, **kwargs)
        with patch.object(self.client, "chat", side_effect=chat):
            state = self.run_graph()
        self.assertEqual(state["status"], "answered")
        self.assertEqual(state["search_query"], state["question"])
        self.assertTrue(any(event["kind"] == "degradation" for event in state["events"]))


class RouterBoundaryTests(unittest.TestCase):
    def test_malformed_worker_decisions_are_recoverable_errors(self):
        from lecture_assistant.router_runner import run_router
        for output in ('[]', 'null', '{"route":[],"scores":{}}', '{"route":"simple","scores":null}', '{"route":"simple","scores":["simple","complex"]}'):
            with self.subTest(output=output), patch("lecture_assistant.router_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")), self.assertRaises(RuntimeError):
                run_router("Вопрос?")

    def test_gateway_route_accepts_only_question_and_does_not_call_chat(self):
        upstream = FakeUpstream()
        gateway = Gateway(upstream)
        decision = {"route": "simple", "scores": {"simple": .7, "complex": .3}, "uncertain": False}
        with patch("lecture_assistant.router_runner.run_router", return_value=decision) as worker:
            self.assertEqual(gateway.dispatch("POST", "/api/route", {"question": "Вопрос?"}), decision)
            worker.assert_called_once_with("Вопрос?")
            with self.assertRaises(GatewayError):
                gateway.dispatch("POST", "/api/route", {"question": "Вопрос?", "command": "cmd.exe"})
        self.assertFalse(any(call[0] == "chat" for call in upstream.calls))

    def test_worker_timeout_is_a_recoverable_router_error(self):
        from lecture_assistant.router_runner import run_router
        with patch("lecture_assistant.router_runner.subprocess.run", side_effect=subprocess.TimeoutExpired("router", 60)), self.assertRaises(RuntimeError):
            run_router("Вопрос?")
