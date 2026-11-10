import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_sandbox as launcher


class SandboxLauncherTests(unittest.TestCase):
    def setUp(self):
        self.parent = launcher.PROJECT.resolve()
        self.temporary = tempfile.TemporaryDirectory(prefix=".launcher-test-", dir=self.parent)
        self.root = Path(self.temporary.name).resolve()
        self.exe = self.root / "fixture.exe"
        self.exe.write_bytes(b"fixture; never executed")

    def tearDown(self):
        if self.root == self.parent or not self.root.resolve().is_relative_to(self.parent):
            raise RuntimeError("Небезопасный путь очистки")
        self.temporary.cleanup()

    def invoke_check(self, *, create_report=True, visible_results=True, visible_sqlite=True):
        args = SimpleNamespace(check=True, question=None, lecture="lecture_demo.md", route="auto", timeout=300, verbose=False)
        observed = {"controls": 0}

        def host_controls():
            observed["controls"] += 1
            if observed["controls"] == 2:
                self.assertTrue(observed["report"].is_file(), "Повторная внешняя проверка должна идти после чтения отчёта")
            return "same-hash"

        def run_child(command, timeout):
            self.assertEqual(observed["controls"], 1)
            self.assertIn(str(self.root / "sandbox_probe.py"), command)
            run_id = command[command.index("--run-id") + 1]
            report_path = self.root / "runs" / f"sandbox-check-{run_id}.json"
            observed["report"] = report_path
            if create_report:
                report = {"run_id": run_id, "execution": {"sandbox": {"sandboxed": True, "box_name": launcher.BOX_NAME}, "model_transport": "gateway"}, "passed": True, "checks": []}
                report_path.write_text(json.dumps(report), encoding="utf-8")
                for directory in ("runs", "results") if visible_results else ("runs",):
                    (self.root / directory / f"probe-{run_id}.txt").write_text("sandbox write test", encoding="utf-8")
                if visible_sqlite:
                    connection = sqlite3.connect(self.root / "runs" / f"probe-{run_id}.sqlite3")
                    try:
                        with connection:
                            connection.execute("CREATE TABLE memory_probe(value TEXT NOT NULL)")
                            connection.execute("INSERT INTO memory_probe VALUES('sandbox sqlite test')")
                    finally:
                        connection.close()
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(launcher, "PROJECT", self.root), patch.object(launcher, "START_EXE", self.exe), patch.object(launcher, "PYTHON_EXE", self.exe), patch.object(launcher, "_arguments", return_value=args), patch.object(launcher, "get_sandbox_info", return_value={"sandboxed": False}), patch.object(launcher, "_load_gateway"), patch.object(launcher, "_host_controls", side_effect=host_controls), patch.object(launcher, "_run_hidden", side_effect=run_child), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = launcher.main()
        return code, observed

    def test_check_reads_report_before_post_checks_and_saves_verification(self):
        code, observed = self.invoke_check()
        self.assertEqual(code, 0)
        self.assertEqual(observed["controls"], 2)
        report = json.loads(observed["report"].read_text(encoding="utf-8"))
        self.assertTrue(report["passed"])
        self.assertTrue(report["host_verification"]["controls_passed"])
        self.assertFalse(list(self.root.glob("*/probe-*.txt")))

    def test_absent_report_is_failure_even_when_start_exits_zero(self):
        code, observed = self.invoke_check(create_report=False)
        self.assertEqual(code, 1)
        self.assertEqual(observed["controls"], 1)

    def test_missing_sqlite_host_output_invalidates_successful_child_report(self):
        code, observed = self.invoke_check(visible_sqlite=False)
        self.assertEqual(code, 1)
        report = json.loads(observed["report"].read_text(encoding="utf-8"))
        self.assertFalse(report["passed"])

    def test_missing_host_output_invalidates_successful_child_report(self):
        code, observed = self.invoke_check(visible_results=False)
        self.assertEqual(code, 1)
        report = json.loads(observed["report"].read_text(encoding="utf-8"))
        self.assertFalse(report["passed"])
        self.assertFalse(report["host_verification"]["controls_passed"])

    def test_verbose_details_are_rendered_when_sandbox_has_no_stdout(self):
        from lecture_assistant.result_format import make_outcome
        args = SimpleNamespace(check=False, question="Вопрос", lecture="lecture_demo.md", route="complex", timeout=300, verbose=True, session=None, no_memory=False, no_observability=False)
        outcome = make_outcome({"status":"insufficient_information", "reason":"verification_failed_after_revision", "result":{"status":"insufficient_information", "reason":"verification_failed_after_revision"}}, "lecture_demo.md")
        def run_child(command, timeout):
            run_id = command[command.index("--run-id") + 1]
            report = {"run_id":run_id, "execution":{"sandbox":{"sandboxed":True,"box_name":launcher.BOX_NAME},"model_transport":"gateway"}, "outcome":outcome}
            (self.root / "runs" / f"run-{run_id}.json").write_text(json.dumps(report), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        output = io.StringIO()
        with patch.object(launcher,"PROJECT",self.root), patch.object(launcher,"START_EXE",self.exe), patch.object(launcher,"PYTHON_EXE",self.exe), patch.object(launcher,"_arguments",return_value=args), patch.object(launcher,"get_sandbox_info",return_value={"sandboxed":False}), patch.object(launcher,"_load_gateway"), patch.object(launcher,"_run_hidden",side_effect=run_child), redirect_stdout(output):
            code = launcher.main()
        self.assertEqual(code, 0)
        self.assertIn("Статус: insufficient_information", output.getvalue())
        self.assertIn("verification_failed_after_revision", output.getvalue())


if __name__ == "__main__":
    unittest.main()
