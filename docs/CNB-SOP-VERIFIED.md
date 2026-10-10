# 交付物的「可验证性」分层（CodeBuddy 自用纪律）

> 背景：本仓库历史上出现过多次「NPC 报 success，但分支没合并 / 配置是臆造的」。
> `docs/CNB-DISPATCH-SOP.md` 已给了回收流程，本文补充**交付物本身的验证分层**，
> 用于判断一份 PR 里哪些结论可以直接采信。

## 三层

| 层 | 含义 | 举例 |
|---|---|---|
| **L1 实测** | 本机跑过命令/读过程序输出，可复现 | `python3 -c "json.load(...)"` 通过；`git rev-parse HEAD` 拿到 commit |
| **L2 静态实证** | 引用上游 **源码/文档的具体 file:line**，未运行 | `emqx_prometheus_api.erl:63` 证明端点路径 |
| **L3 待验证** | 只有推理，或需要 Docker/镜像/真机才能验 | 「Grafana 能连上 TimescaleDB」 |

**规则**：L3 必须单列，且写明「还需什么才能变成 L1/L2」。

## 本仓库的两个已知陷阱

### 陷阱 1：`--depth 1` 钉的是「当下 HEAD」，不是版本

实证：`external/aiomqtt` 裸 clone 后 HEAD 已漂到 `3.0.0-alpha.1`，
其中 `docs/reconnection.md` **不存在**（已并进 `guides.md`）——
导致 I-1/I-2 的 file:line 证据指向一个根本不存在的文件。

→ 要核对某个**锁定版本**的 API，必须 `--branch <tag>` 或 `checkout <tag>`。

### 陷阱 2：裸镜像名会解析到**别的东西**

实证：`emqx:5.6` 与 `emqx/emqx:5.6` 的 digest 完全不同
（`sha256:06f2341d…` vs `sha256:c5a4118b…`），前者根本不是 MQTT broker。

→ 新增任何镜像前先 `docker manifest inspect`。本次新增的 6 个运维镜像
**尚未做这一步**，已在 PR 里列为 L3。
