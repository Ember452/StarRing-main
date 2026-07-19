"""工作流节点执行器测试。

覆盖 4 种节点类型的关键行为：
- start-end 节点：start 写入用户输入，end 合成最终输出
- llm 节点：调用 LLM 并解析输出为 SubAgentDeliverable
- condition 节点：safe_eval 求值与 Command(goto=...) 跳转
- application-call 节点：调用其他 agent 并返回 deliverable

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §五、§八.3
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

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
    """start-end 节点 config.kind 非法时应抛 ValueError。"""
    from starring.agents.buildin.workflow.nodes.start_end import execute_start_end

    state = {"messages": []}
    node = Node(id="x", node_type="start-end", config={"kind": "middle"})
    ctx = WorkflowContext()

    with pytest.raises(ValueError, match="必须为 'start' 或 'end'"):
        await execute_start_end(state, node, ctx)


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


@pytest.mark.asyncio
async def test_application_call_node_invokes_target_agent():
    """application-call 节点应调用目标 agent 并返回 SubAgentDeliverable。"""
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

    # mock agent_manager
    fake_agent = MagicMock()
    fake_context_schema = MagicMock()
    fake_context_instance = MagicMock()
    fake_context_schema.return_value = fake_context_instance
    fake_agent.context_schema = fake_context_schema
    fake_agent._get_checkpointer = AsyncMock(return_value=None)
    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(return_value={
        "messages": [AIMessage(content="子 agent 回答")],
        "artifacts": ["/tmp/output.txt"],
    })
    fake_agent.get_graph = AsyncMock(return_value=fake_graph)

    fake_manager = MagicMock()
    fake_manager._classes = {"target-agent": object()}
    fake_manager.get_agent = MagicMock(return_value=fake_agent)

    with patch(
        "starring.agents.buildin.workflow.nodes.application_call.agent_manager",
        fake_manager,
    ):
        result = await execute_application_call(state, node, ctx)

    assert "node_outputs" in result
    assert "call-1" in result["node_outputs"]
    deliverable = result["node_outputs"]["call-1"]
    assert "子 agent 回答" in deliverable.raw_text
    assert "/tmp/output.txt" in deliverable.artifacts


@pytest.mark.asyncio
async def test_application_call_node_raises_when_target_not_found():
    """目标 agent 不存在时应抛 ValueError。"""
    from starring.agents.buildin.workflow.nodes.application_call import (
        execute_application_call,
    )

    state = {"messages": [], "node_outputs": {}}
    node = Node(id="call-1", node_type="application-call", config={
        "target_agent_slug": "nonexistent-agent",
    })
    ctx = WorkflowContext()

    fake_manager = MagicMock()
    fake_manager._classes = {}
    fake_manager.get_agent = MagicMock(return_value=None)

    with patch(
        "starring.agents.buildin.workflow.nodes.application_call.agent_manager",
        fake_manager,
    ):
        with pytest.raises(ValueError, match="未注册"):
            await execute_application_call(state, node, ctx)
