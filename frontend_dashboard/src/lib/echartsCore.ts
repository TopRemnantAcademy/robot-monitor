// ECharts on-demand registration (bundle slimming). Every import goes through `echarts/core` and sub-paths only,
// never the full `echarts` package.
//
// Why this file is needed:
//   `echarts-for-react`'s default entry (lib/index.js:4) internally does `require("echarts")`
//   —— i.e. the entire echarts package (20+ chart types, all components, both renderers),
//   which Rollup cannot tree-shake, pushing the main chunk to 1209 kB.
//   Switching to its core entry (`echarts-for-react/lib/core`) hands "which capabilities to register"
//   back to us, importing only the modules this repo actually uses.
//
// Registration list cross-checked item-by-item against "the charts the big screen actually renders"
// (verified by reading code, not guessed by name):
//   - LineChart   : RobotDetailDrawer.tsx:148 `type: 'line'` —— that component is the
//                   **only** place under src/components/ that mounts ECharts
//                   (`grep -rn echarts src/` only hits RobotDetailDrawer.tsx:11-12,257
//                    and one comment line in JointHeatmap.tsx:10).
//   - GridComponent   : RobotDetailDrawer.tsx:134,140 `xAxis.type='category'` + `yAxis.type='value'`
//   - TooltipComponent: RobotDetailDrawer.tsx:125 `tooltip.trigger='axis'`
//   - LegendComponent : RobotDetailDrawer.tsx:126-130 `legend.data`
//   - BarChart / GaugeChart: the big screen currently has **no** bars/gauges mounted on ECharts,
//                   so these are **reserved registrations** (see note below), not "actually used chart types".
//   - CanvasRenderer  : default renderer.
//
// Why BarChart / GaugeChart are still registered:
//   Issue #62 explicitly requires keeping the line/bar/gauge trio, and forbids "deleting a chart type the
//   big screen actually uses" in the name of slimming. The cost of both was measured in the same environment
//   at only **+9.46 kB gzip** (226.16 → 235.62 kB), well under the 320 kB budget; if not registered, any
//   future bar/gauge requirement would throw `Series bar is used but not imported` at runtime. This is a
//   deliberate retention after trade-off.
//
// Why HeatmapChart is NOT registered:
//   JointHeatmap.tsx is pure <button> + CSS grid self-drawing (that file has no echarts import at all),
//   so HeatmapChart is **not registered**.

import * as echarts from 'echarts/core';
import { LineChart, BarChart, GaugeChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  LineChart,
  BarChart,
  GaugeChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  CanvasRenderer,
]);

/** The fully-registered echarts core instance, for echarts-for-react's core entry. */
export default echarts;
