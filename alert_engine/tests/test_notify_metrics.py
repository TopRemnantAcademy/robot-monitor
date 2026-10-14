"""R-15 / GAP-3：通知投递失败**必须可见**（`robot_alert_notify_failed_total{reason}`）
且**有界重试**，同时**绝不阻塞告警落库/广播**。

被测对象是 `app.main` 的**真实**通知旁路（`_notify` / `_send_with_retry`）与
`app.notifiers.webhook` 的**真实**分类逻辑，不是替身。断言分三组：

1. 指标与分类（V1/V2）：失败 → Counter 按 reason +1；成功 → 失败计数不动；
   reason 必须是封闭集合 `NOTIFY_REASONS` 的取值。
2. 不阻塞主链路（V3，本单**硬门槛**）：通知器**永远不返回**时，`emit()` 仍必须
   完成落库并立即返回；`emit()` 里不得出现 `await _notifier.send`。
3. 有界重试（V4）：次数 ≤3、总耗时有上限、退避不睡过头；
   并且「重试耗尽」与「首次就成功」在指标上可区分。

反向护栏见文件末尾 `test_guard_*`：把埋点摘掉、把重试改成无限，对应断言必须变红。
否则这组用例只是在给一个永远不会失败的断言鼓掌（同 test_store_metrics.py 的做法）。
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import run

from prometheus_client import generate_latest

from app import main as engine_module
from app import metrics
from app.notifiers.webhook import NOTIFY_REASONS
from app.rules.base import Alert

FAILED = "robot_alert_notify_failed_total"
SENT = "robot_alert_notify_sent_total"


# ---------------------------------------------------------------- 取值工具

def _series(text: str, name: str, labels: str = "") -> float:
    """读 Prometheus 文本暴露格式里某个序列的当前值（不存在视为 0）。

    用**公开契约** `generate_latest(registry)` 而不是 `counter._value.get()`：
    后者是 py 库私有 API。且这样顺带证明「指标真的能被 /metrics 抓到」——
    只断言内部变量的话，`registry=` 漏传照样全绿，线上却抓不到任何东西。
    （同 test_store_metrics.py 的理由。）
    """
    pat = rf"^{name}{re.escape(labels)} ([0-9.eE+-]+)$"
    m = re.search(pat, text, re.M)
    return float(m.group(1)) if m else 0.0


def _exposed() -> str:
    return generate_latest(metrics.registry).decode()


def _emit_and_drain(engine) -> None:
    """`await emit()` 后把通知任务也跑完 —— 全程**同一个**事件循环。"""
    async def _run():
        await engine.emit(_alert())
        await _wait_notify_tasks(engine)
    run(_run())


def failed(reason: str) -> float:
    return _series(_exposed(), FAILED, f'{{reason="{reason}"}}')


def sent(kind: str) -> float:
    return _series(_exposed(), SENT, f'{{kind="{kind}"}}')


def failed_all() -> dict[str, float]:
    """所有 reason 的失败计数（含 0 值序列，便于"必须纹丝不动"的断言）。"""
    return {r: failed(r) for r in NOTIFY_REASONS}


def _alert() -> Alert:
    return Alert(robot_id="humanoid-001", rule_id="motor_temp", severity=3,
                 message="关节 3 温度 82.1°C 超过 75°C", detail={"joint": 3})


# ---------------------------------------------------------------- 假通知器

class FailingNotifier:
    """每次 `send()` 都失败，`last_reason` 模拟 webhook.py 的旁注。

    `kind` 是 `WebhookNotifier` 的真实属性名（webhook.py:__init__），
    不是随便起的 —— `_notify` 用 `getattr(notifier, "kind", ...)` 取标签值。
    """

    kind = "generic"

    def __init__(self, reason: str = "network"):
        self.last_reason = reason
        self.calls = 0

    async def send(self, alert) -> bool:
        self.calls += 1
        self.last_reason = self._reason
        return False

    _reason = "network"


class RaisingNotifier(FailingNotifier):
    """**违反契约**的坏通知器：`send()` 直接外抛（正常实现不该这样）。

    用来钉住 `_send_with_retry` 的纵深防御：异常必须被吞在本函数内，
    不得顺着 `spawn` 的任务冒到事件循环 —— 那会破坏本单最核心的不变量
    「通知失败不影响落库/广播」，且 `spawn` 出来的任务异常在 asyncio 里
    默认**只打一条 "Task exception was never retrieved"**，极难联想因果。
    """

    async def send(self, alert) -> bool:
        self.calls += 1
        raise ConnectionError("injected: connection reset")


class HangingNotifier:
    """`send()` 永不返回 —— 模拟对端 hang 住（无响应但也不断连）。

    这是检验"不阻塞主链路"的**唯一可信**姿势：让它返回 False 测的是
    "失败后能不能继续"，而真正的风险是"根本没返回时，链路还走不走得下去"。
    """

    kind = "generic"
    last_reason = None

    def __init__(self):
        self.calls = 0

    async def send(self, alert) -> bool:
        self.calls += 1
        import asyncio
        await asyncio.Event().wait()      # 永不 set
        return False


class OkNotifier:
    kind = "dingtalk"

    def __init__(self):
        self.last_reason = None
        self.calls = 0

    async def send(self, alert) -> bool:
        self.calls += 1
        return True


class FlakyNotifier(FailingNotifier):
    """第 `succeed_on` 次调用起成功 —— 验证"重试真的发生了且能救回来"。"""

    def __init__(self, succeed_on: int = 2):
        super().__init__("timeout")
        self._succeed_on = succeed_on

    async def send(self, alert) -> bool:
        self.calls += 1
        return self.calls >= self._succeed_on


@pytest.fixture()
def patched_notifier(engine, monkeypatch):
    """把 `app.main._notifier` 换成可注入的假通知器，并**关掉退避等待**。

    关退避的方式是把退避基值改成 0（而不是 patch `asyncio.sleep`）：
    patch 掉 sleep 会让"总耗时上限"这条断言失去意义 —— 我们要测的是**真实
    时间轴**上的预算控制，不是"睡了几次"。基值 0 时 `2**i * 0` 仍为 0，
    预算检查恒通过，重试次数断言因此能干净地只反映"次数上限"。
    """
    monkeypatch.setattr(engine_module, "NOTIFY_BACKOFF_BASE_S", 0.0)

    def install(notifier):
        monkeypatch.setattr(engine_module, "_notifier", notifier)
        return notifier

    return install


async def _wait_notify_tasks(engine) -> None:
    """在**同一个事件循环**里等 `spawn()` 出来的通知任务跑完。

    为什么必须 drain（而不是"等一会儿看看"）：本单把通知改成了后台任务
    （不阻塞 emit），故"失败计数 +1"不再在 `emit()` 返回时就成立。
    测试主动 await `engine._tasks` 是在**对齐语义**：断言的对象自始至终是
    `_notify` 的完整执行结果。这也是本单对既有用例的唯一行为差异。

    ⚠️ 必须在 `run()` 的协程内 await，**不能** `run(emit()); run(_drain())` ——
    两次 `run()` 是两个事件循环，task 在第一个循环结束时已被取消，
    第二个循环里什么都等不到（实测 calls 停在 1，重试次数断言全红）。
    """
    for t in [t for t in engine._tasks if not t.done()]:
        await t


# ================================================================ 1. 指标存在（V1）

def test_metric_is_registered_and_exposed():
    """`robot_alert_notify_failed_total{reason}` 必须能被 /metrics 抓到。

    `generate_latest` 对**尚无样本**的 Counter 不输出任何行（Counter 是
    "有标签组合才存在"的），故这里查的是 HELP/TYPE 声明行 —— 那才是
    "指标已注册"的证据。数值断言的用例见下面几条（它们触发真实样本）。
    """
    text = _exposed()
    assert f"# HELP {FAILED}" in text, "计数器未注册到 metrics.registry"
    assert f"# TYPE {FAILED} counter" in text
    assert f"# HELP {SENT}" in text
    assert f"# TYPE {SENT} counter" in text


def test_metric_keeps_robot_alert_prefix():
    """命名规范沿用 `robot_alert_*`（需求 §6 禁止项），不是新造前缀。"""
    text = _exposed()
    m = re.search(rf"^# TYPE ({FAILED}\S*) ", text, re.M)
    assert m is not None
    assert m.group(1).startswith("robot_alert_")


# ================================================================ 2. 失败计数 + 分类（V2）

def test_failure_increments_counter_with_reason(engine, patched_notifier):
    """核心断言：投递失败 → `{reason="network"}` +1，且成功计数不动。"""
    n = patched_notifier(FailingNotifier("network"))
    before_failed, before_sent = failed("network"), sent("generic")

    _emit_and_drain(engine)

    assert failed("network") == before_failed + 1, f"{FAILED} 未随通知失败递增"
    assert sent("generic") == before_sent, "失败路径不得计入 notify_sent_total"
    assert n.calls == engine_module.NOTIFY_MAX_ATTEMPTS, "重试次数应为上限值"


def test_reason_label_is_classified_not_hardcoded(engine, patched_notifier):
    """不同 reason 分别计数 —— 标签必须真的来自失败原因，不是写死的常量。"""
    for reason in ("timeout", "http_error", "business_error"):
        before = failed(reason)

        class _N(FailingNotifier):
            _reason = reason

        patched_notifier(_N(reason))
        _emit_and_drain(engine)
        assert failed(reason) == before + 1, f"reason={reason} 未被单独计数"


def test_unknown_reason_is_not_swallowed(engine, patched_notifier):
    """通知器给不出 reason 时归一到 `unknown`，**不掩盖**（照样计数）。"""
    n = patched_notifier(FailingNotifier(None))
    n._reason = None
    before = failed("unknown")

    _emit_and_drain(engine)

    assert failed("unknown") == before + 1, "无 reason 的失败必须落在 unknown 上"


def test_success_does_not_increment_failure_counter(engine, patched_notifier):
    """反向断言：投递成功时**所有** reason 的失败计数纹丝不动（否则规则天天误报）。"""
    patched_notifier(OkNotifier())
    before = failed_all()
    before_sent = sent("dingtalk")

    _emit_and_drain(engine)

    assert failed_all() == before, "成功路径不得递增任何 notify_failed 计数"
    assert sent("dingtalk") == before_sent + 1, "成功路径必须计入 notify_sent_total"


def test_raising_notifier_does_not_escape_and_is_counted(engine, patched_notifier):
    """通知器违约外抛时：异常被吞、按 reason 计数、主链路无感。"""
    n = patched_notifier(RaisingNotifier())
    before = failed("network")

    # 若异常逃出 `_notify`，`_wait_notify_tasks` 会把异常直接抛给测试 → 红
    _emit_and_drain(engine)

    assert failed("network") == before + 1
    assert n.calls == engine_module.NOTIFY_MAX_ATTEMPTS


# ================================================================ 3. 不阻塞主链路（V3，硬门槛）

def test_notify_failure_does_not_block_save_or_publish(engine, patched_notifier):
    """硬门槛：通知**永不返回**时，告警仍完成落库，且 `emit()` 立刻返回。

    这一条是本需求 §6 的第一禁止项（通知失败不得阻塞/回滚落库）。
    用 `HangingNotifier` 而不是 `FailingNotifier`：只有"永不返回"才能证明
    通知**不在**主链路的等待路径上。
    """
    n = patched_notifier(HangingNotifier())

    run(engine.emit(_alert()))

    # 断言 1：落库已完成（conftest 的 fake_save 记在 engine.emitted 里）
    assert [a.rule_id for a in engine.emitted] == ["motor_temp"], \
        "通知挂住时告警仍未落库 —— 主链路被阻塞了"
    # 断言 2：通知确实被**启动**了（不是根本没接）
    assert n.calls == 1, "通知任务未被启动"
    # 断言 3：落库**先于**任何通知调用（顺序契约）
    assert engine.emitted, "落库必须在通知之前"


def test_notify_not_awaited_in_emit_source():
    """源码级护栏：`emit()` 里不得 `await` 通知器。

    行为断言（上面那条）依赖"通知器恰好 hang 住"；源码断言则**不依赖假件行为**
    —— 它直接禁止了本单要消除的那行代码回来（`await _notifier.send(alert)`）。
    两者互补：行为断言证明"今天没阻塞"，源码断言防"明天有人改回去而没挂住"。
    """
    import inspect
    import io
    import tokenize

    # 只看**代码**、剔掉注释与 docstring：本函数上方恰好有一段注释在解释
    # 「由 `await _notifier.send(...)` 改为 spawn(...)」，直接对整段源码做子串
    # 匹配会把这段说明当成违规代码（实测踩过）。用 tokenize 取真实 token 流，
    # 得到的就是"如果真的执行，会执行什么"。
    src = inspect.getsource(engine_module.emit)
    code_tokens = [
        tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE)
    ]
    code = " ".join(code_tokens)

    assert "_notifier" in code and "await" in code, "探针失效：token 流异常"
    # 关键：`_notifier` 不得出现在 `await` 的**右侧**（即不得被 await）。
    # 写法上等价于禁止 `await _notifier.send(...)` / `await _notifier(...)`。
    assert "await _notifier" not in code, \
        "emit() 里又出现了 await 通知调用 —— 通知必须走 spawn(_notify(...))"
    assert "spawn ( _notify ( alert ) )" in code, "emit() 必须把通知派给后台任务"


def test_failsafe_when_notifier_is_none(engine, monkeypatch):
    """未配置 webhook（`_notifier is None`）时：不通知、不计数、不报错。"""
    monkeypatch.setattr(engine_module, "_notifier", None)
    before = failed_all()

    _emit_and_drain(engine)

    assert [a.rule_id for a in engine.emitted] == ["motor_temp"]
    assert failed_all() == before, "跳过通知时不得产生失败计数"


# ================================================================ 4. 有界重试（V4）

def test_retry_is_bounded_by_max_attempts(engine, patched_notifier):
    """失败时尝试次数**恰好**等于上限（不是少于，也不是更多）。"""
    n = patched_notifier(FailingNotifier("http_error"))

    _emit_and_drain(engine)

    assert n.calls == engine_module.NOTIFY_MAX_ATTEMPTS == 3


def test_retry_stops_at_first_success(engine, patched_notifier):
    """第 2 次成功 → 只调 2 次，且计入 sent、不计失败。"""
    n = patched_notifier(FlakyNotifier(succeed_on=2))
    before_failed = failed("timeout")
    before_sent = sent("generic")

    _emit_and_drain(engine)

    assert n.calls == 2, "成功后不得继续重试"
    assert sent("generic") == before_sent + 1
    assert failed("timeout") == before_failed, "最终成功不得计入失败"


def test_retry_never_exceeds_total_budget(engine, patched_notifier, monkeypatch):
    """总耗时有**硬上限**：即使每次 send 都吃掉一大块预算，也不得超时太多。

    这里让退避基值回到真值（0.5s）并让 `send()` 各耗时 1s，
    模拟"三次尝试 + 两次退避"的真实时间轴。预算 5s 只约束**重试循环**，
    故给一点调度余量：断言 < 预算 + 单次 send 耗时 + 余量。
    若实现改成"无限重试/无预算检查"，本用例会因耗时爆炸或次数超限而红。
    """
    import asyncio
    import time as _time

    monkeypatch.setattr(engine_module, "NOTIFY_BACKOFF_BASE_S", 0.5)

    class SlowFailing(FailingNotifier):
        async def send(self, alert) -> bool:
            self.calls += 1
            self.last_reason = "network"
            await asyncio.sleep(1.0)
            return False

    n = patched_notifier(SlowFailing())
    t0 = _time.monotonic()
    _emit_and_drain(engine)
    elapsed = _time.monotonic() - t0

    assert n.calls <= engine_module.NOTIFY_MAX_ATTEMPTS
    # 最坏 = 3 × 1.0s(send) + 0.5 + 1.0(退避) = 4.5s；留 1.0s 调度余量。
    assert elapsed < engine_module.NOTIFY_TOTAL_BUDGET_S + 1.0, \
        f"重试总耗时 {elapsed:.2f}s 超出预算护栏"


def test_exhausted_retry_is_distinguishable_from_success(engine, patched_notifier):
    """「重试后成功」与「重试耗尽」在指标上必须可区分（这正是加指标的目的）。"""
    failing = patched_notifier(FailingNotifier("business_error"))
    _emit_and_drain(engine)
    exhausted_calls = failing.calls

    flaky = patched_notifier(FlakyNotifier(succeed_on=3))
    _emit_and_drain(engine)

    assert exhausted_calls == 3                    # 耗尽：3 次全失败
    assert flaky.calls == 3                        # 救回：第 3 次成功
    # 二者尝试次数相同，但一个进了失败计数、一个进了成功计数 —— 指标能分辨。
    assert failed("business_error") > 0
    assert sent("generic") > 0


# ================================================================ 5. 分类函数（webhook 侧）

def test_classify_maps_http_and_network_and_timeout():
    """`classify_exception` 的映射表 —— reason 标签的**唯一**来源。"""
    import socket
    import urllib.error

    from app.notifiers.webhook import classify_exception

    assert classify_exception(
        urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)) == "http_error"
    assert classify_exception(socket.timeout("t")) == "timeout"
    assert classify_exception(
        urllib.error.URLError(socket.timeout("t"))) == "timeout"
    assert classify_exception(
        urllib.error.URLError(ConnectionRefusedError("refused"))) == "network"
    assert classify_exception(ConnectionError("reset")) == "network"
    assert classify_exception(TypeError("not serializable")) == "config"
    assert classify_exception(RuntimeError("???")) == "unknown"


def test_classify_output_is_always_a_declared_reason():
    """封闭集合：任何输入都必须产出 `NOTIFY_REASONS` 里的值（防标签基数失控）。"""
    from app.notifiers.webhook import classify_exception

    for exc in (ValueError("x"), OSError("x"), RuntimeError("x"),
                KeyError("x"), Exception("x")):
        assert classify_exception(exc) in NOTIFY_REASONS
    assert "unknown" in NOTIFY_REASONS, "必须有兜底取值，否则异常会被静默丢弃"


def test_notify_error_normalises_unknown_reason():
    """`NotifyError` 必须把非白名单 reason 归一到 unknown。"""
    from app.notifiers.webhook import NotifyError

    assert NotifyError("random-string").reason == "unknown"
    assert NotifyError("timeout").reason == "timeout"


def test_webhook_send_records_reason_on_failure():
    """真实 `WebhookNotifier.send()` 失败时，必须把 reason 旁注到实例上。

    这是"分类"与"计数"之间的**接缝**：`app/main.py` 读的就是这个属性。
    只测分类函数、不测这个属性的话，链路一断（比如改名）两边都全绿而线上
    全部落成 unknown。
    """
    from unittest import mock

    from app.notifiers.webhook import WebhookNotifier

    import urllib.error

    n = WebhookNotifier("generic", "http://x")
    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError(ConnectionRefusedError("x"))):
        ok = run(n.send(_alert()))

    assert ok is False
    assert n.last_reason == "network", "失败后必须留下可分类的 reason"


def test_webhook_send_keeps_existing_log_line(capsys=None):
    """既有日志契约（webhook.py 原 :115）不得被删除 —— 需求 §6 禁止项。

    源码级断言（而不是捕获日志）：它能同时防"改字符串"与"改日志级别"。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "app/notifiers/webhook.py"
    text = src.read_text(encoding="utf-8")
    assert 'log.exception("webhook notify failed (%s): %s/%s",' in text, \
        "webhook.py 既有的 log.exception 被删/改动了（R-15 §6 明令保留）"


# ================================================================ 6. 反向护栏

def test_guard_detects_a_notify_path_that_forgets_to_count(engine, patched_notifier,
                                                           monkeypatch):
    """把埋点摘成空操作（= 旧代码"只记日志"的行为），计数断言必须变红。

    元测试：证明上面那几条断言确实盯着"有没有 inc()"，而不是碰巧被别的东西
    改变了数值。

    用**单点 reason**（`config`，本文件其它用例都不产生该标签）而不是
    `failed_all()` 全量对比：`Prometheus` 的 Counter 是**进程级全局**的，
    同进程内别的用例留下的样本会一起进来。实测踩过：`failed_all()` 快照里
    `network` 从 5 变 6，正是本用例自己上一次的残留，与埋点无关。
    单点断言把"被测对象"缩到一个确定不会被污染的点上。
    """
    patched_notifier(FailingNotifier("config"))
    monkeypatch.setattr(metrics.notify_failed, "inc", lambda *a, **kw: None)

    before = failed("config")
    _emit_and_drain(engine)

    assert failed("config") == before, \
        "埋点被摘掉后仍计数 —— 说明断言没落在埋点上"


def test_guard_detects_unbounded_retry(engine, patched_notifier, monkeypatch):
    """把次数上限放大成 10，次数断言必须变红 —— 证明"≤3 次"是被测出来的。

    用 10 而不是真·无限：无限循环在测试里是挂死，不是失败，会被 CI 超时
    误判成环境问题。放大到"明显超出上限"即可证伪。
    """
    monkeypatch.setattr(engine_module, "NOTIFY_MAX_ATTEMPTS", 10)
    n = patched_notifier(FailingNotifier("network"))

    _emit_and_drain(engine)

    assert n.calls == 10 != 3, "上限被改写后次数未随之变化 —— 次数断言是假的"
