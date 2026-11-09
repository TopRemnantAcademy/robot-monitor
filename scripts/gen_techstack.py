#!/usr/bin/env python3
"""Generate the RobotMonitor tech-stack / architecture graph from real manifests.

Why this exists
---------------
The "tech stack" diagram shown on the README intro page must never silently go
stale. Instead of hand-drawing it, this script reads the *source of truth*:

  - infra/docker-compose.yml   -> Docker service images (EMQX, TimescaleDB, ...)
  - backend_ingestion/requirements.txt / alert_engine/requirements.txt / simulator/requirements.txt
                                -> pinned Python dependencies
  - frontend_dashboard/package.json -> pinned npm dependencies / dev tooling

...and emits two Mermaid diagrams (a system topology + a layer-by-layer tech
spec graph) plus a canonical docs/tech-stack.mmd. The same diagram block is
injected into README.md / README.ko.md / README.zh.md between
`<!-- TECHSTACK-START -->` / `<!-- TECHSTACK-END -->` markers.

Sync guarantee
--------------
A pre-commit hook (`.githooks/pre-commit`) and a CI job run this script and
`git diff --exit-code` on the affected files. If a manifest changed but the
diagram was not regenerated, the commit / build is blocked with a message
telling the contributor to run `python3 scripts/gen_techstack.py`. Thus the
graph *changes together* with the project, enforced automatically.

Stdlib only (no third-party deps) so it runs in pre-commit and CI unchanged.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Friendly display name + category + description for infra (docker-compose) services.
INFRA_META = {
    "emqx": ("EMQX", "Messaging", "MQTT broker"),
    "timescaledb": ("TimescaleDB", "Storage", "time-series PostgreSQL"),
    "redis": ("Redis", "Cache", "latest-state cache"),
    "prometheus": ("Prometheus", "Observability", "metrics TSDB"),
    "emqx-exporter": ("EMQX exporter", "Observability", "EMQX metrics"),
    "blackbox-exporter": ("Blackbox exporter", "Observability", "probe exporter"),
    "postgres-exporter": ("Postgres exporter", "Observability", "Postgres metrics"),
    "redis-exporter": ("Redis exporter", "Observability", "Redis metrics"),
    "grafana": ("Grafana", "Observability", "dashboards"),
}


def parse_requirements(path: Path):
    """Return list of (name, operator, version) for pinned python deps."""
    deps = []
    if not path.exists():
        return deps
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(
            r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(==|>=|<=|~=|!=|<|>)\s*([^\s;]+)",
            line,
        )
        if m:
            deps.append((m.group(1) + (m.group(2) or ""), m.group(3), m.group(4)))
    return deps


def parse_compose(path: Path):
    """Tolerant parser for docker-compose service -> image (no PyYAML needed)."""
    services: dict[str, str] = {}
    cur = None
    if not path.exists():
        return services
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        svc = re.match(r"^  ([A-Za-z0-9_\-]+):\s*$", raw)
        if svc:
            cur = svc.group(1)
            continue
        if cur is not None:
            img = re.match(r"^    image:\s*(\S+)", raw)
            if img:
                services[cur] = img.group(1)
    return services


def parse_package_json(path: Path):
    """Return dict of section -> list of (name, version)."""
    out: dict[str, list[tuple[str, str]]] = {}
    if not path.exists():
        return out
    data = json.loads(path.read_text(encoding="utf-8"))
    for section in ("dependencies", "devDependencies"):
        items = []
        for name, ver in data.get(section, {}).items():
            items.append((name, str(ver)))
        out[section] = items
    return out


def image_tag(image: str) -> str:
    """Last ':'-segment of an image ref is the tag/version."""
    return image.rsplit(":", 1)[-1]


def node_id(prefix: str, name: str) -> str:
    return prefix + "_" + re.sub(r"[^a-z0-9]", "_", name.lower())


def fmt_ver(name: str, op: str, ver: str) -> str:
    # Escape < / > so Mermaid does not parse them as HTML tags inside labels.
    if op == "==":
        label = f"{name} {ver}"
    else:
        label = f"{name} ({op}{ver})"
    return label.replace("<", "&lt;").replace(">", "&gt;")


def build_topology(compose: dict[str, str], pkg: dict, backend: list) -> str:
    emqx = image_tag(compose.get("emqx", "emqx/emqx:5.6"))
    ts = image_tag(compose.get("timescaledb", "timescale/timescaledb:2.13.1-pg16"))
    redis = image_tag(compose.get("redis", "redis:7.2-alpine"))
    prom = image_tag(compose.get("prometheus", "prom/prometheus:v3.1.0"))
    graf = image_tag(compose.get("grafana", "grafana/grafana:11.5.0"))

    fastapi = next((v for n, o, v in backend if n == "fastapi"), "0.110.0")
    react = next(
        (v for n, v in pkg.get("dependencies", []) if n == "react"), "18.3.1"
    )

    lines = ["flowchart LR"]
    lines.append('  FLEET["Robot fleet (Edge)<br/>100 humanoid @10Hz"] -->|"MQTT 1883"| EMQX["EMQX ' + emqx + '"]')
    lines.append('  SIM["simulator<br/>100-robot mock @10Hz"] -->|"MQTT 1883"| EMQX')
    lines.append('  EMQX --> BE["backend_ingestion<br/>FastAPI ' + fastapi + '"]')
    lines.append('  BE -->|"batch write"| TSDB["TimescaleDB ' + ts + '"]')
    lines.append('  BE -->|"Hash + TTL + Pub/Sub"| REDIS["Redis ' + redis + '"]')
    lines.append('  BE -->|"WebSocket /ws/robots"| FE["frontend_dashboard<br/>React ' + react + '"]')
    lines.append('  AE["alert_engine<br/>FastAPI rules"] -->|"subscribe stream"| BE')
    lines.append('  AE -->|"alert stream"| FE')
    lines.append('  TSDB -.->|"scrape"| PROM["Prometheus ' + prom + '"]')
    lines.append('  PROM --> GRAF["Grafana ' + graf + '"]')
    return "\n".join(lines)


def build_spec(compose: dict[str, str], backend: list, alert: list, sim: list, pkg: dict) -> str:
    L: list[str] = ["graph TD"]

    # ---- Edge / Simulation ----
    sim_lines = []
    for n, o, v in sim:
        sim_lines.append(f"    {node_id('edge', n)}[\"{fmt_ver(n, o, v)}\"]")
    if sim_lines:
        L.append("  subgraph EDGE[\"Edge / Simulation (Python 3.11)\"]")
        L.append("    direction TB")
        L.extend(sim_lines)
        L.append("  end")

    # ---- Infra by category ----
    cats: dict[str, list[str]] = {}
    for svc, image in compose.items():
        meta = INFRA_META.get(svc)
        if not meta:
            continue
        display, cat, desc = meta
        cats.setdefault(cat, []).append(
            f'    {node_id("infra", svc)}["{display} {image_tag(image)}<br/><i>{desc}</i>"]'
        )
    cat_order = ["Messaging", "Storage", "Cache", "Observability"]
    for cat in cat_order:
        if cat in cats:
            title = "Storage and Cache" if cat == "Cache" else cat
            L.append(f'  subgraph INFRA_{cat.upper()}["{title}"]')
            L.append("    direction TB")
            L.extend(cats[cat])
            L.append("  end")

    # ---- Backend services (union, dedupe by name) ----
    seen: set[str] = set()
    be_lines = []
    otel_sdk = otel_instr = None
    for n, o, v in backend:
        if n.startswith("opentelemetry-"):
            if n == "opentelemetry-sdk":
                otel_sdk = v
            elif "instrumentation" in n:
                otel_instr = v
            continue
        if n in seen:
            continue
        seen.add(n)
        be_lines.append(f"    {node_id('be', n)}[\"{fmt_ver(n, o, v)}\"]")
    if otel_sdk:
        be_lines.append(
            f'    {node_id("be", "opentelemetry")}["OpenTelemetry {otel_sdk}'
            + (f' + instrumentation {otel_instr}' if otel_instr else '')
            + '"]'
        )
    if be_lines:
        L.append('  subgraph BACKEND["Backend services (FastAPI)"]')
        L.append("    direction TB")
        L.extend(be_lines)
        L.append("  end")

    # ---- Frontend runtime + tooling ----
    fe_lines = []
    for n, v in pkg.get("dependencies", []):
        fe_lines.append(f'    {node_id("fe", n)}["{n} {v.lstrip("^~")}"]')
    if fe_lines:
        L.append('  subgraph FE["Frontend (React + Vite)"]')
        L.append("    direction TB")
        L.extend(fe_lines)
        L.append("  end")

    dev_lines = []
    for n, v in pkg.get("devDependencies", []):
        dev_lines.append(f'    {node_id("fedev", n)}["{n} {v.lstrip("^~")}"]')
    if dev_lines:
        L.append('  subgraph FEDEV["Frontend tooling"]')
        L.append("    direction TB")
        L.extend(dev_lines)
        L.append("  end")

    return "\n".join(L)


def diagrams_md(topology: str, spec: str) -> str:
    return (
        "\n<!-- TECHSTACK-START -->\n\n"
        "### System topology\n\n"
        "```mermaid\n" + topology + "\n```\n\n"
        "### Tech stack by layer\n\n"
        "```mermaid\n" + spec + "\n```\n\n"
        "<!-- TECHSTACK-END -->\n"
    )


def inject(readme: Path, section_heading: str, before_heading: str, block: str) -> bool:
    """Insert or refresh the tech-stack section in a README.

    Returns True if the file content changed.
    """
    text = readme.read_text(encoding="utf-8")
    start = "<!-- TECHSTACK-START -->"
    end = "<!-- TECHSTACK-END -->"

    if start in text and end in text:
        new_text = re.sub(
            re.escape(start) + r".*?" + re.escape(end),
            block.strip("\n"),
            text,
            flags=re.DOTALL,
        )
    else:
        # Insert a new section right before `before_heading`.
        lines = text.splitlines(keepends=True)
        insert_at = len(lines)
        for i, ln in enumerate(lines):
            if ln.strip().startswith(before_heading):
                insert_at = i
                break
        new_section = (
            f"\n---\n\n## {section_heading}\n\n"
            "> Auto-generated from `infra/docker-compose.yml`, `*/requirements.txt`, "
            "and `frontend_dashboard/package.json` by `scripts/gen_techstack.py`. "
            "Run it after changing dependencies so this diagram stays in sync.\n\n"
            + block.strip("\n")
            + "\n"
        )
        lines.insert(insert_at, new_section)
        new_text = "".join(lines)

    if new_text != text:
        readme.write_text(new_text, encoding="utf-8")
        return True
    return False


def main() -> int:
    compose = parse_compose(ROOT / "infra" / "docker-compose.yml")
    backend = parse_requirements(ROOT / "backend_ingestion" / "requirements.txt")
    alert = parse_requirements(ROOT / "alert_engine" / "requirements.txt")
    sim = parse_requirements(ROOT / "simulator" / "requirements.txt")
    pkg = parse_package_json(ROOT / "frontend_dashboard" / "package.json")

    topology = build_topology(compose, pkg, backend)
    spec = build_spec(compose, backend, alert, sim, pkg)
    block = diagrams_md(topology, spec)

    # Canonical mermaid source file.
    mmd = ROOT / "docs" / "tech-stack.mmd"
    mmd.write_text(
        "# RobotMonitor tech-stack graph (auto-generated by scripts/gen_techstack.py)\n"
        "# Source of truth: docker-compose.yml + requirements.txt + package.json\n\n"
        + topology
        + "\n\n"
        + spec
        + "\n",
        encoding="utf-8",
    )

    targets = [
        (ROOT / "README.md", "Tech stack", "## Contributing"),
        (ROOT / "README.ko.md", "기술 스택", "## 기여하기"),
        (ROOT / "README.zh.md", "技术栈", "## 参与贡献"),
    ]
    changed = []
    for path, heading, before in targets:
        if inject(path, heading, before, block):
            changed.append(path.name)

    print(f"generated docs/tech-stack.mmd ({len(compose)} infra services, "
          f"{len(backend)} backend deps, {len(pkg.get('dependencies', []))} frontend deps)")
    if changed:
        print("updated READMEs: " + ", ".join(changed))
    else:
        print("READMEs already in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
