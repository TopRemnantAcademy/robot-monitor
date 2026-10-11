# 顶层包标记。容器 CMD 现用 WORKDIR /app/backend_ingestion + `app.main:app`
# （与代码内部 `from app import ...` 风格一致，2026-10-11 修正），
# 不再经 backend_ingestion 命名空间导入；保留本文件维持目录的包语义。
