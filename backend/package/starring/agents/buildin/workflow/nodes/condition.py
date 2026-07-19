"""condition 节点执行器：条件分支节点。

使用 safe_eval 求值 config.cases[i].when 表达式，命中时通过 Command(goto=then)
跳转到对应分支节点。所有 case 不命中时走 config.default。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §五.2、§六
"""
from __future__ import annotations

from langgraph.types import Command

from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.buildin.workflow.nodes import register_node
from starring.agents.buildin.workflow.nodes.safe_eval import safe_eval
from starring.agents.buildin.workflow.state import WorkflowState
from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable


@register_node("condition")
async def execute_condition(
    state: WorkflowState, node: Node, context: WorkflowContext
) -> Command:
    """条件分支节点执行器。

    config 字段:
        cases: list[{"when": str, "then": str}]
            when: 受限 Python 表达式（求值结果为 bool）
            then: 命中时跳转的目标节点 ID
        default: str
            所有 case 不命中时的默认分支节点 ID
    """
    config = node.config
    cases = config["cases"]
    default_branch = config.get("default")

    # 求值上下文：把上游各节点产出的 SubAgentDeliverable 字典注入到 node_outputs 变量下，
    # 表达式形如 ``node_outputs["node_1"].summary`` 或 ``node_outputs["node_2"].confidence > 0.7``
    # 即可访问前驱节点的 summary / confidence / key_findings 等字段。
    eval_context = {
        "node_outputs": state.get("node_outputs", {}),
    }

    matched_branch = None
    match_reason = ""

    for case in cases:
        when_expr = case.get("when", "")
        then_branch = case.get("then")
        if not then_branch:
            continue

        try:
            result = safe_eval(when_expr, eval_context)
            if result:
                matched_branch = then_branch
                match_reason = f"命中 case: {when_expr} -> {then_branch}"
                break
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            # 表达式求值失败：跳过此 case，记录错误信息
            match_reason = f"表达式 {when_expr!r} 求值失败: {exc}"
            continue

    # 所有 case 不命中时走 default
    if matched_branch is None:
        if not default_branch:
            raise ValueError(
                f"condition 节点 {node.id} 所有 case 未命中且未配置 default 分支"
            )
        matched_branch = default_branch
        match_reason = "所有 case 未命中，走 default 分支"

    # 写入节点输出（记录路由决策）并跳转到对应分支
    deliverable = SubAgentDeliverable(
        summary=match_reason,
        raw_text=f"路由到: {matched_branch}",
        confidence=1.0,
    )

    return Command(
        update={"node_outputs": {node.id: deliverable}},
        goto=matched_branch,
    )
