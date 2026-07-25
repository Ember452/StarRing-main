"""MemoryMiddleware 单元测试 - 记忆注入 system prompt、每 run 单次检索、remember 工具。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

import starring.memory.service as memory_service
from starring.agents.middlewares.memory import MemoryMiddleware


class _DummyModel:
    _llm_type = "test-chat"
    profile = {"max_input_tokens": 128000}

    def _get_ls_params(self) -> dict[str, str]:
        return {"ls_provider": "openai"}


def _model_request(messages: list) -> ModelRequest:
    return ModelRequest(
        model=_DummyModel(),
        messages=messages,
        system_message=None,
        tools=[],
        runtime=SimpleNamespace(context={}, config={}),
        state={"messages": messages},
    )


def _system_text(system_message) -> str:
    if system_message is None:
        return ""
    return str(system_message.text)


def _make_handler(captured: list):
    async def handler(request: ModelRequest) -> ModelResponse:
        captured.append(request)
        return ModelResponse(result=[AIMessage(content="ok")])

    return handler


@pytest.mark.unit
async def test_awrap_model_call_injects_retrieved_memories(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_retrieve(uid: str, query: str, top_k: int = 5) -> list[dict]:
        calls.append((uid, query))
        return [{"content": "用户偏好简洁的中文回复"}, {"content": "用户是后端工程师"}]

    monkeypatch.setattr(memory_service, "retrieve_memories", fake_retrieve)
    middleware = MemoryMiddleware("u-1")
    captured: list[ModelRequest] = []

    await middleware.awrap_model_call(
        _model_request([HumanMessage(content="帮我写个函数")]),
        _make_handler(captured),
    )

    assert calls == [("u-1", "帮我写个函数")]
    system_text = _system_text(captured[0].system_message)
    assert "## 用户长期记忆" in system_text
    assert "- 用户偏好简洁的中文回复" in system_text
    assert "- 用户是后端工程师" in system_text


@pytest.mark.unit
async def test_awrap_model_call_retrieves_only_once_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_retrieve(uid: str, query: str, top_k: int = 5) -> list[dict]:
        calls.append((uid, query))
        return [{"content": "用户喜欢猫"}]

    monkeypatch.setattr(memory_service, "retrieve_memories", fake_retrieve)
    middleware = MemoryMiddleware("u-1")
    captured: list[ModelRequest] = []
    handler = _make_handler(captured)

    await middleware.awrap_model_call(_model_request([HumanMessage(content="第一轮")]), handler)
    await middleware.awrap_model_call(_model_request([HumanMessage(content="第二轮")]), handler)

    # 只检索一次，但两次模型调用都注入缓存的记忆
    assert len(calls) == 1
    assert "用户喜欢猫" in _system_text(captured[0].system_message)
    assert "用户喜欢猫" in _system_text(captured[1].system_message)


@pytest.mark.unit
async def test_awrap_model_call_skips_injection_when_no_memories(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_retrieve(uid: str, query: str, top_k: int = 5) -> list[dict]:
        calls.append((uid, query))
        return []

    monkeypatch.setattr(memory_service, "retrieve_memories", fake_retrieve)
    middleware = MemoryMiddleware("u-1")
    captured: list[ModelRequest] = []
    handler = _make_handler(captured)
    request = _model_request([HumanMessage(content="你好")])

    await middleware.awrap_model_call(request, handler)
    await middleware.awrap_model_call(_model_request([HumanMessage(content="再问一次")]), handler)

    # 无记忆时不修改请求，且"已检索无结果"同样被缓存
    assert captured[0] is request
    assert captured[0].system_message is None
    assert len(calls) == 1


@pytest.mark.unit
async def test_awrap_model_call_skips_retrieval_without_human_message(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_retrieve(uid: str, query: str, top_k: int = 5) -> list[dict]:
        calls.append((uid, query))
        return [{"content": "不应出现"}]

    monkeypatch.setattr(memory_service, "retrieve_memories", fake_retrieve)
    middleware = MemoryMiddleware("u-1")
    captured: list[ModelRequest] = []

    await middleware.awrap_model_call(_model_request([AIMessage(content="没有用户消息")]), _make_handler(captured))

    assert calls == []
    assert captured[0].system_message is None


@pytest.mark.unit
async def test_awrap_model_call_extracts_query_from_multimodal_content(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_retrieve(uid: str, query: str, top_k: int = 5) -> list[dict]:
        calls.append((uid, query))
        return []

    monkeypatch.setattr(memory_service, "retrieve_memories", fake_retrieve)
    middleware = MemoryMiddleware("u-1")
    message = HumanMessage(
        content=[
            {"type": "text", "text": "看看这张图"},
            {"type": "image_url", "image_url": {"url": "http://example.com/a.png"}},
        ]
    )

    await middleware.awrap_model_call(_model_request([message]), _make_handler([]))

    assert calls == [("u-1", "看看这张图")]


@pytest.mark.unit
async def test_awrap_model_call_retrieval_failure_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_retrieve(uid: str, query: str, top_k: int = 5) -> list[dict]:
        raise RuntimeError("milvus down")

    monkeypatch.setattr(memory_service, "retrieve_memories", fake_retrieve)
    middleware = MemoryMiddleware("u-1")
    captured: list[ModelRequest] = []

    result = await middleware.awrap_model_call(
        _model_request([HumanMessage(content="你好")]),
        _make_handler(captured),
    )

    # 检索异常不阻断模型调用，也不注入
    assert isinstance(result, ModelResponse)
    assert captured[0].system_message is None


@pytest.mark.unit
async def test_remember_tool_passes_uid_and_manual_source(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}

    async def fake_add(uid: str, content: str, *, source: str = "auto", thread_id=None, run_id=None):
        recorded.update(uid=uid, content=content, source=source)
        return {"id": "m1", "content": content, "source": source}

    monkeypatch.setattr(memory_service, "add_memory", fake_add)
    middleware = MemoryMiddleware("u-42")
    tool = middleware.tools[0]

    assert tool.name == "remember"
    assert "content" in tool.args

    result = await tool.coroutine(content="用户喜欢猫", runtime=SimpleNamespace())

    assert result == "已记住：用户喜欢猫"
    assert recorded == {"uid": "u-42", "content": "用户喜欢猫", "source": "manual"}


@pytest.mark.unit
async def test_remember_tool_reports_duplicate_or_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_add(uid: str, content: str, *, source: str = "auto", thread_id=None, run_id=None):
        return None

    monkeypatch.setattr(memory_service, "add_memory", fake_add)
    middleware = MemoryMiddleware("u-42")

    result = await middleware.tools[0].coroutine(content="用户喜欢猫", runtime=SimpleNamespace())

    assert result == "该记忆与已有记忆重复或已达存储上限，未写入。"
