"""Ограниченный локальный шлюз: только выбранные модели и фиксированные параметры CPU."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from lecture_assistant.gateway_config import GATEWAY_URL, PROTOCOL_VERSION
from lecture_assistant.llm_client import ModelError, OllamaClient

ROOT = Path(__file__).resolve().parent
OPTIONS = {"num_gpu": 0, "num_thread": 4, "num_ctx": 4096, "num_predict": 512, "temperature": 0, "seed": 42}
MAX_BODY = 128 * 1024


def validate_schema(value, depth=0):
    if depth > 32:
        raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Схема ответа слишком глубоко вложена.")
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"} and (not isinstance(child, str) or not child.startswith("#/")):
                raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Во внешние схемы, файлы и адреса ссылаться нельзя.")
            validate_schema(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            validate_schema(child, depth + 1)


class GatewayError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code = status, code
        super().__init__(message)


class Gateway:
    def __init__(self, upstream, timeout=300):
        self.upstream = upstream
        self.allowed = {spec.name for spec in upstream.specs.values()}
        self.thinking = {spec.name: spec.disable_thinking for spec in upstream.specs.values()}
        self.timeout = timeout
        self.lock = threading.Lock()
        self.active_model = None

    def _model(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str) or payload["model"] not in self.allowed:
            raise GatewayError(403, "GATEWAY_MODEL_NOT_ALLOWED", "Шлюз разрешает только четыре настроенные модели проекта.")
        return payload["model"]

    @staticmethod
    def _fields(payload, allowed):
        if set(payload) - set(allowed):
            raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Переданы запрещённые параметры запроса.")

    def _models(self, endpoint):
        return self.upstream._models(endpoint)

    def dispatch(self, method, path, payload=None):
        if method == "POST" and path == "/api/route":
            from lecture_assistant.router_runner import run_router
            if not isinstance(payload, dict) or set(payload) != {"question"} or not isinstance(payload["question"], str) or not payload["question"].strip() or len(payload["question"]) > 500:
                raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Для маршрутизации нужен только вопрос до 500 символов.")
            if not self.lock.acquire(blocking=False):
                raise GatewayError(409, "GATEWAY_BUSY", "Шлюз занят другим запросом.")
            try:
                if self._models("ps"):
                    raise GatewayError(409, "MODEL_BUSY", "Сначала завершите использование загруженной модели.")
                return run_router(payload["question"])
            except (OSError, RuntimeError, ValueError) as exc:
                raise ModelError("ROUTER_UNAVAILABLE", str(exc)) from exc
            finally:
                self.lock.release()
        if method == "GET" and path == "/api/tags":
            return {"models": [item for item in self._models("tags") if item["name"] in self.allowed]}
        if method == "GET" and path == "/api/ps":
            # Чужая загруженная модель тоже должна остановить запуск, а не быть скрыта.
            return {"models": [{"name": item["name"]} for item in self._models("ps")]}
        if method != "POST" or path not in {"/api/show", "/api/chat", "/api/generate"}:
            raise GatewayError(403, "GATEWAY_ENDPOINT_NOT_ALLOWED", "Эта операция Ollama через шлюз недоступна.")
        name = self._model(payload)
        if path == "/api/show":
            self._fields(payload, {"model"})
            info = self.upstream._request("show", {"model": name})
            return {"details": info.get("details"), "capabilities": info.get("capabilities", [])}
        if path == "/api/generate":
            self._fields(payload, {"model", "keep_alive"})
            if type(payload.get("keep_alive")) is not int or payload["keep_alive"] != 0:
                raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Разрешено только подтверждение выгрузки модели.")
            if not self.lock.acquire(blocking=False):
                raise GatewayError(409, "GATEWAY_BUSY", "Шлюз ещё выполняет запрос.")
            try:
                if any(item["name"] == name for item in self._models("ps")):
                    # Не выгружаем модель, которую могли загрузить вне этого шлюза.
                    raise GatewayError(409, "MODEL_UNLOAD_FAILED", "Модель остаётся загруженной; проверьте ollama ps.")
                return {"model": name, "done": True, "done_reason": "unload"}
            finally:
                self.lock.release()
        self._fields(payload, {"model", "messages", "format", "stream", "keep_alive", "options", "think"})
        if payload.get("stream") is not False or type(payload.get("keep_alive")) is not int or payload["keep_alive"] != 0:
            raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Нужны непотоковый ответ и немедленная выгрузка модели.")
        if payload.get("options") != OPTIONS:
            raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Параметры запуска должны совпадать с фиксированными настройками CPU.")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 4:
            raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Неверный список сообщений.")
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"role", "content"} or not isinstance(message["role"], str) or message["role"] not in {"system", "user", "assistant"} or not isinstance(message["content"], str) or len(message["content"]) > 32000:
                raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Разрешены только текстовые сообщения без файлов и изображений.")
        if not isinstance(payload.get("format"), dict) or payload["format"].get("type") != "object":
            raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Требуется схема структурированного ответа.")
        validate_schema(payload["format"])
        if "think" in payload and payload["think"] is not False:
            raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Изменение режима рассуждений запрещено.")
        if not self.lock.acquire(blocking=False):
            raise GatewayError(409, "GATEWAY_BUSY", "Шлюз уже обрабатывает другой запрос.")
        error, result, owned = None, None, False
        try:
            if self._models("ps"):
                raise GatewayError(409, "MODEL_BUSY", "В Ollama уже загружена модель. Сначала завершите её использование.")
            request = {**payload, "options": dict(OPTIONS)}
            if self.thinking[name]:
                request["think"] = False
            elif "think" in request:
                del request["think"]
            owned = True
            self.active_model = name
            result = self.upstream._request("chat", request, timeout=self.timeout)
        except (ModelError, GatewayError) as exc:
            error = exc
        finally:
            try:
                if owned:
                    self.upstream._request("generate", {"model": name, "keep_alive": 0}, timeout=30)
                    if any(item["name"] == name for item in self._models("ps")):
                        raise ModelError("MODEL_UNLOAD_FAILED", "Модель не выгружена после запроса шлюза.")
            except ModelError as exc:
                error = ModelError("MODEL_UNLOAD_FAILED", f"Не удалось подтвердить выгрузку модели: {exc}")
            finally:
                self.active_model = None
                self.lock.release()
        if error:
            raise error
        return result


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, gateway, token, instance_id, control=False):
        self.gateway, self.token, self.instance_id, self.control = gateway, token, instance_id, control
        super().__init__(address, GatewayHandler)


class GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Не записываем тексты лекций, токены доступа и промпты в консоль шлюза.
        pass

    def send_json(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle(self, method):
        started = time.perf_counter()
        try:
            if self.server.control:
                if method == "GET" and self.path == "/control":
                    self.send_json(200, {"fixture": "lecture-gateway-control"})
                else:
                    self.send_json(404, {"error": "unknown control endpoint"})
                return
            if method == "GET" and self.path == "/health":
                self.send_json(200, {"service": "lecture-model-gateway", "version": PROTOCOL_VERSION, "instance_id": self.server.instance_id, "capabilities": ["chat", "route"]})
                return
            expected = "Bearer " + self.server.token
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8"), expected.encode("ascii")):
                raise GatewayError(401, "GATEWAY_AUTH_FAILED", "Нет доступа к текущему сеансу шлюза.")
            payload = None
            if method == "POST":
                if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                    raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Ожидается JSON с указанной длиной.")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        raise ValueError("body length")
                    self.connection.settimeout(10)
                    payload = json.loads(self.rfile.read(length))
                except (ValueError, OSError, RecursionError) as exc:
                    raise GatewayError(400, "GATEWAY_INVALID_REQUEST", "Неверный JSON или размер запроса.") from exc
            result = self.server.gateway.dispatch(method, self.path, payload)
            self.send_json(200, result)
            if self.path == "/api/chat":
                print(f"Генерация завершена; модель: {payload['model']}; время: {time.perf_counter() - started:.1f} с", flush=True)
        except GatewayError as exc:
            self.send_json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except ModelError as exc:
            self.send_json(502, {"error": {"code": exc.code, "message": str(exc)}})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main():
    from lecture_assistant.sandbox_runtime import get_sandbox_info
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, choices=range(10, 601), default=300, metavar="10..600")
    args = parser.parse_args()
    if get_sandbox_info()["sandboxed"]:
        raise RuntimeError("Шлюз нужно запускать в обычном терминале Windows, вне Sandboxie.")
    upstream = OllamaClient(ROOT / "models.json", timeout=args.timeout)
    upstream._request("version")
    gateway = Gateway(upstream, args.timeout)
    token, instance_id = secrets.token_hex(32), uuid4().hex
    runtime_file = ROOT / ".gateway.json"
    with GatewayServer(("127.0.0.1", 18734), gateway, token, instance_id) as server:
        with GatewayServer(("127.0.0.1", 18735), gateway, token, instance_id, control=True) as control:
            runtime = {"version": PROTOCOL_VERSION, "base_url": GATEWAY_URL, "token": token, "instance_id": instance_id, "pid": os.getpid(), "allowed_models": sorted(gateway.allowed)}
            runtime_file.write_text(json.dumps(runtime, indent=2), encoding="utf-8")
            thread = threading.Thread(target=control.serve_forever, daemon=True)
            thread.start()
            print("Шлюз готов: 127.0.0.1:18734. Контрольный сервис: 127.0.0.1:18735. Остановка: Ctrl+C.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                control.shutdown()
                thread.join(timeout=5)
                try:
                    current = json.loads(runtime_file.read_text(encoding="utf-8"))
                    if current.get("instance_id") == instance_id:
                        runtime_file.unlink()
                except (OSError, ValueError):
                    pass


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ModelError) as exc:
        print(f"Не удалось запустить шлюз: {exc}", flush=True)
        raise SystemExit(1)
