"""One end-to-end Evals runner. Defaults to Sandboxie; never runs on import."""
import argparse
import csv
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from uuid import uuid4

from lecture_assistant.eval_grading import grade_turn

ROOT = Path(__file__).resolve().parent


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def resources(record):
    try:
        path = Path(record["observability"]["spool_file"]).resolve()
        if not path.is_relative_to(ROOT / "runs" / "telemetry" / "spool") or path.stem != record["run_id"]:
            return {}
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        attrs = next(e["attributes"] for e in reversed(events) if e["event"] == "trace_end")
        return {"peak_rss_mib": attrs.get("memory.peak_rss_mib"), "cpu_seconds": attrs.get("cpu.seconds")}
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return {}


def summarize(report):
    rows = report["turns"]
    cases = set(report["selected_cases"])
    return {"cases": len(cases), "turns": report["planned_turns"],
        "executed": sum(row["grading"]["executed"] for row in rows),
        "passed_turns": sum(row["grading"]["auto_passed"] for row in rows),
        "passed_cases": sum(
            len([r for r in rows if r["case_id"] == case]) == report["case_turns"][case]
            and all(r["grading"]["auto_passed"] for r in rows if r["case_id"] == case) for case in cases)}


def write_report(directory, report):
    summary = report["summary"] = summarize(report)
    manual = directory / "manual.csv"
    columns = ["case_id", "turn", "Вопрос", "Ответ", "Ожидание", "Правильность 0-2", "Полнота 0-2",
               "По источнику да/нет", "Инструкции да/нет", "Правки", "Комментарий"]
    previous = {}
    if manual.exists():
        with manual.open(encoding="utf-8-sig", newline="") as stream:
            previous = {(r["case_id"], r["turn"]): r for r in csv.DictReader(stream, delimiter=";")}
    with manual.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter=";")
        writer.writeheader()
        for row in report["turns"]:
            if row["grading"]["manual_required"] and row["grading"]["executed"]:
                item = {key: "" for key in columns}
                item.update(previous.get((row["case_id"], str(row["turn"])), {}))
                item.update(case_id=row["case_id"], turn=row["turn"], Вопрос=row["question"],
                            Ответ=row["answer"], Ожидание=row["expected_status"])
                writer.writerow(item)
    with manual.open(encoding="utf-8-sig", newline="") as stream:
        manual_rows = list(csv.DictReader(stream, delimiter=";"))
    reviewed = [r for r in manual_rows if all(r.get(key, "") != "" for key in
        ("Правильность 0-2", "Полнота 0-2", "По источнику да/нет", "Инструкции да/нет", "Правки"))]
    for r in reviewed:
        if r["Правильность 0-2"] not in {"0", "1", "2"} or r["Полнота 0-2"] not in {"0", "1", "2"}:
            raise ValueError("Manual correctness and completeness must be 0, 1 or 2.")
        if r["По источнику да/нет"] not in {"да", "нет"} or r["Инструкции да/нет"] not in {"да", "нет"}:
            raise ValueError("Manual confirmation fields must be да or нет.")
    report["manual"] = {"reviewed": len(reviewed), "pending": len(manual_rows)-len(reviewed),
        "correctness_mean": sum(int(r["Правильность 0-2"]) for r in reviewed)/len(reviewed) if reviewed else None,
        "completeness_mean": sum(int(r["Полнота 0-2"]) for r in reviewed)/len(reviewed) if reviewed else None,
        "manual_edits_rate": sum(r["Правки"].strip().lower() != "нет" for r in reviewed)/len(reviewed) if reviewed else None}
    save(directory / "report.json", report)
    with (directory / "results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["Случай", "Шаг", "Выполнен", "Пройден", "Время, с", "RAM процесса, МиБ",
                         "Вызовы моделей", "Ошибки инструментов", "Исправления", "Покрытие фактов", "Цитаты", "Трейс"])
        for row in report["turns"]:
            g, m = row["grading"], row["grading"]["metrics"]
            writer.writerow([row["case_id"], row["turn"], g["executed"], g["auto_passed"],
                m["duration_seconds"], m["peak_rss_mib"], m["model_calls"], m["tool_selection_errors"],
                m["revisions"], g["scores"]["required_fact_coverage"], g["scores"]["citation_grounding"], row["run_id"]])
    text = ["# Evals MVP", "", f"Режим: {report['mode']}. Сценариев: {summary['cases']}; пройдено: {summary['passed_cases']}.",
        f"Обращений: {summary['turns']}; выполнено: {summary['executed']}; пройдено: {summary['passed_turns']}.",
        f"Ручная проверка: {len(reviewed)}; ожидают: {len(manual_rows)-len(reviewed)}.", "",
        "Ожидаемая ошибка входных данных считается успехом только при правильном коде.",
        "Внутренняя Gemma не является независимым оценщиком; LLM-as-a-Judge не подключён.",
        "Точность цитаты и наличие нужного ответа оцениваются отдельно. Результат не гарантирует отсутствие любых галлюцинаций.",
        "RAM относится к процессу помощника; VRAM и стоимость внешних API не измеряются.",
        "", "| Случай | Шаг | Пройден | Причина |", "|---|---:|---|---|"]
    for row in report["turns"]:
        text.append(f"| {row['case_id']} | {row['turn']} | {row['grading']['auto_passed']} | {'; '.join(row['grading']['issues'])} |")
    (directory / "report.md").write_text("\n".join(text)+"\n", encoding="utf-8")


def compare(baseline, current):
    if baseline.get("schema") != "mvp1" or current.get("schema") != "mvp1" or baseline.get("grader_sha256") != current.get("grader_sha256"):
        raise ValueError("Different report or grader versions cannot be compared.")
    a, b = baseline["summary"], current["summary"]
    if baseline["dataset_sha256"] != current["dataset_sha256"] or baseline["mode"] != current["mode"]:
        raise ValueError("Different datasets or execution modes cannot be compared.")
    if a["executed"] != a["turns"] or b["executed"] != b["turns"]:
        raise ValueError("Cannot compare incomplete runs.")
    old = {(r["case_id"], r["turn"]): r["grading"]["auto_passed"] for r in baseline["turns"]}
    new = {(r["case_id"], r["turn"]): r["grading"]["auto_passed"] for r in current["turns"]}
    if old.keys() != new.keys():
        raise ValueError("Different selected cases cannot be compared.")
    return {"regressions": [list(k) for k in old if old[k] and not new[k]],
            "resolved": [list(k) for k in old if not old[k] and new[k]],
            "success_rate_before": sum(old.values())/len(old), "success_rate_after": sum(new.values())/len(new)}


def save_comparison(directory, report, baseline_dir):
    baseline = json.loads((baseline_dir / "report.json").read_text(encoding="utf-8"))
    result = compare(baseline, report)
    settings = json.loads((ROOT / "observability_settings.json").read_text(encoding="utf-8"))
    low = result["success_rate_after"] < settings["quality_min_success_rate"]
    drop = result["success_rate_before"] - result["success_rate_after"] > settings["quality_max_drop"]
    save(directory / "comparison.json", result)
    if result["regressions"] or low or drop:
        from lecture_assistant.telemetry_store import TelemetryStore
        message = json.dumps(result, ensure_ascii=False)
        TelemetryStore(ROOT).alert("quality_regression", message, dedup_key="mvp-eval-"+directory.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="Run one case, for example c01")
    parser.add_argument("--mode", choices=["sandbox", "gateway"], default="sandbox")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--report", type=Path, help="Refresh existing reports after manual CSV annotation; no LLM calls")
    parser.add_argument("--compare", type=Path, help="Compare with a previous MVP report directory")
    parser.add_argument("--router", action="store_true", help="Evaluate only the NLI router on router_holdout.json")
    args = parser.parse_args()
    if not 10 <= args.timeout <= 600:
        parser.error("--timeout must be between 10 and 600")
    directory = ROOT / "runs" / "evals" / uuid4().hex
    if args.report:
        directory = args.report.resolve()
        if not directory.is_relative_to(ROOT / "runs" / "evals"):
            parser.error("--report must be inside runs/evals")
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        if report.get("schema") != "mvp1":
            parser.error("Use a report created by this MVP version; historical reports are kept unchanged.")
        write_report(directory, report)
        if args.compare:
            save_comparison(directory, report, args.compare)
        print(directory / "report.md")
        return
    if not directory.resolve().is_relative_to(ROOT / "runs"):
        raise ValueError("Evals directory escapes project runs.")
    directory.mkdir(parents=True)
    if args.router:
        from router_worker import Router
        router = Router()
        raw = (ROOT / "router_holdout.json").read_bytes()
        cases = json.loads(raw)
        rows = [{"id": case["id"], "expected": case["expected_route"],
                 "decision": router.decide(case["question"])} for case in cases]
        save(directory / "router.json", {"cases_sha256": digest(raw), "rows": rows,
            "accuracy": sum(r["expected"] == r["decision"]["route"] for r in rows)/len(rows),
            "note": "Scores are not calibrated probabilities; route quality is not answer quality."})
        print(directory / "router.json")
        return
    raw = (ROOT / "evals" / "dataset.json").read_bytes()
    dataset = json.loads(raw)
    cases = [c for c in dataset["cases"] if not args.case or c["id"] == args.case]
    if not cases:
        parser.error("Unknown case id")
    save(directory / "dataset.json", dataset)
    code = [*ROOT.glob("*.py"), *(ROOT / "lecture_assistant").glob("*.py"),
            *(ROOT / "instructions").rglob("*.md"), ROOT / "instructions" / "manifest.json",
            ROOT / "models.json", ROOT / "router_settings.json"]
    report = {"schema": "mvp1", "mode": args.mode, "dataset_sha256": digest(raw),
        "python": platform.python_version(), "langgraph": importlib.metadata.version("langgraph"),
        "selected_cases": [c["id"] for c in cases], "case_turns": {c["id"]: len(c["turns"]) for c in cases},
        "planned_turns": sum(len(c["turns"]) for c in cases),
        "grader_sha256": digest((ROOT / "lecture_assistant" / "eval_grading.py").read_bytes()),
        "code_sha256": {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in code}, "turns": []}
    try:
        from lecture_assistant.llm_client import OllamaClient
        client = OllamaClient(ROOT / "models.json")
        report["ollama_version"] = client._request("version").get("version")
        report["model_digests"] = {m["name"]: m.get("digest") for m in client._models("tags")}
    except Exception as exc:
        report["model_metadata_error"] = type(exc).__name__
    for case in cases:
        for index, turn in enumerate(case["turns"], 1):
            document = dataset["documents"][turn["document"]]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", case["id"]) or not re.fullmatch(r"[A-Za-z0-9_-]+", document["filename_group"]):
                raise ValueError("Invalid synthetic document identifier.")
            if document["suffix"] not in {".txt", ".md", ".pdf"} or not isinstance(document["text"], str):
                raise ValueError("Invalid evaluation document.")
            filename = f"eval_{directory.name[:12]}_{case['id']}_{document['filename_group']}{document['suffix']}"
            target = ROOT / "data" / "lectures" / filename
            if not target.resolve().is_relative_to(ROOT / "data" / "lectures"):
                raise ValueError("Synthetic lecture path escapes data/lectures.")
            document_bytes = document["text"].encode("utf-8") if document["exists"] else None
            if document_bytes is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(document_bytes)
            run_id = uuid4().hex
            command = [sys.executable, "-B", "-X", "utf8", str(ROOT / ("run_sandbox.py" if args.mode=="sandbox" else "app.py")),
                "--question="+turn["question"], "--lecture", filename, "--route", case["route"],
                "--run-id", run_id, "--timeout", str(args.timeout)]
            command += ["--session", "eval_"+directory.name[:12]+"_"+case["id"]] if case["memory"] else ["--no-memory"]
            if args.mode == "gateway": command.append("--gateway")
            print(f"{case['id']}.{index}: {turn['question']}", flush=True)
            try:
                result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=5*(args.timeout+60)+120)
                (directory / f"{case['id']}-{index}.log").write_text(result.stdout+"\n"+result.stderr, encoding="utf-8")
            except subprocess.TimeoutExpired:
                (directory / f"{case['id']}-{index}.log").write_text("Launcher timed out.", encoding="utf-8")
            path = ROOT / "runs" / f"run-{run_id}.json"
            record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            if record:
                record["eval_resources"] = resources(record)
            save(directory / f"{case['id']}-{index}.json", {"record": record, "document": document})
            grading = grade_turn(case, turn, record, document_bytes, filename, args.mode)
            report["turns"].append({"case_id": case["id"], "turn": index, "run_id": run_id,
                "question": turn["question"], "expected_status": turn["expected_status"],
                "answer": (record or {}).get("outcome", {}).get("answer"), "grading": grading})
            write_report(directory, report)
    if args.compare:
        save_comparison(directory, report, args.compare)
    print(directory / "report.md")
    return 0 if report["summary"]["passed_turns"] == report["summary"]["turns"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
