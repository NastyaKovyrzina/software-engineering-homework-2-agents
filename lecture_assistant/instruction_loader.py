"""Проверенный снимок инструкций: один набор файлов на один пользовательский запрос."""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DIRECTORY = Path(__file__).resolve().parent.parent / "instructions"


class InstructionError(RuntimeError):
    code = "INSTRUCTIONS_ERROR"


@dataclass(frozen=True)
class Bundle:
    text: str
    metadata: dict


class InstructionSnapshot:
    def __init__(self, directory=DEFAULT_DIRECTORY):
        self.directory = Path(directory).resolve()
        self.files = {}
        try:
            manifest_raw = self._read("manifest.json", ".json")
            manifest = json.loads(manifest_raw.decode("utf-8-sig"))
            if not isinstance(manifest, dict) or manifest.get("version") != 1 or set(manifest.get("roles", {})) != {"search", "answer", "review", "revise"}:
                raise ValueError("Неверная версия или список ролей в manifest.json")
            shared = manifest.get("shared")
            if not isinstance(shared, list) or not shared:
                raise ValueError("Не указан общий контекст")
            self.bundles = {}
            for role, spec in manifest["roles"].items():
                if not isinstance(spec, dict) or set(spec) != {"system", "skills"} or not isinstance(spec["skills"], list) or not spec["skills"]:
                    raise ValueError(f"Неверное описание роли {role}")
                names = [spec["system"], *shared, *spec["skills"]]
                if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
                    raise ValueError("Неверные или повторные имена файлов")
                parts = []
                for name in names:
                    if name not in self.files:
                        raw = self._read(name, ".md")
                        text = raw.decode("utf-8-sig").strip()
                        if not text:
                            raise ValueError(f"Пустая инструкция: {name}")
                        self.files[name] = {"text": text, "sha256": hashlib.sha256(raw).hexdigest()}
                    parts.append(f"[Инструкция: {name}]\n{self.files[name]['text']}")
                text = "\n\n".join(parts)
                if len(text) > 4000:
                    raise ValueError(f"Комплект {role} превышает 4000 символов; сократите инструкции для небольшого контекста")
                metadata = {"bundle": role, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "files": [{"path": name, "sha256": self.files[name]["sha256"]} for name in names], "characters": len(text)}
                self.bundles[role] = Bundle(text, metadata)
            self.manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        except (OSError, ValueError, TypeError) as exc:
            raise InstructionError(f"Не удалось загрузить инструкции: {exc}") from exc

    def _read(self, name, extension):
        if not isinstance(name, str) or not name or "\\" in name or ":" in name:
            raise ValueError("Недопустимое имя инструкции")
        path = (self.directory / name).resolve()
        if not path.is_relative_to(self.directory) or path.suffix != extension:
            raise ValueError(f"Инструкция выходит за разрешённую папку или имеет неверный формат: {name}")
        with path.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError(f"Инструкция слишком велика: {name}")
        return raw

    def bundle(self, role):
        return self.bundles[role]

    def metadata(self):
        return {"manifest_sha256": self.manifest_sha256, "bundles": {name: bundle.metadata for name, bundle in self.bundles.items()}}
