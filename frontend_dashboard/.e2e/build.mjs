// Unified test bundling entry (bundle product source into Node-runnable CJS so real implementations can be used for integration verification)
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
  { entry: '.e2e/entry_alert.tsx', out: '.e2e/alert.cjs', external: [] },
  { entry: '.e2e/entry_drawer.tsx', out: '.e2e/drawer.cjs',
    // Order 09: the product switched to echarts-for-react's core entry, which must also be externalized:
    //   1) avoid CJS default-interop passing the module object as a component to React;
    //   2) let product and tests share the same echarts instance registry
    //      (otherwise the bundle inlines its own echarts/core and getInstanceByDom on the test side is always null).
    // The whole echarts family is externalized uniformly: core, charts/components/renderers must come from
    // the same module instance, otherwise the table registered via use() and the one read via init() won't match (xAxis not found).
    external: ['echarts', 'echarts/core', 'echarts/charts', 'echarts/components',
      'echarts/renderers', 'echarts-for-react', 'echarts-for-react/lib/core'] },
  // Order 26 (I-20): error boundary + react-query cache/retry verification entry
  { entry: '.e2e/entry_order26.tsx', out: '.e2e/order26.cjs', external: [] },
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
