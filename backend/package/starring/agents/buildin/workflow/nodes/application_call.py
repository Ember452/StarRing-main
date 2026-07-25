"""application-call 节点执行器：嵌套调用其他 agent 作为子任务。

与 P0-1 task 工具能力对齐，但走确定性路径（不依赖 LLM 自主决定委派）。
调用目标 agent 后，将其输出解析为 SubAgentDeliverable。

目标智能体按 agents.slug 查库解析（与 chat_service._resolve_agent_runtime 同款链路），
用户自定义智能体的 config_json.context 配置（提示词 / 工具 / MCP / Skill / 知识库）全部生效。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §五.2、§四.2；
         docs/vibe/P2-工作流工具生态扩展细化设计-20260725.md §三
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

from langchain_core.messages import AIMessage, HumanMessage

from starring.agents.buildin import agent_manager
from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.buildin.workflow.nodes import register_node
from starring.agents.buildin.workflow.nodes.llm import _build_node_input
from starring.agents.buildin.workflow.state import WorkflowState
from starring.agents.middlewares.subagent_task import (
    _parse_deliverable,
)


@register_node("application-call")
async def execute_application_call(state: WorkflowState, node: Node, context: WorkflowContext) -> dict:
    """嵌套调用 agent 节点执行器。

    config 字段:
        target_agent_slug: 目标智能体的 slug（必填，agents.slug）
        input_template: 输入模板（可选，Phase 1 简化为前置文本）
    """
    config = node.config
    target_slug = config["target_agent_slug"]

    # 构造输入：拼接上游 summary + 模板
    user_input = _build_node_input(state, node.id)
    input_template = config.get("input_template")
    if input_template:
        user_input = f"{input_template}\n\n{user_input}"

    # 按 agents.slug 查库解析目标智能体（延迟导入避免循环依赖），
    # 权限主体为工作流 owner（context.uid）：只能调用其可见的智能体。
    from starring.agents.context import normalize_agent_context_config
    from starring.repositories.agent_repository import AgentRepository
    from starring.repositories.user_repository import UserRepository
    from starring.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as db:
        user = await UserRepository().get_by_uid_with_db(db, str(context.uid))
        if user is None:
            raise ValueError(f"application-call 节点 {node.id} 无法加载工作流 owner（uid={context.uid!r}）")
        agent_item = await AgentRepository(db).get_visible_by_slug(slug=target_slug, user=user)
        if agent_item is None:
            raise ValueError(
                f"application-call 节点 {node.id} 的目标智能体 {target_slug!r} 不存在或无权限访问；"
                f"target_agent_slug 应为智能体 slug（不再支持 backend 类名，存量配置请改为智能体 slug）"
            )

        if agent_item.backend_id not in agent_manager._classes:
            raise ValueError(
                f"application-call 节点 {node.id} 的目标智能体 {target_slug!r} 的后端 {agent_item.backend_id!r} 未注册"
            )
        target_agent = agent_manager.get_agent(agent_item.backend_id)

        # 嵌套深度限制 = 1：目标 backend 是工作流（context_schema 含 workflow_id 字段）时拒绝，
        # 与 chat_service 的字段探测方式一致，不硬编码 WorkflowBackend 类名
        schema_field_names = {f.name for f in dataclass_fields(target_agent.context_schema)}
        if "workflow_id" in schema_field_names:
            raise ValueError(
                f"application-call 节点 {node.id} 的目标智能体 {target_slug!r} 是工作流类智能体，"
                f"工作流不允许嵌套调用工作流"
            )

        # 注入智能体的 config_json.context 配置（system_prompt / tools / mcps / skills / knowledges 等）
        agent_config = await normalize_agent_context_config(
            (agent_item.config_json or {}).get("context", {}),
            db=db,
            user=user,
            context_schema=target_agent.context_schema,
        )

    # 调用目标 agent（同步执行：ainvoke 单次输入）：先注入用户配置，运行时字段最后覆盖
    target_context = target_agent.context_schema()
    target_context.update_from_dict(agent_config)
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

    deliverable = _parse_deliverable([AIMessage(content=final_text)], [])
    # 合并子 agent 产生的 artifacts
    child_artifacts = list(result.get("artifacts", []) or [])
    if child_artifacts:
        deliverable.artifacts = list(dict.fromkeys(deliverable.artifacts + child_artifacts))

    return {"node_outputs": {node.id: deliverable}}
