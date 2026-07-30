"""CodeAct 中间件 - execute_python 工具 + 使用说明注入 system prompt。

设计文档：docs/vibe/2026-07-27-codeact-execution-paradigm.md

- ``execute_python`` 工具：模型提交一段 Python 代码，写入沙盒 workspace/.codeact/
  后执行；代码内可通过 ``starring_tools.call()`` 经工具桥回调平台工具
- 每次执行前生成 run 级 bridge token（白名单快照入 Redis），执行结束显式吊销
- 连续失败 ≥3 次进入熔断态，不再执行并提示模型改用常规工具
- ``awrap_model_call``：向 system prompt 追加 CodeAct 使用说明（含可桥工具清单）

工具用 ``StructuredTool.from_function`` 在中间件内构建（仿 MemoryMiddleware），
不进全局工具注册表：无中间件挂载时该工具无 token 无法工作，也天然不进桥白名单。
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Awaitable, Callable

try:
    from deepagents.middleware._utils import append_to_system_message
except ImportError:
    append_to_system_message = None  # type: ignore[assignment]
from langchain.agents.middleware.types import AgentMiddleware, ContextT, ModelRequest, ModelResponse, ResponseT
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolRuntime

from starring import config as conf
from starring.services.codeact_service import compute_bridge_whitelist, create_bridge_token, revoke_bridge_token
from starring.utils import logger
from starring.utils.paths import VIRTUAL_PATH_PREFIX, WORKSPACE_DIR_NAME

# 连续失败熔断阈值：达到后本 run 内不再执行代码
_MAX_CONSECUTIVE_FAILURES = 3

_WORKSPACE_ROOT = f"/{VIRTUAL_PATH_PREFIX.strip('/')}/{WORKSPACE_DIR_NAME}"
_CODEACT_DIR = f"{_WORKSPACE_ROOT}/.codeact"

EXECUTE_PYTHON_DESCRIPTION = (
    "在沙盒中执行一段 Python 3 代码并返回其 stdout/stderr。"
    "适合多步组合任务：批量检索、循环处理、数据加工——优先写一段代码完成，"
    "而不是逐步调用多个工具。"
    '代码内可 `import starring_tools` 后用 `starring_tools.call("工具名", 参数名=值)` '
    "调用平台工具（可用清单见 system prompt 的 CodeAct 使用说明）。"
    "只有 print 到 stdout/stderr 的内容会返回，务必 print 出你需要看到的结果。"
)


class CodeActMiddleware(AgentMiddleware):
    """CodeAct 中间件：注册 execute_python 工具并注入使用说明。

    每 run 一个实例（与 MemoryMiddleware 的实例缓存模式一致），
    实例上维护执行序号与连续失败计数。
    """

    def __init__(self, context):
        super().__init__()
        self._context = context
        self._uid = str(getattr(context, "uid", "") or "")
        # 文件沙盒跟随 file_thread_id（会话文件空间可能与运行 thread 分离）
        self._thread_id = str(getattr(context, "file_thread_id", None) or getattr(context, "thread_id", "") or "")
        self._whitelist = compute_bridge_whitelist(context)
        self._exec_index = 0
        self._failure_count = 0
        self.tools = [self._build_execute_python_tool()]

    def _build_execute_python_tool(self) -> StructuredTool:
        def execute_python(code: str, runtime: ToolRuntime) -> str:
            return "execute_python 工具仅支持异步调用"

        async def aexecute_python(code: str, runtime: ToolRuntime) -> str:
            del runtime
            return await self._execute(code)

        return StructuredTool.from_function(
            name="execute_python",
            func=execute_python,
            coroutine=aexecute_python,
            description=EXECUTE_PYTHON_DESCRIPTION,
            infer_schema=True,
        )

    async def _execute(self, code: str) -> str:
        if self._failure_count >= _MAX_CONSECUTIVE_FAILURES:
            return (
                f"CodeAct 已连续失败 {self._failure_count} 次，本次运行内不再执行代码。"
                "请改用常规工具直接完成任务，或向用户说明失败原因。"
            )
        if not str(code or "").strip():
            return "代码为空，未执行。请提供要运行的 Python 代码。"

        self._exec_index += 1
        index = self._exec_index
        token, _ = await create_bridge_token(self._context)
        try:
            response = await asyncio.to_thread(self._run_in_sandbox, code, index, token)
        finally:
            await revoke_bridge_token(token)

        output = (response.output or "").strip()
        if response.exit_code == 0:
            self._failure_count = 0
            result = output or "(执行成功，无输出)"
            if response.truncated:
                result += "\n\n[输出过长已截断，只保留了开头部分]"
            return result

        self._failure_count += 1
        result = f"执行失败（exit_code={response.exit_code}）：\n{output or '(无输出)'}"
        if response.truncated:
            result += "\n\n[输出过长已截断，保留的是包含 traceback 的尾部]"
        if self._failure_count >= _MAX_CONSECUTIVE_FAILURES:
            result += (
                f"\n\n已连续失败 {self._failure_count} 次，execute_python 在本次运行内不再可用。"
                "请改用常规工具完成任务。"
            )
        else:
            result += "\n\n请根据 traceback 修正代码后重试，或改用常规工具。"
        return result

    def _run_in_sandbox(self, code: str, index: int, token: str):
        """同步执行体（在 to_thread 中运行）：注入文件 + 执行脚本。

        输出策略：脚本 stdout/stderr 重定向到日志文件，成功保留头部
        （head，结果通常在前面），失败保留尾部（tail，traceback 在末尾）。
        """
        from starring.agents.backends.sandbox.backend import ProvisionerSandboxBackend
        from starring.agents.codeact import load_client_template

        backend = ProvisionerSandboxBackend(self._thread_id, uid=self._uid)
        script_name = f"act_{index}.py"
        uploads = backend.upload_files(
            [
                (f"{_CODEACT_DIR}/starring_tools.py", load_client_template().encode("utf-8")),
                (f"{_CODEACT_DIR}/{script_name}", code.encode("utf-8")),
            ]
        )
        failed = [item for item in uploads if item.error]
        if failed:
            logger.warning(f"CodeAct 文件注入失败: {failed}")
            from deepagents.backends.protocol import ExecuteResponse

            return ExecuteResponse(
                output=f"Error: 无法写入沙盒执行文件（{failed[0].error}）",
                exit_code=1,
                truncated=False,
            )

        max_bytes = int(conf.sandbox_max_output_bytes)
        timeout = int(conf.sandbox_exec_timeout_seconds)
        log_name = f"act_{index}.log"
        command = (
            f"cd {shlex.quote(_CODEACT_DIR)} && "
            f"STARRING_BRIDGE_URL={shlex.quote(conf.codeact_bridge_url)} "
            f"STARRING_BRIDGE_TOKEN={shlex.quote(token)} "
            f"python {script_name} > {log_name} 2>&1; ec=$?; "
            f"if [ $ec -eq 0 ]; then head -c {max_bytes} {log_name}; "
            f"else tail -c {max_bytes} {log_name}; fi; "
            f"exit $ec"
        )
        return backend.execute(command, timeout=timeout)

    def _build_prompt(self) -> str:
        if self._whitelist:
            tool_lines = "、".join(f"`{name}`" for name in self._whitelist)
            bridge_section = (
                "代码内可通过工具桥调用以下平台工具（其余工具不可桥接，调用会被拒绝）：\n"
                f"{tool_lines}\n\n"
                "调用方式：\n"
                "```python\n"
                "import starring_tools\n"
                'result = starring_tools.call("工具名", 参数名=值)\n'
                "```\n"
                "调用失败会抛出 `starring_tools.ToolCallError`，traceback 会原样返回给你。"
            )
        else:
            bridge_section = "本次运行没有可桥接的平台工具，代码内请勿调用 `starring_tools`。"
        return (
            "## CodeAct 使用说明\n\n"
            "你拥有 `execute_python` 工具：提交一段 Python 3 代码在沙盒中执行。"
            "当任务需要多步工具调用、批量检索或数据加工时，优先写一段代码一次性完成，"
            "而不是逐步调用多个工具。\n\n"
            f"- 代码在沙盒 `{_CODEACT_DIR}/` 目录下执行，会话文件位于 `{_WORKSPACE_ROOT}/`，可直接读写。\n"
            "- 只有 print 到 stdout/stderr 的内容会返回，务必 print 出需要查看的结果。\n"
            "- 单次执行有超时限制，避免死循环与长时间阻塞操作。\n\n"
            f"{bridge_section}"
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        prompt = self._build_prompt()
        if append_to_system_message is not None:
            system_message = append_to_system_message(request.system_message, prompt)
        else:
            system_message = f"{request.system_message}\n\n{prompt}" if request.system_message else prompt
        return await handler(request.override(system_message=system_message))
