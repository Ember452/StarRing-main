"""Trigger repository - 触发器配置 CRUD + 状态更新。"""

from __future__ import annotations

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from starring.storage.postgres.models_business import Trigger
from starring.utils.datetime_utils import utc_now_naive


class TriggerRepository:
    """触发器配置数据访问层，封装对 triggers 表的全部数据库操作。"""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def get(self, trigger_id: str) -> Trigger | None:
        result = await self.db.execute(select(Trigger).where(Trigger.id == trigger_id))
        return result.scalar_one_or_none()

    async def get_for_user(self, trigger_id: str, uid: str) -> Trigger | None:
        """仅返回属于当前用户的触发器（管理 API 鉴权用）。"""
        result = await self.db.execute(select(Trigger).where(and_(Trigger.id == trigger_id, Trigger.uid == str(uid))))
        return result.scalar_one_or_none()

    async def list_for_user(
        self,
        *,
        uid: str,
        trigger_type: str | None = None,
        agent_id: str | None = None,
        is_active: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[Trigger]:
        """列出当前用户的触发器，支持按类型/agent/启用状态过滤。"""
        stmt = select(Trigger).where(Trigger.uid == str(uid))
        if trigger_type:
            stmt = stmt.where(Trigger.trigger_type == trigger_type)
        if agent_id:
            stmt = stmt.where(Trigger.agent_id == agent_id)
        if is_active is not None:
            stmt = stmt.where(Trigger.is_active.is_(is_active))
        stmt = stmt.order_by(Trigger.created_at.desc()).offset(offset).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_active_cron_triggers(self) -> list[Trigger]:
        """cron 元任务扫描用：列出所有启用的定时类触发器（cron / kb_sync，不限用户）。"""
        result = await self.db.execute(
            select(Trigger).where(
                and_(
                    Trigger.trigger_type.in_(("cron", "kb_sync")),
                    Trigger.is_active.is_(True),
                )
            )
        )
        return list(result.scalars().all())

    async def create(self, trigger: Trigger) -> Trigger:
        self.db.add(trigger)
        await self.db.commit()
        await self.db.refresh(trigger)
        return trigger

    async def update_fields(self, trigger: Trigger, *, fields: dict) -> Trigger:
        """字段级更新：仅更新 fields 中传入的键。"""
        for key, value in fields.items():
            if hasattr(trigger, key):
                setattr(trigger, key, value)
        await self.db.commit()
        await self.db.refresh(trigger)
        return trigger

    async def delete(self, trigger: Trigger) -> None:
        await self.db.delete(trigger)
        await self.db.commit()

    async def mark_running(self, trigger_id: str, run_id: str) -> None:
        """标记触发器进入 running 状态（不递增 run_count，run_count 在终结时递增）。"""
        await self.db.execute(
            update(Trigger)
            .where(Trigger.id == trigger_id)
            .values(
                last_run_id=run_id,
                last_run_at=utc_now_naive(),
                last_run_status="running",
            )
        )
        await self.db.commit()

    async def mark_finished(self, trigger_id: str, status: str, run_id: str | None) -> None:
        """无幂等保护的终结标记（用于触发器侧异常路径，此时 run_id 可能为 None）。"""
        values: dict = {
            "last_run_status": status,
            "updated_at": utc_now_naive(),
        }
        if run_id is not None:
            values["last_run_id"] = run_id
            values["run_count"] = Trigger.run_count + 1
        await self.db.execute(update(Trigger).where(Trigger.id == trigger_id).values(**values))
        await self.db.commit()

    async def mark_finished_if_current(self, trigger_id: str, run_id: str, status: str) -> None:
        """幂等保护：仅当 trigger.last_run_id == run_id 时才更新。

        用途：mark_run_terminal 钩子调用，避免旧 run 终结覆盖新 run 的状态。
        注意：run_count 也在此时递增（仅当成功更新时）。
        """
        await self.db.execute(
            update(Trigger)
            .where(and_(Trigger.id == trigger_id, Trigger.last_run_id == run_id))
            .values(
                last_run_status=status,
                run_count=Trigger.run_count + 1,
                updated_at=utc_now_naive(),
            )
        )
        await self.db.commit()
