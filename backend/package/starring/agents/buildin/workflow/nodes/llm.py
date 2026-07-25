"""llm 节点执行器：LLM 推理节点。

加载 LLM（复用 ChatbotAgent 的模型加载链路），构造输入（拼接上游 summary），
调用 LLM 并解析输出为 SubAgentDeliverable。

配置 tools / mcps 后，节点内部变为受限步数的 ReAct 循环（create_agent）；
未配置时保持裸 ainvoke 路径不变（向后兼容）。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §五.2；
         docs/vibe/P2-工作流工具生态扩展细化设计-20260725.md §五
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from starring.agents import load_chat_model, resolve_chat_model_spec
from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.buildin.workflow.nodes import register_node
from starring.agents.buildin.workflow.state import WorkflowState
from starring.agents.middlewares.subagent_task import (
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
async def execute_llm(state: WorkflowState, node: Node, context: WorkflowContext) -> dict:
    """LLM 推理节点执行器。

    config 字段:
        model: 模型规格（如 "openai:gpt-4"），空则用 context.model
        system_prompt: 系统提示词（必填）
        input_template: 输入模板（可选，含 ${node_outputs['xxx'].summary} 占位符，未实现 Phase 1）
        tools: 内置工具名列表（可选，P2 新增）
        mcps: MCP 服务器 slug 列表（可选，P2 新增）
        max_tool_steps: ReAct 循环最大步数（可选，默认 10，上限 25）
    """
    config = node.config
    system_prompt = config["system_prompt"]

    # 模型选择：节点 config.model 优先，否则用 context.model
    model_spec = config.get("model") or context.model
    if not model_spec:
        raise ValueError(f"llm 节点 {node.id} 未配置 model，且 context.model 为空")

    # 构造输入
    user_input = _build_node_input(state, node.id)
    input_template = config.get("input_template")
    if input_template:
        # Phase 1 简化：模板直接拼接在用户输入前（不做 ${...} 替换）
        user_input = f"{input_template}\n\n{user_input}"

    model = load_chat_model(fully_specified_name=resolve_chat_model_spec(model_spec))
    tools_cfg = config.get("tools") or []
    mcps_cfg = config.get("mcps") or []

    if not tools_cfg and not mcps_cfg:
        # 无工具配置：裸 ainvoke（与 P1-B 行为完全一致）
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_input),
        ]
        response = await model.ainvoke(messages)
        raw_text = response.content if isinstance(response.content, str) else str(response.content)
    else:
        # 挂工具：节点内 mini-agent（ReAct 循环），不挂 middleware / checkpointer，
        # 保持 fresh context per node 不变量。延迟导入避免模块加载时触发工具注册表初始化。
        from langchain.agents import create_agent

        from starring.agents.toolkits.service import resolve_configured_runtime_tools

        runtime_tools = await resolve_configured_runtime_tools(SimpleNamespace(tools=tools_cfg, mcps=mcps_cfg))
        # 用户显式配置了工具就应生效：解析为空（如 MCP 全部不可用）时 fail-fast，
        # 不静默退化为裸推理，避免掩盖 MCP 故障
        if not runtime_tools:
            raise ValueError(
                f"llm 节点 {node.id} 配置的工具解析为空（tools={tools_cfg}, mcps={mcps_cfg}），"
                f"请检查工具名与 MCP 服务器状态"
            )

        max_tool_steps = config.get("max_tool_steps") or 10
        agent = create_agent(
            model=model,
            tools=runtime_tools,
            system_prompt=system_prompt,
        )
        # 每步 = 1 次模型调用 + 1 次工具调用，防止节点内死循环拖垮整个工作流 run
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_input)]},
            config={"recursion_limit": max_tool_steps * 2 + 1},
        )
        raw_text = ""
        for msg in reversed(result.get("messages", [])):
            if isinstance(msg, AIMessage) and msg.content:
                raw_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

    deliverable = _parse_deliverable([AIMessage(content=raw_text)], [])

    return {"node_outputs": {node.id: deliverable}}
