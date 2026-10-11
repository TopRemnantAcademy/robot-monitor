# 契约门禁（订单 18 T2）的构建环境。
#
# 【为什么需要自定义镜像，而不是直接 image: node:20】
# 实测（构建 cnb-jbi-1k4k46es7 / cnb-g5r-1k4k48qhe 的 probe stage）：
#   node:20 的 python3 是 Debian 精简包 —— 版本 3.11.2 在 /usr/bin/python3，
#   但 **既没有 pip 也没有 ensurepip**：
#     $ python3 -m pip --version      → No module named pip
#     $ python3 -m ensurepip --version → No module named ensurepip
#   于是每次跑契约导出都要现场 apt-get install python3-pip，既慢又依赖网络。
#
# 按 cnb-pipeline 最佳实践「用自定义 Dockerfile 预装系统依赖」，
# 把 pip + 后端依赖在**镜像构建期**装好，运行时零安装。
# 镜像是按内容缓存的：只有本文件或 requirements.txt 变化时才重建。
#
# 【为什么 pip 装到 /usr/local】
# Debian 的 python3-pip 走 PEP668（externally-managed），默认拒绝写系统环境；
# 构建期是 root，也不能照搬"运行时 --user"（root 的 --user 落 /root/.local，
# 任务容器里换个用户就找不到）。统一 `--break-system-packages` 装进 /usr/local，
# 它同时是系统 python3 的 site 与 PATH 上 python3/pip3 的解析位置，
# 防止"装到 A、读的是 B"的静默漂移。
#
# 【为什么前端依赖装全局，而不是 /tmp/fe + 软链 node_modules】
# 曾用 `ln -s /tmp/fe/node_modules frontend_dashboard/node_modules` 想省一次 npm ci，
# 真机失败（构建 cnb-pm7-1k4k4emnd）：`.bin/` 里的 shim 按**自身真实路径**相对回跳
# `../esbuild/bin/esbuild`，跨 /tmp 与 workspace 两个挂载点后相对路径失效。
# 改为构建期全局安装 CLI，shim 落在 /usr/local/bin，与挂载点无关，PATH 直接解析。
FROM node:20

# pip 走 apt（node:20 基于 Debian bookworm，apt 可用，且构建期以 root 运行）。
# 清 apt 列表，避免镜像里留几百 MB 垃圾。
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*

# 后端依赖（契约导出要 `import app.main`，故 fastapi/pydantic 必须齐）。
# --no-cache-dir 不留 wheel 缓存；固定 requirements.txt 保证与容器内一致。
COPY backend_ingestion/requirements.txt /tmp/backend-requirements.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages \
      -r /tmp/backend-requirements.txt \
 && python3 -c "import fastapi, pydantic; print('deps ok:', fastapi.__version__, pydantic.VERSION)"

# 前端依赖在构建期装好。先只 COPY manifest/lock 再装，使这一层在**源码变动时**
# 仍命中缓存（只有 lock 变了才重装）。
# 注意 `cd frontend_dashboard`：npm 从 cwd 向上找 nearest package.json 决定安装上下文。
# 全局安装 CLI 的版本与 frontend_dashboard/package.json 的 devDependencies 对齐
# （openapi-typescript ^7.13.0 / typescript 5.6.3 / ajv ^8.20.0），
# 避免 CI 与本地各跑一个版本。
COPY frontend_dashboard/package.json frontend_dashboard/package-lock.json frontend_dashboard/
RUN cd frontend_dashboard \
 && npm ci --no-audit --fund=false \
 && npm install -g --no-audit --fund=false \
      openapi-typescript@7.13.0 typescript@5.6.3 ajv@8.20.0 \
 && openapi-typescript --version \
 && tsc --version \
 && python3 -m pip --version
