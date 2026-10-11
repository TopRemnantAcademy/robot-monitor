"""G-4：告警落库失败**必须可观测**（`robot_alert_store_errors_total`）。

被测对象是 `app/store.py` 的真实异常分支，不是替身逻辑：
用真 `Alert` 喂进真 `save()`，只把连接入口换成会抛异常的假池，
再读**真 registry** 里 Counter 的当前值。

为什么不用 `metrics.store_errors._value.get()` 直接读：
那是私有 API（0.21 与 0.26 的取值路径不一样），测试会变成"钉 py 库内部实现"。
`generate_latest(metrics.registry)` 是公开契约，且顺带验证了「指标真的能被
Prometheus 抓到」—— 只断言内部变量、不断言暴露面的话，registry 挂错
（比如漏传 `registry=`）照样全绿，而线上抓不到任何东西。
（`alert_engine/app/metrics.py` 没有 `render()`：它不像后端那样挂在 FastAPI
路由上，而是在 `_Handler.do_GET` 里直接调 `generate_latest(registry)`。）

反向护栏见本文件底部 `test_guard_detects_a_store_that_forgets_to_count`：
把 store.py 还原成「只 log 不计数」的旧版，上面几条必须变红。
否则这组用例只是在给一个永远不会失败的断言鼓掌。
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import run

from prometheus_client import generate_latest

from app import metrics
from app.rules.base import Alert
from app.store import save

METRIC = "robot_alert_store_errors_total"


def _value(text: str, name: str) -> float:
    """从 Prometheus 文本暴露格式里取出无标签指标的当前值（没有则视为 0）。"""
    m = re.search(rf"^{name} ([0-9.eE+-]+)$", text, re.M)
    return float(m.group(1)) if m else 0.0


def _stored() -> float:
    return _value(generate_latest(metrics.registry).decode(), METRIC)


def _alert() -> Alert:
    return Alert(robot_id="humanoid-001", rule_id="motor_temp", severity=3,
                 message="关节 3 温度 82.1°C 超过 75°C", detail={"joint": 3})


class _BrokenPool:
    """`pool.acquire()` 抛异常，模拟连接池耗尽 / 网络中断。

    刻意用**连接获取阶段**的异常而不是 `conn.execute` 阶段的：
    `save()` 里 acquire 在 try 内部，两条路径都必须被计数。
    """

    def acquire(self):
        raise ConnectionError("injected: pool exhausted")


class _GhostRedis:
    """发布成功但不真的连 Redis（本用例只关心落库这一支）。"""

    def __init__(self, sent: list):
        self._sent = sent

    async def publish(self, chan, payload):
        self._sent.append((chan, payload))


@pytest.fixture()
def broken_store(monkeypatch):
    """把 `app.db` 的两个入口换成假件（conftest 已 stub 掉整个 app.db 模块）。"""
    import app.store as store

    sent: list = []

    async def fake_get_pool():
        return _BrokenPool()

    async def fake_get_redis():
        return _GhostRedis(sent)

    monkeypatch.setattr(store, "get_pool", fake_get_pool)
    monkeypatch.setattr(store, "get_redis", fake_get_redis)
    return store, sent


# ------------------------------------------------- 1. 失败要计数

def test_insert_failure_increments_counter(broken_store):
    """核心断言：一次写库失败 → 计数 +1，且指标名可从 /metrics 文本中看到。"""
    before = _stored()
    run(save(_alert()))
    assert _stored() == before + 1, f"{METRIC} 未随落库失败递增"


def test_counter_is_monotonic_across_failures(broken_store):
    """连续失败必须累加（Counter 而非"有没有失败过"的布尔）。"""
    before = _stored()
    for _ in range(3):
        run(save(_alert()))
    assert _stored() == before + 3


def test_failure_still_publishes_to_frontend(broken_store):
    """落库失败**不该**掐掉前端告警流：两段 try 独立。

    这条钉住的是"不要为了修 G-4 而把发布也一起吞掉"——
    值班在界面上仍须看到告警，哪怕它此刻进不了库。
    """
    _store, sent = broken_store
    run(save(_alert()))
    assert len(sent) == 1
    chan, payload = sent[0]
    assert chan == "robot:alerts"
    assert '"rule_id":"motor_temp"' in payload


# ------------------------------------------------- 2. 成功不该计数

def test_success_does_not_increment_counter(monkeypatch):
    """反向断言：写库成功时计数必须纹丝不动（否则规则天天误报）。"""
    import app.store as store

    class OkConn:
        async def execute(self, *a, **kw):
            return "INSERT 0 1"

        async def fetchval(self, *a, **kw):
            # store.py:42 用 RETURNING alert_id 取回自增主键，成功路径必须支持，
            # 否则 AttributeError 被 except 吞成"落库失败" → 本用例的反向护栏红。
            return 1

    class OkPool:
        def acquire(self):
            return _AsyncCtx(OkConn())

    async def fake_get_pool():
        return OkPool()

    async def fake_get_redis():
        return _GhostRedis([])

    monkeypatch.setattr(store, "get_pool", fake_get_pool)
    monkeypatch.setattr(store, "get_redis", fake_get_redis)

    before = _stored()
    run(save(_alert()))
    assert _stored() == before, "成功路径不应递增落库失败计数"


class _AsyncCtx:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *a):
        return False


# ------------------------------------------------- 3. 规则仍在（防被后续改动误删）

def test_rule_file_references_the_metric_with_increase():
    """规则文件必须用 increase(...[5m]) 而不是 `> 0`（Counter 单调，后者永不复位）。"""
    from pathlib import Path

    rules = (Path(__file__).resolve().parents[2]
             / "infra/observability/rules/robot-alerts.yml").read_text(encoding="utf-8")
    assert f"increase({METRIC}[5m]) > 0" in rules


# ------------------------------------------------- 4. 反向护栏

def test_guard_detects_a_store_that_forgets_to_count(monkeypatch):
    """把 store.py 打回"只 log 不计数"的旧版，计数用例必须变红。

    做法：monkeypatch 掉 `metrics.store_errors.inc`，令其成为空操作 ——
    等价于旧代码里漏埋点时指标永远停在原值。
    若本文件的用例真有约束力，`save()` 失败后数值应当**不变**（即旧行为）。
    这一条同时是元测试：它证明上面那几条断言确实在盯着"有没有 inc()"，
    而不是碰巧被别的东西改动了数值。
    """
    import app.store as store

    async def fake_get_pool():
        return _BrokenPool()

    async def fake_get_redis():
        return _GhostRedis([])

    monkeypatch.setattr(store, "get_pool", fake_get_pool)
    monkeypatch.setattr(store, "get_redis", fake_get_redis)
    monkeypatch.setattr(metrics.store_errors, "inc", lambda *a, **kw: None)

    before = _stored()
    run(save(_alert()))
    assert _stored() == before, "埋点被摘掉后仍计数 —— 说明断言没落在埋点上"
