from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import starring.agents.middlewares.subagent_task as subagent_task_middleware
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command
from starring.agents.buildin.chatbot.state import merge_subagent_runs
from starring.agents.middlewares.subagent_task import StarRingSubAgentMiddleware
from starring.repositories.agent_repository import SUB_AGENT_BACKEND_ID
from starring.services.run_queue_service import SUBAGENT_QUEUE_NAME
from starring.utils.subagent_thread_utils import make_child_thread_id


def _patch_subagent_run_tracking(monkeypatch, captured: dict | None = None):
    async def create_run(_self, **kwargs):
        if captured is not None:
            captured["create_run"] = kwargs
        return SimpleNamespace(
            id=f"sub-run-{kwargs['tool_call_id']}",
            request_id=f"sub-req-{kwargs['tool_call_id']}",
            status="queued",
            parent_agent_run_id="parent-run",
            created_at=None,
            finished_at=None,
            error_message=None,
        ), True

    async def set_status(_self, run_id, status, *, error_type=None, error_message=None):
        del error_type
        return SimpleNamespace(
            id=run_id,
            request_id=run_id.replace("sub-run", "sub-req"),
            status=status,
            parent_agent_run_id="parent-run",
            created_at=None,
            finished_at=None,
            error_message=error_message,
        )

    monkeypatch.setattr(StarRingSubAgentMiddleware, "_create_subagent_run", create_run)
    monkeypatch.setattr(StarRingSubAgentMiddleware, "_set_subagent_run_status", set_status)


def _patch_backend(monkeypatch):
    """atask 仅用 backend 做有效性校验（图构建已移入子 worker），返回占位对象即可。"""
    monkeypatch.setattr(
        subagent_task_middleware,
        "_get_agent_backend",
        lambda backend_id: object() if backend_id == SUB_AGENT_BACKEND_ID else None,
    )


def _patch_arq_pool(monkeypatch, captured: dict):
    class _Queue:
        async def enqueue_job(self, task_name, *args, **kwargs):
            captured["enqueue"] = {"task": task_name, "args": args, "kwargs": kwargs}

    async def get_pool():
        return _Queue()

    monkeypatch.setattr(subagent_task_middleware, "get_arq_pool", get_pool)


def _terminal_run(
    run_id: str,
    status: str = "completed",
    *,
    deliverable: dict | None = None,
    error_message: str | None = None,
):
    """构造子 worker 终结后的 run 快照（deliverable 已写入 output_payload）。"""
    return SimpleNamespace(
        id=run_id,
        request_id=run_id.replace("sub-run", "sub-req"),
        status=status,
        parent_agent_run_id="parent-run",
        created_at=None,
        finished_at=None,
        error_message=error_message,
        output_payload={"deliverable": deliverable} if deliverable is not None else None,
    )


def _patch_terminal_poll(monkeypatch, run):
    """让父侧等待循环首次轮询即命中终态 run。"""

    async def load_run(run_id):
        del run_id
        return run

    monkeypatch.setattr(subagent_task_middleware, "_load_subagent_run_record", load_run)


@pytest.mark.asyncio
async def test_create_task_middleware_loads_all_visible_subagents_when_empty(monkeypatch) -> None:
    class _SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _UserRepository:
        async def get_by_uid_with_db(self, _db, uid):
            assert uid == "user-1"
            return SimpleNamespace(uid="user-1", role="user")

    class _AgentRepository:
        def __init__(self, _db):
            pass

        async def list_visible_subagents(self, *, user):
            assert user.uid == "user-1"
            return [
                SimpleNamespace(
                    slug="worker",
                    name="Worker",
                    description="work on scoped tasks",
                    backend_id=SUB_AGENT_BACKEND_ID,
                    config_json={},
                ),
                SimpleNamespace(
                    slug="invalid",
                    name="Invalid",
                    description="invalid backend",
                    backend_id="ChatbotAgent",
                    config_json={},
                ),
            ]

        async def get_visible_subagent_by_slug(self, *, slug, user):
            raise AssertionError("empty subagents should load all visible subagents")

    monkeypatch.setattr(
        subagent_task_middleware,
        "pg_manager",
        SimpleNamespace(get_async_session_context=lambda: _SessionContext()),
    )
    monkeypatch.setattr(subagent_task_middleware, "UserRepository", _UserRepository)
    monkeypatch.setattr(subagent_task_middleware, "AgentRepository", _AgentRepository)

    middleware = await subagent_task_middleware.create_subagent_task_middleware(
        SimpleNamespace(thread_id="parent-thread", uid="user-1", subagents=[])
    )

    assert isinstance(middleware, StarRingSubAgentMiddleware)
    assert middleware.subagent_names == frozenset({"worker"})
    assert middleware.transformers


@pytest.mark.asyncio
async def test_task_tool_rejects_unconfigured_subagent() -> None:
    middleware = StarRingSubAgentMiddleware(
        parent_context=SimpleNamespace(thread_id="parent-thread", uid="user-1"),
        subagents=[
            SimpleNamespace(
                slug="worker",
                name="Worker",
                description="work on scoped tasks",
                backend_id=SUB_AGENT_BACKEND_ID,
                config_json={},
            )
        ],
    )
    runtime = ToolRuntime(
        state={},
        context=None,
        tool_call_id="tool-1",
        store=None,
        stream_writer=lambda _: None,
        config={},
    )

    result = await middleware.tools[0].ainvoke(
        {"description": "do work", "subagent_type": "missing", "runtime": runtime}
    )

    assert result == "无法调用子智能体 missing，可用子智能体只有：`worker`"


@pytest.mark.asyncio
async def test_task_tool_enqueues_subagent_run_and_returns_deliverable(monkeypatch) -> None:
    captured: dict = {}
    _patch_subagent_run_tracking(monkeypatch, captured)
    _patch_backend(monkeypatch)
    _patch_arq_pool(monkeypatch, captured)
    _patch_terminal_poll(
        monkeypatch,
        _terminal_run(
            "sub-run-tool-1",
            deliverable={
                "summary": "child done",
                "raw_text": "child done full",
                "artifacts": ["/home/gem/user-data/outputs/report.md"],
            },
        ),
    )

    middleware = StarRingSubAgentMiddleware(
        parent_context=SimpleNamespace(
            thread_id="child-runtime-thread",
            parent_thread_id="parent-thread",
            file_thread_id="parent-file-thread",
            uid="user-1",
            model="parent:model",
        ),
        subagents=[
            SimpleNamespace(
                slug="worker.agent",
                name="Worker",
                description="work on scoped tasks",
                backend_id=SUB_AGENT_BACKEND_ID,
                config_json={"context": {"model": "provider:model", "subagents": ["nested"]}},
            )
        ],
    )
    runtime = SimpleNamespace(tool_call_id="tool-1", state={}, config={})

    result = await middleware.tools[0].coroutine(
        description="write a report",
        subagent_type="worker.agent",
        runtime=runtime,
    )

    child_thread_id = make_child_thread_id("parent-thread", "worker.agent", "tool-1")
    # 入队契约：任务名 / run_id / 幂等 job id / 独立子智能体队列
    assert captured["enqueue"]["task"] == "process_subagent_run"
    assert captured["enqueue"]["args"] == ("sub-run-tool-1",)
    assert captured["enqueue"]["kwargs"] == {
        "_job_id": "run:sub-run-tool-1",
        "_queue_name": SUBAGENT_QUEUE_NAME,
    }
    # input_payload 快照契约：子 worker 重建上下文所需字段全部由 create_run 落库
    assert captured["create_run"]["child_thread_id"] == child_thread_id
    assert captured["create_run"]["file_thread_id"] == "parent-file-thread"
    assert captured["create_run"]["parent_model"] == "parent:model"
    assert captured["create_run"]["continuing"] is False

    assert isinstance(result, Command)
    tool_message = result.update["messages"][0]
    assert tool_message.tool_call_id == "tool-1"
    assert tool_message.content.startswith(f"> 子智能体线程 ID: {child_thread_id}")
    assert "child done" in tool_message.content
    assert result.update["artifacts"] == ["/home/gem/user-data/outputs/report.md"]
    subagent_run = result.update["subagent_runs"][0]
    assert subagent_run["status"] == "completed"
    assert subagent_run["run_id"] == "sub-run-tool-1"
    assert subagent_run["result_preview"] == "child done"
    assert subagent_run["deliverable"]["summary"] == "child done"


@pytest.mark.asyncio
async def test_task_tool_snapshots_parent_model_for_child_worker(monkeypatch) -> None:
    captured: dict = {}
    _patch_subagent_run_tracking(monkeypatch, captured)
    _patch_backend(monkeypatch)
    _patch_arq_pool(monkeypatch, captured)
    _patch_terminal_poll(
        monkeypatch,
        _terminal_run("sub-run-tool-1", deliverable={"summary": "done", "raw_text": "done"}),
    )

    middleware = StarRingSubAgentMiddleware(
        parent_context=SimpleNamespace(thread_id="parent-thread", uid="user-1", model="parent:model"),
        subagents=[
            SimpleNamespace(
                slug="worker",
                name="Worker",
                description="work on scoped tasks",
                backend_id=SUB_AGENT_BACKEND_ID,
                config_json={"context": {"model": ""}},
            )
        ],
    )
    runtime = SimpleNamespace(tool_call_id="tool-1", state={}, config={})

    result = await middleware.tools[0].coroutine(
        description="write a report",
        subagent_type="worker",
        runtime=runtime,
    )

    assert isinstance(result, Command)
    # 父模型作为回退快照传给 create_run，由子 worker 在子 agent 未配模型时使用
    assert captured["create_run"]["parent_model"] == "parent:model"


@pytest.mark.asyncio
async def test_task_tool_records_failed_subagent_run(monkeypatch) -> None:
    captured: dict = {}
    _patch_subagent_run_tracking(monkeypatch, captured)
    _patch_backend(monkeypatch)
    _patch_arq_pool(monkeypatch, captured)
    _patch_terminal_poll(monkeypatch, _terminal_run("sub-run-tool-1", "failed", error_message="child boom"))
    times = iter(["2026-05-31T02:00:00Z", "2026-05-31T02:00:04Z"])
    monkeypatch.setattr(subagent_task_middleware, "utc_isoformat", lambda: next(times))

    middleware = StarRingSubAgentMiddleware(
        parent_context=SimpleNamespace(thread_id="parent-thread", uid="user-1"),
        subagents=[
            SimpleNamespace(
                slug="worker",
                name="Worker",
                description="work on scoped tasks",
                backend_id=SUB_AGENT_BACKEND_ID,
                config_json={},
            )
        ],
    )
    runtime = SimpleNamespace(tool_call_id="tool-1", state={}, config={})

    result = await middleware.tools[0].coroutine(
        description="write a report",
        subagent_type="worker",
        runtime=runtime,
    )

    assert isinstance(result, Command)
    child_thread_id = make_child_thread_id("parent-thread", "worker", "tool-1")
    assert (
        result.update["messages"][0].content
        == f"> 子智能体线程 ID: {child_thread_id}\n\n---\n\n子智能体 worker 调用失败：child boom"
    )
    run_entry = result.update["subagent_runs"][0]
    assert run_entry["status"] == "failed"
    assert run_entry["error"] == "child boom"
    assert run_entry["run_id"] == "sub-run-tool-1"


@pytest.mark.asyncio
async def test_task_tool_continues_existing_subagent_thread(monkeypatch) -> None:
    captured: dict = {}
    _patch_subagent_run_tracking(monkeypatch, captured)
    _patch_backend(monkeypatch)
    _patch_arq_pool(monkeypatch, captured)
    _patch_terminal_poll(
        monkeypatch,
        _terminal_run(
            "sub-run-tool-2",
            deliverable={"summary": "continued done", "raw_text": "continued done"},
        ),
    )

    child_thread_id = make_child_thread_id("parent-thread", "worker.agent", "tool-old")
    middleware = StarRingSubAgentMiddleware(
        parent_context=SimpleNamespace(thread_id="parent-thread", uid="user-1"),
        subagents=[
            SimpleNamespace(
                slug="worker.agent",
                name="Worker",
                description="work on scoped tasks",
                backend_id=SUB_AGENT_BACKEND_ID,
                config_json={},
            )
        ],
    )
    runtime = SimpleNamespace(tool_call_id="tool-2", state={}, config={})

    result = await middleware.tools[0].coroutine(
        description="continue the report",
        subagent_type="worker.agent",
        runtime=runtime,
        thread_id=child_thread_id,
    )

    assert isinstance(result, Command)
    # 续跑复用既有子线程 ID，新 tool call 仍创建新 run 并入队
    assert captured["create_run"]["child_thread_id"] == child_thread_id
    assert captured["create_run"]["continuing"] is True
    assert captured["enqueue"]["kwargs"]["_job_id"] == "run:sub-run-tool-2"
    tool_message = result.update["messages"][0]
    assert tool_message.content.startswith(f"> 子智能体线程 ID: {child_thread_id}")
    assert "continued done" in tool_message.content
    assert result.update["subagent_runs"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_task_tool_reuses_terminal_run_without_enqueue(monkeypatch) -> None:
    captured: dict = {}

    async def create_run(_self, **kwargs):
        del kwargs
        return _terminal_run(
            "sub-run-tool-1",
            deliverable={"summary": "cached done", "raw_text": "cached done"},
        ), False

    monkeypatch.setattr(StarRingSubAgentMiddleware, "_create_subagent_run", create_run)
    _patch_backend(monkeypatch)
    _patch_arq_pool(monkeypatch, captured)

    middleware = StarRingSubAgentMiddleware(
        parent_context=SimpleNamespace(thread_id="parent-thread", uid="user-1"),
        subagents=[
            SimpleNamespace(
                slug="worker",
                name="Worker",
                description="work on scoped tasks",
                backend_id=SUB_AGENT_BACKEND_ID,
                config_json={},
            )
        ],
    )
    runtime = SimpleNamespace(tool_call_id="tool-1", state={}, config={})

    result = await middleware.tools[0].coroutine(
        description="write a report",
        subagent_type="worker",
        runtime=runtime,
    )

    assert isinstance(result, Command)
    # 已终结的幂等复用 run 直接按终态回传，不重新入队
    assert "enqueue" not in captured
    assert "cached done" in result.update["messages"][0].content
    assert result.update["subagent_runs"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_task_tool_returns_cancelled_response_for_cancelled_run(monkeypatch) -> None:
    captured: dict = {}
    _patch_subagent_run_tracking(monkeypatch, captured)
    _patch_backend(monkeypatch)
    _patch_arq_pool(monkeypatch, captured)
    _patch_terminal_poll(monkeypatch, _terminal_run("sub-run-tool-1", "cancelled"))

    middleware = StarRingSubAgentMiddleware(
        parent_context=SimpleNamespace(thread_id="parent-thread", uid="user-1"),
        subagents=[
            SimpleNamespace(
                slug="worker",
                name="Worker",
                description="work on scoped tasks",
                backend_id=SUB_AGENT_BACKEND_ID,
                config_json={},
            )
        ],
    )
    runtime = SimpleNamespace(tool_call_id="tool-1", state={}, config={})

    result = await middleware.tools[0].coroutine(
        description="write a report",
        subagent_type="worker",
        runtime=runtime,
    )

    assert isinstance(result, Command)
    assert "子智能体任务已取消。" in result.update["messages"][0].content
    assert result.update["subagent_runs"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_task_tool_timeout_cascades_cancel_signal(monkeypatch) -> None:
    captured: dict = {}
    cancelled: list[str] = []
    _patch_subagent_run_tracking(monkeypatch, captured)
    _patch_backend(monkeypatch)
    _patch_arq_pool(monkeypatch, captured)
    # 子 run 永远非终态 → 父侧等待超时
    _patch_terminal_poll(monkeypatch, _terminal_run("sub-run-tool-1", "running"))

    async def capture_cancel(run_id):
        cancelled.append(run_id)

    monkeypatch.setattr(subagent_task_middleware, "publish_cancel_signal", capture_cancel)

    middleware = StarRingSubAgentMiddleware(
        parent_context=SimpleNamespace(thread_id="parent-thread", uid="user-1"),
        subagents=[
            SimpleNamespace(
                slug="worker",
                name="Worker",
                description="work on scoped tasks",
                backend_id=SUB_AGENT_BACKEND_ID,
                config_json={"context": {"subagent_timeout_seconds": 0.05}},
            )
        ],
    )
    runtime = SimpleNamespace(tool_call_id="tool-1", state={}, config={})

    result = await middleware.tools[0].coroutine(
        description="write a report",
        subagent_type="worker",
        runtime=runtime,
    )

    assert isinstance(result, Command)
    # 超时级联取消子 run，回传失败 ToolMessage
    assert cancelled == ["sub-run-tool-1"]
    assert "调用失败" in result.update["messages"][0].content
    assert result.update["subagent_runs"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_await_subagent_terminal_cascades_cancel_on_parent_cancel(monkeypatch) -> None:
    cancelled: list[str] = []

    async def load_run(run_id):
        del run_id
        return _terminal_run("sub-run-tool-1", "running")

    async def capture_cancel(run_id):
        cancelled.append(run_id)

    monkeypatch.setattr(subagent_task_middleware, "_load_subagent_run_record", load_run)
    monkeypatch.setattr(subagent_task_middleware, "publish_cancel_signal", capture_cancel)

    task = asyncio.create_task(
        subagent_task_middleware._await_subagent_terminal("sub-run-tool-1", timeout_seconds=30.0)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 父 run 取消 → 等待循环向子 run 级联发布取消信号后向上传播
    assert cancelled == ["sub-run-tool-1"]


@pytest.mark.asyncio
async def test_task_tool_rejects_invalid_continuation_thread(monkeypatch) -> None:
    captured: dict = {}

    async def reject_continuation(_self, **kwargs):
        raise ValueError(f"无法继续子智能体线程 {kwargs['child_thread_id']}：当前对话中没有找到对应的子智能体运行记录")

    _patch_backend(monkeypatch)
    _patch_arq_pool(monkeypatch, captured)
    monkeypatch.setattr(StarRingSubAgentMiddleware, "_create_subagent_run", reject_continuation)

    middleware = StarRingSubAgentMiddleware(
        parent_context=SimpleNamespace(thread_id="parent-thread", uid="user-1", run_id="parent-run"),
        subagents=[
            SimpleNamespace(
                slug="worker",
                name="Worker",
                description="work on scoped tasks",
                backend_id=SUB_AGENT_BACKEND_ID,
                config_json={},
            )
        ],
    )

    unknown_thread_id = "opaque-child-thread"
    runtime = SimpleNamespace(tool_call_id="tool-2", state={}, config={})
    result = await middleware.tools[0].coroutine(
        description="continue",
        subagent_type="worker",
        runtime=runtime,
        thread_id=unknown_thread_id,
    )

    assert result == f"无法继续子智能体线程 {unknown_thread_id}：当前对话中没有找到对应的子智能体运行记录"
    # 创建被拒 → 不入队
    assert "enqueue" not in captured


def test_make_child_thread_id_fits_agent_run_thread_column() -> None:
    child_thread_id = make_child_thread_id(
        "fa62c751-d124-476f-a58c-855890aebcc4",
        "agent-with-a-very-long-slug-that-would-overflow-the-column",
        "019e86570b418b4ea6b5aee3ef87b1fa",
    )

    assert len(child_thread_id) <= 64
    assert child_thread_id == make_child_thread_id(
        "fa62c751-d124-476f-a58c-855890aebcc4",
        "agent-with-a-very-long-slug-that-would-overflow-the-column",
        "019e86570b418b4ea6b5aee3ef87b1fa",
    )


def test_merge_subagent_runs_reuses_child_thread_entry() -> None:
    child_thread_id = make_child_thread_id("parent-thread", "worker", "tool-old")

    merged = merge_subagent_runs(
        [
            {
                "id": "tool-old",
                "subagent_type": "worker",
                "subagent_name": "Worker",
                "child_thread_id": child_thread_id,
                "description": "first task",
                "status": "completed",
                "created_at": "2026-05-31T01:00:00Z",
            }
        ],
        [
            {
                "id": "tool-new",
                "subagent_type": "worker",
                "subagent_name": "Worker",
                "child_thread_id": child_thread_id,
                "description": "continue task",
                "status": "completed",
                "created_at": "2026-05-31T02:00:00Z",
            }
        ],
    )

    assert merged == [
        {
            "id": "tool-new",
            "subagent_type": "worker",
            "subagent_name": "Worker",
            "child_thread_id": child_thread_id,
            "description": "continue task",
            "status": "completed",
            "created_at": "2026-05-31T02:00:00Z",
        }
    ]
