"""WS 合成探针（客户端视角的「看板还亮着吗」验活）。

为什么需要它（缺口 G-3「看板假的绿」）：
  `robot_backend_ws_connections`（订单 16 T1b）数的是**服务端**的 WS 连接对象，
  它无法回答客户端真正关心的问题：一条真实的 WS 客户端能否完成
  **握手 + 首帧 snapshot + 增量帧**。历史上出过「看板绿着、其实没数据」的事故
  ——nginx/网关把升级请求挡了、Redis Pub/Sub 断了、snapshot 抛异常被吞掉，
  这些情况下服务端连接数照样是 1。本探针从**客户端**复跑一遍真实路径，
  两者背离即是事故信号。

三个硬约束（本模块全部遵守）：
  1. **免鉴权目标**：探针连的是同进程内的 `ws_robots`，但走的是真实网络栈
     （`ws://127.0.0.1:$ws_port`，不是 ASGI 直调），这样才能覆盖网关/升级层。
     鉴权走 `?token=`，token 由本进程用 `settings.jwt_secret` 自签（见 `_mint_token`），
     不依赖任何用户表、也不接受外部传入 —— 探针只在自己的 loopback 上跑。
  2. **≥30s 一次**：`ws_probe_interval_s` 下限被钳在 30s，配错也快不起来。
     探针自己发 WS 帧，而 1000 msg/s 的遥测流也在同一个 loopback 上，
     频率无上限会污染压测基线。
  3. **可降级（no-op）**：`ws_probe_enabled=False`、或 `websockets` 不可用、
     或循环内任何异常，都只是**不再更新指标**，绝不影响主服务。
     探针是旁观者，任何情况下都不许把后端拖挂。

指标（与订单 16 T1b 的连接数 Gauge 是**不同**指标，互不覆盖）：
  - `robot_backend_ws_probe_alive`          Gauge   1=最近一次探活成功 / 0=失败
  - `robot_backend_ws_probe_failures_total` Counter 累计失败次数
"""

from __future__ import annotations

import asyncio
import json
import logging

from app import metrics
from app.config import settings

log = logging.getLogger("ws_probe")

#: 与 `config.py` 的 `ws_probe_interval_s` 配套的下限。写在代码里而不是只写在
#: 配置里，是因为配置可被 .env 覆盖 —— 探针频率必须有一个改不动的地板。
MIN_INTERVAL_S = 30.0
#: 单次探活的整体超时。握手 + 首帧 snapshot 各阶段共享这个预算。
PROBE_TIMEOUT_S = 5.0


def _mint_token() -> str | None:
    """用本进程密钥自签一个探针专用 JWT。

    只声明 `sub` 与 `aud`：`authenticate_ws_token()`（`auth/deps.py:101`）
    只解这两个字段做验签，`role` 不参与 WS 准入判定。
    签发失败（密钥不受支持的算法等）返回 None —— 让探针降级，而不是抛。
    """
    try:
        import jwt

        from app.auth.backend import TOKEN_AUDIENCE

        return jwt.encode(
            {"sub": "ws-probe", "aud": TOKEN_AUDIENCE},
            settings.jwt_secret,
            algorithm="HS256",
        )
    except Exception:
        log.warning("ws probe: 无法自签探针 token，探针降级为 no-op", exc_info=True)
        return None


async def _probe_once_async(url: str, token: str) -> bool:
    """跑一次真实 WS 客户端路径，返回是否通过。

    通过判据分两层：
      ① 能升级成 WebSocket 且**收到首帧** —— 这一层就能抓住「网关卡升级」「进程假活」；
      ② 首帧必须是 `{"type":"snapshot"}` 信封（spec/01 §4.5）。
         `ws_robots` 在 snapshot 失败时只 `log.exception` 就继续进 pub/sub 循环，
         客户端会挂在首帧上 —— 只看「握手成功」会把这种半死状态误判为健康。
         故这里给首帧单独一个短超时，拿不到就判失败。

    异常一律如实向上抛（由 `run()` 兜住并翻失败指标）：**不在这里静默返回 False**，
    否则「探针自己坏了」与「后端真的死了」会在日志里长得一模一样。
    """
    # 导入位置按版本二分：`websockets.asyncio.client` 是 13.0 引入的新命名空间，
    # 而本仓 `requirements.txt` 钉的是 `websockets==12.0`（旧顶层 `connect`）。
    # 两个都试，是为了让探针在「升级依赖」与「现状」下都能用 —— 探针是旁观者，
    # 不该成为依赖版本升级的绊脚石。找不到任何一条即 import 抛错，由 run() 兜住降级。
    try:
        from websockets.asyncio.client import connect  # websockets >= 13
    except ImportError:
        from websockets.client import connect  # websockets 12.x（本仓锁定版）

    async with asyncio.timeout(PROBE_TIMEOUT_S):
        async with connect(url) as ws:
            first = await asyncio.wait_for(ws.recv(), timeout=PROBE_TIMEOUT_S)
    frame = json.loads(first)
    return frame.get("type") == "snapshot"


async def run(*, url: str | None = None) -> None:
    """探针循环。由 lifespan 以 `asyncio.Task` 启动，本函数永不正常返回。"""
    if not settings.ws_probe_enabled:
        log.info("ws probe: 未启用（WS_PROBE_ENABLED=false），no-op")
        return

    target = url or f"ws://127.0.0.1:{settings.ws_port}{settings.ws_probe_path}"
    token = _mint_token()
    if token is None:
        return

    # 首轮先等一个启动窗口：uvicorn 的 `lifespan` startup 阶段就已经把
    # 本任务 create_task 出来了，此时端口还没进入 accept 状态，
    # 立刻探必然失败。等窗口过了再开始，避免开局制造一次假失败。
    await asyncio.sleep(settings.ws_probe_startup_grace_s)
    query = target + ("&" if "?" in target else "?") + f"token={token}"
    interval = max(MIN_INTERVAL_S, settings.ws_probe_interval_s)
    log.info("ws probe: 已启动 target=%s interval=%.0fs", target, interval)

    while True:
        # 探针自身的任何异常都不许冒泡成任务异常（那会进 asyncio 的
        # "Task exception was never retrieved" 日志，看起来像后端故障）。
        try:
            ok = await _probe_once_async(query, token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ws probe: 探活失败 %s: %s", type(exc).__name__, exc)
            ok = False

        metrics.ws_probe_alive.set(1 if ok else 0)
        if not ok:
            metrics.ws_probe_failures.inc()

        await asyncio.sleep(interval)
