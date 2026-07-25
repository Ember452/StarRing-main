"""子智能体 task 工具中间件（Orchestrator-Worker 模式核心）。

实现父智能体通过 ``task`` 工具委派子任务给子智能体的完整链路：
- 在父智能体 system prompt 中注入 ``TASK_SYSTEM_PROMPT``（task 工具使用规范）
- 注册 ``task`` StructuredTool，父智能体调用时创建子 AgentRun 并 enqueue 执行
- 子 run 终结后从其消息流解析结构化 ``SubAgentDeliverable``，渲染为 markdown 回传父 ToolMessage
- 复用 / 失败 / 取消等场景统一通过 ``Command`` 返回结构化 ToolMessage

核心数据契约：``SubAgentDeliverable``（见 ``subagent_deliverable.py``）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

try:
    from deepagents import SubagentTransformer
except ImportError:
    SubagentTransformer = None  # type: ignore[assignment,misc]

try:
    from deepagents.middleware._utils import append_to_system_message
except ImportError:
    append_to_system_message = None  # type: ignore[assignment]
from langchain.agents.middleware.types import AgentMiddleware, ContextT, ModelRequest, ModelResponse, ResponseT
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from starring.agents.context import build_agent_input_context
from starring.agents.middlewares.subagent_deliverable import (
    EMPTY_DELIVERABLE,
    SubAgentDeliverable,
)
from starring.repositories.agent_repository import SUB_AGENT_BACKEND_ID, AgentRepository
from starring.repositories.agent_run_repository import AgentRunRepository
from starring.repositories.user_repository import UserRepository
from starring.storage.postgres.manager import pg_manager
from starring.storage.postgres.models_business import Agent
from starring.utils import logger
from starring.utils.datetime_utils import utc_isoformat
from starring.utils.subagent_thread_utils import make_child_thread_id

_CHILD_STATE_INHERIT_KEYS: frozenset[str] = frozenset()
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "interrupted"}

# 子智能体结构化输出的 fenced code block 正则
# 用专属标识 "subagent-result" 避免误匹配其他代码块（如 python/json）
_SUBAGENT_RESULT_PATTERN = re.compile(
    r"```subagent-result\s*\n(.*?)\n```",
    re.DOTALL,
)

TASK_SYSTEM_PROMPT = """## `task`（子智能体任务工具）

你可以使用 `task` 工具把复杂、独立的子任务交给已配置的子智能体处理。子智能体只返回最终结果，你看不到它的中间步骤。
工具结果会包含子智能体线程 ID，后续需要继续同一个子任务时，把该 ID 作为 `thread_id` 传回 `task`。

### 使用原则

- 任务足够复杂、可以独立完成、或需要隔离上下文时使用。
- 多个互不依赖的子任务可以并行调用多个 `task`。
- 继续既有子智能体任务时传入之前结果中的 `thread_id`；新任务不要填写 `thread_id`。
- 不要并行调用同一个 `thread_id`，避免多个续跑请求同时写入同一子线程。
- 简单问题或少量直接工具调用不要委派。
- 调用时必须选择下方可用的 `subagent_type`，并在 `description` 中写清目标、上下文和期望输出。
- 不要通过 shell、curl、HTTP API 或命令行间接调用子智能体；需要子智能体时必须使用 `task` 工具。

### Orchestrator-Worker 编排约束

作为 Orchestrator（编排者），你调用 task 工具时必须遵循以下四条约束：

1. **Decompose by aspect, not by step**（按正交维度拆，非按步骤拆）
   - 正确：多源研究 → 「查内部知识库」「查外部网络」「查数据库」并行
   - 错误：「先查 X，再查 Y，最后查 Z」串行（用单次 task 调用 + description 内说明即可，不要拆 3 个 task）

2. **Bound depth**（限制嵌套深度）
   - 子智能体不能再调用 task 工具（系统已强制保证），你不要试图在 description 中指示子智能体再委派

3. **Explicit deliverables**（显式声明期望产物）
   - 每次 task 调用的 description 中必须包含 `Expected deliverable:` 字段，说明期望子智能体返回的内容结构
   - 示例：`Expected deliverable: structured result with summary, key_findings (3-5 items), sources (with file_id), confidence`

4. **Synthesis is reasoning, not concatenation**（合成是推理，不是拼接）
   - 拿到多个子智能体的结果后，不要简单拼接摘要
   - 要评估各子结果的 confidence、找冲突、综合判断、产出统一答案
   - 如果两个子结果冲突，明确指出冲突并说明你的判断

### Effort-scaling 分配规则

根据任务复杂度分配子智能体数量，避免过度并行（token 成本失控）或不足并行（效率低下）：

- **简单任务**（单点查询、单一事实）：1 个子智能体，或不调 task 直接用本地工具
- **中等复杂度**（多步推理、单领域分析）：2-4 个并行子智能体
- **复杂研究任务**（多源综合、跨领域对比）：5-10 个并行子智能体
- **超复杂任务**（10+ 子任务）：先评估是否真有必要，优先考虑拆为多轮对话

### 子智能体返回格式

子智能体会以结构化 markdown 返回结果，包含以下字段：

- **摘要**：1-3 句话概括结果
- **关键发现**：列表
- **引用来源**：file_id / chunk_id / snippet
- **置信度**：0-1
- **产物文件**：沙盒路径
- **原始文本**：兜底完整内容

合成最终回答时，**优先参考摘要和关键发现**，必要时打开产物文件或引用来源验证细节。

### Available subagent types

{available_agents}"""

TASK_TOOL_DESCRIPTION = """Launch a configured starring subagent to handle an isolated task.

Available subagent types:
{available_agents}

Use `subagent_type` to select one available subagent and put the full task brief in `description`.
The `description` MUST include an `Expected deliverable:` field declaring what the subagent should return.
Omit `thread_id` for a new task. To continue a previous subagent task, pass the child thread ID returned by
that prior task result as `thread_id`.
Do not call subagents through shell, curl, HTTP APIs, or command-line indirection."""


TASK_DESCRIPTION_ARG = "需要子智能体独立完成的任务描述，包含必要上下文和期望输出。"
SUBAGENT_TYPE_ARG = "要调用的子智能体标识，必须是工具描述中列出的可用类型之一。"
THREAD_ID_ARG = "可选。要继续的既有子智能体线程 ID，必须来自之前 task 工具结果；新任务不要填写。"


def _get_agent_backend(backend_id: str):
    from starring.agents.buildin import agent_manager

    return agent_manager.get_agent(backend_id)


def _final_assistant_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = message.text.rstrip() if message.text else ""
            if text:
                return text
    return "子智能体已完成任务，但没有返回文本结果。"


def _result_artifacts(result: dict[str, Any]) -> list[str]:
    artifacts = result.get("artifacts")
    return list(artifacts) if isinstance(artifacts, list) else []


def _preview_text(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else f"{text[:limit]}..."


def _tool_result_with_thread_id(child_thread_id: str, content: str) -> str:
    return f"> 子智能体线程 ID: {child_thread_id}\n\n---\n\n{content}"


def _truncate_raw_text(text: str, max_chars: int = 5000) -> str:
    """截断超长 raw_text，避免 state 体积膨胀。

    兜底路径（无 fenced block 或解析失败）的 raw_text 截断到 5KB；
    fenced block 内 LLM 显式声明的 raw_text 字段不截断（LLM 已主动控制长度）。
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]"


def _parse_deliverable(messages: list, artifacts_from_state: list[str]) -> SubAgentDeliverable:
    """从子智能体所有 AIMessage 中解析结构化 deliverable。

    扫描策略：不再只看 _final_assistant_text（最后一个非空 AIMessage.text），
    而是扫描所有 AIMessage.text，拼接后查找 fenced block。
    原因：子智能体可能在 fenced block 后继续输出 AIMessage 解释
    （如"我已完成结构化输出"），最后一个 AIMessage 不是 fenced block。

    解析策略（三层兜底，永远不抛异常）：
    1. 拼接所有 AIMessage.text（倒序优先，最近的最相关）
    2. 优先匹配 ```subagent-result``` fenced block 中的 JSON
    3. JSON 解析失败 → raw_text 保留原文，summary 取首段
    4. Pydantic 校验失败 → 同上兜底
    5. 完全无输出 → EMPTY_DELIVERABLE
    artifacts_from_state 始终保留（来自子智能体 state.artifacts）。
    """
    all_text = "\n\n".join(msg.text for msg in reversed(messages) if isinstance(msg, AIMessage) and msg.text)

    if not all_text:
        return EMPTY_DELIVERABLE.model_copy(update={"artifacts": artifacts_from_state})

    match = _SUBAGENT_RESULT_PATTERN.search(all_text)
    if not match:
        return SubAgentDeliverable(
            summary="",  # validator 会从 raw_text 取首段
            raw_text=_truncate_raw_text(all_text),
            artifacts=artifacts_from_state,
        )

    json_str = match.group(1).strip()
    try:
        payload = json.loads(json_str)
    except json.JSONDecodeError:
        return SubAgentDeliverable(
            summary="",
            raw_text=_truncate_raw_text(all_text),
            artifacts=artifacts_from_state,
        )

    # 合并 artifacts：fenced block 中的优先，state.artifacts 补充
    merged_artifacts = list(dict.fromkeys(list(payload.get("artifacts") or []) + artifacts_from_state))
    payload["artifacts"] = merged_artifacts

    try:
        return SubAgentDeliverable.model_validate(payload)
    except Exception as exc:
        # 兜底不抛异常（设计原则），但必须留下日志便于排查 LLM 输出格式问题
        logger.warning(
            f"subagent deliverable pydantic 校验失败，回退到 raw_text 兜底: {exc}",
            exc_info=True,
        )
        return SubAgentDeliverable(
            summary="",
            raw_text=_truncate_raw_text(all_text),
            artifacts=artifacts_from_state,
        )


def _deliverable_to_markdown(deliverable: SubAgentDeliverable, child_thread_id: str, subagent_type: str) -> str:
    """把结构化 deliverable 渲染为 LLM 友好的 markdown。

    不渲染 raw_text：避免 ToolMessage 体积膨胀，符合 Orchestrator-Worker 减少 token 的目标。
    raw_text 保留在 deliverable.raw_text 状态字段中，供前端/Langfuse 查看；
    父 LLM 如需原文细节，通过 open_kb_document / find_kb_document 工具主动调取。
    """
    lines = [
        f"> 子智能体线程 ID: {child_thread_id}",
        f"> 子智能体类型: {subagent_type}",
        f"> 置信度: {deliverable.confidence:.2f}",
        "",
        "## 摘要",
        deliverable.summary,
        "",
    ]
    if deliverable.key_findings:
        lines.append("## 关键发现")
        lines.extend(f"- {finding}" for finding in deliverable.key_findings)
        lines.append("")
    if deliverable.sources:
        lines.append("## 引用来源")
        for src in deliverable.sources:
            snippet_preview = src.snippet[:200] + ("..." if len(src.snippet) > 200 else "")
            if src.type == "kb_chunk":
                lines.append(f"- [知识库] file_id={src.file_id}, chunk_id={src.chunk_id}: {snippet_preview}")
            elif src.type == "file":
                lines.append(f"- [文件] {src.file_id}: {snippet_preview}")
            elif src.type == "url":
                lines.append(f"- [URL] {src.url}: {snippet_preview}")
            else:
                lines.append(f"- [其他] {snippet_preview}")
        lines.append("")
    if deliverable.artifacts:
        lines.append("## 产物文件")
        lines.extend(f"- {path}" for path in deliverable.artifacts)
        lines.append("")
    return "\n".join(lines)


def _new_child_thread_id(
    requested_thread_id: str | None,
    *,
    parent_thread_id: str,
    agent_slug: str,
    tool_call_id: str,
) -> tuple[str, bool]:
    requested_thread_id = str(requested_thread_id or "").strip()
    if requested_thread_id:
        return requested_thread_id, True
    return make_child_thread_id(parent_thread_id, agent_slug, tool_call_id), False


def _subagent_request_id(parent_run_id: str, child_thread_id: str, tool_call_id: str, agent_slug: str) -> str:
    digest = hashlib.sha256(f"{parent_run_id}:{child_thread_id}:{tool_call_id}:{agent_slug}".encode()).hexdigest()
    return f"subagent:{digest[:48]}"


def _with_run_payload(subagent_run: dict[str, Any], run) -> dict[str, Any]:
    if not run:
        return subagent_run
    return {**subagent_run, **_agent_run_state_payload(run)}


def _completed_tool_response(result: dict[str, Any], tool_call_id: str, subagent_run: dict[str, Any]) -> Command:
    """子 run 成功终结时构造父侧 ToolMessage 响应。

    流程：从 result.messages 解析 ``SubAgentDeliverable`` → 渲染为 markdown
    → 包装为带 child_thread_id 的 ToolMessage → 通过 ``Command`` 返回。
    同时把 deliverable 完整快照写入 ``subagent_run`` 状态供前端/Langfuse 查看。
    """
    # 不再调用 _final_assistant_text，改为直接传 messages 给 _parse_deliverable
    # _final_assistant_text 仍保留（向后兼容其他调用点）
    messages = result.get("messages") or []
    artifacts_from_state = _result_artifacts(result)

    deliverable = _parse_deliverable(messages, artifacts_from_state)

    subagent_run = {
        **subagent_run,
        "status": "completed",
        "completed_at": utc_isoformat(),
        "result_preview": _preview_text(deliverable.summary),
        "error": None,
        "artifacts": deliverable.artifacts,
        "deliverable": deliverable.model_dump(mode="json"),  # 含 raw_text 供前端/Langfuse 查看
    }
    tool_result = _tool_result_with_thread_id(
        subagent_run["child_thread_id"],
        _deliverable_to_markdown(
            deliverable,
            subagent_run["child_thread_id"],
            subagent_run.get("subagent_type", ""),
        ),
    )
    update: dict[str, Any] = {"messages": [ToolMessage(tool_result, tool_call_id=tool_call_id)]}
    if deliverable.artifacts:
        update["artifacts"] = deliverable.artifacts
    update["subagent_runs"] = [subagent_run]
    return Command(update=update)


def _reused_run_response(run, tool_call_id: str, subagent_run: dict[str, Any]) -> Command:
    status = str(getattr(run, "status", "") or "unknown")
    if status == "completed":
        message = "子智能体任务已完成，未重复执行。"
    elif status in _TERMINAL_RUN_STATUSES:
        error_message = str(getattr(run, "error_message", "") or "")
        message = f"子智能体任务已结束，状态：{status}。{error_message}".strip()
    else:
        message = f"子智能体任务已存在，当前状态：{status}，未重复提交。"

    subagent_run = {
        **subagent_run,
        **_agent_run_state_payload(run),
        "status": status,
        "result_preview": _preview_text(message),
    }
    tool_message = ToolMessage(
        _tool_result_with_thread_id(subagent_run["child_thread_id"], message),
        tool_call_id=tool_call_id,
    )
    return Command(update={"messages": [tool_message], "subagent_runs": [subagent_run]})


def _agent_run_state_payload(run) -> dict[str, Any]:
    payload = {
        "run_id": run.id,
        "status": run.status,
        "parent_agent_run_id": run.parent_agent_run_id,
        "created_at": utc_isoformat(run.created_at) if run.created_at else None,
        "completed_at": utc_isoformat(run.finished_at) if run.finished_at else None,
        "error": run.error_message,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _failed_tool_response(error: Exception, tool_call_id: str, subagent_run: dict[str, Any]) -> Command:
    error_text = str(error)
    message = f"子智能体 {subagent_run['subagent_type']} 调用失败：{error_text}"
    # summary 与 result_preview 分离：summary 是 deliverable 简洁描述，result_preview 保留完整错误
    failed_deliverable = SubAgentDeliverable(
        summary="子智能体调用失败",
        confidence=0.0,
        raw_text=message,  # 完整错误信息保留在 raw_text
    )
    tool_result = _tool_result_with_thread_id(subagent_run["child_thread_id"], message)
    update = {
        "messages": [ToolMessage(tool_result, tool_call_id=tool_call_id)],
        "subagent_runs": [
            {
                **subagent_run,
                "status": "failed",
                "completed_at": utc_isoformat(),
                "result_preview": _preview_text(message),
                "error": error_text,
                "artifacts": [],
                "deliverable": failed_deliverable.model_dump(mode="json"),
            }
        ],
    }
    return Command(update=update)


def _state_for_child(
    description: str,
    runtime: ToolRuntime,
    *,
    parent_thread_id: str,
    file_thread_id: str,
    skills_thread_id: str,
    continuing: bool = False,
) -> dict[str, Any]:
    state = {} if continuing else {key: runtime.state[key] for key in _CHILD_STATE_INHERIT_KEYS if key in runtime.state}
    state.update(
        {
            "parent_thread_id": parent_thread_id,
            "file_thread_id": file_thread_id,
            "skills_thread_id": skills_thread_id,
        }
    )
    state["messages"] = [HumanMessage(content=description)]
    return state


def _child_config(
    runtime: ToolRuntime,
    *,
    child_thread_id: str,
    uid: str,
    parent_thread_id: str,
    file_thread_id: str,
    skills_thread_id: str,
    subagent_type: str,
    run_id: str | None = None,
    request_id: str | None = None,
) -> dict:
    parent_config = runtime.config or {}
    config: dict[str, Any] = {}
    if "callbacks" in parent_config:
        config["callbacks"] = parent_config["callbacks"]
    if "tags" in parent_config:
        config["tags"] = parent_config["tags"]
    parent_configurable = (
        parent_config.get("configurable") if isinstance(parent_config.get("configurable"), dict) else {}
    )
    parent_configurable = {
        key: value
        for key, value in parent_configurable.items()
        if not str(key).startswith(("checkpoint_", "__pregel_"))
    }
    config["configurable"] = {
        **parent_configurable,
        "thread_id": child_thread_id,
        "uid": uid,
        "parent_thread_id": parent_thread_id,
        "file_thread_id": file_thread_id,
        "skills_thread_id": skills_thread_id,
        "subagent_type": subagent_type,
        "subagent_thread_id": child_thread_id,
        "subagent_tool_call_id": runtime.tool_call_id,
        "run_id": run_id,
        "request_id": request_id,
        "ls_agent_type": "subagent",
    }
    config["recursion_limit"] = parent_config.get("recursion_limit", 300)
    return config


class StarRingSubAgentMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    def __init__(self, *, parent_context, subagents: list[Agent]) -> None:
        super().__init__()
        self.parent_context = parent_context
        self.subagents = {agent.slug: agent for agent in subagents}
        available_agents = "\n".join(f"- {agent.slug}: {agent.description or agent.name}" for agent in subagents)
        self.system_prompt = TASK_SYSTEM_PROMPT.format(available_agents=available_agents)
        self.tools = [self._build_task_tool(available_agents)]
        self.subagent_names = frozenset(self.subagents)
        self.transformers = (
            [lambda scope: SubagentTransformer(scope, subagent_names=self.subagent_names)]
            if SubagentTransformer is not None
            else []
        )

    async def _create_subagent_run(
        self,
        *,
        child_thread_id: str,
        description: str,
        subagent_type: str,
        agent: Agent,
        uid: str,
        parent_thread_id: str,
        tool_call_id: str,
        continuing: bool,
    ):
        """创建子 AgentRun 记录并标记为 running，供父 run 通过 task 工具委派执行。

        幂等性：基于 ``parent_run_id + child_thread_id + tool_call_id + agent_slug``
        生成 request_id，重复调用返回已存在 run（``continuing=True`` 场景下重连复用）。
        ``continuing=True`` 时额外校验子线程归属与子 agent 类型一致。
        """
        parent_run_id = str(getattr(self.parent_context, "run_id", "") or "").strip()
        if not parent_run_id:
            raise ValueError("当前运行时缺少父运行 ID，无法记录子智能体运行")

        async with pg_manager.get_async_session_context() as db:
            repo = AgentRunRepository(db)
            # 校验父 run 存在且属于当前用户，避免越权创建子 run
            parent_run = await repo.get_run_for_user(parent_run_id, uid)
            if not parent_run:
                raise ValueError("父运行任务不存在")

            # 续跑场景：校验子线程归属当前对话，且 agent 类型与首次一致（防串线）
            if continuing:
                previous = await repo.get_latest_subagent_run_by_thread_for_user(child_thread_id, uid)
                if not previous or previous.conversation_id != parent_run.conversation_id:
                    raise ValueError(
                        f"无法继续子智能体线程 {child_thread_id}：当前对话中没有找到对应的子智能体运行记录"
                    )
                if previous.agent_id != subagent_type:
                    raise ValueError(
                        f"无法继续子智能体线程 {child_thread_id}：该线程属于子智能体 {previous.agent_id or '未知'}"
                    )

            # 幂等键：相同 (parent_run_id, child_thread_id, tool_call_id, agent_slug) 视为同一子 run
            request_id = _subagent_request_id(parent_run_id, child_thread_id, tool_call_id, agent.slug)
            existing = await repo.get_run_by_request_id(request_id)
            if existing:
                # 已存在则直接复用，返回 (run, created=False) 让调用方走 reused_run 分支
                return existing, False

            # 首次创建：input_payload 完整快照 description / tool_call_id / 父子线程关系，供后续审计与 langfuse 追踪
            run = await repo.create_run(
                run_id=str(uuid.uuid4()),
                thread_id=child_thread_id,
                agent_id=subagent_type,
                uid=uid,
                request_id=request_id,
                conversation_id=parent_run.conversation_id,
                parent_agent_run_id=parent_run.id,
                run_type="subagent",
                checkpoint_thread_id=child_thread_id,
                input_payload={
                    "description": description,
                    "tool_call_id": tool_call_id,
                    "subagent_type": subagent_type,
                    "subagent_name": agent.name,
                    "parent_thread_id": parent_thread_id,
                    "child_thread_id": child_thread_id,
                    "continuing": continuing,
                },
            )
            return await repo.mark_running(run.id), True

    async def _set_subagent_run_status(
        self,
        run_id: str,
        status: str,
        *,
        error_type: str | None = None,
        error_message: str | None = None,
    ):
        async with pg_manager.get_async_session_context() as db:
            return await AgentRunRepository(db).set_terminal_status(
                run_id,
                status=status,
                error_type=error_type,
                error_message=error_message,
            )

    def _build_task_tool(self, available_agents: str) -> StructuredTool:
        """构建 ``task`` StructuredTool（父智能体通过它委派子任务）。

        ``available_agents`` 为已格式化的子智能体列表文本，注入到工具 docstring
        供 LLM 选择 ``subagent_type``。同步 ``task`` 仅返回提示（实际逻辑在 ``atask``）。
        """

        def task(
            description: Annotated[str, TASK_DESCRIPTION_ARG],
            subagent_type: Annotated[str, SUBAGENT_TYPE_ARG],
            runtime: ToolRuntime,
            thread_id: Annotated[str | None, THREAD_ID_ARG] = None,
        ) -> str:
            return "task 工具仅支持异步调用"

        async def atask(
            description: Annotated[str, TASK_DESCRIPTION_ARG],
            subagent_type: Annotated[str, SUBAGENT_TYPE_ARG],
            runtime: ToolRuntime,
            thread_id: Annotated[str | None, THREAD_ID_ARG] = None,
        ) -> str | Command:
            if subagent_type not in self.subagents:
                allowed = ", ".join(f"`{slug}`" for slug in self.subagents)
                return f"无法调用子智能体 {subagent_type}，可用子智能体只有：{allowed}"
            if not runtime.tool_call_id:
                raise ValueError("Tool call ID is required for subagent invocation")

            parent_thread_id = str(
                getattr(self.parent_context, "parent_thread_id", None) or self.parent_context.thread_id
            )
            file_thread_id = str(getattr(self.parent_context, "file_thread_id", None) or parent_thread_id)
            uid = str(getattr(self.parent_context, "uid", "") or "").strip()
            if not uid:
                return "无法调用子智能体：当前运行时缺少 uid"

            agent = self.subagents[subagent_type]
            backend = _get_agent_backend(agent.backend_id)
            if not backend or agent.backend_id != SUB_AGENT_BACKEND_ID:
                return f"无法调用子智能体 {subagent_type}：后端配置无效"

            child_thread_id, continuing = _new_child_thread_id(
                thread_id,
                parent_thread_id=parent_thread_id,
                agent_slug=agent.slug,
                tool_call_id=runtime.tool_call_id,
            )
            subagent_run = {
                "id": runtime.tool_call_id,
                "subagent_type": subagent_type,
                "subagent_name": agent.name,
                "child_thread_id": child_thread_id,
                "description": description,
                "created_at": utc_isoformat(),
            }

            try:
                run, is_new_run = await self._create_subagent_run(
                    child_thread_id=child_thread_id,
                    description=description,
                    subagent_type=subagent_type,
                    agent=agent,
                    uid=uid,
                    parent_thread_id=parent_thread_id,
                    tool_call_id=runtime.tool_call_id,
                    continuing=continuing,
                )
            except ValueError as exc:
                return str(exc)
            subagent_run = _with_run_payload(subagent_run, run)
            if not is_new_run:
                return _reused_run_response(run, runtime.tool_call_id, subagent_run)

            child_context = backend.context_schema()
            config_context = (agent.config_json or {}).get("context") if isinstance(agent.config_json, dict) else None
            child_input_context = await build_agent_input_context(
                config_context if isinstance(config_context, dict) else {},
                thread_id=child_thread_id,
                uid=uid,
                run_id=run.id,
                request_id=run.request_id,
            )
            if not str(child_input_context.get("model") or "").strip():
                parent_model = str(getattr(self.parent_context, "model", "") or "").strip()
                if parent_model:
                    child_input_context["model"] = parent_model
            child_context.update_from_dict(child_input_context)
            child_context.uid = uid
            child_context.thread_id = child_thread_id
            child_context.parent_thread_id = parent_thread_id
            child_context.file_thread_id = file_thread_id
            child_context.skills_thread_id = child_thread_id
            child_context.run_id = run.id
            child_context.request_id = run.request_id
            child_context.is_subagent_runtime = True
            child_context.output_format = "structured"

            try:
                graph = await backend.get_graph(context=child_context)
                result = await graph.ainvoke(
                    _state_for_child(
                        description,
                        runtime,
                        parent_thread_id=parent_thread_id,
                        file_thread_id=file_thread_id,
                        skills_thread_id=child_thread_id,
                        continuing=continuing,
                    ),
                    config=_child_config(
                        runtime,
                        child_thread_id=child_thread_id,
                        uid=uid,
                        parent_thread_id=parent_thread_id,
                        file_thread_id=file_thread_id,
                        skills_thread_id=child_thread_id,
                        subagent_type=subagent_type,
                        run_id=run.id,
                        request_id=run.request_id,
                    ),
                    context=child_context,
                )
            except asyncio.CancelledError:
                await self._set_subagent_run_status(run.id, "cancelled")
                raise
            except Exception as exc:
                failed_run = await self._set_subagent_run_status(
                    run.id,
                    "failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                return _failed_tool_response(exc, runtime.tool_call_id, _with_run_payload(subagent_run, failed_run))
            completed_run = await self._set_subagent_run_status(run.id, "completed")
            return _completed_tool_response(
                result, runtime.tool_call_id, _with_run_payload(subagent_run, completed_run)
            )

        return StructuredTool.from_function(
            name="task",
            func=task,
            coroutine=atask,
            description=TASK_TOOL_DESCRIPTION.format(available_agents=available_agents),
            infer_schema=True,
        )

    def _append_system_prompt(self, system_message: str) -> str:
        if append_to_system_message is not None:
            return append_to_system_message(system_message, self.system_prompt)
        return f"{system_message}\n\n{self.system_prompt}" if system_message else self.system_prompt

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        return handler(request.override(system_message=self._append_system_prompt(request.system_message)))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        return await handler(request.override(system_message=self._append_system_prompt(request.system_message)))


async def create_subagent_task_middleware(parent_context) -> StarRingSubAgentMiddleware | None:
    selected_slugs = [
        str(slug).strip() for slug in (getattr(parent_context, "subagents", None) or []) if str(slug).strip()
    ]
    uid = str(getattr(parent_context, "uid", "") or "").strip()
    if not uid:
        return None

    async with pg_manager.get_async_session_context() as db:
        user = await UserRepository().get_by_uid_with_db(db, uid)
        if user is None:
            return None
        repo = AgentRepository(db)
        if selected_slugs:
            subagents: list[Agent] = []
            seen: set[str] = set()
            for slug in selected_slugs:
                if slug in seen:
                    continue
                seen.add(slug)
                agent = await repo.get_visible_subagent_by_slug(slug=slug, user=user)
                if agent and agent.backend_id == SUB_AGENT_BACKEND_ID:
                    subagents.append(agent)
        else:
            subagents = [
                agent
                for agent in await repo.list_visible_subagents(user=user)
                if agent.backend_id == SUB_AGENT_BACKEND_ID
            ]

    if not subagents:
        return None
    return StarRingSubAgentMiddleware(parent_context=parent_context, subagents=subagents)
