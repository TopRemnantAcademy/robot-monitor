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
  { entry: '.e2e/entry_drawer.tsx', out: '.e2e/drawer.cjs',
    // 订单 09：产品改用 echarts-for-react 的 core 入口，需一并 external：
    //   1) 避免 CJS default 互操作把 module object 当成组件传给 React；
    //   2) 让产品与测试共用同一个 echarts 实例注册表
    //      （否则产物内联一份 echarts/core，测试侧 getInstanceByDom 恒为 null）。
    // echarts 全家桶统一 external：核心与 charts/components/renderers 必须来自
    // 同一份模块实例，否则 use() 注册的表与 init() 读取的表不是同一张（xAxis not found）。
    external: ['echarts', 'echarts/core', 'echarts/charts', 'echarts/components',
      'echarts/renderers', 'echarts-for-react', 'echarts-for-react/lib/core'] },
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
