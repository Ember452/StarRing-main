from dataclasses import dataclass, field

from starring.agents.context import BaseContext


@dataclass(kw_only=True) # 表示所有的字段都要用关键字传参，不能按位传参
class ChatBotContext(BaseContext): # 继承自 BaseContext，说明这是一个配置类
    """
    用于配置和管理ChatBot上下文，特别是子智能体的控制和启用
    """
    subagents: list[str] | None = field(
        default=None,
        metadata={
            "name": "子智能体",
            "options": [],
            "description": "可选子智能体列表，为空表示启用当前用户可见的全部子智能体。",
            "type": "list",
            "kind": "subagents",
        },
    )

    # 对话级“知识库问答”开关，由前端每次对话通过 meta 下发，不属于智能体静态配置：
    #   - None（默认/其他调用方）：挂载知识库工具，不强制检索（保持原有行为）
    #   - True（知识库问答）：挂载知识库工具 + 回答前强制先检索知识库
    #   - False（普通问答）：不挂载知识库工具，完全不用知识库
    use_knowledge: bool | None = field(
        default=None,
        metadata={
            "name": "知识库问答",
            "configurable": False,
            "hide": True,
            "description": "对话级开关：True 强制基于知识库回答，False 不用知识库。",
        },
    )
