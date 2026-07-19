"""WorkflowDefinition 模型校验测试。

覆盖 fail-fast 校验逻辑：
- 节点列表为空
- start/end 节点缺失或重复
- 边指向不存在的节点
- 节点数超限
- 环路检测
- 节点 config 必填字段校验

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §五
"""
from __future__ import annotations

import pytest

from starring.agents.buildin.workflow.definition import (
    Edge,
    Node,
    WorkflowDefinition,
)


def _make_valid_definition() -> WorkflowDefinition:
    """构造一个合法的工作流定义：start -> llm -> end。"""
    return WorkflowDefinition(
        nodes=[
            Node(id="start", node_type="start-end", config={"kind": "start"}),
            Node(
                id="llm-1",
                node_type="llm",
                config={"system_prompt": "你是助手", "model": "openai:gpt-4"},
            ),
            Node(id="end", node_type="start-end", config={"kind": "end"}),
        ],
        edges=[
            Edge(source="start", target="llm-1"),
            Edge(source="llm-1", target="end"),
        ],
    )


def test_valid_linear_workflow():
    """合法线性工作流：start -> llm -> end。"""
    wf = _make_valid_definition()
    assert wf.get_start_node_id() == "start"
    assert wf.get_end_node_id() == "end"
    assert len(wf.nodes) == 3
    assert len(wf.edges) == 2


def test_empty_nodes_rejected():
    """空节点列表应抛错。"""
    with pytest.raises(ValueError, match="节点列表为空"):
        WorkflowDefinition(nodes=[], edges=[])


def test_missing_start_node_rejected():
    """缺少 start 节点应抛错。"""
    with pytest.raises(ValueError, match="必须有且仅有一个 start 节点"):
        WorkflowDefinition(
            nodes=[
                Node(id="llm", node_type="llm", config={"system_prompt": "x"}),
                Node(id="end", node_type="start-end", config={"kind": "end"}),
            ],
            edges=[Edge(source="llm", target="end")],
        )


def test_missing_end_node_rejected():
    """缺少 end 节点应抛错。"""
    with pytest.raises(ValueError, match="必须有且仅有一个 end 节点"):
        WorkflowDefinition(
            nodes=[
                Node(id="start", node_type="start-end", config={"kind": "start"}),
                Node(id="llm", node_type="llm", config={"system_prompt": "x"}),
            ],
            edges=[Edge(source="start", target="llm")],
        )


def test_duplicate_start_nodes_rejected():
    """重复 start 节点应抛错。"""
    with pytest.raises(ValueError, match="必须有且仅有一个 start 节点"):
        WorkflowDefinition(
            nodes=[
                Node(id="start1", node_type="start-end", config={"kind": "start"}),
                Node(id="start2", node_type="start-end", config={"kind": "start"}),
                Node(id="end", node_type="start-end", config={"kind": "end"}),
            ],
            edges=[],
        )


def test_edge_to_nonexistent_node_rejected():
    """边指向不存在的节点应抛错。"""
    with pytest.raises(ValueError, match="指向不存在的节点"):
        WorkflowDefinition(
            nodes=[
                Node(id="start", node_type="start-end", config={"kind": "start"}),
                Node(id="end", node_type="start-end", config={"kind": "end"}),
            ],
            edges=[Edge(source="start", target="nonexistent")],
        )


def test_edge_from_nonexistent_node_rejected():
    """边从不存在的节点出发应抛错。"""
    with pytest.raises(ValueError, match="指向不存在的节点"):
        WorkflowDefinition(
            nodes=[
                Node(id="start", node_type="start-end", config={"kind": "start"}),
                Node(id="end", node_type="start-end", config={"kind": "end"}),
            ],
            edges=[Edge(source="nonexistent", target="end")],
        )


def test_too_many_nodes_rejected():
    """节点数超过上限 50 应抛错。"""
    nodes = [
        Node(id="start", node_type="start-end", config={"kind": "start"}),
        Node(id="end", node_type="start-end", config={"kind": "end"}),
    ]
    # 添加 49 个 llm 节点（总数 51）
    for i in range(49):
        nodes.insert(
            1,
            Node(id=f"llm-{i}", node_type="llm", config={"system_prompt": "x"}),
        )
    with pytest.raises(ValueError, match="节点数超过上限"):
        WorkflowDefinition(nodes=nodes, edges=[])


def test_cycle_rejected():
    """环路应抛错。"""
    with pytest.raises(ValueError, match="存在环路"):
        WorkflowDefinition(
            nodes=[
                Node(id="start", node_type="start-end", config={"kind": "start"}),
                Node(id="a", node_type="llm", config={"system_prompt": "x"}),
                Node(id="b", node_type="llm", config={"system_prompt": "y"}),
                Node(id="end", node_type="start-end", config={"kind": "end"}),
            ],
            edges=[
                Edge(source="start", target="a"),
                Edge(source="a", target="b"),
                Edge(source="b", target="a"),  # 环：a -> b -> a
                Edge(source="b", target="end"),
            ],
        )


# ---------------------------------------------------------------------------
# 节点 config 必填字段校验
# ---------------------------------------------------------------------------


def test_start_end_node_missing_kind_rejected():
    """start-end 节点缺少 config.kind 应抛错。"""
    with pytest.raises(ValueError, match="缺少 config.kind"):
        Node(id="start", node_type="start-end", config={})


def test_start_end_node_invalid_kind_rejected():
    """start-end 节点 config.kind 非法应抛错。"""
    with pytest.raises(ValueError, match="缺少 config.kind"):
        Node(id="start", node_type="start-end", config={"kind": "middle"})


def test_llm_node_missing_system_prompt_rejected():
    """llm 节点缺少 config.system_prompt 应抛错。"""
    with pytest.raises(ValueError, match="缺少 config.system_prompt"):
        Node(id="llm-1", node_type="llm", config={})


def test_condition_node_missing_cases_rejected():
    """condition 节点缺少 config.cases 应抛错。"""
    with pytest.raises(ValueError, match="缺少 config.cases"):
        Node(id="cond-1", node_type="condition", config={})


def test_application_call_node_missing_target_slug_rejected():
    """application-call 节点缺少 config.target_agent_slug 应抛错。"""
    with pytest.raises(ValueError, match="缺少 config.target_agent_slug"):
        Node(id="call-1", node_type="application-call", config={})


def test_unknown_node_type_rejected():
    """未知节点类型应抛错（Pydantic Literal 校验）。"""
    with pytest.raises(Exception):
        Node(id="x", node_type="unknown-type", config={})


# ---------------------------------------------------------------------------
# 辅助方法测试
# ---------------------------------------------------------------------------


def test_get_node_by_id():
    """get_node 按 ID 查找节点。"""
    wf = _make_valid_definition()
    assert wf.get_node("llm-1").node_type == "llm"
    with pytest.raises(KeyError):
        wf.get_node("nonexistent")


def test_get_outgoing_edges():
    """get_outgoing_edges 返回某节点的所有出边。"""
    wf = _make_valid_definition()
    assert len(wf.get_outgoing_edges("start")) == 1
    assert wf.get_outgoing_edges("start")[0].target == "llm-1"
    assert len(wf.get_outgoing_edges("end")) == 0


def test_definition_with_branch_edges():
    """带分支边的条件工作流定义。"""
    wf = WorkflowDefinition(
        nodes=[
            Node(id="start", node_type="start-end", config={"kind": "start"}),
            Node(id="cond", node_type="condition", config={
                "cases": [{"when": "true", "then": "a"}],
                "default": "b",
            }),
            Node(id="a", node_type="llm", config={"system_prompt": "x"}),
            Node(id="b", node_type="llm", config={"system_prompt": "y"}),
            Node(id="end", node_type="start-end", config={"kind": "end"}),
        ],
        edges=[
            Edge(source="start", target="cond"),
            Edge(source="cond", target="a", branch="a"),
            Edge(source="cond", target="b", branch="b"),
            Edge(source="a", target="end"),
            Edge(source="b", target="end"),
        ],
    )
    assert len(wf.nodes) == 5
    assert len(wf.edges) == 5
    cond_edges = wf.get_outgoing_edges("cond")
    assert len(cond_edges) == 2
    assert {e.branch for e in cond_edges} == {"a", "b"}
