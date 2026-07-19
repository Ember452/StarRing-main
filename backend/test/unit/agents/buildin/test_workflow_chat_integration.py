"""WorkflowBackend 与 chat_service / agent_runtime_service 集成测试。

覆盖关键集成路径：
1. chat_service._resolve_agent_runtime 在解析 WorkflowBackend 时
   自动注入 workflow_id（约定 workflows.slug == agents.slug）
2. agent_runtime_service.resolve_agent_runtime_context 同样注入 workflow_id
3. WorkflowBackend._load_definition 支持 slug + UUID 双路径查找

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §八、§十二
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from starring.agents.buildin import agent_manager
from starring.agents.buildin.workflow import WorkflowBackend
from starring.agents.buildin.workflow.context import WorkflowContext


# ---------------------------------------------------------------------------
# chat_service._resolve_agent_runtime → WorkflowBackend workflow_id 注入
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_service_resolves_workflow_id_for_workflow_backend():
    """chat_service._resolve_agent_runtime 应为 WorkflowBackend 注入 workflow_id。

    约定 workflows.slug == agents.slug，当用户未显式配置 workflow_id 时
    用 agent_item.slug 作为 workflow_id。
    """
    from starring.services import chat_service

    # 构造一个 WorkflowBackend agent_item
    fake_agent_item = SimpleNamespace(
        slug="my-workflow-agent",
        backend_id="WorkflowBackend",
        config_json={},  # 用户未显式配置 workflow_id
    )

    # mock agent_repo.get_visible_by_slug
    fake_agent_repo = MagicMock()
    fake_agent_repo.get_visible_by_slug = AsyncMock(return_value=fake_agent_item)

    # mock conv_repo.get_conversation_by_thread_id（线程不存在，新建）
    fake_conv_repo = MagicMock()
    fake_conv_repo.get_conversation_by_thread_id = AsyncMock(return_value=None)

    fake_db = MagicMock()
    fake_user = SimpleNamespace(uid="user-1", role="user")

    with (
        patch("starring.repositories.agent_repository.AgentRepository", return_value=fake_agent_repo),
        patch("starring.repositories.conversation_repository.ConversationRepository", return_value=fake_conv_repo),
        # normalize_agent_context_config 返回空 dict（用户未配置）
        patch(
            "starring.services.chat_service.normalize_agent_context_config",
            AsyncMock(return_value={}),
        ),
    ):
        agent_item, backend, agent_config = await chat_service._resolve_agent_runtime(
            db=fake_db,
            user=fake_user,
            requested_agent_id="my-workflow-agent",
            thread_id=None,
        )

    # 验证 workflow_id 已注入
    assert agent_config["workflow_id"] == "my-workflow-agent"
    # 验证 backend 是真正的 WorkflowBackend 实例
    assert isinstance(backend, WorkflowBackend)


@pytest.mark.asyncio
async def test_chat_service_preserves_explicit_workflow_id():
    """用户显式配置 workflow_id 时，chat_service 不应覆盖。"""
    from starring.services import chat_service

    fake_agent_item = SimpleNamespace(
        slug="my-wf-agent",
        backend_id="WorkflowBackend",
        config_json={"context": {"workflow_id": "custom-wf-uuid"}},
    )

    fake_agent_repo = MagicMock()
    fake_agent_repo.get_visible_by_slug = AsyncMock(return_value=fake_agent_item)

    fake_conv_repo = MagicMock()
    fake_conv_repo.get_conversation_by_thread_id = AsyncMock(return_value=None)

    fake_db = MagicMock()
    fake_user = SimpleNamespace(uid="user-1", role="user")

    # normalize_agent_context_config 透传用户配置的 workflow_id
    async def _passthrough_normalize(context, **_kwargs):
        return dict(context or {})

    with (
        patch("starring.repositories.agent_repository.AgentRepository", return_value=fake_agent_repo),
        patch("starring.repositories.conversation_repository.ConversationRepository", return_value=fake_conv_repo),
        patch(
            "starring.services.chat_service.normalize_agent_context_config",
            side_effect=_passthrough_normalize,
        ),
    ):
        _item, _backend, agent_config = await chat_service._resolve_agent_runtime(
            db=fake_db,
            user=fake_user,
            requested_agent_id="my-wf-agent",
            thread_id=None,
        )

    # 验证显式配置未被覆盖
    assert agent_config["workflow_id"] == "custom-wf-uuid"


@pytest.mark.asyncio
async def test_chat_service_skips_workflow_id_for_non_workflow_backend():
    """非 WorkflowBackend（如 ChatbotAgent）不应注入 workflow_id。"""
    from starring.services import chat_service

    # ChatbotAgent 不在 workflow 模块，没有 workflow_id 字段
    fake_agent_item = SimpleNamespace(
        slug="my-chatbot",
        backend_id="ChatbotAgent",
        config_json={},
    )

    fake_agent_repo = MagicMock()
    fake_agent_repo.get_visible_by_slug = AsyncMock(return_value=fake_agent_item)

    fake_conv_repo = MagicMock()
    fake_conv_repo.get_conversation_by_thread_id = AsyncMock(return_value=None)

    fake_db = MagicMock()
    fake_user = SimpleNamespace(uid="user-1", role="user")

    with (
        patch("starring.repositories.agent_repository.AgentRepository", return_value=fake_agent_repo),
        patch("starring.repositories.conversation_repository.ConversationRepository", return_value=fake_conv_repo),
        patch(
            "starring.services.chat_service.normalize_agent_context_config",
            AsyncMock(return_value={}),
        ),
    ):
        _item, _backend, agent_config = await chat_service._resolve_agent_runtime(
            db=fake_db,
            user=fake_user,
            requested_agent_id="my-chatbot",
            thread_id=None,
        )

    # 验证未注入 workflow_id（ChatbotAgent 的 context_schema 没有 workflow_id 字段）
    assert "workflow_id" not in agent_config


# ---------------------------------------------------------------------------
# agent_runtime_service.resolve_agent_runtime_context → workflow_id 注入
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_runtime_service_injects_workflow_id():
    """agent_runtime_service.resolve_agent_runtime_context 应为 WorkflowBackend 注入 workflow_id。"""
    from starring.services import agent_runtime_service

    fake_agent_item = SimpleNamespace(
        slug="rt-wf-agent",
        backend_id="WorkflowBackend",
        config_json={},
    )

    fake_agent_repo = MagicMock()
    fake_agent_repo.get_visible_by_slug = AsyncMock(return_value=fake_agent_item)

    fake_db = MagicMock()
    fake_user = SimpleNamespace(uid="user-1", role="user")

    with (
        patch(
            "starring.repositories.agent_repository.AgentRepository",
            return_value=fake_agent_repo,
        ),
        patch(
            "starring.services.agent_runtime_service.normalize_agent_context_config",
            AsyncMock(return_value={}),
        ),
    ):
        context = await agent_runtime_service.resolve_agent_runtime_context(
            db=fake_db,
            user=fake_user,
            bound_agent_id="rt-wf-agent",
        )

    # 验证 workflow_id 已注入到 context
    assert context.workflow_id == "rt-wf-agent"
    assert isinstance(context, WorkflowContext)


# ---------------------------------------------------------------------------
# WorkflowBackend 与 chat_service 的整体集成
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_backend_registered_in_agent_manager():
    """WorkflowBackend 应被 auto_discover_agents 注册到 agent_manager。"""
    assert "WorkflowBackend" in agent_manager._classes
    backend = agent_manager.get_agent("WorkflowBackend")
    assert backend is not None
    assert backend.context_schema is WorkflowContext


@pytest.mark.asyncio
async def test_workflow_context_schema_has_workflow_id_field():
    """WorkflowContext 必须含 workflow_id 字段（chat_service 注入逻辑依赖此）。"""
    from dataclasses import fields

    field_names = {f.name for f in fields(WorkflowContext)}
    assert "workflow_id" in field_names


@pytest.mark.asyncio
async def test_chat_service_injected_workflow_id_loads_definition_via_slug():
    """端到端集成：chat_service 注入的 workflow_id 应能被 _load_definition 通过 slug 路径加载。

    完整链路：
    1. chat_service._resolve_agent_runtime 注入 workflow_id = agent slug
    2. WorkflowBackend.get_graph(context) 用 context.workflow_id 查库
    3. _load_definition 先 get_by_slug 命中
    """
    from starring.agents.buildin.workflow.definition import (
        Edge,
        Node,
        WorkflowDefinition,
    )

    backend = WorkflowBackend()
    ctx = WorkflowContext(workflow_id="my-wf-slug")

    # 准备一个最小合法工作流定义
    fake_definition = WorkflowDefinition(
        nodes=[
            Node(id="start", node_type="start-end", config={"kind": "start"}),
            Node(id="end", node_type="start-end", config={"kind": "end"}),
        ],
        edges=[Edge(source="start", target="end")],
    )

    fake_workflow = MagicMock()
    fake_workflow.is_active = True
    fake_workflow.version = 1
    fake_workflow.definition = fake_definition.model_dump()

    fake_repo = MagicMock()
    fake_repo.get_by_slug = AsyncMock(return_value=fake_workflow)
    fake_repo.get = AsyncMock(return_value=None)  # 不应被调用

    fake_db = MagicMock()

    with (
        patch(
            "starring.storage.postgres.manager.pg_manager.get_async_session_context",
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=fake_db),
                __aexit__=AsyncMock(return_value=None),
            ),
        ),
        patch(
            "starring.repositories.workflow_repository.WorkflowRepository",
            return_value=fake_repo,
        ),
    ):
        graph = await backend.get_graph(context=ctx)

    # 验证走的是 slug 路径
    fake_repo.get_by_slug.assert_awaited_once_with("my-wf-slug")
    fake_repo.get.assert_not_awaited()
    assert graph is not None
    assert ctx.workflow_version == 1
