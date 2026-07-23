"""ChatbotAgent backend 实现（Orchestrator-Worker 模式主入口）。

ChatbotAgent 是 StarRing 三种 backend 范式之一，与 SupervisorAgent（软编排）和
WorkflowBackend（硬编排）并列。其核心定位是「LLM 自主路由的通用对话智能体」：

- 本地工具（KB 检索 / Skills / 文件系统）与 task 工具（子智能体委派）并挂载
- LLM 根据用户问题自主决定：直接调用本地工具 / 委派子智能体 / 综合多源结果
- 上下文压缩、TodoList 跟踪、PatchToolCalls、ModelRetry、TokenUsage 全套中间件

设计依据：docs/vibe/P0-1-Orchestrator-Worker-子智能体结构化交付物-20260719.md
"""
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
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
from starring.agents.middlewares.knowledge_base import KnowledgeBaseMiddleware
from starring.agents.middlewares.skills import SkillsMiddleware
from starring.agents.middlewares.subagent_task import create_subagent_task_middleware
from starring.agents.toolkits.service import resolve_configured_runtime_tools

from .context import ChatBotContext
from .prompt import KB_FORCE_PROMPT, TODO_MID_PROMPT, build_prompt_with_context
from .state import ChatBotState


async def _build_middlewares(context):
    """构建 ChatbotAgent 的中间件栈。

    顺序敏感（前者先 wrap 后者），自外向内大致为：
    文件系统 → 附件保存 → KB（可选）→ Skills → 子智能体 task → 摘要压缩
    → TodoList → PatchToolCalls → ModelRetry → TokenUsage。

    KB 中间件采用「显式关闭才不挂载」策略：context.use_knowledge 为 None 或 True
    均挂载，仅 False 跳过，保持默认行为兼容历史调用方。
    """
    # summary middleware
    # 主 Agent 上下文优化：默认 100k tokens 触发压缩，压缩后保留最近 10 条消息
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

    # 外层中间件：文件系统（带 token 限流 eviction）+ 附件持久化
    middlewares = [
        create_agent_filesystem_middleware(
            getattr(context, "tool_token_limit", DEFAULT_TOOL_RESULT_EVICTION_K_TOKENS) * 1024,
            context=context,
        ),
        save_attachments_to_fs,
    ]
    # 对话级“知识库问答”开关：仅当显式关闭（False）时不挂载知识库工具；
    # None（默认）或 True 均挂载，保持其他调用方行为不变。
    if getattr(context, "use_knowledge", None) is not False:
        middlewares.append(KnowledgeBaseMiddleware())
    # Skills 工具自动发现：从已注册 toolkit 集合中挂载可用工具
    middlewares.append(SkillsMiddleware())
    # Orchestrator-Worker 子智能体 task 工具：未配置子智能体时返回 None 跳过
    subagent_middleware = await create_subagent_task_middleware(context)
    if subagent_middleware:
        middlewares.append(subagent_middleware)
    # 内层中间件：摘要压缩 → TodoList → PatchToolCalls → ModelRetry → TokenUsage
    middlewares.extend(
        [
            summary_middleware,
            TodoListMiddleware(system_prompt=TODO_MID_PROMPT),
            *( [PatchToolCallsMiddleware()] if PatchToolCallsMiddleware is not None else [] ),
            ModelRetryMiddleware(max_retries=getattr(context, "model_retry_times", 2)),
            TokenUsageMiddleware(),
        ]
    )
    return middlewares


class ChatbotAgent(BaseAgent):
    """Orchestrator-Worker 主智能体 backend。

    定位：通用对话智能体，挂载本地工具 + task 子智能体工具，LLM 自主决定路由。
    被自动发现注册到 agent_manager，由 chat_service / agent_run_service 调用 get_graph() 编译。
    """
    name = "智能助手"
    description = "基础的对话机器人，可以回答问题，可在配置中启用需要的工具。"
    capabilities = ["file_upload", "files"]  # 支持文件上传功能
    context_schema = ChatBotContext

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def get_graph(self, context=None, **kwargs):
        """编译并返回 ChatbotAgent 的 LangGraph 图。

        流程：解析 context → 加载模型 → 构造 system prompt（KB 模式时追加强制检索提示）
        → 解析运行时工具 → 构造 middleware 栈 → 调用 create_agent 编译图。
        checkpointer 由 ``self._get_checkpointer`` 提供，保证多轮对话记忆持久化。
        """
        context = await prepare_agent_runtime_context(
            context or self.context_schema(),
            context_schema=self.context_schema,
        )

        # 使用 create_agent 创建智能体
        model_spec = resolve_chat_model_spec(context.model)
        system_prompt = build_prompt_with_context(context)
        # “知识库问答”模式（use_knowledge 为 True）：追加强制检索提示词
        if getattr(context, "use_knowledge", None) is True:
            system_prompt = f"{system_prompt}\n{KB_FORCE_PROMPT}"
        graph = create_agent(
            # TODO：警告 Unexpected type
            model=load_chat_model(fully_specified_name=model_spec), # 智能体使用的大语言模型
            tools=await resolve_configured_runtime_tools(context), # 智能体使用的工具
            system_prompt=system_prompt, # 智能体使用的系统提示
            middleware=await _build_middlewares(context), # 智能体使用的中间件
            state_schema=ChatBotState,  # 图中流转的全局状态的数据结构
            checkpointer=await self._get_checkpointer(), # 传入检查点保存器，让智能体拥有记忆
        )

        return graph


def main():
    pass


if __name__ == "__main__":
    main()
    # asyncio.run(main())
