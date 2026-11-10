"""Fast unit checks and optional file watching. Never calls real LLMs."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parent


def snapshot(root=ROOT):
    files = [*root.glob("*.py"), *(root / "lecture_assistant").rglob("*.py"),
             *(root / "scripts").glob("*.py"), *(root / "tests").glob("*.py"),
             *(root / "instructions").rglob("*.md"), *(root / "evals").glob("*.json")]
    files += [root / name for name in ("models.json", "router_settings.json",
              "observability_settings.json", "instructions/manifest.json", "requirements.txt")]
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files if p.is_file()}


def check():
    before = snapshot()
    folder = ROOT / "runs" / "checks" / uuid4().hex
    folder.mkdir(parents=True)
    command = [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "tests"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=300)
    (folder / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (folder / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    stable = before == snapshot()
    (folder / "check.json").write_text(json.dumps({"source_sha256": before,
        "returncode": result.returncode, "sources_unchanged_during_check": stable}, indent=2), encoding="utf-8")
    print(result.stderr, flush=True)
    print(folder, flush=True)
    return result.returncode if stable else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "watch"], nargs="?", default="check")
    args = parser.parse_args()
    if args.command == "check":
        return check()
    previous = None
    while True:
        current = snapshot()
        if current != previous:
            code = check()
            print("Passed" if code == 0 else "Failed or source changed; see saved check.", flush=True)
            previous = current
        time.sleep(3)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
