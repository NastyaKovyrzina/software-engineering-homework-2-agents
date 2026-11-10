"""Последовательные локальные вызовы Ollama с проверкой структурированных ответов."""

from __future__ import annotations

import json
import socket
import time
from http.client import HTTPException
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from lecture_assistant.gateway_config import GATEWAY_URL, load_gateway_runtime


class ModelSpec(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    role_id: Literal["search", "simple", "complex", "review"]
    name: str
    role: str
    disable_thinking: bool


class ModelError(Exception):
    def __init__(self, code: str, message: str, event: dict | None = None):
        self.code = code
        self.event = event
        super().__init__(message)


@dataclass
class ModelReply:
    parsed: BaseModel
    event: dict


class OllamaClient:
    def __init__(self, models_path: Path, timeout: int = 300, *, transport: str = "direct"):
        try:
            records = json.loads(models_path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                raise ValueError("Ожидается список моделей")
            specs = [ModelSpec.model_validate(record) for record in records]
            self.specs = {spec.role_id: spec for spec in specs}
            if len(specs) != 4 or set(self.specs) != {"search", "simple", "complex", "review"}:
                raise ValueError("Нужны четыре разные роли: search, simple, complex, review")
        except (OSError, ValueError, ValidationError) as exc:
            raise ModelError("MODEL_CONFIG_ERROR", f"Не удалось прочитать настройки моделей: {exc}") from exc
        self.timeout = timeout
        self.transport = transport
        self.api_url = "http://127.0.0.1:11434/api"
        self.headers = {"Content-Type": "application/json"}
        if transport == "gateway":
            try:
                runtime = load_gateway_runtime(models_path.parent)
            except ValueError as exc:
                raise ModelError("GATEWAY_UNAVAILABLE", str(exc)) from exc
            self.api_url = GATEWAY_URL + "/api"
            self.headers["Authorization"] = "Bearer " + runtime["token"]
        elif transport != "direct":
            raise ModelError("MODEL_CONFIG_ERROR", "Неизвестный способ подключения к моделям.")
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._info: dict[str, dict] = {}

    def _request(self, endpoint: str, payload: dict | None = None, timeout: int = 30) -> dict:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_url}/{endpoint}", data=data, headers=self.headers,
        )
        try:
            with self.http.open(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ModelError("MODEL_RESPONSE_TOO_LARGE", "Ответ сервера превышает технический предел 1 МиБ.")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("Сервер вернул не объект JSON")
            return result
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            if self.transport == "gateway":
                try:
                    error = json.loads(detail)["error"]
                    code, message = error["code"], error["message"]
                    if isinstance(code, str) and code.startswith(("GATEWAY_", "MODEL_", "ROUTER_")) and isinstance(message, str):
                        raise ModelError(code, message) from exc
                except (ValueError, KeyError, TypeError):
                    pass
            if exc.code == 404 and endpoint in {"chat", "show"} and payload and payload.get("model"):
                raise ModelError("MODEL_NOT_INSTALLED", f"Модель {payload['model']} не найдена в Ollama.") from exc
            raise ModelError("MODEL_HTTP_ERROR", f"Ollama вернула HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ModelError("MODEL_TIMEOUT", "Истекло время ожидания ответа Ollama.") from exc
        except urllib.error.URLError as exc:
            if self.transport == "gateway":
                raise ModelError("GATEWAY_UNAVAILABLE", "Нет соединения со шлюзом на 127.0.0.1:18734. Запустите model_gateway.py вне Sandboxie.") from exc
            raise ModelError("MODEL_UNAVAILABLE", "Нет соединения с Ollama на 127.0.0.1:11434. Запустите приложение Ollama.") from exc
        except (OSError, HTTPException) as exc:
            raise ModelError("MODEL_CONNECTION_ERROR", "Соединение с Ollama прервалось во время запроса.") from exc
        except (ValueError, UnicodeError) as exc:
            raise ModelError("MODEL_INVALID_RESPONSE", "Ollama вернула некорректный JSON.") from exc

    def model_name(self, role: str) -> str:
        return self.specs[role].name

    def _models(self, endpoint: str) -> list[dict]:
        models = self._request(endpoint).get("models")
        if not isinstance(models, list) or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in models):
            raise ModelError("MODEL_INVALID_RESPONSE", "Ollama вернула некорректный список моделей.")
        return models

    def _model_info(self, role: str) -> dict:
        name = self.model_name(role)
        if name not in self._info:
            installed = {item["name"]: item for item in self._models("tags")}
            if name not in installed:
                raise ModelError("MODEL_NOT_INSTALLED", f"Модель {name} не установлена. Проверьте ollama list.")
            info = self._request("show", {"model": name})
            details = info.get("details")
            if not isinstance(details, dict) or not isinstance(details.get("family"), str) or not details["family"]:
                raise ModelError("MODEL_INVALID_RESPONSE", f"Не удалось определить семейство {name}.")
            if not isinstance(installed[name].get("digest"), str):
                raise ModelError("MODEL_INVALID_RESPONSE", f"Не удалось определить идентификатор {name}.")
            self._info[name] = {"digest": installed[name]["digest"], "details": details}
        return self._info[name]

    def family(self, role: str) -> str:
        return self._model_info(role)["details"]["family"]

    def route_question(self, question: str) -> dict:
        if self.transport == "gateway":
            return self._request("route", {"question": question}, timeout=90)
        from lecture_assistant.router_runner import run_router
        try:
            if self._models("ps"):
                raise ModelError("MODEL_BUSY", "Сначала завершите использование загруженной модели.")
            return run_router(question)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ModelError("ROUTER_UNAVAILABLE", str(exc)) from exc

    def chat(self, role: str, schema: type[BaseModel], system: str, context: dict, json_schema: dict | None = None) -> ModelReply:
        spec = self.specs[role]
        event = {"kind": "model", "role": role, "model": spec.name, "status": "started", "transport": self.transport}
        started = time.perf_counter()
        owned = False
        error = None
        parsed = None
        try:
            info = self._model_info(role)
            event.update(model_digest=info["digest"], family=info["details"]["family"], quantization=info["details"].get("quantization_level"))
            if self._models("ps"):
                raise ModelError("MODEL_BUSY", "В Ollama уже загружена модель. Завершите её использование и выгрузите через ollama stop ИМЯ.")
            json_schema = json_schema if json_schema is not None else schema.model_json_schema()
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"data": context, "response_schema": json_schema}, ensure_ascii=False)},
            ]
            event["messages"] = messages
            options = {"num_gpu": 0, "num_thread": 4, "num_ctx": 4096, "num_predict": 512, "temperature": 0, "seed": 42}
            event["options"] = options
            payload = {"model": spec.name, "messages": messages, "format": json_schema, "stream": False, "keep_alive": 0, "options": options}
            if spec.disable_thinking:
                payload["think"] = False
            owned = True
            response = self._request("chat", payload, timeout=self.timeout)
            message = response.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise ModelError("MODEL_INVALID_RESPONSE", "В ответе Ollama отсутствует текст сообщения.")
            content = message["content"]
            event.update(output=content, done_reason=response.get("done_reason"), prompt_tokens=response.get("prompt_eval_count"), output_tokens=response.get("eval_count"))
            for field in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
                if type(response.get(field)) is int:
                    event[field + "_seconds"] = response[field] / 1_000_000_000
            if type(response.get("eval_count")) is int and type(response.get("eval_duration")) is int and response["eval_duration"] > 0:
                event["generation_tokens_per_second"] = response["eval_count"] * 1_000_000_000 / response["eval_duration"]
            if not response.get("done") or response.get("done_reason") != "stop":
                raise ModelError("MODEL_INCOMPLETE_OUTPUT", "Ответ модели не завершён: возможно, исчерпан лимит токенов.")
            try:
                parsed = schema.model_validate_json(content)
            except (ValidationError, ValueError) as exc:
                raise ModelError("MODEL_INVALID_OUTPUT", "Ответ модели не соответствует требуемой структуре JSON.") from exc
        except ModelError as exc:
            error = exc
        finally:
            if owned:
                try:
                    self._request("generate", {"model": spec.name, "keep_alive": 0}, timeout=30)
                    if any(item["name"] == spec.name for item in self._models("ps")):
                        raise ModelError("MODEL_UNLOAD_FAILED", "Проверяемая модель осталась в памяти.")
                    event["unloaded"] = True
                except ModelError as exc:
                    event["unloaded"] = False
                    event["unload_error"] = str(exc)
                    error = ModelError("MODEL_UNLOAD_FAILED", "Не удалось подтвердить выгрузку модели. Проверьте ollama ps.")
            event["duration_seconds"] = round(time.perf_counter() - started, 3)
        if error is not None:
            event.update(status="error", error_code=error.code, error_message=str(error))
            error.event = event
            raise error
        event["status"] = "completed"
        return ModelReply(parsed, event)
