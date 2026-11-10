"""Проверка фактического контекста, файлов, сети и запуска команд внутри Sandboxie."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from lecture_assistant.gateway_config import CONTROL_URL, GATEWAY_URL, load_gateway_runtime
from lecture_assistant.sandbox_runtime import BOX_NAME, get_sandbox_info

ROOT = Path(__file__).resolve().parent
HTTP = build_opener(ProxyHandler({}))


def get_json(url, headers=None):
    with HTTP.open(Request(url, headers=headers or {}), timeout=4) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{32}", args.run_id):
        parser.error("Неверный идентификатор")
    sandbox = get_sandbox_info()
    checks = []

    def check(name, function, expected_denied=False):
        try:
            value = function()
            passed = bool(value) and not expected_denied
            detail = "Доступ неожиданно разрешён" if expected_denied else ("Успешно" if passed else "Неверный результат")
        except (OSError, URLError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
            passed = expected_denied
            detail = f"{type(exc).__name__}: {exc}"
        checks.append({"name": name, "passed": passed, "detail": detail})

    check("Процесс внутри нужной песочницы", lambda: sandbox["sandboxed"] and sandbox["box_name"] == BOX_NAME)
    if not checks[0]["passed"]:
        print("Проверку запускают через run_sandbox.py --check, а не вне песочницы.")
        return 1
    runtime = load_gateway_runtime(ROOT)
    check("Чтение демонстрационной лекции", lambda: bool((ROOT / "data" / "lectures" / "lecture_demo.md").read_text(encoding="utf-8")))

    def output_write():
        for directory in ("runs", "results"):
            target = ROOT / directory / f"probe-{args.run_id}.txt"
            target.write_text("sandbox write test", encoding="utf-8")
            if target.read_text(encoding="utf-8") != "sandbox write test":
                return False
        return True

    check("Запись и чтение runs/results", output_write)

    def sqlite_write():
        target = ROOT / "runs" / f"probe-{args.run_id}.sqlite3"
        connection = sqlite3.connect(target)
        try:
            with connection:
                connection.execute("CREATE TABLE memory_probe(value TEXT NOT NULL)")
                connection.execute("INSERT INTO memory_probe VALUES(?)", ("sandbox sqlite test",))
            return connection.execute("SELECT value FROM memory_probe").fetchone()[0] == "sandbox sqlite test"
        finally:
            connection.close()

    check("SQLite: транзакция и чтение базы в runs", sqlite_write)
    readonly = ROOT / "readonly-probe.txt"
    before = hashlib.sha256(readonly.read_bytes()).hexdigest()
    def write_readonly():
        with readonly.open("a", encoding="utf-8") as stream:
            return stream.write("\nTEST")
    check("Запись вне runs/results запрещена для контрольного файла", write_readonly, expected_denied=True)
    check("Контрольный файл не изменён", lambda: hashlib.sha256(readonly.read_bytes()).hexdigest() == before)
    outside = ROOT.parent / "isolation-lab" / "outside" / "isolation-control.txt"
    check("Чтение закрытого контрольного файла запрещено", lambda: outside.read_bytes(), expected_denied=True)
    check("Шлюз доступен", lambda: get_json(GATEWAY_URL + "/health")["instance_id"] == runtime["instance_id"])
    headers = {"Authorization": "Bearer " + runtime["token"]}
    check("Сведения о моделях через шлюз доступны", lambda: len(get_json(GATEWAY_URL + "/api/tags", headers)["models"]) == 4)
    check("Прямой доступ к Ollama запрещён", lambda: get_json("http://127.0.0.1:11434/api/version"), expected_denied=True)
    check("Контрольный порт 18735 запрещён", lambda: get_json(CONTROL_URL), expected_denied=True)

    def shell():
        result = subprocess.run([str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "cmd.exe"), "/d", "/c", "exit", "0"], capture_output=True, timeout=5)
        if result.returncode:
            raise RuntimeError(f"Запуск cmd.exe отклонён, код {result.returncode}")
        return True

    check("Запуск cmd.exe запрещён", shell, expected_denied=True)
    report = {"run_id": args.run_id, "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "execution": {"sandbox": sandbox, "model_transport": "gateway"},
              "checks": checks, "passed": all(item["passed"] for item in checks)}
    destination = ROOT / "runs" / f"sandbox-check-{args.run_id}.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
