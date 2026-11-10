import hashlib
import unittest

from lecture_assistant.eval_grading import _fragments, grade_turn


class EvalGradingTests(unittest.TestCase):
    def setUp(self):
        self.raw = "# Lecture\nWater boils at 100 C.\n".encode("utf-8")
        self.text = self.raw.decode()
        self.version = hashlib.sha256(self.raw).hexdigest()
        quote = "Water boils at 100 C."
        start = self.text.index(quote)
        fid = _fragments(self.text, self.version)[0][0]
        src = {"file": "lecture.md", "document_version": self.version, "fragment_id": fid,
               "quote": quote, "char_start": start, "char_end": start + len(quote), "line_start": 2, "line_end": 2}
        self.case = {"id": "water"}
        self.turn = {"question": "At what temperature does water boil?", "expected_status": "answered",
                     "required_fact_groups": [[quote]], "manual_required": False}
        self.record = {"execution": {"sandbox": {"sandboxed": True, "box_name": "LectureIsolationLab"}, "model_transport": "gateway"},
                       "outcome": {"status": "answered", "question": self.turn["question"], "lecture": "lecture.md",
                                   "answer": quote + " [1]", "sources": [src],
                                   "verification": {"exact_quotes": True, "model_review": True}},
                       "state": {"review": {"approved": True, "family": "gemma"}, "writer_family": "qwen",
                                 "result": {"semantic_verification": {"status": "model_approved"}}, "revisions": 0,
                                 "events": [{"kind": "model", "role": "simple", "status": "completed"}]}}

    def grade(self, record=None, case=None, raw=None):
        return grade_turn(case or self.case, self.turn, record or self.record,
                          self.raw if raw is None else raw, "lecture.md", "sandbox")

    def test_valid_exact_citation_passes_without_trusting_model_review(self):
        result = self.grade()
        self.assertTrue(result["auto_passed"])
        self.assertEqual(result["scores"]["citation_grounding"], 1.0)

    def test_irrelevant_quote_and_approval_do_not_prove_required_fact(self):
        record = {**self.record, "outcome": {**self.record["outcome"], "answer": "Other fact [1]",
                  "sources": [{**self.record["outcome"]["sources"][0], "quote": "Water boils at 100 C."}]}}
        record["outcome"]["sources"][0]["quote"] = "# Lecture"
        record["outcome"]["sources"][0].update(char_start=0, char_end=len("# Lecture"), line_start=1, line_end=1)
        result = self.grade(record)
        self.assertFalse(result["auto_passed"])
        self.assertEqual(result["scores"]["required_fact_coverage"], 0.0)
        self.assertEqual(result["scores"]["citation_grounding"], 1.0)

    def test_stale_hash_bad_offsets_and_bad_line_are_rejected(self):
        for change in ({"document_version": "stale"}, {"char_start": 0}, {"line_start": 99}):
            record = {**self.record, "outcome": {**self.record["outcome"], "sources": [
                {**self.record["outcome"]["sources"][0], **change}]}}
            self.assertFalse(self.grade(record)["auto_passed"])

    def test_unsourced_extra_text_fails_even_with_quote(self):
        record = {**self.record, "outcome": {**self.record["outcome"], "answer": "Water boils at 100 C. It is always safe. [1]"}}
        result = self.grade(record)
        self.assertTrue(result["scores"]["unsupported_answer_text"])
        self.assertFalse(result["auto_passed"])

    def test_empty_refusal_or_error_cannot_be_answered(self):
        record = {**self.record, "outcome": {"status": "insufficient_information", "answer": None, "sources": []}}
        result = self.grade(record)
        self.assertFalse(result["auto_passed"])
        self.assertFalse(result["scores"]["status_match"])

    def test_expected_technical_error_passes_and_metrics_stay_unknown(self):
        case = {"id": "file-missing"}
        turn = {"question": self.turn["question"], "expected_status": "technical_error", "expected_error": "FILE_NOT_FOUND", "required_fact_groups": []}
        record = {"execution": {"sandbox": {"sandboxed": True, "box_name": "LectureIsolationLab"}, "model_transport": "gateway"},
                  "outcome": {"status": "technical_error", "question": self.turn["question"], "lecture": "lecture.md", "answer": None, "sources": [], "error": {"code": "FILE_NOT_FOUND"}},
                  "state": {"events": [], "revisions": 0}}
        result = grade_turn(case, turn, record, self.raw, "lecture.md", "sandbox")
        self.assertTrue(result["auto_passed"])
        self.assertIsNone(result["metrics"]["peak_rss_mib"])
        self.assertIsNone(result["metrics"]["prompt_tokens"])

    def test_missing_document_snapshot_fails_closed(self):
        result = grade_turn(self.case, self.turn, self.record, None, "lecture.md", "sandbox")
        self.assertFalse(result["auto_passed"])
        self.assertTrue(any("snapshot unavailable" in issue for issue in result["issues"]))

    def test_ground_truthless_answer_is_manual_not_auto_pass(self):
        case = {"id": "manual"}
        turn = {**self.turn, "required_fact_groups": [], "manual_required": True}
        result = grade_turn(case, turn, self.record, self.raw, "lecture.md", "sandbox")
        self.assertTrue(result["manual_required"])
        self.assertEqual(result["manual_status"], "pending")
        self.assertFalse(result["auto_passed"])

    def test_more_than_30_non_node_events_are_not_a_graph_step_violation(self):
        record = {**self.record, "state": {**self.record["state"], "events":
                  self.record["state"]["events"] + [{"kind": "tool", "tool": "read_lecture", "ok": True} for _ in range(31)]}}
        result = self.grade(record)
        self.assertTrue(result["auto_passed"])

    def test_node_visits_counted_by_name(self):
        node_events = [{"kind": "node", "node": "answer"} for _ in range(4)]
        record = {**self.record, "state": {**self.record["state"], "events": node_events}}
        result = self.grade(record)
        self.assertEqual(result["metrics"]["max_node_visits"], 4)
        self.assertFalse(result["auto_passed"])

    def test_same_family_reviewer_and_reference_zero_are_rejected(self):
        record = {**self.record, "outcome": {**self.record["outcome"], "answer": "Water boils at 100 C. [0]"},
                  "state": {**self.record["state"], "review": {"approved": True, "family": "qwen2.5"}}}
        result = self.grade(record)
        self.assertFalse(result["auto_passed"])
        self.assertTrue(any("families" in issue for issue in result["issues"]))

    def test_malformed_sources_and_records_fail_closed(self):
        record = {**self.record, "outcome": {**self.record["outcome"], "sources": "not-a-list"}}
        result = self.grade(record)
        self.assertFalse(result["auto_passed"])
        self.assertTrue(any("sources must be a list" in issue for issue in result["issues"]))
        result = grade_turn(self.case, self.turn, {"state": []}, self.raw, "lecture.md", "sandbox")
        self.assertFalse(result["auto_passed"])


if __name__ == "__main__":
    unittest.main()
