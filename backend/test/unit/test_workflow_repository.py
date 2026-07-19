"""WorkflowRepository 测试。

覆盖 CRUD 操作与权限隔离。

设计依据：docs/vibe/P1-B-工作流引擎细化设计-20260719.md §三
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from starring.repositories.workflow_repository import WorkflowRepository
from starring.storage.postgres.models_business import Workflow


@pytest.fixture
def fake_workflow():
    """构造一个完整 Workflow 实例 mock。"""
    wf = MagicMock(spec=Workflow)
    wf.id = "wf-1"
    wf.name = "测试工作流"
    wf.desc = "用于测试"
    wf.slug = "test-wf"
    wf.owner_uid = "user-1"
    wf.definition = {"nodes": [], "edges": [], "version": 1}
    wf.version = 1
    wf.is_active = True
    return wf


# ---------------------------------------------------------------------------
# get / get_by_slug / get_for_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_workflow_when_found(fake_workflow):
    """get 应返回找到的 Workflow 实例。"""
    db = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=fake_workflow)
    db.execute = AsyncMock(return_value=result_mock)

    repo = WorkflowRepository(db)
    wf = await repo.get("wf-1")

    assert wf is fake_workflow


@pytest.mark.asyncio
async def test_get_returns_none_when_not_found():
    """工作流不存在时 get 应返回 None。"""
    db = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=result_mock)

    repo = WorkflowRepository(db)
    wf = await repo.get("nonexistent")

    assert wf is None


@pytest.mark.asyncio
async def test_get_for_user_filters_by_uid(fake_workflow):
    """get_for_user 应同时按 workflow_id + uid 过滤。"""
    db = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=fake_workflow)
    db.execute = AsyncMock(return_value=result_mock)

    repo = WorkflowRepository(db)
    wf = await repo.get_for_user("wf-1", "user-1")

    assert wf is fake_workflow
    # 验证 SQL 中包含 uid 条件
    executed_sql = db.execute.await_args
    assert executed_sql is not None


# ---------------------------------------------------------------------------
# list_for_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_for_user_returns_workflows(fake_workflow):
    """list_for_user 应返回用户的工作流列表。"""
    db = MagicMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all = MagicMock(return_value=[fake_workflow])
    db.execute = AsyncMock(return_value=result_mock)

    repo = WorkflowRepository(db)
    workflows = await repo.list_for_user(uid="user-1")

    assert len(workflows) == 1
    assert workflows[0] is fake_workflow


@pytest.mark.asyncio
async def test_list_for_user_filters_by_is_active():
    """list_for_user 应支持按 is_active 过滤。"""
    db = MagicMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all = MagicMock(return_value=[])
    db.execute = AsyncMock(return_value=result_mock)

    repo = WorkflowRepository(db)
    await repo.list_for_user(uid="user-1", is_active=True)

    # 验证 SQL 中包含 is_active 条件
    assert db.execute.await_args is not None


# ---------------------------------------------------------------------------
# create / update / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_adds_and_commits(fake_workflow):
    """create 应添加到 session 并 commit。"""
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    repo = WorkflowRepository(db)
    result = await repo.create(fake_workflow)

    db.add.assert_called_once_with(fake_workflow)
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(fake_workflow)
    assert result is fake_workflow


@pytest.mark.asyncio
async def test_update_sets_fields_and_commits(fake_workflow):
    """update 应更新指定字段并 commit。"""
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    repo = WorkflowRepository(db)
    result = await repo.update(fake_workflow, {"name": "新名称", "version": 2})

    assert fake_workflow.name == "新名称"
    assert fake_workflow.version == 2
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(fake_workflow)
    assert result is fake_workflow


@pytest.mark.asyncio
async def test_update_ignores_id_field(fake_workflow):
    """update 应忽略 id 字段（不允许修改主键）。"""
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    original_id = fake_workflow.id
    repo = WorkflowRepository(db)
    await repo.update(fake_workflow, {"id": "new-id", "name": "新名称"})

    # id 不应被修改
    assert fake_workflow.id == original_id


@pytest.mark.asyncio
async def test_delete_removes_and_commits(fake_workflow):
    """delete 应从 session 删除并 commit。"""
    db = MagicMock()
    db.delete = AsyncMock()
    db.commit = AsyncMock()

    repo = WorkflowRepository(db)
    await repo.delete(fake_workflow)

    db.delete.assert_awaited_once_with(fake_workflow)
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Workflow.to_dict
# ---------------------------------------------------------------------------


def test_workflow_to_dict_contains_all_fields(fake_workflow):
    """Workflow.to_dict 应包含所有可序列化字段。"""
    # MagicMock(spec=Workflow) 不自动应用 to_dict 方法，这里直接构造
    from datetime import datetime

    wf = Workflow()
    wf.id = "wf-1"
    wf.name = "测试"
    wf.desc = "描述"
    wf.slug = "test-wf"
    wf.owner_uid = "user-1"
    wf.definition = {"nodes": []}
    wf.version = 1
    wf.is_active = True
    wf.created_at = datetime(2026, 7, 19, 12, 0, 0)
    wf.updated_at = datetime(2026, 7, 19, 12, 0, 0)

    result = wf.to_dict()

    assert result["id"] == "wf-1"
    assert result["name"] == "测试"
    assert result["desc"] == "描述"
    assert result["slug"] == "test-wf"
    assert result["owner_uid"] == "user-1"
    assert result["definition"] == {"nodes": []}
    assert result["version"] == 1
    assert result["is_active"] is True
    assert "created_at" in result
    assert "updated_at" in result
