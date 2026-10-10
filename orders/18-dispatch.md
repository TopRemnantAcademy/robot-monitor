# 派工单：订单 18 — 契约即代码（消灭"前后端靠人肉对齐"）

> 依据：`docs/design/ENTERPRISE-GAP-ANALYSIS.md` §2.2（缺口 E6）
> 触发方式：`post-issue-comment`（本文件不含触发词）

---

## 0. 为什么要做：本项目这类事故**已真实发生过**

**事实**：`STATUS.md` §六第 2 条记录：
> `/history` 不返回 `joint_positions`（`backend_ingestion/app/main.py`）→
> Drawer 第三条折线「角度(rad)」**恒空**

**事实**：前端类型是手写的 —— `frontend_dashboard/src/types/robot.ts:8-17`，
与后端 `backend_ingestion/app/schemas/telemetry.py:13-22` 的 24 关节键名靠**人眼比对**一致
（见 `STATUS.md` §五，主代理逐字核对过）。

> 也就是说：**目前的一致性是被"人工核对过"保障的，不是被机器保障的**。
> 下一次改字段，还会再空一条线。

---

## 1. 任务

### T1 — 生成 TS 类型（取代手写）
- 后端启动产出 `/openapi.json`（FastAPI 默认已有，确认未被关掉）
- 用 `openapi-typescript`（npm，与 Vite 同栈）从 `/openapi.json` 生成：
  `frontend_dashboard/src/types/api.gen.ts`
- **禁止**改 `types/robot.ts` 的**对外导出名**（`RobotTelemetry` 等），
  否则会波及全部组件；采用「`robot.ts` re-export 生成类型」的桥接方式
- `package.json` 加脚本 `"gen:types": "openapi-typescript ..."`

### T2 — CI 门禁：契约漂移即失败
- `.cnb.yml` 增加一步：生成类型 → `git diff --exit-code frontend_dashboard/src/types/api.gen.ts`
- **漂移即 CI 红**，迫使 PR 作者同步前端类型
- **禁止**用 `|| true` 吞掉失败

### T3 — 契约快照
- 把 `/openapi.json` 存为 `docs/contracts/openapi.snapshot.json` 入库
- CI 对比快照，**breaking change（删字段/改类型/改必填）单独报错**
- 非 breaking（加可选字段）只告警不失败

### T4 — WS 契约（OpenAPI 管不到的部分）
WS 三分支（`main.py:176/193/195`）必须补一份**机器可读**的契约文件：
`docs/contracts/ws-envelope.schema.json`（JSON Schema），
前端用 `ajv` 在开发模式下校验，**生产不校验**（避免性能开销）。

---

## 2. 验收标准

- [ ] `npm run gen:types` 可重复运行，输出**幂等**（连跑两次 `git diff` 为空）
- [ ] 手改 `api.gen.ts` 后，CI 的契约门禁步骤**失败**（贴 CI 日志证明）
- [ ] 故意在后端删掉一个响应字段 → CI 报 breaking change（贴证明）
- [ ] `tsc --noEmit` 0 错误
- [ ] `vite build` RC=0，bundle 体积**无显著增长**（贴改前/改后）
- [ ] WS schema 文件存在，开发模式下能校验出**故意构造的错误信封**

## 3. 禁止项

1. **禁止**生成类型进 `node_modules` 或 `.gitignore`（必须入库，才能被 diff 门禁看到）
2. **禁止**改现有组件的 props 名
3. **禁止**用 `|| true` / `continue-on-error` 绕过门禁
4. **禁止**把生成过程变成手工步骤（必须脚本化）
5. **禁止**伪造 CI 日志
6. **禁止**只推分支就宣称完成 —— 尝试 push + 开 PR；失败则**原文报告**

## 4. 冲突提示

- 本单只改 `frontend_dashboard/src/types/` 与 `.cnb.yml`，
  与订单 11/12/13 的代码改动**无文件重叠**。
- 若订单 11 新增了认证端点，**本单生成类型时会自动包含**，属预期。
