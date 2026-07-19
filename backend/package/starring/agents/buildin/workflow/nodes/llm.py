"""llm 节点执行器：LLM 推理节点。

加载 LLM（复用 ChatbotAgent 的模型加载链路），构造输入（拼接上游 summary），
调用 LLM 并解析输出为 SubAgentDeliverable。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §五.2
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from starring.agents import load_chat_model, resolve_chat_model_spec
from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.buildin.workflow.nodes import register_node
from starring.agents.buildin.workflow.state import WorkflowState
from starring.agents.middlewares.subagent_deliverable import (
    SubAgentDeliverable,
    _parse_deliverable,
)


def _build_node_input(state: WorkflowState, node_id: str) -> str:
    """构造节点输入文本：拼接所有上游节点的 summary。"""
    node_outputs = state.get("node_outputs", {})
    # 上游节点：通过 state 中的 edges 隐含关系推导
    # 由于执行器无法直接访问 definition.edges，这里采用「所有非自己节点」作为输入
    # WorkflowBackend 在编译 StateGraph 时通过条件边保证只有上游节点先执行
    upstream_summaries = []
    for nid, deliverable in node_outputs.items():
        if nid == node_id:
            continue
        if deliverable.summary.strip():
            upstream_summaries.append(f"[来自节点 {nid}]: {deliverable.summary}")

    return "\n\n".join(upstream_summaries) if upstream_summaries else "(无上游输入)"


@register_node("llm")
async def execute_llm(
    state: WorkflowState, node: Node, context: WorkflowContext
) -> dict:
    """LLM 推理节点执行器。

    config 字段:
        model: 模型规格（如 "openai:gpt-4"），空则用 context.model
        system_prompt: 系统提示词（必填）
        input_template: 输入模板（可选，含 ${node_outputs['xxx'].summary} 占位符，未实现 Phase 1）
    """
    config = node.config
    system_prompt = config["system_prompt"]

    # 模型选择：节点 config.model 优先，否则用 context.model
    model_spec = config.get("model") or context.model
    if not model_spec:
        raise ValueError(
            f"llm 节点 {node.id} 未配置 model，且 context.model 为空"
        )

    # 构造输入
    user_input = _build_node_input(state, node.id)
    input_template = config.get("input_template")
    if input_template:
        # Phase 1 简化：模板直接拼接在用户输入前（不做 ${...} 替换）
        user_input = f"{input_template}\n\n{user_input}"

    # 加载 LLM 并调用
    model = load_chat_model(fully_specified_name=resolve_chat_model_spec(model_spec))
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_input),
    ]
    response = await model.ainvoke(messages)

    # 解析输出为 SubAgentDeliverable
    raw_text = response.content if isinstance(response.content, str) else str(response.content)
    deliverable = _parse_deliverable(raw_text)

    return {"node_outputs": {node.id: deliverable}}
