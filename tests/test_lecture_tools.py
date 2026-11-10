import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lecture_assistant.lecture_tools import CHUNK_SIZE, MAX_FILE_BYTES, PROJECT_DIR, LectureTools


class LectureToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".tools-test-", dir=PROJECT_DIR)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / "data" / "lectures"
        self.directory.mkdir(parents=True)
        self.path = self.directory / "lecture.md"
        self.text = "# Изоляция\r\n\r\nИзоляция ограничивает доступ.\r\n\r\n## Память\r\n\r\nПамять хранит данные.\r\n"
        self.path.write_bytes(self.text.encode("utf-8"))
        self.tools = LectureTools("lecture.md", self.root)

    def tearDown(self):
        # Проверяем границу перед рекурсивной очисткой только своего тестового каталога.
        if self.root == PROJECT_DIR or not self.root.resolve().is_relative_to(PROJECT_DIR):
            raise RuntimeError("Небезопасный путь очистки теста")
        self.temporary.cleanup()

    def load(self):
        self.assertTrue(self.tools.read_lecture()["ok"])

    def citation(self):
        self.load()
        found = self.tools.search_fragments("изоляция")["data"]["fragments"][0]
        return {"fragment_id": found["source"]["fragment_id"], "quote": "Изоляция ограничивает доступ."}

    def test_exact_quote_keeps_crlf_offsets_and_source_line(self):
        citation = self.citation()
        data = self.tools.get_quote(**citation)["data"]
        source = data["source"]
        self.assertEqual(source["line_start"], 3)
        self.assertEqual(source["line_end"], 3)
        self.assertEqual(source["section"], "Изоляция")
        self.assertEqual(self.text[source["char_start"]:source["char_end"]], data["quote"])

    def test_multiline_quote_line_end(self):
        citation = self.citation()
        citation["quote"] = "# Изоляция\r\n\r\nИзоляция ограничивает доступ.\r\n"
        result = self.tools.get_quote(**citation)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["source"]["line_end"], 3)

    def test_search_without_matches_is_not_an_error(self):
        self.load()
        result = self.tools.search_fragments("фотосинтез")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["fragments"], [])
        self.assertFalse(result["data"]["found"])

    def test_unloaded_and_invalid_limit(self):
        self.assertEqual(self.tools.search_fragments("память")["error"]["code"], "DOCUMENT_NOT_LOADED")
        for limit in (True, "3", 0, 6):
            with self.subTest(limit=limit):
                self.assertEqual(self.tools.search_fragments("память", limit)["error"]["code"], "INVALID_ARGUMENT")

    def test_case_and_yo_normalization(self):
        self.path.write_text("Учёт данных. Память и данные.", encoding="utf-8")
        self.load()
        self.assertTrue(self.tools.search_fragments("УЧЕТ")["data"]["found"])

    def test_changed_source_requires_reload_and_old_ids_do_not_work(self):
        citation = self.citation()
        self.path.write_text("Изоляция ограничивает доступ. Новая версия.", encoding="utf-8")
        for action in (
            lambda: self.tools.search_fragments("изоляция"),
            lambda: self.tools.get_quote(**citation),
            lambda: self.tools.save_result("Вопрос", "Ответ", [citation]),
        ):
            self.assertEqual(action()["error"]["code"], "STALE_DOCUMENT")
        self.load()
        self.assertEqual(self.tools.get_quote(**citation)["error"]["code"], "UNKNOWN_FRAGMENT")

    def test_invented_quote_and_wrong_fragment_rejected(self):
        citation = self.citation()
        self.assertEqual(self.tools.get_quote(citation["fragment_id"], "Изоляция разрешает всё.")["error"]["code"], "QUOTE_NOT_FOUND")
        self.assertEqual(self.tools.get_quote("other:0001", citation["quote"])["error"]["code"], "UNKNOWN_FRAGMENT")

    def test_path_traversal_and_absolute_paths_rejected(self):
        for name in ("../outside.txt", "..\\outside.txt", str(self.path), "C:outside.txt", "lecture\x00.md", "lecture\n.md"):
            with self.subTest(name=name):
                self.assertEqual(LectureTools(name, self.root).read_lecture()["error"]["code"], "PATH_NOT_ALLOWED")

    def test_symlink_outside_lecture_directory_rejected(self):
        target = self.root / "outside.md"
        target.write_text("Другой файл", encoding="utf-8")
        link = self.directory / "linked.md"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("ОС не разрешила создание символической ссылки")
        self.assertEqual(LectureTools("linked.md", self.root).read_lecture()["error"]["code"], "PATH_NOT_ALLOWED")

    def test_missing_wrong_format_invalid_encoding_empty_and_oversize(self):
        self.assertEqual(LectureTools("missing.txt", self.root).read_lecture()["error"]["code"], "FILE_NOT_FOUND")
        self.assertEqual(LectureTools("lecture.pdf", self.root).read_lecture()["error"]["code"], "UNSUPPORTED_FORMAT")
        for raw, code in ((b"\xff", "INVALID_ENCODING"), (b" \r\n", "EMPTY_DOCUMENT"), (b"a\x00b", "INVALID_TEXT"), (b"a" * (MAX_FILE_BYTES + 1), "FILE_TOO_LARGE")):
            with self.subTest(code=code):
                self.path.write_bytes(raw)
                self.assertEqual(self.tools.read_lecture()["error"]["code"], code)

    def test_utf8_bom_is_supported(self):
        self.path.write_bytes(b"\xef\xbb\xbf" + self.text.encode("utf-8"))
        self.assertTrue(self.tools.read_lecture()["ok"])

    def test_long_sections_are_bounded_and_preserve_exact_text(self):
        self.text = "# Длинный раздел\n" + ("Память хранит данные.\n" * 250)
        self.path.write_bytes(self.text.encode("utf-8"))
        self.load()
        matches = self.tools.search_fragments("память", limit=5)["data"]["fragments"]
        self.assertEqual(len(matches), 5)
        for match in matches:
            self.assertLessEqual(len(match["text"]), CHUNK_SIZE)
            source = match["source"]
            self.assertEqual(self.text[source["char_start"]:source["char_end"]], match["text"])
            self.assertTrue(self.tools.get_quote(source["fragment_id"], match["text"])["ok"])

    def test_dispatch_rejects_unknown_missing_extra_and_wrong_type(self):
        for name, arguments, code in (
            ("__class__", {}, "UNKNOWN_TOOL"),
            ("search_fragments", {}, "INVALID_ARGUMENT"),
            ("read_lecture", {"path": "outside.txt"}, "INVALID_ARGUMENT"),
            ("read_lecture", [], "INVALID_ARGUMENT"),
            ("search_fragments", {"query": 123}, "INVALID_ARGUMENT"),
            ("search_fragments", {"query": "\ud800"}, "INVALID_ARGUMENT"),
        ):
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(self.tools.call_tool(name, arguments)["error"]["code"], code)

    def test_search_prefers_more_distinct_query_words(self):
        self.path.write_text("# Первый\nПамять.\n# Второй\nПамять хранит данные.\n", encoding="utf-8")
        self.load()
        matches = self.tools.search_fragments("память данные")["data"]["fragments"]
        self.assertEqual(matches[0]["source"]["section"], "Второй")
        self.assertEqual(matches[0]["matched_terms_count"], 2)

    def test_save_validates_sources_and_does_not_claim_semantic_verification(self):
        citation = self.citation()
        result = self.tools.save_result("Что такое изоляция?", "Ответ", [citation])
        self.assertTrue(result["ok"])
        path = self.root / result["data"]["file"]
        self.assertEqual(path.parent, self.root / "results")
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["semantic_verification"], "not_performed")
        self.assertEqual(record["citations"][0]["source"]["line_start"], 3)
        for citations in ([], [{"fragment_id": citation["fragment_id"], "quote": "Выдумка"}], [{**citation, "path": "outside.json"}]):
            self.assertFalse(self.tools.save_result("Вопрос", "Ответ", citations)["ok"])
        self.assertEqual(len(list(path.parent.glob("*.json"))), 1)

    def test_write_failure_leaves_no_partial_result(self):
        citation = self.citation()
        with patch("lecture_assistant.lecture_tools.json.dump", side_effect=OSError("Ошибка записи")):
            result = self.tools.save_result("Вопрос", "Ответ", [citation])
        self.assertEqual(result["error"]["code"], "IO_ERROR")
        self.assertEqual(list((self.root / "results").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
