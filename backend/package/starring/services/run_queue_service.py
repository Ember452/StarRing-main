"""AgentRun 异步执行队列的 Redis 基础设施。

本模块是 StarRing 异步对话链路的底层支撑，封装两类 Redis 能力：

1. **取消信号**：通过 ``run:cancel:{run_id}`` 键 + ``run:cancel:ch`` pub/sub 通道
   实现跨进程取消。HTTP 层调用 ``publish_cancel_signal``，worker 端通过
   ``wait_for_cancel_signal`` 监听并协作式取消正在执行的 run。

2. **事件流**：通过 Redis Stream ``run:events:{run_id}`` 持久化 run 执行期间
   产生的所有事件（metadata / messages / interrupt / error / end），
   前端通过 ``list_run_stream_events`` 按 ``after_seq`` 游标增量拉取，
   实现 SSE 流式响应 + 断线重连。

依赖 arq + redis.asyncio；连接句柄全局缓存（``_redis_client`` / ``_arq_pool``），
通过 ``close_queue_clients`` 在应用 shutdown 时统一释放。
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from starring.utils.logging_config import logger

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
RUN_CANCEL_KEY_TTL_SECONDS = int(os.getenv("RUN_CANCEL_KEY_TTL_SECONDS", "1800"))
RUN_EVENTS_STREAM_TTL_SECONDS = int(os.getenv("RUN_EVENTS_STREAM_TTL_SECONDS", "7200"))
RUN_EVENTS_STREAM_MAXLEN = int(os.getenv("RUN_EVENTS_STREAM_MAXLEN", "0"))
RUN_CANCEL_CHANNEL = os.getenv("RUN_CANCEL_CHANNEL", "run:cancel:ch")
# 子智能体 run 专用 ARQ 队列：与主队列隔离，避免父 run 等待子 run 时耗尽 worker 池死锁
SUBAGENT_QUEUE_NAME = os.getenv("SUBAGENT_QUEUE_NAME", "arq:queue:subagent")

_redis_client = None
_arq_pool = None


def _redacted_redis_url(url: str) -> str:
    if "@" in url:
        return url.split("@", 1)[1]
    return url


def _cancel_key(run_id: str) -> str:
    return f"run:cancel:{run_id}"


def _event_stream_key(run_id: str) -> str:
    return f"run:events:{run_id}"


def _is_valid_stream_seq(value: str) -> bool:
    major, sep, minor = value.partition("-")
    if sep != "-":
        return False
    return major.isdigit() and minor.isdigit()


def normalize_after_seq(after_seq: str | None) -> str:
    """Normalize after_seq cursor to redis stream id format."""
    if after_seq is None:
        return "0-0"

    text = str(after_seq).strip()
    if not text:
        return "0-0"

    if _is_valid_stream_seq(text):
        return text
    return "0-0"


def build_run_event_envelope(
    *,
    run_id: str,
    event_type: str,
    payload: dict | None = None,
    thread_id: str | None = None,
    created_at: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "thread_id": thread_id,
        "event": event_type,
        "payload": payload or {},
        "created_at": created_at or datetime.now(tz=UTC).isoformat(),
    }


def _payload_thread_id(payload: dict | None) -> str | None:
    chunk = payload.get("chunk") if isinstance(payload, dict) else None
    if not isinstance(chunk, dict):
        return None
    thread_id = chunk.get("thread_id")
    return thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None


async def get_redis_client():
    """获取全局缓存的 redis.asyncio 客户端（首次调用时建立连接并 ping 校验）。

    连接失败时关闭句柄并抛 RuntimeError，由调用方决定是否重试。
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    try:
        from redis.asyncio import Redis
    except Exception as e:
        raise RuntimeError("redis dependency is required for run queue") from e

    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.ping()
    except Exception as e:
        try:
            await redis.aclose()
        except Exception:
            pass
        raise RuntimeError(f"Redis connection failed ({_redacted_redis_url(REDIS_URL)}): {e}") from e

    _redis_client = redis
    return _redis_client


async def get_arq_pool():
    """获取全局缓存的 ARQ 连接池（用于 enqueue_job 投递异步任务）。

    与 ``get_redis_client`` 分离：ARQ 用自己的 ``RedisSettings`` 创建池，
    内部走 hiredis 协议，与 ``redis.asyncio`` 客户端不共用连接。
    """
    global _arq_pool
    if _arq_pool is not None:
        return _arq_pool

    try:
        from arq.connections import RedisSettings, create_pool
    except Exception as e:
        raise RuntimeError("arq dependency is required for run queue") from e

    settings = RedisSettings.from_dsn(REDIS_URL)
    _arq_pool = await create_pool(settings)
    return _arq_pool


@asynccontextmanager
async def redis_pubsub(channel: str):
    redis = await get_redis_client()
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        yield pubsub
    finally:
        try:
            await pubsub.unsubscribe(channel)
        finally:
            await pubsub.close()


async def publish_cancel_signal(run_id: str) -> None:
    """发布取消信号：写入带 TTL 的 key + publish 到取消通道。

    双通道设计：worker 端 ``wait_for_cancel_signal`` 同时监听 pub/sub 与 key，
    任意一条通道失效（如 worker 重连）都能感知到取消，避免信号丢失。
    TTL 默认 30 分钟，超过窗口的取消信号自动失效，防止僵尸 run 被反复唤醒。
    """
    redis = await get_redis_client()
    key = _cancel_key(run_id)
    try:
        await redis.set(key, "1", ex=RUN_CANCEL_KEY_TTL_SECONDS)
        await redis.publish(RUN_CANCEL_CHANNEL, run_id)
    except Exception as e:
        logger.warning(f"Failed to publish cancel signal for run {run_id}: {e}")


async def has_cancel_signal(run_id: str) -> bool:
    """检查 run 是否已被取消（读 ``run:cancel:{run_id}`` key）。"""
    redis = await get_redis_client()
    key = _cancel_key(run_id)
    try:
        return bool(await redis.get(key))
    except Exception as e:
        logger.warning(f"Failed to read cancel signal for run {run_id}: {e}")
        return False


async def wait_for_cancel_signal(run_id: str, poll_timeout_seconds: float = 1.0) -> bool:
    """阻塞等待 run 的取消信号，超时返回 False。
    Redis Pub/Sub：处理实时通知
    优先检查 key（处理 worker 重连后的延迟信号），随后订阅 pub/sub 通道。
    ``poll_timeout_seconds`` 控制单次 pub/sub 轮询超时，外层调用方可定期 re-check。
    """
    # 首先通过检查key是否还存在，判断是否已经取消
    if await has_cancel_signal(run_id):
        return True

    try:
        # 从Redis的pubsub中等待实时消息，判断是否是ren_id
        async with redis_pubsub(RUN_CANCEL_CHANNEL) as pubsub:
            while True:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=poll_timeout_seconds,
                )
                if msg and str(msg.get("data")) == run_id:
                    return True
                if await has_cancel_signal(run_id):
                    return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"Failed to wait cancel signal for run {run_id}: {e}")
        return False


async def clear_cancel_signal(run_id: str) -> None:
    """run 终结后清理取消信号 key，避免下次同名 run 误判为已取消。"""
    redis = await get_redis_client()
    key = _cancel_key(run_id)
    try:
        await redis.delete(key)
    except Exception as e:
        logger.warning(f"Failed to clear cancel signal for run {run_id}: {e}")


async def append_run_stream_event(run_id: str, event_type: str, payload: dict, *, thread_id: str | None = None) -> str:
    """把单个事件写入 Redis Stream ``run:events:{run_id}``，返回 stream entry id。

    envelope 包含 schema_version / run_id / thread_id / event / payload / created_at，
    保证前端按统一 schema 解析；stream TTL 默认 2 小时，超长 stream 由
    ``RUN_EVENTS_STREAM_MAXLEN`` 控制容量（0 表示不裁剪）。
    """
    redis = await get_redis_client()  # await把当前协程挂起，并等待他完成结果
    key = _event_stream_key(run_id)
    now = datetime.now(tz=UTC)
    now_ms = int(now.timestamp() * 1000)
    # 事件归属的 thread_id：显式传入优先，否则从 payload 推断（兼容旧调用方）
    event_thread_id = thread_id or _payload_thread_id(payload)
    # 构造统一 envelope：保证前端按 schema_version 解析，跨版本兼容
    envelope = build_run_event_envelope(
        run_id=run_id,
        event_type=event_type,
        payload=payload or {},
        thread_id=event_thread_id,
        created_at=now.isoformat(),
    )
    fields = {
        "event_type": event_type,
        "payload": json.dumps(envelope, ensure_ascii=False),
        "ts": str(now_ms),
    }

    # 容量控制：maxlen=0 表示不裁剪；approximate 模式下 Redis 延迟裁剪，吞吐更高
    kwargs = {}
    if RUN_EVENTS_STREAM_MAXLEN > 0:
        kwargs["maxlen"] = RUN_EVENTS_STREAM_MAXLEN  # 设置最大有效长度
        kwargs["approximate"] = True  # 开启近似模式，Redis不会立即裁剪，允许Stream超过最大长度，性能好

    event_id = await redis.xadd(key, fields, **kwargs)
    # 每次写入后刷新 TTL，保证活跃 run 的 stream 不会被过早回收
    await redis.expire(key, RUN_EVENTS_STREAM_TTL_SECONDS)
    return str(event_id)


async def list_run_stream_events(
    run_id: str,
    *,
    after_seq: str = "0-0",
    limit: int = 200,
) -> list[dict]:
    """从 Redis Stream 增量拉取事件，供前端 SSE 断线重连。

    ``after_seq`` 是上一次拉取返回的最大 entry id；首次传 "0-0" 拉取全量。
    使用 ``xrange(min="({after_seq}")`` 开区间查询，避免重复返回边界事件。
    """
    redis = await get_redis_client()
    key = _event_stream_key(run_id)
    start = "-" if after_seq in {"0-0", ""} else f"({after_seq}"
    rows = await redis.xrange(key, min=start, max="+", count=limit)
    events = []

    for event_id, fields in rows:
        payload_raw = fields.get("payload") or "{}"
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}

        event_type = fields.get("event_type") or "message"
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            payload = {
                "schema_version": 1,
                "run_id": run_id,
                "thread_id": None,
                "event": event_type,
                "payload": payload if isinstance(payload, dict) else {},
                "created_at": None,
            }

        ts_value = fields.get("ts")
        events.append(
            {
                "seq": str(event_id),
                "event_type": event_type,
                "payload": payload,
                "ts": int(ts_value) if ts_value else None,
            }
        )
    return events


async def get_last_run_stream_seq(run_id: str) -> str:
    """获取 run 事件流的最新 entry id（用于前端建立 SSE 连接时的初始游标）。"""
    redis = await get_redis_client()
    key = _event_stream_key(run_id)
    rows = await redis.xrevrange(key, max="+", min="-", count=1)
    if not rows:
        return "0-0"
    event_id, _ = rows[0]
    return str(event_id)


async def close_queue_clients() -> None:
    """应用 shutdown 时释放 Redis / ARQ 全局连接句柄。"""
    global _redis_client, _arq_pool
    if _arq_pool is not None:
        try:
            await _arq_pool.close()
        except Exception:
            pass
        _arq_pool = None
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:
            pass
        _redis_client = None
