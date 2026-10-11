// msw Node 侧服务器（在 jsdom 环境的测试进程里拦截 fetch）。
//
// 注意 msw 2.x 的包名是 `msw/node`（不再是 `msw/node` 的 setupServer 别名），
// setupServer 从 'msw/node' 导出。
import { setupServer } from 'msw/node';
import { handlers } from './handlers';

export const server = setupServer(...handlers);
