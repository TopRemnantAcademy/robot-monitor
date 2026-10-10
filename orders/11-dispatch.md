# 派工单：订单 11 — 后端采集层 pytest 单元测试套件

> 依据：`STATUS.md` 阶段 C（质量保障）；后端目前只有静态检查 13/13，**无运行时测试**
> 触发方式：`post-issue-comment`（本文件**不含**触发词，避免重复触发构建）

---

## 0. 背景：后端目前只有静态检查

`backend_ingestion/` 迄今的验证手段是**静态校验**（跨模块关节键名一致、
INSERT 列/占位符/元组对齐、WS 三种 type、REST 路由）与 `py_compile`。
这些能抓结构错误，但**抓不到逻辑错误** —— 本轮已吃过亏：

- `_to_row` 曾用 flush 时刻 `now()` → PK `(time, robot_id)` 冲突 → `ON CONFLICT DO NOTHING` **静默丢行**
- `state_cache` 曾把 Redis Hash 值字符串化 → 前端收到 `status_code:"1"`

这两个都是静态检查看不出来、只有跑起来才会暴露的缺陷。本单**补上这层网**。

## 1. 任务：为 `backend_ingestion` 建立 pytest 测试套件

目录：`backend_ingestion/tests/`

### T1 — 必测的纯函数（价值最高，优先写）

| 目标 | 文件:行 | 要覆盖的用例 |
|---|---|---|
| `Telemetry` 校验 | `app/schemas/telemetry.py:27-58` | ① `max_joint_temp` 缺失时由 `joint_temperatures` 推导<br>② 三个关节字典键必须一致<br>③ `robot_id` 必须匹配 `^humanoid-[0-9]{3}$`（非法值应报错）<br>④ `battery_pct` 边界 0 / 100 / 越界 |
| `_parse_resolution` | `app/main.py:132-141` | `'10s'→10`、`'30s'→30`、`'1m'→60`、`'ms'→ValueError`、非法串→`ValueError` |
| `_as_dict` | `app/main.py:144-152` | dict 直通 / JSON 字符串解析 / 坏 JSON→`{}` / 其他类型→`{}` |
| `_to_row` | `app/services/batch_writer.py` | ① **优先用客户端 `ts`**（10Hz → 100ms 间隔，避免同批时间戳相同）<br>② `ts` 缺失时才回落 `now()`<br>③ 输出元组长度与 `INSERT_SQL` 占位符数量一致（=17） |

> ⚠️ 第 ④ 项的 ① 是**回归防护**：该缺陷已修过一次，必须有测试钉住，
> 否则将来重构时可能再次引入「静默丢行」。

### T2 — `BatchWriter` 的关闭语义（订单 07 新引入）

订单 07 给 `batch_writer.py` 加了 `_STOP` 哨兵与 `_drained` 事件，
因为原实现用 `_running=False` 标志位**唤不醒阻塞在 `queue.get()` 的 `run()`**（会永久挂起）。

必须覆盖：
1. `graceful_shutdown()` 能唤醒阻塞在空队列上的 `run()` 并使其退出
2. 哨兵**之后**仍在队列里的数据会被落库，不丢
3. `_drained` 在排空完成后置位

> 若这些测试写不出来（例如难以构造阻塞场景），**如实标注「未验证 + 原因」**，
> 不要写一个恒真的假测试充数。

### T3 — `state_cache` 类型契约（第二个已修缺陷的回归防护）

`app/services/state_cache.py` 曾把 Redis Hash 值写成字符串，
导致前端收到 `status_code:"1"` 而非 `1`。

必须覆盖：写入 Redis 后取回，`status_code` / `battery_pct` 等**数值字段仍是数值类型**，
不是字符串。

## 2. 依赖与运行方式（本机可跑，不要再说跑不起来）

主代理已实证：本机 `files.pythonhosted.org` 返回 403，但**镜像源可用**：

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  pytest pytest-asyncio fastapi pydantic pydantic-settings aiomqtt redis

cd backend_ingestion && PYTHONPATH=. python -m pytest tests/ -v
```

> 若 `asyncpg` 装不上：**用镜像源**，实测 `asyncpg==0.32.0` 在 Python 3.13.14 上 29 秒装完
> （旧版 0.29.0 才不支持 3.13）。
> 纯函数测试不依赖数据库，可用 stub 替掉 `app.db.session`。

## 3. 验收标准

- [ ] `backend_ingestion/tests/` 存在，且 `pytest` 能跑通
- [ ] T1 的 4 个目标**全部**有用例（尤其 `_to_row` 的 `ts` 优先）
- [ ] T3 的类型契约有用例（防止字符串化回归）
- [ ] 贴出**真实 `pytest -v` 输出**（不是"应该能过"）
- [ ] 每个失败用例若实在跑不通，标「未验证 + 所需条件」
- [ ] 新增 `backend_ingestion/pytest.ini` 或 `pyproject.toml` 段，保证 `PYTHONPATH=.`

## 4. 禁止项

1. **禁止**写恒真断言（如 `assert True`）充数
2. **禁止**为了让测试通过而放宽被测逻辑（如把 `ValueError` 改成返回默认值）
3. **禁止**修改被测函数的行为 —— 本单**只加测试**，不改实现
4. **禁止**伪造 pytest 输出
5. **禁止**只推分支就宣称完成 —— 尝试 push + 开 PR；因 `repo-code:rw` 失败则
   **原文报告错误**

## 5. 与并行订单的关系

订单 06（联调压测）、08（asyncpg 升级）、10（告警抑制）正在 CNB 执行。
- 与 08 可能同改 `requirements.txt`：若冲突，以**含 pytest 依赖 + `asyncpg==0.32.0`** 为准
- 本单**不改后端实现代码**，与 08 的冲突面很小

## 6. 交付方式

1. 分支 `feat/order-11-backend-tests`（基于最新 `main`）
2. 提交信息写明用例数与实测输出摘要
3. 尝试 push + 开 PR 到 `main`
4. push 失败则把完整 diff 与 pytest 输出写进本 issue 评论

---

**定位：你是在补一张能抓住"静默丢行""类型串味"这类缺陷的网。测试必须真的会失败—— 恒真的测试不如没有测试。**
