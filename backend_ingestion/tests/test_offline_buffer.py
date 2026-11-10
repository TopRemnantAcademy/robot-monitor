"""Offline buffer unit tests (Benchmark C, store-and-forward).

Verifies push / replay / length with a duck-typed fake Redis client (no real
Redis). Covers lossless buffering -> replay, idempotent drop of unparseable
records, remainder restoration on writer failure, and safe fallback to
`rejected` when Redis is unavailable.

Sync wrappers call asyncio.run so the suite needs no pytest-asyncio plugin.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db import redis as redis_mod
from app.schemas.telemetry import Telemetry
from app.services.batch_writer import BatchWriter
from app.services.offline_buffer import DurableBuffer


class FakeRedis:
    """Minimal duck-typed Redis list implementation (decode_responses=True)."""

    def __init__(self) -> None:
        self._data: list[str] = []

    async def rpush(self, key: str, value: str) -> None:
        self._data.append(value)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        if end == -1:
            return list(self._data[start:])
        return list(self._data[start : end + 1])

    async def llen(self, key: str) -> int:
        return len(self._data)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        if start == -len(self._data) and end == -1:
            return  # keep all
        self._data = self._data[start : end + 1] if end >= 0 else self._data[start:]

    async def delete(self, key: str) -> None:
        self._data.clear()


def _telemetry(robot_id="humanoid-001", battery=80.0) -> Telemetry:
    temps = {f"j{i}": 50.0 for i in range(3)}
    return Telemetry(
        robot_id=robot_id,
        status_code=1,
        error_code=0,
        battery_pct=battery,
        battery_temp=30.0,
        max_joint_temp=50.0,
        joint_temperatures=temps,
        joint_positions=temps,
        joint_torques=temps,
    )


class _Writer:
    def __init__(self) -> None:
        self.puts: list[Telemetry] = []

    async def put(self, t: Telemetry) -> None:
        self.puts.append(t)


def test_push_and_length():
    async def _run():
        buf = DurableBuffer(FakeRedis(), "k", maxlen=1000)
        assert await buf.length() == 0
        await buf.push(_telemetry().model_dump_json())
        assert await buf.length() == 1
    asyncio.run(_run())


def test_replay_requeues_valid_records():
    async def _run():
        fake = FakeRedis()
        buf = DurableBuffer(fake, "k", maxlen=1000)
        for _ in range(3):
            await buf.push(_telemetry().model_dump_json())
        writer = _Writer()
        n = await buf.replay(writer)
        assert n == 3
        assert len(writer.puts) == 3
        # key deleted on successful completion
        assert await buf.length() == 0
    asyncio.run(_run())


def test_replay_drops_unparseable_but_keeps_valid():
    async def _run():
        fake = FakeRedis()
        buf = DurableBuffer(fake, "k", maxlen=1000)
        await buf.push(_telemetry().model_dump_json())
        await buf.push("not-valid-json{")  # validation fails -> dropped
        await buf.push(_telemetry(robot_id="humanoid-002").model_dump_json())
        writer = _Writer()
        n = await buf.replay(writer)
        assert n == 2  # 1 bad dropped, 2 good replayed
        assert await buf.length() == 0
    asyncio.run(_run())


def test_replay_restores_remainder_on_writer_failure():
    async def _run():
        fake = FakeRedis()
        buf = DurableBuffer(fake, "k", maxlen=1000)
        for _ in range(3):
            await buf.push(_telemetry().model_dump_json())

        class _FailingWriter:
            async def put(self, t: Telemetry) -> None:
                raise RuntimeError("db down again")

        n = await buf.replay(_FailingWriter())
        assert n == 0
        # unprocessed records restored to buffer -> no data loss
        assert await buf.length() == 3
    asyncio.run(_run())


def test_buffer_on_failure_buffers_instead_of_rejecting(monkeypatch):
    async def _run():
        fake = FakeRedis()

        async def _fake_get_redis():
            return fake

        monkeypatch.setattr(redis_mod, "get_redis", _fake_get_redis)
        writer = BatchWriter()
        buf = [_telemetry(), _telemetry(robot_id="humanoid-002")]
        await writer._buffer_on_failure(buf)
        assert writer.buffered == 2
        assert writer.rejected == 0
        assert await fake.llen("robot:buffer:telemetry") == 2
    asyncio.run(_run())


def test_buffer_on_failure_falls_back_to_rejected_when_redis_unavailable(monkeypatch):
    async def _run():
        async def _boom():
            raise RuntimeError("redis down")

        monkeypatch.setattr(redis_mod, "get_redis", _boom)
        writer = BatchWriter()
        buf = [_telemetry()]
        await writer._buffer_on_failure(buf)
        # Redis unavailable -> safe fallback: counted as rejected, no service halt
        assert writer.rejected == 1
        assert writer.buffered == 0
    asyncio.run(_run())
