"""Supervisor 智能体 backend 实现。

与 ChatbotAgent 的核心差异：
1. 不挂载本地工具（tools=[]）：supervisor 不直接调用 KB / Skills / 文件系统工具
2. 不挂载 KnowledgeBaseMiddleware / SkillsMiddleware：supervisor 不直接执行任务
3. 强制挂载 StarRingSubAgentMiddleware：即使 subagents 为空也要求至少 1 个子 agent，
   否则 graph 构建失败（fail-fast，符合 AGENTS.md「良好的软件应该在预设的条件下运行」原则）
4. 使用 SUPERVISOR_SYSTEM_PROMPT：明确「必须委派」语义
5. 复用 ChatBotState（含 subagent_runs 字段，回流子智能体状态）
6. 复用 BaseAgent._get_checkpointer()：状态持久化到 Postgres / SQLite

设计依据：docs/vibe/P1-A-Supervisor细化设计-20260719.md §三
"""
from __future__ import annotations

try:
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
except ImportError:
    PatchToolCallsMiddleware = None  # type: ignore[assignment,misc]
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRetryMiddleware, TodoListMiddleware

from starring.agents import BaseAgent, load_chat_model, resolve_chat_model_spec
from starring.agents.backends import create_agent_filesystem_middleware
from starring.agents.context import (
    DEFAULT_SUMMARY_KEEP_MESSAGES,
    DEFAULT_SUMMARY_THRESHOLD_K,
    DEFAULT_SUMMARY_TOOL_RESULT_TOKEN_LIMIT,
    DEFAULT_TOOL_RESULT_EVICTION_K_TOKENS,
    DEFAULT_STARRING_SUMMARY_PROMPT,
    prepare_agent_runtime_context,
)
from starring.agents.middlewares import (
    TokenUsageMiddleware,
    create_summary_middleware,
    save_attachments_to_fs,
)
from starring.agents.middlewares.subagent_task import create_subagent_task_middleware
from starring.agents.buildin.chatbot.prompt import TODO_MID_PROMPT
from starring.agents.buildin.chatbot.state import ChatBotState
from starring.utils.datetime_utils import shanghai_now

from .context import SupervisorContext
from .prompt import build_supervisor_prompt


async def _build_supervisor_middlewares(context: SupervisorContext):
    """构建 Supervisor 专用 middleware 栈。

    与 ChatbotAgent._build_middlewares 的差异：
    - 移除 KnowledgeBaseMiddleware（supervisor 不直接检索 KB）
    - 移除 SkillsMiddleware（supervisor 不直接执行 Skills）
    - 强制挂载 StarRingSubAgentMiddleware（返回 None 时抛 ValueError）
    - 保留 summary / TodoList / PatchToolCalls / ModelRetry / TokenUsage
    """
    summary_trigger_tokens = getattr(context, "summary_threshold", DEFAULT_SUMMARY_THRESHOLD_K) * 1024
    summary_keep_messages = getattr(context, "summary_keep_messages", DEFAULT_SUMMARY_KEEP_MESSAGES)
    summary_prompt = getattr(context, "summary_prompt", None) or DEFAULT_STARRING_SUMMARY_PROMPT
    summary_tool_result_token_limit = getattr(
        context,
        "summary_tool_result_token_limit",
        DEFAULT_SUMMARY_TOOL_RESULT_TOKEN_LIMIT,
    )
    model_spec = resolve_chat_model_spec(context.model)
    summary_middleware = create_summary_middleware(
        model=load_chat_model(fully_specified_name=model_spec),
        trigger=("tokens", summary_trigger_tokens),
        keep=("messages", summary_keep_messages),
        summary_prompt=summary_prompt,
        trim_tokens_to_summarize=4000,
        tool_result_offload_token_limit=summary_tool_result_token_limit,
    )

    middlewares = [
        create_agent_filesystem_middleware(
            getattr(context, "tool_token_limit", DEFAULT_TOOL_RESULT_EVICTION_K_TOKENS) * 1024,
            context=context,
        ),
        save_attachments_to_fs,
    ]
    # Supervisor 不挂载 KnowledgeBaseMiddleware / SkillsMiddleware（不直接执行任务）
    subagent_middleware = await create_subagent_task_middleware(context)
    if subagent_middleware is None:
        raise ValueError(
            "Supervisor 智能体要求至少配置 1 个可用的子智能体，"
            "请在 Agent 配置中显式指定 subagents 字段，或确保当前用户可见范围内存在 SubAgent 类型的子智能体。"
        )
    middlewares.append(subagent_middleware)
    middlewares.extend(
        [
            summary_middleware,
            TodoListMiddleware(system_prompt=TODO_MID_PROMPT),
            *([PatchToolCallsMiddleware()] if PatchToolCallsMiddleware is not None else []),
            ModelRetryMiddleware(max_retries=getattr(context, "model_retry_times", 2)),
            TokenUsageMiddleware(),
        ]
    )
    return middlewares


def _build_supervisor_system_prompt(context: SupervisorContext) -> str:
    """构造 supervisor 系统提示词。

    合并：
    - 当前日期
    - SUPERVISOR_SYSTEM_PROMPT（角色 + 约束 + 合成规则）
    - context.system_prompt（用户自定义补充提示，可选）

    不包含「Available subagent types」段落（由 StarRingSubAgentMiddleware 注入）。
    """
    current_date = f"当前日期：{shanghai_now().strftime('%Y-%m-%d')}"
    user_extra = context.system_prompt or ""
    parts = [current_date, build_supervisor_prompt()]
    if user_extra.strip():
        parts.append(user_extra.strip())
    return "\n\n".join(parts)


class SupervisorAgent(BaseAgent):
    """Supervisor 智能体：强制通过 task 工具委派给子智能体。

    与 ChatbotAgent 的角色边界（见 docs/vibe/P1-A-Supervisor细化设计-20260719.md §二）：
    - ChatbotAgent：本地工具 + task 工具，LLM 自主决定是否委派（Orchestrator-Worker）
    - SupervisorAgent：仅 task 工具，强制委派（Supervisor 模式）

    典型场景：多角色协作（写作 + 审稿 + 翻译）
    """
    name = "Supervisor 智能体"
    description = "多角色协作编排器，强制通过 task 工具委派给子智能体。适用于写作+审稿、翻译+校对等显式角色协作场景。"
    capabilities = ["file_upload", "files"]
    context_schema = SupervisorContext

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def get_graph(self, context=None, **kwargs):
        context = await prepare_agent_runtime_context(
            context or self.context_schema(),
            context_schema=self.context_schema,
        )

        model_spec = resolve_chat_model_spec(context.model)
        system_prompt = _build_supervisor_system_prompt(context)
        # Supervisor 不挂载本地工具：tools 传空列表，LLM 只能通过 task 工具委派
        graph = create_agent(
            model=load_chat_model(fully_specified_name=model_spec),
            tools=[],
            system_prompt=system_prompt,
            middleware=await _build_supervisor_middlewares(context),
            state_schema=ChatBotState,
            checkpointer=await self._get_checkpointer(),
        )
        return graph
