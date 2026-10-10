#!/usr/bin/env python3
"""从 FastAPI app 导出 /openapi.json，并可选比对契约快照（订单 18 · 契约即代码）。

用法：
  # 写快照（PR 作者在改完后端契约后执行，产出入库）
  python3 scripts/export-openapi.py --out docs/contracts/openapi.snapshot.json

  # CI 漂移比对：非 breaking 只告警（exit 0），breaking 失败（exit 1）
  python3 scripts/export-openapi.py --check docs/contracts/openapi.snapshot.json

【为什么不用 `uvicorn &` + `curl /openapi.json`】
后端 `main.py` 的 lifespan 要连 MQTT/Redis/DB，CI 容器里没有这些依赖，
起服务会卡在启动阶段。而 OpenAPI schema 是**纯静态**地从路由装饰器推导出来的，
不需要任何外部依赖 —— 直接 `import app.main` 调 `app.openapi()` 即可拿到
与线上 `/openapi.json` **逐字节相同**的对象（FastAPI 内部就是同一个 fastapi.openapi.utils）。

【为什么 import 前要注入 JWT_SECRET】
`app/config.py:77` 的 `Settings()` 在**模块导入期**实例化，且 `jwt_secret` 无默认值。
不注入则 import 直接 ValidationError。这里给的是**纯占位值**，
只用于让 Settings 实例化成功，不参与任何签名/网络行为，不入库、不落盘。
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
    """把用户传入的相对路径钉在**仓库根**，而不是被 os.chdir 改过的 cwd。

    踩过的坑：脚本为了对齐容器内 `.env` 读取行为会 `os.chdir(backend_ingestion)`，
    此后 `Path("docs/...")` 会解析到 `backend_ingestion/docs/...`，
    于是快照被写到错误位置（CI 会看到"快照不存在"而误红）。
    """
    path = Path(p)
    return path if path.is_absolute() else (REPO_ROOT / path)

# 必须在 import app.main **之前**注入：Settings 在导入期就已实例化。
os.environ.setdefault("JWT_SECRET", "openapi-export-placeholder-not-a-real-secret")
# 让 `import app.main` 能找到包（脚本从仓库根被调用时 sys.path[0] 是 scripts/）。
sys.path.insert(0, str(BACKEND_DIR))

# 基准目录：后端工作目录下若有 .env 会被 pydantic-settings 读到，
# 这里显式切到 backend_ingestion，保证与容器内行为一致（.env 已被 .gitignore 排除）。
os.chdir(BACKEND_DIR)


def build_spec() -> dict:
    from app.main import app  # noqa: PLC0415 —— 必须延迟到环境变量注入之后再导入

    return app.openapi()


def canonical(spec: dict) -> str:
    """规范化序列化：保证**幂等**（连跑两次输出逐字节相同）。

    - `sort_keys=True`：`paths`/`schemas` 的字典序与插入顺序无关；
    - `ensure_ascii=False` + `indent=2`：中文 description 可读，diff 最小；
    - 结尾补一个换行：避免编辑器/POSIX 工具抱怨 no newline at EOF。
    """
    return json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


# --------------------------------------------------------------- breaking 检测

# 判定规则（订单 18 T3）：
#   breaking（失败）= 删路径 / 删方法 / 删响应字段 / 改响应字段类型 / 新增必填请求字段
#   非 breaking（告警）= 新增路径 / 新增可选字段 / 改 description
# 这里刻意**不做**完整 JSON Schema diff（引入 json-diff 类依赖会与「无幻觉」纪律冲突），
# 而是针对本仓库 REST 契约的真实形态做定向比对：responses.200 的字段名与类型。
# 依据：所有端点都是 GET（无请求体），契约风险集中在响应字段被删/改类型。


def _resolve_ref(spec: dict, node: dict) -> dict:
    if not isinstance(node, dict):
        return {}
    ref = node.get("$ref")
    if ref and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        return spec.get("components", {}).get("schemas", {}).get(name, {})
    return node


def _response_fields(spec: dict, path: str, method: str) -> dict[str, str]:
    """取出某端点 200 响应的 `{字段名: 类型}` 扁平映射（含 $ref 展开、支持嵌套）。"""
    try:
        node = spec["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
    except (KeyError, TypeError):
        return {}
    schema = _resolve_ref(spec, node)
    props = schema.get("properties") or {}
    out: dict[str, str] = {}
    for field, fs in props.items():
        fs = _resolve_ref(spec, fs)
        # 数组/对象型字段：把内层属性也拍平进来，字段名前缀 `父字段.`
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
    """返回 (breaking, warnings)。breaking 非空即 CI 失败。"""
    breaking: list[str] = []
    warnings: list[str] = []

    old_paths = old.get("paths", {})
    new_paths = new.get("paths", {})

    for path, old_ops in old_paths.items():
        if path not in new_paths:
            breaking.append(f"删除路径 {path}")
            continue
        for method in old_ops:
            if method not in new_paths[path]:
                breaking.append(f"删除方法 {method.upper()} {path}")
                continue
            old_fields = _response_fields(old, path, method)
            new_fields = _response_fields(new, path, method)
            for f, t in old_fields.items():
                if f not in new_fields:
                    breaking.append(f"{method.upper()} {path}: 响应删除字段 `{f}`")
                elif new_fields[f] != t:
                    breaking.append(
                        f"{method.upper()} {path}: 响应字段 `{f}` 类型 {t} → {new_fields[f]}"
                    )
            for f in new_fields:
                if f not in old_fields:
                    warnings.append(f"{method.upper()} {path}: 新增响应字段 `{f}`（非 breaking）")

    for path in new_paths:
        if path not in old_paths:
            warnings.append(f"新增路径 {path}（非 breaking）")

    return breaking, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="导出/比对 FastAPI OpenAPI 契约")
    ap.add_argument("--out", help="写出快照到该路径（幂等）")
    ap.add_argument("--check", help="与既有快照比对；breaking 则 exit 1")
    args = ap.parse_args()
    if not args.out and not args.check:
        ap.error("至少给 --out 或 --check 之一")

    spec = build_spec()
    text = canonical(spec)

    if args.check:
        snap_path = _repo_path(args.check)
        if not snap_path.exists():
            print(f"[contract] 快照不存在：{snap_path}", file=sys.stderr)
            print("[contract] 先执行：python3 scripts/export-openapi.py --out " + str(snap_path), file=sys.stderr)
            return 1
        old = json.loads(snap_path.read_text(encoding="utf-8"))
        breaking, warnings = diff_specs(old, spec)
        for w in warnings:
            print(f"[contract][warn] 非 breaking：{w}")
        if breaking:
            print("\n[contract][FAIL] 检测到 breaking change：", file=sys.stderr)
            for b in breaking:
                print(f"  ✗ {b}", file=sys.stderr)
            print(
                "\n如需接受该变更，请更新快照：\n"
                "  python3 scripts/export-openapi.py --out docs/contracts/openapi.snapshot.json",
                file=sys.stderr,
            )
            return 1
        if text == snap_path.read_text(encoding="utf-8"):
            print("[contract] OK：当前 OpenAPI 与快照逐字节一致")
        else:
            print("[contract] OK：仅非 breaking 差异（描述/新增可选字段）")
        return 0

    out = _repo_path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"[contract] 已写出 {out}（{len(text)} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
