"""智能体对话服务模块。

本模块是 StarRing 平台对话能力的核心编排层，负责将用户请求转化为对 LangGraph 智能体的调用，
并完成流式/非流式响应、消息持久化、内容安全审计、中断恢复以及子智能体状态查看等能力。

主要对外入口：
    - ``agent_chat``: 非流式对话，返回完整响应
    - ``stream_agent_chat``: 流式对话，按 NDJSON chunk 逐步推送
    - ``stream_agent_resume``: 中断恢复，基于 ``Command(resume=...)`` 继续执行
    - ``get_agent_state_view``: 读取主智能体或子智能体的 checkpoint 状态

模块内部围绕以下职责拆分辅助函数：
    1. 智能体运行时解析（``_resolve_agent_runtime`` 等绑定权限与后端）
    2. Langfuse 追踪上下文构建（``_build_langfuse_run_context`` 等）
    3. LangGraph 流事件 → StarRing 协议事件转换（``_message_*``、``_protocol_*`` 系列）
    4. 消息持久化（``_save_ai_message``、``_save_tool_message``、``save_messages_from_langgraph_state``）
    5. 中断处理（``_extract_interrupt_info``、``_build_ask_user_question_payload``）
    6. 内容安全（与 ``content_guard`` 协作进行输入/输出过滤）
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from langchain.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.types import Command
from starring import config as conf
from starring.agents.buildin import agent_manager
from starring.agents.context import build_agent_input_context, normalize_agent_context_config
from starring.agents.state import AgentStatePayload
from starring.repositories.agent_repository import AgentRepository
from starring.repositories.agent_run_repository import AgentRunRepository
from starring.repositories.conversation_repository import ConversationRepository
from starring.services.conversation_service import serialize_attachment
from starring.services.langfuse_service import (
    LangfuseRunContext,
    build_run_context,
    flush_langfuse,
    get_trace_info,
)
from starring.storage.postgres.manager import pg_manager
from starring.storage.postgres.models_business import Agent, User
from starring.utils.guard import content_guard
from starring.utils.logging_config import logger
from starring.utils.question_utils import (
    normalize_questions as _normalize_interrupt_questions,
)


def _build_state_files(attachments: list[dict]) -> dict:
    """将附件列表转换为 StateBackend 格式的 files 字典

    StateBackend 期望的格式:
    {
        "/attachments/file.md": {
            "content": ["line1", "line2", ...],
            "created_at": "...",
            "modified_at": "...",
        }
    }
    """
    files = {}
    for attachment in attachments:
        if attachment.get("status") != "parsed":
            continue

        file_path = attachment.get("file_path")
        markdown = attachment.get("markdown")

        if not file_path or not markdown:
            continue

        now = datetime.now(UTC).isoformat()
        # 将 markdown 内容按行拆分
        content_lines = markdown.split("\n")
        files[file_path] = {
            "content": content_lines,
            "created_at": attachment.get("uploaded_at", now),
            "modified_at": attachment.get("uploaded_at", now),
        }

    return files


async def _get_langgraph_messages(agent_instance, config_dict):
    """从 LangGraph checkpoint 中读取当前线程的消息列表。

    用于在非流式或事后保存场景中获取完整的对话消息序列。
    若 checkpoint 不存在或为空，返回 ``None`` 以便调用方跳过保存逻辑。
    """
    graph = await agent_instance.get_graph()
    state = await graph.aget_state(config_dict)

    if not state or not state.values:
        logger.warning("No state found in LangGraph")
        return None

    return state.values.get("messages", [])


def _build_langfuse_run_context(
    *,
    current_user,
    thread_id: str,
    agent_id: str,
    request_id: str,
    operation: str,
    backend_id: str | None = None,
    message_type: str | None = None,
    meta: dict | None = None,
) -> LangfuseRunContext:
    """构建 Langfuse 追踪上下文，用于将本次对话的 LLM 调用、工具调用等事件上报到 Langfuse。

    当请求来源是智能体评测（``meta.source == "agent_evaluation"`` 或 ``meta.evaluation`` 非空）时，
    额外注入数据集/实验相关的 metadata 与 tags，便于在 Langfuse 面板按评测维度筛选与对比。
    """
    extra_metadata = None
    extra_tags = None
    evaluation = (meta or {}).get("evaluation") if isinstance(meta, dict) else None
    # 如果请求来自智能体评测，添加评测相关的 metadata 和 tags，方便在 Langfuse 中进行过滤和分析
    if (meta or {}).get("source") == "agent_evaluation" or (isinstance(evaluation, dict) and evaluation):
        extra_metadata = {
            "source": "agent_evaluation",
            "feature": "agent_evaluation",
        }
        extra_tags = ["agent_evaluation"]
        if isinstance(evaluation, dict):
            dataset_name = evaluation.get("dataset_name")
            experiment_name = evaluation.get("experiment_name")
            for key in ("dataset_name", "dataset_item_id", "experiment_name"):
                value = evaluation.get(key)
                if value:
                    extra_metadata[f"evaluation_{key}"] = str(value)
            if dataset_name:
                extra_tags.append(f"dataset:{dataset_name}")
            if experiment_name:
                extra_tags.append(f"experiment:{experiment_name}")

    return build_run_context(
        user_id=str(getattr(current_user, "uid", current_user.id)),
        thread_id=thread_id,
        agent_id=agent_id,
        request_id=request_id,
        operation=operation,
        backend_id=backend_id,
        message_type=message_type,
        username=getattr(current_user, "username", None),
        login_user_id=getattr(current_user, "uid", None),
        department_id=getattr(current_user, "department_id", None),
        extra_metadata=extra_metadata,
        extra_tags=extra_tags,
    )


def extract_agent_state(values: dict) -> AgentStatePayload:
    """从 LangGraph state 中提取 agent 状态。

    返回的 ``AgentStatePayload`` 包含 todos、files、artifacts、subagent_runs、token_usage 五项；
    其中 todos 会被截断到前 20 条，避免前端渲染过载。当 state 非字典时返回空骨架。
    """
    if not isinstance(values, dict):
        return {"todos": [], "files": {}, "artifacts": [], "subagent_runs": [], "token_usage": None}

    # 直接获取，信任 state 的数据结构
    todos = values.get("todos")
    artifacts = values.get("artifacts")
    subagent_runs = values.get("subagent_runs")
    token_usage = values.get("token_usage")
    result: AgentStatePayload = {
        "todos": list(todos)[:20] if todos else [],
        "files": values.get("files") or {},
        "artifacts": list(artifacts) if artifacts else [],
        "subagent_runs": list(subagent_runs) if subagent_runs else [],
        "token_usage": dict(token_usage) if isinstance(token_usage, dict) else None,
    }

    return result


def _agent_state_signature(agent_state: AgentStatePayload | dict | None) -> str:
    """生成 agent_state 的稳定签名串，用于检测状态是否变化。

    流式输出过程中只有当 agent_state 签名发生变化时才会推送 ``agent_state`` chunk，
    以避免重复推送相同状态造成前端抖动与带宽浪费。
    """
    if not agent_state:
        return ""
    try:
        return json.dumps(agent_state, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(agent_state)


def _metadata_thread_id(metadata: dict | None, fallback: str | None = None) -> str | None:
    """从流事件 metadata 中尽力解析出 thread_id。

    LangGraph 的流事件 metadata 嵌套层次较深（configurable / metadata / stream_event 等都可能携带），
    这里依次尝试多个常见来源，取第一个非空字符串值；都取不到则返回 ``fallback``。
    """
    if not isinstance(metadata, dict):
        return fallback

    for source in (
        metadata,
        metadata.get("configurable"),
        metadata.get("metadata"),
        metadata.get("stream_event"),
    ):
        if isinstance(source, dict):
            value = source.get("thread_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def _metadata_namespace(metadata: dict | None) -> list[str]:
    """从 metadata 中读取子智能体命名空间（namespace）。

    namespace 非空表示当前 chunk 来自子智能体，调用方据此区分主/子线程消息并分别路由。
    """
    if not isinstance(metadata, dict):
        return []
    namespace = metadata.get("namespace")
    if isinstance(namespace, list):
        return [str(item) for item in namespace]
    return []


def _json_safe(value: Any) -> Any:
    """递归将任意对象转换为 JSON 可序列化结构。

    支持 dict/list/标量，并自动调用 pydantic 的 ``model_dump`` 处理模型实例；
    其他不可序列化对象退化为字符串表示。
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(child) for child in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    return str(value)


def _apply_model_override(input_context: dict, meta: dict | None) -> None:
    """对话级模型覆盖：meta.model_spec 优先于智能体配置的 model。值已在创建 run 时校验。"""
    model_spec = (meta or {}).get("model_spec")
    model_spec = model_spec.strip() if isinstance(model_spec, str) else model_spec
    if model_spec:
        input_context["model"] = model_spec


def _stream_message_key(metadata: dict | None, namespace: list[str], thread_id: str | None) -> tuple[str, str]:
    """生成消息在流内的唯一键 (thread_id, run_key)。

    run_key 优先取 ``run_id``，其次取 ``langgraph_node``，最后回退到 namespace 拼接路径，
    用于在同一线程下区分来自不同节点/子智能体的消息流。
    """
    if not isinstance(metadata, dict):
        return thread_id or "", "/".join(namespace)
    return thread_id or "", str(metadata.get("run_id") or metadata.get("langgraph_node") or "/".join(namespace))


def _stream_message_id(
    message_ids: dict[tuple[str, str], str],
    key: tuple[str, str],
    preferred: str | None = None,
) -> str:
    """为指定流键分配稳定的 message_id。

    若调用方提供了 ``preferred``（例如协议层的 message-start 携带的 id），则采用并缓存；
    否则按 key 复用已分配的 uuid，保证同一逻辑消息的多个 chunk 拥有相同 message_id，
    前端可据此聚合 delta。
    """
    if preferred:
        message_ids[key] = preferred
        return preferred
    return message_ids.setdefault(key, str(uuid.uuid4()))


def _message_chunk_STARRING_events(
    msg_dict: dict[str, Any],
    *,
    message_id: str,
    thread_id: str | None,
    namespace: list[str],
) -> list[dict[str, Any]]:
    """将 LangChain ``AIMessageChunk`` 的 dict 表示转换为 StarRing 前端协议事件。

    输出事件类型：
        - ``message_delta``: 文本/推理内容的增量
        - ``tool_call_delta``: 工具调用的增量参数（流式生成中的工具入参）

    ``route`` 字段（thread_id + namespace）让前端可区分主智能体与子智能体的消息流。
    """
    events: list[dict[str, Any]] = []
    route = {"thread_id": thread_id, "namespace": namespace}
    content = msg_dict.get("content")
    additional_kwargs = msg_dict.get("additional_kwargs") if isinstance(msg_dict.get("additional_kwargs"), dict) else {}
    reasoning_content = msg_dict.get("reasoning_content")
    additional_reasoning_content = additional_kwargs.get("reasoning_content")

    message_event: dict[str, Any] = {"type": "message_delta", "message_id": message_id, **route}
    if isinstance(content, str) and content:
        message_event["content"] = content
    if isinstance(reasoning_content, str) and reasoning_content:
        message_event["reasoning_content"] = reasoning_content
    if isinstance(additional_reasoning_content, str) and additional_reasoning_content:
        message_event["additional_reasoning_content"] = additional_reasoning_content
    # 仅当事件携带实际内容（字段数 > 4：type/message_id/thread_id/namespace + 至少一个内容字段）时才发送
    if len(message_event) > 4:
        events.append(message_event)

    tool_call_chunks = msg_dict.get("tool_call_chunks")
    if isinstance(tool_call_chunks, list):
        for tool_call_chunk in tool_call_chunks:
            if not isinstance(tool_call_chunk, dict):
                continue
            args_delta = tool_call_chunk.get("args")
            if args_delta is None:
                args_delta = ""
            elif not isinstance(args_delta, str):
                args_delta = json.dumps(args_delta, ensure_ascii=False)
            # 跳过空的占位 chunk（无 id、name、args_delta 三者皆空）
            if not tool_call_chunk.get("id") and not tool_call_chunk.get("name") and not args_delta:
                continue
            events.append(
                {
                    "type": "tool_call_delta",
                    "message_id": message_id,
                    "tool_call_id": tool_call_chunk.get("id"),
                    "name": tool_call_chunk.get("name") or None,
                    "args_delta": args_delta,
                    "index": tool_call_chunk.get("index") if tool_call_chunk.get("index") is not None else 0,
                    **route,
                }
            )
    return events


def _protocol_event_STARRING_event(
    event: dict[str, Any],
    *,
    message_id: str | None,
    thread_id: str | None,
    namespace: list[str],
) -> dict[str, Any] | None:
    """将底层 LLM 协议事件（如 Anthropic content-block 系列）转换为 StarRing 协议事件。

    - ``message-start`` / ``content-block-start`` / ``message-finish`` 仅作为协议控制信号，不产生对外事件；
    - ``content-block-delta`` 转换为 ``message_delta`` 文本增量；
    - ``content-block-finish``（type == tool_call）转换为完整的 ``tool_call`` 事件。

    若事件不属于上述分支或缺少 message_id，返回 ``None`` 表示丢弃。
    """
    event_name = event.get("event")
    if event_name in {"message-start", "content-block-start", "message-finish"} or not message_id:
        return None

    route = {"thread_id": thread_id, "namespace": namespace}
    if event_name == "content-block-delta":
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        text = delta.get("text")
        if delta.get("type") == "text-delta" and isinstance(text, str) and text:
            return {"type": "message_delta", "message_id": message_id, "content": text, **route}
        return None

    if event_name == "content-block-finish":
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        if content.get("type") != "tool_call" or not content.get("id") and not content.get("name"):
            return None
        return {
            "type": "tool_call",
            "message_id": message_id,
            "tool_call_id": content.get("id"),
            "name": content.get("name"),
            "args": content.get("args") if content.get("args") is not None else {},
            "index": event.get("index") if event.get("index") is not None else 0,
            **route,
        }

    return None


def _stream_event_response(event: dict[str, Any]) -> str:
    """从 StarRing 流事件中提取纯文本内容，用于内容安全检查的累积拼接。"""
    if event.get("type") != "message_delta":
        return ""
    return str(event.get("content") or "")


def _message_payload_STARRING_events(
    msg: Any,
    *,
    metadata: dict[str, Any],
    namespace: list[str],
    thread_id: str | None,
    protocol_message_ids: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    """将 LangGraph 推送的一条消息统一转换为 StarRing 协议事件列表。

    分支：
        1. 若 ``msg`` 是协议层事件 dict（含 ``event`` 字段），走 ``_protocol_event_STARRING_event``；
        2. 否则视为 LangChain 消息 chunk（``AIMessageChunk`` / dict / 其他），走 ``_message_chunk_STARRING_events``。

    ``protocol_message_ids`` 用于在同一流键下保持 message_id 稳定，使前端能正确聚合 delta。
    """
    message_key = _stream_message_key(metadata, namespace, thread_id)
    if isinstance(msg, dict) and isinstance(msg.get("event"), str):
        preferred_message_id = str(msg["id"]) if msg.get("event") == "message-start" and msg.get("id") else None
        message_id = _stream_message_id(protocol_message_ids, message_key, preferred_message_id)
        stream_event = _protocol_event_STARRING_event(
            msg,
            message_id=message_id,
            thread_id=thread_id,
            namespace=namespace,
        )
        return [stream_event] if stream_event else []

    if isinstance(msg, AIMessageChunk) or hasattr(msg, "model_dump"):
        msg_dict = msg.model_dump()
    elif isinstance(msg, dict):
        msg_dict = dict(msg)
    else:
        msg_dict = {"content": str(msg)}

    message_id = str(msg_dict.get("id") or _stream_message_id(protocol_message_ids, message_key))
    return _message_chunk_STARRING_events(
        msg_dict,
        message_id=message_id,
        thread_id=thread_id,
        namespace=namespace,
    )


async def _stream_agent_events(agent, messages, *, input_context=None, **kwargs):
    """统一智能体流式事件来源。

    优先使用带状态推送的 ``stream_messages_with_state``（会额外产生 ``values`` / ``stream_event`` 模式），
    否则回退到普通 ``stream_messages`` 并以 ``(msg, metadata)`` 形式产出 ``messages`` 模式。
    """
    if hasattr(agent, "stream_messages_with_state"):
        async for mode, payload in agent.stream_messages_with_state(
            messages,
            input_context=input_context,
            **kwargs,
        ):
            yield mode, payload
        return

    async for msg, metadata in agent.stream_messages(messages, input_context=input_context, **kwargs):
        yield "messages", (msg, metadata)


async def _get_existing_message_ids(conv_repo: ConversationRepository, thread_id: str) -> set[str]:
    """获取当前线程已持久化消息的 id 集合，用于避免从 state 重复保存已有消息。"""
    existing_messages = await conv_repo.get_messages_by_thread_id(thread_id)
    return {
        msg.extra_metadata["id"]
        for msg in existing_messages
        if msg.extra_metadata and "id" in msg.extra_metadata and isinstance(msg.extra_metadata["id"], str)
    }


async def _save_ai_message(
    conv_repo: ConversationRepository,
    thread_id: str,
    msg_dict: dict,
    trace_info: dict[str, Any] | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
):
    """将 LangGraph state 中的一条 AI 消息落库，并注册其附带的工具调用。

    处理要点：
        - content 为列表时（多模态/混合内容），抽取 ``tool_call`` 项作为工具调用，
          其余 ``text`` 项拼接为纯文本内容；
        - trace_info 合并进 extra_metadata，便于后续在 Langfuse 关联；
        - 工具调用以 ``pending`` 状态写入，待 ``_save_tool_message`` 在收到结果时回填。
    """
    content = msg_dict.get("content", "")
    tool_calls_data = msg_dict.get("tool_calls") or []
    if isinstance(content, list):
        if not tool_calls_data:
            tool_calls_data = [
                {"id": item.get("id"), "name": item.get("name"), "args": item.get("args") or {}}
                for item in content
                if isinstance(item, dict) and item.get("type") == "tool_call"
            ]
        content = "\n".join(
            item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    elif not isinstance(content, str):
        content = str(content)
    extra_metadata = dict(msg_dict)
    if trace_info:
        extra_metadata.update(trace_info)

    ai_msg = await conv_repo.add_message_by_thread_id(
        thread_id=thread_id,
        role="assistant",
        content=content,
        message_type="text",
        extra_metadata=extra_metadata,
        run_id=run_id,
        request_id=request_id,
    )

    if ai_msg and tool_calls_data:
        for tc in tool_calls_data:
            await conv_repo.add_tool_call(
                message_id=ai_msg.id,
                tool_name=tc.get("name") or "unknown",
                tool_input=tc.get("args", {}),
                status="pending",
                langgraph_tool_call_id=tc.get("id"),
            )

    return ai_msg


async def _save_tool_message(conv_repo: ConversationRepository, msg_dict: dict) -> None:
    """将工具返回消息回填到对应的 tool_call 记录。

    通过 ``tool_call_id`` 定位此前 ``_save_ai_message`` 创建的 pending 工具调用，
    将其状态更新为 ``success`` 并写入工具输出。无 tool_call_id 时直接跳过。
    """
    tool_call_id = msg_dict.get("tool_call_id")
    content = msg_dict.get("content", "")

    if not tool_call_id:
        return

    if isinstance(content, list):
        tool_output = json.dumps(content) if content else ""
    else:
        tool_output = str(content)

    await conv_repo.update_tool_call_output(
        langgraph_tool_call_id=tool_call_id,
        tool_output=tool_output,
        status="success",
    )


async def save_partial_message(
    conv_repo: ConversationRepository,
    thread_id: str,
    full_msg=None,
    error_message: str | None = None,
    error_type: str = "interrupted",
    trace_info: dict[str, Any] | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
):
    """保存一次未正常完成的助手消息（中断/出错）。

    将 ``error_type``、``error_message`` 写入 extra_metadata 标记为错误消息，
    便于前端识别"对话已中断"等异常状态；若有部分累积内容也一并落库，避免丢失已生成的文本。
    """
    try:
        extra_metadata = {
            "error_type": error_type,
            "is_error": True,
            "error_message": error_message or f"发生错误: {error_type}",
        }
        if full_msg:
            msg_dict = full_msg.model_dump() if hasattr(full_msg, "model_dump") else {}
            content = full_msg.content if hasattr(full_msg, "content") else str(full_msg)
            extra_metadata = msg_dict | extra_metadata
        else:
            content = ""

        if trace_info:
            extra_metadata.update(trace_info)

        return await conv_repo.add_message_by_thread_id(
            thread_id=thread_id,
            role="assistant",
            content=content,
            message_type="text",
            extra_metadata=extra_metadata,
            run_id=run_id,
            request_id=request_id,
        )

    except Exception as e:
        logger.exception(f"Error saving message: {e}")
        return None


async def save_messages_from_langgraph_state(
    agent_instance,
    thread_id: str,
    conv_repo: ConversationRepository,
    config_dict: dict,
    trace_info: dict[str, Any] | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
) -> None:
    """从 LangGraph checkpoint 中拉取全部消息并增量落库。

    流程：
        1. 读取 checkpoint 中的 messages；
        2. 获取已落库消息 id 集合，跳过已存在消息和 human 消息（human 在请求时已单独保存）；
        3. AI 消息走 ``_save_ai_message``，tool 消息走 ``_save_tool_message``；
        4. 若提供 ``run_id``，将最后一条 AI 消息标记为该 run 的输出，并提交事务。
    """
    messages = await _get_langgraph_messages(agent_instance, config_dict)
    if messages is None:
        return

    existing_ids = await _get_existing_message_ids(conv_repo, thread_id)

    last_ai_message = None
    for msg in messages:
        if hasattr(msg, "model_dump"):
            msg_dict = msg.model_dump()
        elif isinstance(msg, dict):
            msg_dict = dict(msg)
        else:
            continue

        msg_type = msg_dict.get("type", "unknown")
        # 兼容部分消息没有 type 字段、只有 role 的情况
        if msg_type == "unknown":
            role = msg_dict.get("role")
            if role in {"assistant", "ai"}:
                msg_type = "ai"
            elif role in {"user", "human"}:
                msg_type = "human"
            elif role == "tool":
                msg_type = "tool"

        msg_id = getattr(msg, "id", None) or msg_dict.get("id")
        if msg_type == "human" or msg_id in existing_ids:
            continue

        if msg_type == "ai":
            last_ai_message = await _save_ai_message(
                conv_repo,
                thread_id,
                msg_dict,
                trace_info=trace_info,
                run_id=run_id,
                request_id=request_id,
            )
        elif msg_type == "tool":
            await _save_tool_message(conv_repo, msg_dict)

    if run_id and last_ai_message:
        run_repo = AgentRunRepository(conv_repo.db)
        await run_repo.set_output_message(run_id, last_ai_message.id)
        await conv_repo.db.commit()


def _extract_interrupt_info(state) -> Any | None:
    """从 LangGraph state 中提取中断信息"""
    if hasattr(state, "tasks") and state.tasks:
        for task in state.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                return task.interrupts[0]

    interrupt_data = state.values.get("__interrupt__")
    if isinstance(interrupt_data, list) and interrupt_data:
        return interrupt_data[0]

    return None


def _coerce_interrupt_payload(info: Any) -> dict:
    """将 LangGraph interrupt 对象转换为 dict 结构。"""
    if isinstance(info, dict):
        return info

    payload = getattr(info, "value", None)
    if isinstance(payload, dict):
        return payload

    questions = getattr(info, "questions", None)
    source = getattr(info, "source", None)
    result: dict[str, Any] = {}
    if isinstance(questions, list):
        result["questions"] = questions
    if isinstance(source, str) and source.strip():
        result["source"] = source
    return result


def _build_ask_user_question_payload(info: Any, thread_id: str) -> dict[str, Any]:
    """将 interrupt 信息标准化为 ask_user_question_required 载荷。"""
    payload = _coerce_interrupt_payload(info)

    questions = _normalize_interrupt_questions(payload.get("questions"))

    if not questions:
        questions = [
            {
                "question_id": str(uuid.uuid4()),
                "question": "请选择一个选项",
                "options": [],
                "multi_select": False,
                "allow_other": True,
            }
        ]

    source = str(payload.get("source") or payload.get("tool_name") or "interrupt")

    return {
        "questions": questions,
        "source": source,
        "thread_id": thread_id,
    }


def _ensure_full_msg(full_msg: AIMessage | None, accumulated_content: list[str]) -> AIMessage | None:
    """如果 full_msg 为空且有累积内容，构建 AIMessage"""
    if not full_msg and accumulated_content:
        return AIMessage(content="".join(accumulated_content))
    return full_msg


def _extract_ai_message(messages: list[Any] | None) -> AIMessage | None:
    """从消息列表中提取最后一条 AIMessage。"""
    if not isinstance(messages, list):
        return None

    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg

        msg_dict = msg.model_dump() if hasattr(msg, "model_dump") else {}
        if msg_dict.get("type") == "ai":
            content = msg_dict.get("content", "")
            return msg if hasattr(msg, "content") else AIMessage(content=content)

    return None


async def _resolve_agent_runtime(
    *,
    db,
    user: User,
    requested_agent_id: str | None,
    thread_id: str | None,
) -> tuple[Agent, Any, dict]:
    """解析本次对话要使用的智能体运行时三元组 (agent_item, backend, agent_config)。

    绑定规则：
        - 若提供 ``thread_id`` 且线程已存在，则使用线程已绑定的 agent_id；
          用户不匹配或线程已删除则报错；线程已绑定其他智能体则禁止切换。
        - 否则使用 ``requested_agent_id``，必须存在。

    之后通过 ``agent_manager`` 获取后端实例，并基于后端 ``context_schema`` 规范化智能体配置。
    """
    agent_repo = AgentRepository(db)
    conv_repo = ConversationRepository(db)
    bound_agent_id = requested_agent_id

    if thread_id:
        conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
        if conversation:
            if conversation.uid != str(user.uid) or conversation.status == "deleted":
                raise ValueError("对话线程不存在")
            if requested_agent_id and requested_agent_id != conversation.agent_id:
                raise ValueError("已有线程已绑定智能体，不能切换")
            bound_agent_id = conversation.agent_id

    if not bound_agent_id:
        raise ValueError("缺少必需的 agent_id 字段")

    agent_item = await agent_repo.get_visible_by_slug(slug=bound_agent_id, user=user)
    if not agent_item:
        raise ValueError("智能体不存在或无权限访问")

    backend = agent_manager.get_agent(agent_item.backend_id)
    if not backend:
        raise ValueError(f"智能体后端 {agent_item.backend_id} 不存在")

    agent_config = await normalize_agent_context_config(
        (agent_item.config_json or {}).get("context", {}),
        db=db,
        user=user,
        context_schema=backend.context_schema,
    )
    return agent_item, backend, agent_config


async def check_and_handle_interrupts(
    agent,
    langgraph_config: dict,
    make_chunk,
    meta: dict,
    thread_id: str,
) -> AsyncIterator[bytes]:
    """检测 LangGraph 的 interrupt 状态，如有则推送 ``ask_user_question_required`` chunk。

    当智能体执行到 ``interrupt`` 节点（例如主动向用户提问）时，从 state.tasks 或 ``__interrupt__``
    中提取中断信息，标准化为问题载荷并下发，前端据此渲染选择题等交互组件。
    任何异常都被捕获并记录，避免中断检查失败影响主流程。
    """
    try:
        graph = await agent.get_graph()
        state = await graph.aget_state(langgraph_config)

        if not state or not state.values:
            return

        interrupt_info = _extract_interrupt_info(state)
        if interrupt_info:
            question_payload = _build_ask_user_question_payload(interrupt_info, thread_id)
            meta["interrupt"] = question_payload
            yield make_chunk(status="ask_user_question_required", meta=meta, **question_payload)

    except Exception as e:
        logger.exception(f"Error checking interrupts: {e}")


async def _ensure_thread_bound_agent(
    *,
    conv_repo: ConversationRepository,
    thread_id: str,
    uid: str,
    agent_item: Agent,
) -> None:
    """确保线程已绑定到指定智能体。

    若线程不存在则创建新对话并绑定当前 agent；若已存在但绑定到其他 agent 则报错，
    避免在历史线程中切换智能体导致上下文混乱。
    """
    conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
    if not conversation:
        await conv_repo.create_conversation(
            uid=uid,
            agent_id=agent_item.slug,
            thread_id=thread_id,
            metadata={"backend_id": agent_item.backend_id},
        )
        return

    if conversation.agent_id != agent_item.slug:
        raise ValueError("已有线程已绑定智能体，不能切换")


def _normalize_attachment_file_ids(meta: dict | None) -> list[str]:
    """从 meta 中规整 attachment_file_ids：去空白、去重、保序。

    非列表类型返回空列表，调用方可据此区分"无附件"与"显式传入空列表"。
    """
    file_ids = (meta or {}).get("attachment_file_ids") or []
    if not isinstance(file_ids, list):
        return []

    normalized = []
    seen = set()
    for file_id in file_ids:
        value = str(file_id).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


async def _bind_request_attachments(
    *,
    conv_repo: ConversationRepository,
    thread_id: str,
    request_id: str,
    attachment_file_ids: list[str],
) -> list[dict]:
    """为当前请求绑定附件并返回序列化结果。

    若调用方显式提供了 ``attachment_file_ids``，则将它们绑定到该 request_id；
    否则查询该 request 已有的附件（例如在创建 run 时已绑定的附件）。
    线程不存在时返回空列表。
    """
    conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
    if not conversation:
        return []

    if attachment_file_ids:
        attachments = await conv_repo.bind_attachments_to_request(conversation.id, request_id, attachment_file_ids)
    else:
        attachments = await conv_repo.get_attachments_by_request_id(conversation.id, request_id)

    return [serialize_attachment(attachment) for attachment in attachments]


async def agent_chat(
    *,
    query: str,
    agent_id: str,
    thread_id: str | None,
    meta: dict,
    image_content: str | None,
    current_user,
    db,
) -> dict:
    """非流式对话入口：一次性调用智能体并返回完整响应。

    与 ``stream_agent_chat`` 共享相同的预处理流程（敏感词校验、运行时解析、Langfuse 追踪、
    线程绑定、附件绑定、用户消息落库），区别在于调用 ``invoke_messages`` 同步获取完整结果，
    并在输出敏感词检测通过后从 state 拉取消息落库。

    返回字典的 ``status`` 可为：
        - ``finished``: 正常完成
        - ``interrupted``: 输出触发敏感词，已中断并保存部分内容
        - ``error``: 智能体解析失败 / 消息保存失败 / 其他未预期错误
    """
    start_time = asyncio.get_event_loop().time()

    # 构造用户消息：含图则使用多模态 content 结构，否则为纯文本
    if image_content:
        human_message = HumanMessage(
            content=[
                {"type": "text", "text": query},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_content}"}},
            ]
        )
        message_type = "multimodal_image"
    else:
        human_message = HumanMessage(content=query)
        message_type = "text"

    # 输入侧敏感词拦截
    if conf.enable_content_guard and await content_guard.check(query):
        return {
            "status": "error",
            "error_type": "content_guard_blocked",
            "error_message": "输入内容包含敏感词",
            "request_id": meta.get("request_id"),
        }

    uid = str(current_user.uid)
    meta = dict(meta or {})
    # 兜底：确保 request_id / thread_id 存在，便于后续关联与落库
    if "request_id" not in meta or not meta.get("request_id"):
        logger.warning("请求缺少 request_id，已自动生成一个新的 request_id")
        meta["request_id"] = str(uuid.uuid4())

    if not thread_id:
        thread_id = str(uuid.uuid4())
        logger.warning(f"No thread_id provided, generated new thread_id: {thread_id}")

    try:
        agent_item, agent, agent_config = await _resolve_agent_runtime(
            db=db,
            user=current_user,
            requested_agent_id=agent_id,
            thread_id=thread_id,
        )
    except ValueError as e:
        return {
            "status": "error",
            "error_type": "invalid_agent",
            "error_message": str(e),
            "request_id": meta.get("request_id"),
        }

    # 将运行时上下文写入 meta，便于 Langfuse / 日志按维度筛选
    meta.update(
        {
            "query": query,
            "agent_id": agent_item.slug,
            "backend_id": agent_item.backend_id,
            "server_model_name": agent_item.backend_id,
            "thread_id": thread_id,
            "uid": current_user.uid,
            "has_image": bool(image_content),
        }
    )

    messages = [human_message]
    input_context = await build_agent_input_context(
        agent_config,
        thread_id=thread_id,
        uid=uid,
        run_id=meta.get("run_id"),
        request_id=meta.get("request_id"),
    )
    _apply_model_override(input_context, meta)
    langfuse_run = _build_langfuse_run_context(
        current_user=current_user,
        thread_id=thread_id,
        agent_id=agent_item.slug,
        backend_id=agent_item.backend_id,
        request_id=meta["request_id"],
        operation="agent_chat_sync",
        message_type=message_type,
        meta=meta,
    )
    trace_info: dict[str, Any] = {}

    try:
        conv_repo = ConversationRepository(db)
        await _ensure_thread_bound_agent(
            conv_repo=conv_repo,
            thread_id=thread_id,
            uid=uid,
            agent_item=agent_item,
        )

        request_attachments = await _bind_request_attachments(
            conv_repo=conv_repo,
            thread_id=thread_id,
            request_id=meta["request_id"],
            attachment_file_ids=_normalize_attachment_file_ids(meta),
        )

        # 用户消息落库（失败仅记录日志，不阻断主流程）
        try:
            await conv_repo.add_message_by_thread_id(
                thread_id=thread_id,
                role="user",
                content=query,
                message_type=message_type,
                image_content=image_content,
                extra_metadata={
                    "raw_message": human_message.model_dump(),
                    "request_id": meta.get("request_id"),
                    "attachments": request_attachments,
                },
            )
        except Exception as e:
            logger.error(f"Error saving user message: {e}")

        langgraph_config = {"configurable": {"thread_id": thread_id, "uid": uid}}
        # 同步调用智能体（非流式），LangGraph 会通过 checkpointer 自动持久化 state
        invoke_result = await agent.invoke_messages(
            messages,
            input_context=input_context,
            callbacks=langfuse_run.callbacks,
            metadata=langfuse_run.metadata,
            tags=langfuse_run.tags,
        )
        full_msg = _extract_ai_message(invoke_result.get("messages") if isinstance(invoke_result, dict) else None)
        trace_info = get_trace_info(langfuse_run)

        # invoke_messages 未返回 AI 消息时，回退到 checkpoint 读取
        if full_msg is None:
            try:
                graph = await agent.get_graph()
                state = await graph.aget_state(langgraph_config)
                full_msg = _extract_ai_message(getattr(state, "values", {}).get("messages", [])) if state else None
            except Exception:
                full_msg = None

        full_content = full_msg.content if full_msg else ""

        # 输出侧敏感词拦截：触发则保存部分消息并返回 interrupted
        if conf.enable_content_guard and await content_guard.check(full_content):
            await save_partial_message(
                conv_repo,
                thread_id,
                full_msg,
                "content_guard_blocked",
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
            return {
                "status": "interrupted",
                "message": "检测到敏感内容，已中断输出",
                "request_id": meta.get("request_id"),
                "time_cost": asyncio.get_event_loop().time() - start_time,
            }

        # 读取 agent_state 供前端展示 todos/artifacts 等
        try:
            graph = await agent.get_graph()
            state = await graph.aget_state(langgraph_config)
            agent_state = extract_agent_state(getattr(state, "values", {})) if state else {}
        except Exception:
            agent_state = {}

        try:
            await save_messages_from_langgraph_state(
                agent_instance=agent,
                thread_id=thread_id,
                conv_repo=conv_repo,
                config_dict=langgraph_config,
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
        except Exception as e:
            logger.exception(f"Error saving messages from LangGraph state: {e}")
            return {
                "status": "error",
                "error_type": "save_message_error",
                "error_message": f"消息保存失败: {e}",
                "request_id": meta.get("request_id"),
            }

        return {
            "status": "finished",
            "response": full_content,
            "request_id": meta.get("request_id"),
            "thread_id": thread_id,
            "agent_state": agent_state,
            "time_cost": asyncio.get_event_loop().time() - start_time,
        }

    except Exception as e:
        logger.exception(f"Error in agent_chat: {e}")
        return {
            "status": "error",
            "error_type": "unexpected_error",
            "error_message": str(e),
            "request_id": meta.get("request_id"),
        }
    finally:
        # 确保 Langfuse 缓冲的事件被推送到远端
        flush_langfuse()


async def stream_agent_chat(
    *,
    query: str,
    agent_id: str,
    thread_id: str | None,
    meta: dict,
    image_content: str | None,
    current_user,
    db,
    save_user_message: bool = True,
) -> AsyncIterator[bytes]:
    """流式对话入口：按 NDJSON chunk 持续推送对话进展。

    输出 chunk 的 ``status`` 序列大致为：
        ``init`` → (多个 ``loading`` / ``agent_state`` / ``stream_event``) → ``finished``

    期间会实时进行内容安全检查（基于最近若干个 chunk 的累积内容），
    一旦命中敏感词则中断流并保存已生成的部分内容。

    客户端断连（``CancelledError`` / ``ConnectionError``）时通过 ``asyncio.shield``
    在新 session 中保存中断消息，避免丢失上下文；其他异常同样会保存错误消息并返回 ``error`` chunk。
    """
    start_time = asyncio.get_event_loop().time()

    def make_chunk(content=None, **kwargs):
        """构造一个 NDJSON chunk（以 ``\\n`` 结尾的字节串）。

        ``thread_id`` 优先取 kwargs 中显式传入（用于子智能体 chunk），
        其次取 meta 中的，最后回退到外层 thread_id。
        """
        chunk_thread_id = kwargs.pop("thread_id", None) or meta.get("thread_id") or thread_id
        return (
            json.dumps(
                {"request_id": meta.get("request_id"), "response": content, "thread_id": chunk_thread_id, **kwargs},
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )

    meta = dict(meta or {})
    if "request_id" not in meta or not meta.get("request_id"):
        logger.warning("请求缺少 request_id，已自动生成一个新的 request_id")
        meta["request_id"] = str(uuid.uuid4())

    uid = str(current_user.uid)
    if not thread_id:
        thread_id = str(uuid.uuid4())
        logger.warning(f"No thread_id provided, generated new thread_id: {thread_id}")

    # 构造用户消息（含图时使用多模态 content）
    if image_content:
        human_message = HumanMessage(
            content=[
                {"type": "text", "text": query},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_content}"}},
            ]
        )
        message_type = "multimodal_image"
    else:
        human_message = HumanMessage(content=query)
        message_type = "text"

    # 输入侧敏感词拦截
    if conf.enable_content_guard and await content_guard.check(query):
        yield make_chunk(
            status="error", error_type="content_guard_blocked", error_message="输入内容包含敏感词", meta=meta
        )
        return

    try:
        agent_item, agent, agent_config = await _resolve_agent_runtime(
            db=db,
            user=current_user,
            requested_agent_id=agent_id,
            thread_id=thread_id,
        )
    except ValueError as e:
        yield make_chunk(status="error", error_type="invalid_agent", error_message=str(e), meta=meta)
        return

    meta.update(
        {
            "query": query,
            "agent_id": agent_item.slug,
            "backend_id": agent_item.backend_id,
            "server_model_name": agent_item.backend_id,
            "thread_id": thread_id,
            "uid": current_user.uid,
            "has_image": bool(image_content),
        }
    )

    messages = [human_message]
    input_context = await build_agent_input_context(
        agent_config,
        thread_id=thread_id,
        uid=uid,
        run_id=meta.get("run_id"),
        request_id=meta.get("request_id"),
    )
    _apply_model_override(input_context, meta)
    langfuse_run = _build_langfuse_run_context(
        current_user=current_user,
        thread_id=thread_id,
        agent_id=agent_item.slug,
        backend_id=agent_item.backend_id,
        request_id=meta["request_id"],
        operation="agent_chat_stream",
        message_type=message_type,
        meta=meta,
    )
    # 累积状态：full_msg / accumulated_content 用于内容安全检查与最终落库
    full_msg = None
    accumulated_content: list[str] = []
    trace_info: dict[str, Any] = {}
    last_agent_state_signature = ""

    try:
        conv_repo = ConversationRepository(db)
        await _ensure_thread_bound_agent(
            conv_repo=conv_repo,
            thread_id=thread_id,
            uid=uid,
            agent_item=agent_item,
        )

        request_attachments = await _bind_request_attachments(
            conv_repo=conv_repo,
            thread_id=thread_id,
            request_id=meta["request_id"],
            attachment_file_ids=_normalize_attachment_file_ids(meta),
        )

        # init chunk：先下发用户消息元信息，让前端立即显示用户气泡
        init_msg = {
            "role": "user",
            "content": query,
            "type": "human",
            "message_type": message_type,
            "extra_metadata": {
                "request_id": meta.get("request_id"),
                "attachments": request_attachments,
            },
        }
        if image_content:
            init_msg["image_content"] = image_content
        yield make_chunk(status="init", meta=meta, msg=init_msg)

        # 用户消息落库（可通过 save_user_message=False 关闭，例如评测场景）
        if save_user_message:
            try:
                await conv_repo.add_message_by_thread_id(
                    thread_id=thread_id,
                    role="user",
                    content=query,
                    message_type=message_type,
                    image_content=image_content,
                    extra_metadata={
                        "raw_message": human_message.model_dump(),
                        "request_id": meta.get("request_id"),
                        "attachments": request_attachments,
                    },
                )
            except Exception as e:
                logger.error(f"Error saving user message: {e}")

        # 先构建 langgraph_config
        langgraph_config = {"configurable": {"thread_id": thread_id, "uid": uid}}

        # LangGraph 会自动从 checkpointer 恢复 state（包括 uploads）
        # 无需手动加载或传递

        full_msg = None
        accumulated_content = []
        # protocol_message_ids 用于在同一 (thread_id, run_key) 下保持 message_id 稳定
        protocol_message_ids: dict[tuple[str, str], str] = {}
        async for mode, payload in _stream_agent_events(
            agent,
            messages,
            input_context=input_context,
            callbacks=langfuse_run.callbacks,
            metadata=langfuse_run.metadata,
            tags=langfuse_run.tags,
        ):
            # values 模式：state 快照更新，仅在签名变化时下发 agent_state
            if mode == "values":
                agent_state = extract_agent_state(payload if isinstance(payload, dict) else {})
                signature = _agent_state_signature(agent_state)
                if signature and signature != last_agent_state_signature:
                    last_agent_state_signature = signature
                    yield make_chunk(status="agent_state", agent_state=agent_state, meta=meta)
                continue

            # stream_event 模式：透传智能体底层协议事件（如工具调用进度）
            if mode == "stream_event":
                yield make_chunk(
                    status="stream_event",
                    event=payload,
                    namespace=payload.get("namespace") if isinstance(payload, dict) else [],
                    meta=meta,
                    thread_id=payload.get("thread_id") if isinstance(payload, dict) else None,
                )
                continue

            # messages 模式：常规消息 chunk（含主智能体与子智能体）
            msg, metadata = payload
            namespace = _metadata_namespace(metadata)
            chunk_thread_id = _metadata_thread_id(metadata, thread_id if not namespace else None)
            # 子智能体 chunk 必须能解析出 thread_id，否则丢弃
            if namespace and not chunk_thread_id:
                continue

            is_subagent_chunk = bool(chunk_thread_id and chunk_thread_id != thread_id)
            stream_events = _message_payload_STARRING_events(
                msg,
                metadata=metadata,
                namespace=namespace,
                thread_id=chunk_thread_id,
                protocol_message_ids=protocol_message_ids,
            )

            for stream_event in stream_events:
                content = _stream_event_response(stream_event)
                # 仅主智能体的文本内容需要参与内容安全检查与累积
                if not is_subagent_chunk and content:
                    trace_info = get_trace_info(langfuse_run)
                    accumulated_content.append(content)
                    # 仅检查最近 10 个 chunk 的拼接，兼顾性能与时效
                    content_for_check = "".join(accumulated_content[-10:])
                    if conf.enable_content_guard and await content_guard.check_with_keywords(content_for_check):
                        full_msg = AIMessage(content="".join(accumulated_content))
                        await save_partial_message(
                            conv_repo,
                            thread_id,
                            full_msg,
                            "content_guard_blocked",
                            trace_info=trace_info,
                            run_id=meta.get("run_id"),
                            request_id=meta.get("request_id"),
                        )
                        meta["time_cost"] = asyncio.get_event_loop().time() - start_time
                        yield make_chunk(status="interrupted", message="检测到敏感内容，已中断输出", meta=meta)
                        return

                yield make_chunk(
                    content=content,
                    stream_event=stream_event,
                    metadata=metadata,
                    status="loading",
                    thread_id=chunk_thread_id,
                )

        # 流结束后确保 full_msg 存在（协议层未返回完整消息时用累积内容兜底）
        full_msg = _ensure_full_msg(full_msg, accumulated_content)
        trace_info = get_trace_info(langfuse_run)

        # 输出侧整体敏感词检查（防止流式检查漏掉跨 chunk 的敏感词）
        if conf.enable_content_guard and hasattr(full_msg, "content") and await content_guard.check(full_msg.content):
            await save_partial_message(
                conv_repo,
                thread_id,
                full_msg,
                "content_guard_blocked",
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
            meta["time_cost"] = asyncio.get_event_loop().time() - start_time
            yield make_chunk(status="interrupted", message="检测到敏感内容，已中断输出", meta=meta)
            return

        interrupted = False
        # 检查 interrupt 状态：若智能体在执行中提问，会下发 ask_user_question_required chunk
        async for chunk in check_and_handle_interrupts(agent, langgraph_config, make_chunk, meta, thread_id):
            interrupted = True
            yield chunk

        meta["time_cost"] = asyncio.get_event_loop().time() - start_time
        # 读取最终 agent_state，若与流中最后一次推送不同则补发一次
        try:
            graph = await agent.get_graph()
            state = await graph.aget_state(langgraph_config)
            agent_state = extract_agent_state(getattr(state, "values", {})) if state else {}
        except Exception:
            agent_state = {}

        final_signature = _agent_state_signature(agent_state)
        if final_signature and final_signature != last_agent_state_signature:
            last_agent_state_signature = final_signature
            yield make_chunk(status="agent_state", agent_state=agent_state, meta=meta)

        # 先存储数据库，再返回 finished，避免前端查询时数据未落库
        try:
            await save_messages_from_langgraph_state(
                agent_instance=agent,
                thread_id=thread_id,
                conv_repo=conv_repo,
                config_dict=langgraph_config,
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
        except Exception as e:
            logger.exception(f"Error saving messages from LangGraph state: {e}")
            yield make_chunk(status="warning", message=f"消息保存失败: {e}", meta=meta)

        # 若已被 interrupt，则不再发送 finished，等待前端 resume
        if interrupted:
            return

        yield make_chunk(status="finished", meta=meta)

    except (asyncio.CancelledError, ConnectionError) as e:
        # 客户端断连：在新 session 中保存已生成的部分内容，避免丢失上下文
        logger.warning(f"Client disconnected, cancelling stream: {e}")

        async def save_cleanup():
            nonlocal full_msg
            full_msg = _ensure_full_msg(full_msg, accumulated_content)

            async with pg_manager.get_async_session_context() as new_db:
                new_conv_repo = ConversationRepository(new_db)
                await save_partial_message(
                    new_conv_repo,
                    thread_id,
                    full_msg=full_msg,
                    error_message="对话已中断" if not full_msg else None,
                    error_type="interrupted",
                    trace_info=trace_info,
                    run_id=meta.get("run_id"),
                    request_id=meta.get("request_id"),
                )

        # 通过 asyncio.shield 保证清理任务不被外层取消立即终止
        cleanup_task = asyncio.create_task(save_cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Error during cleanup save: {exc}")

        yield make_chunk(status="interrupted", message="对话已中断", meta=meta)

    except Exception as e:
        logger.exception(f"Error streaming messages: {e}")

        error_msg = f"Error streaming messages: {e}"
        error_type = "unexpected_error"

        full_msg = _ensure_full_msg(full_msg, accumulated_content)

        # 异常分支同样在新 session 中保存错误消息，便于后续追溯
        async with pg_manager.get_async_session_context() as new_db:
            new_conv_repo = ConversationRepository(new_db)
            await save_partial_message(
                new_conv_repo,
                thread_id,
                full_msg=full_msg,
                error_message=error_msg,
                error_type=error_type,
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )

        yield make_chunk(status="error", error_type=error_type, error_message=error_msg, meta=meta)
    finally:
        flush_langfuse()


async def stream_agent_resume(
    *,
    thread_id: str,
    resume_input: Any,
    meta: dict,
    current_user,
    db,
) -> AsyncIterator[bytes]:
    """中断恢复入口：根据用户对 interrupt 的回答继续执行智能体。

    通过 LangGraph 的 ``Command(resume=...)`` 把 ``resume_input`` 注入到上次中断的节点，
    后续流式处理逻辑与 ``stream_agent_chat`` 后半段一致（事件转换、状态推送、消息落库）。
    """
    start_time = asyncio.get_event_loop().time()

    def make_resume_chunk(content=None, **kwargs):
        chunk_thread_id = kwargs.pop("thread_id", None) or meta.get("thread_id") or thread_id
        return (
            json.dumps(
                {"request_id": meta.get("request_id"), "response": content, "thread_id": chunk_thread_id, **kwargs},
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )

    yield make_resume_chunk(status="init", meta=meta)

    # 构造 LangGraph resume 命令，将用户回答注入到上次中断的节点
    resume_command = Command(resume=resume_input)

    uid = str(current_user.uid)
    # resume 必须基于已有线程，故 requested_agent_id=None，由线程决定 agent
    try:
        agent_item, agent, agent_config = await _resolve_agent_runtime(
            db=db,
            user=current_user,
            requested_agent_id=None,
            thread_id=thread_id,
        )
    except ValueError as e:
        yield make_resume_chunk(status="error", error_type="invalid_agent", error_message=str(e), meta=meta)
        return

    meta["agent_id"] = agent_item.slug
    meta["backend_id"] = agent_item.backend_id
    input_context = await build_agent_input_context(
        agent_config or {},
        thread_id=thread_id,
        uid=uid,
        run_id=meta.get("run_id"),
        request_id=meta.get("request_id"),
    )
    _apply_model_override(input_context, meta)
    # context 用于后续 get_graph 重新读取 state（保持与首次执行一致的上下文）
    context = agent.context_schema()
    context.update(input_context)
    langfuse_run = _build_langfuse_run_context(
        current_user=current_user,
        thread_id=thread_id,
        agent_id=agent_item.slug,
        backend_id=agent_item.backend_id,
        request_id=meta.get("request_id") or str(uuid.uuid4()),
        operation="agent_chat_resume",
        message_type="resume",
        meta=meta,
    )
    trace_info: dict[str, Any] = {}
    last_agent_state_signature = ""

    stream_source = agent.stream_resume_with_state(
        resume_command,
        input_context=input_context,
        callbacks=langfuse_run.callbacks,
        metadata=langfuse_run.metadata,
        tags=langfuse_run.tags,
    )

    protocol_message_ids: dict[tuple[str, str], str] = {}

    try:
        async for mode, payload in stream_source:
            # values 模式：agent_state 快照更新
            if mode == "values":
                agent_state = extract_agent_state(payload if isinstance(payload, dict) else {})
                signature = _agent_state_signature(agent_state)
                if signature and signature != last_agent_state_signature:
                    last_agent_state_signature = signature
                    yield make_resume_chunk(status="agent_state", agent_state=agent_state, meta=meta)
                continue

            # stream_event 模式：透传底层协议事件
            if mode == "stream_event":
                event_payload = payload if isinstance(payload, dict) else {}
                yield make_resume_chunk(
                    status="stream_event",
                    event=event_payload,
                    namespace=event_payload.get("namespace") or [],
                    meta=meta,
                    thread_id=event_payload.get("thread_id"),
                )
                continue

            if mode != "messages":
                continue

            # messages 模式：常规消息 chunk
            msg, metadata = payload
            metadata = dict(metadata or {})
            namespace = _metadata_namespace(metadata)
            chunk_thread_id = _metadata_thread_id(metadata, thread_id if not namespace else None)
            if namespace and not chunk_thread_id:
                continue

            # 仅主线程消息刷新 trace_info（子智能体消息不覆盖主追踪）
            if chunk_thread_id == thread_id:
                trace_info = get_trace_info(langfuse_run)

            stream_events = _message_payload_STARRING_events(
                msg,
                metadata=metadata,
                namespace=namespace,
                thread_id=chunk_thread_id,
                protocol_message_ids=protocol_message_ids,
            )

            for stream_event in stream_events:
                content = _stream_event_response(stream_event)
                yield make_resume_chunk(
                    content=content,
                    stream_event=stream_event,
                    metadata=metadata,
                    status="loading",
                    thread_id=chunk_thread_id,
                )

        langgraph_config = {"configurable": {"thread_id": thread_id, "uid": uid}}
        # resume 同样可能再次进入 interrupt（多轮提问），需要再次检查
        interrupted = False
        async for chunk in check_and_handle_interrupts(agent, langgraph_config, make_resume_chunk, meta, thread_id):
            interrupted = True
            yield chunk

        meta["time_cost"] = asyncio.get_event_loop().time() - start_time

        try:
            graph = await agent.get_graph(context=context)
            state = await graph.aget_state(langgraph_config)
            agent_state = extract_agent_state(getattr(state, "values", {})) if state else {}
        except Exception:
            agent_state = {}

        final_signature = _agent_state_signature(agent_state)
        if final_signature and final_signature != last_agent_state_signature:
            yield make_resume_chunk(status="agent_state", agent_state=agent_state, meta=meta)

        # 先存储数据库，再返回 finished，避免前端查询时数据未落库
        conv_repo = ConversationRepository(db)
        try:
            await save_messages_from_langgraph_state(
                agent_instance=agent,
                thread_id=thread_id,
                conv_repo=conv_repo,
                config_dict=langgraph_config,
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
        except Exception as e:
            logger.exception(f"Error saving messages from LangGraph state: {e}")
            yield make_resume_chunk(status="warning", message=f"消息保存失败: {e}", meta=meta)

        if interrupted:
            return

        yield make_resume_chunk(status="finished", meta=meta)

    except (asyncio.CancelledError, ConnectionError) as e:
        # 客户端断连：保存中断状态消息，便于后续重新打开时恢复
        logger.warning(f"Client disconnected during resume: {e}")

        async with pg_manager.get_async_session_context() as new_db:
            new_conv_repo = ConversationRepository(new_db)
            await save_partial_message(
                new_conv_repo,
                thread_id,
                error_message="对话恢复已中断",
                error_type="resume_interrupted",
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )

        yield make_resume_chunk(status="interrupted", message="对话恢复已中断", meta=meta)

    except Exception as e:
        logger.exception(f"Error during resume: {e}")

        async with pg_manager.get_async_session_context() as new_db:
            new_conv_repo = ConversationRepository(new_db)
            await save_partial_message(
                new_conv_repo,
                thread_id,
                error_message=f"Error during resume: {e}",
                error_type="resume_error",
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )

        yield make_resume_chunk(message=f"Error during resume: {e}", status="error")
    finally:
        flush_langfuse()


def _serialize_state_messages(values: dict[str, Any]) -> list[dict[str, Any]]:
    """将 checkpoint state 中的 messages 序列化为纯 dict 列表，供接口返回。

    支持 pydantic 模型、dict、其他对象三种情况；其他对象退化为 ``{"type": "unknown", ...}``。
    """
    messages = values.get("messages") if isinstance(values, dict) else None
    if not isinstance(messages, list):
        return []
    serialized = []
    for message in messages:
        if hasattr(message, "model_dump"):
            serialized.append(message.model_dump())
        elif isinstance(message, dict):
            serialized.append(dict(message))
        else:
            serialized.append({"type": "unknown", "content": str(message)})
    return serialized


async def _read_checkpoint_state(agent, *, uid: str, thread_id: str):
    """读取指定线程的 LangGraph checkpoint state，用于状态视图接口。"""
    graph = await agent.get_graph()
    langgraph_config = {"configurable": {"uid": uid, "thread_id": thread_id}}
    return await graph.aget_state(langgraph_config)


def _serialize_subagent_run(run) -> dict[str, Any]:
    """将子智能体 run 记录序列化为前端可展示的字典。

    字段优先从 ``input_payload`` 中提取（创建 run 时写入的子智能体类型、名称、子线程等），
    缺失时回退到 run 自身字段，保证向前端返回结构完整。
    """
    payload = run.input_payload if isinstance(run.input_payload, dict) else {}
    return {
        "id": payload.get("tool_call_id") or run.id,
        "run_id": run.id,
        "subagent_type": payload.get("subagent_type") or run.agent_id,
        "subagent_name": payload.get("subagent_name"),
        "child_thread_id": payload.get("child_thread_id") or run.thread_id,
        "description": payload.get("description"),
        "status": run.status,
        "created_at": run.to_dict().get("created_at"),
        "completed_at": run.to_dict().get("finished_at"),
        "result_preview": payload.get("result_preview"),
        "error": run.error_message,
        "parent_agent_run_id": run.parent_agent_run_id,
    }


async def get_agent_state_view(
    *,
    thread_id: str,
    current_uid: str,
    db,
    include_messages: bool = False,
) -> dict:
    """读取对话状态视图：主智能体优先，否则回退到最新子智能体 run。

    解析顺序：
        1. 若 thread_id 对应一个对话，则读取主智能体 checkpoint，返回 ``agent_state``；
        2. 否则尝试把 thread_id 当作子智能体线程，定位其父 run 与父对话，
           校验属主后读取子智能体 checkpoint，并附带 ``parent_thread_id`` 与 ``subagent_run`` 信息。

    ``include_messages=True`` 时额外返回序列化后的 messages 列表。
    所有属主/存在性校验失败均返回 404。
    """
    from fastapi import HTTPException

    conv_repo = ConversationRepository(db)
    agent_repo = AgentRepository(db)
    conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
    if conversation:
        if conversation.uid != str(current_uid) or conversation.status == "deleted":
            raise HTTPException(status_code=404, detail="对话线程不存在")

        agent_item = await agent_repo.get_by_slug(conversation.agent_id)
        if not agent_item:
            raise HTTPException(status_code=404, detail="智能体不存在")
        agent = agent_manager.get_agent(agent_item.backend_id)
        if not agent:
            raise HTTPException(status_code=404, detail="智能体后端不存在")
        state = await _read_checkpoint_state(agent, uid=str(current_uid), thread_id=thread_id)
        values = getattr(state, "values", {}) if state else {}
        response = {"agent_state": extract_agent_state(values)}
        if include_messages:
            response["messages"] = _serialize_state_messages(values)
        return response

    # 走子智能体分支：thread_id 不是主对话线程，可能是子智能体线程
    run_repo = AgentRunRepository(db)
    subagent_run = await run_repo.get_latest_subagent_run_by_thread_for_user(thread_id, str(current_uid))
    if not subagent_run or not subagent_run.parent_agent_run_id:
        raise HTTPException(status_code=404, detail="对话线程不存在")

    parent_run = await run_repo.get_run_for_user(subagent_run.parent_agent_run_id, str(current_uid))
    if not parent_run:
        raise HTTPException(status_code=404, detail="对话线程不存在")

    # 校验父 run 与父对话一致且属于当前用户
    parent_conversation = await conv_repo.get_conversation_by_thread_id(parent_run.thread_id)
    if (
        not parent_conversation
        or parent_conversation.id != parent_run.conversation_id
        or parent_conversation.uid != str(current_uid)
        or parent_conversation.status == "deleted"
    ):
        raise HTTPException(status_code=404, detail="对话线程不存在")

    child_agent_item = await agent_repo.get_by_slug(subagent_run.agent_id)
    if not child_agent_item:
        raise HTTPException(status_code=404, detail="智能体不存在")
    child_agent = agent_manager.get_agent(child_agent_item.backend_id)
    if not child_agent:
        raise HTTPException(status_code=404, detail="智能体后端不存在")

    # 子智能体可能使用独立的 checkpoint_thread_id，优先使用之，否则用 thread_id
    checkpoint_thread_id = subagent_run.checkpoint_thread_id or subagent_run.thread_id
    child_state = await _read_checkpoint_state(child_agent, uid=str(current_uid), thread_id=checkpoint_thread_id)
    child_values = getattr(child_state, "values", {}) if child_state else {}
    response = {
        "agent_state": extract_agent_state(child_values),
        "parent_thread_id": parent_run.thread_id,
        "subagent_run": _serialize_subagent_run(subagent_run),
    }
    if include_messages:
        response["messages"] = _serialize_state_messages(child_values)
    return response
