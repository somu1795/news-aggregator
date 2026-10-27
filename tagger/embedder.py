"""
Sentence embeddings on the Intel NPU (via OpenVINO), or the CPU when there is
none: bge-small for topics, multilingual-e5-small for story grouping.
"""

import logging
from pathlib import Path
from typing import List

import numpy as np

logger = logging.getLogger("tagger")

# NPUs need static input shapes; headlines fit in 48 tokens
BATCH, MAX_TOKENS = 32, 48


class Embedder:
    """
    A sentence embedding model via OpenVINO, L2-normalized: bge-small (CLS pooling) for
    topics, multilingual-e5-small (mean pooling, "query: " prefix) for story grouping.
    """

    def __init__(self, model_dir: Path, device: str, cache_dir: Path, pooling: str = "cls", prefix: str = "") -> None:
        self.pooling, self.prefix = pooling, prefix
        import openvino as ov
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding(length=MAX_TOKENS)
        core = ov.Core()
        model = core.read_model(model_dir / "model.onnx")
        model.reshape({i.get_any_name(): [BATCH, MAX_TOKENS] for i in model.inputs})
        # The NPU compile takes ~1 minute; CACHE_DIR makes later starts take seconds
        config = {"PERFORMANCE_HINT": "THROUGHPUT", "CACHE_DIR": str(cache_dir)}
        try:
            self.compiled = core.compile_model(model, device, config)
            self.device = device
        except RuntimeError as e:
            if device == "CPU":
                raise
            logger.warning("Could not use %s (%s), falling back to the CPU", device, e)
            self.compiled = core.compile_model(model, "CPU", config)
            self.device = "CPU"

    def embed(self, titles: List[str]) -> np.ndarray:
        out = []
        for start in range(0, len(titles), BATCH):
            batch = titles[start:start + BATCH]
            encoded = self.tokenizer.encode_batch([self.prefix + t for t in batch] + [""] * (BATCH - len(batch)))
            ids = np.array([e.ids for e in encoded], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if any(i.get_any_name() == "token_type_ids" for i in self.compiled.inputs):
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.compiled(feeds)[0][: len(batch)]
            if self.pooling == "cls":
                out.append(hidden[:, 0, :])
            else:
                mask = mask[: len(batch), :, None]
                out.append((hidden * mask).sum(axis=1) / mask.sum(axis=1))
        vectors = np.concatenate(out) if out else np.zeros((0, 384), dtype=np.float32)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
