# Build context: the repository root (infra/package.py ships edgeml/, ml/, and modules/).
# Stage 1 trains the model from code; stage 2 carries only the ONNX model and its runtime.

FROM python:3.12-slim-bookworm AS train
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
COPY ml/requirements.txt ml/requirements.txt
RUN pip install --no-cache-dir -r ml/requirements.txt
COPY edgeml edgeml
COPY ml ml
ARG MODEL=v1
RUN python -m ml.train --config "ml/models/${MODEL}.json" --out /model

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MODEL_DIR=/app/model
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin module
WORKDIR /app

COPY modules/anomaly-detector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY edgeml edgeml
COPY modules/anomaly-detector/detector.py modules/anomaly-detector/anomaly_detector.py ./
COPY --from=train /model /app/model

USER module
CMD ["python", "anomaly_detector.py"]
