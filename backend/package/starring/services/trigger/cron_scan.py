"""cron 触发器扫描元任务。

方案 C（评审决策）：ARQ 的 cron 在 WorkerSettings 启动时静态注册，
用户在前端动态增删 cron 触发器不能重启 worker。采用元任务扫描模式：
- WorkerSettings.cron_jobs 注册 cron(scan_triggers, minute=set()) 每分钟执行
- scan_triggers 扫描 triggers 表，找出到点的触发器
- 到点的触发器 enqueue_job("execute_trigger_run", trigger_id, scheduled_time)
- 幂等：_job_id = f"trigger:{trigger_id}:{scheduled_time_iso}"
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from starring.repositories.trigger_repository import TriggerRepository
from starring.services.run_queue_service import get_arq_pool
from starring.storage.postgres.manager import pg_manager
from starring.storage.postgres.models_business import Trigger
from starring.utils.datetime_utils import utc_now_naive
from starring.utils.logging_config import logger

# 模块加载时一次性导入 cron 依赖，避免 _is_due 每次调用都尝试 import。
# 未安装时记录 error 并设置可用性标志，_is_due 直接返回 False（保持原行为）。
try:
    from croniter import croniter  # type: ignore[import-not-found]
    import pytz  # type: ignore[import-not-found]

    _CRON_DEPS_AVAILABLE = True
except ImportError as e:
    _CRON_DEPS_AVAILABLE = False
    logger.error(
        f"croniter/pytz not installed, cron triggers will be skipped: {e}"
    )


async def scan_triggers(ctx: dict) -> None:
    """ARQ cron 元任务入口：每分钟扫描 triggers 表，到点的触发器入队执行。

    幂等性：相同 (trigger_id, scheduled_time) 维度只入队一次（ARQ _job_id 去重）。
    并发安全：本任务由 ARQ cron 注册，单 worker 进程内串行执行（无并发风险）；
    多 worker 部署时仍依赖 _job_id 去重，未引入分布式锁（首期规模可控）。
    """
    del ctx
    now_utc = utc_now_naive()

    async with pg_manager.get_async_session_context() as session:
        triggers = await TriggerRepository(session).list_active_cron_triggers()
        if not triggers:
            return

        # 归一化到分钟级（避免秒级抖动导致同一分钟多次入队）
        scheduled_time = now_utc.replace(second=0, microsecond=0)
        scheduled_time_iso = scheduled_time.isoformat()

        enqueued_count = 0
        for trigger in triggers:
            if not _is_due(trigger, now_utc):
                continue
            try:
                await _enqueue_trigger_run(trigger, scheduled_time_iso)
                enqueued_count += 1
            except Exception as e:
                logger.exception(
                    f"Failed to enqueue trigger {trigger.id} for {scheduled_time_iso}: {e}"
                )

        if enqueued_count > 0:
            logger.info(f"scan_triggers: enqueued {enqueued_count} trigger(s) for {scheduled_time_iso}")


def _is_due(trigger: Trigger, now_utc: datetime) -> bool:
    """判断触发器是否在当前 UTC 分钟到点（按 config.timezone 解析 cron_expr）。

    算法：
    1. 把 now_utc 转为用户配置时区的本地时间
    2. 归一化到分钟（去掉秒和微秒）
    3. 用 croniter 从「上一分钟」开始算 next，判断 next 是否等于当前分钟
       （用「上一分钟」作基准是因为 croniter.get_next 严格大于基准，
        若用「当前分钟」作基准会跳过当前分钟本身）

    Args:
        trigger: Trigger 模型实例
        now_utc: 当前 UTC naive 时间

    Returns:
        True 表示触发器在当前分钟到点
    """
    if not _CRON_DEPS_AVAILABLE:
        # 依赖未安装时直接返回 False（模块加载时已记录 error 日志）
        return False

    config = trigger.config or {}
    cron_expr = config.get("cron_expr")
    if not cron_expr:
        return False

    timezone_name = config.get("timezone") or "Asia/Shanghai"
    try:
        tz = pytz.timezone(timezone_name)
    except Exception as e:
        logger.warning(
            f"Invalid timezone={timezone_name} for trigger={trigger.id}: {e}"
        )
        return False

    # 把 UTC naive 当作 UTC，转为用户时区本地时间，再归一化到分钟
    now_local = now_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    now_local_minute = now_local.replace(second=0, microsecond=0)

    try:
        cron = croniter(cron_expr, now_local_minute - timedelta(minutes=1))
        next_time = cron.get_next(datetime)
        # 比对时去掉时区信息和微秒
        next_time_minute = next_time.replace(second=0, microsecond=0)
        # croniter 返回的时间可能是 naive（无 tzinfo），now_local_minute 有 tzinfo
        # 比对时统一去掉 tzinfo
        return next_time_minute.replace(tzinfo=None) == now_local_minute.replace(tzinfo=None)
    except Exception as e:
        logger.warning(
            f"Invalid cron_expr={cron_expr!r} for trigger={trigger.id}: {e}"
        )
        return False


async def _enqueue_trigger_run(trigger: Trigger, scheduled_time_iso: str) -> None:
    """入队触发器执行任务。

    幂等 key：trigger:{trigger_id}:{scheduled_time_iso}
    ARQ 相同 _job_id 不会重复入队。
    """
    job_id = f"trigger:{trigger.id}:{scheduled_time_iso}"
    queue = await get_arq_pool()
    await queue.enqueue_job(
        "execute_trigger_run",
        trigger.id,
        scheduled_time_iso,
        _job_id=job_id,
    )
