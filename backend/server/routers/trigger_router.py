"""Trigger router - 触发器管理 API + webhook invoke 入口。

管理 API：所有走 get_required_user，且只能管理自己 uid 下的触发器（Trigger.uid == current_user.uid）。
invoke API：独立走 HMAC 鉴权，不走 get_required_user。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from starring.repositories.trigger_repository import TriggerRepository
from starring.services.trigger.service import execute_webhook_trigger
from starring.services.trigger.webhook import generate_secret
from starring.storage.postgres.models_business import AgentRun, Trigger, User

trigger_router = APIRouter(prefix="/triggers", tags=["Trigger"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class TriggerCreate(BaseModel):
    """创建触发器请求。webhook 类型自动生成 secret，调用方无需传。"""

    name: str = Field(..., min_length=1, max_length=128, description="触发器名称")
    desc: str = Field("", max_length=512, description="描述")
    trigger_type: str = Field(..., description="触发器类型: cron / webhook")
    agent_id: str = Field(..., description="关联的 Agent slug")
    config: dict = Field(default_factory=dict, description="触发器配置")
    input_query: str | None = Field(None, description="触发器执行时的输入 query")
    is_active: bool = Field(True, description="是否启用")


class TriggerUpdate(BaseModel):
    """更新触发器请求。所有字段可选，仅更新传入字段。"""

    name: str | None = Field(None, min_length=1, max_length=128)
    desc: str | None = Field(None, max_length=512)
    config: dict | None = None
    input_query: str | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _validate_trigger_type(trigger_type: str) -> str:
    if trigger_type not in ("cron", "webhook"):
        raise HTTPException(status_code=422, detail="trigger_type 必须为 cron 或 webhook")
    return trigger_type


def _validate_cron_config(config: dict) -> None:
    """cron 触发器必须有 cron_expr，且时区合法。校验逻辑与 cron_scan._is_due 保持一致。"""
    if not config.get("cron_expr"):
        raise HTTPException(status_code=422, detail="cron 触发器必须配置 config.cron_expr")
    timezone_name = config.get("timezone") or "Asia/Shanghai"
    try:
        import pytz
        from croniter import croniter

        pytz.timezone(timezone_name)  # 抛 UnknownTimeZoneError 时由外层捕获
        if not croniter.is_valid(config["cron_expr"]):
            raise ValueError(f"非法 cron 表达式: {config['cron_expr']!r}")
    except ImportError:
        # croniter/pytz 未安装时跳过校验（已在 pyproject 声明依赖）
        pass
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"cron 配置非法: {e}") from e


# ---------------------------------------------------------------------------
# 管理 API
# ---------------------------------------------------------------------------


@trigger_router.post("")
async def create_trigger(
    payload: TriggerCreate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """创建触发器。webhook 类型自动生成 32 字节 hex secret。"""
    _validate_trigger_type(payload.trigger_type)
    config = dict(payload.config or {})
    if payload.trigger_type == "cron":
        _validate_cron_config(config)
        config.setdefault("timezone", "Asia/Shanghai")
    elif payload.trigger_type == "webhook":
        # 自动生成 secret（调用方无需传）
        config["secret"] = config.get("secret") or generate_secret()

    trigger = Trigger(
        id=str(uuid.uuid4()),
        name=payload.name,
        desc=payload.desc,
        trigger_type=payload.trigger_type,
        agent_id=payload.agent_id,
        uid=str(current_user.uid),
        config=config,
        input_query=payload.input_query,
        is_active=payload.is_active,
    )
    await TriggerRepository(db).create(trigger)
    # 创建后首次返回完整 secret（include_secret=True）
    return {"trigger": trigger.to_dict(include_secret=True)}


@trigger_router.get("")
async def list_triggers(
    trigger_type: str | None = Query(None, description="按类型过滤: cron / webhook"),
    agent_id: str | None = Query(None, description="按 Agent slug 过滤"),
    is_active: bool | None = Query(None, description="按启用状态过滤"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户的触发器（仅本人，分页 + 过滤）。"""
    repo = TriggerRepository(db)
    items = await repo.list_for_user(
        uid=str(current_user.uid),
        trigger_type=trigger_type,
        agent_id=agent_id,
        is_active=is_active,
        offset=offset,
        limit=limit,
    )
    return {"triggers": [t.to_dict() for t in items]}


@trigger_router.get("/{trigger_id}")
async def get_trigger(
    trigger_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """触发器详情。仅创建者可见完整 secret。"""
    trigger = await TriggerRepository(db).get_for_user(trigger_id, str(current_user.uid))
    if not trigger:
        raise HTTPException(status_code=404, detail="触发器不存在或无权访问")
    return {"trigger": trigger.to_dict(include_secret=True)}


@trigger_router.patch("/{trigger_id}")
async def update_trigger(
    trigger_id: str,
    payload: TriggerUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """更新触发器字段。仅创建者可改。"""
    repo = TriggerRepository(db)
    trigger = await repo.get_for_user(trigger_id, str(current_user.uid))
    if not trigger:
        raise HTTPException(status_code=404, detail="触发器不存在或无权访问")

    fields: dict[str, Any] = {}
    set_fields = payload.model_fields_set
    if "name" in set_fields and payload.name is not None:
        fields["name"] = payload.name
    if "desc" in set_fields and payload.desc is not None:
        fields["desc"] = payload.desc
    if "input_query" in set_fields:
        fields["input_query"] = payload.input_query
    if "is_active" in set_fields and payload.is_active is not None:
        fields["is_active"] = payload.is_active
    if "config" in set_fields and payload.config is not None:
        config = dict(payload.config)
        # cron 改 config 时校验 cron_expr
        if trigger.trigger_type == "cron":
            _validate_cron_config(config)
        # webhook 改 config 时保留旧 secret（除非显式传入新 secret）
        if trigger.trigger_type == "webhook" and "secret" not in config:
            old_secret = (trigger.config or {}).get("secret", "")
            if old_secret:
                config["secret"] = old_secret
        fields["config"] = config

    if not fields:
        return {"trigger": trigger.to_dict(include_secret=True)}
    updated = await repo.update_fields(trigger, fields=fields)
    return {"trigger": updated.to_dict(include_secret=True)}


@trigger_router.delete("/{trigger_id}")
async def delete_trigger(
    trigger_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """删除触发器。仅创建者可删。"""
    repo = TriggerRepository(db)
    trigger = await repo.get_for_user(trigger_id, str(current_user.uid))
    if not trigger:
        raise HTTPException(status_code=404, detail="触发器不存在或无权访问")
    await repo.delete(trigger)
    return {"success": True}


@trigger_router.post("/{trigger_id}/rotate-secret")
async def rotate_secret(
    trigger_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """重新生成 webhook 触发器的 secret。仅 webhook 类型可轮换。"""
    repo = TriggerRepository(db)
    trigger = await repo.get_for_user(trigger_id, str(current_user.uid))
    if not trigger:
        raise HTTPException(status_code=404, detail="触发器不存在或无权访问")
    if trigger.trigger_type != "webhook":
        raise HTTPException(status_code=422, detail="仅 webhook 触发器可轮换 secret")

    new_secret = generate_secret()
    config = dict(trigger.config or {})
    config["secret"] = new_secret
    updated = await repo.update_fields(trigger, fields={"config": config})
    # 轮换后首次返回完整新 secret
    return {"trigger": updated.to_dict(include_secret=True)}


@trigger_router.get("/{trigger_id}/runs")
async def list_trigger_runs(
    trigger_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """查看触发器执行历史。查 AgentRun.input_payload->>'trigger_id' = trigger_id。

    鉴权：先校验 trigger 归属当前用户，再查 runs（runs 的 uid 已与 trigger.uid 一致）。
    """
    repo = TriggerRepository(db)
    trigger = await repo.get_for_user(trigger_id, str(current_user.uid))
    if not trigger:
        raise HTTPException(status_code=404, detail="触发器不存在或无权访问")

    # JSONB 字段查询：input_payload ->> 'trigger_id' == trigger_id
    stmt = (
        select(AgentRun)
        .where(AgentRun.input_payload["trigger_id"].as_string() == trigger_id)
        .order_by(AgentRun.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    runs = result.scalars().all()
    return {"runs": [run.to_dict() for run in runs]}


# ---------------------------------------------------------------------------
# webhook invoke 入口（HMAC 鉴权，无 get_required_user）
# ---------------------------------------------------------------------------


@trigger_router.post("/{trigger_id}/invoke")
async def invoke_trigger(trigger_id: str, request: Request):
    """webhook 触发器调用入口。

    鉴权：HMAC-SHA256 签名（X-Trigger-Signature）+ 时间戳（X-Trigger-Timestamp，5 分钟内有效）。
    不走 get_required_user，公开可访问。
    """
    body = await request.body()
    signature = request.headers.get("X-Trigger-Signature", "")
    timestamp = request.headers.get("X-Trigger-Timestamp", "")
    return await execute_webhook_trigger(
        trigger_id=trigger_id,
        body=body,
        signature=signature,
        timestamp=timestamp,
    )
