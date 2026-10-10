"""asyncpg 连接池工厂（订单 08 / I-6，asyncpg 0.29.0 → 0.32.0 的行为护栏）。

背景（升级前 vs 升级后，`min_size` 语义变化）：

- <= 0.31：`min_size` = **建池时预建**几条连接，池空闲时可以一路跌到 0，
  下一次 `acquire()` 现连一次（首查询要等一次 TCP + 认证握手）。
- >= 0.32：`min_size` 改成**连接地板**（idle 期间始终维持 N 条，断连后台补齐），
  原本"预建几条"的语义挪给了新参数 `init_size`（默认 = `min_size`）。
  见 asyncpg docs `create_pool()` 的 ``.. versionchanged:: 0.32.0``：
  "The *min_size* parameter now defines the connection floor. The former role of
  *min_size* — setting the initial pool size — is now handled by the new *init_size*
  parameter, which defaults to *min_size*."

我们要的是**两者的并集**：既预建、又当常驻地板（机器人 10Hz 连发 7×24，连接不该被回收）。
所以按官方迁移建议 `min_size=N` → `init_size=N, min_size=0` **不够**，还要保留地板：

    init_size=min_size, min_size=min_size   # 预建 N 条 + 空闲时仍维持 N 条

本模块用单一入口把这件事固定下来，避免两处 `create_pool()` 各写各的、
以后再踩一次同样的语义坑。

兼容性依据（本机实证，见 PR 描述）：
- asyncpg 0.32.0 的 `create_pool()` 同时接受 `init_size` 与 `min_size`
  （`spec` 未锁版本时也安全：0.29/0.31 只忽略未知的 `init_size`）。
- `Pool.get_init_size()` 是 0.32.0 新增，故这里不调用它。
"""

from __future__ import annotations

import asyncpg

__all__ = ["create_pool"]


async def create_pool(
    *,
    dsn: str,
    pool_size: int,
    command_timeout: float = 30.0,
) -> asyncpg.Pool:
    """建一个"预建 N 条 + 空闲维持 N 条"的 asyncpg 池。

    Args:
        dsn: 连接串。
        pool_size: 既是预建条数（`init_size`），也是空闲地板（`min_size`）、
            同时作为 `max_size`。本项目写路径是单连接顺序刷盘
            （`batch_writer._flush` 由 `asyncio.Lock` 串行化），
            池只要够"热"即可，不需要更多并发连接。
        command_timeout: 单条命令的服务端超时。

    注意：`command_timeout` **拦不住** `executemany()` 被锁阻塞时的挂起
    （本机对 PostgreSQL 15.19 实测，见 `batch_writer.py` 的 `asyncio.wait_for`），
    它只覆盖普通语句路径；不要把长事务护栏全押在它身上。
    """
    return await asyncpg.create_pool(
        dsn=dsn,
        init_size=pool_size,   # 0.32.0：建池即预建
        min_size=pool_size,    # 0.32.0：地板；0.29/0.31：预建（语义一致，安全）
        max_size=pool_size,
        command_timeout=command_timeout,
    )
