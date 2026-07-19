"""application-call 节点执行器：嵌套调用其他 agent 作为子任务。

与 P0-1 task 工具能力对齐，但走确定性路径（不依赖 LLM 自主决定委派）。
调用目标 agent 后，将其输出解析为 SubAgentDeliverable。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §五.2、§四.2
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage

from starring.agents.buildin import agent_manager
from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.buildin.workflow.nodes import register_node
from starring.agents.buildin.workflow.nodes.llm import _build_node_input
from starring.agents.buildin.workflow.state import WorkflowState
from starring.agents.middlewares.subagent_deliverable import (
    SubAgentDeliverable,
    _parse_deliverable,
)


@register_node("application-call")
async def execute_application_call(
    state: WorkflowState, node: Node, context: WorkflowContext
) -> dict:
    """嵌套调用 agent 节点执行器。

    config 字段:
        target_agent_slug: 目标 agent 的 slug（必填）
        input_template: 输入模板（可选，Phase 1 简化为前置文本）
    """
    config = node.config
    target_slug = config["target_agent_slug"]

    # 构造输入：拼接上游 summary + 模板
    user_input = _build_node_input(state, node.id)
    input_template = config.get("input_template")
    if input_template:
        user_input = f"{input_template}\n\n{user_input}"

    # 从 agent_manager 查找目标 agent
    target_agent = None
    for agent_id in agent_manager._classes:
        if agent_id == target_slug or target_slug in agent_id:
            target_agent = agent_manager.get_agent(agent_id)
            break

    if target_agent is None:
        raise ValueError(
            f"application-call 节点 {node.id} 的目标 agent slug={target_slug!r} 未注册"
        )

    # 调用目标 agent（同步执行：ainvoke 单次输入）
    # 注意：嵌套深度限制 = 1，目标 agent 不能再触发工作流（由 WorkflowBackend.get_graph 检查）
    target_context = target_agent.context_schema()
    target_context.update_from_dict(
        {
            "thread_id": f"{context.thread_id}:{node.id}",
            "uid": context.uid,
            "run_id": f"{context.run_id}:{node.id}",
            "request_id": f"{context.request_id}:{node.id}",
        }
    )
    target_graph = await target_agent.get_graph(context=target_context)

    # 单次输入调用，不挂载 callbacks（与 P0-1 子 agent 一致的 fresh context 语义）
    result = await target_graph.ainvoke(
        {"messages": [HumanMessage(content=user_input)]},
        config={
            "configurable": {
                "thread_id": target_context.thread_id,
                "uid": target_context.uid,
            },
            "recursion_limit": 25,
        },
    )

    # 提取最终 assistant 输出
    messages = result.get("messages", [])
    final_text = ""
    for msg in reversed(messages):
        if msg.__class__.__name__ == "AIMessage" and msg.content:
            final_text = msg.content if isinstance(msg.content, str) else str(msg.content)
            break

    if not final_text:
        final_text = "子 agent 未返回内容"

    deliverable = _parse_deliverable(final_text)
    # 合并子 agent 产生的 artifacts
    child_artifacts = list(result.get("artifacts", []) or [])
    if child_artifacts:
        deliverable.artifacts = list(dict.fromkeys(deliverable.artifacts + child_artifacts))

    return {"node_outputs": {node.id: deliverable}}
