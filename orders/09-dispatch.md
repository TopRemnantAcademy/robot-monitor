# 派工单：订单 09 — 前端 bundle 瘦身（ECharts 按需引入）

> 依据：`STATUS.md` 阶段 C 的 C1、`docs/requirements/R-01-frontend-dashboard.md`
> 触发方式：`post-issue-comment`（本文件**不含**触发词，避免重复触发构建）

---

## 0. 背景与实测基线

主代理实测（2026-10-10）当前生产构建：

```
dist/index.html                     0.42 kB │ gzip:   0.32 kB
dist/assets/index-CTG3PYTE.css      8.38 kB │ gzip:   2.52 kB
dist/assets/index-CPs8c6Z7.js   1,209.34 kB │ gzip: 401.05 kB

(!) Some chunks are larger than 1200 kB after minification.
```

**1.21 MB（gzip 401 KB）的主因是 ECharts 全量引入**。
`frontend_dashboard/package.json` 中为 `"echarts": "5.5.1"`，
组件里大概率是 `import * as echarts from 'echarts'` 或 `import ReactECharts from 'echarts-for-react'`。

## 1. 任务：改为按需引入（tree-shaking）

ECharts 5 支持从 `echarts/core` 按需注册：
只 import 用到的图表类型与组件，未用到的不进 bundle。

### 需要确认实际用到的能力（先读代码再改）

| 组件 | 用到的 ECharts 能力 |
|---|---|
| `RobotDetailDrawer.tsx` | **折线图**（LineChart）+ tooltip / legend / grid / 直角坐标系 |
| `JointHeatmap.tsx` | **热力图**（HeatmapChart）或自绘网格 —— **先确认是不是真的用了 ECharts** |

> ⚠️ **关键前提**：`JointHeatmap.tsx` 只有 74 行，可能是用 div/grid 自绘而非 ECharts。
> **必须先读代码确认**，不要凭"热力图"这个名字想当然地去 import HeatmapChart。
> 若它确实没用 ECharts，则只需注册折线图相关模块。

### 典型改法（示意，具体以代码实际用法为准）

```ts
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);
```

`echarts-for-react` 若继续使用，需传 `echarts` 实例或使用其 `core` 版本入口。

## 2. 验收标准（可测量）

- [ ] `vite build` **成功**，无新增报错
- [ ] 主 JS chunk **显著下降**：目标 **≤ 600 kB**（gzip ≤ 200 kB）
      若达不到，说明还有其他大头，需在报告中说明实测值与构成
- [ ] `tsc --noEmit` **0 错误**
- [ ] **功能不回退**：Drawer 三条折线（温度/扭矩/角度）仍能渲染；
      热力图 24 格仍正常
- [ ] 贴出**改前 vs 改后**的 `vite build` 产物体积对比（真实输出，不是估算）

## 3. 禁止项

1. **禁止**为了让体积数字好看而删功能（如砍掉第三条折线）
2. **禁止**改设计令牌或布局（`src/theme/tokens.ts` 与三区布局不在本单范围）
3. **禁止**升级 echarts 主版本（当前 5.5.1）
4. **禁止**伪造体积数字 —— 贴真实 `vite build` 输出
5. **禁止**破坏 `frontend_dashboard/.e2e/` 下已有脚本
6. **禁止**只推分支就宣称完成 —— 尝试 push + 开 PR；因 `repo-code:rw` 失败则
   **原文报告错误**

## 4. 与并行订单的关系

订单 06/07/08 正在 CNB 执行，**均不触碰前端代码**，本单无冲突。
若 `package.json` 被其他单改动，以不破坏本单目标为准并说明。

## 5. 依赖安装提示

本机 `files.pythonhosted.org` 对 Python 包是 403，但 **npm 不受影响**。
若需装包直接 `npm install` 即可；
若遇网络问题可用 `npm config set registry https://registry.npmmirror.com`。

> ⚠️ 提交前务必 `git diff --stat -- frontend_dashboard/package-lock.json`：
> npm 在 Windows 上会删除 lock 中的 `libc`/`musl` 平台标记（实测删了 42 行），
> 会让 Linux CI 上的 lock 不完整。**若出现这类删除，恢复原始 lock 再提交。**

## 6. 交付方式

1. 分支 `feat/order-09-bundle-optimization`（基于最新 `main`）
2. 提交信息写明改前/改后体积
3. 尝试 push + 开 PR 到 `main`
4. push 失败则把完整 diff 与体积对比写进本 issue 评论

---

**定位：这是纯性能优化，不改变任何功能与视觉。体积数字必须真实，功能不得回退。**
