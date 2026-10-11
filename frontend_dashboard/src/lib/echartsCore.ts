// ECharts 按需注册（bundle 瘦身）。全部 import 只走 `echarts/core` 与子路径，
// 绝不 import 'echarts' 全量包。
//
// 为什么需要这个文件：
//   `echarts-for-react` 的默认入口（lib/index.js:4）内部 `require("echarts")`
//   —— 即整个 echarts 包（含 20+ 图表类型、全部组件、两种渲染器），
//   Rollup 无法 tree-shake，导致主 chunk 1209 kB。
//   改为使用其 core 入口（`echarts-for-react/lib/core`），把「要注册哪些能力」
//   交还给我们，只引入本仓库真正用到的模块。
//
// 注册清单与「大屏当前实际渲染的图表」逐项对照（读代码取证，非按名字想当然）：
//   - LineChart   ：RobotDetailDrawer.tsx:148 `type: 'line'` —— 该组件在
//                   src/components/ 下是全仓**唯一**挂载 ECharts 的地方
//                   （`grep -rn echarts src/` 仅命中 RobotDetailDrawer.tsx:11-12,257
//                    与 JointHeatmap.tsx:10 的一行注释）。
//   - GridComponent   ：RobotDetailDrawer.tsx:134,140 `xAxis.type='category'` + `yAxis.type='value'`
//   - TooltipComponent：RobotDetailDrawer.tsx:125 `tooltip.trigger='axis'`
//   - LegendComponent ：RobotDetailDrawer.tsx:126-130 `legend.data`
//   - BarChart / GaugeChart：大屏**当前无任何柱子/仪表盘挂在 ECharts 上**，
//                   属**预留注册**（见下方说明），不是「实际使用的图表类型」。
//   - CanvasRenderer  ：默认渲染器。
//
// 为什么仍然注册 BarChart / GaugeChart：
//   Issue #62 明确要求保留 line/bar/gauge 三件套，且「禁止因精简删掉大屏实际使用的
//   图表类型」。二者代价经同环境实测仅 **+9.46 kB gzip**（226.16 → 235.62 kB），
//   远低于 320 kB 预算；若不注册，后续任何柱状/仪表盘需求都会在运行时抛
//   `Series bar is used but not imported`。属权衡后的有意保留。
//
// 为什么不注册 HeatmapChart：
//   JointHeatmap.tsx 是纯 <button> + CSS grid 自绘（该文件无任何 echarts import），
//   因此**不注册** HeatmapChart。

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

/** 注册完毕的 echarts core 实例，供 echarts-for-react 的 core 入口使用。 */
export default echarts;
