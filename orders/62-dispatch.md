# 订单 62 — 前端 ECharts 按需注册补 BarChart/GaugeChart（🟢）

> 触发：Issue #62 评论 `@codebuddy`（workMode=true）。
> 产出：分支 `feat/order62-echarts-gauge-bar` @ `62d3fa9`，PR **#69**。
> 基线：`main @ 591176a`。

## 0. 派单方前提与实测的偏差（据实记录，勿再照抄）

派单正文称「`echartsCore.ts` 目前大概率全量引入，当前 gzip 401 KB」——
**前提已过期**：

| 派单说法 | 实测 | 差异根因 |
| :--- | :--- | :--- |
| echartsCore.ts 全量引入 | 已是 `echarts/core` 按需（订单 09 落地） | 订单 09 先于本单完成 |
| gzip 401 KB | 当前 `main` gzip **226.16 kB** | 401 KB 是订单 09 **之前**的数字 |

**教训（补进 SOP 纪律第 7 条的场景）**：凡引用「当前体积/当前实现」的派单，
派前必须重跑一次 `vite build` 与 `grep`，否则派工单描述的是历史状态。

## 1. 事实核验（先读代码）

- `node_modules/echarts-for-react/lib/index.js:4` → `require("echarts")` 全量（默认入口）。
- 产品走 core 入口：`src/components/RobotDetailDrawer.tsx:11-12`。
- `grep -rn echarts src/` → 仅 `RobotDetailDrawer.tsx:11-12,257` + `JointHeatmap.tsx:10`（注释）。
- **大屏实际挂载 ECharts 的只有 `RobotDetailDrawer.tsx`，且仅 `type: 'line'`（:148）**。
- `grep -rni "barchart|gauge|柱状|仪表" src/` → **零命中**。
  ⇒ `bar`/`gauge` 属**预留注册**，不是「实际使用的图表类型」。

## 2. 产出

| 文件 | 位置 | 内容 |
| :--- | :--- | :--- |
| `src/lib/echartsCore.ts` | `:44-52` | `use([LineChart, BarChart, GaugeChart, Grid/Tooltip/Legend, CanvasRenderer])` |
| `src/lib/echartsCore.ts` | `:1-42` | 注册清单 ↔ 实际渲染图逐项对照 + 预留注册理由与代价 |
| `README.md` | `:25-42` | 同步注册清单与体积表 |

## 3. 验收（全部实测）

- [x] `vite build` RC=0，主 chunk gzip **235.62 kB < 320 kB**（改前 226.16）
- [x] `tsc --noEmit` 0 错误
- [x] `.e2e/run_drawer.mjs` **8/8**（3 条 line series + 30 个历史点 + canvas）
- [x] 真实浏览器 canvas 像素验证 line/bar/gauge 三图全部出图、无运行时错误
- [x] 无 CDN、无新依赖、`package-lock.json` 零漂移
- [ ] ⚠ `.e2e/run_all.sh` 第 6 项失败 —— **HEAD 既有问题**（订单 15 LoginGate
      未同步 e2e），已用 HEAD 单独构建对照复现，非本单引入

## 4. 遗留（另开单）

`.e2e/run_offline.mjs` / `screenshot.mjs` 需注入已登录态，否则订单 15 之后
全部视觉 e2e 都停在登录页。属订单 15 的收尾缺口。

## 5. 交付

- 分支 `feat/order62-echarts-gauge-bar`，已 push（`git push origin` 成功）。
- PR **#69** → `main`，待审查工程师复核。
