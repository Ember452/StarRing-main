"""长期记忆业务编排 - 写入（查重/上限/双写回滚）、run 抽取、召回、删除同步。"""

from __future__ import annotations

import json
import uuid

from starring.config import config
from starring.memory.store import get_memory_store
from starring.repositories.agent_run_repository import AgentRunRepository
from starring.repositories.conversation_repository import ConversationRepository
from starring.repositories.memory_repository import MemoryRepository
from starring.storage.postgres.manager import pg_manager
from starring.storage.postgres.models_business import UserMemory
from starring.utils import logger

# 单用户记忆总数上限，超限拒绝写入
MEMORY_MAX_PER_USER = 200
# 写入查重阈值：同 uid 召回 top-3 中相似度超过该值视为重复，跳过写入
MEMORY_DEDUP_THRESHOLD = 0.92
# 注入检索 top-k 默认值
MEMORY_RETRIEVE_TOP_K = 5
# 抽取时取对话最近 N 条 user/assistant 消息作为上下文窗口
MEMORY_EXTRACT_MESSAGE_WINDOW = 20
# 单条记忆内容长度上限
MEMORY_CONTENT_MAX_LENGTH = 500

MEMORY_EXTRACT_SYSTEM_PROMPT = """你是一个用户记忆抽取助手。从下面的对话中抽取值得长期记住的「用户事实」，用于跨会话个性化。

抽取标准（全部满足才输出）：
1. 关于用户本人的稳定事实：身份、职业、偏好、习惯、长期目标、重要背景（如"用户偏好用中文回复"、"用户是后端工程师"）。
2. 跨会话仍然有效，而非一次性任务细节（如"帮我改这个 bug"、"翻译这段话"不算）。
3. 明确排除敏感信息：密码、密钥、token、身份证号、银行卡号、详细住址、健康隐私等一律不抽取。

输出要求：
- 只输出 JSON 数组，每个元素是一条简洁的中文陈述句（第三人称"用户"开头，不超过 100 字）。
- 没有值得记住的内容时输出空数组 []。
- 不要输出任何解释或代码块以外的文字。

示例输出：
["用户是一名 Python 后端工程师", "用户偏好简洁直接的回复风格"]"""


def parse_extracted_memories(content: str) -> list[str]:
    """解析 LLM 抽取输出为记忆列表。容忍 ``` 代码块包裹；非法输出抛 ValueError。"""
    content = content.strip()
    if "```" in content:
        json_start = content.find("```")
        json_start = content.find("\n", json_start)
        if json_start == -1:
            raise ValueError("AI返回的代码块不完整")
        json_end = content.find("```", json_start)
        if json_end == -1:
            raise ValueError("AI返回的代码块不完整")
        content = content[json_start:json_end].strip()

    data = json.loads(content)
    if not isinstance(data, list):
        raise ValueError("AI返回的记忆格式不正确：期望 JSON 数组")
    memories = []
    for item in data:
        if isinstance(item, str) and item.strip():
            memories.append(item.strip()[:MEMORY_CONTENT_MAX_LENGTH])
    return memories


async def add_memory(
    uid: str,
    content: str,
    *,
    source: str = "auto",
    thread_id: str | None = None,
    run_id: str | None = None,
) -> dict | None:
    """写入一条记忆。查重命中或超限时跳过返回 None；成功返回记忆 dict。

    双写顺序：先 PG（真源）后 Milvus，Milvus 失败回滚删除 PG 行。
    """
    content = (content or "").strip()[:MEMORY_CONTENT_MAX_LENGTH]
    if not content:
        return None

    store = get_memory_store()

    async with pg_manager.get_async_session_context() as db:
        repo = MemoryRepository(db)
        count = await repo.count_by_uid(uid)
        if count >= MEMORY_MAX_PER_USER:
            logger.warning(f"Memory limit reached for uid={uid} ({count}/{MEMORY_MAX_PER_USER}), skip: {content[:50]}")
            return None

        # 向量查重：同 uid top-3 相似度超阈值视为重复
        hits = await store.search(uid, content, top_k=3)
        if hits and hits[0][1] >= MEMORY_DEDUP_THRESHOLD:
            logger.info(f"Duplicate memory for uid={uid} (score={hits[0][1]:.3f}), skip: {content[:50]}")
            return None

        memory = UserMemory(
            id=uuid.uuid4().hex,
            uid=str(uid),
            content=content,
            source=source,
            thread_id=thread_id,
            run_id=run_id,
        )
        memory = await repo.create(memory)
        memory_dict = memory.to_dict()

        try:
            await store.insert(memory.id, uid, content)
        except Exception:
            # Milvus 写入失败回滚 PG，保持双写一致
            await repo.delete(memory)
            raise

    logger.info(f"Memory added for uid={uid}, source={source}: {content[:50]}")
    return memory_dict


async def extract_memories_from_run(run_id: str) -> list[dict]:
    """run 终结后异步抽取记忆：取对话最近消息 → LLM 抽取 → 逐条写入。

    抽取结果为空是正常路径；单条写入失败不影响其余条目。
    """
    from starring.models import select_model

    async with pg_manager.get_async_session_context() as db:
        run = await AgentRunRepository(db).get_run(run_id)
        if not run:
            logger.warning(f"Memory extraction: run {run_id} not found")
            return []
        uid, thread_id = run.uid, run.thread_id
        messages = await ConversationRepository(db).get_messages_by_thread_id(thread_id)

    # 取最近 N 条 user/assistant 非空消息（user 消息 run_id 可能为空，不按 run_id 过滤）
    dialog = [
        {"role": m.role, "content": m.content}
        for m in messages
        if m.role in ("user", "assistant") and (m.content or "").strip()
    ][-MEMORY_EXTRACT_MESSAGE_WINDOW:]
    if not dialog:
        return []

    dialog_text = "\n".join(f"[{m['role']}] {m['content']}" for m in dialog)
    model = select_model(model_spec=config.default_model)
    llm_messages = [
        {"role": "system", "content": MEMORY_EXTRACT_SYSTEM_PROMPT},
        {"role": "user", "content": f"对话内容：\n{dialog_text}"},
    ]
    response = await model.call(llm_messages, stream=False)
    content = response.content if hasattr(response, "content") else str(response)

    try:
        extracted = parse_extracted_memories(content)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Memory extraction parse failed for run {run_id}: {e}, raw: {content[:200]}")
        return []

    added = []
    for item in extracted:
        try:
            result = await add_memory(uid, item, source="auto", thread_id=thread_id, run_id=run_id)
            if result:
                added.append(result)
        except Exception as e:
            logger.error(f"Memory write failed for run {run_id}: {e}")
    logger.info(f"Memory extraction for run {run_id}: extracted={len(extracted)}, added={len(added)}")
    return added


async def retrieve_memories(uid: str, query: str, top_k: int = MEMORY_RETRIEVE_TOP_K) -> list[dict]:
    """向量召回记忆 id 后回 PG 取内容，保持召回相似度顺序。"""
    query = (query or "").strip()
    if not query:
        return []
    hits = await get_memory_store().search(uid, query, top_k=top_k)
    if not hits:
        return []

    async with pg_manager.get_async_session_context() as db:
        memories = await MemoryRepository(db).get_by_ids(uid, [mid for mid, _ in hits])
        memory_map = {m.id: m.to_dict() for m in memories}
    return [memory_map[mid] for mid, _ in hits if mid in memory_map]


async def list_memories(uid: str) -> list[dict]:
    """列出用户全部记忆（按创建时间倒序）。"""
    async with pg_manager.get_async_session_context() as db:
        memories = await MemoryRepository(db).list_by_uid(uid)
        return [m.to_dict() for m in memories]


async def delete_memory(uid: str, memory_id: str) -> bool:
    """删除本人一条记忆，PG + Milvus 同步。不存在或不属于本人返回 False。"""
    async with pg_manager.get_async_session_context() as db:
        repo = MemoryRepository(db)
        memory = await repo.get_for_user(memory_id, uid)
        if not memory:
            return False
        await repo.delete(memory)
    await get_memory_store().delete([memory_id])
    return True


async def clear_memories(uid: str) -> int:
    """清空本人全部记忆，PG + Milvus 同步，返回删除条数。"""
    async with pg_manager.get_async_session_context() as db:
        deleted_ids = await MemoryRepository(db).delete_all_for_user(uid)
    if deleted_ids:
        await get_memory_store().delete(deleted_ids)
    return len(deleted_ids)
