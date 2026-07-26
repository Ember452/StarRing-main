"""human-review 节点执行器：人工审核卡点。

执行时通过 LangGraph ``interrupt`` 暂停工作流（依赖 WorkflowBackend 编译时绑定的
checkpointer），把渲染后的审核提示语下发给前端审批 UI；用户通过 resume run
（``Command(resume={"action": ..., "comment": ...})``）恢复执行：
- approve：审批结论写入 SubAgentDeliverable，继续下游节点
- reject：抛错终止，run 走既有 failed 链路（fail-fast，不做跳过）

interrupt/resume 先例：toolkits/buildin/tools.py 的 ask_user_question。
设计依据：docs/vibe/工作流能力增强设计-20260725.md §四
"""

from __future__ import annotations

from langgraph.types import interrupt

from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.buildin.workflow.nodes import register_node
from starring.agents.buildin.workflow.nodes.expr import render_template
from starring.agents.buildin.workflow.state import WorkflowState
from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable


@register_node("human-review")
async def execute_human_review(state: WorkflowState, node: Node, context: WorkflowContext) -> dict:
    """人工审核节点执行器。

    config 字段:
        message: 审核提示语（必填，支持 {{ expr }} 内嵌插值展示上游产物）

    resume 值约定: {"action": "approve" | "reject", "comment": str}
    """
    message = render_template(node.config["message"], state, where=f"human-review 节点 {node.id} 的 message")

    # interrupt 首次执行时抛 GraphInterrupt 暂停图；resume 后重入返回用户决策
    decision = interrupt(
        {
            "interrupt_type": "human_review",
            "source": "human_review",
            "node_id": node.id,
            "node_name": node.name,
            "message": message,
        }
    )

    if not isinstance(decision, dict):
        raise ValueError(f"human-review 节点 {node.id} 收到非法 resume 值 {decision!r}，期望 {{action, comment}} 结构")

    action = str(decision.get("action") or "").strip()
    comment = str(decision.get("comment") or "").strip()

    if action == "approve":
        deliverable = SubAgentDeliverable(
            summary=f"人工审核通过{f'：{comment}' if comment else ''}",
            raw_text=f"审核提示：{message}\n审批结论：通过\n审批意见：{comment or '无'}",
            confidence=1.0,
        )
        return {"node_outputs": {node.id: deliverable}}

    if action == "reject":
        raise ValueError(f"human-review 节点 {node.id} 人工审核被拒绝：{comment or '未填写拒绝意见'}")

    raise ValueError(f"human-review 节点 {node.id} 收到未知审批动作 {action!r}，仅支持 approve / reject")
