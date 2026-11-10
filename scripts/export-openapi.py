#!/usr/bin/env python3
"""Export /openapi.json from the FastAPI app, with optional contract-snapshot comparison (Order 18 . Contract-as-code).

Usage:
  # Write snapshot (run by the PR author after changing the backend contract, to commit it)
  python3 scripts/export-openapi.py --out docs/contracts/openapi.snapshot.json

  # CI drift comparison: non-breaking only warns (exit 0), breaking fails (exit 1)
  python3 scripts/export-openapi.py --check docs/contracts/openapi.snapshot.json

[Why not use `uvicorn &` + `curl /openapi.json`]
The backend `main.py` lifespan connects to MQTT/Redis/DB; the CI container has none of these
dependencies, so starting the server stalls at the startup stage. The OpenAPI schema, however,
is derived **purely statically** from the route decorators and needs no external dependency --
just `import app.main` and call `app.openapi()` to obtain an object that is **byte-for-byte
identical** to the live `/openapi.json` (FastAPI internally uses the very same
fastapi.openapi.utils).

[Why inject JWT_SECRET before importing]
`app/config.py:77`'s `Settings()` is instantiated at **module-import time** and `jwt_secret`
has no default value. Without injection the import fails immediately with a ValidationError.
The value given here is a **pure placeholder**, used only so Settings instantiates successfully;
it takes part in no signing/network behavior, and is neither persisted nor written to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend_ingestion"


def _repo_path(p: str) -> Path:
    """Pin the user-supplied relative path to the **repo root**, not to a cwd altered by os.chdir.

    Past pitfall: to align with the in-container `.env` reading behavior the script does
    `os.chdir(backend_ingestion)`, after which `Path("docs/...")` resolves to
    `backend_ingestion/docs/...`, so the snapshot is written to the wrong location
    (CI sees "snapshot does not exist" and wrongly turns red).
    """
    path = Path(p)
    return path if path.is_absolute() else (REPO_ROOT / path)

# Must inject BEFORE importing app.main: Settings is already instantiated at import time.
os.environ.setdefault("JWT_SECRET", "openapi-export-placeholder-not-a-real-secret")
# Let `import app.main` find the package (when the script is invoked from the repo root,
# sys.path[0] is scripts/).
sys.path.insert(0, str(BACKEND_DIR))

# Base directory: if the backend working dir has a .env, pydantic-settings will read it;
# explicitly switch to backend_ingestion here to stay consistent with in-container behavior
# (.env is excluded by .gitignore).
os.chdir(BACKEND_DIR)


def build_spec() -> dict:
    from app.main import app  # noqa: PLC0415 -- must be deferred until after the env var is injected

    return app.openapi()


def canonical(spec: dict) -> str:
    """Canonical serialization: guarantees **idempotency** (running twice yields byte-identical output).

    - `sort_keys=True`: dictionary order of `paths`/`schemas` is independent of insertion order;
    - `ensure_ascii=False` + `indent=2`: readable descriptions, minimal diff;
    - trailing newline: avoids editors/POSIX tools complaining about no newline at EOF.
    """
    return json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


# --------------------------------------------------------------- breaking detection

# Judgment rules (Order 18 T3):
#   breaking (fail) = delete path / delete method / delete response field / change response field
#                     type / add required request field
#   non-breaking (warn) = add path / add optional field / change description
# Here we deliberately do **not** do a full JSON Schema diff (pulling in a json-diff-style dependency
# would conflict with the "no hallucination" discipline). Instead we do a targeted comparison against
# the real shape of this repo's REST contract: the field names and types of responses.200.
# Basis: all endpoints are GET (no request body), so contract risk concentrates on response fields
# being deleted or having their type changed.


def _resolve_ref(spec: dict, node: dict) -> dict:
    if not isinstance(node, dict):
        return {}
    ref = node.get("$ref")
    if ref and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        return spec.get("components", {}).get("schemas", {}).get(name, {})
    return node


def _response_fields(spec: dict, path: str, method: str) -> dict[str, str]:
    """Extract a flat `{field_name: type}` map of a endpoint's 200 response (with $ref expansion, nested supported)."""
    try:
        node = spec["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
    except (KeyError, TypeError):
        return {}
    schema = _resolve_ref(spec, node)
    props = schema.get("properties") or {}
    out: dict[str, str] = {}
    for field, fs in props.items():
        fs = _resolve_ref(spec, fs)
        # Array/object fields: flatten inner properties too, with a `parent_field.` name prefix.
        inner = fs.get("properties")
        if isinstance(inner, dict) and inner:
            for sub, sfs in inner.items():
                sfs = _resolve_ref(spec, sfs)
                out[f"{field}.{sub}"] = str(sfs.get("type", "ref"))
            continue
        if fs.get("type") == "array":
            items = _resolve_ref(spec, fs.get("items", {}))
            arr_props = items.get("properties")
            if isinstance(arr_props, dict) and arr_props:
                for sub, sfs in arr_props.items():
                    sfs = _resolve_ref(spec, sfs)
                    out[f"{field}[].{sub}"] = str(sfs.get("type", "ref"))
                continue
        out[field] = str(fs.get("type", "ref"))
    return out


def diff_specs(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """Return (breaking, warnings). Non-empty breaking means CI fails."""
    breaking: list[str] = []
    warnings: list[str] = []

    old_paths = old.get("paths", {})
    new_paths = new.get("paths", {})

    for path, old_ops in old_paths.items():
        if path not in new_paths:
            breaking.append(f"deleted path {path}")
            continue
        for method in old_ops:
            if method not in new_paths[path]:
                breaking.append(f"deleted method {method.upper()} {path}")
                continue
            old_fields = _response_fields(old, path, method)
            new_fields = _response_fields(new, path, method)
            for f, t in old_fields.items():
                if f not in new_fields:
                    breaking.append(f"{method.upper()} {path}: response field `{f}` deleted")
                elif new_fields[f] != t:
                    breaking.append(
                        f"{method.upper()} {path}: response field `{f}` type {t} -> {new_fields[f]}"
                    )
            for f in new_fields:
                if f not in old_fields:
                    warnings.append(f"{method.upper()} {path}: new response field `{f}` (non-breaking)")

    for path in new_paths:
        if path not in old_paths:
            warnings.append(f"new path {path} (non-breaking)")

    return breaking, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Export/compare the FastAPI OpenAPI contract")
    ap.add_argument("--out", help="write snapshot to this path (idempotent)")
    ap.add_argument("--check", help="compare against an existing snapshot; exit 1 on breaking")
    args = ap.parse_args()
    if not args.out and not args.check:
        ap.error("provide at least one of --out or --check")

    spec = build_spec()
    text = canonical(spec)

    if args.check:
        snap_path = _repo_path(args.check)
        if not snap_path.exists():
            print(f"[contract] snapshot does not exist: {snap_path}", file=sys.stderr)
            print("[contract] run first: python3 scripts/export-openapi.py --out " + str(snap_path), file=sys.stderr)
            return 1
        old = json.loads(snap_path.read_text(encoding="utf-8"))
        breaking, warnings = diff_specs(old, spec)
        for w in warnings:
            print(f"[contract][warn] non-breaking: {w}")
        if breaking:
            print("\n[contract][FAIL] detected breaking change:", file=sys.stderr)
            for b in breaking:
                print(f"  ✗ {b}", file=sys.stderr)
            print(
                "\nTo accept this change, update the snapshot:\n"
                "  python3 scripts/export-openapi.py --out docs/contracts/openapi.snapshot.json",
                file=sys.stderr,
            )
            return 1
        if text == snap_path.read_text(encoding="utf-8"):
            print("[contract] OK: current OpenAPI is byte-identical to the snapshot")
        else:
            print("[contract] OK: only non-breaking differences (description / new optional field)")
        return 0

    out = _repo_path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"[contract] wrote {out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
