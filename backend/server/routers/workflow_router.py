"""Workflow router - 工作流管理 API。

管理 API：所有走 get_required_user，且只能管理自己 uid 下的工作流（Workflow.owner_uid == current_user.uid）。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §十二
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from starring.agents.buildin.workflow.definition import WorkflowDefinition
from starring.repositories.workflow_repository import WorkflowRepository
from starring.storage.postgres.models_business import User, Workflow

workflow_router = APIRouter(prefix="/workflows", tags=["Workflow"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class WorkflowCreate(BaseModel):
    """创建工作流请求。"""

    name: str = Field(..., min_length=1, max_length=128, description="工作流名称")
    desc: str = Field("", max_length=512, description="描述")
    slug: str = Field(..., min_length=1, max_length=80, description="工作流唯一 slug")
    definition: dict = Field(default_factory=dict, description="工作流定义 JSON")
    is_active: bool = Field(True, description="是否启用")


class WorkflowUpdate(BaseModel):
    """更新工作流请求。所有字段可选，仅更新传入字段。"""

    name: str | None = Field(None, min_length=1, max_length=128)
    desc: str | None = Field(None, max_length=512)
    definition: dict | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _validate_definition(definition_dict: dict) -> WorkflowDefinition:
    """校验工作流定义合法性（fail-fast，不执行）。"""
    try:
        return WorkflowDefinition.model_validate(definition_dict)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"工作流定义非法: {exc}",
        ) from exc


def _build_validation_response(definition_dict: dict) -> dict:
    """校验工作流定义并构造校验结果响应（不抛异常，返回 valid/error 字段）。

    用于 ``POST /workflows/validate`` 与 ``POST /workflows/{id}/validate``
    两个端点共享校验与响应构造逻辑。

    校验通过后附加 warnings（不阻断保存）：
    - 从 start 不可达的孤立节点
    - condition 节点缺少 default 分支
    """
    try:
        definition = WorkflowDefinition.model_validate(definition_dict)
        warnings = _compute_definition_warnings(definition)
        return {
            "valid": True,
            "node_count": len(definition.nodes),
            "edge_count": len(definition.edges),
            "start_node_id": definition.get_start_node_id(),
            "end_node_id": definition.get_end_node_id(),
            "version": definition.version,
            "warnings": warnings,
        }
    except Exception as exc:
        return {"valid": False, "error": str(exc)}


def _compute_definition_warnings(definition: WorkflowDefinition) -> list[dict]:
    """对已通过 _validate_graph 的工作流定义做 warning 级检查。

    返回 warning dict 列表（各 warning 含 type 与 message 字段），
    不阻断保存，仅供前端编辑器实时提示。
    """
    warnings: list[dict] = []
    node_ids = {n.id for n in definition.nodes}

    # 1. 从 start 不可达的孤立节点（BFS）
    start_id = definition.get_start_node_id()
    # 构建邻接表
    adj: dict[str, list[str]] = {n.id: [] for n in definition.nodes}
    for edge in definition.edges:
        adj.setdefault(edge.source, []).append(edge.target)
    # condition 节点：把 config.cases[i].then 与 default 也纳入邻接表
    for node in definition.nodes:
        if node.node_type == "condition":
            for case in node.config.get("cases") or []:
                then_branch = case.get("then")
                if isinstance(then_branch, str) and then_branch in node_ids:
                    adj.setdefault(node.id, []).append(then_branch)
            default_branch = node.config.get("default")
            if isinstance(default_branch, str) and default_branch in node_ids:
                adj.setdefault(node.id, []).append(default_branch)

    reachable = set()
    if start_id in adj:
        stack = [start_id]
        while stack:
            nid = stack.pop()
            if nid in reachable:
                continue
            reachable.add(nid)
            stack.extend(adj.get(nid, []))

    orphan_ids = node_ids - reachable
    for nid in orphan_ids:
        node = definition.get_node(nid)
        warnings.append({
            "type": "orphan_node",
            "node_id": nid,
            "message": f"节点 {nid}（{node.name or node.node_type}）从 start 不可达，执行时会被跳过",
        })

    # 2. condition 节点缺少 default 分支
    for node in definition.nodes:
        if node.node_type == "condition" and not node.config.get("default"):
            warnings.append({
                "type": "missing_default",
                "node_id": node.id,
                "message": f"condition 节点 {node.id} 缺少 default 分支，所有 case 都不命中时工作流可能卡住",
            })

    return warnings


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@workflow_router.post("", response_model=dict)
async def create_workflow(
    payload: WorkflowCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_required_user),
):
    """创建工作流。"""
    # 校验定义合法性
    if payload.definition:
        _validate_definition(payload.definition)

    # 检查 slug 唯一性
    repo = WorkflowRepository(db)
    existing = await repo.get_by_slug(payload.slug)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"slug={payload.slug!r} 已被占用")

    workflow = Workflow(
        id=str(uuid.uuid4()),
        name=payload.name,
        desc=payload.desc,
        slug=payload.slug,
        owner_uid=str(user.uid),
        definition=payload.definition or {"nodes": [], "edges": [], "version": 1},
        version=1,
        is_active=payload.is_active,
    )
    workflow = await repo.create(workflow)
    return workflow.to_dict()


@workflow_router.get("", response_model=dict)
async def list_workflows(
    is_active: bool | None = Query(None, description="按启用状态过滤"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_required_user),
):
    """列出当前用户的工作流。"""
    repo = WorkflowRepository(db)
    workflows = await repo.list_for_user(
        uid=str(user.uid), is_active=is_active, offset=offset, limit=limit
    )
    return {"workflows": [w.to_dict() for w in workflows]}


@workflow_router.post("/validate", response_model=dict)
async def validate_definition(
    payload: dict,
    user: User = Depends(get_required_user),
):
    """校验工作流定义合法性（不执行，不需要先保存）。

    用于前端编辑器实时校验：POST body 为工作流定义 JSON。
    """
    return _build_validation_response(payload)


@workflow_router.get("/resource-options", response_model=dict)
async def get_workflow_resource_options(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_required_user),
):
    """获取工作流编辑器的工具/MCP/知识库选项（普通用户可用）。

    工具/MCP 管理接口为 admin-only，tool 节点与 llm 节点挂工具面向普通用户，
    这里复用智能体配置页同源的 resolve_agent_resource_options（buildin 工具 +
    已启用 MCP 服务器 + 用户可见知识库，option 字段：key/name/description）。
    """
    from starring.agents.context import resolve_agent_resource_options

    options = await resolve_agent_resource_options({"tools", "mcps", "knowledges"}, db=db, user=user)
    return {
        "tools": options.get("tools", []),
        "mcps": options.get("mcps", []),
        "knowledges": options.get("knowledges", []),
    }


@workflow_router.get("/{workflow_id}", response_model=dict)
async def get_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_required_user),
):
    """获取工作流详情。"""
    repo = WorkflowRepository(db)
    workflow = await repo.get_for_user(workflow_id, str(user.uid))
    if workflow is None:
        raise HTTPException(status_code=404, detail="工作流不存在或无权访问")
    return workflow.to_dict()


@workflow_router.put("/{workflow_id}", response_model=dict)
async def update_workflow(
    workflow_id: str,
    payload: WorkflowUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_required_user),
):
    """更新工作流定义。"""
    repo = WorkflowRepository(db)
    workflow = await repo.get_for_user(workflow_id, str(user.uid))
    if workflow is None:
        raise HTTPException(status_code=404, detail="工作流不存在或无权访问")

    updates: dict[str, Any] = {}
    if payload.name is not None:
        updates["name"] = payload.name
    if payload.desc is not None:
        updates["desc"] = payload.desc
    if payload.is_active is not None:
        updates["is_active"] = payload.is_active
    if payload.definition is not None:
        # 校验新定义合法性
        _validate_definition(payload.definition)
        updates["definition"] = payload.definition
        # 定义变更时版本号自增
        updates["version"] = workflow.version + 1

    workflow = await repo.update(workflow, updates)
    return workflow.to_dict()


@workflow_router.delete("/{workflow_id}", response_model=dict)
async def delete_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_required_user),
):
    """删除工作流（物理删除，运行历史保留在 agent_runs 表）。"""
    repo = WorkflowRepository(db)
    workflow = await repo.get_for_user(workflow_id, str(user.uid))
    if workflow is None:
        raise HTTPException(status_code=404, detail="工作流不存在或无权访问")

    result = {"id": workflow.id, "deleted": True}
    await repo.delete(workflow)
    return result


@workflow_router.post("/{workflow_id}/validate", response_model=dict)
async def validate_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_required_user),
):
    """校验工作流定义合法性（不执行）。"""
    repo = WorkflowRepository(db)
    workflow = await repo.get_for_user(workflow_id, str(user.uid))
    if workflow is None:
        raise HTTPException(status_code=404, detail="工作流不存在或无权访问")

    return _build_validation_response(workflow.definition)
