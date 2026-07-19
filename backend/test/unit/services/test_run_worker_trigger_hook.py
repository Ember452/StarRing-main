"""mark_run_terminal 钩子单测：_update_trigger_status_if_any。

覆盖：
- run 没有 trigger_id（普通 chat run）→ 不调用 mark_finished_if_current
- run 不存在 → 直接 return
- run 有 trigger_id → 调用 mark_finished_if_current（幂等保护）
- mark_finished_if_current 抛异常 → 不向上抛，仅 logger.warning
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import starring.services.run_worker as run_worker


@pytest.mark.asyncio
async def test_update_trigger_status_skip_when_run_not_found(monkeypatch):
    """run 不存在时应直接 return，不查 trigger_id。"""
    fake_repo = AsyncMock()
    fake_repo.get_run = AsyncMock(return_value=None)
    monkeypatch.setattr(run_worker, "AgentRunRepository", lambda db: fake_repo)

    trigger_repo_cls_mock = AsyncMock()
    # 监控 TriggerRepository 是否被实例化
    import_event = []
    monkeypatch.setattr(
        "starring.repositories.trigger_repository.TriggerRepository",
        lambda db: import_event.append(db) or trigger_repo_cls_mock,
    )

    await run_worker._update_trigger_status_if_any(db=object(), run_id="missing", status="completed")
    assert import_event == []  # 未实例化 TriggerRepository


@pytest.mark.asyncio
async def test_update_trigger_status_skip_when_no_trigger_id(monkeypatch):
    """普通 chat run（input_payload 无 trigger_id）应直接 return。"""
    run = SimpleNamespace(input_payload={"query": "hello", "uid": "user-1"})  # 无 trigger_id
    fake_repo = AsyncMock()
    fake_repo.get_run = AsyncMock(return_value=run)
    monkeypatch.setattr(run_worker, "AgentRunRepository", lambda db: fake_repo)

    trigger_repo_instance = AsyncMock()
    import_event = []
    monkeypatch.setattr(
        "starring.repositories.trigger_repository.TriggerRepository",
        lambda db: import_event.append(db) or trigger_repo_instance,
    )

    await run_worker._update_trigger_status_if_any(db=object(), run_id="run-1", status="completed")
    assert import_event == []
    trigger_repo_instance.mark_finished_if_current.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_trigger_status_calls_mark_finished_if_current(monkeypatch):
    """run.input_payload.trigger_id 存在时应调用 mark_finished_if_current。"""
    run = SimpleNamespace(
        input_payload={"trigger_id": "tr-1", "source": "cron", "uid": "user-1"},
    )
    fake_repo = AsyncMock()
    fake_repo.get_run = AsyncMock(return_value=run)
    monkeypatch.setattr(run_worker, "AgentRunRepository", lambda db: fake_repo)

    trigger_repo_instance = AsyncMock()
    trigger_repo_instance.mark_finished_if_current = AsyncMock()
    monkeypatch.setattr(
        "starring.repositories.trigger_repository.TriggerRepository",
        lambda db: trigger_repo_instance,
    )

    await run_worker._update_trigger_status_if_any(
        db=object(), run_id="run-1", status="completed",
    )
    trigger_repo_instance.mark_finished_if_current.assert_awaited_once_with("tr-1", "run-1", "completed")


@pytest.mark.asyncio
async def test_update_trigger_status_handles_repo_exception(monkeypatch):
    """mark_finished_if_current 抛异常时应被捕获，不向上抛（仅 logger.warning）。"""
    run = SimpleNamespace(
        input_payload={"trigger_id": "tr-1", "source": "webhook", "uid": "user-1"},
    )
    fake_repo = AsyncMock()
    fake_repo.get_run = AsyncMock(return_value=run)
    monkeypatch.setattr(run_worker, "AgentRunRepository", lambda db: fake_repo)

    trigger_repo_instance = AsyncMock()
    trigger_repo_instance.mark_finished_if_current = AsyncMock(
        side_effect=RuntimeError("db connection lost"),
    )
    monkeypatch.setattr(
        "starring.repositories.trigger_repository.TriggerRepository",
        lambda db: trigger_repo_instance,
    )

    # 不应抛异常
    await run_worker._update_trigger_status_if_any(
        db=object(), run_id="run-1", status="failed",
    )


@pytest.mark.asyncio
async def test_mark_run_terminal_invokes_hook(monkeypatch):
    """mark_run_terminal 末尾应触发 _update_trigger_status_if_any 钩子。"""
    fake_repo = AsyncMock()
    fake_repo.set_terminal_status = AsyncMock()
    # run 存在且有 trigger_id
    fake_repo.get_run = AsyncMock(return_value=SimpleNamespace(
        input_payload={"trigger_id": "tr-1", "source": "cron", "uid": "user-1"},
    ))
    monkeypatch.setattr(run_worker, "AgentRunRepository", lambda db: fake_repo)

    trigger_repo_instance = AsyncMock()
    trigger_repo_instance.mark_finished_if_current = AsyncMock()
    monkeypatch.setattr(
        "starring.repositories.trigger_repository.TriggerRepository",
        lambda db: trigger_repo_instance,
    )

    @asynccontextmanager
    async def fake_session_ctx():
        yield object()

    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session_ctx)

    await run_worker.mark_run_terminal(run_id="run-1", status="completed")

    fake_repo.set_terminal_status.assert_awaited_once()
    trigger_repo_instance.mark_finished_if_current.assert_awaited_once_with("tr-1", "run-1", "completed")
