"""User 模型 + fastapi-users 的 SQLAlchemy 适配（订单 15 T1）。

**复用既有 DB 连接**（订单 15 禁止项 3：禁止引入第二套 DB 连接或 ORM 会话）：
本项目写路径走 asyncpg 原生池（`app/db/session.py`），ORM 模型只用于读路径与
schema 对齐。fastapi-users 的 `SQLAlchemyUserDatabase` 需要一个 SQLAlchemy 会话，
所以这里只是把同一个 DSN 包成一个 async engine —— 没有第二套连接串、没有第二份凭据，
`schema` 与 `infra/init.sql` 同源。

建表：`robot_user` 由 `infra/init.sql` 建（见该文件第 7 节），**不调用**
`metadata.create_all()` —— 生产库的 DDL 只能有一个来源，否则会与 init.sql 漂移
（订单 19 的教训就是 ORM 与 DDL 各写各的）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime

from fastapi import Depends
from fastapi_users.db import SQLAlchemyBaseUserTable, SQLAlchemyUserDatabase
from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.roles import DEFAULT_ROLE
from app.config import settings
from app.db.models import Base


class RobotUser(SQLAlchemyBaseUserTable[int], Base):
    """机器人监控系统的操作账号。

    继承 `SQLAlchemyBaseUserTable[int]` 带来 fastapi-users 契约要求的五列
    （`email / hashed_password / is_active / is_superuser / is_verified`）。
    `id` **不在基类里**：框架只给 UUID 版本预置主键
    （`SQLAlchemyBaseUserTableUUID` 才有 `id`），整数主键要自己声明 ——
    少了它 SQLAlchemy 会报 "could not assemble any primary key columns"。
    这里按 `infra/init.sql` 的 `BIGSERIAL` 声明成 `BigInteger` 自增。

    关于 `is_superuser`：框架自带，但本项目两级模型用 `role` 表达。
    保留该列是继承契约的一部分，代码里**不读它**做鉴权，避免"两个真值源"。
    """

    __tablename__ = "robot_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 与 init.sql 的列序一致：role / created_at 追加在框架五列之后。
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DEFAULT_ROLE.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _async_dsn() -> str:
    """`postgresql://…` → `postgresql+asyncpg://…`。

    只做前缀替换，不改写其余部分（认证信息、库名、查询参数原样透传），
    保证 auth 与业务读路径连的是**同一个库**。
    """
    dsn = settings.database_url
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+asyncpg://" + dsn[len(prefix):]
    raise ValueError(f"unsupported DATABASE_URL scheme: {dsn.split('://', 1)[0]}")


_engine = create_async_engine(_async_dsn(), pool_pre_ping=True)
_session_maker = async_sessionmaker(_engine, expire_on_commit=False)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每请求一个 ORM 会话。

    会话由 `async with` 保证归还，异常时自动回滚；
    不在这里手动 commit —— 写操作由 fastapi-users 的 manager 负责提交，
    避免"框架提交了、依赖又提交一次"的双重提交。
    """
    async with _session_maker() as session:
        yield session


async def get_user_db(
    session: AsyncSession = Depends(get_async_session),
) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    yield SQLAlchemyUserDatabase(session, RobotUser)


async def close_engine() -> None:
    """lifespan 收尾时释放 engine 持有的连接。"""
    await _engine.dispose()
