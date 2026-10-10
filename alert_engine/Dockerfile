# 告警引擎镜像。
#
# 基础镜像选 python:3.11-slim（与 backend_ingestion/Dockerfile 一致）：
# 3.11 有 asyncpg 预编译 wheel，构建不需要 gcc 工具链，镜像更小、构建更快。
# 注：asyncpg 0.29.0 在 3.13 上编译失败已在 STATUS.md 记录；部署侧锁 3.11 规避，
# 开发侧升级到 0.32.0（订单 08）另行推进。
FROM python:3.11-slim

WORKDIR /app

COPY alert_engine/requirements.txt /app/alert_engine/requirements.txt
RUN pip install --no-cache-dir -r /app/alert_engine/requirements.txt

COPY alert_engine /app/alert_engine

# 8001 = Prometheus 指标端点（alert_engine/app/metrics.py）
EXPOSE 8001
CMD ["python", "-m", "alert_engine.app.main"]
