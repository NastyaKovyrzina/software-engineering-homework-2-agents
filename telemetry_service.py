"""Сборщик вне Sandboxie: файлы телеметрии, метрики, алерты и экспорт в Phoenix."""
import argparse
import json
import threading
import time
from pathlib import Path

import psutil

from lecture_assistant.telemetry_store import ObservabilityServer, TelemetryStore
from lecture_assistant.service_health import ServiceHealthMonitor

ROOT = Path(__file__).resolve().parent


def sample_host():
    memory = psutil.virtual_memory()
    ollama_rss = 0
    found = 0
    for process in psutil.process_iter(["name", "memory_info"]):
        try:
            if (process.info["name"] or "").casefold() in {"ollama.exe", "ollama app.exe", "ollama"}:
                ollama_rss += process.info["memory_info"].rss
                found += 1
        except (psutil.Error, TypeError, AttributeError):
            continue
    return {"ts_ns": time.time_ns(), "system_memory_percent": memory.percent,
            "system_used_mib": memory.used / 2**20, "system_total_mib": memory.total / 2**20,
            "ollama_rss_mib": ollama_rss / 2**20 if found else None,
            "ollama_processes": found, "monitor_rss_mib": psutil.Process().memory_info().rss / 2**20}


def main():
    from lecture_assistant.sandbox_runtime import get_sandbox_info
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Один проход сборщика без веб-сервера")
    parser.add_argument("--no-export", action="store_true", help="Собирать локально без экспорта в Phoenix")
    args = parser.parse_args()
    if get_sandbox_info()["sandboxed"]:
        raise RuntimeError("Сборщик запускается вне Sandboxie")
    settings = json.loads((ROOT / "observability_settings.json").read_text(encoding="utf-8"))
    store = TelemetryStore(ROOT, settings=settings)
    health = ServiceHealthMonitor(ROOT, settings, store)
    exporter = None
    if not args.no_export:
        from lecture_assistant.phoenix_exporter import PhoenixExporter
        exporter = PhoenixExporter()
    stop = threading.Event()

    def collect():
        last_sample, last_prune, next_export, export_failed = 0, 0, 0, False
        next_health = 0
        while not stop.is_set():
            try:
                store.scan_spool()
                now = time.monotonic()
                if now - last_sample >= 5:
                    store.sample_resources(sample_host())
                    last_sample = now
                store.check_alerts()
                if now >= next_health:
                    try:
                        health.check()
                    except Exception as exc:
                        store.alert('health_monitor_error', 'Ошибка проверки доступности: ' + type(exc).__name__)
                    next_health = time.monotonic() + max(5, float(settings.get('health_interval_seconds', 30)))
                if exporter is not None and now >= next_export:
                    rows = store.pending_spans(100)
                    if rows:
                        try:
                            success = exporter.export(rows)
                        except Exception:
                            success = False
                        if success:
                            store.mark_exported([row["span_id"] for row in rows])
                            if export_failed:
                                store.alert("phoenix_export_restored", "Экспорт в Phoenix восстановлен; очередь отправляется.", severity="INFO")
                            export_failed = False
                            next_export = now + 1
                        else:
                            store.alert("phoenix_export_failed", "Phoenix не принял спаны. Они остаются в локальной очереди; повтор через 10 секунд.")
                            export_failed = True
                            next_export = now + 10
                if now - last_prune >= 3600:
                    store.prune()
                    last_prune = now
            except Exception as exc:
                print(f"Ошибка сборщика: {type(exc).__name__}: {exc}", flush=True)
            if args.once:
                return
            stop.wait(1)

    if args.once:
        collect()
        if exporter:
            exporter.close()
        return
    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    with ObservabilityServer(("127.0.0.1", int(settings["dashboard_port"])), store) as server:
        print("Панель наблюдения: http://127.0.0.1:18736; Phoenix: http://127.0.0.1:6006", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            thread.join(timeout=15)
            if exporter:
                exporter.close()


if __name__ == "__main__":
    main()
