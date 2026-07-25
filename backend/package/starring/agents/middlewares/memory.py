"""长期记忆中间件 - remember 工具 + 记忆召回注入 system prompt。

- ``remember`` 工具：用户显式说"记住 XX"时，模型调用即时写入长期记忆（source=manual）
- ``awrap_model_call``：首次模型调用时按最新用户消息向量召回 top-k 记忆，
  以「用户长期记忆」段落追加到 system prompt；每 run 只检索一次（结果缓存在实例上）
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

try:
    from deepagents.middleware._utils import append_to_system_message
except ImportError:
    append_to_system_message = None  # type: ignore[assignment]
from langchain.agents.middleware.types import AgentMiddleware, ContextT, ModelRequest, ModelResponse, ResponseT
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolRuntime

from starring.utils import logger

REMEMBER_TOOL_DESCRIPTION = (
    "把一条关于用户的长期事实写入跨会话记忆。"
    "仅当用户明确要求记住某事（如「记住我喜欢…」「以后都用…」），"
    "或用户陈述了明显值得长期保留的个人偏好/背景时调用。"
    "content 必须是一条简洁的第三人称陈述句（如「用户偏好简洁的中文回复」），"
    "禁止写入密码、密钥等敏感信息。"
)

_MEMORY_UNSET = object()


class MemoryMiddleware(AgentMiddleware):
    """长期记忆中间件：注册 remember 工具并在模型调用前注入召回的记忆。"""

    def __init__(self, uid: str):
        super().__init__()
        self.uid = str(uid)
        # 每 run 只检索一次：首次模型调用后缓存注入文本（None 表示已检索但无记忆）
        self._memory_prompt: str | None | object = _MEMORY_UNSET
        self.tools = [self._build_remember_tool()]

    def _build_remember_tool(self) -> StructuredTool:
        def remember(content: str, runtime: ToolRuntime) -> str:
            return "remember 工具仅支持异步调用"

        async def aremember(content: str, runtime: ToolRuntime) -> str:
            from starring.memory.service import add_memory

            del runtime
            result = await add_memory(self.uid, content, source="manual")
            if result is None:
                return "该记忆与已有记忆重复或已达存储上限，未写入。"
            return f"已记住：{result['content']}"

        return StructuredTool.from_function(
            name="remember",
            func=remember,
            coroutine=aremember,
            description=REMEMBER_TOOL_DESCRIPTION,
            infer_schema=True,
        )

    @staticmethod
    def _latest_human_text(messages) -> str:
        """取 messages 中最新一条 HumanMessage 的文本作为召回 query。"""
        for message in reversed(messages or []):
            if isinstance(message, HumanMessage):
                content = message.content
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    texts = [
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ]
                    return "\n".join(t for t in texts if t)
        return ""

    async def _get_memory_prompt(self, messages) -> str | None:
        if self._memory_prompt is not _MEMORY_UNSET:
            return self._memory_prompt  # type: ignore[return-value]

        from starring.memory.service import retrieve_memories

        prompt: str | None = None
        try:
            query = self._latest_human_text(messages)
            memories = await retrieve_memories(self.uid, query) if query else []
            if memories:
                lines = "\n".join(f"- {m['content']}" for m in memories)
                prompt = (
                    "## 用户长期记忆\n\n"
                    "以下是关于当前用户的长期记忆（来自历史会话），回答时自然地参考，"
                    "不要主动向用户复述这份列表：\n"
                    f"{lines}"
                )
        except Exception as e:
            logger.warning(f"Memory retrieval failed for uid={self.uid}: {e}")
        self._memory_prompt = prompt
        return prompt

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        memory_prompt = await self._get_memory_prompt(request.messages)
        if not memory_prompt:
            return await handler(request)
        if append_to_system_message is not None:
            system_message = append_to_system_message(request.system_message, memory_prompt)
        else:
            system_message = f"{request.system_message}\n\n{memory_prompt}" if request.system_message else memory_prompt
        return await handler(request.override(system_message=system_message))
