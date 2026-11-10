import tempfile
import unittest
from pathlib import Path

from lecture_assistant.memory_store import MemoryStore, MemoryStoreError


class Clock:
    def __init__(self): self.now = 1_800_000_000.0
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


def doc(version="v1"):
    return {"file": "lecture.md", "document_version": version}


def source(version="v1"):
    return {"file":"lecture.md", "document_version":version, "fragment_id":"frag-1",
            "line_start":2, "line_end":2, "char_start":20, "char_end":20 + len("Память хранит факты.")}


def citation(version="v1"):
    return {"quote":"Память хранит факты.", "source":source(version)}


def state(status="answered", approved=True, semantic="model_approved"):
    return {"status":status, "document":doc(), "result":{
        "answer":"Память сохраняет факты.", "semantic_verification":{"status":semantic,"model":"reviewer-x"}},
        "review":{"approved":approved,"model":"reviewer-x"}}


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.db = MemoryStore(Path(self.tmp.name), clock=self.clock)

    def finish(self, run="run1", session="s1", question="Что такое память?"):
        self.db.begin(run, session, question, doc())
        return self.db.complete(run, state(), validated_citations=[citation()])

    def test_recall_and_same_file_version_invalidation(self):
        self.finish()
        self.assertEqual(len(self.db.recall("Что такое память?", doc())), 1)
        result = self.db.begin("run2", "s1", "другая тема", doc("v2"))
        self.assertEqual(result["stale_facts"], 1)
        self.assertEqual(self.db.recall("память", doc()), [])
        self.assertEqual(self.db.recall("память", doc("v2")), [])

    def test_approved_repeat_deduplicates_and_expiry(self):
        self.finish()
        self.clock.advance(1)
        self.db.begin("run2", "s1", "Что такое память?", doc())
        self.assertEqual(self.db.complete("run2", state(), validated_citations=[citation()])["facts_written"], 1)
        self.assertEqual(self.db.stats()["facts"], 1)
        fact = self.db.recall("память", doc())[0]
        self.assertNotEqual(fact["verified_at"], fact["created_at"])
        self.clock.advance(30 * 86400 + 1)
        self.assertEqual(self.db.recall("память", doc()), [])
        self.assertEqual(self.db.prune()["facts"], 1)

    def test_unapproved_answer_is_rejected_without_saving(self):
        self.db.begin("run1", "s1", "Q", doc())
        with self.assertRaises(MemoryStoreError):
            self.db.complete("run1", state(approved=False), validated_citations=[citation()])
        self.assertEqual(self.db.stats()["turns"], 0)

    def test_nonanswer_cannot_promote_citations(self):
        self.db.begin("run1", "s1", "Q", doc())
        with self.assertRaises(MemoryStoreError):
            self.db.complete("run1", state(status="refused"), validated_citations=[citation()])
        self.assertEqual(self.db.stats()["facts"], 0)

    def test_history_is_session_isolated(self):
        self.finish("r1", "alpha", "first")
        self.db.begin("r2", "beta", "second", doc())
        self.assertEqual(self.db.history("alpha")[0]["question"], "first")
        self.assertEqual(self.db.history("beta"), [])
        self.assertEqual(self.db.begin("r3", "alpha", "third", doc())["recent_questions"], ["first"])

    def test_conflicting_citation_rolls_back_atomically(self):
        self.db.begin("run1", "s1", "Q", doc())
        bad = citation(); bad["source"]["file"] = "other.md"
        with self.assertRaises(MemoryStoreError):
            self.db.complete("run1", state(), validated_citations=[bad])
        self.assertEqual(self.db.stats()["turns"], 0)
        self.assertEqual(self.db.stats()["facts"], 0)

    def test_cache_dict_fragments_bounds_ttl_and_document_identity(self):
        fragments = [{"text":"excerpt", "source":source(), "matched_terms":["memory"], "matched_terms_count":1}]
        self.db.put_cache("key", doc(), fragments)
        self.assertEqual(self.db.get_cache("key", doc()), fragments)
        self.assertIsNone(self.db.get_cache("key", doc("v2")))
        self.clock.advance(86401)
        self.assertIsNone(self.db.get_cache("key", doc()))
        with self.assertRaises(MemoryStoreError):
            self.db.put_cache("k", doc(), [{**fragments[0], "text":"x" * 1201}])

    def test_corrupt_sqlite_raises_store_error(self):
        self.db.path.write_bytes(b"not a sqlite database")
        with self.assertRaises(MemoryStoreError) as caught:
            self.db.stats()
        self.assertEqual(caught.exception.code, "MEMORY_STORAGE_ERROR")

    def test_duplicate_citations_count_once_and_unrelated_recall_is_empty(self):
        self.db.begin("r1", "alpha", "Что такое память?", doc())
        result = self.db.complete("r1", state(), validated_citations=[citation(), citation()])
        self.assertEqual(result["facts_written"], 1)
        self.assertEqual(self.db.stats()["facts"], 1)
        self.assertEqual(self.db.recall("Как устроен фотосинтез?", doc()), [])

    def test_cache_rejects_foreign_source_and_corrupt_payload(self):
        import sqlite3
        fragments = [{"text":"excerpt", "source":source(), "matched_terms":["memory"], "matched_terms_count":1}]
        bad = [{**fragments[0], "source":{**source(), "file":"other.md"}}]
        with self.assertRaises(MemoryStoreError):
            self.db.put_cache("key", doc(), bad)
        self.db.put_cache("key", doc(), fragments)
        connection = sqlite3.connect(self.db.path)
        try:
            with connection:
                connection.execute("UPDATE cache SET fragments_json='{}'")
        finally:
            connection.close()
        with self.assertRaises(MemoryStoreError):
            self.db.get_cache("key", doc())

    def test_forget_deletes_only_originating_session(self):
        self.finish("r1", "alpha")
        self.finish("r2", "beta")
        self.assertEqual(self.db.forget_session("alpha")["facts"], 1)
        self.assertEqual(self.db.stats("alpha")["turns"], 0)
        self.assertEqual(self.db.stats("beta")["turns"], 1)


if __name__ == "__main__":
    unittest.main()
