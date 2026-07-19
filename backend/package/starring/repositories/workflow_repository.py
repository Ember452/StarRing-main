"""Workflow repository - 工作流定义 CRUD。

封装对 workflows 表的全部数据库操作，遵循 trigger_repository.py 的模式。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §三
"""
from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from starring.storage.postgres.models_business import Workflow


class WorkflowRepository:
    """工作流定义数据访问层。"""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def get(self, workflow_id: str) -> Workflow | None:
        """按 ID 获取工作流。"""
        result = await self.db.execute(select(Workflow).where(Workflow.id == workflow_id))
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Workflow | None:
        """按 slug 获取工作流（用于 agent_id -> workflow 映射）。"""
        result = await self.db.execute(select(Workflow).where(Workflow.slug == slug))
        return result.scalar_one_or_none()

    async def get_for_user(self, workflow_id: str, uid: str) -> Workflow | None:
        """仅返回属于当前用户的工作流（管理 API 鉴权用）。"""
        result = await self.db.execute(
            select(Workflow).where(and_(Workflow.id == workflow_id, Workflow.owner_uid == str(uid)))
        )
        return result.scalar_one_or_none()

    async def list_for_user(
        self,
        *,
        uid: str,
        is_active: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[Workflow]:
        """列出当前用户的工作流，支持按启用状态过滤。"""
        stmt = select(Workflow).where(Workflow.owner_uid == str(uid))
        if is_active is not None:
            stmt = stmt.where(Workflow.is_active.is_(is_active))
        stmt = stmt.order_by(Workflow.created_at.desc()).offset(offset).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def create(self, workflow: Workflow) -> Workflow:
        """创建工作流。"""
        self.db.add(workflow)
        await self.db.commit()
        await self.db.refresh(workflow)
        return workflow

    async def update(self, workflow: Workflow, updates: dict) -> Workflow:
        """更新工作流字段（部分更新）。"""
        for key, value in updates.items():
            if hasattr(workflow, key) and key != "id":
                setattr(workflow, key, value)
        await self.db.commit()
        await self.db.refresh(workflow)
        return workflow

    async def delete(self, workflow: Workflow) -> None:
        """删除工作流（物理删除）。"""
        await self.db.delete(workflow)
        await self.db.commit()
