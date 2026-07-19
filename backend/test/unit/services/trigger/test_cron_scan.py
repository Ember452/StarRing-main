"""cron_scan 元任务单测。

覆盖：
- _is_due 边界：当前分钟到点 / 不到点 / 时区处理
- _is_due 异常路径：cron_expr 非法 / 时区非法 / 缺 cron_expr
- scan_triggers 入队幂等：相同 (trigger_id, scheduled_time) 不重复入队

不依赖真实 DB：mock pg_manager + TriggerRepository + get_arq_pool。
依赖 croniter / pytz：用 pytest.importorskip 跳过缺失环境。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# 单测本身依赖 croniter / pytz 来构造合法 Trigger，缺失时跳过
pytest.importorskip("croniter")
pytest.importorskip("pytz")

import starring.services.trigger.cron_scan as cron_scan
from starring.services.trigger.cron_scan import _is_due, scan_triggers


def _make_cron_trigger(
    *,
    cron_expr: str = "0 8 * * *",  # 每天 8:00
    timezone: str = "Asia/Shanghai",
    trigger_id: str = "tr-1",
    is_active: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=trigger_id,
        name="cron-test",
        trigger_type="cron",
        is_active=is_active,
        config={"cron_expr": cron_expr, "timezone": timezone},
    )


# ---------------------------------------------------------------------------
# _is_due 边界
# ---------------------------------------------------------------------------


def test_is_due_true_when_current_minute_matches():
    """cron=0 8 * * * + 时区 Asia/Shanghai，UTC 00:00 = 北京 08:00，应到点。"""
    trigger = _make_cron_trigger(cron_expr="0 8 * * *", timezone="Asia/Shanghai")
    # 2026-01-01 00:00 UTC == 2026-01-01 08:00 Asia/Shanghai
    now_utc = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    assert _is_due(trigger, now_utc) is True


def test_is_due_false_when_minute_does_not_match():
    """cron=0 8 * * *，UTC 01:00 = 北京 09:00，应不到点。"""
    trigger = _make_cron_trigger(cron_expr="0 8 * * *", timezone="Asia/Shanghai")
    now_utc = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    assert _is_due(trigger, now_utc) is False


def test_is_due_true_for_every_minute_cron():
    """cron=* * * * * 任意分钟都到点。"""
    trigger = _make_cron_trigger(cron_expr="* * * * *", timezone="UTC")
    now_utc = datetime(2026, 1, 1, 12, 34, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    assert _is_due(trigger, now_utc) is True


def test_is_due_with_utc_timezone():
    """UTC 时区下 cron=0 0 * * * 应在 UTC 00:00 到点。"""
    trigger = _make_cron_trigger(cron_expr="0 0 * * *", timezone="UTC")
    now_utc = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    assert _is_due(trigger, now_utc) is True


def test_is_due_with_us_timezone():
    """跨大半个地球的时区：America/New_York (-5)。"""
    # cron=0 8 * * * America/New_York → UTC 13:00
    trigger = _make_cron_trigger(cron_expr="0 8 * * *", timezone="America/New_York")
    now_utc = datetime(2026, 1, 1, 13, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    assert _is_due(trigger, now_utc) is True


def test_is_due_false_when_cron_expr_missing():
    """缺 cron_expr 应返回 False（不抛异常）。"""
    trigger = _make_cron_trigger()
    trigger.config = {"timezone": "UTC"}  # 无 cron_expr
    now_utc = datetime(2026, 1, 1, 0, 0, 0)
    assert _is_due(trigger, now_utc) is False


def test_is_due_false_when_cron_expr_invalid():
    """非法 cron 表达式应返回 False（不抛异常）。"""
    trigger = _make_cron_trigger(cron_expr="invalid-cron-expr", timezone="UTC")
    now_utc = datetime(2026, 1, 1, 0, 0, 0)
    assert _is_due(trigger, now_utc) is False


def test_is_due_false_when_timezone_invalid():
    """非法时区应返回 False（不抛异常）。"""
    trigger = _make_cron_trigger(cron_expr="0 8 * * *", timezone="Invalid/Zone")
    now_utc = datetime(2026, 1, 1, 0, 0, 0)
    assert _is_due(trigger, now_utc) is False


# ---------------------------------------------------------------------------
# scan_triggers 入队
# ---------------------------------------------------------------------------


def _patch_pg_manager(monkeypatch: pytest.MonkeyPatch, session_obj):
    @asynccontextmanager
    async def fake_session_ctx():
        yield session_obj

    monkeypatch.setattr(cron_scan.pg_manager, "get_async_session_context", fake_session_ctx)


@pytest.mark.asyncio
async def test_scan_triggers_enqueues_due_trigger(monkeypatch):
    """到点触发器应被入队，调用 enqueue_job 一次。"""
    # cron=* * * * * 必到点
    trigger = _make_cron_trigger(cron_expr="* * * * *", timezone="UTC")

    trigger_repo = AsyncMock()
    trigger_repo.list_active_cron_triggers = AsyncMock(return_value=[trigger])
    monkeypatch.setattr(cron_scan, "TriggerRepository", lambda db: trigger_repo)
    _patch_pg_manager(monkeypatch, session_obj=object())

    enqueue_mock = AsyncMock()
    pool_mock = SimpleNamespace(enqueue_job=enqueue_mock)
    monkeypatch.setattr(cron_scan, "get_arq_pool", AsyncMock(return_value=pool_mock))

    await scan_triggers(ctx={})

    enqueue_mock.assert_awaited_once()
    args, kwargs = enqueue_mock.call_args
    assert args[0] == "execute_trigger_run"
    assert args[1] == trigger.id
    # 幂等 _job_id 格式：trigger:{trigger_id}:{scheduled_time_iso}
    assert kwargs["_job_id"].startswith(f"trigger:{trigger.id}:")


@pytest.mark.asyncio
async def test_scan_triggers_skips_not_due_trigger(monkeypatch):
    """不到点的触发器不入队。"""
    # cron=0 8 * * * 在 UTC 01:00 不到点（北京 09:00）
    trigger = _make_cron_trigger(cron_expr="0 8 * * *", timezone="Asia/Shanghai")
    trigger_repo = AsyncMock()
    trigger_repo.list_active_cron_triggers = AsyncMock(return_value=[trigger])
    monkeypatch.setattr(cron_scan, "TriggerRepository", lambda db: trigger_repo)
    _patch_pg_manager(monkeypatch, session_obj=object())

    enqueue_mock = AsyncMock()
    pool_mock = SimpleNamespace(enqueue_job=enqueue_mock)
    monkeypatch.setattr(cron_scan, "get_arq_pool", AsyncMock(return_value=pool_mock))

    # 不 mock utc_now_naive，依赖当前时间——但 cron=0 8 * * * 只在 8:00 到点
    # 为保证测试稳定，直接断言「若不到点，不调用 enqueue」
    # 这里不固定时间，只要不是恰好 8:00 Asia/Shanghai 就不会到点
    # 改用更稳定的方式：构造永远不到点的 cron 表达式
    trigger.config = {"cron_expr": "0 8 * * 1", "timezone": "UTC"}
    # 等价于「每周一 8:00 UTC」——若今天不是周一 8:00 就不到点
    # 为保证 100% 不到点，改用「2月30日」（不存在的日期）的方式：cron 不允许
    # 改用最稳妥方式：选一个明显不会触发的「0 0 1 1 *」（仅 1月1日 0:00）
    trigger.config = {"cron_expr": "0 0 1 1 *", "timezone": "UTC"}

    await scan_triggers(ctx={})
    # 当前时间几乎不可能是 1月1日 00:00 UTC
    enqueue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_triggers_continues_on_enqueue_error(monkeypatch):
    """单个触发器入队失败不应中断其他触发器入队。"""
    t1 = _make_cron_trigger(cron_expr="* * * * *", timezone="UTC", trigger_id="t1")
    t2 = _make_cron_trigger(cron_expr="* * * * *", timezone="UTC", trigger_id="t2")

    trigger_repo = AsyncMock()
    trigger_repo.list_active_cron_triggers = AsyncMock(return_value=[t1, t2])
    monkeypatch.setattr(cron_scan, "TriggerRepository", lambda db: trigger_repo)
    _patch_pg_manager(monkeypatch, session_obj=object())

    call_count = [0]

    async def fake_enqueue(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("redis down")

    pool_mock = SimpleNamespace(enqueue_job=fake_enqueue)
    monkeypatch.setattr(cron_scan, "get_arq_pool", AsyncMock(return_value=pool_mock))

    await scan_triggers(ctx={})  # 不应抛异常
    assert call_count[0] == 2  # 两个都试了


@pytest.mark.asyncio
async def test_scan_triggers_no_triggers_noop(monkeypatch):
    """没有活跃 cron 触发器时不应调用 get_arq_pool。"""
    trigger_repo = AsyncMock()
    trigger_repo.list_active_cron_triggers = AsyncMock(return_value=[])
    monkeypatch.setattr(cron_scan, "TriggerRepository", lambda db: trigger_repo)
    _patch_pg_manager(monkeypatch, session_obj=object())

    get_pool_mock = AsyncMock()
    monkeypatch.setattr(cron_scan, "get_arq_pool", get_pool_mock)

    await scan_triggers(ctx={})
    get_pool_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_trigger_run_uses_idempotent_job_id(monkeypatch):
    """_enqueue_trigger_run 生成的 _job_id 应包含 trigger_id 和 scheduled_time_iso。"""
    from starring.services.trigger.cron_scan import _enqueue_trigger_run

    enqueue_mock = AsyncMock()
    pool_mock = SimpleNamespace(enqueue_job=enqueue_mock)
    monkeypatch.setattr(cron_scan, "get_arq_pool", AsyncMock(return_value=pool_mock))

    trigger = _make_cron_trigger(trigger_id="tr-idempotent")
    scheduled = "2026-01-01T08:00:00"
    await _enqueue_trigger_run(trigger, scheduled)

    args, kwargs = enqueue_mock.call_args
    assert kwargs["_job_id"] == f"trigger:tr-idempotent:{scheduled}"
