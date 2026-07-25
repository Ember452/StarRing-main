"""工作流节点执行器注册中心。

每个节点类型实现 `async def execute(state, node, context)` 函数，
在 NODE_REGISTRY 中按 node_type 注册，被 WorkflowBackend 在编译 StateGraph 时调用。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §五、§八.3
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.buildin.workflow.state import WorkflowState

# 节点执行器签名：接收 (state, node, context)，返回 Command 或 dict（LangGraph 节点函数返回值）
NodeExecutor = Callable[
    [WorkflowState, Node, WorkflowContext],
    Awaitable["dict | object"],
]

# 节点类型注册表：node_type -> executor
NODE_REGISTRY: dict[str, NodeExecutor] = {}


def register_node(node_type: str) -> Callable[[NodeExecutor], NodeExecutor]:
    """装饰器：注册节点执行器到 NODE_REGISTRY。"""

    def decorator(func: NodeExecutor) -> NodeExecutor:
        if node_type in NODE_REGISTRY:
            raise ValueError(f"节点类型 {node_type} 已注册")
        NODE_REGISTRY[node_type] = func
        return func

    return decorator


def get_node_executor(node_type: str) -> NodeExecutor:
    """按节点类型获取执行器，未注册时抛 ValueError。"""
    if node_type not in NODE_REGISTRY:
        raise ValueError(f"未知节点类型 {node_type}，已注册类型：{list(NODE_REGISTRY.keys())}")
    return NODE_REGISTRY[node_type]
