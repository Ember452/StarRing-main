"""codeact_service 单元测试：白名单规则、token 生命周期、分发校验与结果上限。"""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault(
    "SAVE_DIR", os.path.join(os.environ.get("CLAUDE_JOB_DIR", tempfile.gettempdir()), "starring-test-saves")
)

from starring.services.codeact_service import (
    BRIDGE_EXCLUDED_TOOLS,
    MAX_RESULT_BYTES,
    BridgeError,
    compute_bridge_whitelist,
    create_bridge_token,
    dispatch_tool_call,
    revoke_bridge_token,
)

pytestmark = pytest.mark.unit


class FakeTool:
    """最小工具桩：只需 name 与 func/coroutine 属性。"""

    def __init__(self, name: str, func=None, coroutine=None):
        self.name = name
        self.func = func
        self.coroutine = coroutine


class FakeRedis:
    """内存版 redis 桩：记录 set 时的 TTL，便于断言。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    redis = FakeRedis()

    async def get_redis_client():
        return redis

    import starring.services.run_queue_service as run_queue_service

    monkeypatch.setattr(run_queue_service, "get_redis_client", get_redis_client)
    return redis


@pytest.fixture
def fake_categories(monkeypatch):
    """按类别注册假工具：buildin/knowledge/task 各若干，含排除清单成员。"""
    categories = {
        "buildin": [FakeTool("web_search"), FakeTool("ask_user_question"), FakeTool("install_skill")],
        "knowledge": [FakeTool("query_kb"), FakeTool("list_kb"), FakeTool("present_artifacts")],
        "task": [FakeTool("task_dispatch")],
    }

    import starring.agents.toolkits.service as toolkit_service

    monkeypatch.setattr(
        toolkit_service,
        "get_tool_instances_by_category",
        lambda category: categories.get(category, []),
    )
    return categories


# ---------------------------------------------------------------------------
# 白名单规则
# ---------------------------------------------------------------------------


def test_whitelist_buildin_selected_and_knowledge_included(fake_categories):
    context = SimpleNamespace(tools=["web_search", "task_dispatch", "not_registered"], use_knowledge=None)
    assert compute_bridge_whitelist(context) == ["list_kb", "query_kb", "web_search"]


def test_whitelist_excluded_tools_never_pass(fake_categories):
    context = SimpleNamespace(tools=["web_search", "ask_user_question", "install_skill"], use_knowledge=True)
    whitelist = compute_bridge_whitelist(context)
    assert set(whitelist) & BRIDGE_EXCLUDED_TOOLS == set()
    assert "present_artifacts" not in whitelist


def test_whitelist_use_knowledge_false_drops_kb_tools(fake_categories):
    context = SimpleNamespace(tools=["web_search"], use_knowledge=False)
    assert compute_bridge_whitelist(context) == ["web_search"]


def test_whitelist_empty_tools(fake_categories):
    context = SimpleNamespace(tools=None, use_knowledge=False)
    assert compute_bridge_whitelist(context) == []


# ---------------------------------------------------------------------------
# token 生命周期
# ---------------------------------------------------------------------------


async def test_create_token_writes_snapshot_with_ttl(fake_redis, fake_categories):
    context = SimpleNamespace(
        uid="u1", thread_id="t1", run_id="r1", tools=["web_search"], use_knowledge=False, knowledges=["kb1"]
    )
    token, whitelist = await create_bridge_token(context)
    assert whitelist == ["web_search"]

    key = f"codeact:token:{token}"
    snapshot = json.loads(fake_redis.store[key])
    assert snapshot == {
        "uid": "u1",
        "thread_id": "t1",
        "run_id": "r1",
        "allowed_tools": ["web_search"],
        "knowledges": ["kb1"],
    }
    from starring import config as conf

    assert fake_redis.ttls[key] == int(conf.sandbox_exec_timeout_seconds) + 60


async def test_revoked_token_rejected(fake_redis, fake_categories):
    context = SimpleNamespace(uid="u1", thread_id="t1", tools=["web_search"], use_knowledge=False)
    token, _ = await create_bridge_token(context)
    await revoke_bridge_token(token)
    with pytest.raises(BridgeError) as exc_info:
        await dispatch_tool_call(token, "web_search", {})
    assert exc_info.value.error_type == "invalid_token"


async def test_unknown_token_rejected(fake_redis):
    with pytest.raises(BridgeError) as exc_info:
        await dispatch_tool_call("no-such-token", "web_search", {})
    assert exc_info.value.error_type == "invalid_token"


# ---------------------------------------------------------------------------
# 分发校验与执行
# ---------------------------------------------------------------------------


async def _make_token(fake_redis, allowed: list[str]) -> str:
    """直接写快照，绕开白名单计算，聚焦分发逻辑。"""
    token = "tok-test"
    snapshot = {"uid": "u1", "thread_id": "t1", "run_id": None, "allowed_tools": allowed, "knowledges": None}
    await fake_redis.set(f"codeact:token:{token}", json.dumps(snapshot))
    return token


def _patch_registry(monkeypatch, tools: list[FakeTool]):
    import starring.agents.toolkits.registry as registry

    monkeypatch.setattr(registry, "get_all_tool_instances", lambda: tools)


async def test_dispatch_tool_not_in_whitelist(fake_redis, monkeypatch):
    token = await _make_token(fake_redis, ["web_search"])
    _patch_registry(monkeypatch, [FakeTool("query_kb")])
    with pytest.raises(BridgeError) as exc_info:
        await dispatch_tool_call(token, "query_kb", {})
    assert exc_info.value.error_type == "tool_not_allowed"


async def test_dispatch_tool_not_found(fake_redis, monkeypatch):
    token = await _make_token(fake_redis, ["web_search"])
    _patch_registry(monkeypatch, [])
    with pytest.raises(BridgeError) as exc_info:
        await dispatch_tool_call(token, "web_search", {})
    assert exc_info.value.error_type == "tool_not_found"


async def test_dispatch_arguments_must_be_dict(fake_redis, monkeypatch):
    token = await _make_token(fake_redis, ["web_search"])
    with pytest.raises(BridgeError) as exc_info:
        await dispatch_tool_call(token, "web_search", ["not", "a", "dict"])
    assert exc_info.value.error_type == "invalid_arguments"


async def test_dispatch_invalid_signature_arguments(fake_redis, monkeypatch):
    async def search(query: str, runtime=None):
        return query

    token = await _make_token(fake_redis, ["web_search"])
    _patch_registry(monkeypatch, [FakeTool("web_search", coroutine=search)])
    with pytest.raises(BridgeError) as exc_info:
        await dispatch_tool_call(token, "web_search", {"nope": 1})
    assert exc_info.value.error_type == "invalid_arguments"


async def test_dispatch_injects_runtime_context(fake_redis, monkeypatch):
    seen = {}

    async def search(query: str, runtime=None):
        seen["uid"] = runtime.context.uid
        return {"query": query}

    token = await _make_token(fake_redis, ["web_search"])
    _patch_registry(monkeypatch, [FakeTool("web_search", coroutine=search)])
    result = await dispatch_tool_call(token, "web_search", {"query": "hello"})
    assert result == {"query": "hello"}
    assert seen["uid"] == "u1"


async def test_dispatch_tool_exception_wrapped(fake_redis, monkeypatch):
    async def boom(runtime=None):
        raise RuntimeError("kaboom")

    token = await _make_token(fake_redis, ["web_search"])
    _patch_registry(monkeypatch, [FakeTool("web_search", coroutine=boom)])
    with pytest.raises(BridgeError) as exc_info:
        await dispatch_tool_call(token, "web_search", {})
    assert exc_info.value.error_type == "tool_execution_error"
    assert "kaboom" in exc_info.value.message


async def test_dispatch_result_too_large_rejected(fake_redis, monkeypatch):
    async def huge(runtime=None):
        return "x" * (MAX_RESULT_BYTES + 1)

    token = await _make_token(fake_redis, ["web_search"])
    _patch_registry(monkeypatch, [FakeTool("web_search", coroutine=huge)])
    with pytest.raises(BridgeError) as exc_info:
        await dispatch_tool_call(token, "web_search", {})
    assert exc_info.value.error_type == "result_too_large"


async def test_dispatch_result_normalized_to_json(fake_redis, monkeypatch):
    async def structured(runtime=None):
        return {"items": [1, 2], "obj": object()}

    token = await _make_token(fake_redis, ["web_search"])
    _patch_registry(monkeypatch, [FakeTool("web_search", coroutine=structured)])
    result = await dispatch_tool_call(token, "web_search", {})
    assert result["items"] == [1, 2]
    assert isinstance(result["obj"], str)  # default=str 归一化
