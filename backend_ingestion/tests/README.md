# backend_ingestion 验证脚本

本目录存放**按需运行**的验证脚本 —— 都是**独立脚本，不是 pytest 用例**
（跑法见下，退出码 0 = 全部通过）。分两类：

| 脚本 | 依赖 | 何时跑 |
|---|---|---|
| `verify_batch_writer.py` | 零依赖（读源码 AST） | 每次改动 `batch_writer.py` / 连接池后 |
| `verify_db_integration.py` | 真实 PostgreSQL | 有可写 PG 且已执行 `infra/init.sql` 的纯 PG 部分时 |

> ⚠️ **不要用 `pytest` 跑本目录**：两个文件都没有 `test_*` 函数，
> pytest 会收集到 **0 tests** 并静默"通过"，制造假的安全感。
> `verify_db_integration.py` 原名 `test_db_integration.py` 正是因此改名。

## `verify_batch_writer.py`

对 `app/services/batch_writer.py` 与连接池做**静态契约验证**
（读源码 AST，不需要 DB、不需要第三方包）：

- 降级链完好：L1 `_copy_upsert` / L2 `_executemany` / L3 `_isolate_and_write`、
  `_STOP` 哨兵、COPY 路径、`ON CONFLICT` 幂等
- 丢行三元组已接线：`rows_submitted` / `rows_written` / `rows_skipped`
- **L1 必须用 `INSERT ... ON CONFLICT` 的 affected rows 记真实入库行数**
  （否则 `written` 计的是提交行数、`skipped` 永远为 0 —— 本机踩过这个坑）
- 连接池护栏：`MAX_POOL_SIZE` 下限、`max_size != min_size`、`get_pool()` 建池锁

```bash
# 仓库根
python3 backend_ingestion/tests/verify_batch_writer.py
# 或隔离环境
/tmp/rm-venv/bin/python backend_ingestion/tests/verify_batch_writer.py
```

退出码 0 = 全部通过。

> **历史**：本脚本此前由 `fix/issue19-merge-safe` 带入，验证的是该分支的
> `_to_columns` / `unnest` 列式改造 —— 而那套改造 **main 上不存在**
> （main 保留的是三级降级链 + `_to_row`）。即该脚本 import 即
> `AttributeError`，是一份**跑不起来的交付物**。本轮换成验证 main 真实存在的逻辑。

## `verify_db_integration.py`

需要**真实 PostgreSQL**（TimescaleDB 专属语句不在其覆盖范围，见文件头注释）：

```bash
export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/robot_monitor
cd backend_ingestion && python -m tests.verify_db_integration
```

未设 `TEST_DATABASE_URL` 时打印「跳过」并退出 0 —— 所以它**永远不该**被当成
CI 门禁，只作为有 DB 时的人工复核手段。

覆盖 6 组场景（T1 COPY 写真实 JSONB / T2 幂等 / T3 降级链 L1→L2 / T4 坏行隔离 /
T5 吞吐冗余 / T6 ORM DDL 与 `infra/init.sql` 列类型一致），
每组都对应一个**已修掉的真实缺陷**。
