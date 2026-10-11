# 多阶段构建（spec/01 §5）。仓库根必须提供 .dockerignore 排除 .env。
FROM python:3.11-slim AS builder
WORKDIR /app
COPY backend_ingestion/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /install /usr/local
COPY backend_ingestion /app/backend_ingestion
# WORKDIR 必须指进 backend_ingestion/：代码内部全部是顶层 `from app import ...`
# 风格（main.py:32 实录），与 pytest / README / test_integration.sh 一致。
# 旧 CMD 用 `backend_ingestion.app.main:app` + WORKDIR /app，模块体执行到
# `from app import metrics` 即 ModuleNotFoundError —— 容器从未成功启动过，
# 2026-10-11 e2e 取证日志（dump_failure_logs）首次实证。
WORKDIR /app/backend_ingestion
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
