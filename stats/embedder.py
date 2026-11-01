"""
Title embeddings on the CPU: multilingual-e5-small (fp16 ONNX, mean pooling,
"query: " prefix, L2-normalized), the same model and settings as the tagger's
story grouping (tagger/embedder.py).
"""

from pathlib import Path
from typing import List

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

BATCH, MAX_TOKENS, DIMENSIONS = 32, 48, 384
PREFIX = "query: "


class Embedder:
    def __init__(self, model_dir: Path):
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_dir / "model.onnx"), sess_options=options,
                                            providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def embed(self, titles: List[str]) -> np.ndarray:
        out = []
        for start in range(0, len(titles), BATCH):
            encoded = self.tokenizer.encode_batch([PREFIX + t for t in titles[start:start + BATCH]])
            ids = np.array([e.ids for e in encoded], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feeds)[0]
            weights = mask[:, :, None]
            out.append((hidden * weights).sum(axis=1) / weights.sum(axis=1))
        vectors = np.concatenate(out) if out else np.zeros((0, DIMENSIONS), dtype=np.float32)
        return (vectors / np.linalg.norm(vectors, axis=1, keepdims=True)).astype(np.float32) if len(vectors) else vectors
