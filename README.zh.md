# RobotMonitor — 100 台人形机器人工厂实时监控系统

RobotMonitor 是一个开源运维看板，实时监视工厂车间的 **100 台人形机器人**：在线/离线状态、电量、电机与关节温度、扭矩、故障码，以及实时告警。

高频遥测（100 台 × 10Hz = 1000 条/秒）经 MQTT 传输，由 FastAPI 采集服务**批量写入 TimescaleDB**，将最新状态缓存在 **Redis**（Hash + TTL + Pub/Sub）中，并通过 **WebSocket** 推送到 React 看板。告警引擎订阅数据流，执行规则评估，并将告警送入 UI 告警流。

> 🌍 **本项目国际化。** 文档提供以下语言版本：
> - 🇺🇸 [English](./README.md)
> - 🇰🇷 [한국어](./README.ko.md)
> - 🇨🇳 **中文**（本文件）
>
> 看板界面本身支持 **English / 한국어 / 中文**，并带有实时语言切换器（默认：English）。详见 [docs/README.md](./docs/README.md)。

---

## 架构

```
机器人集群(Edge) --MQTT--> [backend_ingestion] --批量--> TimescaleDB
                            |-- Redis (Hash + TTL) + Pub/Sub
[alert_engine]   --订阅数据流--> 规则评估 --> robot_alert + UI 告警流
[frontend_dashboard] <--WebSocket /ws/robots-- [backend_ingestion]
[simulator]      --MQTT--> 模拟 100 台 @10Hz + 异常注入（压测用）
```

| 路径 | 说明 |
| :--- | :--- |
| `spec/` | 6 份设计规范（00 架构 / 01 后端 / 02 告警 / 03 前端 / 04 模拟器 / 05 剧本） |
| `infra/` | `docker-compose.yml`（EMQX + TimescaleDB + Redis）、`init.sql`（超表 + 连续聚合） |
| `backend_ingestion/` | FastAPI 采集存储服务（提供 `/ws/robots` 与 REST） |
| `alert_engine/` | 告警/健康规则引擎 |
| `frontend_dashboard/` | React 看板（Vite + ECharts + TanStack Query + zustand） |
| `simulator/` | 100 台遥测模拟器（含异常注入） |
| `test_integration.sh` | 端到端联调脚本 |

---

## 快速开始（全栈，Docker）

```bash
docker compose -f infra/docker-compose.yml up -d   # 启动中间件（EMQX、TimescaleDB、Redis）
bash test_integration.sh                            # 端到端联调（需先构建各模块）
```

随后打开看板开发服务器：

```bash
cd frontend_dashboard
npm install
npm run dev        # Vite 开发服务器 http://localhost:5173
```

---

## 快速开始（仅前端 — 无需 Docker）

若环境中无法运行 Docker，看板可借助**模拟后端**在无需任何基础设施的情况下运行，该后端会模拟 WebSocket + REST 契约：

```bash
cd frontend_dashboard
npm install
node mock_backend.mjs            # 模拟 WS + REST 服务器 :8787
npm run dev                      # Vite 开发服务器 :5173（代理到 :8787）
```

模拟后端是**前端贡献者**以及无 Docker 机器上的 CI 本地测试推荐路径。详见 [docs/developer-guide.md](./docs/developer-guide.md)。

---

## 国际化（i18n）

看板提供 English（默认）、한국어、中文三种语言。UI 文案集中在 [`frontend_dashboard/src/i18n/locales.ts`](./frontend_dashboard/src/i18n/locales.ts)，通过 `useI18n()` 钩子解析，顶部页眉内置语言切换器。**服务端下发的数据**（告警消息、机器人名称）刻意不做翻译。

新增语言：在 `locales.ts` 添加 `Locale` 项，并在 `LanguageSwitcher.tsx` 的 `<select>` 中添加选项即可。

---

## 参与贡献

我们欢迎任何水平的贡献者——从首次提交到机器人领域专家。请阅读：

- 📖 [CONTRIBUTING.md](./CONTRIBUTING.md) — 环境搭建、分支与 PR 流程
- 📐 [docs/developer-guide.md](./docs/developer-guide.md) — 架构、开发流程与模拟后端路径
- 🤝 [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) — 社区规范
- 🔒 [SECURITY.md](./SECURITY.md) — 漏洞报告方式

入门友好问题标注 [`good first issue`](https://github.com/TopRemnantAcademy/robot-monitor/labels/good%20first%20issue)。

---

## 许可证

[MIT](./LICENSE) — 商业与个人使用均免费。
