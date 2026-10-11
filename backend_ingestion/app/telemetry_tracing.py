"""OpenTelemetry 链路接线（订单 16 T3）：MQTT → 后端 → DB。

设计前提：**默认关闭，必须可降级**（订单 16 §1 T3 + §2 验收）。
工厂内网经常没有 OTLP collector，若 import 时就硬依赖 OTel 包，
或 endpoint 未配置就抛错，本服务会在**没开观测的环境**直接起不来 ——
这是"为可观测性牺牲可用性"，方向反了。

因此本模块的分层是：
  1. 所有 OTel import 放在函数体内（模块顶层零依赖）→ 没装包也不影响 `import app.main`；
  2. `configure_tracing()` 是**唯一**入口，任何异常都在内部消化并降级为 no-op；
  3. 返回 bool 表示"是否真的启用"，供启动日志与测试断言。

覆盖的链路（对应订单 16 的 MQTT → 后端 → DB）：
  - FastAPI：`FastAPIInstrumentor.instrument_app` → 每个 REST/WS 请求一根 span；
  - asyncpg：`AsyncPGInstrumentor().instrument()` → 每次 DB 往返一根子 span。
    batch_writer 的 COPY / executemany 全走 asyncpg，所以落库链自动可见。

为什么不用 `opentelemetry-instrument` 自动注入（`opentelemetry-distro`）：
那要求在启动命令前面包一层 CLI（`opentelemetry-instrument uvicorn ...`），
会改动 Dockerfile 的 CMD 与本地跑法；本模块走**代码内显式接线**，
不改任何启动方式，也不依赖环境里存在那个 CLI。改动面最小。

版本对齐（实测通过，见 PR「实测」章节）：OTel Python 的 API/SDK 是 1.x，
而 instrumentation 包是 0.x b 版（`0.43b0` 对应 SDK `1.22.0`）——
**两者必须成对安装**，且 lower-bound 依赖会自动钉住 SDK。
"""

from __future__ import annotations

import logging

log = logging.getLogger("telemetry_tracing")

# 服务名：出现在 collector 里的 service.name，与 prometheus.yml 的 job 名对齐。
SERVICE_NAME = "robot_backend"

_configured = False


def configure_tracing(endpoint: str, service_name: str = SERVICE_NAME) -> bool:
    """按需启用 OTel trace 导出。**永不抛异常**，返回是否启用成功。

    @param endpoint `OTEL_EXPORTER_OTLP_ENDPOINT`，空串 = 关闭（no-op）。
    @return True = 已接线并开始导出；False = 未配置或降级。

    降级点（每一处都对应一种真实环境）：
      - endpoint 为空                → 内网无 collector，正常关闭，返回 False；
      - import 失败（包没装）        → 返回 False，不影响服务；
      - 初始化期任何异常             → 返回 False 并记 WARNING，不影响服务。
    """
    global _configured

    if not endpoint:
        # 最常见路径：默认部署。这不是错误，用 INFO 且不打印栈。
        log.info("OTel tracing disabled: OTEL_EXPORTER_OTLP_ENDPOINT not set")
        return False

    if _configured:
        # 幂等保护：重复调用会重复注册 span processor，导致每个 span 被导出 N 次。
        log.info("OTel tracing already configured")
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Resource 必须先建：service.name 是 collector 侧区分服务的第一步，
        # 缺失时所有 span 都归到 "unknown_service"，100 台机器人场景下无法下钻。
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)

        # BatchSpanProcessor（异步批量导出）而非 SimpleSpanProcessor（逐 span 同步导出）：
        # 后者会在每个请求内**同步阻塞**等 OTLP 网络往返，1000 msg/s 场景下
        # 直接把事件循环拖死。这是 OTel 官方对生产环境的默认建议。
        #
        # endpoint 直接传 `http(s)://host:port/v1/traces`：OTLPSpanExporter
        # **不会**自动补 `/v1/traces` 路径（实测签名见 PR），必须由调用方给全。
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=_traces_url(endpoint)))
        )
        trace.set_tracer_provider(provider)

        # asyncpg 是全局 instrument：batch_writer 与 /history 的连接都走它。
        AsyncPGInstrumentor().instrument()
        # FastAPI 侧不在这里 instrument —— 需要拿到 app 实例，由 main.py 调用
        # `instrument_fastapi(app)`（见下），避免本模块反向依赖 app 单例。
        _provider = provider  # 持有强引用，防止被 GC 回收导致 span 丢失
        globals()["_provider"] = _provider
        _configured = True
        log.info("OTel tracing enabled: endpoint=%s service=%s", endpoint, service_name)
        return True
    except Exception as exc:  # noqa: BLE001 —— 可观测性绝不能拖垮可用性
        log.warning("OTel tracing disabled (init failed): %s", exc)
        return False


def _traces_url(endpoint: str) -> str:
    """把 base endpoint 规范成 OTLP/HTTP 的 traces 全路径。

    用户可能填 `http://collector:4318` 或 `http://collector:4318/v1/traces`，
    两种都要能用 —— 只补一次，避免拼成 `.../v1/traces/v1/traces`。
    """
    base = endpoint.rstrip("/")
    if base.endswith("/v1/traces"):
        return base
    return f"{base}/v1/traces"


def instrument_fastapi(app) -> bool:
    """给 FastAPI app 挂上 OTel 中间件。未启用时静默跳过。"""
    if not _configured:
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("FastAPI OTel instrumentation failed: %s", exc)
        return False


def shutdown_tracing() -> None:
    """冲刷未导出的 span。进程退出前调用，否则 BatchSpanProcessor 队列里
    最后几秒的 span（恰好是关闭原因最相关的那段时间）会随进程一起消失。"""
    if not _configured:
        return
    try:
        provider = globals().get("_provider")
        if provider is not None:
            provider.shutdown()
    except Exception as exc:  # noqa: BLE001
        log.warning("OTel shutdown failed: %s", exc)
