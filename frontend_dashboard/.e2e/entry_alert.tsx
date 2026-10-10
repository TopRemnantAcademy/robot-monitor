// 验证入口：挂载真实 AlertLogTable，并统计「告警行 DOM 节点的重建次数」。
// 目的：证明新告警到达时，已有行不被重建（memo 生效），而不是整表 60 行重挂。
import { createRoot } from 'react-dom/client';
import AlertLogTable from '../src/components/AlertLogTable';
import { useRobotStore } from '../src/store/robotStore';
import type { RobotAlert } from '../src/types/robot';

declare global {
  interface Window {
    __ALERT__: {
      push: (a: RobotAlert) => void;
      rowCreations: number;
      reset: () => void;
    };
  }
}

// MutationObserver 统计「新增的告警行按钮」数量：
// 若 memo 失效，每来一条告警都会因整表重排而重建全部 60 行；
// 若 memo 生效，每来一条只新增 1 行。
let rowCreations = 0;
const countAddedRows = (mutations: MutationRecord[]) => {
  for (const m of mutations) {
    for (const node of Array.from(m.addedNodes)) {
      if (node.nodeType !== 1) continue;
      const el = node as HTMLElement;
      // 告警行：textContent 里含 "S<severity>" chip
      if (/·\s*S\d/.test(el.textContent ?? '')) rowCreations += 1;
    }
  }
};

window.__ALERT__ = {
  push: (a) => useRobotStore.getState().pushAlert(a),
  get rowCreations() { return rowCreations; },
  reset: () => { rowCreations = 0; },
} as unknown as Window['__ALERT__'];

const root = document.getElementById('root')!;
createRoot(root).render(<AlertLogTable />);
// 观察整个告警表子树
new MutationObserver(countAddedRows).observe(root, { childList: true, subtree: true });
export {};
