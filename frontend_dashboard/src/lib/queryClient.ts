// 订单 26（I-20）：服务端状态层 —— TanStack Query。
//
// 职责边界（external/INTEGRATION-PLAN.md §8.10 明确）：
//   - Query 管**服务端**状态：历史查询的缓存、去重、重试、失效。
//   - Zustand 管**客户端**状态：WS 100ms 节流后的实时遥测与告警（robotStore.ts 不变）。
// 两者互补，**不互相替换** —— 前轮「zustand 不升 v5」决策不变。

import { QueryClient } from '@tanstack/react-query';

/**
 * 全局 QueryClient。
 *
 * 默认值取舍：
 *   - retry: 2 —— 历史查询是可重试的幂等 GET；但对 4xx（含 401/404）不重试，
 *     401 由调用点走 notifyUnauthorized() 回登录页，重试只会重复弹登录。
 *   - staleTime: 10s —— 与历史点的 10s 分辨率对齐：桶内重复打开不重复打后端。
 *   - gcTime: 5min —— 关闭 Drawer 后缓存保留 5 分钟，来回切换机器人不必冷启动。
 *   - refetchOnWindowFocus: false —— 大屏常驻前台，窗口焦点抖动不该触发重拉。
 */
export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          // HTTP 4xx 是确定性失败（认证/路由/参数），重试无意义。
          const status = (error as { status?: number })?.status;
          if (typeof status === 'number' && status >= 400 && status < 500) return false;
          return failureCount < 2;
        },
        retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 8000),
      },
    },
  });
}
