#!/usr/bin/env node
// Generate OpenAPI -> TS types (Order 18 T1).
//
// [Why wrap the CLI call in a script instead of writing `openapi-typescript` directly]
// In CI the frontend deps are GLOBALLY installed (see ci/contract-gate.Dockerfile),
// while npm script execution prepends `<pkg>/node_modules/.bin` to PATH — that is the
// LOCAL node_modules, which doesn't exist in CI; if npm's bin resolution hits a stale
// local shim first, you get a "local green, CI red" mismatch.
// Here we go explicitly via `node <resolved entry>`: the resolution order is fixed to
// "local node_modules -> global node_modules", so both sides hit the same real entry
// file, independent of npm's PATH injection.
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const pkgRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
// Resolution order: local node_modules -> global node_modules (the Dockerfile does
// `npm ci` into the in-image frontend_dashboard/node_modules, so CI hits the first
// item). BOTH candidates use absolute paths, not a bare `require.resolve(
// 'openapi-typescript')` — the latter walks up from this file's directory collecting
// ALL node_modules, which in a monorepo is enough to pull in an out-of-workspace
// /workspace/node_modules; the result depends on what's in the jenkins workspace and is
// not reproducible. Absolute paths make local and CI hit the same copy.
const candidates = [
  path.join(pkgRoot, 'node_modules', 'openapi-typescript'),
  '/usr/local/lib/node_modules/openapi-typescript',
];

const require_ = createRequire(import.meta.url);
let pkgJson = null;
for (const root of candidates) {
  try {
    pkgJson = require_.resolve(path.join(root, 'package.json'));
    break;
  } catch {
    // try the next candidate location
  }
}

if (!pkgJson) {
  console.error('[gen:types] openapi-typescript not found; tried:');
  for (const c of candidates) console.error('  - ' + c);
  console.error('[gen:types] local: cd frontend_dashboard && npm ci');
  process.exit(1);
}

// bin entry: openapi-typescript 7.x package.json.bin points to bin/cli.js
const cli = path.join(path.dirname(pkgJson), 'bin', 'cli.js');
const res = spawnSync(
  process.execPath,
  [cli, '../docs/contracts/openapi.snapshot.json', '-o', 'src/types/api.gen.ts'],
  { cwd: pkgRoot, stdio: 'inherit' },
);
process.exit(res.status ?? 1);
