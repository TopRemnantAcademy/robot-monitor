// msw 处理器：与后端 REST 契约一一对应（spec/00 §5.3）。
//
// 只覆盖测试真正会打到的端点，且 URL 逐字对齐前端 `API_BASE + 路径`：
//   GET {API_BASE}/robots/{id}/history?minutes=5&resolution=10s
// 返回体的字段名必须与 `RobotHistoryResponse`（src/types/robot.ts）一致，
// 否则组件解析出来是 undefined，测试会红 —— 这正是我们要的「契约即断言」。
import { http, HttpResponse } from 'msw';

/** 与 src/components/RobotDetailDrawer.tsx 的 API_BASE 默认值一致。 */
export const API_BASE = 'http://localhost:8000';

/** 两个时间点的历史（10s 分辨率），含全部 24 关节的 temp/torque/positions。 */
export const SAMPLE_HISTORY = {
  robot_id: 'humanoid-042',
  points: [
    {
      time: '2026-10-10T23:49:00Z',
      battery_pct: 20.5,
      max_joint_temp: 66.2,
      joint_temperatures: { left_elbow_pitch: 60.0 },
      joint_torques: { left_elbow_pitch: 1.5 },
      joint_positions: { left_elbow_pitch: 0.42 },
    },
    {
      time: '2026-10-10T23:49:10Z',
      battery_pct: 19.9,
      max_joint_temp: 67.8,
      joint_temperatures: { left_elbow_pitch: 62.5 },
      joint_torques: { left_elbow_pitch: 1.7 },
      joint_positions: { left_elbow_pitch: 0.55 },
    },
  ],
};

/** 平台支持的机型目录样例（与后端 GET /supported-robots 字段逐字对齐）。 */
export const SAMPLE_SUPPORTED_ROBOTS = {
  models: [
    {
      model_type: 'HUMANOID-24DOF',
      display_name: 'Humanoid 24-DOF',
      form_factor: '双足人形',
      dof: 24,
      vendor: 'RobotMonitor Platform',
      description: '平台主力机型：24 自由度高拟人双足机器人。',
      generic: false,
      total: 100,
      online: 97,
      firmware_versions: ['1.0.0'],
      stations: 10,
    },
  ],
  total_robots: 100,
};

/** 默认 handler 集：history 返回上面这份样例数据；supported-robots 返回机型目录。 */
export const handlers = [
  http.get(`${API_BASE}/robots/:robotId/history`, () => HttpResponse.json(SAMPLE_HISTORY)),
  http.get(`${API_BASE}/supported-robots`, () => HttpResponse.json(SAMPLE_SUPPORTED_ROBOTS)),
];
