"""Milvus 记忆向量存储 - 单一 starring_memory 集合，按 uid 过滤召回。

集合仅存 id/uid/embedding，记忆内容真源在 PG user_memories 表。
embedding 使用全局默认 embedding 模型（config.embed_model）；集合与当前模型
维度/型号不匹配时直接抛错（fail-fast），提示运维重建集合，不做静默重建。
"""

from __future__ import annotations

import asyncio
import os

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    db,
    utility,
)

from starring.config import config
from starring.models.providers.cache import model_cache
from starring.utils import logger

MEMORY_COLLECTION_NAME = "starring_memory"
MEMORY_CONNECTION_ALIAS = "milvus_memory"
MEMORY_METRIC_TYPE = "COSINE"


class MemoryVectorStore:
    """记忆向量存储。懒连接 + 懒建集合，所有同步 pymilvus 调用统一线程卸载。"""

    def __init__(self):
        self.milvus_uri = os.getenv("MILVUS_URI") or "http://localhost:19530"
        self.milvus_token = os.getenv("MILVUS_TOKEN") or ""
        self.milvus_db = "starring"
        self._collection: Collection | None = None
        self._lock = asyncio.Lock()

    def _embedding_info(self):
        """获取全局默认 embedding 模型信息（维度/model_id）。"""
        info = model_cache.get_model_info(config.embed_model)
        if not info or info.model_type != "embedding":
            raise ValueError(f"Unsupported embedding model for memory: {config.embed_model}")
        return info

    def _connect_and_get_collection(self) -> Collection:
        """同步：连接 Milvus 并获取/创建记忆集合（在线程中执行）。"""
        connections.connect(alias=MEMORY_CONNECTION_ALIAS, uri=self.milvus_uri, token=self.milvus_token)
        try:
            if self.milvus_db not in db.list_database(using=MEMORY_CONNECTION_ALIAS):
                db.create_database(self.milvus_db, using=MEMORY_CONNECTION_ALIAS)
            db.using_database(self.milvus_db, using=MEMORY_CONNECTION_ALIAS)
        except Exception as e:
            logger.warning(f"Memory store database operation failed, using default: {e}")

        info = self._embedding_info()
        if utility.has_collection(MEMORY_COLLECTION_NAME, using=MEMORY_CONNECTION_ALIAS):
            collection = Collection(name=MEMORY_COLLECTION_NAME, using=MEMORY_CONNECTION_ALIAS)
            # 模型不匹配直接抛错：更换 embedding 模型属运维事件，需手动重建记忆集合
            if info.model_id not in collection.description:
                raise RuntimeError(
                    f"Memory collection '{MEMORY_COLLECTION_NAME}' was built with a different "
                    f"embedding model (expected '{info.model_id}', description: "
                    f"'{collection.description}'). Drop the collection manually to rebuild."
                )
        else:
            embedding_dim = info.dimension or 1024
            fields = [
                FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=100, is_primary=True),
                FieldSchema(name="uid", dtype=DataType.VARCHAR, max_length=100),
                FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=embedding_dim),
            ]
            schema = CollectionSchema(
                fields=fields,
                description=f"User long-term memory collection using {info.model_id}",
            )
            collection = Collection(name=MEMORY_COLLECTION_NAME, schema=schema, using=MEMORY_CONNECTION_ALIAS)
            index_params = {"metric_type": MEMORY_METRIC_TYPE, "index_type": "IVF_FLAT", "params": {"nlist": 1024}}
            collection.create_index("embedding", index_params)
            logger.info(
                f"Created memory collection '{MEMORY_COLLECTION_NAME}', model={info.model_id}, dim={embedding_dim}"
            )

        collection.load()
        return collection

    async def _get_collection(self) -> Collection:
        if self._collection is not None:
            return self._collection
        async with self._lock:
            if self._collection is None:
                self._collection = await asyncio.to_thread(self._connect_and_get_collection)
        return self._collection

    async def _encode(self, text: str) -> list[float]:
        from starring.models.embed import select_embedding_model

        model = select_embedding_model(config.embed_model)
        embeddings = await model.aencode([text])
        return embeddings[0]

    async def insert(self, memory_id: str, uid: str, content: str) -> None:
        """写入一条记忆向量。"""
        collection = await self._get_collection()
        embedding = await self._encode(content)

        def _insert():
            collection.insert([[memory_id], [str(uid)], [embedding]])
            collection.flush()

        await asyncio.to_thread(_insert)

    async def search(self, uid: str, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """按 uid 过滤召回，返回 [(memory_id, 相似度)]，按相似度降序。"""
        collection = await self._get_collection()
        embedding = await self._encode(query)

        def _search():
            results = collection.search(
                data=[embedding],
                anns_field="embedding",
                param={"metric_type": MEMORY_METRIC_TYPE, "params": {"nprobe": 10}},
                limit=top_k,
                expr=f'uid == "{uid}"',
                output_fields=["id"],
            )
            hits = []
            for hit in results[0]:
                hits.append((str(hit.id), float(hit.distance)))
            return hits

        return await asyncio.to_thread(_search)

    async def delete(self, memory_ids: list[str]) -> None:
        """按 id 列表删除记忆向量。"""
        if not memory_ids:
            return
        collection = await self._get_collection()
        id_list = ", ".join(f'"{mid}"' for mid in memory_ids)

        def _delete():
            collection.delete(expr=f"id in [{id_list}]")
            collection.flush()

        await asyncio.to_thread(_delete)


_memory_store: MemoryVectorStore | None = None


def get_memory_store() -> MemoryVectorStore:
    """获取记忆向量存储懒单例。"""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryVectorStore()
    return _memory_store
