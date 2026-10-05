FROM python:3.10-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    OCR_WORKERS=2

# opencv-python (required by ppocr-onnx) needs these shared libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Isolate the project's older dependency pins from base-image build tooling.
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir wheel==0.41.3
ENV PATH="/opt/venv/bin:${PATH}"
COPY deploy/ocr/requirements.txt /app/deploy/ocr/requirements.txt
RUN pip install --no-cache-dir -r deploy/ocr/requirements.txt && pip check

# Only these OAS modules are required by run_ocr_server().
COPY module/exception.py module/logger.py /app/module/
COPY module/ocr/rpc.py module/ocr/ppocr.py /app/module/ocr/
COPY deploy/ocr/server.py deploy/ocr/healthcheck.py /app/deploy/ocr/

RUN useradd --uid 10001 --create-home ocr \
    && mkdir /app/log && chown ocr:ocr /app/log
USER ocr
EXPOSE 22268
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "deploy.ocr.healthcheck"]
CMD ["python", "-m", "deploy.ocr.server"]
