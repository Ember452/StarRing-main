"""DeepReport 知识库报告流水线 backend。

DeepReportAgent 继承 BaseAgent，手工组装固定 StateGraph 五阶段流水线
（非 prompt 驱动），被 auto_discover_agents 自动发现注册，前端零改动：
- backend 列表由 get_agents_info 自动带出
- review 阶段 interrupt 复用 ask_user_question 的渲染契约

图结构：
START → plan（大纲）→ review（interrupt 人工确认）
      → (条件边) → [Send("research_chapter") × N 章节并行]
      → synthesize（压缩合成 + 确定性降级）→ citation_check（引用回验）→ END
"""

from __future__ import annotations

from functools import partial

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from starring.agents import BaseAgent
from starring.agents.buildin.deepreport.context import DeepReportContext
from starring.agents.buildin.deepreport.nodes import (
    citation_check_node,
    plan_node,
    research_chapter_node,
    review_node,
    route_after_review,
    synthesize_node,
)
from starring.agents.buildin.deepreport.state import DeepReportState
from starring.utils import logger


class DeepReportAgent(BaseAgent):
    """DeepReport 智能体：面向大知识库的长文报告生成流水线。

    与 ChatbotAgent（LLM 自主路由）、SupervisorAgent（强制委派）、
    WorkflowBackend（用户自定义流程）不同，DeepReport 是代码固定的
    领域流水线：大纲规划 → 人工评审 → 章节并行研究/写作（双 LLM 分离防幻觉）
    → 合成 → 引用回验，产出带可验证引用的结构化报告。
    """

    name = "DeepReport 智能体"
    description = (
        "知识库深度报告流水线：基于知识库思维导图规划大纲，人工确认后按章节并行研究与写作，"
        "全程引用可回验。适用于大知识库的长文调研报告、综述、合规审查报告等场景。"
    )
    capabilities: list[str] = []
    context_schema = DeepReportContext

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def get_graph(self, context=None, **kwargs) -> CompiledStateGraph:
        """组装五阶段 StateGraph（每次调用按 context 重新编译，同 workflow backend）。"""
        from starring.agents.context import prepare_agent_runtime_context

        ctx = await prepare_agent_runtime_context(
            context or self.context_schema(),
            context_schema=self.context_schema,
        )
        logger.info(f"DeepReportAgent 编译流水线: max_chapters={ctx.max_chapters}, max_replan={ctx.max_replan}")

        builder: StateGraph = StateGraph(DeepReportState)
        builder.add_node("plan", partial(plan_node, ctx))
        builder.add_node("review", partial(review_node, ctx))
        builder.add_node("research_chapter", partial(research_chapter_node, ctx))
        builder.add_node("synthesize", partial(synthesize_node, ctx))
        builder.add_node("citation_check", partial(citation_check_node, ctx))

        builder.add_edge(START, "plan")
        builder.add_edge("plan", "review")
        # review 后条件路由：修改意见回 plan；批准则按章节 Send fan-out 并行研究
        builder.add_conditional_edges("review", route_after_review, ["plan", "research_chapter"])
        builder.add_edge("research_chapter", "synthesize")
        builder.add_edge("synthesize", "citation_check")
        builder.add_edge("citation_check", END)

        # checkpointer 必须挂载：review 节点 interrupt 后恢复执行依赖检查点持久化
        return builder.compile(checkpointer=await self._get_checkpointer())
