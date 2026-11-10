"""Периодическая проверка компонентов без запросов на генерацию текста."""
import argparse
import json
import time
from pathlib import Path

from lecture_assistant.sandbox_runtime import get_sandbox_info
from lecture_assistant.service_health import ServiceHealthMonitor
from lecture_assistant.telemetry_store import TelemetryStore

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if get_sandbox_info()['sandboxed']:
        parser.error('Монитор запускают из обычного терминала Windows')
    settings = json.loads((ROOT / 'observability_settings.json').read_text(encoding='utf-8'))
    monitor = ServiceHealthMonitor(ROOT, settings, TelemetryStore(ROOT, settings=settings))
    try:
        while True:
            snapshot = monitor.check()
            print(json.dumps(snapshot, ensure_ascii=False), flush=True)
            if args.once:
                return 0
            time.sleep(max(5, float(settings.get('health_interval_seconds', 30))))
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
