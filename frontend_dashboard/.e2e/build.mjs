// 统一的测试打包入口（把产品源码打成 Node 可跑的 CJS，便于用真实实现做集成验证）
import esbuild from 'esbuild';

const wsPort = process.env.E2E_WS_PORT ?? '8123';
const apiBase = process.env.E2E_API_BASE ?? 'http://localhost:8000';

const define = {
  'import.meta.env.VITE_WS_HOST': '"localhost"',
  'import.meta.env.VITE_WS_PORT': `"${wsPort}"`,
  'import.meta.env.VITE_API_BASE': `"${apiBase}"`,
};

const targets = [
  { entry: '.e2e/entry.tsx', out: '.e2e/bundle.cjs', external: [] },
  { entry: '.e2e/entry_drawer.tsx', out: '.e2e/drawer.cjs', external: ['echarts-for-react'] },
];

for (const t of targets) {
  esbuild.buildSync({
    entryPoints: [t.entry],
    bundle: true,
    outfile: t.out,
    platform: 'node',
    format: 'cjs',
    jsx: 'automatic',
    loader: { '.tsx': 'tsx', '.ts': 'ts' },
    external: t.external,
    define,
    logLevel: 'error',
  });
  console.log(`bundled ${t.entry} → ${t.out}`);
}
