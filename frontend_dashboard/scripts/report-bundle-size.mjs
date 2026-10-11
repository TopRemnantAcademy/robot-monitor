// ISSUE #62 —— 前端 bundle 体积门禁 + ECharts 全量引入守卫。
//
// 为什么要有这个文件：
//   1) 体积是一次性战斗，但没有守门员就会**悄悄长回去**。原状 1209 kB（gzip 401 kB）
//      的根因是 `echarts-for-react` 默认入口内部 `require("echarts")` 全量包；改走
//      core 入口 + 按需注册后降到约 243 kB gzip。这条防线的作用是让"下次有人不小心
//      加一行全量 import"在 CI 里就红，而不是等产品反馈首屏慢。
//   2) 门禁此前只有 `npm test`，`tsc --noEmit` 不在其中 —— 于是 `#62` 验收要求的
//      "0 tsc 错误"长期不满足（`npm run build` = `tsc --noEmit && vite build`，
//      第一步就失败）而 main 是全绿的。typecheck 已补进 CI stage（见 .cnb.yml），
//      本文件负责体积那一半。
//
// 阈值来源（不是拍脑袋）：issue #62 的验收口径 —— `dist/assets/*.js` 总 gzip < 320 KB。
//   WARN_KB=300 预警带（留 20 KB 余量，接近预算时先提示，不要等到爆了才说）；
//   FAIL_KB=320 硬卡 —— 超了就是不满足验收，红。
// 为什么 gzip 而不是 raw：验收与外部 gzip/brotli 压缩是一致的口径；raw 会被 sourcemap
// 与注释噪声干扰。
//
// 用法：`node scripts/report-bundle-size.mjs`（需先 `vite build`）

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

// ---------------------------------------------------------------- 1. 体积

const jsFiles = walk(DIST, ['.js']);

if (jsFiles.length === 0) {
  console.error(`[bundle] 未找到产物：${DIST}/*.js —— 先跑 npx vite build`);
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
console.log(`[bundle] ${DIST} 共 ${jsFiles.length} 个 chunk：`);
for (const [f, r, g] of rows.sort((a, b) => b[2] - a[2])) {
  console.log(`  ${f.padEnd(42)} raw=${kb(r).padStart(8)} kB  gzip=${kb(g).padStart(8)} kB`);
}
console.log(`  ${'TOTAL'.padEnd(42)} raw=${kb(raw).padStart(8)} kB  gzip=${kb(gz).padStart(8)} kB`);

const gzKb = gz / 1024;
if (gzKb > FAIL_KB) {
  console.error(
    `[bundle] ✗ gzip ${kb(gz)} kB 超过预算 ${FAIL_KB} kB —— ISSUE #62 验收不满足。\n` +
      `          常见回滚原因：又把 \`import * as echarts from 'echarts'\` 引回来了\n` +
      `          （全量包 ≈ +800 kB）；见 src/lib/echartsCore.ts 顶部说明。`,
  );
  process.exit(1);
}
if (gzKb > WARN_KB) {
  console.warn(`[bundle] ⚠ gzip ${kb(gz)} kB 已逼近 ${FAIL_KB} kB 预算（余 ${kb(FAIL_KB * 1024 - gz)} kB）`);
}

// ------------------------------------------- 2. ECharts 全量引入守卫

// 允许的写法只有三种：`echarts/core`、`echarts/charts`、`echarts/components`、
// `echarts/renderers`（子路径按需）；**裸包名** `from 'echarts'` 一律禁止 ——
// 它就是当初把主 chunk 顶到 1209 kB 的那一行。为避免误伤，用正则要求引号紧跟
// `echarts` 后闭合（不匹配 `echarts/core` 这类子路径）。
//
// ⚠️ 必须跳过注释行：src/lib/echartsCore.ts 顶部的说明里恰好**引用**了这条反模式
// （「默认入口内部 require("echarts")」）作为反面教材 —— 首次实跑就把它当成违规
// 命中了（exit 1）。这里是行级启发式（行首 // 或 *），不做完整 TS 解析：
// 代价是理论上能把形如 `const s = "from 'echarts'"` 的字符串漏掉，
// 收益是不必为一个守卫脚本引入解析器。真要藏 import 的人也躲不过下面的体积红线。
const BARE_ECHARTS = /from\s+['"]echarts['"]|require\(\s*['"]echarts['"]\s*\)/;
const offenders = [];
for (const f of walk(SRC, ['.ts', '.tsx'])) {
  const text = readFileSync(f, 'utf8');
  text.split('\n').forEach((line, i) => {
    const t = line.trim();
    if (t.startsWith('//') || t.startsWith('*') || t.startsWith('/*')) return;  // 注释：跳过
    if (BARE_ECHARTS.test(line)) offenders.push(`${f.replaceAll(sep, '/')}:${i + 1}  ${line.trim()}`);
  });
}

if (offenders.length > 0) {
  console.error('[bundle] ✗ 检测到裸包名 ECharts 引入（会拉进全量包）：');
  for (const o of offenders) console.error(`  ${o}`);
  console.error("          正确写法：`import * as echarts from 'echarts/core'` + 按需 `echarts.use([...])`");
  process.exit(1);
}

console.log(`[bundle] ✓ gzip ${kb(gz)} kB < ${FAIL_KB} kB；无裸 ECharts 引入`);
