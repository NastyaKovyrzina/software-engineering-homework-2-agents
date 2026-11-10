"""Командный запуск графа помощника по выбранной лекции."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Этот локальный прототип пишет собственные записи в runs, без облачного трейсинга.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from lecture_assistant.lecture_graph import build_graph, initial_state
from lecture_assistant.lecture_tools import PROJECT_DIR, LectureTools
from lecture_assistant.llm_client import ModelError, OllamaClient
from lecture_assistant.result_format import make_outcome, render_outcome
from lecture_assistant.sandbox_runtime import BOX_NAME, get_sandbox_info
from lecture_assistant.instruction_loader import InstructionError
from lecture_assistant.memory_store import MemoryStore, MemoryStoreError
from lecture_assistant.telemetry_recorder import TelemetryRecorder, NoopRecorder

LABELS = {
    "load": "Читаю лекцию", "rewrite": "Подготавливаю поиск",
    "search": "Ищу подходящие фрагменты", "route": "Выбираю способ ответа",
    "draft": "Подготавливаю ответ", "validate": "Проверяю цитаты",
    "review": "Проверяю соответствие вопросу", "revise": "Исправляю ответ: попытка 1 из 1",
    "accept": "Сохранение одобренного ответа", "abstain": "Подтверждений недостаточно", "finish": "Завершение",
}


def save_run(record: dict) -> Path:
    directory = (PROJECT_DIR / "runs").resolve()
    if not directory.is_relative_to(PROJECT_DIR):
        raise OSError("Папка runs ведёт за пределы проекта")
    directory.mkdir(exist_ok=True)
    destination = directory / f"run-{record['run_id']}.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=".run-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lecture", default="lecture_demo.md", help="Имя .txt/.md в data/lectures")
    parser.add_argument("--question", required=True, help="Вопрос по лекции, до 500 символов")
    parser.add_argument("--route", choices=("auto", "simple", "complex"), default="auto", help="auto: локальный NLI-классификатор; simple/complex: ручной выбор")
    parser.add_argument("--timeout", type=int, default=300, help="Ожидание одного ответа Ollama, секунды (10–600)")
    parser.add_argument("--verbose", action="store_true", help="Показать модели, коды причин и подробности ошибок")
    parser.add_argument("--sandbox", action="store_true", help="Работа внутри Sandboxie через ограниченный шлюз")
    parser.add_argument("--gateway", action="store_true", help="Явный отладочный запуск вне Sandboxie через тот же шлюз моделей")
    parser.add_argument("--session", help="Имя беседы для продолжения: латинские буквы, цифры, _ и -, до 64 символов")
    parser.add_argument("--no-memory", action="store_true", help="Изолированный запрос без чтения и записи памяти")
    parser.add_argument("--no-observability", action="store_true", help="Не записывать отдельную телеметрию этого запуска")
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 10 <= args.timeout <= 600:
        parser.error("--timeout должен быть от 10 до 600 секунд")
    if args.run_id is not None and not re.fullmatch(r"[0-9a-f]{32}", args.run_id):
        parser.error("Некорректный идентификатор запуска")
    if args.session is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.session):
        parser.error("--session: от 1 до 64 латинских букв, цифр, _ или -")
    started = time.perf_counter()
    state = initial_state(args.question, args.route)
    record = {
        "run_id": args.run_id or uuid4().hex, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "lecture": args.lecture, "python": platform.python_version(),
        "langgraph": importlib.metadata.version("langgraph"),
        "code_sha256": {name: hashlib.sha256((PROJECT_DIR / name).read_bytes()).hexdigest() for name in ("app.py", "lecture_assistant/lecture_graph.py", "lecture_assistant/llm_client.py", "lecture_assistant/lecture_tools.py", "lecture_assistant/result_format.py", "lecture_assistant/sandbox_runtime.py", "lecture_assistant/gateway_config.py", "lecture_assistant/instruction_loader.py", "models.json", "lecture_assistant/router_runner.py", "router_worker.py", "router_settings.json", "lecture_assistant/memory_store.py", "lecture_assistant/telemetry_recorder.py")},
        "execution": {"sandbox": get_sandbox_info(), "model_transport": "gateway" if args.sandbox or args.gateway else "direct"},
    }
    telemetry = NoopRecorder() if args.no_observability else TelemetryRecorder(PROJECT_DIR, record["run_id"], attributes={"question": args.question, "lecture": args.lecture, "session_id": args.session or record["run_id"], "requested_route": args.route, "model_transport": record["execution"]["model_transport"], "app.eval.suite_id": os.environ.get("LECTURE_EVAL_SUITE_ID", ""), "app.eval.case_id": os.environ.get("LECTURE_EVAL_CASE_ID", ""), "app.code_sha256": json.dumps(record["code_sha256"]), "app.python": record["python"], "app.langgraph": record["langgraph"]})
    record["observability"] = {"trace_id": telemetry.trace_id, "spool_file": str(telemetry.path) if telemetry.path else None, "recording_error": telemetry.error}

    def progress(name, current):
        if name in {"route", "accept", "abstain", "finish"} and not args.verbose:
            return
        suffix = ""
        if args.verbose:
            suffix = f" [{name}]"
            if name in {"draft", "revise"}:
                suffix += f" — {current['writer_model']}"
        print(f"→ {LABELS[name]}{suffix}", flush=True)

    memory = None
    try:
        sandbox = record["execution"]["sandbox"]
        if args.sandbox and (not sandbox["sandboxed"] or sandbox["box_name"] != BOX_NAME):
            raise ModelError("SANDBOX_REQUIRED", f"Этот режим требует запуска внутри {BOX_NAME} через run_sandbox.py.")
        if not args.sandbox and sandbox["sandboxed"]:
            raise ModelError("SANDBOX_MODE_REQUIRED", "Внутри Sandboxie используйте режим --sandbox и шлюз моделей.")
        client = OllamaClient(PROJECT_DIR / "models.json", timeout=args.timeout, transport=record["execution"]["model_transport"])
        memory = None if args.no_memory else MemoryStore(PROJECT_DIR)
        tools = LectureTools(args.lecture)
        graph = build_graph(tools, client, progress, memory=memory, session_id=args.session or record["run_id"], run_id=record["run_id"], telemetry=telemetry)
        # Сохраняем последний полученный снимок даже при неожиданной ошибке следующего узла.
        for snapshot in graph.stream(state, config={"recursion_limit": 30}, stream_mode="values"):
            state = snapshot
            if memory is not None and state.get("memory_context", {}).get("enabled"):
                nodes = [event["node"] for event in state.get("events", []) if event["kind"] == "node"]
                with telemetry.span("memory.checkpoint", "TOOL", {"app.memory.operation": "checkpoint"}):
                    memory.checkpoint(record["run_id"], nodes[-1] if nodes else "load", state)
        if memory is not None and state.get("memory_context", {}).get("enabled"):
            citations = []
            if make_outcome(state, args.lecture)["status"] == "answered":
                for citation in state["result"]["citations"]:
                    verified = tools.call_tool("get_quote", {"fragment_id": citation["source"]["fragment_id"], "quote": citation["quote"]})
                    if not verified["ok"]:
                        raise MemoryStoreError("Не удалось заново проверить источник перед записью в память: " + verified["error"]["message"])
                    if verified["data"]["source"] != citation["source"]:
                        raise MemoryStoreError("Источник цитаты изменился перед записью в память")
                    citations.append(verified["data"])
            with telemetry.span("memory.complete", "TOOL", {"app.memory.operation": "complete"}) as span:
                summary = memory.complete(record["run_id"], state, validated_citations=citations)
                span.update(summary)
            state["memory_context"].update(summary)
            state["events"].append({"kind": "memory", "action": "commit", **summary})
            telemetry.event("memory_commit", summary)
    except (ModelError, InstructionError, MemoryStoreError) as exc:
        telemetry.event("application_error", {"code": exc.code, "message": str(exc)}, level="ERROR")
        state.update(status="technical_error", error={"code": exc.code, "message": str(exc)})
        state["result"] = {"status": "technical_error", "answer": "Не удалось обработать запрос из-за технической ошибки.", "error": state["error"], "citations": []}
    except Exception as exc:
        telemetry.event("application_error", {"code": "UNEXPECTED_ERROR", "message": f"{type(exc).__name__}: {exc}"}, level="ERROR")
        # Не называем программную ошибку отсутствием информации в лекции.
        state.update(status="technical_error", error={"code": "UNEXPECTED_ERROR", "message": f"{type(exc).__name__}: {exc}"})
        state["result"] = {"status": "technical_error", "answer": "Произошла непредвиденная ошибка выполнения графа.", "error": state["error"], "citations": []}
    outcome = make_outcome(state, args.lecture)
    record.update(outcome=outcome, duration_seconds=round(time.perf_counter() - started, 3), state=state)
    run_file = None
    try:
        run_file = save_run(record)
    except (OSError, RuntimeError, UnicodeError) as exc:
        failed = {**state, "status": "technical_error", "error": {"code": "RUN_SAVE_FAILED", "message": str(exc)}}
        failed["result"] = {"status": "technical_error", "error": failed["error"]}
        outcome = make_outcome(failed, args.lecture)
        telemetry.event("run_save_failed", {"message": str(exc)}, level="ERROR")
    telemetry.finish(outcome["status"], {**outcome, "writer_model": state.get("writer_model")})
    print(f"\n{render_outcome(outcome, verbose=args.verbose)}")
    print(f"\nЗапись запроса: {run_file}" if run_file else "\nЗапись запроса не сохранена.")
    if args.verbose:
        print(f"Время запроса: {record['duration_seconds']:.1f} с")
        if telemetry.trace_id:
            print(f"Идентификатор трейса: {telemetry.trace_id}; панель: http://127.0.0.1:18736")
    return 1 if outcome["status"] == "technical_error" else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
