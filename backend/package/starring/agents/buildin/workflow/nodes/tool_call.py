"""tool 节点执行器：确定性调用单个工具。

不经过 LLM，直接执行内置工具或 MCP 工具，参数由 config.args 提供，
value 支持 {{ expr }} 形式从上游节点输出映射（复用 safe_eval）。
执行结果写入 SubAgentDeliverable，供下游 condition / llm 节点消费。

设计依据：docs/vibe/P2-工作流工具生态扩展细化设计-20260725.md §四
"""

from __future__ import annotations

import json
from typing import Any

from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.buildin.workflow.nodes import register_node
from starring.agents.buildin.workflow.nodes.expr import resolve_expr
from starring.agents.buildin.workflow.state import WorkflowState
from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable


def _resolve_args(args: dict[str, Any], state: WorkflowState) -> dict[str, Any]:
    """解析工具参数：value 匹配 {{ expr }} 时用 safe_eval 求值，否则按字面量透传。

    求值上下文与 condition 节点的 when 表达式一致（node_outputs 变量），
    求值失败 fail-fast 抛错，不静默降级为字面量。
    """
    resolved: dict[str, Any] = {}
    for key, value in args.items():
        try:
            resolved[key] = resolve_expr(value, state)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError(f"tool 节点参数 {key!r} 的表达式 {value!r} 求值失败: {exc}") from exc
    return resolved


@register_node("tool")
async def execute_tool(state: WorkflowState, node: Node, context: WorkflowContext) -> dict:
    """确定性工具调用节点执行器。

    config 字段:
        tool_source: "buildin" | "mcp"（必填）
        tool_name: 工具名（必填，tool.name）
        mcp_server: MCP 服务器 slug（tool_source == "mcp" 时必填）
        args: 参数 dict（可选，value 支持 {{ expr }} 映射）
    """
    config = node.config
    tool_source = config["tool_source"]
    tool_name = config["tool_name"]

    # 解析工具实例（延迟导入避免模块加载时触发工具注册表初始化）
    if tool_source == "buildin":
        from starring.agents.toolkits.service import get_tool_instances_by_category

        candidates = get_tool_instances_by_category("buildin")
    else:
        from starring.agents.mcp.service import get_enabled_mcp_tools

        candidates = await get_enabled_mcp_tools(config["mcp_server"])

    tool = next((t for t in candidates if t.name == tool_name), None)
    if tool is None:
        available = [t.name for t in candidates]
        source_desc = "内置工具" if tool_source == "buildin" else f"MCP 服务器 {config['mcp_server']!r}"
        raise ValueError(f"tool 节点 {node.id} 的工具 {tool_name!r} 在{source_desc}中不存在，可用工具: {available}")

    # 解析参数并执行（异常直接上抛，由 _wrap_node_executor 统一记日志，fail-fast）
    resolved_args = _resolve_args(config.get("args") or {}, state)
    result = await tool.ainvoke(resolved_args)

    # 工具返回值可能是 str / dict / list（MCP 工具常返回结构化内容），统一转文本
    if isinstance(result, (dict, list)):
        raw = json.dumps(result, ensure_ascii=False)
    else:
        raw = str(result)

    deliverable = SubAgentDeliverable(
        summary=raw[:200] + ("..." if len(raw) > 200 else ""),
        raw_text=raw,
        confidence=1.0,
    )
    return {"node_outputs": {node.id: deliverable}}
