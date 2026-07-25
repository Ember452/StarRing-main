"""智能体运行时上下文解析服务。

把 HTTP 请求层的 ``agent_id`` / ``thread_id`` 解析为 ``BaseContext`` 实例，
供文件预览、审批恢复等非对话路径复用（对话路径在 ``chat_service`` 内解析）。
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starring.agents.buildin import agent_manager
from starring.agents.context import BaseContext, normalize_agent_context_config, prepare_agent_runtime_context
from starring.repositories.agent_repository import AgentRepository
from starring.repositories.conversation_repository import ConversationRepository
from starring.services.conversation_service import require_user_conversation
from starring.storage.postgres.models_business import User


async def resolve_agent_runtime_context(
    *,
    db: AsyncSession,
    user: User,
    bound_agent_id: str,
) -> BaseContext:
    """根据 agent slug 解析运行时上下文：加载智能体配置 → 归一化 context 字段 → 返回 BaseContext。

    WorkflowBackend 集成：若 context 携带 ``workflow_id`` 字段且未显式配置，
    用 agent slug 作为查找依据（约定 ``workflows.slug == agents.slug``）。
    通过 ``hasattr`` 检测字段存在性，不引入对 WorkflowBackend 的硬耦合。
    """
    agent_item = await AgentRepository(db).get_visible_by_slug(slug=bound_agent_id, user=user)
    if not agent_item:
        raise HTTPException(status_code=404, detail="智能体不存在")

    backend = agent_manager.get_agent(agent_item.backend_id)
    if not backend:
        raise HTTPException(status_code=404, detail="智能体后端不存在")

    context_schema = backend.context_schema
    context = context_schema(thread_id="", uid=str(user.uid))
    normalized_config = await normalize_agent_context_config(
        (agent_item.config_json or {}).get("context", {}),
        db=db,
        user=user,
        context_schema=context_schema,
    )
    context.update_from_dict(normalized_config)

    # WorkflowBackend 集成：若 context 携带 workflow_id 字段且未显式配置，
    # 用 agent slug 作为 workflow_id 查找依据（约定 workflows.slug == agents.slug）。
    # 该机制通过 hasattr 检测字段存在性，不引入对 WorkflowBackend 的硬耦合。
    if hasattr(context, "workflow_id") and not getattr(context, "workflow_id", None):
        context.workflow_id = agent_item.slug

    return context


async def resolve_thread_agent_runtime_context(
    *,
    thread_id: str,
    user: User,
    db: AsyncSession,
) -> BaseContext:
    """根据线程绑定的 agent 解析运行时上下文（文件预览/审批恢复等线程相关场景入口）。

    与 ``resolve_agent_runtime_context`` 的差异：从线程查 ``agent_id`` 并回填
    ``thread_id``/``uid``，再调 ``prepare_agent_runtime_context`` 完成知识库/工具等
    异步依赖加载。
    """
    conv_repo = ConversationRepository(db)
    conversation = await require_user_conversation(conv_repo, thread_id, str(user.uid))

    runtime_context = await resolve_agent_runtime_context(
        db=db,
        user=user,
        bound_agent_id=conversation.agent_id,
    )
    runtime_context.thread_id = thread_id
    runtime_context.uid = str(user.uid)
    await prepare_agent_runtime_context(runtime_context)
    return runtime_context
