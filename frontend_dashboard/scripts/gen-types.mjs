#!/usr/bin/env node
// 生成 OpenAPI → TS 类型（订单 18 T1）。
//
// 【为什么把 CLI 调用包成脚本，而不是直接写 `openapi-typescript`】
// CI 里前端依赖是**全局安装**的（见 ci/contract-gate.Dockerfile），
// 而 npm script 执行时会预置 `<pkg>/node_modules/.bin` 到 PATH —— 那是**本地**
// node_modules，CI 里不存在，若 npm 的 bin 解析先命中一个陈旧的本地 shim，
// 就会产生"本地绿、CI 红"的错配。
// 这里显式走 `node <resolved entry>`：解析顺序固定为「本地 node_modules → 全局
// node_modules」，两边都命中同一份真实入口文件，不依赖 npm 的 PATH 注入。
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const pkgRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
// 解析顺序：本地 node_modules → 全局 node_modules（Dockerfile 里 npm ci 到镜像内的
// frontend_dashboard/node_modules，故 CI 会命中第一项）。**两个候选都用绝对路径**，
// 不用裸 `require.resolve('openapi-typescript')` —— 后者会从本文件所在目录向上收集
// 所有 node_modules，在有依赖仓库里足以把工作区外的 /workspace/node_modules 卷进来，
// 解析结果取决于 jenkins 工作区里有什么，不可复现。绝对路径让本地与 CI 命中同一份。
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
    // 换下一个候选位置
  }
}

if (!pkgJson) {
  console.error('[gen:types] 未找到 openapi-typescript，已尝试：');
  for (const c of candidates) console.error('  - ' + c);
  console.error('[gen:types] 本地：cd frontend_dashboard && npm ci');
  process.exit(1);
}

// bin 入口：openapi-typescript 7.x 的 package.json.bin 指向 bin/cli.js
const cli = path.join(path.dirname(pkgJson), 'bin', 'cli.js');
const res = spawnSync(
  process.execPath,
  [cli, '../docs/contracts/openapi.snapshot.json', '-o', 'src/types/api.gen.ts'],
  { cwd: pkgRoot, stdio: 'inherit' },
);
process.exit(res.status ?? 1);
