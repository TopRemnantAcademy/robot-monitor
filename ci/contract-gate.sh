#!/bin/sh
# Contract-gate execution script (Order 18 T2).
#
# Deliberately uses `sh` not bash: CNB's script is itself run by sh (dash)
# (verified: `set -euo pipefail` errors with `sh: 1: set: Illegal option -o pipefail`),
# putting logic in .sh and running it explicitly with sh avoids the "passes locally in bash, fails in CI" mismatch.
# Therefore only POSIX syntax is used; no pipefail / [[ ]] / $'...'.
set -eu

echo "== 0) Environment self-check =="
node --version
python3 --version
python3 -m pip --version
# The source of frontend deps must exist: either node_modules already in the workspace (local npm ci),
# or /opt/fe/node_modules prepared in the image (CI; step 2 symlinks it back into the workspace).
# If neither exists, every later step fails cryptically (gen:types not found /
# ajv ERR_MODULE_NOT_FOUND); naming it explicitly here separates "image build-time npm ci did not take effect"
# from "code problem" -- the real machine once tripped on a different mount point (build cnb-pm7-1k4k4emnd).
if [ ! -d frontend_dashboard/node_modules ] && [ ! -d /opt/fe/node_modules ]; then
  echo "FATAL: frontend deps not found -- workspace has no node_modules and image has no /opt/fe/node_modules" >&2
  exit 1
fi

echo
echo "== 1) Contract snapshot drift + breaking detection (T3) =="
# --check semantics: non-breaking (adding optional fields / changing descriptions) only warns and exits 0;
# breaking (removing fields / changing types / removing paths) exits 1.
python3 scripts/export-openapi.py --check docs/contracts/openapi.snapshot.json

echo
echo "== 2) Generated types must match committed artifact (T1/T2 core gate) =="
# [Why link the image's node_modules back into the workspace]
# Real error (build cnb-1tc-1k4k4ng86):
#   Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'ajv'
#   imported from /workspace/frontend_dashboard/tests/ws-envelope.test.mjs
# Root cause: the node_modules npm-ci'd at build time in the Dockerfile stays in the image,
# but the runtime task workspace is checked out from git and has no node_modules;
# and Node's ESM resolution only walks "file dir -> parent" step by step to find node_modules,
# recognizing neither global packages nor NODE_PATH for ESM (both verified,
# see the comment in ci/contract-gate.Dockerfile that pins output to /opt/fe/node_modules).
# Without linking it back, step 3's `import 'ajv'` always fails.
# Symlink the whole thing here (not just ajv) -- ajv also has fast-deep-equal / fast-uri /
# json-schema-traverse / require-from-string as transitive deps; missing any one breaks it.
# Only symlink node_modules, not .bin, so we avoid the .bin shim relative-jump problem recorded in 16ae80c.
cd frontend_dashboard
if [ -d /opt/fe/node_modules ] && [ ! -e node_modules ]; then
  ln -s /opt/fe/node_modules node_modules
fi
npm run gen:types
cd ..
git diff --exit-code -- frontend_dashboard/src/types/api.gen.ts

echo
echo "== 3) ajv validation of WS envelope schema (T4) =="
cd frontend_dashboard
# WARNING: the glob must NOT be quoted: quoting stops the shell from expanding it and passes it literally to node,
# and Node 20's test runner does NOT expand globs (Node 22+ only),
# so it hands `tests/*.test.mjs` as a literal path to statSync:
#     lib/internal/test_runner/runner.js:136  resolve(process.argv[1])
#     lib/internal/test_runner/runner.js:92   statSync(path) -> ENOENT
#     lib/internal/test_runner/runner.js:142  console.error(`Could not find '${err.path}'`)
#   real error: `Could not find '/workspace/frontend_dashboard/tests/*.test.mjs'`
#   (build cnb-pm7-1k4k4emnd). Local node 24 passes because 24 expands globs --
#   a classic "green locally, red in CI"; do not re-add the quotes.
# The `./` prefix is not required (node also resolves `tests/ws-...mjs` by path, verified locally),
# it is written only to make clear "this is a relative path, not a package name", removing ambiguity. What actually makes this step pass
# is linking node_modules back to the workspace above -- when ajv cannot be resolved, with or without `./` it reports the same
# `Cannot find package 'ajv'`.
node --test ./tests/*.test.mjs
cd ..

echo
echo "== 4) Type check (tsc --noEmit, 0 errors to pass) =="
cd frontend_dashboard
# WARNING: run the tsc entrypoint directly with node, do NOT write `npx tsc`: `npx` only looks on PATH for
#   `node_modules/.bin`, but here node_modules is a symlink to /opt/fe,
#   and the symlink does not change the fact that ".bin is not on PATH"; Node 20's bundled npx errors out when it can't find it
#   directly (npm error could not determine executable to run),
#   unlike npm 10.9 which falls back to fetching over the network. The explicit entrypoint also pins "which tsc is used",
#   avoiding npx's implicit resolution picking a different copy in another environment.
node ./node_modules/typescript/bin/tsc --noEmit
cd ..

echo
echo "== Contract gate passed =="
