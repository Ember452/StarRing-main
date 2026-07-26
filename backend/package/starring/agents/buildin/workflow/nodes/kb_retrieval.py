"""kb-retrieval 节点执行器：确定性知识库检索。

不经过 LLM，直接在配置的知识库中检索，query 支持 {{ expr }} 内嵌插值从上游
节点输出取值。权限主体为工作流 owner（context.uid），复用 query_kb 工具的
可见库解析 + retriever 调用链路。检索结果写入 SubAgentDeliverable，供下游
llm / condition 节点消费。

设计依据：docs/vibe/工作流能力增强设计-20260725.md §一
"""

from __future__ import annotations

from starring.agents.buildin.workflow.context import WorkflowContext
from starring.agents.buildin.workflow.definition import Node
from starring.agents.buildin.workflow.nodes import register_node
from starring.agents.buildin.workflow.nodes.expr import render_template
from starring.agents.buildin.workflow.state import WorkflowState
from starring.agents.middlewares.subagent_deliverable import SubAgentDeliverable


@register_node("kb-retrieval")
async def execute_kb_retrieval(state: WorkflowState, node: Node, context: WorkflowContext) -> dict:
    """确定性知识库检索节点执行器。

    config 字段:
        query: 检索文本（必填，支持 {{ expr }} 内嵌插值）
        kb_ids: 知识库 ID 白名单（可选，空表示检索 owner 全部可见库）
        top_k: 每库返回条数（可选，1-50，不填用知识库默认参数）
    """
    config = node.config
    query_text = render_template(config["query"], state, where=f"kb-retrieval 节点 {node.id} 的 query")
    if not query_text.strip():
        raise ValueError(f"kb-retrieval 节点 {node.id} 的 query 渲染后为空")

    # 可见库解析（延迟导入避免模块加载时触发知识库初始化）
    from starring import knowledge_base
    from starring.agents.backends.knowledge_base_backend import resolve_visible_knowledge_bases_for_context

    visible_kbs = await resolve_visible_knowledge_bases_for_context(context)
    if not visible_kbs:
        raise ValueError(f"kb-retrieval 节点 {node.id} 无可用知识库（uid={context.uid!r} 无可见知识库）")
    visible_ids = [str(kb.get("kb_id") or "").strip() for kb in visible_kbs]

    # kb_ids 白名单与可见库求交：配置了不可见/不存在的库直接报错（fail-fast，不静默跳过）
    kb_ids_cfg = [str(kb_id).strip() for kb_id in (config.get("kb_ids") or [])]
    if kb_ids_cfg:
        invisible = [kb_id for kb_id in kb_ids_cfg if kb_id not in visible_ids]
        if invisible:
            raise ValueError(f"kb-retrieval 节点 {node.id} 配置的知识库 {invisible} 不存在或无权限访问")
        target_ids = kb_ids_cfg
    else:
        target_ids = visible_ids

    retrievers = knowledge_base.get_retrievers()
    query_kwargs = {}
    if config.get("top_k"):
        query_kwargs["final_top_k"] = config["top_k"]

    # 逐库检索并拼接结果（检索异常直接上抛，由 _wrap_node_executor 统一记日志）
    kb_names = {str(kb.get("kb_id") or "").strip(): kb.get("name", "") for kb in visible_kbs}
    sections: list[str] = []
    hits_by_kb: dict[str, int] = {}
    for kb_id in target_ids:
        target_info = retrievers.get(kb_id)
        if target_info is None:
            raise ValueError(f"kb-retrieval 节点 {node.id} 的知识库 {kb_id!r} 检索器不存在（可能未就绪）")
        result = await target_info["retriever"](query_text, **query_kwargs)
        results = result.get("results", []) if isinstance(result, dict) else []
        hits_by_kb[kb_id] = len(results)
        for item in results:
            content = item.get("content", "") if isinstance(item, dict) else str(item)
            file_id = item.get("file_id", "") if isinstance(item, dict) else ""
            sections.append(f"[知识库 {kb_names.get(kb_id) or kb_id} | 文件 {file_id}]\n{content}")

    total_hits = sum(hits_by_kb.values())
    raw = "\n\n".join(sections) if sections else f"未检索到与 {query_text!r} 相关的内容"

    deliverable = SubAgentDeliverable(
        summary=f"知识库检索 {query_text!r}：{len(target_ids)} 个库共命中 {total_hits} 条",
        raw_text=raw,
        confidence=1.0,
    )
    return {"node_outputs": {node.id: deliverable}}
