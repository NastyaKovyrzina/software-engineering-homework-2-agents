# Локальный учебный помощник

Локальный MVP: поиск по лекции, ответ из дословных цитат, проверка другой моделью и одна попытка исправления. Windows, Python 3.12, Ollama, LangGraph и Sandboxie-Plus.

## Подготовка

Откройте скачанную папку проекта в VS Code. Все команды ниже — PowerShell из её корня. При первом запуске выполните [установку](docs/SETUP.md).

## Запуск

Запустите Ollama. В отдельных терминалах оставьте работающими шлюз и наблюдаемость:

```powershell
.\.venv\Scripts\python.exe model_gateway.py
```

```powershell
.\.venv\Scripts\python.exe start_observability.py
```

В третьем терминале:

```powershell
.\.venv\Scripts\python.exe run_sandbox.py --check
.\.venv\Scripts\python.exe run_sandbox.py --lecture lecture_demo.md --question 'Что такое изоляция?' --session demo
```

Свою лекцию UTF-8 (`.txt`/`.md`) поместите в `data/lectures/` и передайте её имя через `--lecture`. Одинаковый `--session` связывает вопросы. Серверы останавливаются через Ctrl+C.

- [Русскоязычная панель](http://127.0.0.1:18736) · [Phoenix](http://127.0.0.1:6006).
- Ответы: `results/`; журналы и память: `runs/`.
- [Структура](docs/PROJECT_STRUCTURE.md) · [Граф](docs/GRAPH.md) · [Проверки](docs/CONTINUOUS_CHECKS.md) · [Evals](docs/EVALS.md) · [Требования ТЗ](docs/TZ-CHECKLIST.md).

Ответ цитирует лекцию, но его соответствие вопросу зависит от качества поиска и проверяющей модели. Свободный пересказ пока не реализован.
