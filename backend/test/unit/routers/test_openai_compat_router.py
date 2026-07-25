"""OpenAI 兼容出口（/v1/chat/completions）单测。

覆盖：messages 文本提取（字符串/parts/无 user 消息）、stream=true 与
model 未找到的 OpenAI 错误格式、completed/error 结果映射。
不依赖真实 DB/worker：直接调用端点函数并 monkeypatch service 层。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

import server.routers.openai_compat_router as compat_module
from server.routers.openai_compat_router import (
    ChatCompletionRequest,
    _extract_user_query,
    chat_completions,
)

USER = SimpleNamespace(uid="user-1", role="user")


def _body(response: JSONResponse) -> dict:
    return json.loads(response.body)


def _patch_agent_repo(monkeypatch: pytest.MonkeyPatch, agent) -> AsyncMock:
    get_visible = AsyncMock(return_value=agent)
    monkeypatch.setattr(
        compat_module,
        "AgentRepository",
        lambda db: SimpleNamespace(get_visible_by_slug=get_visible),
    )
    return get_visible


def _patch_run_chain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict,
) -> dict[str, AsyncMock]:
    """替换 create_thread_view / create_run / await_agent_run_result。"""
    mocks = {
        "create_thread_view": AsyncMock(return_value={"id": "thread-1"}),
        "create_run": AsyncMock(return_value={"run_id": "run-1"}),
        "await_agent_run_result": AsyncMock(return_value=result),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(compat_module, name, mock)
    return mocks


# ---------------------------------------------------------------------------
# _extract_user_query
# ---------------------------------------------------------------------------


def test_extract_query_string_content():
    """字符串 content 直接返回，取最后一条 user 消息。"""
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "答"},
        {"role": "user", "content": "第二问"},
    ]
    assert _extract_user_query(messages) == "第二问"


def test_extract_query_parts_content():
    """parts 数组 content 拼接 type=text 段，忽略非文本段。"""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看看这张图"},
                {"type": "image_url", "image_url": {"url": "http://x/1.png"}},
                {"type": "text", "text": "有什么问题"},
            ],
        }
    ]
    assert _extract_user_query(messages) == "看看这张图\n有什么问题"


def test_extract_query_no_user_message():
    """无 user 消息或 content 非法时返回 None。"""
    assert _extract_user_query([{"role": "system", "content": "hi"}]) is None
    assert _extract_user_query([]) is None
    assert _extract_user_query([{"role": "user", "content": None}]) is None


# ---------------------------------------------------------------------------
# chat_completions：请求校验
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_true_returns_openai_error():
    """stream=true 返回 400，OpenAI error 结构提示暂不支持。"""
    payload = ChatCompletionRequest(model="agent-x", messages=[{"role": "user", "content": "hi"}], stream=True)
    response = await chat_completions(payload, current_user=USER, db=object())
    assert response.status_code == 400
    error = _body(response)["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "stream_not_supported"


@pytest.mark.asyncio
async def test_no_user_message_returns_400():
    """messages 中无 user 文本消息时返回 400 invalid_messages。"""
    payload = ChatCompletionRequest(model="agent-x", messages=[{"role": "system", "content": "hi"}])
    response = await chat_completions(payload, current_user=USER, db=object())
    assert response.status_code == 400
    assert _body(response)["error"]["code"] == "invalid_messages"


@pytest.mark.asyncio
async def test_model_not_found_returns_404(monkeypatch):
    """model 对应 agent 不存在或不可见时返回 404 model_not_found。"""
    _patch_agent_repo(monkeypatch, agent=None)
    payload = ChatCompletionRequest(model="ghost-agent", messages=[{"role": "user", "content": "hi"}])
    response = await chat_completions(payload, current_user=USER, db=object())
    assert response.status_code == 404
    error = _body(response)["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "model_not_found"
    assert "ghost-agent" in error["message"]


# ---------------------------------------------------------------------------
# chat_completions：执行结果映射
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_run_maps_to_chat_completion(monkeypatch):
    """run completed 时返回 OpenAI chat.completion 响应。"""
    _patch_agent_repo(monkeypatch, agent=SimpleNamespace(slug="agent-x"))
    mocks = _patch_run_chain(monkeypatch, result={"status": "completed", "output": "你好！"})

    payload = ChatCompletionRequest(model="agent-x", messages=[{"role": "user", "content": "hi"}])
    response = await chat_completions(payload, current_user=USER, db=object())

    assert response["id"] == "chatcmpl-run-1"
    assert response["object"] == "chat.completion"
    assert response["model"] == "agent-x"
    assert response["choices"][0]["message"] == {"role": "assistant", "content": "你好！"}
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["usage"]["total_tokens"] == 0
    # 一次性 thread：metadata 标记 source=openai_compat
    thread_kwargs = mocks["create_thread_view"].await_args.kwargs
    assert thread_kwargs["metadata"] == {"source": "openai_compat"}
    run_kwargs = mocks["create_run"].await_args.kwargs
    assert run_kwargs["meta"] == {"source": "openai_compat"}
    assert run_kwargs["thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_failed_run_maps_to_openai_error(monkeypatch):
    """run 终结为 failed 时返回 500，携带错误信息。"""
    _patch_agent_repo(monkeypatch, agent=SimpleNamespace(slug="agent-x"))
    _patch_run_chain(
        monkeypatch,
        result={"status": "failed", "output": "", "error": {"type": "agent_error", "message": "模型超时"}},
    )

    payload = ChatCompletionRequest(model="agent-x", messages=[{"role": "user", "content": "hi"}])
    response = await chat_completions(payload, current_user=USER, db=object())
    assert response.status_code == 500
    error = _body(response)["error"]
    assert error["type"] == "server_error"
    assert error["code"] == "agent_error"
    assert error["message"] == "模型超时"


@pytest.mark.asyncio
async def test_http_exception_from_service_maps_to_openai_error(monkeypatch):
    """service 层抛 HTTPException 时转 OpenAI error 结构，不透传 detail 风格。"""
    _patch_agent_repo(monkeypatch, agent=SimpleNamespace(slug="agent-x"))
    monkeypatch.setattr(
        compat_module,
        "create_thread_view",
        AsyncMock(side_effect=HTTPException(status_code=404, detail="智能体不存在")),
    )

    payload = ChatCompletionRequest(model="agent-x", messages=[{"role": "user", "content": "hi"}])
    response = await chat_completions(payload, current_user=USER, db=object())
    assert response.status_code == 404
    assert _body(response)["error"]["message"] == "智能体不存在"


def test_request_model_ignores_extra_openai_params():
    """temperature/top_p 等 OpenAI 参数接受但忽略，不引发校验错误。"""
    payload = ChatCompletionRequest(
        model="agent-x",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        top_p=0.9,
        max_tokens=1024,
    )
    assert payload.model == "agent-x"
    assert payload.stream is False
