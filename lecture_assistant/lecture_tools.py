"""Инструменты для одной выбранной лекции. Модели и сеть здесь не используются."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from uuid import uuid4

PROJECT_DIR = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 2 * 1024 * 1024
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 160
STOP_WORDS = frozenset("а без бы был была были было в во вот все где да для до его ее если есть же за и из или их к как какой какая какие ко кто ли на над не но о об от по под при про с со так такое такая также то у уже что это этот эта эти я".split())


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def failure(code: str, message: str) -> dict:
    return {"ok": False, "data": None, "error": {"code": code, "message": message}}


def tool(function):
    """Единый формат результата; программные ошибки не маскируются как ошибки данных."""
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            result = {"ok": True, "data": function(self, *args, **kwargs), "error": None}
        except ToolError as exc:
            result = failure(exc.code, str(exc))
        except OSError:
            result = failure("IO_ERROR", "Не удалось прочитать или записать файл. Проверьте его доступность и права доступа.")
        self.events.append({
            "tool": function.__name__, "ok": result["ok"],
            "error_code": None if result["ok"] else result["error"]["code"],
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        return result
    return wrapped


def checked_text(value, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ToolError("INVALID_ARGUMENT", f"{name}: требуется непустая строка длиной до {maximum} символов.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolError("INVALID_ARGUMENT", f"{name}: строка содержит недопустимые символы Unicode.") from exc
    return value


@dataclass(frozen=True)
class Fragment:
    fragment_id: str
    start: int
    end: int
    section: str


@dataclass(frozen=True)
class Document:
    version: str
    text: str
    fragments: tuple[Fragment, ...]


class LectureTools:
    """Выбор lecture_name делает приложение/пользователь, а не модель.

    project_dir — настройка приложения; она не является аргументом инструмента.
    """

    TOOL_NAMES = ("read_lecture", "search_fragments", "get_quote", "save_result")

    def __init__(self, lecture_name: str, project_dir: Path = PROJECT_DIR):
        self.project_dir = Path(project_dir).resolve()
        self.lecture_name = lecture_name
        self._document: Document | None = None
        # Только технические события текущего процесса, без полных текстов лекции.
        self.events: list[dict] = []

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Диспетчер для будущего агента: разрешены только четыре фиксированные функции."""
        if not isinstance(name, str) or name not in self.TOOL_NAMES:
            return failure("UNKNOWN_TOOL", "Неизвестный инструмент.")
        if not isinstance(arguments, dict) or any(not isinstance(key, str) for key in arguments):
            return failure("INVALID_ARGUMENT", "Аргументы инструмента должны быть объектом с именованными полями.")
        function = getattr(self, name)
        try:
            inspect.signature(function).bind(**arguments)
        except TypeError:
            return failure("INVALID_ARGUMENT", "Отсутствуют обязательные аргументы или переданы лишние поля.")
        return function(**arguments)

    def _lecture_path(self) -> Path:
        name = checked_text(self.lecture_name, "lecture_name", 255)
        if name in {".", ".."} or any(char in name for char in '/\\:') or any(ord(char) < 32 for char in name):
            raise ToolError("PATH_NOT_ALLOWED", "Выберите имя файла из data/lectures, без пути к другой папке.")
        if Path(name).suffix.lower() not in {".txt", ".md"}:
            raise ToolError("UNSUPPORTED_FORMAT", "Поддерживаются только файлы .txt и .md.")
        try:
            directory = (self.project_dir / "data" / "lectures").resolve()
            path = (directory / name).resolve()
        except RuntimeError as exc:
            raise ToolError("PATH_NOT_ALLOWED", "Не удалось разрешить путь: проверьте ссылки на файл и папки.") from exc
        if not directory.is_relative_to(self.project_dir) or path.parent != directory:
            raise ToolError("PATH_NOT_ALLOWED", "Файл или ссылка ведёт за пределы разрешённой папки.")
        return path

    def _read_bytes(self) -> bytes:
        path = self._lecture_path()
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError as exc:
            raise ToolError("FILE_NOT_FOUND", "Файл лекции не найден в data/lectures.") from exc
        if len(raw) > MAX_FILE_BYTES:
            raise ToolError("FILE_TOO_LARGE", "На этом этапе размер лекции ограничен 2 МиБ.")
        return raw

    def _current_document(self) -> Document:
        if self._document is None:
            raise ToolError("DOCUMENT_NOT_LOADED", "Сначала вызовите read_lecture.")
        if hashlib.sha256(self._read_bytes()).hexdigest() != self._document.version:
            raise ToolError("STALE_DOCUMENT", "Лекция изменилась. Вызовите read_lecture и заново выполните поиск.")
        return self._document

    @staticmethod
    def _split(text: str, version: str) -> tuple[Fragment, ...]:
        # Markdown-заголовки задают границы разделов; .txt без заголовков — один раздел.
        headings = list(re.finditer(r"(?m)^#{1,6}[ \t]+([^\r\n]+)", text))
        sections = []
        if not headings or headings[0].start() > 0:
            sections.append((0, headings[0].start() if headings else len(text), "Начало документа"))
        for index, heading in enumerate(headings):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            sections.append((heading.start(), end, heading.group(1).strip()))
        fragments = []
        for start, section_end, section in sections:
            while start < section_end:
                end = min(start + CHUNK_SIZE, section_end)
                if end < section_end:
                    boundary = max(text.rfind("\n", start + CHUNK_SIZE // 2, end), text.rfind(" ", start + CHUNK_SIZE // 2, end))
                    if boundary >= 0:
                        end = boundary + 1
                if text[start:end].strip():
                    fragments.append(Fragment(f"{version[:16]}:{len(fragments) + 1:04d}", start, end, section))
                if end == section_end:
                    break
                start = end - CHUNK_OVERLAP
        return tuple(fragments)

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {word for word in re.findall(r"[^\W_]+", text.casefold().replace("ё", "е")) if word not in STOP_WORDS}

    def _source(self, document: Document, fragment: Fragment, start: int, end: int) -> dict:
        return {
            "file": self.lecture_name, "document_version": document.version,
            "fragment_id": fragment.fragment_id, "section": fragment.section,
            "line_start": document.text.count("\n", 0, start) + 1,
            "line_end": document.text.count("\n", 0, max(start, end - 1)) + 1,
            "char_start": start, "char_end": end,
        }

    def _quote(self, document: Document, fragment_id: str, quote: str) -> dict:
        checked_text(fragment_id, "fragment_id", 100)
        checked_text(quote, "quote", CHUNK_SIZE)
        fragment = next((part for part in document.fragments if part.fragment_id == fragment_id), None)
        if fragment is None:
            raise ToolError("UNKNOWN_FRAGMENT", "Фрагмент не принадлежит загруженной версии лекции.")
        position = document.text[fragment.start:fragment.end].find(quote)
        if position < 0:
            raise ToolError("QUOTE_NOT_FOUND", "Цитата не совпадает дословно с текстом указанного фрагмента.")
        start = fragment.start + position
        return {"quote": quote, "source": self._source(document, fragment, start, start + len(quote))}

    @tool
    def read_lecture(self) -> dict:
        """Прочитать выбранный файл, вернуть сведения о нём, подготовить фрагменты для поиска."""
        # После неудачной перезагрузки прежний снимок не используется.
        self._document = None
        raw = self._read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolError("INVALID_ENCODING", "Сохраните лекцию в кодировке UTF-8.") from exc
        if not text.strip():
            raise ToolError("EMPTY_DOCUMENT", "Файл лекции пуст.")
        if "\x00" in text:
            raise ToolError("INVALID_TEXT", "В файле обнаружены нулевые байты; нужен обычный текстовый файл.")
        version = hashlib.sha256(raw).hexdigest()
        self._document = Document(version, text, self._split(text, version))
        return {"file": self.lecture_name, "document_version": version, "bytes": len(raw), "characters": len(text), "fragments": len(self._document.fragments)}

    @tool
    def search_fragments(self, query: str, limit: int = 3) -> dict:
        """Найти до limit фрагментов по словам запроса, отсутствие совпадений - нормальный результат."""
        checked_text(query, "query", 500)
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ToolError("INVALID_ARGUMENT", "limit должен быть целым числом от 1 до 5.")
        document = self._current_document()
        terms = self._terms(query)
        matches = []
        for fragment in document.fragments:
            text = document.text[fragment.start:fragment.end]
            matched = terms & self._terms(text)
            if matched:
                matches.append((fragment, matched))
        matches.sort(key=lambda item: (-len(item[1]), item[0].start))
        # Расположение вычисляется только для выбранных фрагментов, а не всего файла.
        selected = [{
            "text": document.text[fragment.start:fragment.end],
            "matched_terms": sorted(matched), "matched_terms_count": len(matched),
            "source": self._source(document, fragment, fragment.start, fragment.end),
        } for fragment, matched in matches[:limit]]
        return {"query": query, "found": bool(matches), "fragments": selected}

    @tool
    def get_quote(self, fragment_id: str, quote: str) -> dict:
        """Проверить точную цитату и вернуть её расположение в исходном тексте."""
        return self._quote(self._current_document(), fragment_id, quote)

    @tool
    def save_result(self, question: str, answer: str, citations: list[dict]) -> dict:
        """Сохранить ответ с проверенными дословными цитатами. Проверки смысла здесь нет."""
        checked_text(question, "question", 2000)
        checked_text(answer, "answer", 10000)
        if not isinstance(citations, list) or not 1 <= len(citations) <= 10:
            raise ToolError("INVALID_ARGUMENT", "Для сохранения ответа требуется от 1 до 10 цитат.")
        document = self._current_document()
        verified = []
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {"fragment_id", "quote"}:
                raise ToolError("INVALID_ARGUMENT", "Каждая цитата должна содержать только fragment_id и quote.")
            verified.append(self._quote(document, citation["fragment_id"], citation["quote"]))
        result_id = uuid4().hex
        record = {
            "result_id": result_id, "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "question": question, "answer": answer, "citations": verified,
            "citation_verification": "exact_match", "semantic_verification": "not_performed",
        }
        try:
            directory = (self.project_dir / "results").resolve()
        except RuntimeError as exc:
            raise ToolError("PATH_NOT_ALLOWED", "Не удалось разрешить путь папки результатов.") from exc
        if not directory.is_relative_to(self.project_dir):
            raise ToolError("PATH_NOT_ALLOWED", "Папка результатов ведёт за пределы проекта.")
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"answer-{result_id}.json"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=".answer-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(record, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"result_id": result_id, "file": destination.relative_to(self.project_dir).as_posix(), "citation_count": len(verified), "semantic_verification": "not_performed"}
