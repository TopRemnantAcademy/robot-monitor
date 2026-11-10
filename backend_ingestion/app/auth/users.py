"""User model + fastapi-users SQLAlchemy adapter (order 15 T1).

**Reuse the existing DB connection** (order 15 forbidden item 3: no second DB
connection or ORM session): this project's write path uses the native asyncpg
pool (`app/db/session.py`); the ORM model is only for the read path and schema
alignment. fastapi-users' `SQLAlchemyUserDatabase` needs a SQLAlchemy session, so
here we only wrap the same DSN into an async engine -- no second connection
string, no second set of credentials, and `schema` is co-sourced with
`infra/init.sql`.

Table creation: `robot_user` is created by `infra/init.sql` (see section 7 of that
file); **do not call** `metadata.create_all()` -- the production DB's DDL can have
only one source, otherwise it drifts from init.sql (order 19's lesson was ORM and
DDL each writing their own).
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
    """Operator account for the robot monitoring system.

    Inheriting `SQLAlchemyBaseUserTable[int]` brings the five columns the
    fastapi-users contract requires (`email / hashed_password / is_active /
    is_superuser / is_verified`). `id` is **not** in the base class: the framework
    only pre-seeds a primary key for the UUID version (`SQLAlchemyBaseUserTableUUID`
    has `id`); an integer primary key must be declared yourself -- without it
    SQLAlchemy errors "could not assemble any primary key columns". Here it is
    declared as `BigInteger` auto-increment per `infra/init.sql`'s `BIGSERIAL`.

    On `is_superuser`: it comes with the framework, but this project's two-level
    model expresses roles via `role`. Keeping the column is part of the inherited
    contract; the code does **not** read it for auth, to avoid "two truth sources".
    """

    __tablename__ = "robot_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Column order matches init.sql: role / created_at appended after the five
    # framework columns.
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DEFAULT_ROLE.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _async_dsn() -> str:
    """`postgresql://…` -> `postgresql+asyncpg://…`.

    Only prefix replacement, leaving everything else untouched (credentials, DB
    name, query params pass through verbatim), guaranteeing auth and the business
    read path connect to the **same DB**.
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
    """FastAPI dependency: one ORM session per request.

    The session is returned by `async with`, rolling back automatically on
    exception; do not commit manually here -- writes are committed by fastapi-
    users' manager, avoiding "framework committed, dependency commits again"
    double commit.
    """
    async with _session_maker() as session:
        yield session


async def get_user_db(
    session: AsyncSession = Depends(get_async_session),
) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    yield SQLAlchemyUserDatabase(session, RobotUser)


async def close_engine() -> None:
    """Release connections held by the engine on lifespan teardown."""
    await _engine.dispose()
