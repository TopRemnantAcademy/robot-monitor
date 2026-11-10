// Front-end self-reported re-render counts (Order 16 T2) —— the **only evidence** for AGENTS.md §3's front-end guardrails.
//
// QUESTION TO ANSWER
// The guardrail says "no React re-render per WS message; 100ms throttling is mandatory".
// Enterprise acceptance will inevitably ask: "What makes you say you don't re-render 1000 times/sec?"
// Before, we could only say so by mouth: there was no counter in the code for how many times
// `applyTelemetryBatch` was called or how many times the cards rendered —— it was completely invisible at runtime.
// This module is that "what makes you say so".
//
// SEMANTICS OF THE THREE COUNTS —— the relationship among them is the entire reason this module exists
//   telemetryReceived : number of telemetry messages received over WS (≈ 1000/s at steady state)
//   storeCommits      : actual number of times applyTelemetryBatch was called (≈ 10/s at steady state)
//   cardRenders       : number of times FleetOverviewCards rendered (≈ storeCommits, must **NOT** ≈ telemetryReceived)
//
//   Acceptance semantics (Order 16 §2):
//     · telemetryReceived / storeCommits ≈ 10  →  100ms throttling is in effect
//     · cardRenders ≈ storeCommits (not ≈ telemetryReceived) → memo/subscription granularity is correct
//   If storeCommits ≈ telemetryReceived, the throttle was bypassed (= 1000 re-renders/sec);
//   if cardRenders ≈ telemetryReceived, the card wall's subscription granularity is too coarse.
//
// WHY THE COUNTERS CANNOT LIVE IN REACT STATE
// Putting it in useState is itself a setState → triggers a re-render → the count changes → another render.
// This would **manufacture** the very problem it is meant to observe (observer effect), even an infinite loop.
// So everything is a module-level mutable object + useRef increment: reads/writes have zero React involvement on the render path.

/** Runtime count snapshot. */
export interface ObservabilitySnapshot {
  /** Number of telemetry messages received over WS (includes snapshot frames; a snapshot also counts as "one received"). */
  wsMsgs: number;
  /** Actual number of times applyTelemetryBatch was called (100ms flush frame; only non-empty frames are counted). */
  storeCommits: number;
  /** Number of times FleetOverviewCards rendered (useRef increment; does not trigger extra renders). */
  cardRenders: number;
  /** wsMsgs / storeCommits, the throttle multiplier. Should be ≈ 10 at steady state. */
  ratio: number;
  /** Length of the window since the last print (seconds), used to convert counts into a rate. */
  windowSeconds: number;
}

// Module-level mutable counters. Deliberately not a global store: this module is a **pure bystander**,
// it must not introduce any state-management refactor (Order 16 forbidden item 4), nor make components subscribe to it.
const counters = {
  wsMsgs: 0,
  storeCommits: 0,
  cardRenders: 0,
};

/** Wall-clock of the last print (ms). Uses performance.now() for a monotonic clock. */
let lastReportAt = now();

/** The 5s print timer handle (for idempotent installation). */
let reportTimer: ReturnType<typeof setInterval> | null = null;

function now(): number {
  // performance.now() is a monotonic clock, unaffected by system time jumps (NTP corrections) ——
  // with Date.now(), a clock rollback would make the window length negative and the rate computed negative.
  return typeof performance !== 'undefined' ? performance.now() : Date.now();
}

/** Called when WS receives one telemetry/snapshot frame (call site: useRobotWebSocket). */
export function countWsMessage(): void {
  counters.wsMsgs += 1;
}

/** Called when applyTelemetryBatch is called (call site: useRobotWebSocket's flush frame). */
export function countStoreCommit(): void {
  counters.storeCommits += 1;
}

/**
 * Card wall render count. **Called directly inside the component function body** (paired with useRef increment semantics).
 *
 * Why provide a dedicated function instead of letting components do `ref.current++` themselves: keep "how to count"
 * in one place, so a future change of metric (e.g. dedupe by frame) only needs editing here, not every component.
 * Note: this function must **NOT** trigger any setState —— it is called during the render phase.
 *
 * @param localCount the component's own useRef current value (1-based). Used only for cross-checking:
 *        it must grow in sync with the global cumulative; if a component unmounts and remounts (e.g. switching login),
 *        the local count restarts from 1 while the global keeps accumulating, and that difference is itself the "remount count".
 */
export function countCardRender(localCount: number): void {
  counters.cardRenders += 1;
  // Cross-check: local > global can only mean the counter was wrongly zeroed (resetObservability called in production).
  // Surface it with console.warn rather than an assert that throws —— an observability module must never crash the page.
  if (localCount > counters.cardRenders) {
    console.warn('[observability] cardRenders local > global', {
      localCount,
      global: counters.cardRenders,
    });
  }
}

/** Read-only snapshot, does not change counts (for printing and tests). */
export function getSnapshot(): ObservabilitySnapshot {
  const windowSeconds = Math.max(0, (now() - lastReportAt) / 1000);
  const { wsMsgs, storeCommits, cardRenders } = counters;
  return {
    wsMsgs,
    storeCommits,
    cardRenders,
    // Zero-division guard: at startup storeCommits may be 0 (no frame flushed yet).
    // Returning NaN would break the eyeball "≈10" check, so when commits are 0 the ratio is recorded as 0.
    ratio: storeCommits > 0 ? wsMsgs / storeCommits : 0,
    windowSeconds,
  };
}

/** Zero out (for tests; not called in production, or the "cumulative" semantics would be broken). */
export function resetObservability(): void {
  counters.wsMsgs = 0;
  counters.storeCommits = 0;
  counters.cardRenders = 0;
  lastReportAt = now();
}

/**
 * Start the structured summary print every 5s. **Idempotent**: repeated calls do not stack timers
 * (otherwise the number of summary lines would double and readers would misjudge the rate).
 *
 * Only printed to console, **never reported to any external network** (Order 16 forbidden item 3: the factory
 * intranet has no external egress). When retention is needed, ops exports from the browser console, or a later
 * internal OTLP collector is connected.
 *
 * @param intervalMs print interval, default 5000 (Order 16 §1 T2). Tests may inject a small value.
 * @returns a stop function (for unmount/tests).
 */
export function startObservabilityReport(intervalMs = 5000): () => void {
  if (reportTimer !== null) return stopObservabilityReport;

  reportTimer = setInterval(() => {
    const snap = getSnapshot();
    // Structured single line: `console.info` is easy to filter by level, the object form is easy to expand in DevTools.
    // Field names match Order 16 §1 T2 verbatim (wsMsgs / storeCommits / cardRenders / ratio).
    console.info('[observability]', {
      wsMsgs: snap.wsMsgs,
      storeCommits: snap.storeCommits,
      cardRenders: snap.cardRenders,
      ratio: Number(snap.ratio.toFixed(2)),
    });
  }, intervalMs);

  return stopObservabilityReport;
}

/** Stop the summary print. No-op when not started. */
export function stopObservabilityReport(): void {
  if (reportTimer !== null) {
    clearInterval(reportTimer);
    reportTimer = null;
  }
}
