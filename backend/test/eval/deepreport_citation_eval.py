"""DeepReport 引用准确率评测脚本。

对 DeepReportAgent 产出的报告逐条回验 [S#] 引用是否真实命中知识库原文
（通过 knowledge_base.find_file_content 在被引用文件内核对 snippet），
输出引用准确率汇总；支持用 ChatbotAgent 跑同样任务作为基线对比。

运行前提：
- 后端依赖栈可用（PostgreSQL / Milvus / MinIO 等），即 docker compose 环境
  或本机已配置好 .env 的开发环境
- 评测账号（uid）对配置的知识库有访问权限

用法：
    cd backend
    python test/eval/deepreport_citation_eval.py --config test/eval/deepreport_eval_tasks.json
    python test/eval/deepreport_citation_eval.py --config ... --backend both --output eval_result.json

配置文件格式（JSON）：
    {
      "uid": "评测账号 uid",
      "model": "",                # 留空使用系统默认模型
      "kb_ids": ["kb-xxx"],       # 任务默认知识库
      "tasks": [
        {"query": "写一份 XXX 调研报告", "kb_ids": ["kb-xxx"]}  # kb_ids 可选，缺省用全局
      ]
    }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "package") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "package"))

CITATION_PATTERN = re.compile(r"\[S(\d+)\]")


def _load_config(path: str) -> dict[str, Any]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not config.get("uid"):
        raise SystemExit("配置文件缺少 uid（评测账号）")
    if not config.get("tasks"):
        raise SystemExit("配置文件缺少 tasks（评测任务列表）")
    return config


def _snippet_fragment(snippet: str, max_len: int = 30) -> str:
    """从 snippet 中取最长的一行做关键词回验（find 是按行匹配的）。"""
    lines = [line.strip() for line in str(snippet or "").splitlines() if line.strip()]
    if not lines:
        return ""
    longest = max(lines, key=len)
    return longest[:max_len]


async def _verify_source(kb_ids: list[str], source: dict[str, Any]) -> bool:
    """回验单条引用来源：snippet 片段能否在被引用文件原文中找到。"""
    from starring import knowledge_base

    file_id = str(source.get("file_id") or "").strip()
    fragment = _snippet_fragment(source.get("snippet") or "")
    if not file_id or not fragment:
        return False

    for kb_id in kb_ids:
        try:
            result = await knowledge_base.find_file_content(
                kb_id,
                file_id,
                [fragment],
                use_regex=False,
                max_windows=1,
            )
            if int(result.get("total_matches") or 0) > 0:
                return True
        except Exception:
            continue
    return False


async def _evaluate_report(report_md: str, sources: list[dict[str, Any]], kb_ids: list[str]) -> dict[str, Any]:
    """对单份报告做引用回验，返回评测指标。"""
    markers = [int(match.group(1)) for match in CITATION_PATTERN.finditer(report_md)]
    total_sources = len(sources)
    valid_markers = [marker for marker in markers if 1 <= marker <= total_sources]
    cited_ids = sorted(set(valid_markers))

    verified_ids: list[int] = []
    for source_id in cited_ids:
        if await _verify_source(kb_ids, sources[source_id - 1]):
            verified_ids.append(source_id)

    cited_count = len(cited_ids)
    return {
        "report_chars": len(report_md),
        "total_markers": len(markers),
        "valid_markers": len(valid_markers),
        "invalid_markers": len(markers) - len(valid_markers),
        "cited_sources": cited_count,
        "verified_sources": len(verified_ids),
        "unverified_source_ids": [i for i in cited_ids if i not in set(verified_ids)],
        # 引用准确率：被引用来源中能在知识库原文回验命中的比例
        "citation_accuracy": round(len(verified_ids) / cited_count, 4) if cited_count else 0.0,
        # 标记有效率：正文中 [S#] 标记指向真实来源的比例
        "marker_validity": round(len(valid_markers) / len(markers), 4) if markers else 0.0,
    }


async def _run_deepreport(task: dict[str, Any], config: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """跑一条 DeepReport 任务（大纲评审自动批准），返回 (report_md, sources)。"""
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from starring.agents.buildin import agent_manager
    from starring.agents.buildin.deepreport.nodes import REVIEW_QUESTION_ID

    agent = agent_manager.get_agent("DeepReportAgent")
    kb_ids = task.get("kb_ids") or config.get("kb_ids") or []
    context = agent.context_schema(
        uid=config["uid"],
        thread_id=f"eval-dr-{uuid.uuid4().hex[:12]}",
        model=config.get("model") or "",
        knowledges=kb_ids or None,
    )
    graph = await agent.get_graph(context=context)
    run_config = {"configurable": {"thread_id": context.thread_id, "uid": context.uid}, "recursion_limit": 300}

    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=task["query"])]},
        config=run_config,
        context=context,
    )
    if "__interrupt__" in result:
        # 大纲评审 interrupt：评测场景自动批准
        result = await graph.ainvoke(
            Command(resume={REVIEW_QUESTION_ID: "approve"}),
            config=run_config,
            context=context,
        )

    report_md = str(result.get("report_md") or "")
    sources = [
        source.model_dump() if hasattr(source, "model_dump") else dict(source)
        for source in result.get("sources") or []
    ]
    if not report_md:
        raise RuntimeError("DeepReport 未产出报告（report_md 为空）")
    return report_md, sources


async def _run_chatbot_baseline(task: dict[str, Any], config: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """基线：同样任务直接用 ChatbotAgent 生成报告（无结构化引用契约）。"""
    from langchain_core.messages import AIMessage, HumanMessage

    from starring.agents.buildin import agent_manager

    agent = agent_manager.get_agent("ChatbotAgent")
    kb_ids = task.get("kb_ids") or config.get("kb_ids") or []
    context = agent.context_schema(
        uid=config["uid"],
        thread_id=f"eval-cb-{uuid.uuid4().hex[:12]}",
        model=config.get("model") or "",
        knowledges=kb_ids or None,
        use_knowledge=True,
    )
    graph = await agent.get_graph(context=context)
    run_config = {"configurable": {"thread_id": context.thread_id, "uid": context.uid}, "recursion_limit": 300}

    prompt = (
        f"{task['query']}\n\n"
        "要求：基于知识库内容撰写完整报告，事实性陈述尽量标注引用标记（格式 [S1]、[S2]…）并在文末列出引用来源。"
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content=prompt)]}, config=run_config, context=context)

    report_md = ""
    for message in reversed(result.get("messages") or []):
        if isinstance(message, AIMessage):
            content = message.content
            report_md = content if isinstance(content, str) else str(content)
            break
    if not report_md:
        raise RuntimeError("ChatbotAgent 未产出报告")
    # 基线没有结构化 sources 契约，引用无法回验（这正是对比点）
    return report_md, []


async def _run_backend_tasks(backend: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    runner = _run_deepreport if backend == "deepreport" else _run_chatbot_baseline
    results: list[dict[str, Any]] = []
    for index, task in enumerate(config["tasks"], start=1):
        kb_ids = task.get("kb_ids") or config.get("kb_ids") or []
        print(f"[{backend}] 任务 {index}/{len(config['tasks'])}: {task['query'][:50]}...")
        try:
            report_md, sources = await runner(task, config)
            metrics = await _evaluate_report(report_md, sources, kb_ids)
            results.append({"task": task["query"], "backend": backend, "ok": True, **metrics})
            print(
                f"  引用准确率={metrics['citation_accuracy']:.2%} "
                f"（回验命中 {metrics['verified_sources']}/{metrics['cited_sources']}，"
                f"标记有效率 {metrics['marker_validity']:.2%}）"
            )
        except Exception as exc:  # noqa: BLE001 评测脚本单任务失败不中断整体
            print(f"  任务失败: {exc}")
            results.append({"task": task["query"], "backend": backend, "ok": False, "error": str(exc)})
    return results


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok_results = [item for item in results if item.get("ok")]
    total_cited = sum(item["cited_sources"] for item in ok_results)
    total_verified = sum(item["verified_sources"] for item in ok_results)
    total_markers = sum(item["total_markers"] for item in ok_results)
    total_valid = sum(item["valid_markers"] for item in ok_results)
    return {
        "tasks_total": len(results),
        "tasks_ok": len(ok_results),
        "citation_accuracy": round(total_verified / total_cited, 4) if total_cited else 0.0,
        "marker_validity": round(total_valid / total_markers, 4) if total_markers else 0.0,
        "total_cited_sources": total_cited,
        "total_verified_sources": total_verified,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="DeepReport 引用准确率评测")
    parser.add_argument("--config", required=True, help="评测任务配置文件（JSON）")
    parser.add_argument(
        "--backend",
        choices=["deepreport", "chatbot", "both"],
        default="deepreport",
        help="评测对象：deepreport / chatbot（基线）/ both（对比）",
    )
    parser.add_argument("--output", default="", help="结果 JSON 输出路径（可选）")
    args = parser.parse_args()

    config = _load_config(args.config)
    backends = ["deepreport", "chatbot"] if args.backend == "both" else [args.backend]

    all_results: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    for backend in backends:
        results = await _run_backend_tasks(backend, config)
        all_results.extend(results)
        summaries[backend] = _summarize(results)

    print("\n========== 评测汇总 ==========")
    for backend, summary in summaries.items():
        print(
            f"{backend}: 引用准确率={summary['citation_accuracy']:.2%} "
            f"标记有效率={summary['marker_validity']:.2%} "
            f"（回验命中 {summary['total_verified_sources']}/{summary['total_cited_sources']}，"
            f"任务 {summary['tasks_ok']}/{summary['tasks_total']} 成功）"
        )

    if args.output:
        payload = {"summaries": summaries, "results": all_results}
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
