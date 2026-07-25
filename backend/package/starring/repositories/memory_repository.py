"""Memory repository - 用户长期记忆 CRUD（PG 真源，向量删除同步由 service 层负责）。"""

from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from starring.storage.postgres.models_business import UserMemory


class MemoryRepository:
    """用户记忆数据访问层，封装对 user_memories 表的全部数据库操作。"""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def get_for_user(self, memory_id: str, uid: str) -> UserMemory | None:
        """仅返回属于当前用户的记忆（管理 API 鉴权用）。"""
        result = await self.db.execute(
            select(UserMemory).where(and_(UserMemory.id == memory_id, UserMemory.uid == str(uid)))
        )
        return result.scalar_one_or_none()

    async def list_by_uid(self, uid: str, *, limit: int = 500) -> list[UserMemory]:
        """列出当前用户的全部记忆，按创建时间倒序。"""
        result = await self.db.execute(
            select(UserMemory).where(UserMemory.uid == str(uid)).order_by(UserMemory.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def get_by_ids(self, uid: str, memory_ids: list[str]) -> list[UserMemory]:
        """按 id 列表回表取记忆（Milvus 召回后回 PG 取内容），带 uid 归属校验。"""
        if not memory_ids:
            return []
        result = await self.db.execute(
            select(UserMemory).where(and_(UserMemory.uid == str(uid), UserMemory.id.in_(memory_ids)))
        )
        return list(result.scalars().all())

    async def count_by_uid(self, uid: str) -> int:
        result = await self.db.execute(select(func.count()).select_from(UserMemory).where(UserMemory.uid == str(uid)))
        return int(result.scalar() or 0)

    async def create(self, memory: UserMemory) -> UserMemory:
        self.db.add(memory)
        await self.db.commit()
        await self.db.refresh(memory)
        return memory

    async def delete(self, memory: UserMemory) -> None:
        await self.db.delete(memory)
        await self.db.commit()

    async def delete_all_for_user(self, uid: str) -> list[str]:
        """清空当前用户全部记忆，返回被删除的 id 列表（供 Milvus 同步删除）。"""
        memories = await self.list_by_uid(uid, limit=10000)
        deleted_ids = [m.id for m in memories]
        for memory in memories:
            await self.db.delete(memory)
        await self.db.commit()
        return deleted_ids
