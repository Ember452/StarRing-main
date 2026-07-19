"""工作流引擎 backend 实现。

WorkflowBackend 继承 BaseAgent，把持久化的工作流定义（nodes + edges）
编译为 LangGraph StateGraph，被 auto_discover_agents 自动发现。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §八
"""
from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from starring.agents import BaseAgent
from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import WorkflowDefinition
from starring.agents.buildin.workflow.nodes import get_node_executor
from starring.agents.buildin.workflow.nodes import (
    application_call as _application_call_module,  # noqa: F401 触发 @register_node
    condition as _condition_module,  # noqa: F401 触发 @register_node
    llm as _llm_module,  # noqa: F401 触发 @register_node
    start_end as _start_end_module,  # noqa: F401 触发 @register_node
)
from starring.agents.buildin.workflow.state import WorkflowState
from starring.utils import logger


class WorkflowBackend(BaseAgent):
    """工作流引擎 backend，基于 LangGraph StateGraph 实现确定性流程编排。

    与 ChatbotAgent（Orchestrator-Worker，LLM 自主路由）和 SupervisorAgent
    （软编排，强制委派）形成三种 backend 范式。

    典型场景：合规审查、标准化报告、流水线数据处理等确定性流程。
    """

    name = "工作流引擎"
    description = (
        "确定性流程编排 backend，基于 LangGraph StateGraph。"
        "适用于合规审查、标准化报告、流水线数据处理等流程化任务。"
    )
    capabilities = ["file_upload", "files"]
    context_schema = WorkflowContext

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def get_graph(self, context=None, **kwargs) -> CompiledStateGraph:
        """编译工作流定义为 StateGraph。

        从 context.workflow_id 加载工作流定义（如未加载），解析 nodes + edges
        为 StateGraph 节点 + 边，编译返回 CompiledStateGraph。
        """
        from starring.agents.context import prepare_agent_runtime_context

        ctx = await prepare_agent_runtime_context(
            context or self.context_schema(),
            context_schema=self.context_schema,
        )

        # 从 context 加载工作流定义
        definition = await self._load_definition(ctx)
        logger.info(
            f"WorkflowBackend 编译工作流: nodes={len(definition.nodes)}, "
            f"edges={len(definition.edges)}, version={definition.version}"
        )

        return self._build_state_graph(definition, ctx)

    async def _load_definition(self, context: WorkflowContext) -> WorkflowDefinition:
        """从 context 加载工作流定义。

        优先用 context 中已缓存的 definition；否则按 workflow_id 查库。
        """
        # Phase 1 简化：context.workflow_id 必须由调用方提供
        # 后续接入 workflow_router 后从数据库加载
        if not context.workflow_id:
            raise ValueError(
                "WorkflowBackend 缺少 workflow_id，请通过 context.workflow_id 指定要执行的工作流"
            )

        # 从数据库加载（延迟导入避免循环依赖）
        from starring.repositories.workflow_repository import WorkflowRepository
        from starring.storage.postgres.manager import pg_manager

        async with pg_manager.get_async_session_context() as db:
            repo = WorkflowRepository(db)
            workflow = await repo.get(context.workflow_id)
            if workflow is None:
                raise ValueError(f"工作流 {context.workflow_id} 不存在")
            if not workflow.is_active:
                raise ValueError(f"工作流 {context.workflow_id} 已停用")

            definition = WorkflowDefinition.model_validate(workflow.definition)
            context.workflow_version = workflow.version
            return definition

    def _build_state_graph(
        self, definition: WorkflowDefinition, context: WorkflowContext
    ) -> CompiledStateGraph:
        """把工作流定义编译为 LangGraph StateGraph。

        步骤：
        1. 注册所有节点（按 node_type 从 NODE_REGISTRY 取执行器）
        2. 注册边（含 condition 节点的条件边）
        3. 设置 entry_point = start 节点
        4. 编译返回 CompiledStateGraph
        """
        builder: StateGraph = StateGraph(WorkflowState)

        # 1. 注册节点
        for node in definition.nodes:
            executor = get_node_executor(node.node_type)
            # 用 partial 绑定 node + context，生成 LangGraph 节点函数 (state) -> dict
            node_func = partial(self._wrap_node_executor, executor, node, context)
            builder.add_node(node.id, node_func)

        # 2. 注册边
        # 收集每个节点的出边，按节点分组
        outgoing: dict[str, list] = {}
        for edge in definition.edges:
            outgoing.setdefault(edge.source, []).append(edge)

        # start 节点：直接连接到第一个非 start 节点
        start_id = definition.get_start_node_id()
        end_id = definition.get_end_node_id()

        for source_id, edges in outgoing.items():
            if source_id == end_id:
                # end 节点无出边
                continue

            # 检查是否是 condition 节点
            source_node = definition.get_node(source_id)
            if source_node.node_type == "condition":
                # condition 节点：通过 Command(goto=...) 实现跳转，注册直接边
                # LangGraph 会根据 Command.goto 跳转，无需显式 conditional_edges
                for edge in edges:
                    builder.add_edge(source_id, edge.target)
            else:
                # 普通节点：注册直接边（同源多边时取第一条）
                if edges:
                    builder.add_edge(source_id, edges[0].target)

        # 3. 设置入口与出口
        builder.set_entry_point(start_id)
        builder.set_finish_point(end_id)

        # 4. 编译
        # 注意：checkpointer 在 ainvoke 时通过 config 传入，这里不绑定
        return builder.compile()

    async def _wrap_node_executor(
        self,
        executor,
        node,
        context: WorkflowContext,
        state: WorkflowState,
    ) -> Any:
        """包装节点执行器，统一异常处理与日志。

        LangGraph 节点函数签名是 (state) -> dict，这里通过 partial 绑定 executor/node/context。
        """
        logger.debug(f"工作流节点 {node.id} ({node.node_type}) 开始执行")
        try:
            result = await executor(state, node, context)
            logger.debug(f"工作流节点 {node.id} 执行完成: {type(result).__name__}")
            return result
        except Exception as exc:
            logger.error(f"工作流节点 {node.id} 执行失败: {exc}", exc_info=True)
            raise
