# 派工单：订单 07 — 套用上游开源源的三项修正（I-1 / I-2 / I-3）

> 派单：`cnb issues create-issue --repo flow.237/RobotMonitor --body-file orders/07-dispatch.md`
> 触发：`cnb issues post-issue-comment --repo flow.237/RobotMonitor --number 8 --work-mode --body-file orders/07-dispatch.md`

---

@codebuddy 请接单实现**订单 07**。本单来自对上游开源源的对照审计，
不是新功能，是**把官方明确要求的用法套用到我们已经写好的代码上**。

**完整依据见 `external/INTEGRATION-PLAN.md`**（已入库，必须先读）。
本单只做其中标记为 🔴 和 🟡 的三项：I-1、I-2、I-3。

## 0. 背景：为什么要改

主代理下载了 `empicano/aiomqtt`（aiomqtt 官方仓库，注意已从 `sbtinstruments/aiomqtt` 迁移），
对照官方 `docs/reconnection.md` 与 `docs/alongside-fastapi-and-co.md`（tag `v2.5.1`）后，
发现我们的 MQTT 订阅实现有三处与官方契约不符。其中 I-1 是**功能性隐患**。

## 1. I-1 🔴 升级 `aiomqtt` 2.0.1 → 2.5.1

**文件**：`backend_ingestion/requirements.txt`（当前 `aiomqtt==2.0.1`）

**为什么必须升**（不是追新，是修 bug —— 官方 CHANGELOG 实证）：

```
## [2.5.0] - 2026-01-04
### Fixed
- Let exceptions bubble up in `Client.__aexit__` instead of suppressing them (@yujia21 in #375)
```

我们的重连**完全依赖** `async with` 退出时抛出 `MqttError` 来触发指数退避重连
（`backend_ingestion/app/services/mqtt_subscriber.py:27-38`）。
在 2.0.1 下 `__aexit__` 会**吞掉异常** → 重连分支可能根本不执行
→ MQTT 断开后静默不重连 → 看板永远停在最后一帧，且不报错。

**附带收益**（同 CHANGELOG）：
- 2.3.2 起测试 Python 3.13
- 2.3.1 起改用 `get_running_loop`，替代已废弃的 `get_event_loop`

**风险**：低。2.x 线内 minor 升级。主代理已实证 `Client.__init__(hostname: str, port: int = 1883, ...)`
签名在 v2.5.1 与我们的调用一致（`backend_ingestion/app/services/mqtt_subscriber.py:31-32`）。

**交付**：`requirements.txt` 改为 `aiomqtt==2.5.1`，并同步更新 `backend_ingestion/README.md`
里若出现版本说明的地方。

## 2. I-2 🔴 复用 `aiomqtt.Client` 实例

**文件**：`backend_ingestion/app/services/mqtt_subscriber.py:29-38`

**官方原文**（`external/aiomqtt/docs/reconnection.md` @ v2.5.1）：

> The `Client` context is designed to be **reusable (but not reentrant)**.
>
> ```python
> client = aiomqtt.Client("test.mosquitto.org")   # 循环外创建
> while True:
>     try:
>         async with client:                       # 循环内复用
>             await client.subscribe("humidity/#")
>             async for message in client.messages:
>                 print(message.payload)
>     except aiomqtt.MqttError:
>         await asyncio.sleep(interval)
> ```

**我们当前写法**（每轮新建实例）：

```python
while True:
    try:
        async with aiomqtt.Client(hostname=settings.mqtt_broker,
                                  port=settings.mqtt_port) as client:   # ← 每轮 new
```

**差异影响**：每轮新建 → 每轮生成新的 `identifier` → broker 侧视为全新会话，
无法保留会话状态。功能上"能跑"，但违背官方设计。

**要求**：
1. 把 `Client(...)` 构造移到 `while True` **之前**
2. 循环内改为 `async with client:`

**保留我们更好的部分（不要改回去）**：
官方示例用固定 `interval = 5`。我们用的是**指数退避 1s → 30s 封顶**
（`mqtt_subscriber.py:29,37`）。在 100 台 × 10Hz = 1000 msg/s 场景下，
退避比固定 5s 更合理（避免 broker 抖动时被高频重连打垮）。**保留退避**。

## 3. I-3 🟡 修正 `lifespan` 关闭顺序

**文件**：`backend_ingestion/app/main.py:36-52`

**当前顺序**（有问题）：

```python
for t in (mqtt_task, writer_task):
    t.cancel()                          # ① mqtt 和 writer 一起 cancel
await writer.graceful_shutdown()        # ② 之后才冲刷 writer 队列
await asyncio.gather(mqtt_task, writer_task, return_exceptions=True)   # ③
```

**问题**：`writer_task` 在 ① 就已被 cancel，② 才去冲刷它的队列 —— 存在竞态。
进程退出时可能**丢失最后一批遥测**，而那正是订单 06 要验收的 30,000 行附近的数据。

**官方模式**（`external/aiomqtt/docs/alongside-fastapi-and-co.md` @ v2.5.1）：

```python
task.cancel()
try:
    await task
except asyncio.CancelledError:
    pass
```

**要求改成这个顺序**：

1. 先 `mqtt_task.cancel()` —— 停止接收新消息（入口掐断）
2. 再 `await writer.graceful_shutdown()` —— 冲刷队列中存量数据
3. 再 `writer_task.cancel()`
4. 最后 `await asyncio.gather(..., return_exceptions=True)`

并在每步加注释说明**为什么是这个顺序**。

## 4. 验收标准

- [ ] `requirements.txt` 中 `aiomqtt==2.5.1`
- [ ] `Client` 实例在 `while True` 外构造、循环内 `async with client:` 复用
- [ ] 指数退避（1→30s）**仍然存在**，未被改成固定间隔
- [ ] `lifespan` 关闭顺序为：停 mqtt → 冲刷 writer → 停 writer → gather
- [ ] 每处改动附注释，注明依据（CHANGELOG 条目 / 官方 docs 文件名）
- [ ] 若本机跑不起来（PyPI 403、`asyncpg` 在 Python 3.13 编译失败），
      **如实写「未验证 + 所需条件」**，不得声称已验证通过

## 5. 禁止项

1. **禁止**把指数退避改回固定间隔
2. **禁止**顺手升级其他依赖（zustand / redis-py / fastapi 已评估为**不升**，
   理由见 `external/INTEGRATION-PLAN.md` §3）
3. **禁止**修改 `external/INTEGRATION-PLAN.md` 中的审计结论
4. **禁止**伪造验证结果
5. **禁止**只推分支就宣称完成 —— 必须尝试 push 并开 PR；若因
   `repo-code:rw` scope 失败，**原文报告错误**，不要静默结束

## 6. 交付方式

1. 分支 `feat/order-07-aiomqtt-alignment`（基于最新 `main`）
2. 提交信息写明三项改动与依据
3. 尝试 push + 开 PR 到 `main`
4. push 失败则把完整改动 diff 与验证结论写进 issue #8 评论

---

**定位：这是把官方契约套回我们的代码，不是自由发挥。依据都在 `external/INTEGRATION-PLAN.md`，照着改即可。**
