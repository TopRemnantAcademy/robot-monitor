# 安全政策

> 🌐 其他语言版本： [English](./SECURITY.md) · [한국어](./SECURITY.ko.md)

## 受支持版本

安全修复应用于最新的 `main` 分支。我们不维护单独的发版分支。

## 报告漏洞

**请勿通过公开的 GitHub Issue 报告安全漏洞。**

请改用以下任一方式私下报告：

- GitHub 私有漏洞报告（推荐）：在仓库的 "Security" 标签页中起草 **Security Advisory**。
- 邮件联系维护者（见仓库所有者资料），主题注明 `RobotMonitor security report`。

请包含：

- 漏洞描述及其影响。
- 复现步骤或概念验证（PoC）。
- 受影响的组件及版本/提交。
- 如已知，任何建议的缓解措施。

我们会在几个工作日内确认收悉，同步进展，并在修复可用后协调披露。

## 给贡献者的安全注意事项

- **仓库中不得存放密钥。** `.env`、`.env.local` 与令牌文件已被 git 忽略。CI 仅使用占位密钥（`JWT_SECRET=ci-*-placeholder`）；切勿提交真实凭据。
- **输入校验：** 采集服务通过 MQTT 接收不可信遥测、通过 WS 接收告警，请将所有外部输入视为敌对（限制大小、校验 schema、禁止 `eval`/模板注入）。
- **鉴权：** 看板与 REST API 的变更操作（如告警确认）需要 JWT。不得削弱 `/alerts/{id}/ack` 等鉴权闸门。
- **依赖卫生：** 保持 `requirements.txt` 与 `package-lock.json` 更新，并开启 Dependabot/Renovate 告警。
