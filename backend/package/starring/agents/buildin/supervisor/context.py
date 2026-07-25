"""Supervisor 智能体上下文。

沿用 ChatBotContext 的字段（subagents / use_knowledge），
通过 backend 的 prompt + middleware 配置差异实现 supervisor 语义，
不新增字段以保持 context schema 兼容，前端可复用 ChatbotAgent 配置 UI。
"""

from dataclasses import dataclass

from starring.agents.buildin.chatbot.context import ChatBotContext


@dataclass(kw_only=True)
class SupervisorContext(ChatBotContext):
    """Supervisor 模式上下文。

    与 ChatBotContext 字段完全相同，差异在 backend 实现：
    - 不挂载本地工具（resolve_configured_runtime_tools 不调用）
    - 强制挂载 task 工具（即使 subagents 为空也要求至少 1 个子 agent）
    - prompt 明确「必须通过 task 工具委派」语义
    """

    pass
