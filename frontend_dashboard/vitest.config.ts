/// <reference types="vitest/config" />
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Component test config (Order 24 T2; Issue #78 switched the environment from happy-dom back to jsdom).
//
// Deliberately **independent of** vite.config.ts: vitest's test block is unrelated to the production build,
// and mixing them would make `vite build` also pay the parsing cost of test dependencies.
// The plugin matches production (@vitejs/plugin-react, automatic JSX runtime),
// ensuring we test the real components rather than a differently-transpiled copy.
//
// NOTE — why jsdom instead of happy-dom (Issue #78 empirical conclusion):
// Order 24 once chose happy-dom, on the grounds that "jsdom's AbortSignal is incompatible with undici,
// and signal-bearing fetch hangs". That rationale **no longer holds under this repo's currently locked dependency set**:
// in practice (Node 20/npm 10, after `npm ci`, `npx vitest run`) it was happy-dom that broke —
// msw 2.15.0 (the package-lock pinned value, not the ^2.7.0 floor in package.json) backfills responses via
// `@mswjs/interceptors`' FetchResponse extends globalThis.Response,
// and happy-dom's Response constructor consumes the passed body stream first, so reading the response body throws
//   TypeError: Invalid state: ReadableStream is locked
// As a result, every case that **needs to read the response body** (history line chart / empty data / 500 error state)
// failed consistently, while only the header-only, body-skipping cases passed — precisely the harder-to-find half of the 8/8 red.
// Under jsdom the response body reads normally and AbortSignal no longer collides with undici:
// after switching back to jsdom all 8/8 went green (see the timeout note below for the 500 case).
//
// This is an **environment-layer** problem: no product-code change, no msw handler contract change (handlers still use
// HttpResponse.json to output the RobotHistoryResponse fields from spec/00 §5.3).
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,                       // avoid importing { describe, it, expect } in every file
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    restoreMocks: true,
    // Tests always hit msw's port 8000, decoupled from the local `.env.local` (which may point to a mock backend on 8787).
    // Otherwise, developers running tests locally with `.env.local` would hit 8787 on endpoints like /supported-robots
    // and go red, while CI (no .env.local) stays all green — an asymmetry that misleads contributors.
    // After forcing 8000, every case that depends on a real fetch goes through the msw handler (the contract is the assertion).
    env: {
      VITE_API_BASE: 'http://localhost:8000',
      VITE_WS_HOST: 'localhost',
      VITE_WS_PORT: '8000',
    },
  },
});
