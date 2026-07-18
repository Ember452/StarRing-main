"""subagent_deliverable.py 的 Pydantic 模型单测 + subagent_task.py 解析/渲染函数单测。

测试覆盖：
- SubAgentDeliverable / SubAgentSource 的 schema 校验
- summary validator 兜底逻辑（空 summary 时从 raw_text 取首段）
- confidence / schema_version 边界值
- EMPTY_DELIVERABLE 常量
- _parse_deliverable：fenced block 解析、三层兜底、artifacts 合并、raw_text 截断
- _deliverable_to_markdown：渲染 markdown，不渲染 raw_text
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from starring.agents.middlewares.subagent_deliverable import (
    EMPTY_DELIVERABLE,
    SubAgentDeliverable,
    SubAgentSource,
)
from starring.agents.middlewares.subagent_task import (
    _deliverable_to_markdown,
    _parse_deliverable,
)


class TestSubAgentSource:
    def test_default_values(self):
        src = SubAgentSource(snippet="示例片段")
        assert src.type == "other"
        assert src.file_id is None
        assert src.chunk_id is None
        assert src.url is None
        assert src.snippet == "示例片段"

    def test_kb_chunk_type(self):
        src = SubAgentSource(
            type="kb_chunk",
            file_id="file-001",
            chunk_id="chunk-001",
            snippet="知识库片段内容",
        )
        assert src.type == "kb_chunk"
        assert src.file_id == "file-001"
        assert src.chunk_id == "chunk-001"

    def test_url_type(self):
        src = SubAgentSource(type="url", url="https://example.com/doc", snippet="URL 内容")
        assert src.type == "url"
        assert src.url == "https://example.com/doc"

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            SubAgentSource(type="invalid_type", snippet="x")


class TestSubAgentDeliverableSchema:
    def test_default_values(self):
        d = SubAgentDeliverable()
        assert d.schema_version == "1"
        assert d.summary == ""
        assert d.key_findings == []
        assert d.sources == []
        assert d.confidence == 0.5
        assert d.raw_text == ""
        assert d.artifacts == []

    def test_full_valid_payload(self):
        d = SubAgentDeliverable(
            summary="测试摘要",
            key_findings=["发现1", "发现2"],
            sources=[SubAgentSource(type="kb_chunk", file_id="f1", chunk_id="c1", snippet="片段")],
            confidence=0.85,
            raw_text="原始文本",
            artifacts=["/sandbox/file.txt"],
        )
        assert d.summary == "测试摘要"
        assert d.key_findings == ["发现1", "发现2"]
        assert d.confidence == 0.85
        assert len(d.sources) == 1
        assert d.artifacts == ["/sandbox/file.txt"]

    def test_confidence_lower_bound(self):
        d = SubAgentDeliverable(confidence=0.0)
        assert d.confidence == 0.0

    def test_confidence_upper_bound(self):
        d = SubAgentDeliverable(confidence=1.0)
        assert d.confidence == 1.0

    def test_confidence_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            SubAgentDeliverable(confidence=-0.1)

    def test_confidence_above_one_rejected(self):
        with pytest.raises(ValidationError):
            SubAgentDeliverable(confidence=1.5)

    def test_schema_version_must_be_one(self):
        d = SubAgentDeliverable()
        assert d.schema_version == "1"

    def test_schema_version_other_value_rejected(self):
        with pytest.raises(ValidationError):
            SubAgentDeliverable(schema_version="2")

    def test_schema_version_explicit_one(self):
        d = SubAgentDeliverable(schema_version="1")
        assert d.schema_version == "1"


class TestSummaryFallbackValidator:
    """summary validator 的兜底逻辑：summary 为空时从 raw_text 取首段。"""

    def test_summary_provided_unchanged(self):
        d = SubAgentDeliverable(summary="用户提供的摘要", raw_text="原始文本")
        assert d.summary == "用户提供的摘要"

    def test_summary_empty_raw_text_provided_takes_first_paragraph(self):
        raw = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
        d = SubAgentDeliverable(summary="", raw_text=raw)
        assert d.summary == "第一段内容。"

    def test_summary_whitespace_only_raw_text_provided_takes_first_paragraph(self):
        raw = "  \n  \n第一段内容。\n\n第二段。"
        d = SubAgentDeliverable(summary="   ", raw_text=raw)
        assert d.summary == "第一段内容。"

    def test_summary_empty_raw_text_empty_stays_empty(self):
        d = SubAgentDeliverable(summary="", raw_text="")
        assert d.summary == ""

    def test_summary_empty_raw_text_whitespace_only_stays_empty(self):
        d = SubAgentDeliverable(summary="", raw_text="  \n  \n  ")
        assert d.summary == ""

    def test_first_paragraph_truncated_when_too_long(self):
        long_first_paragraph = "x" * 300
        raw = f"{long_first_paragraph}\n\n第二段。"
        d = SubAgentDeliverable(summary="", raw_text=raw)
        assert d.summary.endswith("...")
        assert len(d.summary) <= 203  # 200 + "..."

    def test_short_first_paragraph_not_truncated(self):
        raw = "短摘要。\n\n其他。"
        d = SubAgentDeliverable(summary="", raw_text=raw)
        assert d.summary == "短摘要。"
        assert "..." not in d.summary


class TestEmptyDeliverableConstant:
    def test_empty_deliverable_summary(self):
        assert EMPTY_DELIVERABLE.summary == "子智能体已完成任务，但未返回结构化结果。"

    def test_empty_deliverable_defaults(self):
        assert EMPTY_DELIVERABLE.schema_version == "1"
        assert EMPTY_DELIVERABLE.key_findings == []
        assert EMPTY_DELIVERABLE.sources == []
        assert EMPTY_DELIVERABLE.confidence == 0.5
        assert EMPTY_DELIVERABLE.raw_text == ""
        assert EMPTY_DELIVERABLE.artifacts == []

    def test_empty_deliverable_is_valid_instance(self):
        assert isinstance(EMPTY_DELIVERABLE, SubAgentDeliverable)

    def test_empty_deliverable_model_copy_update_artifacts(self):
        """EMPTY_DELIVERABLE 用于 _parse_deliverable 兜底时，通过 model_copy 注入 artifacts。"""
        d = EMPTY_DELIVERABLE.model_copy(update={"artifacts": ["/path/a", "/path/b"]})
        assert d.artifacts == ["/path/a", "/path/b"]
        # 原常量未被修改
        assert EMPTY_DELIVERABLE.artifacts == []


class TestParseDeliverable:
    """测试 _parse_deliverable(messages, artifacts_from_state)。

    H2 修订：第一个参数是 messages 列表（扫描所有 AIMessage.text），
    不是单个字符串。测试时需用 [AIMessage(text=...)] 包装。
    """

    def test_parse_valid_fenced_block(self):
        """正常解析 ```subagent-result``` fenced block。"""
        text = '''前缀说明

```subagent-result
{
  "summary": "测试摘要",
  "key_findings": ["发现1", "发现2"],
  "confidence": 0.9
}
```

后缀说明'''
        messages = [AIMessage(text=text)]
        d = _parse_deliverable(messages, artifacts_from_state=[])
        assert d.summary == "测试摘要"
        assert d.key_findings == ["发现1", "发现2"]
        assert d.confidence == 0.9
        # 有结构化输出时 raw_text 默认为空（fenced block 未声明 raw_text）
        assert d.raw_text == ""

    def test_parse_fenced_block_after_explanation_aimessage(self):
        """H2 修订核心场景：fenced block 后还有解释 AIMessage 时仍能正确解析。

        子智能体可能输出 fenced block 后继续输出"我已完成结构化输出"等解释，
        最后一个 AIMessage 不是 fenced block——H2 修订扫描所有 AIMessage 解决此问题。
        """
        text_with_fenced = '''```subagent-result
{
  "summary": "测试摘要",
  "confidence": 0.9
}
```'''
        text_explanation = "我已完成结构化输出，请查收。"
        # 倒序排列：最近的解释 AIMessage 在最后
        messages = [AIMessage(text=text_with_fenced), AIMessage(text=text_explanation)]
        d = _parse_deliverable(messages, artifacts_from_state=[])
        assert d.summary == "测试摘要"
        assert d.confidence == 0.9

    def test_parse_no_fenced_block_fallback(self):
        """没有 fenced block 时兜底到 raw_text。"""
        text = "这是子智能体的自然语言回复，没有结构化输出。"
        messages = [AIMessage(text=text)]
        d = _parse_deliverable(messages, artifacts_from_state=[])
        # summary validator 从 raw_text 取首段
        assert d.summary.startswith("这是子智能体")
        assert d.raw_text == text
        assert d.key_findings == []
        assert d.confidence == 0.5

    def test_parse_invalid_json_fallback(self):
        """fenced block 存在但 JSON 解析失败时兜底。"""
        text = """```subagent-result
{invalid json here
```
"""
        messages = [AIMessage(text=text)]
        d = _parse_deliverable(messages, artifacts_from_state=[])
        assert d.raw_text == text
        assert d.summary  # 不为空，从 raw_text 取首段

    def test_parse_pydantic_validation_failure_fallback(self):
        """JSON 合法但不符合 schema 时兜底（如 confidence 越界）。"""
        text = """```subagent-result
{
  "summary": "x",
  "confidence": 2.0
}
```"""
        messages = [AIMessage(text=text)]
        d = _parse_deliverable(messages, artifacts_from_state=[])
        # 兜底：raw_text 保留原文，summary 从原文取首段
        assert d.raw_text == text
        assert d.summary

    def test_parse_artifacts_merge_with_state(self):
        """fenced block 中的 artifacts 与 state.artifacts 合并去重，保序。"""
        text = """```subagent-result
{
  "summary": "x",
  "artifacts": ["/path/a", "/path/b"]
}
```"""
        messages = [AIMessage(text=text)]
        d = _parse_deliverable(messages, artifacts_from_state=["/path/b", "/path/c"])
        # fenced block 中的优先，state 补充，去重保序
        assert d.artifacts == ["/path/a", "/path/b", "/path/c"]

    def test_parse_empty_messages(self):
        """完全无 AIMessage 输出时返回 EMPTY_DELIVERABLE（L3 修订）。"""
        d = _parse_deliverable(messages=[], artifacts_from_state=["/path/x"])
        assert d.summary == EMPTY_DELIVERABLE.summary
        # artifacts 仍从 state 注入
        assert d.artifacts == ["/path/x"]

    def test_parse_messages_without_aimessage(self):
        """messages 列表非空但没有 AIMessage 时也兜底。"""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="非 AIMessage")]
        d = _parse_deliverable(messages, artifacts_from_state=[])
        assert d.summary == EMPTY_DELIVERABLE.summary
        assert d.artifacts == []

    def test_parse_raw_text_truncated_when_too_long(self):
        """N4 修订：兜底路径 raw_text 超长时截断到 5KB。"""
        long_text = "x" * 6000  # 6KB，超过 5KB 阈值
        messages = [AIMessage(text=long_text)]
        d = _parse_deliverable(messages, artifacts_from_state=[])
        assert len(d.raw_text) < 6000  # 已截断
        assert d.raw_text.endswith("...[truncated]")
        # 截断后长度 = 5000 + "\n...[truncated]" 的字符数
        assert len(d.raw_text) == 5000 + len("\n...[truncated]")


class TestDeliverableToMarkdown:
    """测试 _deliverable_to_markdown(deliverable, child_thread_id, subagent_type)。"""

    def test_render_full_deliverable(self):
        """完整 deliverable 渲染为 markdown（H3 修订：不渲染 raw_text）。"""
        d = SubAgentDeliverable(
            summary="测试摘要",
            key_findings=["发现1"],
            sources=[SubAgentSource(type="kb_chunk", file_id="f1", chunk_id="c1", snippet="片段")],
            confidence=0.85,
            artifacts=["/sandbox/file.txt"],
            raw_text="原始文本",
        )
        md = _deliverable_to_markdown(d, "thread-123", "researcher")
        assert "子智能体线程 ID: thread-123" in md
        assert "子智能体类型: researcher" in md
        assert "置信度: 0.85" in md
        assert "## 摘要" in md
        assert "测试摘要" in md
        assert "## 关键发现" in md
        assert "- 发现1" in md
        assert "## 引用来源" in md
        assert "[知识库]" in md
        assert "## 产物文件" in md
        assert "/sandbox/file.txt" in md
        # H3 修订：raw_text 不渲染到 ToolMessage（避免 token 膨胀）
        assert "## 原始完整文本" not in md
        assert "原始文本" not in md  # raw_text 内容不应出现在 markdown 中

    def test_render_minimal_deliverable(self):
        """最小 deliverable 渲染（无 findings/sources/artifacts）。"""
        d = SubAgentDeliverable(summary="只有摘要", confidence=0.5)
        md = _deliverable_to_markdown(d, "t1", "sub")
        assert "## 摘要" in md
        assert "只有摘要" in md
        assert "## 关键发现" not in md
        assert "## 引用来源" not in md
        assert "## 产物文件" not in md

    def test_render_url_source(self):
        """URL 类型 source 渲染。"""
        d = SubAgentDeliverable(
            summary="x",
            sources=[SubAgentSource(type="url", url="https://example.com", snippet="内容")],
        )
        md = _deliverable_to_markdown(d, "t", "s")
        assert "[URL]" in md
        assert "https://example.com" in md

    def test_render_file_source(self):
        """文件类型 source 渲染。"""
        d = SubAgentDeliverable(
            summary="x",
            sources=[SubAgentSource(type="file", file_id="file-001", snippet="内容")],
        )
        md = _deliverable_to_markdown(d, "t", "s")
        assert "[文件]" in md
        assert "file-001" in md

    def test_render_confidence_format(self):
        """置信度保留两位小数。"""
        d = SubAgentDeliverable(summary="x", confidence=0.123)
        md = _deliverable_to_markdown(d, "t", "s")
        assert "置信度: 0.12" in md
