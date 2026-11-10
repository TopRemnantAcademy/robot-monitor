// Order 18 T4 —— WS envelope **dev-mode** validation.
//
// WHY WS NEEDS ITS OWN CONTRACT
// FastAPI's /openapi.json does **not describe WebSocket** (the OpenAPI 3.1 spec has no place for WS),
// so openapi-typescript's generated api.gen.ts naturally has no snapshot/telemetry/alert.
// Thus the WS three branches (backend_ingestion/app/main.py:288/303/305) are a contract blind spot ——
// the source of truth is docs/contracts/ws-envelope.schema.json, and this module consumes it with ajv.
//
// WHY ONLY VALIDATE IN DEV MODE (AGENTS.md §3 performance guardrail)
// 100 robots × 10Hz = 1000 msg/s. A single ajv frame check is only microseconds, but multiplied by 1000/s
// it would steadily eat the main-thread time slice, directly conflicting with the "100ms throttle prevents re-render" policy.
// Therefore: ajv + schema are loaded only when `import.meta.env.DEV` is true (dynamic import,
// which Vite statically replaces with false in production builds, tree-shaking the whole thing along with ajv).
// Production environment has **zero overhead**; no extra validation branch appears on the cards.

import type { WsEnvelope } from '../types/robot';

/** Validation result: ok or a human-readable list of errors (does not throw; the caller decides how to handle it). */
export interface EnvelopeCheck {
  ok: boolean;
  errors: string[];
}

/** Production short-circuit: does not load ajv or import the schema. */
export function shouldValidate(): boolean {
  return import.meta.env.DEV;
}

let validateFn: ((data: unknown) => boolean) | null = null;
let ajvErrors: (() => string[]) | null = null;
let loadPromise: Promise<void> | null = null;

/**
 * Lazily load ajv + schema (called only in dev mode).
 *
 * Use a dynamic import rather than a top-level import: a top-level import would pull ajv (~120 kB)
 * into the bundle regardless of DEV, directly violating the "no significant bundle growth" acceptance item.
 */
async function ensureValidator(): Promise<void> {
  if (loadPromise) return loadPromise;
  loadPromise = (async () => {
    // Use ajv's 2020-12 entry: the schema declares draft 2020-12 ($defs / oneOf).
    // The schema is inlined via Vite's `?raw` (see the define comment in vite.config.ts):
    // docs/contracts/ is outside tsconfig's include (only ["src"]), so importing the json directly
    // would be TS2307; `?raw` makes Vite turn the file content into a string at build time, and the
    // **single source of truth remains docs/contracts/ws-envelope.schema.json** —— no second copy to maintain.
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
 * Validate an already JSON.parse'd envelope. Returns the real result **in dev mode only**;
 * in production it always returns `{ok: true}` and does no work.
 *
 * Usage (see hooks/useRobotWebSocket.ts's handleEnvelope):
 * on validation failure it does **not drop the message** (to avoid a white screen in dev),
 * but logs a console.error hinting at contract drift.
 */
export function checkEnvelope(envelope: unknown): EnvelopeCheck {
  if (!shouldValidate() || !validateFn || !ajvErrors) return { ok: true, errors: [] };
  const ok = validateFn(envelope);
  return { ok, errors: ok ? [] : ajvErrors() };
}

/**
 * Dev-mode initialization: load the validator. Returns a ready-to-use judgment function.
 * In tests you can directly `await initWsValidator()` before asserting checkEnvelope's result.
 */
export async function initWsValidator(): Promise<void> {
  if (!shouldValidate()) return;
  await ensureValidator();
}

/** Convenience for type narrowing: assert unknown to WsEnvelope (for callers that already passed validation only). */
export function asWsEnvelope(value: unknown): WsEnvelope | null {
  return value && typeof value === 'object' && 'type' in value ? (value as WsEnvelope) : null;
}
