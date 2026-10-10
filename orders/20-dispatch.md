# 订单 20 — 修复 passlib/pkg_resources 在 setuptools≥81 / Python 3.13 下 import 失败

> 本派单文档不改动代码，仅作为给 CNB `@codebuddy` 的任务说明书。
> 阻塞由订单 15 的认证接入引入，必须在本单解决，否则后端无法启动、无法本地/CI 验证。

## 0. 当前状态

- **基线提交**：`main @ 0c909be` 之后已合入订单 15（JWT/RBAC 接入），本单基于该提交。
- **已完成（订单 15）**：`backend_ingestion/app/auth/*` 包、`main.py` 鉴权装配、
  `frontend_dashboard/src/auth/*`、`infra/init.sql` 的 `robot_user` 表。
- **阻塞现象**：订单 15 落地的 `fastapi-users[sqlalchemy]==12.1.3` 传递依赖
  `passlib 1.7.4`，后者在 `passlib/pwd.py:16` 执行 `import pkg_resources`。
  - 本机实测（Python 3.13.14，初始 venv 无 setuptools）：`import pkg_resources` →
    `ModuleNotFoundError: No module named 'pkg_resources'`，连锁导致
    `import fastapi_users` 失败 → uvicorn 启动即崩。
  - 已验证修复方向：安装 `setuptools==80.9.0`（`<81`）后 `import pkg_resources`
    可用（仅废弃警告），`import fastapi_users` 成功返回 `12.1.3`。
  - `setuptools 84.0.0` 已彻底移除 `pkg_resources`（警告原文：
    *"The pkg_resources package is slated for removal as early as 2025-11-30.
    Refrain from using this package or pin to Setuptools<81."*）。

## 1. 必读文件

| 顺序 | 文件 | 目的 |
|---|---|---|
| 1 | `backend_ingestion/requirements.txt` | 当前 `fastapi-users[sqlalchemy]==12.1.3` + `bcrypt==4.0.1` 钉版与注释 |
| 2 | `backend_ingestion/Dockerfile` | `FROM python:3.11-slim`（镜像自带 setuptools，但重建会拉最新，须由 requirements 钉死） |
| 3 | `backend_ingestion/app/auth/manager.py` | 密码哈希走框架默认 `PasswordHelper`（bcrypt），不自造 |
| 4 | `backend_ingestion/app/auth/backend.py` | fastapi-users Bearer/JWT 后端装配 |
| 5 | `backend_ingestion/tests/test_auth.py` | 认证单测，是回归护栏 |

## 2. 任务分解

### T1 — 在 `requirements.txt` 钉死 `setuptools<81`（主方案，最低改动面）

在 `fastapi-users[sqlalchemy]==12.1.3` 附近增加一行：

```
# passlib 1.7.4 在 passlib/pwd.py:16 import pkg_resources；setuptools>=81 已移除该模块，
# 导致 Python 3.12/3.13 + 新版 setuptools 下 import fastapi_users 直接失败。
# 钉 <81 可恢复 pkg_resources（仅废弃警告）。长期应走 T4 移除 passlib。
setuptools<81
```

保留 `bcrypt==4.0.1`（passlib 读 `bcrypt.__about__.__version__`，5.0.0 已删该属性，
降级 4.0.1 解决；详见 requirements.txt 现有注释）。

### T2 — 验证可导入（Python 3.13）

新建干净 venv，`pip install -r requirements.txt` 后执行：

```bash
python -c "import fastapi_users; print('fastapi_users OK', fastapi_users.__version__)"
```

必须成功且无 `ModuleNotFoundError: No module named 'pkg_resources'`。

### T3 — 跑认证单测

```bash
cd backend_ingestion && python -m pytest tests/test_auth.py -q
```

必须 collect 且通过。注意 `pytest.ini` 已把 `backend_ingestion/tests` 移出 `testpaths`
（避免两个同名顶层 `app` 包冲突）——**本单禁止改动这条**。

### T4（备选，仅当 T1 不足根除时）— 彻底移除 passlib

将 `fastapi-users` 升级到不再依赖 passlib 的版本（≥13，改用 `pwdlib`），并相应更新
`app/auth/*` 的密码哈希 API。必须保持 `RobotUser` / JWT 密钥复用 / `DEFAULT_ROLE='viewer'`
/ `on_after_register` 行为不变。此路径改动面更大、回归风险更高，仅作兜底，非首选。

## 3. 验收标准

- [ ] `requirements.txt` 含 `setuptools<81`；`bcrypt==4.0.1` 与 `fastapi-users[sqlalchemy]==12.1.3` 保持
- [ ] 干净 venv + `pip install -r requirements.txt` 后 `python -c "import fastapi_users"` 在 **Python 3.13** 成功
- [ ] 容器内（python:3.11-slim，重建后）`import fastapi_users` 同样成功（说明 `setuptools<81` 已覆盖镜像自带版本）
- [ ] `pytest backend_ingestion/tests/test_auth.py` 通过
- [ ] 订单 15 的 27 个文件**行为不变**：JWT 流程、`RobotUser` schema、前端 `LoginGate`/`useRobotWebSocket` 鉴权逻辑、init.sql 未被改动

## 4. 禁止项

1. 禁止用「日志无报错」代替通过判定
2. 禁止伪造数字；跑不出来写「未验证 + 所需条件」
3. 禁止改动订单 15 的认证 API 面（`RobotUser`、`DEFAULT_ROLE`、`on_after_register`、前端鉴权调用点），除非走 T4 且已显式说明
4. 禁止把 `bcrypt` 升到 `>4.0.1`（`__about__` 已删，5.0.0 会让 `/auth/register` 500）
5. 禁止在 `requirements.txt` 删除 `fastapi-users[sqlalchemy]==12.1.3` 行（除非走 T4 并验证 sqlalchemy 适配器仍可导入）
6. 禁止把 `pytest.ini` 的 `testpaths`/`norecursedirs` 改回包含 `backend_ingestion/tests`（两 `app` 包冲突）

## 5. 交付方式

1. 分支 `feat/order-20-passlib-pkgresources`
2. 尝试 push + 开 PR
3. push 失败 → 把 diff 与结论写进 issue 评论（不要只写「已推送」掩盖 403）

## 6. 失败上报要求

- 构建/导入失败必须原文贴 traceback
- 若 T1 验证仍失败，明确写出实测 `setuptools` 版本与 `import pkg_resources` 结果，不要静默

## 7. 定位一句话

你是验收方，证据优先：每个「已修复」结论必须附可复现的导入/测试命令与输出。
