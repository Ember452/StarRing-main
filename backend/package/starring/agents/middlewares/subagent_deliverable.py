"""子智能体结构化交付物（deliverable）Pydantic 模型。

用于 Anthropic Orchestrator-Worker 模式的 P0 落地：子智能体被父智能体通过 task
工具调用时，输出结构化 deliverable（摘要 / 关键发现 / 引用来源 / 置信度 / 产物），
替代原本的纯文本回传，让父智能体合成更高质量、编排可复现。

模型设计原则：
- 永远有兜底：解析失败时 raw_text 保留原文，summary 从 raw_text 取首段
- 永远不抛异常：所有字段有默认值，confidence 默认 0.5（中等置信）
- 向后兼容：schema_version 固定 "1"，未来演进只新增字段不删除
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SubAgentSource(BaseModel):
    """子智能体引用来源（知识库 chunk / 文件 / URL / 其他）。"""

    type: Literal["kb_chunk", "file", "url", "other"] = Field(
        default="other",
        description="引用来源类型",
    )
    file_id: str | None = Field(default=None, description="知识库文件 ID（type=kb_chunk/file 时使用）")
    chunk_id: str | None = Field(default=None, description="知识库 chunk ID（type=kb_chunk 时使用）")
    url: str | None = Field(default=None, description="URL（type=url 时使用）")
    snippet: str = Field(default="", description="引用片段文本（用于父智能体快速判断相关性）")


class SubAgentDeliverable(BaseModel):
    """子智能体结构化交付物。

    设计参考：Anthropic Orchestrator-Worker 模式（2025-10-22）
    - 子智能体在 fresh context 中执行，最终输出结构化 deliverable
    - 父智能体基于 deliverable 的结构化字段（而非完整子上下文）进行合成
    - 父的 synthesis 是推理过程，不是拼接子输出
    """

    schema_version: Literal["1"] = Field(
        default="1",
        description="deliverable schema 版本，当前固定为 '1'，未来演进只新增字段不删除",
    )
    summary: str = Field(
        default="",
        description="1-3 句话概括任务结果，供父智能体快速判断是否需要深入引用",
    )
    key_findings: list[str] = Field(
        default_factory=list,
        description="关键发现列表，每条 1-2 句，按重要性排序",
    )
    sources: list[SubAgentSource] = Field(
        default_factory=list,
        description="引用来源列表（知识库 chunk / 文件 / URL）",
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="置信度 0.0-1.0，父智能体合成时可据此降权或忽略",
    )
    raw_text: str = Field(
        default="",
        description="子智能体完整原始文本（兜底用，不渲染到父 ToolMessage，只在 deliverable 状态中保留供前端/Langfuse 查看）",
    )
    artifacts: list[str] = Field(
        default_factory=list,
        description="产物文件路径列表（合并 fenced block 中的 artifacts 和 state.artifacts，去重保序）",
    )

    @model_validator(mode="after")
    def _ensure_summary_fallback(self) -> SubAgentDeliverable:
        """兜底：summary 为空时从 raw_text 取首段，保证父智能体永远拿到非空 summary。"""
        if not self.summary.strip() and self.raw_text.strip():
            # 取首段非空文本（按空行分隔），截断到 200 字符
            first_paragraph = next(
                (p.strip() for p in self.raw_text.split("\n\n") if p.strip()),
                "",
            )
            if first_paragraph:
                self.summary = first_paragraph[:200] + ("..." if len(first_paragraph) > 200 else "")
        return self


# 用于 _parse_deliverable 完全无输出兜底的常量实例
EMPTY_DELIVERABLE = SubAgentDeliverable(
    summary="子智能体已完成任务，但未返回结构化结果。",
    raw_text="",
)
