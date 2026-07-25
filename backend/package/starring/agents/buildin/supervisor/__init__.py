"""Supervisor 智能体 backend。

P1-A 软编排 Supervisor 模式：强制通过 task 工具委派给子智能体，
不挂载本地工具（KB / Skills / 文件系统等本地工具全部禁用）。

与 ChatbotAgent（Orchestrator-Worker）的角色边界：
- ChatbotAgent：本地工具 + task 工具，LLM 自主决定是否委派
- SupervisorAgent：仅 task 工具，强制委派给子智能体

设计依据：docs/vibe/P1-A-Supervisor细化设计-20260719.md
"""

from .backend import SupervisorAgent

__all__ = ["SupervisorAgent"]
