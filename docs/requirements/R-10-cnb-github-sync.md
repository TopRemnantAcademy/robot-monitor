# R-10 CNB → GitHub 单向自动同步（C4 / Issue #64）

> 状态（2026-10-16）：**配置已修复 + 激活，待用户在 CNB 控制台设 2 个仓库级 CI 变量后生效**。
> ⚠️ 接入方式已变更：**弃用** §2.2 的「密钥仓 + `imports`」方案（本账户 CNB token 缺
> `group-resource:rw`，建仓实测 403 errcode 10023；且本仓为 PUBLIC，明文 PAT 不可接受），
> 改用**仓库级 CI 变量** `GIT_USERNAME` / `GIT_ACCESS_TOKEN` —— 与 git-sync 官方 README 的
> 「CNB → GitHub 推送模式」示例逐字一致（`stages: → image: tencentcom/git-sync → settings`）。
> 另修复 `6ac9e7f` 误删 `stages:` 包裹致 pipeline 空转的缺陷（`557c664`）；实测 stage 以
> `skipped` 正常跳过（变量未设时流水线保持全绿）。GitHub 侧现状：main 停在 `4208f8c`
> （落后 CNB 46 提交，可 ff），最后推送 `2026-10-12T00:19Z`；因 `4208f8c` 处 `.cnb.yml` 的
> sync 块为注释态、且 §5 记「端到端未验证」，该内容的来源**未能证实为 git-sync 产物**
> （不排除早期手工推送）。
> 事实源：Issue #64（`CNB main 自动同步 GitHub origin (C4)`）。

---

## 1. 目标

CNB `main` 推送后，自动把代码同步到
`github.com/TopRemnantAcademy/robot-monitor`，让外部协作者看到进展。

**单向**：只允许 CNB → GitHub。禁止把 GitHub 的变更反向合入 CNB。

---

## 2. 已落地的实现（`.cnb.yml`）

位置：`.cnb.yml:23-45`，挂在 `main` 分支的 `push` 事件下。

```yaml
main:
  push:
    - name: sync-to-github
      imports:
        - https://cnb.cool/flow.237/RobotMonitor-secrets/-/blob/main/github-sync.yml
      stages:
        - name: push main to github origin
          if: '[ "$CNB_PIPELINE_KEY" != "pipeline-0" ]'
          image: tencentcom/git-sync
          settings:
            target_url: https://github.com/TopRemnantAcademy/robot-monitor.git
            auth_type: https
            username: ${GIT_USERNAME}
            password: ${GIT_ACCESS_TOKEN}
            branch: main
            force: false
            push_tags: false
```

### 2.1 为什么用 `tencentcom/git-sync` 而不是裸 `git push`

CNB 插件市场有官方跨平台同步插件 `tencentcom/git-sync`
（[插件 README](https://cnb.cool/cnb/plugins/market/-/blob/main/plugins/tencentcom/git-sync/README.md)），
文档明示「从 CNB 同步到 GitHub」用推送模式：

- 参数受 `settings` schema 约束（`target_url` / `auth_type` / `username` / `password` / `branch` / `force`），
  拼错参数名会被配置校验拦下，优于手写 shell 里拼 URL。
- 认证走插件内部，**不会把 token 拼进命令行参数**（裸 `git push https://user:token@host/...`
  会把 token 留在进程 argv 与日志里）。

### 2.2 凭据注入方式：`imports`（密钥仓库）

CNB 的密钥注入标准做法是 **密钥仓库 + `imports`**
（[密钥仓库文档](https://docs.cnb.cool/zh/repo/secret.md#在流水线中引用)）：

- 密钥仓库**禁止 clone 到本地**，只能在 Web 界面编辑，有审计日志与水印 —— 天然满足
  「禁止明文 token 入仓」。
- `imports` 把文件内容注入为环境变量，`${GIT_USERNAME}` / `${GIT_ACCESS_TOKEN}`
  是插件 `settings` 里的 mustache 占位符，构建时被替换。
- **行为要点（重要）**：`.cnb.yml` 会绕过 `imports` 的权限检查，
  若 token 未配置，`${GIT_ACCESS_TOKEN}` 会**原样**传进插件而不是替换成空串，
  GitHub 会拒绝认证 → 流水线**显式报红**。这正好满足验收里
  「故意使 token 失效时明确失败而非静默成功」。

### 2.3 防回环：`if: '[ "$CNB_PIPELINE_KEY" != "pipeline-0" ]'`

`main` 下的流水线是**数组**形式，没有显式 key 时平台按索引自动分配，
本流水线自身即 `pipeline-0`。

CNB 的事件语义是：**调用 OpenAPI 的凭证身份触发的 push 会再次触发 `push` 事件**；
而 `CNB_TOKEN` 触发的 push 不触发（[触发规则](https://docs.cnb.cool/zh/build/trigger-rule.md)、
[默认环境变量](https://docs.cnb.cool/zh/build/build-in-env.md)）。
本流水线只做 `git push`、不调 OpenAPI，**理论上不会自触发**；但 git-sync 以什么身份推、
CNB 是否对其去重，仅在远端未验证。这条 `if` 是一道零成本保险：

- 顺带挡掉 git-sync 自己那条「同步成功」的构建（因为它由 git push 触发，
  从它的视角分支看起来「变化了」），避免每次同步产生两条冗余构建。
- 数组形式下平台重新分配 key 时，这行的索引需要同步更新，已写在注释里。

---

## 3. 使用者需要做的一次性配置（前置条件）

> ⚠️ 以下三步**尚未完成**，因此当前同步链路**跑不通**（见 §5）。

### 步骤 1：确认/创建 GitHub 目标仓库

> **事实（未验证项 A）**：截至本次执行，`https://api.github.com/repos/TopRemnantAcademy/robot-monitor`
> 返回 **404**；`https://api.github.com/users/TopRemnantAcademy`
> 存在（`type: User`，非 Organization），其公开仓库里只有 `korean-gospel-ai`。
>
> 即**目标仓库目前不存在**（或是私有仓库，但私有仓库对匿名请求同样返回 404，
> 两者无法从这里区分）。同步前需要在 `TopRemnantAcademy` 下建好 `robot-monitor`。

### 步骤 2：建一个只写该仓库的 PAT

- Fine-grained PAT，`Repository access` 选 `TopRemnantAcademy/robot-monitor`。
- 权限最小化：`Contents: Read and write`（推送分支只需要这一项）。
- **不要**给 `Administration` / `Workflows` 等无关权限。

### 步骤 3：建 CNB 密钥仓库并填凭据

1. 在 CNB 建一个**密钥仓库**，路径就是 `.cnb.yml` 里引用的那个：
   `flow.237/RobotMonitor-secrets`（若改名，请同步改 `.cnb.yml` 的 `imports`）。
2. 在 Web 界面新建文件 `github-sync.yml`，内容模板见
   `docs/secrets/github-sync.yml.example`，填成真实值：

   ```yaml
   GIT_USERNAME: x-access-token
   GIT_ACCESS_TOKEN: <paste-fine-grained-pat-here>
   ```

   > `x-access-token` 是 GitHub 对「用 PAT 走 HTTPS」约定的用户名；
   > 用真实 GitHub 用户名也可以，**token 是唯一凭据**。
3. （建议）在该文件里声明访问范围，收敛到本项目 + 保护分支：

   ```yaml
   allow_slugs: flow.237/RobotMonitor
   allow_events: push
   allow_branches: main
   allow_images: tencentcom/git-sync
   ```

   参考[文件引用权限检查](https://docs.cnb.cool/zh/build/file-reference.md#权限检查)。
   注意：一旦声明 `allow_*`，触发者角色权限不再参与判断，只看规则。

---

## 4. 验收方法（Issue #64 定义）

| 验收项 | 怎么验 |
|---|---|
| CNB main 推送后 GitHub 同步一致 | 记下 `git rev-parse main`（CNB `591176ab`），推送后对 GitHub 跑 `git ls-remote https://github.com/TopRemnantAcademy/robot-monitor.git refs/heads/main`，比对 SHA |
| token 失效时显式失败 | 在密钥仓库把 `GIT_ACCESS_TOKEN` 改成无效值（或撤销 PAT），推一次 main，流水线必须**红色失败**，日志里 git-sync 报认证错误 |

---

## 5. 未验证项（诚实声明）

| # | 未验证内容 | 验证所需条件 |
|---|---|---|
| A | GitHub 目标仓库是否存在/可写 | `TopRemnantAcademy/robot-monitor` 需先存在；本环境无 GitHub 读权限凭证，匿名 API 返回 404 |
| B | 全链路能否真正推上去 | 需要 CNB 密钥仓库 `flow.237/RobotMonitor-secrets` 与有效 PAT；本环境无法创建/读取密钥仓库 |
| C | git-sync 推送是否会触发 CNB 自身 push 回环 | 需要一次真实 push；已用 §2.3 的 `if` 兜底 |
| D | `if` 能否引用 `CNB_PIPELINE_KEY`、不进 shell 能否拿到值 | 需要一次真实 push 观察（文档未明示） |
| E | `imports` 的密钥仓库路径/权限是否生效 | 需要真实密钥仓库 |

**验证命令（本地能做的部分，已执行）**：

```bash
$ node /root/.agents/skills/cnb-pipeline/validator/validate.js .cnb.yml
✅ YAML 语法校验通过
✅ 语义校验通过
❌ Schema 校验失败（共 16 个问题，展示前 4 个）:
  /$/pull_request/comment@npc: 缺少必填字段 "stages"
  ...
```

- 这 16 个问题里 **main 相关为 0**，与改动前基线**逐条一致**（属仓库既有的
  `$` 下 npc 配置与校验器 schema 的口径差异），本次改动未引入新错误。
  验证脚本见 §6。

```bash
$ node -e "<用官方 conf-schema-zh.json 单独校验 main/push 片段>"
✅ fragment Schema OK
```

---

## 6. 校验脚本（可复现）

```bash
cd /root/.agents/skills/cnb-pipeline/validator && [ -d node_modules ] || npm install
node validate.js /workspace/.cnb.yml            # 全文件
node validate.js /tmp/cnb.yml.bak               # 改动前基线，用于对比
```

`conf-schema-zh.json` 由校验器自动缓存/下载（`docs.cnb.cool/conf-schema-zh.json`），
用它单独编译 `#/definitions/pipeline` 可只校验新增片段。

---

## 7. 明确不做

- 反向同步（GitHub → CNB）：Issue #64 明令禁止。
- 把 token/私钥写进 `.cnb.yml` 或任何入库文件：只允许 `${VAR}` 占位符。
- `force: true`：默认非强推，保护 GitHub 侧历史；如遇分叉应显式处理而非覆盖。
