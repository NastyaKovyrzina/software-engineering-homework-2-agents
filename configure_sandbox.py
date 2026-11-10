"""Подготовка и применение добавлений только в профиль LectureIsolationLab."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from lecture_assistant.sandbox_runtime import BOX_NAME, get_sandbox_info

ROOT = Path(__file__).resolve().parent
INI = Path(r"C:\Windows\Sandboxie.ini")
SBIE_INI = Path(r"C:\Program Files\Sandboxie-Plus\SbieIni.exe")


def section_lines(path=INI):
    raw = path.read_bytes()
    text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    found, lines = False, []
    for line in text.splitlines():
        if line.strip().startswith("["):
            found = line.strip().casefold() == f"[{BOX_NAME}]".casefold()
        if found:
            lines.append(line)
    if not lines:
        raise RuntimeError(f"Профиль {BOX_NAME} отсутствует. Создайте его в Sandboxie Plus.")
    return lines


def values(lines):
    result = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith(("#", ";")):
            key, value = line.split("=", 1)
            result.setdefault(key.strip().casefold(), []).append(value.strip())
    return result


def required_rules(root=ROOT, user=Path.home(), python_home=Path(sys.base_prefix)):
    rules = [
        ("ReadFilePath", f"python.exe,{root}\\*.py"),
        ("ReadFilePath", f"python.exe,{root}\\lecture_assistant\\*.py"),
        ("ReadFilePath", f"python.exe,{root}\\*.md"),
        ("ReadFilePath", f"python.exe,{root}\\models.json"),
        ("ReadFilePath", f"python.exe,{root}\\.gateway.json"),
        ("ReadFilePath", f"python.exe,{root}\\data\\*"),
        ("ReadFilePath", f"python.exe,{root}\\instructions\\*"),
        ("ReadFilePath", f"python.exe,{root}\\.venv\\*"),
        ("ReadFilePath", f"python.exe,{root}\\readonly-probe.txt"),
        ("ReadFilePath", f"python.exe,{python_home}\\*"),
        ("OpenFilePath", f"python.exe,{root}\\results\\*"),
        ("OpenFilePath", f"python.exe,{root}\\runs\\*"),
        ("ClosedFilePath", str(root.parent / "isolation-lab" / "outside") + "\\*"),
        ("ClosedFilePath", f"python.exe,{user}\\Documents\\*"),
        ("ClosedFilePath", f"python.exe,{user}\\Downloads\\*"),
        ("ClosedFilePath", f"python.exe,{user}\\.ssh\\*"),
        ("ClosedFilePath", f"python.exe,{user}\\.codex\\*"),
        ("ClosedFilePath", "python.exe," + os.environ.get("OLLAMA_MODELS", str(user / ".ollama" / "models")) + "\\*"),
        ("ClosedFilePath", f"python.exe,{INI}"),
        ("NetworkAccess", "*,Block"),
        ("NetworkAccess", "python.exe,Allow;Address=127.0.0.1;Port=18734;Protocol=TCP"),
        ("ProcessGroup", "<StartRunAccess>,python.exe"),
        ("ClosedIpcPath", "!<StartRunAccess>,*"),
    ]
    return rules


def plan_changes(lines, rules):
    current = values(lines)
    network = current.get("networkaccess", [])
    allowed_network = {value.casefold() for key, value in rules if key == "NetworkAccess"}
    if any(value.casefold() not in allowed_network for value in network):
        raise RuntimeError("В профиле есть дополнительные сетевые разрешения. Сверьте их с желаемым профилем перед применением.")
    groups = [value for value in current.get("processgroup", []) if value.casefold().startswith("<startrunaccess>,")]
    if any(value.casefold() != "<startrunaccess>,python.exe" for value in groups):
        raise RuntimeError("Группа StartRunAccess разрешает другие программы. Для этого приложения нужен только python.exe.")
    return [{"key": key, "value": value} for key, value in rules if value.casefold() not in {item.casefold() for item in current.get(key.casefold(), [])}]


def retired_rules(lines):
    old = f"python.exe,{ROOT.parent}\\isolation-lab\\agent\\results\\*"
    return [{"key": "OpenFilePath", "value": value} for value in values(lines).get("openfilepath", []) if value.casefold() == old.casefold()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Применить подготовленные добавления через SbieIni.exe")
    args = parser.parse_args()
    if os.name != "nt" or get_sandbox_info()["sandboxed"]:
        raise RuntimeError("Настройку выполняют в обычном терминале Windows, вне Sandboxie.")
    lines = section_lines()
    added = plan_changes(lines, required_rules())
    removed = retired_rules(lines)
    allowed_open = {value.casefold() for key, value in required_rules() if key == "OpenFilePath"}
    retired_open = {item["value"].casefold() for item in removed}
    if any(value.casefold() not in allowed_open | retired_open for value in values(lines).get("openfilepath", [])):
        raise RuntimeError("В профиле есть дополнительные разрешения прямой записи. Сверьте OpenFilePath перед настройкой.")
    directory = ROOT / "sandbox-settings"
    directory.mkdir(exist_ok=True)
    desired = [line for line in lines if not any(line.casefold() == f"{item['key']}={item['value']}".casefold() for item in removed)]
    (directory / "desired-profile.ini").write_text("\n".join(desired + [f"{item['key']}={item['value']}" for item in added]) + "\n", encoding="utf-8")
    print(f"Профиль: {BOX_NAME}. Добавлений: {len(added)}; удаление устаревших разрешений: {len(removed)}.", flush=True)
    for item in removed:
        print(f"Убрать: {item['key']}={item['value']}", flush=True)
    for item in added:
        print(f"{item['key']}={item['value']}", flush=True)
    if not args.apply:
        print("План сохранён в sandbox-settings/desired-profile.ini. Для применения: configure_sandbox.py --apply")
        return 0
    listed = subprocess.run([str(SBIE_INI.parent / "Start.exe"), f"/box:{BOX_NAME}", "/listpids"], capture_output=True, timeout=15)
    counts = listed.stdout.decode("utf-8", errors="replace").strip().splitlines()
    if listed.returncode or not counts or counts[0].strip() != "0":
        raise RuntimeError("Сначала завершите процессы внутри LectureIsolationLab через Sandboxie Plus. Изменения не применены.")
    if added or removed:
        before = directory / "profile-before.ini"
        if not before.exists():
            before.write_text("\n".join(lines) + "\n", encoding="utf-8")
        completed = []
        completed_removals = []
        try:
            for item in removed:
                result = subprocess.run([str(SBIE_INI), "delete", BOX_NAME, item["key"], item["value"]], capture_output=True, timeout=15)
                actual = values(section_lines()).get(item["key"].casefold(), [])
                if result.returncode or item["value"].casefold() in {value.casefold() for value in actual}:
                    raise RuntimeError("Не удалось убрать разрешение записи лабораторного скрипта.")
                completed_removals.append(item)
            for item in added:
                result = subprocess.run([str(SBIE_INI), "append", BOX_NAME, item["key"], item["value"]], capture_output=True, timeout=15)
                actual = values(section_lines()).get(item["key"].casefold(), [])
                if result.returncode or item["value"].casefold() not in {value.casefold() for value in actual}:
                    raise RuntimeError(f"Не удалось сохранить {item['key']}. Проверьте права редактирования Sandboxie; добавление не подтверждено.")
                completed.append(item)
            (directory / "applied-changes.json").write_text(json.dumps({"added": completed, "removed": completed_removals}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            # Откат только добавленных этим запуском строк; другие настройки не затрагиваются.
            for item in reversed(completed):
                subprocess.run([str(SBIE_INI), "delete", BOX_NAME, item["key"], item["value"]], capture_output=True, timeout=15)
            for item in completed_removals:
                subprocess.run([str(SBIE_INI), "append", BOX_NAME, item["key"], item["value"]], capture_output=True, timeout=15)
            raise
    print("Добавления подтверждены в Sandboxie.ini. Эффективные ограничения проверяются run_sandbox.py --check.", flush=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Настройка не завершена: {exc}", flush=True)
        raise SystemExit(1)
