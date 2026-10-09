# 多阶段构建（spec/01 §5）。仓库根必须提供 .dockerignore 排除 .env。
FROM python:3.11-slim AS builder
WORKDIR /app
COPY backend_ingestion/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /install /usr/local
COPY backend_ingestion /app/backend_ingestion
EXPOSE 8000
CMD ["uvicorn", "backend_ingestion.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
