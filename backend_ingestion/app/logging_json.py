"""结构化 JSON 日志（订单 16 T4）。

为什么不是继续用 `logging.basicConfig(format="%(asctime)s %(levelname)s ...")`：
订单 07 的采集栈（openobserve / loki）按行读，只有**单行 JSON** 才能被直接
`jq` / 日志管道解析出字段。当前的空格分隔文本在 message 里含空格时（如
"batch flush gave up, 500 rows dropped"）无法可靠切分，采集侧只能全文正则，
字段名一旦改动就静默失配。

契约（订单 16 §1 T4）：每行一个 JSON 对象，至少含
    ts        ISO8601 UTC 时间戳
    level     日志级别（INFO / WARNING / ...）
    logger    日志器名
    msg       已格式化的人类可读消息
    robot_id  可选：仅当 LogRecord 带 robot_id 属性时出现

实现要点：
- 继承 `logging.Formatter`，`format()` 返回**不含换行**的单行字符串；
  logging 自己会补换行，多写一个就会产生空行（空行会让某些采集器误判为日志结束）。
- `default=str` 兜底：LogRecord 里可能塞进非 JSON 原生类型（异常对象、datetime），
  不加这一项 json.dumps 会抛 TypeError，而 logging 内部**吞掉** formatter 异常
  （打印到 stderr 而不是让进程崩），结果是"日志整条消失"且毫无迹象。
- 异常栈进 `exc` 字段（而非塞进 msg）：保留结构化，同时不破坏单行。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

# 不放进 JSON 的 LogRecord 内建字段 —— 它们是 logging 的实现细节，
# 全部导出会让每行多出 15+ 个无用键（采集侧存储成本直接翻倍）。
_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            # `record.created` 是 epoch 秒（float），转 UTC ISO8601 便于跨时区排障。
            # 不用 datetime.now()：那是**格式化时刻**，与事件发生时刻在并发下会错位。
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            # getMessage() 会合并 %s 参数；用 record.msg 会丢掉参数，留下 "batch flush %d"。
            "msg": record.getMessage(),
        }

        # 调用方通过 `log.info("...", extra={"robot_id": rid})` 挂的自定义字段。
        # 只放非内建的，且值本身可 JSON 化（交由 default=str 兜底）。
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value

        if record.exc_info:
            # formatException 返回**多行**字符串。json.dumps 默认会把它转义成 \n，
            # 单行性没问题；但若本模块被非 json.dumps 直接拼接（或未来有人换
            # 序列化器）就会退化成多行。这里显式把换行归一，让"单行"这个
            # 契约不依赖下游序列化器的具体实现（订单 16 §2「单行 JSON」）。
            payload["exc"] = self.formatException(record.exc_info).replace("\n", " | ")

        # ensure_ascii=False：中文 message 保持可读（采集侧无需再解码）。
        # default=str：兜底非原生类型，避免整条日志因序列化失败而消失。
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_json_logging(level: int = logging.INFO) -> None:
    """把根 logger 换成单行 JSON 输出。

    幂等：重复调用只替换 handler，不会叠加（叠加会导致同一行打印 N 次）。
    注意必须同时清掉已有 handler —— uvicorn 默认会装自己的 handler，
    不清掉的话容器日志里会**混着** JSON 行与 uvicorn 的文本行，
    采集侧解析一半失败。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
