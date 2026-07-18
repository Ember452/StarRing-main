"""TASK_SYSTEM_PROMPT 和 TASK_TOOL_DESCRIPTION 的文本生成测试。

验证 P0 Orchestrator-Worker 改造后的 prompt 文本包含：
- Anthropic 四约束（decompose by aspect / bound depth / explicit deliverables / synthesis is reasoning）
- Effort-scaling 分配规则（简单/中等/复杂/超复杂任务）
- 子智能体返回格式说明（摘要/关键发现/引用来源/置信度/产物/原始文本）
- 工具描述中强调 Expected deliverable 字段
"""

from __future__ import annotations

from starring.agents.middlewares.subagent_task import (
    TASK_SYSTEM_PROMPT,
    TASK_TOOL_DESCRIPTION,
)


class TestTaskSystemPrompt:
    def test_contains_four_orchestrator_worker_constraints(self):
        """system prompt 包含 Anthropic Orchestrator-Worker 四约束的英文标题。"""
        assert "Decompose by aspect, not by step" in TASK_SYSTEM_PROMPT
        assert "Bound depth" in TASK_SYSTEM_PROMPT
        assert "Explicit deliverables" in TASK_SYSTEM_PROMPT
        assert "Synthesis is reasoning, not concatenation" in TASK_SYSTEM_PROMPT

    def test_contains_orchestrator_worker_section_header(self):
        """system prompt 包含 Orchestrator-Worker 编排约束章节。"""
        assert "Orchestrator-Worker 编排约束" in TASK_SYSTEM_PROMPT

    def test_contains_effort_scaling(self):
        """system prompt 包含 effort-scaling 分配规则。"""
        assert "Effort-scaling" in TASK_SYSTEM_PROMPT
        assert "简单任务" in TASK_SYSTEM_PROMPT
        assert "中等复杂度" in TASK_SYSTEM_PROMPT
        assert "复杂研究任务" in TASK_SYSTEM_PROMPT
        assert "超复杂任务" in TASK_SYSTEM_PROMPT

    def test_contains_deliverable_format_section(self):
        """system prompt 包含子智能体返回格式说明。"""
        assert "子智能体返回格式" in TASK_SYSTEM_PROMPT
        assert "摘要" in TASK_SYSTEM_PROMPT
        assert "关键发现" in TASK_SYSTEM_PROMPT
        assert "引用来源" in TASK_SYSTEM_PROMPT
        assert "置信度" in TASK_SYSTEM_PROMPT
        assert "产物文件" in TASK_SYSTEM_PROMPT
        assert "原始文本" in TASK_SYSTEM_PROMPT

    def test_contains_synthesis_guidance(self):
        """system prompt 包含合成指导：优先参考摘要和关键发现。"""
        assert "优先参考摘要和关键发现" in TASK_SYSTEM_PROMPT

    def test_contains_available_agents_placeholder(self):
        """system prompt 包含 {available_agents} 占位符供格式化。"""
        assert "{available_agents}" in TASK_SYSTEM_PROMPT

    def test_contains_no_indirect_subagent_call_guidance(self):
        """system prompt 强调不要通过 shell/curl/HTTP API 间接调用子智能体。"""
        assert "shell" in TASK_SYSTEM_PROMPT
        assert "curl" in TASK_SYSTEM_PROMPT
        assert "HTTP API" in TASK_SYSTEM_PROMPT


class TestTaskToolDescription:
    def test_contains_expected_deliverable_field(self):
        """工具描述强调 description 必须包含 Expected deliverable 字段。"""
        assert "Expected deliverable:" in TASK_TOOL_DESCRIPTION

    def test_contains_available_agents_placeholder(self):
        """工具描述包含 {available_agents} 占位符供格式化。"""
        assert "{available_agents}" in TASK_TOOL_DESCRIPTION

    def test_contains_thread_id_guidance(self):
        """工具描述包含 thread_id 使用说明。"""
        assert "thread_id" in TASK_TOOL_DESCRIPTION

    def test_contains_no_indirect_call_guidance(self):
        """工具描述强调不要通过 shell/curl/HTTP APIs/命令行间接调用。"""
        assert "shell" in TASK_TOOL_DESCRIPTION
        assert "curl" in TASK_TOOL_DESCRIPTION
        assert "HTTP APIs" in TASK_TOOL_DESCRIPTION
        assert "command-line" in TASK_TOOL_DESCRIPTION
