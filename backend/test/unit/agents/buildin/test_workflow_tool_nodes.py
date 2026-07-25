"""工作流工具生态扩展测试（P2）。

覆盖：
- tool 节点定义校验（definition.py 的 tool 分支）与 llm 新字段校验（tools/mcps/max_tool_steps）
- tool 节点执行器：_resolve_args 表达式映射、buildin/mcp 工具解析与执行、找不到工具 fail-fast
- llm 节点工具挂载：未配置走裸 ainvoke（向后兼容）、挂工具走 create_agent、解析为空 fail-fast

设计依据：docs/vibe/P2-工作流工具生态扩展细化设计-20260725.md §四、§五、§9.1
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable


# ---------------------------------------------------------------------------
# 定义校验：tool 节点
# ---------------------------------------------------------------------------


def test_tool_node_definition_valid():
    """合法 tool 节点定义应通过校验（mcp 来源含 mcp_server）。"""
    node = Node(id="t1", node_type="tool", config={
        "tool_source": "mcp",
        "tool_name": "search",
        "mcp_server": "tavily",
        "args": {"query": "{{ node_outputs['start'].summary }}"},
    })
    assert node.config["tool_name"] == "search"


def test_tool_node_definition_invalid_source():
    """tool_source 不在 buildin/mcp 内应报错。"""
    with pytest.raises(ValidationError, match="tool_source"):
        Node(id="t1", node_type="tool", config={"tool_source": "http", "tool_name": "x"})


def test_tool_node_definition_missing_tool_name():
    """缺 tool_name 应报错。"""
    with pytest.raises(ValidationError, match="tool_name"):
        Node(id="t1", node_type="tool", config={"tool_source": "buildin"})


def test_tool_node_definition_mcp_requires_server():
    """tool_source=mcp 时缺 mcp_server 应报错。"""
    with pytest.raises(ValidationError, match="mcp_server"):
        Node(id="t1", node_type="tool", config={"tool_source": "mcp", "tool_name": "x"})


def test_tool_node_definition_args_must_be_dict():
    """args 非 dict 应报错。"""
    with pytest.raises(ValidationError, match="args"):
        Node(id="t1", node_type="tool", config={
            "tool_source": "buildin", "tool_name": "x", "args": ["a"],
        })


# ---------------------------------------------------------------------------
# 定义校验：llm 新字段
# ---------------------------------------------------------------------------


def test_llm_node_definition_backward_compatible():
    """存量 llm 定义（无 tools/mcps/max_tool_steps）应照常通过。"""
    node = Node(id="l1", node_type="llm", config={"system_prompt": "你是助手"})
    assert node.config.get("tools") is None


def test_llm_node_definition_with_tool_fields():
    """带合法 tools/mcps/max_tool_steps 的 llm 定义应通过。"""
    node = Node(id="l1", node_type="llm", config={
        "system_prompt": "你是助手",
        "tools": ["web_search"],
        "mcps": ["tavily"],
        "max_tool_steps": 5,
    })
    assert node.config["max_tool_steps"] == 5


def test_llm_node_definition_tools_must_be_str_list():
    """tools 含非字符串元素应报错。"""
    with pytest.raises(ValidationError, match="tools"):
        Node(id="l1", node_type="llm", config={"system_prompt": "p", "tools": [1]})


@pytest.mark.parametrize("steps", [0, 26, "5", True])
def test_llm_node_definition_max_tool_steps_out_of_range(steps):
    """max_tool_steps 越界 / 非整数 / 布尔应报错。"""
    with pytest.raises(ValidationError, match="max_tool_steps"):
        Node(id="l1", node_type="llm", config={"system_prompt": "p", "max_tool_steps": steps})


# ---------------------------------------------------------------------------
# tool 节点：_resolve_args
# ---------------------------------------------------------------------------


def _state_with_outputs():
    return {
        "messages": [],
        "node_outputs": {
            "start": SubAgentDeliverable(summary="北京", raw_text="北京", confidence=1.0),
        },
    }


def test_resolve_args_literal_passthrough():
    """非 {{ }} 的字面量应原样透传（含非字符串类型）。"""
    from starring.agents.buildin.workflow.nodes.tool_call import _resolve_args

    resolved = _resolve_args({"city": "北京", "days": 3, "flag": True}, _state_with_outputs())
    assert resolved == {"city": "北京", "days": 3, "flag": True}


def test_resolve_args_expression_evaluated():
    """{{ expr }} 整串表达式应经 safe_eval 求值为上游输出。"""
    from starring.agents.buildin.workflow.nodes.tool_call import _resolve_args

    resolved = _resolve_args(
        {"query": "{{ node_outputs['start'].summary }}"}, _state_with_outputs()
    )
    assert resolved["query"] == "北京"


def test_resolve_args_partial_template_not_evaluated():
    """非整串的 {{ }}（字符串内插值拼接）不求值，按字面量透传。"""
    from starring.agents.buildin.workflow.nodes.tool_call import _resolve_args

    value = "天气：{{ node_outputs['start'].summary }} 如何"
    resolved = _resolve_args({"query": value}, _state_with_outputs())
    assert resolved["query"] == value


def test_resolve_args_bad_expression_raises():
    """表达式求值失败应 fail-fast 抛错（不静默降级）。"""
    from starring.agents.buildin.workflow.nodes.tool_call import _resolve_args

    with pytest.raises(ValueError, match="求值失败"):
        _resolve_args({"query": "{{ node_outputs['missing'].summary }}"}, _state_with_outputs())


# ---------------------------------------------------------------------------
# tool 节点：执行器
# ---------------------------------------------------------------------------


def _fake_tool(name: str, result):
    tool = MagicMock()
    tool.name = name
    tool.ainvoke = AsyncMock(return_value=result)
    return tool


@pytest.mark.asyncio
async def test_tool_node_executes_buildin_tool():
    """buildin 来源应从内置注册表按 name 匹配并调用，结果写入 deliverable。"""
    from starring.agents.buildin.workflow.nodes.tool_call import execute_tool

    node = Node(id="t1", node_type="tool", config={
        "tool_source": "buildin",
        "tool_name": "web_search",
        "args": {"query": "{{ node_outputs['start'].summary }}"},
    })
    tool = _fake_tool("web_search", "搜索结果文本")

    with patch(
        "starring.agents.toolkits.service.get_tool_instances_by_category",
        return_value=[tool],
    ):
        result = await execute_tool(_state_with_outputs(), node, WorkflowContext())

    tool.ainvoke.assert_awaited_once_with({"query": "北京"})
    deliverable = result["node_outputs"]["t1"]
    assert deliverable.raw_text == "搜索结果文本"
    assert deliverable.confidence == 1.0


@pytest.mark.asyncio
async def test_tool_node_executes_mcp_tool_and_serializes_dict():
    """mcp 来源应从启用工具中匹配；dict 结果转 JSON 文本。"""
    from starring.agents.buildin.workflow.nodes.tool_call import execute_tool

    node = Node(id="t1", node_type="tool", config={
        "tool_source": "mcp",
        "tool_name": "search",
        "mcp_server": "tavily",
        "args": {},
    })
    tool = _fake_tool("search", {"answer": "北京晴", "score": 0.9})

    with patch(
        "starring.agents.mcp.service.get_enabled_mcp_tools",
        AsyncMock(return_value=[tool]),
    ) as fake_get:
        result = await execute_tool(_state_with_outputs(), node, WorkflowContext())

    fake_get.assert_awaited_once_with("tavily")
    deliverable = result["node_outputs"]["t1"]
    assert '"answer": "北京晴"' in deliverable.raw_text


@pytest.mark.asyncio
async def test_tool_node_not_found_lists_available():
    """工具不存在应抛错并列出可用工具名。"""
    from starring.agents.buildin.workflow.nodes.tool_call import execute_tool

    node = Node(id="t1", node_type="tool", config={
        "tool_source": "buildin",
        "tool_name": "nonexistent",
    })

    with patch(
        "starring.agents.toolkits.service.get_tool_instances_by_category",
        return_value=[_fake_tool("web_search", "")],
    ):
        with pytest.raises(ValueError, match="web_search"):
            await execute_tool(_state_with_outputs(), node, WorkflowContext())


@pytest.mark.asyncio
async def test_tool_node_long_result_truncated_in_summary():
    """超过 200 字符的结果 summary 应截断加省略号，raw_text 保留全文。"""
    from starring.agents.buildin.workflow.nodes.tool_call import execute_tool

    long_text = "长" * 300
    node = Node(id="t1", node_type="tool", config={
        "tool_source": "buildin",
        "tool_name": "gen",
    })

    with patch(
        "starring.agents.toolkits.service.get_tool_instances_by_category",
        return_value=[_fake_tool("gen", long_text)],
    ):
        result = await execute_tool(_state_with_outputs(), node, WorkflowContext())

    deliverable = result["node_outputs"]["t1"]
    assert deliverable.summary == "长" * 200 + "..."
    assert deliverable.raw_text == long_text


# ---------------------------------------------------------------------------
# llm 节点：工具挂载
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_node_without_tools_keeps_bare_invoke():
    """未配置 tools/mcps 时走裸 ainvoke，不进入 create_agent 路径。"""
    from starring.agents.buildin.workflow.nodes.llm import execute_llm

    node = Node(id="l1", node_type="llm", config={
        "system_prompt": "你是助手",
        "model": "openai:gpt-4",
    })
    fake_response = MagicMock()
    fake_response.content = "裸推理回答"
    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(return_value=fake_response)

    with (
        patch("starring.agents.buildin.workflow.nodes.llm.load_chat_model", return_value=fake_model),
        patch("starring.agents.buildin.workflow.nodes.llm.resolve_chat_model_spec", return_value="openai:gpt-4"),
        patch(
            "starring.agents.toolkits.service.resolve_configured_runtime_tools",
            AsyncMock(return_value=[]),
        ) as fake_resolve,
    ):
        result = await execute_llm(_state_with_outputs(), node, WorkflowContext())

    fake_model.ainvoke.assert_awaited_once()
    fake_resolve.assert_not_awaited()
    assert "裸推理回答" in result["node_outputs"]["l1"].raw_text


@pytest.mark.asyncio
async def test_llm_node_with_tools_uses_create_agent():
    """配置了 tools 时应走 create_agent，recursion_limit = max_tool_steps * 2 + 1。"""
    from starring.agents.buildin.workflow.nodes.llm import execute_llm

    node = Node(id="l1", node_type="llm", config={
        "system_prompt": "你是助手",
        "model": "openai:gpt-4",
        "tools": ["web_search"],
        "max_tool_steps": 3,
    })
    fake_model = MagicMock()
    fake_tool = _fake_tool("web_search", "")

    fake_agent = MagicMock()
    fake_agent.ainvoke = AsyncMock(return_value={
        "messages": [HumanMessage(content="q"), AIMessage(content="带工具的回答")],
    })
    fake_create_agent = MagicMock(return_value=fake_agent)

    with (
        patch("starring.agents.buildin.workflow.nodes.llm.load_chat_model", return_value=fake_model),
        patch("starring.agents.buildin.workflow.nodes.llm.resolve_chat_model_spec", return_value="openai:gpt-4"),
        patch("langchain.agents.create_agent", fake_create_agent),
        patch(
            "starring.agents.toolkits.service.resolve_configured_runtime_tools",
            AsyncMock(return_value=[fake_tool]),
        ) as fake_resolve,
    ):
        result = await execute_llm(_state_with_outputs(), node, WorkflowContext())

    # resolve 收到的配置对象应携带节点的 tools/mcps
    resolve_arg = fake_resolve.call_args.args[0]
    assert resolve_arg.tools == ["web_search"]
    assert resolve_arg.mcps == []
    # create_agent 不挂 middleware/checkpointer
    assert fake_create_agent.call_args.kwargs == {
        "model": fake_model,
        "tools": [fake_tool],
        "system_prompt": "你是助手",
    }
    # 步数上限换算
    invoke_config = fake_agent.ainvoke.call_args.kwargs["config"]
    assert invoke_config["recursion_limit"] == 3 * 2 + 1
    assert "带工具的回答" in result["node_outputs"]["l1"].raw_text


@pytest.mark.asyncio
async def test_llm_node_with_tools_resolved_empty_raises():
    """配置了工具但解析为空时应 fail-fast，不退化为裸推理。"""
    from starring.agents.buildin.workflow.nodes.llm import execute_llm

    node = Node(id="l1", node_type="llm", config={
        "system_prompt": "你是助手",
        "model": "openai:gpt-4",
        "mcps": ["broken-server"],
    })
    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock()

    with (
        patch("starring.agents.buildin.workflow.nodes.llm.load_chat_model", return_value=fake_model),
        patch("starring.agents.buildin.workflow.nodes.llm.resolve_chat_model_spec", return_value="openai:gpt-4"),
        patch(
            "starring.agents.toolkits.service.resolve_configured_runtime_tools",
            AsyncMock(return_value=[]),
        ),
    ):
        with pytest.raises(ValueError, match="解析为空"):
            await execute_llm(_state_with_outputs(), node, WorkflowContext())

    fake_model.ainvoke.assert_not_awaited()
