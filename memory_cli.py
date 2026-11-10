"""Просмотр и обслуживание локальной памяти; обращения к LLM не выполняются."""
import argparse
import json
import sys

from lecture_assistant.lecture_tools import PROJECT_DIR
from lecture_assistant.memory_store import MemoryStore, MemoryStoreError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stats", help="Количество записей памяти")
    commands.add_parser("notes", help="Проверенные цитаты с источником и датами")
    history = commands.add_parser("history", help="История указанной беседы")
    history.add_argument("--session", required=True)
    task = commands.add_parser("task", help="Последний сохранённый снимок задачи")
    task.add_argument("--run-id", required=True)
    forget = commands.add_parser("forget-session", help="Удалить беседу и первично созданные в ней цитаты из SQLite")
    forget.add_argument("--session", required=True)
    commands.add_parser("prune", help="Удалить просроченные записи")
    commands.add_parser("clear-cache", help="Очистить кеш поиска, сохранив историю и цитаты")
    args = parser.parse_args()
    try:
        store = MemoryStore(PROJECT_DIR)
        if args.command == "stats":
            stats = store.stats()
            for key, title in (("tasks", "Сохранённых задач"), ("turns", "Запросов в истории"), ("facts", "Записей с цитатами"), ("cache", "Записей кеша")):
                print(f"{title}: {stats[key]}")
            print("Состояния задач: " + json.dumps(stats["task_statuses"], ensure_ascii=False))
        elif args.command == "notes":
            with store._connect() as db:
                rows = db.execute("SELECT id,session_id,file,document_version,quote,source_json,created,verified,expires,status FROM facts ORDER BY created DESC").fetchall()
            print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
        elif args.command == "history":
            rows = store.history(args.session)
            if not rows:
                print("История этой беседы пуста.")
            for row in rows:
                print(f"\n{row['created_at']} | {row['status']} | {row['file']}")
                print(f"Вопрос: {row['question']}")
                if row.get("answer"):
                    print(f"Сохранённый ответ: {row['answer']}")
                print(f"Версия лекции: {row['document_version']}; запрос: {row['run_id']}")
        elif args.command == "task":
            snapshot = store.task(args.run_id)
            if snapshot is None:
                print("Задача не найдена.")
                return 1
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        elif args.command == "forget-session":
            print("Удалено из SQLite: " + json.dumps(store.forget_session(args.session), ensure_ascii=False))
            print("Файлы журналов runs и результатов results этой командой не удаляются. Общий кеш поиска очищается отдельно командой clear-cache.")
        elif args.command == "prune":
            print("Удалено просроченных записей: " + json.dumps(store.prune(), ensure_ascii=False))
        elif args.command == "clear-cache":
            print("Кеш очищен: " + json.dumps(store.clear_cache(), ensure_ascii=False))
        return 0
    except MemoryStoreError as exc:
        print(f"Ошибка памяти: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
