FROM python:3.14.8-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# multilingual-e5-small (MIT), fp16 ONNX: the tagger's story model (tagger/Dockerfile)
ADD --checksum=sha256:0e0fe349c99ea21c6f3aa273af21f7fb753c1e1174ef1647032029c2be3251c3 \
    https://huggingface.co/Xenova/multilingual-e5-small/resolve/761b726dd34fb83930e26aab4e9ac3899aa1fa78/onnx/model_fp16.onnx \
    /models/multilingual-e5-small/model.onnx
ADD --checksum=sha256:0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39 \
    https://huggingface.co/Xenova/multilingual-e5-small/resolve/761b726dd34fb83930e26aab4e9ac3899aa1fa78/tokenizer.json \
    /models/multilingual-e5-small/tokenizer.json
RUN chmod -R a+rX /models

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN addgroup --system --gid 1001 app && adduser --system --uid 1001 --gid 1001 app && \
    mkdir /data /public && chown app:app /data /public
WORKDIR /stats
COPY --chown=app:app worker.py backfill.py store.py stories.py scoring.py places.py models.py embedder.py ./
COPY --chown=app:app data/places.json data/homes.json ./data/
COPY --chown=app:app model ./model
USER app

CMD ["python", "worker.py"]
