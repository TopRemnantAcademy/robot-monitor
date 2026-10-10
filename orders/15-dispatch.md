# 派工单：订单 15 — 认证与 RBAC（企业级准入门槛）

> 依据：`docs/design/ENTERPRISE-GAP-ANALYSIS.md` §2.1 / §3.2（缺口 E1）
> 触发方式：`post-issue-comment`（本文件不含触发词）

---

## 0. 现状（已核对，可直接复现）

> **行号已于 2026-10-11 重新核对**（本单初稿写于 `/metrics` 接线之前，行号已整体后移）。

| 事实 | 证据（2026-10-11 复验） |
|---|---|
| 全仓 `auth/jwt/rbac` 零命中 | `grep -rn "auth\|jwt\|rbac" backend_ingestion/app/ alert_engine/app/ --include=*.py` → 无输出 |
| REST 裸奔 | `backend_ingestion/app/main.py:132`（`GET /robots`）、`:137`（`GET /robots/{robot_id}/history`） |
| WS 裸奔 | `backend_ingestion/app/main.py:226`（`@app.websocket("/ws/robots")` 第 228 行直接 `accept()`，鉴权在发快照**之前**就要做完） |
| 前端无登录/无权限 | `frontend_dashboard/src/` 无 auth 目录；全仓仅 **1 处** REST 调用（`components/RobotDetailDrawer.tsx:66`）、**1 处** WS 连接（`hooks/useRobotWebSocket.ts:129`） |
| ⚠️ `/metrics` 已接线并实跑 | `backend_ingestion/app/main.py:115`；Prometheus 靠 **pull** 抓取 |

> **本单最大的新约束**：`/metrics`（`:115`）**必须保持免鉴权**。
> 本单初稿写作时 `/metrics` 还不存在，T1 只写了"`/healthz` 保持免鉴权"——
> 若按原文照做，Prometheus 会抓不到任何指标，**刚接线的可观测性整套失效**。
> 免鉴权白名单 = `/healthz`（探针）+ `/metrics`（Prometheus 拉取）。
> 另：`alert_engine` 的指标是独立 HTTP 服务（`metrics_port=8001`，见其 `config.py`），
> 不经过 FastAPI，**无需**在本单处理。

---

## 1. 任务

### T1 — 后端认证（`fastapi-users`，同栈）
- 新增 `backend_ingestion/app/auth/`：
  - `users.py`：User 模型（接现有 asyncpg/SQLAlchemy 会话，**不要引入第二套 DB 连接**）
  - `backend.py`：JWT 策略（密钥从 `config.py` 读，**禁止硬编码**）
  - `router.py`：`POST /auth/register`、`POST /auth/jwt/login`、`POST /auth/jwt/logout`、`GET /users/me`
- 用 FastAPI `Depends` 护住 `GET /robots`、`GET /robots/{id}/history`
- **免鉴权白名单（两个都别漏）**：
  - `/healthz`（`:67`，存活探针）
  - `/metrics`（`:115`，Prometheus **pull** 模型，加鉴权 = 监控静默失效）

### T2 — WS 鉴权
`/ws/robots` 支持 query 参数 `?token=<JWT>`：
- 无 token / token 非法 → 以 WS close code `1008`（policy violation）关闭，**不得先发 snapshot**
- 校验通过后行为与现在完全一致（三分支 snapshot/telemetry/alert，见 `main.py:176/193/195`）

### T3 — RBAC 两级
在 JWT claims 里带 `role`，实现两个角色：

| 角色 | 权限 |
|---|---|
| `viewer`（操作员） | 只读：`/robots`、`/history`、`/ws/robots` |
| `admin`（管理员） | 上述 + **订单 17** 的告警确认/静默接口（原稿写「订单 13」，已按换号表更正：原 13 → 现 **17**，见 `orders/_collided/README.md`） |

`role` 用 `config.py` 中的常量，禁止散落字符串。

### T4 — 前端接应
- 登录页（本单可先在 `App.tsx` 前置一个 gate）
- token 存 `sessionStorage`（**禁止 localStorage**，大屏是公共终端）
- **REST**：`components/RobotDetailDrawer.tsx:66` 的 `fetch(url, { signal })`
  必须加 `Authorization: Bearer <token>` 头。**这是全仓唯一的 REST 调用点，改漏就是全漏。**
- **WS**：`hooks/useRobotWebSocket.ts:129` 的 `new WebSocket(url)` 拼接 `?token=`
- 401 时清 token 并回到登录态
- 按 `role` 隐藏管理员按钮（本单先留 hook，按钮在**订单 17** 落地）

### T5 — 审计（**列名已由订单 19 落地，不要另起炉灶**）

订单 19 已把 `robot_alert` 的审计列建好，**直接用现成的，不要新增列**：

| 列 | 位置 | 由谁写 |
|---|---|---|
| `ack BOOLEAN NOT NULL DEFAULT FALSE` | `infra/init.sql:82` | 订单 17 的 ack 接口 |
| `ack_at TIMESTAMPTZ` | `infra/init.sql:83` | 同上 |
| `ack_by TEXT` | `infra/init.sql:84` | **本单的 JWT `sub`** |

> 原稿写的列名是 `operator` —— **该列不存在**。
> 订单 19 采用的命名是 `ack_by`，与 Alertmanager/Grafana Oncall 生态一致，沿用即可。
> 取值必须来自 JWT `sub`，**禁止**由前端传入操作人（否则可伪造审计记录）。
> 本单的交付物是"取得到可信 `sub`"，订单 17 负责把它写进 `ack_by`。

---

## 2. 验收标准

- [ ] 未带 token 请求 `GET /robots` → **401**
- [ ] 合法 token → 200，返回体与改动前**逐字段一致**
- [ ] `/ws/robots` 无 token → 被拒（close 1008），**未收到 snapshot 帧**
- [ ] 合法 token → 正常收到 snapshot/telemetry/alert
- [ ] `viewer` 调 admin 接口 → **403**
- [ ] `/healthz` 免鉴权仍 200
- [ ] ⚠️ `/metrics` 免鉴权仍 200 且 `content_type=text/plain; version=0.0.4`
  （Prometheus 抓不到 = 可观测性整套失效；**必须实跑 `curl` 取证，不能只看代码**）
- [ ] `curl -i /robots` 无 `Authorization` 头 → 401（不是 403、不是 500）
- [ ] 密钥不硬编码（`.env.example` 增加 `JWT_SECRET`，`.gitignore` 已排除 `.env`）
- [ ] `tsc --noEmit` 0 错误；`vite build` RC=0
- [ ] **实际运行测试**并贴输出

## 3. 禁止项

1. **禁止**硬编码 JWT 密钥或默认口令进代码/镜像（AGENTS.md §5）
2. **禁止**改 WS 信封结构（三分支与字段名不变）
3. **禁止**引入第二套 DB 连接或第二套 ORM 会话
4. **禁止**为了省事把 `/healthz` 也护起来（会打挂探针）
5. **禁止**伪造测试结果；跑不通写「未验证 + 所需条件」
6. **禁止**只推分支就宣称完成 —— 尝试 push + 开 PR；因 `repo-code:rw` 失败则**原文报告错误**

## 4. 依赖与冲突（**编号已按换号表更正**）

| 原稿写法 | 实际对应 | 现状 |
|---|---|---|
| 订单 12（可观测性） | **订单 16** | ✅ **已完成**（`/metrics` 双服务接线 + observability 配置）。冲突面消失，但本单**必须守住**它产出的 `/metrics` 免鉴权 |
| 订单 13（告警闭环） | **订单 17** | ⏳ 待派。共享 RBAC：`admin` 角色名必须与本单一致 |

- `fastapi-users` 版本需与 `fastapi==0.110.0` 兼容（`backend_ingestion/requirements.txt:1`，已复验）。
  装不上就报告实际错误，**禁止**为了装上而升级 `fastapi`（会动到已验证的 WS/路由行为）。
- **本单是订单 17 的前置**：订单 17 的 `POST /alerts/{alert_id}/ack` 要求 `ack_by` 取自 JWT `sub`、
  禁止前端传入。没有本单的认证，订单 17 要么开天窗（任何人可确认告警），要么无法落地。
  故**先 15 后 17**。

## 5. 本轮补充的禁止项（原稿未覆盖）

8. **禁止**给 `/metrics` 加鉴权（Prometheus 是 pull 模型，加了等于监控静默失效）
9. **禁止**为了通过测试把 `/robots` 的 401 放宽成 200
10. **禁止**把 token 放进 URL query 传给 REST 端点（只 WS 允许 `?token=`，
    因为浏览器 `WebSocket` API 无法自定义请求头；REST 必须走 `Authorization` 头，
    否则 token 会进访问日志）
