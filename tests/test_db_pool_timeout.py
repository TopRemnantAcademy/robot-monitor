"""回归测试：DB 未就绪时，建池必须是**有界失败**，不能挂死。

为什么需要这个测试
------------------
线上症状（CNB 流水线 `e2e-30k-landing`，构建 `cnb-05c-1k4k55gpd`）：

    [e2e] 栈已起（54.2s）
    [e2e] 链路失败：后端 /healthz 在 180s 内未达到 db=up（最后快照：None）

`最后快照：None` 说明 /healthz **一次都没答上来**，而不是答了 `db=down`。
根因不在 e2e，而在后端的探活路径：

1. asyncpg 0.32 起 `init_size` 是「建池即预连」（`app/db/pool.py` 引的官方
   ``.. versionchanged:: 0.32.0``），它把 `connection.connect()` 的默认
   **`timeout=60`** 带进建池过程（`asyncpg/connection.py` 的 `connect(..., timeout=60)`）；
2. `backend_ingestion/app/db/session.py:get_pool()` 用的正是 `init_size=min_size=2`
   —— 建池即预连 2 条；
3. 建池在**探活路径**上：`/healthz` → `_db_healthy()` → `ping()` → `get_pool()`。

三者叠加：DB 未就绪时每次探测阻塞 60s；探针/编排器的超时一旦取消到那句
`await create_pool(...)`，池没有建成、`_pool` 也没被赋值，下一次探测从头再等 60s
—— `db=up` 永远不会到达。**加一个建池超时**即变成有界失败（8s 抛 TimeoutError
→ `ping()` 返回 False → `/healthz` 老实报 `db=down` → 下个周期自愈）。

本测试盯住的就是"这个护栏还在不在"，因为它是**静默**的：删掉 `timeout=` 后
功能测试全绿，只有 DB 慢启动时才炸，且炸法（探针挂死）离因很远。

依赖与口径
----------
- **不依赖 Docker / 真实 PostgreSQL**：用一个「连不上的地址」触发建池失败即可。
  用**黑洞地址**（RFC 5737 TEST-NET）而不是 `127.0.0.1:1`，因为后者是
  `ConnectionRefusedError` 立刻返回，**测不出 60s** 这个关键量。
- 建池超时下限放得很宽（默认 25s）：CI 机器可能慢；只要"远小于 60s"就算护栏还在。
  真正要区分的是 `0~10s`（有护栏）与 `60s`（无护栏），两者差 6 倍以上。
- 反向验证过：删掉 `timeout=` 后本测试会因 60s > 25s 而 FAIL（见 PR 描述）。

运行
----
    python -m pytest tests/test_db_pool_timeout.py -v      # 有 pytest 时
    python tests/test_db_pool_timeout.py                   # 无 pytest 时（自带 main）

依赖：asyncpg（与运行期同版本）+ 标准库。**无 asyncpg 时 skip（退出码 3）**，
不把"没装依赖"伪装成失败。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = ROOT / "backend_ingestion"
SESSION_PY = BACKEND_ROOT / "app" / "db" / "session.py"
BACKEND_POOL_PY = BACKEND_ROOT / "app" / "db" / "pool.py"
BACKEND_CONFIG_PY = BACKEND_ROOT / "app" / "config.py"
ALERT_POOL_PY = ROOT / "alert_engine" / "app" / "pool.py"
ALERT_DB_PY = ROOT / "alert_engine" / "app" / "db.py"
ALERT_CONFIG_PY = ROOT / "alert_engine" / "app" / "config.py"

# RFC 5737 TEST-NET-1：保证不可达、且**不是**立刻 refused（那会掩盖 60s 病征）。
BLACKHOLE_DSN = "postgresql://u:p@192.0.2.1:5432/robot_monitor"
# 建池超时的上限。远低于 asyncpg 默认的 60s，也远高于本仓默认的 8s。
MAX_ACCEPTABLE_POOL_TIMEOUT_S = 25.0
# 本仓默认建池超时（与 backend_ingestion/app/config.py 的 db_connect_timeout_s 对齐）
EXPECTED_DEFAULT_TIMEOUT_S = 8.0


# 环境不具备时的信号。**两套运行方式都要认**：
#   - `python tests/<file>.py`（自带 main 的脚本 runner）：把 Skip 当作 skip，退出码 3；
#   - `python -m pytest`：pytest 只认它自己的 `Skipped`，自定义异常会被记成 FAILED。
#     所以这里让 Skip 直接继承 `pytest.skip.Exception`（pytest 可用时），
#     使「缺依赖」在两种入口下都表现为 skip 而不是红。
try:  # pragma: no cover - 取决于是否装了 pytest
    from _pytest.outcomes import Skipped as _PytestSkipped

    class Skip(_PytestSkipped):  # type: ignore[misc]
        """环境不具备 —— 不是代码失败（pytest 下表现为 SKIPPED）。"""
except Exception:  # noqa: BLE001 - 无 pytest 时退回普通异常，脚本 runner 仍可用
    class Skip(Exception):  # type: ignore[no-redef]
        """环境不具备 —— 不是代码失败。"""


def _asyncpg():
    try:
        import asyncpg  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - 取决于环境
        raise Skip(
            "未安装 asyncpg，无法实测建池阻塞时长。"
            "需要：pip install asyncpg==0.32.0（与 backend_ingestion/requirements.txt 同版本）。"
        ) from exc
    return asyncpg


def test_asyncpg_default_connect_timeout_is_60() -> None:
    """把你我引用的那个「60」钉成可执行事实，防止 asyncpg 改了默认值后注释变谎话。

    仓库多处注释都写着「connection.connect() 的默认 timeout=60」。这条断言让
    那句话有机器可证的落点：默认值变了，测试先红，人再去改注释。
    """
    asyncpg = _asyncpg()
    import inspect  # noqa: PLC0415

    sig = inspect.signature(asyncpg.connection.connect)
    default = sig.parameters["timeout"].default
    assert default == 60, (
        f"asyncpg.connection.connect 的 timeout 默认值已变成 {default!r}（不再是 60）。"
        "请同步修正 app/db/session.py 与 app/db/pool.py 里关于「默认 60s」的注释，"
        "并重新评估 db_connect_timeout_s 的取值。"
    )
    assert asyncpg.__version__, "asyncpg.__version__ 为空，无法记录实测版本"


def test_backend_create_pool_passes_timeout() -> None:
    """`backend_ingestion/app/db/session.py` 建池必须显式带 `timeout=`。

    这条是**静态**断言（AST 解析真实调用点），不依赖网络：即使 CI 里跑不到
    黑洞地址也不会漏掉「有人把 timeout 删了」。
    """
    src = SESSION_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_pool"
    ]
    assert calls, f"{SESSION_PY} 里找不到 asyncpg.create_pool() 调用点"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "init_size" not in kwargs, (
            "backend 的写路径建池用了 init_size —— 那是「建池即预连」，"
            "会把 connection.connect() 的默认 timeout=60 带进来，"
            "必须同时给出 timeout=（或改用 app/db/pool.py 的统一入口）。"
        )
        assert "timeout" in kwargs, (
            f"{SESSION_PY}:{call.lineno} 的 create_pool() 没有 timeout= 参数。"
            "asyncpg 0.32 下建池会走 connection.connect() 的默认 timeout=60："
            "DB 未就绪时每次探测阻塞 60s，/healthz 会挂死而不是报 db=down。"
            "请传 timeout=settings.db_connect_timeout_s。"
        )


def test_backend_config_declares_connect_timeout() -> None:
    """`db_connect_timeout_s` 必须真的存在于 Settings 上。

    本仓已有一次反面教材：`alert_engine/app/db.py` 引用了两个**从未声明**的
    Settings 键（见下面 test_alert_settings_keys_declared），一碰就 AttributeError。
    这里顺手把 backend 侧也钉住，不让同一类错误换个模块重演。
    """
    src = BACKEND_CONFIG_PY.read_text(encoding="utf-8")
    assert re.search(r"^\s*db_connect_timeout_s\s*:\s*float\s*=", src, re.M), (
        f"{BACKEND_CONFIG_PY} 未声明 db_connect_timeout_s —— "
        "但 app/db/session.py 正引用它（缺声明会 AttributeError 起不来）。"
    )


def _declared_settings_keys(config_py: Path) -> set[str]:
    """从 config.py 里抽出 BaseSettings 子类的已声明字段名（AST，不用正则硬凑）。"""
    tree = ast.parse(config_py.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        if not any(
            (isinstance(b, ast.Name) and b.id == "BaseSettings")
            or (isinstance(b, ast.Attribute) and b.attr == "BaseSettings")
            for b in cls.bases
        ):
            continue
        # 有注解即为 pydantic 字段
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                keys.add(stmt.target.id)
    return keys


def test_alert_settings_keys_declared() -> None:
    """`alert_engine/app/db.py` 引用的 settings 键必须都在 config.py 里声明过。

    这是本仓**真实存在过**的缺陷（main 上就有，本测试连带修掉）：
    `db.py:39-40` 读 `settings.db_pool_size` / `settings.db_command_timeout`，
    而 `config.py` 里两个都没声明 → 本机实证：

        asyncio.run(get_pool())
        AttributeError: 'Settings' object has no attribute 'db_pool_size'

    单测发现不了，是因为 `alert_engine/tests/conftest.py` 把 `app.db` **整个 stub 掉**了。
    所以这条必须用静态检查补上：它检验的是「引用与声明一致」这个契约。
    """
    declared = _declared_settings_keys(ALERT_CONFIG_PY)
    assert declared, f"没能从 {ALERT_CONFIG_PY} 解析出任何 Settings 字段，检查解析逻辑"

    used = set(re.findall(r"settings\.([A-Za-z_][A-Za-z0-9_]*)", ALERT_DB_PY.read_text(encoding="utf-8")))
    assert used, f"{ALERT_DB_PY} 里没找到 settings.<key> 引用点"

    missing = sorted(k for k in used if k not in declared)
    assert not missing, (
        f"{ALERT_DB_PY} 引用了 {ALERT_CONFIG_PY} 未声明的 Settings 键：{missing}。"
        "pydantic-settings 对缺声明字段不会给默认值，运行到就是 AttributeError。"
    )


def test_shared_pool_factory_has_connect_timeout_knob() -> None:
    """`app/db/pool.py`（backend 与 alert_engine 各一份）必须暴露 connect_timeout 并透传。

    这两份 pool.py 是「建池唯一入口」的封装。护栏必须建在**入口**上：
    只修某一个调用点，下一个调用方照样能踩进 60s。
    """
    for path in (BACKEND_POOL_PY, ALERT_POOL_PY):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fns = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_pool"
        ]
        assert fns, f"{path} 里找不到 async create_pool()"
        fn = fns[0]
        params = {a.arg for a in fn.args.kwonlyargs} | {a.arg for a in fn.args.args}
        assert "connect_timeout" in params, (
            f"{path}: create_pool() 未暴露 connect_timeout 参数 —— "
            "调用方就没有办法给建池设上限，只能吃 connection.connect() 的默认 60s。"
        )

        call = next(
            (
                n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "create_pool"
            ),
            None,
        )
        assert call is not None, f"{path}: create_pool() 内部没找到 asyncpg.create_pool() 调用"
        kwnames = {kw.arg for kw in call.keywords}
        assert "timeout" in kwnames, (
            f"{path}: 内部 asyncpg.create_pool() 没有透传 timeout= —— "
            "connect_timeout 形参成了摆设，60s 病征照旧。"
        )


def test_pool_build_time_is_bounded() -> None:
    """**实测**：对不可达地址建池必须在 MAX_ACCEPTABLE_POOL_TIMEOUT_S 内失败。

    用法与 `backend_ingestion/app/db/session.py:get_pool()` 对齐
    （`init_size=min_size=2`，即「建池即预连」那条路），只把地址换成黑洞地址。
    这条是整套护栏的端到端证据：前面的静态断言只证明"参数写了"，
    这条证明"写了确实管用"。
    """
    asyncpg = _asyncpg()
    import asyncio  # noqa: PLC0415
    import time  # noqa: PLC0415

    observed: dict[str, float | str] = {}

    async def probe() -> None:
        started = time.monotonic()
        try:
            pool = await asyncpg.create_pool(
                dsn=BLACKHOLE_DSN,
                min_size=2,
                max_size=10,
                # 与 session.get_pool() 一致的写法（含 timeout）
                timeout=EXPECTED_DEFAULT_TIMEOUT_S,
            )
            # 走到这里说明黑洞地址居然连上了（网络环境极不寻常），
            # 不能算通过 —— 本测试的前提没成立。
            await pool.close()
            observed["elapsed"] = time.monotonic() - started
            observed["result"] = "connected-unexpectedly"
        except Exception as exc:  # noqa: BLE001 - 任何异常都算「有界失败」，类型不重要
            observed["elapsed"] = time.monotonic() - started
            observed["result"] = type(exc).__name__

    asyncio.run(probe())
    elapsed = float(observed["elapsed"])
    print(
        f"  [实证] 对 {BLACKHOLE_DSN.split('@')[-1]} 建池："
        f"{observed['result']}，耗时 {elapsed:.2f}s",
        flush=True,
    )

    assert observed["result"] != "connected-unexpectedly", (
        f"黑洞地址 {BLACKHOLE_DSN} 竟然连通了，本测试前提不成立。"
        "请换一个确定不可达的地址（保持 RFC 5737 网段）。"
    )
    assert elapsed < MAX_ACCEPTABLE_POOL_TIMEOUT_S, (
        f"建池耗时 {elapsed:.2f}s，超过上限 {MAX_ACCEPTABLE_POOL_TIMEOUT_S}s。\n"
        f"这几乎就是 asyncpg `connection.connect()` 的默认 timeout=60 —— "
        f"说明建池超时护栏失效了：DB 未就绪时 /healthz 会被拖死，"
        f"报不出 db=down 也等不到 db=up。\n"
        f"修法：create_pool(..., timeout=<小于 60 的值>)，见 "
        f"{SESSION_PY.relative_to(ROOT)}。"
    )


def main() -> int:
    checks = [
        ("asyncpg connect 默认 timeout 仍为 60（注释有据）",
         test_asyncpg_default_connect_timeout_is_60),
        ("backend 建池显式带 timeout=", test_backend_create_pool_passes_timeout),
        ("backend Settings 声明 db_connect_timeout_s", test_backend_config_declares_connect_timeout),
        ("alert_engine 引用的 Settings 键都已声明", test_alert_settings_keys_declared),
        ("两份 pool.py 都暴露并透传 connect_timeout",
         test_shared_pool_factory_has_connect_timeout_knob),
        ("实测：建池阻塞时长有界（< 25s）", test_pool_build_time_is_bounded),
    ]
    failed = 0
    skipped = 0
    for name, fn in checks:
        try:
            fn()
            print(f"  PASS  {name}")
        except Skip as exc:
            skipped += 1
            print(f"  SKIP  {name}\n        {exc}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}\n        {exc}")
    print(f"\n{len(checks) - failed - skipped}/{len(checks)} 通过"
          f"{f'，{skipped} 跳过' if skipped else ''}")
    if failed:
        return 1
    return 3 if skipped == len(checks) else 0


if __name__ == "__main__":
    sys.exit(main())
