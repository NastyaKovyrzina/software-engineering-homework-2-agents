"""Small, bounded SQLite memory for lecture assistant runs."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class MemoryStoreError(Exception):
    code = "MEMORY_STORAGE_ERROR"

    def __init__(self, message: str):
        super().__init__(message)


try:
    from lecture_assistant.lecture_tools import STOP_WORDS
except Exception:  # standalone import remains possible
    STOP_WORDS = frozenset("а без бы был была были было в во все где да для до же за и из или их к как кто ли на над не но о об от по под при про с со так то у уже что это этот эта эти я".split())

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DAY = 86400


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _tokens(value: str) -> set[str]:
    return {x for x in _TOKEN.findall(value.casefold().replace("ё", "е")) if x not in STOP_WORDS}


def _norm(value: str) -> str:
    return " ".join(_TOKEN.findall(value.casefold().replace("ё", "е")))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class MemoryStore:
    def __init__(self, project_dir: Path, *, clock: Callable[[], float] = time.time):
        self.clock = clock
        try:
            project = Path(project_dir).resolve(strict=True)
            if not project.is_dir():
                raise ValueError("project_dir must be a directory")
            runs = project / "runs"
            self._safe_inside(project, runs)
            if runs.exists():
                self._safe_inside(project, runs.resolve(strict=True))
            mem = runs / "memory"
            if mem.exists():
                self._safe_inside(project, mem.resolve(strict=True))
            runs.mkdir(exist_ok=True)
            mem.mkdir(exist_ok=True)
            resolved_runs = runs.resolve(strict=True)
            self._safe_inside(project, resolved_runs)
            self._safe_inside(resolved_runs, mem.resolve(strict=True))
            db = mem / "assistant.sqlite3"
            if db.exists():
                self._safe_inside(resolved_runs, db.resolve(strict=True))
            self.path = db
            self._project = project
            self._runs = resolved_runs
            self._initialize()
        except MemoryStoreError:
            raise
        except (OSError, ValueError, sqlite3.Error, TypeError) as exc:
            raise MemoryStoreError(f"memory store initialization failed: {exc}") from exc

    @staticmethod
    def _safe_inside(root: Path, path: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise MemoryStoreError("memory path escapes project directory") from exc

    @contextmanager
    def _connect(self):
        try:
            # Check again at use time in case a directory was replaced with a symlink.
            self._safe_inside(self._project, self._runs.resolve(strict=True))
            self._safe_inside(self._runs, self.path.parent.resolve(strict=True))
            if self.path.exists():
                self._safe_inside(self._runs, self.path.resolve(strict=True))
            con = sqlite3.connect(self.path, timeout=5)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA busy_timeout=5000")
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise
            finally:
                con.close()
        except MemoryStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise MemoryStoreError(f"memory database unavailable: {exc}") from exc

    def _initialize(self):
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks(
              run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, file TEXT NOT NULL,
              document_version TEXT NOT NULL, question TEXT NOT NULL, status TEXT NOT NULL,
              state_json TEXT, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS turns(
              run_id TEXT PRIMARY KEY REFERENCES tasks(run_id) ON DELETE CASCADE,
              session_id TEXT NOT NULL, file TEXT NOT NULL, document_version TEXT NOT NULL,
              question TEXT NOT NULL, answer TEXT, status TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS turns_recent ON turns(session_id,file,document_version,created);
            CREATE TABLE IF NOT EXISTS facts(
              id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
              file TEXT NOT NULL, document_version TEXT NOT NULL, fragment_id TEXT NOT NULL,
              question TEXT NOT NULL, quote TEXT NOT NULL, quote_hash TEXT NOT NULL,
              source_json TEXT NOT NULL, reviewer TEXT, created REAL NOT NULL,
              verified REAL NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL,
              UNIQUE(file,document_version,fragment_id,quote_hash));
            CREATE INDEX IF NOT EXISTS facts_current ON facts(file,document_version,status,expires);
            CREATE TABLE IF NOT EXISTS cache(
              cache_key TEXT NOT NULL, file TEXT NOT NULL, document_version TEXT NOT NULL,
              fragments_json TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
              PRIMARY KEY(cache_key,file,document_version));
            """)

    @staticmethod
    def _document(document):
        if not isinstance(document, dict) or not isinstance(document.get("file"), str) or not document["file"] or not isinstance(document.get("document_version"), str) or not document["document_version"]:
            raise MemoryStoreError("document requires nonempty file and document_version")
        return document["file"], document["document_version"]

    @staticmethod
    def _id(value, label):
        if not isinstance(value, str) or not _SESSION.fullmatch(value):
            raise MemoryStoreError(f"invalid {label}")

    def begin(self, run_id: str, session_id: str, question: str, document: dict) -> dict:
        self._id(run_id, "run_id"); self._id(session_id, "session_id")
        if not isinstance(question, str) or not question.strip() or len(question) > 500: raise MemoryStoreError("question must contain 1 to 500 characters")
        file, version = self._document(document); now = self.clock()
        self.prune()
        try:
            with self._connect() as db:
                db.execute("DELETE FROM facts WHERE expires<=?", (now,))
                db.execute("DELETE FROM cache WHERE expires<=?", (now,))
                stale = db.execute("UPDATE facts SET status='stale' WHERE file=? AND document_version<>? AND status='active'", (file, version)).rowcount
                db.execute("DELETE FROM cache WHERE file=? AND document_version<>?", (file, version))
                recent = [r[0] for r in db.execute("SELECT question FROM turns WHERE session_id=? AND file=? AND document_version=? AND created>=? ORDER BY created DESC LIMIT 3", (session_id,file,version,now-30*_DAY)).fetchall()][::-1]
                db.execute("INSERT INTO tasks(run_id,session_id,file,document_version,question,status,state_json,created,updated) VALUES(?,?,?,?,?,'running',NULL,?,?)", (run_id,session_id,file,version,question,now,now))
            return {"session_id": session_id, "recent_questions": recent, "stale_facts": stale}
        except MemoryStoreError: raise
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc: raise MemoryStoreError(f"begin failed: {exc}") from exc

    def checkpoint(self, run_id: str, node: str, state: dict) -> None:
        if not isinstance(state, dict) or not isinstance(node, str): raise MemoryStoreError("invalid checkpoint")
        try: payload = _json({"node": node, "state": state})
        except (TypeError, ValueError) as exc: raise MemoryStoreError(f"invalid checkpoint JSON: {exc}") from exc
        try:
            with self._connect() as db:
                cur = db.execute("UPDATE tasks SET state_json=?,updated=? WHERE run_id=?", (payload,self.clock(),run_id))
                if cur.rowcount != 1: raise MemoryStoreError("unknown run_id")
        except MemoryStoreError: raise
        except (sqlite3.Error,OSError,ValueError,TypeError) as exc: raise MemoryStoreError(f"checkpoint failed: {exc}") from exc

    def recall(self, question: str, document: dict, limit: int = 3) -> list[dict]:
        file,version=self._document(document)
        if not isinstance(question,str) or not question.strip() or len(question) > 500 or not isinstance(limit,int) or isinstance(limit,bool) or limit<0: raise MemoryStoreError("invalid recall arguments")
        limit=min(limit,3); now=self.clock(); wanted=_tokens(question); normalized=_norm(question)
        try:
            with self._connect() as db:
                rows=db.execute("SELECT * FROM facts WHERE file=? AND document_version=? AND status='active' AND expires>? ORDER BY created DESC LIMIT 200",(file,version,now)).fetchall()
            def score(row):
                overlap=len(wanted & (_tokens(row['question']) | _tokens(row['quote'])))
                return (0 if _norm(row['question'])==normalized else 1,-overlap,-row['created'])
            result=[]
            for row in sorted((r for r in rows if score(r)[0] == 0 or score(r)[1] < 0),key=score):
                result.append({"id":row['id'],"question":row['question'],"quote":row['quote'],"source":json.loads(row['source_json']),"reviewer":row['reviewer'],"created_at":_iso(row['created']),"verified_at":_iso(row['verified']),"expires_at":_iso(row['expires'])})
            return result[:limit]
        except MemoryStoreError: raise
        except (sqlite3.Error,OSError,ValueError,TypeError,json.JSONDecodeError) as exc: raise MemoryStoreError(f"recall failed: {exc}") from exc

    @staticmethod
    def _valid_fragments(value):
        return (isinstance(value, list) and len(value) <= 2 and all(
            isinstance(x, dict) and isinstance(x.get("text"), str) and len(x["text"]) <= 1200
            and isinstance(x.get("source"), dict) and isinstance(x.get("matched_terms"), list)
            and all(isinstance(x["source"].get(key), str) and x["source"][key] for key in ("file", "document_version", "fragment_id"))
            and type(x["source"].get("char_start")) is int and x["source"]["char_start"] >= 0
            and all(isinstance(term, str) for term in x["matched_terms"])
            and isinstance(x.get("matched_terms_count"), int) and not isinstance(x.get("matched_terms_count"), bool)
            and x["matched_terms_count"] >= 0
            for x in value))

    def get_cache(self, cache_key: str, document: dict) -> list | None:
        file,version=self._document(document)
        try:
            with self._connect() as db:
                row=db.execute("SELECT fragments_json FROM cache WHERE cache_key=? AND file=? AND document_version=? AND expires>?",(cache_key,file,version,self.clock())).fetchone()
            if not row:return None
            value=json.loads(row[0])
            if not self._valid_fragments(value) or any((item["source"]["file"], item["source"]["document_version"]) != (file, version) for item in value): raise ValueError("invalid cached fragments")
            return value
        except MemoryStoreError: raise
        except (sqlite3.Error,OSError,ValueError,TypeError,json.JSONDecodeError) as exc: raise MemoryStoreError(f"cache read failed: {exc}") from exc

    def put_cache(self, cache_key: str, document: dict, fragments: list) -> None:
        file,version=self._document(document)
        if not isinstance(cache_key,str) or not cache_key or not self._valid_fragments(fragments) or any((item["source"]["file"], item["source"]["document_version"]) != (file, version) for item in fragments): raise MemoryStoreError("invalid cache payload")
        try:
            payload=_json(fragments); now=self.clock()
            with self._connect() as db:
                db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?)",(cache_key,file,version,payload,now,now+_DAY))
        except MemoryStoreError: raise
        except (sqlite3.Error,OSError,ValueError,TypeError) as exc: raise MemoryStoreError(f"cache write failed: {exc}") from exc

    def complete(self, run_id: str, state: dict, *, validated_citations: list) -> dict:
        if not isinstance(state,dict) or not isinstance(validated_citations,list): raise MemoryStoreError("invalid completion")
        try: state_json=_json(state)
        except (TypeError,ValueError) as exc: raise MemoryStoreError(f"invalid state JSON: {exc}") from exc
        try:
            with self._connect() as db:
                task=db.execute("SELECT * FROM tasks WHERE run_id=?",(run_id,)).fetchone()
                if task is None: raise MemoryStoreError("unknown run_id")
                status=state.get("status","error"); doc=state.get("document") or {}
                if status not in {"answered", "insufficient_information", "technical_error"}:
                    raise MemoryStoreError("completion requires a terminal application status")
                if self._document(doc)!=(task['file'],task['document_version']): raise MemoryStoreError("completion document differs from begun task")
                result=state.get("result") or {}; review=state.get("review") or {}
                answer=result.get("answer") if isinstance(result,dict) else None
                if status == "answered":
                    sem = result.get("semantic_verification") if isinstance(result,dict) else None
                    if (not isinstance(answer,str) or not answer.strip() or not isinstance(review,dict) or review.get("approved") is not True
                        or not isinstance(sem,dict) or sem.get("status") != "model_approved" or not validated_citations):
                        raise MemoryStoreError("answered completion requires approved review, model approval, and validated citations")
                elif validated_citations:
                    raise MemoryStoreError("only answered completions may include citations")
                terminal_state = _json({"node":"finish","state":state})
                db.execute("UPDATE tasks SET status=?,state_json=?,updated=? WHERE run_id=?",(status,terminal_state,self.clock(),run_id))
                db.execute("INSERT INTO turns VALUES(?,?,?,?,?,?,?,?)",(run_id,task['session_id'],task['file'],task['document_version'],task['question'],answer if status=='answered' else None,status,self.clock()))
                eligible=status=='answered'
                written=0
                seen = set()
                if eligible:
                    for citation in validated_citations:
                        if not isinstance(citation,dict) or not isinstance(citation.get('quote'),str) or not citation['quote'] or len(citation['quote']) > 1200 or not isinstance(citation.get('source'),dict): raise MemoryStoreError("invalid validated citation")
                        source=citation['source']
                        line_start, line_end = source.get("line_start"), source.get("line_end")
                        char_start, char_end = source.get("char_start"), source.get("char_end")
                        valid_bounds = (isinstance(line_start,int) and not isinstance(line_start,bool) and line_start > 0
                            and isinstance(line_end,int) and not isinstance(line_end,bool) and line_end >= line_start
                            and isinstance(char_start,int) and not isinstance(char_start,bool) and char_start >= 0
                            and isinstance(char_end,int) and not isinstance(char_end,bool) and char_end >= char_start
                            and char_end - char_start == len(citation['quote']) and char_end - char_start <= 1200)
                        if (source.get('file')!=task['file'] or source.get('document_version')!=task['document_version']
                            or not isinstance(source.get('fragment_id'),str) or not source.get('fragment_id') or len(source['fragment_id']) > 100 or not valid_bounds):
                            raise MemoryStoreError("citation source conflicts with task document or has invalid offsets")
                        try: source_json=_json(source)
                        except (TypeError,ValueError) as exc: raise MemoryStoreError(f"invalid citation source: {exc}") from exc
                        now=self.clock(); expires=now+30*_DAY; qhash=hashlib.sha256(citation['quote'].encode('utf-8')).hexdigest()
                        if (source['fragment_id'], qhash) in seen:
                            continue
                        seen.add((source['fragment_id'], qhash))
                        reviewer=review.get('model') or ((result.get('semantic_verification') or {}).get('model') if isinstance(result,dict) else None)
                        db.execute("INSERT INTO facts(session_id,run_id,file,document_version,fragment_id,question,quote,quote_hash,source_json,reviewer,created,verified,expires,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active') ON CONFLICT(file,document_version,fragment_id,quote_hash) DO UPDATE SET verified=excluded.verified,expires=excluded.expires,status='active'",(task['session_id'],run_id,task['file'],task['document_version'],source['fragment_id'],task['question'],citation['quote'],qhash,source_json,reviewer,now,now,expires))
                        written+=1
            return {"facts_written":written,"turn_saved":True}
        except MemoryStoreError: raise
        except (sqlite3.Error,OSError,ValueError,TypeError) as exc: raise MemoryStoreError(f"completion failed: {exc}") from exc

    def history(self, session_id: str, limit: int = 10) -> list[dict]:
        self._id(session_id,"session_id")
        if not isinstance(limit,int) or isinstance(limit,bool) or limit<0: raise MemoryStoreError("invalid history limit")
        try:
            with self._connect() as db:
                rows=db.execute("SELECT run_id,file,document_version,question,answer,status,created FROM turns WHERE session_id=? ORDER BY created DESC LIMIT ?",(session_id,min(limit,500))).fetchall()
            return [dict(r,created_at=_iso(r['created'])) for r in reversed(rows)]
        except (sqlite3.Error,OSError,ValueError,TypeError) as exc: raise MemoryStoreError(f"history failed: {exc}") from exc

    def task(self, run_id: str) -> dict | None:
        """Return a task and its latest checkpoint for inspection; this is not resume support."""
        try:
            with self._connect() as db:
                row=db.execute("SELECT * FROM tasks WHERE run_id=?",(run_id,)).fetchone()
            if row is None: return None
            out=dict(row)
            try: out["state"] = json.loads(out.pop("state_json")) if out.get("state_json") else None
            except (ValueError,TypeError,json.JSONDecodeError) as exc: raise MemoryStoreError(f"invalid task state JSON: {exc}") from exc
            out["created_at"]=_iso(out.pop("created")); out["updated_at"]=_iso(out.pop("updated"))
            return out
        except MemoryStoreError: raise
        except (sqlite3.Error,OSError,ValueError,TypeError) as exc: raise MemoryStoreError(f"task read failed: {exc}") from exc

    def clear_cache(self) -> dict:
        """Explicitly clear the bounded fragment cache."""
        try:
            with self._connect() as db:
                count=db.execute("DELETE FROM cache").rowcount
            return {"cache_deleted":count}
        except (sqlite3.Error,OSError,ValueError,TypeError) as exc: raise MemoryStoreError(f"cache clear failed: {exc}") from exc

    def stats(self, session_id: str | None = None) -> dict:
        try:
            with self._connect() as db:
                out={}
                for table in ("tasks","turns","facts","cache"):
                    if session_id is not None and table in ("tasks","turns","facts"):
                        col="session_id"; out[table]=db.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?",(session_id,)).fetchone()[0]
                    else: out[table]=db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if session_id is None:
                    statuses = db.execute("SELECT status,COUNT(*) FROM tasks GROUP BY status")
                else:
                    statuses = db.execute("SELECT status,COUNT(*) FROM tasks WHERE session_id=? GROUP BY status", (session_id,))
                out["task_statuses"]={r[0]:r[1] for r in statuses}
                return out
        except (sqlite3.Error,OSError,ValueError,TypeError) as exc: raise MemoryStoreError(f"stats failed: {exc}") from exc

    def forget_session(self, session_id: str) -> dict:
        self._id(session_id,"session_id")
        try:
            with self._connect() as db:
                counts={t:db.execute(f"SELECT COUNT(*) FROM {t} WHERE session_id=?",(session_id,)).fetchone()[0] for t in ("turns","facts","tasks")}
                db.execute("DELETE FROM tasks WHERE session_id=?",(session_id,))
                db.execute("DELETE FROM facts WHERE session_id=?",(session_id,))
            return counts
        except (sqlite3.Error,OSError,ValueError,TypeError) as exc: raise MemoryStoreError(f"forget failed: {exc}") from exc

    def prune(self) -> dict:
        now=self.clock()
        try:
            with self._connect() as db:
                facts=db.execute("DELETE FROM facts WHERE expires<=?",(now,)).rowcount
                caches=db.execute("DELETE FROM cache WHERE expires<=?",(now,)).rowcount
                turns=db.execute("DELETE FROM turns WHERE created<?",(now-30*_DAY,)).rowcount
                # Remove corresponding old tasks (including errored tasks without turns).
                tasks=db.execute("DELETE FROM tasks WHERE created<?",(now-30*_DAY,)).rowcount
            return {"facts":facts,"cache":caches,"turns":turns,"tasks":tasks}
        except (sqlite3.Error,OSError,ValueError,TypeError) as exc: raise MemoryStoreError(f"prune failed: {exc}") from exc
