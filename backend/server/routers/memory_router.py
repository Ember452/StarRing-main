"""Memory router - 用户长期记忆管理 API。

隐私边界：记忆仅本人可见/可删，管理员也不能查看他人记忆
（所有接口以 current_user.uid 为唯一数据边界）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from server.utils.auth_middleware import get_required_user
from starring.memory import service as memory_service
from starring.storage.postgres.models_business import User

memory_router = APIRouter(prefix="/memory", tags=["Memory"])


@memory_router.get("")
async def list_memories(current_user: User = Depends(get_required_user)):
    """列出当前用户全部记忆（按创建时间倒序）。"""
    memories = await memory_service.list_memories(current_user.uid)
    return {"memories": memories, "total": len(memories)}


@memory_router.delete("/{memory_id}")
async def delete_memory(memory_id: str, current_user: User = Depends(get_required_user)):
    """删除本人一条记忆（PG + Milvus 同步）。不存在或不属于本人返回 404。"""
    deleted = await memory_service.delete_memory(current_user.uid, memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"message": "success", "id": memory_id}


@memory_router.delete("")
async def clear_memories(current_user: User = Depends(get_required_user)):
    """清空本人全部记忆（PG + Milvus 同步），返回删除条数。"""
    deleted_count = await memory_service.clear_memories(current_user.uid)
    return {"message": "success", "deleted": deleted_count}
