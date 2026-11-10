// msw Node-side server (intercepts fetch inside the jsdom test process).
//
// Note: msw 2.x's package is `msw/node` (no longer the `msw/node` setupServer alias);
// setupServer is exported from 'msw/node'.
import { setupServer } from 'msw/node';
import { handlers } from './handlers';

export const server = setupServer(...handlers);
