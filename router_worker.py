"""Отдельный процесс локального NLI-классификатора"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Router:
    def __init__(self):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        self.settings = json.loads((ROOT / "router_settings.json").read_text(encoding="utf-8"))
        directory = Path(self.settings["directory"])
        self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if self.manifest["revision"] != self.settings["revision"]:
            raise ValueError("Модель и настройки относятся к разным версиям")
        for name in ("model.int8.onnx", "tokenizer.json", "config.json"):
            digest = hashlib.sha256()
            with (directory / name).open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != self.manifest["files"][name]:
                raise ValueError(f"Повреждён файл модели: {name}")
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "model.int8.onnx"), sess_options=options, providers=["CPUExecutionProvider"])
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        self.entailment = next(int(index) for index, name in config["id2label"].items() if name == "entailment")
        self.settings_sha256 = hashlib.sha256((ROOT / "router_settings.json").read_bytes()).hexdigest()

    def decide(self, question):
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            raise ValueError("Нужен вопрос длиной от 1 до 500 символов")
        started = time.perf_counter()
        roles = ["simple", "complex"]
        encoded = self.tokenizer.encode_batch([(question, self.settings["hypotheses"][role]) for role in roles])
        arrays = {"input_ids": [item.ids for item in encoded], "attention_mask": [item.attention_mask for item in encoded], "token_type_ids": [item.type_ids for item in encoded]}
        inputs = {node.name: self.np.asarray(arrays[node.name], dtype=self.np.int64) for node in self.session.get_inputs()}
        logits = self.session.run(None, inputs)[0]
        values = logits[:, self.entailment]
        weights = self.np.exp(values - values.max())
        scores = weights / weights.sum()
        if not self.np.isfinite(scores).all():
            raise ValueError("Получены некорректные оценки")
        predicted = roles[int(scores.argmax())]
        margin = float(abs(scores[0] - scores[1]))
        uncertain = margin <= self.settings["margin_threshold"]
        return {"route": "complex" if uncertain else predicted, "predicted_route": predicted, "scores": {role: float(scores[index]) for index, role in enumerate(roles)}, "uncertain": uncertain, "margin": margin, "score_type": "uncalibrated_label_softmax", "model_id": self.settings["model_id"], "revision": self.settings["revision"], "quantization": self.manifest["quantization"], "model_sha256": self.manifest["files"]["model.int8.onnx"], "settings_sha256": self.settings_sha256, "inference_seconds": round(time.perf_counter() - started, 4)}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    try:
        result = Router().decide(args.question)
        result["worker_seconds"] = round(time.perf_counter() - started, 3)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        raise SystemExit(1)
