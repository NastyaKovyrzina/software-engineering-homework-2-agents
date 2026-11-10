"""Запуск строго определённого локального классификатора без произвольных команд."""
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_router(question):
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        raise ValueError("Некорректный вопрос для роутера")
    try:
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(ROOT / "router_worker.py"), "--question", question], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except subprocess.SubprocessError as exc:
        raise RuntimeError("Не удалось завершить процесс классификатора за отведённое время") from exc
    try:
        decision = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("Классификатор не вернул JSON") from exc
    if not isinstance(decision, dict):
        raise RuntimeError("Классификатор вернул JSON другого типа вместо решения")
    if result.returncode or decision.get("error"):
        raise RuntimeError(decision.get("error", "Ошибка процесса классификатора"))
    if not isinstance(decision.get("route"), str) or decision["route"] not in {"simple", "complex"} or not isinstance(decision.get("scores"), dict) or set(decision["scores"]) != {"simple", "complex"}:
        raise RuntimeError("Некорректное структурированное решение")
    values = list(decision["scores"].values())
    if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in values) or abs(sum(values) - 1) > .001 or type(decision.get("uncertain")) is not bool:
        raise RuntimeError("Некорректные числовые оценки классификатора")
    return decision
