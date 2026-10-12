# Ubuntu 24.04: Intel publishes the NPU user-space driver as Ubuntu packages.
FROM ubuntu:24.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Ubuntu's Level Zero loader (1.16) only looks for the driver under its old name
    ZE_ENABLE_ALT_DRIVERS=libze_intel_npu.so.1

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-venv libtbb12 libze1 ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Intel NPU driver (user space only; the kernel driver intel_vpu is the host's)
ADD --checksum=sha256:1efcd4b60c22abee751d8f2705962cbcc2a569de45c7e0e670cf08afbfcdc1d2 \
    https://github.com/intel/linux-npu-driver/releases/download/v1.38.0/linux-npu-driver-v1.38.0.20260910-34487311128-ubuntu2404.tar.gz \
    /tmp/npu-driver.tar.gz
RUN mkdir /tmp/npu-driver && tar xzf /tmp/npu-driver.tar.gz -C /tmp/npu-driver && \
    apt-get update && \
    apt-get install -y --no-install-recommends /tmp/npu-driver/intel-level-zero-npu_*.deb /tmp/npu-driver/intel-driver-compiler-npu_*.deb && \
    rm -rf /tmp/npu-driver* /var/lib/apt/lists/*

# bge-small-en-v1.5 (MIT), fine-tuned on data/seed.json by finetune.py (fp16 ONNX);
# its tokenizer is the original's, from a pinned revision
ADD --checksum=sha256:2b5a19a34489c1b17d2a6769409f903ab496d3a4f59ea847ec218a9c7731b569 \
    https://github.com/somu1795/news-aggregator/releases/download/tagger-model-v1/bge-small-en-v1.5-news-ft1.onnx \
    /models/bge-small-en-v1.5/model.onnx
ADD --checksum=sha256:d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66 \
    https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/ea104dacec62c0de699686887e3f920caeb4f3e3/tokenizer.json \
    /models/bge-small-en-v1.5/tokenizer.json
RUN chmod -R a+rX /models

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt
ENV PATH=/venv/bin:$PATH

RUN groupadd --system --gid 1001 app && useradd --system --uid 1001 --gid app app && \
    mkdir /data && chown app:app /data
WORKDIR /tagger
COPY --chown=app:app tagger.py .
COPY --chown=app:app data ./data
USER app

EXPOSE 8000
CMD ["uvicorn", "tagger:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]
