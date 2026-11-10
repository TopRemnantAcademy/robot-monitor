"""Issue #74 -- `/history`'s `resolution` parameter parsing contract.

Locks the **acceptance criteria** (Issue #74 section Acceptance + section Constraints/Forbidden
items), not implementation details: changing the implementation should not turn red as long as the
semantics are unchanged.

Background (the real defect of the old implementation): `_parse_resolution` used `int(float(...))`
to finish, silently truncating non-integer multiples:
    '2.5s' -> int(2.5)  = 2   (user wants 2.5 seconds, actually queries 2 seconds)
    '1.5m' -> int(90.0) = 90  (happens to equal 90 seconds, but with no "this is an approximation" hint)
    '0.5m' -> int(30.0) = 30  (0.5 minute = 30 seconds, also no hint)
The consequence is that the number of points on the chart does not match the expectation, yet HTTP
200, no alarm -- the hardest class of silent degradation to track down.

Run (must be run separately; rationale in pytest.ini and the conftest.py header):
    python -m pytest backend_ingestion/tests/test_resolution_param.py -v

No PG / Redis / MQTT needed:
- the pure-parsing cases call `_parse_resolution` directly, zero external dependencies;
- the HTTP-layer cases use TestClient to hit the parameter-validation branch **before** the 401 --
  note that FastAPI's dependency-injection order is "route parameter parsing -> dependency (auth)",
  but this endpoint's `_parse_resolution` is **inside the function body** and after the auth
  dependency, so without a token you get 401, not 400. Here we deliberately bypass with a valid
  token; see the note below.
"""

from __future__ import annotations

import pytest

from app.main import _parse_resolution


# ------------------------------------------------------------------ valid whole numbers: behavior must not regress

class TestValidWholeNumbersUnchanged:
    """Constraint 1: must not change the existing behavior for valid integer values
    ('10s'->10, '1m'->60)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("10s", 10),      # spec/00 section 5.3 and the frontend Drawer default; pinned verbatim
            ("30s", 30),      # a valid intermediate value (named in the Issue acceptance criteria)
            ("1s", 1),        # the lower bound itself must pass
            ("1m", 60),       # minute conversion (named in the Issue acceptance criteria)
            ("5m", 300),
            ("10m", 600),
            # case / surrounding whitespace: strip + lower before parsing, existing behavior
            (" 10S ", 10),
            ("1M", 60),
        ],
    )
    def test_whole_numbers(self, raw: str, expected: int):
        assert _parse_resolution(raw) == expected


# ------------------------------------------------------------------ fractional multiples: must be rejected

class TestFractionalRejectedNotTruncated:
    """Core acceptance: '1.5m' / '2.5s' / '0.5m' must all raise ValueError, not silently truncate.

    These three are exactly the samples named in the Issue body. Under the old implementation their
    return values were 90 / 2 / 30 respectively -- this group would **fail** on the old
    implementation ('2.5s' -> 2), so it truly proves the fix works, rather than "rewritten but still
    truncating".
    """

    @pytest.mark.parametrize("raw", ["2.5s", "1.5m", "0.5m", "0.5s", "10.5s", "2.25m"])
    def test_fractional_raises(self, raw: str):
        with pytest.raises(ValueError):
            _parse_resolution(raw)

    def test_2_5s_not_silently_two(self):
        """'2.5s' must never be interpreted as 2 seconds (this is exactly where the old
        implementation's silent truncation crashed)."""
        with pytest.raises(ValueError):
            _parse_resolution("2.5s")

    def test_1_5m_not_silently_90(self):
        """'1.5m' equals 90 seconds numerically, but semantically it is "1.5 minutes" and should
        still be rejected: allowing it would make "non-integer input" a valid path again, re-blurring
        the boundary."""
        with pytest.raises(ValueError):
            _parse_resolution("1.5m")


# ------------------------------------------------------------------ lower bound: must not be relaxed

class TestSubSecondStillRejected:
    """Constraint 2: must not relax the lower bound (still forbidden sub-1s, to avoid crushing the
    query)."""

    @pytest.mark.parametrize("raw", ["500ms", "1ms", "999ms", "0ms"])
    def test_ms_rejected(self, raw: str):
        with pytest.raises(ValueError):
            _parse_resolution(raw)

    @pytest.mark.parametrize("raw", ["0s", "0m"])
    def test_zero_rejected(self, raw: str):
        """If '0s' were caught by max(1, ...) into 1 second, that equals "user says 0, system queries
        1 second" -- also a silent rewrite. Here we explicitly reject (the old implementation leaned
        on max(1,...) as a fallback, with unclear semantics)."""
        with pytest.raises(ValueError):
            _parse_resolution(raw)


# ------------------------------------------------------------------ fully illegal input

class TestMalformedRejected:
    """An input with an illegal format must raise ValueError, not TypeError / crash elsewhere."""

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "s",          # no number
            "m",
            "abc",        # no unit
            "10",         # missing unit (the number itself is valid, but the protocol requires a unit)
            "10x",        # unknown unit
            "s10",        # unit first
            "--5s",
            "1..5s",
            "nan s",
            "nans",
            "infs",
            "-infm",
            "1e3s",       # '1e3' is 1000.0, integer though it contains scientific notation -- we accept it,
                          # see TestEdgeNumericForms; we don't put '1e3s' here to avoid self-contradiction
        ][:-1],
    )
    def test_malformed_raises_valueerror(self, raw: str):
        with pytest.raises(ValueError):
            _parse_resolution(raw)


class TestEdgeNumericForms:
    """Boundary numeric forms: NaN/Inf must be explicitly intercepted into ValueError.

    If not intercepted, `int(float('nan'))` would raise ValueError but the message would be
    "cannot convert float NaN to integer", which does not match the "invalid resolution" semantics,
    and `float('inf')` goes down the same path. Here we just unify them into ValueError.
    """

    @pytest.mark.parametrize("raw", ["nans", "infs", "-infs", "-infm"])
    def test_non_finite_rejected(self, raw: str):
        with pytest.raises(ValueError):
            _parse_resolution(raw)

    def test_scientific_notation_whole_value_accepted(self):
        """'1e1s' = 10.0 seconds, the value happens to be an integer -- accept (no truncation, no
        ambiguity)."""
        assert _parse_resolution("1e1s") == 10

    def test_scientific_notation_fractional_rejected(self):
        """'1.5e0s' = 1.5 seconds -> reject. Scientific notation is not a reason to allow
        non-integers."""
        with pytest.raises(ValueError):
            _parse_resolution("1.5e0s")


# ------------------------------------------------------------------ HTTP layer: 400 vs 200

class TestHttpLayer:
    """Drive the "parse failure -> 400" chain **through a real FastAPI route**.

    Why we must go through the HTTP layer: the Issue acceptance criteria say "should return a 400
    error", not whether `_parse_resolution` raises or not. Between them there is a segment of
    `history()`'s `except ValueError: raise HTTPException(400, ...)` (main.py:227-229) -- only
    looking at the pure function would miss regressions like "forgot to catch the exception".

    Why override auth + patch the pool:
    - `history()`'s auth dependency `current_active_user` really queries the DB to confirm the user
      exists; this environment has no PG, so we override it to focus the case on **parameter
      validation**.
    - A valid resolution continues to `pool = await get_pool()` (main.py:250-252), which also needs a
      stand-in. Note `get_pool` is imported **inside the function** as `from app.db.session import
      get_pool`, so the patch must target the module attribute `app.db.session.get_pool`; patching
      `app.main.get_pool` is ineffective (observed: it would keep connecting to 5432 then 500).
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
        """Fractional multiple -> 400 (**not** 2xx; the old implementation's '2.5s' would have gone
        to the query)."""
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
            ("10s", "10s"),   # frontend Drawer default; echoed verbatim
            ("30s", "30s"),
            ("1s", "1s"),
            # WARNING: '1m' echoing as '60s' is **existing behavior** (the `main.py` response body
            # `f"{res_s}s"`), this issue does not change it: it is not "silent truncation" but
            # "normalized echo after unit conversion", and the frontend only displays resolution,
            # not using it in logic. We pin it here to prevent accidental change.
            ("1m", "60s"),
        ],
    )
    def test_whole_number_is_200(self, client, raw: str, echoed: str):
        """Valid integer -> 200, and the response resolution matches existing behavior (no
        regression)."""
        r = client.get("/robots/humanoid-001/history", params={"resolution": raw})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["resolution"] == echoed
        assert body["robot_id"] == "humanoid-001"
