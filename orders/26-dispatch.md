# 订单 26 — I-20 前端健壮性（🟢）

> 关联：INTEGRATION-PLAN §8.10；源 react-error-boundary + tanstack/query。
> 触发方式：post-issue-comment（本文件不含触发词）。

## 0. 现状

- **缺口**：前端无全局错误边界，`/history` 等请求无重试/缓存（审计 #23/#24）。
- **约束**：**不替换 zustand**（query 管服务端缓存，zustand 管 WS 节流后客户端状态，互补）。
- **状态**：未验证（未实装）。

## 1. 必读文件

| 顺序 | 文件 | 目的 |
|---|---|---|
| 1 | `frontend_dashboard/src/App.tsx` | 外层包 ErrorBoundary 的位置 |
| 2 | `frontend_dashboard/src/components/RobotDetailDrawer.tsx` | /history 请求改交 query 管缓存/重试 |
| 3 | `frontend_dashboard/package.json` | 加依赖位置 |

## 2. 任务

### T1 — 错误边界
- 装 `react-error-boundary`，在 `App.tsx` 外层包 `ErrorBoundary`，崩了给可恢复 UI（非白屏）。

### T2 — 服务端状态
- 装 `@tanstack/react-query`，把 `RobotDetailDrawer` 的 `/history` 请求交给 query（缓存+重试+失败态）。

### T3 — 不破坏现有
- 保留 zustand（WS 节流状态）；query 只管服务端请求。

## 3. 验收标准

- [ ] `App.tsx` 有 ErrorBoundary 包裹
- [ ] `/history` 走 react-query（含 retry/缓存），zustand 仍用于 WS 状态
- [ ] `tsc --noEmit` 0 错误；`vite build` RC=0
- [ ] 故意在 /history 接口返回 500，UI 显示错误态而非崩溃（贴验证）

## 4. 禁止项

1. 禁止升级/替换 zustand v5（前轮决策不变）
2. 禁止只推分支就宣称完成
3. 禁止伪造验证

## 5. 交付方式

1. 分支 `feat/order-26-frontend-robustness`
2. 尝试 push + 开 PR

## 6. 失败上报

- 构建/类型失败原文报告
- 无法实跑 500 路径时贴「未验证 + 条件」

## 7. 定位一句话

你是验收方，证据优先：错误边界与 query 缓存都要有可复现的失败态验证。
