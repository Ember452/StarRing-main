"""知识库定时同步执行服务（kb_sync 触发器）。

调用链路：
    [cron 元任务 scan_triggers] → enqueue execute_kb_sync → execute_kb_sync

同步范围：仅 URL 来源文档（processing_params.original_source 为 http(s) URL）。
逐文件重新抓取，content_hash 不变则跳过；变化则重传 MinIO 后重建（parse → index）。

关键约束：本任务运行在 ARQ worker 进程内，不可使用 API 进程内的 tasker 队列；
knowledge manager 的解析/入库方法本身是普通协程，直接 await 执行。
worker 进程内 KB 元数据可能未加载或过期，同步前强制从 PG 重新加载。
"""

from __future__ import annotations

from starring.repositories.trigger_repository import TriggerRepository
from starring.storage.postgres.manager import pg_manager
from starring.utils.datetime_utils import utc_isoformat, utc_now_naive
from starring.utils.logging_config import logger


async def execute_kb_sync(trigger_id: str, scheduled_time_iso: str | None = None) -> dict:
    """kb_sync 触发器执行入口：同步知识库中的 URL 来源文档。

    Returns:
        {"status": "completed"/"skipped"/"failed", "trigger_id": ...,
         "checked": n, "updated": n, "skipped": n, "failed": n}
    """
    async with pg_manager.get_async_session_context() as db:
        repo = TriggerRepository(db)
        trigger = await repo.get(trigger_id)
        if not trigger or trigger.trigger_type != "kb_sync" or not trigger.is_active:
            logger.info(f"kb_sync trigger {trigger_id} skipped: not found, wrong type or inactive")
            return {"status": "skipped", "reason": "trigger inactive or not found"}

        kb_id = (trigger.config or {}).get("kb_id")
        if not kb_id:
            await repo.update_fields(trigger, fields={"last_run_status": "failed"})
            return {"status": "failed", "trigger_id": trigger_id, "error": "config.kb_id missing"}

        # 标记 running（kb_sync 无 AgentRun，last_run_id 置空）
        await repo.update_fields(
            trigger,
            fields={
                "last_run_at": utc_now_naive(),
                "last_run_status": "running",
                "last_run_id": None,
            },
        )
        operator_id = trigger.uid

    try:
        summary = await _sync_kb_url_files(kb_id, operator_id)
    except Exception as e:
        logger.exception(f"kb_sync trigger {trigger_id} failed for kb {kb_id}: {e}")
        await _finish(trigger_id, "failed")
        return {"status": "failed", "trigger_id": trigger_id, "error": str(e)}

    status = "completed" if summary["failed"] == 0 else "failed"
    await _finish(trigger_id, status)
    logger.info(f"kb_sync trigger {trigger_id} for kb {kb_id} at {scheduled_time_iso}: {summary}")
    return {"status": status, "trigger_id": trigger_id, **summary}


async def _sync_kb_url_files(kb_id: str, operator_id: str | None) -> dict:
    """同步单个知识库的 URL 来源文档，返回 {checked, updated, skipped, failed} 摘要。"""
    # 延迟导入知识库包，避免触发器模块加载时引入重依赖/循环导入
    from starring.knowledge import knowledge_base
    from starring.knowledge.base import FileStatus
    from starring.knowledge.utils.kb_utils import calculate_content_hash
    from starring.knowledge.utils.url_fetcher import fetch_url_content
    from starring.storage.minio import MinIOClient, get_minio_client

    kb_instance = await knowledge_base._get_kb_for_database(kb_id)
    # worker 进程内元数据可能为空或过期，强制从 PG 重新加载
    await kb_instance._load_metadata()

    url_files: list[tuple[str, str]] = []
    for file_id, meta in kb_instance.files_meta.items():
        if meta.get("kb_id") != kb_id or meta.get("is_folder"):
            continue
        source = (meta.get("processing_params") or {}).get("original_source")
        if isinstance(source, str) and source.startswith(("http://", "https://")):
            url_files.append((file_id, source))

    summary = {"checked": len(url_files), "updated": 0, "skipped": 0, "failed": 0}
    for file_id, source in url_files:
        try:
            content_bytes, _final_url = await fetch_url_content(source)
            new_hash = await calculate_content_hash(content_bytes)
            meta = kb_instance.files_meta[file_id]
            if new_hash == meta.get("content_hash"):
                summary["skipped"] += 1
                continue

            # 内容变化：上传新版本到 MinIO 并重置为 uploaded，走 解析 → 入库 重建
            minio_client = get_minio_client()
            bucket_name = MinIOClient.KB_BUCKETS["documents"]
            upload_result = await minio_client.aupload_file(
                bucket_name=bucket_name,
                object_name=f"{kb_id}/upload/{new_hash}.html",
                data=content_bytes,
                content_type="text/html",
            )
            meta["path"] = upload_result.url
            meta["content_hash"] = new_hash
            meta["size"] = len(content_bytes)
            meta["status"] = FileStatus.UPLOADED
            meta.pop("markdown_file", None)
            meta.pop("error", None)
            meta["updated_at"] = utc_isoformat()
            if operator_id:
                meta["updated_by"] = operator_id
            await kb_instance._persist_file(file_id)

            await kb_instance.parse_file(kb_id, file_id, operator_id)
            await kb_instance.index_file(kb_id, file_id, operator_id)
            summary["updated"] += 1
        except Exception as e:
            logger.error(f"kb_sync file {file_id} ({source}) failed: {e}")
            summary["failed"] += 1

    return summary


async def _finish(trigger_id: str, status: str) -> None:
    """终结标记：写 last_run_status 并递增 run_count。"""
    async with pg_manager.get_async_session_context() as db:
        repo = TriggerRepository(db)
        trigger = await repo.get(trigger_id)
        if trigger is None:
            return
        await repo.update_fields(
            trigger,
            fields={"last_run_status": status, "run_count": (trigger.run_count or 0) + 1},
        )
