"""Параметры локального шлюза и чтение его текущего сеанса."""
import json
import re
from pathlib import Path

GATEWAY_URL = "http://127.0.0.1:18734"
CONTROL_URL = "http://127.0.0.1:18735/control"
PROTOCOL_VERSION = 1


def load_gateway_runtime(project_dir: Path) -> dict:
    try:
        with (project_dir / ".gateway.json").open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError("Слишком большой файл сеанса шлюза")
        runtime = json.loads(raw)
        if not isinstance(runtime, dict) or runtime.get("version") != PROTOCOL_VERSION or runtime.get("base_url") != GATEWAY_URL:
            raise ValueError("Неверный адрес или версия шлюза")
        if not isinstance(runtime.get("token"), str) or not re.fullmatch(r"[0-9a-f]{64}", runtime["token"]):
            raise ValueError("Неверный токен сеанса шлюза")
        if not isinstance(runtime.get("instance_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", runtime["instance_id"]):
            raise ValueError("Неверный идентификатор шлюза")
        return runtime
    except (OSError, ValueError) as exc:
        raise ValueError("Нет действующего сеанса шлюза. Сначала запустите model_gateway.py вне Sandboxie.") from exc
