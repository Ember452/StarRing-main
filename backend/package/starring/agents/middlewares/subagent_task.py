"""子智能体 task 工具中间件（Orchestrator-Worker 模式核心）。

实现父智能体通过 ``task`` 工具委派子任务给子智能体的完整链路：
- 在父智能体 system prompt 中注入 ``TASK_SYSTEM_PROMPT``（task 工具使用规范）
- 注册 ``task`` StructuredTool，父智能体调用时创建子 AgentRun 并 enqueue 执行
- 子 run 终结后优先从其最终 state 的 ``structured_response``（LLM 原生结构化输出）
  提取 ``SubAgentDeliverable``，缺失时回退消息流正则解析，渲染为 markdown 回传父 ToolMessage
- 复用 / 失败 / 取消等场景统一通过 ``Command`` 返回结构化 ToolMessage

核心数据契约：``SubAgentDeliverable``（见 ``subagent_deliverable.py``）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
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
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from starring.agents.middlewares.subagent_deliverable import (
    EMPTY_DELIVERABLE,
    SubAgentDeliverable,
)
from starring.repositories.agent_repository import SUB_AGENT_BACKEND_ID, AgentRepository
from starring.repositories.agent_run_repository import AgentRunRepository
from starring.repositories.user_repository import UserRepository
from starring.services.run_queue_service import SUBAGENT_QUEUE_NAME, get_arq_pool, publish_cancel_signal
from starring.storage.postgres.manager import pg_manager
from starring.storage.postgres.models_business import Agent
from starring.utils import logger
from starring.utils.datetime_utils import utc_isoformat
from starring.utils.subagent_thread_utils import make_child_thread_id

_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "interrupted"}

# 父侧等待子 run 终结的轮询间隔与默认超时（对齐子 worker job_timeout 3600s，可被 agent 配置覆盖）
_SUBAGENT_POLL_INTERVAL_SECONDS = 0.5
_DEFAULT_SUBAGENT_WAIT_TIMEOUT_SECONDS = 3600.0

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
   - 示例：`Expected deliverable: structured result with summary, key_findings (3-5 items),
     sources (with file_id), confidence`

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


def _deliverable_from_structured_response(result: dict[str, Any]) -> SubAgentDeliverable | None:
    """从 LLM 原生结构化输出（state 的 ``structured_response`` 通道）提取 deliverable。

    ``create_agent(response_format=ToolStrategy(SubAgentDeliverable))`` 会把 LLM 通过
    工具调用产出的实例写入该通道；缺失或校验失败时返回 None，由调用方回退
    ``_parse_deliverable`` 正则解析路径（应对模型不支持 tool calling 的边缘情况）。
    """
    structured = result.get("structured_response")
    if structured is None:
        return None
    if isinstance(structured, SubAgentDeliverable):
        return structured
    try:
        return SubAgentDeliverable.model_validate(structured)
    except Exception as exc:
        logger.warning(f"subagent structured_response 校验失败，回退正则解析路径: {exc}", exc_info=True)
        return None


def _extract_deliverable(result: dict[str, Any]) -> SubAgentDeliverable:
    """从子智能体最终 state 提取交付物：原生结构化输出优先，正则解析降级兜底。

    原生路径补齐两个字段：
    - artifacts：LLM 声明的优先，state.artifacts 补充（去重保序，与正则路径一致）
    - raw_text：LLM 未填时取最终 AIMessage 文本（结构化 tool call 消息可能无文本，允许为空）
    """
    messages = result.get("messages") or []
    artifacts_from_state = _result_artifacts(result)

    deliverable = _deliverable_from_structured_response(result)
    if deliverable is None:
        return _parse_deliverable(messages, artifacts_from_state)

    payload = deliverable.model_dump()
    payload["artifacts"] = list(dict.fromkeys(list(deliverable.artifacts) + artifacts_from_state))
    if not str(payload.get("raw_text") or "").strip():
        final_text = next(
            (msg.text for msg in reversed(messages) if isinstance(msg, AIMessage) and msg.text),
            "",
        )
        payload["raw_text"] = _truncate_raw_text(final_text)
    # 重新 model_validate 以触发 summary 兜底 validator（model_copy 不会重跑校验）
    return SubAgentDeliverable.model_validate(payload)


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


def _completed_tool_response(
    deliverable: SubAgentDeliverable, tool_call_id: str, subagent_run: dict[str, Any]
) -> Command:
    """子 run 成功终结时构造父侧 ToolMessage 响应。

    deliverable 由子 worker 在终结时提取并写入 ``agent_runs.output_payload``，
    父侧读取后渲染为 markdown → 包装为带 child_thread_id 的 ToolMessage
    → 通过 ``Command`` 返回。同时把 deliverable 完整快照写入 ``subagent_run`` 状态供前端/Langfuse 查看。
    """
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


def _cancelled_tool_response(run, tool_call_id: str, subagent_run: dict[str, Any]) -> Command:
    """子 run 被取消（用户直接取消子 run，父 run 仍存活）时的 ToolMessage 响应。"""
    message = "子智能体任务已取消。"
    subagent_run = {
        **subagent_run,
        **_agent_run_state_payload(run),
        "status": "cancelled",
        "completed_at": utc_isoformat(),
        "result_preview": _preview_text(message),
    }
    tool_message = ToolMessage(
        _tool_result_with_thread_id(subagent_run["child_thread_id"], message),
        tool_call_id=tool_call_id,
    )
    return Command(update={"messages": [tool_message], "subagent_runs": [subagent_run]})


def _deliverable_from_output_payload(run) -> SubAgentDeliverable:
    """从子 run 的 ``output_payload.deliverable`` 快照还原 deliverable。

    缺失或校验失败时降级为仅含说明的兜底 deliverable（旧数据 / 异常子 worker 场景）。
    """
    payload = run.output_payload if isinstance(getattr(run, "output_payload", None), dict) else {}
    snapshot = payload.get("deliverable")
    if isinstance(snapshot, dict):
        try:
            return SubAgentDeliverable.model_validate(snapshot)
        except Exception as exc:
            logger.warning(f"子智能体 output_payload.deliverable 校验失败，回退兜底 deliverable: {exc}", exc_info=True)
    return EMPTY_DELIVERABLE.model_copy()


def _terminal_run_response(run, tool_call_id: str, subagent_run: dict[str, Any]) -> Command:
    """按子 run 终态分发 ToolMessage 响应（completed / cancelled / failed / interrupted）。"""
    status = str(getattr(run, "status", "") or "")
    subagent_run = _with_run_payload(subagent_run, run)
    if status == "completed":
        return _completed_tool_response(_deliverable_from_output_payload(run), tool_call_id, subagent_run)
    if status == "cancelled":
        return _cancelled_tool_response(run, tool_call_id, subagent_run)
    error = RuntimeError(str(getattr(run, "error_message", "") or f"子智能体任务终态异常：{status}"))
    return _failed_tool_response(error, tool_call_id, subagent_run)


async def _load_subagent_run_record(run_id: str):
    async with pg_manager.get_async_session_context() as db:
        return await AgentRunRepository(db).get_run(run_id)


def _subagent_wait_timeout_seconds(agent: Agent) -> float:
    """父侧等待超时：agent 配置 ``context.subagent_timeout_seconds`` 覆盖，默认 3600s。"""
    config = agent.config_json if isinstance(agent.config_json, dict) else {}
    context = config.get("context") if isinstance(config.get("context"), dict) else {}
    try:
        timeout = float(context.get("subagent_timeout_seconds"))
    except (TypeError, ValueError):
        return _DEFAULT_SUBAGENT_WAIT_TIMEOUT_SECONDS
    return timeout if timeout > 0 else _DEFAULT_SUBAGENT_WAIT_TIMEOUT_SECONDS


async def _await_subagent_terminal(
    run_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = _SUBAGENT_POLL_INTERVAL_SECONDS,
):
    """轮询等待子 run 进入终态，返回终态 run 记录。

    - 超过 ``timeout_seconds`` 抛 ``TimeoutError``（调用方负责级联取消子 run）
    - 父 run 被取消（``CancelledError``）时级联发布子 run 取消信号后向上传播，
      子 worker 通过 ``RunContext`` 感知信号并自行终结为 cancelled
    """
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            run = await _load_subagent_run_record(run_id)
            if run and run.status in _TERMINAL_RUN_STATUSES:
                return run
            if time.monotonic() >= deadline:
                raise TimeoutError(f"子智能体运行超时（{int(timeout_seconds)} 秒）")
            await asyncio.sleep(poll_interval_seconds)
    except asyncio.CancelledError:
        # 父 run 被取消：级联发布子 run 取消信号（best-effort）后向上传播
        try:
            await publish_cancel_signal(run_id)
        except Exception as exc:
            logger.warning(f"级联取消子智能体 run {run_id} 失败: {exc}")
        raise


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
        file_thread_id: str,
        parent_model: str,
        tool_call_id: str,
        continuing: bool,
    ):
        """创建子 AgentRun 记录（queued 状态），供独立子 worker 消费执行。

        input_payload 完整快照子 worker 重建上下文所需的全部输入
        （description / 父子线程关系 / file_thread_id / parent_model 回退模型），
        running 状态由 ``process_subagent_run`` 任务自行标记。

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
                    "file_thread_id": file_thread_id,
                    "parent_model": parent_model,
                    "continuing": continuing,
                },
            )
            return run, True

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
                    file_thread_id=file_thread_id,
                    parent_model=str(getattr(self.parent_context, "model", "") or "").strip(),
                    tool_call_id=runtime.tool_call_id,
                    continuing=continuing,
                )
            except ValueError as exc:
                return str(exc)
            subagent_run = _with_run_payload(subagent_run, run)
            # 幂等复用：子 run 已终结（重连 / 重复 tool call）直接按终态回传，无需重新入队
            if not is_new_run and run.status in _TERMINAL_RUN_STATUSES:
                return _terminal_run_response(run, runtime.tool_call_id, subagent_run)

            if is_new_run:
                try:
                    queue = await get_arq_pool()
                    await queue.enqueue_job(
                        "process_subagent_run",
                        run.id,
                        _job_id=f"run:{run.id}",
                        _queue_name=SUBAGENT_QUEUE_NAME,
                    )
                except Exception as exc:
                    failed_run = await self._set_subagent_run_status(
                        run.id,
                        "failed",
                        error_type=type(exc).__name__,
                        error_message=f"子任务入队失败：{exc}",
                    )
                    return _failed_tool_response(exc, runtime.tool_call_id, _with_run_payload(subagent_run, failed_run))

            # 等待子 worker 终结子 run（复用 run 非终态时同样等待，_job_id 幂等保证不重复入队）。
            # 父 run 被取消时 CancelledError 在 _await_subagent_terminal 内级联取消子 run 后向上传播。
            try:
                terminal_run = await _await_subagent_terminal(
                    run.id,
                    timeout_seconds=_subagent_wait_timeout_seconds(agent),
                )
            except TimeoutError as exc:
                # 超时级联取消子 run（best-effort），子 worker 收到信号后自行终结为 cancelled
                try:
                    await publish_cancel_signal(run.id)
                except Exception as cancel_exc:
                    logger.warning(f"超时取消子智能体 run {run.id} 失败: {cancel_exc}")
                return _failed_tool_response(exc, runtime.tool_call_id, subagent_run)
            return _terminal_run_response(terminal_run, runtime.tool_call_id, subagent_run)

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
