# Multi-stage build (spec/01 §5). Repo root must provide .dockerignore to exclude .env.
FROM python:3.11-slim AS builder
WORKDIR /app
COPY backend_ingestion/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /install /usr/local
COPY backend_ingestion /app/backend_ingestion
# WORKDIR must point into backend_ingestion/: all code internally uses top-level `from app import ...`
# style (verified at main.py:32), consistent with pytest / README / test_integration.sh.
# The old CMD used `backend_ingestion.app.main:app` + WORKDIR /app; when the module body executed
# `from app import metrics` it raised ModuleNotFoundError — the container never started successfully,
# first empirically proven by the 2026-10-11 e2e forensic logs (dump_failure_logs).
WORKDIR /app/backend_ingestion
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
