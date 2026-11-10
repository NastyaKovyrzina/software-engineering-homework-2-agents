"""Deterministic, source-grounded scoring for frozen lecture evaluation cases.

Model reviews are treated as workflow metadata only. They never establish facts.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

TOOL_NAMES = {"read_lecture", "search_fragments", "get_quote", "save_result"}
MODEL_ERRORS = {"MODEL_NOT_INSTALLED", "MODEL_UNAVAILABLE", "MODEL_CONNECTION_ERROR", "MODEL_TIMEOUT", "MODEL_BUSY", "MODEL_UNLOAD_FAILED"}


def _fragments(text: str, version: str):
    """Mirror LectureTools._split to validate a fragment pointer, not just a file pointer."""
    headings = list(re.finditer(r"(?m)^#{1,6}[ \t]+([^\r\n]+)", text))
    sections = []
    if not headings or headings[0].start() > 0:
        sections.append((0, headings[0].start() if headings else len(text), "Начало документа"))
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        sections.append((heading.start(), end, heading.group(1).strip()))
    result = []
    for start, end, section in sections:
        while start < end:
            stop = min(start + 1200, end)
            if stop < end:
                boundary = max(text.rfind("\n", start + 600, stop), text.rfind(" ", start + 600, stop))
                if boundary >= 0:
                    stop = boundary + 1
            if text[start:stop].strip():
                result.append((f"{version[:16]}:{len(result) + 1:04d}", start, stop, section))
            if stop == end:
                break
            start = stop - 160
    return result


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _numbers(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"\[(\d+)\]", text)}


def _metrics(record: dict | None) -> dict:
    state = (record or {}).get("state") or {}
    state = state if isinstance(state, dict) else {}
    events = state.get("events") or []
    events = events if isinstance(events, list) else []
    model_events = [e for e in events if isinstance(e, dict) and e.get("kind") == "model"]
    tool_events = [e for e in events if isinstance(e, dict) and ("tool" in e or e.get("kind") == "tool")]
    node_visits: dict[str, int] = {}
    for event in events:
        if isinstance(event, dict) and event.get("kind") == "node":
            name = event.get("node") or event.get("name")
            if name:
                node_visits[str(name)] = node_visits.get(str(name), 0) + 1
    durations = [e.get("duration_seconds") for e in model_events if isinstance(e.get("duration_seconds"), (int, float))]
    resources = (record or {}).get("eval_resources") or {}
    usage = [e for e in model_events if isinstance(e.get("prompt_tokens"), int) and isinstance(e.get("output_tokens"), int)]
    outcome = (record or {}).get("outcome") or {}
    return {
        "revisions": state.get("revisions", 0) if isinstance(state.get("revisions", 0), int) else 0,
        "max_node_visits": max(node_visits.values(), default=0),
        "model_calls": len(model_events), "model_errors": sum(e.get("status") == "error" or bool(e.get("error_code")) for e in model_events),
        "tool_selection_errors": sum(e.get("error_code") in {"UNKNOWN_TOOL", "INVALID_ARGUMENT"} or ((e.get("result") or {}).get("error") or {}).get("code") in {"UNKNOWN_TOOL", "INVALID_ARGUMENT"} for e in tool_events),
        "prompt_tokens": sum(e["prompt_tokens"] for e in usage) if usage else None,
        "output_tokens": sum(e["output_tokens"] for e in usage) if usage else None,
        "token_measured_calls": len(usage),
        "duration_seconds": record.get("duration_seconds") if isinstance((record or {}).get("duration_seconds"), (int, float)) else (sum(durations) if durations else None),
        "peak_rss_mib": resources.get("peak_rss_mib"), "cpu_seconds": resources.get("cpu_seconds"),
        "vram_mib": None, "api_cost": None,
    }


def grade_turn(case: dict, turn: dict, record: dict | None, document_bytes: bytes | None,
               expected_filename: str, execution_mode: str) -> dict:
    issues: list[str] = []
    criteria = {**(case or {}), **(turn or {})}
    record = record if isinstance(record, dict) else None
    outcome = (record or {}).get("outcome") or {}
    state = (record or {}).get("state") or {}
    if not isinstance(outcome, dict):
        outcome = {}
    if not isinstance(state, dict):
        state = {}
    expected = criteria.get("expected_status")
    actual = outcome.get("status")
    status_match = actual == expected
    if not status_match:
        issues.append(f"status mismatch: expected {expected}, got {actual}")

    execution = (record or {}).get("execution") or {}
    execution = execution if isinstance(execution, dict) else {}
    sandbox = execution.get("sandbox") or {}
    sandbox = sandbox if isinstance(sandbox, dict) else {}
    transport = execution.get("model_transport")
    if record is None:
        issues.append("missing run record")
        executed = False
    elif execution_mode == "sandbox":
        executed = sandbox.get("sandboxed") is True and sandbox.get("box_name") == "LectureIsolationLab" and transport == "gateway"
    elif execution_mode == "gateway":
        executed = sandbox.get("sandboxed") is not True and transport == "gateway"
    elif execution_mode == "direct":
        executed = True
    else:
        executed = False
        issues.append(f"unknown execution mode: {execution_mode}")
    if record is not None and not executed:
        issues.append("execution mode/transport does not match requested mode")

    answered = actual == "answered"
    sources = outcome.get("sources") or []
    if not isinstance(sources, list):
        sources = []
        issues.append("sources must be a list")
    answer = outcome.get("answer")
    if outcome.get("question") != turn.get("question"):
        issues.append("run question does not match requested turn")
    if outcome.get("lecture") != expected_filename:
        issues.append("run lecture does not match expected filename")
    source_ok: list[bool] = []
    source_texts: list[str] = []
    text = None
    digest = None
    if document_bytes is not None:
        try:
            text = document_bytes.decode("utf-8-sig")
            digest = hashlib.sha256(document_bytes).hexdigest()
        except UnicodeDecodeError:
            issues.append("provided document is not UTF-8")

    for idx, source in enumerate(sources, 1):
        valid = isinstance(source, dict) and text is not None
        if valid:
            quote = source.get("quote")
            start, end = source.get("char_start"), source.get("char_end")
            valid = (source.get("file") == expected_filename and source.get("document_version") == digest
                     and isinstance(quote, str) and bool(quote) and type(start) is int and type(end) is int
                     and 0 <= start < end <= len(text) and text[start:end] == quote)
            if valid:
                valid = source.get("line_start") == _line(text, start) and source.get("line_end") == _line(text, end - 1)
            if valid:
                valid = any(fid == source.get("fragment_id") and a <= start and end <= b
                            for fid, a, b, _ in _fragments(text, digest))
            if valid:
                source_texts.append(quote)
            else:
                issues.append(f"source {idx} does not exactly match the current document, location, and fragment")
        else:
            issues.append(f"source {idx} is malformed or document snapshot unavailable")
        source_ok.append(bool(valid))

    fact_groups = criteria.get("required_fact_groups") or []
    required_total = len(fact_groups)
    covered = 0
    grounded = 0
    refs = _numbers(answer) if isinstance(answer, str) else set()
    for group in fact_groups:
        equivalents = [s for s in group if isinstance(s, str) and s]
        answer_has = isinstance(answer, str) and any(s in answer for s in equivalents)
        cited_has = any(1 <= i <= len(sources) and source_ok[i - 1] and any(s in sources[i - 1].get("quote", "") for s in equivalents) for i in refs)
        covered += bool(answer_has)
        grounded += bool(answer_has and cited_has)
    fact_coverage = grounded / required_total if required_total else None
    citation_grounding = (sum(source_ok) / len(source_ok)) if answered and source_ok else None
    if required_total and grounded < required_total:
        issues.append("required fact groups are missing from answer or its valid cited quotes")

    unsupported: bool | None = None
    if answered:
        valid_refs = {str(i) for i in refs if 1 <= i <= len(sources) and source_ok[i - 1]}
        reconstructed = "".join(source_texts)
        # Every non-whitespace character must be attributable to cited verbatim source text or citation markers.
        residual = answer or ""
        for i in sorted(valid_refs, key=len, reverse=True):
            residual = residual.replace(f"[{i}]", "")
        for quote in source_texts:
            residual = residual.replace(quote, "", 1)
        unsupported = bool(re.sub(r"\s+", "", residual))
        if unsupported:
            issues.append("answer contains text not reconstructible from its cited verbatim quotes")
        if not answer or not isinstance(answer, str) or not refs or any(i < 1 or i > len(sources) or not source_ok[i-1] for i in refs):
            issues.append("answered result lacks valid answer citations")
        review = state.get("review") if isinstance(state.get("review"), dict) else {}
        if review.get("approved") is not True:
            issues.append("answered result lacks actual review approval")
        verification = outcome.get("verification")
        verification = verification if isinstance(verification, dict) else {}
        result_data = state.get("result")
        result_data = result_data if isinstance(result_data, dict) else {}
        semantic = result_data.get("semantic_verification")
        semantic = semantic if isinstance(semantic, dict) else {}
        if not (verification.get("exact_quotes") is True and verification.get("model_review") is True
                and semantic.get("status") == "model_approved"):
            issues.append("answered result lacks exact-quote and semantic verification workflow markers")
        writer_family = state.get("writer_family") or semantic.get("writer_family") or semantic.get("writer_model_family")
        reviewer_family = review.get("family") or semantic.get("family") or semantic.get("reviewer_family") or semantic.get("reviewer_model_family")
        def family(value):
            if not isinstance(value, str):
                return None
            lowered = value.casefold()
            return "qwen" if lowered.startswith("qwen") else "gemma" if lowered.startswith("gemma") else lowered
        if not writer_family or not reviewer_family or family(writer_family) == family(reviewer_family):
            issues.append("answer and reviewer model families are missing or not independent")
    else:
        if answer not in (None, "") or sources:
            issues.append("refusal/error must not include answer text or sources")

    forbidden = criteria.get("forbidden_phrases") or []
    instruction: bool | None = None
    if isinstance(answer, str):
        instruction = not any(isinstance(p, str) and p and p.casefold() in answer.casefold() for p in forbidden)
        if not instruction:
            issues.append("answer contains forbidden phrase")

    route_match = None
    expected_route = criteria.get("expected_route")
    if expected_route:
        state_routing = state.get("routing") if isinstance(state.get("routing"), dict) else {}
        out_routing = outcome.get("routing") if isinstance(outcome.get("routing"), dict) else {}
        route = state_routing.get("route") or out_routing.get("route")
        route_match = route == expected_route

    # Event metadata can flag known model-install fallback; workflow success is assessed separately.
    events = state.get("events") or []
    events = events if isinstance(events, list) else []
    model_errors = [e for e in events if isinstance(e, dict) and e.get("kind") == "model" and (e.get("error_code") or e.get("status") == "error" or ((e.get("result") or {}).get("error") or {}).get("code"))]
    memory_ok = True
    memory_expect = criteria.get("expected_memory")
    if memory_expect:
        memory = state.get("memory_context")
        memory = memory if isinstance(memory, dict) else {}
        count = len(memory.get("recent_questions") or [])
        memory_ok = memory_expect.get("recent_questions_min", 0) <= count <= memory_expect.get("recent_questions_max", 10**9)
        revalidated = sum(
            (e.get("action") == "retrieve_facts_revalidated")
            or (e.get("action") == "retrieve" and isinstance(e.get("facts_revalidated"), int) and e["facts_revalidated"])
            for e in events if isinstance(e, dict) and e.get("kind") in {"memory_action", "memory"}
        )
        memory_ok = memory_ok and revalidated >= memory_expect.get("facts_revalidated_min", 0)
        if not memory_ok:
            issues.append("expected memory context/revalidation is missing")
    metrics = _metrics(record)
    if metrics["revisions"] > 1 or metrics["max_node_visits"] > 3:
        issues.append("revision or node visit limit exceeded")
    node_event_count = sum(isinstance(e, dict) and e.get("kind") == "node" for e in events)
    if node_event_count > 30:
        issues.append("graph node step limit exceeded")
    known_model_failure = any(e.get("error_code") in MODEL_ERRORS or ((e.get("result") or {}).get("error") or {}).get("code") in MODEL_ERRORS for e in model_errors)
    if model_errors and not known_model_failure:
        issues.append("unexpected model failure")

    workflow_valid = status_match and memory_ok and metrics["revisions"] <= 1 and metrics["max_node_visits"] <= 3 and node_event_count <= 30
    if expected == "technical_error":
        expected_error = criteria.get("expected_error")
        error = outcome.get("error")
        error = error if isinstance(error, dict) else {}
        workflow_valid = workflow_valid and (not expected_error or error.get("code") == expected_error)
    gate = (expected == "answered" and required_total > 0 and grounded == required_total and unsupported is False
            and instruction is not False and workflow_valid)
    if expected in {"insufficient_information", "technical_error"}:
        gate = status_match and workflow_valid
    auto_passed = bool(record is not None and executed and gate and not issues)
    manual_required = bool(criteria.get("manual_required", False) or (answered and not required_total))
    return {"executed": bool(executed), "auto_passed": auto_passed, "issues": issues,
            "scores": {"status_match": status_match, "required_fact_coverage": fact_coverage,
                       "citation_grounding": citation_grounding, "unsupported_answer_text": unsupported,
                       "instruction_compliance": instruction, "route_match": route_match,
                       "workflow_valid": workflow_valid},
            "metrics": metrics, "manual_required": manual_required,
            "manual_status": "pending" if manual_required else "not_required"}
