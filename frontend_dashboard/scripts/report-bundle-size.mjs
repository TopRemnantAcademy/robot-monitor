// ISSUE #62 — frontend bundle-size guardrail + ECharts full-package import guard.
//
// Why this file exists:
//   1) Bundle size is a one-time battle, but without a gatekeeper it silently grows
//      back. The original 1209 kB (gzip 401 kB) root cause was `echarts-for-react`'s
//      default entry internally doing `require("echarts")` (full package); switching to
//      the core entry + on-demand registration dropped it to ~243 kB gzip. This guard's
//      job is to turn "someone accidentally adds a full import again" red in CI, rather
//      than waiting for the product to report a slow first screen.
//   2) The gate used to be only `npm test`; `tsc --noEmit` was not included — so the
//      "0 tsc errors" required by the #62 acceptance was long unmet (`npm run build` =
//      `tsc --noEmit && vite build`, the first step failing) while main stayed all
//      green. typecheck has since been added to the CI stage (see .cnb.yml); this file
//      owns the size half.
//
// Threshold source (not a guess): the #62 acceptance criterion — total gzip of
// `dist/assets/*.js` < 320 KB. WARN_KB=300 is a warning band (20 KB headroom; hint
// before it blows up, don't wait until it breaks); FAIL_KB=320 is a hard cap — over it
// means the acceptance is unmet, red.
// Why gzip not raw: the acceptance uses the same yardstick as external gzip/brotli
// compression; raw is polluted by source-map and comment noise.
//
// Usage: `node scripts/report-bundle-size.mjs` (requires `vite build` first)

import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { join, extname, sep } from 'node:path';
import { gzipSync } from 'node:zlib';

const DIST = 'dist/assets';
const SRC = 'src';
const WARN_KB = 300;
const FAIL_KB = 320;

function walk(dir, exts) {
  const out = [];
  if (!existsSync(dir)) return out;
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p, exts));
    else if (exts.includes(extname(p))) out.push(p);
  }
  return out;
}

// ---------------------------------------------------------------- 1. size

const jsFiles = walk(DIST, ['.js']);

if (jsFiles.length === 0) {
  console.error(`[bundle] no artifacts found: ${DIST}/*.js —— run npx vite build first`);
  process.exit(1);
}

let raw = 0;
let gz = 0;
const rows = [];
for (const f of jsFiles) {
  const buf = readFileSync(f);
  const g = gzipSync(buf, { level: 9 }).length;
  raw += buf.length;
  gz += g;
  rows.push([f.replaceAll(sep, '/'), buf.length, g]);
}

const kb = (n) => (n / 1024).toFixed(2);
console.log(`[bundle] ${DIST} has ${jsFiles.length} chunk(s):`);
for (const [f, r, g] of rows.sort((a, b) => b[2] - a[2])) {
  console.log(`  ${f.padEnd(42)} raw=${kb(r).padStart(8)} kB  gzip=${kb(g).padStart(8)} kB`);
}
console.log(`  ${'TOTAL'.padEnd(42)} raw=${kb(raw).padStart(8)} kB  gzip=${kb(gz).padStart(8)} kB`);

const gzKb = gz / 1024;
if (gzKb > FAIL_KB) {
  console.error(
    `[bundle] ✗ gzip ${kb(gz)} kB exceeds budget ${FAIL_KB} kB —— ISSUE #62 acceptance unmet.\n` +
      `          common rollback cause: the full import \`import * as echarts from 'echarts'\` is back\n` +
      `          (full package ≈ +800 kB); see top of src/lib/echartsCore.ts.`,
  );
  process.exit(1);
}
if (gzKb > WARN_KB) {
  console.warn(`[bundle] ⚠ gzip ${kb(gz)} kB is approaching the ${FAIL_KB} kB budget (${kb(FAIL_KB * 1024 - gz)} kB left)`);
}

// ------------------------------------------- 2. ECharts full-import guard

// Only three forms are allowed: `echarts/core`, `echarts/charts`, `echarts/components`,
// `echarts/renderers` (sub-path, on demand); the BARE package name `from 'echarts'` is
// forbidden — it is the line that originally pushed the main chunk to 1209 kB. To avoid
// false positives, the regex requires the quote to close right after `echarts` (so it
// doesn't match sub-paths like `echarts/core`).
//
// ⚠️ Must skip comment lines: the note at the top of src/lib/echartsCore.ts deliberately
// REFERENCES this anti-pattern (`default entry internally require("echarts")`) as a
// negative example — the first real run flagged it as a violation (exit 1). Here we use a
// line-level heuristic (line starts with //, *, or /*), not full TS parsing: the cost is
// that a string like `const s = "from 'echarts'"` could theoretically slip through, the
// benefit is no parser needed for a guard script. Someone truly trying to hide an import
// still can't beat the size red line below.
const BARE_ECHARTS = /from\s+['"]echarts['"]|require\(\s*['"]echarts['"]\s*\)/;
const offenders = [];
for (const f of walk(SRC, ['.ts', '.tsx'])) {
  const text = readFileSync(f, 'utf8');
  text.split('\n').forEach((line, i) => {
    const t = line.trim();
    if (t.startsWith('//') || t.startsWith('*') || t.startsWith('/*')) return;  // comment: skip
    if (BARE_ECHARTS.test(line)) offenders.push(`${f.replaceAll(sep, '/')}:${i + 1}  ${line.trim()}`);
  });
}

if (offenders.length > 0) {
  console.error('[bundle] ✗ detected bare-package ECharts import (pulls in the full bundle):');
  for (const o of offenders) console.error(`  ${o}`);
  console.error("          correct form: `import * as echarts from 'echarts/core'` + on-demand `echarts.use([...])`");
  process.exit(1);
}

console.log(`[bundle] ✓ gzip ${kb(gz)} kB < ${FAIL_KB} kB; no bare ECharts import`);
