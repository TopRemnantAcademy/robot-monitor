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

# 前端依赖（openapi-typescript / ajv / tsc）在构建期也装好：
# npm ci 需要 package-lock.json，故先只 COPY 这两个文件再装，
# 让这一层在**源码变动时**仍能命中缓存（只有 lock 变了才重装）。
COPY frontend_dashboard/package.json frontend_dashboard/package-lock.json /tmp/fe/
RUN cd /tmp/fe && npm ci --no-audit --fund=false
