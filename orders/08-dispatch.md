# 派工单：订单 08 — `asyncpg` 升级与本机验证通道打通（I-6）

> 依据：`external/INTEGRATION-PLAN.md` 的 **I-6** 章节、`external/SOURCES.md` §4
> 触发方式：`post-issue-comment`（本文件**不含**触发词，避免重复触发构建）

---

## 1. 任务：升级 `asyncpg` 0.29.0 → 0.32.0

**文件**：`backend_ingestion/requirements.txt`（当前 `asyncpg==0.29.0`）

### 为什么必须升（实证，非推测）

| 版本 | 发布日 | Python 支持 |
|---|---|---|
| `v0.29.0`（**我们锁的**） | 2023-11-05 | `requires-python = '>=3.8.0'`，**无 3.13 classifier** |
| `v0.30.0` | 2024-10-20 | **"Support Python 3.13 and PostgreSQL 17"** |
| `v0.31.0` | 2025-11-24 | + Python 3.14 |
| `v0.32.0`（最新） | 2026-10-06 | + Python 3.15 |

HEAD 版 `external/asyncpg/pyproject.toml` 的 classifiers 实证列出
`3.9 / 3.10 / 3.11 / 3.12 / 3.13 / 3.14`。

**关键认知纠正**：此前把「`asyncpg==0.29.0` 在 Python 3.13 上编译失败
（`_PyLong_AsByteArray` 签名变更）」记为**环境错误**，
实为**我们锁的版本过旧**。升 0.30+ 后该阻塞消失。

## 2. 附带成果：本机验证通道已打通（重要，请复用）

本机 `files.pythonhosted.org` 返回 **403**，但**清华镜像源可用**：

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple <package>
```

主代理已用该源在 **Python 3.13.14** 的隔离 venv 中实测：

```
aiomqtt==2.5.1   asyncpg==0.32.0   fastapi==0.143.0   redis==8.1.0   pydantic==2.14.0

[1] schemas.telemetry OK, joints = 24
[2] app.main import OK
[3] aiomqtt 2.5.1
[4] Client params: ['self', 'hostname', 'port', 'username']
```

**这意味着此前标注「未验证（本机无法运行）」的后端项，现在可以真实运行验证了。**
本单要求你**不要再说"本机跑不起来"**，用镜像源装依赖并给出实测输出。

## 3. 具体交付

1. `backend_ingestion/requirements.txt`：`asyncpg==0.29.0` → `asyncpg==0.32.0`
2. 用镜像源在隔离 venv 安装，并**实际运行**验证：
   - `python -c "import asyncpg; print(asyncpg.__version__)"`
   - `PYTHONPATH=. python -c "import app.main"`（在 `backend_ingestion/` 下）
   - 两项都要**贴出真实输出**
3. 若 `alert_engine/` 也有 `requirements.txt` 含 `asyncpg`，同步升级
4. 把镜像源用法写进 `backend_ingestion/README.md` 的「本地开发」章节
   （标注为可选加速，不改动默认 PyPI 源配置）

## 4. 验收标准

- [ ] `requirements.txt` 中 `asyncpg==0.32.0`
- [ ] 贴出 `import asyncpg` 的真实版本输出
- [ ] 贴出 `import app.main` 的成功输出（或失败原文）
- [ ] README 补充镜像源说明
- [ ] 改动附注释，注明依据（v0.30.0 release note "Support Python 3.13 and PostgreSQL 17"）

## 5. 禁止项

1. **禁止**用「本机环境问题」掩盖未验证 —— 镜像源已证明可用
2. **禁止**伪造输出；跑不通就贴错误原文并标「未验证 + 所需条件」
3. **禁止**同时升级其他依赖（`zustand` / `redis-py` / `fastapi` 已评估为不升，
   理由见 `external/INTEGRATION-PLAN.md` §3）
4. **禁止**只推分支就宣称完成 —— 尝试 push 并开 PR；因 `repo-code:rw` 失败则
   **原文报告错误**，不要静默结束

## 6. 与订单 07 的冲突处理

订单 07（aiomqtt 2.0.1 → 2.5.1）也改 `requirements.txt`。
若两者产生冲突，**以同时包含两项的版本为准**：

```
aiomqtt==2.5.1
asyncpg==0.32.0
```

## 7. 交付方式

1. 分支 `feat/order-08-asyncpg-upgrade`（基于最新 `main`）
2. 提交信息写明版本与依据
3. 尝试 push + 开 PR 到 `main`
4. push 失败则把完整 diff 与验证结论写进本 issue 评论

---

**定位：把「环境错误」纠正为「版本问题」，并用镜像源把验证真正跑起来。证据优先。**
