"""OpenAI 兼容 API 出口（/v1）。

首版仅支持非流式 POST /v1/chat/completions：
- ``model`` 参数映射为 Agent slug（走可见性校验）
- 认证复用 get_required_user（已支持 ``Bearer yxkey_...`` API Key）
- 执行链路复用 create_thread_view → create_run → await_agent_run_result，
  每次请求创建一次性 thread（metadata.source="openai_compat"）
- 错误响应统一 OpenAI ``{"error": {...}}`` 结构（与 /api 的 detail 风格区分）
- usage 暂不统计 token，置 0；不支持 stream / /v1/models / 会话复用
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from starring.repositories.agent_repository import AgentRepository
from starring.services.agent_run_service import await_agent_run_result, create_run
from starring.services.conversation_service import create_thread_view
from starring.storage.postgres.models_business import User
from starring.utils.logging_config import logger

openai_compat_router = APIRouter(tags=["OpenAI Compat"])


class ChatCompletionRequest(BaseModel):
    """OpenAI chat/completions 请求体。未列出的 OpenAI 参数接受但忽略。"""

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model: str = Field(..., description="Agent slug")
    messages: list[dict[str, Any]] = Field(..., description="OpenAI messages 数组")
    stream: bool = Field(False, description="暂不支持流式，true 时返回 400")


def _openai_error(status_code: int, message: str, err_type: str, code: str | None = None) -> JSONResponse:
    """OpenAI 风格错误响应：{"error": {"message", "type", "param", "code"}}。"""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "param": None, "code": code}},
    )


def _extract_user_query(messages: list[dict[str, Any]]) -> str | None:
    """取最后一条 role=user 消息的文本内容。

    content 支持字符串与 OpenAI parts 数组（拼接 type=text 段）。
    """
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
            return "\n".join(t for t in texts if t)
        return None
    return None


@openai_compat_router.post("/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """OpenAI 兼容的非流式对话接口。"""
    if payload.stream:
        return _openai_error(
            400,
            "Streaming is not supported yet. Set stream=false.",
            "invalid_request_error",
            "stream_not_supported",
        )

    query = _extract_user_query(payload.messages)
    if not query:
        return _openai_error(
            400,
            "No user message with text content found in 'messages'.",
            "invalid_request_error",
            "invalid_messages",
        )

    agent = await AgentRepository(db).get_visible_by_slug(slug=payload.model, user=current_user)
    if not agent:
        return _openai_error(
            404,
            f"The model '{payload.model}' does not exist or you do not have access to it.",
            "invalid_request_error",
            "model_not_found",
        )

    try:
        # 每请求一次性 thread；create_run 入队后阻塞等待 run 终结
        thread = await create_thread_view(
            agent_id=agent.slug,
            title=f"[openai_compat] {query[:50]}",
            metadata={"source": "openai_compat"},
            db=db,
            current_uid=current_user.uid,
        )
        run_response = await create_run(
            query=query,
            agent_id=agent.slug,
            thread_id=thread["id"],
            meta={"source": "openai_compat"},
            image_content=None,
            current_uid=current_user.uid,
            db=db,
        )
        run_id = run_response["run_id"]
        result = await await_agent_run_result(run_id=run_id, current_uid=current_user.uid)
    except HTTPException as e:
        return _openai_error(e.status_code, str(e.detail), "invalid_request_error")
    except Exception as e:
        logger.exception(f"openai_compat chat/completions failed for model {payload.model}: {e}")
        return _openai_error(500, "Internal server error.", "server_error")

    if result.get("status") != "completed":
        error = result.get("error") or {}
        message = error.get("message") or f"Agent run ended with status '{result.get('status')}'."
        return _openai_error(500, message, "server_error", error.get("type"))

    return {
        "id": f"chatcmpl-{run_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": agent.slug,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.get("output") or ""},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
