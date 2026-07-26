"""工作流定义 Pydantic 模型。

定义工作流的持久化结构：nodes + edges + version。
被 WorkflowBackend 在 get_graph() 阶段解析为 LangGraph StateGraph。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §五
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Node(BaseModel):
    """工作流节点定义。"""

    id: str = Field(description="节点唯一 ID（在工作流内唯一）")
    node_type: Literal["start-end", "llm", "condition", "application-call", "tool", "kb-retrieval", "human-review"] = (
        Field(description="节点类型")
    )
    name: str = Field(default="", description="节点展示名")
    config: dict[str, Any] = Field(default_factory=dict, description="节点类型特定配置")

    @model_validator(mode="after")
    def _validate_config(self) -> Node:
        """校验节点配置必填字段。"""
        if self.node_type == "start-end":
            if self.config.get("kind") not in ("start", "end"):
                raise ValueError(f"start-end 节点 {self.id} 缺少 config.kind，必须为 'start' 或 'end'")
        elif self.node_type == "llm":
            if not self.config.get("system_prompt"):
                raise ValueError(f"llm 节点 {self.id} 缺少 config.system_prompt")
            # 工具挂载字段（可选，P2 新增）：tools / mcps 为字符串列表，max_tool_steps 为 1-25 整数
            for field_name in ("tools", "mcps"):
                value = self.config.get(field_name)
                if value is not None and (
                    not isinstance(value, list) or not all(isinstance(item, str) for item in value)
                ):
                    raise ValueError(f"llm 节点 {self.id} 的 config.{field_name} 必须是字符串列表")
            max_tool_steps = self.config.get("max_tool_steps")
            if max_tool_steps is not None and (
                not isinstance(max_tool_steps, int) or isinstance(max_tool_steps, bool) or not 1 <= max_tool_steps <= 25
            ):
                raise ValueError(f"llm 节点 {self.id} 的 config.max_tool_steps 必须是 1-25 的整数")
        elif self.node_type == "condition":
            if not self.config.get("cases") or not isinstance(self.config["cases"], list):
                raise ValueError(f"condition 节点 {self.id} 缺少 config.cases 列表")
        elif self.node_type == "application-call":
            if not self.config.get("target_agent_slug"):
                raise ValueError(f"application-call 节点 {self.id} 缺少 config.target_agent_slug")
        elif self.node_type == "tool":
            # 工具存在性是运行时状态（validate 端点无 DB 会话），这里仅校验配置形状，
            # 工具不存在在节点执行时 fail-fast
            tool_source = self.config.get("tool_source")
            if tool_source not in ("buildin", "mcp"):
                raise ValueError(f"tool 节点 {self.id} 的 config.tool_source 必须为 'buildin' 或 'mcp'")
            if not self.config.get("tool_name"):
                raise ValueError(f"tool 节点 {self.id} 缺少 config.tool_name")
            if tool_source == "mcp" and not self.config.get("mcp_server"):
                raise ValueError(f"tool 节点 {self.id} 的 tool_source 为 'mcp' 时必须配置 config.mcp_server")
            args = self.config.get("args")
            if args is not None and not isinstance(args, dict):
                raise ValueError(f"tool 节点 {self.id} 的 config.args 必须是 dict")
        elif self.node_type == "kb-retrieval":
            query = self.config.get("query")
            if not query or not isinstance(query, str):
                raise ValueError(f"kb-retrieval 节点 {self.id} 缺少 config.query（非空字符串，支持 {{{{ expr }}}}）")
            kb_ids = self.config.get("kb_ids")
            if kb_ids is not None and (
                not isinstance(kb_ids, list) or not all(isinstance(item, str) for item in kb_ids)
            ):
                raise ValueError(f"kb-retrieval 节点 {self.id} 的 config.kb_ids 必须是字符串列表")
            top_k = self.config.get("top_k")
            if top_k is not None and (not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 50):
                raise ValueError(f"kb-retrieval 节点 {self.id} 的 config.top_k 必须是 1-50 的整数")
        elif self.node_type == "human-review":
            message = self.config.get("message")
            if not message or not isinstance(message, str):
                raise ValueError(f"human-review 节点 {self.id} 缺少 config.message（审核提示语，支持 {{{{ expr }}}}）")

        # 重试策略（可选，适用于所有可执行节点）：失败重试 retry_count 次后仍 fail-fast
        if self.node_type != "start-end":
            retry_count = self.config.get("retry_count")
            if retry_count is not None and (
                not isinstance(retry_count, int) or isinstance(retry_count, bool) or not 0 <= retry_count <= 5
            ):
                raise ValueError(f"节点 {self.id} 的 config.retry_count 必须是 0-5 的整数")
            retry_interval = self.config.get("retry_interval")
            if retry_interval is not None and (
                not isinstance(retry_interval, (int, float))
                or isinstance(retry_interval, bool)
                or not 0 <= retry_interval <= 60
            ):
                raise ValueError(f"节点 {self.id} 的 config.retry_interval 必须是 0-60 的数字（秒）")
        return self


class Edge(BaseModel):
    """工作流边定义。"""

    source: str = Field(description="源节点 ID")
    target: str = Field(description="目标节点 ID")
    branch: str | None = Field(
        default=None,
        description="条件分支标识：source 是 condition 节点时，对应 config.cases[i].then",
    )


class WorkflowDefinition(BaseModel):
    """完整工作流定义（持久化到 workflows.definition JSON 字段）。"""

    nodes: list[Node] = Field(default_factory=list, description="节点列表")
    edges: list[Edge] = Field(default_factory=list, description="边列表")
    version: int = Field(default=1, description="定义版本号")

    @model_validator(mode="after")
    def _validate_graph(self) -> WorkflowDefinition:
        """校验工作流图结构合法性（fail-fast）。"""
        node_ids = {n.id for n in self.nodes}
        if not node_ids:
            raise ValueError("工作流节点列表为空")

        # 必须有且仅有一个 start 与一个 end 节点
        start_nodes = [n for n in self.nodes if n.node_type == "start-end" and n.config.get("kind") == "start"]
        end_nodes = [n for n in self.nodes if n.node_type == "start-end" and n.config.get("kind") == "end"]
        if len(start_nodes) != 1:
            raise ValueError(f"工作流必须有且仅有一个 start 节点，当前 {len(start_nodes)} 个")
        if len(end_nodes) != 1:
            raise ValueError(f"工作流必须有且仅有一个 end 节点，当前 {len(end_nodes)} 个")

        # 边的 source/target 必须指向已存在的节点
        for edge in self.edges:
            if edge.source not in node_ids:
                raise ValueError(f"边 source={edge.source} 指向不存在的节点")
            if edge.target not in node_ids:
                raise ValueError(f"边 target={edge.target} 指向不存在的节点")

        # 节点数限制（防止定义过大）
        if len(self.nodes) > 50:
            raise ValueError(f"工作流节点数超过上限 50，当前 {len(self.nodes)}")

        # 边数限制（防止定义过大）
        if len(self.edges) > 100:
            raise ValueError(f"工作流边数超过上限 100，当前 {len(self.edges)}")

        # 校验 condition 节点的 cases[i].then 与 default 必须指向已存在节点
        for node in self.nodes:
            if node.node_type == "condition":
                cases = node.config.get("cases", [])
                for case in cases:
                    then_branch = case.get("then")
                    if then_branch is not None and then_branch not in node_ids:
                        raise ValueError(f"condition 节点 {node.id} 的 case.then={then_branch!r} 指向不存在的节点")
                default_branch = node.config.get("default")
                if default_branch is not None and default_branch not in node_ids:
                    raise ValueError(f"condition 节点 {node.id} 的 default={default_branch!r} 指向不存在的节点")

        # 简单环路检测：DFS 检测是否存在环（不含 start/end 的环）
        self._check_no_cycles()

        return self

    def _check_no_cycles(self) -> None:
        """DFS 检测环路（不含 start/end 节点的环非法）。

        采用经典的三色标记法（WHITE 未访问 / GRAY 在当前递归栈中 / BLACK 已完成）：
        - 遇到 GRAY 节点说明回边存在，即发现环
        - BLACK 节点可安全跳过，避免重复遍历
        时间复杂度 O(V+E)，足以覆盖 50 节点 / 100 边的工作流上限。
        """
        adj: dict[str, list[str]] = {n.id: [] for n in self.nodes}
        for edge in self.edges:
            adj[edge.source].append(edge.target)

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n.id: WHITE for n in self.nodes}

        def dfs(node: str) -> bool:
            """返回 True 表示发现环。"""
            color[node] = GRAY
            for nxt in adj.get(node, []):
                if color[nxt] == GRAY:
                    return True
                if color[nxt] == WHITE and dfs(nxt):
                    return True
            color[node] = BLACK
            return False

        for node_id in color:
            if color[node_id] == WHITE and dfs(node_id):
                raise ValueError(f"工作流存在环路（涉及节点 {node_id}）")

    def get_start_node_id(self) -> str:
        """获取 start 节点 ID。"""
        for n in self.nodes:
            if n.node_type == "start-end" and n.config.get("kind") == "start":
                return n.id
        raise ValueError("工作流缺少 start 节点")

    def get_end_node_id(self) -> str:
        """获取 end 节点 ID。"""
        for n in self.nodes:
            if n.node_type == "start-end" and n.config.get("kind") == "end":
                return n.id
        raise ValueError("工作流缺少 end 节点")

    def get_node(self, node_id: str) -> Node:
        """按 ID 获取节点。"""
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"节点 {node_id} 不存在")

    def get_outgoing_edges(self, node_id: str) -> list[Edge]:
        """获取某节点的所有出边。"""
        return [e for e in self.edges if e.source == node_id]
