# 订单 12 复核报告：用真实上游源码验证三条结论

> 复核对象：`external/INTEGRATION-PLAN.md` §6「验证边界」中标为 ✅ 实测的三条。
> 复核方式：`git clone --depth 1` 真源码 + `git show <tag>:<file>` 取指定版本原文
> + 隔离 `pip --target` 装包做 `inspect.getsource` 运行时取证。
> 环境：Python 3.13.x｜网络：github.com 可达（本容器实测）。

## 0. 先纠正任务前提（两处）

| 用户所述 | 实际 |
|---|---|
| `orders/12-dispatch.md` 存在 | **不存在**。仓库 `orders/` 只到 `10-`，缺 11/12 |
| `scripts/fetch-external-refs.sh` 存在 | **不存在**，连 `scripts/` 目录都没有 |

本报告已**补齐** `scripts/fetch-external-refs.sh`（可执行、可复跑），并把结论落到本文件。

---

## 1. 上游源拉取结果（14/14 全部成功）

```
OK    mqtt-to-timescaledb      OK    aiomqtt            OK    asyncpg
OK    tsbs                     OK    zustand            OK    echarts-for-react
OK    emqtt-bench              OK    redis-py           OK    starlette
OK    full-stack-fastapi-template   OK    client_python
OK    alembic                  OK    paho.mqtt.python   OK    alertmanager (sparse: docs/)
----- 成功/已存在: 13  失败: 0（alertmanager 单列 sparse，计 14）
```

网络可通 github.com，**不存在**「拉不到」的问题。

---

## 2. 结论①：aiomqtt 2.5.0 的 `__aexit__` 修复 —— ❌ **原表述方向反了**

### 2.1 原文（用户/计划所述）

> I-1：我们的重连**完全依赖** `async with` 退出时抛 `MqttError` 来触发退避重连。
> 在 2.0.1 下 `__aexit__` 会**吞掉异常** → 重连分支可能根本不执行。

即「2.0.1 吞异常，2.5.0 改为抛出」。

### 2.2 源码实证（结论：恰好相反）

`external/aiomqtt`，`git diff v2.0.1 v2.5.0 -- aiomqtt/client.py`：

```diff
     async def __aexit__(self, exc_type, exc, tb) -> None:
         """Disconnect from the broker."""
         if self._disconnected.done():
             # Return early if the client is already disconnected
             if self._lock.locked():
                 self._lock.release()
-            if (exc := self._disconnected.exception()) is not None:
-                # If the disconnect wasn't intentional, raise the error that caused it
-                raise exc
+            # let any error on disconnection exception bubble up without suppressing it
             return
```

- **v2.0.1**（`aiomqtt/client.py:727-729`）：`__aexit__` **会** re-raise
  断开原因（`raise exc`）。
- **v2.5.0**（`aiomqtt/client.py:803-807`）：**删掉了 `raise exc`**，改为直接 `return`。

CHANGELOG（`CHANGELOG.md:48`，`## [2.5.0] - 2026-01-04`）原文：

```
- Let exceptions bubble up in `Client.__aexit__` instead of suppressing them (@yujia21 in #375)
```

### 2.3 运行时取证（不是只读源码）

隔离安装三个版本，用 `inspect.getsource(Client.__aexit__)`：

| 版本 | 安装方式 | `raise exc` 出现 | `bubble up` 注释出现 |
|---|---|---|---|
| 2.0.1 | `pip --target`（aliyun 镜像） | **True** | False |
| 2.5.0 | `pip --target` | **False** | True |
| 2.5.1 | `pip --target` | **False** | True |

复现命令：

```bash
pip install -q -i https://mirrors.aliyun.com/pypi/simple --target /tmp/t250 "aiomqtt==2.5.0"
PYTHONPATH=/tmp/t250 python3 -c "import inspect,aiomqtt;s=inspect.getsource(aiomqtt.Client.__aexit__);print('raise exc' in s)"
# -> False
```

### 2.4 那这条修复到底修了什么？（正确解读）

`__aexit__` 的 `exc` 参数是 **`with` 块内抛出的异常**，不是断开原因。
v2.0.1 的代码把「断开 future 上的异常」重新抛出 ——
而该异常**已在别处被 `MessagesIterator.__anext__` 用 `raise MqttError(...) from ...` 抛出**
（`client.py` 中 `msg = "Disconnected during message iteration"`）。
两条路径同时抛，会在 `async for` 正常收到 `MqttError` 后，
`async with` 退出时**再抛一次**（重复/掩盖真实栈）。
2.5.0 的改动是**消除这层重复抛出**，让异常沿消息迭代路径冒泡 —— 所以 CHANGELOG 用
"bubble up ... instead of suppressing"，但**删除的是 `__aexit__` 里那次多余的 re-raise**。

### 2.5 对 I-1 论断的实际影响

- **升级方向仍然正确**：2.5.0 确实改了 `__aexit__`，`aiomqtt>=2.5.0` 的诉求成立。
- **但「2.0.1 吞异常导致不重连」的因果是错的**：2.0.1 的 `__aexit__` 不吞异常，反而多抛。
  重连靠的是 `MessagesIterator` 抛 `MqttError`，**在 2.0.1 下重连分支也会被触发**。
- 因此 `backend_ingestion/app/services/mqtt_subscriber.py:49-51` 的注释
  （"2.0.1 的 __aexit__ 在重连场景下存在不抛异常的分支，会绕过本 except"）
  **与源码不符，建议改为准确表述**。

---

## 3. 结论②：`Client` reusable 表述 —— ✅ **成立**

`external/aiomqtt`，`git show v2.5.1:docs/reconnection.md` 逐字原文：

> The `Client` context is designed to be [reusable (but not reentrant)](https://docs.python.org/3/library/contextlib.html#reusable-context-managers).
>
> ```python
> client = aiomqtt.Client("test.mosquitto.org")
> interval = 5  # Seconds
> while True:
>     try:
>         async with client:
>             await client.subscribe("humidity/#")
>             async for message in client.messages:
>                 print(message.payload)
>     except aiomqtt.MqttError:
>         print(f"Connection lost; Reconnecting in {interval} seconds ...")
>         await asyncio.sleep(interval)
> ```

源码侧佐证：`__aenter__` 用 `self._lock` 挡重入（`aiomqtt/client.py:769-771` 抛
`MqttReentrantError("The client context manager is reusable, but not reentrant")`）。

本仓库已按此实现：`backend_ingestion/app/services/mqtt_subscriber.py:35-39`
（循环外构造、循环内 `async with client`）—— **一致，无需再改**。
保留指数退避（1→30s）而不退回官方固定 5s，是对 1000 msg/s 场景的合理偏离。

---

## 4. 结论③：Alertmanager inhibition 语义 —— ⚠️ **表述基本成立，但有一处需澄清**

`external/alertmanager`，`git show` 取 `docs/configuration.md` §Inhibition（:505-529）逐字：

> An inhibition rule mutes an alert (target) matching a set of matchers
> when an alert (source) exists that matches another set of matchers.
> Both target and source alerts must have the same label values
> for the label names in the `equal` list.
>
> To prevent an alert from inhibiting itself, an alert that matches _both_ the
> target and the source side of a rule cannot be inhibited by alerts for which
> the same is true (including itself).

**语义正确的部分**：抑制 = 某组「源」告警存在时，静音另一组「目标」告警；
`equal` 列表要求源/目标在这些标签上取值相同（天然适配我们按 `robot_id` 关联）。

**需要澄清的部分**：计划里写「高 severity 抑制低 severity」——
Alertmanager 的 `inhibit_rule` **没有 severity 概念**，它纯粹按 **matcher（标签）** 匹配。
`severity` 在官方文档中只作为**示例标签**出现（如 `configuration.md:738` 的
`{alertname="Watchdog", severity=~"warning|critical"}`），并非内置层级。
所谓「高 severity 抑制低 severity」是**用 matcher 人为表达**出来的，例如：

```yaml
inhibit_rules:
  - source_matchers: [severity = "critical"]   # 源：严重
    target_matchers: [severity = "warning"]    # 目标：警告
    equal: [robot_id]                          # 同机才抑制
```

**对本仓库的结论不变**：`alert_engine/app/deduplicator.py` 确无抑制机制，
`offline`(severity 3) 与 `battery_low`(severity 2) 仍可能同机刷两条 ——
「建议后续补 inhibition」成立。但若落地，实现应对齐
「matcher + equal」模型，而非硬编码 severity 比较。

---

## 5. 汇总表

| # | 结论 | 判定 | 依据 |
|---|---|---|---|
| ① | aiomqtt 2.5.0 修了 `__aexit__` | ✅ 存在该修复，但 ❌ **原因果方向说反了** | `git diff v2.0.1 v2.5.0`；`CHANGELOG.md:48`；`inspect.getsource` 三版本 |
| ① | 「2.0.1 吞异常导致不重连」 | ❌ **不成立**（2.0.1 反而是多抛） | `client.py:727-729` vs `:807` |
| ② | `Client` reusable（not reentrant） | ✅ 成立 | `v2.5.1:docs/reconnection.md`；`client.py:769-771` |
| ③ | Alertmanager 有 inhibition、我们缺 | ✅ 成立 | `docs/configuration.md:505-529` |
| ③ | 「高 severity 抑制低 severity」 | ⚠️ 概念需澄清（无内置 severity） | 官方文档无 severity 内置语义，仅示例标签 |

## 6. 未验证 / 剩余边界

- ①的**运行时端到端行为**（真实断连后是否按预期重连）需真实 broker，未做；
  本报告只证到「源码与运行时 `__aexit__` 实现的版本差异」，未证「重连链路在真机跑通」。
- ③的抑制规则**未落地**，仅为设计对照。
