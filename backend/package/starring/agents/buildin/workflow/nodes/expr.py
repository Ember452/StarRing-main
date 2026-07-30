"""工作流节点共享的 {{ expr }} 表达式解析工具。

两种语义（均复用 safe_eval，求值上下文为 {"node_outputs": ...}）：
- resolve_expr: 整串表达式求值（tool 节点 args 语义，非整串按字面量透传）
- render_template: 模板内嵌插值（llm / application-call 的 input_template、
  kb-retrieval 的 query、human-review 的 message）

求值失败一律 fail-fast 抛错，不静默降级为字面量。

设计依据：docs/vibe/P2-工作流工具生态扩展细化设计-20260725.md §2.3
"""

from __future__ import annotations

import re
from typing import Any

from starring.agents.buildin.workflow.nodes.safe_eval import safe_eval
from starring.agents.buildin.workflow.state import WorkflowState

# 仅匹配"整串表达式"（tool 节点 args 语义，与设计 §2.3 约定一致）
_EXPR_PATTERN = re.compile(r"^\s*\{\{(.+)\}\}\s*$", re.DOTALL)

# 模板内嵌插值：匹配文本中每一处 {{ expr }}（非贪婪）
_TEMPLATE_PATTERN = re.compile(r"\{\{(.+?)\}\}", re.DOTALL)


def _eval_context(state: WorkflowState) -> dict[str, Any]:
    """求值上下文与 condition 节点的 when 表达式一致（node_outputs 变量）。"""
    return {"node_outputs": state.get("node_outputs", {})}


def resolve_expr(value: Any, state: WorkflowState) -> Any:
    """整串 {{ expr }} 求值；非字符串或非整串表达式按字面量原样返回。

    求值异常直接上抛（ValueError / TypeError / KeyError / AttributeError），
    由调用方补充节点/参数定位信息。
    """
    if not isinstance(value, str):
        return value
    match = _EXPR_PATTERN.match(value)
    if not match:
        return value
    return safe_eval(match.group(1).strip(), _eval_context(state))


def render_template(text: str, state: WorkflowState, *, where: str = "模板") -> str:
    """模板内嵌插值：替换文本中每一处 {{ expr }} 为求值结果（非 str 结果转 str）。

    不含 {{ }} 的文本原样返回（与字面量行为完全一致）。
    """

    def _replace(match: re.Match) -> str:
        expr = match.group(1).strip()
        try:
            value = safe_eval(expr, _eval_context(state))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError(f"{where}中表达式 {{{{ {expr} }}}} 求值失败: {exc}") from exc
        return value if isinstance(value, str) else str(value)

    return _TEMPLATE_PATTERN.sub(_replace, text)
