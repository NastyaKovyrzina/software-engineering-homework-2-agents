# Установка

Команды выполняются из корня скачанного проекта в обычном PowerShell, вне песочницы.

## 1. Окружения

Установите Python 3.12, Ollama и Sandboxie-Plus. Используется стандартная установка Sandboxie-Plus в `C:\Program Files\Sandboxie-Plus`.

Разместите проект вне пользовательских `Documents` и `Downloads`: профиль песочницы закрывает эти каталоги. Путь к проекту может быть любым другим доступным каталогом.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
py -3.12 -m venv .observability-venv
.\.observability-venv\Scripts\python.exe -m pip install -r requirements-observability.txt
```

## 2. Модели Ollama

Запустите Ollama и загрузите модели, перечисленные в `models.json`:

```powershell
ollama pull hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M
ollama cp hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M se-qwen05:latest
ollama pull qwen3:0.6b-q4_K_M
ollama pull qwen3.5:2b-q4_K_M
ollama pull gemma3:1b-it-q4_K_M
ollama list
```

`se-qwen05:latest` — локальное имя поисковой модели. Если оно уже импортировано из GGUF, повторный импорт не нужен. [Источник Qwen2.5 GGUF](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF).

## 3. Классификатор маршрута

В `router_settings.json` задайте `directory`: абсолютный путь к папке для весов MiniLM на своём компьютере. Затем:

```powershell
.\.venv\Scripts\python.exe scripts\setup_router.py
```

Интернет требуется для установки пакетов и загрузки весов; подготовленное приложение работает локально.

## 4. Sandboxie

Создайте в Sandboxie-Plus отдельную стандартную песочницу `LectureIsolationLab`. Завершите её процессы перед настройкой:

```powershell
.\.venv\Scripts\python.exe configure_sandbox.py
.\.venv\Scripts\python.exe configure_sandbox.py --apply
```

Первая команда показывает план правил, вторая применяет его. Правила строятся для текущего расположения проекта. После переноса папки их нужно настроить заново; старые разрешения при конфликте удалите в профиле этой песочницы.

Если хранилище Ollama перенесено, перед настройкой задайте переменную среды `OLLAMA_MODELS` с его фактическим путём.

Далее выполните [запуск и проверку](../README.md#запуск). Проверка `run_sandbox.py --check` должна завершиться успешно, включая внешнюю проверку контрольных файлов.
