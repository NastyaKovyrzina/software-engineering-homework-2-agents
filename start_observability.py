"""Запуск локального Phoenix и русскоязычной панели вне Sandboxie."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import psutil

from lecture_assistant.sandbox_runtime import get_sandbox_info

ROOT = Path(__file__).resolve().parent


def stop_child(child):
    """Завершить только созданный нами сервер и его дочерние процессы Windows."""
    try:
        descendants = psutil.Process(child.pid).children(recursive=True)
    except psutil.Error:
        descendants = []
    for process in reversed(descendants):
        try:
            process.terminate()
        except psutil.Error:
            pass
    if child.poll() is None:
        child.terminate()
    _, alive = psutil.wait_procs(descendants, timeout=5)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            pass


def main():
    if get_sandbox_info()["sandboxed"]:
        raise RuntimeError("Запустите start_observability.py в обычном терминале Windows")
    python = ROOT / ".observability-venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise RuntimeError("Нет .observability-venv. Восстановите окружение по docs/OBSERVABILITY.md")
    for port in (6006, 18736):
        with socket.socket() as connection:
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"Порт {port} уже занят. Возможно, панель уже работает; проверьте её терминал.")
    directory = ROOT / "runs" / "telemetry"
    if not directory.resolve().is_relative_to(ROOT / "runs"):
        raise RuntimeError("Папка телеметрии находится вне runs")
    directory.mkdir(parents=True, exist_ok=True)
    phoenix_dir = directory / "phoenix"
    phoenix_dir.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"PHOENIX_WORKING_DIR": str(phoenix_dir), "PHOENIX_SQL_DATABASE_URL": "sqlite:///" + (phoenix_dir / "phoenix.db").as_posix(),
                "PHOENIX_HOST": "127.0.0.1", "PHOENIX_PORT": "6006", "PHOENIX_ENABLE_AUTH": "false",
                "PHOENIX_ALLOW_EXTERNAL_RESOURCES": "false", "PHOENIX_TELEMETRY_ENABLED": "false",
                "PHOENIX_DISABLE_MODEL_COST_AUTO_UPDATE": "true", "PHOENIX_DEFAULT_RETENTION_POLICY_DAYS": "14",
                "PHOENIX_ENABLE_PROMETHEUS": "false", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    children = []
    with (directory / "phoenix-server.log").open("w", encoding="utf-8") as log:
        try:
            phoenix = subprocess.Popen([str(python), "-B", "-X", "utf8", "-m", "phoenix.server.main", "serve"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            children.append(phoenix)
            panel = subprocess.Popen([str(python), "-B", "-X", "utf8", str(ROOT / "telemetry_service.py")], cwd=ROOT, env=env, creationflags=flags)
            children.append(panel)
            http = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + 90
            ready = False
            while time.monotonic() < deadline:
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("Один из серверов завершился. Проверьте runs/telemetry/phoenix-server.log")
                try:
                    with http.open("http://127.0.0.1:6006", timeout=2) as response:
                        phoenix_ready = response.status == 200
                    with http.open("http://127.0.0.1:18736/health", timeout=2) as response:
                        panel_ready = json.load(response).get("service") == "lecture-observability"
                    if phoenix_ready and panel_ready:
                        ready = True
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(1)
            if not ready:
                raise RuntimeError("Серверы не стали доступны за 90 секунд; проверьте журнал Phoenix")
            print("Готово. Панель: http://127.0.0.1:18736; трейсы Phoenix: http://127.0.0.1:6006", flush=True)
            print("Оставьте терминал работающим. Для остановки обоих серверов нажмите Ctrl+C.", flush=True)
            while all(child.poll() is None for child in children):
                time.sleep(1)
            raise RuntimeError("Один из серверов остановился; наблюдаемость завершает работу")
        except KeyboardInterrupt:
            pass
        finally:
            for child in reversed(children):
                stop_child(child)
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Не удалось запустить наблюдаемость: {exc}", file=sys.stderr)
        raise SystemExit(1)
