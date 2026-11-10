"""Ограниченный граф поиска, ответа и независимой проверки по одной лекции."""

from __future__ import annotations

import hashlib
import json
import operator
import re
import time
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from lecture_assistant.lecture_tools import LectureTools, ToolError, checked_text
from lecture_assistant.llm_client import ModelError
from lecture_assistant.instruction_loader import InstructionError, InstructionSnapshot
from lecture_assistant.memory_store import MemoryStoreError
from lecture_assistant.telemetry_recorder import NoopRecorder

MAX_REVISIONS = 1


class StrictOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class SearchOutput(StrictOutput):
    query: str = Field(min_length=1, max_length=200)


class Claim(StrictOutput):
    quote: str = Field(min_length=1, max_length=700)
    fragment_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=400, description="Предложенная формулировка ответа; не название раздела")


class DraftOutput(StrictOutput):
    claims: list[Claim] = Field(max_length=3)
    has_answer: bool


class Evaluation(StrictOutput):
    claim_id: str = Field(min_length=1, max_length=10)
    reason: str = Field(max_length=300)
    supported: bool


class ReviewOutput(StrictOutput):
    answers_question: bool = Field(description="True, если answer_text прямо содержит все сведения, запрошенные в текущем question; false, если хотя бы одного запрошенного сведения нет")
    reason: str = Field(max_length=400, description="По-русски: какие слова дают ответ при true либо каких запрошенных сведений не хватает при false")
    evaluations: list[Evaluation] = Field(min_length=1, max_length=3)


class GraphState(TypedDict, total=False):
    question: str
    requested_route: str
    status: str
    document: dict
    search_query: str
    fragments: list[dict]
    writer_role: str
    writer_model: str
    writer_family: str
    routing: dict
    memory_context: dict
    draft: dict
    validated: list[dict]
    feedback: list[str]
    review: dict | None
    revisions: int
    reason: str
    result: dict
    error: dict | None
    events: Annotated[list[dict], operator.add]


class WorkflowError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def initial_state(question: str, route: str = "auto") -> dict:
    return {"question": question, "requested_route": route, "status": "running", "events": [], "revisions": 0, "feedback": [], "validated": [], "review": None, "error": None}


def family_group(family: str) -> str:
    # Ollama использует разные имена архитектур qwen2/qwen3/qwen35 для одного семейства Qwen.
    normalized = family.casefold()
    return next((group for group in ("qwen", "gemma") if normalized.startswith(group)), normalized)


def requests_calendar_year(question: str) -> bool:
    """Узкая проверка явно запрошенного года, без классификации всех временных вопросов."""
    return bool(re.search(r"\b(?:каком|какого|какой|котором|которого)\s+год(?:у|а)?\b|\bгод\s+(?:создания|появления|основания|выпуска|публикации|изобретения)\b", question.casefold()))


def contains_year_evidence(text: str) -> bool:
    # Это необходимый признак, а не доказательство, что дата относится к нужному событию.
    normalized = text.casefold()
    if re.search(r"\b[1-9]\d{3}\b|\b\d{1,4}(?:[-‑–][а-я]+)?\s*г(?:од(?:у|а|ом)?|\.)", normalized):
        return True
    year_word = re.search(r"\bгод(?:у|а|ом)?\b", normalized)
    written_number = re.search(r"\b(?:тысяч\w*|двухтысяч\w*|девятисот\w*|девятьсот|восемьсот|семьсот|шестьсот|пятьсот|четыреста|триста|двести|сто|двадцат\w*|тридцат\w*|сороков\w*|пятидесят\w*|шестидесят\w*|семидесят\w*|восьмидесят\w*|девяност\w*|перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*|шест\w*|седьм\w*|восьм\w*|девят\w*|десят\w*)\b", normalized.replace("ё", "е"))
    return bool(year_word and written_number)


def build_graph(tools: LectureTools, client, progress=None, *, instructions_dir=None, memory=None, session_id=None, run_id=None, telemetry=None):
    """Один экземпляр tools и графа на один запрос; параллельный доступ не предусмотрен."""

    instructions = InstructionSnapshot() if instructions_dir is None else InstructionSnapshot(instructions_dir)
    telemetry = telemetry or NoopRecorder()

    def invoke_tool(name, arguments, events):
        with telemetry.span(name, "TOOL", {"tool.name": name, "input.value": json.dumps(arguments, ensure_ascii=False)}) as span:
            response = tools.call_tool(name, arguments)
            span.update({"output.value": json.dumps(response, ensure_ascii=False)})
            if not response["ok"]:
                span.set_error(response["error"]["message"], response["error"]["code"])
        events.append({"kind": "tool", "tool": name, "arguments": arguments, "result": response})
        if not response["ok"]:
            raise WorkflowError(response["error"]["code"], response["error"]["message"])
        return response["data"]

    def model(role, schema, purpose, context, events, json_schema=None):
        with telemetry.span(f"{role}:{purpose}", "LLM", {"app.role": role, "app.purpose": purpose, "llm.model_name": client.model_name(role)}) as span:
            bundle = instructions.bundle(purpose)
            span.update({"input.value": json.dumps({"system": bundle.text, "context": context}, ensure_ascii=False), "input.mime_type": "application/json", "metadata": json.dumps(bundle.metadata, ensure_ascii=False)})
            try:
                reply = client.chat(role, schema, bundle.text, context, json_schema=json_schema)
            except ModelError as exc:
                if exc.event:
                    exc.event["instructions"] = bundle.metadata
                    span.update({"output.value": exc.event.get("output", ""), "app.model_event": json.dumps(exc.event, ensure_ascii=False)})
                    for source, target in (("prompt_tokens", "llm.token_count.prompt"), ("output_tokens", "llm.token_count.completion")):
                        if type(exc.event.get(source)) is int:
                            span.update({target: exc.event[source]})
                raise
            span.update({"output.value": reply.event.get("output", ""), "app.model_digest": reply.event.get("model_digest", ""), "app.model_family": reply.event.get("family", ""), "app.quantization": reply.event.get("quantization", ""), "app.model_event": json.dumps(reply.event, ensure_ascii=False)})
            span.update({"llm.invocation_parameters": json.dumps(reply.event.get("options", {}), ensure_ascii=False)})
            for metric in ("generation_tokens_per_second", "total_duration_seconds", "load_duration_seconds", "prompt_eval_duration_seconds", "eval_duration_seconds"):
                if type(reply.event.get(metric)) in (int, float):
                    span.update({"app.model." + metric: reply.event[metric]})
            prompt_tokens, output_tokens = reply.event.get("prompt_tokens"), reply.event.get("output_tokens")
            if type(prompt_tokens) is int:
                span.update({"llm.token_count.prompt": prompt_tokens})
            if type(output_tokens) is int:
                span.update({"llm.token_count.completion": output_tokens})
            if type(prompt_tokens) is int and type(output_tokens) is int:
                span.update({"llm.token_count.total": prompt_tokens + output_tokens})
        reply.event["instructions"] = bundle.metadata
        events.append(reply.event)
        return reply

    def step(name, function):
        def execute(state):
            if progress:
                progress(name, state)
            events = []
            started = time.perf_counter()
            with telemetry.span(name, "CHAIN", {"app.node": name}) as span:
                try:
                    update = function(state, events)
                except (WorkflowError, ModelError, ToolError, InstructionError, MemoryStoreError) as exc:
                    if isinstance(exc, ModelError) and exc.event:
                        events.append(exc.event)
                    update = {"status": "technical_error", "error": {"code": exc.code, "message": str(exc)}}
                    span.set_error(str(exc), exc.code)
                for event in events:
                    # Полные промпты находятся в LLM-спане; в логах сохраняем его сводку.
                    attributes = {key: value for key, value in event.items() if key not in {"messages", "output"}}
                    telemetry.event(event["kind"], attributes, level="ERROR" if event.get("status") == "error" else "INFO")
                if update.get("feedback"):
                    telemetry.event("review_feedback", {"node": name, "feedback": update["feedback"]}, level="WARNING")
                if update.get("review"):
                    span.update({"app.review.approved": update["review"]["approved"], "app.review.reason": update["review"]["reason"]})
                if name == "route":
                    span.update({"app.route": update.get("writer_role", ""), "app.writer_model": update.get("writer_model", "")})
            events.append({"kind": "node", "node": name, "status": update.get("status", state["status"]), "duration_ms": round((time.perf_counter() - started) * 1000, 3)})
            return {**update, "events": events}
        return execute

    def load(state, events):
        checked_text(state["question"], "question", 500)
        if not isinstance(state["requested_route"], str) or state["requested_route"] not in {"auto", "simple", "complex"}:
            raise WorkflowError("INVALID_ARGUMENT", "Режим должен быть auto, simple или complex.")
        events.append({"kind": "instructions", **instructions.metadata()})
        document = invoke_tool("read_lecture", {}, events)
        context = {"enabled": False, "recent_questions": []}
        if memory is not None:
            context = {"enabled": True, **memory.begin(run_id, session_id, state["question"], document)}
            events.append({"kind": "memory", "action": "load_session", "session_id": session_id, "recent_questions": len(context["recent_questions"]), "stale_facts": context["stale_facts"]})
        return {"document": document, "memory_context": context}

    def conversation(state):
        # В контекст передаются только прошлые вопросы. Ответы из истории не становятся источниками фактов.
        return state.get("memory_context", {}).get("recent_questions", [])

    def rewrite(state, events):
        try:
            reply = model("search", SearchOutput, "search", {"question": state["question"], "previous_questions": conversation(state)}, events)
        except ModelError as exc:
            if exc.code != "MODEL_NOT_INSTALLED":
                raise
            if exc.event:
                events.append(exc.event)
            events.append({"kind": "degradation", "code": exc.code, "action": "search_original_question"})
            return {"search_query": state["question"]}
        query = checked_text(reply.parsed.query, "query", 200)
        return {"search_query": query}

    def search(state, events):
        merged = {}
        queries = list(dict.fromkeys([state["question"], state["search_query"]]))
        cache_key = hashlib.sha256(json.dumps({"algorithm": "lexical-v1", "queries": queries}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        cached = memory.get_cache(cache_key, state["document"]) if memory is not None else None
        if cached is not None:
            # Даже кеш не заменяет проверку по текущему файлу и расчёт расположения цитаты.
            for fragment in cached:
                verified = invoke_tool("get_quote", {"fragment_id": fragment["source"]["fragment_id"], "quote": fragment["text"]}, events)
                if verified["source"] != fragment["source"]:
                    raise WorkflowError("MEMORY_SOURCE_MISMATCH", "Кеш не соответствует текущему источнику.")
                merged[fragment["source"]["fragment_id"]] = fragment
        else:
            for query in queries:
                found = invoke_tool("search_fragments", {"query": query, "limit": 3}, events)
                for fragment in found["fragments"]:
                    key = fragment["source"]["fragment_id"]
                    if key in merged:
                        words = sorted(set(merged[key]["matched_terms"]) | set(fragment["matched_terms"]))
                        merged[key].update(matched_terms=words, matched_terms_count=len(words))
                    else:
                        merged[key] = fragment
            if memory is not None:
                selected = sorted(merged.values(), key=lambda item: (-item["matched_terms_count"], item["source"]["char_start"]))[:2]
                memory.put_cache(cache_key, state["document"], selected)
                merged = {part["source"]["fragment_id"]: part for part in selected}
        if memory is not None:
            facts = memory.recall(state["question"], state["document"])
            reused = 0
            for fact in facts:
                verified = invoke_tool("get_quote", {"fragment_id": fact["source"]["fragment_id"], "quote": fact["quote"]}, events)
                if verified["source"] != fact["source"]:
                    raise WorkflowError("MEMORY_SOURCE_MISMATCH", "Запись памяти не соответствует текущему источнику.")
                key = verified["source"]["fragment_id"]
                if key not in merged:
                    terms = tools._terms(state["question"]) & tools._terms(fact["question"] + " " + fact["quote"])
                    merged[key] = {"text": verified["quote"], "source": verified["source"], "matched_terms": sorted(terms), "matched_terms_count": len(terms)}
                reused += 1
            events.append({"kind": "memory", "action": "retrieve", "cache_hit": cached is not None, "facts_revalidated": reused})
        fragments = sorted(merged.values(), key=lambda item: (-item["matched_terms_count"], item["source"]["char_start"]))[:2]
        if not fragments:
            return {"fragments": [], "status": "insufficient_information", "reason": "no_matching_fragments"}
        if requests_calendar_year(state["question"]) and not any(contains_year_evidence(fragment["text"]) for fragment in fragments):
            events.append({"kind": "validation", "check": "calendar_year_required", "passed": False})
            return {"fragments": fragments, "status": "insufficient_information", "reason": "requested_year_not_found"}
        return {"fragments": fragments}

    def route(state, events):
        requested = state["requested_route"]
        if requested != "auto":
            decision = {"route": requested, "source": "manual"}
        else:
            try:
                with telemetry.span("classify_question", "ROUTER", {"input.value": state["question"]}) as span:
                    decision = dict(client.route_question(state["question"]))
                    span.update({"output.value": json.dumps(decision, ensure_ascii=False), "app.router_model": decision.get("model_id", "")})
                if decision.get("route") not in {"simple", "complex"}:
                    raise ValueError("Неизвестный маршрут классификатора")
                decision["source"] = "nli_classifier"
                if decision.get("uncertain"):
                    decision.update(route="complex", warning="Оценки классов близки; выбран более сильный маршрут.")
            except (ModelError, OSError, RuntimeError, ValueError) as exc:
                decision = {"route": "complex", "source": "safe_fallback", "error": str(exc), "warning": "Классификатор недоступен; выбран запасной сложный маршрут."}
        events.append({"kind": "routing", "decision": decision})
        role = decision["route"]
        return {"writer_role": role, "writer_model": client.model_name(role), "routing": decision}

    def sources(state):
        return [{"fragment_id": part["source"]["fragment_id"], "section": part["source"]["section"], "text": part["text"]} for part in state["fragments"]]

    def write(state, events, revision=False):
        context = {"question": state["question"], "previous_questions": conversation(state), "fragments": sources(state)}
        if revision:
            context.update(previous_draft=state["draft"], feedback=state["feedback"])
        role = state["writer_role"]
        routing = dict(state.get("routing", {}))
        try:
            reply = model(role, DraftOutput, "revise" if revision else "answer", context, events)
        except ModelError as exc:
            if exc.code != "MODEL_NOT_INSTALLED" or role != "simple":
                raise
            if exc.event:
                events.append(exc.event)
            role = "complex"
            routing.update(route=role, writer_fallback="simple_to_complex", warning="Маленькая модель не установлена; запрос передан более сильной модели.")
            events.append({"kind": "routing", "decision": routing})
            reply = model(role, DraftOutput, "revise" if revision else "answer", context, events)
        draft = reply.parsed
        if draft.has_answer != bool(draft.claims):
            raise WorkflowError("MODEL_INVALID_OUTPUT", "Поля has_answer и claims противоречат друг другу.")
        # На первом этапе ответ извлекается дословно: модель не добавляет свободный пересказ.
        claims = [{"claim_id": f"c{index}", **claim.model_dump(), "text": claim.quote} for index, claim in enumerate(draft.claims, 1)]
        update = {"draft": {"has_answer": draft.has_answer, "claims": claims}, "writer_family": reply.event["family"], "writer_role": role, "writer_model": client.model_name(role), "routing": routing, "validated": [], "review": None, "feedback": []}
        if revision:
            update["revisions"] = state["revisions"] + 1
        if not draft.has_answer:
            update.update(status="insufficient_information", reason="writer_abstained")
        return update

    def validate(state, events):
        allowed = {part["source"]["fragment_id"] for part in state["fragments"]}
        feedback, validated = [], []
        for claim in state["draft"]["claims"]:
            lines = [line.strip() for line in claim["text"].splitlines() if line.strip()]
            if not lines or all(re.fullmatch(r"#{1,6}\s+.+", line) for line in lines):
                feedback.append(f"{claim['claim_id']}: вместо утверждения указан только заголовок. Напиши полное предложение с ответом, можно дословно повторить подтверждающую цитату.")
                continue
            if claim["fragment_id"] not in allowed:
                feedback.append(f"{claim['claim_id']}: использован фрагмент вне найденных источников.")
                continue
            with telemetry.span("get_quote", "TOOL", {"tool.name": "get_quote", "input.value": claim["quote"]}) as span:
                response = tools.call_tool("get_quote", {"fragment_id": claim["fragment_id"], "quote": claim["quote"]})
                span.update({"output.value": json.dumps(response, ensure_ascii=False)})
                if not response["ok"]:
                    span.set_error(response["error"]["message"], response["error"]["code"])
            events.append({"kind": "tool", "tool": "get_quote", "arguments": {"fragment_id": claim["fragment_id"], "quote": claim["quote"]}, "result": response})
            if not response["ok"]:
                error = response["error"]
                if error["code"] in {"QUOTE_NOT_FOUND", "UNKNOWN_FRAGMENT", "INVALID_ARGUMENT"}:
                    feedback.append(f"{claim['claim_id']}: {error['message']}")
                    continue
                raise WorkflowError(error["code"], error["message"])
            validated.append({"claim_id": claim["claim_id"], **response["data"]})
        if requests_calendar_year(state["question"]) and not contains_year_evidence("\n".join(claim["text"] for claim in state["draft"]["claims"])):
            feedback.append("Вопрос требует календарный год, но выбранные цитаты не содержат года. Выбери точную цитату с годом, относящимся к вопросу, либо откажись от ответа.")
        return {"validated": validated, "feedback": feedback}

    def review(state, events):
        if family_group(client.family("review")) == family_group(state["writer_family"]):
            raise WorkflowError("REVIEWER_FAMILY_CONFLICT", "Проверяющий должен использовать модель другого семейства.")
        expected = {claim["claim_id"] for claim in state["draft"]["claims"]}
        review_schema = ReviewOutput.model_json_schema()
        review_schema["properties"]["evaluations"].update(minItems=len(expected), maxItems=len(expected))
        review_schema["$defs"]["Evaluation"]["properties"]["claim_id"]["enum"] = sorted(expected)
        cited = {claim["fragment_id"] for claim in state["draft"]["claims"]}
        reply = model("review", ReviewOutput, "review", {"question": state["question"], "previous_questions": conversation(state), "answer_text": "\n".join(claim["text"] for claim in state["draft"]["claims"]), "claims": state["draft"]["claims"], "fragments": [fragment for fragment in sources(state) if fragment["fragment_id"] in cited]}, events, json_schema=review_schema)
        review_data = reply.parsed.model_dump()
        received = [item["claim_id"] for item in review_data["evaluations"]]
        if len(received) != len(expected) or set(received) != expected:
            raise WorkflowError("MODEL_INVALID_OUTPUT", "Проверяющий не оценил каждое утверждение ровно один раз.")
        feedback = [f"{item['claim_id']}: {item['reason'] or 'утверждение не подтверждено'}" for item in review_data["evaluations"] if not item["supported"]]
        if not review_data["answers_question"]:
            feedback.append(review_data["reason"] or "Ответ не отвечает на весь вопрос.")
        review_data.update(model=reply.event["model"], family=reply.event["family"], approved=not feedback)
        return {"review": review_data, "feedback": feedback}

    def accept(state, events):
        # Защитная проверка перед сохранением: никакого частично одобренного ответа.
        if not state["review"] or not state["review"]["approved"] or len(state["validated"]) != len(state["draft"]["claims"]):
            raise WorkflowError("INVALID_ACCEPT_STATE", "Не все утверждения прошли проверки.")
        answer = "\n".join(f"{claim['text']} [{index}]" for index, claim in enumerate(state["draft"]["claims"], 1))
        citations = [{"fragment_id": claim["fragment_id"], "quote": claim["quote"]} for claim in state["draft"]["claims"]]
        saved = invoke_tool("save_result", {"question": state["question"], "answer": answer, "citations": citations}, events)
        return {"status": "answered", "result": {"status": "answered", "answer": answer, "citations": state["validated"], "semantic_verification": {"status": "model_approved", "model": state["review"]["model"], "family": state["review"]["family"]}, "saved_answer_file": saved["file"]}}

    def abstain(state, events):
        return {"status": "insufficient_information", "reason": "verification_failed_after_revision"}

    def finish(state, events):
        if state["status"] == "answered":
            return {}
        if state["status"] == "technical_error":
            return {"result": {"status": "technical_error", "answer": "Не удалось обработать запрос из-за технической ошибки.", "error": state["error"], "citations": []}}
        return {"result": {"status": "insufficient_information", "answer": "Недостаточно подтверждённой информации в найденных фрагментах лекции.", "reason": state["reason"], "citations": []}}

    def next_if_running(next_node):
        return lambda state: next_node if state["status"] == "running" else "finish"

    def after_check(state, passed):
        if state["status"] != "running":
            return "finish"
        if not state["feedback"]:
            return passed
        return "revise" if state["revisions"] < MAX_REVISIONS else "abstain"

    graph = StateGraph(GraphState)
    nodes = {"load": load, "rewrite": rewrite, "search": search, "route": route, "draft": write,
             "validate": validate, "review": review, "revise": lambda state, events: write(state, events, True),
             "accept": accept, "abstain": abstain, "finish": finish}
    for name, function in nodes.items():
        graph.add_node(name, step(name, function))
    graph.add_edge(START, "load")
    for current, following in (("load", "rewrite"), ("rewrite", "search"), ("search", "route"), ("route", "draft"), ("draft", "validate"), ("revise", "validate")):
        graph.add_conditional_edges(current, next_if_running(following), [following, "finish"])
    graph.add_conditional_edges("validate", lambda state: after_check(state, "review"), ["review", "revise", "abstain", "finish"])
    graph.add_conditional_edges("review", lambda state: after_check(state, "accept"), ["accept", "revise", "abstain", "finish"])
    graph.add_edge("accept", "finish")
    graph.add_edge("abstain", "finish")
    graph.add_edge("finish", END)
    return graph.compile()
