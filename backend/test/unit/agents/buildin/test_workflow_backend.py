"""WorkflowBackend 测试。

覆盖：
- WorkflowContext 字段
- WorkflowBackend 类属性
- WorkflowBackend 被 auto_discover_agents 自动发现
- _build_state_graph 正确编译线性工作流
- _build_state_graph 正确编译条件分支工作流
- _build_state_graph fail-fast 校验（缺 workflow_id）
- 节点执行器被正确注册到 NODE_REGISTRY

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §八、§十
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from starring.agents.buildin import agent_manager
from starring.agents.buildin.workflow import WorkflowBackend
from starring.agents.buildin.workflow.backend import WorkflowBackend as WorkflowBackendFromBackend
from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Edge, Node, WorkflowDefinition
from starring.agents.buildin.workflow.nodes import NODE_REGISTRY


def _make_linear_definition() -> WorkflowDefinition:
    """构造线性工作流：start -> llm -> end。"""
    return WorkflowDefinition(
        nodes=[
            Node(id="start", node_type="start-end", config={"kind": "start"}),
            Node(id="llm-1", node_type="llm", config={"system_prompt": "你是助手"}),
            Node(id="end", node_type="start-end", config={"kind": "end"}),
        ],
        edges=[
            Edge(source="start", target="llm-1"),
            Edge(source="llm-1", target="end"),
        ],
    )


def _make_branch_definition() -> WorkflowDefinition:
    """构造条件分支工作流：start -> llm -> condition -> branch_a/branch_b -> end。"""
    return WorkflowDefinition(
        nodes=[
            Node(id="start", node_type="start-end", config={"kind": "start"}),
            Node(id="llm-1", node_type="llm", config={"system_prompt": "你是助手"}),
            Node(id="cond", node_type="condition", config={
                "cases": [{"when": "true", "then": "branch_a"}],
                "default": "branch_b",
            }),
            Node(id="branch_a", node_type="llm", config={"system_prompt": "分支 A"}),
            Node(id="branch_b", node_type="llm", config={"system_prompt": "分支 B"}),
            Node(id="end", node_type="start-end", config={"kind": "end"}),
        ],
        edges=[
            Edge(source="start", target="llm-1"),
            Edge(source="llm-1", target="cond"),
            Edge(source="cond", target="branch_a", branch="branch_a"),
            Edge(source="cond", target="branch_b", branch="branch_b"),
            Edge(source="branch_a", target="end"),
            Edge(source="branch_b", target="end"),
        ],
    )


# ---------------------------------------------------------------------------
# Context & class attributes
# ---------------------------------------------------------------------------


def test_workflow_context_fields():
    """WorkflowContext 应包含 workflow_id / workflow_version 字段。"""
    ctx = WorkflowContext()
    assert ctx.workflow_id is None
    assert ctx.workflow_version == 0


def test_workflow_backend_class_attributes():
    """WorkflowBackend 类属性应正确设置。"""
    assert WorkflowBackend.name == "工作流引擎"
    assert "流程" in WorkflowBackend.description or "编排" in WorkflowBackend.description
    assert "file_upload" in WorkflowBackend.capabilities
    assert WorkflowBackend.context_schema is WorkflowContext


def test_workflow_backend_auto_discovered():
    """WorkflowBackend 应被 auto_discover_agents 自动发现。"""
    assert "WorkflowBackend" in agent_manager._classes
    assert agent_manager._classes["WorkflowBackend"] is WorkflowBackend


def test_workflow_backend_same_class_as_backend_module():
    """WorkflowBackend from __init__ 与 backend module 是同一个类。"""
    assert WorkflowBackend is WorkflowBackendFromBackend


# ---------------------------------------------------------------------------
# NODE_REGISTRY
# ---------------------------------------------------------------------------


def test_all_node_types_registered():
    """4 个节点类型都应注册到 NODE_REGISTRY。"""
    assert "start-end" in NODE_REGISTRY
    assert "llm" in NODE_REGISTRY
    assert "condition" in NODE_REGISTRY
    assert "application-call" in NODE_REGISTRY


def test_get_node_executor_unknown_type_raises():
    """未知节点类型应抛 ValueError。"""
    from starring.agents.buildin.workflow.nodes import get_node_executor

    with pytest.raises(ValueError, match="未知节点类型"):
        get_node_executor("unknown-type")


# ---------------------------------------------------------------------------
# _build_state_graph
# ---------------------------------------------------------------------------


def test_build_state_graph_linear_workflow():
    """线性工作流应正确编译为 StateGraph。"""
    backend = WorkflowBackend()
    ctx = WorkflowContext(workflow_id="test-wf")
    definition = _make_linear_definition()

    graph = backend._build_state_graph(definition, ctx)

    # 验证返回的是 CompiledStateGraph
    assert graph is not None
    # 验证 graph 有编译后的节点信息
    assert hasattr(graph, "nodes")


def test_build_state_graph_branch_workflow():
    """条件分支工作流应正确编译。"""
    backend = WorkflowBackend()
    ctx = WorkflowContext(workflow_id="test-wf")
    definition = _make_branch_definition()

    graph = backend._build_state_graph(definition, ctx)

    assert graph is not None
    assert hasattr(graph, "nodes")


# ---------------------------------------------------------------------------
# get_graph (集成)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_graph_raises_when_no_workflow_id():
    """无 workflow_id 时 get_graph 应抛 ValueError（fail-fast）。"""
    backend = WorkflowBackend()
    ctx = WorkflowContext()  # workflow_id is None

    with pytest.raises(ValueError, match="缺少 workflow_id"):
        await backend.get_graph(context=ctx)


@pytest.mark.asyncio
async def test_get_graph_loads_definition_from_db():
    """get_graph 应通过 slug 从数据库加载工作流定义并编译为 graph。"""
    backend = WorkflowBackend()
    ctx = WorkflowContext(workflow_id="test-wf-id")

    fake_workflow = MagicMock()
    fake_workflow.is_active = True
    fake_workflow.version = 1
    fake_workflow.definition = _make_linear_definition().model_dump()

    fake_repo = MagicMock()
    # slug 路径命中（get_by_slug 返回 workflow，get 不会被调用）
    fake_repo.get_by_slug = AsyncMock(return_value=fake_workflow)
    fake_repo.get = AsyncMock(return_value=None)

    fake_db = MagicMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "starring.storage.postgres.manager.pg_manager.get_async_session_context",
            return_value=MagicMock(__aenter__=AsyncMock(return_value=fake_db), __aexit__=AsyncMock(return_value=None)),
        ),
        patch(
            "starring.repositories.workflow_repository.WorkflowRepository",
            return_value=fake_repo,
        ),
    ):
        graph = await backend.get_graph(context=ctx)

    assert graph is not None
    assert ctx.workflow_version == 1
    # 确认走的是 slug 路径
    fake_repo.get_by_slug.assert_awaited_once_with("test-wf-id")
    fake_repo.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_graph_falls_back_to_uuid_when_slug_not_found():
    """slug 查不到时应回退到 UUID 查找。"""
    backend = WorkflowBackend()
    ctx = WorkflowContext(workflow_id="some-uuid-string")

    fake_workflow = MagicMock()
    fake_workflow.is_active = True
    fake_workflow.version = 2
    fake_workflow.definition = _make_linear_definition().model_dump()

    fake_repo = MagicMock()
    fake_repo.get_by_slug = AsyncMock(return_value=None)
    fake_repo.get = AsyncMock(return_value=fake_workflow)

    fake_db = MagicMock()

    with (
        patch(
            "starring.storage.postgres.manager.pg_manager.get_async_session_context",
            return_value=MagicMock(__aenter__=AsyncMock(return_value=fake_db), __aexit__=AsyncMock(return_value=None)),
        ),
        patch(
            "starring.repositories.workflow_repository.WorkflowRepository",
            return_value=fake_repo,
        ),
    ):
        graph = await backend.get_graph(context=ctx)

    assert graph is not None
    assert ctx.workflow_version == 2
    fake_repo.get_by_slug.assert_awaited_once_with("some-uuid-string")
    fake_repo.get.assert_awaited_once_with("some-uuid-string")


@pytest.mark.asyncio
async def test_get_graph_raises_when_workflow_not_found():
    """工作流不存在时（slug 与 UUID 都查不到）get_graph 应抛 ValueError。"""
    backend = WorkflowBackend()
    ctx = WorkflowContext(workflow_id="nonexistent-wf")

    fake_repo = MagicMock()
    fake_repo.get_by_slug = AsyncMock(return_value=None)
    fake_repo.get = AsyncMock(return_value=None)

    fake_db = MagicMock()

    with (
        patch(
            "starring.storage.postgres.manager.pg_manager.get_async_session_context",
            return_value=MagicMock(__aenter__=AsyncMock(return_value=fake_db), __aexit__=AsyncMock(return_value=None)),
        ),
        patch(
            "starring.repositories.workflow_repository.WorkflowRepository",
            return_value=fake_repo,
        ),
    ):
        with pytest.raises(ValueError, match="不存在"):
            await backend.get_graph(context=ctx)


@pytest.mark.asyncio
async def test_get_graph_raises_when_workflow_inactive():
    """工作流已停用时 get_graph 应抛 ValueError。"""
    backend = WorkflowBackend()
    ctx = WorkflowContext(workflow_id="inactive-wf")

    fake_workflow = MagicMock()
    fake_workflow.is_active = False
    fake_workflow.version = 1

    fake_repo = MagicMock()
    fake_repo.get_by_slug = AsyncMock(return_value=fake_workflow)

    fake_db = MagicMock()

    with (
        patch(
            "starring.storage.postgres.manager.pg_manager.get_async_session_context",
            return_value=MagicMock(__aenter__=AsyncMock(return_value=fake_db), __aexit__=AsyncMock(return_value=None)),
        ),
        patch(
            "starring.repositories.workflow_repository.WorkflowRepository",
            return_value=fake_repo,
        ),
    ):
        with pytest.raises(ValueError, match="已停用"):
            await backend.get_graph(context=ctx)


# ---------------------------------------------------------------------------
# _build_state_graph fail-fast 校验
# ---------------------------------------------------------------------------


def test_build_state_graph_normal_node_multi_edges_raises():
    """普通节点（非 condition）有多条出边时应 fail-fast。"""
    backend = WorkflowBackend()
    ctx = WorkflowContext(workflow_id="test-wf")
    # llm-1 节点有 2 条出边（重复 target，无环，但多出边）
    definition = WorkflowDefinition(
        nodes=[
            Node(id="start", node_type="start-end", config={"kind": "start"}),
            Node(id="llm-1", node_type="llm", config={"system_prompt": "x"}),
            Node(id="end", node_type="start-end", config={"kind": "end"}),
        ],
        edges=[
            Edge(source="start", target="llm-1"),
            Edge(source="llm-1", target="end"),
            Edge(source="llm-1", target="end"),  # 多出边（同 target 重复），非法
        ],
    )

    with pytest.raises(ValueError, match="普通节点只允许 1 条出边"):
        backend._build_state_graph(definition, ctx)
