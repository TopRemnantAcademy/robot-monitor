# 订单 17 告警闭环 —— 实测证据

环境：本机 PostgreSQL **15.19**（Debian）+ Redis；TimescaleDB **无**该扩展，
故 init.sql 里 TimescaleDB 专有语句用 shim（`create_hypertable` / `add_*_policy` /
`timescaledb.continuous` / `SET (timescaledb.compress)`）顶替，**其余 DDL 逐字执行**。
`SELECT * FROM pg_indexes WHERE tablename='robot_alert'` 与 `\d robot_alert` 的
结果由真 PG 产出（见下 V1）。

## 1. 单元/回归测试

```
$ python -m pytest tests/ alert_engine/tests/ -q
37 passed

$ cd backend_ingestion && TEST_DATABASE_URL=... python -m pytest tests/ -q
22 passed
```

## 2. 闭环集成验证（真 PG + 真 Redis）verify_alert_closure.py

```
通过 36 / 失败 0
```

覆盖 V1~V7，逐条对应派工单 §2 验收标准，关键片段：

- V1 schema：`robot_alert` 含 `escalated boolean NOT NULL DEFAULT false`；
  索引 `robot_alert_pkey(time,alert_id)` / `idx_alert_ack` / `idx_alert_escalation` 存在。
- V2 ack：
  `SELECT alert_id, ack, ack_at, ack_by, escalated FROM robot_alert WHERE alert_id=3;`
  → `{'alert_id': 3, 'ack': True, 'ack_at': 2026-10-10 23:50:12+00, 'ack_by': 'ops@example.com', 'escalated': False}`
- V3 幂等：第二次 ack `changed=False`，`ack_by` 仍为首次确认人，行数不变。
- V4 unack：`ack=FALSE`、`ack_by/ack_at` 清空；重复 unack `changed=False`。
- V5 分页：`ack=false` 时 `total=2`；`limit=1&offset=0/1` 返回不同行；排序 `time DESC, alert_id DESC`。
- V6 escalation：6 分钟前的 sev3 未确认被 SCAN 命中、`escalated` 变 TRUE；
  已确认/未超时的不命中；已升级的不重复命中；重复 MARK 幂等。
- V7 silence（真 Redis TTL）：静默期命中、`ttl=1`、到期自动恢复；
  全局 Key 静默任意机器人；**静默期内 dedup Key 未被写入**（证明 silence 先返回）。

## 3. escalation 端到端（真跑 escalate_once）

```
$ escalate_once 升级条数: 1
alert ESCALATED: humanoid-050 v17-e2e sev=3 alert_id=108 (未确认超过 300s, repeat=1)
DB 状态: {'ack': False, 'escalated': True}
PUBLISH 收到: {'robot_id': 'humanoid-050', 'rule_id': 'v17-e2e', 'severity': 3,
  'message': 'e2e old', 'detail': {'k': 1}, 'ts': ..., 'escalated': True, 'alert_id': 108}
第二轮升级条数（应为 0）: 0
```

信封字段 `robot_id/rule_id/severity/message/detail/ts` 完整，仅新增数据字段
`escalated`/`alert_id`，**未新增信封 type**（禁止项 3）。

## 4. emit() 顺序端到端（真 Redis）

```
A 无静默: saved=1  dedup Key=1
B 静默中: saved=0  dedup Key=None   ← 静默命中即 return，inhibition/dedup 均未被触达
C 静默解除: saved=1  dedup Key=1
```

## 5. 前端

```
$ ./node_modules/.bin/tsc --noEmit   → 0 错误
$ ./node_modules/.bin/vite build     → RC=0（built in 3.25s）
```

## 未验证

- TimescaleDB 专有语句（`create_hypertable` / 连续聚合 / 压缩 / 保留策略）本机无扩展，
  仍维持订单 19 的「未验证」状态，本单未改动它们。
- 浏览器内真实点击确认按钮的交互（jsdom 未接）：本单只做 `tsc`/`vite build` 静态与构建验证。
