# Alert engine image.
#
# Base image is python:3.11-slim (matches backend_ingestion/Dockerfile):
# 3.11 has a prebuilt asyncpg wheel, so the build needs no gcc toolchain, yielding a smaller image and faster build.
# Note: asyncpg 0.29.0 failing to compile on 3.13 is recorded in STATUS.md; the deployment side pins 3.11 to avoid this,
# while the development side's upgrade to 0.32.0 (order 08) is tracked separately.
FROM python:3.11-slim

WORKDIR /app

COPY alert_engine/requirements.txt /app/alert_engine/requirements.txt
RUN pip install --no-cache-dir -r /app/alert_engine/requirements.txt

COPY alert_engine /app/alert_engine

# 8001 = Prometheus metrics endpoint (alert_engine/app/metrics.py)
EXPOSE 8001
CMD ["python", "-m", "alert_engine.app.main"]
