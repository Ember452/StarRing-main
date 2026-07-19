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
    node_type: Literal["start-end", "llm", "condition", "application-call"] = Field(
        description="节点类型"
    )
    name: str = Field(default="", description="节点展示名")
    config: dict[str, Any] = Field(default_factory=dict, description="节点类型特定配置")

    @model_validator(mode="after")
    def _validate_config(self) -> Node:
        """校验节点配置必填字段。"""
        if self.node_type == "start-end":
            if self.config.get("kind") not in ("start", "end"):
                raise ValueError(
                    f"start-end 节点 {self.id} 缺少 config.kind，必须为 'start' 或 'end'"
                )
        elif self.node_type == "llm":
            if not self.config.get("system_prompt"):
                raise ValueError(f"llm 节点 {self.id} 缺少 config.system_prompt")
        elif self.node_type == "condition":
            if not self.config.get("cases") or not isinstance(self.config["cases"], list):
                raise ValueError(f"condition 节点 {self.id} 缺少 config.cases 列表")
        elif self.node_type == "application-call":
            if not self.config.get("target_agent_slug"):
                raise ValueError(
                    f"application-call 节点 {self.id} 缺少 config.target_agent_slug"
                )
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

        # 简单环路检测：DFS 检测是否存在环（不含 start/end 的环）
        self._check_no_cycles()

        return self

    def _check_no_cycles(self) -> None:
        """DFS 检测环路（不含 start/end 节点的环非法）。"""
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
