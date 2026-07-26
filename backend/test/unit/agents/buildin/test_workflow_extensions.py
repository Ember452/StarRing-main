"""工作流能力增强测试（第一批～第三批）。

覆盖：
- expr.py：resolve_expr 整串求值、render_template 内嵌插值、求值失败
- kb-retrieval 节点：权限过滤、白名单交集、不可见库报错、deliverable 结构
- 节点级重试：mock 前 N 次失败，验证重试次数与最终 fail-fast
- human-review 节点：interrupt payload、Command(resume=approve/reject)

设计依据：docs/vibe/工作流能力增强设计-20260725.md
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node, WorkflowDefinition, Edge
from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable


# ============================================================================
# expr.py：resolve_expr / render_template
# ============================================================================


def _state_with_outputs():
    return {
        "messages": [],
        "node_outputs": {
            "start": SubAgentDeliverable(summary="北京", raw_text="北京", confidence=1.0),
            "llm1": SubAgentDeliverable(summary="晴，25°C", raw_text="晴", confidence=0.9),
        },
    }


class TestResolveExpr:
    """整串 {{ expr }} 求值（tool_call 的既有语义）。"""

    def test_literal_passthrough(self):
        from starring.agents.buildin.workflow.nodes.expr import resolve_expr

        assert resolve_expr("hello", _state_with_outputs()) == "hello"
        assert resolve_expr(42, _state_with_outputs()) == 42
        assert resolve_expr(None, _state_with_outputs()) is None

    def test_expression_evaluated(self):
        from starring.agents.buildin.workflow.nodes.expr import resolve_expr

        result = resolve_expr(
            "{{ node_outputs['start'].summary }}", _state_with_outputs()
        )
        assert result == "北京"

    def test_non_full_expr_not_evaluated(self):
        """非整串 {{ }}（如内嵌拼接）不应被 resolve_expr 求值。"""
        from starring.agents.buildin.workflow.nodes.expr import resolve_expr

        value = "天气：{{ node_outputs['start'].summary }} 如何"
        assert resolve_expr(value, _state_with_outputs()) == value

    def test_bad_expression_raises(self):
        from starring.agents.buildin.workflow.nodes.expr import resolve_expr

        with pytest.raises((ValueError, TypeError, KeyError, AttributeError)):
            resolve_expr("{{ missing_var }}", _state_with_outputs())


class TestRenderTemplate:
    """内嵌插值：逐处替换 {{ expr }}。"""

    def test_no_expr_passthrough(self):
        from starring.agents.buildin.workflow.nodes.expr import render_template

        assert render_template("普通文本", _state_with_outputs()) == "普通文本"

    def test_single_expr(self):
        from starring.agents.buildin.workflow.nodes.expr import render_template

        result = render_template(
            "城市：{{ node_outputs['start'].summary }}", _state_with_outputs()
        )
        assert result == "城市：北京"

    def test_multiple_exprs(self):
        from starring.agents.buildin.workflow.nodes.expr import render_template

        result = render_template(
            "{{ node_outputs['start'].summary }}天气：{{ node_outputs['llm1'].summary }}",
            _state_with_outputs(),
        )
        assert result == "北京天气：晴，25°C"

    def test_eval_failure_raises_with_context(self):
        from starring.agents.buildin.workflow.nodes.expr import render_template

        with pytest.raises(ValueError, match="求值失败"):
            render_template(
                "结果：{{ missing.key }}", _state_with_outputs(), where="测试模板"
            )


# ============================================================================
# kb-retrieval 节点定义校验
# ============================================================================


class TestKbRetrievalDefinition:
    def test_valid_minimal(self):
        node = Node(id="kb1", node_type="kb-retrieval", config={"query": "搜索内容"})
        assert node.config["query"] == "搜索内容"

    def test_missing_query(self):
        with pytest.raises(ValidationError, match="query"):
            Node(id="kb1", node_type="kb-retrieval", config={})

    def test_empty_query(self):
        with pytest.raises(ValidationError, match="query"):
            Node(id="kb1", node_type="kb-retrieval", config={"query": ""})

    def test_kb_ids_non_string_list(self):
        with pytest.raises(ValidationError, match="kb_ids"):
            Node(id="kb1", node_type="kb-retrieval", config={
                "query": "test", "kb_ids": [1, 2],
            })

    def test_top_k_out_of_range(self):
        with pytest.raises(ValidationError, match="top_k"):
            Node(id="kb1", node_type="kb-retrieval", config={
                "query": "test", "top_k": 100,
            })

    def test_top_k_bool_rejected(self):
        with pytest.raises(ValidationError, match="top_k"):
            Node(id="kb1", node_type="kb-retrieval", config={
                "query": "test", "top_k": True,
            })


# ============================================================================
# kb-retrieval 执行器
# ============================================================================


def _fake_retriever(return_dict):
    return AsyncMock(return_value=return_dict)


@pytest.mark.asyncio
async def test_kb_retrieval_searches_visible_kbs():
    """有可见知识库时，应逐库检索并返回 deliverable。"""
    from starring.agents.buildin.workflow.nodes.kb_retrieval import execute_kb_retrieval

    node = Node(id="kb1", node_type="kb-retrieval", config={"query": "北京"})
    visible_kbs = [
        {"kb_id": "kb-a", "name": "地理知识库"},
        {"kb_id": "kb-b", "name": "天气知识库"},
    ]
    retrievers = {
        "kb-a": {"name": "地理知识库", "retriever": _fake_retriever({
            "results": [
                {"file_id": "f1", "content": "北京是中国的首都"},
            ],
        })},
        "kb-b": {"name": "天气知识库", "retriever": _fake_retriever({
            "results": [
                {"file_id": "f2", "content": "北京今天晴"},
            ],
        })},
    }

    with patch(
        "starring.agents.buildin.workflow.nodes.kb_retrieval.resolve_visible_knowledge_bases_for_context",
        AsyncMock(return_value=visible_kbs),
    ), patch(
        "starring.agents.buildin.workflow.nodes.kb_retrieval.knowledge_base.get_retrievers",
        return_value=retrievers,
    ):
        result = await execute_kb_retrieval(
            _state_with_outputs(), node, WorkflowContext(uid="u1")
        )

    deliverable = result["node_outputs"]["kb1"]
    assert "2 个库共命中 2 条" in deliverable.summary
    assert "地理知识库" in deliverable.raw_text
    assert "天气知识库" in deliverable.raw_text


@pytest.mark.asyncio
async def test_kb_retrieval_whitelist_intersection():
    """kb_ids 白名单应与可见库求交，不可见项报错。"""
    from starring.agents.buildin.workflow.nodes.kb_retrieval import execute_kb_retrieval

    node = Node(id="kb1", node_type="kb-retrieval", config={
        "query": "北京", "kb_ids": ["kb-a", "kb-ghost"],
    })
    visible_kbs = [{"kb_id": "kb-a", "name": "地理知识库"}]

    with patch(
        "starring.agents.buildin.workflow.nodes.kb_retrieval.resolve_visible_knowledge_bases_for_context",
        AsyncMock(return_value=visible_kbs),
    ):
        with pytest.raises(ValueError, match="不存在或无权限访问"):
            await execute_kb_retrieval(_state_with_outputs(), node, WorkflowContext(uid="u1"))


@pytest.mark.asyncio
async def test_kb_retrieval_no_visible_kbs():
    """无可见知识库时应 fail-fast 报错。"""
    from starring.agents.buildin.workflow.nodes.kb_retrieval import execute_kb_retrieval

    node = Node(id="kb1", node_type="kb-retrieval", config={"query": "北京"})

    with patch(
        "starring.agents.buildin.workflow.nodes.kb_retrieval.resolve_visible_knowledge_bases_for_context",
        AsyncMock(return_value=[]),
    ):
        with pytest.raises(ValueError, match="无可用知识库"):
            await execute_kb_retrieval(_state_with_outputs(), node, WorkflowContext(uid="u1"))


# ============================================================================
# 节点级重试策略
# ============================================================================


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt():
    """重试 2 次，第 1 次失败第 2 次成功应正常返回。"""
    from starring.agents.buildin.workflow.backend import WorkflowBackend

    counter = {"call": 0}

    async def flaky_executor(state, node, context):
        counter["call"] += 1
        if counter["call"] < 2:
            raise RuntimeError("临时故障")
        return {"node_outputs": {node.id: SubAgentDeliverable(summary="ok")}}

    backend = WorkflowBackend()
    node = Node(id="n1", node_type="llm", config={
        "system_prompt": "test",
        "retry_count": 2,
        "retry_interval": 0,
    })

    result = await backend._wrap_node_executor(
        flaky_executor, node, WorkflowContext(), _state_with_outputs(),
    )

    assert counter["call"] == 2
    assert result["node_outputs"]["n1"].summary == "ok"


@pytest.mark.asyncio
async def test_retry_exhausted_fail_fast():
    """重试次数耗尽后应原样抛出异常（fail-fast）。"""
    from starring.agents.buildin.workflow.backend import WorkflowBackend

    counter = {"call": 0}

    async def always_fail(state, node, context):
        counter["call"] += 1
        raise RuntimeError("永久故障")

    backend = WorkflowBackend()
    node = Node(id="n1", node_type="llm", config={
        "system_prompt": "test",
        "retry_count": 2,
        "retry_interval": 0,
    })

    with pytest.raises(RuntimeError, match="永久故障"):
        await backend._wrap_node_executor(
            always_fail, node, WorkflowContext(), _state_with_outputs(),
        )

    assert counter["call"] == 3  # 1 initial + 2 retries


@pytest.mark.asyncio
async def test_retry_without_config_does_not_retry():
    """未配置 retry_count 的节点不重试。"""
    from starring.agents.buildin.workflow.backend import WorkflowBackend

    counter = {"call": 0}

    async def always_fail(state, node, context):
        counter["call"] += 1
        raise RuntimeError("失败")

    backend = WorkflowBackend()
    node = Node(id="n1", node_type="llm", config={"system_prompt": "test"})

    with pytest.raises(RuntimeError):
        await backend._wrap_node_executor(
            always_fail, node, WorkflowContext(), _state_with_outputs(),
        )

    assert counter["call"] == 1  # 不重试


# ============================================================================
# human-review 节点定义校验
# ============================================================================


class TestHumanReviewDefinition:
    def test_valid(self):
        node = Node(id="hr1", node_type="human-review", config={"message": "请审核报告"})
        assert node.config["message"] == "请审核报告"

    def test_missing_message(self):
        with pytest.raises(ValidationError, match="message"):
            Node(id="hr1", node_type="human-review", config={})


# ============================================================================
# human-review 执行器（interrupt / resume）
# ============================================================================


@pytest.mark.asyncio
async def test_human_review_approve_branch():
    """resume action=approve 应写入 deliverable 并返回。"""
    from starring.agents.buildin.workflow.nodes.human_review import execute_human_review

    node = Node(id="hr1", node_type="human-review", config={"message": "请审核"})

    with patch(
        "starring.agents.buildin.workflow.nodes.human_review.interrupt",
        return_value={"action": "approve", "comment": "同意"},
    ):
        result = await execute_human_review(_state_with_outputs(), node, WorkflowContext())

    deliverable = result["node_outputs"]["hr1"]
    assert "通过" in deliverable.summary
    assert "同意" in deliverable.raw_text


@pytest.mark.asyncio
async def test_human_review_reject_branch():
    """resume action=reject 应抛错终止（fail-fast）。"""
    from starring.agents.buildin.workflow.nodes.human_review import execute_human_review

    node = Node(id="hr1", node_type="human-review", config={"message": "请审核"})

    with patch(
        "starring.agents.buildin.workflow.nodes.human_review.interrupt",
        return_value={"action": "reject", "comment": "数据有误"},
    ):
        with pytest.raises(ValueError, match="被拒绝"):
            await execute_human_review(_state_with_outputs(), node, WorkflowContext())


@pytest.mark.asyncio
async def test_human_review_unknown_action():
    """未知 action 应报错。"""
    from starring.agents.buildin.workflow.nodes.human_review import execute_human_review

    node = Node(id="hr1", node_type="human-review", config={"message": "请审核"})

    with patch(
        "starring.agents.buildin.workflow.nodes.human_review.interrupt",
        return_value={"action": "skip"},
    ):
        with pytest.raises(ValueError, match="未知审批动作"):
            await execute_human_review(_state_with_outputs(), node, WorkflowContext())


@pytest.mark.asyncio
async def test_human_review_interrupt_payload():
    """首次调用 interrupt 时应传入含 interrupt_type/message 的 payload。"""
    from starring.agents.buildin.workflow.nodes.human_review import execute_human_review

    mock_interrupt = MagicMock(return_value={"action": "approve", "comment": ""})
    node = Node(id="hr1", node_type="human-review", config={"message": "请审核结果"})

    with patch(
        "starring.agents.buildin.workflow.nodes.human_review.interrupt", mock_interrupt,
    ):
        await execute_human_review(_state_with_outputs(), node, WorkflowContext())

    called_payload = mock_interrupt.call_args[0][0]
    assert called_payload["interrupt_type"] == "human_review"
    assert called_payload["node_id"] == "hr1"
    assert called_payload["message"] == "请审核结果"


# ============================================================================
# human-review 图集成（MemorySaver）
# ============================================================================


@pytest.mark.asyncio
async def test_human_review_graph_interrupt_and_resume_approve():
    """在一个小图中验证 human-review interrupt + Command(resume=approve)。"""
    import starring.agents.buildin.workflow.nodes.human_review  # noqa: F401 触发注册

    from starring.agents.buildin.workflow.nodes import get_node_executor
    from starring.agents.buildin.workflow.state import WorkflowState

    executor = get_node_executor("human-review")

    builder = StateGraph(WorkflowState)
    node = Node(id="hr1", node_type="human-review", config={"message": "审核测试"})

    async def node_fn(state, n=node):
        return await executor(state, n, WorkflowContext())

    builder.add_node("hr1", node_fn)
    builder.set_entry_point("hr1")
    builder.set_finish_point("hr1")

    checkpointer = MemorySaver()
    graph = builder.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "test-hr-1"}}

    # 首次执行 → interrupt（astream 可能抛 GraphInterrupt 或正常结束）
    try:
        async for _ in graph.astream({"messages": []}, config):
            pass
    except Exception:
        pass

    # 验证中断后状态存在
    state = await graph.aget_state(config)
    assert state is not None

    # resume → approve
    result = await graph.ainvoke(Command(resume={"action": "approve", "comment": "通过"}), config)
    deliverable = result["node_outputs"]["hr1"]
    assert "通过" in deliverable.summary


@pytest.mark.asyncio
async def test_human_review_graph_interrupt_and_resume_reject():
    """resume=reject 应抛错终止。"""
    import starring.agents.buildin.workflow.nodes.human_review  # noqa: F401 触发注册

    from starring.agents.buildin.workflow.nodes import get_node_executor
    from starring.agents.buildin.workflow.state import WorkflowState

    executor = get_node_executor("human-review")

    builder = StateGraph(WorkflowState)
    node = Node(id="hr1", node_type="human-review", config={"message": "审核测试"})

    async def node_fn(state, n=node):
        return await executor(state, n, WorkflowContext())

    builder.add_node("hr1", node_fn)
    builder.set_entry_point("hr1")
    builder.set_finish_point("hr1")

    checkpointer = MemorySaver()
    graph = builder.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "test-hr-2"}}

    # 首次执行 → interrupt
    try:
        async for _ in graph.astream({"messages": []}, config):
            pass
    except Exception:
        pass

    # resume → reject
    with pytest.raises(ValueError, match="被拒绝"):
        await graph.ainvoke(Command(resume={"action": "reject", "comment": "数据不对"}), config)


# ============================================================================
# 重试策略定义校验
# ============================================================================


class TestRetryDefinition:
    def test_valid_retry_config(self):
        node = Node(id="n1", node_type="llm", config={
            "system_prompt": "test",
            "retry_count": 3,
            "retry_interval": 5.0,
        })
        assert node.config["retry_count"] == 3
        assert node.config["retry_interval"] == 5.0

    def test_retry_count_out_of_range(self):
        with pytest.raises(ValidationError, match="retry_count"):
            Node(id="n1", node_type="llm", config={
                "system_prompt": "test", "retry_count": 10,
            })

    def test_retry_interval_out_of_range(self):
        with pytest.raises(ValidationError, match="retry_interval"):
            Node(id="n1", node_type="llm", config={
                "system_prompt": "test", "retry_interval": 100,
            })

    def test_retry_count_bool_rejected(self):
        with pytest.raises(ValidationError, match="retry_count"):
            Node(id="n1", node_type="llm", config={
                "system_prompt": "test", "retry_count": True,
            })

    def test_start_end_ignores_retry(self):
        """start-end 节点不应触发重试校验（即使误传 retry_count 也会被忽略）。"""
        node = Node(id="start", node_type="start-end", config={
            "kind": "start", "retry_count": 5,
        })
        assert node.config.get("retry_count") == 5


# ============================================================================
# validate warnings
# ============================================================================


class TestValidationWarnings:
    def test_no_warnings_for_valid_graph(self):
        from starring.server.routers.workflow_router import _compute_definition_warnings

        definition = WorkflowDefinition.model_validate({
            "nodes": [
                {"id": "start", "node_type": "start-end", "config": {"kind": "start"}},
                {"id": "end", "node_type": "start-end", "config": {"kind": "end"}},
            ],
            "edges": [{"source": "start", "target": "end"}],
            "version": 1,
        })
        assert _compute_definition_warnings(definition) == []

    def test_orphan_node_warning(self):
        from starring.server.routers.workflow_router import _compute_definition_warnings

        definition = WorkflowDefinition.model_validate({
            "nodes": [
                {"id": "start", "node_type": "start-end", "config": {"kind": "start"}},
                {"id": "end", "node_type": "start-end", "config": {"kind": "end"}},
                {"id": "orphan", "node_type": "llm", "config": {"system_prompt": "test"}},
            ],
            "edges": [{"source": "start", "target": "end"}],
            "version": 1,
        })
        warnings = _compute_definition_warnings(definition)
        assert len(warnings) == 1
        assert warnings[0]["type"] == "orphan_node"
        assert warnings[0]["node_id"] == "orphan"

    def test_missing_default_warning(self):
        from starring.server.routers.workflow_router import _compute_definition_warnings

        definition = WorkflowDefinition.model_validate({
            "nodes": [
                {"id": "start", "node_type": "start-end", "config": {"kind": "start"}},
                {"id": "cond", "node_type": "condition", "config": {
                    "cases": [{"when": "True", "then": "end"}],
                }},
                {"id": "end", "node_type": "start-end", "config": {"kind": "end"}},
            ],
            "edges": [
                {"source": "start", "target": "cond"},
                {"source": "cond", "target": "end"},
            ],
            "version": 1,
        })
        warnings = _compute_definition_warnings(definition)
        assert any(w["type"] == "missing_default" for w in warnings)
