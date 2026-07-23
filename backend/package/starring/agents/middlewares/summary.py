"""starring adapter for DeepAgents conversation summarization middleware.

本模块基于 DeepAgents 的 SummarizationMiddleware 进行扩展，提供了 StarRing 项目
特有的工具调用结果脱敏（sanitization）与摘要能力。

核心功能：
1. 工具输出结果（ToolMessage）的自动保存与截断预览，避免大体积工具结果撑爆上下文窗口。
2. 利用 ContextVar 在线程/协程上下文中传递 backend 和脱敏缓存，保证并发安全。
3. 支持同步和异步两种模型调用包装（wrap_model_call / awrap_model_call）。
4. 提供工厂函数 create_summary_middleware 快速创建已配置好的中间件实例。

主要流程：
- 模型调用前：通过 wrap_model_call 设置当前请求的 backend 和脱敏消息缓存。
- 模型调用中：父类 SummarizationMiddleware 在触发摘要条件时，调用本类的
  _sanitize_messages_for_summary 对 ToolMessage 做内容替换，再调用 LLM 生成摘要。
- 模型调用后：通过 finally 块清理 ContextVar，避免上下文泄露。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware.summarization import ContextSize
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.chat_models import BaseChatModel
from langchain_core.messages import AnyMessage, ToolMessage

from starring.agents.backends.composite import create_agent_composite_backend
from starring.utils.paths import VIRTUAL_PATH_CONVERSATION_HISTORY, VIRTUAL_PATH_LARGE_TOOL_RESULTS

# 每个 token 大约对应的字符数，用于估算文本的 token 数量。
# 实际值因模型而异（英文约 4 chars/token，中文约 1.5-2 chars/token），
# 这里取一个偏保守的估计，避免截断过多。
_APPROX_CHARS_PER_TOKEN = 4

# 摘要时工具结果预览的默认 token 上限。
# 超过此限制的工具输出会被截断，完整内容写入后端存储。
_DEFAULT_SUMMARY_TOOL_RESULT_LIMIT_TOKENS = 500

# ToolMessage.additional_kwargs 中的标记键，表示该消息的工具结果已被保存到后端。
# 用于防止重复保存同一工具结果。
_TOOL_RESULT_SAVED_MARKER = "STARRING_tool_result_saved"

# 当前请求上下文中的后端实例（ContextVar，线程/协程安全）。
# 在 wrap_model_call 中设置，在 _sanitize_messages_for_summary 中读取，
# 用于将大体积工具结果写入存储后端。
_SUMMARY_BACKEND: ContextVar[Any | None] = ContextVar("STARRING_summary_backend", default=None)

# 当前请求上下文中的脱敏消息缓存（ContextVar，线程/协程安全）。
# 键为消息 id 元组，值为脱敏后的消息列表。
# 用于避免在同一请求内对同一组消息重复执行脱敏操作。
_SUMMARY_SANITIZED_MESSAGES: ContextVar[dict[tuple[int, ...], list[AnyMessage]] | None] = ContextVar(
    "STARRING_summary_sanitized_messages",
    default=None,
)


def _extract_text_content(content: Any) -> str:
    """从消息内容中提取纯文本字符串。

    支持三种常见的消息内容格式：
    - 纯字符串：直接返回。
    - 列表（多模态内容块）：提取其中 type="text" 的文本块并拼接。
    - 其他类型：fallback 为 str() 转换。

    Args:
        content: 消息的 content 字段，可能是 str、list[dict] 或其他类型。

    Returns:
        提取出的纯文本字符串。如果 content 为 None，返回空字符串。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def _tool_result_path(tool_name: str | None, content: str, large_tool_results_prefix: str) -> str:
    """为工具结果生成唯一的存储路径。

    路径格式：{prefix}/{safe_tool_name}-{sha256_hexdigest[:16]}.txt
    - 工具名中的非法字符（非字母数字、下划线、点、连字符）会被替换为连字符。
    - 对内容做 SHA256 哈希，取前 16 位 hex 作为去重/唯一标识。

    Args:
        tool_name: 工具名称，可能为 None。
        content: 工具输出的完整文本内容。
        large_tool_results_prefix: 存储路径前缀（虚拟目录）。

    Returns:
        格式为 "{prefix}/{safe_name}-{digest}.txt" 的虚拟路径字符串。
    """
    safe_tool_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", (tool_name or "").strip()).strip(".-") or "tool-result"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"{large_tool_results_prefix}/{safe_tool_name}-{digest}.txt"


def _preview_tool_result(content: str, token_limit: int | None) -> tuple[str, int]:
    """按 token 限制截取工具输出的预览文本。

    使用 _APPROX_CHARS_PER_TOKEN 将 token 限制转换为字符数限制进行截断。
    返回 (预览文本, 被省略的字符数) 元组。

    Args:
        content: 原始工具输出文本。
        token_limit: 预览的 token 上限。
            - None: 不截断，返回完整内容。
            - <=0: 不保留任何预览，返回空字符串。

    Returns:
        (preview_str, omitted_chars)：
        - preview_str: 截断后的预览文本（已去除尾部空白）。
        - omitted_chars: 被省略的字符数（0 表示无省略）。
    """
    text = content.strip()
    if token_limit is None:
        return text, 0
    if token_limit <= 0:
        return "", len(text)

    max_chars = token_limit * _APPROX_CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text, 0

    preview = text[:max_chars].rstrip()
    return preview, len(text) - len(preview)


def _write_tool_result(backend, path: str, content: str) -> str | None:
    """将工具结果写入后端存储。

    如果 backend 为 None（未配置存储后端），则跳过写入，返回 None。
    如果写入失败且错误信息包含 "already exists"，视为幂等成功（已存在即跳过）。
    其他错误会抛出 RuntimeError。

    Args:
        backend: 存储后端实例，需支持 write(path, content) 方法。
        path: 目标存储路径（虚拟路径）。
        content: 要写入的完整工具输出内容。

    Returns:
        写入成功时返回目标路径 str，未写入时返回 None。

    Raises:
        RuntimeError: 写入失败且非 "already exists" 错误。
    """
    if backend is None:
        return None

    result = backend.write(path, content)
    error = getattr(result, "error", None)
    if not error:
        return path
    if "already exists" in str(error).lower():
        return path
    raise RuntimeError(f"Failed to write tool result to {path}: {error}")


def _tool_result_replacement_content(
    message: ToolMessage,
    *,
    backend,
    tool_result_offload_token_limit: int | None,
    large_tool_results_prefix: str,
) -> str:
    """生成替换 ToolMessage 内容的摘要文本。

    对给定的 ToolMessage，执行以下操作：
    1. 提取原始文本内容。
    2. 将完整内容写入后端存储。
    3. 按 token 限制生成预览文本。
    4. 拼装替换内容，包含工具名、token 估算、存储路径和预览。

    Args:
        message: 原始 ToolMessage 实例。
        backend: 存储后端，用于保存完整工具结果。
        tool_result_offload_token_limit: 预览的 token 上限（None 表示不截断）。
        large_tool_results_prefix: 存储路径前缀。

    Returns:
        替换后的内容字符串，包含工具结果摘要信息。
    """
    content = _extract_text_content(message.content)
    approx_tokens = max((len(content) + _APPROX_CHARS_PER_TOKEN - 1) // _APPROX_CHARS_PER_TOKEN, 1)
    tool_name = message.name if isinstance(message.name, str) and message.name else None
    path = _write_tool_result(backend, _tool_result_path(tool_name, content, large_tool_results_prefix), content)
    preview, omitted_chars = _preview_tool_result(content, tool_result_offload_token_limit)

    lines = [
        "[Tool result saved]",
        f"Tool: {tool_name or 'unknown'}",
        f"Approx tokens: {approx_tokens}",
    ]
    if path:
        lines.append(f"Full output path: {path}")
    if preview:
        lines.extend(["", "Output preview:", preview])
    if omitted_chars:
        lines.append(f"\n[Truncated {omitted_chars} chars. Read the full output from the saved file.]")
    return "\n".join(lines)


def _replace_tool_message_content(
    message: ToolMessage,
    *,
    backend,
    tool_result_offload_token_limit: int | None,
    large_tool_results_prefix: str,
) -> ToolMessage:
    """创建 ToolMessage 的副本，将其内容替换为脱敏摘要。

    在副本的 additional_kwargs 中设置 _TOOL_RESULT_SAVED_MARKER 标记，
    防止后续重复处理同一消息。

    Args:
        message: 原始 ToolMessage 实例。
        backend: 存储后端，用于保存完整工具结果。
        tool_result_offload_token_limit: 预览的 token 上限。
        large_tool_results_prefix: 存储路径前缀。

    Returns:
        新的 ToolMessage 实例，content 已替换为摘要信息，
        additional_kwargs 中包含 _TOOL_RESULT_SAVED_MARKER 标记。
    """
    additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
    additional_kwargs[_TOOL_RESULT_SAVED_MARKER] = True
    return message.model_copy(
        update={
            "content": _tool_result_replacement_content(
                message,
                backend=backend,
                tool_result_offload_token_limit=tool_result_offload_token_limit,
                large_tool_results_prefix=large_tool_results_prefix,
            ),
            "additional_kwargs": additional_kwargs,
        }
    )


def sanitize_messages_for_summary(
    messages: list[AnyMessage],
    *,
    backend=None,
    tool_result_offload_token_limit: int | None = _DEFAULT_SUMMARY_TOOL_RESULT_LIMIT_TOKENS,
    large_tool_results_prefix: str = VIRTUAL_PATH_LARGE_TOOL_RESULTS,
) -> list[AnyMessage]:
    """构建用于摘要生成的脱敏消息列表。

    遍历消息列表，仅对 ToolMessage 做内容替换（保存完整结果到后端，
    替换为摘要预览），其他类型消息原样保留。已标记 _TOOL_RESULT_SAVED_MARKER
    的消息跳过处理，避免重复保存。

    此函数为纯函数，不依赖类实例，可直接从 StarRingSummarizationMiddleware
    外部调用。

    Args:
        messages: 原始消息列表。
        backend: 存储后端实例，None 表示不保存完整结果。
        tool_result_offload_token_limit: 工具结果预览的 token 上限。
        large_tool_results_prefix: 工具结果存储路径前缀。

    Returns:
        脱敏后的消息列表，其中 ToolMessage 的 content 已被替换为摘要信息。
    """
    sanitized: list[AnyMessage] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            if getattr(message, "additional_kwargs", {}).get(_TOOL_RESULT_SAVED_MARKER) is True:
                sanitized.append(message)
                continue
            sanitized.append(
                _replace_tool_message_content(
                    message,
                    backend=backend,
                    tool_result_offload_token_limit=tool_result_offload_token_limit,
                    large_tool_results_prefix=large_tool_results_prefix,
                )
            )
            continue
        sanitized.append(message)
    return sanitized


class StarRingSummarizationMiddleware(SummarizationMiddleware):
    """DeepAgents 摘要中间件，集成 StarRing 的工具结果脱敏能力。

    继承自 SummarizationMiddleware，在父类基础上增加了：
    - 工具结果（ToolMessage）的自动保存与截断预览。
    - 基于 ContextVar 的请求级 backend 传递和脱敏缓存。
    - 同步/异步双模式的模型调用包装。

    使用时通过 create_summary_middleware 工厂函数创建实例。
    """

    def __init__(
        self,
        *args,
        tool_result_offload_token_limit: int | None = _DEFAULT_SUMMARY_TOOL_RESULT_LIMIT_TOKENS,
        **kwargs,
    ) -> None:
        """初始化 StarRing 摘要中间件。

        Args:
            *args: 传递给父类 SummarizationMiddleware 的位置参数。
            tool_result_offload_token_limit: 工具结果预览的 token 上限。
                None 表示不截断，默认 500 tokens。
            **kwargs: 传递给父类 SummarizationMiddleware 的关键字参数。
        """
        super().__init__(*args, **kwargs)
        self.tool_result_offload_token_limit = tool_result_offload_token_limit

    def _sanitize_messages_for_summary(
        self,
        messages: list[AnyMessage],
        *,
        backend,
    ) -> list[AnyMessage]:
        """对消息列表进行脱敏，并利用 ContextVar 缓存结果。

        在单次请求内，相同消息列表（按 id 去重）仅脱敏一次，后续调用直接返回缓存。
        委托给模块级函数 sanitize_messages_for_summary 执行实际脱敏逻辑。

        Args:
            messages: 原始消息列表。
            backend: 存储后端实例。

        Returns:
            脱敏后的消息列表。
        """
        cache = _SUMMARY_SANITIZED_MESSAGES.get()
        cache_key = tuple(id(message) for message in messages)
        if cache is not None and cache_key in cache:
            return cache[cache_key]

        sanitized = sanitize_messages_for_summary(
            messages,
            backend=backend,
            tool_result_offload_token_limit=self.tool_result_offload_token_limit,
            large_tool_results_prefix=self._large_tool_results_prefix,
        )
        if cache is not None:
            cache[cache_key] = sanitized
        return sanitized

    def _backend_for_request(self, request: ModelRequest):
        """从模型请求中获取当前请求对应的存储后端。

        调用父类的 _get_backend 方法，传入 request.state 和 request.runtime。
        如果获取失败（如无可用后端），返回 None。

        Args:
            request: 当前模型请求对象。

        Returns:
            存储后端实例，获取失败时返回 None。
        """
        try:
            return self._get_backend(request.state, request.runtime)
        except Exception:
            return None

    def _create_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        """同步生成对话摘要。

        先对消息进行脱敏处理（使用 ContextVar 中的 backend），
        再调用父类的 _create_summary 生成摘要文本。

        Args:
            messages_to_summarize: 待摘要的原始消息列表。

        Returns:
            生成的摘要文本。
        """
        return super()._create_summary(
            self._sanitize_messages_for_summary(
                messages_to_summarize,
                backend=_SUMMARY_BACKEND.get(),
            )
        )

    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        """异步生成对话摘要。

        先对消息进行脱敏处理（使用 ContextVar 中的 backend），
        再调用父类的 _acreate_summary 异步生成摘要文本。

        Args:
            messages_to_summarize: 待摘要的原始消息列表。

        Returns:
            生成的摘要文本。
        """
        return await super()._acreate_summary(
            self._sanitize_messages_for_summary(
                messages_to_summarize,
                backend=_SUMMARY_BACKEND.get(),
            )
        )

    def _offload_to_backend(self, backend, messages: list[AnyMessage]) -> str | None:
        """同步将脱敏后的消息卸载到后端存储。

        Args:
            backend: 存储后端实例。
            messages: 原始消息列表。

        Returns:
            存储路径或 None。
        """
        return super()._offload_to_backend(
            backend,
            self._sanitize_messages_for_summary(
                messages,
                backend=backend,
            ),
        )

    async def _aoffload_to_backend(self, backend, messages: list[AnyMessage]) -> str | None:
        """异步将脱敏后的消息卸载到后端存储。

        Args:
            backend: 存储后端实例。
            messages: 原始消息列表。

        Returns:
            存储路径或 None。
        """
        return await super()._aoffload_to_backend(
            backend,
            self._sanitize_messages_for_summary(
                messages,
                backend=backend,
            ),
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """同步包装模型调用，注入请求级上下文。

        在调用前设置 ContextVar：
        - _SUMMARY_BACKEND: 当前请求的存储后端。
        - _SUMMARY_SANITIZED_MESSAGES: 空的脱敏消息缓存字典。

        调用后通过 finally 块清理 ContextVar，避免上下文泄露到其他请求。

        Args:
            request: 模型请求对象。
            handler: 实际的模型调用处理函数。

        Returns:
            模型响应对象。
        """
        backend_token = _SUMMARY_BACKEND.set(self._backend_for_request(request))
        sanitized_token = _SUMMARY_SANITIZED_MESSAGES.set({})
        try:
            return super().wrap_model_call(request, handler)
        finally:
            _SUMMARY_SANITIZED_MESSAGES.reset(sanitized_token)
            _SUMMARY_BACKEND.reset(backend_token)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """异步包装模型调用，注入请求级上下文。

        在调用前设置 ContextVar：
        - _SUMMARY_BACKEND: 当前请求的存储后端。
        - _SUMMARY_SANITIZED_MESSAGES: 空的脱敏消息缓存字典。

        调用后通过 finally 块清理 ContextVar，避免上下文泄露到其他请求。

        Args:
            request: 模型请求对象。
            handler: 实际的异步模型调用处理函数。

        Returns:
            模型响应对象。
        """
        backend_token = _SUMMARY_BACKEND.set(self._backend_for_request(request))
        sanitized_token = _SUMMARY_SANITIZED_MESSAGES.set({})
        try:
            return await super().awrap_model_call(request, handler)
        finally:
            _SUMMARY_SANITIZED_MESSAGES.reset(sanitized_token)
            _SUMMARY_BACKEND.reset(backend_token)


def create_summary_middleware(
    model: str | BaseChatModel,
    *,
    trigger: ContextSize | list[ContextSize] | None,
    keep: ContextSize | list[ContextSize] | None,
    summary_prompt: str | None = None,
    trim_tokens_to_summarize: int | None = None,
    tool_result_offload_token_limit: int | None = _DEFAULT_SUMMARY_TOOL_RESULT_LIMIT_TOKENS,
) -> SummarizationMiddleware:
    """创建已配置好的 StarRing 摘要中间件实例。

    工厂函数，封装了 StarRingSummarizationMiddleware 的创建和配置：
    - 使用 create_agent_composite_backend 作为存储后端工厂。
    - 自动设置 _history_path_prefix 和 _large_tool_results_prefix 虚拟路径。

    Args:
        model: 用于生成摘要的 LLM 模型（模型名或 BaseChatModel 实例）。
        trigger: 触发摘要的上下文大小条件（token 数或消息数阈值）。
        keep: 摘要后保留的最近上下文大小。
        summary_prompt: 自定义摘要提示词，None 使用默认提示词。
        trim_tokens_to_summarize: 传递给 LLM 进行摘要的最大 token 数。
        tool_result_offload_token_limit: 工具结果预览的 token 上限，
            默认 500 tokens。

    Returns:
        配置好的 StarRingSummarizationMiddleware 实例。
    """
    middleware_kwargs = {
        "model": model,
        "backend": create_agent_composite_backend,
        "trigger": trigger,
        "keep": keep,
        "trim_tokens_to_summarize": trim_tokens_to_summarize,
        "tool_result_offload_token_limit": tool_result_offload_token_limit,
    }
    if summary_prompt and summary_prompt.strip():
        middleware_kwargs["summary_prompt"] = summary_prompt
    middleware = StarRingSummarizationMiddleware(**middleware_kwargs)
    middleware._history_path_prefix = VIRTUAL_PATH_CONVERSATION_HISTORY
    middleware._large_tool_results_prefix = VIRTUAL_PATH_LARGE_TOOL_RESULTS
    return middleware