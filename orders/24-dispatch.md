# 订单 24 — I-18 测试框架落地（🟡）

> 关联：INTEGRATION-PLAN §8.8；源 pytest + vitest + playwright + testing-library + msw。
> 触发方式：post-issue-comment（本文件不含触发词）。

## 0. 现状

- **已完成（部分）**：`pytest.ini` 已建、`backend_ingestion/tests/test_auth.py` 已建（29 passed）；
  `pytest.ini` 已把 `backend_ingestion/tests` 移出 testpaths（避免两同名 `app` 包冲突，勿改）。
- **缺口**：`alert_engine` 无 `tests/`；前端无 vitest/playwright；`.cnb.yml` 无 test job。
- **状态**：未验证（未建目录）。

## 1. 必读文件

| 顺序 | 文件 | 目的 |
|---|---|---|
| 1 | `pytest.ini` | 现有 testpaths / norecursedirs（勿改 testpaths） |
| 2 | `backend_ingestion/tests/test_auth.py` | 后端测试范式参考 |
| 3 | `frontend_dashboard/package.json` | 加 vitest/playwright 脚本的位置 |
| 4 | `.cnb.yml` | 加 test job 的位置（仓库根 .cnb.yml） |

## 2. 任务

### T1 — alert_engine 测试
- 新建 `alert_engine/tests/`，覆盖 `emit()` 的 inhibition/dedup/silence 顺序（与订单 17 的闭环协同）。

### T2 — 前端测试
- 前端加 `vitest` + `testing-library` + `msw`；为 `RobotDetailDrawer` 写一个组件测试。

### T3 — CI 门禁
- `.cnb.yml` 加 `test` job：跑 `pytest` 与 `npm test`；失败即红，禁止 `|| true`。

## 3. 验收标准

- [ ] `alert_engine/tests/` 存在且有可收集测试
- [ ] 前端 `npm test` 可跑（至少一个组件测试）
- [ ] `.cnb.yml` 含 test job；故意改坏一处应使 CI 红（贴日志）
- [ ] 未破坏现有 `pytest.ini` 的 testpaths 设置

## 4. 禁止项

1. 禁止改 `pytest.ini` 的 testpaths/norecursedirs（两 `app` 包冲突）
2. 禁止 `|| true` / continue-on-error 绕过门禁
3. 禁止伪造测试数字
4. 禁止只推分支就宣称完成

## 5. 交付方式

1. 分支 `feat/order-24-test-framework`
2. 尝试 push + 开 PR

## 6. 失败上报

- 前端测试若环境缺失，贴「未验证 + 条件」
- CI 失败原文报告

## 7. 定位一句话

你是验收方，证据优先：把 STATUS.md 的「11/11」「20/20」变成 CI 可复现数字。
