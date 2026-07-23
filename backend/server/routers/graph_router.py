from fastapi import APIRouter, Depends, HTTPException, Query

from server.utils.auth_middleware import get_admin_user, get_required_user
from starring import knowledge_base
from starring.knowledge.graphs.milvus_graph_service import MilvusGraphService
from starring.storage.postgres.models_business import User
from starring.utils.logging_config import logger

graph = APIRouter(prefix="/graph", tags=["graph"])


async def _ensure_kb_visible(current_user: User, kb_id: str) -> None:
    """校验当前用户是否有权访问该知识库（复用工作区/知识库的可访问性判定）。"""
    allowed = await knowledge_base.check_accessible(
        {
            "uid": current_user.uid,
            "role": current_user.role,
            "department_id": current_user.department_id,
        },
        kb_id,
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="Access denied")


async def _get_graph_service(kb_id: str) -> MilvusGraphService:
    db_info = await knowledge_base.get_database_info(kb_id)
    if not db_info:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    kb_type = (db_info.get("kb_type") or "").lower()
    if kb_type != "milvus":
        raise HTTPException(status_code=404, detail="Graph API only supports Milvus knowledge bases")

    return MilvusGraphService(kb_id=kb_id)


@graph.get("/list")
async def get_graphs(current_user: User = Depends(get_admin_user)):
    """获取支持图谱能力的 Milvus 知识库列表"""
    try:
        databases = (await knowledge_base.get_databases_by_uid(current_user.uid)).get("databases", [])
        graphs = []
        for db in databases:
            if (db.get("kb_type") or "").lower() != "milvus":
                continue
            graphs.append(
                {
                    "id": db.get("kb_id"),
                    "name": db.get("name"),
                    "type": "milvus",
                    "description": db.get("description"),
                    "status": db.get("status", "active"),
                    "created_at": db.get("created_at"),
                    "metadata": db,
                }
            )
        return {"success": True, "data": graphs}
    except Exception as e:
        logger.exception(f"Failed to list graphs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list graphs: {str(e)}")


@graph.get("/subgraph")
async def get_subgraph(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    node_label: str = Query("*", description="节点标签或查询关键词"),
    max_depth: int = Query(2, description="最大深度", ge=1, le=5),
    max_nodes: int = Query(100, description="最大节点数", ge=1, le=1000),
    exclude_chunk: bool = Query(False, description="是否排除 Chunk 节点"),
    current_user: User = Depends(get_required_user),
):
    """查询 Milvus 知识库图谱子图"""
    try:
        logger.info(f"Querying subgraph - kb_id: {kb_id}, label: {node_label}")
        await _ensure_kb_visible(current_user, kb_id)
        service = await _get_graph_service(kb_id)
        result_data = await service.query_nodes(
            keyword=node_label,
            max_depth=max_depth,
            max_nodes=max_nodes,
            exclude_chunk=exclude_chunk,
        )
        return {"success": True, "data": result_data}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to get subgraph: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get subgraph: {str(e)}")


@graph.get("/labels")
async def get_graph_labels(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(get_required_user),
):
    """获取 Milvus 知识库图谱的所有标签"""
    try:
        await _ensure_kb_visible(current_user, kb_id)
        service = await _get_graph_service(kb_id)
        labels = await service.get_labels()
        return {"success": True, "data": {"labels": labels}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get labels: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get labels: {str(e)}")


@graph.get("/stats")
async def get_graph_stats(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(get_required_user),
):
    """获取 Milvus 知识库图谱统计信息"""
    try:
        await _ensure_kb_visible(current_user, kb_id)
        service = await _get_graph_service(kb_id)
        stats_data = await service.get_stats()
        return {"success": True, "data": stats_data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {str(e)}")
