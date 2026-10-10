# 订单 23 — I-16 可判定的压测（k6 + xk6-mqtt）（🟡）

> 关联：INTEGRATION-PLAN §8.6；源 k6(go.k6.io/k6/v2) + xk6-mqtt 扩展。
> 触发方式：post-issue-comment（本文件不含触发词）。

## 0. 现状

- **缺口**：现有压测用 Erlang emqtt-bench「只吐数字」，无可判定断言，违反 R-07/R-08。
- **替换理由**：k6 用 JS `check()` 写断言 → 可判定通过/失败。
- **注意**：`mock_fleet.py` 不替换（24 关节真实 payload 只有自研能产生，I-4 判断不变）。
- **状态**：未验证（xk6-mqtt 需 Go 编译，未跑）。

## 1. 必读文件

| 顺序 | 文件 | 目的 |
|---|---|---|
| 1 | `backend_ingestion/app/main.py:85,133,250` | 健康/metrics/ws 端点（压测目标） |
| 2 | `external/k6/go.mod` | k6 v2 依赖证据 |
| 3 | `mock_fleet.py`（若存在） | 真实 payload 来源，压测脚本复用 |

## 2. 任务

### T1 — 压测脚本
- 新建 `tests/load/mqtt_pub.js`（k6 + xk6-mqtt）：模拟 N 台机器人以 10Hz 发遥测，
  用 `check()` 断言：P(连接成功)≥99%、P(发布延迟<Xms)≥95%、WS 收包率≥99%。

### T2 — 判定基准
- 输出**可判定数字**（非「只吐数字」）：列出阈值与实测，明确 PASS/FAIL。

### T3 — 不替换 mock_fleet
- 压测 payload 走 mock_fleet 真实 24 关节结构，不自造简化结构。

## 3. 验收标准

- [ ] `tests/load/mqtt_pub.js` 存在，含 `check()` 断言
- [ ] 给出可判定阈值与（若可跑）实测 PASS/FAIL
- [ ] xk6-mqtt 若无法编译，贴「未验证 + 条件（需 Go 编译 xk6）」

## 4. 禁止项

1. 禁止用「只吐数字」式压测糊弄（必须 check 断言）
2. 禁止替换 mock_fleet.py
3. 禁止伪造压测数字
4. 禁止只推分支就宣称完成

## 5. 交付方式

1. 分支 `feat/order-23-load-test`
2. 尝试 push + 开 PR

## 6. 失败上报

- xk6-mqtt 未编译 → 明确「脚本就绪，运行时未验证」
- 阈值设定依据写明

## 7. 定位一句话

你是验收方，证据优先：压测必须有可判定断言，不是吞吐数字。
