"""
StarRing 智能体基础框架。

本模块定义了所有智能体的基类 BaseAgent，以及流式事件处理所需的
工具函数。BaseAgent 封装了 LangGraph 图的生命周期管理、checkpointer
后端切换（SQLite / PostgreSQL / 内存）、流式输出与子智能体路由等
通用能力，子类只需实现 get_graph() 即可获得完整的运行能力。
"""

from __future__ import annotations

import asyncio
import os
from abc import abstractmethod
from contextlib import suppress
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver, aiosqlite
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from starring import config as sys_config
from starring.agents.context import DEFAULT_MAX_EXECUTION_STEPS, BaseContext, resolve_agent_resource_options
from starring.storage.postgres.manager import pg_manager
from starring.utils import logger
from starring.utils.subagent_thread_utils import make_child_thread_id


def _json_safe(value: Any) -> Any:
    """将任意值转换为 JSON 可序列化的安全类型。

    递归处理 dict、list、tuple 等复合类型，对 Pydantic 模型调用
    model_dump() 展开，其余不可序列化对象退化为 str()。

    参数:
        value: 需要转换的任意 Python 对象。

    返回:
        JSON 安全的原始类型、dict、list 或 str。
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


def _normalize_tool_event_data(data: Any) -> Any:
    """规整 tools 流事件：write_todos / task 等返回 Command 的工具，其 tool-finished
    output 是 Command 对象，_json_safe 只能退化成 repr 字符串，前端无法关联结果。
    这里从 Command.update["messages"] 取出真正的 ToolMessage，使其与普通工具一致。"""
    if not isinstance(data, dict) or data.get("event") != "tool-finished":
        return data
    output = data.get("output")
    if not isinstance(output, Command):
        return data
    update = output.update if isinstance(output.update, dict) else {}
    messages = update.get("messages")
    if not isinstance(messages, list):
        return data
    tool_call_id = data.get("tool_call_id")
    tool_message = next(
        (m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == tool_call_id),
        next((m for m in messages if isinstance(m, ToolMessage)), None),
    )
    if tool_message is None:
        return data
    return {**data, "output": tool_message}


def _metadata_thread_id(value: Any) -> str | None:
    """从嵌套的 metadata / configurable 结构中递归提取 thread_id。

    优先查找顶层键 ``thread_id`` 或 ``subagent_thread_id``，若未找到
    则继续向 ``metadata``、``configurable``、``config`` 子字典中递归搜索。

    参数:
        value: 可能是 dict 或任意类型的元数据对象。

    返回:
        提取到的 thread_id 字符串，未找到则返回 None。
    """
    if not isinstance(value, dict):
        return None
    for key in ("thread_id", "subagent_thread_id"):
        thread_id = value.get(key)
        if isinstance(thread_id, str) and thread_id.strip():
            return thread_id.strip()
    for key in ("metadata", "configurable", "config"):
        thread_id = _metadata_thread_id(value.get(key))
        if thread_id:
            return thread_id
    return None


def _subagent_route_for_namespace(
    routes: dict[tuple[str, ...], dict[str, str]], namespace: list[str]
) -> dict[str, str] | None:
    """根据 namespace 查找最佳匹配的子智能体路由信息。

    采用最长前缀匹配策略：在 routes 中按 path 长度降序排列，
    找到第一个与当前 namespace 前缀匹配的路由条目。

    参数:
        routes: 已收集的子智能体路由表，键为 path 元组，值为路由信息。
        namespace: 当前事件的 namespace 列表。

    返回:
        匹配到的路由信息字典，无匹配则返回 None。
    """
    ns = tuple(namespace)
    for path, route in sorted(routes.items(), key=lambda item: len(item[0]), reverse=True):
        if ns[: len(path)] == path:
            return route
    return None


async def _collect_subagent_routes(run, parent_thread_id: str, routes: dict[tuple[str, ...], dict[str, str]]) -> None:
    """从 stream 运行对象中异步收集子智能体路由信息。

    遍历 graph.astream_events 返回的 run 对象的 subagents 属性，
    提取每个子智能体的 path、类型、tool_call_id 和 thread_id，
    写入 routes 字典供后续流式事件路由使用。

    参数:
        run: graph.astream_events 返回的异步迭代器对象。
        parent_thread_id: 父智能体的线程 ID。
        routes: 收集结果的目标字典，会被原地修改。
    """
    subagents = getattr(run, "subagents", None)
    if subagents is None:
        return

    try:
        async for subagent in subagents:
            path = tuple(getattr(subagent, "path", ()) or ())
            subagent_type = getattr(subagent, "name", None) or getattr(subagent, "graph_name", None)
            cause = getattr(subagent, "cause", None)
            tool_call_id = (
                cause.get("tool_call_id") if isinstance(cause, dict) else getattr(subagent, "trigger_call_id", None)
            )
            state = getattr(subagent, "state", None)
            metadata = getattr(subagent, "metadata", None)
            thread_id = _metadata_thread_id(metadata) or _metadata_thread_id(state)
            if not thread_id and isinstance(subagent_type, str) and isinstance(tool_call_id, str) and tool_call_id:
                thread_id = make_child_thread_id(parent_thread_id, subagent_type, tool_call_id)
            if path and isinstance(subagent_type, str) and isinstance(tool_call_id, str) and tool_call_id and thread_id:
                routes[path] = {
                    "thread_id": thread_id,
                    "parent_thread_id": parent_thread_id,
                    "subagent_type": subagent_type,
                    "tool_call_id": tool_call_id,
                }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug(f"collect subagent stream routes failed: {exc}")


def _recursion_limit_from_context(context: BaseContext, default: int) -> int:
    """从上下文中提取并校验 max_execution_steps 作为 recursion_limit。

    若上下文中未配置或值为非正整数，则回退到默认值。

    参数:
        context: 智能体运行上下文实例。
        default: 默认的最大执行步数。

    返回:
        有效的 recursion_limit 整数值。
    """
    value = getattr(context, "max_execution_steps", default)
    return int(value) if isinstance(value, int) and value > 0 else default


class BaseAgent:
    """智能体基类。

    所有智能体（ChatbotAgent、SubAgent 等）均继承此类，它封装了
    LangGraph 图的编译、checkpointer 管理、流式输出、子智能体路由、
    历史记录查询等通用能力。

    子类需要：
        - 实现 get_graph() 抽象方法，返回 CompiledStateGraph
        - 可选覆盖 name、description、capabilities、context_schema 等类属性
    """

    name = "base_agent"
    description = "base_agent"
    capabilities: list[str] = []  # 智能体能力列表，如 ["file_upload", "web_search"] 等
    context_schema: type[BaseContext] = BaseContext  # 智能体上下文 schema，定义可配置参数

    def __init__(self, **kwargs):
        self.graph = None  # 编译后的 LangGraph 图实例，由 get_graph() 延迟创建
        self.checkpointer = None  # 图状态持久化器，根据环境变量选择后端
        self._async_conn = None  # SQLite 异步连接，仅在 SQLite 后端时使用
        self.workdir = Path(sys_config.save_dir) / "agents" / self.module_name
        self.workdir.mkdir(parents=True, exist_ok=True)

    @property
    def module_name(self) -> str:
        """获取智能体类所在模块的名称（取包路径倒数第二段）。"""
        return self.__class__.__module__.split(".")[-2]

    @property
    def id(self) -> str:
        """获取智能体的类名作为唯一标识。"""
        return self.__class__.__name__

    async def get_info(
        self,
        include_configurable_items: bool = True,
        user_role: str | None = None,
        db=None,
        user=None,
    ):
        """获取智能体的完整元信息，供前端 UI 展示和配置使用。

        返回内容包括智能体 id、名称、描述、元数据、可配置项列表和
        能力列表。当提供 db 和 user 参数时，会额外加载 tools、
        knowledges、mcps、skills、subagents 等资源的可用选项。

        参数:
            include_configurable_items: 是否包含可配置项列表。
            user_role: 用户角色，用于过滤配置项（如 admin 字段）。
            db: 数据库会话，用于加载资源选项。
            user: 当前用户对象，用于加载用户可访问的资源。

        返回:
            包含智能体元信息的字典。
        """
        # metadata 固定在代码中，由各 Agent 的类属性提供
        metadata = self.load_metadata()
        configurable_items = {}
        if include_configurable_items:
            configurable_items = self.context_schema.get_configurable_items(user_role=user_role)
            if db is not None and user is not None:
                resource_fields = {
                    item["kind"]
                    for item in configurable_items.values()
                    if item.get("kind") in {"tools", "knowledges", "mcps", "skills", "subagents"}
                }
                resource_options = await resolve_agent_resource_options(resource_fields, db=db, user=user)
                for item in configurable_items.values():
                    if item.get("kind") in resource_options:
                        item["options"] = resource_options[item["kind"]]

        # Merge metadata with class attributes, metadata takes precedence
        return {
            "id": self.id,
            "name": getattr(self, "name", "Unknown"),
            "description": getattr(self, "description", "Unknown"),
            "metadata": metadata,
            "configurable_items": configurable_items,
            "capabilities": getattr(self, "capabilities", []),  # 智能体能力列表
        }

    async def get_config(self):
        """获取一个使用默认值的上下文实例。"""
        return self.context_schema()

    async def stream_values(self, messages: list[str], input_context=None, **kwargs):
        """以 ``values`` 模式流式输出图执行过程中每个节点的完整状态。

        每次 yield 当前节点完成后的完整 messages 列表。

        参数:
            messages: 用户输入消息列表。
            input_context: 可选的上下文配置字典。
        """
        context = self.context_schema()
        context.update_from_dict(input_context or {})
        graph = await self.get_graph(context=context)
        for event in graph.astream({"messages": messages}, stream_mode="values", context=context):
            yield event["messages"]

    async def stream_messages(self, messages: list[str], input_context=None, **kwargs):
        """以 ``messages`` 模式流式输出图中的增量消息事件。

        每个事件为 (message, metadata) 元组，metadata 包含
        LangGraph 节点信息。支持 langfuse 的 callbacks / metadata / tags
        透传。

        参数:
            messages: 用户输入消息列表。
            input_context: 可选的上下文配置字典。
            callbacks: 可选的 langfuse 回调处理器列表。
            metadata: 可选的 langfuse 元数据字典。
            tags: 可选的 langfuse 标签列表。
        """
        context = self.context_schema()
        context.update_from_dict(input_context or {})
        graph = await self.get_graph(context=context)
        logger.debug(f"stream_messages: {context=}")

        # 构建配置：LangGraph 会自动从 checkpointer 恢复 state
        input_config = {
            "configurable": {"thread_id": context.thread_id, "uid": context.uid},
            "recursion_limit": _recursion_limit_from_context(context, DEFAULT_MAX_EXECUTION_STEPS),
        }

        # langfuse metadata and callbacks integration
        if callbacks := kwargs.get("callbacks"):
            input_config["callbacks"] = list(callbacks)
        if metadata := kwargs.get("metadata"):
            input_config["metadata"] = dict(metadata)
        if tags := kwargs.get("tags"):
            input_config["tags"] = list(tags)

        async for msg, metadata in graph.astream(
            {"messages": messages},
            stream_mode="messages",
            context=context,
            config=input_config,
        ):
            yield msg, metadata

    async def _stream_input_with_state(self, graph_input, input_context=None, **kwargs):
        """核心流式方法：使用 ``astream_events`` 以 v3 版本流式输出各类事件。

        与 stream_messages 不同，此方法输出更丰富的事件类型：
        - ``messages``: 增量消息事件，附带 thread_id 和子智能体路由信息
        - ``values``: 顶层状态的完整快照（仅根图）
        - ``stream_event``: tasks / tools / lifecycle 等自定义事件

        同时异步收集子智能体路由表，为每个事件注入正确的 thread_id
        和子智能体关联信息，使前端能正确关联子智能体输出。

        参数:
            graph_input: 图输入，通常为 {"messages": [...]} 或 resume 的 Command。
            input_context: 可选的上下文配置字典。
            callbacks: 可选的 langfuse 回调处理器列表。
            metadata: 可选的 langfuse 元数据字典。
            tags: 可选的 langfuse 标签列表。
        """
        # ── 1. 初始化上下文与图 ──
        context = self.context_schema()
        context.update_from_dict(input_context or {})
        graph = await self.get_graph(context=context)
        logger.debug(f"stream_with_state: {context=}")

        # ── 2. 构建 langgraph 运行配置 ──
        input_config = {
            "configurable": {"thread_id": context.thread_id, "uid": context.uid},
            "recursion_limit": _recursion_limit_from_context(context, DEFAULT_MAX_EXECUTION_STEPS),
        }

        # 可选注入 langfuse 观测回调
        if callbacks := kwargs.get("callbacks"):
            input_config["callbacks"] = list(callbacks)
        if metadata := kwargs.get("metadata"):
            input_config["metadata"] = dict(metadata)
        if tags := kwargs.get("tags"):
            input_config["tags"] = list(tags)

        # ── 3. 启动 v3 事件流，并行开启子智能体路由收集 ──
        run = await graph.astream_events(
            # TODO 警告：Unexpected type
            graph_input,
            context=context,
            config=input_config,
            version="v3",
        )
        # 子智能体路由表：namespace → {subagent_uid, subagent_name, ...}
        subagent_routes: dict[tuple[str, ...], dict[str, str]] = {}
        route_task = asyncio.create_task(_collect_subagent_routes(run, context.thread_id, subagent_routes))

        # ── 4. 遍历事件流，按事件类型分别处理并 yield ──
        try:
            async for event in run:
                # 解析事件通用字段
                params = event.get("params") or {}
                namespace = list(params.get("namespace") or [])
                method = event.get("method")
                data = params.get("data")
                subagent_route = _subagent_route_for_namespace(subagent_routes, namespace)

                if method == "messages":
                    # ① 增量消息事件：注入 namespace / thread_id / 子智能体路由信息
                    msg, metadata = data
                    metadata = dict(metadata or {})
                    actual_thread_id = (
                        _metadata_thread_id(metadata) or _metadata_thread_id(params) or _metadata_thread_id(data)
                    )
                    metadata["namespace"] = namespace
                    metadata["stream_event"] = {"method": method, "namespace": namespace}
                    if subagent_route:
                        metadata.update(subagent_route)
                    if actual_thread_id:
                        metadata["thread_id"] = actual_thread_id
                    yield "messages", (msg, metadata)
                elif method == "values" and not namespace:
                    # ② 顶层状态快照：仅根图（namespace 为空），直接透传
                    yield "values", data
                elif method in {"tasks", "tools", "lifecycle", "custom"}:
                    # ③ 自定义事件：tasks / tools（需规范化）/ lifecycle / custom（图内 get_stream_writer 写入）
                    if method == "tools":
                        data = _normalize_tool_event_data(data)
                    event_payload = {
                        "method": method,
                        "namespace": namespace,
                        "data": _json_safe(data),
                    }
                    actual_thread_id = _metadata_thread_id(params) or _metadata_thread_id(data)
                    if subagent_route:
                        event_payload.update(subagent_route)
                    if actual_thread_id:
                        event_payload["thread_id"] = actual_thread_id
                    yield "stream_event", event_payload
        finally:
            # ── 5. 无论正常结束还是异常，确保取消路由收集协程 ──
            route_task.cancel()
            with suppress(asyncio.CancelledError):
                await route_task

    async def stream_messages_with_state(self, messages: list[str], input_context=None, **kwargs):
        """以带状态信息的富事件模式流式输出消息。
            将普通Message列表适配成_stream_input_with_state期望的字典格式
        是对 _stream_input_with_state 的封装，输入为普通消息列表。
        输出包含 messages、values、stream_event 等多种事件类型。
        """
        async for event in self._stream_input_with_state({"messages": messages}, input_context, **kwargs):
            yield event

    async def stream_resume_with_state(self, resume_input, input_context=None, **kwargs):
        """以带状态信息的富事件模式流式恢复执行。

        用于中断后恢复的场景（如用户确认操作后继续），
        resume_input 通常为 LangGraph 的 Command 对象。
        """
        async for event in self._stream_input_with_state(resume_input, input_context, **kwargs):
            yield event

    async def invoke_messages(self, messages: list[str], input_context=None, **kwargs):
        """非流式调用图，一次性返回完整执行结果。

        适用于不需要流式输出的场景，返回图中所有节点的最终状态。
        支持 langfuse 的 callbacks / metadata / tags 透传。

        参数:
            messages: 用户输入消息列表。
            input_context: 可选的上下文配置字典。

        返回:
            图执行完成后的最终状态字典。
        """
        context = self.context_schema()
        context.update_from_dict(input_context or {})
        graph = await self.get_graph(context=context)
        logger.debug(f"invoke_messages: {context}")

        # 构建配置
        input_config = {
            "configurable": {"thread_id": context.thread_id, "uid": context.uid},
            "recursion_limit": _recursion_limit_from_context(context, DEFAULT_MAX_EXECUTION_STEPS),
        }

        # langfuse metadata and callbacks integration
        if callbacks := kwargs.get("callbacks"):
            input_config["callbacks"] = list(callbacks)
        if metadata := kwargs.get("metadata"):
            input_config["metadata"] = dict(metadata)
        if tags := kwargs.get("tags"):
            input_config["tags"] = list(tags)

        msg = await graph.ainvoke(
            {"messages": messages},
            context=context,
            config=input_config,
        )
        return msg

    async def check_checkpointer(self):
        """检查已编译的图是否配置了 checkpointer。

        返回:
            True 表示 checkpointer 已就绪，False 表示未配置。
        """
        app = await self.get_graph()
        if not hasattr(app, "checkpointer") or app.checkpointer is None:
            return False
        return True

    async def get_history(self, uid, thread_id) -> list[dict]:
        """获取指定用户和线程的历史消息。

        通过 checkpointer 恢复图状态，提取 messages 列表并序列化为字典。

        参数:
            uid: 用户唯一标识。
            thread_id: 对话线程 ID。

        返回:
            消息字典列表，若无历史或出错则返回空列表。
        """
        try:
            app = await self.get_graph()

            if not await self.check_checkpointer():
                return []

            config = {"configurable": {"thread_id": thread_id, "uid": uid}}
            state = await app.aget_state(config)

            result = []
            if state:
                messages = state.values.get("messages", [])
                for msg in messages:
                    if hasattr(msg, "model_dump"):
                        msg_dict = msg.model_dump()  # 转换成字典
                    else:
                        msg_dict = dict(msg) if hasattr(msg, "__dict__") else {"content": str(msg)}
                    result.append(msg_dict)

            return result

        except Exception as e:
            logger.error(f"获取智能体 {self.name} 历史消息出错: {e}")
            return []

    def reload_graph(self):
        """重置 graph 缓存，强制下次调用 get_graph 时重新构建。

        通常在配置变更（如工具列表、模型切换）后调用，使新配置生效。
        """
        self.graph = None
        logger.info(f"{self.name} graph 缓存已清空，将在下次调用时重新构建")

    @abstractmethod
    async def get_graph(self, **kwargs) -> CompiledStateGraph:
        """
        获取并编译对话图实例。
        必须确保在编译时设置 checkpointer，否则将无法获取历史记录。
        例如: graph = workflow.compile(checkpointer=sqlite_checkpointer)
        """
        pass

    async def _get_checkpointer(self):
        """获取或创建图状态持久化器（checkpointer）。

        根据环境变量 ``LANGGRAPH_CHECKPOINTER_BACKEND`` 选择后端：
        - ``postgres``: 使用 PostgreSQL 存储（通过 pg_manager）
        - 其他 / 未设置: 使用 SQLite 文件存储
        - SQLite 不可用时降级为内存存储（InMemorySaver）

        结果会被缓存到 self.checkpointer，后续调用直接返回。
        """
        if self.checkpointer is not None:
            return self.checkpointer

        checkpointer = None
        backend = os.getenv("LANGGRAPH_CHECKPOINTER_BACKEND", "sqlite").strip().lower()

        if backend == "postgres":
            checkpointer = await self._create_postgres_checkpointer()

        if checkpointer is None:
            try:
                checkpointer = AsyncSqliteSaver(await self.get_async_conn())
            except Exception as e:
                # SQLite 不可用时降级到 InMemorySaver，生产环境可能导致状态丢失（每次重启丢失所有对话状态）
                # 默认允许回退（保持现有行为），可通过环境变量 ALLOW_INMEMORY_CHECKPOINTER_FALLBACK=false 强制 fail-fast
                allow_fallback = os.getenv("ALLOW_INMEMORY_CHECKPOINTER_FALLBACK", "true").strip().lower() != "false"
                if not allow_fallback:
                    raise RuntimeError(
                        f"构建 sqlite checkpointer 失败且 ALLOW_INMEMORY_CHECKPOINTER_FALLBACK=false，"
                        f"拒绝降级到 InMemorySaver: {e}"
                    ) from e
                logger.critical(
                    f"构建 sqlite checkpointer 失败，降级到 InMemorySaver（生产环境可能导致状态丢失）: {e}",
                    exc_info=True,
                )
                checkpointer = InMemorySaver()

        self.checkpointer = checkpointer
        return self.checkpointer

    async def _create_postgres_checkpointer(self):
        """创建基于 PostgreSQL 的 checkpointer。

        需要环境变量 ``POSTGRES_URL`` 已配置，且 ``langgraph.checkpoint.postgres.aio``
        模块可用。失败时返回 None，调用方会自动回退到 SQLite。

        返回:
            AsyncPostgresSaver 实例，或 None（回退到 SQLite）。
        """
        postgres_url = os.getenv("POSTGRES_URL")
        if not postgres_url:
            logger.warning("POSTGRES_URL 未配置，无法启用 postgres checkpointer，回退 sqlite")
            return None

        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # type: ignore
        except Exception as e:
            logger.warning(f"langgraph postgres checkpointer 不可用，回退 sqlite: {e}")
            return None

        try:
            saver = AsyncPostgresSaver(pg_manager.langgraph_pool)

            logger.info(f"{self.name} 使用 postgres checkpointer")
            return saver
        except Exception as e:
            logger.warning(f"初始化 postgres checkpointer 失败，回退 sqlite: {e}")
            return None

    async def get_async_conn(self) -> aiosqlite.Connection:
        """获取或创建 SQLite 异步数据库连接。

        连接文件位于智能体工作目录下的 ``aio_history.db``。
        为兼容 LangGraph 的 AsyncSqliteSaver，会为连接补丁
        ``is_alive()`` 方法（若 aiosqlite 版本未提供）。

        返回:
            aiosqlite 异步连接实例。
        """
        if self._async_conn is not None:
            return self._async_conn

        conn = await aiosqlite.connect(os.path.join(self.workdir, "aio_history.db"))
        # Patch: langgraph's AsyncSqliteSaver expects is_alive() method which aiosqlite may not have
        if not hasattr(conn, "is_alive"):
            conn.is_alive = lambda: True
        self._async_conn = conn
        return self._async_conn

    async def get_aio_memory(self) -> AsyncSqliteSaver:
        """获取基于 SQLite 文件存储的异步 checkpointer 实例。

        每次调用创建新的 AsyncSqliteSaver，复用底层连接。
        """
        return AsyncSqliteSaver(await self.get_async_conn())

    def load_metadata(self) -> dict:
        """从智能体类属性中加载元数据。

        子类可通过定义 ``metadata`` 类属性来提供自定义元数据。
        若 metadata 不是 dict 类型，则记录告警并返回空字典。

        返回:
            元数据字典。
        """
        metadata = getattr(self, "metadata", {})
        if isinstance(metadata, dict):
            return metadata
        logger.warning(f"Agent {self.module_name} metadata is not a dict, fallback to empty metadata")
        return {}
