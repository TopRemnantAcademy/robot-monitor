# reports/ — 订单 06 收口产物

本目录是订单 06（端到端联调与压测）的**证据与报告**存放处。

| 文件 | 内容 |
|---|---|
| `order-06-integration-report.md` | 结构化集成报告（spec/ci/integration-call.md 格式 + T1~T4） |
| `perf_backend_offline.py` | 离线性能剖析脚本（实测 `BatchWriter` 的 CPU/组批/水位成分） |
| `evidence/perf-backend-offline.json` | 上述脚本的原始输出（measured / inferred 分栏） |
| `evidence/run0-no-docker.txt` | `test_integration.sh` 在**无 docker** 环境的输出（环境错误分栏证据） |
| `evidence/run1-with-mock-docker.txt` | 受控桩下第 1 次运行（验证脚本控制流与可重复性） |
| `evidence/run2-with-mock-docker.txt` | 受控桩下第 2 次运行（与第 1 次逐项判定一致） |

> `run1/run2` 用的是**假 docker**（PATH 前置的 mock 脚本），目的仅为验证
> `test_integration.sh` 的控制流、超时保护、清理逻辑与分栏判定**真的会执行到**，
> **不得**当作被测系统（01~05）的通过证据。
> 真实端到端结论必须来自具备 Docker daemon 的环境执行 `bash test_integration.sh`。
