# 派工单：订单 08 — asyncpg 升级与连接池/刷盘护栏（I-6）

> 派单：`cnb issues create-issue --repo flow.237/RobotMonitor --body-file orders/08-dispatch.md`
> 触发：`cnb issues post-issue-comment --repo flow.237/RobotMonitor --number 9 --work-mode --body-file orders/08-dispatch.md`

---

@codebuddy 请接单实现**订单 08（asyncpg 升级 I-6）**。

**本机验证通道已打通**：用
`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple <pkg>`
装依赖，不要再说"本机跑不起来"。本单的验收**要求真实运行时证据**，
不接受"静态检查通过"作为结论。

## 0. 背景

`external/INTEGRATION-PLAN.md` §3 把 `aiomqtt` 列为**唯一例外升级项**，
其余依赖（zustand / redis-py / fastapi / echarts）已判定"不升"。
但 `asyncpg` 当时**没有被评估过** —— 它停留在 `0.29.0`（2024-10），
上游已经到 `0.32.0`。本单补上这块，并且**借升级之机把两处真实隐患修掉**。

## 1. 任务

### T1 — `asyncpg` 0.29.0 → 0.32.0

**文件**：
- `backend_ingestion/requirements.txt`
- `alert_engine/requirements.txt`
- `spec/01_backend_data_ingestion_and_storage.md` §2（依赖版本表）

**为什么升**：
- 0.30.0 起支持 Python 3.13 / PostgreSQL 17；0.31.0 起支持 3.14；
  0.32.0 起支持 3.15 / PostgreSQL 19
- 0.30.0 修了 connection pool 的连接泄漏；0.32.0 修了协议中断后释放连接的**池死锁**
- 0.32.0 修了取消被卡住时阻塞连接拆除的问题

**禁止**顺手升级其它依赖（见 `external/INTEGRATION-PLAN.md` §3 的"不升"结论）。

### T2 — 🔴 连接池：`min_size` 语义变化（**这是升级最容易踩的坑**）

asyncpg 0.32.0 起，`create_pool(min_size=N)` 的含义**变了**。
官方 `create_pool()` 的 `.. versionchanged:: 0.32.0` 原文：

> The *min_size* parameter now defines the connection floor. The former role of
> *min_size* — setting the initial pool size — is now handled by the new *init_size*
> parameter, which defaults to *min_size*.

即：`min_size` 从「建池时预建几条」变成「空闲期间维持几条」。
官方给出的迁移建议是 `min_size=N` → `init_size=N, min_size=0`，
**但那会丢掉地板**（池空闲时仍会跌到 0，下一次 `acquire()` 要现连）。

本项目 7×24 收 10Hz 遥测，要的是**预建 + 常驻**两者都要。故采用
`init_size=N, min_size=N`，并把这件事**收敛到单一入口**，避免两个服务各写各的：
- 新增 `backend_ingestion/app/db/pool.py:create_pool()`
- 新增 `alert_engine/app/pool.py:create_pool()`
- `backend_ingestion/app/db/session.py` 与 `alert_engine/app/db.py` 改为调用它
- 池条数走配置 `DB_POOL_SIZE`（默认后端 4 / 告警 2），不再硬编码

### T3 — 🔴 刷盘加硬超时护栏（**本次实测发现的真实隐患**）

**文件**：`backend_ingestion/app/services/batch_writer.py`

**实测事实**（本机 PostgreSQL 15.19 + asyncpg 0.32.0，见 §4 验证证据）：
当另一会话 `LOCK TABLE robot_telemetry IN ACCESS EXCLUSIVE MODE` 时，
`conn.executemany()` 会**无限挂起**，并且 **`command_timeout` 完全不生效**
（连接级 `command_timeout=5` 也挂 >25s；`copy_records_to_table` 同样）。

这是 **0.29 / 0.31 / 0.32 三代一致的行为**，不是升级引入的；
但正是这次升级的实测把它暴露出来了，所以必须在本单修掉。

**后果**：`_flush` 不返回 → `run()` 不再消费队列 → 队列涨到 `maxsize=20000`
→ 新遥测在 `put()` 被丢弃，整条采集链路**静默停摆**。

**要求**：刷盘走 `asyncio.wait_for(..., FLUSH_TIMEOUT_SECONDS)`。
超时/异常都吞掉（采集循环优先于丢批），但必须打 error 日志，
让 `/healthz` 的 `written` 不增长成为可观测信号。

### T4 — 本机真实运行时验证（**核心交付**）

"本机跑不起来"不再是理由。必须实际起 PostgreSQL（或 TimescaleDB）+ Redis + 一个
可用的 MQTT broker，跑通端到端并给出**命令 + 输出**：
1. 30000 条（100 台 × 10Hz × 30s）落库核对
2. 表被锁 → 护栏丢批 + 采集循环存活；解锁 → 自愈
3. 连接池预建条数符合预期（`get_size()`/`get_init_size()`）
4. `/healthz`、`/robots`、`/robots/{id}/history` 在真实库上返回正确

## 2. 验收标准

- [ ] `backend_ingestion/requirements.txt` 与 `alert_engine/requirements.txt` 均为 `asyncpg==0.32.0`
- [ ] `spec/01` §2 依赖表同步
- [ ] 连接池统一走 `create_pool()`，`init_size` 与 `min_size` 都设，注释写明 0.32.0 语义变化依据
- [ ] `batch_writer` 有 `asyncio.wait_for` 硬超时护栏，注释写明实测依据
- [ ] 每处改动附注释，注明依据（官方 CHANGELOG / 官方文档原文 / 本机实测）
- [ ] 端到端实测数字（吞吐 / 落库行数 / 队列峰值 / 丢包）写入 PR 描述
- [ ] 无法验证的项**如实标注「未验证 + 所需条件」**

## 3. 禁止项

1. **禁止**只改 `requirements.txt` 不处理 `min_size` 语义变化（那是本次最大的坑）
2. **禁止**按官方建议写成 `min_size=0` 丢掉地板
3. **禁止**顺手升级其它依赖
4. **禁止**伪造验证结果；跑不出来就写"未验证 + 所需条件"
5. **禁止**只推分支就宣称完成 —— 必须尝试 push 并开 PR；失败则**原文报告错误**
6. **禁止**为了让测试通过而放宽阈值

## 4. 交付方式

1. 分支 `feat/order-08-asyncpg-upgrade`（基于最新 `main`）
2. 提交信息写明升级项与实测数字
3. push + 开 PR 到 `main`
4. push 失败则把完整 diff 与验证结论写进 issue #9 评论

---

**定位：不是"把版本号改大"，是"升级时必须处理的语义变化 + 顺带修掉实测发现的真隐患"。**
