"""kb_sync 触发器执行服务单测。

覆盖：execute_kb_sync 入口校验与状态流转、_sync_kb_url_files 的
hash 对比跳过/更新/单文件失败分支、cron_scan 按类型分流入队、
repository 扫描过滤包含 kb_sync。
不依赖真实 DB/MinIO/网络：monkeypatch 延迟导入的知识库与存储依赖。
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import starring.services.trigger.kb_sync as kb_sync_module
from starring.services.trigger.kb_sync import execute_kb_sync, _sync_kb_url_files


def _make_trigger(
    *,
    trigger_id: str = "tr-kb-1",
    trigger_type: str = "kb_sync",
    is_active: bool = True,
    uid: str = "user-1",
    config: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=trigger_id,
        trigger_type=trigger_type,
        is_active=is_active,
        uid=uid,
        config=config if config is not None else {"cron_expr": "0 8 * * *", "kb_id": "kb-1"},
        last_run_at=None,
        last_run_status=None,
        last_run_id=None,
        run_count=0,
    )


def _patch_pg_and_repo(monkeypatch: pytest.MonkeyPatch, trigger) -> AsyncMock:
    """替换 pg_manager 会话与 TriggerRepository，返回 repo mock。"""

    @asynccontextmanager
    async def fake_session_ctx():
        yield object()

    monkeypatch.setattr(kb_sync_module.pg_manager, "get_async_session_context", fake_session_ctx)

    repo = AsyncMock()
    repo.get = AsyncMock(return_value=trigger)
    repo.update_fields = AsyncMock()
    monkeypatch.setattr(kb_sync_module, "TriggerRepository", lambda db: repo)
    return repo


class _FakeKb:
    """内存版 KB 实例：只提供 kb_sync 用到的元数据与协程方法。"""

    def __init__(self, files_meta: dict):
        self.files_meta = files_meta
        self._load_metadata = AsyncMock()
        self._persist_file = AsyncMock()
        self.parse_file = AsyncMock()
        self.index_file = AsyncMock()


def _patch_kb_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_kb: _FakeKb,
    fetch_mock: AsyncMock,
) -> AsyncMock:
    """替换 _sync_kb_url_files 内延迟导入的知识库/minio 依赖，返回 aupload mock。"""
    import starring.knowledge as knowledge_pkg
    import starring.knowledge.utils.url_fetcher as url_fetcher_module
    import starring.storage.minio as minio_pkg

    monkeypatch.setattr(knowledge_pkg.knowledge_base, "_get_kb_for_database", AsyncMock(return_value=fake_kb))
    monkeypatch.setattr(url_fetcher_module, "fetch_url_content", fetch_mock)

    aupload = AsyncMock(return_value=SimpleNamespace(url="http://minio/kb-documents/kb-1/upload/new.html"))
    monkeypatch.setattr(minio_pkg, "get_minio_client", lambda: SimpleNamespace(aupload_file=aupload))
    return aupload


# ---------------------------------------------------------------------------
# execute_kb_sync：入口校验与状态流转
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_kb_sync_skip_when_not_found(monkeypatch):
    """触发器不存在时返回 skipped。"""
    _patch_pg_and_repo(monkeypatch, trigger=None)
    result = await execute_kb_sync(trigger_id="missing")
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_execute_kb_sync_skip_when_wrong_type_or_inactive(monkeypatch):
    """非 kb_sync 类型或未启用的触发器返回 skipped。"""
    _patch_pg_and_repo(monkeypatch, trigger=_make_trigger(trigger_type="cron"))
    assert (await execute_kb_sync(trigger_id="tr-kb-1"))["status"] == "skipped"

    _patch_pg_and_repo(monkeypatch, trigger=_make_trigger(is_active=False))
    assert (await execute_kb_sync(trigger_id="tr-kb-1"))["status"] == "skipped"


@pytest.mark.asyncio
async def test_execute_kb_sync_failed_when_kb_id_missing(monkeypatch):
    """config.kb_id 缺失时标记 failed。"""
    repo = _patch_pg_and_repo(monkeypatch, trigger=_make_trigger(config={"cron_expr": "0 8 * * *"}))
    result = await execute_kb_sync(trigger_id="tr-kb-1")
    assert result["status"] == "failed"
    assert "kb_id" in result["error"]
    repo.update_fields.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_kb_sync_completed_and_increments_run_count(monkeypatch):
    """同步成功：标记 running → completed，run_count+1，返回摘要。"""
    trigger = _make_trigger()
    repo = _patch_pg_and_repo(monkeypatch, trigger=trigger)
    sync_mock = AsyncMock(return_value={"checked": 2, "updated": 1, "skipped": 1, "failed": 0})
    monkeypatch.setattr(kb_sync_module, "_sync_kb_url_files", sync_mock)

    result = await execute_kb_sync(trigger_id="tr-kb-1", scheduled_time_iso="2026-01-01T08:00:00")

    assert result["status"] == "completed"
    assert result["updated"] == 1
    sync_mock.assert_awaited_once_with("kb-1", "user-1")
    # 第一次 update_fields 标记 running（last_run_id 置空），第二次终结写 completed + run_count+1
    first_fields = repo.update_fields.await_args_list[0].kwargs["fields"]
    assert first_fields["last_run_status"] == "running"
    assert first_fields["last_run_id"] is None
    last_fields = repo.update_fields.await_args_list[-1].kwargs["fields"]
    assert last_fields["last_run_status"] == "completed"
    assert last_fields["run_count"] == 1


@pytest.mark.asyncio
async def test_execute_kb_sync_failed_when_sync_raises(monkeypatch):
    """同步过程抛异常时标记 failed，不向上抛。"""
    repo = _patch_pg_and_repo(monkeypatch, trigger=_make_trigger())
    monkeypatch.setattr(kb_sync_module, "_sync_kb_url_files", AsyncMock(side_effect=RuntimeError("kb boom")))

    result = await execute_kb_sync(trigger_id="tr-kb-1")
    assert result["status"] == "failed"
    assert "kb boom" in result["error"]
    assert repo.update_fields.await_args_list[-1].kwargs["fields"]["last_run_status"] == "failed"


# ---------------------------------------------------------------------------
# _sync_kb_url_files：hash 对比与重建分支
# ---------------------------------------------------------------------------


def _url_file_meta(kb_id: str, source: str, content: bytes) -> dict:
    return {
        "kb_id": kb_id,
        "status": "indexed",
        "content_hash": hashlib.sha256(content).hexdigest(),
        "processing_params": {"original_source": source},
    }


@pytest.mark.asyncio
async def test_sync_skips_when_hash_unchanged(monkeypatch):
    """内容 hash 未变化时跳过，不触发重传/解析/入库。"""
    content = b"<html>same</html>"
    fake_kb = _FakeKb({"file-1": _url_file_meta("kb-1", "https://example.com/a", content)})
    aupload = _patch_kb_deps(
        monkeypatch,
        fake_kb=fake_kb,
        fetch_mock=AsyncMock(return_value=(content, "https://example.com/a")),
    )

    summary = await _sync_kb_url_files("kb-1", "user-1")

    assert summary == {"checked": 1, "updated": 0, "skipped": 1, "failed": 0}
    aupload.assert_not_awaited()
    fake_kb.parse_file.assert_not_awaited()
    fake_kb._load_metadata.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_rebuilds_when_hash_changed(monkeypatch):
    """内容变化时重传 MinIO、重置元数据并走 parse → index 重建。"""
    old_content, new_content = b"<html>old</html>", b"<html>new</html>"
    meta = _url_file_meta("kb-1", "https://example.com/a", old_content)
    meta["markdown_file"] = "http://minio/kb-parsed/old.md"
    fake_kb = _FakeKb({"file-1": meta})
    aupload = _patch_kb_deps(
        monkeypatch,
        fake_kb=fake_kb,
        fetch_mock=AsyncMock(return_value=(new_content, "https://example.com/a")),
    )

    summary = await _sync_kb_url_files("kb-1", "user-1")

    assert summary == {"checked": 1, "updated": 1, "skipped": 0, "failed": 0}
    new_hash = hashlib.sha256(new_content).hexdigest()
    assert meta["content_hash"] == new_hash
    assert meta["status"] == "uploaded"
    assert "markdown_file" not in meta
    assert aupload.await_args.kwargs["object_name"] == f"kb-1/upload/{new_hash}.html"
    fake_kb._persist_file.assert_awaited_once_with("file-1")
    fake_kb.parse_file.assert_awaited_once_with("kb-1", "file-1", "user-1")
    fake_kb.index_file.assert_awaited_once_with("kb-1", "file-1", "user-1")


@pytest.mark.asyncio
async def test_sync_single_file_failure_continues(monkeypatch):
    """单文件抓取失败计入 failed，不影响其余文件继续同步。"""
    content = b"<html>ok</html>"
    fake_kb = _FakeKb(
        {
            "file-bad": _url_file_meta("kb-1", "https://example.com/bad", b"x"),
            "file-ok": _url_file_meta("kb-1", "https://example.com/ok", content),
        }
    )

    async def fetch(url: str):
        if "bad" in url:
            raise ValueError("fetch failed")
        return content, url

    _patch_kb_deps(monkeypatch, fake_kb=fake_kb, fetch_mock=AsyncMock(side_effect=fetch))

    summary = await _sync_kb_url_files("kb-1", "user-1")
    assert summary == {"checked": 2, "updated": 0, "skipped": 1, "failed": 1}


@pytest.mark.asyncio
async def test_sync_ignores_non_url_and_foreign_files(monkeypatch):
    """本地上传文件（无 original_source）、其他 KB 的文件与文件夹不参与同步。"""
    fake_kb = _FakeKb(
        {
            "file-local": {"kb_id": "kb-1", "processing_params": {}},
            "file-other-kb": _url_file_meta("kb-2", "https://example.com/a", b"x"),
            "folder-1": {"kb_id": "kb-1", "is_folder": True, "processing_params": {"original_source": "https://e.com"}},
        }
    )
    fetch_mock = AsyncMock()
    _patch_kb_deps(monkeypatch, fake_kb=fake_kb, fetch_mock=fetch_mock)

    summary = await _sync_kb_url_files("kb-1", "user-1")
    assert summary == {"checked": 0, "updated": 0, "skipped": 0, "failed": 0}
    fetch_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# 扫描与入队分流
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_active_cron_triggers_includes_kb_sync():
    """扫描过滤条件应同时包含 cron 与 kb_sync 类型。"""
    from starring.repositories.trigger_repository import TriggerRepository

    captured = {}

    class FakeDB:
        async def execute(self, stmt):
            captured["stmt"] = stmt
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    await TriggerRepository(FakeDB()).list_active_cron_triggers()
    compiled = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True}))
    assert "'cron'" in compiled
    assert "'kb_sync'" in compiled


@pytest.mark.asyncio
async def test_enqueue_dispatches_by_trigger_type(monkeypatch):
    """kb_sync 触发器入队 execute_kb_sync，其余入队 execute_trigger_run。"""
    import starring.services.trigger.cron_scan as cron_scan

    queue = AsyncMock()
    monkeypatch.setattr(cron_scan, "get_arq_pool", AsyncMock(return_value=queue))

    await cron_scan._enqueue_trigger_run(_make_trigger(trigger_type="kb_sync"), "2026-01-01T08:00:00")
    assert queue.enqueue_job.await_args.args[0] == "execute_kb_sync"

    await cron_scan._enqueue_trigger_run(_make_trigger(trigger_type="cron"), "2026-01-01T08:00:00")
    assert queue.enqueue_job.await_args.args[0] == "execute_trigger_run"
    # 幂等 _job_id 格式不区分类型
    assert queue.enqueue_job.await_args.kwargs["_job_id"] == "trigger:tr-kb-1:2026-01-01T08:00:00"
