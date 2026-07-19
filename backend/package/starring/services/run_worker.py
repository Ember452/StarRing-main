"""ARQ worker：异步执行 AgentRun 的核心循环。

本模块是 StarRing 异步对话链路的「消费者」端，由 ``arq worker`` 进程加载：
- ``process_agent_run``: 主入口，消费 chat / resume run，把 LangGraph 流式输出
  转换为事件写入 Redis Stream，并维护 run 状态机（running → completed/failed/interrupted/cancelled）
- ``execute_trigger_run``: 触发器执行入口，由 cron_scan 元任务 enqueue 触发
- ``WorkerSettings``: ARQ worker 配置（functions / max_tries / job_timeout / cron_jobs）

关键设计：
1. **协作式取消**：通过 ``RunContext`` 监听 Redis pub/sub 取消信号，在每次 chunk 间隙检查，
   不暴力 kill 任务，保证状态机干净终结
2. **ChunkedEventWriter**：攒批写 Redis Stream，避免 LLM token 流频繁 I/O
3. **可重试错误分类**：``RetryableRunError`` / ``NonRetryableRunError`` 区分瞬态与永久错误，
   ``OperationalError`` / ``ConnectionError`` 等自动归为可重试
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from starring.agents.mcp.service import ensure_builtin_mcp_servers_in_db
from starring.agents.skills.service import init_builtin_skills
from starring.repositories.agent_run_repository import TERMINAL_RUN_STATUSES, AgentRunRepository
from starring.services.chat_service import stream_agent_chat, stream_agent_resume
from starring.services.run_queue_service import (
    append_run_stream_event,
    clear_cancel_signal,
    has_cancel_signal,
    wait_for_cancel_signal,
)
from starring.services.trigger.cron_scan import scan_triggers
from starring.storage.postgres.manager import pg_manager
from starring.storage.postgres.models_business import User
from starring.utils.logging_config import logger

LOADING_FLUSH_INTERVAL_MS = 100
LOADING_FLUSH_MAX_CHARS = 512
RUN_CANCEL_POLL_SECONDS = 0.2
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


class RetryableRunError(Exception):
    """可重试错误：触发 ARQ 重新投递任务（受 ``WorkerSettings.max_tries`` 限制）。"""


class NonRetryableRunError(Exception):
    """不可重试错误：直接标记 run 失败，不进入 ARQ 重试队列。"""


@dataclass
class RunContext:
    """单个 run 的执行上下文，封装取消信号监听任务。

    ``start()`` 创建后台 watch task 订阅 Redis 取消通道；``is_cancelled()``
    被 ``process_agent_run`` 在每次 chunk 间隙调用，实现协作式取消。
    """
    run_id: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    _watch_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch_cancel_signal())

    async def close(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
            await asyncio.gather(self._watch_task, return_exceptions=True)
            self._watch_task = None

    async def wait_cancelled(self) -> None:
        await self.cancel_event.wait()

    async def is_cancelled(self) -> bool:
        if self.cancel_event.is_set():
            return True
        if await has_cancel_signal(self.run_id):
            self.cancel_event.set()
            return True
        return False

    async def _watch_cancel_signal(self) -> None:
        while not self.cancel_event.is_set():
            cancelled = await wait_for_cancel_signal(
                self.run_id,
                poll_timeout_seconds=RUN_CANCEL_POLL_SECONDS,
            )
            if cancelled:
                self.cancel_event.set()
                return


_ALL_THREADS = object()


@dataclass
class _ThreadBuffer:
    items: list[dict] = field(default_factory=list)
    chars: int = 0
    last_flush: float = field(default_factory=time.monotonic)


class ChunkedEventWriter:
    """把高频的小块数据攒起来，攒够了再一次性写入数据库/Redis，避免频繁 I/O
    因为在LLM流式对话中，模型是一字一字吐数据的，如果每收到一个token就写，性能极差
    """
    def __init__(self, run_id: str, thread_id: str | None, interval_ms: int = 100, max_chars: int = 512):
        self.run_id = run_id
        self.thread_id = thread_id
        self.interval_seconds = interval_ms / 1000
        self.max_chars = max_chars
        self.thread_buffers: dict[str | None, _ThreadBuffer] = {}

    def _target_thread_id(self, thread_id: str | None = None) -> str | None:
        return thread_id or self.thread_id

    async def append(self, chunk: dict, *, thread_id: str | None = None):
        target_thread_id = self._target_thread_id(thread_id or _thread_id_from_mapping(chunk))
        buffer = self.thread_buffers.setdefault(target_thread_id, _ThreadBuffer())
        buffer.items.append(chunk)
        buffer.chars += _loading_chunk_size(chunk)

        if _flush_loading_chunk_immediately(chunk):
            await self.flush(target_thread_id)
            return

        if (time.monotonic() - buffer.last_flush) >= self.interval_seconds or buffer.chars >= self.max_chars:
            await self.flush(target_thread_id)

    async def flush(self, thread_id: str | None | object = _ALL_THREADS):
        if thread_id is _ALL_THREADS:
            for target_thread_id in list(self.thread_buffers):
                await self.flush(target_thread_id)
            return

        buffer = self.thread_buffers.get(thread_id)
        if not buffer or not buffer.items:
            return
        await append_run_event(self.run_id, "messages", {"items": buffer.items}, thread_id=thread_id)
        buffer.items = []
        buffer.chars = 0
        buffer.last_flush = time.monotonic()


async def _get_run(run_id: str):
    async with pg_manager.get_async_session_context() as db:
        repo = AgentRunRepository(db)
        return await repo.get_run(run_id)


async def append_run_event(run_id: str, event_type: str, payload: dict, *, thread_id: str | None = None):
    await append_run_stream_event(run_id, event_type, payload, thread_id=thread_id)


async def mark_run_running(run_id: str):
    async with pg_manager.get_async_session_context() as db:
        repo = AgentRunRepository(db)
        await repo.mark_running(run_id)


async def mark_run_terminal(run_id: str, status: str, error_type: str | None = None, error_message: str | None = None):
    async with pg_manager.get_async_session_context() as db:
        repo = AgentRunRepository(db)
        await repo.set_terminal_status(run_id, status=status, error_type=error_type, error_message=error_message)
        # 触发器状态钩子：若 run 来自触发器，更新对应 Trigger 的 last_run_status
        await _update_trigger_status_if_any(db, run_id, status)


async def _update_trigger_status_if_any(db, run_id: str, status: str) -> None:
    """若 AgentRun.input_payload.trigger_id 存在，更新对应 Trigger 的 last_run_status。

    幂等保护：TriggerRepository.mark_finished_if_current 仅当 last_run_id == run_id 时才更新，
    避免旧 run 终结覆盖新 run 的状态。普通 chat run（input_payload 无 trigger_id）立即 return。
    """
    from starring.repositories.trigger_repository import TriggerRepository

    run = await AgentRunRepository(db).get_run(run_id)
    if not run:
        return
    trigger_id = (run.input_payload or {}).get("trigger_id")
    if not trigger_id:
        return
    try:
        await TriggerRepository(db).mark_finished_if_current(trigger_id, run_id, status)
    except Exception as e:
        logger.warning(f"Failed to update trigger status for run {run_id}: {e}")


async def _load_user(uid: str):
    async with pg_manager.get_async_session_context() as db:
        result = await db.execute(select(User).where(User.uid == uid, User.is_deleted == 0))
        return result.scalar_one_or_none()


async def _is_cancel_requested(run_id: str) -> bool:
    run = await _get_run(run_id)
    return bool(run and run.status == "cancel_requested")


def _job_try(ctx) -> int:
    if isinstance(ctx, dict):
        try:
            return int(ctx.get("job_try") or 1)
        except Exception:
            return 1
    return 1


def _is_last_try(ctx) -> bool:
    return _job_try(ctx) >= max(1, int(getattr(WorkerSettings, "max_tries", 1)))


def _is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, NonRetryableRunError):
        return False
    return isinstance(exc, (RetryableRunError, OperationalError, ConnectionError, TimeoutError, asyncio.TimeoutError))


def _iter_json_chunks(chunk_bytes: bytes) -> list[dict]:
    text = chunk_bytes.decode("utf-8")
    chunks: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            chunks.append(json.loads(line))
        except Exception:
            logger.warning(f"Failed to parse run stream chunk: {line[:200]}")
    return chunks


def _thread_id_from_mapping(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    thread_id = value.get("thread_id")
    if isinstance(thread_id, str) and thread_id.strip():
        return thread_id.strip()
    for key in ("meta", "metadata", "configurable", "stream_event"):
        nested = value.get(key)
        if isinstance(nested, dict):
            nested_thread_id = _thread_id_from_mapping(nested)
            if nested_thread_id:
                return nested_thread_id
    return None


def _loading_chunk_size(chunk: dict) -> int:
    response = chunk.get("response")
    total = len(response) if isinstance(response, str) else 0
    stream_event = chunk.get("stream_event")
    if not isinstance(stream_event, dict):
        return total

    for key in ("content", "reasoning_content", "additional_reasoning_content", "args_delta"):
        value = stream_event.get(key)
        if isinstance(value, str):
            total += len(value)
    return total


def _flush_loading_chunk_immediately(chunk: dict) -> bool:
    stream_event = chunk.get("stream_event")
    return isinstance(stream_event, dict) and stream_event.get("type") == "tool_call"


def _chunk_thread_id(chunk: dict, fallback: str | None) -> str | None:
    return _thread_id_from_mapping(chunk) or fallback


def _map_chunk_to_run_event(chunk: dict) -> tuple[str, dict]:
    status = chunk.get("status") or "event"
    if status == "loading":
        return "messages", {"chunk": chunk}
    if status == "agent_state":
        return "custom", {"name": "starring.agent_state", "chunk": chunk, "agent_state": chunk.get("agent_state") or {}}
    if status in {"ask_user_question_required", "human_approval_required", "interrupted"}:
        reason = "human_approval" if status == "human_approval_required" else status
        return "interrupt", {"reason": reason, "chunk": chunk}
    if status == "warning":
        return "custom", {"name": "starring.warning", "chunk": chunk}
    if status == "error":
        return "error", {"chunk": chunk, "retryable": bool(chunk.get("retryable"))}
    if status == "finished":
        return "end", {"status": "completed", "chunk": chunk}
    return "custom", {"name": f"starring.{status}", "chunk": chunk}


async def _append_end_event(run_id: str, status: str, *, thread_id: str | None, payload: dict | None = None):
    end_payload = {"status": status}
    if payload:
        end_payload.update(payload)
    await append_run_event(run_id, "end", end_payload, thread_id=thread_id)


async def _consume_stream_with_cancel(agen, run_ctx: RunContext):
    while True:
        next_task = asyncio.create_task(agen.__anext__())
        cancel_task = asyncio.create_task(run_ctx.wait_cancelled())
        done, _ = await asyncio.wait({next_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)

        if cancel_task in done:
            next_task.cancel()
            await asyncio.gather(next_task, return_exceptions=True)
            raise asyncio.CancelledError(f"run {run_ctx.run_id} cancelled")

        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        try:
            yield next_task.result()
        except StopAsyncIteration:
            return


async def process_agent_run(ctx, run_id: str):
    """ARQ 主入口：消费单个 AgentRun，把 LangGraph 流式输出转换为 Redis Stream 事件。

    流程：
    1. 加载 run + user，校验状态（已终结则跳过）
    2. 标记 running，启动 ``RunContext`` 监听取消信号
    3. 根据 ``run_type`` 选择 ``stream_agent_chat`` 或 ``stream_agent_resume``
    4. 消费 LangGraph 流：loading chunk 攒批写入，状态 chunk 同步更新 run 状态
    5. 终结时标记 completed / failed / interrupted / cancelled，写入 end 事件

    异常处理：
    - ``CancelledError``: 标记 cancelled，写入 interrupt 事件
    - 可重试异常：未达 max_tries 则抛 ``RetryableRunError`` 触发 ARQ 重投；
      达到 max_tries 则标记 failed
    - 不可重试异常：直接标记 failed
    """
    run = await _get_run(run_id)
    if not run:
        logger.warning(f"Run not found: {run_id}")
        return

    if run.status in TERMINAL_RUN_STATUSES:
        logger.info(f"Run already terminal, skip: {run_id}, status={run.status}")
        return

    payload = run.input_payload or {}
    query = payload.get("query")
    resume_input = payload.get("resume")
    run_type = payload.get("run_type") or "chat"
    config = payload.get("config") or {}
    agent_id = payload.get("agent_id")
    image_content = payload.get("image_content")
    uid = payload.get("uid")
    request_id = payload.get("request_id")
    thread_id = config.get("thread_id") or payload.get("thread_id")

    user = await _load_user(uid)
    if not user:
        await mark_run_terminal(run_id, "failed", "user_not_found", f"user {uid} not found")
        return

    if not request_id:
        request_id = run.request_id

    meta = {
        "run_id": run_id,
        "request_id": request_id,
        "query": query,
        "agent_id": agent_id,
        "server_model_name": config.get("model", agent_id),
        "thread_id": config.get("thread_id"),
        "uid": user.uid,
        "has_image": bool(image_content),
        "attachment_file_ids": payload.get("attachment_file_ids") or [],
        "use_knowledge": payload.get("use_knowledge"),
        "model_spec": payload.get("model_spec"),
    }
    if payload.get("source"):
        meta["source"] = payload.get("source")
    if isinstance(payload.get("evaluation"), dict):
        meta["evaluation"] = payload.get("evaluation") or {}
    # 标记任务为Running
    await mark_run_running(run_id)
    run_ctx = RunContext(run_id=run_id)
    # 创建ChunkedEventWriter，避免频繁写入数据库
    writer = ChunkedEventWriter(
        run_id=run_id,
        thread_id=thread_id,
        interval_ms=LOADING_FLUSH_INTERVAL_MS,
        max_chars=LOADING_FLUSH_MAX_CHARS,
    )
    await run_ctx.start()
    # 将本次数据写入
    await append_run_event(
        run_id,
        "metadata",
        {
            "request_id": request_id,
            "agent_id": agent_id,
            "backend_id": payload.get("backend_id"),
            "uid": uid,
            "source": payload.get("source"),
            "evaluation": payload.get("evaluation") or {},
        },
        thread_id=thread_id,
    )
    # 一个哨兵标志，追踪是否通通过正常的chunk标记为终结
    terminal_set = False

    try:
        async with pg_manager.get_async_session_context() as db:
            if run_type == "resume":
                stream = stream_agent_resume(
                    thread_id=thread_id,
                    resume_input=resume_input,
                    meta=meta,
                    current_user=user,
                    db=db,
                )
            else:
                # 返回一个异步生成器，产出对AI的响应
                stream = stream_agent_chat(
                    query=query,
                    agent_id=config.get("agent_id") or agent_id,
                    thread_id=thread_id,
                    meta=meta,
                    image_content=image_content,
                    current_user=user,
                    db=db,
                    save_user_message=False,
                )

            async for chunk_bytes in _consume_stream_with_cancel(stream, run_ctx):
                for chunk in _iter_json_chunks(chunk_bytes): # 把字节流解析层json
                    target_thread_id = _chunk_thread_id(chunk, thread_id)
                    if chunk.get("status") == "loading":
                        # 追加到writer缓冲区
                        await writer.append(chunk, thread_id=target_thread_id)
                        continue
                    # 刷新缓存区
                    await writer.flush(target_thread_id)
                    status = chunk.get("status") or "event"
                    # 把chunk映射为run_event写入
                    event_type, event_payload = _map_chunk_to_run_event(chunk)
                    if event_type != "end":
                        await append_run_event(run_id, event_type, event_payload, thread_id=target_thread_id)

                    if target_thread_id != thread_id:
                        if await run_ctx.is_cancelled():
                            raise asyncio.CancelledError(f"run {run_id} cancelled")
                        continue

                    if status == "finished":
                        await mark_run_terminal(run_id, "completed")
                        await _append_end_event(run_id, "completed", thread_id=thread_id, payload={"chunk": chunk})
                        terminal_set = True
                    elif status == "error":
                        await mark_run_terminal(
                            run_id,
                            "failed",
                            error_type=chunk.get("error_type") or "stream_error",
                            error_message=chunk.get("error_message") or chunk.get("message"),
                        )
                        await _append_end_event(run_id, "failed", thread_id=thread_id, payload={"chunk": chunk})
                        terminal_set = True
                    elif status == "interrupted":
                        status_value = "cancelled" if await _is_cancel_requested(run_id) else "interrupted"
                        await mark_run_terminal(
                            run_id,
                            status_value,
                            error_type=status_value,
                            error_message=chunk.get("message"),
                        )
                        await _append_end_event(run_id, status_value, thread_id=thread_id, payload={"chunk": chunk})
                        terminal_set = True
                    elif status in {"ask_user_question_required", "human_approval_required"}:
                        questions = chunk.get("questions") if isinstance(chunk, dict) else None
                        first_question = ""
                        if isinstance(questions, list) and questions:
                            first = questions[0]
                            if isinstance(first, dict):
                                first_question = str(first.get("question") or "").strip()

                        await mark_run_terminal(
                            run_id,
                            "interrupted",
                            error_type=status,
                            error_message=first_question or "需要用户回答问题",
                        )
                        await _append_end_event(run_id, "interrupted", thread_id=thread_id, payload={"chunk": chunk})
                        terminal_set = True

                    if await run_ctx.is_cancelled():
                        raise asyncio.CancelledError(f"run {run_id} cancelled")
        # 正常流程兜底，手动标记为completed，确保run不会卡在Running
        await writer.flush()
        if not terminal_set:
            finished_chunk = {"status": "finished", "request_id": request_id}
            await mark_run_terminal(run_id, "completed")
            await _append_end_event(run_id, "completed", thread_id=thread_id, payload={"chunk": finished_chunk})
    # 用户取消异常
    except asyncio.CancelledError:
        await writer.flush()
        cancel_chunk = {"status": "interrupted", "message": "对话已取消", "request_id": request_id}
        await append_run_event(
            run_id,
            "interrupt",
            {"reason": "cancelled", "chunk": cancel_chunk},
            thread_id=thread_id,
        )
        await mark_run_terminal(run_id, "cancelled", error_type="cancelled", error_message="对话已取消")
        await _append_end_event(run_id, "cancelled", thread_id=thread_id, payload={"chunk": cancel_chunk})
        logger.info(f"Run cancelled: {run_id}")
    # 通用异常，含重试
    except Exception as e:
        await writer.flush()
        if _is_retryable_exception(e):
            job_try = _job_try(ctx)
            logger.warning(f"Run retryable failure {run_id} (try={job_try}): {e}")
            retryable_error_chunk = {
                "status": "error",
                "error_type": "retryable_worker_error",
                "error_message": str(e),
                "request_id": request_id,
                "retryable": True,
                "job_try": job_try,
            }
            await append_run_event(
                run_id,
                "error",
                {"chunk": retryable_error_chunk, "retryable": True},
                thread_id=thread_id,
            )
            if _is_last_try(ctx):
                await mark_run_terminal(
                    run_id,
                    "failed",
                    error_type="retryable_worker_error",
                    error_message=str(e),
                )
                await _append_end_event(
                    run_id,
                    "failed",
                    thread_id=thread_id,
                    payload={"chunk": retryable_error_chunk},
                )
                logger.error(f"Run failed after retries exhausted {run_id}: {e}")
                return

            if isinstance(e, RetryableRunError):
                raise
            raise RetryableRunError(str(e)) from e

        logger.error(f"Run failed {run_id}: {e}")
        error_chunk = {
            "status": "error",
            "error_type": "worker_error",
            "error_message": str(e),
            "request_id": request_id,
            "retryable": False,
        }
        await append_run_event(
            run_id,
            "error",
            {"chunk": error_chunk, "retryable": False},
            thread_id=thread_id,
        )
        await mark_run_terminal(run_id, "failed", error_type="worker_error", error_message=str(e))
        await _append_end_event(run_id, "failed", thread_id=thread_id, payload={"chunk": error_chunk})
        return
    finally:
        await run_ctx.close()
        await clear_cancel_signal(run_id)


async def _worker_startup(ctx):
    del ctx
    pg_manager.initialize()
    await pg_manager.create_business_tables()
    await pg_manager.ensure_business_schema()
    await ensure_builtin_mcp_servers_in_db()
    async with pg_manager.get_async_session_context() as session:
        await init_builtin_skills(session)


async def _worker_shutdown(ctx):
    await pg_manager.close()


async def execute_trigger_run(ctx: dict, trigger_id: str, scheduled_time_iso: str) -> dict:
    """ARQ 任务入口：执行到点的触发器。

    与 process_agent_run 同级注册在 WorkerSettings.functions 中，
    由 scan_triggers 元任务 enqueue 触发。
    """
    from starring.services.trigger.service import execute_trigger

    del ctx
    return await execute_trigger(
        trigger_id=trigger_id, scheduled_time_iso=scheduled_time_iso
    )


class WorkerSettings:
    """ARQ worker 配置入口，由 ``arq worker`` 进程读取。

    - ``functions``: 注册两类任务 - ``process_agent_run``（主对话 run）与
      ``execute_trigger_run``（触发器执行）
    - ``max_tries``: 单任务最大重试次数（含首次执行）
    - ``job_timeout``: 单任务最大执行时长（秒），超时由 ARQ 强制 cancel
    - ``cron_jobs``: 注册 ``scan_triggers`` 元任务，每分钟第 0 秒执行一次
    - ``on_startup`` / ``on_shutdown``: 生命周期钩子，初始化与释放 PG 连接
    """
    functions = [process_agent_run, execute_trigger_run]
    max_tries = 2
    retry_jobs = True
    job_timeout = 3600
    keep_result = 60
    on_startup = _worker_startup
    on_shutdown = _worker_shutdown
    # cron_jobs：注册每分钟扫描 triggers 表的元任务（方案 C）
    # 用 try/except 兜底：arq.cron 在某些版本可能未导出
    cron_jobs: list = []
    try:
        from arq.cron import cron as _arq_cron

        # 每分钟第 0 秒执行 scan_triggers；minute=None 等价于 "*"（每分钟）
        cron_jobs = [_arq_cron(scan_triggers, hour=None, minute=None, second={0})]
    except Exception:
        cron_jobs = []
    try:
        from arq.connections import RedisSettings

        redis_settings = RedisSettings.from_dsn(REDIS_URL)
    except Exception:
        redis_settings = None
