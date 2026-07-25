"""子智能体 run 的 ARQ worker（独立队列消费端）。

本模块把子智能体的执行从父 worker 进程内的 ``graph.ainvoke`` 迁移到独立 ARQ 任务：
- ``process_subagent_run``: 加载子 AgentRun → 重建 child_context（``output_format="structured"``）
  → ``graph.astream`` 流式执行 → chunk 事件写入 **父 run 的事件流**（``run:events:{parent_run_id}``，
  envelope 携带 ``thread_id=child_thread_id``，复用 run_worker 的子线程路由约定）
  → 终结时把 ``SubAgentDeliverable`` 写入 ``agent_runs.output_payload`` 供父侧读取
- ``SubAgentWorkerSettings``: 消费 ``SUBAGENT_QUEUE_NAME`` 独立队列，与主队列隔离，
  避免父 run 占用 slot 等待子 run 时耗尽 worker 池死锁

取消：复用 ``RunContext`` 协作式取消（父侧级联 ``publish_cancel_signal`` 或用户直接取消子 run），
任务启动时先检查排队期取消信号。Langfuse 上下文无法跨进程传递，
在任务内基于子 run 元数据重建，parent_run_id 通过 metadata 关联父 trace。
"""

from __future__ import annotations

import asyncio
import os

from langchain_core.messages import HumanMessage
from starring.agents.context import build_agent_input_context
from starring.agents.middlewares.subagent_task import _extract_deliverable, _get_agent_backend
from starring.repositories.agent_repository import SUB_AGENT_BACKEND_ID, AgentRepository
from starring.repositories.agent_run_repository import TERMINAL_RUN_STATUSES, AgentRunRepository
from starring.services.chat_service import _message_payload_STARRING_events, _metadata_namespace
from starring.services.langfuse_service import build_run_context
from starring.services.run_queue_service import SUBAGENT_QUEUE_NAME, clear_cancel_signal, has_cancel_signal
from starring.services.run_worker import (
    LOADING_FLUSH_INTERVAL_MS,
    LOADING_FLUSH_MAX_CHARS,
    REDIS_URL,
    ChunkedEventWriter,
    RetryableRunError,
    RunContext,
    _consume_stream_with_cancel,
    _is_retryable_exception,
    _job_try,
    _load_user,
    _worker_shutdown,
    _worker_startup,
    append_run_event,
    mark_run_running,
)
from starring.storage.postgres.manager import pg_manager
from starring.utils.logging_config import logger

SUBAGENT_RUN_STATUS_EVENT = "starring.subagent_run_status"


def _is_last_try(ctx) -> bool:
    return _job_try(ctx) >= max(1, int(getattr(SubAgentWorkerSettings, "max_tries", 1)))


async def _mark_subagent_terminal(
    run_id: str,
    status: str,
    *,
    error_type: str | None = None,
    error_message: str | None = None,
    output_payload: dict | None = None,
) -> None:
    """子 run 终态落库（幂等）。不走 ``mark_run_terminal``：子 run 无触发器/记忆钩子。"""
    async with pg_manager.get_async_session_context() as db:
        await AgentRunRepository(db).set_terminal_status(
            run_id,
            status=status,
            error_type=error_type,
            error_message=error_message,
            output_payload=output_payload,
        )


async def _emit_subagent_status(
    parent_run_id: str,
    *,
    run_id: str,
    child_thread_id: str,
    tool_call_id: str | None,
    status: str,
    error_message: str | None = None,
) -> None:
    """把子 run 状态变更作为 custom 事件写入父 run 事件流（前端子线程 modal 消费）。"""
    chunk = {
        "status": "subagent_run_status",
        "run_id": run_id,
        "subagent_run_status": status,
        "thread_id": child_thread_id,
        "subagent_tool_call_id": tool_call_id,
    }
    if error_message:
        chunk["error_message"] = error_message
    try:
        await append_run_event(
            parent_run_id,
            "custom",
            {"name": SUBAGENT_RUN_STATUS_EVENT, "chunk": chunk},
            thread_id=child_thread_id,
        )
    except Exception as e:
        logger.warning(f"Failed to emit subagent status event for run {run_id}: {e}")


async def _load_subagent_run(run_id: str):
    async with pg_manager.get_async_session_context() as db:
        return await AgentRunRepository(db).get_run(run_id)


async def _load_subagent(slug: str):
    async with pg_manager.get_async_session_context() as db:
        return await AgentRepository(db).get_by_slug(slug)


async def _build_child_context(backend, agent, *, payload: dict, run) -> object:
    """重建子智能体执行上下文（与父进程 atask 原逻辑等价，输入全部来自 run 快照）。"""
    child_thread_id = run.thread_id
    parent_thread_id = str(payload.get("parent_thread_id") or child_thread_id)
    child_context = backend.context_schema()
    config_context = (agent.config_json or {}).get("context") if isinstance(agent.config_json, dict) else None
    child_input_context = await build_agent_input_context(
        config_context if isinstance(config_context, dict) else {},
        thread_id=child_thread_id,
        uid=run.uid,
        run_id=run.id,
        request_id=run.request_id,
    )
    # 子 agent 未配置模型时回退父智能体模型（创建子 run 时快照进 input_payload）
    if not str(child_input_context.get("model") or "").strip():
        parent_model = str(payload.get("parent_model") or "").strip()
        if parent_model:
            child_input_context["model"] = parent_model
    child_context.update_from_dict(child_input_context)
    child_context.uid = run.uid
    child_context.thread_id = child_thread_id
    child_context.parent_thread_id = parent_thread_id
    child_context.file_thread_id = str(payload.get("file_thread_id") or parent_thread_id)
    child_context.skills_thread_id = child_thread_id
    child_context.run_id = run.id
    child_context.request_id = run.request_id
    child_context.is_subagent_runtime = True
    child_context.output_format = "structured"
    return child_context


def _child_run_config(run, payload: dict, langfuse_run) -> dict:
    """构建子 graph 的 RunnableConfig。

    Langfuse callbacks 不可跨进程传递，此处基于子 run 元数据重建 handler，
    metadata 中携带 parent_run_id / parent_thread_id 实现与父 trace 的链路关联。
    """
    child_thread_id = run.thread_id
    parent_thread_id = str(payload.get("parent_thread_id") or child_thread_id)
    config: dict = {
        "configurable": {
            "thread_id": child_thread_id,
            "uid": run.uid,
            "parent_thread_id": parent_thread_id,
            "file_thread_id": str(payload.get("file_thread_id") or parent_thread_id),
            "skills_thread_id": child_thread_id,
            "subagent_type": run.agent_id,
            "subagent_thread_id": child_thread_id,
            "subagent_tool_call_id": payload.get("tool_call_id"),
            "run_id": run.id,
            "request_id": run.request_id,
            "ls_agent_type": "subagent",
        },
        "recursion_limit": 300,
    }
    if langfuse_run is not None:
        config["callbacks"] = langfuse_run.callbacks
        config["metadata"] = langfuse_run.metadata
        config["tags"] = langfuse_run.tags
    return config


def _child_state(description: str, run, payload: dict) -> dict:
    """重建子 graph 输入 state（对应原 ``_state_for_child``；继承键集合为空，无需父 state）。"""
    child_thread_id = run.thread_id
    parent_thread_id = str(payload.get("parent_thread_id") or child_thread_id)
    return {
        "parent_thread_id": parent_thread_id,
        "file_thread_id": str(payload.get("file_thread_id") or parent_thread_id),
        "skills_thread_id": child_thread_id,
        "messages": [HumanMessage(content=description)],
    }


def _loading_chunk(stream_event: dict, *, request_id: str, child_thread_id: str, tool_call_id: str | None) -> dict:
    """把 StarRing 协议事件包装为与 chat_service ``make_chunk`` 同构的 loading chunk。"""
    content = stream_event.get("content") if stream_event.get("type") == "message_delta" else None
    return {
        "request_id": request_id,
        "response": content if isinstance(content, str) else None,
        "thread_id": child_thread_id,
        "status": "loading",
        "stream_event": stream_event,
        "subagent_tool_call_id": tool_call_id,
    }


async def process_subagent_run(ctx, run_id: str):
    """ARQ 任务入口：消费单个子智能体 AgentRun。

    流程：
    1. 加载子 run，终态/取消信号则直接跳过或标记 cancelled
    2. 重建 child_context / config / state，标记 running
    3. ``graph.astream(stream_mode=["messages", "values"])`` 流式执行：
       messages 模式转换为 loading chunk 攒批写入父 run 事件流，values 模式保留最终 state
    4. 终结：completed 时提取 deliverable 写入 output_payload；
       CancelledError → cancelled；可重试异常未达 max_tries 则 ARQ 重投
    """
    run = await _load_subagent_run(run_id)
    if not run:
        logger.warning(f"Subagent run not found: {run_id}")
        return
    if run.status in TERMINAL_RUN_STATUSES:
        logger.info(f"Subagent run already terminal, skip: {run_id}, status={run.status}")
        return

    payload = run.input_payload or {}
    parent_run_id = run.parent_agent_run_id
    child_thread_id = run.thread_id
    tool_call_id = payload.get("tool_call_id")
    description = str(payload.get("description") or "")
    if not parent_run_id:
        await _mark_subagent_terminal(run_id, "failed", error_type="invalid_run", error_message="缺少父运行 ID")
        return

    # 排队期取消：任务尚未启动就被取消（父 run 取消级联或用户直接取消）
    if await has_cancel_signal(run_id):
        await _mark_subagent_terminal(run_id, "cancelled", error_type="cancelled", error_message="子任务已取消")
        await _emit_subagent_status(
            parent_run_id,
            run_id=run_id,
            child_thread_id=child_thread_id,
            tool_call_id=tool_call_id,
            status="cancelled",
        )
        await clear_cancel_signal(run_id)
        return

    user = await _load_user(run.uid)
    if not user:
        await _mark_subagent_terminal(
            run_id, "failed", error_type="user_not_found", error_message=f"user {run.uid} not found"
        )
        return

    agent = await _load_subagent(run.agent_id)
    if not agent or agent.backend_id != SUB_AGENT_BACKEND_ID:
        await _mark_subagent_terminal(
            run_id, "failed", error_type="invalid_subagent", error_message=f"子智能体 {run.agent_id} 配置无效"
        )
        return
    backend = _get_agent_backend(agent.backend_id)
    if not backend:
        await _mark_subagent_terminal(
            run_id, "failed", error_type="invalid_subagent", error_message=f"子智能体后端 {agent.backend_id} 不存在"
        )
        return

    # Langfuse 上下文重建：parent_run_id 写入 metadata 实现父子 trace 关联
    langfuse_run = None
    try:
        langfuse_run = build_run_context(
            user_id=str(user.uid),
            thread_id=child_thread_id,
            agent_id=run.agent_id,
            request_id=run.request_id,
            operation="subagent_run",
            backend_id=agent.backend_id,
            username=getattr(user, "username", None),
            login_user_id=getattr(user, "uid", None),
            department_id=getattr(user, "department_id", None),
            extra_metadata={
                "parent_run_id": parent_run_id,
                "parent_thread_id": str(payload.get("parent_thread_id") or ""),
                "subagent_tool_call_id": tool_call_id,
            },
            extra_tags=["subagent"],
        )
    except Exception as e:
        logger.warning(f"Failed to build langfuse context for subagent run {run_id}: {e}")

    await mark_run_running(run_id)
    run_ctx = RunContext(run_id=run_id)
    writer = ChunkedEventWriter(
        run_id=parent_run_id,
        thread_id=child_thread_id,
        interval_ms=LOADING_FLUSH_INTERVAL_MS,
        max_chars=LOADING_FLUSH_MAX_CHARS,
    )
    await run_ctx.start()
    await _emit_subagent_status(
        parent_run_id,
        run_id=run_id,
        child_thread_id=child_thread_id,
        tool_call_id=tool_call_id,
        status="running",
    )

    result_values: dict = {}
    protocol_message_ids: dict = {}
    try:
        child_context = await _build_child_context(backend, agent, payload=payload, run=run)
        graph = await backend.get_graph(context=child_context)
        stream = graph.astream(
            _child_state(description, run, payload),
            config=_child_run_config(run, payload, langfuse_run),
            context=child_context,
            stream_mode=["messages", "values"],
        )
        async for mode, item in _consume_stream_with_cancel(stream, run_ctx):
            if mode == "values":
                # 保留最终 state 快照，终结时从中提取 structured_response / artifacts
                if isinstance(item, dict):
                    result_values = item
                continue
            msg, metadata = item if isinstance(item, tuple) else (item, {})
            metadata = metadata if isinstance(metadata, dict) else {}
            events = _message_payload_STARRING_events(
                msg,
                metadata=metadata,
                namespace=_metadata_namespace(metadata),
                thread_id=child_thread_id,
                protocol_message_ids=protocol_message_ids,
            )
            for stream_event in events:
                await writer.append(
                    _loading_chunk(
                        stream_event,
                        request_id=run.request_id,
                        child_thread_id=child_thread_id,
                        tool_call_id=tool_call_id,
                    ),
                    thread_id=child_thread_id,
                )

        await writer.flush()
        deliverable = _extract_deliverable(result_values)
        await _mark_subagent_terminal(
            run_id,
            "completed",
            output_payload={"deliverable": deliverable.model_dump(mode="json")},
        )
        await _emit_subagent_status(
            parent_run_id,
            run_id=run_id,
            child_thread_id=child_thread_id,
            tool_call_id=tool_call_id,
            status="completed",
        )
        logger.info(f"Subagent run completed: {run_id}")
    except asyncio.CancelledError:
        await writer.flush()
        await _mark_subagent_terminal(run_id, "cancelled", error_type="cancelled", error_message="子任务已取消")
        await _emit_subagent_status(
            parent_run_id,
            run_id=run_id,
            child_thread_id=child_thread_id,
            tool_call_id=tool_call_id,
            status="cancelled",
        )
        logger.info(f"Subagent run cancelled: {run_id}")
    except Exception as e:
        await writer.flush()
        if _is_retryable_exception(e) and not _is_last_try(ctx):
            logger.warning(f"Subagent run retryable failure {run_id} (try={_job_try(ctx)}): {e}")
            if isinstance(e, RetryableRunError):
                raise
            raise RetryableRunError(str(e)) from e
        logger.error(f"Subagent run failed {run_id}: {e}")
        await _mark_subagent_terminal(run_id, "failed", error_type=type(e).__name__, error_message=str(e))
        await _emit_subagent_status(
            parent_run_id,
            run_id=run_id,
            child_thread_id=child_thread_id,
            tool_call_id=tool_call_id,
            status="failed",
            error_message=str(e),
        )
    finally:
        await run_ctx.close()
        await clear_cancel_signal(run_id)


class SubAgentWorkerSettings:
    """子智能体专用 ARQ worker 配置（``arq server.worker_main.SubAgentWorkerSettings``）。

    独立 ``queue_name`` 与主 worker 隔离：父 run 占用主队列 slot 等待子 run 期间，
    子任务始终有独立执行容量，杜绝池耗尽死锁。生命周期钩子与主 worker 一致。
    """

    functions = [process_subagent_run]
    queue_name = SUBAGENT_QUEUE_NAME
    max_tries = 2
    retry_jobs = True
    job_timeout = int(os.getenv("SUBAGENT_JOB_TIMEOUT_SECONDS", "3600"))
    keep_result = 60
    max_jobs = int(os.getenv("SUBAGENT_MAX_JOBS", "10"))
    on_startup = _worker_startup
    on_shutdown = _worker_shutdown
    try:
        from arq.connections import RedisSettings

        redis_settings = RedisSettings.from_dsn(REDIS_URL)
    except Exception:
        redis_settings = None
