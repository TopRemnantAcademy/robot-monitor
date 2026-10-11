// 订单 18 T4 —— WS 信封的**开发模式**校验。
//
// 【为什么 WS 要单独出一套契约】
// FastAPI 的 /openapi.json **不描述 WebSocket**（OpenAPI 3.1 规范里没有 WS 的位置），
// 所以 openapi-typescript 生成的 api.gen.ts 里天然没有 snapshot/telemetry/alert。
// 于是 WS 三分支（backend_ingestion/app/main.py:288/303/305）是契约盲区 ——
// 事实来源是 docs/contracts/ws-envelope.schema.json，本模块用 ajv 消费它。
//
// 【为什么只在开发模式校验（AGENTS.md §3 性能护栏）】
// 100 台 × 10Hz = 1000 msg/s。ajv 单帧校验虽只微秒级，但乘以 1000/s 后
// 会稳定吃掉主线程时间片，与「100ms 节流防重渲染」的既定方针直接冲突。
// 因此：`import.meta.env.DEV` 为真才加载 ajv + schema（动态 import，
// 生产构建时 Vite 静态替换成 false，整段连同 ajv 一起被 tree-shake 掉）。
// 生产环境**零开销**，卡片上不会多出一个校验分支。

import type { WsEnvelope } from '../types/robot';

/** 校验结果：ok 或 人类可读的错误列表（不抛异常，调用方自行决定怎么处理）。 */
export interface EnvelopeCheck {
  ok: boolean;
  errors: string[];
}

/** 生产环境直通：不加载 ajv、不引入 schema。 */
export function shouldValidate(): boolean {
  return import.meta.env.DEV;
}

let validateFn: ((data: unknown) => boolean) | null = null;
let ajvErrors: (() => string[]) | null = null;
let loadPromise: Promise<void> | null = null;

/**
 * 懒加载 ajv + schema（仅开发模式调用）。
 *
 * 用 dynamic import 而不是顶层 import：顶层 import 会让 ajv（约 120 kB）
 * 无论 DEV 与否都进 bundle，直接违反"体积无显著增长"的验收项。
 */
async function ensureValidator(): Promise<void> {
  if (loadPromise) return loadPromise;
  loadPromise = (async () => {
    // 用 ajv 的 2020-12 入口：schema 声明的是 draft 2020-12（$defs / oneOf）。
    // schema 走 Vite 的 `?raw` 内联（见 vite.config.ts 的 define 注释）：
    // docs/contracts/ 在 tsconfig 的 include（仅 ["src"]）之外，直接 import json
    // 会 TS2307；`?raw` 让 Vite 在构建期把文件内容变成字符串，**单一事实来源仍在
    // docs/contracts/ws-envelope.schema.json**，不需要维护第二份拷贝。
    const [{ default: Ajv2020 }, schemaModule] = await Promise.all([
      import('ajv/dist/2020'),
      import('../../../docs/contracts/ws-envelope.schema.json?raw'),
    ]);
    const schema = JSON.parse((schemaModule as { default: string }).default) as object;
    const ajv = new Ajv2020({ allErrors: true, strict: false });
    const validate = ajv.compile(schema);
    validateFn = (data: unknown) => validate(data) as boolean;
    ajvErrors = () =>
      (validate.errors ?? []).map(
        (e) => `${e.instancePath || '/'} ${e.message ?? 'invalid'}`,
      );
  })();
  return loadPromise;
}

/**
 * 校验一个已 JSON.parse 的信封。**仅开发模式**返回真实结果；
 * 生产模式恒返回 `{ok: true}` 且不做任何工作。
 *
 * 用法（见 hooks/useRobotWebSocket.ts 的 handleEnvelope）：
 * 校验失败**不丢弃消息**（避免开发期白屏），而是打 console.error 提示契约漂移。
 */
export function checkEnvelope(envelope: unknown): EnvelopeCheck {
  if (!shouldValidate() || !validateFn || !ajvErrors) return { ok: true, errors: [] };
  const ok = validateFn(envelope);
  return { ok, errors: ok ? [] : ajvErrors() };
}

/**
 * 开发模式初始化：加载校验器。返回一个已就绪的判定函数。
 * 在测试里可直接 `await initWsValidator()` 后再断言 checkEnvelope 的结果。
 */
export async function initWsValidator(): Promise<void> {
  if (!shouldValidate()) return;
  await ensureValidator();
}

/** 便于类型收窄：把 unknown 断言为 WsEnvelope（仅供已通过校验的调用方使用）。 */
export function asWsEnvelope(value: unknown): WsEnvelope | null {
  return value && typeof value === 'object' && 'type' in value ? (value as WsEnvelope) : null;
}
