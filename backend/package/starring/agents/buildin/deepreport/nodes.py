"""DeepReport 流水线五节点实现。

图结构：START → plan → review → (条件边) → [Send("research_chapter") × N]
        → synthesize → citation_check → END

节点函数签名为 (context, state)，由 backend 通过 functools.partial 绑定 context
（与 workflow backend 的 _wrap_node_executor 相同做法）。

防幻觉设计（GroundedAgent 模式）：
- research_chapter 内部研究/写作双 LLM 分离：研究员只收集带来源的事实，
  写作者只能引用编号后的 [S#] 事实
- synthesize 确定性完成全局引用重编号与正文拼接（引用标记不经 LLM），
  LLM 只看章节摘要写引言/结论，失败时降级为确定性模板拼接
- citation_check 确定性回验所有 [S#] 标记、对被引用的 kb 来源做语义回验，
  并生成引用来源章节
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.errors import GraphInterrupt
from langgraph.types import Send, interrupt

from starring.agents.buildin.deepreport.context import DeepReportContext
from starring.agents.buildin.deepreport.prompt import (
    OUTLINE_FEEDBACK_SECTION,
    OUTLINE_PROMPT,
    RESEARCHER_PROMPT,
    SYNTHESIS_PROMPT,
    WRITER_INPUT_TEMPLATE,
    WRITER_PROMPT,
)
from starring.agents.buildin.deepreport.state import (
    ChapterResearch,
    ChapterResult,
    DeepReportState,
    Outline,
    OutlineChapter,
    ReportFraming,
)
from starring.agents.middlewares.subagent_deliverable import SubAgentSource
from starring.agents.models import load_chat_model, resolve_chat_model_spec
from starring.utils import logger
from starring.utils.question_utils import normalize_questions

# review 节点 interrupt 问题的固定 ID（resume 答案按此 ID 提取）
REVIEW_QUESTION_ID = "deepreport-outline-review"
# 大纲评审的"批准"选项值
APPROVE_VALUE = "approve"
# 引用标记模式：[S1]、[S23]
CITATION_PATTERN = re.compile(r"\[S(\d+)\]")
# 研究员子图的递归上限（章节级研究不需要太多步数）
_RESEARCH_RECURSION_LIMIT = 50
# 章节阶段（研究/写作）最大尝试次数（瞬时网络/模型错误重试一次即可，避免拉长流水线时延）
_CHAPTER_MAX_ATTEMPTS = 2
# 章节阶段重试间隔（秒）
_CHAPTER_RETRY_DELAY_SECONDS = 2.0
# 合成阶段每章送入 LLM 的摘要长度上限（正文由确定性拼接，LLM 只看摘要写引言/结论）
_SYNTHESIS_SUMMARY_CHARS = 300
# citation_check 语义回验的来源数量上限（防超长报告拖慢终检）
_VERIFY_MAX_SOURCES = 30
# 进度事件的 source 标识（前端进度卡片按此识别）
PROGRESS_SOURCE = "deepreport_progress"


# ========== 通用辅助 ==========


def _content_to_text(content: Any) -> str:
    """把 LangChain 消息 content（str 或 content blocks 列表）拍平成文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return str(content or "")


def _latest_user_query(messages: list) -> str:
    """从消息列表末尾向前找最近一条用户消息作为报告需求。"""
    for message in reversed(messages or []):
        if isinstance(message, HumanMessage) or getattr(message, "type", None) == "human":
            text = _content_to_text(getattr(message, "content", "")).strip()
            if text:
                return text
    raise ValueError("DeepReport 需要用户提供报告需求（未找到用户消息）")


def _load_llm(context: DeepReportContext):
    """加载 context 配置的聊天模型（留空回退系统默认模型）。"""
    return load_chat_model(fully_specified_name=resolve_chat_model_spec(context.model))


def _visible_kbs(context: DeepReportContext) -> list[dict[str, Any]]:
    """读取 prepare_agent_runtime_context 解析好的会话可见知识库列表。"""
    visible = getattr(context, "_visible_knowledge_bases", None)
    return visible if isinstance(visible, list) else []


def _emit_progress(payload: dict[str, Any]) -> None:
    """向流式通道写阶段/章节进度事件（custom 流，前端进度卡片消费）。

    非流式调用（单测直调节点 / ainvoke）时 stream writer 不存在，静默跳过；
    进度事件是旁路可观测性，任何失败都不影响流水线主链路。
    """
    try:
        writer = get_stream_writer()
        writer({"source": PROGRESS_SOURCE, **payload})
    except Exception:
        return


def outline_to_markdown(outline: Outline) -> str:
    """把大纲渲染为 markdown（用于评审问题文本与重规划 prompt）。"""
    lines = [f"# {outline.title}"]
    for idx, chapter in enumerate(outline.chapters, start=1):
        lines.append(f"{idx}. **{chapter.heading}**")
        if chapter.brief:
            lines.append(f"   - {chapter.brief}")
    return "\n".join(lines)


# ========== plan 节点 ==========


def _mindmap_to_text(node: dict, level: int = 0) -> str:
    """递归把知识库思维导图 JSON 转为层级文本（同 get_mindmap 工具的展示格式）。"""
    if not isinstance(node, dict):
        return ""
    indent = "  " * level
    text = f"{indent}- {node.get('content', '')}\n"
    for child in node.get("children", []) or []:
        text += _mindmap_to_text(child, level + 1)
    return text


async def _load_kb_structure(context: DeepReportContext) -> str:
    """汇总会话可见知识库的思维导图结构，供大纲生成使用。

    fail-fast：无可访问知识库、或所有知识库都没有思维导图时直接报错
    （符合 AGENTS.md 的 fail-fast 原则，让用户先完成知识库准备）。
    """
    visible = _visible_kbs(context)
    if not visible:
        raise ValueError("DeepReport 需要至少一个可访问的知识库，请先在智能体配置中启用知识库")

    from starring.repositories.knowledge_base_repository import KnowledgeBaseRepository

    kb_repo = KnowledgeBaseRepository()
    sections: list[str] = []
    mindmap_found = False
    for kb_info in visible:
        kb_id = str(kb_info.get("kb_id") or "").strip()
        name = str(kb_info.get("name") or kb_id)
        description = str(kb_info.get("description") or "").strip()
        if not kb_id:
            continue
        kb = await kb_repo.get_by_kb_id(kb_id)
        mindmap = getattr(kb, "mindmap", None) if kb is not None else None
        if mindmap:
            mindmap_found = True
            sections.append(f"### 知识库「{name}」（kb_id: {kb_id}）\n{_mindmap_to_text(mindmap)}")
        else:
            sections.append(f"### 知识库「{name}」（kb_id: {kb_id}）\n（暂无思维导图）{description}")

    if not mindmap_found:
        raise ValueError(
            "DeepReport 依赖知识库思维导图来规划大纲，但当前启用的知识库都还没有生成思维导图，"
            "请先在知识库页面生成思维导图后重试"
        )
    return "\n\n".join(sections)


def normalize_outline(outline: Outline, max_chapters: int) -> Outline:
    """规整 LLM 生成的大纲：截断到 max_chapters、重新分配稳定章节 ID。"""
    limit = max_chapters if isinstance(max_chapters, int) and max_chapters > 0 else 8
    chapters = [chapter for chapter in outline.chapters if str(chapter.heading or "").strip()][:limit]
    if not chapters:
        raise ValueError("大纲生成失败：没有有效章节，请调整报告需求后重试")
    normalized = [
        OutlineChapter(id=f"ch-{idx}", heading=chapter.heading.strip(), brief=str(chapter.brief or "").strip())
        for idx, chapter in enumerate(chapters, start=1)
    ]
    title = str(outline.title or "").strip() or "研究报告"
    return Outline(title=title, chapters=normalized)


async def plan_node(context: DeepReportContext, state: DeepReportState) -> dict:
    """大纲生成：知识库思维导图 + 用户需求 → LLM 结构化输出 Outline。

    首次进入生成初版大纲；带 review_feedback 进入时按修改意见重新生成。
    """
    query = _latest_user_query(state.get("messages") or [])
    _emit_progress({"stage": "plan", "status": "started"})
    kb_structure = await _load_kb_structure(context)

    feedback = str(state.get("review_feedback") or "").strip()
    previous_outline = state.get("outline")
    feedback_section = "\n"
    if feedback and previous_outline is not None:
        feedback_section = OUTLINE_FEEDBACK_SECTION.format(
            feedback=feedback,
            previous_outline=outline_to_markdown(previous_outline),
        )

    prompt = OUTLINE_PROMPT.format(
        max_chapters=context.max_chapters,
        kb_structure=kb_structure,
        feedback_section=feedback_section,
        query=query,
    )
    llm = _load_llm(context).with_structured_output(Outline)
    outline = await llm.ainvoke(prompt)
    if not isinstance(outline, Outline):
        raise ValueError("大纲生成失败：模型未返回结构化大纲")

    outline = normalize_outline(outline, context.max_chapters)
    logger.info(f"DeepReport 大纲生成完成: {outline.title}（{len(outline.chapters)} 章）")
    _emit_progress(
        {"stage": "plan", "status": "completed", "title": outline.title, "total_chapters": len(outline.chapters)}
    )
    # 清空反馈，避免旧反馈残留触发死循环
    return {"outline": outline, "review_feedback": ""}


# ========== review 节点 ==========


def build_review_questions(outline: Outline) -> list[dict[str, Any]]:
    """构造大纲评审问题（复用 ask_user_question 的 questions 契约，前端零改动渲染）。"""
    question_text = (
        "已生成报告大纲，请确认是否按此大纲撰写报告：\n\n"
        f"{outline_to_markdown(outline)}\n\n"
        "如需调整，请选择 Other 并输入修改意见。"
    )
    return normalize_questions(
        [
            {
                "question_id": REVIEW_QUESTION_ID,
                "question": question_text,
                "options": [{"label": "按此大纲生成报告 (Recommended)", "value": APPROVE_VALUE}],
                "multi_select": False,
                "allow_other": True,
            }
        ]
    )


def parse_review_answer(answer: Any) -> str:
    """解析 resume 回传的评审答案，返回修改意见（空串表示批准）。

    ask_user_question 的 answer 契约：{question_id: value}，value 可能是
    string（选项）、list（多选）或 dict（Other 自定义文本）。这里做宽容解析，
    无法识别的结构一律当作批准处理（不阻塞流水线）。
    """
    value: Any = answer
    if isinstance(answer, dict):
        value = answer.get(REVIEW_QUESTION_ID)
        if value is None and answer:
            value = next(iter(answer.values()))

    if isinstance(value, dict):
        # Other 自定义文本：取常见字段，取不到时拼接全部字符串值
        for key in ("other", "value", "text", "answer"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                value = candidate
                break
        else:
            value = " ".join(str(v) for v in value.values() if isinstance(v, str))
    elif isinstance(value, list):
        value = " ".join(str(item) for item in value)

    text = str(value or "").strip()
    if not text:
        # 有回传但解析不出内容：静默批准前留痕，便于排查前端 answer 结构变更
        if answer not in (None, ""):
            logger.warning(f"DeepReport 大纲评审答案无法识别，按批准处理: {answer!r}")
        return ""
    if text.lower() == APPROVE_VALUE:
        return ""
    return text


def review_node(context: DeepReportContext, state: DeepReportState) -> dict:
    """大纲人工评审：interrupt 等待用户批准或给出修改意见。

    三分支：
    - 批准（或答案无法识别）→ review_feedback 置空，继续 research
    - 修改意见且未超 max_replan → 记录反馈回 plan 重新生成
    - 修改意见但已达 max_replan → 一次性闸门，直接采用当前大纲继续（防死循环）
    """
    outline = state.get("outline")
    if outline is None:
        raise ValueError("review 节点缺少大纲（plan 节点未产出 outline）")

    answer = interrupt(
        {
            "questions": build_review_questions(outline),
            "source": "ask_user_question",
        }
    )
    feedback = parse_review_answer(answer)
    if not feedback:
        return {"review_feedback": ""}

    replan_count = int(state.get("replan_count") or 0)
    max_replan = context.max_replan if isinstance(context.max_replan, int) and context.max_replan >= 0 else 0
    if replan_count >= max_replan:
        logger.warning(f"DeepReport 重新规划次数已达上限（{max_replan}），忽略新修改意见，直接采用当前大纲继续")
        return {"review_feedback": ""}

    logger.info(f"DeepReport 收到大纲修改意见（第 {replan_count + 1} 次重新规划）: {feedback[:100]}")
    return {"review_feedback": feedback, "replan_count": replan_count + 1}


def route_after_review(state: DeepReportState):
    """review 后条件路由：有修改意见回 plan，否则按章节 Send fan-out 并行研究。"""
    if str(state.get("review_feedback") or "").strip():
        return "plan"

    outline = state.get("outline")
    if outline is None or not outline.chapters:
        raise ValueError("无法开始章节研究：大纲为空")
    total = len(outline.chapters)
    return [
        Send(
            "research_chapter",
            {
                "chapter": chapter.model_dump(),
                "report_title": outline.title,
                "chapter_index": index,
                "total_chapters": total,
            },
        )
        for index, chapter in enumerate(outline.chapters, start=1)
    ]


# ========== research_chapter 节点（Send 并行分支） ==========


def _kb_list_text(context: DeepReportContext) -> str:
    lines = [f"- {kb.get('kb_id')}: {kb.get('name')}" for kb in _visible_kbs(context) if kb.get("kb_id")]
    return "\n".join(lines) if lines else "（无）"


async def _run_researcher(context: DeepReportContext, chapter: dict[str, Any], report_title: str) -> ChapterResearch:
    """研究阶段：进程内 create_agent 子图（挂知识库工具 + ToolStrategy 结构化输出）。

    invoke 时透传父 runtime context，保证 uid / 知识库可见性权限一致。
    瞬时错误（网络/模型抖动）重试一次；GraphInterrupt 属于控制流必须透传。
    """
    from starring.agents.toolkits.kbs.tools import find_kb_document, open_kb_document, query_kb

    researcher = create_agent(
        model=_load_llm(context),
        tools=[query_kb, find_kb_document, open_kb_document],
        system_prompt=RESEARCHER_PROMPT.format(
            report_title=report_title,
            heading=chapter.get("heading", ""),
            brief=chapter.get("brief", "") or "（无）",
            kb_list=_kb_list_text(context),
        ),
        response_format=ToolStrategy(ChapterResearch),
    )
    last_error: Exception | None = None
    for attempt in range(1, _CHAPTER_MAX_ATTEMPTS + 1):
        try:
            result = await researcher.ainvoke(
                {"messages": [HumanMessage(content=f"请为章节「{chapter.get('heading', '')}」收集事实清单。")]},
                context=context,
                config={"recursion_limit": _RESEARCH_RECURSION_LIMIT},
            )
            research = result.get("structured_response") if isinstance(result, dict) else None
            if not isinstance(research, ChapterResearch):
                raise ValueError("研究员未返回结构化事实清单")
            return research
        except GraphInterrupt:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= _CHAPTER_MAX_ATTEMPTS:
                break
            logger.warning(
                f"DeepReport 章节「{chapter.get('heading', '')}」研究第 {attempt} 次尝试失败，"
                f"{_CHAPTER_RETRY_DELAY_SECONDS}s 后重试: {exc}"
            )
            await asyncio.sleep(_CHAPTER_RETRY_DELAY_SECONDS)
    raise last_error if last_error is not None else ValueError("研究员执行失败")


def format_facts(facts: list) -> str:
    """把事实清单编号为 [S1..Sn] 文本，供写作阶段引用。"""
    lines: list[str] = []
    for idx, fact in enumerate(facts, start=1):
        snippet = str(getattr(fact.source, "snippet", "") or "").strip()
        line = f"S{idx}: {fact.statement}"
        if snippet:
            line += f"\n  来源片段: {snippet[:200]}"
        lines.append(line)
    return "\n".join(lines)


async def _run_writer(context: DeepReportContext, chapter: dict[str, Any], report_title: str, facts: list) -> str:
    """写作阶段：纯 LLM 调用（无工具），输入仅为编号后的事实清单。

    瞬时错误重试一次：写作失败会作废整章已完成的研究成果，重试收益远大于时延成本。
    """
    writer_input = WRITER_INPUT_TEMPLATE.format(
        report_title=report_title,
        heading=chapter.get("heading", ""),
        brief=chapter.get("brief", "") or "（无）",
        facts=format_facts(facts),
    )
    last_error: Exception | None = None
    for attempt in range(1, _CHAPTER_MAX_ATTEMPTS + 1):
        try:
            response = await _load_llm(context).ainvoke(
                [SystemMessage(content=WRITER_PROMPT), HumanMessage(content=writer_input)]
            )
            content = _content_to_text(response.content).strip()
            if not content:
                raise ValueError("写作阶段未产出章节正文")
            return content
        except Exception as exc:
            last_error = exc
            if attempt >= _CHAPTER_MAX_ATTEMPTS:
                break
            logger.warning(
                f"DeepReport 章节「{chapter.get('heading', '')}」写作第 {attempt} 次尝试失败，"
                f"{_CHAPTER_RETRY_DELAY_SECONDS}s 后重试: {exc}"
            )
            await asyncio.sleep(_CHAPTER_RETRY_DELAY_SECONDS)
    raise last_error if last_error is not None else ValueError("写作阶段执行失败")


async def research_chapter_node(context: DeepReportContext, payload: dict[str, Any]) -> dict:
    """单章节研究 + 写作（Send 并行分支）。

    单章失败不炸全局：写入占位 ChapterResult（confidence=0，正文注明失败原因），
    由合成阶段如实呈现。GraphInterrupt 属于控制流，必须透传不吞掉。
    """
    chapter = payload.get("chapter") or {}
    chapter_id = str(chapter.get("id") or "")
    heading = str(chapter.get("heading") or "")
    report_title = str(payload.get("report_title") or "")
    progress_base = {
        "stage": "research",
        "chapter_id": chapter_id,
        "heading": heading,
        "chapter_index": payload.get("chapter_index"),
        "total_chapters": payload.get("total_chapters"),
    }
    _emit_progress({**progress_base, "status": "started"})

    try:
        research = await _run_researcher(context, chapter, report_title)
        facts = [fact for fact in research.facts if str(fact.statement or "").strip()]
        if not facts:
            logger.warning(f"DeepReport 章节「{heading}」未检索到任何事实，写入占位结果")
            _emit_progress({**progress_base, "status": "completed", "facts_count": 0})
            return {
                "chapters": [
                    ChapterResult(
                        chapter_id=chapter_id,
                        heading=heading,
                        content_md="> 本章节未能从知识库中检索到相关事实，暂无法撰写。",
                        confidence=0.0,
                    )
                ]
            }

        content_md = await _run_writer(context, chapter, report_title, facts)
        # 置信度 = 研究员自评（主导）+ 事实数量兜底修正（防自评极端虚高/虚低）
        confidence = round(min(1.0, 0.6 * research.confidence + 0.4 * min(1.0, len(facts) / 6)), 2)
        _emit_progress({**progress_base, "status": "completed", "facts_count": len(facts)})
        return {
            "chapters": [
                ChapterResult(
                    chapter_id=chapter_id,
                    heading=heading,
                    content_md=content_md,
                    sources=[fact.source for fact in facts],
                    confidence=confidence,
                )
            ]
        }
    except GraphInterrupt:
        raise
    except Exception as exc:
        logger.error(f"DeepReport 章节「{heading}」生成失败: {exc}", exc_info=True)
        _emit_progress({**progress_base, "status": "failed", "error": str(exc)[:200]})
        return {
            "chapters": [
                ChapterResult(
                    chapter_id=chapter_id,
                    heading=heading,
                    content_md=f"> 本章节生成失败：{exc}",
                    confidence=0.0,
                )
            ]
        }


# ========== synthesize 节点 ==========


def renumber_citations(
    ordered_chapters: list[ChapterResult],
) -> tuple[list[SubAgentSource], list[tuple[str, str]]]:
    """确定性完成全局引用重编号。

    各章节正文中的局部 [S#]（# 为章内 sources 下标 +1）统一映射为全局编号；
    超出章内 sources 范围的无效局部标记直接剔除（写作 LLM 编造的引用）。

    返回: (全局 sources 列表, [(章节标题, 重编号后正文), ...])
    """
    global_sources: list[SubAgentSource] = []
    sections: list[tuple[str, str]] = []
    for chapter in ordered_chapters:
        offset = len(global_sources)
        local_count = len(chapter.sources)

        def _replace(match: re.Match, *, _offset: int = offset, _count: int = local_count) -> str:
            local = int(match.group(1))
            if 1 <= local <= _count:
                return f"[S{local + _offset}]"
            return ""

        content = CITATION_PATTERN.sub(_replace, chapter.content_md)
        global_sources.extend(chapter.sources)
        sections.append((chapter.heading, content))
    return global_sources, sections


def build_fallback_report(title: str, sections: list[tuple[str, str]]) -> str:
    """确定性模板拼接（合成 LLM 失败时的降级路径）：标题 + 各章正文。"""
    parts = [f"# {title}"]
    for heading, content in sections:
        parts.append(f"## {heading}\n\n{content}")
    return "\n\n".join(parts)


def _chapter_summary(content: str, max_chars: int = _SYNTHESIS_SUMMARY_CHARS) -> str:
    """把章节正文压成给合成 LLM 看的摘要：剔除引用标记、压空白、截断。"""
    text = CITATION_PATTERN.sub("", content)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _ordered_chapter_results(state: DeepReportState) -> tuple[Outline, list[ChapterResult]]:
    """按大纲顺序整理章节结果，缺失章节补占位（防御 Send 分支意外无写回）。"""
    outline = state.get("outline")
    if outline is None:
        raise ValueError("synthesize 节点缺少大纲")
    by_id = {chapter.chapter_id: chapter for chapter in (state.get("chapters") or [])}
    ordered: list[ChapterResult] = []
    for chapter in outline.chapters:
        result = by_id.get(chapter.id)
        if result is None:
            result = ChapterResult(
                chapter_id=chapter.id,
                heading=chapter.heading,
                content_md="> 本章节未产出内容。",
                confidence=0.0,
            )
        ordered.append(result)
    return outline, ordered


async def synthesize_node(context: DeepReportContext, state: DeepReportState) -> dict:
    """合成：确定性全局引用重编号 + 正文拼接，LLM 只看章节摘要写引言/结论。

    正文（含 [S#] 引用标记）完全不经 LLM，从根上消除合成阶段篡改/丢失引用的风险；
    引言/结论生成失败时降级为确定性模板拼接，保证流水线始终有产出。
    """
    _emit_progress({"stage": "synthesize", "status": "started"})
    outline, ordered = _ordered_chapter_results(state)
    global_sources, sections = renumber_citations(ordered)

    body_md = "\n\n".join(f"## {heading}\n\n{content}" for heading, content in sections)
    try:
        summaries = "\n\n".join(f"### {heading}\n{_chapter_summary(content)}" for heading, content in sections)
        llm = _load_llm(context).with_structured_output(ReportFraming)
        framing = await llm.ainvoke(SYNTHESIS_PROMPT.format(title=outline.title, chapters=summaries))
        if not isinstance(framing, ReportFraming) or not framing.introduction.strip():
            raise ValueError("合成模型未返回有效引言/结论")
        # 铁律兼容：引言/结论不应含引用标记，LLM 违规时确定性剔除
        introduction = CITATION_PATTERN.sub("", framing.introduction).strip()
        conclusion = CITATION_PATTERN.sub("", framing.conclusion).strip()
        parts = [f"# {outline.title}", introduction, body_md]
        if conclusion:
            parts.append(f"## 结论\n\n{conclusion}")
        report_md = "\n\n".join(parts)
    except Exception as exc:
        # 降级路径：确定性模板拼接，保证流水线始终有产出（DevAgent-Studio Reporter 三层模式）
        logger.warning(f"DeepReport 引言/结论生成失败，降级为确定性模板拼接: {exc}")
        report_md = build_fallback_report(outline.title, sections)

    _emit_progress({"stage": "synthesize", "status": "completed", "total_sources": len(global_sources)})
    return {"report_md": report_md, "sources": global_sources}


# ========== citation_check 节点 ==========


def _source_line(index: int, source: SubAgentSource, unverified: bool = False) -> str:
    """渲染单条引用来源（file_id / url / snippet 预览，回验未命中时标注）。"""
    location = source.url or source.file_id or "未知来源"
    snippet = (source.snippet or "").strip().replace("\n", " ")
    preview = f"：{snippet[:80]}..." if len(snippet) > 80 else (f"：{snippet}" if snippet else "")
    mark = "（未回验）" if unverified else ""
    return f"- [S{index}] ({source.type}) {location}{mark}{preview}"


def _snippet_fragment(snippet: str, max_len: int = 30) -> str:
    """从 snippet 中取最长的一行做关键词回验（find_file_content 是按行匹配的）。"""
    lines = [line.strip() for line in str(snippet or "").splitlines() if line.strip()]
    if not lines:
        return ""
    longest = max(lines, key=len)
    return longest[:max_len]


def _fragment_regex(fragment: str) -> str:
    """把精确片段转成空白不敏感正则（字符间允许任意空白）。

    只容忍入库规整引入的空白/换行差异，不放松字符序列本身的匹配要求。
    """
    return r"\s*".join(re.escape(ch) for ch in fragment if not ch.isspace())


async def _verify_source_in_kbs(kb_ids: list[str], source: SubAgentSource) -> bool:
    """回验单条引用来源：snippet 片段能否在被引用文件原文中找到。

    SubAgentSource 不携带 kb_id，逐个可见知识库尝试（命中即停）；
    精确匹配 miss 时降级为空白不敏感正则重试（容忍入库时的空白/换行规整差异，
    减少真实引用被误标"未回验"）；单库查询异常不影响其他库的尝试。
    """
    from starring import knowledge_base

    file_id = str(source.file_id or "").strip()
    fragment = _snippet_fragment(source.snippet or "")
    if not file_id or not fragment:
        return False
    patterns = [(fragment, False), (_fragment_regex(fragment), True)]
    for kb_id in kb_ids:
        for pattern, use_regex in patterns:
            try:
                result = await knowledge_base.find_file_content(
                    kb_id,
                    file_id,
                    [pattern],
                    use_regex=use_regex,
                    max_windows=1,
                )
                if int(result.get("total_matches") or 0) > 0:
                    return True
            except Exception:
                # 该库查询异常（如文件不属于此库），换下一个库
                break
    return False


async def _verify_citation_sources(
    context: DeepReportContext, sources: list[SubAgentSource], cited_ids: list[int]
) -> tuple[dict[str, Any], set[int]]:
    """对被引用的 kb 来源做语义回验（find_file_content 轻量版）。

    只回验真正被 [S#] 引用且类型为 kb_chunk/file 的来源，数量设上限防超长报告
    拖慢终检；返回 (回验统计, 未命中的引用编号集合)。
    """
    kb_ids = [str(kb.get("kb_id") or "").strip() for kb in _visible_kbs(context) if kb.get("kb_id")]
    if not kb_ids or not cited_ids:
        return {"mode": "skipped", "reason": "无可见知识库" if not kb_ids else "无被引用来源"}, set()

    checked: list[int] = []
    skipped: list[int] = []
    for source_id in cited_ids:
        source = sources[source_id - 1]
        if source.type in ("kb_chunk", "file") and len(checked) < _VERIFY_MAX_SOURCES:
            checked.append(source_id)
        else:
            skipped.append(source_id)

    results = await asyncio.gather(
        *(_verify_source_in_kbs(kb_ids, sources[source_id - 1]) for source_id in checked),
        return_exceptions=True,
    )
    unverified = {source_id for source_id, hit in zip(checked, results) if hit is not True}
    if unverified:
        logger.warning(f"DeepReport 语义回验未命中 {len(unverified)}/{len(checked)} 条引用: {sorted(unverified)}")
    stats = {
        "mode": "find_file_content",
        "checked": len(checked),
        "verified": len(checked) - len(unverified),
        "unverified": sorted(unverified),
        "skipped": skipped,
    }
    return stats, unverified


async def citation_check_node(context: DeepReportContext, state: DeepReportState) -> dict:
    """引用回验：剔除无效 [S#] 标记、语义回验被引用来源、生成引用统计与引用来源章节。

    引用来源章节只列正文真正引用的来源（uncited 仅进 citation_report 供排查）；
    语义回验未命中的条目加“（未回验）”标注，不阻断产出。
    最终报告写入 state.report_md 并作为最终 AIMessage 输出（前端 markdown 渲染）。
    """
    _emit_progress({"stage": "citation_check", "status": "started"})
    report_md = str(state.get("report_md") or "")
    sources = state.get("sources") or []
    total_sources = len(sources)

    markers = [int(match.group(1)) for match in CITATION_PATTERN.finditer(report_md)]
    invalid = sorted({marker for marker in markers if not 1 <= marker <= total_sources})
    if invalid:
        logger.warning(f"DeepReport 引用回验发现 {len(invalid)} 个无效标记，已剔除: {invalid}")
        report_md = CITATION_PATTERN.sub(
            lambda match: match.group(0) if 1 <= int(match.group(1)) <= total_sources else "",
            report_md,
        )

    valid_markers = [marker for marker in markers if 1 <= marker <= total_sources]
    cited = sorted(set(valid_markers))
    verification, unverified_ids = await _verify_citation_sources(context, sources, cited)
    citation_report = {
        "total_sources": total_sources,
        "total_markers": len(markers),
        "valid_markers": len(valid_markers),
        "invalid_markers": invalid,
        "cited_source_ids": cited,
        "uncited_source_ids": [idx for idx in range(1, total_sources + 1) if idx not in set(cited)],
        "source_verification": verification,
    }

    if cited:
        source_lines = "\n".join(_source_line(idx, sources[idx - 1], unverified=idx in unverified_ids) for idx in cited)
        final_report = f"{report_md.rstrip()}\n\n## 引用来源\n\n{source_lines}\n"
    else:
        final_report = f"{report_md.rstrip()}\n\n## 引用来源\n\n（本报告未包含知识库引用）\n"

    _emit_progress(
        {
            "stage": "citation_check",
            "status": "completed",
            "cited": len(cited),
            "verified": verification.get("verified", 0),
        }
    )
    return {
        "report_md": final_report,
        "citation_report": citation_report,
        "messages": [AIMessage(content=final_report)],
    }
