"""工作流节点执行器测试。

覆盖 4 种节点类型的关键行为：
- start-end 节点：start 写入用户输入，end 合成最终输出
- llm 节点：调用 LLM 并解析输出为 SubAgentDeliverable
- condition 节点：safe_eval 求值与 Command(goto=...) 跳转
- application-call 节点：按 slug 查库解析智能体并调用（tool 节点与新字段见
  test_workflow_tool_nodes.py）

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §五、§八.3；
         docs/vibe/P2-工作流工具生态扩展细化设计-20260725.md §三
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable


# ---------------------------------------------------------------------------
# start-end 节点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_node_writes_user_input_to_node_outputs():
    """start 节点应把用户输入写入 node_outputs。"""
    from starring.agents.buildin.workflow.nodes.start_end import execute_start_end

    state = {"messages": [HumanMessage(content="用户问题")]}
    node = Node(id="start", node_type="start-end", config={"kind": "start"})
    ctx = WorkflowContext()

    result = await execute_start_end(state, node, ctx)

    assert "node_outputs" in result
    assert "start" in result["node_outputs"]
    deliverable = result["node_outputs"]["start"]
    assert "用户问题" in deliverable.summary
    assert deliverable.raw_text == "用户问题"
    assert deliverable.confidence == 1.0


@pytest.mark.asyncio
async def test_start_node_uses_input_template_when_no_message():
    """无消息时 start 节点应使用 input_template 兜底。"""
    from starring.agents.buildin.workflow.nodes.start_end import execute_start_end

    state = {"messages": []}
    node = Node(id="start", node_type="start-end", config={
        "kind": "start",
        "input_template": "默认输入",
    })
    ctx = WorkflowContext()

    result = await execute_start_end(state, node, ctx)

    deliverable = result["node_outputs"]["start"]
    assert deliverable.raw_text == "默认输入"


@pytest.mark.asyncio
async def test_end_node_synthesizes_all_node_outputs():
    """end 节点应合成所有节点 summary 为最终 AIMessage。"""
    from starring.agents.buildin.workflow.nodes.start_end import execute_start_end

    state = {
        "messages": [],
        "node_outputs": {
            "start": SubAgentDeliverable(summary="用户问题", raw_text="用户问题"),
            "llm-1": SubAgentDeliverable(summary="LLM 回答", raw_text="LLM 完整回答"),
        },
    }
    node = Node(id="end", node_type="start-end", config={"kind": "end"})
    ctx = WorkflowContext()

    result = await execute_start_end(state, node, ctx)

    assert "messages" in result
    assert "node_outputs" in result
    assert "end" in result["node_outputs"]
    final_msg = result["messages"][0]
    assert isinstance(final_msg, AIMessage)
    assert "LLM 回答" in final_msg.content


@pytest.mark.asyncio
async def test_start_end_node_invalid_kind_raises():
    """start-end 节点 config.kind 非法时定义校验与执行器均应 fail-fast。"""
    from starring.agents.buildin.workflow.nodes.start_end import execute_start_end

    # 定义层：Node 构造期即拦截
    with pytest.raises(ValidationError, match="kind"):
        Node(id="x", node_type="start-end", config={"kind": "middle"})

    # 执行器层兜底：绕过定义校验直接改 config 也应抛错
    node = Node(id="x", node_type="start-end", config={"kind": "start"})
    node.config["kind"] = "middle"
    ctx = WorkflowContext()

    with pytest.raises(ValueError, match="必须为 'start' 或 'end'"):
        await execute_start_end({"messages": []}, node, ctx)


# ---------------------------------------------------------------------------
# llm 节点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_node_invokes_model_and_returns_deliverable():
    """llm 节点应调用 LLM 并解析输出为 SubAgentDeliverable。"""
    from starring.agents.buildin.workflow.nodes.llm import execute_llm

    state = {
        "messages": [],
        "node_outputs": {
            "start": SubAgentDeliverable(summary="用户问题", raw_text="用户问题"),
        },
    }
    node = Node(id="llm-1", node_type="llm", config={
        "system_prompt": "你是助手",
        "model": "openai:gpt-4",
    })
    ctx = WorkflowContext(model="openai:gpt-4")

    fake_response = MagicMock()
    fake_response.content = "LLM 回答内容"
    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(return_value=fake_response)

    with (
        patch(
            "starring.agents.buildin.workflow.nodes.llm.load_chat_model",
            return_value=fake_model,
        ),
        patch(
            "starring.agents.buildin.workflow.nodes.llm.resolve_chat_model_spec",
            return_value="openai:gpt-4",
        ),
    ):
        result = await execute_llm(state, node, ctx)

    assert "node_outputs" in result
    assert "llm-1" in result["node_outputs"]
    deliverable = result["node_outputs"]["llm-1"]
    assert isinstance(deliverable, SubAgentDeliverable)
    # raw_text 应包含 LLM 输出
    assert "LLM 回答内容" in deliverable.raw_text


@pytest.mark.asyncio
async def test_llm_node_raises_when_no_model_configured():
    """llm 节点未配置 model 且 context.model 为空时应抛错。"""
    from starring.agents.buildin.workflow.nodes.llm import execute_llm

    state = {"messages": [], "node_outputs": {}}
    node = Node(id="llm-1", node_type="llm", config={"system_prompt": "你是助手"})
    ctx = WorkflowContext(model="")  # 空 model

    with pytest.raises(ValueError, match="未配置 model"):
        await execute_llm(state, node, ctx)


# ---------------------------------------------------------------------------
# condition 节点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_condition_node_matches_case_returns_command():
    """condition 节点命中 case 时应返回 Command(goto=...)。"""
    from langgraph.types import Command

    from starring.agents.buildin.workflow.nodes.condition import execute_condition

    state = {
        "messages": [],
        "node_outputs": {
            "llm-1": SubAgentDeliverable(summary="合规", confidence=0.9),
        },
    }
    node = Node(id="cond", node_type="condition", config={
        "cases": [
            {"when": "node_outputs['llm-1'].confidence > 0.8", "then": "branch_a"},
            {"when": "true", "then": "branch_b"},
        ],
        "default": "branch_b",
    })
    ctx = WorkflowContext()

    result = await execute_condition(state, node, ctx)

    assert isinstance(result, Command)
    assert result.goto == "branch_a"
    # node_outputs 应包含 condition 节点的决策记录
    assert "cond" in result.update["node_outputs"]


@pytest.mark.asyncio
async def test_condition_node_falls_back_to_default():
    """所有 case 不命中时 condition 节点应走 default 分支。"""
    from langgraph.types import Command

    from starring.agents.buildin.workflow.nodes.condition import execute_condition

    state = {
        "messages": [],
        "node_outputs": {
            "llm-1": SubAgentDeliverable(summary="合规", confidence=0.3),
        },
    }
    node = Node(id="cond", node_type="condition", config={
        "cases": [
            {"when": "node_outputs['llm-1'].confidence > 0.8", "then": "branch_a"},
        ],
        "default": "branch_b",
    })
    ctx = WorkflowContext()

    result = await execute_condition(state, node, ctx)

    assert isinstance(result, Command)
    assert result.goto == "branch_b"


@pytest.mark.asyncio
async def test_condition_node_raises_when_no_default_and_no_match():
    """无 default 分支且所有 case 不命中时应抛 ValueError。"""
    from starring.agents.buildin.workflow.nodes.condition import execute_condition

    state = {
        "messages": [],
        "node_outputs": {
            "llm-1": SubAgentDeliverable(summary="合规", confidence=0.3),
        },
    }
    node = Node(id="cond", node_type="condition", config={
        "cases": [
            {"when": "node_outputs['llm-1'].confidence > 0.8", "then": "branch_a"},
        ],
        # 不设 default
    })
    ctx = WorkflowContext()

    with pytest.raises(ValueError, match="default 分支"):
        await execute_condition(state, node, ctx)


@pytest.mark.asyncio
async def test_condition_node_skips_invalid_expression():
    """condition 节点应跳过求值失败的表达式（不抛错）。"""
    from langgraph.types import Command

    from starring.agents.buildin.workflow.nodes.condition import execute_condition

    state = {
        "messages": [],
        "node_outputs": {
            "llm-1": SubAgentDeliverable(summary="合规", confidence=0.9),
        },
    }
    node = Node(id="cond", node_type="condition", config={
        "cases": [
            # 非法表达式（求值失败应跳过）
            {"when": "undefined_var", "then": "branch_a"},
            # 后续合法表达式命中
            {"when": "node_outputs['llm-1'].confidence > 0.5", "then": "branch_b"},
        ],
        "default": "branch_b",
    })
    ctx = WorkflowContext()

    result = await execute_condition(state, node, ctx)

    assert isinstance(result, Command)
    assert result.goto == "branch_b"


# ---------------------------------------------------------------------------
# application-call 节点
# ---------------------------------------------------------------------------


@dataclass
class _FakeTargetContext:
    """目标智能体的 context_schema 替身（真 dataclass，供嵌套探测用 dataclass_fields）。"""

    thread_id: str = ""
    uid: str = ""
    run_id: str = ""
    request_id: str = ""
    system_prompt: str = ""

    def update_from_dict(self, data):
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)


def _patch_agent_resolution(agent_item, *, agent_config=None):
    """patch application-call 的 DB 解析链路（pg_manager / 仓库 / normalize）。"""
    fake_db = MagicMock()

    class _SessionCtx:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *args):
            return False

    fake_pg = MagicMock()
    fake_pg.get_async_session_context = MagicMock(return_value=_SessionCtx())

    fake_user_repo_cls = MagicMock()
    fake_user_repo_cls.return_value.get_by_uid_with_db = AsyncMock(return_value=SimpleNamespace(uid="user-1"))

    fake_agent_repo_cls = MagicMock()
    fake_agent_repo_cls.return_value.get_visible_by_slug = AsyncMock(return_value=agent_item)

    return (
        patch("starring.storage.postgres.manager.pg_manager", fake_pg),
        patch("starring.repositories.user_repository.UserRepository", fake_user_repo_cls),
        patch("starring.repositories.agent_repository.AgentRepository", fake_agent_repo_cls),
        patch(
            "starring.agents.context.normalize_agent_context_config",
            AsyncMock(return_value=agent_config or {}),
        ),
    )


@pytest.mark.asyncio
async def test_application_call_node_invokes_target_agent():
    """application-call 节点应按 slug 查库解析并调用目标 agent，返回 SubAgentDeliverable。"""
    from starring.agents.buildin.workflow.nodes.application_call import (
        execute_application_call,
    )

    state = {
        "messages": [],
        "node_outputs": {
            "start": SubAgentDeliverable(summary="用户问题", raw_text="用户问题"),
        },
    }
    node = Node(id="call-1", node_type="application-call", config={
        "target_agent_slug": "target-agent",
    })
    ctx = WorkflowContext(
        thread_id="parent-thread",
        uid="user-1",
        run_id="run-1",
        request_id="req-1",
    )

    agent_item = SimpleNamespace(backend_id="TargetBackend", config_json={"context": {}})

    fake_agent = MagicMock()
    fake_agent.context_schema = _FakeTargetContext
    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(return_value={
        "messages": [AIMessage(content="子 agent 回答")],
        "artifacts": ["/tmp/output.txt"],
    })
    fake_agent.get_graph = AsyncMock(return_value=fake_graph)

    fake_manager = MagicMock()
    fake_manager._classes = {"TargetBackend": object()}
    fake_manager.get_agent = MagicMock(return_value=fake_agent)

    patches = _patch_agent_resolution(agent_item)
    with (
        patch(
            "starring.agents.buildin.workflow.nodes.application_call.agent_manager",
            fake_manager,
        ),
        patches[0], patches[1], patches[2], patches[3],
    ):
        result = await execute_application_call(state, node, ctx)

    assert "node_outputs" in result
    assert "call-1" in result["node_outputs"]
    deliverable = result["node_outputs"]["call-1"]
    assert "子 agent 回答" in deliverable.raw_text
    assert "/tmp/output.txt" in deliverable.artifacts
    # 运行时字段按 父线程:节点 派生
    target_context = fake_agent.get_graph.call_args.kwargs["context"]
    assert target_context.thread_id == "parent-thread:call-1"
    assert target_context.uid == "user-1"


@pytest.mark.asyncio
async def test_application_call_node_raises_when_target_not_found():
    """目标智能体不存在或不可见时应抛 ValueError（错误信息含 slug）。"""
    from starring.agents.buildin.workflow.nodes.application_call import (
        execute_application_call,
    )

    state = {"messages": [], "node_outputs": {}}
    node = Node(id="call-1", node_type="application-call", config={
        "target_agent_slug": "nonexistent-agent",
    })
    ctx = WorkflowContext(uid="user-1")

    patches = _patch_agent_resolution(agent_item=None)
    with patches[0], patches[1], patches[2], patches[3]:
        with pytest.raises(ValueError, match="nonexistent-agent.*不存在或无权限"):
            await execute_application_call(state, node, ctx)


@pytest.mark.asyncio
async def test_application_call_node_rejects_nested_workflow():
    """目标 backend 的 context_schema 含 workflow_id 字段（工作流类）时应拒绝嵌套。"""
    from starring.agents.buildin.workflow.nodes.application_call import (
        execute_application_call,
    )

    @dataclass
    class _WorkflowLikeContext:
        workflow_id: str = ""
        thread_id: str = ""

    state = {"messages": [], "node_outputs": {}}
    node = Node(id="call-1", node_type="application-call", config={
        "target_agent_slug": "nested-workflow",
    })
    ctx = WorkflowContext(uid="user-1")

    agent_item = SimpleNamespace(backend_id="WorkflowBackend", config_json=None)
    fake_agent = MagicMock()
    fake_agent.context_schema = _WorkflowLikeContext
    fake_manager = MagicMock()
    fake_manager._classes = {"WorkflowBackend": object()}
    fake_manager.get_agent = MagicMock(return_value=fake_agent)

    patches = _patch_agent_resolution(agent_item)
    with (
        patch(
            "starring.agents.buildin.workflow.nodes.application_call.agent_manager",
            fake_manager,
        ),
        patches[0], patches[1], patches[2], patches[3],
    ):
        with pytest.raises(ValueError, match="不允许嵌套"):
            await execute_application_call(state, node, ctx)


@pytest.mark.asyncio
async def test_application_call_node_injects_agent_config():
    """智能体 config_json.context 应注入 target_context，运行时字段最后覆盖。"""
    from starring.agents.buildin.workflow.nodes.application_call import (
        execute_application_call,
    )

    state = {"messages": [], "node_outputs": {}}
    node = Node(id="call-1", node_type="application-call", config={
        "target_agent_slug": "custom-agent",
    })
    ctx = WorkflowContext(thread_id="t", uid="user-1", run_id="r", request_id="q")

    agent_item = SimpleNamespace(
        backend_id="TargetBackend",
        config_json={"context": {"system_prompt": "自定义提示词", "thread_id": "脏数据"}},
    )
    fake_agent = MagicMock()
    fake_agent.context_schema = _FakeTargetContext
    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(return_value={"messages": [AIMessage(content="ok")]})
    fake_agent.get_graph = AsyncMock(return_value=fake_graph)
    fake_manager = MagicMock()
    fake_manager._classes = {"TargetBackend": object()}
    fake_manager.get_agent = MagicMock(return_value=fake_agent)

    # normalize 原样回传 context 配置，验证注入与运行时覆盖的先后顺序
    patches = _patch_agent_resolution(
        agent_item, agent_config={"system_prompt": "自定义提示词", "thread_id": "脏数据"}
    )
    with (
        patch(
            "starring.agents.buildin.workflow.nodes.application_call.agent_manager",
            fake_manager,
        ),
        patches[0], patches[1], patches[2], patches[3],
    ):
        await execute_application_call(state, node, ctx)

    target_context = fake_agent.get_graph.call_args.kwargs["context"]
    # 用户配置生效
    assert target_context.system_prompt == "自定义提示词"
    # 运行时字段覆盖配置里的脏 thread_id
    assert target_context.thread_id == "t:call-1"
