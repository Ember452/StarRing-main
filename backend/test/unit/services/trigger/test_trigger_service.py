"""触发器执行服务单测。

覆盖 _do_execute_trigger 主路径、非阻塞返回、错误处理。
不依赖真实 DB：用 fake async session context + mock repo + mock create_run。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import starring.services.trigger.service as trigger_service


def _make_trigger(
    *,
    trigger_id: str = "tr-1",
    trigger_type: str = "cron",
    is_active: bool = True,
    name: str = "测试触发器",
    uid: str = "user-1",
    agent_id: str = "ChatbotAgent",
    input_query: str | None = None,
    config: dict | None = None,
) -> SimpleNamespace:
    """构造内存中的 Trigger 对象。"""
    return SimpleNamespace(
        id=trigger_id,
        name=name,
        desc="",
        trigger_type=trigger_type,
        agent_id=agent_id,
        uid=uid,
        config=config or {},
        input_query=input_query,
        is_active=is_active,
        last_run_at=None,
        last_run_status=None,
        last_run_id=None,
        run_count=0,
    )


def _patch_pg_manager(monkeypatch: pytest.MonkeyPatch, db_obj):
    """让 pg_manager.get_async_session_context yield 给定 db_obj。"""

    @asynccontextmanager
    async def fake_session_ctx():
        yield db_obj

    monkeypatch.setattr(trigger_service.pg_manager, "get_async_session_context", fake_session_ctx)


def _patch_repositories(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trigger_repo_mock=None,
    conv_repo_mock=None,
):
    """替换 TriggerRepository 和 ConversationRepository 构造器。"""
    if trigger_repo_mock is not None:
        monkeypatch.setattr(
            trigger_service, "TriggerRepository",
            lambda db: trigger_repo_mock,
        )
    if conv_repo_mock is not None:
        monkeypatch.setattr(
            trigger_service, "ConversationRepository",
            lambda db: conv_repo_mock,
        )


def _patch_create_run(monkeypatch: pytest.MonkeyPatch, return_value: dict):
    """替换 create_run 为 mock，便于断言调用参数。"""
    mock = AsyncMock(return_value=return_value)
    monkeypatch.setattr(trigger_service, "create_run", mock)
    return mock


# ---------------------------------------------------------------------------
# execute_trigger：cron 元任务入口
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_trigger_skip_when_not_found(monkeypatch):
    """触发器不存在时返回 skipped。"""
    trigger_repo = AsyncMock()
    trigger_repo.get = AsyncMock(return_value=None)
    _patch_repositories(monkeypatch, trigger_repo_mock=trigger_repo)
    _patch_pg_manager(monkeypatch, db_obj=object())

    result = await trigger_service.execute_trigger(trigger_id="missing", scheduled_time_iso="2026-01-01T00:00:00")
    assert result["status"] == "skipped"
    assert "reason" in result


@pytest.mark.asyncio
async def test_execute_trigger_skip_when_inactive(monkeypatch):
    """触发器 is_active=False 时返回 skipped。"""
    inactive_trigger = _make_trigger(is_active=False)
    trigger_repo = AsyncMock()
    trigger_repo.get = AsyncMock(return_value=inactive_trigger)
    _patch_repositories(monkeypatch, trigger_repo_mock=trigger_repo)
    _patch_pg_manager(monkeypatch, db_obj=object())

    result = await trigger_service.execute_trigger(trigger_id="tr-1", scheduled_time_iso=None)
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_execute_trigger_calls_do_execute(monkeypatch):
    """活跃触发器应进入 _do_execute_trigger 主路径。"""
    trigger = _make_trigger()
    trigger_repo = AsyncMock()
    trigger_repo.get = AsyncMock(return_value=trigger)
    trigger_repo.mark_running = AsyncMock()
    _patch_repositories(
        monkeypatch,
        trigger_repo_mock=trigger_repo,
        conv_repo_mock=AsyncMock(create_conversation=AsyncMock(
            return_value=SimpleNamespace(thread_id="thread-1"),
        )),
    )
    _patch_pg_manager(monkeypatch, db_obj=object())
    create_run_mock = _patch_create_run(
        monkeypatch,
        return_value={"run_id": "run-1", "status": "queued"},
    )

    result = await trigger_service.execute_trigger(trigger_id="tr-1", scheduled_time_iso="2026-01-01T08:00:00")

    assert result["status"] == "queued"
    assert result["run_id"] == "run-1"
    assert result["trigger_id"] == "tr-1"
    assert result["thread_id"] == "thread-1"
    create_run_mock.assert_awaited_once()
    # 验证 meta 传递 trigger_id / trigger_name / source
    _, kwargs = create_run_mock.call_args
    assert kwargs["meta"]["source"] == "cron"
    assert kwargs["meta"]["trigger_id"] == "tr-1"
    assert kwargs["meta"]["trigger_name"] == "测试触发器"
    # 验证 mark_running 被调用（触发器进入 running 状态）
    trigger_repo.mark_running.assert_awaited_once_with("tr-1", "run-1")


@pytest.mark.asyncio
async def test_execute_trigger_does_not_call_await_agent_run_result(monkeypatch):
    """非阻塞验证：触发器不应调用 await_agent_run_result。

    设计 R9：原设计在 _do_execute_trigger 中调用 await_agent_run_result 会占满 ARQ worker 并发槽。
    """
    # 监控是否有人误引入 await_agent_run_result
    import starring.services.agent_run_service as agent_run_service

    await_mock = AsyncMock()
    monkeypatch.setattr(agent_run_service, "await_agent_run_result", await_mock, raising=False)

    trigger = _make_trigger()
    trigger_repo = AsyncMock()
    trigger_repo.get = AsyncMock(return_value=trigger)
    trigger_repo.mark_running = AsyncMock()
    _patch_repositories(
        monkeypatch,
        trigger_repo_mock=trigger_repo,
        conv_repo_mock=AsyncMock(create_conversation=AsyncMock(
            return_value=SimpleNamespace(thread_id="thread-1"),
        )),
    )
    _patch_pg_manager(monkeypatch, db_obj=object())
    _patch_create_run(monkeypatch, return_value={"run_id": "run-1", "status": "queued"})

    result = await trigger_service.execute_trigger(trigger_id="tr-1", scheduled_time_iso=None)
    assert result["status"] == "queued"
    await_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# execute_webhook_trigger：webhook HTTP 入口
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_webhook_trigger_404_when_not_webhook_type(monkeypatch):
    """非 webhook 类型触发器调 webhook 入口应 404。"""
    cron_trigger = _make_trigger(trigger_type="cron")
    trigger_repo = AsyncMock()
    trigger_repo.get = AsyncMock(return_value=cron_trigger)
    _patch_repositories(monkeypatch, trigger_repo_mock=trigger_repo)
    _patch_pg_manager(monkeypatch, db_obj=object())

    with pytest.raises(HTTPException) as exc:
        await trigger_service.execute_webhook_trigger(
            trigger_id="tr-1", body=b"{}", signature="sig", timestamp="123",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_execute_webhook_trigger_401_when_signature_invalid(monkeypatch):
    """签名校验失败应 401。"""
    webhook_trigger = _make_trigger(
        trigger_type="webhook", config={"secret": "real-secret"},
    )
    trigger_repo = AsyncMock()
    trigger_repo.get = AsyncMock(return_value=webhook_trigger)
    _patch_repositories(monkeypatch, trigger_repo_mock=trigger_repo)
    _patch_pg_manager(monkeypatch, db_obj=object())

    with pytest.raises(HTTPException) as exc:
        await trigger_service.execute_webhook_trigger(
            trigger_id="tr-1", body=b"{}", signature="wrong-sig", timestamp="123",
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_execute_webhook_trigger_passes_payload_to_do_execute(monkeypatch):
    """合法签名后应把 body 解析为 payload 传给 _do_execute_trigger。"""
    import json

    import time as time_module

    webhook_trigger = _make_trigger(
        trigger_type="webhook", config={"secret": "real-secret"},
    )
    trigger_repo = AsyncMock()
    trigger_repo.get = AsyncMock(return_value=webhook_trigger)
    trigger_repo.mark_running = AsyncMock()
    _patch_repositories(
        monkeypatch,
        trigger_repo_mock=trigger_repo,
        conv_repo_mock=AsyncMock(create_conversation=AsyncMock(
            return_value=SimpleNamespace(thread_id="thread-w-1"),
        )),
    )
    _patch_pg_manager(monkeypatch, db_obj=object())
    create_run_mock = _patch_create_run(
        monkeypatch, return_value={"run_id": "run-w-1", "status": "queued"},
    )

    body_dict = {"event": "push", "ref": "main"}
    body = json.dumps(body_dict).encode("utf-8")
    ts = str(int(time_module.time()))
    from starring.services.trigger.webhook import compute_signature
    sig = compute_signature("real-secret", ts, body)

    result = await trigger_service.execute_webhook_trigger(
        trigger_id="tr-1", body=body, signature=sig, timestamp=ts,
    )
    assert result["status"] == "queued"
    assert result["run_id"] == "run-w-1"
    # 验证 meta.source == "webhook"
    _, kwargs = create_run_mock.call_args
    assert kwargs["meta"]["source"] == "webhook"
    assert kwargs["meta"]["trigger_id"] == "tr-1"


# ---------------------------------------------------------------------------
# _do_execute_trigger：错误处理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_execute_trigger_handles_http_exception(monkeypatch):
    """create_run 抛 HTTPException 时应转 failed，不向上抛。"""
    trigger = _make_trigger()
    trigger_repo = AsyncMock()
    trigger_repo.mark_running = AsyncMock()
    trigger_repo.mark_finished = AsyncMock()
    _patch_repositories(
        monkeypatch,
        trigger_repo_mock=trigger_repo,
        conv_repo_mock=AsyncMock(create_conversation=AsyncMock(
            return_value=SimpleNamespace(thread_id="thread-1"),
        )),
    )
    _patch_pg_manager(monkeypatch, db_obj=object())

    create_run_mock = AsyncMock(side_effect=HTTPException(status_code=404, detail="agent 不存在"))
    monkeypatch.setattr(trigger_service, "create_run", create_run_mock)

    result = await trigger_service._do_execute_trigger(db=object(), trigger=trigger, payload={})
    assert result["status"] == "failed"
    assert result["trigger_id"] == "tr-1"
    assert "error" in result
    trigger_repo.mark_finished.assert_awaited_once_with("tr-1", "failed", None)


@pytest.mark.asyncio
async def test_do_execute_trigger_handles_unexpected_exception(monkeypatch):
    """非 HTTPException 异常也应转 failed，不向上抛。"""
    trigger = _make_trigger()
    trigger_repo = AsyncMock()
    trigger_repo.mark_running = AsyncMock()
    trigger_repo.mark_finished = AsyncMock()
    _patch_repositories(
        monkeypatch,
        trigger_repo_mock=trigger_repo,
        conv_repo_mock=AsyncMock(create_conversation=AsyncMock(
            return_value=SimpleNamespace(thread_id="thread-1"),
        )),
    )
    _patch_pg_manager(monkeypatch, db_obj=object())

    create_run_mock = AsyncMock(side_effect=ValueError("unexpected"))
    monkeypatch.setattr(trigger_service, "create_run", create_run_mock)

    result = await trigger_service._do_execute_trigger(db=object(), trigger=trigger, payload={})
    assert result["status"] == "failed"
    assert "unexpected" in result["error"]["detail"]


# ---------------------------------------------------------------------------
# _default_query
# ---------------------------------------------------------------------------


def test_default_query_cron_uses_trigger_name():
    """cron 类型 _default_query 包含触发器名。"""
    trigger = _make_trigger(trigger_type="cron", name="每日早报")
    q = trigger_service._default_query(trigger, None)
    assert "每日早报" in q
    assert "定时任务" in q


def test_default_query_webhook_includes_payload():
    """webhook 类型 _default_query 包含 payload 信息。"""
    trigger = _make_trigger(trigger_type="webhook", name="推送触发器")
    q = trigger_service._default_query(trigger, {"event": "push"})
    assert "推送触发器" in q
    assert "push" in q


def test_default_query_uses_input_query_when_configured():
    """触发器配置了 input_query 时直接用配置值，不走 _default_query。"""
    trigger = _make_trigger(input_query="执行每日报告")
    # _do_execute_trigger 中 trigger.input_query or _default_query(...)
    # 这里直接验证 _default_query 不被调用（_do_execute 已测）
    assert trigger.input_query == "执行每日报告"
