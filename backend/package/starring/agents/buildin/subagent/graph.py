from typing import Any

try:
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
except ImportError:
    PatchToolCallsMiddleware = None  # type: ignore[assignment,misc]
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRetryMiddleware, TodoListMiddleware
from langchain.agents.middleware.types import AgentMiddleware

from starring.agents import BaseAgent, BaseState, load_chat_model, resolve_chat_model_spec
from starring.agents.backends import create_agent_filesystem_middleware
from starring.agents.buildin.chatbot.prompt import TODO_MID_PROMPT, build_prompt_with_context
from starring.agents.buildin.subagent.context import SubAgentContext
from starring.agents.context import (
    DEFAULT_STARRING_SUMMARY_PROMPT,
    DEFAULT_SUMMARY_KEEP_MESSAGES,
    DEFAULT_SUMMARY_THRESHOLD_K,
    DEFAULT_SUMMARY_TOOL_RESULT_TOKEN_LIMIT,
    DEFAULT_TOOL_RESULT_EVICTION_K_TOKENS,
    prepare_agent_runtime_context,
)
from starring.agents.middlewares import TokenUsageMiddleware, create_summary_middleware, save_attachments_to_fs
from starring.agents.middlewares.knowledge_base import KnowledgeBaseMiddleware
from starring.agents.middlewares.skills import SkillsMiddleware
from starring.agents.toolkits.service import resolve_configured_runtime_tools

_SUBAGENT_DISABLED_TOOLS = frozenset({"present_artifacts", "ask_user_question", "install_skill"})

# 子智能体被父智能体通过 task 工具调用时，追加到 system_prompt 末尾的结构化输出说明。
# 仅在 context.output_format == "structured" 时追加；自然对话模式不加，避免破坏子智能体直接被用户调用的体验。
_STRUCTURED_OUTPUT_PROMPT_SUFFIX = """\

## 输出格式要求（被父智能体调用模式）

本次任务由父智能体通过 task 工具委派，**最终回复必须**用以下格式输出结构化交付物：

```subagent-result
{
  "summary": "1-3 句话概括任务结果",
  "key_findings": ["关键发现 1", "关键发现 2"],
  "sources": [
    {"type": "kb_chunk", "file_id": "...", "chunk_id": "...", "snippet": "..."}
  ],
  "confidence": 0.85,
  "artifacts": ["/sandbox/path/to/file"]
}
```

字段说明：
- `summary`：必填，任务结果摘要（1-3 句话）
- `key_findings`：关键发现列表，每条 1 句话
- `sources`：引用来源，type 可选 kb_chunk / file / url / other
- `confidence`：0-1 之间的置信度，0.5=中等不确定，0.9=高确信
- `artifacts`：产物文件路径（沙盒绝对路径）

注意：
- 只输出 fenced code block，不要在前后加自然语言解释
- 如果任务失败或无关键发现，仍要输出结构化结果（confidence 设低，key_findings 留空）
- raw_text 字段由父智能体自动填充，你不需要写
"""


def _tool_name(tool) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
    else:
        name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


def _filter_disabled_tools(tools):
    return [tool for tool in tools if _tool_name(tool) not in _SUBAGENT_DISABLED_TOOLS]


class _SubAgentToolFilterMiddleware(AgentMiddleware[Any, Any, Any]):
    def wrap_model_call(self, request, handler):
        return handler(request.override(tools=_filter_disabled_tools(request.tools or [])))

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(tools=_filter_disabled_tools(request.tools or [])))


async def _build_middlewares(context):
    summary_trigger_tokens = getattr(context, "summary_threshold", DEFAULT_SUMMARY_THRESHOLD_K) * 1024
    summary_keep_messages = getattr(context, "summary_keep_messages", DEFAULT_SUMMARY_KEEP_MESSAGES)
    summary_prompt = getattr(context, "summary_prompt", None) or DEFAULT_STARRING_SUMMARY_PROMPT
    summary_tool_result_token_limit = getattr(
        context,
        "summary_tool_result_token_limit",
        DEFAULT_SUMMARY_TOOL_RESULT_TOKEN_LIMIT,
    )
    model_spec = resolve_chat_model_spec(context.model)
    summary_middleware = create_summary_middleware(
        model=load_chat_model(fully_specified_name=model_spec),
        trigger=("tokens", summary_trigger_tokens),
        keep=("messages", summary_keep_messages),
        summary_prompt=summary_prompt,
        trim_tokens_to_summarize=4000,
        tool_result_offload_token_limit=summary_tool_result_token_limit,
    )

    return [
        create_agent_filesystem_middleware(
            getattr(context, "tool_token_limit", DEFAULT_TOOL_RESULT_EVICTION_K_TOKENS) * 1024,
            context=context,
        ),
        save_attachments_to_fs,
        KnowledgeBaseMiddleware(),
        SkillsMiddleware(),
        summary_middleware,
        TodoListMiddleware(system_prompt=TODO_MID_PROMPT),
        *([PatchToolCallsMiddleware()] if PatchToolCallsMiddleware is not None else []),
        _SubAgentToolFilterMiddleware(),
        ModelRetryMiddleware(),
        TokenUsageMiddleware(),
    ]


class SubAgentBackend(BaseAgent):
    name = "子智能体"
    description = "用于被主智能体通过 task 工具调用的专用智能体后端。"
    capabilities = ["file_upload", "files"]
    context_schema = SubAgentContext

    async def get_info(
        self,
        include_configurable_items: bool = True,
        user_role: str | None = None,
        db=None,
        user=None,
    ):
        info = await super().get_info(
            include_configurable_items=include_configurable_items,
            user_role=user_role,
            db=db,
            user=user,
        )
        tools_item = (info.get("configurable_items") or {}).get("tools")
        if isinstance(tools_item, dict):
            tools_item["options"] = [
                option
                for option in tools_item.get("options") or []
                if option.get("key") not in _SUBAGENT_DISABLED_TOOLS
            ]
        return info

    async def get_graph(self, context=None, **kwargs):
        context = await prepare_agent_runtime_context(
            context or self.context_schema(),
            context_schema=self.context_schema,
        )
        model_spec = resolve_chat_model_spec(context.model)

        system_prompt = build_prompt_with_context(context)
        # 被父智能体调用时追加结构化输出说明；自然对话模式不加，避免破坏直接对话体验
        if getattr(context, "output_format", "natural") == "structured":
            system_prompt = f"{system_prompt}\n{_STRUCTURED_OUTPUT_PROMPT_SUFFIX}"

        return create_agent(
            model=load_chat_model(fully_specified_name=model_spec),
            tools=_filter_disabled_tools(await resolve_configured_runtime_tools(context)),
            system_prompt=system_prompt,
            middleware=await _build_middlewares(context),
            state_schema=BaseState,
            checkpointer=await self._get_checkpointer(),
        )
