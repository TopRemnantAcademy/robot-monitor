# 派工单：订单 15 — 认证与 RBAC（企业级准入门槛）

> 依据：`docs/design/ENTERPRISE-GAP-ANALYSIS.md` §2.1 / §3.2（缺口 E1）
> 触发方式：`post-issue-comment`（本文件不含触发词）

---

## 0. 现状（已核对，可直接复现）

| 事实 | 证据 |
|---|---|
| 全仓 `auth/jwt/rbac/token` 零命中 | `grep -rn "auth\|jwt\|rbac\|token" backend_ingestion/app/ alert_engine/app/` → 无输出 |
| REST 裸奔 | `backend_ingestion/app/main.py:78`（`GET /robots`）、`:84`（`GET /robots/{id}/history`） |
| WS 裸奔 | `backend_ingestion/app/main.py:168`（`@app.websocket("/ws/robots")` 直接 `accept()`，无鉴权） |
| 前端无登录/无权限 | `frontend_dashboard/src/` 13 个文件，无 auth 相关 |

---

## 1. 任务

### T1 — 后端认证（`fastapi-users`，同栈）
- 新增 `backend_ingestion/app/auth/`：
  - `users.py`：User 模型（接现有 asyncpg/SQLAlchemy 会话，**不要引入第二套 DB 连接**）
  - `backend.py`：JWT 策略（密钥从 `config.py` 读，**禁止硬编码**）
  - `router.py`：`POST /auth/register`、`POST /auth/jwt/login`、`POST /auth/jwt/logout`、`GET /users/me`
- 用 FastAPI `Depends` 护住 `GET /robots`、`GET /robots/{id}/history`
- `/healthz` **保持免鉴权**（探针用）

### T2 — WS 鉴权
`/ws/robots` 支持 query 参数 `?token=<JWT>`：
- 无 token / token 非法 → 以 WS close code `1008`（policy violation）关闭，**不得先发 snapshot**
- 校验通过后行为与现在完全一致（三分支 snapshot/telemetry/alert，见 `main.py:176/193/195`）

### T3 — RBAC 两级
在 JWT claims 里带 `role`，实现两个角色：

| 角色 | 权限 |
|---|---|
| `viewer`（操作员） | 只读：`/robots`、`/history`、`/ws/robots` |
| `admin`（管理员） | 上述 + 订单 13 的告警确认/静默接口 |

`role` 用 `config.py` 中的常量，禁止散落字符串。

### T4 — 前端接应
- 登录页（新增路由，见订单 15；本单可先在 `App.tsx` 前置一个 gate）
- token 存 `sessionStorage`（**禁止 localStorage**，大屏是公共终端）
- `useRobotWebSocket` 连接时拼接 `?token=`
- 401 时清 token 并回到登录态
- 按 `role` 隐藏管理员按钮（本单先留 hook，按钮在订单 13 落地）

### T5 — 审计
`robot_alert` 的写操作（订单 13 的 ack）必须落 `operator`（从 JWT `sub` 取），
禁止由前端传入操作人。

---

## 2. 验收标准

- [ ] 未带 token 请求 `GET /robots` → **401**
- [ ] 合法 token → 200，返回体与改动前**逐字段一致**
- [ ] `/ws/robots` 无 token → 被拒（close 1008），**未收到 snapshot 帧**
- [ ] 合法 token → 正常收到 snapshot/telemetry/alert
- [ ] `viewer` 调 admin 接口 → **403**
- [ ] `/healthz` 免鉴权仍 200
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

## 4. 依赖与冲突

- 与订单 12（可观测性）**会同时改 `backend_ingestion/app/main.py`** → 本单负责 `Depends` 注入，订单 12 负责中间件，合并时两者都要保留
- 与订单 13（告警闭环）共享 RBAC：`admin` 名称必须一致
- `fastapi-users` 版本需与 `fastapi==0.110.0` 兼容（`backend_ingestion/requirements.txt`），装不上就报告实际错误
