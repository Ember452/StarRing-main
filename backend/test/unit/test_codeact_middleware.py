"""CodeActMiddleware 单元测试：熔断计数、输出组装、prompt 注入与开关默认值。"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault(
    "SAVE_DIR", os.path.join(os.environ.get("CLAUDE_JOB_DIR", tempfile.gettempdir()), "starring-test-saves")
)

from deepagents.backends.protocol import ExecuteResponse
from starring.agents.middlewares import code_act
from starring.agents.middlewares.code_act import _MAX_CONSECUTIVE_FAILURES, CodeActMiddleware

pytestmark = pytest.mark.unit


@pytest.fixture
def middleware(monkeypatch):
    """构建隔离的中间件实例：白名单/token/沙盒执行全部打桩。"""
    monkeypatch.setattr(code_act, "compute_bridge_whitelist", lambda context: ["query_kb", "web_search"])

    tokens = {"created": [], "revoked": []}

    async def create_bridge_token(context):
        token = f"tok-{len(tokens['created'])}"
        tokens["created"].append(token)
        return token, ["query_kb", "web_search"]

    async def revoke_bridge_token(token):
        tokens["revoked"].append(token)

    monkeypatch.setattr(code_act, "create_bridge_token", create_bridge_token)
    monkeypatch.setattr(code_act, "revoke_bridge_token", revoke_bridge_token)

    context = SimpleNamespace(uid="u1", thread_id="t1", file_thread_id=None)
    instance = CodeActMiddleware(context)
    instance._test_tokens = tokens  # 测试观察点
    return instance


def _stub_sandbox(middleware, responses: list[ExecuteResponse]) -> list[dict]:
    """替换 _run_in_sandbox：按序返回预设响应并记录调用。"""
    calls: list[dict] = []

    def run_in_sandbox(code, index, token):
        calls.append({"code": code, "index": index, "token": token})
        return responses[len(calls) - 1]

    middleware._run_in_sandbox = run_in_sandbox
    return calls


# ---------------------------------------------------------------------------
# 熔断计数
# ---------------------------------------------------------------------------


async def test_consecutive_failures_trip_circuit(middleware):
    failure = ExecuteResponse(output="Traceback ...", exit_code=1, truncated=False)
    calls = _stub_sandbox(middleware, [failure] * _MAX_CONSECUTIVE_FAILURES)

    for i in range(_MAX_CONSECUTIVE_FAILURES - 1):
        result = await middleware._execute("print(1)")
        assert "执行失败" in result
        assert "不再可用" not in result

    result = await middleware._execute("print(1)")
    assert "不再可用" in result

    # 熔断后不再执行，也不再生成 token
    tokens_before = len(middleware._test_tokens["created"])
    result = await middleware._execute("print(1)")
    assert "不再执行代码" in result
    assert len(calls) == _MAX_CONSECUTIVE_FAILURES
    assert len(middleware._test_tokens["created"]) == tokens_before


async def test_success_resets_failure_count(middleware):
    responses = [
        ExecuteResponse(output="boom", exit_code=1, truncated=False),
        ExecuteResponse(output="boom", exit_code=1, truncated=False),
        ExecuteResponse(output="ok", exit_code=0, truncated=False),
        ExecuteResponse(output="boom", exit_code=1, truncated=False),
    ]
    _stub_sandbox(middleware, responses)

    await middleware._execute("x")
    await middleware._execute("x")
    assert middleware._failure_count == 2
    result = await middleware._execute("x")
    assert result == "ok"
    assert middleware._failure_count == 0
    await middleware._execute("x")
    assert middleware._failure_count == 1


async def test_token_revoked_even_when_sandbox_raises(middleware):
    def run_in_sandbox(code, index, token):
        raise RuntimeError("sandbox down")

    middleware._run_in_sandbox = run_in_sandbox
    with pytest.raises(RuntimeError):
        await middleware._execute("print(1)")
    assert middleware._test_tokens["revoked"] == middleware._test_tokens["created"]


async def test_empty_code_not_executed(middleware):
    calls = _stub_sandbox(middleware, [])
    result = await middleware._execute("   ")
    assert "代码为空" in result
    assert calls == []


# ---------------------------------------------------------------------------
# 输出组装（截断提示）
# ---------------------------------------------------------------------------


async def test_success_truncated_keeps_head_hint(middleware):
    _stub_sandbox(middleware, [ExecuteResponse(output="partial", exit_code=0, truncated=True)])
    result = await middleware._execute("x")
    assert "partial" in result
    assert "开头部分" in result


async def test_failure_truncated_keeps_tail_hint(middleware):
    _stub_sandbox(middleware, [ExecuteResponse(output="...Traceback tail", exit_code=1, truncated=True)])
    result = await middleware._execute("x")
    assert "exit_code=1" in result
    assert "尾部" in result


async def test_success_empty_output_message(middleware):
    _stub_sandbox(middleware, [ExecuteResponse(output="", exit_code=0, truncated=False)])
    result = await middleware._execute("x")
    assert "无输出" in result or "执行成功" in result


# ---------------------------------------------------------------------------
# 沙盒命令构造（head/tail 策略 + 环境变量注入）
# ---------------------------------------------------------------------------


async def test_run_in_sandbox_command_and_uploads(middleware, monkeypatch):
    from starring.agents.backends.sandbox import backend as sandbox_backend

    recorded = {}

    class FakeBackend:
        def __init__(self, thread_id, *, uid, **kwargs):
            recorded["thread_id"] = thread_id
            recorded["uid"] = uid

        def upload_files(self, files):
            recorded["files"] = files
            return [SimpleNamespace(path=path, error=None) for path, _ in files]

        def execute(self, command, *, timeout=None):
            recorded["command"] = command
            recorded["timeout"] = timeout
            return ExecuteResponse(output="done", exit_code=0, truncated=False)

    monkeypatch.setattr(sandbox_backend, "ProvisionerSandboxBackend", FakeBackend)

    response = middleware._run_in_sandbox("print('hi')", 1, "tok-abc")
    assert response.exit_code == 0
    assert recorded["thread_id"] == "t1"
    assert recorded["uid"] == "u1"

    paths = [path for path, _ in recorded["files"]]
    assert any(path.endswith("/.codeact/starring_tools.py") for path in paths)
    assert any(path.endswith("/.codeact/act_1.py") for path in paths)

    command = recorded["command"]
    assert "STARRING_BRIDGE_TOKEN=tok-abc" in command
    assert "STARRING_BRIDGE_URL=" in command
    assert "head -c" in command and "tail -c" in command
    assert "exit $ec" in command


async def test_run_in_sandbox_upload_failure_returns_error(middleware, monkeypatch):
    from starring.agents.backends.sandbox import backend as sandbox_backend

    class FakeBackend:
        def __init__(self, thread_id, *, uid, **kwargs):
            pass

        def upload_files(self, files):
            return [SimpleNamespace(path=path, error="permission_denied") for path, _ in files]

        def execute(self, command, *, timeout=None):  # pragma: no cover - 不应被调用
            raise AssertionError("上传失败后不应执行命令")

    monkeypatch.setattr(sandbox_backend, "ProvisionerSandboxBackend", FakeBackend)

    response = middleware._run_in_sandbox("print('hi')", 1, "tok-abc")
    assert response.exit_code == 1
    assert "permission_denied" in response.output


# ---------------------------------------------------------------------------
# prompt 注入与开关默认值
# ---------------------------------------------------------------------------


def test_prompt_lists_whitelist_tools(middleware):
    prompt = middleware._build_prompt()
    assert "CodeAct 使用说明" in prompt
    assert "`query_kb`" in prompt
    assert "`web_search`" in prompt
    assert "starring_tools" in prompt


def test_prompt_without_bridge_tools(monkeypatch):
    monkeypatch.setattr(code_act, "compute_bridge_whitelist", lambda context: [])
    instance = CodeActMiddleware(SimpleNamespace(uid="u1", thread_id="t1", file_thread_id=None))
    prompt = instance._build_prompt()
    assert "没有可桥接的平台工具" in prompt


def test_execute_python_tool_registered(middleware):
    assert [tool.name for tool in middleware.tools] == ["execute_python"]


def test_use_code_act_defaults_off():
    """开关默认关闭：存量智能体行为零变化（graph.py 仅在 True 时挂载）。"""
    from starring.agents.buildin.chatbot.context import ChatBotContext

    context = ChatBotContext(uid="u1", thread_id="t1")
    assert context.use_code_act is False
