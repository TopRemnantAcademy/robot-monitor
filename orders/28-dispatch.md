# 订单 28 — G-6：robot_metadata 心跳写入 + offline 规则改读 is_online（🟢）

> 本文件作为订单规格存档随交付分支入库。执行中发现的**基线偏差**记录在
> 「附录 A：与原始派工单的偏差」——按 AGENTS.md §6「发现描述与磁盘实存不符，
> 先报告差异再动手」处置。

## 定位

你是开发工程师，证据优先。写完先自查，再交付；发现本单描述与磁盘实存不符，
先报告差异再动手，禁止按文档臆断。

## 基线

- 起点：`main @ e1e89d6`（订单 28 派发时的最新 HEAD）
- 参考分支（**只作素材，不得直接推/合**）：`auto/metadata-heartbeat-3f2a @ 0dba075f`
- 交付分支：`feat/order-28-metadata-heartbeat`

## 必读（逐个读，禁止跳过）

1. `infra/init.sql:34-52` —— `robot_metadata.last_seen` / `is_online` 两列已建，
   本单之前**没有任何批量写方**（STATUS.md 的 G-6）
2. `backend_ingestion/app/services/liveness.py` —— ISSUE #77 引入的在线台账
   （热路径只碰 Redis；本单把 `robot_metadata` 的写方收成唯一一处）
3. `backend_ingestion/app/services/state_cache.py` —— 心跳投递挂载点
4. `alert_engine/app/rules/offline.py` —— 本单改动判定口径
5. `alert_engine/app/store.py` —— 维护告警持有状态 + 本单元数据读路径
6. `AGENTS.md` §3 性能护栏 / §4 数据契约

## 参考分支实测 Diff（`git diff --stat main...0dba075f`）

| 文件 | 增量 | 性质 |
| --- | --- | --- |
| `backend_ingestion/app/services/metadata_writer.py` | +168 | **全新**（核心） |
| `backend_ingestion/tests/test_metadata_writer.py` | +158 | **全新** |
| `backend_ingestion/tests/verify_metadata_heartbeat.py` | +192 | **全新**（人工脚本，需真实库） |
| `backend_ingestion/tests/README.md` | +32 | 全新 |
| `backend_ingestion/app/config.py` | +6 | 开关 |
| `backend_ingestion/app/main.py` | +13 | 挂载 |
| `backend_ingestion/app/services/state_cache.py` | +11 | |
| `alert_engine/app/main.py` | +35 | 挂载 |
| `alert_engine/app/rules/offline.py` | +42 | 改读 is_online |
| `alert_engine/app/store.py` | +25 | |
| `alert_engine/app/config.py` | +6 | |
| `alert_engine/app/db.py` | +7 | |
| `alert_engine/README.md` / `.env.example` / `STATUS.md` | 少量文档 | |

参考分支实际共 **16 个文件**（原始派工单表只列 11 个，其余 5 个见附录 A）。

## 任务分解

### T1 元数据心跳写入（backend）

- `metadata_writer.py`：批量/周期性维护 `robot_metadata.last_seen` 与 `is_online`
- 写入**幂等**（`UPDATE ... FROM unnest` + `IS DISTINCT FROM`），禁止追加式重复行
- config 开关（`metadata_heartbeat_enabled`）控制，默认开启；关闭时 `run()` 直接退出、
  `flush()` 不发任何语句 —— **不改变既有遥测写路径的语义与吞吐**（性能护栏）
- 心跳走批量：1000 条遥测折叠成 **1 条 UPDATE / ≤100 个 robot_id**

### T2 offline 规则改读 is_online

- `rules/offline.py` 判定**优先读** `robot_metadata.is_online`
- **迁移期回落**：`is_online is None`（列还没写 / 读不到该行）时回落到 `last_seen`
  时间差判定，**不得**误报离线
- 开关 `offline_use_is_online`（默认 True）保留一句话回滚

### T3 测试

- `test_metadata_writer.py`（9 个用例：幂等 upsert / 批量合并 / 开关关闭不写 /
  语句数不随条数增长 / 只写既有列 / `stop()` 不丢在飞心跳）
- `test_offline_is_online.py`（9 个用例：列即判据 / 判据单源 / 迁移期回落 / 开关回滚）
- `test_metadata_write.py` 追加 3 个读路径用例（`load_online_state` + ticker 透传）
- `verify_metadata_heartbeat.py` 收作**人工脚本**（需真实 TimescaleDB/PG，CI 跑不了），
  在 README 标明 How to run

## 验收标准（可判定）

- [x] `python -m pytest -q` 全绿（87 passed, 1 skipped）
- [x] `python -m pytest backend_ingestion/tests/ -q --timeout=60` 全绿
      （164 passed；1 个 `test_resolution_param` teardown error 为 **main 既有**
      —— 见附录 A.3，clean main 同样 error）
- [x] CI 三件套（`import app.main` / compileall / test 文件健全性）不因本单变红
- [x] 新增测试文件全部含真实 `def test_` 函数
- [x] `orders/28-dispatch.md` 一并提交进分支
- [x] 人工脚本实测 `PASS=17 FAIL=0`（真实 PostgreSQL 15.19）

## 禁止项

1. 禁止直接 merge 参考分支到 main 再改（必须净增量重做）
2. 禁止弱化既有测试断言
3. 禁止为跑通测试而 mock 掉真实语义
4. 禁止改 `pytest.ini` 的 testpaths
5. 禁止用进程内 dict 缓存替身冒充真实 Redis/Postgres 行为

---

## 附录 A：与原始派工单的偏差（取证）

### A.1 「参考分支 diff 只有 11 个文件」不实

实测 `git diff --stat main...0dba075f` 为 **16 文件 +749/-29**。派工单表遗漏了
`.env.example` / `STATUS.md` / `alert_engine/README.md` / `alert_engine/app/config.py` /
`alert_engine/app/db.py`。

### A.2 参考分支基于「#77 之前」的 main，直接照抄会打红 main 上既有测试

main 已前进到 `5dd1d1a fix(#77)`，新增 `backend_ingestion/app/services/liveness.py`。
参考分支的两处做法与 main 现状**直接冲突**：

1. **删除 `alert_engine/app/store.py:update_online()`**。但 main 上
   `alert_engine/tests/test_metadata_write.py`（4 passed）正在测它——
   照抄违背「禁止项 2」。→ 本单**保留** `update_online` 与既有写路径，
   只把 offline 的**判据**改成读列；新增 `load_online_state()` 读路径。
2. **强制 `offline_after_s == float(state_ttl_seconds)`**。但 main 的
   `backend_ingestion/app/config.py:59-62` 明文写着：二者**刻意不同**
   （`offline_after_s` = 告警阈值，`robot_offline_threshold_s` = 展示阈值），
   「不要为了『看着一致』把它们并成一个值」。→ 本单**不引入**该等式。

### A.3 一个 pre-existing 的测试错误（非本单引入）

`backend_ingestion/tests/test_resolution_param.py::TestHttpLayer::test_fractional_is_400_not_truncated[2.5s]`
在**整套连跑**时 teardown 报 `RuntimeError: Event loop is closed`；单跑该文件
54 passed 无错。取证：`git stash -u` 到 clean main 后整套连跑**同样**
`155 passed, 1 error`。故与本单无关，未修（避免越界改动）。

### A.4 双写方风险的收口选择

main 上 `liveness.py:23` 的注释写「`robot_metadata.is_online/last_seen` 由告警引擎
按状态变化低频落库」。本单新增 `metadata_writer` 后，短期存在两个写方
（backend 心跳 + alert_engine 状态变化落库）。二者**语义同源**（都来自「收到遥测」），
不会写出矛盾值；但双写方本身是技术债。

本单采取的折中（不改既有绿测试的前提下把风险降到最低）：
- **写方语义统一**在 `is_online = 最近是否收到遥测`，两个写方用同一判据；
- **判据单源**：offline 规则只读列，不再自行推算 → 即使写方短暂不一致，
  读到的是同一列，不会出现「看板说在线、告警说离线」；
- 彻底收敛为单写方（删 `update_online` + 改造 `test_metadata_write.py`）需
  用户拍板，已在 ISSUE #96 评论中提出，未擅自执行（属「禁止项 2」灰区）。
