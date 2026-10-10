# backend_ingestion 验证脚本

本目录存放**零外部依赖**的可复现验证脚本（不依赖 pytest，纯标准库可跑）。

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

## `test_db_integration.py`

需要真实 TimescaleDB，按需运行（`pytest`）：

```bash
python -m pytest backend_ingestion/tests/test_db_integration.py -q
```
