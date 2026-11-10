"""Test harness: lets backend_ingestion's `app` package coexist with alert_engine's
same-named package.

The problem (structural to this repo, not introduced by this change):
Both modules have a top-level package named `app` -- `backend_ingestion/app` and
`alert_engine/app`. When running `pytest` once from the repo root, whichever is
imported first permanently occupies `sys.modules["app"]`, and the other's
`import app.xxx` all fail (observed `ModuleNotFoundError: No module named 'app.rules'`
and `'app.db' is not a package`). The only reason "running from root used to pass"
was that back then **only** alert_engine had an `app` package.

Solution: this directory's `app` package is re-registered rooted at this directory,
overriding `sys.modules["app"]`; at the same time `backend_ingestion` is inserted at
`sys.path[0]` so the package's internal `from app.xxx import yyy` also resolves here.
In practice `pytest backend_ingestion/tests/` and `pytest alert_engine/tests/`
each go fully green this way.

Known limitation: collecting both directories in a **single** `pytest` process still
clobbers each other (`sys.modules["app"]` has only one slot). The repo-root `pytest.ini`
`testpaths` keep the two suites separate: by default only the repo-root `tests/` and
`alert_engine/tests/` run; the backend suite is run separately with
`pytest backend_ingestion/tests/` (see README). Renaming the top-level package is the
real fix, but that is a cross-module refactor out of scope for Order 15.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# settings.jwt_secret is required (no default), so it must be injected before
# importing app.*, otherwise Settings() raises ValidationError at construction.
os.environ.setdefault("JWT_SECRET", "test-only-secret-0123456789abcdef")

BACKEND_ROOT = str(Path(__file__).resolve().parents[1])

# Drop any top-level `app` possibly occupied by the alert_engine side, so the next
# import resolves to this directory instead.
for _name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
    del sys.modules[_name]

while BACKEND_ROOT in sys.path:
    sys.path.remove(BACKEND_ROOT)
sys.path.insert(0, BACKEND_ROOT)
