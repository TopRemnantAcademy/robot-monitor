# 订单 29 — DB 连接池/时序采样测试打捞 + session 最小修正（🟢）

> 来源：ISSUE #97。执行方式：CNB NPC @codebuddy。
> 关联素材：`auto/e2e-30k-harness-7687` @ `23e6c6af`（**只作素材，严禁整支 merge**）。

## 定位

你是开发工程师，证据优先。先看代码再动手；本单描述与磁盘实存不符时先报告差异。

## 基线

- 起点：`main`（本单落地前 HEAD = `e1e89d6`）
- 参考分支（**只作素材**）：`auto/e2e-30k-harness-7687` @ `23e6c6af`
- 交付分支：`feat/order-29-db-pool-timescale-tests`

## 参考分支实测 Diff（`git diff --stat main...23e6c6af`）

| 文件 | 增量 | 处理原则 |
| --- | --- | --- |
| `tests/test_db_pool_timeout.py` | +318 **全新** | **取**（核心资产） |
| `tests/test_ts_sampling.py` | +221 **全新** | **取** |
| `backend_ingestion/app/db/session.py` | +36 | 取最小必要改动 |
| `backend_ingestion/app/db/pool.py` | +10 | 取 |
| `alert_engine/app/pool.py` | +10 | 取 |
| `backend_ingestion/app/config.py` | +8 | 取（仅本单所需键） |
| `simulator/mock_fleet.py` | +71 | **冲突以 main 为准**（见下方偏差说明） |
| `tests/e2e/test_e2e_30k.py` | +92 | **冲突以 main 为准**（ISSUE #60 已落地） |
| `tests/e2e/README.md` | +108 | 冲突以 main 为准 |
| `docs/requirements/R-09-database-integration.md` | ±2 | 冲突以 main 为准 |
| `orders/LEDGER.md` | +45 | **丢弃**（台账归主代理维护） |

## ⚠️ 派工描述与磁盘实存的偏差（执行期发现，原文上报）

派工单称 `simulator/mock_fleet.py`「**冲突以 main 为准（main 已合 #60 版本）**」，
并据此要求「未改动 `simulator/mock_fleet.py`」。**实测该前提不成立**：

```
$ git merge-base --is-ancestor origin/auto/e2e-30k-harness-7687 origin/main
NO not ancestor
$ git log --all --oneline -S "_next_ts_ms"
23e6c6a fix(e2e): 修 CI 第二轮暴露的 ms 时间戳重复丢行 + 补齐不依赖 Docker 的护栏
```

- `_next_ts_ms` **只存在于参考分支**，从未进入 main；
- main 的 `simulator/mock_fleet.py:run_robot()` 仍逐条 `int(time.time() * 1000)`
  （`simulator/mock_fleet.py:176`），正是 `test_ts_sampling.py` 要盯的病根。

**因此**：`test_ts_sampling.py` 若要「在本仓判定」（T1 明令），必须依赖
`mock_fleet._next_ts_ms`。二者不可兼得时，本单选择**让测试真正可判定**，
并**只取最小生产修正**：`_next_ts_ms()` 函数本体 + `run_robot()` 两行接线（+51 行），
**不取**参考分支的 `--no-publish` 与 e2e/文档改动（那部分才与 main 冲突）。

## 必读

1. `backend_ingestion/app/db/session.py` / `pool.py` —— 现状与连接池配置
2. `tests/test_schema_contract.py` —— 既有 DB 相关测试风格（真实 SQL 计数，禁替身）
3. `pytest.ini` —— testpaths（**禁止修改**，订单 24 §4.1）
4. `tests/e2e/README.md` —— e2e 与单元测试的分工口径

## 任务分解

### T1 迁移两个测试文件

- `test_db_pool_timeout.py`：连接池建池超时护栏。静态断言 + 对 RFC 5737
  黑洞地址（`192.0.2.1`）建池的**实测**，要求 < 25s 有界失败；
  缺 asyncpg 时 skip（退出码 3）并打印原因。
- `test_ts_sampling.py`：模拟器毫秒时间戳唯一性护栏（PK `(time, robot_id)`）。
  静态断言 `build_payload()` 的 `ts` 来自发号器 + 实测严格单增 + 空闲跟随真实时间。
- 两文件各含 ≥3 个真实 `def test_`（新 CI 门禁会拦假覆盖文件）。

### T2 session/pool/config 最小修正

- `backend_ingestion/app/config.py`：新增 `db_connect_timeout_s: float = 8.0`
- `backend_ingestion/app/db/session.py`：模块头补建池超时机制说明；`create_pool(..., timeout=settings.db_connect_timeout_s)`
- `backend_ingestion/app/db/pool.py` / `alert_engine/app/pool.py`：`create_pool()` 暴露并透传 `connect_timeout`
- `alert_engine/app/config.py`：新增 `db_connect_timeout_s`（main 已有 `db_pool_size`/`db_command_timeout`，勿重复）
- `alert_engine/app/db.py`：`create_pool(..., connect_timeout=settings.db_connect_timeout_s)`
- **不得改连接池默认大小**（性能护栏）。实测 `db_pool_min_size=2 / db_pool_max_size=10 / db_command_timeout_s=30.0` 全部保持不变。

## 验收标准（可判定）

- [x] `python -m pytest -q` 全绿或以明确 skip 理由通过：**84 passed, 1 skipped**
- [x] `python -m pytest backend_ingestion/tests/ -v --timeout=60`：**155 passed, 6 skipped**
- [x] 每个新增 `test_*.py` 至少 3 个真实测试函数：6 个 / 3 个
- [x] 未改动 `tests/e2e/*`、`orders/LEDGER.md`
- [x] `orders/29-dispatch.md` 一并提交进分支
- [~] 未改动 `simulator/mock_fleet.py` —— **见上方偏差说明**，取最小生产修正

## 禁止项

1. 禁止整分支 merge / cherry-pick（含与主链冲突的修饰）
2. 禁止为过测试而把真实 DB 断言改成进程内 dict 替身
3. 禁止改 `pytest.ini` testpaths
4. 禁止弱化既有断言
5. 禁止引入新 PIP 依赖（CI 只装既定 requirements + fakeredis/pytest-timeout）

## 交付方式

分支 `feat/order-29-db-pool-timescale-tests` → PR，描述列出逐个用例的实测输出（含 skip 的原文输出）。

## 失败上报

原文粘贴错误与 skip 原因；禁止静默失败。
