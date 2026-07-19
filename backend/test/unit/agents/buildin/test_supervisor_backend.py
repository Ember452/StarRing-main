"""Supervisor 智能体 backend 单测。

覆盖：
- SupervisorContext 继承关系
- SupervisorAgent 类属性
- 被 auto_discover_agents 自动发现
- build_supervisor_prompt 关键约束
- _build_supervisor_system_prompt 合并日期 + 用户自定义
- _build_supervisor_middlewares 在无子 agent 时抛 ValueError（fail-fast）
- _build_supervisor_middlewares 不挂载 KB / Skills middleware
- _build_supervisor_middlewares 强制挂载 task middleware
- SupervisorAgent.get_graph 在无子 agent 时抛 ValueError
- SupervisorAgent.get_graph 正常返回 graph

设计依据：docs/vibe/P1-A-Supervisor细化设计-20260719.md §四
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain.agents.middleware import AgentMiddleware

from starring.agents.buildin import agent_manager
from starring.agents.buildin.chatbot.context import ChatBotContext
from starring.agents.buildin.chatbot.state import ChatBotState
from starring.agents.buildin.supervisor import SupervisorAgent
from starring.agents.buildin.supervisor.backend import (
    _build_supervisor_middlewares,
    _build_supervisor_system_prompt,
)
from starring.agents.buildin.supervisor.context import SupervisorContext
from starring.agents.buildin.supervisor.prompt import build_supervisor_prompt
from starring.agents.middlewares.subagent_task import StarRingSubAgentMiddleware


def test_supervisor_context_inherits_chatbot_context():
    """SupervisorContext 应继承 ChatBotContext，保持 schema 兼容。"""
    assert issubclass(SupervisorContext, ChatBotContext)
    ctx = SupervisorContext()
    assert ctx.subagents is None
    assert ctx.use_knowledge is None


def test_supervisor_agent_class_attributes():
    """SupervisorAgent 类属性应正确设置。"""
    assert SupervisorAgent.name == "Supervisor 智能体"
    assert "task" in SupervisorAgent.description or "委派" in SupervisorAgent.description
    assert "file_upload" in SupervisorAgent.capabilities
    assert SupervisorAgent.context_schema is SupervisorContext


def test_supervisor_agent_auto_discovered():
    """SupervisorAgent 应被 auto_discover_agents 自动发现并注册到 agent_manager。"""
    # agent_manager 在 buildin/__init__.py 导入时已自动发现
    assert "SupervisorAgent" in agent_manager._classes
    assert agent_manager._classes["SupervisorAgent"] is SupervisorAgent


def test_build_supervisor_prompt_contains_key_constraints():
    """build_supervisor_prompt 应包含核心约束关键词。"""
    prompt = build_supervisor_prompt()
    assert "Supervisor 智能体" in prompt
    assert "必须委派" in prompt
    assert "不可直答" in prompt
    assert "不可调用本地工具" in prompt
    assert "Synthesis is reasoning" in prompt


def test_build_supervisor_system_prompt_merges_date_and_user_prompt():
    """_build_supervisor_system_prompt 应合并日期 + supervisor prompt + 用户自定义。"""
    # 不含用户自定义
    ctx_no_user = SupervisorContext()
    prompt_no_user = _build_supervisor_system_prompt(ctx_no_user)
    assert "当前日期" in prompt_no_user
    assert "Supervisor 智能体" in prompt_no_user

    # 含用户自定义
    ctx_with_user = SupervisorContext(system_prompt="用户自定义补充：专注于中文内容")
    prompt_with_user = _build_supervisor_system_prompt(ctx_with_user)
    assert "用户自定义补充：专注于中文内容" in prompt_with_user
    assert "Supervisor 智能体" in prompt_with_user


@pytest.mark.asyncio
async def test_build_supervisor_middlewares_raises_when_no_subagents():
    """无可用子 agent 时应抛出 ValueError（fail-fast）。"""
    ctx = SupervisorContext(uid="test-uid")
    with patch(
        "starring.agents.buildin.supervisor.backend.create_subagent_task_middleware",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(ValueError, match="至少配置 1 个可用的子智能体"):
            await _build_supervisor_middlewares(ctx)


@pytest.mark.asyncio
async def test_build_supervisor_middlewares_excludes_kb_and_skills():
    """Supervisor middleware 栈不应包含 KnowledgeBaseMiddleware / SkillsMiddleware。"""
    ctx = SupervisorContext(uid="test-uid")
    fake_middleware = StarRingSubAgentMiddleware(
        parent_context=ctx,
        subagents=[SimpleNamespace(slug="writer", name="写作子智能体", description="写作")],
    )
    with patch(
        "starring.agents.buildin.supervisor.backend.create_subagent_task_middleware",
        new_callable=AsyncMock,
        return_value=fake_middleware,
    ):
        middlewares = await _build_supervisor_middlewares(ctx)

    # 不应包含 KB / Skills middleware
    middleware_types = [type(m).__name__ for m in middlewares]
    assert "KnowledgeBaseMiddleware" not in middleware_types
    assert "SkillsMiddleware" not in middleware_types


@pytest.mark.asyncio
async def test_build_supervisor_middlewares_includes_subagent_middleware():
    """Supervisor middleware 栈应强制包含 StarRingSubAgentMiddleware。"""
    ctx = SupervisorContext(uid="test-uid")
    fake_middleware = StarRingSubAgentMiddleware(
        parent_context=ctx,
        subagents=[SimpleNamespace(slug="writer", name="写作子智能体", description="写作")],
    )
    with patch(
        "starring.agents.buildin.supervisor.backend.create_subagent_task_middleware",
        new_callable=AsyncMock,
        return_value=fake_middleware,
    ):
        middlewares = await _build_supervisor_middlewares(ctx)

    # 应包含 task middleware
    assert any(isinstance(m, StarRingSubAgentMiddleware) for m in middlewares)
    # 应包含 summary / TodoList / TokenUsage
    middleware_types = {type(m).__name__ for m in middlewares}
    assert "TokenUsageMiddleware" in middleware_types


@pytest.mark.asyncio
async def test_supervisor_get_graph_raises_when_no_subagents():
    """SupervisorAgent.get_graph 在无子 agent 时应抛出 ValueError。"""
    agent = SupervisorAgent()
    with patch(
        "starring.agents.buildin.supervisor.backend.create_subagent_task_middleware",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(ValueError, match="至少配置 1 个可用的子智能体"):
            await agent.get_graph(context=SupervisorContext(uid="test-uid"))


@pytest.mark.asyncio
async def test_supervisor_get_graph_returns_compiled_graph():
    """SupervisorAgent.get_graph 正常情况下应返回编译后的 graph。"""
    agent = SupervisorAgent()
    ctx = SupervisorContext(uid="test-uid")

    fake_middleware = StarRingSubAgentMiddleware(
        parent_context=ctx,
        subagents=[SimpleNamespace(slug="writer", name="写作子智能体", description="写作")],
    )

    fake_graph = MagicMock(name="CompiledStateGraph")
    with (
        patch(
            "starring.agents.buildin.supervisor.backend.create_subagent_task_middleware",
            new_callable=AsyncMock,
            return_value=fake_middleware,
        ),
        patch(
            "starring.agents.buildin.supervisor.backend.create_agent",
            return_value=fake_graph,
        ) as mock_create_agent,
        patch(
            "starring.agents.buildin.supervisor.backend.load_chat_model",
            return_value=MagicMock(),
        ),
        patch(
            "starring.agents.buildin.supervisor.backend.resolve_chat_model_spec",
            return_value="openai:gpt-4",
        ),
        patch.object(
            agent,
            "_get_checkpointer",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ),
    ):
        graph = await agent.get_graph(context=ctx)

    assert graph is fake_graph
    mock_create_agent.assert_called_once()
    # 验证 tools 为空列表（supervisor 不挂载本地工具）
    _, kwargs = mock_create_agent.call_args
    assert kwargs.get("tools") == []
    # 验证 state_schema 为 ChatBotState
    assert kwargs.get("state_schema") is ChatBotState
    # 验证 system_prompt 包含 supervisor 关键词
    system_prompt = kwargs.get("system_prompt", "")
    assert "Supervisor 智能体" in system_prompt
