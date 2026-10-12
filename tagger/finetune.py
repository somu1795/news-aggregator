"""
Fine-tunes bge-small-en-v1.5 on data/seed.json and exports the fp16 ONNX model
the tagger image downloads (release tagger-model-v1). Run on a CUDA GPU, not in
the tagger image:

    pip install torch transformers onnx onnxconverter-common scikit-learn fastapi
    python finetune.py out/

Training is deterministic: the same inputs give the same file (sha256 in the
Dockerfile). It prints the result on data/eval.json, scored like the tagger's
retrain gate; data/eval.json is never trained on.
"""

import json
import random
import sys
from pathlib import Path

import onnx
import torch
from onnxconverter_common import float16
from transformers import AutoModel, AutoTokenizer

from tagger import BUNDLED, OTHER, TOPICS, evaluate, train

BASE = "BAAI/bge-small-en-v1.5"
EPOCHS, LR, BATCH, SEED = 4, 5e-5, 32, 0
CLASSES = TOPICS[:5] + [OTHER, "uplifting"]  # Every seed label

tokenizer = AutoTokenizer.from_pretrained(BASE)


def encode(titles):
    return tokenizer(titles, padding=True, truncation=True, max_length=48, return_tensors="pt").to("cuda")


def cls(encoder, titles):
    return torch.nn.functional.normalize(encoder(**encode(titles)).last_hidden_state[:, 0], dim=-1)


def finetune(rows):
    """The encoder with a softmax head over the seed labels and an uplifting head (as tagger.train), class-balanced."""
    torch.manual_seed(SEED)
    random.seed(SEED)
    encoder = AutoModel.from_pretrained(BASE).to("cuda")
    topic_head = torch.nn.Linear(384, len(CLASSES)).to("cuda")
    uplifting_head = torch.nn.Linear(384, 1).to("cuda")
    y = torch.tensor([CLASSES.index(r["label"]) for r in rows])
    uplifting = CLASSES.index("uplifting")
    counts = torch.bincount(y, minlength=len(CLASSES)).float()
    topic_loss = torch.nn.CrossEntropyLoss(weight=(counts.sum() / (len(CLASSES) * counts)).to("cuda"))
    positives = (y == uplifting).sum()
    uplifting_loss = torch.nn.BCEWithLogitsLoss(pos_weight=((len(y) - positives) / positives).reshape(1).to("cuda"))
    params = [*encoder.parameters(), *topic_head.parameters(), *uplifting_head.parameters()]
    optimizer = torch.optim.AdamW(params, lr=LR, weight_decay=0.01)
    steps = EPOCHS * ((len(rows) + BATCH - 1) // BATCH)
    # 10% linear warm-up, then linear decay
    schedule = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda s: min(1, s / (0.1 * steps)) * max(0, (steps - s) / steps))
    order = list(range(len(rows)))
    for _ in range(EPOCHS):
        encoder.train()
        random.shuffle(order)
        for start in range(0, len(order), BATCH):
            batch = order[start:start + BATCH]
            h = cls(encoder, [rows[i]["title"] for i in batch]) * 20  # Scaled cosine features
            yb = y[batch].to("cuda")
            loss = topic_loss(topic_head(h), yb) + uplifting_loss(uplifting_head(h).squeeze(-1), (yb == uplifting).float())
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            schedule.step()
    return encoder.eval()


def embed(encoder, titles):
    with torch.no_grad():
        return torch.cat([cls(encoder, titles[i:i + 128]) for i in range(0, len(titles), 128)]).cpu().numpy()


class LastHiddenState(torch.nn.Module):
    """The interface of the original ONNX export, which the tagger's Embedder expects."""

    def __init__(self, encoder):
        super().__init__()
        self.encoder = encoder

    def forward(self, input_ids, attention_mask, token_type_ids):
        return self.encoder(input_ids=input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids).last_hidden_state


def main(out: Path):
    seed = json.loads((BUNDLED / "seed.json").read_text())
    eval_set = json.loads((BUNDLED / "eval.json").read_text())
    encoder = finetune(seed)
    classifier = train(embed(encoder, [r["title"] for r in seed]), [r["label"] for r in seed])
    print("eval:", evaluate(classifier, embed(encoder, [r["title"] for r in eval_set]), [r["tags"] for r in eval_set]))

    out.mkdir(parents=True, exist_ok=True)
    names = ["input_ids", "attention_mask", "token_type_ids", "last_hidden_state"]
    ids = torch.ones(2, 48, dtype=torch.long)
    torch.onnx.export(LastHiddenState(encoder).cpu(), (ids, ids, torch.zeros_like(ids)), out / "model_fp32.onnx",
                      dynamo=False, opset_version=17, input_names=names[:3], output_names=names[3:],
                      dynamic_axes={n: {0: "batch", 1: "seq"} for n in names})
    model = float16.convert_float_to_float16(onnx.load(out / "model_fp32.onnx"), keep_io_types=True)
    onnx.save(model, out / "bge-small-en-v1.5-news-ft1.onnx")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
