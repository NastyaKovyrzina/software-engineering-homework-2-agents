from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener
from lecture_assistant.sandbox_runtime import get_sandbox_info


PROJECT = Path(__file__).resolve().parent
BOX_NAME = "LectureIsolationLab"
START_EXE = Path(r"C:\Program Files\Sandboxie-Plus\Start.exe")
PYTHON_EXE = PROJECT / ".venv" / "Scripts" / "python.exe"
GATEWAY_CONFIG = PROJECT / ".gateway.json"
GATEWAY_URL = "http://127.0.0.1:18734"


class LauncherError(Exception):
    pass


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Запуск помощника лекций в Sandboxie.")
    parser.add_argument("--check", action="store_true", help="проверить изоляцию и gateway")
    parser.add_argument("--question", help="вопрос по лекции")
    parser.add_argument("--lecture", default="lecture_demo.md", help="имя файла лекции")
    parser.add_argument("--route", choices=("auto", "simple", "complex"), default="auto")
    parser.add_argument("--timeout", type=int, choices=range(10, 601), default=300)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--session", help="Имя беседы: латинские буквы, цифры, _ и -, до 64 символов")
    parser.add_argument("--no-memory", action="store_true", help="Запрос без чтения и записи памяти")
    parser.add_argument("--no-observability", action="store_true", help="Не записывать отдельную телеметрию запуска")
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.run_id is not None and not re.fullmatch(r"[0-9a-f]{32}", args.run_id):
        parser.error("Некорректный идентификатор запуска")
    if args.session is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.session):
        parser.error("Некорректное имя беседы")
    if not args.check and args.question is None:
        parser.error("--question обязателен, если не указан --check")
    return args


def _load_gateway() -> tuple[str, str]:
    try:
        config = json.loads(GATEWAY_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LauncherError(f"Не удалось прочитать {GATEWAY_CONFIG}: {exc}") from exc
    if not isinstance(config, dict) or config.get("version") != 1:
        raise LauncherError("Некорректная версия .gateway.json; требуется version=1.")
    if config.get("base_url") != GATEWAY_URL:
        raise LauncherError(f"Gateway должен быть настроен строго на {GATEWAY_URL}.")
    token = config.get("token")
    instance_id = config.get("instance_id")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", token):
        raise LauncherError("В .gateway.json отсутствует корректный 64-значный token.")
    if not isinstance(instance_id, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", instance_id):
        raise LauncherError("В .gateway.json отсутствует корректный 32-значный instance_id.")

    request = Request(GATEWAY_URL + "/health", headers={"Authorization": f"Bearer {token}"})
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            health = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LauncherError(f"Проверка gateway /health завершилась ошибкой: {exc}") from exc
    if not isinstance(health, dict) or health.get("service") != "lecture-model-gateway":
        raise LauncherError("Gateway /health вернул неожиданный service.")
    if health.get("instance_id") != instance_id:
        raise LauncherError("Gateway instance_id не совпадает с .gateway.json.")
    return token, instance_id


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LauncherError(f"Не удалось прочитать отчёт {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LauncherError(f"Отчёт {path} имеет неверный формат.")
    return value


def _run_hidden(command: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            command, cwd=PROJECT, shell=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            startupinfo=startup, creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if stdout:
            print(stdout, end="")
        if stderr:
            print(stderr, end="", file=sys.stderr)
        raise LauncherError(f"Sandboxie не завершил запуск за {timeout} секунд.") from exc
    except OSError as exc:
        raise LauncherError(f"Не удалось запустить Sandboxie: {exc}") from exc


def _print_captured(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def _host_controls():
    """Проверяем снаружи, что запрещаемые ресурсы реально доступны на host."""
    opener = build_opener(ProxyHandler({}))
    for url in ("http://127.0.0.1:18735/control", "http://127.0.0.1:11434/api/version"):
        with opener.open(url, timeout=5) as response:
            if not isinstance(json.load(response), dict):
                raise LauncherError(f"Некорректный контрольный ответ: {url}")
    outside = PROJECT.parent / "isolation-lab" / "outside" / "isolation-control.txt"
    outside.read_bytes()
    shell = subprocess.run([str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "cmd.exe"), "/d", "/c", "exit", "0"], capture_output=True, timeout=5)
    if shell.returncode:
        raise LauncherError("Контрольный cmd.exe не работает даже вне Sandboxie.")
    return hashlib.sha256((PROJECT / "readonly-probe.txt").read_bytes()).hexdigest()


def main() -> int:
    args = _arguments()
    try:
        if os.name != "nt" or get_sandbox_info()["sandboxed"]:
            raise LauncherError("Запустите run_sandbox.py в обычном терминале Windows, вне Sandboxie.")
        _load_gateway()
        baseline_hash = _host_controls() if args.check else None
        (PROJECT / "results").mkdir(parents=True, exist_ok=True)
        runs_dir = PROJECT / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = getattr(args, "run_id", None) or uuid.uuid4().hex
        report_name = f"sandbox-check-{run_id}.json" if args.check else f"run-{run_id}.json"
        report_path = runs_dir / report_name

        if not START_EXE.is_file():
            raise LauncherError(f"Не найден Sandboxie Start.exe: {START_EXE}")
        if not PYTHON_EXE.is_file():
            raise LauncherError(f"Не найден Python проекта: {PYTHON_EXE}")

        command = [str(START_EXE), f"/box:{BOX_NAME}", "/silent", "/wait",
                   str(PYTHON_EXE), "-B", "-X", "utf8"]
        if args.check:
            command += [str(PROJECT / "sandbox_probe.py"), "--run-id", run_id]
        else:
            command += [str(PROJECT / "app.py"), "--sandbox", "--run-id", run_id,
                        f"--question={args.question}", "--lecture", args.lecture,
                        "--route", args.route, "--timeout", str(args.timeout)]
        if args.verbose and not args.check:
            command.append("--verbose")
        if not args.check and args.session is not None:
            command += ["--session", args.session]
        if not args.check and args.no_memory:
            command.append("--no-memory")
        if not args.check and args.no_observability:
            command.append("--no-observability")

        print(f"Запускаю процесс в песочнице {BOX_NAME}…", flush=True)
        result = _run_hidden(command, 5 * (args.timeout + 60) + 120)
        if not report_path.is_file():
            _print_captured(result)
            raise LauncherError(f"Sandboxie не создал ожидаемый отчёт {report_path}. Код Start.exe: {result.returncode}. Stderr: {result.stderr.strip() or '(пусто)'}")
        report = _read_json(report_path)
        if report.get("run_id") != run_id:
            raise LauncherError("Идентификатор отчёта не совпадает с текущим запуском.")
        execution = report.get("execution")
        sandbox = execution.get("sandbox") if isinstance(execution, dict) else None
        if not isinstance(sandbox, dict) or sandbox.get("sandboxed") is not True or sandbox.get("box_name") != BOX_NAME:
            raise LauncherError(f"Отчёт не подтверждает запуск в боксе {BOX_NAME}: {report_path}")
        if execution.get("model_transport") != "gateway":
            raise LauncherError(f"Отчёт не подтверждает model_transport='gateway': {report_path}")
        if args.check:
            after_hash = _host_controls()
            host_passed = after_hash == baseline_hash
            for directory in ("runs", "results"):
                target = PROJECT / directory / f"probe-{run_id}.txt"
                if not target.resolve().is_relative_to(PROJECT):
                    raise LauncherError("Контрольный путь результатов выходит за пределы проекта.")
                if not target.is_file() or target.read_text(encoding="utf-8") != "sandbox write test":
                    host_passed = False
                else:
                    target.unlink()
            database = PROJECT / "runs" / f"probe-{run_id}.sqlite3"
            if not database.resolve().is_relative_to(PROJECT / "runs") or not database.is_file():
                host_passed = False
            else:
                connection = sqlite3.connect(database)
                try:
                    host_passed = host_passed and connection.execute("SELECT value FROM memory_probe").fetchone() == ("sandbox sqlite test",)
                except sqlite3.Error:
                    host_passed = False
                finally:
                    connection.close()
                database.unlink()
            report["host_verification"] = {"services_available_before_and_after": True, "controls_passed": host_passed}
            report["passed"] = report.get("passed") is True and host_passed
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            checks = report.get("checks", [])
            print("Проверки:")
            print(json.dumps(checks, ensure_ascii=False, indent=2))
            print(f"Отчёт: {report_path}")
            return 0 if report.get("passed") is True else 1

        from lecture_assistant.result_format import render_outcome

        outcome = report.get("outcome")
        if not isinstance(outcome, dict):
            raise LauncherError(f"В отчёте отсутствует execution outcome: {report_path}")
        if args.verbose and result.stdout:
            _print_captured(result)
        else:
            print(render_outcome(outcome, verbose=args.verbose))
        print(f"Запись выполнения: {report_path}")
        return 1 if outcome.get("status") == "technical_error" else 0
    except (LauncherError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
