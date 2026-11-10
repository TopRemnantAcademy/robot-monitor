# Build environment for the contract gate (Order 18 T2).
#
# [Why a custom image is needed instead of just image: node:20]
# Verified (probe stage of builds cnb-jbi-1k4k46es7 / cnb-g5r-1k4k48qhe):
#   node:20's python3 is a slim Debian package -- version 3.11.2 at /usr/bin/python3,
#   but has neither pip nor ensurepip:
#     $ python3 -m pip --version      -> No module named pip
#     $ python3 -m ensurepip --version -> No module named ensurepip
#   So every contract export had to apt-get install python3-pip on the spot -- slow and network-dependent.
#
# Following cnb-pipeline best practice "preinstall system deps with a custom Dockerfile",
# install pip + backend deps at image build time, zero install at runtime.
# Images are content-cached: rebuilt only when this file or requirements.txt changes.
#
# [Why install pip into /usr/local]
# Debian's python3-pip follows PEP668 (externally-managed) and by default refuses to write to the system env;
# even at build time as root, we cannot just copy "runtime --user" (root's --user lands in /root/.local,
# and a task container running as another user won't find it). Install everything with --break-system-packages into /usr/local,
# which is both the system python3's site and the resolved location for python3/pip3 on PATH,
# preventing the silent "installed in A but read from B" drift.
#
# [Why install frontend deps globally instead of /tmp/fe + symlink node_modules]
# We once used `ln -s /tmp/fe/node_modules frontend_dashboard/node_modules` to skip one npm ci,
# but it failed on the real machine (build cnb-pm7-1k4k4emnd): the shim in `.bin/` resolves relative to its own real path
# `../esbuild/bin/esbuild`, and across the /tmp and workspace mount points the relative path breaks.
# Instead, install the CLI globally at build time; the shim lands in /usr/local/bin, mount-point-independent, resolved directly via PATH.
FROM node:20

# Install pip via apt (node:20 is Debian bookworm, apt works, and the build runs as root).
# Clear apt lists to avoid hundreds of MB of junk in the image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*

# Backend deps (contract export needs `import app.main`, so fastapi/pydantic must be present).
# --no-cache-dir leaves no wheel cache; pinned requirements.txt keeps it consistent with the container.
COPY backend_ingestion/requirements.txt /tmp/backend-requirements.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages \
      -r /tmp/backend-requirements.txt \
 && python3 -c "import fastapi, pydantic; print('deps ok:', fastapi.__version__, pydantic.VERSION)"

# Frontend deps installed at build time. Copy only manifest/lock first, then install, so this layer still hits cache on source changes
# (reinstalled only when the lock changes).
# Note `cd frontend_dashboard`: npm walks up from cwd to find nearest package.json to decide install context.
# Globally installed CLI versions are aligned with frontend_dashboard/package.json devDependencies
# (openapi-typescript ^7.13.0 / typescript 5.6.3 / ajv ^8.20.0),
# to avoid CI and local running different versions.
COPY frontend_dashboard/package.json frontend_dashboard/package-lock.json frontend_dashboard/
# [Why move node_modules to /opt/fe/node_modules after install]
# The runtime task workspace is checked out from git and has no node_modules;
# but Node's ESM resolution only walks "file -> parent dir" to find node_modules, and does NOT recognize global packages,
# and NODE_PATH is also ineffective for ESM (both verified). So step 3's `import 'ajv'` would always fail
# (real error: build cnb-1tc-1k4k4ng86, Cannot find package 'ajv').
# Here we pin the npm ci output to an absolute path independent of the workspace mount point,
# for ci/contract-gate.sh to symlink back into frontend_dashboard/node_modules at runtime.
#
# WARNING: the dir must be named exactly `node_modules` (so /opt/fe/node_modules, not /opt/fe-modules):
# packages' internal `import '@redocly/openapi-core'` resolves based on their own real path
# /opt/fe/node_modules/openapi-typescript/bin/cli.js walks up to find node_modules.
# With /opt/fe-modules, the ancestor chain only has /opt/fe-modules/openapi-typescript/node_modules
# and /opt/node_modules -- neither reaches the sibling @redocly,
# so it errors Cannot find package '@redocly/openapi-core' (verified).
# It must be the full directory, not a single package: openapi-typescript depends on @redocly/*,
# and ajv depends on fast-deep-equal / fast-uri / json-schema-traverse / require-from-string.
RUN cd frontend_dashboard \
 && npm ci --no-audit --fund=false \
 && mkdir -p /opt/fe \
 && mv node_modules /opt/fe/node_modules \
 && npm install -g --no-audit --fund=false \
      openapi-typescript@7.13.0 typescript@5.6.3 ajv@8.20.0 \
 && openapi-typescript --version \
 && tsc --version \
 && test -d /opt/fe/node_modules/ajv \
 && python3 -m pip --version
