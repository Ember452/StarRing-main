"""工作流引擎 backend 实现。

WorkflowBackend 继承 BaseAgent，把持久化的工作流定义（nodes + edges）
编译为 LangGraph StateGraph，被 auto_discover_agents 自动发现。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §八
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from starring.agents import BaseAgent
from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import WorkflowDefinition
from starring.agents.buildin.workflow.nodes import (
    application_call as _application_call_module,  # noqa: F401 触发 @register_node
)
from starring.agents.buildin.workflow.nodes import (
    condition as _condition_module,  # noqa: F401 触发 @register_node
)
from starring.agents.buildin.workflow.nodes import get_node_executor
from starring.agents.buildin.workflow.nodes import (
    kb_retrieval as _kb_retrieval_module,  # noqa: F401 触发 @register_node
)
from starring.agents.buildin.workflow.nodes import (
    llm as _llm_module,  # noqa: F401 触发 @register_node
)
from starring.agents.buildin.workflow.nodes import (
    start_end as _start_end_module,  # noqa: F401 触发 @register_node
)
from starring.agents.buildin.workflow.nodes import (
    tool_call as _tool_call_module,  # noqa: F401 触发 @register_node
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
        "确定性流程编排 backend，基于 LangGraph StateGraph。适用于合规审查、标准化报告、流水线数据处理等流程化任务。"
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
        workflow_id 支持两种形式：
        - slug（推荐，由 agent_runtime_service 注入 agents.slug）
        - UUID（用户显式配置 workflows.id）
        """
        if not context.workflow_id:
            raise ValueError(
                "WorkflowBackend 缺少 workflow_id，请通过 context.workflow_id 指定要执行的工作流 "
                "（值为 workflows.slug 或 workflows.id）"
            )

        # 从数据库加载（延迟导入避免循环依赖）
        from starring.repositories.workflow_repository import WorkflowRepository
        from starring.storage.postgres.manager import pg_manager

        async with pg_manager.get_async_session_context() as db:
            repo = WorkflowRepository(db)
            # 先按 slug 查（最常见路径，由 agent_runtime_service 注入 agents.slug）
            workflow = await repo.get_by_slug(context.workflow_id)
            if workflow is None:
                # fallback：按 UUID 查（用户显式配置 workflows.id 的场景）
                workflow = await repo.get(context.workflow_id)
            if workflow is None:
                raise ValueError(f"工作流 {context.workflow_id!r} 不存在（已按 slug 与 id 两次查询）")
            if not workflow.is_active:
                raise ValueError(f"工作流 {context.workflow_id} 已停用")

            definition = WorkflowDefinition.model_validate(workflow.definition)
            context.workflow_version = workflow.version
            return definition

    def _build_state_graph(self, definition: WorkflowDefinition, context: WorkflowContext) -> CompiledStateGraph:
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
                # condition 节点：运行时通过 Command(goto=...) 动态跳转，不走 conditional_edges。
                # 这里把所有可能目标都注册为直接边是 LangGraph 的硬性要求：
                # 编译期需要枚举所有可达目标以构建图结构，未注册的 goto 目标会触发 KeyError。
                for edge in edges:
                    builder.add_edge(source_id, edge.target)
            else:
                # 普通节点：只允许有一条出边（多出边是配置错误，fail-fast）
                if len(edges) > 1:
                    raise ValueError(
                        f"节点 {source_id}（类型 {source_node.node_type}）"
                        f"有 {len(edges)} 条出边，普通节点只允许 1 条出边；"
                        f"如需多路分支请使用 condition 节点"
                    )
                if edges:
                    builder.add_edge(source_id, edges[0].target)

        # 3. 设置入口与出口
        builder.set_entry_point(start_id)
        builder.set_finish_point(end_id)

        # 4. 编译
        # checkpointer 不在这里绑定：LangGraph 支持在 astream/ainvoke 时通过 config={"configurable": {"checkpoint_ns": ...}}
        # 传入 checkpointer，由调用方（chat_service / agent_run_service）统一管理持久化后端，
        # 这样同一个编译产物可以在不同 thread / 不同 checkpointer 下复用。
        return builder.compile()

    async def _wrap_node_executor(
        self,
        executor,
        node,
        context: WorkflowContext,
        state: WorkflowState,
    ) -> Any:
        """包装节点执行器，统一异常处理、重试与日志。

        LangGraph 节点函数签名是 (state) -> dict，这里通过 partial 绑定 executor/node/context。
        节点 config 可选 retry_count（0-5，默认 0 不重试）/ retry_interval（秒，默认 1）：
        失败后等待 retry_interval 再重试，超限后原样抛出（fail-fast，不做跳过）。
        """
        config = node.config or {}
        retry_count = int(config.get("retry_count") or 0)
        retry_interval_cfg = config.get("retry_interval")
        retry_interval = float(retry_interval_cfg) if retry_interval_cfg is not None else 1.0

        logger.debug(f"工作流节点 {node.id} ({node.node_type}) 开始执行")
        for attempt in range(retry_count + 1):
            try:
                result = await executor(state, node, context)
                logger.debug(f"工作流节点 {node.id} 执行完成: {type(result).__name__}")
                return result
            except GraphInterrupt:
                # interrupt 是控制流（如 human-review 等待审批），不是失败，不参与重试
                raise
            except Exception as exc:
                if attempt < retry_count:
                    logger.warning(
                        f"工作流节点 {node.id} 第 {attempt + 1}/{retry_count} 次重试"
                        f"（{retry_interval}s 后）: {exc}"
                    )
                    await asyncio.sleep(retry_interval)
                    continue
                logger.error(f"工作流节点 {node.id} 执行失败: {exc}", exc_info=True)
                raise
