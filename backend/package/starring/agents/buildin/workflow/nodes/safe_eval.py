"""受限 Python 表达式求值器（用于 condition 节点）。

仅允许：布尔运算 / 比较运算 / 算术运算 / 字面量 / 变量访问 / 下标 / 属性读。
禁用：函数调用 / import / lambda / comprehensions / 赋值 / 属性写。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §六
"""
from __future__ import annotations

import ast
import operator
from typing import Any

# 表达式最大长度（防止 DoS）
MAX_EXPR_LEN = 500

# 允许的 AST 节点类型白名单
_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Subscript,
    ast.Attribute,
    ast.Tuple,
    ast.List,
    ast.Dict,
    ast.Index,  # Python < 3.9 兼容
)

# 允许的二元运算符
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}

# 允许的布尔运算符
_ALLOWED_BOOLOPS = {
    ast.And: lambda a, b: a and b,
    ast.Or: lambda a, b: a or b,
}

# 允许的一元运算符
_ALLOWED_UNARYOPS = {
    ast.Not: operator.not_,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

# 允许的比较运算符
_ALLOWED_CMPOPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

# 禁止访问的属性名（防 __import__ / __builtins__ 等）
_FORBIDDEN_ATTRS = frozenset(
    {
        "__import__",
        "__builtins__",
        "__globals__",
        "__locals__",
        "__code__",
        "__class__",
        "__bases__",
        "__subclasses__",
        "__mro__",
    }
)


def safe_eval(expr: str, context: dict[str, Any]) -> Any:
    """受限 Python 表达式求值。

    参数:
        expr: Python 表达式字符串（最长 500 字符）
        context: 变量上下文字典，通常包含 node_outputs

    返回:
        表达式求值结果

    抛出:
        ValueError: 表达式超长 / 包含不允许的语法 / 访问禁止属性
        TypeError: 操作数类型不匹配
    """
    if not isinstance(expr, str):
        raise ValueError(f"表达式必须是字符串，收到 {type(expr).__name__}")
    if len(expr) > MAX_EXPR_LEN:
        raise ValueError(f"表达式长度 {len(expr)} 超过上限 {MAX_EXPR_LEN}")
    if not expr.strip():
        raise ValueError("表达式不能为空")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"表达式语法错误: {exc.msg}") from exc

    return _eval_node(tree.body, context)


def _eval_node(node: ast.AST, context: dict[str, Any]) -> Any:
    """递归求值 AST 节点。"""
    if not isinstance(node, _ALLOWED_NODES):
        raise ValueError(f"不允许的语法: {type(node).__name__}")

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id not in context:
            raise ValueError(f"未定义的变量: {node.id}")
        return context[node.id]

    if isinstance(node, ast.BoolOp):
        op_func = _ALLOWED_BOOLOPS[type(node.op)]
        result = _eval_node(node.values[0], context)
        for val_node in node.values[1:]:
            result = op_func(result, _eval_node(val_node, context))
        return result

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_BINOPS:
            raise ValueError(f"不允许的二元运算符: {op_type.__name__}")
        left = _eval_node(node.left, context)
        right = _eval_node(node.right, context)
        return _ALLOWED_BINOPS[op_type](left, right)

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_UNARYOPS:
            raise ValueError(f"不允许的一元运算符: {op_type.__name__}")
        operand = _eval_node(node.operand, context)
        return _ALLOWED_UNARYOPS[op_type](operand)

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        for op, comparator in zip(node.ops, node.comparators):
            op_type = type(op)
            if op_type not in _ALLOWED_CMPOPS:
                raise ValueError(f"不允许的比较运算符: {op_type.__name__}")
            right = _eval_node(comparator, context)
            if not _ALLOWED_CMPOPS[op_type](left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.Subscript):
        value = _eval_node(node.value, context)
        # Python 3.9+ ast.Subscript.slice 直接是表达式；<3.9 是 ast.Index
        slice_node = node.slice
        if isinstance(slice_node, ast.Index):  # pragma: no cover
            slice_node = slice_node.value
        index = _eval_node(slice_node, context)
        return value[index]

    if isinstance(node, ast.Attribute):
        if node.attr in _FORBIDDEN_ATTRS:
            raise ValueError(f"禁止访问的属性: {node.attr}")
        if not isinstance(node.ctx, ast.Load):
            raise ValueError("属性仅支持读访问，不支持写入")
        value = _eval_node(node.value, context)
        return getattr(value, node.attr)

    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(elt, context) for elt in node.elts)

    if isinstance(node, ast.List):
        return [_eval_node(elt, context) for elt in node.elts]

    if isinstance(node, ast.Dict):
        return {
            _eval_node(k, context): _eval_node(v, context)
            for k, v in zip(node.keys, node.values)
        }

    raise ValueError(f"未处理的 AST 节点: {type(node).__name__}")
