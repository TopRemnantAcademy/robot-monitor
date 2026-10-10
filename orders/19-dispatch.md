# 派工单：订单 19 — `robot_alert` 身份与 ack 字段落地（消除 ORM/DDL 漂移）

> 依据：主代理 2026-10-11 对 22 个远端分支的全量对账结论
> 触发方式：`post-issue-comment`（本文件不含触发词）
> 前置：`main` = `7a142c2`（已含 /metrics 接线、丢行计数、第五批外部源清单）

---

## 0. 现状实证（逐条 file:line，勿凭印象改）

| 事实 | 位置 |
|---|---|
| `robot_alert` 建表**没有声明任何主键**，靠 hypertable 分区 | `infra/init.sql:55-63` |
| 仅有 `ack BOOLEAN DEFAULT FALSE`，**无法指出"哪一条"被确认、谁在何时确认** | `infra/init.sql:62` |
| ORM 侧用**映射级** `__mapper_args__` 绕开"无主键却必须有主键"的矛盾 | `backend_ingestion/app/db/models.py:78-86` |
| 写入路径是**位置参数 INSERT**，不含 `alert_id` 列名 | `alert_engine/app/store.py:19-41` |
| 写入用 `pool.acquire()`（失败时丢弃该连接，避免脏连接回池） | `alert_engine/app/store.py:33`、`:64` |
| 存在 `idx_alert_ack` 部分索引（`WHERE ack = FALSE`） | `infra/init.sql:171` |

## 1. 为什么要做：现在的主键语义是**错的**

`models.py:86` 的 `__mapper_args__ = {"primary_key": [time, robot_id]}` 是**权宜之计**：
它不产生 DDL，所以躲开了"凭空造主键"的问题，但 ORM 身份仍被定义为
`(time, robot_id)` —— 语义等价于「同一机器人同一时刻只能有一条告警」。

**这与运行期事实冲突**：`alert_engine` 的 `emit()` 在同一轮 tick 内可对同一机器人
命中**多条不同规则**（`motor_temp` / `battery_low` / `joint_stuck` 各自独立去重窗口，
见 `alert_engine/app/main.py` 的 `DEDUP_WINDOW`）。两条告警拿到相同 `(time, robot_id)`
时，SQLAlchemy 的身份映射会把后一条**覆盖前一条**。

同时权宜之计带来第二重代价：ORM 的 DDL 与 `init.sql` **永远不一致**
（一个无 PK、一个靠 mapper 声明），任何 `metadata.create_all()` 都对不上库内真实结构。

**正解**：让主键**真实存在** —— 加 `alert_id BIGSERIAL`，主键声明为
`(time, alert_id)`（含分区列 `time`，满足 TimescaleDB 硬性要求）。
这样 ORM 与 DDL 天然一致，`__mapper_args__` 可以**整个删掉**，`ack` 也能指向具体某一条。

---

## 2. 任务

### T1 — `infra/init.sql`：告警表结构升级

`CREATE TABLE IF NOT EXISTS robot_alert`（`init.sql:55`）改为：

```sql
CREATE TABLE IF NOT EXISTS robot_alert (
    alert_id  BIGSERIAL,
    time      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    robot_id  TEXT        NOT NULL,
    rule_id   TEXT        NOT NULL,
    severity  INTEGER     NOT NULL,
    message   TEXT        NOT NULL,
    detail    JSONB,
    ack       BOOLEAN     NOT NULL DEFAULT FALSE,
    ack_at    TIMESTAMPTZ,
    ack_by    TEXT,
    -- 主键必须含分区列 time（TimescaleDB 硬性要求）
    PRIMARY KEY (time, alert_id)
);
```

约束：
- `create_hypertable('robot_alert', 'time', ...)`（`:65`）**不得改动**
- `idx_alert_robot_time`（`:170`）与 `idx_alert_ack`（`:171`）保留
- 压缩/保留的幂等 `DO $$` 块（`:144-165`）**一个字都别动**

### T2 — `backend_ingestion/app/db/models.py`：删掉 `__mapper_args__` 权宜之计

```python
class RobotAlert(Base):
    __tablename__ = "robot_alert"

    alert_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), primary_key=True)
    robot_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    ...
    ack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ack_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ack_by: Mapped[str | None] = mapped_column(Text)
```

- **删除** `__mapper_args__` 与 `models.py:64-77` 那段为权宜之计辩护的注释
  （换成说明"主键真实存在，与 init.sql 一致"）
- ⚠️ `detail` **必须是 `JSONB`**，不是 `sqlalchemy.JSON`。
  用 `JSON` 会编译成 `JSON`（非 `JSONB`）→ 与 `init.sql` 的 `JSONB` 漂移，
  且丢失 JSONB 的索引/操作符能力。此坑已踩过，见 `models.py:8-11`。
- ⚠️ `RobotTelemetry` 的 `primary_key=True` 复合主键 `(time, robot_id)` **保持不动**

### T3 — 写入路径兼容性确认（**大概率无需改代码，但要验证**）

`alert_engine/app/store.py:19` 的 `INSERT_SQL` 若为**位置参数**写法，
`alert_id` 由 `BIGSERIAL` 自动填充，**无需改动**。
若它是**具名列**写法，则必须补上 `alert_id` 之外的列，或改为位置参数。

无论哪种：
- **必须保留** `pool.acquire()`（`:33`、`:64`）。不得改回 `pool.execute()` ——
  后者在连接处于异常状态时可能把脏连接放回池中复用，导致后续写入连环失败。
- **不得**往 `INSERT` 里显式写 `alert_id` 值。

### T4 — 加回归测试（防漂移再犯）

在 `tests/test_schema_contract.py` 增加断言（该文件已有 4 个用例，可跑）：

1. `CreateTable(RobotAlert.__table__).compile(dialect=postgresql.dialect())`
   产出的 DDL **含** `PRIMARY KEY (time, alert_id)`
2. 同上 DDL 中 `detail` 的类型是 **`JSONB`**（不是 `JSON`）
3. `RobotAlert` **不存在** `__mapper_args__` 覆盖（或覆盖为空）
4. `models.py` 中 `RobotAlert` 声明的列集合 == `init.sql:55-63` 建表的列集合
   （用正则解析两边，逐列比对，缺失/多余都报错）

> 第 4 条是关键：本单的本质就是**消灭 ORM 与 DDL 的漂移**，
> 没有这条断言，下次改表还会再漂一次。

---

## 3. 保持不动清单（**整文件照抄这些路径 = 直接判定回退**）

| 路径 | 为什么不能动 |
|---|---|
| `alert_engine/app/notifiers/*.py` | 订单 17 前置的 webhook 通知器，已接线 |
| `alert_engine/app/metrics.py` | `alerts_emitted` / `alerts_suppressed` / `robots_online` 已接线 |
| `alert_engine/app/main.py` 的 `emit()` / `ticker()` | 抑制逻辑 + 指标递增已接线 |
| `alert_engine/app/store.py` 的 `pool.acquire()` | 见 T3 |
| `backend_ingestion/app/services/batch_writer.py` | 三级降级链 L1/L2/L3 + 丢行三元组 |
| `backend_ingestion/app/services/mqtt_subscriber.py` | MQTT 计数器刚接线（订单 19 前一轮） |
| `backend_ingestion/requirements.txt` | `aiomqtt==2.5.1`、`asyncpg==0.32.0`、`prometheus-client==0.21.1` 均锁定 |
| `alert_engine/requirements.txt` | 同上 |
| `infra/init.sql:96-165`（遥测压缩/保留的幂等块） | 已改为 `DO $$` 幂等写法，重跑安全 |
| `pytest.ini` | 刚新增，用于把 `external/` 排除出测试收集 |

已知回退陷阱（本轮对账实证，**不要重犯**）：
- `fix/issue19-p0-gaps` 的 `alert_engine/app/store.py` 会**删掉** `pool.acquire()`
- `fix/issue19-merge-safe` 的 `batch_writer.py` 会**删掉整条三级降级链**
- `feat/order-07` / `feat/order-08` 的 `requirements.txt` 会把 `aiomqtt` 降回 `2.0.1`
- 多个旧分支的 `models.py` 会把 `JSONB` 换成 `JSON`

---

## 4. 完成前自检（**逐条跑，把输出贴进 PR**）

```bash
# 1. ORM 编译出的 DDL 必须含主键，且 detail 是 JSONB
python -c "
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql
from backend_ingestion.app.db.models import RobotAlert
print(CreateTable(RobotAlert.__table__).compile(dialect=postgresql.dialect()))
"

# 2. init.sql 主键声明
grep -n 'PRIMARY KEY' infra/init.sql

# 3. 全仓测试（pytest.ini 已排除 external/）
python -m pytest -q
# 期望：全部通过，且数量 >= 25（当前基线 25）

# 4. 回退护栏（每条都必须有输出）
grep -n 'JSONB'  backend_ingestion/app/db/models.py
grep -n 'pool.acquire()' alert_engine/app/store.py
grep -n '_STOP'  backend_ingestion/app/services/batch_writer.py
grep -n 'aiomqtt==2.5.1' backend_ingestion/requirements.txt
grep -n '__mapper_args__' backend_ingestion/app/db/models.py   # 期望：无输出
```

**验收硬条件**：第 1 条的 DDL 输出里必须能看到 `PRIMARY KEY (time, alert_id)`，
且与第 2 条 `init.sql` 的声明**逐字一致**。对不上就是没做完。

---

## 5. 边界（诚实标注，不要假装做了）

- 本机**无 TimescaleDB**，`create_hypertable` / 压缩 / 保留策略**无法实跑验证**。
  T1 的结构改动请保证 PG 语法正确，并在注释里标注「TimescaleDB 部分未在本机验证」。
- 已有数据的**迁移脚本不在本单范围**（仓库尚无生产部署，`init.sql` 为初始化脚本）。
  但请在 `infra/init.sql` 注释里写明：存量库需要手工 `ALTER TABLE` 补 `alert_id`，
  或说明"仅限全新初始化"。
- `ack_at` / `ack_by` 只**建字段**，不做 ack 接口 —— 那是订单 17（告警闭环）的范围。
