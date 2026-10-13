# R-14 — 告警引擎 `consume()` Redis 断连监督（进程级韧性）

> **@//codebuddy/** — 本需求由本地（主代理）撰写，指派 codebuddy NPC 读仓执行。
> 本地**只写需求规格、不写业务修复代码**；codebuddy 落地后主代理按两点 diff 白名单核验 + CI 双绿回收。
>
> 状态：**待执行（韧性修复类，中风险）**。实证见 §1，非推测。
> ⚠️ 改动落在 `alert_engine` 主协程树，§6 红线为硬门槛。

---

## 1. 背景（file:line 实证）

`alert_engine/app/main.py` 的引擎入口：
- `amain()` 用 `asyncio.gather(consume(), ticker(), escalation_loop())`（`:312`），即**三个协程共享一条协程树**；
- `consume()`（`:222`）建 `pubsub`（`:224`）→ `subscribe`（`:225`）→ `async for msg in pubsub.listen()`（`:227`）；
- 循环体内**只对 `json.JSONDecodeError` 做了 try/except**（`:232`），对单条规则求值 `rule.evaluate` 有兜底（`:245-246`）；
- 但 `pubsub.listen()` 本身**没有 Redis 连接异常兜底**。

**故障模式（真，非推测）**：Redis 闪断 / 网络抖动时，`pubsub.listen()` 抛 `ConnectionError`（或 `redis` 驱动的 `ConnectionError`/`OSError`/`TimeoutError`）。该异常**逃出 `consume()`**，被 `asyncio.gather`（`:312`）**向上传播** → 整个 `amain()` 协程树失败 → 引擎进程退出。
即便容器有 `restart` 策略，进程内**流式告警消费在重启前是永久停的**，且 `ticker()`（离线检测）与 `escalation_loop()`（升级扫描）**被同一 gather 连带取消**（它们本身没坏，却因兄弟协程崩溃而死）。

`main.py:264` 的注释也自证了这一设计脆弱点：「后者会在本引擎重启 / Redis pubsub 断连时悄悄失真」——即作者已知断连是真实场景，但**没在 `consume()` 加监督**。

---

## 2. 目标

- `consume()` 增加 **Redis 断连监督**：连接异常不再逃出协程、不再拖垮整条 `gather` 协程树；
- 断连后**自动退避重连**（指数退避 + 上限），重连成功后**重新 `subscribe`**（`subscribe` 状态不会跨连接保留）；
- 保留现有行为：正常消息处理、`JSONDecodeError` 跳过、单条规则异常兜底——全部不变；
- 加**可观测**：重连次数计数/日志，让"闪断"可被发现（与 R-15 的通知指标同思路，但本需求只做重连相关）。

---

## 3. 范围边界（明确不做）

- **只改** `consume()` 的容错与重连（及必要的 `amain()` 编排）；
- **不改** `ticker()`（`:268-286`）、`escalation_loop()`、`escalation.py` 的既有逻辑；
- **不改** 规则求值、去重（`deduplicator.py`）、静默（`silence.py`）、落库（`store.py`）的既有语义；
- **不引入** 新的外部依赖（用标准库 `asyncio` + 现有 `redis` 客户端的异常类型）；
- **不改** 指标命名规范（沿用 `robot_alert_*` 前缀，见 `metrics.py`）；
- 不改 `backend_ingestion`（本需求只在 `alert_engine`）。

---

## 4. 任务分解

### T1 — `consume()` 断连监督 + 退避重连（`alert_engine/app/main.py`）
- 把 `consume()` 的 `async for msg in pubsub.listen()` 包进**自重启结构**：
  `while True:` → 内层 `try:` 跑监听；`except (ConnectionError, OSError, TimeoutError, redis 的断连异常类型):` → 记录日志 + 退避（`asyncio.sleep`，指数退避 + 最大上限，如 1s→30s 封顶）→ **重建 pubsub 并重新 `subscribe`** → `continue`。
- 关键：**异常不得逃出 `consume()`**；且重连后必须重新 `subscribe(settings.chan_stream_all)`（`:225`），否则订阅丢失 → 静默不消费。
- 保留 `:232` 的 `JSONDecodeError` 处理与 `:245-246` 的规则异常兜底（不动）。

### T2 — 可观测（重连可发现）
- 重连发生时打 `log.warning`（含退避秒数、连续失败次数）；
- 建议加一个 Counter（如 `robot_alert_redis_reconnect_total`）或在既有指标文件 `alert_engine/app/metrics.py` 中登记；若加指标，**必须**用 `robot_alert_*` 前缀（与 `metrics.py:34-82` 现有命名一致），并在 `/metrics` 可见。
- 严禁把"重连"当成致命错误 `raise`（那会让本需求失效）。

### T3 — 编排安全（`amain()`，可选但推荐）
- 若 `asyncio.gather(...)`（`:312`）未加 `return_exceptions=True`，评估是否给 `consume/ticker/escalation` 各自的异常加隔离，避免**任一**协程意外崩溃再连带兄弟。
- 注意：加 `return_exceptions=True` 会**吞掉真正的崩溃**（进程静默假活），须配合 T2 日志/指标让故障可见；若采用，请在注释写明取舍。

### T4 — 回归测试
- 单测：mock `pubsub.listen()` **先抛 `ConnectionError` 再恢复正常** → 断言 `consume()` 不退出、退避后重新 `subscribe`、能继续消费；
- 单测：断言异常**不传播**出 `consume()`（模拟后 `gather` 不因它失败）；
- 保证现有告警规则相关单测仍 PASS（不得弱化）。

---

## 5. 验收标准（可判定）

| # | 用例 | 期望 | 判定依据 |
|---|---|---|---|
| V1 | 断连自愈 | `pubsub.listen()` 抛 `ConnectionError` → `consume()` 不退出、退避后重连并**重新 subscribe** | 新单测（mock 断连） |
| V2 | 不拖垮兄弟 | 该异常**不传播**出 `consume()`；`ticker()`/`escalation_loop()` 不被连带取消 | 新单测 + 读 `:312` 编排 |
| V3 | 现有行为不变 | 正常消息处理、`JSONDecodeError` 跳过、规则异常兜底逐字保留；既有单测全绿 | 既有 `alert_engine` 单测 |
| V4 | 可观测 | 重连有 warning 日志 / 指标（若加指标须 `robot_alert_*` 前缀且 `/metrics` 可见） | 日志/metrics 实证 |
| V5 | CI 双绿 | `pipeline-1(test)` + `pipeline-2(e2e-30k-landing)` 均 success | `cnb pulls list-pull-commit-statuses` |

---

## 6. 约束 / 禁止项（白名单）

- `git diff` 白名单：`alert_engine/app/main.py`（`consume()`/`amain()` 容错）± `alert_engine/app/metrics.py`（仅新增指标）± 新增/修改的 `alert_engine` 测试文件。
- 禁止改动 `ticker()`、`escalation_loop()`、`escalation.py`、`rules/`、`deduplicator.py`、`silence.py`、`store.py` 的既有逻辑。
- 禁止因加容错而**弱化**现有异常处理（`.232` / `:245-246` 必须保留）。
- 禁止引入新第三方依赖。
- 禁止把重连失败再次 `raise` 出协程（那等于没修）。
- 不得夹带 `backend_ingestion` / 前端 / `.cnb.yml` 改动。

---

## 7. 交付物

- `alert_engine/app/main.py`：`consume()` 断连监督 + 退避重连 + 重新 subscribe（含 T2 日志/指标）。
- 回归测试：mock 断连自愈用例 + 异常不传播用例。
- PR 的 `git diff` 白名单如上；CI 双绿。

## 8. 关联

- 来源：`docs/GAP-SCAN-2026-10-13.md` **GAP-2**（MED，韧性）。
- 相关：`main.py:264` 注释自证断连是已知场景；R-15（通知失败可观测）为同批可观测改进。
