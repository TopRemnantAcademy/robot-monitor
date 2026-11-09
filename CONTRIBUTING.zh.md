# 为 RobotMonitor 做贡献

感谢你有兴趣改进 RobotMonitor！本指南介绍如何搭建环境、分支策略以及发起 Pull Request。

## 行为准则

参与即表示你同意遵守我们的[行为准则](./CODE_OF_CONDUCT.md)。

## 语言

- **讨论与 Issue：** 英语是通用语，但欢迎韩语和中文（许多贡献者是韩国人形工厂的现场操作员以及中文 diaspora 运维社区）。
- **代码与注释：** 请用**英文**编写，以便最广泛的贡献者审查。
- **UI 字符串：** 切勿硬编码显示文本。请加入 [`frontend_dashboard/src/i18n/locales.ts`](./frontend_dashboard/src/i18n/locales.ts)（英文 key + 韩文 + 中文），并通过 `useI18n()` 解析。服务端下发的数据（告警消息、机器人名称）有意不翻译。

## 开发环境

### 全栈（Docker）

```bash
docker compose -f infra/docker-compose.yml up -d
bash test_integration.sh
```

### 仅前端（无需 Docker）

```bash
cd frontend_dashboard
npm install
node mock_backend.mjs            # :8787 启动 mock WS+REST 服务
VITE_API_BASE=http://localhost:8787 VITE_WS_HOST=localhost VITE_WS_PORT=8787 npm run dev   # :5173
```

详见 [docs/developer-guide.md](./docs/developer-guide.md)（含 mock 后端如何替代 EMQX/TimescaleDB/Redis）。

## 分支与提交

- 从 `main` 分出分支：`feat/...`、`fix/...`、`docs/...`、`chore/...`。
- 提交应聚焦且有清晰说明。
- **禁止提交密钥。** `.env`、`.env.local`、`*.token` 已被 git 忽略，切勿强制添加。
- CI 必须保持绿色（[`.github/workflows/ci.yml`](./.github/workflows/ci.yml)）：后端 pytest、前端 `npm test` + 类型检查 + `vite build` + 包体守卫、契约门禁、真实数据库 e2e（30k landing）。

## Pull Request

- 填写 [PR 模板](./.github/PULL_REQUEST_TEMPLATE.md)。
- 关联相关 Issue。
- 说明改了什么、**为什么**，并在相关处附上证据（测试输出、截图、前后对比）。
- 涉及 UI 改动时，请确认本地 `npm test` 与 `npm run typecheck` 通过。

## 报告 Bug 与提议功能

请使用 [`.github/ISSUE_TEMPLATE/`](./.github/ISSUE_TEMPLATE/) 下的模板：

- **Bug report** — 复现步骤、预期 vs 实际、环境。
- **Feature request** — 操作员痛点与期望行为。
- **Good first issue** — 面向新人的小而明确任务。

## 提出想法 & 早期讨论

对于早期或开放式的想法，优先使用 **GitHub Discussions** 而非正式 Issue：
- 💡 **Ideas** 分类 — 勾勒功能、收集反馈，在写代码前打磨范围。
- 👋 **Welcome** 讨论 (#16) — 打个招呼，询问从哪里开始。

推荐流程：在 **Ideas** 发帖 → 获得维护者/社区反馈 → 范围清晰后，开一个 **Feature request** Issue（模板在 `.github/ISSUE_TEMPLATE/` 下）并回链到该讨论。这样能让 Issue 列表聚焦在可落地、范围明确的任务上。

## 安全

发现漏洞？**请勿公开提 Issue。** 遵循 [SECURITY.md](./SECURITY.md) 私下进行报告。

## 许可证

贡献即表示你同意你的贡献以 [MIT 许可证](./LICENSE) 授权。
