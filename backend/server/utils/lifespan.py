import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # type: ignore[attr-defined]

from starring.services.task_service import tasker
from starring.agents.mcp.service import ensure_builtin_mcp_servers_in_db
from starring.models.providers.service import ensure_builtin_model_providers_in_db
from starring.services.run_queue_service import close_queue_clients, get_redis_client
from starring.storage.postgres.manager import pg_manager
from starring.knowledge import knowledge_base
from starring.utils import logger
from starring.agents.backends.sandbox import init_sandbox_provider, shutdown_sandbox_provider
from starring import get_version


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan事件管理器
    在应用启动或关闭时执行数据库初始化等行为"""
    # 初始化数据库连接
    try:
        pg_manager.initialize()
        await pg_manager.create_tables()
        await pg_manager.ensure_business_schema()
        await pg_manager.ensure_knowledge_schema()
    except Exception as e:
        logger.error(f"Failed to initialize database during startup: {e}")

    # 确保内置 MCP 服务器定义存在于数据库
    try:
        await ensure_builtin_mcp_servers_in_db()
    except Exception as e:
        logger.error(f"Failed to ensure builtin MCP servers during startup: {e}")

    try:
        from starring.agents.skills.service import init_builtin_skills

        async with pg_manager.get_async_session_context() as session:
            await init_builtin_skills(session)
    except Exception as e:
        logger.error(f"Failed to initialize builtin skills during startup: {e}")

    try:
        from starring.repositories.agent_repository import AgentRepository

        async with pg_manager.get_async_session_context() as session:
            repository = AgentRepository(session)
            await repository.ensure_default_agent()
            await repository.ensure_general_purpose_subagent()
            await repository.ensure_web_search_subagent()
            await repository.ensure_deep_research_agents()
    except Exception as e:
        logger.error(f"Failed to ensure default agent during startup: {e}")

    # 初始化内置模型供应商配置
    try:
        async with pg_manager.get_async_session_context() as session:
            await ensure_builtin_model_providers_in_db(session)
    except Exception as e:
        logger.error(f"Failed to ensure builtin model providers during startup: {e}")

    # 初始化模型缓存（v2 模型选择使用）
    try:
        from starring.models.providers.cache import model_cache
        from starring.models.providers.service import get_all_model_providers

        async with pg_manager.get_async_session_context() as session:
            providers = await get_all_model_providers(session)
            model_cache.rebuild(providers)
    except Exception as e:
        logger.error(f"Failed to initialize model cache during startup: {e}")

    # 初始化知识库管理器
    if os.environ.get("LITE_MODE", "").lower() in ("true", "1"):
        logger.info("LITE_MODE enabled, skipping knowledge base initialization")
    else:
        try:
            await knowledge_base.initialize()
        except Exception as e:
            logger.error(f"Failed to initialize knowledge base manager: {e}")

    # 预热 Redis（run 队列）
    # 确保 Redis启动正常，消除第一次访问延迟问题
    try:
        redis = await get_redis_client()
        await redis.ping()
    except Exception as e:
        logger.warning(f"Run queue redis unavailable on startup: {e}")

    try:
        init_sandbox_provider()
    except Exception as e:
        logger.error(f"Failed to initialize sandbox provider during startup: {e}")

    # =========================================================
    # 2. 核心修复：在这里执行一次 setup()，建完表就完成
    # =========================================================
    checkpointer = AsyncPostgresSaver(pg_manager.langgraph_pool)
    await checkpointer.setup()
    print("LangGraph Checkpoint tables verified/created!")

    await tasker.start()
    logger.info(f"""
  ╔═══════════════════════════════════════╗
  ║   😊StarRing  ·  Knowledge Platform   ║
  ║             v{get_version()}          ║
  ╚═══════════════════════════════════════╝
    """)
    logger.info("starring backend startup complete")
    yield
    await tasker.shutdown()
    shutdown_sandbox_provider()
    await close_queue_clients()

    # 关闭共享 Neo4j 连接，避免 driver 泄漏
    try:
        from starring.storage.neo4j.manager import get_shared_neo4j_connection

        conn = get_shared_neo4j_connection()
        if conn.is_running():
            conn.close()
            logger.info("Neo4j shared connection closed on shutdown")
    except Exception as e:
        logger.warning(f"Failed to close Neo4j connection on shutdown: {e}")

    await pg_manager.close()
