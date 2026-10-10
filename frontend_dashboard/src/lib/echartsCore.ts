// 订单 09：ECharts 按需注册（bundle 瘦身）。
//
// 为什么需要这个文件：
//   `echarts-for-react` 的默认入口（lib/index.js）内部 `require("echarts")`
//   —— 即整个 echarts 包（含 20+ 图表类型、全部组件、两种渲染器），
//   Rollup 无法 tree-shake，导致主 chunk 1209 kB。
//   改为使用其 core 入口（`echarts-for-react/lib/core`），把「要注册哪些能力」
//   交还给我们，只引入本仓库真正用到的模块。
//
// 本仓库实际用到的 ECharts 能力（逐项对照 RobotDetailDrawer.tsx 的 chartOption，
// src/components/RobotDetailDrawer.tsx:110-146）：
//   - 折线图 LineChart（3 条 line series：温度/扭矩/角度）
//   - 直角坐标系 GridComponent（xAxis.type='category' + yAxis.type='value'）
//   - TooltipComponent（tooltip.trigger='axis'）
//   - LegendComponent（legend.data）
//   - CanvasRenderer（默认渲染器）
//   另：折线用 connectNulls / showSymbol / lineStyle / itemStyle —— 属 LineChart
//   自身能力，无需额外注册。
//
// 注意：JointHeatmap.tsx 不用 ECharts（见该文件：纯 <button> CSS grid 自绘），
//   因此**不注册** HeatmapChart。这是读代码确认后的结论，不是猜测。

import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);

/** 注册完毕的 echarts core 实例，供 echarts-for-react 的 core 入口使用。 */
export default echarts;
