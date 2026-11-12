# RobotMonitor 需求索引（docs/requirements/）

> 每个需求一份独立文档，文件名 `R-<主题>.md`。本地实现（Agent 模式），未经用户确认不推送 CNB。

| 文档 | 主题 | 状态 | 关键门禁 |
|---|---|---|---|
| [R-VLA-EVAL.md](./R-VLA-EVAL.md) | RobotMonitor 作为 VLA 评估可观测性底座 | Phase 1 实现中 | 禁手搓 · 真开源 · 引入来源清单表（rosbag2/rosetta/LeRobot/statsmodels）· Rust/Python 双后端零偏差 |

## 通用门禁（所有 R-* 适用）
- 任何「统计 / ROS bag 转换 / VLA 数据集格式 / 加密 / HTTP 客户端 / 限流」类能力，**优先复用真实开源组件**，禁止自写等价实现。
- 每个新增模块的 PR/交付必须附「引入来源清单表」（组件 / 镜像全名+tag / 官方 URL / 是否实际拉取 / 是否有本地改写）。
- 配置片段凡官方有样例，必须取自官方文档/仓库；无现成开源组件的能力不自己实现，列「未找到可复用实现」清单。
- CI/质量门禁沿用项目既有约定（ruff / pre-commit / 无 AI 水印 / Bearer push）。
