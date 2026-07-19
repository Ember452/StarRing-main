"""受限 Python 表达式求值器单测。

覆盖合法表达式 / 非法语法 / 危险访问 / 边界场景。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §六
"""
from __future__ import annotations

import pytest

from starring.agents.buildin.workflow.nodes.safe_eval import MAX_EXPR_LEN, safe_eval


# ---------------------------------------------------------------------------
# 合法表达式
# ---------------------------------------------------------------------------


def test_safe_eval_constant():
    """字面量常量。"""
    assert safe_eval("42", {}) == 42
    assert safe_eval("3.14", {}) == 3.14
    assert safe_eval("True", {}) is True
    assert safe_eval("False", {}) is False
    assert safe_eval("None", {}) is None
    assert safe_eval("'hello'", {}) == "hello"
    assert safe_eval('"world"', {}) == "world"


def test_safe_eval_compare():
    """比较运算符。"""
    assert safe_eval("1 < 2", {}) is True
    assert safe_eval("2 < 1", {}) is False
    assert safe_eval("1 <= 1", {}) is True
    assert safe_eval("3 > 2", {}) is True
    assert safe_eval("2 >= 2", {}) is True
    assert safe_eval("1 == 1", {}) is True
    assert safe_eval("1 != 2", {}) is True
    assert safe_eval("'a' in 'abc'", {}) is True
    assert safe_eval("'d' not in 'abc'", {}) is True


def test_safe_eval_bool_op():
    """布尔运算符 and / or。"""
    assert safe_eval("True and False", {}) is False
    assert safe_eval("True or False", {}) is True
    assert safe_eval("not True", {}) is False
    assert safe_eval("1 > 0 and 2 > 1", {}) is True


def test_safe_eval_arithmetic():
    """算术运算符。"""
    assert safe_eval("1 + 2", {}) == 3
    assert safe_eval("5 - 3", {}) == 2
    assert safe_eval("2 * 3", {}) == 6
    assert safe_eval("6 / 2", {}) == 3.0
    assert safe_eval("7 % 3", {}) == 1
    assert safe_eval("7 // 2", {}) == 3
    assert safe_eval("-5", {}) == -5
    assert safe_eval("+5", {}) == 5


def test_safe_eval_variable_access():
    """变量访问。"""
    ctx = {"x": 10, "name": "Alice"}
    assert safe_eval("x", ctx) == 10
    assert safe_eval("name", ctx) == "Alice"
    assert safe_eval("x > 5", ctx) is True


def test_safe_eval_subscript():
    """下标访问（dict / list / tuple）。"""
    ctx = {"d": {"a": 1, "b": 2}, "lst": [10, 20, 30], "t": (1, 2, 3)}
    assert safe_eval("d['a']", ctx) == 1
    assert safe_eval("d['b']", ctx) == 2
    assert safe_eval("lst[0]", ctx) == 10
    assert safe_eval("lst[2]", ctx) == 30
    assert safe_eval("t[1]", ctx) == 2


def test_safe_eval_attribute_access():
    """属性读取（Pydantic 模型字段）。"""
    from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable

    deliverable = SubAgentDeliverable(summary="test", confidence=0.8)
    ctx = {"d": deliverable}
    assert safe_eval("d.summary", ctx) == "test"
    assert safe_eval("d.confidence", ctx) == 0.8
    assert safe_eval("d.confidence > 0.5", ctx) is True


def test_safe_eval_node_outputs_typical():
    """典型工作流表达式：访问 node_outputs 中节点的 deliverable。"""
    from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable

    ctx = {
        "node_outputs": {
            "llm-1": SubAgentDeliverable(summary="合规", confidence=0.9, key_findings=["a", "b"]),
            "llm-2": SubAgentDeliverable(summary="不合规", confidence=0.3),
        }
    }
    assert safe_eval("node_outputs['llm-1'].confidence > 0.8", ctx) is True
    assert safe_eval("node_outputs['llm-2'].confidence < 0.5", ctx) is True
    assert safe_eval("'合规' in node_outputs['llm-1'].summary", ctx) is True
    assert (
        safe_eval(
            "node_outputs['llm-1'].confidence > 0.8 and len(node_outputs['llm-1'].key_findings) > 1",
            ctx,
        )
        is True
    )


def test_safe_eval_dict_literal():
    """dict / list / tuple 字面量构造。"""
    assert safe_eval("{'a': 1, 'b': 2}", {}) == {"a": 1, "b": 2}
    assert safe_eval("[1, 2, 3]", {}) == [1, 2, 3]
    assert safe_eval("(1, 2, 3)", {}) == (1, 2, 3)


# ---------------------------------------------------------------------------
# 非法表达式（应抛 ValueError）
# ---------------------------------------------------------------------------


def test_safe_eval_rejects_empty_expr():
    """空表达式。"""
    with pytest.raises(ValueError, match="不能为空"):
        safe_eval("", {})
    with pytest.raises(ValueError, match="不能为空"):
        safe_eval("   ", {})


def test_safe_eval_rejects_non_string():
    """非字符串表达式。"""
    with pytest.raises(ValueError, match="必须是字符串"):
        safe_eval(123, {})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="必须是字符串"):
        safe_eval(None, {})  # type: ignore[arg-type]


def test_safe_eval_rejects_too_long_expr():
    """表达式超长。"""
    long_expr = "1 + " * 300 + "1"  # > 500 字符
    assert len(long_expr) > MAX_EXPR_LEN
    with pytest.raises(ValueError, match="超过上限"):
        safe_eval(long_expr, {})


def test_safe_eval_rejects_syntax_error():
    """Python 语法错误。"""
    with pytest.raises(ValueError, match="语法错误"):
        safe_eval("1 +", {})
    with pytest.raises(ValueError, match="语法错误"):
        safe_eval("def x(): pass", {})


def test_safe_eval_rejects_function_call():
    """禁止函数调用。"""
    with pytest.raises(ValueError, match="不允许的语法"):
        safe_eval("print('hello')", {})
    with pytest.raises(ValueError, match="不允许的语法"):
        safe_eval("len([1, 2, 3])", {})


def test_safe_eval_rejects_import():
    """禁止 import 语句。"""
    with pytest.raises(ValueError, match="不允许的语法"):
        safe_eval("__import__('os')", {})


def test_safe_eval_rejects_lambda():
    """禁止 lambda。"""
    with pytest.raises(ValueError, match="不允许的语法"):
        safe_eval("lambda x: x", {})


def test_safe_eval_rejects_comprehension():
    """禁止列表/字典/集合推导式。"""
    with pytest.raises(ValueError, match="不允许的语法"):
        safe_eval("[x for x in [1, 2, 3]]", {})


def test_safe_eval_rejects_assignment():
    """禁止赋值。"""
    with pytest.raises(ValueError, match="不允许的语法"):
        safe_eval("x = 1", {})


def test_safe_eval_rejects_forbidden_dunder_attrs():
    """禁止访问危险 dunder 属性。"""
    with pytest.raises(ValueError, match="禁止访问的属性"):
        safe_eval("().__class__", {})
    with pytest.raises(ValueError, match="禁止访问的属性"):
        safe_eval("().__class__.__bases__", {})


def test_safe_eval_rejects_undefined_variable():
    """未定义变量。"""
    with pytest.raises(ValueError, match="未定义的变量"):
        safe_eval("undefined_var", {})


def test_safe_eval_rejects_attribute_write():
    """禁止属性写入（仅允许读取）。"""
    # ast.parse 会把 a.b = 1 当作 Assign 节点，不在白名单
    with pytest.raises(ValueError, match="不允许的语法"):
        safe_eval("x.y = 1", {})


# ---------------------------------------------------------------------------
# 求值异常（非 ValueError）
# ---------------------------------------------------------------------------


def test_safe_eval_raises_on_type_mismatch():
    """类型不匹配时抛 TypeError。"""
    # 1 + 'a' 触发 TypeError
    with pytest.raises(TypeError):
        safe_eval("1 + 'a'", {})


def test_safe_eval_raises_on_key_error():
    """字典 key 不存在时抛 KeyError。"""
    ctx = {"d": {"a": 1}}
    with pytest.raises(KeyError):
        safe_eval("d['nonexistent']", ctx)


def test_safe_eval_raises_on_attribute_error():
    """访问不存在的属性时抛 AttributeError。"""
    from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable

    ctx = {"d": SubAgentDeliverable()}
    with pytest.raises(AttributeError):
        safe_eval("d.nonexistent_field", ctx)
