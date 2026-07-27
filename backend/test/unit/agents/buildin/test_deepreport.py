"""DeepReport 报告流水线单元测试。

覆盖计划中的关键行为：
- Outline 结构化解析与 fail-fast（无 KB / 空 mindmap）
- review resume 三分支（approve / 修改意见回 plan / 超过 max_replan 闸门）
- Send fan-out 构造（N 章生成 N 个 Send，携带章节序号）
- 研究员瞬时错误重试与 confidence 加权（自评 + 事实数量）
- 引用全局重编号与 citation_check 无效标记剔除 / 语义回验 / 只列被引用来源
- 合成阶段正文确定性拼接（LLM 只看摘要写引言/结论）与失败降级模板
- 图结构组装（五节点 + 条件边）

设计依据：docs/vibe/DeepReport报告流水线-20260727.md
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Send

from starring.agents.buildin.deepreport.context import DeepReportContext
from starring.agents.buildin.deepreport.state import (
    ChapterFact,
    ChapterResearch,
    ChapterResult,
    Outline,
    OutlineChapter,
    merge_chapters,
)
from starring.agents.middlewares.subagent_deliverable import SubAgentSource

NODES_MODULE = "starring.agents.buildin.deepreport.nodes"


def _make_context(**kwargs) -> DeepReportContext:
    ctx = DeepReportContext(**kwargs)
    setattr(ctx, "_visible_knowledge_bases", [{"kb_id": "kb-1", "name": "测试库", "description": "desc"}])
    return ctx


def _make_outline(n: int = 2) -> Outline:
    return Outline(
        title="测试报告",
        chapters=[OutlineChapter(id=f"ch-{i}", heading=f"章节{i}", brief=f"要点{i}") for i in range(1, n + 1)],
    )


# ---------------------------------------------------------------------------
# plan 节点：Outline 解析与 fail-fast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_fails_fast_without_knowledge_base():
    """无可访问知识库时 plan 节点应 fail-fast 报错。"""
    from starring.agents.buildin.deepreport.nodes import plan_node

    ctx = DeepReportContext()
    setattr(ctx, "_visible_knowledge_bases", [])
    state = {"messages": [HumanMessage(content="写一份报告")]}

    with pytest.raises(ValueError, match="至少一个可访问的知识库"):
        await plan_node(ctx, state)


@pytest.mark.asyncio
async def test_plan_fails_fast_when_all_mindmaps_empty():
    """所有知识库都没有思维导图时应 fail-fast 报错。"""
    from starring.agents.buildin.deepreport.nodes import plan_node

    ctx = _make_context()
    state = {"messages": [HumanMessage(content="写一份报告")]}

    repo = MagicMock()
    repo.get_by_kb_id = AsyncMock(return_value=MagicMock(mindmap=None))
    with patch(
        "starring.repositories.knowledge_base_repository.KnowledgeBaseRepository",
        return_value=repo,
    ):
        with pytest.raises(ValueError, match="思维导图"):
            await plan_node(ctx, state)


@pytest.mark.asyncio
async def test_plan_generates_normalized_outline():
    """plan 节点应产出规整后的大纲（重分配 ch-# ID、清空反馈）。"""
    from starring.agents.buildin.deepreport.nodes import plan_node

    ctx = _make_context(max_chapters=8)
    state = {"messages": [HumanMessage(content="写一份测试报告")], "review_feedback": ""}

    raw_outline = Outline(
        title="测试报告",
        chapters=[
            OutlineChapter(id="whatever", heading="第一章", brief="a"),
            OutlineChapter(id="", heading="第二章", brief="b"),
        ],
    )
    fake_llm = MagicMock()
    fake_llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=raw_outline)

    with (
        patch(f"{NODES_MODULE}._load_kb_structure", new=AsyncMock(return_value="- 结构")),
        patch(f"{NODES_MODULE}._load_llm", return_value=fake_llm),
    ):
        result = await plan_node(ctx, state)

    outline = result["outline"]
    assert result["review_feedback"] == ""
    assert [c.id for c in outline.chapters] == ["ch-1", "ch-2"]
    assert outline.title == "测试报告"


def test_normalize_outline_truncates_and_fails_on_empty():
    """normalize_outline 应按 max_chapters 截断，空大纲 fail-fast。"""
    from starring.agents.buildin.deepreport.nodes import normalize_outline

    outline = Outline(
        title="t",
        chapters=[OutlineChapter(heading=f"h{i}") for i in range(10)],
    )
    normalized = normalize_outline(outline, 3)
    assert len(normalized.chapters) == 3
    assert [c.id for c in normalized.chapters] == ["ch-1", "ch-2", "ch-3"]

    with pytest.raises(ValueError, match="没有有效章节"):
        normalize_outline(Outline(title="t", chapters=[]), 3)


# ---------------------------------------------------------------------------
# review 节点：三分支 + 答案解析
# ---------------------------------------------------------------------------


def test_parse_review_answer_variants():
    """答案解析：approve / Other 文本 / 列表 / 无法识别结构。"""
    from starring.agents.buildin.deepreport.nodes import REVIEW_QUESTION_ID, parse_review_answer

    assert parse_review_answer({REVIEW_QUESTION_ID: "approve"}) == ""
    assert parse_review_answer({REVIEW_QUESTION_ID: {"other": "多加一章风险分析"}}) == "多加一章风险分析"
    assert parse_review_answer({REVIEW_QUESTION_ID: ["approve"]}) == ""
    assert parse_review_answer("直接给的意见") == "直接给的意见"
    assert parse_review_answer(None) == ""
    assert parse_review_answer({}) == ""


def test_review_approve_branch():
    """批准分支：review_feedback 清空，不递增 replan_count。"""
    from starring.agents.buildin.deepreport.nodes import REVIEW_QUESTION_ID, review_node

    ctx = _make_context()
    state = {"outline": _make_outline(), "replan_count": 0}

    with patch(f"{NODES_MODULE}.interrupt", return_value={REVIEW_QUESTION_ID: "approve"}):
        result = review_node(ctx, state)

    assert result == {"review_feedback": ""}


def test_review_feedback_branch_increments_replan_count():
    """修改意见分支：记录反馈并递增 replan_count（回 plan 重新规划）。"""
    from starring.agents.buildin.deepreport.nodes import REVIEW_QUESTION_ID, review_node

    ctx = _make_context(max_replan=2)
    state = {"outline": _make_outline(), "replan_count": 0}

    with patch(
        f"{NODES_MODULE}.interrupt",
        return_value={REVIEW_QUESTION_ID: {"other": "合并前两章"}},
    ):
        result = review_node(ctx, state)

    assert result["review_feedback"] == "合并前两章"
    assert result["replan_count"] == 1


def test_review_gate_when_max_replan_exceeded():
    """闸门分支：达到 max_replan 后忽略修改意见，直接采用当前大纲。"""
    from starring.agents.buildin.deepreport.nodes import REVIEW_QUESTION_ID, review_node

    ctx = _make_context(max_replan=2)
    state = {"outline": _make_outline(), "replan_count": 2}

    with patch(
        f"{NODES_MODULE}.interrupt",
        return_value={REVIEW_QUESTION_ID: {"other": "再改一版"}},
    ):
        result = review_node(ctx, state)

    assert result == {"review_feedback": ""}


def test_review_interrupt_payload_uses_ask_user_question_contract():
    """interrupt payload 必须符合 ask_user_question 契约（前端零改动渲染）。"""
    from starring.agents.buildin.deepreport.nodes import review_node

    ctx = _make_context()
    state = {"outline": _make_outline(), "replan_count": 0}
    captured: dict = {}

    def _fake_interrupt(payload):
        captured.update(payload)
        return "approve"

    with patch(f"{NODES_MODULE}.interrupt", side_effect=_fake_interrupt):
        review_node(ctx, state)

    assert captured["source"] == "ask_user_question"
    questions = captured["questions"]
    assert len(questions) == 1
    assert questions[0]["allow_other"] is True
    assert questions[0]["options"][0]["value"] == "approve"
    assert "章节1" in questions[0]["question"]


# ---------------------------------------------------------------------------
# 条件路由：Send fan-out
# ---------------------------------------------------------------------------


def test_route_after_review_returns_plan_on_feedback():
    from starring.agents.buildin.deepreport.nodes import route_after_review

    state = {"review_feedback": "改大纲", "outline": _make_outline()}
    assert route_after_review(state) == "plan"


def test_route_after_review_fans_out_sends_per_chapter():
    """N 章大纲应生成 N 个 Send，payload 携带章节、报告标题与章节序号。"""
    from starring.agents.buildin.deepreport.nodes import route_after_review

    state = {"review_feedback": "", "outline": _make_outline(3)}
    sends = route_after_review(state)

    assert len(sends) == 3
    assert all(isinstance(item, Send) and item.node == "research_chapter" for item in sends)
    assert sends[0].arg["chapter"]["id"] == "ch-1"
    assert sends[2].arg["chapter"]["heading"] == "章节3"
    assert sends[0].arg["report_title"] == "测试报告"
    assert sends[0].arg["chapter_index"] == 1 and sends[2].arg["chapter_index"] == 3
    assert all(item.arg["total_chapters"] == 3 for item in sends)


def test_route_after_review_fails_on_empty_outline():
    from starring.agents.buildin.deepreport.nodes import route_after_review

    with pytest.raises(ValueError, match="大纲为空"):
        route_after_review({"review_feedback": "", "outline": None})


# ---------------------------------------------------------------------------
# research_chapter 节点：结构化事实 → 写作 → 失败占位
# ---------------------------------------------------------------------------


def _fact(statement: str, file_id: str = "f-1") -> ChapterFact:
    return ChapterFact(
        statement=statement,
        source=SubAgentSource(type="kb_chunk", file_id=file_id, snippet=f"原文：{statement}"),
    )


@pytest.mark.asyncio
async def test_research_chapter_success():
    """研究/写作双阶段成功：产出带 sources 的 ChapterResult。"""
    from starring.agents.buildin.deepreport.nodes import research_chapter_node

    ctx = _make_context()
    payload = {"chapter": {"id": "ch-1", "heading": "背景", "brief": "b"}, "report_title": "测试报告"}
    research = ChapterResearch(facts=[_fact("事实一"), _fact("事实二")])

    with (
        patch(f"{NODES_MODULE}._run_researcher", new=AsyncMock(return_value=research)),
        patch(f"{NODES_MODULE}._run_writer", new=AsyncMock(return_value="正文 [S1] 和 [S2]")),
    ):
        result = await research_chapter_node(ctx, payload)

    chapter = result["chapters"][0]
    assert chapter.chapter_id == "ch-1"
    assert chapter.content_md == "正文 [S1] 和 [S2]"
    assert len(chapter.sources) == 2
    # 加权公式：0.6 * 自评(默认 0.5) + 0.4 * min(1, 2/6)
    assert chapter.confidence == 0.43


@pytest.mark.asyncio
async def test_research_chapter_confidence_weights_self_report():
    """confidence 加权：研究员自评主导，事实数量兜底修正。"""
    from starring.agents.buildin.deepreport.nodes import research_chapter_node

    ctx = _make_context()
    payload = {"chapter": {"id": "ch-1", "heading": "背景", "brief": "b"}, "report_title": "t"}
    research = ChapterResearch(facts=[_fact(f"事实{i}") for i in range(6)], confidence=1.0)

    with (
        patch(f"{NODES_MODULE}._run_researcher", new=AsyncMock(return_value=research)),
        patch(f"{NODES_MODULE}._run_writer", new=AsyncMock(return_value="正文")),
    ):
        result = await research_chapter_node(ctx, payload)

    assert result["chapters"][0].confidence == 1.0


@pytest.mark.asyncio
async def test_run_researcher_retries_once_on_transient_error():
    """研究员首次失败后应重试一次并成功返回。"""
    from starring.agents.buildin.deepreport.nodes import _run_researcher

    ctx = _make_context()
    research = ChapterResearch(facts=[_fact("事实一")])
    fake_researcher = MagicMock()
    fake_researcher.ainvoke = AsyncMock(side_effect=[RuntimeError("网络抖动"), {"structured_response": research}])

    with (
        patch(f"{NODES_MODULE}._load_llm", return_value=MagicMock()),
        patch(f"{NODES_MODULE}.create_agent", return_value=fake_researcher),
        patch(f"{NODES_MODULE}._RESEARCH_RETRY_DELAY_SECONDS", 0),
    ):
        result = await _run_researcher(ctx, {"id": "ch-1", "heading": "背景"}, "t")

    assert result is research
    assert fake_researcher.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_run_researcher_raises_after_max_attempts():
    """重试耗尽后应抛出最后一次错误（由 research_chapter_node 写入占位）。"""
    from starring.agents.buildin.deepreport.nodes import _run_researcher

    ctx = _make_context()
    fake_researcher = MagicMock()
    fake_researcher.ainvoke = AsyncMock(side_effect=RuntimeError("持续失败"))

    with (
        patch(f"{NODES_MODULE}._load_llm", return_value=MagicMock()),
        patch(f"{NODES_MODULE}.create_agent", return_value=fake_researcher),
        patch(f"{NODES_MODULE}._RESEARCH_RETRY_DELAY_SECONDS", 0),
    ):
        with pytest.raises(RuntimeError, match="持续失败"):
            await _run_researcher(ctx, {"id": "ch-1", "heading": "背景"}, "t")

    assert fake_researcher.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_research_chapter_failure_writes_placeholder():
    """单章失败不炸全局：写入 confidence=0 的占位结果并注明原因。"""
    from starring.agents.buildin.deepreport.nodes import research_chapter_node

    ctx = _make_context()
    payload = {"chapter": {"id": "ch-1", "heading": "背景", "brief": ""}, "report_title": "t"}

    with patch(f"{NODES_MODULE}._run_researcher", new=AsyncMock(side_effect=RuntimeError("检索超时"))):
        result = await research_chapter_node(ctx, payload)

    chapter = result["chapters"][0]
    assert chapter.confidence == 0.0
    assert "检索超时" in chapter.content_md
    assert chapter.sources == []


@pytest.mark.asyncio
async def test_research_chapter_no_facts_placeholder():
    """研究员未收集到事实时写入占位结果，不进入写作阶段。"""
    from starring.agents.buildin.deepreport.nodes import research_chapter_node

    ctx = _make_context()
    payload = {"chapter": {"id": "ch-2", "heading": "现状", "brief": ""}, "report_title": "t"}

    writer = AsyncMock()
    with (
        patch(f"{NODES_MODULE}._run_researcher", new=AsyncMock(return_value=ChapterResearch(facts=[]))),
        patch(f"{NODES_MODULE}._run_writer", new=writer),
    ):
        result = await research_chapter_node(ctx, payload)

    assert result["chapters"][0].confidence == 0.0
    writer.assert_not_awaited()


def test_merge_chapters_reducer_dedupes_by_chapter_id():
    """chapters reducer 按 chapter_id 合并，新结果覆盖旧结果。"""
    old = [ChapterResult(chapter_id="ch-1", content_md="旧")]
    new = [ChapterResult(chapter_id="ch-1", content_md="新"), ChapterResult(chapter_id="ch-2")]
    merged = merge_chapters(old, new)
    assert len(merged) == 2
    assert {c.chapter_id: c.content_md for c in merged}["ch-1"] == "新"


# ---------------------------------------------------------------------------
# synthesize：全局引用重编号 + 降级模板
# ---------------------------------------------------------------------------


def _chapter_result(chapter_id: str, heading: str, content: str, n_sources: int) -> ChapterResult:
    return ChapterResult(
        chapter_id=chapter_id,
        heading=heading,
        content_md=content,
        sources=[SubAgentSource(type="kb_chunk", file_id=f"{chapter_id}-f{i}") for i in range(n_sources)],
    )


def test_renumber_citations_global_offsets_and_strips_invalid():
    """第二章的局部 [S1] 应重编号为全局 [S3]；超范围局部标记应剔除。"""
    from starring.agents.buildin.deepreport.nodes import renumber_citations

    chapters = [
        _chapter_result("ch-1", "一", "甲 [S1] 乙 [S2] 编造 [S9]", 2),
        _chapter_result("ch-2", "二", "丙 [S1]", 1),
    ]
    sources, sections = renumber_citations(chapters)

    assert len(sources) == 3
    assert sections[0][1] == "甲 [S1] 乙 [S2] 编造 "
    assert sections[1][1] == "丙 [S3]"


@pytest.mark.asyncio
async def test_synthesize_falls_back_to_template_on_llm_failure():
    """引言/结论 LLM 失败时降级为确定性模板拼接（标题 + 各章正文）。"""
    from starring.agents.buildin.deepreport.nodes import synthesize_node

    ctx = _make_context()
    state = {
        "outline": _make_outline(2),
        "chapters": [
            _chapter_result("ch-1", "章节1", "第一章内容 [S1]", 1),
            _chapter_result("ch-2", "章节2", "第二章内容 [S1]", 1),
        ],
    }

    fake_llm = MagicMock()
    fake_llm.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=RuntimeError("模型超时"))
    with patch(f"{NODES_MODULE}._load_llm", return_value=fake_llm):
        result = await synthesize_node(ctx, state)

    report = result["report_md"]
    assert report.startswith("# 测试报告")
    assert "## 章节1" in report and "## 章节2" in report
    assert "[S1]" in report and "[S2]" in report  # 全局重编号后
    assert len(result["sources"]) == 2


@pytest.mark.asyncio
async def test_synthesize_deterministic_body_with_llm_framing():
    """正文由确定性拼接（引用不经 LLM），LLM 只看摘要产出引言/结论。"""
    from starring.agents.buildin.deepreport.nodes import synthesize_node
    from starring.agents.buildin.deepreport.state import ReportFraming

    ctx = _make_context()
    state = {
        "outline": _make_outline(2),
        "chapters": [_chapter_result("ch-1", "章节1", "内容 [S1]", 1)],  # ch-2 缺失
    }

    framing = ReportFraming(introduction="这是引言 [S1]", conclusion="这是结论")
    fake_llm = MagicMock()
    fake_llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=framing)
    with patch(f"{NODES_MODULE}._load_llm", return_value=fake_llm):
        result = await synthesize_node(ctx, state)

    report = result["report_md"]
    assert report.startswith("# 测试报告")
    assert "这是引言" in report and "这是引言 [S1]" not in report  # 引言中的引用标记被剔除
    assert "## 章节1\n\n内容 [S1]" in report  # 正文原样拼接，引用保留
    assert "## 结论\n\n这是结论" in report
    # LLM 只见摘要：引用标记已剔除，缺失章节占位也在摘要中
    prompt_arg = fake_llm.with_structured_output.return_value.ainvoke.await_args.args[0]
    assert "[S1]" not in prompt_arg
    assert "未产出内容" in prompt_arg


def test_chapter_summary_strips_citations_and_truncates():
    """章节摘要：剔除 [S#]、压空白、按上限截断。"""
    from starring.agents.buildin.deepreport.nodes import _chapter_summary

    text = "甲 [S1]\n\n乙  [S2]  丙" + "长" * 400
    summary = _chapter_summary(text, max_chars=50)
    assert "[S1]" not in summary and "\n" not in summary
    assert summary.startswith("甲 乙 丙")
    assert len(summary) == 50


# ---------------------------------------------------------------------------
# citation_check：无效标记剔除 + 语义回验 + 引用来源章节
# ---------------------------------------------------------------------------

_VERIFY_ALL_PASS = ({"mode": "find_file_content", "checked": 2, "verified": 2, "unverified": [], "skipped": []}, set())


@pytest.mark.asyncio
async def test_citation_check_strips_invalid_and_appends_sources():
    from starring.agents.buildin.deepreport.nodes import citation_check_node

    ctx = _make_context()
    sources = [
        SubAgentSource(type="kb_chunk", file_id="f-1", snippet="片段一"),
        SubAgentSource(type="kb_chunk", file_id="f-2", snippet="片段二"),
    ]
    state = {"report_md": "# 报告\n\n事实甲 [S1]，编造 [S7]，事实乙 [S2]", "sources": sources}

    with patch(f"{NODES_MODULE}._verify_citation_sources", new=AsyncMock(return_value=_VERIFY_ALL_PASS)):
        result = await citation_check_node(ctx, state)

    report = result["report_md"]
    assert "[S7]" not in report
    assert "[S1]" in report and "[S2]" in report
    assert "## 引用来源" in report
    assert "f-1" in report and "f-2" in report
    assert "（未回验）" not in report

    cr = result["citation_report"]
    assert cr["total_sources"] == 2
    assert cr["total_markers"] == 3
    assert cr["valid_markers"] == 2
    assert cr["invalid_markers"] == [7]
    assert cr["cited_source_ids"] == [1, 2]
    assert cr["uncited_source_ids"] == []
    assert cr["source_verification"]["verified"] == 2

    final_message = result["messages"][0]
    assert isinstance(final_message, AIMessage)
    assert final_message.content == report


@pytest.mark.asyncio
async def test_citation_check_lists_only_cited_and_marks_unverified():
    """引用来源章节只列被引用的来源；回验未命中的条目加标注。"""
    from starring.agents.buildin.deepreport.nodes import citation_check_node

    ctx = _make_context()
    sources = [
        SubAgentSource(type="kb_chunk", file_id="f-1", snippet="片段一"),
        SubAgentSource(type="kb_chunk", file_id="f-2", snippet="片段二"),
        SubAgentSource(type="kb_chunk", file_id="f-3", snippet="片段三"),
    ]
    state = {"report_md": "# 报告\n\n甲 [S1] 乙 [S2]", "sources": sources}
    verification = (
        {"mode": "find_file_content", "checked": 2, "verified": 1, "unverified": [2], "skipped": []},
        {2},
    )

    with patch(f"{NODES_MODULE}._verify_citation_sources", new=AsyncMock(return_value=verification)):
        result = await citation_check_node(ctx, state)

    report = result["report_md"]
    assert "f-1" in report and "f-2" in report
    assert "f-3" not in report  # 未被引用的来源不进引用来源章节
    assert "f-2（未回验）" in report
    assert result["citation_report"]["uncited_source_ids"] == [3]


@pytest.mark.asyncio
async def test_citation_check_handles_no_sources():
    from starring.agents.buildin.deepreport.nodes import citation_check_node

    ctx = _make_context()
    result = await citation_check_node(ctx, {"report_md": "# 报告\n\n无引用正文 [S1]", "sources": []})

    assert "[S1]" not in result["report_md"]
    assert "未包含知识库引用" in result["report_md"]
    assert result["citation_report"]["invalid_markers"] == [1]
    assert result["citation_report"]["source_verification"]["mode"] == "skipped"


@pytest.mark.asyncio
async def test_verify_citation_sources_skips_without_kb():
    """无可见知识库时直接跳过回验，不发起任何查询。"""
    from starring.agents.buildin.deepreport.nodes import _verify_citation_sources

    ctx = DeepReportContext()
    setattr(ctx, "_visible_knowledge_bases", [])
    sources = [SubAgentSource(type="kb_chunk", file_id="f-1", snippet="片段")]

    stats, unverified = await _verify_citation_sources(ctx, sources, [1])
    assert stats["mode"] == "skipped"
    assert unverified == set()


@pytest.mark.asyncio
async def test_verify_citation_sources_filters_types_and_reports_misses():
    """只回验 kb_chunk/file 类型的被引用来源；url 类型进 skipped；未命中进 unverified。"""
    from starring.agents.buildin.deepreport.nodes import _verify_citation_sources

    ctx = _make_context()
    sources = [
        SubAgentSource(type="kb_chunk", file_id="f-1", snippet="片段一"),
        SubAgentSource(type="url", url="https://example.com"),
        SubAgentSource(type="kb_chunk", file_id="f-3", snippet="片段三"),
    ]

    async def _fake_verify(kb_ids, source):
        return source.file_id == "f-1"

    with patch(f"{NODES_MODULE}._verify_source_in_kbs", new=_fake_verify):
        stats, unverified = await _verify_citation_sources(ctx, sources, [1, 2, 3])

    assert stats["checked"] == 2 and stats["verified"] == 1
    assert stats["skipped"] == [2]
    assert stats["unverified"] == [3]
    assert unverified == {3}


def test_snippet_fragment_picks_longest_line():
    from starring.agents.buildin.deepreport.nodes import _snippet_fragment

    assert _snippet_fragment("短\n这是最长的一行内容\n中等长度") == "这是最长的一行内容"
    assert _snippet_fragment("") == ""
    assert _snippet_fragment("x" * 100, max_len=30) == "x" * 30


# ---------------------------------------------------------------------------
# 进度事件
# ---------------------------------------------------------------------------


def test_emit_progress_writes_payload_with_source():
    """进度事件带 source 标识；无 stream writer（非流式调用）时静默跳过。"""
    from starring.agents.buildin.deepreport.nodes import PROGRESS_SOURCE, _emit_progress

    written: list[dict] = []
    with patch(f"{NODES_MODULE}.get_stream_writer", return_value=written.append):
        _emit_progress({"stage": "plan", "status": "started"})
    assert written == [{"source": PROGRESS_SOURCE, "stage": "plan", "status": "started"}]

    # 单测直调节点时 get_stream_writer 抛错，不得影响主链路
    with patch(f"{NODES_MODULE}.get_stream_writer", side_effect=RuntimeError("no runtime")):
        _emit_progress({"stage": "plan", "status": "started"})


# ---------------------------------------------------------------------------
# 图结构组装
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_graph_wires_five_nodes():
    """五阶段流水线图应包含全部节点，且从 plan 入口。"""
    from langgraph.checkpoint.memory import InMemorySaver

    from starring.agents.buildin.deepreport.backend import DeepReportAgent

    agent = DeepReportAgent()
    ctx = _make_context()

    async def _identity_context(context, **kwargs):
        return context

    with (
        patch("starring.agents.context.prepare_agent_runtime_context", new=_identity_context),
        patch.object(DeepReportAgent, "_get_checkpointer", new=AsyncMock(return_value=InMemorySaver())),
    ):
        graph = await agent.get_graph(context=ctx)

    nodes = set(graph.get_graph().nodes)
    assert {"plan", "review", "research_chapter", "synthesize", "citation_check"} <= nodes
