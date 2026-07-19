"""触发器执行服务。

非阻塞设计：触发器 enqueue run 后立即返回，不调用 await_agent_run_result。
状态更新由 mark_run_terminal 钩子（run_worker._update_trigger_status_if_any）异步推送。

调用链路：
    [cron 元任务] → enqueue execute_trigger_run → execute_trigger → _do_execute_trigger
    [webhook HTTP] → execute_webhook_trigger → _do_execute_trigger
    _do_execute_trigger:
        1. create_conversation(uid=trigger.uid, agent_id=trigger.agent_id)
        2. create_run(... meta={source:"cron"/"webhook", trigger_id, trigger_name})
        3. mark_running(trigger.id, run_id)
        4. return {"status": "queued", "run_id": ...}  ← 立即返回

    [ARQ worker 后续 process_agent_run] → mark_run_terminal → 钩子更新 Trigger.last_run_status
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from starring.repositories.conversation_repository import ConversationRepository
from starring.repositories.trigger_repository import TriggerRepository
from starring.services.agent_run_service import create_run
from starring.services.trigger.webhook import verify_signature
from starring.storage.postgres.manager import pg_manager
from starring.utils.datetime_utils import utc_now_naive
from starring.utils.logging_config import logger

if TYPE_CHECKING:
    from starring.storage.postgres.models_business import Trigger


async def execute_trigger(*, trigger_id: str, scheduled_time_iso: str | None = None) -> dict:
    """cron 元任务入口：到点触发器执行。

    Args:
        trigger_id: 触发器 ID
        scheduled_time_iso: 调度时间的 ISO 字符串（用于 conversation title 标识）

    Returns:
        {"status": "queued"/"skipped"/"failed", "run_id": ..., "trigger_id": ...}
    """
    async with pg_manager.get_async_session_context() as db:
        trigger = await TriggerRepository(db).get(trigger_id)
        if not trigger or not trigger.is_active:
            logger.info(f"Trigger {trigger_id} skipped: not found or inactive")
            return {"status": "skipped", "reason": "trigger inactive or not found"}

        return await _do_execute_trigger(
            db, trigger, payload={"scheduled_time": scheduled_time_iso}
        )


async def execute_webhook_trigger(
    *,
    trigger_id: str,
    body: bytes,
    signature: str,
    timestamp: str,
) -> dict:
    """webhook HTTP 入口：校验签名后执行触发器。

    Raises:
        HTTPException 404: 触发器不存在或未启用
        HTTPException 401: 签名校验失败
    """
    async with pg_manager.get_async_session_context() as db:
        trigger = await TriggerRepository(db).get(trigger_id)
        if not trigger or trigger.trigger_type != "webhook" or not trigger.is_active:
            raise HTTPException(status_code=404, detail="触发器不存在或未启用")

        secret = (trigger.config or {}).get("secret", "")
        if not verify_signature(secret, signature, timestamp, body):
            logger.warning(f"Webhook trigger {trigger_id} signature verification failed")
            raise HTTPException(status_code=401, detail="签名校验失败")

        # 解析 body：合法 JSON → dict；非法 → 包成 {"_raw": ...}
        try:
            payload = json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"_raw": body.decode("utf-8", errors="replace")}

        payload["_webhook_timestamp"] = timestamp
        return await _do_execute_trigger(db, trigger, payload=payload)


async def _do_execute_trigger(
    db: AsyncSession, trigger: "Trigger", payload: dict | None = None
) -> dict:
    """通用执行路径（非阻塞）：创建 conversation → create_run → 标记 running → 立即返回。

    不阻塞等待 run 结果——run 终结后由 mark_run_terminal 钩子异步更新 Trigger.last_run_status。
    原因：若调用 await_agent_run_result 会占满 ARQ worker 并发槽，阻塞普通 chat run。
    """
    try:
        # 1. 创建 conversation（每次触发独立 thread）
        conv_repo = ConversationRepository(db)
        scheduled_time = (payload or {}).get("scheduled_time") or utc_now_naive().isoformat()
        title = f"[{trigger.trigger_type}] {trigger.name} {scheduled_time}"
        conversation = await conv_repo.create_conversation(
            uid=trigger.uid, agent_id=trigger.agent_id, title=title,
        )

        # 2. 调 create_run（service 层 enqueue ARQ 任务后立即返回）
        run_response = await create_run(
            query=trigger.input_query or _default_query(trigger, payload),
            agent_id=trigger.agent_id,
            thread_id=conversation.thread_id,
            meta={
                "source": trigger.trigger_type,  # "cron" or "webhook"
                "trigger_id": trigger.id,
                "trigger_name": trigger.name,
            },
            image_content=None,
            current_uid=trigger.uid,
            db=db,
        )
        run_id = run_response["run_id"]

        # 3. 更新触发器状态为 running（last_run_id / last_run_at / last_run_status="running"）
        await TriggerRepository(db).mark_running(trigger.id, run_id)

        # 4. 立即返回，不等待 run 结果（run 终结后由 mark_run_terminal 钩子更新 Trigger 状态）
        return {
            "status": "queued",
            "run_id": run_id,
            "trigger_id": trigger.id,
            "thread_id": conversation.thread_id,
        }

    except HTTPException as e:
        # service 层抛 HTTPException，触发器上下文转 logger
        logger.warning(
            f"Trigger {trigger.id} failed: status={e.status_code} detail={e.detail}"
        )
        await TriggerRepository(db).mark_finished(trigger.id, "failed", None)
        return {
            "status": "failed",
            "trigger_id": trigger.id,
            "error": {"detail": str(e.detail), "code": e.status_code},
        }
    except Exception as e:
        logger.exception(f"Trigger {trigger.id} unexpected error: {e}")
        await TriggerRepository(db).mark_finished(trigger.id, "failed", None)
        return {
            "status": "failed",
            "trigger_id": trigger.id,
            "error": {"detail": str(e)},
        }


def _default_query(trigger: "Trigger", payload: dict | None) -> str:
    """触发器未配置 input_query 时的默认 query。"""
    if trigger.trigger_type == "cron":
        return f"请按定时任务「{trigger.name}」执行"
    return f"请按 Webhook 触发器「{trigger.name}」执行，参数：{payload}"
