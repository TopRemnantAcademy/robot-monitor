// Test entry: mount the real AlertLogTable and count how many times alert-row DOM nodes are (re)created.
// Goal: prove that when a new alert arrives, existing rows are NOT rebuilt (memo works) — instead of remounting all 60 rows.
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

// MutationObserver counts the number of "newly added alert-row buttons":
//  - if memo fails, every incoming alert rebuilds all 60 rows because the whole table reorders;
//  - if memo works, every incoming alert adds only 1 new row.
let rowCreations = 0;
const countAddedRows = (mutations: MutationRecord[]) => {
  for (const m of mutations) {
    for (const node of Array.from(m.addedNodes)) {
      if (node.nodeType !== 1) continue;
      const el = node as HTMLElement;
      // Alert row: textContent contains the "S<severity>" chip
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
// Observe the entire alert-table subtree
new MutationObserver(countAddedRows).observe(root, { childList: true, subtree: true });
export {};
