"""process_subagent_run 任务的单元测试。

覆盖：状态机流转（跳过终态 / 缺父 run / 排队期取消 / 完成 / 取消 / 失败重试）、
chunk 事件写入父 run 事件流（thread_id=child_thread_id 路由约定）、
deliverable 写入 output_payload、子 run 状态 custom 事件发布。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import starring.services.subagent_run_worker as subagent_run_worker
from starring.repositories.agent_repository import SUB_AGENT_BACKEND_ID
from starring.services.run_queue_service import SUBAGENT_QUEUE_NAME
from starring.services.run_worker import RetryableRunError
from starring.services.subagent_run_worker import SubAgentWorkerSettings, process_subagent_run


class _ChildContext:
    def update_from_dict(self, values: dict):
        for key, value in values.items():
            setattr(self, key, value)


class _FakeRunContext:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True


class _FakeWriter:
    instances: list["_FakeWriter"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chunks: list[tuple[dict, str | None]] = []
        self.flush_count = 0
        _FakeWriter.instances.append(self)

    async def append(self, chunk, *, thread_id=None):
        self.chunks.append((chunk, thread_id))

    async def flush(self):
        self.flush_count += 1


def _subagent_run(status: str = "queued", *, parent_run_id: str | None = "parent-run"):
    return SimpleNamespace(
        id="sub-run-1",
        status=status,
        thread_id="child-thread",
        uid="user-1",
        agent_id="worker",
        request_id="sub-req-1",
        parent_agent_run_id=parent_run_id,
        input_payload={
            "description": "write a report",
            "tool_call_id": "tool-1",
            "parent_thread_id": "parent-thread",
            "file_thread_id": "parent-file-thread",
            "parent_model": "parent:model",
        },
    )


def _patch_common(monkeypatch, run, *, stream_items=None, stream_error=None):
    """打桩子 worker 的全部外部依赖，返回捕获容器。"""
    captured: dict = {"terminal": [], "status_events": [], "mark_running": []}
    _FakeWriter.instances = []

    async def load_run(run_id):
        del run_id
        return run

    async def load_user(uid):
        return SimpleNamespace(uid=uid, username="tester", department_id=None)

    async def load_subagent(slug):
        return SimpleNamespace(
            slug=slug,
            name="Worker",
            backend_id=SUB_AGENT_BACKEND_ID,
            config_json={"context": {"model": ""}},
        )

    async def mark_terminal(run_id, status, *, error_type=None, error_message=None, output_payload=None):
        captured["terminal"].append(
            {
                "run_id": run_id,
                "status": status,
                "error_type": error_type,
                "error_message": error_message,
                "output_payload": output_payload,
            }
        )

    async def emit_status(parent_run_id, *, run_id, child_thread_id, tool_call_id, status, error_message=None):
        captured["status_events"].append(
            {
                "parent_run_id": parent_run_id,
                "run_id": run_id,
                "child_thread_id": child_thread_id,
                "tool_call_id": tool_call_id,
                "status": status,
                "error_message": error_message,
            }
        )

    async def mark_running(run_id):
        captured["mark_running"].append(run_id)

    async def has_cancel(run_id):
        del run_id
        return False

    async def clear_cancel(run_id):
        del run_id

    async def build_input_context(config_context, *, thread_id, uid, run_id, request_id):
        del config_context, thread_id, uid, run_id, request_id
        return {"model": ""}

    def fake_events(msg, *, metadata, namespace, thread_id, protocol_message_ids):
        del metadata, namespace, thread_id, protocol_message_ids
        return [{"type": "message_delta", "content": getattr(msg, "content", "")}]

    class _Graph:
        def astream(self, state, *, config, context, stream_mode):
            captured["astream"] = {
                "state": state,
                "config": config,
                "context": context,
                "stream_mode": stream_mode,
            }

            async def gen():
                for item in stream_items or []:
                    yield item
                if stream_error is not None:
                    raise stream_error

            return gen()

    class _Backend:
        context_schema = _ChildContext

        async def get_graph(self, *, context):
            captured["graph_context"] = context
            return _Graph()

    async def passthrough(stream, run_ctx):
        del run_ctx
        async for item in stream:
            yield item

    monkeypatch.setattr(subagent_run_worker, "_load_subagent_run", load_run)
    monkeypatch.setattr(subagent_run_worker, "_load_user", load_user)
    monkeypatch.setattr(subagent_run_worker, "_load_subagent", load_subagent)
    monkeypatch.setattr(subagent_run_worker, "_mark_subagent_terminal", mark_terminal)
    monkeypatch.setattr(subagent_run_worker, "_emit_subagent_status", emit_status)
    monkeypatch.setattr(subagent_run_worker, "mark_run_running", mark_running)
    monkeypatch.setattr(subagent_run_worker, "has_cancel_signal", has_cancel)
    monkeypatch.setattr(subagent_run_worker, "clear_cancel_signal", clear_cancel)
    monkeypatch.setattr(subagent_run_worker, "build_agent_input_context", build_input_context)
    monkeypatch.setattr(subagent_run_worker, "build_run_context", lambda **kwargs: None)
    monkeypatch.setattr(subagent_run_worker, "_message_payload_STARRING_events", fake_events)
    monkeypatch.setattr(subagent_run_worker, "_get_agent_backend", lambda backend_id: _Backend())
    monkeypatch.setattr(subagent_run_worker, "RunContext", _FakeRunContext)
    monkeypatch.setattr(subagent_run_worker, "ChunkedEventWriter", _FakeWriter)
    monkeypatch.setattr(subagent_run_worker, "_consume_stream_with_cancel", passthrough)
    return captured


@pytest.mark.asyncio
async def test_process_subagent_run_skips_terminal_run(monkeypatch) -> None:
    captured = _patch_common(monkeypatch, _subagent_run("completed"))

    await process_subagent_run({"job_try": 1}, "sub-run-1")

    assert captured["mark_running"] == []
    assert captured["terminal"] == []


@pytest.mark.asyncio
async def test_process_subagent_run_fails_without_parent_run(monkeypatch) -> None:
    captured = _patch_common(monkeypatch, _subagent_run(parent_run_id=None))

    await process_subagent_run({"job_try": 1}, "sub-run-1")

    assert captured["terminal"] == [
        {
            "run_id": "sub-run-1",
            "status": "failed",
            "error_type": "invalid_run",
            "error_message": "缺少父运行 ID",
            "output_payload": None,
        }
    ]


@pytest.mark.asyncio
async def test_process_subagent_run_cancelled_while_queued(monkeypatch) -> None:
    captured = _patch_common(monkeypatch, _subagent_run())

    async def has_cancel(run_id):
        del run_id
        return True

    monkeypatch.setattr(subagent_run_worker, "has_cancel_signal", has_cancel)

    await process_subagent_run({"job_try": 1}, "sub-run-1")

    assert captured["mark_running"] == []
    assert captured["terminal"][0]["status"] == "cancelled"
    assert [event["status"] for event in captured["status_events"]] == ["cancelled"]


@pytest.mark.asyncio
async def test_process_subagent_run_completes_and_streams_to_parent(monkeypatch) -> None:
    stream_items = [
        ("messages", (SimpleNamespace(content="hello"), {})),
        (
            "values",
            {
                "messages": [],
                "structured_response": {"summary": "done", "raw_text": "done full"},
                "artifacts": ["/home/gem/user-data/outputs/report.md"],
            },
        ),
    ]
    captured = _patch_common(monkeypatch, _subagent_run(), stream_items=stream_items)

    await process_subagent_run({"job_try": 1}, "sub-run-1")

    assert captured["mark_running"] == ["sub-run-1"]
    # 事件写入父 run 事件流：writer 绑定 parent_run_id，chunk 携带 child_thread_id 路由
    writer = _FakeWriter.instances[0]
    assert writer.kwargs["run_id"] == "parent-run"
    assert writer.kwargs["thread_id"] == "child-thread"
    chunk, chunk_thread_id = writer.chunks[0]
    assert chunk_thread_id == "child-thread"
    assert chunk["thread_id"] == "child-thread"
    assert chunk["status"] == "loading"
    assert chunk["subagent_tool_call_id"] == "tool-1"
    assert chunk["stream_event"] == {"type": "message_delta", "content": "hello"}
    assert writer.flush_count >= 1
    # 终结：deliverable（含 state.artifacts 合并）写入 output_payload
    assert captured["terminal"][0]["status"] == "completed"
    deliverable = captured["terminal"][0]["output_payload"]["deliverable"]
    assert deliverable["summary"] == "done"
    assert deliverable["artifacts"] == ["/home/gem/user-data/outputs/report.md"]
    # 状态事件顺序：running → completed
    assert [event["status"] for event in captured["status_events"]] == ["running", "completed"]
    # 子上下文契约：结构化输出 + 子运行时标记
    context = captured["graph_context"]
    assert context.output_format == "structured"
    assert context.is_subagent_runtime is True
    assert context.model == "parent:model"
    assert captured["astream"]["stream_mode"] == ["messages", "values"]
    assert captured["astream"]["state"]["messages"][0].content == "write a report"
    assert captured["astream"]["config"]["configurable"]["thread_id"] == "child-thread"
    assert captured["astream"]["config"]["configurable"]["subagent_tool_call_id"] == "tool-1"


@pytest.mark.asyncio
async def test_process_subagent_run_marks_cancelled_on_cancel(monkeypatch) -> None:
    captured = _patch_common(monkeypatch, _subagent_run(), stream_error=asyncio.CancelledError())

    await process_subagent_run({"job_try": 1}, "sub-run-1")

    assert captured["terminal"][0]["status"] == "cancelled"
    assert [event["status"] for event in captured["status_events"]] == ["running", "cancelled"]


@pytest.mark.asyncio
async def test_process_subagent_run_reraises_retryable_before_last_try(monkeypatch) -> None:
    captured = _patch_common(monkeypatch, _subagent_run(), stream_error=RetryableRunError("boom"))

    with pytest.raises(RetryableRunError):
        await process_subagent_run({"job_try": 1}, "sub-run-1")

    # 未达 max_tries：不落终态，交给 ARQ 重投
    assert captured["terminal"] == []


@pytest.mark.asyncio
async def test_process_subagent_run_fails_on_last_try(monkeypatch) -> None:
    captured = _patch_common(monkeypatch, _subagent_run(), stream_error=RetryableRunError("boom"))

    await process_subagent_run({"job_try": SubAgentWorkerSettings.max_tries}, "sub-run-1")

    assert captured["terminal"][0]["status"] == "failed"
    assert captured["terminal"][0]["error_message"] == "boom"
    assert [event["status"] for event in captured["status_events"]] == ["running", "failed"]


def test_subagent_worker_settings_contract() -> None:
    assert process_subagent_run in SubAgentWorkerSettings.functions
    assert SubAgentWorkerSettings.queue_name == SUBAGENT_QUEUE_NAME
    assert SubAgentWorkerSettings.max_tries >= 1
    assert SubAgentWorkerSettings.retry_jobs is True
