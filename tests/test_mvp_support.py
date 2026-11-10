"""Regression tests for the compact Evals/check helpers; no models or network."""
import csv
import tempfile
import unittest
from pathlib import Path

import eval_system as evaluation
from regression_checks import snapshot


def row(case_id="c01", passed=True, executed=True):
    return {"case_id": case_id, "turn": 1,
            "grading": {"auto_passed": passed, "executed": executed}}


def report(rows):
    value = {"schema": "mvp1", "mode": "sandbox", "dataset_sha256": "same",
        "grader_sha256": "same", "selected_cases": ["c01", "c02"],
        "case_turns": {"c01": 1, "c02": 1}, "planned_turns": 2, "turns": rows}
    value["summary"] = evaluation.summarize(value)
    return value


class CompactEvaluationTests(unittest.TestCase):
    def test_unexecuted_cases_stay_in_denominator(self):
        summary = report([row()])["summary"]
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(summary["turns"], 2)
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(summary["passed_cases"], 1)

    def test_multiturn_case_requires_all_steps(self):
        value = report([row(), {**row(), "turn": 2, "grading": {"auto_passed": False, "executed": True}}])
        value.update(selected_cases=["c01"], case_turns={"c01": 2})
        self.assertEqual(evaluation.summarize(value)["passed_cases"], 0)

    def test_compare_detects_regression(self):
        old = report([row(), row("c02")])
        new = report([row(passed=False), row("c02")])
        result = evaluation.compare(old, new)
        self.assertEqual(result["regressions"], [["c01", 1]])
        self.assertEqual(result["success_rate_after"], 0.5)

    def test_compare_rejects_incomplete_or_different_grader(self):
        old = report([row(), row("c02")])
        with self.assertRaises(ValueError):
            evaluation.compare(old, report([row()]))
        new = report([row(), row("c02")])
        new["grader_sha256"] = "changed"
        with self.assertRaises(ValueError):
            evaluation.compare(old, new)

    def test_manual_csv_accumulates_turns_and_preserves_annotations(self):
        def annotated(case):
            value = row(case)
            value.update(run_id="a"*32, question="Вопрос", answer="Ответ", expected_status="answered")
            value["grading"].update(manual_required=True, issues=[], scores={"required_fact_coverage": 1, "citation_grounding": 1},
                metrics={"duration_seconds": 1, "peak_rss_mib": 2, "model_calls": 1, "tool_selection_errors": 0, "revisions": 0})
            return value
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            value = report([annotated("c01")])
            evaluation.write_report(directory, value)
            manual = directory / "manual.csv"
            with manual.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream, delimiter=";"))
            rows[0].update({"Правильность 0-2": "2", "Полнота 0-2": "2", "По источнику да/нет": "да",
                            "Инструкции да/нет": "да", "Правки": "нет"})
            with manual.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            value["turns"].append(annotated("c02"))
            evaluation.write_report(directory, value)
            with manual.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream, delimiter=";"))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["Правильность 0-2"], "2")
            self.assertEqual(value["manual"]["reviewed"], 1)
            self.assertEqual(value["manual"]["pending"], 1)

    def test_watch_tracks_source_but_ignores_run_results(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "app.py").write_text("before", encoding="utf-8")
            (root / "runs").mkdir()
            (root / "runs" / "record.json").write_text("data")
            before = snapshot(root)
            (root / "runs" / "record.json").write_text("changed")
            self.assertEqual(before, snapshot(root))
            (root / "app.py").write_text("after")
            self.assertNotEqual(before, snapshot(root))


if __name__ == "__main__":
    unittest.main()
