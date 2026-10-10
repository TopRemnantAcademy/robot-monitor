# 企业级能力差距分析（Enterprise Gap Analysis）

> 触发：issue #19 评论「现在远远不够，一个大企业需要什么样的…前后台都需要完美的接应」
> 方法：**只写能在本仓库 `文件:行号` 找到证据的差距**；每条给出「现状证据 → 企业级要求 → 直接可套用的开源 → 派单」。
> 纪律：区分【事实】（已核对代码）与【判断】（我的评估，注明依据）。未实测的标「未验证」。

---

## 0. 一句话结论

当前系统是一个**能跑通的单机 PoC**（100 台 × 10Hz 链路已闭合），
但距离**企业级可交付**还差 **4 大类、17 项**能力。
其中 6 项属「半成品」——**数据库/前端已埋了一半的坑，另一半没接上**，这类最危险。

---

## 1. 已核对的「半成品」（最优先，因为做了 50% 更误导人）

| # | 现象 | 证据（file:line） | 企业级后果 |
|---|---|---|---|
| H1 | `robot_alert.ack` 列 + 部分索引已建（但表连主键/ID 都没有，见 H7），但**全仓无写入/更新 ack 的代码**，前端也无确认按钮 | `infra/init.sql:62`（`ack BOOLEAN DEFAULT FALSE`）<br>`infra/init.sql:92`（`idx_alert_ack ... WHERE ack = FALSE`） | 值班人员无法「认领/关闭」告警 → 告警永远是流水，无法闭环 SLA |
| H2 | 告警 `severity` 已分 2/3 级，**但无升级（escalation）策略**：一直挂在原级别，无「5 分钟未确认→升级」 | `alert_engine/app/rules/*.py`（各规则只返回 severity 常量） | 严重告警被淹没，无人响应无兜底 |
| H3 | `robot_metadata` 表已建（100 台播种），**但没有任何业务代码读它做分组/负责人/工位** | `infra/init.sql:33` | 无法按产线/工位/班次过滤，100 台只能一起看 |
| H4 | 前端 `HealthSummaryHeader` 分母**写死 100** | `HealthSummaryHeader.tsx:36`（注释：在线率 = 在线数 / 100）<br>`robotStore.ts:17`（`buildRobotIds(count = 100)`） | 扩容到 200 台需改代码，无法配置化 |
| H5 | 前端 `AlertLogTable` 只有 200 条内存缓冲，**无分页/无历史查询** | `robotStore.ts:14`（`ALERT_BUFFER_LIMIT = 200`） | 交班复盘时告警已丢失，无法追溯 |
| H6 | `RobotDetailDrawer` 历史固定 `minutes=5`，**无时间范围选择器** | `backend_ingestion/app/main.py:84`（`minutes: int = 5`） | 无法查「昨天 14:00 发生了什么」 |
| H7 | `robot_alert` **无主键、无 ID 列**，无法寻址单条告警 | `infra/init.sql:55-64`（列：`time/robot_id/rule_id/severity/message/detail/ack`）；对比 `init.sql:27` 遥测表有 `PRIMARY KEY (time, robot_id)` | 告警**无法被确认/关闭**——想做 ack 都没有"哪一条"的句柄 |

> 我的判断（依据）：H1/H3 是「Schema 先行、能力未接」的典型。
> 企业采购时最怕这种——演示能过，交付即穿帮。

---

## 2. 后台（Backend）差距

### 2.1 安全与身份 —— 当前**完全没有**

**事实**：全仓搜索 `auth / jwt / rbac / token`，`backend_ingestion/app/` 与 `alert_engine/app/` **零命中**。
`GET /robots`、`WS /ws/robots`、`GET /robots/{id}/history` 全部**裸奔**（`backend_ingestion/app/main.py:78/84/168`）。

企业级要求：
- 认证（登录）
- 授权（RBAC：操作员/班长/管理员，能看什么、能确认什么）
- WS 鉴权（连接时带 token，不能靠"在内网"当安全）
- 审计（谁在什么时候确认了哪条告警）

**可直接套用的开源（已实测可 clone，见 §5）**：

| 源 | 用途 | 套用点 |
|---|---|---|
| `fastapi-users/fastapi-users` | FastAPI 原生用户/登录/JWT，**与现有 FastAPI 同栈** | 新增 `backend_ingestion/app/auth/`，护住 REST + WS |
| `casdoor/casdoor` | 企业 SSO/OIDC 一体化（含 UI） | 若要求对接公司 AD/LDAP |
| `keycloak/keycloak` | 老牌 IAM，OIDC/SAML/LDAP | 大企业标准答案 |

> 判断：**先 fastapi-users**（同栈、改动最小、能真实跑通），
> 保留 OIDC 接口以便日后换 Keycloak。不要一上来就塞 Keycloak——那是拿大炮打蚊子。

### 2.2 REST/WS 契约未机器化

**事实**：REST 有 `?minutes=5&resolution=10s`（`main.py:83-84`），WS 有 `type` 信封（`main.py:176/193/195`），
但**没有 OpenAPI 契约快照，也没有前端类型自动生成**。
前后端靠人肉对齐——已实测出过一次事故（`STATUS.md` §六第 2 条：`/history` 漏 `joint_positions` 导致 Drawer 第三条折线恒空）。

企业级要求：**契约即代码**，CI 里做 breaking-change 检测。

**可套用**：
| 源 | 用途 |
|---|---|
| `OpenAPITools/openapi-generator` | 从 FastAPI 的 `/openapi.json` 生成 TS 类型 → 前端 `types/robot.ts` 不再手写 |
| `open-telemetry`（见 2.4） | 契约/链路一起上 |

### 2.3 告警能力只是「规则判定」，缺运维闭环

**事实**：有 4 条规则 + 去重（`alert_engine/app/rules/`、`deduplicator.py`），
订单 10 正在加**抑制（inhibition）**。但仍缺：

| 缺什么 | 企业为什么需要 | 可套用 |
|---|---|---|
| 确认/关闭（runbook） | 告警必须能被"处理掉"，否则永远 noisy | 自研 + H1 的 `ack` 列 |
| 升级（escalation） | 5min 无人确认自动升级/通知上级 | Alertmanager 的 `route`/`repeat_interval` 语义 |
| 静默（silence） | 计划性维护时闭嘴 | Alertmanager `silence` |
| 通知渠道 | 大屏之外还要企业微信/钉钉/邮件 | `samber/awesome-prometheus-alerts`（规则模板库） |
| 通知去抖/合并 | 一次事故不要发 100 条 | Alertmanager `group_by` |

**可套用**：`prometheus/alertmanager`（已在 `external/`，sparse docs）——把 `inhibit/silence/route` 三件套完整对照实现，而不是只抄 inhibition。

### 2.4 可观测性 —— 当前**零**

**事实**：全仓无 metrics 端点、无结构化日志、无 trace。
`GET /healthz`（`main.py:66`）只返回存活，不返回「队列积压多少 / 落库 p99 多少」。

企业级要求（**这是"前后台完美接应"的物理基础**）：
- 后端：队列水位、落库吞吐、Redis 往返、WS 连接数
- 前端：**React 重渲染帧率、WS 消息速率、节流命中率**（AGENTS.md §3 的性能护栏目前**无法验证**）
- 全链路：MQTT→后端→DB 的端到端延迟

**可套用**：
| 源 | 用途 | 本项目已有基础 |
|---|---|---|
| `prometheus/client_python` | 后端 `/metrics` | 已下载（`INTEGRATION-PLAN.md` I-4） |
| `open-telemetry/opentelemetry-python` | 后端 trace | 可直接接 FastAPI 中间件 |
| `open-telemetry/opentelemetry-js` | **前端 trace + 重渲染可观测** | 前端零基础，本单核心 |
| `openobserve/openobserve` / `grafana/loki` | 日志聚合（自托管，轻量） | 当前日志只有 stdout |

> 判断：前端可观测是**被忽略最狠的一环**。AGENTS.md 明令「严禁每条 WS 消息触发重渲染」，
> 但**没有任何代码能证明它没发生**。企业验收会直接问这个。

### 2.5 数据层

**事实**：
- 超表 + 连续聚合已建（`infra/init.sql:30/65/70`）
- **无保留策略（retention）**：遥测按 1000 行/秒累积，无 TTL 会撑爆磁盘
- **无压缩策略（compression）**：TimescaleDB 的杀手锏没用上
- **无迁移工具**：改 Schema 靠手改 `init.sql`
- `asyncpg==0.29.0` 仍锁旧版（`backend_ingestion/requirements.txt:5`，订单 08 处理中）

**可套用**：`sqlalchemy/alembic`（已下载）；保留/压缩策略属 TimescaleDB 内建 SQL，无需外部源。

---

## 3. 前台（Frontend）差距

### 3.1 只有「一块大屏」，没有企业前台的骨架

**事实**：`frontend_dashboard/src/` 共 13 个文件，无路由（无 `react-router`）、
无登录页、无权限控制、无布局框架。
`App.tsx` 直接把三个组件纵向堆叠。

企业级要求：
| 能力 | 现状 | 可套用 |
|---|---|---|
| 路由/多页（大屏/告警中心/机器人档案/报表） | ❌ 单页堆叠 | `remix-run/react-router` 或 `tanstack/router` |
| 服务端状态缓存（历史查询/分页/重试） | ❌ 裸 `fetch` | `tanstack/query` |
| 表单与校验（阈值配置、机器人元数据） | ❌ 无 | `react-hook-form` |
| 组件库（表格/日期选择/弹窗/下拉） | ❌ 全手写 | `shadcn-ui` 或 `ant-design`（**工业看板场景 antd 更省事**） |
| 国际化（中外合资工厂常有） | ❌ 硬编码中文 | `i18next` |

### 3.2 「完美接应」缺的具体接口

把后台能力映射到前台，**每个后台缺口都对应一个前台缺口**：

| 后台能力 | 前台应有 | 现状 |
|---|---|---|
| RBAC | 按角色隐藏/禁用按钮 | ❌ |
| `ack` 列（H1） | 告警「确认/认领」按钮 + 已确认态 | ❌ |
| 升级策略（H2） | 升级标记 + 显著色 | ❌ |
| `robot_metadata`（H3） | 按产线/工位/班次筛选器 | ❌ |
| 历史任意区间（H6） | 时间选择器 + 缩放 | ❌ |
| 告警历史（H5） | 分页表 + 导出 | ❌ |
| 保留策略 | 「数据仅保留 N 天」提示 | ❌ |
| 可观测性 | 大屏自身健康（WS 速率/掉线） | 仅 WS 红绿点（`HealthSummaryHeader`） |

### 3.3 前端质量工程 —— 当前有目录但未成体系

**事实**：`package.json` 有 `puppeteer`/`jsdom`，`orders/09` 提到 `.e2e/`，
但 **`src/` 下无任何单测文件**，`package.json` 无 `test` 脚本。

企业级要求：单测 + 组件测试 + E2E + 视觉回归（大屏最怕"改一处歪一片"）。
**可套用**：
| 源 | 用途 |
|---|---|
| `testing-library/react-testing-library` | 组件测试 |
| `mswjs/msw` | mock WS/REST（**大屏离线开发必备**） |
| `microsoft/playwright` | E2E + 截图对比（比 puppeteer 更适合回归） |
| `vitest-dev/vitest` | 与 Vite 同栈的测试运行器 |

---

## 4. 交付与运维（企业采购必查）

**事实**：
- `infra/docker-compose.yml` 有 3 个服务 + healthcheck，但**无资源限制（`deploy.limits`）、无 restart 策略**
- 无 CI 部署产物、无环境分级（dev/staging/prod）
- 单机单点，无 HA

| 缺什么 | 企业要求 | 可套用 |
|---|---|---|
| 容器编排 | K8s/Helm 或至少 compose prod profile | `helm/helm`（如需） |
| 反向代理 + TLS | Nginx/Traefik + HTTPS | `traefik/traefik` |
| 密钥管理 | 不用 `.env` 明文，用 Vault/K8s Secret | `hashicorp/vault` |
| 备份/恢复 | TimescaleDB 定期 dump + PITR | 属 DB 内建能力 |
| 值班/通知 | 与企业微信/钉钉/Slack 对接 | Alertmanager receiver |

---

## 5. 可下载开源清单（**均已实测 `git ls-remote` 可达**）

> 实测时间：本轮。命令：`git ls-remote --heads https://github.com/<repo>.git`，逐条返回 refs = 可达。

| # | 仓库 | 可达 | 对应缺口 | 优先级 |
|---|---|---|---|---|
| E1 | `fastapi-users/fastapi-users` | ✅ | 2.1 认证/授权 | **P0** |
| E2 | `open-telemetry/opentelemetry-python` | ✅ | 2.4 后端可观测 | **P0** |
| E3 | `open-telemetry/opentelemetry-js` | ✅ | 2.4 前端可观测 | **P0** |
| E4 | `prometheus/alertmanager` | ✅（已有） | 2.3 silence/route/escalation | **P0** |
| E5 | `samber/awesome-prometheus-alerts` | ✅ | 2.3 规则模板库 | P1 |
| E6 | `OpenAPITools/openapi-generator` | ✅ | 2.2 契约即代码 | **P0** |
| E7 | `openobserve/openobserve` | ✅ | 2.4 日志聚合（轻量自托管） | P1 |
| E8 | `grafana/loki` | ✅ | 2.4 日志聚合（备选） | P2 |
| E9 | `casdoor/casdoor` | ✅ | 2.1 企业 SSO | P2 |
| E10 | `keycloak/keycloak` | ✅ | 2.1 企业 IAM | P2 |
| E11 | `microsoft/playwright` | ✅ | 3.3 E2E + 视觉回归 | P1 |
| E12 | `tanstack/query` | ✅ | 3.1 服务端状态 | P1 |
| E13 | `mswjs/msw` | ✅ | 3.3 mock | P1 |
| E14 | `prometheus/blackbox_exporter` | ✅ | 4 外部可用性探针 | P2 |
| E15 | `victoriametrics/victoria-metrics` | ❌ 本次不可达 | —（降级：用 Prometheus 本体） | — |
| E16 | `apache/apisix` | ✅ | 4 网关/限流 | P2 |
| E17 | `temporalio/temporal` | ✅ | 2.3 告警工作流（重编排用途） | P3 |
| E18 | `camunda/camunda` | ✅ | 4 检修工单流程 | P3 |

> **诚实标注**：`victoriametrics/victoria-metrics` 本次 `ls-remote` 失败（可能瞬时网络），
> 未验证其可达性，故不列为推荐项。其余 17 条均为本轮实测返回 refs。

---

## 6. 派单建议（按优先级，可直接执行）

| 单号 | 名称 | 对应缺口 | 判据（可验证） |
|---|---|---|---|
| 11 | **认证与 RBAC 落地** | 2.1 / 3.2 | `fastapi-users` 接入；`/robots` 未带 token 返回 401；WS 连接鉴权；操作员/管理员两角色 |
| 12 | **可观测性三件套** | 2.4 / 3.2 | 后端 `/metrics`（队列水位/落库 p99）；前端上报重渲染次数与 WS 速率；OTel trace 贯通 MQTT→DB |
| 13 | **告警闭环**（ack/escalation/silence） | H1/H2/**H7**/2.3 | 先补 `robot_alert` 主键/ID 列（blocker）；`ack` 被真实写入；前端确认按钮；未确认 5min 升级；静默生效 |
| 14 | **契约即代码** | 2.2 | CI 生成 TS 类型；`/history` 字段变更能触发类型报错 |
| 15 | **企业前台骨架** | 3.1 / H3/H5/H6 | 路由 4 页；按产线筛选；告警分页导出；历史时间选择器 |
| 16 | **数据生命周期** | 2.5 | retention + compression 策略落地；alembic 迁移基线 |
| 17 | **前端质量工程** | 3.3 | vitest 单测 + playwright E2E + 视觉回归截图基线 |

---

## 7. 我的判断（非事实，注明依据）

1. **最大风险不是"功能少"，而是"半成品多"**（§1 的 6 项）。
   依据：`ack` 列 + 索引已建但无代码，`robot_metadata` 已播种但无消费方。
   这类"看起来有"的东西在企业验收时比"明确没有"更致命。
2. **"前后台完美接应"的关键是第 2.4 节的可观测性**，不是再多加几个页面。
   依据：AGENTS.md §3 设了硬性性能护栏（100ms 节流），但当前**无任何手段验证**——
   等于没有护栏。没有可观测性的"接应"是口号。
3. **认证要自己上（fastapi-users），IAM 大件留接口别急上**。
   依据：本项目是 100 台单厂场景，Keycloak 的运维成本远超收益；
   但若客户是大企业集团，SSO 是准入条件，所以**接口必须预留**。
4. **前端应尽快从"手写一切"转向"组件库 + 状态缓存"**。
   依据：`src/` 只有 13 个文件就已出现「Drawer 第三条折线恒空」这类跨端契约事故；
   规模再大，手写成本与缺陷率会非线性上升。
5. **订单 13 的隐藏 blocker 是表结构，不是业务逻辑**。
   依据：`robot_alert` 无主键无 ID（`infra/init.sql:55-64`），
   而 `ack` 列和它的部分索引却已建好 —— 说明当初设计时想过这件事，但**只做了一半就停了**。
   这类"半成品"必须优先补齐，因为演示时会直接暴露。

---

## 8. 未验证清单（诚实标注）

| 项 | 原因 | 需要什么才能验证 |
|---|---|---|
| 上述 17 个仓库的**具体文件路径/版本** | 本轮只做 `ls-remote` 可达性核对，未 clone 内容 | `git clone --depth 1` 后核对 |
| `fastapi-users` 与 FastAPI 0.110 的兼容性 | 未安装测试 | 本地 venv 实装 |
| OTel JS 在前端 100ms 节流场景的开销 | 未压测 | 需真实构建 + 压测 |
| TimescaleDB retention/compression 对本项目 chunk 的影响 | 未在真实 DB 执行 | 需 Docker 起 TimescaleDB |
| 前端视觉回归基线 | 未跑 playwright | 需浏览器环境 |

---

*本文所有 file:line 均可逐条核对；未核对的项已在 §8 明确标注。*
