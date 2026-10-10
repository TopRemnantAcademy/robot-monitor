# backend_ingestion 验证脚本

本目录存放**零外部依赖**的可复现验证脚本（不引入 pytest，因本机与 CI 均无该环境）。

## `verify_batch_writer.py`

验证 `app/services/batch_writer.py` 的 unnest 列式批量改造（ISSUE #19 G2）：

- 17 个列式数组与 `INSERT_SQL` 的 `unnest($1..$17)` 顺序一一对应
- 时间戳取自客户端毫秒值（而非刷盘时刻 `now()`）
- 丢行三元组 `rows_submitted / rows_skipped / flush_failures` 在
  「全插入 / 部分冲突 / 整批失败」三种情况下的行为

它通过 sys.modules 桩掉 `redis` / `asyncpg` / `app.config` / `app.db.*`，
以便在**无 DB、无第三方包**的环境下真实执行核心逻辑。

```bash
# 仓库根
python3 backend_ingestion/tests/verify_batch_writer.py
```

退出码 0 = 全部通过。
