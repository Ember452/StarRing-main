"""start-end 节点执行器。

start 节点：接收用户输入，写入 node_outputs[start_id]
end 节点：合成所有节点输出为最终响应，写入 messages

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §五.2
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.buildin.workflow.nodes import register_node
from starring.agents.buildin.workflow.state import WorkflowState
from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable


@register_node("start-end")
async def execute_start_end(state: WorkflowState, node: Node, context: WorkflowContext) -> dict:
    """start / end 节点执行器。

    config.kind == "start": 从 state.messages 取用户最后一条输入，写入 node_outputs
    config.kind == "end": 合成所有节点 summary 为最终 AIMessage
    """
    kind = node.config.get("kind")

    if kind == "start":
        # 从 messages 中取最后一条用户输入
        user_message = ""
        for msg in reversed(state.get("messages", [])):
            if getattr(msg, "type", None) == "human" or msg.__class__.__name__ == "HumanMessage":
                user_message = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

        if not user_message:
            # 兜底：用 input_template 或空串
            user_message = node.config.get("input_template", "")

        deliverable = SubAgentDeliverable(
            summary=user_message[:200] + ("..." if len(user_message) > 200 else ""),
            raw_text=user_message,
            confidence=1.0,
        )
        return {"node_outputs": {node.id: deliverable}}

    if kind == "end":
        # 合成所有节点 summary 为最终响应
        node_outputs = state.get("node_outputs", {})
        # 拼接所有上游节点 summary（按节点 ID 顺序，排除自身）
        summaries = []
        for nid, deliverable in node_outputs.items():
            if nid == node.id:
                continue
            if deliverable.summary.strip():
                summaries.append(f"[节点 {nid}]: {deliverable.summary}")

        if summaries:
            final_text = "\n\n".join(summaries)
        else:
            final_text = "工作流执行完成，但未产生输出。"

        deliverable = SubAgentDeliverable(
            summary=final_text[:200] + ("..." if len(final_text) > 200 else ""),
            raw_text=final_text,
            confidence=1.0,
        )

        # end 节点同时写入 node_outputs 与 messages（供前端展示）
        return {
            "node_outputs": {node.id: deliverable},
            "messages": [AIMessage(content=final_text)],
        }

    raise ValueError(f"start-end 节点 {node.id} 的 config.kind 必须为 'start' 或 'end'，收到 {kind!r}")
