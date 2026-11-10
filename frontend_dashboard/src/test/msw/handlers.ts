// msw handlers: one-to-one with the backend REST contract (spec/00 §5.3).
//
// Only cover the endpoints the tests actually hit, and align the URL verbatim with the frontend `API_BASE + path`:
//   GET {API_BASE}/robots/{id}/history?minutes=5&resolution=10s
// The response field names must match `RobotHistoryResponse` (src/types/robot.ts),
// otherwise the component parses undefined and the test goes red —— which is exactly the "contract is the assertion" we want.
import { http, HttpResponse } from 'msw';

/** Matches the API_BASE default in src/components/RobotDetailDrawer.tsx. */
export const API_BASE = 'http://localhost:8000';

/** History at two time points (10s resolution), covering all 24 joints' temp/torque/positions. */
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

/** Sample catalog of platform-supported robot models (verbatim field alignment with backend GET /supported-robots). */
export const SAMPLE_SUPPORTED_ROBOTS = {
  models: [
    {
      model_type: 'HUMANOID-24DOF',
      display_name: 'Humanoid 24-DOF',
      form_factor: 'Bipedal humanoid',
      dof: 24,
      vendor: 'RobotMonitor Platform',
      description: 'Platform main model: 24-DOF high-fidelity bipedal humanoid robot.',
      generic: false,
      total: 100,
      online: 97,
      firmware_versions: ['1.0.0'],
      stations: 10,
    },
  ],
  total_robots: 100,
};

/** Default handler set: history returns the sample above; supported-robots returns the model catalog. */
export const handlers = [
  http.get(`${API_BASE}/robots/:robotId/history`, () => HttpResponse.json(SAMPLE_HISTORY)),
  http.get(`${API_BASE}/supported-robots`, () => HttpResponse.json(SAMPLE_SUPPORTED_ROBOTS)),
];
