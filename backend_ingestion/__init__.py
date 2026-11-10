# Top-level package marker. The container CMD now uses WORKDIR /app/backend_ingestion
# + `app.main:app` (consistent with the in-code `from app import ...` style; fixed 2026-10-11),
# so imports no longer go through the backend_ingestion namespace; keep this file to
# preserve the package semantics of the directory.
