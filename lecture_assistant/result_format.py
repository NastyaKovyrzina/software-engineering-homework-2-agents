"""Единый пользовательский результат и его представление в терминале."""

from __future__ import annotations


INSUFFICIENT = {
    "requested_year_not_found": (
        "Вопрос требует календарный год, но в найденных фрагментах нет указания года.",
        ["Проверьте, содержит ли лекция нужную дату, или уточните запрос."],
    ),
    "no_matching_fragments": (
        "Поиск не нашёл фрагментов с ключевыми словами запроса.",
        ["Переформулируйте вопрос, используя термины из лекции.", "Проверьте, что выбрана нужная лекция."],
    ),
    "writer_abstained": (
        "Фрагменты найдены, но модель не смогла выбрать из них подтверждённый ответ.",
        ["Задайте более узкий вопрос о конкретном термине или разделе."],
    ),
    "verification_failed_after_revision": (
        "Ответ не прошёл все проверки после одной попытки исправления и не будет показан как готовый.",
        ["Уточните вопрос или укажите конкретный термин из лекции."],
    ),
}

ERRORS = {
    "MEMORY_STORAGE_ERROR": ("Не удалось прочитать или обновить локальную память.", "Проверьте права записи и свободное место в runs/memory. Для отдельного запроса без памяти можно указать --no-memory."),
    "MEMORY_SOURCE_MISMATCH": ("Сохранённая память не соответствует текущему источнику.", "Проверьте версию лекции. Кеш очищается командой memory_cli.py clear-cache; отдельный запрос можно выполнить с --no-memory."),
    "INSTRUCTIONS_ERROR": ("Не удалось загрузить инструкции агентов.", "Проверьте папку instructions, manifest.json, кодировку UTF-8 и права чтения. Затем запустите новый запрос."),
    "SANDBOX_REQUIRED": ("Процесс запущен вне нужной песочницы.", "Запустите приложение командой run_sandbox.py из обычного терминала Windows."),
    "SANDBOX_MODE_REQUIRED": ("Для работы в песочнице нужен ограниченный шлюз моделей.", "Используйте run_sandbox.py, который включает режим --sandbox."),
    "GATEWAY_UNAVAILABLE": ("Не удалось подключиться к локальному шлюзу моделей.", "В отдельном обычном терминале Windows запустите model_gateway.py и оставьте его работающим."),
    "GATEWAY_AUTH_FAILED": ("Сеанс шлюза изменился или устарел.", "Завершите текущий запуск и заново запустите run_sandbox.py при работающем model_gateway.py."),
    "GATEWAY_BUSY": ("Шлюз занят обработкой другого запроса.", "Дождитесь его завершения и повторите запуск."),
    "GATEWAY_ENDPOINT_NOT_ALLOWED": ("Запрошенная операция недоступна через шлюз.", "Используйте предусмотренный процесс app.py; установка и удаление моделей выполняются вне песочницы."),
    "GATEWAY_MODEL_NOT_ALLOWED": ("Модель не входит в разрешённый список шлюза.", "Проверьте models.json и перезапустите шлюз после изменения списка моделей."),
    "FILE_NOT_FOUND": ("Файл выбранной лекции не найден.", "Поместите файл в data/lectures и проверьте его имя в --lecture."),
    "UNSUPPORTED_FORMAT": ("Формат лекции не поддерживается.", "Используйте текстовый файл .txt или .md."),
    "INVALID_ENCODING": ("Не удалось прочитать кодировку лекции.", "Пересохраните файл в кодировке UTF-8."),
    "EMPTY_DOCUMENT": ("Файл лекции пуст.", "Добавьте текст лекции и повторите запрос."),
    "INVALID_TEXT": ("Файл не похож на обычный текстовый документ.", "Сохраните лекцию как текст в UTF-8, а не переименовывайте PDF или другой двоичный файл."),
    "FILE_TOO_LARGE": ("Лекция превышает текущий предел 2 МиБ.", "Разделите материал на несколько файлов и выберите нужный."),
    "PATH_NOT_ALLOWED": ("Выбранный файл или папка находятся за разрешёнными границами.", "Используйте файл внутри data/lectures и передавайте только его имя."),
    "STALE_DOCUMENT": ("Лекция изменилась во время обработки вопроса.", "Сохраните изменения и заново запустите запрос."),
    "INVALID_ARGUMENT": ("Не удалось обработать параметры запроса.", "Проверьте вопрос: от 1 до 500 символов, и параметры команды запуска."),
    "IO_ERROR": ("Не удалось прочитать или сохранить файлы проекта.", "Проверьте доступность лекции и права чтения и записи в папке проекта."),
    "MODEL_UNAVAILABLE": ("Не удалось подключиться к локальному серверу моделей.", "Запустите приложение Ollama в Windows и повторите запрос."),
    "MODEL_CONNECTION_ERROR": ("Соединение с сервером моделей прервалось.", "Проверьте, что Ollama работает, и повторите запрос."),
    "MODEL_TIMEOUT": ("Модель не ответила за отведённое время.", "Закройте ненужные программы или увеличьте ожидание параметром --timeout 600."),
    "MODEL_NOT_INSTALLED": ("Одна из нужных моделей не установлена.", "Проверьте ollama list и наличие моделей, указанных в models.json."),
    "MODEL_BUSY": ("В Ollama уже загружена модель для другой задачи.", "Дождитесь завершения той задачи. Проверьте ollama ps перед повторным запуском."),
    "MODEL_UNLOAD_FAILED": ("Не удалось подтвердить выгрузку модели из памяти.", "Проверьте ollama ps. Перед новым запуском завершите её использование и выполните ollama stop ИМЯ_МОДЕЛИ."),
    "MODEL_CONFIG_ERROR": ("Не удалось прочитать настройки моделей.", "Проверьте models.json и запись подробностей запроса."),
    "REVIEWER_FAMILY_CONFLICT": ("Модель проверки относится к тому же семейству, что и модель ответа.", "Выберите в models.json проверяющую модель другого семейства, например Gemma для ответа Qwen."),
    "RUN_SAVE_FAILED": ("Не удалось сохранить запись выполнения запроса.", "Проверьте свободное место и права записи в папку runs, затем повторите запрос."),
    "INVALID_FINAL_RESULT": ("Приложение не сформировало согласованный конечный результат.", "Откройте запись запроса для поиска причины. Готовый ответ не выдаётся."),
}
for _code in ("MODEL_INVALID_OUTPUT", "MODEL_INVALID_RESPONSE", "MODEL_INCOMPLETE_OUTPUT", "MODEL_RESPONSE_TOO_LARGE"):
    ERRORS[_code] = ("Ответ модели не удалось обработать в требуемом формате.", "Повторите запрос. Если ошибка повторяется, откройте запись выполнения и проверьте выбранную модель.")


def make_outcome(state: dict, lecture: str) -> dict:
    """Готовый ответ берётся только из result, никогда из черновика или замечаний модели."""
    result = state.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    status = result.get("status")
    error = result.get("error") or state.get("error")
    invalid = status not in {"answered", "insufficient_information", "technical_error"} or status != state.get("status")
    sources = []
    if status == "answered" and not invalid:
        verification = result.get("semantic_verification") or {}
        review = state.get("review") or {}
        citations = result.get("citations")
        invalid = not (
            isinstance(result.get("answer"), str) and result["answer"].strip()
            and isinstance(citations, list) and citations
            and isinstance(review, dict) and review.get("approved") is True
            and isinstance(verification, dict) and verification.get("status") == "model_approved"
        )
        if not invalid:
            for index, citation in enumerate(citations, 1):
                if not isinstance(citation, dict):
                    invalid = True
                    break
                source = citation.get("source") or {}
                if not (
                    isinstance(source, dict) and isinstance(citation.get("quote"), str) and citation["quote"].strip()
                    and isinstance(source.get("file"), str) and isinstance(source.get("section"), str)
                    and type(source.get("line_start")) is int and type(source.get("line_end")) is int
                    and 1 <= source["line_start"] <= source["line_end"]
                ):
                    invalid = True
                    break
                sources.append({**source, "number": index, "quote": citation["quote"]})
    if invalid:
        status = "technical_error"
        error = {"code": "INVALID_FINAL_RESULT", "message": "Конечное состояние, источники или отметки проверки не согласованы."}

    outcome = {
        "schema_version": 1, "status": status, "title": "", "question": state.get("question", ""),
        "lecture": lecture, "message": "", "answer": None, "sources": [],
        "reason_code": None, "next_steps": [],
        "verification": {"exact_quotes": False, "model_review": False, "reviewer_model": None},
        "corrections_used": state.get("revisions", 0), "error": None, "routing": state.get("routing"),
        "memory": state.get("memory_context"),
    }
    if status == "answered":
        outcome.update(
            title="Ответ по лекции", answer=result["answer"], sources=sources,
            message="Ответ составлен из дословных цитат. Проверка цитат и проверка моделью пройдены.",
            verification={"exact_quotes": True, "model_review": True, "reviewer_model": result["semantic_verification"].get("model")},
        )
    elif status == "insufficient_information":
        reason = result.get("reason") or state.get("reason")
        message, actions = INSUFFICIENT.get(reason, (
            "Не удалось получить достаточно подтверждённый ответ по найденным фрагментам.",
            ["Уточните вопрос и проверьте выбранную лекцию."],
        ))
        outcome.update(title="Недостаточно подтверждений", message=message, reason_code=reason, next_steps=list(actions))
    else:
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            error = {"code": "UNEXPECTED_ERROR", "message": "Причина не указана."}
        message, action = ERRORS.get(error["code"], (
            "Обработку запроса не удалось завершить из-за технической ошибки.",
            "Откройте запись запроса и подробности ошибки через --verbose.",
        ))
        outcome.update(title="Техническая ошибка", message=message, reason_code=error["code"], next_steps=[action], error=error)
    return outcome


def render_outcome(outcome: dict, *, verbose: bool = False) -> str:
    """Человекочитаемый вывод; подробности инфраструктуры доступны через --verbose."""
    lines = [f"Результат: {outcome['title']}", f"Лекция: {outcome['lecture']}", f"Вопрос: {outcome['question']}", ""]
    if outcome["status"] == "answered":
        lines += ["Ответ:", outcome["answer"], "", "Источники:"]
        for source in outcome["sources"]:
            span = str(source["line_start"]) if source["line_start"] == source["line_end"] else f"{source['line_start']}–{source['line_end']}"
            label = "строка" if source["line_start"] == source["line_end"] else "строки"
            lines.append(f"[{source['number']}] {source['file']}, раздел «{source['section']}», {label} {span}.")
            # В извлекающем режиме ответ уже содержит цитату; второй раз её не печатаем.
            if source["quote"] not in outcome["answer"]:
                lines.append(f"Цитата: «{source['quote']}»")
        lines += ["", outcome["message"]]
        if outcome["corrections_used"]:
            lines.append("Перед выдачей ответа выполнена одна попытка исправления и повторная проверка.")
    else:
        lines.append(outcome["message"])
    if outcome["next_steps"]:
        lines += ["", "Что можно сделать:"]
        lines += [f"- {action}" for action in outcome["next_steps"]]
    routing = outcome.get("routing") or {}
    if routing.get("warning"):
        lines += ["", f"Маршрутизация: {routing['warning']}"]
    if verbose:
        lines += ["", f"Статус: {outcome['status']}; исправлений: {outcome['corrections_used']}"]
        memory = outcome.get("memory") or {}
        if memory.get("enabled"):
            lines.append(f"Беседа: {memory['session_id']}; прошлых вопросов в контексте: {len(memory.get('recent_questions', []))}")
            if "facts_written" in memory:
                lines.append(f"Проверенных записей сохранено или обновлено: {memory['facts_written']}")
        if routing:
            labels = {"simple": "простая модель", "complex": "сложная модель"}
            lines.append(f"Выбранный маршрут: {labels.get(routing.get('route'), 'не определён')}; источник решения: {routing.get('source', 'не указан')}")
            if routing.get("model_id"):
                lines.append(f"Модель маршрутизатора: {routing['model_id']}")
            scores = routing.get("scores")
            if isinstance(scores, dict) and set(scores) == {"simple", "complex"}:
                lines.append(f"Оценки маршрутов: простой {scores['simple']:.3f}; сложный {scores['complex']:.3f} (не вероятность правильного ответа)")
        if outcome["reason_code"]:
            lines.append(f"Код причины: {outcome['reason_code']}")
        if outcome["error"]:
            lines.append(f"Подробности: {outcome['error']['message']}")
        if outcome["verification"]["reviewer_model"]:
            lines.append(f"Проверяющая модель: {outcome['verification']['reviewer_model']}")
    return "\n".join(lines)
