"""DeepReport 智能体上下文配置。

DeepReportContext 继承 BaseContext（复用 model / knowledges / system_prompt /
summary 等标准配置），额外提供报告流水线专属参数：

- max_chapters: 大纲最大章节数（超出截断，控制 Send 并行分支数量）
- max_replan: 大纲人工评审阶段的最大重新规划次数（一次性闸门，防死循环）
"""

from dataclasses import dataclass, field

from starring.agents.context import BaseContext

DEFAULT_MAX_CHAPTERS = 8
DEFAULT_MAX_REPLAN = 2


@dataclass(kw_only=True)
class DeepReportContext(BaseContext):
    """DeepReport 知识库报告流水线的可配置上下文。"""

    max_chapters: int = field(
        default=DEFAULT_MAX_CHAPTERS,
        metadata={
            "name": "最大章节数",
            "description": f"报告大纲允许的最大章节数，超出部分会被截断，默认 {DEFAULT_MAX_CHAPTERS}。",
            "type": "number",
        },
    )

    max_replan: int = field(
        default=DEFAULT_MAX_REPLAN,
        metadata={
            "name": "最大重新规划次数",
            "description": (
                "大纲评审阶段允许用户提出修改意见并重新生成大纲的最大次数，"
                f"超过后直接采用当前大纲继续执行（防死循环），默认 {DEFAULT_MAX_REPLAN}。"
            ),
            "type": "number",
        },
    )
