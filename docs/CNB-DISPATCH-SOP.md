# CNB NPC 派单标准作业程序（SOP）

> 本文规定**如何给 CNB 的 @codebuddy 指派任务**，以及**如何判别它到底做没做完**。
> 全部条目均来自本项目实测（2026-10-09 ~ 10），非推测。未经实证的标「未验证」。

---

## 0. 三条派单通道

| 通道 | 触发方式 | 适用场景 | 是否注入 `CNB_NPC_TRIGGER_CONTENT` |
|---|---|---|---|
| **A. Issue/PR 评论**（主用） | 评论里出现 `@codebuddy` | 单次具体任务 | ✅ 是 |
| **B. crontab 定时** | `.cnb.yml` 的 `crontab` 阶段 | 周期性巡检 | ❌ 否 |
| **C. 指定角色** | `@flow.237/RobotMonitor(角色名)` | 需要特定专家视角 | ✅ 是 |

**本项目只用通道 A。** 通道 B 的坑见 §5.3。

---

## 1. 标准派单流程（7 步）

### 步骤 1 — 确认基线提交
```bash
git log --oneline -1          # 记下 sha，写进派单正文
```
派单正文必须写明「基于 `main @ <sha>`」，否则 NPC 可能基于旧代码开工。

### 步骤 2 — 先把需求写成文件
**禁止**把长需求直接塞进 `--body`。一律写成 `orders/NN-dispatch.md` 再 `--body-file`。
理由：可入库留档、可复用、可 diff，且避免 shell 转义问题。

### 步骤 3 — 创建 Issue（**正文不要放触发词**）
```bash
cnb issues create-issue \
  --repo flow.237/RobotMonitor \
  --title "NN — <一句话任务>" \
  --body-file orders/NN-dispatch.md
```

> ⚠️ **实测陷阱**：如果 `create-issue` 的正文里含 `@codebuddy`，
> CNB 会**立即触发一次构建**；随后再 `post-issue-comment` 又会触发第二次
> → 两个 NPC 同时改同一批文件，产生冲突与重复提交。
> （本项目订单 07 实际发生过，已用 `stop-build` 停掉一个。）
>
> **规则：create-issue 正文不含触发词，触发只在步骤 4 做一次。**

### 步骤 4 — 用评论触发 NPC（**必须带 `--work-mode`**）
```bash
cnb issues post-issue-comment \
  --repo flow.237/RobotMonitor \
  --number <issue号> \
  --work-mode \
  --body "@codebuddy 请接单执行订单 NN。<一句话要点>。完整需求见 orders/NN-dispatch.md。"
```

`--work-mode` **不可省略**。实测：没有它时 NPC 得到 `CNB_NPC_ENABLE_WORKMODE=false`，
无法写仓库，最终只产出分析不落地。

### 步骤 5 — 立刻核验 `workMode`
```bash
cnb build get-build-status --repo flow.237/RobotMonitor --sn <sn>
```
确认输出含：
```
npc:
  name: CodeBuddy
  match: @codebuddy
  workMode: true          # ← 必须是 true
```
`workMode: false` 说明步骤 4 漏了参数，白跑一场。

### 步骤 6 — 等构建结束，查状态
```bash
cnb build get-build-status --repo flow.237/RobotMonitor --sn <sn>
# status: pending → success / error
```
单次 NPC 阶段实测耗时 **约 12 分钟**（订单 05：716 秒）。

### 步骤 7 — ⭐ 判别「真完成」（**最关键，别信 status**）

`status: success` **不等于**代码已进 main。实测订单 05 就是 success 但 `main` 纹丝不动。

必须走这个判据：

```bash
# 7-1 看远端有没有新分支（NPC 推不动 main，只能推分支）
git -c http.extraHeader="Authorization: Bearer ${TOKEN}" ls-remote --heads cnb

# 7-2 有新分支 → fetch + 合并
git -c http.extraHeader="Authorization: Bearer ${TOKEN}" fetch cnb <branch>
git merge <sha>

# 7-3 再看 main 是否真的前进了
git log --oneline -3
```

> **口诀：success 但没有新 commit = 产出在分支上。**

---

## 2. 派单正文必须包含的 8 个要素

缺一项就容易被 NPC 自由发挥或假通过：

| # | 要素 | 作用 |
|---|---|---|
| 1 | **基线提交 sha** | 防止基于旧代码开工 |
| 2 | **必读文件清单**（带路径） | 防止跳读导致契约不符 |
| 3 | **任务分解**（T1/T2…） | 可逐条验收 |
| 4 | **验收标准**（可判定的 `- [ ]`） | 防止"我觉得做完了" |
| 5 | **禁止项** | 防止顺手改坏别处 |
| 6 | **交付方式**（分支名 / 是否开 PR） | 便于回收 |
| 7 | **失败上报要求**（禁止用"已推送"掩盖 403） | 防止静默失败 |
| 8 | **定位一句话**（如"你是验收方，证据优先"） | 约束角色行为 |

### 最小可用模板

```markdown
@codebuddy 请接单执行订单 NN（<任务名>）。

## 0. 当前状态
基线提交：main @ <sha>
已完成：<列出前置订单>

## 1. 必读文件
| 顺序 | 文件 | 目的 |

## 2. 任务分解
### T1 — ...

## 3. 验收标准
- [ ] ...

## 4. 禁止项
1. 禁止用「日志无报错」代替通过判定
2. 禁止伪造数字；跑不出来写「未验证 + 所需条件」
3. 禁止只推分支就宣称完成；push 失败必须原文报告

## 5. 交付方式
1. 分支 feat/order-NN-<slug>
2. 尝试 push + 开 PR
3. push 失败 → 把 diff 与结论写进 issue 评论
```

---

## 3. 令牌与推送（CNB 特殊要求）

### 取令牌
```bash
TOKEN=$(python -c "import json,os;\
p=os.path.expanduser('~/.cnb/token');\
print(json.load(open(p))['auths']['cnb.cool']['access_token'])")
```

### 推送（**必须用 Bearer 头**）
```bash
git -c http.extraHeader="Authorization: Bearer ${TOKEN}" push cnb main
```

> ⚠️ **实测**：以下两种写法都会**无限挂起**（不是慢，是 hang）：
> - URL 内嵌令牌 `https://user:TOKEN@cnb.cool/...`
> - `cnb git-credential` helper（返回 `unknown host: cnb.cool`，exit 2）
>
> 另注：`git ls-remote`（GET）正常，`git push`（POST git-receive-pack）才 hang ——
> 很容易误判成"权限问题"，其实是协议问题。

---

## 4. 失败诊断表

| 现象 | 根因 | 处理 |
|---|---|---|
| 构建日志 `403 ... repo-code:rw` | NPC 令牌无写仓库 scope | NPC 推不进 main → **主代理手工合并分支** |
| `402 [EXCEEDING_LIMIT] AI 积分已用尽` | 账号级别额度耗尽 | 需用户充值，非代码问题 |
| `workMode: false` | 派单漏了 `--work-mode` | 重新派单并带上参数 |
| 一次派单出现 **2 个构建** | create-issue 正文含触发词 | `cnb build stop-build --sn <重复sn>` |
| `status: success` 但 main 没动 | NPC 只推了分支 | 走 §1 步骤 7 |
| crontab 阶段秒退 `process.exit(1)` | crontab 不注入触发内容，`system_prompt` 或 `user_prompt` 为空 | 两个 prompt 都要**内联非空** |
| `[CONFIG_YAML_EMPTY]` | CI 配置放错位置 | 必须在**仓库根** `.cnb.yml`，不是 `.cnb/.yml` |

---

## 5. 派单纪律（违反即返工）

1. **一次一单**：不在同一个 issue 里追加第二个任务，避免上下文污染
2. **不重复触发**：create-issue 不放触发词（见 §1 步骤 3）
3. **冲突预判**：若两单改同一文件（如都改 `requirements.txt`），
   在后一单里写明「以同时包含两项为准」，见 `orders/08-dispatch.md` §6
4. **不为通过而放宽阈值**：如 30,000 行不得改成 300
5. **区分环境错误与代码错误**：见 `docs/requirements/R-07-integration-and-stress.md` §4
6. **NPC 产出必须主代理验收**：合并后至少做一次编译/导入验证

---

## 6. 本项目派单台账

| 订单 | Issue | 构建 sn | 状态 | 备注 |
|---|---|---|---|---|
| 05 前端大屏 | #5 | `cnb-ft3-1k4hf69oe` | ✅ success → 已手工合并 | 34 文件/6696 行；`tsc` 0 错误、`vite build` 通过 |
| 06 联调压测 | #6 | `cnb-q81-1k4hgt1ee` | ⏳ pending | 收口单 |
| 07 aiomqtt 契约（I-1/I-2/I-3） | #8 | `cnb-s51-1k4hhvjem` | ⏳ pending | 另有一个重复构建已 stop |
| 08 asyncpg 升级（I-6） | #9 | `cnb-ijj-1k4himq2k` | ⏳ pending | 与 07 同改 `requirements.txt`，已注明冲突处理 |
| 09 前端 bundle 瘦身 | #10 | `cnb-ua1-1k4hj5d3h` | ⏳ pending | 已补发 echarts 引用范围实证，省其调研 |

> ⚠️ **Issue 号与订单号不对齐**：#6=订单06，但 **#8=订单07、#9=订单08、#10=订单09**
> （#7 被其他 issue 占用）。**派单后务必记录实际号**，后续回收分支靠它。

### 追加情报的正确姿势

已派单后又发现关键事实，可**追加评论**补充 —— 但正文**不得含 `@codebuddy`**，
否则会再触发一次构建。实测：订单 09 派单后追加了一条 echarts 引用范围的情报
（不含触发词），构建数保持 1 个。

---

## 7. 相关文件索引

| 文件 | 内容 |
|---|---|
| `.cnb.yml` | CI/NPC 流水线定义（**必须在仓库根**） |
| `.cnb/settings.yml` | 10 个 NPC 角色定义 |
| `AGENTS.md` | NPC 每次运行都会自动注入的工程纪律 |
| `orders/NN-dispatch.md` | 各单派工正文 |
| `orders/NN-<name>.md` | 各单原始任务定义 |
| `docs/requirements/R-*.md` | 验收规格 |
| `external/INTEGRATION-PLAN.md` | 上游开源源套用计划 |
| `external/SOURCES.md` | 下载源 provenance |
