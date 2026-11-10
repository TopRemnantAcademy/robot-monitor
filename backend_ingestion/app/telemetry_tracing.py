"""OpenTelemetry tracing wiring (order 16 T3): MQTT -> backend -> DB.

Design premise: **off by default, must be degradable** (order 16 SS1 T3 + SS2
acceptance). A factory intranet often has no OTLP collector; if we hard-depend on
the OTel package at import time, or raise when the endpoint is unset, this service
would fail to start in **environments without observability** -- that is
"sacrificing availability for observability", backwards.

So this module's layering is:
  1. All OTel imports live inside function bodies (zero top-level dependency) ->
     not having the package installed does not break `import app.main`;
  2. `configure_tracing()` is the **only** entry point; any exception is digested
     internally and degrades to no-op;
  3. returns bool for "actually enabled", for startup logging and test assertions.

Covered traces (corresponding to order 16's MQTT -> backend -> DB):
  - FastAPI: `FastAPIInstrumentor.instrument_app` -> one span per REST/WS request;
  - asyncpg: `AsyncPGInstrumentor().instrument()` -> one child span per DB
    round-trip. batch_writer's COPY / executemany all go through asyncpg, so the
    write path is automatically visible.

Why not `opentelemetry-instrument` auto-injection (`opentelemetry-distro`):
That wraps the start command with a CLI (`opentelemetry-instrument uvicorn ...`),
which changes the Dockerfile CMD and the local run method; this module does
**explicit in-code wiring**, changing no launch method and not depending on that
CLI existing in the environment. Smallest change surface.

Version alignment (measured pass, see the PR "measured" section): OTel Python's
API/SDK is 1.x, while instrumentation packages are 0.x beta (`0.43b0` matches
SDK `1.22.0`) -- **the two must be installed as a pair**, and the lower-bound
dependency auto-pins the SDK.
"""

from __future__ import annotations

import logging

log = logging.getLogger("telemetry_tracing")

# Service name: appears as service.name in the collector, aligned with
# prometheus.yml's job name.
SERVICE_NAME = "robot_backend"

_configured = False


def configure_tracing(endpoint: str, service_name: str = SERVICE_NAME) -> bool:
    """Enable OTel trace export on demand. **Never raises**, returns whether it
    successfully enabled.

    @param endpoint `OTEL_EXPORTER_OTLP_ENDPOINT`, empty string = off (no-op).
    @return True = wired and exporting; False = unconfigured or degraded.

    Degradation points (each maps to a real environment):
      - endpoint empty                -> intranet has no collector, normal off, False;
      - import failure (package missing) -> False, service unaffected;
      - any exception during init     -> False and WARNING logged, service unaffected.
    """
    global _configured

    if not endpoint:
        # Most common path: default deployment. This is not an error; use INFO and
        # no stack.
        log.info("OTel tracing disabled: OTEL_EXPORTER_OTLP_ENDPOINT not set")
        return False

    if _configured:
        # Idempotency guard: repeated calls would re-register span processors,
        # causing each span to be exported N times.
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

        # Resource must be built first: service.name is the collector's first step
        # to distinguish services; when missing, all spans fall under
        # "unknown_service" and cannot be drilled down in a 100-robot scenario.
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)

        # BatchSpanProcessor (async batch export) rather than SimpleSpanProcessor
        # (per-span sync export): the latter would **synchronously block** waiting
        # for the OTLP network round-trip inside every request, and at 1000 msg/s
        # it would directly kill the event loop. This is OTel's official production
        # default.
        #
        # Pass `http(s)://host:port/v1/traces` as endpoint: OTLPSpanExporter does
        # **not** auto-complete the `/v1/traces` path (measured signature, see PR),
        # the caller must give the full path.
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=_traces_url(endpoint)))
        )
        trace.set_tracer_provider(provider)

        # asyncpg is global instrumentation: batch_writer and /history both use it.
        AsyncPGInstrumentor().instrument()
        # FastAPI is not instrumented here -- we need the app instance, which is
        # done by main.py calling `instrument_fastapi(app)` (see below), so this
        # module does not reverse-depend on the app singleton.
        _provider = provider  # hold a strong reference to prevent GC reclaim losing spans
        globals()["_provider"] = _provider
        _configured = True
        log.info("OTel tracing enabled: endpoint=%s service=%s", endpoint, service_name)
        return True
    except Exception as exc:  # noqa: BLE001 -- observability must never drag down availability
        log.warning("OTel tracing disabled (init failed): %s", exc)
        return False


def _traces_url(endpoint: str) -> str:
    """Normalize the base endpoint into the OTLP/HTTP full traces path.

    The user may fill `http://collector:4318` or
    `http://collector:4318/v1/traces`; both must work -- only append once, to
    avoid `.../v1/traces/v1/traces`.
    """
    base = endpoint.rstrip("/")
    if base.endswith("/v1/traces"):
        return base
    return f"{base}/v1/traces"


def instrument_fastapi(app) -> bool:
    """Attach OTel middleware to the FastAPI app. Silently skips when not enabled."""
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
    """Flush unexported spans. Call before process exit, otherwise the last few
    seconds of spans in the BatchSpanProcessor queue (exactly the most relevant
    window for the shutdown reason) vanish with the process."""
    if not _configured:
        return
    try:
        provider = globals().get("_provider")
        if provider is not None:
            provider.shutdown()
    except Exception as exc:  # noqa: BLE001
        log.warning("OTel shutdown failed: %s", exc)
