# Подготовка классификатора

Задайте каталог весов в `router_settings.json`, затем из корня проекта:

```powershell
.\.venv\Scripts\python.exe scripts\setup_router.py
```

Скрипт скачивает фиксированную ревизию MiniLM и квантует ONNX в INT8. Для обычного запуска повторять не нужно. Оценка: `eval_system.py --router`. Полная подготовка: [SETUP.md](../docs/SETUP.md).
