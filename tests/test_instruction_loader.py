import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lecture_assistant.instruction_loader import DEFAULT_DIRECTORY, InstructionError, InstructionSnapshot
from lecture_assistant.lecture_graph import build_graph, initial_state
from lecture_assistant.lecture_tools import PROJECT_DIR, LectureTools
from test_lecture_graph import FakeClient


class InstructionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".instructions-test-", dir=PROJECT_DIR)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / "instructions"
        shutil.copytree(DEFAULT_DIRECTORY, self.directory)

    def tearDown(self):
        if self.root == PROJECT_DIR or not self.root.resolve().is_relative_to(PROJECT_DIR):
            raise RuntimeError("Небезопасный путь очистки")
        self.temp.cleanup()

    def test_disk_change_applies_only_to_new_snapshot_and_changes_hash(self):
        snapshot = InstructionSnapshot(self.directory)
        before = snapshot.bundle("search")
        with (self.directory / "roles/search.md").open("a", encoding="utf-8") as stream:
            stream.write("\nТЕСТОВОЕ ПРАВИЛО\n")
        after = InstructionSnapshot(self.directory).bundle("search")
        self.assertNotIn("ТЕСТОВОЕ ПРАВИЛО", before.text)
        self.assertIn("ТЕСТОВОЕ ПРАВИЛО", after.text)
        self.assertNotEqual(before.metadata["sha256"], after.metadata["sha256"])

    def test_missing_empty_and_invalid_utf8_are_not_silently_ignored(self):
        path = self.directory / "roles/search.md"
        for content in (b"", b"\xff"):
            path.write_bytes(content)
            with self.assertRaises(InstructionError):
                InstructionSnapshot(self.directory)
        path.unlink()
        with self.assertRaises(InstructionError):
            InstructionSnapshot(self.directory)

    def test_manifest_cannot_load_a_file_outside_instruction_directory(self):
        path = self.directory / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["roles"]["search"]["system"] = "../outside.md"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(InstructionError):
            InstructionSnapshot(self.directory)

    def test_graph_passes_files_to_models_and_loads_revision_skill_only_when_needed(self):
        lecture_dir = self.root / "data/lectures"
        lecture_dir.mkdir(parents=True)
        (lecture_dir / "lecture.md").write_text("# Изоляция\nИзоляция ограничивает доступ.\n", encoding="utf-8")
        client = FakeClient()
        client.bad_quotes = 1
        seen = []
        original = client.chat

        def chat(role, schema, system, context, json_schema=None):
            seen.append((role, system))
            return original(role, schema, system, context, json_schema=json_schema)

        client.chat = chat
        state = build_graph(LectureTools("lecture.md", self.root), client, instructions_dir=self.directory).invoke(initial_state("Что такое изоляция?"), config={"recursion_limit": 30})
        self.assertEqual(state["status"], "answered")
        writer_prompts = [text for role, text in seen if role == "simple"]
        self.assertNotIn("skills/revise.md", writer_prompts[0])
        self.assertIn("skills/revise.md", writer_prompts[1])
        self.assertTrue(all("context/memory.md" in text for _, text in seen))
        events = [event for event in state["events"] if event["kind"] == "model"]
        self.assertEqual([event["instructions"]["bundle"] for event in events], ["search", "answer", "revise", "review"])
        self.assertTrue(all(event["instructions"]["files"] for event in events))


if __name__ == "__main__":
    unittest.main()
