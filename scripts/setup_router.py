"""Однократная загрузка фиксированной версии NLI-модели и динамическое квантование CPU."""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

from _project import ROOT



def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    from onnxruntime.quantization import QuantType, quantize_dynamic
    settings = json.loads((ROOT / "router_settings.json").read_text(encoding="utf-8"))
    directory = Path(settings["directory"])
    directory.mkdir(parents=True, exist_ok=True)
    api = f"https://huggingface.co/api/models/{settings['model_id']}/revision/{settings['revision']}?blobs=true"
    metadata = json.load(urllib.request.urlopen(api, timeout=30))
    if metadata["sha"] != settings["revision"]:
        raise RuntimeError("Ревизия источника не совпала")
    files = {item["rfilename"]: item for item in metadata["siblings"]}
    checksums = {}
    for name in ("model.onnx", "tokenizer.json", "config.json"):
        remote = "onnx/" + name
        target = directory / name
        expected = files[remote].get("lfs", {}).get("sha256")
        if not target.exists() or (expected and sha(target) != expected):
            print(f"Загрузка {remote}…", flush=True)
            url = f"https://huggingface.co/{settings['model_id']}/resolve/{settings['revision']}/{remote}"
            temporary = target.with_suffix(target.suffix + ".part")
            with urllib.request.urlopen(url, timeout=120) as source, temporary.open("wb") as output:
                while block := source.read(1024 * 1024):
                    output.write(block)
            if expected and sha(temporary) != expected:
                raise RuntimeError(f"Не совпала контрольная сумма {name}")
            temporary.replace(target)
        checksums[name] = sha(target)
    quantized = directory / "model.int8.onnx"
    print("Динамическое квантование MatMul/Gather в INT8…", flush=True)
    quantize_dynamic(str(directory / "model.onnx"), str(quantized), weight_type=QuantType.QInt8, op_types_to_quantize=["MatMul", "Gather"])
    checksums[quantized.name] = sha(quantized)
    manifest = {"model_id": settings["model_id"], "revision": settings["revision"], "quantization": "dynamic-int8-MatMul-Gather", "files": checksums}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Модель готова: {quantized}; размер: {quantized.stat().st_size / 2**20:.1f} МиБ", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
