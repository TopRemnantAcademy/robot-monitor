"""Issue #74 —— `/history` 的 `resolution` 参数解析契约。

锁定的是**验收标准**（Issue #74 §验收标准 + §约束/禁止项），不是实现细节：
改实现只要语义不变就不该红。

背景（旧实现的真实缺陷）：`_parse_resolution` 用 `int(float(...))` 收尾，
把非整数倍**静默截断**：
    '2.5s' -> int(2.5)  = 2   （用户要 2.5 秒，实际查 2 秒）
    '1.5m' -> int(90.0) = 90  （碰巧等于 90 秒，但没有任何"这是近似"的提示）
    '0.5m' -> int(30.0) = 30  （0.5 分钟 = 30 秒，同样无提示）
后果是图上点数与预期对不上，但 HTTP 200、无告警 —— 最难查的一类静默降级。

跑法（**必须单独跑**，理由见 pytest.ini 与 conftest.py 头注释）：
    python -m pytest backend_ingestion/tests/test_resolution_param.py -v

不需要 PG / Redis / MQTT：
- 纯解析用例直接调 `_parse_resolution`，零外部依赖；
- HTTP 层用例用 TestClient 打 401 之前的**参数校验**分支 —— 注意
  FastAPI 的依赖注入顺序是「路由参数解析 → 依赖（鉴权）」，但本端点的
  `_parse_resolution` 在**函数体内**且位于鉴权依赖之后，故未带 token 时
  拿到的是 401 而非 400。这里刻意用带合法 token 的方式绕过，见下文说明。
"""

from __future__ import annotations

import pytest

from app.main import _parse_resolution


# ------------------------------------------------------------------ 合法整数：行为不得回退

class TestValidWholeNumbersUnchanged:
    """约束项 1：不得改动合法整数值（'10s'→10、'1m'→60）的既有行为。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("10s", 10),      # spec/00 §5.3 与前端 Drawer 的默认值，逐字钉住
            ("30s", 30),      # 合法中间值（Issue 验收标准点名）
            ("1s", 1),        # 下限本身必须可过
            ("1m", 60),       # 分钟换算（Issue 验收标准点名）
            ("5m", 300),
            ("10m", 600),
            # 大小写 / 前后空白：解析前 strip + lower，属既有行为，一并钉住
            (" 10S ", 10),
            ("1M", 60),
        ],
    )
    def test_whole_numbers(self, raw: str, expected: int):
        assert _parse_resolution(raw) == expected


# ------------------------------------------------------------------ 非整数倍：必须拒绝

class TestFractionalRejectedNotTruncated:
    """验收标准核心：'1.5m' / '2.5s' / '0.5m' 一律 ValueError，**不得静默截断**。

    这三条正是 Issue 正文点名的样例。旧实现下它们的返回值分别是
    90 / 2 / 30 —— 本组用例在旧实现上**必定失败**（'2.5s' -> 2），
    故能真正证明修复生效，而不是"换个写法仍然截断"。
    """

    @pytest.mark.parametrize("raw", ["2.5s", "1.5m", "0.5m", "0.5s", "10.5s", "2.25m"])
    def test_fractional_raises(self, raw: str):
        with pytest.raises(ValueError):
            _parse_resolution(raw)

    def test_2_5s_not_silently_two(self):
        """'2.5s' 绝不能被解释成 2 秒（旧实现的静默截断就是这里翻车的）。"""
        with pytest.raises(ValueError):
            _parse_resolution("2.5s")

    def test_1_5m_not_silently_90(self):
        """'1.5m' 数值上虽等于 90 秒，但语义是"1.5 分钟"，仍应拒绝：
        放行等于让"非整数输入"成为合法路径，边界重新模糊。"""
        with pytest.raises(ValueError):
            _parse_resolution("1.5m")


# ------------------------------------------------------------------ 下限：不得放宽

class TestSubSecondStillRejected:
    """约束项 2：不得放宽下限（仍禁止 sub-1s，避免压垮查询）。"""

    @pytest.mark.parametrize("raw", ["500ms", "1ms", "999ms", "0ms"])
    def test_ms_rejected(self, raw: str):
        with pytest.raises(ValueError):
            _parse_resolution(raw)

    @pytest.mark.parametrize("raw", ["0s", "0m"])
    def test_zero_rejected(self, raw: str):
        """'0s' 若被 max(1, ...) 兜成 1 秒，就等于"用户说 0、系统查 1 秒"——
        同样是静默改写。这里明确拒绝（旧实现靠 max(1,...) 兜底，语义也不清晰）。"""
        with pytest.raises(ValueError):
            _parse_resolution(raw)


# ------------------------------------------------------------------ 完全非法输入

class TestMalformedRejected:
    """格式非法的输入必须 ValueError，而不是抛 TypeError / 崩在别处。"""

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "s",          # 无数字
            "m",
            "abc",        # 无单位
            "10",         # 缺单位（数字本身合法，但协议要求带单位）
            "10x",        # 未知单位
            "s10",        # 单位在前
            "--5s",
            "1..5s",
            "nan s",
            "nans",
            "infs",
            "-infm",
            "1e3s",       # '1e3' 是 1000.0，虽是整数但含科学计数法 —— 我们接受它，
                          # 见 TestEdgeNumericForms；此处不放 '1e3s' 以免自相矛盾
        ][:-1],
    )
    def test_malformed_raises_valueerror(self, raw: str):
        with pytest.raises(ValueError):
            _parse_resolution(raw)


class TestEdgeNumericForms:
    """边界数字形态：NaN/Inf 必须被显式拦成 ValueError。

    不拦的话 `int(float('nan'))` 会抛 ValueError 但错误信息是
    "cannot convert float NaN to integer"，与「invalid resolution」语义不符，
    且 `float('inf')` 走的也是同一条路径。这里统一归到 ValueError 即可。
    """

    @pytest.mark.parametrize("raw", ["nans", "infs", "-infs", "-infm"])
    def test_non_finite_rejected(self, raw: str):
        with pytest.raises(ValueError):
            _parse_resolution(raw)

    def test_scientific_notation_whole_value_accepted(self):
        """'1e1s' = 10.0 秒，值恰好是整数 —— 接受（不是截断，也无歧义）。"""
        assert _parse_resolution("1e1s") == 10

    def test_scientific_notation_fractional_rejected(self):
        """'1.5e0s' = 1.5 秒 → 拒绝。科学计数法不构成放行非整数的理由。"""
        with pytest.raises(ValueError):
            _parse_resolution("1.5e0s")


# ------------------------------------------------------------------ HTTP 层：400 vs 200

class TestHttpLayer:
    """把「解析失败 → 400」这条链路**走一遍真实 FastAPI 路由**验证。

    为什么必须走 HTTP 层：Issue 验收标准写的是「应返回 400 错误」，
    不是在说 `_parse_resolution` 抛不抛异常。两者之间还有一段
    `history()` 的 `except ValueError: raise HTTPException(400, ...)`
    （`main.py:227-229`）—— 只看纯函数会漏掉「忘了接住异常」这类回归。

    为什么 override 鉴权 + 打补丁 pool：
    - `history()` 的鉴权依赖 `current_active_user` 会真的查库确认用户存在，
      本环境无 PG；override 掉它，把用例聚焦在**参数校验**这一段。
    - 合法 resolution 会继续走到 `pool = await get_pool()`（`main.py:250-252`），
      同样需要替身。注意 `get_pool` 是**函数内** `from app.db.session import get_pool`，
      故补丁必须打在 `app.db.session.get_pool` 这个模块属性上，
      打在 `app.main.get_pool` 上无效（实测：会继续连 5432 然后 500）。
    """

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app.auth.deps import current_active_user
        from app.auth.users import RobotUser
        from app.main import app
        import app.db.session as dbsession

        async def _fake_user():
            return RobotUser(
                id=42,
                email="resolution-test@example.com",
                hashed_password="not-used",
                is_active=True,
                is_superuser=False,
                is_verified=True,
                role="viewer",
            )

        class _FakeConn:
            async def fetch(self, *_a, **_k):
                return []

        class _FakePool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self):
                        return _FakeConn()

                    async def __aexit__(self, *_exc):
                        return False

                return _Ctx()

        async def _fake_get_pool():
            return _FakePool()

        app.dependency_overrides[current_active_user] = _fake_user
        _saved = dbsession.get_pool
        dbsession.get_pool = _fake_get_pool
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
        dbsession.get_pool = _saved
        app.dependency_overrides.clear()

    @pytest.mark.parametrize("raw", ["2.5s", "1.5m", "0.5m", "0.5s", "10.5s"])
    def test_fractional_is_400_not_truncated(self, client, raw: str):
        """非整数倍 → 400（**不是** 2xx；旧实现在这里的 '2.5s' 会走到查询）。"""
        r = client.get("/robots/humanoid-001/history", params={"resolution": raw})
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == f"invalid resolution: {raw}"

    @pytest.mark.parametrize("raw", ["500ms", "abc", "", "10"])
    def test_malformed_is_400(self, client, raw: str):
        r = client.get("/robots/humanoid-001/history", params={"resolution": raw})
        assert r.status_code == 400, r.text

    @pytest.mark.parametrize(
        ("raw", "echoed"),
        [
            ("10s", "10s"),   # 前端 Drawer 默认值，逐字回显
            ("30s", "30s"),
            ("1s", "1s"),
            # ⚠️ '1m' 回显为 '60s' 是**既有行为**（`main.py` 响应体 `f"{res_s}s"`），
            # 本单不改：它不是「静默截断」，而是「单位换算后的规范化回显」，
            # 且前端拿 resolution 只做展示、不参与逻辑。此处一并钉住，防止误改。
            ("1m", "60s"),
        ],
    )
    def test_whole_number_is_200(self, client, raw: str, echoed: str):
        """合法整数 → 200，且响应 resolution 与既有行为一致（不得回退）。"""
        r = client.get("/robots/humanoid-001/history", params={"resolution": raw})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["resolution"] == echoed
        assert body["robot_id"] == "humanoid-001"
