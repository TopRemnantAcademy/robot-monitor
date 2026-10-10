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

> ⚠️ **实测陷阱（2026-10-11 订单 19）**：`--body` 里**必须真的写上 `@codebuddy`**。
> 只带 `--work-mode` 但正文没写触发词时，API 照常返回 `201`、评论也正常出现在 issue 里，
> **但一个构建都不会被拉起** —— 从返回码完全看不出失败。
> 派单后必须立即执行步骤 5（查 `get-build-logs`），而不是等到想起来才查。

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
| 评论返回 `201` 但**没有任何构建** | 触发评论里**漏写了触发词** | 补发一条含 `@codebuddy` 的评论（2026-10-11 订单 19 实测踩过） |
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
7. **派单前必须复验派工单本身**（2026-10-11 订单 15 实测）：
   派工单写完后若隔了几轮才派，里面的 `file:line`、依赖订单号、列名都会腐烂。
   本次复验发现 4 处过期 —— 行号整体后移（`/metrics` 接线后）、
   引用了换号前的订单号（13→17）、审计列名写成不存在的 `operator`、
   以及**漏掉 `/metrics` 免鉴权**（照原稿做会把 Prometheus 拉取挡在门外，
   刚接线的可观测性整套失效）。
   → **复验清单**：①行号 ②订单号/换号表 ③列名是否存在 ④有无"派单后才新增的端点"
     需要纳入约束（尤其是免鉴权白名单）。

---

## 6. 本项目派单台账

| 订单 | Issue | 构建 sn | 状态 | 备注 |
|---|---|---|---|---|
| 05 前端大屏 | #5 | `cnb-ft3-1k4hf69oe` | ✅ success → 已手工合并 | 34 文件/6696 行；`tsc` 0 错误、`vite build` 通过 |
| 06 联调压测 | #6 | `cnb-q81-1k4hgt1ee` | ⏳ pending | 收口单 |
| 07 aiomqtt 契约（I-1/I-2/I-3） | #8 | `cnb-s51-1k4hhvjem` | ⏳ pending | 另有一个重复构建已 stop |
| 08 asyncpg 升级（I-6） | #9 | `cnb-ijj-1k4himq2k` | ⏳ pending | 与 07 同改 `requirements.txt`，已注明冲突处理 |
| 09 前端 bundle 瘦身 | #10 | `cnb-ua1-1k4hj5d3h` | ✅ 已回收验证 | 1209.34→671.23 kB（gzip 401.05→224.79） |
| 10 告警抑制（I-5） | #13 | — | ✅ 已回收验证 | `alert_engine` 11/11 tests passed |
| 12 CNB 自行下载上游源 | #15 | — | ✅ 已回收 | 推翻了我的 I-1 结论，见 `external/ORDER12-VERIFICATION.md` |
| 13 可观测性接线 + 6 分支收敛 | #33 | `cnb-f0b-1k4j8osuu` | ✅ 已回收验证 | 产出 `feat/order-13` @ `911b667`；按路径回收，其 alert `main.py`/`config.py` 判为回退已弃；补了 NPC 漏掉的 `alerts_suppressed` |
| 19 `robot_alert` 身份 + ack 字段 | #34 | `cnb-38h-1k4jdbsm4` | ✅ 已回收并反向验证 | 产出 `auto/alert-identity-759f` @ `d27b972`；测试 25→29；NPC 发现 SQLAlchemy 会把 BIGSERIAL 排到复合 PK 最前，已用 `PrimaryKeyConstraint` 显式定序 |
| 15 认证与 RBAC（fastapi-users） | #36 | `cnb-e2k-1k4jf5tfo` | ⏳ pending | 基线 `main@58bafb5`；workMode=true 已核验。**派前修正**：原稿行号过期、订单号未换号、审计列名写错（`operator`→实际 `ack_by`）、漏了 `/metrics` 免鉴权 |

> ⚠️ **Issue 号与订单号不对齐，且跳跃极大**：#6=06、**#8=07、#9=08、#10=09、#13=10、
> #15=12、#33=13、#34=19**。中间号被其他 issue 占用。**派单后务必记录实际号**，
> 后续回收分支与追加情报都靠它。

### 2026-10-11 批量回收台账（远端曾累积到 19 个分支）

一次性回收 12 个分支，主代理在**本机实测**后分批落地：

| 分支 | 处置 | 依据 |
|---|---|---|
| `feat/order-06-integration` | ✅ 全取（仅 `reports/`） | 纯证据文件，零代码 |
| `auto/upstream-sources-3ec1`、`codebuddy/external-sources-download` | ✅ 全取 | 纯文档 |
| `auto/ui-alert-render-997e` | ✅ 全取 | 前端告警行增量渲染；`tsc` 0 错误 |
| `feat/ops-observability-layer` | ✅ 全取 | +13448 行是 9 个 Grafana 看板 JSON，非垃圾 |
| `feat/order-10-alert-inhibition` | ✅ 全取 | 自有订单，11/11 测试通过 |
| `auto/db-integration-copy` | ✅ 全取 | 引入 JSONB 类型修正与三级降级链 |
| `fix/infra-image-facts-and-contract-drift` | ⚠️ 部分取 | 取其 EMQX 撤稿与 `init.sql` 契约洞察；**`db/models.py` 拒取**（用 `sqlalchemy.JSON` 会回退 JSONB 修复） |
| `fix/issue19-merge-safe` | ⚠️ 只取新增文件 | 其 `batch_writer.py` **会删除整条三级降级链** |
| `codebuddy/observability-direct-wiring` | ⚠️ 只取新增文件 | 其 `main.py` 会回退 `/healthz` 语义 |
| `feat/order-08-asyncpg-upgrade` | ⚠️ 只取 `pool.py` + 单行依赖 | 其 `requirements.txt` 会把 aiomqtt 从 2.5.1 退回 2.0.1 |
| `cb/issue19-github-rescan`、`docs/detail-gap-reassessment`、`feat/enterprise-gap-analysis-issue19` | ⏳ 移交订单 13 | 三方同时追加 `STATUS.md`/`external/*.md`，需并集合并 |

### 追加情报的正确姿势

已派单后又发现关键事实，可**追加评论**补充 —— 但正文**不得含 `@codebuddy`**，
否则会再触发一次构建。实测：订单 09 派单后追加了一条 echarts 引用范围的情报
（不含触发词），构建数保持 1 个。

---

## 6.5 批量回收纪律（2026-10-11，一次回收 12 个分支的实测总结）

并行派单会让远端在几小时内堆积十几个分支。回收时**禁止一次性 merge**，
必须按下列顺序处置。

### 步骤 1：先测每个分支的「基线世代」

```bash
for b in <branch>...; do
  base=$(git merge-base main cnb/$b)
  echo "$b : 含order07=$(git merge-base --is-ancestor <sha7> cnb/$b && echo Y||echo N) \
        含order09=$(git merge-base --is-ancestor <sha9> cnb/$b && echo Y||echo N)"
done
```

**基于旧世代的分支，其"未改动"的文件里藏着已被修好的旧 bug。**
实测：`feat/order-08` 的 `batch_writer.py` 不含 `_STOP` 哨兵、
`requirements.txt` 写着 `aiomqtt==2.0.1` —— 直接 checkout 会静默回退订单 07。

### 步骤 2：区分「新增文件」与「修改文件」

```bash
git diff --name-status --diff-filter=A $base cnb/$b   # 新增 → 可直接取
git diff --name-status --diff-filter=M $base cnb/$b   # 修改 → 必须逐个审
```

**新增文件零回归风险，可批量 `git checkout cnb/$b -- <path>`。
修改文件是全部风险所在。**

### 步骤 3：修改文件逐条做「是回退还是改进」判定

判定标准：**对方版本相对 `main` 删掉了什么**。删除比新增危险得多。
实测三个真实案例：

| 分支改动 | 判定 | 依据 |
|---|---|---|
| 删掉三级降级链（L1/L2/L3） | ❌ 拒取 | 是 `spec/01 §4.2` 的容错核心 |
| `/healthz` 从依赖可达性退化成硬编码 `status:ok` | ❌ 拒取 | 丢掉 `db`/`retries`/`rejected` |
| `primary_key=True` → 完全无主键 | ❌ 拒取（且**跑不通**） | SQLAlchemy 抛 `ArgumentError` |

### 步骤 4：合并演练，量化冲突面

```bash
git checkout -b tmp/probe main
for b in ...; do
  git merge --no-commit --no-ff cnb/$b -m x >/dev/null 2>&1
  echo "$b conflicts=$(git diff --name-only --diff-filter=U | wc -l)"
  git diff --name-only --diff-filter=U
  git merge --abort
done
```

⚠️ **演练前务必确认当前不在 `tmp/probe` 上**，否则最后 `git branch -D tmp/probe`
 会失败，后续 commit 全部落进临时分支（本次实际踩中，导致一次空推送）。

### 步骤 5：无法自动合并的，把「判定」和「执行」分开

主代理负责**判定**（哪些该留、哪些会回退、为什么），
把判定结果写成派单里的**显式保留规则表**，交给 CNB 去**执行**合并与验证。
不要自己手搓十几个冲突文件 —— 易错且不可复现。

### 步骤 6：找「分支独有贡献」要用**三点** diff，不是两点

```bash
git diff main...cnb/$b        # ← 正确：merge-base → 分支，即分支自己的改动
git diff main      cnb/$b     # ← 错误：会把 main 后加的内容也算成"分支删掉的"
```

两点 diff 显示的 `D` 大多是「main 有、分支没有」，不是分支真删了东西。
用三点 diff 才能拿到该分支**真正新增的东西**（2026-10-11 第二轮实测：
22 个分支里真正有价值的独有贡献只有 5 处）。

### 步骤 7：即使是"纯新增"，也要先校验它相对 main 是不是超集

三点 diff 显示 `+N -0` **不等于** 可以整文件 checkout。分支往往是旧版：

```bash
git diff --numstat main cnb/$b -- <file>     # 看删除数；不是 0 就不能整取
```

实测：`cb/issue19-github-rescan` 的 `external/SOURCES.md` 三点看是 `+138 -0`，
但两点看是 `+143 -101` —— 整取会删掉 main 后加的「§1.1 撤稿」纠正段。
正解是**只抽取该分支独有的段落**追加。

⚠️ **顺带检查章节编号冲突**：两边都有 `## 5.` 但内容不同
（main 的 §5=第四批、分支的 §5=第五批），直接追加会产生两个 §5。
抽取时按目标文件的实际编号**重编号**再追加。

### 步骤 8：回收后固定跑两种全仓扫描

```bash
# ① 假覆盖扫描：test_*.py 里没有 test_* 函数 = pytest 收集 0 却静默"通过"
#    （本仓已踩两次：alert_engine/tests/test_notifier.py、
#     backend_ingestion/tests/test_db_integration.py）
python - <<'PY'
import pathlib, re
for p in pathlib.Path(".").rglob("test_*.py"):
    if "external" in p.parts: continue
    txt = p.read_text(encoding="utf-8", errors="ignore")
    if not re.search(r'^\s*(async\s+)?def\s+test_', txt, re.M):
        print("FAKE-COVERAGE:", p)
PY

# ② 确认仓库有 pytest 配置，否则根跑 pytest 会去收集 external/ 的第三方源码
#    （本仓此前实测 61 个 collection error 并中断，本仓自身结果完全看不见）
python -m pytest -q        # 有 pytest.ini 后：29 passed

# ③ 外部源"下载完了吗"不能靠仓库断言 —— external/* 被 .gitignore 排除，
#    源码根本不入库，"已下载 22 个"这种结论在仓库里查不出来。
#    必须拿 fetch-sources.sh 的声明清单与磁盘实存比对：
python - <<'PY'
import re, pathlib
block = re.search(r'SOURCES=\((.*?)\)',
    pathlib.Path("external/fetch-sources.sh").read_text(encoding="utf-8"), re.S).group(1)
declared = set(re.findall(r'^\s*"([A-Za-z0-9._-]+)\|', block, re.M)) | {"alertmanager"}
actual = {p.name for p in pathlib.Path("external").iterdir() if (p/".git").exists()}
print("缺失:", sorted(declared - actual), " 多余:", sorted(actual - declared))
PY
#    实测（2026-10-11）：声明 22 / 实存 14，缺 8 个 —— 此前"已下载 22 个"的说法是错的。
```

⚠️ **正则陷阱**：用 `"([^|]+)\|` 会把前一条的结尾引号一起吃进去，
产出 `'\n  "aiomqtt'` 这种脏名字。必须用 `^\s*"([A-Za-z0-9._-]+)\|` 加 `re.M`。

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
