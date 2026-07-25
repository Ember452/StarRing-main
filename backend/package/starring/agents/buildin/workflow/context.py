"""工作流上下文。

WorkflowContext 沿用 BaseContext 字段，新增 workflow_id / workflow_version
用于在 get_graph() 阶段加载持久化的工作流定义。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §八
"""

from __future__ import annotations

from dataclasses import dataclass, field

from starring.agents.context import BaseContext


@dataclass(kw_only=True)
class WorkflowContext(BaseContext):
    """工作流 backend 上下文。

    字段说明：
    - workflow_id: 工作流 ID（UUID），运行时从 agents.slug 派生并加载 workflows 表
    - workflow_version: 工作流版本号（运行时快照）
    - definition: 工作流定义缓存（运行时加载，避免重复查库）
    """

    workflow_id: str | None = field(
        default=None,
        metadata={"name": "工作流 ID", "configurable": False, "hide": True},
    )
    workflow_version: int = field(
        default=0,
        metadata={"name": "工作流版本", "configurable": False, "hide": True},
    )
