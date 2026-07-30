"""CodeAct 工具桥服务：run 级 bridge token、白名单快照与工具调用分发。

设计文档：docs/vibe/2026-07-27-codeact-execution-paradigm.md

- token：``CodeActMiddleware`` 在每次 execute_python 执行前生成，上下文快照写入 Redis
  （TTL = 沙盒执行超时 + 60s），执行结束显式吊销
- 白名单：仅该智能体本次运行挂载、类别属于 buildin/knowledge、且不在排除清单内的工具
- 分发：按快照重建最小 runtime 注入后调用工具，权限边界与对话内直接调用一致
"""

from __future__ import annotations

import inspect
import json
import uuid
from dataclasses import dataclass
from typing import Any

from starring import config as conf
from starring.utils import logger

# 显式排除清单：类别属于 buildin/knowledge 但不允许进桥的工具。
# - execute_python：防止沙盒内代码递归触发沙盒执行
# - ask_user_question：内部调用 LangGraph interrupt()，桥端点的 HTTP 上下文中无 graph 运行时
# - install_skill：Skills 安装类，Phase 1 明确不桥接
# - present_artifacts：返回 Command 更新 graph state，脱离 graph 无意义
BRIDGE_EXCLUDED_TOOLS = frozenset({"execute_python", "ask_user_question", "install_skill", "present_artifacts"})

# 单次工具调用结果上限：超限直接报错拒绝（引导模型改用分页/精确查询），不做隐式截断
MAX_RESULT_BYTES = 262_144

_TOKEN_KEY_PREFIX = "codeact:token:"


class BridgeError(Exception):
    """桥调用的结构化错误：不静默降级，错误体原样回传沙盒内客户端抛出。"""

    def __init__(self, error_type: str, message: str):
        super().__init__(f"[{error_type}] {message}")
        self.error_type = error_type
        self.message = message


@dataclass
class _BridgeToolRuntime:
    """桥分发用的最小 runtime：现有可桥工具只从 runtime.context 读取字段。"""

    context: Any


def compute_bridge_whitelist(context) -> list[str]:
    """计算可进桥的工具名单。

    规则：本次运行挂载的 buildin 工具（context.tools 快照）+ knowledge 类工具
    （use_knowledge 显式为 False 时不挂载，与 KnowledgeBaseMiddleware 一致），
    再去掉 ``BRIDGE_EXCLUDED_TOOLS``。
    """
    from starring.agents.toolkits.service import get_tool_instances_by_category

    allowed: set[str] = set()
    buildin_names = {t.name for t in get_tool_instances_by_category("buildin")}
    selected = getattr(context, "tools", None) or []
    allowed |= buildin_names & {str(name) for name in selected if isinstance(name, str)}
    if getattr(context, "use_knowledge", None) is not False:
        allowed |= {t.name for t in get_tool_instances_by_category("knowledge")}
    return sorted(allowed - BRIDGE_EXCLUDED_TOOLS)


def _token_key(token: str) -> str:
    return f"{_TOKEN_KEY_PREFIX}{token}"


async def create_bridge_token(context) -> tuple[str, list[str]]:
    """生成 run 级 bridge token 并将上下文快照写入 Redis，返回 (token, 白名单)。"""
    from starring.services.run_queue_service import get_redis_client

    token = uuid.uuid4().hex
    whitelist = compute_bridge_whitelist(context)
    snapshot = {
        "uid": str(getattr(context, "uid", "") or ""),
        "thread_id": str(getattr(context, "thread_id", "") or ""),
        "run_id": getattr(context, "run_id", None),
        "allowed_tools": whitelist,
        "knowledges": getattr(context, "knowledges", None),
    }
    ttl = int(getattr(conf, "sandbox_exec_timeout_seconds", 180)) + 60
    redis = await get_redis_client()
    await redis.set(_token_key(token), json.dumps(snapshot, ensure_ascii=False), ex=ttl)
    return token, whitelist


async def revoke_bridge_token(token: str) -> None:
    """执行结束后吊销 token（TTL 只是兜底，正常路径显式失效）。"""
    from starring.services.run_queue_service import get_redis_client

    redis = await get_redis_client()
    await redis.delete(_token_key(token))


async def _load_snapshot(token: str) -> dict | None:
    from starring.services.run_queue_service import get_redis_client

    redis = await get_redis_client()
    raw = await redis.get(_token_key(token))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def _find_tool(tool_name: str) -> Any | None:
    from starring.agents.toolkits.registry import get_all_tool_instances

    for tool_obj in get_all_tool_instances():
        if tool_obj.name == tool_name:
            return tool_obj
    return None


async def _invoke_tool(tool_obj: Any, arguments: dict, runtime: _BridgeToolRuntime) -> Any:
    """调用工具：优先取原始函数并注入 runtime，无原始函数时走标准 ainvoke。"""
    func = getattr(tool_obj, "coroutine", None) or getattr(tool_obj, "func", None)
    if func is None:
        return await tool_obj.ainvoke(arguments)

    kwargs = dict(arguments)
    signature = inspect.signature(func)
    if "runtime" in signature.parameters:
        kwargs["runtime"] = runtime
    try:
        bound = signature.bind(**kwargs)
    except TypeError as exc:
        raise BridgeError("invalid_arguments", f"参数不符合工具 '{tool_obj.name}' 的签名: {exc}") from exc

    result = func(*bound.args, **bound.kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


async def dispatch_tool_call(token: str, tool_name: str, arguments: Any) -> Any:
    """校验 token 与白名单后分发工具调用，返回 JSON 可序列化的工具结果。

    所有失败路径抛 ``BridgeError``：token 无效/工具不在白名单/工具不存在/
    参数错误/执行异常/结果超限，由路由层转成 ``{"ok": false, "error": ...}``。
    """
    snapshot = await _load_snapshot(token)
    if snapshot is None:
        raise BridgeError("invalid_token", "bridge token 无效或已过期")

    normalized_name = str(tool_name or "").strip()
    if normalized_name not in set(snapshot.get("allowed_tools") or []):
        raise BridgeError("tool_not_allowed", f"工具 '{normalized_name}' 不在本次运行的白名单内")
    if not isinstance(arguments, dict):
        raise BridgeError("invalid_arguments", "arguments 必须是 JSON 对象")

    tool_obj = _find_tool(normalized_name)
    if tool_obj is None:
        raise BridgeError("tool_not_found", f"工具 '{normalized_name}' 不存在")

    # 按快照重建上下文：uid/thread_id/knowledges 与发起 run 一致，权限边界不放大
    from starring.agents.context import BaseContext

    context = BaseContext(
        uid=str(snapshot.get("uid") or ""),
        thread_id=str(snapshot.get("thread_id") or ""),
        knowledges=snapshot.get("knowledges"),
    )
    runtime = _BridgeToolRuntime(context=context)

    try:
        result = await _invoke_tool(tool_obj, arguments, runtime)
    except BridgeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"CodeAct 桥工具 '{normalized_name}' 执行失败: {exc}")
        raise BridgeError("tool_execution_error", f"{type(exc).__name__}: {exc}") from exc

    payload = json.dumps(result, ensure_ascii=False, default=str)
    if len(payload.encode("utf-8")) > MAX_RESULT_BYTES:
        raise BridgeError(
            "result_too_large",
            f"工具结果超过 {MAX_RESULT_BYTES} 字节上限，请改用分页参数或更精确的查询条件",
        )
    # 用序列化往返归一化结果，保证路由层返回体一定 JSON 可序列化
    return json.loads(payload)
