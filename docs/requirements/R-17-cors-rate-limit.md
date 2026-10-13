# R-17 — 后端 CORS 配置 + 可选限流（不同源部署 / 滥用防护）

> **@//codebuddy/** — 本需求由本地（主代理）撰写，指派 codebuddy NPC 读仓执行。
> 本地**只写需求规格、不写业务修复代码**；codebuddy 落地后主代理按两点 diff 白名单核验 + CI 双绿回收。
>
> 状态：**待执行（生产化加固，低风险）**。实证见 §1，非推测。
> ⚠️ **安全默认必须是"关闭/最小暴露"**：CORS 不得默认 `allow_origins=["*"]`，限流不得破坏 e2e-30k 真绿。

---

## 1. 背景（file:line 实证）

- `backend_ingestion/app/main.py` 全文 grep `cors|CORSMiddleware|rate.?limit|throttle|slowapi|add_middleware` → **0 命中**（本轮实证，非推测）。
- 现有防护只有 JWT 鉴权（`backend_ingestion/app/auth/`，WS 亦鉴权 `main.py` 的 `authenticate_ws_token`）。
- `infra/docker-compose.app.yml:31` 与 `:59` 显示 backend 与前端/内部服务同在 compose 网络；`DATABASE_URL` 走服务名 `timescaledb`。

**两类真实风险**：
1. **CORS**：若前端与后端**不同源**部署（如前端独立域名/CDN、或本地联调 `:5173` 访问 `:8000`），浏览器会拦掉 REST 与 WS 握手。**当前无任何 CORS 中间件** → 跨域调用直接失败，且报错发生在浏览器侧，排查成本高。
2. **限流**：对外暴露的接口**无速率限制**。当前部署在内部可信网络（`compose` 内网）尚可接受，但一旦对外暴露（含鉴权接口），缺乏暴力破解/刷接口的基础防护。

---

## 2. 目标

- 加 **`CORSMiddleware`**，来源由环境变量配置（如 `CORS_ALLOW_ORIGINS`，逗号分隔），**默认不开启**（空=不加中间件或仅同源），避免"默认放行所有来源"的安全回退；
- 加**可选限流**：可通过环境变量开关与配置阈值；**默认关闭或足够宽松**，确保**不破坏既有 e2e**（见 §6 硬门槛）；
- 两者都不得改变既有路由契约与鉴权行为（R-03 §4 契约）。

---

## 3. 范围边界（明确不做）

- **只加中间件**，不改路由处理函数、不改 Pydantic 契约、不改鉴权逻辑；
- **不改** `/healthz`、`/metrics` 的既有响应（它们被 CI/e2e/监控依赖，见 §6 硬门槛）；
- **不引入** 重量级网关（不用 Kong/Nginx 配置，仅在 ASGI 层加中间件）；若需新依赖（如 `slowapi`），须**显式说明并评估体积/维护成本**，优先用标准库或现有栈自研；
- **不做** 鉴权/用户体系改造；
- 不改前端。

---

## 4. 任务分解

### T1 — CORS（`backend_ingestion/app/main.py`）
- 按 env `CORS_ALLOW_ORIGINS`（逗号分隔）配置 `allow_origins`；**默认值为空** → 不启用 CORS 中间件（或仅允许同源），**绝不默认 `*`**。
- `allow_credentials`、`allow_methods`、`allow_headers` 取**最小可用集**，并在注释写明"为什么不能默认 `*`"（配合凭据时会放大 CSRF/信息泄露面）。
- 支持 `CORS_ALLOW_ORIGINS=*` 的**显式**配置路径（用户明确要求时才放行），并在注释/文档中标注其风险。

### T2 — 限流（可选，默认关/宽松）
- 通过 env 开关（如 `RATE_LIMIT_ENABLED=false` 默认关闭）与阈值（`RATE_LIMIT_PER_MINUTE`）。
- 作用范围**仅限对外 REST**；**必须排除** `/healthz`、`/metrics`、内部写入路径与 WS 握手（见 §6 硬门槛）。
- 超限返回 `429`，响应体格式与既有错误响应风格一致。

### T3 — 配置文档
- 在 `docs/` 或 `.env.example` 中补两行说明（变量名、默认值、取值范围、风险提示）。

### T4 — 回归测试
- 单测：默认无 `CORS_ALLOW_ORIGINS` → 不带 `Access-Control-Allow-Origin` 头（证明默认不放宽）；
- 单测：配置指定 origin → 预检 `OPTIONS` 返回正确 `Access-Control-Allow-Origin`/`Allow-Methods`；
- 单测（若实现限流）：默认关闭不生效；开启后超限返 `429`，且 `/healthz`、`/metrics` **不受限**（关键）；
- 既有 `backend_ingestion` 单测全绿。

---

## 5. 验收标准（可判定）

| # | 用例 | 期望 | 判定依据 |
|---|---|---|---|
| V1 | CORS 默认安全 | 未配置 `CORS_ALLOW_ORIGINS` → 响应**无** `Access-Control-Allow-Origin`（默认不放宽） | 新单测 |
| V2 | CORS 可配置 | 配置指定 origin → `OPTIONS` 预检返正确头 | 新单测 |
| V3 | 限流默认不影响 | 限流默认关闭（或足够宽松），`/healthz`、`/metrics` **永不被限** | 新单测 + 读代码 |
| V4 | e2e 不被破坏（硬门槛） | `e2e-30k-landing` 仍真绿（`rows_written≥30000`、无 `【E2E SKIPPED】`）；**限流不得拦掉落库/探活路径** | `cnb pulls list-pull-commit-statuses` + 按需 `get-build-logs` |
| V5 | 契约/鉴权不变 | `/healthz`、`/robots`、`/history`、`/ws/robots` 响应契约与鉴权行为逐字不变 | 既有单测 + CI |
| V6 | CI 双绿 | `pipeline-1(test)` + `pipeline-2(e2e-30k-landing)` 均 success | `cnb pulls list-pull-commit-statuses` |

---

## 6. 约束 / 禁止项（白名单 + 硬门槛）

- `git diff` 白名单：`backend_ingestion/app/main.py`（仅中间件挂载）± 新增中间件模块 ± `.env.example`/`docs` 配置说明 ± `backend_ingestion` 测试文件。
- **硬门槛（CI 不得红）**：`e2e-30k-landing` 必须仍真绿（`rows_written≥30000` 且无 `【E2E SKIPPED】`）。**限流绝不能拦掉落库链路、`/healthz` 探活或 `/metrics`** —— 一旦 e2e 因限流变红，本需求直接判 FAIL。
- 禁止默认 `allow_origins=["*"]`（安全回退，须显式配置才放行）。
- 禁止改动路由契约、Pydantic 模型、鉴权逻辑（R-03 §4 硬红线）。
- 禁止引入重量级依赖；如需新包，必须在 PR 描述说明理由与体量。
- 不得夹带 `alert_engine` / 前端 / `.cnb.yml` / `infra/init.sql` 改动。

---

## 7. 交付物

- `backend_ingestion/app/main.py`：CORS 中间件（env 驱动、默认关闭）+ 可选限流（默认关闭、排除 `/healthz` 与 `/metrics`）。
- 配置说明（`.env.example` 或 `docs/`）。
- 回归测试：默认安全、可配置、限流不影响关键路径。
- PR 的 `git diff` 白名单如上；**CI 双绿（尤以 e2e 真绿为硬门槛）**。

## 8. 关联

- 来源：`docs/GAP-SCAN-2026-10-13.md` **GAP-4**（LOW）。
- 相关：`R-03-backend-api-contract.md`（契约不得变）；`infra/docker-compose.app.yml`（部署拓扑）。
