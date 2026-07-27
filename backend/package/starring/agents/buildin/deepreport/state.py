"""DeepReport 报告流水线状态与数据模型。

数据流契约（五阶段流水线）：
- plan 写入 outline（Outline，LLM 结构化输出）
- review 写入 review_feedback / replan_count（interrupt 人工评审结果）
- research_chapter（Send 并行分支）各自写入 chapters（merge_chapters reducer 按 chapter_id 合并）
- synthesize 写入 report_md + sources（全局重编号后的引用来源）
- citation_check 覆写 report_md（附引用来源章节）并写入 citation_report

引用来源结构复用 SubAgentSource（kb_chunk / file / url / other），
与 task 子智能体 deliverable 契约保持一致，便于前端与评测脚本统一处理。
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain.agents import AgentState
from pydantic import BaseModel, Field

from starring.agents.middlewares.subagent_deliverable import SubAgentSource


class OutlineChapter(BaseModel):
    """大纲中的单个章节。"""

    id: str = Field(default="", description="章节 ID（流水线内部使用，如 ch-1）")
    heading: str = Field(description="章节标题")
    brief: str = Field(default="", description="章节写作要点（1-3 句，指导研究员检索方向）")


class Outline(BaseModel):
    """报告大纲（plan 节点的 LLM 结构化输出）。"""

    title: str = Field(description="报告标题")
    chapters: list[OutlineChapter] = Field(default_factory=list, description="章节列表，按阅读顺序排列")


class ChapterFact(BaseModel):
    """研究阶段收集的单条事实（写作阶段的唯一信息来源）。"""

    statement: str = Field(description="事实陈述（1-3 句，忠实于知识库原文，不做推断）")
    source: SubAgentSource = Field(
        default_factory=SubAgentSource,
        description="事实的引用来源（kb_chunk 时必须带 file_id，snippet 保留原文片段）",
    )


class ChapterResearch(BaseModel):
    """研究员子图的结构化交付物：某章节的事实清单。"""

    facts: list[ChapterFact] = Field(default_factory=list, description="事实清单，按与章节主题的相关性排序")
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="研究员自评：事实清单对章节写作要点的覆盖程度（0-1，检索结果贴合度低时如实给低分）",
    )


class ReportFraming(BaseModel):
    """合成阶段的 LLM 结构化输出：报告引言与结论（正文由确定性拼接负责，不经 LLM）。"""

    introduction: str = Field(default="", description="报告引言（100-200 字，不含 [S#] 引用标记）")
    conclusion: str = Field(default="", description="报告结论（100-250 字，不含 [S#] 引用标记）")


class ChapterResult(BaseModel):
    """单章节产出（research_chapter 分支写回主状态的数据契约）。"""

    chapter_id: str = Field(description="对应 OutlineChapter.id")
    heading: str = Field(default="", description="章节标题")
    content_md: str = Field(default="", description="章节正文 markdown，引用只允许 [S#] 标记（章节内局部编号）")
    sources: list[SubAgentSource] = Field(
        default_factory=list,
        description="章节引用来源列表，[S#] 的 # 即列表下标 +1",
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="置信度，失败占位章节为 0")


def merge_chapters(
    existing: list[ChapterResult] | None,
    new: list[ChapterResult] | None,
) -> list[ChapterResult]:
    """合并 Send 并行分支写回的章节结果（按 chapter_id 去重，新结果覆盖旧结果）。"""
    merged: dict[str, ChapterResult] = {}
    for item in (existing or []) + (new or []):
        merged[item.chapter_id] = item
    return list(merged.values())


class DeepReportState(AgentState):
    """DeepReport 流水线执行状态。

    - messages: 标准消息列表（用户 query 输入，citation_check 合成最终 AIMessage 写入）
    - outline: 当前大纲（plan 节点写入，review 反馈后重新生成）
    - review_feedback: 用户修改意见（非空表示需要回 plan 重新规划）
    - replan_count: 已重新规划次数（超过 context.max_replan 后直接采用当前大纲）
    - chapters: 各章节研究/写作结果（Send 分支通过 reducer 聚合）
    - report_md: 最终报告 markdown
    - sources: 全局重编号后的引用来源列表（synthesize 写入）
    - citation_report: 引用回验结果（citation_check 写入）
    """

    outline: Outline | None
    review_feedback: str
    replan_count: int
    chapters: Annotated[list[ChapterResult], merge_chapters]
    report_md: str
    sources: list[SubAgentSource]
    citation_report: dict[str, Any]
