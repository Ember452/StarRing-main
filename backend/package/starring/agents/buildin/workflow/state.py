"""工作流执行状态。

WorkflowState 扩展自 BaseState，新增：
- node_outputs: 每个节点执行后写入的 SubAgentDeliverable（节点间数据契约）
- workflow_context: 运行时上下文快照（workflow_id / version / definition）

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §四
"""

from __future__ import annotations

from typing import Annotated

from langchain.agents import AgentState

from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable


def merge_node_outputs(
    existing: dict[str, SubAgentDeliverable] | None,
    new: dict[str, SubAgentDeliverable] | None,
) -> dict[str, SubAgentDeliverable]:
    """合并节点输出（新输出覆盖同名旧输出，保留未变更的）。"""
    if existing is None:
        return dict(new or {})
    if new is None:
        return existing
    return {**existing, **new}


class WorkflowState(AgentState):
    """工作流执行状态。

    - messages: 标准消息列表（兼容 LangGraph，end 节点合成最终 AIMessage 写入）
    - node_outputs: 节点 ID -> SubAgentDeliverable 映射
    """

    node_outputs: Annotated[dict[str, SubAgentDeliverable], merge_node_outputs]
